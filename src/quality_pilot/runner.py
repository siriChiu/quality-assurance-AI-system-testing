from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import CaseContract, CommandContract

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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_case(contract: CaseContract, context: RunContext, *, dry_run: bool = False) -> dict[str, Any]:
    started_at = utc_now()
    case_evidence_dir = context.evidence_dir / contract.case_id
    case_evidence_dir.mkdir(parents=True, exist_ok=True)
    if not dry_run and _review_required_before_run(contract):
        return _blocked_case(contract, context.root, case_evidence_dir, started_at)
    command_results = []
    status = "PASS"
    exit_code = 0
    for command in contract.commands:
        result = _dry_command(command, case_evidence_dir) if dry_run else _run_command(command, context.root, case_evidence_dir)
        command_results.append(result)
        if result.get("status") == "BLOCK":
            status = "BLOCK"
            exit_code = result["exit_code"]
            break
        if result["exit_code"] != command.expected_exit_code:
            status = "FAIL"
            exit_code = result["exit_code"]
            break
    ended_at = utc_now()
    swqa_gate = evaluate_swqa_gate(contract, command_results)
    if not dry_run and status == "PASS" and not swqa_gate["allowed"]:
        status = "BLOCK"
        exit_code = 2
    payload = {
        "case_id": contract.case_id,
        "title": contract.title,
        "status": "NOT_RUN" if dry_run else status,
        "commands": command_results,
        "evidence": sorted(_relative_or_str(path, context.root) for path in case_evidence_dir.glob("*")),
        "contract_hash": contract.contract_hash,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": 0 if dry_run else exit_code,
        "swqa_gate": swqa_gate,
    }
    result_path = case_evidence_dir / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    payload["result_path"] = _relative_or_str(result_path, context.root)
    return payload


def _review_required_before_run(contract: CaseContract) -> bool:
    qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    return bool(qa.get("review_required_before_run"))


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


def _blocked_case(contract: CaseContract, root: Path, evidence_dir: Path, started_at: str) -> dict[str, Any]:
    ended_at = utc_now()
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
            "blocked_reason": "review_required_before_run",
        }
        for command in contract.commands
    ]
    payload = {
        "case_id": contract.case_id,
        "title": contract.title,
        "status": "BLOCK",
        "commands": command_results,
        "evidence": [],
        "contract_hash": contract.contract_hash,
        "started_at": started_at,
        "ended_at": ended_at,
        "exit_code": 2,
        "blocked_reason": "review_required_before_run",
    }
    result_path = evidence_dir / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    payload["result_path"] = _relative_or_str(result_path, root)
    payload["evidence"] = [payload["result_path"]]
    return payload


def _run_command(command: CommandContract, root: Path, evidence_dir: Path) -> dict[str, Any]:
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
        )
    try:
        completed = subprocess.run(
            command.run,
            cwd=root,
            shell=True,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
            env=_runner_env(),
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        status = "PASS" if completed.returncode == command.expected_exit_code else "FAIL"
        blocked_reason = None
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + f"\ncommand timed out after {timeout_sec}s"
        status = "BLOCK"
        blocked_reason = "command_timeout"
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
) -> dict[str, Any]:
    payload = {
        "id": command.id,
        "run": command.run,
        "expected_exit_code": command.expected_exit_code,
        "exit_code": exit_code,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
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
    redacted = str(text or "")
    patterns = [
        r"sk-[A-Za-z0-9_-]{12,}",
        r"ghp_[A-Za-z0-9_]{12,}",
        r"(?i)(password|passwd|api[_-]?key|token|secret)\s*[:=]\s*[^ \n\r\t]+",
        r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    ]
    for pattern in patterns:
        redacted = re.sub(pattern, lambda match: _redacted_secret(match.group(0)), redacted)
    return redacted


def _redacted_secret(value: str) -> str:
    if "=" in value or ":" in value:
        separator = "=" if "=" in value else ":"
        key = value.split(separator, 1)[0]
        return f"{key}{separator}[REDACTED]"
    return "[REDACTED]"


def _runner_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in ENV_ALLOWLIST or key.startswith("QUALITY_PILOT_"):
            env[key] = value
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
