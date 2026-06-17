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

    def test_contract_parser_rejects_missing_command_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.yaml"
            path.write_text("case_id: TC-1\ntitle: Demo\ncommands:\n  - id: one\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                load_contract(path)


if __name__ == "__main__":
    unittest.main()
