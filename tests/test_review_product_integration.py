from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality_pilot.config import ProjectConfig, project_paths
from quality_pilot.review import _browser_result_case, _build_report, _product_result_case, _pytest_failure_details, _render_detailed_text, _render_markdown, _render_reproduction_playbook, _run_comprehensive_review_qa, prepare_gitea_review_reply, review_gate


class ReviewProductIntegrationTest(unittest.TestCase):
    def _config(self, root: Path) -> ProjectConfig:
        return ProjectConfig(root=root, path=root / ".quality-pilot.yaml", data={"runtime": {}}, paths=project_paths(root))

    def test_review_gate_blocks_non_green_review_even_when_report_generation_succeeds(self) -> None:
        gate = review_gate({"status": "ok", "conclusion": "TEST_FAILURE_REQUIRES_TRIAGE"})
        self.assertEqual(gate["status"], "BLOCKED")
        self.assertFalse(gate["execution_allowed"])
        self.assertFalse(gate["merge_allowed"])

    def test_review_gate_keeps_clean_review_human_owned(self) -> None:
        gate = review_gate({"status": "ok", "conclusion": "NO_BLOCKING_FINDINGS"})
        self.assertEqual(gate["status"], "HUMAN_GATE_REQUIRED")
        self.assertTrue(gate["execution_allowed"])
        self.assertFalse(gate["merge_allowed"])

    def test_product_block_is_reported_and_advisory_comment_can_be_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            qa = {
                "schema": "quality-pilot.review-qa.v1",
                "mode": "comprehensive",
                "product_test": {
                    "status": "BLOCK",
                    "reason": "product_test_contract_missing",
                    "result_path": ".quality-pilot-project/evidence/product-test-result.json",
                },
                "matrix": {
                    "white_box": {"status": "PASS"},
                    "product_binary": {"status": "BLOCK", "reason": "product_test_contract_missing"},
                },
                "outcome": "BLOCK",
            }
            report = _build_report(
                config,
                repo="owner/repo",
                pr_number=1,
                snapshot={"head_sha": "head", "base_sha": "base", "diff": "diff", "changed_files": []},
                snapshot_path="snapshot.json",
                diff_hash="diff-hash",
                diff_info={"status": "PASS"},
                worktree={"status": "ready", "path": str(root)},
                test_selection={"selected": [{"id": "regression"}], "coverage_gap": False, "unavailable": []},
                test_results=[{"id": "regression", "status": "PASS"}],
                findings=[],
                qa_report=qa,
                dry_run=False,
            )
            self.assertEqual(report["product_test_outcome"], "BLOCK")
            self.assertEqual(report["conclusion"], "HOLD_FOR_PRODUCT_TEST_COVERAGE")
            self.assertEqual(report["browser_evidence"], [])
            developer = report["developer_review"]
            self.assertEqual(developer["schema"], "quality-pilot.developer-code-review.v1")
            self.assertEqual(developer["decision"], "COMMENT")
            self.assertGreaterEqual(developer["summary"]["should_fix"], 1)
            self.assertIn("zh-TW", developer["localized_summary"])
            reply = prepare_gitea_review_reply(config, report, report_hash="hash", confirm=True, dry_run=False)
            self.assertEqual(reply["status"], "local_only_pending")
            self.assertEqual(reply["review_state"], "COMMENT")
            self.assertEqual(reply["approval_decision"], "USER_DECISION_REQUIRED")
            self.assertFalse(reply["remote_apply"])
            request = json.loads((root / reply["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["state"], "COMMENT")
            self.assertTrue(request["advisory_only"])
            self.assertIn("不是核准", request["body"])
            self.assertIn("product-test-contract", {item["id"] for item in request["recommendations"]})

    def test_pytest_failure_details_are_single_test_reproducible(self) -> None:
        output = """________________ test_run ________________\n\n> page.get_by_role(\"button\").click()\nE   playwright.sync_api.TimeoutError: Locator.click: Timeout 10000ms exceeded.\nE     - waiting for element to be visible, enabled and stable\n================ short test summary info ================\nFAILED tests/browser_ui/test_ui.py::test_run\n"""
        details = _pytest_failure_details(output, ["tests/browser_ui/test_ui.py::test_run"], command=".venv/bin/python -m pytest tests/browser_ui -q")
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["category"], "playwright_actionability_timeout")
        self.assertEqual(details[0]["reproduce"], ".venv/bin/python -m pytest tests/browser_ui/test_ui.py::test_run -q")

    def test_detailed_text_includes_browser_evidence_and_user_owned_merge_decision(self) -> None:
        report = {
            "repo": "owner/repo",
            "pr_number": 1,
            "test_outcome": "FAIL",
            "product_test_outcome": "HOLD",
            "browser_ui_outcome": "FAIL",
            "qa_outcome": "FAIL",
            "conclusion": "TEST_FAILURE_REQUIRES_TRIAGE",
            "qa_review": {
                "product_test": {
                    "case_id": "PR-1-PRODUCT",
                    "status": "HOLD",
                    "reason": "browser_probe_only_no_semantic_state_assertion",
                    "execution_target": "remote_ssh",
                    "evidence_origin": "remote",
                    "browser": {
                        "case_id": "PR-1-PRODUCT-BROWSER-UI",
                        "status": "FAIL",
                        "reason": "browser_interaction_timeout:PRODUCT_UI_FAILURE",
                        "execution_target": "remote_ssh",
                        "evidence_origin": "remote",
                        "interaction_count": 1,
                        "positive_assertion_count": 1,
                        "state_assertion_count": 1,
                        "evidence": {"screenshot": "browser/failure.png", "trace": "browser/failure.trace.zip"},
                    },
                },
                "browser_regression_case": {
                    "case_id": "PR-BROWSER-UI-REGRESSION",
                    "status": "FAIL",
                    "reason": "test_command_failed",
                    "execution_target": "local_pinned_worktree",
                    "evidence_origin": "local",
                    "commands": [{"command": "pytest tests/browser_ui -q", "exit_code": 1}],
                    "evidence": ["browser/stdout.log", "browser/stderr.log"],
                },
                "matrix": {},
            },
            "test_results": [{
                "id": "diff-targeted-pytest", "status": "FAIL", "reason": "test_command_failed",
                "exit_code": 1, "failed_tests": ["tests/browser_ui/test_settings.py::test_run"],
                "pytest_summary": "1 failed, 2 passed",
            }],
            "findings": [],
            "developer_review": {"summary": {}, "sections": {}, "evidence": {"test_results": []}},
        }
        text = _render_detailed_text(report)
        self.assertIn("Playwright／產品測試執行證據", text)
        self.assertIn("screenshot：browser/failure.png（失敗截圖）", text)
        self.assertIn("PR 合併決定：由 PR 擁有者決定", text)
        self.assertIn("tests/browser_ui/test_settings.py::test_run", text)
        comment = __import__("quality_pilot.review", fromlist=["_review_comment_body"])._review_comment_body(report, [])
        self.assertIn("PR 合併決定仍由 PR 擁有者負責", comment)
        self.assertIn("截圖=有", comment)

    def test_reproduction_playbook_uses_confirmed_or_candidate_data_not_product_specific_steps(self) -> None:
        candidate_report = {
            "qa_review": {
                "product_test": {
                    "plan": {
                        "web_ui": {
                            "candidate_steps": [
                                {"action": "expect_visible", "summary": "expect configured settings tab", "source": "tests/ui.py", "line": 12}
                            ]
                        },
                        "candidate_commands": ["python product_entry.py --check"],
                    }
                }
            }
        }
        candidate_text = "\n".join(_render_reproduction_playbook(candidate_report))
        self.assertIn("expect configured settings tab", candidate_text)
        self.assertIn("只能用來設計與確認", candidate_text)
        self.assertNotIn("PID Settings", candidate_text)
        self.assertNotIn("Run auto_PID_tool", candidate_text)

        confirmed_report = {
            "qa_review": {
                "product_test": {
                    "confirmed_browser_steps": [
                        {"action": "expect_visible", "locator": {"role": "tab", "name": "Configured Product Tab"}}
                    ]
                }
            }
        }
        confirmed_text = "\n".join(_render_reproduction_playbook(confirmed_report))
        self.assertIn("Configured Product Tab", confirmed_text)
        self.assertIn("已確認的 Browser steps", confirmed_text)

    def test_detailed_text_contains_complete_qa_matrix_and_final_result(self) -> None:
        dimensions = ("white_box", "functional", "black_box", "boundary", "stress", "documentation", "product_binary", "browser_ui", "ui", "ux")
        report = {
            "repo": "owner/repo",
            "pr_number": 1,
            "test_outcome": "NOT_RUN",
            "product_test_outcome": "NOT_EVALUATED",
            "browser_ui_outcome": "NOT_EVALUATED",
            "qa_outcome": "NOT_RUN",
            "conclusion": "HOLD_FOR_TEST_COVERAGE",
            "qa_review": {"matrix": {dimension: {"status": "NOT_RUN", "reason": "not_configured"} for dimension in dimensions}},
            "findings": [],
        }
        text = _render_detailed_text(report)
        for dimension in dimensions:
            self.assertIn(f"`{dimension}`", text)
        self.assertIn("工程師可直接複製的重現手冊", text)
        self.assertIn("最終結果（請以本節作為摘要）", text)
        self.assertIn("PR 合併決定：由 PR 擁有者決定", text)

    def test_detailed_text_report_contains_reproduction_and_redacted_context(self) -> None:
        report = {
            "repo": "owner/repo", "pr_number": 1, "head_sha": "head",
            "test_outcome": "BLOCK", "product_test_outcome": "BLOCK", "qa_outcome": "BLOCK",
            "conclusion": "REQUEST_CHANGES",
            "findings": [{
                "id": "secret-1", "severity": "CRITICAL", "category": "security",
                "path": "src/app.py", "line": 10, "message": "secret-like material",
                "code_context": "token = [REDACTED:credential_assignment]",
                "evidence": {"kind": "credential_assignment"},
                "recommendation": "Use an environment variable",
                "reproducibility": {"steps": ["Run the pinned scan"], "expected": "finding", "actual": "found", "evidence": "diff"},
            }],
            "developer_review": {
                "summary": {"total_issues": 1, "must_fix": 1, "should_fix": 0, "nice_to_have": 0},
                "sections": {"must_fix": [], "should_fix": [], "nice_to_have": [], "verification": []},
                "evidence": {"test_results": []},
                "collaboration": {"status": "NOT_RUN", "reason": "unavailable"},
            },
        }
        text = _render_detailed_text(report)
        self.assertIn("確定性問題詳細內容", text)
        self.assertIn("程式碼片段（已遮蔽敏感內容）", text)
        self.assertIn("重現步驟", text)
        self.assertIn("建議修補方式", text)
        self.assertIn("修補後驗證方式", text)
        self.assertNotIn("raw-secret-value", text)

    def test_product_and_browser_results_are_traceable_cases(self) -> None:
        product = {"case_id": "PR-28-PRODUCT", "run_id": "run-28", "contract_hash": "hash-28", "status": "PASS", "result_path": "product.json", "browser": {"case_id": "PR-28-PRODUCT-BROWSER-UI", "run_id": "run-28", "contract_identity_hash": "hash-28", "status": "PASS", "interaction_count": 2, "state_assertion_count": 1, "evidence": {"screenshot": "screen.png", "trace": "trace.zip"}, "result_path": "browser.json"}}
        product_case = _product_result_case(product, repo="owner/repo", pr_number=28, head_sha="head", root=Path("."))
        browser_case = _browser_result_case(product, parent_case_id="PR-28-PRODUCT", root=Path("."))
        self.assertEqual(product_case["case_id"], "PR-28-PRODUCT")
        self.assertEqual(product_case["run_id"], "run-28")
        self.assertEqual(browser_case["case_type"], "playwright_ui")
        self.assertEqual(browser_case["dimensions"], ["black_box", "functional", "ui", "ux"])
        self.assertEqual(browser_case["contract_hash"], "hash-28")

    def test_canonical_product_case_writes_contract_and_result_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root)
            contract = __import__("quality_pilot.product_case_adapter", fromlist=["build_product_case_contract"]).build_product_case_contract(config, case_id="PR-1-PRODUCT", title="product", review_id="run-1", snapshot={"head_sha": "head"})
            self.assertEqual(contract.raw["case_type"], "product")
            self.assertTrue((config.paths.cases / "PR-1-PRODUCT.yaml").exists())

    def test_markdown_renderer_handles_developer_review_evidence(self) -> None:
        markdown = _render_markdown({
            "repo": "owner/repo",
            "pr_number": 1,
            "base_ref": "main",
            "head_sha": "head",
            "test_results": [{"id": "test", "status": "PASS", "command": "python -m pytest tests -q"}],
            "product_test_outcome": "BLOCK",
            "browser_ui_outcome": "NOT_RUN",
            "qa_outcome": "BLOCK",
            "conclusion": "HOLD_FOR_PRODUCT_TEST_COVERAGE",
            "developer_review": {
                "summary": {"total_issues": 1, "must_fix": 0, "should_fix": 1, "nice_to_have": 0},
                "sections": {"must_fix": [], "should_fix": [{"id": "x", "severity": "HIGH", "status": "HOLD", "recommendation": "Add an oracle", "verification": "Run it"}], "nice_to_have": [], "verification": []},
                "localized_summary": {"en": {}, "zh-TW": {}},
                "collaboration": {"status": "NOT_RUN", "mode": "candidate-only", "reason": "unavailable"},
                "evidence": {"test_results": [{"id": "test", "status": "PASS", "command": "python -m pytest tests -q", "reproduction": {"steps": ["run command"], "expected": "exit 0", "actual": "exit 0", "evidence": ["stdout.log"]}}]},
            },
            "findings": [],
            "qa_review": {"matrix": {}},
            "remote_reply": {"preview": {}},
        })
        self.assertIn("測試執行證據", markdown)
        self.assertIn("應該修復", markdown)
        self.assertNotIn("Multi-Agent Debate / Peer Review", markdown)
        self.assertNotIn("Detailed Review (English)", markdown)

    def test_comprehensive_review_calls_product_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("# product\n", encoding="utf-8")
            config = self._config(root)
            product = {"status": "BLOCK", "reason": "product_test_contract_missing", "result_path": "product.json"}
            with patch("quality_pilot.product_case_adapter.run_product_tests", return_value=product) as adapter, patch(
                "quality_pilot.review.generate_cases_init", return_value={"status": "ok", "generated": []}
            ), patch("quality_pilot.review.load_contracts", return_value=[]):
                result = _run_comprehensive_review_qa(
                    config,
                    snapshot={"title": "PR", "head_sha": "head", "base_sha": "base", "diff": "diff", "changed_files": []},
                    worktree={"status": "ready", "path": str(root), "source": str(root)},
                    repo="owner/repo",
                    pr_number=1,
                    head_sha="head",
                    timeout_seconds=30,
                    dry_run=False,
                )
            adapter.assert_called_once()
            self.assertEqual(result["product_test"]["status"], "BLOCK")
            self.assertEqual(result["matrix"]["product_binary"]["status"], "BLOCK")
            self.assertEqual(result["cases"][0]["case_type"], "product")
            self.assertTrue(result["cases"][0]["case_id"].startswith("PR-1-"))


if __name__ == "__main__":
    unittest.main()
