from __future__ import annotations

import json
import os
import hashlib
import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .hermes_mcp import hermes_mcp_readiness, mcp_server_is_available
from .issues import issue_fingerprint, load_issue_snapshot
from .runner import utc_now
from .subagents import text_generation_handoff
from .write_gate import evaluate_write_gate

REDMINE_MCP_ENV = "QUALITY_PILOT_REDMINE_MCP_ISSUES_JSON"
REDMINE_IMPORT_NAME = "redmine-import.json"
REDMINE_GITEA_SYNC_STATE_NAME = "redmine-gitea-sync-state.json"
REDMINE_GITEA_CANDIDATES_DIR = "gitea-candidates"
GITEA_ISSUE_WRITE_REQUEST_NAME = "issue-write-request.json"
GITEA_ISSUE_WRITE_RESULT_NAME = "issue-write-result.json"
GITEA_ISSUE_WRITE_REQUEST_SCHEMA = "quality-pilot.gitea-mcp-issue-write-request.v1"
REDMINE_MCP_SNAPSHOT_SCHEMA = "quality-pilot.redmine-mcp-issues.v1"
REDMINE_MCP_SNAPSHOT_REQUIRED_INCLUDE = ("description", "custom_fields", "journals", "attachments")
REDMINE_DESCRIPTION_KEYS = (
    "description",
    "description_text",
    "descriptionText",
    "description_raw",
    "descriptionRaw",
    "raw_description",
    "rawDescription",
    "full_description",
    "fullDescription",
    "body",
    "text",
    "notes",
    "content",
)
REDMINE_TEXT_KEYS = (
    "text",
    "value",
    "content",
    "body",
    "description",
    "notes",
    "markdown",
    "plain_text",
    "plainText",
    "raw",
)
REDMINE_SAFE_COMMAND_FIELD_HINTS = (
    "safe probe command",
    "safe test command",
    "safe testcase command",
    "safe runner command",
    "safe runner",
    "qa safe command",
    "quality pilot safe probe",
    "quality pilot safe command",
    "verified safe command",
    "read only test command",
    "readonly test command",
    "read-only test command",
    "non destructive test command",
    "non-destructive test command",
)
REDMINE_EXPECTED_EXIT_CODE_FIELD_HINTS = (
    "expected exit code",
    "expected rc",
    "expected return code",
    "safe probe expected exit code",
)
REDMINE_SAFE_FIXTURE_FIELD_HINTS = (
    "fixture",
    "environment",
    "credential",
    "login",
    "config",
    "lab",
    "target",
    "file",
    "workspace",
)
REDMINE_SAFE_ORACLE_FIELD_HINTS = (
    "oracle",
    "pass fail",
    "pass/fail",
    "expected",
    "acceptance",
    "should",
    "expected exit code",
    "expected rc",
    "expected return code",
)
REDMINE_SAFE_SIDE_EFFECT_FIELD_HINTS = (
    "side effect",
    "side-effect",
    "sideeffect",
    "safe boundary",
    "side effect boundary",
    "boundary",
    "read only",
    "read-only",
    "readonly",
    "non destructive",
    "non-destructive",
    "dry run",
)


class RedmineError(RuntimeError):
    pass


def redmine_config(config: ProjectConfig) -> dict[str, Any]:
    tracker = config.data.get("tracker") if isinstance(config.data.get("tracker"), dict) else {}
    redmine = tracker.get("redmine") if isinstance(tracker.get("redmine"), dict) else {}
    mcp = tracker.get("mcp") if isinstance(tracker.get("mcp"), dict) else {}
    return {
        "backend": str(redmine.get("backend") or "mcp").strip().lower(),
        "project": str(redmine.get("project") or ""),
        "mcp_issues_json": str(redmine.get("mcp_issues_json") or mcp.get("redmine_issues_json") or ".quality-pilot-project/state/redmine-mcp/issues.json"),
    }


def redmine_mcp_snapshot_path(config: ProjectConfig) -> Path:
    raw_path = os.getenv(REDMINE_MCP_ENV) or redmine_config(config)["mcp_issues_json"]
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (config.root / path).resolve()


def redmine_readiness(config: ProjectConfig, *, requested_issue_ids: list[int] | None = None) -> dict[str, Any]:
    cfg = redmine_config(config)
    path = redmine_mcp_snapshot_path(config)
    checks: list[dict[str, Any]] = [{"name": "tracker.redmine.backend", "status": "PASS", "value": cfg["backend"]}]
    blockers: list[str] = []
    if cfg["backend"] != "mcp":
        blockers.append("redmine_backend_not_mcp")
        checks[0]["status"] = "WARN"
        checks[0]["message"] = "Redmine V1 supports Hermes MCP snapshots only."
    mcp_ready = hermes_mcp_readiness(config)
    checks.extend([check for check in mcp_ready.get("checks", []) if str(check.get("name")) in {"hermes.mcp.status", "hermes.mcp.redmine"}])
    if path.exists():
        checks.append({"name": "tracker.mcp.redmine_issues_json", "status": "PASS", "path": _relative_or_str(path, config.root)})
    else:
        blockers.append("redmine_mcp_snapshot_missing")
        checks.append(
            {
                "name": "tracker.mcp.redmine_issues_json",
                "status": "WARN",
                "path": _relative_or_str(path, config.root),
                "message": "Use Hermes Redmine MCP to write this issue snapshot before generating Redmine cases.",
            }
        )
    found_ids: list[int] = []
    missing_ids: list[int] = []
    snapshot_validation: dict[str, Any] | None = None
    if path.exists() and requested_issue_ids:
        try:
            loaded = _read_redmine_mcp_snapshot(path)
            snapshot_validation = validate_redmine_mcp_snapshot(config, loaded, issue_ids=requested_issue_ids, path=path)
            if not snapshot_validation["valid"]:
                blockers.append(str(snapshot_validation["error"]))
                checks.append(
                    {
                        "name": "tracker.redmine.snapshot_contract",
                        "status": "WARN",
                        "error": snapshot_validation["error"],
                        "message": snapshot_validation["message"],
                        "path": _relative_or_str(path, config.root),
                    }
                )
            else:
                found_ids = list(snapshot_validation.get("requested_issue_ids", requested_issue_ids))
        except RedmineError as exc:
            found_ids = []
            blockers.append("redmine_mcp_snapshot_invalid")
            checks.append({"name": "tracker.redmine.snapshot_contract", "status": "WARN", "message": str(exc)})
        missing_ids = [issue_id for issue_id in requested_issue_ids if issue_id not in found_ids]
        if missing_ids and not snapshot_validation:
            blockers.append("redmine_issue_ids_missing")
            checks.append({"name": "tracker.redmine.issue_ids", "status": "WARN", "missing_issue_ids": missing_ids})
        elif snapshot_validation and snapshot_validation.get("valid"):
            checks.append({"name": "tracker.redmine.issue_ids", "status": "PASS", "requested_issue_ids": requested_issue_ids})
    return {
        "status": "ready" if not blockers else "blocked",
        "provider": "redmine",
        "backend": cfg["backend"],
        "mcp_issues_json": _relative_or_str(path, config.root),
        "mcp_snapshot_exists": path.exists(),
        "hermes_redmine_mcp_available": mcp_server_is_available(config, "redmine"),
        "hermes_mcp": mcp_ready,
        "requested_issue_ids": requested_issue_ids or [],
        "found_issue_ids": found_ids,
        "missing_issue_ids": missing_ids,
        "snapshot_validation": snapshot_validation,
        "blockers": sorted(set(blockers)),
        "checks": checks,
    }


