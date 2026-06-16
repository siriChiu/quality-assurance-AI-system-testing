# Configuration model

The host project owns `.qa-aist.yaml` and `.qa-aist-project/`.

QA-AIST V1 is Hermes MCP-first. The config does **not** store Gitea base URLs, repo names, tracker token env names, or HTTP credentials. Hermes owns the Gitea/Redmine MCP connections; QA-AIST owns validation, local snapshots, evidence, reports, and gated Wiki handoff payloads.

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
  provider: hermes_mcp
  wiki_page: "Test status (Siri)"
  mcp:
    required_servers:
      - gitea
      - redmine
    status_json: .qa-aist-project/state/hermes-mcp/status.json
    gitea_issues_json: .qa-aist-project/state/gitea-mcp/issues.json
    redmine_issues_json: .qa-aist-project/state/redmine-mcp/issues.json
    wiki_write_request_json: .qa-aist-project/state/gitea-mcp/wiki-write-request.json
    wiki_write_result_json: .qa-aist-project/state/gitea-mcp/wiki-write-result.json

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

## Hermes MCP Readiness

`/qa-aist setup` creates the MCP-only config. `/qa-aist doctor` then checks whether Hermes has exposed the required MCP servers.

QA-AIST accepts either:

```bash
QA_AIST_HERMES_MCP_SERVERS=gitea,redmine
```

or this local status file:

```json
{
  "servers": ["gitea", "redmine"]
}
```

Default path:

```text
.qa-aist-project/state/hermes-mcp/status.json
```

If Gitea or Redmine MCP is missing or unknown, `doctor` reports it immediately. QA-AIST will still create local plans/reports, but remote issue sync and Wiki apply are not marked ready.

## Issue Snapshots

Gitea issue sync is a two-step MCP handoff:

1. Hermes reads issues through its configured Gitea MCP server using the current repo context.
2. Hermes writes the raw JSON snapshot to `tracker.mcp.gitea_issues_json`.
3. QA-AIST runs `/qa-aist issues sync` to mirror, dedupe, prune closed issues, and persist local state.

Redmine import has two explicit paths:

1. Hermes reads requested Redmine IDs through Redmine MCP.
2. Hermes writes the snapshot to `tracker.mcp.redmine_issues_json`.
3. QA-AIST runs `/qa-aist issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when those Redmine tickets should be mirrored locally and created as gated Gitea issues through Hermes MCP.
4. QA-AIST runs `/qa-aist cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when linked testcase contracts should be generated directly. This command does not create a Gitea sync plan.

## Wiki Status

`/qa-aist setup` creates `.qa-aist-project/rules/wiki-categories.yaml` and defaults `tracker.wiki_page` to `Test status (Siri)`.

Wiki auto-sync is enabled by default through `policy.auto_publish_wiki: true`. It runs after case generation, test execution, close-loop execution, and successful gated write summaries.

`/qa-aist publish wiki apply` never uses an internal token. When the Wiki gate passes and Hermes Gitea MCP is available, QA-AIST writes:

- `.qa-aist-project/state/wiki-plan.json`
- `.qa-aist-project/state/gitea-mcp/wiki-write-request.json`
- `.qa-aist-project/reports/wiki-status.md`

Hermes then uses Gitea MCP to update only the requested Wiki page and writes the result JSON to:

```text
.qa-aist-project/state/gitea-mcp/wiki-write-result.json
```

QA-AIST must not use MCP to create issue comments, create issues, create PRs, or write arbitrary Wiki pages.

## Policy Fields

The SWQA policy fields require every confirmed bug to be expanded into sibling-surface, boundary, invalid-value, and side-effect-safe regression coverage before it can be called PASS.

`paths.issues` is optional for older configs. If it is missing, QA-AIST uses `<workspace>/issues`.

Secrets must not be stored in `.qa-aist.yaml`, case YAML, issue mirrors, reports, or Wiki content.
