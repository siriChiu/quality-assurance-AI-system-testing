from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quality_pilot.config import load_project_config
from quality_pilot.execution_contract import apply_discovered_contract, normalize_execution_contract
from quality_pilot import cli


class ExecutionContractTest(unittest.TestCase):
    def _setup(self, root: Path) -> None:
        with __import__("contextlib").redirect_stdout(__import__("io").StringIO()):
            code = cli.main(["setup", "--root", str(root), "--json"])
        self.assertEqual(code, 0)

    def test_discovery_is_candidate_only_until_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._setup(root)
            (root / "main.py").write_text("# --browser\n", encoding="utf-8")
            browser_dir = root / "tests" / "browser_ui"
            browser_dir.mkdir(parents=True)
            (browser_dir / "test_ui.py").write_text("page.get_by_role('button').click()\n", encoding="utf-8")
            config = load_project_config(root)
            contract = normalize_execution_contract(config)
            self.assertEqual(contract["status"], "CONFIRMATION_REQUIRED")
            self.assertTrue(contract["candidate_contract"])
            self.assertEqual(contract["candidate_contract"]["url_discovery"], "stdout")
            self.assertEqual(contract["candidate_contract"]["steps"][0]["selector"], "body")

    def test_legacy_web_ui_is_normalized_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._setup(root)
            config_path = root / ".quality-pilot.yaml"
            text = config_path.read_text(encoding="utf-8")
            text = text.replace("  product_testing:\n", "  web_ui:\n    enabled: true\n    start_command: python3 main.py --browser\n    url_discovery: stdout\n    url_pattern: 'https?://[^\\s]+'\n    steps:\n      - action: expect_visible\n        selector: body\n  product_testing:\n")
            config_path.write_text(text, encoding="utf-8")
            config = load_project_config(root)
            contract = normalize_execution_contract(config)
            self.assertEqual(contract["status"], "READY")
            self.assertIn("web_ui", contract["product_testing"])
            self.assertEqual(contract["product_testing"]["web_ui"]["start_command"], "python3 main.py --browser")

    def test_preflight_observation_does_not_change_contract_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._setup(root)
            (root / "main.py").write_text("# --browser\n", encoding="utf-8")
            browser_dir = root / "tests" / "browser_ui"
            browser_dir.mkdir(parents=True)
            (browser_dir / "test_ui.py").write_text("page.locator('button').click()\n", encoding="utf-8")
            config = load_project_config(root)
            first = normalize_execution_contract(config)
            data = config.data.copy()
            data["runtime"] = dict(data.get("runtime") or {})
            data["runtime"]["remote_preflight"] = {"status": "READY", "created_at": "one", "checks": {"ssh": "PASS"}}
            updated = type(config)(root=config.root, path=config.path, data=data, paths=config.paths)
            second = normalize_execution_contract(updated)
            self.assertEqual(first["contract_hash"], second["contract_hash"])

    def test_confirmed_discovery_persists_nested_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._setup(root)
            (root / "main.py").write_text("# --browser\n", encoding="utf-8")
            browser_dir = root / "tests" / "browser_ui"
            browser_dir.mkdir(parents=True)
            (browser_dir / "test_ui.py").write_text("page.locator('button').click()\n", encoding="utf-8")
            config = load_project_config(root)
            result = apply_discovered_contract(config, confirm=True)
            self.assertEqual(result["status"], "ok")
            updated = load_project_config(root)
            self.assertTrue(updated.data["runtime"]["product_testing"]["web_ui"]["enabled"])
            self.assertTrue((root / ".quality-pilot-project" / "state" / "effective-execution-contract.json").exists())


if __name__ == "__main__":
    unittest.main()
