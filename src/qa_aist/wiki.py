from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps, load_yaml
from .contracts import list_contract_paths
from .gitea import GiteaClient, GiteaError, gitea_config_from_project
from .issues import load_issue_snapshot
from .runner import utc_now
from .write_gate import evaluate_write_gate

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

WIKI_PLAN_NAME = "wiki-plan.json"
WIKI_APPLY_NAME = "wiki-apply-result.json"
WIKI_REPORT_NAME = "wiki-status.md"
WIKI_CATEGORIES_NAME = "wiki-categories.yaml"
DEFAULT_WIKI_PAGE = "Test status (Siri)"
WIKI_SCHEMA = "qa-aist.wiki-plan.v1"

WIKI_EVENTS = {"manual", "case_generation", "test_result", "gitea_write_summary"}


class WikiPublishError(RuntimeError):
    pass


def plan_wiki(
    config: ProjectConfig,
    *,
    event: str = "manual",
    latest_run: str | Path | dict[str, Any] | None = None,
    source_payload: dict[str, Any] | None = None,
    gitea_write_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = _normalize_event(event)
    run = _load_latest_run(config, latest_run)
    cases = _load_case_rows(config, run=run, event=event)
    issues = load_issue_snapshot(config)
    categories = _load_category_rules(config)
    page = gitea_config_from_project(config.data).wiki_page or DEFAULT_WIKI_PAGE
    body = render_wiki_body(
        page=page,
        event=event,
        cases=cases,
        issue_snapshot=issues,
        category_rules=categories,
        latest_run=run,
        source_payload=source_payload,
        gitea_write_result=gitea_write_result,
    )
    report_path = write_wiki_report(config, body)
    gate = evaluate_wiki_gate(
        config,
        event=event,
        body=body,
        cases=cases,
        latest_run=run,
        gitea_write_result=gitea_write_result,
    )
    remote = wiki_remote_readiness(config, gate)
    blocked_reasons = list(gate.get("reason_codes", [])) + list(remote.get("blockers", []))
    status = "ready" if gate.get("allowed") and remote.get("remote_write_ready") else "blocked"
    plan = {
        "schema": WIKI_SCHEMA,
        "status": status,
        "event": event,
        "page": page,
        "message": _wiki_commit_message(event),
        "body": body,
        "report_path": _relative_or_str(report_path, config.root),
        "provider": "gitea",
        "remote": remote,
        "write_gate_result": gate,
        "blocked_by_gate": 0 if gate.get("allowed") else 1,
        "blocked_reasons": sorted(set(str(item) for item in blocked_reasons if item)),
        "next_action": "/qa-aist publish wiki apply" if status == "ready" else "/qa-aist publish wiki status",
        "created_at": utc_now(),
    }
    path = wiki_plan_path(config)
    config.paths.state.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(plan) + "\n", encoding="utf-8")
    return {**plan, "plan_path": _relative_or_str(path, config.root)}


def apply_wiki_plan(config: ProjectConfig, *, plan_path: str | Path | None = None) -> dict[str, Any]:
    plan = load_wiki_plan(config, plan_path)
    gate = plan.get("write_gate_result") if isinstance(plan.get("write_gate_result"), dict) else {}
    remote = wiki_remote_readiness(config, gate)
    if plan.get("status") != "ready" or not gate.get("allowed") or not remote.get("remote_write_ready"):
        payload = {
            "status": "blocked",
            "error": "wiki_write_blocked",
            "page": plan.get("page"),
            "blocked_by_gate": 0 if gate.get("allowed") else 1,
            "blocked_reasons": sorted(set(list(plan.get("blocked_reasons", [])) + list(remote.get("blockers", [])))),
            "write_gate_result": gate,
            "remote": remote,
        }
        _write_wiki_apply_result(config, payload)
        return payload

    gitea_cfg = gitea_config_from_project(config.data)
    try:
        response = GiteaClient(gitea_cfg).update_wiki_page(
            page=str(plan.get("page") or gitea_cfg.wiki_page),
            content=str(plan.get("body") or ""),
            message=str(plan.get("message") or "QA-AIST wiki status update"),
        )
    except GiteaError:
        raise
    payload = {
        "status": "ok",
        "event": plan.get("event"),
        "page": plan.get("page"),
        "applied_count": 1,
        "applied": [{"id": "wiki-status", "type": "wiki_update", "response": response}],
        "remote": remote,
    }
    path = _write_wiki_apply_result(config, payload)
    return {**payload, "apply_result_path": _relative_or_str(path, config.root)}