def sync_redmine_issues(
    config: ProjectConfig,
    *,
    issue_ids: list[int],
    dry_run: bool = False,
    create_gitea_issues: bool = True,
) -> dict[str, Any]:
    if not issue_ids:
        raise RedmineError("--redmine-issues requires at least one issue id")
    issues = load_redmine_issues(config, issue_ids=issue_ids)
    if not dry_run:
        config.paths.issues.mkdir(parents=True, exist_ok=True)
        config.paths.state.mkdir(parents=True, exist_ok=True)
    mirrors: list[str] = []
    for issue in issues:
        path = config.paths.issues / f"redmine-{issue['id']}.md"
        mirrors.append(_relative_or_str(path, config.root))
        if not dry_run:
            path.write_text(render_redmine_mirror(issue), encoding="utf-8")

    plan = build_redmine_gitea_sync_plan(config, issues)
    plan = attach_gitea_issue_write_gate(config, plan)
    candidate_mirrors = _write_gitea_candidate_mirrors(config, plan, dry_run=dry_run)
    write_request = build_gitea_issue_write_request(config, plan) if create_gitea_issues else _empty_gitea_issue_write_request(config, status="not_requested")
    request_path = gitea_issue_write_request_path(config)
    result_path = gitea_issue_write_result_path(config)
    payload = {
        "schema": "quality-pilot.redmine-import.v1",
        "status": _redmine_sync_status(write_request, dry_run=dry_run),
        "synced_at": utc_now(),
        "source": "redmine_mcp",
        "mode": "redmine_issues",
        "requested_issue_ids": issue_ids,
        "imported_issue_ids": [issue["id"] for issue in issues],
        "mirror_paths": mirrors,
        "gitea_issue_candidate_mirror_paths": candidate_mirrors,
        "qa_summary": build_redmine_qa_summary_payload(config, issues),
        "issues": issues,
    }
    import_path = config.paths.state / REDMINE_IMPORT_NAME
    sync_state_path = config.paths.state / REDMINE_GITEA_SYNC_STATE_NAME
    if not dry_run:
        import_path.write_text(json_dumps(payload) + "\n", encoding="utf-8")
        sync_state_path.write_text(json_dumps(plan) + "\n", encoding="utf-8")
        if create_gitea_issues and write_request["actions"]:
            request_path.parent.mkdir(parents=True, exist_ok=True)
            request_path.write_text(json_dumps(write_request) + "\n", encoding="utf-8")
    return {
        **payload,
        "import_path": _relative_or_str(import_path, config.root),
        "gitea_sync_state_path": _relative_or_str(sync_state_path, config.root),
        "gitea_issue_candidates": plan["issue_candidates"],
        "gitea_issue_candidate_count": len(plan["issue_candidates"]),
        "label_resolution": _label_resolution(write_request),
        "remote_write": write_request["status"],
        "blocked_by_gate": write_request["blocked_by_gate"],
        "mcp_issue_write_request": write_request if write_request["actions"] else None,
        "mcp_issue_write_request_path": _relative_or_str(request_path, config.root),
        "mcp_issue_write_result_path": _relative_or_str(result_path, config.root),
        "message": _redmine_sync_message(write_request, dry_run=dry_run),
    }


def import_redmine_issues(config: ProjectConfig, *, issue_ids: list[int]) -> dict[str, Any]:
    if not issue_ids:
        raise RedmineError("--redmine-issues requires at least one issue id")
    issues = load_redmine_issues(config, issue_ids=issue_ids)
    config.paths.issues.mkdir(parents=True, exist_ok=True)
    config.paths.state.mkdir(parents=True, exist_ok=True)
    mirrors: list[str] = []
    for issue in issues:
        path = config.paths.issues / f"redmine-{issue['id']}.md"
        path.write_text(render_redmine_mirror(issue), encoding="utf-8")
        mirrors.append(_relative_or_str(path, config.root))
    payload = {
        "schema": "quality-pilot.redmine-import.v1",
        "status": "ok",
        "synced_at": utc_now(),
        "source": "redmine_mcp",
        "mode": "redmine_case_generation",
        "requested_issue_ids": issue_ids,
        "imported_issue_ids": [issue["id"] for issue in issues],
        "mirror_paths": mirrors,
        "qa_summary": build_redmine_qa_summary_payload(config, issues),
        "issues": issues,
    }
    import_path = config.paths.state / REDMINE_IMPORT_NAME
    import_path.write_text(json_dumps(payload) + "\n", encoding="utf-8")
    return {**payload, "import_path": _relative_or_str(import_path, config.root)}


def load_redmine_issues(config: ProjectConfig, *, issue_ids: list[int]) -> list[dict[str, Any]]:
    path = redmine_mcp_snapshot_path(config)
    if not path.exists():
        raise RedmineError(
            "redmine_mcp_snapshot_missing: tracker.redmine.backend is mcp, but issue snapshot JSON was not found at "
            f"{_relative_or_str(path, config.root)}. Use Hermes Redmine MCP to write raw issues JSON there, "
            f"or set {REDMINE_MCP_ENV}."
        )
    loaded = _read_redmine_mcp_snapshot(path)
    validation = validate_redmine_mcp_snapshot(config, loaded, issue_ids=issue_ids, path=path)
    if not validation["valid"]:
        raise RedmineError(_redmine_snapshot_contract_error_message(config, validation, path))
    normalized = [normalize_redmine_issue(item) for item in _extract_issue_list(loaded)]
    requested = {int(issue_id) for issue_id in issue_ids}
    selected = [item for item in normalized if int(item["id"]) in requested]
    missing = sorted(requested - {int(item["id"]) for item in selected})
    if missing:
        raise RedmineError(f"redmine_issue_ids_missing: {missing}")
    return selected


