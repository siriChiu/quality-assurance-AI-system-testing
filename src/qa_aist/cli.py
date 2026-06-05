from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_PROJECT_WORKSPACE = ".qa-aist-project"
LEGACY_PROJECT_WORKSPACE = ".qa-aist"
CONFIG_FILE = ".qa-aist.yaml"


def default_config(workspace: str = DEFAULT_PROJECT_WORKSPACE) -> str:
    return f"""# QA-AIST project configuration
# This file belongs to the host project, not to the QA-AIST tool repository.
project:
  name: example-project
  default_branch: main

paths:
  workspace: {workspace}
  cases: {workspace}/cases
  runners: {workspace}/runners
  rules: {workspace}/rules
  state: {workspace}/state
  evidence: {workspace}/evidence
  reports: {workspace}/reports

tracker:
  provider: none
  project: ""
  api_token_env: QA_AIST_TRACKER_TOKEN

policy:
  deterministic_first: true
  require_write_gate: true
  prohibit_closed_issue_comments: true
  prohibit_raw_secrets_in_repo: true
  require_swqa_pattern_expansion: true
  require_sibling_surface_scan: true
  require_boundary_invalid_tests: true
  require_side_effect_safe_repro: true
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

SWQA_TEST_DESIGN_RULE = """# SWQA test-design rule

QA-AIST treats each confirmed bug as a reusable failure pattern, not as a
single reproduction command. A fix is not complete until deterministic tests
prove the original failure, adjacent inputs, invalid inputs, and safe no-op
smoke paths.

## Required expansion for every new bug

```yaml
bug_pattern:
  exact_reproduction:
    required: true
    evidence: failing automated test or side-effect-safe CLI repro
  sibling_surface_scan:
    required: true
    question: which commands/features share the same parser, validator, state, or transport path?
  negative_cases:
    required: true
    include: invalid values, missing values, duplicate/conflicting flags, and documented disable modes
  boundary_values:
    required: true
    include: zero, negative, minimum positive, default, maximum/huge values, empty strings, and values that look like flags
  side_effect_safe_smoke:
    required: true
    examples: --help, --version, dry-run, parser-only fixtures, or explicit no-op fakes
```

## CLI argument-order matrix

For any CLI parser or command contract change, cover these dimensions before
marking PASS:

```yaml
cli_argument_order_matrix:
  flag_scope:
    - app_or_global_flag
    - command_local_flag
    - same_name_global_and_local_flag
  position:
    - before_command
    - after_command_before_options
    - after_command_options
    - after_positional_argument
    - inline_equals_form
    - short_alias_form
    - after_double_dash_separator_must_not_be_rewritten
  value_shape:
    - normal_value
    - empty_value
    - value_beginning_with_dash
    - path_value
    - url_value
    - duration_or_number_boundary
  assertions:
    - contextual_help_stays_contextual
    - local_flags_are_not_mistaken_for_global_flags
    - global_flags_do_not_steal_command_flag_values
    - positional_arguments_do_not_hide_later_local_flags_unless_the_contract_rejects_that_shape
```

## Boundary and invalid-value tests

Do not assume parser acceptance means semantic validity.

```yaml
validation_matrix:
  durations:
    valid: [minimum_positive, default]
    invalid_when_retry_enabled: [zero, negative]
    note: use an explicit disable flag or max-attempts=0; do not let 0s become busy retry
  retry_or_count_flags:
    valid: [0_if_documented_disable, 1, default]
    invalid: [negative]
  booleans:
    valid: [present, absent, explicit_true_false_when_supported]
    invalid: [ambiguous_or_conflicting_forms]
```

## PASS gate

A SWQA PASS for a bug fix requires:

