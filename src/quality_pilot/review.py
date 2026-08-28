"""Deterministic local review workflow for any readable Gitea Pull Request.

The first implementation is snapshot/handoff based: Hermes supplies a PR
snapshot, this module pins the head in a detached worktree, selects local
regression tests, writes a redacted report, and emits a gated Gitea review
request only after explicit confirmation.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .case_generation import CaseGenerationError, generate_cases_init
from .config import ProjectConfig, json_dumps, load_project_config, project_paths
from .contracts import load_contract, load_contracts
from .environment import environment_profile_status, remote_preflight
from .execution_contract import apply_discovered_contract, normalize_execution_contract
from .hermes_mcp import configured_mcp_json_path, mcp_server_is_available
from .product_case_adapter import build_product_case_contract
from .product_testing import run_product_tests
from .runner import RunContext, run_case, stamp_result_run_id, utc_now
from .security import ensure_safe_structure, find_secret_text, redact_structure

REVIEW_SCHEMA = "quality-pilot.code-review.v1"
REVIEW_REQUEST_SCHEMA = "quality-pilot.gitea-mcp-review-write-request.v1"
DIFF_TARGETED_ORACLE_SCHEMA = "quality-pilot.diff-targeted-oracle.v1"


class ReviewError(RuntimeError):
    pass


def review_gate(report: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Return the fail-closed gate that callers must honor before proceeding."""
    if dry_run:
        return {
            "status": "PREVIEW",
            "reason": "dry_run",
            "execution_allowed": False,
            "merge_allowed": False,
            "human_decision_required": True,
        }
    conclusion = str(report.get("conclusion") or "")
    blocked = str(report.get("status") or "") != "ok" or conclusion != "NO_BLOCKING_FINDINGS"
    return {
        "status": "BLOCKED" if blocked else "HUMAN_GATE_REQUIRED",
        "reason": conclusion or "review_status_unavailable",
        "execution_allowed": not blocked,
        "merge_allowed": False,
        "human_decision_required": True,
    }


def review_pr(
    config: ProjectConfig,
    *,
    repo: str,
    pr_number: int,
    pr_json: str | Path | None = None,
    checkout: str | Path | None = None,
    confirm: bool = False,
    dry_run: bool = False,
    timeout_seconds: int = 120,
    test_timeout_seconds: int | None = None,
    comprehensive: bool = True,
    prepare_dependencies: bool = True,
    confirm_discovery: bool = False,
) -> dict[str, Any]:
    snapshot, snapshot_path = load_pr_snapshot(config, repo=repo, pr_number=pr_number, pr_json=pr_json)
    effective_contract = normalize_execution_contract(config, snapshot=snapshot)
    if comprehensive and not dry_run and effective_contract.get("status") in {"CONFIGURATION_REQUIRED", "CONFIRMATION_REQUIRED"}:
        if not confirm_discovery:
            return {
                "status": "configuration_required",
                "reason": "product_execution_contract_confirmation_required",
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": snapshot.get("head_sha"),
                "effective_execution_contract": effective_contract,
                "next_action": "Confirm the discovered contract, then rerun review with --confirm-discovery",
                "remote_apply": False,
            }
        discovery_apply = apply_discovered_contract(config, confirm=True, expected_head_sha=str(snapshot.get("head_sha") or ""))
        if discovery_apply.get("status") != "ok":
            return {
                "status": "configuration_required",
                "reason": discovery_apply.get("reason") or "product_execution_contract_confirmation_failed",
                "effective_execution_contract": discovery_apply.get("effective_contract", effective_contract),
                "remote_apply": False,
            }
        config = load_project_config(config.root, config.path)
        effective_contract = discovery_apply.get("effective_contract") or normalize_execution_contract(config, snapshot=snapshot)
    if not dry_run and comprehensive and str(effective_contract.get("execution", {}).get("product_target") or "local") == "remote_ssh":
        remote_preflight(config, expected_head_sha=str(snapshot.get("head_sha") or ""))
        config = load_project_config(config.root, config.path)
        effective_contract = normalize_execution_contract(config, snapshot=snapshot)
    worktree = _prepare_detached_worktree(
        config,
        snapshot,
        checkout=checkout,
        dry_run=dry_run,
    )
    diff_info = _reconstruct_snapshot_diff(snapshot, worktree)
    head_sha = snapshot["head_sha"]
    diff_hash = _sha256(str(snapshot.get("diff") or ""))
    review_worktree_path = Path(str(worktree.get("path"))) if worktree.get("path") else None
    review_python_command = _review_python_executable(config, review_worktree_path)
    test_timeout_base = max(1, int(test_timeout_seconds or 900))
    dependency_preparation = prepare_review_dependencies(
        config,
        worktree=review_worktree_path,
        python_executable=review_python_command,
        environment_profile=environment_profile_status(config),
        enabled=prepare_dependencies and not dry_run,
        timeout_seconds=timeout_seconds,
        execution_contract=effective_contract,
    )
    test_selection = select_applicable_tests(
        worktree.get("path"),
        snapshot.get("changed_files", []),
        python_executable=review_python_command,
    )
    test_results = run_selected_tests(
        test_selection["selected"],
        worktree.get("path") if worktree.get("status") in {"ready", "planned"} else None,
        config,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        timeout_seconds=test_timeout_base,
        dry_run=dry_run,
    )
    findings = analyze_diff(snapshot)
    qa_report = (
        _run_comprehensive_review_qa(
            config,
            snapshot=snapshot,
            worktree=worktree,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            timeout_seconds=test_timeout_base,
            dry_run=dry_run,
            test_selection=test_selection,
            test_results=test_results,
            python_executable=review_python_command,
        )
        if comprehensive
        else _diff_only_qa_report()
    )
    report = _build_report(
        config,
        repo=repo,
        pr_number=pr_number,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        diff_hash=diff_hash,
        diff_info=diff_info,
        worktree=worktree,
        test_selection=test_selection,
        test_results=test_results,
        findings=findings,
        qa_report=qa_report,
        dependency_preparation=dependency_preparation,
        effective_execution_contract=effective_contract,
        dry_run=dry_run,
    )
    report["review_gate"] = review_gate(report, dry_run=dry_run)
    report_paths = write_review_report(config, report)
    report["report_paths"] = {
        key: value for key, value in report_paths.items() if key != "json_path_obj"
    }
    safe_report, redaction_findings = redact_structure(report, prefix="review_report")
    if not isinstance(safe_report, dict):
        raise ReviewError("review_report_redaction_failed_closed")
    if redaction_findings:
        report["redaction_findings"] = [item.as_dict() for item in redaction_findings]
    report_hash = _sha256(json_dumps(safe_report))
    report["report_hash"] = report_hash

    remote = prepare_gitea_review_reply(
        config,
        report,
        report_hash=report_hash,
        confirm=confirm,
        dry_run=dry_run,
    )
    report["remote_reply"] = remote
    safe_report["report_hash"] = report_hash
    safe_report["remote_reply"], _ = redact_structure(remote, prefix="review_report.remote_reply")
    safe_report["report_paths"] = report["report_paths"]
    report_paths["json_path_obj"].write_text(json_dumps(safe_report) + "\n", encoding="utf-8")
    markdown_path = config.root / str(report_paths["markdown_path"])
    markdown_path.write_text(_render_markdown(safe_report), encoding="utf-8")
    return report


def load_pr_snapshot(
    config: ProjectConfig,
    *,
    repo: str,
    pr_number: int,
    pr_json: str | Path | None = None,
) -> tuple[dict[str, Any], str]:
    path = Path(pr_json).expanduser() if pr_json else pr_snapshot_path(config, repo, pr_number)
    if not path.is_absolute():
        path = config.root / path
    path = path.resolve()
    if not path.exists():
        raise ReviewError(
            f"gitea_pr_snapshot_missing: provide Hermes Gitea PR read JSON at {_relative_or_str(path, config.root)} "
            "or pass --pr-json"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"gitea_pr_snapshot_invalid: {path}") from exc
    if not isinstance(raw, dict):
        raise ReviewError("gitea_pr_snapshot_invalid: PR snapshot must be an object")
    snapshot = _normalize_snapshot(raw, repo=repo, pr_number=pr_number)
    return snapshot, _relative_or_str(path, config.root)


def pr_snapshot_path(config: ProjectConfig, repo: str, pr_number: int) -> Path:
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(repo).strip()).strip("_") or "repo"
    return config.paths.state / "gitea-mcp" / "pull-requests" / f"{safe_repo}-pr-{int(pr_number)}.json"


def select_applicable_tests(
    worktree: str | None,
    changed_files: list[dict[str, Any]],
    *,
    python_executable: str = "python3",
) -> dict[str, Any]:
    if not worktree:
        return {
            "selected": [],
            "skipped": [],
            "unavailable": [{"reason": "detached_worktree_unavailable"}],
            "signals": [],
            "coverage_gap": True,
        }
    root = Path(worktree)
    changed_paths = [str(item.get("path") or "") for item in changed_files if isinstance(item, dict)]
    signals: list[str] = []
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    tests_dir = root / "tests"
    targeted_oracle: dict[str, Any] = {
        "schema": DIFF_TARGETED_ORACLE_SCHEMA,
        "kind": "product_test_suite",
        "status": "HOLD",
        "reason": "diff_targeted_test_oracle_not_found",
        "changed_files": changed_paths,
        "test_files": [],
        "test_id": None,
    }
    if tests_dir.is_dir():
        signals.append("tests_directory")
        pytest_detected = _tests_use_pytest(tests_dir)
        targeted_files = _diff_targeted_test_files(tests_dir, changed_paths)
        targeted_oracle["test_files"] = targeted_files
        if pytest_detected:
            if targeted_files:
                targeted_command = " ".join(
                    [
                        shlex.quote(python_executable),
                        "-m",
                        "pytest",
                        *(shlex.quote(str(Path("tests") / path)) for path in targeted_files),
                        "-q",
                    ]
                )
                targeted_oracle.update(
                    {
                        "status": "READY",
                        "reason": "changed_files_mapped_to_product_test_oracle",
                        "test_id": "diff-targeted-pytest",
                    }
                )
                selected.append(
                    {
                        "id": "diff-targeted-pytest",
                        "command": targeted_command,
                        "reason": "changed-file-driven product test oracle",
                        "timeout_seconds": 600,
                        "oracle": targeted_oracle,
                    }
                )
                signals.append("diff_targeted_product_tests")
            else:
                targeted_oracle["reason"] = "diff_targeted_test_oracle_not_found"
            command = f"{shlex.quote(python_executable)} -m pytest tests -q"
            test_id = "regression-pytest"
            signals.append("pytest_tests")
        else:
            targeted_oracle["reason"] = "targeted_test_runner_not_detected"
            command = f"{shlex.quote(python_executable)} -m unittest discover -s tests"
            test_id = "regression-unittest"
        selected.append({"id": test_id, "command": command, "reason": "repository regression suite", "timeout_seconds": 900})
        matching = _matching_test_files(tests_dir, changed_paths)
        signals.extend(f"changed_test:{path}" for path in matching)
    if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists() and _contains_pytest_config(root / "pyproject.toml"):
        signals.append("pytest_metadata")
    for path in changed_paths:
        if path:
            signals.append(f"changed_file:{path}")
    if not selected:
        skipped.append({"reason": "no_repo_regression_command_detected"})
    return {
        "selected": selected,
        "skipped": skipped,
        "unavailable": [],
        "signals": sorted(set(signals)),
        "coverage_gap": not bool(selected),
        "targeted_oracle": targeted_oracle,
    }


