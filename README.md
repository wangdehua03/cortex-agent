# Agent Platform

## 介绍
基于 LLM 的智能体平台，支持 Lead Agent + 异步 SubAgent 的协调协作架构。Lead Agent 负责任务规划、分解和结果整合，SubAgent 在独立线程中异步执行具体任务并通过 MessageBus 回报结果。

## Features

### 已发布技能 (Skills)

智能体平台持续扩展功能，以下是已发布的技能：

| 发布日期 | 技能名称 | 描述 |
|---------|---------|------|
| 2025-05-21 | **unit_test_generator** | 为 Python 项目自动生成标准化单元测试用例 Excel 文件 |
| 2025-06-05 | **cuda_upgrader** | 通过 SSH 远程升级 NVIDIA 驱动和 CUDA Toolkit |
| 2025-06-08 | **vllm_model_deployer** | 通过 SSH + Docker 部署 vLLM 模型服务 |

- **unit_test_generator** — 自动扫描 Python 项目，识别可测试函数和方法；智能优先级排序；生成标准化 Excel 文件
- **cuda_upgrader** — 自动检查当前驱动和 CUDA 版本；静默安装 NVIDIA 驱动和 CUDA Toolkit；自动配置环境变量
- **vllm_model_deployer** — 自动检查服务器环境（Docker、GPU）；智能配置模型参数；支持镜像版本升级；完整健康检查和 API 验证

### 核心特性

### 任务规划与委托
- **Task Store** (`task_create` / `task_update` / `task_list` / `task_get` / `task_delegate`) — 持久化任务管理，支持依赖关系（`blocked_by`）、状态跟踪、任务到 SubAgent 的自动绑定
- **任务 Nag 机制** — Lead Agent 连续多轮未操作任务时自动提醒，确保执行不偏离计划

### 异步 SubAgent
- SubAgent 在独立线程中运行，不阻塞 Lead Agent
- 完成后通过 MessageBus 发送 `subagent_done` 消息回报结果
- 进入 `awaiting_review` 状态，等待 Lead 的 `shutdown_request`（确认关闭）或 `revision_request`（修订指令）
- Bash 危险命令通过 MessageBus 向 Lead 请求权限确认

### 消息总线 (MessageBus)
- 基于内存队列的线程安全消息传递
- 支持消息类型：`message`、`broadcast`、`subagent_done`、`shutdown_request`、`revision_request`、`permission_request`、`permission_response`
- Completion Listener 机制：SubAgent 完成或收到关闭请求时自动触发回调

### 上下文管理
- **Auto Compact** — token 使用超过阈值（默认 80%）时自动调用 LLM 生成对话摘要，压缩上下文
- **Micro Compact** — 基于黑名单策略，对旧轮次中仅使用可丢弃工具（`write_file`、`edit_file`、`bash` 等）的交互进行原地压缩
- **循环检测** — 滑动窗口检测连续重复的工具调用，自动注入中断消息打破死循环

### 用户交互
- **UserInputQueue** — 队列化用户输入，与 inbox 消息统一通过事件轮询消费
- **Steer 机制** — Agent 运行时用户输入进入 steer_queue，在轮边界注入为 user 消息，实现方向纠正
- **Interrupt** — 发送 `/interrupt` 或 `/stop` 可中断当前 Agent 循环
- **Bash 权限控制** — 危险命令（sudo/shutdown 等）和敏感命令（wget/pip/kill 等）需要用户 y/N 确认

### Skills 系统
- 自动发现 `skills/` 目录下所有包含 `SKILL.md` 的技能
- Lead Agent system prompt 中注入技能注册表描述
- `task_delegate` 可通过 `skill_name` 为 SubAgent 加载完整技能工作流

## 软件架构

```
┌─────────────────────────────────────────────────────┐
│                   Lead Agent                         │
│  (coordinator: 规划 → 委托 → 综合 → 验证)             │
│                                                     │
│  Task Store  ── 持久化任务 DAG, 依赖管理              │
│  MessageBus  ── 接收 SubAgent 回报和权限请求          │
│  UserInputQueue ── 队列化用户输入和 steer 消息        │
└───────┬───────────────────────────┬─────────────────┘
        │ task_delegate             │ task_delegate
        ▼                           ▼
┌──────────────────┐    ┌──────────────────┐
│  AsyncSubAgent   │    │  AsyncSubAgent   │
│  (独立线程运行)    │    │  (独立线程运行)    │
│                  │    │                  │
│  - ReAct 循环    │    │  - Todo 跟踪     │
│  - 工具调用      │    │  - Skill 工作流  │
│  - 权限请求      │    │  - 结果回报      │
└──────────────────┘    └──────────────────┘
        │ subagent_done               │ subagent_done
        └─────────┬───────────────────┘
                  ▼
          MessageBus (inbox)
        Lead Agent 读取并处理
```

### 核心组件

| 组件 | 路径 | 职责 |
|------|------|------|
| `Agent` | `src/agents/agent.py` | Lead Agent — 任务规划、委托、inbox 处理、权限审批 |
| `AsyncSubAgent` | `src/agents/async_subagent.py` | 异步 SubAgent — 独立线程执行任务、Todo 跟踪、结果回报 |
| `BaseAgent` | `src/agents/agent.py` | Agent 基类 — ReAct 循环、工具调用、上下文压缩、循环检测 |
| `MemoryMessageBus` | `src/infrastructure/message_bus.py` | 线程安全的消息传递基础设施 |
| `UserInputQueue` | `src/infrastructure/message_bus.py` | 队列化用户输入，支持 steer 和 interrupt |
| `MemoryTaskManager` | `src/infrastructure/task_store.py` | 持久化任务管理，支持依赖关系 |
| `TodoManager` | `src/utils/managers.py` | SubAgent 的轻量级 Todo 跟踪 |
| `SkillLoader` | `src/utils/managers.py` | 技能自动发现和加载 |
| `AgentLogger` | `src/utils/stdio_redirect.py` | 每 agent 独立写日志文件，互不干扰 |

