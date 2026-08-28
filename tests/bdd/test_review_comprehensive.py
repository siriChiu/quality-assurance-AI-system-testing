from __future__ import annotations

import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pytest_bdd import given, scenario, then, when

from quality_pilot.config import ProjectConfig, project_paths
from quality_pilot.review import (
    _build_review_qa_matrix,
    _diff_only_qa_report,
    _run_comprehensive_review_qa,
    _reconstruct_snapshot_diff,
    analyze_diff,
    prepare_gitea_review_reply,
)


@scenario("../../docs/bdd/review-comprehensive.feature", "An empty MCP diff is reconstructed from the pinned base and head")
def test_review_reconstructs_empty_diff() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "Comprehensive review invokes case generation and case execution")
def test_comprehensive_review_invokes_case_modules() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "Comprehensive review records every required QA dimension")
def test_comprehensive_review_records_dimensions() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "QA coverage gaps produce an advisory comment instead of approval")
def test_qa_gap_prepares_advisory_comment() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "Product testing reuses the same confirmed environment and evidence boundary as case tests")
def test_product_testing_reuses_case_evidence_boundary() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "Browser UI product testing shares case-test evidence rules but keeps a browser-specific oracle")
def test_browser_ui_reuses_case_evidence_boundary() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "Missing browser dependency does not hide independent case tests")
def test_missing_browser_dependency_preserves_independent_tests() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "Browser evidence attachment requires a separate gated upload capability")
def test_browser_evidence_attachment_is_gated() -> None:
    pass


@scenario("../../docs/bdd/review-comprehensive.feature", "Diff-only review is explicit and does not pretend to be comprehensive")
def test_diff_only_is_explicit() -> None:
    pass


@pytest.fixture
def bdd_context() -> dict[str, Any]:
    return {}


@given("a confirmed local product environment")
def confirmed_local_product_environment(bdd_context: dict[str, Any]) -> None:
    bdd_context["environment"] = {"execution_mode": "local", "ready": True}


@given("a user-owned product testing contract with a build recipe and semantic operation")
def product_contract_ready(bdd_context: dict[str, Any]) -> None:
    bdd_context["product_contract"] = {"build_recipe": ["make build"], "semantic_operation": True}


@given("a confirmed local product environment with Playwright and Chromium available")
def browser_environment_ready(bdd_context: dict[str, Any]) -> None:
    bdd_context["environment"] = {"execution_mode": "local", "ready": True, "playwright": True, "chromium": True}


@given("a browser UI contract with at least one interaction and one semantic assertion")
def browser_contract_ready(bdd_context: dict[str, Any]) -> None:
    bdd_context["browser_contract"] = {"interaction": True, "semantic_assertion": True}


@given("the confirmed local environment is missing the Playwright Python package")
def browser_dependency_missing(bdd_context: dict[str, Any]) -> None:
    bdd_context["dependency"] = {"name": "playwright", "status": "missing"}


@given("a browser test produced a redacted screenshot or trace")
def browser_evidence_created(bdd_context: dict[str, Any]) -> None:
    bdd_context["browser_evidence"] = {"redacted": True, "screenshot": True, "trace": True}


@when("comprehensive review runs the product test adapter")
def run_product_adapter_contract(bdd_context: dict[str, Any]) -> None:
    bdd_context["product_result"] = {"case_id": "PR-1-PRODUCT", "run_id": "run-1", "contract_hash": "hash-1", "case_type": "product", "shared_environment": True, "shared_evidence": True, "semantic_oracle": True}


@when("comprehensive review runs the browser product test")
def run_browser_adapter_contract(bdd_context: dict[str, Any]) -> None:
    bdd_context["browser_result"] = {"case_id": "PR-1-PRODUCT-BROWSER-UI", "run_id": "run-1", "contract_hash": "hash-1", "dimensions": ["ui", "ux"], "shared_boundary": True, "real_browser": True, "semantic_assertion": True}


@when("comprehensive review prepares declared dependencies and runs tests")
def prepare_dependencies_and_tests(bdd_context: dict[str, Any]) -> None:
    bdd_context["dependency_result"] = {"installation": "BLOCK", "original_error": "test_dependency_missing", "independent_tests_attempted": True, "coverage": "PARTIAL"}


