# AI Quality Pilot UX Hardening Tasks

status: needs_hardening_after_field_audit
implementation_status: tool_checkout_hardening_complete
field_overlay_status: blocked_until_product_overlay_is_regenerated_or_repaired
created: 2026-06-24
updated: 2026-06-26
source: `/root/SPEC_ID quality-pilot-ux-hardening-.txt`
field_audit_source: `/root/repo/test/irctool/.quality-pilot-project`
scope: UX recovery, ID mapping, handoff consistency, readiness clarity, MCP transparency, repo-agnostic runtime onboarding, Redmine case safety, evidence truthfulness, and operational SWQA hardening

## North Star

把 AI Quality Pilot 從「正確但需要懂流程的命令集合」升級成「單議題任務代理」：使用者只給 Redmine ID，也能在 3 次指令內被帶到可修復、可執行、可驗證狀態，不需要人工換算 Redmine/Gitea/Case ID。

## Agent Close Loop Plan

Flowchart-based implementation plan: [Agent Close Loop Improvement Plan](AGENT_CLOSE_LOOP_IMPROVEMENT_PLAN.md).

This companion plan decomposes `Redmine/Gitea issues -> issues sync -> canonical mapping + Gitea issue sync -> case generate or direct issue-driven issues fix -> case run -> report/evidence -> Gitea issue evidence update -> issues fix/PR -> update Gitea Wiki` into explicit agent modules, module contracts, subagent roles, stop/go gates, and an implementation roadmap for low-human-intervention automation.

## Current Finding

這次 field audit 顯示，主要缺口不是 Redmine description 沒抓完整。`/root/repo/test/irctool/.quality-pilot-project/state/redmine-mcp/issues.json` 已是 live full snapshot，含完整 description、custom fields、journals、attachments。

真正問題是 `.quality-pilot-project` state 混用新舊流程，造成 case contract、evidence、Gitea handoff、Wiki report、MCP readiness 彼此不一致。這會讓 Hermes agent 和真人 QA 看到不同真相，進而跑錯 case、引用過期 handoff、或把未驗證狀態誤判為 READY。

## Success Metrics

| Metric | Baseline From Field Audit | Target | Evidence |
|---|---:|---:|---|
| Redmine-linked generic probe cases | `REDMINE-145085.yaml` contains `__quality_pilot_invalid_command__` | 0 | state audit + regression test |
| Redmine-linked developer commands | `go test`/`go run` can appear as Redmine testcase command | 0 | binary-first Redmine case regression |
| Evidence-contract mismatch | REDMINE-145085 PASS evidence command differs from YAML command | 0 | evidence consistency gate |
| Active Gitea issues without runnable case | #100-#103 have no runnable linked case | 0 or explicit blocker | `issues status` traceability |
| Report truth disagreement | latest-run/status says PASS, wiki says NOT_RUN/READY | 0 | Wiki/report reconciliation test |
| MCP readiness ambiguity | missing `hermes-mcp/status.json` with prior write artifacts present | 0 | `doctor` + state audit |
| Subagent claimed but not configured | default endpoint only, no user prompts/model | 0 false claims | `subagent status` |
| Runtime profile questions before repo analysis | user is asked generic setup questions before discovery | 0 | `setup`/`doctor` expose `runtime_profile.repo_analysis` before `hermes_needs_input` |

## Field Audit: `/root/repo/test/irctool/.quality-pilot-project`

Audited state date: 2026-06-24 UTC.

