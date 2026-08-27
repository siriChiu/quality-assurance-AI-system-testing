from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import CaseContract, CommandAssertion, CommandContract
from .security import redact_text as _redact_text

DEFAULT_TIMEOUT_SEC = 120
TIMEOUT_ENV = "QUALITY_PILOT_RUN_TIMEOUT_SEC"
ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "TMPDIR",
    "TEMP",
    "TMP",
}
RISK_PATTERNS = [
    r"\brm\s+-[^\n;|&]*r[^\n;|&]*\s+/",
    r"\brm\s+-[^\n;|&]*f[^\n;|&]*\s+/",
    r">\s*/dev/(?:sd|nvme|disk)",
    r"\bdd\s+.*\bof=/dev/",
    r"\bmkfs(?:\.\w+)?\b",
    r"\bshutdown\b|\breboot\b|\bpoweroff\b",
    r"\bchmod\s+-R\s+777\s+/",
    r"\bchown\s+-R\b.*\s+/",
    r"\bcurl\b.*\|\s*(?:sh|bash)",
    r"\bwget\b.*\|\s*(?:sh|bash)",
]


@dataclass(frozen=True)
class RunContext:
    root: Path
    evidence_dir: Path
    # CLI supplies the redacted environment profile.  Direct library callers
    # may omit it to preserve the existing low-level runner behaviour.
    environment_profile: dict[str, Any] | None = None
    adapter_config: Any | None = None
    adapter_snapshot: dict[str, Any] | None = None
    adapter_review_id: str | None = None
    product_python: Path | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_case(contract: CaseContract, context: RunContext, *, dry_run: bool = False) -> dict[str, Any]:
    started_at = utc_now()
    case_type = str(contract.raw.get("case_type") or "command")
    if case_type in {"product", "product_build", "product_operation", "playwright_ui"}:
        if context.adapter_config is None or context.adapter_snapshot is None or not context.adapter_review_id:
            return _blocked_case(contract, context.root, context.evidence_dir / contract.case_id, started_at, blocked_reason="product_case_adapter_context_missing")
        from .product_case_adapter import execute_product_case
        return execute_product_case(contract, context, config=context.adapter_config, snapshot=context.adapter_snapshot, review_id=context.adapter_review_id, dry_run=dry_run)
    case_evidence_dir = context.evidence_dir / contract.case_id
    case_evidence_dir.mkdir(parents=True, exist_ok=True)
    if not dry_run and _review_required_before_run(contract):
        return _blocked_case(contract, context.root, case_evidence_dir, started_at)
    if not dry_run:
        environment_block = _environment_blocker(contract, context.environment_profile)
        if environment_block:
            return _blocked_case(
                contract,
                context.root,
                case_evidence_dir,
                started_at,
                blocked_reason=environment_block["reason"],
                blocked_details=environment_block,
                environment_profile=context.environment_profile,
            )
    command_results = []
    status = "PASS"
    exit_code = 0
    for command in contract.commands:
        result = _dry_command(command, case_evidence_dir) if dry_run else _run_command(command, context.root, case_evidence_dir, context.environment_profile)
        command_results.append(result)
        if result.get("status") == "BLOCK":
            status = "BLOCK"
            exit_code = result["exit_code"]
            break
        if result.get("status") == "FAIL":
            status = "FAIL"
            # A semantic oracle can fail even when the product process exits 0.
            # The case-level code represents the QA result; the command's actual
            # process code remains available in commands[].exit_code.
            exit_code = result["exit_code"] if result["exit_code"] != command.expected_exit_code else 1
            break
    ended_at = utc_now()
    swqa_gate = evaluate_swqa_gate(contract, command_results)
    if not dry_run and status == "PASS" and not swqa_gate["allowed"]:
        status = "BLOCK"
        exit_code = 2
    oracle_profile = _case_oracle_profile(contract)
    quality_pilot = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    payload = {
        "case_id": contract.case_id,
        "title": contract.title,
        "status": "NOT_RUN" if dry_run else status,
        "partial_probe": _is_partial_probe(contract) or bool(oracle_profile["oracle_partial"]),
        "official_result": not (_is_partial_probe(contract) or bool(oracle_profile["oracle_partial"])),
        "truth_status": (
            "NOT_RUN"
            if dry_run
            else ("HOLD" if (_is_partial_probe(contract) or bool(oracle_profile["oracle_partial"])) else status)
        ),
        "commands": command_results,
        "evidence": sorted(_relative_or_str(path, context.root) for path in case_evidence_dir.glob("*")),
        "contract_hash": contract.contract_hash,
        "run_id": None,
        "confirmed_bug": bool(contract.raw.get("confirmed_bug") or quality_pilot.get("confirmed_bug")),
        "evidence_profile": quality_pilot.get("evidence_profile") or contract.raw.get("evidence_profile"),
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": 0 if dry_run else exit_code,
        "swqa_gate": swqa_gate,
        "environment_profile": _safe_environment_profile(context.environment_profile),
        **oracle_profile,
    }
    result_path = case_evidence_dir / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    payload["result_path"] = _relative_or_str(result_path, context.root)
    return payload


