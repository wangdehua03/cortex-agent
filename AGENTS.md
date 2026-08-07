# Cortex Agent - AI Agent 项目指南

> 本文件面向 AI 编程助手。阅读前请假设你对本项目一无所知。以下描述基于项目当前实际内容，不依赖 README 中的历史说明。

## 1. 项目概述

Cortex Agent 是一个基于大语言模型（LLM）的**智能体协作工具**，采用 **Lead Agent + 异步 SubAgent** 的架构：

- **Lead Agent**（`src/agents/agent.py::Agent`）：任务规划者，负责任务分解、通过 `task_delegate` 委托子任务、整合 SubAgent 返回结果、审批危险命令。
- **AsyncSubAgent**（`src/agents/async_subagent.py`）：在独立线程中执行具体任务，完成后通过 `MessageBus` 向 Lead 发送 `subagent_done` 消息，并等待 `shutdown_request`（确认关闭）或 `revision_request`（修订指令）。
- **MessageBus**（`src/infrastructure/message_bus.py`）：内存中的线程安全消息总线，用于 Agent 间通信。
- **Task Store**（`src/infrastructure/task_store.py`）：内存中的持久化任务管理，支持依赖关系 `blockedBy`。
- **ContextStore**（`src/infrastructure/context_store.py`）：对话历史存储 + 视图层，支持自动摘要压缩与 turn 级压缩。
- **Conversation / SessionManager**（`src/infrastructure/conversation.py`、`src/infrastructure/session.py`）：会话级状态容器与会话管理器，将 Agent 与会话状态解耦。

项目语言以 **中文** 为主（注释、README、终端输出、prompt 均使用中文）。

## 2. 技术栈

- **语言**：Python（代码使用 Python 3.10+ 类型注解语法，如 `dict | None`、`list[str]`）
- **LLM 客户端**：OpenAI 兼容 API（`openai` 库）
- **Tokenizer**：`transformers.AutoTokenizer` + `tiktoken`
- **文件格式**：Excel 读写使用 `openpyxl` / `xlrd`，技能元数据使用 `PyYAML`
- **并发**：Python `threading`
- **平台兼容**：通过 `src/utils/shell_backend.py` 提供跨平台 shell 后端
  - Linux / macOS → `PosixBackend`（bash + `os.setsid` + `pty`）
  - Windows → `WindowsBackend`（PowerShell + `CREATE_NEW_PROCESS_GROUP`）
  - 交互式命令（ssh / docker login 等）在 Windows 上暂不支持伪终端，会返回明确错误。

## 3. 项目结构

```
cortex_agent/
├── main.py                              # 统一入口：单 Agent 交互模式
├── README.md                            # 项目说明（部分内容与当前目录实际状态不一致）
├── .env.example                         # 环境变量配置示例
├── .gitignore                           # Python 标准忽略规则（含 .env、logs/）
├── .vscode/                             # VS Code 调试配置（含硬编码 API 密钥，勿提交）
│   ├── launch.json
│   └── settings.json
├── config/
│   ├── __init__.py
│   ├── config.py                        # LLM 连接、路径、阈值等配置
│   ├── prompts/
│   │   ├── __init__.py
│   │   ├── single.py                    # Lead Agent system prompt
│   │   ├── subagent.py                  # SubAgent system prompt
│   │   └── compact.py                   # Auto Compact 摘要 prompt
│   └── tools/
│       ├── __init__.py                  # 聚合导出 common / lead / subagent 工具
│       ├── common.py                    # 共享工具定义 + handlers（run_shell / 文件 / Excel / send_message）
│       ├── lead.py                      # Lead 专用工具（task_delegate / task CRUD）
│       └── subagent.py                  # SubAgent 专用工具（todo）
├── src/
│   ├── __init__.py
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── agent.py                     # BaseAgent + Agent（Lead）
│   │   └── async_subagent.py            # AsyncSubAgent + spawn_subagent
│   ├── infrastructure/
│   │   ├── __init__.py
│   │   ├── message_bus.py               # MemoryMessageBus + UserInputQueue
│   │   ├── task_store.py                # MemoryTaskManager
│   │   ├── context_store.py             # ContextStore（存储层 + 视图层）
│   │   ├── conversation.py              # Conversation + TokenTracker（会话级状态）
│   │   ├── session.py                   # SessionManager（会话管理器）
│   │   └── clients/
│   │       └── llm_clients.py           # LLMClient（OpenAI 兼容）
│   └── utils/
│       ├── __init__.py
│       ├── function.py                  # 工具实现（文件/Excel 读写、safe_path）
│       ├── shell_backend.py             # 跨平台 shell 后端 + 命令分级清单
│       ├── managers.py                  # TodoManager、SkillLoader
│       └── stdio_redirect.py            # AgentLogger（每 Agent 独立日志）
├── skills/                              # 技能目录（当前为空，没有 SKILL.md）
│   ├── cuda_upgrader/
│   ├── unit_test_generator/
│   └── vllm_model_deployer/
├── docs/                                # 文档目录（当前为空）
├── test/                                # 测试目录（当前为空）
└── logs/                                # 运行日志（.gitignore 忽略）
    ├── lead_agent/
    └── subagents/
```

