# Hermes Agent Install

這份文件描述 AI Quality Pilot 目前已實作的 Hermes 整合方式：**Hermes dynamic skill**。

重要邊界：Hermes 不會因為你把某個 agent json 放進系統目錄就自動支援 `/quality-pilot`。目前可驗證的安裝目標是：

```text
~/.hermes/skills/quality-pilot/SKILL.md
```

Hermes 掃到這份 `SKILL.md` 後，agent 會依 skill 指示呼叫 AI Quality Pilot dispatcher。這不是 native Hermes router，也不是 Python package autoload。

## Install From Source Checkout

假設 AI Quality Pilot source checkout 在 `/root/repo/AI Quality Pilot`：

```bash
cd "/root/repo/AI Quality Pilot"
SRC="$PWD/src"
RUNNER="$HOME/.local/bin/quality-pilot-hermes"
mkdir -p "$(dirname "$RUNNER")"
cat > "$RUNNER" <<SH
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$SRC\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m quality_pilot.hermes "\$@"
SH
chmod +x "$RUNNER"
PYTHONPATH="$SRC" python3 -m quality_pilot.hermes install-skill --force --runner-command "$RUNNER"
```

安裝後會產生：

```text
/root/.hermes/skills/quality-pilot/SKILL.md
```

如果 checkout 路徑不同，請在該 checkout 重新執行上方命令；wrapper 會把實際 `src` 路徑固定寫入。

## Check Install

```bash
PYTHONPATH="/root/repo/AI Quality Pilot/src" python3 -m quality_pilot.hermes skill-status
```

預期：

```json
{
  "status": "ok",
  "skill_path": "/root/.hermes/skills/quality-pilot/SKILL.md",
  "skill_exists": true,
  "skill_valid": true,
  "command_prefix": "/quality-pilot"
}
```

回到 Hermes 聊天室重掃 skill：

```text
/reload-skills
```

`/reload-skills` 只刷新 skill 檔案與 skill map；若是剛安裝或更新 Hermes 的
slash completion integration，還要退出並重新啟動目前的 Hermes CLI/TUI
process，讓 Python completer 載入新程式。背景 gateway 若由 systemd 管理，也
需要一併重啟；重掃 skill 本身不會 hot-reload 已執行中的 process。

`install-skill` 也會在 Hermes skills directory 缺少 `grill-me` 時植入最小
companion `SKILL.md`；若使用者已經有自己的 `grill-me`，安裝器不會覆寫它。

重掃並重啟後，在輸入框鍵入 `/quality-pilot` 尚未按 Enter 時，Hermes 會顯示輸入期下拉補全。第一列是裸指令總覽，後面可沿著 nested tree 繼續選擇 `cases generate`、`publish wiki status`、`close-loop heartbeat`，leaf command 也會提示 `--init`、`--growing` 等安全選項。這個選單來自 `SKILL.md` 的 `metadata.hermes.completion`，不是 dispatcher 回傳後的 `next_actions`。若舊版 Hermes 只顯示 `/quality-pilot` 名稱而沒有子功能，需更新 Hermes 的 slash completer；本環境目前已在 `/usr/local/lib/hermes-agent` 套用這項 completion integration，日後更新 Hermes 時需保留同等支援。

只有生產型 `/quality-pilot` workflow 會強制執行已安裝的 `grill-me` companion：
`setup`、`doctor --fix`、`environment configure`、`issues sync/create-from-failure/fix`、所有 `cases generate ...`、`cases push-pr`、
`publish wiki plan/apply`、所有 `close-loop ...`、`tracker plan-write` 與
`subagent configure`。使用者不需要另外輸入 `/grill-me`；Hermes 會自行完成訪談、等待回答
後才呼叫 dispatcher。status/list/report/validate、Wiki status、issue show 與已明確的
`cases run <case_id>` 不會觸發 gate。若 production scope 缺少 companion，流程會停止並
回報 `grill_me_required`，不會靜默改走一般 clarify。

再確認：

```text
/quality-pilot help
```

如果 `/quality-pilot` 沒出現，先檢查：

