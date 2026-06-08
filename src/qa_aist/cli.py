from __future__ import annotations

import argparse
import json
import re
import subprocess
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
    generate_cases_from_issues,
    review_generated_cases,
    validate_generated_cases,
)
from .contracts import ContractError, list_contract_paths, load_contract, load_contracts, select_contracts
from .fix_issues import FixIssueError, fix_status, plan_fix_issue, run_fix_issue, submit_fix_pr
from .gitea import GiteaError
from .issues import IssueSyncError, dedupe_issues, issue_status, issue_sync_readiness, show_issue, sync_issues
from .pipeline import PIPELINE_ORDER, run_close_loop
from .publishing import PublishError, apply_publish_plan, plan_publish, publish_status
from .reports import load_latest_results, render_status_report
from .runner import RunContext, run_case
from .templates import EXAMPLE_CONTRACT, EXAMPLE_RUNNER, SWQA_TEST_DESIGN_RULE
from .write_gate import evaluate_write_gate


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

    issue_sync = None
    config_error = None
    if paths.config.exists():
        try:
            config = load_project_config(root)
            issue_sync = issue_sync_readiness(config)
        except QAConfigError as exc:
            config_error = {"error": exc.error, "message": exc.message, **exc.details}

    return print_json({
        "status": "ok",
        "root": str(root),
        "created": created,
        "workspace": str(paths.workspace),
        "tracker_setup": tracker_setup["payload"],
        "issue_sync": issue_sync,
        "config_error": config_error,
        "embedded_tool_checkout_detected": is_qa_aist_source_checkout(root / LEGACY_PROJECT_WORKSPACE),
    })


def cmd_setup(args: argparse.Namespace) -> int:
    return cmd_init_project(args)


