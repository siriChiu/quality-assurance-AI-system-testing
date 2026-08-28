# Command Surface

AI Quality Pilot 的公開入口是 Hermes 聊天室中的 `/quality-pilot ...`。CLI 只是 Hermes 背後呼叫的 deterministic engine；CI 或本機除錯可以直接用同一組參數。

直接輸入 `/quality-pilot` 不需要再補 `help`：Hermes 會開啟繁中總覽，並自動顯示最多四個依目前狀態排序的建議功能。選單中的唯讀動作可直接執行；涉及寫檔、測試、MCP、Wiki、issue 或 PR 的項目會標示「需確認」。

在 Hermes 輸入框中，輸入 `/quality-pilot` 尚未按 Enter 時也會出現即時下拉補全：第一列保留裸指令的總覽入口，輸入空格後由 skill-owned nested completion tree 列出 `help`、`setup`、`doctor`、`environment`、`issues`、`cases`、`publish`、`close-loop`、`report`、`tracker` 與 `subagent`。可繼續補全 `/quality-pilot cases generate`、`/quality-pilot publish wiki status`、`/quality-pilot close-loop heartbeat`，leaf command 後也會提示安全選項如 `--init`、`--growing`。這些只是 UI suggestion，不是 native Hermes subcommands 或授權；按 Enter 後仍由 Quality Pilot dispatcher 驗證整條指令。
`environment` 也包含在同一棵 nested completion tree：輸入 `/quality-pilot environment` 後會建議 `status`、`preflight` 與 `configure`，並提示 `--mode local|remote`、`--entrypoint`、`--fixture`、`--expected-head-sha` 等欄位。

注意：`/reload-skills` 只會重新掃描 `SKILL.md` 與 skill map，不會重新載入已在記憶體中的 Hermes Python slash completer。若剛更新 Hermes completion integration，必須退出並重新啟動目前的 Hermes CLI/TUI process；只執行 `/reload-skills` 仍會看到舊的單一指令補全。

只有生產型 `/quality-pilot` 指令會由 Hermes 系統級提示強制先執行已安裝的
`grill-me` companion：`setup`、`doctor --fix`、`environment configure/preflight`、`issues sync/create-from-failure/fix`、`review pr/apply`、所有
`cases generate ...`、`cases push-pr`、`publish wiki plan/apply`、所有
`close-loop ...`、`tracker plan-write` 與 `subagent configure`。這不是建議，也不需要
使用者另外輸入 `/grill-me`；Hermes 必須完成訪談、等待回答，再把答案帶入 dispatcher。
`status`、`list`、`report`、`validate`、Wiki status、issue show，以及已明確的
`cases run <case_id>` 不會觸發 gate。缺少 companion 時必須停止並回報
`grill_me_required`，不能靜默退回一般 clarify 流程。

首次 `/quality-pilot setup` 的順序是 `grill-me -> setup -> reconcile repo analysis -> environment configure -> environment status`；先收集使用者已知的 local/remote、入口、fixture、credential env 名稱與副作用邊界，setup/doctor 完成 repo 分析後，再補問真正缺失的入口或 fixture。Hermes 應沿用同一輪答案，不要在同一回合重複 grill-me。這樣不會用 repo 推測冒充「已取得測試環境授權」。

`/quality-pilot help` 是唯一 help 指令。不再支援子分類 help。

命令是否出現在 help，不代表對應的完整 autonomous agent module 已完成。
使用前先看 [Capability Matrix](CAPABILITY_MATRIX.md)：目前可獨立呼叫多個
workflow entry points，但 resumable A0-A8 module session 仍是 Partial。

## Public Commands

```text
/quality-pilot
/quality-pilot help
/quality-pilot setup
/quality-pilot doctor
/quality-pilot doctor --fix
/quality-pilot environment status
/quality-pilot environment preflight
/quality-pilot environment configure --mode <local|remote>
/quality-pilot audit state
/quality-pilot review pr --repo <owner/repo> --pr-number <number>
/quality-pilot review pr --repo <owner/repo> --pr-number <number> --confirm-discovery
/quality-pilot review apply

/quality-pilot issues sync
/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/quality-pilot issues status
/quality-pilot issues report
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
/quality-pilot close-loop heartbeat

/quality-pilot report status
/quality-pilot report json
/quality-pilot tracker plan-write
/quality-pilot subagent status
/quality-pilot subagent configure
```

