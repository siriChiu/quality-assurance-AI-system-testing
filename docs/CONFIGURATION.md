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
    backend: http
    base_url: "https://git.example.com"
    repo: "owner/repo"
    token_env: QA_AIST_GITEA_TOKEN
    mcp_issues_json: .qa-aist-project/state/gitea-mcp/issues.json
    wiki_page: "Test status (Siri)"
    branch_prefix: "qa-aist/issue-"

policy:
  deterministic_first: true
  require_write_gate: true
  auto_publish_wiki: true
  prohibit_closed_issue_comments: true
  prohibit_raw_secrets_in_repo: true
  require_swqa_pattern_expansion: true
  require_sibling_surface_scan: true
  require_boundary_invalid_tests: true
  require_side_effect_safe_repro: true
```

The SWQA policy fields require every confirmed bug to be expanded into sibling-surface, boundary, invalid-value, and side-effect-safe regression coverage before it can be called PASS.

`paths.issues` is optional for older configs. If it is missing, QA-AIST uses `<workspace>/issues`.

`/qa-aist setup` uses `--tracker-provider auto` by default. When the target repo has a parseable `git remote origin`, setup fills `tracker.provider: gitea`, `tracker.gitea.backend: mcp`, `tracker.gitea.base_url`, and `tracker.gitea.repo` automatically. Without a remote, setup keeps `tracker.provider: none`.

## Gitea Backends

`tracker.gitea.backend` controls how `/qa-aist issues sync` reads remote issue state:

| Backend | Purpose | Token required for `issues sync` | Remote writes |
|---|---|---:|---|
| `http` | QA-AIST calls Gitea REST API directly | yes, via `token_env` | yes, automatic gated Wiki sync, `publish wiki apply`, legacy `publish apply`, and `submit-pr` |
| `mcp` | Hermes uses Gitea MCP read tooling, writes a JSON snapshot, then QA-AIST imports it | no | no, blocked in V1 |

MCP read-only config:

```yaml
tracker:
  provider: gitea
  gitea:
    backend: mcp
    repo: "owner/repo"
    mcp_issues_json: .qa-aist-project/state/gitea-mcp/issues.json
```

When `backend: mcp`, Hermes must fetch Gitea issues through its configured Gitea MCP tool and write the raw issue JSON to `tracker.gitea.mcp_issues_json` before running `/qa-aist issues sync`. The environment variable `QA_AIST_GITEA_MCP_ISSUES_JSON` can override that path.

Do not use Gitea MCP to write comments, wiki pages, issues, or PRs directly. QA-AIST V1 only accepts MCP as a read input for issue sync.

## Wiki Status

`/qa-aist setup` creates `.qa-aist-project/rules/wiki-categories.yaml` and defaults `tracker.gitea.wiki_page` to `Test status (Siri)`.

Wiki auto-sync is enabled by default through `policy.auto_publish_wiki: true`. It runs after case generation, test execution, close-loop execution, and successful Gitea writes. Remote Wiki writes require:

- `tracker.provider: gitea`
- `tracker.gitea.backend: http`
- `tracker.gitea.base_url`
- `tracker.gitea.repo`
- token env present
- Wiki gate allowed

If any requirement is missing, QA-AIST only writes local Wiki state:

- `.qa-aist-project/state/wiki-plan.json`
- `.qa-aist-project/state/wiki-apply-result.json`
- `.qa-aist-project/reports/wiki-status.md`

Gitea HTTP remote writes require:

- `tracker.provider: gitea`
- `tracker.gitea.backend: http`
- `tracker.gitea.base_url`
- `tracker.gitea.repo`
- `tracker.gitea.token_env`
- the environment variable named by `token_env`

Secrets must be referenced by environment variable name, not stored as literal values.
