from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

from .config import ProjectConfig, find_raw_secret_paths, json_dumps
from .contracts import ContractError, load_contracts
from .issues import case_id_for_issue, load_issue_snapshot
from .policy_pack import common_questions, dimension_specs, policy_pack
from .redmine import RedmineError, import_redmine_issues
from .runner import utc_now

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class CaseGenerationError(RuntimeError):
    pass


SUPPORTED_PROFILES = {"auto", "cli", "api", "hardware", "repo"}
DEFAULT_SCRATCH_COUNT = 5
GROWTH_CONTEXT_NAME = "growth-context.json"
INIT_CONTEXT_NAME = "init-context.json"


def generate_cases_from_issues(
    config: ProjectConfig,
    *,
    issue_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    snapshot = load_issue_snapshot(config)
    items = [item for item in snapshot.get("items", []) if isinstance(item, dict)]
    if issue_id is not None:
        items = [item for item in items if int(item.get("issue_id", -1)) == issue_id]
    if issue_id is not None and not items:
        return {"status": "error", "error": "issue_not_in_snapshot", "issue_id": issue_id}

    config.paths.cases.mkdir(parents=True, exist_ok=True)
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    for item in items:
        contract = draft_contract_for_issue(item)
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        item_questions = contract.get("quality_pilot", {}).get("questions", [])
        if item_questions:
            questions.append({"case_id": contract["case_id"], "issue_id": item.get("issue_id"), "questions": item_questions})
        generated.append(
            {
                "case_id": contract["case_id"],
                "issue_id": item.get("issue_id"),
                "path": _relative_or_str(path, config.root),
                "draft": bool(contract.get("quality_pilot", {}).get("draft")),
                "question_count": len(item_questions),
            }
        )

    status = "needs_input" if questions else "ok"
    return {
        "status": status,
        "generated": generated,
        "skipped": skipped,
        "questions": questions,
        "snapshot_path": _relative_or_str(config.paths.state / "issues-snapshot.json", config.root),
        "message": "Draft cases generated; answer questions before running qa-test." if questions else "Cases generated.",
    }


def generate_cases_from_scratch(
    config: ProjectConfig,
    *,
    feature: str | None = None,
    profile: str = "auto",
    count: int = DEFAULT_SCRATCH_COUNT,
    force: bool = False,
) -> dict[str, Any]:
    if profile not in SUPPORTED_PROFILES:
        raise CaseGenerationError(f"unsupported profile: {profile}")
    if count < 1:
        raise CaseGenerationError("count must be >= 1")

    config.paths.cases.mkdir(parents=True, exist_ok=True)
    signals = inspect_repo_signals(config)
    resolved_profile = _resolve_profile(profile, signals)
    feature_name = _feature_name(config, feature, signals)
    policy = policy_pack()

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    for index, spec in enumerate(_select_dimension_specs(count), start=1):
        contract = draft_contract_from_scratch(
            config,
            feature=feature_name,
            requested_profile=profile,
            resolved_profile=resolved_profile,
            signals=signals,
            spec=spec,
            index=index,
            policy=policy,
        )
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        item_questions = contract.get("quality_pilot", {}).get("questions", [])
        if item_questions:
            questions.append({"case_id": contract["case_id"], "questions": item_questions})
        generated.append(
            {
                "case_id": contract["case_id"],
                "path": _relative_or_str(path, config.root),
                "draft": bool(contract.get("quality_pilot", {}).get("draft")),
                "question_count": len(item_questions),
                "profile": resolved_profile,
                "swqa_dimensions": contract.get("swqa_dimensions", []),
            }
        )

    return {
        "status": "needs_input" if questions else "ok",
        "source": "from_scratch",
        "feature": feature_name,
        "requested_profile": profile,
        "resolved_profile": resolved_profile,
        "generated": generated,
        "skipped": skipped,
        "questions": questions,
        "policy_pack": policy["name"],
        "policy_dimensions": policy["swqa_dimensions"],
        "closed_loop_steps": policy["closed_loop_steps"],
        "repo_signals": signals,
        "message": "Starter executable safe-probe cases generated.",
    }


def generate_cases_init(
    config: ProjectConfig,
    *,
    feature: str | None = None,
    profile: str = "auto",
    count: int | None = None,
    fast: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    if profile not in SUPPORTED_PROFILES:
        raise CaseGenerationError(f"unsupported profile: {profile}")
    if count is not None and count < 1:
        raise CaseGenerationError("generated_count must be >= 1")

    config.paths.cases.mkdir(parents=True, exist_ok=True)
    config.paths.state.mkdir(parents=True, exist_ok=True)
    context = build_init_context(config, feature=feature, profile=profile)
    context["fast"] = bool(fast)
    context["generated_count_limit"] = count
    context["interaction_scope"] = "autonomous" if fast else "category"
    context["fast_mode_assumptions"] = fast_mode_assumptions(context) if fast else []
    context_path = init_context_path(config)
    context_path.write_text(json_dumps(context) + "\n", encoding="utf-8")

    candidates = build_init_candidates(context, count=count)
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    deduped: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    seen = existing_case_fingerprints(config)
    in_batch: set[str] = set()

    for index, candidate in enumerate(candidates, start=1):
        fingerprint = candidate_fingerprint(candidate)
        if fingerprint in seen or fingerprint in in_batch:
            deduped.append(
                {
                    "title": candidate.get("title"),
                    "fingerprint": fingerprint,
                    "reason": "duplicate_existing_case" if fingerprint in seen else "duplicate_init_candidate",
                }
            )
            continue
        in_batch.add(fingerprint)
        contract = draft_contract_from_init(config, candidate=candidate, index=index, context=context)
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        item_questions = contract.get("quality_pilot", {}).get("questions", [])
        if item_questions:
            questions.append({"case_id": contract["case_id"], "questions": item_questions})
        generated.append(
            {
                "case_id": contract["case_id"],
                "path": _relative_or_str(path, config.root),
                "draft": bool(contract.get("quality_pilot", {}).get("draft")),
                "question_count": len(item_questions),
                "profile": contract.get("profile"),
                "init_seed": contract.get("init_seed", {}).get("id"),
                "swqa_dimensions": contract.get("swqa_dimensions", []),
            }
        )

    advisory_inputs = [] if fast else init_missing_inputs(context)
    missing_inputs: list[str] = []
    needs_input = bool(questions or missing_inputs)
    return {
        "status": "needs_input" if needs_input else "ok",
        "source": "init",
        "mode": "init",
        "fast": bool(fast),
        "interaction_scope": "autonomous" if fast else "category",
        "generation_strategy": "opinionated_swqa_init",
        "generation_limit": "all_init_seed_dimension_pairs" if count is None else "manual_generated_count_cap",
        "generated_count_limit": count,
        "requested_generated_count": count,
        "requested_count": count,
        "assumption_policy": (
            "Fast mode: AI Quality Pilot chooses the strictest safe defaults and does not ask case-by-case questions."
            if fast else
            "Generate the initial SWQA map first; ask only category-level blocking execution inputs."
        ),
        "fast_mode_assumptions": context["fast_mode_assumptions"],
        "feature": context["feature"],
        "requested_profile": profile,
        "resolved_profile": context["resolved_profile"],
        "init_context_path": _relative_or_str(context_path, config.root),
        "analyzed_files_count": context["repo_inventory"]["analyzed_files_count"],
        "candidate_count": len(candidates),
        "generated": generated,
        "skipped": skipped,
        "deduped": deduped,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "deduped_count": len(deduped),
        "questions": questions,
        "missing_inputs": missing_inputs,
        "missing_input_count": len(missing_inputs),
        "advisory_inputs": advisory_inputs,
        "advisory_input_count": len(advisory_inputs),
        "policy_pack": context["policy_pack"]["name"],
        "policy_dimensions": context["policy_pack"]["swqa_dimensions"],
        "closed_loop_steps": context["policy_pack"]["closed_loop_steps"],
        "message": (
            "Initial SWQA executable case map generated in fast autonomous mode with strict safe defaults."
            if fast else
            "Initial SWQA executable case map generated with opinionated side-effect-safe coverage; lab inputs remain advisory until you add lab runners."
            if needs_input else
            "Initial SWQA executable case map generated."
        ),
    }


def generate_cases_growing(
    config: ProjectConfig,
    *,
    feature: str | None = None,
    profile: str = "auto",
    count: int | None = DEFAULT_SCRATCH_COUNT,
    fast: bool = False,
    force: bool = False,
    candidate_json: str | Path | None = None,
    issue_id: int | None = None,
) -> dict[str, Any]:
    if profile not in SUPPORTED_PROFILES:
        raise CaseGenerationError(f"unsupported profile: {profile}")
    if count is None:
        count = DEFAULT_SCRATCH_COUNT
    if count < 1:
        raise CaseGenerationError("count must be >= 1")

    config.paths.cases.mkdir(parents=True, exist_ok=True)
    config.paths.state.mkdir(parents=True, exist_ok=True)
    context = build_growth_context(config, feature=feature, profile=profile, issue_id=issue_id)
    context["fast"] = bool(fast)
    context["generated_count_limit"] = count
    context["interaction_scope"] = "autonomous" if fast else "category"
    context["fast_mode_assumptions"] = fast_mode_assumptions(context) if fast else []
    context_path = growth_context_path(config)
    context_path.write_text(json_dumps(context) + "\n", encoding="utf-8")

    if candidate_json:
        raw_candidates = load_candidate_json(candidate_json)
        candidates = normalize_external_candidates(raw_candidates, context)
        candidate_source = _relative_or_str(Path(candidate_json), config.root)
    else:
        candidates = build_growth_candidates(context, count=count)
        candidate_source = "deterministic_growth_generator"

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    deduped: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    seen = existing_case_fingerprints(config)
    in_batch: set[str] = set()

    for index, candidate in enumerate(candidates[:count], start=1):
        fingerprint = candidate_fingerprint(candidate)
        if fingerprint in seen or fingerprint in in_batch:
            deduped.append(
                {
                    "title": candidate.get("title"),
                    "fingerprint": fingerprint,
                    "reason": "duplicate_existing_case" if fingerprint in seen else "duplicate_growth_candidate",
                }
            )
            continue
        in_batch.add(fingerprint)
        contract = draft_contract_from_growth(config, candidate=candidate, index=index, context=context)
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        item_questions = contract.get("quality_pilot", {}).get("questions", [])
        if item_questions:
            questions.append({"case_id": contract["case_id"], "questions": item_questions})
        generated.append(
            {
                "case_id": contract["case_id"],
                "path": _relative_or_str(path, config.root),
                "draft": bool(contract.get("quality_pilot", {}).get("draft")),
                "question_count": len(item_questions),
                "profile": contract.get("profile"),
                "growth_seed": contract.get("growth_seed", {}).get("id"),
                "swqa_dimensions": contract.get("swqa_dimensions", []),
            }
        )

    advisory_inputs = [] if fast else growth_missing_inputs(context)
    missing_inputs: list[str] = []
    needs_input = bool(questions or missing_inputs)
    return {
        "status": "needs_input" if needs_input else "ok",
        "source": "growth",
        "mode": "growing",
        "fast": bool(fast),
        "interaction_scope": "autonomous" if fast else "category",
        "feature": context["feature"],
        "requested_profile": profile,
        "resolved_profile": context["resolved_profile"],
        "growth_context_path": _relative_or_str(context_path, config.root),
        "growth_seed_count": len(context["growth_seeds"]),
        "candidate_source": candidate_source,
        "candidate_count": len(candidates),
        "generated": generated,
        "skipped": skipped,
        "deduped": deduped,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "deduped_count": len(deduped),
        "questions": questions,
        "missing_inputs": missing_inputs,
        "missing_input_count": len(missing_inputs),
        "advisory_inputs": advisory_inputs,
        "advisory_input_count": len(advisory_inputs),
        "generation_limit": "manual_generated_count_cap",
        "generated_count_limit": count,
        "requested_generated_count": count,
        "requested_count": count,
        "assumption_policy": (
            "Fast mode: AI Quality Pilot chooses the strictest safe defaults and does not ask case-by-case questions."
            if fast else
            "Growing generation asks only category-level blocking inputs, never one question per test case."
        ),
        "fast_mode_assumptions": context["fast_mode_assumptions"],
        "policy_pack": context["policy_pack"]["name"],
        "policy_dimensions": context["policy_pack"]["swqa_dimensions"],
        "closed_loop_steps": context["policy_pack"]["closed_loop_steps"],
        "message": (
            "Growth executable cases generated in fast autonomous mode with strict safe defaults."
            if fast else
            "Growth executable cases generated with side-effect-safe probes; lab inputs remain advisory until you add lab runners."
        ),
    }


def generate_cases_from_redmine_issues(
    config: ProjectConfig,
    *,
    issue_ids: list[int],
    force: bool = False,
) -> dict[str, Any]:
    if not issue_ids:
        raise CaseGenerationError("--redmine-issues requires at least one issue id")
    imported = import_redmine_issues(config, issue_ids=issue_ids)
    config.paths.cases.mkdir(parents=True, exist_ok=True)
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for issue in imported["issues"]:
        contract = draft_contract_for_redmine_issue(config, issue)
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        generated.append(
            {
                "case_id": contract["case_id"],
                "redmine_issue_id": issue["id"],
                "path": _relative_or_str(path, config.root),
                "draft": False,
                "question_count": 0,
                "swqa_dimensions": contract.get("swqa_dimensions", []),
            }
        )
    return {
        "status": "ok",
        "source": "redmine",
        "mode": "redmine_issues",
        "requested_issue_ids": issue_ids,
        "imported_issue_ids": imported["imported_issue_ids"],
        "redmine_import_path": imported["import_path"],
        "mirror_paths": imported.get("mirror_paths", []),
        "remote_write": "not_applicable",
        "generated": generated,
        "skipped": skipped,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "message": "Redmine MCP issues were read directly and executable linked cases were generated.",
    }


def build_growth_context(
    config: ProjectConfig,
    *,
    feature: str | None = None,
    profile: str = "auto",
    issue_id: int | None = None,
) -> dict[str, Any]:
    signals = inspect_repo_signals(config)
    resolved_profile = _resolve_profile(profile, signals)
    feature_name = _feature_name(config, feature, signals)
    snapshot = load_issue_snapshot(config)
    items = [item for item in snapshot.get("items", []) if isinstance(item, dict)]
    if issue_id is not None:
        items = [item for item in items if int(item.get("issue_id", -1)) == issue_id]
    existing_cases = existing_case_summaries(config)
    latest_run = load_state_json(config, "latest-run.json")
    publish_plan = load_state_json(config, "publish-plan.json")
    pr_refs = collect_pr_references(items)
    context: dict[str, Any] = {
        "schema": "quality-pilot.growth-context.v1",
        "generated_at": utc_now(),
        "feature": feature_name,
        "requested_profile": profile,
        "resolved_profile": resolved_profile,
        "policy_pack": policy_pack(),
        "repo_signals": signals,
        "issue_snapshot": {
            "snapshot_exists": issue_snapshot_path_exists(config),
            "synced_at": snapshot.get("synced_at"),
            "open_count": len(items),
            "closed_issue_policy": snapshot.get("closed_issue_policy"),
            "items": items,
        },
        "pr_references": pr_refs,
        "latest_run": summarize_latest_run(latest_run),
        "publish_plan": summarize_publish_plan(publish_plan),
        "existing_cases": existing_cases,
        "existing_runners": file_summaries(config.paths.runners),
        "project_rules": file_summaries(config.paths.rules),
    }
    context["growth_seeds"] = build_growth_seeds(context)
    return context


def build_init_context(
    config: ProjectConfig,
    *,
    feature: str | None = None,
    profile: str = "auto",
) -> dict[str, Any]:
    signals = inspect_repo_signals(config)
    resolved_profile = _resolve_profile(profile, signals)
    feature_name = _feature_name(config, feature, signals)
    inventory = scan_repo_inventory(config)
    context: dict[str, Any] = {
        "schema": "quality-pilot.init-context.v1",
        "generated_at": utc_now(),
        "feature": feature_name,
        "requested_profile": profile,
        "resolved_profile": resolved_profile,
        "policy_pack": policy_pack(),
        "repo_signals": signals,
        "repo_inventory": inventory,
        "existing_cases": existing_case_summaries(config),
        "existing_runners": file_summaries(config.paths.runners),
        "project_rules": file_summaries(config.paths.rules),
    }
    context["init_seeds"] = build_init_seeds(context)
    return context


def build_init_seeds(context: dict[str, Any]) -> list[dict[str, Any]]:
    signals = context["repo_signals"]
    inventory = context["repo_inventory"]
    project_name = str(signals.get("project_name") or context["feature"] or "Repository")
    seeds: list[dict[str, Any]] = []
    for command in signals.get("cli_commands", [])[:4]:
        seeds.append(
            {
                "id": f"cli-{_slug(str(command)).lower()}",
                "type": "cli_command",
                "title": f"{command} CLI",
                "summary": "CLI entry point discovered from project metadata.",
                "surface": str(command),
            }
        )
    for package_file in inventory.get("package_files", [])[:4]:
        seeds.append(
            {
                "id": f"package-{_slug(str(package_file.get('name'))).lower()}",
                "type": "package_metadata",
                "title": str(package_file.get("name")),
                "summary": "Package or build metadata discovered during initial repo scan.",
                "surface": str(package_file.get("path")),
            }
        )
    for code_root in inventory.get("code_roots", [])[:5]:
        seeds.append(
            {
                "id": f"code-{_slug(str(code_root.get('path'))).lower()}",
                "type": "code_surface",
                "title": str(code_root.get("path")),
                "summary": f"{code_root.get('count')} source files under this top-level surface.",
                "surface": str(code_root.get("path")),
            }
        )
    if not seeds:
        seeds.append(
            {
                "id": f"repo-{_slug(project_name).lower()}",
                "type": "repository",
                "title": project_name,
                "summary": "Repository-level behavior discovered during initial AI Quality Pilot setup.",
                "surface": project_name,
            }
        )
    return seeds


def build_init_candidates(context: dict[str, Any], *, count: int | None = None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seeds = context.get("init_seeds", [])
    specs = init_dimension_specs()
    for seed in seeds:
        for spec in specs:
            candidates.append(candidate_from_init_seed(context, seed, spec))
    if count is not None:
        return candidates[:count]
    return candidates


def candidate_from_init_seed(context: dict[str, Any], seed: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    feature = str(seed.get("title") or context["feature"])
    return {
        "title": f"{feature}: {spec['title']}",
        "feature": feature,
        "profile": context["resolved_profile"],
        "expected": spec["expected"],
        "swqa_dimensions": spec["dimensions"],
        "init_seed": seed,
        "analysis_reason": f"Initial full-repo SWQA map expands {seed.get('type')} through {spec['key']} coverage.",
        "questions": [],
        "risk_controls": [
            "side_effect_safe_probe_first",
            "do_not_publish_until_write_gate_passes",
            "side_effect_boundary_must_be_confirmed_before_lab_runner",
            "stress_or_timeout_requires_baseline_before_defect_filing",
            "missing_fixture_or_runner_stays_advisory_for_init_probe",
        ],
    }


def draft_contract_from_init(
    config: ProjectConfig,
    *,
    candidate: dict[str, Any],
    index: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    fingerprint = candidate_fingerprint(candidate)
    case_id = f"INIT-{_slug(str(candidate.get('feature') or context['feature']))}-{fingerprint[:8].upper()}"
    commands = safe_commands_for_generated_case(config, case_id=case_id, candidate=candidate, context=context)
    return {
        "case_id": case_id,
        "title": str(candidate["title"]),
        "source": {
            "type": "init",
            "method": "full_repo_swqa_init",
            "context_schema": context["schema"],
            "context_path": _relative_or_str(init_context_path(config), config.root),
            "candidate_fingerprint": fingerprint,
        },
        "profile": candidate.get("profile") or context["resolved_profile"],
        "feature": candidate.get("feature") or context["feature"],
        "priority": _priority_for_spec(str(candidate.get("swqa_dimensions", [""])[0])),
        "contract_version": 1,
        "init_seed": candidate["init_seed"],
        "analysis_reason": candidate["analysis_reason"],
        "quality_pilot": {
            "draft": False,
            "generation_mode": "init",
            "review_required_before_run": False,
            "executable": True,
            "executable_scope": "side_effect_safe_probe",
            "interaction_scope": context.get("interaction_scope", "category"),
            "fast_mode": bool(context.get("fast")),
            "fast_mode_assumptions": context.get("fast_mode_assumptions", []),
            "questions": candidate.get("questions", []),
            "policy_pack": context["policy_pack"]["name"],
            "closed_loop_steps": context["policy_pack"]["closed_loop_steps"],
            "gates": context["policy_pack"]["gates"],
            "triage_categories": context["policy_pack"]["triage_categories"],
        },
        "swqa_dimensions": candidate["swqa_dimensions"],
        "commands": commands,
        "expected": str(candidate.get("expected") or "Expected behavior must be confirmed during cases review."),
        "risk_controls": candidate.get("risk_controls", []),
    }


def build_growth_seeds(context: dict[str, Any]) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for item in context["issue_snapshot"]["items"]:
        issue_id = item.get("issue_id")
        title = str(item.get("title") or f"Issue {issue_id}")
        seeds.append(
            {
                "id": f"issue-{issue_id}",
                "type": "issue",
                "title": title,
                "summary": _compact_text(str(item.get("body") or title)),
                "labels": item.get("labels", []),
                "url": item.get("url"),
                "issue_id": issue_id,
                "pull_requests": item.get("pull_requests", []),
                "dimensions_hint": ["exact_reproduction", "negative", "boundary", "sibling_surface"],
            }
        )

    latest = context.get("latest_run", {})
    for result in latest.get("interesting_results", []):
        case_id = result.get("case_id") or "unknown"
        seeds.append(
            {
                "id": f"run-{case_id}",
                "type": "latest_run",
                "title": str(result.get("title") or case_id),
                "summary": f"{case_id} ended as {result.get('status')} with exit code {result.get('exit_code')}",
                "case_id": case_id,
                "dimensions_hint": ["exact_reproduction", "side_effect_safe", "stress_timeout_risk"],
            }
        )

    for case in context.get("existing_cases", [])[:8]:
        seeds.append(
            {
                "id": f"case-{case.get('case_id')}",
                "type": "existing_case",
                "title": str(case.get("title") or case.get("case_id")),
                "summary": str(case.get("expected") or case.get("title") or ""),
                "case_id": case.get("case_id"),
                "feature": case.get("feature"),
                "dimensions_hint": ["sibling_surface", "boundary", "invalid_input"],
            }
        )

    if context.get("feature") or not seeds:
        seeds.insert(
            0,
            {
                "id": f"feature-{_slug(str(context.get('feature') or context['repo_signals'].get('project_name') or 'repo')).lower()}",
                "type": "feature",
                "title": str(context.get("feature") or context["repo_signals"].get("project_name") or "Repository behavior"),
                "summary": "User-requested or repo-detected surface for proactive growth testing.",
                "dimensions_hint": ["positive", "negative", "boundary", "invalid_input", "side_effect_safe"],
            },
        )
    return seeds


def build_growth_candidates(context: dict[str, Any], *, count: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    specs = growth_dimension_specs()
    for seed in context.get("growth_seeds", []):
        for spec in specs:
            candidates.append(candidate_from_seed(context, seed, spec))
            if len(candidates) >= count * 3:
                return candidates
    return candidates


def candidate_from_seed(context: dict[str, Any], seed: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    title = f"{seed.get('title')}: {spec['title']}"
    dimensions = sorted(set(spec["dimensions"]) | set(seed.get("dimensions_hint", [])[:1]))
    return {
        "title": title,
        "feature": seed.get("feature") or context["feature"],
        "profile": context["resolved_profile"],
        "expected": spec["expected"],
        "swqa_dimensions": dimensions,
        "growth_seed": seed,
        "growth_reason": f"Expand {seed.get('type')} signal through {spec['key']} coverage.",
        "six_hats": six_hats_for(seed, spec),
        "questions": [],
        "risk_controls": [
            "side_effect_safe_probe_first",
            "dedupe_against_existing_cases",
            "side_effect_boundary_must_be_confirmed_before_lab_runner",
            "write_gate_required_before_tracker_write",
        ],
    }


def draft_contract_from_growth(
    config: ProjectConfig,
    *,
    candidate: dict[str, Any],
    index: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    fingerprint = candidate_fingerprint(candidate)
    case_id = f"GROW-{_slug(str(candidate.get('feature') or context['feature']))}-{fingerprint[:8].upper()}"
    commands = candidate.get("commands") if isinstance(candidate.get("commands"), list) else safe_commands_for_generated_case(
        config,
        case_id=case_id,
        candidate=candidate,
        context=context,
    )
    return {
        "case_id": case_id,
        "title": str(candidate["title"]),
        "source": {
            "type": "growth",
            "method": "growing",
            "context_schema": context["schema"],
            "context_path": _relative_or_str(growth_context_path(config), config.root),
            "candidate_fingerprint": fingerprint,
        },
        "profile": candidate.get("profile") or context["resolved_profile"],
        "feature": candidate.get("feature") or context["feature"],
        "priority": candidate.get("priority") or "P2",
        "contract_version": 1,
        "growth_seed": candidate["growth_seed"],
        "six_hats": candidate["six_hats"],
        "growth_reason": candidate["growth_reason"],
        "quality_pilot": {
            "draft": False,
            "generation_mode": "growth",
            "review_required_before_run": False,
            "executable": True,
            "executable_scope": "side_effect_safe_probe",
            "interaction_scope": context.get("interaction_scope", "category"),
            "fast_mode": bool(context.get("fast")),
            "fast_mode_assumptions": context.get("fast_mode_assumptions", []),
            "questions": candidate.get("questions", []),
            "policy_pack": context["policy_pack"]["name"],
            "closed_loop_steps": context["policy_pack"]["closed_loop_steps"],
            "gates": context["policy_pack"]["gates"],
            "triage_categories": context["policy_pack"]["triage_categories"],
        },
        "swqa_dimensions": candidate["swqa_dimensions"],
        "commands": commands,
        "expected": str(candidate.get("expected") or "Expected behavior must be confirmed during cases review."),
        "risk_controls": candidate.get("risk_controls", []),
    }


def review_generated_cases(config: ProjectConfig) -> dict[str, Any]:
    reviews: list[dict[str, Any]] = []
    for path in sorted([*config.paths.cases.glob("*.yaml"), *config.paths.cases.glob("*.yml")]):
        try:
            data = _load_yaml(path)
        except Exception:
            continue
        qa = data.get("quality_pilot") if isinstance(data.get("quality_pilot"), dict) else {}
        if qa.get("draft") or qa.get("questions"):
            reviews.append(
                {
                    "case_id": data.get("case_id"),
                    "path": _relative_or_str(path, config.root),
                    "draft": bool(qa.get("draft")),
                    "questions": qa.get("questions", []),
                    "source": data.get("source"),
                }
            )
    return {"status": "ok", "draft_count": len(reviews), "drafts": reviews}


def validate_generated_cases(config: ProjectConfig) -> dict[str, Any]:
    try:
        contracts = load_contracts(config.paths.cases)
    except ContractError as exc:
        return {"status": "error", "error": exc.error, "message": exc.message, "path": exc.path}
    review = review_generated_cases(config)
    return {
        "status": "ok",
        "case_count": len(contracts),
        "draft_count": review["draft_count"],
        "cases": [
            {
                "case_id": contract.case_id,
                "title": contract.title,
                "contract_hash": contract.contract_hash,
                "path": _relative_or_str(contract.path, config.root),
            }
            for contract in contracts
        ],
        "drafts": review["drafts"],
    }


def draft_contract_for_issue(item: dict[str, Any]) -> dict[str, Any]:
    case_id = case_id_for_issue(item)
    issue_id = int(item.get("issue_id"))
    title = str(item.get("title") or f"Gitea issue #{issue_id}")
    body = str(item.get("body") or "")
    command = extract_repro_command(body)
    questions: list[str] = []
    run = command or f"python3 -c \"print('AI Quality Pilot safe issue probe for Gitea issue #{issue_id}: no repro command was provided')\""
    policy = policy_pack()
    return {
        "case_id": case_id,
        "title": f"Gitea #{issue_id}: {title}",
        "source": {
            "type": "issue",
            "provider": "gitea",
            "issue_id": issue_id,
            "issue_url": item.get("url") or "",
        },
        "quality_pilot": {
            "draft": bool(questions),
            "questions": questions,
            "review_required_before_run": False,
            "executable": True,
            "executable_scope": "side_effect_safe_probe" if not command else "issue_reproduction_command",
            "policy_pack": policy["name"],
            "closed_loop_steps": policy["closed_loop_steps"],
        },
        "commands": [
            {
                "id": "reproduce",
                "run": run,
                "expected_exit_code": 0,
            }
        ],
        "expected": "Original issue is reproduced or verified through a side-effect-safe user-facing path.",
        "swqa_dimensions": [
            "exact_reproduction",
            "sibling_surface",
            "negative",
            "boundary",
            "side_effect_safe",
        ],
        "risk_controls": [
            "closed_issue_references_are_pruned_before_publish",
            "write_gate_required_before_tracker_write",
            "pass_expected_or_fixture_corrected_results_do_not_comment_on_issues",
        ],
        "swqa_expansion": [
            "exact_reproduction",
            "sibling_surface_scan",
            "negative_cases",
            "boundary_values",
            "side_effect_safe_smoke",
        ],
    }


def draft_contract_for_redmine_issue(config: ProjectConfig, issue: dict[str, Any]) -> dict[str, Any]:
    policy = policy_pack()
    case_id = f"REDMINE-{int(issue['id'])}"
    candidate = {
        "feature": f"Redmine #{issue['id']}",
        "title": issue["subject"],
        "swqa_dimensions": ["exact_reproduction", "functional", "negative", "boundary", "side_effect_safe"],
        "init_seed": {"surface": f"Redmine #{issue['id']}", "title": issue["subject"]},
    }
    return {
        "case_id": case_id,
        "title": f"Redmine #{issue['id']}: {issue['subject']}",
        "source": {
            "type": "redmine",
            "provider": "redmine",
            "redmine_issue_id": int(issue["id"]),
            "redmine_url": issue.get("url") or "",
            "redmine_message": issue.get("full_message") or issue.get("description") or "",
        },
        "profile": "auto",
        "feature": f"Redmine #{issue['id']}",
        "priority": "P1",
        "contract_version": 1,
        "quality_pilot": {
            "draft": False,
            "generation_mode": "redmine_issues",
            "review_required_before_run": False,
            "executable": True,
            "executable_scope": "side_effect_safe_probe",
            "questions": [],
            "policy_pack": policy["name"],
            "closed_loop_steps": policy["closed_loop_steps"],
            "gates": policy["gates"],
            "triage_categories": policy["triage_categories"],
        },
        "swqa_dimensions": candidate["swqa_dimensions"],
        "commands": [
            {
                "id": "safe_probe",
                "run": _safe_probe_command(config, candidate=candidate, context={"repo_signals": inspect_repo_signals(config)}),
                "expected_exit_code": 0,
            }
        ],
        "expected": "The Redmine-reported behavior is covered first by a side-effect-safe probe, then can be strengthened with a lab runner after review.",
        "risk_controls": [
            "redmine_mcp_snapshot_must_be_valid",
            "gitea_issue_candidate_requires_write_gate",
            "side_effect_safe_probe_first",
            "do_not_publish_until_write_gate_passes",
        ],
    }


def draft_contract_from_scratch(
    config: ProjectConfig,
    *,
    feature: str,
    requested_profile: str,
    resolved_profile: str,
    signals: dict[str, Any],
    spec: dict[str, Any],
    index: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    case_id = f"GEN-{_slug(feature)}-{index:03d}"
    questions: list[str] = []
    candidate = {
        "feature": feature,
        "swqa_dimensions": spec["dimensions"],
        "init_seed": {"surface": feature, "title": feature},
    }

    return {
        "case_id": case_id,
        "title": f"{feature}: {spec['title']}",
        "source": {
            "type": "generated",
            "method": "from_scratch",
            "feature": feature,
            "requested_profile": requested_profile,
            "resolved_profile": resolved_profile,
        },
        "profile": resolved_profile,
        "feature": feature,
        "priority": _priority_for_spec(str(spec["key"])),
        "contract_version": 1,
        "quality_pilot": {
            "draft": False,
            "generation_mode": "from_scratch",
            "review_required_before_run": False,
            "executable": True,
            "executable_scope": "side_effect_safe_probe",
            "questions": questions,
            "policy_pack": policy["name"],
            "closed_loop_steps": policy["closed_loop_steps"],
            "gates": policy["gates"],
            "triage_categories": policy["triage_categories"],
            "repo_signals": signals,
        },
        "swqa_dimensions": spec["dimensions"],
        "commands": [
            {
                "id": "safe_probe",
                "run": _safe_probe_command(config, candidate=candidate, context={"repo_signals": signals}),
                "expected_exit_code": 0,
            }
        ],
        "expected": spec["expected"],
        "risk_controls": [
            "review_required_before_run",
            "do_not_publish_until_write_gate_passes",
            "do_not_file_product_defect_without_confirmed_fixture_and_evidence",
            "side_effect_boundary_must_be_confirmed",
            "timeout_or_stress_results_require_baseline_before_defect_filing",
        ],
    }


def inspect_repo_signals(config: ProjectConfig) -> dict[str, Any]:
    root = config.root
    readme_text = _first_existing_text(root, ["README.md", "README.rst", "README.txt"], limit=6000)
    pyproject_text = _read_text_if_exists(root / "pyproject.toml", limit=8000)
    package_json_text = _read_text_if_exists(root / "package.json", limit=8000)
    go_mod_text = _read_text_if_exists(root / "go.mod", limit=4000)
    openapi_exists = any((root / name).exists() for name in ["openapi.yaml", "openapi.yml", "swagger.yaml", "swagger.yml"])
    existing_runner_count = len([p for p in config.paths.runners.glob("*") if p.is_file()]) if config.paths.runners.exists() else 0
    existing_case_count = len([*config.paths.cases.glob("*.yaml"), *config.paths.cases.glob("*.yml")]) if config.paths.cases.exists() else 0

    cli_commands = _extract_pyproject_scripts(pyproject_text) + _extract_package_bins(package_json_text)
    project_name = str(config.data.get("project", {}).get("name") or root.name)
    suggested_command = cli_commands[0] if cli_commands else ""
    return {
        "project_name": project_name,
        "detected_profile": _detect_profile(readme_text, pyproject_text, package_json_text, go_mod_text, openapi_exists, cli_commands),
        "suggested_feature": f"{suggested_command} CLI" if suggested_command else project_name,
        "has_readme": bool(readme_text),
        "has_pyproject": bool(pyproject_text),
        "has_package_json": bool(package_json_text),
        "has_go_mod": bool(go_mod_text),
        "has_openapi": openapi_exists,
        "existing_runner_count": existing_runner_count,
        "existing_case_count": existing_case_count,
        "cli_commands": cli_commands[:5],
        "suggested_command": suggested_command,
    }


def scan_repo_inventory(config: ProjectConfig) -> dict[str, Any]:
    ignored_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".quality-pilot",
        ".quality-pilot-project",
        ".qa-project",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
    }
    source_exts = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".java",
        ".kt",
        ".rb",
        ".sh",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".md",
        ".rst",
        ".txt",
    }
    package_names = {
        "README.md",
        "README.rst",
        "README.txt",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "setup.py",
        "setup.cfg",
        "Makefile",
    }
    ignored_files = {".quality-pilot.yaml"}
    ext_counts: dict[str, int] = {}
    root_counts: dict[str, int] = {}
    package_files: list[dict[str, Any]] = []
    source_samples: list[dict[str, Any]] = []
    analyzed_files = 0

    for path in sorted(config.root.rglob("*")):
        rel_parts = path.relative_to(config.root).parts
        if any(part in ignored_dirs for part in rel_parts):
            continue
        if path.is_dir():
            continue
        if path.name in ignored_files:
            continue
        if path.name in package_names:
            package_files.append({"name": path.name, "path": _relative_or_str(path, config.root), "size": path.stat().st_size})
        if path.suffix not in source_exts:
            continue
        analyzed_files += 1
        ext_counts[path.suffix or "<none>"] = ext_counts.get(path.suffix or "<none>", 0) + 1
        top = rel_parts[0] if rel_parts else path.name
        root_counts[top] = root_counts.get(top, 0) + 1
        if len(source_samples) < 40:
            source_samples.append({"path": _relative_or_str(path, config.root), "size": path.stat().st_size})

    code_roots = [
        {"path": path, "count": count}
        for path, count in sorted(root_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
    ]
    return {
        "analyzed_files_count": analyzed_files,
        "extension_counts": dict(sorted(ext_counts.items())),
        "code_roots": code_roots,
        "source_samples": source_samples,
        "package_files": package_files[:24],
        "ignored_runtime_dirs": sorted(ignored_dirs),
    }


def init_missing_inputs(context: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    signals = context.get("repo_signals", {})
    suggested_command = str(signals.get("suggested_command") or "").strip()
    existing_runners = context.get("existing_runners")
    if suggested_command:
        missing.append(
            f"AI Quality Pilot 建議先用 `{suggested_command}` 作為 read-only/dry-run 初始測試入口；若這不是正確入口，請提供要使用的 runner 或 command。"
        )
    elif existing_runners:
        missing.append(
            "AI Quality Pilot 找到既有 runner；請確認哪一個 runner 作為初始測試入口，或回覆可由 AI Quality Pilot 依 profile 選擇。"
        )
    else:
        missing.append(
            "請提供一個 side-effect-safe 的初始測試入口，例如 CLI help/status、dry-run command、parser-only runner 或 repo health check。"
        )
    missing.append(
        "請列出必要 fixture/lab target/輸入檔/credential env 名稱與不可碰範圍；不要貼 secret。若沒有特別限制，回覆「使用 repo-only 與 dry-run 優先」。"
    )
    return missing


def growth_missing_inputs(context: dict[str, Any]) -> list[str]:
    seeds = context.get("growth_seeds") if isinstance(context.get("growth_seeds"), list) else []
    seed_types = sorted({str(seed.get("type")) for seed in seeds if isinstance(seed, dict) and seed.get("type")})
    scope = ", ".join(seed_types) if seed_types else "repo/latest status"
    return [
        (
            f"AI Quality Pilot 會依 {scope} 的最新訊號自行擴散測試；若有大分類優先順序或不可碰範圍，請一次列出。"
            "若沒有，回覆「由 AI Quality Pilot 依風險排序」。"
        ),
        (
            "若 growth 測試需要共用 fixture、lab target、輸入檔或 credential env 名稱，請一次列出分類；"
            "不要貼 secret。若沒有，回覆「repo-only 與 dry-run 優先」。"
        ),
    ]


def fast_mode_assumptions(context: dict[str, Any]) -> list[str]:
    profile = str(context.get("resolved_profile") or context.get("requested_profile") or "auto")
    return [
        "Use repo-only, read-only, dry-run, parser-only, mock, no-op fixture, or help/status paths before any state-changing operation.",
        "Treat missing lab targets, fixture files, credentials, or destructive permissions as advisory lab enhancements, not as PASS or product FAIL.",
        "Never infer or print raw secrets; only refer to credential environment variable names.",
        "Prefer broad SWQA coverage across functional, positive, negative, boundary, invalid input, sibling surface, side-effect-safe, and stress/timeout-risk dimensions.",
        "Stress and timeout-risk cases must be bounded and require a baseline before defect filing.",
        f"Resolve ambiguous profile as `{profile}` and choose the strictest side-effect-safe interpretation.",
    ]


def extract_repro_command(body: str) -> str | None:
    patterns = [
        r"(?im)^\s*(?:quality-pilot\s+)?(?:test[_ -]?command|repro(?:duction)? command|command)\s*:\s*(.+?)\s*$",
        r"(?im)^\s*actual command\s*:\s*(.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            value = match.group(1).strip().strip("`")
            if value:
                return value
    fenced = re.search(r"```(?:bash|sh|shell)?\s*\n(.+?)\n```", body, re.DOTALL | re.IGNORECASE)
    if fenced:
        lines = [line.strip() for line in fenced.group(1).splitlines() if line.strip() and not line.strip().startswith("#")]
        if len(lines) == 1 and not lines[0].startswith("curl "):
            return lines[0]
    return None


def missing_input_questions(item: dict[str, Any], *, has_command: bool) -> list[str]:
    issue_id = item.get("issue_id")
    questions: list[str] = []
    if not has_command:
        questions.append(f"issue #{issue_id} 要用哪個使用者可見指令或 runner 重現？")
    questions.extend(
        [
            "測試需要哪些 fixture、輸入檔、環境變數或 lab target？",
            "成功條件與預期 exit code 是什麼？",
            "哪些操作有副作用，必須改用 dry-run、mock、parser-only 或 no-op fixture？",
            "哪些 sibling command、邊界值與 invalid input 需要一起覆蓋？",
        ]
    )
    return questions


def growth_dimension_specs() -> list[dict[str, Any]]:
    return [
        {
            "key": "exact-reproduction",
            "title": "Exact or nearest reproducible path",
            "dimensions": ["exact_reproduction", "side_effect_safe"],
            "expected": "The reported or inferred behavior is reproduced through a confirmed safe path, or held with a clear missing-input reason.",
        },
        *dimension_specs(),
    ]


def init_dimension_specs() -> list[dict[str, Any]]:
    return [
        {
            "key": "functional-primary",
            "title": "Functional primary behavior path",
            "dimensions": ["functional", "positive", "side_effect_safe"],
            "expected": "The main user-visible function succeeds through a confirmed safe command, API endpoint, or runner.",
        },
        {
            "key": "positive-smoke",
            "title": "Positive smoke path",
            "dimensions": ["positive", "side_effect_safe"],
            "expected": "A minimal valid input or happy-path scenario succeeds with clear observable output.",
        },
        {
            "key": "negative-invalid-input",
            "title": "Negative invalid input path",
            "dimensions": ["negative", "invalid_input", "side_effect_safe"],
            "expected": "Invalid input is rejected clearly without mutating product, repository, tracker, or lab state.",
        },
        {
            "key": "boundary-min-empty-large",
            "title": "Boundary input path",
            "dimensions": ["boundary", "invalid_input", "side_effect_safe"],
            "expected": "Empty, minimum, repeated, maximum, or very large inputs are handled deterministically.",
        },
        {
            "key": "stress-timeout-baseline",
            "title": "Stress and timeout baseline path",
            "dimensions": ["stress_timeout_risk", "boundary", "side_effect_safe"],
            "expected": "Repeated, large, slow, or timeout-prone work stays bounded and records a baseline before defect filing.",
        },
        {
            "key": "sibling-surface-consistency",
            "title": "Sibling surface consistency path",
            "dimensions": ["sibling_surface", "functional", "negative", "side_effect_safe"],
            "expected": "Adjacent commands, APIs, file types, or modes follow consistent validation and error behavior.",
        },
    ]


def six_hats_for(seed: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:
    seed_title = str(seed.get("title") or seed.get("id") or "signal")
    return {
        "white": f"Observed signal: {seed_title} ({seed.get('type', 'unknown')}).",
        "red": "User risk exists if this behavior silently fails, surprises users, or hides unsafe side effects.",
        "black": f"Regression risk: adjacent flows may share the same parser, validator, state transition, or fixture boundary for {spec['key']}.",
        "yellow": "Value: converts a fresh signal into a repeatable contract before knowledge decays.",
        "green": "Explore sibling surfaces, invalid input, dry-run/no-op paths, and timeout baselines before filing defects.",
        "blue": "add_new_tc",
    }


def load_candidate_json(path: str | Path) -> list[dict[str, Any]]:
    candidate_path = Path(path)
    loaded = json.loads(candidate_path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        raw = loaded.get("candidates")
    else:
        raw = loaded
    if not isinstance(raw, list):
        raise CaseGenerationError("candidate JSON must be a list or an object with candidates[]")
    return [item for item in raw if isinstance(item, dict)]


def normalize_external_candidates(candidates: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        validate_candidate(candidate, index=index)
        seed = candidate.get("growth_seed") if isinstance(candidate.get("growth_seed"), dict) else {
            "id": f"external-{index + 1}",
            "type": "external_candidate",
            "title": str(candidate.get("title") or f"External candidate {index + 1}"),
            "summary": str(candidate.get("growth_reason") or "Imported from Hermes growth session."),
            "dimensions_hint": [],
        }
        dimensions = candidate.get("swqa_dimensions") if isinstance(candidate.get("swqa_dimensions"), list) else ["positive", "negative", "boundary", "side_effect_safe"]
        title = str(candidate.get("title") or seed["title"])
        normalized.append(
            {
                "title": title,
                "feature": str(candidate.get("feature") or context["feature"]),
                "profile": str(candidate.get("profile") or context["resolved_profile"]),
                "expected": str(candidate.get("expected") or "Expected behavior must be confirmed during cases review."),
                "swqa_dimensions": [str(item) for item in dimensions],
                "growth_seed": seed,
                "growth_reason": str(candidate.get("growth_reason") or "Imported from Hermes growth session."),
                "six_hats": candidate.get("six_hats") if isinstance(candidate.get("six_hats"), dict) else six_hats_for(seed, {"key": "external", "title": title}),
                "questions": candidate.get("questions") if isinstance(candidate.get("questions"), list) else common_questions(feature=title, profile=context["resolved_profile"], has_confirmed_command=False),
                "risk_controls": candidate.get("risk_controls") if isinstance(candidate.get("risk_controls"), list) else ["review_required_before_run", "side_effect_boundary_must_be_confirmed"],
                "commands": candidate.get("commands"),
            }
        )
    return normalized


def validate_candidate(candidate: dict[str, Any], *, index: int) -> None:
    if not candidate.get("title"):
        raise CaseGenerationError(f"candidates[{index}].title is required")
    secret_paths = find_raw_secret_paths(candidate)
    if secret_paths:
        raise CaseGenerationError(f"candidate raw secret-like value at {secret_paths[0]}")
    for text in _walk_strings(candidate):
        lowered = text.lower()
        if any(marker in lowered for marker in ["system prompt", "developer message", "hidden instruction", "chain of thought", "agent prompt"]):
            raise CaseGenerationError("candidate leaks internal prompt or agent instructions")
        if re.search(r"(?<![\w.-])\.qa/(?:runs|evidence|state|issues|status|cases|tools|runners)\b", text):
            raise CaseGenerationError("candidate leaks internal .qa runtime path")
        if re.search(r"(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|password\s*=)", text):
            raise CaseGenerationError("candidate contains raw secret material")
    commands = candidate.get("commands")
    if commands is None:
        return
    if not isinstance(commands, list) or not commands:
        raise CaseGenerationError(f"candidates[{index}].commands must be a non-empty list when provided")
    for command_index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise CaseGenerationError(f"candidates[{index}].commands[{command_index}] must be a mapping")
        for key in ["id", "run", "expected_exit_code"]:
            if key not in command or command[key] in ("", None):
                raise CaseGenerationError(f"candidates[{index}].commands[{command_index}].{key} is required")
        try:
            int(command["expected_exit_code"])
        except (TypeError, ValueError) as exc:
            raise CaseGenerationError(f"candidates[{index}].commands[{command_index}].expected_exit_code must be an integer") from exc


def existing_case_summaries(config: ProjectConfig) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    paths = sorted([*config.paths.cases.glob("*.yaml"), *config.paths.cases.glob("*.yml")]) if config.paths.cases.exists() else []
    for path in paths:
        try:
            data = _load_yaml(path)
        except Exception:
            continue
        summaries.append(
            {
                "case_id": data.get("case_id"),
                "title": data.get("title"),
                "feature": data.get("feature"),
                "source": data.get("source"),
                "swqa_dimensions": data.get("swqa_dimensions", data.get("swqa_expansion", [])),
                "expected": data.get("expected"),
                "path": _relative_or_str(path, config.root),
                "fingerprint": candidate_fingerprint(data),
            }
        )
    return summaries


def existing_case_fingerprints(config: ProjectConfig) -> set[str]:
    return {item["fingerprint"] for item in existing_case_summaries(config) if item.get("fingerprint")}


def file_summaries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for item in sorted(child for child in path.iterdir() if child.is_file())[:24]:
        out.append({"name": item.name, "path": str(item), "size": item.stat().st_size})
    return out


def load_state_json(config: ProjectConfig, name: str) -> dict[str, Any] | None:
    path = config.paths.state / name
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "invalid_json", "path": _relative_or_str(path, config.root)}
    return loaded if isinstance(loaded, dict) else {"status": "not_object", "path": _relative_or_str(path, config.root)}


def summarize_latest_run(run: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(run, dict):
        return {"exists": False}
    results = [item for item in run.get("results", []) if isinstance(item, dict)]
    interesting = [
        {
            "case_id": item.get("case_id"),
            "title": item.get("title"),
            "status": item.get("status"),
            "exit_code": item.get("exit_code"),
            "contract_hash": item.get("contract_hash"),
        }
        for item in results
        if item.get("status") in {"FAIL", "BLOCK", "ABORT", "NOT_RUN"}
    ]
    return {
        "exists": True,
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "case_counts": run.get("case_counts"),
        "report_path": run.get("report_path"),
        "interesting_results": interesting,
    }


def summarize_publish_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {"exists": False}
    actions = [item for item in plan.get("actions", []) if isinstance(item, dict)]
    return {
        "exists": True,
        "status": plan.get("status"),
        "blocked_by_gate": plan.get("blocked_by_gate"),
        "action_count": len(actions),
        "actions": [{"id": item.get("id"), "type": item.get("type"), "issue_id": item.get("issue_id")} for item in actions],
    }


def collect_pr_references(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in items:
        for pr in item.get("pull_requests", []) if isinstance(item.get("pull_requests"), list) else []:
            if isinstance(pr, dict):
                refs.append(
                    {
                        "issue_id": item.get("issue_id"),
                        "number": pr.get("number") or pr.get("index") or pr.get("id"),
                        "title": pr.get("title") or pr.get("html_url") or pr.get("url"),
                        "state": pr.get("state"),
                        "url": pr.get("html_url") or pr.get("url"),
                    }
                )
    return refs


def issue_snapshot_path_exists(config: ProjectConfig) -> bool:
    return (config.paths.state / "issues-snapshot.json").exists()


def init_context_path(config: ProjectConfig) -> Path:
    return config.paths.state / INIT_CONTEXT_NAME


def growth_context_path(config: ProjectConfig) -> Path:
    return config.paths.state / GROWTH_CONTEXT_NAME


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    source = {
        "title": candidate.get("title"),
        "feature": candidate.get("feature"),
        "expected": candidate.get("expected"),
        "swqa_dimensions": candidate.get("swqa_dimensions", candidate.get("swqa_expansion", [])),
    }
    return hashlib.sha1(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(_walk_strings(str(key)))
            out.extend(_walk_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_walk_strings(item))
        return out
    return []


def _compact_text(value: str, *, limit: int = 480) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit]


def _resolve_profile(profile: str, signals: dict[str, Any]) -> str:
    if profile != "auto":
        return profile
    detected = signals.get("detected_profile")
    return str(detected or "repo")


def _feature_name(config: ProjectConfig, feature: str | None, signals: dict[str, Any]) -> str:
    if feature and feature.strip():
        return feature.strip()
    value = signals.get("suggested_feature")
    if isinstance(value, str) and value.strip():
        return value.strip()
    project = config.data.get("project", {})
    if isinstance(project, dict) and project.get("name"):
        return str(project["name"])
    return config.root.name


def _select_dimension_specs(count: int) -> list[dict[str, Any]]:
    specs = dimension_specs()
    selected: list[dict[str, Any]] = []
    while len(selected) < count:
        selected.extend(specs)
    return selected[:count]


def safe_commands_for_generated_case(
    config: ProjectConfig,
    *,
    case_id: str,
    candidate: dict[str, Any],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "id": "safe_probe",
            "run": _safe_probe_command(config, candidate=candidate, context=context),
            "expected_exit_code": 0,
        }
    ]


def _safe_probe_command(config: ProjectConfig, *, candidate: dict[str, Any], context: dict[str, Any]) -> str:
    dimensions = {str(item) for item in candidate.get("swqa_dimensions", []) if item}
    go_cli = _first_go_cli_package(config)
    if go_cli:
        package = go_cli["package"]
        command_name = go_cli["name"]
        if dimensions & {"negative", "invalid_input"}:
            return _shell_command(f"go run {shlex.quote(package)} __quality_pilot_invalid_command__ >/dev/null 2>&1; test $? -ne 0")
        if "stress_timeout_risk" in dimensions:
            return _shell_command(f"for i in 1 2 3; do go run {shlex.quote(package)} --help >/dev/null || exit 1; done")
        if "sibling_surface" in dimensions:
            subcommands = _readme_help_subcommands(config, command_name)
            if subcommands:
                checks = " && ".join(
                    f"go run {shlex.quote(package)} {shlex.quote(subcommand)} --help >/dev/null"
                    for subcommand in subcommands[:3]
                )
                return _shell_command(checks)
        return f"go run {shlex.quote(package)} --help"

    if (config.root / "go.mod").exists():
        return "go test ./... -run '^$'"

    python_targets = _python_compile_targets(config)
    if python_targets:
        return "python3 -m compileall -q " + " ".join(shlex.quote(target) for target in python_targets)

    seed = candidate.get("init_seed") if isinstance(candidate.get("init_seed"), dict) else candidate.get("growth_seed")
    surface = str(seed.get("surface") or "") if isinstance(seed, dict) else ""
    if surface and (config.root / surface).exists():
        code = f"from pathlib import Path; assert Path({surface!r}).exists(); print('AI Quality Pilot safe probe: {surface}')"
        return "python3 -c " + shlex.quote(code)

    repo_probe_targets = ["README.md", "README.rst", "README.txt", "go.mod", "pyproject.toml", "package.json", ".quality-pilot.yaml"]
    code = (
        "from pathlib import Path; "
        f"targets={repo_probe_targets!r}; "
        "assert any(Path(item).exists() for item in targets); "
        "print('AI Quality Pilot safe repo probe ok')"
    )
    return "python3 -c " + shlex.quote(code)


def _first_go_cli_package(config: ProjectConfig) -> dict[str, str] | None:
    cmd_dir = config.root / "cmd"
    if not cmd_dir.exists():
        return None
    for child in sorted(cmd_dir.iterdir()):
        if child.is_dir() and (child / "main.go").exists():
            return {"name": child.name, "package": f"./cmd/{child.name}"}
    return None


def _readme_help_subcommands(config: ProjectConfig, command_name: str) -> list[str]:
    text = _first_existing_text(config.root, ["README.md", "README.rst", "README.txt"], limit=80000)
    if not text:
        return []
    pattern = re.compile(rf"(?:\./)?{re.escape(command_name)}\s+([A-Za-z0-9_-]+)\s+--help")
    out: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        value = match.group(1)
        if value in {"--help", "help"} or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _python_compile_targets(config: ProjectConfig) -> list[str]:
    ignored = {".git", ".quality-pilot", ".quality-pilot-project", ".qa-project", "__pycache__", ".venv", "venv", "node_modules"}
    targets: list[str] = []
    for child in sorted(config.root.iterdir()):
        if child.name in ignored or child.name.startswith("."):
            continue
        if child.is_file() and child.suffix == ".py":
            targets.append(child.name)
        elif child.is_dir() and any(path.suffix == ".py" for path in child.rglob("*.py")):
            targets.append(child.name)
        if len(targets) >= 6:
            break
    return targets


def _shell_command(script: str) -> str:
    return "sh -c " + shlex.quote(script)


def _draft_blocker_command(case_id: str) -> str:
    return (
        "python3 -c \"import sys; "
        f"sys.stderr.write('AI Quality Pilot draft {case_id} requires cases review before execution\\\\n'); "
        "sys.exit(2)\""
    )


def _priority_for_spec(key: str) -> str:
    if key in {"positive", "negative-invalid-option"}:
        return "P1"
    return "P2"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").upper()
    return slug[:48] or "SCRATCH"


def _first_existing_text(root: Path, names: list[str], *, limit: int) -> str:
    for name in names:
        text = _read_text_if_exists(root / name, limit=limit)
        if text:
            return text
    return ""


def _read_text_if_exists(path: Path, *, limit: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _extract_pyproject_scripts(text: str) -> list[str]:
    if not text:
        return []
    match = re.search(r"(?ms)^\[project\.scripts\]\s*(.+?)(?:^\[|\Z)", text)
    if not match:
        return []
    names: list[str] = []
    for line in match.group(1).splitlines():
        script = re.match(r"\s*([A-Za-z0-9_.-]+)\s*=", line)
        if script:
            names.append(script.group(1))
    return names


def _extract_package_bins(text: str) -> list[str]:
    if not text:
        return []
    names: list[str] = []
    bin_object = re.search(r'"bin"\s*:\s*\{(.+?)\}', text, re.DOTALL)
    if bin_object:
        names.extend(re.findall(r'"([^"]+)"\s*:', bin_object.group(1)))
    bin_string = re.search(r'"bin"\s*:\s*"([^"]+)"', text)
    name_string = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    if bin_string and name_string:
        names.append(name_string.group(1))
    return names


def _detect_profile(
    readme_text: str,
    pyproject_text: str,
    package_json_text: str,
    go_mod_text: str,
    openapi_exists: bool,
    cli_commands: list[str],
) -> str:
    combined = " ".join([readme_text, pyproject_text, package_json_text, go_mod_text]).lower()
    if openapi_exists or any(word in combined for word in ["openapi", "swagger", "rest api", "graphql"]):
        return "api"
    if any(word in combined for word in ["hardware", "device", "lab target", "bmc", "redfish"]):
        return "hardware"
    if cli_commands or any(word in combined for word in ["command line", "cli", "console_scripts", "usage:"]):
        return "cli"
    return "repo"


def _dump_yaml(data: dict[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return json_dumps(data) + "\n"


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        import json

        loaded = json.loads(path.read_text(encoding="utf-8"))
    else:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
