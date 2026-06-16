# Hermes Agent Install

這份文件描述 QA-AIST 目前已實作的 Hermes 整合方式：**Hermes dynamic skill**。

重要邊界：Hermes 不會因為你把某個 agent json 放進系統目錄就自動支援 `/qa-aist`。目前可驗證的安裝目標是：

```text
~/.hermes/skills/qa-aist/SKILL.md
```

Hermes 掃到這份 `SKILL.md` 後，agent 會依 skill 指示呼叫 QA-AIST dispatcher。這不是 native Hermes router，也不是 Python package autoload。

## Install From Source Checkout

假設 QA-AIST source checkout 在 `/root/repo/QA-AIST`：

```bash
cd /root/repo/QA-AIST
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes install-skill --force \
  --runner-command "/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes"
```

安裝後會產生：

```text
/root/.hermes/skills/qa-aist/SKILL.md
```

如果 checkout 路徑不同，請同步替換 `PYTHONPATH` 和 `--runner-command`。

## Check Install

```bash
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes skill-status
```

預期：

```json
{
  "status": "ok",
  "skill_path": "/root/.hermes/skills/qa-aist/SKILL.md",
  "skill_exists": true,
  "skill_valid": true,
  "command_prefix": "/qa-aist"
}
```

回到 Hermes 聊天室重掃 skill：

```text
/reload-skills
```

再確認：

```text
/qa-aist help
```

如果 `/qa-aist` 沒出現，先檢查：

- `~/.hermes/skills/qa-aist/SKILL.md` 是否存在。
- frontmatter 是否有 `name: qa-aist`。
- Hermes 是否已執行 `/reload-skills`。
- Hermes 是否使用不同 `$HERMES_HOME`。

## Public Commands

安裝後，Hermes 聊天室只應公開這些指令：

```text
/qa-aist help
/qa-aist setup
/qa-aist doctor

/qa-aist issues sync
/qa-aist issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/qa-aist issues status
/qa-aist issues show <issue_id>
/qa-aist issues fix --all
/qa-aist issues fix --issue <id>
/qa-aist issues fix --issue <id> --push-pr

/qa-aist cases generate --init
/qa-aist cases generate --init --count 5
/qa-aist cases generate --growing
/qa-aist cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/qa-aist cases review
/qa-aist cases validate
/qa-aist cases list
/qa-aist cases run
/qa-aist cases run <case_id>
/qa-aist cases push-pr
/qa-aist cases push-pr <case_id>

/qa-aist publish wiki status
/qa-aist publish wiki plan
/qa-aist publish wiki apply

/qa-aist close-loop status
/qa-aist close-loop run-once

/qa-aist report status
/qa-aist report json
/qa-aist tracker plan-write
```

被移除的舊命令必須由 dispatcher 回 `command_removed`，Hermes 不可偷偷轉址執行。

## Verify Dispatcher Directly

這一步不經 Hermes，確認 engine 能跑。請在產品 repo root 執行：

```bash
cd /path/to/your-product
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist setup
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist doctor
```

`/qa-aist setup` 會寫入 MCP-only 設定；不會寫 Gitea repo、base URL 或 token env：

```yaml
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
```

這只是設定 QA-AIST 與 Hermes MCP 的本地交接路徑；它不會修改 Hermes 的 MCP server 註冊。

Hermes 需要在執行 dispatcher 前把可用 MCP server 告訴 QA-AIST，例如：

```bash
QA_AIST_HERMES_MCP_SERVERS=gitea,redmine
```

或寫入：

```json
{
  "servers": ["gitea", "redmine"]
}
```

預設位置是 `.qa-aist-project/state/hermes-mcp/status.json`。如果缺 Gitea 或 Redmine MCP，`/qa-aist doctor` 會一開始就顯示。

## Gitea MCP Workflow

MCP backend 在 V1 只允許三件事：

1. 讀 Gitea issues snapshot，供 `/qa-aist issues sync` 使用。
2. 在 `/qa-aist issues sync --redmine-issues ...` gate 通過後，依 gated `mcp_issue_write_request` 建立新 Gitea issues。
3. 在 `/qa-aist publish wiki apply` gate 通過後，只更新設定中的 Wiki page。

### Issue Sync

當 `/qa-aist doctor` 或 `/qa-aist issues sync` 回 `gitea_mcp_snapshot_missing`：

1. Hermes 用已設定好的 Gitea MCP read tool 讀取 repo issues。
2. Hermes 把原始 JSON 寫到 `.qa-aist-project/state/gitea-mcp/issues.json`。
3. Hermes 再執行 `/qa-aist issues sync`。

QA-AIST 會負責 mirror、dedupe、prune、closed issue 移除、remote duplicate action plan。Hermes 不可以把 MCP read 當作 sync 完成。

