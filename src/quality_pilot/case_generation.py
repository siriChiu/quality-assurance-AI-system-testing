from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .command_policy import generated_contract_policy_violation_message, validate_generated_contract_commands
from .config import ProjectConfig, find_raw_secret_paths, json_dumps
from .contracts import ContractError, load_contracts
from .issues import case_id_for_issue, load_issue_snapshot
from .policy_pack import common_questions, dimension_specs, policy_pack
from .redmine import (
    RedmineError,
    build_redmine_qa_summary_payload,
    import_redmine_issues,
    redmine_issue_qa_summary,
    redmine_safe_probe_command,
)
from .runner import utc_now
from .runtime_profile import primary_runtime_binary, runtime_profile_status

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class CaseGenerationError(RuntimeError):
    pass


SUPPORTED_PROFILES = {"auto", "cli", "api", "hardware", "repo"}
DEFAULT_SCRATCH_COUNT = 20
GROWTH_CANDIDATE_MULTIPLIER = 8
GROWTH_CANDIDATE_MIN_BUDGET = 80
GROWTH_CANDIDATE_MAX_BUDGET = 240
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
    runtime_profile = runtime_profile_status(config)
    if runtime_profile.get("needs_user_input"):
        return _runtime_profile_required_generation_payload(
            source="issues",
            mode="issues",
            context={"issue_id": issue_id, "item_count": len(items)},
            context_path=config.paths.state / "issues-snapshot.json",
            root=config.root,
            context_path_key="snapshot_path",
            candidates=[],
            runtime_profile=runtime_profile,
            message=(
                "Runtime profile must be confirmed before issue-linked executable case contracts are generated. "
                "No placeholder case YAML was written."
            ),
        )

    for item in items:
        contract = draft_contract_for_issue(config, item)
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        _enforce_generated_contract_commands(config, contract)
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
    runtime_profile = runtime_profile_status(config)
    if runtime_profile.get("needs_user_input"):
        return _runtime_profile_required_generation_payload(
            source="from_scratch",
            mode="from_scratch",
            context={"feature": feature_name, "profile": profile, "repo_signals": signals},
            context_path=config.paths.state / "scratch-context.json",
            root=config.root,
            context_path_key="scratch_context_path",
            candidates=[],
            runtime_profile=runtime_profile,
            count=count,
            message=(
                "Runtime profile must be confirmed before executable scratch case contracts are generated. "
                "No placeholder case YAML was written."
            ),
        )

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
        _enforce_generated_contract_commands(config, contract)
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
    runtime_profile = runtime_profile_status(config)
    if runtime_profile.get("needs_user_input"):
        return _runtime_profile_required_generation_payload(
            source="init",
            mode="init",
            context=context,
            context_path=context_path,
            root=config.root,
            context_path_key="init_context_path",
            candidates=candidates,
            runtime_profile=runtime_profile,
            count=count,
            fast=fast,
            message=(
                "Runtime profile must be confirmed before executable init case contracts are generated. "
                "No placeholder case YAML was written."
            ),
        )
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    deduped: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    seen = existing_case_fingerprints(config)
    in_batch: set[str] = set()
    seen_command_runs = existing_case_command_runs(config)
    in_batch_command_runs: set[str] = set()
    existing_coverage_count = 0

    for index, candidate in enumerate(candidates, start=1):
        if count is not None and len(generated) + existing_coverage_count >= count:
            break
        fingerprint = candidate_fingerprint(candidate)
        if fingerprint in seen or fingerprint in in_batch:
            if fingerprint in seen:
                existing_coverage_count += 1
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
            existing_coverage_count += 1
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        _enforce_generated_contract_commands(config, contract)
        duplicate_command = _duplicate_generated_command_reason(contract, seen_command_runs, in_batch_command_runs)
        if duplicate_command:
            if duplicate_command.get("reason") == "duplicate_existing_command":
                existing_coverage_count += 1
            deduped.append(
                {
                    "case_id": contract["case_id"],
                    "title": contract.get("title"),
                    "fingerprint": fingerprint,
                    **duplicate_command,
                }
            )
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        in_batch_command_runs.update(_contract_command_runs(contract))
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
        "existing_coverage_count": existing_coverage_count,
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

    runtime_profile = runtime_profile_status(config)
    if runtime_profile.get("needs_user_input"):
        payload = _runtime_profile_required_generation_payload(
            source="growth",
            mode="growing",
            context=context,
            context_path=context_path,
            root=config.root,
            context_path_key="growth_context_path",
            candidates=candidates,
            runtime_profile=runtime_profile,
            count=count,
            fast=fast,
            message=(
                "Runtime profile must be confirmed before executable growth case contracts are generated. "
                "No placeholder case YAML was written."
            ),
        )
        payload["growth_seed_count"] = len(context["growth_seeds"])
        payload["growth_sensor_summary"] = context.get("growth_sensor_summary", {})
        payload["candidate_source"] = candidate_source
        return payload

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    deduped: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    seen = existing_case_fingerprints(config)
    in_batch: set[str] = set()
    seen_command_runs = existing_case_command_runs(config)
    in_batch_command_runs: set[str] = set()
    existing_coverage_count = 0

    for index, candidate in enumerate(candidates, start=1):
        if count is not None and len(generated) >= count:
            break
        fingerprint = candidate_fingerprint(candidate)
        if fingerprint in seen or fingerprint in in_batch:
            if fingerprint in seen:
                existing_coverage_count += 1
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
            existing_coverage_count += 1
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        _enforce_generated_contract_commands(config, contract)
        duplicate_command = _duplicate_generated_command_reason(contract, seen_command_runs, in_batch_command_runs)
        if duplicate_command:
            if duplicate_command.get("reason") == "duplicate_existing_command":
                existing_coverage_count += 1
            deduped.append(
                {
                    "case_id": contract["case_id"],
                    "title": contract.get("title"),
                    "fingerprint": fingerprint,
                    **duplicate_command,
                }
            )
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        in_batch_command_runs.update(_contract_command_runs(contract))
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
                "swqa_operation": contract.get("quality_pilot", {}).get("swqa_operation", {}),
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
        "growth_sensor_summary": context.get("growth_sensor_summary", {}),
        "candidate_source": candidate_source,
        "candidate_count": len(candidates),
        "generated": generated,
        "skipped": skipped,
        "deduped": deduped,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "deduped_count": len(deduped),
        "existing_coverage_count": existing_coverage_count,
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


def _runtime_profile_required_generation_payload(
    *,
    source: str,
    mode: str,
    context: dict[str, Any],
    context_path: Path,
    root: Path,
    context_path_key: str,
    candidates: list[dict[str, Any]],
    runtime_profile: dict[str, Any],
    count: int | None = None,
    fast: bool = False,
    message: str = "",
) -> dict[str, Any]:
    questions = runtime_profile.get("questions") if isinstance(runtime_profile.get("questions"), list) else []
    missing_inputs = [str(item.get("prompt") or item.get("id") or "") for item in questions if isinstance(item, dict)]
    payload = {
        "status": "needs_input",
        "source": source,
        "mode": mode,
        "fast": bool(fast),
        "interaction_scope": "runtime_profile_required",
        "generation_strategy": "blocked_until_runtime_profile_confirmed",
        "generation_limit": "all_init_seed_dimension_pairs" if mode == "init" and count is None else "manual_generated_count_cap",
        "generated_count_limit": count,
        "requested_generated_count": count,
        "requested_count": count,
        "assumption_policy": (
            "AI Quality Pilot analyzed the repo but will not create placeholder executable cases until the runtime profile is confirmed."
        ),
        "fast_mode_assumptions": context.get("fast_mode_assumptions", []),
        "feature": context.get("feature"),
        "requested_profile": context.get("requested_profile"),
        "resolved_profile": context.get("resolved_profile"),
        context_path_key: _relative_or_str(context_path, root),
        "analyzed_files_count": context.get("repo_inventory", {}).get("analyzed_files_count", 0),
        "candidate_count": len(candidates),
        "generated": [],
        "skipped": [],
        "deduped": [],
        "generated_count": 0,
        "skipped_count": 0,
        "deduped_count": 0,
        "questions": [],
        "missing_inputs": [item for item in missing_inputs if item],
        "missing_input_count": len([item for item in missing_inputs if item]),
        "advisory_inputs": [],
        "advisory_input_count": 0,
        "runtime_profile": runtime_profile,
        "repo_analysis": runtime_profile.get("repo_analysis", {}),
        "input_required": True,
        "interaction": {
            "type": "needs_input",
            "title": "Runtime profile setup",
            "field": "payload.hermes_needs_input",
            "handler": "clarify",
        },
        "hermes_needs_input": {
            "status": "required",
            "title": "Runtime profile setup",
            "language": "zh-Hant",
            "mode": "questionnaire",
            "reason": "runtime_profile_missing",
            "preferred_mechanism": "clarify",
            "clarify": {
                "tool": "clarify",
                "mode": "one_question_at_a_time",
                "question_field": "prompt",
            },
            "questions": questions,
            "answer_format": (
                "請用條列式回覆，例如：\n"
                "- binary/runner/API: ...\n"
                "- fixture/config: ...\n"
                "- credential env: ...\n"
                "- target/resource: ...\n"
                "- side-effect boundary: ..."
            ),
            "ui_hint": "Do not generate placeholder executable cases. Ask once for the runtime profile after repo analysis.",
            "repo_analysis": runtime_profile.get("repo_analysis", {}),
        },
        "message": message,
    }
    if isinstance(context.get("policy_pack"), dict):
        payload["policy_pack"] = context["policy_pack"].get("name")
        payload["policy_dimensions"] = context["policy_pack"].get("swqa_dimensions", [])
        payload["closed_loop_steps"] = context["policy_pack"].get("closed_loop_steps", [])
    return payload


