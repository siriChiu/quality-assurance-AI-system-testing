from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contracts import list_contract_paths, load_contract
from .issues import load_issue_snapshot
from .runner import utc_now

UX_METRICS_NAME = "ux-metrics.jsonl"
ISSUE_WRITE_RESULT = ".quality-pilot-project/state/gitea-mcp/issue-write-result.json"

PROBLEM_CLASSES = {
    "typo_argument",
    "removed_command",
    "id_domain_mismatch",
    "case_not_found",
    "handoff_inconsistent",
    "mcp_not_ready",
    "write_gate_blocked",
}


def with_recovery(
    payload: dict[str, Any],
    *,
    argv: list[str] | None = None,
    exit_code: int = 0,
    root: Path | None = None,
) -> dict[str, Any]:
    if isinstance(payload.get("ux_recovery"), dict):
        return payload
    recovery = infer_recovery(payload, argv=argv or [], exit_code=exit_code)
    if recovery is None:
        return payload
    out = {**payload, "ux_recovery": recovery}
    record_ux_metric(root, argv=argv or [], payload=out)
    return out


def infer_recovery(payload: dict[str, Any], *, argv: list[str], exit_code: int) -> dict[str, Any] | None:
    status = str(payload.get("status") or "").lower()
    error = str(payload.get("error") or "")
    message = str(payload.get("message") or "")
    if exit_code == 0 and status in {"ok", "pass", "ready", "needs_mcp_apply", "dry_run", "no_remote_write_needed"}:
        return None

    command = _canonical_command_from_argv(argv)
    problem_class = ""
    root_cause = message or error or status or "recoverable issue"
    recommended = ""
    confirm = False
    confidence = "medium"

    if error == "command_removed":
        problem_class = "removed_command"
        recommended = str(payload.get("replacement") or "/quality-pilot help")
        root_cause = str(payload.get("message") or "This command was removed from the public surface.")
        confirm = bool(recommended and recommended != "/quality-pilot help")
        confidence = "high"
    elif error == "case_not_found" or "case not found" in message.lower():
        problem_class = "case_not_found"
        exact = _first_recovered_case(payload)
        recommended = f"/quality-pilot cases run {exact}" if exact else "/quality-pilot cases list"
        root_cause = message or "The requested case id is not runnable in the current case directory."
        confirm = bool(exact)
        confidence = "high" if exact else "medium"
    elif error == "argument_error" and _has_redmine_typo(argv):
        problem_class = "typo_argument"
        recommended = _fix_redmine_typo_command(argv)
        root_cause = "The command used `--redmine-issuses`; the canonical option is `--redmine-issues`."
        confirm = True
        confidence = "high"
    elif payload.get("id_resolution") and not _id_resolution_has_target(payload.get("id_resolution")):
        problem_class = "id_domain_mismatch"
        recommended = "/quality-pilot issues status"
        root_cause = "The supplied id could not be mapped to a unique Gitea issue or case id."
        confidence = "medium"
    elif str(payload.get("status")) == "handoff_blocked" or error == "handoff_case_id_not_runnable":
        problem_class = "handoff_inconsistent"
        exact = _first_recovered_case(payload)
        recommended = f"/quality-pilot cases run {exact}" if exact else "/quality-pilot cases list"
        root_cause = "The fix handoff referenced a case id that is not currently runnable."
        confirm = bool(exact)
        confidence = "high" if exact else "medium"
    elif "redmine_mcp_snapshot_missing" in message or "gitea_mcp_snapshot_missing" in message:
        problem_class = "mcp_not_ready"
        recommended = command
        root_cause = message or "The required Hermes MCP snapshot is missing."
        confirm = True
        confidence = "high"
    elif _mcp_not_ready(payload):
        problem_class = "mcp_not_ready"
        recommended = "/quality-pilot doctor"
        root_cause = "Hermes MCP readiness is missing or blocked, so remote write workflows are not ready."
        confidence = "high"
    elif _write_gate_blocked(payload):
        problem_class = "write_gate_blocked"
        recommended = "/quality-pilot publish wiki status" if "wiki" in command else "/quality-pilot tracker plan-write"
        root_cause = "The deterministic write gate blocked the requested remote write."
        confidence = "high"
    elif error or status in {"error", "fail", "warn", "blocked"}:
        problem_class = "mcp_not_ready" if "mcp" in message.lower() else "write_gate_blocked" if "gate" in message.lower() else ""
        if not problem_class:
            return None
        recommended = "/quality-pilot doctor"

    if problem_class not in PROBLEM_CLASSES:
        return None
    return {
        "problem_class": problem_class,
        "root_cause": root_cause,
        "recommended_command": recommended,
        "recommended_command_requires_confirmation": bool(confirm),
        "confidence": confidence,
    }


