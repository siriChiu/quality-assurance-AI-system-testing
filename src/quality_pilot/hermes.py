from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from . import __version__, cli
from .config import CONFIG_FILE, json_dumps

PRIMARY_PREFIX = "/quality-pilot"
ALIAS_PREFIX = "quality-pilot"
ACCEPTED_PREFIXES = {PRIMARY_PREFIX, ALIAS_PREFIX}
ROOT_COMMANDS = {
    "setup",
    "doctor",
    "issues",
    "cases",
    "publish",
    "close-loop",
    "report",
    "tracker",
}
AGENT_MANIFEST_NAME = "quality-pilot.agent.json"
AGENT_WRAPPER_NAME = "quality-pilot-agent.sh"
HERMES_SKILL_NAME = "quality-pilot"
HERMES_SKILL_FILE_NAME = "SKILL.md"


@dataclass(frozen=True)
class HermesCommand:
    prefix: str
    engine_argv: list[str]


def parse_chat_command(message: str, *, root: str | Path = ".") -> HermesCommand:
    parts = shlex.split(message)
    if not parts:
        raise ValueError("empty_hermes_message")
    prefix = parts[0]
    if prefix not in ACCEPTED_PREFIXES:
        raise ValueError("not_a_quality_pilot_command")
    if len(parts) == 1:
        raise ValueError("empty_quality_pilot_command")

    engine_argv = list(parts[1:])
    if not _has_option(engine_argv, "--json"):
        engine_argv.insert(0, "--json")
    _inject_project_context(engine_argv, Path(root).resolve())
    return HermesCommand(prefix=prefix, engine_argv=engine_argv)


def dispatch_chat_command(message: str, *, root: str | Path = ".") -> dict[str, Any]:
    try:
        command = parse_chat_command(message, root=root)
    except ValueError as exc:
        error = str(exc)
        payload = {
            "status": "error",
            "error": error,
            "message": _parse_error_message(error),
        }
        payload = _with_next_actions(payload, [], 2)
        return {
            "status": "error",
            "interface": "hermes",
            "command": message,
            "accepted_prefixes": sorted(ACCEPTED_PREFIXES),
            "engine_argv": [],
            "exit_code": 2,
            "payload": payload,
            "chat_response": render_chat_response(payload, exit_code=2),
        }

    help_payload = _help_payload(command.engine_argv)
    if help_payload:
        help_payload = _with_next_actions(help_payload, command.engine_argv, 0)
        return {
            "status": "ok",
            "interface": "hermes",
            "command": message,
            "prefix": command.prefix,
            "engine_argv": command.engine_argv,
            "exit_code": 0,
            "payload": help_payload,
            "chat_response": render_chat_response(help_payload, exit_code=0),
        }

    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(command.engine_argv)
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 2

    raw_stdout = stdout.getvalue().strip()
    raw_stderr = stderr.getvalue().strip()
    payload = _parse_engine_json(raw_stdout)
    if payload is None:
        payload = {
            "status": "error",
            "error": "engine_output_not_json",
            "message": raw_stderr or raw_stdout or "AI Quality Pilot engine did not emit JSON.",
            "stdout": raw_stdout,
            "stderr": raw_stderr,
        }
        if exit_code == 0:
            exit_code = 1
    elif raw_stderr:
        payload = {**payload, "stderr": raw_stderr}

    payload = _with_next_actions(payload, command.engine_argv, exit_code)
    return {
        "status": _dispatch_status(payload, exit_code),
        "interface": "hermes",
        "command": message,
        "prefix": command.prefix,
        "engine_argv": command.engine_argv,
        "exit_code": exit_code,
        "payload": payload,
        "chat_response": render_chat_response(payload, exit_code=exit_code),
    }


def render_chat_response(payload: dict[str, Any], *, exit_code: int = 0) -> str:
    if isinstance(payload.get("help_text"), str):
        menu = _next_actions_text(payload)
        return f"{payload['help_text']}\n\n{menu}" if menu else payload["help_text"]

    status = str(payload.get("status") or ("ok" if exit_code == 0 else "error"))
    lines = [f"quality-pilot> {status.upper()}"]

    if payload.get("error"):
        lines.append(f"         error: {payload.get('error')}")
    if payload.get("message"):
        lines.append(f"         message: {payload.get('message')}")
    if payload.get("path"):
        lines.append(f"         path: {payload.get('path')}")
    if payload.get("workspace"):
        lines.append(f"         workspace: {payload.get('workspace')}")
    if isinstance(payload.get("tracker_setup"), dict):
        tracker_setup = payload["tracker_setup"]
        lines.append(f"         tracker_setup: {tracker_setup.get('provider', '-')}/{tracker_setup.get('gitea_backend', '-')}")
        if tracker_setup.get("gitea_repo"):
            lines.append(f"         gitea_repo: {tracker_setup.get('gitea_repo')}")
        if tracker_setup.get("auto_configured_mcp"):
            lines.append("         auto_configured_mcp: true")
    if "case_contract_count" in payload:
        lines.append(f"         cases: {payload.get('case_contract_count')}")
    if "runner_count" in payload:
        lines.append(f"         runners: {payload.get('runner_count')}")
    if payload.get("latest_run_json"):
        lines.append(f"         latest_run_json: {payload.get('latest_run_json')}")
    if payload.get("report_path"):
        lines.append(f"         report: {payload.get('report_path')}")
    if isinstance(payload.get("wiki"), dict):
        wiki = payload["wiki"]
        auto = wiki.get("auto_sync") if isinstance(wiki.get("auto_sync"), dict) else {}
        if auto:
            lines.append(f"         wiki.auto_sync: {auto.get('status')}")
        if wiki.get("page"):
            lines.append(f"         wiki.page: {wiki.get('page')}")
        if wiki.get("plan_path"):
            lines.append(f"         wiki.plan: {wiki.get('plan_path')}")
        if wiki.get("apply_result_path"):
            lines.append(f"         wiki.apply: {wiki.get('apply_result_path')}")
        if wiki.get("blocked_by_gate") is not None:
            lines.append(f"         wiki.blocked_by_gate: {wiki.get('blocked_by_gate')}")
    elif payload.get("page") and ("wiki" in str(payload.get("schema", "")) or "wiki" in str(payload.get("event", "")) or payload.get("report_path") == ".quality-pilot-project/reports/wiki-status.md"):
        lines.append(f"         wiki.page: {payload.get('page')}")
    if isinstance(payload.get("tracker_writes"), dict):
        blocked = payload["tracker_writes"].get("blocked_by_gate")
        lines.append(f"         tracker_writes.blocked_by_gate: {blocked}")
    if isinstance(payload.get("write_gate_result"), dict):
        lines.append(f"         write_gate: {payload['write_gate_result'].get('reason')}")
    if "open_count" in payload:
        lines.append(f"         open_issues: {payload.get('open_count')}")
    if "generated" in payload and isinstance(payload.get("generated"), list):
        lines.append(f"         generated_cases: {len(payload.get('generated', []))}")
    if "growth_seed_count" in payload:
        lines.append(f"         growth_seeds: {payload.get('growth_seed_count')}")
    if "deduped_count" in payload:
        lines.append(f"         deduped_cases: {payload.get('deduped_count')}")
    if payload.get("init_context_path"):
        lines.append(f"         init_context: {payload.get('init_context_path')}")
    if "analyzed_files_count" in payload:
        lines.append(f"         analyzed_files: {payload.get('analyzed_files_count')}")
    if "missing_input_count" in payload:
        lines.append(f"         missing_inputs: {payload.get('missing_input_count')}")
    if "advisory_input_count" in payload:
        lines.append(f"         advisory_inputs: {payload.get('advisory_input_count')}")
    if payload.get("growth_context_path"):
        lines.append(f"         growth_context: {payload.get('growth_context_path')}")
    if payload.get("source"):
        lines.append(f"         source: {payload.get('source')}")
    if payload.get("resolved_profile"):
        lines.append(f"         profile: {payload.get('resolved_profile')}")
    if isinstance(payload.get("questions"), list) and payload.get("questions"):
        lines.append(f"         questions: {len(payload.get('questions', []))}")
    if "plan_path" in payload:
        lines.append(f"         plan: {payload.get('plan_path')}")
    if "mcp_write_request_path" in payload:
        lines.append(f"         mcp_request: {payload.get('mcp_write_request_path')}")
    if "mcp_write_result_path" in payload:
        lines.append(f"         mcp_result: {payload.get('mcp_write_result_path')}")
    if "blocked_by_gate" in payload:
        lines.append(f"         blocked_by_gate: {payload.get('blocked_by_gate')}")
    if payload.get("setup_required"):
        lines.append("         setup_required: true")
    if isinstance(payload.get("issue_sync"), dict):
        lines.extend(_issue_sync_lines(payload["issue_sync"]))
    if isinstance(payload.get("checks"), list):
        attention = _attention_check_lines(payload["checks"])
        if attention:
            lines.extend(attention)

    needs_input = payload.get("hermes_needs_input")
    if isinstance(needs_input, dict) and needs_input.get("status") == "required":
        lines.extend(["", *_needs_input_lines(needs_input)])

    first_result = _first_result(payload)
    if first_result:
        if first_result.get("case_id"):
            lines.append(f"         case: {first_result.get('case_id')}")
        if first_result.get("result_path"):
            lines.append(f"         result: {first_result.get('result_path')}")
        elif first_result.get("evidence"):
            lines.append(f"         evidence: {', '.join(map(str, first_result.get('evidence', [])[:3]))}")
    next_actions = payload.get("next_actions")
    if isinstance(next_actions, list) and next_actions:
        lines.extend(["", *_next_actions_lines(next_actions)])
    return "\n".join(lines)


