from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from qa_aist import hermes
from qa_aist.cli import main


class LifecycleTest(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(args)
        return code, json.loads(buf.getvalue())

    def init_gitea_project(self, tmp: str) -> Path:
        root = Path(tmp)
        self.run_cli(["init-project", "--root", tmp])
        config = root / ".qa-aist.yaml"
        text = config.read_text(encoding="utf-8")
        text = text.replace("  provider: none", "  provider: gitea")
        text = text.replace('    base_url: ""', '    base_url: "https://git.example.test"')
        text = text.replace('    repo: ""', '    repo: "Redfish/irctool"')
        config.write_text(text, encoding="utf-8")
        return root

    def init_gitea_mcp_project(self, tmp: str) -> Path:
        root = self.init_gitea_project(tmp)
        config = root / ".qa-aist.yaml"
        text = config.read_text(encoding="utf-8")
        text = text.replace("    backend: http", "    backend: mcp")
        config.write_text(text, encoding="utf-8")
        return root

    def write_issues(self, root: Path) -> Path:
        issues = [
            {
                "number": 1,
                "state": "open",
                "title": "CLI help command fails",
                "body": "Command: python3 --version\n\nThe user-facing help path should stay callable.",
                "html_url": "https://git.example.test/Redfish/irctool/issues/1",
                "updated_at": "2026-06-08T00:00:00Z",
                "labels": [{"name": "bug"}],
                "comments": [{"user": {"login": "qa"}, "body": "Please add regression coverage."}],
            },
            {
                "number": 2,
                "state": "closed",
                "title": "Old closed issue",
                "body": "Already fixed.",
                "html_url": "https://git.example.test/Redfish/irctool/issues/2",
            },
        ]
        path = root / "issues.json"
        path.write_text(json.dumps(issues), encoding="utf-8")
        return path

    def test_issues_sync_removes_closed_mirror_and_writes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            stale = root / ".qa-aist-project" / "issues" / "2.md"
            stale.write_text("stale closed mirror", encoding="utf-8")
            issues_json = self.write_issues(root)

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json), "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["open_active_issue_ids"], [1])
            self.assertEqual(payload["closed_issue_ids"], [2])
            self.assertEqual(payload["removed_mirror_ids"], [2])
            self.assertTrue((root / ".qa-aist-project" / "issues" / "1.md").exists())
            self.assertFalse(stale.exists())
            snapshot = json.loads((root / ".qa-aist-project" / "state" / "issues-snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["items"][0]["issue_id"], 1)

    def test_issues_sync_reads_gitea_mcp_snapshot_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_mcp_project(tmp)
            stale = root / ".qa-aist-project" / "issues" / "2.md"
            stale.write_text("stale closed mirror", encoding="utf-8")
            mcp_path = root / ".qa-aist-project" / "state" / "gitea-mcp" / "issues.json"
            mcp_path.parent.mkdir(parents=True, exist_ok=True)
            issues = json.loads(self.write_issues(root).read_text(encoding="utf-8"))
            mcp_path.write_text(json.dumps({"content": [{"type": "text", "text": json.dumps({"issues": issues})}]}), encoding="utf-8")

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["source"], "mcp")
            self.assertEqual(payload["mcp_issues_json"], ".qa-aist-project/state/gitea-mcp/issues.json")
            self.assertEqual(payload["open_active_issue_ids"], [1])
            self.assertEqual(payload["removed_mirror_ids"], [2])
            self.assertTrue((root / ".qa-aist-project" / "issues" / "1.md").exists())
            self.assertFalse(stale.exists())

    def test_issues_sync_mcp_backend_missing_snapshot_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.init_gitea_mcp_project(tmp)

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "IssueSyncError")
            self.assertIn("gitea_mcp_snapshot_missing", payload["message"])
            self.assertIn("QA_AIST_GITEA_MCP_ISSUES_JSON", payload["message"])
            self.assertNotIn("QA_AIST_TRACKER_TOKEN", payload["message"])

    def test_cases_generate_review_validate_and_qa_test_run_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            code, generated = self.run_cli(["cases", "generate", "--root", tmp, "--from-issues", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "needs_input")
            self.assertEqual(generated["generated"][0]["case_id"], "ISSUE-1")
            self.assertGreater(generated["generated"][0]["question_count"], 0)

            code, review = self.run_cli(["cases", "review", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(review["draft_count"], 1)

            code, validated = self.run_cli(["cases", "validate", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(validated["case_count"], 2)

            code, run_one = self.run_cli(["qa-test", "run-one", "--root", tmp, "--json", "ISSUE-1"])
            self.assertEqual(code, 0)
            self.assertEqual(run_one["status"], "PASS")
            self.assertEqual(run_one["results"][0]["case_id"], "ISSUE-1")

    def test_publish_plan_apply_gate_and_fix_pr_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])
            self.run_cli(["cases", "generate", "--root", tmp, "--from-issues"])
            self.run_cli(["close-loop", "run-once", "--root", tmp, "--case-id", "ISSUE-1"])

            code, plan = self.run_cli(["publish", "plan", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["blocked_by_gate"], 0)
            self.assertTrue((root / ".qa-aist-project" / "state" / "publish-plan.json").exists())

            code, apply_result = self.run_cli(["publish", "apply", "--root", tmp, "--json"])
            self.assertEqual(code, 4)
            self.assertEqual(apply_result["status"], "blocked")
            self.assertEqual(apply_result["error"], "gitea_not_configured")

            code, fix_plan = self.run_cli(["fix-issues", "plan", "--root", tmp, "--issue", "1", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(fix_plan["status"], "ready")
            self.assertIn("ISSUE-1", fix_plan["case_ids"])

            code, pr = self.run_cli(["fix-issues", "submit-pr", "--root", tmp, "--issue", "1", "--dry-run", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(pr["status"], "dry_run")
            self.assertEqual(pr["pr_payload"]["head"], "qa-aist/issue-1")

    def test_hermes_new_workflow_commands_and_qa_test_without_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)

            help_result = hermes.dispatch_chat_command("/qa-aist qa-test", root=root)
            self.assertEqual(help_result["status"], "ok")
            self.assertIn("qa-test 是什麼", help_result["chat_response"])
            self.assertIn("下一步可以選", help_result["chat_response"])
            self.assertTrue(help_result["payload"]["next_actions"])

            sync = hermes.dispatch_chat_command(f"/qa-aist issues sync --issues-json {issues_json}", root=root)
            self.assertEqual(sync["status"], "ok")
            self.assertEqual(sync["payload"]["open_count"], 1)
            self.assertEqual(sync["payload"]["next_actions"][0]["command"], "/qa-aist issues dedupe")

            generate = hermes.dispatch_chat_command("/qa-aist cases generate --from-issues", root=root)
            self.assertEqual(generate["status"], "needs_input")
            self.assertIn("generated_cases", generate["chat_response"])
            self.assertTrue(any(action.get("kind") == "ask_user" for action in generate["payload"]["next_actions"]))

    def test_hermes_mcp_snapshot_error_guides_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_mcp_project(tmp)

            result = hermes.dispatch_chat_command("/qa-aist issues sync", root=root)

            self.assertEqual(result["status"], "error")
            self.assertIn("gitea_mcp_snapshot_missing", result["chat_response"])
            self.assertIn("下一步可以選", result["chat_response"])
            self.assertEqual(result["payload"]["next_actions"][0]["command"], "/qa-aist issues sync")
            self.assertTrue(result["payload"]["next_actions"][0]["requires_confirmation"])

    def test_status_and_doctor_detect_missing_gitea_mcp_snapshot_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.init_gitea_mcp_project(tmp)

            status = hermes.dispatch_chat_command("/qa-aist status", root=tmp)
            self.assertEqual(status["status"], "warn")
            self.assertFalse(status["payload"]["issue_sync"]["issue_sync_ready"])
            self.assertIn("gitea_mcp_snapshot_missing", status["chat_response"])
            self.assertEqual(status["payload"]["next_actions"][0]["command"], "/qa-aist issues sync")

            doctor = hermes.dispatch_chat_command("/qa-aist doctor", root=tmp)
            self.assertEqual(doctor["status"].lower(), "warn")
            self.assertFalse(doctor["payload"]["issue_sync"]["issue_sync_ready"])
            self.assertIn("gitea_mcp_snapshot_missing", doctor["chat_response"])
            self.assertIn("需要處理", doctor["chat_response"])


if __name__ == "__main__":
    unittest.main()
