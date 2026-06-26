# 架构设计文档

本文档描述 hczkbot 项目的核心架构。所有引用的代码文件均可点击跳转。

## 1. 核心数据流

消息从外部渠道进入，经消息总线解耦后由 AgentLoop 调度，最终通过 AgentRunner 调用 LLM 并产出回复。

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ Channels │──▶│MessageBus│──▶│AgentLoop │──▶│AgentRunner│──▶│   LLM    │
│ (TG/Slack│   │ inbound  │   │ _dispatch│   │  _run_core│   │ Provider │
│  /CLI/…) │   │  queue   │   │ per-sess │   │  gov chain│   └────┬─────┘
└──────────┘   └──────────┘   └────┬─────┘   └─────┬────┘        │
     ▲              │               │               │             │
     │              │          ┌────▼─────┐   ┌─────▼────┐        │
     │              │          │ 8-state  │   │ tools /  │◀───────┘
     │              │          │  FSM     │   │ injections│
     │              │          └──────────┘   └──────────┘
     │              ▼
     │         ┌──────────┐
     └─────────│outbound  │  Channels consume_outbound → 用户
               │  queue   │
               └──────────┘
```

渠道层调用 `publish_inbound` 投递 [InboundMessage](file:///Volumes/data/hczkAgent/nanobot/hczkbot/bus/events.py)；[AgentLoop._run_main_loop](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/loop.py) 消费后为每个 session 创建 `_dispatch` 任务；最终回复通过 `publish_outbound` 写回 [MessageBus](file:///Volumes/data/hczkAgent/nanobot/hczkbot/bus/queue.py) 的 outbound 队列。

## 2. AgentLoop 状态机

[AgentLoop](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/loop.py) 把单个消息的处理切分为 8 个状态（`TurnState` 枚举）：

| 状态 | 职责 |
|------|------|
| `RESTORE` | 恢复 runtime checkpoint / pending user turn，提取文档与媒体 |
| `COMPACT` | `AutoCompact.prepare_session` 决定是否触发空闲压缩，产出 summary |
| `COMMAND` | 命令路由；命中快捷命令返回 `shortcut` 直达 `DONE`，否则 `dispatch` |
| `BUILD` | 拉取 history、构建 initial_messages、提前持久化用户消息 |
| `RUN` | 调用 `_run_agent_loop` → `AgentRunner.run` |
| `SAVE` | `_save_turn` 写入 session、enforce_file_cap、清理 checkpoint |
| `RESPOND` | 组装 `OutboundMessage`（含流式标记、延迟统计） |
| `DONE` | 终态 |

`_TRANSITIONS` 是事件驱动的转移表，handler 返回事件字符串，driver 查表得到下一状态；缺转移即抛 `RuntimeError`。`TurnContext` dataclass 在整个 turn 内承载 `msg`、`session`、`history`、`initial_messages`、`final_content`、`tools_used`、`trace` 等可变状态，避免跨 handler 传参。

**并发模型**：`_dispatch` 使用 `_session_locks: dict[str, asyncio.Lock]` 保证同一 session 串行；跨 session 并发由 `_concurrency_gate`（`Semaphore`，默认 3，由 `HCZKBOT_MAX_CONCURRENT_REQUESTS` 控制）限流。活跃 turn 期间，`_pending_queues[session_key]` 接收后续消息实现 mid-turn 注入，而非创建竞争任务。

## 3. AgentRunner 执行循环

[AgentRunner](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/runner.py) 是与产品层无关的工具循环引擎。

- `AgentRunSpec`：单次执行的配置（`initial_messages`、`tools`、`model`、`max_iterations`、各类 callback、`context_window_tokens`、`goal_active_predicate` 等）。
- `AgentRunResult`：执行结果（`final_content`、`messages`、`tools_used`、`usage`、`stop_reason`、`had_injections` 等）。

**上下文治理链**（`_run_core` 每次迭代调用，作用于 `messages_for_model`，不污染持久化 messages）：

1. `_drop_orphan_tool_results` — 丢弃没有对应 assistant `tool_calls` 的 tool 结果。
2. `_backfill_missing_tool_results` — 为孤立的 `tool_use` 注入合成错误结果（`[Tool result unavailable…]`）。
3. `_microcompact` — 超过 `_MICROCOMPACT_KEEP_RECENT`(10) 条的旧可压缩工具结果（`read_file`/`exec`/`grep`/`web_search` 等）替换为一行摘要。
4. `_apply_tool_result_budget` — 按 `max_tool_result_chars` 归一化/截断 tool 内容。
5. `_snip_history` — 估算 prompt tokens，从尾部保留消息直到预算耗尽，并修正到合法 user 起点。
6. 再次 `_drop_orphan` + `_backfill`（snip 可能制造新孤儿）。

**Mid-turn 注入**：`_try_drain_injections` 在「工具执行后」「最终回复后」「错误后」「max_iterations 后」四个检查点调用；单次最多 `_MAX_INJECTIONS_PER_TURN`(3) 条，单 turn 最多 `_MAX_INJECTION_CYCLES`(5) 轮。无注入但持续目标激活时注入 `goal_continue` 消息。

**Checkpoint**：`_emit_checkpoint` 在 `awaiting_tools` / `tools_completed` / `final_response` 阶段将 `assistant_message`、已完成与待执行的 tool_calls 写入 session metadata（`runtime_checkpoint`），`/stop` 或崩溃后由 `_restore_runtime_checkpoint` 物化为历史，避免丢失已积累的工具结果。

## 4. 上下文构建

[ContextBuilder.build_system_prompt](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/context.py) 按以下顺序拼接（`\n\n---\n\n` 分隔）：

1. **Identity** — `_get_identity`：workspace 路径、运行时、平台策略、channel、`guard_level`。
2. **Bootstrap files** — `_load_bootstrap_files`：`AGENTS.md`、`SOUL.md`、`USER.md`。
3. **Tool contract** — `agent/tool_contract.md` 模板。
4. **Tool index** — Tools & Skills Index（由 `AgentLoop._build_tool_index` 传入）。
5. **Memory** — `memory.get_memory_context()`（与模板内容相同时跳过）。
6. **Active skills** — `always_skills` 的完整内容。
7. **Skills summary** — 非 always 技能的摘要（`agent/skills_section.md`）。
8. **Recent history** — `read_recent_history_for_prompt`，截断到 `_MAX_HISTORY_TOKENS`(8000)，超出部分压缩为 Backlog。
9. **Session summary** — AutoCompact 产出的 Archived Context Summary（如有）。

`build_messages` 把 system prompt、history、当前用户消息（合并 Runtime Context 块以避免连续同角色消息）组装成最终 messages 列表。

## 5. 会话管理

[Session](file:///Volumes/data/hczkAgent/nanobot/hczkbot/session/manager.py) dataclass：`key`、`messages`、`created_at`/`updated_at`、`metadata`、`last_consolidated`（已归档到文件前缀数）。`get_history` 切片未归档消息，按 token 预算从尾部保留，并对齐到合法 user 起点；`retain_recent_legal_suffix` 与 `enforce_file_cap`（默认 2000 条）在超限时归档旧前缀。

**SessionManager**：JSONL 持久化于 `workspace/sessions/<safe_key>.jsonl`，首行为 metadata 记录，后续每行一条消息。`save` 采用 tmp 文件 + `os.replace` 原子写，`fsync=True` 时额外刷盘目录（用于优雅关闭 `flush_all`）。`_load` 支持从 legacy 路径迁移、`_repair` 容忍坏行；`fork_session_before_user_index` 支持 WebUI 分叉。

**AutoCompact**：`check_expired` 在主循环空闲分支按 `session_ttl_minutes` 触发后台压缩；`prepare_session` 返回 `(session, pending_summary)`，summary 注入 system prompt 第 9 层。

**持续目标状态**（[goal_state](file:///Volumes/data/hczkAgent/nanobot/hczkbot/session/goal_state.py)）：`metadata[GOAL_STATE_KEY]` 存储 `long_task` 目标。`goal_state_runtime_lines` 把目标文本追加进 Runtime Context；`sustained_goal_active` 为真时 `runner_wall_llm_timeout_s` 返回 `0.0`，关闭 LLM 墙钟超时以允许长任务运行（仍受 stream idle 超时约束）。

## 6. 消息总线

[MessageBus](file:///Volumes/data/hczkAgent/nanobot/hczkbot/bus/queue.py) 持有 `inbound` / `outbound` 两个 `asyncio.Queue`，提供 `publish_inbound`/`consume_inbound` 与 `publish_outbound`/`consume_outbound`，完全解耦渠道层与 agent 核心。

[InboundMessage](file:///Volumes/data/hczkAgent/nanobot/hczkbot/bus/events.py) / `OutboundMessage` 为 dataclass：前者含 `channel`、`sender_id`、`chat_id`、`content`、`media`、`metadata`、`session_key_override`（`session_key` 属性默认 `channel:chat_id`）；后者含 `channel`、`chat_id`、`content`、`reply_to`、`media`、`metadata`（可携带 `_agent_ui`、流式标记、延迟统计等）、`buttons`。

`RuntimeEventBus` 提供订阅/发布 `RuntimeEvent`；`RuntimeEventPublisher` 封装 turn 生命周期事件（`session_turn_started`、`run_status_changed`、`turn_completed`、`record_turn_runtime`、`record_turn_latency`、`runtime_model_changed`），供 WebUI 等客户端实时展示运行状态。

## 7. 子代理管理

[SubagentManager](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/subagent.py) 采用 fire-and-forget 模型：`spawn` 通过 `asyncio.create_task` 启动后台任务并立即返回 `task_id`，不阻塞当前 turn。`_running_tasks`、`_task_statuses`、`_session_tasks` 三张表分别跟踪任务、状态、session 归属。

并发由 `max_concurrent_subagents`（来自 `AgentDefaults`）限制；`cancel_by_session` 用于 `/stop` 取消该 session 所有子代理；`get_running_count_by_session` 被 `_drain_pending` 用来在没有新注入但子代理仍跑时阻塞等待结果，保证子代理完成按序注入而非另起 turn。子代理使用独立 `ToolRegistry`（`scope="subagent"`）与 `FileStates`，完成后通过 system channel 把结果回投给 `MessageBus`。

## 8. MCP 服务器集成

[mcp.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/mcp.py) 支持三种传输模式：`stdio`（`stdio_client`，启动子进程）、`sse`（`sse_client`）、`streamable_http`（`streamable_http_client`）。连接由 `connect_mcp_servers` 经 `AsyncExitStack` 管理，工具按 `mcp_<server>_` 前缀注册到 `ToolRegistry`。

**跨 task cancel scope 问题**：anyio cancel scope（由 `stdio_client` 进入）是 task-local 的，必须在进入它的同一 task 内退出。`_mcp_owner_task` 记录调用 `_connect_mcp` 的 task（即 `run()`）；`_dispatch` 子任务无法直接关闭旧栈。

**延迟关闭**：`_close_server` 检测到当前 task ≠ owner 时，把旧栈推入 `_mcp_deferred_stacks`；owner task 在主循环空闲分支通过 `_close_deferred_mcp_stacks` 安全关闭。

**延迟重连**：子任务检测到 stale session 后，把 `(server, tool, stale_tool, future)` 推入 `_mcp_reconnect_requests` 并 await future；owner task 通过 `_process_mcp_reconnects` 执行 `_refresh_terminated_server`，确保新栈的 cancel scope 在 owner task 内进入，与后续关闭对齐。`close_mcp` 在关闭前取消所有 pending future 防止子任务挂起。

## 9. Cron 定时任务

[CronService](file:///Volumes/data/hczkAgent/nanobot/hczkbot/cron/service.py) 采用异步定时器调度：`_arm_timer` 用 `asyncio.sleep` 安排下一次 tick，`_on_timer` 取出到期 job 依次执行后 `_save_store` 并重新 arm。`max_sleep_ms`(默认 5 分钟) 为最长睡眠上限。

调度类型（`CronSchedule.kind`）：`at`（一次性绝对时间）、`every`（固定间隔）、`cron`（croniter 表达式 + 可选 `tz`）。`_compute_next_run` 计算下次运行时间。

**持久化**：`jobs.json` 原子写（tmp + `os.replace` + `fsync` 目录）；`action.jsonl` + `FileLock` 用于跨实例变更合并（`_merge_action`）。`_load_store` 每次重载并合并 action；解析失败时保留 `.corrupt-<ts>` 备份，优先沿用内存快照而非清空。

**任务注册**：`register_system_job` 注册内部系统任务（`kind="system_event"`，重启幂等，受保护不可删除/修改）；`add_job` 创建用户 `agent_turn` 任务，必须通过 `_enforce_agent_binding` 绑定到具体 session（`session_key`/`origin_channel`/`origin_chat_id`），否则禁用。`CronTurnCoordinator` 与 `_cron_turns.defer_if_active` 让 cron turn 在 session 活跃时排队而非抢占。