def _issue_sync_lines(issue_sync: dict[str, Any]) -> list[str]:
    lines = [
        f"         issue_sync: {issue_sync.get('status', 'unknown')}",
        f"         tracker: {issue_sync.get('provider', '-')}/{issue_sync.get('backend', '-')}",
    ]
    blockers = issue_sync.get("blockers")
    if isinstance(blockers, list) and blockers:
        lines.append(f"         blockers: {', '.join(map(str, blockers[:5]))}")
    if issue_sync.get("mcp_issues_json"):
        exists = "exists" if issue_sync.get("mcp_snapshot_exists") else "missing"
        lines.append(f"         mcp_issues_json: {issue_sync.get('mcp_issues_json')} ({exists})")
    return lines


def _attention_check_lines(checks: list[Any]) -> list[str]:
    attention = [
        check for check in checks
        if isinstance(check, dict) and str(check.get("status")) in {"WARN", "FAIL"}
    ]
    if not attention:
        return []
    lines = ["", "需要處理："]
    for check in attention[:5]:
        name = check.get("name", "check")
        status = check.get("status", "WARN")
        message = check.get("message") or check.get("path") or check.get("value") or ""
        lines.append(f"- {status} {name}: {message}")
    return lines


def _next_actions_text(payload: dict[str, Any]) -> str:
    actions = payload.get("next_actions")
    if not isinstance(actions, list) or not actions:
        return ""
    return "\n".join(_next_actions_lines(actions))


def _next_actions_lines(next_actions: list[Any]) -> list[str]:
    lines = ["下一步可以選："]
    for index, action in enumerate([item for item in next_actions if isinstance(item, dict)][:4], start=1):
        label = action.get("label") or action.get("command") or "下一步"
        command = action.get("command")
        requires_confirmation = action.get("requires_confirmation")
        suffix = "（需確認）" if requires_confirmation else ""
        if command:
            lines.append(f"{index}. {label}：`{command}`{suffix}")
        else:
            lines.append(f"{index}. {label}{suffix}")
    lines.append("請回覆選項編號，或直接輸入下一個 `/quality-pilot ...` 指令。")
    return lines


def _with_next_actions(payload: dict[str, Any], engine_argv: list[str], exit_code: int) -> dict[str, Any]:
    if isinstance(payload.get("next_actions"), list):
        return _with_hermes_needs_input(payload)
    actions = suggest_next_actions(payload, engine_argv, exit_code)
    if not actions:
        return _with_hermes_needs_input(payload)
    return _with_hermes_needs_input({**payload, "next_actions": actions})


def _with_hermes_needs_input(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("hermes_needs_input"), dict):
        return payload
    questions = _collect_needs_input_questions(payload)
    if not questions:
        return payload
    needs_input = {
        "status": "required",
        "title": "AI Quality Pilot clarify",
        "language": "zh-Hant",
        "mode": "questionnaire",
        "preferred_mechanism": "clarify",
        "clarify": {
            "tool": "clarify",
            "mode": "one_question_at_a_time",
            "question_field": "prompt",
        },
        "questions": questions,
        "answer_format": "請用大分類一次回覆；可用題號回答，也可以直接補充 fixture、輸入檔、成功條件或不可碰範圍。",
        "ui_hint": "Call Hermes clarify for each question. Do not render a separate needs-input title.",
    }
    return {
        **payload,
        "input_required": True,
        "interaction": {
            "type": "needs_input",
            "title": "AI Quality Pilot clarify",
            "field": "payload.hermes_needs_input",
            "handler": "clarify",
        },
        "hermes_needs_input": needs_input,
    }


def _needs_input_lines(needs_input: dict[str, Any]) -> list[str]:
    lines = ["需要補充資訊："]
    questions = needs_input.get("questions")
    if isinstance(questions, list):
        for index, item in enumerate([q for q in questions if isinstance(q, dict)][:8], start=1):
            case_suffix = f" [{item.get('case_id')}]" if item.get("case_id") else ""
            prompt = item.get("prompt") or item.get("label") or "請補充需要的資訊。"
            lines.append(f"{index}.{case_suffix} {prompt}")
        if len(questions) > 8:
            lines.append(f"... 還有 {len(questions) - 8} 題，請先回答前面的必要資訊。")
    answer_format = needs_input.get("answer_format")
    if answer_format:
        lines.append(str(answer_format))
    return lines


def _collect_needs_input_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(prompt: Any, *, source: str, case_id: Any = None, label: Any = None) -> None:
        prompt_text = str(prompt or "").strip()
        if not prompt_text:
            return
        key = f"{source}:{case_id or ''}:{prompt_text}"
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {
            "id": _question_id(source, case_id, len(questions) + 1),
            "prompt": prompt_text,
            "source": source,
            "required": True,
        }
        if case_id:
            item["case_id"] = str(case_id)
        if label:
            item["label"] = str(label)
        questions.append(item)

    _collect_question_groups(payload.get("questions"), source="payload.questions", add=add)
    _collect_question_groups(payload.get("drafts"), source="draft.questions", add=add, nested_key="questions")

    missing_inputs = payload.get("missing_inputs")
    if isinstance(missing_inputs, list):
        for item in missing_inputs:
            add(_missing_input_prompt(item), source="payload.missing_inputs")

    if str(payload.get("status") or "").lower() == "needs_input" and not questions:
        add("請補齊 AI Quality Pilot 回報的必要測試資訊後再繼續。", source="payload.status")
    return questions


def _missing_input_prompt(item: Any) -> str:
    if isinstance(item, dict):
        prompt = item.get("prompt") or item.get("message") or item.get("id")
        return str(prompt or "").strip()
    text = str(item or "").strip()
    if not text:
        return ""
    if text.startswith(("請", "是否", "如果", "目前")):
        return text
    return f"請補齊測試需要的資訊：{text}"


def _collect_question_groups(
    value: Any,
    *,
    source: str,
    add: Any,
    nested_key: str | None = None,
) -> None:
    if isinstance(value, str):
        add(value, source=source)
        return
    if not isinstance(value, list):
        return
    for group in value:
        if isinstance(group, str):
            add(group, source=source)
            continue
        if not isinstance(group, dict):
            continue
        case_id = group.get("case_id") or group.get("id")
        candidates = group.get(nested_key) if nested_key else group.get("questions")
        if isinstance(candidates, list):
            for question in candidates:
                add(question, source=source, case_id=case_id)
        elif isinstance(candidates, str):
            add(candidates, source=source, case_id=case_id)


def _question_id(source: str, case_id: Any, index: int) -> str:
    raw = f"{source}-{case_id or 'general'}-{index}"
    chars = []
    for char in raw.lower():
        chars.append(char if char.isalnum() else "_")
    return "_".join(part for part in "".join(chars).split("_") if part)[:80] or f"question_{index}"


