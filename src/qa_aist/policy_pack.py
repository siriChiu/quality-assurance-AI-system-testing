from __future__ import annotations

from typing import Any

CLOSED_LOOP_STEPS = [
    "Observe",
    "Normalize",
    "Execute",
    "Triage",
    "Publish",
    "Evolve",
    "Prune",
]

SWQA_DIMENSIONS = [
    "exact_reproduction",
    "functional",
    "positive",
    "negative",
    "boundary",
    "invalid_input",
    "sibling_surface",
    "side_effect_safe",
    "stress_timeout_risk",
]

GATES = {
    "PASS": "Evidence matches the expected result with the confirmed fixture, command, and side-effect boundary.",
    "HOLD": "The idea is valuable, but fixture, target, input, credential env, or success criteria still needs user confirmation.",
    "BLOCK": "Execution or publishing is unsafe because evidence, sync, contract, fixture, side-effect, or write-gate conditions are not satisfied.",
}

TRIAGE_CATEGORIES = [
    "product_defect",
    "harness_gap",
    "doc_gap",
    "environment_gap",
    "expected_or_capability_difference",
]


def policy_pack() -> dict[str, Any]:
    return {
        "name": "qa-aist-swqa-closed-loop-v1",
        "closed_loop_steps": list(CLOSED_LOOP_STEPS),
        "swqa_dimensions": list(SWQA_DIMENSIONS),
        "gates": dict(GATES),
        "triage_categories": list(TRIAGE_CATEGORIES),
        "rules": [
            "Repository files are the system of record; chat context is steering only.",
            "Read and sync tracker state before generating, triaging, publishing, or fixing tracker-linked work.",
            "Closed tracker items are remote truth and must be pruned from active local mirrors and references.",
            "PASS, expected, or fixture-corrected results are published to status/wiki/report, not as tracker issue noise.",
            "FAIL triage distinguishes product defects from harness, documentation, and environment gaps.",
            "Case generation should cover functional, positive, negative, boundary, invalid input, sibling surface, side-effect-safe, and timeout-risk dimensions.",
            "Reports must separate inventory total, run scope, and executed count.",
            "Remote writes require deterministic write gate approval.",
        ],
    }


def dimension_specs() -> list[dict[str, Any]]:
    return [
        {
            "key": "positive",
            "title": "Positive smoke path",
            "dimensions": ["positive", "side_effect_safe"],
            "expected": "The primary user-facing path succeeds with a confirmed safe target and input.",
        },
        {
            "key": "negative-invalid-option",
            "title": "Negative invalid input path",
            "dimensions": ["negative", "invalid_input", "side_effect_safe"],
            "expected": "Invalid input is rejected clearly without modifying product or lab state.",
        },
        {
            "key": "boundary-empty-minimal",
            "title": "Boundary minimal or empty input path",
            "dimensions": ["boundary", "invalid_input", "side_effect_safe"],
            "expected": "Empty, missing, or minimal input is handled deterministically with a clear result.",
        },
        {
            "key": "sibling-surface",
            "title": "Sibling surface consistency path",
            "dimensions": ["sibling_surface", "positive", "negative", "side_effect_safe"],
            "expected": "Adjacent commands, APIs, modes, or files follow the same validation and error behavior.",
        },
        {
            "key": "stress-timeout-risk",
            "title": "Stress and timeout-risk path",
            "dimensions": ["stress_timeout_risk", "boundary", "side_effect_safe"],
            "expected": "Large, repeated, slow, or timeout-prone work is bounded and compared against an appropriate baseline before defect filing.",
        },
    ]


def common_questions(*, feature: str, profile: str, has_confirmed_command: bool) -> list[str]:
    questions = [
        f"`{feature}` 的實際測試目標是什麼？請提供 command、API endpoint、runner 或 repo check。",
        "測試需要哪些 fixture、輸入檔、環境變數、憑證 env 或 lab target？",
        "成功條件、可接受 return code、stdout/stderr 關鍵字或輸出檔案是什麼？",
        "哪些操作有副作用，必須改用 dry-run、mock、parser-only、no-op fixture 或 rollback？",
        "哪些 sibling command/API/mode、邊界值、invalid input 與 timeout-risk 要一起覆蓋？",
    ]
    if has_confirmed_command:
        return questions[1:]
    if profile == "auto":
        questions.insert(1, "QA-AIST 尚未能從 repo 訊號安全推斷 profile；這比較像 CLI、API、hardware/lab，還是 repo health 測試？")
    return questions
