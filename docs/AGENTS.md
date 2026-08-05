本文件为在本仓库中工作的 AI 编码代理提供指导。

## 项目概述

biscuitbot 是一个轻量级、开源的 AI Agent 框架，使用 Python 编写，配备 React/TypeScript WebUI。围绕精简的 agent 循环构建：从聊天渠道接收消息 → 调用 LLM Provider → 执行工具 → 管理会话记忆。

- **PyPI 包名**：`biscuitbot`
- **版本**：0.2.7
- **CLI 语言**：全中文（命令帮助、运行时提示、交互引导）

## 开发命令

```bash
# Python：运行单个测试 / lint
pytest tests/test_openai_api.py::test_function -v
ruff check biscuitbot/

# WebUI：开发服务器（代理 API/WS 到网关 :8765）、构建、测试
# 构建输出到 ../biscuitbot/web/dist（打包进 Python wheel）
cd webui && bun run dev      # 或 BISCUITBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# 网关（启动后自动打开 WebUI 浏览器）
biscuitbot gateway

# 查看版本
biscuitbot -v
```

## 高层架构

### 核心数据流

消息通过异步 `MessageBus`（[biscuitbot/bus/queue.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/bus/queue.py)）流动，解耦聊天渠道与 agent 核心：

1. **Channels**（`biscuitbot/channels/`）从外部平台接收消息，发布 `InboundMessage` 事件到总线。
2. **`AgentLoop`**（[biscuitbot/agent/loop.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/loop.py)）消费入站消息，构建上下文，协调 turn。
3. **`AgentRunner`**（[biscuitbot/agent/runner.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/runner.py)）执行多轮 LLM 对话：发送消息到 provider、接收工具调用、执行工具、流式响应。
4. 响应以 `OutboundMessage` 事件发布回对应 channel。

### 关键子系统

- **Agent Loop**（`biscuitbot/agent/loop.py`、`runner.py`）：核心处理引擎。`AgentLoop` 管理 session key、hook 和上下文构建。`AgentRunner` 执行带工具调用的多轮 LLM 对话。
- **LLM Providers**（[biscuitbot/providers/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/)）：Provider 实现（Anthropic、OpenAI 兼容、DeepSeek、智谱、Kimi、通义、Ollama 等），基于统一基类 [base.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/base.py)。包含图像生成（`image_generation.py`）和语音转写（`transcription.py`）。`factory.py` 和 `registry.py` 负责实例化与模型发现。
- **Channels**（[biscuitbot/channels/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/)）：平台集成（Telegram、Discord、Slack、飞书、Matrix、WhatsApp、QQ、微信、企业微信、钉钉、Email、MoChat、MS Teams、WebSocket）。`manager.py` 发现并协调所有频道。通过 `pkgutil` 扫描 + entry-point 插件自动发现。
- **Tools**（[biscuitbot/agent/tools/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/tools/)）：暴露给 LLM 的 Agent 能力：文件系统（读/写/编辑/列表）、Shell 执行（含沙箱后端）、网页搜索/抓取、MCP 服务器、cron 定时任务、子代理派生、长任务/持续目标（`long_task.py`）、图像生成、自我描述。通过 `pkgutil` 扫描 + entry-point 插件自动发现。MCP 集成（`mcp.py`）将外部 MCP 服务器的工具/资源/提示词包装为原生工具，支持热重载、自动重连、超时触发重连。
- **Memory**（[biscuitbot/agent/memory.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py)）：会话历史持久化与 Dream 两阶段记忆整合。使用原子写入 + fsync 保证持久性。
- **Session 管理**（[biscuitbot/session/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/session/)）：每会话历史、上下文压缩、TTL 自动压缩（`manager.py`）、持续目标状态追踪（`goal_state.py`）。
- **Config**（[biscuitbot/config/schema.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/schema.py)、`loader.py`）：基于 Pydantic 的配置系统，从 `~/.biscuitbot/config.json` 加载。支持 camelCase 别名以兼容 JSON。
- **Bridge**（`bridge/`）：TypeScript 桥接服务（如 WhatsApp 桥接），通过 `pyproject.toml` 的 `force-include` 打包进 wheel。
- **WebUI**（`webui/`）：基于 Vite 的 React SPA，通过 WebSocket 多路复用协议与网关通信。开发服务器代理 `/api`、`/webui`、`/auth` 和 WebSocket 流量到网关。
- **API 服务器**（[biscuitbot/api/server.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/api/server.py)）：OpenAI 兼容 HTTP API（`/v1/chat/completions`、`/v1/models`）。
- **命令路由**（[biscuitbot/command/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/command/)）：斜杠命令路由与内置命令处理器。
- **心跳**（`biscuitbot/templates/HEARTBEAT.md`）：通过 `cron` 任务定期检查任务列表。
- **配对**（[biscuitbot/pairing/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/pairing/)）：DM 发送者审批存储，按 channel 维护持久化配对码。
- **Skills**（[biscuitbot/skills/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/skills/)）：内置技能定义（长目标、cron、GitHub、图像生成等），加载到 agent 上下文。
- **安全**（[biscuitbot/security/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/security/)）：PTH 文件防护等安全措施，在 CLI 入口激活。
- **版本检查与自更新**（[biscuitbot/webui/version_check.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/version_check.py)）：WebUI 设置页面支持检查 PyPI 新版本并一键自动更新。

### 入口点

- **CLI**：[biscuitbot/cli/commands.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/cli/commands.py)
- **Python SDK**：`biscuitbot/biscuitbot.py`

## 详细文档

| 文档 | 内容 |
|------|------|
| [PROJECT_GUIDE.md](PROJECT_GUIDE.md) | 项目概览、快速开始、命令参考 |
| [architecture.md](architecture.md) | AgentLoop 状态机、AgentRunner、上下文构建、MCP |
| [tools-system.md](tools-system.md) | 渐进式发现、冷门仓库、重复检测、夜间维护 |
| [memory-design.md](memory-design.md) | Dream 两阶段整合、Consolidator、AutoCompact |
| [providers-channels.md](providers-channels.md) | LLM Provider、频道集成 |
| [security-webui-config.md](security-webui-config.md) | SSRF、防护等级、配置系统、WebUI、自更新 |
| [pip-build-upload.md](pip-build-upload.md) | PyPI 打包与上传流程 |

## 贡献流程

遵循以下代码风格和测试规范。提交 PR 前确保 `ruff check` 和 `pytest` 通过。

## 代码风格

- Python 3.11+，全面使用 asyncio。
- 行宽：100。
- Lint：`ruff`，规则 E、F、I、N、W（E501 忽略）。
- pytest 使用 `asyncio_mode = "auto"`。
- CLI 命令和选项必须包含中文帮助文本。
- 所有 `except Exception` 块必须包含 `logger.exception()` 防止静默失败。

## 常见文件位置

- 配置 schema：[biscuitbot/config/schema.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/schema.py)
- Provider 基类 / 新 provider 模板：[biscuitbot/providers/base.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/base.py)
- Channel 基类 / 新 channel 模板：[biscuitbot/channels/base.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/base.py)
- 工具注册表：[biscuitbot/agent/tools/registry.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/tools/registry.py)
- WebUI 开发代理配置：`webui/vite.config.ts`
- 测试镜像 `biscuitbot/` 包结构。
