# Agent Platform - AI Agent 项目指南

> 本文件面向 AI 编程助手。阅读前请假设你对本项目一无所知。以下描述基于项目当前实际内容，不依赖 README 中的历史说明。

## 1. 项目概述

Agent Platform 是一个基于大语言模型（LLM）的**智能体协作平台**，采用 **Lead Agent + 异步 SubAgent** 的架构：

- **Lead Agent**（`src/agents/agent.py`）：任务规划者，负责任务分解、通过 `task_delegate` 委托子任务、整合 SubAgent 返回结果、审批危险命令。
- **AsyncSubAgent**（`src/agents/async_subagent.py`）：在独立线程中执行具体任务，完成后通过 `MessageBus` 向 Lead 发送 `subagent_done` 消息，并等待 `shutdown_request`（确认关闭）或 `revision_request`（修订指令）。
- **MessageBus**（`src/infrastructure/message_bus.py`）：内存中的线程安全消息总线，用于 Agent 间通信。
- **Task Store**（`src/infrastructure/task_store.py`）：内存中的持久化任务管理，支持依赖关系 `blocked_by`。
- **ContextStore**（`src/infrastructure/context_store.py`）：对话历史存储 + 视图层，支持自动摘要压缩。

项目语言以 **中文** 为主（注释、README、终端输出、prompt 均使用中文）。

## 2. 技术栈

- **语言**：Python
- **LLM 客户端**：OpenAI 兼容 API（`openai` 库）
- **Tokenizer**：`transformers.AutoTokenizer` + `tiktoken`
- **文件格式**：Excel 读写使用 `openpyxl` / `xlrd`，技能元数据使用 `PyYAML`
- **并发**：Python `threading`
- **平台依赖**：代码中使用了 `os.setsid`、`pty`、`signal` 等 Unix/Linux 特有 API，**在 Windows 上无法直接运行**。

## 3. 项目结构

```
agent_platform/
├── main.py                              # 统一入口：单 Agent 交互模式
├── README.md                            # 项目说明（部分内容与当前目录实际状态不一致）
├── .gitignore                           # Python 标准忽略规则
├── config/
│   ├── config.py                        # LLM 连接、路径、阈值等配置
│   ├── prompts/
│   │   ├── single.py                    # Lead Agent system prompt
│   │   ├── subagent.py                  # SubAgent system prompt
│   │   └── compact.py                   # Auto Compact 摘要 prompt
│   └── tools/
│       ├── common.py                    # 共享工具定义（bash / 文件 / Excel / send_message）
│       ├── lead.py                      # Lead 专用工具（task_delegate / task CRUD）
│       └── subagent.py                  # SubAgent 专用工具（todo）
├── src/
│   ├── agents/
│   │   ├── agent.py                     # BaseAgent + Agent（Lead）
│   │   └── async_subagent.py            # AsyncSubAgent + spawn_subagent
│   ├── infrastructure/
│   │   ├── message_bus.py               # MemoryMessageBus + UserInputQueue
│   │   ├── task_store.py                # MemoryTaskManager
│   │   ├── context_store.py             # 对话历史 ContextStore
│   │   └── clients/
│   │       └── llm_clients.py           # LLMClient（OpenAI 兼容）
│   └── utils/
│       ├── function.py                  # 工具实现（bash 安全校验、文件/Excel 读写）
│       ├── managers.py                  # TodoManager、SkillLoader
│       └── stdio_redirect.py            # AgentLogger（每 Agent 独立日志）
├── skills/                              # 技能目录（当前为空，没有 SKILL.md）
│   ├── cuda_upgrader/
│   ├── unit_test_generator/
│   └── vllm_model_deployer/
├── docs/                                # 文档目录（当前为空）
└── test/                                # 测试目录（当前为空）
```

### 重要现状说明

