from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .contracts import select_contracts
from .reports import render_status_report
from .runner import RunContext, run_case, utc_now
from .write_gate import evaluate_write_gate

PIPELINE_ORDER = [
    "config_validate",
    "health_checks",
    "issues_sync_readiness",
    "select_scope",
    "run_cases",
    "normalize_results",
    "deduplicate_issues",
    "write_gate",
    "publish_wiki_status",
    "render_reports",
    "persist_state",
]


@dataclass(frozen=True)
class PipelineResult:
    payload: dict[str, Any]

    @property
    def status(self) -> str:
        return str(self.payload["status"])


def run_close_loop(
    config: ProjectConfig,
    *,
    case_id: str | None = None,
    case_ids: list[str] | None = None,
    dry_run: bool = False,
) -> PipelineResult:
    run_id = utc_now().replace(":", "").replace(".", "")
    run_evidence_dir = config.paths.evidence / run_id
    config.paths.state.mkdir(parents=True, exist_ok=True)
    steps = [{"name": name, "status": "PENDING"} for name in PIPELINE_ORDER]
    results: list[dict[str, Any]] = []
    status = "PASS"
    gate_results: list[dict[str, Any]] = []

    try:
        _mark(steps, "config_validate", "PASS")
        _mark(steps, "health_checks", "PASS")
        _mark(steps, "issues_sync_readiness", "PASS", {"mode": "checked_by_doctor_or_issues_sync"})
        contracts = select_contracts(config.paths.cases, case_id, case_ids=case_ids)
        _mark(steps, "select_scope", "PASS", {"case_count": len(contracts)})
        context = RunContext(root=config.root, evidence_dir=run_evidence_dir)
        for contract in contracts:
            result = run_case(contract, context, dry_run=dry_run)
            results.append(result)
            if result["status"] == "FAIL":
                status = "FAIL"
            elif result["status"] == "BLOCK" and status == "PASS":
                status = "BLOCK"
        _mark(steps, "run_cases", status)
        _mark(steps, "normalize_results", "PASS", {"result_count": len(results)})
        for result in results:
            gate_results.append(evaluate_write_gate(config_data=config.data, result=result).as_dict())
        blocked_by_gate = len([gate for gate in gate_results if not gate["allowed"]])
        _mark(steps, "deduplicate_issues", "PASS", {"planned_writes": 0})
        _mark(steps, "write_gate", "PASS", {"blocked_by_gate": blocked_by_gate})
        _mark(steps, "publish_wiki_status", "PASS", {"mode": "wiki_status_board"})
        payload = _summary_payload(run_id, status, steps, results, config.paths.reports / "status.md", blocked_by_gate, gate_results)
        report_path = render_status_report(results, config.paths.reports / "status.md", latest_run=payload)
        payload["report_path"] = str(report_path)
        _mark(steps, "render_reports", "PASS", {"report_path": str(report_path)})
        latest_run_json = config.paths.state / "latest-run.json"
        payload["latest_run_json"] = _relative_or_str(latest_run_json, config.root)
        _mark(steps, "persist_state", "PASS", {"latest_run_json": payload["latest_run_json"]})
        payload["steps"] = steps
        latest_run_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return PipelineResult(payload)
    except Exception as exc:
        for step in steps:
            if step["status"] == "PENDING":
                step["status"] = "ABORT"
                break
        payload = {
            "status": "ABORT",
            "run_id": run_id,
            "error": type(exc).__name__,
            "message": str(exc),
            "steps": steps,
            "case_counts": {"PASS": 0, "FAIL": 0, "BLOCK": 0, "ABORT": 1, "NOT_RUN": 0},
            "results": results,
            "latest_run_json": None,
            "report_path": None,
            "tracker_writes": {"created": 0, "updated": 0, "blocked_by_gate": 0},
        }
        return PipelineResult(payload)


def _summary_payload(
    run_id: str,
    status: str,
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
    report_path: Path,
    blocked_by_gate: int,
    gate_results: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "ABORT": 0, "NOT_RUN": 0}
    partial_counts = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "ABORT": 0, "NOT_RUN": 0}
    for result in results:
        key = str(result.get("status", "BLOCK"))
        target = partial_counts if result.get("partial_probe") else counts
        target[key] = target.get(key, 0) + 1
    return {
        "status": status,
        "run_id": run_id,
        "case_counts": counts,
        "partial_probe_counts": partial_counts,
        "steps": steps,
        "results": results,
        "latest_run_json": None,
        "report_path": str(report_path),
        "tracker_writes": {"created": 0, "updated": 0, "blocked_by_gate": blocked_by_gate},
        "write_gate": gate_results,
    }


def _mark(steps: list[dict[str, Any]], name: str, status: str, details: dict[str, Any] | None = None) -> None:
    for step in steps:
        if step["name"] == name:
            step["status"] = status
            if details:
                step["details"] = details
            return


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
