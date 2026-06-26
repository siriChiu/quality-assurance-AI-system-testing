from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .contracts import list_contract_paths, load_contract
from .gitea_ledger import record_gitea_mcp_write_request, write_ledger_path
from .issues import issue_status
from .runner import utc_now
from .write_gate import evaluate_write_gate

ISSUES_REPORT_JSON_NAME = "issues-report.json"
ISSUES_REPORT_MD_NAME = "issues-report.md"
ISSUE_EVIDENCE_WRITE_REQUEST_NAME = "issue-evidence-update-request.json"
ISSUE_EVIDENCE_WRITE_RESULT_NAME = "issue-evidence-update-result.json"


def render_status_report(results: list[dict[str, Any]], report_path: Path, *, latest_run: dict[str, Any] | None = None) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    official = [result for result in results if not result.get("partial_probe")]
    partial = [result for result in results if result.get("partial_probe")]
    official_counts = _count_results(official)
    partial_counts = _count_results(partial)
    stale_reason = _stale_reason(official, latest_run)
    lines = [
        "# AI Quality Pilot status",
        "",
        f"- Generated at: {utc_now()}",
        f"- Source run: {_source_run_id(latest_run)}",
        f"- Source status: {_source_status(latest_run)}",
        "",
        "## Official Case Counters",
        "",
        "| PASS | FAIL | BLOCK | ABORT | NOT_RUN |",
        "|---:|---:|---:|---:|---:|",
        f"| {official_counts['PASS']} | {official_counts['FAIL']} | {official_counts['BLOCK']} | {official_counts['ABORT']} | {official_counts['NOT_RUN']} |",
        "",
        "## Stale Report Check",
        "",
        f"- Status: {'STALE' if stale_reason else 'CURRENT'}",
    ]
    if stale_reason:
        lines.append(f"- Stale report warning: {stale_reason}")
    lines.extend([
        "",
        "## Official Case Results",
        "",
        "| Case | Status | Commands | Evidence |",
        "|---|---|---:|---|",
    ])
    if not official:
        lines.append("| - | NOT_RUN | 0 | No official case results were available |")
    for result in official:
        evidence = ", ".join(result.get("evidence", [])) or "-"
        lines.append(f"| {result.get('case_id', '')} | {result.get('status', '')} | {len(result.get('commands', []))} | {evidence} |")

    lines.extend(
        [
            "",
            "## Partial Probes",
            "",
            "Partial probes are supplemental diagnostics and are not counted in official case counters.",
            "",
            "| PASS | FAIL | BLOCK | ABORT | NOT_RUN |",
            "|---:|---:|---:|---:|---:|",
            f"| {partial_counts['PASS']} | {partial_counts['FAIL']} | {partial_counts['BLOCK']} | {partial_counts['ABORT']} | {partial_counts['NOT_RUN']} |",
            "",
            "| Case | Status | Commands | Evidence |",
            "|---|---|---:|---|",
        ]
    )
    if not partial:
        lines.append("| - | - | 0 | No partial probes were reported |")
    for result in partial:
        evidence = ", ".join(result.get("evidence", [])) or "-"
        lines.append(f"| {result.get('case_id', '')} | {result.get('status', '')} | {len(result.get('commands', []))} | {evidence} |")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def load_latest_payload(state_dir: Path) -> dict[str, Any] | None:
    latest = state_dir / "latest-run.json"
    if not latest.exists():
        return None
    payload = json.loads(latest.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def load_latest_results(state_dir: Path) -> list[dict[str, Any]]:
    payload = load_latest_payload(state_dir)
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    return list(results) if isinstance(results, list) else []


def render_issues_report(config: ProjectConfig) -> dict[str, Any]:
    issue_payload = issue_status(config)
    latest_payload = load_latest_payload(config.paths.state)
    latest_results = list(latest_payload.get("results", [])) if isinstance(latest_payload, dict) and isinstance(latest_payload.get("results"), list) else []
    results_by_case = {str(result.get("case_id")): result for result in latest_results if isinstance(result, dict) and result.get("case_id")}
    contracts = _contracts_by_case(config)
    rows: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in issue_payload.get("traceability", []):
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("case_id") or "")
        result = results_by_case.get(case_id) if case_id else None
        status = str(result.get("status") or item.get("latest_status") or "NOT_RUN") if isinstance(result, dict) else str(item.get("latest_status") or "NOT_RUN")
        row = {
            "gitea_issue_id": item.get("gitea_issue_id"),
            "redmine_issue_ids": item.get("redmine_issue_ids", []),
            "case_id": case_id or None,
            "coverage_status": item.get("coverage_status"),
            "latest_status": status,
            "latest_evidence": result.get("evidence", []) if isinstance(result, dict) else item.get("latest_evidence", []),
            "current_blocker": _current_issue_blocker(item, result),
            "recommended_next_module": _recommended_next_module(item, result),
            "title": item.get("title"),
        }
        rows.append(row)
        if status not in {"FAIL", "BLOCK"} or not isinstance(result, dict):
            continue
        action = _evidence_update_action(config, traceability_row=item, issue_row=row, result=result, contracts=contracts)
        if not action:
            continue
        if action.get("write_gate_result", {}).get("allowed"):
            actions.append(action)
        else:
            blocked.append(action)

    report_json = {
        "schema": "quality-pilot.issues-report.v1",
        "generated_at": utc_now(),
        "latest_run": _latest_run_summary(latest_payload),
        "issue_count": len(rows),
        "issues": rows,
        "evidence_update_candidates": len(actions) + len(blocked),
        "evidence_update_actions": actions,
        "evidence_update_blocked": blocked,
    }
    report_json_path = config.paths.state / ISSUES_REPORT_JSON_NAME
    report_md_path = config.paths.reports / ISSUES_REPORT_MD_NAME
    report_json_path.parent.mkdir(parents=True, exist_ok=True)
    report_md_path.parent.mkdir(parents=True, exist_ok=True)
    report_json_path.write_text(json_dumps(report_json) + "\n", encoding="utf-8")
    report_md_path.write_text(_render_issues_report_markdown(report_json), encoding="utf-8")

    write_request = _build_issue_evidence_write_request(config, actions, blocked)
    request_path = issue_evidence_write_request_path(config)
    if actions:
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json_dumps(write_request) + "\n", encoding="utf-8")
        ledger = record_gitea_mcp_write_request(
            config,
            write_request,
            request_path,
            source_module="issues_report",
            target_type="issue_evidence_update",
        )
    else:
        ledger = {"entry_count": 0, "touched_operation_ids": []}

    return {
        "status": write_request["status"],
        "report_path": _relative_or_str(report_md_path, config.root),
        "report_json_path": _relative_or_str(report_json_path, config.root),
        "issue_count": len(rows),
        "evidence_update_count": len(actions),
        "blocked_by_gate": len(blocked),
        "mcp_issue_evidence_write_request": write_request if actions else None,
        "mcp_issue_evidence_write_request_path": _relative_or_str(request_path, config.root),
        "mcp_issue_evidence_write_result_path": _relative_or_str(issue_evidence_write_result_path(config), config.root),
        "mcp_write_ledger_path": _relative_or_str(write_ledger_path(config), config.root),
        "mcp_write_ledger": {
            "entry_count": ledger.get("entry_count", 0),
            "touched_operation_ids": ledger.get("touched_operation_ids", []),
        },
    }