def render_wiki(config: ProjectConfig, *, event: str = "manual", latest_run: str | Path | dict[str, Any] | None = None) -> dict[str, Any]:
    event = _normalize_event(event)
    run = _load_latest_run(config, latest_run)
    page = gitea_config_from_project(config.data).wiki_page or DEFAULT_WIKI_PAGE
    body = render_wiki_body(
        page=page,
        event=event,
        cases=_load_case_rows(config, run=run, event=event),
        issue_snapshot=load_issue_snapshot(config),
        category_rules=_load_category_rules(config),
        latest_run=run,
    )
    report_path = write_wiki_report(config, body)
    return {
        "status": "ok",
        "event": event,
        "page": page,
        "report_path": _relative_or_str(report_path, config.root),
        "body": body,
    }


def wiki_status(config: ProjectConfig) -> dict[str, Any]:
    plan_path = wiki_plan_path(config)
    apply_path = wiki_apply_path(config)
    report_path = wiki_report_path(config)
    plan = load_wiki_plan(config) if plan_path.exists() else None
    apply_result = _load_json_file(apply_path) if apply_path.exists() else None
    return {
        "status": "ok",
        "page": gitea_config_from_project(config.data).wiki_page,
        "plan_exists": plan_path.exists(),
        "plan_path": _relative_or_str(plan_path, config.root),
        "apply_result_exists": apply_path.exists(),
        "apply_result_path": _relative_or_str(apply_path, config.root),
        "report_exists": report_path.exists(),
        "report_path": _relative_or_str(report_path, config.root),
        "blocked_by_gate": plan.get("blocked_by_gate") if isinstance(plan, dict) else None,
        "blocked_reasons": plan.get("blocked_reasons", []) if isinstance(plan, dict) else [],
        "last_event": plan.get("event") if isinstance(plan, dict) else None,
        "last_apply_status": apply_result.get("status") if isinstance(apply_result, dict) else None,
        "remote": wiki_remote_readiness(config, plan.get("write_gate_result", {}) if isinstance(plan, dict) else {}),
    }


