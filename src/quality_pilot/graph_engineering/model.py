from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..security import RedactionError, ensure_safe_structure

GRAPH_SCHEMA = "quality-pilot.knowledge-graph.v1"
ONTOLOGY_SCHEMA = "quality-pilot.graph-ontology.v1"
PROVENANCE_SCHEMA = "quality-pilot.graph-provenance.v1"
GRAPH_STAGE_SCHEMA = "quality-pilot.graph-stage.v1"
_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_VAGUE_RELATIONS = {"RELATED_TO", "HAS_LINK", "LINKED_TO", "ASSOCIATED_WITH"}


class GraphValidationError(ValueError):
    """A graph artifact is incomplete, unsafe, or violates its ontology."""

    def __init__(self, error: str, message: str | None = None, *, details: dict[str, Any] | None = None) -> None:
        self.error = error
        self.details = details or {}
        super().__init__(message or error)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_payload(value: Any, *, context: str) -> Any:
    try:
        ensure_safe_structure(value, context=context)
    except RedactionError as exc:
        raise GraphValidationError("graph_redaction_failed_closed", details={"context": context}) from exc
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise GraphValidationError("graph_payload_not_json", details={"context": context}) from exc


def slug(value: str) -> str:
    text = _ID_RE.sub("-", str(value).strip()).strip("-").lower()
    return text or "item"


def normalize_text(value: str) -> str:
    return " ".join(str(value).casefold().split())


def content_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Provenance:
    source_ref: str
    source_type: str = "unknown"
    evidence: str = ""
    confidence: float | None = None
    extracted_at: str = field(default_factory=utc_now)
    valid_from: str | None = None
    valid_to: str | None = None
    extractor: str = "deterministic"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.source_ref).strip():
            raise GraphValidationError("graph_provenance_source_required")
        if self.confidence is not None and not 0 <= float(self.confidence) <= 1:
            raise GraphValidationError("graph_provenance_confidence_invalid")
        if not str(self.evidence).strip():
            raise GraphValidationError("graph_provenance_evidence_required")
        safe_payload(dict(self.metadata), context="graph provenance metadata")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROVENANCE_SCHEMA,
            "source_ref": self.source_ref,
            "source_type": self.source_type,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "extracted_at": self.extracted_at,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "extractor": self.extractor,
            "metadata": safe_payload(dict(self.metadata), context="graph provenance metadata"),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Provenance":
        if not isinstance(value, Mapping):
            raise GraphValidationError("graph_provenance_not_mapping")
        confidence = None
        if value.get("confidence") is not None:
            try:
                confidence = float(value["confidence"])
            except (TypeError, ValueError) as exc:
                raise GraphValidationError("graph_provenance_confidence_invalid") from exc
        return cls(
            source_ref=str(value.get("source_ref") or value.get("source") or ""),
            source_type=str(value.get("source_type") or "unknown"),
            evidence=str(value.get("evidence") or value.get("evidence_span") or ""),
            confidence=confidence,
            extracted_at=str(value.get("extracted_at") or utc_now()),
            valid_from=str(value.get("valid_from")) if value.get("valid_from") else None,
            valid_to=str(value.get("valid_to")) if value.get("valid_to") else None,
            extractor=str(value.get("extractor") or "deterministic"),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
        )


