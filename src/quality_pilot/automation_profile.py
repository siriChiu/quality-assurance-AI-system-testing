from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .runtime_profile import runtime_profile_status


AUTOMATION_PROFILE_SCHEMA = "quality-pilot.automation-profile-candidate.v1"
AUTOMATION_PROFILE_PATH = "automation-profile.candidate.json"

_PATH_FLAG_WORDS = {
    "config",
    "conf",
    "profile",
    "fixture",
    "file",
    "path",
    "input",
    "login",
    "credential",
    "credentials",
    "cert",
    "certificate",
    "keyfile",
}
_CREDENTIAL_FLAG_WORDS = {
    "user",
    "username",
    "password",
    "passwd",
    "pass",
    "token",
    "api-key",
    "apikey",
    "secret",
}
_TARGET_FLAG_WORDS = {"host", "hostname", "target", "server", "url", "endpoint", "bmc", "device", "resource"}
_MUTATING_WORDS = {
    "add",
    "apply",
    "attach",
    "boot",
    "change",
    "clear",
    "create",
    "delete",
    "disable",
    "enable",
    "erase",
    "factory",
    "flash",
    "format",
    "mount",
    "patch",
    "power",
    "post",
    "put",
    "reboot",
    "remove",
    "reset",
    "restart",
    "set",
    "start",
    "stop",
    "update",
    "upload",
    "write",
}
_READ_ONLY_WORDS = {
    "check",
    "describe",
    "diagnose",
    "dump",
    "find",
    "get",
    "inspect",
    "inventory",
    "list",
    "read",
    "show",
    "status",
    "test",
    "validate",
    "view",
}
_READINESS_FLAGS = {"-h", "--help", "help", "-v", "--version", "version"}


def build_automation_profile_candidate(
    config: ProjectConfig,
    runtime_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = runtime_profile or runtime_profile_status(config)
    runtime = profile.get("effective") if isinstance(profile.get("effective"), dict) else {}
    repo_analysis = profile.get("repo_analysis") if isinstance(profile.get("repo_analysis"), dict) else {}
    commands = _repo_command_candidates(config.root, repo_analysis)
    command_candidates = [_classify_command(command) for command in commands]
    fixtures = _fixture_candidates(command_candidates, runtime)
    credentials = _credential_candidates(command_candidates, runtime)
    targets = _target_candidates(command_candidates, runtime)
    missing_external_facts = _missing_external_facts(
        profile=profile,
        runtime=runtime,
        fixtures=fixtures,
        credentials=credentials,
        targets=targets,
    )
    questions = _questions_from_missing(missing_external_facts)
    return {
        "schema": AUTOMATION_PROFILE_SCHEMA,
        "status": "needs_external_facts" if missing_external_facts else "ready_for_candidate_generation",
        "analysis_first": True,
        "raw_secrets_allowed": False,
        "runtime": {
            "primary_entrypoint": str(runtime.get("primary_entrypoint") or ""),
            "binary_env": str(runtime.get("binary_env") or "QUALITY_PILOT_BINARY"),
            "target_host_env": str(runtime.get("target_host_env") or "QUALITY_PILOT_TARGET_HOST"),
            "side_effect_boundary": str(runtime.get("side_effect_boundary") or ""),
            "source": str(runtime.get("source") or ""),
        },
        "repo_analysis": {
            "detected_profile": repo_analysis.get("detected_profile", "unknown"),
            "surface_count": repo_analysis.get("surface_count", 0),
            "suggested_primary_entrypoint": repo_analysis.get("suggested_primary_entrypoint", ""),
            "suggested_executable_path": repo_analysis.get("suggested_executable_path", ""),
            "suggested_executable_confidence": repo_analysis.get("suggested_executable_confidence", ""),
        },
        "execution_modes": _execution_modes(runtime),
        "fixtures": fixtures,
        "credentials": credentials,
        "targets": targets,
        "command_candidates": command_candidates,
        "missing_external_fact_count": len(missing_external_facts),
        "missing_external_facts": missing_external_facts,
        "questions": questions,
        "guidance": (
            "This candidate is generated from repo/config analysis. Ask the user only for missing external facts "
            "such as env var names, lab target identifiers, fixture paths, and side-effect boundaries."
        ),
    }


def write_automation_profile_candidate(config: ProjectConfig, profile: dict[str, Any]) -> str:
    path = config.paths.state / AUTOMATION_PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(profile) + "\n", encoding="utf-8")
    return _relative_or_str(path, config.root)


