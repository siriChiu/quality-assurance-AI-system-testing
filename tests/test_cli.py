from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from qa_aist import cli


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
            self.assertTrue((root / ".qa-aist.yaml").exists())
            self.assertTrue((root / ".qa-aist-project" / "cases" / "example-contract.yaml").exists())
            self.assertTrue((root / ".qa-aist-project" / "runners" / "example-runner.sh").exists())
            swqa_rule = root / ".qa-aist-project" / "rules" / "swqa-test-design.md"
            self.assertTrue(swqa_rule.exists())
            rule_text = swqa_rule.read_text(encoding="utf-8")
            self.assertIn("CLI argument-order matrix", rule_text)
            self.assertIn("Boundary and invalid-value tests", rule_text)
            self.assertFalse((root / ".qa-aist" / "cases").exists())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(Path(payload["workspace"]).name, ".qa-aist-project")
            self.assertIn("workspace: .qa-aist-project", (root / ".qa-aist.yaml").read_text(encoding="utf-8"))
            self.assertEqual(payload["tracker_setup"]["provider"], "hermes_mcp")
            config = (root / ".qa-aist.yaml").read_text(encoding="utf-8")
            self.assertNotIn("QA_AIST_" + "GITEA_TOKEN", config)
            self.assertNotIn("QA_AIST_" + "TRACKER_TOKEN", config)
            self.assertNotIn("api_token_env", config)
            self.assertNotIn("token_env", config)
            self.assertNotIn("  gitea:", config)

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
            config = (root / ".qa-aist.yaml").read_text(encoding="utf-8")
            self.assertIn("provider: hermes_mcp", config)
            self.assertIn("gitea_issues_json: .qa-aist-project/state/gitea-mcp/issues.json", config)
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
            config = (root / ".qa-aist.yaml").read_text(encoding="utf-8")
            self.assertIn("provider: hermes_mcp", config)
            self.assertNotIn("backend: http", config)
            self.assertNotIn("base_url:", config)
            self.assertNotIn("    repo:", config)
            self.assertNotIn("token_env", config)

    def test_setup_refuses_to_write_into_tool_checkout_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_checkout = root / ".qa-aist"
            (tool_checkout / "src" / "qa_aist").mkdir(parents=True)
            (tool_checkout / "pyproject.toml").write_text('[project]\nname = "qa-aist"\n', encoding="utf-8")
            (tool_checkout / "src" / "qa_aist" / "cli.py").write_text("", encoding="utf-8")

            code, payload = self.run_cli(["setup", "--root", tmp, "--workspace", ".qa-aist"])

            self.assertEqual(code, 4)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "workspace_is_tool_checkout")
            self.assertFalse((tool_checkout / "cases").exists())
            self.assertFalse((root / ".qa-aist.yaml").exists())

    def test_setup_uses_safe_default_when_tool_checkout_is_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_checkout = root / ".qa-aist"
            (tool_checkout / "src" / "qa_aist").mkdir(parents=True)
            (tool_checkout / "pyproject.toml").write_text('[project]\nname = "qa-aist"\n', encoding="utf-8")

            code, payload = self.run_cli(["setup", "--root", tmp])

            self.assertEqual(code, 0)
            self.assertEqual(Path(payload["workspace"]).name, ".qa-aist-project")
            self.assertTrue((root / ".qa-aist-project" / "cases" / "example-contract.yaml").exists())
            self.assertFalse((tool_checkout / "cases").exists())

    def test_doctor_reports_config_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])
            code, payload = self.run_cli(["doctor", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["tool"], "qa-aist")
            self.assertTrue(any(check["name"] == "config" for check in payload["checks"]))
            self.assertIn("hermes_mcp", payload)
            self.assertIn("issue_sync", payload)

    def test_removed_status_and_config_commands_return_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])
            for args, replacement in [
                (["status", "--root", tmp, "--json"], "/qa-aist doctor"),
                (["config", "validate", "--config", str(Path(tmp) / ".qa-aist.yaml"), "--json"], "/qa-aist doctor"),
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