@dataclass(frozen=True)
class GraphEntity:
    entity_id: str
    entity_type: str
    canonical: str
    aliases: tuple[str, ...] = ()
    attributes: Mapping[str, Any] = field(default_factory=dict)
    provenance: tuple[Provenance, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("entity_id", "entity_type", "canonical"):
            if not str(getattr(self, field_name)).strip():
                raise GraphValidationError(f"graph_entity_{field_name}_required")
        if not self.provenance:
            raise GraphValidationError("graph_entity_provenance_required", details={"entity_id": self.entity_id})
        safe_payload(dict(self.attributes), context=f"entity {self.entity_id} attributes")

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "canonical": self.canonical,
            "aliases": list(dict.fromkeys(str(item) for item in self.aliases if str(item).strip())),
            "attributes": safe_payload(dict(self.attributes), context=f"entity {self.entity_id} attributes"),
            "provenance": [item.as_dict() for item in self.provenance],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphEntity":
        provenance = value.get("provenance")
        if isinstance(provenance, Mapping):
            provenance = [provenance]
        if not isinstance(provenance, list):
            provenance = []
        return cls(
            entity_id=str(value.get("entity_id") or value.get("id") or ""),
            entity_type=str(value.get("entity_type") or value.get("type") or ""),
            canonical=str(value.get("canonical") or value.get("name") or ""),
            aliases=tuple(str(item) for item in value.get("aliases", []) if str(item).strip()) if isinstance(value.get("aliases"), list) else (),
            attributes=value.get("attributes") if isinstance(value.get("attributes"), Mapping) else {},
            provenance=tuple(Provenance.from_dict(item) for item in provenance if isinstance(item, Mapping)),
        )


@dataclass(frozen=True)
class GraphRelation:
    relation_id: str
    relation_type: str
    subject_id: str
    object_id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    provenance: tuple[Provenance, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("relation_id", "relation_type", "subject_id", "object_id"):
            if not str(getattr(self, field_name)).strip():
                raise GraphValidationError(f"graph_relation_{field_name}_required")
        if not self.provenance:
            raise GraphValidationError("graph_relation_provenance_required", details={"relation_id": self.relation_id})
        safe_payload(dict(self.attributes), context=f"relation {self.relation_id} attributes")

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "relation_type": self.relation_type,
            "subject_id": self.subject_id,
            "object_id": self.object_id,
            "attributes": safe_payload(dict(self.attributes), context=f"relation {self.relation_id} attributes"),
            "provenance": [item.as_dict() for item in self.provenance],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphRelation":
        provenance = value.get("provenance")
        if isinstance(provenance, Mapping):
            provenance = [provenance]
        if not isinstance(provenance, list):
            provenance = []
        return cls(
            relation_id=str(value.get("relation_id") or value.get("id") or ""),
            relation_type=str(value.get("relation_type") or value.get("type") or ""),
            subject_id=str(value.get("subject_id") or value.get("subject") or ""),
            object_id=str(value.get("object_id") or value.get("object") or ""),
            attributes=value.get("attributes") if isinstance(value.get("attributes"), Mapping) else {},
            provenance=tuple(Provenance.from_dict(item) for item in provenance if isinstance(item, Mapping)),
        )


@dataclass(frozen=True)
class GraphEvent:
    event_id: str
    event_type: str
    trigger: str
    arguments: Mapping[str, str]
    occurred_at: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    provenance: tuple[Provenance, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("event_id", "event_type", "trigger"):
            if not str(getattr(self, field_name)).strip():
                raise GraphValidationError(f"graph_event_{field_name}_required")
        if not self.provenance:
            raise GraphValidationError("graph_event_provenance_required", details={"event_id": self.event_id})
        safe_payload(dict(self.arguments), context=f"event {self.event_id} arguments")
        safe_payload(dict(self.attributes), context=f"event {self.event_id} attributes")

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "trigger": self.trigger,
            "arguments": {str(key): str(value) for key, value in self.arguments.items()},
            "occurred_at": self.occurred_at,
            "attributes": safe_payload(dict(self.attributes), context=f"event {self.event_id} attributes"),
            "provenance": [item.as_dict() for item in self.provenance],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphEvent":
        provenance = value.get("provenance")
        if isinstance(provenance, Mapping):
            provenance = [provenance]
        if not isinstance(provenance, list):
            provenance = []
        arguments = value.get("arguments") if isinstance(value.get("arguments"), Mapping) else {}
        return cls(
            event_id=str(value.get("event_id") or value.get("id") or ""),
            event_type=str(value.get("event_type") or value.get("type") or ""),
            trigger=str(value.get("trigger") or ""),
            arguments={str(key): str(item) for key, item in arguments.items()},
            occurred_at=str(value.get("occurred_at")) if value.get("occurred_at") else None,
            attributes=value.get("attributes") if isinstance(value.get("attributes"), Mapping) else {},
            provenance=tuple(Provenance.from_dict(item) for item in provenance if isinstance(item, Mapping)),
        )


@dataclass(frozen=True)
class Ontology:
    ontology_id: str
    entity_types: Mapping[str, Mapping[str, Any]]
    relations: tuple[Mapping[str, Any], ...] = ()
    events: tuple[Mapping[str, Any], ...] = ()
    competency_questions: tuple[str, ...] = ()
    version: str = "1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ONTOLOGY_SCHEMA,
            "ontology_id": self.ontology_id,
            "version": self.version,
            "competency_questions": list(self.competency_questions),
            "entities": safe_payload(dict(self.entity_types), context="ontology entities"),
            "relations": safe_payload([dict(item) for item in self.relations], context="ontology relations"),
            "events": safe_payload([dict(item) for item in self.events], context="ontology events"),
        }


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GraphValidationError("graph_ontology_read_failed", details={"path": str(path)}) from exc
    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise GraphValidationError("yaml_dependency_missing") from exc
            loaded = yaml.safe_load(text) or {}
        else:
            loaded = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise GraphValidationError("graph_ontology_parse_failed", details={"path": str(path)}) from exc
    if not isinstance(loaded, dict):
        raise GraphValidationError("graph_ontology_not_mapping", details={"path": str(path)})
    return loaded


def validate_ontology(value: Mapping[str, Any]) -> Ontology:
    if not isinstance(value, Mapping):
        raise GraphValidationError("graph_ontology_not_mapping")
    entity_value = value.get("entities") or value.get("entity_types")
    if isinstance(entity_value, list):
        entities: dict[str, Mapping[str, Any]] = {}
        for item in entity_value:
            if not isinstance(item, Mapping) or not str(item.get("name") or item.get("type") or "").strip():
                raise GraphValidationError("graph_ontology_entity_invalid")
            name = str(item.get("name") or item.get("type"))
            entities[name] = dict(item)
    elif isinstance(entity_value, Mapping):
        entities = {str(key): dict(item) if isinstance(item, Mapping) else {} for key, item in entity_value.items()}
    else:
        raise GraphValidationError("graph_ontology_entities_required")
    if not entities or any(not key.strip() for key in entities):
        raise GraphValidationError("graph_ontology_entities_required")

    raw_relations = value.get("relations", [])
    if isinstance(raw_relations, Mapping):
        relations = [dict(item, name=str(name)) if isinstance(item, Mapping) else {"name": str(name)} for name, item in raw_relations.items()]
    elif isinstance(raw_relations, list):
        relations = [dict(item) for item in raw_relations if isinstance(item, Mapping)]
    else:
        raise GraphValidationError("graph_ontology_relations_invalid")
    for relation in relations:
        name = str(relation.get("name") or relation.get("relation_type") or "").strip()
        domain = str(relation.get("domain") or "").strip()
        range_name = str(relation.get("range") or relation.get("object_type") or "").strip()
        if not name or not domain or not range_name:
            raise GraphValidationError("graph_ontology_relation_domain_range_required", details={"relation": relation})
        if name.upper() in _VAGUE_RELATIONS:
            raise GraphValidationError("graph_ontology_relation_too_vague", details={"relation": name})
        if domain not in entities or range_name not in entities:
            raise GraphValidationError("graph_ontology_relation_type_unknown", details={"relation": name, "domain": domain, "range": range_name})
        relation["name"] = name
        relation["domain"] = domain
        relation["range"] = range_name

    raw_events = value.get("events", [])
    if isinstance(raw_events, Mapping):
        events = [dict(item, name=str(name)) if isinstance(item, Mapping) else {"name": str(name)} for name, item in raw_events.items()]
    elif isinstance(raw_events, list):
        events = [dict(item) for item in raw_events if isinstance(item, Mapping)]
    else:
        raise GraphValidationError("graph_ontology_events_invalid")
    for event in events:
        if not str(event.get("name") or event.get("event_type") or "").strip() or not str(event.get("trigger") or "").strip():
            raise GraphValidationError("graph_ontology_event_trigger_required")
        event["name"] = str(event.get("name") or event.get("event_type"))

    questions = value.get("competency_questions", value.get("questions", []))
    if not isinstance(questions, list):
        raise GraphValidationError("graph_ontology_questions_invalid")
    normalized = {
        "schema": ONTOLOGY_SCHEMA,
        "ontology_id": str(value.get("ontology_id") or value.get("name") or "quality-pilot-graph"),
        "version": str(value.get("version") or "1"),
        "competency_questions": [str(item) for item in questions if str(item).strip()],
        "entities": entities,
        "relations": relations,
        "events": events,
    }
    safe_payload(normalized, context="graph ontology")
    return Ontology(
        ontology_id=normalized["ontology_id"],
        entity_types=entities,
        relations=tuple(relations),
        events=tuple(events),
        competency_questions=tuple(normalized["competency_questions"]),
        version=normalized["version"],
    )


def load_ontology(path: str | Path) -> Ontology:
    return validate_ontology(_load_mapping(Path(path).expanduser().resolve()))


def relation_spec(ontology: Ontology, relation_type: str) -> Mapping[str, Any] | None:
    wanted = str(relation_type)
    return next((item for item in ontology.relations if str(item.get("name")) == wanted), None)


def event_spec(ontology: Ontology, event_type: str) -> Mapping[str, Any] | None:
    wanted = str(event_type)
    return next((item for item in ontology.events if str(item.get("name")) == wanted), None)
