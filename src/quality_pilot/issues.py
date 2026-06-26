from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .contracts import list_contract_paths, load_contract
from .gitea import GiteaClient, GiteaError, gitea_config_from_project, issue_number
from .gitea_ledger import reconcile_gitea_mcp_write_results, write_ledger_path
from .hermes_mcp import hermes_mcp_readiness, mcp_server_is_available
from .runner import utc_now

ISSUE_SNAPSHOT_NAME = "issues-snapshot.json"
TRACEABILITY_MAP_NAME = "traceability-map.json"
MCP_ISSUES_ENV = "QUALITY_PILOT_GITEA_MCP_ISSUES_JSON"


class IssueSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class NormalizedIssue:
    issue_id: int
    state: str
    title: str
    body: str
    html_url: str
    updated_at: str
    labels: list[str]
    comments: list[dict[str, Any]]
    pull_requests: list[dict[str, Any]]
    raw: dict[str, Any]

    @property
    def open(self) -> bool:
        return self.state == "open"


def sync_issues(
    config: ProjectConfig,
    *,
    issues_json: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    issues, source_info = _load_input_issues(config, issues_json)
    normalized = [normalize_issue(issue) for issue in issues]
    config.paths.issues.mkdir(parents=True, exist_ok=True)
    config.paths.state.mkdir(parents=True, exist_ok=True)

    open_issues = [issue for issue in normalized if issue.open]
    closed_ids = sorted(issue.issue_id for issue in normalized if not issue.open)
    existing_mirrors = {int(path.stem): path for path in config.paths.issues.glob("*.md") if path.stem.isdigit()}
    removed: list[int] = []
    mirror_paths: list[str] = []
    closed_archive_paths: list[str] = []

    for issue_id in closed_ids:
        path = existing_mirrors.get(issue_id)
        if path and path.exists():
            removed.append(issue_id)
            if not dry_run:
                archived = archive_closed_issue(config, issue_id=issue_id, mirror_path=path)
                closed_archive_paths.append(_relative_or_str(archived, config.root))
                path.unlink()

    for issue in open_issues:
        path = issue_mirror_path(config, issue.issue_id)
        mirror_paths.append(_relative_or_str(path, config.root))
        if not dry_run:
            path.write_text(render_issue_mirror(issue, config), encoding="utf-8")

    snapshot = build_issue_snapshot(config, open_issues)
    snapshot_path = issue_snapshot_path(config)
    if not dry_run:
        reconcile_gitea_mcp_write_results(config)
    traceability_map = build_traceability_map(config, snapshot)
    traceability_path = traceability_map_path(config)
    if not dry_run:
        snapshot_path.write_text(json_dumps(snapshot) + "\n", encoding="utf-8")
        traceability_path.write_text(json_dumps(traceability_map) + "\n", encoding="utf-8")

    return {
        "status": "dry_run" if dry_run else "ok",
        **source_info,
        "snapshot_path": _relative_or_str(snapshot_path, config.root),
        "traceability_map_path": _relative_or_str(traceability_path, config.root),
        "issues_dir": _relative_or_str(config.paths.issues, config.root),
        "open_active_issue_ids": [issue.issue_id for issue in open_issues],
        "closed_issue_ids": closed_ids,
        "closed_archive_paths": closed_archive_paths,
        "removed_mirror_ids": removed,
        "mirror_paths": mirror_paths,
        "open_count": len(open_issues),
        "closed_count": len(closed_ids),
    }


def issue_status(config: ProjectConfig, *, persist_traceability: bool = True) -> dict[str, Any]:
    snapshot = load_issue_snapshot(config)
    mirrors = sorted(path for path in config.paths.issues.glob("*.md")) if config.paths.issues.exists() else []
    readiness = issue_sync_readiness(config)
    write_ledger = reconcile_gitea_mcp_write_results(config)
    traceability_map = build_traceability_map(config, snapshot)
    if persist_traceability:
        _write_traceability_map(config, traceability_map)
    traceability = traceability_map["rows"]
    return {
        "status": "ok",
        "snapshot_exists": issue_snapshot_path(config).exists(),
        "snapshot_path": _relative_or_str(issue_snapshot_path(config), config.root),
        "traceability_map_exists": traceability_map_path(config).exists(),
        "traceability_map_path": _relative_or_str(traceability_map_path(config), config.root),
        "write_ledger_exists": write_ledger_path(config).exists(),
        "write_ledger_path": _relative_or_str(write_ledger_path(config), config.root),
        "write_ledger": {
            "entry_count": write_ledger.get("entry_count", 0),
            "updated_count": write_ledger.get("updated_count", 0),
            "entries": write_ledger.get("entries", []),
        },
        "issues_dir": _relative_or_str(config.paths.issues, config.root),
        "open_count": len(snapshot.get("items", [])),
        "mirror_count": len(mirrors),
        "open_active_issue_ids": [item.get("issue_id") for item in snapshot.get("items", [])],
        "synced_at": snapshot.get("synced_at"),
        "issue_sync": readiness,
        "traceability": traceability,
    }


def issue_sync_readiness(config: ProjectConfig) -> dict[str, Any]:
    tracker = config.data.get("tracker") if isinstance(config.data.get("tracker"), dict) else {}
    provider = str(tracker.get("provider") or "none").lower()
    gitea_cfg = gitea_config_from_project(config.data)
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    provider_ok = provider == "hermes_mcp"
    if not provider_ok:
        blockers.append("tracker_provider_not_hermes_mcp")
        checks.append({
            "name": "tracker.provider",
            "status": "WARN",
            "value": provider,
            "message": "AI Quality Pilot V1 only syncs remote issues through Hermes MCP. Set tracker.provider: hermes_mcp.",
        })
    else:
        checks.append({"name": "tracker.provider", "status": "PASS", "value": provider})

    checks.append({"name": "tracker.backend", "status": "PASS", "value": gitea_cfg.backend})

    if not gitea_cfg.uses_mcp:
        blockers.append("tracker_backend_not_mcp")
        checks.append({
            "name": "tracker.backend",
            "status": "FAIL",
            "value": gitea_cfg.backend,
            "message": "AI Quality Pilot V1 does not use internal Gitea HTTP credentials. Use Hermes Gitea MCP.",
        })

    mcp_ready = hermes_mcp_readiness(config)
    checks.extend([check for check in mcp_ready.get("checks", []) if str(check.get("name")) in {"hermes.mcp.status", "hermes.mcp.gitea"}])
    path = mcp_issues_snapshot_path(config, gitea_cfg)
    if path.exists():
        checks.append({
            "name": "tracker.mcp.gitea_issues_json",
            "status": "PASS",
            "path": _relative_or_str(path, config.root),
        })
    else:
        blockers.append("gitea_mcp_snapshot_missing")
        checks.append({
            "name": "tracker.mcp.gitea_issues_json",
            "status": "WARN",
            "path": _relative_or_str(path, config.root),
            "message": "Use Hermes Gitea MCP read-only fetch to write this JSON before issues sync.",
        })
    issue_sync_ready = provider_ok and gitea_cfg.uses_mcp and path.exists()
    gitea_mcp_known = mcp_server_is_available(config, "gitea")
    return {
        "status": "ready" if issue_sync_ready else "blocked",
        "provider": provider,
        "backend": gitea_cfg.backend,
        "issue_sync_ready": issue_sync_ready,
        "remote_write_ready": bool(provider_ok and gitea_cfg.uses_mcp and gitea_mcp_known),
        "remote_write_reason": "hermes_gitea_mcp" if gitea_mcp_known else "hermes_gitea_mcp_unknown_or_missing",
        "blockers": sorted(set(blockers)),
        "checks": checks,
        "mcp_issues_json": _relative_or_str(path, config.root),
        "mcp_snapshot_exists": path.exists(),
        "hermes_mcp": mcp_ready,
        "snapshot_exists": issue_snapshot_path(config).exists(),
        "snapshot_path": _relative_or_str(issue_snapshot_path(config), config.root),
    }


def show_issue(config: ProjectConfig, issue_id: int) -> dict[str, Any]:
    mirror = issue_mirror_path(config, issue_id)
    if not mirror.exists():
        return {
            "status": "error",
            "error": "issue_mirror_not_found",
            "issue_id": issue_id,
            "mirror_path": _relative_or_str(mirror, config.root),
        }
    snapshot_item = next((item for item in load_issue_snapshot(config).get("items", []) if int(item.get("issue_id", -1)) == issue_id), None)
    return {
        "status": "ok",
        "issue_id": issue_id,
        "mirror_path": _relative_or_str(mirror, config.root),
        "snapshot_item": snapshot_item,
        "content": mirror.read_text(encoding="utf-8"),
    }


def dedupe_issues(config: ProjectConfig) -> dict[str, Any]:
    snapshot = load_issue_snapshot(config)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in snapshot.get("items", []):
        if not isinstance(item, dict):
            continue
        fingerprint = issue_fingerprint(str(item.get("title", "")), str(item.get("body", "")))
        groups.setdefault(fingerprint, []).append(item)
    duplicates = [
        {
            "fingerprint": fingerprint,
            "issue_ids": [item.get("issue_id") for item in items],
            "titles": [item.get("title") for item in items],
        }
        for fingerprint, items in sorted(groups.items())
        if fingerprint and len(items) > 1
    ]
    return {
        "status": "ok",
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "snapshot_path": _relative_or_str(issue_snapshot_path(config), config.root),
    }


def normalize_issue(raw: dict[str, Any]) -> NormalizedIssue:
    number = issue_number(raw)
    if number is None:
        raise IssueSyncError(f"issue entry has no number/index/id: {raw!r}")
    labels = raw.get("labels") or []
    if isinstance(labels, list):
        label_names = [str(label.get("name") if isinstance(label, dict) else label) for label in labels]
    else:
        label_names = [str(labels)]
    comments = raw.get("comments") if isinstance(raw.get("comments"), list) else []
    pull_requests_raw = raw.get("pull_requests") if isinstance(raw.get("pull_requests"), list) else []
    if isinstance(raw.get("pull_request"), dict):
        pull_requests_raw = [raw["pull_request"], *pull_requests_raw]
    return NormalizedIssue(
        issue_id=number,
        state=str(raw.get("state") or "").strip().lower() or "unknown",
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        html_url=str(raw.get("html_url") or raw.get("url") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        labels=label_names,
        comments=[comment for comment in comments if isinstance(comment, dict)],
        pull_requests=[pr for pr in pull_requests_raw if isinstance(pr, dict)],
        raw=raw,
    )


def build_issue_snapshot(config: ProjectConfig, open_issues: list[NormalizedIssue]) -> dict[str, Any]:
    gitea = gitea_config_from_project(config.data)
    return {
        "schema": "quality-pilot.issue-snapshot.v1",
        "synced_at": utc_now(),
        "provider": "gitea",
        "repo": gitea.repo or config.data.get("tracker", {}).get("project", ""),
        "source_of_truth": "Gitea live issue state",
        "closed_issue_policy": "remove_all_local_references",
        "items": [
            {
                "issue_id": issue.issue_id,
                "state": "open",
                "title": issue.title,
                "body": issue.body,
                "url": issue.html_url,
                "updated_at": issue.updated_at,
                "labels": issue.labels,
                "comment_count": len(issue.comments),
                "pull_requests": issue.pull_requests,
                "mirror": _relative_or_str(issue_mirror_path(config, issue.issue_id), config.root),
                "case_id": case_id_for_issue(issue),
                "fingerprint": issue_fingerprint(issue.title, issue.body),
            }
            for issue in sorted(open_issues, key=lambda item: item.issue_id)
        ],
    }


def render_issue_mirror(issue: NormalizedIssue, config: ProjectConfig) -> str:
    labels = ", ".join(issue.labels) if issue.labels else "-"
    comments = render_comments(issue.comments)
    return "\n".join(
        [
            f"# Gitea issue #{issue.issue_id}: {issue.title}",
            "",
            f"- URL: {issue.html_url or '-'}",
            "- State: open",
            f"- Synced at: {utc_now()}",
            f"- Updated at: {issue.updated_at or '-'}",
            f"- Labels: {labels}",
            f"- Pull requests: {len(issue.pull_requests)}",
            f"- Suggested case: {case_id_for_issue(issue)}",
            "",
            "## Body",
            "",
            issue.body.strip() or "_No issue body._",
            "",
            "## Comments",
            "",
            comments,
            "",
            "## Pull Requests",
            "",
            render_pull_requests(issue.pull_requests),
            "",
            "## AI Quality Pilot Notes",
            "",
            "- Source of truth is the live Gitea issue state.",
            "- Closed issues are removed from active mirrors and must not be reopened automatically.",
            "- Use `/quality-pilot cases generate --growing` to grow draft case contracts from current issue and repo state.",
            "",
        ]
    )


def render_comments(comments: list[dict[str, Any]]) -> str:
    if not comments:
        return "_No comments synced._"
    out: list[str] = []
    for comment in comments:
        user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
        author = user.get("login") or user.get("username") or comment.get("author") or "unknown"
        created = comment.get("created_at") or comment.get("updated_at") or "-"
        out.extend([f"### {author} at {created}", "", str(comment.get("body") or "").strip() or "_No body._", ""])
    return "\n".join(out).rstrip()


def render_pull_requests(pull_requests: list[dict[str, Any]]) -> str:
    if not pull_requests:
        return "_No pull request references synced._"
    out: list[str] = []
    for pr in pull_requests:
        number = pr.get("number") or pr.get("index") or pr.get("id") or "-"
        title = pr.get("title") or pr.get("html_url") or pr.get("url") or ""
        state = pr.get("state") or "-"
        out.append(f"- PR {number}: {title} ({state})")
    return "\n".join(out)


def load_issue_snapshot(config: ProjectConfig) -> dict[str, Any]:
    path = issue_snapshot_path(config)
    if not path.exists():
        return {"schema": "quality-pilot.issue-snapshot.v1", "items": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IssueSyncError(f"invalid issue snapshot JSON: {path}") from exc
    return loaded if isinstance(loaded, dict) else {"schema": "quality-pilot.issue-snapshot.v1", "items": []}


def issue_snapshot_path(config: ProjectConfig) -> Path:
    return config.paths.state / ISSUE_SNAPSHOT_NAME


def traceability_map_path(config: ProjectConfig) -> Path:
    return config.paths.state / TRACEABILITY_MAP_NAME


def issue_mirror_path(config: ProjectConfig, issue_id: int) -> Path:
    return config.paths.issues / f"{issue_id}.md"


def archive_closed_issue(config: ProjectConfig, *, issue_id: int, mirror_path: Path) -> Path:
    archive_dir = config.paths.issues / "closed"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / f"{issue_id}.md"
    source = mirror_path.read_text(encoding="utf-8") if mirror_path.exists() else ""
    archived.write_text(
        "\n".join(
            [
                f"# Closed Gitea issue #{issue_id}",
                "",
                f"- Archived at: {utc_now()}",
                f"- Source mirror: {_relative_or_str(mirror_path, config.root)}",
                "",
                "## Last active mirror",
                "",
                source,
            ]
        ),
        encoding="utf-8",
    )
    return archived


def build_traceability(config: ProjectConfig, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    contracts = _contracts_by_case(config)
    latest = _latest_results_by_case(config)
    ledger_links = _issue_links_from_write_ledger(config)
    pr_links = _pr_links_from_write_ledger(config)
    ledger_by_gitea: dict[int, list[dict[str, Any]]] = {}
    for link in ledger_links:
        ledger_by_gitea.setdefault(int(link["gitea_issue_id"]), []).append(link)
    pr_by_gitea: dict[int, list[dict[str, Any]]] = {}
    for link in pr_links:
        pr_by_gitea.setdefault(int(link["gitea_issue_id"]), []).append(link)
    rows: list[dict[str, Any]] = []
    seen_gitea_ids: set[int] = set()
    for item in snapshot.get("items", []):
        if not isinstance(item, dict):
            continue
        issue_id = item.get("issue_id")
        normalized_issue_id = _int_or_none(issue_id)
        case_id = str(item.get("case_id") or "")
        runnable_case_id = case_id if case_id in contracts else _case_for_issue(contracts, issue_id, item)
        contract = contracts.get(runnable_case_id or "")
        coverage = _coverage_for_issue(item, snapshot_case_id=case_id, runnable_case_id=runnable_case_id, contract=contract)
        result = latest.get(runnable_case_id or "")
        linked_entries = ledger_by_gitea.get(normalized_issue_id or -1, [])
        linked_pr_entries = pr_by_gitea.get(normalized_issue_id or -1, [])
        redmine_refs = _merge_redmine_refs(
            _redmine_refs(item),
            [entry["redmine_issue_id"] for entry in linked_entries] + [entry.get("redmine_issue_id") for entry in linked_pr_entries],
        )
        if normalized_issue_id is not None:
            seen_gitea_ids.add(normalized_issue_id)
        rows.append(
            {
                "gitea_issue_id": issue_id,
                "redmine_issue_ids": redmine_refs,
                "case_id": runnable_case_id,
                "snapshot_case_id": case_id or None,
                "case_runnable": bool(runnable_case_id),
                "coverage_status": coverage["status"],
                "coverage_reason": coverage["reason"],
                "repair_action": coverage["repair_action"],
                "latest_status": result.get("status") if isinstance(result, dict) else None,
                "latest_evidence": result.get("evidence", []) if isinstance(result, dict) else [],
                "title": item.get("title"),
                "source": "gitea_snapshot",
                "source_write_ledger_entries": [entry["operation_id"] for entry in linked_entries],
                "pr_linkage": _pr_linkage_summary(linked_pr_entries),
                "source_pr_ledger_entries": [entry["operation_id"] for entry in linked_pr_entries],
            }
        )
    for link in ledger_links:
        gitea_issue_id = int(link["gitea_issue_id"])
        if gitea_issue_id in seen_gitea_ids:
            continue
        synthetic_issue = {
            "issue_id": gitea_issue_id,
            "title": f"Redmine #{link['redmine_issue_id']} linked via Gitea MCP result",
            "body": f"Linked Redmine #{link['redmine_issue_id']} issue created or updated through gated Gitea MCP result.",
            "url": link.get("remote_url") or "",
            "case_id": None,
        }
        runnable_case_id = _case_for_redmine(contracts, link["redmine_issue_id"], gitea_issue_id=gitea_issue_id)
        contract = contracts.get(runnable_case_id or "")
        coverage = _coverage_for_issue(synthetic_issue, snapshot_case_id="", runnable_case_id=runnable_case_id, contract=contract)
        result = latest.get(runnable_case_id or "")
        linked_pr_entries = pr_by_gitea.get(gitea_issue_id, [])
        rows.append(
            {
                "gitea_issue_id": gitea_issue_id,
                "redmine_issue_ids": [link["redmine_issue_id"]],
                "case_id": runnable_case_id,
                "snapshot_case_id": None,
                "case_runnable": bool(runnable_case_id),
                "coverage_status": coverage["status"],
                "coverage_reason": coverage["reason"],
                "repair_action": coverage["repair_action"],
                "latest_status": result.get("status") if isinstance(result, dict) else None,
                "latest_evidence": result.get("evidence", []) if isinstance(result, dict) else [],
                "title": synthetic_issue["title"],
                "source": "gitea_mcp_write_ledger",
                "source_result_status": link.get("result_status"),
                "source_result_path": link.get("result_path"),
                "source_write_ledger_entries": [link["operation_id"]],
                "pr_linkage": _pr_linkage_summary(linked_pr_entries),
                "source_pr_ledger_entries": [entry["operation_id"] for entry in linked_pr_entries],
            }
        )
    return sorted(rows, key=lambda item: (_int_or_none(item.get("gitea_issue_id")) is None, _int_or_none(item.get("gitea_issue_id")) or 0))


def build_traceability_map(config: ProjectConfig, snapshot: dict[str, Any]) -> dict[str, Any]:
    rows = build_traceability(config, snapshot)
    return {
        "schema": "quality-pilot.traceability-map.v1",
        "generated_at": utc_now(),
        "snapshot_path": _relative_or_str(issue_snapshot_path(config), config.root),
        "row_count": len(rows),
        "rows": rows,
    }


def _write_traceability_map(config: ProjectConfig, traceability_map: dict[str, Any]) -> None:
    path = traceability_map_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(traceability_map) + "\n", encoding="utf-8")


def case_id_for_issue(issue: NormalizedIssue | dict[str, Any]) -> str:
    title = issue.title if isinstance(issue, NormalizedIssue) else str(issue.get("title", ""))
    body = issue.body if isinstance(issue, NormalizedIssue) else str(issue.get("body", ""))
    match = re.search(r"\bTC-[A-Z0-9][A-Z0-9-]*\b", f"{title}\n{body}")
    if match:
        return match.group(0)
    issue_id = issue.issue_id if isinstance(issue, NormalizedIssue) else int(issue.get("issue_id", issue.get("number", 0)))
    return f"ISSUE-{issue_id}"


def _contracts_by_case(config: ProjectConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in list_contract_paths(config.paths.cases):
        try:
            contract = load_contract(path)
        except Exception:
            continue
        out[contract.case_id] = contract
    return out


def _latest_results_by_case(config: ProjectConfig) -> dict[str, dict[str, Any]]:
    path = config.paths.state / "latest-run.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for result in loaded.get("results", []) if isinstance(loaded, dict) else []:
        if isinstance(result, dict) and result.get("case_id"):
            out[str(result["case_id"])] = result
    return out


def _case_for_issue(contracts: dict[str, Any], issue_id: Any, issue: dict[str, Any]) -> str | None:
    redmine_refs = _redmine_refs(issue)
    for case_id, contract in contracts.items():
        source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
        if _int_or_none(source.get("issue_id")) == _int_or_none(issue_id):
            return case_id
        if _int_or_none(source.get("gitea_issue_id")) == _int_or_none(issue_id):
            return case_id
        if _int_or_none(source.get("redmine_issue_id")) in redmine_refs:
            return case_id
    return None


def _case_for_redmine(contracts: dict[str, Any], redmine_issue_id: Any, *, gitea_issue_id: Any | None = None) -> str | None:
    redmine_id = _int_or_none(redmine_issue_id)
    gitea_id = _int_or_none(gitea_issue_id)
    if redmine_id is None:
        return None
    direct = f"REDMINE-{redmine_id}"
    if direct in contracts:
        return direct
    for case_id, contract in contracts.items():
        source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
        if _int_or_none(source.get("redmine_issue_id")) == redmine_id:
            return case_id
        if gitea_id is not None and _int_or_none(source.get("gitea_issue_id")) == gitea_id:
            return case_id
        if gitea_id is not None and _int_or_none(source.get("issue_id")) == gitea_id:
            return case_id
    return None


def _coverage_for_issue(
    issue: dict[str, Any],
    *,
    snapshot_case_id: str,
    runnable_case_id: str | None,
    contract: Any | None,
) -> dict[str, Any]:
    redmine_refs = _redmine_refs(issue)
    if not runnable_case_id or contract is None:
        return {
            "status": "no_case",
            "reason": "No runnable case contract exists for this active issue.",
            "repair_action": _repair_action_for_issue(redmine_refs),
        }
    qa = contract.raw.get("quality_pilot") if isinstance(getattr(contract, "raw", {}).get("quality_pilot"), dict) else {}
    questions = qa.get("questions") if isinstance(qa.get("questions"), list) else []
    if qa.get("review_required_before_run") or questions:
        return {
            "status": "needs_input",
            "reason": "The linked case exists but requires user input or review before execution.",
            "repair_action": "/quality-pilot cases review",
        }
    if _contract_has_stale_redmine_probe(contract):
        return {
            "status": "stale_case",
            "reason": "The linked Redmine case still uses a generic probe and is not a confirmed reproduction contract.",
            "repair_action": _repair_action_for_issue(redmine_refs) or f"/quality-pilot cases generate --redmine-issues {_redmine_from_case_id(contract.case_id)}",
        }
    if snapshot_case_id and snapshot_case_id != runnable_case_id and snapshot_case_id.startswith("ISSUE-"):
        return {
            "status": "covered",
            "reason": f"Recovered canonical runnable case {runnable_case_id} from stale snapshot alias {snapshot_case_id}.",
            "repair_action": "/quality-pilot issues sync",
        }
    return {
        "status": "covered",
        "reason": "A runnable linked case contract exists.",
        "repair_action": None,
    }


def _contract_has_stale_redmine_probe(contract: Any) -> bool:
    if not str(getattr(contract, "case_id", "")).upper().startswith("REDMINE-"):
        source = contract.raw.get("source") if isinstance(getattr(contract, "raw", {}).get("source"), dict) else {}
        if "redmine" not in " ".join(str(source.get(key) or "") for key in ("type", "provider", "redmine_issue_id")).lower():
            return False
    qa = contract.raw.get("quality_pilot") if isinstance(getattr(contract, "raw", {}).get("quality_pilot"), dict) else {}
    if str(qa.get("executable_scope") or "") == "side_effect_safe_probe" and not str(qa.get("safe_command_source") or "").strip():
        return True
    return any("__quality_pilot_invalid_command__" in command.run for command in getattr(contract, "commands", []))


def _repair_action_for_issue(redmine_refs: list[int]) -> str | None:
    if redmine_refs:
        joined = " ".join(str(item) for item in redmine_refs)
        return f"/quality-pilot cases generate --redmine-issues {joined}"
    return "/quality-pilot cases generate --growing"


def _redmine_from_case_id(case_id: str) -> str:
    match = re.search(r"REDMINE-(\d+)", case_id, flags=re.IGNORECASE)
    return match.group(1) if match else "<redmine_issue_id>"


def _redmine_refs(issue: dict[str, Any]) -> list[int]:
    text = "\n".join(str(issue.get(key) or "") for key in ("title", "body", "url"))
    return sorted({int(match) for match in re.findall(r"(?i)\bredmine\s*#?\s*(\d+)\b", text)})


def _merge_redmine_refs(primary: list[int], secondary: list[Any]) -> list[int]:
    merged = {_int_or_none(item) for item in [*primary, *secondary]}
    return sorted(item for item in merged if item is not None)


def _issue_links_from_write_ledger(config: ProjectConfig) -> list[dict[str, Any]]:
    path = write_ledger_path(config)
    if not path.exists():
        return []
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    links: list[dict[str, Any]] = []
    for entry in ledger.get("entries", []) if isinstance(ledger, dict) else []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("target_type") or "") not in {"issue_create", "issue_update", "issue_evidence_update"}:
            continue
        redmine_issue_id = _int_or_none(entry.get("redmine_issue_id"))
        gitea_issue_id = _int_or_none(entry.get("remote_id"))
        if redmine_issue_id is None or gitea_issue_id is None:
            continue
        links.append(
            {
                "operation_id": str(entry.get("operation_id") or ""),
                "target_type": entry.get("target_type"),
                "redmine_issue_id": redmine_issue_id,
                "gitea_issue_id": gitea_issue_id,
                "result_status": entry.get("result_status"),
                "result_path": entry.get("result_path"),
                "remote_url": entry.get("remote_url") or entry.get("url"),
            }
        )
    return sorted(links, key=lambda item: (item["gitea_issue_id"], item["redmine_issue_id"], item["operation_id"]))


def _pr_links_from_write_ledger(config: ProjectConfig) -> list[dict[str, Any]]:
    path = write_ledger_path(config)
    if not path.exists():
        return []
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    links: list[dict[str, Any]] = []
    for entry in ledger.get("entries", []) if isinstance(ledger, dict) else []:
        if not isinstance(entry, dict) or str(entry.get("target_type") or "") != "pr_linkage":
            continue
        gitea_issue_id = _int_or_none(entry.get("gitea_issue_id"))
        if gitea_issue_id is None:
            continue
        links.append(
            {
                "operation_id": str(entry.get("operation_id") or ""),
                "gitea_issue_id": gitea_issue_id,
                "redmine_issue_id": _int_or_none(entry.get("redmine_issue_id")),
                "case_id": entry.get("case_id"),
                "case_ids": entry.get("case_ids") if isinstance(entry.get("case_ids"), list) else [],
                "evidence_paths": entry.get("evidence_paths") if isinstance(entry.get("evidence_paths"), list) else [],
                "result_status": entry.get("result_status"),
                "result_path": entry.get("result_path"),
                "pr_number": entry.get("remote_id"),
                "pr_url": entry.get("remote_url"),
                "gate_result": entry.get("gate_result"),
            }
        )
    return sorted(links, key=lambda item: (item["gitea_issue_id"], str(item.get("operation_id") or "")))


def _pr_linkage_summary(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not entries:
        return None
    latest = entries[-1]
    return {
        "count": len(entries),
        "latest_operation_id": latest.get("operation_id"),
        "latest_result_status": latest.get("result_status"),
        "latest_result_path": latest.get("result_path"),
        "pr_number": latest.get("pr_number"),
        "pr_url": latest.get("pr_url"),
        "case_ids": latest.get("case_ids", []),
        "evidence_paths": latest.get("evidence_paths", []),
        "gate_result": latest.get("gate_result"),
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def issue_fingerprint(title: str, body: str) -> str:
    text = re.sub(r"https?://\S+", "", f"{title}\n{body}".lower())
    text = re.sub(r"#[0-9]+", "", text)
    words = re.findall(r"[a-z0-9_/-]+", text)
    return " ".join(words[:24])


def mcp_issues_snapshot_path(config: ProjectConfig, gitea_cfg: Any | None = None) -> Path:
    gitea_cfg = gitea_cfg or gitea_config_from_project(config.data)
    raw_path = os.getenv(MCP_ISSUES_ENV) or gitea_cfg.mcp_issues_json
    path = Path(str(raw_path)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (config.root / path).resolve()


def _load_input_issues(config: ProjectConfig, issues_json: str | Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if issues_json:
        loaded = json.loads(Path(issues_json).read_text(encoding="utf-8"))
        return _extract_issue_list(loaded), {"source": "json", "issues_json": str(Path(issues_json))}

    gitea_cfg = gitea_config_from_project(config.data)
    if gitea_cfg.uses_mcp:
        path = mcp_issues_snapshot_path(config, gitea_cfg)
        if not path.exists():
            raise IssueSyncError(
                "gitea_mcp_snapshot_missing: AI Quality Pilot is configured for Hermes Gitea MCP, but issue snapshot JSON was not found at "
                f"{_relative_or_str(path, config.root)}. Use Hermes Gitea MCP read-only fetch to write raw issues JSON there, "
                f"or set {MCP_ISSUES_ENV}."
            )
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise IssueSyncError(f"gitea_mcp_snapshot_invalid: {path}") from exc
        return _extract_issue_list(loaded), {"source": "mcp", "mcp_issues_json": _relative_or_str(path, config.root)}

    if not gitea_cfg.configured:
        raise GiteaError("Gitea HTTP backend is not configured. New AI Quality Pilot projects should use Hermes MCP snapshots instead.")
    return GiteaClient(gitea_cfg).list_issues(state="all", include_comments=True), {"source": "gitea"}


def _extract_issue_list(loaded: Any) -> list[dict[str, Any]]:
    issues = _maybe_extract_issue_list(loaded)
    if issues is None:
        raise IssueSyncError("issues JSON must be a list, an object with an issues list, or a Gitea MCP content payload")
    return [item for item in issues if isinstance(item, dict)]


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
        value = _nested_get(loaded, path)
        extracted = _maybe_extract_issue_list(value)
        if extracted is not None:
            return extracted

    content = loaded.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            for key in ("json", "data", "structuredContent", "structured_content"):
                extracted = _maybe_extract_issue_list(item.get(key))
                if extracted is not None:
                    return extracted
            text = item.get("text")
            if isinstance(text, str):
                parsed = _parse_json_text(text)
                if parsed is not None:
                    extracted = _maybe_extract_issue_list(parsed)
                    if extracted is not None:
                        return extracted
    return None


def _nested_get(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _parse_json_text(text: str) -> Any | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