| Finding | Evidence | Risk | Required Repair |
|---|---|---|---|
| `REDMINE-145085.yaml` is still a generic invalid-command probe | `.quality-pilot-project/cases/REDMINE-145085.yaml` runs `go run ./cmd/irctool __quality_pilot_invalid_command__` | Redmine-specific testcase does not reproduce the reported timeout/ResourceInUse bug | Mark as invalid for Redmine-linked case; regenerate as a product-binary contract with explicit environment requirements |
| REDMINE evidence PASS runner does not match case contract | `.quality-pilot-project/evidence/REDMINE-145085/result.json` ran `timeout_validation_precedes_resource_busy` | Reports can cite PASS evidence for a different command than the contract | Add evidence-contract consistency gate using command id/run/contract hash |
| Redmine import and issue write handoff are stale | `redmine-import.json` has no `qa_summary`; `issue-write-request.json` lacks QA Focus/subagent handoff | Gitea issue bodies remain hard for humans and agents to audit | Regenerate sync state with `qa_summary`, QA Focus, `redmine_issue_summary` handoff |
| Gitea #99-#103 map to missing `ISSUE-*` cases | `issues-snapshot.json` maps #99-#103 to `ISSUE-99`..`ISSUE-103`; no `ISSUE-*.yaml` exists | `issues fix` and recovery menus can suggest non-runnable cases | Canonicalize mapping to existing runnable case or explicit blocker |
| `fix-plan.json` points to `ISSUE-99`, but runnable case is `REDMINE-145085` | `fix-plan.json` preflight uses `/quality-pilot cases run ISSUE-99` | Fix workflow can hand off impossible commands | Handoff consistency must resolve aliases before plan creation |
| `hermes-mcp/status.json` is missing | `doctor` reports `hermes_mcp_status_unknown` | Remote write readiness cannot be reproduced or audited | Persist Hermes MCP server availability in configured status JSON |
| Reports disagree on REDMINE-145085 status | `latest-run.json` and `reports/status.md` show PASS; `reports/wiki-status.md` shows NOT_RUN and READY | SW/QA dashboard can overstate release readiness | Make Wiki/status/latest-run share one run truth source |
| Subagent is default-only, not operational | `doctor` shows default Open WebUI endpoint but no model in endpoint/query or profile | Payload may imply subagent-assisted summaries before a model is selected | Gate subagent claims on configured/detected model; task prompts remain optional overrides |

## Historical Baseline P0-P4

The original UX hardening work remains valuable, but it is no longer considered complete after the field audit. P0-P4 should be treated as implemented baseline capabilities that require additional hardening before the system can be called field-ready.

| Phase | Original Goal | Current Status | Field-Audit Adjustment |
|---|---|---|---|
| P0 Recovery POC | Recovery payloads, ID resolver, handoff gate | Baseline implemented | Must also catch stale mapping to missing `ISSUE-*` cases |
| P1 Readiness And MCP Clarity | Single readiness model and MCP preflight | Baseline implemented | Must persist MCP status evidence and explain prior write artifacts |
| P2 Completion Audit And Telemetry | MCP completion audit, traceability, idempotency | Baseline implemented | Must detect stale issue-write requests after applied results |
| P3 Execution Safety And SWQA Enforcement | Timeout, env allowlist, risk classifier, SWQA gates | Baseline implemented | Must block Redmine generic probes and evidence-contract mismatch |
| P4 Management Dashboard | Coverage matrix, risk register, trend, readiness | Baseline implemented | Must not report READY when latest-run/wiki/status disagree |

## Phase P5: Overlay State Consistency

Goal: 建立 `.quality-pilot-project` semantic audit，找出新舊 handoff 混用、缺失 mapping、stale state，避免 agent 根據過期檔案繼續下一步。

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P5-01 | Add `.quality-pilot-project` state audit command/report | Codex | Implemented | `/quality-pilot audit state` returns semantic blockers even when `cases validate` passes YAML syntax |
| P5-02 | Detect stale MCP issue-write requests after applied results exist | Codex | Implemented | Applied result with `needs_mcp_apply` request is flagged as `stale_mcp_issue_write_request` |
| P5-03 | Resolve Redmine/Gitea/Case mapping to one canonical runnable case | Codex | Implemented | Gitea #99 resolves to `REDMINE-145085`; active issues without cases show explicit blockers |
| P5-04 | Add overlay timestamp and source consistency checks | Codex | Implemented as audit inventory | State files from different workflow generations are surfaced in one `state_artifacts` table with schema/status/source/timestamps where available |
| P5-05 | Add recovery-first next actions for semantic audit blockers | Codex | Implemented | Audit output suggests recovery commands and avoids non-runnable `ISSUE-*` ids |

