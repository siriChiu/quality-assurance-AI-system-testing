from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import parse

from .config import (
    CONFIG_FILE,
    DEFAULT_PROJECT_WORKSPACE,
    LEGACY_PROJECT_WORKSPACE,
    QAConfigError,
    default_config,
    is_qa_aist_source_checkout,
    json_dumps,
    load_project_config,
    load_yaml,
    project_paths,
    validate_config_data,
    write_if_missing,
)
from .case_generation import (
    CaseGenerationError,
    generate_cases_from_redmine_issues,
    generate_cases_init,
    generate_cases_growing,
    review_generated_cases,
    validate_generated_cases,
)
from .contracts import ContractError, list_contract_paths, load_contract, load_contracts, select_contracts
from .fix_issues import FixIssueError, fix_status, plan_fix_issue, run_fix_issue, submit_fix_pr
from .gitea import GiteaError
from .hermes_mcp import hermes_mcp_readiness
from .issues import IssueSyncError, dedupe_issues, issue_status, issue_sync_readiness, show_issue, sync_issues
from .pipeline import PIPELINE_ORDER, run_close_loop
from .publishing import PublishError, apply_publish_plan, plan_publish, publish_status
from .redmine import RedmineError, redmine_readiness, sync_redmine_issues
from .reports import load_latest_results, render_status_report
from .runner import RunContext, run_case, utc_now
from .templates import EXAMPLE_CONTRACT, EXAMPLE_RUNNER, SWQA_TEST_DESIGN_RULE, WIKI_CATEGORIES_RULE
from .write_gate import evaluate_write_gate
from .wiki import (
    WikiPublishError,
    apply_wiki_plan,
    auto_sync_wiki,
    complete_mcp_wiki_apply,
    plan_wiki,
    render_wiki,
    wiki_readiness,
    wiki_status,
)


REMOVED_COMMAND_REPLACEMENTS: dict[tuple[str, ...], str] = {
    ("init-project",): "/qa-aist setup",
    ("status",): "/qa-aist doctor",
    ("config",): "/qa-aist doctor",
    ("qa-test",): "/qa-aist cases run",
    ("qa-test", "help"): "/qa-aist help",
    ("qa-test", "list"): "/qa-aist cases list",
    ("qa-test", "validate"): "/qa-aist cases validate",
    ("qa-test", "dry-run"): "/qa-aist cases run",
    ("qa-test", "run"): "/qa-aist cases run",
    ("qa-test", "run-one"): "/qa-aist cases run <case_id>",
    ("issues", "dedupe"): "/qa-aist issues sync",
    ("fix-issues",): "/qa-aist issues fix --issue <id>",
    ("fix-issues", "plan"): "/qa-aist issues fix --issue <id>",
    ("fix-issues", "run"): "/qa-aist issues fix --issue <id>",
    ("fix-issues", "submit-pr"): "/qa-aist issues fix --issue <id> --push-pr",
    ("fix-issues", "status"): "/qa-aist issues status",
    ("publish", "plan"): "/qa-aist publish wiki plan",
    ("publish", "apply"): "/qa-aist publish wiki apply",
    ("publish", "status"): "/qa-aist publish wiki status",
    ("publish", "wiki", "render"): "/qa-aist publish wiki plan",
    ("publish", "wiki", "complete-mcp"): "/qa-aist publish wiki apply",
    ("sync-gitea",): "/qa-aist issues sync",
    ("sync-gitea", "pull"): "/qa-aist issues sync",
    ("sync-gitea", "status"): "/qa-aist issues status",
    ("sync-gitea", "validate"): "/qa-aist issues status",
    ("find-new-issues",): "/qa-aist cases generate --growing",
    ("find-new-issues", "run"): "/qa-aist cases generate --growing",
    ("find-new-issues", "dry-run"): "/qa-aist cases generate --growing",
}


def print_json(payload: dict[str, Any], *, exit_code: int = 0) -> int:
    print(json_dumps(payload))
    return exit_code


