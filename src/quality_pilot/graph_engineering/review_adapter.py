"""Fail-closed validation for pinned PR review reports.

The review module remains authoritative. This adapter only validates the
redacted report identity, current PR snapshot state/freshness, and evidence
paths before projecting a safe, read-only view for the graph layer. It never
decides whether a PR is approvable or mergeable.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from ..config import ProjectConfig, json_dumps
from ..review import REVIEW_SCHEMA, pr_snapshot_path
from ..security import find_sensitive_paths

REVIEW_PROJECTION_SCHEMA = "quality-pilot.review-projection.v1"
_REQUIRED_FIELDS = ("schema", "repo", "pr_number", "base_sha", "head_sha", "changed_files", "qa_review")


class ReviewAdapterError(ValueError):
    """A review artifact is missing, stale, unsafe, or internally inconsistent."""

    def __init__(self, error: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.error = error
        self.details = dict(details or {})
        super().__init__(error)


def _resolve_project_path(config: ProjectConfig, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config.root / path
    path = path.resolve()
    try:
        path.relative_to(config.root.resolve())
    except ValueError as exc:
        raise ReviewAdapterError("review_artifact_outside_project", details={"path": str(path)}) from exc
    return path


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewAdapterError("review_artifact_read_failed", details={"path": str(path)}) from exc
    if not isinstance(value, dict):
        raise ReviewAdapterError("review_artifact_not_mapping", details={"path": str(path)})
    return value


def _report_hash(report: Mapping[str, Any]) -> str:
    # review_pr computes report_hash before it appends report_hash and the
    # remote_reply reconciliation payload.  redaction_findings are also added
    # after the hash calculation when present.
    canonical = copy.deepcopy(dict(report))
    canonical.pop("report_hash", None)
    canonical.pop("remote_reply", None)
    canonical.pop("redaction_findings", None)
    return hashlib.sha256(json_dumps(canonical).encode("utf-8")).hexdigest()


def _artifact_paths(report: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    test_results = report.get("test_results") if isinstance(report.get("test_results"), list) else []
    for item in test_results:
        if not isinstance(item, Mapping):
            continue
        for key in ("stdout", "stderr", "result_path"):
            value = item.get(key)
            if value:
                paths.append(str(value))
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        paths.extend(str(value) for value in evidence if value)
    qa_review = report.get("qa_review") if isinstance(report.get("qa_review"), Mapping) else {}
    cases = qa_review.get("cases") if isinstance(qa_review.get("cases"), list) else []
    for item in cases:
        if not isinstance(item, Mapping):
            continue
        for key in ("result_path",):
            if item.get(key):
                paths.append(str(item[key]))
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        paths.extend(str(value) for value in evidence if value)
    return list(dict.fromkeys(paths))


def _check_evidence_paths(config: ProjectConfig, report: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for value in _artifact_paths(report):
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = config.root / path
        path = path.resolve()
        try:
            path.relative_to(config.root.resolve())
        except ValueError:
            missing.append(value)
            continue
        if not path.exists() or not path.is_file():
            missing.append(value)
    return missing


def load_review_artifact(
    config: ProjectConfig,
    path: str | Path,
    *,
    strict: bool = False,
    expected_head_sha: str | None = None,
    require_current_snapshot: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a review report and return it with a validation summary.

    ``strict=True`` is used for PR-scoped graph projections.  General QA graph
    projections may keep a report optional, but a supplied report is still
    represented with its validation status.
    """

    artifact_path = _resolve_project_path(config, path)
    if not artifact_path.exists():
        raise ReviewAdapterError("review_artifact_missing", details={"path": str(artifact_path)})
    report = _read_mapping(artifact_path)
    sensitive = find_sensitive_paths(report)
    if sensitive:
        finding = sensitive[0]
        raise ReviewAdapterError("review_artifact_secret_detected", details={"path": finding.path, "kind": finding.kind})

    missing = [field for field in _REQUIRED_FIELDS if field not in report or report.get(field) in (None, "")]
    if missing:
        raise ReviewAdapterError("review_artifact_required_field_missing", details={"path": str(artifact_path), "fields": missing})
    if report.get("schema") != REVIEW_SCHEMA:
        raise ReviewAdapterError("review_artifact_schema_invalid", details={"expected": REVIEW_SCHEMA, "actual": report.get("schema")})
    if not isinstance(report.get("changed_files"), list) or not isinstance(report.get("qa_review"), Mapping):
        raise ReviewAdapterError("review_artifact_shape_invalid", details={"path": str(artifact_path)})

    head_sha = str(report.get("head_sha") or "")
    repo = str(report.get("repo") or "")
    pr_number = report.get("pr_number")
    reported_hash = str(report.get("report_hash") or "")
    computed_hash = _report_hash(report)
    hash_status = "MATCH" if reported_hash and reported_hash == computed_hash else ("MISSING" if not reported_hash else "MISMATCH")
    validation: dict[str, Any] = {
        "schema": REVIEW_PROJECTION_SCHEMA,
        "status": "PASS",
        "artifact_path": str(artifact_path.relative_to(config.root.resolve())),
        "report_hash": reported_hash or None,
        "computed_report_hash": computed_hash,
        "hash_status": hash_status,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "current_head_status": "UNVERIFIED",
        "current_identity_status": "UNVERIFIED",
        "current_state": "UNVERIFIED",
        "current_updated_at_status": "UNPINNED",
        "evidence_missing": [],
        "strict": strict,
    }
    if strict and hash_status != "MATCH":
        raise ReviewAdapterError("review_artifact_hash_invalid", details=validation)

    if expected_head_sha:
        expected = str(expected_head_sha).strip()
        if expected != head_sha:
            raise ReviewAdapterError("review_artifact_head_stale", details={**validation, "expected_head_sha": expected})
        validation["current_head_status"] = "MATCH"
    else:
        snapshot = pr_snapshot_path(config, repo, int(pr_number))
        if snapshot.exists():
            current = _read_mapping(snapshot)
            snapshot_repo = str(current.get("repo") or current.get("full_name") or "").strip()
            if isinstance(current.get("repository"), Mapping):
                snapshot_repo = str(
                    current["repository"].get("full_name")
                    or current["repository"].get("name")
                    or snapshot_repo
                ).strip()
            snapshot_number = current.get("number") or current.get("pr_number") or current.get("index")
            identity_ok = True
            if snapshot_repo and snapshot_repo != repo:
                identity_ok = False
                raise ReviewAdapterError(
                    "review_current_snapshot_repo_mismatch",
                    details={**validation, "snapshot_repo": snapshot_repo, "snapshot_path": str(snapshot)},
                )
            if snapshot_number is not None:
                try:
                    if int(snapshot_number) != int(pr_number):
                        identity_ok = False
                        raise ReviewAdapterError(
                            "review_current_snapshot_number_mismatch",
                            details={**validation, "snapshot_number": snapshot_number, "snapshot_path": str(snapshot)},
                        )
                except (TypeError, ValueError) as exc:
                    raise ReviewAdapterError(
                        "review_current_snapshot_number_invalid",
                        details={**validation, "snapshot_number": snapshot_number, "snapshot_path": str(snapshot)},
                    ) from exc
            validation["current_identity_status"] = "MATCH" if identity_ok and snapshot_repo and snapshot_number is not None else "UNVERIFIED"

            current_head_obj = current.get("head") if isinstance(current.get("head"), Mapping) else {}
            current_base_obj = current.get("base") if isinstance(current.get("base"), Mapping) else {}
            current_head = str(
                current.get("head_sha")
                or current.get("head_commit_id")
                or current_head_obj.get("sha")
                or current_head_obj.get("commit_id")
                or ""
            )
            current_base = str(
                current.get("base_sha")
                or current_base_obj.get("sha")
                or current_base_obj.get("commit_id")
                or ""
            )
            if current_head and current_head != head_sha:
                raise ReviewAdapterError(
                    "review_artifact_head_stale",
                    details={**validation, "expected_head_sha": current_head, "snapshot_path": str(snapshot)},
                )
            if current_base and str(report.get("base_sha") or "") and current_base != str(report.get("base_sha")):
                raise ReviewAdapterError(
                    "review_artifact_base_stale",
                    details={**validation, "expected_base_sha": current_base, "snapshot_path": str(snapshot)},
                )
            current_head_ref = str(current.get("head_ref") or current_head_obj.get("ref") or "")
            current_base_ref = str(current.get("base_ref") or current_base_obj.get("ref") or "")
            if current_head_ref and report.get("head_ref") and current_head_ref != str(report.get("head_ref")):
                raise ReviewAdapterError(
                    "review_artifact_head_ref_stale",
                    details={**validation, "expected_head_ref": current_head_ref, "snapshot_path": str(snapshot)},
                )
            if current_base_ref and report.get("base_ref") and current_base_ref != str(report.get("base_ref")):
                raise ReviewAdapterError(
                    "review_artifact_base_ref_stale",
                    details={**validation, "expected_base_ref": current_base_ref, "snapshot_path": str(snapshot)},
                )
            validation["current_head_status"] = "MATCH" if current_head else "UNVERIFIED"

            current_state = str(current.get("state") or current.get("status") or "").strip().lower()
            current_merged = current.get("merged") is True
            validation["current_state"] = current_state or "UNVERIFIED"
            if strict and current_merged:
                raise ReviewAdapterError(
                    "review_current_pr_merged",
                    details={**validation, "snapshot_path": str(snapshot)},
                )
            if strict and current_state and current_state not in {"open", "opened"}:
                raise ReviewAdapterError(
                    "review_current_pr_not_open",
                    details={**validation, "snapshot_path": str(snapshot)},
                )

            current_updated = str(current.get("updated_at") or current.get("updated_on") or "").strip()
            reported_updated = str(report.get("pr_updated_at") or report.get("updated_at") or "").strip()
            if reported_updated:
                if current_updated and current_updated != reported_updated:
                    raise ReviewAdapterError(
                        "review_artifact_updated_at_stale",
                        details={
                            **validation,
                            "expected_updated_at": current_updated,
                            "reported_updated_at": reported_updated,
                            "snapshot_path": str(snapshot),
                        },
                    )
                validation["current_updated_at_status"] = "MATCH" if current_updated else "UNVERIFIED"
            else:
                validation["current_updated_at_status"] = "UNPINNED"
        elif require_current_snapshot:
            raise ReviewAdapterError("review_current_head_unverifiable", details={**validation, "snapshot_path": str(snapshot)})

    missing_evidence = _check_evidence_paths(config, report)
    validation["evidence_missing"] = missing_evidence
    if strict and missing_evidence:
        raise ReviewAdapterError("review_evidence_missing", details=validation)
    if strict and require_current_snapshot and validation["current_head_status"] != "MATCH":
        raise ReviewAdapterError("review_current_head_unverifiable", details=validation)
    if strict and require_current_snapshot and validation["current_identity_status"] != "MATCH":
        raise ReviewAdapterError("review_current_identity_unverifiable", details=validation)

    validation["status"] = "PASS" if hash_status == "MATCH" and not missing_evidence and validation["current_head_status"] in {"MATCH", "UNVERIFIED"} else "HOLD"
    return report, validation
