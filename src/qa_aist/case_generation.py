from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import ProjectConfig, json_dumps
from .contracts import ContractError, load_contracts
from .issues import case_id_for_issue, load_issue_snapshot

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class CaseGenerationError(RuntimeError):
    pass


def generate_cases_from_issues(
    config: ProjectConfig,
    *,
    issue_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    snapshot = load_issue_snapshot(config)
    items = [item for item in snapshot.get("items", []) if isinstance(item, dict)]
    if issue_id is not None:
        items = [item for item in items if int(item.get("issue_id", -1)) == issue_id]
    if issue_id is not None and not items:
        return {"status": "error", "error": "issue_not_in_snapshot", "issue_id": issue_id}

    config.paths.cases.mkdir(parents=True, exist_ok=True)
    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    for item in items:
        contract = draft_contract_for_issue(item)
        path = config.paths.cases / f"{contract['case_id']}.yaml"
        if path.exists() and not force:
            skipped.append({"case_id": contract["case_id"], "path": _relative_or_str(path, config.root), "reason": "exists"})
            continue
        path.write_text(_dump_yaml(contract), encoding="utf-8")
        item_questions = contract.get("qa_aist", {}).get("questions", [])
        if item_questions:
            questions.append({"case_id": contract["case_id"], "issue_id": item.get("issue_id"), "questions": item_questions})
        generated.append(
            {
                "case_id": contract["case_id"],
                "issue_id": item.get("issue_id"),
                "path": _relative_or_str(path, config.root),
                "draft": bool(contract.get("qa_aist", {}).get("draft")),
                "question_count": len(item_questions),
            }
        )

    status = "needs_input" if questions else "ok"
    return {
        "status": status,
        "generated": generated,
        "skipped": skipped,
        "questions": questions,
        "snapshot_path": _relative_or_str(config.paths.state / "issues-snapshot.json", config.root),
        "message": "Draft cases generated; answer questions before running qa-test." if questions else "Cases generated.",
    }


def review_generated_cases(config: ProjectConfig) -> dict[str, Any]:
    reviews: list[dict[str, Any]] = []
    for path in sorted([*config.paths.cases.glob("*.yaml"), *config.paths.cases.glob("*.yml")]):
        try:
            data = _load_yaml(path)
        except Exception:
            continue
        qa = data.get("qa_aist") if isinstance(data.get("qa_aist"), dict) else {}
        if qa.get("draft") or qa.get("questions"):
            reviews.append(
                {
                    "case_id": data.get("case_id"),
                    "path": _relative_or_str(path, config.root),
                    "draft": bool(qa.get("draft")),
                    "questions": qa.get("questions", []),
                    "source": data.get("source"),
                }
            )
    return {"status": "ok", "draft_count": len(reviews), "drafts": reviews}


def validate_generated_cases(config: ProjectConfig) -> dict[str, Any]:
    try:
        contracts = load_contracts(config.paths.cases)
    except ContractError as exc:
        return {"status": "error", "error": exc.error, "message": exc.message, "path": exc.path}
    review = review_generated_cases(config)
    return {
        "status": "ok",
        "case_count": len(contracts),
        "draft_count": review["draft_count"],
        "cases": [
            {
                "case_id": contract.case_id,
                "title": contract.title,
                "contract_hash": contract.contract_hash,
                "path": _relative_or_str(contract.path, config.root),
            }
            for contract in contracts
        ],
        "drafts": review["drafts"],
    }


def draft_contract_for_issue(item: dict[str, Any]) -> dict[str, Any]:
    case_id = case_id_for_issue(item)
    issue_id = int(item.get("issue_id"))
    title = str(item.get("title") or f"Gitea issue #{issue_id}")
    body = str(item.get("body") or "")
    command = extract_repro_command(body)
    questions = missing_input_questions(item, has_command=bool(command))
    run = command or f"python3 -c \"raise SystemExit('TODO: fill real repro command for Gitea issue #{issue_id}')\""
    return {
        "case_id": case_id,
        "title": f"Gitea #{issue_id}: {title}",
        "source": {
            "provider": "gitea",
            "issue_id": issue_id,
            "issue_url": item.get("url") or "",
        },
        "qa_aist": {
            "draft": bool(questions),
            "questions": questions,
            "review_required_before_run": bool(questions),
        },
        "commands": [
            {
                "id": "reproduce",
                "run": run,
                "expected_exit_code": 0,
            }
        ],
        "expected": "Original issue is reproduced or verified through a side-effect-safe user-facing path.",
        "swqa_expansion": [
            "exact_reproduction",
            "sibling_surface_scan",
            "negative_cases",
            "boundary_values",
            "side_effect_safe_smoke",
        ],
    }


def extract_repro_command(body: str) -> str | None:
    patterns = [
        r"(?im)^\s*(?:qa-aist\s+)?(?:test[_ -]?command|repro(?:duction)? command|command)\s*:\s*(.+?)\s*$",
        r"(?im)^\s*actual command\s*:\s*(.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            value = match.group(1).strip().strip("`")
            if value:
                return value
    fenced = re.search(r"```(?:bash|sh|shell)?\s*\n(.+?)\n```", body, re.DOTALL | re.IGNORECASE)
    if fenced:
        lines = [line.strip() for line in fenced.group(1).splitlines() if line.strip() and not line.strip().startswith("#")]
        if len(lines) == 1 and not lines[0].startswith("curl "):
            return lines[0]
    return None


def missing_input_questions(item: dict[str, Any], *, has_command: bool) -> list[str]:
    issue_id = item.get("issue_id")
    questions: list[str] = []
    if not has_command:
        questions.append(f"issue #{issue_id} 要用哪個使用者可見指令或 runner 重現？")
    questions.extend(
        [
            "測試需要哪些 fixture、輸入檔、環境變數或 lab target？",
            "成功條件與預期 exit code 是什麼？",
            "哪些操作有副作用，必須改用 dry-run、mock、parser-only 或 no-op fixture？",
            "哪些 sibling command、邊界值與 invalid input 需要一起覆蓋？",
        ]
    )
    return questions


def _dump_yaml(data: dict[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return json_dumps(data) + "\n"


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        import json

        loaded = json.loads(path.read_text(encoding="utf-8"))
    else:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
