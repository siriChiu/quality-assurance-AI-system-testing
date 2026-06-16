from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in minimal installs
    yaml = None

DEFAULT_PROJECT_WORKSPACE = ".qa-aist-project"
LEGACY_PROJECT_WORKSPACE = ".qa-aist"
CONFIG_FILE = ".qa-aist.yaml"

REQUIRED_CONFIG_PATHS = ["workspace", "cases", "runners", "rules", "state", "evidence", "reports"]
OPTIONAL_CONFIG_PATHS = ["issues"]
SECRET_KEY_RE = re.compile(r"(token|password|passwd|secret|api[_-]?key)", re.IGNORECASE)
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class QAConfigError(ValueError):
    def __init__(self, error: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    config: Path
    workspace: Path
    cases: Path
    runners: Path
    rules: Path
    issues: Path
    state: Path
    evidence: Path
    reports: Path

    def as_dict(self) -> dict[str, Path]:
        return {
            "root": self.root,
            "config": self.config,
            "workspace": self.workspace,
            "cases": self.cases,
            "runners": self.runners,
            "rules": self.rules,
            "issues": self.issues,
            "state": self.state,
            "evidence": self.evidence,
            "reports": self.reports,
        }


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    path: Path
    data: dict[str, Any]
    paths: ProjectPaths


def default_config(
    workspace: str = DEFAULT_PROJECT_WORKSPACE,
    *,
    project_name: str = "example-project",
    default_branch: str = "main",
    tracker_provider: str = "hermes_mcp",
    gitea_backend: str = "http",
    gitea_base_url: str = "",
    gitea_repo: str = "",
    gitea_token_env: str = "",
) -> str:
    _ = (gitea_backend, gitea_base_url, gitea_repo, gitea_token_env)
    return f"""# QA-AIST project configuration
# This file belongs to the host project, not to the QA-AIST tool repository.
project:
  name: {_yaml_string(project_name)}
  default_branch: {_yaml_string(default_branch)}

paths:
  workspace: {workspace}
  cases: {workspace}/cases
  runners: {workspace}/runners
  rules: {workspace}/rules
  issues: {workspace}/issues
  state: {workspace}/state
  evidence: {workspace}/evidence
  reports: {workspace}/reports

tracker:
  provider: {tracker_provider}
  wiki_page: "Test status (Siri)"
  mcp:
    required_servers:
      - gitea
      - redmine
    status_json: {workspace}/state/hermes-mcp/status.json
    gitea_issues_json: {workspace}/state/gitea-mcp/issues.json
    redmine_issues_json: {workspace}/state/redmine-mcp/issues.json
    wiki_write_request_json: {workspace}/state/gitea-mcp/wiki-write-request.json
    wiki_write_result_json: {workspace}/state/gitea-mcp/wiki-write-result.json

policy:
  deterministic_first: true
  require_write_gate: true
  auto_publish_wiki: true
  prohibit_closed_issue_comments: true
  prohibit_raw_secrets_in_repo: true
  require_swqa_pattern_expansion: true
  require_sibling_surface_scan: true
  require_boundary_invalid_tests: true
  require_side_effect_safe_repro: true
"""


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def workspace_path(root: Path, workspace: str | Path = DEFAULT_PROJECT_WORKSPACE) -> Path:
    requested = Path(workspace)
    if requested.is_absolute():
        return requested.resolve()
    return (root / requested).resolve()


def project_paths(root: Path, workspace: str | Path = DEFAULT_PROJECT_WORKSPACE) -> ProjectPaths:
    root = root.resolve()
    resolved_workspace = workspace_path(root, workspace)
    return ProjectPaths(
        root=root,
        config=root / CONFIG_FILE,
        workspace=resolved_workspace,
        cases=resolved_workspace / "cases",
        runners=resolved_workspace / "runners",
        rules=resolved_workspace / "rules",
        issues=resolved_workspace / "issues",
        state=resolved_workspace / "state",
        evidence=resolved_workspace / "evidence",
        reports=resolved_workspace / "reports",
    )


def is_qa_aist_source_checkout(path: Path) -> bool:
    if not path.is_dir():
        return False
    package_dir = path / "src" / "qa_aist"
    pyproject = path / "pyproject.toml"
    if package_dir.is_dir() and pyproject.exists():
        try:
            if 'name = "qa-aist"' in pyproject.read_text(encoding="utf-8"):
                return True
        except OSError:
            return False
    return (package_dir / "cli.py").exists() and (path / "docs" / "PROJECT_BOUNDARY.md").exists()


def write_if_missing(path: Path, content: str, *, executable: bool = False, force: bool = False) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o111)
    return True


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise QAConfigError("config_not_found", f"Config not found: {path}", details={"path": str(path)})
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(text) or {}
    else:
        loaded = _load_simple_yaml(text)
    if not isinstance(loaded, dict):
        raise QAConfigError("config_not_mapping", "Config root must be a mapping")
    return loaded


