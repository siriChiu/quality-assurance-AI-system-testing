from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PROJECT_DIR = ".qa-aist"
CONFIG_FILE = ".qa-aist.yaml"

DEFAULT_CONFIG = """# QA-AIST project configuration
# This file belongs to the host project, not to the QA-AIST tool repository.
project:
  name: example-project
  default_branch: main

paths:
  workspace: .qa-aist
  cases: .qa-aist/cases
  runners: .qa-aist/runners
  rules: .qa-aist/rules
  state: .qa-aist/state
  evidence: .qa-aist/evidence
  reports: .qa-aist/reports

tracker:
  provider: none
  project: ""
  api_token_env: QA_AIST_TRACKER_TOKEN

policy:
  deterministic_first: true
  require_write_gate: true
  prohibit_closed_issue_comments: true
  prohibit_raw_secrets_in_repo: true
"""

EXAMPLE_CONTRACT = """case_id: EXAMPLE-001
title: Example deterministic smoke test
owner: qa-team
feature: example
priority: P2
contract_version: 1
commands:
  - id: smoke
    run: python --version
    expected_exit_code: 0
expected: command exits successfully and prints a version string
artifacts:
  - evidence/stdout.log
write_gate:
  tracker_write_allowed: false
  reason: example contract only
"""

EXAMPLE_RUNNER = """#!/usr/bin/env bash
set -euo pipefail
python --version
"""


def project_paths(root: Path) -> dict[str, Path]:
    workspace = root / PROJECT_DIR
    return {
        "root": root,
        "config": root / CONFIG_FILE,
        "workspace": workspace,
        "cases": workspace / "cases",
        "runners": workspace / "runners",
        "rules": workspace / "rules",
        "state": workspace / "state",
        "evidence": workspace / "evidence",
        "reports": workspace / "reports",
    }


def write_if_missing(path: Path, content: str, *, executable: bool = False, force: bool = False) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o111)
    return True


def cmd_init_project(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = project_paths(root)
    for key in ["cases", "runners", "rules", "state", "evidence", "reports"]:
        paths[key].mkdir(parents=True, exist_ok=True)

    created = []
    if write_if_missing(paths["config"], DEFAULT_CONFIG, force=args.force):
        created.append(str(paths["config"]))
    if write_if_missing(paths["cases"] / "example-contract.yaml", EXAMPLE_CONTRACT, force=args.force):
        created.append(str(paths["cases"] / "example-contract.yaml"))
    if write_if_missing(paths["runners"] / "example-runner.sh", EXAMPLE_RUNNER, executable=True, force=args.force):
        created.append(str(paths["runners"] / "example-runner.sh"))

    return print_json({
        "status": "ok",
        "root": str(root),
        "created": created,
        "workspace": str(paths["workspace"]),
    })


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    paths = project_paths(root)
    cases = sorted(paths["cases"].glob("*.yaml")) if paths["cases"].exists() else []
    runners = sorted(paths["runners"].glob("*")) if paths["runners"].exists() else []
    return print_json({
        "tool": "qa-aist",
        "root": str(root),
        "config_exists": paths["config"].exists(),
        "workspace_exists": paths["workspace"].exists(),
        "case_contract_count": len(cases),
        "runner_count": len([p for p in runners if p.is_file()]),
    })


def cmd_config_validate(args: argparse.Namespace) -> int:
    config = Path(args.config).resolve()
    if not config.exists():
        return print_json({"status": "error", "error": "config_not_found", "path": str(config)}, exit_code=2)
    text = config.read_text(encoding="utf-8")
    required_tokens = ["project:", "paths:", "policy:"]
    missing = [token for token in required_tokens if token not in text]
    if missing:
        return print_json({"status": "error", "error": "missing_required_sections", "missing": missing}, exit_code=3)
    return print_json({"status": "ok", "path": str(config)})


def print_json(payload: dict[str, Any], *, exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-aist", description="Reusable deterministic-first QA toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    init_project = sub.add_parser("init-project", help="Create a generic QA-AIST project overlay in a target repo")
    init_project.add_argument("--root", default=".", help="Target project root")
    init_project.add_argument("--force", action="store_true", help="Overwrite generated starter files")
    init_project.set_defaults(func=cmd_init_project)

    status = sub.add_parser("status", help="Show QA-AIST project overlay status")
    status.add_argument("--root", default=".", help="Target project root")
    status.set_defaults(func=cmd_status)

    config = sub.add_parser("config", help="Config commands")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    validate = config_sub.add_parser("validate", help="Validate the basic project config shape")
    validate.add_argument("--config", default=CONFIG_FILE)
    validate.set_defaults(func=cmd_config_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
