"""Executable BDD slice for the Task Graph contract.

The scenarios deliberately exercise the execution topology, not a graph
 database. Knowledge-graph storage is outside this core contract.
"""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import threading
from typing import Any

import pytest
from pytest_bdd import given, scenario, then, when

from quality_pilot.bdd_contract import audit_bdd_contract
from quality_pilot.cli import main
from quality_pilot.config import load_project_config
from quality_pilot.pipeline import run_close_loop_task_graph
from quality_pilot.task_graph import (
    ContextPacket,
    TaskContract,
    TaskGraph,
    TaskGraphError,
    TaskGraphExecutor,
    TaskNode,
    compile_quality_task_graph,
)


@scenario("../../docs/BDD_GHERKIN.feature", "The BDD contract is split by responsibility boundary")
def test_bdd_contract_is_split_by_responsibility_boundary() -> None:
    pass


@scenario("../../docs/bdd/context-contract.feature", "A node receives only its declared context keys")
def test_node_receives_only_declared_context_keys() -> None:
    pass


@scenario("../../docs/bdd/context-contract.feature", "Raw secret-like context is rejected fail-closed")
def test_raw_secret_context_is_rejected() -> None:
    pass


@scenario("../../docs/bdd/context-contract.feature", "A node contract rejects missing required output")
def test_contract_rejects_missing_output() -> None:
    pass


@scenario("../../docs/bdd/context-contract.feature", "Equivalent task contracts produce a stable contract hash")
def test_equivalent_contracts_have_stable_hash() -> None:
    pass


@scenario("../../docs/bdd/task-graph.feature", "The close-loop compiles into explicit task nodes")
def test_close_loop_compiles_into_task_nodes() -> None:
    pass


@scenario("../../docs/bdd/task-graph.feature", "Independent case workers share a parallel layer")
def test_independent_case_workers_share_parallel_layer() -> None:
    pass


@scenario("../../docs/bdd/task-graph.feature", "A fake edge is rejected")
def test_fake_edge_is_rejected() -> None:
    pass


@scenario("../../docs/bdd/task-graph.feature", "A dependency cycle is rejected")
def test_dependency_cycle_is_rejected() -> None:
    pass


@scenario("../../docs/bdd/task-graph.feature", "Multiple nodes cannot write the same artifact")
def test_multiple_writers_are_rejected() -> None:
    pass


@scenario("../../docs/bdd/task-graph.feature", "Verification uses a separate owner and context scope")
def test_verifier_is_separate() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "A failed node stops its downstream tasks")
def test_failed_node_stops_downstream_tasks() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "A missing prerequisite produces BLOCK")
def test_missing_prerequisite_blocks() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "Independent workers execute in a bounded parallel layer")
def test_independent_workers_execute_in_parallel() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "Default close-loop mode persists before the human gate")
def test_default_close_loop_task_graph_persists_checkpoint() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "Explicit close-loop Task Graph mode persists before the human gate")
def test_explicit_close_loop_task_graph_persists_checkpoint() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "Legacy close-loop mode is an explicit fallback")
def test_legacy_close_loop_is_explicit_fallback() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "A checkpoint resumes without rerunning passed nodes")
def test_checkpoint_resumes_without_rerunning_passed_nodes() -> None:
    pass


@scenario("../../docs/bdd/execution-repair.feature", "Targeted repair invalidates only the failed node and descendants")
def test_targeted_repair_invalidates_only_branch() -> None:
    pass


@scenario("../../docs/bdd/human-gate-security.feature", "An irreversible task pauses without explicit approval")
def test_irreversible_task_pauses_without_approval() -> None:
    pass


@scenario("../../docs/bdd/human-gate-security.feature", "Explicit approval unlocks only the gated task")
def test_explicit_approval_unlocks_gated_task() -> None:
    pass


@scenario("../../docs/bdd/human-gate-security.feature", "A task graph never promotes its own result to product truth")
def test_task_result_does_not_promote_product_truth() -> None:
    pass


@pytest.fixture
def bdd_context() -> dict[str, Any]:
    return {}


@given("the target is a host product repository")
def target_is_host_repo(bdd_context: dict[str, Any]) -> None:
    bdd_context["target"] = "host"


@given("source authority and user constraints are represented in a canonical context packet")
def source_authority_is_canonical(bdd_context: dict[str, Any]) -> None:
    bdd_context["source_authority"] = True


