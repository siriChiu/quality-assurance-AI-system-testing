from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quality_pilot.config import default_config, load_project_config, validate_config_data


class ConfigTest(unittest.TestCase):
    def test_schema_rejects_missing_sections(self) -> None:
        errors = validate_config_data({"project": {"name": "x"}})
        self.assertIn("missing project.default_branch", errors)
        self.assertIn("missing paths section", errors)
        self.assertIn("missing tracker section", errors)
        self.assertIn("missing policy section", errors)

    def test_schema_rejects_raw_secret(self) -> None:
        data = {
            "project": {"name": "x", "default_branch": "main"},
            "paths": {key: f".quality-pilot-project/{key}" for key in ["workspace", "cases", "runners", "rules", "state", "evidence", "reports"]},
            "tracker": {"provider": "gitea", "api_token": "real-token"},
            "policy": {"require_write_gate": True},
        }
        errors = validate_config_data(data)
        self.assertIn("raw secret-like value at tracker.api_token", errors)

    def test_load_generated_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".quality-pilot.yaml").write_text(default_config(), encoding="utf-8")
            config = load_project_config(root)
            self.assertEqual(config.data["project"]["name"], "example-project")
            self.assertEqual(config.paths.cases, root / ".quality-pilot-project" / "cases")


if __name__ == "__main__":
    unittest.main()
