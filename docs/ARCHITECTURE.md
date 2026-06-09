# Architecture

QA-AIST uses a deterministic-first close-loop pipeline. Agents and LLMs may summarize evidence or draft text, but they do not decide whether to skip required steps or write to trackers.

```text
+--------------------+
| qa-aist CLI        |
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

The policy pack is intentionally generic. It defines stable dimensions such as exact reproduction, positive, negative, boundary, invalid input, sibling surface, side-effect-safe, and stress/timeout-risk coverage. Project-specific assumptions such as lab topology, hardware fixture paths, Redfish baselines, or VM images belong in the host project's `.qa-aist-project/rules/` or generated case contracts, not in QA-AIST core.

## Init and growing case generation

`cases generate` requires `--init` or `--growing`; a bare command returns `explicit_generation_mode_required`.

`cases generate --init` builds `.qa-aist-project/state/init-context.json` from README presence, code inventory, package metadata, existing cases, runners, and rules. It writes `source.type: init` draft contracts for functional, positive, negative, boundary, side-effect-safe, and stress/timeout-risk coverage.

`cases generate --growing` builds `.qa-aist-project/state/growth-context.json` from repo metadata, issue snapshots, PR references, latest run, publish plan, existing cases, runners, and rules. It then writes `source.type: growth` draft case contracts under `.qa-aist-project/cases/`.

`--generated_count <max>` is the explicit generation limit for users who want a smaller batch. `--fast` switches case generation to autonomous strict-safe defaults: QA-AIST asks no interactive category questions, records its assumptions, and leaves unsafe or unconfirmed execution as HOLD/BLOCK rather than pretending a draft is runnable.

Hermes may use a separate growth session to analyze the context, but that session may only produce candidate JSON. QA-AIST validates candidate schema, dedupe fingerprints, secret leakage, internal prompt leakage, dangerous `.qa` runtime paths, and command fields before writing YAML.

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
