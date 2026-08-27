#!/usr/bin/env python3
"""Install the optional Hermes nested-skill completion patch reproducibly.

The AI Quality Pilot package does not vendor or silently mutate Hermes.  This
script applies the checked-in, context-pinned patch to an installed Hermes
source checkout.  It is deliberately fail-closed when the target has drifted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


PATCH_NAME = "hermes-agent-nested-completion.patch"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-root",
        default=os.getenv("HERMES_AGENT_ROOT", "/usr/local/lib/hermes-agent"),
        help="Hermes source checkout to patch",
    )
    parser.add_argument("--check", action="store_true", help="Validate whether the patch can be applied; do not modify files")
    args = parser.parse_args()

    root = Path(args.hermes_root).expanduser().resolve()
    patch_path = Path(__file__).resolve().with_name(PATCH_NAME)
    payload = {
        "schema": "quality-pilot.hermes-completion-patch.v1",
        "operation": "install_hermes_nested_completion_patch",
        "hermes_root": str(root),
        "patch": patch_path.name,
        "check_only": bool(args.check),
    }
    if not patch_path.is_file():
        return emit({**payload, "status": "blocked", "reason": "patch_file_missing"}, 2)
    if not (root / ".git").exists():
        return emit({**payload, "status": "blocked", "reason": "hermes_source_checkout_required"}, 2)

    already = _git_check(root, patch_path, reverse=True)
    if already == 0:
        return emit({**payload, "status": "already_applied"}, 0)

    check = _git_check(root, patch_path, reverse=False)
    if check != 0:
        return emit({
            **payload,
            "status": "blocked",
            "reason": "hermes_source_drift_or_patch_not_applicable",
            "next_action": "Inspect Hermes version and update the checked-in patch; do not edit the installation blindly.",
        }, 2)
    if args.check:
        return emit({**payload, "status": "applicable"}, 0)

    applied = subprocess.run(
        ["git", "-C", str(root), "apply", "--whitespace=nowarn", str(patch_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if applied.returncode != 0:
        return emit({
            **payload,
            "status": "blocked",
            "reason": "hermes_patch_apply_failed",
            "stderr_kind": "present" if applied.stderr else "absent",
        }, 2)
    return emit({**payload, "status": "applied"}, 0)


def _git_check(root: Path, patch_path: Path, *, reverse: bool) -> int:
    command = ["git", "-C", str(root), "apply", "--check"]
    if reverse:
        command.append("--reverse")
    command.append(str(patch_path))
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return completed.returncode


def emit(payload: dict[str, object], code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