def suggest_next_actions(payload: dict[str, Any], engine_argv: list[str], exit_code: int = 0) -> list[dict[str, Any]]:
    args = _positional_args(engine_argv)
    current = " ".join(args[:2]) if len(args) >= 2 else (args[0] if args else "")
    current3 = " ".join(args[:3]) if len(args) >= 3 else current
    status = str(payload.get("status") or "").lower()
    error = str(payload.get("error") or "")
    message = str(payload.get("message") or "")
    issue_sync = payload.get("issue_sync") if isinstance(payload.get("issue_sync"), dict) else {}
    blockers = set(issue_sync.get("blockers", [])) if isinstance(issue_sync.get("blockers"), list) else set()

    if error == "command_removed":
        replacement = payload.get("replacement")
        return [_next("改用新的正式指令", str(replacement), confirm=True)] if replacement else [_next("查看正式指令", "/quality-pilot help")]
    if error == "config_not_found":
        return [
            _next("初始化目前 repo", "/quality-pilot setup", confirm=True),
            _next("執行健康檢查", "/quality-pilot doctor"),
        ]
    if "gitea_mcp_snapshot_missing" in message:
        return [
            _next("用 Hermes Gitea MCP 讀取 issues，寫入 snapshot 後重跑 sync", "/quality-pilot issues sync", confirm=True),
            _next("查看 issue sync 狀態", "/quality-pilot issues status"),
            _next("執行健康檢查", "/quality-pilot doctor"),
        ]
    if "redmine_mcp_snapshot_missing" in message:
        return [
            _redmine_snapshot_retry_action(current, engine_argv),
            _next("執行健康檢查", "/quality-pilot doctor"),
        ]
    if error in {"GiteaError", "IssueSyncError"}:
        return [
            _next("執行健康檢查", "/quality-pilot doctor"),
            _next("查看 issue 狀態", "/quality-pilot issues status"),
        ]
    if error in {"QAConfigError", "config_invalid"} or status == "error" and "config" in error.lower():
        return [_next("執行健康檢查", "/quality-pilot doctor"), _next("查看正式指令", "/quality-pilot help")]

    if not args or args[0] == "help":
        return [
            _next("初始化產品 repo", "/quality-pilot setup", confirm=True),
            _next("執行健康檢查", "/quality-pilot doctor"),
            _next("同步 Gitea issues", "/quality-pilot issues sync", confirm=True),
            _next("首次建立 SWQA cases", "/quality-pilot cases generate --init", confirm=True),
        ]
    if current == "setup":
        if "gitea_mcp_snapshot_missing" in blockers:
            return [
                _next("用 Hermes Gitea MCP 讀取 issues，寫入 snapshot 後重跑 sync", "/quality-pilot issues sync", confirm=True),
                _next("執行健康檢查", "/quality-pilot doctor"),
            ]
        return [_next("執行健康檢查", "/quality-pilot doctor"), _next("同步 Gitea issues", "/quality-pilot issues sync", confirm=True)]
    if current == "doctor":
        if "gitea_mcp_snapshot_missing" in blockers:
            return [
                _next("用 Hermes Gitea MCP 讀取 issues，寫入 snapshot 後重跑 sync", "/quality-pilot issues sync", confirm=True),
                _next("查看 issue sync 狀態", "/quality-pilot issues status"),
            ]
        if "hermes_gitea_mcp_unknown_or_missing" in blockers or "hermes_gitea_mcp_missing" in blockers or "hermes_gitea_mcp_unknown" in blockers:
            return [{"label": "在 Hermes 啟用 Gitea MCP，或提供 QUALITY_PILOT_HERMES_MCP_SERVERS/status JSON 後重跑 doctor", "kind": "ask_user"}]
        if "hermes_redmine_mcp_missing" in blockers or "hermes_redmine_mcp_unknown" in blockers:
            return [{"label": "在 Hermes 啟用 Redmine MCP，或提供 QUALITY_PILOT_HERMES_MCP_SERVERS/status JSON 後重跑 doctor", "kind": "ask_user"}]
        if "tracker_provider_disabled" in blockers:
            return [{"label": "執行 /quality-pilot setup 產生 tracker.provider: hermes_mcp 後重跑 doctor", "kind": "ask_user"}]
        if status in {"warn", "error", "fail"}:
            return [_next("查看 issue sync 狀態", "/quality-pilot issues status"), _next("查看 Wiki 狀態", "/quality-pilot publish wiki status")]
        return [
            _next("同步 Gitea issues", "/quality-pilot issues sync", confirm=True),
            _next("首次建立 SWQA cases", "/quality-pilot cases generate --init", confirm=True),
            _next("列出測試 cases", "/quality-pilot cases list"),
        ]
    if current == "issues sync":
        if exit_code == 0 and (payload.get("source") == "redmine_mcp" or payload.get("mode") == "redmine_issues"):
            issue_ids = " ".join(str(item) for item in payload.get("imported_issue_ids", []) or payload.get("requested_issue_ids", []))
            if status == "needs_mcp_apply":
                return [
                    _next("用 Hermes Gitea MCP 建立這批 Gitea issues", "/quality-pilot issues sync --redmine-issues " + issue_ids, confirm=True, destructive=True),
                    _next("建立完成後產生 linked testcases", f"/quality-pilot cases generate --redmine-issues {issue_ids}".strip(), confirm=True),
                    _next("查看 issue sync 狀態", "/quality-pilot issues status"),
                ]
            if status in {"ok", "dry_run", "no_remote_write_needed"}:
                command = f"/quality-pilot cases generate --redmine-issues {issue_ids}".strip()
                return [
                    _next("針對這批 Redmine tickets 產生 linked testcases", command, confirm=True),
                    _next("查看 issue sync 狀態", "/quality-pilot issues status"),
                    _next("查看 Gitea issue 建立狀態", "/quality-pilot issues status"),
                ]
        if exit_code == 0 and status in {"ok", "dry_run"}:
            return [
                _next("用最新狀態長出測試 cases", "/quality-pilot cases generate --growing", confirm=True),
                _next("查看 issue sync 狀態", "/quality-pilot issues status"),
                _next("修復全部 open issues", "/quality-pilot issues fix --all", confirm=True),
            ]
        return [_next("執行健康檢查", "/quality-pilot doctor"), _next("查看 issue sync 狀態", "/quality-pilot issues status")]
    if current == "issues status":
        if not payload.get("snapshot_exists"):
            return [_next("同步 Gitea issues", "/quality-pilot issues sync", confirm=True), _next("執行健康檢查", "/quality-pilot doctor")]
        return [
            _next("長出測試 cases", "/quality-pilot cases generate --growing", confirm=True),
            _next("修復指定 issue", "/quality-pilot issues fix --issue <id>", confirm=True),
        ]
    if current == "issues fix":
        return [
            _next("執行 linked cases", "/quality-pilot cases run", confirm=True),
            _next("推產品修復 PR", "/quality-pilot issues fix --issue <id> --push-pr", confirm=True, destructive=True),
            _next("查看 issue/fix 狀態", "/quality-pilot issues status"),
        ]
    if current == "cases generate":
        if error == "explicit_generation_mode_required":
            return [
                _next("首次全 repo SWQA 建案", "/quality-pilot cases generate --init", confirm=True),
                _next("依最新狀態擴散 cases", "/quality-pilot cases generate --growing", confirm=True),
                _next("從 Redmine issues 產生 cases", "/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]", confirm=True),
            ]
        if status == "needs_input":
            return [
                _next("查看 Wiki draft 狀態", "/quality-pilot publish wiki status"),
                _next("審查待補資訊", "/quality-pilot cases review"),
                {"label": "一次補齊大分類 fixture、輸入檔、成功條件與不可碰範圍", "kind": "ask_user"},
                _next("補完後驗證 cases", "/quality-pilot cases validate"),
            ]
        return [
            _next("查看 Wiki draft 狀態", "/quality-pilot publish wiki status"),
            _next("驗證 cases", "/quality-pilot cases validate"),
            _next("執行所有 safe probes", "/quality-pilot cases run", confirm=True),
            _next("列出可跑測試", "/quality-pilot cases list"),
        ]
    if current in {"cases review", "cases validate"}:
        return [
            _next("列出可跑測試", "/quality-pilot cases list"),
            _next("執行 cases", "/quality-pilot cases run", confirm=True),
        ]
    if current == "cases list":
        first_case = _first_case_id(payload)
        actions = [_next("執行所有 cases", "/quality-pilot cases run", confirm=True)]
        if first_case:
            actions.append(_next(f"執行單一 case {first_case}", f"/quality-pilot cases run {first_case}", confirm=True))
        actions.append(_next("驗證 case YAML", "/quality-pilot cases validate"))
        return actions
    if current == "cases run":
        return [
            _next("查看 Wiki 自動同步狀態", "/quality-pilot publish wiki status"),
            _next("產生報告", "/quality-pilot report status"),
            _next("手動重建 Wiki plan", "/quality-pilot publish wiki plan", confirm=True),
            _next("推產品修復 PR", "/quality-pilot cases push-pr <case_id>", confirm=True, destructive=True),
        ]
    if current == "cases push-pr":
        return [_next("查看 issue/fix 狀態", "/quality-pilot issues status"), _next("查看 Wiki 狀態", "/quality-pilot publish wiki status")]
    if current == "close-loop run-once":
        return [
            _next("查看 Wiki 自動同步狀態", "/quality-pilot publish wiki status"),
            _next("產生報告", "/quality-pilot report status"),
            _next("手動重建 Wiki plan", "/quality-pilot publish wiki plan", confirm=True),
        ]
    if current == "report status":
        return [
            _next("更新 Wiki plan", "/quality-pilot publish wiki plan", confirm=True),
            _next("查看 latest run JSON", "/quality-pilot report json"),
        ]
    if current3 == "publish wiki plan":
        if payload.get("status") == "ready":
            return [
                _next("套用 Wiki 更新", "/quality-pilot publish wiki apply", confirm=True, destructive=True),
                _next("查看 Wiki 狀態", "/quality-pilot publish wiki status"),
            ]
        return [
            _next("查看 Wiki 狀態", "/quality-pilot publish wiki status"),
            _next("執行 doctor 檢查 token/backend/gate", "/quality-pilot doctor"),
        ]
    if current3 == "publish wiki apply":
        if status == "needs_mcp_apply":
            return [
                {
                    "label": "Hermes 依 gated request 呼叫 Gitea MCP 更新 Wiki，並在同一 apply 流程回填結果",
                    "kind": "mcp_write",
                    "requires_confirmation": True,
                    "request_path": payload.get("mcp_write_request_path"),
                    "result_path": payload.get("mcp_write_result_path"),
                    "command": "/quality-pilot publish wiki apply",
                },
                _next("查看 Wiki 狀態", "/quality-pilot publish wiki status"),
            ]
        if status == "blocked":
            return [
                _next("查看 Wiki 狀態", "/quality-pilot publish wiki status"),
                _next("重新產生 Wiki plan", "/quality-pilot publish wiki plan", confirm=True),
            ]
        return [_next("查看 Wiki 狀態", "/quality-pilot publish wiki status")]
    if current3 == "publish wiki status":
        return [
            _next("重建 Wiki plan", "/quality-pilot publish wiki plan", confirm=True),
        ]
    return []