## Intent-based happy paths

不要把所有公開命令當成一條必跑 checklist。`close-loop run-once` 與 heartbeat 預設使用
Task Graph；run-once 第一次會在 local publication gate 回 `HOLD`，再用
`--resume-task-graph --confirm-publish` 繼續。只有明確指定 `--legacy` 才會選擇舊
fixed-sequence pipeline。先選使用目的，執行最短路徑，
再依 dispatcher 的 `next_actions` 繼續。`doctor --fix` 只在 setup 尚未完成或
`doctor` 建議修復 safe skeleton 時使用；不需要每次同時執行 `doctor` 和
`doctor --fix`。

### PR review path

```text
/quality-pilot review pr --repo <owner/repo> --pr-number <number>
```

這個命令會針對 pinned branch/PR head 建立 detached worktree；若 MCP snapshot
只有 changed files 沒有 diff，會從 pinned base/head commits 重建 diff。它會選擇
repository regression suite，並實際執行測試。若 tests 使用 pytest，會先在 disposable
review worktree 建立 `<review-worktree>/.venv`，再使用 `.venv/bin/python -m pytest tests -q`；
不得把 Quality Pilot checkout、host product repo 或 `/usr/bin/python3` 當成 local review
pytest interpreter。`python3 -m venv .venv` 只負責建立隔離環境；pip、Playwright 與 pytest
都必須透過 worktree venv 執行。Remote product/Browser pytest 若另行執行，必須標記為 remote
product checkout evidence，不得混入 local disposable regression evidence；否則使用 bounded unittest
discovery。預設也會透過 case-generation/case-run 模組建立 PR-scoped QA matrix，
涵蓋 black-box、white-box、functional、boundary、stress、documentation；
`--diff-only` 才跳過這個 comprehensive path。Regression pytest 有獨立 timeout boundary：
預設 targeted suite 最多 600 秒、full suite 最多 900 秒；可用 `--test-timeout <seconds>`
提高共同上限。這不會把 timeout 當成 dependency missing，仍會回 `BLOCK`/`FAIL` 並保存
exit code 124 或實際測試結果。Comprehensive review 會依 changed files
建立 `diff_targeted_oracle`；只有實際對應的 product test command 成功執行才會成為
functional evidence，找不到對應測試仍是 HOLD，不會自動產生黑箱結論。Comprehensive
mode 會先分析 README、`--help`、CLI parser、既有 Browser tests、目前 config 與 SSH aliases，
產生 runner/target/URL/semantic-step candidate；使用者只需一次確認，不需要手寫完整
`runtime.product_testing` YAML。確認後的 normalized execution contract 是唯一 authority。
Product build、product operation、Browser UI 是獨立 cases；local white-box 使用 pinned
worktree，remote product 可使用 SSH target，Browser client 依 contract 使用 local Playwright
+ SSH tunnel。Product build BLOCK 不會自動阻塞獨立 Browser case。只有在 remote source HEAD
等於 pinned PR head 且工作樹乾淨時，remote evidence 才是 official。README command 只能在明確
allowlist 後執行。若啟用 Web UI contract，會使用真實 Playwright browser interaction
與 positive UI assertion；缺少 Playwright/browser/server/oracle 回 `BLOCK`/`HOLD`，不會
降級為 curl/API probe。測試輸出、build log、artifact hash、browser screenshot/trace
會保存到 redacted review evidence；缺少測試依賴會回 `BLOCK`，不會誤稱產品測試
`FAIL`。`--dry-run` 才是不執行測試的模式；`--diff-only` 明確跳過 product case/build/browser
流程，只做 deterministic diff、targeted test 與 regression test。命令回應會直接顯示
PR head、test result、product test、QA matrix、conclusion、finding、具體修補建議與 remote reply
preview；不需要先打開 JSON 才能預覽內容。即使 QA 有 BLOCK/HOLD，`--confirm` 仍只會準備
明確標示為 `COMMENT` 的 advisory Gitea review；Quality Pilot 不自動 APPROVED，最終
COMMENT/REQUEST_CHANGES/APPROVED 由使用者決定。若 conclusion 不是
`NO_BLOCKING_FINDINGS`，review report 會產生 `review_gate: BLOCKED`，CLI 以 exit code 4
阻擋後續 automation；即使沒有 blocking finding，也只會是 `HUMAN_GATE_REQUIRED`，不會
宣稱 merge allowed。

