from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .contracts import load_contracts
from .gitea import GiteaClient, gitea_config_from_project
from .issues import dedupe_issues, load_issue_snapshot
from .subagents import text_generation_handoff

FIX_PLAN_NAME = "fix-plan.json"
FIX_RUN_NAME = "fix-run-handoff.json"
FIX_PR_NAME = "fix-pr-result.json"
MAX_PR_TITLE_LEN = 120
REDMINE_REF_RE = re.compile(r"\bRedmine\s*#\s*(\d+)\b", re.IGNORECASE)


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
    recovered_case_ids = _recoverable_case_ids(config, issue_id, issue)
    blockers: list[str] = []
    if not snapshot.get("synced_at"):
        blockers.append("issue_sync_required")
    if issue is None:
        blockers.append("issue_not_open_or_not_synced")
    if duplicate_issue_ids:
        blockers.append("duplicate_issue_candidates")
    if issue is not None and not case_ids:
        blockers.append("handoff_case_id_not_runnable")

    plan = {
        "schema": "quality-pilot.fix-plan.v1",
        "status": "blocked" if blockers else "ready",
        "issue_id": issue_id,
        "issue": issue,
        "case_ids": case_ids,
        "recovered_case_ids": recovered_case_ids,
        "branch": branch,
        "base_branch": config.data.get("project", {}).get("default_branch", "main"),
        "blockers": blockers,
        "duplicate_issue_ids": duplicate_issue_ids,
        "preflight": [
            "/quality-pilot issues sync",
            f"/quality-pilot cases run {case_ids[0]}" if case_ids else "/quality-pilot cases generate --growing",
            "/quality-pilot publish wiki status",
            "/quality-pilot publish wiki plan",
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
        if "handoff_case_id_not_runnable" in plan.get("blockers", []):
            return {
                "status": "handoff_blocked",
                "error": "handoff_case_id_not_runnable",
                "issue_id": issue_id,
                "case_ids": plan.get("case_ids", []),
                "recovered_case_ids": plan.get("recovered_case_ids", []),
                "plan": plan,
            }
        return {"status": "blocked", "error": "fix_plan_blocked", "plan": plan}
    payload = {
        "schema": "quality-pilot.fix-run-handoff.v1",
        "status": "handoff",
        "issue_id": issue_id,
        "branch": plan["branch"],
        "case_ids": plan["case_ids"],
        "instructions": [
            "Create or switch to the planned branch.",
            "Make the minimal product code change needed for the synced open issue.",
            "Run the linked AI Quality Pilot case contracts.",
            "Check /quality-pilot publish wiki status, then run /quality-pilot publish wiki plan before any issue remote write.",
            "Run /quality-pilot issues fix --issue <id> --push-pr only after tests and gate pass.",
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
    title = render_pr_title(plan)
    body = render_pr_body(plan, latest_report)
    payload = {
        "title": title,
        "body": body,
        "head": plan["branch"],
        "base": plan["base_branch"],
    }
    if dry_run:
        return {"status": "dry_run", "issue_id": issue_id, "pr_payload": payload, "text_generation": text_generation_handoff(config, "pull_request_body")}

    gitea_cfg = gitea_config_from_project(config.data)
    if not gitea_cfg.configured:
        return {"status": "blocked", "error": "gitea_not_configured", "token_env": gitea_cfg.token_env}
    if gitea_cfg.uses_mcp:
        return {
            "status": "blocked",
            "error": "gitea_mcp_write_not_supported",
            "message": "tracker.gitea.backend: mcp supports issue sync and gated Wiki-only handoff. Configure HTTP backend with token_env before /quality-pilot fix-issues submit-pr creates a pull request.",
            "backend": gitea_cfg.backend,
        }
    _push_branch(config.root, plan["branch"])
    response = GiteaClient(gitea_cfg).create_pull_request(**payload)
    result = {"status": "ok", "issue_id": issue_id, "pr_payload": payload, "text_generation": text_generation_handoff(config, "pull_request_body"), "response": response}
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
    issue = plan.get("issue") if isinstance(plan.get("issue"), dict) else {}
    refs = _issue_refs(plan, issue)
    case_lines = [f"- {case_id}" for case_id in plan.get("case_ids", [])] or [
        "- No linked case IDs were found; add manual verification steps before merging."
    ]
    return "\n".join(
        [
            f"Fixes Gitea issue #{plan.get('issue_id')}.",
            "",
            "## Summary",
            "",
            f"Addresses: {_clean_summary(issue.get('title'))}",
            "",
            "## Problem",
            "",
            _issue_problem_text(issue),
            "",
            "## How to Reproduce",
            "",
            _issue_reproduction_text(issue),
            "",
            "## Linked Tickets",
            "",
            *[f"- {ref}" for ref in refs],
            "",
            "## Verification",
            "",
            *case_lines,
            "",
            f"- Latest report: {report_path}",
            "",
            "## Reviewer Notes",
            "",
            "- Confirm the reproduction path is covered by the linked case or by a manual check.",
            "- Close the linked issue only after the fix is verified in the target environment.",
        ]
    )


def render_pr_title(plan: dict[str, Any]) -> str:
    issue = plan.get("issue") if isinstance(plan.get("issue"), dict) else {}
    refs = _issue_refs(plan, issue)
    prefix = f"Fix {' / '.join(refs)}: "
    summary_limit = max(24, MAX_PR_TITLE_LEN - len(prefix))
    summary = _ellipsize(_clean_summary(issue.get("title")), summary_limit)
    return f"{prefix}{summary}"


def _issue_problem_text(issue: dict[str, Any]) -> str:
    title = _clean_summary(issue.get("title"))
    body = _strip_tooling_noise(issue.get("body"))
    if body:
        return f"{title}\n\n{_ellipsize_block(body, 3000)}"
    return title


def _issue_reproduction_text(issue: dict[str, Any]) -> str:
    body = _strip_tooling_noise(issue.get("body"))
    signal_lines: list[str] = []
    for line in body.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("command:", "steps", "reproduce", "expected", "actual", "observed", "run:", "failure")):
            signal_lines.append(line)
    if signal_lines:
        return "\n".join(signal_lines).strip()
    if body:
        return "Use the problem description above as the starting reproduction context; confirm exact steps with the reporter if the failure is not reproducible."
    return "No reproduction detail was available in the synced issue. Add manual reproduction steps before merging."


def _strip_tooling_noise(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    skipped_phrases = (
        "ai quality pilot",
        "/quality-pilot",
        ".quality-pilot-project",
        "write gate",
        "gitea write gate",
    )
    lines: list[str] = []
    skip_section = False
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        is_heading = stripped.startswith("#")
        if is_heading and any(phrase in lowered for phrase in ("ai quality pilot", "raw redmine json")):
            skip_section = True
            continue
        if skip_section and is_heading:
            skip_section = False
        if skip_section:
            continue
        if any(phrase in lowered for phrase in skipped_phrases):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _ellipsize_block(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def fix_plan_path(config: ProjectConfig) -> Path:
    return config.paths.state / FIX_PLAN_NAME


def _case_ids_for_issue(config: ProjectConfig, issue_id: int, issue: dict[str, Any] | None) -> list[str]:
    case_ids: list[str] = []
    available = _available_case_ids(config)
    if issue and issue.get("case_id") and str(issue["case_id"]) in available:
        case_ids.append(str(issue["case_id"]))
    try:
        for contract in load_contracts(config.paths.cases):
            source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
            if int(source.get("issue_id", -1)) == issue_id or int(source.get("gitea_issue_id", -1)) == issue_id:
                case_ids.append(contract.case_id)
            for redmine_id in _redmine_refs(issue):
                if int(source.get("redmine_issue_id", -1)) == redmine_id:
                    case_ids.append(contract.case_id)
    except Exception:
        pass
    return sorted(set(case_ids))


def _recoverable_case_ids(config: ProjectConfig, issue_id: int, issue: dict[str, Any] | None) -> list[str]:
    recovered: list[str] = []
    available = _available_case_ids(config)
    if issue and issue.get("case_id") and str(issue["case_id"]) in available:
        recovered.append(str(issue["case_id"]))
    for redmine_id in _redmine_refs(issue):
        exact = f"REDMINE-{redmine_id}"
        if exact in available:
            recovered.append(exact)
    try:
        for contract in load_contracts(config.paths.cases):
            source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
            if int(source.get("issue_id", -1)) == issue_id or int(source.get("gitea_issue_id", -1)) == issue_id:
                recovered.append(contract.case_id)
            for redmine_id in _redmine_refs(issue):
                if int(source.get("redmine_issue_id", -1)) == redmine_id:
                    recovered.append(contract.case_id)
    except Exception:
        pass
    return sorted(set(recovered))


def _available_case_ids(config: ProjectConfig) -> set[str]:
    try:
        return {contract.case_id for contract in load_contracts(config.paths.cases)}
    except Exception:
        return set()


def _redmine_refs(issue: dict[str, Any] | None) -> list[int]:
    if not isinstance(issue, dict):
        return []
    text = "\n".join(str(issue.get(key) or "") for key in ("title", "body", "url"))
    return sorted({int(match) for match in REDMINE_REF_RE.findall(text)})


def _duplicate_issue_ids(duplicates: dict[str, Any], issue_id: int) -> list[int]:
    out: list[int] = []
    for group in duplicates.get("duplicates", []):
        ids = [int(item) for item in group.get("issue_ids", []) if item is not None]
        if issue_id in ids:
            out.extend([item for item in ids if item != issue_id])
    return sorted(set(out))


def _issue_refs(plan: dict[str, Any], issue: dict[str, Any]) -> list[str]:
    issue_id = plan.get("issue_id")
    refs = [f"Gitea #{issue_id}" if issue_id is not None else "Gitea issue"]
    haystack = "\n".join(str(issue.get(key) or "") for key in ("title", "body", "url"))
    for redmine_id in REDMINE_REF_RE.findall(haystack):
        ref = f"Redmine #{redmine_id}"
        if ref not in refs:
            refs.append(ref)
    return refs


def _case_suffix(case_ids: Any) -> str:
    if not isinstance(case_ids, list) or not case_ids:
        return ""
    ids = [str(case_id) for case_id in case_ids[:3]]
    if len(case_ids) > 3:
        ids.append("...")
    return f" (cases: {', '.join(ids)})"


def _clean_summary(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -:|")
    text = re.sub(r"(?i)\b(token|password|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
    return text or "linked product issue"


def _ellipsize(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3].rstrip() + "..."


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
