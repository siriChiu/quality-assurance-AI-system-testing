from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from quality_pilot import hermes


class HermesDispatchTest(unittest.TestCase):
    def test_help_command_returns_traditional_chinese_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = hermes.dispatch_chat_command("/quality-pilot help", root=tmp)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["payload"]["topic"], "overview")
            self.assertEqual(result["payload"]["language"], "zh-Hant")
            self.assertIn("AI Quality Pilot 中文使用手冊", result["chat_response"])
            self.assertIn("/quality-pilot setup", result["chat_response"])
            self.assertIn("/quality-pilot cases list", result["chat_response"])
            self.assertIn("/quality-pilot cases run <case_id>", result["chat_response"])
            self.assertNotIn("/quality-pilot qa-test list", result["chat_response"])

    def test_removed_help_topics_and_qa_test_commands_return_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for command in ["/quality-pilot help qa-test", "/quality-pilot qa-test help"]:
                with self.subTest(command=command):
                    result = hermes.dispatch_chat_command(command, root=tmp)
                    self.assertEqual(result["exit_code"], 2)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["payload"]["error"], "command_removed")
                    self.assertIn("replacement", result["payload"])

    def test_redmine_missing_snapshot_retry_keeps_current_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes.dispatch_chat_command("/quality-pilot setup", root=root)

            sync = hermes.dispatch_chat_command("/quality-pilot issues sync --redmine-issues 144732 144694 144693 144780", root=root)
            self.assertEqual(sync["exit_code"], 2)
            self.assertEqual(sync["payload"]["error"], "RedmineError")
            self.assertEqual(
                sync["payload"]["next_actions"][0]["command"],
                "/quality-pilot issues sync --redmine-issues 144732 144694 144693 144780",
            )
            self.assertIn("重跑 sync", sync["payload"]["next_actions"][0]["label"])
            self.assertNotIn("cases generate", sync["payload"]["next_actions"][0]["command"])

            generate = hermes.dispatch_chat_command("/quality-pilot cases generate --redmine-issues 144732 144694", root=root)
            self.assertEqual(generate["exit_code"], 2)
            self.assertEqual(
                generate["payload"]["next_actions"][0]["command"],
                "/quality-pilot cases generate --redmine-issues 144732 144694",
            )
            self.assertIn("重跑 generate", generate["payload"]["next_actions"][0]["label"])

    def test_quick_start_chat_commands_dispatch_to_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            setup = hermes.dispatch_chat_command("/quality-pilot setup", root=root)
            self.assertEqual(setup["exit_code"], 0)
            self.assertEqual(setup["status"], "ok")
            self.assertIn("--root", setup["engine_argv"])
            self.assertTrue((root / ".quality-pilot.yaml").exists())
            self.assertTrue((root / ".quality-pilot-project" / "cases" / "example-contract.yaml").exists())

            doctor = hermes.dispatch_chat_command("/quality-pilot doctor", root=root)
            self.assertEqual(doctor["status"], "WARN")
            self.assertIn("Hermes MCP server list was not provided", doctor["chat_response"])
            self.assertIn("hermes_mcp_status_unknown", doctor["payload"]["hermes_mcp"]["blockers"])
            self.assertIn("下一步可以選", doctor["chat_response"])

            listed = hermes.dispatch_chat_command("/quality-pilot cases list", root=root)
            self.assertEqual(listed["status"], "ok")
            self.assertEqual(listed["payload"]["cases"][0]["case_id"], "EXAMPLE-001")

            run_one = hermes.dispatch_chat_command("/quality-pilot cases run EXAMPLE-001", root=root)
            self.assertEqual(run_one["status"], "PASS")
            self.assertIn("result:", run_one["chat_response"])

            close_loop = hermes.dispatch_chat_command("/quality-pilot close-loop run-once", root=root)
            self.assertEqual(close_loop["status"], "PASS")
            self.assertIn("latest_run_json", close_loop["payload"])
            self.assertIn("report_path", close_loop["payload"])

            report = hermes.dispatch_chat_command("/quality-pilot report status", root=root)
            self.assertEqual(report["status"], "ok")
            self.assertTrue((root / ".quality-pilot-project" / "reports" / "status.md").exists())

            plan = hermes.dispatch_chat_command("/quality-pilot tracker plan-write", root=root)
            self.assertEqual(plan["status"], "ok")
            self.assertEqual(plan["payload"]["write_gate_result"]["reason"], "allowed")

    def test_command_cheat_sheet_chat_commands_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes.dispatch_chat_command("/quality-pilot setup", root=root)

            commands = [
                "/quality-pilot help",
                "/quality-pilot doctor",
                "/quality-pilot issues status",
                "/quality-pilot cases list",
                "/quality-pilot cases validate",
                "/quality-pilot cases run",
                "/quality-pilot cases run EXAMPLE-001",
                "/quality-pilot close-loop status",
                "/quality-pilot close-loop run-once",
                "/quality-pilot report status",
                "/quality-pilot report json",
                "/quality-pilot tracker plan-write",
            ]
            for command in commands:
                with self.subTest(command=command):
                    result = hermes.dispatch_chat_command(command, root=root)
                    self.assertEqual(result["exit_code"], 0)
                    self.assertNotEqual(result["status"], "error")
                    self.assertTrue(result["engine_argv"])
                    self.assertIn("payload", result)

    def test_alias_is_accepted_but_other_chat_text_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes.dispatch_chat_command("/quality-pilot setup", root=root)

            alias = hermes.dispatch_chat_command("quality-pilot doctor", root=root)
            self.assertEqual(alias["exit_code"], 0)
            self.assertEqual(alias["prefix"], "quality-pilot")

            rejected = hermes.dispatch_chat_command("status", root=root)
            self.assertEqual(rejected["exit_code"], 2)
            self.assertEqual(rejected["status"], "error")
            self.assertEqual(rejected["payload"]["error"], "not_a_quality_pilot_command")

            unsupported = hermes.dispatch_chat_command("/quality-pilot rm -rf .", root=root)
            self.assertNotEqual(unsupported["exit_code"], 0)
            self.assertEqual(unsupported["status"], "error")
            self.assertEqual(unsupported["payload"]["error"], "argument_error")

    def test_console_entrypoint_emits_hermes_dispatch_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes.dispatch_chat_command("/quality-pilot setup", root=tmp)
            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["--root", tmp, "/quality-pilot", "doctor"])
            payload = json.loads(buf.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["interface"], "hermes")
            self.assertEqual(payload["command"], "/quality-pilot doctor")
            self.assertEqual(payload["payload"]["tool"], "quality-pilot")

            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["--root", tmp, "/quality-pilot", "cases", "generate", "--init", "--feature", "CLI help", "--profile", "cli", "--count", "1"])
            payload = json.loads(buf.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["payload"]["source"], "init")
            self.assertEqual(payload["payload"]["feature"], "CLI help")

    def test_agent_manifest_install_status_and_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as agent_tmp, tempfile.TemporaryDirectory() as project_tmp:
            manifest = hermes.build_agent_manifest()
            self.assertEqual(manifest["command_prefix"], "/quality-pilot")
            self.assertIn("quality-pilot", manifest["aliases"])
            self.assertIn("/quality-pilot help", manifest["commands"])
            self.assertIn("/quality-pilot cases generate --init", manifest["commands"])
            self.assertIn("/quality-pilot cases generate --init --count 5", manifest["commands"])
            self.assertIn("/quality-pilot cases generate --growing", manifest["commands"])
            self.assertIn("/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]", manifest["commands"])
            self.assertIn("/quality-pilot cases run <case_id>", manifest["commands"])
            self.assertNotIn("/quality-pilot qa-test help", manifest["commands"])
            self.assertEqual(manifest["permissions"]["tracker_write"], "write_gate_apply_only")
            self.assertIn("gitea_mcp_read_and_gated_wiki_write_when_configured", manifest["permissions"]["network"])
            self.assertIn("gitea_mcp_gated_issue_create_from_redmine_sync", manifest["permissions"]["network"])
            self.assertEqual(manifest["outputs"]["needs_input_field"], "payload.hermes_needs_input")
            self.assertEqual(manifest["outputs"]["interaction_style"], "guided_menu_with_needs_input")

            installed = hermes.install_agent(agent_tmp, runner_command=f"{os.sys.executable} -m quality_pilot.hermes")
            self.assertEqual(installed["status"], "ok")
            manifest_path = Path(installed["manifest_path"])
            wrapper_path = Path(installed["wrapper_path"])
            self.assertTrue(manifest_path.exists())
            self.assertTrue(wrapper_path.exists())
            self.assertTrue(os.access(wrapper_path, os.X_OK))

            status = hermes.agent_status(agent_tmp)
            self.assertEqual(status["status"], "ok")
            self.assertTrue(status["manifest_valid"])

            duplicate = hermes.install_agent(agent_tmp, runner_command=f"{os.sys.executable} -m quality_pilot.hermes")
            self.assertEqual(duplicate["status"], "error")
            self.assertEqual(duplicate["error"], "agent_files_exist")

            hermes.dispatch_chat_command("/quality-pilot setup", root=project_tmp)
            env = os.environ.copy()
            env["PYTHONPATH"] = "src"
            env["HERMES_PROJECT_ROOT"] = project_tmp
            completed = subprocess.run(
                [str(wrapper_path), "/quality-pilot", "doctor"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["interface"], "hermes")
            self.assertEqual(payload["payload"]["tool"], "quality-pilot")

    def test_agent_console_commands(self) -> None:
        with tempfile.TemporaryDirectory() as agent_tmp:
            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["manifest"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["command_prefix"], "/quality-pilot")

            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["install", "--agent-dir", agent_tmp, "--runner-command", f"{os.sys.executable} -m quality_pilot.hermes"])
            self.assertEqual(code, 0)
            installed = json.loads(buf.getvalue())
            self.assertEqual(installed["status"], "ok")

            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["status", "--agent-dir", agent_tmp])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "ok")

    def test_install_skill_creates_hermes_skill_slash_command(self) -> None:
        with tempfile.TemporaryDirectory() as skills_tmp:
            payload = hermes.install_skill(
                skills_tmp,
                runner_command="/usr/bin/env PYTHONPATH=/repo/AI Quality Pilot/src python3 -m quality_pilot.hermes",
            )
            self.assertEqual(payload["status"], "ok")
            skill_path = Path(payload["skill_path"])
            self.assertTrue(skill_path.exists())
            text = skill_path.read_text(encoding="utf-8")
            self.assertIn("name: quality-pilot", text)
            self.assertIn("AI Quality Pilot Hermes Skill", text)
            self.assertIn("skill-mediated", text)
            self.assertIn("not a native Hermes router", text)
            self.assertIn("Do not answer from memory", text)
            self.assertIn("Gitea MCP snapshot workflow", text)
            self.assertIn("Gitea MCP Wiki write workflow", text)
            self.assertIn("Gitea MCP Redmine issue creation workflow", text)
            self.assertIn("needs_mcp_apply", text)
            self.assertNotIn("/quality-pilot publish wiki complete-mcp --result-json <path>", text)
            self.assertIn("chat_response", text)
            self.assertIn("call Hermes `clarify`", text)
            self.assertIn("payload.hermes_needs_input", text)
            self.assertNotIn("Hermes needs your input", text)
            self.assertIn("product repository root", text)
            self.assertIn("/quality-pilot help", text)
            self.assertNotIn("/quality-pilot help qa-test", text)
            self.assertIn("/quality-pilot cases generate --init", text)
            self.assertIn("/quality-pilot subagent status", text)
            self.assertIn("/quality-pilot subagent configure", text)
            self.assertIn("https://172.17.20.220/", text)
            self.assertIn("candidate-only", text)
            self.assertIn("opinionated SWQA engineer", text)
            self.assertIn("executable safe-probe cases", text)
            self.assertIn("Every INIT case must have `commands[].run`", text)
            self.assertIn("--count 5", text)
            self.assertNotIn("--generated_count 5", text)
            self.assertNotIn("--fast", text)
            self.assertIn("/usr/bin/env PYTHONPATH=/repo/AI Quality Pilot/src python3 -m quality_pilot.hermes", text)
            reference_path = Path(payload["reference_path"])
            self.assertTrue(reference_path.exists())
            reference_text = reference_path.read_text(encoding="utf-8")
            self.assertIn("MCP issue list may include PRs", reference_text)
            self.assertIn("/quality-pilot issues sync", reference_text)
            self.assertIn("Gitea MCP may create new issues only after", reference_text)

            status = hermes.skill_status(skills_tmp)
            self.assertEqual(status["status"], "ok")
            self.assertTrue(status["skill_valid"])

            duplicate = hermes.install_skill(skills_tmp)
            self.assertEqual(duplicate["status"], "error")
            self.assertEqual(duplicate["error"], "skill_exists")

    def test_skill_console_commands(self) -> None:
        with tempfile.TemporaryDirectory() as skills_tmp:
            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main([
                    "install-skill",
                    "--skills-dir",
                    skills_tmp,
                    "--runner-command",
                    "quality-pilot-hermes",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["command_prefix"], "/quality-pilot")

            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["skill-status", "--skills-dir", skills_tmp])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "ok")


if __name__ == "__main__":
    unittest.main()
