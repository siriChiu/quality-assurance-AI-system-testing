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

PRIMARY_PREFIX = "/qa-aist"
ALIAS_PREFIX = "qa-aist"
ACCEPTED_PREFIXES = {PRIMARY_PREFIX, ALIAS_PREFIX}
ROOT_COMMANDS = {
    "init-project",
    "setup",
    "status",
    "doctor",
    "issues",
    "cases",
    "qa-test",
    "publish",
    "fix-issues",
    "close-loop",
    "report",
    "tracker",
    "sync-gitea",
    "find-new-issues",
}
HELP_TOPICS = {"issues", "cases", "qa-test", "publish", "fix-issues"}
AGENT_MANIFEST_NAME = "qa-aist.agent.json"
AGENT_WRAPPER_NAME = "qa-aist-agent.sh"
HERMES_SKILL_NAME = "qa-aist"
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
        raise ValueError("not_a_qa_aist_command")
    if len(parts) == 1:
        raise ValueError("empty_qa_aist_command")

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
            "message": raw_stderr or raw_stdout or "QA-AIST engine did not emit JSON.",
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
    lines = [f"qa-aist> {status.upper()}"]

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
    elif issue_sync.get("token_env"):
        token_state = "set" if issue_sync.get("token_present") else "missing"
        lines.append(f"         token_env: {issue_sync.get('token_env')} ({token_state})")
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
    lines.append("請回覆選項編號，或直接輸入下一個 `/qa-aist ...` 指令。")
    return lines


def _with_next_actions(payload: dict[str, Any], engine_argv: list[str], exit_code: int) -> dict[str, Any]:
    if isinstance(payload.get("next_actions"), list):
        return payload
    actions = suggest_next_actions(payload, engine_argv, exit_code)
    if not actions:
        return payload
    return {**payload, "next_actions": actions}


