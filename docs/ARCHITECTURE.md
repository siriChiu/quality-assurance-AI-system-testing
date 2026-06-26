# Architecture

AI Quality Pilot uses a deterministic-first close-loop pipeline. Agents and LLMs may summarize evidence or draft text, but they do not decide whether to skip required steps or write to trackers.

```text
+--------------------+
| quality-pilot CLI        |
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
                             | tracker adapters   |
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

V1 implements the full deterministic order. Tracker pull/write steps are
explicit no-op/dry-run stages unless a future adapter is enabled behind the
write gate; they are still emitted in run summaries so automation can verify
that no step was silently skipped.

## SWQA policy pack

Case generation and close-loop guidance share the built-in policy pack:

```text
Observe -> Normalize -> Execute -> Triage -> Publish -> Evolve -> Prune
```

The policy pack is intentionally generic. It defines stable dimensions such as exact reproduction, positive, negative, boundary, invalid input, sibling surface, side-effect-safe, and stress/timeout-risk coverage. Project-specific assumptions such as lab topology, hardware fixture paths, Redfish baselines, or VM images belong in the host project's `.quality-pilot-project/rules/` or generated case contracts, not in AI Quality Pilot core.

## Init and growing case generation

`cases generate` requires `--init` or `--growing`; a bare command returns `explicit_generation_mode_required`.

`cases generate --init` builds `.quality-pilot-project/state/init-context.json` from README presence, code inventory, package metadata, existing cases, runners, and rules. It writes `source.type: init` executable contracts for functional, positive, negative, boundary, side-effect-safe, and stress/timeout-risk coverage. Every generated contract gets a side-effect-safe `commands[].run` probe; lab fixtures are later enhancements, not init blockers.

`cases generate --growing` builds `.quality-pilot-project/state/growth-context.json` from repo metadata, issue snapshots, PR references, latest run, publish plan, existing cases, runners, and rules. It then writes `source.type: growth` executable case contracts under `.quality-pilot-project/cases/`.

`--generated_count <max>` is the explicit generation limit for users who want a smaller batch. `--fast` switches case generation to autonomous strict-safe defaults after the runtime profile has been confirmed. If the runtime profile is missing, case generation stops with `needs_input`; repo-only metadata checks remain readiness probes and are not written as placeholder testcase contracts.

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
