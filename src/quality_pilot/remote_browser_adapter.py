"""Remote SSH browser lifecycle adapter.

The remote adapter owns only the transport boundary.  Playwright still runs in
this process, against an SSH tunnel to the remote product.  Secrets printed in
runtime URLs remain in memory only and are never written to evidence.
"""
from __future__ import annotations

import json
import re
import shlex
import socket
import subprocess
import tempfile
import shutil
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .browser_adapter import _extract_discovered_url, _url_sensitive_values, run_browser_test
from .security import redact_text

REMOTE_BROWSER_SCHEMA = "quality-pilot.remote-browser-adapter.v1"


def run_remote_browser_test(
    settings: Mapping[str, Any],
    *,
    environment_profile: Mapping[str, Any],
    evidence_dir: Path,
    contract_identity_hash: str,
    root: Path,
    case_id: str,
    run_id: str,
    playwright_python: str | Path | None = None,
    timeout_ms: int = 60_000,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Launch the configured product over SSH and run local Playwright via tunnel."""
    runtime = environment_profile.get("configured") if isinstance(environment_profile.get("configured"), Mapping) else {}
    host = str(runtime.get("ssh_host") or environment_profile.get("target", {}).get("value") or "").strip()
    repo = str(runtime.get("remote_repo") or "").strip()
    remote_python = str(runtime.get("remote_python") or "").strip()
    if not host or not repo or not remote_python:
        return _result(case_id, run_id, "BLOCK", "remote_browser_configuration_missing", contract_identity_hash)
    command = str(settings.get("start_command") or "").strip()
    if not command:
        return _result(case_id, run_id, "BLOCK", "browser_contract_missing", contract_identity_hash)
    safe_command, command_reason = _safe_remote_command(command, remote_python)
    if safe_command is None:
        return _result(case_id, run_id, "BLOCK", f"unsafe_remote_browser_command:{command_reason}", contract_identity_hash, evidence_origin="remote")
    if dry_run:
        return _result(case_id, run_id, "PLANNED", "dry_run", contract_identity_hash, evidence_origin="remote")
    preflight = environment_profile.get("remote_preflight") if isinstance(environment_profile.get("remote_preflight"), Mapping) else None
    source_identity = preflight.get("source_identity") if isinstance(preflight, Mapping) and isinstance(preflight.get("source_identity"), Mapping) else {}
    if not preflight:
        return _result(case_id, run_id, "BLOCK", "REMOTE_PREFLIGHT_REQUIRED", contract_identity_hash, evidence_origin="remote")
    if str(source_identity.get("status") or "") != "VERIFIED":
        reason = "REMOTE_SOURCE_MISMATCH" if source_identity.get("status") == "MISMATCH" else ("REMOTE_SOURCE_DIRTY" if source_identity.get("status") == "DIRTY" else "REMOTE_SOURCE_UNVERIFIED")
        return _result(case_id, run_id, "BLOCK", reason, contract_identity_hash, evidence_origin="remote")
    if str(preflight.get("status") or "") != "READY":
        return _result(case_id, run_id, "BLOCK", "REMOTE_PREFLIGHT_REQUIRED", contract_identity_hash, evidence_origin="remote")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix="quality-pilot-remote-browser-"))
    stdout_path = temporary_dir / "remote-server.stdout.log"
    stderr_path = temporary_dir / "remote-server.stderr.log"
    persisted_stdout_path = evidence_dir / "remote-server.stdout.log"
    persisted_stderr_path = evidence_dir / "remote-server.stderr.log"
    meta_path = evidence_dir / "remote-server.meta.json"
    remote_process: subprocess.Popen[bytes] | None = None
    tunnel: subprocess.Popen[bytes] | None = None
    tunnel_port: int | None = None
    remote_pid: int | None = None
    result: dict[str, Any] | None = None
    sensitive_values: list[str] = []

    def finish(value: dict[str, Any]) -> dict[str, Any]:
        nonlocal result
        result = value
        return value
    try:
        # The complete remote command is one SSH argument.  The login shell is
        # intentional here because cwd + exec must be composed remotely.
        remote_command = (
            f"cd {shlex.quote(repo)} && "
            f"setsid sh -c {shlex.quote('exec ' + safe_command)} & "
            "pid=$!; printf 'QUALITY_PILOT_REMOTE_PID:%s\\n' \"$pid\" >&2; "
            "wait \"$pid\""
        )
        out = stdout_path.open("wb")
        err = stderr_path.open("wb")
        try:
            remote_process = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", host, remote_command],
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            out.close()
            err.close()

        pattern = str(settings.get("url_pattern") or r"https?://[^\s]+")
        deadline = time.monotonic() + timeout_ms / 1000
        remote_url = None
        while time.monotonic() < deadline:
            text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
            stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
            pid_match = re.search(r"QUALITY_PILOT_REMOTE_PID:(\d+)", stderr_text)
            if pid_match:
                remote_pid = int(pid_match.group(1))
            match = re.search(pattern, text)
            if match:
                remote_url = _extract_discovered_url(match.group(0))
                break
            if remote_process.poll() is not None:
                break
            time.sleep(0.05)
        if not remote_url:
            return finish(_result(case_id, run_id, "BLOCK", "remote_browser_url_discovery_failed", contract_identity_hash, evidence_origin="remote"))
        sensitive_values = _url_sensitive_values(remote_url)
        parsed = urlsplit(remote_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            return finish(_result(case_id, run_id, "BLOCK", "remote_browser_url_invalid", contract_identity_hash, evidence_origin="remote"))

        tunnel_port = _free_port()
        tunnel = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-N", "-L", f"127.0.0.1:{tunnel_port}:{parsed.hostname}:{parsed.port}", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.25)
        if tunnel.poll() is not None:
            return finish(_result(case_id, run_id, "BLOCK", "remote_browser_tunnel_failed", contract_identity_hash, evidence_origin="remote"))

        local_url = urlunsplit((parsed.scheme, f"127.0.0.1:{tunnel_port}", parsed.path, parsed.query, parsed.fragment))
        browser_settings = dict(settings)
        browser_settings["url"] = local_url
        browser_settings.pop("url_discovery", None)
        browser_settings["start_command"] = command
        result = run_browser_test(
            browser_settings,
            cwd=root,
            evidence_dir=evidence_dir / "playwright",
            contract_identity_hash=contract_identity_hash,
            environment_profile={"ready": True, "configured": {}},
            timeout_ms=timeout_ms,
            root=root,
            playwright_python=playwright_python,
            case_id=case_id,
            run_id=run_id,
            prestarted=True,
            server_url=local_url,
            server_stdout_path=stdout_path,
            server_stderr_path=stderr_path,
            server_command=command,
        )
        result["evidence_origin"] = "remote"
        result["remote_url_redacted"] = _redact_url(remote_url)
        # The local Playwright adapter reports the navigated URL; replace it
        # before the result can be persisted because the query may contain the
        # per-run browser token.
        result["url"] = _redact_url(remote_url)
        result["tunnel"] = {"status": "PASS", "local_port": tunnel_port}
        result.setdefault("evidence", {}).update({
            "remote_server_stdout": _relative_or_str(persisted_stdout_path, root),
            "remote_server_stderr": _relative_or_str(persisted_stderr_path, root),
            "remote_server_meta": _relative_or_str(meta_path, root),
        })
        return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        return finish(_result(case_id, run_id, "BLOCK", "remote_browser_transport_failed", contract_identity_hash, evidence_origin="remote", error=type(exc).__name__))
    finally:
        cleanup = _cleanup_remote_process(host, remote_pid)
        _terminate(tunnel)
        _terminate(remote_process)
        if result is not None:
            result["remote_cleanup"] = cleanup
            if cleanup.get("status") != "PASS" and result.get("status") == "PASS":
                result["status"] = "BLOCK"
                result["reason"] = "REMOTE_PROCESS_CLEANUP_FAILED"
        persisted_stdout_path.write_text(_redact_file(stdout_path, sensitive_values), encoding="utf-8")
        persisted_stderr_path.write_text(_redact_file(stderr_path, sensitive_values), encoding="utf-8")
        shutil.rmtree(temporary_dir, ignore_errors=True)
        meta_path.write_text(json.dumps({"schema": REMOTE_BROWSER_SCHEMA, "target_alias": host, "repo": repo, "remote_python": remote_python, "tunnel_port": tunnel_port, "token_redacted": True}, indent=2) + "\n", encoding="utf-8")


def _safe_remote_command(command: str, remote_python: str) -> tuple[str | None, str]:
    if any(marker in command for marker in (";", "|", "&", ">", "<", "`", "$", "\n", "\r")):
        return None, "shell_metacharacter"
    try:
        argv = shlex.split(command)
    except ValueError:
        return None, "command_parse_failed"
    if not argv:
        return None, "empty_command"
    executable = Path(argv[0]).name.lower()
    if executable in {"ssh", "scp", "sudo", "su", "doas", "curl", "wget", "nc", "netcat", "rm", "rmdir"}:
        return None, "blocked_executable"
    if any(token in {"--password", "--passwd", "--token", "--secret", "--api-key"} for token in argv):
        return None, "credential_argument"
    if remote_python and executable.startswith("python"):
        argv[0] = remote_python
    if not (executable.startswith("python") or argv[0].startswith("./") or argv[0].startswith("/")):
        return None, "executable_not_allowlisted"
    return " ".join(shlex.quote(token) for token in argv), ""


def _result(case_id: str, run_id: str, status: str, reason: str, contract_hash: str, **extra: Any) -> dict[str, Any]:
    return {"schema": REMOTE_BROWSER_SCHEMA, "case_id": case_id, "run_id": run_id, "status": status, "reason": reason, "contract_identity_hash": contract_hash, **extra}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cleanup_remote_process(host: str, remote_pid: int | None) -> dict[str, str]:
    if not remote_pid:
        return {"status": "UNVERIFIED", "reason": "remote_pid_not_observed"}
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                host,
                f"kill -TERM -- -{remote_pid} 2>/dev/null || true; sleep 0.2; if kill -0 -- -{remote_pid} 2>/dev/null; then exit 1; fi",
            ],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "TOOLING_FAIL", "reason": "remote_cleanup_dispatch_failed"}
    if completed.returncode != 0:
        return {"status": "FAIL", "reason": "remote_cleanup_command_failed"}
    return {"status": "PASS", "reason": "remote_process_group_terminated"}


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    query = "&".join(f"{key}=<redacted>" if key.lower() in {"token", "auth", "key", "secret"} else f"{key}=<redacted>" for key, _, _ in (part.partition("=") for part in parsed.query.split("&") if part))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _redact_file(path: Path, sensitive_values: list[str] | None = None) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    for value in sensitive_values or []:
        if value:
            text = text.replace(value, "[REDACTED:runtime_token]")
    safe, _ = redact_text(text, path=str(path))
    path.write_text(safe, encoding="utf-8")
    return safe
