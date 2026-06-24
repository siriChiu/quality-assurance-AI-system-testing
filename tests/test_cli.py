from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from quality_pilot import cli


class CliTest(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        buf = StringIO()
        with redirect_stdout(buf):
            code = cli.main(args)
        return code, json.loads(buf.getvalue())

    def test_setup_creates_host_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_cli(["setup", "--root", tmp])
            self.assertEqual(code, 0)
            root = Path(tmp)
            self.assertTrue((root / ".quality-pilot.yaml").exists())
            self.assertTrue((root / ".quality-pilot-project" / "cases" / "example-contract.yaml").exists())
            self.assertTrue((root / ".quality-pilot-project" / "runners" / "example-runner.sh").exists())
            swqa_rule = root / ".quality-pilot-project" / "rules" / "swqa-test-design.md"
            self.assertTrue(swqa_rule.exists())
            rule_text = swqa_rule.read_text(encoding="utf-8")
            self.assertIn("CLI argument-order matrix", rule_text)
            self.assertIn("Boundary and invalid-value tests", rule_text)
            self.assertFalse((root / ".quality-pilot" / "cases").exists())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(Path(payload["workspace"]).name, ".quality-pilot-project")
            self.assertIn("workspace: .quality-pilot-project", (root / ".quality-pilot.yaml").read_text(encoding="utf-8"))
            self.assertEqual(payload["tracker_setup"]["provider"], "hermes_mcp")
            config = (root / ".quality-pilot.yaml").read_text(encoding="utf-8")
            self.assertNotIn("QUALITY_PILOT_" + "GITEA_TOKEN", config)
            self.assertNotIn("QUALITY_PILOT_" + "TRACKER_TOKEN", config)
            self.assertNotIn("api_token_env", config)
            self.assertNotIn("token_env", config)
            self.assertNotIn("  gitea:", config)
            self.assertIn("subagents:", config)
            self.assertIn("provider: open_webui", config)
            self.assertIn('endpoint: "https://172.17.20.220/"', config)
            self.assertIn("gitea_issue_body: open-webui", config)
            self.assertIn('user_instructions: ""', config)

    def test_setup_auto_configures_gitea_mcp_from_git_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", "git@git.sw.ciot.work:Redfish/irctool.git"], cwd=root, check=True)

            code, payload = self.run_cli(["setup", "--root", tmp])

            self.assertEqual(code, 0)
            self.assertEqual(payload["tracker_setup"]["provider"], "hermes_mcp")
            self.assertEqual(payload["tracker_setup"]["backend"], "mcp")
            self.assertEqual(payload["tracker_setup"]["git_remote_base_url_detected"], "https://git.sw.ciot.work")
            self.assertEqual(payload["tracker_setup"]["git_remote_repo_detected"], "Redfish/irctool")
            config = (root / ".quality-pilot.yaml").read_text(encoding="utf-8")
            self.assertIn("provider: hermes_mcp", config)
            self.assertIn("gitea_issues_json: .quality-pilot-project/state/gitea-mcp/issues.json", config)
            self.assertNotIn("base_url:", config)
            self.assertNotIn("    repo:", config)
            self.assertNotIn("  gitea:", config)

    def test_setup_legacy_gitea_flags_do_not_write_tracker_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            code, payload = self.run_cli([
                "setup",
                "--root",
                tmp,
                "--tracker-provider",
                "gitea",
                "--gitea-backend",
                "http",
                "--gitea-base-url",
                "https://git.example.test",
                "--gitea-repo",
                "owner/repo",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["tracker_setup"]["provider"], "hermes_mcp")
            self.assertEqual(payload["tracker_setup"]["backend"], "mcp")
            self.assertEqual(payload["tracker_setup"]["git_remote_base_url_detected"], "https://git.example.test")
            self.assertEqual(payload["tracker_setup"]["git_remote_repo_detected"], "owner/repo")
            config = (root / ".quality-pilot.yaml").read_text(encoding="utf-8")
            self.assertIn("provider: hermes_mcp", config)
            self.assertNotIn("backend: http", config)
            self.assertNotIn("base_url:", config)
            self.assertNotIn("    repo:", config)
            self.assertNotIn("token_env", config)

    def test_setup_refuses_to_write_into_tool_checkout_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_checkout = root / ".quality-pilot"
            (tool_checkout / "src" / "quality_pilot").mkdir(parents=True)
            (tool_checkout / "pyproject.toml").write_text('[project]\nname = "quality-pilot"\n', encoding="utf-8")
            (tool_checkout / "src" / "quality_pilot" / "cli.py").write_text("", encoding="utf-8")

            code, payload = self.run_cli(["setup", "--root", tmp, "--workspace", ".quality-pilot"])

            self.assertEqual(code, 4)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "workspace_is_tool_checkout")
            self.assertFalse((tool_checkout / "cases").exists())
            self.assertFalse((root / ".quality-pilot.yaml").exists())

    def test_setup_uses_safe_default_when_tool_checkout_is_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_checkout = root / ".quality-pilot"
            (tool_checkout / "src" / "quality_pilot").mkdir(parents=True)
            (tool_checkout / "pyproject.toml").write_text('[project]\nname = "quality-pilot"\n', encoding="utf-8")

            code, payload = self.run_cli(["setup", "--root", tmp])

            self.assertEqual(code, 0)
            self.assertEqual(Path(payload["workspace"]).name, ".quality-pilot-project")
            self.assertTrue((root / ".quality-pilot-project" / "cases" / "example-contract.yaml").exists())
            self.assertFalse((tool_checkout / "cases").exists())

    def test_doctor_reports_config_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])
            code, payload = self.run_cli(["doctor", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["tool"], "quality-pilot")
            self.assertTrue(any(check["name"] == "config" for check in payload["checks"]))
            self.assertIn("hermes_mcp", payload)
            self.assertIn("issue_sync", payload)
            self.assertEqual(payload["subagents"]["endpoint"], "https://172.17.20.220/")

    def test_subagent_status_and_configure_use_open_webui_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])

            code, status = self.run_cli(["subagent", "status", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(status["subagents"]["provider"], "open_webui")
            self.assertEqual(status["subagents"]["endpoint"], "https://172.17.20.220/")
            self.assertIn("gitea_issue_body", status["subagents"]["tasks"])
            self.assertIn("user_instructions", status["subagents"]["missing_user_fields"])

            config_path = Path(tmp) / ".quality-pilot.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_path.write_text(config_text.split("\nsubagents:", 1)[0] + "\npolicy:" + config_text.split("\npolicy:", 1)[1], encoding="utf-8")

            code, configured = self.run_cli(["subagent", "configure", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(configured["subagents"]["provider"], "open_webui")
            self.assertEqual(configured["subagents"]["endpoint"], "https://172.17.20.220/")
            updated = config_path.read_text(encoding="utf-8")
            self.assertIn("subagents:", updated)
            self.assertIn("system_prompt: ''", updated)

    def test_removed_status_and_config_commands_return_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])
            for args, replacement in [
                (["status", "--root", tmp, "--json"], "/quality-pilot doctor"),
                (["config", "validate", "--config", str(Path(tmp) / ".quality-pilot.yaml"), "--json"], "/quality-pilot doctor"),
            ]:
                with self.subTest(args=args):
                    code, payload = self.run_cli(args)
                    self.assertEqual(code, 2)
                    self.assertEqual(payload["error"], "command_removed")
                    self.assertEqual(payload["replacement"], replacement)

    def test_cases_list_and_run_with_json_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])
            code, payload = self.run_cli(["cases", "list", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["cases"][0]["case_id"], "EXAMPLE-001")

            code, payload = self.run_cli(["cases", "run", "--root", tmp, "--json", "EXAMPLE-001"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["results"][0]["case_id"], "EXAMPLE-001")
            self.assertIn("contract_hash", payload["results"][0])

    def test_close_loop_and_tracker_plan_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])
            code, payload = self.run_cli(["close-loop", "run-once", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertIn("latest_run_json", payload)
            self.assertEqual(payload["tracker_writes"]["blocked_by_gate"], 0)
            self.assertIn("issues_sync_readiness", [step["name"] for step in payload["steps"]])
            self.assertIn("publish_wiki_status", [step["name"] for step in payload["steps"]])

            code, payload = self.run_cli(["tracker", "plan-write", "--root", tmp, "--target-state", "closed", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["write_gate_result"]["reason"], "closed_issue_write_forbidden")


if __name__ == "__main__":
    unittest.main()