### Local repo QA

不需要 Redmine/Gitea 的第一次產品掃描：

```text
/quality-pilot setup
# setup 會先分析 README、--help 與既有測試；確認後可使用 --confirm-discovery
/quality-pilot setup --confirm-discovery
/quality-pilot environment status
/quality-pilot doctor
/quality-pilot cases generate --init
```

若 `environment status` 仍是 `needs_user_input`，不要執行準備環境的 case；先完成
`grill-me` 訪談，再由 Hermes 寫入 `environment configure`。若 `doctor` 回報可安全修復的 config/overlay 缺口，才執行
`/quality-pilot doctor --fix`。Generation 完成後，先看 `next_actions`；通常是
`cases review`、`cases validate` 或執行一個已確認 side-effect boundary 的
case。不要在 fresh repo 中無條件緊接著執行 `--growing`。

### Issue-driven QA

使用者只需提供 Redmine ID 或目前 Gitea repo context，不需要自行換算
Redmine/Gitea/case IDs：

```text
/quality-pilot issues sync --redmine-issues 144780 144693
```

Hermes 應先透過 Redmine MCP live-read 並完成 snapshot handoff，再由同一個
使用者流程執行 sync。接著依 `next_actions` 選擇產生 linked cases 或直接進入
feature/fix handoff：

```text
/quality-pilot cases generate --redmine-issues 144780 144693
/quality-pilot issues fix --issue <id>
```

`144780 144693` 只是範例，可替換成任意多個 Redmine issue ID。

### Developer fix and retest

```text
/quality-pilot issues fix --issue <id>
/quality-pilot cases run <linked_case_id>
/quality-pilot issues report
```

`--push-pr` 必須等 acceptance coverage/evidence 與 write gate 通過。不要因為
單一 command `test_outcome: PASS` 就跳過 linked/sibling/boundary retest。

### Scheduled growth

先手動驗證單一 tick：

```text
/quality-pilot close-loop heartbeat
```

Heartbeat 只執行一次，不會安裝 12 小時計時器。Hermes 或外部 scheduler
必須觸發下一次；cron、lock、cwd、log 與 recovery 範例見
[Heartbeat and external scheduling](HEARTBEAT_CRON.md)。

## Command Groups

| Group | Commands | Purpose |
|---|---|---|
| root | `help`, `setup`, `doctor`, `doctor --fix` | 看手冊、初始化、檢查/修復 config skeleton、檢查 Gitea/Redmine MCP readiness |
| environment | `status`, `configure`, `tui-probe` | 確認 local/remote、入口、fixture、credential env 與副作用邊界；PTY transcript probe 需 explicit marker；不保存秘密值 |
| audit | `state` | 只讀檢查 overlay 語意一致性：case、evidence、issues、reports、MCP、subagent |
| graph | `tutor`, `status`, `scope`, `representation`, `ontology`, `extract`, `quality-gate`, `fuse`, `evaluate`, `serve`, `run --from-qa` | Knowledge Graph 九階段 + Task Graph orchestration；優先投影既有 cases/runs/evidence/review artifacts；SQLite canonical、JSON export、provenance、fusion gate、read-only serving |
| review | `pr`, `apply` | pinned detached local review、allowlisted regression report、分離 snapshot/report/evidence traceability、BLOCK/HOLD remediation recommendations、advisory COMMENT Gitea review handoff 與 MCP result reconciliation |
| issues | `sync`, `status`, `report`, `show`, `fix` | 同步、去重、prune、issue QA report、Gitea evidence writeback handoff、修復 handoff、產品修復 PR |
| cases | `generate`, `review`, `validate`, `list`, `run`, `push-pr` | 產生與執行 test case contracts，依 linked case/evidence 建產品修復或驗證 PR |
| publish wiki | `status`, `plan`, `apply` | 狀態看板，只更新 Gitea Wiki，不建立 issue comment/issue/PR；auto-sync 僅 local plan |
| close-loop | `status`, `run-once`, `run-once --legacy`, `heartbeat`, `heartbeat --legacy` | Task Graph 預設 orchestration；checkpoint/verifier/human gate；`--legacy` 僅作 fixed-sequence fallback；sensor-driven 持續成長 |
| report | `status`, `json` | 查看 Markdown/JSON 報告 |
| tracker | `plan-write` | 相容保留的單一 write-gate 檢查 |
| subagent | `status`, `configure` | 設定文字生成 subagent handoff，預設 Open WebUI |

