"""Fail-closed, product-facing build and README operation tests.

This module is deliberately separate from repository regression tests.  A PR
review may use it only in comprehensive mode and only with an explicit
user-owned runtime product-test contract.  README commands are candidate input;
they are executable only when the user enables them and supplies an allowlist.
The pinned review worktree is never used as a writable build directory: builds
run in a disposable copy and every result carries a contract identity hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .config import ProjectConfig
from .execution_contract import effective_product_settings
from .security import redact_structure, redact_text

PRODUCT_TEST_SCHEMA = "quality-pilot.product-build-run.v1"
PRODUCT_TEST_ADAPTER_VERSION = "1.0.0"
PRODUCT_TEST_OUTCOMES = {"PASS", "FAIL", "BLOCK", "HOLD", "INTERRUPTED", "NOT_RUN", "PLANNED"}

# Commands are executed with shell=False.  The allowlist is intentionally small;
# a project may use an explicit executable path below the sandbox root.
_ALLOWED_EXECUTABLES = {
    "cargo",
    "make",
    "node",
    "npm",
    "pnpm",
    "python",
    "python3",
    "uv",
    "yarn",
    "go",
    "java",
}
_BLOCKED_EXECUTABLES = {"curl", "wget", "nc", "netcat", "ssh", "scp", "sudo", "su", "doas", "rm", "rmdir"}
_SHELL_META = re.compile(r"[;|&><`$()]|\r|\n")
_BUILD_PREFIXES = (
    "make ",
    "go build",
    "cargo build",
    "npm run build",
    "pnpm build",
    "yarn build",
    "python -m build",
    "python3 -m build",
    "uv build",
)
_RUN_PREFIXES = (
    "./",
    "python ",
    "python3 ",
    "node ",
    "npm run start",
    "npm run dev",
    "pnpm start",
    "pnpm dev",
    "yarn start",
    "yarn dev",
    "java -jar",
)


def run_product_tests(
    config: ProjectConfig,
    *,
    worktree: Path,
    snapshot: Mapping[str, Any],
    review_id: str,
    evidence_dir: Path,
    environment_profile: Mapping[str, Any] | None,
    dry_run: bool = False,
    report_root: Path | None = None,
    case_id: str | None = None,
    run_id: str | None = None,
    contract_hash: str | None = None,
    product_python: Path | None = None,
    product_settings: Mapping[str, Any] | None = None,
    execution_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and exercise a product according to a user-owned contract.

    ``runtime.product_testing`` is the preferred configuration section.  For
    compatibility, direct ``runtime`` keys are also accepted.  An absent
    contract is a BLOCK in comprehensive review: repository unit tests are not
    a substitute for a product test.
    """
    report_root = report_root or config.root
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    settings, configured = _product_settings(runtime)
    if product_settings is not None:
        settings = dict(product_settings)
        configured = True
    if settings.get("enabled") is False or runtime.get("product_testing_enabled") is False:
        return _base_result(
            status="BLOCK",
            reason="product_testing_disabled",
            review_id=review_id,
            snapshot=snapshot,
            plan={},
        )

    plan = resolve_product_test_plan(config, worktree=worktree, snapshot=snapshot, settings=settings, configured=configured, execution_contract=execution_contract)
    case_id = case_id or f"PRODUCT-{_safe_name(review_id)}"
    run_id = run_id or review_id
    base = _base_result(
        status=plan.get("status", "BLOCK"),
        reason=plan.get("reason"),
        review_id=review_id,
        snapshot=snapshot,
        plan=plan,
    )
    base["case_id"] = case_id
    base["run_id"] = run_id
    base["case_type"] = "product_build_and_semantic_operation"
    base["candidate_commands"] = plan.get("candidate_commands", [])
    base["execution_contract_hash"] = (execution_contract or {}).get("contract_hash")
    base["execution_target"] = (execution_contract or {}).get("execution", {}).get("product_target") if isinstance((execution_contract or {}).get("execution"), Mapping) else ("remote_ssh" if str((environment_profile or {}).get("execution_mode") or "local") == "remote" else "local")
    base["evidence_origin"] = "remote" if base["execution_target"] == "remote_ssh" else "local"
    if plan.get("status") != "READY":
        evidence_dir.mkdir(parents=True, exist_ok=True)
        return _persist_result(base, evidence_dir, report_root)
    if not worktree.exists() or not worktree.is_dir():
        base.update({"status": "BLOCK", "reason": "pinned_worktree_missing"})
        return _persist_result(base, evidence_dir, report_root)
    if environment_profile is not None and not bool(environment_profile.get("ready")):
        base.update(
            {
                "status": "BLOCK",
                "reason": "environment_profile_required",
                "environment_blockers": list(environment_profile.get("blockers", [])),
            }
        )
        return _persist_result(base, evidence_dir, report_root)

    evidence_dir.mkdir(parents=True, exist_ok=True)
    contract_hash = contract_hash or compute_contract_identity_hash(
        review_id=review_id,
        snapshot=snapshot,
        settings=settings,
        plan=plan,
    )
    base["contract_identity_hash"] = contract_hash
    if dry_run:
        base.update({"status": "PLANNED", "reason": "dry_run"})
        return base

    # Keep the writable copy outside the source worktree.  Placing it below
    # the worktree would make copytree recurse into its own destination.
    sandbox_path: Path | None = None
    build_results: list[dict[str, Any]] = []
    operation_results: list[dict[str, Any]] = []
    browser_result: dict[str, Any] | None = None
    build_blocked: dict[str, Any] | None = None
    operation_blocked: dict[str, Any] | None = None
    product_target = str((execution_contract or {}).get("execution", {}).get("product_target") or ("remote_ssh" if str((environment_profile or {}).get("execution_mode") or "local") == "remote" else "local"))
    try:
        sandbox_path = Path(tempfile.mkdtemp(prefix=f"quality-pilot-product-{_safe_name(review_id)}-"))
        shutil.copytree(
            worktree,
            sandbox_path,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", ".quality-pilot-project"),
        )
        base["sandbox_type"] = "disposable_copy"
        base["sandbox_path"] = "<temporary-disposable-copy-removed-after-run>"

        for index, command in enumerate(plan.get("build_recipe", [])):
            if product_target == "remote_ssh":
                build_blocked = {"status": "BLOCK", "reason": "remote_product_build_adapter_not_supported", "results": build_results}
                break
            result = _execute_product_command(
                command,
                cwd=sandbox_path,
                evidence_dir=evidence_dir / "build",
                evidence_name=f"build-{index}",
                timeout_ms=int(settings.get("build_timeout_ms", 300_000)),
                environment_profile=environment_profile,
                phase="build",
                product_python=product_python or _product_python_executable(worktree),
            )
            build_results.append(result)
            if result["status"] != "PASS":
                build_blocked = {"status": result["status"], "reason": result.get("reason") or "build_failed", "results": build_results}
                if not (web_ui_enabled := bool((plan.get("web_ui") or {}).get("enabled")) and product_target == "remote_ssh"):
                    base.update({"status": "BLOCK" if result["status"] == "BLOCK" else "FAIL", "reason": build_blocked["reason"], "build": build_blocked, "run_operations": operation_results})
                    return _persist_result(base, evidence_dir, report_root)
                break

        if plan.get("build_required") and build_blocked is None:
            artifact = _verify_artifact(sandbox_path, plan.get("artifact_path"), evidence_dir / "build", report_root)
            if artifact["status"] != "PASS":
                build_blocked = {"status": "BLOCK", "reason": artifact.get("reason", "build_artifact_missing"), "results": build_results, "artifact": artifact}
                if not (bool((plan.get("web_ui") or {}).get("enabled")) and product_target == "remote_ssh"):
                    base.update({"status": "BLOCK", "reason": build_blocked["reason"], "build": build_blocked, "run_operations": operation_results})
                    return _persist_result(base, evidence_dir, report_root)
        build_summary = {"status": "NOT_RUN", "reason": "build_not_required_for_remote_browser"} if not plan.get("build_required") else (build_blocked or {"status": "PASS", "results": build_results, "artifact": artifact})

        for index, operation in enumerate(plan.get("run_operations", [])):
            if product_target == "remote_ssh":
                operation_blocked = {"status": "BLOCK", "reason": "remote_product_operation_adapter_not_supported", "operation": operation.get("id")}
                operation_results.append(operation_blocked)
                continue
            result = _execute_product_operation(
                operation,
                cwd=sandbox_path,
                evidence_dir=evidence_dir / "run",
                evidence_name=f"run-{index}",
                timeout_ms=int(operation.get("timeout_ms") or settings.get("run_timeout_ms", 60_000)),
                environment_profile=environment_profile,
                report_root=report_root,
                product_python=product_python or _product_python_executable(worktree),
            )
            operation_results.append(result)
            if result["status"] != "PASS":
                outcome = result["status"] if result["status"] in {"FAIL", "BLOCK", "HOLD"} else "HOLD"
                operation_blocked = result
                if not (bool((plan.get("web_ui") or {}).get("enabled")) and product_target == "remote_ssh"):
                    base.update({"status": outcome, "reason": result.get("reason") or "product_operation_not_passed", "build": build_summary, "run_operations": operation_results})
                    return _persist_result(base, evidence_dir, report_root)

        web_ui = plan.get("web_ui") if isinstance(plan.get("web_ui"), dict) else {}
        if web_ui.get("enabled"):
            if product_target == "remote_ssh":
                preflight = (environment_profile or {}).get("remote_preflight") if isinstance((environment_profile or {}).get("remote_preflight"), Mapping) else None
                source_identity = preflight.get("source_identity") if isinstance(preflight, Mapping) and isinstance(preflight.get("source_identity"), Mapping) else {}
                if str(source_identity.get("status") or "UNVERIFIED") != "VERIFIED":
                    source_reason = "REMOTE_SOURCE_MISMATCH" if source_identity.get("status") == "MISMATCH" else ("REMOTE_SOURCE_DIRTY" if source_identity.get("status") == "DIRTY" else "REMOTE_SOURCE_UNVERIFIED")
                    browser_result = {"status": "BLOCK", "reason": source_reason, "evidence_origin": "remote", "source_identity": dict(source_identity)}
                else:
                    from .remote_browser_adapter import run_remote_browser_test
                    browser_result = run_remote_browser_test(
                        web_ui,
                        environment_profile=environment_profile or {},
                        evidence_dir=evidence_dir / "browser",
                        contract_identity_hash=contract_hash,
                        root=report_root,
                        case_id=f"{case_id}-BROWSER-UI",
                        run_id=run_id,
                        timeout_ms=int(web_ui.get("timeout_ms") or settings.get("run_timeout_ms", 60_000)),
                        dry_run=False,
                    )
                    browser_result["source_identity"] = dict(source_identity)
            else:
                from .browser_adapter import run_browser_test
                browser_result = run_browser_test(
                    web_ui,
                    cwd=sandbox_path,
                    evidence_dir=evidence_dir / "browser",
                    contract_identity_hash=contract_hash,
                    environment_profile=environment_profile,
                    timeout_ms=int(web_ui.get("timeout_ms") or settings.get("run_timeout_ms", 60_000)),
                    dry_run=False,
                    root=report_root,
                    case_id=f"{case_id}-BROWSER-UI",
                    run_id=run_id,
                )
            browser_result["execution_target"] = product_target
            browser_result.setdefault("evidence_origin", "remote" if product_target == "remote_ssh" else "local")
            if browser_result.get("status") != "PASS":
                base.update(
                    {
                        "status": browser_result.get("status", "HOLD"),
                        "reason": browser_result.get("reason") or "browser_ui_not_passed",
                        "build": build_summary,
                        "run_operations": operation_results,
                        "browser": browser_result,
                    }
                )
                return _persist_result(base, evidence_dir, report_root)
        semantic_operation_passed = any(_operation_has_semantic_assertion(item) for item in plan.get("run_operations", []))
        if browser_result and browser_result.get("status") == "FAIL":
            final_status = "FAIL"
            final_reason = browser_result.get("reason") or "browser_ui_not_passed"
        elif browser_result and browser_result.get("status") == "BLOCK":
            final_status = "BLOCK"
            final_reason = browser_result.get("reason") or "browser_ui_blocked"
        elif build_blocked or operation_blocked:
            final_status = "BLOCK"
            final_reason = (build_blocked or operation_blocked or {}).get("reason") or "product_stage_blocked"
        elif not semantic_operation_passed and not browser_result:
            final_status = "HOLD"
            final_reason = "probe_only_no_semantic_assertion"
        else:
            final_status = "PASS"
            final_reason = "build_and_semantic_product_operations_passed"
        base.update(
            {
                "status": final_status,
                "reason": final_reason,
                "build": build_summary,
                "run_operations": operation_results,
                "browser": browser_result,
            }
        )
        return _persist_result(base, evidence_dir, report_root)
    except KeyboardInterrupt:
        base.update({"status": "INTERRUPTED", "reason": "review_interrupted"})
        return _persist_result(base, evidence_dir, report_root)
    except (OSError, shutil.Error) as exc:
        base.update({"status": "BLOCK", "reason": "product_sandbox_failed", "error": type(exc).__name__})
        return _persist_result(base, evidence_dir, report_root)
    finally:
        if sandbox_path is not None:
            shutil.rmtree(sandbox_path, ignore_errors=True)


