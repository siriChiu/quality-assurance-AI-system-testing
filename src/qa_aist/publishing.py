from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .contracts import load_contracts
from .gitea import GiteaClient, GiteaError, gitea_config_from_project
from .issues import issue_fingerprint, load_issue_snapshot
from .write_gate import evaluate_write_gate

PUBLISH_PLAN_NAME = "publish-plan.json"
PUBLISH_APPLY_NAME = "publish-apply-result.json"


class PublishError(RuntimeError):
    pass


def plan_publish(config: ProjectConfig, *, latest_run: str | Path | None = None) -> dict[str, Any]:
    run = _load_latest_run(config, latest_run)
    results = [item for item in run.get("results", []) if isinstance(item, dict)]
    snapshot = load_issue_snapshot(config)
    sync_current = bool(snapshot.get("synced_at"))
    contract_sources = _contract_sources(config)
    gitea = gitea_config_from_project(config.data)
    actions: list[dict[str, Any]] = []

    wiki_body = render_wiki_status(run, results)
    wiki_gate = evaluate_write_gate(
        config_data=config.data,
        result=_aggregate_gate_result(results),
        sync_current=sync_current,
        write_text=wiki_body,
    ).as_dict()
    actions.append(
        {
            "id": "wiki-status",
            "type": "wiki_update",
            "page": gitea.wiki_page,
            "message": "QA-AIST test status update",
            "body": wiki_body,
            "write_gate_result": wiki_gate,
        }
    )

    for result in results:
        if result.get("status") != "FAIL":
            continue
        source = contract_sources.get(str(result.get("case_id")), {})
        issue_id = _issue_id_from_source(source)
        body = render_issue_body(result, source)
        duplicate = False
        if issue_id is None:
            duplicate = _duplicates_existing_issue(snapshot, result, body)
        gate = evaluate_write_gate(
            config_data=config.data,
            result=result,
            target_state="open" if issue_id is not None else "unknown",
            duplicate_candidate=duplicate,
            sync_current=sync_current,
            write_text=body,
        ).as_dict()
        actions.append(
            {
                "id": f"issue-{result.get('case_id')}",
                "type": "issue_comment" if issue_id is not None else "issue_create",
                "issue_id": issue_id,
                "title": f"[QA][AUTO][{result.get('case_id')}] {result.get('title') or 'QA-AIST failure'}",
                "body": body,
                "write_gate_result": gate,
            }
        )

    plan = {
        "schema": "qa-aist.publish-plan.v1",
        "status": "blocked" if any(not action["write_gate_result"]["allowed"] for action in actions) else "ready",
        "run_id": run.get("run_id"),
        "latest_run_json": _relative_or_str(_latest_run_path(config, latest_run), config.root),
        "provider": "gitea",
        "actions": actions,
        "blocked_by_gate": len([action for action in actions if not action["write_gate_result"]["allowed"]]),
    }
    path = publish_plan_path(config)
    config.paths.state.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(plan) + "\n", encoding="utf-8")
    return {**plan, "plan_path": _relative_or_str(path, config.root)}


def apply_publish_plan(config: ProjectConfig, *, plan_path: str | Path | None = None) -> dict[str, Any]:
    plan = load_publish_plan(config, plan_path)
    blocked = [action for action in plan.get("actions", []) if not action.get("write_gate_result", {}).get("allowed")]
    if blocked:
        return {
            "status": "blocked",
            "error": "write_gate_blocked",
            "blocked_by_gate": len(blocked),
            "blocked_actions": [{"id": action.get("id"), "reason_codes": action.get("write_gate_result", {}).get("reason_codes", [])} for action in blocked],
        }
    gitea_cfg = gitea_config_from_project(config.data)
    if not gitea_cfg.configured:
        return {"status": "blocked", "error": "gitea_not_configured", "token_env": gitea_cfg.token_env}
    if gitea_cfg.uses_mcp:
        return {
            "status": "blocked",
            "error": "gitea_mcp_write_not_supported",
            "message": "tracker.gitea.backend: mcp supports issue sync and gated Wiki-only handoff. Configure HTTP backend with token_env before legacy /qa-aist publish apply writes issue comments or mixed publish output.",
            "backend": gitea_cfg.backend,
        }
    client = GiteaClient(gitea_cfg)
    applied: list[dict[str, Any]] = []
    for action in plan.get("actions", []):
        kind = action.get("type")
        if kind == "wiki_update":
            response = client.update_wiki_page(page=str(action.get("page") or gitea_cfg.wiki_page), content=str(action.get("body") or ""), message=str(action.get("message") or "QA-AIST update"))
        elif kind == "issue_comment":
            response = client.create_issue_comment(int(action["issue_id"]), str(action.get("body") or ""))
        elif kind == "issue_create":
            response = client.create_issue(title=str(action.get("title") or "QA-AIST failure"), body=str(action.get("body") or ""))
        else:
            response = {"skipped": True, "reason": "unknown_action_type"}
        applied.append({"id": action.get("id"), "type": kind, "response": response})
    payload = {"status": "ok", "applied_count": len(applied), "applied": applied}
    path = publish_apply_path(config)
    path.write_text(json_dumps(payload) + "\n", encoding="utf-8")
    return {**payload, "apply_result_path": _relative_or_str(path, config.root)}