def generate_cases_from_redmine_issues(
    config: ProjectConfig,
    *,
    issue_ids: list[int],
    force: bool = False,
) -> dict[str, Any]:
    if not issue_ids:
        raise CaseGenerationError("--redmine-issues requires at least one issue id")
    imported = import_redmine_issues(config, issue_ids=issue_ids)
    qa_summary = build_redmine_qa_summary_payload(config, imported["issues"])
    runtime_profile = runtime_profile_status(config)
    if runtime_profile.get("needs_user_input") and any(not _has_user_confirmed_redmine_runner(issue) for issue in imported["issues"]):
        return _runtime_profile_required_generation_payload(
            source="redmine_issues",
            mode="redmine_issues",
            context={"redmine_issue_ids": issue_ids, "qa_summary": qa_summary},
            context_path=config.paths.state / "redmine-mcp" / "issues.json",
            root=config.root,
            context_path_key="redmine_mcp_issues_json",
            candidates=[],
            runtime_profile=runtime_profile,
            message=(
                "Runtime profile or a user-confirmed Redmine safe runner is required before Redmine executable case contracts are generated. "
                "No placeholder case YAML was written."
            ),
        )
    config.paths.cases.mkdir(parents=True, exist_ok=True)
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for issue in imported["issues"]:
        contract = draft_contract_for_redmine_issue(config, issue)
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            if _is_stale_redmine_generic_probe(path):
                contract.setdefault("quality_pilot", {})["regenerated_from_stale_generic_probe"] = True
                contract.setdefault("quality_pilot", {})["regeneration_reason"] = "redmine_generic_invalid_command_probe"
            else:
                skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
                continue
        _enforce_generated_contract_commands(config, contract)
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        generated.append(
            {
                "case_id": contract["case_id"],
                "redmine_issue_id": issue["id"],
                "path": _relative_or_str(path, config.root),
                "draft": False,
                "question_count": 0,
                "swqa_dimensions": contract.get("swqa_dimensions", []),
                "safe_command_source": contract.get("quality_pilot", {}).get("safe_command_source"),
                "safe_command_source_type": contract.get("quality_pilot", {}).get("safe_command_source_type"),
                "automation_confidence": contract.get("quality_pilot", {}).get("automation_confidence"),
                "requires_prepared_environment": bool(contract.get("quality_pilot", {}).get("requires_prepared_environment")),
                "regenerated_from_stale_generic_probe": bool(contract.get("quality_pilot", {}).get("regenerated_from_stale_generic_probe")),
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
        "qa_summary": qa_summary,
        "generated": generated,
        "skipped": skipped,
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "message": "Redmine MCP issues were analyzed and executable linked cases were generated with user-confirmed or AI-derived product-binary probes and explicit environment requirements.",
    }


def _is_stale_redmine_generic_probe(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    lowered = text.lower()
    return "__quality_pilot_invalid_command__" in text and ("redmine" in lowered or "redmine_issue_id" in lowered)


def _enforce_generated_contract_commands(config: ProjectConfig, contract: dict[str, Any]) -> None:
    violations = validate_generated_contract_commands(config, contract)
    if violations:
        raise CaseGenerationError(generated_contract_policy_violation_message(str(contract.get("case_id") or ""), violations))


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
    repo_inventory = scan_repo_inventory(config)
    git_history = summarize_git_history(config)
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
        "repo_inventory": repo_inventory,
        "git_history": git_history,
        "existing_runners": file_summaries(config.paths.runners),
        "project_rules": file_summaries(config.paths.rules),
    }
    context["growth_seeds"] = build_growth_seeds(context)
    context["growth_sensor_summary"] = growth_sensor_summary(context)
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
    readme_operation_seed_count = 0
    for surface in signals.get("readme_cli_operations", [])[:16]:
        args = surface.get("args") if isinstance(surface, dict) else None
        if not isinstance(args, list) or not args:
            continue
        label = str(surface.get("label") or " ".join(str(item) for item in args))
        seeds.append(
            {
                "id": f"readme-op-{_slug(label).lower()}",
                "type": "readme_cli_operation",
                "title": label,
                "summary": "Read-only product CLI operation documented in README; requires prepared runtime fixture when credentials or target are needed.",
                "surface": label,
                "command_args": [str(item) for item in args],
                "command_source": surface.get("source") or "README",
                "requires_prepared_environment": bool(surface.get("requires_prepared_environment")),
                "environment_requirements": surface.get("environment_requirements", []),
            }
        )
        readme_operation_seed_count += 1
    if readme_operation_seed_count == 0:
        for surface in signals.get("readme_cli_surfaces", [])[:16]:
            args = surface.get("args") if isinstance(surface, dict) else None
            if not isinstance(args, list) or not args:
                continue
            label = str(surface.get("label") or " ".join(str(item) for item in args))
            seeds.append(
                {
                    "id": f"readme-cli-{_slug(label).lower()}",
                    "type": "readme_cli_surface",
                    "title": label,
                    "summary": "Side-effect-safe product CLI surface documented in README.",
                    "surface": label,
                    "command_args": [str(item) for item in args],
                    "command_source": surface.get("source") or "README",
                }
            )
    for command in signals.get("cli_commands", [])[:4]:
        seeds.append(
            {
                "id": f"cli-{_slug(command).lower()}",
                "type": "cli_command",
                "title": f"{command} command surface",
                "summary": f"Exercise the discovered CLI entry point `{command}` with safe probes.",
                "surface": command,
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
    for spec in specs:
        for seed in seeds:
            candidates.append(candidate_from_init_seed(context, seed, spec))
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
    seed = candidate["init_seed"]
    executable_scope = (
        "prepared_environment_readonly_product_command"
        if seed.get("type") == "readme_cli_operation"
        else "side_effect_safe_probe"
    )
    environment_requirements = seed.get("environment_requirements") if isinstance(seed.get("environment_requirements"), list) else []
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
            "executable_scope": executable_scope,
            "safe_command_source": seed.get("command_source") or "",
            "requires_prepared_environment": bool(seed.get("requires_prepared_environment")),
            "environment_requirements": environment_requirements,
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

    for pr in context.get("pr_references", [])[:24]:
        number = pr.get("number") or pr.get("id") or "unknown"
        title = str(pr.get("title") or f"PR {number}")
        seeds.append(
            {
                "id": f"pr-{number}",
                "type": "pr_reference",
                "title": title,
                "summary": f"Linked Gitea PR reference from synced issue state: {title}",
                "issue_id": pr.get("issue_id"),
                "pr_number": number,
                "url": pr.get("url"),
                "dimensions_hint": ["exact_reproduction", "sibling_surface", "boundary", "side_effect_safe"],
            }
        )

    git_history = context.get("git_history") if isinstance(context.get("git_history"), dict) else {}
    for commit in git_history.get("recent_commits", [])[:30] if isinstance(git_history.get("recent_commits"), list) else []:
        if not isinstance(commit, dict):
            continue
        short_hash = str(commit.get("short_hash") or commit.get("hash") or "commit")[:12]
        subject = str(commit.get("subject") or short_hash)
        touched_roots = commit.get("touched_roots") if isinstance(commit.get("touched_roots"), list) else []
        seeds.append(
            {
                "id": f"git-{_slug(short_hash).lower()}",
                "type": "git_commit",
                "title": subject,
                "summary": _compact_text(
                    f"Recent commit {short_hash} touched {', '.join(str(item) for item in touched_roots[:5]) or 'unknown files'}."
                ),
                "commit": short_hash,
                "touched_files": commit.get("touched_files", [])[:12] if isinstance(commit.get("touched_files"), list) else [],
                "touched_roots": touched_roots[:8],
                "issue_refs": commit.get("issue_refs", []) if isinstance(commit.get("issue_refs"), list) else [],
                "pr_refs": commit.get("pr_refs", []) if isinstance(commit.get("pr_refs"), list) else [],
                "dimensions_hint": ["sibling_surface", "boundary", "stress_timeout_risk", "side_effect_safe"],
            }
        )

    inventory = context.get("repo_inventory") if isinstance(context.get("repo_inventory"), dict) else {}
    for code_root in inventory.get("code_roots", [])[:16] if isinstance(inventory.get("code_roots"), list) else []:
        if not isinstance(code_root, dict):
            continue
        root_name = str(code_root.get("path") or "").strip()
        if not root_name:
            continue
        seeds.append(
            {
                "id": f"code-root-{_slug(root_name).lower()}",
                "type": "code_root",
                "title": root_name,
                "summary": f"{code_root.get('count')} source files under `{root_name}`; expand adjacent behavior and regression probes.",
                "surface": root_name,
                "dimensions_hint": ["sibling_surface", "boundary", "invalid_input", "stress_timeout_risk"],
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

    readme_operation_seed_count = 0
    for surface in context["repo_signals"].get("readme_cli_operations", [])[:8]:
        args = surface.get("args") if isinstance(surface, dict) else None
        if not isinstance(args, list) or not args:
            continue
        label = str(surface.get("label") or " ".join(str(item) for item in args))
        seeds.append(
            {
                "id": f"readme-op-{_slug(label).lower()}",
                "type": "readme_cli_operation",
                "title": label,
                "summary": "Read-only product CLI operation documented in README; requires prepared runtime fixture when credentials or target are needed.",
                "feature": label,
                "command_args": [str(item) for item in args],
                "command_source": surface.get("source") or "README",
                "requires_prepared_environment": bool(surface.get("requires_prepared_environment")),
                "environment_requirements": surface.get("environment_requirements", []),
                "dimensions_hint": ["functional", "positive", "side_effect_safe"],
            }
        )
        readme_operation_seed_count += 1

    if readme_operation_seed_count == 0:
        for surface in context["repo_signals"].get("readme_cli_surfaces", [])[:8]:
            args = surface.get("args") if isinstance(surface, dict) else None
            if not isinstance(args, list) or not args:
                continue
            label = str(surface.get("label") or " ".join(str(item) for item in args))
            seeds.append(
                {
                    "id": f"readme-cli-{_slug(label).lower()}",
                    "type": "readme_cli_surface",
                    "title": label,
                    "summary": "Side-effect-safe product CLI surface documented in README.",
                    "feature": label,
                    "command_args": [str(item) for item in args],
                    "command_source": surface.get("source") or "README",
                "dimensions_hint": ["functional", "side_effect_safe", "sibling_surface"],
            }
        )

    seeds.extend(_monkey_cli_sweep_seeds(context))

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


def _monkey_cli_sweep_seeds(context: dict[str, Any]) -> list[dict[str, Any]]:
    surfaces = []
    for surface in context.get("repo_signals", {}).get("readme_cli_surfaces", []):
        if not isinstance(surface, dict):
            continue
        args = surface.get("args")
        if not isinstance(args, list) or not args:
            continue
        normalized = [str(item) for item in args]
        lowered = [item.lower() for item in normalized]
        if lowered[-1:] == ["--help"] or lowered in (["--version"], ["-v"]):
            surfaces.append({
                "args": normalized,
                "label": str(surface.get("label") or _readme_cli_surface_label(normalized)),
                "source": surface.get("source") or "README",
            })
    if not surfaces:
        surfaces = [{"args": ["--help"], "label": "root help", "source": "runtime profile"}]

    seeds: list[dict[str, Any]] = []
    chunk_size = 4
    for index in range(0, min(len(surfaces), 32), chunk_size):
        group = surfaces[index:index + chunk_size]
        labels = [str(item["label"]) for item in group]
        seeds.append(
            {
                "id": f"monkey-cli-sweep-{index // chunk_size + 1}",
                "type": "monkey_cli_help_sweep",
                "title": f"Bounded monkey CLI sweep {index // chunk_size + 1}",
                "summary": "Deterministic side-effect-safe monkey sweep across documented CLI help/version surfaces.",
                "command_groups": [item["args"] for item in group],
                "surface": ", ".join(labels),
                "command_source": "README/runtime profile",
                "dimensions_hint": ["monkey", "sibling_surface", "side_effect_safe", "stress_timeout_risk"],
            }
        )
    return seeds


def growth_sensor_summary(context: dict[str, Any]) -> dict[str, Any]:
    seeds = context.get("growth_seeds") if isinstance(context.get("growth_seeds"), list) else []
    seed_counts: dict[str, int] = {}
    for seed in seeds:
        if not isinstance(seed, dict):
            continue
        seed_type = str(seed.get("type") or "unknown")
        seed_counts[seed_type] = seed_counts.get(seed_type, 0) + 1
    git_history = context.get("git_history") if isinstance(context.get("git_history"), dict) else {}
    repo_inventory = context.get("repo_inventory") if isinstance(context.get("repo_inventory"), dict) else {}
    return {
        "seed_count": len(seeds),
        "seed_counts": dict(sorted(seed_counts.items())),
        "issue_count": context.get("issue_snapshot", {}).get("open_count", 0),
        "pr_reference_count": len(context.get("pr_references", []) if isinstance(context.get("pr_references"), list) else []),
        "recent_commit_count": git_history.get("recent_commit_count", 0),
        "repo_analyzed_files_count": repo_inventory.get("analyzed_files_count", 0),
        "monkey_seed_count": seed_counts.get("monkey_cli_help_sweep", 0),
    }


def build_growth_candidates(context: dict[str, Any], *, count: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    specs = growth_dimension_specs()
    budget = min(
        GROWTH_CANDIDATE_MAX_BUDGET,
        max(GROWTH_CANDIDATE_MIN_BUDGET, count * GROWTH_CANDIDATE_MULTIPLIER),
    )
    for seed in context.get("growth_seeds", []):
        for spec in specs:
            for operation in swqa_growth_operations_for_seed(seed, spec):
                candidates.append(candidate_from_seed(context, seed, spec, operation=operation))
                if len(candidates) >= budget:
                    return candidates
    return candidates


def swqa_growth_operations_for_seed(seed: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    key = str(spec.get("key") or "")
    seed_type = str(seed.get("type") or "")
    operations: list[dict[str, Any]] = []

    if seed_type == "monkey_cli_help_sweep":
        operations.extend([
            _swqa_operation("monkey_help_sweep", "Bounded monkey help sweep", ["monkey", "sibling_surface", "side_effect_safe"]),
            _swqa_operation("monkey_repeatability", "Repeated monkey help sweep", ["monkey", "stress_timeout_risk", "side_effect_safe"]),
            _swqa_operation("monkey_concurrency", "Concurrent monkey help sweep", ["monkey", "stress_timeout_risk", "side_effect_safe"]),
        ])
        return operations

    if key in {"exact-reproduction", "functional-primary", "positive-smoke", "positive"}:
        operations.append(_swqa_operation("surface_probe", "Read-only surface probe", ["functional", "positive", "side_effect_safe"]))
    if key in {"negative-invalid-input", "negative-invalid-option"} or "negative" in spec.get("dimensions", []):
        operations.append(_swqa_operation("invalid_option_rejection", "Invalid option rejection", ["negative", "invalid_input", "side_effect_safe"]))
    if key.startswith("boundary") or "boundary" in spec.get("dimensions", []):
        operations.append(_swqa_operation("boundary_invalid_value", "Boundary invalid value rejection", ["boundary", "invalid_input", "side_effect_safe"]))
    if key.startswith("sibling") or "sibling_surface" in spec.get("dimensions", []):
        operations.append(_swqa_operation("sibling_help_sweep", "Sibling surface sweep", ["sibling_surface", "side_effect_safe"]))
    if key.startswith("stress") or "stress_timeout_risk" in spec.get("dimensions", []):
        operations.extend([
            _swqa_operation("repeatability_probe", "Repeated read-only probe", ["stress_timeout_risk", "side_effect_safe"]),
            _swqa_operation("concurrency_probe", "Concurrent read-only probe", ["stress_timeout_risk", "side_effect_safe"]),
            _swqa_operation("timeout_baseline", "Bounded timeout baseline", ["stress_timeout_risk", "boundary", "side_effect_safe"]),
        ])

    if not operations:
        operations.append(_swqa_operation("surface_probe", "Read-only surface probe", ["side_effect_safe"]))
    return operations


def _swqa_operation(key: str, title: str, dimensions: list[str]) -> dict[str, Any]:
    return {"key": key, "title": title, "dimensions": dimensions}


def candidate_from_seed(
    context: dict[str, Any],
    seed: dict[str, Any],
    spec: dict[str, Any],
    *,
    operation: dict[str, Any],
) -> dict[str, Any]:
    operation_title = str(operation.get("title") or operation.get("key") or "SWQA operation")
    title = f"{seed.get('title')}: {spec['title']} - {operation_title}"
    dimensions = sorted(set(spec["dimensions"]) | set(seed.get("dimensions_hint", [])[:1]) | set(operation.get("dimensions", [])))
    return {
        "title": title,
        "feature": seed.get("feature") or context["feature"],
        "profile": context["resolved_profile"],
        "expected": spec["expected"],
        "swqa_dimensions": dimensions,
        "swqa_operation": operation,
        "growth_seed": seed,
        "growth_reason": f"Expand {seed.get('type')} signal through {spec['key']} coverage using {operation.get('key')} operation.",
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
    seed = candidate["growth_seed"]
    executable_scope = (
        "prepared_environment_readonly_product_command"
        if seed.get("type") == "readme_cli_operation"
        else "side_effect_safe_probe"
    )
    environment_requirements = seed.get("environment_requirements") if isinstance(seed.get("environment_requirements"), list) else []
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
            "executable_scope": executable_scope,
            "safe_command_source": seed.get("command_source") or "",
            "requires_prepared_environment": bool(seed.get("requires_prepared_environment")),
            "environment_requirements": environment_requirements,
            "swqa_operation": candidate.get("swqa_operation", {}),
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
    semantic_findings: list[dict[str, Any]] = []
    for path in sorted([*config.paths.cases.glob("*.yaml"), *config.paths.cases.glob("*.yml")]):
        try:
            data = _load_yaml(path)
        except Exception:
            continue
        semantic_findings.extend(_case_contract_semantic_findings(data, path=path, root=config.root))
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
    return {
        "status": "needs_review" if semantic_findings else "ok",
        "draft_count": len(reviews),
        "drafts": reviews,
        "semantic_finding_count": len(semantic_findings),
        "semantic_findings": semantic_findings,
    }


def validate_generated_cases(config: ProjectConfig) -> dict[str, Any]:
    try:
        contracts = load_contracts(config.paths.cases)
    except ContractError as exc:
        return {"status": "error", "error": exc.error, "message": exc.message, "path": exc.path}
    review = review_generated_cases(config)
    return {
        "status": "needs_review" if review.get("semantic_findings") else "ok",
        "case_count": len(contracts),
        "draft_count": review["draft_count"],
        "semantic_finding_count": review["semantic_finding_count"],
        "semantic_findings": review["semantic_findings"],
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


def _case_contract_semantic_findings(data: dict[str, Any], *, path: Path, root: Path) -> list[dict[str, Any]]:
    case_id = str(data.get("case_id") or path.stem)
    if not _is_issue_linked_case_contract(data, case_id):
        return []
    oracle_text = _case_contract_oracle_text(data)
    if not _oracle_text_indicates_rejection(oracle_text):
        return []
    commands = data.get("commands") if isinstance(data.get("commands"), list) else []
    mismatched_commands: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            continue
        expected_exit = _int_or_none(command.get("expected_exit_code"))
        run = str(command.get("run") or "")
        if expected_exit == 0 and not _command_wraps_negative_exit_assertion(run):
            mismatched_commands.append(
                {
                    "index": index,
                    "id": command.get("id"),
                    "expected_exit_code": expected_exit,
                }
            )
    if not mismatched_commands:
        return []
    return [
        {
            "id": "negative_oracle_expected_exit_code_mismatch",
            "case_id": case_id,
            "path": _relative_or_str(path, root),
            "severity": "BLOCKER",
            "commands": mismatched_commands,
            "message": (
                "The case oracle says the product should reject/fail the input, "
                "but at least one command still expects exit code 0."
            ),
            "recommendation": (
                "Set the command expected_exit_code to the product's rejection exit code, "
                "or wrap the command with an explicit non-zero assertion and stderr/stdout oracle."
            ),
        }
    ]


def _is_issue_linked_case_contract(data: dict[str, Any], case_id: str) -> bool:
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    source_type = str(source.get("type") or "").lower()
    return source_type in {"redmine", "issue"} or case_id.startswith(("REDMINE-", "ISSUE-"))


def _case_contract_oracle_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ["title", "expected"]:
        value = data.get(key)
        if isinstance(value, str):
            parts.append(value)
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    qa_summary = source.get("qa_summary") if isinstance(source.get("qa_summary"), dict) else {}
    for key in ["expected", "observed", "problem", "reproduction"]:
        value = qa_summary.get(key)
        if isinstance(value, str):
            parts.append(value)
    for container in [source.get("safe_runner"), data.get("quality_pilot")]:
        if isinstance(container, dict):
            parts.extend(_oracle_values_from_mapping(container))
    commands = data.get("commands") if isinstance(data.get("commands"), list) else []
    for command in commands:
        if isinstance(command, dict):
            for key in ["expected_stdout", "expected_stderr", "stderr_contains", "stdout_contains", "oracle"]:
                value = command.get(key)
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(value, list):
                    parts.extend(str(item) for item in value if str(item).strip())
    return "\n".join(part for part in parts if str(part).strip())


def _oracle_values_from_mapping(value: dict[str, Any]) -> list[str]:
    out: list[str] = []
    user_inputs = value.get("user_confirmed_inputs") if isinstance(value.get("user_confirmed_inputs"), dict) else {}
    oracle = user_inputs.get("oracle") if isinstance(user_inputs.get("oracle"), list) else []
    for item in oracle:
        if isinstance(item, dict):
            out.append(str(item.get("value") or ""))
        else:
            out.append(str(item))
    safe_runner = value.get("safe_runner") if isinstance(value.get("safe_runner"), dict) else {}
    if safe_runner:
        out.extend(_oracle_values_from_mapping(safe_runner))
    return out


def _oracle_text_indicates_rejection(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    patterns = [
        r"\btimeout\s*(?:0|zero)\b",
        r"\bmust be positive\b",
        r"\bshould\s+(?:fail|be rejected|return non[- ]?zero)\b",
        r"\bmust\s+(?:fail|be rejected|return non[- ]?zero)\b",
        r"\bexpected\s+(?:failure|non[- ]?zero|exit code\s+[1-9])\b",
        r"\bstderr\b.*\b(?:invalid|error|must be positive)\b",
        r"(?:應|应该|必須|必须).{0,12}(?:拒絕|拒绝|失敗|失败|非零|錯誤|错误|為正|为正)",
        r"(?:不應|不应|不能|不可).{0,12}(?:成功|開始|开始|通過|通过)",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def _command_wraps_negative_exit_assertion(command: str) -> bool:
    text = str(command or "")
    patterns = [
        r"\btest\s+\$\?\s+-ne\s+0\b",
        r"\[\s+\$\?\s+-ne\s+0\s+\]",
        r"\bif\b.+\bthen\s+exit\s+1\b",
        r"!\s+(?:\"?\$binary\"?|[A-Za-z0-9_./-]+)\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def draft_contract_for_issue(config: ProjectConfig, item: dict[str, Any]) -> dict[str, Any]:
    case_id = case_id_for_issue(item)
    issue_id = int(item.get("issue_id"))
    title = str(item.get("title") or f"Gitea issue #{issue_id}")
    body = str(item.get("body") or "")
    command = extract_repro_command(body)
    questions: list[str] = []
    runtime_binary = primary_runtime_binary(config)
    command_uses_runtime = bool(command and _command_uses_runtime_binary(command, runtime_binary))
    run = command if command_uses_runtime else _runtime_binary_help_command(runtime_binary)
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
            "executable_scope": "issue_reproduction_command" if command_uses_runtime else "runtime_binary_safe_probe",
            "safe_command_source": "issue_reproduction_command" if command_uses_runtime else "runtime_profile_primary_entrypoint",
            "rejected_repro_command": command if command and not command_uses_runtime else "",
            "follow_up_needed": (
                []
                if command_uses_runtime
                else ["Exact issue reproduction command was missing or did not use the configured product executable; add a focused acceptance command before claiming issue coverage."]
            ),
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
    safe_command = _redmine_safe_probe_or_ai_default(config, issue)
    qa_summary = redmine_issue_qa_summary(issue)
    safe_runner = {
        "command": str(safe_command["run"]),
        "command_source": safe_command.get("source"),
        "source_type": safe_command.get("source_type", "user_confirmed"),
        "expected_exit_code": int(safe_command.get("expected_exit_code", 0)),
        "expected_exit_code_source": safe_command.get("expected_exit_code_source"),
        "user_confirmed_inputs": safe_command.get("user_confirmed_inputs", {}),
        "ai_analysis": safe_command.get("ai_analysis", {}),
        "automation_confidence": safe_command.get("automation_confidence", "medium"),
        "follow_up_needed": safe_command.get("follow_up_needed", []),
        "environment_requirements": safe_command.get("environment_requirements", []),
        "rejected_safe_command": safe_command.get("rejected_safe_command"),
    }
    source_type = str(safe_command.get("source_type") or "user_confirmed")
    executable_scope = "redmine_user_confirmed_safe_probe" if source_type == "user_confirmed" else "redmine_ai_derived_safe_probe"
    environment_requirements = safe_runner["environment_requirements"]
    requires_prepared_environment = bool(safe_command.get("requires_prepared_environment", environment_requirements))
    return {
        "case_id": case_id,
        "title": f"Redmine #{issue['id']}: {issue['subject']}",
        "source": {
            "type": "redmine",
            "provider": "redmine",
            "redmine_issue_id": int(issue["id"]),
            "redmine_url": issue.get("url") or "",
            "redmine_message": issue.get("full_message") or issue.get("description") or "",
            "qa_summary": qa_summary,
            "safe_runner": safe_runner,
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
            "executable_scope": executable_scope,
            "safe_command_source": safe_command.get("source"),
            "safe_command_source_type": source_type,
            "safe_runner": safe_runner,
            "automation_confidence": safe_runner["automation_confidence"],
            "follow_up_needed": safe_runner["follow_up_needed"],
            "environment_requirements": environment_requirements,
            "requires_prepared_environment": requires_prepared_environment,
            "questions": [],
            "policy_pack": policy["name"],
            "closed_loop_steps": policy["closed_loop_steps"],
            "gates": policy["gates"],
            "triage_categories": policy["triage_categories"],
        },
        "swqa_dimensions": ["exact_reproduction", "functional", "side_effect_safe"],
        "commands": [
            {
                "id": "safe_probe",
                "run": str(safe_command["run"]),
                "expected_exit_code": int(safe_command.get("expected_exit_code", 0)),
                "requires_prepared_environment": requires_prepared_environment,
            }
        ],
        "expected": qa_summary.get("expected")
        or "The Redmine-reported behavior is covered by the binary-based probe after the required test environment is prepared.",
        "risk_controls": [
            "redmine_mcp_snapshot_must_be_valid",
            "redmine_safe_probe_must_use_product_binary_or_runner",
            "prefer_user_confirmed_safe_probe_when_present",
            "developer_unit_test_commands_are_implementation_hints_only",
            "environment_requirements_must_be_visible_before_free_hand_execution",
            "record_follow_up_gaps_instead_of_blocking_generation",
        ],
    }


def _redmine_safe_probe_or_ai_default(config: ProjectConfig, issue: dict[str, Any]) -> dict[str, Any]:
    confirmed = redmine_safe_probe_command(issue)
    if confirmed is not None:
        if _is_developer_test_command(str(confirmed.get("run") or "")):
            derived = _ai_derived_redmine_safe_probe(config, issue)
            derived["rejected_safe_command"] = {
                "run": confirmed.get("run"),
                "source": confirmed.get("source"),
                "reason": "Developer-level unit/build command is not accepted as a Redmine QA binary contract.",
            }
            derived.setdefault("follow_up_needed", []).append(
                "Redmine safe command was a developer test command; provide a product binary runner only if the derived binary command is not the intended QA entrypoint."
            )
            derived["follow_up_needed"] = _unique_text(derived["follow_up_needed"])
            derived.setdefault("ai_analysis", {})["rejected_safe_command"] = derived["rejected_safe_command"]
            return derived
        out = dict(confirmed)
        out["source_type"] = "user_confirmed"
        out["automation_confidence"] = "high"
        out["environment_requirements"] = _redmine_environment_requirements(config, issue, command=str(out.get("run") or ""))
        out["requires_prepared_environment"] = bool(out["environment_requirements"])
        out["ai_analysis"] = {
            "strategy": "use_redmine_user_confirmed_safe_probe",
            "reason": "Redmine payload includes a safe probe command field.",
        }
        out["follow_up_needed"] = _redmine_follow_up_needed(issue, confirmed=True)
        return out
    return _ai_derived_redmine_safe_probe(config, issue)


def _has_user_confirmed_redmine_runner(issue: dict[str, Any]) -> bool:
    confirmed = redmine_safe_probe_command(issue)
    return bool(confirmed and not _is_developer_test_command(str(confirmed.get("run") or "")))


def _ai_derived_redmine_safe_probe(config: ProjectConfig, issue: dict[str, Any]) -> dict[str, Any]:
    qa_summary = redmine_issue_qa_summary(issue)
    commands = _redmine_issue_command_hints(issue)
    command_hint = next((command for command in commands if not _is_developer_test_command(command)), commands[0] if commands else "")
    command, analysis = _derive_side_effect_safe_command(config, issue, command_hint)
    oracle = qa_summary.get("expected") or "AI-derived probe exits with the expected return code and exercises the safest available issue-related surface."
    expected_exit = _expected_exit_from_oracle_text(str(oracle))
    environment = qa_summary.get("environment") if isinstance(qa_summary.get("environment"), list) else []
    evidence = qa_summary.get("evidence") if isinstance(qa_summary.get("evidence"), list) else []
    return {
        "run": command,
        "source": analysis["source"],
        "source_type": "ai_derived",
        "expected_exit_code": expected_exit["code"],
        "expected_exit_code_source": expected_exit["source"],
        "user_confirmed_inputs": {
            "command_source": analysis["source"],
            "expected_exit_code": expected_exit["code"],
            "expected_exit_code_source": expected_exit["source"],
            "fixtures_environment": [{"field": "AI-derived", "value": value} for value in environment[:5]],
            "oracle": [{"field": "AI-derived expected behavior", "value": str(oracle)}],
            "side_effect_boundaries": [
                {
                    "field": "AI-derived safety boundary",
                    "value": "Probe is restricted to help/parser/static repo checks and must not contact lab hardware, credentials, or external services.",
                }
            ],
            "evidence_hints": [{"field": "Redmine evidence", "value": value} for value in evidence[:5]],
        },
        "ai_analysis": analysis,
        "automation_confidence": analysis["automation_confidence"],
        "environment_requirements": _redmine_environment_requirements(config, issue, command=command, analysis=analysis),
        "requires_prepared_environment": True,
        "follow_up_needed": _redmine_follow_up_needed(issue, confirmed=False),
    }


def _expected_exit_from_oracle_text(text: str) -> dict[str, Any]:
    if _oracle_text_indicates_rejection(text):
        return {"code": 1, "source": "AI-inferred rejection oracle"}
    return {"code": 0, "source": "AI-derived safe probe default"}


def _derive_side_effect_safe_command(config: ProjectConfig, issue: dict[str, Any], command_hint: str) -> tuple[str, dict[str, Any]]:
    binary_hint = _redmine_binary_hint(config, command_hint)
    if binary_hint:
        command, binary_analysis = _redmine_binary_command_from_hint(binary_hint, command_hint)
        if command:
            return command, binary_analysis
    subcommand = _subcommand_from_redmine_command(command_hint)
    if command_hint:
        executable = _first_command_token(command_hint)
        if executable and not _is_developer_tool_name(executable):
            command = _redmine_binary_help_command(executable, subcommand=subcommand)
            return command, {
                "strategy": "installed_or_local_binary_help_probe_from_redmine_command",
                "source": "AI-derived product binary from Redmine reproduction command",
                "redmine_command_hint": command_hint,
                "selected_surface": subcommand or executable,
                "binary": executable,
                "automation_confidence": "low",
                "reason": "The issue names a product executable; the generated contract requires a prebuilt binary and uses a non-mutating help probe.",
            }
    raise CaseGenerationError("runtime_profile_required_for_redmine_case_generation")


def _redmine_binary_hint(config: ProjectConfig, command_hint: str) -> dict[str, Any] | None:
    if command_hint and not _is_developer_test_command(command_hint):
        try:
            tokens = shlex.split(command_hint)
        except ValueError:
            tokens = command_hint.split()
        if tokens:
            executable = Path(tokens[0]).name
            if executable and not _is_developer_tool_name(executable):
                return {"binary": executable, "tokens": tokens, "source": "redmine_command"}
    runtime_binary = primary_runtime_binary(config)
    if runtime_binary and not _is_developer_tool_name(runtime_binary):
        return {"binary": runtime_binary, "tokens": [], "source": "runtime_profile_or_repo_analysis"}
    return None


def _redmine_binary_command_from_hint(binary_hint: dict[str, Any], command_hint: str) -> tuple[str, dict[str, Any]]:
    binary = str(binary_hint["binary"])
    tokens = binary_hint.get("tokens") if isinstance(binary_hint.get("tokens"), list) else []
    args = [str(token) for token in tokens[1:]]
    subcommand = _subcommand_from_redmine_command(command_hint)
    if args and _redmine_args_look_read_only(args):
        command = _redmine_binary_exec_command(binary, args)
        return command, {
            "strategy": "binary_reproduction_from_redmine_command",
            "source": "AI-derived product binary command from Redmine reproduction steps",
            "redmine_command_hint": command_hint,
            "selected_surface": subcommand or binary,
            "binary": binary,
            "binary_source": binary_hint.get("source"),
            "automation_confidence": "medium",
            "reason": "The Redmine reproduction command uses a product binary and appears read-only; the contract keeps the binary command and makes environment preparation explicit.",
        }
    command = _redmine_binary_help_command(binary, subcommand=subcommand)
    return command, {
        "strategy": "binary_help_probe_from_redmine_or_repo",
        "source": "AI-derived product binary help probe",
        "redmine_command_hint": command_hint,
        "selected_surface": subcommand or binary,
        "binary": binary,
        "binary_source": binary_hint.get("source"),
        "automation_confidence": "medium" if binary_hint.get("source") == "repo_cli" else "low",
        "reason": "The exact Redmine command was not proven side-effect-safe, so the generated command uses the product binary help surface and records the missing runtime details.",
    }


def _redmine_binary_exec_command(binary: str, args: list[str]) -> str:
    args_text = " ".join(_redmine_shell_arg_fragments(args))
    script = f'binary="${{QUALITY_PILOT_BINARY:-./{binary}}}"; test -x "$binary"; exec "$binary"'
    if args_text:
        script += f" {args_text}"
    return _shell_command(script)


def _redmine_shell_arg_fragments(args: list[str]) -> list[str]:
    fragments: list[str] = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        lowered = arg.lower()
        next_value = str(args[i + 1]) if i + 1 < len(args) else ""
        if lowered in {"--passwd", "--password", "--pass"} and next_value:
            fragments.append(shlex.quote(arg))
            fragments.append('"${QUALITY_PILOT_TEST_PASSWORD:?set QUALITY_PILOT_TEST_PASSWORD}"')
            i += 2
            continue
        if lowered in {"--user", "--username"} and next_value:
            fragments.append(shlex.quote(arg))
            fragments.append(f'"${{QUALITY_PILOT_TEST_USER:-{_shell_default_value(next_value)}}}"')
            i += 2
            continue
        if lowered in {"--host", "--hostname", "--target", "--bmc"} and next_value:
            fragments.append(shlex.quote(arg))
            fragments.append(f'"${{QUALITY_PILOT_TARGET_HOST:-{_shell_default_value(next_value)}}}"')
            i += 2
            continue
        fixture_env = _fixture_env_name_for_flag_value(arg, next_value)
        if fixture_env:
            fragments.append(shlex.quote(arg))
            fragments.append(f'"${{{fixture_env}:-{_shell_default_value(next_value)}}}"')
            i += 2
            continue
        fragments.append(shlex.quote(arg))
        i += 1
    return fragments


def _fixture_env_name_for_flag_value(flag: str, value: str) -> str:
    if not flag.startswith("-") or not value:
        return ""
    lowered = flag.lower()
    if lowered in _OUTPUT_VALUE_FLAGS or lowered in _CREDENTIAL_VALUE_FLAGS or lowered in _TARGET_VALUE_FLAGS:
        return ""
    if not (_flag_name_suggests_fixture_path(lowered) or _value_looks_like_fixture_path(value)):
        return ""
    name = re.sub(r"[^A-Za-z0-9]+", "_", flag.strip("-")).strip("_").upper()
    return f"QUALITY_PILOT_FIXTURE_{name or 'PATH'}"


def _flag_name_suggests_fixture_path(flag: str) -> bool:
    if not flag.startswith("-"):
        return False
    name = flag.strip("-").replace("_", "-").lower()
    words = {part for part in name.split("-") if part}
    if words & {"config", "fixture", "profile", "login", "credential", "credentials", "certificate", "cert"}:
        return True
    return name.endswith("-file") or name.endswith("-path")


def _value_looks_like_fixture_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "/" in text or "\\" in text:
        return True
    return lowered.endswith((
        ".cfg",
        ".conf",
        ".ini",
        ".json",
        ".pem",
        ".profile",
        ".properties",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    ))


def _fixture_env_names_in_command(command: str) -> list[str]:
    names = re.findall(r"\bQUALITY_PILOT_FIXTURE_[A-Z0-9_]+\b", command)
    return sorted(set(names))


_CREDENTIAL_VALUE_FLAGS = {"-p", "--passwd", "--password", "--pass", "--token", "--api-key", "--secret"}
_TARGET_VALUE_FLAGS = {"-r", "--host", "--hostname", "--target", "--bmc", "--server", "--url", "--endpoint"}
_OUTPUT_VALUE_FLAGS = {"-o", "--out", "--output", "--outfile", "--output-file"}


def _shell_default_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def _redmine_binary_help_command(binary: str, *, subcommand: str = "") -> str:
    args = [subcommand, "--help"] if subcommand else ["--help"]
    return _redmine_binary_exec_command(binary, args)


def _redmine_args_look_read_only(args: list[str]) -> bool:
    mutating = {
        "add",
        "apply",
        "boot",
        "clear",
        "create",
        "delete",
        "disable",
        "enable",
        "flash",
        "format",
        "mount",
        "power",
        "push",
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
    meaningful = [arg.lower() for arg in args if arg and not arg.startswith("-")]
    return not any(arg in mutating for arg in meaningful)


def _is_developer_test_command(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    normalized = [Path(token).name.lower() for token in tokens[:3]]
    if normalized[:2] in (["go", "test"], ["go", "run"]):
        return True
    if normalized[:3] == ["python", "-m", "pytest"] or normalized[:3] == ["python3", "-m", "pytest"]:
        return True
    if normalized[0] in {"pytest", "go"} and (len(normalized) == 1 or normalized[1] in {"test", "run"}):
        return True
    return False


def _is_developer_tool_name(name: str) -> bool:
    return Path(name).name.lower() in {
        "go",
        "pytest",
        "python",
        "python3",
        "sh",
        "bash",
        "make",
        "cmake",
        "ninja",
    }


def _redmine_environment_requirements(
    config: ProjectConfig,
    issue: dict[str, Any],
    *,
    command: str,
    analysis: dict[str, Any] | None = None,
) -> list[str]:
    qa_summary = redmine_issue_qa_summary(issue)
    requirements: list[str] = []
    binary = ""
    if analysis and analysis.get("binary"):
        binary = str(analysis["binary"])
    else:
        command_token = _first_command_token(command)
        if command_token and not _is_developer_tool_name(command_token):
            binary = command_token
        else:
            binary = primary_runtime_binary(config)
    if binary:
        requirements.append(
            f"Build or install the product binary `{binary}` before running; set QUALITY_PILOT_BINARY to its path or place it at `./{binary}`."
        )
    if "QUALITY_PILOT_TEST_PASSWORD" in command:
        requirements.append("Set QUALITY_PILOT_TEST_PASSWORD for the prepared test account; do not store the raw password in the case YAML.")
    if "QUALITY_PILOT_TEST_USER" in command:
        requirements.append("Set QUALITY_PILOT_TEST_USER when the prepared test account differs from the issue example user.")
    if "QUALITY_PILOT_TARGET_HOST" in command:
        requirements.append("Set QUALITY_PILOT_TARGET_HOST to the prepared test system/BMC address.")
    for env_name in _fixture_env_names_in_command(command):
        requirements.append(f"Set {env_name} to the prepared fixture/config path referenced by the command.")
    environment = qa_summary.get("environment") if isinstance(qa_summary.get("environment"), list) else []
    for item in environment[:5]:
        requirements.append(f"Prepare Redmine test environment/resource: {item}")
    reproduction = str(qa_summary.get("reproduction") or "").strip()
    if reproduction:
        requirements.append(f"Prepare fixtures and credentials referenced by Redmine reproduction steps: {reproduction}")
    evidence = qa_summary.get("evidence") if isinstance(qa_summary.get("evidence"), list) else []
    for item in evidence[:3]:
        requirements.append(f"Keep Redmine evidence available for oracle comparison: {item}")
    requirements.append("Confirm the command is safe to run in the prepared test system before enabling unattended/free-hand execution.")
    return _unique_text(requirements)


def _redmine_issue_command_hints(issue: dict[str, Any]) -> list[str]:
    raw = issue.get("raw") if isinstance(issue.get("raw"), dict) else {}
    hints: list[str] = []
    for item in raw.get("custom_fields", []) if isinstance(raw.get("custom_fields"), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        if any(word in name for word in ("command", "procedure", "reproduce", "reproduction", "step")):
            value = str(item.get("value") or "").strip()
            if value:
                hints.append(value)
    text = "\n".join(
        str(value or "")
        for value in (
            issue.get("description"),
            issue.get("description_text"),
            issue.get("full_message"),
            raw.get("description"),
            raw.get("description_text"),
        )
    )
    text = _normalize_redmine_command_text(text)
    for block in re.findall(r"<pre[^>]*>(.*?)</pre>", text, flags=re.IGNORECASE | re.DOTALL):
        for line in block.splitlines():
            command = _command_line_from_redmine_text(line)
            if command:
                hints.append(command)
    for match in re.finditer(r"`([^`\n]*(?:[A-Za-z0-9_-]+)\s+[^`\n]+)`", text):
        hints.append(match.group(1).strip())
    for line in text.splitlines():
        command = _command_line_from_redmine_text(line)
        if command:
            hints.append(command)
            continue
        stripped = line.strip().lstrip("-*0123456789. ")
        if stripped.lower().startswith("run "):
            hints.append(stripped[4:].strip("` "))
    return _unique_text(hints)


def _normalize_redmine_command_text(text: str) -> str:
    normalized = str(text or "")
    replacements = {
        "\\r\\n": "\n",
        "\\n": "\n",
        "\\r": "\n",
        "\\u003c": "<",
        "\\u003e": ">",
        "\\u0026": "&",
    }
    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)
        normalized = normalized.replace(src.upper(), dst)
    return normalized


def _command_line_from_redmine_text(line: str) -> str:
    stripped = line.strip().strip("`")
    if not stripped:
        return ""
    stripped = re.sub(r"^(?:[$#>]\s*)", "", stripped)
    stripped = stripped.lstrip("-*0123456789. ")
    lowered = stripped.lower()
    if stripped.startswith("|") or stripped.count("|") >= 2 or re.fullmatch(r"[-+\s|]+", stripped):
        return ""
    if lowered.startswith(("run ", "execute ")):
        stripped = stripped.split(maxsplit=1)[1].strip("` ") if len(stripped.split(maxsplit=1)) > 1 else ""
    if not stripped or stripped.startswith("<") or stripped.endswith(":"):
        return ""
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if len(tokens) < 2:
        return ""
    first = tokens[0]
    first_name = Path(first).name.lower()
    has_option = any(token.startswith("-") for token in tokens[1:])
    if not re.fullmatch(r"[a-z0-9_.-]+", first_name):
        return ""
    if first.startswith(("./", "/")) or (has_option and first_name not in {"the", "this", "that", "when", "expected", "observed"}):
        return stripped
    return ""


def _subcommand_from_redmine_command(command: str) -> str:
    if not command:
        return ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    if len(tokens) >= 3 and tokens[0] == "go" and tokens[1] == "run":
        tokens = tokens[3:]
    else:
        tokens = tokens[1:]
    for token in tokens:
        if token.startswith("-"):
            continue
        if "/" in token or "\\" in token or "." in token:
            continue
        if "=" in token:
            continue
        return token
    return ""


def _first_command_token(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    if len(tokens) >= 3 and tokens[0] == "go" and tokens[1] == "run":
        return "go"
    return Path(tokens[0]).name


def _redmine_search_terms(issue: dict[str, Any]) -> list[str]:
    text = " ".join(str(issue.get(key) or "") for key in ("subject", "description", "description_text", "full_message"))
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
    ignored = {"after", "before", "with", "from", "this", "that", "should", "would", "could", "expected", "observed"}
    terms = [word for word in words if word.lower() not in ignored]
    return _unique_text(terms)[:8] or [f"Redmine {issue.get('id')}"]


def _redmine_follow_up_needed(issue: dict[str, Any], *, confirmed: bool) -> list[str]:
    qa_summary = redmine_issue_qa_summary(issue)
    missing = qa_summary.get("missing_for_executable_case") if isinstance(qa_summary.get("missing_for_executable_case"), list) else []
    out = [str(item) for item in missing if str(item).strip()]
    if not confirmed:
        out.append("Exact lab reproduction remains advisory until a user-confirmed runner/fixture boundary is provided.")
    return _unique_text(out)


def _unique_text(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


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
    readme_text = _first_existing_text(root, ["README.md", "README.rst", "README.txt"], limit=80000)
    pyproject_text = _read_text_if_exists(root / "pyproject.toml", limit=8000)
    package_json_text = _read_text_if_exists(root / "package.json", limit=8000)
    go_mod_text = _read_text_if_exists(root / "go.mod", limit=4000)
    openapi_exists = any((root / name).exists() for name in ["openapi.yaml", "openapi.yml", "swagger.yaml", "swagger.yml"])
    existing_runner_count = len([p for p in config.paths.runners.glob("*") if p.is_file()]) if config.paths.runners.exists() else 0
    existing_case_count = len([*config.paths.cases.glob("*.yaml"), *config.paths.cases.glob("*.yml")]) if config.paths.cases.exists() else 0

    cli_commands = _extract_pyproject_scripts(pyproject_text) + _extract_package_bins(package_json_text)
    project_name = str(config.data.get("project", {}).get("name") or root.name)
    suggested_command = cli_commands[0] if cli_commands else ""
    runtime_binary = primary_runtime_binary(config)
    readme_cli_surfaces = _readme_cli_surfaces_from_text(
        readme_text,
        runtime_binary=runtime_binary,
        cli_commands=cli_commands,
        project_name=project_name,
    )
    readme_cli_operations = _readme_cli_operations_from_text(
        readme_text,
        runtime_binary=runtime_binary,
        cli_commands=cli_commands,
        project_name=project_name,
    )
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
        "readme_cli_operations": readme_cli_operations[:24],
        "readme_cli_surfaces": readme_cli_surfaces[:24],
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


def summarize_git_history(config: ProjectConfig, *, limit: int = 30) -> dict[str, Any]:
    git_dir = config.root / ".git"
    if not git_dir.exists():
        return {"exists": False, "recent_commits": []}
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(config.root),
                "log",
                f"-n{limit}",
                "--date=iso-strict",
                "--name-only",
                "--pretty=format:__QP_COMMIT__%x1f%H%x1f%h%x1f%ad%x1f%s",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"exists": True, "status": "unreadable", "error": type(exc).__name__, "recent_commits": []}
    if completed.returncode != 0:
        return {
            "exists": True,
            "status": "unreadable",
            "error": _compact_text(completed.stderr or completed.stdout, limit=240),
            "recent_commits": [],
        }

    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("__QP_COMMIT__"):
            if current:
                commits.append(current)
            parts = line.split("\x1f", 4)
            current = {
                "hash": parts[1] if len(parts) > 1 else "",
                "short_hash": parts[2] if len(parts) > 2 else "",
                "date": parts[3] if len(parts) > 3 else "",
                "subject": parts[4] if len(parts) > 4 else "",
                "touched_files": [],
                "touched_roots": [],
                "issue_refs": [],
                "pr_refs": [],
            }
            continue
        if current is None:
            continue
        files = current.setdefault("touched_files", [])
        if isinstance(files, list) and len(files) < 20:
            files.append(line)
    if current:
        commits.append(current)

    for commit in commits:
        touched_files = [str(item) for item in commit.get("touched_files", []) if str(item).strip()]
        commit["touched_roots"] = sorted({Path(item).parts[0] for item in touched_files if Path(item).parts})[:8]
        issue_refs, pr_refs = _extract_issue_pr_refs(str(commit.get("subject") or ""))
        commit["issue_refs"] = issue_refs
        commit["pr_refs"] = pr_refs

    return {
        "exists": True,
        "status": "ok",
        "recent_commit_count": len(commits),
        "recent_commits": commits[:limit],
    }


def _extract_issue_pr_refs(text: str) -> tuple[list[str], list[str]]:
    issue_refs = sorted({match.group(1) for match in re.finditer(r"(?i)(?:fix(?:es|ed)?|close(?:s|d)?|issue)\s+#?(\d+)", text)})
    pr_refs = sorted({match.group(1) for match in re.finditer(r"(?i)(?:pr|pull request)\s+#?(\d+)", text)})
    pr_refs.extend(ref for ref in re.findall(r"\(#(\d+)\)", text) if ref not in pr_refs)
    return issue_refs[:8], pr_refs[:8]


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


def existing_case_command_runs(config: ProjectConfig) -> dict[str, set[str]]:
    runs: dict[str, set[str]] = {}
    paths = sorted([*config.paths.cases.glob("*.yaml"), *config.paths.cases.glob("*.yml")]) if config.paths.cases.exists() else []
    for path in paths:
        try:
            data = _load_yaml(path)
        except Exception:
            continue
        case_id = str(data.get("case_id") or path.stem)
        for run in _contract_command_runs(data):
            runs.setdefault(run, set()).add(case_id)
    return runs


def _contract_command_runs(contract: dict[str, Any]) -> set[str]:
    commands = contract.get("commands") if isinstance(contract.get("commands"), list) else []
    runs: set[str] = set()
    for command in commands:
        if not isinstance(command, dict):
            continue
        run = str(command.get("run") or "").strip()
        if run:
            runs.add(run)
    return runs


def _duplicate_generated_command_reason(
    contract: dict[str, Any],
    existing_runs: dict[str, set[str]],
    batch_runs: set[str],
) -> dict[str, Any] | None:
    case_id = str(contract.get("case_id") or "")
    for run in _contract_command_runs(contract):
        if run in batch_runs:
            return {"reason": "duplicate_generated_command", "run": run}
        existing_case_ids = sorted(existing_runs.get(run, set()) - {case_id})
        if existing_case_ids:
            return {
                "reason": "duplicate_existing_command",
                "run": run,
                "existing_case_ids": existing_case_ids,
            }
    return None


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
    runtime_binary = primary_runtime_binary(config)
    if runtime_binary:
        operation = candidate.get("swqa_operation") if isinstance(candidate.get("swqa_operation"), dict) else {}
        operation_key = str(operation.get("key") or "")
        seed_groups = _candidate_seed_command_groups(candidate)
        if seed_groups and operation_key in {"monkey_help_sweep", "sibling_help_sweep"}:
            return _runtime_binary_group_command(runtime_binary, seed_groups)
        if seed_groups and operation_key == "monkey_repeatability":
            return _runtime_binary_repeat_group_command(runtime_binary, seed_groups)
        if seed_groups and operation_key == "monkey_concurrency":
            return _runtime_binary_concurrent_group_command(runtime_binary, seed_groups)
        seed_args = _candidate_seed_command_args(candidate)
        if operation_key == "invalid_option_rejection":
            return _runtime_binary_invalid_option_command(runtime_binary, _base_args_for_operation(seed_args), _candidate_operation_slug(candidate, "invalid"))
        if operation_key == "boundary_invalid_value":
            return _runtime_binary_boundary_invalid_value_command(runtime_binary, _base_args_for_operation(seed_args), _candidate_operation_slug(candidate, "boundary"))
        if operation_key == "repeatability_probe":
            return _runtime_binary_repeat_command(runtime_binary, _safe_args_for_repeated_operation(seed_args))
        if operation_key == "concurrency_probe":
            return _runtime_binary_concurrent_command(runtime_binary, _safe_args_for_repeated_operation(seed_args))
        if operation_key == "timeout_baseline":
            return _runtime_binary_timeout_command(runtime_binary, _safe_args_for_repeated_operation(seed_args))
        if operation_key == "sibling_help_sweep":
            groups = _readme_help_surface_groups(config, runtime_binary=runtime_binary)
            if groups:
                return _runtime_binary_group_command(runtime_binary, groups)
        if seed_args:
            if _candidate_seed_type(candidate) == "readme_cli_operation":
                return _runtime_binary_readme_operation_command(runtime_binary, seed_args)
            return _runtime_binary_exec_command(runtime_binary, seed_args)
        if "stress_timeout_risk" in dimensions:
            return _runtime_binary_help_command(runtime_binary)
        if "sibling_surface" in dimensions:
            surfaces = [
                surface
                for surface in _readme_cli_surfaces(config, runtime_binary=runtime_binary)
                if isinstance(surface.get("args"), list)
                and surface["args"]
                and surface["args"][-1] == "--help"
                and len(surface["args"]) > 1
            ]
            if surfaces:
                checks = " && ".join(
                    _runtime_binary_exec_fragment(runtime_binary, [str(item) for item in surface["args"]])
                    for surface in surfaces[:3]
                )
                return _shell_command(checks)
        return _runtime_binary_help_command(runtime_binary)

    raise CaseGenerationError("runtime_profile_required_for_executable_command")


def _candidate_operation_slug(candidate: dict[str, Any], prefix: str) -> str:
    seed = candidate.get("growth_seed") if isinstance(candidate.get("growth_seed"), dict) else {}
    operation = candidate.get("swqa_operation") if isinstance(candidate.get("swqa_operation"), dict) else {}
    raw = "-".join([
        prefix,
        str(seed.get("type") or "seed"),
        str(seed.get("id") or seed.get("title") or ""),
        str(operation.get("key") or ""),
    ])
    return _slug(raw).lower()[:40] or prefix


def _base_args_for_operation(seed_args: list[str]) -> list[str]:
    args = [str(item) for item in seed_args if str(item).strip()]
    if args and args[-1] == "--help":
        return args[:-1]
    if args in (["--version"], ["-v"]):
        return []
    return [arg for arg in args if not arg.startswith("-")][:3]


def _safe_args_for_repeated_operation(seed_args: list[str]) -> list[str]:
    args = [str(item) for item in seed_args if str(item).strip()]
    if args and args[-1] == "--help":
        return args
    if args in (["--version"], ["-v"]):
        return args
    base = [arg for arg in args if not arg.startswith("-")][:3]
    return [*base, "--help"] if base else ["--help"]


def _readme_help_surface_groups(config: ProjectConfig, *, runtime_binary: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for surface in _readme_cli_surfaces(config, runtime_binary=runtime_binary):
        args = surface.get("args") if isinstance(surface, dict) else None
        if isinstance(args, list) and args and args[-1] == "--help":
            groups.append([str(item) for item in args])
    return groups[:8]


def _candidate_seed_command_groups(candidate: dict[str, Any]) -> list[list[str]]:
    for key in ["growth_seed", "init_seed"]:
        seed = candidate.get(key)
        if not isinstance(seed, dict):
            continue
        groups = seed.get("command_groups")
        if not isinstance(groups, list):
            continue
        out: list[list[str]] = []
        for group in groups:
            if isinstance(group, list) and all(str(item).strip() for item in group):
                out.append([str(item) for item in group])
        if out:
            return out
    return []


def _runtime_binary_exec_command(binary: str, args: list[str]) -> str:
    args_text = " ".join(_redmine_shell_arg_fragments(args))
    script = f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary"; exec "$binary"'
    if args_text:
        script += f" {args_text}"
    return _shell_command(script)


def _runtime_binary_group_command(binary: str, groups: list[list[str]]) -> str:
    checks = " && ".join(_runtime_binary_exec_fragment(binary, args) for args in groups[:8])
    return _shell_command(checks)


def _runtime_binary_repeat_group_command(binary: str, groups: list[list[str]]) -> str:
    checks = " && ".join(_runtime_binary_repeat_fragment(binary, args, repeats=3) for args in groups[:4])
    return _shell_command(checks)


def _runtime_binary_concurrent_group_command(binary: str, groups: list[list[str]]) -> str:
    checks = " && ".join(_runtime_binary_concurrent_fragment(binary, args) for args in groups[:4])
    return _shell_command(checks)


def _runtime_binary_invalid_option_command(binary: str, args: list[str], slug: str) -> str:
    invalid_flag = f"--quality-pilot-invalid-{slug}"
    args_text = " ".join(_redmine_shell_arg_fragments([*args, invalid_flag]))
    script = (
        f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary"; '
        f'"$binary" {args_text} >/dev/null 2>&1; rc=$?; test "$rc" -ne 0'
    )
    return _shell_command(script)


def _runtime_binary_boundary_invalid_value_command(binary: str, args: list[str], slug: str) -> str:
    invalid_flag = f"--quality-pilot-boundary-{slug}"
    args_text = " ".join(_redmine_shell_arg_fragments([*args, invalid_flag, ""]))
    script = (
        f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary"; '
        f'"$binary" {args_text} >/dev/null 2>&1; rc=$?; test "$rc" -ne 0'
    )
    return _shell_command(script)


def _runtime_binary_repeat_command(binary: str, args: list[str]) -> str:
    return _shell_command(_runtime_binary_repeat_fragment(binary, args, repeats=5))


def _runtime_binary_concurrent_command(binary: str, args: list[str]) -> str:
    return _shell_command(_runtime_binary_concurrent_fragment(binary, args))


def _runtime_binary_timeout_command(binary: str, args: list[str]) -> str:
    args_text = " ".join(_redmine_shell_arg_fragments(args))
    script = (
        f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary"; '
        f'timeout "${{QUALITY_PILOT_TIMEOUT_SECONDS:-5}}" "$binary"'
    )
    if args_text:
        script += f" {args_text}"
    script += " >/dev/null"
    return _shell_command(script)


def _runtime_binary_repeat_fragment(binary: str, args: list[str], *, repeats: int) -> str:
    args_text = " ".join(_redmine_shell_arg_fragments(args))
    command = f'"$binary" {args_text} >/dev/null' if args_text else '"$binary" --help >/dev/null'
    return (
        f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary"; '
        f'for i in $(seq 1 {repeats}); do {command}; done'
    )


def _runtime_binary_concurrent_fragment(binary: str, args: list[str]) -> str:
    args_text = " ".join(_redmine_shell_arg_fragments(args))
    command = f'"$binary" {args_text} >/dev/null' if args_text else '"$binary" --help >/dev/null'
    return (
        f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary"; '
        f'{command} & p1=$!; {command} & p2=$!; wait "$p1"; wait "$p2"'
    )


def _runtime_binary_readme_operation_command(binary: str, args: list[str]) -> str:
    script = f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary";'
    exec_parts = ['exec "$binary"']
    i = 0
    while i < len(args):
        arg = str(args[i])
        next_value = str(args[i + 1]) if i + 1 < len(args) else ""
        fixture_env = _fixture_env_name_for_flag_value(arg, next_value)
        if fixture_env:
            var_name = f"fixture_{i}"
            script += f' {var_name}="${{{fixture_env}:-{_shell_default_value(next_value)}}}"; test -f "${var_name}";'
            exec_parts.extend([shlex.quote(arg), f'"${var_name}"'])
            i += 2
            continue
        exec_parts.append(shlex.quote(arg))
        i += 1
    script += " " + " ".join(exec_parts)
    return _shell_command(script)


def _runtime_binary_help_command(binary: str, *, subcommand: str = "") -> str:
    args = [*shlex.split(subcommand), "--help"] if subcommand else ["--help"]
    return _runtime_binary_exec_command(binary, args)


def _runtime_binary_exec_fragment(binary: str, args: list[str]) -> str:
    args_text = " ".join(_redmine_shell_arg_fragments(args))
    script = f'binary="${{QUALITY_PILOT_BINARY:-{binary}}}"; command -v "$binary" >/dev/null 2>&1 || test -x "$binary"; "$binary"'
    if args_text:
        script += f" {args_text}"
    return script + " >/dev/null"


def _runtime_binary_help_fragment(binary: str, *, subcommand: str = "") -> str:
    args = [*shlex.split(subcommand), "--help"] if subcommand else ["--help"]
    return _runtime_binary_exec_fragment(binary, args)


def _command_uses_runtime_binary(command: str, runtime_binary: str) -> bool:
    text = str(command or "").strip()
    binary = str(runtime_binary or "").strip()
    if not text or not binary:
        return False
    if "QUALITY_PILOT_BINARY" in text:
        return True
    binary_names = {binary, Path(binary).name}
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for token in tokens[:6]:
        cleaned = token.strip("'\"")
        if cleaned in binary_names or Path(cleaned).name in binary_names:
            return True
    return False


def _candidate_seed_command_args(candidate: dict[str, Any]) -> list[str]:
    for key in ["init_seed", "growth_seed"]:
        seed = candidate.get(key)
        if not isinstance(seed, dict):
            continue
        args = seed.get("command_args")
        if isinstance(args, list) and args:
            return [str(item) for item in args if str(item).strip()]
    return []


def _candidate_seed_type(candidate: dict[str, Any]) -> str:
    for key in ["init_seed", "growth_seed"]:
        seed = candidate.get(key)
        if isinstance(seed, dict) and seed.get("type"):
            return str(seed.get("type"))
    return ""


def _readme_cli_surfaces(config: ProjectConfig, *, runtime_binary: str = "") -> list[dict[str, Any]]:
    text = _first_existing_text(config.root, ["README.md", "README.rst", "README.txt"], limit=80000)
    if not text:
        return []
    return _readme_cli_surfaces_from_text(
        text,
        runtime_binary=runtime_binary or primary_runtime_binary(config),
        cli_commands=[],
        project_name=str(config.data.get("project", {}).get("name") or config.root.name),
    )


def _readme_cli_surfaces_from_text(
    text: str,
    *,
    runtime_binary: str = "",
    cli_commands: list[str] | None = None,
    project_name: str = "",
) -> list[dict[str, Any]]:
    if not text:
        return []
    command_names = _readme_command_names(runtime_binary, cli_commands or [], project_name)
    if not command_names:
        return []
    surfaces: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        raw_args = _readme_command_args_from_line(line, command_names)
        args = _safe_readme_cli_args(raw_args)
        if not args:
            continue
        key = tuple(args)
        if key in seen:
            continue
        seen.add(key)
        surfaces.append(
            {
                "label": _readme_cli_surface_label(args),
                "args": args,
                "source": f"README:{line_no}",
            }
        )
    return surfaces


def _readme_cli_operations_from_text(
    text: str,
    *,
    runtime_binary: str = "",
    cli_commands: list[str] | None = None,
    project_name: str = "",
) -> list[dict[str, Any]]:
    if not text:
        return []
    command_names = _readme_command_names(runtime_binary, cli_commands or [], project_name)
    if not command_names:
        return []
    operations: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        raw_args = _readme_command_args_from_line(line, command_names)
        candidate = _safe_readme_operation_args(raw_args)
        if not candidate:
            continue
        args = candidate["args"]
        key = tuple(args)
        if key in seen:
            continue
        seen.add(key)
        operations.append(
            {
                "label": _readme_operation_label(args),
                "args": args,
                "source": f"README:{line_no}",
                "requires_prepared_environment": candidate["requires_prepared_environment"],
                "environment_requirements": candidate["environment_requirements"],
            }
        )
    return operations


def _readme_command_names(runtime_binary: str, cli_commands: list[str], project_name: str) -> set[str]:
    names: set[str] = set()
    binary = str(runtime_binary or "").strip()
    if binary:
        first = binary.split()[0]
        names.add(_clean_readme_token(first))
        names.add(Path(first).name)
    if not names:
        for value in [*cli_commands, project_name]:
            cleaned = _clean_readme_token(str(value or ""))
            if cleaned:
                names.add(cleaned)
                names.add(Path(cleaned).name)
    return {name for name in names if name}


def _readme_command_args_from_line(line: str, command_names: set[str]) -> list[str]:
    text = line.strip()
    if not text or text.startswith("```"):
        return []
    text = text.strip("`")
    text = re.sub(r"^\s*(?:[-*+]\s*)?(?:\$|>|%)\s*", "", text)
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    if not tokens:
        return []
    cleaned_tokens = [_clean_readme_token(token) for token in tokens]
    for index, token in enumerate(cleaned_tokens[:8]):
        if token in command_names or Path(token).name in command_names:
            args: list[str] = []
            for value in cleaned_tokens[index + 1 :]:
                if not value or value.startswith("#"):
                    break
                args.append(value)
            return args
    return []


def _safe_readme_operation_args(args: list[str]) -> dict[str, Any] | None:
    if not args:
        return None
    if _looks_like_readme_usage_or_description(args):
        return None
    lowered = [arg.lower() for arg in args]
    if "--help" in lowered or "--version" in lowered or "-v" in lowered:
        return None
    if any(arg in {"-u", "--user", "-p", "--passwd", "--password", "--pass", "-r", "--host"} for arg in lowered):
        return None
    if any(arg in {"-o", "--out", "--output", "--outfile", "--file"} for arg in lowered):
        return None
    if any(arg in {"--force", "--confirm", "--yes"} for arg in lowered):
        return None

    command_words = _operation_command_words(args)
    if not command_words:
        return None
    if any(word in _MUTATING_OPERATION_WORDS for word in command_words):
        return None
    if not any(word in _READONLY_OPERATION_WORDS for word in command_words):
        return None

    requirements = _fixture_environment_requirements_for_args(args)
    if requirements is None:
        return None
    return {
        "args": args,
        "requires_prepared_environment": bool(requirements),
        "environment_requirements": requirements,
    }


def _fixture_environment_requirements_for_args(args: list[str]) -> list[str] | None:
    requirements: list[str] = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        next_value = str(args[i + 1]) if i + 1 < len(args) else ""
        if arg.startswith("-") and _flag_name_suggests_fixture_path(arg.lower()) and not next_value:
            return None
        env_name = _fixture_env_name_for_flag_value(arg, next_value)
        if env_name:
            requirements.append(
                f"Prepare fixture/config path for `{arg}` in {env_name}, or place the README default file at the documented relative path."
            )
            i += 2
            continue
        i += 1
    return _unique_text(requirements)


def _looks_like_readme_usage_or_description(args: list[str]) -> bool:
    for arg in args:
        value = str(arg or "").strip()
        if not value:
            return True
        lowered = value.lower()
        if value == "-":
            return True
        if "[" in value or "]" in value:
            return True
        if value.startswith("<") or value.endswith(">"):
            return True
        if lowered in {"options", "arguments", "command", "commands"}:
            return True
        if lowered.endswith("..."):
            return True
    return False


def _operation_command_words(args: list[str]) -> list[str]:
    words: list[str] = []
    skip_next = False
    value_flags = {
        "--format",
        "--severity",
        "--type",
        "--after",
        "--before",
        "--contains",
        "--interfaces",
    }
    for arg in args:
        lowered = arg.lower()
        if skip_next:
            skip_next = False
            continue
        if lowered in value_flags or _flag_name_suggests_fixture_path(lowered):
            skip_next = True
            continue
        if lowered.startswith("-"):
            continue
        words.append(lowered)
    return words


_READONLY_OPERATION_WORDS = {
    "entry",
    "firmware",
    "get",
    "info",
    "list",
    "mcinfo",
    "query",
    "read",
    "show",
    "sensors",
    "status",
    "validate",
    "view",
}


_MUTATING_OPERATION_WORDS = {
    "apply",
    "clear",
    "collect",
    "create",
    "delete",
    "download",
    "eject",
    "insert",
    "passwd",
    "password",
    "reboot",
    "remove",
    "reset",
    "set",
    "update",
    "upload",
    "write",
}


def _safe_readme_cli_args(args: list[str]) -> list[str]:
    if not args:
        return []
    credential_or_target_flags = {
        "-p",
        "--passwd",
        "--password",
        "--pass",
        "-u",
        "--user",
        "-r",
        "--host",
        "--login",
        "--token",
        "--api-key",
        "--secret",
    }
    lowered = [arg.lower() for arg in args]
    if any(arg in credential_or_target_flags for arg in lowered):
        return []
    if "--version" in lowered:
        return ["--version"]
    if "-v" in lowered and len(args) == 1:
        return ["-v"]
    if "--help" not in lowered:
        return []
    help_index = lowered.index("--help")
    head = args[:help_index]
    if any(arg.startswith("-") for arg in head):
        return []
    safe = [arg for arg in head if arg]
    return [*safe, "--help"]


def _readme_cli_surface_label(args: list[str]) -> str:
    if args == ["--help"]:
        return "root help"
    if args in (["--version"], ["-v"]):
        return "version"
    if args and args[-1] == "--help":
        return " ".join(args[:-1]) + " help"
    return " ".join(args)


def _readme_operation_label(args: list[str]) -> str:
    words = _operation_command_words(args)
    return " ".join(words) or "read-only operation"


def _clean_readme_token(token: str) -> str:
    return str(token or "").strip().strip("`'\"").rstrip(".,;:")


def _readme_help_subcommands(config: ProjectConfig, command_name: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for surface in _readme_cli_surfaces(config, runtime_binary=command_name):
        args = surface.get("args") if isinstance(surface, dict) else None
        if not isinstance(args, list) or len(args) <= 1 or args[-1] != "--help":
            continue
        value = " ".join(str(item) for item in args[:-1])
        if not value or value in {"--help", "help"} or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _shell_command(script: str) -> str:
    return "sh -c " + shlex.quote(script)


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
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=4096)
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