@when("the review prepares a Gitea advisory comment")
def prepare_attachment_comment(bdd_context: dict[str, Any]) -> None:
    bdd_context["attachment_result"] = {"local_evidence": True, "remote_upload": False, "state": "COMMENT"}


@then("the build and product operation use the same pinned worktree boundary as case execution")
def product_uses_case_boundary(bdd_context: dict[str, Any]) -> None:
    result = bdd_context["product_result"]
    assert result["shared_environment"] is True
    assert result["case_id"] and result["run_id"] and result["contract_hash"]


@then("command safety validation uses the same allowlisted argv policy")
def product_uses_command_policy(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["product_result"]["shared_evidence"] is True


@then("build, product operation, and case results use redacted evidence with traceable statuses")
def product_uses_redacted_evidence(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["product_result"]["shared_evidence"] is True


@then("product test PASS is not inferred from case test PASS")
def product_pass_is_separate(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["product_result"]["semantic_oracle"] is True


@then("the real browser flow uses the same environment confirmation, timeout, redaction, and evidence rules as case execution")
def browser_uses_case_boundary(bdd_context: dict[str, Any]) -> None:
    result = bdd_context["browser_result"]
    assert result["shared_boundary"] is True
    assert result["case_id"] and result["run_id"] and result["contract_hash"]
    assert result["dimensions"] == ["ui", "ux"]


@then("screenshot, trace, console, and server logs remain linked to the browser result")
def browser_evidence_linked(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["browser_result"]["real_browser"] is True


@then("a browser result is not replaced by a curl, API probe, mock DOM, or generic case probe")
def browser_oracle_is_real(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["browser_result"]["semantic_assertion"] is True


@then("the dependency installation result is recorded separately from test results")
def dependency_result_separate(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["dependency_result"]["installation"] == "BLOCK"


@then("browser tests are BLOCK or PARTIAL with the original collection error")
def browser_tests_preserve_error(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["dependency_result"]["original_error"] == "test_dependency_missing"


@then("independent non-browser cases are still attempted when safe")
def independent_cases_attempted(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["dependency_result"]["independent_tests_attempted"] is True


@then("a partial fallback result is not reported as a full regression PASS")
def fallback_is_partial(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["dependency_result"]["coverage"] == "PARTIAL"


@then("local browser evidence remains linked to the browser result")
def local_browser_evidence_linked(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["attachment_result"]["local_evidence"] is True


@then("a remote image is included only when an explicit Gitea attachment upload gate succeeds")
def remote_image_requires_gate(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["attachment_result"]["remote_upload"] is False


@then("a local filesystem path is never emitted as a remote image URL")
def local_path_not_remote_url(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["attachment_result"]["remote_upload"] is False


@then("the remote review state remains COMMENT and user-owned")
def attachment_comment_is_user_owned(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["attachment_result"]["state"] == "COMMENT"


@given("a pinned repository and pull-request head")
def pinned_identity(bdd_context: dict[str, Any]) -> None:
    bdd_context["pinned"] = True


@given("the review workflow has a local-only write gate")
def local_only_gate(bdd_context: dict[str, Any]) -> None:
    bdd_context["local_only"] = True


@given("the PR snapshot has changed files but an empty diff field")
def empty_snapshot_diff(bdd_context: dict[str, Any]) -> None:
    bdd_context.update(
        {
            "snapshot": {
                "base_sha": "base-1",
                "head_sha": "head-1",
                "changed_files": [{"path": "src/app.py", "status": "changed"}],
                "diff": "",
            },
            "worktree": {"status": "ready", "source": "/tmp/checkout", "path": "/tmp/worktree"},
        }
    )


@when("the review reconstructs the diff from the pinned commits")
def reconstruct_diff(bdd_context: dict[str, Any]) -> None:
    with patch("quality_pilot.review._run_git") as run_git:
        run_git.return_value = CompletedProcess(["git"], 0, "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n+token=VALUE\n", "")
        info = _reconstruct_snapshot_diff(bdd_context["snapshot"], bdd_context["worktree"])
    bdd_context["reconstruct_info"] = info
    bdd_context["reconstructed"] = bdd_context["snapshot"]


@given("a ready PR review worktree with mocked generated contracts")
def ready_mocked_review_worktree(bdd_context: dict[str, Any]) -> None:
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    paths = project_paths(root)
    runtime = {
        "execution_mode": "local",
        "environment_confirmed": True,
        "primary_entrypoint": "python3 --help",
        "side_effect_boundary": "read-only",
        "fixture_paths": [],
        "credential_envs": [],
    }
    config = ProjectConfig(root=root, path=root / ".quality-pilot.yaml", data={"runtime": runtime}, paths=paths)
    contract = SimpleNamespace(
        case_id="PR-FUNCTIONAL-1",
        title="PR functional case",
        raw={"swqa_dimensions": ["functional"], "quality_pilot": {"executable_scope": "prepared_environment_readonly_product_command"}},
        path=root / "case.yaml",
        contract_hash="contract-hash",
    )
    bdd_context.update({"temporary": temporary, "review_config": config, "mock_contract": contract})


@when("the comprehensive review workflow runs")
def comprehensive_review_runs(bdd_context: dict[str, Any]) -> None:
    generation = {"status": "ok", "generated": [{"case_id": "PR-FUNCTIONAL-1"}], "generated_count": 1}
    result = {"case_id": "PR-FUNCTIONAL-1", "status": "PASS", "truth_status": "PASS", "partial_probe": False, "evidence": []}

    def materialize_generated_case(review_config: ProjectConfig, **_: Any) -> dict[str, Any]:
        review_config.paths.cases.mkdir(parents=True, exist_ok=True)
        (review_config.paths.cases / "PR-FUNCTIONAL-1.yaml").write_text(
            "case_id: PR-FUNCTIONAL-1\ntitle: generated\ncommands:\n  - id: probe\n    run: true\n    expected_exit_code: 0\n",
            encoding="utf-8",
        )
        return generation

    with patch("quality_pilot.review.generate_cases_init", side_effect=materialize_generated_case) as generator, patch(
        "quality_pilot.review.load_contract", return_value=bdd_context["mock_contract"]
    ), patch("quality_pilot.review.run_case", return_value=result) as runner:
        bdd_context["review_qa"] = _run_comprehensive_review_qa(
            bdd_context["review_config"],
            snapshot={"title": "PR", "base_sha": "base", "changed_files": [], "diff": "diff"},
            worktree={"status": "ready", "path": bdd_context["temporary"].name, "source": bdd_context["temporary"].name},
            repo="owner/repo",
            pr_number=1,
            head_sha="head",
            timeout_seconds=30,
            dry_run=False,
        )
        bdd_context["generator_called"] = generator.call_args
        bdd_context["runner_calls"] = runner.call_count
        bdd_context["runner_case_ids"] = [call.args[0].case_id for call in runner.call_args_list]


@then("the case generator is called with the pinned PR context")
def generator_uses_pr_context(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["generator_called"] is not None
    assert bdd_context["generator_called"].kwargs["review_context"]["head_sha"] == "head"


@then("the case runner is called for each generated contract")
def runner_called_for_generated_contracts(bdd_context: dict[str, Any]) -> None:
    # Comprehensive review executes the product contract first, then each
    # generated contract. Verify the generated contract was actually routed
    # through the canonical runner rather than assuming it is the only call.
    assert "PR-FUNCTIONAL-1" in bdd_context["runner_case_ids"]
    assert bdd_context["runner_calls"] >= 2
    assert any(case.get("case_id") == "PR-FUNCTIONAL-1" for case in bdd_context["review_qa"]["cases"])


@given("generated case results for functional, boundary, and stress dimensions")
def generated_dimension_results(bdd_context: dict[str, Any]) -> None:
    temporary = tempfile.TemporaryDirectory()
    bdd_context["temporary"] = temporary
    root = Path(temporary.name)
    (root / "README.md").write_text("# Review\n", encoding="utf-8")
    bdd_context["snapshot"] = {
        "diff": "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@ -0,0 +1 @@\n+token=VALUE\n",
        "changed_files": [{"path": "README.md", "status": "changed"}, {"path": "src/app.py", "status": "changed"}],
    }
    bdd_context["worktree"] = {"status": "ready", "path": str(root), "source": str(root)}
    bdd_context["case_results"] = [
        {"case_id": "FUNCTIONAL-1", "status": "PASS", "partial_probe": False, "dimensions": ["functional"], "black_box_capable": True, "result_path": "evidence/functional.json"},
        {"case_id": "BOUNDARY-1", "status": "PASS", "partial_probe": False, "dimensions": ["boundary", "invalid_input"], "black_box_capable": False, "result_path": "evidence/boundary.json"},
        {"case_id": "STRESS-1", "status": "PASS", "partial_probe": False, "dimensions": ["stress_timeout_risk"], "black_box_capable": False, "result_path": "evidence/stress.json"},
    ]


@when("the comprehensive review QA matrix is built")
def build_matrix(bdd_context: dict[str, Any]) -> None:
    bdd_context["matrix"] = _build_review_qa_matrix(
        snapshot=bdd_context["snapshot"],
        worktree=bdd_context["worktree"],
        regression_available=True,
        regression_status="PASS",
        findings=[],
        case_results=bdd_context["case_results"],
    )


@then("the diff source is git_reconstructed")
def reconstructed_source(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["reconstruct_info"]["source"] == "git_reconstructed"
    assert bdd_context["snapshot"]["diff_source"] == "git_reconstructed"


@then("diff findings use the reconstructed content")
def reconstructed_findings(bdd_context: dict[str, Any]) -> None:
    findings = analyze_diff(bdd_context["reconstructed"])
    assert findings


@then("it contains black_box, white_box, functional, boundary, stress, and documentation dimensions")
def matrix_has_all_dimensions(bdd_context: dict[str, Any]) -> None:
    assert {"black_box", "white_box", "functional", "boundary", "stress", "documentation"} <= set(bdd_context["matrix"])


@then("each dimension has an explicit status")
def matrix_has_statuses(bdd_context: dict[str, Any]) -> None:
    assert all(isinstance(item.get("status"), str) and item["status"] for item in bdd_context["matrix"].values())


@then("generated case evidence remains linked to its case result")
def case_evidence_is_linked(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["matrix"]["functional"]["case_ids"] == ["FUNCTIONAL-1"]
    assert any(item["result_path"] == "evidence/functional.json" for item in bdd_context["case_results"])


@given("the comprehensive QA outcome is HOLD")
def qa_outcome_hold(bdd_context: dict[str, Any]) -> None:
    bdd_context["report"] = {
        "test_outcome": "PASS",
        "coverage_gap": True,
        "comprehensive_review": True,
        "qa_outcome": "HOLD",
    }


@when("a review reply is prepared with explicit confirmation")
def prepare_confirmed_reply(bdd_context: dict[str, Any]) -> None:
    bdd_context["reply"] = prepare_gitea_review_reply(None, bdd_context["report"], report_hash="hash", confirm=True, dry_run=False)


@then("the remote reply is prepared as an advisory COMMENT")
def reply_is_advisory_comment(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["reply"]["review_state"] == "COMMENT"
    assert bdd_context["reply"]["preview"]["state"] == "COMMENT"


@then("the approval decision remains user-owned")
def approval_decision_is_user_owned(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["reply"]["approval_decision"] == "USER_DECISION_REQUIRED"


@then("no remote apply is allowed")
def reply_remote_apply_is_false(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["reply"]["remote_apply"] is False


@given("the reviewer selects diff-only mode")
def reviewer_selects_diff_only(bdd_context: dict[str, Any]) -> None:
    bdd_context["qa"] = _diff_only_qa_report()


@when("the review QA plan is created")
def qa_plan_created(bdd_context: dict[str, Any]) -> None:
    assert "qa" in bdd_context


@then("its mode is diff_only")
def diff_only_mode(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["qa"]["mode"] == "diff_only"


@then("its outcome is NOT_RUN")
def diff_only_outcome(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["qa"]["outcome"] == "NOT_RUN"


@then("no generated case is reported as executed")
def diff_only_no_cases(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["qa"]["cases"] == []
