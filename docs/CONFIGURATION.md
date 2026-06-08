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
  issues: .qa-aist-project/issues
  state: .qa-aist-project/state
  evidence: .qa-aist-project/evidence
  reports: .qa-aist-project/reports

tracker:
  provider: gitea
  project: ""
  api_token_env: QA_AIST_TRACKER_TOKEN
  gitea:
    base_url: "https://git.example.com"
    repo: "owner/repo"
    token_env: QA_AIST_GITEA_TOKEN
    wiki_page: "Test status"
    branch_prefix: "qa-aist/issue-"

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

`paths.issues` is optional for older configs. If it is missing, QA-AIST uses `<workspace>/issues`.

Gitea remote writes require:

- `tracker.provider: gitea`
- `tracker.gitea.base_url`
- `tracker.gitea.repo`
- `tracker.gitea.token_env`
- the environment variable named by `token_env`

Secrets must be referenced by environment variable name, not stored as literal values.