## Graph Engineering

Quality Pilot follows the reference project's two halves:

```text
Knowledge Graph (what agents remember)
  scope -> representation -> ontology -> entities/relations/events
        -> quality-gate -> reversible fusion -> evaluation -> read-only serving

Task Graph (how agents work)
  Context -> Contract -> DAG -> source adapter -> parallel extraction -> independent verifier
          -> merge owner -> human gate -> checkpoint -> repair
```

Knowledge Graph commands:

```text
/quality-pilot graph tutor
/quality-pilot graph scope --question "Which test run produced evidence for this case?"
/quality-pilot graph representation
/quality-pilot graph ontology
/quality-pilot graph run --from-qa --question "Which test run produced evidence for this case?" \
  --case-id CASE-001 --review state/reviews/pr-report.json
/quality-pilot graph extract --input candidate.json  # external adapter only
/quality-pilot graph quality-gate --gold labels.json
/quality-pilot graph fuse
/quality-pilot graph evaluate --gold labels.json
/quality-pilot graph serve --entity <id-or-name>
```

The canonical local store is SQLite and the portable artifact is JSON. Every fact needs
source, extraction time, confidence, and evidence. Candidate extraction is validated before
write; fusion is reversible and requires explicit confirmation; serving is read-only.

The integration-first path is `graph run --from-qa`: it projects existing case contracts,
canonical run/evidence records, and an optional pinned PR review report into candidates.
When `--review` is supplied, schema, report hash, pinned head/base, PR identity,
open/merged state, optional `updated_at`, and evidence paths are validated fail-closed.
External candidate input remains explicit, so an LLM/subagent cannot write directly:

```json
{
  "entities": [{
    "entity_id": "case-1",
    "entity_type": "TestCase",
    "canonical": "CASE-1",
    "provenance": {
      "source_ref": "cases/CASE-1.yaml",
      "evidence": "CASE-1 is the executable contract",
      "confidence": 1.0
    }
  }],
  "relations": [],
  "events": []
}
```

`/quality-pilot graph run --from-qa` compiles these stages with
`compile_graph_engineering_task_graph()`: existing QA artifacts are projected first,
entity extraction runs before relation/event fan-out, a separate verifier checks quality,
fusion is behind a human gate, and the graph workflow checkpoint supports resume/repair.
The graph is a read model; it does not need Neo4j and graph counts do not produce QA `PASS`,
`READY`, `APPROVED`, or `MERGE_ALLOWED`.

The QA Task Graph path is now the default close-loop orchestration:

```text
/quality-pilot close-loop run-once
/quality-pilot close-loop run-once --resume-task-graph --confirm-publish
/quality-pilot close-loop run-once --repair-node execute:<case_id> --resume-task-graph
/quality-pilot close-loop run-once --legacy  # explicit fixed-sequence fallback
```

## Audit

`audit state` 是只讀語意稽核，不會修改 `.quality-pilot-project`。它補足 `cases validate` 的盲點：YAML 語法可以有效，但 overlay state 仍可能混用新舊流程，造成 evidence、Gitea handoff、Wiki report、graph provenance 和 MCP readiness 互相不一致。

```text
/quality-pilot audit state
```

它會列出 blocker/warning，例如 Redmine generic command、evidence-contract mismatch、stale MCP issue-write request、active issue without runnable case、Wiki READY/NOT_RUN disagreement、missing Hermes MCP status、subagent profile incomplete。`doctor` 和 `issues status` 也會帶出 state audit 摘要。

## Cases

`cases generate` 必須指定模式。裸指令會回 `explicit_generation_mode_required`，Hermes 應引導使用者選：

```text
/quality-pilot cases generate --init
/quality-pilot cases generate --growing
/quality-pilot cases generate --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
```

`--init` 是第一次導入產品時的 repo SWQA 建案。它會掃 README、程式碼、
package metadata、既有 cases/runners/rules，並用第一版 stratified selector
將 case budget 分散到可用的 operation/dimension strata，避免被單一 surface 填滿。
它產生的是目前可證明安全且可執行的 product-runtime contracts，不代表已完成
全部白箱、黑箱、mutation、security、UI 或 load/soak 測試。沒有 public
`--fast` 參數。

