# 工具系统设计文档

## 1. 概述

hczkbot 的工具系统采用 **渐进式发现（Progressive Discovery）** 核心理念：不把所有工具的完整 JSON Schema 一次性塞给模型，而是只默认下发少量"常驻"工具的 schema，其余工具通过 `INDEX.md` 目录索引暴露名称与一句话能力描述，由模型按需通过 `discover_tools` 元工具加载完整 schema 后再调用。

该设计在工具数量增长时仍能控制系统提示词体积，同时保留可发现性。围绕这一理念，系统还提供：冷门仓库（长期未调用工具的轮转与自动恢复）、夜间维护（文档一致性 / 重复检测 / 冷门轮转三合一 cron）、运行时自定义工具注册等能力。

核心入口：[registry.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/registry.py) 中的 `ToolRegistry`。

## 2. 工具注册与自动发现

[loader.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/loader.py) 中的 `ToolLoader` 提供两条发现路径：

1. **pkgutil 包扫描**：遍历 `hczkbot.agent.tools` 包下所有模块（跳过 `base`、`schema`、`registry`、`context`、`loader`、`config`、`file_state`、`sandbox`、`mcp`、`__init__`、`runtime_state` 以及以 `_` 开头的模块），收集非抽象 `Tool` 子类。受 `_plugin_discoverable` 与 `_scopes` 控制。
2. **entry_points 插件**：通过 `entry_points(group="hczkbot.tools")` 加载外部插件，内置工具同名时优先保留内置。

`load()` 时按 `tool_cls.enabled(ctx)` 过滤、`tool_cls.create(ctx)` 实例化、`registry.register(tool)` 注册，内置优先于插件。

[base.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/base.py) 中 `Tool` 基类的关键属性：

| 属性 | 含义 |
|------|------|
| `_capability: str` | 一句话能力边界，用于 INDEX.md 检索与紧凑摘要；为空时回退到 `description[:120]` |
| `_always_include: bool` | 为 `True` 时每轮都下发完整 schema，其余工具需 `discover_tools` 加载 |
| `_usage_md: str` | 使用说明 md 路径（约定 `docs/<name>.md`），模型调用前用 `read_file` 读取 |
| `_custom: bool` | 运行时由 `register_tool` 注册的自定义工具标记，用于持久化 |
| `_plugin_discoverable: bool` | 是否可被 `ToolLoader` 自动发现 |
| `_scopes: set[str]` | 作用域集合，`load(scope=...)` 时过滤 |

## 3. 渐进式发现机制

### 3.1 INDEX.md 生成

[registry.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/registry.py) 的 `generate_index()` 负责生成注入系统提示词的 `INDEX.md`，逻辑要点：

- **排除 `_always_include` 工具**：其完整 schema 已通过 `tools` 参数下发，列入索引会冗余。
- **排除冷门工具**：`self._usage_stats.is_cold(name)` 为 `True` 的工具已被轮转到冷门仓库，需通过 `cold_storage` 搜索。
- **三个头部说明**：①已加载工具的 schema 在 tools 参数中；②按需工具使用流程（read_file → discover_tools → 调用）；③不满足需求时调用 `cold_storage` 搜索冷门仓库。
- **两段表格**：`On-demand Discovery`（按需工具）与 `Skills`（技能，由调用方传入 `skills_entries`）。每行 `| name | capability | usage_md |`。
- 结果缓存于 `_cached_index`，注册/卸载/`set_usage_stats` 时失效。

### 3.2 discover_tools 元工具

[discover.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/discover.py) 中的 `DiscoverToolsTool`（`_always_include=True`）接收 `query` 与可选 `limit`（默认 5，上限 10），调用 `registry.fuzzy_search(query, limit)`：

- 名称精确匹配 +100 分、子串匹配 +20 分；
- 名称 + capability 的 token 重叠数 ×4 分，全命中再 +5 分；
- 按分数降序、名称升序排序后返回 top-N。

返回 payload 包含每个匹配工具的完整 schema 与 `_usage_md` 提示，模型在下一轮即可按精确名称与参数调用。

### 3.3 使用流程

```
1. read_file(usage_md)         # 学习参数细节、示例、注意事项
2. discover_tools("name")      # 加载完整 JSON schema
3. 调用工具(name, ...)          # schema 已可用，按精确名称调用
```

若工具不存在，`prepare_call` 会通过 `_suggest_name` 做大小写/字母数字归一化的"Did you mean"提示。

## 4. 冷门仓库机制

### 4.1 调用热度追踪

