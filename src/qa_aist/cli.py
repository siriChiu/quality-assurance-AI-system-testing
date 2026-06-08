from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

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
from .contracts import ContractError, list_contract_paths, load_contract, load_contracts, select_contracts
from .pipeline import PIPELINE_ORDER, run_close_loop
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
    if is_qa_aist_source_checkout(paths.workspace):
        return print_json({
            "status": "error",
            "error": "workspace_is_tool_checkout",
            "workspace": str(paths.workspace),
            "message": "Refusing to write host-project assets into a QA-AIST source checkout. Use --workspace .qa-aist-project or another host-owned overlay path.",
        }, exit_code=4)

    for path in [paths.cases, paths.runners, paths.rules, paths.state, paths.evidence, paths.reports]:
        path.mkdir(parents=True, exist_ok=True)

    created = []
    if write_if_missing(paths.config, default_config(str(workspace)), force=args.force):
        created.append(str(paths.config))
    if write_if_missing(paths.cases / "example-contract.yaml", EXAMPLE_CONTRACT, force=args.force):
        created.append(str(paths.cases / "example-contract.yaml"))
    if write_if_missing(paths.runners / "example-runner.sh", EXAMPLE_RUNNER, executable=True, force=args.force):
        created.append(str(paths.runners / "example-runner.sh"))
    if write_if_missing(paths.rules / "swqa-test-design.md", SWQA_TEST_DESIGN_RULE, force=args.force):
        created.append(str(paths.rules / "swqa-test-design.md"))

    return print_json({
        "status": "ok",
        "root": str(root),
        "created": created,
        "workspace": str(paths.workspace),
        "embedded_tool_checkout_detected": is_qa_aist_source_checkout(root / LEGACY_PROJECT_WORKSPACE),
    })


def cmd_setup(args: argparse.Namespace) -> int:
    return cmd_init_project(args)


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    workspace = args.workspace or DEFAULT_PROJECT_WORKSPACE
    paths = project_paths(root, workspace)
    config_exists = paths.config.exists()
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
        "tool": "qa-aist",
        "root": str(root),
        "config_exists": config_exists,
        "workspace": str(paths.workspace),
        "workspace_exists": paths.workspace.exists(),
        "workspace_is_tool_checkout": is_qa_aist_source_checkout(paths.workspace),
        "embedded_tool_checkout_detected": is_qa_aist_source_checkout(root / LEGACY_PROJECT_WORKSPACE),
        "case_contract_count": len(cases),
        "runner_count": len([p for p in runners if p.is_file()]),
        "latest_run": latest_payload,
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
        status = "PASS" if all(check["status"] == "PASS" for check in checks) else "WARN"
        return print_json({"status": status, "checks": checks})
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

    return parser


def _add_root_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Target project root")
    parser.add_argument("--workspace", default=DEFAULT_PROJECT_WORKSPACE, help="Host-owned overlay directory, relative to --root unless absolute")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _add_root_workspace_force(parser: argparse.ArgumentParser) -> None:
    _add_root_workspace(parser)
    parser.add_argument("--force", action="store_true", help="Overwrite generated starter files")


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