def _load_simple_yaml(text: str) -> dict[str, Any]:
    """Small fallback parser for QA-AIST's generated config shape."""
    data: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not raw.startswith(" ") and line.endswith(":"):
            key = line[:-1].strip()
            current = {}
            data[key] = current
            continue
        if current is not None and raw.startswith("  ") and ":" in line:
            key, value = line.strip().split(":", 1)
            current[key.strip()] = _parse_scalar(value.strip())
    return data


def _parse_scalar(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def validate_config_data(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    project = data.get("project")
    paths = data.get("paths")
    tracker = data.get("tracker")
    policy = data.get("policy")
    if not isinstance(project, dict):
        errors.append("missing project section")
    else:
        for key in ["name", "default_branch"]:
            if not project.get(key):
                errors.append(f"missing project.{key}")
    if not isinstance(paths, dict):
        errors.append("missing paths section")
    else:
        for key in REQUIRED_CONFIG_PATHS:
            if not paths.get(key):
                errors.append(f"missing paths.{key}")
    if not isinstance(tracker, dict):
        errors.append("missing tracker section")
    elif "provider" not in tracker:
        errors.append("missing tracker.provider")
    if not isinstance(policy, dict):
        errors.append("missing policy section")
    elif policy.get("require_write_gate") is not True:
        errors.append("policy.require_write_gate must be true")
    secret_paths = find_raw_secret_paths(data)
    for path in secret_paths:
        errors.append(f"raw secret-like value at {path}")
    return errors


def find_raw_secret_paths(data: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if SECRET_KEY_RE.search(str(key)) and isinstance(value, str):
                if value and not _is_allowed_secret_reference(str(key), value):
                    found.append(path)
            found.extend(find_raw_secret_paths(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(find_raw_secret_paths(item, f"{prefix}[{index}]"))
    return found


def _is_allowed_secret_reference(key: str, value: str) -> bool:
    if value in {"", "[REDACTED]", "REDACTED"}:
        return True
    if key.endswith("_env") or key.endswith("-env") or key.lower() in {"api_token_env", "api_key_env"}:
        return bool(ENV_NAME_RE.match(value))
    if ENV_NAME_RE.match(value) and ("ENV" in key.upper() or value.startswith("QA_AIST_")):
        return True
    return False


def load_project_config(root: Path, config_path: str | Path | None = None) -> ProjectConfig:
    root = root.resolve()
    path = Path(config_path).resolve() if config_path else root / CONFIG_FILE
    data = load_yaml(path)
    errors = validate_config_data(data)
    if errors:
        raise QAConfigError("config_invalid", "Config validation failed", details={"errors": errors})
    paths_data = data["paths"]
    paths = ProjectPaths(
        root=root,
        config=path,
        workspace=_resolve_project_path(root, paths_data["workspace"]),
        cases=_resolve_project_path(root, paths_data["cases"]),
        runners=_resolve_project_path(root, paths_data["runners"]),
        rules=_resolve_project_path(root, paths_data["rules"]),
        issues=_resolve_project_path(root, paths_data.get("issues", f"{paths_data['workspace']}/issues")),
        state=_resolve_project_path(root, paths_data["state"]),
        evidence=_resolve_project_path(root, paths_data["evidence"]),
        reports=_resolve_project_path(root, paths_data["reports"]),
    )
    return ProjectConfig(root=root, path=path, data=data, paths=paths)


def _resolve_project_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
