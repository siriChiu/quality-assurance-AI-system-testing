from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig


RUNTIME_PROFILE_SCHEMA = "quality-pilot.runtime-profile.v1"
DEFAULT_BINARY_ENV = "QUALITY_PILOT_BINARY"
DEFAULT_TARGET_HOST_ENV = "QUALITY_PILOT_TARGET_HOST"


def runtime_profile_status(config: ProjectConfig) -> dict[str, Any]:
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    discovery = discover_runtime_surfaces(config)
    configured_entrypoint = str(runtime.get("primary_entrypoint") or "").strip()
    inferred_entrypoint = str(discovery.get("suggested_executable_path") or "").strip()
    effective_entrypoint = configured_entrypoint or inferred_entrypoint
    side_effect_boundary = str(runtime.get("side_effect_boundary") or "").strip()
    inferred_side_effect_boundary = (
        "Inferred safe boundary: parser/help/version probes only; no target, network, credentialed, or mutating operations."
        if effective_entrypoint else ""
    )
    fixture_paths = _string_list(runtime.get("fixture_paths"))
    credential_envs = _string_list(runtime.get("credential_envs"))

    missing: list[str] = []
    if not effective_entrypoint:
        missing.append("primary_entrypoint")

    status = "ready" if configured_entrypoint and side_effect_boundary else ("ready_inferred" if not missing else "needs_user_confirmation")
    questions = _runtime_questions(discovery, missing)
    return {
        "schema": RUNTIME_PROFILE_SCHEMA,
        "status": status,
        "needs_user_input": bool(missing),
        "missing_fields": missing,
        "configured": {
            "primary_entrypoint": configured_entrypoint,
            "binary_env": str(runtime.get("binary_env") or DEFAULT_BINARY_ENV),
            "target_host_env": str(runtime.get("target_host_env") or DEFAULT_TARGET_HOST_ENV),
            "fixture_paths": fixture_paths,
            "credential_envs": credential_envs,
            "side_effect_boundary": side_effect_boundary,
        },
        "inferred": {
            "primary_entrypoint": inferred_entrypoint,
            "side_effect_boundary": inferred_side_effect_boundary,
            "source": discovery.get("suggested_executable_source", ""),
            "confidence": discovery.get("suggested_executable_confidence", ""),
        },
        "effective": {
            "primary_entrypoint": effective_entrypoint,
            "binary_env": str(runtime.get("binary_env") or DEFAULT_BINARY_ENV),
            "target_host_env": str(runtime.get("target_host_env") or DEFAULT_TARGET_HOST_ENV),
            "fixture_paths": fixture_paths,
            "credential_envs": credential_envs,
            "side_effect_boundary": side_effect_boundary or inferred_side_effect_boundary,
            "source": "configured" if configured_entrypoint else ("inferred_repo_analysis" if inferred_entrypoint else ""),
        },
        "repo_analysis": discovery,
        "questions": questions,
        "guidance": (
            "AI Quality Pilot analyzes the repo first and infers the executable runtime when possible. "
            "User input is required only for genuinely missing runner, target, fixture, or credential env details."
        ),
    }


