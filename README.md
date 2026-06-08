# QA-AIST

![Status](https://img.shields.io/badge/status-Gitea--first%20lifecycle-green)
![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)
![Hermes](https://img.shields.io/badge/hermes-dynamic%20skill-purple)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

QA-AIST 是給 Hermes 使用的開源 SWQA lifecycle agent/plugin。使用者在 Hermes 聊天室輸入 `/qa-aist ...`，Hermes 依 `SKILL.md` 呼叫 QA-AIST engine，同步 Gitea issues、產生 test cases、執行測試、保存 evidence、更新 wiki/issues，並在修復後送出 Gitea PR。

English summary: QA-AIST is a Hermes-first deterministic QA lifecycle engine for Gitea issue sync, test-case generation, evidence-based test execution, gated publishing, and PR handoff.

## What Is QA-AIST?

QA-AIST 把 SWQA 流程固定成一條可重跑、可審計的 pipeline：issue sync -> case generation -> qa-test -> publish gate -> fix/PR。Hermes 可以協助問答和修碼，但不能自己跳過 QA-AIST 的 sync、contract、evidence、write gate 或 duplicate checks。

目前整合方式是 **Hermes dynamic skill**：Hermes 會掃描 `~/.hermes/skills/qa-aist/SKILL.md`，再由 agent 依 skill 指示執行 QA-AIST dispatcher。這不是 native Hermes router；如果你要 LLM 前置的 deterministic slash command，需要另外接 Hermes router/plugin。

## How It Works

```mermaid
flowchart LR
  user["User<br/>Hermes chat"] --> slash["/qa-aist ..."]
  slash --> skill["Hermes SKILL.md"]
  skill --> dispatcher["qa_aist.hermes dispatcher"]
  dispatcher --> engine["QA-AIST engine"]
  engine --> next["next_actions<br/>guided menu"]
  next --> user
  engine --> cfg[".qa-aist.yaml"]
  engine --> ws[".qa-aist-project"]
  ws --> issues["issues mirror"]
  ws --> cases["case contracts"]
  ws --> evidence["evidence + state"]
  ws --> reports["reports"]
  engine --> gate["write gate"]
  gate --> gitea["Gitea wiki/issues/PR<br/>only on apply/submit-pr"]
```

```mermaid
flowchart TD
  A["issues sync"] --> B["cases generate --from-issues"]
  B --> C["cases review / Q&A"]
  C --> D["qa-test run-one/run"]
  D --> E["publish plan"]
  E --> F{"write gate"}
  F -->|allowed| G["publish apply"]
  F -->|blocked| H["fix config/evidence/contract"]
  D --> I["fix-issues plan/run"]
  I --> J["fix-issues submit-pr"]
```

## 5-minute Quick Start

假設 QA-AIST source checkout 在 `/root/repo/QA-AIST`，產品 repo 在 `/path/to/your-product`。

1. 安裝 Hermes skill：

```bash
cd /root/repo/QA-AIST
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes install-skill --force \
  --runner-command "/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes"
```

2. 在 Hermes 聊天室重掃 skills：

```text
/reload-skills
```

3. 在產品 repo 的 Hermes session 輸入：

```text
/qa-aist help
/qa-aist setup
/qa-aist doctor
```

4. 設定 `.qa-aist.yaml` 的 Gitea provider。

如果你要讓 QA-AIST 直接用 Gitea REST API 同步 issues、並在 gate 通過後執行 `publish apply` / `submit-pr`，用 HTTP backend，token 只放 env：

```yaml
tracker:
  provider: gitea
  gitea:
    backend: http
    base_url: "https://git.example.com"
    repo: "owner/repo"
    token_env: QA_AIST_GITEA_TOKEN
    wiki_page: "Test status"
    branch_prefix: "qa-aist/issue-"
```

如果你的 Hermes 已經有 Gitea MCP，QA-AIST 也支援 read-only MCP issue sync：Hermes 先用 Gitea MCP 讀 issues，將原始 JSON 寫入 `mcp_issues_json`，再呼叫 `/qa-aist issues sync`。這條路不需要 token，但只支援同步，不支援遠端寫入。

```yaml
tracker:
  provider: gitea
  gitea:
    backend: mcp
    repo: "owner/repo"
    mcp_issues_json: .qa-aist-project/state/gitea-mcp/issues.json
```

5. 跑完整新手流程：

```text
/qa-aist issues sync
/qa-aist issues dedupe
/qa-aist cases generate --from-issues
/qa-aist cases review
/qa-aist cases validate
/qa-aist qa-test list
/qa-aist qa-test run-one <case_id>
/qa-aist publish plan
/qa-aist publish apply
```

如果 `cases generate` 回傳 questions，Hermes 必須先用繁體中文問答補齊測試輸入、fixture、成功條件與副作用邊界，再把 draft case 當成可執行測試。

## Guided Interaction

QA-AIST 的 Hermes skill 不是被動轉貼 JSON。每次 `/qa-aist ...` 執行後，engine 會回傳 `next_actions`，Hermes 應該用繁體中文列出下一步選單，讓使用者回覆編號或直接輸入下一個指令。

範例：

```text
qa-aist> OK
         open_issues: 3

下一步可以選：
1. 檢查重複 issue：/qa-aist issues dedupe
2. 從 issues 產生測試 cases：/qa-aist cases generate --from-issues（需確認）
3. 查看 issue sync 狀態：/qa-aist issues status

請回覆選項編號，或直接輸入下一個 /qa-aist ... 指令。
```

互動原則：

- 安全的查詢類指令，例如 `status`、`doctor`、`issues status`、`qa-test list`，Hermes 可以主動提議立即執行。
- `status` 和 `doctor` 會提前檢查 issue sync readiness；如果 Gitea/MCP/token/snapshot 尚未準備好，會先顯示 blocker，不必等到 `issues sync` 才失敗。
- 會寫檔、跑測試、讀 Gitea MCP、publish、push branch、建立 PR 的動作，Hermes 必須先問使用者確認。
- `cases generate` 若產生問題，Hermes 要逐題用繁體中文問答補齊，不要亂猜。
- `publish apply` 和 `fix-issues submit-pr` 前，Hermes 必須摘要將寫入的目標與 gate 結果。

## Command Cheat Sheet

| 你想做的事 | Hermes command | 說明 |
|---|---|---|
| 看中文手冊 | `/qa-aist help` | 列出 workflow 與 topic help |
| 看 qa-test 教學 | `/qa-aist qa-test` 或 `/qa-aist help qa-test` | 解釋 case contract、run-one、evidence |
| 初始化產品 repo | `/qa-aist setup` | 建立 `.qa-aist.yaml` 與 `.qa-aist-project` |
| 健康檢查 | `/qa-aist doctor` | 檢查 config、paths、secret references |
| 同步 Gitea issues | `/qa-aist issues sync` | open issue 寫 mirror；closed issue 移出 active mirror |
| 看 issue 狀態 | `/qa-aist issues status` | 顯示 snapshot 與 mirror 數量 |
| 看單一 issue | `/qa-aist issues show <id>` | 印出本地 issue mirror |
| 檢查重複 issue | `/qa-aist issues dedupe` | 找疑似重複 active issue |
| 產生測試 cases | `/qa-aist cases generate --from-issues` | 由 issue mirror 產生 draft contract |
| 審查 draft | `/qa-aist cases review` | 顯示待問答問題 |
| 驗證 case YAML | `/qa-aist cases validate` | 確認可被 qa-test 讀取 |
| 列出測試 | `/qa-aist qa-test list` | 列出 case_id 與 commands |
| 預覽測試 | `/qa-aist qa-test dry-run` | 不執行，只產生 NOT_RUN result |
| 跑單一 case | `/qa-aist qa-test run-one <case_id>` | 最適合第一次除錯 |
| 跑全部 cases | `/qa-aist qa-test run` | 保存 stdout/stderr/rc/meta/result JSON |
| 產生發布計畫 | `/qa-aist publish plan` | 將 latest run 轉成 wiki/issues candidates 並跑 gate |
| 寫入 Gitea | `/qa-aist publish apply` | gate 全部通過且 token 存在才寫 wiki/issues |
| 修復前檢查 | `/qa-aist fix-issues plan --issue <id>` | 確認 sync、dedupe、open issue、case linkage |
| 修復 handoff | `/qa-aist fix-issues run --issue <id>` | 產生 Hermes 修碼 handoff |
| 建立 PR | `/qa-aist fix-issues submit-pr --issue <id>` | push branch 並用 Gitea API 建 PR |
| 報告 | `/qa-aist report status` | 產生 Markdown status report |

Legacy aliases:

| Legacy | Current |
|---|---|
| `/qa-aist sync-gitea pull` | `/qa-aist issues sync` |
| `/qa-aist sync-gitea status` | `/qa-aist issues status` |
| `/qa-aist find-new-issues run` | `/qa-aist publish plan` |
| `/qa-aist tracker plan-write` | 單一 write-gate 相容指令 |

## Project Layout

```text
your-product/
  .qa-aist.yaml
  .qa-aist-project/
    issues/       # Gitea open issue mirrors
    cases/        # YAML case contracts
    runners/      # project-specific runner scripts
    rules/        # SWQA rules copied from QA-AIST
    state/        # snapshots, latest-run.json, plans
      gitea-mcp/  # optional read-only MCP issue input snapshot
    evidence/     # stdout/stderr/rc/meta/result JSON
    reports/      # Markdown/JSON reports
```

`.qa-aist` 是工具本體；`.qa-aist-project` 是 host project runtime data。不要把 token、password、lab credentials、customer data 寫進 tool source 或 tracked config。

## Case Contract

最小 contract：

```yaml
case_id: CLI-HELP-001
title: CLI help can be rendered
source:
  provider: gitea
  issue_id: 123
commands:
  - id: help
    run: python3 -m your_package --help
    expected_exit_code: 0
```

必填欄位：

| Field | Required | Meaning |
|---|---:|---|
| `case_id` | yes | Stable test id |
| `title` | yes | Human-readable title |
| `commands[].id` | yes | Stable command id |
| `commands[].run` | yes | Shell command or runner path |
| `commands[].expected_exit_code` | yes | Expected return code |

`cases generate --from-issues` 產生的 draft 會附 `qa_aist.questions`。Hermes 要先問使用者並補齊測試需要的 runner、fixture、輸入檔、環境變數、成功條件、side-effect safe 邊界，再跑正式測試。

## Reports And Evidence

每次 `qa-test` 會產生：

- `<command>.stdout.log`
- `<command>.stderr.log`
- `<command>.rc`
- `<command>.meta`
- `result.json`

Normalized result 固定包含 `case_id`、`status`、`commands`、`evidence`、`contract_hash`、`started_at`、`ended_at`、`exit_code`。`close-loop run-once` 會寫 `.qa-aist-project/state/latest-run.json`，`report status` 會寫 `.qa-aist-project/reports/status.md`。

## Write Gate

QA-AIST 可以真實寫 Gitea，但只允許明確的 apply/submit-pr 流程：

| Gate condition | Result |
|---|---|
| closed issue write | blocked |
| duplicate issue candidate | blocked |
| stale or missing issue sync | blocked |
| contract drift | blocked |
| missing/current evidence | blocked |
| raw secret leakage | blocked |
| modifying another user's post | blocked |
| internal `.qa/`, run internals, prompt text leakage | blocked |
| tracker disabled or token missing | blocked |
| `tracker.gitea.backend: mcp` remote write | blocked |

Hermes 不可以自己組 Gitea comment、issue、wiki 或 PR API request。需要遠端寫入時，只能走：

```text
/qa-aist publish plan
/qa-aist publish apply
/qa-aist fix-issues submit-pr --issue <id>
```

## What This Is Not

- QA-AIST 不是讓 Hermes 任意拼 shell command 或 tracker action 的捷徑。
- QA-AIST 不會自動 reopen closed issues。
- QA-AIST 不會修改不是目前 actor 自己張貼的 Gitea post。
- QA-AIST 不會在沒有 sync、evidence、contract hash、write gate 的情況下寫遠端。
- QA-AIST 不是 native Hermes router；目前是 dynamic skill mediated flow。

## Developer / CI Usage

Hermes 背後使用同一個 deterministic engine。CI 或本機除錯可以直接跑：

```bash
PYTHONPATH=src python3 -m qa_aist.cli init-project --root /path/to/product
PYTHONPATH=src python3 -m qa_aist.cli issues sync --root /path/to/product --issues-json issues.json
PYTHONPATH=src python3 -m qa_aist.cli cases generate --root /path/to/product --from-issues
PYTHONPATH=src python3 -m qa_aist.cli qa-test run-one --root /path/to/product ISSUE-1
PYTHONPATH=src python3 -m qa_aist.cli publish plan --root /path/to/product
PYTHONPATH=src python3 -m qa_aist.cli fix-issues submit-pr --root /path/to/product --issue 1 --dry-run
```

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Security

- Store tokens in environment variables only, for example `QA_AIST_GITEA_TOKEN`.
- Do not paste raw secrets into `.qa-aist.yaml`, issue mirrors, case YAML, runner output, reports, PR bodies, or screenshots.
- If a secret leaks, revoke it first, then report through the project's security channel.

## Contributing

Issues and PRs are welcome. Good contributions include:

- provider adapters behind the existing write gate;
- test runners and contract examples;
- safer case generation templates;
- Hermes integration improvements;
- documentation that helps non-experts operate the lifecycle.

PRs should include tests for changed behavior and must not commit host project runtime data, credentials, customer data, or lab topology.

## Roadmap

- Gitea-first lifecycle hardening.
- Redmine/GitHub provider adapters.
- Native Hermes router/plugin integration.
- Richer SWQA case generation and review workflow.
- Wiki/report templates for different engineering audiences.

## FAQ

### Why does `/qa-aist qa-test` show help?

Because `qa-test` is a command group. Use `qa-test list`, `qa-test dry-run`, `qa-test run-one <case_id>`, or `qa-test run`.

### Why is `publish apply` blocked?

Usually because write gate blocked it, Gitea token env is missing, issue sync is stale/missing, evidence is missing, or tracker provider is disabled.

If `.qa-aist.yaml` uses `tracker.gitea.backend: mcp`, `publish apply` and `fix-issues submit-pr` are intentionally blocked. MCP backend is read-only in V1 and only feeds `/qa-aist issues sync`. Use `backend: http` plus `QA_AIST_GITEA_TOKEN` for real remote writes.

### Why did `/qa-aist issues sync` ask for an MCP JSON snapshot?

Your config uses `tracker.gitea.backend: mcp`. Hermes must use the configured Gitea MCP read tool to fetch issues, write the raw issue list to `.qa-aist-project/state/gitea-mcp/issues.json` or the path named by `QA_AIST_GITEA_MCP_ISSUES_JSON`, then rerun `/qa-aist issues sync`.

### Why does `qa-aist-hermes` not exist?

That console script exists only after package installation. From a source checkout, use:

```bash
PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes skill-status
```

### Can QA-AIST really write Gitea?

Yes, but only through `publish apply` and `fix-issues submit-pr`, after deterministic gates pass and `QA_AIST_GITEA_TOKEN` is available.
