# Configuration model

The host project owns `.qa-aist.yaml` and `.qa-aist/`.

Minimal config shape:

```yaml
project:
  name: example-project
  default_branch: main

paths:
  workspace: .qa-aist
  cases: .qa-aist/cases
  runners: .qa-aist/runners
  rules: .qa-aist/rules
  state: .qa-aist/state
  evidence: .qa-aist/evidence
  reports: .qa-aist/reports

tracker:
  provider: none
  project: ""
  api_token_env: QA_AIST_TRACKER_TOKEN

policy:
  deterministic_first: true
  require_write_gate: true
  prohibit_closed_issue_comments: true
  prohibit_raw_secrets_in_repo: true
```

Secrets must be referenced by environment variable name, not stored as literal values.
