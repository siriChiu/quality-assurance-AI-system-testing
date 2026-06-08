from __future__ import annotations

import argparse
import json
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from . import cli
from .config import CONFIG_FILE, json_dumps

PRIMARY_PREFIX = "/qa-aist"
ALIAS_PREFIX = "qa-aist"
ACCEPTED_PREFIXES = {PRIMARY_PREFIX, ALIAS_PREFIX}
ROOT_COMMANDS = {"init-project", "setup", "status", "doctor", "qa-test", "close-loop", "report", "tracker"}


@dataclass(frozen=True)
class HermesCommand:
    prefix: str
    engine_argv: list[str]


def parse_chat_command(message: str, *, root: str | Path = ".") -> HermesCommand:
    parts = shlex.split(message)
    if not parts:
        raise ValueError("empty_hermes_message")
    prefix = parts[0]
    if prefix not in ACCEPTED_PREFIXES:
        raise ValueError("not_a_qa_aist_command")
    if len(parts) == 1:
        raise ValueError("empty_qa_aist_command")

    engine_argv = list(parts[1:])
    if not _has_option(engine_argv, "--json"):
        engine_argv.insert(0, "--json")
    _inject_project_context(engine_argv, Path(root).resolve())
    return HermesCommand(prefix=prefix, engine_argv=engine_argv)


def dispatch_chat_command(message: str, *, root: str | Path = ".") -> dict[str, Any]:
    try:
        command = parse_chat_command(message, root=root)
    except ValueError as exc:
        error = str(exc)
        payload = {
            "status": "error",
            "error": error,
            "message": _parse_error_message(error),
        }
        return {
            "status": "error",
            "interface": "hermes",
            "command": message,
            "accepted_prefixes": sorted(ACCEPTED_PREFIXES),
            "engine_argv": [],
            "exit_code": 2,
            "payload": payload,
            "chat_response": render_chat_response(payload, exit_code=2),
        }

    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(command.engine_argv)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 2

    raw_stdout = stdout.getvalue().strip()
    raw_stderr = stderr.getvalue().strip()
    payload = _parse_engine_json(raw_stdout)
    if payload is None:
        payload = {
            "status": "error",
            "error": "engine_output_not_json",
            "message": raw_stderr or raw_stdout or "QA-AIST engine did not emit JSON.",
            "stdout": raw_stdout,
            "stderr": raw_stderr,
        }
        if exit_code == 0:
            exit_code = 1
    elif raw_stderr:
        payload = {**payload, "stderr": raw_stderr}

    return {
        "status": _dispatch_status(payload, exit_code),
        "interface": "hermes",
        "command": message,
        "prefix": command.prefix,
        "engine_argv": command.engine_argv,
        "exit_code": exit_code,
        "payload": payload,
        "chat_response": render_chat_response(payload, exit_code=exit_code),
    }


def render_chat_response(payload: dict[str, Any], *, exit_code: int = 0) -> str:
    status = str(payload.get("status") or ("ok" if exit_code == 0 else "error"))
    lines = [f"qa-aist> {status.upper()}"]

    if payload.get("error"):
        lines.append(f"         error: {payload.get('error')}")
    if payload.get("message"):
        lines.append(f"         message: {payload.get('message')}")
    if payload.get("path"):
        lines.append(f"         path: {payload.get('path')}")
    if payload.get("workspace"):
        lines.append(f"         workspace: {payload.get('workspace')}")
    if "case_contract_count" in payload:
        lines.append(f"         cases: {payload.get('case_contract_count')}")
    if "runner_count" in payload:
        lines.append(f"         runners: {payload.get('runner_count')}")
    if payload.get("latest_run_json"):
        lines.append(f"         latest_run_json: {payload.get('latest_run_json')}")
    if payload.get("report_path"):
        lines.append(f"         report: {payload.get('report_path')}")
    if isinstance(payload.get("tracker_writes"), dict):
        blocked = payload["tracker_writes"].get("blocked_by_gate")
        lines.append(f"         tracker_writes.blocked_by_gate: {blocked}")
    if isinstance(payload.get("write_gate_result"), dict):
        lines.append(f"         write_gate: {payload['write_gate_result'].get('reason')}")

    first_result = _first_result(payload)
    if first_result:
        if first_result.get("case_id"):
            lines.append(f"         case: {first_result.get('case_id')}")
        if first_result.get("result_path"):
            lines.append(f"         result: {first_result.get('result_path')}")
        elif first_result.get("evidence"):
            lines.append(f"         evidence: {', '.join(map(str, first_result.get('evidence', [])[:3]))}")
    return "\n".join(lines)


def _inject_project_context(engine_argv: list[str], root: Path) -> None:
    path = _command_path(engine_argv)
    if len(path) >= 2 and path[0] == "config" and path[1] == "validate":
        if not _has_option(engine_argv, "--config"):
            engine_argv.extend(["--config", str(root / CONFIG_FILE)])
        return
    if len(path) >= 2 and path[0] == "config" and path[1] == "show":
        if not _has_option(engine_argv, "--root"):
            engine_argv.extend(["--root", str(root)])
        return
    if path and path[0] in ROOT_COMMANDS and not _has_option(engine_argv, "--root"):
        engine_argv.extend(["--root", str(root)])


def _command_path(argv: list[str]) -> list[str]:
    path: list[str] = []
    for item in argv:
        if item == "--json":
            continue
        if item.startswith("-"):
            continue
        path.append(item)
        if len(path) == 2:
            break
    return path


def _has_option(argv: list[str], option: str) -> bool:
    return any(item == option or item.startswith(f"{option}=") for item in argv)


def _parse_engine_json(raw_stdout: str) -> dict[str, Any] | None:
    if not raw_stdout:
        return None
    try:
        loaded = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _dispatch_status(payload: dict[str, Any], exit_code: int) -> str:
    payload_status = payload.get("status")
    if isinstance(payload_status, str) and payload_status:
        return payload_status
    return "ok" if exit_code == 0 else "error"


def _first_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    return None


def _parse_error_message(error: str) -> str:
    if error == "empty_hermes_message":
        return "Expected a Hermes chat command such as /qa-aist status."
    if error == "not_a_qa_aist_command":
        return "Only /qa-aist commands are accepted by this dispatcher."
    if error == "empty_qa_aist_command":
        return "Expected /qa-aist followed by a QA-AIST subcommand."
    return error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qa-aist-hermes", description="Dispatch a Hermes /qa-aist chat command to the QA-AIST engine")
    parser.add_argument("--root", default=".", help="Product repository root provided by Hermes context")
    parser.add_argument("message", nargs=argparse.REMAINDER, help="Hermes chat message, for example: /qa-aist status")
    args = parser.parse_args(argv)
    message = " ".join(args.message).strip()
    result = dispatch_chat_command(message, root=args.root)
    print(json_dumps(result))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
