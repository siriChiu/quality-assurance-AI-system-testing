from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .hermes_mcp import hermes_mcp_readiness, mcp_server_is_available
from .issues import issue_fingerprint, load_issue_snapshot
from .runner import utc_now
from .write_gate import evaluate_write_gate

REDMINE_MCP_ENV = "QUALITY_PILOT_REDMINE_MCP_ISSUES_JSON"
REDMINE_IMPORT_NAME = "redmine-import.json"
REDMINE_GITEA_SYNC_STATE_NAME = "redmine-gitea-sync-state.json"
REDMINE_GITEA_CANDIDATES_DIR = "gitea-candidates"
GITEA_ISSUE_WRITE_REQUEST_NAME = "issue-write-request.json"
GITEA_ISSUE_WRITE_RESULT_NAME = "issue-write-result.json"
GITEA_ISSUE_WRITE_REQUEST_SCHEMA = "quality-pilot.gitea-mcp-issue-write-request.v1"


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
    description = str(raw.get("description") or raw.get("body") or "")
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    tracker = raw.get("tracker") if isinstance(raw.get("tracker"), dict) else {}
    project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    return {
        "id": issue_id,
        "subject": subject,
        "description": description,
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
            "## AI Quality Pilot Notes",
            "",
            "- Imported through Hermes Redmine MCP.",
            "- AI Quality Pilot may generate Gitea issue candidates and case contracts from this mirror.",
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
        lines.extend(
            [
                f"#### Entry {index}",
                "",
                f"- User: {user_text}",
                f"- Time: {timestamp}",
                "",
                str(notes or "_No notes._"),
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


def build_redmine_gitea_sync_plan(config: ProjectConfig, issues: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot_items = [item for item in load_issue_snapshot(config).get("items", []) if isinstance(item, dict)]
    candidates = []
    for issue in issues:
        title = f"[Redmine #{issue['id']}] {issue['subject']}"
        body = render_gitea_candidate_body(issue)
        fingerprint = issue_fingerprint(issue["subject"], issue.get("description", ""))
        existing = _find_existing_gitea_issue(issue, fingerprint, snapshot_items)
        candidates.append(
            {
                "id": f"redmine-{issue['id']}",
                "source": "redmine",
                "redmine_issue_id": issue["id"],
                "title": title,
                "body": body,
                "labels": ["redmine", "quality-pilot", "needs-triage"],
                "dedupe_fingerprint": fingerprint,
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
                }
            )
            continue
        if not gate.get("allowed"):
            blocked.append(
                {
                    "id": candidate.get("id"),
                    "redmine_issue_id": candidate.get("redmine_issue_id"),
                    "reason_codes": gate.get("reason_codes", []),
                }
            )
            continue
        actions.append(
            {
                "id": candidate.get("id"),
                "operation": "gitea.issue.create",
                "redmine_issue_id": candidate.get("redmine_issue_id"),
                "title": candidate.get("title"),
                "body": candidate.get("body"),
                "labels": candidate.get("labels", []),
                "dedupe_fingerprint": candidate.get("dedupe_fingerprint"),
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
    lines = [
        f"Imported from Redmine #{issue['id']}.",
        "",
        "## Redmine Ticket",
        "",
        f"- Redmine issue: #{issue['id']}",
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
        "## AI Quality Pilot Traceability",
        "",
        "- Local source mirror: `.quality-pilot-project/issues/redmine-{id}.md`".format(id=issue["id"]),
        "- Generated by `/quality-pilot issues sync --redmine-issues` from Hermes Redmine MCP snapshot.",
        "- Gitea write gate must pass before this candidate is created or linked remotely.",
    ]
    return "\n".join(lines) + "\n"


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


def _find_existing_gitea_issue(issue: dict[str, Any], fingerprint: str, snapshot_items: list[dict[str, Any]]) -> dict[str, Any] | None:
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