def _next(label: str, command: str, *, confirm: bool = False, destructive: bool = False) -> dict[str, Any]:
    return {
        "label": label,
        "command": command,
        "requires_confirmation": confirm or destructive,
        "destructive": destructive,
    }


def _redmine_snapshot_retry_action(current: str, engine_argv: list[str]) -> dict[str, Any]:
    ids = _redmine_issue_ids_from_argv(engine_argv)
    id_text = " ".join(ids) if ids else "<redmine_issue_id> [<redmine_issue_id> ...]"
    if current == "issues sync":
        return _next(
            "用 Hermes Redmine MCP 讀取指定 issues，寫入 snapshot 後重跑 sync",
            f"/quality-pilot issues sync --redmine-issues {id_text}",
            confirm=True,
        )
    return _next(
        "用 Hermes Redmine MCP 讀取指定 issues，寫入 snapshot 後重跑 generate",
        f"/quality-pilot cases generate --redmine-issues {id_text}",
        confirm=True,
    )


def _redmine_issue_ids_from_argv(engine_argv: list[str]) -> list[str]:
    ids: list[str] = []
    capture = False
    for item in engine_argv:
        if item == "--redmine-issues":
            capture = True
            continue
        if item.startswith("--redmine-issues="):
            raw = item.split("=", 1)[1]
            ids.extend([part for part in raw.replace(",", " ").split() if part.isdigit()])
            capture = False
            continue
        if capture:
            if item.startswith("-"):
                break
            if item.isdigit():
                ids.append(item)
    return ids


def _first_case_id(payload: dict[str, Any]) -> str | None:
    cases = payload.get("cases")
    if isinstance(cases, list):
        for item in cases:
            if isinstance(item, dict) and item.get("case_id"):
                return str(item["case_id"])
    results = payload.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("case_id"):
                return str(item["case_id"])
    return None


def build_agent_manifest(*, wrapper_path: str | None = None, runner_command: str = "quality-pilot-hermes") -> dict[str, Any]:
    entrypoint_command = [wrapper_path] if wrapper_path else [runner_command, "--root", "${HERMES_PROJECT_ROOT}", "${HERMES_MESSAGE}"]
    return {
        "schema": "hermes.agent.v1",
        "name": "quality-pilot",
        "display_name": "AI Quality Pilot",
        "version": __version__,
        "description": "Hermes-first deterministic QA lifecycle agent/plugin for Gitea issue sync, tests, publishing, and PR flow.",
        "command_prefix": PRIMARY_PREFIX,
        "aliases": [ALIAS_PREFIX],
        "entrypoint": {
            "type": "process",
            "command": entrypoint_command,
            "root_env": "HERMES_PROJECT_ROOT",
            "message_env": "HERMES_MESSAGE",
            "message_args": "append",
        },
        "python_api": {
            "module": "quality_pilot.hermes",
            "callable": "dispatch_chat_command",
            "signature": "dispatch_chat_command(message: str, root: str | Path = '.') -> dict",
        },
        "engine": {
            "console_script": "quality-pilot",
            "hermes_console_script": runner_command,
            "json_output": True,
        },
        "commands": [
            f"{PRIMARY_PREFIX} help",
            f"{PRIMARY_PREFIX} setup",
            f"{PRIMARY_PREFIX} doctor",
            f"{PRIMARY_PREFIX} issues sync",
            f"{PRIMARY_PREFIX} issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]",
            f"{PRIMARY_PREFIX} issues status",
            f"{PRIMARY_PREFIX} issues show <issue_id>",
            f"{PRIMARY_PREFIX} issues fix --all",
            f"{PRIMARY_PREFIX} issues fix --issue <id>",
            f"{PRIMARY_PREFIX} issues fix --issue <id> --push-pr",
            f"{PRIMARY_PREFIX} cases generate --init",
            f"{PRIMARY_PREFIX} cases generate --init --count 5",
            f"{PRIMARY_PREFIX} cases generate --growing",
            f"{PRIMARY_PREFIX} cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]",
            f"{PRIMARY_PREFIX} cases review",
            f"{PRIMARY_PREFIX} cases validate",
            f"{PRIMARY_PREFIX} cases list",
            f"{PRIMARY_PREFIX} cases run",
            f"{PRIMARY_PREFIX} cases run <case_id>",
            f"{PRIMARY_PREFIX} cases push-pr",
            f"{PRIMARY_PREFIX} cases push-pr <case_id>",
            f"{PRIMARY_PREFIX} publish wiki status",
            f"{PRIMARY_PREFIX} publish wiki plan",
            f"{PRIMARY_PREFIX} publish wiki apply",
            f"{PRIMARY_PREFIX} close-loop status",
            f"{PRIMARY_PREFIX} close-loop run-once",
            f"{PRIMARY_PREFIX} report status",
            f"{PRIMARY_PREFIX} report json",
            f"{PRIMARY_PREFIX} tracker plan-write",
        ],
        "permissions": {
            "filesystem": ["project_root"],
            "network": [
                "gitea_http_when_apply_or_submit_pr",
                "gitea_mcp_read_and_gated_wiki_write_when_configured",
                "gitea_mcp_gated_issue_create_from_redmine_sync",
            ],
            "tracker_write": "write_gate_apply_only",
        },
        "security": {
            "deterministic_write_gate_required": True,
            "raw_secret_output_forbidden": True,
            "closed_issue_write_forbidden": True,
            "llm_may_not_reorder_pipeline": True,
        },
        "outputs": {
            "format": "json",
            "chat_response_field": "chat_response",
            "payload_field": "payload",
            "next_actions_field": "payload.next_actions",
            "needs_input_field": "payload.hermes_needs_input",
            "input_required_field": "payload.input_required",
            "interaction_field": "payload.interaction",
            "interaction_style": "guided_menu_with_needs_input",
        },
    }