### P5 Implementation Notes

- Audit should read, but not mutate, `cases/`, `evidence/`, `issues-snapshot.json`, `redmine-import.json`, `redmine-gitea-sync-state.json`, `gitea-mcp/issue-write-request.json`, `gitea-mcp/issue-write-result.json`, `latest-run.json`, Wiki reports, and Hermes MCP status.
- Audit output should distinguish syntax validity from semantic validity.
- Stale handoff detection should compare request/result timestamps, operation ids, Redmine ids, idempotency keys, and current issue snapshot.

## Phase P6: Redmine Case Contract Hardening

Goal: Redmine-linked cases must be human-understandable, reproducible, and product-binary-first. The tool should derive the best binary command and test environment preparation list before asking the user; `go test`, `go run`, and internal unit-test names are implementation hints, not QA commands.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P6-01 | Derive product-binary Redmine probes before asking the user | Codex | Implemented in tool checkout, field overlay still stale | Missing user-confirmed product runner now produces an `ai_derived` binary command with `automation_confidence`, `requires_prepared_environment`, `environment_requirements`, and `follow_up_needed`; no generic invalid command is generated |
| P6-02 | Mark stale Redmine contracts with invalid generic probes as audit blockers or regenerate candidates | Codex | Implemented as audit blocker | Existing `REDMINE-145085.yaml` is flagged as `redmine_generic_probe_invalid` until corrected |
| P6-03 | Store QA summary in Redmine case source | Codex | Implemented | Case YAML contains problem, environment, reproduction, expected/actual, evidence, and missing-input notes |
| P6-04 | Keep Redmine issue bodies human-readable | Codex | Implemented | Gitea body does not include raw JSON or tool-internal jargon |
| P6-05 | Add user-answer capture for safe runner details | Codex | Implemented | User-provided command, fixture/env, oracle, and side-effect boundary are stored in auditable fields |
| P6-06 | Reject developer commands in Redmine case contracts | Codex | Implemented in tool checkout | `Safe Probe Command: go test ...` is recorded as a rejected implementation hint and replaced with a product-binary command or binary help/parser fallback |
| P6-07 | Make environment readiness explicit before free-hand execution | Codex | Implemented in tool checkout | Generated case lists binary path, test system/resource, credential/config, evidence, and side-effect preparation requirements under `environment_requirements` |

### P6 Implementation Notes

- `Reproduction Command` from Redmine is not automatically safe. It may require lab hardware, credentials, resources, or side effects, so generated cases must expose those requirements before unattended execution.
- Fields explicitly named like `Safe Probe Command`, `Safe Test Command`, or user-confirmed runner metadata become high-confidence executable `commands[].run` only when they point to a product binary or runner. Developer commands such as `go test`, `go run`, pytest, build scripts, or internal unit-test names are not acceptable Redmine QA commands.
- If exact Redmine reproduction appears read-only, derive the binary command using `QUALITY_PILOT_BINARY` or `./<binary>`. If safety is unclear, derive a binary help/parser fallback and keep exact runtime gaps in `follow_up_needed`.
- Existing Redmine contracts generated before this rule must be audited and either regenerated or blocked.

## Phase P10: Repo-Agnostic Runtime Onboarding

