from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .command_policy import validate_generated_command
from .config import ProjectConfig
from .contracts import CaseContract, list_contract_paths, load_contract
from .hermes_mcp import configured_mcp_json_path, hermes_mcp_status, hermes_mcp_status_path
from .issues import IssueSyncError, issue_status
from .runner import utc_now
from .subagents import subagent_status


STATE_AUDIT_SCHEMA = "quality-pilot.state-audit.v1"
TRUSTED_RESULT_STATUSES = {"PASS", "FAIL", "BLOCK"}


def audit_project_state(config: ProjectConfig) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    state_artifacts = _state_artifacts(config)
    contracts = _load_contracts(config, findings)
    latest_run = _load_json(config.paths.state / "latest-run.json", findings, required=False)
    traceability = _audit_issue_traceability(config, findings)

    _audit_redmine_case_contracts(config, contracts, findings)
    _audit_generated_command_policy(config, contracts, findings)
    _audit_evidence_contract_consistency(config, contracts, latest_run, findings)
    _audit_redmine_handoffs(config, findings)
    _audit_gitea_handoffs(config, findings)
    _audit_wiki_handoffs(config, findings)
    _audit_fix_plan(config, contracts, traceability, findings)
    _audit_reports(config, latest_run, findings)
    _audit_mcp_status(config, findings)
    _audit_subagents(config, findings)

    finding_counts = _count_by_severity(findings)
    status = "blocked" if finding_counts.get("blocker", 0) else ("warn" if finding_counts.get("warning", 0) else "ok")
    return {
        "schema": STATE_AUDIT_SCHEMA,
        "status": status,
        "audited_at": utc_now(),
        "root": str(config.root),
        "workspace": _relative_or_str(config.paths.workspace, config.root),
        "syntax_valid": not any(item["id"] == "contract_invalid" for item in findings),
        "semantic_valid": status == "ok",
        "case_count": len(contracts),
        "state_artifacts": state_artifacts,
        "finding_counts": finding_counts,
        "blockers": [item["id"] for item in findings if item.get("severity") == "blocker"],
        "warnings": [item["id"] for item in findings if item.get("severity") == "warning"],
        "findings": findings,
        "next_actions": _next_actions(findings),
    }


def audit_summary(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": audit.get("schema"),
        "status": audit.get("status"),
        "semantic_valid": audit.get("semantic_valid"),
        "finding_counts": audit.get("finding_counts", {}),
        "blockers": audit.get("blockers", []),
        "warnings": audit.get("warnings", []),
        "next_actions": audit.get("next_actions", [])[:5],
    }


def _load_contracts(config: ProjectConfig, findings: list[dict[str, Any]]) -> dict[str, CaseContract]:
    contracts: dict[str, CaseContract] = {}
    for path in list_contract_paths(config.paths.cases):
        try:
            contract = load_contract(path)
        except Exception as exc:
            _add_finding(
                findings,
                id="contract_invalid",
                severity="blocker",
                category="case_contract",
                message="A case YAML exists but cannot be loaded as a valid contract.",
                evidence=[path],
                recommendation="Fix the YAML contract before relying on audit, evidence, or reports.",
                error=str(exc),
            )
            continue
        contracts[contract.case_id] = contract
    return contracts


def _audit_redmine_case_contracts(
    config: ProjectConfig,
    contracts: dict[str, CaseContract],
    findings: list[dict[str, Any]],
) -> None:
    for contract in contracts.values():
        if not _is_redmine_linked(contract):
            continue
        qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
        generic_runs = [command.run for command in contract.commands if "__quality_pilot_invalid_command__" in command.run]
        developer_runs = [command.run for command in contract.commands if _is_developer_redmine_command(command.run)]
        stale_scope = (
            str(qa.get("executable_scope") or "") == "side_effect_safe_probe"
            and not str(qa.get("safe_command_source") or "").strip()
        )
        if generic_runs or stale_scope:
            _add_finding(
                findings,
                id="redmine_generic_probe_invalid",
                severity="blocker",
                category="case_contract",
                message="A Redmine-linked case still uses a generic safe probe instead of a product-binary contract.",
                evidence=[contract.path],
                recommendation="Regenerate the Redmine case as a product-binary contract with explicit environment requirements.",
                case_id=contract.case_id,
                redmine_issue_id=_redmine_issue_id(contract),
                executable_scope=qa.get("executable_scope"),
            )
        if developer_runs:
            _add_finding(
                findings,
                id="redmine_developer_command_invalid",
                severity="blocker",
                category="case_contract",
                message="A Redmine-linked case uses a developer/build command instead of the product binary.",
                evidence=[contract.path],
                recommendation="Replace go test/go run/internal unit-test commands with a product-binary command and record binary/test-system/resource requirements.",
                case_id=contract.case_id,
                redmine_issue_id=_redmine_issue_id(contract),
                invalid_commands=developer_runs,
            )


