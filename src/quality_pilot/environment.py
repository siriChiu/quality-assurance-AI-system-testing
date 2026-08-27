from __future__ import annotations

"""Explicit execution-environment profile and preflight checks.

The repo can tell us a great deal about *what* might be executable, but it
cannot safely decide whether a case should run against the checkout, a lab,
or a remote target.  This module keeps that decision in the host project's
config and exposes only redacted readiness facts (never secret values).
"""

import json
import os
import re
import shlex
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import ENV_NAME_RE, ProjectConfig, QAConfigError
from .security import ensure_safe_text, redact_structure


ENVIRONMENT_PROFILE_SCHEMA = "quality-pilot.environment-profile.v1"
VALID_MODES = {"local", "remote"}


def environment_profile_status(config: ProjectConfig) -> dict[str, Any]:
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    legacy_profile = "execution_mode" not in runtime and "environment_confirmed" not in runtime
    configured_mode = str(runtime.get("execution_mode") or "").strip().lower()
    remote_config = runtime.get("remote") if isinstance(runtime.get("remote"), dict) else {}
    # Treat explicit remote coordinates as remote even when an older config has
    # a stale/blank execution_mode.  Never validate those paths locally.
    remote_coordinates = any(runtime.get(key) for key in ("ssh_host", "host", "remote_repo", "repo", "remote_python", "python", "remote_fixture_paths", "fixture", "fixture_paths")) or any(remote_config.get(key) for key in ("ssh_host", "remote_repo", "repo", "remote_python", "python"))
    mode = configured_mode or ("remote" if remote_coordinates else ("local" if legacy_profile else ""))
    confirmed = bool(runtime.get("environment_confirmed")) or legacy_profile
    target_host_env = str(runtime.get("target_host_env") or "QUALITY_PILOT_TARGET_HOST").strip()
    authentication = runtime.get("authentication") if isinstance(runtime.get("authentication"), dict) else {}
    auth_method = str(authentication.get("auth_method") or ("ssh_agent" if mode == "remote" and (runtime.get("ssh_host") or runtime.get("host") or remote_config.get("ssh_host")) else "env_credentials")).strip().lower()
    credential_envs = _string_list(authentication.get("credential_envs")) or _string_list(runtime.get("credential_envs"))
    fixture_paths = _string_list(runtime.get("remote_fixture_paths")) if mode == "remote" and runtime.get("remote_fixture_paths") else _string_list(runtime.get("fixture_paths"))
    remote_repo = str(remote_config.get("remote_repo") or remote_config.get("repo") or runtime.get("remote_repo") or runtime.get("repo") or "").strip()
    remote_python = str(remote_config.get("remote_python") or remote_config.get("python") or runtime.get("remote_python") or runtime.get("python") or "").strip()
    remote_fixture_paths = _string_list(runtime.get("remote_fixture_paths") or runtime.get("fixture"))
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
        if mode == "remote":
            fixture_checks.append({"path": value, "scope": "remote", "exists": None, "verification": "remote_preflight_required"})
        else:
            path = _resolve_path(config.root, value)
            exists = path.exists()
            fixture_checks.append({"path": value, "scope": "local", "exists": exists})
            if not exists:
                blockers.append(f"fixture_missing:{value}")
    preflight = runtime.get("remote_preflight") if isinstance(runtime.get("remote_preflight"), dict) else None
    if mode == "remote":
        if not preflight:
            blockers.append("REMOTE_PREFLIGHT_REQUIRED")
        elif str(preflight.get("status") or "") != "READY":
            blockers.append(f"remote_preflight:{str(preflight.get('status') or 'UNKNOWN')}")
        elif not _remote_preflight_is_fresh(preflight, runtime):
            blockers.append("remote_preflight:STALE")
        if preflight:
            source_identity = preflight.get("source_identity") if isinstance(preflight.get("source_identity"), dict) else {}
            if str(source_identity.get("status") or "") == "MISMATCH":
                blockers.append("REMOTE_SOURCE_MISMATCH")
            elif str(source_identity.get("status") or "") == "DIRTY":
                blockers.append("REMOTE_SOURCE_DIRTY")
            elif str(preflight.get("expected_head_sha") or "") and str(source_identity.get("status") or "") != "VERIFIED":
                blockers.append("REMOTE_SOURCE_UNVERIFIED")
    credential_checks = []
    for name in credential_envs:
        valid_name = bool(ENV_NAME_RE.match(name))
        present = bool(valid_name and os.environ.get(name))
        credential_checks.append({"name": name, "valid_name": valid_name, "present": present})
        if not valid_name:
            blockers.append(f"credential_env_invalid:{name}")
        elif auth_method != "ssh_agent" and not present:
            blockers.append(f"credential_env_missing:{name}")

    target_required = mode == "remote"
    ssh_host_value = str(remote_config.get("ssh_host") or runtime.get("ssh_host") or runtime.get("host") or "").strip()
    target_present = bool(ssh_host_value or (ENV_NAME_RE.match(target_host_env) and os.environ.get(target_host_env)))
    if target_required and ssh_host_value:
        target_host_env = "ssh_host_alias"
    if target_required and not runtime.get("ssh_host") and not ENV_NAME_RE.match(target_host_env):
        blockers.append("target_host_env_invalid")
    elif target_required and not target_present:
        blockers.append(f"target_host_missing:{target_host_env}")

    return {
        "schema": ENVIRONMENT_PROFILE_SCHEMA,
        "status": _environment_status(blockers, mode),
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
            "authentication": {"transport": str(authentication.get("transport") or "local"), "auth_method": auth_method, "credential_envs": credential_envs},
            "ssh_host": ssh_host_value,
            "remote_repo": remote_repo,
            "remote_python": remote_python,
            "remote_fixture_paths": remote_fixture_paths,
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
        "remote_preflight": runtime.get("remote_preflight") if isinstance(runtime.get("remote_preflight"), dict) else None,
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
    ssh_host: str | None = None,
    remote_repo: str | None = None,
    remote_python: str | None = None,
    remote_fixture_paths: list[str] | None = None,
    auth_method: str | None = None,
) -> dict[str, Any]:
    selected_mode = str(mode or "").strip().lower()
    if selected_mode not in VALID_MODES:
        raise QAConfigError("invalid_execution_mode", "Environment mode must be local or remote")
    runtime = deepcopy(config.data.get("runtime")) if isinstance(config.data.get("runtime"), dict) else {}
    if entrypoint is not None:
        selected_entrypoint = str(entrypoint).strip()
        try:
            ensure_safe_text(selected_entrypoint, context="runtime primary entrypoint")
        except ValueError as exc:
            raise QAConfigError("entrypoint_contains_secret", "The product entrypoint contains secret-like material; use environment references only") from exc
        runtime["primary_entrypoint"] = selected_entrypoint
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
    execution = runtime.get("execution") if isinstance(runtime.get("execution"), dict) else {}
    if selected_mode == "remote":
        execution.setdefault("product_target", "remote_ssh")
        execution.setdefault("playwright_target", "local_via_ssh_tunnel")
    else:
        execution.setdefault("product_target", "local")
        execution.setdefault("playwright_target", "local")
    runtime["execution"] = execution
    if ssh_host is not None:
        runtime["ssh_host"] = str(ssh_host).strip()
    if remote_repo is not None:
        runtime["remote_repo"] = str(remote_repo).strip()
    if remote_python is not None:
        runtime["remote_python"] = str(remote_python).strip()
    if remote_fixture_paths is not None:
        runtime["remote_fixture_paths"] = [str(item).strip() for item in remote_fixture_paths if str(item).strip()]
    if auth_method is not None:
        if auth_method not in {"ssh_agent", "env_credentials"}:
            raise QAConfigError("invalid_auth_method", "auth_method must be ssh_agent or env_credentials")
        runtime["authentication"] = {"transport": "ssh", "auth_method": auth_method, "credential_envs": credential_envs}
    data = deepcopy(config.data)
    data["runtime"] = runtime
    _write_config(config.path, data)
    updated = ProjectConfig(root=config.root, path=config.path, data=data, paths=config.paths)
    status = environment_profile_status(updated)
    state_path = config.paths.state / "environment-profile.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_json(status), encoding="utf-8")
    return {"status": "ok", "environment_profile": status, "config_path": str(config.path), "state_path": str(state_path)}


