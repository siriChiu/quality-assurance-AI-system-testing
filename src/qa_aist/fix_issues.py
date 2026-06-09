from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .contracts import load_contracts
from .gitea import GiteaClient, gitea_config_from_project
from .issues import dedupe_issues, load_issue_snapshot

FIX_PLAN_NAME = "fix-plan.json"
FIX_RUN_NAME = "fix-run-handoff.json"
FIX_PR_NAME = "fix-pr-result.json"


class FixIssueError(RuntimeError):
    pass


def plan_fix_issue(config: ProjectConfig, *, issue_id: int) -> dict[str, Any]:
    snapshot = load_issue_snapshot(config)
    issue = next((item for item in snapshot.get("items", []) if int(item.get("issue_id", -1)) == issue_id), None)
    duplicates = dedupe_issues(config)
    duplicate_issue_ids = _duplicate_issue_ids(duplicates, issue_id)
    gitea = gitea_config_from_project(config.data)
    branch = f"{gitea.branch_prefix}{issue_id}"
    case_ids = _case_ids_for_issue(config, issue_id, issue)
    blockers: list[str] = []
    if not snapshot.get("synced_at"):
        blockers.append("issue_sync_required")
    if issue is None:
        blockers.append("issue_not_open_or_not_synced")
    if duplicate_issue_ids:
        blockers.append("duplicate_issue_candidates")

    plan = {
        "schema": "qa-aist.fix-plan.v1",
        "status": "blocked" if blockers else "ready",
        "issue_id": issue_id,
        "issue": issue,
        "case_ids": case_ids,
        "branch": branch,
        "base_branch": config.data.get("project", {}).get("default_branch", "main"),
        "blockers": blockers,
        "duplicate_issue_ids": duplicate_issue_ids,
        "preflight": [
            "/qa-aist issues sync",
            f"/qa-aist qa-test run-one {case_ids[0]}" if case_ids else "/qa-aist cases generate --growing",
            "/qa-aist publish plan",
        ],
        "handoff": "Hermes may perform the minimal code change only after this plan is ready.",
    }
    path = fix_plan_path(config)
    config.paths.state.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(plan) + "\n", encoding="utf-8")
    return {**plan, "plan_path": _relative_or_str(path, config.root)}


def run_fix_issue(config: ProjectConfig, *, issue_id: int) -> dict[str, Any]:
    plan = plan_fix_issue(config, issue_id=issue_id)
    if plan["status"] != "ready":
        return {"status": "blocked", "error": "fix_plan_blocked", "plan": plan}
    payload = {
        "schema": "qa-aist.fix-run-handoff.v1",
        "status": "handoff",
        "issue_id": issue_id,
        "branch": plan["branch"],
        "case_ids": plan["case_ids"],
        "instructions": [
            "Create or switch to the planned branch.",
            "Make the minimal product code change needed for the synced open issue.",
            "Run the linked QA-AIST case contracts.",
            "Run /qa-aist publish plan before any remote write.",
            "Run /qa-aist fix-issues submit-pr --issue <id> only after tests and gate pass.",
        ],
    }
    path = config.paths.state / FIX_RUN_NAME
    path.write_text(json_dumps(payload) + "\n", encoding="utf-8")
    return {**payload, "handoff_path": _relative_or_str(path, config.root)}