### 重要现状说明

- `skills/`、`docs/`、`test/` 三个目录**当前为空**，没有 `SKILL.md`、测试文件或文档。
- 根目录**存在 `.git/`**，是 Git 仓库；当前有未提交修改（`README.md`、`AGENTS.md`、`config/config.py`、`main.py` 等处于修改状态）。
- 项目**没有** `pyproject.toml`、`setup.py`、`requirements.txt`、`Pipfile`、`environment.yml`、`package.json`、`Cargo.toml` 等依赖/构建清单文件。
- 当前环境 Python 版本为 3.8.x，但代码使用了大量 Python 3.10+ 语法（如 `dict | None`），在 3.8 上运行会触发 `TypeError`。
- Shell 工具在 JSON schema 中已改名为 `run_shell`，内部兼容旧名 `bash`。

## 4. 运行方式

### 4.1 环境要求

README 中声明需要 **Python 3.10+**，代码实际运行还需要以下第三方包：

- `openai`
- `tiktoken`
- `transformers`
- `openpyxl`
- `xlrd`
- `pyyaml`

> 注意：当前环境为 Python 3.8，直接使用 `python main.py` 会因类型注解语法不兼容而失败；建议在 Python 3.10+ 环境中运行，或先用 `py_compile` 做语法检查。

### 4.2 配置

所有运行配置集中在 `config/config.py`，并可通过环境变量覆盖（推荐）：

- `LLM_BASE_URL` / `KIMI_BASE_URL`：LLM API 基础地址（兼容 OpenAI 协议；支持第三方云端或本地部署）。
- `LLM_API_KEY` / `KIMI_API_KEY`：API 密钥。
- `LLM_MODEL` / `KIMI_MODEL`：模型名称。
- `TOKENIZER_PATH`：本地 HuggingFace tokenizer 目录；**留空则使用 tiktoken (cl100k_base) 近似统计**。
- `WORKDIR`：Agent 执行 bash / 文件操作的工作目录，**默认为项目根目录（`Path.cwd()`）**。
- `LLM_CONTEXT_WINDOW`：上下文窗口大小（按模型自动选择，可被覆盖）。
- `KEEP_RECENT_ROUNDS`：ContextStore 保留的最近完整回合数。
- `TASK_NAG_ROUNDS`：Lead 连续 N 轮未操作任务时触发提醒。
- `AUTO_COMPACT_*`：自动上下文压缩开关与阈值。
- `LOOP_DETECT_*`：循环检测开关与阈值。

环境变量优先级：`LLM_*` > 旧版 `KIMI_*` > `config/config.py` 默认值。

建议直接复制 `.env.example` 为 `.env` 并填写，或运行前 `export` 到 shell。`.env` 已被 `.gitignore` 忽略。