def auto_sync_wiki(
    config: ProjectConfig,
    *,
    event: str,
    latest_run: str | Path | dict[str, Any] | None = None,
    source_payload: dict[str, Any] | None = None,
    gitea_write_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _auto_wiki_enabled(config):
        return {
            "auto_sync": {"status": "disabled", "event": event},
            "page": gitea_config_from_project(config.data).wiki_page,
            "next_action": "/qa-aist publish wiki plan",
        }
    try:
        plan = plan_wiki(
            config,
            event=event,
            latest_run=latest_run,
            source_payload=source_payload,
            gitea_write_result=gitea_write_result,
        )
        payload = _wiki_payload_from_plan(plan, status="planned")
        if plan.get("status") == "ready":
            applied = apply_wiki_plan(config)
            payload["auto_sync"] = {
                "status": "applied" if applied.get("status") == "ok" else "blocked",
                "event": event,
                "remote_apply": applied.get("status") == "ok",
                "reason": applied.get("error"),
            }
            payload["apply_result_path"] = applied.get("apply_result_path") or payload.get("apply_result_path")
            payload["blocked_by_gate"] = applied.get("blocked_by_gate", payload.get("blocked_by_gate"))
            payload["next_action"] = "/qa-aist publish wiki status"
            return payload
        payload["auto_sync"] = {
            "status": "blocked",
            "event": event,
            "remote_apply": False,
            "reason": ",".join(plan.get("blocked_reasons", [])) or "wiki_write_blocked",
        }
        return payload
    except Exception as exc:  # Keep Wiki sync from hiding the primary command result.
        return {
            "auto_sync": {"status": "error", "event": event, "message": str(exc), "error": type(exc).__name__},
            "page": gitea_config_from_project(config.data).wiki_page,
            "next_action": "/qa-aist publish wiki status",
        }


def wiki_readiness(config: ProjectConfig) -> dict[str, Any]:
    return wiki_remote_readiness(config, {})


def evaluate_wiki_gate(
    config: ProjectConfig,
    *,
    event: str,
    body: str,
    cases: list[dict[str, Any]],
    latest_run: dict[str, Any] | None,
    gitea_write_result: dict[str, Any] | None,
) -> dict[str, Any]:
    event = _normalize_event(event)
    result = _gate_result_for_event(event, cases=cases, latest_run=latest_run, gitea_write_result=gitea_write_result)
    gate = evaluate_write_gate(
        config_data=config.data,
        result=result,
        target_state="open",
        sync_current=True,
        write_text=body,
    ).as_dict()
    reasons = list(gate.get("reason_codes", []))
    if event == "test_result" and not _latest_run_has_current_evidence(latest_run):
        reasons.append("missing_current_evidence")
    if event == "gitea_write_summary" and not _valid_gitea_write_result(gitea_write_result):
        reasons.append("missing_gated_write_result")
    if _contains_raw_secret_text(body):
        reasons.append("raw_secret_detected")
    reasons = sorted(set(reasons))
    allowed = not reasons
    gate.update(
        {
            "allowed": allowed,
            "reason": "allowed" if allowed else reasons[0],
            "reason_codes": reasons,
            "wiki_gate_mode": event,
        }
    )
    return gate


def wiki_remote_readiness(config: ProjectConfig, gate: dict[str, Any]) -> dict[str, Any]:
    tracker = config.data.get("tracker") if isinstance(config.data.get("tracker"), dict) else {}
    provider = str(tracker.get("provider", "none")).lower()
    gitea = gitea_config_from_project(config.data)
    blockers: list[str] = []
    if provider != "gitea":
        blockers.append("tracker_disabled")
    if gitea.uses_mcp:
        blockers.append("gitea_mcp_write_not_supported")
    if not gitea.base_url or not gitea.repo:
        blockers.append("gitea_not_configured")
    if gitea.uses_http and not gitea.token:
        blockers.append("gitea_http_token_missing")
    if gate and not gate.get("allowed", False):
        blockers.append("write_gate_blocked")
    return {
        "provider": provider,
        "backend": gitea.backend,
        "page": gitea.wiki_page,
        "token_env": gitea.token_env,
        "token_present": bool(gitea.token),
        "remote_write_ready": provider == "gitea" and gitea.uses_http and bool(gitea.base_url and gitea.repo and gitea.token) and bool(gate.get("allowed", True)),
        "blockers": sorted(set(blockers)),
    }


def render_wiki_body(
    *,
    page: str,
    event: str,
    cases: list[dict[str, Any]],
    issue_snapshot: dict[str, Any],
    category_rules: dict[str, Any],
    latest_run: dict[str, Any] | None,
    source_payload: dict[str, Any] | None = None,
    gitea_write_result: dict[str, Any] | None = None,
) -> str:
    counts = _count_statuses(cases)
    open_issues = [item for item in issue_snapshot.get("items", []) if isinstance(item, dict)]
    grouped = _group_cases_by_category(cases, category_rules)
    lines = [
        f"# {page or DEFAULT_WIKI_PAGE}",
        "",
        "## 總覽",
        "",
        f"- 更新事件：{_event_label(event)}",
        f"- 產生時間：{utc_now()}",
        f"- Cases：{len(cases)}",
        f"- PASS：{counts.get('PASS', 0)}",
        f"- FAIL：{counts.get('FAIL', 0)}",
        f"- BLOCK：{counts.get('BLOCK', 0)}",
        f"- DRAFT/NEEDS_INPUT：{counts.get('DRAFT', 0) + counts.get('NEEDS_INPUT', 0)}",
        f"- Active Gitea issues：{len(open_issues)}",
        "",
        "## 測試結果明細",
        "",
        "| Case | Category | Status | Feature | Title |",
        "|---|---|---|---|---|",
    ]
    if cases:
        for case in cases:
            lines.append(
                f"| {_md(case.get('case_id'))} | {_md(case.get('category'))} | {_md(case.get('status'))} | {_md(case.get('feature'))} | {_md(case.get('title'))} |"
            )
    else:
        lines.append("| - | Uncategorized | NOT_RUN | - | No case contracts yet |")

    for category, items in grouped.items():
        lines.extend(["", f"### {category}", "", "| Case | Status | SWQA dimensions | Missing input |", "|---|---|---|---|"])
        for case in items:
            dimensions = ", ".join(str(item) for item in case.get("swqa_dimensions", [])[:8]) or "-"
            missing = str(case.get("question_count") or 0)
            lines.append(f"| {_md(case.get('case_id'))} | {_md(case.get('status'))} | {_md(dimensions)} | {missing} |")

    lines.extend(
        [
            "",
            "## 補充 partial probes（不併入正式 case counters）",
            "",
        ]
    )
    probes = [case for case in cases if case.get("partial_probe")]
    if probes:
        for case in probes:
            lines.append(f"- {_md(case.get('case_id'))}: {_md(case.get('title'))}")
    else:
        lines.append("_目前沒有標記為 partial probe 的補充探測。_")

    lines.extend(["", "## 活動中的 Gitea issues", ""])
    if open_issues:
        lines.extend(["| Issue | Labels | Linked case | Title |", "|---:|---|---|---|"])
        for item in open_issues:
            labels = ", ".join(str(label) for label in item.get("labels", [])) or "-"
            lines.append(f"| #{item.get('issue_id')} | {_md(labels)} | {_md(item.get('case_id'))} | {_md(item.get('title'))} |")
    else:
        lines.append("_目前沒有本地 active issue snapshot。_")

    lines.extend(["", "## 已關閉／歷史 issues（不列 active blocker）", ""])
    lines.append("_Closed issue 由 Gitea sync 作為遠端事實來源；本頁不把 closed/history issue 列為 active blocker。_")

    lines.extend(["", "## 六色帽回顧", ""])
    hats = _six_hat_rows(cases)
    if hats:
        lines.extend(["| Case | White | Red | Black | Yellow | Green | Blue |", "|---|---|---|---|---|---|---|"])
        for row in hats[:12]:
            lines.append(
                f"| {_md(row.get('case_id'))} | {_md(row.get('white'))} | {_md(row.get('red'))} | {_md(row.get('black'))} | {_md(row.get('yellow'))} | {_md(row.get('green'))} | {_md(row.get('blue'))} |"
            )
    else:
        lines.append("_目前沒有六色帽 metadata。`cases generate --init` 或 `--growing` 會補上。_")

    if source_payload and event == "case_generation":
        lines.extend(["", "## 生成摘要", ""])
        lines.append(f"- generated：{source_payload.get('generated_count', len(source_payload.get('generated', [])) if isinstance(source_payload.get('generated'), list) else 0)}")
        lines.append(f"- deduped：{source_payload.get('deduped_count', 0)}")
        lines.append(f"- questions：{len(source_payload.get('questions', [])) if isinstance(source_payload.get('questions'), list) else 0}")
    if gitea_write_result and event == "gitea_write_summary":
        lines.extend(["", "## Gitea 寫入摘要", ""])
        lines.append(f"- status：{gitea_write_result.get('status')}")
        if gitea_write_result.get("applied_count") is not None:
            lines.append(f"- applied_count：{gitea_write_result.get('applied_count')}")
        if gitea_write_result.get("issue_id") is not None:
            lines.append(f"- issue：#{gitea_write_result.get('issue_id')}")
    lines.extend(["", "_Generated by QA-AIST deterministic Wiki renderer._", ""])
    return "\n".join(lines)


def write_wiki_report(config: ProjectConfig, body: str) -> Path:
    path = wiki_report_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def load_wiki_plan(config: ProjectConfig, plan_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(plan_path) if plan_path else wiki_plan_path(config)
    if not path.exists():
        raise WikiPublishError(f"wiki plan not found: {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise WikiPublishError("wiki plan must be a JSON object")
    return loaded


def wiki_plan_path(config: ProjectConfig) -> Path:
    return config.paths.state / WIKI_PLAN_NAME


def wiki_apply_path(config: ProjectConfig) -> Path:
    return config.paths.state / WIKI_APPLY_NAME


def wiki_report_path(config: ProjectConfig) -> Path:
    return config.paths.reports / WIKI_REPORT_NAME


def wiki_categories_path(config: ProjectConfig) -> Path:
    return config.paths.rules / WIKI_CATEGORIES_NAME


def _load_latest_run(config: ProjectConfig, latest_run: str | Path | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(latest_run, dict):
        return latest_run
    path = Path(latest_run) if latest_run else config.paths.state / "latest-run.json"
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _load_case_rows(config: ProjectConfig, *, run: dict[str, Any] | None, event: str) -> list[dict[str, Any]]:
    result_by_case = {}
    if event != "case_generation" and isinstance(run, dict):
        for result in run.get("results", []):
            if isinstance(result, dict) and result.get("case_id"):
                result_by_case[str(result["case_id"])] = result
    snapshot = load_issue_snapshot(config)
    issue_by_id = {
        int(item.get("issue_id")): item
        for item in snapshot.get("items", [])
        if isinstance(item, dict) and item.get("issue_id") is not None
    }
    rows: list[dict[str, Any]] = []
    for path in list_contract_paths(config.paths.cases):
        try:
            data = load_yaml(path)
        except Exception:
            continue
        case_id = str(data.get("case_id") or path.stem)
        qa = data.get("qa_aist") if isinstance(data.get("qa_aist"), dict) else {}
        result = result_by_case.get(case_id)
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        source_issue = _source_issue(source, issue_by_id)
        questions = qa.get("questions") if isinstance(qa.get("questions"), list) else []
        status = _case_status(data, qa, result)
        rows.append(
            {
                "case_id": case_id,
                "title": str(data.get("title") or case_id),
                "feature": str(data.get("feature") or ""),
                "source": source,
                "source_issue": source_issue,
                "status": status,
                "exit_code": result.get("exit_code") if isinstance(result, dict) else None,
                "swqa_dimensions": data.get("swqa_dimensions", data.get("swqa_expansion", [])) if isinstance(data, dict) else [],
                "question_count": len(questions),
                "review_required_before_run": bool(qa.get("review_required_before_run")),
                "six_hats": data.get("six_hats") if isinstance(data.get("six_hats"), dict) else {},
                "commands": data.get("commands", []),
                "wiki": data.get("wiki") if isinstance(data.get("wiki"), dict) else {},
                "partial_probe": bool(data.get("partial_probe") or (isinstance(data.get("wiki"), dict) and data["wiki"].get("partial_probe"))),
            }
        )
    categories = _load_category_rules(config)
    for row in rows:
        row["category"] = classify_case(row, categories)
    return sorted(rows, key=lambda item: (str(item.get("category")), str(item.get("case_id"))))


def _case_status(data: dict[str, Any], qa: dict[str, Any], result: dict[str, Any] | None) -> str:
    if isinstance(result, dict) and result.get("status"):
        return str(result["status"])
    if qa.get("review_required_before_run") or qa.get("questions"):
        return "NEEDS_INPUT"
    if qa.get("draft"):
        return "DRAFT"
    return "NOT_RUN"


def _source_issue(source: dict[str, Any], issue_by_id: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    try:
        issue_id = int(source.get("issue_id"))
    except (TypeError, ValueError):
        return None
    return issue_by_id.get(issue_id)


def _load_category_rules(config: ProjectConfig) -> dict[str, Any]:
    path = wiki_categories_path(config)
    if not path.exists():
        return _default_category_rules()
    try:
        if yaml is not None:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        else:
            loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_category_rules()
    return loaded if isinstance(loaded, dict) else _default_category_rules()


def _default_category_rules() -> dict[str, Any]:
    return {"categories": [], "fallback": "Uncategorized"}


def classify_case(case: dict[str, Any], rules: dict[str, Any]) -> str:
    wiki = case.get("wiki") if isinstance(case.get("wiki"), dict) else {}
    if wiki.get("category"):
        return str(wiki["category"])
    feature = str(case.get("feature") or "").strip()
    names = _category_names(rules)
    if feature and any(feature.lower() == name.lower() for name in names):
        return next(name for name in names if feature.lower() == name.lower())
    issue = case.get("source_issue") if isinstance(case.get("source_issue"), dict) else {}
    labels = issue.get("labels") if isinstance(issue.get("labels"), list) else []
    for label in labels:
        match = _match_category(str(label), rules)
        if match:
            return match
    text = " ".join(
        [
            str(case.get("title") or ""),
            str(case.get("feature") or ""),
            " ".join(str(cmd.get("run", "")) for cmd in case.get("commands", []) if isinstance(cmd, dict)),
        ]
    )
    match = _match_category(text, rules)
    if match:
        return match
    return str(rules.get("fallback") or "Uncategorized")


def _category_names(rules: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in rules.get("categories", []):
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def _match_category(text: str, rules: dict[str, Any]) -> str | None:
    lowered = text.lower()
    for item in rules.get("categories", []):
        if isinstance(item, str):
            if item.lower() in lowered:
                return item
            continue
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        keywords = item.get("keywords") if isinstance(item.get("keywords"), list) else [name]
        if any(str(keyword).lower() in lowered for keyword in keywords):
            return name
    return None


def _group_cases_by_category(cases: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    ordered_names = _category_names(rules)
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in ordered_names}
    for case in cases:
        grouped.setdefault(str(case.get("category") or "Uncategorized"), []).append(case)
    return {name: items for name, items in grouped.items() if items}


def _six_hat_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        hats = case.get("six_hats") if isinstance(case.get("six_hats"), dict) else {}
        if hats:
            rows.append({"case_id": case.get("case_id"), **hats})
    return rows


def _count_statuses(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"PASS": 0, "FAIL": 0, "BLOCK": 0, "DRAFT": 0, "NEEDS_INPUT": 0, "NOT_RUN": 0}
    for case in cases:
        status = str(case.get("status") or "NOT_RUN")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _gate_result_for_event(
    event: str,
    *,
    cases: list[dict[str, Any]],
    latest_run: dict[str, Any] | None,
    gitea_write_result: dict[str, Any] | None,
) -> dict[str, Any]:
    if event == "test_result":
        return _aggregate_latest_run(latest_run)
    if event == "gitea_write_summary":
        return {
            "status": "PASS" if _valid_gitea_write_result(gitea_write_result) else "BLOCK",
            "evidence": ["gitea-write-result"] if _valid_gitea_write_result(gitea_write_result) else [],
            "contract_hash": "wiki-gitea-write-summary",
        }
    return {
        "status": "BLOCK",
        "evidence": ["wiki-status-draft"],
        "contract_hash": f"wiki-{event}",
        "case_count": len(cases),
    }


def _aggregate_latest_run(latest_run: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(latest_run, dict):
        return {"status": "BLOCK", "evidence": [], "contract_hash": "wiki-test-result"}
    results = [item for item in latest_run.get("results", []) if isinstance(item, dict)]
    evidence: list[str] = []
    statuses = []
    for result in results:
        statuses.append(str(result.get("status") or "BLOCK"))
        if isinstance(result.get("evidence"), list):
            evidence.extend(str(item) for item in result["evidence"])
    status = "PASS"
    if any(item == "FAIL" for item in statuses):
        status = "FAIL"
    elif any(item in {"BLOCK", "ABORT"} for item in statuses):
        status = "BLOCK"
    return {"status": status, "evidence": evidence, "contract_hash": "wiki-test-result"}


def _latest_run_has_current_evidence(latest_run: dict[str, Any] | None) -> bool:
    if not isinstance(latest_run, dict):
        return False
    for result in latest_run.get("results", []):
        if isinstance(result, dict) and result.get("status") in {"PASS", "FAIL", "BLOCK"} and result.get("evidence"):
            return True
    return False


def _valid_gitea_write_result(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    return result.get("status") == "ok" or result.get("applied_count", 0) or result.get("response")


def _contains_raw_secret_text(text: str) -> bool:
    return bool(re.search(r"(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|password\s*=|api_token:)", text))


def _normalize_event(event: str) -> str:
    value = str(event or "manual")
    if value not in WIKI_EVENTS:
        raise WikiPublishError(f"unsupported wiki event: {value}")
    return value


def _event_label(event: str) -> str:
    return {
        "manual": "manual",
        "case_generation": "case generation",
        "test_result": "test result",
        "gitea_write_summary": "Gitea write summary",
    }[event]


def _wiki_commit_message(event: str) -> str:
    return {
        "manual": "QA-AIST wiki status update",
        "case_generation": "QA-AIST draft case status update",
        "test_result": "QA-AIST test result status update",
        "gitea_write_summary": "QA-AIST Gitea write summary update",
    }[event]


def _auto_wiki_enabled(config: ProjectConfig) -> bool:
    policy = config.data.get("policy") if isinstance(config.data.get("policy"), dict) else {}
    return policy.get("auto_publish_wiki", True) is not False


def _wiki_payload_from_plan(plan: dict[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "auto_sync": {"status": status, "event": plan.get("event"), "remote_apply": False},
        "plan_path": plan.get("plan_path"),
        "apply_result_path": None,
        "page": plan.get("page"),
        "blocked_by_gate": plan.get("blocked_by_gate"),
        "blocked_reasons": plan.get("blocked_reasons", []),
        "next_action": plan.get("next_action"),
    }


def _write_wiki_apply_result(config: ProjectConfig, payload: dict[str, Any]) -> Path:
    path = wiki_apply_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(payload) + "\n", encoding="utf-8")
    return path


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _md(value: Any) -> str:
    text = "-" if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