def suggest_next_actions(payload: dict[str, Any], engine_argv: list[str], exit_code: int = 0) -> list[dict[str, Any]]:
    args = _positional_args(engine_argv)
    current = " ".join(args[:2]) if len(args) >= 2 else (args[0] if args else "")
    status = str(payload.get("status") or "").lower()
    error = str(payload.get("error") or "")
    message = str(payload.get("message") or "")
    issue_sync = payload.get("issue_sync") if isinstance(payload.get("issue_sync"), dict) else {}
    blockers = set(issue_sync.get("blockers", [])) if isinstance(issue_sync.get("blockers"), list) else set()

    if error == "config_not_found":
        return [
            _next("初始化目前 repo", "/qa-aist setup", confirm=True),
            _next("檢查目前路徑", "/qa-aist status"),
        ]
    if "gitea_mcp_snapshot_missing" in message:
        return [
            _next("用 Hermes Gitea MCP 讀取 issues，寫入 snapshot 後重跑 sync", "/qa-aist issues sync", confirm=True),
            _next("查看 Gitea/MCP 設定", "/qa-aist config show"),
            _next("查看 issue sync 狀態", "/qa-aist issues status"),
        ]
    if error in {"GiteaError", "IssueSyncError"}:
        return [
            _next("檢查設定", "/qa-aist config show"),
            _next("執行健康檢查", "/qa-aist doctor"),
            _next("查看 issue 狀態", "/qa-aist issues status"),
        ]
    if error in {"QAConfigError", "config_invalid"} or status == "error" and "config" in error.lower():
        return [
            _next("驗證設定", "/qa-aist config validate"),
            _next("查看設定", "/qa-aist config show"),
        ]

    if not args or args[0] == "help":
        return [
            _next("初始化產品 repo", "/qa-aist setup", confirm=True),
            _next("執行健康檢查", "/qa-aist doctor"),
            _next("同步 Gitea issues", "/qa-aist issues sync", confirm=True),
            _next("首次建立 SWQA cases", "/qa-aist cases generate --init", confirm=True),
        ]
    if current == "setup":
        if "gitea_mcp_snapshot_missing" in blockers:
            return [
                _next("用 Hermes Gitea MCP 讀取 issues，寫入 snapshot 後重跑 sync", "/qa-aist issues sync", confirm=True),
                _next("執行健康檢查", "/qa-aist doctor"),
                _next("查看 Gitea/MCP 設定", "/qa-aist config show"),
            ]
        return [
            _next("執行健康檢查", "/qa-aist doctor"),
            _next("查看設定", "/qa-aist config show"),
            _next("同步 Gitea issues", "/qa-aist issues sync", confirm=True),
        ]
    if current == "status" and payload.get("config_exists") is False:
        return [
            _next("初始化產品 repo", "/qa-aist setup", confirm=True),
            _next("查看中文手冊", "/qa-aist help"),
        ]
    if current in {"doctor", "status"}:
        if "gitea_mcp_snapshot_missing" in blockers:
            return [
                _next("用 Hermes Gitea MCP 讀取 issues，寫入 snapshot 後重跑 sync", "/qa-aist issues sync", confirm=True),
                _next("查看 Gitea/MCP 設定", "/qa-aist config show"),
                _next("查看 issue sync 狀態", "/qa-aist issues status"),
            ]
        if "gitea_http_token_missing" in blockers:
            return [
                _next("查看 Gitea HTTP 設定", "/qa-aist config show"),
                {"label": "設定 token env 後再重跑 doctor", "kind": "ask_user"},
                _next("切換成 MCP read-only sync", "/qa-aist config show"),
            ]
        if "tracker_provider_disabled" in blockers:
            return [
                _next("查看 tracker 設定", "/qa-aist config show"),
                {"label": "設定 tracker.provider: gitea 後重跑 doctor", "kind": "ask_user"},
            ]
        if status in {"warn", "error", "fail"}:
            return [
                _next("驗證設定", "/qa-aist config validate"),
                _next("查看設定", "/qa-aist config show"),
                _next("查看 issue sync 狀態", "/qa-aist issues status"),
            ]
        return [
            _next("同步 Gitea issues", "/qa-aist issues sync", confirm=True),
            _next("首次建立 SWQA cases", "/qa-aist cases generate --init", confirm=True),
            _next("列出測試 cases", "/qa-aist qa-test list"),
        ]
    if current == "config validate":
        return [
            _next("執行健康檢查", "/qa-aist doctor"),
            _next("同步 Gitea issues", "/qa-aist issues sync", confirm=True),
        ]
    if current == "issues sync":
        if exit_code == 0 and status in {"ok", "dry_run"}:
            return [
                _next("檢查重複 issue", "/qa-aist issues dedupe"),
                _next("用最新狀態長出測試 cases", "/qa-aist cases generate --growing", confirm=True),
                _next("查看 issue sync 狀態", "/qa-aist issues status"),
            ]
        return [
            _next("查看設定", "/qa-aist config show"),
            _next("查看 issue sync 狀態", "/qa-aist issues status"),
        ]
    if current == "issues status":
        if not payload.get("snapshot_exists"):
            return [
                _next("同步 Gitea issues", "/qa-aist issues sync", confirm=True),
                _next("查看設定", "/qa-aist config show"),
            ]
        return [
            _next("檢查重複 issue", "/qa-aist issues dedupe"),
            _next("長出測試 cases", "/qa-aist cases generate --growing", confirm=True),
        ]
    if current == "issues dedupe":
        return [
            _next("長出測試 cases", "/qa-aist cases generate --growing", confirm=True),
            _next("查看單一 issue", "/qa-aist issues show <issue_id>"),
        ]
    if current == "cases generate":
        if error == "explicit_generation_mode_required":
            return [
                _next("首次全 repo SWQA 建案", "/qa-aist cases generate --init", confirm=True),
                _next("依最新狀態擴散 cases", "/qa-aist cases generate --growing", confirm=True),
                _next("查看 cases 教學", "/qa-aist help cases"),
            ]
        if error == "candidate_json_requires_growing":
            return [
                _next("用 growing 匯入候選 JSON", "/qa-aist cases generate --growing --candidate-json <path>", confirm=True),
                _next("首次全 repo SWQA 建案", "/qa-aist cases generate --init", confirm=True),
            ]
        if status == "needs_input":
            return [
                _next("審查待補資訊", "/qa-aist cases review"),
                {"label": "逐題回答 fixture、輸入檔、成功條件與不可碰範圍", "kind": "ask_user"},
                _next("補完後驗證 cases", "/qa-aist cases validate"),
            ]
        return [
            _next("審查產生的 cases", "/qa-aist cases review"),
            _next("驗證 cases", "/qa-aist cases validate"),
            _next("列出可跑測試", "/qa-aist qa-test list"),
        ]
    if current in {"cases review", "cases validate"}:
        return [
            _next("列出可跑測試", "/qa-aist qa-test list"),
            _next("先 dry-run", "/qa-aist qa-test dry-run"),
        ]
    if args and args[0] == "qa-test":
        if args == ["qa-test"]:
            return [
                _next("列出可跑測試", "/qa-aist qa-test list"),
                _next("先 dry-run", "/qa-aist qa-test dry-run"),
                _next("看完整 qa-test 教學", "/qa-aist help qa-test"),
            ]
        if args[:2] == ["qa-test", "list"]:
            first_case = _first_case_id(payload)
            actions = [_next("先 dry-run", "/qa-aist qa-test dry-run")]
            if first_case:
                actions.append(_next(f"先跑單一 case {first_case}", f"/qa-aist qa-test run-one {first_case}", confirm=True))
            actions.append(_next("驗證 case YAML", "/qa-aist qa-test validate"))
            return actions
        if args[:2] in (["qa-test", "dry-run"], ["qa-test", "validate"]):
            first_case = _first_case_id(payload)
            if first_case:
                return [_next(f"執行單一 case {first_case}", f"/qa-aist qa-test run-one {first_case}", confirm=True)]
            return [_next("列出 cases", "/qa-aist qa-test list")]
        if args[:2] in (["qa-test", "run"], ["qa-test", "run-one"]):
            return [
                _next("產生報告", "/qa-aist report status"),
                _next("產生 publish plan", "/qa-aist publish plan", confirm=True),
                _next("查看 latest run JSON", "/qa-aist report json"),
            ]
    if current == "close-loop run-once":
        return [
            _next("產生報告", "/qa-aist report status"),
            _next("產生 publish plan", "/qa-aist publish plan", confirm=True),
        ]
    if current == "report status":
        return [
            _next("產生 publish plan", "/qa-aist publish plan", confirm=True),
            _next("查看 latest run JSON", "/qa-aist report json"),
        ]
    if current == "publish plan":
        if payload.get("status") == "ready" or payload.get("blocked_by_gate") == 0:
            return [
                _next("套用 Gitea 寫入計畫", "/qa-aist publish apply", confirm=True, destructive=True),
                _next("查看 publish 狀態", "/qa-aist publish status"),
            ]
        return [
            _next("查看 publish 狀態", "/qa-aist publish status"),
            _next("查看 write gate", "/qa-aist tracker plan-write"),
        ]
    if current == "publish apply":
        if status == "blocked":
            return [
                _next("查看 publish 狀態", "/qa-aist publish status"),
                _next("重新產生 publish plan", "/qa-aist publish plan", confirm=True),
            ]
        return [_next("查看 publish 狀態", "/qa-aist publish status")]
    if current == "fix-issues plan":
        if status == "ready":
            return [
                _next("建立 Hermes 修復 handoff", "/qa-aist fix-issues run --issue <id>", confirm=True),
                _next("先跑相關測試", "/qa-aist qa-test list"),
            ]
        return [
            _next("同步 issues", "/qa-aist issues sync", confirm=True),
            _next("檢查重複 issue", "/qa-aist issues dedupe"),
        ]
    if current == "fix-issues run":
        return [
            {"label": "讓 Hermes 依 handoff 做最小修復，完成後跑 linked case", "kind": "handoff"},
            _next("查看修復狀態", "/qa-aist fix-issues status"),
        ]
    if current == "fix-issues submit-pr":
        return [
            _next("查看 PR lifecycle 狀態", "/qa-aist fix-issues status"),
            _next("產生/更新報告", "/qa-aist report status"),
        ]
    return []


