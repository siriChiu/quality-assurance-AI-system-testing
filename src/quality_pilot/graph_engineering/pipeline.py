from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import (
    GRAPH_STAGE_SCHEMA,
    GraphEntity,
    GraphEvent,
    GraphRelation,
    GraphValidationError,
    Ontology,
    content_hash,
    event_spec,
    load_ontology,
    normalize_text,
    relation_spec,
    safe_payload,
    slug,
    utc_now,
    validate_ontology,
)
from .paths import GraphPaths, graph_paths
from .store import GraphStore


GRAPH_STAGE_ORDER = (
    "scope",
    "representation",
    "ontology",
    "extract.entities",
    "extract.relations",
    "extract.events",
    "quality-gate",
    "fusion",
    "evaluate",
    "serve",
)


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _write_json(path: Path, payload: Mapping[str, Any], *, root: Path) -> str:
    safe = safe_payload(dict(payload), context=f"graph artifact {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(safe, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise GraphValidationError("graph_artifact_write_failed", details={"path": str(path)}) from exc
    return _relative(path, root)


def _stage(
    paths: GraphPaths,
    store: GraphStore,
    stage: str,
    status: str,
    payload: Mapping[str, Any],
    *,
    artifact: Path | None = None,
) -> dict[str, Any]:
    stage_identity = {"stage": stage, "payload": payload}
    run_id = f"{slug(stage)}-{content_hash(stage_identity)[:12]}"
    safe = {
        "schema": GRAPH_STAGE_SCHEMA,
        "stage": stage,
        "status": status,
        "run_id": run_id,
        "generated_at": utc_now(),
        **dict(payload),
    }
    if artifact is not None:
        safe["artifact_path"] = _write_json(artifact, safe, root=paths.root)
    store.record_stage(run_id, stage, status, safe)
    return safe


def _require_scope(paths: GraphPaths) -> dict[str, Any]:
    if not paths.scope.exists():
        raise GraphValidationError("graph_scope_required", details={"path": str(paths.scope)})
    try:
        value = json.loads(paths.scope.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphValidationError("graph_scope_invalid", details={"path": str(paths.scope)}) from exc
    if not isinstance(value, dict) or not isinstance(value.get("competency_questions"), list) or not value["competency_questions"]:
        raise GraphValidationError("graph_scope_questions_required")
    return value


def _ontology_or_error(paths: GraphPaths, store: GraphStore, ontology_path: str | Path | None = None) -> Ontology:
    if ontology_path:
        source = Path(ontology_path).expanduser().resolve()
        if not source.exists():
            raise GraphValidationError("graph_ontology_read_failed", details={"path": str(source)})
    elif paths.ontology.exists():
        source = paths.ontology
    else:
        source = None
    if source is not None and source.exists():
        ontology = load_ontology(source)
        store.save_ontology(ontology)
        return ontology
    stored = store.load_ontology()
    if isinstance(stored, dict):
        return validate_ontology(stored)
    raise GraphValidationError("graph_ontology_required", details={"path": str(source or paths.ontology)})


def graph_scope(
    config: Any,
    *,
    questions: Iterable[str] = (),
    source_refs: Iterable[str] = (),
    graph_id: str = "quality-pilot-knowledge",
    dry_run: bool = False,
) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    normalized_questions = list(dict.fromkeys(str(item).strip() for item in questions if str(item).strip()))
    if not normalized_questions and paths.scope.exists():
        try:
            existing = json.loads(paths.scope.read_text(encoding="utf-8"))
            normalized_questions = [str(item) for item in existing.get("competency_questions", []) if str(item).strip()]
        except (OSError, json.JSONDecodeError):
            normalized_questions = []
    if not normalized_questions:
        return _stage(
            paths,
            store,
            "scope",
            "BLOCK",
            {
                "reason": "competency_questions_required",
                "next_action": "Provide at least one real multi-hop question with --question",
            },
        )
    refs = list(dict.fromkeys(str(item) for item in source_refs if str(item).strip()))
    payload = {
        "graph_id": graph_id,
        "competency_questions": normalized_questions,
        "source_authority": refs,
        "value_test": {
            "multi_hop_questions": len(normalized_questions),
            "simpler_structure_comparison_required": True,
            "decision": "candidate_requires_review",
        },
        "authority_rule": "source_systems_remain_authoritative",
    }
    if dry_run:
        return _stage(paths, store, "scope", "DRY_RUN", payload)
    return _stage(paths, store, "scope", "READY", payload, artifact=paths.scope)


def graph_representation(config: Any, *, mode: str = "sqlite_json", dry_run: bool = False) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    try:
        scope = _require_scope(paths)
    except GraphValidationError as exc:
        return _stage(paths, store, "representation", "BLOCK", {"reason": exc.error, "details": exc.details})
    if mode not in {"sqlite_json", "json", "sqlite"}:
        return _stage(paths, store, "representation", "BLOCK", {"reason": "representation_mode_invalid", "mode": mode})
    payload = {
        "mode": mode,
        "canonical_store": "sqlite" if mode != "json" else "json",
        "portable_export": "json",
        "scope_hash": content_hash(scope),
        "fact_requirements": ["source_ref", "extracted_at", "confidence", "evidence"],
        "query_boundary": "local_read_only",
        "database_path": _relative(paths.database, paths.root),
        "json_export_path": _relative(paths.json_export, paths.root),
    }
    if dry_run:
        return _stage(paths, store, "representation", "DRY_RUN", payload)
    return _stage(paths, store, "representation", "READY", payload, artifact=paths.representation)


def graph_ontology(config: Any, *, ontology_path: str | Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    try:
        _require_scope(paths)
        ontology = _ontology_or_error(paths, store, ontology_path)
    except GraphValidationError as exc:
        return _stage(paths, store, "ontology", "BLOCK", {"reason": exc.error, "details": exc.details})
    payload = {
        "ontology_id": ontology.ontology_id,
        "version": ontology.version,
        "entity_type_count": len(ontology.entity_types),
        "relation_count": len(ontology.relations),
        "event_count": len(ontology.events),
        "competency_question_count": len(ontology.competency_questions),
        "ontology_hash": content_hash(ontology.as_dict()),
        "source_path": _relative(Path(ontology_path).resolve(), paths.root) if ontology_path else _relative(paths.ontology, paths.root),
    }
    if dry_run:
        return _stage(paths, store, "ontology", "DRY_RUN", payload)
    return _stage(paths, store, "ontology", "READY", payload, artifact=paths.stages / "ontology.json")


def _load_candidate_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            import yaml  # type: ignore
            value = yaml.safe_load(text) or {}
        else:
            value = json.loads(text)
    except (OSError, ValueError, json.JSONDecodeError, ImportError) as exc:
        raise GraphValidationError("graph_extraction_input_parse_failed", details={"path": str(path)}) from exc
    if isinstance(value, list):
        value = {"entities": value}
    if not isinstance(value, dict):
        raise GraphValidationError("graph_extraction_input_not_mapping", details={"path": str(path)})
    return value


def _provenance_source(item: Mapping[str, Any], source_path: Path) -> list[Mapping[str, Any]]:
    raw = item.get("provenance")
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, Mapping)]
    return []


def _validate_candidates(
    ontology: Ontology,
    *,
    entities_raw: list[Any],
    relations_raw: list[Any],
    events_raw: list[Any],
    source_path: Path,
    known_entities: Iterable[GraphEntity] = (),
) -> tuple[list[GraphEntity], list[GraphRelation], list[GraphEvent]]:
    entities: list[GraphEntity] = list(known_entities)
    ids: set[str] = {item.entity_id for item in entities}
    for raw in entities_raw:
        if not isinstance(raw, Mapping):
            raise GraphValidationError("graph_entity_candidate_invalid", details={"source": str(source_path)})
        if not _provenance_source(raw, source_path):
            raise GraphValidationError("graph_entity_provenance_required", details={"entity_id": raw.get("id")})
        entity = GraphEntity.from_dict(raw)
        if entity.entity_type not in ontology.entity_types:
            raise GraphValidationError("graph_entity_type_not_in_ontology", details={"entity_type": entity.entity_type})
        if entity.entity_id in ids:
            raise GraphValidationError("graph_entity_duplicate_id", details={"entity_id": entity.entity_id})
        ids.add(entity.entity_id)
        entities.append(entity)
    existing_ids = {item.entity_id for item in entities}
    relations: list[GraphRelation] = []
    for raw in relations_raw:
        if not isinstance(raw, Mapping):
            raise GraphValidationError("graph_relation_candidate_invalid")
        if not _provenance_source(raw, source_path):
            raise GraphValidationError("graph_relation_provenance_required", details={"relation_id": raw.get("id")})
        relation = GraphRelation.from_dict(raw)
        spec = relation_spec(ontology, relation.relation_type)
        if spec is None:
            raise GraphValidationError("graph_relation_type_not_in_ontology", details={"relation_type": relation.relation_type})
        if relation.subject_id not in existing_ids or relation.object_id not in existing_ids:
            raise GraphValidationError("graph_relation_endpoint_missing", details={"relation_id": relation.relation_id})
        subject = next(item for item in entities if item.entity_id == relation.subject_id)
        object_item = next(item for item in entities if item.entity_id == relation.object_id)
        if subject.entity_type != str(spec.get("domain")) or object_item.entity_type != str(spec.get("range")):
            raise GraphValidationError(
                "graph_relation_domain_range_mismatch",
                details={"relation_id": relation.relation_id, "expected": {"domain": spec.get("domain"), "range": spec.get("range")}},
            )
        relations.append(relation)
    events: list[GraphEvent] = []
    for raw in events_raw:
        if not isinstance(raw, Mapping):
            raise GraphValidationError("graph_event_candidate_invalid")
        if not _provenance_source(raw, source_path):
            raise GraphValidationError("graph_event_provenance_required", details={"event_id": raw.get("id")})
        event = GraphEvent.from_dict(raw)
        if event_spec(ontology, event.event_type) is None:
            raise GraphValidationError("graph_event_type_not_in_ontology", details={"event_type": event.event_type})
        unknown = [value for value in event.arguments.values() if value not in existing_ids]
        if unknown:
            raise GraphValidationError("graph_event_argument_endpoint_missing", details={"event_id": event.event_id, "unknown": unknown})
        events.append(event)
    return entities, relations, events


def graph_extract(
    config: Any,
    *,
    input_paths: Iterable[str | Path] = (),
    kind: str = "all",
    ontology_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    if kind not in {"all", "entities", "relations", "events"}:
        return _stage(paths, store, "extract", "BLOCK", {"reason": "graph_extraction_kind_invalid", "kind": kind})
    try:
        ontology = _ontology_or_error(paths, store, ontology_path)
    except GraphValidationError as exc:
        return _stage(paths, store, "extract", "BLOCK", {"reason": exc.error, "details": exc.details})
    sources = [Path(item).expanduser().resolve() for item in input_paths if str(item).strip()]
    if not sources:
        return _stage(paths, store, "extract", "BLOCK", {"reason": "graph_extraction_input_required", "next_action": "Provide candidate JSON/YAML with provenance"})
    all_entities: list[GraphEntity] = []
    all_relations: list[GraphRelation] = []
    all_events: list[GraphEvent] = []
    try:
        for source in sources:
            payload = _load_candidate_file(source)
            entities_raw = payload.get("entities", []) if kind in {"all", "entities"} else []
            relations_raw = payload.get("relations", []) if kind in {"all", "relations"} else []
            events_raw = payload.get("events", []) if kind in {"all", "events"} else []
            known_entities = [] if kind == "all" else [GraphEntity.from_dict(item) for item in store.entities()]
            known_ids = {item.entity_id for item in known_entities}
            entities, relations, events = _validate_candidates(
                ontology,
                entities_raw=entities_raw if isinstance(entities_raw, list) else [],
                relations_raw=relations_raw if isinstance(relations_raw, list) else [],
                events_raw=events_raw if isinstance(events_raw, list) else [],
                source_path=source,
                known_entities=known_entities,
            )
            all_entities.extend(item for item in entities if item.entity_id not in known_ids)
            all_relations.extend(relations)
            all_events.extend(events)
    except GraphValidationError as exc:
        return _stage(paths, store, "extract", "BLOCK", {"reason": exc.error, "details": exc.details})
    for collection, id_key in ((all_entities, "entity_id"), (all_relations, "relation_id"), (all_events, "event_id")):
        seen_ids: set[str] = set()
        for item in collection:
            item_id = str(getattr(item, id_key))
            if item_id in seen_ids:
                return _stage(paths, store, "extract", "BLOCK", {"reason": "graph_candidate_duplicate_id", "id": item_id})
            seen_ids.add(item_id)
    payload = {
        "kind": kind,
        "source_paths": [_relative(item, paths.root) for item in sources],
        "candidate_counts": {"entities": len(all_entities), "relations": len(all_relations), "events": len(all_events)},
        "ontology_id": ontology.ontology_id,
        "provenance_required": True,
        "llm_boundary": "candidate_only; deterministic validation owns graph writes",
    }
    if dry_run:
        return _stage(paths, store, "extract", "DRY_RUN", payload)
    try:
        for entity in all_entities:
            store.upsert_entity(entity)
        for relation in all_relations:
            store.upsert_relation(relation)
        for event in all_events:
            store.upsert_event(event)
        store.export_json()
    except GraphValidationError as exc:
        return _stage(paths, store, "extract", "BLOCK", {"reason": exc.error, "details": exc.details})
    payload["json_export_path"] = _relative(paths.json_export, paths.root)
    return _stage(paths, store, "extract", "READY", payload, artifact=paths.extraction / f"{slug(kind)}.json")


def _structural_graph_checks(ontology: Ontology, store: GraphStore) -> dict[str, Any]:
    entities = {str(item["entity_id"]): item for item in store.entities()}
    relation_errors: list[dict[str, Any]] = []
    for raw in store.relations():
        spec = relation_spec(ontology, str(raw.get("relation_type")))
        subject = entities.get(str(raw.get("subject_id")))
        object_item = entities.get(str(raw.get("object_id")))
        if spec is None or subject is None or object_item is None:
            relation_errors.append({"relation_id": raw.get("relation_id"), "reason": "schema_or_endpoint_invalid"})
            continue
        if subject.get("entity_type") != spec.get("domain") or object_item.get("entity_type") != spec.get("range"):
            relation_errors.append({"relation_id": raw.get("relation_id"), "reason": "domain_range_mismatch"})
    missing_provenance = []
    for collection, id_key in ((store.entities(), "entity_id"), (store.relations(), "relation_id"), (store.events(), "event_id")):
        for item in collection:
            if not item.get("provenance"):
                missing_provenance.append(item.get(id_key))
    return {
        "entity_count": len(entities),
        "relation_count": len(store.relations()),
        "event_count": len(store.events()),
        "relation_errors": relation_errors,
        "missing_provenance": [item for item in missing_provenance if item],
        "status": "PASS" if not relation_errors and not missing_provenance else "BLOCK",
    }


def _metric_report(predicted: set[tuple[Any, ...]], expected: set[tuple[Any, ...]]) -> dict[str, float | int]:
    true_positive = len(predicted & expected)
    false_positive = len(predicted - expected)
    false_negative = len(expected - predicted)
    precision = true_positive / len(predicted) if predicted else 0.0
    recall = true_positive / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _gold_sets(path: Path) -> tuple[set[tuple[Any, ...]], set[tuple[Any, ...]], set[tuple[Any, ...]]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GraphValidationError("graph_gold_parse_failed", details={"path": str(path)}) from exc
    if not isinstance(value, Mapping):
        raise GraphValidationError("graph_gold_not_mapping")
    entities = set()
    for item in value.get("entities", []) if isinstance(value.get("entities"), list) else []:
        if isinstance(item, Mapping):
            entities.add((str(item.get("entity_id") or item.get("id") or ""), str(item.get("entity_type") or item.get("type") or ""), normalize_text(str(item.get("canonical") or item.get("name") or ""))))
    relations = set()
    for item in value.get("relations", []) if isinstance(value.get("relations"), list) else []:
        if isinstance(item, Mapping):
            relations.add((str(item.get("relation_type") or item.get("type") or ""), str(item.get("subject_id") or item.get("subject") or ""), str(item.get("object_id") or item.get("object") or "")))
    events = set()
    for item in value.get("events", []) if isinstance(value.get("events"), list) else []:
        if isinstance(item, Mapping):
            events.add((str(item.get("event_id") or item.get("id") or ""), str(item.get("event_type") or item.get("type") or ""), normalize_text(str(item.get("trigger") or ""))))
    return entities, relations, events


def graph_quality_gate(config: Any, *, gold_path: str | Path | None = None, threshold: float = 0.9) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    try:
        ontology = _ontology_or_error(paths, store)
    except GraphValidationError as exc:
        return _stage(paths, store, "quality-gate", "BLOCK", {"reason": exc.error, "details": exc.details})
    structural = _structural_graph_checks(ontology, store)
    payload: dict[str, Any] = {"structural": structural, "threshold": threshold, "authority_rule": "metrics do not replace source authority"}
    if gold_path is None:
        payload.update({"status": "HOLD", "reason": "gold_labels_required", "next_action": "Provide --gold with adjudicated entities/relations/events"})
        return _stage(paths, store, "quality-gate", "HOLD", payload, artifact=paths.stages / "quality-gate.json")
    try:
        expected_entities, expected_relations, expected_events = _gold_sets(Path(gold_path).expanduser().resolve())
    except GraphValidationError as exc:
        return _stage(paths, store, "quality-gate", "BLOCK", {"reason": exc.error, "details": exc.details})
    predicted_entities = {(str(item.get("entity_id")), str(item.get("entity_type")), normalize_text(str(item.get("canonical")))) for item in store.entities()}
    predicted_relations = {(str(item.get("relation_type")), str(item.get("subject_id")), str(item.get("object_id"))) for item in store.relations()}
    predicted_events = {(str(item.get("event_id")), str(item.get("event_type")), normalize_text(str(item.get("trigger")))) for item in store.events()}
    metrics = {
        "entities": _metric_report(predicted_entities, expected_entities),
        "relations": _metric_report(predicted_relations, expected_relations),
        "events": _metric_report(predicted_events, expected_events),
    }
    measured = []
    for key, metric in metrics.items():
        expected = {"entities": expected_entities, "relations": expected_relations, "events": expected_events}[key]
        predicted = {"entities": predicted_entities, "relations": predicted_relations, "events": predicted_events}[key]
        if expected or predicted:
            measured.append(float(metric["precision"]))
    payload["metrics"] = metrics
    payload["status"] = "PASS" if structural["status"] == "PASS" and measured and min(measured) >= threshold else "HOLD"
    if payload["status"] != "PASS":
        payload["reason"] = "quality_threshold_not_met"
    _write_json(paths.stages / "quality-gate.json", payload, root=paths.root)
    store.record_stage(f"quality-gate-{content_hash(payload)[:12]}", "quality-gate", str(payload["status"]), payload)
    return payload


def _fusion_candidates(entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocks: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for entity in entities:
        canonical = normalize_text(str(entity.get("canonical") or ""))
        blocks[(str(entity.get("entity_type") or ""), canonical)].append(entity)
        for alias in entity.get("aliases", []) if isinstance(entity.get("aliases"), list) else []:
            if str(alias).strip():
                blocks[(str(entity.get("entity_type") or ""), normalize_text(str(alias)))].append(entity)
    matches: dict[tuple[str, str], set[str]] = defaultdict(set)
    for group in blocks.values():
        ids = {str(item.get("entity_id")) for item in group}
        if len(ids) > 1:
            for item in group:
                matches[(str(item.get("entity_type")), normalize_text(str(item.get("canonical"))))].update(ids)
    plans: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for (entity_type, key), ids in sorted(matches.items()):
        ordered = tuple(sorted(item for item in ids if item))
        if len(ordered) < 2 or (entity_type, ordered) in seen:
            continue
        seen.add((entity_type, ordered))
        plans.append({"entity_type": entity_type, "block": key, "canonical_id": ordered[0], "duplicate_ids": list(ordered[1:]), "confidence": 1.0, "basis": "exact_normalized_name_or_alias"})
    return plans, []


def graph_fuse(config: Any, *, confirm: bool = False, dry_run: bool = False) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    matches, ambiguous = _fusion_candidates(store.entities())
    payload: dict[str, Any] = {
        "matches": matches,
        "ambiguous": ambiguous,
        "candidate_count": len(matches),
        "merge_policy": "canonical lowest stable id; preserve aliases, conflicting attributes, and provenance",
        "human_gate_required": bool(matches),
    }
    if dry_run:
        payload["status"] = "DRY_RUN"
        return _stage(paths, store, "fusion", "DRY_RUN", payload)
    if matches and not confirm:
        payload.update({"status": "HOLD", "reason": "human_fusion_approval_required", "next_action": "Review fusion-plan.json and rerun with --confirm"})
        return _stage(paths, store, "fusion", "HOLD", payload, artifact=paths.fusion_plan)
    if not matches:
        payload["status"] = "READY"
        return _stage(paths, store, "fusion", "READY", payload, artifact=paths.fusion_plan)
    try:
        for plan in matches:
            ledger = {
                "ledger_id": f"fusion-{content_hash(plan)[:16]}",
                "entity_type": plan["entity_type"],
                "canonical_id": plan["canonical_id"],
                "duplicate_ids": plan["duplicate_ids"],
                "confidence": plan["confidence"],
                "basis": plan["basis"],
                "reversible": True,
                "created_at": utc_now(),
            }
            entities = {str(item["entity_id"]): GraphEntity.from_dict(item) for item in store.entities()}
            canonical = entities.get(str(plan["canonical_id"]))
            if canonical is None:
                continue
            store.merge_entities(canonical, set(str(item) for item in plan["duplicate_ids"]), ledger=ledger)
    except GraphValidationError as exc:
        return _stage(paths, store, "fusion", "BLOCK", {"reason": exc.error, "details": exc.details})
    store.export_json()
    payload["status"] = "PASS"
    payload["json_export_path"] = _relative(paths.json_export, paths.root)
    return _stage(paths, store, "fusion", "PASS", payload, artifact=paths.fusion_plan)


def graph_evaluate(config: Any, *, gold_path: str | Path | None = None) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    if gold_path is None:
        payload = {"status": "HOLD", "reason": "gold_evaluation_set_required", "claims_allowed": False}
        return _stage(paths, store, "evaluate", "HOLD", payload, artifact=paths.evaluation)
    try:
        expected_entities, expected_relations, expected_events = _gold_sets(Path(gold_path).expanduser().resolve())
    except GraphValidationError as exc:
        return _stage(paths, store, "evaluate", "BLOCK", {"reason": exc.error, "details": exc.details})
    predicted_entities = {(str(item.get("entity_id")), str(item.get("entity_type")), normalize_text(str(item.get("canonical")))) for item in store.entities()}
    predicted_relations = {(str(item.get("relation_type")), str(item.get("subject_id")), str(item.get("object_id"))) for item in store.relations()}
    predicted_events = {(str(item.get("event_id")), str(item.get("event_type")), normalize_text(str(item.get("trigger")))) for item in store.events()}
    payload = {
        "status": "PASS",
        "gold_path": _relative(Path(gold_path).expanduser().resolve(), paths.root),
        "metrics": {
            "entities": _metric_report(predicted_entities, expected_entities),
            "relations": _metric_report(predicted_relations, expected_relations),
            "events": _metric_report(predicted_events, expected_events),
        },
        "claims_allowed": True,
        "caveat": "sample metrics are not release or merge approval",
    }
    return _stage(paths, store, "evaluate", "PASS", payload, artifact=paths.evaluation)


def _find_entity(store: GraphStore, query: str) -> dict[str, Any] | None:
    wanted = normalize_text(query)
    for entity in store.entities():
        values = [entity.get("entity_id"), entity.get("canonical"), *(entity.get("aliases") or [])]
        if any(normalize_text(str(value)) == wanted for value in values if value is not None):
            return entity
    return None


def graph_serve(config: Any, *, entity: str | None = None, hops: int = 1) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    store.initialize()
    if not entity:
        return _stage(paths, store, "serve", "BLOCK", {"reason": "entity_link_required", "next_action": "Provide --entity <id-or-canonical-name>"})
    if hops < 0 or hops > 3:
        return _stage(paths, store, "serve", "BLOCK", {"reason": "graph_hops_out_of_bounds", "maximum": 3})
    start = _find_entity(store, entity)
    if start is None:
        return _stage(paths, store, "serve", "HOLD", {"reason": "entity_not_found", "entity": entity})
    visited = {str(start["entity_id"])}
    queue: deque[tuple[str, int]] = deque([(str(start["entity_id"]), 0)])
    relations: list[dict[str, Any]] = []
    relation_ids: set[str] = set()
    entities = {str(item["entity_id"]): item for item in store.entities()}
    while queue:
        current, distance = queue.popleft()
        if distance >= hops:
            continue
        for relation in store.relations():
            if relation.get("subject_id") == current or relation.get("object_id") == current:
                relation_id = str(relation.get("relation_id") or "")
                if relation_id in relation_ids:
                    continue
                relation_ids.add(relation_id)
                relations.append(relation)
                other = relation.get("object_id") if relation.get("subject_id") == current else relation.get("subject_id")
                if other in entities and other not in visited:
                    visited.add(str(other))
                    queue.append((str(other), distance + 1))
    node_ids = sorted(visited | {str(item.get("subject_id")) for item in relations} | {str(item.get("object_id")) for item in relations})
    result = {
        "status": "PASS",
        "query_entity": entity,
        "hops": hops,
        "nodes": [entities[item] for item in node_ids if item in entities],
        "relations": relations,
        "serialization": [
            f"({entities.get(str(item.get('subject_id')), {}).get('canonical', item.get('subject_id'))})-[{item.get('relation_type')}]->({entities.get(str(item.get('object_id')), {}).get('canonical', item.get('object_id'))})"
            for item in relations
        ],
        "provenance_required": True,
        "read_only": True,
        "graph_counts_are_not_quality_evidence": True,
    }
    return _stage(paths, store, "serve", "PASS", result)


def graph_status(config: Any) -> dict[str, Any]:
    paths = graph_paths(config)
    store = GraphStore(paths)
    if paths.database.exists():
        store.initialize()
        counts = store.counts()
        stage_runs = store.stage_runs()
        ontology_present = paths.ontology.exists() or store.load_ontology() is not None
    else:
        counts = {"entities": 0, "relations": 0, "events": 0, "fusion_records": 0}
        stage_runs = []
        ontology_present = paths.ontology.exists()
    return {
        "status": "ok",
        "graph_engineering": "knowledge_and_task_graphs",
        "canonical_store": "sqlite",
        "portable_export": "json",
        "path_map": {key: _relative(value, paths.root) for key, value in paths.as_dict().items()},
        "ontology_present": ontology_present,
        "scope_present": paths.scope.exists(),
        "counts": counts,
        "stage_runs": stage_runs,
        "neo4j_required": False,
        "source_authority": "source systems and evidence remain authoritative",
    }


def graph_tutor() -> dict[str, Any]:
    modules = [
        {"stage": 1, "name": "scope", "exercise": "Write three competency questions and explain why a table is insufficient."},
        {"stage": 2, "name": "representation", "exercise": "Choose SQLite/JSON and list provenance fields."},
        {"stage": 3, "name": "ontology", "exercise": "Define five entity types and typed relations with domain/range."},
        {"stage": 4, "name": "entities", "exercise": "Provide a ten-document or structured-source extraction sample."},
        {"stage": 5, "name": "relations", "exercise": "Provide relation evidence spans and reject co-occurrence."},
        {"stage": 6, "name": "events", "exercise": "Model one event trigger, arguments, and time anchor."},
        {"stage": 7, "name": "quality-gate", "exercise": "Adjudicate a sample and measure precision/recall."},
        {"stage": 8, "name": "fusion", "exercise": "Define blocking, matching, review band, and reversible merge policy."},
        {"stage": 9, "name": "serve", "exercise": "Write three graph questions and a vector-only baseline."},
    ]
    return {
        "status": "ok",
        "mode": "teaching",
        "rule": "one stage per exchange; use the user's QA domain as the running example",
        "modules": modules,
        "next": modules[0],
        "references": ["graph-engineering/SKILL.md", "graph-engineering/WORKFLOWS.md", "graph-engineering/references/"],
    }