- `skills/`、`docs/`、`test/` 三个目录**当前为空**，没有 `SKILL.md`、测试文件或文档。
- 仓库**不是 Git 仓库**（根目录无 `.git`）。
- 项目**没有** `pyproject.toml`、`setup.py`、`requirements.txt`、`Pipfile`、`environment.yml`、`package.json`、`Cargo.toml` 等依赖/构建清单文件。

## 4. 运行方式

### 4.1 环境要求

README 中声明需要 **Python 3.10+**，但代码中实际运行还需要以下第三方包：

- `openai`
- `tiktoken`
- `transformers`
- `openpyxl`
- `xlrd`
- `pyyaml`

### 4.2 配置

所有运行配置集中在 `config/config.py`：

- `BASE_URL`：LLM API 基础地址。
- `API_KEY`：API 密钥。
- `MODEL`：模型名称。
- `TOKENIZER_PATH`：本地 tokenizer 路径（`rsrc/tokenizer/qwen3-32B`，默认被 `.gitignore` 忽略）。
- `WORKDIR`：Agent 执行 bash / 文件操作的工作目录。**当前硬编码为 `/data/PyProject-dev2/wangdehua`**，不是项目根目录。
- `CONTEXT_WINDOW`：上下文窗口大小（默认 128,000）。
- `KEEP_RECENT_ROUNDS`：ContextStore 保留的最近完整回合数。
- `TASK_NAG_ROUNDS`：Lead 连续 N 轮未操作任务时触发提醒。
- `AUTO_COMPACT_*`：自动上下文压缩开关与阈值。
- `LOOP_DETECT_*`：循环检测开关与阈值。

> 修改 `WORKDIR`、LLM 地址、模型名后才能在本机运行。

### 4.3 启动

```bash
python main.py
```

### 4.4 交互命令

在终端中可使用：

| 命令 | 功能 |
|------|------|
| `/inbox` | 查看并处理 Lead 的 inbox 消息 |
| `/task_list` | 列出所有任务 |
| `/manual_compact` | 手动触发上下文压缩 |
| `/interrupt` 或 `/stop` | 中断当前 Agent 循环 |
| `q` / `exit` | 退出程序 |

## 5. 构建与测试

- **没有构建步骤**：纯 Python 项目，无需编译。
- **没有依赖清单**：运行前需手动安装 `openai`、`tiktoken`、`transformers`、`openpyxl`、`xlrd`、`pyyaml`。
- **没有现成测试文件**：`test/` 目录为空。README 中提到的测试命令（如 `python test/test_s09_message_bus.py`）当前无法执行。
- 可以使用 `python -m py_compile main.py src/**/*.py config/**/*.py` 做基础语法检查。

## 6. 代码组织与模块职责

### 6.1 Agent 层

| 文件 | 核心类 | 职责 |
|------|--------|------|
| `src/agents/agent.py` | `BaseAgent` | ReAct 主循环 `_loop_core`、工具调用、上下文压缩、截断续写、循环检测、后台 bash 升级。 |
| `src/agents/agent.py` | `Agent` | Lead Agent：加载 system prompt、skill 注册表、处理 `task_delegate` / task CRUD、SubAgent 生命周期跟踪。 |
| `src/agents/async_subagent.py` | `AsyncSubAgent` | 异步 SubAgent：独立线程运行、todo 跟踪、通过 MessageBus 向 Lead 请求权限 / 回报结果。 |
| `src/agents/async_subagent.py` | `spawn_subagent` | 工厂函数：创建并启动异步 SubAgent。 |

### 6.2 基础设施层

| 文件 | 核心类 | 职责 |
|------|--------|------|
| `src/infrastructure/message_bus.py` | `MemoryMessageBus` | 线程安全的内存消息总线，支持 inbox、广播、completion listener。 |
| `src/infrastructure/message_bus.py` | `UserInputQueue` | 队列化用户输入，支持 steer / interrupt / 交互式命令期间让出 stdin。 |
| `src/infrastructure/task_store.py` | `MemoryTaskManager` | 内存任务管理，支持状态、依赖关系 `blockedBy`、owner。 |
| `src/infrastructure/context_store.py` | `ContextStore` | 事件存储层（只追加）+ 视图层（摘要 checkpoint + turn 级压缩）。 |
| `src/infrastructure/clients/llm_clients.py` | `LLMClient` | 封装 OpenAI 兼容 API，支持流式输出、token 统计、JSON 修复。 |

