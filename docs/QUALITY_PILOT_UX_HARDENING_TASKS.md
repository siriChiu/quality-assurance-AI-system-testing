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

## Roadmap Delta From Updated Docs

The updated docs make seven roadmap commitments explicit:

1. Generated testcase contracts are product-runtime command contracts, not repo-only readiness checks.
2. `issues sync` can lead to either `case generate` or direct `issues fix --issue <id>` from the same canonical mapping.
3. Hermes may automate local MCP reads, local overlay writes, and verified side-effect-safe case runs after an explicit `/quality-pilot ...` command.
4. Remote writes, Wiki apply, branch push, PR creation, and externally side-effectful tests remain gated.
5. A bug is not PASS just because one command ran. The case/report loop must prove exact reproduction, sibling surface scan, boundary/invalid coverage, side-effect control, and an explicit residual risk list.
6. Public command docs, Hermes install docs, and config docs are now conformance contracts. Dispatcher help, removed-command recovery, default config, and Hermes skill install must stay aligned.
7. Subagent setup is intentionally simple: endpoint plus model and API key env name are enough; task prompts are optional overrides and must not become required user input.

The following phases extend P5-P10 with implementation work that should be tracked after this documentation alignment.

## Current Finding

這次 field audit 顯示，主要缺口不是 Redmine description 沒抓完整。`/root/repo/test/irctool/.quality-pilot-project/state/redmine-mcp/issues.json` 已是 live full snapshot，含完整 description、custom fields、journals、attachments。

真正問題是 `.quality-pilot-project` state 混用新舊流程，造成 case contract、evidence、Gitea handoff、Wiki report、MCP readiness 彼此不一致。這會讓 Hermes agent 和真人 QA 看到不同真相，進而跑錯 case、引用過期 handoff、或把未驗證狀態誤判為 READY。

## Success Metrics

| Metric | Baseline From Field Audit | Target | Evidence |
|---|---:|---:|---|
| Redmine-linked generic command cases | `REDMINE-145085.yaml` contains `__quality_pilot_invalid_command__` | 0 | state audit + regression test |
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
| `REDMINE-145085.yaml` is still a generic invalid-command case | `.quality-pilot-project/cases/REDMINE-145085.yaml` runs `go run ./cmd/irctool __quality_pilot_invalid_command__` | Redmine-specific testcase does not reproduce the reported timeout/ResourceInUse bug | Mark as invalid for Redmine-linked case; regenerate as a product-binary contract with explicit environment requirements |
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
| P3 Execution Safety And SWQA Enforcement | Timeout, env allowlist, risk classifier, SWQA gates | Baseline implemented | Must block Redmine generic commands and evidence-contract mismatch |
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
| P6-01 | Derive product-binary Redmine commands before asking the user | Codex | Implemented in tool checkout, field overlay still stale | Missing user-confirmed product runner now produces an `ai_derived` binary command with `automation_confidence`, `requires_prepared_environment`, `environment_requirements`, and `follow_up_needed`; no generic invalid command is generated |
| P6-02 | Mark stale Redmine contracts with invalid generic commands as audit blockers or regenerate candidates | Codex | Implemented as audit blocker | Existing `REDMINE-145085.yaml` is flagged as `redmine_generic_probe_invalid` until corrected |
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
- Repo-only metadata checks are readiness checks, not testcase contracts. They must not be duplicated across generated cases or counted as product behavior coverage.
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

## Phase P11: Product-Runtime Contract Enforcement

Goal: Make the product-runtime-only rule a single reusable validation policy across init, growing, Redmine, and Gitea issue generation.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P11-01 | Centralize generated command validation | Codex | Implemented | `src/quality_pilot/command_policy.py` rejects repo-only checks, `python3 -c`, `compileall`, synthetic invalid commands, `go test`, and `go run` unless explicitly configured as product runner |
| P11-02 | Persist rejected command hints | Codex | Partially implemented | Redmine/Gitea issue-derived developer commands are stored as rejected hints; broader candidate-source rejected-hint persistence is still pending |
| P11-03 | Add product-runtime command contract audit | Codex | Implemented | `audit state` flags generated cases whose `commands[].run` does not use configured/inferred product runtime as `generated_command_policy_violation` |
| P11-04 | Add generation conformance tests | Codex | Implemented | Init, growing external candidate, Redmine, and Gitea issue fixtures prove fake executable coverage is blocked before YAML or by state audit |

