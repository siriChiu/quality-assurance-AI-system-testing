from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality_pilot.remote_browser_adapter import _redact_url, _safe_remote_command, run_remote_browser_test


class RemoteBrowserAdapterTest(unittest.TestCase):
    def test_remote_command_is_argv_normalized_and_shell_safe(self) -> None:
        command, reason = _safe_remote_command(".venv/bin/python main.py --browser", "/remote/repo/.venv/bin/python")
        self.assertEqual(reason, "")
        self.assertEqual(command, "/remote/repo/.venv/bin/python main.py --browser")
        self.assertIsNone(_safe_remote_command("python main.py --browser && rm -rf /", "/remote/python")[0])

    def test_dynamic_url_redacts_query_values(self) -> None:
        value = _redact_url("http://172.17.23.148:46017/?token=secret-value&mode=browser")
        self.assertEqual(value, "http://172.17.23.148:46017/?token=<redacted>&mode=<redacted>")

    def test_dynamic_url_redacts_fragment_values(self) -> None:
        value = _redact_url("http://172.17.23.148:46017/#token=secret-value")
        self.assertEqual(value, "http://172.17.23.148:46017/#token=<redacted>")

    def test_missing_remote_coordinates_block_without_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("quality_pilot.remote_browser_adapter.subprocess.run") as run:
                result = run_remote_browser_test(
                    {"enabled": True, "start_command": ".venv/bin/python main.py --browser", "steps": [{"action": "expect_visible", "selector": "body"}]},
                    environment_profile={"configured": {}},
                    evidence_dir=Path(tmp) / "evidence",
                    contract_identity_hash="hash",
                    root=Path(tmp),
                    case_id="CASE-BROWSER",
                    run_id="run-1",
                )
            self.assertEqual(result["status"], "BLOCK")
            self.assertEqual(result["reason"], "remote_browser_configuration_missing")
            run.assert_not_called()

    def test_remote_flow_discovers_url_and_uses_prestarted_playwright_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence"

            class FakeProcess:
                def __init__(self, argv, stdout=None, stderr=None, **kwargs):
                    self.argv = argv
                    self._returncode = None
                    if "-N" not in argv:
                        if stdout is not None:
                            stdout.write(b"Browser UI: http://172.17.23.148:46017/?token=secret-token\n")
                            stdout.flush()
                        if stderr is not None:
                            stderr.write(b"QUALITY_PILOT_REMOTE_PID:12345\n")
                            stderr.flush()

                def poll(self):
                    return self._returncode

                def terminate(self):
                    self._returncode = 0

                def wait(self, timeout=None):
                    self._returncode = 0
                    return 0

                def kill(self):
                    self._returncode = -9

            def fake_run_browser(settings, **kwargs):
                self.assertTrue(kwargs["prestarted"])
                self.assertIn("127.0.0.1:", kwargs["server_url"])
                self.assertIn("token=secret-token", kwargs["server_url"])
                return {"status": "PASS", "evidence": {}}

            with patch("quality_pilot.remote_browser_adapter.subprocess.Popen", side_effect=FakeProcess), patch(
                "quality_pilot.remote_browser_adapter.subprocess.run", return_value=type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            ), patch("quality_pilot.remote_browser_adapter.run_browser_test", side_effect=fake_run_browser):
                result = run_remote_browser_test(
                    {"enabled": True, "start_command": ".venv/bin/python main.py --browser", "url_discovery": "stdout", "url_pattern": r"Browser UI: https?://[^\s]+", "steps": [{"action": "expect_visible", "selector": "body"}]},
                    environment_profile={"configured": {"ssh_host": "smartfan-x86-qa", "remote_repo": "/remote/repo", "remote_python": "/remote/python"}, "remote_preflight": {"status": "READY", "source_identity": {"status": "VERIFIED"}}},
                    evidence_dir=evidence,
                    contract_identity_hash="hash",
                    root=root,
                    case_id="CASE-BROWSER",
                    run_id="run-1",
                )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["evidence_origin"], "remote")
            self.assertEqual(result["remote_cleanup"]["status"], "PASS")
            persisted = (evidence / "remote-server.stdout.log").read_text(encoding="utf-8")
            self.assertNotIn("secret-token", persisted)

    def test_dry_run_does_not_start_remote_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("quality_pilot.remote_browser_adapter.subprocess.Popen") as popen:
                result = run_remote_browser_test(
                    {"enabled": True, "start_command": ".venv/bin/python main.py --browser", "steps": [{"action": "expect_visible", "selector": "body"}]},
                    environment_profile={"configured": {"ssh_host": "smartfan-x86-qa", "remote_repo": "/remote/repo", "remote_python": "/remote/python"}},
                    evidence_dir=Path(tmp) / "evidence",
                    contract_identity_hash="hash",
                    root=Path(tmp),
                    case_id="CASE-BROWSER",
                    run_id="run-1",
                    dry_run=True,
                )
            self.assertEqual(result["status"], "PLANNED")
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
