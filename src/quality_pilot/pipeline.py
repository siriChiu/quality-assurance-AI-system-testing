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
    gate_results: list[dict[str, Any]] = []

    try:
        _mark(steps, "config_validate", "PASS")
        _mark(steps, "health_checks", "SKIPPED", {"reason": "health checks are owned by doctor"})
        _mark(
            steps,
            "issues_sync_readiness",
            "SKIPPED",
            {"reason": "issue sync readiness must be established by doctor or issues sync"},
        )
        contracts = select_contracts(config.paths.cases, case_id, case_ids=case_ids)
        if contracts:
            _mark(steps, "select_scope", "PASS", {"case_count": len(contracts)})
        else:
            _mark(steps, "select_scope", "HOLD", {"case_count": 0, "reason": "no_case_contracts_selected"})
        context = RunContext(root=config.root, evidence_dir=run_evidence_dir)
        for contract in contracts:
            result = run_case(contract, context, dry_run=dry_run)
            results.append(result)
        test_outcome = _test_outcome(results, dry_run=dry_run)
        probe_outcome = _probe_outcome(results, dry_run=dry_run)
        _mark(
            steps,
            "run_cases",
            test_outcome,
            {"case_count": len(contracts), "executed_count": len([item for item in results if item.get("status") != "NOT_RUN"])},
        )
        _mark(steps, "normalize_results", "PASS", {"result_count": len(results)})
        if not dry_run:
            for result in results:
                gate_results.append(evaluate_write_gate(config_data=config.data, result=result).as_dict())
        blocked_by_gate = len([gate for gate in gate_results if not gate["allowed"]])
        gate_status = _gate_status(gate_results, dry_run=dry_run)
        _mark(steps, "deduplicate_issues", "SKIPPED", {"reason": "no tracker writes are planned by this runner"})
        if gate_status == "BLOCKED":
            _mark(steps, "write_gate", "BLOCKED", {"blocked_by_gate": blocked_by_gate})
        elif gate_status == "ALLOWED":
            _mark(steps, "write_gate", "PASS", {"blocked_by_gate": 0})
        else:
            _mark(steps, "write_gate", "SKIPPED", {"blocked_by_gate": 0, "reason": "no executable results"})
        _mark(
            steps,
            "publish_wiki_status",
            "SKIPPED",
            {"reason": "wiki publication is handled by the caller after truth is persisted"},
        )
        status = _legacy_status(test_outcome)
        workflow_status = _workflow_status(test_outcome, gate_status)
        health_status = _health_status()
        payload = _summary_payload(
            run_id,
            status,
            test_outcome,
            probe_outcome,
            workflow_status,
            gate_status,
            health_status,
            steps,
            results,
            config.paths.reports / "status.md",
            blocked_by_gate,
            gate_results,
        )
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
        _abort_pending_steps(steps)
        payload = {
            "status": "ABORT",
            "workflow_status": "FAILED",
            "test_outcome": "ABORT",
            "probe_outcome": "NOT_RUN",
            "gate_status": "NOT_EVALUATED",
            "health_status": "NOT_EVALUATED",
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
    test_outcome: str,
    probe_outcome: str,
    workflow_status: str,
    gate_status: str,
    health_status: str,
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
        "workflow_status": workflow_status,
        "test_outcome": test_outcome,
        "probe_outcome": probe_outcome,
        "gate_status": gate_status,
        "health_status": health_status,
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


def _test_outcome(results: list[dict[str, Any]], *, dry_run: bool) -> str:
    if not results:
        return "HOLD"
    if dry_run:
        return "NOT_RUN"
    official_results = [result for result in results if not result.get("partial_probe")]
    if not official_results:
        return "HOLD"
    return _result_outcome(official_results)


def _probe_outcome(results: list[dict[str, Any]], *, dry_run: bool) -> str:
    probe_results = [result for result in results if result.get("partial_probe")]
    if not probe_results or dry_run:
        return "NOT_RUN"
    return _result_outcome(probe_results)


def _result_outcome(results: list[dict[str, Any]]) -> str:
    statuses = {str(result.get("status") or "BLOCK").upper() for result in results}
    if statuses == {"NOT_RUN"}:
        return "NOT_RUN"
    if "FAIL" in statuses:
        return "FAIL"
    if "BLOCK" in statuses or "ABORT" in statuses:
        return "BLOCK"
    if statuses == {"PASS"}:
        return "PASS"
    return "HOLD"


def _gate_status(gate_results: list[dict[str, Any]], *, dry_run: bool) -> str:
    if dry_run or not gate_results:
        return "NOT_EVALUATED"
    return "ALLOWED" if all(bool(gate.get("allowed")) for gate in gate_results) else "BLOCKED"


def _legacy_status(test_outcome: str) -> str:
    return test_outcome


def _workflow_status(test_outcome: str, gate_status: str) -> str:
    if test_outcome == "NOT_RUN":
        return "PLANNED"
    if test_outcome in {"HOLD", "BLOCK"} or gate_status == "BLOCKED":
        return "BLOCKED"
    if test_outcome == "ABORT":
        return "FAILED"
    return "COMPLETED"


def _health_status() -> str:
    # This runner does not execute doctor-style health checks. QA and write-gate
    # outcomes must not be reused as evidence that the project is healthy.
    return "NOT_EVALUATED"


def _abort_pending_steps(steps: list[dict[str, Any]]) -> None:
    abort_marked = False
    for step in steps:
        if step["status"] != "PENDING":
            continue
        if not abort_marked:
            step["status"] = "ABORT"
            abort_marked = True
        else:
            step["status"] = "SKIPPED"


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
