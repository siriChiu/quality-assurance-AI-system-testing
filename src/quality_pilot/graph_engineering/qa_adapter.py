"""Adapters from canonical Quality Pilot artifacts into graph candidates.

This module is deliberately a projection, not a second source of truth.  It
reads the existing case contracts, latest-run payload, evidence references, and
optional PR review report, then emits provenance-backed candidates for the
Knowledge Graph stages.  The graph store never becomes authoritative for QA
truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..config import ProjectConfig
from ..contracts import ContractError, CaseContract, load_contracts
from .model import safe_payload
from .review_adapter import ReviewAdapterError, load_review_artifact

QA_CANDIDATE_SCHEMA = "quality-pilot.qa-graph-candidates.v1"


class QAAdapterError(ValueError):
    """Canonical QA artifacts cannot be safely projected into graph candidates."""

    def __init__(self, error: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.error = error
        self.details = dict(details or {})
        super().__init__(error)


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _safe_path(value: str | Path, root: Path, *, required: bool = True) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if required and not path.exists():
        raise QAAdapterError("qa_adapter_source_missing", details={"path": str(path)})
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise QAAdapterError("qa_adapter_source_outside_project", details={"path": str(path)}) from exc
    return path


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "|".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _provenance(source_ref: str, evidence: str, *, source_type: str = "quality_pilot") -> dict[str, Any]:
    return {
        "source_ref": source_ref,
        "source_type": source_type,
        "evidence": evidence,
        "confidence": 1.0,
    }


def _entity(
    entity_id: str,
    entity_type: str,
    canonical: str,
    *,
    attributes: Mapping[str, Any],
    source_ref: str,
    evidence: str,
    aliases: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "canonical": canonical,
        "aliases": list(dict.fromkeys(str(item) for item in aliases if str(item).strip())),
        "attributes": dict(attributes),
        "provenance": [_provenance(source_ref, evidence)],
    }


def _relation(
    relation_id: str,
    relation_type: str,
    subject_id: str,
    object_id: str,
    *,
    source_ref: str,
    evidence: str,
) -> dict[str, Any]:
    return {
        "relation_id": relation_id,
        "relation_type": relation_type,
        "subject_id": subject_id,
        "object_id": object_id,
        "provenance": [_provenance(source_ref, evidence)],
    }


def _event(
    event_id: str,
    event_type: str,
    trigger: str,
    arguments: Mapping[str, str],
    *,
    source_ref: str,
    evidence: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "trigger": trigger,
        "arguments": dict(arguments),
        "provenance": [_provenance(source_ref, evidence)],
    }


def _load_cases(config: ProjectConfig, case_ids: Iterable[str], *, strict: bool = True) -> list[CaseContract]:
    try:
        contracts = load_contracts(config.paths.cases)
    except ContractError as exc:
        raise QAAdapterError("qa_case_contract_invalid", details={"path": exc.path, "error": exc.error}) from exc
    requested = {str(item).strip() for item in case_ids if str(item).strip()}
    if not requested:
        return contracts
    selected = [contract for contract in contracts if contract.case_id in requested]
    missing = sorted(requested - {contract.case_id for contract in selected})
    if missing and strict:
        raise QAAdapterError("qa_case_not_found", details={"case_ids": missing})
    return selected


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QAAdapterError("qa_adapter_json_invalid", details={"path": str(path)}) from exc
    if not isinstance(value, dict):
        raise QAAdapterError("qa_adapter_json_not_mapping", details={"path": str(path)})
    return value


def _case_entity(contract: CaseContract, root: Path) -> dict[str, Any]:
    source_ref = _relative(contract.path, root)
    raw = contract.raw
    return _entity(
        contract.case_id,
        "TestCase",
        contract.case_id,
        aliases=(str(raw.get("title") or ""),),
        attributes={
            "case_id": contract.case_id,
            "title": contract.title,
            "feature": str(raw.get("feature") or ""),
            "contract_hash": contract.contract_hash,
            "contract_path": source_ref,
            "dimensions": [str(item) for item in raw.get("swqa_dimensions", []) if item],
        },
        source_ref=source_ref,
        evidence=f"case contract {contract.case_id} declares {len(contract.commands)} executable command(s)",
    )


def _result_run(
    result: Mapping[str, Any],
    *,
    case_id: str,
    source_ref: str,
    run_namespace: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    raw_run_id = str(result.get("run_id") or "").strip()
    run_id = raw_run_id or _stable_id("run", run_namespace, case_id, result.get("result_path") or result.get("started_at") or "unknown")
    entity_id = _stable_id("run", run_id, case_id)
    status = str(result.get("status") or "NOT_RUN")
    truth_status = str(result.get("truth_status") or status)
    run_entity = _entity(
        entity_id,
        "TestRun",
        f"{run_id}:{case_id}",
        attributes={
            "run_id": run_id,
            "case_id": case_id,
            "outcome": status,
            "truth_status": truth_status,
            "contract_hash": str(result.get("contract_hash") or ""),
            "started_at": str(result.get("started_at") or ""),
            "ended_at": str(result.get("ended_at") or ""),
            "result_path": str(result.get("result_path") or ""),
        },
        source_ref=source_ref,
        evidence=f"canonical Quality Pilot run recorded {case_id} as {status}",
    )
    relations = [
        _relation(
            _stable_id("relation", "PRODUCED_RUN", case_id, entity_id),
            "PRODUCED_RUN",
            case_id,
            entity_id,
            source_ref=source_ref,
            evidence=f"run record links case {case_id} to {run_id}",
        )
    ]
    evidence_entities: list[dict[str, Any]] = []
    evidence_relations: list[dict[str, Any]] = []
    raw_evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    for evidence_path in raw_evidence:
        evidence_ref = str(evidence_path).strip()
        if not evidence_ref:
            continue
        evidence_id = _stable_id("evidence", evidence_ref)
        evidence_entities.append(
            _entity(
                evidence_id,
                "Evidence",
                evidence_ref,
                attributes={
                    "path": evidence_ref,
                    "artifact_type": Path(evidence_ref).suffix.lstrip(".") or "artifact",
                    "case_id": case_id,
                    "run_id": run_id,
                },
                source_ref=evidence_ref,
                evidence=f"canonical run {run_id} lists {evidence_ref} as produced evidence",
            )
        )
        evidence_relations.append(
            _relation(
                _stable_id("relation", "PRODUCED_EVIDENCE", entity_id, evidence_id),
                "PRODUCED_EVIDENCE",
                entity_id,
                evidence_id,
                source_ref=source_ref,
                evidence=f"run record lists evidence path {evidence_ref}",
            )
        )
    event = _event(
        _stable_id("event", "TestExecution", entity_id),
        "TestExecution",
        "executed",
        {"case": case_id, "run": entity_id},
        source_ref=source_ref,
        evidence=f"canonical run record contains execution result for {case_id}",
    )
    return run_entity, relations + evidence_relations, evidence_entities, entity_id


def _validated_canonical_result(
    root: Path,
    result_ref: str,
    *,
    expected_case_id: str,
    expected_contract_hash: str,
    expected_run_id: str,
) -> dict[str, Any]:
    path = _safe_path(result_ref, root, required=True)
    if path.name != "result.json":
        raise QAAdapterError("qa_adapter_result_not_canonical", details={"path": str(path)})
    payload = _load_json(path)
    if str(payload.get("case_id") or "") != expected_case_id:
        raise QAAdapterError("qa_adapter_result_case_mismatch", details={"path": str(path), "expected": expected_case_id, "actual": payload.get("case_id")})
    if expected_contract_hash and str(payload.get("contract_hash") or "") != expected_contract_hash:
        raise QAAdapterError("qa_adapter_result_contract_mismatch", details={"path": str(path), "expected": expected_contract_hash, "actual": payload.get("contract_hash")})
    if expected_run_id and str(payload.get("run_id") or "") != expected_run_id:
        raise QAAdapterError("qa_adapter_result_run_mismatch", details={"path": str(path), "expected": expected_run_id, "actual": payload.get("run_id")})
    raw_evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    for evidence_ref in raw_evidence:
        evidence_path = _safe_path(str(evidence_ref), root, required=True)
        if not evidence_path.exists():
            raise QAAdapterError("qa_adapter_evidence_missing", details={"path": str(evidence_path)})
    return payload


def _review_projection(
    report: Mapping[str, Any],
    *,
    report_path: Path,
    root: Path,
    case_ids: set[str],
    known_case_ids: set[str],
    validation: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    source_ref = _relative(report_path, root)
    repo = str(report.get("repo") or "")
    number = str(report.get("pr_number") or "")
    head_sha = str(report.get("head_sha") or "")
    if not number:
        return [], [], [], []
    pr_id = _stable_id("pr", repo, number, head_sha)
    pr_entity = _entity(
        pr_id,
        "PullRequest",
        f"{repo}#{number}" if repo else f"PR #{number}",
        attributes={
            "repo": repo,
            "number": number,
            "base_ref": str(report.get("base_ref") or ""),
            "base_sha": str(report.get("base_sha") or ""),
            "head_ref": str(report.get("head_ref") or ""),
            "head_sha": head_sha,
            "diff_hash": str(report.get("diff_hash") or ""),
            "changed_file_count": len(report.get("changed_files", [])) if isinstance(report.get("changed_files"), list) else 0,
            "qa_outcome": str(report.get("qa_outcome") or ""),
            "conclusion": str(report.get("conclusion") or ""),
            "review_report_hash": str((validation or {}).get("report_hash") or report.get("report_hash") or ""),
            "review_validation_status": str((validation or {}).get("status") or "UNKNOWN"),
            "review_head_status": str((validation or {}).get("current_head_status") or "UNVERIFIED"),
        },
        source_ref=source_ref,
        evidence=f"review report identifies PR #{number} at pinned head {head_sha or 'unknown'}",
    )
    entities = [pr_entity]
    relations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    qa_review = report.get("qa_review") if isinstance(report.get("qa_review"), Mapping) else {}
    cases = qa_review.get("cases") if isinstance(qa_review.get("cases"), list) else []
    # Review report case entries are only accepted when their canonical result
    # is linked.  A matrix-only item cannot create a graph TestRun.
    for item in cases:
        if not isinstance(item, Mapping):
            continue
        case_id = str(item.get("case_id") or "").strip()
        if not case_id or case_ids and case_id not in case_ids:
            continue
        if case_id not in known_case_ids:
            entities.append(
                _entity(
                    case_id,
                    "TestCase",
                    case_id,
                    attributes={
                        "case_id": case_id,
                        "title": str(item.get("title") or case_id),
                        "dimensions": [str(value) for value in item.get("dimensions", []) if value] if isinstance(item.get("dimensions"), list) else [],
                        "case_type": str(item.get("case_type") or "generated_case"),
                        "oracle": dict(item.get("oracle") or {}) if isinstance(item.get("oracle"), Mapping) else {},
                        "contract_hash": str(item.get("contract_hash") or ""),
                        "run_id": str(item.get("run_id") or ""),
                        "source": "review_report",
                    },
                    source_ref=source_ref,
                    evidence=f"review report identifies generated case {case_id}",
                )
            )
        relations.append(
            _relation(
                _stable_id("relation", "HAS_CASE", pr_id, case_id),
                "HAS_CASE",
                pr_id,
                case_id,
                source_ref=source_ref,
                evidence=f"review report associates PR #{number} with generated case {case_id}",
            )
        )
        result_ref = str(item.get("result_path") or "").strip()
        if not result_ref:
            continue
        canonical_result = _validated_canonical_result(root, result_ref, expected_case_id=case_id, expected_contract_hash=str(item.get("contract_hash") or ""), expected_run_id=str(item.get("run_id") or ""))
        review_run = {
            "run_id": f"review:{head_sha or hashlib.sha256(source_ref.encode('utf-8')).hexdigest()[:16]}",
            "status": str(canonical_result.get("status") or item.get("status") or "NOT_RUN"),
            "truth_status": str(canonical_result.get("truth_status") or item.get("truth_status") or item.get("status") or "NOT_RUN"),
            "contract_hash": str(canonical_result.get("contract_hash") or item.get("contract_hash") or ""),
            "case_type": str(item.get("case_type") or "generated_case"),
            "oracle": dict(item.get("oracle") or {}) if isinstance(item.get("oracle"), Mapping) else {},
            "run_id": str(canonical_result.get("run_id") or item.get("run_id") or ""),
            "result_path": result_ref,
            "evidence": list(canonical_result.get("evidence", [])) if isinstance(canonical_result.get("evidence"), list) else [],
        }
        run_entity, run_relations, evidence_entities, _ = _result_run(
            review_run,
            case_id=case_id,
            source_ref=source_ref,
            run_namespace=f"review:{head_sha}:{case_id}",
        )
        entities.extend([run_entity, *evidence_entities])
        relations.extend(run_relations)
        events.append(
            _event(
                _stable_id("event", "PRReview", pr_id, case_id),
                "TestExecution",
                "reviewed",
                {"case": case_id, "run": str(run_entity["entity_id"])},
                source_ref=source_ref,
                evidence=f"review report records QA case {case_id} for PR #{number}",
            )
        )
    return entities, relations, events, []


def _dedupe_candidates(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Collapse repeated projections while preserving distinct provenance."""

    result: dict[str, dict[str, Any]] = {}
    for item in items:
        item_key = str(item.get(key) or "")
        if not item_key:
            raise QAAdapterError("qa_adapter_candidate_id_missing", details={"key": key})
        existing = result.get(item_key)
        if existing is None:
            result[item_key] = dict(item)
            continue
        for field in ("entity_type", "canonical", "relation_type", "subject_id", "object_id", "event_type", "trigger", "arguments"):
            if field in existing or field in item:
                if existing.get(field) != item.get(field):
                    raise QAAdapterError("qa_adapter_candidate_conflict", details={"key": key, "id": item_key, "field": field})
        provenance = list(existing.get("provenance", [])) if isinstance(existing.get("provenance"), list) else []
        incoming = item.get("provenance") if isinstance(item.get("provenance"), list) else []
        existing["provenance"] = provenance + [value for value in incoming if value not in provenance]
        if isinstance(existing.get("attributes"), dict) and isinstance(item.get("attributes"), dict):
            for attribute, value in item["attributes"].items():
                existing["attributes"].setdefault(attribute, value)
    return list(result.values())


