"""Case-first adapters for product build/run and Playwright product flows."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ProjectConfig
from .contracts import CaseContract, CommandContract
from .execution_contract import normalize_execution_contract
from .product_testing import run_product_tests
from .runner import RunContext, utc_now


def _write_contract(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSON is valid YAML and avoids introducing a second serializer here.
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

PRODUCT_CASE_TYPES = {"product_build", "product_operation", "playwright_ui", "product"}


def build_product_case_contract(config: ProjectConfig, *, case_id: str, title: str, review_id: str, snapshot: dict[str, Any]) -> CaseContract:
    effective = normalize_execution_contract(config, snapshot=snapshot)
    settings = dict(effective.get("product_testing") or {})
    raw = {
        "case_id": case_id,
        "title": title,
        "case_type": "product",
        "commands": [{"id": "product-adapter", "run": "product-adapter", "expected_exit_code": 0}],
        "quality_pilot": {
            "case_type": "product",
            "product_testing": settings,
            "execution_contract": effective,
            "review_id": review_id,
            "snapshot_head_sha": snapshot.get("head_sha"),
            "swqa_dimensions": ["black_box", "functional", "ui", "ux"],
        },
    }
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _write_contract(config.paths.cases / f"{case_id}.yaml", raw)
    return CaseContract(
        case_id=case_id,
        title=title,
        commands=[CommandContract(id="product-adapter", run="product-adapter", expected_exit_code=0)],
        path=config.paths.cases / f"{case_id}.yaml",
        raw=raw,
        contract_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def execute_product_case(contract: CaseContract, context: RunContext, *, config: ProjectConfig, snapshot: dict[str, Any], review_id: str, dry_run: bool = False) -> dict[str, Any]:
    started = utc_now()
    quality = contract.raw.get("quality_pilot") if isinstance(contract.raw.get("quality_pilot"), dict) else {}
    execution_contract = quality.get("execution_contract") if isinstance(quality.get("execution_contract"), dict) else {}
    product = run_product_tests(
        config,
        worktree=context.root,
        snapshot=snapshot,
        review_id=review_id,
        evidence_dir=context.evidence_dir / contract.case_id / "product-test",
        environment_profile=context.environment_profile,
        dry_run=dry_run,
        report_root=config.root,
        case_id=contract.case_id,
        run_id=review_id,
        contract_hash=contract.contract_hash,
        product_python=context.product_python,
        product_settings=quality.get("product_testing") if isinstance(quality.get("product_testing"), dict) else None,
        execution_contract=execution_contract,
    )
    status = str(product.get("status") or "BLOCK")
    evidence = []
    if product.get("result_path"):
        evidence.append(str(product["result_path"]))
    browser = product.get("browser") if isinstance(product.get("browser"), dict) else None
    if browser:
        for value in browser.get("evidence", {}).values() if isinstance(browser.get("evidence"), dict) else []:
            if value:
                evidence.append(str(value))
    result = {
        "case_id": contract.case_id,
        "title": contract.title,
        "case_type": "product",
        "status": status,
        "truth_status": "HOLD" if status in {"HOLD", "PLANNED", "NOT_RUN"} else status,
        "official_result": status == "PASS",
        "partial_probe": False,
        "commands": [{"id": "product-adapter", "status": status, "exit_code": 0 if status == "PASS" else 2}],
        "oracle": {"type": "product_build_and_semantic_operation", "product_status": status, "browser_status": browser.get("status") if browser else None},
        "evidence": sorted(set(evidence)),
        "contract_hash": contract.contract_hash,
        "execution_contract_hash": execution_contract.get("contract_hash"),
        "execution_target": execution_contract.get("execution", {}).get("product_target", "local"),
        "evidence_origin": "remote" if execution_contract.get("execution", {}).get("product_target") == "remote_ssh" else "local",
        "run_id": review_id,
        "started_at": started,
        "ended_at": utc_now(),
        "product_result": product,
        "environment_profile": context.environment_profile,
    }
    result_path = context.evidence_dir / contract.case_id / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["result_path"] = str(result_path.relative_to(config.root)) if result_path.is_relative_to(config.root) else str(result_path)

    # Browser is a child case with its own canonical lineage, not merely a
    # matrix cell.  It is derived from the already executed product result so
    # the browser flow is not run twice.
    if browser is not None:
        browser_case_id = str(browser.get("case_id") or f"{contract.case_id}-BROWSER-UI")
        browser_raw = {
            "case_id": browser_case_id,
            "title": f"{contract.title} browser UI and UX",
            "case_type": "playwright_ui",
            "commands": [{"id": "playwright-adapter", "run": "playwright-adapter", "expected_exit_code": 0}],
            "quality_pilot": {"case_type": "playwright_ui", "parent_case_id": contract.case_id, "swqa_dimensions": ["black_box", "functional", "ui", "ux"]},
        }
        browser_contract_hash = hashlib.sha256(json.dumps(browser_raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        _write_contract(config.paths.cases / f"{browser_case_id}.yaml", browser_raw)
        browser_result = {
            "case_id": browser_case_id,
            "title": browser_raw["title"],
            "case_type": "playwright_ui",
            "status": browser.get("status", "NOT_RUN"),
            "truth_status": "HOLD" if browser.get("status") in {"HOLD", "NOT_RUN"} else browser.get("status"),
            "official_result": browser.get("status") == "PASS",
            "partial_probe": False,
            "commands": [{"id": "playwright-adapter", "status": browser.get("status", "NOT_RUN"), "exit_code": 0 if browser.get("status") == "PASS" else 2}],
            "oracle": {"type": "playwright_ui", "interaction_count": browser.get("interaction_count", 0), "state_assertion_count": browser.get("state_assertion_count", 0)},
            "evidence": [str(value) for key, value in (browser.get("evidence") or {}).items() if value and not str(key).endswith("sha256")],
            "contract_hash": browser_contract_hash,
            "run_id": review_id,
            "parent_case_id": contract.case_id,
            "started_at": started,
            "ended_at": utc_now(),
            "browser_result": browser,
        }
        browser_result_path = context.evidence_dir / browser_case_id / "result.json"
        browser_result_path.parent.mkdir(parents=True, exist_ok=True)
        browser_result_path.write_text(json.dumps(browser_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        browser_result["result_path"] = str(browser_result_path.relative_to(config.root)) if browser_result_path.is_relative_to(config.root) else str(browser_result_path)
        result["browser_case_result"] = browser_result
        result["browser_contract_raw"] = browser_raw
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
