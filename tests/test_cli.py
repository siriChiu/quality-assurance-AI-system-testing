from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
