# biscuitbot 记忆系统设计文档

## 1. 概述

biscuitbot 的记忆系统是一个**两层 + 两阶段**的混合记忆架构，旨在让 Agent 跨会话保留长期信息，同时控制上下文窗口的 token 成本。

核心设计目标：

- **持久化**：跨会话、跨重启保留重要信息
- **分层遗忘**：短期会话历史可压缩，长期事实可沉淀
- **MECE 分类**：不同类型的信息路由到不同文件，避免重复
- **可追溯**：所有长期记忆变更通过 Git 版本控制
- **可回滚**：支持恢复到任意 Dream 快照

核心代码位置：[biscuitbot/agent/memory.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py)

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        Agent Loop (turn)                         │
│                                                                  │
│  InboundMessage ──► ContextBuilder ──► LLM ──► Tool Calls        │
│                          │                                       │
│                          │                                       │
│          ┌───────────────┴───────────────┐                       │
│          ▼                               ▼                       │
│   ┌─────────────┐                ┌──────────────┐                │
│   │ MemoryStore │◄──────────────►│ Consolidator │                │
│   │ (文件 I/O)  │                │ (token 整合) │                │
│   └─────┬───────┘                └──────┬───────┘                │
│         │                               │                        │
│         │  ┌────────────────────────────┘                        │
│         ▼  ▼                                                       │
│   ┌────────────────────────────────────────────────────┐         │
│   │              AutoCompact (空闲压缩)                │         │
│   └────────────────────────────────────────────────────┘         │
│                                                                  │
│   后台定时任务                                                    │
│   ┌────────────────────────────────────────────────────┐         │
│   │              Dream (两阶段记忆整合)                │         │
│   │  Phase 1: 采集 (append_history)                    │         │
│   │  Phase 2: 整合 (LLM 更新 SOUL/USER/MEMORY/SKILL)   │         │
│   └────────────────────────────────────────────────────┘         │
│                                                                  │
│   持久化与版本控制                                                │
│   ┌────────────────────────────────────────────────────┐         │
│   │              GitStore (dulwich Git)                │         │
│   └────────────────────────────────────────────────────┘         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. 记忆文件分层

记忆系统管理四类文件，由 [MemoryStore](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L40) 统一管理，遵循 **MECE（相互独立、完全穷尽）** 原则：

| 文件 | 路径 | 用途 | 管理者 |
|------|------|------|--------|
| `SOUL.md` | `workspace/SOUL.md` | Agent 人格、行为规则、工具使用策略 | Dream |
| `USER.md` | `workspace/USER.md` | 用户画像：身份、偏好、习惯、沟通风格 | Dream |
| `MEMORY.md` | `workspace/memory/MEMORY.md` | 长期事实：项目上下文、架构决策、基础设施 | Dream |
| `SKILL.md` | `workspace/skills/<name>/SKILL.md` | 可复用工作流模板（命令、步骤、示例） | Dream |
| `history.jsonl` | `workspace/memory/history.jsonl` | 追加式历史日志（带 cursor 追踪） | Consolidator / AutoCompact |
| `.cursor` | `workspace/memory/.cursor` | history.jsonl 的写入游标 | MemoryStore |
| `.dream_cursor` | `workspace/memory/.dream_cursor` | Dream 已处理到的历史游标 | MemoryStore |

### 3.1 文件路由规则

Dream 模板（[templates/agent/dream.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/agent/dream.md)）定义了严格的路由规则：

- **USER.md**：个人属性（身份、偏好、习惯、沟通风格）— 不含技术配置
- **SOUL.md**：Agent 行为规则、护栏、交互模式、工具策略 — 不含用户事实
- **MEMORY.md**：项目上下文（目标、架构、决策、基础设施）— 不含操作细节
- **SKILL.md**：可复用工作流模板（具体命令、API、步骤）

跨边界规则：USER.md 不放技术配置，SOUL.md 不放用户事实，MEMORY.md 不放操作细节。若一个事实适合多个文件，保留最具体的副本，其余删除。

### 3.2 保护机制

