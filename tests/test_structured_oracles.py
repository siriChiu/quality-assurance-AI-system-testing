from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quality_pilot.contracts import load_contract
from quality_pilot.runner import RunContext, run_case


class StructuredOracleTest(unittest.TestCase):
    def test_exit_zero_with_unexpected_error_output_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "semantic-failure.yaml"
            case.write_text(
                """case_id: ORACLE-FAIL
title: Exit zero is not sufficient
commands:
  - id: reports_error
    run: python3 -c "import sys; print('ERROR invalid state', file=sys.stderr)"
    expected_exit_code: 0
    assertions:
      - type: stderr
        id: no-error-output
        operator: regex
        expected: '^$'
""",
                encoding="utf-8",
            )

            result = run_case(load_contract(case), RunContext(root=root, evidence_dir=root / "evidence"))

            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["oracle_strength"], "semantic")
            self.assertFalse(result["oracle_partial"])
            command = result["commands"][0]
            self.assertEqual(command["exit_code"], 0)
            self.assertEqual(command["status"], "FAIL")
            self.assertTrue(command["oracle_results"][0]["passed"])
            self.assertFalse(command["oracle_results"][1]["passed"])
            self.assertEqual(command["oracle_results"][1]["id"], "no-error-output")
            self.assertEqual(command["oracle_results"][1]["type"], "stderr")

    def test_all_supported_semantic_oracles_are_evaluated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "semantic-pass.yaml"
            case.write_text(
                """case_id: ORACLE-PASS
title: Structured oracle pass
commands:
  - id: output_probe
    run: python3 -c "import sys; print('ready 42'); print('warning none', file=sys.stderr)"
    expected_exit_code: 0
    assertions:
      - type: stdout
        operator: contains
        expected: ready
      - type: stdout
        operator: regex
        expected: 'ready [0-9]+'
      - type: stderr
        operator: contains
        expected: warning
      - type: duration_ms
        operator: less_than
        expected: 5000
""",
                encoding="utf-8",
            )

            result = run_case(load_contract(case), RunContext(root=root, evidence_dir=root / "evidence"))

            self.assertEqual(result["status"], "PASS")
            command = result["commands"][0]
            self.assertTrue(all(oracle["passed"] for oracle in command["oracle_results"]))
            self.assertEqual(
                [oracle["type"] for oracle in command["oracle_results"]],
                ["exit_code", "stdout", "stdout", "stderr", "duration_ms"],
            )
            self.assertIsInstance(command["duration_ms"], float)

    def test_legacy_exit_only_contract_is_marked_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "legacy.yaml"
            case.write_text(
                """case_id: ORACLE-LEGACY
title: Legacy v1 contract
commands:
  - id: legacy_probe
    run: python3 --version
    expected_exit_code: 0
""",
                encoding="utf-8",
            )

            result = run_case(load_contract(case), RunContext(root=root, evidence_dir=root / "evidence"))

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["oracle_strength"], "exit_only")
            self.assertTrue(result["oracle_partial"])
            self.assertTrue(result["partial_probe"])
            oracle = result["commands"][0]["oracle_results"][0]
            self.assertEqual(oracle["id"], "expected-exit-code")
            self.assertEqual(oracle["source"], "legacy_expected_exit_code")
            self.assertTrue(oracle["passed"])

    def test_mixed_command_oracles_make_the_case_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "mixed.yaml"
            case.write_text(
                """case_id: ORACLE-MIXED
title: Mixed oracle strength
commands:
  - id: semantic_probe
    run: python3 -c "print('ready')"
    expected_exit_code: 0
    assertions:
      - type: stdout
        operator: contains
        expected: ready
  - id: exit_only_probe
    run: python3 --version
    expected_exit_code: 0
""",
                encoding="utf-8",
            )

            result = run_case(load_contract(case), RunContext(root=root, evidence_dir=root / "evidence"))

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["oracle_strength"], "mixed")
            self.assertTrue(result["oracle_partial"])
            self.assertTrue(result["partial_probe"])


if __name__ == "__main__":
    unittest.main()
