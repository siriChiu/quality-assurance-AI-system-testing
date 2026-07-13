# Configuration model

The host project owns `.quality-pilot.yaml` and `.quality-pilot-project/`.

AI Quality Pilot V1 is Hermes MCP-first for tracker integration. Local repo
analysis, case generation, validation, execution, evidence, audit, and local
reports can still be used when Gitea or Redmine is unavailable; only the
corresponding integration is not ready. The config does **not** store Gitea base
URLs, repo names, tracker token env names, or HTTP credentials. Hermes owns the
Gitea/Redmine MCP connections; AI Quality Pilot owns validation, local
snapshots, evidence, reports, and gated handoff payloads.

Generated config skeleton (most users should run `setup` instead of writing it
by hand):

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
      endpoint: ""
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
  require_swqa_pattern_expansion: true  # target policy; enforcement is partial
  require_sibling_surface_scan: true    # target policy; enforcement is partial
  require_boundary_invalid_tests: true  # target policy; enforcement is partial
  require_side_effect_safe_repro: true  # target policy; enforcement is partial
```

`required_servers` declares the integrations needed for the full tracker loop;
it does not turn a local-only QA workflow into a failure. Interpret readiness by
capability: local work may be ready while Redmine, Gitea, Wiki, or subagent
health is unavailable. See [`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md).

## Runtime Profile

`runtime` is intentionally user-overridable, but AI Quality Pilot analyzes the repo first and infers it when possible. If a product executable is found under common output paths such as `cmd/<name>/<name>`, `bin/<name>`, `dist/<name>`, or the repo root, `runtime_profile.status` becomes `ready_inferred` and no entrypoint question is asked.

Use these fields to prepare automation once:

- `primary_entrypoint`: the user-facing product runner, binary, or API command.
- `binary_env`: env var pointing to the built product binary when applicable; default `QUALITY_PILOT_BINARY`.
- `target_host_env`: env var for a prepared target/lab resource when applicable; default `QUALITY_PILOT_TARGET_HOST`.
- `fixture_paths`: non-secret fixture/config paths required for tests.
- `credential_envs`: names of env vars that hold credentials; never store raw secret values.
- `side_effect_boundary`: what the runner may and may not touch during unattended execution.

`doctor` exposes `runtime_profile.repo_analysis` before asking anything. Clarify prompts are bullet-listed and ask only for details the repo analysis could not infer, such as missing runner path, credential env names, target resources, fixture/config paths, or side-effect boundaries for non-parser tests.

Generated testcase commands must use this product entrypoint or a user-confirmed runner. Repo-only metadata checks, static repo checks, `python3 -c`, `compileall`, synthetic invalid commands, `go test`, and `go run` are readiness or implementation hints, not testcase `commands[].run`, unless the user explicitly configured one of them as the product runner.

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
3. AI Quality Pilot runs `/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when those Redmine tickets should be mirrored locally and created or updated as linked Gitea issues through Hermes MCP.
4. AI Quality Pilot runs `/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]` when linked testcase contracts should be generated directly. This command does not create a Gitea sync plan.

Legacy/raw/trimmed Redmine snapshots are rejected for `--redmine-issues`; this prevents stale local snapshot data from masking newer live Redmine descriptions, journals, or attachments.

## Automation Profile Candidate

`/quality-pilot setup` and `/quality-pilot doctor` write:

```text
.quality-pilot-project/state/automation-profile.candidate.json
```

This file is generated from repo/config analysis. It records the inferred product entrypoint, user-visible command candidates, generic fixture env names such as `QUALITY_PILOT_FIXTURE_PROFILE`, credential env placeholders such as `QUALITY_PILOT_TEST_USER`, target env names such as `QUALITY_PILOT_TARGET_HOST`, safety classes, and missing external facts.

It must not contain raw secrets and must not be treated as verified test coverage. Case generation should use it as candidate context, then write runnable YAML only after command/oracle/fixture/side-effect review.

## Wiki Status

`/quality-pilot setup` creates `.quality-pilot-project/rules/wiki-categories.yaml` and defaults `tracker.wiki_page` to `Quality Pilot Test Status`.

Wiki auto-sync is enabled by default through `policy.auto_publish_wiki: true`.
Here, **auto-sync means refreshing local Wiki plan/status artifacts** after case
generation, test execution, close-loop execution, and successful gated write
summaries. It does not grant a remote write and does not silently update Gitea.

`/quality-pilot publish wiki apply` never uses an internal token. When the Wiki gate passes and Hermes Gitea MCP is available, AI Quality Pilot writes:

- `.quality-pilot-project/state/wiki-plan.json`
- `.quality-pilot-project/state/gitea-mcp/wiki-write-request.json`
- `.quality-pilot-project/reports/wiki-status.md`

Only an explicit `/quality-pilot publish wiki apply` flow may cross the remote
write gate. Hermes then uses Gitea MCP to update only the requested Wiki page
and writes the result JSON to:

```text
.quality-pilot-project/state/gitea-mcp/wiki-write-result.json
```

AI Quality Pilot must not use Wiki apply to create issue comments, create issues, create PRs, or write arbitrary Wiki pages. Gitea issue create/update and FAIL/BLOCK evidence writeback are separate issue-sync/report handoffs and must use their own gated request payloads.

## Subagent Text Generation

`subagents` configures candidate-only text generation. Open WebUI is a supported
provider profile, but no private network address is a universal product default.
The deployment owns the endpoint and model. For example:

```yaml
endpoint: "https://open-webui.example.invalid/?model=<model-name>"
```

or:

```yaml
endpoint: "https://open-webui.example.invalid/"
model: "<model-name>"
api_key_env: "OPEN_WEBUI_API_KEY"
```

`api_key_env` stores only an environment variable name, never the raw API key. `task_prompts` are optional overrides for advanced users; blank task prompts do not block subagent readiness.

Use:

```text
/quality-pilot subagent status
/quality-pilot subagent configure
/quality-pilot doctor --fix
```

`doctor --fix` and `subagent configure` can create the Open WebUI routing
skeleton, but endpoint/model/API settings remain user-owned. If the deployment
has no subagent, leave it unconfigured or disable it; deterministic local
features remain usable. Configured subagents may draft candidate text for Gitea
issue bodies, PR bodies, Wiki summaries, Redmine summaries, case candidate
analysis, and reviewer notes. They must not write files, create issues, edit
Wiki pages, open PRs, close issues, or bypass AI Quality Pilot validation/write
gates.

## Policy Fields

The SWQA policy fields express the intended issue-level quality policy. The
current checkout supports structured command assertions, four-axis truth, and a
first stratified generation slice. Complete enforcement that every confirmed
bug has deep sibling-surface, boundary, invalid-value, residual-risk, and
white-box/black-box evidence is still Partial. A command-level
`test_outcome: PASS` must not be presented as complete issue or release
readiness. Exit-only/partial probes are summarized through `probe_outcome`; a
partial-only run has official `test_outcome: HOLD`. See
[`SWQA_TEST_DESIGN.md`](SWQA_TEST_DESIGN.md).

`paths.issues` is optional for older configs. If it is missing, AI Quality Pilot uses `<workspace>/issues`.

Secrets must not be stored in `.quality-pilot.yaml`, case YAML, issue mirrors, reports, or Wiki content.
