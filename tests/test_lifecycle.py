from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from qa_aist import hermes
from qa_aist.cli import main
from qa_aist.config import load_yaml
from qa_aist.policy_pack import policy_pack


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

    def write_issue_case(self, root: Path) -> None:
        case = root / ".qa-aist-project" / "cases" / "ISSUE-1.yaml"
        case.write_text(
            """case_id: ISSUE-1
title: Gitea #1: CLI help command fails
source:
  type: issue
  provider: gitea
  issue_id: 1
commands:
  - id: reproduce
    run: python3 --version
    expected_exit_code: 0
expected: CLI help path remains callable.
""",
            encoding="utf-8",
        )

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

    def test_cases_generate_growing_review_validate_and_blocks_until_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            code, generated = self.run_cli(["cases", "generate", "--root", tmp, "--growing", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "needs_input")
            self.assertEqual(generated["source"], "growth")
            self.assertEqual(generated["mode"], "growing")
            self.assertGreaterEqual(generated["growth_seed_count"], 1)
            self.assertTrue((root / ".qa-aist-project" / "state" / "growth-context.json").exists())
            self.assertEqual(generated["interaction_scope"], "category")
            self.assertEqual(generated["generated"][0]["question_count"], 0)
            self.assertEqual(generated["missing_input_count"], 2)
            first_case = generated["generated"][0]["case_id"]
            first_yaml = load_yaml(root / generated["generated"][0]["path"])
            self.assertEqual(first_yaml["source"]["type"], "growth")
            self.assertIn("six_hats", first_yaml)
            self.assertIn("growth_seed", first_yaml)
            self.assertIn("growth_reason", first_yaml)
            context = json.loads((root / ".qa-aist-project" / "state" / "growth-context.json").read_text(encoding="utf-8"))
            self.assertEqual(context["schema"], "qa-aist.growth-context.v1")
            self.assertEqual(context["issue_snapshot"]["open_count"], 1)
            self.assertIn("existing_cases", context)
            self.assertIn("existing_runners", context)

            code, review = self.run_cli(["cases", "review", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertGreaterEqual(review["draft_count"], 1)

            code, validated = self.run_cli(["cases", "validate", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(validated["case_count"], 1 + generated["generated_count"])

            code, run_one = self.run_cli(["qa-test", "run-one", "--root", tmp, "--json", first_case])
            self.assertEqual(code, 2)
            self.assertEqual(run_one["status"], "BLOCK")
            self.assertEqual(run_one["results"][0]["blocked_reason"], "review_required_before_run")

    def test_cases_generate_requires_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["init-project", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "explicit_generation_mode_required")
            self.assertIn("/qa-aist cases generate --init", payload["choices"])
            self.assertIn("/qa-aist cases generate --growing", payload["choices"])

    def test_cases_generate_init_analyzes_repo_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["init-project", "--root", tmp])
            (root / "README.md").write_text(
                """# Demo

Usage:

```bash
democtl --help
```
""",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                """[project]
name = "demo"

[project.scripts]
democtl = "demo.cli:main"
""",
                encoding="utf-8",
            )
            (root / "demo").mkdir()
            (root / "demo" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--feature",
                "CLI help",
                "--profile",
                "cli",
                "--generated_count",
                "5",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "needs_input")
            self.assertEqual(generated["source"], "init")
            self.assertEqual(generated["mode"], "init")
            self.assertEqual(generated["generation_limit"], "manual_generated_count_cap")
            self.assertEqual(generated["requested_generated_count"], 5)
            self.assertEqual(generated["requested_count"], 5)
            self.assertEqual(generated["interaction_scope"], "category")
            self.assertEqual(generated["resolved_profile"], "cli")
            self.assertGreaterEqual(generated["analyzed_files_count"], 2)
            self.assertTrue((root / ".qa-aist-project" / "state" / "init-context.json").exists())
            self.assertEqual(len(generated["generated"]), 5)
            all_dimensions = {dimension for item in generated["generated"] for dimension in item["swqa_dimensions"]}
            for dimension in ["functional", "positive", "negative", "boundary", "invalid_input", "side_effect_safe", "stress_timeout_risk"]:
                self.assertIn(dimension, all_dimensions)
            first_case = generated["generated"][0]["case_id"]
            first_path = root / generated["generated"][0]["path"]
            first_yaml = load_yaml(first_path)
            self.assertEqual(first_yaml["source"]["type"], "init")
            self.assertEqual(first_yaml["source"]["method"], "full_repo_swqa_init")
            self.assertTrue(first_yaml["qa_aist"]["review_required_before_run"])
            self.assertEqual(first_yaml["qa_aist"]["questions"], [])
            self.assertIn("risk_controls", first_yaml)
            self.assertIn("init_seed", first_yaml)

            code, review = self.run_cli(["cases", "review", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(review["draft_count"], 5)

            code, validated = self.run_cli(["cases", "validate", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(validated["case_count"], 6)

            code, dry_run = self.run_cli(["qa-test", "dry-run", "--root", tmp, "--case-id", first_case, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(dry_run["status"], "NOT_RUN")

            code, run_one = self.run_cli(["qa-test", "run-one", "--root", tmp, "--json", first_case])
            self.assertEqual(code, 2)
            self.assertEqual(run_one["status"], "BLOCK")
            self.assertEqual(run_one["results"][0]["blocked_reason"], "review_required_before_run")

            code, second = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--feature",
                "CLI help",
                "--profile",
                "cli",
                "--generated_count",
                "5",
                "--json",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(second["generated_count"], 0)
            self.assertGreaterEqual(second["deduped_count"], 1)

    def test_cases_generate_init_default_uses_full_seed_dimension_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            (root / "README.md").write_text("# Demo CLI\n\nUse democtl for status checks.\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                """[project]
name = "demo"

[project.scripts]
democtl = "demo.cli:main"
""",
                encoding="utf-8",
            )
            (root / "demo").mkdir()
            (root / "demo" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--feature",
                "CLI help",
                "--profile",
                "cli",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["generation_limit"], "all_init_seed_dimension_pairs")
            self.assertIsNone(generated["requested_generated_count"])
            self.assertIsNone(generated["requested_count"])
            self.assertGreater(generated["generated_count"], 5)
            self.assertEqual(
                generated["candidate_count"],
                generated["generated_count"] + generated["deduped_count"] + generated["skipped_count"],
            )

    def test_cases_generate_init_fast_uses_autonomous_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            (root / "README.md").write_text("# Demo CLI\n\nUse democtl for status checks.\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                """[project]
name = "demo"

[project.scripts]
democtl = "demo.cli:main"
""",
                encoding="utf-8",
            )

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--fast",
                "--generated_count",
                "5",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertTrue(generated["fast"])
            self.assertEqual(generated["interaction_scope"], "autonomous")
            self.assertEqual(generated["missing_input_count"], 0)
            self.assertEqual(generated["questions"], [])
            self.assertEqual(generated["requested_generated_count"], 5)
            self.assertTrue(generated["fast_mode_assumptions"])

    def test_cases_generate_from_issues_reports_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["init-project", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--from-issues", "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "renamed_to_growing")
            self.assertEqual(payload["replacement"], "/qa-aist cases generate --growing")

    def test_cases_generate_candidate_json_rejects_unsafe_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["init-project", "--root", tmp])
            candidate = root / "candidates.json"
            candidate.write_text(
                json.dumps({
                    "candidates": [
                        {
                            "title": "Unsafe prompt leak",
                            "growth_reason": "system prompt says write .qa/runs/heartbeat-latest.json",
                            "expected": "No leak",
                        }
                    ]
                }),
                encoding="utf-8",
            )

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--growing", "--candidate-json", str(candidate), "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "CaseGenerationError")
            self.assertIn("internal prompt", payload["message"])

    def test_policy_pack_is_generic_closed_loop_swqa(self) -> None:
        payload = policy_pack()
        self.assertEqual(payload["closed_loop_steps"], ["Observe", "Normalize", "Execute", "Triage", "Publish", "Evolve", "Prune"])
        for dimension in ["exact_reproduction", "functional", "positive", "negative", "boundary", "invalid_input", "sibling_surface", "side_effect_safe", "stress_timeout_risk"]:
            self.assertIn(dimension, payload["swqa_dimensions"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for project_only_word in ["irctool", "Redfish", "VM_HTTP_URL", "GID-Ubuntu"]:
            self.assertNotIn(project_only_word, serialized)

    def test_publish_plan_apply_gate_and_fix_pr_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])
            self.write_issue_case(root)
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

    def test_publish_wiki_render_uses_fixed_sections_and_dynamic_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["init-project", "--root", tmp])

            code, payload = self.run_cli(["publish", "wiki", "render", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["page"], "Test status (Siri)")
            report = (root / ".qa-aist-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            for heading in [
                "# Test status (Siri)",
                "## 總覽",
                "## 測試結果明細",
                "## 補充 partial probes（不併入正式 case counters）",
                "## 活動中的 Gitea issues",
                "## 已關閉／歷史 issues（不列 active blocker）",
                "## 六色帽回顧",
            ]:
                self.assertIn(heading, report)
            self.assertIn("### CLI Smoke", report)
            self.assertTrue((root / ".qa-aist-project" / "rules" / "wiki-categories.yaml").exists())

    def test_cases_generate_init_auto_plans_wiki_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["init-project", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--init", "--generated_count", "2", "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["wiki"]["auto_sync"]["event"], "case_generation")
            self.assertEqual(payload["wiki"]["auto_sync"]["status"], "blocked")
            self.assertTrue((root / ".qa-aist-project" / "state" / "wiki-plan.json").exists())
            report = (root / ".qa-aist-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            self.assertIn("NEEDS_INPUT", report)
            self.assertNotIn("PASS：1", report)

    def test_qa_test_run_one_auto_plans_test_result_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["init-project", "--root", tmp])

            code, payload = self.run_cli(["qa-test", "run-one", "--root", tmp, "--json", "EXAMPLE-001"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["wiki"]["auto_sync"]["event"], "test_result")
            self.assertTrue((root / ".qa-aist-project" / "state" / "latest-run.json").exists())
            self.assertTrue((root / ".qa-aist-project" / "state" / "wiki-plan.json").exists())
            report = (root / ".qa-aist-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            self.assertIn("| EXAMPLE-001 | CLI Smoke | PASS |", report)

    def test_publish_wiki_apply_blocks_mcp_backend_and_missing_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_mcp_project(tmp)
            self.write_issue_case(root)
            self.run_cli(["qa-test", "run-one", "--root", tmp, "ISSUE-1"])

            code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "blocked")
            self.assertIn("gitea_mcp_write_not_supported", plan["blocked_reasons"])

            code, apply_result = self.run_cli(["publish", "wiki", "apply", "--root", tmp, "--json"])
            self.assertEqual(code, 4)
            self.assertEqual(apply_result["status"], "blocked")
            self.assertIn("gitea_mcp_write_not_supported", apply_result["blocked_reasons"])

    def test_publish_wiki_apply_http_token_updates_only_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_issue_case(root)
            self.run_cli(["qa-test", "run-one", "--root", tmp, "ISSUE-1"])

            with patch.dict(os.environ, {"QA_AIST_GITEA_TOKEN": "test-token"}, clear=False):
                with patch("qa_aist.wiki.GiteaClient.update_wiki_page", return_value={"ok": True}) as update_wiki:
                    code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
                    self.assertEqual(code, 0)
                    self.assertEqual(plan["status"], "ready")

                    code, apply_result = self.run_cli(["publish", "wiki", "apply", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(apply_result["status"], "ok")
            self.assertEqual(apply_result["applied"][0]["type"], "wiki_update")
            update_wiki.assert_called_once()

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

            renamed = hermes.dispatch_chat_command("/qa-aist cases generate --from-issues", root=root)
            self.assertEqual(renamed["status"], "error")
            self.assertEqual(renamed["payload"]["error"], "renamed_to_growing")

            mode_required = hermes.dispatch_chat_command("/qa-aist cases generate", root=root)
            self.assertEqual(mode_required["status"], "error")
            self.assertEqual(mode_required["payload"]["error"], "explicit_generation_mode_required")
            self.assertEqual(mode_required["payload"]["next_actions"][0]["command"], "/qa-aist cases generate --init")

            init = hermes.dispatch_chat_command('/qa-aist cases generate --init --feature "CLI help" --profile cli --generated_count 2', root=root)
            self.assertEqual(init["status"], "needs_input")
            self.assertEqual(init["payload"]["source"], "init")
            self.assertIn("init_context:", init["chat_response"])
            self.assertIn("missing_inputs:", init["chat_response"])
            self.assertTrue(init["payload"]["input_required"])
            self.assertEqual(init["payload"]["interaction"]["type"], "needs_input")
            self.assertEqual(init["payload"]["interaction"]["handler"], "clarify")
            self.assertEqual(init["payload"]["hermes_needs_input"]["status"], "required")
            self.assertEqual(init["payload"]["hermes_needs_input"]["title"], "QA-AIST clarify")
            self.assertEqual(init["payload"]["hermes_needs_input"]["preferred_mechanism"], "clarify")
            self.assertTrue(init["payload"]["hermes_needs_input"]["questions"])
            self.assertLessEqual(len(init["payload"]["hermes_needs_input"]["questions"]), 2)
            self.assertNotIn("Hermes needs your input", init["chat_response"])
            self.assertIn("需要補充資訊", init["chat_response"])
            self.assertTrue(any(action.get("kind") == "ask_user" for action in init["payload"]["next_actions"]))

            fast = hermes.dispatch_chat_command('/qa-aist cases generate --init --feature "CLI help" --profile cli --fast --generated_count 1', root=root)
            self.assertEqual(fast["status"], "ok")
            self.assertFalse(fast["payload"].get("input_required", False))
            self.assertNotIn("hermes_needs_input", fast["payload"])

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
