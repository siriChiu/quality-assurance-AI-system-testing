from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .case_generation import generate_cases_growing
from .config import ProjectConfig, json_dumps
from .issues import issue_status
from .pipeline import run_close_loop
from .runner import utc_now
from .wiki import auto_sync_wiki


HEARTBEAT_SCHEMA = "quality-pilot.close-loop-heartbeat.v1"
HEARTBEAT_DEFAULT_EVERY = "12h"
HEARTBEAT_DEFAULT_GROW_COUNT = 20


def parse_heartbeat_interval(value: str | int | None) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        if value < 0:
            raise ValueError("heartbeat interval must be >= 0")
        return value
    raw = str(value).strip().lower()
    match = re.fullmatch(r"(\d+)([smh]?)", raw)
    if not match:
        raise ValueError("heartbeat interval must look like 30s, 5m, 1h, or 0")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return amount * multiplier


def run_heartbeat(
    config: ProjectConfig,
    *,
    every_seconds: int = 12 * 60 * 60,
    grow_count: int = HEARTBEAT_DEFAULT_GROW_COUNT,
    case_id: str | None = None,
    dry_run: bool = False,
    run_existing_if_no_growth: bool = False,
) -> dict[str, Any]:
    if every_seconds < 0:
        raise ValueError("heartbeat interval must be >= 0")
    if grow_count < 1:
        raise ValueError("grow_count must be >= 1")

    heartbeat_id = _heartbeat_id()
    tick = run_heartbeat_once(
        config,
        heartbeat_id=heartbeat_id,
        grow_count=grow_count,
        case_id=case_id,
        dry_run=dry_run,
        run_existing_if_no_growth=run_existing_if_no_growth,
    )
    payload = _heartbeat_payload(heartbeat_id, tick, every_seconds=every_seconds, dry_run=dry_run)
    _persist_heartbeat(config, payload)
    return payload


def run_heartbeat_once(
    config: ProjectConfig,
    *,
    heartbeat_id: str,
    grow_count: int,
    case_id: str | None,
    dry_run: bool,
    run_existing_if_no_growth: bool,
) -> dict[str, Any]:
    sensors: list[dict[str, Any]] = []
    issue_sensor = _issue_sensor(config)
    sensors.append(issue_sensor)

    growth = generate_cases_growing(config, count=grow_count, fast=True, force=False)
    generated_case_ids = [
        str(item.get("case_id"))
        for item in growth.get("generated", [])
        if isinstance(item, dict) and item.get("case_id")
    ]
    sensors.append({
        "name": "case_generate_growing",
        "status": str(growth.get("status") or "unknown"),
        "generated_count": int(growth.get("generated_count") or 0),
        "deduped_count": int(growth.get("deduped_count") or 0),
        "skipped_count": int(growth.get("skipped_count") or 0),
        "growth_seed_count": int(growth.get("growth_seed_count") or 0),
        "growth_context_path": growth.get("growth_context_path"),
        "generated_case_ids": generated_case_ids,
    })

    if growth.get("status") == "needs_input" and _growth_has_no_actionable_input(growth, issue_sensor):
        return {
            "heartbeat_id": heartbeat_id,
            "tick": 1,
            "status": "idle",
            "reason": "no_actionable_sensor_input",
            "sensors": sensors,
            "growth": growth,
            "next_action": "/quality-pilot cases generate --init",
        }

    if growth.get("status") == "needs_input":
        return {
            "heartbeat_id": heartbeat_id,
            "tick": 1,
            "status": "blocked",
            "reason": "growth_needs_input",
            "sensors": sensors,
            "growth": growth,
            "next_action": "/quality-pilot doctor",
        }

    selected_case_ids = generated_case_ids
    if case_id:
        selected_case_ids = [case_id]
    elif not selected_case_ids and run_existing_if_no_growth:
        selected_case_ids = []

    if not selected_case_ids and not run_existing_if_no_growth:
        return {
            "heartbeat_id": heartbeat_id,
            "tick": 1,
            "status": "idle",
            "reason": "no_new_growth",
            "sensors": sensors,
            "growth": growth,
            "next_action": "/quality-pilot cases generate --growing",
        }

    result = run_close_loop(
        config,
        case_id=case_id if case_id else None,
        case_ids=None if case_id or run_existing_if_no_growth else selected_case_ids,
        dry_run=dry_run,
    )
    run_payload = result.payload
    wiki = None
    if not dry_run and result.status in {"PASS", "FAIL", "BLOCK"}:
        wiki = auto_sync_wiki(config, event="test_result", latest_run=run_payload, source_payload=run_payload)

    return {
        "heartbeat_id": heartbeat_id,
        "tick": 1,
        "status": "ok" if result.status in {"PASS", "FAIL"} else "blocked",
        "reason": "new_growth_executed" if generated_case_ids and not case_id else "requested_scope_executed",
        "sensors": sensors,
        "growth": growth,
        "run": run_payload,
        "run_status": result.status,
        "executed_case_ids": selected_case_ids if selected_case_ids else "all",
        "wiki": wiki,
        "next_action": "/quality-pilot issues report",
    }


def _growth_has_no_actionable_input(growth: dict[str, Any], issue_sensor: dict[str, Any]) -> bool:
    return (
        _safe_int(issue_sensor.get("open_count")) == 0
        and _safe_int(growth.get("analyzed_files_count")) == 0
        and _safe_int(growth.get("advisory_input_count")) == 0
        and _safe_int(growth.get("generated_count")) == 0
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _issue_sensor(config: ProjectConfig) -> dict[str, Any]:
    try:
        payload = issue_status(config)
    except Exception as exc:  # Keep issue sensor failures from hiding growth signals.
        return {"name": "issues_status", "status": "blocked", "error": type(exc).__name__, "message": str(exc)}
    return {
        "name": "issues_status",
        "status": str(payload.get("status") or "unknown"),
        "open_count": payload.get("open_count"),
        "traceability_path": payload.get("traceability_map_path"),
    }


def _heartbeat_payload(
    heartbeat_id: str,
    tick: dict[str, Any],
    *,
    every_seconds: int,
    dry_run: bool,
) -> dict[str, Any]:
    status = str(tick.get("status") or "unknown")
    return {
        "schema": HEARTBEAT_SCHEMA,
        "status": status,
        "heartbeat_id": heartbeat_id,
        "created_at": heartbeat_id,
        "every_seconds": every_seconds,
        "next_heartbeat_after_seconds": every_seconds,
        "dry_run": dry_run,
        "latest_tick": tick,
        "state_path": ".quality-pilot-project/state/close-loop/heartbeat-latest.json",
        "history_path": ".quality-pilot-project/state/close-loop/heartbeat-history.jsonl",
    }


def _persist_heartbeat(config: ProjectConfig, payload: dict[str, Any]) -> None:
    directory = config.paths.state / "close-loop"
    directory.mkdir(parents=True, exist_ok=True)
    latest = directory / "heartbeat-latest.json"
    history = directory / "heartbeat-history.jsonl"
    latest.write_text(json_dumps(payload) + "\n", encoding="utf-8")
    history.write_text(history.read_text(encoding="utf-8") + json_dumps(payload) + "\n" if history.exists() else json_dumps(payload) + "\n", encoding="utf-8")


def _heartbeat_id() -> str:
    return utc_now().replace(":", "").replace(".", "")