### 6.3 工具与配置层

| 文件 | 职责 |
|------|------|
| `config/config.py` | 全局配置：LLM、路径、阈值、开关。 |
| `config/tools/common.py` | 共享工具定义 + handlers（bash、read/write/edit、Excel、send_message）。 |
| `config/tools/lead.py` | Lead 专用工具：`task_delegate`、`task_create/update/list/get`。 |
| `config/tools/subagent.py` | SubAgent 专用工具：`todo`。 |
| `config/prompts/*.py` | Lead / SubAgent / Auto Compact 的 system prompt。 |
| `src/utils/function.py` | 工具的具体实现，含 bash 安全校验、路径沙箱、Excel 转换。 |
| `src/utils/managers.py` | `TodoManager`、`SkillLoader`。 |
| `src/utils/stdio_redirect.py` | `AgentLogger`：每个 Agent 独立日志文件。 |

### 6.4 全局单例

- `bus`：全局 `MemoryMessageBus` 实例。
- `tasks`：全局 `MemoryTaskManager` 实例。

## 7. 关键机制

### 7.1 ReAct 循环（`BaseAgent._loop_core`）

1. 从 `ContextStore` 构建对话上下文。
2. 每轮调用 `LLMClient.chat_stream()` 流式获取 assistant 输出。
3. 若输出被截断（`finish_reason == "length"`），自动续写最多 `max_continuation_rounds` 次。
4. 若 assistant 调用工具，通过 `tool_handler` 执行并返回 tool 结果。
5. 支持 `on_round_start` / `on_loop_exit` 扩展点。

### 7.2 上下文压缩

- **Auto Compact**：当 prompt tokens 超过 `CONTEXT_WINDOW * AUTO_COMPACT_THRESHOLD_RATIO`（默认 80%）时，调用 LLM 生成摘要。
- **Turn-level 压缩**：旧回合被压缩为 `[user] + [assistant 最终回复] + [里程碑记录]`。
- **摘要 checkpoint**：保留最近 `KEEP_RECENT_ROUNDS` 个完整回合作为 bridge context。

### 7.3 循环检测

- 记录最近 `LOOP_DETECT_WINDOW` 轮的工具调用签名。
- 当连续 `LOOP_REPEAT_THRESHOLD` 次签名相同时，判定为循环，压缩重复轮次并注入 `<loop_detector_intervention>` 用户消息打断。

### 7.4 Bash 权限与安全

`src/utils/function.py` 中的 `_validate_command` 将命令分为三级：

1. **白名单命令**（如 `ls`、`cat`、`grep`、`python`、`git`）：直接执行。
2. **敏感命令**（如 `wget`、`pip`、`npm`、`kill`）：需用户确认。
3. **危险命令**（如 `sudo`、`shutdown`、`mkfs`、`systemctl`）：明确警告后需用户确认。

额外规则：

- `rm` / `mv` / `chmod` / `chown` / `dd` 等高风险文件操作，目标路径必须位于 `WORKDIR` 内（`safe_path`）。
- 交互式命令（如 `ssh`、`docker login`）使用 `pty` 运行，期间暂停 `UserInputQueue` 对 stdin 的监听。
- SubAgent 的危险命令通过 MessageBus 向 Lead 请求权限确认。

### 7.5 异步 SubAgent 生命周期

1. Lead 调用 `task_delegate` → `spawn_subagent()` 启动独立线程。
2. SubAgent 执行 `todo` + 工具循环。
3. 任务完成后发送 `subagent_done` 给 Lead，进入 `awaiting_review`。
4. Lead 可回复 `shutdown_request`（关闭）或 `revision_request`（修订）。
5. Lead 的 `_idle_drain_inbox` 在空闲时轮询 inbox，处理 SubAgent 消息和权限请求。