## Phase P12: Canonical Issue Flow And Direct Fix

Goal: Treat Redmine and Gitea issues as first-class inputs, then route from canonical mapping to either testcase generation or direct issue-driven fix.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P12-01 | Persist canonical issue map | Codex | Partially implemented | `issues sync`/`issues status` write `state/traceability-map.json` with Gitea ID, Redmine IDs, canonical runnable case ID, snapshot case ID, latest evidence, coverage status, MCP issue-write result links, and first-pass PR linkage ledger summaries before the next Gitea snapshot refresh; fix-attempt history still needs module-state integration |
| P12-02 | Make Redmine sync idempotent | Codex | Proposed | Redmine sync updates/reuses linked Gitea issue when mapping exists and never duplicates it |
| P12-03 | Route FAIL/BLOCK evidence writeback | Codex | Implemented first pass | `issues report` writes `reports/issues-report.md`, `state/issues-report.json`, and a gated human-readable `gitea.issue.update` evidence payload to the linked Gitea issue for FAIL/BLOCK latest results |
| P12-04 | Support direct issue-driven fix after sync | Codex | Implemented | `issues fix --issue <id>` works for synced feature/direct-fix issues without a runnable case, and marks the flow `issue_driven_development` |
| P12-05 | Gate PR creation on verification | Codex | Implemented | `--push-pr` blocks with `verification_case_required_before_pr` or `verification_evidence_required_before_pr` until linked case evidence or issue-driven acceptance verification exists |

## Phase P13: Automation Boundary And Gate Policy

Goal: Reduce human intervention without allowing uncontrolled remote writes or unsafe test execution.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P13-01 | Add action safety classes | Codex | Proposed | Module outputs classify actions as local read, local write, side-effect-safe run, external-resource run, remote write, branch push, or PR create |
| P13-02 | Automate safe local actions | Codex | Proposed | Hermes can read MCP, write local overlay state, and run verified side-effect-safe cases after explicit `/quality-pilot ...` command |
| P13-03 | Gate external and remote actions | Codex | Proposed | Wiki apply, Gitea issue writes, branch push, PR creation, and externally side-effectful tests stop at deterministic gate/confirmation |
| P13-04 | Persist gate decisions | Codex | Proposed | Evidence, reports, and handoff payloads record the action safety class and gate result |

## Phase P14: Wiki/Gitea Output Ledger

Goal: Split Wiki-only apply from issue/report/PR handoffs while preserving one auditable remote write ledger.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P14-01 | Add remote write ledger | Codex | Partially implemented | `state/gitea-mcp/write-ledger.json` records gated request handoffs by operation id and idempotency key; `issues status` and `publish wiki status` reconcile observed result JSON back into ledger entries |
| P14-02 | Add target-specific write types | Codex | Partially implemented | Ledger records `issue_create`, `issue_evidence_update`, `pr_linkage`, and `wiki_update`; generic non-evidence `issue_update` remains pending |
| P14-03 | Keep Wiki apply Wiki-only | Codex | Implemented for MCP request shape | `publish wiki apply` emits only `gitea.wiki.update_page` with `safety.allowed_targets: [wiki]`; broader negative side-effect fixture remains pending |
| P14-04 | Reconcile write results into traceability | Codex | Partially implemented | Ledger observes successful issue create/Wiki/PR results and remote ids from result JSON; issue create/update results with Redmine IDs are merged into canonical traceability rows, and PR linkage summaries are surfaced per issue even before the next Gitea snapshot |

## Phase P15: SWQA Pattern Expansion And PASS Gate