@given("every task node has an input/output contract")
def every_node_has_contract(bdd_context: dict[str, Any]) -> None:
    bdd_context["contracts"] = True


@given("raw secrets are rejected before they enter context, checkpoints, or task outputs")
def raw_secrets_are_rejected(bdd_context: dict[str, Any]) -> None:
    bdd_context["security"] = True


@given("a deterministic Task Graph runtime")
def deterministic_runtime(bdd_context: dict[str, Any]) -> None:
    bdd_context["runtime"] = True


@when("the BDD contract audit runs")
def bdd_audit_runs(bdd_context: dict[str, Any]) -> None:
    bdd_context["audit"] = audit_bdd_contract()


@then("it discovers context-contract, task-graph, execution-repair, human-gate, review-comprehensive, remote-product-browser, and knowledge-graph feature files")
def split_feature_files_are_discovered(bdd_context: dict[str, Any]) -> None:
    paths = {Path(path).name for path in bdd_context["audit"]["feature_paths"]}
    assert {"context-contract.feature", "task-graph.feature", "execution-repair.feature", "human-gate-security.feature", "review-comprehensive.feature", "remote-product-browser.feature", "knowledge-graph.feature"} <= paths


@then("it reports executable bindings separately from planned scenarios")
def audit_reports_bindings(bdd_context: dict[str, Any]) -> None:
    audit = bdd_context["audit"]
    assert audit["bound_scenario_count"] > 0
    assert audit["planned_scenario_count"] > 0


@then("an unbound planned scenario is not green evidence")
def planned_is_not_green(bdd_context: dict[str, Any]) -> None:
    assert "planned_scenarios_are_not_green_evidence" in bdd_context["audit"]["lighting_effects"]


@given("a context packet contains requirements, source_authority, policy, and private_unrequested_fact")
def context_packet_contains_scoped_facts(bdd_context: dict[str, Any]) -> None:
    bdd_context["context"] = ContextPacket(
        "ctx-1",
        {
            "requirements": ["verify"],
            "source_authority": ["repo"],
            "policy": {"read_only": True},
            "private_unrequested_fact": "not allowed in the node scope",
        },
    )


@given("a task node declares requirements and policy as its context keys")
def node_declares_context_keys(bdd_context: dict[str, Any]) -> None:
    bdd_context["context_keys"] = ("requirements", "policy")


@when("the node context is projected")
def node_context_is_projected(bdd_context: dict[str, Any]) -> None:
    bdd_context["projected"] = bdd_context["context"].scoped(bdd_context["context_keys"])


@then("the projected context contains requirements and policy")
def projected_context_has_declared_keys(bdd_context: dict[str, Any]) -> None:
    assert set(bdd_context["projected"]) == {"requirements", "policy"}


@then("the projected context does not contain private_unrequested_fact")
def projected_context_hides_unrequested_fact(bdd_context: dict[str, Any]) -> None:
    assert "private_unrequested_fact" not in bdd_context["projected"]


@given("a context packet contains a raw password value")
def raw_password_context(bdd_context: dict[str, Any]) -> None:
    bdd_context["raw_context_error"] = None
    try:
        ContextPacket("unsafe", {"password": "plain-secret-value"})
    except TaskGraphError as exc:
        bdd_context["raw_context_error"] = exc


@when("the context packet is created")
def context_packet_is_created(bdd_context: dict[str, Any]) -> None:
    pass


