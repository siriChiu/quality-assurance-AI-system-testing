from __future__ import annotations

import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, scenario, then, when

from quality_pilot.cli import main
from quality_pilot.config import load_project_config
from quality_pilot.graph_engineering import (
    GraphStore,
    build_qa_candidate_snapshot,
    graph_extract,
    graph_fuse,
    graph_ontology,
    graph_quality_gate,
    graph_scope,
    graph_serve,
    graph_paths,
)
from quality_pilot.pipeline import run_close_loop_task_graph
from quality_pilot.task_graph import compile_graph_engineering_task_graph


@pytest.fixture
def bdd_context() -> dict[str, Any]:
    return {}


@scenario("../../docs/bdd/knowledge-graph.feature", "Scope requires competency questions before ontology work")
def test_graph_scope_requires_questions() -> None:
    pass


@scenario("../../docs/bdd/knowledge-graph.feature", "Ontology validation enforces typed relation domains and ranges")
def test_graph_ontology_validation() -> None:
    pass


@scenario("../../docs/bdd/knowledge-graph.feature", "Extraction rejects a fact without provenance evidence")
def test_graph_extraction_requires_provenance() -> None:
    pass


@scenario("../../docs/bdd/knowledge-graph.feature", "Structural graph checks do not become quality PASS without gold labels")
def test_graph_quality_gate_requires_gold() -> None:
    pass


@scenario("../../docs/bdd/knowledge-graph.feature", "Fusion is previewed before a reversible human-approved merge")
def test_graph_fusion_requires_human_gate() -> None:
    pass


@scenario("../../docs/bdd/knowledge-graph.feature", "Serving returns a provenance-preserving read-only subgraph")
def test_graph_serving_is_read_only() -> None:
    pass


@scenario("../../docs/bdd/knowledge-graph.feature", "The nine-stage graph workflow is compiled as a Task Graph")
def test_graph_workflow_task_graph() -> None:
    pass


@scenario("../../docs/bdd/knowledge-graph.feature", "Existing Quality Pilot artifacts feed the graph read model")
def test_qa_artifacts_feed_graph_read_model() -> None:
    pass


def _fresh(context: dict[str, Any]) -> None:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    with redirect_stdout(StringIO()):
        assert main(["setup", "--root", str(root)]) == 0
    context["temporary"] = temporary
    context["config"] = load_project_config(root)


def _scope(context: dict[str, Any]) -> None:
    context["scope"] = graph_scope(context["config"], questions=["Which test run produced evidence for this case?"])


def _ontology(context: dict[str, Any]) -> None:
    _scope(context)
    context["ontology"] = graph_ontology(context["config"])


def _write_candidate(context: dict[str, Any], payload: dict[str, Any], name: str = "candidate.json") -> Path:
    path = context["config"].root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    context["candidate"] = path
    return path


def _entity(entity_id: str, *, canonical: str = "PR 1", entity_type: str = "PullRequest", evidence: str = "PR evidence") -> dict[str, Any]:
    item = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "canonical": canonical,
        "provenance": {"source_ref": f"source:{entity_id}", "evidence": evidence, "confidence": 1.0},
    }
    return item


@given("a fresh graph project")
def fresh_graph_project(bdd_context: dict[str, Any]) -> None:
    _fresh(bdd_context)


@when("graph scope is requested without a competency question")
def scope_without_question(bdd_context: dict[str, Any]) -> None:
    bdd_context["scope"] = graph_scope(bdd_context["config"], questions=[])


