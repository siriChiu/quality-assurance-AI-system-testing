# QA-AIST

QA-AIST (QA - AI self tester) 是一個可安裝的 QA 自動化 CLI。它的核心用途是：

- 在任何產品 repo 裡建立 QA 工作區。
- 讓使用者用 YAML 定義「要跑哪些測試指令」。
- 執行測試並保存 stdout、stderr、return code、metadata、result JSON。
- 產生 latest run state 和 Markdown report。
- 在要寫 tracker 前先跑 deterministic write gate；V1 只做 dry-run plan，不會真的留言、reopen 或 close issue。

最短說法：Hermes 或人類只要呼叫 `qa-aist`，不要自己猜流程。

## 1. 安裝或本地執行

如果你正在 QA-AIST source checkout 裡開發：

```bash
cd /path/to/qa-aist
PYTHONPATH=src python3 -m qa_aist.cli --help
```

如果要安裝成 `qa-aist` 指令：

```bash
cd /path/to/qa-aist
python3 -m pip install .
qa-aist --help
```

也可以先建 wheel：

```bash
cd /path/to/qa-aist
python3 -m pip wheel . -w dist
```

## 2. 在產品 repo 初始化

假設你的產品 repo 是 `/path/to/product-repo`：

```bash
qa-aist init-project --root /path/to/product-repo --json
```

如果還沒安裝 `qa-aist`，在 QA-AIST source checkout 裡改用：

```bash
PYTHONPATH=src python3 -m qa_aist.cli init-project --root /path/to/product-repo --json
```

初始化後，產品 repo 會長出：

```text
product-repo/
  .qa-aist.yaml
  .qa-aist-project/
    cases/
    runners/
    rules/
    state/
    evidence/
    reports/
```

重要邊界：

- `.qa-aist.yaml` 和 `.qa-aist-project/` 屬於產品 repo。
- QA-AIST tool source 不應該保存產品專用 case、runner、evidence、hostname、token、password。
- 如果工具被放在 `product-repo/.qa-aist/`，產品資料仍然要放在 `product-repo/.qa-aist-project/`。

## 3. 檢查環境

```bash
qa-aist status --root /path/to/product-repo --json
qa-aist doctor --root /path/to/product-repo --json
qa-aist config validate --config /path/to/product-repo/.qa-aist.yaml --json
```

你應該看到：

- config 存在。
- workspace 存在。
- case contract 數量。
- runner 數量。
- secret 只顯示 env var 名稱，不顯示值。

## 4. 新增一個測試 case

在產品 repo 建立或修改 `.qa-aist-project/cases/*.yaml`。

範例：

```yaml
case_id: CLI-HELP-001
title: CLI help can be rendered
feature: cli
priority: P2
contract_version: 1
commands:
  - id: help
    run: ./irctool --help
    expected_exit_code: 0
expected: help text renders successfully
write_gate:
  tracker_write_allowed: false
  reason: local deterministic regression only
```

`commands` 是 ordered command set。QA-AIST 會照順序跑，並用整份 contract 計算 `contract_hash`，避免 issue 複測方法漂移。

也可以呼叫 runner script：

```yaml
case_id: SMOKE-001
title: Project smoke test
commands:
  - id: smoke
    run: .qa-aist-project/runners/smoke.sh
    expected_exit_code: 0
```

runner script 放在：

```text
.qa-aist-project/runners/
```

## 5. 列出、驗證、執行測試

列出 cases：

```bash
qa-aist qa-test list --root /path/to/product-repo --json
```

驗證 case contract：

```bash
qa-aist qa-test validate --root /path/to/product-repo --json
```

只看會跑什麼，不真的執行：

```bash
qa-aist qa-test dry-run --root /path/to/product-repo --json
```

跑全部：

```bash
qa-aist qa-test run --root /path/to/product-repo --json
```

只跑一個 case：

```bash
qa-aist qa-test run-one --root /path/to/product-repo CLI-HELP-001 --json
```

每次執行會在 `evidence/` 下保存：

```text
.qa-aist-project/evidence/<case-id>/
  <command-id>.stdout.log
  <command-id>.stderr.log
  <command-id>.rc
  <command-id>.meta
  result.json
```

`close-loop run-once` 會用 run id 分資料夾：

```text
.qa-aist-project/evidence/<run-id>/<case-id>/
```

## 6. 跑完整 close-loop

```bash
qa-aist close-loop run-once --root /path/to/product-repo --json
```

固定 pipeline 順序是：

```text
config_validate
health_checks
tracker_pull_open_items
select_scope
run_cases
normalize_results
deduplicate_tracker_actions
write_gate
tracker_write_when_allowed
render_reports
persist_state
```

V1 的 tracker pull/write 是明確的 no-op/dry-run stage；它們仍會出現在 JSON summary 裡，方便確認流程沒有被跳過。

輸出會包含：

- `status`
- `run_id`
- `case_counts`
- `steps`
- `results`
- `latest_run_json`
- `report_path`
- `tracker_writes`
- `write_gate`

## 7. 看最新狀態與報告

顯示 close-loop 狀態：

```bash
qa-aist close-loop status --root /path/to/product-repo --json
```

產生 Markdown report：

```bash
qa-aist report status --root /path/to/product-repo --json
```

輸出 latest run JSON：

```bash
qa-aist report json --root /path/to/product-repo
```

常用檔案：

```text
.qa-aist-project/state/latest-run.json
.qa-aist-project/reports/status.md
```

## 8. Tracker write gate

V1 不會真的寫 tracker，只會做 plan：

```bash
qa-aist tracker plan-write --root /path/to/product-repo --json
```

測 closed issue guard：

```bash
qa-aist tracker plan-write \
  --root /path/to/product-repo \
  --target-state closed \
  --json
```

常見 gate reason：

- `tracker_disabled`
- `closed_issue_write_forbidden`
- `contract_drift`
- `missing_current_evidence`
- `raw_secret_detected`
- `allowed`

即使 reason 是 `allowed`，V1 也只回傳 plan，不會寫外部 tracker。

## 9. Hermes 應該怎麼用

Hermes 應該只呼叫 QA-AIST CLI，並讀 JSON 結果。Hermes 不應該：

- 自己跳過 pipeline step。
- 自己改 ordered commands。
- 自己直接 comment/reopen/close tracker issue。
- 把 raw API key 寫入 repo。

建議 Hermes flow：

```bash
qa-aist init-project --root "$REPO" --json
qa-aist doctor --root "$REPO" --json
qa-aist qa-test validate --root "$REPO" --json
qa-aist close-loop run-once --root "$REPO" --json
qa-aist tracker plan-write --root "$REPO" --json
```

## 10. 開發 QA-AIST 本身

在 `.qa-aist` repo 內跑測試：

```bash
cd /path/to/qa-aist
PYTHONPATH=src python3 -m unittest discover -s tests
```

建 wheel：

```bash
python3 -m pip wheel . -w dist
```

如果你的 Ubuntu 沒有 `python3-venv`，`python3 -m venv` 可能會失敗；可以先用 `pip wheel` 或 `pip install --target` 驗證 package。

## 11. 設計規則

1. Deterministic steps first；LLM/agents 只能輔助摘要或建議。
2. Tracker writes 必須先通過 deterministic write gate。
3. Closed tracker issues 不是 active work item。
4. Retest comment 必須使用同一份 canonical contract。
5. 每個 confirmed bug 都要擴展 sibling-surface、boundary、invalid-value、side-effect-safe regression coverage。
6. Secrets 只能放在 env var 或 secret store，不能寫進 repo。
