# 安全机制、WebUI 与配置系统文档

本文档涵盖 biscuitbot 的安全防护机制、WebUI 后端服务与配置系统实现。所有路径策略与 SSRF 防护为结构性边界，不随防护等级关闭。

## 1. 安全机制概览

biscuitbot 的安全机制分为两层：

- **结构性边界**（始终启用）：SSRF 网络隔离、工作区路径边界、环境变量白名单、子代理隔离、运行时上下文标记。这些不是提示注入防线，关闭会造成与注入无关的严重损害。
- **可配置防护等级**（`guard_level`）：针对提示注入与 Shell 拦截的分级策略，由 [guard_level.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/security/guard_level.py) 统一管理。

此外包含配对审批、凭据保护等运行时机制。

## 2. SSRF 防护

实现于 [network.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/security/network.py)。

### 2.1 封锁的网络段

`_BLOCKED_NETWORKS` 默认拦截以下网段：

| 网段 | 说明 |
|------|------|
| `0.0.0.0/8` | 本机网络 |
| `10.0.0.0/8` | RFC1918 私有 |
| `100.64.0.0/10` | 运营商级 NAT |
| `127.0.0.0/8` | 环回 |
| `169.254.0.0/16` | 链路本地 / 云元数据 |
| `172.16.0.0/12` | RFC1918 私有 |
| `192.168.0.0/16` | RFC1918 私有 |
| `::1/128` | IPv6 环回 |
| `fc00::/7` | IPv6 唯一本地地址 |
| `fe80::/10` | IPv6 链路本地 |

### 2.2 核心函数

- `validate_url_target(url, *, allow_loopback=False) -> (ok, error)`：校验 scheme（仅 http/https）、netloc、解析后的 IP。`allow_loopback` 仅在所有解析地址均为字面环回地址时放行，不允许 RFC1918、链路本地、元数据或解析到环回的公网域名。
- `validate_resolved_url`：DNS 解析后对实际 IP 再次校验，防止 TOCTOU。
- `_normalize_addr`：将 IPv6 映射的 IPv4（`::ffff:127.0.0.1`）归一化为 IPv4，确保黑名单匹配生效。
- `contains_internal_url`：扫描命令字符串中的内部 URL。

### 2.3 SSRF 白名单

`configure_ssrf_whitelist(cidrs)` 允许指定 CIDR 绕过封锁（如 Tailscale 的 `100.64.0.0/10`）。配置项 `ToolsConfig.ssrf_whitelist`（列表）在启动时加载。

## 3. 工作区边界

实现于 [workspace_policy.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/security/workspace_policy.py) 与 [workspace_access.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/security/workspace_access.py)。

### 3.1 路径校验

- `WorkspaceBoundaryError(PermissionError)`：路径越界时抛出，附带 `WORKSPACE_BOUNDARY_NOTE`（明确这是硬策略边界，禁止用 Shell 技巧绕过）。
- `resolve_path(path, workspace, *, strict)`：相对路径基于 workspace 解析后 `resolve()`。
- `is_path_within(path, root)` / `is_path_allowed(path, roots)`：判断路径是否在根目录内。
- `require_path_within(path, root, *, message)`：解析并强制要求路径在 root 内，否则抛 `WorkspaceBoundaryError`。

### 3.2 WorkspaceScopeResolver

`WorkspaceAccessMode` 为 `"restricted"` 或 `"full"`，通过 `WORKSPACE_SCOPE_METADATA_KEY` 注入运行时上下文。`WorkspaceSandboxStatus` 暴露沙箱状态：`restrict_to_workspace`、`workspace_root`、`level`、`enforced`、`provider`（`none` / `unknown` / `macos_app_sandbox` / `bwrap`）及 `provider_label`、`summary`。

## 4. 防护等级

实现于 [guard_level.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/security/guard_level.py)。`GuardPolicy` 为 `frozen dataclass`，由 `ToolsConfig.guard_level` 构造。

### 4.1 等级枚举

`VALID_LEVELS = ("standard", "minimal", "off")`，未知值经 `normalize_guard_level` 回退为 `standard`。

### 4.2 等级 → 特性映射表

| 特性 | standard | minimal | off |
|------|:---:|:---:|:---:|
| `shell_denylist`（完整硬编码 deny-list：下载执行、内部状态文件保护等摩擦型模式） | ✓ | ✗ | ✗ |
| `catastrophic_shell_blocks`（灾难命令：`rm -rf`、`mkfs`、`dd` 到设备、fork 炸弹、关机） | ✓ | ✓ | ✗ |
| `untrusted_banner`（`web_fetch` 前置 `[External content …]` 横幅） | ✓ | ✗ | ✗ |
| `untrusted_snippet`（系统提示注入不可信内容片段） | ✓ | ✓ | ✗ |

### 4.3 Shell deny-list 分层

- **STANDARD**：完整 deny-list（灾难性 + 摩擦型模式，如 download-and-execute、内部状态文件保护）。
- **MINIMAL**：仅灾难性命令拦截，移除摩擦型拦截以提升灵活性。
- **OFF**：无 Shell 拦截。

