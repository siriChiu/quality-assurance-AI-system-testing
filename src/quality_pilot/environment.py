from __future__ import annotations

"""Explicit execution-environment profile and preflight checks.

The repo can tell us a great deal about *what* might be executable, but it
cannot safely decide whether a case should run against the checkout, a lab,
or a remote target.  This module keeps that decision in the host project's
config and exposes only redacted readiness facts (never secret values).
"""

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import ENV_NAME_RE, ProjectConfig, QAConfigError


ENVIRONMENT_PROFILE_SCHEMA = "quality-pilot.environment-profile.v1"
VALID_MODES = {"local", "remote"}


def environment_profile_status(config: ProjectConfig) -> dict[str, Any]:
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    legacy_profile = "execution_mode" not in runtime and "environment_confirmed" not in runtime
    mode = str(runtime.get("execution_mode") or ("local" if legacy_profile else "")).strip().lower()
    confirmed = bool(runtime.get("environment_confirmed")) or legacy_profile
    target_host_env = str(runtime.get("target_host_env") or "QUALITY_PILOT_TARGET_HOST").strip()
    credential_envs = _string_list(runtime.get("credential_envs"))
    fixture_paths = _string_list(runtime.get("fixture_paths"))
    entrypoint = str(runtime.get("primary_entrypoint") or "").strip()
    boundary = str(runtime.get("side_effect_boundary") or "").strip()

    blockers: list[str] = []
    if mode not in VALID_MODES:
        blockers.append("execution_mode")
    if not confirmed:
        blockers.append("environment_confirmed")
    if not entrypoint:
        blockers.append("primary_entrypoint")
    if not boundary:
        blockers.append("side_effect_boundary")
    fixture_checks = []
    for value in fixture_paths:
        path = _resolve_path(config.root, value)
        exists = path.exists()
        fixture_checks.append({"path": value, "exists": exists})
        if not exists:
            blockers.append(f"fixture_missing:{value}")
    credential_checks = []
    for name in credential_envs:
        valid_name = bool(ENV_NAME_RE.match(name))
        present = bool(valid_name and os.environ.get(name))
        credential_checks.append({"name": name, "valid_name": valid_name, "present": present})
        if not valid_name:
            blockers.append(f"credential_env_invalid:{name}")
        elif not present:
            blockers.append(f"credential_env_missing:{name}")

    target_required = mode == "remote"
    target_present = bool(ENV_NAME_RE.match(target_host_env) and os.environ.get(target_host_env))
    if target_required and not ENV_NAME_RE.match(target_host_env):
        blockers.append("target_host_env_invalid")
    elif target_required and not target_present:
        blockers.append(f"target_host_missing:{target_host_env}")

    return {
        "schema": ENVIRONMENT_PROFILE_SCHEMA,
        "status": "ready" if not blockers else "needs_user_input",
        "ready": not blockers,
        "needs_user_input": bool(blockers),
        "execution_mode": mode or None,
        "environment_confirmed": confirmed,
        "configured": {
            "execution_mode": mode,
            "environment_confirmed": confirmed,
            "primary_entrypoint": entrypoint,
            "target_host_env": target_host_env,
            "fixture_paths": fixture_paths,
            "credential_envs": credential_envs,
            "side_effect_boundary": boundary,
        },
        "target": {
            "required": target_required,
            "env_name": target_host_env,
            "configured": bool(target_host_env),
            "present": target_present if target_required else None,
        },
        "fixtures": fixture_checks,
        "credentials": credential_checks,
        "blockers": blockers,
        "questions": _questions(blockers, mode),
        "safety": {
            "raw_secret_values_stored": False,
            "remote_target_value_stored": False,
            "credential_values_stored": False,
        },
        "legacy_profile_compatibility": legacy_profile,
        "guidance": (
            "先由 grill-me 確認 local/remote、入口、fixture、credential env 與副作用邊界；"
            "環境未 ready 前，需準備環境的 case 只能回 BLOCK，不得執行後宣稱 PASS。"
        ),
    }