def prepare_review_dependencies(
    config: ProjectConfig,
    *,
    worktree: Path | None,
    python_executable: str,
    environment_profile: dict[str, Any],
    enabled: bool,
    timeout_seconds: int,
    execution_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare dependencies inside the disposable review worktree only.

    The host interpreter is used solely for ``python -m venv``.  All pip,
    Playwright, and pytest operations run through ``<worktree>/.venv/bin/python``
    so Debian's PEP 668 policy cannot block review preparation and the product
    repository's own environment is never mutated.
    """
    if not enabled:
        return {"status": "NOT_RUN", "reason": "disabled_or_dry_run"}
    local_pytest = bool((execution_contract or {}).get("execution", {}).get("local_pytest", True))
    if not worktree or (not environment_profile.get("ready") and not local_pytest):
        return {"status": "BLOCK", "reason": "local_environment_not_confirmed"}
    if not local_pytest:
        return {"status": "NOT_RUN", "reason": "local_pytest_disabled"}

    venv_dir = worktree / ".venv"
    venv_path = venv_dir / "bin" / "python"
    python_command = ".venv/bin/python"
    bootstrap = sys.executable if Path(sys.executable).is_file() else "python3"
    evidence_dir = config.paths.evidence / "review-dependency-preparation"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    def run_dependency_command(label: str, argv: list[str]) -> dict[str, Any]:
        index = len(results)
        try:
            completed = subprocess.run(
                argv,
                cwd=worktree,
                shell=False,
                text=True,
                capture_output=True,
                timeout=max(1, timeout_seconds),
                check=False,
            )
            stdout, _ = _redact_output(completed.stdout or "")
            stderr, _ = _redact_output(completed.stderr or "")
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, _ = _redact_output(exc.stdout if isinstance(exc.stdout, str) else "")
            stderr, _ = _redact_output(exc.stderr if isinstance(exc.stderr, str) else "")
            exit_code = 124
        except OSError as exc:
            stdout = ""
            stderr, _ = _redact_output(str(exc))
            exit_code = 127
        stdout_path = evidence_dir / f"install-{index}.stdout.log"
        stderr_path = evidence_dir / f"install-{index}.stderr.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        result = {
            "id": label,
            "command": argv,
            "status": "PASS" if exit_code == 0 else "BLOCK",
            "exit_code": exit_code,
            "stdout": _relative_or_str(stdout_path, config.root),
            "stderr": _relative_or_str(stderr_path, config.root),
            "execution_target": "local_disposable_review_worktree",
            "evidence_origin": "local",
        }
        results.append(result)
        return result

    if not venv_path.is_file():
        created = run_dependency_command("create-review-venv", [bootstrap, "-m", "venv", ".venv"])
        if created["status"] != "PASS" or not venv_path.is_file():
            return {
                "status": "BLOCK",
                "reason": "review_venv_creation_failed",
                "python": python_command,
                "bootstrap_python": bootstrap,
                "venv": ".venv",
                "results": results,
                "execution_target": "local_disposable_review_worktree",
                "evidence_origin": "local",
            }

    requirements_path = worktree / "requirements.txt"
    if not requirements_path.is_file():
        fallback_requirements = config.root / "requirements.txt"
        requirements_path = fallback_requirements if fallback_requirements.is_file() else requirements_path
    if requirements_path.is_file():
        requirements_arg = str(requirements_path.relative_to(worktree)) if requirements_path.is_relative_to(worktree) else str(requirements_path)
        dependency = run_dependency_command(
            "install-requirements",
            [python_command, "-m", "pip", "install", "-r", requirements_arg],
        )
        if dependency["status"] != "PASS":
            return {
                "status": "BLOCK",
                "reason": "dependency_install_failed",
                "python": python_command,
                "venv": ".venv",
                "requirements_source": requirements_arg,
                "results": results,
                "execution_target": "local_disposable_review_worktree",
                "evidence_origin": "local",
            }
    elif (worktree / "pyproject.toml").is_file():
        dependency = run_dependency_command(
            "install-project",
            [python_command, "-m", "pip", "install", "-e", "."],
        )
        if dependency["status"] != "PASS":
            return {
                "status": "BLOCK",
                "reason": "dependency_install_failed",
                "python": python_command,
                "venv": ".venv",
                "requirements_source": "pyproject.toml",
                "results": results,
                "execution_target": "local_disposable_review_worktree",
                "evidence_origin": "local",
            }

    browser_install = run_dependency_command(
        "install-playwright-chromium",
        [python_command, "-m", "playwright", "install", "chromium"],
    )
    if browser_install["status"] != "PASS":
        return {
            "status": "BLOCK",
            "reason": "dependency_install_failed",
            "python": python_command,
            "venv": ".venv",
            "requirements_source": str(requirements_path) if requirements_path.is_file() else None,
            "results": results,
            "execution_target": "local_disposable_review_worktree",
            "evidence_origin": "local",
        }
    return {
        "status": "PASS",
        "reason": "local_declared_dependencies_prepared",
        "python": python_command,
        "venv": ".venv",
        "requirements_source": str(requirements_path) if requirements_path.is_file() else None,
        "results": results,
        "execution_target": "local_disposable_review_worktree",
        "evidence_origin": "local",
    }


def run_selected_tests(
    selected: list[dict[str, Any]],
    worktree: str | None,
    config: ProjectConfig,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    timeout_seconds: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    if dry_run:
        return [{"id": item.get("id"), "command": item.get("command"), "status": "NOT_RUN", "reason": "dry_run", "execution_target": "local_disposable_review_worktree", "evidence_origin": "local"} for item in selected]
    if not worktree:
        return [{"id": item.get("id"), "command": item.get("command"), "status": "BLOCK", "reason": "detached_worktree_unavailable", "execution_target": "local_disposable_review_worktree", "evidence_origin": "local"} for item in selected]
    evidence_dir = config.paths.evidence / "reviews" / f"{_repo_slug(repo)}-pr-{pr_number}-{head_sha[:12]}"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for item in selected:
        command = str(item.get("command") or "")
        command_timeout_seconds = max(int(timeout_seconds), int(item.get("timeout_seconds") or 0))
        try:
            argv = _safe_test_argv(command, worktree=Path(worktree))
            if argv is None:
                results.append({
                    "id": item.get("id"),
                    "command": command,
                    "status": "BLOCK",
                    "reason": "unsafe_review_test_command",
                    "exit_code": 2,
                    "stdout": None,
                    "stderr": None,
                    "execution_target": "local_disposable_review_worktree",
                    "evidence_origin": "local",
                })
                continue
            completed = subprocess.run(
                argv,
                cwd=worktree,
                shell=False,
                text=True,
                capture_output=True,
                timeout=max(1, command_timeout_seconds),
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            exit_code = completed.returncode
            infrastructure_reason = _review_test_infrastructure_reason(stdout, stderr)
            if completed.returncode == 0:
                status = "PASS"
                reason = None
            elif infrastructure_reason:
                status = "BLOCK"
                reason = infrastructure_reason
            else:
                status = "FAIL"
                reason = "test_command_failed"
        except subprocess.TimeoutExpired as exc:
            status = "BLOCK"
            reason = "test_command_timeout"
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            exit_code = 124
        except OSError as exc:
            status = "BLOCK"
            reason = "test_executable_missing"
            stdout = ""
            stderr = str(exc)
            exit_code = 127
        fallback = None
        fallback_attempted = False
        if status == "BLOCK" and reason == "test_dependency_missing" and len(argv) >= 4 and argv[1:3] == ["-m", "pytest"]:
            fallback_attempted = True
            requested_paths = argv[3:-1]
            if requested_paths == ["tests"]:
                fallback_paths = [
                    str(path.relative_to(Path(worktree))).replace("\\", "/")
                    for path in sorted(Path(worktree).joinpath("tests").rglob("test_*.py"))
                    if "browser" not in str(path).lower() and "/ui/" not in str(path).lower()
                ]
            else:
                fallback_paths = [path for path in requested_paths if "browser" not in path.lower() and "ui" not in path.lower()]
            if fallback_paths:
                fallback_argv = argv[:3] + fallback_paths + argv[-1:]
                fallback_run = subprocess.run(
                    fallback_argv,
                    cwd=worktree,
                    shell=False,
                    text=True,
                    capture_output=True,
                    timeout=max(1, command_timeout_seconds),
                    check=False,
                )
                fallback_stdout = fallback_run.stdout or ""
                fallback_stderr = fallback_run.stderr or ""
                fallback_status = "PASS" if fallback_run.returncode == 0 else "FAIL"
                fallback = {
                    "command": " ".join(shlex.quote(part) for part in fallback_argv),
                    "status": fallback_status,
                    "exit_code": fallback_run.returncode,
                    "stdout": fallback_stdout,
                    "stderr": fallback_stderr,
                    "reason": None if fallback_status == "PASS" else "fallback_test_command_failed",
                    "coverage_status": "PARTIAL",
                    "skipped_scope": [path for path in requested_paths if path not in fallback_paths],
                    "skipped_reason": "browser_or_ui_dependency_scope_excluded",
                }
            else:
                fallback = {
                    "command": None,
                    "status": "NOT_RUN",
                    "reason": "fallback_scope_empty",
                    "coverage_status": "PARTIAL",
                    "skipped_scope": requested_paths,
                    "skipped_reason": "all_requested_scope_requires_missing_dependency",
                }
        safe_stdout, _ = _redact_output(stdout)
        safe_stderr, _ = _redact_output(stderr)
        pytest_summary = _pytest_result_summary(safe_stdout) if "pytest" in command else {"failed_tests": [], "summary": None}
        failure_details = _pytest_failure_details(safe_stdout, pytest_summary.get("failed_tests", []), command=command) if "pytest" in command else []
        stdout_path = evidence_dir / f"{item.get('id', 'test')}.stdout.log"
        stderr_path = evidence_dir / f"{item.get('id', 'test')}.stderr.log"
        stdout_path.write_text(safe_stdout, encoding="utf-8")
        stderr_path.write_text(safe_stderr, encoding="utf-8")
        results.append(
            {
                "id": item.get("id"),
                "command": command,
                "status": status,
                "reason": reason,
                "exit_code": exit_code,
                "fallback": fallback,
                "fallback_attempted": fallback_attempted,
                "coverage_status": "PARTIAL" if fallback_attempted else "FULL",
                "execution_target": "local_disposable_review_worktree",
                "evidence_origin": "local",
                "python_executable": argv[0],
                "timeout_seconds": command_timeout_seconds,
                "failed_tests": pytest_summary.get("failed_tests", []),
                "failure_details": failure_details,
                "pytest_summary": pytest_summary.get("summary"),
                "stdout": _relative_or_str(stdout_path, config.root),
                "stderr": _relative_or_str(stderr_path, config.root),
                "reproduction": {
                    "steps": ["Use the pinned review worktree.", f"Run `{command}`."],
                    "expected": "The selected test command completes with exit code 0.",
                    "actual": f"status={status}, reason={reason}, exit_code={exit_code}",
                    "evidence": [_relative_or_str(stdout_path, config.root), _relative_or_str(stderr_path, config.root)],
                },
            }
        )
    return results


def analyze_diff(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    diff = str(snapshot.get("diff") or "")
    findings: list[dict[str, Any]] = []
    current_file = ""
    new_line = 0
    for raw_line in diff.splitlines():
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            continue
        if raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)", raw_line)
            new_line = int(match.group(1)) if match else 0
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            line_text = raw_line[1:]
            findings.extend(_secret_findings_for_line(current_file, new_line, line_text, diff_hash=_sha256(diff), head_sha=str(snapshot.get("head_sha") or ""), repo=str(snapshot.get("repo") or ""), pr_number=int(snapshot.get("pr_number") or 0)))
            new_line += 1
        elif not raw_line.startswith("-"):
            new_line += 1
    return findings


def write_review_report(config: ProjectConfig, report: dict[str, Any]) -> dict[str, Any]:
    repo = str(report.get("repo") or "repo")
    pr_number = int(report.get("pr_number") or 0)
    head_sha = str(report.get("head_sha") or "unknown")
    directory = config.paths.reports / "reviews"
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{_repo_slug(repo)}-pr-{pr_number}-{head_sha[:12]}"
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    legacy_text_path = directory / f"{stem}.txt"
    safe_report, _ = redact_structure(report, prefix="review_report")
    json_path.write_text(json_dumps(safe_report) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(safe_report), encoding="utf-8")
    # The detailed human-readable report is now Markdown, not a third .txt file.
    legacy_text_path.unlink(missing_ok=True)
    return {
        "json_path": _relative_or_str(json_path, config.root),
        "markdown_path": _relative_or_str(markdown_path, config.root),
        "json_path_obj": json_path,
    }


def _review_comment_body(report: dict[str, Any], inline: list[dict[str, Any]]) -> str:
    """Return the exact canonical Markdown report used for the PR message.

    Inline findings remain separate Gitea review comments.  The main PR body
    deliberately has one source of truth: the same deterministic Markdown
    written to ``<repo>/.quality-pilot-project/reports/reviews/*.md``.
    """
    _ = inline
    return _render_markdown(report)

def _gitea_review_comments(inline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for item in inline:
        path = str(item.get("path") or "").strip()
        try:
            line = int(item.get("line"))
        except (TypeError, ValueError):
            line = 0
        if not path or line <= 0:
            continue
        comments.append(
            {
                "path": path,
                "new_line_num": line,
                "body": str(item.get("body") or "Review finding."),
            }
        )
    return comments


def prepare_gitea_review_reply(
    config: ProjectConfig,
    report: dict[str, Any],
    *,
    report_hash: str,
    confirm: bool,
    dry_run: bool,
) -> dict[str, Any]:
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    inline = _review_inline_comments(findings, head_sha=str(report.get("head_sha") or ""), report_hash=report_hash)
    summary = str(report.get("conclusion") or "")
    body = _review_comment_body(report, inline)
    gitea_comments = _gitea_review_comments(inline)
    preview = {
        "repo": report.get("repo"),
        "pr_number": report.get("pr_number"),
        "head_sha": report.get("head_sha"),
        "summary": summary,
        "state": "COMMENT",
        "body": body,
        "inline_comments": inline,
        "report_hash": report_hash,
        "remote_action": "gitea.pull_request.review",
    }
    base = {
        "status": "dry_run" if dry_run else ("awaiting_confirmation" if not confirm else "local_only_pending"),
        "review_state": "COMMENT",
        "approval_decision": "USER_DECISION_REQUIRED",
        "preview": preview,
        "remote_apply": False,
    }
    review_scope_present = "comprehensive_review" in report
    qa_incomplete = review_scope_present and (
        not bool(report.get("comprehensive_review")) or str(report.get("qa_outcome") or "") != "PASS"
    )
    evidence_incomplete = str(report.get("test_outcome") or "") != "PASS" or bool(report.get("coverage_gap")) or qa_incomplete
    if dry_run:
        return base
    if not confirm:
        return {
            **base,
            "status": "awaiting_confirmation",
            "reason": "human_confirmation_required_for_advisory_comment",
            "next_action": "Review the report recommendations, then explicitly confirm the COMMENT handoff if desired",
        }
    if config is None:
        return {
            **base,
            "status": "local_only_pending",
            "reason": "review_request_config_missing",
            "next_action": "Use a configured product repository root to persist and apply the advisory COMMENT request",
        }
    request_path = review_mcp_request_path(config)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request = {
        "schema": REVIEW_REQUEST_SCHEMA,
        "operation": "gitea.pull_request.review",
        "status": "needs_mcp_apply",
        "repo": report.get("repo"),
        "pr_number": report.get("pr_number"),
        "head_sha": report.get("head_sha"),
        "commit_id": report.get("head_sha"),
        "report_hash": report_hash,
        "state": "COMMENT",
        "body": body,
        "summary": summary,
        "comments": gitea_comments,
        "inline_comments": inline,
        "advisory_only": True,
        "approval_decision": "USER_DECISION_REQUIRED",
        "recommendations": report.get("recommendations", []),
        "developer_review": report.get("developer_review", {}),
        "evidence_incomplete": evidence_incomplete,
        "evidence": {
            "test_outcome": report.get("test_outcome"),
            "qa_outcome": report.get("qa_outcome"),
            "qa_matrix": report.get("qa_review", {}).get("matrix", {}) if isinstance(report.get("qa_review"), dict) else {},
            "coverage_gap": bool(report.get("coverage_gap")),
            "diff_targeted_oracle": report.get("diff_targeted_oracle", {}),
            "product_test_outcome": report.get("product_test_outcome"),
            "browser_ui_outcome": report.get("browser_ui_outcome"),
            "browser_evidence": report.get("browser_evidence", []),
            "product_test": report.get("qa_review", {}).get("product_test", {}) if isinstance(report.get("qa_review"), dict) else {},
            "test_results": report.get("test_results", []),
            "snapshot_path": report.get("snapshot_path"),
            "review_report_path": report.get("report_paths", {}).get("json_path"),
            "review_report_markdown_path": report.get("report_paths", {}).get("markdown_path"),
            "report_hash": report_hash,
        },
        "safety": {
            "write_gate_required": True,
            "current_head_required": True,
            "self_review_allowed_but_not_approval": True,
            "allowed_targets": ["pull_request_review"],
            "deduplication_key": f"{report.get('repo')}:{report.get('pr_number')}:{report.get('head_sha')}:{report_hash}",
        },
        "request_paths": {
            "result": _relative_or_str(review_mcp_result_path(config), config.root),
        },
    }
    try:
        ensure_safe_structure(request, context="review request")
    except ValueError as exc:
        return {
            **base,
            "status": "local_only_pending",
            "reason": "redaction_failed_closed",
            "message": "review request did not pass the centralized security detector",
        }
    safe_request, redaction_findings = redact_structure(request, prefix="review_request")
    if redaction_findings:
        return {
            **base,
            "status": "local_only_pending",
            "reason": "redaction_failed_closed",
            "redaction_findings": [item.as_dict() for item in redaction_findings],
        }
    request_path.write_text(json_dumps(safe_request) + "\n", encoding="utf-8")
    remote_ready = mcp_server_is_available(config, "gitea")
    return {
        **base,
        "status": "needs_mcp_apply" if remote_ready else "local_only_pending",
        "remote_ready": remote_ready,
        "request_path": _relative_or_str(request_path, config.root),
        "next_action": "Call the configured Gitea MCP review tool" if remote_ready else "Retry after Gitea MCP readiness is available",
    }


def review_mcp_request_path(config: ProjectConfig) -> Path:
    return configured_mcp_json_path(config, "review_write_request_json")


def review_mcp_result_path(config: ProjectConfig) -> Path:
    return configured_mcp_json_path(config, "review_write_result_json")


def review_apply_ledger_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / "review-write-ledger.json"


def complete_gitea_review_apply(
    config: ProjectConfig,
    *,
    request_json: str | Path | None = None,
    result_json: str | Path | None = None,
) -> dict[str, Any]:
    """Reconcile a Hermes Gitea MCP review result without performing a write.

    Hermes performs the actual MCP call.  This deterministic step validates
    that the returned result belongs to the exact repo/PR/head/report request,
    records a retryable failure, and rejects duplicate application evidence.
    """
    request_path = _resolve_review_path(config, request_json, review_mcp_request_path(config))
    result_path = _resolve_review_path(config, result_json, review_mcp_result_path(config))
    request = _load_json_object(request_path, "review request")
    result = _load_json_object(result_path, "review result")
    if request.get("schema") != REVIEW_REQUEST_SCHEMA or request.get("operation") != "gitea.pull_request.review":
        raise ReviewError("review_request_invalid_schema_or_operation")
    safety = request.get("safety") if isinstance(request.get("safety"), dict) else {}
    if safety.get("allowed_targets") != ["pull_request_review"]:
        raise ReviewError("review_request_target_not_allowed")
    if request.get("state") != "COMMENT" or request.get("advisory_only") is not True:
        raise ReviewError("review_request_must_be_advisory_comment")
    key = str(safety.get("deduplication_key") or "").strip()
    if not key:
        raise ReviewError("review_request_deduplication_key_missing")
    ledger = _load_json_object(review_apply_ledger_path(config), "review apply ledger", required=False)
    entries = ledger.get("entries") if isinstance(ledger.get("entries"), list) else []
    previous = next((item for item in entries if isinstance(item, dict) and item.get("deduplication_key") == key), None)
    if isinstance(previous, dict) and previous.get("status") == "ok":
        return {
            "status": "duplicate",
            "reason": "review_reply_already_reconciled",
            "deduplication_key": key,
            "previous": previous,
            "request_path": _relative_or_str(request_path, config.root),
            "result_path": _relative_or_str(result_path, config.root),
        }
    _validate_review_result_identity(request, result)
    try:
        ensure_safe_structure(result, context="review result")
    except ValueError as exc:
        raise ReviewError("review_result_redaction_failed_closed") from exc
    success = result.get("ok") is True or str(result.get("status") or "").lower() in {"ok", "success", "applied"}
    entry = {
        "deduplication_key": key,
        "status": "ok" if success else "blocked",
        "repo": request.get("repo"),
        "pr_number": request.get("pr_number"),
        "head_sha": request.get("head_sha"),
        "report_hash": request.get("report_hash"),
        "result_status": result.get("status"),
        "result_path": _relative_or_str(result_path, config.root),
        "updated_at": utc_now(),
    }
    merged_entries = [item for item in entries if not (isinstance(item, dict) and item.get("deduplication_key") == key)]
    merged_entries.append(entry)
    ledger_payload = {
        "schema": "quality-pilot.gitea-review-write-ledger.v1",
        "updated_at": utc_now(),
        "entries": sorted(merged_entries, key=lambda item: str(item.get("deduplication_key") or "")),
    }
    ledger_path = review_apply_ledger_path(config)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json_dumps(ledger_payload) + "\n", encoding="utf-8")
    payload = {
        "status": "ok" if success else "blocked",
        "reason": None if success else "gitea_mcp_review_write_failed",
        "retryable": not success,
        "deduplication_key": key,
        "repo": request.get("repo"),
        "pr_number": request.get("pr_number"),
        "head_sha": request.get("head_sha"),
        "report_hash": request.get("report_hash"),
        "request_path": _relative_or_str(request_path, config.root),
        "result_path": _relative_or_str(result_path, config.root),
        "ledger_path": _relative_or_str(ledger_path, config.root),
        "response": result,
    }
    safe_payload, findings = redact_structure(payload, prefix="review_apply")
    if findings:
        raise ReviewError("review_apply_redaction_failed_closed")
    apply_path = config.paths.state / "gitea-mcp" / "review-apply-result.json"
    apply_path.parent.mkdir(parents=True, exist_ok=True)
    apply_path.write_text(json_dumps(safe_payload) + "\n", encoding="utf-8")
    return {**payload, "apply_result_path": _relative_or_str(apply_path, config.root)}


def _review_inline_comments(findings: list[Any], *, head_sha: str, report_hash: str) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in findings:
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get("id") or "finding")
        dedupe = f"{finding_id}:{head_sha}:{report_hash}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        comments.append({
            "finding_id": finding_id,
            "path": item.get("path"),
            "line": item.get("line"),
            "severity": item.get("severity"),
            "body": item.get("message"),
            "head_sha": head_sha,
            "report_hash": report_hash,
            "idempotency_key": dedupe,
        })
    return comments


def _validate_review_result_identity(request: dict[str, Any], result: dict[str, Any]) -> None:
    for field in ("repo", "pr_number", "head_sha", "report_hash"):
        returned = result.get(field)
        if returned in (None, ""):
            continue
        if str(returned) != str(request.get(field)):
            raise ReviewError(f"review_result_stale_{field}")


def _resolve_review_path(config: ProjectConfig, value: str | Path | None, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    if not path.is_absolute():
        path = config.root / path
    return path.resolve()


def _load_json_object(path: Path, label: str, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ReviewError(f"{label}_missing")
        return {"entries": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"{label}_invalid") from exc
    if not isinstance(value, dict):
        raise ReviewError(f"{label}_invalid")
    return value


def _normalize_snapshot(raw: dict[str, Any], *, repo: str, pr_number: int) -> dict[str, Any]:
    raw_repo = str(raw.get("repo") or raw.get("full_name") or raw.get("repository") or repo)
    if isinstance(raw.get("repository"), dict):
        raw_repo = str(raw["repository"].get("full_name") or raw["repository"].get("name") or repo)
    if raw_repo != repo:
        raise ReviewError(f"PR snapshot repository mismatch: expected {repo}, got {raw_repo}")
    number = _int_or_none(raw.get("number") or raw.get("index") or raw.get("pr_number"))
    if number is not None and number != int(pr_number):
        raise ReviewError(f"PR snapshot number mismatch: expected {pr_number}, got {number}")
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    head_sha = str(raw.get("head_sha") or raw.get("head_commit_id") or head.get("sha") or head.get("commit_id") or "").strip()
    if not head_sha:
        raise ReviewError("gitea_pr_snapshot_invalid: head SHA is required")
    files = raw.get("changed_files") if isinstance(raw.get("changed_files"), list) else raw.get("files", [])
    changed_files = []
    for item in files if isinstance(files, list) else []:
        if isinstance(item, str):
            changed_files.append({"path": item})
        elif isinstance(item, dict) and (item.get("filename") or item.get("path")):
            changed_files.append({"path": item.get("filename") or item.get("path"), "status": item.get("status")})
    author = raw.get("user") if isinstance(raw.get("user"), dict) else raw.get("author") if isinstance(raw.get("author"), dict) else {}
    raw_state = str(raw.get("state") or "").strip().lower()
    if not raw_state and (raw.get("closed") is True or raw.get("merged") is True):
        raw_state = "closed"
    return {
        "repo": repo,
        "pr_number": int(pr_number),
        "title": str(raw.get("title") or ""),
        "state": raw_state,
        "merged": bool(raw.get("merged") is True),
        "updated_at": str(raw.get("updated_at") or raw.get("updated_on") or ""),
        "url": str(raw.get("html_url") or raw.get("url") or ""),
        "author": str(author.get("login") or author.get("username") or raw.get("author_login") or ""),
        "base_sha": str(raw.get("base_sha") or base.get("sha") or ""),
        "base_ref": str(raw.get("base_ref") or base.get("ref") or ""),
        "head_sha": head_sha,
        "head_ref": str(raw.get("head_ref") or head.get("ref") or ""),
        "diff": str(raw.get("diff") or raw.get("patch") or ""),
        "changed_files": changed_files,
        "source_version": str(raw.get("updated_at") or raw.get("updated_on") or head_sha),
    }


def _prepare_detached_worktree(
    config: ProjectConfig,
    snapshot: dict[str, Any],
    *,
    checkout: str | Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    source = Path(checkout).expanduser().resolve() if checkout else config.root
    head_sha = str(snapshot["head_sha"])
    worktree = config.paths.state / "reviews" / _repo_slug(str(snapshot["repo"])) / f"pr-{snapshot['pr_number']}-{head_sha[:12]}"
    if dry_run:
        return {"status": "planned", "source": str(source), "path": str(worktree), "head_sha": head_sha}
    if not (source / ".git").exists() and not (source / "HEAD").exists():
        return {"status": "blocked", "reason": "checkout_not_git", "source": _safe_output(str(source)), "path": None, "head_sha": head_sha}
    worktree.parent.mkdir(parents=True, exist_ok=True)
    fetch_status: dict[str, Any] = {}
    if not worktree.exists():
        fetch = _run_git(["-C", str(source), "fetch", "--no-tags", "origin", head_sha], timeout=30)
        fetch_status["head"] = "ok" if fetch.returncode == 0 else "failed"
        if fetch.returncode != 0:
            return {"status": "blocked", "reason": "git_fetch_failed", "message": _safe_output(fetch.stderr), "source": _safe_output(str(source)), "path": None, "head_sha": head_sha}
        base_sha = str(snapshot.get("base_sha") or "").strip()
        if base_sha and base_sha != head_sha:
            base_fetch = _run_git(["-C", str(source), "fetch", "--no-tags", "origin", base_sha], timeout=30)
            fetch_status["base"] = "ok" if base_fetch.returncode == 0 else "failed"
        add = _run_git(["-C", str(source), "worktree", "add", "--detach", str(worktree), head_sha], timeout=30)
        if add.returncode != 0:
            return {"status": "blocked", "reason": "git_worktree_failed", "message": _safe_output(add.stderr), "source": _safe_output(str(source)), "path": None, "head_sha": head_sha}
    return {"status": "ready", "source": str(source), "path": str(worktree), "head_sha": head_sha, "fetch": fetch_status}


def _reconstruct_snapshot_diff(snapshot: dict[str, Any], worktree: dict[str, Any]) -> dict[str, Any]:
    existing = str(snapshot.get("diff") or "")
    if existing:
        snapshot["diff_source"] = "mcp_snapshot"
        return {"status": "PASS", "source": "mcp_snapshot", "reconstructed": False}
    changed_files = snapshot.get("changed_files") if isinstance(snapshot.get("changed_files"), list) else []
    if not changed_files:
        snapshot["diff_source"] = "empty_snapshot"
        return {"status": "NOT_APPLICABLE", "source": "empty_snapshot", "reconstructed": False}
    if worktree.get("status") != "ready":
        snapshot["diff_source"] = "unavailable"
        return {"status": "BLOCK", "reason": "detached_worktree_unavailable", "reconstructed": False}
    source = str(worktree.get("source") or "")
    base_sha = str(snapshot.get("base_sha") or "").strip()
    head_sha = str(snapshot.get("head_sha") or "").strip()
    if not source or not base_sha or not head_sha:
        snapshot["diff_source"] = "unavailable"
        return {"status": "BLOCK", "reason": "base_or_head_sha_missing", "reconstructed": False}
    result = _run_git(["-C", source, "diff", "--no-ext-diff", base_sha, head_sha], timeout=60)
    if result.returncode != 0:
        fetch = _run_git(["-C", source, "fetch", "--no-tags", "origin", base_sha], timeout=30)
        if fetch.returncode == 0:
            result = _run_git(["-C", source, "diff", "--no-ext-diff", base_sha, head_sha], timeout=60)
    if result.returncode != 0:
        snapshot["diff_source"] = "reconstruction_failed"
        return {
            "status": "BLOCK",
            "reason": "git_diff_reconstruction_failed",
            "message": _safe_output(result.stderr),
            "reconstructed": False,
        }
    diff = result.stdout or ""
    if not diff:
        snapshot["diff_source"] = "reconstructed_empty"
        return {"status": "BLOCK", "reason": "git_diff_empty_for_changed_files", "reconstructed": False}
    snapshot["diff"] = diff
    snapshot["diff_source"] = "git_reconstructed"
    return {"status": "PASS", "source": "git_reconstructed", "reconstructed": True, "changed_file_count": len(changed_files)}


def _resolve_review_product_python(config: ProjectConfig, worktree: Path) -> Path | None:
    """Resolve the confirmed host product interpreter for the disposable sandbox."""
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    entrypoint = str(runtime.get("primary_entrypoint") or "")
    try:
        candidate = Path(__import__("shlex").split(entrypoint)[0]) if entrypoint else Path(".venv/bin/python")
    except ValueError:
        candidate = Path(".venv/bin/python")
    if not candidate.is_absolute():
        candidate = config.root / candidate
    candidate = candidate.resolve()
    return candidate if candidate.is_file() else None


def _build_review_project_config(config: ProjectConfig, worktree: Path, review_workspace: Path) -> ProjectConfig:
    data = deepcopy(config.data)
    paths = project_paths(worktree, review_workspace)
    return ProjectConfig(
        root=worktree,
        path=worktree / ".quality-pilot-review.yaml",
        data=data,
        paths=paths,
    )


def _run_comprehensive_review_qa(
    config: ProjectConfig,
    *,
    snapshot: dict[str, Any],
    worktree: dict[str, Any],
    repo: str,
    pr_number: int,
    head_sha: str,
    timeout_seconds: int,
    dry_run: bool,
    test_selection: dict[str, Any] | None = None,
    test_results: list[dict[str, Any]] | None = None,
    python_executable: str | None = None,
) -> dict[str, Any]:
    dimensions = ["black_box", "white_box", "functional", "boundary", "stress", "ui", "ux", "documentation"]
    if dry_run:
        return {
            "schema": "quality-pilot.review-qa.v1",
            "mode": "comprehensive",
            "status": "PLANNED",
            "generation": {"status": "PLANNED", "reason": "dry_run"},
            "cases": [],
            "product_test": {"schema": "quality-pilot.product-build-run.v1", "status": "PLANNED", "reason": "dry_run"},
            "matrix": {dimension: {"status": "PLANNED", "reason": "dry_run"} for dimension in dimensions} | {"product_binary": {"status": "PLANNED", "reason": "dry_run"}},
            "outcome": "PLANNED",
            "required_dimensions": dimensions,
        }
    if worktree.get("status") != "ready" or not worktree.get("path"):
        return {
            "schema": "quality-pilot.review-qa.v1",
            "mode": "comprehensive",
            "status": "BLOCK",
            "generation": {"status": "BLOCK", "reason": "detached_worktree_unavailable"},
            "cases": [],
            "matrix": {dimension: {"status": "BLOCK", "reason": "detached_worktree_unavailable"} for dimension in dimensions},
            "outcome": "BLOCK",
            "required_dimensions": dimensions,
        }

    worktree_path = Path(str(worktree["path"]))
    review_workspace = config.paths.state / "reviews" / _repo_slug(repo) / f"pr-{pr_number}-{head_sha[:12]}" / "quality-pilot"
    review_config = _build_review_project_config(config, worktree_path, review_workspace)
    profile = environment_profile_status(review_config)
    run_id = f"review-{_repo_slug(repo)}-pr-{pr_number}-{head_sha[:12]}"
    review_python_command = python_executable or _review_python_executable(config, worktree_path)
    product_contract = build_product_case_contract(
        review_config,
        case_id=f"PR-{pr_number}-PRODUCT",
        title=f"PR #{pr_number} product build and semantic operation",
        review_id=run_id,
        snapshot=snapshot,
    )
    product_case_result = run_case(
        product_contract,
        RunContext(
            root=worktree_path,
            evidence_dir=review_config.paths.evidence / run_id,
            environment_profile=profile,
            adapter_config=review_config,
            adapter_snapshot=snapshot,
            adapter_review_id=run_id,
            product_python=_resolve_review_product_python(config, worktree_path),
            review_python=review_python_command,
        ),
        dry_run=dry_run,
    )
    browser_regression_case_result = _run_browser_regression_case(
        review_config,
        worktree=worktree_path,
        run_id=run_id,
        selected_tests=test_selection or {},
        existing_test_results=test_results or [],
        environment_profile=profile,
        python_executable=review_python_command,
        timeout_seconds=timeout_seconds,
    )
    product_test = product_case_result.get("product_result") if isinstance(product_case_result.get("product_result"), dict) else product_case_result
    product_test["case_id"] = product_case_result.get("case_id")
    product_test["run_id"] = product_case_result.get("run_id")
    product_test["contract_hash"] = product_case_result.get("contract_hash")
    product_test["case_result_path"] = product_case_result.get("result_path")
    generation: dict[str, Any]
    case_results: list[dict[str, Any]] = []
    product_case = _canonical_product_case(product_case_result, root=config.root)
    if product_case is not None:
        case_results.append(product_case)
        browser_case = _canonical_browser_case(product_case_result, root=config.root)
        if browser_case is not None:
            case_results.append(browser_case)
    if browser_regression_case_result is not None:
        case_results.append(browser_regression_case_result)
    contracts: list[Any] = []
    product_contract_raw = deepcopy(product_contract.raw)
    browser_contract_raw = deepcopy(product_case_result.get("browser_contract_raw")) if isinstance(product_case_result.get("browser_contract_raw"), dict) else None
    try:
        # The overlay is PR/head scoped and disposable. Rebuild its generated
        # contracts on every review, but preserve the already executed product
        # and Browser contracts as canonical lineage sources.
        #
        # The overlay is PR/head scoped and disposable. Rebuild its contracts on
        # every review so a new diff or a changed QA engine cannot reuse stale
        # generated cases from a previous review invocation.
        shutil.rmtree(review_config.paths.cases, ignore_errors=True)
        generation = generate_cases_init(
            review_config,
            feature=f"PR #{pr_number}: {snapshot.get('title') or repo}",
            profile="auto",
            count=len(dimensions),
            fast=True,
            force=True,
            review_context={
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "base_sha": snapshot.get("base_sha"),
                "diff_hash": _sha256(str(snapshot.get("diff") or "")),
                "changed_files": [
                    str(item.get("path") or "")
                    for item in snapshot.get("changed_files", [])
                    if isinstance(item, dict) and item.get("path")
                ],
            },
        )
        review_config.paths.cases.mkdir(parents=True, exist_ok=True)
        (review_config.paths.cases / f"{product_contract.case_id}.yaml").write_text(
            json.dumps(product_contract_raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if browser_contract_raw:
            browser_case_id = str(browser_contract_raw.get("case_id") or "")
            if browser_case_id:
                (review_config.paths.cases / f"{browser_case_id}.yaml").write_text(
                    json.dumps(browser_contract_raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        excluded_case_ids = {product_contract.case_id, str((browser_contract_raw or {}).get("case_id") or "")}
        generated_paths = [
            path for path in sorted(review_config.paths.cases.glob("*.yaml"))
            if path.stem not in excluded_case_ids
        ]
        if generated_paths:
            # Validate only generated contracts here.  The canonical product
            # contract carries review lineage metadata such as review_id; it
            # is already executed and must not be revalidated as a generated
            # test contract or mistaken for opaque secret material.
            # ``generated_paths`` contains individual contract files.  Load
            # each file directly; passing a file to ``load_contracts`` silently
            # returns an empty list because that API expects a directory.
            contracts = [load_contract(path) for path in generated_paths]
        else:
            # Keep direct-library and mocked generation callers compatible
            # when no generated files were materialized.
            contracts = [contract for contract in load_contracts(review_config.paths.cases) if contract.case_id not in excluded_case_ids]
    except (CaseGenerationError, OSError, ValueError) as exc:
        generation = {"status": "BLOCK", "reason": "case_generation_failed", "error": type(exc).__name__}

    for contract in contracts:
        try:
            result = run_case(
                contract,
                RunContext(
                    root=worktree_path,
                    evidence_dir=review_config.paths.evidence / run_id,
                    environment_profile=profile,
                    review_python=review_python_command,
                ),
                dry_run=False,
            )
            stamp_result_run_id(result, worktree_path, run_id)
        except Exception as exc:
            result = {
                "case_id": contract.case_id,
                "status": "BLOCK",
                "truth_status": "BLOCK",
                "partial_probe": False,
                "evidence": [],
                "blocked_reason": "review_case_run_failed",
                "error": type(exc).__name__,
            }
        dimensions_for_case = [str(item) for item in contract.raw.get("swqa_dimensions", []) if str(item).strip()]
        quality = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
        case_results.append(
            {
                "case_id": contract.case_id,
                "title": contract.title,
                "contract_hash": contract.contract_hash,
                "status": result.get("status"),
                "truth_status": result.get("truth_status"),
                "partial_probe": bool(result.get("partial_probe")),
                "dimensions": dimensions_for_case,
                "black_box_capable": bool(
                    quality.get("executable_scope") == "prepared_environment_readonly_product_command"
                    or str(quality.get("safe_command_source_type") or "").startswith("prepared_environment")
                ),
                "result_path": _review_artifact_path(result.get("result_path"), worktree_path, config.root),
                "evidence": [
                    _review_artifact_path(item, worktree_path, config.root)
                    for item in result.get("evidence", [])
                    if item
                ],
            }
        )

    matrix = _build_review_qa_matrix(
        snapshot=snapshot,
        worktree=worktree,
        regression_available=True,
        regression_status="PASS" if not dry_run else "PLANNED",
        findings=[],
        case_results=case_results,
        product_test=product_test,
    )
    # The diff/white-box cells are completed by the caller after diff analysis.
    return {
        "schema": "quality-pilot.review-qa.v1",
        "mode": "comprehensive",
        "status": generation.get("status", "ok"),
        "generation": generation,
        "cases": case_results,
        "product_test": product_test,
        "browser_regression_case": browser_regression_case_result,
        "matrix": matrix,
        "outcome": (
            "BLOCK"
            if generation.get("status") == "BLOCK"
            else _review_qa_outcome(matrix)
        ),
        "required_dimensions": dimensions,
        "workspace": _relative_or_str(review_workspace, config.root),
        "profile": {
            "status": profile.get("status"),
            "ready": profile.get("ready"),
            "blockers": profile.get("blockers", []),
        },
    }


def _canonical_product_case(result: dict[str, Any], *, root: Path) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not result.get("case_id"):
        return None
    product = result.get("product_result") if isinstance(result.get("product_result"), dict) else result
    return {
        "case_id": result.get("case_id"),
        "title": result.get("title") or "產品建置與語意操作",
        "case_type": "product",
        "contract_hash": result.get("contract_hash"),
        "run_id": result.get("run_id"),
        "status": result.get("status", "NOT_RUN"),
        "truth_status": result.get("truth_status") or result.get("status", "NOT_RUN"),
        "partial_probe": bool(result.get("partial_probe")),
        "dimensions": ["black_box", "functional"],
        "black_box_capable": result.get("status") == "PASS",
        "oracle": result.get("oracle", {"type": "product_build_and_semantic_operation"}),
        "evidence": result.get("evidence", []),
        "result_path": result.get("result_path"),
        "source": "product_case_adapter",
        "execution_target": result.get("execution_target") or product.get("execution_target") or "local",
        "evidence_origin": result.get("evidence_origin") or product.get("evidence_origin") or "local",
        "product_result": product,
    }


def _run_browser_regression_case(
    config: ProjectConfig,
    *,
    worktree: Path,
    run_id: str,
    selected_tests: dict[str, Any],
    existing_test_results: list[dict[str, Any]],
    environment_profile: dict[str, Any],
    python_executable: str,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    selected = selected_tests.get("selected", []) if isinstance(selected_tests, dict) else []
    browser_items = [
        item for item in selected
        if isinstance(item, dict) and any(token in str(item.get("command") or "").lower() for token in ("browser", "playwright", "ui"))
    ]
    if not browser_items:
        return None
    item = browser_items[0]
    command_timeout_seconds = max(int(timeout_seconds), int(item.get("timeout_seconds") or 0))
    command = str(item.get("command") or "")
    try:
        command_argv = shlex.split(command)
        if command_argv and Path(command_argv[0]).name.lower().startswith("python") and python_executable:
            command = shlex.join([python_executable, *command_argv[1:]])
    except ValueError:
        pass
    case_id = "PR-BROWSER-UI-REGRESSION"
    evidence_dir = config.paths.evidence / "reviews" / run_id / case_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    existing = next((value for value in existing_test_results if value.get("id") == item.get("id")), None)
    if isinstance(existing, dict):
        status = str(existing.get("status") or "BLOCK")
        reason = existing.get("reason")
        exit_code = existing.get("exit_code", 2)
        evidence = [value for value in (existing.get("stdout"), existing.get("stderr")) if value]
        result = {
            "case_id": case_id,
            "title": "PR browser UI and UX regression tests",
            "case_type": "playwright_ui_regression",
            "status": status,
            "truth_status": status,
            "official_result": status == "PASS",
            "partial_probe": False,
            "dimensions": ["functional", "ui", "ux"],
            "black_box_capable": False,
            "contract_hash": _sha256(f"PR-BROWSER-UI-REGRESSION|{command}|{run_id}"),
            "run_id": run_id,
            "oracle": {"type": "pytest_playwright_semantic_tests", "command": command},
            "commands": [{"id": "browser-regression", "command": command, "status": status, "exit_code": exit_code}],
            "evidence": evidence,
            "result_path": str((evidence_dir / "result.json").relative_to(config.root)) if (evidence_dir / "result.json").is_relative_to(config.root) else str(evidence_dir / "result.json"),
            "reason": reason,
            "timeout_seconds": existing.get("timeout_seconds"),
            "failed_tests": existing.get("failed_tests", []),
            "failure_details": existing.get("failure_details", []),
            "pytest_summary": existing.get("pytest_summary"),
            "screenshot": existing.get("screenshot"),
            "screenshot_sha256": existing.get("screenshot_sha256"),
            "source": "existing_targeted_test_result",
            "execution_target": "local_pinned_worktree",
            "evidence_origin": "local",
        }
        (evidence_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return result
    argv = _safe_test_argv(command, worktree=worktree)
    if not environment_profile.get("ready") and str(environment_profile.get("execution_mode") or "local") != "remote":
        status, reason, exit_code, stdout, stderr = "BLOCK", "environment_profile_required", 2, "", ""
    elif argv is None:
        status, reason, exit_code, stdout, stderr = "BLOCK", "unsafe_review_test_command", 2, "", ""
    else:
        completed = subprocess.run(argv, cwd=worktree, shell=False, text=True, capture_output=True, timeout=max(1, command_timeout_seconds), check=False)
        stdout, stderr = completed.stdout or "", completed.stderr or ""
        exit_code = completed.returncode
        status = "PASS" if exit_code == 0 else ("BLOCK" if _review_test_infrastructure_reason(stdout, stderr) else "FAIL")
        reason = None if status == "PASS" else (_review_test_infrastructure_reason(stdout, stderr) or "browser_ui_test_failed")
    stdout_path = evidence_dir / "stdout.log"
    stderr_path = evidence_dir / "stderr.log"
    safe_stdout, _ = _redact_output(stdout)
    safe_stderr, _ = _redact_output(stderr)
    pytest_summary = _pytest_result_summary(safe_stdout)
    stdout_path.write_text(safe_stdout, encoding="utf-8")
    stderr_path.write_text(safe_stderr, encoding="utf-8")
    result = {
        "case_id": case_id,
        "title": "PR browser UI and UX regression tests",
        "case_type": "playwright_ui_regression",
        "status": status,
        "execution_target": "local_pinned_worktree",
        "evidence_origin": "local",
        "truth_status": status,
        "official_result": status == "PASS",
        "partial_probe": False,
        "dimensions": ["functional", "ui", "ux"],
        "black_box_capable": False,
        "contract_hash": _sha256(f"{case_id}|{command}|{run_id}"),
        "run_id": run_id,
        "oracle": {"type": "pytest_playwright_semantic_tests", "command": command},
        "commands": [{"id": "browser-regression", "command": command, "status": status, "exit_code": exit_code}],
        "evidence": [_relative_or_str(stdout_path, config.root), _relative_or_str(stderr_path, config.root)],
        "result_path": str((evidence_dir / "result.json").relative_to(config.root)) if (evidence_dir / "result.json").is_relative_to(config.root) else str(evidence_dir / "result.json"),
        "reason": reason,
        "timeout_seconds": command_timeout_seconds,
        "failed_tests": pytest_summary.get("failed_tests", []),
        "failure_details": _pytest_failure_details(safe_stdout, pytest_summary.get("failed_tests", []), command=command),
        "pytest_summary": pytest_summary.get("summary"),
        "screenshot": None,
        "screenshot_sha256": None,
    }
    (evidence_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _product_result_case(product: dict[str, Any], *, repo: str, pr_number: int, head_sha: str, root: Path) -> dict[str, Any] | None:
    if not isinstance(product, dict):
        return None
    case_id = str(product.get("case_id") or f"PR-{pr_number}-PRODUCT")
    return {
        "case_id": case_id,
        "title": "PR product build and semantic operation",
        "case_type": "product_build_and_semantic_operation",
        "contract_hash": product.get("contract_hash") or product.get("contract_identity_hash"),
        "run_id": product.get("run_id"),
        "status": product.get("status", "NOT_RUN"),
        "truth_status": product.get("truth_status") or product.get("status", "NOT_RUN"),
        "partial_probe": False,
        "dimensions": ["black_box", "functional"],
        "black_box_capable": product.get("status") == "PASS",
        "oracle": {"type": "product_build_and_semantic_operation", "semantic": True},
        "evidence": [product.get("result_path")] if product.get("result_path") else [],
        "result_path": product.get("result_path"),
        "source": "product_testing_adapter",
        "execution_target": product.get("execution_target") or "local",
        "evidence_origin": product.get("evidence_origin") or "local",
        "pr_identity": {"repo": repo, "pr_number": pr_number, "head_sha": head_sha},
    }


def _canonical_browser_case(result: dict[str, Any], *, root: Path) -> dict[str, Any] | None:
    browser = result.get("browser_case_result") if isinstance(result.get("browser_case_result"), dict) else None
    if browser is None:
        return None
    return _browser_result_case(browser, parent_case_id=str(result.get("case_id") or "PRODUCT"), root=root)


def _browser_result_case(product: dict[str, Any], *, parent_case_id: str, root: Path) -> dict[str, Any] | None:
    browser = product.get("browser") if isinstance(product.get("browser"), dict) else None
    if browser is None:
        return None
    return {
        "case_id": str(browser.get("case_id") or f"{parent_case_id}-BROWSER-UI"),
        "title": "PR browser UI and UX semantic flow",
        "case_type": "playwright_ui",
        "contract_hash": browser.get("contract_identity_hash") or product.get("contract_hash"),
        "run_id": browser.get("run_id") or product.get("run_id"),
        "status": browser.get("status", "NOT_RUN"),
        "truth_status": browser.get("truth_status") or browser.get("status", "NOT_RUN"),
        "partial_probe": False,
        "dimensions": ["black_box", "functional", "ui", "ux"],
        "black_box_capable": browser.get("status") == "PASS",
        "oracle": {"type": "playwright_ui", "interaction_count": browser.get("interaction_count", 0), "state_assertion_count": browser.get("state_assertion_count", 0)},
        "evidence": [str(value) for key, value in (browser.get("evidence", {}) if isinstance(browser.get("evidence"), dict) else {}).items() if value and not str(key).endswith("sha256")],
        "result_path": browser.get("result_path"),
        "source": "browser_adapter",
        "execution_target": browser.get("execution_target") or ("remote_ssh" if browser.get("evidence_origin") == "remote" else "local_disposable_worktree"),
        "evidence_origin": browser.get("evidence_origin") or "local",
    }


def _build_review_qa_matrix(
    *,
    snapshot: dict[str, Any],
    worktree: dict[str, Any],
    regression_available: bool,
    regression_status: str,
    findings: list[dict[str, Any]],
    case_results: list[dict[str, Any]],
    product_test: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    matrix: dict[str, dict[str, Any]] = {}
    diff = str(snapshot.get("diff") or "")
    matrix["white_box"] = {
        "status": regression_status if diff else "BLOCK",
        "reason": "regression_suite_and_reconstructed_diff" if diff else "diff_unavailable",
        "evidence": [item.get("result_path") for item in case_results if item.get("result_path")],
    }
    for dimension, labels in {
        "functional": {"functional", "positive"},
        "boundary": {"boundary", "invalid_input", "negative"},
        "stress": {"stress_timeout_risk"},
        "ui": {"ui"},
        "ux": {"ux"},
    }.items():
        selected = [item for item in case_results if labels.intersection(set(item.get("dimensions", [])))]
        matrix[dimension] = _review_case_dimension_result(
            selected,
            reason_if_empty=f"no_{dimension}_case",
            require_product_adapter=(dimension not in {"ui", "ux"}),
        )
    black_box_cases = [item for item in case_results if item.get("black_box_capable")]
    matrix["black_box"] = _review_case_dimension_result(black_box_cases, reason_if_empty="product_black_box_adapter_not_proven")
    matrix["documentation"] = _review_documentation_result(worktree, snapshot.get("changed_files", []))
    if isinstance(product_test, dict) and str(product_test.get("status") or "NOT_RUN") not in {"NOT_RUN"}:
        product_status = str(product_test.get("status") or "HOLD")
        matrix["product_binary"] = {
            "status": product_status,
            "reason": product_test.get("reason"),
            "evidence": [product_test.get("result_path")] if product_test.get("result_path") else [],
            "contract_identity_hash": product_test.get("contract_identity_hash"),
        }
        browser = product_test.get("browser") if isinstance(product_test.get("browser"), dict) else None
        if browser is not None:
            matrix["browser_ui"] = {
                "status": str(browser.get("status") or "HOLD"),
                "reason": browser.get("reason"),
                "evidence": browser.get("evidence", {}),
            }
    return matrix


def _review_artifact_path(value: Any, worktree: Path, root: Path) -> str | None:
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = worktree / path
    return _relative_or_str(path.resolve(), root)


def _review_case_dimension_result(
    cases: list[dict[str, Any]],
    *,
    reason_if_empty: str,
    require_product_adapter: bool = False,
) -> dict[str, Any]:
    if not cases:
        return {"status": "HOLD", "reason": reason_if_empty, "case_count": 0}
    if require_product_adapter and any(not item.get("black_box_capable") for item in cases):
        return {
            "status": "HOLD",
            "reason": "product_black_box_adapter_not_proven",
            "case_count": len(cases),
            "case_ids": [item.get("case_id") for item in cases],
        }
    # ``status`` is the command/process result; ``truth_status`` is the
    # case-level oracle result.  A partial probe may have exit code 0 while
    # still being HOLD, so dimension aggregation must use truth_status.
    statuses = {str(item.get("truth_status") or item.get("status") or "BLOCK").upper() for item in cases}
    if "BLOCK" in statuses:
        outcome = "BLOCK"
    elif "FAIL" in statuses:
        outcome = "FAIL"
    elif "HOLD" in statuses or any(item.get("partial_probe") for item in cases):
        outcome = "HOLD"
    elif statuses == {"PASS"}:
        outcome = "PASS"
    else:
        outcome = "HOLD"
    return {
        "status": outcome,
        "case_count": len(cases),
        "case_ids": [item.get("case_id") for item in cases],
        "execution_targets": sorted({str(item.get("execution_target")) for item in cases if item.get("execution_target")}),
        "evidence_origins": sorted({str(item.get("evidence_origin")) for item in cases if item.get("evidence_origin")}),
    }


def _review_documentation_result(worktree: dict[str, Any], changed_files: list[dict[str, Any]]) -> dict[str, Any]:
    documentation = [
        str(item.get("path") or "")
        for item in changed_files
        if isinstance(item, dict) and Path(str(item.get("path") or "")).suffix.lower() in {".md", ".rst", ".txt", ".html", ".htm"}
    ]
    if not documentation:
        return {"status": "NOT_APPLICABLE", "reason": "no_documentation_changes", "case_count": 0}
    root = Path(str(worktree.get("path") or "."))
    invalid = []
    for relative in documentation:
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            invalid.append(relative)
            continue
        if not text.strip():
            invalid.append(relative)
        if path.suffix.lower() in {".html", ".htm"}:
            parser = HTMLParser()
            try:
                parser.feed(text)
                parser.close()
            except Exception:
                invalid.append(relative)
    if invalid:
        return {"status": "FAIL", "reason": "documentation_parse_or_read_failed", "invalid": invalid}
    return {"status": "PASS", "reason": "changed_documentation_read_and_parsed", "files": documentation}


def _targeted_oracle_summary(
    test_selection: dict[str, Any],
    test_results: list[dict[str, Any]],
) -> dict[str, Any]:
    plan = test_selection.get("targeted_oracle") if isinstance(test_selection.get("targeted_oracle"), dict) else {}
    test_id = str(plan.get("test_id") or "")
    result = next((item for item in test_results if str(item.get("id") or "") == test_id), None)
    status = str(plan.get("status") or "HOLD").upper()
    if status == "READY":
        if not isinstance(result, dict):
            status = "BLOCK"
            reason = "diff_targeted_oracle_result_missing"
        else:
            result_status = str(result.get("status") or "BLOCK").upper()
            status = result_status if result_status in {"PASS", "FAIL", "BLOCK", "HOLD"} else "HOLD"
            reason = {
                "PASS": "diff_targeted_product_test_oracle_passed",
                "FAIL": "diff_targeted_product_test_oracle_failed",
                "BLOCK": str(result.get("reason") or "diff_targeted_product_test_oracle_blocked"),
                "HOLD": str(result.get("reason") or "diff_targeted_product_test_oracle_held"),
            }.get(status, "diff_targeted_product_test_oracle_held")
    else:
        reason = str(plan.get("reason") or "diff_targeted_test_oracle_unavailable")
        status = "HOLD"
    return {
        "schema": DIFF_TARGETED_ORACLE_SCHEMA,
        "kind": "product_test_suite",
        "status": status,
        "reason": reason,
        "test_id": test_id or None,
        "test_files": list(plan.get("test_files", [])) if isinstance(plan.get("test_files"), list) else [],
        "changed_files": list(plan.get("changed_files", [])) if isinstance(plan.get("changed_files"), list) else [],
        "test_count": len(plan.get("test_files", [])) if isinstance(plan.get("test_files"), list) else 0,
        "result_path": result.get("stdout") if isinstance(result, dict) else None,
        "recorded_test_status": result.get("status") if isinstance(result, dict) else None,
    }


def _review_qa_outcome(matrix: dict[str, dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "HOLD") for item in matrix.values()}
    if "FAIL" in statuses:
        return "FAIL"
    if "BLOCK" in statuses:
        return "BLOCK"
    if statuses and statuses <= {"PASS", "NOT_APPLICABLE"}:
        return "PASS"
    return "HOLD"


def _diff_only_qa_report() -> dict[str, Any]:
    return {
        "schema": "quality-pilot.review-qa.v1",
        "mode": "diff_only",
        "status": "NOT_RUN",
        "generation": {"status": "NOT_RUN", "reason": "diff_only"},
        "cases": [],
        "matrix": {},
        "outcome": "NOT_RUN",
        "required_dimensions": [],
    }


def _review_recommendations(
    *,
    test_outcome: str,
    qa_outcome: str,
    product_test: dict[str, Any],
    matrix: dict[str, dict[str, Any]],
    case_results: list[dict[str, Any]],
    comprehensive: bool,
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []

    def add(
        recommendation_id: str,
        *,
        severity: str,
        category: str,
        status: str,
        recommendation: str,
        verification: str,
    ) -> None:
        recommendations.append(
            {
                "id": recommendation_id,
                "severity": severity,
                "category": category,
                "status": status,
                "recommendation": recommendation,
                "verification": verification,
            }
        )

    if test_outcome != "PASS":
        add(
            "regression-test-follow-up",
            severity="HIGH" if test_outcome in {"FAIL", "BLOCK"} else "MEDIUM",
            category="test-execution",
            status=test_outcome,
            recommendation=(
                "修復回歸測試失敗或測試環境/依賴問題後重新執行 review；不要把未執行的測試當成通過。"
                if test_outcome in {"FAIL", "BLOCK"}
                else "補足可執行的回歸測試並重新執行 review。"
            ),
            verification="targeted 與完整 regression command 均成功，且 evidence path 可追溯。",
        )

    product_status = str(product_test.get("status") or "NOT_RUN").upper()
    product_reason = str(product_test.get("reason") or "")
    if comprehensive and product_status != "PASS":
        if product_reason in {
            "product_test_contract_missing",
            "build_recipe_missing",
            "run_operation_missing",
            "artifact_path_missing",
            "build_artifact_missing",
        }:
            product_message = (
                "在 target repo 的 runtime.product_testing 提供 user-owned build_recipe、artifact_path、"
                "至少一個帶 semantic assertion 的 run_operations；若 README 命令要執行，另提供明確 allowlist。"
            )
            verification = "重新 review 時產生 artifact hash，並有實際產品操作的 semantic evidence。"
        elif product_reason == "product_testing_disabled":
            product_message = "Comprehensive review 需要產品驗證；啟用 product_testing，或明確改用 --diff-only，不要以停用設定冒充完整 review。"
            verification = "product build/run contract 實際執行並產生 semantic evidence。"
        elif product_status == "FAIL":
            product_message = "檢查 product build/run evidence 與實際產品行為；若需求正確，修復產品後重新執行 semantic oracle。"
            verification = "build 成功且產品操作 assertion 通過；不要只以 exit code 替代語意驗證。"
        elif product_status == "HOLD":
            product_message = "將目前 probe 補成明確的產品操作與 positive semantic assertion，避免 help/version/exit-only probe。"
            verification = "至少一個真實產品操作取得 PASS，並保存 stdout/stderr/rc 與 assertion evidence。"
        else:
            product_message = "補齊並確認產品 build/run contract 與執行環境，再重新執行 comprehensive review。"
            verification = "product binary contract 不再是 NOT_RUN/BLOCK/HOLD。"
        add(
            "product-test-contract",
            severity="HIGH",
            category="product-validation",
            status=product_status,
            recommendation=product_message,
            verification=verification,
        )

    for dimension, item in matrix.items():
        if dimension in {"white_box", "product_binary", "browser_ui", "documentation"} or not isinstance(item, dict):
            continue
        status = str(item.get("status") or "HOLD").upper()
        if status in {"PASS", "NOT_APPLICABLE"}:
            continue
        reason = str(item.get("reason") or "")
        if reason == "test_dependency_missing" and dimension == "functional":
            continue
        if dimension == "black_box":
            message = "新增產品專屬 black-box adapter（CLI/TUI/API/UI 依產品實際介面），並以 semantic assertion 驗證，不要用 generic probe 代替。"
            verification = "black_box adapter 在 pinned product artifact 上實際執行並產生可追溯 evidence。"
        elif dimension == "boundary":
            message = "補充產品邊界契約：空值、型別錯誤、範圍邊界、硬體/環境前置條件與明確 failure oracle。"
            verification = "boundary cases 實際執行，結果與預期狀態/錯誤語意一致。"
        elif dimension == "stress":
            message = "補充 bounded stress/timeout/soak contract，定義負載、時間上限、資源上限與可接受結果。"
            verification = "stress evidence 包含 duration、timeout/resource observations 與明確 oracle。"
        else:
            message = f"處理 {dimension} 維度的缺口（{reason or 'missing_or_incomplete_oracle'}），補上產品專屬 contract 與 evidence。"
            verification = f"{dimension} matrix status 變為 PASS，且 evidence 可追溯。"
        add(
            f"{dimension}-coverage",
            severity="HIGH" if dimension in {"black_box", "boundary"} else "MEDIUM",
            category="qa-coverage",
            status=status,
            recommendation=message,
            verification=verification,
        )

    partial_cases = [str(item.get("case_id") or "case") for item in case_results if item.get("partial_probe")]
    if partial_cases:
        add(
            "partial-probe-follow-up",
            severity="MEDIUM",
            category="case-oracle",
            status="HOLD",
            recommendation=f"將 partial probe case（{', '.join(partial_cases)}）改為完整產品操作，補上成功/失敗語意 oracle。",
            verification="case 不再標記 partial_probe，且結果由產品行為而非命令退出碼決定。",
        )

    if findings:
        add(
            "inline-finding-follow-up",
            severity="HIGH",
            category="code-review",
            status="FINDINGS",
            recommendation="逐一處理 inline finding 的修補建議，或由使用者在 Gitea review 中標記為接受風險並說明理由。",
            verification="每個 finding 有修補、明確 rationale 或 user-owned disposition。",
        )

    add(
        "human-review-decision",
        severity="INFO",
        category="human-gate",
        status="USER_DECISION_REQUIRED",
        recommendation="由使用者依報告與上述建議決定 Gitea 的 COMMENT、REQUEST_CHANGES 或 APPROVED；Quality Pilot 不自動代替批准。",
        verification="使用者明確確認最終 review state；本工具的 remote handoff 預設只建立 COMMENT。",
    )
    return recommendations


def _recommendation_next_actions(recommendations: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for item in recommendations:
        if not isinstance(item, dict) or item.get("id") == "human-review-decision":
            continue
        recommendation_id = str(item.get("id") or "")
        action = {
            "product-test-contract": "補上 runtime.product_testing contract（build_recipe、artifact_path、semantic run_operations）後重新執行 review",
            "black_box-coverage": "補上產品專屬 black-box adapter 與 semantic assertion",
            "boundary-coverage": "補上 boundary cases、硬體前置條件與 failure oracle",
            "stress-coverage": "補上 bounded stress/timeout/soak contract 與 resource oracle",
            "partial-probe-follow-up": "將 partial probe 改成實際產品操作與語意 assertion",
            "regression-test-follow-up": "處理 regression/test infrastructure 缺口後重新執行 review",
        }.get(recommendation_id)
        if action:
            actions.append(action)
    actions.append("由使用者決定 Gitea COMMENT、REQUEST_CHANGES 或 APPROVED；Quality Pilot 不代替批准")
    return actions


def _build_developer_review_report(
    *,
    findings: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    test_outcome: str,
    product_test_outcome: str,
    qa_outcome: str,
    matrix: dict[str, dict[str, Any]],
    test_results: list[dict[str, Any]],
) -> dict[str, Any]:
    actionable = [
        item for item in recommendations
        if isinstance(item, dict) and item.get("id") != "human-review-decision"
    ]
    must_fix: list[dict[str, Any]] = []
    should_fix: list[dict[str, Any]] = []
    nice_to_have: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "").upper()
        if severity in {"CRITICAL", "BLOCKER"} or item.get("blocking") is True:
            must_fix.append(item)
        else:
            should_fix.append(item)
    for item in actionable:
        severity = str(item.get("severity") or "INFO").upper()
        if severity in {"CRITICAL", "BLOCKER"}:
            must_fix.append(item)
        elif severity == "HIGH":
            should_fix.append(item)
        else:
            nice_to_have.append(item)

    def item_text(item: dict[str, Any]) -> str:
        return str(item.get("recommendation") or item.get("message") or item.get("body") or "").strip()

    def verification_text(item: dict[str, Any]) -> str:
        return str(item.get("verification") or item.get("recommendation") or "").strip()

    categories = {
        "security": 0,
        "maintainability": 0,
        "performance": 0,
        "style": 0,
    }
    for item in findings + actionable:
        category = str(item.get("category") or "").lower()
        if "security" in category:
            categories["security"] += 1
        elif "stress" in category or "performance" in category:
            categories["performance"] += 1
        elif "style" in category:
            categories["style"] += 1
        else:
            categories["maintainability"] += 1

    status_by_dimension = {
        key: str(value.get("status") or "UNKNOWN")
        for key, value in matrix.items()
        if isinstance(value, dict)
    }
    return {
        "schema": "quality-pilot.developer-code-review.v1",
        "decision": "REQUEST_CHANGES" if must_fix else "COMMENT",
        "decision_owner": "USER",
        "decision_note": "This is a user-owned recommendation. The remote handoff remains COMMENT and never grants merge permission.",
        "summary": {
            "total_issues": len(must_fix) + len(should_fix) + len(nice_to_have),
            "must_fix": len(must_fix),
            "should_fix": len(should_fix),
            "nice_to_have": len(nice_to_have),
            "findings": len(findings),
            "recommendations": len(actionable),
        },
        "evidence": {
            "test_results": [
                {
                    "id": item.get("id"),
                    "command": item.get("command"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "exit_code": item.get("exit_code"),
                    "timeout_seconds": item.get("timeout_seconds"),
                    "failed_tests": item.get("failed_tests", []),
                    "failure_details": item.get("failure_details", []),
                    "pytest_summary": item.get("pytest_summary"),
                    "stdout": item.get("stdout"),
                    "stderr": item.get("stderr"),
                    "reproduction": {
                        "steps": [
                            "Use the pinned review worktree.",
                            f"Run `{item.get('command')}`.",
                        ],
                        "expected": "The selected test command completes with exit code 0.",
                        "actual": f"status={item.get('status')}, reason={item.get('reason')}, exit_code={item.get('exit_code')}",
                        "evidence": [item.get("stdout"), item.get("stderr")],
                    },
                }
                for item in test_results
            ],
            "finding_evidence": [
                {
                    "id": item.get("id"),
                    "path": item.get("path"),
                    "line": item.get("line"),
                    "kind": (item.get("evidence") or {}).get("kind") if isinstance(item.get("evidence"), dict) else None,
                    "reproducibility": item.get("reproducibility"),
                }
                for item in findings
            ],
        },
        "impact_areas": {
            "security": categories["security"],
            "maintainability": categories["maintainability"],
            "performance": categories["performance"],
            "style": categories["style"],
        },
        "test_evidence": {
            "test_outcome": test_outcome,
            "product_test_outcome": product_test_outcome,
            "qa_outcome": qa_outcome,
            "matrix": status_by_dimension,
        },
        "sections": {
            "must_fix": must_fix,
            "should_fix": should_fix,
            "nice_to_have": nice_to_have,
            "verification": [
                {
                    "item": str(item.get("id") or "finding"),
                    "action": verification_text(item),
                }
                for item in must_fix + should_fix + nice_to_have
            ],
        },
        "localized_summary": {
            "en": {
                "must_fix": "None." if not must_fix else [item_text(item) for item in must_fix],
                "should_fix": "None." if not should_fix else [item_text(item) for item in should_fix],
                "nice_to_have": "None." if not nice_to_have else [item_text(item) for item in nice_to_have],
                "verification": [verification_text(item) for item in must_fix + should_fix + nice_to_have],
            },
            "zh-TW": {
                "must_fix": "無。" if not must_fix else [item_text(item) for item in must_fix],
                "should_fix": "無。" if not should_fix else [item_text(item) for item in should_fix],
                "nice_to_have": "無。" if not nice_to_have else [item_text(item) for item in nice_to_have],
                "verification": [verification_text(item) for item in must_fix + should_fix + nice_to_have],
            },
        },
    }


def _browser_evidence_records(
    qa_report: dict[str, Any] | None,
    test_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expose Browser/Playwright evidence as a first-class report section.

    The canonical case/result files remain the evidence authority.  This list
    is a redacted, report-facing index so engineers do not need to inspect the
    JSON case lineage just to find a screenshot or trace.
    """
    qa = qa_report if isinstance(qa_report, dict) else {}
    records: list[dict[str, Any]] = []
    related_product_screenshot: Any = None
    product = qa.get("product_test") if isinstance(qa.get("product_test"), dict) else {}
    browser = product.get("browser") if isinstance(product.get("browser"), dict) else {}
    if browser:
        evidence = browser.get("evidence") if isinstance(browser.get("evidence"), dict) else {}
        related_product_screenshot = evidence.get("screenshot")
        status = str(browser.get("status") or "NOT_RUN")
        records.append(
            {
                "id": str(browser.get("case_id") or f"{product.get('case_id', 'PRODUCT')}-BROWSER-UI"),
                "kind": "remote_product_browser",
                "title": "Remote product semantic Browser flow",
                "status": status,
                "reason": browser.get("reason"),
                "failure_type": browser.get("failure_type"),
                "execution_target": browser.get("execution_target") or product.get("execution_target") or "remote_ssh",
                "evidence_origin": browser.get("evidence_origin") or product.get("evidence_origin") or "remote",
                "interaction_count": browser.get("interaction_count", 0),
                "positive_assertion_count": browser.get("positive_assertion_count", 0),
                "state_assertion_count": browser.get("state_assertion_count", 0),
                "source_identity": browser.get("source_identity"),
                "remote_cleanup": browser.get("remote_cleanup"),
                "evidence": dict(evidence),
                "screenshot": evidence.get("screenshot"),
                "screenshot_sha256": evidence.get("screenshot_sha256"),
                "failure_screenshot": evidence.get("screenshot") if status in {"FAIL", "BLOCK"} else None,
            }
        )
    regression = qa.get("browser_regression_case") if isinstance(qa.get("browser_regression_case"), dict) else {}
    if regression:
        command = ((regression.get("commands") or [{}])[0] if isinstance(regression.get("commands"), list) else {})
        existing = next(
            (
                item
                for item in (test_results or [])
                if isinstance(item, dict) and item.get("id") == "diff-targeted-pytest"
            ),
            {},
        )
        records.append(
            {
                "id": regression.get("case_id", "PR-BROWSER-UI-REGRESSION"),
                "kind": "local_playwright_pytest_regression",
                "title": regression.get("title", "Local Playwright Browser regression suite"),
                "status": regression.get("status", "NOT_RUN"),
                "reason": regression.get("reason"),
                "failure_type": None,
                "execution_target": regression.get("execution_target", "local_pinned_worktree"),
                "evidence_origin": regression.get("evidence_origin", "local"),
                "command": command.get("command") if isinstance(command, dict) else None,
                "exit_code": command.get("exit_code") if isinstance(command, dict) else None,
                "timeout_seconds": existing.get("timeout_seconds"),
                "failed_tests": existing.get("failed_tests", []),
                "failure_details": existing.get("failure_details", []),
                "pytest_summary": existing.get("pytest_summary"),
                "evidence": {"stdout": (regression.get("evidence") or [None, None])[0] if isinstance(regression.get("evidence"), list) else None,
                             "stderr": (regression.get("evidence") or [None, None])[1] if isinstance(regression.get("evidence"), list) and len(regression.get("evidence") or []) > 1 else None},
                "screenshot": regression.get("screenshot"),
                "screenshot_sha256": regression.get("screenshot_sha256"),
                "failure_screenshot": regression.get("screenshot"),
                "related_product_screenshot": related_product_screenshot,
                "screenshot_note": "pytest regression does not expose a browser page screenshot; a related product Browser screenshot is listed separately when available.",
            }
        )
    return records


def _build_report(
    config: ProjectConfig,
    *,
    repo: str,
    pr_number: int,
    snapshot: dict[str, Any],
    snapshot_path: str,
    diff_hash: str,
    diff_info: dict[str, Any],
    worktree: dict[str, Any],
    test_selection: dict[str, Any],
    test_results: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    qa_report: dict[str, Any],
    dependency_preparation: dict[str, Any] | None = None,
    effective_execution_contract: dict[str, Any] | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    test_outcome = "NOT_RUN" if dry_run else ("HOLD" if not test_results else "PASS")
    if any(item.get("status") == "BLOCK" for item in test_results) or worktree.get("status") == "blocked":
        test_outcome = "BLOCK"
    elif any(item.get("status") == "FAIL" for item in test_results):
        test_outcome = "FAIL"
    matrix = qa_report.get("matrix") if isinstance(qa_report.get("matrix"), dict) else {}
    white_box = matrix.get("white_box") if isinstance(matrix.get("white_box"), dict) else {}
    if findings:
        white_box.update({"status": "FAIL", "reason": "deterministic_diff_finding", "finding_count": len(findings)})
    elif diff_info.get("status") != "PASS":
        white_box.update({"status": "BLOCK", "reason": diff_info.get("reason") or "diff_unavailable"})
    elif test_outcome in {"FAIL", "BLOCK", "HOLD", "NOT_RUN"}:
        white_box.update({"status": test_outcome, "reason": "regression_test_outcome"})
    if white_box:
        matrix["white_box"] = white_box
    targeted_oracle = _targeted_oracle_summary(test_selection, test_results)
    product_test = qa_report.get("product_test") if isinstance(qa_report.get("product_test"), dict) else {}
    product_test_outcome = str(product_test.get("status") or "NOT_RUN")
    browser_result = product_test.get("browser") if isinstance(product_test.get("browser"), dict) else {}
    browser_outcome = str(browser_result.get("status") or "NOT_RUN")
    remote_preflight = effective_execution_contract.get("remote_preflight") if isinstance(effective_execution_contract, dict) and isinstance(effective_execution_contract.get("remote_preflight"), dict) else {}
    preflight_status = str(remote_preflight.get("status") or "NOT_RUN")
    tooling_outcome = "TOOLING_FAIL" if preflight_status == "TOOLING_FAIL" else "NOT_RUN"
    infrastructure_outcome = preflight_status if preflight_status in {"INFRASTRUCTURE_BLOCK", "REMOTE_SOURCE_MISMATCH", "REMOTE_SOURCE_DIRTY", "REMOTE_SOURCE_UNVERIFIED"} else "NOT_RUN"
    if targeted_oracle["status"] in {"PASS", "FAIL", "BLOCK"}:
        matrix["functional"] = {
            "status": targeted_oracle["status"],
            "reason": targeted_oracle["reason"],
            "case_count": targeted_oracle.get("test_count", 0),
            "test_id": targeted_oracle.get("test_id"),
            "test_files": targeted_oracle.get("test_files", []),
        }
    matrix_outcome = _review_qa_outcome(matrix) if matrix else str(qa_report.get("outcome") or "NOT_RUN")
    qa_outcome = "BLOCK" if qa_report.get("outcome") == "BLOCK" else matrix_outcome
    if tooling_outcome == "TOOLING_FAIL" or infrastructure_outcome != "NOT_RUN":
        product_test_outcome = "NOT_EVALUATED"
        browser_outcome = "NOT_EVALUATED"
        qa_outcome = "UNASSESSED"
        matrix["product_binary"] = {
            "status": "NOT_RUN",
            "reason": "blocked_by_quality_pilot_tooling_failure" if tooling_outcome == "TOOLING_FAIL" else "blocked_by_remote_infrastructure_preflight",
        }
    qa_report["matrix"] = matrix
    qa_report["targeted_oracle"] = targeted_oracle
    qa_report["outcome"] = qa_outcome
    comprehensive = qa_report.get("mode") == "comprehensive"
    coverage_gap = bool(
        test_selection.get("coverage_gap")
        or test_selection.get("unavailable")
        or (comprehensive and qa_outcome != "PASS")
        or (comprehensive and product_test_outcome not in {"PASS", "NOT_RUN"})
    )
    if findings:
        conclusion = "REQUEST_CHANGES"
    elif tooling_outcome == "TOOLING_FAIL":
        conclusion = "QUALITY_PILOT_TOOLING_FAILURE_REQUIRES_REPAIR"
    elif infrastructure_outcome != "NOT_RUN":
        conclusion = "REMOTE_SOURCE_RECONCILIATION_REQUIRED" if infrastructure_outcome in {"REMOTE_SOURCE_MISMATCH", "REMOTE_SOURCE_DIRTY", "REMOTE_SOURCE_UNVERIFIED"} else "INFRASTRUCTURE_PREFLIGHT_REQUIRED"
    elif test_outcome == "FAIL":
        conclusion = "TEST_FAILURE_REQUIRES_TRIAGE"
    elif qa_outcome == "FAIL" and product_test_outcome == "FAIL":
        conclusion = "PRODUCT_TEST_FAILURE_REQUIRES_TRIAGE"
    elif qa_outcome == "FAIL":
        conclusion = "QA_MATRIX_FAILURE_REQUIRES_TRIAGE"
    elif product_test_outcome in {"BLOCK", "HOLD", "INTERRUPTED", "PLANNED"}:
        conclusion = "HOLD_FOR_PRODUCT_TEST_COVERAGE"
    elif test_outcome in {"BLOCK", "HOLD", "NOT_RUN"} or qa_outcome in {"BLOCK", "HOLD", "PLANNED", "NOT_RUN"} or coverage_gap:
        conclusion = "HOLD_FOR_TEST_COVERAGE"
    else:
        conclusion = "NO_BLOCKING_FINDINGS"
    recommendations = _review_recommendations(
        test_outcome=test_outcome,
        qa_outcome=qa_outcome,
        product_test=product_test,
        matrix=matrix,
        case_results=qa_report.get("cases", []) if isinstance(qa_report.get("cases"), list) else [],
        comprehensive=comprehensive,
        findings=findings,
    )
    developer_review = _build_developer_review_report(
        findings=findings,
        recommendations=recommendations,
        test_outcome=test_outcome,
        product_test_outcome=product_test_outcome,
        qa_outcome=qa_outcome,
        matrix=matrix,
        test_results=test_results,
    )
    browser_evidence = _browser_evidence_records(qa_report, test_results)
    product_test_evidence = {
        "status": product_test_outcome,
        "reason": product_test.get("reason"),
        "execution_target": product_test.get("execution_target"),
        "evidence_origin": product_test.get("evidence_origin"),
        "case_id": product_test.get("case_id"),
        "result_path": product_test.get("result_path") or product_test.get("case_result_path"),
        "build": product_test.get("build"),
        "browser_status": browser_result.get("status") if isinstance(browser_result, dict) else None,
    }
    return {
        "schema": REVIEW_SCHEMA,
        "report_locale": "zh-TW",
        "status": "ok" if worktree.get("status") in {"ready", "planned"} else "blocked",
        "repo": repo,
        "pr_number": pr_number,
        "title": snapshot.get("title"),
        "author": snapshot.get("author"),
        "pr_state": snapshot.get("state"),
        "pr_merged": bool(snapshot.get("merged") is True),
        "pr_updated_at": snapshot.get("updated_at"),
        "base_sha": snapshot.get("base_sha"),
        "base_ref": snapshot.get("base_ref"),
        "head_sha": snapshot.get("head_sha"),
        "head_ref": snapshot.get("head_ref"),
        "diff_hash": diff_hash,
        "diff_source": snapshot.get("diff_source"),
        "diff_reconstruction": diff_info,
        "snapshot_path": snapshot_path,
        "changed_files": snapshot.get("changed_files", []),
        "worktree": {key: value for key, value in worktree.items() if key != "source" or value},
        "test_selection": test_selection,
        "diff_targeted_oracle": targeted_oracle,
        "test_results": test_results,
        "test_outcome": test_outcome,
        "product_test_outcome": product_test_outcome,
        "browser_ui_outcome": browser_outcome,
        "browser_evidence": browser_evidence,
        "product_test_evidence": product_test_evidence,
        "tooling_outcome": tooling_outcome,
        "infrastructure_outcome": infrastructure_outcome,
        "product_evaluation_status": "NOT_EVALUATED" if tooling_outcome == "TOOLING_FAIL" or infrastructure_outcome != "NOT_RUN" else "EVALUATED",
        "findings": findings,
        "qa_review": qa_report,
        "qa_outcome": qa_outcome,
        "coverage_gap": coverage_gap,
        "conclusion": conclusion,
        "recommendation": conclusion,
        "review_decision": "USER_DECISION_REQUIRED",
        "recommendations": recommendations,
        "next_actions": _recommendation_next_actions(recommendations),
        "developer_review": developer_review,
        "residual_risk": ["Test coverage gap must be reviewed before treating this report as approval."] if coverage_gap else [],
        "comprehensive_review": comprehensive,
        "dependency_preparation": dependency_preparation or {"status": "NOT_RUN"},
        "effective_execution_contract": effective_execution_contract or {},
        "execution_targets": {
            "local_review_worktree": bool((effective_execution_contract or {}).get("execution", {}).get("local_review_worktree", True)),
            "local_pytest": bool((effective_execution_contract or {}).get("execution", {}).get("local_pytest", True)),
            "local_python": (dependency_preparation or {}).get("python", ".venv/bin/python"),
            "local_execution_target": (dependency_preparation or {}).get("execution_target", "local_disposable_review_worktree"),
            "local_evidence_origin": (dependency_preparation or {}).get("evidence_origin", "local"),
            "remote_pytest": "NOT_RUN",
            "remote_pytest_evidence_origin": "remote_separate",
            "product_target": (effective_execution_contract or {}).get("execution", {}).get("product_target", "local"),
            "playwright_target": (effective_execution_contract or {}).get("execution", {}).get("playwright_target", "local"),
            "evidence_origin_policy": "local_and_remote_explicit",
        },
        "remote_preflight": ((effective_execution_contract or {}).get("remote_preflight") if isinstance((effective_execution_contract or {}).get("remote_preflight"), dict) else None),
        "dry_run": dry_run,
        "generated_at": utc_now(),
    }


def _secret_findings_for_line(path: str, line: int, text: str, *, diff_hash: str = "", head_sha: str = "", repo: str = "", pr_number: int = 0) -> list[dict[str, Any]]:
    safe_context, _ = _redact_output(text)
    return [
        {
            "id": f"secret-{_sha256(f'{path}:{line}:{finding.kind}')[:12]}",
            "severity": "CRITICAL",
            "category": "security",
            "blocking": True,
            "path": path or None,
            "line": line or None,
            "symbol": None,
            "code_context": safe_context,
            "message": "The added line contains secret-like material and must be removed or referenced through an environment variable.",
            "evidence": {"kind": finding.kind},
            "recommendation": "Remove the raw value and use an approved environment-variable reference or redacted fixture.",
            "reproducibility": {
                "status": "REPRODUCIBLE_FROM_DETERMINISTIC_INPUT",
                "deterministic_input": {
                    "diff_hash": diff_hash,
                    "head_sha": head_sha,
                    "repo": repo,
                    "pr_number": pr_number,
                    "file_path": path,
                    "line_number": line,
                    "pattern_name": finding.kind,
                },
                "steps": [
                    f"Checkout the pinned PR head `{head_sha}`.",
                    f"Run the review against `{repo}#{pr_number}` with the same pinned head.",
                    f"Verify the finding is reported at `{path}:{line}` with kind `{finding.kind}`.",
                ],
                "expected": "The same file and line are reported as credential_assignment.",
                "actual": "Quality Pilot detected secret-like material; the raw value is intentionally redacted.",
                "evidence": "deterministic diff scan; finding evidence kind=credential_assignment",
            },
        }
        for finding in find_secret_text(text, path=path or "diff")
    ]


def _redact_output(text: str) -> tuple[str, list[dict[str, str]]]:
    safe, findings = redact_structure(str(text or ""), prefix="test_output")
    return str(safe), [item.as_dict() for item in findings]


def _render_markdown(report: dict[str, Any]) -> str:
    """Render the one canonical Markdown report for disk and the PR body.

    The report is redacted before rendering, but it is not summarized or
    reformatted for Gitea.  This guarantees that the local ``.md`` report and
    the main PR message are byte-for-byte equivalent for the same report
    snapshot.  Inline review comments are separate annotations, not a second
    report.
    """
    safe_report, _ = redact_structure(report, prefix="review_report_markdown")
    if not isinstance(safe_report, dict):
        raise ReviewError("review_report_markdown_redaction_failed_closed")
    return _render_detailed_text(safe_report)

def _zh_value(value: Any) -> str:
    mapping = {
        "CRITICAL": "嚴重",
        "HIGH": "高",
        "MEDIUM": "中",
        "LOW": "低",
        "INFO": "資訊",
        "PASS": "通過",
        "FAIL": "失敗",
        "BLOCK": "阻擋",
        "HOLD": "暫緩",
        "NOT_RUN": "未執行",
        "NOT_EVALUATED": "未評估",
        "PLANNED": "已規劃但未執行",
        "UNKNOWN": "未知",
        "READY": "就緒",
        "TOOLING_FAIL": "工具失敗",
        "HUMAN_GATE_REQUIRED": "等待使用者決定",
        "BLOCKED": "Quality Pilot 建議阻擋（不是 Gitea merge gate）",
        "ADVISORY": "建議",
        "security": "安全性",
        "test-execution": "測試執行",
        "product-validation": "產品驗證",
        "qa-coverage": "品質保證覆蓋",
        "case-oracle": "測試案例判定規則",
        "code-review": "程式碼審查",
        "human-gate": "人工決策閘門",
        "COMMENT": "留言審查",
        "REQUEST_CHANGES": "要求修改",
        "APPROVED": "核准",
        "PASS": "通過",
        "FAIL": "失敗",
        "BLOCK": "阻擋",
        "HOLD": "暫緩",
        "NOT_RUN": "未執行",
    }
    if value is None:
        return "未記錄"
    text = str(value)
    return mapping.get(text, text)


def _detailed_reason(item: dict[str, Any]) -> str:
    reasons = {
        "regression-test-follow-up": "測試沒有完成，無法確認修改後仍通過既有回歸測試；依賴缺口也可能掩蓋真實的回歸問題。",
        "product-test-contract": "沒有建置產物與產品語意操作，只能證明部分測試程式，不能證明使用者實際操作的產品行為。",
        "boundary-coverage": "未涵蓋異常輸入、範圍與前置條件，實際產品可能出現未定義行為。",
        "black_box-coverage": "單元測試通過不代表從產品入口執行時的整體行為正確。",
        "stress-coverage": "沒有負載、逾時與資源判定規則，無法判斷卡死、資源耗盡或效能退化行為。",
        "partial-probe-follow-up": "部分探針只能證明命令可執行，不能證明產品輸出或狀態正確。",
        "inline-finding-follow-up": "每個問題可能有不同風險與修補範圍，必須逐項處理或留下可追溯的接受風險理由。",
    }
    return reasons.get(str(item.get("id") or ""), "此缺口會降低 code review 的可驗證性或增加產品風險。")


def _status_for_engineer(status: Any, *, reason: str = "", exit_code: Any = None) -> str:
    value = str(status or "UNKNOWN").upper()
    if value == "PASS":
        return "通過（已實際執行）"
    if value == "FAIL":
        return "失敗（已實際執行，請修復後重跑）"
    if value == "BLOCK" and (reason == "test_command_timeout" or exit_code == 124):
        return "逾時（已開始但未完成，不能判定 PASS）"
    if value == "BLOCK":
        return "阻擋（前置條件、工具或命令未完成）"
    if value == "HOLD":
        return "暫緩（證據或 oracle 不足）"
    if value in {"NOT_RUN", "NOT_EVALUATED"}:
        return "未執行／未評估"
    return value


def _engineer_reason(status: Any, reason: Any, exit_code: Any = None) -> str:
    value = str(status or "UNKNOWN").upper()
    detail = str(reason or "").strip()
    if detail == "test_command_timeout" or exit_code == 124:
        return "測試程序已啟動，但在時間上限內沒有完成；這不是缺少依賴，也不能直接當成產品缺陷。"
    if detail == "test_command_failed":
        return "pytest 已實際執行並回報失敗；請先看失敗測試名稱與標準輸出／錯誤輸出，再修復產品或測試。"
    if detail == "browser_probe_only_no_semantic_state_assertion":
        return "Browser 只驗證頁面／元素可見，沒有使用者互動與狀態斷言；因此只能 HOLD，不能宣稱 UI/UX PASS。"
    if detail == "remote_product_build_adapter_not_supported":
        return "遠端產品建置 adapter 尚未實作；這是產品建置尚未評估，不是產品建置失敗。"
    if detail == "browser_prerequisites_absent":
        return "Browser client 缺少 Python Playwright 或 Chromium；這是 Quality Pilot／環境阻擋，不是產品失敗。"
    if detail == "dependency_install_failed":
        return "依賴安裝失敗；先修復審查工作樹的 venv，再重跑測試。"
    if detail == "diff_targeted_product_test_oracle_failed":
        return "變更檔案對應的 targeted pytest 已執行但失敗；功能維度不能判定通過。"
    if detail == "regression_test_outcome":
        return "本地回歸沒有通過；白箱結果不能判定通過。"
    if detail == "missing_or_incomplete_oracle":
        return "有執行候選檢查，但缺少足夠的產品專屬 oracle；不能用通用探針代替。"
    if detail == "product_black_box_adapter_not_proven":
        return "尚未用產品入口完成可追溯的黑箱 adapter；不能用單元／回歸通過代替。"
    if detail == "no_boundary_case":
        return "目前沒有邊界／無效輸入案例；這是覆蓋缺口，不是產品通過。"
    if detail == "no_stress_case":
        return "目前沒有受限時間與資源的壓力案例；這是覆蓋缺口，不是產品通過。"
    if detail == "changed_documentation_read_and_parsed":
        return "已實際讀取並解析變更文件；這只能代表文件檢查通過，不代表產品行為全部通過。"
    if detail == "diff_targeted_product_test_oracle_passed":
        return "changed-file 對應的 targeted pytest 已完成且 oracle 通過。"
    if detail == "browser_semantic_interaction_passed":
        return "真實 Playwright 互動與語意狀態斷言都通過。"
    if detail == "playwright_actionability_timeout":
        return "Playwright 找到元素，但元素在測試 timeout 內沒有達到可安全互動的穩定狀態。"
    if detail == "browser_server_startup_or_url_timeout":
        return "產品 Browser server 在 startup deadline 內沒有輸出可連線 URL。"
    if detail == "review_worktree_path_assumption":
        return "測試把 checkout 目錄名稱寫死為產品名稱；pinned review worktree 應使用 PR 專屬目錄，測試應改用 repo root/config，而不是 basename。"
    if value == "NOT_EVALUATED":
        return "上游 tooling 或 infrastructure gate 阻擋，因此本項沒有形成產品結論。"
    if detail:
        return detail
    if value == "PASS":
        return "命令完成且 oracle 通過。"
    if value == "FAIL":
        return "命令完成但 oracle 或測試斷言失敗。"
    if value == "HOLD":
        return "目前證據不足以支持通過或產品缺陷結論。"
    return "需要查看執行證據後才能決定下一步。"


def _engineer_reproduction_steps(item: dict[str, Any], report: dict[str, Any]) -> list[str]:
    item_id = str(item.get("id") or "")
    if item_id == "regression-test-follow-up":
        results = report.get("test_results") if isinstance(report.get("test_results"), list) else []
        steps = [
            "在同一個 local disposable review worktree 執行下方測試命令，不要改用 host repo 或 /usr/bin/python3。",
        ]
        for result in results:
            if isinstance(result, dict) and result.get("command"):
                steps.append(f"執行 `{result.get('command')}`；目前結果={result.get('status')}，exit={result.get('exit_code', '未記錄')}。")
        steps.append("先修復第一個失敗測試或增加 test timeout，再用相同命令重跑；不要以未完成的 full suite 推定 PASS。")
        return steps
    if item_id == "product-test-contract":
        qa = report.get("qa_review") if isinstance(report.get("qa_review"), dict) else {}
        product = qa.get("product_test") if isinstance(qa.get("product_test"), dict) else {}
        browser = product.get("browser") if isinstance(product.get("browser"), dict) else {}
        return [
            "確認 product build/operation 與 Browser UI 是兩個獨立 case。",
            f"目前 Browser 結果={browser.get('status', '未記錄')}；原因={browser.get('reason', '未記錄')}。",
            "以產品專屬 semantic workflow 執行 tab、欄位、validation 或 workflow state assertion；body/button visibility 只能是前置 probe。",
        ]
    if item_id.endswith("-coverage"):
        dimension = item_id.removesuffix("-coverage")
        matrix = (report.get("qa_review") or {}).get("matrix", {}) if isinstance(report.get("qa_review"), dict) else {}
        observation = matrix.get(dimension) if isinstance(matrix, dict) and isinstance(matrix.get(dimension), dict) else {}
        return [
            f"查看 QA matrix 的 `{dimension}` row；目前狀態={observation.get('status', item.get('status', '未記錄'))}，原因={observation.get('reason', '未記錄')}。",
            f"補上 `{dimension}` 專屬 case、oracle 與 evidence，不要用其他維度的 PASS 代替。",
            "修補後只重跑這個維度及其受影響的 regression，確認 result/case/evidence lineage 完整。",
        ]
    return [
        "查看本報告的 QA matrix 與該項 evidence。",
        "執行建議的產品專屬 case，確認 expected/actual/oracle 都有明確結果。",
    ]


def _quality_pilot_recommendation(report: dict[str, Any]) -> str:
    test_outcome = str(report.get("test_outcome") or "NOT_RUN").upper()
    qa_outcome = str(report.get("qa_outcome") or "NOT_RUN").upper()
    product_outcome = str(report.get("product_test_outcome") or "NOT_RUN").upper()
    browser_outcome = str(report.get("browser_ui_outcome") or "NOT_RUN").upper()
    if test_outcome == "FAIL" or qa_outcome == "FAIL":
        return "暫不建議合併：本地回歸或品質保證檢查有已執行但失敗的結果，請先完成分類與修復。"
    if product_outcome in {"FAIL", "BLOCK", "HOLD"} or browser_outcome in {"FAIL", "BLOCK", "HOLD"}:
        return "暫不建議合併：產品或 Browser 證據尚未形成完整通過結果。"
    if test_outcome in {"BLOCK", "HOLD", "NOT_RUN"} or qa_outcome in {"BLOCK", "HOLD", "NOT_RUN"}:
        return "暫不建議合併：測試證據尚未完整。"
    return "可以考慮合併：目前沒有發現阻擋性失敗；最終決定仍由 PR 擁有者做出。"


def _report_browser_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    records = report.get("browser_evidence") if isinstance(report.get("browser_evidence"), list) else None
    if records is None:
        qa = report.get("qa_review") if isinstance(report.get("qa_review"), dict) else {}
        records = _browser_evidence_records(qa, report.get("test_results") if isinstance(report.get("test_results"), list) else [])
    else:
        records = list(records)
    supplemental = report.get("supplemental_browser_evidence") if isinstance(report.get("supplemental_browser_evidence"), list) else []
    return [item for item in [*records, *supplemental] if isinstance(item, dict)]


def _browser_record_title(record: dict[str, Any]) -> str:
    kind = str(record.get("kind") or "")
    if kind == "remote_product_browser":
        return "遠端產品語意 Browser 流程"
    if kind == "local_playwright_pytest_regression":
        return "本地 Playwright pytest 回歸套件"
    if kind == "supplemental_manual_playwright":
        return "補充的遠端人工 Playwright 驗證"
    if kind == "supplemental_manual_local_playwright_regression":
        return "補充的本地 Playwright 回歸重跑"
    return str(record.get("title") or record.get("kind") or "Browser 測試")


def _render_browser_evidence(report: dict[str, Any]) -> list[str]:
    records = _report_browser_records(report)
    lines = ["", "Playwright／產品測試執行證據", "-" * 80]
    if any(item.get("kind") == "supplemental_manual_playwright" and item.get("status") == "PASS" for item in records) and any(item.get("kind") == "remote_product_browser" and item.get("status") == "HOLD" for item in records):
        lines.extend([
            "判讀說明：正式審查的 Browser case 使用原始 smoke contract，因此是暫緩；補充的人工執行 case 使用已確認的語意 workflow，因此是獨立通過，兩者不是同一次執行。",
            "",
        ])
    if any(item.get("kind") == "supplemental_manual_local_playwright_regression" for item in records) and any(item.get("kind") == "local_playwright_pytest_regression" and item.get("status") == "BLOCK" for item in records):
        lines.extend([
            "判讀說明：正式審查的本地 Browser 命令在舊 timeout 邊界回報阻擋；後續補充的完整重跑已完成並回報失敗，應以補充重跑的失敗診斷進行分類與修復。",
            "",
        ])
    product_evidence = report.get("product_test_evidence") if isinstance(report.get("product_test_evidence"), dict) else {}
    if product_evidence:
        lines.extend([
            "產品建置／操作案例：",
            f"  結果：{_status_for_engineer(product_evidence.get('status'), reason=product_evidence.get('reason'))}",
            f"  實際意思：{_engineer_reason(product_evidence.get('status'), product_evidence.get('reason'))}",
            f"  執行位置：{product_evidence.get('execution_target', '未記錄')}；證據來源：{product_evidence.get('evidence_origin', '未記錄')}",
            f"  建置結果：{_zh_value((product_evidence.get('build') or {}).get('status', '未記錄'))}；原因代碼={(product_evidence.get('build') or {}).get('reason', '未記錄')}",
            f"  案例證據：{product_evidence.get('result_path') or '未建立'}",
            "",
        ])
    if not records:
        lines.append("沒有 Browser/Playwright evidence record；不能由缺少 record 推定 PASS。")
        return lines
    for record in records:
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        reason = str(record.get("reason") or "")
        lines.extend([
            f"Browser 案例：{record.get('id', '未命名')}（{_browser_record_title(record)}）",
            f"  結果：{_status_for_engineer(status, reason=reason, exit_code=record.get('exit_code'))}",
            f"  實際意思：{_engineer_reason(status, reason, record.get('exit_code'))}",
            f"  執行位置：{record.get('execution_target', '未記錄')}；證據來源：{record.get('evidence_origin', '未記錄')}",
        ])
        if record.get("command"):
            lines.append(f"  命令：{record.get('command')}")
        if record.get("exit_code") is not None:
            lines.append(f"  exit code：{record.get('exit_code')}")
        if record.get("timeout_seconds") is not None:
            lines.append(f"  timeout：{record.get('timeout_seconds')} 秒")
        if record.get("interaction_count") is not None:
            lines.append(
                f"  互動次數：{record.get('interaction_count')}；正向斷言：{record.get('positive_assertion_count', 0)}；狀態斷言：{record.get('state_assertion_count', 0)}"
            )
        if record.get("failure_type"):
            lines.append(f"  失敗類型：{record.get('failure_type')}")
        if record.get("pytest_summary"):
            lines.append(f"  pytest 摘要：{record.get('pytest_summary')}")
        failed_tests = record.get("failed_tests") if isinstance(record.get("failed_tests"), list) else []
        if failed_tests:
            lines.append(f"  失敗測試：{'; '.join(str(item) for item in failed_tests[:20])}")
        failure_details = record.get("failure_details") if isinstance(record.get("failure_details"), list) else []
        for detail in failure_details[:20]:
            if not isinstance(detail, dict):
                continue
            lines.extend([
                f"  失敗診斷：{detail.get('test', '未命名')}",
                f"    分類：{detail.get('category', '未分類')}",
                f"    位置：{detail.get('location') or '未擷取'}",
                f"    觀察：{detail.get('observed') or '未擷取'}",
                f"    實際錯誤：{detail.get('error') or '未擷取'}",
                f"    可複製單測試命令：{detail.get('reproduce') or '未擷取'}",
            ])
        source_identity = record.get("source_identity") if isinstance(record.get("source_identity"), dict) else {}
        if source_identity:
            lines.append(
                f"  remote source：{source_identity.get('status', '未記錄')}；HEAD={source_identity.get('observed_head_sha', '未記錄')}；clean={source_identity.get('dirty', '未記錄')}"
            )
        evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
        screenshot = record.get("screenshot") or evidence.get("screenshot")
        if screenshot:
            lines.append(f"  screenshot：{screenshot}{'（失敗截圖）' if record.get('failure_screenshot') else ''}")
            if record.get("screenshot_sha256"):
                lines.append(f"  screenshot SHA-256：{record.get('screenshot_sha256')}")
        elif str(status or "").upper() in {"FAIL", "BLOCK"}:
            lines.append(f"  失敗截圖：未建立；{record.get('screenshot_note') or '執行在建立 Browser page 前已失敗，不能偽造截圖。'}")
            if record.get("related_product_screenshot"):
                lines.append(f"  相關產品 Browser screenshot：{record.get('related_product_screenshot')}（不是 local pytest failure 的同一個 page session）")
        for key in ("trace", "dom", "interaction", "diagnostics", "console", "network", "server_stdout", "server_stderr", "remote_server_stdout", "remote_server_stderr"):
            value = evidence.get(key)
            if value and key != "screenshot":
                lines.append(f"  {key} evidence：{value}")
        if record.get("remote_cleanup"):
            lines.append(f"  remote cleanup：{record.get('remote_cleanup')}")
        lines.append("")
    return lines


def _render_engineer_execution_summary(report: dict[str, Any]) -> list[str]:
    """Render the first-screen, action-oriented review explanation."""
    lines = ["", "工程師快速判讀（先看這裡）", "-" * 80]
    gate = report.get("review_gate") if isinstance(report.get("review_gate"), dict) else {}
    conclusion = str(report.get("conclusion") or "UNKNOWN")
    lines.extend([
        f"結論：{conclusion}",
        f"Quality Pilot 建議：{_quality_pilot_recommendation(report)}",
        "PR 合併決定：由 PR 擁有者決定；Quality Pilot 只提供建議與留言，不執行 merge，也不代替你的 gate。",
        "重要規則：PASS 只代表該項命令和 oracle 實際通過；BLOCK/HOLD/逾時都不能當成 PASS。",
        "分類規則：測試 FAIL 是已執行的失敗；BLOCK 是未完成或前置／工具阻擋；HOLD 是證據不足，不直接指控產品有缺陷。",
        "",
        "實際執行的檢查結果",
        "-" * 80,
    ])
    test_results = report.get("test_results") if isinstance(report.get("test_results"), list) else []
    if test_results:
        for item in test_results:
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason") or "")
            status = item.get("status")
            command = str(item.get("command") or "未記錄")
            if len(command) > 420:
                command = command[:420] + " …"
            evidence = [str(item.get(key)) for key in ("stdout", "stderr") if item.get(key)]
            lines.extend([
                f"測試：{item.get('id', '未命名')}",
                f"  結果：{_status_for_engineer(status, reason=reason, exit_code=item.get('exit_code'))}",
                f"  命令：{command}",
                f"  執行位置：{item.get('execution_target', 'local_disposable_review_worktree')}；證據來源：{item.get('evidence_origin', 'local')}",
                f"  時間上限：{item.get('timeout_seconds', '未記錄')}{' 秒' if item.get('timeout_seconds') is not None else ''}；exit code：{item.get('exit_code', '未記錄')}",
                f"  實際意思：{_engineer_reason(status, reason, item.get('exit_code'))}",
                f"  pytest 摘要：{item.get('pytest_summary') or '未擷取'}",
                f"  失敗測試：{'; '.join(str(value) for value in (item.get('failed_tests') or [])[:12]) or '未擷取'}",
                f"  證據：{', '.join(evidence) if evidence else '未建立'}",
                f"  下一步：{'增加 --test-timeout 或拆分 targeted/full suite 後重跑' if reason == 'test_command_timeout' else '查看失敗測試與 stdout/stderr，修復後用相同命令重跑' if str(status).upper() == 'FAIL' else '先解除阻擋條件，再重跑本項'}",
                "",
            ])
    else:
        lines.extend(["沒有 local regression result；不能由沒有結果推定通過。", ""])

    preflight = report.get("remote_preflight") if isinstance(report.get("remote_preflight"), dict) else {}
    identity = preflight.get("source_identity") if isinstance(preflight.get("source_identity"), dict) else {}
    lines.extend([
        "遠端來源／執行環境",
        f"  preflight：{preflight.get('status', 'NOT_RUN')}",
        f"  expected HEAD：{identity.get('expected_head_sha', '未記錄')}",
        f"  observed HEAD：{identity.get('observed_head_sha', '未記錄')}",
        f"  worktree clean：{identity.get('dirty', '未記錄')}",
        f"  判讀：{'remote source identity 已驗證，可看產品／Browser 結果' if preflight.get('status') == 'READY' and identity.get('status') == 'VERIFIED' else 'remote source 或環境尚未達到 official evidence 條件'}",
        "",
    ])

    qa = report.get("qa_review") if isinstance(report.get("qa_review"), dict) else {}
    product = qa.get("product_test") if isinstance(qa.get("product_test"), dict) else {}
    browser = product.get("browser") if isinstance(product.get("browser"), dict) else {}
    if product or browser:
        lines.extend([
            "產品／Browser",
            f"  product build/operation：{_status_for_engineer(product.get('status'), reason=product.get('reason'))}",
            f"  product 判讀：{_engineer_reason(product.get('status'), product.get('reason'))}",
            f"  Browser：{_status_for_engineer(browser.get('status'), reason=browser.get('reason'))}",
            f"  Browser 判讀：{_engineer_reason(browser.get('status'), browser.get('reason'))}",
            f"  Browser 執行位置：{browser.get('execution_target', product.get('execution_target', '未記錄'))}；證據來源：{browser.get('evidence_origin', product.get('evidence_origin', '未記錄'))}",
            "  下一步：將已確認的產品專屬 semantic workflow（tab、欄位、validation、workflow state）加入 contract，再重新執行；body/button probe 不能當成 UI/UX PASS。",
            "",
        ])

    matrix = qa.get("matrix") if isinstance(qa.get("matrix"), dict) else {}
    if matrix:
        lines.extend(["品質保證矩陣（每一列都是獨立判定）", "-" * 80])
        for dimension, item in matrix.items():
            if not isinstance(item, dict):
                continue
            lines.append(f"- {dimension}: {_status_for_engineer(item.get('status'), reason=item.get('reason'))}；{_engineer_reason(item.get('status'), item.get('reason'))}")
        lines.append("")
    return lines


def _case_title_zh(case: dict[str, Any]) -> str:
    case_type = str(case.get("case_type") or "")
    if case_type in {"product", "product_build_and_semantic_operation"}:
        return "產品建置與語意操作"
    if case_type in {"playwright_ui", "playwright_ui_regression"}:
        return "Browser UI／UX Playwright 回歸"
    return str(case.get("title") or "未命名測試案例")


def _qa_dimension_label(dimension: str) -> str:
    return {
        "black_box": "黑箱／產品入口",
        "white_box": "白箱／程式與回歸",
        "functional": "功能行為",
        "boundary": "邊界與無效輸入",
        "stress": "壓力、逾時與資源",
        "documentation": "文件與契約",
        "product_binary": "產品建置與產物",
        "browser_ui": "Browser UI",
        "ui": "UI 互動",
        "ux": "UX／可用性",
    }.get(dimension, dimension)


def _qa_matrix_command(report: dict[str, Any], dimension: str, item: dict[str, Any]) -> str:
    test_results = report.get("test_results") if isinstance(report.get("test_results"), list) else []
    wanted = "regression-pytest" if dimension == "white_box" else "diff-targeted-pytest" if dimension == "functional" else None
    if dimension in {"ui", "ux"}:
        wanted = "diff-targeted-pytest"
    if wanted:
        result = next((value for value in test_results if isinstance(value, dict) and value.get("id") == wanted), None)
        if isinstance(result, dict) and result.get("command"):
            return str(result.get("command"))
    if dimension == "product_binary":
        product = (report.get("qa_review") or {}).get("product_test", {}) if isinstance(report.get("qa_review"), dict) else {}
        plan = product.get("plan") if isinstance(product, dict) and isinstance(product.get("plan"), dict) else {}
        recipe = plan.get("build_recipe") if isinstance(plan.get("build_recipe"), list) else []
        return "；".join(str(value) for value in recipe) if recipe else "未配置產品建置命令"
    if dimension == "documentation":
        files = item.get("files") if isinstance(item.get("files"), list) else []
        return "讀取並解析變更文件：" + ", ".join(str(value) for value in files) if files else "讀取變更文件"
    return "未建立可執行命令"


def _qa_matrix_evidence(item: dict[str, Any]) -> list[str]:
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        return [str(value) for value in evidence if value]
    if isinstance(evidence, dict):
        return [f"{key}={value}" for key, value in evidence.items() if value]
    return []


def _render_complete_qa_matrix(report: dict[str, Any]) -> list[str]:
    qa = report.get("qa_review") if isinstance(report.get("qa_review"), dict) else {}
    matrix = qa.get("matrix") if isinstance(qa.get("matrix"), dict) else {}
    lines = ["", "完整品質保證矩陣（包含非 UI／UX 測試）", "=" * 80]
    lines.extend([
        "這一節是 code review 的完整驗證範圍；UI／UX 只是其中兩個維度。",
        "未執行、證據不足或 adapter 尚未支援的維度，不會被描述為通過。",
        "",
    ])
    order = ("white_box", "functional", "black_box", "boundary", "stress", "documentation", "product_binary", "browser_ui", "ui", "ux")
    for dimension in order:
        item = matrix.get(dimension) if isinstance(matrix.get(dimension), dict) else {}
        status = item.get("status", "NOT_RUN")
        reason = str(item.get("reason") or "")
        status_text = _status_for_engineer(status, reason=reason)
        executed = "是" if str(status).upper() in {"PASS", "FAIL"} else "否／未形成完整結果"
        evidence = _qa_matrix_evidence(item)
        command = _qa_matrix_command(report, dimension, item)
        lines.extend([
            "",
            f"維度：{_qa_dimension_label(dimension)}（`{dimension}`）",
            f"  結果：{status_text}",
            f"  本次是否實際執行：{executed}",
            f"  判讀：{_engineer_reason(status, reason)}",
            f"  原因代碼：`{reason or '未記錄'}`",
            f"  可複製命令／方法：{command}",
            f"  證據：{'; '.join(evidence) if evidence else '未建立；不能由缺少證據推定通過'}",
            f"  case 數量：{item.get('case_count', '未記錄')}",
        ])
        if item.get("test_files"):
            lines.append(f"  涉及測試檔案：{', '.join(str(value) for value in item.get('test_files', []))}")
        if dimension in {"boundary", "stress", "black_box"} and str(status).upper() in {"HOLD", "BLOCK", "NOT_RUN"}:
            lines.append("  補足方式：建立產品專屬 case、expected/actual、failure oracle 與 evidence，再單獨重跑此維度。")
    cases = qa.get("cases") if isinstance(qa.get("cases"), list) else []
    lines.extend(["", "生成／執行的 case 清單", "-" * 80])
    if not cases:
        lines.append("沒有生成 case；這不是 PASS。")
    for case in cases:
        if not isinstance(case, dict):
            continue
        case_status = case.get("truth_status") or case.get("status")
        lines.extend([
            f"- `{case.get('case_id', '未命名')}`：{_case_title_zh(case)}",
            f"  - 結果：{_status_for_engineer(case_status, reason=case.get('reason'))}",
            f"  - 原始命令結果：{_status_for_engineer(case.get('status'), reason=case.get('reason'))}",
            f"  - 維度：{', '.join(str(value) for value in case.get('dimensions', [])) or '未記錄'}",
            f"  - 執行位置：{case.get('execution_target', '未記錄')}；證據來源：{case.get('evidence_origin', '未記錄')}",
            f"  - result：{case.get('result_path', '未建立')}",
            f"  - evidence：{', '.join(str(value) for value in case.get('evidence', []) if value) or '未建立'}",
        ])
    return lines


def _render_reproduction_playbook(report: dict[str, Any]) -> list[str]:
    worktree = (report.get("worktree") or {}).get("path") if isinstance(report.get("worktree"), dict) else None
    worktree = str(worktree or "$REVIEW_WORKTREE")
    test_results = report.get("test_results") if isinstance(report.get("test_results"), list) else []
    lines = ["", "工程師可直接複製的重現手冊", "=" * 80]
    lines.extend([
        "以下命令使用 pinned review worktree 的 `.venv/bin/python`；不要改用 Quality Pilot checkout、產品主 repo venv 或 `/usr/bin/python3`。",
        "",
        "### 0. 進入正確的審查工作樹",
        "```bash",
        f"cd \"{worktree}\"",
        "test -x .venv/bin/python",
        "```",
    ])
    if test_results:
        lines.extend(["", "### 1. 執行本次 review 選取的測試", ""])
        for item in test_results:
            if not isinstance(item, dict) or not item.get("command"):
                continue
            lines.extend([
                f"#### {item.get('id', 'test')}",
                f"目前結果：{_status_for_engineer(item.get('status'), reason=item.get('reason'), exit_code=item.get('exit_code'))}",
                "```bash",
                str(item.get("command")),
                "```",
                f"預期：exit code 0。實際：exit code {item.get('exit_code', '未記錄')}；原因={item.get('reason', '未記錄')}。",
            ])
            details = item.get("failure_details") if isinstance(item.get("failure_details"), list) else []
            for detail in details[:20]:
                if isinstance(detail, dict):
                    lines.extend([
                        f"- 失敗：`{detail.get('test')}`",
                        f"  - 分類：{detail.get('category')}",
                        f"  - 觀察：{detail.get('observed')}",
                        f"  - 錯誤：{detail.get('error')}",
                        f"  - 單測試命令：`{detail.get('reproduce')}`",
                    ])
    qa = report.get("qa_review") if isinstance(report.get("qa_review"), dict) else {}
    product = qa.get("product_test") if isinstance(qa.get("product_test"), dict) else {}
    plan = product.get("plan") if isinstance(product.get("plan"), dict) else {}
    web_ui = plan.get("web_ui") if isinstance(plan.get("web_ui"), dict) else {}
    browser_contract = product.get("browser_contract") if isinstance(product.get("browser_contract"), dict) else {}
    confirmed_steps = product.get("confirmed_browser_steps")
    if not isinstance(confirmed_steps, list):
        confirmed_steps = browser_contract.get("steps") if isinstance(browser_contract.get("steps"), list) else []
    candidate_steps = web_ui.get("candidate_steps") if isinstance(web_ui.get("candidate_steps"), list) else []
    run_operations = product.get("run_operations") if isinstance(product.get("run_operations"), list) else []
    candidate_commands = plan.get("candidate_commands") if isinstance(plan.get("candidate_commands"), list) else []
    lines.extend([
        "",
        "### 2. 產品 Browser／UI 語意流程",
        "",
        "Browser 必須先通過遠端前置檢查，再由 SSH tunnel 將動態 URL 交給本地 Playwright。",
        "只有 confirmed contract／confirmed steps 才能直接當作 executable oracle；discovery candidate 只能用來設計與確認，不能宣稱測試通過。",
    ])
    if confirmed_steps:
        lines.extend([
            "",
            "本次已確認的 Browser steps（可依 contract 重現）：",
            "```json",
            json.dumps(confirmed_steps, ensure_ascii=False, indent=2),
            "```",
        ])
    else:
        lines.extend([
            "",
            "本次沒有可直接執行的 confirmed Browser steps；以下資料若存在，僅是 candidate，不能當成 PASS：",
        ])
        for candidate in candidate_steps[:40]:
            if not isinstance(candidate, dict):
                continue
            summary = candidate.get("summary") or candidate.get("description") or candidate.get("action") or "未命名 candidate"
            source = candidate.get("source") or "未記錄來源"
            line = candidate.get("line")
            location = f"{source}:{line}" if line else str(source)
            lines.append(f"- candidate：{summary}（來源：{location}）")
        if run_operations:
            lines.append("候選產品操作（仍需明確 confirmation）：")
            for operation in run_operations[:20]:
                lines.append(f"- `{operation}`")
        if candidate_commands:
            lines.append("候選命令（仍需明確 confirmation；不可直接作為 oracle）：")
            lines.extend([f"```bash\n{command}\n```" for command in candidate_commands[:20]])
        if not candidate_steps and not run_operations and not candidate_commands:
            lines.append("- 未建立 candidate；請先在產品 contract 中定義 locator、操作、expected state 與 failure oracle。")
    lines.extend([
        "",
        "### 3. 非 UI／UX 維度的補測方式",
        "- 黑箱：使用產品實際 CLI/TUI/API/UI 入口，保存命令、標準輸出／錯誤輸出、預期／實際結果與判定規則。",
        "- 功能：依變更檔案執行 targeted tests，並保存第一個失敗測試與完整輸出。",
        "- 白箱：執行完整回歸，確認沒有把未完成的測試套件當成通過。",
        "- 邊界／無效輸入：建立空值、型別錯誤、範圍錯誤與硬體前置條件案例。",
        "- 壓力／逾時：先定義受限執行時間、逾時、資源上限與基準，再執行。",
        "- 文件：重新讀取變更文件與契約，確認命令、欄位與 workflow 描述一致。",
        "- 產品建置：遠端 adapter 完成前，只能標示尚未評估，不能標示通過。",
    ])
    return lines


def _render_final_result(report: dict[str, Any]) -> list[str]:
    records = _report_browser_records(report)
    passed = [item for item in records if str(item.get("status") or "").upper() == "PASS"]
    failed = [item for item in records if str(item.get("status") or "").upper() == "FAIL"]
    product = report.get("product_test_evidence") if isinstance(report.get("product_test_evidence"), dict) else {}
    return [
        "",
        "最終結果（請以本節作為摘要）",
        "=" * 80,
        f"Quality Pilot 建議：{_quality_pilot_recommendation(report)}",
        "PR 合併決定：由 PR 擁有者決定；Quality Pilot 沒有執行 merge，也沒有修改 merge gate。",
        f"實際通過的 Browser／Playwright case：{len(passed)} 個。",
        f"實際失敗的 Browser／Playwright case：{len(failed)} 個。",
        f"產品建置／產物：{_status_for_engineer(product.get('status'), reason=product.get('reason')) if product else '未建立'}；{_engineer_reason(product.get('status'), product.get('reason')) if product else '沒有產品建置結果。'}",
        f"完整 QA 結果：{_status_for_engineer(report.get('qa_outcome'))}。",
        "只有具備實際命令、oracle 與 evidence 的 case 才能支持通過；HOLD、BLOCK、逾時與未評估都不是 PASS。",
        "",
        "工程師下一步：依上方『工程師可直接複製的重現手冊』先重現失敗，再逐項修復或提供可追溯的風險接受理由。",
    ]


def _render_detailed_text(report: dict[str, Any]) -> str:
    """Render the complete developer-facing plain-text report."""
    developer = report.get("developer_review") if isinstance(report.get("developer_review"), dict) else {}
    summary = developer.get("summary") if isinstance(developer.get("summary"), dict) else {}
    lines = [
        "QUALITY PILOT－開發人員詳細程式碼審查報告",
        "=" * 80,
        f"儲存庫：{report.get('repo')}",
        f"合併請求：第 {report.get('pr_number')} 號",
        f"基準：{report.get('base_ref') or report.get('base_sha')}",
        f"目前版本：{report.get('head_ref') or report.get('head_sha')}",
        "報告語言：繁體中文（zh-TW）",
        "",
        "本報告用於協助開發人員理解、重現與修復問題，不是自動核准或合併決定。",
        "本報告同時是 PR 留言內容；它是 Quality Pilot 的 advisory COMMENT，不是核准、要求修改或 merge 動作。",
        "最終的留言、要求修改或核准由 PR 擁有者決定。",
        "所有建議的重現步驟都會標示是否實際執行；沒有證據的步驟不宣稱為測試結果。",
        "",
        *_render_engineer_execution_summary(report),
        *_render_browser_evidence(report),
        "整體審查狀態",
        f"建議決定：{_zh_value(developer.get('decision', 'COMMENT'))}",
        f"測試結果：{_zh_value(report.get('test_outcome'))}",
        f"產品測試結果：{_zh_value(report.get('product_test_outcome'))}",
        f"Browser UI 結果：{_zh_value(report.get('browser_ui_outcome'))}",
        f"Quality Pilot 工具結果：{_zh_value(report.get('tooling_outcome'))}",
        f"基礎環境前置檢查結果：{_zh_value(report.get('infrastructure_outcome'))}",
        f"產品評估狀態：{_zh_value(report.get('product_evaluation_status'))}",
        f"品質保證結果：{_zh_value(report.get('qa_outcome'))}",
        f"結論：{report.get('conclusion')}",
        f"問題總數：{summary.get('total_issues', 0)}",
        f"必須修復：{summary.get('must_fix', 0)}",
        f"應該修復：{summary.get('should_fix', 0)}",
        f"可改善項目：{summary.get('nice_to_have', 0)}",
        "",
        "執行分層與證據來源",
        f"- 本地審查工作樹：{_zh_value(report.get('execution_targets', {}).get('local_review_worktree')) if isinstance(report.get('execution_targets'), dict) else '未確認'}",
        f"- 本地 pytest：{_zh_value(report.get('execution_targets', {}).get('local_pytest')) if isinstance(report.get('execution_targets'), dict) else '未確認'}",
        f"- 本地 Python：{(report.get('execution_targets') or {}).get('local_python', '未確認') if isinstance(report.get('execution_targets'), dict) else '未確認'}",
        f"- 本地證據來源：{(report.get('execution_targets') or {}).get('local_evidence_origin', '未確認') if isinstance(report.get('execution_targets'), dict) else '未確認'}",
        f"- 遠端 pytest：{_zh_value((report.get('execution_targets') or {}).get('remote_pytest', 'NOT_RUN')) if isinstance(report.get('execution_targets'), dict) else '未執行'}（與本地回歸分開記錄）",
        f"- 產品執行目標：{(report.get('execution_targets') or {}).get('product_target', '未確認') if isinstance(report.get('execution_targets'), dict) else '未確認'}",
        f"- Playwright 執行目標：{(report.get('execution_targets') or {}).get('playwright_target', '未確認') if isinstance(report.get('execution_targets'), dict) else '未確認'}",
        f"- 遠端前置檢查：{_zh_value(((report.get('remote_preflight') or {}).get('status', 'NOT_RUN')) if isinstance(report.get('remote_preflight'), dict) else 'NOT_RUN')}",
        f"- Quality Pilot 建議閘門：{_zh_value(((report.get('review_gate') or {}).get('status', 'BLOCKED')) if isinstance(report.get('review_gate'), dict) else 'BLOCKED')}；原因={((report.get('review_gate') or {}).get('reason', '未建立')) if isinstance(report.get('review_gate'), dict) else '未建立'}；這不是 Gitea merge gate",
        "- 每個案例都必須明確標示執行位置與證據來源；本地與遠端證據不得混寫。",
        "",
        "證據規則",
        "- 確定性問題來自 pinned diff、實際執行命令、產品測試介面或生成案例證據。",
        "- 疑似機密值會完整遮蔽，但在安全範圍內保留程式碼上下文。",
        "- 建議的重現步驟只有在附有執行狀態與證據時，才會被描述為已執行結果。",
        "",
    ]
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    lines.extend(["確定性問題詳細內容", "-" * 80])
    if not findings:
        lines.append("無。")
    for number, item in enumerate(findings, 1):
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        repro = item.get("reproducibility") if isinstance(item.get("reproducibility"), dict) else {}
        context = item.get("code_context") or "[已遮蔽的上下文目前無法取得]"
        lines.extend([
            "",
            f"問題 {number}：{item.get('id')}",
            f"嚴重程度：{_zh_value(item.get('severity'))}",
            f"分類：{_zh_value(item.get('category'))}",
            f"位置：{item.get('path')}：第 {item.get('line')} 行",
            f"來源：確定性差異掃描器；規則={evidence.get('kind', '未知')}",
            "程式碼片段（已遮蔽敏感內容）：",
            f"  {context}",
            "問題：",
            f"  {item.get('message')}",
            "為什麼需要處理：",
            "  原始碼中的疑似機密值可能被提交、複製、寫入紀錄，或在不應有的信任範圍外被使用。",
            "重現步驟：",
            *[f"  {step}" for step in repro.get("steps", [])],
            f"預期結果：{repro.get('expected', '在相同位置產生相同問題。')}",
            f"實際結果：{repro.get('actual', '偵測到疑似機密內容；原始值已遮蔽。')}",
            f"證據：{repro.get('evidence', '確定性 diff 掃描')}",
            "建議修補方式：",
            f"  {item.get('recommendation')}",
            "修補後驗證方式：",
            "  移除原始值，改用核准的環境變數／秘密儲存區參照或明確標示的遮蔽測試 fixture，然後重新執行相同的 pinned review。",
        ])
    sections = developer.get("sections") if isinstance(developer.get("sections"), dict) else {}
    for key, title in (("must_fix", "必須修復"), ("should_fix", "應該修復"), ("nice_to_have", "可改善項目")):
        lines.extend(["", title, "-" * 80])
        items = sections.get(key) if isinstance(sections.get(key), list) else []
        if not items:
            lines.append("無。")
            continue
        for number, item in enumerate(items, 1):
            item_id = item.get("id", f"{key}-{number}")
            lines.extend([
                "",
                f"{title} {number}：{item_id}",
                f"嚴重程度：{_zh_value(item.get('severity', 'INFO'))}",
                f"狀態：{_zh_value(item.get('status', 'ADVISORY'))}",
                f"來源：{_zh_value(item.get('category', '審查分析'))}",
                "問題／缺口：",
                f"  {item.get('recommendation') or item.get('message')}",
                "為什麼應該處理：",
                f"  {_detailed_reason(item)}",
                "調查／重現步驟：",
                *[f"  {step}" for step in _engineer_reproduction_steps(item, report)],
                "修補後預期結果：",
                f"  {item.get('verification') or '具備確定性的產品或測試 oracle，且驗證通過。'}",
                "目前實際狀態：",
                f"  本次 review 回報狀態={item.get('status', 'UNKNOWN')}；這不自動代表產品有缺陷。",
                "建議實作方式：",
                f"  {item.get('recommendation') or item.get('message')}",
                "驗證方式：",
                f"  {item.get('verification') or '重新執行受影響的測試並保存證據。'}",
            ])
    test_results = developer.get("evidence", {}).get("test_results", []) if isinstance(developer.get("evidence"), dict) else []
    lines.extend(["", "測試執行證據", "-" * 80])
    for item in test_results:
        if not isinstance(item, dict):
            continue
        repro = item.get("reproduction") if isinstance(item.get("reproduction"), dict) else {}
        lines.extend([
            "",
            f"測試：{item.get('id')}",
            f"命令：{item.get('command')}",
            f"狀態：{_status_for_engineer(item.get('status'), reason=item.get('reason'), exit_code=item.get('exit_code'))}",
            f"原因代碼：`{item.get('reason') or '未記錄'}`",
            f"結束代碼：{item.get('exit_code')}",
            f"覆蓋狀態：{_zh_value(item.get('coverage_status', 'FULL'))}",
            f"是否嘗試替代測試：{'是' if item.get('fallback_attempted', False) else '否'}",
            f"替代測試詳情：狀態={_zh_value((item.get('fallback') or {}).get('status', 'NOT_RUN'))}；原因={(item.get('fallback') or {}).get('reason', '無')}；跳過範圍={', '.join(str(value) for value in ((item.get('fallback') or {}).get('skipped_scope') or [])) or '無'}",
            f"預期結果：{repro.get('expected', '結束代碼為 0')}",
            f"實際結果：{repro.get('actual', _engineer_reason(item.get('status'), item.get('reason'), item.get('exit_code')))}",
            f"證據：{', '.join(str(path) for path in repro.get('evidence', []) if path) or '未建立'}",
        ])
    lines.extend(_render_complete_qa_matrix(report))
    lines.extend(_render_reproduction_playbook(report))
    lines.extend(_render_final_result(report))
    return "\n".join(lines) + "\n"


def _pytest_result_summary(stdout: str) -> dict[str, Any]:
    failed_tests = [line.strip()[7:] for line in str(stdout or "").splitlines() if line.strip().startswith("FAILED ")]
    summary_lines = [line.strip() for line in str(stdout or "").splitlines() if re.search(r"\b(?:passed|failed|error|skipped)\b", line, re.IGNORECASE)]
    return {
        "failed_tests": failed_tests[:40],
        "summary": summary_lines[-1] if summary_lines else None,
    }


def _pytest_failure_details(stdout: str, failed_tests: list[str] | None = None, *, command: str = "") -> list[dict[str, Any]]:
    """Extract bounded, copyable failure diagnostics from pytest output."""
    text = str(stdout or "")
    names = list(failed_tests or _pytest_result_summary(text).get("failed_tests", []))
    details: list[dict[str, Any]] = []
    for full_name in names[:40]:
        short_name = str(full_name).rsplit("::", 1)[-1]
        marker = re.search(rf"^_+\s*{re.escape(short_name)}\s*_+$", text, re.MULTILINE)
        block = text[marker.start():] if marker else text
        body_start = block.find("\n") + 1
        next_header = re.search(r"^_{5,}\s*\S.*?_{5,}$", block[body_start:], re.MULTILINE)
        if next_header:
            block = block[: body_start + next_header.start()]
        location_match = re.search(r"(?m)^\s*(tests/[^\s:]+:\d+)", block)
        error_lines = []
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("E   ") or "TimeoutError:" in stripped or "AssertionError:" in stripped:
                error_lines.append(stripped)
        if "element to be visible, enabled and stable" in block or "Locator.click: Timeout" in block:
            category = "playwright_actionability_timeout"
            observed = "Playwright located the element but could not complete click because it never became stable within the test timeout."
        elif "Browser UI did not print an access URL" in block or "browser_url_discovery" in block:
            category = "browser_server_startup_or_url_timeout"
            observed = "The product test process did not expose a Browser URL before the test startup deadline."
        elif "root.name == \"auto_PID_tool\"" in block or "AssertionError: assert root.name" in block:
            category = "review_worktree_path_assumption"
            observed = "The test assumes the checkout directory has the product repository name; pinned review worktrees intentionally use a PR-specific directory."
        else:
            category = "pytest_assertion_or_product_test_failure"
            observed = "Pytest completed this case with an assertion or test error; inspect the bounded excerpt below."
        if command and " -m pytest" in command:
            pytest_command = command.split(" -m pytest", 1)[0] + " -m pytest"
            reproduce = f"{pytest_command} {full_name} -q"
        else:
            reproduce = f"{command} {full_name}".strip() if command else full_name
        details.append(
            {
                "test": full_name,
                "category": category,
                "location": location_match.group(1) if location_match else None,
                "observed": observed,
                "error": "; ".join(error_lines[-3:])[:1200] or "No bounded error line was extracted; inspect stdout evidence.",
                "reproduce": reproduce,
            }
        )
    return details


def _review_test_infrastructure_reason(stdout: str, stderr: str) -> str | None:
    text = f"{stdout}\n{stderr}".lower()
    if "modulenotfounderror" in text or "no module named" in text or re.search(r"importerror:.*cannot import", text):
        return "test_dependency_missing"
    if "command not found" in text or "executable_not_found" in text:
        return "test_executable_missing"
    return None


def _safe_test_argv(command: str, *, worktree: Path | None = None) -> list[str] | None:
    """Convert a selected repository test command into argv without a shell.

    Review tests are discovered from repository metadata, not chat.  Still, a
    review checkout is an untrusted boundary, so only bounded Python unittest
    or pytest commands are executable by this workflow.
    """
    try:
        argv = shlex.split(str(command or ""))
    except ValueError:
        return None
    if len(argv) < 3:
        return None
    executable = Path(argv[0]).name.lower()
    if not executable.startswith("python"):
        return None
    if argv[1:] == ["-m", "unittest", "discover", "-s", "tests"]:
        return argv
    if len(argv) >= 5 and argv[1:3] == ["-m", "pytest"] and argv[-1] == "-q":
        test_paths = argv[3:-1]
        if test_paths and all(
            path.startswith("tests/")
            and path != "tests/"
            and not path.startswith("-")
            and path.endswith(".py")
            and ".." not in Path(path).parts
            and (
                worktree is None
                or (
                    (worktree / path).resolve().is_file()
                    and (worktree / path).resolve().is_relative_to((worktree / "tests").resolve())
                )
            )
            for path in test_paths
        ):
            return argv
    if argv[1:] == ["-m", "pytest", "tests", "-q"]:
        return argv
    return None


def _matching_test_files(tests_dir: Path, changed_paths: list[str]) -> list[str]:
    stems = {Path(path).stem.replace("test_", "") for path in changed_paths if path}
    return sorted(str(path.relative_to(tests_dir)) for path in tests_dir.rglob("test_*.py") if path.stem.replace("test_", "") in stems)


def _diff_targeted_test_files(tests_dir: Path, changed_paths: list[str]) -> list[str]:
    """Map changed product surfaces to existing, product-owned test files.

    This is deliberately conservative: a missing mapping stays HOLD instead of
    inventing a black-box oracle. Matching uses explicit changed test files,
    filename tokens, and import references found in test source.
    """
    tests = sorted(path for path in tests_dir.rglob("test_*.py") if path.is_file())
    if not tests:
        return []
    selected: set[Path] = set()
    normalized_changed = [str(path).replace("\\", "/").strip("/") for path in changed_paths if str(path).strip()]
    for changed in normalized_changed:
        changed_path = Path(changed)
        if changed.startswith("tests/"):
            candidate = tests_dir.parent / changed
            if candidate.is_file():
                selected.add(candidate.resolve())
        stem = changed_path.stem.lower()
        stem_tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", stem)
            if len(token) >= 3 and token not in {"py", "test"}
        }
        if stem_tokens:
            for test in tests:
                test_tokens = {
                    token
                    for token in re.split(r"[^a-z0-9]+", test.stem.lower())
                    if len(token) >= 3 and token not in {"py", "test"}
                }
                if stem_tokens & test_tokens:
                    selected.add(test.resolve())
        module_path = changed_path.with_suffix("").as_posix().replace("/", ".")
        import_pattern = re.compile(
            rf"(?m)^\s*(?:from|import)\s+{re.escape(module_path)}(?:\s|$)"
        )
        for test in tests:
            try:
                text = test.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if import_pattern.search(text):
                selected.add(test.resolve())
        if changed_path.name == "main.py":
            selected.update(test.resolve() for test in tests if test.stem.lower() in {"test_cli", "test_main"})

    relative = []
    for path in sorted(selected):
        try:
            value = path.relative_to(tests_dir.parent).as_posix()
        except ValueError:
            continue
        if value.startswith("tests/"):
            relative.append(value.removeprefix("tests/"))
    return relative[:12]


def _tests_use_pytest(tests_dir: Path) -> bool:
    for path in tests_dir.rglob("test_*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"(?m)^\s*(?:import\s+pytest|from\s+pytest\b)", text):
            return True
        # Bare test functions and pytest fixtures are unambiguous enough for
        # review selection, while unittest.TestCase classes are not matched.
        if re.search(r"(?m)^\s*def\s+test_[A-Za-z0-9_]+\s*\(", text):
            return True
    return False


def _review_python_executable(config: ProjectConfig, worktree: Path | None = None) -> str:
    """Return the interpreter command for the selected review execution boundary.

    Once a disposable worktree exists, pytest must always be addressed through
    its own relative ``.venv/bin/python``.  This prevents the Quality Pilot
    checkout venv, the product repository venv, or Debian ``/usr/bin/python3``
    from being mistaken for local review evidence.
    """
    if worktree is not None:
        return ".venv/bin/python"
    candidates: list[str] = []
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    entrypoint = str(runtime.get("primary_entrypoint") or "").strip()
    if entrypoint:
        try:
            first = shlex.split(entrypoint)[0]
        except ValueError:
            first = ""
        if first:
            candidates.append(first)
    candidates.extend(
        [
            str(config.root / ".venv" / "bin" / "python"),
            sys.executable,
            "python3",
        ]
    )
    for candidate in candidates:
        name = Path(candidate).name.lower()
        if name.startswith("python") and (Path(candidate).is_file() or candidate == sys.executable or candidate == "python3"):
            return candidate
    return "python3"


def _contains_pytest_config(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "pytest" in text.lower() or "[tool.pytest" in text


def _run_git(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run an explicit git argv command.

    Callers pass git's arguments (for example ``-C <checkout> fetch ...``);
    the executable itself must remain the first argv item.  Keeping this
    wrapper explicit prevents a flag such as ``-C`` from being interpreted as
    the executable by ``subprocess``.
    """
    return subprocess.run(["git", *args], text=True, capture_output=True, check=False, timeout=timeout)


def _safe_output(value: str | None) -> str:
    text = str(value or "")
    safe, _findings = _redact_output(text[-1000:])
    return safe


def _repo_slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(repo).strip()).strip("_") or "repo"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
