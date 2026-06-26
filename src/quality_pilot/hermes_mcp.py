from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import ProjectConfig

MCP_SERVERS_ENV = "QUALITY_PILOT_HERMES_MCP_SERVERS"
MCP_STATUS_ENV = "QUALITY_PILOT_HERMES_MCP_STATUS_JSON"
MCP_STATUS_SCHEMA = "quality-pilot.hermes-mcp-status.v1"


def tracker_mcp_config(config_data: dict[str, Any]) -> dict[str, Any]:
    tracker = config_data.get("tracker") if isinstance(config_data.get("tracker"), dict) else {}
    mcp = tracker.get("mcp") if isinstance(tracker.get("mcp"), dict) else {}
    return {
        "required_servers": _list_of_strings(mcp.get("required_servers") or ["gitea", "redmine"]),
        "status_json": str(mcp.get("status_json") or ".quality-pilot-project/state/hermes-mcp/status.json"),
        "gitea_issues_json": str(mcp.get("gitea_issues_json") or ".quality-pilot-project/state/gitea-mcp/issues.json"),
        "redmine_issues_json": str(mcp.get("redmine_issues_json") or ".quality-pilot-project/state/redmine-mcp/issues.json"),
        "wiki_write_request_json": str(mcp.get("wiki_write_request_json") or ".quality-pilot-project/state/gitea-mcp/wiki-write-request.json"),
        "wiki_write_result_json": str(mcp.get("wiki_write_result_json") or ".quality-pilot-project/state/gitea-mcp/wiki-write-result.json"),
    }


def hermes_mcp_status_path(config: ProjectConfig) -> Path:
    raw = os.getenv(MCP_STATUS_ENV) or tracker_mcp_config(config.data)["status_json"]
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (config.root / path).resolve()


def configured_mcp_json_path(config: ProjectConfig, key: str) -> Path:
    raw = tracker_mcp_config(config.data)[key]
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (config.root / path).resolve()


def hermes_mcp_status(config: ProjectConfig) -> dict[str, Any]:
    env_servers = os.getenv(MCP_SERVERS_ENV)
    if env_servers is not None:
        servers = _normalize_server_names(env_servers.split(","))
        return {
            "known": True,
            "source": "env",
            "servers": sorted(servers),
            "status_path": _relative_or_str(hermes_mcp_status_path(config), config.root),
        }

    path = hermes_mcp_status_path(config)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "known": False,
                "source": "status_json",
                "status_path": _relative_or_str(path, config.root),
                "error": "hermes_mcp_status_invalid",
                "servers": [],
            }
        servers = _servers_from_payload(loaded)
        return {
            "known": True,
            "source": "status_json",
            "status_path": _relative_or_str(path, config.root),
            "servers": sorted(servers),
        }

    return {
        "known": False,
        "source": "missing",
        "status_path": _relative_or_str(path, config.root),
        "servers": [],
    }


def persist_hermes_mcp_status_from_env(config: ProjectConfig) -> dict[str, Any]:
    env_servers = os.getenv(MCP_SERVERS_ENV)
    path = hermes_mcp_status_path(config)
    if env_servers is None:
        return {
            "status": "not_requested",
            "source": "env_missing",
            "status_path": _relative_or_str(path, config.root),
            "persisted": False,
        }
    servers = sorted(_normalize_server_names(env_servers.split(",")))
    payload = {
        "schema": MCP_STATUS_SCHEMA,
        "source": "env",
        "env": MCP_SERVERS_ENV,
        "servers": servers,
    }
    before = None
    if path.exists():
        try:
            before = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            before = None
    changed = before != payload
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "source": "env",
        "status_path": _relative_or_str(path, config.root),
        "servers": servers,
        "persisted": True,
        "changed": changed,
    }


def hermes_mcp_readiness(config: ProjectConfig) -> dict[str, Any]:
    cfg = tracker_mcp_config(config.data)
    status = hermes_mcp_status(config)
    required = cfg["required_servers"]
    servers = set(status.get("servers", []))
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []

    if status.get("known"):
        checks.append({
            "name": "hermes.mcp.status",
            "status": "PASS",
            "source": status.get("source"),
            "servers": sorted(servers),
        })
    else:
        blockers.append("hermes_mcp_status_unknown")
        checks.append({
            "name": "hermes.mcp.status",
            "status": "WARN",
            "path": status.get("status_path"),
            "message": (
                "Hermes MCP server list was not provided to AI Quality Pilot. "
                f"Set {MCP_SERVERS_ENV}=gitea,redmine or write the configured MCP status JSON before setup/doctor."
            ),
            "expected_minimal_json": {"servers": required},
        })

    for server in required:
        available = _server_available(servers, server)
        if available:
            checks.append({"name": f"hermes.mcp.{server}", "status": "PASS", "server": server})
        else:
            blockers.append(f"hermes_{server}_mcp_missing" if status.get("known") else f"hermes_{server}_mcp_unknown")
            checks.append({
                "name": f"hermes.mcp.{server}",
                "status": "WARN",
                "server": server,
                "message": f"Hermes {server} MCP server is not available to AI Quality Pilot yet.",
            })

    return {
        "status": "ready" if not blockers else "blocked",
        "required_servers": required,
        "servers": sorted(servers),
        "known": bool(status.get("known")),
        "status_path": status.get("status_path"),
        "blockers": sorted(set(blockers)),
        "checks": checks,
        "paths": {
            "gitea_issues_json": _relative_or_str(configured_mcp_json_path(config, "gitea_issues_json"), config.root),
            "redmine_issues_json": _relative_or_str(configured_mcp_json_path(config, "redmine_issues_json"), config.root),
            "wiki_write_request_json": _relative_or_str(configured_mcp_json_path(config, "wiki_write_request_json"), config.root),
            "wiki_write_result_json": _relative_or_str(configured_mcp_json_path(config, "wiki_write_result_json"), config.root),
        },
    }


def mcp_server_is_available(config: ProjectConfig, server: str) -> bool:
    status = hermes_mcp_status(config)
    return bool(status.get("known")) and _server_available(set(status.get("servers", [])), server)


def _servers_from_payload(payload: Any) -> set[str]:
    if isinstance(payload, list):
        return _normalize_server_names(payload)
    if not isinstance(payload, dict):
        return set()
    for key in ("servers", "mcp_servers", "available_servers"):
        if isinstance(payload.get(key), list):
            return _normalize_server_names(payload[key])
    servers: set[str] = set()
    for key in ("gitea", "redmine"):
        value = payload.get(key)
        if value is True or (isinstance(value, dict) and value.get("available") is True):
            servers.add(key)
    tools = payload.get("tools")
    if isinstance(tools, list):
        servers |= _normalize_server_names(tools)
    return servers


def _normalize_server_names(items: list[Any]) -> set[str]:
    servers: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("server") or item.get("name") or item.get("id") or "")
        else:
            name = str(item)
        normalized = name.strip().lower()
        if normalized:
            servers.add(normalized)
    return servers


def _server_available(servers: set[str], server: str) -> bool:
    target = server.lower()
    return any(target == item or target in item for item in servers)


def _list_of_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip().lower() for item in value.split(",") if item.strip()]
    return []


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
