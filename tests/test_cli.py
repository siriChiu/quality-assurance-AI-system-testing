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

    def test_init_project_creates_host_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = self.run_cli(["init-project", "--root", tmp])
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
            self.assertEqual(payload["tracker_setup"]["provider"], "none")

    def test_init_project_auto_configures_gitea_mcp_from_git_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", "git@git.sw.ciot.work:Redfish/irctool.git"], cwd=root, check=True)

            code, payload = self.run_cli(["init-project", "--root", tmp])

            self.assertEqual(code, 0)
            self.assertEqual(payload["tracker_setup"]["provider"], "gitea")
            self.assertEqual(payload["tracker_setup"]["gitea_backend"], "mcp")
            self.assertEqual(payload["tracker_setup"]["gitea_base_url"], "https://git.sw.ciot.work")
            self.assertEqual(payload["tracker_setup"]["gitea_repo"], "Redfish/irctool")
            config = (root / ".qa-aist.yaml").read_text(encoding="utf-8")
            self.assertIn("provider: gitea", config)
            self.assertIn("backend: mcp", config)
            self.assertIn('base_url: "https://git.sw.ciot.work"', config)
            self.assertIn('repo: "Redfish/irctool"', config)

    def test_init_project_accepts_explicit_http_gitea_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            code, payload = self.run_cli([
                "init-project",
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
            self.assertEqual(payload["tracker_setup"]["provider"], "gitea")
            self.assertEqual(payload["tracker_setup"]["gitea_backend"], "http")
            config = (root / ".qa-aist.yaml").read_text(encoding="utf-8")
            self.assertIn("backend: http", config)
            self.assertIn('base_url: "https://git.example.test"', config)
            self.assertIn('repo: "owner/repo"', config)

    def test_init_project_refuses_to_write_into_tool_checkout_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_checkout = root / ".qa-aist"
            (tool_checkout / "src" / "qa_aist").mkdir(parents=True)
            (tool_checkout / "pyproject.toml").write_text('[project]\nname = "qa-aist"\n', encoding="utf-8")
            (tool_checkout / "src" / "qa_aist" / "cli.py").write_text("", encoding="utf-8")

            code, payload = self.run_cli(["init-project", "--root", tmp, "--workspace", ".qa-aist"])

            self.assertEqual(code, 4)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "workspace_is_tool_checkout")
            self.assertFalse((tool_checkout / "cases").exists())
            self.assertFalse((root / ".qa-aist.yaml").exists())

    def test_init_project_uses_safe_default_when_tool_checkout_is_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool_checkout = root / ".qa-aist"
            (tool_checkout / "src" / "qa_aist").mkdir(parents=True)
            (tool_checkout / "pyproject.toml").write_text('[project]\nname = "qa-aist"\n', encoding="utf-8")

            code, payload = self.run_cli(["init-project", "--root", tmp])

            self.assertEqual(code, 0)
            self.assertEqual(Path(payload["workspace"]).name, ".qa-aist-project")
            self.assertTrue((root / ".qa-aist-project" / "cases" / "example-contract.yaml").exists())
            self.assertFalse((tool_checkout / "cases").exists())

    def test_status_reports_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["init-project", "--root", tmp])
            code, payload = self.run_cli(["status", "--root", tmp])
            self.assertEqual(code, 0)
            self.assertTrue(payload["config_exists"])
            self.assertEqual(payload["case_contract_count"], 1)
            self.assertEqual(payload["runner_count"], 1)

    def test_config_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["init-project", "--root", tmp])
            code, payload = self.run_cli(["config", "validate", "--config", str(Path(tmp) / ".qa-aist.yaml")])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")

    def test_qa_test_list_and_run_with_json_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["init-project", "--root", tmp])
            code, payload = self.run_cli(["qa-test", "list", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["cases"][0]["case_id"], "EXAMPLE-001")

            code, payload = self.run_cli(["qa-test", "run-one", "--root", tmp, "--json", "EXAMPLE-001"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["results"][0]["case_id"], "EXAMPLE-001")
            self.assertIn("contract_hash", payload["results"][0])

    def test_close_loop_and_tracker_plan_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["init-project", "--root", tmp])
            code, payload = self.run_cli(["close-loop", "run-once", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertIn("latest_run_json", payload)
            self.assertGreaterEqual(payload["tracker_writes"]["blocked_by_gate"], 1)
            self.assertIn("tracker_pull_open_items", [step["name"] for step in payload["steps"]])
            self.assertIn("tracker_write_when_allowed", [step["name"] for step in payload["steps"]])

            code, payload = self.run_cli(["tracker", "plan-write", "--root", tmp, "--target-state", "closed", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["write_gate_result"]["reason"], "closed_issue_write_forbidden")


if __name__ == "__main__":
    unittest.main()