Goal: Align generated cases, run evidence, issue reports, and Wiki readiness with `docs/SWQA_TEST_DESIGN.md`: every confirmed bug must become a reusable test pattern, not a single shallow command run.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P15-01 | Persist bug pattern cards | Codex | Proposed | Each confirmed Redmine/Gitea bug can emit a structured card with trigger class, exact repro, shared surfaces, equivalence classes, boundary values, regression evidence, and residual risks |
| P15-02 | Add sibling-surface and boundary matrix generation | Codex | Proposed | Case generation proposes sibling commands/features and positive/negative/boundary/invalid variants from repo and issue signals before writing YAML |
| P15-03 | Add PASS/HOLD gate to reports | Codex | Proposed | A bug-linked issue cannot be reported as PASS unless exact repro, deterministic regression, user-facing smoke, sibling surfaces, boundary/invalid checks, and evidence paths are recorded; otherwise status is HOLD or BLOCK |
| P15-04 | Classify untested risk explicitly | Codex | Proposed | Reports and Wiki include what was intentionally not tested and why the remaining risk is acceptable, instead of silently omitting lab-only or side-effectful coverage |
| P15-05 | Add CLI parser matrix support | Codex | Proposed | CLI bug patterns can generate/order-check cases for global flags, command-local flags, same-name flags, positional/inline/short/double-dash variants, and value-shape boundaries |
| P15-06 | Prevent subagents from approving PASS | Codex | Proposed | Subagents may draft pattern cards or wording, but deterministic gates decide PASS/HOLD/FAIL from evidence and case metadata |

### P15 Implementation Notes

- `docs/SWQA_TEST_DESIGN.md` is generic product-agnostic knowledge. Project-specific commands, targets, fixtures, credentials, issue IDs, and evidence remain in the host project overlay.
- Side-effect-safe order should prefer parser/unit/contract checks, help/version paths that exercise parsing, dry-run fixtures, fake targets, and only then real targets after scope and risk are explicit.
- A single exact repro is necessary but not sufficient. The report must show sibling surfaces checked or explicitly held, invalid values checked or explicitly held, and evidence paths for every claim.
- PASS without an explicit risk list is forbidden for bug-linked issues.

## Phase P16: Documentation Contract Conformance

Goal: Treat `README.md`, `docs/COMMANDS.md`, `docs/CONFIGURATION.md`, `docs/HERMES_AGENT_INSTALL.md`, and `docs/SWQA_TEST_DESIGN.md` as executable roadmap contracts, not static prose. When docs say a command/config/skill behavior exists, tests should either prove it or the roadmap should show the remaining gap.

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P16-01 | Add public command contract tests | Codex | Proposed | Dispatcher help, Hermes skill public commands, `README.md`, and `docs/COMMANDS.md` expose the same supported command surface |
| P16-02 | Add removed-command recovery tests | Codex | Proposed | Every removed command listed in `docs/COMMANDS.md` returns `command_removed` with the documented replacement instead of silently redirecting |
| P16-03 | Add config skeleton conformance test | Codex | Proposed | `setup`/`doctor --fix` create MCP-only config with runtime fields, simplified Open WebUI subagent fields, SWQA policy flags, and no Gitea/Redmine token/base URL storage |
| P16-04 | Add config migration/repair checks | Codex | Proposed | Older configs missing `runtime`, `subagents`, `tracker.mcp`, or SWQA policy fields are repaired without overwriting user-owned model/API env settings |
| P16-05 | Add Hermes install contract test | Codex | Proposed | `install-skill`, `skill-status`, and generated `SKILL.md` use the documented runner command, command prefix, and direct dispatcher verification path |
| P16-06 | Add issue snapshot contract fixtures | Codex | Proposed | Gitea snapshot -> `issues sync`; Redmine full manifest -> `issues sync --redmine-issues` and `cases generate --redmine-issues`; legacy/trimmed Redmine snapshots are rejected |
| P16-07 | Add simplified subagent config tests | Codex | Proposed | Endpoint query model `?model=...` and separate `model` both resolve; `api_key_env` rejects raw secret-like values; blank `task_prompts` never blocks readiness |

### P16 Implementation Notes

- Documentation drift should fail tests before users discover mismatched Hermes behavior in the field.
- Config conformance must remain repo-agnostic: no product name, issue ID, host, raw token, or lab secret should be introduced by default setup.
- `doctor --fix` may repair structure and safe defaults, but user-owned model, API key env, lab target, fixture, credential env names, and side-effect boundary stay explicit.