## 目录结构

```
agent_platform/
├── main.py                    # 统一入口
├── config/
│   ├── config.py              # LLM 连接参数、压缩/循环检测阈值
│   ├── prompts/
│   │   ├── single.py          # Lead Agent system prompt
│   │   ├── subagent.py        # SubAgent system prompt
│   │   └── compact.py         # Auto Compact 摘要 prompt
│   └── tools/
│       ├── common.py          # 共享工具：bash, 文件读写, excel, send_message
│       ├── lead.py            # Lead 专用：task_delegate, task CRUD
│       └── subagent.py        # SubAgent 专用：todo
├── src/
│   ├── agents/
│   │   ├── agent.py           # BaseAgent + Agent (Lead)
│   │   └── async_subagent.py  # AsyncSubAgent + spawn_subagent
│   ├── infrastructure/
│   │   ├── message_bus.py     # MemoryMessageBus + UserInputQueue
│   │   ├── task_store.py      # MemoryTaskManager
│   │   └── clients/
│   │       └── llm_clients.py # LLM API 客户端
│   └── utils/
│       ├── function.py        # 工具实现：bash(含权限校验), 文件/Excel 读写
│       ├── managers.py        # TodoManager, SkillLoader
│       └── stdio_redirect.py  # AgentLogger (独立日志文件)
├── skills/                    # 可加载技能
│   ├── unit_test_generator/
│   ├── cuda_upgrader/
│   └── vllm_model_deployer/
├── demos/                     # 功能演示
│   ├── s01_basic_loop.py
│   ├── s02_tool_use.py
│   ├── s03_todo_write.py
│   └── s04_subagent.py
├── test/                      # 测试用例
├── docs/                      # 技术文档
├── logs/                      # Agent 运行日志
└── rsrc/                      # 资源文件（tokenizer 等）
```

## 工具列表

### Lead Agent 工具

| 工具 | 描述 |
|------|------|
| `task_delegate` | 生成异步 SubAgent，可选绑定 skill 和 task_id |
| `task_create` | 创建持久化任务，支持 subject/description |
| `task_update` | 更新任务状态、依赖关系（blocked_by） |
| `task_list` | 列出所有任务，支持按 owner/status 过滤 |
| `task_get` | 获取单个任务的详细信息 |
| `bash` | 执行 shell 命令（带安全校验） |
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件 |
| `edit_file` | 精确替换文件中的文本 |
| `read_excel` | 读取 Excel 文件为 Markdown 表格 |
| `write_excel` | 将 Markdown 表格写入 Excel |
| `send_message` | 向 SubAgent 发送 shutdown_request / revision_request |

### SubAgent 工具

| 工具 | 描述 |
|------|------|
| `todo` | 更新任务清单，跟踪多步骤任务进度 |
| `bash` | 执行 shell 命令（危险命令通过 MessageBus 向 Lead 请求权限） |
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件 |
| `edit_file` | 精确替换文件中的文本 |
| `read_excel` | 读取 Excel 文件为 Markdown 表格 |
| `write_excel` | 将 Markdown 表格写入 Excel |
| `send_message` | 向 Lead 发送消息 |

## 安装和使用

### 环境要求
- Python 3.10+
- 配置 LLM API 连接（修改 `config/config.py`）

### 启动

```bash
python main.py
```

### 终端命令

| 命令 | 功能 |
|------|------|
| `/inbox` | 查看并处理 Lead 的 inbox 消息 |
| `/task_list` | 列出所有任务 |
| `/manual_compact` | 手动触发上下文压缩 |
| `/interrupt` 或 `/stop` | 中断当前 Agent 循环 |
| `q` / `exit` | 退出程序 |

### Bash 权限控制

命令分为三级：
- **白名单**（ls/grep/cat/python/git 等）— 直接执行
- **敏感命令**（wget/pip/npm/kill 等）— 需用户确认
- **危险命令**（sudo/shutdown/mkfs 等）— 明确警告后需用户确认

高风险文件操作（rm/mv/chmod/chown/dd）还需校验路径在工作目录内。

SubAgent 的权限请求通过 MessageBus 转发给 Lead，Lead 再转发给用户，确保跨线程的权限确认。

## 测试

```bash
# 消息总线测试
python test/test_s09_message_bus.py

# Task Store 测试
python test/test_s07_task_store.py

# Idle inbox drain 测试
python test/test_idle_drain.py

# 循环检测测试
python test/test_break_loop.py

# Bash 权限控制测试
python test/test_bash_permission.py
```

## 日志

每个 Agent 独立写入日志文件，不干扰终端输出：

- **Lead Agent**: `logs/lead_agent/lead_{timestamp}.log`
- **SubAgent**: `logs/subagents/{subagent_id}_{timestamp}.log`

## 文档

- [单 Agent 模式技术分享](docs/single_agent_tech_share_final.md) — 架构、ReAct 循环、工具系统、上下文管理的完整技术文档
- [s01 - 基础循环](docs/s01_basic_loop.md)
- [s02 - 工具调用](docs/s02_tool_use.md)
- [s03 - Todo 任务跟踪](docs/s03_todo_write.md)
- [s04 - SubAgent 委托](docs/s04_subagent.md)