def publish_status(config: ProjectConfig) -> dict[str, Any]:
    plan_path = publish_plan_path(config)
    apply_path = publish_apply_path(config)
    plan = load_publish_plan(config) if plan_path.exists() else None
    return {
        "status": "ok",
        "plan_exists": plan_path.exists(),
        "plan_path": _relative_or_str(plan_path, config.root),
        "apply_result_exists": apply_path.exists(),
        "apply_result_path": _relative_or_str(apply_path, config.root),
        "blocked_by_gate": plan.get("blocked_by_gate") if isinstance(plan, dict) else None,
        "action_count": len(plan.get("actions", [])) if isinstance(plan, dict) else 0,
    }


def render_wiki_status(run: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [
        "# QA-AIST Test Status",
        "",
        f"- Status: {run.get('status', 'unknown')}",
        f"- Run: {run.get('run_id', '-')}",
        f"- Report: {run.get('report_path', '-')}",
        "",
        "| Case | Status | Exit code |",
        "|---|---|---:|",
    ]
    if not results:
        lines.append("| - | NOT_RUN | 0 |")
    for result in results:
        lines.append(f"| {result.get('case_id')} | {result.get('status')} | {result.get('exit_code')} |")
    return "\n".join(lines) + "\n"


def render_issue_body(result: dict[str, Any], source: dict[str, Any]) -> str:
    command_lines = []
    for command in result.get("commands", []):
        if isinstance(command, dict):
            command_lines.append(f"- `{command.get('run')}` -> rc {command.get('exit_code')} (expected {command.get('expected_exit_code')})")
    evidence_count = len(result.get("evidence", [])) if isinstance(result.get("evidence"), list) else 0
    lines = [
        "## QA-AIST failure report",
        "",
        f"- Case: {result.get('case_id')}",
        f"- Status: {result.get('status')}",
        f"- Contract hash: {result.get('contract_hash')}",
        f"- Evidence files: {evidence_count}",
    ]
    if source.get("issue_id"):
        lines.append(f"- Source issue: #{source.get('issue_id')}")
    lines.extend(["", "## Commands", "", *command_lines, "", "## Notes", "", "Generated by QA-AIST publish plan after deterministic test execution."])
    return "\n".join(lines) + "\n"


def publish_plan_path(config: ProjectConfig) -> Path:
    return config.paths.state / PUBLISH_PLAN_NAME


def publish_apply_path(config: ProjectConfig) -> Path:
    return config.paths.state / PUBLISH_APPLY_NAME


def load_publish_plan(config: ProjectConfig, plan_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(plan_path) if plan_path else publish_plan_path(config)
    if not path.exists():
        raise PublishError(f"publish plan not found: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise PublishError("publish plan must be a JSON object")
    return loaded


def _load_latest_run(config: ProjectConfig, latest_run: str | Path | None) -> dict[str, Any]:
    path = _latest_run_path(config, latest_run)
    if not path.exists():
        raise PublishError(f"latest run not found: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise PublishError("latest run must be a JSON object")
    return loaded


def _latest_run_path(config: ProjectConfig, latest_run: str | Path | None) -> Path:
    return Path(latest_run) if latest_run else config.paths.state / "latest-run.json"


def _aggregate_gate_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    evidence: list[Any] = []
    for result in results:
        if isinstance(result.get("evidence"), list):
            evidence.extend(result["evidence"])
    return {
        "status": "PASS" if results else "NOT_RUN",
        "evidence": evidence,
        "contract_hash": "aggregate",
    }


def _contract_sources(config: ProjectConfig) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    try:
        contracts = load_contracts(config.paths.cases)
    except Exception:
        return sources
    for contract in contracts:
        source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
        sources[contract.case_id] = source
    return sources


def _issue_id_from_source(source: dict[str, Any]) -> int | None:
    try:
        return int(source.get("issue_id"))
    except (TypeError, ValueError):
        return None


def _duplicates_existing_issue(snapshot: dict[str, Any], result: dict[str, Any], body: str) -> bool:
    title = str(result.get("title") or "")
    candidate = issue_fingerprint(title, body)
    return any(item.get("fingerprint") == candidate for item in snapshot.get("items", []) if isinstance(item, dict))


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
