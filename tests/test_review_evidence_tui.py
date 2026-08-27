from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from quality_pilot.bdd_contract import audit_bdd_contract
from quality_pilot.cli import main
from quality_pilot.config import load_project_config
from quality_pilot.evidence import evaluate_confirmed_bug_evidence
from quality_pilot.environment import configure_environment
from quality_pilot.review import _run_git, _safe_test_argv, complete_gitea_review_apply, prepare_gitea_review_reply
from quality_pilot.tui_probe import tui_probe


class ReviewEvidenceTuiTest(unittest.TestCase):
    def test_bdd_audit_exposes_current_binding_gap(self) -> None:
        payload = audit_bdd_contract()
        self.assertEqual(payload["scenario_count"], payload["current_scenario_count"] + payload["planned_scenario_count"])
        self.assertEqual(payload["current_scenario_count"], 39)
        self.assertEqual(payload["bound_scenario_count"], 39)
        self.assertEqual(payload["unbound_current_scenario_count"], 0)
        self.assertEqual(payload["coverage_percent"], 100.0)
        self.assertEqual(payload["overall"]["status"], "PARTIAL")
        self.assertIn("planned_scenarios_are_not_green_evidence", payload["lighting_effects"])







    def test_tui_probe_requires_environment_and_marker_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(StringIO()):
                main(["setup", "--root", tmp])
            config = load_project_config(root)
            blocked = tui_probe(config, entrypoint="python3 -c 'print(\"TUI\")'", expected_markers=["TUI"], confirm=True)
            self.assertEqual(blocked["status"], "BLOCK")
            self.assertEqual(blocked["blocked_reason"], "environment_profile_required")
            config_path = root / ".quality-pilot.yaml"
            text = config_path.read_text(encoding="utf-8")
            text = text.replace('  primary_entrypoint: ""', '  primary_entrypoint: "python3 -c \'print(\\"TUI\\")\'"')
            text = text.replace('  execution_mode: ""', '  execution_mode: "local"')
            text = text.replace('  environment_confirmed: false', '  environment_confirmed: true')
            text = text.replace('  side_effect_boundary: ""', '  side_effect_boundary: "transcript-only"')
            config_path.write_text(text, encoding="utf-8")
            config = load_project_config(root)
            dry = tui_probe(config, expected_markers=["TUI"], dry_run=True)
            self.assertEqual(dry["status"], "dry_run")
            hold = tui_probe(config, expected_markers=[], confirm=True)
            self.assertEqual(hold["status"], "HOLD")

    def test_tui_probe_captures_explicit_marker_and_persists_result_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(StringIO()):
                main(["setup", "--root", tmp])
            config = load_project_config(root)
            configure_environment(
                config,
                mode="local",
                entrypoint='python3 -c \'print("TUI READY")\'',
                side_effect_boundary="transcript-only",
                confirm=True,
            )
            config = load_project_config(root)
            result = tui_probe(config, expected_markers=["TUI READY"], confirm=True, duration_seconds=1.0)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["markers_missing"], [])
            result_path = root / result["result_path"]
            self.assertTrue(result_path.is_file())
            persisted = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["result_path"], result["result_path"])
            self.assertIn("transcript_path", result)


    def test_tui_probe_classifies_known_ipmi_invalid_command_as_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(StringIO()):
                main(["setup", "--root", tmp])
            config = load_project_config(root)
            configure_environment(
                config,
                mode="local",
                entrypoint='python3 -c \'print("rsp=0xc1 Invalid command")\'',
                side_effect_boundary="transcript-only",
                confirm=True,
            )
            config = load_project_config(root)
            result = tui_probe(config, expected_markers=["FAN ZONE STATUS"], confirm=True, duration_seconds=1.0)
            self.assertEqual(result["status"], "BLOCK")
            self.assertEqual(result["reason"], "hardware_preflight_invalid_command")
            self.assertNotEqual(result["test_outcome"], "FAIL")







    def test_confirmed_bug_evidence_separates_hold_and_block(self) -> None:
        incomplete = evaluate_confirmed_bug_evidence(
            {"status": "FAIL", "contract_hash": "h", "run_id": "r", "evidence_profile": {}},
            contract_hash="h",
        )
        self.assertEqual(incomplete["outcome"], "HOLD")
        blocked = evaluate_confirmed_bug_evidence(
            {
                "status": "BLOCK",
                "contract_hash": "h",
                "run_id": "r",
                "blocked_reason": "environment_profile_required",
                "evidence_profile": {},
            },
            contract_hash="h",
        )
        self.assertEqual(blocked["outcome"], "BLOCK")

    def test_review_mcp_apply_rejects_stale_and_deduplicates_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(StringIO()):
                main(["setup", "--root", tmp])
            config = load_project_config(root)
            report = {
                "repo": "owner/repo",
                "pr_number": 7,
                "head_sha": "abc123",
                "test_outcome": "PASS",
                "coverage_gap": False,
                "conclusion": "NO_BLOCKING_FINDINGS",
                "test_results": [{"id": "regression", "status": "PASS"}],
                "snapshot_path": "state/pr.json",
                "report_paths": {"json_path": "reports/reviews/pr.json", "markdown_path": "reports/reviews/pr.md"},
                "findings": [{"id": "F-1", "path": "src/app.py", "line": 4, "severity": "LOW", "message": "review note"}],
            }
            prepared = prepare_gitea_review_reply(config, report, report_hash="report-1", confirm=True, dry_run=False)
            self.assertEqual(prepared["status"], "local_only_pending")
            request_path = root / prepared["request_path"]
            request = json.loads(request_path.read_text(encoding="utf-8"))
            result_path = root / ".quality-pilot-project/state/gitea-mcp/review-write-result.json"
            result_path.write_text(json.dumps({"status": "ok", "head_sha": "def456"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "review_result_stale_head_sha"):
                complete_gitea_review_apply(config)
            result_path.write_text(
                json.dumps({"status": "ok", "repo": "owner/repo", "pr_number": 7, "head_sha": "abc123", "report_hash": "report-1"}) + "\n",
                encoding="utf-8",
            )
            applied = complete_gitea_review_apply(config)
            self.assertEqual(applied["status"], "ok")
            duplicate = complete_gitea_review_apply(config)
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(request["safety"]["allowed_targets"], ["pull_request_review"])

    @patch("quality_pilot.review.subprocess.run")
    def test_review_git_wrapper_keeps_git_as_executable(self, run: object) -> None:
        from subprocess import CompletedProcess

        run.return_value = CompletedProcess(["git"], 0, "", "")
        _run_git(["-C", "/tmp/checkout", "fetch", "--no-tags", "origin", "abc"], timeout=30)
        run.assert_called_once_with(
            ["git", "-C", "/tmp/checkout", "fetch", "--no-tags", "origin", "abc"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def test_review_selector_rejects_shell_command(self) -> None:
        self.assertEqual(
            _safe_test_argv("python3 -m unittest discover -s tests"),
            ["python3", "-m", "unittest", "discover", "-s", "tests"],
        )
        self.assertEqual(
            _safe_test_argv("python3 -m pytest tests -q"),
            ["python3", "-m", "pytest", "tests", "-q"],
        )
        self.assertIsNone(_safe_test_argv("python3 -m unittest discover -s tests; rm -rf /"))


if __name__ == "__main__":
    unittest.main()