def _next(label: str, command: str, *, confirm: bool = False, destructive: bool = False) -> dict[str, Any]:
    return {
        "label": label,
        "command": command,
        "requires_confirmation": confirm or destructive,
        "destructive": destructive,
    }


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


def build_agent_manifest(*, wrapper_path: str | None = None, runner_command: str = "qa-aist-hermes") -> dict[str, Any]:
    entrypoint_command = [wrapper_path] if wrapper_path else [runner_command, "--root", "${HERMES_PROJECT_ROOT}", "${HERMES_MESSAGE}"]
    return {
        "schema": "hermes.agent.v1",
        "name": "qa-aist",
        "display_name": "QA-AIST",
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
            "module": "qa_aist.hermes",
            "callable": "dispatch_chat_command",
            "signature": "dispatch_chat_command(message: str, root: str | Path = '.') -> dict",
        },
        "engine": {
            "console_script": "qa-aist",
            "hermes_console_script": runner_command,
            "json_output": True,
        },
        "commands": [
            f"{PRIMARY_PREFIX} help",
            f"{PRIMARY_PREFIX} help qa-test",
            f"{PRIMARY_PREFIX} setup",
            f"{PRIMARY_PREFIX} status",
            f"{PRIMARY_PREFIX} doctor",
            f"{PRIMARY_PREFIX} config show",
            f"{PRIMARY_PREFIX} config validate",
            f"{PRIMARY_PREFIX} issues sync",
            f"{PRIMARY_PREFIX} issues status",
            f"{PRIMARY_PREFIX} issues show <issue_id>",
            f"{PRIMARY_PREFIX} issues dedupe",
            f"{PRIMARY_PREFIX} cases generate --init",
            f"{PRIMARY_PREFIX} cases generate --growing",
            f"{PRIMARY_PREFIX} cases generate --init --feature <name>",
            f"{PRIMARY_PREFIX} cases generate --init --profile auto|cli|api|hardware|repo",
            f"{PRIMARY_PREFIX} cases generate --init --count 5",
            f"{PRIMARY_PREFIX} cases generate --growing --candidate-json <path>",
            f"{PRIMARY_PREFIX} cases review",
            f"{PRIMARY_PREFIX} cases validate",
            f"{PRIMARY_PREFIX} qa-test list",
            f"{PRIMARY_PREFIX} qa-test validate",
            f"{PRIMARY_PREFIX} qa-test dry-run",
            f"{PRIMARY_PREFIX} qa-test run",
            f"{PRIMARY_PREFIX} qa-test run-one <case_id>",
            f"{PRIMARY_PREFIX} qa-test help",
            f"{PRIMARY_PREFIX} publish plan",
            f"{PRIMARY_PREFIX} publish apply",
            f"{PRIMARY_PREFIX} publish status",
            f"{PRIMARY_PREFIX} fix-issues plan --issue <id>",
            f"{PRIMARY_PREFIX} fix-issues run --issue <id>",
            f"{PRIMARY_PREFIX} fix-issues submit-pr --issue <id>",
            f"{PRIMARY_PREFIX} fix-issues status",
            f"{PRIMARY_PREFIX} close-loop status",
            f"{PRIMARY_PREFIX} close-loop run-once",
            f"{PRIMARY_PREFIX} report status",
            f"{PRIMARY_PREFIX} report json",
            f"{PRIMARY_PREFIX} tracker plan-write",
        ],
        "permissions": {
            "filesystem": ["project_root"],
            "network": ["gitea_http_when_apply_or_submit_pr", "gitea_mcp_read_when_configured"],
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
            "interaction_style": "guided_menu",
        },
    }


def install_agent(agent_dir: str | Path, *, force: bool = False, runner_command: str = "qa-aist-hermes") -> dict[str, Any]:
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


