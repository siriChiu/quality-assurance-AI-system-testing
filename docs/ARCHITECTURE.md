# Architecture

AI Quality Pilot uses a deterministic-first close-loop pipeline. Agents and LLMs may summarize evidence or draft text, but they do not decide whether to skip required steps or write to trackers.

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

## SWQA policy pack

Case generation and close-loop guidance share the built-in policy pack:

```text
Observe -> Normalize -> Execute -> Triage -> Publish -> Evolve -> Prune
```

The policy pack is intentionally generic. It defines stable dimensions such as exact reproduction, positive, negative, boundary, invalid input, sibling surface, side-effect-safe, and stress/timeout-risk coverage. Project-specific assumptions such as lab topology, hardware fixture paths, Redfish baselines, or VM images belong in the host project's `.quality-pilot-project/rules/` or generated case contracts, not in AI Quality Pilot core.

## Init and growing case generation

`cases generate` requires `--init` or `--growing`; a bare command returns `explicit_generation_mode_required`.

`cases generate --init` builds `.quality-pilot-project/state/init-context.json` from README presence, code inventory, package metadata, existing cases, runners, and rules. It writes `source.type: init` executable contracts for functional, positive, negative, boundary, side-effect-safe, and stress/timeout-risk coverage. Every generated contract gets a product-runtime `commands[].run`; lab fixtures are later enhancements, not init blockers.

`cases generate --growing` builds `.quality-pilot-project/state/growth-context.json` from repo metadata, code inventory, Gitea issue snapshots, linked PR references, recent git commit history, latest run, publish plan, existing cases, runners, and rules. It is intentionally aggressive: the default target is 20 new growth cases, and the candidate pool is much larger than the write limit so the generator can dedupe and select useful work instead of repeating old commands. Duplicate existing cases/commands are recorded as existing coverage and do not consume the new-case budget. It then writes `source.type: growth` executable case contracts under `.quality-pilot-project/cases/`.

Growth candidates are expanded through an SWQA operation matrix before YAML is written. The current matrix includes read-only surface probes, invalid-option rejection, boundary invalid-value rejection, sibling help sweep, repeatability loops, concurrency probes, timeout baselines, and bounded monkey sweep variants. The command policy still rejects repo-only probes, developer commands, raw destructive commands, and placeholders; every generated command must use the configured or inferred product runtime.

The first monkey-test sensor is bounded and deterministic: `monkey_cli_help_sweep` groups documented CLI help/version surfaces and executes them through the configured product runtime, with safe repeatability/concurrency variants when useful. It does not invent destructive random commands or repo-only probes.

`close-loop heartbeat` composes growing generation with execution. It first runs sensors that manufacture new workflow input; the first implemented sensor is growing case generation. If new cases are created, heartbeat executes only those new cases through the close-loop runner and records `.quality-pilot-project/state/close-loop/heartbeat-latest.json`. If no new cases are created, it reports `idle` instead of rerunning old work. An empty project with no real issue/code/advisory signal also idles instead of forcing runtime questions. Heartbeat is a single tick; the default scheduling metadata is once every 12 hours, with up to 20 new growth cases per tick. External schedulers or Hermes should trigger the next heartbeat.

`--count <max>` is the explicit generation limit for users who want a smaller batch. `--init` is already autonomous high-standard mode; there is no public `--fast` option. If the runtime profile is missing, case generation stops with `needs_input`; repo-only metadata checks remain readiness checks and are not written as placeholder testcase contracts.

Generated case commands must use the configured or inferred product binary/API/runner, or a user-confirmed runner. Repo-only metadata checks, `python3 -c`, `compileall`, synthetic invalid commands, `go test`, and `go run` are rejected as testcase commands unless the user explicitly configured them as the user-facing product runner.

## Issue Sync And Fix Entry

`issues sync` accepts Gitea issue snapshots and Redmine issue IDs. Redmine sync creates local Redmine mirrors, generates QA-focused summaries, and emits gated Gitea issue create/update requests. The canonical issue mapping ties Redmine ID, Gitea issue ID, case ID, evidence path, and PR linkage together.

After sync, `issues fix --issue <id>` may start directly for feature/development issues even before a runnable case exists. That mode is marked `issue_driven_development`; PR creation remains blocked until acceptance cases/evidence are available.

Hermes may use a separate growth session to analyze the context, but that session may only produce candidate JSON. AI Quality Pilot validates candidate schema, dedupe fingerprints, secret leakage, internal prompt leakage, dangerous `.qa` runtime paths, and command fields before writing YAML.

Long human-facing text can also be delegated to a configured subagent as candidate-only generation. The default profile is Open WebUI at `https://172.17.20.220/`; the user only needs to provide a model through `?model=<name>` in the endpoint or the separate `model` field. Optional API credentials are referenced through `api_key_env`, never stored as raw secrets. Subagents may draft Gitea issue bodies, PR bodies, Wiki summaries, Redmine summaries, case candidate analysis, and reviewer notes; they must not write files, create tracker records, update Wiki pages, open PRs, or bypass validation/write gates.

## Invariants

```yaml
invariants:
  deterministic_first: true
  write_gate_required: true
  closed_tracker_items_are_not_active: true
  issue_retest_contract_must_match: true
  bug_fixes_expand_to_swqa_patterns: true
  sibling_surface_scan_required: true
  boundary_invalid_value_tests_required: true
  side_effect_safe_repro_required: true
  raw_secrets_in_repo: false
  project_state_inside_tool_repo: false
```
