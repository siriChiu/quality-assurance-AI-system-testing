from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

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
        self.run_cli(["setup", "--root", tmp])
        self.write_hermes_mcp_status(root)
        return root

    def init_gitea_mcp_project(self, tmp: str) -> Path:
        return self.init_gitea_project(tmp)

    def write_hermes_mcp_status(self, root: Path, servers: list[str] | None = None) -> None:
        status_path = root / ".qa-aist-project" / "state" / "hermes-mcp" / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({"servers": servers or ["gitea", "redmine"]}), encoding="utf-8")

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

    def write_redmine_issues(self, root: Path) -> Path:
        issues = [
            {
                "id": 144780,
                "subject": "Sensor list fails after cold boot",
                "description": "After cold boot, sensor collection returns an empty list for one BMC.",
                "status": {"name": "New"},
                "tracker": {"name": "Bug"},
                "project": {"name": "IRCTool"},
                "updated_on": "2026-06-08T12:00:00Z",
                "url": "https://redmine.example.test/issues/144780",
            },
            {
                "id": 144693,
                "subject": "CLI help omits virtual media options",
                "description": "The help output should include SMB virtual media flags.",
                "status": {"name": "Assigned"},
                "tracker": {"name": "Bug"},
                "project": {"name": "IRCTool"},
                "updated_on": "2026-06-08T13:00:00Z",
                "url": "https://redmine.example.test/issues/144693",
            },
        ]
        path = root / ".qa-aist-project" / "state" / "redmine-mcp" / "issues.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"content": [{"type": "text", "text": json.dumps({"issues": issues})}]}), encoding="utf-8")
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
            self.assertNotIn("QA_AIST_" + "TRACKER_TOKEN", payload["message"])

    def test_issues_sync_redmine_issues_writes_local_mirrors_and_gitea_create_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_redmine_issues(root)

            code, payload = self.run_cli([
                "issues",
                "sync",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "144693",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_mcp_apply")
            self.assertEqual(payload["source"], "redmine_mcp")
            self.assertEqual(payload["mode"], "redmine_issues")
            self.assertEqual(payload["imported_issue_ids"], [144780, 144693])
            self.assertEqual(payload["gitea_issue_candidate_count"], 2)
            self.assertEqual(payload["remote_write"], "needs_mcp_apply")
            self.assertEqual(payload["blocked_by_gate"], 0)
            self.assertTrue((root / ".qa-aist-project" / "issues" / "redmine-144780.md").exists())
            self.assertTrue((root / ".qa-aist-project" / "issues" / "gitea-candidates" / "redmine-144780.md").exists())
            self.assertTrue((root / ".qa-aist-project" / "state" / "redmine-import.json").exists())
            state = json.loads((root / ".qa-aist-project" / "state" / "redmine-gitea-sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["schema"], "qa-aist.redmine-gitea-sync-state.v1")
            self.assertEqual(state["issue_candidates"][0]["action"], "create_gitea_issue_candidate")
            self.assertTrue(state["issue_candidates"][0]["write_gate_result"]["allowed"])
            self.assertIn("Redmine #144780", state["issue_candidates"][0]["body"])
            request_path = root / payload["mcp_issue_write_request_path"]
            self.assertTrue(request_path.exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["schema"], "qa-aist.gitea-mcp-issue-write-request.v1")
            self.assertEqual(request["operation"], "gitea.issue.sync_from_redmine")
            self.assertEqual(request["safety"]["allowed_targets"], ["issues"])
            self.assertEqual(request["actions"][0]["operation"], "gitea.issue.create")
            self.assertEqual(request["actions"][0]["redmine_issue_id"], 144780)
            self.assertFalse((root / ".qa-aist-project" / "cases" / "REDMINE-144780.yaml").exists())

    def test_cases_generate_redmine_issues_uses_multiple_ids_directly_without_gitea_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_redmine_issues(root)

            code, payload = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "144693",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["source"], "redmine")
            self.assertEqual(payload["imported_issue_ids"], [144780, 144693])
            self.assertEqual(payload["generated_count"], 2)
            self.assertEqual(payload["remote_write"], "not_applicable")
            self.assertNotIn("gitea_sync_plan_path", payload)
            self.assertNotIn("gitea_sync_state_path", payload)
            self.assertNotIn("gitea_issue_candidates", payload)
            self.assertTrue((root / ".qa-aist-project" / "cases" / "REDMINE-144780.yaml").exists())
            self.assertTrue((root / ".qa-aist-project" / "cases" / "REDMINE-144693.yaml").exists())
            case_yaml = load_yaml(root / ".qa-aist-project" / "cases" / "REDMINE-144780.yaml")
            self.assertEqual(case_yaml["source"]["redmine_issue_id"], 144780)
            self.assertNotIn("gitea_sync_plan", case_yaml["source"])
            self.assertEqual(case_yaml["commands"][0]["id"], "safe_probe")

    def test_cases_generate_growing_review_validate_and_runs_safe_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            code, generated = self.run_cli(["cases", "generate", "--root", tmp, "--growing", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertEqual(generated["source"], "growth")
            self.assertEqual(generated["mode"], "growing")
            self.assertGreaterEqual(generated["growth_seed_count"], 1)
            self.assertTrue((root / ".qa-aist-project" / "state" / "growth-context.json").exists())
            self.assertEqual(generated["interaction_scope"], "autonomous")
            self.assertEqual(generated["generated"][0]["question_count"], 0)
            self.assertEqual(generated["missing_input_count"], 0)
            self.assertEqual(generated["advisory_input_count"], 0)
            first_case = generated["generated"][0]["case_id"]
            first_yaml = load_yaml(root / generated["generated"][0]["path"])
            self.assertEqual(first_yaml["source"]["type"], "growth")
            self.assertFalse(first_yaml["qa_aist"]["review_required_before_run"])
            self.assertEqual(first_yaml["commands"][0]["id"], "safe_probe")
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
            self.assertEqual(review["draft_count"], 0)

            code, validated = self.run_cli(["cases", "validate", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(validated["case_count"], 1 + generated["generated_count"])

            code, run_one = self.run_cli(["cases", "run", "--root", tmp, "--json", first_case])
            self.assertEqual(code, 0)
            self.assertEqual(run_one["status"], "PASS")
            self.assertTrue(run_one["results"][0]["evidence"])

    def test_cases_generate_requires_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "explicit_generation_mode_required")
            self.assertIn("/qa-aist cases generate --init", payload["choices"])
            self.assertIn("/qa-aist cases generate --growing", payload["choices"])

    def test_cases_generate_init_analyzes_repo_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
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
                "--count",
                "5",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertEqual(generated["source"], "init")
            self.assertEqual(generated["mode"], "init")
            self.assertEqual(generated["generation_limit"], "manual_generated_count_cap")
            self.assertEqual(generated["requested_generated_count"], 5)
            self.assertEqual(generated["requested_count"], 5)
            self.assertEqual(generated["interaction_scope"], "autonomous")
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
            self.assertFalse(first_yaml["qa_aist"]["review_required_before_run"])
            self.assertTrue(first_yaml["qa_aist"]["executable"])
            self.assertEqual(first_yaml["qa_aist"]["questions"], [])
            self.assertEqual(first_yaml["commands"][0]["id"], "safe_probe")
            self.assertIn("risk_controls", first_yaml)
            self.assertIn("init_seed", first_yaml)

            code, review = self.run_cli(["cases", "review", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(review["draft_count"], 0)

            code, validated = self.run_cli(["cases", "validate", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(validated["case_count"], 6)

            code, removed_dry_run = self.run_cli(["qa-test", "dry-run", "--root", tmp, "--case-id", first_case, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(removed_dry_run["error"], "command_removed")
            self.assertEqual(removed_dry_run["replacement"], "/qa-aist cases run")

            code, run_one = self.run_cli(["cases", "run", "--root", tmp, "--json", first_case])
            self.assertEqual(code, 0)
            self.assertEqual(run_one["status"], "PASS")
            self.assertEqual(run_one["results"][0]["commands"][0]["id"], "safe_probe")

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
                "--count",
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
                "--count",
                "5",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertTrue(generated["fast"])
            self.assertEqual(generated["interaction_scope"], "autonomous")
            self.assertEqual(generated["missing_input_count"], 0)
            self.assertEqual(generated["advisory_input_count"], 0)
            self.assertEqual(generated["questions"], [])
            self.assertEqual(generated["requested_generated_count"], 5)
            self.assertTrue(generated["fast_mode_assumptions"])

    def test_cases_generate_from_issues_returns_removed_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--from-issues", "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "command_removed")
            self.assertEqual(payload["replacement"], "/qa-aist cases generate --growing")

    def test_cases_generate_candidate_json_is_removed_public_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
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
            self.assertEqual(payload["error"], "command_removed")
            self.assertEqual(payload["replacement"], "/qa-aist cases generate --growing")

    def test_policy_pack_is_generic_closed_loop_swqa(self) -> None:
        payload = policy_pack()
        self.assertEqual(payload["closed_loop_steps"], ["Observe", "Normalize", "Execute", "Triage", "Publish", "Evolve", "Prune"])
        for dimension in ["exact_reproduction", "functional", "positive", "negative", "boundary", "invalid_input", "sibling_surface", "side_effect_safe", "stress_timeout_risk"]:
            self.assertIn(dimension, payload["swqa_dimensions"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for project_only_word in ["irctool", "Redfish", "VM_HTTP_URL", "GID-Ubuntu"]:
            self.assertNotIn(project_only_word, serialized)

    def test_publish_wiki_plan_and_issues_fix_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])
            self.write_issue_case(root)
            self.run_cli(["close-loop", "run-once", "--root", tmp, "--case-id", "ISSUE-1"])

            code, removed_publish = self.run_cli(["publish", "plan", "--root", tmp, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(removed_publish["error"], "command_removed")
            self.assertEqual(removed_publish["replacement"], "/qa-aist publish wiki plan")

            code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["blocked_by_gate"], 0)
            self.assertEqual(plan["blocked_reasons"], [])
            self.assertTrue((root / ".qa-aist-project" / "state" / "wiki-plan.json").exists())

            code, apply_result = self.run_cli(["publish", "apply", "--root", tmp, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(apply_result["error"], "command_removed")
            self.assertEqual(apply_result["replacement"], "/qa-aist publish wiki apply")

            code, fix_plan = self.run_cli(["issues", "fix", "--root", tmp, "--issue", "1", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(fix_plan["status"], "handoff")
            self.assertIn("ISSUE-1", fix_plan["case_ids"])

            code, removed_fix = self.run_cli(["fix-issues", "plan", "--root", tmp, "--issue", "1", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(removed_fix["error"], "command_removed")
            self.assertEqual(removed_fix["replacement"], "/qa-aist issues fix --issue <id>")

    def test_publish_wiki_render_uses_fixed_sections_and_dynamic_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--json"])

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
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--init", "--count", "2", "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["wiki"]["auto_sync"]["event"], "case_generation")
            self.assertEqual(payload["wiki"]["auto_sync"]["status"], "blocked")
            self.assertTrue((root / ".qa-aist-project" / "state" / "wiki-plan.json").exists())
            report = (root / ".qa-aist-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            self.assertIn("NOT_RUN", report)
            self.assertNotIn("PASS：1", report)

    def test_qa_test_run_one_auto_plans_test_result_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["cases", "run", "--root", tmp, "--json", "EXAMPLE-001"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["wiki"]["auto_sync"]["event"], "test_result")
            self.assertTrue((root / ".qa-aist-project" / "state" / "latest-run.json").exists())
            self.assertTrue((root / ".qa-aist-project" / "state" / "wiki-plan.json").exists())
            report = (root / ".qa-aist-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            self.assertIn("| EXAMPLE-001 | CLI Smoke | PASS |", report)

    def test_publish_wiki_apply_mcp_backend_creates_gated_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_mcp_project(tmp)
            self.write_issue_case(root)
            self.run_cli(["cases", "run", "--root", tmp, "ISSUE-1"])

            code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["remote"]["backend"], "mcp")
            self.assertTrue(plan["remote"]["requires_hermes_mcp_apply"])
            self.assertNotIn("gitea_mcp_write_not_supported", plan["blocked_reasons"])

            code, apply_result = self.run_cli(["publish", "wiki", "apply", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(apply_result["status"], "needs_mcp_apply")
            self.assertEqual(apply_result["remote"]["write_backend"], "hermes_gitea_mcp")
            request_path = root / apply_result["mcp_write_request_path"]
            self.assertTrue(request_path.exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["schema"], "qa-aist.gitea-mcp-wiki-write-request.v1")
            self.assertEqual(request["operation"], "gitea.wiki.update_page")
            self.assertIsNone(request["repo"])
            self.assertEqual(request["repo_source"], "hermes_session")
            self.assertEqual(request["page"], "Test status (Siri)")
            self.assertIn("## 總覽", request["body"])
            self.assertEqual(request["safety"]["allowed_targets"], ["wiki"])

            result_path = root / apply_result["mcp_write_result_path"]
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps({"status": "ok", "url": "https://git.example.test/wiki/Test-status"}), encoding="utf-8")

            code, completed = self.run_cli([
                "publish",
                "wiki",
                "complete-mcp",
                "--root",
                tmp,
                "--result-json",
                str(result_path),
                "--json",
            ])

            self.assertEqual(code, 2)
            self.assertEqual(completed["status"], "error")
            self.assertEqual(completed["error"], "command_removed")
            self.assertEqual(completed["replacement"], "/qa-aist publish wiki apply")

    def test_publish_wiki_apply_uses_mcp_handoff_not_internal_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_issue_case(root)
            self.run_cli(["cases", "run", "--root", tmp, "ISSUE-1"])

            code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "ready")
            self.assertNotIn("token_env", plan["remote"])
            self.assertNotIn("token_present", plan["remote"])

            code, apply_result = self.run_cli(["publish", "wiki", "apply", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(apply_result["status"], "needs_mcp_apply")
            self.assertEqual(apply_result["mcp_write_request"]["repo_source"], "hermes_session")

    def test_hermes_new_workflow_commands_and_qa_test_without_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)

            help_result = hermes.dispatch_chat_command("/qa-aist qa-test", root=root)
            self.assertEqual(help_result["status"], "error")
            self.assertEqual(help_result["payload"]["error"], "command_removed")
            self.assertEqual(help_result["payload"]["replacement"], "/qa-aist cases run")
            self.assertIn("下一步可以選", help_result["chat_response"])
            self.assertTrue(help_result["payload"]["next_actions"])

            sync = hermes.dispatch_chat_command(f"/qa-aist issues sync --issues-json {issues_json}", root=root)
            self.assertEqual(sync["status"], "ok")
            self.assertEqual(sync["payload"]["open_count"], 1)
            self.assertEqual(sync["payload"]["next_actions"][0]["command"], "/qa-aist cases generate --growing")

            renamed = hermes.dispatch_chat_command("/qa-aist cases generate --from-issues", root=root)
            self.assertEqual(renamed["status"], "error")
            self.assertEqual(renamed["payload"]["error"], "command_removed")
            self.assertEqual(renamed["payload"]["replacement"], "/qa-aist cases generate --growing")

            mode_required = hermes.dispatch_chat_command("/qa-aist cases generate", root=root)
            self.assertEqual(mode_required["status"], "error")
            self.assertEqual(mode_required["payload"]["error"], "explicit_generation_mode_required")
            self.assertEqual(mode_required["payload"]["next_actions"][0]["command"], "/qa-aist cases generate --init")

            init = hermes.dispatch_chat_command('/qa-aist cases generate --init --feature "CLI help" --profile cli --count 2', root=root)
            self.assertEqual(init["status"], "ok")
            self.assertEqual(init["payload"]["source"], "init")
            self.assertIn("init_context:", init["chat_response"])
            self.assertIn("generated_cases:", init["chat_response"])
            self.assertFalse(init["payload"].get("input_required", False))
            self.assertNotIn("interaction", init["payload"])
            self.assertNotIn("hermes_needs_input", init["payload"])
            self.assertNotIn("Hermes needs your input", init["chat_response"])
            self.assertNotIn("需要補充資訊", init["chat_response"])
            self.assertTrue(any(action.get("command") == "/qa-aist cases validate" for action in init["payload"]["next_actions"]))

            fast = hermes.dispatch_chat_command('/qa-aist cases generate --init --feature "CLI help" --profile cli --fast --count 1', root=root)
            self.assertEqual(fast["status"], "error")
            self.assertEqual(fast["payload"]["error"], "command_removed")
            self.assertEqual(fast["payload"]["replacement"], "/qa-aist cases generate --init")

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
            self.assertEqual(status["status"], "error")
            self.assertEqual(status["payload"]["error"], "command_removed")
            self.assertEqual(status["payload"]["replacement"], "/qa-aist doctor")

            doctor = hermes.dispatch_chat_command("/qa-aist doctor", root=tmp)
            self.assertEqual(doctor["status"].lower(), "warn")
            self.assertFalse(doctor["payload"]["issue_sync"]["issue_sync_ready"])
            self.assertIn("gitea_mcp_snapshot_missing", doctor["chat_response"])
            self.assertIn("需要處理", doctor["chat_response"])


if __name__ == "__main__":
    unittest.main()
