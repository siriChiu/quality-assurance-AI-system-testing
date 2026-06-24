# Command Surface

AI Quality Pilot 的公開入口是 Hermes 聊天室中的 `/quality-pilot ...`。CLI 只是 Hermes 背後呼叫的 deterministic engine；CI 或本機除錯可以直接用同一組參數。

`/quality-pilot help` 是唯一 help 指令。不再支援子分類 help。

## Public Commands

```text
/quality-pilot help
/quality-pilot setup
/quality-pilot doctor

/quality-pilot issues sync
/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/quality-pilot issues status
/quality-pilot issues show <issue_id>
/quality-pilot issues fix --all
/quality-pilot issues fix --issue <id>
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

/quality-pilot report status
/quality-pilot report json
/quality-pilot tracker plan-write
/quality-pilot subagent status
/quality-pilot subagent configure
```

## Recommended Flow

```text
/quality-pilot setup
/quality-pilot doctor
/quality-pilot issues sync
/quality-pilot cases generate --init
/quality-pilot cases validate
/quality-pilot cases list
/quality-pilot cases run <case_id>
/quality-pilot cases run
/quality-pilot publish wiki status
/quality-pilot publish wiki apply
```

後續有新 issues、PR、latest run 或 reports 時，用：

```text
/quality-pilot cases generate --growing
```

Redmine issues 由 Hermes Redmine MCP 讀取 snapshot，再交給 AI Quality Pilot。`144780 144693` 只是 Redmine issue ID 範例；實際使用時可放任意多個 Redmine issue ID。

```text
/quality-pilot issues sync --redmine-issues 144780 144693
/quality-pilot cases generate --redmine-issues 144780 144693
```

## Command Groups

| Group | Commands | Purpose |
|---|---|---|
| root | `help`, `setup`, `doctor` | 看手冊、初始化、檢查 Gitea/Redmine MCP readiness |
| issues | `sync`, `status`, `show`, `fix` | 同步、去重、prune、修復 handoff、產品修復 PR |
| cases | `generate`, `review`, `validate`, `list`, `run`, `push-pr` | 產生與執行 test case contracts，依 failing case 建產品修復 PR |
| publish wiki | `status`, `plan`, `apply` | 狀態看板，只更新 Gitea Wiki，不建立 issue comment/issue/PR |
| close-loop | `status`, `run-once` | Observe/Normalize/Execute/Triage/Publish/Evolve/Prune health 與單輪流程 |
| report | `status`, `json` | 查看 Markdown/JSON 報告 |
| tracker | `plan-write` | 相容保留的單一 write-gate 檢查 |
| subagent | `status`, `configure` | 設定文字生成 subagent handoff，預設 Open WebUI |

## Cases

`cases generate` 必須指定模式。裸指令會回 `explicit_generation_mode_required`，Hermes 應引導使用者選：

```text
/quality-pilot cases generate --init
/quality-pilot cases generate --growing
/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
```

`--init` 是第一次導入產品時的全 repo SWQA 建案。它會掃 README、程式碼、package metadata、既有 cases/runners/rules，產生可執行的 side-effect-safe probes，覆蓋功能、正向、反向、邊界、invalid input、side-effect-safe、stress/timeout-risk。`--init` 預設就是快速且嚴謹的自主模式，不需要另外加 fast 參數。

`--count <n>` 是唯一正式的數量限制：

```text
/quality-pilot cases generate --init --count 5
```

`--growing` 是後續增量擴散。它會讀 repo signals、Gitea/local issues、Redmine imports、PR refs、latest run、reports、existing cases/runners/rules，產生新的 executable growth cases。

`--redmine-issues` 支援多個 Redmine issue ID，會直接用 Hermes Redmine MCP snapshot 產生 linked testcase contracts。它不負責建立 Gitea issue，也不產生 Gitea sync plan；如果要先把 Redmine ticket 記錄到 Gitea repo issues，請使用 `issues sync --redmine-issues`。

`cases run` 取代舊測試執行群組：

```text
/quality-pilot cases list
/quality-pilot cases run <case_id>
/quality-pilot cases run
```

## Issues

`issues sync` 內建 sync、dedupe、prune 與遠端 duplicate action plan。closed/resolved issue 以遠端為事實來源：本地 active mirror 會移除，不留言、不 reopen。

```text
/quality-pilot issues sync
/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/quality-pilot issues status
/quality-pilot issues show <issue_id>
```