> 运行前至少需要配置 `LLM_API_KEY` / `KIMI_API_KEY` / `API_KEY`；使用本地模型时还需设置 `LLM_BASE_URL` 与 `LLM_MODEL`。

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
- 基础语法检查：`python -m py_compile main.py src/**/*.py config/**/*.py` 可通过。

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
| `src/infrastructure/conversation.py` | `Conversation` / `TokenTracker` | 会话级上下文容器 + token 统计缓存。 |
| `src/infrastructure/session.py` | `SessionManager` | 按 `session_id` 创建 / 查找 / 销毁 `Conversation`。 |
| `src/infrastructure/clients/llm_clients.py` | `LLMClient` | 封装 OpenAI 兼容 API，支持流式输出、token 统计、JSON 修复。 |

### 6.3 工具与配置层

| 文件 | 职责 |
|------|------|
| `config/config.py` | 全局配置：LLM、路径、阈值、开关。 |
| `config/tools/common.py` | 共享工具定义 + handlers（`run_shell`、`read_file`、`write_file`、`edit_file`、`read_excel`、`write_excel`、`send_message`）。 |
| `config/tools/lead.py` | Lead 专用工具：`task_delegate`、`task_create`、`task_update`、`task_list`、`task_get`。 |
| `config/tools/subagent.py` | SubAgent 专用工具：`todo`。 |
| `config/prompts/*.py` | Lead / SubAgent / Auto Compact 的 system prompt。 |
| `src/utils/shell_backend.py` | 跨平台 shell 后端：命令解析、命令分级、进程执行。 |
| `src/utils/function.py` | 工具的具体实现（文件/Excel 读写、路径沙箱）。 |
| `src/utils/managers.py` | `TodoManager`、`SkillLoader`。 |
| `src/utils/stdio_redirect.py` | `AgentLogger`：每个 Agent 独立日志文件。 |

### 6.4 全局单例

- `bus`：全局 `MemoryMessageBus` 实例（`src/infrastructure/message_bus.py`）。
- `tasks`：全局 `MemoryTaskManager` 实例（`src/infrastructure/task_store.py`）。

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
- **TokenTracker**：缓存 API 返回的精确 `prompt_tokens` 与 messages 快照，减少本地 tokenizer 估算误差。

### 7.3 循环检测

- 记录最近 `LOOP_DETECT_WINDOW` 轮的工具调用签名。
- 当连续 `LOOP_REPEAT_THRESHOLD` 次签名相同时，判定为循环，压缩重复轮次并注入 `<loop_detector_intervention>` 用户消息打断。

### 7.4 Bash 权限与安全（跨平台）

`src/utils/shell_backend.py` 按平台维护命令分级清单，`src/utils/function.py` 中 `_validate_command` 统一校验：

1. **白名单命令**：直接执行（如 `ls`、`cat`、`grep`、`python`、`git` 等）。
2. **敏感命令**：需用户确认（如 `wget`、`pip`、`npm`、`kill` 等）。
3. **危险命令**：明确警告后需用户确认（如 `sudo`、`shutdown`、`mkfs`、`systemctl` 等）。

额外规则：

- `rm` / `mv` / `chmod` / `chown` / `dd` 等高风险文件操作，目标路径必须位于 `WORKDIR` 内（`safe_path`）。
- 交互式命令（如 `ssh`、`docker login`）在 POSIX 平台使用 `pty` 运行，期间暂停 `UserInputQueue` 对 stdin 的监听；Windows 暂不支持。
- SubAgent 的危险命令通过 MessageBus 向 Lead 请求权限确认。
- 命令超时 120 秒后会提升为后台线程继续执行，完成后通过 bus 通知结果。

### 7.5 异步 SubAgent 生命周期