def install_agent(agent_dir: str | Path, *, force: bool = False, runner_command: str = "quality-pilot-hermes") -> dict[str, Any]:
    target = Path(agent_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    wrapper_path = target / AGENT_WRAPPER_NAME
    manifest_path = target / AGENT_MANIFEST_NAME
    if not force and (wrapper_path.exists() or manifest_path.exists()):
        return {
            "status": "error",
            "error": "agent_files_exist",
            "message": "Hermes agent files already exist. Re-run with --force to overwrite.",
            "agent_dir": str(target),
            "manifest_path": str(manifest_path),
            "wrapper_path": str(wrapper_path),
        }

    wrapper_path.write_text(_wrapper_script(runner_command), encoding="utf-8")
    wrapper_path.chmod(wrapper_path.stat().st_mode | 0o111)
    manifest = build_agent_manifest(wrapper_path=str(wrapper_path), runner_command=runner_command)
    manifest_path.write_text(json_dumps(manifest) + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "agent_dir": str(target),
        "manifest_path": str(manifest_path),
        "wrapper_path": str(wrapper_path),
        "command_prefix": PRIMARY_PREFIX,
        "aliases": [ALIAS_PREFIX],
    }


def agent_status(agent_dir: str | Path) -> dict[str, Any]:
    target = Path(agent_dir).expanduser().resolve()
    manifest_path = target / AGENT_MANIFEST_NAME
    wrapper_path = target / AGENT_WRAPPER_NAME
    manifest: dict[str, Any] | None = None
    manifest_valid = False
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
                manifest_valid = loaded.get("command_prefix") == PRIMARY_PREFIX
        except json.JSONDecodeError:
            manifest_valid = False
    return {
        "status": "ok" if manifest_valid and wrapper_path.exists() else "missing",
        "agent_dir": str(target),
        "manifest_path": str(manifest_path),
        "manifest_exists": manifest_path.exists(),
        "manifest_valid": manifest_valid,
        "wrapper_path": str(wrapper_path),
        "wrapper_exists": wrapper_path.exists(),
        "command_prefix": manifest.get("command_prefix") if manifest else None,
    }


def default_skills_dir() -> Path:
    hermes_home = os.getenv("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home).expanduser() / "skills"
    return Path.home() / ".hermes" / "skills"


def build_skill_markdown(*, runner_command: str = "quality-pilot-hermes") -> str:
    return f"""---
name: quality-pilot
description: "AI Quality Pilot dynamic skill: call the deterministic QA lifecycle engine for issues, cases, Wiki status, close-loop health, reports, and gated PR flow."
version: {__version__}
author: AI Quality Pilot contributors
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [qa, testing, deterministic, evidence, write-gate, tracker, dynamic-skill]
---

# AI Quality Pilot Hermes Skill

This SKILL.md is the current Hermes integration for AI Quality Pilot.

It makes `/quality-pilot ...` visible to Hermes as a dynamic skill slash command, then instructs the Hermes agent to call the deterministic AI Quality Pilot dispatcher. This is skill-mediated. It is not a native Hermes router, not a pre-LLM command hook, and not a Python package autoload mechanism.

AI Quality Pilot is responsible for issue sync, case contracts, test execution, evidence, write gate, automatic Gitea Wiki status sync, gated issue publishing, and Gitea PR creation. Hermes may answer questions and make code changes, but it must not bypass AI Quality Pilot for tracker/wiki/PR decisions.

## Public Command Surface

Only these `/quality-pilot` commands are public:

- `/quality-pilot help`
- `/quality-pilot setup`
- `/quality-pilot doctor`
- `/quality-pilot issues sync`
- `/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]`
- `/quality-pilot issues status`
- `/quality-pilot issues show <issue_id>`
- `/quality-pilot issues fix --all`
- `/quality-pilot issues fix --issue <id>`
- `/quality-pilot issues fix --issue <id> --push-pr`
- `/quality-pilot cases generate --init`
- `/quality-pilot cases generate --init --count 5`
- `/quality-pilot cases generate --growing`
- `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]`
- `/quality-pilot cases review`
- `/quality-pilot cases validate`
- `/quality-pilot cases list`
- `/quality-pilot cases run`
- `/quality-pilot cases run <case_id>`
- `/quality-pilot cases push-pr`
- `/quality-pilot cases push-pr <case_id>`
- `/quality-pilot publish wiki status`
- `/quality-pilot publish wiki plan`
- `/quality-pilot publish wiki apply`
- `/quality-pilot close-loop status`
- `/quality-pilot close-loop run-once`
- `/quality-pilot report status`
- `/quality-pilot report json`
- `/quality-pilot tracker plan-write`

Removed commands must not be run. If the user asks for `qa-test`, `fix-issues`, `issues dedupe`, `config`, legacy `publish plan/apply/status`, `sync-gitea`, `find-new-issues`, or `help <topic>`, run the dispatcher and report its `command_removed` replacement. Do not silently translate and execute old commands.

## Required Behavior For Every `/quality-pilot` Turn

When the user invokes `/quality-pilot <arguments>`, you must:

1. Treat everything after `/quality-pilot` as AI Quality Pilot dispatcher arguments.
2. Use the current product repository root as `--root`. Do not use the AI Quality Pilot source checkout as root unless the user is working on AI Quality Pilot itself.
3. Execute the dispatcher through the terminal. Do not answer from memory.
4. Read the returned JSON.
5. Reply primarily with the JSON `chat_response` field.
6. If `payload.hermes_needs_input.status == "required"` or `payload.input_required == true`, call Hermes `clarify` for the category-level blocking inputs in `payload.hermes_needs_input.questions[]`; do not downgrade it to a normal next-action menu.
7. If `chat_response` is missing, summarize `status`, `payload.status`, `payload.error`, `payload.message`, `latest_run_json`, `report_path`, and evidence paths.
8. Preserve failures. If the dispatcher exits non-zero or emits invalid JSON, tell the user the exit code and useful stderr/stdout details.

Hermes MCP rule: AI Quality Pilot does not store Gitea/Redmine URLs, repo names, or token environment variables in `.quality-pilot.yaml`. It relies on the user's Hermes session to provide MCP servers. At the start of setup/doctor, make the available server list visible to AI Quality Pilot through `QUALITY_PILOT_HERMES_MCP_SERVERS=gitea,redmine` or the configured `.quality-pilot-project/state/hermes-mcp/status.json`. If Gitea or Redmine MCP is missing or unknown, tell the user immediately and do not pretend remote sync/write is ready.

Gitea MCP rule: if the product repo config uses `tracker.provider: hermes_mcp`, you may use Hermes' configured Gitea MCP tooling for three narrow operations only: read issue data before `/quality-pilot issues sync`, create Gitea issues after `/quality-pilot issues sync --redmine-issues ...` returns `status: needs_mcp_apply` with a gated `mcp_issue_write_request`, and update the configured Wiki page after `/quality-pilot publish wiki apply` returns `status: needs_mcp_apply` with a gated `mcp_write_request`. Do not treat the MCP read itself as a completed sync.

Gitea MCP snapshot workflow (when the user confirms, chooses a suggested sync option, or invokes `/quality-pilot issues sync` after `gitea_mcp_snapshot_missing`):
1. Use Gitea MCP read-only pagination for the current Hermes product repository context, typically `state=all` and `perPage=50`, until an empty page is returned.
2. Preserve the MCP payload shape as JSON and write it to `tracker.mcp.gitea_issues_json`, creating parent directories if needed.
3. If the MCP list response includes pull requests mixed with issues, keep only real Gitea issues before writing the AI Quality Pilot `issues` list. A reliable guard is `html_url` containing `/issues/` and excluding `/pulls/`.
4. Immediately run `/quality-pilot issues sync` via the dispatcher command.
5. In this snapshot workflow, never use Gitea MCP for issue comments, issue creation, PRs, or arbitrary remote writes.

Gitea MCP Wiki write workflow (only after `/quality-pilot publish wiki apply` returns `status: needs_mcp_apply`):
1. Read `payload.mcp_write_request`.
2. Confirm the request schema is `quality-pilot.gitea-mcp-wiki-write-request.v1`, operation is `gitea.wiki.update_page`, and `safety.allowed_targets` is only `wiki`.
3. Call the configured Hermes Gitea MCP wiki update/write-page tool for the exact `repo`, `page`, `body`, and `message` in that request only.
4. Write the MCP tool result JSON to `payload.mcp_write_result_path`.
5. Treat this as the same `/quality-pilot publish wiki apply` user flow. Report the MCP result and suggest `/quality-pilot publish wiki status`; do not expose a second completion command to the user.

Gitea MCP Redmine issue creation workflow (only after `/quality-pilot issues sync --redmine-issues ...` returns `status: needs_mcp_apply`):
1. Read `payload.mcp_issue_write_request`.
2. Confirm the request schema is `quality-pilot.gitea-mcp-issue-write-request.v1`, operation is `gitea.issue.sync_from_redmine`, and `safety.allowed_targets` is only `issues`.
3. For each action, confirm `operation` is exactly `gitea.issue.create` and `write_gate_result.allowed` is true.
4. Call the configured Hermes Gitea MCP issue-create tool for each action's `title`, `body`, and `labels` in the current product repo context.
5. Write the combined MCP tool results JSON to `payload.mcp_issue_write_result_path`.
6. Treat this as the same `/quality-pilot issues sync --redmine-issues ...` user flow. Report created issue IDs/URLs, then suggest `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]`.
7. Do not create comments, close issues, reopen issues, edit arbitrary issues, or create PRs in this workflow.

Redmine MCP rule: AI Quality Pilot V1 reads Redmine only through a Hermes Redmine MCP snapshot. When `/quality-pilot doctor` reports missing Redmine MCP readiness, use Hermes Redmine MCP to read the requested IDs and write the configured snapshot path. Then run `/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` to create local Redmine mirrors and gated Gitea issue-create requests; if it returns `needs_mcp_apply`, execute the Gitea MCP Redmine issue creation workflow immediately. Run `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` only when testcase contracts are needed.

Reference: see `references/gitea-mcp-snapshot.md` for the MCP snapshot pitfall and recommended JSON shape.

Setup rule: `/quality-pilot setup` always writes `tracker.provider: hermes_mcp` plus `tracker.mcp.*` local handoff paths. It may report the detected git remote in JSON for human context, but it must not write Gitea repo URL, repo name, token env, or HTTP credentials into `.quality-pilot.yaml`.

## Interactive Guidance Model

Do not behave like a passive command relay. After every AI Quality Pilot turn, guide the user toward the next useful step in Traditional Chinese.

Hermes clarify / needs-input contract:

- `payload.input_required: true` means AI Quality Pilot needs a user answer before the workflow should continue.
- `payload.interaction.type: "needs_input"` points Hermes to the interaction type.
- `payload.interaction.handler: "clarify"` says the Hermes agent should call `clarify`, not invent a custom prompt flow.
- `payload.hermes_needs_input.preferred_mechanism` is `clarify`.
- `payload.hermes_needs_input.questions[]` is the canonical question list. It should contain category-level blocking inputs, not one question per generated test case.
- Call `clarify` only for these category-level questions, in order. Since `clarify` handles one prompt at a time, start with the first unanswered required question.
- If `clarify` is unavailable in the current Hermes runtime, render the same questions in chat under a short Traditional Chinese heading, then wait for the user's answer.
- Do not run cases, publish, create issues, or submit PRs from a draft that still has unanswered needs-input questions.

Use this pattern:

1. Briefly explain what just happened.
2. If the JSON payload contains `next_actions`, present them as a small numbered menu.
3. Ask the user to choose a number, approve the recommended action, or type another `/quality-pilot ...` command.
4. If the next action is safe and read-only, you may offer to run it immediately.
5. If the next action writes files, runs tests, uses Gitea MCP, publishes to Gitea, pushes a branch, or creates a PR, ask for confirmation first.
6. If the command returns `payload.hermes_needs_input`, call `clarify` for the listed category-level questions in Traditional Chinese and wait for the user's answer.

Preferred menu style:

```text
下一步可以選：
1. 執行健康檢查：/quality-pilot doctor
2. 同步 Gitea issues：/quality-pilot issues sync（需確認）
3. 首次建立 SWQA cases：/quality-pilot cases generate --init（需確認）

請回覆 1、2、3，或直接輸入下一個 /quality-pilot ... 指令。
```

Recommended interaction by situation:

- After `/quality-pilot setup`: suggest `/quality-pilot doctor`, then `/quality-pilot issues sync`.
- If setup reports `auto_configured_mcp: true`, explain that AI Quality Pilot is configured for Hermes MCP and the remaining step is `doctor` confirming Hermes Gitea/Redmine MCP availability.
- After `/quality-pilot doctor` warning: explain the warning and suggest the smallest check that resolves it.
- After `gitea_mcp_snapshot_missing`: offer to use Hermes Gitea MCP read-only fetch, write the snapshot, then rerun `/quality-pilot issues sync`.
- After `/quality-pilot issues sync`: explain that sync includes dedupe/prune, then suggest `/quality-pilot issues status` and `/quality-pilot cases generate --growing`.
- If the user asks for first-time test ideas or has no cases yet, run `/quality-pilot cases generate --init`; it acts as an opinionated SWQA engineer, scans README, code, package metadata, existing runners, existing cases, and project rules, then creates executable safe-probe cases across functional, positive, negative, boundary, invalid-input, side-effect-safe, and stress/timeout-risk coverage. Every INIT case must have `commands[].run`; it must not ask case-by-case confirmation questions.
- `/quality-pilot cases generate --init` is already fast/high-standard autonomous mode.
- If the user wants a smaller first batch, run `/quality-pilot cases generate --init --count 5`.
- If the user asks for follow-up ideas after issues/PRs/runs changed, run `/quality-pilot cases generate --growing`; it creates incremental executable growth cases from repo, issues, PR references, latest run, reports, existing cases, and runners.
- If the user names Redmine issue IDs and asks to sync or record issues, run `/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]`; Hermes supplies the MCP snapshot, AI Quality Pilot validates it and writes local Redmine mirrors plus gated Gitea issue candidates.
- If the user names one or more Redmine issue IDs and asks for testcases, run `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]`; Hermes supplies the MCP snapshot, AI Quality Pilot directly uses those IDs and writes linked executable case contracts. Do not create a Gitea issue plan in this command.
- If the user types bare `/quality-pilot cases generate`, run the dispatcher and present its mode-selection error; do not silently choose a mode.
- After `cases generate --init` or `cases generate --growing`, assume the generated cases are runnable safe probes unless AI Quality Pilot explicitly returns `payload.hermes_needs_input`. If needs-input exists, call `clarify` only for category-level blockers. Do not discuss each generated case one by one unless the user explicitly asks.
- After `/quality-pilot cases list`: suggest running one selected case first, then all cases.
- After `cases generate --init` or `cases generate --growing`: AI Quality Pilot auto-plans the Wiki draft/missing-input status. Suggest `/quality-pilot publish wiki status`.
- After a test run: AI Quality Pilot auto-plans or applies the Wiki test-result status. Suggest `/quality-pilot publish wiki status` and `/quality-pilot report status`.
- If the user explicitly wants to update only the Wiki, use `/quality-pilot publish wiki plan`, then `/quality-pilot publish wiki apply` after confirmation. AI Quality Pilot returns a gated `mcp_write_request`; Hermes Gitea MCP performs the Wiki update in the same user flow. This path must never create issue comments, new issues, or PRs.
- If `/quality-pilot publish wiki apply` returns `status: needs_mcp_apply`, read `payload.mcp_write_request`. Call the configured Hermes Gitea MCP wiki update/write-page tool for the request's `page`, `body`, and `message` in the current product repo context; if `repo` is present, enforce it exactly. Write the MCP tool result JSON to `payload.mcp_write_result_path`, then summarize the result and suggest `/quality-pilot publish wiki status`.
- If `/quality-pilot issues sync --redmine-issues ...` returns `status: needs_mcp_apply`, read `payload.mcp_issue_write_request`. Call the configured Hermes Gitea MCP issue-create tool for each gated action, write the result JSON to `payload.mcp_issue_write_result_path`, then summarize created issue IDs/URLs and suggest `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]`.
- Before `/quality-pilot issues fix --issue <id> --push-pr` or `/quality-pilot cases push-pr <case_id>`: ask for explicit confirmation and summarize what will be written remotely.

Use this command shape:

```bash
{runner_command} --root "$PWD" /quality-pilot <arguments>
```

`$PWD` must be the user's product repository root. If the active root is unclear, inspect the current workspace/cwd. If it is still unclear, ask the user for the product repo path instead of creating `.quality-pilot-project` in the wrong directory.

Examples:

```bash
{runner_command} --root "$PWD" /quality-pilot help
{runner_command} --root "$PWD" /quality-pilot setup
{runner_command} --root "$PWD" /quality-pilot doctor
{runner_command} --root "$PWD" /quality-pilot issues sync
{runner_command} --root "$PWD" /quality-pilot issues sync --redmine-issues 144780 144693
{runner_command} --root "$PWD" /quality-pilot issues status
{runner_command} --root "$PWD" /quality-pilot issues fix --issue 123 --push-pr
{runner_command} --root "$PWD" /quality-pilot cases generate --init
{runner_command} --root "$PWD" /quality-pilot cases generate --init --count 5
{runner_command} --root "$PWD" /quality-pilot cases generate --growing
{runner_command} --root "$PWD" /quality-pilot cases generate --redmine-issues 144780 144693
{runner_command} --root "$PWD" /quality-pilot cases list
{runner_command} --root "$PWD" /quality-pilot cases run EXAMPLE-001
{runner_command} --root "$PWD" /quality-pilot publish wiki status
{runner_command} --root "$PWD" /quality-pilot publish wiki plan
{runner_command} --root "$PWD" /quality-pilot publish wiki apply
{runner_command} --root "$PWD" /quality-pilot close-loop run-once
{runner_command} --root "$PWD" /quality-pilot report status
```

## Safety Rules

- Do not directly write Gitea comments, close/reopen/edit issues, or PRs. New Gitea issue creation is allowed only through the gated MCP handoff returned by `/quality-pilot issues sync --redmine-issues ...` with `status: needs_mcp_apply`. Wiki remote writes are allowed only by AI Quality Pilot auto-sync, `/quality-pilot publish wiki apply`, or the gated MCP handoff returned by `/quality-pilot publish wiki apply` with `status: needs_mcp_apply`. Product PR creation remains behind `/quality-pilot issues fix --issue <id> --push-pr` or `/quality-pilot cases push-pr <case_id>`.
- Automatic Wiki sync must only update the configured Wiki page. It must not create issue comments, create issues, or open PRs.
- Do not use Gitea MCP for issue comments, issue edits, issue close/reopen, PR creation, or arbitrary writes. In AI Quality Pilot V1, Hermes MCP may create new issues only from the gated `mcp_issue_write_request` returned by `/quality-pilot issues sync --redmine-issues ...`, and may update only the configured Wiki page from `/quality-pilot publish wiki apply`.
- Do not reorder the AI Quality Pilot close-loop pipeline.
- Do not invent evidence paths.
- Do not print raw secrets.
- Do not run arbitrary shell commands assembled from chat. The only command you should run for `/quality-pilot ...` is the dispatcher command above with the user's AI Quality Pilot arguments.
- Do not bypass `write_gate`, issue sync, duplicate checks, or case contracts, even if the user asks you to write tracker output directly.
- `/quality-pilot cases generate --init` and `--growing` should produce executable side-effect-safe probes. If they return `payload.hermes_needs_input`, call `clarify` for category-level blocking inputs in Traditional Chinese. Do not force the user to approve test cases one by one.
- If you open a separate growth session/agent, it may only produce candidate analysis for AI Quality Pilot to validate; it must not directly edit case YAML, tracker, wiki, PRs, or reports.
- If the user types a removed command, report `command_removed` and its replacement.

## Expected Human Reply

Prefer concise replies like:

```text
quality-pilot> PASS
         cases: 1
         runners: 1
         latest_run_json: .quality-pilot-project/state/latest-run.json
         report: .quality-pilot-project/reports/status.md
```

If the result is blocked, failed, or invalid, include the reason and the next actionable command, for example `/quality-pilot help`, `/quality-pilot setup`, `/quality-pilot doctor`, or `/quality-pilot cases list`.

When `next_actions` exists, do not stop at the status line. Show a compact menu and invite the user to choose. The goal is an interactive QA assistant, not a silent JSON printer.

## If The Dispatcher Is Missing Or Broken

If `{runner_command}` is not found, tell the user that the console script may not be installed. Recommend reinstalling this skill from the AI Quality Pilot source checkout with an explicit runner command:

```bash
PYTHONPATH=/path/to/AI Quality Pilot/src python3 -m quality_pilot.hermes install-skill --force --runner-command "/usr/bin/env PYTHONPATH=/path/to/AI Quality Pilot/src python3 -m quality_pilot.hermes"
```

If direct dispatcher verification is needed, tell the user to run this from the product repo root:

```bash
PYTHONPATH=/path/to/AI Quality Pilot/src python3 -m quality_pilot.hermes --root "$PWD" /quality-pilot doctor
```
"""


def build_gitea_mcp_snapshot_reference(*, runner_command: str = "quality-pilot-hermes") -> str:
    return f"""# Gitea MCP snapshot for AI Quality Pilot issue sync

Use this when AI Quality Pilot reports `gitea_mcp_snapshot_missing` and the product config has `tracker.provider: hermes_mcp`.

## Workflow

1. Use the current Hermes product repository context.
2. Read pages with Gitea MCP using `state=all`, `perPage=50`, incrementing `page` until the returned page is empty.
3. Write the local snapshot to the configured `tracker.mcp.gitea_issues_json` path, usually `.quality-pilot-project/state/gitea-mcp/issues.json`.
4. Run the dispatcher command from the product repo root:
   `{runner_command} --root "$PWD" /quality-pilot issues sync`
5. Report the dispatcher `chat_response`; do not treat the MCP read itself as a completed sync.

## Pitfall: MCP issue list may include PRs

Some Gitea MCP `list_issues` responses can include pull requests as well as issues. AI Quality Pilot's HTTP client uses `type=issues`, so the MCP snapshot should avoid turning PRs into issue mirrors.

Recommended safe shape:

```json
{{
  "schema": "quality-pilot.gitea-mcp-issues.v1",
  "repo": "OWNER/REPO",
  "source_tool": "mcp_gitea_list_issues",
  "state": "all",
  "pages": [
    {{"page": 1, "perPage": 50, "returned": 50}},
    {{"page": 2, "perPage": 50, "returned": 0}}
  ],
  "issues": [
    {{
      "number": 123,
      "state": "open",
      "title": "...",
      "body": "...",
      "html_url": "https://.../issues/123",
      "labels": ["qa-auto"],
      "comments": [],
      "updated_at": "..."
    }}
  ]
}}
```

Filtering rule:

- Keep `html_url` containing `/issues/`.
- Exclude `html_url` containing `/pulls/` or entries carrying explicit PR markers.
- Preserve real issue bodies/comments when available; closed issues may be minimal because AI Quality Pilot only needs them to remove stale mirrors.

Remote write rule: never use Gitea MCP for comments, issue edits, issue close/reopen, PRs, or arbitrary writes in AI Quality Pilot. Gitea MCP may create new issues only after `/quality-pilot issues sync --redmine-issues ...` returns a gated `mcp_issue_write_request`, and may update only the configured Wiki page after `/quality-pilot publish wiki apply` returns a gated `mcp_write_request`. Write MCP result JSON to the requested path and report it as the same user flow. Product PR creation is a separate explicit workflow and must not be folded into Wiki apply or Redmine sync.
"""


def install_skill(
    skills_dir: str | Path | None = None,
    *,
    force: bool = False,
    runner_command: str = "quality-pilot-hermes",
) -> dict[str, Any]:
    base = Path(skills_dir).expanduser().resolve() if skills_dir else default_skills_dir().expanduser().resolve()
    skill_dir = base / HERMES_SKILL_NAME
    skill_path = skill_dir / HERMES_SKILL_FILE_NAME
    if skill_path.exists() and not force:
        return {
            "status": "error",
            "error": "skill_exists",
            "message": "Hermes AI Quality Pilot skill already exists. Re-run with --force to overwrite.",
            "skills_dir": str(base),
            "skill_dir": str(skill_dir),
            "skill_path": str(skill_path),
            "command_prefix": PRIMARY_PREFIX,
        }
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(build_skill_markdown(runner_command=runner_command), encoding="utf-8")
    reference_path = skill_dir / "references" / "gitea-mcp-snapshot.md"
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    reference_path.write_text(build_gitea_mcp_snapshot_reference(runner_command=runner_command), encoding="utf-8")
    return {
        "status": "ok",
        "skills_dir": str(base),
        "skill_dir": str(skill_dir),
        "skill_path": str(skill_path),
        "reference_path": str(reference_path),
        "command_prefix": PRIMARY_PREFIX,
        "reload_command": "/reload-skills",
        "runner_command": runner_command,
    }


def skill_status(skills_dir: str | Path | None = None) -> dict[str, Any]:
    base = Path(skills_dir).expanduser().resolve() if skills_dir else default_skills_dir().expanduser().resolve()
    skill_dir = base / HERMES_SKILL_NAME
    skill_path = skill_dir / HERMES_SKILL_FILE_NAME
    exists = skill_path.exists()
    valid = False
    if exists:
        text = skill_path.read_text(encoding="utf-8")
        valid = "name: quality-pilot" in text and "AI Quality Pilot Hermes Skill" in text
    return {
        "status": "ok" if valid else "missing",
        "skills_dir": str(base),
        "skill_dir": str(skill_dir),
        "skill_path": str(skill_path),
        "skill_exists": exists,
        "skill_valid": valid,
        "command_prefix": PRIMARY_PREFIX if valid else None,
    }


def _help_payload(engine_argv: list[str]) -> dict[str, Any] | None:
    args = _positional_args(engine_argv)
    if not args:
        return None
    if args == ["help"]:
        return _overview_help_payload()
    return None


def _overview_help_payload() -> dict[str, Any]:
    commands = [
        {"command": "/quality-pilot help", "purpose": "顯示這份中文手冊"},
        {"command": "/quality-pilot setup", "purpose": "在目前產品 repo 建立 .quality-pilot.yaml 與 .quality-pilot-project"},
        {"command": "/quality-pilot doctor", "purpose": "檢查設定、Hermes MCP、Gitea/Redmine readiness、runner、secret reference"},
        {"command": "/quality-pilot issues sync", "purpose": "同步 Gitea issues，內建 dedupe、prune 與遠端 duplicate gated action plan"},
        {"command": "/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]", "purpose": "透過 Hermes Redmine MCP snapshot 同步 Redmine mirror，並經 gate 用 Gitea MCP 建立 issues"},
        {"command": "/quality-pilot issues status", "purpose": "查看 issue sync、duplicates、fix queue、PR/handoff 狀態"},
        {"command": "/quality-pilot issues show <issue_id>", "purpose": "查看單一 issue mirror"},
        {"command": "/quality-pilot issues fix --all", "purpose": "依 open issue queue 逐一修復，遇到 gate/block 停下"},
        {"command": "/quality-pilot issues fix --issue <id>", "purpose": "對指定 issue 做 preflight、handoff、linked case/evidence 檢查"},
        {"command": "/quality-pilot issues fix --issue <id> --push-pr", "purpose": "修復與 gate 通過後建立產品修復 PR"},
        {"command": "/quality-pilot cases generate --init", "purpose": "首次全 repo SWQA 建案，依 README/code/metadata 產生可執行 safe-probe cases"},
        {"command": "/quality-pilot cases generate --init --count 5", "purpose": "限制初始建案第一批 case 數量"},
        {"command": "/quality-pilot cases generate --growing", "purpose": "依最新 issues/PR/latest-run/reports 狀態擴散 executable cases"},
        {"command": "/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]", "purpose": "透過 Hermes Redmine MCP snapshot 生成 linked cases"},
        {"command": "/quality-pilot cases review", "purpose": "查看仍需人工補強的 drafts；通常 init/growing 產物可直接 validate/dry-run"},
        {"command": "/quality-pilot cases validate", "purpose": "驗證 case YAML 是否可執行"},
        {"command": "/quality-pilot cases list", "purpose": "列出可以跑的測試 case"},
        {"command": "/quality-pilot cases run <case_id>", "purpose": "只跑一個 case，最適合第一次測試"},
        {"command": "/quality-pilot cases run", "purpose": "跑全部 case"},
        {"command": "/quality-pilot cases push-pr <case_id>", "purpose": "依 failing case/evidence 建立產品修復 PR"},
        {"command": "/quality-pilot publish wiki status", "purpose": "查看自動 Wiki 狀態同步結果"},
        {"command": "/quality-pilot publish wiki plan", "purpose": "手動產生 Wiki-only gated plan"},
        {"command": "/quality-pilot publish wiki apply", "purpose": "gate 通過後只更新 Gitea Wiki；MCP backend 會產生 Hermes MCP write request"},
        {"command": "/quality-pilot close-loop status", "purpose": "查看 Observe/Normalize/Execute/Triage/Publish/Evolve/Prune health dashboard"},
        {"command": "/quality-pilot close-loop run-once", "purpose": "跑完整 pipeline：檢查、測試、write gate、報告、保存 state"},
        {"command": "/quality-pilot report status", "purpose": "產生 Markdown report"},
        {"command": "/quality-pilot report json", "purpose": "輸出 latest run JSON"},
        {"command": "/quality-pilot tracker plan-write", "purpose": "相容舊版：只檢查單一 tracker write gate"},
    ]
    return {
        "status": "ok",
        "tool": "quality-pilot",
        "command_group": "help",
        "topic": "overview",
        "language": "zh-Hant",
        "commands": commands,
        "help_text": _overview_help_text(commands),
    }


def _overview_help_text(commands: list[dict[str, str]]) -> str:
    command_lines = [f"- `{item['command']}`：{item['purpose']}" for item in commands]
    return "\n".join(
        [
            "quality-pilot> HELP",
            "AI Quality Pilot 中文使用手冊",
            "",
            "`/quality-pilot setup` 會自動讀 git remote origin；若能辨識 Gitea repo，會先設定成 Hermes-friendly MCP backend。",
            "",
            "第一次使用建議流程：",
            "1. `/quality-pilot setup`：初始化目前產品 repo。",
            "2. `/quality-pilot doctor`：確認設定和目錄健康。",
            "3. `/quality-pilot issues sync`：同步 Gitea issues 到本地 mirror。",
            "4. `/quality-pilot cases generate --init`：首次分析 README、程式碼、metadata 與 rules，建立可執行 SWQA safe-probe cases。",
            "5. `/quality-pilot cases validate`：確認 generated contracts 可執行。",
            "6. `/quality-pilot cases list`：看有哪些 case_id。",
            "7. `/quality-pilot cases run <case_id>`：先跑一個 case，再決定是否跑全部。",
            "8. 測試或產生 cases 後，AI Quality Pilot 會自動更新本地 Wiki plan；查看 `/quality-pilot publish wiki status`。",
            "9. 若需要手動更新遠端 Wiki，跑 `/quality-pilot publish wiki plan` 再確認 `/quality-pilot publish wiki apply`。",
            "   若 backend 是 MCP，`apply` 會產生 gated MCP write request，由 Hermes 在同一流程呼叫 Gitea MCP。",
            "",
            "常用指令：",
            *command_lines,
            "",
            "移除的舊指令會回 `command_removed`，請照 replacement 改用新 workflow。",
            "",
            "最小 case YAML：",
            "```yaml",
            "case_id: EXAMPLE-001",
            "title: Project smoke test",
            "commands:",
            "  - id: smoke",
            "    run: .quality-pilot-project/runners/example-runner.sh",
            "    expected_exit_code: 0",
            "```",
            "",
            "重點名詞：",
            "- `case_id`：測試編號，例如 `EXAMPLE-001`，`cases run <case_id>` 會用到它。",
            "- `commands[].run`：真正要執行的測試 command 或 runner path。",
            "- `expected_exit_code`：預期 return code，通常是 `0`。",
            "- evidence：每次執行後保存 stdout、stderr、rc、meta、result.json 的資料夾。",
        ]
    )


def _positional_args(argv: list[str]) -> list[str]:
    value_options = {
        "--agent-dir",
        "--case-id",
        "--candidate-json",
        "--config",
        "--count",
        "--generated_count",
        "--generated-count",
        "--expected-contract-hash",
        "--event",
        "--feature",
        "--issue",
        "--issues-json",
        "--latest-run",
        "--plan",
        "--profile",
        "--result",
        "--result-json",
        "--root",
        "--runner-command",
        "--skills-dir",
        "--target-state",
        "--workspace",
    }
    out: list[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--json":
            continue
        if item.startswith("--"):
            option = item.split("=", 1)[0]
            skip_next = option in value_options and "=" not in item
            continue
        if item.startswith("-"):
            continue
        out.append(item)
    return out


def _inject_project_context(engine_argv: list[str], root: Path) -> None:
    path = _command_path(engine_argv)
    if len(path) >= 2 and path[0] == "config" and path[1] == "validate":
        if not _has_option(engine_argv, "--config"):
            engine_argv.extend(["--config", str(root / CONFIG_FILE)])
        return
    if len(path) >= 2 and path[0] == "config" and path[1] == "show":
        if not _has_option(engine_argv, "--root"):
            engine_argv.extend(["--root", str(root)])
        return
    if path and path[0] in ROOT_COMMANDS and not _has_option(engine_argv, "--root"):
        engine_argv.extend(["--root", str(root)])


def _command_path(argv: list[str]) -> list[str]:
    path: list[str] = []
    for item in argv:
        if item == "--json":
            continue
        if item.startswith("-"):
            continue
        path.append(item)
        if len(path) == 2:
            break
    return path


def _has_option(argv: list[str], option: str) -> bool:
    return any(item == option or item.startswith(f"{option}=") for item in argv)


def _parse_engine_json(raw_stdout: str) -> dict[str, Any] | None:
    if not raw_stdout:
        return None
    try:
        loaded = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _dispatch_status(payload: dict[str, Any], exit_code: int) -> str:
    payload_status = payload.get("status")
    if isinstance(payload_status, str) and payload_status:
        return payload_status
    return "ok" if exit_code == 0 else "error"


def _first_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    return None


def _parse_error_message(error: str) -> str:
    if error == "empty_hermes_message":
        return "Expected a Hermes chat command such as /quality-pilot doctor."
    if error == "not_a_quality_pilot_command":
        return "Only /quality-pilot commands are accepted by this dispatcher."
    if error == "empty_quality_pilot_command":
        return "Expected /quality-pilot followed by a AI Quality Pilot subcommand."
    return error


def _wrapper_script(runner_command: str) -> str:
    runner_argv = " ".join(shlex.quote(part) for part in shlex.split(runner_command))
    return f"""#!/usr/bin/env bash
set -euo pipefail
root="${{HERMES_PROJECT_ROOT:-${{PWD}}}}"
if [[ "$#" -eq 0 && -n "${{HERMES_MESSAGE:-}}" ]]; then
  exec {runner_argv} --root "$root" "${{HERMES_MESSAGE}}"
fi
exec {runner_argv} --root "$root" "$@"
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] in {"manifest", "install", "status", "install-skill", "skill-status"}:
        return _main_agent_command(argv)
    parser = argparse.ArgumentParser(prog="quality-pilot-hermes", description="Dispatch a Hermes /quality-pilot chat command to the AI Quality Pilot engine")
    parser.add_argument("--root", default=".", help="Product repository root provided by Hermes context")
    parser.add_argument("message", nargs=argparse.REMAINDER, help="Hermes chat message, for example: /quality-pilot doctor")
    args = parser.parse_args(argv)
    message = args.message[0].strip() if len(args.message) == 1 else shlex.join(args.message).strip()
    result = dispatch_chat_command(message, root=args.root)
    print(json_dumps(result))
    return int(result["exit_code"])


def _main_agent_command(argv: list[str]) -> int:
    command = argv[0]
    if command == "manifest":
        parser = argparse.ArgumentParser(prog="quality-pilot-hermes manifest", description="Print a portable Hermes agent manifest")
        parser.add_argument("--runner-command", default="quality-pilot-hermes")
        args = parser.parse_args(argv[1:])
        print(json_dumps(build_agent_manifest(runner_command=args.runner_command)))
        return 0
    if command == "install":
        parser = argparse.ArgumentParser(prog="quality-pilot-hermes install", description="Install AI Quality Pilot agent files into a Hermes agents directory")
        parser.add_argument("--agent-dir", required=True, help="Hermes agents directory")
        parser.add_argument("--runner-command", default="quality-pilot-hermes")
        parser.add_argument("--force", action="store_true", help="Overwrite existing AI Quality Pilot agent files")
        args = parser.parse_args(argv[1:])
        payload = install_agent(args.agent_dir, force=args.force, runner_command=args.runner_command)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 4
    if command == "install-skill":
        parser = argparse.ArgumentParser(prog="quality-pilot-hermes install-skill", description="Install AI Quality Pilot as a Hermes dynamic skill slash command")
        parser.add_argument("--skills-dir", default=None, help="Hermes skills directory; defaults to $HERMES_HOME/skills or ~/.hermes/skills")
        parser.add_argument("--runner-command", default="quality-pilot-hermes")
        parser.add_argument("--force", action="store_true", help="Overwrite an existing AI Quality Pilot skill")
        args = parser.parse_args(argv[1:])
        payload = install_skill(args.skills_dir, force=args.force, runner_command=args.runner_command)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 4
    if command == "skill-status":
        parser = argparse.ArgumentParser(prog="quality-pilot-hermes skill-status", description="Check AI Quality Pilot Hermes skill installation")
        parser.add_argument("--skills-dir", default=None, help="Hermes skills directory; defaults to $HERMES_HOME/skills or ~/.hermes/skills")
        args = parser.parse_args(argv[1:])
        payload = skill_status(args.skills_dir)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 2
    parser = argparse.ArgumentParser(prog="quality-pilot-hermes status", description="Check AI Quality Pilot Hermes agent installation")
    parser.add_argument("--agent-dir", required=True, help="Hermes agents directory")
    args = parser.parse_args(argv[1:])
    payload = agent_status(args.agent_dir)
    print(json_dumps(payload))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
