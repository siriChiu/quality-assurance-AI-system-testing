from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from quality_pilot import cli
from quality_pilot.config import load_project_config
from quality_pilot.heartbeat import run_heartbeat
from quality_pilot.pipeline import PipelineResult, run_close_loop


class TruthModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with redirect_stdout(StringIO()):
            code = cli.main(["setup", "--root", str(self.root)])
        self.assertEqual(code, 0)
        self.config = load_project_config(self.root)

    def test_empty_scope_is_hold_not_pass(self) -> None:
        for path in self.config.paths.cases.glob("*.y*ml"):
            path.unlink()

        payload = run_close_loop(self.config).payload

        self.assertEqual(payload["status"], "HOLD")
        self.assertEqual(payload["test_outcome"], "HOLD")
        self.assertEqual(payload["workflow_status"], "BLOCKED")
        self.assertEqual(payload["gate_status"], "NOT_EVALUATED")
        self.assertEqual(payload["health_status"], "NOT_EVALUATED")
        steps = {step["name"]: step for step in payload["steps"]}
        self.assertEqual(steps["select_scope"]["status"], "HOLD")
        self.assertEqual(steps["health_checks"]["status"], "SKIPPED")
        self.assertEqual(steps["issues_sync_readiness"]["status"], "SKIPPED")

    def test_dry_run_is_not_run_not_pass(self) -> None:
        payload = run_close_loop(self.config, dry_run=True).payload

        self.assertEqual(payload["status"], "NOT_RUN")
        self.assertEqual(payload["test_outcome"], "NOT_RUN")
        self.assertEqual(payload["workflow_status"], "PLANNED")
        self.assertEqual(payload["gate_status"], "NOT_EVALUATED")
        self.assertNotEqual(payload["health_status"], "HEALTHY")
        self.assertTrue(all(result["status"] == "NOT_RUN" for result in payload["results"]))

    def test_blocked_write_gate_is_visible_without_changing_qa_outcome(self) -> None:
        denied = Mock()
        denied.as_dict.return_value = {"allowed": False, "reason": "policy_denied"}

        official_result = {
            "case_id": "EXAMPLE-001",
            "title": "Official result",
            "status": "PASS",
            "partial_probe": False,
            "commands": [],
            "evidence": ["evidence/result.json"],
            "contract_hash": "official-hash",
        }
        with (
            patch("quality_pilot.pipeline.run_case", return_value=official_result),
            patch("quality_pilot.pipeline.evaluate_write_gate", return_value=denied),
        ):
            payload = run_close_loop(self.config).payload

        self.assertEqual(payload["test_outcome"], "PASS")
        self.assertEqual(payload["gate_status"], "BLOCKED")
        self.assertEqual(payload["workflow_status"], "BLOCKED")
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["health_status"], "NOT_EVALUATED")
        self.assertEqual(payload["tracker_writes"]["blocked_by_gate"], 1)
        steps = {step["name"]: step for step in payload["steps"]}
        self.assertEqual(steps["write_gate"]["status"], "BLOCKED")
        self.assertEqual(steps["publish_wiki_status"]["status"], "SKIPPED")

    def test_all_partial_probes_hold_official_truth(self) -> None:
        partial_result = {
            "case_id": "EXAMPLE-001",
            "title": "Exit-only probe",
            "status": "PASS",
            "partial_probe": True,
            "commands": [],
            "evidence": ["evidence/result.json"],
            "contract_hash": "probe-hash",
        }

        with patch("quality_pilot.pipeline.run_case", return_value=partial_result):
            payload = run_close_loop(self.config).payload

        self.assertEqual(payload["status"], "HOLD")
        self.assertEqual(payload["test_outcome"], "HOLD")
        self.assertEqual(payload["probe_outcome"], "PASS")
        self.assertEqual(payload["case_counts"]["PASS"], 0)
        self.assertEqual(payload["partial_probe_counts"]["PASS"], 1)

    def test_successful_test_and_gate_do_not_claim_health(self) -> None:
        official_result = {
            "case_id": "EXAMPLE-001",
            "title": "Official result",
            "status": "PASS",
            "partial_probe": False,
            "commands": [],
            "evidence": ["evidence/result.json"],
            "contract_hash": "official-hash",
        }
        allowed = Mock()
        allowed.as_dict.return_value = {"allowed": True, "reason": "allowed"}

        with (
            patch("quality_pilot.pipeline.run_case", return_value=official_result),
            patch("quality_pilot.pipeline.evaluate_write_gate", return_value=allowed),
        ):
            payload = run_close_loop(self.config).payload

        self.assertEqual(payload["test_outcome"], "PASS")
        self.assertEqual(payload["gate_status"], "ALLOWED")
        self.assertEqual(payload["health_status"], "NOT_EVALUATED")

    @patch("quality_pilot.heartbeat.run_close_loop")
    @patch("quality_pilot.heartbeat.generate_cases_growing")
    @patch("quality_pilot.heartbeat.issue_status", side_effect=RuntimeError("snapshot unavailable"))
    def test_blocked_issue_sensor_stops_heartbeat_and_alerts(
        self,
        _issue_status: Mock,
        generate: Mock,
        close_loop: Mock,
    ) -> None:
        payload = run_heartbeat(self.config)

        generate.assert_not_called()
        close_loop.assert_not_called()
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["qa_outcome"], "NOT_RUN")
        self.assertTrue(payload["alert_required"])
        self.assertEqual(payload["latest_tick"]["reason"], "issue_sensor_blocked")
        self.assertEqual(payload["latest_tick"]["sensors"][0]["status"], "blocked")

    def test_legacy_exit_only_case_is_automatically_held_as_partial(self) -> None:
        for path in self.config.paths.cases.glob("*.y*ml"):
            path.unlink()
        (self.config.paths.cases / "legacy.yaml").write_text(
            """case_id: LEGACY-EXIT-ONLY
title: Legacy exit-only contract
commands:
  - id: probe
    run: python3 --version
    expected_exit_code: 0
""",
            encoding="utf-8",
        )

        payload = run_close_loop(self.config).payload

        self.assertEqual(payload["test_outcome"], "HOLD")
        self.assertEqual(payload["probe_outcome"], "PASS")
        self.assertEqual(payload["case_counts"]["PASS"], 0)
        self.assertEqual(payload["partial_probe_counts"]["PASS"], 1)
        self.assertTrue(payload["results"][0]["partial_probe"])

    @patch("quality_pilot.heartbeat.run_close_loop")
    @patch("quality_pilot.heartbeat.generate_cases_growing")
    def test_heartbeat_dry_run_is_plan_only_and_not_persisted(self, generate: Mock, close_loop: Mock) -> None:
        payload = run_heartbeat(self.config, dry_run=True, grow_count=7)

        generate.assert_not_called()
        close_loop.assert_not_called()
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["qa_outcome"], "NOT_RUN")
        self.assertFalse(payload["alert_required"])
        self.assertFalse(payload["state_persisted"])
        self.assertEqual(payload["latest_tick"]["planned_scope"]["grow_count"], 7)
        self.assertFalse((self.config.paths.state / "close-loop" / "heartbeat-latest.json").exists())

    @patch("quality_pilot.heartbeat.auto_sync_wiki", return_value={"status": "ok"})
    @patch("quality_pilot.heartbeat.run_close_loop")
    @patch("quality_pilot.heartbeat.generate_cases_growing")
    @patch("quality_pilot.heartbeat.issue_status", return_value={"status": "ok", "open_count": 1})
    def test_explicit_heartbeat_case_skips_growth(
        self,
        _issue_status: Mock,
        generate: Mock,
        close_loop: Mock,
        _wiki: Mock,
    ) -> None:
        close_loop.return_value = PipelineResult({
            "status": "PASS",
            "test_outcome": "PASS",
            "workflow_status": "COMPLETED",
            "gate_status": "ALLOWED",
        })

        payload = run_heartbeat(self.config, case_id="EXAMPLE-001", legacy=True)

        generate.assert_not_called()
        close_loop.assert_called_once_with(self.config, case_id="EXAMPLE-001", case_ids=None, dry_run=False)
        self.assertEqual(payload["qa_outcome"], "PASS")
        self.assertFalse(payload["alert_required"])
        self.assertEqual(payload["latest_tick"]["growth"]["reason"], "explicit_case_scope")

    @patch("quality_pilot.heartbeat.auto_sync_wiki", return_value={"status": "ok"})
    @patch("quality_pilot.heartbeat.run_close_loop")
    @patch("quality_pilot.heartbeat.generate_cases_growing")
    @patch("quality_pilot.heartbeat.issue_status", return_value={"status": "ok", "open_count": 1})
    def test_heartbeat_exposes_failed_qa_and_alert(
        self,
        _issue_status: Mock,
        generate: Mock,
        close_loop: Mock,
        _wiki: Mock,
    ) -> None:
        generate.return_value = {
            "status": "ok",
            "generated": [{"case_id": "GROWTH-001"}],
            "generated_count": 1,
        }
        close_loop.return_value = PipelineResult({
            "status": "FAIL",
            "test_outcome": "FAIL",
            "workflow_status": "COMPLETED",
            "gate_status": "BLOCKED",
        })

        payload = run_heartbeat(self.config, legacy=True)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["qa_outcome"], "FAIL")
        self.assertTrue(payload["alert_required"])

    @patch("quality_pilot.cli.run_heartbeat")
    def test_fail_on_test_failure_controls_heartbeat_exit_code(self, heartbeat: Mock) -> None:
        heartbeat.return_value = {"status": "ok", "qa_outcome": "FAIL", "alert_required": True}

        with redirect_stdout(StringIO()) as output:
            exit_code = cli.main([
                "close-loop",
                "heartbeat",
                "--root",
                str(self.root),
                "--fail-on-test-failure",
            ])

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue())["qa_outcome"], "FAIL")
        self.assertTrue(heartbeat.call_args.kwargs["fail_on_test_failure"])

    @patch("quality_pilot.cli._with_auto_wiki", side_effect=lambda _config, payload, **_kwargs: payload)
    @patch("quality_pilot.cli.run_close_loop_task_graph")
    def test_fail_on_test_failure_controls_run_once_exit_code(self, close_loop: Mock, _wiki: Mock) -> None:
        close_loop.return_value = PipelineResult({
            "status": "FAIL",
            "test_outcome": "FAIL",
            "workflow_status": "COMPLETED",
            "gate_status": "BLOCKED",
            "health_status": "UNHEALTHY",
        })

        with redirect_stdout(StringIO()):
            exit_code = cli.main([
                "close-loop",
                "run-once",
                "--root",
                str(self.root),
                "--fail-on-test-failure",
            ])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
