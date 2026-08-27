from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quality_pilot.review import _safe_test_argv, _targeted_oracle_summary, select_applicable_tests


class ReviewDiffOracleTest(unittest.TestCase):
    def _repo(self) -> Path:
        root = Path(tempfile.mkdtemp())
        tests = root / "tests"
        tests.mkdir()
        (tests / "test_cli.py").write_text(
            "def test_cli_surface():\n    assert True\n", encoding="utf-8"
        )
        (tests / "test_fanzone_tui.py").write_text(
            "from application.ui.tui.fanzone import FanZoneTui\n\n"
            "def test_fanzone_surface(tmp_path):\n    assert FanZoneTui\n", encoding="utf-8"
        )
        (tests / "test_unrelated.py").write_text(
            "def test_unrelated():\n    assert True\n", encoding="utf-8"
        )
        return root

    def test_changed_product_files_map_to_targeted_product_tests(self) -> None:
        root = self._repo()
        selection = select_applicable_tests(
            str(root),
            [
                {"path": "application/cli.py"},
                {"path": "application/ui/tui/fanzone.py"},
            ],
            python_executable="python3",
        )
        plan = selection["targeted_oracle"]
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(plan["test_id"], "diff-targeted-pytest")
        self.assertEqual(plan["test_files"], ["test_cli.py", "test_fanzone_tui.py"])
        targeted = selection["selected"][0]
        self.assertEqual(targeted["id"], "diff-targeted-pytest")
        self.assertEqual(
            _safe_test_argv(targeted["command"]),
            [
                "python3",
                "-m",
                "pytest",
                "tests/test_cli.py",
                "tests/test_fanzone_tui.py",
                "-q",
            ],
        )

    def test_missing_targeted_test_stays_hold(self) -> None:
        root = self._repo()
        selection = select_applicable_tests(str(root), [{"path": "application/orchestrator.py"}], python_executable="python3")
        self.assertEqual(selection["targeted_oracle"]["status"], "HOLD")
        self.assertEqual(selection["targeted_oracle"]["reason"], "diff_targeted_test_oracle_not_found")

    def test_targeted_oracle_result_is_not_inferred_without_execution(self) -> None:
        root = self._repo()
        selection = select_applicable_tests(str(root), [{"path": "application/cli.py"}], python_executable="python3")
        summary = _targeted_oracle_summary(selection, [])
        self.assertEqual(summary["status"], "BLOCK")
        self.assertEqual(summary["reason"], "diff_targeted_oracle_result_missing")

    def test_targeted_test_argv_rejects_path_traversal(self) -> None:
        self.assertIsNone(_safe_test_argv("python3 -m pytest tests/../outside.py -q"))

    def test_targeted_oracle_passes_only_from_matching_test_result(self) -> None:
        root = self._repo()
        selection = select_applicable_tests(str(root), [{"path": "application/cli.py"}], python_executable="python3")
        summary = _targeted_oracle_summary(
            selection,
            [{"id": "diff-targeted-pytest", "status": "PASS", "stdout": "evidence.log"}],
        )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["reason"], "diff_targeted_product_test_oracle_passed")
        self.assertEqual(summary["test_files"], ["test_cli.py"])


if __name__ == "__main__":
    unittest.main()
