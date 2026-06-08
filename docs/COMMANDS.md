# Command Surface

QA-AIST 的主介面是 Hermes 聊天室中的 `/qa-aist ...`。CLI 是 Hermes 背後的 deterministic engine，同一組 command surface 也可給 CI 或本機除錯使用。

## Hermes Workflow

```text
/qa-aist setup
/qa-aist doctor
/qa-aist issues sync
/qa-aist issues dedupe
/qa-aist cases generate --from-issues
/qa-aist cases review
/qa-aist cases validate
/qa-aist qa-test list
/qa-aist qa-test run-one <case_id>
/qa-aist publish plan
/qa-aist publish apply
/qa-aist fix-issues plan --issue <id>
/qa-aist fix-issues submit-pr --issue <id>
```

## Command Groups

| Group | Commands | Purpose |
|---|---|---|
| setup | `setup`, `init-project`, `status`, `doctor` | bootstrap and health checks |
| config | `show`, `validate` | inspect host-owned `.qa-aist.yaml` |
| issues | `sync`, `status`, `show`, `dedupe` | sync Gitea issue mirrors and detect duplicates |
| cases | `generate`, `review`, `validate` | generate/review case contracts from synced issues |
| qa-test | `list`, `validate`, `dry-run`, `run`, `run-one`, `help` | execute case contracts and collect evidence |
| publish | `plan`, `apply`, `status` | convert latest run into gated Gitea wiki/issues writes |
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
qa-aist issues sync --root <target-repo>
qa-aist cases generate --root <target-repo> --from-issues
qa-aist qa-test run-one --root <target-repo> ISSUE-1
qa-aist publish plan --root <target-repo>
qa-aist publish apply --root <target-repo>
qa-aist fix-issues submit-pr --root <target-repo> --issue 1 --dry-run
```

From a source checkout:

```bash
PYTHONPATH=src python3 -m qa_aist.cli issues sync --root <target-repo> --issues-json issues.json
```

## Remote Write Rule

QA-AIST can write real Gitea wiki/issues/PRs only through:

```text
/qa-aist publish apply
/qa-aist fix-issues submit-pr --issue <id>
```

Every remote write must pass deterministic write gate first. Blocked writes must stay blocked; Hermes must not call Gitea directly to bypass QA-AIST.

## Host Data Boundary

All commands must accept explicit paths. Tool source stays in `.qa-aist` or an installed package; host runtime data stays in `.qa-aist-project`.

`init-project` refuses to use a workspace that is itself a QA-AIST source checkout. This prevents embedded-tool layouts such as `<target-repo>/.qa-aist/` from receiving host-project cases, issue mirrors, evidence, or runtime state.
