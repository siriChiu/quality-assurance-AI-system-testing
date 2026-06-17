from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def render_status_report(results: list[dict[str, Any]], report_path: Path) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI Quality Pilot status",
        "",
        "| Case | Status | Commands | Evidence |",
        "|---|---|---:|---|",
    ]
    for result in results:
        evidence = ", ".join(result.get("evidence", [])) or "-"
        lines.append(f"| {result.get('case_id', '')} | {result.get('status', '')} | {len(result.get('commands', []))} | {evidence} |")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def load_latest_results(state_dir: Path) -> list[dict[str, Any]]:
    latest = state_dir / "latest-run.json"
    if not latest.exists():
        return []
    payload = json.loads(latest.read_text(encoding="utf-8"))
    return list(payload.get("results", []))