- 用户和 Agent **不应直接编辑** `SOUL.md`、`USER.md`、`MEMORY.md`，由 Dream 自动管理
- `history.jsonl` 和 `.dream_cursor` 受 Shell 工具保护，禁止直接写入（[tools/shell.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/tools/shell.py#L189)）
- 模板内容检测：[ContextBuilder._is_template_content](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/context.py#L179) 会跳过未自定义的模板文件，避免注入占位符内容

---

## 4. MemoryStore：纯文件 I/O 层

[MemoryStore](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L40) 是记忆系统的底层存储抽象，只负责文件读写，不涉及 LLM 调用。

### 4.1 核心职责

```python
class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""
```

- 读写 `MEMORY.md` / `SOUL.md` / `USER.md`
- 追加式写入 `history.jsonl`（带原子 cursor 分配）
- 维护 `.cursor`（写入进度）和 `.dream_cursor`（Dream 处理进度）
- 从遗留 `HISTORY.md` 迁移到 `history.jsonl`
- 构建 Dream 提示词和受限工具集
- 通过 GitStore 进行版本控制

### 4.2 history.jsonl 格式

每行一个 JSON 对象：

```json
{"cursor": 1, "timestamp": "2026-06-24 10:00", "content": "用户询问了部署流程", "session_key": "telegram:12345"}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `cursor` | int | 自增游标（原子分配，用于去重和进度追踪） |
| `timestamp` | str | `%Y-%m-%d %H:%M` 格式 |
| `content` | str | 摘要内容（经 `strip_think` 清理） |
| `session_key` | str (可选) | 来源会话，用于会话隔离过滤 |

### 4.3 原子写入与并发安全

- **cursor 分配 + 追加**：使用 `threading.Lock` 串行化（[memory.py:275](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L275)），防止并发写入产生重复 cursor
- **compact_history 加锁**：`compact_history` 在 `_append_lock` 内执行读-改-写（[memory.py:387](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L387)），防止并发 `append_history` 的条目被原子重写覆盖丢失
- **整文件重写**：`_write_entries` 使用 temp 文件 + `os.replace` + `fsync` + 目录 `fsync` 保证原子性和持久性（[memory.py:483](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L483)）
- **游标文件原子写入**：`.cursor` 和 `.dream_cursor` 均使用 `atomic_write_text`（temp + rename + fsync）写入（[helpers.py:355](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/utils/helpers.py#L355)），崩溃时不会产生部分写入
- **history.jsonl 追加 fsync**：`append_history` 在追加后 `f.flush()` + `os.fsync()`，保证数据落盘
- **Windows 兼容**：目录 fsync 在 Windows 上跳过（NTFS 同步元数据）

### 4.4 内容清理

`append_history` 在持久化前会经过 `strip_think` 清理：
- 移除 `<think>...</think>` 推理块
- 移除未闭合的 `<think` 前缀
- 移除 `<channel|>` 等模板标记泄漏

若清理后内容为空但原始内容非空，仍持久化空字符串，避免泄漏内容通过回放污染上下文。

### 4.5 容量限制

| 常量 | 值 | 说明 |
|------|----|------|
| `_DEFAULT_MAX_HISTORY` | 1000 | history.jsonl 最大条目数 |
| `_HISTORY_ENTRY_HARD_CAP` | 64,000 | 单条目硬上限（兜底） |
| `_RAW_ARCHIVE_MAX_CHARS` | 16,000 | LLM 失败时的原始转储上限 |
| `_ARCHIVE_SUMMARY_MAX_CHARS` | 8,000 | LLM 摘要上限 |

`compact_history()` 在条目数超限时丢弃最旧的条目，**但不会静默删除**——被驱逐的条目归档到 `history-archive-YYYY-MM.jsonl`（[memory.py:406](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L406)），保留审计可追溯性。

### 4.6 `_read_last_entry` 健壮读取

`_read_last_entry`（[memory.py:437](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L437)）用于高效读取 history.jsonl 的最后一条记录（避免全文件扫描来分配下一个 cursor）：

- **循环倍增读取**：从 8KB 开始，若最后一行 JSON 解析失败（截断），倍增读取窗口（16KB→32KB→...）直到成功或读完全文件。单条记录可达 24KB+（8000 字符摘要 × 3 字节 UTF-8），固定 4KB 窗口会截断。
- **UnicodeDecodeError 处理**：当读取窗口恰好切断多字节 UTF-8 字符（CJK 文本常见），`decode("utf-8")` 抛出异常时也倍增窗口重试，而非直接返回 None。这对中文场景至关重要。
- **回退策略**：所有方法失败时返回 None，`_next_cursor` 回退到全文件扫描取 `max(cursor)+1`。

### 4.7 遗留迁移

`_maybe_migrate_legacy_history()` 一次性将旧版 `HISTORY.md`（Markdown 格式）迁移到 `history.jsonl`：
- 解析时间戳前缀 `[YYYY-MM-DD HH:MM]`
- 处理 `[RAW]` 原始消息块
- 迁移后备份为 `HISTORY.md.bak`
- 默认将 dream_cursor 设为最后一条，避免首次启动回放全部历史

---

## 5. Consolidator：Token 预算触发的会话内整合

[Consolidator](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L623) 负责在会话进行中，当 prompt token 接近上下文窗口时，将旧消息摘要归档到 `history.jsonl`。

### 5.1 Token 预算模型

```python
_input_token_budget = context_window_tokens - max_completion_tokens - _SAFETY_BUFFER
# _SAFETY_BUFFER = 1024 (tokenizer 估算漂移余量)
```

- **触发阈值**：估算 prompt token > `budget`
- **目标阈值**：压缩到 `budget * consolidation_ratio`（默认 0.5，即 50%）
- **估算方式**：`estimate_session_prompt_tokens` 构建完整 probe 消息链，调用 `estimate_prompt_tokens_chain`

### 5.2 整合流程

`maybe_consolidate_by_tokens` 的核心循环（[memory.py:862](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L862)）：

1. **获取会话锁**：`get_lock(session_key)` 使用 `WeakValueDictionary` 维护每会话 `asyncio.Lock`
2. **刷新会话引用**：AutoCompact 可能已替换会话对象
3. **回放窗口溢出归档**：`_consolidate_replay_overflow` 归档超出 `replay_max_messages` 的旧消息
4. **估算当前 prompt token**
5. **多轮归档**（最多 `_MAX_CONSOLIDATION_ROUNDS = 5` 轮）：
   - `pick_consolidation_boundary` 在 user-turn 边界选取安全的归档点
   - `archive` 调用 LLM 摘要该 chunk
   - 推进 `session.last_consolidated` 游标
   - 重新估算 token，直到 <= target 或无安全边界
6. **持久化最后摘要**：写入 `session.metadata["_last_summary"]`，供下次 turn 注入

### 5.3 边界选取策略

`pick_consolidation_boundary` 在 user-turn 边界选取归档点：
- 只在 `role == "user"` 的消息前切分（保留完整的 user-assistant 对）
- 累计被移除消息的 token 数，直到达到 `tokens_to_remove`
- 避免在工具调用中间切分，保证消息链合法性

### 5.4 LLM 摘要归档

`archive` 方法（[memory.py:855](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L855)）：

1. 格式化消息为 `[timestamp] ROLE [tools: ...]: content` 形式
2. 截断到 token 预算内
3. 调用 LLM，使用 [consolidator_archive.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/agent/consolidator_archive.md) 模板，注入 `session_key` 上下文（多会话场景下帮助 LLM 区分不同渠道/聊天的事实）
4. LLM 输出带属性标签的原子事实列表
5. 追加到 `history.jsonl`
6. **失败兜底**：LLM 调用失败时 `raw_archive` 原始转储（最多 16,000 字符）

### 5.5 历史属性标签

Consolidator 摘要输出使用 **SNIP 标准**（Signal / Novel / Important / Persistent）标注：

| 标签 | 含义 | Dream 处理 |
|------|------|-----------|
| `[permanent]` | 核心偏好、个人特质、习惯 — 永不过期 | 永久保留 |
| `[durable]` | 技术发现、项目知识、配置 — 有效数月 | 更新到位 |
| `[ephemeral]` | 活跃任务状态、临时决策 — 数周内变化 | 过期移除 |
| `[correction]` | 对先前记忆的纠正 | 原地替换 |
| `[skip]` | 不满足 SNIP，对话填充或可从仓库推导 | 不写入长期记忆 |

Dream 在第二阶段会读取这些标签作为路由和保留提示，并从最终保存的内容中剥离这些标签。

---

## 6. Dream：两阶段长期记忆整合

Dream 是 biscuitbot 记忆系统的核心创新，采用**两阶段**设计将短期会话历史沉淀为长期结构化记忆。

### 6.1 两阶段流程

```
阶段一：采集（实时）                    阶段二：整合（定时/手动）
┌─────────────────────┐               ┌─────────────────────────┐
│  会话进行中          │               │  Dream 定时任务（每 2h）│
│  Consolidator 归档   │               │  或 /dream 手动触发     │
│  → append_history    │ ─────────────►│                         │
│  (原子 cursor 分配)  │  history.jsonl│  build_dream_prompt     │
│                     │               │  读取 .dream_cursor 之后│
└─────────────────────┘               │  的未处理历史           │
                                      │                         │
                                      │  LLM + 受限工具集       │
                                      │  更新 SOUL/USER/MEMORY/ │
                                      │  SKILL                  │
                                      │                         │
                                      │  set_last_dream_cursor  │
                                      │  Git auto-commit        │
                                      └─────────────────────────┘
```

### 6.2 阶段一：采集

由 `MemoryStore.append_history` 完成：
- 每次会话 Consolidator 归档时调用
- 原子分配自增 cursor
- 追加到 `history.jsonl`
- 内容经 `strip_think` 清理
- 记录 `session_key` 用于会话隔离

### 6.3 阶段二：整合

#### 6.3.1 触发方式

- **定时触发**：由 cron 系统任务自动执行，默认每 2 小时（`DreamConfig.interval_h`）
- **手动触发**：用户执行 `/dream` 命令（[command/builtin.py:306](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/command/builtin.py#L306)）

#### 6.3.1a 多批次循环处理

Dream 每次运行支持**多批次循环**（`_DREAM_MAX_BATCHES = 3`），在一次运行中连续处理多个 20 条批次，加速消化积压历史：

```
while batches < _DREAM_MAX_BATCHES:
    result = build_dream_prompt()      # 取下一批 20 条
    if result is None: break           # 无更多历史
    resp = process_direct(...)         # LLM 整合
    if not dream_run_completed: break  # 未完成则停止
    set_last_dream_cursor(last_cursor) # 推进游标
    batches += 1
```

- **token 控制**：最大 3 批次 × 20 条 = 60 条/次，平衡积压消化速度与 token 消耗
- **安全停止**：任一批次未干净完成则立即停止，游标不推进
- **进度报告**：运行结束后报告处理批次数和剩余 pending 条目数
- **常量复用**：`cmd_dream` 和 cron job 共享 `_DREAM_MAX_BATCHES` 常量（[builtin.py:20](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/command/builtin.py#L20)）

#### 6.3.2 提示词构建

`build_dream_prompt`（[memory.py:467](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L467)）：

1. 读取 `.dream_cursor`，获取未处理的历史条目
2. 取最近 `max_entries=20` 条
3. 每条截断到 500 字符
4. 渲染 [dream.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/agent/dream.md) 模板
5. 拼接历史上下文

#### 6.3.3 受限工具集

`build_dream_tools`（[memory.py:491](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L491)）构建一个**受限**的 ToolRegistry，仅包含：

| 工具 | 可读路径 | 可写路径 |
|------|---------|---------|
| `ReadFileTool` | workspace + 内置 skills 目录 | — |
| `EditFileTool` | — | memory_dir + SOUL.md + USER.md + skills/ |
| `ApplyPatchTool` | — | memory_dir + SOUL.md + USER.md + skills/ |
| `WriteFileTool` | — | skills/ 目录 |

这种最小权限设计确保 Dream 只能修改记忆文件，不能执行任意代码或影响其他系统。

#### 6.3.4 Dream 提示词设计要点

[dream.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/agent/dream.md) 模板的核心设计：

- **文件路由明确**：每个事实路由到唯一规范文件
- **MECE 强制**：跨文件不重复，多文件冲突时保留最具体副本
- **历史属性标签处理**：读取 Consolidator 标签作为路由和保留提示
- **删除优先**：移除陈旧内容与添加新事实同等重要
- **原子事实**：提取最小可独立事实，而非笼统描述
- **纠正机制**：新信息与旧信息冲突时，原地替换而非追加
- **技能发现**：识别重复 2+ 次的工作流，创建新 SKILL.md
- **年龄衰减**：冲刺目标保留当前+下一个；架构决策无限期保留；基础设施变更原地更新

#### 6.3.5 完成判定与游标推进

```python
@staticmethod
def dream_run_completed(resp: object | None) -> bool:
    metadata = getattr(resp, "metadata", None)
    return isinstance(metadata, dict) and metadata.get("_stop_reason") == "completed"
```

只有当 Dream 会话**干净完成**时，才推进 `.dream_cursor`。未完成时游标保持不变，下次 Dream 会重新处理这批历史。

#### 6.3.6 会话管理

- `dream_session_key()` 返回 `dream:YYYYMMDD-HHMMSS` 格式的唯一会话键
- Dream 会话标记为 `ephemeral=True`，不持久化到正常会话列表
- `prune_dream_sessions` 保留最近 10 个 Dream 会话文件，自动清理更旧的

---

## 7. AutoCompact：空闲会话自动压缩

[AutoCompact](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/autocompact.py) 处理长时间空闲的会话，避免累积过多历史。

### 7.1 触发条件

- `session_ttl_minutes`（默认 15 分钟）配置空闲阈值
- 会话 `updated_at` 超过 TTL 且当前无在途 Agent 任务时触发
- 排除内部会话（`dream:` 前缀）

### 7.2 压缩流程

`compact_idle_session`（[memory.py:971](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L971)）：

1. 获取会话级整合锁
2. 重新加载会话（可能已被其他流程替换）
3. 保留最近 `_RECENT_SUFFIX_MESSAGES = 8` 条合法消息后缀
4. 对被移除的消息调用 `archive` 摘要
5. 用保留的后缀替换会话消息
6. 重置 `last_consolidated = 0`
7. 持久化摘要到 `session.metadata["_last_summary"]`

### 7.3 摘要注入

`prepare_session` 在下次该会话被激活时：
- **热路径**：从内存 `_summaries` 字典获取（进程未重启）
- **冷路径**：从 `session.metadata["_last_summary"]` 获取（进程重启后）

摘要以 `[Archived Context Summary]` 形式注入到系统提示词。

---

## 8. 上下文注入

[ContextBuilder](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/context.py#L51) 负责将记忆注入到 LLM 的系统提示词中。

### 8.1 系统提示词组装顺序

```
1. Identity (workspace, runtime, platform policy, channel)
2. Bootstrap Files (AGENTS.md, SOUL.md, USER.md)
3. Tool Contract
4. Memory (MEMORY.md 长期事实，跳过未自定义模板)
5. Active Skills (always: true 的技能)
6. Skills Summary (其他技能摘要)
7. Recent History (history.jsonl 未处理条目，最多 50 条 / 8000 tokens)
8. Session Summary (AutoCompact/Consolidator 摘要)
```

### 8.2 历史注入策略

`read_recent_history_for_prompt`（[memory.py:365](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L365)）：

- 读取 `.dream_cursor` 之后的未处理历史
- **会话隔离模式**（默认）：只返回当前 `session_key` 的条目
- **统一会话模式**（`unified_session=True`）：返回当前会话条目 + 非内部会话条目（排除 `cron:`、`dream:`、`heartbeat`）
- 最多 50 条，截断到 8,000 tokens

#### 8.2a 积压历史 Backlog 摘要

当未处理历史超过 `_MAX_RECENT_HISTORY`（50 条）时，`_format_recent_history`（[context.py:184](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/context.py#L184)）不会静默丢弃早期条目，而是将积压区**最接近 recent 窗口的 20 条**（`backlog[-20:]`）截断为 100 字符摘要，以 `[Backlog]` 前缀注入：

```
- [Backlog: 80 earlier entries not shown in full]
  - [2026-06-24 10:00] 用户询问了部署流程...
  - [2026-06-24 10:15] 讨论了数据库迁移方案...
  ...
- [2026-06-24 14:00] 最新条目完整内容
- [2026-06-24 14:05] 另一条完整条目
```

这确保 Agent 至少知道积压历史的存在和大致内容，避免用户感知到"遗忘"。

### 8.3 内部会话识别

```python
_INTERNAL_HISTORY_SESSION_PREFIXES = ("cron:", "dream:")
_INTERNAL_HISTORY_SESSION_KEYS = {"heartbeat"}
```

这些内部会话的历史不会污染用户会话的上下文。

---

## 9. GitStore：版本控制与回滚

[GitStore](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/utils/gitstore.py#L45) 使用 dulwich（纯 Python Git 实现）为记忆文件提供版本控制。

### 9.1 追踪文件

```python
GitStore(workspace, tracked_files=[
    "SOUL.md", "USER.md", "memory/MEMORY.md",
])
```

注意：
- `history.jsonl` **不**纳入 Git 版本控制（频繁追加，体积大）
- `.dream_cursor` **不**纳入 Git 版本控制——`/dream-restore` 回滚记忆文件内容时不应回滚处理进度游标，否则会导致 Dream 重复处理已整合的历史（因为 `history.jsonl` 本身也不回滚）。`.dream_cursor` 的真实值始终保留在文件中，Git 仅用于记忆内容的版本化。

### 9.2 自动提交

- Dream 每次运行后自动提交（`auto_commit`）
- 提交消息包含 Dream 摘要内容
- 作者固定为 `biscuitbot <biscuitbot@dream>`
- `.gitignore` 配置为只追踪指定文件（`/*` 排除所有，再 `!` 白名单）

### 9.3 命令支持

| 命令 | 功能 |
|------|------|
| `/dream` | 手动触发 Dream 整合（多批次循环，最多 3 批次）|
| `/dream-log` | 显示最近 Dream 变更的 diff + 未处理历史 pending 条目数 |
| `/dream-log <sha>` | 显示指定 commit 的 diff |
| `/dream-restore` | 列出可恢复的 Dream 快照 |
| `/dream-restore <sha>` | 恢复到指定快照（创建新 commit 记录回滚，不回滚 `.dream_cursor`）|

`revert` 操作通过将 tracked files 恢复到目标 commit 的父 commit 状态来实现，然后创建一个新的 revert commit，保证历史可追溯。`.dream_cursor` 不在 tracked files 中，因此回滚不会影响 Dream 处理进度。

`/dream-log` 末尾通过 `count_unprocessed_history()`（[memory.py:530](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py#L530)）显示当前 pending 条目数，让用户直观感知 Dream 是否积压。

---

## 10. 配置

### 10.1 DreamConfig

定义于 [config/schema.py:52](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/schema.py#L52)：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 启动时是否注册定时 Dream 任务 |
| `interval_h` | `2` | 触发间隔（小时） |
| `cron` | `null` | 遗留 cron 表达式覆盖（优先于 interval_h） |
| `model_override` | `null` | Dream 专用模型覆盖（未实现） |

### 10.2 相关 AgentDefaults 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `session_ttl_minutes` | `15` | 空闲会话压缩阈值（0 = 禁用） |
| `max_messages` | `120` | 会话回放最大消息数 |
| `consolidation_ratio` | `0.5` | 整合目标比例 |
| `unified_session` | `false` | 跨渠道共享单一会话 |

### 10.3 配置示例

```json
{
  "agents": {
    "defaults": {
      "sessionTtlMinutes": 15,
      "maxMessages": 120,
      "consolidationRatio": 0.5,
      "dream": {
        "enabled": true,
        "intervalH": 2
      }
    }
  }
}
```

---

## 11. 数据流总结

### 11.1 写入路径（信息进入记忆）

```
用户消息
  ↓
AgentLoop 处理 turn
  ↓
Consolidator.maybe_consolidate_by_tokens (token 超预算时)
  ↓
archive() → LLM 摘要 → append_history() → history.jsonl
  ↓
（定时）Dream 读取 .dream_cursor 之后的历史
  ↓
LLM + 受限工具 → 更新 SOUL/USER/MEMORY/SKILL
  ↓
GitStore.auto_commit → 版本控制
```

### 11.2 读取路径（记忆注入上下文）

```
AgentLoop 收到消息
  ↓
AutoCompact.prepare_session (检查空闲压缩)
  ↓
Consolidator.maybe_consolidate_by_tokens (检查 token 预算)
  ↓
ContextBuilder.build_system_prompt
  ├── Bootstrap Files (AGENTS.md, SOUL.md, USER.md)
  ├── Memory (MEMORY.md)
  ├── Recent History (history.jsonl 未处理条目)
  └── Session Summary (上次压缩摘要)
  ↓
LLM 调用
```

### 11.3 搜索路径（Agent 主动检索）

Agent 可通过内置 `grep` 工具搜索 `history.jsonl`（见 [skills/memory/SKILL.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/skills/memory/SKILL.md)）：

```
grep(pattern="keyword", path="memory/history.jsonl", case_insensitive=true)
grep(pattern="2026-04-02 10:00", path="memory/history.jsonl", fixed_strings=true)
grep(pattern="keyword", path="memory", glob="*.jsonl", output_mode="count")
```

`history.jsonl` **不**加载到上下文，避免 token 膨胀，按需检索。

---

## 12. 设计亮点

### 12.1 两阶段解耦

- **阶段一（Consolidator）**：实时、会话内、token 驱动，保证会话不超窗口
- **阶段二（Dream）**：定时、跨会话、结构化，沉淀长期记忆
- 两者通过 `history.jsonl` 和 cursor 解耦，互不阻塞

### 12.2 MECE 分类

四类文件职责严格分离，Dream 强制去重和路由，避免信息散落多处。

### 12.3 游标驱动

- `.cursor`：history.jsonl 写入游标，保证原子自增
- `.dream_cursor`：Dream 处理游标，保证至少一次处理
- `session.last_consolidated`：会话内整合游标，保证消息不重复归档

### 12.4 全链路原子写入

所有记忆文件的写入均使用原子操作（temp + rename + fsync）：
- `atomic_write_text`（[helpers.py:355](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/utils/helpers.py#L355)）统一处理 `.cursor`、`.dream_cursor`
- `_write_entries` 处理 `history.jsonl` 整文件重写
- `append_history` 追加后 fsync 保证数据落盘
- `compact_history` 在 `_append_lock` 内执行，消除并发竞态

### 12.5 多批次积压消化

Dream 每次运行循环处理最多 3 批次（60 条），加速消化积压历史，同时通过 `_DREAM_MAX_BATCHES` 上限控制 token 消耗。`/dream-log` 显示 pending 条目数，让用户感知积压状态。

### 12.6 渐进式降级

- LLM 摘要失败 → `raw_archive` 原始转储
- Dream 未完成 → 游标不推进，下次重试
- history.jsonl 损坏 → 跳过非法条目，告警一次
- `_read_last_entry` 失败 → 回退到全文件扫描
- 积压历史超 50 条 → Backlog 摘要保留而非静默丢弃
- `compact_history` 驱逐 → 归档到 `history-archive-*.jsonl` 而非删除

### 12.7 最小权限

Dream 工具集严格限制为只读 + 记忆文件编辑，无法执行 shell、网络请求或影响系统状态。

### 12.8 可观测可回滚

- Git 版本控制所有长期记忆变更（`.dream_cursor` 故意排除，避免回滚导致重复处理）
- `/dream-log` 可视化 diff + pending 进度
- `/dream-restore` 一键回滚（仅回滚记忆内容，不回滚处理进度）
- `history-archive-*.jsonl` 保留被 compact 的历史条目供审计

---

## 13. 关键文件索引

| 文件 | 职责 |
|------|------|
| [biscuitbot/agent/memory.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/memory.py) | MemoryStore + Consolidator 核心 |
| [biscuitbot/agent/context.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/context.py) | 上下文构建与记忆注入（含 Backlog 摘要） |
| [biscuitbot/agent/autocompact.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/autocompact.py) | 空闲会话自动压缩 |
| [biscuitbot/agent/loop.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/agent/loop.py) | Agent 主循环，整合触发点 |
| [biscuitbot/utils/helpers.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/utils/helpers.py) | `atomic_write_text` 原子写入工具 |
| [biscuitbot/utils/gitstore.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/utils/gitstore.py) | Git 版本控制 |
| [biscuitbot/templates/agent/dream.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/agent/dream.md) | Dream 提示词模板 |
| [biscuitbot/templates/agent/consolidator_archive.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/agent/consolidator_archive.md) | Consolidator 摘要模板（含 session_key 上下文） |
| [biscuitbot/templates/memory/MEMORY.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/memory/MEMORY.md) | MEMORY.md 模板 |
| [biscuitbot/templates/SOUL.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/SOUL.md) | SOUL.md 模板 |
| [biscuitbot/templates/USER.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/templates/USER.md) | USER.md 模板 |
| [biscuitbot/skills/memory/SKILL.md](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/skills/memory/SKILL.md) | 记忆系统技能说明 |
| [biscuitbot/command/builtin.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/command/builtin.py) | /dream 系列命令 + `_DREAM_MAX_BATCHES` 常量 |
| [biscuitbot/config/schema.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/schema.py) | DreamConfig 配置 |
| [biscuitbot/cli/commands.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/cli/commands.py) | Dream cron 任务（多批次循环） |
| [tests/agent/test_memory_store.py](file:///Volumes/data/hczkAgent/nanobot/tests/agent/test_memory_store.py) | MemoryStore 测试（含归档、计数测试） |
| [tests/command/test_builtin_dream.py](file:///Volumes/data/hczkAgent/nanobot/tests/command/test_builtin_dream.py) | Dream 命令测试（含多批次循环测试） |