`issues sync --redmine-issues ...` 會透過 Hermes Redmine MCP snapshot 解析多個 Redmine issue ID，同步本地 Redmine mirror，產生 gated `mcp_issue_write_request`，並由 Hermes Gitea MCP 在同一流程建立 Gitea issues。CLI engine 本身不保存 token，也不直接打 Gitea HTTP。

產品修復流程集中在 `issues fix`：

```text
/quality-pilot issues fix --issue 123
/quality-pilot issues fix --issue 123 --push-pr
/quality-pilot issues fix --all
```

`--push-pr` 只有在 preflight、linked cases/evidence、write gate 通過後才建立產品修復 PR。

## Publish Wiki

Wiki 是 AI Quality Pilot 的預設狀態看板。只保留三個公開指令：

```text
/quality-pilot publish wiki status
/quality-pilot publish wiki plan
/quality-pilot publish wiki apply
```

`apply` 只同步 Gitea Wiki，不建立 issue comments、不建立新 issue、不建立 PR。

AI Quality Pilot 不保存 Gitea token，也不直接用 HTTP 寫 Wiki。`publish wiki apply` 會在 gate 通過後回 `status: needs_mcp_apply` 與 gated `mcp_write_request`；Hermes 用 Gitea MCP 更新指定 Wiki page 後，把結果寫到 `payload.mcp_write_result_path` 並回覆使用者。不要再暴露第二個 completion command。

## Subagent

長文字候選稿可透過 subagent 產生，但 subagent 只產 candidate，不負責寫檔、建立 issue、更新 Wiki 或開 PR。

預設 provider 是 Open WebUI：

```text
https://172.17.20.220/
```

查看設定：

```text
/quality-pilot subagent status
```

補上或重建預設設定：

```text
/quality-pilot subagent configure
```

`setup` 與 `configure` 只會寫 endpoint、provider、任務 routing；`model`、`system_prompt`、`user_instructions`、各任務 `task_prompts` 會保留空白，讓使用者自行填寫。

## Removed Commands

被移除的指令不應偷偷轉址執行。Hermes 應呼叫 dispatcher，回報 `command_removed` 與 replacement。

| Removed | Replacement |
|---|---|
| `/quality-pilot status` | `/quality-pilot doctor` |
| `/quality-pilot config ...` | `/quality-pilot doctor` |
| `/quality-pilot qa-test list` | `/quality-pilot cases list` |
| `/quality-pilot qa-test run-one <case_id>` | `/quality-pilot cases run <case_id>` |
| `/quality-pilot qa-test run` | `/quality-pilot cases run` |
| `/quality-pilot issues dedupe` | `/quality-pilot issues sync` |
| `/quality-pilot fix-issues run --issue <id>` | `/quality-pilot issues fix --issue <id>` |
| `/quality-pilot fix-issues submit-pr --issue <id>` | `/quality-pilot issues fix --issue <id> --push-pr` |
| `/quality-pilot publish plan` | `/quality-pilot publish wiki plan` |
| `/quality-pilot publish apply` | `/quality-pilot publish wiki apply` |
| `/quality-pilot publish status` | `/quality-pilot publish wiki status` |
| `/quality-pilot sync-gitea ...` | `/quality-pilot issues sync` |
| `/quality-pilot find-new-issues ...` | `/quality-pilot cases generate --growing` |
| `/quality-pilot help <topic>` | `/quality-pilot help` |

Removed case-generation options:

| Removed | Replacement |
|---|---|
| `--generated_count` | `--count` |
| `--fast` | no longer needed; `--init` is autonomous high-standard mode |
| `--from-issues` | `--growing` |
| `--candidate-json` | not public; external sessions may analyze, but AI Quality Pilot owns case writing |

## Direct Engine Examples

From an installed package:

```bash
quality-pilot setup --root /path/to/product
quality-pilot doctor --root /path/to/product
quality-pilot issues sync --root /path/to/product
quality-pilot cases generate --root /path/to/product --init
quality-pilot cases generate --root /path/to/product --init --count 5
quality-pilot cases run --root /path/to/product CASE-001
quality-pilot publish wiki apply --root /path/to/product
quality-pilot subagent status --root /path/to/product
quality-pilot subagent configure --root /path/to/product
```

From a source checkout:

```bash
PYTHONPATH=src python3 -m quality_pilot.cli doctor --root /path/to/product
PYTHONPATH=src python3 -m quality_pilot.cli cases run --root /path/to/product CASE-001
```