## Phase P17: Anti-Overfit Case Generation Architecture

Goal: Prevent field fixes from becoming product-specific core behavior. `irctool` may remain a regression fixture and field evidence, but product semantics must live in project overlay state, runtime profile, candidate evidence, or explicit local rules, not in reusable generator code.

### Flow Gaps Causing The Follow-On Problems

| Gap | Current Symptom | Downstream Problem | Required Flow Change |
|---|---|---|---|
| Final YAML is written too early | README/Redmine heuristics can become runnable cases in one pass | A weak guess becomes official coverage and later reports/Wiki trust it | Add `case-candidates.json`; write YAML only after candidate review/gate |
| Generator mixes too many responsibilities | Repo scan, issue parsing, command inference, fixture env mapping, safety classification, oracle inference, and YAML writing all live in one flow | Fixing one repo adds product-shaped branches to core | Split into RepoAnalyzer, IssueIntake, CaseIntentPlanner, CommandCandidateBuilder, EnvironmentResolver, OracleBuilder, SafetyClassifier, ContractReviewer, ContractWriter |
| Help fallback is used to fill requested count | Meaningful issue/README commands are replaced by repeated `--help` when candidates are filtered/deduped | Test suites look runnable but do not test user behavior | Treat help/version as readiness/surface probes unless explicitly selected as the testcase objective |
| Product-specific fixture names leak into core | `--login` previously mapped to `QUALITY_PILOT_LOGIN_FILE` | New products need new special env names and rules | Use generic `QUALITY_PILOT_FIXTURE_<FLAG>` env names derived from path-like config/profile/fixture flags |
| Static safety heuristics are treated as approval | Read-only/mutating word lists decide whether a command can run | Unknown product semantics can be falsely marked safe | Output `read_only`, `mutating`, `credentialed`, `target_required`, or `unknown` with confidence; unknown stops before YAML |
| Batch generation blends unrelated intents | One pass tries to generate all testcase commands | Subagent/prompt context drifts and commands become repetitive | Generate and review one testcase candidate at a time, carrying only that candidate's evidence |
| Syntax validation and semantic review are not cleanly separated | `cases validate` can be pulled toward semantic findings | Users cannot tell schema validity from QA truth | Keep schema validation separate from `cases review`/`audit state`, or rename semantic mode as lint/review |

### P17 Tasks

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P17-01 | Replace product-specific fixture env mapping with generic fixture-path env names | Codex | Implemented first pass | Generated commands use `QUALITY_PILOT_FIXTURE_<FLAG>` such as `QUALITY_PILOT_FIXTURE_LOGIN` or `QUALITY_PILOT_FIXTURE_PROFILE`; core no longer emits `QUALITY_PILOT_LOGIN_FILE` |
| P17-02 | Add anti-overfit fixture tests | Codex | Implemented first pass | Lifecycle tests cover non-login config/profile flags and assert old product-specific fixture env names do not return |
| P17-03 | Add `case-candidates.json` stage before YAML writes | Codex | Proposed | Init/growing/Redmine/Gitea generation can run `--plan-only`, persist candidates, and write no case YAML |
| P17-04 | Add per-candidate reviewer gate | Codex | Proposed | Each candidate records source evidence, runtime confidence, oracle confidence, fixture confidence, side-effect confidence, and reviewer status before contract writing |
| P17-05 | Demote generic help/version fallback to readiness coverage | Codex | Proposed | Help/version commands are not used to fill issue-specific testcase count unless the case objective is CLI discovery/help correctness |
| P17-06 | Make safety classification tri-state-plus | Codex | Proposed | Command safety is `read_only`, `mutating`, `credentialed`, `target_required`, or `unknown`; `unknown` yields HOLD/needs_input and writes no runnable YAML |
| P17-07 | Add core token guard for product names | Codex | Proposed | Tests fail when reusable `src/quality_pilot` code introduces field-only terms such as product names, lab IDs, issue IDs, or old product-specific env names |
| P17-08 | Split semantic review from schema validation | Codex | Proposed | `cases validate` reports contract/schema validity; `cases review` or `audit state` owns oracle/safety/evidence semantic blockers |

