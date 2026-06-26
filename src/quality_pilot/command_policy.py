from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .runtime_profile import configured_runtime_binary, primary_runtime_binary, primary_runtime_entrypoint


GENERATED_COMMAND_POLICY_SCHEMA = "quality-pilot.generated-command-policy.v1"


def validate_generated_command(
    config: ProjectConfig,
    run: str,
    *,
    source_type: str = "",
    allow_user_confirmed_runner: bool = False,
) -> dict[str, Any]:
    command = str(run or "").strip()
    runtime_binary = primary_runtime_binary(config)
    configured_binary = configured_runtime_binary(config)
    configured_entrypoint = primary_runtime_entrypoint(config)
    explicitly_configured = _uses_configured_entrypoint(command, configured_entrypoint)
    uses_runtime = _uses_runtime_binary(command, runtime_binary) or bool(
        configured_binary and _uses_runtime_binary(command, configured_binary)
    )
    disallowed_reason = _disallowed_generated_command_reason(command)

    allowed = True
    reasons: list[str] = []
    command_kind = "product_runtime" if uses_runtime else "unknown"
    if not command:
        allowed = False
        reasons.append("empty_command")
    if disallowed_reason and not explicitly_configured:
        allowed = False
        reasons.append(disallowed_reason)
    if not uses_runtime and not explicitly_configured:
        if allow_user_confirmed_runner:
            command_kind = "user_confirmed_runner"
        else:
            allowed = False
            reasons.append("command_does_not_use_product_runtime")
    if explicitly_configured:
        command_kind = "configured_product_runner"

    return {
        "schema": GENERATED_COMMAND_POLICY_SCHEMA,
        "allowed": allowed,
        "reasons": _unique(reasons),
        "command_kind": command_kind,
        "uses_product_runtime": bool(uses_runtime or explicitly_configured),
        "runtime_binary": runtime_binary,
        "configured_entrypoint": configured_entrypoint,
        "source_type": source_type,
        "allow_user_confirmed_runner": bool(allow_user_confirmed_runner),
    }


def validate_generated_contract_commands(config: ProjectConfig, contract: dict[str, Any]) -> list[dict[str, Any]]:
    qa = contract.get("quality_pilot") if isinstance(contract.get("quality_pilot"), dict) else {}
    source = contract.get("source") if isinstance(contract.get("source"), dict) else {}
    safe_runner = source.get("safe_runner") if isinstance(source.get("safe_runner"), dict) else {}
    source_type = str(qa.get("safe_command_source_type") or safe_runner.get("source_type") or "")
    allow_user_confirmed = source_type == "user_confirmed"
    findings: list[dict[str, Any]] = []
    commands = contract.get("commands") if isinstance(contract.get("commands"), list) else []
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            continue
        result = validate_generated_command(
            config,
            str(command.get("run") or ""),
            source_type=source_type,
            allow_user_confirmed_runner=allow_user_confirmed,
        )
        if result.get("allowed"):
            continue
        findings.append(
            {
                "command_index": index,
                "command_id": command.get("id"),
                "run": command.get("run"),
                "policy": result,
            }
        )
    return findings


def generated_contract_policy_violation_message(case_id: str, violations: list[dict[str, Any]]) -> str:
    first = violations[0] if violations else {}
    policy = first.get("policy") if isinstance(first.get("policy"), dict) else {}
    reasons = policy.get("reasons") if isinstance(policy.get("reasons"), list) else []
    reason = ",".join(str(item) for item in reasons) or "generated_command_policy_violation"
    return f"{case_id}: generated command rejected by product-runtime policy ({reason})"


def _disallowed_generated_command_reason(command: str) -> str:
    normalized = re.sub(r"\s+", " ", command.strip().lower())
    if not normalized:
        return "empty_command"
    if "__quality_pilot_invalid_command__" in command:
        return "synthetic_invalid_command"
    if "repo-only probe" in normalized or "safe repo probe" in normalized:
        return "repo_only_static_check"
    if re.search(r"(^|['\";(&|]\s*)python3?\s+-c\b", normalized):
        return "python_inline_metadata_check"
    if re.search(r"(^|['\";(&|]\s*)python3?\s+-m\s+compileall\b", normalized) or re.search(r"(^|['\";(&|]\s*)compileall\b", normalized):
        return "compileall_metadata_check"
    if re.search(r"(^|['\";(&|]\s*)go\s+test\b", normalized):
        return "go_test_developer_command"
    if re.search(r"(^|['\";(&|]\s*)go\s+run\b", normalized):
        return "go_run_developer_command"
    if re.search(r"(^|['\";(&|]\s*)pytest\b", normalized) or re.search(r"(^|['\";(&|]\s*)python3?\s+-m\s+pytest\b", normalized):
        return "pytest_developer_command"
    return ""


def _uses_configured_entrypoint(command: str, entrypoint: str) -> bool:
    configured = str(entrypoint or "").strip()
    if not command or not configured:
        return False
    if configured in command:
        return True
    configured_tokens = _tokens(configured)
    command_tokens = _tokens(command)
    if not configured_tokens or not command_tokens:
        return False
    return command_tokens[: len(configured_tokens)] == configured_tokens


def _uses_runtime_binary(command: str, runtime_binary: str) -> bool:
    if not command or not runtime_binary:
        return False
    if "QUALITY_PILOT_BINARY" in command:
        return True
    binary_names = {runtime_binary, Path(runtime_binary).name}
    for token in _tokens(command)[:8]:
        cleaned = token.strip("'\"")
        if cleaned in binary_names or Path(cleaned).name in binary_names:
            return True
    return False


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