@then("the graph scope status is BLOCK")
def scope_is_blocked(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["scope"]["status"] == "BLOCK"


@then("graph scope does not invent a question")
def scope_does_not_invent(bdd_context: dict[str, Any]) -> None:
    assert not bdd_context["config"].paths.state.joinpath("graph/scope.json").exists()


@given("a fresh graph project with a valid scope")
def fresh_scoped_graph_project(bdd_context: dict[str, Any]) -> None:
    _fresh(bdd_context)
    _scope(bdd_context)


@when("the starter graph ontology is validated")
def validate_starter_ontology(bdd_context: dict[str, Any]) -> None:
    bdd_context["ontology"] = graph_ontology(bdd_context["config"])


@then("the graph ontology status is READY")
def ontology_is_ready(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["ontology"]["status"] == "READY"


@then("the graph ontology has typed relations")
def ontology_has_typed_relations(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["ontology"]["relation_count"] > 0


@given("a valid graph ontology and a candidate entity without evidence")
def invalid_candidate_graph(bdd_context: dict[str, Any]) -> None:
    _fresh(bdd_context)
    _ontology(bdd_context)
    _write_candidate(bdd_context, {"entities": [{"entity_id": "bad", "entity_type": "PullRequest", "canonical": "Bad"}]})


@when("the graph candidate is extracted")
def extract_invalid_candidate(bdd_context: dict[str, Any]) -> None:
    bdd_context["extraction"] = graph_extract(bdd_context["config"], input_paths=[bdd_context["candidate"]])


@then("the graph extraction status is BLOCK")
def extraction_is_blocked(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["extraction"]["status"] == "BLOCK"


@then("the graph remains without that entity")
def graph_has_no_invalid_entity(bdd_context: dict[str, Any]) -> None:
    store = GraphStore(graph_paths(bdd_context["config"]))
    assert not any(item.get("entity_id") == "bad" for item in store.entities())


@given("a graph with a provenance-backed entity")
def graph_with_entity(bdd_context: dict[str, Any]) -> None:
    _fresh(bdd_context)
    _ontology(bdd_context)
    _write_candidate(bdd_context, {"entities": [_entity("pr-1")]})
    assert graph_extract(bdd_context["config"], input_paths=[bdd_context["candidate"]])["status"] == "READY"


@when("the graph quality gate runs without adjudicated labels")
def quality_gate_without_gold(bdd_context: dict[str, Any]) -> None:
    bdd_context["gate"] = graph_quality_gate(bdd_context["config"])


@then("the graph quality gate status is HOLD")
def graph_gate_is_hold(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["gate"]["status"] == "HOLD"


@then("the reason is gold_labels_required")
def graph_reason_gold_labels(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["gate"]["reason"] == "gold_labels_required"


@given("a graph with two exact duplicate entities")
def graph_with_duplicates(bdd_context: dict[str, Any]) -> None:
    _fresh(bdd_context)
    _ontology(bdd_context)
    _write_candidate(bdd_context, {"entities": [_entity("pr-a"), _entity("pr-b")]})
    assert graph_extract(bdd_context["config"], input_paths=[bdd_context["candidate"]])["status"] == "READY"


@when("the graph fusion plan runs without confirmation")
def fusion_without_confirmation(bdd_context: dict[str, Any]) -> None:
    bdd_context["fusion"] = graph_fuse(bdd_context["config"], confirm=False)


@then("the graph fusion status is HOLD")
def fusion_is_hold(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["fusion"]["status"] == "HOLD"


@then("the reason is human_fusion_approval_required")
def fusion_requires_approval(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["fusion"]["reason"] == "human_fusion_approval_required"


@given("a graph with a linked relation")
def graph_with_relation(bdd_context: dict[str, Any]) -> None:
    _fresh(bdd_context)
    _ontology(bdd_context)
    payload = {
        "entities": [_entity("pr-1"), _entity("case-1", canonical="CASE-1", entity_type="TestCase", evidence="CASE-1 contract")],
        "relations": [{
            "relation_id": "r-1",
            "relation_type": "HAS_CASE",
            "subject_id": "pr-1",
            "object_id": "case-1",
            "provenance": {"source_ref": "source:pr-1", "evidence": "PR 1 has CASE-1", "confidence": 1.0},
        }],
    }
    _write_candidate(bdd_context, payload)
    assert graph_extract(bdd_context["config"], input_paths=[bdd_context["candidate"]])["status"] == "READY"


@when("the graph serves one hop from the source entity")
def serve_graph(bdd_context: dict[str, Any]) -> None:
    bdd_context["serving"] = graph_serve(bdd_context["config"], entity="pr-1", hops=1)


@then("the graph serving status is PASS")
def serving_is_pass(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["serving"]["status"] == "PASS"


@then("every served relation has provenance")
def served_relations_have_provenance(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["serving"]["relations"]
    assert all(item.get("provenance") for item in bdd_context["serving"]["relations"])


@when("the Knowledge Graph Task Graph is compiled")
def compile_graph_workflow(bdd_context: dict[str, Any]) -> None:
    bdd_context["task_graph"] = compile_graph_engineering_task_graph()


@given("a clean Quality Pilot project with a canonical case run")
def clean_project_with_canonical_run(bdd_context: dict[str, Any]) -> None:
    _fresh(bdd_context)
    bdd_context["run"] = run_close_loop_task_graph(bdd_context["config"], confirm_publish=True).payload
    assert bdd_context["run"]["status"] == "PASS"


@when("the QA graph adapter projects existing cases runs and evidence")
def project_qa_artifacts(bdd_context: dict[str, Any]) -> None:
    bdd_context["candidate"] = build_qa_candidate_snapshot(bdd_context["config"])


@then("the graph candidate source mode is quality_pilot_canonical_artifacts")
def candidate_source_mode_is_qa(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["candidate"]["source_mode"] == "quality_pilot_canonical_artifacts"


@then("the graph candidate contains a TestCase, TestRun, and Evidence")
def candidate_contains_canonical_types(bdd_context: dict[str, Any]) -> None:
    types = {item.get("entity_type") for item in bdd_context["candidate"]["entities"]}
    assert {"TestCase", "TestRun", "Evidence"} <= types


@then("entity extraction fans out before relation and event extraction")
def extraction_topology(bdd_context: dict[str, Any]) -> None:
    graph = bdd_context["task_graph"]
    assert "graph.source.project" in graph.node_map
    assert graph.node_map["graph.extract.entities"].depends_on == ("graph.ontology", "graph.source.project")
    assert graph.node_map["graph.extract.relations"].depends_on == ("graph.ontology", "graph.extract.entities")
    assert graph.node_map["graph.extract.events"].depends_on == ("graph.ontology", "graph.extract.entities")


@then("fusion has an explicit human gate")
def fusion_topology_gate(bdd_context: dict[str, Any]) -> None:
    graph = bdd_context["task_graph"]
    assert "approval:graph.fusion.apply" in graph.node_map["graph.fusion.gate"].produces
    assert graph.node_map["graph.fusion.apply"].irreversible is True


@then("the graph workflow has a checkpoint contract")
def graph_checkpoint_contract(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["task_graph"].contract_hash