def build_qa_candidate_snapshot(
    config: ProjectConfig,
    *,
    case_ids: Iterable[str] = (),
    run_path: str | Path | None = None,
    review_path: str | Path | None = None,
    strict_review: bool = False,
) -> dict[str, Any]:
    """Project existing Quality Pilot contracts/runs into graph candidates."""

    requested_case_ids = {str(item).strip() for item in case_ids if str(item).strip()}
    contracts = _load_cases(config, requested_case_ids, strict=not bool(review_path))
    entities = [_case_entity(contract, config.root) for contract in contracts]
    relations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    source_authority = [_relative(contract.path, config.root) for contract in contracts]
    result_count = 0

    selected_run_path = _safe_path(run_path, config.root) if run_path else config.paths.state / "latest-run.json"
    if selected_run_path.exists():
        run_payload = _load_json(selected_run_path)
        source_ref = _relative(selected_run_path, config.root)
        source_authority.append(source_ref)
        results = run_payload.get("results") if isinstance(run_payload.get("results"), list) else []
        contract_by_id = {contract.case_id: contract for contract in contracts}
        for result in results:
            if not isinstance(result, Mapping):
                continue
            case_id = str(result.get("case_id") or "").strip()
            if not case_id or requested_case_ids and case_id not in requested_case_ids:
                continue
            if case_id not in contract_by_id:
                # Keep the canonical run visible, but do not fabricate a case contract.
                continue
            run_entity, run_relations, evidence_entities, _ = _result_run(
                result,
                case_id=case_id,
                source_ref=source_ref,
                run_namespace=f"latest:{run_payload.get('run_id') or source_ref}",
            )
            entities.extend([run_entity, *evidence_entities])
            relations.extend(run_relations)
            events.append(
                _event(
                    _stable_id("event", "TestExecution", str(run_entity["entity_id"])),
                    "TestExecution",
                    "executed",
                    {"case": case_id, "run": str(run_entity["entity_id"])},
                    source_ref=source_ref,
                    evidence=f"latest-run.json records execution for case {case_id}",
                )
            )
            result_count += 1

    review_validation: dict[str, Any] | None = None
    if review_path:
        selected_review_path = _safe_path(review_path, config.root)
        try:
            report, review_validation = load_review_artifact(
                config,
                selected_review_path,
                strict=strict_review,
                require_current_snapshot=strict_review,
            )
        except ReviewAdapterError as exc:
            raise QAAdapterError(exc.error, details=exc.details) from exc
        review_entities, review_relations, review_events, _ = _review_projection(
            report,
            report_path=selected_review_path,
            root=config.root,
            case_ids=requested_case_ids,
            known_case_ids={contract.case_id for contract in contracts},
            validation=review_validation,
        )
        source_authority.append(_relative(selected_review_path, config.root))
        entities.extend(review_entities)
        relations.extend(review_relations)
        events.extend(review_events)

    if not entities:
        raise QAAdapterError("qa_adapter_no_canonical_artifacts", details={"cases_path": _relative(config.paths.cases, config.root)})

    entities = _dedupe_candidates(entities, "entity_id")
    relations = _dedupe_candidates(relations, "relation_id")
    events = _dedupe_candidates(events, "event_id")
    payload = {
        "schema": QA_CANDIDATE_SCHEMA,
        "source_mode": "quality_pilot_canonical_artifacts",
        "source_authority": sorted(set(source_authority)),
        "case_ids": sorted(requested_case_ids),
        "run_result_count": result_count,
        "review_validation": review_validation,
        "entities": entities,
        "relations": relations,
        "events": events,
        "authority_rule": "source contracts, run records, evidence, and review reports remain authoritative",
        "graph_role": "provenance_backed_read_model",
    }
    try:
        return safe_payload(payload, context="quality pilot graph adapter")
    except Exception as exc:
        raise QAAdapterError("qa_adapter_redaction_failed_closed") from exc


def write_qa_candidate_snapshot(config: ProjectConfig, payload: Mapping[str, Any]) -> Path:
    """Persist a redacted adapter snapshot atomically with owner-only permissions."""

    safe = safe_payload(dict(payload), context="quality pilot graph adapter snapshot")
    digest = hashlib.sha256(json.dumps(safe, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    target = config.paths.state / "graph" / "inputs" / f"qa-candidates-{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(safe, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise QAAdapterError("qa_adapter_snapshot_write_failed", details={"path": str(target)}) from exc
    return target


def prepare_qa_candidate_snapshot(
    config: ProjectConfig,
    *,
    case_ids: Iterable[str] = (),
    run_path: str | Path | None = None,
    review_path: str | Path | None = None,
    strict_review: bool = False,
) -> tuple[Path, dict[str, Any]]:
    payload = build_qa_candidate_snapshot(
        config,
        case_ids=case_ids,
        run_path=run_path,
        review_path=review_path,
        strict_review=strict_review,
    )
    return write_qa_candidate_snapshot(config, payload), payload