def build_skill_markdown(*, runner_command: str = "qa-aist-hermes") -> str:
    return f"""---
name: qa-aist
description: "QA-AIST dynamic skill: call the deterministic QA lifecycle engine for Gitea issue sync, case generation, tests, evidence, publishing, and PR flow."
version: {__version__}
author: QA-AIST contributors
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [qa, testing, deterministic, evidence, write-gate, tracker, dynamic-skill]
---

# QA-AIST Hermes Skill

This SKILL.md is the current Hermes integration for QA-AIST.

It makes `/qa-aist ...` visible to Hermes as a dynamic skill slash command, then instructs the Hermes agent to call the deterministic QA-AIST dispatcher. This is skill-mediated. It is not a native Hermes router, not a pre-LLM command hook, and not a Python package autoload mechanism.

QA-AIST is responsible for issue sync, case contracts, test execution, evidence, write gate, Gitea wiki/issues publishing, and Gitea PR creation. Hermes may answer questions and make code changes, but it must not bypass QA-AIST for tracker/wiki/PR decisions.

## Required Behavior For Every `/qa-aist` Turn

When the user invokes `/qa-aist <arguments>`, you must:

1. Treat everything after `/qa-aist` as QA-AIST dispatcher arguments.
2. Use the current product repository root as `--root`. Do not use the QA-AIST source checkout as root unless the user is working on QA-AIST itself.
3. Execute the dispatcher through the terminal. Do not answer from memory.
4. Read the returned JSON.
5. Reply primarily with the JSON `chat_response` field.
6. If `chat_response` is missing, summarize `status`, `payload.status`, `payload.error`, `payload.message`, `latest_run_json`, `report_path`, and evidence paths.
7. Preserve failures. If the dispatcher exits non-zero or emits invalid JSON, tell the user the exit code and useful stderr/stdout details.

Gitea MCP rule: if the product repo config uses `tracker.gitea.backend: mcp`, you may use Hermes' configured Gitea MCP tooling only to read issue data before `/qa-aist issues sync`. Write the raw MCP issue result as JSON to `.qa-aist-project/state/gitea-mcp/issues.json`, unless `.qa-aist.yaml` or `QA_AIST_GITEA_MCP_ISSUES_JSON` specifies another path. Then run the QA-AIST dispatcher normally. Do not treat the MCP read itself as a completed sync.

Gitea MCP snapshot workflow (when the user confirms, chooses a suggested sync option, or invokes `/qa-aist issues sync` after `gitea_mcp_snapshot_missing`):
1. Use Gitea MCP read-only pagination for the configured repo, typically `state=all` and `perPage=50`, until an empty page is returned.
2. Preserve the MCP payload shape as JSON and write it to the configured `mcp_issues_json` path, creating parent directories if needed.
3. If the MCP list response includes pull requests mixed with issues, keep only real Gitea issues before writing the QA-AIST `issues` list. A reliable guard is `html_url` containing `/issues/` and excluding `/pulls/`.
4. Immediately run `/qa-aist issues sync` via the dispatcher command.
5. Never use Gitea MCP for remote writes; publish/PR writes must remain gated through QA-AIST commands.

Reference: see `references/gitea-mcp-snapshot.md` for the MCP snapshot pitfall and recommended JSON shape.

Setup rule: `/qa-aist setup` auto-detects `git remote origin`. If the remote looks like Gitea, setup writes `tracker.provider: gitea`, `tracker.gitea.backend: mcp`, `base_url`, `repo`, and the MCP snapshot path into `.qa-aist.yaml`. Do not ask the user to hand-edit this unless detection is wrong or they explicitly want HTTP token mode.

## Interactive Guidance Model

Do not behave like a passive command relay. After every QA-AIST turn, guide the user toward the next useful step in Traditional Chinese.

Use this pattern:

1. Briefly explain what just happened.
2. If the JSON payload contains `next_actions`, present them as a small numbered menu.
3. Ask the user to choose a number, approve the recommended action, or type another `/qa-aist ...` command.
4. If the next action is safe and read-only, you may offer to run it immediately.
5. If the next action writes files, runs tests, uses Gitea MCP, publishes to Gitea, pushes a branch, or creates a PR, ask for confirmation first.
6. If the command returns questions or missing inputs, ask the questions one at a time in Traditional Chinese and wait for the user's answer.

Preferred menu style:

```text
下一步可以選：
1. 執行健康檢查：/qa-aist doctor
2. 同步 Gitea issues：/qa-aist issues sync（需確認）
3. 查看 qa-test 教學：/qa-aist help qa-test

請回覆 1、2、3，或直接輸入下一個 /qa-aist ... 指令。
```

Recommended interaction by situation:

- After `/qa-aist setup`: suggest `/qa-aist doctor`, `/qa-aist config show`, then `/qa-aist issues sync`.
- If setup reports `auto_configured_mcp: true`, explain that QA-AIST is configured and the remaining step is a Hermes Gitea MCP read to create the local snapshot.
- After `/qa-aist doctor` warning: explain the warning and suggest the smallest check that resolves it.
- After `gitea_mcp_snapshot_missing`: offer to use Hermes Gitea MCP read-only fetch, write the snapshot, then rerun `/qa-aist issues sync`.
- After `/qa-aist issues sync`: suggest `/qa-aist issues dedupe` and `/qa-aist cases generate --growing`.
- If the user asks for first-time test ideas or has no cases yet, run `/qa-aist cases generate --init`; it scans README, code, package metadata, existing runners, existing cases, and project rules to create an initial SWQA test map.
- If the user asks for follow-up ideas after issues/PRs/runs changed, run `/qa-aist cases generate --growing`; it creates incremental growth draft cases from repo, issues, PR references, latest run, reports, existing cases, and runners.
- If the user types bare `/qa-aist cases generate`, run the dispatcher and present its mode-selection error; do not silently choose a mode.
- After `cases generate --init` or `cases generate --growing` returns questions: ask the questions interactively before treating the draft as runnable.
- After `/qa-aist qa-test list`: suggest dry-run or running one selected case, not all cases by default.
- After a test run: suggest `/qa-aist report status` and `/qa-aist publish plan`.
- Before `/qa-aist publish apply` or `/qa-aist fix-issues submit-pr`: ask for explicit confirmation and summarize what will be written remotely.

Use this command shape:

```bash
{runner_command} --root "$PWD" /qa-aist <arguments>
```

`$PWD` must be the user's product repository root. If the active root is unclear, inspect the current workspace/cwd. If it is still unclear, ask the user for the product repo path instead of creating `.qa-aist-project` in the wrong directory.

Examples:

```bash
{runner_command} --root "$PWD" /qa-aist help
{runner_command} --root "$PWD" /qa-aist help qa-test
{runner_command} --root "$PWD" /qa-aist setup
{runner_command} --root "$PWD" /qa-aist status
{runner_command} --root "$PWD" /qa-aist doctor
{runner_command} --root "$PWD" /qa-aist issues sync
{runner_command} --root "$PWD" /qa-aist cases generate --init
{runner_command} --root "$PWD" /qa-aist cases generate --growing
{runner_command} --root "$PWD" /qa-aist cases generate --init --feature "CLI help" --profile cli --count 5
{runner_command} --root "$PWD" /qa-aist cases generate --growing --candidate-json .qa-aist-project/state/growth-candidates.json
{runner_command} --root "$PWD" /qa-aist qa-test list
{runner_command} --root "$PWD" /qa-aist qa-test
{runner_command} --root "$PWD" /qa-aist qa-test run-one EXAMPLE-001
{runner_command} --root "$PWD" /qa-aist publish plan
{runner_command} --root "$PWD" /qa-aist publish apply
{runner_command} --root "$PWD" /qa-aist fix-issues plan --issue 123
{runner_command} --root "$PWD" /qa-aist fix-issues submit-pr --issue 123
{runner_command} --root "$PWD" /qa-aist close-loop run-once
{runner_command} --root "$PWD" /qa-aist report status
```

## Supported User-facing Commands

- `/qa-aist help`
- `/qa-aist help qa-test`
- `/qa-aist setup`
- `/qa-aist status`
- `/qa-aist doctor`
- `/qa-aist config show`
- `/qa-aist config validate`
- `/qa-aist issues sync`
- `/qa-aist issues status`
- `/qa-aist issues show <issue_id>`
- `/qa-aist issues dedupe`
- `/qa-aist cases generate --init`
- `/qa-aist cases generate --growing`
- `/qa-aist cases generate --init --feature <name>`
- `/qa-aist cases generate --init --profile auto|cli|api|hardware|repo`
- `/qa-aist cases generate --init --count 5`
- `/qa-aist cases generate --growing --candidate-json <path>`
- `/qa-aist cases review`
- `/qa-aist cases validate`
- `/qa-aist qa-test list`
- `/qa-aist qa-test validate`
- `/qa-aist qa-test dry-run`
- `/qa-aist qa-test run`
- `/qa-aist qa-test run-one <case_id>`
- `/qa-aist qa-test help`
- `/qa-aist publish plan`
- `/qa-aist publish apply`
- `/qa-aist publish status`
- `/qa-aist fix-issues plan --issue <id>`
- `/qa-aist fix-issues run --issue <id>`
- `/qa-aist fix-issues submit-pr --issue <id>`
- `/qa-aist fix-issues status`
- `/qa-aist close-loop status`
- `/qa-aist close-loop run-once`
- `/qa-aist report status`
- `/qa-aist report json`
- `/qa-aist tracker plan-write`

## Safety Rules

- Do not directly write Gitea comments, issues, wiki pages, or PRs. Remote writes are allowed only by `/qa-aist publish apply` or `/qa-aist fix-issues submit-pr` after QA-AIST write gate passes.
- Do not use Gitea MCP for remote writes. In QA-AIST V1, `tracker.gitea.backend: mcp` is read-only and only feeds `/qa-aist issues sync` through a local JSON snapshot.
- Do not reorder the QA-AIST close-loop pipeline.
- Do not invent evidence paths.
- Do not print raw secrets.
- Do not run arbitrary shell commands assembled from chat. The only command you should run for `/qa-aist ...` is the dispatcher command above with the user's QA-AIST arguments.
- Do not bypass `write_gate`, issue sync, duplicate checks, or case contracts, even if the user asks you to write tracker output directly.
- If `/qa-aist cases generate --init` or `/qa-aist cases generate --growing` returns questions, ask those questions in Traditional Chinese and wait for answers before treating a draft as runnable.
- If you open a separate growth session/agent, it may only write candidate JSON for `/qa-aist cases generate --growing --candidate-json <path>`; it must not directly edit case YAML, tracker, wiki, PRs, or reports.
- If the user types `/qa-aist qa-test` without a subcommand, show the QA test help instead of guessing.

## Expected Human Reply

Prefer concise replies like:

```text
qa-aist> PASS
         cases: 1
         runners: 1
         latest_run_json: .qa-aist-project/state/latest-run.json
         report: .qa-aist-project/reports/status.md
```

If the result is blocked, failed, or invalid, include the reason and the next actionable command, for example `/qa-aist help`, `/qa-aist setup`, `/qa-aist config validate`, or `/qa-aist qa-test list`.

When `next_actions` exists, do not stop at the status line. Show a compact menu and invite the user to choose. The goal is an interactive QA assistant, not a silent JSON printer.

## If The Dispatcher Is Missing Or Broken

If `{runner_command}` is not found, tell the user that the console script may not be installed. Recommend reinstalling this skill from the QA-AIST source checkout with an explicit runner command:

```bash
PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes install-skill --force --runner-command "/usr/bin/env PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes"
```

If direct dispatcher verification is needed, tell the user to run this from the product repo root:

```bash
PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist doctor
```
"""


