from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality_pilot.config import ProjectConfig, project_paths
from quality_pilot.product_testing import (
    extract_readme_commands,
    resolve_product_test_plan,
    run_product_tests,
    validate_product_command,
)


class ProductTestingTest(unittest.TestCase):
    def _config(self, root: Path, settings: dict[str, object]) -> ProjectConfig:
        return ProjectConfig(
            root=root,
            path=root / ".quality-pilot.yaml",
            data={
                "runtime": {
                    "execution_mode": "local",
                    "environment_confirmed": True,
                    "primary_entrypoint": "python3",
                    "side_effect_boundary": "disposable sandbox",
                    "fixture_paths": [],
                    "credential_envs": [],
                    "product_testing": settings,
                }
            },
            paths=project_paths(root),
        )

    def _profile(self) -> dict[str, object]:
        return {"ready": True, "configured": {"credential_envs": []}, "blockers": []}

    def test_real_build_and_semantic_operation_pass_in_disposable_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build.py").write_text(
                "from pathlib import Path\nPath('bin').mkdir()\nPath('bin/product.py').write_text(\"print('PRODUCT_OK')\\n\")\n",
                encoding="utf-8",
            )
            (root / "run.py").write_text("print('PRODUCT_OK')\n", encoding="utf-8")
            config = self._config(
                root,
                {
                    "enabled": True,
                    "build_recipe": ["python3 build.py"],
                    "artifact_path": "bin/product.py",
                    "run_operations": [
                        {
                            "id": "smoke",
                            "command": "python3 run.py",
                            "assertion_type": "SEMANTIC",
                            "assertions": [{"type": "stdout_contains", "expected": "PRODUCT_OK"}],
                        }
                    ],
                },
            )
            result = run_product_tests(
                config,
                worktree=root,
                snapshot={"base_sha": "base", "head_sha": "head"},
                review_id="review-1",
                evidence_dir=root / ".quality-pilot-project" / "evidence" / "review-1",
                environment_profile=self._profile(),
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["run_operations"][0]["status"], "PASS")
            self.assertTrue(result["build"]["artifact"]["sha256"])
            self.assertFalse((root / "bin" / "product.py").exists())
            result_path = root / result["result_path"]
            self.assertTrue(result_path.exists())
            self.assertEqual(json.loads(result_path.read_text())["status"], "PASS")

    def test_remote_browser_plan_does_not_require_local_build_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root, {
                "enabled": True,
                "web_ui": {
                    "enabled": True,
                    "start_command": ".venv/bin/python main.py --browser",
                    "url_discovery": "stdout",
                    "url_pattern": r"https?://[^\\s]+",
                    "steps": [{"action": "expect_visible", "selector": "body"}],
                },
            })
            plan = resolve_product_test_plan(
                config,
                worktree=root,
                snapshot={"head_sha": "head"},
                execution_contract={"execution": {"product_target": "remote_ssh"}},
            )
            self.assertEqual(plan["status"], "READY")
            self.assertFalse(plan["build_required"])
            self.assertEqual(plan["build_recipe"], [])
            self.assertEqual(plan["artifact_path"], "")

    def test_remote_browser_runs_even_when_local_product_build_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root, {
                "enabled": True,
                "web_ui": {
                    "enabled": True,
                    "start_command": ".venv/bin/python main.py --browser",
                    "url_discovery": "stdout",
                    "url_pattern": r"https?://[^\\s]+",
                    "steps": [{"action": "expect_visible", "selector": "body"}],
                },
            })
            config.data["runtime"]["execution_mode"] = "remote"
            profile = {
                "ready": True,
                "execution_mode": "remote",
                "configured": {"ssh_host": "smartfan-x86-qa", "remote_repo": "/remote/repo", "remote_python": "/remote/python"},
                "remote_preflight": {"status": "READY", "source_identity": {"status": "VERIFIED"}},
                "blockers": [],
            }
            contract = {"contract_hash": "contract", "execution": {"product_target": "remote_ssh"}}
            with patch("quality_pilot.remote_browser_adapter.run_remote_browser_test", return_value={"status": "PASS", "evidence": {}, "evidence_origin": "remote"}) as remote_run:
                result = run_product_tests(
                    config,
                    worktree=root,
                    snapshot={"head_sha": "head"},
                    review_id="review-remote",
                    evidence_dir=root / "evidence",
                    environment_profile=profile,
                    product_settings=config.data["runtime"]["product_testing"],
                    execution_contract=contract,
                )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["build"]["status"], "NOT_RUN")
            self.assertEqual(result["browser"]["status"], "PASS")
            remote_run.assert_called_once()

    def test_probe_only_product_operation_is_hold_after_real_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build.py").write_text("from pathlib import Path; Path('artifact').write_text('ok')\n", encoding="utf-8")
            (root / "run.py").write_text("print('probe')\n", encoding="utf-8")
            config = self._config(
                root,
                {
                    "enabled": True,
                    "build_recipe": ["python3 build.py"],
                    "artifact_path": "artifact",
                    "run_operations": [
                        {
                            "id": "probe",
                            "command": "python3 run.py",
                            "assertion_type": "PROBE",
                            "assertions": [{"type": "exit_code", "expected": 0}],
                        }
                    ],
                },
            )
            result = run_product_tests(
                config,
                worktree=root,
                snapshot={"head_sha": "head"},
                review_id="review-2",
                evidence_dir=root / "evidence",
                environment_profile=self._profile(),
            )
            self.assertEqual(result["status"], "HOLD")
            self.assertEqual(result["reason"], "probe_only_no_semantic_assertion")
            self.assertEqual(result["run_operations"][0]["status"], "HOLD")

    def test_missing_contract_blocks_but_keeps_readme_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "```bash\npython3 build.py\n./bin/product --version\n```\n", encoding="utf-8"
            )
            config = self._config(root, {"enabled": True})
            result = run_product_tests(
                config,
                worktree=root,
                snapshot={"head_sha": "head"},
                review_id="review-3",
                evidence_dir=root / "evidence",
                environment_profile=self._profile(),
            )
            self.assertEqual(result["status"], "BLOCK")
            self.assertEqual(result["reason"], "build_recipe_missing")
            self.assertEqual(result["candidate_commands"], ["python3 build.py", "./bin/product --version"])

    def test_readme_allowlist_is_explicit_and_probe_remains_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "```bash\npython3 build.py\npython3 run.py\n```\n", encoding="utf-8"
            )
            config = self._config(
                root,
                {
                    "enabled": True,
                    "allow_readme_commands": True,
                    "readme_command_allowlist": [r"python3 build\.py", r"python3 run\.py"],
                    "artifact_path": "artifact",
                },
            )
            plan = resolve_product_test_plan(config, worktree=root, snapshot={"head_sha": "head"})
            self.assertEqual(plan["status"], "READY")
            self.assertEqual(plan["reason"], "probe_only_no_semantic_assertion")
            self.assertEqual(plan["build_recipe"], ["python3 build.py"])

    def test_unsafe_product_commands_are_rejected_without_execution(self) -> None:
        self.assertEqual(validate_product_command("make build && rm -rf /")["reason"], "shell_metacharacter")
        self.assertEqual(validate_product_command("curl https://example.invalid")["reason"], "network_or_destructive_executable")
        self.assertEqual(validate_product_command("sudo make build")["reason"], "network_or_destructive_executable")

    def test_missing_artifact_blocks_after_successful_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build.py").write_text("print('built')\n", encoding="utf-8")
            config = self._config(
                root,
                {
                    "enabled": True,
                    "build_recipe": ["python3 build.py"],
                    "artifact_path": "bin/missing",
                    "run_operations": [
                        {
                            "command": "python3 build.py",
                            "assertion_type": "SEMANTIC",
                            "assertions": [{"type": "stdout_contains", "expected": "built"}],
                        }
                    ],
                },
            )
            result = run_product_tests(
                config,
                worktree=root,
                snapshot={"head_sha": "head"},
                review_id="review-4",
                evidence_dir=root / "evidence",
                environment_profile=self._profile(),
            )
            self.assertEqual(result["status"], "BLOCK")
            self.assertEqual(result["reason"], "build_artifact_missing")

    def test_readme_candidate_extraction_never_includes_install_or_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "```bash\npip install -r requirements.txt\nchmod +x ./bin/product\nmake build\n./bin/product --version\n```\n",
                encoding="utf-8",
            )
            self.assertEqual(extract_readme_commands(root), ["make build", "./bin/product --version"])


if __name__ == "__main__":
    unittest.main()
