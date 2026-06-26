from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from quality_pilot import config as config_module
from quality_pilot.cli import main
from quality_pilot.config import load_yaml
from quality_pilot.hermes_mcp import MCP_SERVERS_ENV


class StateAuditTest(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(args)
        return code, json.loads(buf.getvalue())

    def init_project(self, root: Path) -> None:
        code, payload = self.run_cli(["setup", "--root", str(root), "--json"])
        self.assertEqual(code, 0, payload)

    def write_runtime_profile(self, root: Path, *, entrypoint: str = "cmd/democtl/democtl") -> None:
        config_path = root / ".quality-pilot.yaml"
        data = load_yaml(config_path)
        data["runtime"] = {
            "primary_entrypoint": entrypoint,
            "binary_env": "QUALITY_PILOT_BINARY",
            "target_host_env": "QUALITY_PILOT_TARGET_HOST",
            "fixture_paths": [],
            "credential_envs": [],
            "side_effect_boundary": "Read-only local parser/help probes only.",
        }
        config_path.write_text(config_module.yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def write_audited_irctool_overlay_shape(self, root: Path) -> None:
        self.init_project(root)
        qp = root / ".quality-pilot-project"
        (qp / "cases" / "REDMINE-145085.yaml").write_text(
            """case_id: REDMINE-145085
title: "Redmine 145085 timeout validation"
feature: "diagnostic collect"
source:
  type: redmine
  provider: redmine
  redmine_issue_id: 145085
  gitea_issue_id: 99
quality_pilot:
  executable: true
  review_required_before_run: false
  executable_scope: side_effect_safe_probe
commands:
  - id: safe_probe
    run: >-
      sh -c 'go run ./cmd/irctool __quality_pilot_invalid_command__ >/dev/null 2>&1; test $? -ne 0'
    expected_exit_code: 0
expected: "CLI rejects unknown commands."
""",
            encoding="utf-8",
        )
        issue_snapshot = {
            "schema": "quality-pilot.issue-snapshot.v1",
            "synced_at": "2026-06-24T14:30:00Z",
            "items": [
                {
                    "issue_id": 99,
                    "state": "open",
                    "title": "[Redmine #145085] timeout 0 should fail before ResourceInUse retry",
                    "body": "Imported from Redmine #145085. Linked quality-pilot case: REDMINE-145085.",
                    "labels": [],
                    "case_id": "ISSUE-99",
                    "url": "https://git.example.test/issues/99",
                },
                {
                    "issue_id": 100,
                    "state": "open",
                    "title": "[Redmine #144732] retry interval 0s should be rejected",
                    "body": "Imported from Redmine #144732.",
                    "labels": [],
                    "case_id": "ISSUE-100",
                    "url": "https://git.example.test/issues/100",
                },
            ],
        }
        (qp / "state" / "issues-snapshot.json").write_text(json.dumps(issue_snapshot), encoding="utf-8")
        result = {
            "case_id": "REDMINE-145085",
            "title": "Redmine 145085 timeout validation",
            "status": "PASS",
            "contract_hash": "old-contract-hash",
            "commands": [
                {
                    "id": "timeout_validation_precedes_resource_busy",
                    "run": "bash -lc 'echo real timeout validation runner'",
                    "expected_exit_code": 0,
                    "exit_code": 0,
                    "status": "PASS",
                }
            ],
            "evidence": [".quality-pilot-project/evidence/REDMINE-145085/result.json"],
            "exit_code": 0,
        }
        latest = {
            "status": "PASS",
            "run_id": "2026-06-24T083528077967Z",
            "case_counts": {"PASS": 1, "FAIL": 0, "BLOCK": 0, "ABORT": 0, "NOT_RUN": 0},
            "results": [result],
            "source": "cases",
            "latest_run_json": ".quality-pilot-project/state/latest-run.json",
            "report_path": ".quality-pilot-project/reports/status.md",
        }
        (qp / "state" / "latest-run.json").write_text(json.dumps(latest), encoding="utf-8")
        evidence_dir = qp / "evidence" / "REDMINE-145085"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (qp / "reports" / "status.md").write_text(
            """# AI Quality Pilot status

| Case | Status | Commands | Evidence |
|---|---|---:|---|
| REDMINE-145085 | PASS | 1 | .quality-pilot-project/evidence/REDMINE-145085/result.json |
""",
            encoding="utf-8",
        )
        (qp / "reports" / "wiki-status.md").write_text(
            """# Test status

## Release Readiness

- Status：READY

## 測試結果明細

| Case | Category | Status | Feature | Title |
|---|---|---|---|---|
| REDMINE-145085 | Uncategorized | NOT_RUN | diagnostic collect | Redmine 145085 timeout validation |
| ISSUE-100 | Uncategorized | NOT_RUN | diagnostic collect | retry interval |
""",
            encoding="utf-8",
        )
        (qp / "state" / "redmine-import.json").write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.redmine-import.v1",
                    "mode": "redmine_case_generation",
                    "imported_issue_ids": [145085],
                    "issues": [{"id": 145085, "subject": "timeout 0 should fail before ResourceInUse retry"}],
                }
            ),
            encoding="utf-8",
        )
        (qp / "state" / "redmine-gitea-sync-state.json").write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.redmine-gitea-sync-state.v1",
                    "issue_candidates": [
                        {
                            "id": "redmine-145085",
                            "redmine_issue_id": 145085,
                            "body": "Problem copied from Redmine without QA Focus.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        gitea_state = qp / "state" / "gitea-mcp"
        gitea_state.mkdir(parents=True, exist_ok=True)
        (gitea_state / "issue-write-request.json").write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.gitea-mcp-issue-write-request.v1",
                    "status": "needs_mcp_apply",
                    "operation": "gitea.issue.sync_from_redmine",
                    "actions": [
                        {
                            "id": "redmine-145085",
                            "operation": "gitea.issue.create",
                            "redmine_issue_id": 145085,
                            "title": "[Redmine #145085] timeout 0 should fail before ResourceInUse retry",
                            "body": "Problem text.\n\n### Raw Redmine JSON\n{}",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (gitea_state / "issue-write-result.json").write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.gitea-mcp-issue-write-result.v1",
                    "status": "applied",
                    "operation": "gitea.issue.sync_from_redmine",
                    "created_count": 1,
                    "actions": [{"redmine_issue_id": 145085, "created_issue_index": 99}],
                }
            ),
            encoding="utf-8",
        )
        (gitea_state / "wiki-write-request.json").write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.gitea-mcp-wiki-write-request.v1",
                    "status": "needs_mcp_apply",
                    "operation": "gitea.wiki.update_page",
                    "page": "Quality Pilot Test Status",
                    "event": "test_result",
                }
            ),
            encoding="utf-8",
        )
        (gitea_state / "wiki-write-result.json").write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.gitea-mcp-wiki-write-result.v1",
                    "request_schema": "quality-pilot.gitea-mcp-wiki-write-request.v1",
                    "status": "ok",
                    "page": "Quality Pilot Test Status",
                }
            ),
            encoding="utf-8",
        )
        (qp / "state" / "fix-plan.json").write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.fix-plan.v1",
                    "status": "ready",
                    "issue_id": 99,
                    "case_ids": ["ISSUE-99"],
                    "preflight": ["/quality-pilot cases run ISSUE-99"],
                }
            ),
            encoding="utf-8",
        )

    def test_audit_state_reports_semantic_blockers_when_yaml_validate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_audited_irctool_overlay_shape(root)

            code, validate = self.run_cli(["cases", "validate", "--root", str(root), "--json"])
            self.assertEqual(code, 0, validate)

            code, audit = self.run_cli(["audit", "state", "--root", str(root), "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(audit["schema"], "quality-pilot.state-audit.v1")
            self.assertEqual(audit["status"], "blocked")
            self.assertTrue(audit["syntax_valid"])
            self.assertFalse(audit["semantic_valid"])
            ids = {finding["id"] for finding in audit["findings"]}
            for expected in {
                "redmine_generic_probe_invalid",
                "redmine_developer_command_invalid",
                "evidence_contract_mismatch",
                "redmine_import_missing_qa_summary",
                "redmine_gitea_sync_missing_qa_handoff",
                "gitea_handoff_missing_qa_summary",
                "gitea_handoff_contains_raw_redmine_json",
                "stale_mcp_issue_write_request",
                "stale_mcp_wiki_write_request",
                "active_issue_missing_runnable_case",
                "stale_issue_case_alias",
                "fix_plan_non_runnable_case",
                "report_truth_disagreement",
                "wiki_ready_without_execution",
                "hermes_mcp_status_missing",
                "subagent_profile_incomplete",
            }:
                self.assertIn(expected, ids)
            artifact_paths = {item["path"] for item in audit["state_artifacts"]}
            self.assertIn(".quality-pilot-project/state/redmine-import.json", artifact_paths)
            self.assertIn(".quality-pilot-project/state/latest-run.json", artifact_paths)
            self.assertIn(".quality-pilot-project/reports/wiki-status.md", artifact_paths)
            self.assertIn("/quality-pilot cases generate --redmine-issues 145085", audit["next_actions"])
            self.assertIn("/quality-pilot cases run REDMINE-145085", audit["next_actions"])
            self.assertIn("/quality-pilot cases generate --redmine-issues 144732", audit["next_actions"])
            self.assertIn("/quality-pilot publish wiki plan", audit["next_actions"])

    def test_issues_status_exposes_canonical_mapping_and_audit_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_audited_irctool_overlay_shape(root)

            code, payload = self.run_cli(["issues", "status", "--root", str(root), "--json"])

            self.assertEqual(code, 0)
            rows = {row["gitea_issue_id"]: row for row in payload["traceability"]}
            self.assertEqual(rows[99]["snapshot_case_id"], "ISSUE-99")
            self.assertEqual(rows[99]["case_id"], "REDMINE-145085")
            self.assertTrue(rows[99]["case_runnable"])
            self.assertEqual(rows[99]["coverage_status"], "stale_case")
            self.assertEqual(rows[99]["repair_action"], "/quality-pilot cases generate --redmine-issues 145085")
            self.assertEqual(rows[100]["snapshot_case_id"], "ISSUE-100")
            self.assertIsNone(rows[100]["case_id"])
            self.assertFalse(rows[100]["case_runnable"])
            self.assertEqual(rows[100]["coverage_status"], "no_case")
            self.assertEqual(rows[100]["repair_action"], "/quality-pilot cases generate --redmine-issues 144732")
            self.assertEqual(payload["state_audit"]["status"], "blocked")
            self.assertIn("active_issue_missing_runnable_case", payload["state_audit"]["blockers"])

    def test_doctor_keeps_mcp_missing_and_local_audit_findings_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_audited_irctool_overlay_shape(root)

            code, payload = self.run_cli(["doctor", "--root", str(root), "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["state_audit"]["status"], "blocked")
            check_names = {check["name"] for check in payload["checks"]}
            self.assertIn("hermes.mcp.status", check_names)
            self.assertIn("state.audit", check_names)
            self.assertIn("hermes_mcp_status_missing", payload["state_audit"]["blockers"])
            self.assertIn("redmine_generic_probe_invalid", payload["state_audit"]["blockers"])
            self.assertIn("redmine_developer_command_invalid", payload["state_audit"]["blockers"])

    def test_audit_flags_generated_command_policy_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.init_project(root)
            self.write_runtime_profile(root)
            cases = root / ".quality-pilot-project" / "cases"
            cases.mkdir(parents=True, exist_ok=True)
            (cases / "INIT-DEMO.yaml").write_text(
                """case_id: INIT-DEMO
title: "Generated repo-only placeholder"
source:
  type: init
quality_pilot:
  generation_mode: init
  executable: true
commands:
  - id: repo_probe
    run: python3 -c 'from pathlib import Path; assert Path("README.md").exists()'
    expected_exit_code: 0
expected: "Repo metadata exists."
""",
                encoding="utf-8",
            )

            code, audit = self.run_cli(["audit", "state", "--root", str(root), "--json"])

            self.assertEqual(code, 0)
            self.assertIn("generated_command_policy_violation", audit["blockers"])
            finding = next(item for item in audit["findings"] if item["id"] == "generated_command_policy_violation")
            self.assertEqual(finding["case_id"], "INIT-DEMO")
            self.assertIn("python_inline_metadata_check", finding["invalid_commands"][0]["policy_reasons"])

    def test_wiki_render_does_not_mark_ready_when_latest_run_is_not_reflected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.init_project(root)
            case_path = root / ".quality-pilot-project" / "cases" / "REDMINE-145085.yaml"
            case_path.write_text(
                """case_id: REDMINE-145085
title: "Redmine 145085"
commands:
  - id: safe_probe
    run: python3 --version
    expected_exit_code: 0
""",
                encoding="utf-8",
            )
            latest = {
                "status": "PASS",
                "run_id": "stale-run",
                "results": [{"case_id": "OTHER-CASE", "status": "PASS", "commands": [], "evidence": []}],
            }
            (root / ".quality-pilot-project" / "state" / "latest-run.json").write_text(json.dumps(latest), encoding="utf-8")

            code, payload = self.run_cli(["publish", "wiki", "plan", "--root", str(root), "--json"])

            self.assertEqual(code, 0)
            self.assertIn("NOT_READY_NO_CURRENT_RUN", payload["body"])
            self.assertIn("Stale report warning：all listed cases are NOT_RUN", payload["body"])
            self.assertNotIn("Status：READY", payload["body"])

    def test_doctor_persists_hermes_mcp_status_from_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.init_project(root)
            status_path = root / ".quality-pilot-project" / "state" / "hermes-mcp" / "status.json"
            self.assertFalse(status_path.exists())

            with patch.dict(os.environ, {MCP_SERVERS_ENV: "gitea,redmine"}):
                code, payload = self.run_cli(["doctor", "--root", str(root), "--json"])

            self.assertEqual(code, 0)
            self.assertTrue(payload["hermes_mcp_status_persist"]["persisted"])
            self.assertTrue(status_path.exists())
            written = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(written["servers"], ["gitea", "redmine"])

            code, payload = self.run_cli(["doctor", "--root", str(root), "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(payload["hermes_mcp"]["known"])
            self.assertEqual(payload["hermes_mcp"]["servers"], ["gitea", "redmine"])


if __name__ == "__main__":
    unittest.main()
