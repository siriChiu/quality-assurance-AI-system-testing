from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import (
    GRAPH_SCHEMA,
    GraphEntity,
    GraphEvent,
    GraphRelation,
    GraphValidationError,
    Ontology,
    safe_payload,
    utc_now,
)
from .paths import GraphPaths


class GraphStore:
    """SQLite canonical graph store with deterministic JSON export.

    SQLite is only a local projection. Source artifacts and provenance remain the
    authority; every write is schema-checked and every exported snapshot is safe
    to copy or inspect without a database service.
    """

    def __init__(self, paths: GraphPaths):
        self.paths = paths

    def _connect(self) -> sqlite3.Connection:
        self.paths.state.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.paths.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        self._initialize(connection)
        try:
            os.chmod(self.paths.database, 0o600)
        except OSError:
            pass
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS graph_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_ontology (
                ontology_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_entities (
                entity_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                canonical TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_relations (
                relation_id TEXT PRIMARY KEY,
                relation_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                object_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_fusion_ledger (
                ledger_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS graph_stage_runs (
                run_id TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS graph_entities_type_idx ON graph_entities(entity_type);
            CREATE INDEX IF NOT EXISTS graph_relations_subject_idx ON graph_relations(subject_id);
            CREATE INDEX IF NOT EXISTS graph_relations_object_idx ON graph_relations(object_id);
            CREATE INDEX IF NOT EXISTS graph_relations_type_idx ON graph_relations(relation_type);
            """
        )
        connection.commit()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)",
                ("schema", GRAPH_SCHEMA),
            )
            connection.execute(
                "INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)",
                ("updated_at", utc_now()),
            )
            connection.commit()

    def save_ontology(self, ontology: Ontology) -> None:
        payload = safe_payload(ontology.as_dict(), context="graph ontology store")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_ontology(ontology_id, version, payload, updated_at) VALUES (?, ?, ?, ?)",
                (ontology.ontology_id, ontology.version, json.dumps(payload, ensure_ascii=False, sort_keys=True), utc_now()),
            )
            connection.execute("INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)", ("updated_at", utc_now()))
            connection.commit()

    def load_ontology(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM graph_ontology ORDER BY updated_at DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_entity(self, entity: GraphEntity) -> None:
        payload = safe_payload(entity.as_dict(), context=f"graph entity {entity.entity_id}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_entities(entity_id, entity_type, canonical, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
                (entity.entity_id, entity.entity_type, entity.canonical, json.dumps(payload, ensure_ascii=False, sort_keys=True), utc_now()),
            )
            connection.execute("INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)", ("updated_at", utc_now()))
            connection.commit()

    def upsert_relation(self, relation: GraphRelation) -> None:
        payload = safe_payload(relation.as_dict(), context=f"graph relation {relation.relation_id}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_relations(relation_id, relation_type, subject_id, object_id, payload, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    relation.relation_id,
                    relation.relation_type,
                    relation.subject_id,
                    relation.object_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            connection.execute("INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)", ("updated_at", utc_now()))
            connection.commit()

    def upsert_event(self, event: GraphEvent) -> None:
        payload = safe_payload(event.as_dict(), context=f"graph event {event.event_id}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_events(event_id, event_type, payload, updated_at) VALUES (?, ?, ?, ?)",
                (event.event_id, event.event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), utc_now()),
            )
            connection.execute("INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)", ("updated_at", utc_now()))
            connection.commit()

    def entities(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM graph_entities ORDER BY entity_id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def relations(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM graph_relations ORDER BY relation_id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM graph_events ORDER BY event_id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def fusion_ledger(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM graph_fusion_ledger ORDER BY created_at, ledger_id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def record_fusion(self, ledger_id: str, payload: Mapping[str, Any]) -> None:
        safe = safe_payload(dict(payload), context="graph fusion ledger")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_fusion_ledger(ledger_id, payload, created_at) VALUES (?, ?, ?)",
                (ledger_id, json.dumps(safe, ensure_ascii=False, sort_keys=True), utc_now()),
            )
            connection.commit()

    def record_stage(self, run_id: str, stage: str, status: str, payload: Mapping[str, Any]) -> None:
        safe = safe_payload(dict(payload), context=f"graph stage {stage}")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO graph_stage_runs(run_id, stage, status, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, stage, status, json.dumps(safe, ensure_ascii=False, sort_keys=True), utc_now()),
            )
            connection.commit()

    def stage_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, stage, status, payload, created_at FROM graph_stage_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {"run_id": row["run_id"], "stage": row["stage"], "status": row["status"], "created_at": row["created_at"], **json.loads(row["payload"])}
            for row in rows
        ]

    def replace_graph(
        self,
        *,
        entities: Iterable[GraphEntity] = (),
        relations: Iterable[GraphRelation] = (),
        events: Iterable[GraphEvent] = (),
    ) -> None:
        entities = list(entities)
        relations = list(relations)
        events = list(events)
        payloads = [item.as_dict() for item in entities] + [item.as_dict() for item in relations] + [item.as_dict() for item in events]
        safe_payload(payloads, context="graph replace")
        with self._connect() as connection:
            for entity in entities:
                payload = json.dumps(entity.as_dict(), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT OR REPLACE INTO graph_entities(entity_id, entity_type, canonical, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (entity.entity_id, entity.entity_type, entity.canonical, payload, utc_now()),
                )
            for relation in relations:
                payload = json.dumps(relation.as_dict(), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT OR REPLACE INTO graph_relations(relation_id, relation_type, subject_id, object_id, payload, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (relation.relation_id, relation.relation_type, relation.subject_id, relation.object_id, payload, utc_now()),
                )
            for event in events:
                payload = json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT OR REPLACE INTO graph_events(event_id, event_type, payload, updated_at) VALUES (?, ?, ?, ?)",
                    (event.event_id, event.event_type, payload, utc_now()),
                )
            connection.execute("INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)", ("updated_at", utc_now()))
            connection.commit()

    def merge_entities(self, canonical: GraphEntity, duplicate_ids: set[str], *, ledger: Mapping[str, Any]) -> None:
        if canonical.entity_id in duplicate_ids:
            duplicate_ids = set(duplicate_ids) - {canonical.entity_id}
        if not duplicate_ids:
            return
        entities = {str(item["entity_id"]): GraphEntity.from_dict(item) for item in self.entities()}
        relations = [dict(item) for item in self.relations()]
        events = [dict(item) for item in self.events()]
        merged_aliases = list(canonical.aliases)
        merged_attributes = dict(canonical.attributes)
        merged_provenance = list(canonical.provenance)
        for duplicate_id in sorted(duplicate_ids):
            duplicate = entities.get(duplicate_id)
            if duplicate is None:
                raise GraphValidationError("graph_fusion_duplicate_missing", details={"entity_id": duplicate_id})
            merged_aliases.extend([duplicate.canonical, *duplicate.aliases])
            for key, value in duplicate.attributes.items():
                merged_attributes.setdefault(key, value)
            merged_provenance.extend(duplicate.provenance)
        merged = GraphEntity(
            entity_id=canonical.entity_id,
            entity_type=canonical.entity_type,
            canonical=canonical.canonical,
            aliases=tuple(dict.fromkeys(merged_aliases)),
            attributes=merged_attributes,
            provenance=tuple(merged_provenance),
        )
        entity_ids = set(entities) - duplicate_ids
        entity_ids.discard(canonical.entity_id)
        kept_entities = [entities[item] for item in sorted(entity_ids)] + [merged]
        kept_relations: list[GraphRelation] = []
        for raw in relations:
            raw["subject_id"] = canonical.entity_id if raw.get("subject_id") in duplicate_ids else raw.get("subject_id")
            raw["object_id"] = canonical.entity_id if raw.get("object_id") in duplicate_ids else raw.get("object_id")
            if raw.get("subject_id") not in {item.entity_id for item in kept_entities} or raw.get("object_id") not in {item.entity_id for item in kept_entities}:
                continue
            kept_relations.append(GraphRelation.from_dict(raw))
        kept_events: list[GraphEvent] = []
        for raw in events:
            args = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
            raw["arguments"] = {key: canonical.entity_id if value in duplicate_ids else value for key, value in args.items()}
            kept_events.append(GraphEvent.from_dict(raw))
        ledger_id = str(ledger.get("ledger_id") or canonical.entity_id)
        safe_ledger = safe_payload(dict(ledger), context="graph fusion ledger")
        with self._connect() as connection:
            connection.execute("DELETE FROM graph_entities")
            connection.execute("DELETE FROM graph_relations")
            connection.execute("DELETE FROM graph_events")
            for entity in kept_entities:
                connection.execute(
                    "INSERT INTO graph_entities(entity_id, entity_type, canonical, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (entity.entity_id, entity.entity_type, entity.canonical, json.dumps(entity.as_dict(), ensure_ascii=False, sort_keys=True), utc_now()),
                )
            for relation in kept_relations:
                connection.execute(
                    "INSERT INTO graph_relations(relation_id, relation_type, subject_id, object_id, payload, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (relation.relation_id, relation.relation_type, relation.subject_id, relation.object_id, json.dumps(relation.as_dict(), ensure_ascii=False, sort_keys=True), utc_now()),
                )
            for event in kept_events:
                connection.execute(
                    "INSERT INTO graph_events(event_id, event_type, payload, updated_at) VALUES (?, ?, ?, ?)",
                    (event.event_id, event.event_type, json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True), utc_now()),
                )
            connection.execute(
                "INSERT OR REPLACE INTO graph_fusion_ledger(ledger_id, payload, created_at) VALUES (?, ?, ?)",
                (ledger_id, json.dumps(safe_ledger, ensure_ascii=False, sort_keys=True), utc_now()),
            )
            connection.execute("INSERT OR REPLACE INTO graph_metadata(key, value) VALUES (?, ?)", ("updated_at", utc_now()))
            connection.commit()

    def snapshot(self) -> dict[str, Any]:
        ontology = self.load_ontology()
        payload = {
            "schema": GRAPH_SCHEMA,
            "generated_at": utc_now(),
            "ontology": ontology,
            "entities": self.entities(),
            "relations": self.relations(),
            "events": self.events(),
            "fusion_ledger": self.fusion_ledger(),
        }
        return safe_payload(payload, context="graph snapshot")

    def export_json(self, path: str | Path | None = None) -> Path:
        target = Path(path).expanduser().resolve() if path else self.paths.json_export
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise GraphValidationError("graph_export_failed", details={"path": str(target)}) from exc
        return target

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            return {
                "entities": int(connection.execute("SELECT COUNT(*) FROM graph_entities").fetchone()[0]),
                "relations": int(connection.execute("SELECT COUNT(*) FROM graph_relations").fetchone()[0]),
                "events": int(connection.execute("SELECT COUNT(*) FROM graph_events").fetchone()[0]),
                "fusion_records": int(connection.execute("SELECT COUNT(*) FROM graph_fusion_ledger").fetchone()[0]),
            }
