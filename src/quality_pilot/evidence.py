"""Confirmed-bug evidence completeness and traceability gate.

A normal case result can be useful without this profile.  The stricter profile
is activated only when a result/contract explicitly declares a confirmed bug or
``enforce_confirmed_bug_evidence``.  This keeps legacy overlays compatible while
making PASS/merge eligibility deterministic for bug claims.
"""

from __future__ import annotations

from typing import Any

REQUIRED_CONFIRMED_BUG_EVIDENCE = (
    "exact_reproduction",
    "deterministic_regression",
    "user_facing_smoke",
    "sibling_surface",
    "boundary_invalid",
    "side_effect",
    "residual_risk",
)


def confirmed_bug_evidence_required(value: dict[str, Any] | None) -> bool:
    payload = value if isinstance(value, dict) else {}
    quality_pilot = payload.get("quality_pilot") if isinstance(payload.get("quality_pilot"), dict) else {}
    return bool(
        payload.get("confirmed_bug")
        or payload.get("bug_confirmation")
        or payload.get("enforce_confirmed_bug_evidence")
        or quality_pilot.get("confirmed_bug")
        or quality_pilot.get("enforce_confirmed_bug_evidence")
    )


def evaluate_confirmed_bug_evidence(
    result: dict[str, Any] | None,
    *,
    contract_hash: str | None = None,
    pr_head: str | None = None,
    required: tuple[str, ...] = REQUIRED_CONFIRMED_BUG_EVIDENCE,
) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    profile = payload.get("evidence_profile")
    records = _records(profile)
    missing: list[str] = []
    invalid: list[dict[str, Any]] = []
    normalized: dict[str, dict[str, Any]] = {}
    for name in required:
        record = records.get(name)
        if not record:
            missing.append(name)
            continue
        normalized[name] = record
        record_status = str(record.get("status") or "").upper()
        evidence_paths = record.get("evidence_paths")
        if not isinstance(evidence_paths, list):
            evidence_paths = record.get("evidence") if isinstance(record.get("evidence"), list) else []
        problems: list[str] = []
        if record_status != "PASS":
            problems.append("status_not_pass")
        if not evidence_paths:
            problems.append("evidence_missing")
        if not record.get("case_id"):
            problems.append("case_id_missing")
        if not record.get("run_id"):
            problems.append("run_id_missing")
        expected_hash = contract_hash or payload.get("contract_hash")
        if expected_hash and record.get("contract_hash") != expected_hash:
            problems.append("contract_drift")
        if pr_head and record.get("pr_head") != pr_head:
            problems.append("pr_head_drift")
        if name == "residual_risk" and not str(record.get("summary") or record.get("assessment") or "").strip():
            problems.append("residual_risk_summary_missing")
        if problems:
            invalid.append({"name": name, "reason_codes": problems})

    blockers = _execution_blockers(payload)
    if blockers:
        outcome = "BLOCK"
        reason = "confirmed_bug_execution_prerequisite_missing"
    elif missing or invalid:
        outcome = "HOLD"
        reason = "confirmed_bug_evidence_incomplete"
    else:
        outcome = "PASS"
        reason = "confirmed_bug_evidence_complete"
    return {
        "required": list(required),
        "outcome": outcome,
        "allowed": outcome == "PASS",
        "reason": reason,
        "missing": missing,
        "invalid": invalid,
        "records": normalized,
        "blockers": blockers,
        "traceability": {
            "contract_hash": contract_hash or payload.get("contract_hash"),
            "run_id": payload.get("run_id"),
            "pr_head": pr_head or payload.get("pr_head") or payload.get("head_sha"),
            "report_hash": payload.get("report_hash"),
        },
    }


def _records(profile: Any) -> dict[str, dict[str, Any]]:
    if isinstance(profile, dict):
        if isinstance(profile.get("records"), dict):
            return {str(key): value for key, value in profile["records"].items() if isinstance(value, dict)}
        return {str(key): value for key, value in profile.items() if isinstance(value, dict)}
    if isinstance(profile, list):
        output: dict[str, dict[str, Any]] = {}
        for item in profile:
            if isinstance(item, dict) and item.get("name"):
                output[str(item["name"])] = item
        return output
    return {}


def _execution_blockers(payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for key in ("environment_blockers", "execution_blockers"):
        value = payload.get(key)
        if isinstance(value, list):
            blockers.extend(str(item) for item in value if str(item))
    if str(payload.get("blocked_reason") or "") in {
        "environment_profile_required",
        "fixture_missing",
        "credential_env_missing",
        "target_host_missing",
        "executable_not_found",
        "unsafe_command_pattern",
        "command_timeout",
    }:
        blockers.append(str(payload["blocked_reason"]))
    environment = payload.get("environment_profile") if isinstance(payload.get("environment_profile"), dict) else {}
    if environment.get("ready") is False and payload.get("status") == "BLOCK":
        blockers.extend(str(item) for item in environment.get("blockers", []) if str(item))
    return sorted(set(blockers))