- `~/.hermes/skills/quality-pilot/SKILL.md` 是否存在。
- frontmatter 是否有 `name: quality-pilot`。
- Hermes 是否已執行 `/reload-skills`。
- 若 completion 程式剛更新，是否已退出並重新啟動 Hermes CLI/TUI。
- Hermes 是否使用不同 `$HERMES_HOME`。

## Public Commands

安裝後，Hermes 聊天室只應公開這些指令：

```text
/quality-pilot help
/quality-pilot setup
/quality-pilot doctor
/quality-pilot doctor --fix
/quality-pilot environment status
/quality-pilot environment configure --mode <local|remote>
/quality-pilot audit state

/quality-pilot issues sync
/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/quality-pilot issues status
/quality-pilot issues report
/quality-pilot issues create-from-failure --local --case <case_id>
/quality-pilot issues create-from-failure --remote --case <case_id>
/quality-pilot issues create-from-failure --local --all
/quality-pilot issues create-from-failure --remote --all
/quality-pilot issues show <issue_id>
/quality-pilot issues fix --all
/quality-pilot issues fix --issue <id>
/quality-pilot issues fix --case <case_id>
/quality-pilot issues fix --issue <id> --push-pr

/quality-pilot cases generate --init
/quality-pilot cases generate --init --count 5
/quality-pilot cases generate --growing
/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/quality-pilot cases review
/quality-pilot cases validate
/quality-pilot cases list
/quality-pilot cases run
/quality-pilot cases run <case_id>
/quality-pilot cases push-pr
/quality-pilot cases push-pr <case_id>

/quality-pilot publish wiki status
/quality-pilot publish wiki plan
/quality-pilot publish wiki apply

/quality-pilot close-loop status
/quality-pilot close-loop run-once
/quality-pilot close-loop heartbeat

/quality-pilot report status
/quality-pilot report json
/quality-pilot tracker plan-write
/quality-pilot subagent status
/quality-pilot subagent configure
```

被移除的舊命令必須由 dispatcher 回 `command_removed`，Hermes 不可偷偷轉址執行。

## Verify Dispatcher Directly

這一步不經 Hermes，確認 engine 能跑。請在產品 repo root 執行：

```bash
cd /path/to/your-product
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot setup
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot doctor
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot doctor --fix
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot audit state
```

`/quality-pilot setup` 會寫入 MCP-only 設定；不會寫 Gitea repo、base URL 或 token env：

```yaml
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
```

這只是設定 AI Quality Pilot 與 Hermes MCP 的本地交接路徑；它不會修改 Hermes 的 MCP server 註冊。

Hermes 需要在執行 dispatcher 前把可用 MCP server 告訴 AI Quality Pilot，例如：

```bash
QUALITY_PILOT_HERMES_MCP_SERVERS=gitea,redmine
```

或寫入：

```json
{
  "servers": ["gitea", "redmine"]
}
```

預設位置是 `.quality-pilot-project/state/hermes-mcp/status.json`。如果缺 Gitea 或 Redmine MCP，`/quality-pilot doctor` 會一開始就顯示。

## Gitea MCP Workflow

MCP backend 在 V1 只允許五件事：

1. 讀 Gitea issues snapshot，供 `/quality-pilot issues sync` 使用。
2. 在 `/quality-pilot issues sync --redmine-issues ...` gate 通過後，依 gated `mcp_issue_write_request` 建立或更新 linked Gitea issues。
3. 在明確的 `/quality-pilot issues create-from-failure --remote ...` gate 通過後，依 gated `mcp_issue_write_request` 建立由 official FAIL/BLOCK 產生的新 Gitea issue；`--local` 永遠不建立遠端 request。
4. 在 `/quality-pilot issues report` gate 通過後，更新 linked FAIL/BLOCK evidence。
5. 在 `/quality-pilot publish wiki apply` gate 通過後，只更新設定中的 Wiki page。

### Issue Sync

當 `/quality-pilot doctor` 或 `/quality-pilot issues sync` 回 `gitea_mcp_snapshot_missing`：

1. Hermes 用已設定好的 Gitea MCP read tool 讀取 repo issues。
2. Hermes 把原始 JSON 寫到 `.quality-pilot-project/state/gitea-mcp/issues.json`。
3. Hermes 再執行 `/quality-pilot issues sync`。

