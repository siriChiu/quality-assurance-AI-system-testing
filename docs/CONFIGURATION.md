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
  wiki_page: "Quality Pilot Test Status"
  mcp:
    required_servers:
      - gitea
      - redmine
    status_json: .quality-pilot-project/state/hermes-mcp/status.json
    gitea_issues_json: .quality-pilot-project/state/gitea-mcp/issues.json
    redmine_issues_json: .quality-pilot-project/state/redmine-mcp/issues.json
    wiki_write_request_json: .quality-pilot-project/state/gitea-mcp/wiki-write-request.json
    wiki_write_result_json: .quality-pilot-project/state/gitea-mcp/wiki-write-result.json

runtime:
  primary_entrypoint: ""
  binary_env: QUALITY_PILOT_BINARY
  target_host_env: QUALITY_PILOT_TARGET_HOST
  fixture_paths: []
  credential_envs: []
  side_effect_boundary: ""

subagents:
  enabled: true
  default_profile: open-webui
  profiles:
    open-webui:
      provider: open_webui
      endpoint: "https://172.17.20.220/"
      model: ""
      api_base: ""
      api_key_env: ""
  text_generation:
    mode: subagent_handoff
    review_required: true
    tasks:
      gitea_issue_body: open-webui
      pull_request_body: open-webui
      wiki_status_summary: open-webui
      case_candidate_analysis: open-webui
      redmine_issue_summary: open-webui
      reviewer_notes: open-webui
    task_prompts: {}

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

## Runtime Profile

`runtime` is intentionally user-overridable, but AI Quality Pilot analyzes the repo first and infers it when possible. If a product executable is found under common output paths such as `cmd/<name>/<name>`, `bin/<name>`, `dist/<name>`, or the repo root, `runtime_profile.status` becomes `ready_inferred` and no entrypoint question is asked.

Use these fields to prepare automation once:

- `primary_entrypoint`: the user-facing runner, binary, API command, or repo-only health entrypoint.
- `binary_env`: env var pointing to the built product binary when applicable; default `QUALITY_PILOT_BINARY`.
- `target_host_env`: env var for a prepared target/lab resource when applicable; default `QUALITY_PILOT_TARGET_HOST`.
- `fixture_paths`: non-secret fixture/config paths required for tests.
- `credential_envs`: names of env vars that hold credentials; never store raw secret values.
- `side_effect_boundary`: what the runner may and may not touch during unattended execution.

`doctor` exposes `runtime_profile.repo_analysis` before asking anything. Clarify prompts are bullet-listed and ask only for details the repo analysis could not infer, such as missing runner path, credential env names, target resources, fixture/config paths, or side-effect boundaries for non-parser tests.

## Hermes MCP Readiness

`/quality-pilot setup` creates the MCP-only config. `/quality-pilot doctor` then checks whether Hermes has exposed the required MCP servers. `/quality-pilot doctor --fix` repairs a missing or incomplete safe config skeleton and overlay directories before running the same checks.

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

1. Hermes live-reads requested Redmine IDs through Redmine MCP.
2. Hermes writes a verified `quality-pilot.redmine-mcp-issues.v1` manifest to `tracker.mcp.redmine_issues_json`, including `fetched_at`, `requested_issue_ids`, `include: [description, custom_fields, journals, attachments]`, `payload_completeness: full`, and issue entries with full description, `updated_on`, custom fields, journals/comments, and attachments.
3. AI Quality Pilot runs `/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when those Redmine tickets should be mirrored locally and created as gated Gitea issues through Hermes MCP.
4. AI Quality Pilot runs `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when linked testcase contracts should be generated directly. This command does not create a Gitea sync plan.

Legacy/raw/trimmed Redmine snapshots are rejected for `--redmine-issues`; this prevents stale local snapshot data from masking newer live Redmine descriptions, journals, or attachments.

## Wiki Status

`/quality-pilot setup` creates `.quality-pilot-project/rules/wiki-categories.yaml` and defaults `tracker.wiki_page` to `Quality Pilot Test Status`.

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

## Subagent Text Generation

`subagents` configures candidate-only text generation. The default profile is Open WebUI:

```text
https://172.17.20.220/
```

AI Quality Pilot writes the endpoint and routing defaults. The only required user-owned model setting is either:

```yaml
endpoint: "https://172.17.20.220/?model=qwen3.6-chat-direct"
```

or:

```yaml
endpoint: "https://172.17.20.220/"
model: "qwen3.6-chat-direct"
api_key_env: "OPEN_WEBUI_API_KEY"
```

`api_key_env` stores only an environment variable name, never the raw API key. `task_prompts` are optional overrides for advanced users; blank task prompts do not block subagent readiness.

Use:

```text
/quality-pilot subagent status
/quality-pilot subagent configure
/quality-pilot doctor --fix
```

`doctor --fix` and `subagent configure` can create the Open WebUI routing skeleton, but model/API settings remain user-owned. Configured subagents may draft candidate text for Gitea issue bodies, PR bodies, Wiki summaries, Redmine summaries, case candidate analysis, and reviewer notes. They must not write files, create issues, edit Wiki pages, open PRs, close issues, or bypass AI Quality Pilot validation/write gates.

## Policy Fields

The SWQA policy fields require every confirmed bug to be expanded into sibling-surface, boundary, invalid-value, and side-effect-safe regression coverage before it can be called PASS.

`paths.issues` is optional for older configs. If it is missing, AI Quality Pilot uses `<workspace>/issues`.

Secrets must not be stored in `.quality-pilot.yaml`, case YAML, issue mirrors, reports, or Wiki content.
