from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from io import StringIO
from pathlib import Path

from quality_pilot import cli
from quality_pilot.config import ProjectConfig, project_paths
from quality_pilot.environment import environment_profile_status, remote_preflight


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

    def test_remote_fixture_is_not_checked_with_local_path_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, _ = self.run_cli(["setup", "--root", tmp])
            self.assertEqual(code, 0)
            code, payload = self.run_cli([
                "environment", "configure", "--root", tmp, "--mode", "remote",
                "--entrypoint", "/remote/bin/python -m app", "--ssh-host", "smartfan-x86-qa",
                "--remote-repo", "/root/siri/auto_PID_tool",
                "--remote-python", "/root/siri/auto_PID_tool/.venv/bin/python",
                "--remote-fixture", "/root/siri/auto_PID_tool/configs/config.yaml",
                "--auth-method", "ssh_agent", "--side-effect-boundary", "read-only",
            ])
            self.assertEqual(code, 0)
            profile = payload["environment_profile"]
            self.assertEqual(profile["status"], "REMOTE_PREFLIGHT_REQUIRED")
            self.assertIn("REMOTE_PREFLIGHT_REQUIRED", profile["blockers"])
            self.assertNotIn("fixture_missing:/root/siri/auto_PID_tool/configs/config.yaml", profile["blockers"])
            self.assertEqual(profile["fixtures"][0]["scope"], "remote")
            self.assertFalse(any(item["name"] == "QUALITY_PILOT_TEST_PASSWORD" and not item["present"] for item in profile["credentials"]))

    def test_failed_remote_preflight_does_not_make_profile_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {
                "runtime": {
                    "execution_mode": "remote",
                    "environment_confirmed": True,
                    "primary_entrypoint": ".venv/bin/python main.py --browser",
                    "side_effect_boundary": "read-only",
                    "ssh_host": "smartfan-x86-qa",
                    "remote_repo": "/root/siri/auto_PID_tool",
                    "remote_python": "/root/siri/auto_PID_tool/.venv/bin/python",
                    "remote_preflight": {"status": "TOOLING_FAIL"},
                }
            }
            config = ProjectConfig(root=root, path=root / ".quality-pilot.yaml", data=data, paths=project_paths(root))
            profile = environment_profile_status(config)
            self.assertFalse(profile["ready"])
            self.assertEqual(profile["status"], "TOOLING_FAIL")
            self.assertIn("remote_preflight:TOOLING_FAIL", profile["blockers"])

    def test_remote_preflight_records_independent_checks_and_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code, _ = self.run_cli(["setup", "--root", tmp])
            self.assertEqual(code, 0)
            code, _ = self.run_cli([
                "environment", "configure", "--root", tmp, "--mode", "remote",
                "--entrypoint", ".venv/bin/python main.py --browser",
                "--ssh-host", "smartfan-x86-qa", "--remote-repo", "/root/siri/auto_PID_tool",
                "--remote-python", "/root/siri/auto_PID_tool/.venv/bin/python",
                "--auth-method", "ssh_agent", "--side-effect-boundary", "read-only",
            ])
            self.assertEqual(code, 0)

            class Completed:
                returncode = 0
                stdout = "abcdef1234567890\n"
                stderr = ""

            def fake_run(argv, **kwargs):
                command = str(argv[-1])
                if "status --porcelain" in command:
                    value = Completed()
                    value.stdout = ""
                    return value
                return Completed()

            config = __import__("quality_pilot.config", fromlist=["load_project_config"]).load_project_config(root)
            with patch("quality_pilot.environment.subprocess.run", side_effect=fake_run):
                result = remote_preflight(config, expected_head_sha="abcdef1234567890")
            self.assertEqual(result["status"], "READY")
            self.assertEqual(result["checks"]["ssh"], "PASS")
            self.assertEqual(result["checks"]["remote_requirements"], "PASS")
            self.assertEqual(result["source_identity"]["status"], "VERIFIED")
            self.assertTrue(Path(result["evidence_path"]).exists() if Path(result["evidence_path"]).is_absolute() else (root / result["evidence_path"]).exists())

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
