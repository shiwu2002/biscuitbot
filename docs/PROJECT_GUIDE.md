# hczkbot 项目文档

> **hczkbot** 是一个轻量级、开源的 AI Agent 框架，使用 Python + React/TypeScript 构建。围绕精简的 agent 循环：从聊天渠道接收消息 → 调用 LLM Provider → 执行工具 → 管理会话记忆。

| 属性 | 值 |
|------|---|
| **版本** | 0.2.1 |
| **语言** | Python 3.11+ / TypeScript |
| **许可证** | MIT |
| **构建** | hatchling (Python) / Vite + bun (WebUI) |
| **代码风格** | ruff (E, F, I, N, W，E501 忽略，行宽 100) |
| **测试** | pytest (asyncio_mode=auto) / vitest |

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [架构设计](architecture.md) | AgentLoop 状态机、AgentRunner 执行循环、上下文构建、会话管理、消息总线、MCP 集成、Cron 定时 |
| [工具系统](tools-system.md) | 渐进式发现、冷门仓库、重复检测、夜间维护、工具清单、配置项 |
| [记忆系统](memory-design.md) | Dream 两阶段整合、Consolidator、AutoCompact、GitStore 版本控制 |
| [Provider 与 Channel](providers-channels.md) | LLM Provider 抽象层、故障转移、聊天平台集成、自动发现 |
| [安全 / WebUI / 配置](security-webui-config.md) | SSRF 防护、防护等级、配对审批、配置系统、WebUI 前后端 |

---

## 核心数据流

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

1. **Channels** 接收外部消息，发布 `InboundMessage` 到 `MessageBus`
2. **AgentLoop** 消费消息，驱动 8 状态状态机（`RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE`）
3. **AgentRunner** 执行多轮 LLM 对话：上下文治理 → 请求模型 → 执行工具 → 流式响应
4. 响应以 `OutboundMessage` 回传到对应 channel

详见 [架构设计文档](architecture.md)。

---

## 核心特性

- **渐进式工具发现** — INDEX.md 目录索引驱动，模型按需 `discover_tools` 加载 schema，冷门工具自动转入冷门仓库
- **多 Provider 支持** — Anthropic、OpenAI、DeepSeek、智谱、Kimi、通义、Ollama 等
- **多渠道接入** — Telegram、Discord、Slack、飞书、钉钉、QQ、微信、企业微信、Matrix、Email、WebSocket 等
- **Dream 两阶段记忆** — 会话级归档 + 周期性长期记忆更新，MECE 分类，Git 版本控制
- **WebUI** — React + Vite 富客户端，流式输出、活动追踪、技能管理、高级设置
- **OpenAI 兼容 API** — `/v1/chat/completions`、`/v1/models`
- **夜间维护** — 每天 23:00 自动执行文档一致性检查、重复检测、冷门轮转

---

## 快速开始

### 环境准备

```bash
# Python 3.11+
python3 --version

# 推荐使用 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Node.js 18+ 和 bun（WebUI 开发）
curl -fsSL https://bun.sh/install | bash
```

### 安装

```bash
cd /Volumes/data/hczkAgent/nanobot

# 开发模式安装
uv pip install -e ".[dev]"

# WebUI 依赖
cd webui && bun install
```

### 配置初始化

```bash
# 交互式引导（推荐首次使用）
hczkbot onboard --wizard

# 或手动创建
mkdir -p ~/.hczkbot
cp config.example.yaml ~/.hczkbot/config.yaml
```

设置环境变量：
```bash
export ANTHROPIC_API_KEY="sk-ant-xxx"
export OPENAI_API_KEY="sk-xxx"
# ... 其他 provider key
```

### 启动

```bash
# 启动网关（含 WebUI + Channels + Agent）
hczkbot gateway

# WebUI 前端热重载（另一个终端）
cd webui && bun run dev

# OpenAI 兼容 API 服务器
hczkbot serve

# CLI 直接对话
hczkbot agent
```

访问 `http://127.0.0.1:8765` 使用 WebUI。

---

## 开发命令

```bash
# Python 测试
pytest tests/agent/ -v              # 运行模块测试
pytest tests/agent/tools/ -v        # 工具系统测试
pytest --cov=hczkbot                # 带覆盖率

# Python lint
ruff check hczkbot/                 # 检查
ruff check hczkbot/ --fix           # 自动修复

# WebUI
cd webui && bun run dev             # 开发服务器
cd webui && bun run build           # 构建
cd webui && bun run test            # 测试
cd webui && bun run lint            # Lint
```

---

## 常用命令速查

| 命令 | 说明 |
|------|------|
| `hczkbot onboard --wizard` | 交互式初始化配置 |
| `hczkbot gateway` | 启动网关（WebUI + Channels + Agent） |
| `hczkbot gateway -v` | 启动网关（详细日志） |
| `hczkbot serve` | 启动 OpenAI 兼容 API 服务器 |
| `hczkbot agent` | CLI 模式与 agent 对话 |
| `hczkbot status` | 查看状态 |
| `cd webui && bun run dev` | 启动 WebUI 前端开发服务器 |

## 关键环境变量

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API Key |
| `OPENAI_API_KEY` | — | OpenAI API Key |
| `HCZKBOT_MAX_CONCURRENT_REQUESTS` | 3 | 全局并发请求数 |
| `HCZKBOT_LLM_TIMEOUT_S` | 300 | LLM 调用超时（秒） |
| `HCZKBOT_STREAM_IDLE_TIMEOUT_S` | 90 | 流式空闲超时（秒） |

---

## 项目结构概览

```
hczkbot/
├── agent/              # 智能体核心引擎
│   ├── loop.py         # AgentLoop 状态机
│   ├── runner.py       # AgentRunner LLM 调用循环
│   ├── context.py      # 上下文构建
│   ├── memory.py       # 记忆系统与 Dream 整合
│   ├── tools/          # 工具系统（渐进式发现 + 冷门仓库）
│   └── ...
├── providers/          # LLM Provider 抽象层
├── channels/           # 聊天平台集成
├── bus/                # 异步消息总线
├── session/            # 会话管理
├── config/             # Pydantic 配置系统
├── security/           # 安全机制（SSRF + 边界 + 防护等级）
├── webui/              # WebUI 后端服务
├── cli/                # 命令行界面
├── cron/               # 定时任务
├── command/            # 斜杠命令路由
├── skills/             # 内置技能
├── templates/          # 模板文件
└── utils/              # 工具模块
webui/                  # React 前端 SPA
bridge/                 # TypeScript 桥接服务
tests/                  # 测试（镜像 hczkbot/ 结构）
```
