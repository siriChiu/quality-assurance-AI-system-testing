from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from quality_pilot.cli import main
from quality_pilot.config import load_project_config
from quality_pilot.contracts import load_contract
from quality_pilot.pipeline import PIPELINE_ORDER, run_close_loop, run_close_loop_task_graph
from quality_pilot.runner import RunContext, run_case
from quality_pilot.write_gate import evaluate_write_gate


class RunnerPipelineTest(unittest.TestCase):
    def test_runner_captures_stdout_stderr_rc_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "case.yaml"
            case.write_text(
                """case_id: FAIL-1
title: Failing command
commands:
  - id: fail
    run: python3 -c "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"
    expected_exit_code: 0
""",
                encoding="utf-8",
            )
            result = run_case(load_contract(case), RunContext(root=root, evidence_dir=root / "evidence"))
            self.assertEqual(result["status"], "FAIL")
            command = result["commands"][0]
            self.assertTrue((root / command["stdout"]).exists())
            self.assertTrue((root / command["stderr"]).exists())
            self.assertTrue((root / command["rc"]).exists())

    def test_write_gate_denies_closed_drift_secret_and_missing_evidence(self) -> None:
        config = {"tracker": {"provider": "gitea"}, "policy": {"require_write_gate": True}}
        result = {"status": "PASS", "evidence": ["x"], "contract_hash": "abc"}
        self.assertEqual(evaluate_write_gate(config_data=config, result=result, target_state="closed").reason, "closed_issue_write_forbidden")
        self.assertEqual(evaluate_write_gate(config_data=config, result=result, expected_contract_hash="def").reason, "contract_drift")
        self.assertEqual(evaluate_write_gate(config_data={"tracker": {"provider": "gitea", "api_token": "secret"}}, result=result).reason, "raw_secret_detected")
        self.assertEqual(evaluate_write_gate(config_data=config, result={"status": "PASS", "contract_hash": "abc"}).reason, "missing_current_evidence")

    def test_task_graph_run_persists_checkpoint_and_resumes_through_human_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(StringIO()):
                main(["setup", "--root", tmp])
            config = load_project_config(Path(tmp))
            first = run_close_loop_task_graph(config)
            self.assertEqual(first.payload["execution_mode"], "task_graph")
            self.assertEqual(first.payload["status"], "HOLD")
            self.assertEqual(first.payload["test_outcome"], "PASS")
            self.assertEqual(first.payload["task_graph"]["status"], "HOLD")
            self.assertEqual(first.payload["task_graph"]["human_gate_status"], "HOLD")
            checkpoint_path = config.paths.state / "close-loop" / "task-graph-latest.json"
            self.assertTrue(checkpoint_path.exists())
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["schema"], "quality-pilot.task-graph-checkpoint.v1")
            self.assertEqual(checkpoint["contract_hash"], first.payload["task_graph"]["contract_hash"])
            self.assertEqual(checkpoint_path.stat().st_mode & 0o777, 0o600)

            resumed = run_close_loop_task_graph(config, resume=True, confirm_publish=True)
            self.assertEqual(resumed.payload["status"], "PASS")
            self.assertEqual(resumed.payload["task_graph"]["status"], "PASS")
            self.assertEqual(resumed.payload["task_graph"]["nodes"]["publish.publish"]["status"], "PASS")
            self.assertEqual(resumed.payload["task_graph"]["contract_hash"], first.payload["task_graph"]["contract_hash"])

    def test_pipeline_order_and_latest_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(StringIO()):
                main(["setup", "--root", tmp])
            config = load_project_config(root)
            result = run_close_loop(config)
            self.assertEqual([step["name"] for step in result.payload["steps"]], PIPELINE_ORDER)
            self.assertTrue((root / ".quality-pilot-project" / "state" / "latest-run.json").exists())
            latest = json.loads((root / ".quality-pilot-project" / "state" / "latest-run.json").read_text(encoding="utf-8"))
            self.assertIn("tracker_writes", latest)
            self.assertEqual(latest["steps"][-1]["name"], "persist_state")
            self.assertEqual(latest["steps"][-1]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