def build_readiness(
    *,
    issue_sync: dict[str, Any] | None = None,
    wiki_sync: dict[str, Any] | None = None,
    redmine_sync: dict[str, Any] | None = None,
    hermes_mcp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue_sync = issue_sync or {}
    wiki_sync = wiki_sync or {}
    redmine_sync = redmine_sync or {}
    hermes_mcp = hermes_mcp or {}
    blockers = _collect_blockers(issue_sync, wiki_sync, redmine_sync, hermes_mcp)
    issue_ready = bool(issue_sync.get("issue_sync_ready") or issue_sync.get("snapshot_exists"))
    remote_ready = bool(
        issue_sync.get("remote_write_ready")
        or wiki_sync.get("remote_write_ready")
        or redmine_sync.get("remote_write_ready")
    )
    if remote_ready and not blockers:
        mode = "WRITE_READY"
    elif any("snapshot_missing" in item or item.endswith("_missing") or item.endswith("_unknown") for item in blockers):
        mode = "SYNC_BLOCKED" if not issue_ready else "WRITE_BLOCKED_MCP"
    elif blockers:
        mode = "WRITE_BLOCKED_MCP"
    else:
        mode = "READ_ONLY_READY"
    return {
        "mode": mode,
        "issue_sync_ready": issue_ready,
        "remote_write_ready": remote_ready and not blockers,
        "blockers": sorted(set(blockers)),
    }


def attach_readiness(payload: dict[str, Any], readiness: dict[str, Any] | None) -> dict[str, Any]:
    if not readiness:
        return payload
    return {**payload, "readiness": readiness}


def resolve_identifier(config: Any, value: Any) -> dict[str, Any]:
    raw = str(value or "").strip()
    result: dict[str, Any] = {
        "input_id": raw,
        "input_domain": _input_domain(raw),
        "resolved_gitea_issue_id": None,
        "resolved_case_id": None,
        "resolution_source": None,
        "candidates": [],
    }
    if not raw:
        return result

    exact_case = _find_case(config, raw)
    if exact_case:
        result.update({"input_domain": "case", "resolved_case_id": exact_case, "resolution_source": "cases-list"})
        return result

    redmine_id = _redmine_id(raw)
    if redmine_id is not None:
        result["input_domain"] = "redmine"
        mapped_issue = _gitea_issue_for_redmine(config, redmine_id)
        if mapped_issue is not None:
            result["resolved_gitea_issue_id"] = mapped_issue
            result["resolution_source"] = "issue-write-result"
        case_id = _case_for_redmine(config, redmine_id)
        if case_id:
            result["resolved_case_id"] = case_id
            result["resolution_source"] = result["resolution_source"] or "cases-list"
        return result

    issue_id = _gitea_issue_id(raw)
    if issue_id is not None:
        result["input_domain"] = "gitea"
        result["resolved_gitea_issue_id"] = issue_id
        case_id = _case_for_gitea_issue(config, issue_id)
        if case_id:
            result["resolved_case_id"] = case_id
            result["resolution_source"] = "cases-list"
        else:
            snapshot_case = _snapshot_case_for_issue(config, issue_id)
            if snapshot_case:
                result["resolved_case_id"] = snapshot_case
                result["resolution_source"] = "issues-snapshot"
        return result

    result["input_domain"] = "unknown"
    result["candidates"] = _case_candidates(config, raw)
    return result


def canonical_issue_id(id_resolution: dict[str, Any]) -> int | None:
    try:
        value = id_resolution.get("resolved_gitea_issue_id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def canonical_case_id(id_resolution: dict[str, Any]) -> str | None:
    value = id_resolution.get("resolved_case_id")
    return str(value) if value else None


def record_ux_metric(root: Path | None, *, argv: list[str], payload: dict[str, Any]) -> None:
    if root is None:
        return
    recovery = payload.get("ux_recovery") if isinstance(payload.get("ux_recovery"), dict) else {}
    id_resolution = payload.get("id_resolution") if isinstance(payload.get("id_resolution"), dict) else {}
    if not recovery and not id_resolution:
        return
    try:
        path = root / ".quality-pilot-project" / "state" / UX_METRICS_NAME
        if not path.parent.exists():
            return
        row = {
            "timestamp": utc_now(),
            "command": _canonical_command_from_argv(argv),
            "problem_class": recovery.get("problem_class"),
            "auto_correction_applied": False,
            "id_resolution_applied": _id_resolution_has_target(id_resolution),
            "retries_before_success": 0,
            "time_to_recovery_sec": 0,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def _input_domain(value: str) -> str:
    if _redmine_id(value) is not None:
        return "redmine"
    if _gitea_issue_id(value) is not None:
        return "gitea"
    if value:
        return "case"
    return "unknown"


def _redmine_id(value: str) -> int | None:
    match = re.search(r"(?i)\bredmine[-_\s#]*(\d+)\b", value)
    if match:
        return int(match.group(1))
    return None


def _gitea_issue_id(value: str) -> int | None:
    match = re.fullmatch(r"(?i)(?:issue[-_#]*)?(\d+)", value.strip())
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"(?i)issue[-_#]*(\d+)", value.strip())
    return int(match.group(1)) if match else None


def _find_case(config: Any, case_id: str) -> str | None:
    wanted = case_id.strip()
    if not wanted:
        return None
    for path in list_contract_paths(config.paths.cases):
        try:
            contract = load_contract(path)
        except Exception:
            continue
        if contract.case_id == wanted:
            return contract.case_id
    return None


def _case_for_redmine(config: Any, redmine_id: int) -> str | None:
    exact = _find_case(config, f"REDMINE-{redmine_id}")
    if exact:
        return exact
    for path in list_contract_paths(config.paths.cases):
        try:
            contract = load_contract(path)
        except Exception:
            continue
        source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
        if _int_or_none(source.get("redmine_issue_id")) == redmine_id:
            return contract.case_id
    return None


def _case_for_gitea_issue(config: Any, issue_id: int) -> str | None:
    exact = _find_case(config, f"ISSUE-{issue_id}")
    if exact:
        return exact
    redmine_refs = _redmine_refs_for_gitea_issue(config, issue_id)
    for redmine_id in redmine_refs:
        case_id = _case_for_redmine(config, redmine_id)
        if case_id:
            return case_id
    for path in list_contract_paths(config.paths.cases):
        try:
            contract = load_contract(path)
        except Exception:
            continue
        source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
        if _int_or_none(source.get("issue_id")) == issue_id or _int_or_none(source.get("gitea_issue_id")) == issue_id:
            return contract.case_id
    return None


def _snapshot_case_for_issue(config: Any, issue_id: int) -> str | None:
    for item in load_issue_snapshot(config).get("items", []):
        if isinstance(item, dict) and _int_or_none(item.get("issue_id")) == issue_id and item.get("case_id"):
            case_id = str(item["case_id"])
            return case_id if _find_case(config, case_id) else None
    return None


def _redmine_refs_for_gitea_issue(config: Any, issue_id: int) -> list[int]:
    refs: list[int] = []
    for item in load_issue_snapshot(config).get("items", []):
        if not isinstance(item, dict) or _int_or_none(item.get("issue_id")) != issue_id:
            continue
        text = "\n".join(str(item.get(key) or "") for key in ("title", "body", "url"))
        refs.extend(int(match) for match in re.findall(r"(?i)\bredmine\s*#?\s*(\d+)\b", text))
    return sorted(set(refs))


def _gitea_issue_for_redmine(config: Any, redmine_id: int) -> int | None:
    for item in _walk_dicts(_load_issue_write_result(config)):
        if _int_or_none(item.get("redmine_issue_id")) != redmine_id:
            continue
        for key in ("issue_id", "gitea_issue_id", "number", "index", "id"):
            issue_id = _int_or_none(item.get(key))
            if issue_id is not None:
                return issue_id
    marker = f"redmine #{redmine_id}".lower()
    for item in load_issue_snapshot(config).get("items", []):
        if not isinstance(item, dict):
            continue
        text = "\n".join(str(item.get(key) or "") for key in ("title", "body", "url")).lower()
        if marker in text:
            return _int_or_none(item.get("issue_id"))
    return None


def _load_issue_write_result(config: Any) -> Any:
    path = config.root / ISSUE_WRITE_RESULT
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for item in value.values():
            out.extend(_walk_dicts(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_walk_dicts(item))
    return out


def _case_candidates(config: Any, query: str) -> list[str]:
    lowered = query.lower()
    out: list[str] = []
    for path in list_contract_paths(config.paths.cases):
        try:
            contract = load_contract(path)
        except Exception:
            continue
        if lowered in contract.case_id.lower() or lowered in contract.title.lower():
            out.append(contract.case_id)
    return out[:10]


def _collect_blockers(*payloads: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for payload in payloads:
        value = payload.get("blockers")
        if isinstance(value, list):
            blockers.extend(str(item) for item in value if item)
        hermes = payload.get("hermes_mcp") if isinstance(payload.get("hermes_mcp"), dict) else {}
        nested = hermes.get("blockers")
        if isinstance(nested, list):
            blockers.extend(str(item) for item in nested if item)
    return blockers


def _first_recovered_case(payload: dict[str, Any]) -> str | None:
    recovered = payload.get("recovered_case_ids")
    if isinstance(recovered, list) and recovered:
        return str(recovered[0])
    id_resolution = payload.get("id_resolution") if isinstance(payload.get("id_resolution"), dict) else {}
    return canonical_case_id(id_resolution)


def _mcp_not_ready(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False).lower()
    return "mcp" in text and any(marker in text for marker in ["missing", "unknown", "not_ready", "not ready", "blocked"])


def _write_gate_blocked(payload: dict[str, Any]) -> bool:
    if payload.get("blocked_by_gate"):
        return True
    gate = payload.get("write_gate_result") if isinstance(payload.get("write_gate_result"), dict) else {}
    if gate and not gate.get("allowed", True):
        return True
    return "write_gate_blocked" in json.dumps(payload, ensure_ascii=False).lower()


def _has_redmine_typo(argv: list[str]) -> bool:
    return any(item == "--redmine-issuses" for item in argv)


def _fix_redmine_typo_command(argv: list[str]) -> str:
    fixed = ["--redmine-issues" if item == "--redmine-issuses" else item for item in _public_argv(argv)]
    return "/quality-pilot " + " ".join(fixed)


def _canonical_command_from_argv(argv: list[str]) -> str:
    args = _public_argv(argv)
    return "/quality-pilot " + " ".join(args) if args else "/quality-pilot help"


def _public_argv(argv: list[str]) -> list[str]:
    out: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--json":
            continue
        if item == "--root":
            skip_next = True
            continue
        if item.startswith("--root="):
            continue
        out.append(item)
    return out


def _id_resolution_has_target(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(value.get("resolved_gitea_issue_id") or value.get("resolved_case_id"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
