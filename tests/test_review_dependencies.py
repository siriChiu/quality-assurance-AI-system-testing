from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality_pilot.config import ProjectConfig, project_paths
from quality_pilot.review import prepare_review_dependencies, run_selected_tests, select_applicable_tests


class ReviewDependencyPreparationTest(unittest.TestCase):
    def _config(self, root: Path) -> ProjectConfig:
        return ProjectConfig(root=root, path=root / ".quality-pilot.yaml", data={}, paths=project_paths(root))

    def test_dependencies_are_prepared_in_worktree_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "review-worktree"
            worktree.mkdir()
            (worktree / "requirements.txt").write_text("pytest\nplaywright\n", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(argv, **kwargs):
                calls.append(list(argv))
                if argv[1:3] == ["-m", "venv"]:
                    python = worktree / ".venv" / "bin" / "python"
                    python.parent.mkdir(parents=True, exist_ok=True)
                    python.write_text("#!/bin/sh\n", encoding="utf-8")
                    python.chmod(0o755)
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("quality_pilot.review.subprocess.run", side_effect=fake_run):
                result = prepare_review_dependencies(
                    self._config(root),
                    worktree=worktree,
                    python_executable="/usr/bin/python3",
                    environment_profile={"ready": False},
                    enabled=True,
                    timeout_seconds=30,
                    execution_contract={"execution": {"local_pytest": True}},
                )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["python"], ".venv/bin/python")
            self.assertEqual(result["execution_target"], "local_disposable_review_worktree")
            self.assertEqual(calls[0], [sys.executable, "-m", "venv", ".venv"])
            self.assertEqual(calls[1][:3], [".venv/bin/python", "-m", "pip"])
            self.assertEqual(calls[2], [".venv/bin/python", "-m", "playwright", "install", "chromium"])
            self.assertNotIn("/usr/bin/python3", calls[1])
            self.assertNotIn("/usr/bin/python3", calls[2])

    def test_selected_pytest_command_uses_worktree_relative_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_pid.py").write_text("def test_pid():\n    assert True\n", encoding="utf-8")
            selection = select_applicable_tests(
                str(root),
                [{"path": "application/pid.py"}],
                python_executable=".venv/bin/python",
            )
            self.assertTrue(selection["selected"])
            self.assertTrue(all(item["command"].startswith(".venv/bin/python -m pytest") for item in selection["selected"]))

    def test_missing_review_venv_is_a_block_not_a_runner_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "review-worktree"
            (worktree / "tests").mkdir(parents=True)
            (worktree / "tests" / "test_one.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
            result = run_selected_tests(
                [{"id": "pytest", "command": ".venv/bin/python -m pytest tests/test_one.py -q"}],
                str(worktree),
                self._config(root),
                repo="owner/repo",
                pr_number=1,
                head_sha="head",
                timeout_seconds=5,
                dry_run=False,
            )
            self.assertEqual(result[0]["status"], "BLOCK")
            self.assertEqual(result[0]["reason"], "test_executable_missing")
            self.assertEqual(result[0]["execution_target"], "local_disposable_review_worktree")


if __name__ == "__main__":
    unittest.main()