def _remote_preflight_is_fresh(preflight: Mapping[str, Any], runtime: Mapping[str, Any]) -> bool:
    created_at = str(preflight.get("created_at") or "").strip()
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    ttl = int(runtime.get("remote_preflight_ttl_seconds") or 3600)
    return (datetime.now(timezone.utc) - created).total_seconds() <= max(1, ttl)


def _environment_status(blockers: list[str], mode: str) -> str:
    if not blockers:
        return "ready"
    if "REMOTE_PREFLIGHT_REQUIRED" in blockers or "remote_preflight:STALE" in blockers:
        return "REMOTE_PREFLIGHT_REQUIRED"
    if "REMOTE_SOURCE_MISMATCH" in blockers:
        return "REMOTE_SOURCE_MISMATCH"
    if "REMOTE_SOURCE_DIRTY" in blockers:
        return "REMOTE_SOURCE_DIRTY"
    if "REMOTE_SOURCE_UNVERIFIED" in blockers:
        return "REMOTE_SOURCE_UNVERIFIED"
    if any(item == "remote_preflight:TOOLING_FAIL" for item in blockers):
        return "TOOLING_FAIL"
    if any(item.startswith("remote_preflight:") for item in blockers):
        return "INFRASTRUCTURE_BLOCK"
    return "needs_user_input"


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
    if "REMOTE_PREFLIGHT_REQUIRED" in blockers:
        questions.append({"id": "remote_preflight", "prompt": "Remote 路徑已被辨識；請執行 /quality-pilot environment preflight，工具會透過 SSH 驗證，不需手寫 fixture 的本機路徑。"})
    if any(item.startswith("fixture_missing") for item in blockers):
        questions.append({"id": "fixture_paths", "prompt": "README 指令需要的 fixture/config 路徑尚未存在；請準備檔案或修正 fixture_paths。"})
    if any(item.startswith("credential_env_missing") for item in blockers):
        questions.append({"id": "credential_envs", "prompt": "請在執行環境設定所需 credential env；工具只檢查是否存在，不會記錄值。"})
    return questions


