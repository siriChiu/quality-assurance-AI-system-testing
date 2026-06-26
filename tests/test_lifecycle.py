from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from quality_pilot import hermes
from quality_pilot import config as config_module
from quality_pilot.cli import main
from quality_pilot.config import load_project_config, load_yaml
from quality_pilot.case_generation import CaseGenerationError, generate_cases_from_issues, generate_cases_growing
from quality_pilot.fix_issues import submit_fix_pr
from quality_pilot.policy_pack import policy_pack


class LifecycleTest(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        buf = StringIO()
        with redirect_stdout(buf):
            code = main(args)
        return code, json.loads(buf.getvalue())

    def init_gitea_project(self, tmp: str) -> Path:
        root = Path(tmp)
        self.run_cli(["setup", "--root", tmp])
        self.write_runtime_profile(root)
        self.write_hermes_mcp_status(root)
        return root

    def init_gitea_mcp_project(self, tmp: str) -> Path:
        return self.init_gitea_project(tmp)

    def write_hermes_mcp_status(self, root: Path, servers: list[str] | None = None) -> None:
        status_path = root / ".quality-pilot-project" / "state" / "hermes-mcp" / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({"servers": servers or ["gitea", "redmine"]}), encoding="utf-8")

    def write_runtime_profile(self, root: Path, *, entrypoint: str = "python3") -> None:
        config_path = root / ".quality-pilot.yaml"
        data = load_yaml(config_path)
        data["runtime"] = {
            "primary_entrypoint": entrypoint,
            "binary_env": "QUALITY_PILOT_BINARY",
            "target_host_env": "QUALITY_PILOT_TARGET_HOST",
            "fixture_paths": [],
            "credential_envs": [],
            "side_effect_boundary": "Read-only local smoke probes only; no network, tracker, source, or lab writes.",
        }
        config_path.write_text(config_module.yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def assert_no_placeholder_command(self, command: str) -> None:
        self.assertNotIn("__quality_pilot_invalid_command__", command)
        self.assertNotIn("repo-only probe", command)
        self.assertNotIn("AI Quality Pilot safe repo probe", command)
        self.assertNotIn("python3 -c", command)
        self.assertNotIn("compileall", command)
        self.assertNotIn("go test", command)
        self.assertNotIn("go run", command)

    def write_issues(self, root: Path) -> Path:
        issues = [
            {
                "number": 1,
                "state": "open",
                "title": "CLI help command fails",
                "body": "Command: python3 --version\n\nThe user-facing help path should stay callable.",
                "html_url": "https://git.example.test/Redfish/irctool/issues/1",
                "updated_at": "2026-06-08T00:00:00Z",
                "labels": [{"name": "bug"}],
                "comments": [{"user": {"login": "qa"}, "body": "Please add regression coverage."}],
            },
            {
                "number": 2,
                "state": "closed",
                "title": "Old closed issue",
                "body": "Already fixed.",
                "html_url": "https://git.example.test/Redfish/irctool/issues/2",
            },
        ]
        path = root / "issues.json"
        path.write_text(json.dumps(issues), encoding="utf-8")
        return path

    def write_feature_issues(self, root: Path) -> Path:
        issues = [
            {
                "number": 7,
                "state": "open",
                "title": "Add CSV export mode",
                "body": "Feature request: support exporting diagnostic output as CSV.",
                "html_url": "https://git.example.test/Redfish/irctool/issues/7",
                "updated_at": "2026-06-08T00:00:00Z",
                "labels": [{"name": "enhancement"}],
                "comments": [],
            }
        ]
        path = root / "feature-issues.json"
        path.write_text(json.dumps(issues), encoding="utf-8")
        return path

    def write_redmine_issues(self, root: Path) -> Path:
        issues = [
            {
                "id": 144780,
                "subject": "Sensor list fails after cold boot",
                "description": "After cold boot, sensor collection returns an empty list for one BMC.",
                "description_text": "\n".join(
                    [
                        "Steps to reproduce:",
                        "1. AC cycle the SIRI-LAB-BMC-01 system.",
                        "2. Wait for the BMC to finish cold boot.",
                        "3. Run `irctool sensors --login lab.yaml`.",
                        "",
                        "Observed: the command exits successfully but returns an empty sensor inventory.",
                        "",
                        "Expected: populated sensor inventory should be returned after cold boot.",
                    ]
                ),
                "status": {"name": "New"},
                "tracker": {"name": "Bug"},
                "project": {"name": "IRCTool"},
                "updated_on": "2026-06-08T12:00:00Z",
                "url": "https://redmine.example.test/issues/144780",
                "custom_fields": [
                    {"id": 7, "name": "BMC Model", "value": "SIRI-LAB-BMC-01"},
                    {"id": 8, "name": "Reproduction Command", "value": "irctool sensors --login lab.yaml"},
                ],
                "journals": [
                    {
                        "id": 9001,
                        "user": {"name": "QA Engineer"},
                        "created_on": "2026-06-08T12:10:00Z",
                        "notes": "Full journal note: cold boot reproduces after AC cycle; do not shorten this text.",
                        "details": [{"property": "attr", "name": "status_id", "old_value": "1", "new_value": "2"}],
                    }
                ],
                "attachments": [
                    {"id": 501, "filename": "cold-boot-sensors.log", "filesize": 4096, "content_url": "https://redmine.example.test/attachments/501"}
                ],
            },
            {
                "id": 144693,
                "subject": "CLI help omits virtual media options",
                "description": "The help output should include SMB virtual media flags.",
                "status": {"name": "Assigned"},
                "tracker": {"name": "Bug"},
                "project": {"name": "IRCTool"},
                "updated_on": "2026-06-08T13:00:00Z",
                "url": "https://redmine.example.test/issues/144693",
                "custom_fields": [],
                "journals": [],
                "attachments": [],
            },
        ]
        path = root / ".quality-pilot-project" / "state" / "redmine-mcp" / "issues.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.redmine-mcp-issues.v1",
                    "source": "hermes_redmine_mcp_live_read",
                    "fetched_at": "2026-06-08T13:05:00Z",
                    "requested_issue_ids": [144780, 144693],
                    "include": ["description", "custom_fields", "journals", "attachments"],
                    "payload_completeness": "full",
                    "issues": issues,
                }
            ),
            encoding="utf-8",
        )
        return path

    def write_issue_case(self, root: Path) -> None:
        case = root / ".quality-pilot-project" / "cases" / "ISSUE-1.yaml"
        case.write_text(
            """case_id: ISSUE-1
title: Gitea #1: CLI help command fails
source:
  type: issue
  provider: gitea
  issue_id: 1
commands:
  - id: reproduce
    run: python3 --version
    expected_exit_code: 0
expected: CLI help path remains callable.
""",
            encoding="utf-8",
        )

    def write_redmine_failure_case(self, root: Path, *, gitea_issue_id: int = 501) -> None:
        case = root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml"
        case.write_text(
            f"""case_id: REDMINE-144780
title: Redmine #144780: Sensor list after cold boot
source:
  type: redmine
  provider: redmine_mcp
  redmine_issue_id: 144780
  gitea_issue_id: {gitea_issue_id}
commands:
  - id: reproduce
    run: python3 --version
    expected_exit_code: 99
expected: Sensor inventory should be populated after cold boot.
""",
            encoding="utf-8",
        )

    def test_setup_and_doctor_analyze_repo_before_runtime_questions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                """[project]
name = "demo"

[project.scripts]
democtl = "demo.cli:main"
""",
                encoding="utf-8",
            )

            code, setup = self.run_cli(["setup", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            runtime = setup["runtime_profile"]
            self.assertEqual(runtime["status"], "needs_user_confirmation")
            self.assertEqual(runtime["repo_analysis"]["suggested_primary_entrypoint"], "democtl")
            self.assertIn("primary_entrypoint", runtime["missing_fields"])
            config_yaml = load_yaml(root / ".quality-pilot.yaml")
            self.assertIn("runtime", config_yaml)
            self.assertEqual(config_yaml["runtime"]["binary_env"], "QUALITY_PILOT_BINARY")

            code, doctor = self.run_cli(["doctor", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(doctor["runtime_profile"]["repo_analysis"]["suggested_primary_entrypoint"], "democtl")
            check = next(item for item in doctor["checks"] if item["name"] == "runtime.profile")
            self.assertEqual(check["status"], "WARN")
            self.assertEqual(doctor["hermes_needs_input"]["reason"], "runtime_profile_missing")
            prompts = [item["prompt"] for item in doctor["hermes_needs_input"]["questions"]]
            self.assertTrue(any("democtl" in prompt for prompt in prompts))
            self.assertTrue(any("\n- " in prompt for prompt in prompts))

    def test_cases_generate_uses_inferred_repo_executable_without_asking_for_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.test/demo\n\ngo 1.22\n", encoding="utf-8")
            (root / "cmd" / "democtl").mkdir(parents=True)
            binary = root / "cmd" / "democtl" / "democtl"
            binary.write_text("#!/bin/sh\nprintf 'democtl help\\n'\n", encoding="utf-8")
            binary.chmod(0o755)

            code, setup = self.run_cli(["setup", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(setup["runtime_profile"]["status"], "ready_inferred")
            self.assertFalse(setup["runtime_profile"]["needs_user_input"])
            self.assertEqual(setup["runtime_profile"]["effective"]["primary_entrypoint"], "cmd/democtl/democtl")

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--profile",
                "cli",
                "--count",
                "1",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertEqual(generated["generated_count"], 1)
            self.assertNotIn("hermes_needs_input", generated)
            case_yaml = load_yaml(root / generated["generated"][0]["path"])
            command = case_yaml["commands"][0]["run"]
            self.assertIn("cmd/democtl/democtl", command)
            self.assert_no_placeholder_command(command)

    def test_issue_case_generation_uses_runtime_binary_not_developer_or_repo_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            (root / "cmd" / "democtl").mkdir(parents=True)
            binary = root / "cmd" / "democtl" / "democtl"
            binary.write_text("#!/bin/sh\nprintf 'democtl help\\n'\n", encoding="utf-8")
            binary.chmod(0o755)
            self.write_runtime_profile(root, entrypoint="cmd/democtl/democtl")
            issues = [
                {
                    "number": 8,
                    "state": "open",
                    "title": "Runtime command fails after export",
                    "body": "Command: go test ./internal/export -run TestCSVExport\n\nPlease verify this through the user-facing binary.",
                    "html_url": "https://git.example.test/Redfish/irctool/issues/8",
                    "updated_at": "2026-06-26T00:00:00Z",
                    "labels": [{"name": "bug"}],
                    "comments": [],
                }
            ]
            issues_json = root / "issue-with-dev-command.json"
            issues_json.write_text(json.dumps(issues), encoding="utf-8")
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            payload = generate_cases_from_issues(load_project_config(root), issue_id=8)

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(len(payload["generated"]), 1)
            case_yaml = load_yaml(root / payload["generated"][0]["path"])
            command = case_yaml["commands"][0]["run"]
            self.assertIn("cmd/democtl/democtl", command)
            self.assert_no_placeholder_command(command)
            self.assertEqual(case_yaml["quality_pilot"]["executable_scope"], "runtime_binary_safe_probe")
            self.assertEqual(case_yaml["quality_pilot"]["safe_command_source"], "runtime_profile_primary_entrypoint")
            self.assertIn("go test ./internal/export", case_yaml["quality_pilot"]["rejected_repro_command"])

    def test_issue_case_generation_requires_runtime_profile_instead_of_repo_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
            issues_json = self.write_feature_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            payload = generate_cases_from_issues(load_project_config(root), issue_id=7)

            self.assertEqual(payload["status"], "needs_input")
            self.assertEqual(payload["generated_count"], 0)
            self.assertEqual(payload["interaction_scope"], "runtime_profile_required")
            self.assertFalse(any((root / ".quality-pilot-project" / "cases").glob("ISSUE-*.yaml")))

    def test_issues_sync_removes_closed_mirror_and_writes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            stale = root / ".quality-pilot-project" / "issues" / "2.md"
            stale.write_text("stale closed mirror", encoding="utf-8")
            issues_json = self.write_issues(root)

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json), "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["open_active_issue_ids"], [1])
            self.assertEqual(payload["closed_issue_ids"], [2])
            self.assertEqual(payload["removed_mirror_ids"], [2])
            self.assertTrue((root / ".quality-pilot-project" / "issues" / "1.md").exists())
            self.assertFalse(stale.exists())
            snapshot = json.loads((root / ".quality-pilot-project" / "state" / "issues-snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["items"][0]["issue_id"], 1)
            traceability_path = root / payload["traceability_map_path"]
            self.assertTrue(traceability_path.exists())
            traceability = json.loads(traceability_path.read_text(encoding="utf-8"))
            self.assertEqual(traceability["schema"], "quality-pilot.traceability-map.v1")
            self.assertEqual(traceability["rows"][0]["gitea_issue_id"], 1)
            self.assertEqual(traceability["rows"][0]["snapshot_case_id"], "ISSUE-1")
            self.assertIsNone(traceability["rows"][0]["case_id"])
            self.assertFalse(traceability["rows"][0]["case_runnable"])

    def test_external_candidate_developer_command_is_rejected_before_yaml_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            (root / "cmd" / "democtl").mkdir(parents=True)
            binary = root / "cmd" / "democtl" / "democtl"
            binary.write_text("#!/bin/sh\nprintf 'democtl help\\n'\n", encoding="utf-8")
            binary.chmod(0o755)
            self.write_runtime_profile(root, entrypoint="cmd/democtl/democtl")
            candidate_json = root / "candidate.json"
            candidate_json.write_text(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "title": "Developer-only candidate must not become QA coverage",
                                "feature": "democtl",
                                "expected": "The user-facing binary behavior is verified.",
                                "swqa_dimensions": ["exact_reproduction", "side_effect_safe"],
                                "commands": [
                                    {
                                        "id": "unit",
                                        "run": "go test ./internal/export -run TestCSVExport",
                                        "expected_exit_code": 0,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(CaseGenerationError) as ctx:
                generate_cases_growing(load_project_config(root), candidate_json=candidate_json, count=1)

            self.assertIn("go_test_developer_command", str(ctx.exception))
            self.assertFalse(any((root / ".quality-pilot-project" / "cases").glob("GROW-*.yaml")))

    def test_issues_sync_reads_gitea_mcp_snapshot_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_mcp_project(tmp)
            stale = root / ".quality-pilot-project" / "issues" / "2.md"
            stale.write_text("stale closed mirror", encoding="utf-8")
            mcp_path = root / ".quality-pilot-project" / "state" / "gitea-mcp" / "issues.json"
            mcp_path.parent.mkdir(parents=True, exist_ok=True)
            issues = json.loads(self.write_issues(root).read_text(encoding="utf-8"))
            mcp_path.write_text(json.dumps({"content": [{"type": "text", "text": json.dumps({"issues": issues})}]}), encoding="utf-8")

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["source"], "mcp")
            self.assertEqual(payload["mcp_issues_json"], ".quality-pilot-project/state/gitea-mcp/issues.json")
            self.assertEqual(payload["open_active_issue_ids"], [1])
            self.assertEqual(payload["removed_mirror_ids"], [2])
            self.assertTrue((root / ".quality-pilot-project" / "issues" / "1.md").exists())
            self.assertFalse(stale.exists())

    def test_issues_sync_mcp_backend_missing_snapshot_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.init_gitea_mcp_project(tmp)

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "IssueSyncError")
            self.assertIn("gitea_mcp_snapshot_missing", payload["message"])
            self.assertIn("QUALITY_PILOT_GITEA_MCP_ISSUES_JSON", payload["message"])
            self.assertNotIn("QUALITY_PILOT_" + "TRACKER_TOKEN", payload["message"])

    def test_redmine_sync_rejects_legacy_or_trimmed_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            path = root / ".quality-pilot-project" / "state" / "redmine-mcp" / "issues.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "issues": [
                            {
                                "id": 145085,
                                "subject": "Sensor list fails after cold boot",
                                "description": "Short stale summary only.",
                                "updated_on": "2026-06-24T06:17:57Z",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            code, payload = self.run_cli(["issues", "sync", "--root", tmp, "--redmine-issues", "145085", "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["error"], "RedmineError")
            self.assertIn("redmine_mcp_snapshot_unverified", payload["message"])
            self.assertIn("schema=quality-pilot.redmine-mcp-issues.v1", payload["message"])
            self.assertFalse((root / ".quality-pilot-project" / "issues" / "redmine-145085.md").exists())

    def test_issues_sync_redmine_issues_writes_local_mirrors_and_gitea_create_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_redmine_issues(root)

            code, payload = self.run_cli([
                "issues",
                "sync",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "144693",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "needs_mcp_apply")
            self.assertEqual(payload["source"], "redmine_mcp")
            self.assertEqual(payload["mode"], "redmine_issues")
            self.assertEqual(payload["imported_issue_ids"], [144780, 144693])
            self.assertEqual(payload["gitea_issue_candidate_count"], 2)
            self.assertEqual(payload["remote_write"], "needs_mcp_apply")
            self.assertEqual(payload["blocked_by_gate"], 0)
            self.assertEqual(payload["qa_summary"]["text_generation"]["task"], "redmine_issue_summary")
            self.assertEqual(payload["qa_summary"]["issues"][0]["redmine_issue_id"], 144780)
            self.assertIn("irctool sensors --login lab.yaml", payload["qa_summary"]["issues"][0]["reproduction"])
            self.assertTrue((root / ".quality-pilot-project" / "issues" / "redmine-144780.md").exists())
            self.assertTrue((root / ".quality-pilot-project" / "issues" / "gitea-candidates" / "redmine-144780.md").exists())
            self.assertTrue((root / ".quality-pilot-project" / "state" / "redmine-import.json").exists())
            state = json.loads((root / ".quality-pilot-project" / "state" / "redmine-gitea-sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["schema"], "quality-pilot.redmine-gitea-sync-state.v1")
            self.assertEqual(state["issue_candidates"][0]["action"], "create_gitea_issue_candidate")
            self.assertTrue(state["issue_candidates"][0]["write_gate_result"]["allowed"])
            self.assertEqual(state["issue_candidates"][0]["qa_text_generation"]["task"], "redmine_issue_summary")
            self.assertEqual(state["issue_candidates"][0]["qa_summary"]["redmine_issue_id"], 144780)
            body = state["issue_candidates"][0]["body"]
            self.assertIn("## Problem", body)
            self.assertIn("## QA Focus", body)
            self.assertIn("Problem to verify", body)
            self.assertIn("Missing before an executable testcase", body)
            self.assertIn("## Steps to Reproduce", body)
            self.assertIn("## Expected Result", body)
            self.assertIn("Redmine #144780", body)
            self.assertIn("After cold boot, sensor collection returns an empty list for one BMC.", body)
            self.assertIn("Steps to reproduce:", body)
            self.assertIn("Expected: populated sensor inventory should be returned after cold boot.", body)
            self.assertIn("Full journal note: cold boot reproduces after AC cycle; do not shorten this text.", body)
            self.assertIn("irctool sensors --login lab.yaml", body)
            self.assertIn("cold-boot-sensors.log", body)
            self.assertNotIn("AI Quality Pilot", body)
            self.assertNotIn("/quality-pilot", body)
            self.assertNotIn(".quality-pilot-project", body)
            self.assertNotIn("write gate", body.lower())
            self.assertNotIn("### Raw Redmine JSON", body)
            mirror_text = (root / ".quality-pilot-project" / "issues" / "redmine-144780.md").read_text(encoding="utf-8")
            self.assertIn("## Full Redmine Message", mirror_text)
            self.assertIn("### Raw Redmine JSON", mirror_text)
            self.assertIn("Steps to reproduce:", mirror_text)
            self.assertIn("Expected: populated sensor inventory should be returned after cold boot.", mirror_text)
            self.assertIn("Full journal note: cold boot reproduces after AC cycle; do not shorten this text.", mirror_text)
            self.assertIn("cold-boot-sensors.log", mirror_text)
            request_path = root / payload["mcp_issue_write_request_path"]
            self.assertTrue(request_path.exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["schema"], "quality-pilot.gitea-mcp-issue-write-request.v1")
            self.assertEqual(request["operation"], "gitea.issue.sync_from_redmine")
            self.assertEqual(request["safety"]["allowed_targets"], ["issues"])
            self.assertEqual(request["actions"][0]["operation"], "gitea.issue.create")
            self.assertEqual(request["actions"][0]["redmine_issue_id"], 144780)
            self.assertEqual(request["actions"][0]["qa_text_generation"]["task"], "redmine_issue_summary")
            self.assertEqual(request["actions"][0]["qa_summary"]["redmine_issue_id"], 144780)
            self.assertIn("Full journal note: cold boot reproduces after AC cycle; do not shorten this text.", request["actions"][0]["body"])
            self.assertIn("Steps to reproduce:", request["actions"][0]["body"])
            self.assertNotIn("AI Quality Pilot", request["actions"][0]["body"])
            ledger_path = root / payload["mcp_write_ledger_path"]
            self.assertTrue(ledger_path.exists())
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(ledger["schema"], "quality-pilot.gitea-mcp-write-ledger.v1")
            self.assertEqual(ledger["entry_count"], 2)
            entry = next(item for item in ledger["entries"] if item["redmine_issue_id"] == 144780)
            self.assertEqual(entry["target_type"], "issue_create")
            self.assertEqual(entry["operation"], "gitea.issue.create")
            self.assertEqual(entry["request_operation"], "gitea.issue.sync_from_redmine")
            self.assertEqual(entry["source_module"], "redmine_issue_sync")
            self.assertEqual(entry["request_schema"], "quality-pilot.gitea-mcp-issue-write-request.v1")
            self.assertEqual(entry["request_status"], "needs_mcp_apply")
            self.assertEqual(entry["request_path"], payload["mcp_issue_write_request_path"])
            self.assertEqual(entry["result_path"], payload["mcp_issue_write_result_path"])
            self.assertEqual(entry["idempotency_key"], request["actions"][0]["idempotency_key"])
            self.assertFalse(entry["result_exists"])
            result_path = root / payload["mcp_issue_write_result_path"]
            result_path.write_text(
                json.dumps(
                    {
                        "schema": "quality-pilot.gitea-mcp-issue-write-result.v1",
                        "status": "applied",
                        "created": [
                            {"redmine_issue_id": 144780, "issue_id": 501, "url": "https://git.example.test/issues/501"},
                            {"redmine_issue_id": 144693, "issue_id": 502, "url": "https://git.example.test/issues/502"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            code, status_payload = self.run_cli(["issues", "status", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            reconciled = next(item for item in status_payload["write_ledger"]["entries"] if item["redmine_issue_id"] == 144780)
            self.assertTrue(reconciled["result_exists"])
            self.assertEqual(reconciled["result_status"], "applied")
            self.assertEqual(reconciled["remote_id"], 501)
            trace_row = next(item for item in status_payload["traceability"] if item["gitea_issue_id"] == 501)
            self.assertEqual(trace_row["redmine_issue_ids"], [144780])
            self.assertEqual(trace_row["source"], "gitea_mcp_write_ledger")
            self.assertEqual(trace_row["source_result_status"], "applied")
            self.assertEqual(trace_row["source_result_path"], payload["mcp_issue_write_result_path"])
            self.assertEqual(trace_row["coverage_status"], "no_case")
            self.assertEqual(trace_row["repair_action"], "/quality-pilot cases generate --redmine-issues 144780")
            traceability_path = root / status_payload["traceability_map_path"]
            traceability = json.loads(traceability_path.read_text(encoding="utf-8"))
            persisted_row = next(item for item in traceability["rows"] if item["gitea_issue_id"] == 501)
            self.assertEqual(persisted_row["redmine_issue_ids"], [144780])
            self.assertEqual(persisted_row["source"], "gitea_mcp_write_ledger")
            self.assertFalse((root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml").exists())

    def test_cases_generate_redmine_issues_derives_safe_probes_without_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_runtime_profile(root, entrypoint="./irctool")
            self.write_redmine_issues(root)

            code, payload = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "144693",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["source"], "redmine")
            self.assertEqual(payload["imported_issue_ids"], [144780, 144693])
            self.assertEqual(payload["generated_count"], 2)
            self.assertEqual(payload["remote_write"], "not_applicable")
            self.assertFalse(payload.get("input_required", False))
            self.assertNotIn("interaction", payload)
            self.assertNotIn("case_generation_blockers", payload)
            self.assertEqual(payload["generated"][0]["safe_command_source"], "AI-derived product binary command from Redmine reproduction steps")
            self.assertEqual(payload["generated"][0]["safe_command_source_type"], "ai_derived")
            self.assertEqual(payload["generated"][0]["automation_confidence"], "medium")
            self.assertTrue(payload["generated"][0]["requires_prepared_environment"])
            self.assertEqual(payload["qa_summary"]["text_generation"]["task"], "redmine_issue_summary")
            self.assertEqual(payload["qa_summary"]["issues"][0]["redmine_issue_id"], 144780)
            self.assertNotIn("gitea_sync_plan_path", payload)
            self.assertNotIn("gitea_sync_state_path", payload)
            self.assertNotIn("gitea_issue_candidates", payload)
            case_path = root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml"
            self.assertTrue(case_path.exists())
            case_yaml = load_yaml(case_path)
            self.assertEqual(case_yaml["quality_pilot"]["safe_command_source_type"], "ai_derived")
            self.assertEqual(case_yaml["quality_pilot"]["executable_scope"], "redmine_ai_derived_safe_probe")
            self.assertIn("product binary", case_yaml["quality_pilot"]["safe_command_source"])
            self.assertTrue(case_yaml["quality_pilot"]["requires_prepared_environment"])
            self.assertTrue(any("QUALITY_PILOT_BINARY" in item for item in case_yaml["quality_pilot"]["environment_requirements"]))
            self.assertIn('${QUALITY_PILOT_BINARY:-./irctool}', case_yaml["commands"][0]["run"])
            self.assertIn("sensors --login", case_yaml["commands"][0]["run"])
            self.assertIn("QUALITY_PILOT_LOGIN_FILE", case_yaml["commands"][0]["run"])
            self.assertIn("Exact lab reproduction remains advisory", case_yaml["quality_pilot"]["follow_up_needed"][-1])
            self.assert_no_placeholder_command(case_yaml["commands"][0]["run"])
            second_case_yaml = load_yaml(root / ".quality-pilot-project" / "cases" / "REDMINE-144693.yaml")
            self.assertIn("./irctool", second_case_yaml["commands"][0]["run"])
            self.assert_no_placeholder_command(second_case_yaml["commands"][0]["run"])

            chat = hermes.dispatch_chat_command("/quality-pilot cases generate --redmine-issues 144780 144693", root=root)
            self.assertEqual(chat["exit_code"], 0)
            self.assertEqual(chat["payload"]["status"], "ok")
            self.assertNotIn("hermes_needs_input", chat["payload"])
            self.assertNotIn("需要補充資訊", chat["chat_response"])

    def test_issues_report_writes_gated_evidence_update_for_linked_failed_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_redmine_issues(root)

            code, sync_payload = self.run_cli([
                "issues",
                "sync",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "--json",
            ])
            self.assertEqual(code, 0)
            result_path = root / sync_payload["mcp_issue_write_result_path"]
            result_path.write_text(
                json.dumps(
                    {
                        "schema": "quality-pilot.gitea-mcp-issue-write-result.v1",
                        "status": "applied",
                        "created": [{"redmine_issue_id": 144780, "issue_id": 501, "url": "https://git.example.test/issues/501"}],
                    }
                ),
                encoding="utf-8",
            )
            self.write_redmine_failure_case(root, gitea_issue_id=501)

            code, run_payload = self.run_cli(["cases", "run", "--root", tmp, "--json", "REDMINE-144780"])
            self.assertEqual(code, 1)
            self.assertEqual(run_payload["status"], "FAIL")

            code, report_payload = self.run_cli(["issues", "report", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(report_payload["status"], "needs_mcp_apply")
            self.assertEqual(report_payload["evidence_update_count"], 1)
            self.assertEqual(report_payload["blocked_by_gate"], 0)
            request_path = root / report_payload["mcp_issue_evidence_write_request_path"]
            self.assertTrue(request_path.exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["operation"], "gitea.issue.evidence_update")
            self.assertEqual(request["safety"]["allowed_operations"], ["gitea.issue.update"])
            action = request["actions"][0]
            self.assertEqual(action["operation"], "gitea.issue.update")
            self.assertEqual(action["update_kind"], "evidence")
            self.assertEqual(action["gitea_issue_id"], 501)
            self.assertEqual(action["redmine_issue_id"], 144780)
            self.assertEqual(action["redmine_issue_ids"], [144780])
            self.assertEqual(action["case_id"], "REDMINE-144780")
            self.assertEqual(action["status"], "FAIL")
            self.assertTrue(action["write_gate_result"]["allowed"])
            self.assertIn("## QA Evidence Update", action["body"])
            self.assertIn("## Reproduction Command", action["body"])
            self.assertIn("REDMINE-144780", action["body"])
            self.assertIn(run_payload["results"][0]["result_path"], action["body"])
            self.assertNotIn("### Raw", action["body"])
            report_json = json.loads((root / report_payload["report_json_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report_json["issues"][0]["gitea_issue_id"], 501)
            ledger = json.loads((root / report_payload["mcp_write_ledger_path"]).read_text(encoding="utf-8"))
            entry = next(item for item in ledger["entries"] if item["request_path"] == report_payload["mcp_issue_evidence_write_request_path"])
            self.assertEqual(entry["target_type"], "issue_evidence_update")
            self.assertEqual(entry["operation"], "gitea.issue.update")
            self.assertEqual(entry["source_module"], "issues_report")
            self.assertEqual(entry["redmine_issue_id"], 144780)

    def test_cases_generate_redmine_issues_uses_user_confirmed_safe_probe_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            snapshot_path = self.write_redmine_issues(root)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            for issue in snapshot["issues"]:
                if issue["id"] == 144780:
                    issue["custom_fields"].append({"id": 9, "name": "Safe Probe Command", "value": "irctool --version"})
                    issue["custom_fields"].append({"id": 10, "name": "Expected Exit Code", "value": "0"})
                    issue["custom_fields"].append({"id": 11, "name": "Safe Probe Fixture / Environment", "value": "No lab fixture required; local Python interpreter only."})
                    issue["custom_fields"].append({"id": 12, "name": "Pass/Fail Oracle", "value": "Command exits 0 and prints Python version."})
                    issue["custom_fields"].append({"id": 13, "name": "Side Effect Boundary", "value": "Read-only local version check; no network, hardware, or file writes."})
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            code, payload = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["generated_count"], 1)
            self.assertFalse(payload.get("input_required", False))
            case_path = root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml"
            self.assertTrue(case_path.exists())
            case_yaml = load_yaml(root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml")
            self.assertEqual(case_yaml["source"]["redmine_issue_id"], 144780)
            self.assertEqual(case_yaml["source"]["qa_summary"]["redmine_issue_id"], 144780)
            self.assertIn("environment", case_yaml["source"]["qa_summary"])
            self.assertIn("reproduction", case_yaml["source"]["qa_summary"])
            self.assertIn("observed", case_yaml["source"]["qa_summary"])
            self.assertIn("expected", case_yaml["source"]["qa_summary"])
            self.assertIn("evidence", case_yaml["source"]["qa_summary"])
            self.assertIn("missing_for_executable_case", case_yaml["source"]["qa_summary"])
            self.assertEqual(case_yaml["source"]["safe_runner"]["command"], "irctool --version")
            self.assertEqual(case_yaml["source"]["safe_runner"]["command_source"], "Safe Probe Command")
            self.assertEqual(case_yaml["source"]["safe_runner"]["expected_exit_code"], 0)
            confirmed = case_yaml["source"]["safe_runner"]["user_confirmed_inputs"]
            self.assertTrue(any("No lab fixture required" in item["value"] for item in confirmed["fixtures_environment"]))
            self.assertTrue(any("prints Python version" in item["value"] for item in confirmed["oracle"]))
            self.assertTrue(any("Read-only local version check" in item["value"] for item in confirmed["side_effect_boundaries"]))
            self.assertIn("Steps to reproduce:", case_yaml["source"]["redmine_message"])
            self.assertIn("Expected: populated sensor inventory should be returned after cold boot.", case_yaml["source"]["redmine_message"])
            self.assertIn("Full journal note: cold boot reproduces after AC cycle; do not shorten this text.", case_yaml["source"]["redmine_message"])
            self.assertIn("irctool sensors --login lab.yaml", case_yaml["source"]["redmine_message"])
            self.assertIn("cold-boot-sensors.log", case_yaml["source"]["redmine_message"])
            self.assertIn("### Raw Redmine JSON", case_yaml["source"]["redmine_message"])
            self.assertNotIn("gitea_sync_plan", case_yaml["source"])
            self.assertEqual(case_yaml["quality_pilot"]["executable_scope"], "redmine_user_confirmed_safe_probe")
            self.assertEqual(case_yaml["quality_pilot"]["safe_command_source"], "Safe Probe Command")
            self.assertEqual(case_yaml["quality_pilot"]["safe_runner"]["command"], "irctool --version")
            self.assertEqual(case_yaml["commands"][0]["id"], "safe_probe")
            self.assertEqual(case_yaml["commands"][0]["run"], "irctool --version")
            self.assertNotIn("__quality_pilot_invalid_command__", case_yaml["commands"][0]["run"])

    def test_cases_generate_redmine_issues_keeps_incomplete_safe_runner_as_follow_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            snapshot_path = self.write_redmine_issues(root)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            for issue in snapshot["issues"]:
                if issue["id"] == 144780:
                    issue["custom_fields"].append({"id": 9, "name": "Safe Probe Command", "value": "irctool --version"})
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            code, payload = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["generated_count"], 1)
            case_path = root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml"
            self.assertTrue(case_path.exists())
            case_yaml = load_yaml(case_path)
            self.assertEqual(case_yaml["quality_pilot"]["safe_command_source_type"], "user_confirmed")
            self.assertEqual(case_yaml["commands"][0]["run"], "irctool --version")
            follow_up = case_yaml["quality_pilot"]["follow_up_needed"]
            self.assertIn("fixtures, environment, credentials, or explicit none-required note", follow_up)
            self.assertIn("pass/fail oracle or expected result", follow_up)
            self.assertIn("side-effect boundaries", follow_up)
            self.assertNotIn("__quality_pilot_invalid_command__", case_yaml["commands"][0]["run"])

    def test_cases_generate_redmine_issues_rejects_developer_safe_command_and_uses_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            snapshot_path = self.write_redmine_issues(root)
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            for issue in snapshot["issues"]:
                if issue["id"] == 144780:
                    issue["custom_fields"].append(
                        {
                            "id": 9,
                            "name": "Safe Probe Command",
                            "value": "go test ./internal/diaglog -run TestCollectAndFindEntry_ValidatesWaitConfigBeforeResourceInUse -count=1",
                        }
                    )
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            code, payload = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["generated_count"], 1)
            case_path = root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml"
            case_yaml = load_yaml(case_path)
            self.assertEqual(case_yaml["quality_pilot"]["safe_command_source_type"], "ai_derived")
            self.assertEqual(case_yaml["quality_pilot"]["safe_runner"]["rejected_safe_command"]["source"], "Safe Probe Command")
            self.assertIn("go test ./internal/diaglog", case_yaml["quality_pilot"]["safe_runner"]["rejected_safe_command"]["run"])
            self.assertIn('${QUALITY_PILOT_BINARY:-./irctool}', case_yaml["commands"][0]["run"])
            self.assertIn("sensors --login", case_yaml["commands"][0]["run"])
            self.assertIn("QUALITY_PILOT_LOGIN_FILE", case_yaml["commands"][0]["run"])
            self.assertNotIn("go test", case_yaml["commands"][0]["run"])
            self.assertNotIn("go run", case_yaml["commands"][0]["run"])
            self.assertTrue(any("Build or install the product binary" in item for item in case_yaml["quality_pilot"]["environment_requirements"]))

    def test_cases_generate_redmine_issues_regenerates_stale_generic_probe_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_redmine_issues(root)
            case_path = root / ".quality-pilot-project" / "cases" / "REDMINE-144780.yaml"
            case_path.write_text(
                """case_id: REDMINE-144780
title: Stale generic Redmine probe
source:
  type: redmine
  provider: redmine
  redmine_issue_id: 144780
commands:
  - id: safe_probe
    run: sh -c 'go run ./cmd/irctool __quality_pilot_invalid_command__ >/dev/null 2>&1; test $? -ne 0'
    expected_exit_code: 0
""",
                encoding="utf-8",
            )

            code, payload = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--redmine-issues",
                "144780",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["generated_count"], 1)
            self.assertEqual(payload["skipped_count"], 0)
            self.assertTrue(payload["generated"][0]["regenerated_from_stale_generic_probe"])
            case_yaml = load_yaml(case_path)
            self.assertTrue(case_yaml["quality_pilot"]["regenerated_from_stale_generic_probe"])
            self.assertEqual(case_yaml["quality_pilot"]["regeneration_reason"], "redmine_generic_invalid_command_probe")
            self.assertNotIn("__quality_pilot_invalid_command__", case_yaml["commands"][0]["run"])

    def test_submit_fix_pr_title_includes_purpose_summary_and_issue_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])
            self.write_issue_case(root)
            run_code, run_payload = self.run_cli(["cases", "run", "--root", tmp, "ISSUE-1", "--json"])
            self.assertEqual(run_code, 0)
            self.assertEqual(run_payload["status"], "PASS")

            payload = submit_fix_pr(load_project_config(root), issue_id=1, dry_run=True)

            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["push_pr_blockers"], [])
            self.assertEqual(payload["pr_linkage"]["gitea_issue_id"], 1)
            self.assertEqual(payload["pr_linkage"]["case_ids"], ["ISSUE-1"])
            self.assertTrue(payload["pr_linkage"]["evidence_paths"])
            self.assertEqual(payload["pr_payload"]["title"], "Fix Gitea #1: CLI help command fails")
            body = payload["pr_payload"]["body"]
            self.assertIn("## Problem", body)
            self.assertIn("CLI help command fails", body)
            self.assertIn("Command: python3 --version", body)
            self.assertIn("## How to Reproduce", body)
            self.assertIn("## Linked Tickets", body)
            self.assertIn("- Gitea #1", body)
            self.assertIn("## Traceability", body)
            self.assertIn("- Gitea issue: #1", body)
            self.assertIn("- Case IDs: ISSUE-1", body)
            self.assertIn("## Evidence", body)
            self.assertIn(".quality-pilot-project/evidence/", body)
            self.assertIn("## Verification", body)
            self.assertIn("- ISSUE-1", body)
            self.assertNotIn("AI Quality Pilot", body)
            self.assertNotIn("/quality-pilot", body)

    def test_push_pr_mcp_blocked_records_pr_linkage_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])
            self.write_issue_case(root)
            run_code, run_payload = self.run_cli(["cases", "run", "--root", tmp, "ISSUE-1", "--json"])
            self.assertEqual(run_code, 0)
            self.assertEqual(run_payload["status"], "PASS")

            code, payload = self.run_cli(["issues", "fix", "--root", tmp, "--issue", "1", "--push-pr", "--json"])

            self.assertEqual(code, 4)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["error"], "gitea_mcp_write_not_supported")
            self.assertEqual(payload["pr_linkage"]["gitea_issue_id"], 1)
            self.assertEqual(payload["pr_linkage"]["case_ids"], ["ISSUE-1"])
            self.assertTrue(payload["pr_linkage"]["evidence_paths"])
            request = json.loads((root / payload["pr_linkage_request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["schema"], "quality-pilot.gitea-pr-linkage-request.v1")
            self.assertEqual(request["status"], "blocked")
            self.assertEqual(request["actions"][0]["operation"], "gitea.pull_request.create")
            self.assertEqual(request["actions"][0]["gitea_issue_id"], 1)
            self.assertEqual(request["actions"][0]["case_ids"], ["ISSUE-1"])
            self.assertTrue(request["actions"][0]["evidence_paths"])

            ledger = json.loads((root / payload["mcp_write_ledger_path"]).read_text(encoding="utf-8"))
            pr_entry = next(item for item in ledger["entries"] if item["target_type"] == "pr_linkage")
            self.assertEqual(pr_entry["source_module"], "issues_fix")
            self.assertEqual(pr_entry["operation"], "gitea.pull_request.create")
            self.assertEqual(pr_entry["gitea_issue_id"], 1)
            self.assertEqual(pr_entry["case_ids"], ["ISSUE-1"])
            self.assertTrue(pr_entry["evidence_paths"])
            self.assertFalse(pr_entry["gate_result"]["allowed"])

            status_code, status_payload = self.run_cli(["issues", "status", "--root", tmp, "--json"])
            self.assertEqual(status_code, 0)
            trace_row = next(item for item in status_payload["traceability"] if item["gitea_issue_id"] == 1)
            self.assertEqual(trace_row["pr_linkage"]["case_ids"], ["ISSUE-1"])
            self.assertTrue(trace_row["pr_linkage"]["evidence_paths"])

    def test_issues_fix_supports_issue_driven_handoff_after_sync_without_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_feature_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            code, payload = self.run_cli(["issues", "fix", "--root", tmp, "--issue", "7", "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "handoff")
            self.assertEqual(payload["workflow_mode"], "issue_driven_development")
            self.assertEqual(payload["case_ids"], [])
            self.assertEqual(payload["push_pr_blockers"], ["verification_case_required_before_pr"])
            self.assertTrue(any("/quality-pilot cases generate --growing" in item for item in payload["instructions"]))

    def test_issue_driven_fix_blocks_push_pr_until_verification_case_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_feature_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            code, payload = self.run_cli(["issues", "fix", "--root", tmp, "--issue", "7", "--push-pr", "--json"])

            self.assertEqual(code, 4)
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(payload["error"], "verification_case_required_before_pr")
            self.assertEqual(payload["push_pr_blockers"], ["verification_case_required_before_pr"])
            self.assertEqual(payload["plan"]["workflow_mode"], "issue_driven_development")

    def test_cases_generate_growing_review_validate_and_runs_safe_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])

            code, generated = self.run_cli(["cases", "generate", "--root", tmp, "--growing", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertEqual(generated["source"], "growth")
            self.assertEqual(generated["mode"], "growing")
            self.assertGreaterEqual(generated["growth_seed_count"], 1)
            self.assertTrue((root / ".quality-pilot-project" / "state" / "growth-context.json").exists())
            self.assertEqual(generated["interaction_scope"], "autonomous")
            self.assertEqual(generated["generated"][0]["question_count"], 0)
            self.assertEqual(generated["missing_input_count"], 0)
            self.assertEqual(generated["advisory_input_count"], 0)
            first_case = generated["generated"][0]["case_id"]
            first_yaml = load_yaml(root / generated["generated"][0]["path"])
            self.assertEqual(first_yaml["source"]["type"], "growth")
            self.assertFalse(first_yaml["quality_pilot"]["review_required_before_run"])
            self.assertEqual(first_yaml["commands"][0]["id"], "safe_probe")
            self.assertIn("six_hats", first_yaml)
            self.assertIn("growth_seed", first_yaml)
            self.assertIn("growth_reason", first_yaml)
            context = json.loads((root / ".quality-pilot-project" / "state" / "growth-context.json").read_text(encoding="utf-8"))
            self.assertEqual(context["schema"], "quality-pilot.growth-context.v1")
            self.assertEqual(context["issue_snapshot"]["open_count"], 1)
            self.assertIn("existing_cases", context)
            self.assertIn("existing_runners", context)

            code, review = self.run_cli(["cases", "review", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(review["draft_count"], 0)

            code, validated = self.run_cli(["cases", "validate", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(validated["case_count"], 1 + generated["generated_count"])

            code, run_one = self.run_cli(["cases", "run", "--root", tmp, "--json", first_case])
            self.assertEqual(code, 0)
            self.assertEqual(run_one["status"], "PASS")
            self.assertTrue(run_one["results"][0]["evidence"])

    def test_cases_generate_requires_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "explicit_generation_mode_required")
            self.assertIn("/quality-pilot cases generate --init", payload["choices"])
            self.assertIn("/quality-pilot cases generate --growing", payload["choices"])

    def test_cases_generate_init_analyzes_repo_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
            self.write_runtime_profile(root)
            (root / "README.md").write_text(
                """# Demo

Usage:

```bash
democtl --help
```
""",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                """[project]
name = "demo"

[project.scripts]
democtl = "demo.cli:main"
""",
                encoding="utf-8",
            )
            (root / "demo").mkdir()
            (root / "demo" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--feature",
                "CLI help",
                "--profile",
                "cli",
                "--count",
                "5",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertEqual(generated["source"], "init")
            self.assertEqual(generated["mode"], "init")
            self.assertEqual(generated["generation_limit"], "manual_generated_count_cap")
            self.assertEqual(generated["requested_generated_count"], 5)
            self.assertEqual(generated["requested_count"], 5)
            self.assertEqual(generated["interaction_scope"], "autonomous")
            self.assertEqual(generated["resolved_profile"], "cli")
            self.assertGreaterEqual(generated["analyzed_files_count"], 2)
            self.assertTrue((root / ".quality-pilot-project" / "state" / "init-context.json").exists())
            self.assertEqual(len(generated["generated"]), 5)
            all_dimensions = {dimension for item in generated["generated"] for dimension in item["swqa_dimensions"]}
            for dimension in ["functional", "positive", "negative", "boundary", "invalid_input", "side_effect_safe", "stress_timeout_risk"]:
                self.assertIn(dimension, all_dimensions)
            first_case = generated["generated"][0]["case_id"]
            first_path = root / generated["generated"][0]["path"]
            first_yaml = load_yaml(first_path)
            self.assertEqual(first_yaml["source"]["type"], "init")
            self.assertEqual(first_yaml["source"]["method"], "full_repo_swqa_init")
            self.assertFalse(first_yaml["quality_pilot"]["review_required_before_run"])
            self.assertTrue(first_yaml["quality_pilot"]["executable"])
            self.assertEqual(first_yaml["quality_pilot"]["questions"], [])
            self.assertEqual(first_yaml["commands"][0]["id"], "safe_probe")
            for item in generated["generated"]:
                case_yaml = load_yaml(root / item["path"])
                command = case_yaml["commands"][0]["run"]
                self.assertIn("QUALITY_PILOT_BINARY", command)
                self.assert_no_placeholder_command(command)
            self.assertIn("risk_controls", first_yaml)
            self.assertIn("init_seed", first_yaml)

            code, review = self.run_cli(["cases", "review", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(review["draft_count"], 0)

            code, validated = self.run_cli(["cases", "validate", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(validated["case_count"], 6)

            code, removed_dry_run = self.run_cli(["qa-test", "dry-run", "--root", tmp, "--case-id", first_case, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(removed_dry_run["error"], "command_removed")
            self.assertEqual(removed_dry_run["replacement"], "/quality-pilot cases run")

            code, run_one = self.run_cli(["cases", "run", "--root", tmp, "--json", first_case])
            self.assertEqual(code, 0)
            self.assertEqual(run_one["status"], "PASS")
            self.assertEqual(run_one["results"][0]["commands"][0]["id"], "safe_probe")

            code, second = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--feature",
                "CLI help",
                "--profile",
                "cli",
                "--count",
                "5",
                "--json",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(second["generated_count"], 0)
            self.assertGreaterEqual(second["deduped_count"], 1)

    def test_cases_generate_init_requires_runtime_profile_instead_of_placeholder_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
            (root / "go.mod").write_text("module example.test/demo\n\ngo 1.22\n", encoding="utf-8")
            (root / "cmd" / "democtl").mkdir(parents=True)
            (root / "cmd" / "democtl" / "main.go").write_text(
                "package main\n\nfunc main() {}\n",
                encoding="utf-8",
            )

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--profile",
                "cli",
                "--count",
                "1",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "needs_input")
            self.assertEqual(generated["generated_count"], 0)
            self.assertEqual(generated["interaction_scope"], "runtime_profile_required")
            self.assertEqual(generated["hermes_needs_input"]["reason"], "runtime_profile_missing")
            self.assertEqual(generated["runtime_profile"]["repo_analysis"]["suggested_primary_entrypoint"], "democtl")
            self.assertFalse(any((root / ".quality-pilot-project" / "cases").glob("GEN-*.yaml")))

    def test_cases_generate_init_default_uses_full_seed_dimension_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            (root / "README.md").write_text("# Demo CLI\n\nUse democtl for status checks.\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                """[project]
name = "demo"

[project.scripts]
democtl = "demo.cli:main"
""",
                encoding="utf-8",
            )
            (root / "demo").mkdir()
            (root / "demo" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--feature",
                "CLI help",
                "--profile",
                "cli",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["generation_limit"], "all_init_seed_dimension_pairs")
            self.assertIsNone(generated["requested_generated_count"])
            self.assertIsNone(generated["requested_count"])
            self.assertGreater(generated["generated_count"], 5)
            self.assertEqual(
                generated["candidate_count"],
                generated["generated_count"] + generated["deduped_count"] + generated["skipped_count"],
            )

    def test_cases_generate_init_fast_uses_autonomous_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            (root / "README.md").write_text("# Demo CLI\n\nUse democtl for status checks.\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                """[project]
name = "demo"

[project.scripts]
democtl = "demo.cli:main"
""",
                encoding="utf-8",
            )

            code, generated = self.run_cli([
                "cases",
                "generate",
                "--root",
                tmp,
                "--init",
                "--count",
                "5",
                "--json",
            ])

            self.assertEqual(code, 0)
            self.assertEqual(generated["status"], "ok")
            self.assertTrue(generated["fast"])
            self.assertEqual(generated["interaction_scope"], "autonomous")
            self.assertEqual(generated["missing_input_count"], 0)
            self.assertEqual(generated["advisory_input_count"], 0)
            self.assertEqual(generated["questions"], [])
            self.assertEqual(generated["requested_generated_count"], 5)
            self.assertTrue(generated["fast_mode_assumptions"])

    def test_cases_generate_from_issues_returns_removed_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--from-issues", "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "command_removed")
            self.assertEqual(payload["replacement"], "/quality-pilot cases generate --growing")

    def test_cases_generate_candidate_json_is_removed_public_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
            candidate = root / "candidates.json"
            candidate.write_text(
                json.dumps({
                    "candidates": [
                        {
                            "title": "Unsafe prompt leak",
                            "growth_reason": "system prompt says write .qa/runs/heartbeat-latest.json",
                            "expected": "No leak",
                        }
                    ]
                }),
                encoding="utf-8",
            )

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--growing", "--candidate-json", str(candidate), "--json"])

            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "command_removed")
            self.assertEqual(payload["replacement"], "/quality-pilot cases generate --growing")

    def test_policy_pack_is_generic_closed_loop_swqa(self) -> None:
        payload = policy_pack()
        self.assertEqual(payload["closed_loop_steps"], ["Observe", "Normalize", "Execute", "Triage", "Publish", "Evolve", "Prune"])
        for dimension in ["exact_reproduction", "functional", "positive", "negative", "boundary", "invalid_input", "sibling_surface", "side_effect_safe", "stress_timeout_risk"]:
            self.assertIn(dimension, payload["swqa_dimensions"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for project_only_word in ["irctool", "Redfish", "VM_HTTP_URL", "GID-Ubuntu"]:
            self.assertNotIn(project_only_word, serialized)

    def test_publish_wiki_plan_and_issues_fix_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)
            self.run_cli(["issues", "sync", "--root", tmp, "--issues-json", str(issues_json)])
            self.write_issue_case(root)
            self.run_cli(["close-loop", "run-once", "--root", tmp, "--case-id", "ISSUE-1"])

            code, removed_publish = self.run_cli(["publish", "plan", "--root", tmp, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(removed_publish["error"], "command_removed")
            self.assertEqual(removed_publish["replacement"], "/quality-pilot publish wiki plan")

            code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["blocked_by_gate"], 0)
            self.assertEqual(plan["blocked_reasons"], [])
            self.assertTrue((root / ".quality-pilot-project" / "state" / "wiki-plan.json").exists())

            code, apply_result = self.run_cli(["publish", "apply", "--root", tmp, "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(apply_result["error"], "command_removed")
            self.assertEqual(apply_result["replacement"], "/quality-pilot publish wiki apply")

            code, fix_plan = self.run_cli(["issues", "fix", "--root", tmp, "--issue", "1", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(fix_plan["status"], "handoff")
            self.assertIn("ISSUE-1", fix_plan["case_ids"])

            code, removed_fix = self.run_cli(["fix-issues", "plan", "--root", tmp, "--issue", "1", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(removed_fix["error"], "command_removed")
            self.assertEqual(removed_fix["replacement"], "/quality-pilot issues fix --issue <id>")

    def test_publish_wiki_render_uses_fixed_sections_and_dynamic_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["page"], "Quality Pilot Test Status")
            report = (root / ".quality-pilot-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            for heading in [
                "# Quality Pilot Test Status",
                "## 總覽",
                "## 測試結果明細",
                "## 補充 partial probes（不併入正式 case counters）",
                "## 活動中的 Gitea issues",
                "## 已關閉／歷史 issues（不列 active blocker）",
                "## 六色帽回顧",
            ]:
                self.assertIn(heading, report)
            self.assertIn("### CLI Smoke", report)
            self.assertTrue((root / ".quality-pilot-project" / "rules" / "wiki-categories.yaml").exists())

    def test_cases_generate_init_auto_plans_wiki_without_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
            self.write_runtime_profile(root)

            code, payload = self.run_cli(["cases", "generate", "--root", tmp, "--init", "--count", "2", "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["wiki"]["auto_sync"]["event"], "case_generation")
            self.assertEqual(payload["wiki"]["auto_sync"]["status"], "blocked")
            self.assertTrue((root / ".quality-pilot-project" / "state" / "wiki-plan.json").exists())
            report = (root / ".quality-pilot-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            self.assertIn("NOT_RUN", report)
            self.assertNotIn("PASS：1", report)

    def test_qa_test_run_one_auto_plans_test_result_wiki(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])

            code, payload = self.run_cli(["cases", "run", "--root", tmp, "--json", "EXAMPLE-001"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["wiki"]["auto_sync"]["event"], "test_result")
            self.assertTrue((root / ".quality-pilot-project" / "state" / "latest-run.json").exists())
            self.assertTrue((root / ".quality-pilot-project" / "state" / "wiki-plan.json").exists())
            report = (root / ".quality-pilot-project" / "reports" / "wiki-status.md").read_text(encoding="utf-8")
            self.assertIn("| EXAMPLE-001 | CLI Smoke | PASS |", report)

    def test_partial_probes_do_not_count_as_official_case_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.run_cli(["setup", "--root", tmp])
            partial_path = root / ".quality-pilot-project" / "cases" / "PARTIAL-001.yaml"
            partial_path.write_text(
                """case_id: PARTIAL-001
title: "Supplemental version probe"
partial_probe: true
commands:
  - id: version
    run: python3 --version
    expected_exit_code: 0
""",
                encoding="utf-8",
            )

            code, payload = self.run_cli(["cases", "run", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(payload["case_counts"]["PASS"], 1)
            self.assertEqual(payload["partial_probe_counts"]["PASS"], 1)
            partial_result = next(item for item in payload["results"] if item["case_id"] == "PARTIAL-001")
            self.assertTrue(partial_result["partial_probe"])

            code, report_payload = self.run_cli(["report", "status", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            report = Path(report_payload["report_path"]).read_text(encoding="utf-8")
            self.assertIn("## Official Case Counters", report)
            self.assertIn("## Partial Probes", report)
            self.assertIn("Partial probes are supplemental diagnostics", report)
            self.assertIn("| PARTIAL-001 | PASS |", report)

    def test_publish_wiki_apply_mcp_backend_creates_gated_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_mcp_project(tmp)
            self.write_issue_case(root)
            self.run_cli(["cases", "run", "--root", tmp, "ISSUE-1"])

            code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["remote"]["backend"], "mcp")
            self.assertTrue(plan["remote"]["requires_hermes_mcp_apply"])
            self.assertNotIn("gitea_mcp_write_not_supported", plan["blocked_reasons"])

            code, apply_result = self.run_cli(["publish", "wiki", "apply", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(apply_result["status"], "needs_mcp_apply")
            self.assertEqual(apply_result["remote"]["write_backend"], "hermes_gitea_mcp")
            request_path = root / apply_result["mcp_write_request_path"]
            self.assertTrue(request_path.exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["schema"], "quality-pilot.gitea-mcp-wiki-write-request.v1")
            self.assertEqual(request["operation"], "gitea.wiki.update_page")
            self.assertIsNone(request["repo"])
            self.assertEqual(request["repo_source"], "hermes_session")
            self.assertEqual(request["page"], "Quality Pilot Test Status")
            self.assertIn("## 總覽", request["body"])
            self.assertEqual(request["safety"]["allowed_targets"], ["wiki"])
            ledger_path = root / apply_result["mcp_write_ledger_path"]
            self.assertTrue(ledger_path.exists())
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(ledger["schema"], "quality-pilot.gitea-mcp-write-ledger.v1")
            self.assertGreaterEqual(ledger["entry_count"], 1)
            entry = next(item for item in ledger["entries"] if item["request_path"] == apply_result["mcp_write_request_path"])
            self.assertEqual(entry["target_type"], "wiki_update")
            self.assertEqual(entry["operation"], "gitea.wiki.update_page")
            self.assertEqual(entry["source_module"], "publish_wiki_apply")
            self.assertEqual(entry["request_schema"], "quality-pilot.gitea-mcp-wiki-write-request.v1")
            self.assertEqual(entry["request_path"], apply_result["mcp_write_request_path"])
            self.assertEqual(entry["result_path"], apply_result["mcp_write_result_path"])
            self.assertFalse(entry["result_exists"])

            result_path = root / apply_result["mcp_write_result_path"]
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps({"status": "ok", "url": "https://git.example.test/wiki/Test-status"}), encoding="utf-8")

            code, status_payload = self.run_cli(["publish", "wiki", "status", "--root", tmp, "--json"])
            self.assertEqual(code, 0)
            reconciled = next(
                item
                for item in status_payload["write_ledger"]["entries"]
                if item["request_path"] == apply_result["mcp_write_request_path"]
            )
            self.assertTrue(reconciled["result_exists"])
            self.assertEqual(reconciled["result_status"], "ok")
            self.assertEqual(reconciled["remote_id"], "https://git.example.test/wiki/Test-status")

            code, completed = self.run_cli([
                "publish",
                "wiki",
                "complete-mcp",
                "--root",
                tmp,
                "--result-json",
                str(result_path),
                "--json",
            ])

            self.assertEqual(code, 2)
            self.assertEqual(completed["status"], "error")
            self.assertEqual(completed["error"], "command_removed")
            self.assertEqual(completed["replacement"], "/quality-pilot publish wiki apply")

    def test_publish_wiki_apply_uses_mcp_handoff_not_internal_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            self.write_issue_case(root)
            self.run_cli(["cases", "run", "--root", tmp, "ISSUE-1"])

            code, plan = self.run_cli(["publish", "wiki", "plan", "--root", tmp, "--event", "test_result", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(plan["status"], "ready")
            self.assertNotIn("token_env", plan["remote"])
            self.assertNotIn("token_present", plan["remote"])

            code, apply_result = self.run_cli(["publish", "wiki", "apply", "--root", tmp, "--json"])

            self.assertEqual(code, 0)
            self.assertEqual(apply_result["status"], "needs_mcp_apply")
            self.assertEqual(apply_result["mcp_write_request"]["repo_source"], "hermes_session")

    def test_hermes_new_workflow_commands_and_qa_test_without_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_project(tmp)
            issues_json = self.write_issues(root)

            help_result = hermes.dispatch_chat_command("/quality-pilot qa-test", root=root)
            self.assertEqual(help_result["status"], "error")
            self.assertEqual(help_result["payload"]["error"], "command_removed")
            self.assertEqual(help_result["payload"]["replacement"], "/quality-pilot cases run")
            self.assertIn("下一步可以選", help_result["chat_response"])
            self.assertTrue(help_result["payload"]["next_actions"])

            sync = hermes.dispatch_chat_command(f"/quality-pilot issues sync --issues-json {issues_json}", root=root)
            self.assertEqual(sync["status"], "ok")
            self.assertEqual(sync["payload"]["open_count"], 1)
            sync_actions = [item["command"] for item in sync["payload"]["next_actions"]]
            self.assertEqual(sync_actions[0], "/quality-pilot issues fix --issue <id>")
            self.assertIn("/quality-pilot cases generate --growing", sync_actions)

            renamed = hermes.dispatch_chat_command("/quality-pilot cases generate --from-issues", root=root)
            self.assertEqual(renamed["status"], "error")
            self.assertEqual(renamed["payload"]["error"], "command_removed")
            self.assertEqual(renamed["payload"]["replacement"], "/quality-pilot cases generate --growing")

            mode_required = hermes.dispatch_chat_command("/quality-pilot cases generate", root=root)
            self.assertEqual(mode_required["status"], "error")
            self.assertEqual(mode_required["payload"]["error"], "explicit_generation_mode_required")
            self.assertEqual(mode_required["payload"]["next_actions"][0]["command"], "/quality-pilot cases generate --init")

            init = hermes.dispatch_chat_command('/quality-pilot cases generate --init --feature "CLI help" --profile cli --count 2', root=root)
            self.assertEqual(init["status"], "ok")
            self.assertEqual(init["payload"]["source"], "init")
            self.assertIn("init_context:", init["chat_response"])
            self.assertIn("generated_cases:", init["chat_response"])
            self.assertFalse(init["payload"].get("input_required", False))
            self.assertNotIn("interaction", init["payload"])
            self.assertNotIn("hermes_needs_input", init["payload"])
            self.assertNotIn("Hermes needs your input", init["chat_response"])
            self.assertNotIn("需要補充資訊", init["chat_response"])
            self.assertTrue(any(action.get("command") == "/quality-pilot cases validate" for action in init["payload"]["next_actions"]))

            fast = hermes.dispatch_chat_command('/quality-pilot cases generate --init --feature "CLI help" --profile cli --fast --count 1', root=root)
            self.assertEqual(fast["status"], "error")
            self.assertEqual(fast["payload"]["error"], "command_removed")
            self.assertEqual(fast["payload"]["replacement"], "/quality-pilot cases generate --init")

    def test_hermes_mcp_snapshot_error_guides_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.init_gitea_mcp_project(tmp)

            result = hermes.dispatch_chat_command("/quality-pilot issues sync", root=root)

            self.assertEqual(result["status"], "error")
            self.assertIn("gitea_mcp_snapshot_missing", result["chat_response"])
            self.assertIn("下一步可以選", result["chat_response"])
            self.assertEqual(result["payload"]["next_actions"][0]["command"], "/quality-pilot issues sync")
            self.assertTrue(result["payload"]["next_actions"][0]["requires_confirmation"])

    def test_status_and_doctor_detect_missing_gitea_mcp_snapshot_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.init_gitea_mcp_project(tmp)

            status = hermes.dispatch_chat_command("/quality-pilot status", root=tmp)
            self.assertEqual(status["status"], "error")
            self.assertEqual(status["payload"]["error"], "command_removed")
            self.assertEqual(status["payload"]["replacement"], "/quality-pilot doctor")

            doctor = hermes.dispatch_chat_command("/quality-pilot doctor", root=tmp)
            self.assertEqual(doctor["status"].lower(), "warn")
            self.assertFalse(doctor["payload"]["issue_sync"]["issue_sync_ready"])
            self.assertIn("gitea_mcp_snapshot_missing", doctor["chat_response"])
            self.assertIn("需要處理", doctor["chat_response"])


if __name__ == "__main__":
    unittest.main()