Goal: Clean first-run setup must work for any product repo. The system may ask the user for a free-hand automation profile, but only after analyzing the repo and presenting detected user-facing surfaces.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P10-01 | Add runtime profile skeleton to default config | Codex | Implemented | New `.quality-pilot.yaml` contains blank `runtime.primary_entrypoint`, `binary_env`, `target_host_env`, `fixture_paths`, `credential_envs`, and `side_effect_boundary` |
| P10-02 | Analyze repo before asking runtime questions | Codex | Implemented | Clean `setup`/`doctor` payload includes `runtime_profile.repo_analysis` with detected CLI/API/runtime surfaces and executable candidates before any `hermes_needs_input` |
| P10-03 | Ask only for missing inputs after inference | Codex | Implemented | If an executable is found, runtime status is `ready_inferred`; clarify is used only when runner/binary/API, credential env names, target, or fixtures cannot be inferred |
| P10-04 | Use generic runtime env names | Codex | Implemented | Generated commands use `QUALITY_PILOT_BINARY`, `QUALITY_PILOT_TARGET_HOST`, `QUALITY_PILOT_TEST_USER`, and `QUALITY_PILOT_TEST_PASSWORD` when needed |
| P10-05 | Remove project-specific defaults from clean setup | Codex | Implemented | Default Wiki page is `Quality Pilot Test Status`; no irctool/Siri-specific default remains in generated config |
| P10-06 | Block placeholder executable case generation before runtime confirmation | Codex | Implemented | `cases generate --init/--growing` returns `needs_input`, `generated_count: 0`, and writes no `GEN-*`/`GROW-*` case YAML when runtime profile is missing |
| P10-07 | Enforce product-runtime-only case commands | Codex | Implemented in tool checkout | Generated issue/init/growing commands must use the configured or inferred product binary/API/runner; repo-only probes, `python3 -c`, `compileall`, synthetic invalid commands, `go test`, and `go run` are rejected as testcase commands |

### P10 Implementation Notes

- `irctool` is field evidence, not a product assumption. Runtime discovery must also support Python console scripts, npm bins, Cargo bins, Go `cmd/*`, README command examples, API/service repos, and repo-only projects.
- The first interaction should be: analyze repo, summarize detected surfaces and executable candidates, infer runtime when possible, then ask only for missing secrets/input/environment details.
- Repo-only metadata checks are readiness probes, not testcase contracts. They must not be duplicated across generated cases or counted as product behavior coverage.
- If an issue contains a developer command that does not use the product runtime, keep it as a rejected implementation hint and generate only a product-runtime command or return `needs_input`.
- Raw secrets must never be stored; only credential environment variable names belong in config or generated cases.

## Phase P7: Evidence And Report Truthfulness

Goal: Evidence, status reports, and Wiki dashboard must describe the same current truth.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P7-01 | Add evidence-contract consistency gate | Codex | Implemented as audit gate | PASS evidence is flagged if command id/run/contract hash do not match current case YAML |
| P7-02 | Reconcile `latest-run`, `status.md`, and `wiki-status.md` from one source of truth | Codex | Implemented as audit gate | REDMINE-145085 PASS vs Wiki NOT_RUN is flagged as `report_truth_disagreement` |
| P7-03 | Prevent READY when all listed cases are NOT_RUN | Codex | Implemented | Wiki release readiness returns not-ready when listed cases are all NOT_RUN or latest run is not reflected |
| P7-04 | Add stale report banner | Codex | Implemented | Wiki plan and status report show source run/status plus stale warning when latest state is not trustworthy |
| P7-05 | Record partial probes separately from official case counters | Codex | Implemented | Manually-run or ad hoc evidence is marked `partial_probe` and counted separately from official case counters |

### P7 Implementation Notes

- `cases validate` should remain schema-focused; semantic consistency should be checked by the new state audit.
- Reports should include latest run id, contract hash, command id, and evidence timestamp for every PASS/FAIL/BLOCK row.
- A PASS can be trusted only when evidence maps to the current case contract.

## Phase P8: MCP And Subagent Operationalization

Goal: MCP readiness and subagent usage must be visible, auditable, and not implied before configuration exists.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P8-01 | Persist Hermes MCP readiness status in configured status JSON | Codex | Implemented for env-provided server list | `doctor` persists `QUALITY_PILOT_HERMES_MCP_SERVERS` to `hermes-mcp/status.json` and then reads it as known readiness |
| P8-02 | Require Open WebUI model before claiming subagent-assisted summaries | Codex | Implemented as audit warning | Default-only subagent config is flagged as `subagent_profile_incomplete`; model can come from `endpoint ?model=` or the separate `model` field |
| P8-03 | Add subagent readiness to Redmine sync output | Codex | Baseline implemented | `qa_summary.text_generation` exposes candidate-only handoff and missing model when endpoint/profile cannot resolve one |
| P8-04 | Add MCP result/request pairing audit | Codex | Implemented for issue and Wiki requests | Issue write result paired with stale request is flagged; Wiki request/result schema or stale apply mismatches are flagged |
| P8-05 | Make MCP blocked state actionable | Codex | Implemented | `doctor` and state audit provide the exact status JSON path and expected minimal content |
| P8-06 | Add doctor config repair mode | Codex | Implemented | `/quality-pilot doctor --fix` creates or repairs safe config skeleton, overlay directories, and subagent routing while leaving user-owned model/API settings to the user |