def remote_preflight(config: ProjectConfig, *, expected_head_sha: str | None = None) -> dict[str, Any]:
    """Run redacted, independent SSH checks for a remote product target.

    A non-zero remote check is an infrastructure result.  Python/SSH launcher
    errors are TOOLING_FAIL.  The function never stores command output.
    """
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    remote_config = runtime.get("remote") if isinstance(runtime.get("remote"), dict) else {}
    host_env = str(runtime.get("target_host_env") or "QUALITY_PILOT_TARGET_HOST")
    host = str(remote_config.get("ssh_host") or runtime.get("ssh_host") or runtime.get("host") or os.environ.get(host_env, "")).strip()
    repo = str(remote_config.get("remote_repo") or remote_config.get("repo") or runtime.get("remote_repo") or runtime.get("repo") or "").strip()
    python_path = str(remote_config.get("remote_python") or remote_config.get("python") or runtime.get("remote_python") or runtime.get("python") or "").strip()
    fixtures = _string_list(runtime.get("remote_fixture_paths") or runtime.get("fixture")) or _string_list(runtime.get("fixture_paths"))
    remote_expected_head = str(expected_head_sha or runtime.get("expected_head_sha") or "").strip()
    evidence_dir = config.paths.state / "remote-preflight"
    evidence_path = evidence_dir / "remote_preflight_evidence.json"
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    base = {
        "schema": "quality-pilot.remote-preflight.v1",
        "target_alias": host,
        "repo_path": repo,
        "python_path": python_path,
        "fixture_paths": fixtures,
        "expected_head_sha": remote_expected_head,
        "created_at": created_at,
        "raw_output_stored": False,
    }
    if not host or not repo or not python_path:
        result = {**base, "status": "REMOTE_PREFLIGHT_REQUIRED", "ssh": "NOT_RUN", "checks": {}, "source_identity": {"status": "UNVERIFIED"}, "reason": "remote_target_or_paths_missing"}
        return _persist_remote_preflight(config, runtime, result, evidence_path)
    if not re.fullmatch(r"[A-Za-z0-9_.:@:/-]+", host):
        result = {**base, "status": "TOOLING_FAIL", "ssh": "NOT_RUN", "checks": {}, "source_identity": {"status": "UNVERIFIED"}, "reason": "unsafe_ssh_host"}
        return _persist_remote_preflight(config, runtime, result, evidence_path)

    check_specs: list[tuple[str, str]] = [
        ("remote_repo", "test -d " + shlex.quote(repo)),
        ("remote_python", "test -x " + shlex.quote(python_path)),
    ]
    check_specs.extend((f"remote_fixture_{index}", "test -f " + shlex.quote(value)) for index, value in enumerate(fixtures, start=1))
    check_specs.extend([
        ("remote_python_import", shlex.quote(python_path) + " -c " + shlex.quote("import sys; print(sys.version_info[:2])")),
        ("remote_playwright_import", shlex.quote(python_path) + " -c " + shlex.quote("import playwright")),
        ("remote_chromium", shlex.quote(python_path) + " -c " + shlex.quote("from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); path=Path(p.chromium.executable_path); p.stop(); assert path.is_file() and path.stat().st_mode & 0o111")),
        ("remote_process_launcher", "command -v setsid"),
        ("remote_requirements_requirements_txt", "test -f " + shlex.quote(repo + "/requirements.txt")),
        ("remote_requirements_pyproject", "test -f " + shlex.quote(repo + "/pyproject.toml")),
        ("remote_source_commit", "git -C " + shlex.quote(repo) + " rev-parse HEAD"),
        ("remote_source_dirty", "test -z \"$(git -C " + shlex.quote(repo) + " status --porcelain)\""),
    ])
    results: dict[str, str] = {}
    outputs: dict[str, str] = {}
    tooling_failure = False
    ssh_failure = False
    try:
        transport = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "true"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        results["ssh"] = "PASS" if transport.returncode == 0 else "FAIL"
        if transport.returncode != 0:
            ssh_failure = True
        if not ssh_failure:
            for name, command in check_specs:
                completed = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                results[name] = "PASS" if completed.returncode == 0 else "FAIL"
                if name == "remote_source_commit" and completed.returncode == 0:
                    observed = (completed.stdout or "").strip().splitlines()[-1:] or [""]
                    outputs["remote_source_commit"] = observed[0] if re.fullmatch(r"[0-9a-fA-F]{7,64}", observed[0]) else "UNAVAILABLE"
    except (OSError, subprocess.TimeoutExpired):
        tooling_failure = True

    req_status = "PASS" if results.get("remote_requirements_requirements_txt") == "PASS" or results.get("remote_requirements_pyproject") == "PASS" else "FAIL"
    results["remote_requirements"] = req_status
    observed_head = outputs.get("remote_source_commit", "")
    source_dirty = results.get("remote_source_dirty")
    if source_dirty != "PASS":
        source_status = "DIRTY"
    elif remote_expected_head and observed_head != remote_expected_head:
        source_status = "MISMATCH"
    elif not remote_expected_head or not observed_head:
        source_status = "UNVERIFIED"
    else:
        source_status = "VERIFIED"
    source_identity = {"status": source_status, "expected_head_sha": remote_expected_head, "observed_head_sha": observed_head or None, "dirty": source_dirty}
    results["remote_source_commit"] = "PASS" if observed_head else results.get("remote_source_commit", "FAIL")
    results["remote_source_dirty"] = "PASS" if source_dirty == "PASS" else results.get("remote_source_dirty", "FAIL")
    required_result_values = [
        value for name, value in results.items()
        if name not in {"remote_requirements_requirements_txt", "remote_requirements_pyproject"}
    ]
    if tooling_failure:
        status = "TOOLING_FAIL"
    elif ssh_failure:
        status = "INFRASTRUCTURE_BLOCK"
    elif any(value == "FAIL" for value in required_result_values) or req_status != "PASS":
        status = "INFRASTRUCTURE_BLOCK"
    elif source_status == "MISMATCH":
        status = "REMOTE_SOURCE_MISMATCH"
    elif source_status == "DIRTY":
        status = "REMOTE_SOURCE_DIRTY"
    elif remote_expected_head and source_status != "VERIFIED":
        status = "REMOTE_SOURCE_UNVERIFIED"
    else:
        status = "READY"
    result = {**base, "status": status, "ssh": results.get("ssh", "FAIL"), "checks": results, "source_identity": source_identity}
    return _persist_remote_preflight(config, runtime, result, evidence_path)


def _persist_remote_preflight(config: ProjectConfig, runtime: dict[str, Any], result: dict[str, Any], evidence_path: Path) -> dict[str, Any]:
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    result["evidence_path"] = str(evidence_path.relative_to(config.root)) if evidence_path.is_relative_to(config.root) else str(evidence_path)
    redacted, _ = redact_structure(result, prefix="remote_preflight")
    evidence_path.write_text(json.dumps(redacted, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    runtime["remote_preflight"] = result
    data = deepcopy(config.data)
    data["runtime"] = runtime
    _write_config(config.path, data)
    return result


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
