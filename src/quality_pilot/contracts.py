from __future__ import annotations

import hashlib
import json
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
class CommandContract:
    id: str
    run: str
    expected_exit_code: int


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
        for key in ["id", "run", "expected_exit_code"]:
            if key not in item or item[key] in ("", None):
                raise ContractError("missing_command_field", f"commands[{index}].{key} is required", path=str(path))
        command_id = str(item["id"])
        if command_id in seen:
            raise ContractError("duplicate_command_id", f"Duplicate command id: {command_id}", path=str(path))
        seen.add(command_id)
        try:
            expected_exit_code = int(item["expected_exit_code"])
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid_expected_exit_code", f"commands[{index}].expected_exit_code must be an integer", path=str(path)) from exc
        commands.append(CommandContract(id=command_id, run=str(item["run"]), expected_exit_code=expected_exit_code))
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