def discover_runtime_surfaces(config: ProjectConfig) -> dict[str, Any]:
    root = config.root
    pyproject = _read_text(root / "pyproject.toml", limit=12000)
    package_json = _read_text(root / "package.json", limit=12000)
    cargo_toml = _read_text(root / "Cargo.toml", limit=8000)
    readme = _first_existing_text(root, ["README.md", "README.rst", "README.txt"], limit=16000)
    go_commands = _go_cmd_packages(root)
    python_scripts = _extract_pyproject_scripts(pyproject)
    node_bins = _extract_package_bins(package_json)
    cargo_bins = _extract_cargo_bins(cargo_toml)
    readme_commands = _extract_readme_commands(readme)
    surfaces = _unique_surfaces([
        *({"kind": "go_cmd", "name": item["name"], "entrypoint": item["name"], "source": item["package"]} for item in go_commands),
        *({"kind": "python_console_script", "name": item, "entrypoint": item, "source": "pyproject.toml"} for item in python_scripts),
        *({"kind": "node_bin", "name": item, "entrypoint": item, "source": "package.json"} for item in node_bins),
        *({"kind": "cargo_bin", "name": item, "entrypoint": item, "source": "Cargo.toml"} for item in cargo_bins),
        *({"kind": "readme_command", "name": Path(item.split()[0]).name, "entrypoint": item, "source": "README"} for item in readme_commands),
    ])
    executable_candidates = _executable_candidates(root, surfaces)
    suggested_executable = executable_candidates[0] if executable_candidates else {}
    detected_profile = _detected_profile(root, readme, pyproject, package_json, cargo_toml, surfaces)
    return {
        "root": str(root),
        "detected_profile": detected_profile,
        "surface_count": len(surfaces),
        "user_visible_surfaces": surfaces[:12],
        "executable_candidates": executable_candidates[:12],
        "suggested_executable_path": suggested_executable.get("path", ""),
        "suggested_executable_source": suggested_executable.get("source", ""),
        "suggested_executable_confidence": suggested_executable.get("confidence", ""),
        "suggested_primary_entrypoint": surfaces[0]["entrypoint"] if surfaces else "",
        "has_readme": bool(readme),
        "has_pyproject": bool(pyproject),
        "has_package_json": bool(package_json),
        "has_go_mod": (root / "go.mod").exists(),
        "has_cargo_toml": bool(cargo_toml),
    }


def primary_runtime_entrypoint(config: ProjectConfig) -> str:
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    configured = str(runtime.get("primary_entrypoint") or "").strip()
    if configured:
        return configured
    discovery = discover_runtime_surfaces(config)
    return str(discovery.get("suggested_executable_path") or discovery.get("suggested_primary_entrypoint") or "").strip()


def configured_runtime_binary(config: ProjectConfig) -> str:
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), dict) else {}
    configured = str(runtime.get("primary_entrypoint") or "").strip()
    if not configured:
        return ""
    try:
        first = configured.split()[0]
    except IndexError:
        return ""
    return first


def primary_runtime_binary(config: ProjectConfig) -> str:
    entrypoint = primary_runtime_entrypoint(config)
    if not entrypoint:
        return ""
    try:
        first = entrypoint.split()[0]
    except IndexError:
        return ""
    return first


def _runtime_questions(discovery: dict[str, Any], missing: list[str]) -> list[dict[str, str]]:
    if not missing:
        return []
    surfaces = discovery.get("user_visible_surfaces") if isinstance(discovery.get("user_visible_surfaces"), list) else []
    executables = discovery.get("executable_candidates") if isinstance(discovery.get("executable_candidates"), list) else []
    surface_lines = [
        f"- {item.get('entrypoint')} ({item.get('kind')} from {item.get('source')})"
        for item in surfaces[:5]
        if isinstance(item, dict) and item.get("entrypoint")
    ] or ["- No user-facing entrypoint was confidently detected."]
    executable_lines = [
        f"- {item.get('path')} ({item.get('source')})"
        for item in executables[:5]
        if isinstance(item, dict) and item.get("path")
    ] or ["- No executable file was found under common product output paths."]
    return [
        {
            "id": "runtime_primary_entrypoint",
            "prompt": (
                "我已先分析 repo，但還沒有找到可直接執行的產品入口。\n"
                "\n"
                "已偵測到的使用者可見入口：\n"
                + "\n".join(surface_lines)
                + "\n\n已檢查的可執行檔候選：\n"
                + "\n".join(executable_lines)
                + "\n\n請只補工具無法從 repo 判斷的資訊：\n"
                "- 產品 binary / runner / API entrypoint 的實際路徑或命令\n"
                "- 若需要 build，請提供 build 後輸出路徑，不需要貼 build log\n"
                "- 若 testcase 需要帳密或 token，請只提供 env var 名稱，不要貼 raw secret\n"
                "- 若有必要 fixture/config，請提供路徑或命名規則"
            ),
        },
    ]


def _executable_candidates(root: Path, surfaces: list[dict[str, str]]) -> list[dict[str, str]]:
    names = _unique_strings([str(item.get("name") or Path(str(item.get("entrypoint") or "")).name) for item in surfaces])
    candidates: list[dict[str, str]] = []
    for name in names:
        if not name:
            continue
        for rel in _candidate_paths_for_name(name):
            path = root / rel
            if _is_executable_file(path):
                candidates.append({
                    "name": name,
                    "path": rel,
                    "source": f"matched surface `{name}`",
                    "confidence": "high",
                })
    if not candidates:
        for path in _scan_executable_files(root):
            rel = str(path.relative_to(root))
            candidates.append({
                "name": path.name,
                "path": rel,
                "source": "repo executable scan",
                "confidence": "medium",
            })
            if len(candidates) >= 12:
                break
    return _unique_candidate_paths(candidates)


