from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quality_pilot.cli import main
from quality_pilot.config import load_project_config
from quality_pilot.graph_engineering.review_adapter import ReviewAdapterError, _report_hash, load_review_artifact
from quality_pilot.graph_engineering.workflow import run_graph_task_graph
from quality_pilot.review import pr_snapshot_path


class GraphReviewAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        from contextlib import redirect_stdout
        from io import StringIO

        with redirect_stdout(StringIO()):
            self.assertEqual(main(["setup", "--root", str(self.root)]), 0)
        self.config = load_project_config(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _report(
        self,
        *,
        head_sha: str = "head-1",
        evidence: list[str] | None = None,
        pr_updated_at: str | None = None,
    ) -> dict[str, object]:
        report: dict[str, object] = {
            "schema": "quality-pilot.code-review.v1",
            "repo": "owner/repo",
            "pr_number": 7,
            "base_sha": "base-1",
            "head_sha": head_sha,
            "changed_files": [],
            "qa_review": {
                "schema": "quality-pilot.review-qa.v1",
                "cases": [],
                "matrix": {},
                "outcome": "HOLD",
            },
            "test_results": [],
            "generated_at": "2026-08-20T00:00:00Z",
        }
        if evidence:
            report["test_results"] = [{"id": "regression", "evidence": evidence}]
        if pr_updated_at is not None:
            report["pr_updated_at"] = pr_updated_at
        report["report_hash"] = _report_hash(report)
        return report

    def _write_report(self, report: dict[str, object]) -> Path:
        path = self.root / "review.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot = pr_snapshot_path(self.config, "owner/repo", 7)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(
            json.dumps({"repo": "owner/repo", "pr_number": 7, "head_sha": report["head_sha"]}),
            encoding="utf-8",
        )
        return path

    def _update_snapshot(self, **updates: object) -> None:
        snapshot = pr_snapshot_path(self.config, "owner/repo", 7)
        current = json.loads(snapshot.read_text(encoding="utf-8"))
        current.update(updates)
        snapshot.write_text(json.dumps(current), encoding="utf-8")

    def test_strict_review_projection_requires_matching_report_and_current_head(self) -> None:
        path = self._write_report(self._report())
        report, validation = load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(report["head_sha"], "head-1")
        self.assertEqual(validation["hash_status"], "MATCH")
        self.assertEqual(validation["current_head_status"], "MATCH")

    def test_strict_review_projection_rejects_hash_mismatch(self) -> None:
        report = self._report()
        report["report_hash"] = "bad"
        path = self._write_report(report)
        with self.assertRaises(ReviewAdapterError) as caught:
            load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(caught.exception.error, "review_artifact_hash_invalid")

    def test_strict_review_projection_rejects_stale_head(self) -> None:
        path = self._write_report(self._report(head_sha="head-1"))
        snapshot = pr_snapshot_path(self.config, "owner/repo", 7)
        snapshot.write_text(json.dumps({"repo": "owner/repo", "pr_number": 7, "head_sha": "head-2"}), encoding="utf-8")
        with self.assertRaises(ReviewAdapterError) as caught:
            load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(caught.exception.error, "review_artifact_head_stale")

    def test_strict_review_projection_rejects_missing_evidence(self) -> None:
        path = self._write_report(self._report(evidence=[".quality-pilot-project/evidence/missing.log"]))
        with self.assertRaises(ReviewAdapterError) as caught:
            load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(caught.exception.error, "review_evidence_missing")

    def test_strict_review_projection_rejects_closed_or_merged_pr(self) -> None:
        path = self._write_report(self._report())
        self._update_snapshot(state="closed")
        with self.assertRaises(ReviewAdapterError) as caught:
            load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(caught.exception.error, "review_current_pr_not_open")

        self._update_snapshot(state="open", merged=True)
        with self.assertRaises(ReviewAdapterError) as caught:
            load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(caught.exception.error, "review_current_pr_merged")

    def test_strict_review_projection_rejects_base_and_updated_at_drift(self) -> None:
        path = self._write_report(self._report(pr_updated_at="2026-08-20T00:00:00Z"))
        self._update_snapshot(state="open", base_sha="base-2", updated_at="2026-08-20T00:00:00Z")
        with self.assertRaises(ReviewAdapterError) as caught:
            load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(caught.exception.error, "review_artifact_base_stale")

        self._update_snapshot(base_sha="base-1", updated_at="2026-08-21T00:00:00Z")
        with self.assertRaises(ReviewAdapterError) as caught:
            load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(caught.exception.error, "review_artifact_updated_at_stale")

    def test_strict_review_projection_reports_updated_at_match(self) -> None:
        path = self._write_report(self._report(pr_updated_at="2026-08-20T00:00:00Z"))
        self._update_snapshot(state="open", updated_at="2026-08-20T00:00:00Z")
        _, validation = load_review_artifact(self.config, path, strict=True, require_current_snapshot=True)
        self.assertEqual(validation["current_identity_status"], "MATCH")
        self.assertEqual(validation["current_updated_at_status"], "MATCH")

    def test_graph_from_qa_dry_run_has_no_graph_side_effects(self) -> None:
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())
        result = run_graph_task_graph(
            self.config,
            questions=["Which test run produced evidence for this case?"],
            from_qa=True,
            dry_run=True,
        )
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file())
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(before, after)
        self.assertEqual(result["side_effects"]["sqlite"], False)
        self.assertEqual(result["side_effects"]["candidate_snapshot"], False)
        self.assertIsNone(result["qa_candidate_path"])


if __name__ == "__main__":
    unittest.main()