> 注：SSRF 网络隔离、工作区边界、环境变量白名单、子代理隔离、运行时上下文标记为结构性控制，**不随 `guard_level` 关闭**。

## 5. 配对审批

实现于 [pairing/store.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/pairing/store.py)。持久化于 `~/.biscuitbot/pairing.json`，按 channel 维护 `approved` 与 `pending` 两个集合。

### 5.1 配对码生成与审批流程

- 字母表 `_ALPHABET = ascii_uppercase + digits`，长度 `_CODE_LENGTH = 8`，格式如 `ABCD-EFGH`。
- 默认 TTL `_TTL_DEFAULT_S = 600`（10 分钟），`_gc_pending` 在每次加载时清理过期 pending 项。
- `_save` 通过 `_write_text_atomic` 原子写入；`threading.Lock` 保证同步 CLI 与异步 channel handler 并发安全。
- approved 列表加载时转为 `set` 以 O(1) 查询，保存时转回有序 list 序列化。

### 5.2 日志脱敏

`_mask_code(code)` 仅保留分隔符前的头部，尾部替换为 `****`（如 `ABCD-****`），避免明文配对码进入日志。

## 6. 凭据保护

- **配置文件权限**：加载配置时尽力 `chmod 0o600`（Windows 上为 no-op）。涉及 [loader.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/loader.py)（line 149）、[websocket.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/websocket.py)（line 484）及 whatsapp/weixin/matrix 等渠道状态文件。
- **HTTP token 仅限 Authorization 头**：[gateway_tokens.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/gateway_tokens.py) 中 `check_api_token` 仅从 `Authorization` 头读取 token，query-param token 仅保留给 WebSocket 握手（浏览器限制）。token 形如 `nbwt_<secrets.token_urlsafe(32)>`。
- **无 secret 时 localhost-only**：[websocket.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/websocket.py) 的 `wildcard_host_requires_auth` 校验——当 `host` 为 `0.0.0.0` 或 `::`（全接口）时，必须设置 `token` 或 `token_issue_secret`，否则启动报错，防止未认证暴露。

## 7. 配置系统

### 7.1 配置加载

实现于 [loader.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/loader.py)。

- **双格式**：JSON 为主，YAML 为遗留格式。`_migrate_yaml_to_json` 在无 `config.json` 时自动将 `config.yaml`/`config.yml` 迁移为 JSON。
- **查找顺序**：`get_config_path()` 优先返回 `set_config_path` 设置的路径（多实例支持），否则回退 `~/.biscuitbot/config.json`。
- **环境变量引用**：`${VAR}` 模式在纯字符串/字典/列表中递归解析（`os.environ.get`），未定义变量解析为空。
- **遗留迁移**：YAML → JSON 自动迁移，保留原 YAML。

### 7.2 核心 Schema

实现于 [schema.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/schema.py)，基于 pydantic `BaseSettings`。

#### Config（根配置）

包含 `agents`、`channels`、`transcription`、`providers`、`api`、`gateway`、`tools`、`model_presets`。`_validate_model_preset` 校验 `default` 预留名及 `model_preset`/`fallback_models` 引用有效性。`resolve_preset` 返回具名预设或隐式默认。

#### AgentDefaults

关键字段：`workspace`、`model`、`provider`、`max_tokens`(8192)、`context_window_tokens`(65536)、`temperature`(0.1)、`fallback_models`、`max_tool_iterations`(200)、`max_concurrent_subagents`(≥1)、`max_tool_result_chars`(16000)、`timezone`("Asia/Shanghai")、`bot_name`、`unified_session`、`disabled_skills`、`session_ttl_minutes`(15, 别名 `idleCompactAfterMinutes`)、`max_messages`(120)、`consolidation_ratio`(0.5)、`dream`、`vision_model`。

#### ToolsConfig

| 字段 | 默认 | 说明 |
|------|------|------|
| `restrict_to_workspace` | `False` | 工具访问尽量限制在工作区内 |
| `guard_level` | `standard` | 防护等级 standard/minimal/off |
| `webui_allow_local_service_access` | `True` | WebUI Full Access 时允许 localhost 服务访问（遗留 `allowLocalPreviewAccess`） |
| `mcp_servers` | `{}` | MCP 服务器配置 |
| `ssrf_whitelist` | `[]` | 绕过 SSRF 封锁的 CIDR |
| `cold_storage_days` | `14` | 工具/技能多少天未调用转入冷门仓库（0=禁用，0–365） |
| `duplicate_similarity_threshold` | `0.6` | 重复检测 token 重叠率阈值（0.0–1.0） |

另含各工具子配置：`web`、`exec`、`file`、`cli_apps`、`my`、`image_generation`、`screenshot`。

#### DreamConfig

记忆整合配置：`enabled`、`interval_h`(2)、`cron`(遗留覆盖)、`model_override`、`build_schedule(timezone)` 优先使用 cron 否则按 `interval_h` 生成 `every` 调度。

### 7.3 运行时路径