### P17 Implementation Notes

- Product-specific examples are allowed in fixtures, docs, and audited overlay evidence. They are not allowed as reusable generator assumptions.
- Subagents should receive one candidate at a time and return candidate JSON only. They may suggest commands, oracle text, and risk notes, but deterministic gates decide whether YAML can be written.
- The generator should prefer fewer high-quality approved cases over filling a requested count with repeated help/readiness probes.
- A generated command must be traceable to repo analysis, issue text, runtime profile, project-local rules, or explicit user confirmation.

## Phase P18: Setup-Time Automation Profile

Goal: Reduce downstream testcase guessing by creating a durable, repo-agnostic automation profile candidate during `setup` and `doctor`. The tool must analyze repo/config first, persist what it can infer, and ask only for missing external facts such as credential env names, target resources, fixture paths, and side-effect boundaries.

### P18 Tasks

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P18-01 | Persist `state/automation-profile.candidate.json` from setup/doctor | Codex | Implemented first pass | `setup` and `doctor` write a JSON candidate profile with runtime, command candidates, fixtures, credentials, targets, missing facts, and questions |
| P18-02 | Classify repo-derived command candidates before case generation | Codex | Implemented first pass | Commands are classified as `readiness`, `read_only`, `credentialed`, `target_required`, `mutating`, or `unknown`; classification does not create runnable YAML by itself |
| P18-03 | Use generic env names only | Codex | Implemented first pass | Fixture envs are derived as `QUALITY_PILOT_FIXTURE_<FLAG>` and credentials are represented as env names/placeholders only; no raw secret is stored |
| P18-04 | Surface profile status in doctor | Codex | Implemented first pass | `doctor` includes an `automation.profile` check and exposes the candidate path without blocking setup completion |
| P18-05 | Feed profile into one-candidate-at-a-time generation | Codex | Proposed | `case-candidates.json` consumes the automation profile, generates one candidate at a time, and asks only for facts still missing after repo/profile analysis |

### P18 Implementation Notes

- The automation profile is candidate context, not evidence and not PASS coverage.
- Missing target/credential/fixture facts should produce precise bullet-listed questions, not a broad request for a full environment form.
- The profile must remain product-agnostic; field repos can influence local overlay state, not reusable core assumptions.

## Phase P19: Sensor-Driven Close-Loop Heartbeat

Goal: Turn close-loop from a one-shot runner into a growth loop. Heartbeat must run sensors that manufacture new workflow input, create new case candidates/contracts when justified, execute only new or explicitly requested work, and report `idle` when nothing changed.

### P19 Tasks

| ID | Task | Owner | Status | Acceptance |
|---|---|---|---|---|
| P19-01 | Add `/quality-pilot close-loop heartbeat` | Codex | Implemented | Command runs one heartbeat tick, defaults to 12-hour scheduling metadata, uses up to 20 growth cases, and has no `--iterations` option |
| P19-02 | Add growing-case sensor | Codex | Implemented first pass | Heartbeat calls growing generation first and executes only newly generated cases when available |
| P19-03 | Persist heartbeat state and history | Codex | Implemented first pass | Writes `state/close-loop/heartbeat-latest.json` and `state/close-loop/heartbeat-history.jsonl` |
| P19-04 | Avoid rerunning old work by default | Codex | Implemented first pass | If sensors produce no new case and no explicit scope is requested, heartbeat returns `idle` |
| P19-05 | Add broader sensors | Codex | Implemented first pass | Growth context now includes Gitea issue snapshots, linked PR references, recent git commits, repo code roots, README surfaces, existing cases, latest run state, and bounded monkey CLI help sweeps |
| P19-06 | Increase growth aggressiveness without fake coverage | Codex | Implemented second pass | `cases generate --growing` defaults to 20 cases, expands candidates through an SWQA operation matrix, treats duplicate existing commands as already-covered signals instead of consuming new-case budget, and still rejects repo-only/developer/synthetic commands |
| P19-07 | Add resumable module session | Codex | Proposed | Heartbeat records module-level handoff state so failures resume at the blocked module instead of restarting the whole loop |

