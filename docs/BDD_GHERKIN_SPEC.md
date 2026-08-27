# AI Quality Pilot BDD / Graph Engineering Specification

狀態：`draft-9`

本版依 Graph Engineering 的正確定義重新設計。這裡同時涵蓋兩個互補部分：
**Task Graph**（agent 如何工作）與 **Knowledge Graph**（agent 記住什麼）。
Knowledge Graph 是 provenance-backed optional local product，不是 relationship-database setup 依賴。

## 1. 核心定義

Graph Engineering 驗證的是 AI workflow 的拓撲與控制邊界：

```text
canonical context
  → contract compiler
  → task graph DAG
  → independent workers
  → separate verifier context
  → owned merge
  → human gate
  → durable checkpoint
  → targeted repair / resume
```

兩個不可混淆的概念：

| 概念 | 定義 | 本專案定位 |
|---|---|---|
| Task Graph | agent 如何工作；節點、資料依賴、驗證、修復、gate | 核心 Graph Engineering BDD 對象 |
| Knowledge Graph | agent 記住什麼；ontology、entities、relations、events、fusion、serving | 本版以 SQLite canonical + JSON export 實作，source systems 仍是 authority |

Knowledge-graph database、Browser 與遠端 graph service 都不是本版的必要執行條件；本版提供 SQLite/JSON local serving，GraphRAG/LLM serving 仍須明確 adapter 與 eval set。

## 2. Context 與 Contract

### Context

Context 定義每個 node **允許相信什麼**。每個 node 只收到 scoped context packet，
不能直接讀取所有 repo、prompt、上一個 agent 的隱含記憶或未驗證資料。

Context 至少包含：

- `context_id`
- trusted source references
- user requirements / constraints
- environment profile
- policy / side-effect boundary
- allowed facts for this node

Raw secret-like values、未授權 customer/restricted data、未確認的 environment fact
在 context boundary fail closed。

### Contract

Contract 定義每個 node **必須交付什麼**：

- required inputs
- required outputs
- deterministic validator
- owner / writer
- side-effect boundary
- retry / repair policy
- evidence and checkpoint references

「模型說完成」不是 contract proof；schema、dependency、artifact、reference 與
validator 必須由 deterministic code 判定。

## 3. Task Graph 規則

1. **真實 edge**：只有 downstream 會消費 upstream output 時，才建立 edge；無資料依賴的假 edge 必須被拒絕。
2. **平行 fan-out**：只有互相獨立的工作才放在同一 layer；不要把 sequential work 強行拆成 agents。
3. **獨立 verifier**：verifier 必須有不同 owner 與 context scope，不能由同一個 worker 自評。
4. **單一 merge owner**：多個 worker 的結果只能由一個明確 owner merge。
5. **單一 writer**：同一 artifact 不得由兩個 node 同時擁有寫入權。
6. **Human gate**：publish、tracker write、push PR、deploy 等不可逆動作前才放 gate；read-only 工作不應每一步都要求人核准。
7. **Stop rule**：限制最大 round、agent fan-out 與 repair 次數；超過上限即停止，不得無限重試。
8. **Checkpoint**：每個已驗證 node 的 output、validator、contract hash 與狀態都要可恢復。
9. **Targeted repair**：失敗 node 只使自己與 descendants 重跑；無關的已通過 branch 保持 PASS checkpoint。
10. **Authority separation**：Task Graph execution state 不會自動產生產品 `PASS`、`READY`、`APPROVED` 或 `MERGE_ALLOWED`。

## 4. BDD 文件責任邊界

```text
docs/BDD_GHERKIN.feature                 # index / shared background
docs/bdd/context-contract.feature       # scoped context + node contracts
docs/bdd/task-graph.feature              # topology + dependency + verifier
 docs/bdd/execution-repair.feature       # executor + checkpoint + repair
 docs/bdd/human-gate-security.feature    # approval boundary + safety
 docs/bdd/review-comprehensive.feature   # PR identity + comprehensive QA matrix
 docs/bdd/remote-product-browser.feature # local/remote execution + Browser lifecycle + evidence lineage
docs/bdd/knowledge-graph.feature        # KG scope/ontology/extraction/fusion/serving + bridge
```