def resolve_setup_tracker(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    detected = detect_gitea_remote(root)
    provider = str(getattr(args, "tracker_provider", "auto") or "auto")
    if provider == "auto":
        provider = "gitea" if detected else "none"

    backend_arg = getattr(args, "gitea_backend", None)
    backend = str(backend_arg or ("mcp" if provider == "gitea" else "http"))
    base_url = str(getattr(args, "gitea_base_url", "") or (detected or {}).get("base_url", ""))
    repo = str(getattr(args, "gitea_repo", "") or (detected or {}).get("repo", ""))
    token_env = str(getattr(args, "gitea_token_env", "") or "QA_AIST_GITEA_TOKEN")
    project_name = repo.rsplit("/", 1)[-1] if repo else root.name
    default_branch = detect_default_branch(root) or "main"

    if provider != "gitea":
        backend = "http"
        base_url = ""
        repo = ""

    return {
        "config_kwargs": {
            "project_name": project_name or "example-project",
            "default_branch": default_branch,
            "tracker_provider": provider,
            "gitea_backend": backend,
            "gitea_base_url": base_url,
            "gitea_repo": repo,
            "gitea_token_env": token_env,
        },
        "payload": {
            "provider": provider,
            "gitea_backend": backend,
            "gitea_base_url": base_url,
            "gitea_repo": repo,
            "gitea_token_env": token_env,
            "git_remote_detected": bool(detected),
            "git_remote_url": (detected or {}).get("remote_url"),
            "auto_configured_mcp": provider == "gitea" and backend == "mcp" and bool(detected),
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
    config_error = None
    if config_exists:
        try:
            config = load_project_config(root)
            issue_sync = issue_sync_readiness(config)
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
        tracker = config.data.get("tracker", {})
        token_env = tracker.get("api_token_env") if isinstance(tracker, dict) else None
        if token_env:
            checks.append({"name": "tracker.api_token_env", "status": "PASS", "env": token_env, "value_printed": False})
        readiness = issue_sync_readiness(config)
        checks.extend(readiness.get("checks", []))
        statuses = {str(check.get("status")) for check in checks}
        status = "FAIL" if "FAIL" in statuses else ("WARN" if "WARN" in statuses else "PASS")
        return print_json({"status": status, "checks": checks, "issue_sync": readiness})
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
        payload = sync_issues(config, issues_json=args.issues_json, dry_run=args.dry_run)
    except (QAConfigError, IssueSyncError, GiteaError) as exc:
        return _error_payload(exc)
    return print_json(payload)


def cmd_issues_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        payload = issue_status(config)
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


def cmd_cases_generate(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
        if not args.from_issues:
            return print_json({"status": "error", "error": "generation_source_required", "message": "Use --from-issues for V1."}, exit_code=2)
        payload = generate_cases_from_issues(config, issue_id=args.issue, force=args.force)
    except (QAConfigError, CaseGenerationError, IssueSyncError) as exc:
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
    return print_json({"status": status, "results": results}, exit_code=1 if status == "FAIL" else 0)


def cmd_close_loop_status(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
    except QAConfigError as exc:
        return _error_payload(exc)
    latest = config.paths.state / "latest-run.json"
    payload = json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else None
    return print_json({"status": "ok", "pipeline_order": PIPELINE_ORDER, "latest_run": payload})


def cmd_close_loop_run_once(args: argparse.Namespace) -> int:
    try:
        config = load_project_config(Path(args.root), args.config)
    except QAConfigError as exc:
        return _error_payload(exc)
    result = run_close_loop(config, case_id=args.case_id, dry_run=args.dry_run)
    return print_json(result.payload, exit_code=0 if result.status in {"PASS", "FAIL"} else 2)


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
    if isinstance(exc, (IssueSyncError, GiteaError, CaseGenerationError, PublishError, FixIssueError)):
        return print_json({"status": "error", "error": type(exc).__name__, "message": str(exc)}, exit_code=2)
    return print_json({"status": "error", "error": type(exc).__name__, "message": str(exc)}, exit_code=1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-aist", description="Reusable deterministic-first QA toolkit")
    parser.add_argument("--json", action="store_true", help="Emit JSON output; accepted for stable Hermes scripts")
    sub = parser.add_subparsers(dest="command", required=True)

    init_project = sub.add_parser("init-project", help="Create a generic QA-AIST project overlay in a target repo")
    _add_root_workspace_force(init_project)
    init_project.set_defaults(func=cmd_init_project)

    setup = sub.add_parser("setup", help="Alias for init-project; safe for Hermes bootstrap flows")
    _add_root_workspace_force(setup)
    setup.set_defaults(func=cmd_setup)

    status = sub.add_parser("status", help="Show QA-AIST project overlay status")
    _add_root_workspace(status)
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="Check install, config, paths, and secret references")
    _add_root_config(doctor)
    doctor.set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="Config commands")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    validate = config_sub.add_parser("validate", help="Validate the project config shape")
    validate.add_argument("--config", default=CONFIG_FILE)
    validate.add_argument("--json", action="store_true", help="Emit JSON output")
    validate.set_defaults(func=cmd_config_validate)
    show = config_sub.add_parser("show", help="Show parsed project config")
    _add_root_config(show)
    show.set_defaults(func=cmd_config_show)

    qa_test = sub.add_parser("qa-test", help="Case contract commands")
    qa_sub = qa_test.add_subparsers(dest="qa_command", required=True)
    for name, func, help_text in [
        ("list", cmd_qa_test_list, "List case contracts"),
        ("validate", cmd_qa_test_validate, "Validate case contracts"),
        ("dry-run", cmd_qa_test_dry_run, "Render the commands that would run"),
        ("run", cmd_qa_test_run, "Run selected or all case contracts"),
    ]:
        command = qa_sub.add_parser(name, help=help_text)
        _add_root_config(command)
        command.add_argument("--case-id", default=None)
        command.set_defaults(func=func)
    run_one = qa_sub.add_parser("run-one", help="Run one case contract")
    _add_root_config(run_one)
    run_one.add_argument("case_id")
    run_one.set_defaults(func=cmd_qa_test_run_one)

    issues = sub.add_parser("issues", help="Gitea issue mirror and dedupe commands")
    issues_sub = issues.add_subparsers(dest="issues_command", required=True)
    issues_sync = issues_sub.add_parser("sync", help="Sync local issue mirrors from Gitea or an issues JSON file")
    _add_root_config(issues_sync)
    issues_sync.add_argument("--issues-json", default=None, help="Offline JSON input for tests or manual sync")
    issues_sync.add_argument("--dry-run", action="store_true", help="Preview mirror/snapshot changes without writing")
    issues_sync.set_defaults(func=cmd_issues_sync)
    issues_status = issues_sub.add_parser("status", help="Show local issue sync state")
    _add_root_config(issues_status)
    issues_status.set_defaults(func=cmd_issues_status)
    issues_show = issues_sub.add_parser("show", help="Show one local issue mirror")
    _add_root_config(issues_show)
    issues_show.add_argument("issue_id", type=int)
    issues_show.set_defaults(func=cmd_issues_show)
    issues_dedupe = issues_sub.add_parser("dedupe", help="Detect duplicate active issue mirrors")
    _add_root_config(issues_dedupe)
    issues_dedupe.set_defaults(func=cmd_issues_dedupe)

    cases = sub.add_parser("cases", help="Generate, review, and validate case contracts")
    cases_sub = cases.add_subparsers(dest="cases_command", required=True)
    cases_generate = cases_sub.add_parser("generate", help="Generate draft case contracts from synced issues")
    _add_root_config(cases_generate)
    cases_generate.add_argument("--from-issues", action="store_true", help="Generate from local issue mirrors/snapshot")
    cases_generate.add_argument("--issue", type=int, default=None, help="Generate only one issue")
    cases_generate.add_argument("--force", action="store_true", help="Overwrite existing generated case YAML")
    cases_generate.set_defaults(func=cmd_cases_generate)
    cases_review = cases_sub.add_parser("review", help="List generated drafts and Q&A prompts")
    _add_root_config(cases_review)
    cases_review.set_defaults(func=cmd_cases_review)
    cases_validate = cases_sub.add_parser("validate", help="Validate generated case contracts")
    _add_root_config(cases_validate)
    cases_validate.set_defaults(func=cmd_cases_validate)

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
    publish_plan = publish_sub.add_parser("plan", help="Create a gated publish plan from latest run")
    _add_root_config(publish_plan)
    publish_plan.add_argument("--latest-run", default=None)
    publish_plan.set_defaults(func=cmd_publish_plan)
    publish_apply = publish_sub.add_parser("apply", help="Apply a publish plan to Gitea after write gate passes")
    _add_root_config(publish_apply)
    publish_apply.add_argument("--plan", default=None)
    publish_apply.set_defaults(func=cmd_publish_apply)
    publish_status_cmd = publish_sub.add_parser("status", help="Show publish plan/apply status")
    _add_root_config(publish_status_cmd)
    publish_status_cmd.set_defaults(func=cmd_publish_status)

    fix_issues = sub.add_parser("fix-issues", help="Plan, hand off, and submit PRs for synced Gitea issues")
    fix_sub = fix_issues.add_subparsers(dest="fix_command", required=True)
    fix_plan = fix_sub.add_parser("plan", help="Preflight one issue before repair")
    _add_root_config(fix_plan)
    fix_plan.add_argument("--issue", type=int, required=True)
    fix_plan.set_defaults(func=cmd_fix_issues_plan)
    fix_run = fix_sub.add_parser("run", help="Create a Hermes handoff for minimal repair")
    _add_root_config(fix_run)
    fix_run.add_argument("--issue", type=int, required=True)
    fix_run.set_defaults(func=cmd_fix_issues_run)
    fix_pr = fix_sub.add_parser("submit-pr", help="Push planned branch and create a Gitea pull request")
    _add_root_config(fix_pr)
    fix_pr.add_argument("--issue", type=int, required=True)
    fix_pr.add_argument("--dry-run", action="store_true", help="Render PR payload without pushing or calling Gitea")
    fix_pr.set_defaults(func=cmd_fix_issues_submit_pr)
    fix_status_cmd = fix_sub.add_parser("status", help="Show latest fix plan/handoff/PR result")
    _add_root_config(fix_status_cmd)
    fix_status_cmd.set_defaults(func=cmd_fix_issues_status)

    sync_gitea = sub.add_parser("sync-gitea", help="Legacy alias for issues sync/status")
    sync_sub = sync_gitea.add_subparsers(dest="sync_gitea_command", required=True)
    sync_pull = sync_sub.add_parser("pull", help="Alias for issues sync")
    _add_root_config(sync_pull)
    sync_pull.add_argument("--issues-json", default=None)
    sync_pull.add_argument("--dry-run", action="store_true")
    sync_pull.set_defaults(func=cmd_sync_gitea_pull)
    for name, func in [("status", cmd_sync_gitea_status), ("validate", cmd_sync_gitea_validate)]:
        command = sync_sub.add_parser(name, help=f"Alias for issues {name}")
        _add_root_config(command)
        command.set_defaults(func=func)

    find_new = sub.add_parser("find-new-issues", help="Legacy alias for publish plan")
    find_sub = find_new.add_subparsers(dest="find_command", required=True)
    for name in ["run", "dry-run"]:
        command = find_sub.add_parser(name, help="Alias for publish plan")
        _add_root_config(command)
        command.add_argument("--latest-run", default=None)
        command.set_defaults(func=cmd_find_new_issues_run)

    return parser


def _add_root_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Target project root")
    parser.add_argument("--workspace", default=DEFAULT_PROJECT_WORKSPACE, help="Host-owned overlay directory, relative to --root unless absolute")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _add_root_workspace_force(parser: argparse.ArgumentParser) -> None:
    _add_root_workspace(parser)
    parser.add_argument("--force", action="store_true", help="Overwrite generated starter files")
    parser.add_argument("--tracker-provider", default="auto", choices=["auto", "none", "gitea"], help="Tracker provider for generated config; auto uses git remote when available")
    parser.add_argument("--gitea-backend", default=None, choices=["mcp", "http"], help="Gitea backend for generated config; defaults to mcp when a git remote is detected")
    parser.add_argument("--gitea-base-url", default="", help="Gitea base URL override for generated config")
    parser.add_argument("--gitea-repo", default="", help="Gitea owner/repo override for generated config")
    parser.add_argument("--gitea-token-env", default="QA_AIST_GITEA_TOKEN", help="Environment variable name for HTTP Gitea token")


def _add_root_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Target project root")
    parser.add_argument("--config", default=None, help="Project config path")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
