# hczkbot 项目文档

> **hczkbot** 是一个轻量级、开源的 AI Agent 框架，使用 Python 编写，搭配 React/TypeScript WebUI。它围绕一个精简的 agent 循环构建：从聊天渠道接收消息 → 调用 LLM Provider → 执行工具 → 管理会话记忆。

---

## 目录

- [一、项目概览](#一项目概览)
- [二、核心数据流](#二核心数据流)
- [三、模块详解](#三模块详解)
  - [3.1 agent — 智能体核心引擎](#31-agent--智能体核心引擎)
  - [3.2 providers — LLM 提供商抽象层](#32-providers--llm-提供商抽象层)
  - [3.3 channels — 聊天平台集成层](#33-channels--聊天平台集成层)
  - [3.4 bus — 异步消息总线](#34-bus--异步消息总线)
  - [3.5 session — 会话管理](#35-session--会话管理)
  - [3.6 config — 配置系统](#36-config--配置系统)
  - [3.7 api — OpenAI 兼容 HTTP API](#37-api--openai-兼容-http-api)
  - [3.8 cli — 命令行界面](#38-cli--命令行界面)
  - [3.9 command — 斜杠命令路由](#39-command--斜杠命令路由)
  - [3.10 cron — 定时任务](#310-cron--定时任务)
  - [3.11 pairing — DM 发送者审批](#311-pairing--dm-发送者审批)
  - [3.12 security — 安全机制](#312-security--安全机制)
  - [3.14 skills — 内置技能](#314-skills--内置技能)
  - [3.15 templates — 模板文件](#315-templates--模板文件)
  - [3.16 utils — 工具模块](#316-utils--工具模块)
  - [3.17 webui — WebUI 后端服务](#317-webui--webui-后端服务)
  - [3.18 webui/ — React 前端 SPA](#318-webui--react-前端-spa)
  - [3.19 bridge — TypeScript 桥接服务](#319-bridge--typescript-桥接服务)
- [四、配置文件详解](#四配置文件详解)
  - [4.1 配置文件查找顺序](#41-配置文件查找顺序)
  - [4.2 完整配置项说明](#42-完整配置项说明)
  - [4.3 环境变量引用](#43-环境变量引用)
- [五、开发环境配置与启动](#五开发环境配置与启动)
  - [5.1 环境准备](#51-环境准备)
  - [5.2 安装项目](#52-安装项目)
  - [5.3 配置文件初始化](#53-配置文件初始化)
  - [5.4 启动开发环境](#54-启动开发环境)
  - [5.5 开发工作流](#55-开发工作流)
  - [5.6 测试与代码检查](#56-测试与代码检查)
  - [5.7 Docker 部署](#57-docker-部署)

---

## 一、项目概览

| 项目属性 | 值 |
|---------|---|
| **名称** | hczkbot-ai |
| **版本** | 0.2.1 |
| **语言** | Python 3.11+ / TypeScript |
| **许可证** | MIT |
| **构建工具** | hatchling (Python) / Vite + bun (WebUI) |
| **代码风格** | ruff (E, F, I, N, W 规则，E501 忽略)，行宽 100 |
| **测试框架** | pytest (asyncio_mode=auto) / vitest |

### 核心特性

- **多 Provider 支持**：30+ LLM Provider（Anthropic、OpenAI、DeepSeek、智谱、Kimi、通义、Ollama、vLLM 等）
- **多渠道接入**：Telegram、Discord、Slack、飞书、钉钉、QQ、微信、企业微信、Matrix、Email、MS Teams、WebSocket 等
- **工具系统**：文件系统、Shell 执行、Web 搜索/抓取、MCP 服务器、Cron 定时、子代理、图像生成、自我修改等
- **记忆系统**：Dream 两阶段记忆整合（会话级归档 + 周期性长期记忆更新）
- **WebUI**：基于 React + Vite 的富客户端，支持流式输出、活动追踪、技能管理
- **OpenAI 兼容 API**：`/v1/chat/completions`、`/v1/models`

---

## 二、核心数据流

消息流通过异步 `MessageBus`（`hczkbot/bus/queue.py`）解耦聊天渠道与 agent 核心：

```
┌─────────────┐   InboundMessage   ┌─────────────┐   上下文+消息    ┌─────────────┐
│   Channel   │ ─────────────────▶ │  AgentLoop  │ ──────────────▶ │ AgentRunner │
│ (Telegram/  │                    │  (loop.py)  │                 │ (runner.py) │
│  Discord/…) │                    └─────────────┘                 └─────────────┘
└─────────────┘                                                          │
       ▲                                                                  │ LLM 调用 + 工具执行
       │   OutboundMessage                                               ▼
       │ ┌─────────────┐   OutboundMessage   ┌─────────────────────────────┐
       └─│  Channel    │ ◀───────────────── │  MessageBus (outbound)      │
         │  Manager    │                    └─────────────────────────────┘
         └─────────────┘
```

1. **Channels**（`hczkbot/channels/`）从外部平台接收消息，发布 `InboundMessage` 事件到 bus
2. **`AgentLoop`**（`hczkbot/agent/loop.py`）消费 inbound 消息，构建上下文，协调 turn
3. **`AgentRunner`**（`hczkbot/agent/runner.py`）执行 LLM 多轮对话循环：发送消息 → 接收工具调用 → 执行工具 → 流式响应
4. 响应作为 `OutboundMessage` 事件发布回相应渠道

---

## 三、模块详解

### 3.1 agent — 智能体核心引擎

`hczkbot/agent/` 是框架的核心处理引擎，负责消息接收、上下文构建、LLM 调用、工具执行、会话记忆管理。

#### 3.1.1 `loop.py` — AgentLoop 顶层协调者

**核心类：`AgentLoop`**

- **状态机驱动的 turn 处理**：`TurnState` 枚举定义状态流转
  `RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE`
- **`TurnContext`**：承载单次 turn 的全部可变状态（消息、会话、工具、流式回调、trace 等）
- **`_dispatch`**：消息分发入口，实现"会话内串行、跨会话并发"
  - per-session `asyncio.Lock` + 全局 `asyncio.Semaphore`（`HCZKBOT_MAX_CONCURRENT_REQUESTS`，默认 3）
  - 维护 `_pending_queues` 实现 turn 中消息注入
- **`_process_message`**：单条消息处理主循环，驱动状态机直至 `DONE`
- **`_run_agent_loop`**：构建 `AgentProgressHook`、注入回调（checkpoint、drain pending）、调用 `AgentRunner.run`
- **子组件装配**：`ContextBuilder`、`SessionManager`、`ToolRegistry`、`AgentRunner`、`SubagentManager`、`Consolidator`、`AutoCompact`、`CronTurnCoordinator`
- 支持 MCP 服务器懒连接、模型预设热切换、运行时事件总线（`RuntimeEventBus`）

#### 3.1.2 `runner.py` — AgentRunner LLM 调用循环

**核心类：`AgentRunner`**

执行"发送消息给 provider → 接收工具调用 → 执行工具 → 继续多轮"的核心循环。

- **`AgentRunSpec`**：单次执行配置（初始消息、工具注册表、模型、max_iterations、各类回调、超时、目标续跑谓词等）
- **`AgentRunResult`**：执行结果（最终内容、消息列表、工具使用、usage、stop_reason）
- **`run`**：对外入口，包裹 `before_run/after_run/on_error/on_finally` 钩子，处理 `CancelledError`
- **`_run_core`**：迭代主循环（最多 `max_iterations` 次）
  - 每轮做"上下文治理"：`_drop_orphan_tool_results` → `_backfill_missing_tool_results` → `_microcompact` → `_apply_tool_result_budget` → `_snip_history`
  - 请求模型 → 处理工具调用 → 注入 drain
- **`_request_model`**：构造请求参数，区分流式 / 进度流式 / 非流式，处理 LLM 超时（默认 300s，`HCZKBOT_LLM_TIMEOUT_S`）
- **`_execute_tools`**：按批次执行工具，支持并发（`spec.concurrent_tools`）或串行
- **`_try_drain_injections`**：drain 待注入的用户消息，支持 sustained-goal 续跑

#### 3.1.3 `memory.py` — 会话记忆与 Dream 两阶段整合

**两个核心类：**

**(1) `MemoryStore`** — 纯文件 I/O 层
- 管理 `memory/MEMORY.md`（长期记忆）、`memory/history.jsonl`（历史归档）、`SOUL.md`（人格）、`USER.md`（用户画像）
- 使用 `GitStore` 对关键文件做原子写入 + git 版本化
- **Dream cursor**：`.dream_cursor` 文件记录 Dream 已处理到的 history 游标

**(2) `Consolidator`** — 轻量整合器
- **`archive`**：用 LLM 摘要消息并追加到 history；失败时降级为 `raw_archive`（原样转储）
- **`maybe_consolidate_by_tokens`**：循环归档直到 prompt 落入安全预算（`consolidation_ratio` 默认 0.5）
- **`compact_idle_session`**：硬截断空闲会话，保留最近 8 条后缀消息

**Dream 两阶段记忆整合机制：**

- **第一阶段（Consolidation/Archive）**：会话过长或空闲时，`Consolidator.archive` 调用 LLM 把旧消息摘要追加到 `history.jsonl`（"会话→历史"实时整合）
- **第二阶段（Dream）**：通过 cron 周期性触发 Dream agent 运行
  - `build_dream_prompt` 读取 `.dream_cursor` 之后的未处理 history 条目，渲染 `agent/dream.md` 模板
  - `build_dream_tools` 构建受限工具注册表（仅 ReadFile/EditFile/WriteFile/ApplyPatch，可写 SOUL.md/USER.md/skills）
  - Dream agent 据此更新 `MEMORY.md`、`SOUL.md`、`USER.md`、skills 等长期记忆文件
  - 通过 `set_last_dream_cursor` 推进游标

#### 3.1.4 `context.py` — 上下文构建

**核心类：`ContextBuilder`**

组装 system prompt + messages：
- **`build_system_prompt`** 按顺序拼接：
  1. identity（身份模板）
  2. bootstrap 文件（AGENTS.md / SOUL.md / USER.md）
  3. tool_contract 模板
  4. MEMORY.md
  5. always-on skills
  6. skills 摘要
  7. 最近历史（`_MAX_RECENT_HISTORY=50`，`_MAX_HISTORY_TOKENS=8000`）
  8. 会话归档摘要

#### 3.1.5 `hook.py` / `progress_hook.py` — 钩子机制

- **`AgentHook`**：最小生命周期接口
  - 方法：`before_run/after_run/on_error/on_finally`、`before_iteration/after_iteration`、`on_stream/on_stream_end`、`before_execute_tools`、`emit_reasoning/emit_reasoning_end`、`finalize_content`、`wants_streaming`
- **`CompositeHook`**：扇出钩子，按顺序委托给多个 hook
- **`AgentProgressHook`**：把 runner 事件翻译为用户可见的进度信号
  - 持有 `on_progress/on_stream/on_stream_end` 回调
  - 使用 `IncrementalThinkExtractor` 分离 `<think>` 推理与正文

#### 3.1.6 `skills.py` — 技能加载

**核心类：`SkillsLoader`**

加载 markdown 技能文件（`SKILL.md`），教 agent 如何使用特定工具或执行任务：
- 技能来源：workspace `skills/` 目录优先，builtin `hczkbot/skills/` 目录补充（同名 workspace 覆盖 builtin）
- `list_skills`：列出所有技能
- `load_skills_for_context`：加载指定技能并去除 YAML frontmatter
- `build_skills_summary`：构建技能摘要供渐进式加载

#### 3.1.7 `subagent.py` — 子代理管理

**核心类：`SubagentManager`**

管理后台子代理执行：
- `SubagentStatus`：实时状态（phase: initializing/awaiting_tools/tools_completed/final_response/done/error）
- 维护 `_running_tasks`、`_task_statuses`、`_session_tasks`
- 支持 `max_concurrent_subagents` 并发上限
- 内部持有独立的 `AgentRunner` 和受限的 `ToolsConfig`

#### 3.1.8 `autocompact.py` — 自动压缩

**核心类：`AutoCompact`**

主动压缩空闲会话以降低 token 成本与延迟：
- `check_expired`：扫描所有会话，对超过 TTL 且无在途任务的会话调度后台归档
- `prepare_session`：会话激活时返回归档摘要

#### 3.1.9 `model_presets.py` — 模型预设

运行时模型预设选择辅助：
- `configured_model_presets`：合并用户预设与 default 预设
- `build_static_preset_snapshot` / `build_runtime_preset_snapshot`：构建 `ProviderSnapshot`

#### 3.1.10 `tools/` 子目录 — 工具系统

**核心基础设施：**

| 文件 | 作用 |
|------|------|
| `base.py` | `Tool` 抽象基类与 `Schema` 抽象基类，`tool_parameters` 装饰器绑定参数 schema |
| `schema.py` | 具体 Schema 类型：`StringSchema`、`IntegerSchema`、`ArraySchema`、`ObjectSchema` 等 |
| `registry.py` | `ToolRegistry` — 动态工具注册与执行，`get_definitions`、`prepare_call`（含"Did you mean"建议） |
| `loader.py` | `ToolLoader` — 通过 `pkgutil.iter_modules` 扫描包自动发现工具类，支持 entry_points 插件 |
| `context.py` | `RequestContext`（per-request 上下文）、`ToolContext`（工具构造上下文），使用 `ContextVar` 实现 session-safe 共享 |
| `path_utils.py` | `resolve_workspace_path` — 路径解析与 workspace 包含约束 |
| `file_state.py` | `FileStates` — per-session 文件读写追踪，用于"read-before-edit"警告与读去重 |
| `runtime_state.py` | `RuntimeState` Protocol — `MyTool` 所需的最小 agent loop 状态契约 |
| `exec_session.py` | 长运行 exec 工作流的会话支持 |

**具体工具：**

| 文件 | 工具名 | 作用 |
|------|--------|------|
| `filesystem.py` | read_file / write_file / edit_file / list_dir | 文件系统操作，含 workspace 约束、read-before-edit 追踪 |
| `shell.py` | exec | Shell 命令执行，含沙箱（bwrap）、超时、allow/deny 模式 |
| `web.py` | web_search / web_fetch | Web 搜索（duckduckgo/bocha/volcengine）与网页抓取（可选 Jina Reader） |
| `search.py` | find_files / grep | 文件发现与 grep，按语言映射 glob 模式 |
| `mcp.py` | mcp_* | MCP 客户端，连接 MCP 服务器并把其工具包装为原生工具 |
| `cron.py` | cron | 调度提醒与周期任务，支持 add/list/remove |
| `long_task.py` | long_task / complete_goal | Codex 风格 sustained goal，多轮持续目标追踪 |
| `spawn.py` | spawn | 创建后台子代理 |
| `self.py` | my | 运行时状态检查与配置（model/iterations/context window 等） |
| `image_generation.py` | generate_image | 图像生成 |
| `apply_patch.py` | apply_patch | 结构化文件编辑 |
| `message.py` | send_message | 主动/跨渠道发消息 |
| `cli_apps.py` | cli_apps | 受控运行已安装的 CLI Apps（如 gimp/safari/obsidian） |
| `sandbox.py` | — | Shell 命令沙箱后端（当前实现 `_bwrap`） |

---

### 3.2 providers — LLM 提供商抽象层

`hczkbot/providers/` 封装各类 LLM provider，统一基于 `LLMProvider` 基类。

#### 3.2.1 `base.py` — Provider 基类

**核心类：`LLMProvider`**

- **重试策略**：`_CHAT_RETRY_DELAYS=(1,2,4)`、`_PERSISTENT_MAX_DELAY=60`、`_RETRYABLE_STATUS_CODES={408,409,429}`
  - 区分可重试与不可重试的 429（`_NON_RETRYABLE_429_ERROR_TOKENS` 含 insufficient_quota 等）
- **瞬时错误识别**：`_TRANSIENT_ERROR_MARKERS`（含中英文）
- **消息清洗**：`_sanitize_empty_content`、`_sanitize_request_messages`、`_tool_cache_marker_indices`
- **抽象方法**：`chat`（非流式）、`chat_stream`（流式）、`get_default_model`
- **重试包装**：`chat_with_retry`、`chat_stream_with_retry`
- **流式空闲超时**：`resolve_stream_idle_timeout_s`（默认 90s，env `HCZKBOT_STREAM_IDLE_TIMEOUT_S`）

**关键数据类：**
- `ToolCallRequest`：LLM 工具调用请求
- `LLMResponse`：LLM 响应（content/tool_calls/finish_reason/usage/retry_after/reasoning_content + 结构化错误元数据）
- `GenerationSettings`：默认生成参数

#### 3.2.2 `factory.py` — 实例化工厂

- **`ProviderSnapshot`**：不可变快照（provider + model + context_window_tokens + signature），用于检测 provider 变化触发 runner 重建
- **`_make_provider_core`**：根据 `backend` 字段分发——`openai_codex`/`azure_openai`/`github_copilot`/`anthropic`/`bedrock`/`openai_compat`（默认）
- 校验 api_key/api_base 缺失情况，处理 OAuth/local/direct 豁免

#### 3.2.3 `registry.py` — 模型发现与 provider 元数据

- **`ProviderSpec`**：provider 元数据（name/keywords/env_key/display_name/backend/is_gateway/is_local 等）
- **`PROVIDERS` 注册表**：顺序即匹配优先级，包含 30+ provider
- 辅助函数：`find_by_name`、`create_dynamic_spec`

#### 3.2.4 具体 Provider 实现

| 文件 | Provider | 说明 |
|------|----------|------|
| `anthropic_provider.py` | `AnthropicProvider` | 原生 Anthropic SDK 集成 Claude，处理 prompt caching、extended thinking、tool calls |
| `openai_compat_provider.py` | `OpenAICompatProvider` | 所有非 Anthropic LLM API 的通用 OpenAI 兼容 provider，集成 Responses API |
| `openai_responses/` | — | OpenAI Responses API 共享助手（converters.py + parsing.py） |
| `azure_openai_provider.py` | `AzureOpenAIProvider` | Azure OpenAI，双认证（API key + Microsoft Entra ID/AAD） |
| `bedrock_provider.py` | `BedrockProvider` | AWS Bedrock Runtime Converse API |
| `github_copilot_provider.py` | `GitHubCopilotProvider` | OAuth 背书，使用 `oauth_cli_kit` 管理 GitHub OAuth token |
| `openai_codex_provider.py` | `OpenAICodexProvider` | 使用 Codex OAuth 调用 Responses API |
| `fallback_provider.py` | `FallbackProvider` | 透明故障转移包装器，含熔断器（`_PRIMARY_FAILURE_THRESHOLD=3`、`_PRIMARY_COOLDOWN_S=60`） |

#### 3.2.5 辅助 Provider

- **`image_generation.py`**：图像生成 provider 辅助，覆盖 OpenRouter/AiHubMix/Gemini/Ollama 各家
- **`transcription.py`**：语音转写适配器，支持 Groq、OpenAI Whisper、OpenRouter、Xiaomi MiMo ASR、AssemblyAI、StepFun ASR

---

### 3.3 channels — 聊天平台集成层

`hczkbot/channels/` 实现各聊天平台的接入。

#### 核心组件

**`base.py` — `BaseChannel` 抽象基类**
- 定义统一接口：`start()` / `stop()` / `send()` 三个抽象方法
- 流式输出能力：`send_delta()`（文本流）、`send_reasoning_delta()`/`send_reasoning_end()`（推理流）、`send_file_edit_events()`
- 权限控制 `is_allowed()`：优先级为 `*` 通配 > allowFrom 白名单 > pairing 审批库 > 拒绝
- `_handle_message()` 是入站消息统一入口

**`manager.py` — `ChannelManager` 通道管理器**
- 负责初始化、启停、路由出站消息
- 通过 `registry.discover_channel_names()` + `discover_enabled()` 仅导入启用的通道
- 出站消息分发带指数退避重试（1s/2s/4s）

**`registry.py` — 自动发现机制**
- `discover_channel_names()`：用 `pkgutil.iter_modules` 零导入扫描内置通道模块名
- `discover_plugins()`：通过 `entry_points(group="hczkbot.channels")` 发现外部插件

#### 各 Channel 实现

| Channel | 文件 | 说明 |
|---------|------|------|
| **telegram** | `telegram.py` | 基于 `python-telegram-bot`；支持 HTML 渲染、InlineKeyboard、消息拆分 |
| **discord** | `discord.py` | 基于 `discord.py`；20MB 附件限制、2000 字符限制 |
| **slack** | `slack.py` | 基于 Socket Mode（WebSocket）；含 DM 策略配置 |
| **feishu** | `feishu.py` | 飞书/Lark，基于 `lark-oapi` SDK 的 WebSocket 长连接 |
| **matrix** | `matrix.py` | Element/Matrix，基于 `nio` 异步客户端 |
| **qq** | `qq.py` | QQ 频道，基于 `botpy` SDK |
| **weixin** | `weixin.py` | 个人微信，基于 HTTP 长轮询 API |
| **wecom** | `wecom.py` | 企业微信，基于 `wecom_aibot_sdk` |
| **dingtalk** | `dingtalk.py` | 钉钉，基于 Stream Mode |
| **email** | `email.py` | IMAP 轮询收信 + SMTP 回信 |
| **msteams** | `msteams.py` | MS Teams，内置 HTTP webhook 服务器 |
| **whatsapp** | `whatsapp.py` | 基于 Node.js bridge 子进程 |
| **signal** | `signal.py` | 基于 `signal-cli` daemon 的 JSON-RPC 接口 |
| **websocket** | `websocket.py` | hczkbot 作为 WebSocket 服务器，是 WebUI 的核心传输层 |
| **mochat** | `mochat.py` | 基于 Socket.IO + HTTP polling fallback |
| **napcat** | `napcat.py` | NapCat（OneBot v11）QQ 协议 |

---

### 3.4 bus — 异步消息总线

`hczkbot/bus/` 是核心解耦组件。

**`queue.py` — `MessageBus`**
- 两个 `asyncio.Queue`：`inbound` + `outbound`
- 通道 `publish_inbound()` 推入，AgentLoop `consume_inbound()` 阻塞消费
- Agent `publish_outbound()` 推入，ChannelManager `consume_outbound()` 分发

**`events.py` — 事件数据类**
- `InboundMessage`：含 channel/sender_id/chat_id/content/media/metadata，`session_key` 属性默认 `channel:chat_id`
- `OutboundMessage`：含 reply_to/media/buttons/metadata

**`progress.py` — 进度回调**
- `build_bus_progress_callback()` 将 agent 进度回调转换为 outbound 消息
- 支持 tool_hint、tool_events、file_edit_events、reasoning 等多种进度类型

**`runtime_events.py` — 运行时事件总线**
- 独立于消息总线，用于进程内状态通知（WebUI 订阅渲染）
- 事件类型：`SessionTurnStarted`、`TurnRunStatusChanged`、`TurnCompleted`、`GoalStateChanged`、`RuntimeModelChanged`

---

### 3.5 session — 会话管理

`hczkbot/session/` 提供会话历史与状态管理。

**`manager.py` — `SessionManager` / `Session`**
- `Session` dataclass：key、messages、created_at/updated_at、metadata、`last_consolidated`
- 文件持久化，单文件最多 2000 条消息
- 会话列表预览：最多 200 条记录、100 万字符
- Fork 时清除易失元数据

**`goal_state.py` — 持续目标状态**
- 管理 `long_task`/`complete_goal` 的会话元数据
- `sustained_goal_active()` 判断是否有活跃目标
- `sustained_goal_turn()` 判断当前 turn 是否应使用持续目标运行时限制

**`keys.py` — 会话键常量**
- `UNIFIED_SESSION_KEY = "unified:default"` 用于统一会话模式
- `session_key_for_channel()` 根据 `unified_session` 标志返回统一键或 `channel:chat_id`

**`turn_continuation.py` — 内部 turn 续接**
- 处理预算边界续接策略
- 持续目标最多续接 12 轮（`_MAX_GOAL_CONTINUATION_ROUNDS`）

**`webui_turns.py` — WebUI 会话 turn 辅助**
- 管理 WebUI 标记、标题生成（最多 60 字符）

---

### 3.6 config — 配置系统

`hczkbot/config/` 基于 Pydantic 的配置系统。

**`loader.py` — 配置加载**
- 支持 YAML（`.yaml`/`.yml`）和 JSON 双格式
- 优先级：已设置路径 > config.yaml > config.yml > config.json
- 全局 `_current_config_path` 支持多实例
- `resolve_config_env_vars()` 解析 `${VAR}` 环境变量引用
- `_migrate_config()` 迁移旧配置格式

**`schema.py` — Pydantic 配置 Schema**
- `Config`（根配置）：agents / channels / transcription / providers / api / gateway / tools / model_presets
- `AgentDefaults`：workspace / model / provider / max_tokens / context_window_tokens / temperature / dream 等
- `ProvidersConfig`：30+ 内置 provider + 自定义 provider（通过 `extra="allow"`）
- `ToolsConfig`：web / exec / file / my / image_generation / cli_apps / mcp_servers / ssrf_whitelist
- `ChannelsConfig`：send_progress / send_tool_hints / show_reasoning / extract_document_text
- 支持 camelCase 别名（`AliasChoices`）兼容 JSON

**`paths.py` — 运行时路径**
- 所有路径基于 `get_config_path().parent`（即 `~/.hczkbot/`）
- `get_data_dir()` / `get_runtime_subdir(name)` 派生 media/cron/logs/webui 子目录
- `get_workspace_path()` 默认 `~/.hczkbot/workspace`

---

### 3.7 api — OpenAI 兼容 HTTP API

`hczkbot/api/server.py`

- 提供 `/v1/chat/completions` 和 `/v1/models` 端点
- 所有请求路由到单一持久会话 `api:default`
- 基于 `aiohttp.web`，支持 SSE 流式响应
- 复用 `media_decode` 模块处理 base64 data URL 媒体保存（10MB 上限）

---

### 3.8 cli — 命令行界面

`hczkbot/cli/`

**`commands.py` — CLI 主入口**
- 基于 `typer` + `prompt_toolkit` + `rich`
- 主要命令：
  - `hczkbot onboard` — 初始化配置和工作区
  - `hczkbot gateway` — 启动网关（含 WebUI、channels、agent）
  - `hczkbot serve` — 启动 OpenAI 兼容 API 服务器
  - `hczkbot agent` — 直接与 agent 对话
  - `hczkbot status` — 显示状态

**`onboard.py` — 交互式引导**
- 基于 `questionary` + `rich` 的引导问卷

**`stream.py` — 流式渲染**
- 基于 `rich.live.Live` 实现原地 markdown 更新
- `ThinkingSpinner`：显示 "hczkbot is thinking..." 带暂停

---

### 3.9 command — 斜杠命令路由

`hczkbot/command/`

**`router.py` — `CommandRouter`**
- 纯字典分派，三层优先级：
  1. **priority**：精确匹配，在 dispatch lock 之前处理（如 `/stop`、`/restart`）
  2. **exact**：精确匹配，在 lock 内处理
  3. **prefix**：最长前缀优先（如 `/team `）

**`builtin.py` — 内置命令**
- 内置命令：`/new`、`/stop`、`/restart`、`/status`、`/model [preset]`、`/history [n]`、`/goal` 等

---

### 3.10 cron — 定时任务

`hczkbot/cron/`

**`types.py` — 数据类型**
- `CronSchedule`：三种调度 kind — `at`（一次性时间戳）、`every`（间隔 ms）、`cron`（cron 表达式 + 时区）
- `CronPayload`：执行内容，含 `kind`（system_event/agent_turn）、message、session_key/origin_channel/origin_chat_id

**`service.py` — `CronService`**
- 基于 `filelock` 的文件锁持久化
- `_compute_next_run()` 计算下次运行时间（cron 用 `croniter` + `ZoneInfo`）

**`bound_runner.py` — 会话绑定执行**
- `BoundCronAgent` Protocol 定义 `submit_cron_turn` 接口
- `_bound_session_delivery_context()` 路由回原会话

---

### 3.11 pairing — DM 发送者审批

`hczkbot/pairing/store.py`

- 持久化于 `~/.hczkbot/pairing.json`
- 含 `approved`（已批准，按通道分组）和 `pending`（待审批 code）
- 配对码格式：8 位大写字母+数字（如 `ABCD-EFGH`），默认 TTL 600 秒
- `generate_code()` 生成新码，`is_approved()` 检查批准状态

---

### 3.12 security — 安全机制

`hczkbot/security/`

**`network.py` — SSRF 防护**
- `_BLOCKED_NETWORKS` 封锁私有/链路本地/云元数据地址（10.0.0.0/8、127.0.0.0/8、169.254.0.0/16、fc00::/7 等）
- `configure_ssrf_whitelist()` 允许特定 CIDR 绕过（如 Tailscale 100.64.0.0/10）

**`workspace_access.py` — 工作区访问作用域**
- `WorkspaceAccessMode`：`restricted` / `full`
- `ContextVar` `_CURRENT_WORKSPACE_SCOPE` 支持上下文级隔离

**`workspace_policy.py` — 路径边界**
- 应用层守卫（非 OS 沙箱替代）
- `resolve_path()` 相对路径解析到 workspace
- `require_path_within()` 越界抛 `WorkspaceBoundaryError`

---

### 3.14 skills — 内置技能

`hczkbot/skills/` 每个技能目录含 `SKILL.md`（YAML frontmatter + markdown 指导）：

| 技能 | 作用 |
|------|------|
| **clawhub** | 从 ClawHub 公共技能注册表搜索/安装 agent skills |
| **cron** | 定时提醒与循环任务（Reminder/Task/One-time 三种模式） |
| **github** | 通过 `gh` CLI 操作 GitHub（PR/issue/CI run/api） |
| **image-generation** | 调用 `generate_image` 工具生成/迭代编辑图片 |
| **long-goal** | 通过 `long_task`/`complete_goal` 实现多轮持续目标 |
| **memory** | 两层记忆系统（Dream 管理 SOUL.md/USER.md/MEMORY.md + history.jsonl） |
| **my** | 自省工具：检查/设置运行时状态（`always: true` 常驻） |
| **skill-creator** | 创建/更新 AgentSkills 的指导 |
| **summarize** | 用 `summarize` CLI 总结 URL/文件/YouTube |
| **tmux** | 远程控制 tmux 会话（发送按键 + 抓取 pane 输出） |
| **update-setup** | 一次性设置向导，生成个性化升级技能 |
| **weather** | 通过 wttr.in / Open-Meteo 获取天气 |

---

### 3.15 templates — 模板文件

`hczkbot/templates/`

**顶层模板**
- `AGENTS.md`：工作区级 agent 指导
- `HEARTBEAT.md`：周期性任务清单，由 gateway 的 protected heartbeat cron job 读取
- `SOUL.md`：agent 人格与执行原则
- `USER.md`：用户画像模板

**`agent/` 子目录**
- `identity.md`：运行时身份模板（Jinja2），注入 runtime/workspace_path/memory 路径
- `dream.md`：Dream 记忆整合引擎 prompt
- `evaluator.md`：通知门控 prompt
- `tool_contract.md`：工具使用契约
- `subagent_system.md` / `subagent_announce.md`：子 agent 系统 prompt 与公告模板
- `platform_policy.md`：平台策略
- `skills_section.md`：技能列表注入段
- `_snippets/untrusted_content.md`：不可信内容处理片段

---

### 3.16 utils — 工具模块

`hczkbot/utils/`

| 文件 | 作用 |
|------|------|
| `helpers.py` | 核心工具函数：`strip_think()`、`estimate_message_tokens()`、`split_message()`、`safe_filename()`、`sync_workspace_templates()` |
| `prompt_templates.py` | Jinja2 模板渲染（`render_template()`） |
| `runtime.py` | 运行时常量与提示词 |
| `llm_runtime.py` | `LLMRuntime` dataclass（provider+model） |
| `evaluator.py` | heartbeat 后通知评估 |
| `gitstore.py` | 基于 dulwich 的 Git 版本控制（memory 文件） |
| `media_decode.py` | base64 data URL 解码到磁盘（10MB 上限） |
| `artifacts.py` | 生成媒体 artifact 持久化 |
| `document.py` | 文档文本提取（PDF/DOCX/XLSX/PPTX/TXT/MD/CSV/JSON/XML/HTML） |
| `tool_hints.py` | 工具调用的人类可读提示格式化 |
| `searchusage.py` | Web 搜索 provider 用量查询 |
| `restart.py` | 重启通知辅助 |
| `path.py` | 路径缩写 |
| `logging_bridge.py` | 库日志重定向 |

---

### 3.17 webui — WebUI 后端服务

`hczkbot/webui/` 提供 WebUI 后端服务。

**`gateway_services.py`** — `GatewayServices` 聚合 http/tokens/media/transcripts/workspaces/session_manager/cron_service

**`settings_api.py`** — 设置 REST，集成 transcription/image_gen provider 解析、token_usage、workspace sandbox 状态

**`skills_api.py`** — 技能 API，返回技能列表与详情

**`mcp_presets_api.py`** — MCP server 配置预设管理

**`media_api.py`** — HMAC 签名媒体 URL 生成与验证

**`session_automations.py`** — 会话自动化（绑定到 WebUI 会话的 cron job）

**其他后端文件**
- `transcript.py`：追加式 WebUI 显示 transcript（JSONL，schema v3）
- `workspaces.py`：WebUI 项目工作区状态持久化
- `forking.py`：WebUI chat fork 编排
- `token_usage.py`：工作区级 token 用量遥测
- `ws_http.py`：`GatewayHTTPHandler` 网关 HTTP 处理器
- `gateway_tokens.py`：`GatewayTokenStore` WebSocket token 管理

---

### 3.18 webui/ — React 前端 SPA

`webui/` Vite + React + TypeScript 单页应用。

**整体架构**
- `main.tsx` 入口（StrictMode 禁用以避免 delta 累积双调用）
- 通过 WebSocket 多路复用协议与 gateway 通信
- 构建输出到 `../hczkbot/web/dist`（打包进 Python wheel）

**目录结构**

- `src/components/`：
  - `thread/`：会话核心组件（ThreadShell、ThreadMessages、ThreadComposer、ThreadHeader、PromptRail、AgentActivityCluster、activity/）
  - `settings/`：SettingsView、SkillsCatalogSettings、TokenUsageHeatmap
  - `ui/`：shadcn/ui 基础组件
  - 顶层：Sidebar、ChatList、MessageBubble、MarkdownText、CodeBlock、AttachmentTile、FilePreviewPanel、ImageLightbox、SessionSearchDialog 等
- `src/hooks/`：useHczkbotStream、useSessions、useSkills、useTheme、useSidebarState、useAttachedImages、useClipboardAndDrop、useVoiceRecorder 等
- `src/lib/`：hczkbot-client（WebSocket 客户端）、api（REST 封装）、runtime、bootstrap、types、activity-timeline、tool-traces 等
- `src/i18n/`：支持 zh-CN（简体）/ zh-TW（繁体）
- `src/providers/ClientProvider.tsx`：React Context 提供 `HczkbotClient`

**构建配置**
- `vite.config.ts`：dev server 代理 `/api`、`/webui`、`/auth` 到 gateway :8765
- `tailwind.config.js` + `postcss.config.js`：Tailwind CSS
- `components.json`：shadcn/ui 配置

---

### 3.19 bridge — TypeScript 桥接服务

`bridge/` TypeScript 服务，打包进 wheel 通过 `pyproject.toml` `force-include`：
- `whatsapp.ts`：WhatsApp bridge 子进程
- `server.ts`：bridge 服务器
- `index.ts`：入口

---

## 四、配置文件详解

### 4.1 配置文件查找顺序

hczkbot 自动按以下顺序查找配置文件：

1. 命令行 `--config` 指定的路径
2. `~/.hczkbot/config.yaml`
3. `~/.hczkbot/config.yml`
4. `~/.hczkbot/config.json`

支持 YAML 和 JSON 两种格式，根据文件扩展名自动判断。

### 4.2 完整配置项说明

配置文件采用 Pydantic Schema 定义，支持 camelCase 别名（兼容 JSON）。以下是完整配置示例（YAML 格式）：

```yaml
# ============================================================================
# 智能体默认设置
# ============================================================================
agents:
  defaults:
    workspace: ~/.hczkbot/workspace       # 工作区目录
    modelPreset: null                      # 默认模型预设名（优先级高于 model/provider）
    model: anthropic/claude-opus-4-5       # 默认模型（格式：provider/model）
    provider: auto                         # Provider 选择策略：auto 或指定名
    maxTokens: 8192                        # LLM 生成最大 token
    contextWindowTokens: 65536             # 上下文窗口大小
    temperature: 0.1                       # 温度
    maxToolIterations: 200                 # 单轮对话最大工具调用次数
    maxConcurrentSubagents: 1              # 最大并发子代理数
    maxToolResultChars: 16000              # 工具结果字符上限
    providerRetryMode: standard            # 重试策略：standard / persistent
    reasoningEffort: null                  # 推理强度：low/medium/high/adaptive/none
    timezone: Asia/Shanghai                # 时区（影响 cron 任务、时间显示）
    botName: hczkbot                       # 智能体显示名
    botIcon: "🐈"                          # 智能体图标
    unifiedSession: false                  # true 时跨渠道共享单一会话
    sessionTtlMinutes: 15                  # 空闲会话自动压缩阈值（分钟，0=禁用）
    maxMessages: 120                       # 历史回放消息上限
    consolidationRatio: 0.5                # 上下文压缩目标比例
    disabledSkills: []                     # 禁用的 skill 列表
    dream:
      enabled: true                        # 启用 Dream 记忆整合
      intervalH: 2                         # 整合间隔（小时）

# ============================================================================
# LLM Provider 配置
# ============================================================================
providers:
  anthropic:
    apiKey: ${ANTHROPIC_API_KEY}
  openai:
    apiKey: ${OPENAI_API_KEY}
  deepseek:
    apiKey: ${DEEPSEEK_API_KEY}
  zhipu:
    apiKey: ${ZHIPU_API_KEY}
  moonshot:
    apiKey: ${MOONSHOT_API_KEY}
  dashscope:
    apiKey: ${DASHSCOPE_API_KEY}
  openrouter:
    apiKey: ${OPENROUTER_API_KEY}
  volcengine:
    apiKey: ${VOLCENGINE_API_KEY}
  ollama:                                  # 本地模型，无需 apiKey
    apiBase: http://127.0.0.1:11434/v1
  # 自定义 OpenAI 兼容 provider
  # custom:
  #   apiKey: ${CUSTOM_API_KEY}
  #   apiBase: https://your-api-endpoint/v1

# ============================================================================
# 模型预设
# ============================================================================
modelPresets:
  default:
    label: "默认模型"
    model: anthropic/claude-opus-4-5
    provider: anthropic
    maxTokens: 8192
    contextWindowTokens: 65536
    temperature: 0.1
  high-performance:
    label: "高性能"
    model: anthropic/claude-opus-4-5
    provider: anthropic
    maxTokens: 16384
    contextWindowTokens: 200000
    temperature: 0.1
    reasoningEffort: high
  economy:
    label: "经济模式"
    model: deepseek/deepseek-chat
    provider: deepseek
    maxTokens: 4096
    contextWindowTokens: 65536
    temperature: 0.3
  local:
    label: "本地模型"
    model: ollama/qwen2.5:14b
    provider: ollama
    maxTokens: 4096
    contextWindowTokens: 32768

# ============================================================================
# 工具配置
# ============================================================================
tools:
  web:
    search:
      enable: true
      provider: duckduckgo                 # duckduckgo / bocha / volcengine
      maxResults: 5
      timeout: 15
    fetch:
      enable: true
      useJinaReader: false                 # 使用 Jina Reader 优化网页抓取
  exec:
    enable: true
    timeout: 60                            # 默认超时（秒，最大 600）
    sandbox: null                          # 沙箱后端：null / bwrap
    restrictToWorkspace: false             # 限制命令在工作区内执行
  file:
    enable: true
  my:
    enable: true
    allowSet: true                         # 允许 agent 修改自身配置
  imageGeneration:
    enabled: false                         # 默认关闭
    provider: openrouter
    model: black-forest-labs/flux-1-dev
    maxImagesPerTurn: 1
  cliApps:
    enable: false
  restrictToWorkspace: false               # 是否限制文件操作在工作区内
  webuiAllowLocalServiceAccess: true       # WebUI 是否允许访问本地服务
  ssrfWhitelist: []                        # SSRF 白名单（CIDR 格式）
  mcpServers: {}                           # MCP 服务器配置
    # filesystem:
    #   type: stdio
    #   command: npx
    #   args: ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
    #   enabledTools: ["*"]
    # remote-api:
    #   type: streamableHttp
    #   url: http://127.0.0.1:8080/mcp
    #   headers:
    #     Authorization: "Bearer token"
    #   toolTimeout: 30

# ============================================================================
# 聊天渠道配置
# ============================================================================
channels:
  sendProgress: true                       # 发送工具执行进度
  sendToolHints: true                      # 发送工具调用提示
  showReasoning: true                      # 显示推理过程
  extractDocumentText: true                # 自动提取文档文本
  sendMaxRetries: 3                        # 发送失败重试次数
  websocket:                               # WebSocket 渠道（WebUI 使用）
    enabled: true
  # telegram:
  #   enabled: true
  #   token: ${TELEGRAM_BOT_TOKEN}
  #   allowFrom: []                       # 允许的用户 ID 列表
  # discord:
  #   enabled: true
  #   token: ${DISCORD_BOT_TOKEN}
  # feishu:
  #   enabled: true
  #   appId: ${FEISHU_APP_ID}
  #   appSecret: ${FEISHU_APP_SECRET}
  # dingtalk:
  #   enabled: true
  #   clientId: ${DINGTALK_CLIENT_ID}
  #   clientSecret: ${DINGTALK_CLIENT_SECRET}

# ============================================================================
# 音频转录配置
# ============================================================================
transcription:
  enable: true
  provider: openai                         # openai / assemblyai
  model: whisper-1
  apiKey: ${OPENAI_API_KEY}

# ============================================================================
# API 服务器配置（hczkbot serve 使用）
# ============================================================================
api:
  host: 127.0.0.1                          # 绑定地址
  port: 8900                               # 端口
  timeout: 120.0                           # 单次请求超时（秒）

# ============================================================================
# 网关配置（hczkbot gateway 使用）
# ============================================================================
gateway:
  host: 127.0.0.1
  port: 8765                               # 网关端口
  heartbeat:
    enabled: false                         # 心跳机制
    intervalS: 300                         # 检查间隔（秒）
    keepRecentMessages: 8                  # 保留的最近消息数
```

### 4.3 环境变量引用

可在任意字符串值中使用 `${VAR_NAME}` 引用环境变量，加载时会自动替换。例如：

```yaml
providers:
  anthropic:
    apiKey: ${ANTHROPIC_API_KEY}
```

**注意**：引用的环境变量必须已设置，否则会报错。

此外，`Config` 类支持 `HCZKBOT_` 前缀的环境变量覆盖（`env_nested_delimiter="__"`），例如：
- `HCZKBOT_AGENTS__DEFAULTS__MODEL=deepseek/deepseek-chat`
- `HCZKBOT_GATEWAY__PORT=9000`

---

## 五、开发环境配置与启动

### 5.1 环境准备

#### Python 环境（后端）

```bash
# 需要 Python 3.11+
python3 --version

# 推荐使用 uv（快速）或 venv
# 方式 1：使用 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 方式 2：使用 venv
python3 -m venv ~/.hczkbot/venv
source ~/.hczkbot/venv/bin/activate
```

#### Node.js 环境（前端 WebUI）

```bash
# 需要 Node.js 18+ 和 bun（推荐）
# 安装 bun
curl -fsSL https://bun.sh/install | bash

# 或使用 npm
node --version
npm --version
```

#### 系统依赖（可选）

根据需要启用的渠道和功能安装：

```bash
# macOS
brew install gh tmux                    # GitHub CLI、tmux（用于 tmux skill）

# Ubuntu/Debian
sudo apt install -y gh tmux

# 可选：本地 LLM
# 安装 Ollama: https://ollama.com/download
```

### 5.2 安装项目

#### 方式 1：开发模式安装（推荐用于开发）

```bash
cd /Volumes/data/hczkAgent/hczkbot

# 使用 uv（推荐）
uv pip install -e ".[dev]"

# 或使用 pip
pip install -e ".[dev]"
```

`[dev]` extra 包含：pytest、pytest-asyncio、aiohttp、pytest-cov、ruff、pymupdf。

#### 方式 2：按需安装可选依赖

```bash
# Azure OpenAI（AAD 认证）
pip install -e ".[azure]"

# Discord
pip install -e ".[discord]"

# Matrix
pip install -e ".[matrix]"

# 企业微信
pip install -e ".[wecom]"

# 个人微信
pip install -e ".[weixin]"

# MS Teams
pip install -e ".[msteams]"

# PDF 解析
pip install -e ".[pdf]"

# API 服务器
pip install -e ".[api]"

# 组合安装
pip install -e ".[dev,azure,discord,pdf]"
```

#### 方式 3：使用安装脚本

```bash
# 从 PyPI 安装
./scripts/install.sh

# 从 GitHub main 分支安装
./scripts/install.sh --dev
```

#### 安装 WebUI 依赖

```bash
cd webui
bun install
# 或 npm install
```

### 5.3 配置文件初始化

#### 方式 1：交互式引导（推荐首次使用）

```bash
# 启动交互式引导问卷
hczkbot onboard --wizard

# 指定工作区
hczkbot onboard --wizard -w ~/.hczkbot/workspace

# 指定配置文件路径
hczkbot onboard --wizard -c ~/.hczkbot/config.yaml
```

引导会依次询问：模型选择、Provider API Key、工作区路径等，并自动生成配置文件。

#### 方式 2：手动创建配置文件

```bash
# 创建配置目录
mkdir -p ~/.hczkbot

# 复制示例配置
cp config.example.yaml ~/.hczkbot/config.yaml

# 编辑配置文件
vim ~/.hczkbot/config.yaml
```

#### 方式 3：使用默认配置

```bash
# 不带 --wizard，直接生成默认配置
hczkbot onboard
```

#### 设置环境变量

将 API Key 等敏感信息通过环境变量注入（避免硬编码到配置文件）：

```bash
# 添加到 ~/.zshrc 或 ~/.bashrc
export ANTHROPIC_API_KEY="sk-ant-xxx"
export OPENAI_API_KEY="sk-xxx"
export DEEPSEEK_API_KEY="sk-xxx"
export ZHIPU_API_KEY="xxx"
export MOONSHOT_API_KEY="sk-xxx"
export DASHSCOPE_API_KEY="sk-xxx"
export OPENROUTER_API_KEY="sk-or-xxx"
export VOLCENGINE_API_KEY="xxx"

# 可选：覆盖默认配置
export HCZKBOT_MAX_CONCURRENT_REQUESTS=3   # 全局并发请求数
export HCZKBOT_LLM_TIMEOUT_S=300           # LLM 调用超时（秒）
export HCZKBOT_STREAM_IDLE_TIMEOUT_S=90    # 流式空闲超时（秒）
```

```bash
source ~/.zshrc
```

### 5.4 启动开发环境

#### 启动网关（含 WebUI + Channels + Agent）

这是最常用的开发启动方式，会同时启动 WebUI 后端、所有启用的 channels、agent 循环：

```bash
# 默认端口 8765
hczkbot gateway

# 指定端口
hczkbot gateway -p 8765

# 指定工作区
hczkbot gateway -w ~/.hczkbot/workspace

# 指定配置文件
hczkbot gateway -c ~/.hczkbot/config.yaml

# 详细日志
hczkbot gateway -v
```

启动后访问 `http://127.0.0.1:8765` 即可使用 WebUI。

#### 启动 WebUI 前端开发服务器（热重载）

如果需要修改 WebUI 前端代码，启动 Vite dev server 获得热重载体验：

```bash
# 终端 1：启动 gateway（后端）
hczkbot gateway

# 终端 2：启动 WebUI dev server（前端，默认 5173 端口）
cd webui
bun run dev
# 或 npm run dev
```

Vite dev server 会自动代理 `/api`、`/webui`、`/auth` 到 gateway :8765。访问 `http://127.0.0.1:5173`。

如需指定后端地址：

```bash
HCZKBOT_API_URL=http://127.0.0.1:8765 bun run dev
```

#### 启动 OpenAI 兼容 API 服务器

```bash
# 默认 127.0.0.1:8900
hczkbot serve

# 指定 host 和 port
hczkbot serve -H 0.0.0.0 -p 8900

# 指定工作区
hczkbot serve -w /path/to/workspace
```

#### 直接与 agent 对话（CLI 模式）

```bash
# 交互式对话
hczkbot agent

# 单条消息
hczkbot agent -m "Hello!"

# 指定会话 ID
hczkbot agent -m "Hello!" -s cli:direct
```

#### 查看状态

```bash
hczkbot status
```

### 5.5 开发工作流

#### 推荐的开发目录结构

```
~/.hczkbot/                          # 配置与数据根目录
├── config.yaml                      # 主配置文件
├── workspace/                       # 默认工作区
│   ├── AGENTS.md                    # 工作区级 agent 指导
│   ├── SOUL.md                      # agent 人格
│   ├── USER.md                      # 用户画像
│   ├── HEARTBEAT.md                 # 周期性任务清单
│   ├── memory/
│   │   ├── MEMORY.md                # 长期记忆
│   │   └── history.jsonl            # 历史归档
│   ├── sessions/                    # 会话历史
│   ├── skills/                      # 自定义技能
│   ├── cron/                        # cron 任务
│   └── media/                       # 媒体文件
├── pairing.json                     # DM 配对审批
└── logs/                            # 日志
```

#### 前端开发工作流

```bash
cd webui

# 开发（热重载）
bun run dev

# 构建（输出到 ../hczkbot/web/dist，打包进 Python wheel）
bun run build

# 测试
bun run test

# 测试监听模式
bun run test:watch

# Lint
bun run lint
```

#### 后端开发工作流

```bash
# 运行单个测试
pytest tests/test_openai_api.py::test_function -v

# 运行某个模块的所有测试
pytest tests/agent/ -v

# 运行所有测试
pytest

# Lint
ruff check hczkbot/

# 自动修复
ruff check hczkbot/ --fix
```

### 5.6 测试与代码检查

#### Python 测试

```bash
# 运行所有测试
pytest

# 运行特定模块测试
pytest tests/agent/ -v
pytest tests/providers/ -v
pytest tests/channels/ -v
pytest tests/tools/ -v

# 运行单个测试函数
pytest tests/test_openai_api.py::test_function -v

# 带覆盖率
pytest --cov=hczkbot --cov-report=term-missing
```

#### Python Lint

```bash
# 检查
ruff check hczkbot/

# 自动修复
ruff check hczkbot/ --fix

# 格式化
ruff format hczkbot/
```

#### WebUI 测试

```bash
cd webui

# 运行所有测试
bun run test

# 监听模式
bun run test:watch

# Lint
bun run lint
```

### 5.7 Docker 部署

项目提供 `Dockerfile` 和 `docker-compose.yml` 用于容器化部署：

```bash
# 启动所有服务（gateway + api）
docker compose up -d

# 仅启动 gateway
docker compose up -d hczkbot-gateway

# 仅启动 api
docker compose up -d hczkbot-api

# 启动 CLI（交互式）
docker compose run --rm hczkbot-cli

# 查看日志
docker compose logs -f hczkbot-gateway
```

**端口映射：**
- `hczkbot-gateway`: 18790（WebUI）、8765（WebSocket）
- `hczkbot-api`: 127.0.0.1:8900（OpenAI 兼容 API）

**数据持久化：** 通过 volume `~/.hczkbot:/home/hczkbot/.hczkbot` 挂载配置和数据。

---

## 附录：常用命令速查

| 命令 | 说明 |
|------|------|
| `hczkbot onboard --wizard` | 交互式初始化配置 |
| `hczkbot gateway` | 启动网关（WebUI + Channels + Agent） |
| `hczkbot gateway -v` | 启动网关（详细日志） |
| `hczkbot serve` | 启动 OpenAI 兼容 API 服务器 |
| `hczkbot agent` | CLI 模式与 agent 对话 |
| `hczkbot agent -m "Hello!"` | 单条消息模式 |
| `hczkbot status` | 查看状态 |
| `cd webui && bun run dev` | 启动 WebUI 前端开发服务器 |
| `cd webui && bun run build` | 构建 WebUI |
| `cd webui && bun run test` | 运行 WebUI 测试 |
| `pytest tests/agent/ -v` | 运行 agent 模块测试 |
| `ruff check hczkbot/` | Python lint |
| `docker compose up -d` | Docker 部署 |

## 附录：关键环境变量

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API Key |
| `OPENAI_API_KEY` | — | OpenAI API Key |
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key |
| `ZHIPU_API_KEY` | — | 智谱 GLM API Key |
| `MOONSHOT_API_KEY` | — | Moonshot Kimi API Key |
| `DASHSCOPE_API_KEY` | — | 通义千问 API Key |
| `OPENROUTER_API_KEY` | — | OpenRouter API Key |
| `VOLCENGINE_API_KEY` | — | 火山引擎 API Key |
| `HCZKBOT_MAX_CONCURRENT_REQUESTS` | 3 | 全局并发请求数 |
| `HCZKBOT_LLM_TIMEOUT_S` | 300 | LLM 调用超时（秒） |
| `HCZKBOT_STREAM_IDLE_TIMEOUT_S` | 90 | 流式空闲超时（秒） |
| `HCZKBOT_API_URL` | `http://127.0.0.1:8765` | WebUI dev server 代理目标 |