BDD audit 的 scenario 數量與 binding 數量由工具即時產生，不在本規格文件硬編碼固定數字。

新增的 remote product/browser、contract discovery、source identity、dynamic URL、tunnel、trace
sanitization、cleanup 與 canonical lineage 場景目前標記為 `planned`，在實作與 executable
integration fixture 完成前不能算綠燈證據。`planned_scenarios_are_not_green_evidence` 仍保留，
避免規格存在被誤解為實作完成。Knowledge Graph 的 structural readiness 也不等於 QA release truth。

## 5. Current executable core

目前先以 deterministic Python core 驗證以下行為：

- `ContextPacket.scoped()` 只傳遞 node 宣告的 keys
- raw secret context fail closed
- `TaskContract` 驗證 required inputs/outputs
- `TaskGraph.validate()` 拒絕 cycle、fake edge、multiple writers
- compiler 建立 context → contract → case worker → verifier → merge → gate → publish
- 獨立 case workers 位於同一 parallel layer
- verifier owner/context scope 與 worker 分離
- executor 在 FAIL/BLOCK 後停止 downstream
- bounded parallel workers 在同一 layer 執行獨立 nodes（hard limit 16）
- checkpoint 可原子保存、恢復且保留 contract hash
- targeted repair 只 invalidate failed branch
- irreversible task 沒有 approval 時回 `HOLD`
- review workflow 會重建空 MCP diff，並將 case generation/run 的 QA matrix 與 review reply gate 分離
- local product build/run 測試與一般 case test 共用已確認的 local environment、pinned worktree、argv 安全規則、逾時、去機密、證據追溯、狀態語意與人工 write gate；產品測試只增加自己的 artifact 與 semantic oracle
- local Playwright browser 測試與一般 case test 共用上述 local 執行邊界，但保留真實瀏覽器互動與 UI state assertion；不能降級為 curl、API probe、mock DOM 或 generic case probe
- remote product/browser lifecycle、source identity、SSH tunnel、dynamic URL、trace sanitization 與 local/remote evidence reconciliation 由 `remote-product-browser.feature` 定義，尚未完成前只能標記為 planned
- 缺少瀏覽器依賴時，保留原始 collection error，安全地嘗試不依賴該套件的測試；fallback PASS 不得冒充完整 regression PASS
- browser screenshot/trace 預設只屬於本地 evidence；只有獨立且明確 gated 的 Gitea attachment upload 能力成功時，才可作為遠端圖片，不能輸出本機檔案路徑
- Knowledge Graph local runtime 驗證 scope、SQLite/JSON representation、ontology domain/range、provenance-backed extraction、quality gate、reversible fusion、gold evaluation 與 read-only serving
- `graph run --from-qa` 只從既有 case contracts、canonical runs/evidence 與 pinned review reports 建立 read-model candidates，不另造 QA authority
- `compile_graph_engineering_task_graph()` 將 entity extraction fan-out、independent verifier、fusion human gate、checkpoint/repair 編排成 DAG

Explicit integration boundary：預設 `close-loop run-once` 會實際執行 case、verifier、
merge 與 human gate；第一次沒有 confirmation 時保存 checkpoint 並回 `HOLD`。
`--legacy` 才會選擇 fixed-sequence fallback。

Implementation boundary:

```text
src/quality_pilot/task_graph.py
src/quality_pilot/execution_contract.py
src/quality_pilot/remote_browser_adapter.py
src/quality_pilot/graph_engineering/
tests/bdd/test_task_graph_contract.py
tests/bdd/test_knowledge_graph.py
```

## 6. Status semantics

| 狀態 | Task Graph 意義 |
|---|---|
| `PASS` | 此 node 的 deterministic contract validator 通過 |
| `FAIL` | node 已執行，但 output 不符合 contract |
| `BLOCK` | context、input、environment 或 prerequisite 缺失，尚未證明產品失敗 |
| `HOLD` | 等待 human gate 或其他明確決策 |
| `SKIPPED` | upstream 未通過，因此不允許執行 |
| `PENDING` | 尚未執行或已被 targeted repair 重新排入 |

Node `PASS` 不等於整個產品 QA PASS；整體 truth 仍由 case、run、evidence、write gate
與 source authority 分別判定。