def _candidate_paths_for_name(name: str) -> list[str]:
    return [
        name,
        f"./{name}",
        f"bin/{name}",
        f"build/{name}",
        f"dist/{name}",
        f"cmd/{name}/{name}",
        f"cmd/{name}/{name}.exe",
        f"target/debug/{name}",
        f"target/release/{name}",
    ]


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def _scan_executable_files(root: Path) -> list[Path]:
    ignored = {".git", ".quality-pilot", ".quality-pilot-project", "node_modules", ".venv", "venv", "__pycache__"}
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in ignored for part in rel_parts):
            continue
        if len(rel_parts) > 4:
            continue
        if path.name.endswith((".sample", ".log", ".md", ".txt", ".yaml", ".yml", ".json")):
            continue
        if _is_executable_file(path):
            out.append(path)
    return out


def _go_cmd_packages(root: Path) -> list[dict[str, str]]:
    cmd_dir = root / "cmd"
    if not cmd_dir.exists():
        return []
    out: list[dict[str, str]] = []
    for child in sorted(cmd_dir.iterdir()):
        if child.is_dir() and (child / "main.go").exists():
            out.append({"name": child.name, "package": f"./cmd/{child.name}"})
    return out


def _extract_pyproject_scripts(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    in_scripts = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_scripts = line in {"[project.scripts]", "[tool.poetry.scripts]"}
            continue
        if in_scripts and "=" in line and not line.startswith("#"):
            out.append(line.split("=", 1)[0].strip().strip('"\''))
    return out


def _extract_package_bins(text: str) -> list[str]:
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    bin_value = data.get("bin") if isinstance(data, dict) else None
    if isinstance(bin_value, str):
        name = str(data.get("name") or "").rsplit("/", 1)[-1]
        return [name] if name else []
    if isinstance(bin_value, dict):
        return [str(key) for key in bin_value.keys() if str(key).strip()]
    return []


def _extract_cargo_bins(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for match in re.finditer(r"(?m)^\s*name\s*=\s*['\"]([^'\"]+)['\"]", text):
        name = match.group(1).strip()
        if name and name not in out:
            out.append(name)
    return out[:4]


def _extract_readme_commands(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for match in re.finditer(r"`((?:\./)?[A-Za-z0-9_.-]+(?:\s+--?[A-Za-z0-9][^`]*)?)`", text):
        command = match.group(1).strip()
        if _looks_like_user_command(command):
            out.append(command)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("$ ", "> ")):
            command = stripped[2:].strip()
            if _looks_like_user_command(command):
                out.append(command)
    return _unique_strings(out)[:8]


def _looks_like_user_command(command: str) -> bool:
    try:
        first = command.split()[0]
    except IndexError:
        return False
    name = Path(first).name.lower()
    if name in {"go", "python", "python3", "pytest", "npm", "yarn", "make", "cmake", "ninja", "docker"}:
        return False
    return bool(re.fullmatch(r"\.?/?[A-Za-z0-9_.-]+", first))


def _detected_profile(root: Path, readme: str, pyproject: str, package_json: str, cargo_toml: str, surfaces: list[dict[str, str]]) -> str:
    combined = " ".join([readme, pyproject, package_json, cargo_toml]).lower()
    if any(word in combined for word in ("openapi", "swagger", "rest api", "graphql")):
        return "api"
    if any(word in combined for word in ("hardware", "device", "lab target", "bmc", "redfish", "firmware")):
        return "hardware"
    if surfaces:
        return "cli"
    if (root / "go.mod").exists() or pyproject or package_json or cargo_toml:
        return "repo"
    return "unknown"


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


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


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


def _unique_surfaces(values: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        entrypoint = str(value.get("entrypoint") or "").strip()
        if not entrypoint or entrypoint in seen:
            continue
        seen.add(entrypoint)
        out.append(value)
    return out


def _unique_candidate_paths(values: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        path = str(value.get("path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(value)
    return out