def configure_environment(
    config: ProjectConfig,
    *,
    mode: str,
    entrypoint: str | None = None,
    target_host_env: str | None = None,
    fixture_paths: list[str] | None = None,
    credential_envs: list[str] | None = None,
    side_effect_boundary: str | None = None,
    confirm: bool = True,
) -> dict[str, Any]:
    selected_mode = str(mode or "").strip().lower()
    if selected_mode not in VALID_MODES:
        raise QAConfigError("invalid_execution_mode", "Environment mode must be local or remote")
    runtime = deepcopy(config.data.get("runtime")) if isinstance(config.data.get("runtime"), dict) else {}
    if entrypoint is not None:
        runtime["primary_entrypoint"] = str(entrypoint).strip()
    if target_host_env is not None:
        value = str(target_host_env).strip()
        if value and not ENV_NAME_RE.match(value):
            raise QAConfigError("invalid_target_host_env", "target-host-env must be an environment variable name")
        runtime["target_host_env"] = value
    if fixture_paths is not None:
        runtime["fixture_paths"] = [str(item).strip() for item in fixture_paths if str(item).strip()]
    if credential_envs is not None:
        values = [str(item).strip() for item in credential_envs if str(item).strip()]
        invalid = [item for item in values if not ENV_NAME_RE.match(item)]
        if invalid:
            raise QAConfigError("invalid_credential_env", "credential-env values must be environment variable names", details={"invalid": invalid})
        runtime["credential_envs"] = values
    if side_effect_boundary is not None:
        runtime["side_effect_boundary"] = str(side_effect_boundary).strip()
    runtime["execution_mode"] = selected_mode
    runtime["environment_confirmed"] = bool(confirm)
    data = deepcopy(config.data)
    data["runtime"] = runtime
    _write_config(config.path, data)
    updated = ProjectConfig(root=config.root, path=config.path, data=data, paths=config.paths)
    status = environment_profile_status(updated)
    state_path = config.paths.state / "environment-profile.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_json(status), encoding="utf-8")
    return {"status": "ok", "environment_profile": status, "config_path": str(config.path), "state_path": str(state_path)}


def _questions(blockers: list[str], mode: str) -> list[dict[str, str]]:
    questions: list[dict[str, str]] = []
    if "execution_mode" in blockers:
        questions.append({"id": "execution_mode", "prompt": "這次測試要在 local checkout、隔離測試環境，還是 remote target 執行？請選 local 或 remote。"})
    if "environment_confirmed" in blockers:
        questions.append({"id": "environment_confirmed", "prompt": "請確認上面的測試環境與副作用邊界已準備好；未確認前不會執行準備環境的 cases。"})
    if "primary_entrypoint" in blockers:
        questions.append({"id": "primary_entrypoint", "prompt": "請提供產品實際入口命令或 binary 路徑（不要提供 secret）。"})
    if "side_effect_boundary" in blockers:
        questions.append({"id": "side_effect_boundary", "prompt": "請描述可接受的測試副作用邊界，例如唯讀、sandbox、測試資料庫或可回復資源。"})
    if mode == "remote" and any(item.startswith("target_host_missing") for item in blockers):
        questions.append({"id": "target_host_env", "prompt": "請設定 target host 的 env var（只需提供變數名稱，不要貼 host/token 值），再重新執行 environment status。"})
    if any(item.startswith("fixture_missing") for item in blockers):
        questions.append({"id": "fixture_paths", "prompt": "README 指令需要的 fixture/config 路徑尚未存在；請準備檔案或修正 fixture_paths。"})
    if any(item.startswith("credential_env_missing") for item in blockers):
        questions.append({"id": "credential_envs", "prompt": "請在執行環境設定所需 credential env；工具只檢查是否存在，不會記錄值。"})
    return questions


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _write_config(path: Path, data: dict[str, Any]) -> None:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise QAConfigError("yaml_required", "PyYAML is required for environment configure") from exc
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
