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
    duplicate_candidate: bool = False
    sync_current: bool = True
    post_authored_by_actor: bool = True
    contains_internal_text: bool = False
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "target_state": self.target_state,
            "contract_match": self.contract_match,
            "evidence_current": self.evidence_current,
            "contains_raw_secret": self.contains_raw_secret,
            "duplicate_candidate": self.duplicate_candidate,
            "sync_current": self.sync_current,
            "post_authored_by_actor": self.post_authored_by_actor,
            "contains_internal_text": self.contains_internal_text,
            "reason_codes": list(self.reason_codes or ((self.reason,) if self.reason != "allowed" else ())),
        }


def evaluate_write_gate(
    *,
    config_data: dict[str, Any],
    result: dict[str, Any] | None = None,
    target_state: str = "unknown",
    expected_contract_hash: str | None = None,
    duplicate_candidate: bool = False,
    sync_current: bool = True,
    actor_login: str | None = None,
    post_author_login: str | None = None,
    write_text: str = "",
) -> WriteGateResult:
    result = result or {}
    contains_raw_secret = bool(find_raw_secret_paths({"config": config_data, "result": result}))
    contract_hash = result.get("contract_hash")
    contract_match = expected_contract_hash in {None, "", contract_hash}
    evidence_current = bool(result.get("evidence")) and result.get("status") in {"PASS", "FAIL", "BLOCK"}
    post_authored_by_actor = _post_authored_by_actor(actor_login, post_author_login)
    contains_internal_text = _contains_internal_text(write_text)
    tracker = config_data.get("tracker") if isinstance(config_data.get("tracker"), dict) else {}
    provider = str(tracker.get("provider", "none")).lower()
    reasons: list[str] = []
    if contains_raw_secret:
        reasons.append("raw_secret_detected")
    if target_state == "closed":
        reasons.append("closed_issue_write_forbidden")
    if duplicate_candidate:
        reasons.append("duplicate_issue_candidate")
    if not sync_current:
        reasons.append("stale_or_missing_issue_sync")
    if not contract_match:
        reasons.append("contract_drift")
    if not evidence_current:
        reasons.append("missing_current_evidence")
    if not post_authored_by_actor:
        reasons.append("post_not_authored_by_actor")
    if contains_internal_text:
        reasons.append("internal_text_leak")
    if provider in {"", "none", "disabled"}:
        reasons.append("tracker_disabled")
    reason = reasons[0] if reasons else "allowed"
    return WriteGateResult(
        allowed=reason == "allowed",
        reason=reason,
        target_state=target_state,
        contract_match=contract_match,
        evidence_current=evidence_current,
        contains_raw_secret=contains_raw_secret,
        duplicate_candidate=duplicate_candidate,
        sync_current=sync_current,
        post_authored_by_actor=post_authored_by_actor,
        contains_internal_text=contains_internal_text,
        reason_codes=tuple(reasons),
    )


def _post_authored_by_actor(actor_login: str | None, post_author_login: str | None) -> bool:
    if not post_author_login:
        return True
    if not actor_login:
        return False
    return actor_login == post_author_login


def _contains_internal_text(text: str) -> bool:
    lowered = text.lower()
    blocked_tokens = [".qa/", "run_id", "heartbeat", "agent prompt", "system prompt", "raw token", "api_token:"]
    return any(token in lowered for token in blocked_tokens)
