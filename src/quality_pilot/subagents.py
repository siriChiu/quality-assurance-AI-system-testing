from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import config as config_module
from .config import ProjectConfig, QAConfigError, load_yaml

DEFAULT_SUBAGENT_PROFILE = "open-webui"
DEFAULT_SUBAGENT_PROVIDER = "open_webui"
DEFAULT_OPEN_WEBUI_ENDPOINT = "https://172.17.20.220/"
DEFAULT_OPEN_WEBUI_API_KEY_ENV = "OPEN_WEBUI_API_KEY"
SUBAGENT_TASKS = [
    "gitea_issue_body",
    "pull_request_body",
    "wiki_status_summary",
    "case_candidate_analysis",
    "redmine_issue_summary",
    "reviewer_notes",
]
USER_OWNED_PROFILE_FIELDS = ["model"]
OPTIONAL_PROFILE_FIELDS = ["api_key_env", "api_base"]


class SubagentConfigError(RuntimeError):
    pass


def default_subagent_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "default_profile": DEFAULT_SUBAGENT_PROFILE,
        "profiles": {
            DEFAULT_SUBAGENT_PROFILE: {
                "provider": DEFAULT_SUBAGENT_PROVIDER,
                "endpoint": DEFAULT_OPEN_WEBUI_ENDPOINT,
                "model": "",
                "api_base": "",
                "api_key_env": "",
            }
        },
        "text_generation": {
            "mode": "subagent_handoff",
            "review_required": True,
            "tasks": {task: DEFAULT_SUBAGENT_PROFILE for task in SUBAGENT_TASKS},
            "task_prompts": {},
        },
    }


def merged_subagent_config(config: ProjectConfig) -> dict[str, Any]:
    defaults = default_subagent_config()
    raw = config.data.get("subagents")
    if not isinstance(raw, dict):
        return defaults
    return _deep_merge(defaults, raw)


def subagent_status(config: ProjectConfig) -> dict[str, Any]:
    configured = isinstance(config.data.get("subagents"), dict)
    settings = merged_subagent_config(config)
    profile_name = str(settings.get("default_profile") or DEFAULT_SUBAGENT_PROFILE)
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    profile = profiles.get(profile_name) if isinstance(profiles.get(profile_name), dict) else {}
    endpoint = str(profile.get("endpoint") or "")
    provider = str(profile.get("provider") or "")
    resolved_model, model_source = _resolved_model(profile)
    missing_user_fields = [] if resolved_model else ["model"]
    task_prompts = settings.get("text_generation", {}).get("task_prompts") if isinstance(settings.get("text_generation"), dict) else {}
    configured_task_prompts = sorted(task for task in SUBAGENT_TASKS if str((task_prompts or {}).get(task) or "").strip())
    api_key_env = str(profile.get("api_key_env") or "")
    api_base = str(profile.get("api_base") or "")
    checks = [
        {
            "name": "subagent.config",
            "status": "PASS" if configured else "WARN",
            "message": "Subagent config exists." if configured else "Run /quality-pilot subagent configure to write the default Open WebUI profile.",
        },
        {
            "name": "subagent.default_profile",
            "status": "PASS" if profile else "FAIL",
            "profile": profile_name,
        },
        {
            "name": "subagent.endpoint",
            "status": "PASS" if endpoint else "WARN",
            "provider": provider or DEFAULT_SUBAGENT_PROVIDER,
            "endpoint": endpoint or DEFAULT_OPEN_WEBUI_ENDPOINT,
        },
        {
            "name": "subagent.user_content",
            "status": "WARN" if missing_user_fields else "PASS",
            "message": (
                "Open WebUI model is missing. Paste an endpoint with ?model=<name> or set subagents.profiles.open-webui.model."
                if missing_user_fields
                else "Open WebUI endpoint/model are configured; task prompts are optional overrides."
            ),
            "missing_profile_fields": missing_user_fields,
            "missing_task_prompts": [],
            "configured_task_prompts": configured_task_prompts,
        },
    ]
    return {
        "status": "configured" if configured else "default_available",
        "configured": configured,
        "enabled": bool(settings.get("enabled", True)),
        "default_profile": profile_name,
        "provider": provider or DEFAULT_SUBAGENT_PROVIDER,
        "endpoint": endpoint or DEFAULT_OPEN_WEBUI_ENDPOINT,
        "model": resolved_model,
        "model_source": model_source,
        "api_base": api_base,
        "api_key_env": api_key_env,
        "mode": str(settings.get("text_generation", {}).get("mode") or "subagent_handoff") if isinstance(settings.get("text_generation"), dict) else "subagent_handoff",
        "tasks": list(SUBAGENT_TASKS),
        "review_required": bool(settings.get("text_generation", {}).get("review_required", True)) if isinstance(settings.get("text_generation"), dict) else True,
        "user_owned_fields": USER_OWNED_PROFILE_FIELDS,
        "optional_profile_fields": OPTIONAL_PROFILE_FIELDS,
        "missing_user_fields": missing_user_fields,
        "missing_task_prompts": [],
        "configured_task_prompts": configured_task_prompts,
        "checks": checks,
    }


