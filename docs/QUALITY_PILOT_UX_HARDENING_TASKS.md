# AI Quality Pilot UX Hardening Tasks

status: implemented
created: 2026-06-24
source: `/root/SPEC_ID quality-pilot-ux-hardening-.txt`
scope: UX recovery, ID mapping, handoff consistency, readiness clarity, MCP transparency, and longer-term SWQA hardening

## North Star

把 AI Quality Pilot 從「正確但需要懂流程的命令集合」升級成「單議題任務代理」：使用者只給 Redmine ID，也能在 3 次指令內被帶到可修復、可執行、可驗證狀態，不需要人工換算 Redmine/Gitea/Case ID。

## Success Metrics

| Metric | Baseline | Target | Evidence |
|---|---:|---:|---|
| First-try success rate | Instrumented, pending rollout sample | +20% | `.quality-pilot-project/state/ux-metrics.jsonl` |
| Average retries before success | Instrumented, pending rollout sample | -30% | `.quality-pilot-project/state/ux-metrics.jsonl` |
| Error-to-recovery time | Instrumented, pending rollout sample | -40% | `.quality-pilot-project/state/ux-metrics.jsonl` |
| Handoff runnable-case mismatch | 0 known regression-test cases | 0 known cases | T5 regression test |
| Redmine/Gitea/Case ID manual conversion | required | not required for common paths | T3, T4 regression tests |

## Phase P0: Recovery POC

Goal: 修掉最常見的 UX 斷裂，讓錯誤回應可被 Hermes agent 穩定恢復。

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P0-01 | Add `payload.ux_recovery` to non-OK responses | Codex | Done | Error/warn response includes `problem_class`, `root_cause`, `recommended_command`, confirmation flag, and confidence |
| P0-02 | Implement ID Domain Resolver | Codex | Done | `redmine-<id>`, `ISSUE-<id>`, case id, and numeric id resolve to canonical command when unique |
| P0-03 | Add handoff consistency gate | Codex | Done | `issues fix --issue <id>` never emits a handoff with non-runnable `case_ids[]` |
| P0-04 | Make `next_actions` recovery-first | Codex | Done | Recovery commands appear before wiki/report/push-pr after recoverable errors |
| P0-05 | Add P0 regression tests T1-T5 | Codex | Done | Tests pass locally and in CI |

### P0-01 UX Recovery Schema

- [x] Add dispatcher helper to classify recoverable problems.
- [x] Populate `payload.ux_recovery.problem_class`.
- [x] Populate `payload.ux_recovery.root_cause`.
- [x] Populate `payload.ux_recovery.recommended_command`.
- [x] Populate `payload.ux_recovery.recommended_command_requires_confirmation`.
- [x] Populate `payload.ux_recovery.confidence`.
- [x] Update Hermes skill text to prefer `payload.ux_recovery.recommended_command`.

Allowed `problem_class` values:

```text
typo_argument
removed_command
id_domain_mismatch
case_not_found
handoff_inconsistent
mcp_not_ready
write_gate_blocked
```

### P0-02 ID Domain Resolver

- [x] Accept Redmine IDs in user-facing recovery paths, for example `redmine-145085`.
- [x] Map Redmine to Gitea from `.quality-pilot-project/state/gitea-mcp/issue-write-result.json`.
- [x] Map Gitea to Case from `.quality-pilot-project/state/issues-snapshot.json`.
- [x] Map Case aliases from `.quality-pilot-project/cases/*.yaml`.
- [x] Return `payload.id_resolution.input_id`.
- [x] Return `payload.id_resolution.input_domain`.
- [x] Return `payload.id_resolution.resolved_gitea_issue_id`.
- [x] Return `payload.id_resolution.resolved_case_id`.
- [x] Return `payload.id_resolution.resolution_source`.
- [x] Clarify only when multiple valid mappings exist.

### P0-03 Handoff Consistency Gate