## 8. 代码风格与约定

- **注释语言**：中文。
- **命名风格**：函数/变量使用 `snake_case`，类使用 `PascalCase`。
- **类型注解**：大量使用 Python 3.10+ 语法，如 `dict | None`、`list[str]`、`str | None`。
- **字符串**：优先使用双引号，prompt 模板使用三引号字符串。
- **模块组织**：按职责分层（agents / infrastructure / utils / config），配置与实现分离。
- **全局单例模式**：`bus` 和 `tasks` 在模块加载时创建。
- **日志**：每个 Agent 独立写入 `logs/` 目录，不在 stdout 上互相竞争。

## 9. 开发注意事项

### 9.1 运行前必须修改

- 在 `config/config.py` 中设置正确的 `BASE_URL`、`API_KEY`、`MODEL`、`TOKENIZER_PATH`。
- 确认 `WORKDIR` 指向你希望 Agent 操作的真实目录（当前为 `/data/PyProject-dev2/wangdehua`）。
- 若使用 Windows 开发，需要修复 `function.py` / `agent.py` 中 `os.setsid`、`pty.openpty`、`signal.SIGTERM` 等 Unix-only 调用，或切换到 Linux/macOS/WSL 环境。

### 9.2 添加新工具

1. 在 `config/tools/common.py`（共享）、`lead.py`（Lead）、或 `subagent.py`（SubAgent）中定义工具 JSON schema。
2. 在 `config/tools/common.py` 的 `build_tool_handlers()` 或对应模块中注册 handler。
3. 在对应模块的 milestone extractors 中补充 `MILESTONE_EXTRACTORS`，以便上下文压缩保留关键信息。

### 9.3 添加新技能

1. 在 `skills/<skill_name>/` 下创建 `SKILL.md`（必须包含 YAML frontmatter，含 `name` 和 `description`）。
2. 可选添加 `README.md`、`examples/*.md`、`scripts/`。
3. `SkillLoader.discover()` 启动时会自动扫描。

> 当前 `skills/` 下三个目录均为空，没有 `SKILL.md`，因此 `skill_loader._registry` 为空。

### 9.4 测试

目前项目没有测试文件。建议后续补充：

- `test/test_message_bus.py`：验证 `MemoryMessageBus` 的 send / read / listener。
- `test/test_task_store.py`：验证 task CRUD 和 `blocked_by` 依赖清理。
- `test/test_bash_permission.py`：验证白名单 / 敏感 / 危险命令分级。
- `test/test_context_store.py`：验证 turn 分组、摘要 checkpoint、micro 压缩。

## 10. 安全考虑

- **路径沙箱**：文件读写强制限制在 `WORKDIR` 内（`safe_path`）。
- **命令分级**：危险/敏感命令需要用户显式确认。
- **权限委托**：SubAgent 的危险命令不直接由用户确认，而是经 MessageBus 上报给 Lead，再由 Lead 统一询问用户。
- **API 密钥**：`config/config.py` 中可能包含硬编码密钥，注意不要在提交时泄露。
- **后台任务**：`run_shell_command` 超时后会将命令提升为后台线程继续执行，并通过 bus 通知结果；注意这可能产生长时间运行的子进程。

## 11. 已知问题 / 待完善

- 依赖管理缺失：没有 `requirements.txt` 或 `pyproject.toml`，新环境需要手动安装依赖。
- 平台兼容性：当前代码依赖 Unix API，Windows 上无法直接运行。
- `docs/`、`test/`、`skills/` 目录为空，README 中部分描述与当前文件系统不一致。
- Python 版本：README 要求 3.10+，但当前运行环境可能是 Python 3.8；大量使用 `dict | None` 等 3.10 语法可能在旧版本运行时触发 `TypeError`。