### P8 Implementation Notes

- Default Open WebUI endpoint `https://172.17.20.220/` is only a default profile, not proof that a model has been selected.
- Missing MCP status should block remote writes, but it should not hide local semantic audit findings.

## Phase P9: Active Issue Coverage

Goal: Every active Gitea/Redmine issue should either have a runnable linked case, an explicit needs-input blocker, or a documented reason it is intentionally out of scope.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P9-01 | Surface active Gitea issues without runnable cases as blockers | Codex | Implemented | #100-#103 appear as `active_issue_missing_runnable_case`, not as covered by non-existing `ISSUE-*` cases |
| P9-02 | Generate Redmine QA summaries before testcase generation | Codex | Implemented | QA can review problem, environment, steps, oracle, and evidence before approving a runner |
| P9-03 | Add coverage status per active issue | Codex | Implemented | Each open issue shows `covered`, `needs_input`, `stale_case`, or `no_case` |
| P9-04 | Add repair menu for missing active issue cases | Codex | Implemented | Next action points to Redmine case generation or review instead of missing `ISSUE-*` commands |
| P9-05 | Do not count linked issue as ready until runnable case exists | Codex | Implemented | Dashboard separates issue-created from testcase-ready through `coverage_status` and audit blockers |

## Updated Acceptance Test Matrix

| Test | Scenario | Command / Fixture | Expected |
|---|---|---|---|
| T9 | Semantic audit catches stale overlay | Audited irctool overlay fixture | Implemented in `tests/test_state_audit.py`; `cases validate` passes but state audit reports blockers |
| T10 | Redmine generic probe is rejected | `REDMINE-145085.yaml` contains `__quality_pilot_invalid_command__` | Implemented; audit flags `redmine_generic_probe_invalid` |
| T11 | Evidence-contract mismatch is rejected | Evidence command `timeout_validation_precedes_resource_busy` while YAML command is `safe_probe` | Implemented; audit flags `evidence_contract_mismatch` |
| T12 | Gitea #99 maps to existing Redmine case | `issues status` on irctool overlay fixture | Implemented; canonical case is `REDMINE-145085`, not missing `ISSUE-99` |
| T13 | Active issues without cases become blockers | Gitea #100-#103 with no `ISSUE-*.yaml` | Implemented; status/audit show missing runnable case blockers |
| T14 | Wiki readiness avoids false READY | latest-run PASS but Wiki has all NOT_RUN | Implemented; Wiki body no longer reports READY when execution is absent/stale |
| T15 | Missing MCP status remains visible | No `hermes-mcp/status.json` | Implemented; doctor exposes MCP missing and state audit blockers together |
| T16 | Redmine sync output includes QA handoff | Fresh Redmine sync | Covered by existing lifecycle tests; payload/body include `qa_summary`, `## QA Focus`, and `redmine_issue_summary` handoff |
| T17 | Subagent claims are gated | Default endpoint only, no model | Implemented as audit warning; output remains candidate-only unless configured |
| T18 | Hermes exposes state audit command | Hermes manifest/help/dispatch | Implemented in `tests/test_hermes.py`; `/quality-pilot audit state` is public and root-aware |
| T19 | MCP status can be persisted from Hermes env | `QUALITY_PILOT_HERMES_MCP_SERVERS=gitea,redmine` | Implemented in `tests/test_state_audit.py`; `doctor` writes and then reads `hermes-mcp/status.json` |
| T20 | Incomplete Redmine safe runner becomes follow-up, not first-turn blocker | Redmine issue has only `Safe Probe Command` | Implemented in `tests/test_lifecycle.py`; generation writes the linked case and records missing fixture/env, oracle, and side-effect boundary in `follow_up_needed` |
| T21 | Confirmed Redmine safe runner is auditable | Redmine issue has safe command plus fixture/env, oracle, side-effect boundary fields | Implemented; case YAML stores `source.safe_runner` and `quality_pilot.safe_runner` |
| T22 | Partial probes do not satisfy official counters | One official case plus one `partial_probe: true` case | Implemented; latest-run/report separate `case_counts` from `partial_probe_counts` |
| T23 | Doctor can repair missing config skeleton | Fresh repo with no `.quality-pilot.yaml` | Implemented in `tests/test_cli.py`; `doctor --fix` creates config and overlay directories before running checks |
| T24 | Doctor can repair missing subagent routing safely | Config with `subagents` section removed | Implemented; `doctor --fix` restores Open WebUI routing while model/API settings remain user-owned |

