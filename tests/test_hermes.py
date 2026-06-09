from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from qa_aist import hermes


class HermesDispatchTest(unittest.TestCase):
    def test_help_command_returns_traditional_chinese_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = hermes.dispatch_chat_command("/qa-aist help", root=tmp)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["payload"]["topic"], "overview")
            self.assertEqual(result["payload"]["language"], "zh-Hant")
            self.assertIn("QA-AIST 中文使用手冊", result["chat_response"])
            self.assertIn("/qa-aist setup", result["chat_response"])
            self.assertIn("/qa-aist qa-test list", result["chat_response"])
            self.assertIn("/qa-aist help qa-test", result["chat_response"])

    def test_qa_test_help_explains_case_contract_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for command in ["/qa-aist help qa-test", "/qa-aist qa-test help"]:
                with self.subTest(command=command):
                    result = hermes.dispatch_chat_command(command, root=tmp)
                    self.assertEqual(result["exit_code"], 0)
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["payload"]["topic"], "qa-test")
                    self.assertEqual(result["payload"]["language"], "zh-Hant")
                    self.assertIn("qa-test 是什麼", result["chat_response"])
                    self.assertIn(".qa-aist-project/cases/*.yaml", result["chat_response"])
                    self.assertIn("case_id", result["chat_response"])
                    self.assertIn("dry-run", result["chat_response"])
                    self.assertIn("/qa-aist qa-test run-one EXAMPLE-001", result["chat_response"])

    def test_quick_start_chat_commands_dispatch_to_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            setup = hermes.dispatch_chat_command("/qa-aist setup", root=root)
            self.assertEqual(setup["exit_code"], 0)
            self.assertEqual(setup["status"], "ok")
            self.assertIn("--root", setup["engine_argv"])
            self.assertTrue((root / ".qa-aist.yaml").exists())
            self.assertTrue((root / ".qa-aist-project" / "cases" / "example-contract.yaml").exists())

            doctor = hermes.dispatch_chat_command("/qa-aist doctor", root=root)
            self.assertEqual(doctor["status"], "WARN")
            self.assertIn("tracker_provider_disabled", doctor["chat_response"])
            self.assertIn("下一步可以選", doctor["chat_response"])

            listed = hermes.dispatch_chat_command("/qa-aist qa-test list", root=root)
            self.assertEqual(listed["status"], "ok")
            self.assertEqual(listed["payload"]["cases"][0]["case_id"], "EXAMPLE-001")

            run_one = hermes.dispatch_chat_command("/qa-aist qa-test run-one EXAMPLE-001", root=root)
            self.assertEqual(run_one["status"], "PASS")
            self.assertIn("result:", run_one["chat_response"])

            close_loop = hermes.dispatch_chat_command("/qa-aist close-loop run-once", root=root)
            self.assertEqual(close_loop["status"], "PASS")
            self.assertIn("latest_run_json", close_loop["payload"])
            self.assertIn("report_path", close_loop["payload"])

            report = hermes.dispatch_chat_command("/qa-aist report status", root=root)
            self.assertEqual(report["status"], "ok")
            self.assertTrue((root / ".qa-aist-project" / "reports" / "status.md").exists())

            plan = hermes.dispatch_chat_command("/qa-aist tracker plan-write", root=root)
            self.assertEqual(plan["status"], "ok")
            self.assertEqual(plan["payload"]["write_gate_result"]["reason"], "tracker_disabled")

    def test_command_cheat_sheet_chat_commands_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes.dispatch_chat_command("/qa-aist setup", root=root)

            commands = [
                "/qa-aist help",
                "/qa-aist help qa-test",
                "/qa-aist status",
                "/qa-aist doctor",
                "/qa-aist config show",
                "/qa-aist config validate",
                "/qa-aist qa-test list",
                "/qa-aist qa-test validate",
                "/qa-aist qa-test dry-run",
                "/qa-aist qa-test help",
                "/qa-aist qa-test run",
                "/qa-aist qa-test run-one EXAMPLE-001",
                "/qa-aist close-loop status",
                "/qa-aist close-loop run-once",
                "/qa-aist report status",
                "/qa-aist report json",
                "/qa-aist tracker plan-write",
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
            hermes.dispatch_chat_command("/qa-aist setup", root=root)

            alias = hermes.dispatch_chat_command("qa-aist status", root=root)
            self.assertEqual(alias["exit_code"], 0)
            self.assertEqual(alias["prefix"], "qa-aist")

            rejected = hermes.dispatch_chat_command("status", root=root)
            self.assertEqual(rejected["exit_code"], 2)
            self.assertEqual(rejected["status"], "error")
            self.assertEqual(rejected["payload"]["error"], "not_a_qa_aist_command")

            unsupported = hermes.dispatch_chat_command("/qa-aist rm -rf .", root=root)
            self.assertNotEqual(unsupported["exit_code"], 0)
            self.assertEqual(unsupported["status"], "error")
            self.assertEqual(unsupported["payload"]["error"], "engine_output_not_json")

    def test_console_entrypoint_emits_hermes_dispatch_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes.dispatch_chat_command("/qa-aist setup", root=tmp)
            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["--root", tmp, "/qa-aist", "status"])
            payload = json.loads(buf.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["interface"], "hermes")
            self.assertEqual(payload["command"], "/qa-aist status")
            self.assertEqual(payload["payload"]["tool"], "qa-aist")

            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["--root", tmp, "/qa-aist", "cases", "generate", "--init", "--feature", "CLI help", "--profile", "cli", "--count", "1"])
            payload = json.loads(buf.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["payload"]["source"], "init")
            self.assertEqual(payload["payload"]["feature"], "CLI help")

    def test_agent_manifest_install_status_and_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as agent_tmp, tempfile.TemporaryDirectory() as project_tmp:
            manifest = hermes.build_agent_manifest()
            self.assertEqual(manifest["command_prefix"], "/qa-aist")
            self.assertIn("qa-aist", manifest["aliases"])
            self.assertIn("/qa-aist help", manifest["commands"])
            self.assertIn("/qa-aist help qa-test", manifest["commands"])
            self.assertIn("/qa-aist cases generate --init", manifest["commands"])
            self.assertIn("/qa-aist cases generate --growing", manifest["commands"])
            self.assertIn("/qa-aist qa-test help", manifest["commands"])
            self.assertEqual(manifest["permissions"]["tracker_write"], "write_gate_apply_only")
            self.assertEqual(manifest["outputs"]["needs_input_field"], "payload.hermes_needs_input")
            self.assertEqual(manifest["outputs"]["interaction_style"], "guided_menu_with_needs_input")

            installed = hermes.install_agent(agent_tmp, runner_command=f"{os.sys.executable} -m qa_aist.hermes")
            self.assertEqual(installed["status"], "ok")
            manifest_path = Path(installed["manifest_path"])
            wrapper_path = Path(installed["wrapper_path"])
            self.assertTrue(manifest_path.exists())
            self.assertTrue(wrapper_path.exists())
            self.assertTrue(os.access(wrapper_path, os.X_OK))

            status = hermes.agent_status(agent_tmp)
            self.assertEqual(status["status"], "ok")
            self.assertTrue(status["manifest_valid"])

            duplicate = hermes.install_agent(agent_tmp, runner_command=f"{os.sys.executable} -m qa_aist.hermes")
            self.assertEqual(duplicate["status"], "error")
            self.assertEqual(duplicate["error"], "agent_files_exist")

            hermes.dispatch_chat_command("/qa-aist setup", root=project_tmp)
            env = os.environ.copy()
            env["PYTHONPATH"] = "src"
            env["HERMES_PROJECT_ROOT"] = project_tmp
            completed = subprocess.run(
                [str(wrapper_path), "/qa-aist", "status"],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["interface"], "hermes")
            self.assertEqual(payload["payload"]["tool"], "qa-aist")

    def test_agent_console_commands(self) -> None:
        with tempfile.TemporaryDirectory() as agent_tmp:
            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["manifest"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["command_prefix"], "/qa-aist")

            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["install", "--agent-dir", agent_tmp, "--runner-command", f"{os.sys.executable} -m qa_aist.hermes"])
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
                runner_command="/usr/bin/env PYTHONPATH=/repo/QA-AIST/src python3 -m qa_aist.hermes",
            )
            self.assertEqual(payload["status"], "ok")
            skill_path = Path(payload["skill_path"])
            self.assertTrue(skill_path.exists())
            text = skill_path.read_text(encoding="utf-8")
            self.assertIn("name: qa-aist", text)
            self.assertIn("QA-AIST Hermes Skill", text)
            self.assertIn("skill-mediated", text)
            self.assertIn("not a native Hermes router", text)
            self.assertIn("Do not answer from memory", text)
            self.assertIn("Gitea MCP snapshot workflow", text)
            self.assertIn("chat_response", text)
            self.assertIn("call Hermes `clarify`", text)
            self.assertIn("payload.hermes_needs_input", text)
            self.assertNotIn("Hermes needs your input", text)
            self.assertIn("product repository root", text)
            self.assertIn("/qa-aist help", text)
            self.assertIn("/qa-aist help qa-test", text)
            self.assertIn("/qa-aist cases generate --init", text)
            self.assertIn("opinionated SWQA engineer", text)
            self.assertIn("initial test map", text)
            self.assertIn("candidate JSON", text)
            self.assertIn("/usr/bin/env PYTHONPATH=/repo/QA-AIST/src python3 -m qa_aist.hermes", text)
            reference_path = Path(payload["reference_path"])
            self.assertTrue(reference_path.exists())
            reference_text = reference_path.read_text(encoding="utf-8")
            self.assertIn("MCP issue list may include PRs", reference_text)
            self.assertIn("/qa-aist issues sync", reference_text)

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
                    "qa-aist-hermes",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["command_prefix"], "/qa-aist")

            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["skill-status", "--skills-dir", skills_tmp])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "ok")


if __name__ == "__main__":
    unittest.main()