def _repo_command_candidates(root: Path, repo_analysis: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    surfaces = repo_analysis.get("user_visible_surfaces") if isinstance(repo_analysis.get("user_visible_surfaces"), list) else []
    for surface in surfaces:
        if isinstance(surface, dict) and surface.get("entrypoint"):
            commands.append(str(surface["entrypoint"]))
    readme = _first_existing_text(root, ["README.md", "README.rst", "README.txt"], limit=24000)
    commands.extend(_extract_readme_commands(readme))
    entrypoint = str(repo_analysis.get("suggested_executable_path") or repo_analysis.get("suggested_primary_entrypoint") or "").strip()
    if entrypoint:
        commands.append(f"{entrypoint} --help")
    return _unique_strings(commands)[:16]


def _extract_readme_commands(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for match in re.finditer(r"`([^`\n]+)`", text):
        command = match.group(1).strip()
        if _looks_like_product_command(command):
            out.append(command)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("$ ", "> ")):
            command = stripped[2:].strip()
            if _looks_like_product_command(command):
                out.append(command)
    return _unique_strings(out)


def _looks_like_product_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    first = Path(tokens[0]).name.lower()
    if first in {"go", "python", "python3", "pytest", "npm", "yarn", "make", "cmake", "ninja", "docker"}:
        return False
    return bool(re.fullmatch(r"\.?/?[A-Za-z0-9_.-]+", tokens[0]))


def _classify_command(command: str) -> dict[str, Any]:
    tokens = _split(command)
    flags = _flags(tokens)
    words = _word_tokens(tokens)
    fixture_flags = [flag for flag in flags if _is_path_flag(flag)]
    credential_flags = [flag for flag in flags if _is_credential_flag(flag)]
    target_flags = [flag for flag in flags if _is_target_flag(flag)]
    classes: list[str] = []
    if any(token in _READINESS_FLAGS for token in tokens):
        classes.append("readiness")
    if credential_flags:
        classes.append("credentialed")
    if target_flags:
        classes.append("target_required")
    if any(word in _MUTATING_WORDS for word in words):
        classes.append("mutating")
    if any(word in _READ_ONLY_WORDS for word in words):
        classes.append("read_only")
    if not classes:
        classes.append("unknown")
    return {
        "command": command,
        "safety_classes": classes,
        "fixture_flags": fixture_flags,
        "credential_flags": credential_flags,
        "target_flags": target_flags,
        "suggested_use": _suggested_use(classes),
    }


def _fixture_candidates(command_candidates: list[dict[str, Any]], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    configured = [str(item) for item in runtime.get("fixture_paths", []) if str(item).strip()] if isinstance(runtime.get("fixture_paths"), list) else []
    by_flag: dict[str, dict[str, Any]] = {}
    for candidate in command_candidates:
        for flag in candidate.get("fixture_flags", []):
            env_name = f"QUALITY_PILOT_FIXTURE_{_env_suffix(flag)}"
            by_flag.setdefault(flag, {
                "flag": flag,
                "env_name": env_name,
                "configured_paths": configured,
                "source": "repo_command_analysis",
                "needs_user_value": not configured,
            })
    if configured and not by_flag:
        return [{"flag": "", "env_name": "", "configured_paths": configured, "source": "runtime_config", "needs_user_value": False}]
    return list(by_flag.values())


def _credential_candidates(command_candidates: list[dict[str, Any]], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    configured = [str(item) for item in runtime.get("credential_envs", []) if str(item).strip()] if isinstance(runtime.get("credential_envs"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for candidate in command_candidates:
        for flag in candidate.get("credential_flags", []):
            out.setdefault(flag, {
                "flag": flag,
                "env_name": _credential_env(flag),
                "configured_envs": configured,
                "source": "repo_command_analysis",
                "needs_user_value": not configured,
                "raw_secret_required": False,
            })
    if configured and not out:
        return [{"flag": "", "env_name": "", "configured_envs": configured, "source": "runtime_config", "needs_user_value": False, "raw_secret_required": False}]
    return list(out.values())


def _target_candidates(command_candidates: list[dict[str, Any]], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    target_env = str(runtime.get("target_host_env") or "QUALITY_PILOT_TARGET_HOST")
    out: dict[str, dict[str, Any]] = {}
    for candidate in command_candidates:
        for flag in candidate.get("target_flags", []):
            out.setdefault(flag, {
                "flag": flag,
                "env_name": target_env,
                "source": "repo_command_analysis",
                "needs_user_value": True,
            })
    return list(out.values())


def _missing_external_facts(
    *,
    profile: dict[str, Any],
    runtime: dict[str, Any],
    fixtures: list[dict[str, Any]],
    credentials: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    if profile.get("needs_user_input"):
        missing.append({
            "id": "primary_entrypoint",
            "kind": "runtime",
            "question": "產品 binary/runner/API entrypoint 的實際命令或 build 後輸出路徑。",
        })
    for item in fixtures:
        if item.get("needs_user_value"):
            missing.append({
                "id": f"fixture:{item.get('flag')}",
                "kind": "fixture",
                "question": f"{item.get('flag')} 使用的 fixture/config 檔案路徑或對應 env var `{item.get('env_name')}`。",
            })
    for item in credentials:
        if item.get("needs_user_value"):
            missing.append({
                "id": f"credential:{item.get('flag')}",
                "kind": "credential_env",
                "question": f"{item.get('flag')} 所需的 credential env var 名稱；不要提供 raw secret。",
            })
    for item in targets:
        if item.get("needs_user_value"):
            missing.append({
                "id": f"target:{item.get('flag')}",
                "kind": "target_resource",
                "question": f"{item.get('flag')} 可使用的 lab/target/resource env var 名稱，例如 `{item.get('env_name')}`。",
            })
    if not str(runtime.get("side_effect_boundary") or "").strip():
        missing.append({
            "id": "side_effect_boundary",
            "kind": "side_effect_boundary",
            "question": "哪些 operations 可以 free-hand 執行，哪些必須停在 gate 等待確認。",
        })
    return _unique_missing(missing)


def _questions_from_missing(missing: list[dict[str, str]]) -> list[dict[str, str]]:
    if not missing:
        return []
    lines = ["請只補工具無法從 repo/config 判斷的外部資訊："]
    for item in missing:
        lines.append(f"- {item['question']}")
    lines.extend([
        "",
        "請不要貼 raw secret；credential 只提供 env var 名稱。",
        "若某項目前不允許自動執行，請寫明 side-effect boundary 或標記為 gated。",
    ])
    return [{"id": "automation_profile_external_facts", "prompt": "\n".join(lines)}]


def _execution_modes(runtime: dict[str, Any]) -> list[dict[str, str]]:
    has_entrypoint = bool(str(runtime.get("primary_entrypoint") or "").strip())
    return [
        {"id": "parser_readiness", "status": "allowed" if has_entrypoint else "needs_entrypoint", "boundary": "help/version/parser-only probes"},
        {"id": "local_readonly", "status": "candidate" if has_entrypoint else "needs_entrypoint", "boundary": "local read-only product commands with no target or credentials"},
        {"id": "target_readonly", "status": "gated", "boundary": "target/lab read-only commands require configured target env"},
        {"id": "mutating", "status": "blocked_by_default", "boundary": "state-changing commands require explicit gate"},
    ]


def _suggested_use(classes: list[str]) -> str:
    if "mutating" in classes:
        return "gate_required"
    if "credentialed" in classes or "target_required" in classes:
        return "needs_external_fact"
    if "readiness" in classes:
        return "readiness_only"
    if "read_only" in classes:
        return "candidate_test_command"
    return "review_required"


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _flags(tokens: list[str]) -> list[str]:
    out: list[str] = []
    for token in tokens:
        if token.startswith("--") and len(token) > 2:
            out.append(token.split("=", 1)[0])
        elif token.startswith("-") and len(token) > 1:
            out.append(token)
    return _unique_strings(out)


def _word_tokens(tokens: list[str]) -> list[str]:
    words: list[str] = []
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        for part in re.split(r"[^A-Za-z0-9]+", token.lower()):
            if part:
                words.append(part)
    return words


def _is_path_flag(flag: str) -> bool:
    normalized = flag.lstrip("-").lower().replace("_", "-")
    parts = set(re.split(r"[-.]", normalized))
    return normalized in _PATH_FLAG_WORDS or bool(parts & _PATH_FLAG_WORDS)


def _is_credential_flag(flag: str) -> bool:
    normalized = flag.lstrip("-").lower().replace("_", "-")
    parts = set(re.split(r"[-.]", normalized))
    return normalized in _CREDENTIAL_FLAG_WORDS or bool(parts & _CREDENTIAL_FLAG_WORDS)


def _is_target_flag(flag: str) -> bool:
    normalized = flag.lstrip("-").lower().replace("_", "-")
    parts = set(re.split(r"[-.]", normalized))
    return normalized in _TARGET_FLAG_WORDS or bool(parts & _TARGET_FLAG_WORDS)


def _env_suffix(flag: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", flag.lstrip("-")).strip("_").upper()
    return suffix or "PATH"


def _credential_env(flag: str) -> str:
    suffix = _env_suffix(flag)
    if "PASS" in suffix:
        return "QUALITY_PILOT_TEST_PASSWORD"
    if "USER" in suffix:
        return "QUALITY_PILOT_TEST_USER"
    if "TOKEN" in suffix:
        return "QUALITY_PILOT_TEST_TOKEN"
    if "API_KEY" in suffix or "APIKEY" in suffix:
        return "QUALITY_PILOT_TEST_API_KEY"
    return f"QUALITY_PILOT_SECRET_{suffix}"


def _read_text(path: Path, *, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _first_existing_text(root: Path, names: list[str], *, limit: int) -> str:
    for name in names:
        text = _read_text(root / name, limit=limit)
        if text:
            return text
    return ""


def _unique_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", str(value).strip())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _unique_missing(values: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        key = str(value.get("id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