def _is_developer_redmine_command(command: str) -> bool:
    normalized = re.sub(r"\s+", " ", command.strip().lower())
    return bool(
        re.search(r"(^|['\";(&|]\s*)go\s+(test|run)\b", normalized)
        or re.search(r"(^|['\";(&|]\s*)pytest\b", normalized)
        or re.search(r"(^|['\";(&|]\s*)python3?\s+-m\s+pytest\b", normalized)
    )


def _audit_generated_command_policy(
    config: ProjectConfig,
    contracts: dict[str, CaseContract],
    findings: list[dict[str, Any]],
) -> None:
    for contract in contracts.values():
        if not _is_generated_contract(contract):
            continue
        qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
        source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
        safe_runner = source.get("safe_runner") if isinstance(source.get("safe_runner"), dict) else {}
        source_type = str(qa.get("safe_command_source_type") or safe_runner.get("source_type") or "")
        allow_user_confirmed = source_type == "user_confirmed"
        invalid: list[dict[str, Any]] = []
        for command in contract.commands:
            result = validate_generated_command(
                config,
                command.run,
                source_type=source_type,
                allow_user_confirmed_runner=allow_user_confirmed,
            )
            if result.get("allowed"):
                continue
            invalid.append(
                {
                    "command_id": command.id,
                    "run": command.run,
                    "policy_reasons": result.get("reasons", []),
                    "command_kind": result.get("command_kind"),
                }
            )
        if not invalid:
            continue
        _add_finding(
            findings,
            id="generated_command_policy_violation",
            severity="blocker",
            category="case_contract",
            message="A generated case command does not satisfy the product-runtime command policy.",
            evidence=[contract.path],
            recommendation="Regenerate the case so commands[].run uses the configured/inferred product binary/API/runner, or a user-confirmed runner.",
            case_id=contract.case_id,
            invalid_commands=invalid,
        )


def _is_generated_contract(contract: CaseContract) -> bool:
    qa = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
    source_type = str(source.get("type") or "").lower()
    generation_mode = str(qa.get("generation_mode") or "").lower()
    if generation_mode:
        return True
    return source_type in {"issue", "redmine", "init", "growth", "from_scratch"}


