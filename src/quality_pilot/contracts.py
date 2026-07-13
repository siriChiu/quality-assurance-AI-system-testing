from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_yaml


class ContractError(ValueError):
    def __init__(self, error: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.path = path


@dataclass(frozen=True)
class CommandAssertion:
    type: str
    operator: str
    expected: str | int | float
    id: str | None = None


@dataclass(frozen=True)
class CommandContract:
    id: str
    run: str
    expected_exit_code: int
    assertions: tuple[CommandAssertion, ...] = ()


@dataclass(frozen=True)
class CaseContract:
    case_id: str
    title: str
    commands: list[CommandContract]
    path: Path
    raw: dict[str, Any]
    contract_hash: str


def load_contract(path: Path) -> CaseContract:
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise ContractError("contract_not_mapping", "Contract root must be a mapping", path=str(path))
    for key in ["case_id", "title", "commands"]:
        if key not in data or data[key] in ("", None):
            raise ContractError("missing_required_field", f"Missing {key}", path=str(path))
    commands_data = data["commands"]
    if not isinstance(commands_data, list) or not commands_data:
        raise ContractError("commands_invalid", "commands must be a non-empty list", path=str(path))
    commands: list[CommandContract] = []
    seen: set[str] = set()
    for index, item in enumerate(commands_data):
        if not isinstance(item, dict):
            raise ContractError("command_not_mapping", f"commands[{index}] must be a mapping", path=str(path))
        for key in ["id", "run"]:
            if key not in item or item[key] in ("", None):
                raise ContractError("missing_command_field", f"commands[{index}].{key} is required", path=str(path))
        command_id = str(item["id"])
        if command_id in seen:
            raise ContractError("duplicate_command_id", f"Duplicate command id: {command_id}", path=str(path))
        seen.add(command_id)
        assertions = _load_command_assertions(item, command_index=index, path=path)
        explicit_exit_codes = [
            assertion.expected
            for assertion in assertions
            if assertion.type == "exit_code" and assertion.operator == "equals"
        ]
        if "expected_exit_code" in item and item["expected_exit_code"] not in ("", None):
            expected_exit_code = _parse_int(
                item["expected_exit_code"],
                error="invalid_expected_exit_code",
                message=f"commands[{index}].expected_exit_code must be an integer",
                path=path,
            )
        elif explicit_exit_codes:
            expected_exit_code = int(explicit_exit_codes[0])
        else:
            raise ContractError(
                "missing_command_field",
                f"commands[{index}].expected_exit_code or an exit_code assertion is required",
                path=str(path),
            )
        if any(int(value) != expected_exit_code for value in explicit_exit_codes):
            raise ContractError(
                "conflicting_exit_code_assertion",
                f"commands[{index}] exit_code assertion conflicts with expected_exit_code",
                path=str(path),
            )
        commands.append(
            CommandContract(
                id=command_id,
                run=str(item["run"]),
                expected_exit_code=expected_exit_code,
                assertions=assertions,
            )
        )
    canonical = _canonical_contract(data)
    return CaseContract(
        case_id=str(data["case_id"]),
        title=str(data["title"]),
        commands=commands,
        path=path,
        raw=data,
        contract_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def list_contract_paths(cases_dir: Path) -> list[Path]:
    if not cases_dir.exists():
        return []
    return sorted([*cases_dir.glob("*.yaml"), *cases_dir.glob("*.yml")])


def load_contracts(cases_dir: Path) -> list[CaseContract]:
    return [load_contract(path) for path in list_contract_paths(cases_dir)]


def select_contracts(cases_dir: Path, case_id: str | None = None, case_ids: list[str] | None = None) -> list[CaseContract]:
    contracts = load_contracts(cases_dir)
    if case_ids is None:
        case_ids = [case_id] if case_id else []
    if not case_ids:
        return contracts
    requested = set(case_ids)
    selected = [contract for contract in contracts if contract.case_id in requested]
    if not selected:
        raise ContractError("case_not_found", f"Case not found: {', '.join(case_ids)}")
    missing = [item for item in case_ids if item not in {contract.case_id for contract in selected}]
    if missing:
        raise ContractError("case_not_found", f"Case not found: {', '.join(missing)}")
    return selected


def _canonical_contract(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_command_assertions(item: dict[str, Any], *, command_index: int, path: Path) -> tuple[CommandAssertion, ...]:
    assertions: list[CommandAssertion] = []
    assertion_ids: set[str] = set()
    for field in ("assertions", "oracles"):
        raw = item.get(field)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise ContractError(
                "assertions_invalid",
                f"commands[{command_index}].{field} must be a list",
                path=str(path),
            )
        for assertion_index, value in enumerate(raw):
            location = f"commands[{command_index}].{field}[{assertion_index}]"
            if not isinstance(value, dict):
                raise ContractError("assertion_not_mapping", f"{location} must be a mapping", path=str(path))
            for key in ("type", "operator", "expected"):
                if key not in value or value[key] is None:
                    raise ContractError("missing_assertion_field", f"{location}.{key} is required", path=str(path))
            assertion_type = str(value["type"]).strip().lower()
            operator = str(value["operator"]).strip().lower()
            expected = _validate_assertion(
                assertion_type,
                operator,
                value["expected"],
                location=location,
                path=path,
            )
            if "id" in value and not str(value["id"] or "").strip():
                raise ContractError("invalid_assertion_id", f"{location}.id must not be empty", path=str(path))
            assertion_id = str(value.get("id") or f"assertion-{len(assertions) + 1}").strip()
            if assertion_id in assertion_ids:
                raise ContractError(
                    "duplicate_assertion_id",
                    f"commands[{command_index}] has duplicate assertion id: {assertion_id}",
                    path=str(path),
                )
            assertion_ids.add(assertion_id)
            assertions.append(
                CommandAssertion(
                    type=assertion_type,
                    operator=operator,
                    expected=expected,
                    id=assertion_id,
                )
            )
    return tuple(assertions)


def _validate_assertion(
    assertion_type: str,
    operator: str,
    expected: Any,
    *,
    location: str,
    path: Path,
) -> str | int | float:
    operators = {
        "exit_code": {"equals"},
        "stdout": {"contains", "regex", "equals"},
        "stderr": {"contains", "regex", "equals"},
        "duration_ms": {"less_than", "less_than_or_equal"},
    }
    if assertion_type not in operators:
        raise ContractError(
            "unsupported_assertion_type",
            f"{location}.type must be one of: {', '.join(sorted(operators))}",
            path=str(path),
        )
    if operator not in operators[assertion_type]:
        raise ContractError(
            "unsupported_assertion_operator",
            f"{location}.operator is not supported for {assertion_type}",
            path=str(path),
        )
    if assertion_type == "exit_code":
        return _parse_int(
            expected,
            error="invalid_assertion_expected",
            message=f"{location}.expected must be an integer",
            path=path,
        )
    if assertion_type == "duration_ms":
        if isinstance(expected, bool):
            value = -1.0
        else:
            try:
                value = float(expected)
            except (TypeError, ValueError):
                value = -1.0
        if value < 0:
            raise ContractError(
                "invalid_assertion_expected",
                f"{location}.expected must be a non-negative number",
                path=str(path),
            )
        return value
    if not isinstance(expected, str):
        raise ContractError(
            "invalid_assertion_expected",
            f"{location}.expected must be a string",
            path=str(path),
        )
    if operator == "regex":
        try:
            re.compile(expected)
        except re.error as exc:
            raise ContractError(
                "invalid_assertion_regex",
                f"{location}.expected is not a valid regex: {exc}",
                path=str(path),
            ) from exc
    return expected


def _parse_int(value: Any, *, error: str, message: str, path: Path) -> int:
    if isinstance(value, bool):
        raise ContractError(error, message, path=str(path))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(error, message, path=str(path)) from exc
