"""Normalize discovered and configured product execution into one contract.

Discovery is candidate-only.  A candidate becomes executable only after an
explicit local confirmation.  Runtime consumers must use the normalized
contract instead of independently reading legacy and nested YAML sections.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .config import ProjectConfig, QAConfigError
from .runtime_profile import runtime_profile_status
from .security import redact_structure, ensure_safe_structure

EFFECTIVE_CONTRACT_SCHEMA = "quality-pilot.effective-execution-contract.v1"


def effective_product_settings(runtime: Mapping[str, Any]) -> tuple[dict[str, Any], bool, str]:
    """Return one effective product-testing section and its source.

    Nested ``runtime.product_testing`` is authoritative when a field exists.
    The old direct ``runtime.web_ui`` section is a compatibility source only
    for missing nested browser settings.
    """
    nested = runtime.get("product_testing") if isinstance(runtime.get("product_testing"), Mapping) else None
    settings = dict(nested) if nested is not None else {}
    direct_keys = {
        "enabled",
        "allow_readme_commands",
        "readme_command_allowlist",
        "build_recipe",
        "build_artifact",
        "artifact_path",
        "run_operations",
        "build",
        "browser",
        "web_ui",
        "build_timeout_ms",
        "run_timeout_ms",
    }
    if nested is None:
        settings.update({key: runtime[key] for key in direct_keys if key in runtime})
        source = "runtime.direct_legacy" if settings else "none"
    else:
        source = "runtime.product_testing"
        direct_web_ui = runtime.get("web_ui") if isinstance(runtime.get("web_ui"), Mapping) else None
        nested_web_ui = settings.get("web_ui") if isinstance(settings.get("web_ui"), Mapping) else None
        if direct_web_ui is not None and (
            nested_web_ui is None
            or (not bool(nested_web_ui.get("enabled")) and bool(direct_web_ui.get("enabled")))
        ):
            settings["web_ui"] = dict(direct_web_ui)
            source = "runtime.product_testing+runtime.web_ui_compatibility"
    return settings, bool(settings), source


def normalize_execution_contract(
    config: ProjectConfig,
    *,
    worktree: Path | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted, deterministic effective contract and discovery view."""
    runtime = config.data.get("runtime") if isinstance(config.data.get("runtime"), Mapping) else {}
    settings, configured, settings_source = effective_product_settings(runtime)
    execution = runtime.get("execution") if isinstance(runtime.get("execution"), Mapping) else {}
    remote = runtime.get("remote") if isinstance(runtime.get("remote"), Mapping) else {}
    mode = str(runtime.get("execution_mode") or "").strip().lower()
    product_target = str(
        execution.get("product_target")
        or execution.get("remote_product_runner") and "remote_ssh"
        or ("remote_ssh" if mode == "remote" else "local")
    ).strip()
    playwright_target = str(
        execution.get("playwright_target")
        or ("local_via_ssh_tunnel" if product_target == "remote_ssh" else "local")
    ).strip()

    ssh_host = str(
        remote.get("ssh_host")
        or runtime.get("ssh_host")
        or runtime.get("host")
        or ""
    ).strip()
    remote_repo = str(
        remote.get("remote_repo")
        or remote.get("repo")
        or runtime.get("remote_repo")
        or runtime.get("repo")
        or ""
    ).strip()
    remote_python = str(
        remote.get("remote_python")
        or remote.get("python")
        or runtime.get("remote_python")
        or runtime.get("python")
        or ""
    ).strip()
    expected_head_sha = str(
        remote.get("expected_head_sha")
        or runtime.get("expected_head_sha")
        or (snapshot or {}).get("head_sha")
        or ""
    ).strip()

    web_ui = dict(settings.get("web_ui") or {}) if isinstance(settings.get("web_ui"), Mapping) else {}
    discovery = _discover(config, worktree or config.root)
    candidate_runner = str(discovery.get("runner_candidates", [{}])[0].get("command") or "") if discovery.get("runner_candidates") else ""
    candidate_web_ui = {
        "enabled": True,
        "start_command": _target_command(candidate_runner, product_target=product_target, remote_python=remote_python, remote_repo=remote_repo, root=worktree or config.root),
        "url_discovery": "stdout",
        "url_pattern": r"https?://[^\s]+",
        # Safe role-based navigation and visibility assertions are promoted
        # only as a confirmation candidate.  Mutating actions such as Run,
        # fill, and press remain excluded until the user explicitly confirms
        # a product-specific workflow.
        "steps": discovery.get("browser_semantic_steps") or _smoke_steps(),
        "candidate_steps": discovery.get("browser_step_candidates", []),
    } if candidate_runner and discovery.get("browser_test_files") else None

    configured_web_ui = settings.get("web_ui") if isinstance(settings.get("web_ui"), Mapping) else {}
    discovery_conflicts: list[str] = []
    if configured_web_ui and candidate_runner and str(configured_web_ui.get("start_command") or "") and "--browser" in candidate_runner:
        configured_start = str(configured_web_ui.get("start_command") or "")
        if "application.ui.browser.server" in configured_start or ("--browser" not in configured_start and "--tui" not in configured_start):
            discovery_conflicts.append("configured_browser_runner_differs_from_discovered_main_browser_runner")
    explicit_confirmation = bool(execution.get("contract_confirmed") or runtime.get("product_contract_confirmed") or settings.get("contract_confirmed"))
    confirmed = bool(
        explicit_confirmation
        or (not discovery_conflicts and configured and bool(configured_web_ui.get("enabled")) and bool(configured_web_ui.get("start_command")) and bool(configured_web_ui.get("steps")))
    )
    effective_web_ui = deepcopy(web_ui)
    if not effective_web_ui and confirmed and candidate_web_ui:
        effective_web_ui = deepcopy(candidate_web_ui)
    if effective_web_ui:
        settings["web_ui"] = effective_web_ui

    contract = {
        "schema": EFFECTIVE_CONTRACT_SCHEMA,
        "configured": configured,
        "confirmed": confirmed,
        "settings_source": settings_source,
        "execution": {
            "local_review_worktree": bool(execution.get("local_review_worktree", True)),
            "local_pytest": bool(execution.get("local_pytest", True)),
            "product_target": product_target,
            "playwright_target": playwright_target,
        },
        "remote": {
            "ssh_host": ssh_host,
            "repo": remote_repo,
            "python": remote_python,
            "expected_head_sha": expected_head_sha,
        },
        "remote_preflight": deepcopy(runtime.get("remote_preflight")) if isinstance(runtime.get("remote_preflight"), Mapping) else None,
        "product_testing": settings,
        "discovery": discovery,
        "candidate_contract": candidate_web_ui,
        "discovery_conflicts": discovery_conflicts,
    }
    safe, findings = redact_structure(contract, prefix="effective_contract")
    if findings:
        raise QAConfigError("effective_contract_contains_secret", "Discovered execution contract contains secret-like material", details={"findings": [item.as_dict() for item in findings]})
    ensure_safe_structure(safe, context="effective execution contract")
    hash_view = deepcopy(safe)
    # Preflight results and discovery candidates are observations, not part of
    # the executable contract identity.  Otherwise every preflight timestamp
    # would invalidate case lineage.
    hash_view.pop("remote_preflight", None)
    hash_view.pop("discovery", None)
    hash_view.pop("candidate_contract", None)
    canonical = json.dumps(hash_view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    safe["contract_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    safe["status"] = _contract_status(safe)
    safe["missing"] = _missing_fields(safe)
    return safe


def apply_discovered_contract(config: ProjectConfig, *, confirm: bool, expected_head_sha: str | None = None) -> dict[str, Any]:
    """Persist a discovered contract only after explicit confirmation."""
    contract = normalize_execution_contract(config)
    if not confirm:
        return {"status": "awaiting_confirmation", "effective_contract": contract}
    candidate = contract.get("candidate_contract") if isinstance(contract.get("candidate_contract"), Mapping) else None
    if not candidate:
        return {"status": "CONFIGURATION_REQUIRED", "effective_contract": contract, "reason": "no_safe_product_browser_candidate"}
    data = deepcopy(config.data)
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    settings = runtime.get("product_testing") if isinstance(runtime.get("product_testing"), dict) else {}
    settings = deepcopy(settings)
    settings["web_ui"] = deepcopy(dict(candidate))
    settings["contract_confirmed"] = True
    runtime["product_testing"] = settings
    if expected_head_sha and str(runtime.get("execution_mode") or "").strip().lower() == "remote":
        runtime["expected_head_sha"] = str(expected_head_sha)
    execution = runtime.get("execution") if isinstance(runtime.get("execution"), dict) else {}
    execution["contract_confirmed"] = True
    execution["product_target"] = contract.get("execution", {}).get("product_target", execution.get("product_target") or "local")
    execution["playwright_target"] = contract.get("execution", {}).get("playwright_target", execution.get("playwright_target") or "local")
    if expected_head_sha and str(runtime.get("execution_mode") or "").strip().lower() == "remote":
        execution["expected_head_sha"] = str(expected_head_sha)
    runtime["execution"] = execution
    runtime["product_contract_confirmed"] = True
    data["runtime"] = runtime
    _write_yaml(config.path, data)
    updated = ProjectConfig(root=config.root, path=config.path, data=data, paths=config.paths)
    normalized = normalize_execution_contract(updated)
    state_path = config.paths.state / "effective-execution-contract.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "ok", "effective_contract": normalized, "config_path": str(config.path), "state_path": str(state_path)}


def _contract_status(contract: Mapping[str, Any]) -> str:
    if contract.get("discovery_conflicts") and not contract.get("confirmed"):
        return "CONFIRMATION_REQUIRED"
    if not contract.get("configured") and not contract.get("candidate_contract"):
        return "CONFIGURATION_REQUIRED"
    if not contract.get("confirmed"):
        return "CONFIRMATION_REQUIRED"
    missing = _missing_fields(contract)
    return "READY" if not missing else "CONFIGURATION_REQUIRED"


def _missing_fields(contract: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    execution = contract.get("execution") if isinstance(contract.get("execution"), Mapping) else {}
    remote = contract.get("remote") if isinstance(contract.get("remote"), Mapping) else {}
    settings = contract.get("product_testing") if isinstance(contract.get("product_testing"), Mapping) else {}
    if execution.get("product_target") == "remote_ssh":
        for key in ("ssh_host", "repo", "python"):
            if not str(remote.get(key) or "").strip():
                missing.append(f"remote.{key}")
    web_ui = settings.get("web_ui") if isinstance(settings.get("web_ui"), Mapping) else {}
    if web_ui.get("enabled"):
        if not str(web_ui.get("start_command") or "").strip():
            missing.append("product_testing.web_ui.start_command")
        if not web_ui.get("steps"):
            missing.append("product_testing.web_ui.steps")
        if str(web_ui.get("url_discovery") or "").lower() == "stdout" and not str(web_ui.get("url_pattern") or "").strip():
            missing.append("product_testing.web_ui.url_pattern")
    return missing


def _discover(config: ProjectConfig, root: Path) -> dict[str, Any]:
    profile = runtime_profile_status(ProjectConfig(root=root, path=config.path, data=config.data, paths=config.paths))
    analysis = profile.get("repo_analysis") if isinstance(profile.get("repo_analysis"), Mapping) else {}
    candidates: list[dict[str, Any]] = []
    for item in analysis.get("entrypoint_candidates", []) if isinstance(analysis.get("entrypoint_candidates"), list) else []:
        if not isinstance(item, Mapping):
            continue
        command = str(item.get("entrypoint") or "").strip()
        if command:
            candidates.append({"command": command, "source": item.get("source"), "confidence": item.get("confidence")})
    browser_candidates = [item for item in candidates if "--browser" in str(item.get("command") or "")]
    runner_candidates = browser_candidates or candidates[:8]
    browser_test_files = _browser_test_files(root)
    all_browser_steps = _browser_step_candidates(root)
    semantic_steps = _safe_semantic_steps(root)
    return {
        "runner_candidates": runner_candidates,
        "browser_test_files": browser_test_files,
        "browser_test_count": len(browser_test_files),
        "browser_step_candidates": all_browser_steps[:40],
        "browser_step_candidate_count": len(all_browser_steps),
        "browser_semantic_steps": semantic_steps,
        "browser_smoke_steps": _smoke_steps() if browser_test_files else [],
        "source": ["README", "--help/source analysis", "runtime profile", "existing browser tests"],
    }


def _target_command(command: str, *, product_target: str, remote_python: str, remote_repo: str, root: Path) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if not tokens:
        return command
    if Path(tokens[0]).name.lower().startswith("python") and product_target == "remote_ssh" and remote_python:
        try:
            if remote_repo and Path(remote_python).resolve().is_relative_to(Path(remote_repo).resolve()):
                tokens[0] = str(Path(remote_python).resolve().relative_to(Path(remote_repo).resolve())).replace("\\", "/")
            else:
                tokens[0] = remote_python
        except (OSError, ValueError):
            tokens[0] = remote_python
    elif Path(tokens[0]).is_absolute():
        try:
            tokens[0] = str(Path(tokens[0]).resolve().relative_to(root.resolve())).replace("\\", "/")
        except (OSError, ValueError):
            pass
    return " ".join(shlex.quote(token) for token in tokens)


def _safe_semantic_steps(root: Path) -> list[dict[str, Any]]:
    """Extract only read-only role navigation/visibility candidates.

    Existing Browser tests are discovery evidence, not authority.  This
    extractor deliberately excludes fill/press/run/clear actions and only
    returns literal role/name locators that can be reviewed once before being
    persisted as the product Browser contract.
    """
    role_pattern = re.compile(
        r"""get_by_role\(\s*['\"](?P<role>[^'\"]+)['\"]\s*,\s*name\s*=\s*['\"](?P<name>[^'\"]+)['\"]"""
    )
    label_pattern = re.compile(r"""get_by_label\(\s*['\"](?P<label>[^'\"]+)['\"]\s*\)""")
    allowed_roles = {"tab", "tabpanel", "heading", "button", "link"}
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative in _browser_test_files(root):
        path = root / relative
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            action = "click" if ".click(" in stripped else ("expect_visible" if "to_be_visible" in stripped else None)
            if action is None:
                continue
            role_match = role_pattern.search(stripped)
            if role_match:
                role = role_match.group("role")
                name = role_match.group("name")
                if role not in allowed_roles or (action == "click" and role != "tab"):
                    continue
                locator = {"type": "role", "role": role, "name": name}
            else:
                label_match = label_pattern.search(stripped)
                if not label_match or action != "expect_visible":
                    continue
                locator = {"type": "label", "name": label_match.group("label")}
            key = json.dumps([action, locator], ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            steps.append({"action": action, "locator": locator})
            if len(steps) >= 16:
                return steps
    return steps


def _smoke_steps() -> list[dict[str, str]]:
    return [
        {"action": "expect_visible", "selector": "body"},
        {"action": "expect_visible", "selector": "button"},
    ]


def _browser_test_files(root: Path) -> list[str]:
    values: list[str] = []
    tests_root = root / "tests"
    if not tests_root.exists():
        return values
    for path in sorted(tests_root.rglob("test_*.py")):
        lowered = str(path).lower()
        if any(token in lowered for token in ("browser", "playwright", "/ui/")):
            values.append(str(path.relative_to(root)).replace("\\", "/"))
    return values[:100]


def _browser_step_candidates(root: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for relative in _browser_test_files(root):
        path = root / relative
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            action = None
            if ".click(" in stripped:
                action = "click"
            elif ".fill(" in stripped:
                action = "fill"
            elif ".press(" in stripped:
                action = "press"
            elif "expect(" in stripped or "to_be_visible" in stripped or "to_have_text" in stripped:
                action = "semantic_assertion"
            if action:
                candidates.append({"action": action, "source": relative, "line": index, "summary": stripped[:240]})
    return candidates[:100]


def _write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise QAConfigError("yaml_required", "PyYAML is required to persist the effective contract") from exc
    path.write_text(yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False), encoding="utf-8")