### P19 Implementation Notes

- `run-once` remains the deterministic executor for a selected scope.
- `heartbeat` is the evolving orchestrator entrypoint: Observe -> Evolve -> Execute -> Report -> Publish.
- Growing case generation now writes operation intent into each growth contract under `quality_pilot.swqa_operation`, so review/reporting can distinguish surface probes, invalid-option rejection, boundary invalid-value checks, repeatability, concurrency, timeout baseline, and bounded monkey sweeps.
- Remote writes, PR creation, and external-resource tests still stop at gates.

## Updated Acceptance Test Matrix

| Test | Scenario | Command / Fixture | Expected |
|---|---|---|---|
| T9 | Semantic audit catches stale overlay | Audited irctool overlay fixture | Implemented in `tests/test_state_audit.py`; `cases validate` passes but state audit reports blockers |
| T10 | Redmine generic command is rejected | `REDMINE-145085.yaml` contains `__quality_pilot_invalid_command__` | Implemented; audit flags `redmine_generic_probe_invalid` |
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
| T25 | Product-runtime validator rejects fake coverage | Generated case with repo-only check/developer command | Implemented in `tests/test_lifecycle.py` and `tests/test_state_audit.py`; validator rejects command unless explicitly configured as product runner |
| T26 | Direct fix after sync is traceable | Synced Gitea or Redmine issue without runnable case | Implemented in `tests/test_lifecycle.py`; `issues fix --issue` enters `issue_driven_development` and `--push-pr` blocks until verification |
| T27 | FAIL/BLOCK evidence writes back to linked issue | Failing linked case with canonical Gitea issue mapping | Implemented in `tests/test_lifecycle.py`; `issues report` emits gated `gitea.issue.update` targeting the linked issue and records an `issue_evidence_update` ledger entry |
| T28 | PR linkage is traceable before remote PR creation | `issues fix --issue <id> --push-pr` on MCP backend after linked evidence exists | Implemented in `tests/test_lifecycle.py`; PR creation is blocked for MCP backend, but the PR linkage request, ledger entry, Gitea issue id, case id, and evidence paths are persisted and exposed through `issues status` |
| T29 | Hermes action safety boundary is enforced | Explicit `/quality-pilot ...` command with local and remote actions | Proposed; local reads/writes and verified safe runs proceed, remote writes/PR/external tests stop at gate |
| T30 | Wiki apply remains Wiki-only | Wiki apply with issue/PR side effect attempt | Partially implemented; lifecycle test verifies MCP request is `gitea.wiki.update_page` with `allowed_targets: [wiki]`, and Wiki result status reconciles into ledger; explicit side-effect attempt fixture is still pending |
| T31 | Bug pattern card is required for confirmed bugs | Redmine/Gitea bug with exact repro and shared parser/validator path | Proposed; card records trigger class, exact repro, sibling surfaces, boundary/invalid matrix, evidence, and residual risks |
| T32 | PASS gate rejects shallow single-command evidence | One bug-linked case passes exact repro but lacks sibling/boundary/invalid/risk evidence | Proposed; report returns HOLD/BLOCK instead of PASS/READY |
| T33 | CLI parser matrix expands issue-specific coverage | CLI flag-order bug fixture | Proposed; generated candidates include global/local/same-name flag, position, inline/short/double-dash, and value-shape variants through product runtime |
| T34 | Public command docs match dispatcher/Hermes | `README.md`, `docs/COMMANDS.md`, generated `SKILL.md`, `/quality-pilot help` | Proposed; command lists and removed-command replacements cannot drift |
| T35 | Config docs match setup/doctor output | Fresh repo plus old partial config fixtures | Proposed; generated/repaired config includes documented runtime, MCP, subagent, and SWQA policy fields without secrets |
| T36 | Hermes install path remains verifiable | `install-skill`, `skill-status`, direct wrapper execution | Proposed; documented install commands produce a valid `~/.hermes/skills/quality-pilot/SKILL.md` and working dispatcher |
| T37 | Snapshot contracts match docs | Full/legacy/trimmed Redmine manifests and Gitea issues snapshot fixtures | Proposed; full manifests pass, stale or trimmed manifests fail before sync/generation |
| T38 | Simplified Open WebUI config works | Endpoint query model, separate model, blank task prompts, raw api key mistake | Proposed; model resolves, task prompts stay optional, raw secret-like `api_key_env` is repairable/actionable |
| T39 | Heartbeat grows new work before execution | Synced issue state plus no growth case yet | Implemented in `tests/test_lifecycle.py`; heartbeat runs growing sensor, executes only newly generated cases, and persists heartbeat latest/history state |

