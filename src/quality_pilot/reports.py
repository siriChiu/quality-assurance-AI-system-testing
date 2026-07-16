from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .contracts import list_contract_paths, load_contract
from .gitea_ledger import record_gitea_mcp_write_request, write_ledger_path
from .issues import (
    issue_fingerprint,
    issue_status,
    load_issue_snapshot,
    local_case_work_item_path,
    local_failure_metadata_path,
    local_failure_report_path,
)
from .runner import utc_now
from .write_gate import evaluate_write_gate

ISSUES_REPORT_JSON_NAME = "issues-report.json"
ISSUES_REPORT_MD_NAME = "issues-report.md"
ISSUE_EVIDENCE_WRITE_REQUEST_NAME = "issue-evidence-update-request.json"
ISSUE_EVIDENCE_WRITE_RESULT_NAME = "issue-evidence-update-result.json"
ISSUE_FAILURE_WRITE_REQUEST_NAME = "issue-failure-write-request.json"
ISSUE_FAILURE_WRITE_RESULT_NAME = "issue-failure-write-result.json"


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
    linked_case_ids: set[str] = set()
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
        if _int_or_none(item.get("gitea_issue_id")) is not None and case_id:
            linked_case_ids.add(case_id)
        if status not in {"FAIL", "BLOCK"} or not isinstance(result, dict):
            continue
        action = _evidence_update_action(config, traceability_row=item, issue_row=row, result=result, contracts=contracts)
        if not action:
            continue
        if action.get("write_gate_result", {}).get("allowed"):
            actions.append(action)
        else:
            blocked.append(action)

    standalone_failures = _standalone_failure_candidates(latest_results, linked_case_ids)
    eligible_standalone = [item for item in standalone_failures if item.get("eligible_for_issue_create")]

    report_json = {
        "schema": "quality-pilot.issues-report.v1",
        "generated_at": utc_now(),
        "latest_run": _latest_run_summary(latest_payload),
        "issue_count": len(rows),
        "issues": rows,
        "standalone_failure_count": len(standalone_failures),
        "standalone_failure_candidates": standalone_failures,
        "standalone_issue_create_candidate_count": len(eligible_standalone),
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
        "standalone_failure_count": len(standalone_failures),
        "standalone_issue_create_candidate_count": len(eligible_standalone),
        "standalone_failure_candidates": standalone_failures,
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


def create_issues_from_failures(
    config: ProjectConfig,
    *,
    case_id: str | None = None,
    all_failures: bool = False,
    include_partial: bool = False,
    dry_run: bool = False,
    mode: str = "remote",
) -> dict[str, Any]:
    """Create a local report or a gated remote issue handoff for latest failures.

    ``mode=local`` is deliberately side-effect limited to local report files.  The
    remote path is opt-in at the CLI and emits only a gated, redacted SWQA report.
    """
    if mode not in {"local", "remote"}:
        return {"status": "error", "error": "invalid_failure_mode", "message": "Use mode=local or mode=remote."}
    latest_payload = load_latest_payload(config.paths.state)
    if not isinstance(latest_payload, dict):
        return {
            "status": "error",
            "error": "latest_run_missing",
            "message": "Run `/quality-pilot cases run` before creating issues from failures.",
        }
    if not all_failures and case_id is None:
        return {
            "status": "error",
            "error": "failure_scope_required",
            "message": "Use `--case <case_id>` for one case or `--all` for all official FAIL/BLOCK cases.",
        }

    latest_results = [item for item in latest_payload.get("results", []) if isinstance(item, dict)]
    selected = [
        result for result in latest_results
        if str(result.get("status") or "").upper() in {"FAIL", "BLOCK"}
        and (case_id is None or str(result.get("case_id") or "") == case_id)
    ]
    if case_id is not None and not selected:
        observed = next((item for item in latest_results if str(item.get("case_id") or "") == case_id), None)
        return {
            "status": "error",
            "error": "case_not_failed",
            "case_id": case_id,
            "observed_status": observed.get("status") if isinstance(observed, dict) else None,
            "message": f"Latest run has no FAIL/BLOCK result for case `{case_id}`.",
        }
    contracts = _contracts_by_case(config)
    report_results = [
        result for result in selected
        if include_partial or not result.get("partial_probe")
    ]
    local_report_path = issue_failure_local_report_path(config)
    local_report_json_path = issue_failure_local_json_path(config)
    local_case_paths: list[str] = []
    local_report_written = False
    if mode == "local" or (mode == "remote" and not dry_run):
        local_report_path.parent.mkdir(parents=True, exist_ok=True)
        local_report_json_path.parent.mkdir(parents=True, exist_ok=True)
        local_report_path.write_text(
            _render_failure_local_report(report_results, contracts, latest_payload, root=config.root),
            encoding="utf-8",
        )
        for result in report_results:
            current_case_id = str(result.get("case_id") or "").strip()
            if not current_case_id:
                continue
            case_path = local_case_work_item_path(config, current_case_id)
            case_path.parent.mkdir(parents=True, exist_ok=True)
            case_path.write_text(
                _render_failure_case_report(
                    result,
                    contracts.get(current_case_id),
                    public=False,
                    root=config.root,
                ),
                encoding="utf-8",
            )
            local_case_paths.append(_relative_or_str(case_path, config.root))
        local_report_json_path.write_text(
            json_dumps({
                "schema": "quality-pilot.failure-local-report.v1",
                "generated_at": utc_now(),
                "latest_run": _latest_run_summary(latest_payload),
                "case_ids": [str(item.get("case_id")) for item in report_results],
                "status_counts": _count_results(report_results),
                "report_path": _relative_or_str(local_report_path, config.root),
                "case_work_items": local_case_paths,
                "publication_mode": mode,
                "remote_write": "not_requested" if mode == "local" else "gated_mcp_handoff",
            }) + "\n",
            encoding="utf-8",
        )
        local_report_written = True
    if mode == "local":
        return {
            "status": "local_report_ready" if local_report_written else "dry_run",
            "mode": "local",
            "case_id": case_id,
            "latest_run": _latest_run_summary(latest_payload),
            "selected_failure_count": len(report_results),
            "issue_create_count": 0,
            "excluded_partial": [
                {"case_id": str(item.get("case_id") or ""), "status": item.get("status"), "reason": "partial_probe_excluded_by_default"}
                for item in selected if item.get("partial_probe") and not include_partial
            ],
            "local_report_path": _relative_or_str(local_report_path, config.root),
            "local_report_json_path": _relative_or_str(local_report_json_path, config.root),
            "local_report_written": local_report_written,
            "local_case_work_items": local_case_paths,
            "remote_write": "not_requested",
            "mcp_issue_write_request": None,
            "message": "Local SWQA failure report written; no Gitea issue request or remote write was created.",
        }
    issue_payload = issue_status(config, persist_traceability=not dry_run)
    traceability_by_case = {
        str(item.get("case_id")): item
        for item in issue_payload.get("traceability", [])
        if isinstance(item, dict) and item.get("case_id")
    }
    snapshot = load_issue_snapshot(config)
    pending_case_ids = _pending_failure_issue_case_ids(config)
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    excluded_partial: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for result in selected:
        current_case_id = str(result.get("case_id") or "")
        if not current_case_id:
            skipped.append({"reason": "case_id_missing", "status": result.get("status")})
            continue
        if result.get("partial_probe") and not include_partial:
            excluded_partial.append({
                "case_id": current_case_id,
                "status": result.get("status"),
                "reason": "partial_probe_excluded_by_default",
            })
            continue

        traceability = traceability_by_case.get(current_case_id, {})
        linked_issue_id = _int_or_none(traceability.get("gitea_issue_id"))
        if linked_issue_id is not None:
            skipped.append({
                "case_id": current_case_id,
                "reason": "linked_existing_issue",
                "gitea_issue_id": linked_issue_id,
            })
            continue
        if current_case_id in pending_case_ids:
            skipped.append({"case_id": current_case_id, "reason": "issue_create_already_requested"})
            continue

        contract = contracts.get(current_case_id)
        source = contract.raw.get("source") if contract is not None and isinstance(contract.raw.get("source"), dict) else {}
        body = _render_failure_remote_report(result, contract, source, root=config.root)
        title = f"[SWQA][{str(result.get('status') or 'FAIL').upper()}] {current_case_id}: {_public_text(result.get('title') or 'Test failure')}"
        duplicate = _issue_fingerprint_exists(snapshot, title, body) or _issue_case_exists(snapshot, current_case_id)
        expected_hash = getattr(contract, "contract_hash", None)
        gate = evaluate_write_gate(
            config_data=config.data,
            result=result,
            target_state="open",
            expected_contract_hash=expected_hash,
            duplicate_candidate=duplicate,
            sync_current=True,
            write_text=body,
        ).as_dict()
        action = {
            "id": f"failure-issue-{current_case_id}",
            "operation": "gitea.issue.create",
            "action_safety_class": "new_issue_from_failed_case",
            "case_id": current_case_id,
            "status": result.get("status"),
            "title": title,
            "body": body,
            "report_schema": "quality-pilot.swqa-failure-report.v1",
            "report_redaction": "credentials, bearer values, access tokens, and workstation paths removed",
            "labels": ["swqa-failure", "needs-triage"],
            "requested_labels": ["swqa-failure", "needs-triage"],
            "applied_labels": [],
            "unmatched_labels": ["swqa-failure", "needs-triage"],
            "result_path": result.get("result_path"),
            "evidence": result.get("evidence", []),
            "contract_hash": result.get("contract_hash"),
            "dedupe_fingerprint": issue_fingerprint(title, body),
            "idempotency_key": _failure_issue_idempotency_key(result, title, body),
            "write_gate_result": gate,
        }
        if gate.get("allowed"):
            actions.append(action)
        else:
            blocked.append(action)

    request = _build_failure_issue_write_request(config, actions, blocked)
    request_path = issue_failure_write_request_path(config)
    if not dry_run and (actions or blocked):
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json_dumps(request) + "\n", encoding="utf-8")
        ledger = record_gitea_mcp_write_request(
            config,
            request,
            request_path,
            source_module="issues_create_from_failure",
            target_type="issue_create",
        ) if actions else {"entry_count": 0, "touched_operation_ids": []}
    else:
        ledger = {"entry_count": 0, "touched_operation_ids": []}

    status = "dry_run" if dry_run else request["status"]
    return {
        "status": status,
        "mode": "remote",
        "scope": "case" if case_id else "all",
        "case_id": case_id,
        "latest_run": _latest_run_summary(latest_payload),
        "selected_failure_count": len(selected),
        "issue_create_count": len(actions),
        "blocked_by_gate": len(blocked),
        "skipped": skipped,
        "excluded_partial": excluded_partial,
        "mcp_issue_write_request": request if actions else None,
        "mcp_issue_write_request_path": _relative_or_str(request_path, config.root),
        "mcp_issue_write_result_path": _relative_or_str(issue_failure_write_result_path(config), config.root),
        "mcp_write_ledger_path": _relative_or_str(write_ledger_path(config), config.root),
        "mcp_write_ledger": {
            "entry_count": ledger.get("entry_count", 0),
            "touched_operation_ids": ledger.get("touched_operation_ids", []),
        },
        "remote_write": "gated_mcp_handoff",
        "local_report_path": _relative_or_str(local_report_path, config.root),
        "local_report_json_path": _relative_or_str(local_report_json_path, config.root),
        "local_report_written": local_report_written,
        "local_case_work_items": local_case_paths,
        "message": _failure_issue_message(status, actions, blocked, excluded_partial),
    }