## 7. Boundary / non-goals

本版已提供 local Knowledge Graph first slice，但不把下列事項冒充完成：

- Neo4j/Aura/Browser 或 remote graph service
- 未經 provenance/evidence 的自由文字 LLM extraction
- 沒有 adjudicated gold set 的 precision/recall claim
- 沒有 review/approval 的 entity merge
- GraphRAG answer quality 或 graph-as-memory claim without a project adapter and eval set
- 以 knowledge-graph node/edge 數量代替 workflow evidence
- 讓 LLM 自己決定 dependency、approval 或 release truth

Knowledge Graph 不能取代 Task Graph；Task Graph 不能取代 source authority。

## 8. Review execution contract 與 configuration UX

PR comprehensive review 必須先建立一份 normalized effective execution contract。使用者不應
被要求手寫完整的 `runtime.product_testing` YAML；Quality Pilot 應先分析 README、`--help`、
CLI parser、既有 Browser/Playwright tests、現有 config 與 SSH config，再顯示候選並要求一次
明確確認。

候選在確認前只能是 candidate，不能成為 official QA oracle。確認後寫入的 normalized contract
是唯一 authority，後續 contract hash、case、run、evidence、report 與 Graph projection 都只能
讀取這份 contract，不能分別重新讀取 `runtime.web_ui` 或 `runtime.product_testing.web_ui`。

使用者確認的是設定決策，不是 QA 結果：

```text
README / --help / existing tests
  → candidate runner / target / URL / semantic steps
  → one explicit confirmation
  → normalized contract + contract hash
  → execution
```

Remote host、repo、Python path 的 discovery 優先順序是：

```text
existing confirmed config → SSH config alias → git/repository metadata → ask only missing facts
```

日常 `review pr` 若缺少 normalized contract，應直接啟動一次 discovery confirmation wizard；
使用者拒絕時回 `CONFIGURATION_REQUIRED`，不得執行 product 或 Browser command。

Contract 至少要分開描述：

```yaml
execution:
  local_review_worktree: true
  local_pytest: true
  product_target: remote_ssh
  playwright_target: local_via_ssh_tunnel
remote:
  ssh_host: smartfan-x86-qa
  repo: /remote/repo
  python: /remote/repo/.venv/bin/python
  expected_head_sha: <pinned-pr-head>
product_testing:
  build:
    enabled: false
  browser:
    enabled: true
    start_argv: [.venv/bin/python, main.py, --browser]
    url_discovery:
      source: stdout
      pattern: 'Browser UI: https?://...'
```

Product build、product operation 與 Browser UI 是獨立 cases。Product build BLOCK 不會自動
阻止已具備 remote target 的 Browser case；composite product outcome 仍可為 BLOCK，但 Browser
case 必須保存自己的結果與 evidence。

Remote product evidence 只有在 remote source HEAD 等於 pinned PR head、工作樹乾淨、preflight
fresh 且 required checks 通過時，才可成為 official evidence。若 HEAD 不符、工作樹 dirty、
或 source identity 未驗證，應分別回報 `REMOTE_SOURCE_MISMATCH`、`REMOTE_SOURCE_DIRTY`、
`REMOTE_SOURCE_UNVERIFIED`、`REMOTE_PREFLIGHT_REQUIRED` 或 `INFRASTRUCTURE_BLOCK`；這些
observation 不能宣稱 PR PASS。

Playwright 的 local client 透過 SSH tunnel 使用 remote product server。Remote adapter 負責
remote process、stdout URL discovery、tunnel、cleanup；Playwright core 只負責 pre-started
server 的真實互動與 semantic assertions。Dynamic token 只能存在於暫存記憶體，trace、URL、
headers、console、network、DOM 與 report 在成為 evidence 前必須 sanitize。

## 9. BDD 執行方式

```bash
PYTHONPATH=src pytest -q tests/bdd
PYTHONPATH=src python3 -m unittest discover -s tests
```

BDD audit 可由 `setup`、`doctor`、`audit state` 讀取，但 audit 只報告 binding
coverage，不把 scenario 數量或一般 unit-test 綠燈冒充 workflow proof。
