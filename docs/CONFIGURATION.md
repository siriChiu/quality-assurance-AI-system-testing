# Configuration model

The host project owns `.quality-pilot.yaml` and `.quality-pilot-project/`.

AI Quality Pilot V1 is Hermes MCP-first. The config does **not** store Gitea base URLs, repo names, tracker token env names, or HTTP credentials. Hermes owns the Gitea/Redmine MCP connections; AI Quality Pilot owns validation, local snapshots, evidence, reports, and gated Wiki handoff payloads.

Minimal config shape:

```yaml
project:
  name: example-project
  default_branch: main

paths:
  workspace: .quality-pilot-project
  cases: .quality-pilot-project/cases
  runners: .quality-pilot-project/runners
  rules: .quality-pilot-project/rules
  issues: .quality-pilot-project/issues
  state: .quality-pilot-project/state
  evidence: .quality-pilot-project/evidence
  reports: .quality-pilot-project/reports

tracker:
  provider: hermes_mcp
  wiki_page: "Test status (Siri)"
  mcp:
    required_servers:
      - gitea
      - redmine
    status_json: .quality-pilot-project/state/hermes-mcp/status.json
    gitea_issues_json: .quality-pilot-project/state/gitea-mcp/issues.json
    redmine_issues_json: .quality-pilot-project/state/redmine-mcp/issues.json
    wiki_write_request_json: .quality-pilot-project/state/gitea-mcp/wiki-write-request.json
    wiki_write_result_json: .quality-pilot-project/state/gitea-mcp/wiki-write-result.json

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

`/quality-pilot setup` creates the MCP-only config. `/quality-pilot doctor` then checks whether Hermes has exposed the required MCP servers.

AI Quality Pilot accepts either:

```bash
QUALITY_PILOT_HERMES_MCP_SERVERS=gitea,redmine
```

or this local status file:

```json
{
  "servers": ["gitea", "redmine"]
}
```

Default path:

```text
.quality-pilot-project/state/hermes-mcp/status.json
```

If Gitea or Redmine MCP is missing or unknown, `doctor` reports it immediately. AI Quality Pilot will still create local plans/reports, but remote issue sync and Wiki apply are not marked ready.

## Issue Snapshots

Gitea issue sync is a two-step MCP handoff:

1. Hermes reads issues through its configured Gitea MCP server using the current repo context.
2. Hermes writes the raw JSON snapshot to `tracker.mcp.gitea_issues_json`.
3. AI Quality Pilot runs `/quality-pilot issues sync` to mirror, dedupe, prune closed issues, and persist local state.

Redmine import has two explicit paths:

1. Hermes reads requested Redmine IDs through Redmine MCP.
2. Hermes writes the snapshot to `tracker.mcp.redmine_issues_json`.
3. AI Quality Pilot runs `/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when those Redmine tickets should be mirrored locally and created as gated Gitea issues through Hermes MCP.
4. AI Quality Pilot runs `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when linked testcase contracts should be generated directly. This command does not create a Gitea sync plan.

## Wiki Status

`/quality-pilot setup` creates `.quality-pilot-project/rules/wiki-categories.yaml` and defaults `tracker.wiki_page` to `Test status (Siri)`.

Wiki auto-sync is enabled by default through `policy.auto_publish_wiki: true`. It runs after case generation, test execution, close-loop execution, and successful gated write summaries.

`/quality-pilot publish wiki apply` never uses an internal token. When the Wiki gate passes and Hermes Gitea MCP is available, AI Quality Pilot writes:

- `.quality-pilot-project/state/wiki-plan.json`
- `.quality-pilot-project/state/gitea-mcp/wiki-write-request.json`
- `.quality-pilot-project/reports/wiki-status.md`

Hermes then uses Gitea MCP to update only the requested Wiki page and writes the result JSON to:

```text
.quality-pilot-project/state/gitea-mcp/wiki-write-result.json
```

AI Quality Pilot must not use MCP to create issue comments, create issues, create PRs, or write arbitrary Wiki pages.

## Policy Fields

The SWQA policy fields require every confirmed bug to be expanded into sibling-surface, boundary, invalid-value, and side-effect-safe regression coverage before it can be called PASS.

`paths.issues` is optional for older configs. If it is missing, AI Quality Pilot uses `<workspace>/issues`.

Secrets must not be stored in `.quality-pilot.yaml`, case YAML, issue mirrors, reports, or Wiki content.