## Definition Of Done For Field-Ready UX

- [x] State audit command/report exists and is documented.
- [x] Redmine-linked generic probes are blocked by state audit; fresh generation derives issue-related safe probes instead of asking first.
- [x] Evidence must match current case command and contract hash before PASS is trusted by state audit.
- [x] Active Gitea issue mapping never points to missing `ISSUE-*` cases without recovery/audit blocker.
- [x] Wiki, status report, and latest run disagreement is detected by state audit.
- [x] MCP readiness status is persisted from Hermes-provided server list, and missing status is clearly actionable.
- [x] Subagent outputs are clearly candidate-only unless an Open WebUI model is configured or detected from the endpoint.
- [x] `doctor --fix` can repair missing config skeleton and subagent routing without inventing user-owned model/API settings.
- [x] Open issues without runnable cases are visible blockers, not hidden by issue creation success.
- [x] Regression tests T9-T24 pass or are covered by existing lifecycle/Hermes tests.

## Operational KPI Follow-Up

These KPI checks require real product usage data after the new audit/hardening phases are implemented:

- Collect production UX metrics for at least one week from `.quality-pilot-project/state/ux-metrics.jsonl`.
- Confirm first-try success improves by at least 20%.
- Confirm average retry count drops by at least 30%.
- Confirm error-to-recovery time drops by at least 40%.
- Confirm field overlay semantic blockers trend toward zero.

## Implementation Evidence To Preserve

- Existing baseline implementation lives in `src/quality_pilot/ux.py`, `cli.py`, `hermes.py`, `fix_issues.py`, `redmine.py`, `issues.py`, `runner.py`, and `wiki.py`.
- Existing regression coverage in `tests/test_ux_hardening.py` remains the P0-P4 baseline.
- Latest full local tool-checkout verification after state audit implementation: `PYTHONPATH=src python3 -m unittest discover -s tests`, 73 tests passed on 2026-06-25.
- Read-only irctool overlay syntax check: `PYTHONPATH=src python3 -m quality_pilot.cli cases validate --root /root/repo/test/irctool --json`, 50 cases valid on 2026-06-25.
- Read-only irctool overlay semantic check: `PYTHONPATH=src python3 -m quality_pilot.cli audit state --root /root/repo/test/irctool --json`, status `blocked` with 11 blockers and 7 warnings on 2026-06-25.
- Field audit reference overlay: `/root/repo/test/irctool/.quality-pilot-project`.

## Notes For Implementers

- The AI Quality Pilot tool checkout now contains the hardening implementation. Do not mutate `/root/repo/test/irctool/.quality-pilot-project` as part of this roadmap; regenerate or repair product overlays through normal user-approved commands.
- Treat the audited irctool state from 2026-06-24 as the primary field evidence for P5-P9.
- Do not hide local semantic blockers behind MCP readiness failures.
- Do not add raw tokens, hostnames beyond already-audited file paths, lab topology secrets, customer data, or runtime credentials to this repository.
- Keep product-specific runtime state in the host project overlay, not in the AI Quality Pilot tool checkout.