`--count <n>` 是唯一正式的數量限制：

```text
/quality-pilot cases generate --init --count 5
```

`--growing` 是後續擴散。它會讀 repo signals、code inventory、Gitea/local
issues、Redmine imports、PR refs、recent git commits、latest run、reports、
existing cases/runners/rules，產生新的 product-runtime command contracts。
預設上限目標是 20 個 growth cases；duplicate command 不消耗新增 budget。
數量不是品質保證，generator 的 operation diversity 也不是完整 risk coverage。
若只想小批量，使用 `--count <n>`。

Growing 不是單純測 help 指令是否存在。候選會先被轉成 SWQA operation matrix，再寫成 product-runtime command，例如 invalid-option rejection、boundary invalid-value rejection、repeatability loop、concurrency probe、timeout baseline、sibling surface sweep、bounded monkey sweep。這些 operation 都必須經過 command policy，使用已設定/已推論的產品 binary/API/runner，且保持 side-effect-safe。

Monkey test 第一版是 bounded `monkey_cli_help_sweep`：只會把 README/產品 runtime 已知的 help/version surfaces 組成 side-effect-safe sweep，或在安全 envelope 內做 repeatability/concurrency 變體；不會產生 destructive random command、repo-only probe 或未受控的 synthetic invalid command。

`close-loop heartbeat` 會先跑 sensors，第一版 sensor 包含
`cases generate --growing`。若有新增 cases，heartbeat 只執行新增或指定 scope；
若沒有新增工作，回報 `idle`。預設是單次 tick，12 小時只是 scheduling
metadata，每次最多長出 20 個 cases；不再使用 `--iterations`。`--every 6h`
或 `--every 24h` 只調整 metadata，不會安裝 timer。實際週期由外部
scheduler/Hermes 觸發，詳見 [Heartbeat runbook](HEARTBEAT_CRON.md)。

所有 generated `commands[].run` 都必須使用已設定/已推論的產品 binary/API/runner，或使用者確認的 runner。Repo-only checks、`python3 -c`、`compileall`、synthetic invalid command、`go test`、`go run` 不能偽裝成 testcase command，除非使用者明確把它們設定為產品 runner。

`--redmine-issues` 支援多個 Redmine issue ID，會直接用 Hermes Redmine MCP snapshot 產生 linked testcase contracts。Redmine ticket 到 Gitea issue 的建立/更新不在 case generation 內執行；請先使用 `issues sync --redmine-issues`，讓 sync flow 透過 gated handoff 建立或更新 linked Gitea issue，再由 canonical mapping 串起 case/evidence/fix。

`cases run` 取代舊測試執行群組：

```text
/quality-pilot cases list
/quality-pilot cases run <case_id>
/quality-pilot cases run
```

執行前會檢查 environment profile。README 指南中可辨識的唯讀命令會被標記為
`readme_cli_operation`，並保留原始命令與 fixture 要求；環境未確認、fixture
不存在、remote target env 未設定、credential env 不存在或入口找不到時，case
會回 `BLOCK`，不會執行後把錯誤包裝成 `PASS`。只有全為 partial probe 時，整體
結果是 `HOLD`（partial 統計仍保留），不是正式 QA `PASS`。

## Issues

`issues sync` 內建 sync、dedupe、prune 與遠端 duplicate action plan。closed/resolved issue 以遠端為事實來源：本地 active mirror 會移除，不留言、不 reopen。

```text
/quality-pilot issues sync
/quality-pilot issues sync --redmine-issues <redmine_issue_id> [<redmine_issue_id> ...]
/quality-pilot issues status
/quality-pilot issues report
/quality-pilot issues create-from-failure --local --case <case_id>
/quality-pilot issues create-from-failure --remote --case <case_id>
/quality-pilot issues create-from-failure --local --all
/quality-pilot issues create-from-failure --remote --all
/quality-pilot issues show <issue_id>
/quality-pilot issues fix --issue <id>
/quality-pilot issues fix --case <case_id>
/quality-pilot issues fix --all
```

`issues sync --redmine-issues ...` 會透過 Hermes Redmine MCP snapshot 解析多個 Redmine issue ID，同步本地 Redmine mirror，產生 gated `mcp_issue_write_request`，並由 Hermes Gitea MCP 在同一流程建立或更新 linked Gitea issues。CLI engine 本身不保存 token，也不直接打 Gitea HTTP。

