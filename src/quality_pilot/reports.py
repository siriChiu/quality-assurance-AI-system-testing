from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runner import utc_now


def render_status_report(results: list[dict[str, Any]], report_path: Path, *, latest_run: dict[str, Any] | None = None) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    official = [result for result in results if not result.get("partial_probe")]
    partial = [result for result in results if result.get("partial_probe")]
    official_counts = _count_results(official)
    partial_counts = _count_results(partial)
    stale_reason = _stale_reason(official, latest_run)
    lines = [
        "# AI Quality Pilot status",
        "",
        f"- Generated at: {utc_now()}",
        f"- Source run: {_source_run_id(latest_run)}",
        f"- Source status: {_source_status(latest_run)}",
        "",
        "## Official Case Counters",
        "",
        "| PASS | FAIL | BLOCK | ABORT | NOT_RUN |",
        "|---:|---:|---:|---:|---:|",
        f"| {official_counts['PASS']} | {official_counts['FAIL']} | {official_counts['BLOCK']} | {official_counts['ABORT']} | {official_counts['NOT_RUN']} |",
        "",
        "## Stale Report Check",
        "",
        f"- Status: {'STALE' if stale_reason else 'CURRENT'}",
    ]
    if stale_reason:
        lines.append(f"- Stale report warning: {stale_reason}")
    lines.extend([
        "",
        "## Official Case Results",
        "",
        "| Case | Status | Commands | Evidence |",
        "|---|---|---:|---|",
    ])
    if not official:
        lines.append("| - | NOT_RUN | 0 | No official case results were available |")
    for result in official:
        evidence = ", ".join(result.get("evidence", [])) or "-"
        lines.append(f"| {result.get('case_id', '')} | {result.get('status', '')} | {len(result.get('commands', []))} | {evidence} |")

    lines.extend(
        [
            "",
            "## Partial Probes",
            "",
            "Partial probes are supplemental diagnostics and are not counted in official case counters.",
            "",
            "| PASS | FAIL | BLOCK | ABORT | NOT_RUN |",
            "|---:|---:|---:|---:|---:|",
            f"| {partial_counts['PASS']} | {partial_counts['FAIL']} | {partial_counts['BLOCK']} | {partial_counts['ABORT']} | {partial_counts['NOT_RUN']} |",
            "",
            "| Case | Status | Commands | Evidence |",
            "|---|---|---:|---|",
        ]
    )
    if not partial:
        lines.append("| - | - | 0 | No partial probes were reported |")
    for result in partial:
        evidence = ", ".join(result.get("evidence", [])) or "-"
        lines.append(f"| {result.get('case_id', '')} | {result.get('status', '')} | {len(result.get('commands', []))} | {evidence} |")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def load_latest_payload(state_dir: Path) -> dict[str, Any] | None:
    latest = state_dir / "latest-run.json"
    if not latest.exists():
        return None
    payload = json.loads(latest.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def load_latest_results(state_dir: Path) -> list[dict[str, Any]]:
    payload = load_latest_payload(state_dir)
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    return list(results) if isinstance(results, list) else []


def _count_results(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "ABORT": 0, "NOT_RUN": 0}
    for result in results:
        key = str(result.get("status") or "BLOCK")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _source_run_id(latest_run: dict[str, Any] | None) -> str:
    if not isinstance(latest_run, dict):
        return "-"
    return str(latest_run.get("run_id") or "-")


def _source_status(latest_run: dict[str, Any] | None) -> str:
    if not isinstance(latest_run, dict):
        return "missing"
    return str(latest_run.get("status") or "unknown")


def _stale_reason(official_results: list[dict[str, Any]], latest_run: dict[str, Any] | None) -> str | None:
    if not isinstance(latest_run, dict):
        return "no latest-run payload was available for this report"
    latest_results = latest_run.get("results")
    if not isinstance(latest_results, list):
        return "latest-run payload has no results list"
    if str(latest_run.get("status") or "").upper() == "PASS" and not any(
        str(result.get("status") or "").upper() == "PASS" for result in official_results
    ):
        return "latest-run is PASS but no official case result reflects PASS evidence"
    if official_results and all(str(result.get("status") or "").upper() == "NOT_RUN" for result in official_results):
        return "all official case results are NOT_RUN"
    return None