def _audit_evidence_contract_consistency(
    config: ProjectConfig,
    contracts: dict[str, CaseContract],
    latest_run: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> None:
    for result, path, source in _iter_result_payloads(config, latest_run, findings):
        case_id = str(result.get("case_id") or "")
        if not case_id:
            continue
        contract = contracts.get(case_id)
        if contract is None:
            if str(result.get("status") or "") in TRUSTED_RESULT_STATUSES:
                _add_finding(
                    findings,
                    id="evidence_missing_case_contract",
                    severity="blocker",
                    category="evidence",
                    message="A trusted result exists for a case that no longer has a contract.",
                    evidence=[path],
                    recommendation="Recreate the case contract or discard the stale evidence from current status calculations.",
                    case_id=case_id,
                    result_status=result.get("status"),
                    source=source,
                )
            continue
        mismatches = _result_contract_mismatches(contract, result)
        if not mismatches:
            continue
        _add_finding(
            findings,
            id="evidence_contract_mismatch",
            severity="blocker",
            category="evidence",
            message="A trusted result does not match the current case contract command id/run/hash.",
            evidence=[path, contract.path],
            recommendation="Do not count this result as current PASS/FAIL/BLOCK; rerun the current contract or mark the report stale.",
            case_id=case_id,
            result_status=result.get("status"),
            source=source,
            mismatches=mismatches,
            current_contract_hash=contract.contract_hash,
            result_contract_hash=result.get("contract_hash"),
        )


def _audit_issue_traceability(config: ProjectConfig, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        payload = issue_status(config, persist_traceability=False)
    except IssueSyncError as exc:
        _add_finding(
            findings,
            id="issue_snapshot_invalid",
            severity="blocker",
            category="issue_mapping",
            message="Issue snapshot cannot be loaded for traceability audit.",
            evidence=[config.paths.state / "issues-snapshot.json"],
            recommendation="Regenerate the issue snapshot before fixing mapped issues.",
            error=str(exc),
        )
        return []
    traceability = [item for item in payload.get("traceability", []) if isinstance(item, dict)]
    for row in traceability:
        snapshot_case = str(row.get("snapshot_case_id") or "")
        case_id = str(row.get("case_id") or "")
        if not row.get("case_runnable"):
            _add_finding(
                findings,
                id="active_issue_missing_runnable_case",
                severity="blocker",
                category="issue_mapping",
                message="An active Gitea issue has no runnable linked case contract.",
                evidence=[config.paths.state / "issues-snapshot.json"],
                recommendation="Generate a runnable case from confirmed safe inputs, or mark the issue as needs_input/no_case.",
                gitea_issue_id=row.get("gitea_issue_id"),
                redmine_issue_ids=row.get("redmine_issue_ids", []),
                snapshot_case_id=snapshot_case or None,
                title=row.get("title"),
                coverage_status=row.get("coverage_status"),
                repair_action=row.get("repair_action"),
            )
        elif snapshot_case and snapshot_case != case_id:
            _add_finding(
                findings,
                id="stale_issue_case_alias",
                severity="warning",
                category="issue_mapping",
                message="The issue snapshot points to a non-canonical case id, but a runnable case can be recovered.",
                evidence=[config.paths.state / "issues-snapshot.json"],
                recommendation="Rewrite future handoffs to the canonical runnable case id.",
                gitea_issue_id=row.get("gitea_issue_id"),
                redmine_issue_ids=row.get("redmine_issue_ids", []),
                snapshot_case_id=snapshot_case,
                canonical_case_id=case_id,
            )
    return traceability


def _audit_redmine_handoffs(config: ProjectConfig, findings: list[dict[str, Any]]) -> None:
    import_path = config.paths.state / "redmine-import.json"
    imported = _load_json(import_path, findings, required=False)
    if isinstance(imported, dict) and "qa_summary" not in imported:
        _add_finding(
            findings,
            id="redmine_import_missing_qa_summary",
            severity="warning",
            category="redmine_handoff",
            message="Redmine import state was generated by an older flow and has no QA summary.",
            evidence=[import_path],
            recommendation="Regenerate Redmine sync state so QA can review problem, environment, reproduction, expected/actual, evidence, and missing testcase inputs.",
            mode=imported.get("mode"),
            schema=imported.get("schema"),
        )

    sync_path = config.paths.state / "redmine-gitea-sync-state.json"
    sync_state = _load_json(sync_path, findings, required=False)
    candidates = sync_state.get("issue_candidates") if isinstance(sync_state, dict) else None
    if isinstance(candidates, list):
        stale = [
            item.get("id")
            for item in candidates
            if isinstance(item, dict)
            and (not isinstance(item.get("qa_summary"), dict) or "## QA Focus" not in str(item.get("body") or ""))
        ]
        if stale:
            _add_finding(
                findings,
                id="redmine_gitea_sync_missing_qa_handoff",
                severity="warning",
                category="redmine_handoff",
                message="Redmine to Gitea sync state lacks the current QA handoff details.",
                evidence=[sync_path],
                recommendation="Regenerate the sync state with QA Focus and redmine_issue_summary handoff fields.",
                candidate_ids=stale[:10],
            )


def _audit_gitea_handoffs(config: ProjectConfig, findings: list[dict[str, Any]]) -> None:
    request_path = config.paths.state / "gitea-mcp" / "issue-write-request.json"
    result_path = config.paths.state / "gitea-mcp" / "issue-write-result.json"
    request = _load_json(request_path, findings, required=False)
    result = _load_json(result_path, findings, required=False)
    if isinstance(request, dict):
        actions = [item for item in request.get("actions", []) if isinstance(item, dict)]
        missing_qa = [
            action.get("id") or action.get("redmine_issue_id")
            for action in actions
            if not isinstance(action.get("qa_summary"), dict)
            or not isinstance(action.get("qa_text_generation"), dict)
            or "## QA Focus" not in str(action.get("body") or "")
        ]
        if missing_qa:
            _add_finding(
                findings,
                id="gitea_handoff_missing_qa_summary",
                severity="warning",
                category="gitea_handoff",
                message="Gitea issue write request does not contain the current human-readable QA handoff.",
                evidence=[request_path],
                recommendation="Regenerate the Redmine sync request before creating or reviewing Gitea issues.",
                action_ids=missing_qa[:10],
            )
        raw_body = [action.get("id") for action in actions if "Raw Redmine JSON" in str(action.get("body") or "")]
        if raw_body:
            _add_finding(
                findings,
                id="gitea_handoff_contains_raw_redmine_json",
                severity="warning",
                category="gitea_handoff",
                message="Gitea issue body request still contains raw Redmine JSON.",
                evidence=[request_path],
                recommendation="Keep raw Redmine JSON in local mirrors only; remote Gitea issues should be human-readable.",
                action_ids=raw_body[:10],
            )
    if isinstance(request, dict) and isinstance(result, dict):
        request_status = str(request.get("status") or "")
        result_status = str(result.get("status") or "")
        if request_status == "needs_mcp_apply" and result_status in {"applied", "ok", "success"}:
            _add_finding(
                findings,
                id="stale_mcp_issue_write_request",
                severity="warning",
                category="gitea_handoff",
                message="An MCP issue-write result is already applied while the request still says needs_mcp_apply.",
                evidence=[request_path, result_path],
                recommendation="Treat the request as stale and use the applied result or regenerate a fresh gated request.",
                operation=request.get("operation"),
                created_count=result.get("created_count"),
            )


def _audit_wiki_handoffs(config: ProjectConfig, findings: list[dict[str, Any]]) -> None:
    request_path = configured_mcp_json_path(config, "wiki_write_request_json")
    result_path = configured_mcp_json_path(config, "wiki_write_result_json")
    request = _load_json(request_path, findings, required=False)
    result = _load_json(result_path, findings, required=False)
    if not isinstance(request, dict) or not isinstance(result, dict):
        return
    if request.get("schema") and result.get("request_schema") and request.get("schema") != result.get("request_schema"):
        _add_finding(
            findings,
            id="wiki_mcp_result_request_schema_mismatch",
            severity="blocker",
            category="wiki_handoff",
            message="Wiki MCP write result does not point back to the request schema that produced it.",
            evidence=[request_path, result_path],
            recommendation="Discard the stale Wiki MCP result or regenerate/apply a fresh Wiki write request.",
            request_schema=request.get("schema"),
            result_request_schema=result.get("request_schema"),
        )
    request_status = str(request.get("status") or "")
    result_status = str(result.get("status") or "")
    if request_status == "needs_mcp_apply" and result_status in {"applied", "ok", "success"}:
        _add_finding(
            findings,
            id="stale_mcp_wiki_write_request",
            severity="warning",
            category="wiki_handoff",
            message="A Wiki MCP write result is already applied while the request still says needs_mcp_apply.",
            evidence=[request_path, result_path],
            recommendation="Treat the Wiki request as stale and regenerate status before publishing again.",
            page=request.get("page"),
            event=request.get("event"),
        )


def _audit_fix_plan(
    config: ProjectConfig,
    contracts: dict[str, CaseContract],
    traceability: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> None:
    path = config.paths.state / "fix-plan.json"
    plan = _load_json(path, findings, required=False)
    if not isinstance(plan, dict):
        return
    referenced = _fix_plan_case_ids(plan)
    if not referenced:
        return
    issue_id = plan.get("issue_id")
    row = next((item for item in traceability if item.get("gitea_issue_id") == issue_id), None)
    canonical = row.get("case_id") if isinstance(row, dict) else None
    missing = [case_id for case_id in referenced if case_id not in contracts]
    if missing:
        _add_finding(
            findings,
            id="fix_plan_non_runnable_case",
            severity="blocker",
            category="issue_mapping",
            message="The latest fix plan references case ids that are not runnable contracts.",
            evidence=[path],
            recommendation="Regenerate the fix plan after resolving aliases to canonical runnable case ids.",
            issue_id=issue_id,
            missing_case_ids=missing,
            canonical_case_id=canonical,
        )


def _audit_reports(
    config: ProjectConfig,
    latest_run: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> None:
    wiki_path = config.paths.reports / "wiki-status.md"
    status_path = config.paths.reports / "status.md"
    wiki_text = _read_text(wiki_path)
    status_text = _read_text(status_path)
    if not wiki_text:
        return
    latest_status_by_case = {
        str(result.get("case_id")): str(result.get("status") or "")
        for result in latest_run.get("results", []) if isinstance(result, dict) and result.get("case_id")
    } if isinstance(latest_run, dict) else {}
    wiki_status_by_case = _markdown_case_statuses(wiki_text)
    disagreements = [
        {
            "case_id": case_id,
            "latest_run_status": status,
            "wiki_status": wiki_status_by_case.get(case_id),
        }
        for case_id, status in sorted(latest_status_by_case.items())
        if wiki_status_by_case.get(case_id) and wiki_status_by_case.get(case_id) != status
    ]
    if disagreements:
        _add_finding(
            findings,
            id="report_truth_disagreement",
            severity="blocker",
            category="reporting",
            message="latest-run/status and wiki report disagree for one or more cases.",
            evidence=[config.paths.state / "latest-run.json", status_path, wiki_path],
            recommendation="Regenerate reports from one current run source before claiming readiness.",
            disagreements=disagreements,
        )
    wiki_ready = bool(re.search(r"Status[：:]\s*READY\b", wiki_text))
    statuses = [status for status in wiki_status_by_case.values() if status]
    if wiki_ready and statuses and all(status == "NOT_RUN" for status in statuses):
        _add_finding(
            findings,
            id="wiki_ready_without_execution",
            severity="blocker",
            category="reporting",
            message="Wiki release readiness says READY while all listed cases are NOT_RUN.",
            evidence=[wiki_path],
            recommendation="Downgrade readiness and regenerate Wiki from current execution evidence.",
            case_count=len(statuses),
        )


def _audit_mcp_status(config: ProjectConfig, findings: list[dict[str, Any]]) -> None:
    status = hermes_mcp_status(config)
    if status.get("known"):
        return
    expected_json = {"servers": ["gitea", "redmine"]}
    _add_finding(
        findings,
        id="hermes_mcp_status_missing",
        severity="blocker",
        category="mcp",
        message="Hermes MCP status JSON is missing or unreadable, so remote write readiness is not reproducible.",
        evidence=[hermes_mcp_status_path(config)],
        recommendation="Write the configured MCP status JSON or set QUALITY_PILOT_HERMES_MCP_SERVERS before remote write flows.",
        status_path=status.get("status_path"),
        source=status.get("source"),
        error=status.get("error"),
        expected_minimal_json=expected_json,
    )


def _audit_subagents(config: ProjectConfig, findings: list[dict[str, Any]]) -> None:
    status = subagent_status(config)
    missing_profile = list(status.get("missing_user_fields", []))
    missing_prompts = list(status.get("missing_task_prompts", []))
    if not missing_profile and not missing_prompts and status.get("configured"):
        return
    _add_finding(
        findings,
        id="subagent_profile_incomplete",
        severity="warning",
        category="subagent",
        message="Subagent profile is not operationally configured with an Open WebUI model.",
        evidence=[config.path],
        recommendation="Set subagents.profiles.open-webui.model or paste an endpoint containing ?model=<name>; task prompts are optional overrides.",
        configured=status.get("configured"),
        endpoint=status.get("endpoint"),
        model=status.get("model"),
        model_source=status.get("model_source"),
        missing_user_fields=missing_profile,
        missing_task_prompts=missing_prompts,
    )


def _iter_result_payloads(
    config: ProjectConfig,
    latest_run: dict[str, Any] | None,
    findings: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], Path, str]]:
    payloads: list[tuple[dict[str, Any], Path, str]] = []
    latest_path = config.paths.state / "latest-run.json"
    if isinstance(latest_run, dict):
        for result in latest_run.get("results", []):
            if isinstance(result, dict):
                payloads.append((result, latest_path, "latest-run"))
    for path in sorted(config.paths.evidence.glob("*/result.json")) if config.paths.evidence.exists() else []:
        result = _load_json(path, findings, required=False)
        if isinstance(result, dict):
            payloads.append((result, path, "evidence-result"))
    return payloads


def _result_contract_mismatches(contract: CaseContract, result: dict[str, Any]) -> list[dict[str, Any]]:
    if str(result.get("status") or "") not in TRUSTED_RESULT_STATUSES:
        return []
    mismatches: list[dict[str, Any]] = []
    result_hash = str(result.get("contract_hash") or "")
    if result_hash and result_hash != contract.contract_hash:
        mismatches.append({"type": "contract_hash", "result": result_hash, "current": contract.contract_hash})
    contract_commands = {command.id: command for command in contract.commands}
    result_commands = result.get("commands") if isinstance(result.get("commands"), list) else []
    for command in result_commands:
        if not isinstance(command, dict):
            continue
        command_id = str(command.get("id") or "")
        expected = contract_commands.get(command_id)
        if expected is None:
            mismatches.append({"type": "command_id", "result": command_id, "current_ids": sorted(contract_commands)})
            continue
        if str(command.get("run") or "") != expected.run:
            mismatches.append({"type": "command_run", "command_id": command_id})
        if _int_or_none(command.get("expected_exit_code")) != expected.expected_exit_code:
            mismatches.append({"type": "expected_exit_code", "command_id": command_id})
    return mismatches


def _is_redmine_linked(contract: CaseContract) -> bool:
    source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
    text = " ".join(str(source.get(key) or "") for key in ("type", "provider", "redmine_issue_id", "redmine_url"))
    return contract.case_id.upper().startswith("REDMINE-") or "redmine" in text.lower()


def _redmine_issue_id(contract: CaseContract) -> int | None:
    source = contract.raw.get("source") if isinstance(contract.raw.get("source"), dict) else {}
    value = source.get("redmine_issue_id")
    if value is None:
        match = re.search(r"REDMINE-(\d+)", contract.case_id, flags=re.IGNORECASE)
        value = match.group(1) if match else None
    return _int_or_none(value)


def _fix_plan_case_ids(plan: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in plan.get("case_ids", []):
        if str(item or "").strip():
            out.append(str(item).strip())
    for command in plan.get("preflight", []):
        match = re.search(r"cases\s+run\s+([A-Za-z0-9_.:-]+)", str(command))
        if match:
            out.append(match.group(1))
    return sorted(set(out))


def _markdown_case_statuses(text: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    known = {"PASS", "FAIL", "BLOCK", "ABORT", "NOT_RUN", "DRAFT", "NEEDS_INPUT"}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "case":
            continue
        status = next((cell for cell in cells[1:] if cell in known), None)
        if status:
            statuses[cells[0]] = status
    return statuses


def _load_json(path: Path, findings: list[dict[str, Any]], *, required: bool) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            _add_finding(
                findings,
                id="state_file_missing",
                severity="blocker",
                category="state",
                message="Required state file is missing.",
                evidence=[path],
                recommendation="Regenerate the missing state file before continuing.",
            )
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _add_finding(
            findings,
            id="state_file_invalid_json",
            severity="blocker",
            category="state",
            message="State file is not valid JSON.",
            evidence=[path],
            recommendation="Regenerate the invalid state file before continuing.",
            error=str(exc),
        )
        return None
    return loaded if isinstance(loaded, dict) else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _state_artifacts(config: ProjectConfig) -> list[dict[str, Any]]:
    candidates = [
        config.paths.state / "issues-snapshot.json",
        config.paths.state / "redmine-import.json",
        config.paths.state / "redmine-gitea-sync-state.json",
        config.paths.state / "gitea-mcp" / "issue-write-request.json",
        config.paths.state / "gitea-mcp" / "issue-write-result.json",
        configured_mcp_json_path(config, "wiki_write_request_json"),
        configured_mcp_json_path(config, "wiki_write_result_json"),
        config.paths.state / "latest-run.json",
        config.paths.state / "fix-plan.json",
        hermes_mcp_status_path(config),
        config.paths.reports / "status.md",
        config.paths.reports / "wiki-status.md",
    ]
    artifacts: list[dict[str, Any]] = []
    for path in candidates:
        artifacts.append(_state_artifact(config, path))
    return artifacts


def _state_artifact(config: ProjectConfig, path: Path) -> dict[str, Any]:
    payload = {
        "path": _relative_or_str(path, config.root),
        "exists": path.exists(),
    }
    if not path.exists():
        return payload
    try:
        stat = path.stat()
        payload["mtime_utc"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        payload["size_bytes"] = stat.st_size
    except OSError:
        return payload
    if path.suffix.lower() == ".json":
        loaded = _load_json_for_artifact(path)
        if isinstance(loaded, dict):
            for key in ("schema", "status", "source", "mode", "operation", "event", "run_id", "synced_at", "created_at", "fetched_at"):
                if loaded.get(key) is not None:
                    payload[key] = loaded.get(key)
            if isinstance(loaded.get("issues"), list):
                payload["issue_count"] = len(loaded["issues"])
            if isinstance(loaded.get("items"), list):
                payload["item_count"] = len(loaded["items"])
            if isinstance(loaded.get("actions"), list):
                payload["action_count"] = len(loaded["actions"])
            if isinstance(loaded.get("results"), list):
                payload["result_count"] = len(loaded["results"])
        else:
            payload["json_valid"] = False
    else:
        payload["source_type"] = "markdown"
    return payload


def _load_json_for_artifact(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _add_finding(
    findings: list[dict[str, Any]],
    *,
    id: str,
    severity: str,
    category: str,
    message: str,
    evidence: list[Path],
    recommendation: str,
    **details: Any,
) -> None:
    finding = {
        "id": id,
        "severity": severity,
        "category": category,
        "message": message,
        "evidence": [_relative_or_str(path, path_anchor(path)) for path in evidence],
        "recommendation": recommendation,
    }
    finding.update({key: value for key, value in details.items() if value is not None})
    findings.append(finding)


def path_anchor(path: Path) -> Path:
    parts = path.resolve().parts
    if ".quality-pilot-project" in parts:
        index = parts.index(".quality-pilot-project")
        return Path(*parts[:index])
    return path.parent


def _count_by_severity(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"blocker": 0, "warning": 0, "info": 0}
    for finding in findings:
        severity = str(finding.get("severity") or "info")
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _next_actions(findings: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for finding in findings:
        finding_id = str(finding.get("id") or "")
        if finding_id in {"redmine_generic_probe_invalid", "redmine_developer_command_invalid"}:
            redmine_id = finding.get("redmine_issue_id")
            actions.append(f"/quality-pilot cases generate --redmine-issues {redmine_id}" if redmine_id else "/quality-pilot cases generate --redmine-issues <redmine_issue_id>")
        elif finding_id == "evidence_contract_mismatch":
            case_id = finding.get("case_id")
            actions.append(f"/quality-pilot cases run {case_id}" if case_id else "/quality-pilot cases run <case_id>")
        elif finding_id == "active_issue_missing_runnable_case":
            repair = finding.get("repair_action")
            actions.append(str(repair) if repair else "/quality-pilot issues status")
        elif finding_id == "fix_plan_non_runnable_case":
            issue_id = finding.get("issue_id")
            actions.append(f"/quality-pilot issues fix --issue {issue_id}" if issue_id else "/quality-pilot issues status")
        elif finding_id == "hermes_mcp_status_missing":
            actions.append("/quality-pilot doctor")
        elif finding_id in {"redmine_import_missing_qa_summary", "gitea_handoff_missing_qa_summary"}:
            redmine_ids = _redmine_ids_from_finding(finding)
            actions.append(f"/quality-pilot issues sync --redmine-issues {' '.join(str(item) for item in redmine_ids)}" if redmine_ids else "/quality-pilot issues sync --redmine-issues <redmine_issue_id>")
        elif finding_id in {"stale_mcp_wiki_write_request", "wiki_mcp_result_request_schema_mismatch", "report_truth_disagreement", "wiki_ready_without_execution"}:
            actions.append("/quality-pilot publish wiki plan")
    return _unique(actions)


def _redmine_ids_from_finding(finding: dict[str, Any]) -> list[int]:
    values: list[Any] = []
    for key in ("redmine_issue_ids", "action_ids", "candidate_ids"):
        raw = finding.get(key)
        if isinstance(raw, list):
            values.extend(raw)
    out: list[int] = []
    for value in values:
        if isinstance(value, int):
            out.append(value)
            continue
        match = re.search(r"redmine-(\d+)", str(value), flags=re.IGNORECASE)
        if match:
            out.append(int(match.group(1)))
    return sorted(set(out))


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)