_SECRET_VALUE_RE = re.compile(
    r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key|authorization|bearer)\b\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_TOKEN_RE = re.compile(r"\b(?:sk|ghp|glpat|xox[baprs]-)[A-Za-z0-9._-]{8,}\b")
_URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/@\s:]+):([^/@\s]+)@")
_LOCAL_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:root|home|tmp|var|workspace|Users)/[^\s`]+")


def _public_text(value: Any, *, limit: int = 4000) -> str:
    """Return human-facing text with credentials and workstation paths removed."""
    text = "" if value is None else str(value)
    text = _URL_CREDENTIAL_RE.sub(r"\1[CREDENTIALS_REDACTED]@", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SECRET_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = _TOKEN_RE.sub("[REDACTED]", text)
    text = re.sub(r"\.quality-pilot-project(?:/[^\s`]*)?", "[LOCAL_ARTIFACT_REDACTED]", text)
    text = _LOCAL_PATH_RE.sub("[LOCAL_PATH_REDACTED]", text)
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + " … [truncated]"
    return text


def _technical_text(value: Any, *, limit: int = 4000) -> str:
    """Keep local diagnostic detail while still never copying obvious secrets."""
    return _public_text(value, limit=limit).replace("[LOCAL_ARTIFACT_REDACTED]", ".quality-pilot-project/[redacted]")