实现于 [paths.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/config/paths.py)，全部基于 `get_config_path().parent` 派生实例级数据目录。

| 函数 | 用途 |
|------|------|
| `get_config_path` | 委托 loader，惰性导入避免循环依赖 |
| `get_data_dir` | 实例运行时数据目录 |
| `get_runtime_subdir(name)` | 命名子目录 |
| `get_media_dir(channel)` | 媒体目录（可按 channel 命名空间） |
| `get_cron_dir` / `get_logs_dir` / `get_webui_dir` | cron / 日志 / WebUI 持久化线程 |
| `get_workspace_path(workspace)` | 代理工作区（默认 `~/.biscuitbot/workspace`） |

## 8. WebUI 后端

### 8.1 GatewayServices

实现于 [gateway_services.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/gateway_services.py)。`GatewayServices` 为 `frozen dataclass`，显式声明 WebSocket 传输与 HTTP 路由共享的依赖：`http`、`tokens`、`media`、`transcripts`、`workspaces`、`session_manager`、`cron_service`、`cron_pending_job_ids`。`build_gateway_services` 组装 `GatewayTokenStore`、`WebUIMediaGateway`、`WebUITranscriptRecorder`、`WebUIWorkspaceController`、`GatewayHTTPHandler`。

### 8.2 设置 API

实现于 [settings_api.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/settings_api.py)。`advanced` 设置载荷包含：`restrict_to_workspace`、`workspace_sandbox`、`webui_allow_local_service_access`、`webui_default_access_mode`、`private_service_protection_enabled`、`ssrf_whitelist_count`、`guard_level`、`cold_storage_days`、`duplicate_similarity_threshold`、`mcp_server_count`、`exec_enabled`、`exec_sandbox`。

更新校验（`_query_first_alias` 同时支持 snake/camel）：

- `cold_storage_days`：整数，范围 0–365（0=禁用冷门轮转）。
- `duplicate_similarity_threshold`：数值，范围 0.0–1.0（夜间维护任务据此报告潜在重复）。

校验失败抛 `WebUISettingsError`（带 HTTP status）。

### 8.3 其他后端服务

- [skills_api.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/skills_api.py)：技能目录与启用/禁用。
- [mcp_presets_api.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/mcp_presets_api.py)：MCP 服务器预设管理。
- [media_api.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/media_api.py)：媒体文件上传/预览。
- [session_automations.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/session_automations.py)：会话自动化任务调度。

### 8.4 版本检查与自更新

#### 版本检查

实现于 [version_check.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/version_check.py)。`check_for_update()` 请求 PyPI API（`https://pypi.org/pypi/biscuitbot/json`）获取最新版本号，与当前 `__version__` 比对。使用 5 分钟缓存（`_CACHE_TTL_S = 300`）避免频繁请求。

API 端点：`GET /api/settings/version-check`，返回 `{ updateAvailable: { currentVersion, latestVersion, pypiUrl } | null }`。

#### 一键自更新

实现于 [settings_routes.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/webui/settings_routes.py) 的 `_handle_settings_self_update`。

API 端点：`GET /api/settings/self-update`（使用 GET 因 websockets 服务器不支持 POST 方法）。

执行流程：
1. 调用 `subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "biscuitbot"])`，超时 120 秒
2. 解析 pip 输出提取新版本号
3. 返回 `{ selfUpdate: { success, newVersion, output }, requires_restart: true }`

前端在设置页面显示「检查更新」按钮，检测到新版本后显示「立即更新」按钮，点击后调用自更新 API，更新成功后提示重启应用。

> **安全考虑**：自更新端点需通过 API token 认证（`_authorized` 检查），且仅执行 `pip install --upgrade biscuitbot`，不接受任意包名或命令参数。

## 9. WebUI 前端

技术栈：React 18 + TypeScript + Vite，UI 基于 Radix UI + Tailwind CSS，i18n 使用 i18next（zh-CN / zh-TW），Markdown 渲染集成 remark/rehype + KaTeX，测试用 Vitest + Testing Library。源码位于 [webui/src](file:///Volumes/data/hczkAgent/nanobot/webui/src)。

目录结构：

- `components/`：`settings/`（设置视图、技能目录、token 用量热图）、`thread/`（会话主界面：消息流、Composer、PromptRail、活动集群、工作区控制）、`ui/`（Radix 基础组件）及顶层组件（`Sidebar`、`ChatList`、`MessageBubble`、`MarkdownText`、`CodeBlock`、`FilePreviewPanel` 等）。
- `hooks/`：会话、流式、附件、剪贴板、技能、主题、语音录制等 React hooks。
- `lib/`：`api.ts`、`biscuitbot-client.ts`、`http.ts`、`workspace.ts`、`tool-traces.ts`、`bootstrap.ts` 等业务逻辑。
- `providers/ClientProvider.tsx`：客户端上下文。
- `i18n/locales/`：多语言资源。
- `workers/imageEncode.worker.ts`：图片编码 Web Worker。
- `tests/`：Vitest 单元/组件测试。
