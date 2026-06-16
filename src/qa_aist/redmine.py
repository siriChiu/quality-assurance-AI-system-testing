from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .hermes_mcp import hermes_mcp_readiness, mcp_server_is_available
from .runner import utc_now

REDMINE_MCP_ENV = "QA_AIST_REDMINE_MCP_ISSUES_JSON"
REDMINE_IMPORT_NAME = "redmine-import.json"
REDMINE_GITEA_PLAN_NAME = "redmine-gitea-sync-plan.json"


class RedmineError(RuntimeError):
    pass


def redmine_config(config: ProjectConfig) -> dict[str, Any]:
    tracker = config.data.get("tracker") if isinstance(config.data.get("tracker"), dict) else {}
    redmine = tracker.get("redmine") if isinstance(tracker.get("redmine"), dict) else {}
    mcp = tracker.get("mcp") if isinstance(tracker.get("mcp"), dict) else {}
    return {
        "backend": str(redmine.get("backend") or "mcp").strip().lower(),
        "project": str(redmine.get("project") or ""),
        "mcp_issues_json": str(redmine.get("mcp_issues_json") or mcp.get("redmine_issues_json") or ".qa-aist-project/state/redmine-mcp/issues.json"),
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


def import_redmine_issues(config: ProjectConfig, *, issue_ids: list[int]) -> dict[str, Any]:
    issues = load_redmine_issues(config, issue_ids=issue_ids)
    config.paths.issues.mkdir(parents=True, exist_ok=True)
    config.paths.state.mkdir(parents=True, exist_ok=True)
    mirrors: list[str] = []
    for issue in issues:
        path = config.paths.issues / f"redmine-{issue['id']}.md"
        path.write_text(render_redmine_mirror(issue), encoding="utf-8")
        mirrors.append(_relative_or_str(path, config.root))
    payload = {
        "schema": "qa-aist.redmine-import.v1",
        "status": "ok",
        "synced_at": utc_now(),
        "source": "redmine_mcp",
        "requested_issue_ids": issue_ids,
        "imported_issue_ids": [issue["id"] for issue in issues],
        "mirror_paths": mirrors,
        "issues": issues,
    }
    import_path = config.paths.state / REDMINE_IMPORT_NAME
    import_path.write_text(json_dumps(payload) + "\n", encoding="utf-8")
    plan = build_redmine_gitea_sync_plan(config, issues)
    plan_path = config.paths.state / REDMINE_GITEA_PLAN_NAME
    plan_path.write_text(json_dumps(plan) + "\n", encoding="utf-8")
    return {
        **payload,
        "import_path": _relative_or_str(import_path, config.root),
        "gitea_sync_plan_path": _relative_or_str(plan_path, config.root),
        "gitea_issue_candidates": plan["issue_candidates"],
    }


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
            "## Description",
            "",
            issue.get("description") or "_No description._",
            "",
            "## QA-AIST Notes",
            "",
            "- Imported through Hermes Redmine MCP.",
            "- QA-AIST may generate Gitea issue candidates and case contracts from this mirror.",
            "",
        ]
    )


def build_redmine_gitea_sync_plan(config: ProjectConfig, issues: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        {
            "source": "redmine",
            "redmine_issue_id": issue["id"],
            "title": f"[Redmine #{issue['id']}] {issue['subject']}",
            "body": "\n".join(
                [
                    f"Imported from Redmine #{issue['id']}.",
                    "",
                    issue.get("description") or "_No description._",
                    "",
                    "QA-AIST generated this as a gated Gitea issue candidate; write gate must pass before remote creation/update.",
                ]
            ),
            "labels": ["redmine", "qa-aist"],
            "dedupe_fingerprint": _fingerprint(issue["subject"], issue.get("description", "")),
        }
        for issue in issues
    ]
    return {
        "schema": "qa-aist.redmine-gitea-sync-plan.v1",
        "status": "planned",
        "provider": "gitea",
        "created_at": utc_now(),
        "issue_candidates": candidates,
        "write_gate_required": True,
    }


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


def _fingerprint(title: str, body: str) -> str:
    text = re.sub(r"https?://\S+", "", f"{title}\n{body}".lower())
    words = re.findall(r"[a-z0-9_/-]+", text)
    return " ".join(words[:24])


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
