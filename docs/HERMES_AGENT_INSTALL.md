# Hermes Agent Install

這份文件只描述目前 QA-AIST 已實作、已驗證的 Hermes 整合方式：**Hermes dynamic skill**。

重要更正：Hermes 不會因為你把 `qa-aist.agent.json` 或 shell wrapper 放進 `/usr/local/lib/hermes-agent/agent` 就自動產生 `/qa-aist`。在這台 Hermes 環境裡，dynamic slash commands 來自：

```text
~/.hermes/skills/**/SKILL.md
```

所以 QA-AIST 的實際安裝目標是：

```text
~/.hermes/skills/qa-aist/SKILL.md
```

## What This Enables

安裝後，Hermes skill scanner 可以看到 `/qa-aist`，使用者可以在 Hermes 聊天室輸入：

```text
/qa-aist help
/qa-aist status
/qa-aist doctor
/qa-aist issues sync
/qa-aist cases generate --from-issues
/qa-aist qa-test list
/qa-aist qa-test
/qa-aist publish plan
/qa-aist publish apply
/qa-aist fix-issues plan --issue 123
/qa-aist fix-issues submit-pr --issue 123
```

邊界請講清楚：這是 **skill-mediated flow**。Hermes 會把 `/qa-aist ...` 轉成 skill invocation，agent 再依 `SKILL.md` 的指示執行 QA-AIST dispatcher。這不是 native Hermes router，也不是 LLM 前置的 deterministic command hook。

QA-AIST 可以真實寫 Gitea，但只有在 `/qa-aist publish apply` 或 `/qa-aist fix-issues submit-pr` 明確執行、且 deterministic write gate 通過、且 token env 存在時才會碰遠端。Hermes 不可以自己用 curl/API 繞過 QA-AIST。

如果 Hermes 環境已經配置 Gitea MCP，QA-AIST 的正確用法仍然是 `/qa-aist issues sync`。差別只是 issue 來源改成 read-only MCP snapshot：

1. `.qa-aist.yaml` 設定 `tracker.gitea.backend: mcp`。
2. Hermes agent 依 `SKILL.md` 用 Gitea MCP 讀取 issues。
3. Hermes 將原始 issue JSON 寫入 `.qa-aist-project/state/gitea-mcp/issues.json`，或 `QA_AIST_GITEA_MCP_ISSUES_JSON` 指定的路徑。
4. Hermes 再執行 `/qa-aist issues sync`，由 QA-AIST 產生 mirror、snapshot 和後續 gate inputs。

MCP backend 在 V1 是 read-only。Hermes 不可以用 Gitea MCP 直接新增 issue comment、更新 wiki 或建立 PR；遠端寫入仍必須走 QA-AIST HTTP backend、write gate、`publish apply` / `submit-pr`。

## Install From Source Checkout

如果 QA-AIST source checkout 在 `/root/repo/QA-AIST`，請執行：

```bash
cd /root/repo/QA-AIST
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes install-skill --force \
  --runner-command "/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes"
```

這會寫入：

```text
/root/.hermes/skills/qa-aist/SKILL.md
```

若你的 QA-AIST checkout 路徑不同，請同步替換兩個地方：

```bash
PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes install-skill --force \
  --runner-command "/usr/bin/env PYTHONPATH=/path/to/QA-AIST/src python3 -m qa_aist.hermes"
```

## Check Install Status

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

接著回到 Hermes 聊天室輸入：

```text
/reload-skills
```

先用不需要專案初始化的 help 指令確認 skill 真的可用：

```text
/qa-aist help
```

## Verify Hermes Can See `/qa-aist`

可以用 Hermes 自己的 skill scanner 驗證：

```bash
PYTHONPATH=/usr/local/lib/hermes-agent python3 - <<'PY'
from agent.skill_commands import scan_skill_commands
cmds = scan_skill_commands()
print("/qa-aist" in cmds)
print(cmds.get("/qa-aist"))
PY
```

第一行應該是：

```text
True
```

如果是 `False`，優先檢查：

- `~/.hermes/skills/qa-aist/SKILL.md` 是否存在。
- `SKILL.md` frontmatter 是否有 `name: qa-aist`。
- Hermes 聊天室是否已執行 `/reload-skills`。
- Hermes 是否使用不同的 `$HERMES_HOME`。

## Verify QA-AIST Dispatcher Directly

這一步不經 Hermes，單純確認 QA-AIST engine 能跑。請在產品 repo root 執行，不要在 QA-AIST source repo 執行：

```bash
cd /path/to/your-product
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist doctor
```

如果產品 repo 還沒初始化：

```bash
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist setup
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist doctor
```

## Why `qa-aist-hermes` May Not Exist

`qa-aist-hermes` 是 Python package 安裝後才會出現的 console script。如果你只是 clone repo，它不會自動存在。

Ubuntu 24.04 可能會拒絕系統層級 `pip install .`，並顯示 `externally-managed-environment`。這時不要硬改 system Python。請使用其中一種方式：

```bash
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes skill-status
```

或使用 venv/pipx 安裝後再呼叫：

