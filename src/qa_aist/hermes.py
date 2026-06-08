from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from . import __version__, cli
from .config import CONFIG_FILE, json_dumps

PRIMARY_PREFIX = "/qa-aist"
ALIAS_PREFIX = "qa-aist"
ACCEPTED_PREFIXES = {PRIMARY_PREFIX, ALIAS_PREFIX}
ROOT_COMMANDS = {"init-project", "setup", "status", "doctor", "qa-test", "close-loop", "report", "tracker"}
AGENT_MANIFEST_NAME = "qa-aist.agent.json"
AGENT_WRAPPER_NAME = "qa-aist-agent.sh"
HERMES_SKILL_NAME = "qa-aist"
HERMES_SKILL_FILE_NAME = "SKILL.md"


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


def build_agent_manifest(*, wrapper_path: str | None = None, runner_command: str = "qa-aist-hermes") -> dict[str, Any]:
    entrypoint_command = [wrapper_path] if wrapper_path else [runner_command, "--root", "${HERMES_PROJECT_ROOT}", "${HERMES_MESSAGE}"]
    return {
        "schema": "hermes.agent.v1",
        "name": "qa-aist",
        "display_name": "QA-AIST",
        "version": __version__,
        "description": "Hermes-first deterministic QA automation agent/plugin.",
        "command_prefix": PRIMARY_PREFIX,
        "aliases": [ALIAS_PREFIX],
        "entrypoint": {
            "type": "process",
            "command": entrypoint_command,
            "root_env": "HERMES_PROJECT_ROOT",
            "message_env": "HERMES_MESSAGE",
            "message_args": "append",
        },
        "python_api": {
            "module": "qa_aist.hermes",
            "callable": "dispatch_chat_command",
            "signature": "dispatch_chat_command(message: str, root: str | Path = '.') -> dict",
        },
        "engine": {
            "console_script": "qa-aist",
            "hermes_console_script": runner_command,
            "json_output": True,
        },
        "commands": [
            f"{PRIMARY_PREFIX} setup",
            f"{PRIMARY_PREFIX} status",
            f"{PRIMARY_PREFIX} doctor",
            f"{PRIMARY_PREFIX} config show",
            f"{PRIMARY_PREFIX} config validate",
            f"{PRIMARY_PREFIX} qa-test list",
            f"{PRIMARY_PREFIX} qa-test validate",
            f"{PRIMARY_PREFIX} qa-test dry-run",
            f"{PRIMARY_PREFIX} qa-test run",
            f"{PRIMARY_PREFIX} qa-test run-one <case_id>",
            f"{PRIMARY_PREFIX} close-loop status",
            f"{PRIMARY_PREFIX} close-loop run-once",
            f"{PRIMARY_PREFIX} report status",
            f"{PRIMARY_PREFIX} report json",
            f"{PRIMARY_PREFIX} tracker plan-write",
        ],
        "permissions": {
            "filesystem": ["project_root"],
            "network": [],
            "tracker_write": "never_in_v1",
        },
        "security": {
            "deterministic_write_gate_required": True,
            "raw_secret_output_forbidden": True,
            "closed_issue_write_forbidden": True,
            "llm_may_not_reorder_pipeline": True,
        },
        "outputs": {
            "format": "json",
            "chat_response_field": "chat_response",
            "payload_field": "payload",
        },
    }