def submit_fix_pr(config: ProjectConfig, *, issue_id: int, dry_run: bool = False) -> dict[str, Any]:
    plan = plan_fix_issue(config, issue_id=issue_id)
    if plan["status"] != "ready":
        return {"status": "blocked", "error": "fix_plan_blocked", "plan": plan}
    latest_report = config.paths.reports / "status.md"
    title = f"Fix Gitea issue #{issue_id}"
    body = render_pr_body(plan, latest_report)
    payload = {
        "title": title,
        "body": body,
        "head": plan["branch"],
        "base": plan["base_branch"],
    }
    if dry_run:
        return {"status": "dry_run", "issue_id": issue_id, "pr_payload": payload}

    gitea_cfg = gitea_config_from_project(config.data)
    if not gitea_cfg.configured:
        return {"status": "blocked", "error": "gitea_not_configured", "token_env": gitea_cfg.token_env}
    if gitea_cfg.uses_mcp:
        return {
            "status": "blocked",
            "error": "gitea_mcp_write_not_supported",
            "message": "tracker.gitea.backend: mcp is read-only for issue sync. Configure HTTP backend with token_env before /qa-aist fix-issues submit-pr.",
            "backend": gitea_cfg.backend,
        }
    _push_branch(config.root, plan["branch"])
    response = GiteaClient(gitea_cfg).create_pull_request(**payload)
    result = {"status": "ok", "issue_id": issue_id, "pr_payload": payload, "response": response}
    path = config.paths.state / FIX_PR_NAME
    path.write_text(json_dumps(result) + "\n", encoding="utf-8")
    return {**result, "pr_result_path": _relative_or_str(path, config.root)}


def fix_status(config: ProjectConfig) -> dict[str, Any]:
    paths = {
        "plan_path": fix_plan_path(config),
        "handoff_path": config.paths.state / FIX_RUN_NAME,
        "pr_result_path": config.paths.state / FIX_PR_NAME,
    }
    return {
        "status": "ok",
        **{name: _relative_or_str(path, config.root) for name, path in paths.items()},
        **{name.replace("_path", "_exists"): path.exists() for name, path in paths.items()},
    }


def render_pr_body(plan: dict[str, Any], report_path: Path) -> str:
    case_lines = [f"- {case_id}" for case_id in plan.get("case_ids", [])] or ["- No linked case IDs found; see QA-AIST plan."]
    return "\n".join(
        [
            f"Fixes Gitea issue #{plan.get('issue_id')}.",
            "",
            "## QA-AIST verification",
            "",
            *case_lines,
            "",
            f"Report: {report_path}",
            "",
            "This PR was prepared through QA-AIST fix-issues submit-pr after issue sync and duplicate checks.",
        ]
    )


def fix_plan_path(config: ProjectConfig) -> Path:
    return config.paths.state / FIX_PLAN_NAME


def _case_ids_for_issue(config: ProjectConfig, issue_id: int, issue: dict[str, Any] | None) -> list[str]:
    case_ids: list[str] = []
    if issue and issue.get("case_id"):
        case_ids.append(str(issue["case_id"]))
    try:
        for contract in load_contracts(config.paths.cases):
            source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
            if int(source.get("issue_id", -1)) == issue_id:
                case_ids.append(contract.case_id)
    except Exception:
        pass
    return sorted(set(case_ids))


def _duplicate_issue_ids(duplicates: dict[str, Any], issue_id: int) -> list[int]:
    out: list[int] = []
    for group in duplicates.get("duplicates", []):
        ids = [int(item) for item in group.get("issue_ids", []) if item is not None]
        if issue_id in ids:
            out.extend([item for item in ids if item != issue_id])
    return sorted(set(out))


def _push_branch(root: Path, branch: str) -> None:
    current = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=False)
    if current.returncode != 0:
        raise FixIssueError(current.stderr.strip() or "failed to read current branch")
    current_branch = current.stdout.strip()
    if current_branch != branch:
        checkout = subprocess.run(["git", "checkout", "-B", branch], cwd=root, text=True, capture_output=True, check=False)
        if checkout.returncode != 0:
            raise FixIssueError(checkout.stderr.strip() or f"failed to create branch {branch}")
    pushed = subprocess.run(["git", "push", "-u", "origin", branch], cwd=root, text=True, capture_output=True, check=False)
    if pushed.returncode != 0:
        raise FixIssueError(pushed.stderr.strip() or f"failed to push branch {branch}")


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