```bash
qa-aist-hermes skill-status
qa-aist-hermes install-skill --force
```

文件中的 fallback 一律以 `PYTHONPATH=... python3 -m qa_aist.hermes ...` 為準，避免使用者以為 console script 一定存在。

## What The Skill Actually Tells Hermes Agent To Do

`SKILL.md` 不會自己執行 Python。它是 Hermes agent 的操作規則。當使用者輸入：

```text
/qa-aist help qa-test
```

agent 應該在目前產品 repo root 執行：

```bash
/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist help qa-test
```

然後讀取 JSON，優先回覆 JSON 裡的 `chat_response`。

`chat_response` 會包含給使用者的互動選單；payload 也會包含 `next_actions`。agent 不應該只機械式轉貼 JSON，應該用繁體中文帶使用者完成下一步：

```text
下一步可以選：
1. 執行健康檢查：/qa-aist doctor
2. 同步 Gitea issues：/qa-aist issues sync（需確認）
3. 查看 qa-test 教學：/qa-aist help qa-test
```

互動規則：

- 使用者回覆 `1`、`2`、`3` 時，執行對應的 `next_actions[].command`。
- 安全查詢類動作可主動詢問「要我現在跑嗎？」。
- `/qa-aist status` 和 `/qa-aist doctor` 會提前檢查 issue sync readiness。若看到 `tracker_provider_disabled`、`gitea_mcp_snapshot_missing` 或 `gitea_http_token_missing`，先依選單補齊設定，不要等到 `issues sync` 才處理。
- 寫檔、跑測試、Gitea MCP 讀取、publish、push branch、建立 PR 都要先取得確認。
- `next_actions[].requires_confirmation: true` 時，不可直接執行。
- 若 `next_actions` 的 kind 是 `ask_user` 或結果內有 questions，逐題用繁體中文詢問，不要自行猜測。

若產品 repo 的 `.qa-aist.yaml` 包含：

```yaml
tracker:
  provider: gitea
  gitea:
    backend: mcp
    repo: "owner/repo"
    mcp_issues_json: .qa-aist-project/state/gitea-mcp/issues.json
```

agent 在執行 `/qa-aist issues sync` 前，可以使用 Hermes 已安裝的 Gitea MCP 讀取 issues，並只把讀到的 JSON 寫入 `mcp_issues_json`。之後仍然必須呼叫 QA-AIST dispatcher，不可以直接把 MCP 結果當成 QA-AIST sync 完成。

如果 Hermes skill 有觸發，但 agent 只用文字回答、沒有執行 terminal command，這代表 skill-mediated flow 沒被 agent 遵守。請要求 agent 依 `~/.hermes/skills/qa-aist/SKILL.md` 呼叫 QA-AIST dispatcher。

## Native Router Is A Separate Future Integration

若你要的是「使用者輸入 `/qa-aist doctor` 後，Hermes 在 LLM 之前直接執行 QA-AIST」，那不是目前的 dynamic skill 模式。需要在 Hermes 本體或 plugin router 加上類似：

```python
from qa_aist.hermes import dispatch_chat_command

result = dispatch_chat_command("/qa-aist doctor", root=session.project_root)
return result["chat_response"]
```

這條路徑目前尚未實作。現階段可交付、可驗證的是 Hermes dynamic skill。

## Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| `qa-aist-hermes: command not found` | Python console script 沒安裝 | 用 `PYTHONPATH=... python3 -m qa_aist.hermes ...`，或安裝到 venv/pipx。 |
| `/qa-aist` 不出現在 Hermes | Hermes 沒掃到 skill | 確認 `~/.hermes/skills/qa-aist/SKILL.md`，執行 `/reload-skills`。 |
| scanner 顯示 `False` | skill path/frontmatter/HERMES_HOME 不對 | 檢查 `name: qa-aist` 與 Hermes 的 skills 目錄。 |
| `/qa-aist` 有觸發但沒跑測試 | agent 沒遵守 SKILL | 要求 agent 依 SKILL 執行 dispatcher terminal command。 |
| `config_not_found` | root 指到錯的 repo 或尚未 setup | 回到產品 repo root，執行 `/qa-aist setup`。 |
| `tracker_disabled` | provider 未啟用 | 設定 `tracker.provider: gitea` 與 `tracker.gitea.*`。 |
| `gitea_not_configured` | apply/submit-pr 缺 token 或 repo 設定 | 設定 `QA_AIST_GITEA_TOKEN` 與 `.qa-aist.yaml`。 |
| `gitea_mcp_snapshot_missing` | `backend: mcp` 但沒有 issue snapshot | 用 Hermes Gitea MCP 讀 issues 並寫入 `.qa-aist-project/state/gitea-mcp/issues.json`，或設定 `QA_AIST_GITEA_MCP_ISSUES_JSON`。 |
| `gitea_mcp_write_not_supported` | MCP backend 嘗試寫遠端 | V1 MCP 只支援 issue sync 讀取；真實寫入請改 HTTP backend/token。 |
| `write_gate_blocked` | QA-AIST 拒絕遠端寫入 | 先修 sync/evidence/contract/duplicate/secret 問題，不要繞過 gate。 |