def install_agent(agent_dir: str | Path, *, force: bool = False, runner_command: str = "qa-aist-hermes") -> dict[str, Any]:
    target = Path(agent_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    wrapper_path = target / AGENT_WRAPPER_NAME
    manifest_path = target / AGENT_MANIFEST_NAME
    if not force and (wrapper_path.exists() or manifest_path.exists()):
        return {
            "status": "error",
            "error": "agent_files_exist",
            "message": "Hermes agent files already exist. Re-run with --force to overwrite.",
            "agent_dir": str(target),
            "manifest_path": str(manifest_path),
            "wrapper_path": str(wrapper_path),
        }

    wrapper_path.write_text(_wrapper_script(runner_command), encoding="utf-8")
    wrapper_path.chmod(wrapper_path.stat().st_mode | 0o111)
    manifest = build_agent_manifest(wrapper_path=str(wrapper_path), runner_command=runner_command)
    manifest_path.write_text(json_dumps(manifest) + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "agent_dir": str(target),
        "manifest_path": str(manifest_path),
        "wrapper_path": str(wrapper_path),
        "command_prefix": PRIMARY_PREFIX,
        "aliases": [ALIAS_PREFIX],
    }


def agent_status(agent_dir: str | Path) -> dict[str, Any]:
    target = Path(agent_dir).expanduser().resolve()
    manifest_path = target / AGENT_MANIFEST_NAME
    wrapper_path = target / AGENT_WRAPPER_NAME
    manifest: dict[str, Any] | None = None
    manifest_valid = False
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
                manifest_valid = loaded.get("command_prefix") == PRIMARY_PREFIX
        except json.JSONDecodeError:
            manifest_valid = False
    return {
        "status": "ok" if manifest_valid and wrapper_path.exists() else "missing",
        "agent_dir": str(target),
        "manifest_path": str(manifest_path),
        "manifest_exists": manifest_path.exists(),
        "manifest_valid": manifest_valid,
        "wrapper_path": str(wrapper_path),
        "wrapper_exists": wrapper_path.exists(),
        "command_prefix": manifest.get("command_prefix") if manifest else None,
    }


def default_skills_dir() -> Path:
    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).expanduser() / "skills"
    return Path.home() / ".hermes" / "skills"


def build_skill_markdown(*, runner_command: str = "qa-aist-hermes") -> str:
    return f"""---
name: qa-aist
description: "QA-AIST: deterministic QA setup, tests, evidence, reports, and write-gated tracker plans."
version: {__version__}
author: QA-AIST contributors
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [qa, testing, deterministic, evidence, write-gate, tracker]
---

# QA-AIST Hermes Skill

This skill makes `/qa-aist ...` visible to Hermes as a dynamic skill slash command.

Important boundary: Hermes skill slash commands are prompt-mediated. This skill tells the agent exactly how to call the deterministic QA-AIST dispatcher, but Hermes itself still routes this as a skill invocation unless a native plugin/router is installed.

## Required Behavior

When the user invokes `/qa-aist <arguments>`:

1. Treat `<arguments>` as a QA-AIST command, for example `doctor`, `qa-test list`, or `close-loop run-once`.
2. Do not answer from memory.
3. Run the QA-AIST dispatcher through the terminal from the current product repository root.
4. Read the returned JSON.
5. Reply with the `chat_response` field. If the JSON has no `chat_response`, summarize `payload.status`, `payload.error`, and any report/evidence paths.

Use this command shape:

```bash
{runner_command} --root "$PWD" /qa-aist <arguments>
```

Examples:

```bash
{runner_command} --root "$PWD" /qa-aist status
{runner_command} --root "$PWD" /qa-aist doctor
{runner_command} --root "$PWD" /qa-aist qa-test list
{runner_command} --root "$PWD" /qa-aist qa-test run-one EXAMPLE-001
{runner_command} --root "$PWD" /qa-aist close-loop run-once
{runner_command} --root "$PWD" /qa-aist tracker plan-write
```

## Safety Rules

- Do not directly write tracker comments, reopen issues, close issues, or create issue text.
- Do not reorder the QA-AIST close-loop pipeline.
- Do not invent evidence paths.
- Do not print raw secrets.
- Tracker writes in QA-AIST V1 are dry-run plans only.

## If The Dispatcher Is Missing

If `{runner_command}` is not found, tell the user to install QA-AIST into the same environment Hermes uses, or reinstall this skill with a runner command such as:

```bash
PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes install-skill --force --runner-command "/usr/bin/env PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes"
```
"""


def install_skill(
    skills_dir: str | Path | None = None,
    *,
    force: bool = False,
    runner_command: str = "qa-aist-hermes",
) -> dict[str, Any]:
    base = Path(skills_dir).expanduser().resolve() if skills_dir else default_skills_dir().expanduser().resolve()
    skill_dir = base / HERMES_SKILL_NAME
    skill_path = skill_dir / HERMES_SKILL_FILE_NAME
    if skill_path.exists() and not force:
        return {
            "status": "error",
            "error": "skill_exists",
            "message": "Hermes QA-AIST skill already exists. Re-run with --force to overwrite.",
            "skills_dir": str(base),
            "skill_dir": str(skill_dir),
            "skill_path": str(skill_path),
            "command_prefix": PRIMARY_PREFIX,
        }
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(build_skill_markdown(runner_command=runner_command), encoding="utf-8")
    return {
        "status": "ok",
        "skills_dir": str(base),
        "skill_dir": str(skill_dir),
        "skill_path": str(skill_path),
        "command_prefix": PRIMARY_PREFIX,
        "reload_command": "/reload-skills",
        "runner_command": runner_command,
    }