def build_gitea_mcp_snapshot_reference(*, runner_command: str = "qa-aist-hermes") -> str:
    return f"""# Gitea MCP snapshot for QA-AIST issue sync

Use this when QA-AIST reports `gitea_mcp_snapshot_missing` and the product config has `tracker.gitea.backend: mcp`.

## Workflow

1. Determine `owner/repo` from `.qa-aist.yaml` (`tracker.gitea.repo`).
2. Read pages with Gitea MCP using `state=all`, `perPage=50`, incrementing `page` until the returned page is empty.
3. Write the local snapshot to the configured `tracker.gitea.mcp_issues_json` path, usually `.qa-aist-project/state/gitea-mcp/issues.json`.
4. Run the dispatcher command from the product repo root:
   `{runner_command} --root "$PWD" /qa-aist issues sync`
5. Report the dispatcher `chat_response`; do not treat the MCP read itself as a completed sync.

## Pitfall: MCP issue list may include PRs

Some Gitea MCP `list_issues` responses can include pull requests as well as issues. QA-AIST's HTTP client uses `type=issues`, so the MCP snapshot should avoid turning PRs into issue mirrors.

Recommended safe shape:

```json
{{
  "schema": "qa-aist.gitea-mcp-issues.v1",
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
- Preserve real issue bodies/comments when available; closed issues may be minimal because QA-AIST only needs them to remove stale mirrors.

Remote write rule: never use Gitea MCP for comments/wiki/PR writes in QA-AIST. Only `/qa-aist publish apply` and `/qa-aist fix-issues submit-pr` may perform gated writes.
"""


