# MoneyWiz-API

![Static Badge](https://img.shields.io/badge/Python-3-blue?style=flat&logo=Python)
![PyPI](https://img.shields.io/pypi/v/moneywiz-api)

一个用于访问 MoneyWiz SQLite 数据库的 Python API，支持读取账户/分类/交易，并提供 CLI 工具。
Fork的分支支持写入账单，配合Agent功能让记账体验起飞！

## 功能概览

- 读取 MoneyWiz 数据库中的账户、交易、分类、收付款对象、投资持仓等信息
- 提供 `moneywiz-cli` 交互式 shell 用于数据查看
- 支持通过 CLI 自动写入简单收支账单（收入/支出）
- 支持写入去重（按金额+时间窗口）避免重复记账
- 支持自动记账审计日志（JSONL），方便后续追踪与 AI 查询

## 安装

```bash
pip install moneywiz-api
```

开发环境安装：

```bash
pip install -e ".[dev]"
```

## 快速开始（Python）

```python
from moneywiz_api import MoneywizApi

moneywiz_api = MoneywizApi("<你的sqlite文件路径>")

(
    accessor,
    account_manager,
    payee_manager,
    category_manager,
    transaction_manager,
    investment_holding_manager,
) = (
    moneywiz_api.accessor,
    moneywiz_api.account_manager,
    moneywiz_api.payee_manager,
    moneywiz_api.category_manager,
    moneywiz_api.transaction_manager,
    moneywiz_api.investment_holding_manager,
)

record = accessor.get_record(1001)
print(record)
```

## CLI 使用

### 1) 启动交互式只读 Shell

```bash
moneywiz-cli "/path/to/ipadMoneyWiz.sqlite"
```

### 2) 自动写入一条账单（收入/支出）

```bash
moneywiz-cli "/path/to/ipadMoneyWiz.sqlite" \
  --add-transaction \
  --kind expense \
  --account-id 4928 \
  --amount 39.9 \
  --desc "午餐" \
  --notes "工作日午餐" \
  --datetime "2026-03-06 12:30:00"
```

> 安全保护说明：
>
> - 默认会做写锁预检查（`BEGIN IMMEDIATE`），若无法获取写锁会拒绝写入
> - 默认会在写入前自动备份数据库
> - 默认禁止直接写 MoneyWiz 主库（容器目录下的数据库）
> - 若你确认风险并要写主库，需要显式加 `--allow-main-db-write`

参数说明（自动记账）：

- `--add-transaction`：启用写入模式
- `--kind`：`expense`（支出）或 `income`（收入）
- `--account-id`：目标账户 ID
- `--amount`：金额（传正数）
- `--desc`：账单描述
- `--notes`：备注（可选）
- `--category`：分类名称（可选，会匹配现有分类并写入分类关联）
- `--datetime`：交易时间（可选，格式 `YYYY-mm-dd HH:MM:SS` 或 `YYYY-mm-dd`）
- `--dedupe-window-seconds`：去重窗口秒数（默认 600）
- `--audit-log-path`：审计日志路径（默认 `~/.moneywiz_api/auto_bookkeeping.jsonl`）
- `--allow-main-db-write`：允许写主库（默认禁用）
- `--skip-sidecar-check`：跳过写锁预检查（不建议）
- `--backup-before-write / --no-backup-before-write`：写前是否备份（默认开启）
- `--backup-dir`：备份目录（默认 `~/.moneywiz_api/backups`）
- `--trigger-sync`：写入成功后触发同步命令
- `--sync-command`：同步命令（可不传，读取环境变量 `MONEYWIZ_SYNC_COMMAND`）
- `--sync-timeout-seconds`：同步命令超时时间（默认 60 秒）
- `--sync-mode`：同步模式，`applescript`（默认）或 `command`
- `--sync-wait-seconds`：`applescript` 模式下打开 MoneyWiz 后等待秒数（默认 20）

### 3) 查看自动记账审计日志

```bash
moneywiz-cli "/path/to/ipadMoneyWiz.sqlite" --show-auto-logs --last 50
```

### 4) 写入后触发同步

你可以在写入成功后自动执行一个本机同步命令，把新增账单同步到云端，再让其他设备拉取。

```bash
moneywiz-cli "/path/to/ipadMoneyWiz.sqlite" \
  --allow-main-db-write \
  --add-transaction \
  --kind expense \
  --account-id 1683 \
  --amount 12.8 \
  --desc "午餐" \
  --trigger-sync \
  --sync-mode applescript \
  --sync-wait-seconds 20
```

`applescript` 模式会自动执行：打开 MoneyWiz -> 等待 -> 退出。

如需自定义命令，可切换到 `command` 模式：

```bash
moneywiz-cli "/path/to/ipadMoneyWiz.sqlite" \
  --allow-main-db-write \
  --add-transaction ... \
  --trigger-sync \
  --sync-mode command \
  --sync-command "open -a MoneyWiz"
```

也可通过环境变量提供同步命令：

```bash
export MONEYWIZ_SYNC_COMMAND="open -a MoneyWiz"
```

然后写入时只传 `--trigger-sync`。

> 注意：`--sync-command` 是本机 shell 命令，请只配置你信任的命令。

> 安全保护：写账单前会检查 MoneyWiz 进程是否正在运行；若在运行会拒绝写入，避免库冲突。

## 去重与审计机制

- 写入前按“同时间窗口 + 同金额（容差 0.01）”做去重（不区分账户）
- 命中重复时不会写入新记录，会返回并记录已有交易 ID
- 每次写入（成功/失败）都会写入 JSONL 审计日志

审计日志字段示例：

- `status`：`success` / `failed`
- `action`：`inserted` / `deduplicated`
- `created_id`、`created_gid`
- `kind`、`account_id`、`amount`、`description`、`notes`
- `transaction_datetime`、`db_path`

## 安全建议

- 建议先在数据库副本上测试自动记账流程
- 正式写主库前请先备份
- 自动化场景建议保留审计日志，便于复盘与问题排查

## 质量检查与测试

```bash
ruff check .
ruff format .
mypy src
pytest tests
```

单个测试示例：

```bash
pytest tests/unit/test_dummy.py::test_dummy
```

## 贡献

项目仍在持续完善，欢迎 Issue 和 PR。

## AI 调用专用模板

下面内容可直接提供给 AI 助手（如你的自动化 Agent）作为执行规范。

### 1) 固定执行原则

- 只使用 `--add-transaction` 写入“收入/支出”两类账单
- 金额始终传正数，支出/收入由 `--kind` 决定
- 默认带上去重参数，避免重复截图导致重复入账
- 默认写审计日志，确保每次写入都有留痕
- 对无法确认的信息（如账户 ID）先报错并提示，不要猜测写入

### 2) 建议命令模板

```bash
moneywiz-cli "/path/to/ipadMoneyWiz.sqlite" \
  --add-transaction \
  --kind "<expense|income>" \
  --account-id <账户ID> \
  --amount <正数金额> \
  --desc "<账单描述>" \
  --category "<分类名，可选>" \
  --notes "<备注，可为空>" \
  --datetime "<YYYY-mm-dd HH:MM:SS，可选>" \
  --dedupe-window-seconds 600 \
  --audit-log-path "~/.moneywiz_api/auto_bookkeeping.jsonl"
```

建议流程（OCR 场景）：

1. 由 Agent 先读取数据库并做分类预测
2. 若置信度高，写入时传 `--category`
3. 若置信度低，不传 `--category`，后续手工补分类
4. 如需多设备同步，在写入命令中增加 `--trigger-sync`

多条账单批量写入规则（重要）：

- 当同一张截图识别出多条账单时，AI 应逐条执行写入命令
- 前 N-1 条写入命令不要带 `--trigger-sync`
- 仅最后一条写入命令带 `--trigger-sync`（并按需带 `--sync-mode applescript`）
- 这样可减少重复拉起/关闭 MoneyWiz，提高稳定性并降低冲突概率

### 3) AI 输出规范（建议）

AI 在执行后应输出：

- 本次操作：`inserted` 或 `deduplicated`
- 交易 ID：`created_id`
- 关键参数回显：`kind/account_id/amount/desc/datetime`
- 审计日志路径

如果失败，输出：

- 错误原因
- 未写入确认
- 建议修复动作（例如账户 ID 不存在、时间格式错误等）

### 4) 常见错误处理

- `--amount must be a positive number`：金额需传正数
- `Account <id> not found or has no currency`：账户 ID 不存在或账户币种为空
- `--kind/--account-id/--amount/--desc is required`：缺少必填参数
- `Skipped duplicate...`：命中去重，未重复写入，属于正常行为
- `检测到你正在写 MoneyWiz 主库...`：默认安全策略阻止主库写入，可先用副本验证
- `数据库当前无法获取写锁...`：库可能仍被占用，请先关闭 MoneyWiz 后重试

### 5) 日志查询模板

```bash
moneywiz-cli "/path/to/ipadMoneyWiz.sqlite" \
  --show-auto-logs \
  --last 50 \
  --audit-log-path "~/.moneywiz_api/auto_bookkeeping.jsonl"
```

可据此让 AI 做二次分析（例如统计最近 7 天自动记账金额、查找失败记录、筛选重复命中记录等）。

## AI 使用规范流程（推荐）

适用于“截图 OCR -> AI 解析 -> 调用 CLI 自动记账”的端到端流程。

1. 字段抽取

- 从截图中提取：`kind`（收入/支出）、`amount`、`description/merchant`、`time`、`account`
- 若缺失关键字段（如金额或账户），AI 应终止写入并返回缺失信息

2. 分类处理

- 由 Agent 读取 DB 后进行分类预测
- 若置信度高：写入命令带 `--category`
- 若置信度低：不传 `--category`，备注中标记“待分类”

3. 写入执行

- 单条账单：直接执行 `--add-transaction`
- 多条账单：按时间顺序逐条执行，依赖内置去重保护

4. 同步触发（多条场景重点）

- 前 N-1 条写入命令：不要带 `--trigger-sync`
- 最后一条写入命令：带 `--trigger-sync`
- 推荐 `--sync-mode applescript --sync-wait-seconds 20`

5. 审计与回执

- 每次写入后记录审计日志（JSONL）
- AI 回执至少包含：`action`（inserted/deduplicated）、`created_id`、`amount`、`desc`、`category`、`sync_status`
- 失败时返回：错误原因 + 未写入确认 + 建议修复动作

## Agent 初始化问询流程（首次接入）

用于让 AI Agent 在第一次接入你的记账流程时完成必要配置，减少后续反复确认。

1. 数据库与环境确认

- 确认 MoneyWiz 主库绝对路径
- 确认是否允许写主库
- 可选：先用测试库演练 1~3 笔

2. 写入安全策略

- 写入前若检测到 MoneyWiz 正在运行，默认中止并提醒关闭
- 开启写前自动备份（推荐）
- 确认备份目录（默认 `~/.moneywiz_api/backups`）

3. 账户映射设置

- 设置默认支出账户（账户名或 ID）
- 设置默认收入账户（账户名或 ID）
- OCR 未识别到账户时是否自动用默认账户（推荐：是）

4. 分类处理策略

- 分类预测由 Agent 端读取数据库后完成
- 置信度高时传 `--category`
- 置信度低时不传 `--category`，在备注标记“待分类”

5. 去重参数设置

- 当前规则：同时间窗口内金额相同即判重
- 确认 `--dedupe-window-seconds`（默认 600）

6. 同步策略设置

- 是否启用写后自动同步
- 推荐：`--sync-mode applescript`
- 确认 `--sync-wait-seconds`（默认 20）
- 多条账单场景：仅最后一条带 `--trigger-sync`

7. 审计与回执规范

- 确认审计日志路径（默认 `~/.moneywiz_api/auto_bookkeeping.jsonl`）
- 成功回执最少包含：`action`、`created_id`、`amount`、`desc`、`category`、`sync_status`
- 失败回执必须包含：错误原因、未写入确认、修复建议

8. OCR 输入执行规则

- 同一截图多条账单：按时间顺序逐条写入
- 前 N-1 条不触发同步，最后一条触发同步
- 关键字段缺失（金额/账户）时，中止该条并输出缺失项

9. 首次联调步骤

- 先做 1 笔单条写入联调（含同步与审计）
- 再做 1 次多条批量联调（仅最后一条同步）
- 通过后切换正式自动化

### 初始化配置模板（可让 Agent 持久化）

```yaml
moneywiz:
  db_path: "/ABS/PATH/ipadMoneyWiz.sqlite"
  allow_main_db_write: true
  backup_before_write: true
  backup_dir: "~/.moneywiz_api/backups"
  audit_log_path: "~/.moneywiz_api/auto_bookkeeping.jsonl"

defaults:
  expense_account_id: 1683
  income_account_id: 1683
  dedupe_window_seconds: 600

classification:
  mode: "agent_predict"
  low_confidence_action: "skip_category"
  low_confidence_note_tag: "待分类"

sync:
  enabled: true
  mode: "applescript"
  wait_seconds: 20
  trigger_on_last_item_only: true

execution:
  stop_if_moneywiz_running: true
  require_fields: ["kind", "amount", "account_id", "description"]
```

## CLI 与 Agent 责任划分

为保证稳定性与可维护性，建议采用“Agent 负责智能决策，CLI 负责安全执行”的边界。

### CLI 负责（执行层）

- 参数化写入账单（收入/支出）
- 账单字段落库：账户、金额、描述、备注、时间、收付款对象、分类（可选）
- 去重控制（同时间窗口 + 同金额）
- 写入安全保护：
  - 检查 MoneyWiz 进程是否运行
  - 写锁预检查
  - 主库写入显式开关
  - 写前备份
- 审计日志落盘（成功/失败/去重/同步结果）
- 同步触发（applescript 或 command）

### Agent 负责（智能层）

- OCR 结果解析与结构化（商家、金额、时间、账户线索）
- 交易类型判断（收入/支出）
- 账户映射（自然语言到账户 ID）
- 分类预测（读取数据库后推断，支持模糊场景）
- 多条账单编排（前 N-1 条不触发同步，最后一条触发同步）
- 失败重试策略与用户交互（缺失字段、冲突、人工确认）

### 交互契约（推荐）

- Agent 给 CLI：只传“确定字段”，不做越权数据库操作
- CLI 给 Agent：返回结构化执行结果（inserted/deduplicated/failed + id + 错误）
- Agent 再给用户：人类可读总结 + 下一步建议
