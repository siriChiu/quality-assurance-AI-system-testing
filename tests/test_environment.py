from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from quality_pilot import cli


class EnvironmentProfileTest(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        output = StringIO()
        with redirect_stdout(output):
            code = cli.main([*args, "--json"])
        return code, json.loads(output.getvalue())

    def test_default_setup_requires_explicit_environment_before_prepared_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, _ = self.run_cli(["setup", "--root", tmp])
            self.assertEqual(code, 0)

            case_path = root / ".quality-pilot-project" / "cases" / "README-001.yaml"
            case_path.write_text(
                """case_id: README-001
title: README documented command
quality_pilot:
  requires_prepared_environment: true
  environment_requirements:
    - product binary
commands:
  - id: status
    run: ./missing-product status
    expected_exit_code: 0
""",
                encoding="utf-8",
            )

            code, payload = self.run_cli(["cases", "run", "--root", tmp])
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "BLOCK")
            result = next(item for item in payload["results"] if item["case_id"] == "README-001")
            self.assertEqual(result["blocked_reason"], "environment_profile_required")
            self.assertEqual(result["environment_profile"]["status"], "needs_user_input")

    def test_configure_remote_records_names_only_and_checks_target_presence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self.run_cli(["setup", "--root", tmp])
            self.assertEqual(code, 0)
            code, payload = self.run_cli(
                [
                    "environment",
                    "configure",
                    "--root",
                    tmp,
                    "--mode",
                    "remote",
                    "--entrypoint",
                    "./product",
                    "--target-host-env",
                    "QA_TARGET_HOST",
                    "--credential-env",
                    "QA_TEST_PASSWORD",
                    "--side-effect-boundary",
                    "isolated lab only",
                ]
            )
            self.assertEqual(code, 0)
            profile = payload["environment_profile"]
            self.assertEqual(profile["execution_mode"], "remote")
            self.assertIn("target_host_missing:QA_TARGET_HOST", profile["blockers"])
            config_text = (Path(tmp) / ".quality-pilot.yaml").read_text(encoding="utf-8")
            self.assertIn("QA_TARGET_HOST", config_text)
            self.assertNotIn("supersecret-value", config_text)
            self.assertFalse(profile["safety"]["raw_secret_values_stored"])

    def test_prepared_readme_command_not_found_is_block_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, _ = self.run_cli(["setup", "--root", tmp])
            self.assertEqual(code, 0)
            code, _ = self.run_cli(
                [
                    "environment",
                    "configure",
                    "--root",
                    tmp,
                    "--mode",
                    "local",
                    "--entrypoint",
                    "./missing-product",
                    "--side-effect-boundary",
                    "readonly checkout",
                ]
            )
            self.assertEqual(code, 0)
            case_path = root / ".quality-pilot-project" / "cases" / "README-002.yaml"
            case_path.write_text(
                """case_id: README-002
title: README command with missing executable
quality_pilot:
  safe_command_source_type: readme_cli_operation
  requires_prepared_environment: true
  environment_requirements:
    - product binary
commands:
  - id: status
    run: ./missing-product status
    expected_exit_code: 0
""",
                encoding="utf-8",
            )

            code, payload = self.run_cli(["cases", "run", "--root", tmp])
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "BLOCK")
            result = next(item for item in payload["results"] if item["case_id"] == "README-002")
            self.assertEqual(result["status"], "BLOCK")
            self.assertEqual(result["commands"][0]["blocked_reason"], "executable_not_found")
