"""Deterministic secret detection and redaction helpers.

The quality engine must fail closed at remote-write and persistence boundaries.
This module intentionally contains no network or model calls: callers can use
it before writing case contracts, evidence, graph events, subagent prompts, or
Gitea/Redmine payloads.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


_ENV_REFERENCE_RE = re.compile(r"^\$\{?[A-Z_][A-Z0-9_]*\}?$")
_SECRET_KEY_RE = re.compile(r"(?:token|password|passwd|secret|api[_-]?key|authorization|bearer)", re.IGNORECASE)
_RESTRICTED_KEY_RE = re.compile(
    r"(?:customer(?:[_-]?(?:data|id|name))?|pii|personal[_-]?(?:data|information)|restricted(?:[_-]?(?:data|lab))?|account[_-]?number|email)",
    re.IGNORECASE,
)
_SKIP_HIGH_ENTROPY_KEYS = re.compile(
    r"(?:hash|sha|checksum|digest|fingerprint|contract_hash|report_hash|idempotency|uuid|commit|revision|version)",
    re.IGNORECASE,
)

# The patterns are deliberately conservative.  They detect recognizable secret
# formats and explicit credential assignments/flags without treating ordinary
# prose, URLs, commit hashes, or environment variable references as secrets.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    ),
    (
        "token_prefix",
        re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|glpat-[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|api[_-]?key|token|secret|authorization|bearer)\s*[:=]\s*(?!\$\{?[A-Z_][A-Z0-9_]*\}?)(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;]+)"
        ),
    ),
    (
        "credential_flag",
        re.compile(
            r"(?i)(?:--password|--passwd|--api[-_]key|--token|--secret)\s+(?!\$\{?[A-Z_][A-Z0-9_]*\}?)(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;]+)"
        ),
    ),
    (
        "authorization_header",
        re.compile(r"(?i)\b(?:authorization|x-api-key)\s*:\s*(?!\$\{?[A-Z_][A-Z0-9_]*\}?)[^\s,;]+"),
    ),
    (
        "pii_marker",
        re.compile(
            r"(?i)\b(?:customer(?:[_-]?(?:id|name|data))?|pii|personal[_-]?(?:data|information)|restricted[_-]?(?:data|lab)|account[_-]?number)\s*[:=]\s*(?!\$\{?[A-Z_][A-Z0-9_]*\}?)(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;]+)"
        ),
    ),
    (
        "email_address",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
)
_OPAQUE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9])")
_SAFE_METADATA_RE = re.compile(
    r"(?:QUALITY_PILOT_[A-Z0-9_]+|quality-pilot|runtime_profile|growth-context|init-context|\.quality-pilot-project|[A-Za-z0-9_.-]+\.(?:json|yaml|yml|md|log|py|sh)|(?:INIT|GROW|REDMINE|ISSUE)-[A-Z0-9_-]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SecretFinding:
    path: str
    kind: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind}


class RedactionError(ValueError):
    """Raised when a sensitive payload cannot be safely redacted."""


def find_secret_text(text: str, *, path: str = "") -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    value = str(text or "")
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            findings.append(SecretFinding(path=path or "$", kind=kind))
    if not _skip_opaque_scan(path) and _has_opaque_token(value):
        findings.append(SecretFinding(path=path or "$", kind="opaque_secret"))
    return _unique_findings(findings)


def redact_text(text: str, *, path: str = "") -> tuple[str, list[SecretFinding]]:
    value = str(text or "")
    findings: list[SecretFinding] = []
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            findings.append(SecretFinding(path=path or "$", kind=kind))
            value = pattern.sub(f"[REDACTED:{kind}]", value)
    if not _skip_opaque_scan(path) and _has_opaque_token(value):
        findings.append(SecretFinding(path=path or "$", kind="opaque_secret"))
        value = _OPAQUE_TOKEN_RE.sub(_redact_opaque_match, value)
    return value, _unique_findings(findings)


def find_sensitive_paths(value: Any, prefix: str = "$") -> list[str]:
    """Find raw secret-like values in arbitrary JSON/YAML-shaped data.

    Key names are checked even when the value is not a recognizable token.  A
    variable reference (``$ENV``/``${ENV}``) and explicit redaction marker are
    safe references, not raw values.  High-entropy checks are limited to
    credential-shaped keys so hashes and commit IDs are not false positives.
    """

    findings: list[SecretFinding] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix != "$" else str(key)
            key_text = str(key)
            if isinstance(item, str) and _sensitive_key_match(key_text) and not _is_safe_reference(key_text, item):
                kind = "credential_field" if _SECRET_KEY_RE.search(key_text) else "restricted_field"
                findings.append(SecretFinding(path=path, kind=kind))
                if _looks_opaque_secret(item) and not _SKIP_HIGH_ENTROPY_KEYS.search(key_text):
                    findings.append(SecretFinding(path=path, kind="opaque_secret"))
            findings.extend(find_sensitive_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(find_sensitive_paths(item, f"{prefix}[{index}]"))
    elif isinstance(value, str):
        findings.extend(find_secret_text(value, path=prefix))
    return _unique_paths_and_kinds(findings)


def redact_structure(value: Any, prefix: str = "$") -> tuple[Any, list[SecretFinding]]:
    """Return a recursively redacted JSON/YAML-shaped copy."""

    findings: list[SecretFinding] = []
    if isinstance(value, dict):
        output: dict[Any, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix != "$" else str(key)
            if isinstance(item, str) and _sensitive_key_match(str(key)) and not _is_safe_reference(str(key), item):
                kind = "credential_field" if _SECRET_KEY_RE.search(str(key)) else "restricted_field"
                output[key] = f"[REDACTED:{kind}]"
                findings.append(SecretFinding(path=path, kind=kind))
                continue
            redacted, nested = redact_structure(item, path)
            output[key] = redacted
            findings.extend(nested)
        return output, _unique_paths_and_kinds(findings)
    if isinstance(value, list):
        output_list: list[Any] = []
        for index, item in enumerate(value):
            redacted, nested = redact_structure(item, f"{prefix}[{index}]")
            output_list.append(redacted)
            findings.extend(nested)
        return output_list, _unique_paths_and_kinds(findings)
    if isinstance(value, str):
        redacted, text_findings = redact_text(value, path=prefix)
        return redacted, text_findings
    return value, []


def ensure_safe_structure(value: Any, *, context: str = "payload") -> None:
    findings = find_sensitive_paths(value)
    if findings:
        first = findings[0]
        raise RedactionError(f"{context} contains raw secret-like material at {first.path} ({first.kind})")


def ensure_safe_text(text: str, *, context: str = "payload") -> None:
    findings = find_secret_text(text)
    if findings:
        first = findings[0]
        raise RedactionError(f"{context} contains raw secret-like material ({first.kind})")


def _sensitive_key_match(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key)) or _RESTRICTED_KEY_RE.search(str(key)))


def _skip_opaque_scan(path: str) -> bool:
    return bool(_SKIP_HIGH_ENTROPY_KEYS.search(str(path or "")))


def _has_opaque_token(value: str) -> bool:
    text = str(value or "").strip()
    matches = list(_OPAQUE_TOKEN_RE.finditer(text))
    if len(matches) != 1:
        return False
    match = matches[0]
    if match.start() != 0 or match.end() != len(text):
        return False
    return _looks_opaque_secret(match.group(0)) and not _SAFE_METADATA_RE.search(text)


def _redact_opaque_match(match: re.Match[str]) -> str:
    token = match.group(0)
    if re.fullmatch(r"[0-9a-fA-F]{32,}", token):
        return token
    return "[REDACTED:opaque_secret]"


def _is_safe_reference(key: str, value: str) -> bool:
    stripped = str(value).strip()
    if stripped in {"", "[REDACTED]", "REDACTED"}:
        return True
    if key.lower().endswith(("_env", "-env")) or key.lower() in {"api_token_env", "api_key_env"}:
        return bool(re.fullmatch(r"[A-Z_][A-Z0-9_]*", stripped))
    return bool(_ENV_REFERENCE_RE.fullmatch(stripped))


def _looks_opaque_secret(value: str) -> bool:
    text = str(value).strip()
    if len(text) < 24 or any(char.isspace() for char in text):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", text):
        return False
    if re.fullmatch(r"[0-9a-fA-F-]{32,}", text):
        return False
    alphabet = set(text)
    entropy = -sum((text.count(char) / len(text)) * math.log2(text.count(char) / len(text)) for char in alphabet)
    return entropy >= 4.2 and len(alphabet) >= 12


def _unique_findings(findings: list[SecretFinding]) -> list[SecretFinding]:
    seen: set[tuple[str, str]] = set()
    output: list[SecretFinding] = []
    for finding in findings:
        key = (finding.path, finding.kind)
        if key not in seen:
            seen.add(key)
            output.append(finding)
    return output


def _unique_paths_and_kinds(findings: list[SecretFinding]) -> list[SecretFinding]:
    return _unique_findings(findings)