AI Quality Pilot 會負責 mirror、dedupe、prune、closed issue 移除、remote duplicate action plan。Hermes 不可以把 MCP read 當作 sync 完成。

### Failed Case to Issue

`/quality-pilot issues report` 會同時顯示 linked evidence 與沒有 tracker mapping 的 standalone official FAIL/BLOCK。若輸出有 `standalone_issue_create_candidate_count > 0`，使用：

```text
/quality-pilot issues create-from-failure --local --case <case_id>
/quality-pilot issues create-from-failure --remote --case <case_id>
/quality-pilot issues create-from-failure --local --all
/quality-pilot issues create-from-failure --remote --all
```

`--local` 只產生本地完整技術報告；`--remote` 會先同時產生這份本地報告，再建立 gated `mcp_issue_write_request`。Hermes 必須確認每個 action 的 `operation: gitea.issue.create`、`action_safety_class: new_issue_from_failed_case` 與 `write_gate_result.allowed: true`，並確認 body 是不含 credentials、token、工作站路徑、內部 run identifier 的完整 SWQA 報告，再用 Gitea MCP 建立 issue，將結果寫入 `mcp_issue_write_result_path`。完成後重跑 `/quality-pilot issues sync`。`partial_probe` 預設排除，只有明確加 `--include-partial` 才納入。

後續修復使用同一個通用入口：remote Gitea work item 使用 issue ID；local failure work item 使用 testcase ID，兩者分別位於 `.quality-pilot-project/issues/remote/` 與 `.quality-pilot-project/issues/local/`，由 `/quality-pilot issues fix --issue <id>` 或 `/quality-pilot issues fix --case <case_id>` 消費。

### Wiki Apply

使用者執行：

```text
/quality-pilot publish wiki apply
```

若 backend 是 MCP 且 gate 通過，AI Quality Pilot 會回：

```json
{
  "status": "needs_mcp_apply",
  "mcp_write_request": {
    "schema": "quality-pilot.gitea-mcp-wiki-write-request.v1",
    "operation": "gitea.wiki.update_page",
    "repo": null,
    "repo_source": "hermes_session",
    "page": "Quality Pilot Test Status"
  },
  "mcp_write_result_path": ".quality-pilot-project/state/gitea-mcp/wiki-write-result.json"
}
```

Hermes 接著在同一個 `/quality-pilot publish wiki apply` 使用者流程裡：

1. 驗證 request schema、operation、repo、page。
2. 用 Gitea MCP 更新 request 指定的 Wiki page。
3. 將 MCP 結果 JSON 寫入 `mcp_write_result_path`。
4. 回覆使用者結果，建議 `/quality-pilot publish wiki status`。

不要暴露第二個 completion 指令。Wiki apply 不可以建立 issue comment、建立 issue、建立 PR 或修改任意 Wiki page。`/quality-pilot issues sync --redmine-issues ...` 與明確的 `/quality-pilot issues create-from-failure --remote ...` 回傳 gated `mcp_issue_write_request` 時，Hermes 可以在同一流程用 Gitea MCP 建立或更新 linked/failed-case issue；`--local` 完全不進入 MCP 寫入流程。仍禁止任意 comment、edit、close/reopen issue 或建立 PR。

## Redmine MCP Workflow

Redmine V1 只走 Hermes Redmine MCP 讀取，不做 AI Quality Pilot 內建遠端 adapter。

當使用者輸入：

```text
/quality-pilot issues sync --redmine-issues 144780 144693
/quality-pilot cases generate --redmine-issues 144780 144693
```

`144780 144693` 只是 Redmine issue ID 範例；可替換成任意多個 Redmine issue ID。

Hermes 必須先用 Redmine MCP **live read** 這些 ID，不能只因為本地 snapshot 已存在同一個 ID 就直接呼叫 dispatcher。寫到 `.quality-pilot-project/state/redmine-mcp/issues.json` 或 `QUALITY_PILOT_REDMINE_MCP_ISSUES_JSON` 指定路徑時，snapshot 必須是 manifest JSON：

```json
{
  "schema": "quality-pilot.redmine-mcp-issues.v1",
  "source": "hermes_redmine_mcp_live_read",
  "fetched_at": "2026-06-24T09:20:00Z",
  "requested_issue_ids": [144780, 144693],
  "include": ["description", "custom_fields", "journals", "attachments"],
  "payload_completeness": "full",
  "issues": []
}
```

