# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

biscuitbot 是一个轻量级、开源的 AI Agent 框架（Python 3.11+ / asyncio）+ React/TypeScript WebUI，另有 Tauri 2 桌面壳。核心循环：**聊天渠道收消息 → 调用 LLM Provider → 执行工具 → 管理会话记忆**。

CLI、命令帮助、运行时提示、交互引导全部为中文。

## 常用命令

依赖管理用 `uv`（Python）+ `bun`（WebUI）。

```bash
# 安装依赖（首次）
uv pip install -e ".[dev]"
cd webui && bun install

# 运行
uv run biscuitbot gateway        # 主网关（channels + WebUI + agent，自动打开浏览器）
uv run biscuitbot agent          # CLI 对话
uv run biscuitbot serve          # OpenAI 兼容 API 服务器
uv run biscuitbot onboard --wizard  # 交互式配置初始化

# Python 测试
uv run pytest tests/             # 全部
uv run pytest tests/agent/tools/test_xxx.py::test_func -v   # 单个测试

# Python lint / 类型检查
uv run ruff check biscuitbot     # 只跑 check（见下方 ruff 陷阱）
uv run pyright biscuitbot

# WebUI（构建输出到 biscuitbot/web/dist，会打包进 wheel）
cd webui && bun run dev          # 开发服务器（代理 API/WS 到网关 :8765）
cd webui && bun run build        # tsc + vite build
cd webui && bun run test         # vitest
cd webui && bun run lint         # eslint --max-warnings 0
```

CLI 子命令：`onboard`、`serve`、`gateway`、`desktop`、`sidecar`、`agent`、`status`，以及 Typer 子应用 `channels`（`status`/`login`）、`plugins`（`list`）、`talent-market`。

## 核心架构

**数据流**：`Channels`（`biscuitbot/channels/`）从外部平台收消息 → 发布 `InboundMessage` 到异步 `MessageBus`（`biscuitbot/bus/queue.py`）→ `AgentLoop` 消费并驱动 8 状态状态机（`RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE`，`biscuitbot/agent/loop.py`）→ `AgentRunner` 执行多轮 LLM + 工具调用（`biscuitbot/agent/runner.py`）→ 以 `OutboundMessage` 回传 channel。详见 `docs/architecture.md`。

**关键子系统**：

- **AgentLoop / AgentRunner**（`agent/loop.py`、`runner.py`）：核心路径，改动要最小化。`AgentRunner` 每次迭代跑一条上下文治理链（孤儿 tool 结果清理 → microcompact → tool 结果预算 → 历史截断），支持 mid-turn 注入与 runtime checkpoint。
- **LLM Providers**（`providers/`）：统一基类 `base.py`；`factory.py`/`registry.py` 负责实例化与自动发现。
- **Channels**（`channels/`）：Telegram、Discord、Slack、飞书、钉钉、QQ、微信、企业微信、Email、WebSocket 等；`manager.py` 发现并协调。每个 channel 文件应自包含、可独立阅读（不抽共享基类）。
- **Tools**（`agent/tools/`）：`registry.py` 为工具注册表；文件系统、Shell（含沙箱）、网页搜索、MCP、cron、子代理、数字员工（`employee.py`/`employee_discover.py`）等。渐进式发现 + 冷门仓库（`cold_storage.py`）。
- **Memory / Session**（`agent/memory.py`、`session/`）：JSONL 持久化，原子写（tmp + fsync + rename）。Dream 两阶段记忆整合 + AutoCompact。
- **Config**（`config/schema.py`、`loader.py`）：Pydantic 模型，从 `~/.biscuitbot/config.json` 加载（注意是 JSON，非 yaml）。
- **Security**（`security/`）：SSRF 防护（`network.py`）、workspace 边界、guard_level 策略、PTH 防护。
- **WebUI 后端**（`biscuitbot/webui/`）：WebSocket 多路复用协议 + HTTP API，覆盖资产、技能/能力、会话分叉、媒体、知识库、人才市场等。
- **Capability 模型**（`capabilities/registry.py`）：把「技能 / CLI 应用 / MCP 预设」读时聚合为统一能力列表，按 `runtime`（`prompt`/`process`/`mcp`）区分执行方式。这是近期「能力中心」重构的产物。

