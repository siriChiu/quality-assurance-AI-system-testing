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
    if path.exists() and requested_issue_ids:
        try:
            found_ids = [int(item["id"]) for item in load_redmine_issues(config, issue_ids=requested_issue_ids)]
        except RedmineError:
            found_ids = []
        missing_ids = [issue_id for issue_id in requested_issue_ids if issue_id not in found_ids]
        if missing_ids:
            blockers.append("redmine_issue_ids_missing")
            checks.append({"name": "tracker.redmine.issue_ids", "status": "WARN", "missing_issue_ids": missing_ids})
        else:
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
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RedmineError(f"redmine_mcp_snapshot_invalid: {path}") from exc
    normalized = [normalize_redmine_issue(item) for item in _extract_issue_list(loaded)]
    requested = {int(issue_id) for issue_id in issue_ids}
    selected = [item for item in normalized if int(item["id"]) in requested]
    missing = sorted(requested - {int(item["id"]) for item in selected})
    if missing:
        raise RedmineError(f"redmine_issue_ids_missing: {missing}")
    return selected


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