def configure_subagent(
    config: ProjectConfig,
    *,
    profile: str = DEFAULT_SUBAGENT_PROFILE,
    provider: str = DEFAULT_SUBAGENT_PROVIDER,
    endpoint: str | None = None,
    model: str | None = None,
    api_key_env: str | None = None,
    api_base: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if config_module.yaml is None:
        raise SubagentConfigError("PyYAML is required to update .quality-pilot.yaml")
    path = config.path
    data = load_yaml(path)
    before = deepcopy(data.get("subagents")) if isinstance(data.get("subagents"), dict) else None
    subagents = data.get("subagents") if isinstance(data.get("subagents"), dict) else {}
    defaults = default_subagent_config()
    merged = _deep_merge(defaults, subagents)
    merged["enabled"] = True
    merged["default_profile"] = profile
    profiles = merged.setdefault("profiles", {})
    existing_profile = profiles.get(profile) if isinstance(profiles.get(profile), dict) else {}
    configured_endpoint = DEFAULT_OPEN_WEBUI_ENDPOINT if force else str(existing_profile.get("endpoint") or DEFAULT_OPEN_WEBUI_ENDPOINT)
    configured_model = "" if force else str(existing_profile.get("model") or "")
    configured_api_key_env = "" if force else str(existing_profile.get("api_key_env") or "")
    configured_api_base = "" if force else str(existing_profile.get("api_base") or "")
    if endpoint is not None:
        configured_endpoint = endpoint
    if model is not None:
        configured_model = model
    if api_key_env is not None:
        configured_api_key_env = api_key_env
    if api_base is not None:
        configured_api_base = api_base
    profiles[profile] = {
        "provider": provider,
        "endpoint": configured_endpoint,
        "model": configured_model,
        "api_base": configured_api_base,
        "api_key_env": configured_api_key_env,
    }
    text_generation = merged.setdefault("text_generation", {})
    text_generation["mode"] = "subagent_handoff"
    text_generation["review_required"] = True
    text_generation["tasks"] = {task: profile for task in SUBAGENT_TASKS}
    prompts = text_generation.get("task_prompts") if isinstance(text_generation.get("task_prompts"), dict) else {}
    text_generation["task_prompts"] = {} if force else {task: str(value) for task, value in prompts.items() if str(value).strip()}
    data["subagents"] = merged
    path.write_text(config_module.yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    updated_config = ProjectConfig(root=config.root, path=config.path, data=data, paths=config.paths)
    return {
        "status": "ok",
        "path": _relative_or_str(path, config.root),
        "changed": before != data.get("subagents"),
        "subagents": subagent_status(updated_config),
        "message": "Subagent profile configured; provide either endpoint ?model=<name> or the separate model field when ready.",
    }


def text_generation_handoff(config: ProjectConfig, task: str) -> dict[str, Any]:
    settings = merged_subagent_config(config)
    text_generation = settings.get("text_generation") if isinstance(settings.get("text_generation"), dict) else {}
    tasks = text_generation.get("tasks") if isinstance(text_generation.get("tasks"), dict) else {}
    profile_name = str(tasks.get(task) or settings.get("default_profile") or DEFAULT_SUBAGENT_PROFILE)
    profiles = settings.get("profiles") if isinstance(settings.get("profiles"), dict) else {}
    profile = profiles.get(profile_name) if isinstance(profiles.get(profile_name), dict) else {}
    task_prompts = text_generation.get("task_prompts") if isinstance(text_generation.get("task_prompts"), dict) else {}
    endpoint = str(profile.get("endpoint") or DEFAULT_OPEN_WEBUI_ENDPOINT)
    resolved_model, model_source = _resolved_model(profile)
    return {
        "mode": str(text_generation.get("mode") or "subagent_handoff"),
        "task": task,
        "profile": profile_name,
        "provider": str(profile.get("provider") or DEFAULT_SUBAGENT_PROVIDER),
        "endpoint": endpoint,
        "model": resolved_model,
        "model_source": model_source,
        "api_base": str(profile.get("api_base") or ""),
        "api_key_env": str(profile.get("api_key_env") or ""),
        "candidate_only": True,
        "review_required": bool(text_generation.get("review_required", True)),
        "user_content_required": [] if resolved_model else ["model"],
        "task_prompt_required": False,
        "task_prompt_configured": bool(str(task_prompts.get(task) or "").strip()),
        "write_policy": "Subagent may draft candidate text only; AI Quality Pilot validates and performs any gated write.",
    }


def _resolved_model(profile: dict[str, Any]) -> tuple[str, str]:
    explicit = str(profile.get("model") or "").strip()
    if explicit:
        return explicit, "profile"
    endpoint_model = _model_from_endpoint(str(profile.get("endpoint") or ""))
    if endpoint_model:
        return endpoint_model, "endpoint_query"
    return "", "missing"


def _model_from_endpoint(endpoint: str) -> str:
    if not endpoint:
        return ""
    try:
        values = parse_qs(urlsplit(endpoint).query)
    except ValueError:
        return ""
    for key in ("model", "models"):
        raw = values.get(key)
        if raw and str(raw[0]).strip():
            return str(raw[0]).strip()
    return ""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