def skill_status(skills_dir: str | Path | None = None) -> dict[str, Any]:
    base = Path(skills_dir).expanduser().resolve() if skills_dir else default_skills_dir().expanduser().resolve()
    skill_dir = base / HERMES_SKILL_NAME
    skill_path = skill_dir / HERMES_SKILL_FILE_NAME
    exists = skill_path.exists()
    valid = False
    if exists:
        text = skill_path.read_text(encoding="utf-8")
        valid = "name: qa-aist" in text and "QA-AIST Hermes Skill" in text
    return {
        "status": "ok" if valid else "missing",
        "skills_dir": str(base),
        "skill_dir": str(skill_dir),
        "skill_path": str(skill_path),
        "skill_exists": exists,
        "skill_valid": valid,
        "command_prefix": PRIMARY_PREFIX if valid else None,
    }


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


def _wrapper_script(runner_command: str) -> str:
    runner_argv = " ".join(shlex.quote(part) for part in shlex.split(runner_command))
    return f"""#!/usr/bin/env bash
set -euo pipefail
root="${{HERMES_PROJECT_ROOT:-${{PWD}}}}"
if [[ "$#" -eq 0 && -n "${{HERMES_MESSAGE:-}}" ]]; then
  exec {runner_argv} --root "$root" "${{HERMES_MESSAGE}}"
fi
exec {runner_argv} --root "$root" "$@"
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in {"manifest", "install", "status", "install-skill", "skill-status"}:
        return _main_agent_command(argv)
    parser = argparse.ArgumentParser(prog="qa-aist-hermes", description="Dispatch a Hermes /qa-aist chat command to the QA-AIST engine")
    parser.add_argument("--root", default=".", help="Product repository root provided by Hermes context")
    parser.add_argument("message", nargs=argparse.REMAINDER, help="Hermes chat message, for example: /qa-aist status")
    args = parser.parse_args(argv)
    message = " ".join(args.message).strip()
    result = dispatch_chat_command(message, root=args.root)
    print(json_dumps(result))
    return int(result["exit_code"])


def _main_agent_command(argv: list[str]) -> int:
    command = argv[0]
    if command == "manifest":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes manifest", description="Print a portable Hermes agent manifest")
        parser.add_argument("--runner-command", default="qa-aist-hermes")
        args = parser.parse_args(argv[1:])
        print(json_dumps(build_agent_manifest(runner_command=args.runner_command)))
        return 0
    if command == "install":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes install", description="Install QA-AIST agent files into a Hermes agents directory")
        parser.add_argument("--agent-dir", required=True, help="Hermes agents directory")
        parser.add_argument("--runner-command", default="qa-aist-hermes")
        parser.add_argument("--force", action="store_true", help="Overwrite existing QA-AIST agent files")
        args = parser.parse_args(argv[1:])
        payload = install_agent(args.agent_dir, force=args.force, runner_command=args.runner_command)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 4
    if command == "install-skill":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes install-skill", description="Install QA-AIST as a Hermes dynamic skill slash command")
        parser.add_argument("--skills-dir", default=None, help="Hermes skills directory; defaults to $HERMES_HOME/skills or ~/.hermes/skills")
        parser.add_argument("--runner-command", default="qa-aist-hermes")
        parser.add_argument("--force", action="store_true", help="Overwrite an existing QA-AIST skill")
        args = parser.parse_args(argv[1:])
        payload = install_skill(args.skills_dir, force=args.force, runner_command=args.runner_command)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 4
    if command == "skill-status":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes skill-status", description="Check QA-AIST Hermes skill installation")
        parser.add_argument("--skills-dir", default=None, help="Hermes skills directory; defaults to $HERMES_HOME/skills or ~/.hermes/skills")
        args = parser.parse_args(argv[1:])
        payload = skill_status(args.skills_dir)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 2
    parser = argparse.ArgumentParser(prog="qa-aist-hermes status", description="Check QA-AIST Hermes agent installation")
    parser.add_argument("--agent-dir", required=True, help="Hermes agents directory")
    args = parser.parse_args(argv[1:])
    payload = agent_status(args.agent_dir)
    print(json_dumps(payload))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
