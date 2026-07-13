from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quality_pilot.contracts import ContractError, load_contract


class ContractsTest(unittest.TestCase):
    def test_contract_parser_requires_ordered_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.yaml"
            path.write_text(
                """case_id: TC-1
title: Demo
commands:
  - id: one
    run: python3 --version
    expected_exit_code: 0
  - id: two
    run: python3 --version
    expected_exit_code: 0
""",
                encoding="utf-8",
            )
            contract = load_contract(path)
            self.assertEqual([command.id for command in contract.commands], ["one", "two"])
            self.assertEqual(len(contract.contract_hash), 64)
            self.assertEqual(contract.commands[0].assertions, ())

    def test_contract_parser_accepts_structured_assertions_and_oracles_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.yaml"
            path.write_text(
                """case_id: TC-2
title: Structured oracle
commands:
  - id: probe
    run: python3 --version
    assertions:
      - type: exit_code
        id: process-exit
        operator: equals
        expected: 0
      - type: stdout
        operator: regex
        expected: 'Python [0-9]+'
    oracles:
      - type: duration_ms
        operator: less_than
        expected: 5000
""",
                encoding="utf-8",
            )

            command = load_contract(path).commands[0]

            self.assertEqual(command.expected_exit_code, 0)
            self.assertEqual(
                [assertion.id for assertion in command.assertions],
                ["process-exit", "assertion-2", "assertion-3"],
            )
            self.assertEqual(
                [(assertion.type, assertion.operator) for assertion in command.assertions],
                [("exit_code", "equals"), ("stdout", "regex"), ("duration_ms", "less_than")],
            )

    def test_contract_parser_rejects_duplicate_assertion_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.yaml"
            path.write_text(
                """case_id: TC-DUPLICATE
title: Duplicate assertion IDs
commands:
  - id: probe
    run: python3 --version
    expected_exit_code: 0
    assertions:
      - id: output-check
        type: stdout
        operator: contains
        expected: Python
      - id: output-check
        type: stderr
        operator: equals
        expected: ''
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ContractError, "duplicate assertion id"):
                load_contract(path)

    def test_contract_parser_rejects_invalid_assertion_regex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.yaml"
            path.write_text(
                """case_id: TC-3
title: Invalid regex
commands:
  - id: probe
    run: python3 --version
    expected_exit_code: 0
    assertions:
      - type: stderr
        operator: regex
        expected: '['
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ContractError, "valid regex"):
                load_contract(path)

    def test_contract_parser_rejects_missing_command_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.yaml"
            path.write_text("case_id: TC-1\ntitle: Demo\ncommands:\n  - id: one\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                load_contract(path)


if __name__ == "__main__":
    unittest.main()
