from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .runner import utc_now

WRITE_LEDGER_NAME = "write-ledger.json"
WRITE_LEDGER_SCHEMA = "quality-pilot.gitea-mcp-write-ledger.v1"


def write_ledger_path(config: ProjectConfig) -> Path:
    return config.paths.state / "gitea-mcp" / WRITE_LEDGER_NAME


def record_gitea_mcp_write_request(
    config: ProjectConfig,
    request: dict[str, Any],
    request_path: str | Path,
    *,
    source_module: str,
    target_type: str,
) -> dict[str, Any]:
    ledger_path = write_ledger_path(config)
    now = utc_now()
    existing = _load_ledger(ledger_path)
    entries_by_id = {
        str(entry.get("operation_id")): entry
        for entry in existing.get("entries", [])
        if isinstance(entry, dict) and entry.get("operation_id")
    }
    touched: list[str] = []
    for entry in _entries_from_request(
        config,
        request,
        Path(request_path),
        source_module=source_module,
        target_type=target_type,
        now=now,
    ):
        operation_id = str(entry["operation_id"])
        previous = entries_by_id.get(operation_id, {})
        merged = {
            **previous,
            **entry,
            "first_seen_at": previous.get("first_seen_at") or now,
            "updated_at": now,
        }
        entries_by_id[operation_id] = merged
        touched.append(operation_id)
    entries = sorted(entries_by_id.values(), key=lambda item: str(item.get("operation_id") or ""))
    ledger = {
        "schema": WRITE_LEDGER_SCHEMA,
        "updated_at": now,
        "entry_count": len(entries),
        "entries": entries,
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json_dumps(ledger) + "\n", encoding="utf-8")
    return {
        **ledger,
        "path": _relative_or_str(ledger_path, config.root),
        "touched_operation_ids": touched,
    }


def reconcile_gitea_mcp_write_results(config: ProjectConfig) -> dict[str, Any]:
    ledger_path = write_ledger_path(config)
    ledger = _load_ledger(ledger_path)
    now = utc_now()
    updated = 0
    entries: list[dict[str, Any]] = []
    for entry in ledger.get("entries", []):
        if not isinstance(entry, dict):
            continue
        result_path = _entry_result_path(config, entry)
        result_payload = _load_json(result_path)
        next_entry = dict(entry)
        next_entry["result_exists"] = bool(result_path and result_path.exists())
        next_entry["result_status"] = _result_status(result_payload)
        next_entry["remote_id"] = _extract_remote_id(result_payload, entry)
        if next_entry != entry:
            next_entry["updated_at"] = now
            if result_payload is not None:
                next_entry["last_reconciled_at"] = now
            updated += 1
        entries.append(next_entry)
    next_ledger = {
        "schema": WRITE_LEDGER_SCHEMA,
        "updated_at": now if updated else ledger.get("updated_at", now),
        "entry_count": len(entries),
        "entries": sorted(entries, key=lambda item: str(item.get("operation_id") or "")),
    }
    if updated:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json_dumps(next_ledger) + "\n", encoding="utf-8")
    return {
        **next_ledger,
        "path": _relative_or_str(ledger_path, config.root),
        "exists": ledger_path.exists(),
        "updated_count": updated,
    }


def _entries_from_request(
    config: ProjectConfig,
    request: dict[str, Any],
    request_path: Path,
    *,
    source_module: str,
    target_type: str,
    now: str,
) -> list[dict[str, Any]]:
    actions = [item for item in request.get("actions", []) if isinstance(item, dict)]
    if actions:
        return [
            _entry_from_action(
                config,
                request,
                action,
                request_path,
                source_module=source_module,
                target_type=_target_type_for_action(target_type, action),
                now=now,
            )
            for action in actions
        ]
    return [
        _entry_from_request(
            config,
            request,
            request_path,
            source_module=source_module,
            target_type=target_type,
            now=now,
        )
    ]


def _entry_from_action(
    config: ProjectConfig,
    request: dict[str, Any],
    action: dict[str, Any],
    request_path: Path,
    *,
    source_module: str,
    target_type: str,
    now: str,
) -> dict[str, Any]:
    operation = str(action.get("operation") or request.get("operation") or "gitea.write")
    idempotency_key = str(action.get("idempotency_key") or action.get("id") or _hash_payload(action))
    result_path = _result_path(config, request)
    result_payload = _load_json(result_path)
    return {
        "operation_id": f"{operation}:{idempotency_key}",
        "target_type": target_type,
        "operation": operation,
        "request_operation": request.get("operation"),
        "source_module": source_module,
        "request_schema": request.get("schema"),
        "request_status": request.get("status"),
        "request_path": _relative_or_str(request_path, config.root),
        "result_path": _relative_or_str(result_path, config.root) if result_path else None,
        "result_exists": bool(result_path and result_path.exists()),
        "result_status": _result_status(result_payload),
        "remote_id": _extract_remote_id(result_payload, action),
        "remote_url": _extract_remote_url(result_payload, action),
        "idempotency_key": idempotency_key,
        "action_id": action.get("id"),
        "gitea_issue_id": action.get("gitea_issue_id"),
        "redmine_issue_id": action.get("redmine_issue_id"),
        "redmine_issue_ids": action.get("redmine_issue_ids"),
        "case_id": action.get("case_id"),
        "case_ids": action.get("case_ids"),
        "evidence_paths": action.get("evidence_paths"),
        "action_safety_class": action.get("action_safety_class"),
        "gate_result": action.get("write_gate_result") or request.get("write_gate_result"),
        "created_at": request.get("created_at") or now,
    }


