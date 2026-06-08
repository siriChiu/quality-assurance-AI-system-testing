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