## 桌面打包（Tauri 2 + PyInstaller sidecar）

桌面应用 = Tauri 2 壳（`src-tauri/`，系统 WebView 渲染 WebUI）+ PyInstaller onedir 打包的无头 Python gateway（sidecar 子进程）。构建脚本 `scripts/build-desktop.sh`（macOS/Linux）、`scripts/build-desktop.ps1`（Windows），sidecar 打包（排除 telegram/slack/lark 等 channel 依赖）内联在脚本中。`biscuitbot/desktop/` 含 pywebview 桌面入口（`run_desktop`）与 sidecar 看门狗（靠 `BISCUITBOT_PARENT_PID` 探测壳存活，每 5s `os.kill(pid,0)`）。

改完前端需重跑 `scripts/build-desktop.sh`（dist 嵌在 PyInstaller bundle 里）。

## 关键约束与陷阱（必读）

- **不要运行 `ruff format`**：会破坏 git blame 历史，只用 `ruff check`（见 `.agent/gotchas.md`）。
- **Config `${VAR}` 引用**：`config/loader.py` 在加载时解析 `config.json` 里的 `${VAR}`，环境变量缺失会抛 `ValueError` 并回退默认配置。
- **MCP cancel scope 是 task-local 的**：永远不要在当前 task 之外 `stack.aclose()` 或调用 `connect_mcp_servers`（会在别的 task 进入 anyio cancel scope → `RuntimeError` + 泄漏）。tracked as `AgentLoop._mcp_owner_task`；跨 task 时用 deferred close（`_mcp_deferred_stacks`）/ deferred reconnect（`_mcp_reconnect_requests`）。详见 `docs/architecture.md` §8 与 `.agent/gotchas.md`。
- **site-packages 遮蔽**：`.venv` 里装的 biscuitbot 可能是普通（非 editable）副本，console script 会走 site-packages 旧代码 → 新路由 404。跑 `biscuitbot gateway` 要加 `PYTHONPATH=<repo>`，或用 `pip install -e . --no-build-isolation`。
- **React hooks 顺序**：`webui/src` 中 hooks 必须全部放在条件 return 之前，否则触发 React #310「Rendered more hooks」白屏（历史根因在 `ChatList.tsx`）。
- **Workspace 边界 / SSRF**：任何新路径处理须过 `_resolve_path`（`agent/tools/filesystem.py`）；工具内禁止直接 `httpx.get`/`requests.get`，须过 `validate_url_target`（`security/network.py`）。这些结构性访问控制**不**受 `guard_level` 门控。

## 设计原则（`.agent/design.md`）

核心保持精简，能力加到边缘（channels/tools/skills/MCP）；少结构、多智能；重复优于过早抽象（channel/provider 各自自包含）；最小改动解决真实问题；配置须在 `config/schema.py` 显式声明。

## 文档索引

- `docs/AGENTS.md` — 面向 AI 代理的完整指导（注意其中文件路径与版本号可能过时，以本仓库实际为准）
- `docs/architecture.md` — AgentLoop 状态机、AgentRunner、上下文构建、MCP、Cron
- `docs/tools-system.md` — 渐进式发现、冷门仓库、重复检测
- `docs/memory-design.md` — Dream 两阶段整合、Consolidator、AutoCompact
- `docs/providers-channels.md` — LLM Provider、渠道集成
- `docs/security-webui-config.md` — SSRF、防护等级、配置系统、WebUI、自更新
- `docs/pip-build-upload.md` — PyPI 打包与上传
- `.agent/gotchas.md` / `.agent/design.md` / `.agent/security.md` — 陷阱、设计约束、安全边界（务必遵循）