1. the exact old failure is reproduced first;
2. the fix is verified through the real user-facing interface;
3. sibling commands/features sharing the same pattern are checked;
4. boundary and invalid-value cases are explicitly listed;
5. evidence is real and safe to share; and
6. any remaining untested risk is reported as HOLD, not hidden as PASS.
"""


def workspace_path(root: Path, workspace: str | Path = DEFAULT_PROJECT_WORKSPACE) -> Path:
    requested = Path(workspace)
    if requested.is_absolute():
        return requested.resolve()
    return (root / requested).resolve()


def project_paths(root: Path, workspace: str | Path = DEFAULT_PROJECT_WORKSPACE) -> dict[str, Path]:
    resolved_workspace = workspace_path(root, workspace)
    return {
        "root": root,
        "config": root / CONFIG_FILE,
        "workspace": resolved_workspace,
        "cases": resolved_workspace / "cases",
        "runners": resolved_workspace / "runners",
        "rules": resolved_workspace / "rules",
        "state": resolved_workspace / "state",
        "evidence": resolved_workspace / "evidence",
        "reports": resolved_workspace / "reports",
    }


def is_qa_aist_source_checkout(path: Path) -> bool:
    if not path.is_dir():
        return False
    package_dir = path / "src" / "qa_aist"
    pyproject = path / "pyproject.toml"
    if package_dir.is_dir() and pyproject.exists():
        try:
            if 'name = "qa-aist"' in pyproject.read_text(encoding="utf-8"):
                return True
        except OSError:
            return False
    return (package_dir / "cli.py").exists() and (path / "docs" / "PROJECT_BOUNDARY.md").exists()


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
    workspace = args.workspace or DEFAULT_PROJECT_WORKSPACE
    paths = project_paths(root, workspace)
    if is_qa_aist_source_checkout(paths["workspace"]):
        return print_json({
            "status": "error",
            "error": "workspace_is_tool_checkout",
            "workspace": str(paths["workspace"]),
            "message": "Refusing to write host-project assets into a QA-AIST source checkout. Use --workspace .qa-aist-project or another host-owned overlay path.",
        }, exit_code=4)

    for key in ["cases", "runners", "rules", "state", "evidence", "reports"]:
        paths[key].mkdir(parents=True, exist_ok=True)

    created = []
    if write_if_missing(paths["config"], default_config(str(workspace)), force=args.force):
        created.append(str(paths["config"]))
    if write_if_missing(paths["cases"] / "example-contract.yaml", EXAMPLE_CONTRACT, force=args.force):
        created.append(str(paths["cases"] / "example-contract.yaml"))
    if write_if_missing(paths["runners"] / "example-runner.sh", EXAMPLE_RUNNER, executable=True, force=args.force):
        created.append(str(paths["runners"] / "example-runner.sh"))
    if write_if_missing(paths["rules"] / "swqa-test-design.md", SWQA_TEST_DESIGN_RULE, force=args.force):
        created.append(str(paths["rules"] / "swqa-test-design.md"))

    return print_json({
        "status": "ok",
        "root": str(root),
        "created": created,
        "workspace": str(paths["workspace"]),
        "embedded_tool_checkout_detected": is_qa_aist_source_checkout(root / LEGACY_PROJECT_WORKSPACE),
    })


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    workspace = args.workspace or DEFAULT_PROJECT_WORKSPACE
    paths = project_paths(root, workspace)
    cases = sorted(paths["cases"].glob("*.yaml")) if paths["cases"].exists() else []
    runners = sorted(paths["runners"].glob("*")) if paths["runners"].exists() else []
    return print_json({
        "tool": "qa-aist",
        "root": str(root),
        "config_exists": paths["config"].exists(),
        "workspace": str(paths["workspace"]),
        "workspace_exists": paths["workspace"].exists(),
        "workspace_is_tool_checkout": is_qa_aist_source_checkout(paths["workspace"]),
        "embedded_tool_checkout_detected": is_qa_aist_source_checkout(root / LEGACY_PROJECT_WORKSPACE),
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
    init_project.add_argument("--workspace", default=DEFAULT_PROJECT_WORKSPACE, help="Host-owned overlay directory, relative to --root unless absolute")
    init_project.add_argument("--force", action="store_true", help="Overwrite generated starter files")
    init_project.set_defaults(func=cmd_init_project)

    status = sub.add_parser("status", help="Show QA-AIST project overlay status")
    status.add_argument("--root", default=".", help="Target project root")
    status.add_argument("--workspace", default=DEFAULT_PROJECT_WORKSPACE, help="Host-owned overlay directory, relative to --root unless absolute")
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