[usage_stats.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/usage_stats.py) 中的 `UsageStats` 记录每个工具的 `call_count` 与 `last_called_at`，持久化到工作目录 `.agent_tools/` 下的 `usage_stats.json` 与 `cold_storage.json`。`ToolRegistry.execute` 每次成功调用后通过 `self._usage_stats.record_call(name)` 记录。

### 4.2 冷门轮转

`rotate_cold(registry, threshold_days)` 的规则：

- `threshold_days <= 0` 直接返回（禁用）；
- **`_always_include` 工具永不轮转**（核心基础设施）；
- 已在冷门仓库的工具跳过；
- 未被追踪或 `last_called_at == 0`（从未调用）的工具跳过，给新工具机会；
- 超过阈值未调用的工具构造 `ColdEntry`（含 name / capability / usage_md / source_file / cold_since）写入 `_cold`，返回新增冷门工具列表。

### 4.3 冷门搜索工具

[cold_storage.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/cold_storage.py) 中的 `ColdStorageTool`（`_always_include=True`，名称 `cold_storage`）搜索冷门仓库：`search_cold(query, limit)` 先按 token 重叠评分，回退到子串匹配；`query` 为 `list all` / `全部` / `所有` 时返回全部冷门条目。返回每个条目的 `name / capability / usage_md / source_file` 与"调用后自动恢复"提示。

### 4.4 自动恢复

`UsageStats.record_call(name)` 在记录调用的同时，若该工具在 `_cold` 中则立即删除并保存——即调用冷门工具后自动恢复到活跃索引。`ToolRegistry.execute` 在每次成功调用后触发该记录，形成"搜索 → discover → 调用 → 自动恢复"闭环。

### 4.5 配置

`cold_storage_days`（默认 **14**，`0` 表示禁用冷门轮转），定义于 [schema.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/config/schema.py) 的 `ToolsConfig`。

## 5. 夜间维护

[commands.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/cli/commands.py) 中注册的系统 cron 任务 `nightly_maintenance`，调度 `0 23 * * *`（每天 23:00，时区取 `config.agents.defaults.timezone`）。任务处理逻辑为三合一：

1. **文档一致性检查**：`check_docs_consistency(agent.tools, agent.workspace)` 发现不匹配时，`build_repair_task(mismatches)` 构造修复任务 prompt，通过 `agent.subagents.spawn(..., label="docs-repair", ...)` 派生修复 subagent 自动重写 stale md。
2. **重复检测**：以 `tools_config.duplicate_similarity_threshold` 为阈值调用 `check_duplicates`，发现重复时 `build_duplicate_report` 生成报告并 `logger.warning`（仅报告，不自动合并）。
3. **冷门轮转**：以 `tools_config.cold_storage_days` 为阈值调用 `stats.rotate_cold`，记录新转入冷门仓库的工具。

返回值汇总三步摘要（如 `"docs-repair: 2 mismatch(es); duplicates: 1 pair(s); cold-rotated: 3 tool(s)"`），无变更时返回 `None`。

## 6. 重复检测

[duplicate_check.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/duplicate_check.py) 通过 **Jaccard token 重叠率** 检测功能相似的工具或技能：

- `_tokenize` 对 `name + capability` 小写化、按非字母数字切分，过滤停用词与长度 < 3 的 token；
- `_jaccard(a, b)` 返回 `|交集| / |并集|`，两空集视为 0；
- `check_duplicates(registry, threshold)` 两两比对工具；`check_skill_duplicates(skills_entries, registry, threshold)` 比对技能 vs 工具；
- 命中阈值的对按相似度降序、名称升序返回 `DuplicatePair`（含 `kind`：`TOOL_DUPLICATE` / `SKILL_DUPLICATE`）。

`build_duplicate_report` 生成中文报告，含每对的类型、相似度、共享关键词与合并建议。阈值由 `duplicate_similarity_threshold`（默认 **0.6**）控制。

## 7. 文档一致性检查

[docs_consistency.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/docs_consistency.py) 的 `check_docs_consistency(registry, workspace)` 对每个带 `_usage_md` 的工具检查 4 项匹配：

1. **md 文件存在**：`_resolve_md_path` 依次尝试绝对路径、tools 包相对、workspace 相对。
2. **标题匹配**：md 中需出现 `# {tool.name}`。
3. **参数一致**：从 `tool.parameters.properties` 提取 schema 参数名，从 md 的 `## 参数` / `## Parameters` / `## 参数说明` / `## 参数列表` 表格提取参数名，比对缺失/多余。
4. **能力体现**：`_capability_keywords` 提取 capability 关键词（长度 ≥ 4、非停用词），要求至少一个出现在 md 正文（小写匹配）。