def _read_evidence_excerpt(value: Any, root: Path | None) -> str:
    if not root or not value:
        return ""
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _render_failure_case_report(result: dict[str, Any], contract: Any, *, public: bool, root: Path | None = None) -> str:
    case_id = str(result.get("case_id") or "-")
    status = str(result.get("status") or "UNKNOWN").upper()
    title = _public_text(result.get("title") or getattr(contract, "title", None) or "SWQA test failure")
    expected = (contract.raw.get("expected") if contract is not None and isinstance(contract.raw, dict) else None) or "The documented acceptance criteria should be satisfied."
    cleaner = _public_text if public else _technical_text
    lines = [
        f"# SWQA Test Failure Report — {case_id}",
        "",
        "## Executive Summary",
        "",
        f"- Test case: {case_id}",
        f"- Scenario: {title}",
        f"- Outcome: **{status}**",
        f"- Assessment: {'Execution was blocked; a product defect cannot be concluded yet.' if status == 'BLOCK' else 'The observed behavior did not satisfy the documented acceptance criteria.'}",
        "",
        "## Test Scope and Method",
        "",
        "This report records a deterministic software-quality verification run. It is intended to be readable without access to the test automation tool.",
        f"- Acceptance objective: {cleaner(expected)}",
        f"- Partial/environment probe: {'yes' if result.get('partial_probe') else 'no'}",
        "",
        "## Reproduction Procedure",
        "",
    ]
    commands = result.get("commands") if isinstance(result.get("commands"), list) else []
    if not commands:
        lines.append("1. No executable command was recorded; review the environment or contract before rerunning.")
    for index, command in enumerate(commands, start=1):
        if not isinstance(command, dict):
            continue
        run = cleaner(command.get("run") or "(command unavailable)")
        expected_exit = command.get("expected_exit_code", "-")
        observed_exit = command.get("exit_code", "-")
        lines.extend([
            f"{index}. Run: `{run}`",
            f"   - Expected exit code: `{expected_exit}`",
            f"   - Observed exit code: `{observed_exit}`",
        ])
    lines.extend(["", "## Expected Result", "", cleaner(expected), "", "## Actual Result", ""])
    if commands:
        for command in commands:
            if not isinstance(command, dict):
                continue
            lines.append(f"- `{command.get('id') or 'command'}` status: **{command.get('status') or status}**")
            if command.get("blocked_reason"):
                lines.append(f"  - Blocker: {cleaner(command.get('blocked_reason'))}")
            for stream in ("stdout", "stderr"):
                value = command.get(stream)
                excerpt = _read_evidence_excerpt(value, root)
                if excerpt:
                    lines.append(f"  - {stream} excerpt: `{cleaner(excerpt)}`")
                elif value:
                    lines.append(f"  - {stream} evidence reference: `{cleaner(value)}`")
    else:
        lines.append("- No command result was recorded.")
    lines.extend(["", "## Oracle / Verification Evidence", ""])
    oracle_rows = []
    for command in commands:
        if isinstance(command, dict) and isinstance(command.get("oracle_results"), list):
            oracle_rows.extend(command["oracle_results"])
    if oracle_rows:
        for oracle in oracle_rows:
            if not isinstance(oracle, dict):
                continue
            verdict = "PASS" if oracle.get("passed") else "FAIL"
            lines.append(
                f"- {oracle.get('id') or oracle.get('type') or 'assertion'}: **{verdict}** "
                f"({cleaner(oracle.get('operator') or '')} {cleaner(oracle.get('expected') or '')}; observed {cleaner(oracle.get('actual') or '')})"
            )
    else:
        lines.append("- No assertion-level evidence was recorded.")
    lines.extend([
        "",
        "## Risk and Follow-up",
        "",
        f"- Triage classification: {'environment or prerequisite gap' if status == 'BLOCK' else 'test failure requiring investigation'}.",
        "- Re-run the procedure in the intended product environment, then attach the resulting evidence and confirm the acceptance criteria.",
        "",
        "## Data Handling",
        "",
        "Credentials, bearer values, access tokens, and workstation-specific paths are redacted from this report.",
        "Detailed evidence artifacts remain in the test owner's controlled workspace and can be supplied separately under the project's access policy.",
    ])
    if not public:
        evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
        lines.extend(["", "## Local Evidence References", ""])
        lines.extend(f"- {_technical_text(item)}" for item in evidence if item)
        if result.get("result_path"):
            lines.append(f"- Result record: {_technical_text(result.get('result_path'))}")
    return "\n".join(lines) + "\n"


