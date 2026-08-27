"""Deterministic task-graph primitives for reliable agent workflows.

This module deliberately does not depend on a graph database.  A task graph is
an execution topology: each node receives a scoped context packet, produces a
contract-checked result, and persists a checkpoint before downstream work runs.
Knowledge-graph storage is a separate read-model concern and is not required by
this runtime; adapters may project its inputs from source-authoritative QA artifacts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .security import RedactionError, ensure_safe_structure

TASK_GRAPH_SCHEMA = "quality-pilot.task-graph.v1"
TASK_CONTEXT_SCHEMA = "quality-pilot.task-context.v1"
TASK_CONTRACT_SCHEMA = "quality-pilot.task-contract.v1"
TASK_CHECKPOINT_SCHEMA = "quality-pilot.task-graph-checkpoint.v1"
TASK_GRAPH_MAX_WORKERS = 16


class TaskGraphError(ValueError):
    """A task graph is invalid or cannot safely execute."""

    def __init__(self, error: str, message: str | None = None, *, details: dict[str, Any] | None = None) -> None:
        self.error = error
        self.details = details or {}
        super().__init__(message or error)


class TaskGraphHold(TaskGraphError):
    """A node is valid but cannot be promoted without more evidence or a decision."""


Validator = Callable[[dict[str, Any]], Iterable[str]]
Runner = Callable[["TaskNode", dict[str, Any], dict[str, Any]], dict[str, Any]]
CheckpointWriter = Callable[["TaskExecution"], None]


@dataclass(frozen=True)
class ContextPacket:
    """The facts a node is allowed to treat as input."""

    context_id: str
    facts: Mapping[str, Any]
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.context_id).strip():
            raise TaskGraphError("context_id_required")
        safe_facts = dict(self.facts)
        try:
            ensure_safe_structure(safe_facts, context="task graph context")
        except RedactionError as exc:
            raise TaskGraphError("context_redaction_failed_closed") from exc
        object.__setattr__(self, "facts", safe_facts)
        object.__setattr__(self, "source_refs", tuple(str(item) for item in self.source_refs if str(item).strip()))

    def scoped(self, keys: Iterable[str]) -> dict[str, Any]:
        requested = tuple(dict.fromkeys(str(key) for key in keys))
        missing = [key for key in requested if key not in self.facts]
        if missing:
            raise TaskGraphError("context_fact_missing", details={"context_id": self.context_id, "missing": missing})
        return {key: copy.deepcopy(self.facts[key]) for key in requested}

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": TASK_CONTEXT_SCHEMA,
            "context_id": self.context_id,
            "facts": copy.deepcopy(dict(self.facts)),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class TaskContract:
    """Input/output obligations for one task node."""

    required_inputs: tuple[str, ...] = ()
    required_outputs: tuple[str, ...] = ()
    validator_name: str = "required_outputs"
    side_effect_boundary: str = "read-only"
    max_attempts: int = 1
    validator: Validator | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise TaskGraphError("task_contract_max_attempts_invalid")
        if not str(self.side_effect_boundary).strip():
            raise TaskGraphError("task_contract_side_effect_boundary_required")
        object.__setattr__(self, "required_inputs", tuple(dict.fromkeys(str(item) for item in self.required_inputs)))
        object.__setattr__(self, "required_outputs", tuple(dict.fromkeys(str(item) for item in self.required_outputs)))

    def validate_inputs(self, inputs: Mapping[str, Any]) -> list[str]:
        return [key for key in self.required_inputs if key not in inputs]

    def validate_outputs(self, outputs: Mapping[str, Any]) -> list[str]:
        errors = [key for key in self.required_outputs if key not in outputs]
        if self.validator is not None:
            errors.extend(str(item) for item in self.validator(dict(outputs)))
        return list(dict.fromkeys(errors))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": TASK_CONTRACT_SCHEMA,
            "required_inputs": list(self.required_inputs),
            "required_outputs": list(self.required_outputs),
            "validator": self.validator_name,
            "side_effect_boundary": self.side_effect_boundary,
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True)
class TaskNode:
    node_id: str
    kind: str
    owner: str
    depends_on: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    context_keys: tuple[str, ...] = ()
    context_scope: str = "default"
    contract: TaskContract = field(default_factory=TaskContract)
    verifies: str | None = None
    repair_for: str | None = None
    irreversible: bool = False

    def __post_init__(self) -> None:
        if not str(self.node_id).strip():
            raise TaskGraphError("task_node_id_required")
        if not str(self.owner).strip():
            raise TaskGraphError("task_node_owner_required", details={"node_id": self.node_id})
        if self.kind not in {"worker", "verifier", "merge", "human_gate", "repair"}:
            raise TaskGraphError("task_node_kind_invalid", details={"node_id": self.node_id, "kind": self.kind})
        for name in ("depends_on", "consumes", "produces", "writes", "context_keys"):
            object.__setattr__(self, name, tuple(dict.fromkeys(str(item) for item in getattr(self, name))))

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "owner": self.owner,
            "depends_on": list(self.depends_on),
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "writes": list(self.writes),
            "context_keys": list(self.context_keys),
            "context_scope": self.context_scope,
            "contract": self.contract.as_dict(),
            "verifies": self.verifies,
            "repair_for": self.repair_for,
            "irreversible": self.irreversible,
        }


@dataclass(frozen=True)
class TaskGraph:
    graph_id: str
    nodes: tuple[TaskNode, ...]
    max_rounds: int = 1
    human_gate_required: bool = True
    input_contract_hashes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not str(self.graph_id).strip():
            raise TaskGraphError("task_graph_id_required")
        if self.max_rounds < 1:
            raise TaskGraphError("task_graph_max_rounds_invalid")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        raw_hashes = self.input_contract_hashes.items() if isinstance(self.input_contract_hashes, Mapping) else self.input_contract_hashes
        object.__setattr__(
            self,
            "input_contract_hashes",
            tuple(sorted((str(case_id), str(contract_hash)) for case_id, contract_hash in raw_hashes)),
        )

    @property
    def node_map(self) -> dict[str, TaskNode]:
        return {node.node_id: node for node in self.nodes}

    def validate(self) -> None:
        nodes = self.node_map
        if len(nodes) != len(self.nodes):
            raise TaskGraphError("duplicate_task_node_id")
        for node in self.nodes:
            unknown = [dep for dep in node.depends_on if dep not in nodes]
            if unknown:
                raise TaskGraphError("task_dependency_missing", details={"node_id": node.node_id, "dependencies": unknown})
            if node.contract.required_inputs and not set(node.contract.required_inputs).issubset(set(node.consumes)):
                raise TaskGraphError(
                    "task_contract_input_not_declared",
                    details={"node_id": node.node_id, "missing_declarations": sorted(set(node.contract.required_inputs) - set(node.consumes))},
                )
            for dependency in node.depends_on:
                producer = nodes[dependency]
                if not set(producer.produces).intersection(node.consumes):
                    raise TaskGraphError(
                        "fake_task_edge",
                        details={"from": dependency, "to": node.node_id, "reason": "downstream does not consume producer output"},
                    )
        writers: dict[str, str] = {}
        for node in self.nodes:
            for artifact in node.writes:
                if artifact in writers:
                    raise TaskGraphError("multiple_task_writers", details={"artifact": artifact, "writers": [writers[artifact], node.node_id]})
                writers[artifact] = node.node_id
        for node in self.nodes:
            if node.verifies:
                if node.kind != "verifier" or node.verifies not in nodes:
                    raise TaskGraphError("verifier_target_invalid", details={"node_id": node.node_id, "target": node.verifies})
                target = nodes[node.verifies]
                if node.verifies not in node.depends_on:
                    raise TaskGraphError("verifier_dependency_missing", details={"node_id": node.node_id, "target": node.verifies})
                if node.owner == target.owner or node.context_scope == target.context_scope:
                    raise TaskGraphError("verifier_context_not_separate", details={"node_id": node.node_id, "target": node.verifies})
            if node.repair_for and node.repair_for not in nodes:
                raise TaskGraphError("repair_target_invalid", details={"node_id": node.node_id, "target": node.repair_for})
            if node.irreversible and self.human_gate_required:
                gate = next(
                    (
                        nodes[dep]
                        for dep in node.depends_on
                        if nodes[dep].kind == "human_gate" and f"approval:{node.node_id}" in nodes[dep].produces
                    ),
                    None,
                )
                if gate is None or f"approval:{node.node_id}" not in node.consumes:
                    raise TaskGraphError("irreversible_task_missing_human_gate", details={"node_id": node.node_id})
        self.topological_layers()

    def topological_layers(self) -> list[list[TaskNode]]:
        nodes = self.node_map
        indegree = {node.node_id: len(node.depends_on) for node in self.nodes}
        remaining = set(nodes)
        layers: list[list[TaskNode]] = []
        while remaining:
            ready = sorted(node_id for node_id in remaining if indegree[node_id] == 0)
            if not ready:
                raise TaskGraphError("task_graph_cycle")
            layer = [nodes[node_id] for node_id in ready]
            layers.append(layer)
            for node_id in ready:
                remaining.remove(node_id)
                for child in self.nodes:
                    if node_id in child.depends_on:
                        indegree[child.node_id] -= 1
        return layers

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": TASK_GRAPH_SCHEMA,
            "graph_id": self.graph_id,
            "max_rounds": self.max_rounds,
            "human_gate_required": self.human_gate_required,
            "input_contract_hashes": {case_id: contract_hash for case_id, contract_hash in self.input_contract_hashes},
            "nodes": [node.as_dict() for node in self.nodes],
            "layers": [[node.node_id for node in layer] for layer in self.topological_layers()],
        }

    @property
    def contract_hash(self) -> str:
        encoded = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def compile_quality_task_graph(
    case_ids: Iterable[str],
    *,
    contract_hashes: Mapping[str, str] | None = None,
) -> TaskGraph:
    """Compile the deterministic close-loop shape from selected case IDs.

    Optional source contract hashes pin a checkpoint to the exact case
    contracts, not merely to their IDs.  A changed case therefore cannot
    silently resume from an old compiled checkpoint.
    """

    normalized = tuple(dict.fromkeys(str(case_id) for case_id in case_ids if str(case_id).strip()))
    normalized_hashes = tuple(
        (case_id, str(contract_hashes[case_id]))
        for case_id in normalized
        if contract_hashes is not None and case_id in contract_hashes
    )
    nodes: list[TaskNode] = [
        TaskNode(
            node_id="context.build",
            kind="worker",
            owner="context-builder",
            produces=("canonical_context",),
            writes=("context.packet",),
            context_keys=("requirements", "source_authority", "policy"),
            context_scope="context-builder",
            contract=TaskContract(required_outputs=("canonical_context",), validator_name="context_complete"),
        ),
        TaskNode(
            node_id="contract.compile",
            kind="worker",
            owner="contract-compiler",
            depends_on=("context.build",),
            consumes=("canonical_context",),
            produces=("compiled_contracts",),
            writes=("task.contracts",),
            context_keys=("requirements", "policy"),
            context_scope="contract-compiler",
            contract=TaskContract(required_inputs=("canonical_context",), required_outputs=("compiled_contracts",), validator_name="contract_schema"),
        ),
    ]
    verified_outputs: list[str] = []
    for case_id in normalized:
        result_key = f"result:{case_id}"
        verified_key = f"verified:{case_id}"
        execute_id = f"execute:{case_id}"
        verify_id = f"verify:{case_id}"
        nodes.append(
            TaskNode(
                node_id=execute_id,
                kind="worker",
                owner=f"case-runner:{case_id}",
                depends_on=("contract.compile",),
                consumes=("compiled_contracts",),
                produces=(result_key,),
                writes=(f"evidence:{case_id}",),
                context_keys=("environment", "policy"),
                context_scope=f"worker:{case_id}",
                contract=TaskContract(required_inputs=("compiled_contracts",), required_outputs=(result_key,), validator_name="case_result"),
            )
        )
        nodes.append(
            TaskNode(
                node_id=verify_id,
                kind="verifier",
                owner=f"verifier:{case_id}",
                depends_on=(execute_id,),
                consumes=(result_key,),
                produces=(verified_key,),
                writes=(f"verification:{case_id}",),
                context_keys=("policy",),
                context_scope=f"verifier:{case_id}",
                verifies=execute_id,
                contract=TaskContract(required_inputs=(result_key,), required_outputs=(verified_key,), validator_name="deterministic_verification"),
            )
        )
        verified_outputs.append(verified_key)
    merge_deps = tuple(f"verify:{case_id}" for case_id in normalized)
    nodes.append(
        TaskNode(
            node_id="merge.results",
            kind="merge",
            owner="merge-owner",
            depends_on=merge_deps or ("contract.compile",),
            consumes=tuple(verified_outputs) or ("compiled_contracts",),
            produces=("merged_report",),
            writes=("report.latest",),
            context_keys=("policy",),
            context_scope="merge-owner",
            contract=TaskContract(required_outputs=("merged_report",), validator_name="report_complete"),
        )
    )
    nodes.append(
        TaskNode(
            node_id="gate.publish",
            kind="human_gate",
            owner="human",
            depends_on=("merge.results",),
            consumes=("merged_report",),
            produces=("approval:publish.publish",),
            writes=("gate.publish",),
            context_keys=("policy",),
            context_scope="human-gate",
            contract=TaskContract(required_inputs=("merged_report",), required_outputs=("approval:publish.publish",), validator_name="explicit_approval"),
        )
    )
    nodes.append(
        TaskNode(
            node_id="publish.publish",
            kind="worker",
            owner="publisher",
            depends_on=("merge.results", "gate.publish"),
            consumes=("merged_report", "approval:publish.publish"),
            produces=("publication",),
            writes=("publication.latest",),
            context_keys=("policy",),
            context_scope="publisher",
            irreversible=True,
            contract=TaskContract(required_inputs=("merged_report", "approval:publish.publish"), required_outputs=("publication",), validator_name="publication_record"),
        )
    )
    graph = TaskGraph(
        graph_id="quality-pilot.close-loop",
        nodes=tuple(nodes),
        max_rounds=3,
        input_contract_hashes=normalized_hashes,
    )
    graph.validate()
    return graph


def compile_graph_engineering_task_graph(
    *,
    input_contract_hashes: Mapping[str, str] | None = None,
) -> TaskGraph:
    """Compile the nine-stage Knowledge Graph workflow as a Task Graph.

    The knowledge graph is the artifact being built; this DAG controls how it is
    built. A source adapter first projects the selected canonical input, entity
    extraction is the fan-out point, relation/event extraction consume the
    recognized entities, and a separate quality verifier checks the
    extraction, fusion is behind a human gate, and serving is read-only.
    """

    nodes = [
        TaskNode(
            node_id="graph.scope",
            kind="worker",
            owner="graph-scoper",
            produces=("graph.scope",),
            writes=("graph.scope.artifact",),
            context_keys=("graph_root", "source_paths", "mode", "questions"),
            context_scope="graph-scope",
            contract=TaskContract(required_outputs=("graph.scope",), validator_name="graph_scope_ready"),
        ),
        TaskNode(
            node_id="graph.representation",
            kind="worker",
            owner="graph-representation",
            depends_on=("graph.scope",),
            consumes=("graph.scope",),
            produces=("graph.representation",),
            writes=("graph.representation.artifact",),
            context_keys=("graph_root", "mode"),
            context_scope="graph-representation",
            contract=TaskContract(required_inputs=("graph.scope",), required_outputs=("graph.representation",), validator_name="graph_representation_ready"),
        ),
        TaskNode(
            node_id="graph.ontology",
            kind="worker",
            owner="graph-ontology",
            depends_on=("graph.representation",),
            consumes=("graph.representation",),
            produces=("graph.ontology",),
            writes=("graph.ontology.artifact",),
            context_keys=("graph_root", "ontology_path"),
            context_scope="graph-ontology",
            contract=TaskContract(required_inputs=("graph.representation",), required_outputs=("graph.ontology",), validator_name="ontology_schema"),
        ),
        TaskNode(
            node_id="graph.source.project",
            kind="worker",
            owner="graph-source-adapter",
            depends_on=("graph.ontology",),
            consumes=("graph.ontology",),
            produces=("graph.source.candidates",),
            writes=("graph.source.artifact",),
            context_keys=("graph_root", "source_paths", "source_mode", "case_ids"),
            context_scope="graph-source-adapter",
            contract=TaskContract(required_inputs=("graph.ontology",), required_outputs=("graph.source.candidates",), validator_name="graph_source_projection"),
        ),
        TaskNode(
            node_id="graph.extract.entities",
            kind="worker",
            owner="graph-entity-extractor",
            depends_on=("graph.ontology", "graph.source.project"),
            consumes=("graph.ontology", "graph.source.candidates"),
            produces=("graph.entities.candidates",),
            writes=("graph.entities.artifact",),
            context_keys=("graph_root", "source_paths", "ontology_path"),
            context_scope="graph-extract-entities",
            contract=TaskContract(required_inputs=("graph.ontology", "graph.source.candidates"), required_outputs=("graph.entities.candidates",), validator_name="entity_candidates"),
        ),
        TaskNode(
            node_id="graph.extract.relations",
            kind="worker",
            owner="graph-relation-extractor",
            depends_on=("graph.ontology", "graph.extract.entities"),
            consumes=("graph.ontology", "graph.entities.candidates"),
            produces=("graph.relations.candidates",),
            writes=("graph.relations.artifact",),
            context_keys=("graph_root", "source_paths", "ontology_path"),
            context_scope="graph-extract-relations",
            contract=TaskContract(required_inputs=("graph.ontology", "graph.entities.candidates"), required_outputs=("graph.relations.candidates",), validator_name="relation_candidates"),
        ),
        TaskNode(
            node_id="graph.extract.events",
            kind="worker",
            owner="graph-event-extractor",
            depends_on=("graph.ontology", "graph.extract.entities"),
            consumes=("graph.ontology", "graph.entities.candidates"),
            produces=("graph.events.candidates",),
            writes=("graph.events.artifact",),
            context_keys=("graph_root", "source_paths", "ontology_path"),
            context_scope="graph-extract-events",
            contract=TaskContract(required_inputs=("graph.ontology", "graph.entities.candidates"), required_outputs=("graph.events.candidates",), validator_name="event_candidates"),
        ),
        TaskNode(
            node_id="graph.quality-gate",
            kind="verifier",
            owner="graph-independent-verifier",
            depends_on=("graph.extract.entities", "graph.extract.relations", "graph.extract.events"),
            consumes=("graph.entities.candidates", "graph.relations.candidates", "graph.events.candidates"),
            produces=("graph.quality-gate",),
            writes=("graph.quality-gate.artifact",),
            context_keys=("graph_root", "gold_path"),
            context_scope="graph-independent-verifier",
            verifies="graph.extract.relations",
            contract=TaskContract(required_inputs=("graph.entities.candidates", "graph.relations.candidates", "graph.events.candidates"), required_outputs=("graph.quality-gate",), validator_name="graph_quality_gate"),
        ),
        TaskNode(
            node_id="graph.fusion.plan",
            kind="worker",
            owner="graph-fusion-planner",
            depends_on=("graph.quality-gate",),
            consumes=("graph.quality-gate",),
            produces=("graph.fusion-plan",),
            writes=("graph.fusion-plan.artifact",),
            context_keys=("graph_root",),
            context_scope="graph-fusion-planner",
            contract=TaskContract(required_inputs=("graph.quality-gate",), required_outputs=("graph.fusion-plan",), validator_name="fusion_plan"),
        ),
        TaskNode(
            node_id="graph.fusion.gate",
            kind="human_gate",
            owner="human",
            depends_on=("graph.fusion.plan",),
            consumes=("graph.fusion-plan",),
            produces=("approval:graph.fusion.apply",),
            writes=("graph.fusion.gate.artifact",),
            context_keys=("graph_root",),
            context_scope="human-graph-gate",
            contract=TaskContract(required_inputs=("graph.fusion-plan",), required_outputs=("approval:graph.fusion.apply",), validator_name="explicit_fusion_approval"),
        ),
        TaskNode(
            node_id="graph.fusion.apply",
            kind="merge",
            owner="graph-merge-owner",
            depends_on=("graph.fusion.plan", "graph.fusion.gate"),
            consumes=("graph.fusion-plan", "approval:graph.fusion.apply"),
            produces=("graph.fused",),
            writes=("graph.fused.artifact",),
            context_keys=("graph_root",),
            context_scope="graph-merge-owner",
            irreversible=True,
            contract=TaskContract(required_inputs=("graph.fusion-plan", "approval:graph.fusion.apply"), required_outputs=("graph.fused",), validator_name="graph_fusion_applied"),
        ),
        TaskNode(
            node_id="graph.evaluate",
            kind="verifier",
            owner="graph-evaluation-verifier",
            depends_on=("graph.fusion.apply",),
            consumes=("graph.fused",),
            produces=("graph.evaluation",),
            writes=("graph.evaluation.artifact",),
            context_keys=("graph_root", "gold_path"),
            context_scope="graph-evaluation-verifier",
            verifies="graph.fusion.apply",
            contract=TaskContract(required_inputs=("graph.fused",), required_outputs=("graph.evaluation",), validator_name="graph_evaluation"),
        ),
        TaskNode(
            node_id="graph.serve",
            kind="worker",
            owner="graph-serving",
            depends_on=("graph.evaluate",),
            consumes=("graph.evaluation",),
            produces=("graph.serving",),
            writes=("graph.serving.artifact",),
            context_keys=("graph_root", "entity"),
            context_scope="graph-serving",
            contract=TaskContract(required_inputs=("graph.evaluation",), required_outputs=("graph.serving",), validator_name="graph_read_only_serve"),
        ),
    ]
    graph = TaskGraph(
        graph_id="quality-pilot.graph-engineering",
        nodes=tuple(nodes),
        max_rounds=3,
        human_gate_required=True,
        input_contract_hashes=tuple(sorted((str(key), str(value)) for key, value in (input_contract_hashes or {}).items())),
    )
    graph.validate()
    return graph


@dataclass
class TaskExecution:
    graph_id: str
    contract_hash: str
    status: str = "PENDING"
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    round: int = 1

    def checkpoint(self) -> dict[str, Any]:
        return {
            "schema": TASK_CHECKPOINT_SCHEMA,
            "graph_id": self.graph_id,
            "contract_hash": self.contract_hash,
            "status": self.status,
            "round": self.round,
            "nodes": copy.deepcopy(self.nodes),
        }


@dataclass(frozen=True)
class TaskCheckpointStore:
    """Persist one redacted, contract-pinned execution checkpoint atomically."""

    path: Path

    def load(self, graph: TaskGraph) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskGraphError("checkpoint_corrupt", details={"path": str(self.path)}) from exc
        if not isinstance(payload, dict):
            raise TaskGraphError("checkpoint_not_mapping", details={"path": str(self.path)})
        try:
            ensure_safe_structure(payload, context="task graph checkpoint")
        except RedactionError as exc:
            raise TaskGraphError("checkpoint_redaction_failed_closed", details={"path": str(self.path)}) from exc
        if payload.get("schema") != TASK_CHECKPOINT_SCHEMA:
            raise TaskGraphError("checkpoint_schema_mismatch", details={"path": str(self.path)})
        if payload.get("graph_id") != graph.graph_id or payload.get("contract_hash") != graph.contract_hash:
            raise TaskGraphError(
                "checkpoint_contract_mismatch",
                details={"path": str(self.path), "graph_id": payload.get("graph_id"), "contract_hash": payload.get("contract_hash")},
            )
        if not isinstance(payload.get("nodes"), dict):
            raise TaskGraphError("checkpoint_nodes_invalid", details={"path": str(self.path)})
        return payload

    def save(self, execution: "TaskExecution") -> dict[str, Any]:
        return self.save_payload(execution.checkpoint())

    def save_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(dict(payload))
        try:
            ensure_safe_structure(payload, context="task graph checkpoint")
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (RedactionError, TypeError, ValueError) as exc:
            raise TaskGraphError("checkpoint_redaction_failed_closed", details={"path": str(self.path)}) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise TaskGraphError("checkpoint_persist_failed", details={"path": str(self.path)}) from exc
        return payload


class TaskGraphExecutor:
    """Run a validated task graph with deterministic node contracts."""

    @staticmethod
    def _mark_descendants_skipped(graph: TaskGraph, execution: TaskExecution, node_id: str) -> None:
        descendants = {node_id}
        changed = True
        while changed:
            changed = False
            for node in graph.nodes:
                if node.node_id not in descendants and any(dep in descendants for dep in node.depends_on):
                    descendants.add(node.node_id)
                    changed = True
        for descendant in descendants - {node_id}:
            if descendant not in execution.nodes:
                execution.nodes[descendant] = {"status": "SKIPPED", "reason": "upstream_not_passed"}

    def execute(
        self,
        graph: TaskGraph,
        context: ContextPacket,
        runner: Runner,
        *,
        approvals: set[str] | None = None,
        checkpoint: Mapping[str, Any] | None = None,
        checkpoint_writer: CheckpointWriter | None = None,
        max_workers: int = 4,
    ) -> TaskExecution:
        """Execute topological layers, parallelising independent nodes.

        The runner is only called after context and input contracts pass.  A
        layer's outputs are committed in stable node order after its workers
        finish, so concurrency cannot make the checkpoint nondeterministic.
        """
        graph.validate()
        if max_workers < 1:
            raise TaskGraphError("task_graph_max_workers_invalid")
        if max_workers > TASK_GRAPH_MAX_WORKERS:
            raise TaskGraphError("task_graph_max_workers_exceeded", details={"maximum": TASK_GRAPH_MAX_WORKERS})
        approvals = approvals or set()
        execution = TaskExecution(graph.graph_id, graph.contract_hash)
        if checkpoint:
            if checkpoint.get("graph_id") != graph.graph_id or checkpoint.get("contract_hash") != graph.contract_hash:
                raise TaskGraphError("checkpoint_contract_mismatch")
            execution.nodes = copy.deepcopy(checkpoint.get("nodes", {})) if isinstance(checkpoint.get("nodes"), dict) else {}
            execution.round = int(checkpoint.get("round") or 1)
            if execution.round < 1 or execution.round > graph.max_rounds:
                raise TaskGraphError("task_graph_stop_rule_exceeded")

        def persist() -> None:
            if checkpoint_writer is not None:
                checkpoint_writer(execution)

        def fail_and_stop(node: TaskNode, record: dict[str, Any]) -> TaskExecution:
            execution.nodes[node.node_id] = record
            self._mark_descendants_skipped(graph, execution, node.node_id)
            execution.status = str(record.get("status") or "FAIL")
            persist()
            return execution

        for layer in graph.topological_layers():
            runnable: list[tuple[TaskNode, dict[str, Any], dict[str, Any], int]] = []
            for node in layer:
                previous = execution.nodes.get(node.node_id, {})
                if previous.get("status") == "PASS":
                    continue
                dependency_states = [execution.nodes.get(dep, {}).get("status") for dep in node.depends_on]
                if any(state != "PASS" for state in dependency_states):
                    execution.nodes[node.node_id] = {"status": "SKIPPED", "reason": "dependency_not_passed"}
                    persist()
                    continue
                if node.kind == "human_gate":
                    approval_token = node.produces[0] if node.produces else node.node_id
                    if approval_token not in approvals and node.node_id not in approvals:
                        execution.nodes[node.node_id] = {"status": "HOLD", "reason": "human_approval_required"}
                        self._mark_descendants_skipped(graph, execution, node.node_id)
                        execution.status = "HOLD"
                        persist()
                        return execution
                try:
                    scoped_context = context.scoped(node.context_keys)
                except TaskGraphError as exc:
                    return fail_and_stop(node, {"status": "BLOCK", "error": exc.error, "details": exc.details})
                inputs: dict[str, Any] = {}
                for dependency in node.depends_on:
                    output = execution.nodes.get(dependency, {}).get("output")
                    if isinstance(output, dict):
                        inputs.update(output)
                missing = node.contract.validate_inputs(inputs)
                if missing:
                    return fail_and_stop(node, {"status": "BLOCK", "reason": "contract_inputs_missing", "missing": missing})
                runnable.append((node, scoped_context, inputs, int(previous.get("attempts") or 0) + 1))

            if not runnable:
                continue

            def invoke(item: tuple[TaskNode, dict[str, Any], dict[str, Any], int]) -> tuple[str, dict[str, Any]]:
                node, scoped_context, inputs, attempts = item
                try:
                    output = runner(node, scoped_context, inputs)
                    if not isinstance(output, dict):
                        return node.node_id, {"status": "FAIL", "attempts": attempts, "reason": "runner_output_not_mapping"}
                    try:
                        ensure_safe_structure(output, context=f"task output {node.node_id}")
                        json.dumps(output, ensure_ascii=False, sort_keys=True)
                    except (RedactionError, TypeError, ValueError):
                        return node.node_id, {"status": "FAIL", "attempts": attempts, "reason": "task_output_redaction_failed_closed"}
                    errors = node.contract.validate_outputs(output)
                    if errors:
                        return node.node_id, {"status": "FAIL", "attempts": attempts, "errors": errors}
                    return node.node_id, {"status": "PASS", "attempts": attempts, "output": copy.deepcopy(output)}
                except TaskGraphHold as exc:
                    return node.node_id, {
                        "status": "HOLD",
                        "attempts": attempts,
                        "reason": exc.error,
                        "details": exc.details,
                    }
                except Exception as exc:  # runner failures are evidence, not executor crashes
                    return node.node_id, {
                        "status": "FAIL",
                        "attempts": attempts,
                        "reason": "runner_exception",
                        "error": type(exc).__name__,
                    }

            if max_workers == 1 or len(runnable) == 1:
                records = [invoke(item) for item in runnable]
            else:
                with ThreadPoolExecutor(max_workers=min(max_workers, len(runnable)), thread_name_prefix="quality-pilot-task") as pool:
                    futures = [pool.submit(invoke, item) for item in runnable]
                    records = [future.result() for future in futures]
            records_by_id = dict(records)
            for node, _context, _inputs, _attempts in runnable:
                execution.nodes[node.node_id] = records_by_id[node.node_id]
                persist()
            failed = next(
                (node for node, _context, _inputs, _attempts in runnable if records_by_id[node.node_id].get("status") in {"FAIL", "BLOCK", "HOLD"}),
                None,
            )
            if failed is not None:
                record = execution.nodes[failed.node_id]
                self._mark_descendants_skipped(graph, execution, failed.node_id)
                execution.status = str(record.get("status") or "FAIL")
                persist()
                return execution
        execution.status = "PASS"
        persist()
        return execution

    @staticmethod
    def invalidate_from(checkpoint: Mapping[str, Any], graph: TaskGraph, node_id: str) -> dict[str, Any]:
        graph.validate()
        if node_id not in graph.node_map:
            raise TaskGraphError("checkpoint_node_missing", details={"node_id": node_id})
        invalidated = copy.deepcopy(dict(checkpoint))
        nodes = invalidated.setdefault("nodes", {})
        descendants = {node_id}
        changed = True
        while changed:
            changed = False
            for node in graph.nodes:
                if node.node_id not in descendants and any(dep in descendants for dep in node.depends_on):
                    descendants.add(node.node_id)
                    changed = True
        for item in descendants:
            nodes.pop(item, None)
        invalidated["status"] = "PENDING"
        invalidated["round"] = int(invalidated.get("round") or 1) + 1
        if invalidated["round"] > graph.max_rounds:
            raise TaskGraphError("task_graph_stop_rule_exceeded")
        return invalidated