def _count_results(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "ABORT": 0, "NOT_RUN": 0}
    for result in results:
        key = str(result.get("status") or "BLOCK")
        counts[key] = counts.get(key, 0) + 1
    return counts


def issue_evidence_write_request_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / ISSUE_EVIDENCE_WRITE_REQUEST_NAME


def issue_evidence_write_result_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / ISSUE_EVIDENCE_WRITE_RESULT_NAME


def _contracts_by_case(config: ProjectConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in list_contract_paths(config.paths.cases):
        try:
            contract = load_contract(path)
        except Exception:
            continue
        out[contract.case_id] = contract
    return out


def _current_issue_blocker(traceability_row: dict[str, Any], result: dict[str, Any] | None) -> str | None:
    if traceability_row.get("coverage_status") in {"no_case", "needs_input", "stale_case"}:
        return str(traceability_row.get("coverage_reason") or traceability_row.get("coverage_status"))
    if isinstance(result, dict) and result.get("status") == "BLOCK":
        return str(result.get("blocked_reason") or "latest evidence is BLOCK")
    return None


def _recommended_next_module(traceability_row: dict[str, Any], result: dict[str, Any] | None) -> str:
    status = str(result.get("status") or "") if isinstance(result, dict) else ""
    if traceability_row.get("coverage_status") != "covered":
        return str(traceability_row.get("repair_action") or "/quality-pilot cases generate --growing")
    if status in {"FAIL", "BLOCK"}:
        return "/quality-pilot issues fix --issue <gitea_issue_id>"
    if status == "PASS":
        return "/quality-pilot publish wiki apply"
    return "/quality-pilot cases run <case_id>"


def _evidence_update_action(
    config: ProjectConfig,
    *,
    traceability_row: dict[str, Any],
    issue_row: dict[str, Any],
    result: dict[str, Any],
    contracts: dict[str, Any],
) -> dict[str, Any] | None:
    gitea_issue_id = _int_or_none(traceability_row.get("gitea_issue_id"))
    case_id = str(traceability_row.get("case_id") or result.get("case_id") or "")
    if gitea_issue_id is None or not case_id:
        return None
    contract = contracts.get(case_id)
    expected_hash = getattr(contract, "contract_hash", None)
    body = _render_issue_evidence_update_body(issue_row, result)
    gate = evaluate_write_gate(
        config_data=config.data,
        result=result,
        target_state="open",
        expected_contract_hash=expected_hash,
        sync_current=True,
        write_text=body,
    ).as_dict()
    return {
        "id": f"issue-evidence-{gitea_issue_id}-{case_id}",
        "operation": "gitea.issue.update",
        "update_kind": "evidence",
        "gitea_issue_id": gitea_issue_id,
        "redmine_issue_id": _first_redmine_id(traceability_row.get("redmine_issue_ids", [])),
        "redmine_issue_ids": traceability_row.get("redmine_issue_ids", []),
        "case_id": case_id,
        "status": result.get("status"),
        "body": body,
        "evidence": result.get("evidence", []),
        "result_path": result.get("result_path"),
        "contract_hash": result.get("contract_hash"),
        "idempotency_key": _evidence_update_idempotency_key(gitea_issue_id, case_id, result),
        "write_gate_result": gate,
    }


def _render_issue_evidence_update_body(issue_row: dict[str, Any], result: dict[str, Any]) -> str:
    commands = result.get("commands") if isinstance(result.get("commands"), list) else []
    lines = [
        "## QA Evidence Update",
        "",
        f"- Status: {result.get('status')}",
        f"- Case: {result.get('case_id')}",
        f"- Redmine: {_format_redmine_refs(issue_row.get('redmine_issue_ids', []))}",
        f"- Result path: {result.get('result_path') or '-'}",
        f"- Evidence: {', '.join(result.get('evidence', [])) or '-'}",
        "",
        "## Reproduction Command",
        "",
    ]
    if commands:
        for command in commands:
            if not isinstance(command, dict):
                continue
            lines.append(f"- `{command.get('id')}`: `{command.get('run')}`")
    else:
        lines.append("- No command payload was recorded.")
    lines.extend([
        "",
        "## Observed Result",
        "",
        f"- Exit code: {result.get('exit_code')}",
        f"- Latest status: {result.get('status')}",
        f"- Blocker: {result.get('blocked_reason') or issue_row.get('current_blocker') or '-'}",
        "",
        "## Next Step",
        "",
        f"- {issue_row.get('recommended_next_module')}",
        "",
    ])
    return "\n".join(lines)


def _build_issue_evidence_write_request(config: ProjectConfig, actions: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> dict[str, Any]:
    if blocked:
        status = "blocked"
    elif actions:
        status = "needs_mcp_apply"
    else:
        status = "no_remote_write_needed"
    return {
        "schema": "quality-pilot.gitea-mcp-issue-write-request.v1",
        "status": status,
        "operation": "gitea.issue.evidence_update",
        "created_at": utc_now(),
        "repo_source": "hermes_session",
        "actions": actions,
        "blocked": blocked,
        "blocked_by_gate": len(blocked),
        "safety": {
            "allowed_targets": ["issues"],
            "allowed_operations": ["gitea.issue.update"],
            "source": "issues_report",
            "write_gate_required": True,
            "do_not_create_duplicate_issues": True,
            "do_not_close_or_reopen_issues": True,
        },
        "result_path": _relative_or_str(issue_evidence_write_result_path(config), config.root),
    }


def _render_issues_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Issue QA Report",
        "",
        f"- Generated at: {report.get('generated_at')}",
        f"- Latest run: {report.get('latest_run', {}).get('run_id') or '-'}",
        f"- Evidence updates: {report.get('evidence_update_candidates', 0)}",
        "",
        "| Gitea | Redmine | Case | Coverage | Latest | Next |",
        "|---:|---|---|---|---|---|",
    ]
    for issue in report.get("issues", []):
        if not isinstance(issue, dict):
            continue
        redmine = _format_redmine_refs(issue.get("redmine_issue_ids", []))
        lines.append(
            f"| {issue.get('gitea_issue_id') or '-'} | {redmine} | {issue.get('case_id') or '-'} | "
            f"{issue.get('coverage_status') or '-'} | {issue.get('latest_status') or '-'} | {issue.get('recommended_next_module') or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


def _latest_run_summary(latest_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(latest_payload, dict):
        return {"status": "missing"}
    return {
        "status": latest_payload.get("status"),
        "run_id": latest_payload.get("run_id"),
        "latest_run_json": latest_payload.get("latest_run_json"),
        "report_path": latest_payload.get("report_path"),
    }


def _evidence_update_idempotency_key(gitea_issue_id: int, case_id: str, result: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "gitea_issue_id": gitea_issue_id,
            "case_id": case_id,
            "status": result.get("status"),
            "contract_hash": result.get("contract_hash"),
            "result_path": result.get("result_path"),
            "evidence": result.get("evidence", []),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"issue-evidence-{gitea_issue_id}-{digest}"


def _format_redmine_refs(values: Any) -> str:
    refs = [str(item) for item in values] if isinstance(values, list) else []
    return ", ".join(f"#{item}" for item in refs) if refs else "-"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_redmine_id(values: Any) -> int | None:
    if not isinstance(values, list):
        return None
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _source_run_id(latest_run: dict[str, Any] | None) -> str:
    if not isinstance(latest_run, dict):
        return "-"
    return str(latest_run.get("run_id") or "-")


def _source_status(latest_run: dict[str, Any] | None) -> str:
    if not isinstance(latest_run, dict):
        return "missing"
    return str(latest_run.get("status") or "unknown")


def _stale_reason(official_results: list[dict[str, Any]], latest_run: dict[str, Any] | None) -> str | None:
    if not isinstance(latest_run, dict):
        return "no latest-run payload was available for this report"
    latest_results = latest_run.get("results")
    if not isinstance(latest_results, list):
        return "latest-run payload has no results list"
    if str(latest_run.get("status") or "").upper() == "PASS" and not any(
        str(result.get("status") or "").upper() == "PASS" for result in official_results
    ):
        return "latest-run is PASS but no official case result reflects PASS evidence"
    if official_results and all(str(result.get("status") or "").upper() == "NOT_RUN" for result in official_results):
        return "all official case results are NOT_RUN"
    return None