def _render_failure_remote_report(result: dict[str, Any], contract: Any, source: dict[str, Any] | None = None, *, root: Path | None = None) -> str:
    del source  # Source tracker payloads may contain private/internal details; do not publish them.
    return _render_failure_case_report(result, contract, public=True, root=root)


def _render_failure_local_report(results: list[dict[str, Any]], contracts: dict[str, Any], latest_payload: dict[str, Any], *, root: Path | None = None) -> str:
    lines = [
        "# Local SWQA Failure Report",
        "",
        f"- Generated at: {utc_now()}",
        f"- Latest run outcome: {latest_payload.get('status') or '-'}",
        f"- Failure/blocker count: {len(results)}",
        "",
        "This local report contains technical reproduction and evidence references. It does not create, queue, or upload a Gitea issue.",
        "",
    ]
    for result in results:
        contract = contracts.get(str(result.get("case_id") or ""))
        lines.append(_render_failure_case_report(result, contract, public=False, root=root).rstrip())
        lines.extend(["", "---", ""])
    if not results:
        lines.append("No selected FAIL/BLOCK cases were available in the latest run.\n")
    return "\n".join(lines).rstrip() + "\n"


def _standalone_failure_candidates(results: list[dict[str, Any]], linked_case_ids: set[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for result in results:
        status = str(result.get("status") or "").upper()
        case_id = str(result.get("case_id") or "")
        if status not in {"FAIL", "BLOCK"} or not case_id or case_id in linked_case_ids:
            continue
        partial = bool(result.get("partial_probe"))
        candidates.append({
            "case_id": case_id,
            "status": status,
            "partial_probe": partial,
            "eligible_for_issue_create": not partial,
            "title": result.get("title") or "AI Quality Pilot failure",
            "evidence": result.get("evidence", []),
            "result_path": result.get("result_path"),
            "recommended_command": "/quality-pilot issues create-from-failure --local --case " + case_id,
        })
    return candidates


def _pending_failure_issue_case_ids(config: ProjectConfig) -> set[str]:
    path = write_ledger_path(config)
    if not path.exists():
        return set()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        str(entry.get("case_id"))
        for entry in loaded.get("entries", []) if isinstance(entry, dict)
        and str(entry.get("target_type") or "") == "issue_create"
        and str(entry.get("case_id") or "")
    }


def _issue_fingerprint_exists(snapshot: dict[str, Any], title: str, body: str) -> bool:
    fingerprint = issue_fingerprint(title, body)
    return any(
        isinstance(item, dict) and item.get("fingerprint") == fingerprint
        for item in snapshot.get("items", [])
    )


def _issue_case_exists(snapshot: dict[str, Any], case_id: str) -> bool:
    return any(
        isinstance(item, dict) and str(item.get("case_id") or "") == case_id
        for item in snapshot.get("items", [])
    )


def _failure_issue_idempotency_key(result: dict[str, Any], title: str, body: str) -> str:
    material = json.dumps({
        "case_id": result.get("case_id"),
        "status": result.get("status"),
        "contract_hash": result.get("contract_hash"),
        "result_path": result.get("result_path"),
        "title": title,
        "body": body,
    }, sort_keys=True, ensure_ascii=False)
    return "failure-issue-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _build_failure_issue_write_request(config: ProjectConfig, actions: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> dict[str, Any]:
    status = "blocked" if blocked else ("needs_mcp_apply" if actions else "no_remote_write_needed")
    return {
        "schema": "quality-pilot.gitea-mcp-issue-write-request.v1",
        "status": status,
        "operation": "gitea.issue.create_from_failure",
        "created_at": utc_now(),
        "repo_source": "hermes_session",
        "actions": actions,
        "blocked": blocked,
        "blocked_by_gate": len(blocked),
        "safety": {
            "allowed_targets": ["issues"],
            "allowed_operations": ["gitea.issue.create"],
            "source": "latest_run_failure",
            "write_gate_required": True,
            "do_not_duplicate_existing_issues": True,
            "do_not_comment_or_close_existing_issues": True,
        },
        "result_path": _relative_or_str(issue_failure_write_result_path(config), config.root),
    }


def _failure_issue_message(status: str, actions: list[dict[str, Any]], blocked: list[dict[str, Any]], excluded_partial: list[dict[str, Any]]) -> str:
    if status == "dry_run":
        return f"Dry-run selected {len(actions) + len(blocked)} failure issue candidate(s); no remote write request was applied."
    if status == "needs_mcp_apply":
        return f"Prepared {len(actions)} gated Gitea issue-create action(s); Hermes Gitea MCP apply is required."
    if status == "blocked":
        return f"Issue creation was blocked by the write gate for {len(blocked)} candidate(s)."
    if excluded_partial:
        return f"No issue was created; {len(excluded_partial)} partial probe failure(s) were excluded by default."
    return "No eligible standalone FAIL/BLOCK case required a new issue."


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


def issue_failure_write_request_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / ISSUE_FAILURE_WRITE_REQUEST_NAME


def issue_failure_write_result_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / ISSUE_FAILURE_WRITE_RESULT_NAME


def issue_failure_local_report_path(config: ProjectConfig) -> Path:
    return local_failure_report_path(config)


def issue_failure_local_json_path(config: ProjectConfig) -> Path:
    return local_failure_metadata_path(config)


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
        f"- Standalone FAIL/BLOCK: {report.get('standalone_failure_count', 0)}",
        f"- Eligible new issue candidates: {report.get('standalone_issue_create_candidate_count', 0)}",
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
    lines.extend(["", "## Standalone Failures", "", "| Case | Status | Partial probe | Next action |", "|---|---|---|---|"])
    standalone = report.get("standalone_failure_candidates", [])
    if not standalone:
        lines.append("| - | - | - | No unlinked FAIL/BLOCK cases |")
    for failure in standalone:
        if not isinstance(failure, dict):
            continue
        lines.append(
            f"| {failure.get('case_id') or '-'} | {failure.get('status') or '-'} | "
            f"{'yes' if failure.get('partial_probe') else 'no'} | {failure.get('recommended_command') or '-'} |"
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