## Definition Of Done For Field-Ready UX

- [x] State audit command/report exists and is documented.
- [x] Redmine-linked generic commands are blocked by state audit; fresh generation derives issue-related product-runtime commands instead of asking first.
- [x] Evidence must match current case command and contract hash before PASS is trusted by state audit.
- [x] Active Gitea issue mapping never points to missing `ISSUE-*` cases without recovery/audit blocker.
- [x] Wiki, status report, and latest run disagreement is detected by state audit.
- [x] MCP readiness status is persisted from Hermes-provided server list, and missing status is clearly actionable.
- [x] Subagent outputs are clearly candidate-only unless an Open WebUI model is configured or detected from the endpoint.
- [x] `doctor --fix` can repair missing config skeleton and subagent routing without inventing user-owned model/API settings.
- [x] Open issues without runnable cases are visible blockers, not hidden by issue creation success.
- [x] Regression tests T9-T24 pass or are covered by existing lifecycle/Hermes tests.
- [x] Product-runtime contract validator is centralized across all generation paths.
- [x] Direct issue-driven fix after sync is covered by canonical mapping and PR gate tests.
- [x] Heartbeat first pass grows new work before execution and avoids rerunning old cases by default.
- [ ] Hermes action safety classes are persisted and enforced across module outputs.
- [ ] Wiki-only apply and Gitea issue/report/PR handoffs share one auditable write ledger. Issue create, issue evidence update, PR linkage, and Wiki update are covered first-pass; generic issue update and richer apply reconciliation remain pending.
- [ ] SWQA PASS/HOLD gate requires exact repro, sibling-surface scan, boundary/invalid coverage, side-effect control, evidence paths, and explicit residual risk.
- [ ] Public docs, Hermes skill install, dispatcher help, removed command recovery, and generated config stay under regression tests.

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
- Latest targeted tool-checkout verification after P12/P14 issue evidence writeback: `PYTHONPATH=src python3 -m unittest tests.test_lifecycle.LifecycleTest.test_issues_report_writes_gated_evidence_update_for_linked_failed_case`, 1 test passed on 2026-06-26.
- Latest full local tool-checkout verification after P14 result-to-traceability reconciliation: `PYTHONPATH=src python3 -m unittest discover -s tests`, 89 tests passed on 2026-06-26.
- Read-only irctool overlay syntax check: `PYTHONPATH=src python3 -m quality_pilot.cli cases validate --root /root/repo/test/irctool --json`, 50 cases valid on 2026-06-25.
- Read-only irctool overlay semantic check: `PYTHONPATH=src python3 -m quality_pilot.cli audit state --root /root/repo/test/irctool --json`, status `blocked` with 11 blockers and 7 warnings on 2026-06-25.
- Field audit reference overlay: `/root/repo/test/irctool/.quality-pilot-project`.

## Notes For Implementers

- The AI Quality Pilot tool checkout now contains the hardening implementation. Do not mutate `/root/repo/test/irctool/.quality-pilot-project` as part of this roadmap; regenerate or repair product overlays through normal user-approved commands.
- Treat the audited irctool state from 2026-06-24 as the primary field evidence for P5-P9.
- Do not hide local semantic blockers behind MCP readiness failures.
- Do not add raw tokens, hostnames beyond already-audited file paths, lab topology secrets, customer data, or runtime credentials to this repository.
- Keep product-specific runtime state in the host project overlay, not in the AI Quality Pilot tool checkout.