def _read_redmine_mcp_snapshot(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RedmineError(f"redmine_mcp_snapshot_invalid: {path}") from exc


def validate_redmine_mcp_snapshot(
    config: ProjectConfig,
    loaded: Any,
    *,
    issue_ids: list[int],
    path: Path,
) -> dict[str, Any]:
    requested = sorted({int(issue_id) for issue_id in issue_ids})
    if not isinstance(loaded, dict):
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_unverified",
            "Redmine MCP snapshot must be a manifest object, not a bare issue list.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
        )

    schema = str(loaded.get("schema") or "")
    if schema != REDMINE_MCP_SNAPSHOT_SCHEMA:
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_unverified",
            f"Redmine MCP snapshot must use schema {REDMINE_MCP_SNAPSHOT_SCHEMA}. Legacy/raw snapshots are not allowed for --redmine-issues.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
            actual_schema=schema or None,
        )

    source = str(loaded.get("source") or loaded.get("origin") or loaded.get("fetched_via") or "")
    live_read = bool(loaded.get("live_read") or source in {"hermes_redmine_mcp_live_read", "redmine_mcp_live_read", "hermes_redmine_mcp"})
    if not live_read:
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_unverified",
            "Redmine MCP snapshot must declare a live Hermes Redmine MCP read for this handoff.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
            source=source or None,
        )

    fetched_at = str(loaded.get("fetched_at") or loaded.get("generated_at") or loaded.get("synced_at") or "").strip()
    if not fetched_at:
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_unverified",
            "Redmine MCP snapshot must include fetched_at so stale snapshots are visible.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
        )

    manifest_ids = _manifest_issue_ids(loaded)
    missing_manifest_ids = sorted(set(requested) - set(manifest_ids))
    if missing_manifest_ids:
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_requested_ids_mismatch",
            "Redmine MCP snapshot was not fetched for every requested issue id.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
            snapshot_issue_ids=manifest_ids,
            missing_issue_ids=missing_manifest_ids,
        )

    include = _normalized_redmine_include_fields(loaded)
    missing_include = [field for field in REDMINE_MCP_SNAPSHOT_REQUIRED_INCLUDE if field not in include]
    if missing_include:
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_incomplete_payload",
            "Redmine MCP snapshot must request full issue payload fields: description, custom_fields, journals, attachments.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
            include=sorted(include),
            missing_include=missing_include,
        )

    if not _redmine_full_payload_declared(loaded):
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_incomplete_payload",
            "Redmine MCP snapshot must declare payload_completeness: full or full_payload: true.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
        )

    raw_issues = _extract_issue_list(loaded)
    selected = [item for item in raw_issues if (_raw_redmine_issue_id(item) in set(requested))]
    missing_payload_ids = sorted(set(requested) - {_raw_redmine_issue_id(item) for item in selected if _raw_redmine_issue_id(item) is not None})
    if missing_payload_ids:
        return _redmine_snapshot_validation(
            False,
            "redmine_issue_ids_missing",
            "Redmine MCP snapshot payload does not contain every requested issue id.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
            missing_issue_ids=missing_payload_ids,
        )

    incomplete = []
    for item in selected:
        gaps = _redmine_issue_payload_gaps(item)
        if gaps:
            incomplete.append({"id": _raw_redmine_issue_id(item), "missing": gaps})
    if incomplete:
        return _redmine_snapshot_validation(
            False,
            "redmine_mcp_snapshot_incomplete_payload",
            "Redmine MCP snapshot contains issue entries without the required full payload fields.",
            requested_issue_ids=requested,
            path=path,
            root=config.root,
            incomplete_issues=incomplete,
        )

    return _redmine_snapshot_validation(
        True,
        None,
        "Redmine MCP snapshot is a verified full live-read handoff.",
        requested_issue_ids=requested,
        path=path,
        root=config.root,
        fetched_at=fetched_at,
        include=sorted(include),
        snapshot_issue_ids=manifest_ids,
    )


def normalize_redmine_issue(raw: dict[str, Any]) -> dict[str, Any]:
    issue_id = raw.get("id") or raw.get("issue_id") or raw.get("number")
    try:
        issue_id = int(issue_id)
    except (TypeError, ValueError) as exc:
        raise RedmineError(f"redmine issue has no numeric id: {raw!r}") from exc
    subject = str(raw.get("subject") or raw.get("title") or f"Redmine issue {issue_id}")
    description, description_sources = _redmine_description(raw)
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    tracker = raw.get("tracker") if isinstance(raw.get("tracker"), dict) else {}
    project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    return {
        "id": issue_id,
        "subject": subject,
        "description": description,
        "description_source_fields": description_sources,
        "status": status.get("name") or raw.get("status") or "unknown",
        "tracker": tracker.get("name") or raw.get("tracker") or "",
        "project": project.get("name") or raw.get("project") or "",
        "updated_at": str(raw.get("updated_on") or raw.get("updated_at") or ""),
        "url": str(raw.get("url") or raw.get("html_url") or ""),
        "full_message": render_full_redmine_message(raw, issue_id=issue_id, subject=subject, description=description),
        "raw": raw,
    }


def render_redmine_mirror(issue: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Redmine issue #{issue['id']}: {issue['subject']}",
            "",
            f"- Status: {issue.get('status') or '-'}",
            f"- Tracker: {issue.get('tracker') or '-'}",
            f"- Project: {issue.get('project') or '-'}",
            f"- Updated at: {issue.get('updated_at') or '-'}",
            f"- URL: {issue.get('url') or '-'}",
            "",
            "## Full Redmine Message",
            "",
            issue.get("full_message") or issue.get("description") or "_No Redmine message synced._",
            "",
            "## Local Sync Notes",
            "",
            "- Imported through Hermes Redmine MCP.",
            "- Keep this mirror as the local source copy for later verification and traceability.",
            "",
        ]
    )


def render_full_redmine_message(raw: dict[str, Any], *, issue_id: int, subject: str, description: str) -> str:
    lines = [
        "### Subject",
        "",
        subject or f"Redmine issue {issue_id}",
        "",
        "### Description",
        "",
        description or "_No description._",
    ]
    lines.extend(_render_named_values_section("Custom Fields", raw.get("custom_fields")))
    lines.extend(_render_journal_section(raw))
    lines.extend(_render_named_values_section("Attachments", raw.get("attachments")))
    lines.extend(
        [
            "",
            "### Raw Redmine JSON",
            "",
            "```json",
            json_dumps(raw),
            "```",
        ]
    )
    return "\n".join(lines).strip()


def _redmine_description(raw: dict[str, Any]) -> tuple[str, list[str]]:
    chunks: list[str] = []
    sources: list[str] = []
    for key in REDMINE_DESCRIPTION_KEYS:
        if key not in raw:
            continue
        field_chunks = _redmine_text_chunks(raw.get(key))
        if not field_chunks:
            continue
        chunks.extend(field_chunks)
        sources.append(key)
    for container_key in ("issue", "redmine_issue", "ticket"):
        nested = raw.get(container_key)
        if not isinstance(nested, dict):
            continue
        for key in REDMINE_DESCRIPTION_KEYS:
            if key not in nested:
                continue
            field_chunks = _redmine_text_chunks(nested.get(key))
            if not field_chunks:
                continue
            chunks.extend(field_chunks)
            sources.append(f"{container_key}.{key}")
    return _join_unique_text(chunks), _unique_strings(sources)


