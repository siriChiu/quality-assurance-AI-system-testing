"""Read-only audit helpers for the repository's Gherkin contract.

The feature file is intentionally broader than the executable pytest-bdd slice.
This module makes that difference visible instead of allowing the unittest
count or a small fake-adapter test to stand in for BDD coverage.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_TAG_RE = re.compile(r"^\s*(@[A-Za-z0-9_-]+(?:\s+@[A-Za-z0-9_-]+)*)\s*$")
_SCENARIO_RE = re.compile(r"^\s*Scenario(?: Outline)?:\s*(.+?)\s*$")
_BINDING_RE = re.compile(r"@scenario\(.*?\n?\s*[^\n]*,\s*\n?\s*['\"]([^'\"]+)['\"]", re.DOTALL)
_QUOTED_SCENARIO_RE = re.compile(r"(?:Scenario|scenario)[^\n]*['\"]([^'\"]+)['\"]")


def default_feature_path() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "BDD_GHERKIN.feature"


def default_feature_paths() -> list[Path]:
    root = default_feature_path()
    split_dir = root.parent / "bdd"
    return [root, *sorted(split_dir.glob("*.feature"))]


def default_bdd_tests_path() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "bdd"


def audit_bdd_contract(
    feature_path: str | Path | None = None,
    tests_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return scenario maturity and executable-binding coverage.

    This is deliberately dependency-free so ``doctor``/``audit state`` can run
    even when pytest-bdd is not installed.  It does not claim that a binding is
    green; it only proves that a scenario is referenced by an executable test.
    """

    feature = Path(feature_path).expanduser().resolve() if feature_path else default_feature_path()
    feature_paths = [feature] if feature_path else default_feature_paths()
    feature_paths = [path for path in feature_paths if path.exists()]
    tests = Path(tests_path).expanduser().resolve() if tests_path else default_bdd_tests_path()
    if not feature_paths:
        return {
            "status": "not_available",
            "feature_path": str(feature),
            "feature_paths": [],
            "tests_path": str(tests),
            "source_available": False,
            "message": "BDD feature file is not present in this installation.",
            "scenario_count": 0,
            "current_scenario_count": 0,
            "bound_scenario_count": 0,
            "unbound_current_scenario_count": 0,
            "coverage_percent": None,
            "lighting_effects": ["bdd_feature_unavailable"],
        }

    scenarios = []
    for path in feature_paths:
        scenarios.extend(_read_scenarios(path))
    test_text = _read_test_text(tests)
    bound_names = _bound_scenario_names(test_text)
    rows: list[dict[str, Any]] = []
    for item in scenarios:
        name = item["name"]
        tags = item["tags"]
        current = "current" in tags
        planned = "planned" in tags
        partial = "partial" in tags
        defect = "defect" in tags
        bound = name in bound_names
        if planned:
            maturity = "planned"
        elif defect:
            maturity = "defect"
        elif partial:
            maturity = "partial"
        elif current:
            maturity = "current_supported"
        else:
            maturity = "unclassified"
        rows.append({
            "name": name,
            "line": item["line"],
            "tags": tags,
            "maturity": maturity,
            "bound": bound,
            "binding_status": "bound" if bound else "unbound",
        })

    current_rows = [row for row in rows if row["maturity"] in {"current_supported", "partial"}]
    bound_current = [row for row in current_rows if row["bound"]]
    unbound_current = [row for row in current_rows if not row["bound"]]
    planned_rows = [row for row in rows if row["maturity"] == "planned"]
    fake_only = _fake_contract_signal(test_text)
    coverage = (len(bound_current) / len(current_rows) * 100.0) if current_rows else 100.0
    lighting_effects: list[str] = []
    if unbound_current:
        lighting_effects.append("current_scenarios_without_executable_binding")
    if fake_only and not bound_current:
        lighting_effects.append("fake_adapter_without_current_feature_binding")
    if planned_rows and all(not row["bound"] for row in planned_rows):
        lighting_effects.append("planned_scenarios_are_not_green_evidence")

    status = "ok" if not unbound_current else "coverage_gap"
    return {
        "status": status,
        "feature_path": str(feature),
        "feature_paths": [str(path) for path in feature_paths],
        "tests_path": str(tests),
        "source_available": True,
        "scenario_count": len(rows),
        "current_scenario_count": len(current_rows),
        "bound_scenario_count": len(bound_current),
        "unbound_current_scenario_count": len(unbound_current),
        "planned_scenario_count": len(planned_rows),
        "planned_bound_scenario_count": len([row for row in planned_rows if row["bound"]]),
        "coverage_percent": round(coverage, 2),
        "current_supported": {"total": len(current_rows), "bound": len(bound_current), "coverage_percent": round(coverage, 2)},
        "planned": {"total": len(planned_rows), "bound": len([row for row in planned_rows if row["bound"]]), "coverage_percent": round((len([row for row in planned_rows if row["bound"]]) / len(planned_rows) * 100.0) if planned_rows else 0.0, 2)},
        "overall": {"status": "COMPLETE" if not planned_rows and not unbound_current else "PARTIAL"},
        "fake_adapter_signal": fake_only,
        "lighting_effects": lighting_effects,
        "scenarios": rows,
        "next_action": "Bind current scenarios with pytest-bdd steps or mark them partial/planned; do not use core unittest count as BDD coverage.",
    }


def _read_scenarios(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pending_tags: list[str] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        tag_match = _TAG_RE.match(raw)
        if tag_match:
            pending_tags.extend(tag_match.group(1).split())
            continue
        scenario_match = _SCENARIO_RE.match(raw)
        if scenario_match:
            rows.append({
                "name": scenario_match.group(1).strip(),
                "line": line_number,
                "tags": sorted(tag.lstrip("@").lower() for tag in pending_tags),
            })
            pending_tags = []
            continue
        if raw.strip() and not raw.lstrip().startswith("#") and not raw.strip().startswith("And "):
            # Tags apply only to the immediate scenario/rule declaration.
            if not raw.strip().startswith("@"):
                pending_tags = []
    return rows


def _read_test_text(path: Path) -> str:
    if not path.exists():
        return ""
    chunks: list[str] = []
    for item in sorted(path.rglob("*.py")):
        try:
            chunks.append(item.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(chunks)


def _bound_scenario_names(text: str) -> set[str]:
    names = set(match.group(1).strip() for match in _BINDING_RE.finditer(text))
    # Be tolerant of formatting changes in decorators while remaining read-only.
    names.update(match.group(1).strip() for match in _QUOTED_SCENARIO_RE.finditer(text))
    return {name for name in names if name}


def _fake_contract_signal(text: str) -> bool:
    return "FakeGiteaAdapter" in text