def cmd_init_project(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = args.workspace or DEFAULT_PROJECT_WORKSPACE
    paths = project_paths(root, workspace)
    tracker_setup = resolve_setup_tracker(root, args)
    if is_qa_aist_source_checkout(paths.workspace):
        return print_json({
            "status": "error",
            "error": "workspace_is_tool_checkout",
            "workspace": str(paths.workspace),
            "message": "Refusing to write host-project assets into a QA-AIST source checkout. Use --workspace .qa-aist-project or another host-owned overlay path.",
        }, exit_code=4)

    for path in [paths.cases, paths.runners, paths.rules, paths.issues, paths.state, paths.evidence, paths.reports]:
        path.mkdir(parents=True, exist_ok=True)

    created = []
    if write_if_missing(paths.config, default_config(str(workspace), **tracker_setup["config_kwargs"]), force=args.force):
        created.append(str(paths.config))
    if write_if_missing(paths.cases / "example-contract.yaml", EXAMPLE_CONTRACT, force=args.force):
        created.append(str(paths.cases / "example-contract.yaml"))
    if write_if_missing(paths.runners / "example-runner.sh", EXAMPLE_RUNNER, executable=True, force=args.force):
        created.append(str(paths.runners / "example-runner.sh"))
    if write_if_missing(paths.rules / "swqa-test-design.md", SWQA_TEST_DESIGN_RULE, force=args.force):
        created.append(str(paths.rules / "swqa-test-design.md"))
    if write_if_missing(paths.rules / "wiki-categories.yaml", WIKI_CATEGORIES_RULE, force=args.force):
        created.append(str(paths.rules / "wiki-categories.yaml"))

    issue_sync = None
    wiki_sync = None
    config_error = None
    if paths.config.exists():
        try:
            config = load_project_config(root)
            issue_sync = issue_sync_readiness(config)
            wiki_sync = wiki_readiness(config)
        except QAConfigError as exc:
            config_error = {"error": exc.error, "message": exc.message, **exc.details}

    return print_json({
        "status": "ok",
        "root": str(root),
        "created": created,
        "workspace": str(paths.workspace),
        "tracker_setup": tracker_setup["payload"],
        "issue_sync": issue_sync,
        "wiki_sync": wiki_sync,
        "config_error": config_error,
        "embedded_tool_checkout_detected": is_qa_aist_source_checkout(root / LEGACY_PROJECT_WORKSPACE),
    })


def cmd_setup(args: argparse.Namespace) -> int:
    return cmd_init_project(args)


def resolve_setup_tracker(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    detected = detect_gitea_remote(root)
    provider = str(getattr(args, "tracker_provider", "auto") or "auto")
    if provider in {"auto", "gitea"}:
        provider = "hermes_mcp"
    backend = "mcp"
    base_url = str(getattr(args, "gitea_base_url", "") or (detected or {}).get("base_url", ""))
    repo = str(getattr(args, "gitea_repo", "") or (detected or {}).get("repo", ""))
    project_name = repo.rsplit("/", 1)[-1] if repo else root.name
    default_branch = detect_default_branch(root) or "main"

    if provider == "none":
        project_name = root.name

    return {
        "config_kwargs": {
            "project_name": project_name or "example-project",
            "default_branch": default_branch,
            "tracker_provider": provider,
            "gitea_backend": backend,
            "gitea_base_url": "",
            "gitea_repo": "",
            "gitea_token_env": "",
        },
        "payload": {
            "provider": provider,
            "backend": backend,
            "mcp_required_servers": ["gitea", "redmine"] if provider == "hermes_mcp" else [],
            "mcp_status_json": f"{DEFAULT_PROJECT_WORKSPACE}/state/hermes-mcp/status.json",
            "gitea_mcp_required": provider == "hermes_mcp",
            "redmine_mcp_required": provider == "hermes_mcp",
            "git_remote_detected": bool(detected),
            "git_remote_url": (detected or {}).get("remote_url"),
            "git_remote_base_url_detected": base_url or None,
            "git_remote_repo_detected": repo or None,
            "auto_configured_mcp": provider == "hermes_mcp",
        },
    }


def detect_default_branch(root: Path) -> str | None:
    for command in [
        ["git", "symbolic-ref", "--short", "HEAD"],
        ["git", "branch", "--show-current"],
    ]:
        result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
        branch = result.stdout.strip()
        if result.returncode == 0 and branch:
            return branch
    return None


def detect_gitea_remote(root: Path) -> dict[str, str] | None:
    result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return None
    remote_url = result.stdout.strip()
    parsed = parse_git_remote(remote_url)
    if not parsed:
        return None
    return {**parsed, "remote_url": remote_url}


def parse_git_remote(remote_url: str) -> dict[str, str] | None:
    raw = remote_url.strip()
    if not raw:
        return None

    scp_match = re.match(r"^[^@]+@([^:]+):(.+)$", raw)
    if scp_match:
        host = scp_match.group(1)
        repo = _clean_repo_path(scp_match.group(2))
        return {"base_url": f"https://{host}", "repo": repo} if repo else None

    parsed = parse.urlparse(raw)
    if parsed.scheme in {"http", "https", "ssh", "git"} and parsed.netloc:
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        base_url = f"https://{netloc}" if parsed.scheme in {"ssh", "git"} else f"{parsed.scheme}://{netloc}"
        repo = _clean_repo_path(parsed.path)
        return {"base_url": base_url.rstrip("/"), "repo": repo} if repo else None
    return None


def _clean_repo_path(path: str) -> str:
    value = path.strip().lstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    parts = [part for part in value.split("/") if part]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return value


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    workspace = args.workspace or DEFAULT_PROJECT_WORKSPACE
    paths = project_paths(root, workspace)
    config_exists = paths.config.exists()
    payload_status = "ok" if config_exists else "setup_required"
    issue_sync = None
    wiki_sync = None
    config_error = None
    if config_exists:
        try:
            config = load_project_config(root)
            issue_sync = issue_sync_readiness(config)
            wiki_sync = wiki_readiness(config)
            if not issue_sync.get("issue_sync_ready"):
                payload_status = "warn"
        except QAConfigError as exc:
            payload_status = "error"
            config_error = {"error": exc.error, "message": exc.message, **exc.details}
    latest_run = paths.state / "latest-run.json"
    latest_payload = None
    if latest_run.exists():
        try:
            latest_payload = json.loads(latest_run.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            latest_payload = {"status": "error", "error": "latest_run_invalid_json"}
    cases = list_contract_paths(paths.cases)
    runners = sorted(paths.runners.glob("*")) if paths.runners.exists() else []
    return print_json({
        "status": payload_status,
        "tool": "qa-aist",
        "root": str(root),
        "config_exists": config_exists,
        "setup_required": not config_exists,
        "config_error": config_error,
        "workspace": str(paths.workspace),
        "workspace_exists": paths.workspace.exists(),
        "workspace_is_tool_checkout": is_qa_aist_source_checkout(paths.workspace),
        "embedded_tool_checkout_detected": is_qa_aist_source_checkout(root / LEGACY_PROJECT_WORKSPACE),
        "case_contract_count": len(cases),
        "runner_count": len([p for p in runners if p.is_file()]),
        "latest_run": latest_payload,
        "issue_sync": issue_sync,
        "wiki_sync": wiki_sync,
    })


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    checks: list[dict[str, Any]] = []
    try:
        config = load_project_config(root, args.config)
        checks.append({"name": "config", "status": "PASS", "path": str(config.path)})
        for name, path in config.paths.as_dict().items():
            if name in {"root", "config"}:
                continue
            checks.append({"name": f"path.{name}", "status": "PASS" if path.exists() else "WARN", "path": str(path)})
        hermes_ready = hermes_mcp_readiness(config)
        checks.extend(hermes_ready.get("checks", []))
        readiness = issue_sync_readiness(config)
        checks.extend(readiness.get("checks", []))
        redmine_ready = redmine_readiness(config)
        checks.extend(redmine_ready.get("checks", []))
        wiki_ready = wiki_readiness(config)
        if wiki_ready.get("remote_write_ready"):
            checks.append({"name": "wiki.remote_write", "status": "PASS", "page": wiki_ready.get("page")})
        else:
            checks.append({
                "name": "wiki.remote_write",
                "status": "WARN",
                "page": wiki_ready.get("page"),
                "message": ", ".join(wiki_ready.get("blockers", [])) or "Wiki remote write is not ready.",
            })
        checks = _dedupe_checks(checks)
        statuses = {str(check.get("status")) for check in checks}
        status = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")
        return print_json({"status": status, "tool": "qa-aist", "checks": checks, "hermes_mcp": hermes_ready, "issue_sync": readiness, "redmine_sync": redmine_ready, "wiki_sync": wiki_ready})
    except QAConfigError as exc:
        return print_json({"status": "FAIL", "error": exc.error, "message": exc.message, **exc.details}, exit_code=2)


def cmd_config_validate(args: argparse.Namespace) -> int:
    config = Path(args.config).resolve()
    try:
        data = load_yaml(config)
    except QAConfigError as exc:
        return print_json({"status": "error", "error": exc.error, "message": exc.message, **exc.details}, exit_code=2)
    errors = validate_config_data(data)
    if errors:
        return print_json({"status": "error", "error": "config_invalid", "errors": errors, "path": str(config)}, exit_code=3)
    return print_json({"status": "ok", "path": str(config)})


def cmd_config_show(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
    except QAConfigError as exc:
        return print_json({"status": "error", "error": exc.error, "message": exc.message, **exc.details}, exit_code=2)
    return print_json({"status": "ok", "path": str(config.path), "config": config.data})


def cmd_qa_test_list(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        contracts = load_contracts(config.paths.cases)
    except (QAConfigError, ContractError) as exc:
        return _error_payload(exc)
    return print_json({"status": "ok", "cases": [_contract_payload(contract) for contract in contracts]})


def cmd_qa_test_validate(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        contracts = load_contracts(config.paths.cases)
    except (QAConfigError, ContractError) as exc:
        return _error_payload(exc)
    return print_json({"status": "ok", "case_count": len(contracts), "cases": [_contract_payload(contract) for contract in contracts]})


def cmd_qa_test_dry_run(args: argparse.Namespace) -> int:
    return _run_cases(args, dry_run=True, one=False)


def cmd_qa_test_run(args: argparse.Namespace) -> int:
    return _run_cases(args, dry_run=False, one=False)


def cmd_qa_test_run_one(args: argparse.Namespace) -> int:
    return _run_cases(args, dry_run=False, one=True)


def cmd_issues_sync(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        if args.redmine_issues:
            payload = sync_redmine_issues(config, issue_ids=args.redmine_issues, dry_run=args.dry_run)
            duplicates = dedupe_issues(config)
            payload["duplicates"] = duplicates.get("duplicates", [])
            payload["duplicate_count"] = duplicates.get("duplicate_count", 0)
            payload["remote_duplicate_actions"] = _remote_duplicate_actions(duplicates)
        else:
            payload = sync_issues(config, issues_json=args.issues_json, dry_run=args.dry_run)
            duplicates = dedupe_issues(config)
            payload["duplicates"] = duplicates.get("duplicates", [])
            payload["duplicate_count"] = duplicates.get("duplicate_count", 0)
            payload["remote_duplicate_actions"] = _remote_duplicate_actions(duplicates)
    except (QAConfigError, IssueSyncError, GiteaError, RedmineError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_issues_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = issue_status(config)
        duplicates = dedupe_issues(config)
        payload["duplicate_count"] = duplicates.get("duplicate_count", 0)
        payload["duplicates"] = duplicates.get("duplicates", [])
        payload["fix"] = fix_status(config)
    except (QAConfigError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_issues_show(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = show_issue(config, args.issue_id)
    except (QAConfigError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") == "ok" else 2)


def cmd_issues_dedupe(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = dedupe_issues(config)
    except (QAConfigError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_issues_fix(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        if args.all:
            snapshot = issue_status(config)
            issue_ids = [int(item) for item in snapshot.get("open_active_issue_ids", []) if item is not None]
            results = []
            for issue_id in issue_ids:
                result = run_fix_issue(config, issue_id=issue_id)
                results.append(result)
                if result.get("status") == "blocked":
                    return print_json({"status": "blocked", "mode": "all", "processed": results, "blocked_issue_id": issue_id}, exit_code=4)
            return print_json({"status": "ok", "mode": "all", "processed_count": len(results), "processed": results})
        if args.issue is None:
            return print_json(
                {
                    "status": "error",
                    "error": "issue_required",
                    "message": "Use /qa-aist issues fix --issue <id> or /qa-aist issues fix --all.",
                },
                exit_code=2,
            )
        if args.push_pr:
            payload = submit_fix_pr(config, issue_id=args.issue, dry_run=False)
            if payload.get("status") == "ok":
                payload = _with_auto_wiki(config, payload, event="gitea_write_summary")
            return print_json(payload, exit_code=0 if payload.get("status") == "ok" else 4)
        payload = run_fix_issue(config, issue_id=args.issue)
    except (QAConfigError, FixIssueError, GiteaError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") != "blocked" else 4)


def cmd_cases_generate(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        generated_count = args.count
        modes = [name for name in ["init", "growing", "redmine_issues"] if getattr(args, name)]
        if not modes:
            return print_json(
                {
                    "status": "error",
                    "error": "explicit_generation_mode_required",
                    "message": "cases generate requires an explicit mode. Use --init, --growing, or --redmine-issues.",
                    "choices": [
                        "/qa-aist cases generate --init",
                        "/qa-aist cases generate --growing",
                        "/qa-aist cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]",
                    ],
                },
                exit_code=2,
            )
        if len(modes) > 1:
            return print_json(
                {
                    "status": "error",
                    "error": "ambiguous_generation_mode",
                    "message": "Choose exactly one generation mode: --init, --growing, or --redmine-issues.",
                    "choices": [
                        "/qa-aist cases generate --init",
                        "/qa-aist cases generate --growing",
                        "/qa-aist cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]",
                    ],
                },
                exit_code=2,
            )
        if args.redmine_issues:
            payload = generate_cases_from_redmine_issues(config, issue_ids=args.redmine_issues, force=args.force)
        elif args.init:
            payload = generate_cases_init(
                config,
                feature=args.feature,
                profile=args.profile,
                count=generated_count,
                fast=True,
                force=args.force,
            )
        else:
            payload = generate_cases_growing(
                config,
                feature=args.feature,
                profile=args.profile,
                count=generated_count,
                fast=True,
                force=args.force,
            )
        payload = _with_auto_wiki(config, payload, event="case_generation")
    except (QAConfigError, CaseGenerationError, IssueSyncError, RedmineError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") != "error" else 2)


def cmd_cases_review(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = review_generated_cases(config)
    except (QAConfigError, CaseGenerationError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_cases_validate(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = validate_generated_cases(config)
    except (QAConfigError, CaseGenerationError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") == "ok" else 3)


def cmd_cases_list(args: argparse.Namespace) -> int:
    return cmd_qa_test_list(args)


def cmd_cases_run(args: argparse.Namespace) -> int:
    return _run_cases(args, dry_run=False, one=bool(args.case_id))


def cmd_cases_push_pr(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        issue_id = _issue_id_for_case_push(config, args.case_id)
        if issue_id is None:
            return print_json(
                {
                    "status": "blocked",
                    "error": "linked_issue_required",
                    "message": "cases push-pr needs a case linked to an open issue. Use /qa-aist issues fix --issue <id> --push-pr when the issue is known.",
                    "case_id": args.case_id,
                },
                exit_code=4,
            )
        payload = submit_fix_pr(config, issue_id=issue_id, dry_run=False)
        if payload.get("status") == "ok":
            payload = _with_auto_wiki(config, payload, event="gitea_write_summary")
    except (QAConfigError, ContractError, FixIssueError, GiteaError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") == "ok" else 4)


def cmd_publish_plan(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = plan_publish(config, latest_run=args.latest_run)
    except (QAConfigError, PublishError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_publish_apply(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = apply_publish_plan(config, plan_path=args.plan)
        if payload.get("status") == "ok":
            payload = _with_auto_wiki(config, payload, event="gitea_write_summary")
    except (QAConfigError, PublishError, GiteaError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") == "ok" else 4)


def cmd_publish_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = publish_status(config)
    except QAConfigError as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_publish_wiki_plan(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = plan_wiki(config, event=args.event, latest_run=args.latest_run)
    except (QAConfigError, WikiPublishError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_publish_wiki_apply(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = apply_wiki_plan(config, plan_path=args.plan)
    except (QAConfigError, WikiPublishError, GiteaError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") in {"ok", "needs_mcp_apply"} else 4)


def cmd_publish_wiki_complete_mcp(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = complete_mcp_wiki_apply(config, result_json=args.result_json)
    except (QAConfigError, WikiPublishError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") == "ok" else 4)


def cmd_publish_wiki_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = wiki_status(config)
    except (QAConfigError, WikiPublishError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_publish_wiki_render(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = render_wiki(config, event=args.event, latest_run=args.latest_run)
    except (QAConfigError, WikiPublishError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_fix_issues_plan(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = plan_fix_issue(config, issue_id=args.issue)
    except (QAConfigError, FixIssueError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") != "error" else 2)


def cmd_fix_issues_run(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = run_fix_issue(config, issue_id=args.issue)
    except (QAConfigError, FixIssueError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") != "blocked" else 4)


def cmd_fix_issues_submit_pr(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = submit_fix_pr(config, issue_id=args.issue, dry_run=args.dry_run)
        if payload.get("status") == "ok":
            payload = _with_auto_wiki(config, payload, event="gitea_write_summary")
    except (QAConfigError, FixIssueError, GiteaError, IssueSyncError) as exc:
        return _error_payload(exc)
    return print_json(payload, exit_code=0 if payload.get("status") in {"ok", "dry_run"} else 4)


def cmd_fix_issues_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = fix_status(config)
    except QAConfigError as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_sync_gitea_pull(args: argparse.Namespace) -> int:
    return cmd_issues_sync(args)


def cmd_sync_gitea_status(args: argparse.Namespace) -> int:
    return cmd_issues_status(args)


def cmd_sync_gitea_validate(args: argparse.Namespace) -> int:
    return cmd_issues_status(args)


def cmd_find_new_issues_run(args: argparse.Namespace) -> int:
    return cmd_publish_plan(args)


def _run_cases(args: argparse.Namespace, *, dry_run: bool, one: bool) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        contracts = select_contracts(config.paths.cases, args.case_id if one or getattr(args, "case_id", None) else None)
        context = RunContext(root=config.root, evidence_dir=config.paths.evidence)
        results = [run_case(contract, context, dry_run=dry_run) for contract in contracts]
    except (QAConfigError, ContractError) as exc:
        return _error_payload(exc)
    status = "PASS"
    if dry_run:
        status = "NOT_RUN"
    elif any(result["status"] == "FAIL" for result in results):
        status = "FAIL"
    elif any(result["status"] == "BLOCK" for result in results):
        status = "BLOCK"
    exit_code = 1 if status == "FAIL" else (2 if status == "BLOCK" else 0)
    payload = {"status": status, "results": results}
    if not dry_run:
        run_payload = _persist_qa_test_run(config, status=status, results=results)
        payload = {**run_payload, "results": results}
        payload = _with_auto_wiki(config, payload, event="test_result", latest_run=run_payload)
    return print_json(payload, exit_code=exit_code)


def cmd_close_loop_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        issue_sync = issue_status(config)
        duplicates = dedupe_issues(config)
        cases = load_contracts(config.paths.cases)
        latest = config.paths.state / "latest-run.json"
        latest_payload = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else None
        wiki_sync = wiki_status(config)
        components = _close_loop_components(
            issue_sync=issue_sync,
            duplicates=duplicates,
            case_count=len(cases),
            latest_run=latest_payload,
            wiki_sync=wiki_sync,
            config=config,
        )
    except QAConfigError as exc:
        return _error_payload(exc)
    except (IssueSyncError, ContractError, WikiPublishError) as exc:
        return _error_payload(exc)
    status = "FAIL" if any(item["status"] == "FAIL" for item in components) else ("WARN" if any(item["status"] == "WARN" for item in components) else "PASS")
    return print_json({"status": status, "closed_loop_components": components, "pipeline_order": PIPELINE_ORDER, "latest_run": latest_payload})


def cmd_close_loop_run_once(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
    except QAConfigError as exc:
        return _error_payload(exc)
    result = run_close_loop(config, case_id=args.case_id, dry_run=args.dry_run)
    payload = result.payload
    if not args.dry_run and result.status in {"PASS", "FAIL", "BLOCK"}:
        payload = _with_auto_wiki(config, payload, event="test_result", latest_run=payload)
    return print_json(payload, exit_code=0 if result.status in {"PASS", "FAIL"} else 2)


def cmd_report_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
    except QAConfigError as exc:
        return _error_payload(exc)
    results = load_latest_results(config.paths.state)
    report_path = render_status_report(results, config.paths.reports / "status.md")
    return print_json({"status": "ok", "report_path": str(report_path), "case_count": len(results)})


def cmd_report_json(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
    except QAConfigError as exc:
        return _error_payload(exc)
    latest = config.paths.state / "latest-run.json"
    if not latest.exists():
        return print_json({"status": "error", "error": "latest_run_not_found"}, exit_code=2)
    print(latest.read_text(encoding="utf-8"))
    return 0


def cmd_tracker_plan_write(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
    except QAConfigError as exc:
        return _error_payload(exc)
    result = None
    if args.result:
        result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    elif (config.paths.state / "latest-run.json").exists():
        latest = json.loads((config.paths.state / "latest-run.json").read_text(encoding="utf-8"))
        result = latest.get("results", [None])[0]
    gate = evaluate_write_gate(
        config_data=config.data,
        result=result,
        target_state=args.target_state,
        expected_contract_hash=args.expected_contract_hash,
    )
    return print_json({"status": "ok", "write_gate_result": gate.as_dict(), "planned_writes": []})


def _persist_qa_test_run(config: Any, *, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    run_id = utc_now().replace(":", "").replace(".", "")
    config.paths.state.mkdir(parents=True, exist_ok=True)
    counts = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "ABORT": 0, "NOT_RUN": 0}
    for result in results:
        key = str(result.get("status", "BLOCK"))
        counts[key] = counts.get(key, 0) + 1
    report_path = render_status_report(results, config.paths.reports / "status.md")
    latest_run_json = config.paths.state / "latest-run.json"
    payload = {
        "status": status,
        "run_id": run_id,
        "case_counts": counts,
        "results": results,
        "latest_run_json": _relative_or_str(latest_run_json, config.root),
        "report_path": _relative_or_str(report_path, config.root),
        "tracker_writes": {"created": 0, "updated": 0, "blocked_by_gate": 0},
        "source": "cases",
    }
    latest_run_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _with_auto_wiki(
    config: Any,
    payload: dict[str, Any],
    *,
    event: str,
    latest_run: dict[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    wiki = auto_sync_wiki(config, event=event, latest_run=latest_run, source_payload=payload, gitea_write_result=payload)
    return {**payload, "wiki": wiki}


def _contract_payload(contract: Any) -> dict[str, Any]:
    return {
        "case_id": contract.case_id,
        "title": contract.title,
        "path": str(contract.path),
        "contract_hash": contract.contract_hash,
        "commands": [{"id": command.id, "run": command.run, "expected_exit_code": command.expected_exit_code} for command in contract.commands],
    }


def _error_payload(exc: Exception) -> int:
    if isinstance(exc, QAConfigError):
        return print_json({"status": "error", "error": exc.error, "message": exc.message, **exc.details}, exit_code=2)
    if isinstance(exc, ContractError):
        payload = {"status": "error", "error": exc.error, "message": exc.message}
        if exc.path:
            payload["path"] = exc.path
        return print_json(payload, exit_code=3)
    if isinstance(exc, (IssueSyncError, GiteaError, CaseGenerationError, PublishError, FixIssueError, WikiPublishError, RedmineError)):
        return print_json({"status": "error", "error": type(exc).__name__, "message": str(exc)}, exit_code=2)
    return print_json({"status": "error", "error": type(exc).__name__, "message": str(exc)}, exit_code=1)


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


PUBLIC_COMMANDS = [
    "/qa-aist help",
    "/qa-aist setup",
    "/qa-aist doctor",
    "/qa-aist issues sync",
    "/qa-aist issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]",
    "/qa-aist issues status",
    "/qa-aist issues show <issue_id>",
    "/qa-aist issues fix --all",
    "/qa-aist issues fix --issue <id>",
    "/qa-aist issues fix --issue <id> --push-pr",
    "/qa-aist cases generate --init",
    "/qa-aist cases generate --init --count 5",
    "/qa-aist cases generate --growing",
    "/qa-aist cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]",
    "/qa-aist cases review",
    "/qa-aist cases validate",
    "/qa-aist cases list",
    "/qa-aist cases run",
    "/qa-aist cases run <case_id>",
    "/qa-aist cases push-pr",
    "/qa-aist cases push-pr <case_id>",
    "/qa-aist publish wiki status",
    "/qa-aist publish wiki plan",
    "/qa-aist publish wiki apply",
    "/qa-aist close-loop status",
    "/qa-aist close-loop run-once",
    "/qa-aist report status",
    "/qa-aist report json",
    "/qa-aist tracker plan-write",
]


def cmd_help(args: argparse.Namespace) -> int:
    return print_json(
        {
            "status": "ok",
            "tool": "qa-aist",
            "command_group": "help",
            "language": "zh-Hant",
            "commands": [{"command": command} for command in PUBLIC_COMMANDS],
            "help_text": "\n".join(
                [
                    "qa-aist> HELP",
                    "QA-AIST 指令總覽",
                    "",
                    "第一次使用建議流程：",
                    "1. /qa-aist setup",
                    "2. /qa-aist doctor",
                    "3. /qa-aist issues sync",
                    "4. /qa-aist cases generate --init",
                    "5. /qa-aist cases validate",
                    "6. /qa-aist cases run",
                    "7. /qa-aist publish wiki apply",
                    "",
                    "正式指令：",
                    *[f"- {command}" for command in PUBLIC_COMMANDS],
                ]
            ),
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-aist", description="Reusable deterministic-first QA toolkit")
    parser.add_argument("--json", action="store_true", help="Emit JSON output; accepted for stable Hermes scripts")
    sub = parser.add_subparsers(dest="command", required=True)

    help_cmd = sub.add_parser("help", help="Show all QA-AIST commands")
    help_cmd.add_argument("--json", action="store_true", help="Emit JSON output")
    help_cmd.set_defaults(func=cmd_help)

    setup = sub.add_parser("setup", help="Initialize QA-AIST project files for the current product repository")
    _add_root_workspace_force(setup)
    setup.set_defaults(func=cmd_setup)

    doctor = sub.add_parser("doctor", help="Check install, config, paths, and secret references")
    _add_root_config(doctor)
    doctor.set_defaults(func=cmd_doctor)

    issues = sub.add_parser("issues", help="Issue sync, status, show, and fix commands")
    issues_sub = issues.add_subparsers(dest="issues_command", required=True)
    issues_sync = issues_sub.add_parser("sync", help="Sync local issue mirrors from Gitea, Redmine MCP, or an issues JSON file")
    _add_root_config(issues_sync)
    issues_sync.add_argument("--issues-json", default=None, help="Offline JSON input for tests or manual sync")
    issues_sync.add_argument("--redmine-issues", nargs="+", type=int, default=[], help="Read one or more Redmine MCP issue IDs, sync mirrors, and create gated Gitea issues through Hermes MCP")
    issues_sync.add_argument("--dry-run", action="store_true", help="Preview mirror/snapshot changes without writing")
    issues_sync.set_defaults(func=cmd_issues_sync)
    issues_status = issues_sub.add_parser("status", help="Show local issue sync state")
    _add_root_config(issues_status)
    issues_status.set_defaults(func=cmd_issues_status)
    issues_show = issues_sub.add_parser("show", help="Show one local issue mirror")
    _add_root_config(issues_show)
    issues_show.add_argument("issue_id", type=int)
    issues_show.set_defaults(func=cmd_issues_show)
    issues_fix = issues_sub.add_parser("fix", help="Fix synced issues and optionally push a PR")
    _add_root_config(issues_fix)
    scope = issues_fix.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="Fix all synced open issues until blocked")
    scope.add_argument("--issue", type=int, help="Fix one synced open issue")
    issues_fix.add_argument("--push-pr", action="store_true", help="Push branch and create a PR after issue fix checks pass")
    issues_fix.set_defaults(func=cmd_issues_fix)

    cases = sub.add_parser("cases", help="Generate, review, and validate case contracts")
    cases_sub = cases.add_subparsers(dest="cases_command", required=True)
    cases_generate = cases_sub.add_parser("generate", help="Generate case contracts; requires --init, --growing, or --redmine-issues")
    _add_root_config(cases_generate)
    cases_generate.add_argument("--init", action="store_true", help="First-time full-repo SWQA generation from README, code, metadata, runners, and rules")
    cases_generate.add_argument("--growing", action="store_true", help="Incremental growth-mode generation from latest repo/issues/PR/run/report state")
    cases_generate.add_argument("--redmine-issues", nargs="+", type=int, default=[], help="Read one or more Redmine MCP issue IDs directly and generate linked cases")
    cases_generate.add_argument("--feature", default=None, help="Feature or user-visible surface to bias growth generation")
    cases_generate.add_argument("--profile", default="auto", choices=["auto", "cli", "api", "hardware", "repo"], help="Generation profile; auto inspects repo signals")
    cases_generate.add_argument("--count", type=int, default=None, help="Optional maximum number of cases to generate, for example --count 5")
    cases_generate.add_argument("--force", action="store_true", help="Overwrite existing generated case YAML")
    cases_generate.set_defaults(func=cmd_cases_generate)
    cases_review = cases_sub.add_parser("review", help="List generated drafts and Q&A prompts")
    _add_root_config(cases_review)
    cases_review.set_defaults(func=cmd_cases_review)
    cases_validate = cases_sub.add_parser("validate", help="Validate generated case contracts")
    _add_root_config(cases_validate)
    cases_validate.set_defaults(func=cmd_cases_validate)
    cases_list = cases_sub.add_parser("list", help="List case contracts")
    _add_root_config(cases_list)
    cases_list.set_defaults(func=cmd_cases_list)
    cases_run = cases_sub.add_parser("run", help="Run all case contracts or one case_id")
    _add_root_config(cases_run)
    cases_run.add_argument("case_id", nargs="?")
    cases_run.set_defaults(func=cmd_cases_run)
    cases_push_pr = cases_sub.add_parser("push-pr", help="Create a product fix PR for linked failing case(s)")
    _add_root_config(cases_push_pr)
    cases_push_pr.add_argument("case_id", nargs="?")
    cases_push_pr.set_defaults(func=cmd_cases_push_pr)

    close_loop = sub.add_parser("close-loop", help="Fixed QA close-loop pipeline")
    close_sub = close_loop.add_subparsers(dest="close_command", required=True)
    close_status = close_sub.add_parser("status", help="Show pipeline order and latest run")
    _add_root_config(close_status)
    close_status.set_defaults(func=cmd_close_loop_status)
    run_once = close_sub.add_parser("run-once", help="Run the fixed deterministic pipeline once")
    _add_root_config(run_once)
    run_once.add_argument("--case-id", default=None)
    run_once.add_argument("--dry-run", action="store_true")
    run_once.set_defaults(func=cmd_close_loop_run_once)

    report = sub.add_parser("report", help="Report commands")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_status = report_sub.add_parser("status", help="Render markdown status report")
    _add_root_config(report_status)
    report_status.set_defaults(func=cmd_report_status)
    report_json = report_sub.add_parser("json", help="Print latest run JSON")
    _add_root_config(report_json)
    report_json.set_defaults(func=cmd_report_json)

    tracker = sub.add_parser("tracker", help="Tracker dry-run planning commands")
    tracker_sub = tracker.add_subparsers(dest="tracker_command", required=True)
    plan_write = tracker_sub.add_parser("plan-write", help="Evaluate write gate without writing tracker")
    _add_root_config(plan_write)
    plan_write.add_argument("--result", default=None)
    plan_write.add_argument("--target-state", default="unknown", choices=["open", "closed", "missing", "unknown"])
    plan_write.add_argument("--expected-contract-hash", default=None)
    plan_write.set_defaults(func=cmd_tracker_plan_write)

    publish = sub.add_parser("publish", help="Plan or apply gated Gitea wiki/issues writes")
    publish_sub = publish.add_subparsers(dest="publish_command", required=True)
    publish_wiki = publish_sub.add_parser("wiki", help="Plan, apply, or status the Gitea Wiki status page")
    wiki_sub = publish_wiki.add_subparsers(dest="publish_wiki_command", required=True)
    wiki_plan = wiki_sub.add_parser("plan", help="Create a gated Wiki-only status plan")
    _add_root_config(wiki_plan)
    wiki_plan.add_argument("--event", default="manual", choices=["manual", "case_generation", "test_result", "gitea_write_summary"])
    wiki_plan.add_argument("--latest-run", default=None)
    wiki_plan.set_defaults(func=cmd_publish_wiki_plan)
    wiki_apply = wiki_sub.add_parser("apply", help="Apply a gated Wiki-only status plan")
    _add_root_config(wiki_apply)
    wiki_apply.add_argument("--plan", default=None)
    wiki_apply.set_defaults(func=cmd_publish_wiki_apply)
    wiki_status_cmd = wiki_sub.add_parser("status", help="Show latest Wiki plan/apply status")
    _add_root_config(wiki_status_cmd)
    wiki_status_cmd.set_defaults(func=cmd_publish_wiki_status)

    return parser


def _add_root_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Target project root")
    parser.add_argument("--workspace", default=DEFAULT_PROJECT_WORKSPACE, help="Host-owned overlay directory, relative to --root unless absolute")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _add_root_workspace_force(parser: argparse.ArgumentParser) -> None:
    _add_root_workspace(parser)
    parser.add_argument("--force", action="store_true", help="Overwrite generated starter files")
    parser.add_argument("--tracker-provider", default="auto", choices=["auto", "none", "hermes_mcp", "gitea"], help="Tracker provider for generated config; auto/gitea both use Hermes MCP")
    parser.add_argument("--gitea-backend", default=None, choices=["mcp", "http"], help=argparse.SUPPRESS)
    parser.add_argument("--gitea-base-url", default="", help=argparse.SUPPRESS)
    parser.add_argument("--gitea-repo", default="", help=argparse.SUPPRESS)
    parser.add_argument("--gitea-token-env", default="", help=argparse.SUPPRESS)


def _add_root_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Target project root")
    parser.add_argument("--config", default=None, help="Project config path")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def removed_command_payload(argv: list[str]) -> dict[str, Any] | None:
    args = _positional_args(argv)
    if not args:
        return None
    if args[0] == "help" and len(args) > 1:
        return _command_removed(" ".join(args[:2]), "/qa-aist help", "Subtopic help was removed; /qa-aist help now lists every command.")
    if args[:2] == ["cases", "generate"]:
        removed_options = {
            "--generated_count": "/qa-aist cases generate --init --count 5",
            "--generated-count": "/qa-aist cases generate --init --count 5",
            "--fast": "/qa-aist cases generate --init",
            "--candidate-json": "/qa-aist cases generate --growing",
            "--from-issues": "/qa-aist cases generate --growing",
            "--from-scratch": "/qa-aist cases generate --init",
            "--issue": "/qa-aist cases generate --growing",
        }
        for option, replacement in removed_options.items():
            if option in argv:
                return _command_removed(f"cases generate {option}", replacement, f"{option} is no longer part of the public command surface.")
    for length in range(min(3, len(args)), 0, -1):
        key = tuple(args[:length])
        replacement = REMOVED_COMMAND_REPLACEMENTS.get(key)
        if replacement:
            return _command_removed(" ".join(key), replacement)
    return None


def _command_removed(command: str, replacement: str, message: str | None = None) -> dict[str, Any]:
    return {
        "status": "error",
        "error": "command_removed",
        "removed_command": command,
        "replacement": replacement,
        "message": message or f"`{command}` was removed from QA-AIST public commands. Use `{replacement}`.",
    }


def _dedupe_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for check in checks:
        key = (
            str(check.get("name") or ""),
            str(check.get("status") or ""),
            str(check.get("path") or check.get("server") or check.get("value") or ""),
            str(check.get("message") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(check)
    return deduped


def _positional_args(argv: list[str]) -> list[str]:
    out: list[str] = []
    skip_next = False
    options_with_values = {
        "--root",
        "--workspace",
        "--config",
        "--issues-json",
        "--plan",
        "--latest-run",
        "--result",
        "--target-state",
        "--expected-contract-hash",
        "--tracker-provider",
        "--gitea-backend",
        "--gitea-base-url",
        "--gitea-repo",
        "--gitea-token-env",
        "--feature",
        "--profile",
        "--count",
        "--generated_count",
        "--generated-count",
        "--candidate-json",
        "--issue",
        "--result-json",
    }
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--json":
            continue
        if arg in options_with_values:
            skip_next = True
            continue
        if arg.startswith("--"):
            continue
        out.append(arg)
    return out


def _remote_duplicate_actions(duplicates: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for group in duplicates.get("duplicates", []):
        ids = [int(item) for item in group.get("issue_ids", []) if item is not None]
        if len(ids) < 2:
            continue
        canonical = min(ids)
        for issue_id in sorted(item for item in ids if item != canonical):
            actions.append(
                {
                    "type": "mark_remote_duplicate",
                    "status": "planned",
                    "issue_id": issue_id,
                    "canonical_issue_id": canonical,
                    "write_gate_required": True,
                    "operation": "close_or_mark_duplicate",
                    "message": f"Mark issue #{issue_id} as duplicate of #{canonical}; do not hard-delete remote issue.",
                }
            )
    return actions


def _issue_id_for_case_push(config: Any, case_id: str | None) -> int | None:
    if case_id:
        contract = load_contract(config.paths.cases / f"{case_id}.yaml")
        return _source_issue_id(contract.raw)
    latest_path = config.paths.state / "latest-run.json"
    if latest_path.exists():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        for result in latest.get("results", []):
            if result.get("status") == "FAIL" and result.get("case_id"):
                try:
                    contract = load_contract(config.paths.cases / f"{result['case_id']}.yaml")
                except ContractError:
                    continue
                issue_id = _source_issue_id(contract.raw)
                if issue_id is not None:
                    return issue_id
    return None


def _source_issue_id(raw: dict[str, Any]) -> int | None:
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    for key in ("issue_id", "gitea_issue_id"):
        if source.get(key) is not None:
            try:
                return int(source[key])
            except (TypeError, ValueError):
                return None
    return None


def _close_loop_components(
    *,
    issue_sync: dict[str, Any],
    duplicates: dict[str, Any],
    case_count: int,
    latest_run: dict[str, Any] | None,
    wiki_sync: dict[str, Any],
    config: Any,
) -> list[dict[str, Any]]:
    latest_results = latest_run.get("results", []) if isinstance(latest_run, dict) else []
    report_exists = (config.paths.reports / "status.md").exists()
    return [
        {
            "name": "Observe",
            "status": "PASS" if issue_sync.get("snapshot_exists") else "WARN",
            "checks": ["issue snapshot", "latest run", "wiki status"],
            "details": {"snapshot_path": issue_sync.get("snapshot_path"), "synced_at": issue_sync.get("synced_at")},
        },
        {
            "name": "Normalize",
            "status": "PASS" if duplicates.get("duplicate_count", 0) == 0 else "WARN",
            "checks": ["duplicate issues", "active mirror pruning"],
            "details": {"duplicate_count": duplicates.get("duplicate_count", 0)},
        },
        {
            "name": "Execute",
            "status": "PASS" if case_count > 0 else "WARN",
            "checks": ["case contracts", "runners", "evidence"],
            "details": {"case_count": case_count, "latest_result_count": len(latest_results)},
        },
        {
            "name": "Triage",
            "status": "PASS" if latest_run else "WARN",
            "checks": ["PASS/FAIL/BLOCK counts", "write gate inputs"],
            "details": {"case_counts": (latest_run or {}).get("case_counts")},
        },
        {
            "name": "Publish",
            "status": "PASS" if wiki_sync.get("status") == "ok" else "WARN",
            "checks": ["wiki plan", "wiki apply", "status report"],
            "details": {"wiki": wiki_sync.get("page"), "report_exists": report_exists},
        },
        {
            "name": "Evolve",
            "status": "PASS" if case_count > 0 else "WARN",
            "checks": ["init/growing/redmine case generation", "case review"],
            "details": {"cases_dir": _relative_or_str(config.paths.cases, config.root)},
        },
        {
            "name": "Prune",
            "status": "PASS" if issue_sync.get("snapshot_exists") else "WARN",
            "checks": ["closed issue removal", "stale active references"],
            "details": {"open_count": issue_sync.get("open_count"), "mirror_count": issue_sync.get("mirror_count")},
        },
    ]


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    removed = removed_command_payload(argv)
    if removed:
        return print_json(removed, exit_code=2)
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
