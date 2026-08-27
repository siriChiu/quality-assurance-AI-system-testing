# Architecture

AI Quality Pilot uses a deterministic-first close-loop pipeline. Agents and
LLMs may summarize evidence or draft candidate text, but they do not decide
whether to skip required steps or write to trackers. See
[`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md) before treating a target design
as implemented behavior.

```text
+--------------------+
| quality-pilot CLI  |
+---------+----------+
          |
          v
+--------------------+      +--------------------+
| project overlay    | ---> | pipeline engine    |
+--------------------+      +--------------------+
          |                           |
          v                           v
+--------------------+      +--------------------+
| runner registry    |      | result normalizer  |
+--------------------+      +--------------------+
          |                           |
          v                           v
+--------------------+      +--------------------+
| evidence store     |      | write gate         |
+--------------------+      +--------------------+
                                      |
                                      v
                             +--------------------+
                             | MCP handoff files  |
                             +--------------------+
```

## Fixed pipeline order

```yaml
pipeline:
  - config_validate
  - health_checks
  - tracker_pull_open_items
  - select_scope
  - run_cases
  - normalize_results
  - deduplicate_tracker_actions
  - write_gate
  - tracker_write_when_allowed
  - render_reports
  - persist_state
```

V1 implements the deterministic order with Hermes MCP handoff files for remote
state. The engine validates, mirrors, plans, gates, and persists request/result
JSON; it does not store tracker tokens or write directly through Gitea/Redmine
HTTP. Hermes MCP applies only the gated request payloads for linked or
failure-derived Gitea issue create/update and Wiki updates.

## Four-axis truth model

The first supported truth-model slice keeps four different questions separate:

```yaml
truth:
  workflow_status: where the lifecycle is now
  test_outcome: what the executed assertions observed
  gate_status: whether the next gated action may proceed
  health_status: whether relevant component or integration health was independently evaluated
```

A command-level `test_outcome: PASS` must not silently become workflow success,
write permission, or release health. For example, an assertion can pass while
`workflow_status` is `BLOCKED` because required risk coverage is missing, or while
`gate_status` blocks a remote write. Older overlays can require regeneration or
repair before all four axes are present.

The current close-loop runner marks `health_status: NOT_EVALUATED` because it
does not execute the `doctor` health checks. QA and write-gate outcomes never
stand in for independent component health.

## Graph Engineering: two complementary graphs

This project follows the reference definition: Graph Engineering designs both the
structures an agent remembers and the structures an agent works through.

```text
Knowledge Graph (memory)
  scope -> representation -> ontology -> entities/relations/events
          -> quality gate -> reversible fusion -> evaluation -> read-only serving
                                      ^
                                      |
Task Graph (work)
  Context -> Contract -> DAG -> source adapter -> parallel extraction -> independent verifier
          -> owned merge -> human gate -> checkpoint -> targeted repair
```

### Knowledge Graph runtime

`src/quality_pilot/graph_engineering/` implements a local first slice:

- SQLite is the canonical store; JSON is the portable snapshot/export.
- `GraphEntity`, `GraphRelation`, and `GraphEvent` require source, timestamp,
  confidence, and evidence provenance.
- Ontology validation enforces typed relation domain/range and event schemas.
- The integration-first adapter projects existing case contracts, canonical run/evidence
  records, and pinned PR review reports into graph candidates; external deterministic or
  LLM-generated candidate JSON/YAML remains an explicitly owned adapter path. The
  deterministic validator owns graph writes.
- Quality gates distinguish structural validity from adjudicated precision/recall.
- Fusion uses deterministic blocking, records reversible merge ledgers, and requires
  explicit confirmation before applying merges.
- Serving is read-only and provenance-preserving; graph counts never become QA truth.

The public modular workflow is:

```text
/quality-pilot graph scope --question <question>
/quality-pilot graph representation
/quality-pilot graph ontology
/quality-pilot graph run --from-qa --question <question>
/quality-pilot graph quality-gate
/quality-pilot graph fuse
/quality-pilot graph evaluate
/quality-pilot graph serve
```

`graph run --from-qa` consumes the existing Quality Pilot case/run/evidence/review
artifacts; it does not create a competing QA authority. A supplied PR review report is
validated for schema, report hash, pinned head/base, PR repo/number/ref identity, open/merged state,
optional `updated_at`, and evidence paths before strict projection.
`graph extract --input` remains available for a separately owned candidate adapter.

### Task Graph orchestration

The deterministic core owns `ContextPacket`, node contracts, real data dependencies,
single-writer ownership, topological scheduling, validator results, checkpoints, stop
rules, and repair invalidation. `compile_graph_engineering_task_graph()` compiles the
nine-stage KG workflow into a Task Graph: entity extraction fans out, relation/event
extraction consume recognized entities, an independent verifier checks the graph, fusion
is behind a human gate, and evaluation/serving remain downstream.

The default `close-loop run-once` orchestrates QA cases through Task Graph. `graph run --from-qa`
projects those existing case/run/evidence/review artifacts into the Knowledge Graph workflow;
it uses the same executor and persists
`state/graph/task-graph-latest.json`. A node `PASS` is local evidence only. It cannot become
product `PASS`, `READY`, `APPROVED`, or `MERGE_ALLOWED` without separate deterministic truth
and write-gate policies.

The external MIT-licensed `codejunkie99/graph-engineering` teaching material is bundled into
the installed Quality Pilot skill under `references/graph-engineering/` (source snapshot
`cfacb56a05a31ba69bf84d0b8b00f5ce463127ef`). It provides tutor mode and modular workflow
references; the dispatcher remains the authority for safe, provenance-backed local artifacts.

## Development-only subagent assistance

The local subagent council is an out-of-band development aid for objective code,
architecture, and state analysis. It is not a runtime Task Graph node, product
oracle, mapping authority, QA source, or release feature. Provider suggestions
remain candidate text; the lead agent must independently validate and manually
apply any resulting code or policy change. No subagent output may write
contracts, runs, evidence, graph state, or remote requests.

## Pinned Gitea PR review

`review pr --repo <owner/repo> --pr-number <number>` consumes a Hermes Gitea
read snapshot, pins the reported head SHA, creates a detached local worktree,
selects available repository regression tests, and writes a redacted report.
When the checkout contains pytest-based tests it prefers the project-owned Python
environment and runs `python -m pytest tests -q`; otherwise it runs the bounded
unittest discovery command. Missing test dependencies are `BLOCK`, not a product
FAIL. In comprehensive mode the review also creates a PR-scoped temporary case
overlay, calls case generation and case execution, and reports black-box, white-box,
functional, boundary, stress, and documentation dimensions separately. It also
invokes the product-test adapter: a user-owned build recipe must produce a real
artifact in a disposable writable copy of the pinned worktree, then at least one
semantic product operation must pass. README commands are candidate input only and
require explicit allowlisting; exit-only probes remain `HOLD`. When a web UI contract
is enabled, the browser adapter uses real Playwright interaction and positive UI
assertions; missing browser prerequisites are `BLOCK` and there is no curl/API
fallback. Missing product-specific adapters remain `HOLD`; they cannot be counted as
PASS. Review test selection now also emits a changed-file-driven
`diff_targeted_oracle` plan when changed product files map to existing product tests;
the plan is `PASS` only after that exact targeted command executes successfully.
Missing mappings remain `HOLD`. Review test and product execution use allowlisted
argv commands without `shell=True`; their payloads separate snapshot path,
review-report paths, product/build/browser evidence, and report hash. The report
also emits actionable remediation recommendations for each BLOCK/HOLD dimension.
Without `--confirm` it only returns the conclusion, recommendations, and payload
preview; confirmation creates a local gated Gitea **COMMENT** request even when
QA evidence is incomplete. This is an advisory code review, not an approval.
Hermes performs the actual MCP call, then `/quality-pilot review apply`
validates the result against repo/PR/head/report hash, advisory COMMENT state,
allowed target, and a local deduplication ledger. The user owns the final
COMMENT/REQUEST_CHANGES/APPROVED decision. Permission reconciliation, live
Gitea comment lookup, and automatic new-head invalidation remain project-owned
adapter work.

## Bounded PTY/TUI environment boundary

Terminal products need an adapter beyond `subprocess.run`. The current
`environment tui-probe` boundary uses a pseudo-terminal, fixed terminal size,
allowlisted keys, bounded duration, redacted transcript, and explicit screen
markers. It reports `BLOCK` for an unconfirmed environment or known hardware/
runtime preflight failure, `HOLD` when no oracle marker is supplied or the
marker is missing, and only reports PASS when every explicit marker is
observed. Process exit alone cannot produce FAIL/PASS; it remains diagnostic.
Remote mode uses argv-only SSH and never stores the target value.
This is a deterministic boundary foundation, not a complete UI test oracle;
project-owned adapters must add screen state, widget/keypress semantics, and
hardware/BMC preflight.

## Environment-confirmed execution

Repository analysis can infer a likely executable, but it cannot infer whether
that executable is allowed to touch the checkout, an isolated lab, or a remote
target. Production flows therefore use a mandatory `grill-me` interview before
generation or prepared-environment execution. The answers are persisted as a
redacted `runtime` environment profile:

```text
grill-me -> setup -> reconcile repo analysis -> environment configure
         -> environment status -> doctor -> cases generate --init
         -> cases validate -> cases run
```

The profile records `execution_mode` (`local` or `remote`), confirmation,
entrypoint, fixture paths, credential env names, target env name, and the
side-effect boundary. It never records raw target, credential, or secret
values. A case with `quality_pilot.requires_prepared_environment: true` (which
includes recognized README CLI operations) is preflighted against this profile.
Missing mode/confirmation/fixture/credential/remote target or an unavailable
executable produces `BLOCK` with a reason and evidence; it cannot become
`PASS` merely because a shell process returned a superficially acceptable
status. A run containing only partial probes is `HOLD`, not official `PASS`.

`probe_outcome` is an auxiliary result for partial probes. When a run contains
only `oracle_partial`/partial-probe cases, their observations are preserved in
`probe_outcome`, while the official `test_outcome` is `HOLD`; shallow probes
cannot manufacture an official PASS.

## SWQA policy pack

Case generation and close-loop guidance share this lifecycle vocabulary:

```text
Observe -> Normalize -> Execute -> Triage -> Publish -> Evolve -> Prune
```

The policy pack is intentionally generic. The current supported slice can label
and stratify work across dimensions such as exact reproduction, positive,
negative, boundary, invalid input, sibling surface, side-effect-safe, and
stress/timeout-risk. Labels and generated variants do not prove deep coverage.
Full white-box/black-box analysis, mutation testing, fuzzing, security testing,
UI testing, and load/soak strategies remain planned. Project-specific
assumptions such as lab topology, hardware fixture paths, service baselines, or
VM images belong in the host project's `.quality-pilot-project/rules/` or case
contracts, not in AI Quality Pilot core.

## Init and growing case generation

`cases generate` requires `--init` or `--growing`; a bare command returns `explicit_generation_mode_required`.

`cases generate --init` builds `.quality-pilot-project/state/init-context.json`
from README presence, code inventory, package metadata, existing cases, runners,
and rules. The first supported stratified selector distributes the available
case budget across operation/dimension strata instead of filling it from one
repeated command surface. It writes `source.type: init` executable contracts
only when a product-runtime `commands[].run` can be established. A stratum label
is a selection intent, not proof of requirements, code-path, or residual-risk
coverage.

`cases generate --growing` builds `.quality-pilot-project/state/growth-context.json`
from repo metadata, code inventory, Gitea issue snapshots, linked PR references,
recent git commit history, latest run, publish plan, existing cases, runners, and
rules. The default target is up to 20 new growth cases, with a larger candidate
pool for dedupe and operation selection. Duplicate commands do not consume the
new-case budget. Twenty generated files are not a quality target: meaningful
oracles, risk relevance, and explicit coverage gaps matter more than filling the
budget.

Growth candidates are expanded through the first supported SWQA operation
matrix before YAML is written. It includes read-only surface probes,
invalid-option rejection, boundary invalid-value rejection, sibling help sweep,
repeatability loops, concurrency probes, timeout baselines, and bounded monkey
sweep variants. This is operation diversity, not complete white-box or black-box
coverage. The command policy still rejects repo-only probes, developer commands,
raw destructive commands, and placeholders; every generated command must use
the configured or inferred product runtime.

The first monkey-test sensor is bounded and deterministic: `monkey_cli_help_sweep` groups documented CLI help/version surfaces and executes them through the configured product runtime, with safe repeatability/concurrency variants when useful. It does not invent destructive random commands or repo-only probes.

`close-loop heartbeat` composes growing generation with execution. It first runs
sensors that create new workflow input; the first implemented sensor is growing
case generation. If new cases are created, heartbeat executes only those new
cases through the close-loop runner and records
`.quality-pilot-project/state/close-loop/heartbeat-latest.json`. If no new work
is created, it reports `idle` instead of rerunning old work. Heartbeat is one
tick. Its default 12-hour value is metadata; it does not install a timer. Hermes
or an external scheduler must invoke the next tick. See
[`HEARTBEAT_CRON.md`](HEARTBEAT_CRON.md).

`--count <max>` is the explicit generation limit for a smaller batch. `--init`
uses the current autonomous first-slice selector; it is not a complete product
audit. If the runtime profile is missing, case generation
stops with `needs_input`; repo-only metadata checks remain readiness checks and
are not written as placeholder testcase contracts.

Generated case commands must use the configured or inferred product binary/API/runner, or a user-confirmed runner. Repo-only metadata checks, `python3 -c`, `compileall`, synthetic invalid commands, `go test`, and `go run` are rejected as testcase commands unless the user explicitly configured them as the user-facing product runner.

## Issue Sync And Fix Entry

`issues sync` accepts Gitea issue snapshots and Redmine issue IDs. Redmine sync creates local Redmine mirrors, generates QA-focused summaries, and emits gated Gitea issue create/update requests. The canonical issue mapping ties Redmine ID, Gitea issue ID, case ID, evidence path, and PR linkage together.

`issues create-from-failure` has an explicit publication boundary: `--local`
writes a detailed local SWQA failure report and never creates a remote request;
`--remote` writes that same full local report first, then renders one complete,
independently readable report per failed case, redacts credentials and
workstation-specific paths, and emits only a gated Gitea MCP issue-create
handoff. The remote report is deliberately free of internal run identifiers and
automation-specific evidence paths.

The canonical local work-item layout is split by ownership rather than by
report format:

```text
.quality-pilot-project/issues/
├── local/                  # testcase-id work items and full technical reports
│   ├── failure-report.md
│   └── <case-id>.md
├── remote/                 # synced Gitea issue-id mirrors
│   └── <gitea-issue-id>.md
└── <gitea-issue-id>.md     # legacy mirror kept for compatibility
```

`state/issues-snapshot.json` remains the synced remote truth; the two folders
are local, inspectable work queues. The generic `issues fix` entry resolves a
Gitea issue ID to the remote workflow or a testcase ID/name to the local case
workflow. It never requires manually copying a report between folders.

After sync, `issues fix --issue <id>` may start directly for feature/development issues even before a runnable case exists. That mode is marked `issue_driven_development`; PR creation remains blocked until acceptance cases/evidence are available.

Hermes may use a separate growth session to analyze the context, but that session may only produce candidate JSON. AI Quality Pilot validates candidate schema, dedupe fingerprints, secret leakage, internal prompt leakage, dangerous `.qa` runtime paths, and command fields before writing YAML.

Long human-facing text can be delegated to a configured subagent as
candidate-only generation. No private Open WebUI address is a universal product
default; deployments must provide their own endpoint and model. Optional API
credentials are referenced through `api_key_env`, never stored as raw secrets.
Subagents may draft Gitea issue bodies, PR bodies, Wiki summaries, Redmine
summaries, case candidate analysis, and reviewer notes; they must not write
files, create tracker records, update Wiki pages, open PRs, or bypass validation
or write gates.

## Enforced invariants and target policies

```yaml
invariants:
  deterministic_first: true
  write_gate_required: true
  closed_tracker_items_are_not_active: true
  issue_retest_contract_must_match: true
  raw_secrets_in_repo: false
  project_state_inside_tool_repo: false
```

Pattern expansion, sibling-surface analysis, boundary/invalid coverage, and
side-effect control are target policies with a first supported generation slice;
their complete issue-level PASS/HOLD enforcement is still Partial. See the
[capability matrix](CAPABILITY_MATRIX.md) and
[SWQA test-design knowledge](SWQA_TEST_DESIGN.md).