- [x] Validate every `case_ids[]` in fix handoff against `cases list`.
- [x] If no runnable case exists, return `status: handoff_blocked`.
- [x] Return `payload.error: handoff_case_id_not_runnable`.
- [x] Return `payload.recovered_case_ids`.
- [x] Make the first `next_actions` item a directly runnable case command when one exists.

### P0-04 Recovery-first next_actions

- [x] `case_not_found`: recommend `cases list`, then `cases run <exact_case_id>`.
- [x] `id_domain_mismatch`: recommend `issues status`, then mapped fix command.
- [x] `removed_command`: recommend the replacement command.
- [x] `mcp_not_ready`: recommend `doctor` or MCP status repair.
- [x] Do not show push-pr in the first three recovery actions.

## Phase P1: Readiness And MCP Clarity

Goal: 讓使用者在遠端寫入前就知道目前是 read-only ready、write-ready，還是 MCP blocked。

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P1-01 | Add single readiness model | Codex | Done | `payload.readiness.mode` appears on doctor/issues/wiki readiness outputs |
| P1-02 | Add MCP preflight fail-fast | Codex | Done | Remote write flows stop early when MCP status is unknown or missing |
| P1-03 | Add label mismatch transparency | Codex | Done | Issue-create result shows requested, applied, and unmatched labels |
| P1-04 | Add P1 regression tests T6-T8 | Codex | Done | Tests pass locally and in CI |

Readiness modes:

```text
READ_ONLY_READY
WRITE_READY
WRITE_BLOCKED_MCP
SYNC_BLOCKED
```

### P1-01 Readiness Single Truth

- [x] Add `payload.readiness.mode`.
- [x] Add `payload.readiness.issue_sync_ready`.
- [x] Add `payload.readiness.remote_write_ready`.
- [x] Add `payload.readiness.blockers`.
- [x] Update chat response to show one summary mode before details.

### P1-02 MCP Preflight

- [x] Run MCP readiness before setup/doctor/issues sync/publish wiki plan.
- [x] Return `problem_class: mcp_not_ready` if required MCP is unknown or missing.
- [x] Allow read-only commands to continue.
- [x] Block remote write suggestions until MCP readiness is resolved.

### P1-03 Label Mismatch Transparency

- [x] Include `requested_labels`.
- [x] Include `applied_labels`.
- [x] Include `unmatched_labels`.
- [x] Include `label_resolution_note`.
- [x] Surface partial label application in `chat_response`.

## Phase P2: Completion Audit And Telemetry

Goal: 讓 MCP handoff 和錯誤恢復都可追蹤、可量化、可審計。

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P2-01 | Add UX telemetry | Codex | Done | `ux-metrics.jsonl` records recovery events without sensitive data |
| P2-02 | Add MCP write completion audit | Codex | Done | Wiki and issue create requests/results are validated and linked to request/idempotency ids |
| P2-03 | Add Redmine to Gitea to Case traceability table | Codex | Done | One command can show mapping chain and latest evidence |
| P2-04 | Add closed issue archive | Codex | Done | Closed issues are removed from active mirrors but retained for audit |
| P2-05 | Add idempotency key for issue creation | Codex | Done | Re-running Redmine sync does not create duplicate Gitea issues |

Telemetry fields:

```json
{
  "timestamp": "...",
  "command": "...",
  "problem_class": "...",
  "auto_correction_applied": false,
  "id_resolution_applied": false,
  "retries_before_success": 0,
  "time_to_recovery_sec": 0
}
```

## Phase P3: Execution Safety And SWQA Enforcement

