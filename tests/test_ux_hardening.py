from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from quality_pilot import hermes
from quality_pilot.cli import main
from quality_pilot.config import load_project_config
from quality_pilot.contracts import load_contract
from quality_pilot.runner import RunContext, run_case


class UxHardeningTest(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(args)
        return code, json.loads(buf.getvalue())

    def init_project(self, tmp: str, *, mcp: bool = True) -> Path:
        root = Path(tmp)
        self.run_cli(["setup", "--root", tmp])
        if mcp:
            status_path = root / ".quality-pilot-project" / "state" / "hermes-mcp" / "status.json"
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(json.dumps({"servers": ["gitea", "redmine"]}), encoding="utf-8")
        return root

    def write_ready_mcp_snapshots(self, root: Path) -> None:
        gitea_path = root / ".quality-pilot-project" / "state" / "gitea-mcp" / "issues.json"
        gitea_path.parent.mkdir(parents=True, exist_ok=True)
        gitea_path.write_text(
            json.dumps({"schema": "quality-pilot.gitea-mcp.issues.v1", "items": []}),
            encoding="utf-8",
        )
        redmine_path = root / ".quality-pilot-project" / "state" / "redmine-mcp" / "issues.json"
        redmine_path.parent.mkdir(parents=True, exist_ok=True)
        redmine_path.write_text(
            json.dumps({"schema": "quality-pilot.redmine-mcp.issues.v1", "issues": []}),
            encoding="utf-8",
        )

    def write_issue_snapshot(self, root: Path, *, issue_id: int = 99, redmine_id: int = 145085, case_id: str = "ISSUE-99") -> None:
        path = root / ".quality-pilot-project" / "state" / "issues-snapshot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.issue-snapshot.v1",
                    "synced_at": "2026-06-24T00:00:00Z",
                    "items": [
                        {
                            "issue_id": issue_id,
                            "state": "open",
                            "title": f"[Redmine #{redmine_id}] Cold boot sensor regression",
                            "body": f"Imported from Redmine #{redmine_id}.",
                            "labels": ["bug"],
                            "case_id": case_id,
                            "url": f"https://git.example.test/issues/{issue_id}",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def write_redmine_case(self, root: Path, *, issue_id: int = 99, redmine_id: int = 145085) -> None:
        case = root / ".quality-pilot-project" / "cases" / f"REDMINE-{redmine_id}.yaml"
        case.write_text(
            f"""case_id: REDMINE-{redmine_id}
title: "Redmine {redmine_id}: Cold boot sensor regression"
feature: Sensor
source:
  type: issue
  provider: redmine
  redmine_issue_id: {redmine_id}
  gitea_issue_id: {issue_id}
swqa_dimensions:
  - exact_reproduction
  - sibling_surface
  - boundary
  - invalid_input
  - side_effect_safe
quality_pilot:
  executable: true
commands:
  - id: safe_probe
    run: python3 --version
    expected_exit_code: 0
expected: Safe probe remains runnable.
""",
            encoding="utf-8",
        )

    def write_redmine_snapshot(self, root: Path, *, redmine_id: int = 145085) -> None:
        path = root / ".quality-pilot-project" / "state" / "redmine-mcp" / "issues.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        issue = {
            "id": redmine_id,
            "subject": "Cold boot sensor regression",
            "description": "Sensor inventory is empty after cold boot.",
            "status": {"name": "New"},
            "tracker": {"name": "Bug"},
            "project": {"name": "IRCTool"},
            "updated_on": "2026-06-24T09:19:43Z",
            "custom_fields": [],
            "journals": [],
            "attachments": [],
        }
        path.write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.redmine-mcp-issues.v1",
                    "source": "hermes_redmine_mcp_live_read",
                    "fetched_at": "2026-06-24T09:20:00Z",
                    "requested_issue_ids": [redmine_id],
                    "include": ["description", "custom_fields", "journals", "attachments"],
                    "payload_completeness": "full",
                    "issues": [issue],
                }
            ),
            encoding="utf-8",
        )

    def test_t1_typo_tolerance_returns_recovery_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)

            result = hermes.dispatch_chat_command("/quality-pilot issues sync --redmine-issuses 145085", root=root)

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["payload"]["ux_recovery"]["problem_class"], "typo_argument")
            self.assertIn("--redmine-issues", result["payload"]["ux_recovery"]["recommended_command"])
            metrics = root / ".quality-pilot-project" / "state" / "ux-metrics.jsonl"
            self.assertTrue(metrics.exists())

    def test_t2_removed_command_migration_returns_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = hermes.dispatch_chat_command("/quality-pilot fix-issues", root=tmp)

            self.assertEqual(result["payload"]["error"], "command_removed")
            self.assertEqual(result["payload"]["ux_recovery"]["problem_class"], "removed_command")
            self.assertEqual(result["payload"]["replacement"], "/quality-pilot issues fix --issue <id>")

    def test_t3_redmine_id_fix_mapping_resolves_gitea_and_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)
            self.write_issue_snapshot(root)
            self.write_redmine_case(root)

            code, payload = self.run_cli(["issues", "fix", "--root", tmp, "--issue", "redmine-145085", "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "handoff")
            self.assertEqual(payload["id_resolution"]["resolved_gitea_issue_id"], 99)
            self.assertEqual(payload["id_resolution"]["resolved_case_id"], "REDMINE-145085")
            self.assertEqual(payload["case_ids"], ["REDMINE-145085"])

    def test_t4_issue_alias_case_run_maps_to_redmine_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)
            self.write_issue_snapshot(root)
            self.write_redmine_case(root)

            code, payload = self.run_cli(["cases", "run", "--root", tmp, "--json", "ISSUE-99"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["id_resolution"]["resolved_case_id"], "REDMINE-145085")
            self.assertEqual(payload["results"][0]["case_id"], "REDMINE-145085")

    def test_t5_handoff_consistency_blocks_non_runnable_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)
            self.write_issue_snapshot(root, case_id="ISSUE-99")

            code, payload = self.run_cli(["issues", "fix", "--root", tmp, "--issue", "99", "--json"])

            self.assertEqual(code, 4)
            self.assertEqual(payload["status"], "handoff_blocked")
            self.assertEqual(payload["error"], "handoff_case_id_not_runnable")
            self.assertEqual(payload["case_ids"], [])
            self.assertEqual(payload["ux_recovery"]["problem_class"], "handoff_inconsistent")

    def test_t6_doctor_returns_single_readiness_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.init_project(tmp)

            code, payload = self.run_cli(["doctor", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertIn(payload["readiness"]["mode"], {"WRITE_READY", "WRITE_BLOCKED_MCP", "SYNC_BLOCKED", "READ_ONLY_READY"})
            self.assertIn("remote_write_ready", payload["readiness"])

    def test_t6_doctor_write_ready_warn_does_not_claim_mcp_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)
            self.write_ready_mcp_snapshots(root)

            code, payload = self.run_cli(["doctor", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "WARN")
            self.assertEqual(payload["readiness"]["mode"], "WRITE_READY")
            self.assertNotIn("ux_recovery", payload)

    def test_t7_publish_wiki_plan_with_mcp_unknown_fails_fast_with_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.init_project(tmp, mcp=False)

            code, payload = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "blocked")
            self.assertIn(payload["readiness"]["mode"], {"WRITE_BLOCKED_MCP", "SYNC_BLOCKED"})
            self.assertEqual(payload["ux_recovery"]["problem_class"], "mcp_not_ready")
            self.assertEqual(payload["ux_recovery"]["recommended_command"], "/quality-pilot doctor")

    def test_t8_redmine_sync_returns_label_transparency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)
            self.write_redmine_snapshot(root)

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--redmine-issues", "145085", "--json"])

            self.assertEqual(code, 0)
            action = payload["mcp_issue_write_request"]["actions"][0]
            self.assertEqual(action["requested_labels"], ["redmine", "needs-triage", "needs-reproduction"])
            self.assertEqual(action["applied_labels"], [])
            self.assertEqual(action["unmatched_labels"], ["redmine", "needs-triage", "needs-reproduction"])
            self.assertEqual(action["label_resolution_note"], "pending_mcp_apply")
            self.assertTrue(action["idempotency_key"].startswith("redmine-145085-"))
            self.assertEqual(payload["label_resolution"][0]["redmine_issue_id"], 145085)

    def test_closed_issue_archive_and_traceability_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)
            stale = root / ".quality-pilot-project" / "issues" / "2.md"
            stale.write_text("stale closed mirror", encoding="utf-8")
            issues_json = root / "issues.json"
            issues_json.write_text(
                json.dumps(
                    [
                        {"number": 2, "state": "closed", "title": "closed", "body": "done"},
                        {
                            "number": 99,
                            "state": "open",
                            "title": "[Redmine #145085] Cold boot sensor regression",
                            "body": "Imported from Redmine #145085.",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            self.write_redmine_case(root)

            code, sync = self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json), "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(sync["closed_archive_paths"])
            self.assertTrue((root / sync["closed_archive_paths"][0]).exists())

            code, status = self.run_cli(["issues", "status", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            row = status["traceability"][0]
            self.assertEqual(row["gitea_issue_id"], 99)
            self.assertEqual(row["redmine_issue_ids"], [145085])
            self.assertEqual(row["case_id"], "REDMINE-145085")
            self.assertTrue(row["case_runnable"])

    def test_runner_safety_redacts_blocks_and_hashes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_case = root / "secret.yaml"
            secret_case.write_text(
                """case_id: SAFE-SECRET
title: Secret redaction
commands:
  - id: safe_probe
    run: python3 -c "print('password=supersecret')"
    expected_exit_code: 0
""",
                encoding="utf-8",
            )
            result = run_case(load_contract(secret_case), RunContext(root=root, evidence_dir=root / "evidence"))
            command = result["commands"][0]
            self.assertEqual(result["status"], "PASS")
            self.assertIn("[REDACTED]", (root / command["stdout"]).read_text(encoding="utf-8"))
            self.assertIn("stdout", command["evidence_sha256"])

            unsafe_case = root / "unsafe.yaml"
            unsafe_case.write_text(
                """case_id: UNSAFE-1
title: Unsafe command
commands:
  - id: unsafe
    run: rm -rf /
    expected_exit_code: 0
""",
                encoding="utf-8",
            )
            blocked = run_case(load_contract(unsafe_case), RunContext(root=root, evidence_dir=root / "evidence"))
            self.assertEqual(blocked["status"], "BLOCK")
            self.assertEqual(blocked["commands"][0]["blocked_reason"], "unsafe_command_pattern")

    def test_swqa_policy_gate_is_enforced_when_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gated_case = root / "gated.yaml"
            gated_case.write_text(
                """case_id: SWQA-GATE
title: Explicit SWQA gate
swqa_dimensions:
  - functional
quality_pilot:
  enforce_swqa_gates: true
commands:
  - id: safe_probe
    run: python3 --version
    expected_exit_code: 0
""",
                encoding="utf-8",
            )

            result = run_case(load_contract(gated_case), RunContext(root=root, evidence_dir=root / "evidence"))

            self.assertEqual(result["status"], "BLOCK")
            self.assertFalse(result["swqa_gate"]["allowed"])
            self.assertIn("missing_dimension:exact_reproduction", result["swqa_gate"]["reason_codes"])

    def test_wiki_management_dashboard_sections_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_project(tmp)
            self.write_redmine_case(root)

            code, payload = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            report = (root / payload["report_path"]).read_text(encoding="utf-8")
            for heading in ["## Coverage Matrix", "## Risk Register", "## Trend", "## Flaky Signal", "## Release Readiness"]:
                self.assertIn(heading, report)


if __name__ == "__main__":
    unittest.main()
