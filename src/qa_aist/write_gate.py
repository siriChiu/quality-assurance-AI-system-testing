from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import find_raw_secret_paths


@dataclass(frozen=True)
class WriteGateResult:
    allowed: bool
    reason: str
    target_state: str
    contract_match: bool
    evidence_current: bool
    contains_raw_secret: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "target_state": self.target_state,
            "contract_match": self.contract_match,
            "evidence_current": self.evidence_current,
            "contains_raw_secret": self.contains_raw_secret,
        }


def evaluate_write_gate(
    *,
    config_data: dict[str, Any],
    result: dict[str, Any] | None = None,
    target_state: str = "unknown",
    expected_contract_hash: str | None = None,
) -> WriteGateResult:
    result = result or {}
    contains_raw_secret = bool(find_raw_secret_paths({"config": config_data, "result": result}))
    contract_hash = result.get("contract_hash")
    contract_match = expected_contract_hash in {None, "", contract_hash}
    evidence_current = bool(result.get("evidence")) and result.get("status") in {"PASS", "FAIL", "BLOCK"}
    tracker = config_data.get("tracker") if isinstance(config_data.get("tracker"), dict) else {}
    provider = str(tracker.get("provider", "none")).lower()
    if contains_raw_secret:
        reason = "raw_secret_detected"
    elif target_state == "closed":
        reason = "closed_issue_write_forbidden"
    elif not contract_match:
        reason = "contract_drift"
    elif not evidence_current:
        reason = "missing_current_evidence"
    elif provider in {"", "none", "disabled"}:
        reason = "tracker_disabled"
    else:
        reason = "allowed"
    return WriteGateResult(
        allowed=reason == "allowed",
        reason=reason,
        target_state=target_state,
        contract_match=contract_match,
        evidence_current=evidence_current,
        contains_raw_secret=contains_raw_secret,
    )
