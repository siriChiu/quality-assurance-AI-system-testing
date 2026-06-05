# Configuration model

The host project owns `.qa-aist.yaml` and `.qa-aist-project/`.

Minimal config shape:

```yaml
project:
  name: example-project
  default_branch: main

paths:
  workspace: .qa-aist-project
  cases: .qa-aist-project/cases
  runners: .qa-aist-project/runners
  rules: .qa-aist-project/rules
  state: .qa-aist-project/state
  evidence: .qa-aist-project/evidence
  reports: .qa-aist-project/reports

tracker:
  provider: none
  project: ""
  api_token_env: QA_AIST_TRACKER_TOKEN

policy:
  deterministic_first: true
  require_write_gate: true
  prohibit_closed_issue_comments: true
  prohibit_raw_secrets_in_repo: true
  require_swqa_pattern_expansion: true
  require_sibling_surface_scan: true
  require_boundary_invalid_tests: true
  require_side_effect_safe_repro: true
```

The SWQA policy fields require every confirmed bug to be expanded into sibling-surface, boundary, invalid-value, and side-effect-safe regression coverage before it can be called PASS.

Secrets must be referenced by environment variable name, not stored as literal values.
