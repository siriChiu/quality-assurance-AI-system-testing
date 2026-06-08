from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from qa_aist import hermes


class HermesDispatchTest(unittest.TestCase):
    def test_quick_start_chat_commands_dispatch_to_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            setup = hermes.dispatch_chat_command("/qa-aist setup", root=root)
            self.assertEqual(setup["exit_code"], 0)
            self.assertEqual(setup["status"], "ok")
            self.assertIn("--root", setup["engine_argv"])
            self.assertTrue((root / ".qa-aist.yaml").exists())
            self.assertTrue((root / ".qa-aist-project" / "cases" / "example-contract.yaml").exists())

            doctor = hermes.dispatch_chat_command("/qa-aist doctor", root=root)
            self.assertEqual(doctor["status"], "PASS")
            self.assertIn("qa-aist> PASS", doctor["chat_response"])

            listed = hermes.dispatch_chat_command("/qa-aist qa-test list", root=root)
            self.assertEqual(listed["status"], "ok")
            self.assertEqual(listed["payload"]["cases"][0]["case_id"], "EXAMPLE-001")

            run_one = hermes.dispatch_chat_command("/qa-aist qa-test run-one EXAMPLE-001", root=root)
            self.assertEqual(run_one["status"], "PASS")
            self.assertIn("result:", run_one["chat_response"])

            close_loop = hermes.dispatch_chat_command("/qa-aist close-loop run-once", root=root)
            self.assertEqual(close_loop["status"], "PASS")
            self.assertIn("latest_run_json", close_loop["payload"])
            self.assertIn("report_path", close_loop["payload"])

            report = hermes.dispatch_chat_command("/qa-aist report status", root=root)
            self.assertEqual(report["status"], "ok")
            self.assertTrue((root / ".qa-aist-project" / "reports" / "status.md").exists())

            plan = hermes.dispatch_chat_command("/qa-aist tracker plan-write", root=root)
            self.assertEqual(plan["status"], "ok")
            self.assertEqual(plan["payload"]["write_gate_result"]["reason"], "tracker_disabled")

    def test_command_cheat_sheet_chat_commands_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes.dispatch_chat_command("/qa-aist setup", root=root)

            commands = [
                "/qa-aist status",
                "/qa-aist doctor",
                "/qa-aist config show",
                "/qa-aist config validate",
                "/qa-aist qa-test list",
                "/qa-aist qa-test validate",
                "/qa-aist qa-test dry-run",
                "/qa-aist qa-test run",
                "/qa-aist qa-test run-one EXAMPLE-001",
                "/qa-aist close-loop status",
                "/qa-aist close-loop run-once",
                "/qa-aist report status",
                "/qa-aist report json",
                "/qa-aist tracker plan-write",
            ]
            for command in commands:
                with self.subTest(command=command):
                    result = hermes.dispatch_chat_command(command, root=root)
                    self.assertEqual(result["exit_code"], 0)
                    self.assertNotEqual(result["status"], "error")
                    self.assertTrue(result["engine_argv"])
                    self.assertIn("payload", result)

    def test_alias_is_accepted_but_other_chat_text_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes.dispatch_chat_command("/qa-aist setup", root=root)

            alias = hermes.dispatch_chat_command("qa-aist status", root=root)
            self.assertEqual(alias["exit_code"], 0)
            self.assertEqual(alias["prefix"], "qa-aist")

            rejected = hermes.dispatch_chat_command("status", root=root)
            self.assertEqual(rejected["exit_code"], 2)
            self.assertEqual(rejected["status"], "error")
            self.assertEqual(rejected["payload"]["error"], "not_a_qa_aist_command")

            unsupported = hermes.dispatch_chat_command("/qa-aist rm -rf .", root=root)
            self.assertNotEqual(unsupported["exit_code"], 0)
            self.assertEqual(unsupported["status"], "error")
            self.assertEqual(unsupported["payload"]["error"], "engine_output_not_json")

    def test_console_entrypoint_emits_hermes_dispatch_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes.dispatch_chat_command("/qa-aist setup", root=tmp)
            buf = StringIO()
            with redirect_stdout(buf):
                code = hermes.main(["--root", tmp, "/qa-aist", "status"])
            payload = json.loads(buf.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["interface"], "hermes")
            self.assertEqual(payload["command"], "/qa-aist status")
            self.assertEqual(payload["payload"]["tool"], "qa-aist")


if __name__ == "__main__":
    unittest.main()