def stamp_result_run_id(result: dict[str, Any], root: Path, run_id: str) -> dict[str, Any]:
    """Bind a case result and its persisted result.json to the enclosing run."""
    result["run_id"] = run_id
    raw_path = result.get("result_path")
    if not raw_path:
        return result
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = root / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(payload, dict):
        return result
    payload["run_id"] = run_id
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _review_required_before_run(contract: CaseContract) -> bool:
    qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    return bool(qa.get("review_required_before_run"))


def _is_partial_probe(contract: CaseContract) -> bool:
    qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    wiki = contract.raw.get("wiki") if isinstance(contract.raw.get("wiki"), dict) else {}
    return bool(contract.raw.get("partial_probe") or qa.get("partial_probe") or wiki.get("partial_probe"))


def _requires_prepared_environment(contract: CaseContract) -> bool:
    qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    requirements = qa.get("environment_requirements")
    source = str(qa.get("safe_command_source_type") or "")
    return bool(
        qa.get("requires_prepared_environment")
        or (isinstance(requirements, list) and requirements)
        or source in {"prepared_environment_readonly_product_command", "readme_cli_operation"}
    )


def _environment_blocker(contract: CaseContract, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not _requires_prepared_environment(contract) or profile is None:
        return None
    if profile.get("ready"):
        return None
    blockers = [str(item) for item in profile.get("blockers", []) if item]
    return {
        "reason": "environment_profile_required",
        "blockers": blockers,
        "case_requires_prepared_environment": True,
    }


def _safe_environment_profile(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not profile:
        return None
    # The status object is intentionally already redacted; copy only stable
    # readiness fields into evidence so later reports explain a BLOCK without
    # leaking target or credential values.
    return {
        "status": profile.get("status"),
        "execution_mode": profile.get("execution_mode"),
        "environment_confirmed": profile.get("environment_confirmed"),
        "blockers": profile.get("blockers", []),
        "target": profile.get("target", {}),
        "fixtures": profile.get("fixtures", []),
        "credentials": profile.get("credentials", []),
        "remote_preflight": profile.get("remote_preflight"),
    }


def evaluate_swqa_gate(contract: CaseContract, command_results: list[dict[str, Any]]) -> dict[str, Any]:
    qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    gates = qa.get("gates") if isinstance(qa.get("gates"), dict) else {}
    enforce = bool(qa.get("enforce_swqa_gates"))
    if not enforce:
        return {"enforced": False, "allowed": True, "reason_codes": []}
    dimensions = {
        str(item)
        for item in contract.raw.get("swqa_dimensions", contract.raw.get("swqa_expansion", []))
        if item
    }
    required = set(gates.get("required_dimensions") or ["exact_reproduction", "sibling_surface", "boundary", "invalid_input"])
    reasons = [f"missing_dimension:{item}" for item in sorted(required - dimensions)]
    side_effect_required = bool(gates.get("side_effect_evidence_required") or "side_effect_safe" in dimensions)
    if side_effect_required and not _has_side_effect_evidence(command_results):
        reasons.append("missing_side_effect_evidence")
    return {
        "enforced": True,
        "allowed": not reasons,
        "reason_codes": reasons,
        "required_dimensions": sorted(required),
        "present_dimensions": sorted(dimensions),
    }


def _has_side_effect_evidence(command_results: list[dict[str, Any]]) -> bool:
    text = json.dumps(command_results, ensure_ascii=False).lower()
    return any(marker in text for marker in ["side_effect", "readonly", "read-only", "dry-run", "safe_probe"])


def _blocked_case(
    contract: CaseContract,
    root: Path,
    evidence_dir: Path,
    started_at: str,
    *,
    blocked_reason: str = "review_required_before_run",
    blocked_details: dict[str, Any] | None = None,
    environment_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ended_at = utc_now()
    oracle_profile = _case_oracle_profile(contract)
    quality_pilot = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    command_results = [
        {
            "id": command.id,
            "run": command.run,
            "expected_exit_code": command.expected_exit_code,
            "exit_code": 2,
            "status": "BLOCK",
            "started_at": None,
            "ended_at": None,
            "stdout": None,
            "stderr": None,
            "rc": None,
            "meta": None,
            "blocked_reason": blocked_reason,
            "duration_ms": None,
            "oracle_results": _not_evaluated_oracle_results(command, blocked_reason),
            **_command_oracle_profile(command),
        }
        for command in contract.commands
    ]
    payload = {
        "case_id": contract.case_id,
        "title": contract.title,
        "status": "BLOCK",
        "partial_probe": _is_partial_probe(contract) or bool(oracle_profile["oracle_partial"]),
        "official_result": False,
        "truth_status": "BLOCK",
        "commands": command_results,
        "evidence": [],
        "contract_hash": contract.contract_hash,
        "run_id": None,
        "confirmed_bug": bool(contract.raw.get("confirmed_bug") or quality_pilot.get("confirmed_bug")),
        "evidence_profile": quality_pilot.get("evidence_profile") or contract.raw.get("evidence_profile"),
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": 2,
        "blocked_reason": blocked_reason,
        "environment_profile": _safe_environment_profile(environment_profile),
        **(blocked_details or {}),
        **oracle_profile,
    }
    result_path = evidence_dir / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    payload["result_path"] = _relative_or_str(result_path, root)
    payload["evidence"] = [payload["result_path"]]
    return payload


def _run_command(
    command: CommandContract,
    root: Path,
    evidence_dir: Path,
    environment_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stdout_path = evidence_dir / f"{command.id}.stdout.log"
    stderr_path = evidence_dir / f"{command.id}.stderr.log"
    rc_path = evidence_dir / f"{command.id}.rc"
    meta_path = evidence_dir / f"{command.id}.meta"
    started_at = utc_now()
    timeout_sec = _timeout_sec()
    risk = classify_command_risk(command.run)
    meta_path.write_text(json.dumps({
        "id": command.id,
        "run": command.run,
        "expected_exit_code": command.expected_exit_code,
        "started_at": started_at,
        "timeout_sec": timeout_sec,
        "risk": risk,
        "assertions": [_assertion_payload(assertion, source) for assertion, source in _effective_assertions(command)],
    }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if risk["decision"] == "block":
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(risk["reason"] + "\n", encoding="utf-8")
        rc_path.write_text("2\n", encoding="utf-8")
        ended_at = utc_now()
        return _command_payload(
            command,
            root=root,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            rc_path=rc_path,
            meta_path=meta_path,
            exit_code=2,
            status="BLOCK",
            started_at=started_at,
            ended_at=ended_at,
            blocked_reason=risk["reason"],
            duration_ms=0.0,
            oracle_results=_not_evaluated_oracle_results(command, risk["reason"]),
        )
    monotonic_started = time.monotonic()
    try:
        completed = subprocess.run(
            command.run,
            cwd=root,
            shell=True,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
            env=_runner_env(environment_profile),
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        duration_ms = (time.monotonic() - monotonic_started) * 1000
        oracle_results = _evaluate_command_oracles(
            command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        )
        status = "PASS" if all(result["passed"] for result in oracle_results) else "FAIL"
        blocked_reason = None
        if exit_code in {126, 127} and command.expected_exit_code not in {126, 127}:
            status = "BLOCK"
            blocked_reason = "executable_not_found" if exit_code == 127 else "executable_not_executable"
            oracle_results = _not_evaluated_oracle_results(command, blocked_reason)
    except subprocess.TimeoutExpired as exc:
        duration_ms = (time.monotonic() - monotonic_started) * 1000
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + f"\ncommand timed out after {timeout_sec}s"
        status = "BLOCK"
        blocked_reason = "command_timeout"
        oracle_results = _not_evaluated_oracle_results(command, blocked_reason)
    stdout_path.write_text(redact_secrets(stdout), encoding="utf-8")
    stderr_path.write_text(redact_secrets(stderr), encoding="utf-8")
    rc_path.write_text(f"{exit_code}\n", encoding="utf-8")
    ended_at = utc_now()
    return _command_payload(
        command,
        root=root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        rc_path=rc_path,
        meta_path=meta_path,
        exit_code=exit_code,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        blocked_reason=blocked_reason,
        duration_ms=duration_ms,
        oracle_results=oracle_results,
    )


def _command_payload(
    command: CommandContract,
    *,
    root: Path,
    stdout_path: Path,
    stderr_path: Path,
    rc_path: Path,
    meta_path: Path,
    exit_code: int,
    status: str,
    started_at: str,
    ended_at: str,
    blocked_reason: str | None = None,
    duration_ms: float | None = None,
    oracle_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": command.id,
        "run": command.run,
        "expected_exit_code": command.expected_exit_code,
        "exit_code": exit_code,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "stdout": _relative_or_str(stdout_path, root),
        "stderr": _relative_or_str(stderr_path, root),
        "rc": _relative_or_str(rc_path, root),
        "meta": _relative_or_str(meta_path, root),
        "evidence_sha256": {
            "stdout": _sha256_file(stdout_path),
            "stderr": _sha256_file(stderr_path),
            "rc": _sha256_file(rc_path),
            "meta": _sha256_file(meta_path),
        },
        "oracle_results": oracle_results or [],
        **_command_oracle_profile(command),
    }
    if blocked_reason:
        payload["blocked_reason"] = blocked_reason
    return payload


def _dry_command(command: CommandContract, evidence_dir: Path) -> dict[str, Any]:
    return {
        "id": command.id,
        "run": command.run,
        "expected_exit_code": command.expected_exit_code,
        "exit_code": 0,
        "status": "NOT_RUN",
        "started_at": None,
        "ended_at": None,
        "stdout": None,
        "stderr": None,
        "rc": None,
        "meta": str(evidence_dir / f"{command.id}.meta"),
        "risk": classify_command_risk(command.run),
        "duration_ms": None,
        "oracle_results": _not_evaluated_oracle_results(command, "dry_run"),
        **_command_oracle_profile(command),
    }


def _effective_assertions(command: CommandContract) -> list[tuple[CommandAssertion, str]]:
    assertions = [(assertion, "contract") for assertion in command.assertions]
    if not any(assertion.type == "exit_code" for assertion in command.assertions):
        assertions.insert(
            0,
            (
                CommandAssertion(type="exit_code", operator="equals", expected=command.expected_exit_code),
                "legacy_expected_exit_code",
            ),
        )
    return assertions


def _evaluate_command_oracles(
    command: CommandContract,
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_ms: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for assertion, source in _effective_assertions(command):
        if assertion.type == "exit_code":
            actual: Any = exit_code
            passed = actual == assertion.expected
        elif assertion.type == "duration_ms":
            actual = round(duration_ms, 3)
            if assertion.operator == "less_than":
                passed = duration_ms < float(assertion.expected)
            else:
                passed = duration_ms <= float(assertion.expected)
        else:
            actual_text = stdout if assertion.type == "stdout" else stderr
            actual = {
                "length": len(actual_text),
                "excerpt": redact_secrets(actual_text[:500]),
            }
            if assertion.operator == "contains":
                passed = str(assertion.expected) in actual_text
            elif assertion.operator == "regex":
                passed = re.search(str(assertion.expected), actual_text) is not None
            else:
                passed = actual_text == assertion.expected
        result = {
            **_assertion_payload(assertion, source),
            "actual": actual,
            "passed": passed,
            "status": "PASS" if passed else "FAIL",
        }
        if not passed:
            result["reason"] = "oracle_mismatch"
        results.append(result)
    return results


def _not_evaluated_oracle_results(command: CommandContract, reason: str) -> list[dict[str, Any]]:
    return [
        {
            **_assertion_payload(assertion, source),
            "actual": None,
            "passed": None,
            "status": "NOT_EVALUATED",
            "reason": reason,
        }
        for assertion, source in _effective_assertions(command)
    ]


def _assertion_payload(assertion: CommandAssertion, source: str) -> dict[str, Any]:
    return {
        "id": assertion.id or "expected-exit-code",
        "type": assertion.type,
        "operator": assertion.operator,
        "expected": assertion.expected,
        "source": source,
    }


def _command_oracle_profile(command: CommandContract) -> dict[str, Any]:
    has_semantic_oracle = any(assertion.type != "exit_code" for assertion in command.assertions)
    return {
        "oracle_strength": "semantic" if has_semantic_oracle else "exit_only",
        "oracle_partial": not has_semantic_oracle,
    }


def _case_oracle_profile(contract: CaseContract) -> dict[str, Any]:
    semantic_count = sum(
        1 for command in contract.commands if any(assertion.type != "exit_code" for assertion in command.assertions)
    )
    if semantic_count == len(contract.commands):
        strength = "semantic"
    elif semantic_count:
        strength = "mixed"
    else:
        strength = "exit_only"
    return {
        "oracle_strength": strength,
        "oracle_partial": semantic_count != len(contract.commands),
    }


def classify_command_risk(command: str) -> dict[str, Any]:
    text = str(command or "")
    for pattern in RISK_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return {
                "level": "high",
                "decision": "block",
                "reason": "unsafe_command_pattern",
                "pattern": pattern,
            }
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = []
    executable = tokens[0] if tokens else ""
    if executable in {"python", "python3", "pytest", "sh", "bash", "go", "npm", "node", "ruby", "perl", "echo", "true", "false"}:
        return {"level": "low", "decision": "allow", "reason": "known_safe_probe_prefix"}
    return {"level": "medium", "decision": "allow", "reason": "no_blocked_pattern"}


def redact_secrets(text: str) -> str:
    """Redact known secret formats while preserving the runner API."""
    redacted, _findings = _redact_text(str(text or ""))
    return re.sub(r"\[REDACTED:[^\]]+\]", "[REDACTED]", redacted)


def _runner_env(environment_profile: dict[str, Any] | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in ENV_ALLOWLIST or key.startswith("QUALITY_PILOT_"):
            env[key] = value
    configured = environment_profile.get("configured", {}) if isinstance(environment_profile, dict) else {}
    names = [configured.get("target_host_env"), *(configured.get("credential_envs") or [])]
    for name in names:
        if isinstance(name, str) and name and name in os.environ:
            env[name] = os.environ[name]
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    return env


def _timeout_sec() -> int:
    try:
        value = int(os.environ.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SEC)))
    except ValueError:
        value = DEFAULT_TIMEOUT_SEC
    return max(1, min(value, 3600))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