1. Lead 调用 `task_delegate` → `spawn_subagent()` 启动独立线程。
2. SubAgent 执行 `todo` + 工具循环。
3. 任务完成后发送 `subagent_done` 给 Lead，进入 `awaiting_review`。
4. Lead 可回复 `shutdown_request`（关闭）或 `revision_request`（修订）。
5. Lead 的 `_idle_drain_inbox` 在空闲时轮询 inbox，处理 SubAgent 消息和权限请求。

### 7.6 会话与状态解耦

- `Agent` 实例不持有 `ContextStore` 或 `Conversation`，由 `main.py` 创建并传入。
- 同一 `Agent` 实例可服务多个 `Conversation`。
- `SessionManager` 按 `session_id` 管理会话，但当前 `main.py` 只使用单个 `default_user` 会话。

## 8. 代码风格与约定

- **注释语言**：中文。
- **命名风格**：函数/变量使用 `snake_case`，类使用 `PascalCase`。
- **类型注解**：大量使用 Python 3.10+ 语法，如 `dict | None`、`list[str]`、`str | None`。
- **字符串**：优先使用双引号，prompt 模板使用三引号字符串。
- **模块组织**：按职责分层（agents / infrastructure / utils / config），配置与实现分离。
- **全局单例模式**：`bus` 和 `tasks` 在模块加载时创建。
- **日志**：每个 Agent 独立写入 `logs/` 目录，不在 stdout 上互相竞争。
- **跨平台 shell 工具名**：JSON schema 中统一为 `run_shell`，描述与后端解耦。

## 9. 开发注意事项

### 9.1 运行前必须修改

- 复制 `.env.example` 为 `.env`（或直接在 shell 中 `export`），设置 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。
  也支持旧版 `KIMI_API_KEY`、`KIMI_MODEL` 等环境变量。
- 若使用本地大模型，建议同时设置 `TOKENIZER_PATH` 指向对应的 HuggingFace tokenizer 目录；
  留空则自动使用 tiktoken (cl100k_base) 近似统计。
- 确认 `WORKDIR` 指向你希望 Agent 操作的真实目录（默认 `Path.cwd()`）。
- 当前环境为 Python 3.8，而代码使用 Python 3.10+ 类型语法；请在 Python 3.10+ 环境中运行，或做兼容性改造。
- `.vscode/settings.json` 与 `.vscode/launch.json` 中硬编码了 API 密钥，**不要提交到公共仓库**。

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
- `test/test_shell_backend.py`：验证 POSIX / Windows 命令解析与分级清单。

## 10. 安全考虑

- **路径沙箱**：文件读写强制限制在 `WORKDIR` 内（`safe_path`）。
- **命令分级**：危险/敏感命令需要用户显式确认。
- **权限委托**：SubAgent 的危险命令不直接由用户确认，而是经 MessageBus 上报给 Lead，再由 Lead 统一询问用户。
- **API 密钥**：`config/config.py` 与 `.vscode/launch.json`、`.vscode/settings.json` 中包含硬编码密钥，**不要在提交时泄露**；生产环境应使用 `.env` 或环境变量注入。
- **后台任务**：`run_shell_command` 超时后会将命令提升为后台线程继续执行，并通过 bus 通知结果；注意这可能产生长时间运行的子进程。
- **.gitignore**：已忽略 `.env`、`.env.*`、`logs/`、`.vscode/` 等敏感/生成文件，但 `.vscode` 目录当前已被 Git 跟踪，需手动确认是否需要移除。

## 11. 已知问题 / 待完善

- 依赖管理缺失：没有 `requirements.txt` 或 `pyproject.toml`，新环境需要手动安装依赖。
- Python 版本不一致：README 要求 3.10+，但当前运行环境为 Python 3.8；大量使用 `dict | None` 等 3.10 语法在旧版本运行时会触发 `TypeError`。
- `docs/`、`test/`、`skills/` 目录为空，README 中部分描述与当前文件系统不一致。
- 交互式命令在 Windows 上暂不支持（缺少伪终端实现）。
- 当前仓库存在未提交修改，且 `.vscode` 中硬编码了 API 密钥，提交前需清理。
