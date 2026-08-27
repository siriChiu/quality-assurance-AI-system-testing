"""Bounded PTY probe for user-facing terminal applications.

This adapter is deliberately narrower than a TUI test framework.  It launches
an already-confirmed product entrypoint in a pseudo-terminal, captures a
redacted transcript, optionally sends an allowlisted set of keys, and reports
PASS only when explicit screen markers are observed.  It never infers product
truth from process exit alone.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pty
import selectors
import shlex
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .environment import environment_profile_status
from .runner import utc_now
from .security import ensure_safe_text, redact_structure

TUI_PROBE_SCHEMA = "quality-pilot.tui-probe.v1"
_ALLOWED_KEYS = {
    "ENTER": "\r",
    "RETURN": "\r",
    "ESC": "\x1b",
    "ESCAPE": "\x1b",
    "TAB": "\t",
    "SPACE": " ",
    "BACKSPACE": "\x7f",
    "UP": "\x1b[A",
    "DOWN": "\x1b[B",
    "LEFT": "\x1b[D",
    "RIGHT": "\x1b[C",
    "HOME": "\x1b[H",
    "END": "\x1b[F",
    "CTRL-C": "\x03",
    "CTRL-D": "\x04",
    "CTRL-L": "\x0c",
}


class TUIProbeError(RuntimeError):
    pass


def tui_probe(
    config: ProjectConfig,
    *,
    entrypoint: str | None = None,
    duration_seconds: float = 5.0,
    expected_markers: list[str] | None = None,
    keys: list[str] | None = None,
    width: int = 120,
    height: int = 32,
    confirm: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a bounded local/remote PTY probe using the confirmed environment.

    The command is intentionally gated twice: ``environment_profile_status``
    must be ready, and a non-dry invocation requires explicit ``confirm``.
    Remote execution uses argv-only ``ssh`` and does not invoke a shell.
    """
    profile = environment_profile_status(config)
    selected_entrypoint = str(entrypoint or profile.get("configured", {}).get("primary_entrypoint") or "").strip()
    markers = [str(item) for item in (expected_markers or []) if str(item).strip()]
    selected_keys = [str(item) for item in (keys or []) if str(item).strip()]
    base_payload = {
        "schema": TUI_PROBE_SCHEMA,
        "operation": "environment_tui_probe",
        "execution_mode": profile.get("execution_mode"),
        "entrypoint": selected_entrypoint,
        "expected_markers": markers,
        "keys": [_key_label(item) for item in selected_keys],
        "terminal": {"width": int(width), "height": int(height)},
        "environment_profile": _safe_environment_profile(profile),
        "authority": "explicit screen markers and transcript only; process exit is not sufficient",
    }
    if not profile.get("ready"):
        return {
            **base_payload,
            "status": "BLOCK",
            "test_outcome": "BLOCK",
            "blocked_reason": "environment_profile_required",
            "blockers": list(profile.get("blockers") or []),
            "next_action": "/quality-pilot environment configure --mode <local|remote>",
        }
    if not selected_entrypoint:
        return {**base_payload, "status": "BLOCK", "test_outcome": "BLOCK", "blocked_reason": "tui_entrypoint_missing"}
    if not markers and not dry_run:
        return {
            **base_payload,
            "status": "HOLD",
            "test_outcome": "HOLD",
            "blocked_reason": "tui_expected_marker_required",
            "message": "Provide at least one --expect marker; a live TUI without an oracle is not PASS.",
        }
    if not confirm and not dry_run:
        return {
            **base_payload,
            "status": "awaiting_confirmation",
            "test_outcome": "NOT_RUN",
            "next_action": "Re-run with --confirm after reviewing the side-effect boundary.",
        }
    try:
        ensure_safe_text(selected_entrypoint, context="tui entrypoint")
    except ValueError as exc:
        raise TUIProbeError("tui_entrypoint_redaction_failed_closed") from exc
    argv = _build_probe_argv(config, selected_entrypoint, profile)
    key_bytes = [_encode_key(item) for item in selected_keys]
    if dry_run:
        return {
            **base_payload,
            "status": "dry_run",
            "test_outcome": "NOT_RUN",
            "argv": _redact_argv(argv),
            "remote": profile.get("execution_mode") == "remote",
            "readiness": "planned_only",
        }

    started_at = utc_now()
    evidence_dir = config.paths.evidence / "tui-probe" / _run_slug()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    result = _run_pty(
        argv,
        cwd=config.root if profile.get("execution_mode") != "remote" else None,
        evidence_dir=evidence_dir,
        markers=markers,
        key_bytes=key_bytes,
        duration_seconds=max(0.1, min(float(duration_seconds), 300.0)),
        width=max(40, min(int(width), 300)),
        height=max(10, min(int(height), 120)),
    )
    result.update(base_payload)
    result["started_at"] = started_at
    result["ended_at"] = utc_now()
    result["argv"] = _redact_argv(argv)
    result["evidence_dir"] = _relative_or_str(evidence_dir, config.root)
    result_path = evidence_dir / "result.json"
    result["result_path"] = _relative_or_str(result_path, config.root)
    safe_result, findings = redact_structure(result, prefix="tui_probe")
    if findings:
        # Keep the persisted artifact honest about why the probe is HOLD; the
        # transcript itself has already been transformed before this point.
        result["redaction_findings"] = [item.as_dict() for item in findings]
        safe_result, _ = redact_structure(result, prefix="tui_probe")
    result_path.write_text(json.dumps(safe_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _restrict_file(result_path)
    return result


def _build_probe_argv(config: ProjectConfig, entrypoint: str, profile: dict[str, Any]) -> list[str]:
    try:
        command = shlex.split(entrypoint)
    except ValueError as exc:
        raise TUIProbeError("tui_entrypoint_invalid") from exc
    if not command or any(not token.strip() for token in command):
        raise TUIProbeError("tui_entrypoint_invalid")
    if profile.get("execution_mode") != "remote":
        return command
    target_name = str(profile.get("target", {}).get("env_name") or "QUALITY_PILOT_TARGET_HOST")
    target = os.environ.get(target_name, "").strip()
    if not target:
        raise TUIProbeError("remote_target_missing")
    # The target value is read only at execution time.  No shell is used and
    # the target is omitted from returned payloads.
    return [
        "ssh",
        "-tt",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        target,
        "--",
        *command,
    ]


def _run_pty(
    argv: list[str],
    *,
    cwd: Path | None,
    evidence_dir: Path,
    markers: list[str],
    key_bytes: list[bytes],
    duration_seconds: float,
    width: int,
    height: int,
) -> dict[str, Any]:
    master, slave = pty.openpty()
    _set_winsize(slave, width, height)
    process: subprocess.Popen[bytes] | None = None
    transcript = bytearray()
    started = time.monotonic()
    sent_keys = 0
    launch_error: str | None = None
    try:
        process = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            shell=False,
            cwd=str(cwd) if cwd else None,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        launch_error = type(exc).__name__
    finally:
        os.close(slave)
    if launch_error:
        os.close(master)
        return {
            "status": "BLOCK",
            "test_outcome": "BLOCK",
            "blocked_reason": "tui_process_launch_failed",
            "launch_error": launch_error,
            "markers_found": [],
            "markers_missing": markers,
            "exit_code": None,
        }

    os.set_blocking(master, False)
    selector = selectors.DefaultSelector()
    selector.register(master, selectors.EVENT_READ)
    try:
        while time.monotonic() - started < duration_seconds:
            for key, _ in selector.select(timeout=0.1):
                if key.fileobj != master:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        chunk = b""
                    else:
                        raise
                if chunk:
                    transcript.extend(chunk)
            if key_bytes and sent_keys < len(key_bytes) and time.monotonic() - started >= 0.25:
                os.write(master, key_bytes[sent_keys])
                sent_keys += 1
            if process.poll() is not None and not selector.select(timeout=0):
                break
        timed_out = process.poll() is None
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait(timeout=1)
        try:
            while True:
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                transcript.extend(chunk)
        except OSError:
            pass
        os.close(master)

    raw_text = transcript.decode("utf-8", errors="replace")
    safe_text, redaction_findings = redact_structure(raw_text, prefix="tui_transcript")
    transcript_path = evidence_dir / "transcript.log"
    transcript_path.write_text(str(safe_text), encoding="utf-8")
    _restrict_file(transcript_path)
    text_lower = str(safe_text).casefold()
    found = [marker for marker in markers if marker.casefold() in text_lower]
    missing = [marker for marker in markers if marker not in found]
    exit_code = process.returncode
    infrastructure_reason = _infrastructure_blocker_reason(text_lower)
    if redaction_findings:
        outcome = "HOLD"
        status = "HOLD"
        reason = "transcript_redaction_findings_require_review"
    elif not markers:
        outcome = "HOLD"
        status = "HOLD"
        reason = "tui_expected_marker_required"
    elif infrastructure_reason:
        outcome = "BLOCK"
        status = "BLOCK"
        reason = infrastructure_reason
    elif found == markers:
        outcome = "PASS"
        status = "PASS"
        reason = None
    else:
        # A missing screen oracle is evidence-incomplete, not a product FAIL.
        # Process exit is retained for diagnosis but cannot promote this path
        # to a code conclusion.
        outcome = "HOLD"
        status = "HOLD"
        reason = "expected_tui_marker_missing"
    return {
        "status": status,
        "test_outcome": outcome,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "sent_key_count": sent_keys,
        "markers_found": found,
        "markers_missing": missing,
        "transcript_path": str(transcript_path),
        "redaction_findings": [item.as_dict() for item in redaction_findings],
        "reason": reason,
    }


def _infrastructure_blocker_reason(text_lower: str) -> str | None:
    text = str(text_lower or "")
    if "rsp=0xc1" in text and "invalid command" in text:
        return "hardware_preflight_invalid_command"
    if "unable to send raw command" in text or "ipmitool raw" in text and "failed" in text:
        return "hardware_preflight_failed"
    if "permission denied" in text or "no such file or directory" in text or "command not found" in text:
        return "tui_runtime_precondition_failed"
    return None


def _restrict_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _set_winsize(fd: int, width: int, height: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))


def _encode_key(value: str) -> bytes:
    text = str(value or "").strip()
    upper = text.upper()
    if upper in _ALLOWED_KEYS:
        return _ALLOWED_KEYS[upper].encode()
    if upper.startswith("TEXT:"):
        literal = text[5:]
        if len(literal) > 128 or any(ord(char) < 0x20 and char not in "\t\r\n" for char in literal):
            raise TUIProbeError("tui_key_not_allowlisted")
        return literal.encode("utf-8")
    if len(text) == 1 and text.isprintable():
        return text.encode("utf-8")
    raise TUIProbeError(f"tui_key_not_allowlisted:{upper[:32]}")


def _key_label(value: str) -> str:
    text = str(value or "").strip()
    if text.upper() in _ALLOWED_KEYS:
        return text.upper()
    return "TEXT:<redacted>" if text.upper().startswith("TEXT:") else text[:1]


def _redact_argv(argv: list[str]) -> list[str]:
    # Never return the remote target or credential-bearing argv values.
    target_index = None
    if argv[:1] == ["ssh"] and "--" in argv:
        separator = argv.index("--")
        if separator > 0:
            target_index = separator - 1
    redacted: list[str] = []
    for index, item in enumerate(argv):
        if index == target_index:
            redacted.append("<remote-target>")
            continue
        safe, findings = redact_structure(str(item), prefix=f"tui.argv[{index}]")
        redacted.append("[REDACTED]" if findings else str(safe))
    return redacted


def _safe_environment_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": profile.get("status"),
        "execution_mode": profile.get("execution_mode"),
        "environment_confirmed": profile.get("environment_confirmed"),
        "blockers": profile.get("blockers", []),
        "target": {"required": (profile.get("target") or {}).get("required"), "env_name": (profile.get("target") or {}).get("env_name")},
        "fixtures": profile.get("fixtures", []),
    }


def _run_slug() -> str:
    return utc_now().replace(":", "").replace(".", "").replace("-", "")


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
