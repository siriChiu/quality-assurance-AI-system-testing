from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import CaseContract, CommandContract


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
    command_results = []
    status = "PASS"
    exit_code = 0
    for command in contract.commands:
        result = _dry_command(command, case_evidence_dir) if dry_run else _run_command(command, context.root, case_evidence_dir)
        command_results.append(result)
        if result["exit_code"] != command.expected_exit_code:
            status = "FAIL"
            exit_code = result["exit_code"]
            break
    ended_at = utc_now()
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
    }
    result_path = case_evidence_dir / "result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    payload["result_path"] = _relative_or_str(result_path, context.root)
    return payload


def _run_command(command: CommandContract, root: Path, evidence_dir: Path) -> dict[str, Any]:
    stdout_path = evidence_dir / f"{command.id}.stdout.log"
    stderr_path = evidence_dir / f"{command.id}.stderr.log"
    rc_path = evidence_dir / f"{command.id}.rc"
    meta_path = evidence_dir / f"{command.id}.meta"
    started_at = utc_now()
    meta_path.write_text(json.dumps({
        "id": command.id,
        "run": command.run,
        "expected_exit_code": command.expected_exit_code,
        "started_at": started_at,
    }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    completed = subprocess.run(command.run, cwd=root, shell=True, text=True, capture_output=True, check=False)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    rc_path.write_text(f"{completed.returncode}\n", encoding="utf-8")
    ended_at = utc_now()
    return {
        "id": command.id,
        "run": command.run,
        "expected_exit_code": command.expected_exit_code,
        "exit_code": completed.returncode,
        "status": "PASS" if completed.returncode == command.expected_exit_code else "FAIL",
        "started_at": started_at,
        "ended_at": ended_at,
        "stdout": _relative_or_str(stdout_path, root),
        "stderr": _relative_or_str(stderr_path, root),
        "rc": _relative_or_str(rc_path, root),
        "meta": _relative_or_str(meta_path, root),
    }


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
    }


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
