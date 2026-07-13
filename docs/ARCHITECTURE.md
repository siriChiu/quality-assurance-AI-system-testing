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
HTTP. Hermes MCP applies only the gated request payloads for linked Gitea issue
create/update and Wiki updates.

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
