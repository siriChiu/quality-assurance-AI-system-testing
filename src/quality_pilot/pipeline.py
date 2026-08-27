from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .contracts import select_contracts
from .environment import environment_profile_status
from .reports import render_status_report
from .runner import RunContext, run_case, stamp_result_run_id, utc_now
from .security import ensure_safe_structure
from .task_graph import ContextPacket, TaskCheckpointStore, TaskGraphError, TaskGraphExecutor, TaskNode, compile_quality_task_graph
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
        execution_block = None if dry_run else _execution_prerequisite_block(contracts, environment_profile_status(config))
        if execution_block:
            _mark(steps, "select_scope", "BLOCK", execution_block)
            payload = _summary_payload(
                run_id,
                "BLOCK",
                "BLOCK",
                "NOT_RUN",
                "BLOCKED",
                "NOT_EVALUATED",
                _health_status(),
                steps,
                [],
                config.paths.reports / "status.md",
                0,
                [],
                environment_profile_status(config),
            )
            payload["blocked_reason"] = execution_block
            payload["latest_run_json"] = _relative_or_str(config.paths.state / "latest-run.json", config.root)
            payload["steps"] = steps
            config.paths.state.joinpath("latest-run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            return PipelineResult(payload)
        if contracts:
            _mark(steps, "select_scope", "PASS", {"case_count": len(contracts)})
        else:
            _mark(steps, "select_scope", "HOLD", {"case_count": 0, "reason": "no_case_contracts_selected"})
        environment_profile = environment_profile_status(config)
        context = RunContext(
            root=config.root,
            evidence_dir=run_evidence_dir,
            environment_profile=environment_profile,
        )
        for contract in contracts:
            result = run_case(contract, context, dry_run=dry_run)
            stamp_result_run_id(result, config.root, run_id)
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
            environment_profile,
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


def run_close_loop_task_graph(
    config: ProjectConfig,
    *,
    case_id: str | None = None,
    case_ids: list[str] | None = None,
    dry_run: bool = False,
    resume: bool = False,
    repair_node: str | None = None,
    confirm_publish: bool = False,
    max_workers: int = 4,
) -> PipelineResult:
    """Execute the default close-loop through the deterministic Task Graph.

    The legacy fixed sequence remains available through the CLI's explicit
    ``--legacy`` fallback. This path persists a contract-pinned checkpoint after
    every node, keeps the human gate separate from the test outcome, and never
    performs a remote write.
    """
    run_id = utc_now().replace(":", "").replace(".", "")
    checkpoint_path = config.paths.state / "close-loop" / "task-graph-latest.json"
    checkpoint_store = TaskCheckpointStore(checkpoint_path)
    results_by_case: dict[str, dict[str, Any]] = {}
    graph = None
    try:
        contracts = select_contracts(config.paths.cases, case_id, case_ids=case_ids)
        graph = compile_quality_task_graph(
            [contract.case_id for contract in contracts],
            contract_hashes={contract.case_id: contract.contract_hash for contract in contracts},
        )
        profile = environment_profile_status(config)
        execution_block = None if dry_run else _execution_prerequisite_block(contracts, profile)
        if execution_block:
            payload = _summary_payload(
                run_id,
                "BLOCK",
                "BLOCK",
                "NOT_RUN",
                "BLOCKED",
                "NOT_EVALUATED",
                _health_status(),
                [{"name": node.node_id, "status": "BLOCK" if node.node_id == "context.build" else "SKIPPED"} for node in graph.nodes],
                [],
                config.paths.reports / "status.md",
                0,
                [],
                profile,
            )
            payload.update(
                {
                    "execution_mode": "task_graph",
                    "blocked_reason": execution_block,
                    "task_graph": _task_graph_metadata(graph, None, checkpoint_path, max_workers=max_workers),
                    "latest_run_json": _relative_or_str(config.paths.state / "latest-run.json", config.root),
                }
            )
            config.paths.state.mkdir(parents=True, exist_ok=True)
            config.paths.state.joinpath("latest-run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            return PipelineResult(payload)

        context = ContextPacket(
            context_id=run_id,
            facts={
                "requirements": [{"case_id": contract.case_id, "contract_hash": contract.contract_hash} for contract in contracts],
                "source_authority": {
                    "config": _relative_or_str(config.path, config.root),
                    "cases": [_relative_or_str(contract.path, config.root) for contract in contracts],
                },
                "policy": {
                    "deterministic_first": True,
                    "write_gate_required": True,
                    "remote_writes": False,
                },
                "environment": profile,
            },
            source_refs=tuple(_relative_or_str(contract.path, config.root) for contract in contracts),
        )
        checkpoint = None
        if resume or repair_node:
            checkpoint = checkpoint_store.load(graph)
            if checkpoint is None:
                raise TaskGraphError("checkpoint_required", details={"path": str(checkpoint_path)})
            if repair_node:
                checkpoint = TaskGraphExecutor.invalidate_from(checkpoint, graph, repair_node)
                checkpoint_store.save_payload(checkpoint)

        contract_map = {contract.case_id: contract for contract in contracts}

        def task_runner(node: TaskNode, scoped_context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
            if node.node_id == "context.build":
                return {
                    "canonical_context": {
                        "context_id": run_id,
                        "case_ids": sorted(contract_map),
                        "source_refs": list(context.source_refs),
                    }
                }
            if node.node_id == "contract.compile":
                return {"compiled_contracts": {case_id: contract.contract_hash for case_id, contract in sorted(contract_map.items())}}
            if node.node_id.startswith("execute:"):
                selected_case_id = node.node_id.split(":", 1)[1]
                contract = contract_map[selected_case_id]
                result = run_case(
                    contract,
                    RunContext(root=config.root, evidence_dir=config.paths.evidence / run_id, environment_profile=profile),
                    dry_run=dry_run,
                )
                stamp_result_run_id(result, config.root, run_id)
                results_by_case[selected_case_id] = result
                return {node.produces[0]: result}
            if node.node_id.startswith("verify:"):
                selected_case_id = node.node_id.split(":", 1)[1]
                result = inputs.get(f"result:{selected_case_id}")
                contract = contract_map[selected_case_id]
                if not _verify_task_result(result, contract):
                    return {}
                return {
                    node.produces[0]: {
                        "case_id": selected_case_id,
                        "contract_hash": contract.contract_hash,
                        "status": result.get("status"),
                        "truth_status": result.get("truth_status"),
                        "result": result,
                    }
                }
            if node.node_id == "merge.results":
                verified = [inputs[key] for key in sorted(inputs) if key.startswith("verified:") and isinstance(inputs[key], dict)]
                results = [item["result"] for item in verified if isinstance(item.get("result"), dict)]
                return {
                    "merged_report": {
                        "case_count": len(results),
                        "verified_count": len(verified),
                        "test_outcome": _test_outcome(results, dry_run=dry_run),
                        "probe_outcome": _probe_outcome(results, dry_run=dry_run),
                        "results": results,
                    }
                }
            if node.node_id == "gate.publish":
                merged = inputs.get("merged_report")
                if not isinstance(merged, dict) or merged.get("test_outcome") != "PASS":
                    return {}
                return {
                    "approval:publish.publish": {
                        "scope": "publish.publish",
                        "approved": True,
                        "contract_hash": graph.contract_hash if graph is not None else None,
                    }
                }
            if node.node_id == "publish.publish":
                return {
                    "publication": {
                        "kind": "local_task_graph_checkpoint",
                        "remote_write": False,
                        "run_id": run_id,
                    }
                }
            return {}

        execution = TaskGraphExecutor().execute(
            graph,
            context,
            task_runner,
            approvals={"approval:publish.publish"} if confirm_publish else set(),
            checkpoint=checkpoint,
            checkpoint_writer=checkpoint_store.save,
            max_workers=max_workers,
        )
        results = _task_graph_results(execution, contracts)
        gate_results = [] if dry_run else [evaluate_write_gate(config_data=config.data, result=result).as_dict() for result in results]
        blocked_by_gate = len([gate for gate in gate_results if not gate["allowed"]])
        gate_status = _gate_status(gate_results, dry_run=dry_run)
        test_outcome = _test_outcome(results, dry_run=dry_run)
        probe_outcome = _probe_outcome(results, dry_run=dry_run)
        status = _legacy_status(test_outcome)
        if execution.status == "HOLD" and status in {"PASS", "NOT_RUN"}:
            status = "HOLD"
        elif execution.status in {"BLOCK", "FAIL"} and status in {"PASS", "NOT_RUN"}:
            status = execution.status
        workflow_status = _workflow_status(test_outcome, gate_status)
        if execution.status != "PASS":
            workflow_status = "BLOCKED"
        steps = [{"name": node.node_id, **execution.nodes.get(node.node_id, {"status": "PENDING"})} for node in graph.nodes]
        payload = _summary_payload(
            run_id,
            status,
            test_outcome,
            probe_outcome,
            workflow_status,
            gate_status,
            _health_status(),
            steps,
            results,
            config.paths.reports / "status.md",
            blocked_by_gate,
            gate_results,
            profile,
        )
        payload["execution_mode"] = "task_graph"
        payload["task_graph"] = _task_graph_metadata(graph, execution, checkpoint_path, max_workers=max_workers)
        report_path = render_status_report(results, config.paths.reports / "status.md", latest_run=payload)
        payload["report_path"] = str(report_path)
        payload["latest_run_json"] = _relative_or_str(config.paths.state / "latest-run.json", config.root)
        config.paths.state.mkdir(parents=True, exist_ok=True)
        ensure_safe_structure(payload, context="task graph latest run")
        config.paths.state.joinpath("latest-run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return PipelineResult(payload)
    except Exception as exc:
        payload = {
            "status": "ABORT",
            "workflow_status": "FAILED",
            "test_outcome": "ABORT",
            "probe_outcome": "NOT_RUN",
            "gate_status": "NOT_EVALUATED",
            "health_status": "NOT_EVALUATED",
            "run_id": run_id,
            "execution_mode": "task_graph",
            "error": type(exc).__name__,
            "message": str(exc),
            "task_graph": _task_graph_metadata(graph, None, checkpoint_path, max_workers=max_workers) if graph is not None else None,
            "results": list(results_by_case.values()),
            "latest_run_json": None,
            "report_path": None,
            "tracker_writes": {"created": 0, "updated": 0, "blocked_by_gate": 0},
        }
        return PipelineResult(payload)


def _task_graph_results(execution: Any, contracts: list[Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for contract in contracts:
        node = execution.nodes.get(f"execute:{contract.case_id}", {})
        output = node.get("output") if isinstance(node, dict) else None
        result = output.get(f"result:{contract.case_id}") if isinstance(output, dict) else None
        if isinstance(result, dict):
            results.append(result)
    return results


def _verify_task_result(result: Any, contract: Any) -> bool:
    return bool(
        isinstance(result, dict)
        and result.get("case_id") == contract.case_id
        and result.get("contract_hash") == contract.contract_hash
        and result.get("status") in {"PASS", "FAIL", "BLOCK", "NOT_RUN"}
        and isinstance(result.get("evidence"), list)
    )


def _task_graph_metadata(graph: Any, execution: Any, checkpoint_path: Path, *, max_workers: int) -> dict[str, Any]:
    metadata = {
        "schema": "quality-pilot.task-graph-execution.v1",
        "graph_id": graph.graph_id if graph is not None else None,
        "contract_hash": graph.contract_hash if graph is not None else None,
        "input_contract_hashes": (
            {case_id: contract_hash for case_id, contract_hash in graph.input_contract_hashes}
            if graph is not None
            else {}
        ),
        "checkpoint_path": str(checkpoint_path),
        "max_workers": max_workers,
        "human_gate_required": True,
    }
    if execution is not None:
        metadata.update(
            {
                "status": execution.status,
                "round": execution.round,
                "nodes": execution.nodes,
                "human_gate_status": execution.nodes.get("gate.publish", {}).get("status"),
            }
        )
    else:
        metadata["status"] = "NOT_STARTED"
    return metadata


def _execution_prerequisite_block(contracts: list[Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    if not contracts or profile.get("ready"):
        return None
    requires_environment = any(
        bool(
            (contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}).get("requires_prepared_environment")
            or (contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}).get("environment_requirements")
        )
        for contract in contracts
    )
    if not requires_environment:
        return None
    return {
        "reason": "environment_profile_required",
        "blockers": [str(item) for item in profile.get("blockers", []) if item],
        "case_count": len(contracts),
    }


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
    environment_profile: dict[str, Any],
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
        "environment_profile": environment_profile,
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