def _redmine_text_chunks(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        return [text] if text else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            chunks.extend(_redmine_text_chunks(item))
        return chunks
    if isinstance(value, dict):
        chunks = []
        for key in REDMINE_TEXT_KEYS:
            if key in value:
                chunks.extend(_redmine_text_chunks(value.get(key)))
        if chunks:
            return chunks
        return [json_dumps(value)]
    return [str(value).strip()]


def _join_unique_text(chunks: list[str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        text = str(chunk).replace("\r\n", "\n").replace("\r", "\n").strip()
        key = re.sub(r"\s+", " ", text).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return "\n\n".join(out).strip()


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _render_named_values_section(title: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    lines = ["", f"### {title}", ""]
    for index, item in enumerate(value, start=1):
        if isinstance(item, dict):
            name = item.get("name") or item.get("filename") or item.get("id") or f"item {index}"
            rendered = item.get("value") if "value" in item else item
            lines.append(f"- {name}: {_render_redmine_value(rendered)}")
        else:
            lines.append(f"- {_render_redmine_value(item)}")
    return lines


def _render_journal_section(raw: dict[str, Any]) -> list[str]:
    journals = raw.get("journals")
    if not isinstance(journals, list) or not journals:
        journals = raw.get("comments")
    if not isinstance(journals, list) or not journals:
        return []
    lines = ["", "### Journals / Comments", ""]
    for index, journal in enumerate(journals, start=1):
        if not isinstance(journal, dict):
            lines.extend([f"#### Entry {index}", "", _render_redmine_value(journal), ""])
            continue
        user = journal.get("user")
        if isinstance(user, dict):
            user_text = user.get("name") or user.get("login") or user.get("id") or "-"
        else:
            user_text = user or journal.get("author") or "-"
        timestamp = journal.get("created_on") or journal.get("created_at") or journal.get("updated_on") or journal.get("updated_at") or "-"
        notes = journal.get("notes") if "notes" in journal else journal.get("body", "")
        notes_text = _render_redmine_text_value(notes) or "_No notes._"
        lines.extend(
            [
                f"#### Entry {index}",
                "",
                f"- User: {user_text}",
                f"- Time: {timestamp}",
                "",
                notes_text,
            ]
        )
        details = journal.get("details")
        if details:
            lines.extend(["", "Details:", "", "```json", json_dumps(details), "```"])
        lines.append("")
    return lines


def _render_redmine_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json_dumps(value)
    return str(value)


def _render_redmine_text_value(value: Any) -> str:
    text = _join_unique_text(_redmine_text_chunks(value))
    if text:
        return text
    return _render_redmine_value(value)


def build_redmine_gitea_sync_plan(config: ProjectConfig, issues: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot_items = [item for item in load_issue_snapshot(config).get("items", []) if isinstance(item, dict)]
    write_result_items = _load_issue_write_result_items(config)
    candidates = []
    for issue in issues:
        title = f"[Redmine #{issue['id']}] {issue['subject']}"
        body = render_gitea_candidate_body(issue)
        qa_summary = redmine_issue_qa_summary(issue)
        fingerprint = issue_fingerprint(issue["subject"], issue.get("description", ""))
        existing = _find_existing_gitea_issue(issue, fingerprint, snapshot_items, write_result_items)
        labels = ["redmine", "needs-triage", "needs-reproduction"]
        candidates.append(
            {
                "id": f"redmine-{issue['id']}",
                "source": "redmine",
                "redmine_issue_id": issue["id"],
                "title": title,
                "body": body,
                "labels": labels,
                "qa_summary": qa_summary,
                "qa_text_generation": text_generation_handoff(config, "redmine_issue_summary"),
                "text_generation": text_generation_handoff(config, "gitea_issue_body"),
                "dedupe_fingerprint": fingerprint,
                "idempotency_key": _issue_create_idempotency_key(issue["id"], fingerprint),
                "action": "link_existing_gitea_issue" if existing else "create_gitea_issue_candidate",
                "existing_gitea_issue_id": existing.get("issue_id") if existing else None,
                "existing_gitea_issue_url": existing.get("url") if existing else None,
                "write_gate_required": True,
                "write_gate_result": None,
            }
        )
    return {
        "schema": "quality-pilot.redmine-gitea-sync-state.v1",
        "status": "ready_for_gated_mcp_apply",
        "provider": "gitea",
        "created_at": utc_now(),
        "issue_candidates": candidates,
        "write_gate_required": True,
        "remote_write": "pending_gate",
        "notes": [
            "This state records Redmine tickets and the gated Gitea issue creation request.",
            "Remote Gitea issue creation/update is done through a gated Hermes Gitea MCP issue write request.",
        ],
    }


def attach_gitea_issue_write_gate(config: ProjectConfig, plan: dict[str, Any]) -> dict[str, Any]:
    for candidate in plan.get("issue_candidates", []):
        if not isinstance(candidate, dict):
            continue
        if candidate.get("action") != "create_gitea_issue_candidate":
            candidate["write_gate_result"] = {
                "allowed": True,
                "reason": "existing_gitea_issue_linked",
                "reason_codes": [],
            }
            continue
        text = "\n\n".join([str(candidate.get("title") or ""), str(candidate.get("body") or "")])
        gate = evaluate_write_gate(
            config_data=config.data,
            result={
                "status": "PASS",
                "evidence": ["redmine_mcp_snapshot"],
                "contract_hash": f"redmine-{candidate.get('redmine_issue_id')}",
            },
            target_state="open",
            duplicate_candidate=False,
            sync_current=True,
            write_text=text,
        ).as_dict()
        candidate["write_gate_result"] = gate
    blocked = [item for item in plan.get("issue_candidates", []) if isinstance(item, dict) and not item.get("write_gate_result", {}).get("allowed")]
    plan["blocked_by_gate"] = len(blocked)
    plan["remote_write"] = "gate_blocked" if blocked else "gated_mcp_request_ready"
    return plan


def build_gitea_issue_write_request(config: ProjectConfig, plan: dict[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in plan.get("issue_candidates", []):
        if not isinstance(candidate, dict):
            continue
        gate = candidate.get("write_gate_result") if isinstance(candidate.get("write_gate_result"), dict) else {}
        if candidate.get("action") != "create_gitea_issue_candidate":
            skipped.append(
                {
                    "id": candidate.get("id"),
                    "redmine_issue_id": candidate.get("redmine_issue_id"),
                    "reason": "existing_gitea_issue_linked",
                    "existing_gitea_issue_id": candidate.get("existing_gitea_issue_id"),
                    "idempotency_key": candidate.get("idempotency_key"),
                    "requested_labels": candidate.get("labels", []),
                    "applied_labels": [],
                    "unmatched_labels": candidate.get("labels", []),
                    "label_resolution_note": "skipped_existing_issue_labels_not_modified",
                }
            )
            continue
        if not gate.get("allowed"):
            blocked.append(
                {
                    "id": candidate.get("id"),
                    "redmine_issue_id": candidate.get("redmine_issue_id"),
                    "reason_codes": gate.get("reason_codes", []),
                    "idempotency_key": candidate.get("idempotency_key"),
                    "requested_labels": candidate.get("labels", []),
                    "applied_labels": [],
                    "unmatched_labels": candidate.get("labels", []),
                    "label_resolution_note": "blocked_before_mcp_apply",
                }
            )
            continue
        labels = list(candidate.get("labels", []))
        actions.append(
            {
                "id": candidate.get("id"),
                "operation": "gitea.issue.create",
                "redmine_issue_id": candidate.get("redmine_issue_id"),
                "title": candidate.get("title"),
                "body": candidate.get("body"),
                "labels": labels,
                "qa_summary": candidate.get("qa_summary"),
                "qa_text_generation": candidate.get("qa_text_generation"),
                "text_generation": candidate.get("text_generation"),
                "requested_labels": labels,
                "applied_labels": [],
                "unmatched_labels": labels,
                "label_resolution_note": "pending_mcp_apply",
                "dedupe_fingerprint": candidate.get("dedupe_fingerprint"),
                "idempotency_key": candidate.get("idempotency_key"),
                "write_gate_result": gate,
            }
        )
    if blocked:
        status = "blocked"
    elif actions:
        status = "needs_mcp_apply"
    else:
        status = "no_remote_write_needed"
    return {
        "schema": GITEA_ISSUE_WRITE_REQUEST_SCHEMA,
        "status": status,
        "operation": "gitea.issue.sync_from_redmine",
        "created_at": utc_now(),
        "repo_source": "hermes_session",
        "actions": actions,
        "blocked": blocked,
        "skipped": skipped,
        "blocked_by_gate": len(blocked),
        "safety": {
            "allowed_targets": ["issues"],
            "allowed_operations": ["gitea.issue.create"],
            "source": "redmine_mcp_snapshot",
            "write_gate_required": True,
            "do_not_comment_or_close_existing_issues": True,
        },
        "result_path": _relative_or_str(gitea_issue_write_result_path(config), config.root),
    }


def _empty_gitea_issue_write_request(config: ProjectConfig, *, status: str) -> dict[str, Any]:
    return {
        "schema": GITEA_ISSUE_WRITE_REQUEST_SCHEMA,
        "status": status,
        "operation": "gitea.issue.sync_from_redmine",
        "created_at": utc_now(),
        "repo_source": "hermes_session",
        "actions": [],
        "blocked": [],
        "skipped": [],
        "blocked_by_gate": 0,
        "safety": {
            "allowed_targets": ["issues"],
            "allowed_operations": ["gitea.issue.create"],
            "source": "redmine_mcp_snapshot",
            "write_gate_required": True,
            "do_not_comment_or_close_existing_issues": True,
        },
        "result_path": _relative_or_str(gitea_issue_write_result_path(config), config.root),
    }


def gitea_issue_write_request_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / GITEA_ISSUE_WRITE_REQUEST_NAME


def gitea_issue_write_result_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / GITEA_ISSUE_WRITE_RESULT_NAME


def _redmine_sync_status(write_request: dict[str, Any], *, dry_run: bool) -> str:
    if dry_run:
        return "dry_run"
    return str(write_request.get("status") or "ok")


def _redmine_sync_message(write_request: dict[str, Any], *, dry_run: bool) -> str:
    if dry_run:
        return "Redmine issues parsed in dry-run mode; no local files or Gitea MCP request were written."
    status = write_request.get("status")
    if status == "needs_mcp_apply":
        return "Redmine issues synced locally; gated Hermes Gitea MCP issue creation is required now."
    if status == "blocked":
        return "Redmine issues synced locally, but Gitea issue creation was blocked by the write gate."
    if status == "not_requested":
        return "Redmine issues synced locally for testcase generation; Gitea issue creation was not requested."
    return "Redmine issues synced locally; no new Gitea issue creation was required."


def render_gitea_candidate_body(issue: dict[str, Any]) -> str:
    description = str(issue.get("description") or "").strip()
    lines = [
        "## Problem",
        "",
        f"Redmine #{issue['id']} reports: {issue.get('subject') or 'Untitled issue'}",
        "",
        _first_paragraph(description) or "The Redmine ticket did not include a separate description paragraph.",
        "",
        "## Source Redmine Ticket",
        "",
        f"- Redmine issue: #{issue['id']}",
        f"- Status: {issue.get('status') or '-'}",
        f"- Tracker: {issue.get('tracker') or '-'}",
        f"- Project: {issue.get('project') or '-'}",
        f"- Updated at: {issue.get('updated_at') or '-'}",
        f"- URL: {issue.get('url') or '-'}",
        "",
        "## QA Focus",
        "",
        render_redmine_qa_focus(issue),
        "",
        "## Full Description From Redmine",
        "",
        description or "_No Redmine description was provided._",
        "",
        "## Steps to Reproduce",
        "",
        _render_reproduction_details(issue),
        "",
        "## Observed Result",
        "",
        _render_observed_result(issue),
        "",
        "## Expected Result",
        "",
        _render_expected_result(issue),
        "",
        "## Redmine Fields, Comments, and Attachments",
        "",
        _render_human_redmine_context(issue),
    ]
    return "\n".join(lines) + "\n"


def build_redmine_qa_summary_payload(config: ProjectConfig, issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "quality-pilot.redmine-qa-summary.v1",
        "generated_at": utc_now(),
        "text_generation": text_generation_handoff(config, "redmine_issue_summary"),
        "issues": [redmine_issue_qa_summary(issue) for issue in issues],
    }


def redmine_issue_qa_summary(issue: dict[str, Any]) -> dict[str, Any]:
    missing = redmine_case_missing_inputs(issue)
    return {
        "redmine_issue_id": int(issue["id"]),
        "title": str(issue.get("subject") or ""),
        "status": str(issue.get("status") or ""),
        "updated_at": str(issue.get("updated_at") or ""),
        "problem": _clip_summary(_first_paragraph(str(issue.get("description") or "")) or str(issue.get("subject") or "")),
        "environment": _redmine_environment_hints(issue),
        "reproduction": _clip_summary(_render_reproduction_details(issue), limit=900),
        "observed": _clip_summary(_render_observed_result(issue), limit=700),
        "expected": _clip_summary(_render_expected_result(issue), limit=700),
        "evidence": _redmine_evidence_hints(issue),
        "missing_for_executable_case": missing,
        "human_review_required": bool(missing),
    }


def render_redmine_qa_focus(issue: dict[str, Any]) -> str:
    summary = redmine_issue_qa_summary(issue)
    lines = [
        f"- Problem to verify: {summary['problem'] or issue.get('subject') or '-'}",
        f"- Reproduction path: {summary['reproduction'] or '-'}",
        f"- Observed result: {summary['observed'] or '-'}",
        f"- Expected result: {summary['expected'] or '-'}",
    ]
    environment = summary.get("environment") if isinstance(summary.get("environment"), list) else []
    if environment:
        lines.append(f"- Environment hints: {'; '.join(str(item) for item in environment[:5])}")
    evidence = summary.get("evidence") if isinstance(summary.get("evidence"), list) else []
    if evidence:
        lines.append(f"- Evidence to check: {'; '.join(str(item) for item in evidence[:5])}")
    missing = summary.get("missing_for_executable_case") if isinstance(summary.get("missing_for_executable_case"), list) else []
    if missing:
        lines.append(f"- Missing before an executable testcase: {'; '.join(str(item) for item in missing)}")
    return "\n".join(lines)


def redmine_safe_probe_command(issue: dict[str, Any]) -> dict[str, Any] | None:
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    expected_exit = _redmine_expected_exit(raw)
    for name, value in _custom_field_entries(raw):
        normalized = _normalized_field_name(name)
        if not any(hint in normalized for hint in REDMINE_SAFE_COMMAND_FIELD_HINTS):
            continue
        command = _clean_redmine_command_value(value)
        if command:
            runner_inputs = _redmine_safe_runner_inputs(issue, command_source=name, expected_exit=expected_exit)
            return {
                "run": command,
                "source": name,
                "expected_exit_code": expected_exit["code"],
                "expected_exit_code_source": expected_exit.get("source"),
                "user_confirmed_inputs": runner_inputs,
            }
    return None


def redmine_case_missing_inputs(issue: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    safe_command = redmine_safe_probe_command(issue)
    if safe_command is None:
        missing.append("user-confirmed safe command or runner path")
        missing.append("fixtures, environment, credentials, and side-effect boundaries")
        if not _redmine_has_explicit_expected(issue):
            missing.append("pass/fail oracle or expected result")
        return _unique_strings(missing)
    confirmed = safe_command.get("user_confirmed_inputs") if isinstance(safe_command.get("user_confirmed_inputs"), dict) else {}
    if not confirmed.get("fixtures_environment"):
        missing.append("fixtures, environment, credentials, or explicit none-required note")
    if not confirmed.get("oracle"):
        missing.append("pass/fail oracle or expected result")
    if not confirmed.get("side_effect_boundaries"):
        missing.append("side-effect boundaries")
    return _unique_strings(missing)


def _render_reproduction_details(issue: dict[str, Any]) -> str:
    entries = _matching_custom_fields(
        issue,
        ("repro", "reproduce", "step", "command", "scenario", "procedure", "environment", "model", "platform"),
    )
    description_steps = _description_lines_matching(
        issue,
        ("steps to reproduce", "reproduce:", "1.", "2.", "3.", "run ", "run `"),
    )
    if entries:
        lines = ["Use these Redmine-provided details first:"]
        lines.extend(_format_field_bullets(entries))
        if description_steps:
            lines.append("")
            lines.append("Description step hints:")
            lines.extend(description_steps)
        lines.append("")
        lines.append("If this does not reproduce, use the comments and attachments below to confirm the missing environment detail with the reporter.")
        return "\n".join(lines).strip()
    if description_steps:
        return "\n".join(description_steps)
    return (
        "No dedicated reproduction command or step list was synced from Redmine. "
        "Start from the full description and Redmine comments below, then confirm exact steps with the reporter before changing behavior."
    )


def _clip_summary(value: str, *, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _redmine_environment_hints(issue: dict[str, Any]) -> list[str]:
    entries = _matching_custom_fields(
        issue,
        ("environment", "model", "platform", "version", "fw", "firmware", "bmc", "os", "server", "system", "lab", "target", "board"),
    )
    hints = [f"{name}: {_clip_summary(value, limit=180)}" for name, value in entries]
    hints.extend(
        line[2:]
        for line in _description_lines_matching(
            issue,
            ("environment:", "model:", "platform:", "version:", "firmware:", "bmc:", "lab:", "target:"),
        )
    )
    return _unique_strings([hint for hint in hints if hint.strip()])[:8]


def _redmine_evidence_hints(issue: dict[str, Any]) -> list[str]:
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    hints: list[str] = []
    for line in _human_attachment_lines(raw):
        text = line[2:] if line.startswith("- ") else line
        if text.strip():
            hints.append(_clip_summary(text, limit=220))
    journals = raw.get("journals")
    if not isinstance(journals, list) or not journals:
        journals = raw.get("comments")
    if isinstance(journals, list):
        for journal in journals[:3]:
            if isinstance(journal, dict):
                note = _render_redmine_text_value(journal.get("notes") if "notes" in journal else journal.get("body", ""))
            else:
                note = _render_redmine_text_value(journal)
            note = _clip_summary(note, limit=220)
            if note:
                hints.append(note)
    return _unique_strings(hints)[:8]


def _redmine_has_explicit_expected(issue: dict[str, Any]) -> bool:
    if _matching_custom_fields(issue, ("expected", "acceptance", "target", "should")):
        return True
    return bool(_description_lines_matching(issue, ("expected:", "expected result:", "should ")))


def _redmine_expected_exit(raw: dict[str, Any]) -> dict[str, Any]:
    for name, value in _custom_field_entries(raw):
        normalized = _normalized_field_name(name)
        if not any(hint in normalized for hint in REDMINE_EXPECTED_EXIT_CODE_FIELD_HINTS):
            continue
        match = re.search(r"-?\d+", str(value))
        if match:
            try:
                return {"code": int(match.group(0)), "source": name}
            except ValueError:
                return {"code": 0, "source": name}
    return {"code": 0, "source": None}


def _redmine_expected_exit_code(raw: dict[str, Any]) -> int:
    return int(_redmine_expected_exit(raw)["code"])


def _redmine_safe_runner_inputs(issue: dict[str, Any], *, command_source: str, expected_exit: dict[str, Any]) -> dict[str, Any]:
    fixtures = _safe_input_fields(issue, REDMINE_SAFE_FIXTURE_FIELD_HINTS, exclude_names={command_source})
    oracle = _safe_input_fields(issue, REDMINE_SAFE_ORACLE_FIELD_HINTS, exclude_names={command_source})
    side_effects = _safe_input_fields(issue, REDMINE_SAFE_SIDE_EFFECT_FIELD_HINTS, exclude_names={command_source})
    if expected_exit.get("source"):
        oracle.append({"field": str(expected_exit["source"]), "value": f"expected exit code {expected_exit['code']}"})
    if not oracle and _redmine_has_explicit_expected(issue):
        oracle.extend(
            {"field": name, "value": value}
            for name, value in _matching_custom_fields(issue, ("expected", "acceptance", "target", "should"))
            if name != command_source
        )
    return {
        "command_source": command_source,
        "expected_exit_code": int(expected_exit.get("code", 0)),
        "expected_exit_code_source": expected_exit.get("source"),
        "fixtures_environment": _unique_field_values(fixtures),
        "oracle": _unique_field_values(oracle),
        "side_effect_boundaries": _unique_field_values(side_effects),
    }


def _safe_input_fields(issue: dict[str, Any], hints: tuple[str, ...], *, exclude_names: set[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    excluded = {_normalized_field_name(name) for name in exclude_names}
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    for name, value in _custom_field_entries(raw):
        normalized = _normalized_field_name(name)
        if normalized in excluded:
            continue
        if any(hint in normalized for hint in hints):
            out.append({"field": name, "value": value})
    return out


def _unique_field_values(items: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("field") or ""), str(item.get("value") or ""))
        if key in seen or not key[1].strip():
            continue
        seen.add(key)
        out.append({"field": key[0], "value": key[1]})
    return out


def _normalized_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _clean_redmine_command_value(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        text = text[1:-1].strip()
    return text


def _render_observed_result(issue: dict[str, Any]) -> str:
    entries = _matching_custom_fields(issue, ("actual", "observed", "failure", "symptom", "result"))
    if entries:
        return "\n".join(_format_field_bullets(entries))
    observed_lines = _description_lines_matching(issue, ("observed:", "actual:", "failure:", "symptom:"))
    if observed_lines:
        return "\n".join(observed_lines)
    description = str(issue.get("description") or "").strip()
    return _first_paragraph(description) or str(issue.get("subject") or "The Redmine issue describes the observed failure.")


def _render_expected_result(issue: dict[str, Any]) -> str:
    entries = _matching_custom_fields(issue, ("expected", "acceptance", "target", "should"))
    if entries:
        return "\n".join(_format_field_bullets(entries))
    expected_lines = _description_lines_matching(issue, ("expected:", "expected result:", "should "))
    if expected_lines:
        return "\n".join(expected_lines)
    return (
        "The fix is acceptable when the reproduction path no longer shows the observed failure "
        "and related behavior in the same environment does not regress."
    )


def _render_human_redmine_context(issue: dict[str, Any]) -> str:
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    sections: list[str] = []
    custom_fields = _custom_field_entries(raw)
    if custom_fields:
        sections.extend(["### Custom Fields", "", *_format_field_bullets(custom_fields), ""])
    journals = _human_journal_lines(raw)
    if journals:
        sections.extend(["### Redmine Comments", "", *journals, ""])
    attachments = _human_attachment_lines(raw)
    if attachments:
        sections.extend(["### Attachments", "", *attachments, ""])
    if not sections:
        return "_No custom fields, comments, or attachments were synced from Redmine._"
    return "\n".join(sections).strip()


def _custom_field_entries(raw: dict[str, Any]) -> list[tuple[str, str]]:
    fields = raw.get("custom_fields")
    if not isinstance(fields, list):
        return []
    entries: list[tuple[str, str]] = []
    for index, item in enumerate(fields, start=1):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or f"field {index}")
            value = _render_redmine_text_value(item.get("value") if "value" in item else item)
        else:
            name = f"field {index}"
            value = _render_redmine_text_value(item)
        if value.strip():
            entries.append((name, value.strip()))
    return entries


def _matching_custom_fields(issue: dict[str, Any], hints: tuple[str, ...]) -> list[tuple[str, str]]:
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    matches: list[tuple[str, str]] = []
    for name, value in _custom_field_entries(raw):
        haystack = f"{name}\n{value}".lower()
        if any(hint in haystack for hint in hints):
            matches.append((name, value))
    return matches


def _description_lines_matching(issue: dict[str, Any], hints: tuple[str, ...]) -> list[str]:
    description = str(issue.get("description") or "")
    lines: list[str] = []
    for line in description.splitlines():
        text = line.strip()
        if not text:
            continue
        lowered = text.lower()
        if any(hint in lowered for hint in hints):
            lines.append(f"- {text}")
    return lines


def _format_field_bullets(entries: list[tuple[str, str]]) -> list[str]:
    lines: list[str] = []
    for name, value in entries:
        lines.append(f"- {name}: {_indent_continuation(value)}")
    return lines


def _human_journal_lines(raw: dict[str, Any]) -> list[str]:
    journals = raw.get("journals")
    if not isinstance(journals, list) or not journals:
        journals = raw.get("comments")
    if not isinstance(journals, list) or not journals:
        return []
    lines: list[str] = []
    for index, journal in enumerate(journals, start=1):
        if not isinstance(journal, dict):
            lines.extend([f"#### Comment {index}", "", _render_redmine_text_value(journal), ""])
            continue
        user = journal.get("user")
        if isinstance(user, dict):
            user_text = user.get("name") or user.get("login") or user.get("id") or "-"
        else:
            user_text = user or journal.get("author") or "-"
        timestamp = journal.get("created_on") or journal.get("created_at") or journal.get("updated_on") or journal.get("updated_at") or "-"
        notes = journal.get("notes") if "notes" in journal else journal.get("body", "")
        lines.extend(
            [
                f"#### Comment {index}",
                "",
                f"- Author: {user_text}",
                f"- Time: {timestamp}",
                "",
                _render_redmine_text_value(notes) or "_No comment text._",
                "",
            ]
        )
    return lines


def _human_attachment_lines(raw: dict[str, Any]) -> list[str]:
    attachments = raw.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return []
    lines: list[str] = []
    for index, attachment in enumerate(attachments, start=1):
        if not isinstance(attachment, dict):
            lines.append(f"- {_render_redmine_text_value(attachment)}")
            continue
        filename = attachment.get("filename") or attachment.get("name") or f"attachment {index}"
        size = attachment.get("filesize") or attachment.get("size")
        url = attachment.get("content_url") or attachment.get("url") or attachment.get("download_url")
        suffix = []
        if size:
            suffix.append(f"{size} bytes")
        if url:
            suffix.append(str(url))
        detail = f" ({'; '.join(suffix)})" if suffix else ""
        lines.append(f"- {filename}{detail}")
    return lines


def _indent_continuation(value: str) -> str:
    return str(value).replace("\n", "\n  ")


def _first_paragraph(value: str) -> str:
    for paragraph in re.split(r"\n\s*\n", value.strip()):
        text = paragraph.strip()
        if text:
            return text
    return ""


def render_gitea_candidate_mirror(candidate: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Gitea issue candidate for Redmine #{candidate.get('redmine_issue_id')}",
            "",
            f"- Action: {candidate.get('action')}",
            f"- MCP write status: {candidate.get('write_gate_result', {}).get('reason') if isinstance(candidate.get('write_gate_result'), dict) else '-'}",
            f"- Existing Gitea issue: {candidate.get('existing_gitea_issue_id') or '-'}",
            f"- Existing Gitea URL: {candidate.get('existing_gitea_issue_url') or '-'}",
            f"- Dedupe fingerprint: {candidate.get('dedupe_fingerprint') or '-'}",
            "- Remote write: planned only",
            "- Write gate required: true",
            "",
            "## Title",
            "",
            str(candidate.get("title") or ""),
            "",
            "## Body",
            "",
            str(candidate.get("body") or "").strip(),
            "",
            "## Labels",
            "",
            ", ".join(str(label) for label in candidate.get("labels", [])) or "-",
            "",
        ]
    )


def _write_gitea_candidate_mirrors(config: ProjectConfig, plan: dict[str, Any], *, dry_run: bool) -> list[str]:
    directory = config.paths.issues / REDMINE_GITEA_CANDIDATES_DIR
    paths: list[str] = []
    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
    for candidate in plan.get("issue_candidates", []):
        if not isinstance(candidate, dict):
            continue
        path = directory / f"redmine-{candidate.get('redmine_issue_id')}.md"
        candidate["mirror"] = _relative_or_str(path, config.root)
        paths.append(_relative_or_str(path, config.root))
        if not dry_run:
            path.write_text(render_gitea_candidate_mirror(candidate), encoding="utf-8")
    return paths


def _label_resolution(write_request: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bucket in ("actions", "blocked", "skipped"):
        for item in write_request.get(bucket, []):
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "id": item.get("id"),
                    "redmine_issue_id": item.get("redmine_issue_id"),
                    "requested_labels": item.get("requested_labels", item.get("labels", [])),
                    "applied_labels": item.get("applied_labels", []),
                    "unmatched_labels": item.get("unmatched_labels", []),
                    "label_resolution_note": item.get("label_resolution_note", bucket),
                }
            )
    return rows


def _find_existing_gitea_issue(
    issue: dict[str, Any],
    fingerprint: str,
    snapshot_items: list[dict[str, Any]],
    write_result_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for item in write_result_items:
        if _int_or_none(item.get("redmine_issue_id")) != int(issue["id"]):
            continue
        issue_id = _int_or_none(
            item.get("issue_id")
            or item.get("gitea_issue_id")
            or item.get("number")
            or item.get("index")
            or item.get("id")
        )
        if issue_id is not None:
            return {"issue_id": issue_id, "url": item.get("url") or item.get("html_url")}
    redmine_marker = f"redmine #{issue['id']}".lower()
    for item in snapshot_items:
        title = str(item.get("title") or "")
        body = str(item.get("body") or "")
        if redmine_marker in f"{title}\n{body}".lower():
            return item
    for item in snapshot_items:
        if fingerprint and str(item.get("fingerprint") or "") == fingerprint:
            return item
    return None


def _load_issue_write_result_items(config: ProjectConfig) -> list[dict[str, Any]]:
    path = gitea_issue_write_result_path(config)
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _walk_dicts(loaded)


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


def _issue_create_idempotency_key(redmine_issue_id: int, fingerprint: str) -> str:
    digest = hashlib.sha256(f"redmine:{redmine_issue_id}:{fingerprint}".encode("utf-8")).hexdigest()[:16]
    return f"redmine-{redmine_issue_id}-{digest}"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _redmine_snapshot_validation(
    valid: bool,
    error: str | None,
    message: str,
    *,
    requested_issue_ids: list[int],
    path: Path,
    root: Path,
    **details: Any,
) -> dict[str, Any]:
    return {
        "valid": valid,
        "error": error,
        "message": message,
        "schema": REDMINE_MCP_SNAPSHOT_SCHEMA,
        "path": _relative_or_str(path, root),
        "requested_issue_ids": requested_issue_ids,
        **{key: value for key, value in details.items() if value not in (None, [], {})},
    }


def _redmine_snapshot_contract_error_message(config: ProjectConfig, validation: dict[str, Any], path: Path) -> str:
    requested = " ".join(str(item) for item in validation.get("requested_issue_ids", [])) or "<redmine_issue_id> [<redmine_issue_id> ...]"
    missing = validation.get("missing_issue_ids") or validation.get("missing_include") or validation.get("incomplete_issues") or []
    missing_text = f" details={json_dumps(missing)}" if missing else ""
    return (
        f"{validation.get('error')}: {validation.get('message')} "
        f"path={_relative_or_str(path, config.root)} requested_issue_ids=[{requested}].{missing_text} "
        "Refresh through Hermes Redmine MCP live read before rerunning. The snapshot must be a JSON object with "
        f"schema={REDMINE_MCP_SNAPSHOT_SCHEMA}, source=hermes_redmine_mcp_live_read, fetched_at, "
        "requested_issue_ids, include=[description, custom_fields, journals, attachments], "
        "payload_completeness=full, and issues containing full description, updated_on, custom_fields, journals/comments, and attachments."
    )


def _manifest_issue_ids(loaded: dict[str, Any]) -> list[int]:
    for key in ("requested_issue_ids", "issue_ids", "redmine_issue_ids"):
        value = loaded.get(key)
        if isinstance(value, list):
            ids = [_int_or_none(item) for item in value]
            return sorted(item for item in ids if item is not None)
    manifest = loaded.get("manifest") if isinstance(loaded.get("manifest"), dict) else {}
    for key in ("requested_issue_ids", "issue_ids", "redmine_issue_ids"):
        value = manifest.get(key)
        if isinstance(value, list):
            ids = [_int_or_none(item) for item in value]
            return sorted(item for item in ids if item is not None)
    return []


def _normalized_redmine_include_fields(loaded: dict[str, Any]) -> set[str]:
    raw = loaded.get("include") or loaded.get("fields") or loaded.get("requested_fields") or []
    manifest = loaded.get("manifest") if isinstance(loaded.get("manifest"), dict) else {}
    if not raw:
        raw = manifest.get("include") or manifest.get("fields") or manifest.get("requested_fields") or []
    values = raw if isinstance(raw, list) else [raw]
    normalized = {_normalize_redmine_include_field(item) for item in values}
    normalized.discard("")
    if "comments" in normalized:
        normalized.add("journals")
    return normalized


def _normalize_redmine_include_field(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"journal", "journals", "comment", "comments", "notes"}:
        return "journals"
    if text in {"attachment", "attachments", "files"}:
        return "attachments"
    if text in {"custom_field", "custom_fields", "fields"}:
        return "custom_fields"
    if text in {"description", "body", "full_description", "description_text"}:
        return "description"
    return text


def _redmine_full_payload_declared(loaded: dict[str, Any]) -> bool:
    completeness = str(loaded.get("payload_completeness") or loaded.get("completeness") or "").strip().lower()
    if completeness in {"full", "complete", "full_payload"}:
        return True
    return bool(loaded.get("full_payload") or loaded.get("complete_payload") or loaded.get("full_issue_payload"))


def _raw_redmine_issue_id(raw: dict[str, Any]) -> int | None:
    return _int_or_none(raw.get("id") or raw.get("issue_id") or raw.get("number"))


def _redmine_issue_payload_gaps(raw: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    description, _ = _redmine_description(raw)
    if not description:
        gaps.append("description")
    if not (raw.get("updated_on") or raw.get("updated_at")):
        gaps.append("updated_on")
    if "custom_fields" not in raw or not isinstance(raw.get("custom_fields"), list):
        gaps.append("custom_fields")
    if not (
        ("journals" in raw and isinstance(raw.get("journals"), list))
        or ("comments" in raw and isinstance(raw.get("comments"), list))
    ):
        gaps.append("journals")
    if "attachments" not in raw or not isinstance(raw.get("attachments"), list):
        gaps.append("attachments")
    return gaps


def _extract_issue_list(loaded: Any) -> list[dict[str, Any]]:
    extracted = _maybe_extract_issue_list(loaded)
    if extracted is None:
        raise RedmineError("Redmine MCP JSON must contain an issue list")
    return [item for item in extracted if isinstance(item, dict)]


def _maybe_extract_issue_list(loaded: Any) -> list[Any] | None:
    if isinstance(loaded, list):
        return loaded
    if not isinstance(loaded, dict):
        return None
    for path in [
        ("issues",),
        ("result",),
        ("data", "issues"),
        ("structuredContent", "issues"),
        ("structured_content", "issues"),
    ]:
        value: Any = loaded
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        nested = _maybe_extract_issue_list(value)
        if nested is not None:
            return nested
    content = loaded.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            for key in ("json", "data", "structuredContent", "structured_content"):
                nested = _maybe_extract_issue_list(item.get(key))
                if nested is not None:
                    return nested
            text = item.get("text")
            if isinstance(text, str):
                try:
                    nested = _maybe_extract_issue_list(json.loads(text.strip()))
                except json.JSONDecodeError:
                    nested = None
                if nested is not None:
                    return nested
    return None


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