Goal: 把 side-effect-safe 和 SWQA policy 從文件承諾變成執行層硬約束。

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P3-01 | Add runner timeout | Codex | Done | Case command cannot hang indefinitely |
| P3-02 | Add environment allowlist | Codex | Done | Runner only exposes approved env vars |
| P3-03 | Add command risk classifier | Codex | Done | Unsafe commands are blocked or require explicit review |
| P3-04 | Add stdout/stderr redaction | Codex | Done | Evidence, reports, and wiki never contain raw secrets |
| P3-05 | Add evidence artifact checksums | Codex | Done | Write gate can verify evidence integrity |
| P3-06 | Enforce SWQA policy gates | Codex | Done | PASS requires exact repro, sibling surface, boundary/invalid, and side-effect evidence when explicitly enabled |

## Phase P4: Management Dashboard

Goal: 把 Wiki/status 從測試表格升級成 SW 主管能看的品質投資看板。

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P4-01 | Add coverage matrix | Codex | Done | Wiki shows feature by SWQA dimension coverage |
| P4-02 | Add risk register | Codex | Done | Wiki shows accepted risks and blocked reasons |
| P4-03 | Add trend data | Codex | Done | Wiki shows latest run trend and last green status |
| P4-04 | Add flaky signal | Codex | Done | Repeated inconsistent results are surfaced |
| P4-05 | Add release readiness summary | Codex | Done | Wiki includes objective release-readiness status |

## Acceptance Test Matrix

| Test | Scenario | Command | Expected |
|---|---|---|---|
| T1 | Typo tolerance | `/quality-pilot issues sync --redmine-issuses 145085` | Preserve failure evidence and return `problem_class: typo_argument` with canonical command |
| T2 | Removed command migration | `/quality-pilot fix-issues` | Return `problem_class: removed_command` and replacement |
| T3 | Redmine ID fix mapping | `/quality-pilot issues fix --issue redmine-145085` | Return `resolved_gitea_issue_id` and canonical fix command |
| T4 | ISSUE alias case run | `/quality-pilot cases run ISSUE-99` | Run mapped case or return recovery-first menu |
| T5 | Handoff consistency | `/quality-pilot issues fix --issue 99` | Handoff case ids are runnable, otherwise `handoff_blocked` |
| T6 | Readiness consistency | `/quality-pilot doctor` | Return single `payload.readiness.mode` |
| T7 | MCP missing preflight | `/quality-pilot publish wiki plan` with MCP unknown | Fail fast with repair steps before write path |
| T8 | Label transparency | `/quality-pilot issues sync --redmine-issues 145085` | Result includes requested/applied/unmatched labels |

## Definition Of Done

- [x] P0 tasks are complete.
- [x] T1 through T5 pass.
- [x] `cases run` and `issues fix` no longer recommend commands that cannot execute.
- [x] Readiness output has a single summary mode.
- [x] MCP missing states fail fast before remote write workflows.
- [x] UX metrics instrumentation writes `ux-metrics.jsonl`.

## Operational KPI Follow-Up

The code path is complete. These KPI checks require real product usage data after rollout:

- Collect production UX metrics for at least one week from `.quality-pilot-project/state/ux-metrics.jsonl`.
- Confirm first-try success improves by at least 20%.
- Confirm average retry count drops by at least 30%.
- Confirm error-to-recovery time drops by at least 40%.

## Implementation Evidence

- Implemented in `src/quality_pilot/ux.py`, `cli.py`, `hermes.py`, `fix_issues.py`, `redmine.py`, `issues.py`, `runner.py`, and `wiki.py`.
- Regression coverage: `tests/test_ux_hardening.py` covers T1-T8 plus closed archive, traceability, runner safety, SWQA gate opt-in, and Wiki dashboard sections.
- Verification command: `PYTHONPATH=src python3 -m unittest discover -s tests`.
- Latest local result: 63 tests passed on 2026-06-24.

## Notes For Review

- P0 should stay backward-compatible by only adding fields.
- Hermes skill should use new fields but older payload consumers must continue working.
- Do not add raw tokens, hostnames, lab topology, customer data, or runtime evidence to this repository.
- Keep product-specific runtime state in the host project overlay, not in the AI Quality Pilot tool checkout.