def resolve_product_test_plan(
    config: ProjectConfig,
    *,
    worktree: Path,
    snapshot: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
    configured: bool | None = None,
    execution_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve explicit product operations and README candidates without running them."""
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    if settings is None:
        settings, inferred_configured = _product_settings(runtime)
        configured = inferred_configured if configured is None else configured
    settings = dict(settings or {})
    configured = bool(configured)
    readme_candidates = extract_readme_commands(worktree)
    explicit_build = _command_list(settings.get("build_recipe"))
    explicit_operations = _operation_list(settings.get("run_operations"))
    artifact_path = str(settings.get("artifact_path") or settings.get("build_artifact") or "").strip()
    web_ui = dict(settings.get("web_ui") or {}) if isinstance(settings.get("web_ui"), Mapping) else {}
    product_target = str((execution_contract or {}).get("execution", {}).get("product_target") or "local")
    build_config = settings.get("build") if isinstance(settings.get("build"), Mapping) else {}
    browser_only_remote = bool(web_ui.get("enabled")) and product_target == "remote_ssh" and not explicit_build and not artifact_path
    build_required = bool(build_config.get("enabled", not browser_only_remote))

    if (build_required and not explicit_build) or (not explicit_operations and not web_ui.get("enabled")):
        if not bool(settings.get("allow_readme_commands")):
            missing_reason = "build_recipe_missing" if build_required and not explicit_build else "run_operation_missing"
            if not configured:
                missing_reason = "product_test_contract_missing"
            return {
                "status": "BLOCK",
                "reason": missing_reason,
                "candidate_commands": readme_candidates,
                "message": "Provide runtime.product_testing build_recipe/run_operations/artifact_path, or explicitly enable and allowlist README commands.",
            }
        allowlist = [str(item) for item in settings.get("readme_command_allowlist", []) if str(item).strip()]
        allowed = [item for item in readme_candidates if _matches_allowlist(item, allowlist)]
        rejected = [item for item in readme_candidates if item not in allowed]
        if rejected:
            return {
                "status": "BLOCK",
                "reason": "readme_command_not_allowlisted",
                "candidate_commands": readme_candidates,
                "rejected_commands": rejected,
            }
        if build_required and not explicit_build:
            explicit_build = [item for item in allowed if _looks_like_build(item)]
        if not explicit_operations:
            explicit_operations = [
                {"id": f"readme-{index}", "command": item, "assertion_type": "PROBE", "assertions": [{"type": "exit_code", "expected": 0}]}
                for index, item in enumerate(allowed, start=1)
                if _looks_like_run(item)
            ]

    if build_required and not explicit_build:
        return {"status": "BLOCK", "reason": "build_recipe_missing", "candidate_commands": readme_candidates}
    if build_required and not artifact_path:
        return {"status": "BLOCK", "reason": "build_artifact_missing", "candidate_commands": readme_candidates}
    if not explicit_operations and not web_ui.get("enabled"):
        return {"status": "BLOCK", "reason": "run_operation_missing", "candidate_commands": readme_candidates}

    unsafe = []
    if product_target != "remote_ssh":
        for command in explicit_build:
            if validate_product_command(command, cwd=worktree)["status"] != "SAFE":
                unsafe.append(command)
        for operation in explicit_operations:
            if validate_product_command(str(operation.get("command") or ""), cwd=worktree)["status"] != "SAFE":
                unsafe.append(str(operation.get("command") or ""))
    if unsafe:
        return {"status": "BLOCK", "reason": "unsafe_product_command", "unsafe_commands": unsafe}

    semantic_operations = [item for item in explicit_operations if _operation_has_semantic_assertion(item)]
    plan_warning = "probe_only_no_semantic_assertion" if explicit_operations and not semantic_operations and not web_ui.get("enabled") else None
    return {
        "status": "READY",
        "reason": plan_warning,
        "build_recipe": explicit_build,
        "build_required": build_required,
        "run_operations": explicit_operations,
        "artifact_path": artifact_path,
        "web_ui": web_ui,
        "candidate_commands": readme_candidates,
        "readme_sha256": _sha256(_readme_text(worktree)),
        "configured": configured,
        "snapshot_head_sha": str(snapshot.get("head_sha") or ""),
    }


def extract_readme_commands(root: Path) -> list[str]:
    """Extract candidate commands from README code fences; never execute them."""
    text = _readme_text(root)
    candidates: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence or not line or line.startswith("#"):
            continue
        line = re.sub(r"^(?:\$|>)\s+", "", line)
        if line.startswith(("cd ", "pip install", "pip3 install", "chmod ")):
            continue
        if _looks_like_build(line) or _looks_like_run(line):
            if line not in candidates:
                candidates.append(line)
    return candidates


def _product_python_executable(worktree: Path) -> Path | None:
    candidate = (worktree / ".venv" / "bin" / "python").resolve()
    return candidate if candidate.is_file() else None


def _normalize_product_python(command: str, product_python: Path | None) -> str:
    if product_python is None:
        return command
    parts = shlex.split(str(command or ""))
    if parts and parts[0] in {".venv/bin/python", ".venv/bin/python3", "python", "python3"}:
        parts[0] = str(product_python)
        return " ".join(shlex.quote(part) for part in parts)
    return command


def validate_product_command(command: str, *, cwd: Path | None = None, allowed_external_executable: Path | None = None) -> dict[str, Any]:
    value = str(command or "").strip()
    if not value:
        return {"status": "UNSAFE", "reason": "empty_command"}
    if _SHELL_META.search(value):
        return {"status": "UNSAFE", "reason": "shell_metacharacter"}
    try:
        argv = shlex.split(value)
    except ValueError:
        return {"status": "UNSAFE", "reason": "command_parse_failed"}
    if not argv:
        return {"status": "UNSAFE", "reason": "empty_command"}
    executable = Path(argv[0]).name.lower()
    if executable in _BLOCKED_EXECUTABLES or executable in {"pip", "pip3"} and any(item == "install" for item in argv[1:]):
        return {"status": "UNSAFE", "reason": "network_or_destructive_executable"}
    if argv[0].startswith("/"):
        resolved_executable = Path(argv[0]).resolve()
        allowed = allowed_external_executable is not None and resolved_executable == allowed_external_executable.resolve()
        if not allowed and (cwd is None or not _within(resolved_executable, cwd.resolve())):
            return {"status": "UNSAFE", "reason": "absolute_executable_outside_worktree"}
    elif not argv[0].startswith("./") and executable not in _ALLOWED_EXECUTABLES:
        return {"status": "UNSAFE", "reason": "executable_not_allowlisted"}
    if any(token in {"sudo", "su", "doas"} for token in argv):
        return {"status": "UNSAFE", "reason": "privilege_escalation"}
    if any(token in {"--password", "--token", "--secret", "--api-key"} for token in argv):
        return {"status": "UNSAFE", "reason": "credential_argument"}
    return {"status": "SAFE", "argv": argv}


def compute_contract_identity_hash(
    *,
    review_id: str,
    snapshot: Mapping[str, Any],
    settings: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> str:
    payload = {
        "schema": PRODUCT_TEST_SCHEMA,
        "adapter_version": PRODUCT_TEST_ADAPTER_VERSION,
        "review_id": str(review_id),
        "snapshot_sha": str(snapshot.get("head_sha") or ""),
        "base_sha": str(snapshot.get("base_sha") or ""),
        "runtime_product_testing": settings,
        "build_recipe": plan.get("build_recipe", []),
        "run_operations": plan.get("run_operations", []),
        "artifact_path": plan.get("artifact_path"),
        "web_ui": plan.get("web_ui", {}),
    }
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _product_settings(runtime: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    settings, configured, _source = effective_product_settings(runtime)
    return settings, configured


def _command_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _operation_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            result.append({"id": f"operation-{index}", "command": item.strip(), "assertion_type": "PROBE", "assertions": [{"type": "exit_code", "expected": 0}]})
        elif isinstance(item, Mapping) and str(item.get("command") or "").strip():
            result.append(
                {
                    "id": str(item.get("id") or f"operation-{index}"),
                    "command": str(item["command"]).strip(),
                    "assertion_type": str(item.get("assertion_type") or "PROBE").upper(),
                    "assertions": list(item.get("assertions") or item.get("assertion_rules") or []),
                    "timeout_ms": item.get("timeout_ms"),
                }
            )
    return result


def _operation_has_semantic_assertion(operation: Mapping[str, Any]) -> bool:
    if str(operation.get("assertion_type") or "PROBE").upper() != "SEMANTIC":
        return False
    return any(str(item.get("type") or "") not in {"exit_code", "duration_ms"} for item in operation.get("assertions", []) if isinstance(item, Mapping))


def _execute_product_operation(
    operation: Mapping[str, Any],
    *,
    cwd: Path,
    evidence_dir: Path,
    evidence_name: str,
    timeout_ms: int,
    environment_profile: Mapping[str, Any] | None,
    report_root: Path,
    product_python: Path | None = None,
) -> dict[str, Any]:
    result = _execute_product_command(
        str(operation.get("command") or ""),
        cwd=cwd,
        evidence_dir=evidence_dir,
        evidence_name=evidence_name,
        timeout_ms=timeout_ms,
        environment_profile=environment_profile,
        phase="run",
        report_root=report_root,
        product_python=product_python,
    )
    result["id"] = operation.get("id")
    result["assertion_type"] = operation.get("assertion_type", "PROBE")
    result["assertions"] = operation.get("assertions", [])
    if result["status"] == "PASS" and not _operation_has_semantic_assertion(operation):
        result["status"] = "HOLD"
        result["reason"] = "probe_only_no_semantic_assertion"
    elif result["status"] == "PASS":
        assertion_result = _evaluate_assertions(operation, result)
        result["assertion_results"] = assertion_result
        if not all(item["passed"] for item in assertion_result):
            result["status"] = "FAIL"
            result["reason"] = "semantic_assertion_failed"
    result.pop("_stdout_text", None)
    result.pop("_stderr_text", None)
    return result


def _execute_product_command(
    command: str,
    *,
    cwd: Path,
    evidence_dir: Path,
    evidence_name: str,
    timeout_ms: int,
    environment_profile: Mapping[str, Any] | None,
    phase: str,
    report_root: Path | None = None,
    product_python: Path | None = None,
) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_root = report_root or cwd
    command = _normalize_product_python(command, product_python)
    safety = validate_product_command(command, cwd=cwd, allowed_external_executable=product_python)
    stdout_path = evidence_dir / f"{evidence_name}.stdout.log"
    stderr_path = evidence_dir / f"{evidence_name}.stderr.log"
    meta_path = evidence_dir / f"{evidence_name}.meta.json"
    rc_path = evidence_dir / f"{evidence_name}.rc"
    if safety["status"] != "SAFE":
        stderr_path.write_text(str(safety.get("reason")) + "\n", encoding="utf-8")
        rc_path.write_text("2\n", encoding="utf-8")
        return {"command": command, "phase": phase, "status": "BLOCK", "reason": safety.get("reason"), "exit_code": 2, "stdout": _relative_or_str(stdout_path, report_root), "stderr": _relative_or_str(stderr_path, report_root)}
    started = time.monotonic()
    try:
        completed = subprocess.run(
            safety["argv"],
            cwd=cwd,
            env=_execution_environment(environment_profile),
            shell=False,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1, timeout_ms) / 1000,
        )
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        stdout, stdout_findings = redact_text(completed.stdout or "", path=str(stdout_path))
        stderr, stderr_findings = redact_text(completed.stderr or "", path=str(stderr_path))
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        rc_path.write_text(f"{completed.returncode}\n", encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.product-command.v1",
                    "command": command,
                    "argv": safety["argv"],
                    "phase": phase,
                    "duration_ms": duration_ms,
                    "timeout_ms": timeout_ms,
                    "redacted": bool(stdout_findings or stderr_findings),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if completed.returncode == 0:
            status, reason = "PASS", None
        elif _infrastructure_failure(stdout + "\n" + stderr):
            status, reason = "BLOCK", "product_test_dependency_missing"
        else:
            status, reason = "FAIL", f"{phase}_command_failed"
        result = {
            "command": command,
            "phase": phase,
            "status": status,
            "reason": reason,
            "exit_code": completed.returncode,
            "duration_ms": duration_ms,
            "stdout": _relative_or_str(stdout_path, report_root),
            "stderr": _relative_or_str(stderr_path, report_root),
            "meta": _relative_or_str(meta_path, report_root),
            "rc": _relative_or_str(rc_path, report_root),
        }
        if phase == "run":
            result["_stdout_text"] = stdout
            result["_stderr_text"] = stderr
        return result
    except FileNotFoundError:
        stderr_path.write_text("product_executable_missing\n", encoding="utf-8")
        rc_path.write_text("127\n", encoding="utf-8")
        return {
            "command": command,
            "phase": phase,
            "status": "BLOCK",
            "reason": "product_executable_missing",
            "exit_code": 127,
            "stdout": _relative_or_str(stdout_path, report_root),
            "stderr": _relative_or_str(stderr_path, report_root),
            "rc": _relative_or_str(rc_path, report_root),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        stdout, _ = redact_text(stdout, path=str(stdout_path))
        stderr, _ = redact_text(stderr, path=str(stderr_path))
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        rc_path.write_text("124\n", encoding="utf-8")
        return {
            "command": command,
            "phase": phase,
            "status": "BLOCK" if phase == "build" else "FAIL",
            "reason": "build_timeout" if phase == "build" else "run_timeout",
            "exit_code": 124,
            "stdout": _relative_or_str(stdout_path, report_root),
            "stderr": _relative_or_str(stderr_path, report_root),
            "rc": _relative_or_str(rc_path, report_root),
        }


def _evaluate_assertions(operation: Mapping[str, Any], result: Mapping[str, Any]) -> list[dict[str, Any]]:
    stdout = str(result.get("_stdout_text") or "")
    stderr = str(result.get("_stderr_text") or "")
    evaluated: list[dict[str, Any]] = []
    for index, raw in enumerate(operation.get("assertions", [])):
        if not isinstance(raw, Mapping):
            evaluated.append({"id": f"assertion-{index}", "passed": False, "reason": "invalid_assertion"})
            continue
        kind = str(raw.get("type") or "").strip().lower()
        expected = raw.get("expected")
        if kind == "exit_code":
            passed = int(result.get("exit_code", 2)) == int(expected)
        elif kind == "stdout_contains":
            passed = str(expected) in stdout
        elif kind == "stderr_contains":
            passed = str(expected) in stderr
        elif kind == "stdout_regex":
            try:
                passed = re.search(str(expected), stdout) is not None
            except re.error:
                passed = False
        elif kind == "duration_ms":
            passed = float(result.get("duration_ms") or 0) <= float(expected)
        else:
            passed = False
        evaluated.append({"id": str(raw.get("id") or f"assertion-{index}"), "type": kind, "expected": expected, "passed": passed})
    return evaluated


def _verify_artifact(sandbox: Path, artifact_path: Any, evidence_dir: Path, root: Path) -> dict[str, Any]:
    relative = str(artifact_path or "").strip()
    if not relative:
        return {"status": "BLOCK", "reason": "build_artifact_missing"}
    path = (sandbox / relative).resolve()
    if not _within(path, sandbox.resolve()) or not path.exists():
        return {"status": "BLOCK", "reason": "build_artifact_missing", "artifact_path": relative}
    if not path.is_file() and not path.is_dir():
        return {"status": "BLOCK", "reason": "build_artifact_invalid", "artifact_path": relative}
    digest = _hash_path(path)
    payload = {"path": relative, "sha256": digest, "kind": "directory" if path.is_dir() else "file"}
    evidence_dir.mkdir(parents=True, exist_ok=True)
    artifact_meta = evidence_dir / "artifact-sha256.json"
    artifact_meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "PASS", "artifact_path": relative, "sha256": digest, "evidence": _relative_or_str(artifact_meta, root)}


def _base_result(*, status: str, reason: str | None, review_id: str, snapshot: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": PRODUCT_TEST_SCHEMA,
        "adapter_version": PRODUCT_TEST_ADAPTER_VERSION,
        "status": status if status in PRODUCT_TEST_OUTCOMES else "HOLD",
        "reason": reason,
        "candidate_only": False,
        "review_id": review_id,
        "snapshot_head_sha": str(snapshot.get("head_sha") or ""),
        "build": None,
        "run_operations": [],
        "browser": None,
        "plan": dict(plan),
    }


def _persist_result(result: dict[str, Any], evidence_dir: Path, root: Path) -> dict[str, Any]:
    redacted, findings = redact_structure(result, prefix="product_test")
    safe_result = redacted if isinstance(redacted, dict) else dict(result)
    if findings:
        safe_result["redaction_findings"] = [item.as_dict() for item in findings]
    path = evidence_dir / "product-test-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe_result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    safe_result["result_path"] = _relative_or_str(path, root)
    return safe_result


def _readme_text(root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt"):
        path = root / name
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:100_000]
        except OSError:
            continue
    return ""


def _looks_like_build(command: str) -> bool:
    value = command.strip().lower()
    return value.startswith(_BUILD_PREFIXES) or bool(re.match(r"^python(?:3)?\s+.*(?:build|setup)\.py(?:\s|$)", value))


def _looks_like_run(command: str) -> bool:
    value = command.strip().lower()
    return (value.startswith(_RUN_PREFIXES) or (value.startswith("npm run ") and "build" not in value)) and not _looks_like_build(value)


def _matches_allowlist(command: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return False
    return any(_valid_pattern(pattern, command) for pattern in allowlist)


def _valid_pattern(pattern: str, command: str) -> bool:
    try:
        return re.fullmatch(pattern, command) is not None
    except re.error:
        return False


def _execution_environment(profile: Mapping[str, Any] | None) -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "VIRTUAL_ENV", "PYTHONPATH"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    if isinstance(profile, Mapping):
        configured = profile.get("configured") if isinstance(profile.get("configured"), Mapping) else {}
        for name in configured.get("credential_envs", []):
            if str(name) in os.environ:
                env[str(name)] = os.environ[str(name)]
    return env


def _infrastructure_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("modulenotfounderror", "no module named", "command not found", "cannot find module", "executable not found"))


def _read_result_text(value: Any) -> str:
    if not value:
        return ""
    path = Path(str(value))
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    else:
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(child.relative_to(path).as_posix().encode("utf-8"))
            with child.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "review"


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())
