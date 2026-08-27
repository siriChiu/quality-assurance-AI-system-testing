from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from ..task_graph import (
    ContextPacket,
    TaskCheckpointStore,
    TaskGraphError,
    TaskGraphHold,
    TaskGraphExecutor,
    compile_graph_engineering_task_graph,
)
from .model import content_hash
from .paths import graph_paths
from .qa_adapter import QAAdapterError, prepare_qa_candidate_snapshot
from .pipeline import (
    graph_evaluate,
    graph_extract,
    graph_fuse,
    graph_ontology,
    graph_quality_gate,
    graph_representation,
    graph_scope,
    graph_serve,
)


def _file_hash(path: str | Path) -> str:
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists() or not candidate.is_file():
        return "missing"
    digest = hashlib.sha256()
    digest.update(candidate.read_bytes())
    return digest.hexdigest()


def run_graph_task_graph(
    config: Any,
    *,
    questions: Iterable[str] = (),
    source_paths: Iterable[str | Path] = (),
    from_qa: bool = False,
    case_ids: Iterable[str] = (),
    run_path: str | Path | None = None,
    review_path: str | Path | None = None,
    ontology_path: str | Path | None = None,
    gold_path: str | Path | None = None,
    entity: str | None = None,
    confirm_fusion: bool = False,
    resume: bool = False,
    repair_node: str | None = None,
    dry_run: bool = False,
    max_workers: int = 4,
) -> dict[str, Any]:
    paths = graph_paths(config)
    normalized_sources = [str(Path(item).expanduser().resolve()) for item in source_paths if str(item).strip()]
    normalized_case_ids = [str(item).strip() for item in case_ids if str(item).strip()]
    normalized_questions = [str(item).strip() for item in questions if str(item).strip()]
    if from_qa and normalized_sources:
        return {
            "status": "BLOCK",
            "error": "graph_input_mode_ambiguous",
            "details": {"from_qa": True, "input_paths": normalized_sources},
        }
    qa_candidate_path: Path | None = None
    qa_candidate_payload: dict[str, Any] | None = None
    if from_qa:
        try:
            if dry_run:
                from .qa_adapter import build_qa_candidate_snapshot

                qa_candidate_payload = build_qa_candidate_snapshot(
                    config,
                    case_ids=normalized_case_ids,
                    run_path=run_path,
                    review_path=review_path,
                    strict_review=bool(review_path),
                )
            else:
                qa_candidate_path, qa_candidate_payload = prepare_qa_candidate_snapshot(
                    config,
                    case_ids=normalized_case_ids,
                    run_path=run_path,
                    review_path=review_path,
                    strict_review=bool(review_path),
                )
                normalized_sources = [str(qa_candidate_path)]
        except QAAdapterError as exc:
            return {"status": "BLOCK", "error": exc.error, "details": exc.details}
    normalized_ontology = str(Path(ontology_path).expanduser().resolve()) if ontology_path else str(paths.ontology)
    normalized_gold = str(Path(gold_path).expanduser().resolve()) if gold_path else ""
    identity = {
        "scope_questions": normalized_questions,
        "source_paths": [(item, _file_hash(item)) for item in normalized_sources],
        "ontology_path": (normalized_ontology, _file_hash(normalized_ontology)),
        "gold_path": (normalized_gold, _file_hash(normalized_gold)) if normalized_gold else "",
        "entity": entity or "",
        "source_mode": "quality_pilot_canonical_artifacts" if from_qa else "candidate_input",
        "case_ids": normalized_case_ids,
        "run_path": str(run_path or ""),
        "review_path": str(review_path or ""),
        "qa_candidate_hash": content_hash(qa_candidate_payload) if qa_candidate_payload else "",
    }
    graph = compile_graph_engineering_task_graph(input_contract_hashes={"graph-input": hashlib.sha256(str(identity).encode()).hexdigest()})
    checkpoint_path = paths.state / "task-graph-latest.json"
    if dry_run:
        return {
            "status": "DRY_RUN",
            "execution_mode": "knowledge_graph_task_graph",
            "graph": graph.as_dict(),
            "checkpoint_path": str(checkpoint_path),
            "human_gate_status": "NOT_RUN",
            "source_paths": normalized_sources,
            "source_mode": "quality_pilot_canonical_artifacts" if from_qa else "candidate_input",
            "case_ids": normalized_case_ids,
            "qa_candidate_path": None,
            "qa_candidate_counts": {
                key: len(qa_candidate_payload.get(key, []))
                for key in ("entities", "relations", "events")
            } if qa_candidate_payload else None,
            "qa_review_validation": qa_candidate_payload.get("review_validation") if qa_candidate_payload else None,
            "side_effects": {
                "sqlite": False,
                "json_export": False,
                "candidate_snapshot": False,
                "checkpoint": False,
            },
            "entity_query": entity,
        }
    checkpoint_store = TaskCheckpointStore(checkpoint_path)
    checkpoint = checkpoint_store.load(graph) if resume else None
    if repair_node:
        if checkpoint is None:
            return {"status": "BLOCK", "error": "graph_task_checkpoint_required", "checkpoint_path": str(checkpoint_path)}
        try:
            checkpoint = TaskGraphExecutor.invalidate_from(checkpoint, graph, repair_node)
        except TaskGraphError as exc:
            return {"status": "BLOCK", "error": exc.error, "details": exc.details}

    context = ContextPacket(
        context_id=f"graph-run-{graph.contract_hash[:12]}",
        facts={
            "graph_root": str(paths.root),
            "source_paths": normalized_sources,
            "source_mode": "quality_pilot_canonical_artifacts" if from_qa else "candidate_input",
            "case_ids": normalized_case_ids,
            "ontology_path": normalized_ontology,
            "gold_path": normalized_gold,
            "mode": "dry-run" if dry_run else "execute",
            "questions": normalized_questions,
            "entity": entity or "",
        },
        source_refs=tuple(normalized_sources),
    )

    def _must(stage: dict[str, Any], node_id: str) -> dict[str, Any]:
        if stage.get("status") == "HOLD":
            raise TaskGraphHold(str(stage.get("reason") or f"{node_id}_hold"), details={"stage": stage})
        if stage.get("status") in {"BLOCK", "FAIL"}:
            raise TaskGraphError(str(stage.get("reason") or f"{node_id}_blocked"), details={"stage": stage})
        return stage

    def runner(node: Any, scoped: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        node_id = node.node_id
        if node_id == "graph.scope":
            return {"graph.scope": _must(graph_scope(config, questions=scoped.get("questions", []), source_refs=scoped.get("source_paths", []), dry_run=dry_run), node_id)}
        if node_id == "graph.representation":
            return {"graph.representation": _must(graph_representation(config, mode="sqlite_json", dry_run=dry_run), node_id)}
        if node_id == "graph.ontology":
            return {"graph.ontology": _must(graph_ontology(config, ontology_path=scoped.get("ontology_path"), dry_run=dry_run), node_id)}
        if node_id == "graph.source.project":
            source_paths = [str(item) for item in scoped.get("source_paths", []) if str(item).strip()]
            if not source_paths:
                raise TaskGraphError("graph_source_projection_required", details={"source_mode": scoped.get("source_mode")})
            return {
                "graph.source.candidates": {
                    "source_mode": scoped.get("source_mode"),
                    "source_paths": source_paths,
                    "case_ids": list(scoped.get("case_ids", [])),
                    "projection": "existing_quality_pilot_artifacts" if scoped.get("source_mode") == "quality_pilot_canonical_artifacts" else "external_candidate_adapter",
                }
            }
        if node_id == "graph.extract.entities":
            return {"graph.entities.candidates": _must(graph_extract(config, input_paths=scoped.get("source_paths", []), kind="entities", ontology_path=scoped.get("ontology_path"), dry_run=dry_run), node_id)}
        if node_id == "graph.extract.relations":
            return {"graph.relations.candidates": _must(graph_extract(config, input_paths=scoped.get("source_paths", []), kind="relations", ontology_path=scoped.get("ontology_path"), dry_run=dry_run), node_id)}
        if node_id == "graph.extract.events":
            return {"graph.events.candidates": _must(graph_extract(config, input_paths=scoped.get("source_paths", []), kind="events", ontology_path=scoped.get("ontology_path"), dry_run=dry_run), node_id)}
        if node_id == "graph.quality-gate":
            return {"graph.quality-gate": _must(graph_quality_gate(config, gold_path=scoped.get("gold_path") or None), node_id)}
        if node_id == "graph.fusion.plan":
            return {"graph.fusion-plan": graph_fuse(config, confirm=False, dry_run=dry_run)}
        if node_id == "graph.fusion.gate":
            return {"approval:graph.fusion.apply": {"approved": True, "scope": "graph.fusion.apply"}}
        if node_id == "graph.fusion.apply":
            return {"graph.fused": _must(graph_fuse(config, confirm=True, dry_run=dry_run), node_id)}
        if node_id == "graph.evaluate":
            return {"graph.evaluation": _must(graph_evaluate(config, gold_path=scoped.get("gold_path") or None), node_id)}
        if node_id == "graph.serve":
            return {"graph.serving": _must(graph_serve(config, entity=scoped.get("entity") or None), node_id)}
        raise TaskGraphError("graph_task_node_unknown", details={"node_id": node_id})

    approvals = {"approval:graph.fusion.apply"} if confirm_fusion else set()
    if dry_run:
        approvals = {"approval:graph.fusion.apply"}
    try:
        execution = TaskGraphExecutor().execute(
            graph,
            context,
            runner,
            approvals=approvals,
            checkpoint=checkpoint,
            checkpoint_writer=checkpoint_store.save,
            max_workers=max_workers,
        )
    except TaskGraphError as exc:
        return {
            "status": "BLOCK",
            "error": exc.error,
            "details": exc.details,
            "graph": graph.as_dict(),
            "checkpoint_path": str(checkpoint_path),
        }
    return {
        "status": execution.status,
        "execution_mode": "knowledge_graph_task_graph",
        "graph": graph.as_dict(),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint": execution.checkpoint(),
        "human_gate_status": execution.nodes.get("graph.fusion.gate", {}).get("status"),
        "source_paths": normalized_sources,
        "source_mode": "quality_pilot_canonical_artifacts" if from_qa else "candidate_input",
        "qa_candidate_path": str(qa_candidate_path) if qa_candidate_path else None,
        "qa_candidate_counts": {
            key: len(qa_candidate_payload.get(key, []))
            for key in ("entities", "relations", "events")
        } if qa_candidate_payload else None,
        "qa_review_validation": qa_candidate_payload.get("review_validation") if qa_candidate_payload else None,
        "case_ids": normalized_case_ids,
        "entity_query": entity,
    }