def install_skill(
    skills_dir: str | Path | None = None,
    *,
    force: bool = False,
    runner_command: str = "qa-aist-hermes",
) -> dict[str, Any]:
    base = Path(skills_dir).expanduser().resolve() if skills_dir else default_skills_dir().expanduser().resolve()
    skill_dir = base / HERMES_SKILL_NAME
    skill_path = skill_dir / HERMES_SKILL_FILE_NAME
    if skill_path.exists() and not force:
        return {
            "status": "error",
            "error": "skill_exists",
            "message": "Hermes QA-AIST skill already exists. Re-run with --force to overwrite.",
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
        valid = "name: qa-aist" in text and "QA-AIST Hermes Skill" in text
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
    if args[0] == "help":
        topic = args[1] if len(args) > 1 else "overview"
        if topic in {"overview", "commands", "command", "all"}:
            return _overview_help_payload()
        if topic == "qa-test":
            return _qa_test_help_payload()
        if topic in HELP_TOPICS:
            return _workflow_help_payload(topic)
        return _unknown_help_payload(topic)
    if args[0] == "qa-test" and (len(args) == 1 or (len(args) >= 2 and args[1] in {"help", "-h", "--help", "?"})):
        return _qa_test_help_payload()
    return None


def _overview_help_payload() -> dict[str, Any]:
    commands = [
        {"command": "/qa-aist help", "purpose": "顯示這份中文手冊"},
        {"command": "/qa-aist help qa-test", "purpose": "只看 qa-test 測試 case 教學"},
        {"command": "/qa-aist setup", "purpose": "在目前產品 repo 建立 .qa-aist.yaml 與 .qa-aist-project"},
        {"command": "/qa-aist doctor", "purpose": "檢查設定、目錄、runner、secret reference 是否健康"},
        {"command": "/qa-aist status", "purpose": "查看 workspace、case 數量、latest run"},
        {"command": "/qa-aist config show", "purpose": "顯示目前解析後的 QA-AIST 設定"},
        {"command": "/qa-aist config validate", "purpose": "驗證 .qa-aist.yaml 是否完整且沒有 raw secret"},
        {"command": "/qa-aist issues sync", "purpose": "從 Gitea 同步 open/closed issues 到本地 mirror"},
        {"command": "/qa-aist issues status", "purpose": "查看本地 issue snapshot 是否已同步"},
        {"command": "/qa-aist issues show <issue_id>", "purpose": "查看單一 issue mirror"},
        {"command": "/qa-aist issues dedupe", "purpose": "檢查本地 active issues 是否疑似重複"},
        {"command": "/qa-aist cases generate --init", "purpose": "首次全 repo SWQA 建案，依 README/code/metadata 產生 draft cases"},
        {"command": "/qa-aist cases generate --growing", "purpose": "依最新 issues/PR/latest-run/reports 狀態擴散 draft cases"},
        {"command": "/qa-aist cases generate --init --feature \"CLI help\" --profile cli --count 5", "purpose": "指定功能、profile 與數量來引導初始建案"},
        {"command": "/qa-aist cases generate --growing --candidate-json <path>", "purpose": "匯入 Hermes growth session 候選 JSON，經 engine 驗證後寫入 draft"},
        {"command": "/qa-aist cases review", "purpose": "查看 draft cases 與待回答問題"},
        {"command": "/qa-aist cases validate", "purpose": "驗證 generated case YAML 是否可被 qa-test 執行"},
        {"command": "/qa-aist qa-test list", "purpose": "列出可以跑的測試 case"},
        {"command": "/qa-aist qa-test validate", "purpose": "檢查 case YAML 格式是否正確"},
        {"command": "/qa-aist qa-test dry-run", "purpose": "預覽會執行哪些 command，但不真的跑"},
        {"command": "/qa-aist qa-test help", "purpose": "等同 /qa-aist help qa-test"},
        {"command": "/qa-aist qa-test run-one <case_id>", "purpose": "只跑一個 case，最適合第一次測試"},
        {"command": "/qa-aist qa-test run", "purpose": "跑全部 case"},
        {"command": "/qa-aist publish plan", "purpose": "把 latest run 轉成 wiki/issue write plan 並跑 gate"},
        {"command": "/qa-aist publish apply", "purpose": "gate 通過後真的寫 Gitea wiki/issues"},
        {"command": "/qa-aist publish status", "purpose": "查看最新 publish plan/apply 結果"},
        {"command": "/qa-aist fix-issues plan --issue <id>", "purpose": "修復前同步/去重/檢查 open issue"},
        {"command": "/qa-aist fix-issues run --issue <id>", "purpose": "產生給 Hermes 的最小修復 handoff"},
        {"command": "/qa-aist fix-issues submit-pr --issue <id>", "purpose": "push branch 並用 Gitea API 建 PR"},
        {"command": "/qa-aist fix-issues status", "purpose": "查看修復/PR lifecycle 狀態"},
        {"command": "/qa-aist close-loop status", "purpose": "查看 close-loop pipeline 順序與 latest run"},
        {"command": "/qa-aist close-loop run-once", "purpose": "跑完整 pipeline：檢查、測試、write gate、報告、保存 state"},
        {"command": "/qa-aist report status", "purpose": "產生 Markdown report"},
        {"command": "/qa-aist report json", "purpose": "輸出 latest run JSON"},
        {"command": "/qa-aist tracker plan-write", "purpose": "相容舊版：只檢查單一 tracker write gate"},
    ]
    return {
        "status": "ok",
        "tool": "qa-aist",
        "command_group": "help",
        "topic": "overview",
        "language": "zh-Hant",
        "commands": commands,
        "help_text": _overview_help_text(commands),
    }


def _qa_test_help_payload() -> dict[str, Any]:
    steps = [
        "先跑 /qa-aist setup，讓專案產生範例 case。",
        "跑 /qa-aist qa-test list，找到 case_id。",
        "跑 /qa-aist qa-test dry-run，確認 QA-AIST 會執行哪些 command。",
        "跑 /qa-aist qa-test run-one <case_id>，先只跑一個 case。",
        "看 .qa-aist-project/evidence/<case_id>/ 裡的 stdout、stderr、rc、meta、result.json。",
        "單一 case 穩定後，再跑 /qa-aist qa-test run 或 /qa-aist close-loop run-once。",
    ]
    commands = [
        {"command": "/qa-aist qa-test list", "purpose": "列出所有 case_id、title、contract hash"},
        {"command": "/qa-aist qa-test validate", "purpose": "只檢查 YAML 格式，不執行測試"},
        {"command": "/qa-aist qa-test dry-run", "purpose": "預覽 command 順序，不執行測試"},
        {"command": "/qa-aist qa-test run-one EXAMPLE-001", "purpose": "執行單一 case"},
        {"command": "/qa-aist qa-test run", "purpose": "執行全部 case"},
    ]
    return {
        "status": "ok",
        "tool": "qa-aist",
        "command_group": "help",
        "topic": "qa-test",
        "language": "zh-Hant",
        "steps": steps,
        "commands": commands,
        "help_text": _qa_test_help_text(steps, commands),
    }


def _unknown_help_payload(topic: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "tool": "qa-aist",
        "command_group": "help",
        "topic": topic,
        "language": "zh-Hant",
        "help_text": "\n".join(
            [
                "qa-aist> HELP",
                f"找不到 `{topic}` 這個 help topic。",
                "",
                "可用手冊：",
                "- /qa-aist help",
                "- /qa-aist help qa-test",
            ]
        ),
    }


def _overview_help_text(commands: list[dict[str, str]]) -> str:
    command_lines = [f"- `{item['command']}`：{item['purpose']}" for item in commands]
    return "\n".join(
        [
            "qa-aist> HELP",
            "QA-AIST 中文使用手冊",
            "",
            "`/qa-aist setup` 會自動讀 git remote origin；若能辨識 Gitea repo，會先設定成 Hermes-friendly MCP backend。",
            "",
            "第一次使用建議流程：",
            "1. `/qa-aist setup`：初始化目前產品 repo。",
            "2. `/qa-aist doctor`：確認設定和目錄健康。",
            "3. `/qa-aist issues sync`：同步 Gitea issues 到本地 mirror。",
            "4. `/qa-aist cases generate --init`：首次分析 README、程式碼、metadata 與 rules，建立 SWQA draft cases。",
            "5. `/qa-aist cases review`：回答缺少的測試輸入問題。",
            "6. `/qa-aist qa-test run-one <case_id>`：先跑一個 case。",
            "7. `/qa-aist publish plan`：產生 wiki/issues 寫入計畫並通過 gate。",
            "8. `/qa-aist publish apply`：明確要求後才真的寫 Gitea。",
            "",
            "常用指令：",
            *command_lines,
            "",
            "qa-test 看不懂時，直接輸入：",
            "`/qa-aist help qa-test`",
            "",
            "完整 lifecycle topic：",
            "`/qa-aist help issues`、`/qa-aist help cases`、`/qa-aist help publish`、`/qa-aist help fix-issues`",
        ]
    )


def _workflow_help_payload(topic: str) -> dict[str, Any]:
    topic_text = {
        "issues": [
            "issues 用來同步 Gitea 遠端狀態到 `.qa-aist-project/issues`。",
            "先跑 `/qa-aist issues sync`，closed issue 會從 active mirror 移除。",
            "再用 `/qa-aist issues dedupe` 確認沒有重複 active issue。",
        ],
        "cases": [
            "cases 用來產生可審查的 growth draft case contract。",
            "首次導入先跑 `/qa-aist cases generate --init`，QA-AIST 會讀 README、程式碼、metadata、既有 runners/cases/rules，建立功能、正向、反向、邊界與壓力測試 draft。",
            "後續有新 issues、PR、latest run 或 reports 時跑 `/qa-aist cases generate --growing`，讓 case 從最新狀態繼續擴散。",
            "可用 `--feature`、`--profile auto|cli|api|hardware|repo`、`--count` 控制生成方向；獨立 growth session 的候選 JSON 必須搭配 `--growing --candidate-json` 匯入。",
            "如果輸出 questions，Hermes 必須用繁中問答補齊 fixture、輸入檔、成功條件和副作用邊界。",
        ],
        "publish": [
            "publish 用來把 latest run 變成 Gitea wiki/issues 寫入。",
            "`publish plan` 只產生 gated plan；`publish apply` 才真的寫遠端。",
            "gate blocked 時，Hermes 不得自己改用 curl 或 API 繞過。",
        ],
        "fix-issues": [
            "fix-issues 用來修復 synced open issue 並送 PR。",
            "先跑 `fix-issues plan --issue <id>` 確認 sync、dedupe、case/evidence 狀態。",
            "Hermes 修碼後再跑測試、publish plan，最後 `submit-pr` 建 Gitea PR。",
        ],
    }
    lines = topic_text[topic]
    return {
        "status": "ok",
        "tool": "qa-aist",
        "command_group": "help",
        "topic": topic,
        "language": "zh-Hant",
        "help_text": "\n".join(["qa-aist> HELP", f"{topic} 使用說明", "", *lines]),
    }


def _qa_test_help_text(steps: list[str], commands: list[dict[str, str]]) -> str:
    step_lines = [f"{index}. {step}" for index, step in enumerate(steps, start=1)]
    command_lines = [f"- `{item['command']}`：{item['purpose']}" for item in commands]
    return "\n".join(
        [
            "qa-aist> HELP",
            "qa-test 是什麼？",
            "",
            "`qa-test` 是 QA-AIST 用來執行「case contract」的指令群組。",
            "你不用讓 Hermes 自己拼測試指令；你只要把測試步驟寫在 `.qa-aist-project/cases/*.yaml`，QA-AIST 會照順序執行、保存 evidence，並計算 contract hash。",
            "",
            "最小 case YAML：",
            "```yaml",
            "case_id: EXAMPLE-001",
            "title: Project smoke test",
            "commands:",
            "  - id: smoke",
            "    run: .qa-aist-project/runners/example-runner.sh",
            "    expected_exit_code: 0",
            "```",
            "",
            "建議操作順序：",
            *step_lines,
            "",
            "qa-test 指令：",
            *command_lines,
            "",
            "重點名詞：",
            "- `case_id`：測試編號，例如 `EXAMPLE-001`，run-one 會用到它。",
            "- `commands[].run`：真正要執行的測試 command 或 runner path。",
            "- `expected_exit_code`：預期 return code，通常是 `0`。",
            "- evidence：每次執行後保存 stdout、stderr、rc、meta、result.json 的資料夾。",
            "",
            "最安全的第一步：",
            "`/qa-aist qa-test list`",
        ]
    )


def _positional_args(argv: list[str]) -> list[str]:
    value_options = {
        "--agent-dir",
        "--case-id",
        "--candidate-json",
        "--config",
        "--count",
        "--expected-contract-hash",
        "--feature",
        "--issue",
        "--issues-json",
        "--latest-run",
        "--plan",
        "--profile",
        "--result",
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
        return "Expected a Hermes chat command such as /qa-aist status."
    if error == "not_a_qa_aist_command":
        return "Only /qa-aist commands are accepted by this dispatcher."
    if error == "empty_qa_aist_command":
        return "Expected /qa-aist followed by a QA-AIST subcommand."
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
    parser = argparse.ArgumentParser(prog="qa-aist-hermes", description="Dispatch a Hermes /qa-aist chat command to the QA-AIST engine")
    parser.add_argument("--root", default=".", help="Product repository root provided by Hermes context")
    parser.add_argument("message", nargs=argparse.REMAINDER, help="Hermes chat message, for example: /qa-aist status")
    args = parser.parse_args(argv)
    message = args.message[0].strip() if len(args.message) == 1 else shlex.join(args.message).strip()
    result = dispatch_chat_command(message, root=args.root)
    print(json_dumps(result))
    return int(result["exit_code"])


def _main_agent_command(argv: list[str]) -> int:
    command = argv[0]
    if command == "manifest":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes manifest", description="Print a portable Hermes agent manifest")
        parser.add_argument("--runner-command", default="qa-aist-hermes")
        args = parser.parse_args(argv[1:])
        print(json_dumps(build_agent_manifest(runner_command=args.runner_command)))
        return 0
    if command == "install":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes install", description="Install QA-AIST agent files into a Hermes agents directory")
        parser.add_argument("--agent-dir", required=True, help="Hermes agents directory")
        parser.add_argument("--runner-command", default="qa-aist-hermes")
        parser.add_argument("--force", action="store_true", help="Overwrite existing QA-AIST agent files")
        args = parser.parse_args(argv[1:])
        payload = install_agent(args.agent_dir, force=args.force, runner_command=args.runner_command)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 4
    if command == "install-skill":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes install-skill", description="Install QA-AIST as a Hermes dynamic skill slash command")
        parser.add_argument("--skills-dir", default=None, help="Hermes skills directory; defaults to $HERMES_HOME/skills or ~/.hermes/skills")
        parser.add_argument("--runner-command", default="qa-aist-hermes")
        parser.add_argument("--force", action="store_true", help="Overwrite an existing QA-AIST skill")
        args = parser.parse_args(argv[1:])
        payload = install_skill(args.skills_dir, force=args.force, runner_command=args.runner_command)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 4
    if command == "skill-status":
        parser = argparse.ArgumentParser(prog="qa-aist-hermes skill-status", description="Check QA-AIST Hermes skill installation")
        parser.add_argument("--skills-dir", default=None, help="Hermes skills directory; defaults to $HERMES_HOME/skills or ~/.hermes/skills")
        args = parser.parse_args(argv[1:])
        payload = skill_status(args.skills_dir)
        print(json_dumps(payload))
        return 0 if payload["status"] == "ok" else 2
    parser = argparse.ArgumentParser(prog="qa-aist-hermes status", description="Check QA-AIST Hermes agent installation")
    parser.add_argument("--agent-dir", required=True, help="Hermes agents directory")
    args = parser.parse_args(argv[1:])
    payload = agent_status(args.agent_dir)
    print(json_dumps(payload))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