def _entry_from_request(
    config: ProjectConfig,
    request: dict[str, Any],
    request_path: Path,
    *,
    source_module: str,
    target_type: str,
    now: str,
) -> dict[str, Any]:
    operation = str(request.get("operation") or "gitea.write")
    idempotency_key = _request_idempotency_key(request)
    result_path = _result_path(config, request)
    result_payload = _load_json(result_path)
    return {
        "operation_id": f"{operation}:{idempotency_key}",
        "target_type": target_type,
        "operation": operation,
        "request_operation": request.get("operation"),
        "source_module": source_module,
        "request_schema": request.get("schema"),
        "request_status": request.get("status"),
        "request_path": _relative_or_str(request_path, config.root),
        "result_path": _relative_or_str(result_path, config.root) if result_path else None,
        "result_exists": bool(result_path and result_path.exists()),
        "result_status": _result_status(result_payload),
        "remote_id": _extract_remote_id(result_payload, request),
        "remote_url": _extract_remote_url(result_payload, request),
        "idempotency_key": idempotency_key,
        "page": request.get("page"),
        "plan_path": request.get("plan_path"),
        "gate_result": request.get("write_gate_result"),
        "created_at": request.get("created_at") or now,
    }


def _target_type_for_action(default: str, action: dict[str, Any]) -> str:
    operation = str(action.get("operation") or "")
    if operation == "gitea.pull_request.create" or str(action.get("update_kind") or "") == "pr_linkage":
        return "pr_linkage"
    if operation == "gitea.issue.create":
        return "issue_create"
    if operation == "gitea.issue.update":
        if default == "issue_evidence_update" or str(action.get("update_kind") or "") == "evidence":
            return "issue_evidence_update"
        return "issue_update"
    return default


def _request_idempotency_key(request: dict[str, Any]) -> str:
    if request.get("idempotency_key"):
        return str(request["idempotency_key"])
    page = str(request.get("page") or "")
    plan_path = str(request.get("plan_path") or "")
    body = str(request.get("body") or "")
    message = str(request.get("message") or "")
    digest = hashlib.sha256(f"{page}\n{plan_path}\n{message}\n{body}".encode("utf-8")).hexdigest()[:16]
    prefix = "wiki" if request.get("operation") == "gitea.wiki.update_page" else "request"
    return f"{prefix}-{digest}"


def _result_path(config: ProjectConfig, request: dict[str, Any]) -> Path | None:
    raw = request.get("result_path")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if path.is_absolute():
        return path
    return config.root / path


def _entry_result_path(config: ProjectConfig, entry: dict[str, Any]) -> Path | None:
    raw = entry.get("result_path")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if path.is_absolute():
        return path
    return config.root / path


def _load_ledger(path: Path) -> dict[str, Any]:
    loaded = _load_json(path)
    if isinstance(loaded, dict) and loaded.get("schema") == WRITE_LEDGER_SCHEMA and isinstance(loaded.get("entries"), list):
        return loaded
    return {"schema": WRITE_LEDGER_SCHEMA, "entries": []}


def _load_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _result_status(result_payload: Any) -> str | None:
    if not isinstance(result_payload, dict):
        return None
    status = result_payload.get("status")
    if status is not None:
        return str(status)
    if result_payload.get("ok") is True:
        return "ok"
    return None


def _extract_remote_id(result_payload: Any, source: dict[str, Any]) -> Any:
    if not isinstance(result_payload, dict):
        return None
    direct = result_payload.get("issue_id") or result_payload.get("gitea_issue_id") or result_payload.get("number")
    if direct is not None:
        return direct
    redmine_issue_id = source.get("redmine_issue_id")
    for item in _walk_dicts(result_payload):
        if redmine_issue_id is not None and item.get("redmine_issue_id") not in {None, redmine_issue_id}:
            continue
        remote_id = item.get("issue_id") or item.get("gitea_issue_id") or item.get("number") or item.get("index")
        if remote_id is not None:
            return remote_id
    return result_payload.get("url") or result_payload.get("html_url")


def _extract_remote_url(result_payload: Any, source: dict[str, Any]) -> Any:
    if isinstance(result_payload, dict):
        for key in ("html_url", "url", "remote_url"):
            if result_payload.get(key):
                return result_payload.get(key)
        for item in _walk_dicts(result_payload):
            for key in ("html_url", "url", "remote_url"):
                if item.get(key):
                    return item.get(key)
    for key in ("html_url", "url", "remote_url"):
        if source.get(key):
            return source.get(key)
    return None


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for item in value.values():
            out.extend(_walk_dicts(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_walk_dicts(item))
    return out


def _hash_payload(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