`issues[]` 必須保留 live Redmine detail payload：完整 description、`updated_on`、`custom_fields`、`journals` 或 `comments`、`attachments`。若 MCP list 結果只有摘要，Hermes 要再對每個 requested ID 呼叫 detail/read issue，補齊後才寫 snapshot。AI Quality Pilot 會拒絕 legacy/raw/精簡 snapshot，避免舊資料覆蓋較新的 Redmine issue。

- `/quality-pilot issues sync --redmine-issues ...` 會驗證 snapshot、同步 local Redmine mirrors、建立 gated `mcp_issue_write_request`，Hermes 在同一流程用 Gitea MCP 建立或更新 linked Gitea issues。
- `/quality-pilot cases generate --redmine-issues ...` 會直接用這些 Redmine IDs 產生 linked testcase contracts，不產生 Gitea plan。

`/quality-pilot doctor` 會檢查 Redmine MCP snapshot path、最近讀取狀態與 issue id coverage。

## Expected Agent Behavior

Hermes agent 收到 `/quality-pilot ...` 時必須：

1. 在產品 repo root 呼叫 dispatcher。
2. 讀 JSON。
3. 優先回覆 `chat_response`。
4. 把 `payload.next_actions` 呈現成繁中選單。
5. 若 `payload.hermes_needs_input.status == "required"`，呼叫 `clarify`，只問大分類阻擋問題，不逐一審每個 testcase。
6. 使用者明確輸入 `/quality-pilot ...` 後，可自動讀 MCP、寫本地 overlay state、執行已驗證 side-effect-safe 的 case；遠端寫入、Wiki apply、push branch、建立 PR、或可能碰觸外部資源的測試仍必須經 AI Quality Pilot gate 與必要確認。
7. 長文字候選稿可透過 `/quality-pilot subagent status` 所設定的 subagent 產生；Open WebUI endpoint 與 model 都由 deployment owner 提供，core 不預設 private network 位址。API key 僅能以 `api_key_env` 指向環境變數。Subagent 只能回 candidate text/JSON，不可直接寫檔、建立 issue、更新 Wiki 或開 PR。

Dispatcher command shape:

```bash
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot <arguments>
```

範例：

```bash
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot doctor
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot doctor --fix
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot cases run CASE-001
~/.local/bin/quality-pilot-hermes --root "$PWD" /quality-pilot subagent status
```

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `quality-pilot-hermes: command not found` | skill 指到未安裝的 console script | 依本文件重新建立 `~/.local/bin/quality-pilot-hermes` wrapper，並用 `install-skill --force --runner-command "$HOME/.local/bin/quality-pilot-hermes"` 重裝 skill，再執行 `/reload-skills`。 |
| `/quality-pilot` 不出現在 Hermes | Hermes 沒掃到 skill | 檢查 `~/.hermes/skills/quality-pilot/SKILL.md` 並執行 `/reload-skills`。 |
| `config_not_found` | root 指到錯 repo 或尚未 setup | 回產品 repo root 執行 `/quality-pilot setup`。 |
| `gitea_mcp_snapshot_missing` | MCP backend 尚未寫 issue snapshot | 用 Gitea MCP 讀 issues，寫入設定的 snapshot path，再跑 `/quality-pilot issues sync`。 |
| `redmine_mcp_snapshot_missing` / `redmine_mcp_snapshot_unverified` / `redmine_mcp_snapshot_incomplete_payload` | Redmine MCP snapshot 缺失、舊格式、或不是 full live-read payload | 用 Redmine MCP live read 指定 IDs，寫入 `quality-pilot.redmine-mcp-issues.v1` manifest；要建立或更新 linked Gitea issues 跑 `issues sync --redmine-issues`，要產 testcases 跑 `cases generate --redmine-issues`。 |
| `needs_mcp_apply` | Wiki gate 通過，等待 Hermes 用 Gitea MCP 寫 Wiki | 在同一個 apply 流程中更新指定 Wiki page，寫 result JSON，回報狀態。 |
| `command_removed` | 使用者輸入舊命令 | 顯示 replacement，不要偷偷轉址執行。 |
