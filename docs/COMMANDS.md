# Command Surface

QA-AIST 的主介面是 Hermes 聊天室中的 `/qa-aist ...`。CLI 是 Hermes 背後的 deterministic engine，同一組 command surface 也可給 CI 或本機除錯使用。

Hermes 回覆不是純 JSON dump。QA-AIST dispatcher 會在 payload 裡放 `next_actions`，skill 應該把它呈現成繁體中文選單，讓使用者回覆編號後繼續操作。`requires_confirmation: true` 的下一步不可直接執行。

需要使用者補資料時，dispatcher 會固定回傳 `payload.input_required: true`、`payload.interaction.type: "needs_input"` 與 `payload.hermes_needs_input.questions[]`。Hermes 應逐題呼叫 `clarify`；若 runtime 不支援，才用聊天室逐題詢問。

## Hermes Workflow

```text
/qa-aist setup
/qa-aist doctor
/qa-aist issues sync
/qa-aist issues dedupe
/qa-aist cases generate --init
/qa-aist cases review
/qa-aist cases validate
/qa-aist qa-test list
/qa-aist qa-test run-one <case_id>
/qa-aist publish wiki status
/qa-aist publish wiki plan
/qa-aist publish wiki apply
/qa-aist fix-issues plan --issue <id>
/qa-aist fix-issues submit-pr --issue <id>
```

## Command Groups

| Group | Commands | Purpose |
|---|---|---|
| setup | `setup`, `init-project`, `status`, `doctor` | bootstrap and health checks |
| config | `show`, `validate` | inspect host-owned `.qa-aist.yaml` |
| issues | `sync`, `status`, `show`, `dedupe` | sync Gitea issue mirrors and detect duplicates |
| cases | `generate`, `review`, `validate` | generate/review growth draft case contracts |
| qa-test | `list`, `validate`, `dry-run`, `run`, `run-one`, `help` | execute case contracts and collect evidence |
| publish wiki | `plan`, `apply`, `status`, `render` | maintain the Wiki-only status board |
| publish | `plan`, `apply`, `status` | legacy mixed wiki/issues publish flow |
| fix-issues | `plan`, `run`, `submit-pr`, `status` | preflight repair work and create Gitea PRs |
| close-loop | `status`, `run-once` | run the deterministic local test/report pipeline |
| report | `status`, `json` | render latest reports |
| tracker | `plan-write` | legacy single-result write-gate check |

## Legacy Aliases

| Legacy | Current |
|---|---|
| `sync-gitea pull` | `issues sync` |
| `sync-gitea status` | `issues status` |
| `sync-gitea validate` | `issues status` |
| `find-new-issues run` | `publish plan` |
| `find-new-issues dry-run` | `publish plan` |

## Direct Engine Examples

```bash
qa-aist init-project --root <target-repo>
qa-aist init-project --root <target-repo> --tracker-provider gitea --gitea-backend mcp --gitea-base-url https://git.example.com --gitea-repo owner/repo
qa-aist issues sync --root <target-repo>
qa-aist cases generate --root <target-repo> --init
qa-aist cases generate --root <target-repo> --init --feature "CLI help" --profile cli
qa-aist cases generate --root <target-repo> --growing
qa-aist cases generate --root <target-repo> --growing --candidate-json growth-candidates.json
qa-aist qa-test run-one --root <target-repo> ISSUE-1
qa-aist publish wiki status --root <target-repo>
qa-aist publish wiki plan --root <target-repo>
qa-aist publish wiki apply --root <target-repo>
qa-aist fix-issues submit-pr --root <target-repo> --issue 1 --dry-run
```

From a source checkout:

```bash
PYTHONPATH=src python3 -m qa_aist.cli issues sync --root <target-repo> --issues-json issues.json
```

`setup` / `init-project` defaults to `--tracker-provider auto`. If the target repo has a parseable `git remote origin`, QA-AIST writes a Gitea MCP-ready config automatically. Use `--tracker-provider none` to keep tracker disabled, or `--gitea-backend http` when CI should call Gitea REST directly with a token env.

If `.qa-aist.yaml` uses `tracker.gitea.backend: mcp`, Hermes must first use its configured Gitea MCP read tool to write raw issue JSON to `.qa-aist-project/state/gitea-mcp/issues.json` or `QA_AIST_GITEA_MCP_ISSUES_JSON`, then run:

```text
/qa-aist issues sync
```

## Case Generation

`cases generate` requires an explicit mode. Bare `/qa-aist cases generate` returns `explicit_generation_mode_required` so Hermes can ask the user which path they want:

```text
/qa-aist cases generate --init
/qa-aist cases generate --growing
/qa-aist cases generate --init --feature "CLI help" --profile cli
/qa-aist cases generate --growing --candidate-json <path>
```

`--init` is the first-time full-repo SWQA map. It scans README, code inventory, package metadata, existing runners, existing cases, and rules to generate functional, positive, negative, boundary, side-effect-safe, and stress/timeout draft contracts. It does not apply an arbitrary default count cap; by default it generates the full init seed x SWQA dimension map. Use `--count <max>` only when you intentionally want a smaller exploratory subset.

`--growing` is the follow-up mode. It creates draft YAML contracts under `.qa-aist-project/cases/` using repo signals, issue snapshot, PR references, latest run, reports, existing cases/runners, and the built-in SWQA policy pack. Drafts include `growth_seed`, `six_hats`, `growth_reason`, `qa_aist.questions`, and usually `review_required_before_run: true`; those cases are visible to `cases review` and `qa-test dry-run`, but formal execution returns `BLOCK` until the contract is reviewed and given a confirmed command/fixture.

`--from-issues` has been replaced by growing mode and returns a structured `renamed_to_growing` error.

## Remote Write Rule

QA-AIST can write real Gitea Wiki/issues/PRs only through:

```text
/qa-aist publish wiki apply
/qa-aist publish apply
/qa-aist fix-issues submit-pr --issue <id>
```

`publish wiki apply` is Wiki-only and never creates issue comments, issues, or PRs. The old `publish apply` is the mixed wiki/issues flow. Every remote write must pass deterministic write gate first. Blocked writes must stay blocked; Hermes must not call Gitea directly to bypass QA-AIST.

`tracker.gitea.backend: mcp` is read-only in V1 and cannot apply Wiki plans, mixed publish plans, or submit PRs. Use HTTP backend plus token env for real remote writes.

## Host Data Boundary

All commands must accept explicit paths. Tool source stays in `.qa-aist` or an installed package; host runtime data stays in `.qa-aist-project`.

`init-project` refuses to use a workspace that is itself a QA-AIST source checkout. This prevents embedded-tool layouts such as `<target-repo>/.qa-aist/` from receiving host-project cases, issue mirrors, evidence, or runtime state.