### Wiki Apply

使用者執行：

```text
/qa-aist publish wiki apply
```

若 backend 是 MCP 且 gate 通過，QA-AIST 會回：

```json
{
  "status": "needs_mcp_apply",
  "mcp_write_request": {
    "schema": "qa-aist.gitea-mcp-wiki-write-request.v1",
    "operation": "gitea.wiki.update_page",
    "repo": null,
    "repo_source": "hermes_session",
    "page": "Test status (Siri)"
  },
  "mcp_write_result_path": ".qa-aist-project/state/gitea-mcp/wiki-write-result.json"
}
```

Hermes 接著在同一個 `/qa-aist publish wiki apply` 使用者流程裡：

1. 驗證 request schema、operation、repo、page。
2. 用 Gitea MCP 更新 request 指定的 Wiki page。
3. 將 MCP 結果 JSON 寫入 `mcp_write_result_path`。
4. 回覆使用者結果，建議 `/qa-aist publish wiki status`。

不要暴露第二個 completion 指令。Wiki apply 不可以建立 issue comment、建立 issue、建立 PR 或修改任意 Wiki page。唯一例外是 `/qa-aist issues sync --redmine-issues ...` 回傳 gated `mcp_issue_write_request` 時，Hermes 可以在同一流程用 Gitea MCP 建立新 issue；仍禁止 comment、edit、close/reopen issue 或建立 PR。

## Redmine MCP Workflow

Redmine V1 只走 Hermes Redmine MCP 讀取，不做 QA-AIST 內建遠端 adapter。

當使用者輸入：

```text
/qa-aist issues sync --redmine-issues 144780 144693
/qa-aist cases generate --redmine-issues 144780 144693
```

`144780 144693` 只是 Redmine issue ID 範例；可替換成任意多個 Redmine issue ID。

Hermes 應先用 Redmine MCP 讀取這些 ID，寫到 `.qa-aist-project/state/redmine-mcp/issues.json` 或 `QA_AIST_REDMINE_MCP_ISSUES_JSON` 指定路徑，再呼叫 dispatcher。

- `/qa-aist issues sync --redmine-issues ...` 會驗證 snapshot、同步 local Redmine mirrors、建立 gated `mcp_issue_write_request`，Hermes 在同一流程用 Gitea MCP 建立 Gitea issues。
- `/qa-aist cases generate --redmine-issues ...` 會直接用這些 Redmine IDs 產生 linked testcase contracts，不產生 Gitea plan。

`/qa-aist doctor` 會檢查 Redmine MCP snapshot path、最近讀取狀態與 issue id coverage。

## Expected Agent Behavior

Hermes agent 收到 `/qa-aist ...` 時必須：

1. 在產品 repo root 呼叫 dispatcher。
2. 讀 JSON。
3. 優先回覆 `chat_response`。
4. 把 `payload.next_actions` 呈現成繁中選單。
5. 若 `payload.hermes_needs_input.status == "required"`，呼叫 `clarify`，只問大分類阻擋問題，不逐一審每個 testcase。
6. 寫檔、跑測試、讀 MCP、寫 Wiki、push branch、建立 PR 前先取得使用者確認。

Dispatcher command shape:

```bash
/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist <arguments>
```

範例：

```bash
/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist doctor
/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist cases run CASE-001
```

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `qa-aist-hermes: command not found` | console script 沒安裝 | 用 `PYTHONPATH=... python3 -m qa_aist.hermes ...`，或裝到 venv/pipx。 |
| `/qa-aist` 不出現在 Hermes | Hermes 沒掃到 skill | 檢查 `~/.hermes/skills/qa-aist/SKILL.md` 並執行 `/reload-skills`。 |
| `config_not_found` | root 指到錯 repo 或尚未 setup | 回產品 repo root 執行 `/qa-aist setup`。 |
| `gitea_mcp_snapshot_missing` | MCP backend 尚未寫 issue snapshot | 用 Gitea MCP 讀 issues，寫入設定的 snapshot path，再跑 `/qa-aist issues sync`。 |
| `redmine_mcp_snapshot_missing` | Redmine MCP snapshot 尚未準備 | 用 Redmine MCP 讀指定 IDs，寫入 snapshot；要建立 Gitea issues 跑 `issues sync --redmine-issues`，要產 testcases 跑 `cases generate --redmine-issues`。 |
| `needs_mcp_apply` | Wiki gate 通過，等待 Hermes 用 Gitea MCP 寫 Wiki | 在同一個 apply 流程中更新指定 Wiki page，寫 result JSON，回報狀態。 |
| `command_removed` | 使用者輸入舊命令 | 顯示 replacement，不要偷偷轉址執行。 |