不匹配项汇总为 `DocsMismatch` 列表，`build_repair_task` 据此生成修复 subagent 的任务 prompt（规定 md 格式：标题 / 何时使用 / 参数表 / 调用示例 / 注意事项，中文撰写）。

## 8. 工具清单

| 名称 | 文件 | 能力 | always_include |
|------|------|------|:---:|
| apply_patch | [apply_patch.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/apply_patch.py) | 应用统一 diff 补丁修改文件 | 否 |
| cold_storage | [cold_storage.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/cold_storage.py) | 搜索冷门仓库中被轮转的工具 | 是 |
| complete_goal | [long_task.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/long_task.py) | 完成长任务目标 | 否 |
| cron | [cron.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/cron.py) | 管理 cron 定时任务 | 否 |
| discover_tools | [discover.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/discover.py) | 按需加载工具完整 schema | 是 |
| edit_file | [filesystem.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/filesystem.py) | 精确字符串替换编辑文件 | 否 |
| exec | [shell.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/shell.py) | 执行 shell 命令 | 是 |
| find_files | [search.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/search.py) | 按文件名 glob 模式查找 | 否 |
| generate_image | [image_generation.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/image_generation.py) | 生成图像 | 否 |
| grep | [search.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/search.py) | 正则搜索文件内容 | 否 |
| list_dir | [filesystem.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/filesystem.py) | 列出目录条目 | 否 |
| list_exec_sessions | [exec_session.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/exec_session.py) | 列出运行中的 exec 会话 | 否 |
| long_task | [long_task.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/long_task.py) | 启动长任务 | 否 |
| message | [message.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/message.py) | 发送消息到通道 | 是 |
| my | [self.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/self.py) | 自我描述与配置查询 | 否 |
| read_file | [filesystem.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/filesystem.py) | 读取文件内容 | 是 |
| register_tool | [register_tool.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/register_tool.py) | 注册运行时自定义工具 | 是 |
| run_cli_app | [cli_apps.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/cli_apps.py) | 运行 CLI 应用 | 否 |
| screenshot | [screenshot.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/screenshot.py) | 截屏 | 否 |
| spawn | [spawn.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/spawn.py) | 派生子代理 | 否 |
| unregister_tool | [register_tool.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/register_tool.py) | 卸载自定义工具 | 是 |
| web_fetch | [web.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/web.py) | 抓取 URL 并转 markdown | 否 |
| web_search | [web.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/web.py) | 网页搜索 | 是 |
| write_file | [filesystem.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/filesystem.py) | 写入或覆盖文件 | 否 |
| write_stdin | [exec_session.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/exec_session.py) | 向 exec 会话写入 stdin | 否 |

> MCP 工具（`mcp_` 前缀）由 [mcp.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/agent/tools/mcp.py) 动态生成，名称来自远端 server，未列入上表。

## 9. 配置项

`ToolsConfig`（[schema.py](file:///Volumes/data/hczkAgent/nanobot/hczkbot/config/schema.py) 第 300–346 行）配置项：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `web` | `WebToolsConfig` | 内置默认 | 网页搜索/抓取子配置 |
| `exec` | `ExecToolConfig` | 内置默认 | shell 执行子配置 |
| `file` | `FileToolsConfig` | 内置默认 | 文件操作子配置 |
| `cli_apps` | `CliAppsToolConfig` | 内置默认 | CLI 应用子配置 |
| `my` | `MyToolConfig` | 内置默认 | 自我描述子配置 |
| `image_generation` | `ImageGenerationToolConfig` | 内置默认 | 图像生成子配置 |
| `screenshot` | `ScreenshotToolConfig` | 内置默认 | 截屏子配置 |
| `restrict_to_workspace` | `bool` | `False` | 是否将工具访问限制在工作区内 |
| `guard_level` | `str` | `"standard"` | 提示注入 / shell 拦截防护级别：`standard` / `minimal` / `off` |
| `webui_allow_local_service_access` | `bool` | `True` | 允许 WebUI Full Access shell 检查访问 localhost 服务 |
| `mcp_servers` | `dict[str, MCPServerConfig]` | `{}` | MCP 服务器配置 |
| `ssrf_whitelist` | `list[str]` | `[]` | SSRF 拦截豁免的 CIDR 范围 |
| `cold_storage_days` | `int` | `14` | 工具多少天未被调用即转入冷门仓库；`0` 禁用 |
| `duplicate_similarity_threshold` | `float` | `0.6` | 重复检测的 Jaccard token 重叠率阈值（0.0–1.0） |
