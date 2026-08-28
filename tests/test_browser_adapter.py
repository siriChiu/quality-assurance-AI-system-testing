from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from quality_pilot.browser_adapter import _add_playwright_site_packages, _extract_discovered_url, _sanitize_trace, _step_locator, run_browser_test


class BrowserAdapterTest(unittest.TestCase):
    def _settings(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "enabled": True,
            "url": "http://127.0.0.1:8765/",
            "start_command": "python3 -m http.server 8765",
            "steps": [{"action": "expect_visible", "selector": "body"}],
        }
        value.update(overrides)
        return value

    def test_role_locator_contract_uses_semantic_playwright_locator(self) -> None:
        class FakePage:
            def get_by_role(self, role, **kwargs):
                return ("role", role, kwargs)

            def get_by_label(self, name):
                return ("label", name)

        self.assertEqual(
            _step_locator(FakePage(), {"locator": {"type": "role", "role": "tab", "name": "Fan Zone"}}),
            ("role", "tab", {"name": "Fan Zone"}),
        )
        self.assertEqual(
            _step_locator(FakePage(), {"locator": {"type": "label", "name": "CPU0-TMP"}}),
            ("label", "CPU0-TMP"),
        )

    def test_review_venv_site_packages_are_selected_for_browser_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / ".venv" / "lib" / "python3.12" / "site-packages"
            site.mkdir(parents=True)
            with patch.object(sys, "path", []):
                _add_playwright_site_packages(".venv/bin/python", cwd=root)
                self.assertEqual(sys.path[0], str(site))

    def test_missing_browser_contract_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_browser_test(
                {"enabled": True},
                cwd=Path(temporary),
                evidence_dir=Path(temporary) / "evidence",
                contract_identity_hash="hash",
                environment_profile={"ready": True},
            )
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["reason"], "browser_contract_missing")

    def test_remote_browser_url_is_blocked_in_local_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_browser_test(
                self._settings(url="https://example.invalid/"),
                cwd=Path(temporary),
                evidence_dir=Path(temporary) / "evidence",
                contract_identity_hash="hash",
                environment_profile={"ready": True},
            )
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["reason"], "browser_remote_url_not_supported")
        self.assertEqual(result["case_id"], "BROWSER-UI")

    def test_network_fallback_is_not_allowed_for_browser_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_browser_test(
                self._settings(start_command="curl http://127.0.0.1:8765"),
                cwd=Path(temporary),
                evidence_dir=Path(temporary) / "evidence",
                contract_identity_hash="hash",
                environment_profile={"ready": True},
            )
        self.assertEqual(result["status"], "BLOCK")
        self.assertTrue(result["reason"].startswith("unsafe_browser_start_command"))

    def test_not_ready_environment_blocks_before_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_browser_test(
                self._settings(),
                cwd=Path(temporary),
                evidence_dir=Path(temporary) / "evidence",
                contract_identity_hash="hash",
                environment_profile={"ready": False, "blockers": ["fixture_missing"]},
            )
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["reason"], "environment_profile_required")

    def test_url_discovery_extracts_url_from_log_prefix(self) -> None:
        self.assertEqual(
            _extract_discovered_url("Browser UI: http://172.17.23.148:46017/?token=secret-token"),
            "http://172.17.23.148:46017/?token=secret-token",
        )

    def test_trace_is_sanitized_before_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("trace.trace", '{"url":"http://127.0.0.1/?token=secret-token"}')
            _sanitize_trace(path, ["secret-token"])
            with zipfile.ZipFile(path) as archive:
                content = archive.read("trace.trace").decode("utf-8")
            self.assertNotIn("secret-token", content)
            self.assertIn("[REDACTED:", content)

    def test_dry_run_is_planned_without_starting_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = run_browser_test(
                self._settings(),
                cwd=Path(temporary),
                evidence_dir=Path(temporary) / "evidence",
                contract_identity_hash="hash",
                environment_profile={"ready": True},
                dry_run=True,
            )
        self.assertEqual(result["status"], "PLANNED")
        self.assertEqual(result["reason"], "dry_run")


if __name__ == "__main__":
    unittest.main()
