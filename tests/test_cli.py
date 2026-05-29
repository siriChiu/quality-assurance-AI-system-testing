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
            self.assertTrue((root / ".qa-aist" / "cases" / "example-contract.yaml").exists())
            self.assertTrue((root / ".qa-aist" / "runners" / "example-runner.sh").exists())
            self.assertEqual(payload["status"], "ok")

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