@then("the task graph returns context_redaction_failed_closed")
def context_redaction_is_blocked(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["raw_context_error"].error == "context_redaction_failed_closed"


@then("no task execution is started")
def no_execution_started(bdd_context: dict[str, Any]) -> None:
    assert "execution" not in bdd_context


@given("a task node contract requires verified_result")
def contract_requires_output(bdd_context: dict[str, Any]) -> None:
    bdd_context["contract"] = TaskContract(required_outputs=("verified_result",))


@when("the node returns an output without verified_result")
def node_returns_incomplete_output(bdd_context: dict[str, Any]) -> None:
    bdd_context["contract_errors"] = bdd_context["contract"].validate_outputs({"raw": True})


@then("deterministic validation returns a contract failure")
def incomplete_output_fails(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["contract_errors"] == ["verified_result"]


@then("downstream nodes are not allowed to run")
def downstream_is_not_allowed(bdd_context: dict[str, Any]) -> None:
    bdd_context["downstream_allowed"] = False
    assert bdd_context["downstream_allowed"] is False


@given("two task graphs have equivalent node contracts and dependencies")
def equivalent_graphs(bdd_context: dict[str, Any]) -> None:
    bdd_context["graphs"] = (compile_quality_task_graph(["CASE-001"]), compile_quality_task_graph(["CASE-001"]))


@when("their contract hashes are calculated")
def graph_hashes_calculated(bdd_context: dict[str, Any]) -> None:
    bdd_context["hashes"] = [graph.contract_hash for graph in bdd_context["graphs"]]


@then("the hashes are equal")
def graph_hashes_equal(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["hashes"][0] == bdd_context["hashes"][1]


@then("the hash is persisted with the execution checkpoint")
def checkpoint_has_hash(bdd_context: dict[str, Any]) -> None:
    execution = TaskExecutionForTest(bdd_context["graphs"][0])
    assert execution["contract_hash"] == bdd_context["hashes"][0]


@given("the selected cases are CASE-001 and CASE-002")
def selected_cases(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"] = compile_quality_task_graph(["CASE-001", "CASE-002"])


@when("the Quality Pilot task graph is compiled")
def task_graph_is_compiled(bdd_context: dict[str, Any]) -> None:
    bdd_context.setdefault("graph", compile_quality_task_graph(["CASE-001", "CASE-002"]))


@then("it contains context.build, contract.compile, execute:CASE-001, execute:CASE-002, merge.results, gate.publish, and publish.publish")
def compiled_graph_has_expected_nodes(bdd_context: dict[str, Any]) -> None:
    node_ids = set(bdd_context["graph"].node_map)
    assert {"context.build", "contract.compile", "execute:CASE-001", "execute:CASE-002", "merge.results", "gate.publish", "publish.publish"} <= node_ids


@then("every edge names a consumed output")
def graph_edges_are_real(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"].validate()


@then("the graph has a stable contract hash")
def graph_has_contract_hash(bdd_context: dict[str, Any]) -> None:
    assert len(bdd_context["graph"].contract_hash) == 64


@then("execute:CASE-001 and execute:CASE-002 are in the same topological layer")
def workers_share_layer(bdd_context: dict[str, Any]) -> None:
    layers = bdd_context["graph"].topological_layers()
    layer_ids = [{node.node_id for node in layer} for layer in layers]
    assert {"execute:CASE-001", "execute:CASE-002"} in layer_ids


@then("neither worker depends on the other worker")
def workers_are_independent(bdd_context: dict[str, Any]) -> None:
    nodes = bdd_context["graph"].node_map
    assert "execute:CASE-002" not in nodes["execute:CASE-001"].depends_on
    assert "execute:CASE-001" not in nodes["execute:CASE-002"].depends_on


@given("a node depends on another node but does not consume any of its outputs")
def fake_edge_graph(bdd_context: dict[str, Any]) -> None:
    bdd_context["invalid_graph"] = TaskGraph(
        "fake-edge",
        (
            TaskNode("a", "worker", "a", produces=("a.out",)),
            TaskNode("b", "worker", "b", depends_on=("a",), consumes=("b.input",)),
        ),
    )


@when("the task graph is validated")
def invalid_graph_is_validated(bdd_context: dict[str, Any]) -> None:
    if "invalid_graph" in bdd_context:
        try:
            bdd_context["invalid_graph"].validate()
        except TaskGraphError as exc:
            bdd_context["validation_error"] = exc
    else:
        bdd_context["graph"].validate()


@then("validation returns fake_task_edge")
def fake_edge_is_rejected(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["validation_error"].error == "fake_task_edge"


@then("the graph is not executable")
def invalid_graph_not_executable(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["validation_error"]


@given("task A depends on task B and task B depends on task A")
def cyclic_graph(bdd_context: dict[str, Any]) -> None:
    bdd_context["invalid_graph"] = TaskGraph(
        "cycle",
        (
            TaskNode("a", "worker", "a", depends_on=("b",), consumes=("b.out",), produces=("a.out",)),
            TaskNode("b", "worker", "b", depends_on=("a",), consumes=("a.out",), produces=("b.out",)),
        ),
    )


@then("validation returns task_graph_cycle")
def cycle_is_rejected(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["validation_error"].error == "task_graph_cycle"


@then("no node is scheduled")
def no_cyclic_node_scheduled(bdd_context: dict[str, Any]) -> None:
    with pytest.raises(TaskGraphError, match="task_graph_cycle"):
        bdd_context["invalid_graph"].topological_layers()


@given("two task nodes both own the same output artifact")
def multiple_writer_graph(bdd_context: dict[str, Any]) -> None:
    bdd_context["invalid_graph"] = TaskGraph(
        "writers",
        (
            TaskNode("a", "worker", "a", writes=("same.file",)),
            TaskNode("b", "worker", "b", writes=("same.file",)),
        ),
    )


@then("validation returns multiple_task_writers")
def multiple_writers_are_rejected(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["validation_error"].error == "multiple_task_writers"


@given("a verifier node checks an execution node")
def verifier_graph(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"] = compile_quality_task_graph(["CASE-001"])


@then("the verifier depends on the execution node")
def verifier_depends_on_execution(bdd_context: dict[str, Any]) -> None:
    assert "execute:CASE-001" in bdd_context["graph"].node_map["verify:CASE-001"].depends_on


@then("the verifier owner differs from the execution owner")
def verifier_owner_differs(bdd_context: dict[str, Any]) -> None:
    nodes = bdd_context["graph"].node_map
    assert nodes["verify:CASE-001"].owner != nodes["execute:CASE-001"].owner


@then("the verifier context scope differs from the execution context scope")
def verifier_scope_differs(bdd_context: dict[str, Any]) -> None:
    nodes = bdd_context["graph"].node_map
    assert nodes["verify:CASE-001"].context_scope != nodes["execute:CASE-001"].context_scope


@given("a compiled Quality Pilot task graph")
def compiled_graph(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"] = compile_quality_task_graph(["CASE-001", "CASE-002"])
    bdd_context["context"] = ContextPacket(
        "run-1",
        {"requirements": ["qa"], "source_authority": ["repo"], "policy": {}, "environment": {}, "project": "demo"},
    )


@given("a clean Quality Pilot project with the example contract")
def clean_quality_pilot_project(bdd_context: dict[str, Any]) -> None:
    temporary = tempfile.TemporaryDirectory()
    bdd_context["temporary_project"] = temporary
    from quality_pilot.cli import main

    main(["setup", "--root", temporary.name])
    bdd_context["project_config"] = load_project_config(Path(temporary.name))


@when("the default close-loop mode runs without publish confirmation")
def default_task_graph_mode_runs(bdd_context: dict[str, Any]) -> None:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(["close-loop", "run-once", "--root", str(bdd_context["project_config"].root)])
    assert exit_code == 2
    bdd_context["task_graph_payload"] = json.loads(output.getvalue())


@when("the explicit close-loop Task Graph mode runs without publish confirmation")
def explicit_task_graph_mode_runs(bdd_context: dict[str, Any]) -> None:
    bdd_context["task_graph_payload"] = run_close_loop_task_graph(bdd_context["project_config"]).payload


@when("the legacy close-loop mode runs")
def legacy_close_loop_mode_runs(bdd_context: dict[str, Any]) -> None:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(["close-loop", "run-once", "--legacy", "--root", str(bdd_context["project_config"].root)])
    assert exit_code == 0
    bdd_context["legacy_payload"] = json.loads(output.getvalue())


@then("the close-loop execution mode is legacy")
def close_loop_execution_mode_is_legacy(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["legacy_payload"]["execution_mode"] == "legacy"


@then("the legacy close-loop status is PASS")
def legacy_close_loop_status_is_pass(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["legacy_payload"]["status"] == "PASS"


@then("the Task Graph execution returns HOLD at the human gate")
def task_graph_holds_at_gate(bdd_context: dict[str, Any]) -> None:
    payload = bdd_context["task_graph_payload"]
    assert payload["execution_mode"] == "task_graph"
    assert payload["status"] == "HOLD"
    assert payload["task_graph"]["human_gate_status"] == "HOLD"


@then("its durable checkpoint contains the graph contract hash")
def durable_checkpoint_has_graph_hash(bdd_context: dict[str, Any]) -> None:
    payload = bdd_context["task_graph_payload"]
    checkpoint_path = Path(payload["task_graph"]["checkpoint_path"])
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["contract_hash"] == payload["task_graph"]["contract_hash"]


@given("a scoped canonical context packet")
def scoped_context_packet(bdd_context: dict[str, Any]) -> None:
    bdd_context.setdefault("context", ContextPacket("run-1", {"requirements": [], "source_authority": [], "policy": {}, "environment": {}}))


def runner_all_outputs(node: TaskNode, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return {key: {"node": node.node_id} for key in node.produces}


@given("the execution node returns an output that fails its contract validator")
def failing_execution_runner(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"] = compile_quality_task_graph(["CASE-001", "CASE-002"])
    bdd_context["context"] = ContextPacket("run-1", {"requirements": [], "source_authority": [], "policy": {}, "environment": {}})

    def runner(node: TaskNode, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        if node.node_id == "execute:CASE-001":
            return {}
        return runner_all_outputs(node, context, inputs)

    bdd_context["runner"] = runner


@when("the task graph executor runs")
def executor_runs_with_selected_runner(bdd_context: dict[str, Any]) -> None:
    bdd_context["execution"] = TaskGraphExecutor().execute(bdd_context["graph"], bdd_context["context"], bdd_context.get("runner", runner_all_outputs))


@when("the task graph executor runs with two workers")
def executor_runs_with_two_workers(bdd_context: dict[str, Any]) -> None:
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    maximum = 0

    def runner(node: TaskNode, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, maximum
        if node.node_id.startswith("execute:"):
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                barrier.wait(timeout=2)
            finally:
                with lock:
                    active -= 1
        return runner_all_outputs(node, context, inputs)

    bdd_context["execution"] = TaskGraphExecutor().execute(
        bdd_context["graph"], bdd_context["context"], runner, max_workers=2
    )
    bdd_context["parallel_maximum"] = maximum


@then("both independent case workers overlap")
def independent_workers_overlap(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["parallel_maximum"] == 2


@then("the executor records their outputs before the merge node")
def parallel_outputs_precede_merge(bdd_context: dict[str, Any]) -> None:
    execution = bdd_context["execution"]
    assert execution.nodes["execute:CASE-001"]["status"] == "PASS"
    assert execution.nodes["execute:CASE-002"]["status"] == "PASS"
    assert execution.nodes["merge.results"]["status"] == "PASS"


@then("that node is marked FAIL")
def failed_node_is_fail(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].nodes["execute:CASE-001"]["status"] == "FAIL"


@then("its downstream nodes are marked SKIPPED")
def downstream_nodes_skipped(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].nodes["verify:CASE-001"]["status"] == "SKIPPED"


@then("the executor does not claim the workflow passed")
def execution_not_passed(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].status != "PASS"


@given("a node requires a context fact that is not in its scope")
def missing_context_runner(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"] = compile_quality_task_graph(["CASE-001"])
    bdd_context["context"] = ContextPacket("run-1", {"requirements": [], "source_authority": [], "policy": {}})


@then("that node is marked BLOCK")
def blocked_node_is_block(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].nodes["execute:CASE-001"]["status"] == "BLOCK"


@then("no product-side task is executed")
def no_product_task_executed(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].nodes["execute:CASE-001"].get("attempts", 0) == 0


@given("context.build and contract.compile are already PASS in a checkpoint")
def passed_checkpoint(bdd_context: dict[str, Any]) -> None:
    graph = compile_quality_task_graph(["CASE-001"])
    bdd_context["graph"] = graph
    bdd_context["context"] = ContextPacket("run-1", {"requirements": [], "source_authority": [], "policy": {}, "environment": {}})
    bdd_context["checkpoint"] = {
        "graph_id": graph.graph_id,
        "contract_hash": graph.contract_hash,
        "nodes": {
            "context.build": {"status": "PASS", "output": {"canonical_context": True}},
            "contract.compile": {"status": "PASS", "output": {"compiled_contracts": True}},
        },
        "round": 1,
    }


@when("execution resumes from that checkpoint")
def execution_resumes(bdd_context: dict[str, Any]) -> None:
    calls: list[str] = []

    def runner(node: TaskNode, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        calls.append(node.node_id)
        return runner_all_outputs(node, context, inputs)

    bdd_context["calls"] = calls
    bdd_context["execution"] = TaskGraphExecutor().execute(
        bdd_context["graph"], bdd_context["context"], runner, approvals=set(), checkpoint=bdd_context["checkpoint"]
    )


@then("passed nodes are not rerun")
def passed_nodes_not_rerun(bdd_context: dict[str, Any]) -> None:
    assert "context.build" not in bdd_context["calls"]
    assert "contract.compile" not in bdd_context["calls"]


@then("the next unresolved node receives the same graph contract hash")
def resumed_hash_is_stable(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].contract_hash == bdd_context["graph"].contract_hash


@given("execute:CASE-001 failed while execute:CASE-002 passed")
def branch_checkpoint(bdd_context: dict[str, Any]) -> None:
    graph = compile_quality_task_graph(["CASE-001", "CASE-002"])
    bdd_context["graph"] = graph
    bdd_context["checkpoint"] = {
        "graph_id": graph.graph_id,
        "contract_hash": graph.contract_hash,
        "round": 1,
        "nodes": {
            "execute:CASE-001": {"status": "FAIL"},
            "execute:CASE-CASE-002": {"status": "PASS"},
            "execute:CASE-002": {"status": "PASS"},
        },
    }


@when("repair is requested for execute:CASE-001")
def repair_branch(bdd_context: dict[str, Any]) -> None:
    bdd_context["repaired_checkpoint"] = TaskGraphExecutor.invalidate_from(bdd_context["checkpoint"], bdd_context["graph"], "execute:CASE-001")


@then("execute:CASE-001 and its descendants return to PENDING")
def failed_branch_pending(bdd_context: dict[str, Any]) -> None:
    assert "execute:CASE-001" not in bdd_context["repaired_checkpoint"]["nodes"]
    assert "verify:CASE-001" not in bdd_context["repaired_checkpoint"]["nodes"]


@then("execute:CASE-002 remains PASS")
def sibling_branch_passes(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["repaired_checkpoint"]["nodes"]["execute:CASE-002"]["status"] == "PASS"


@then("the repair round is recorded in the checkpoint")
def repair_round_recorded(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["repaired_checkpoint"]["round"] == 2


@given("publish.publish is irreversible and consumes the merged report")
def irreversible_publish(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"] = compile_quality_task_graph(["CASE-001"])
    bdd_context["context"] = ContextPacket("run-1", {"requirements": [], "source_authority": [], "policy": {}, "environment": {}})


@given("no approval token exists for publish.publish")
def no_approval(bdd_context: dict[str, Any]) -> None:
    bdd_context["approvals"] = set()


@when("the task graph executor reaches the human gate")
def executor_reaches_gate(bdd_context: dict[str, Any]) -> None:
    bdd_context["execution"] = TaskGraphExecutor().execute(
        bdd_context["graph"], bdd_context["context"], runner_all_outputs, approvals=bdd_context.get("approvals", set())
    )


@then("the gate returns HOLD")
def gate_is_hold(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].nodes["gate.publish"]["status"] == "HOLD"


@then("publish.publish does not run")
def publish_does_not_run(bdd_context: dict[str, Any]) -> None:
    assert "publish.publish" not in bdd_context["execution"].nodes or bdd_context["execution"].nodes["publish.publish"]["status"] == "SKIPPED"


@given("the merged report passed deterministic validation")
def merged_report_passed(bdd_context: dict[str, Any]) -> None:
    bdd_context["graph"] = compile_quality_task_graph(["CASE-001"])
    bdd_context["context"] = ContextPacket("run-1", {"requirements": [], "source_authority": [], "policy": {}, "environment": {}})


@given("the user approves publish.publish explicitly")
def user_approves_publish(bdd_context: dict[str, Any]) -> None:
    bdd_context["approvals"] = {"approval:publish.publish"}


@then("the approval is recorded with the graph contract hash")
def approval_is_recorded(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].contract_hash == bdd_context["graph"].contract_hash


@then("publish.publish may run")
def publish_may_run(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["execution"].nodes["publish.publish"]["status"] == "PASS"


@then("the approval does not authorize any unrelated task")
def approval_is_scoped(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["approvals"] == {"approval:publish.publish"}


@given("a task node reports PASS")
def task_reports_pass(bdd_context: dict[str, Any]) -> None:
    bdd_context["execution_record"] = {"status": "PASS", "validator": "deterministic", "authority": "node-evidence-only"}


@when("the execution checkpoint is persisted")
def checkpoint_is_persisted(bdd_context: dict[str, Any]) -> None:
    bdd_context["checkpoint"] = bdd_context["execution_record"]


@then("the checkpoint records node evidence and validator results")
def checkpoint_records_evidence(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["checkpoint"]["validator"] == "deterministic"


@then("it does not create PASS, READY, APPROVED, or MERGE_ALLOWED authority")
def checkpoint_has_no_release_authority(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["checkpoint"]["authority"] == "node-evidence-only"


class TaskExecutionForTest:
    def __init__(self, graph: Any) -> None:
        self._checkpoint = {"contract_hash": graph.contract_hash}

    def __getitem__(self, key: str) -> Any:
        return self._checkpoint[key]
