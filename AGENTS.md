# AGENTS.md

本文件面向在本仓库工作的 Agent（含代码生成/修改 Agent）。
目标：在不破坏现有结构与工作流的前提下，高质量完成改动。

## 0. 仓库信息

- 项目类型：Python 库（`moneywiz-api`）
- 源码目录：`src/moneywiz_api/`
- 测试目录：`tests/unit/`、`tests/integration/`
- 配置文件：`pyproject.toml`（setuptools + PEP 621）
- CI：`.github/workflows/ci.yaml`
- Python：最低 `>=3.10`，CI 覆盖 `3.10` 与 `3.11`

## 1. 环境准备

建议使用虚拟环境并安装开发依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

说明：

- `.[dev]` 包含 `pytest`、`pytest-cov`、`ruff`、`mypy`、`build`、`twine`
- 仅运行库本身可用 `pip install -e .`

## 2. Build / Lint / Test

### 2.1 Build（打包）

```bash
python -m build
python -m twine check dist/*
```

### 2.2 Lint / Format / Type Check

以 Ruff + mypy 为主（与 CI 对齐）：

```bash
ruff check .
ruff format .
mypy src
```

补充：`Makefile` 中有 `pylint` / `black` 目标，属于历史脚本；新改动优先 Ruff。

### 2.3 测试命令

```bash
pytest tests
pytest tests/unit
pytest tests/integration
```

### 2.4 单测定点运行（重点）

```bash
# 单个文件
pytest tests/unit/test_dummy.py

# 单个函数
pytest tests/unit/test_dummy.py::test_dummy

# 按关键字筛选
pytest tests -k "accounts and not integration"

# 调试常用
pytest tests -x
pytest tests -vv
```

### 2.5 CI 对齐命令

```bash
ruff check --output-format=github
ruff format --diff
pytest tests/unit --doctest-modules
```

提交前至少保证：

1. `ruff check .` 通过
2. `ruff format .` 无差异（或已应用格式化）
3. 受影响测试通过（至少定点，理想 `pytest tests`）

## 3. 代码风格与约定

### 3.1 导入（imports）

- 顺序：标准库 -> 第三方 -> 本地包
- 分组之间空一行
- 不保留未使用导入
- 禁止 `from x import *`
- 多符号导入可使用括号换行

### 3.2 格式化（formatting）

- 行宽 `88`，缩进 4 空格
- 字符串优先双引号
- 让 Ruff 负责格式化，避免手工对抗格式器

### 3.3 类型（types）

- 新增/修改函数尽量补全类型注解
- 沿用别名：`ID = int`、`GID = str`、`ENT_ID = int`
- 容器类型写清楚（如 `Dict[ID, T]`、`List[T]`）
- Python 3.10+ 可用 `T | None`
- 管理器和对外 API 返回类型要明确

### 3.4 命名（naming）

- 类名：`PascalCase`
- 函数/方法/变量：`snake_case`
- 常量：`UPPER_SNAKE_CASE`
- 测试：文件名 `test_*.py`，函数名 `test_*`

### 3.5 模型与管理器模式

- `model/` 实体通常继承 `Record`，在 `__init__` 中完成字段映射
- `managers/` 通常继承 `RecordManager[T]`，通过 `ents` 建立类型映射
- 查询/排序/过滤逻辑优先放在 Manager 层
- 新增实体需同步更新：模型、`ents` 映射、测试

### 3.6 错误处理（error handling）

- 现有代码大量使用 `assert` 做数据完整性校验，保持一致
- 业务冲突/重复键等不可恢复状态，抛显式异常（如 `RuntimeError`）
- 参数非法使用 `ValueError`（CLI 已有实践）
- 不要静默吞异常，必要时补充上下文后再抛出

### 3.7 日志与输出

- 库代码使用 `logging`，避免直接 `print`
- 推荐 `logger = logging.getLogger(__name__)`
- CLI 层可用 `click.secho` 做可读输出

### 3.8 测试约定

- 单元测试：`tests/unit/`
- 集成测试：`tests/integration/`
- 优先覆盖变更涉及的 Manager / Model 行为
- 集成测试依赖本地 MoneyWiz DB 路径，避免让 CI 强依赖本地环境

## 4. Agent 执行准则

- 改动最小化：只改任务直接相关代码
- 优先修复根因，不做表层补丁
- 不在无关任务里做大规模重排、重命名
- 公共接口变化时，同步更新测试与必要文档
- 提交前至少运行：`ruff check .` + 受影响测试

## 5. Cursor / Copilot 规则扫描

已检查：

- `.cursor/rules/`
- `.cursorrules`
- `.github/copilot-instructions.md`

结果：当前仓库未发现上述规则文件。
若后续新增，请将其内容并入本文件，并标记为“高优先级覆盖规则”。

## 6. 常用命令速查

```bash
pip install -e ".[dev]"
ruff check .
ruff format .
mypy src
pytest tests
pytest tests/unit/test_dummy.py::test_dummy
python -m build
```

---

若你是自动化 Agent：

1. 先读本文件再开始改动
2. 优先与 CI 流程保持一致
3. 优先正确性与可维护性，其次才是改动规模