`issues report` 會從 canonical issue map 和 latest-run 產生 `reports/issues-report.md` 與 `state/issues-report.json`。若 linked case 最新結果是 FAIL/BLOCK，會產生 gated `gitea.issue.update` evidence handoff，目標是 linked Gitea issue；內容是人類可讀摘要、reproduction command、result/evidence path 和下一步，不是 raw JSON。

`issues report` 也會列出沒有 tracker mapping 的 standalone official FAIL/BLOCK。這些失敗不會因為 `issue_count: 0` 而被當成不存在；`partial_probe` 會另外標記，預設不直接建立產品缺陷 issue。要把正式失敗轉成 Gitea issue 建立 handoff，使用：

```text
/quality-pilot issues create-from-failure --local --case <case_id>
/quality-pilot issues create-from-failure --remote --case <case_id>
/quality-pilot issues create-from-failure --local --all
/quality-pilot issues create-from-failure --remote --all
```

`--local` 只寫入 `issues/local/failure-report.md`、每個 testcase 的 local work item，以及 `state/failure-report.json`，不建立 Gitea request 或 issue ledger；`--remote` 會同時寫入同一份本地完整技術報告，再產生 gated `mcp_issue_write_request`。遠端每個 issue 都包含測試範圍、重現步驟、預期/實際結果、oracle evidence、風險與後續行動，並自動移除 credentials、token、工作站路徑與內部工具細節，讓沒有 Quality Pilot 的協作者也能獨立閱讀。Hermes 確認後以 Gitea MCP 建立 issue，再執行 `/quality-pilot issues sync`；partial/environment probe 只有明確加上 `--include-partial` 才會納入。

產品修復與新功能開發 handoff 集中在 `issues fix`。`issues sync` 後即可對 synced issue 執行 `issues fix --issue <id>`；如果該 issue 尚未有 runnable linked case，會進入 issue-driven development handoff，要求先補 acceptance cases/evidence 再建立 PR：

```text
/quality-pilot issues fix --issue 123
/quality-pilot issues fix --case FAIL-001
/quality-pilot issues fix --issue 123 --push-pr
/quality-pilot issues fix --all
```

`issues` 是後續修復流程的通用 work-item 入口：Gitea remote work item 使用 issue ID（例如 `123` 或 `ISSUE-123`），本地 failure work item 使用 testcase ID（例如 `FAIL-001`）。本地完整報告與 testcase work item 會放在 `.quality-pilot-project/issues/local/`；同步的 Gitea mirror 會放在 `.quality-pilot-project/issues/remote/`，根目錄下的舊 mirror 仍保留作為相容層。兩者都由 `issues fix` 消費，不需要人工搬移檔案。

`--push-pr` 只有在 preflight、linked cases/evidence 或 issue-driven acceptance coverage、write gate 通過後才建立產品修復/新功能 PR。

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

預設 provider 類型可使用 Open WebUI，但沒有任何 private endpoint 是通用產品
預設。每個 deployment 必須提供自己的 endpoint/model；不需要 subagent 的
local workflow 可以保持未設定或停用。

查看設定：

```text
/quality-pilot subagent status
```

補上或重建預設設定：

```text
/quality-pilot subagent configure
```

`setup` 的 `readiness.mode` 是 `SETUP_READY`，代表本地 config/overlay 建立成功；遠端 issue/Wiki 狀態另看 `remote_sync_readiness` 與 `readiness.remote_sync_blockers`。`gitea_mcp_snapshot_missing` 不應讓 setup 顯示成 `SYNC_BLOCKED`，但在 snapshot 完成前仍禁止 remote sync/write。

`setup`、`doctor --fix` 與 `configure` 只應建立安全 routing skeleton；使用者
需要提供 deployment-owned Open WebUI endpoint/model。API key 只允許用
`api_key_env` 指向環境變數；各任務 `task_prompts` 是 optional override。

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
| `--fast` | no longer supported; use `--init` and optionally `--count <n>`; this does not imply complete deep coverage |
| `--from-issues` | `--growing` |
| `--candidate-json` | not public; external sessions may analyze, but AI Quality Pilot owns case writing |

## Direct Engine Examples

From an installed package:

```bash
quality-pilot setup --root /path/to/product
quality-pilot doctor --root /path/to/product
quality-pilot doctor --fix --root /path/to/product
quality-pilot audit state --root /path/to/product
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
