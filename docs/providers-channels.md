# Provider 与 Channel 系统文档

本文档描述 biscuitbot 的 LLM Provider 子系统与聊天平台 Channel 子系统的设计与实现。

## 1. Provider 系统概览

Provider 子系统负责将统一的 chat/chat_stream 调用适配到不同 LLM 厂商。核心代码位于 [providers/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/) 目录。

- **注册表**：[registry.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/registry.py) 中的 `PROVIDERS` 元组当前登记 9 个提供商（custom、anthropic、openai、deepseek、dashscope、zhipu、moonshot、stepfun、ollama），元组顺序即匹配优先级，网关类优先。
- **后端实现**：2 种 backend —— `openai_compat`（OpenAI 兼容协议，覆盖绝大多数厂商）与 `anthropic`（原生 Anthropic SDK）。backend 字段决定工厂分发到哪个实现类。
- **三大模块**：基类 [base.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/base.py)、工厂 [factory.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/factory.py)、注册表 [registry.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/registry.py)，外加故障转移包装器 [fallback_provider.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/fallback_provider.py)。

## 2. LLMProvider 基类

[base.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/base.py) 中的 `LLMProvider(ABC)` 定义所有 provider 的统一契约与重试策略。

### 重试策略

- `_CHAT_RETRY_DELAYS = (1, 2, 4)`：瞬时失败时的指数退避间隔（秒）。
- `_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})`：可重试的 HTTP 状态码。
- `_PERSISTENT_IDENTICAL_ERROR_LIMIT = 10`：连续相同错误上限，超过则放弃重试。
- `chat_stream_with_retry` 在流式场景下复用同一策略，并通过 `on_stream_recover` 回调支持跨分段恢复。

### 429 语义分类

并非所有 429 都可重试。基类将 429 细分两类：

- **可重试**（`_RETRYABLE_429_ERROR_TOKENS`）：`rate_limit_exceeded`、`too_many_requests`、`overloaded_error` 等并发/速率类。
- **不可重试**（`_NON_RETRYABLE_429_ERROR_TOKENS`）：`insufficient_quota`、`quota_exhausted`、`billing_hard_limit_reached`、`insufficient_balance`、`payment_required` 等配额/计费类 —— 重试无意义，应直接走故障转移。

文本侧另有 `_RETRYABLE_429_TEXT_MARKERS` 与 `_NON_RETRYABLE_429_TEXT_MARKERS`，用于缺少结构化错误码时的兜底匹配（含中文「速率限制」）。

### 瞬时错误识别

`_TRANSIENT_ERROR_MARKERS` 元组覆盖 `429`、`500`、`502`、`503`、`504`、`overloaded`、`timeout`、`connection`、`server error`、`temporarily unavailable`、`访问量过大` 等中英文标记。`_is_transient_response` 优先使用结构化 `error_should_retry` / `error_status_code`，文本标记仅作旧 provider 兜底。

### 流式空闲超时

通过环境变量 `BISCUITBOT_STREAM_IDLE_TIMEOUT_S` 配置，`resolve_stream_idle_timeout_s` 负责安全解析：默认 `90.0` 秒，上限 `3600.0` 秒；非法或非正值回退默认，超过上限则钳制。超时后视为瞬时错误触发重试。

### 抽象方法

- `chat(...)` —— 一次性补全，子类必须实现。
- `get_default_model()` —— 返回默认模型标识，子类必须实现。
- `chat_stream(...)` —— 流式补全，提供默认实现（退化为 `chat` 并把完整内容作为单 delta 回调），支持原生流式的 provider 覆写之。回调包括 `on_content_delta`、`on_thinking_delta`、`on_tool_call_delta`。

`LLMResponse` 携带 `content`、`tool_calls`、`reasoning_content`、`thinking_blocks`，以及结构化错误元数据（`error_status_code`、`error_kind`、`error_retry_after_s` 等）。

## 3. Provider 实例化

[factory.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/factory.py) 负责从配置创建 provider 实例。

### ProviderSnapshot 不可变快照

`@dataclass(frozen=True)` 的 `ProviderSnapshot` 封装 `provider`、`model`、`context_window_tokens` 与 `signature` 四元组。frozen 保证快照可哈希、可比较，用于检测配置是否变化。

### backend 分发

`_make_provider_core` 解析 preset 与 provider 名后，按 `spec.backend` 分发：

- `anthropic` → 实例化 `AnthropicProvider`（api_key、api_base、default_model、extra_headers）。
- 其余（含 `openai_compat` 与未识别）→ 实例化 `OpenAICompatProvider`，附带 `spec`、`extra_body`、`api_type`、`extra_query` 等参数。

未在注册表中登记但配置了 `api_base` 的 provider，经 `create_dynamic_spec` 生成动态 spec（`is_direct=True`，自动剥离模型前缀）。纯转写 provider（`is_transcription_only`）在此被拒绝。

### signature 触发重建

`signature` 元组汇总影响实例的身份字段（key、base、model、headers 等）。调用方比对前后两次 signature 即可判断是否需要重建 provider，避免无谓的重新实例化。

## 4. Provider 注册表

[registry.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/registry.py) 是 provider 元数据的唯一真相源。

- `PROVIDERS: tuple[ProviderSpec, ...]` 按**优先级排序**，注释明确「Order = priority」，网关类排在前面。新增 provider 只需追加一个 `ProviderSpec` 并在 `config/schema.py` 的 `ProvidersConfig` 加字段即可。
- `ProviderSpec` 为 `frozen=True` dataclass，核心字段：`name`（配置字段名）、`keywords`（模型名匹配关键字）、`env_key`（API Key 环境变量）、`display_name`、`backend`。
- 网关/本地检测：`is_gateway`（路由任意模型）、`is_local`（本地部署）、`detect_by_key_prefix`、`detect_by_base_keyword`、`default_api_base`。
- 网关行为：`strip_model_prefix` / `strip_model_prefixes`（发送前剥离 `provider/` 前缀）、`supports_max_completion_tokens`。
- 推理控制：`thinking_style`（`thinking_type` / `enable_thinking` / `reasoning_split`）、`gateway_reasoning_style`（`reasoning_effort`）、`reasoning_as_content`。
- 查询函数：`find_by_name` 按名查找；`create_dynamic_spec` 构造动态 spec。

## 5. 故障转移

[fallback_provider.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/fallback_provider.py) 的 `FallbackProvider(LLMProvider)` 包装主 provider 并透明切换到备用模型。

- **主备切换**：主模型在流出内容前返回可转移错误（`_FALLBACK_ERROR_KINDS`：timeout、connection、server_error、rate_limit、overloaded）时，按顺序逐个尝试 `fallback_presets`。已流出内容则不切换以避免重复输出；流式超时是例外 —— 通过 `on_stream_recover` 回调关闭当前分段后在新分段续接。
- **熔断器**：主 provider 连续失败计数 `_primary_failures` 达到 `_PRIMARY_FAILURE_THRESHOLD = 3` 即跳闸，冷却 `_PRIMARY_COOLDOWN_S = 60` 秒；冷却结束后半开试探一次。
- **不可转移错误**（`_NON_FALLBACK_ERROR_KINDS`：authentication、permission、content_filter、refusal、context_length、invalid_request）直接抛回，不消耗备用配额。
- 设计上请求级无状态、递归故障转移由工厂返回纯 provider 来防止。

## 6. 辅助 Provider

- **图像生成**：[image_generation.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/image_generation.py) 定义 `ImageGenerationError` 与 `GeneratedImageResponse`（images、content、raw），支持 DashScope 万相、Zhipu CogView、Gemini Imagen、Ollama 等多端点，默认超时 120 秒。
- **语音转写**：[transcription.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/providers/transcription.py) 适配 OpenAI Whisper、Groq 等兼容端点。`_resolve_transcription_url` 兼容 chat 风格 base 与完整 `/audio/transcriptions` 路径。自带重试：`_MAX_RETRIES = 3`、`_BACKOFF_S = (1.0, 2.0, 4.0)`、`_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}`，覆盖 Whisper 端点偶发 502/503 与移动网络读写错误。

## 7. Channel 系统概览

Channel 子系统适配各聊天平台，核心位于 [channels/](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/) 目录。当前内置 **16 个** channel 模块，通过 [registry.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/registry.py) 的自动发现机制加载，并支持经 `entry_points` 注册的外部插件。

## 8. BaseChannel 抽象基类

[base.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/base.py) 的 `BaseChannel(ABC)` 定义三个抽象方法：

- `start()` —— 连接平台并长驻监听入站消息，转交 `bus.publish_inbound`。
- `stop()` —— 停止并清理资源。
- `send(msg: OutboundMessage)` —— 发送出站消息，失败时抛异常以便管理器统一重试。

### 流式接口（默认空实现，按需覆写）

- `send_delta(chat_id, delta, metadata)` —— 投递流式文本块；状态化实现须按 `_stream_id` 而非仅 `chat_id` 缓存。
- `send_reasoning_delta(...)` / `send_reasoning_end(...)` —— 推理/思考内容流，平台可用原生低强调控件渲染（Slack context block、Telegram 折叠引用、Discord subtext 等）。
- `send_file_edit_events(...)` —— 结构化实时文件编辑事件，富界面可借此展示编辑进度。
- `supports_streaming` 属性：配置启用 streaming 且子类覆写了 `send_delta` 时为真。

### 权限控制

`is_allowed(sender_id)` 按优先级判定：`allow_from` 含 `"*"` → 放行；精确匹配 allowlist → 放行；`is_approved` 配对库命中 → 放行；否则拒绝。未授权 DM 场景下发配对码（`generate_code` / `format_pairing_reply`）。`_handle_message` 在 `supports_streaming` 时自动注入 `_wants_stream` 元数据。

## 9. ChannelManager

[manager.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/manager.py) 的 `ChannelManager` 统管 channel 生命周期与消息路由。

- **初始化** `_init_channels`：先用 `discover_channel_names()` 廉价列出模块名（零导入），再仅导入 `enabled=True` 的 channel；websocket channel 额外构建 gateway 服务。逐 channel 应用 `send_progress` / `send_tool_hints` / `show_reasoning` 布尔覆写（支持 camelCase 别名），并校验 `allow_from` 缺省时进入「配对码模式」。
- **启停**：`start_all()` 创建出站分发协程并并发启动所有 channel；`stop_all()` 先取消分发协程再逐个 stop。
- **出站路由** `_dispatch_outbound`：从消息总线消费 `OutboundMessage`，按 `msg.channel` 路由到对应 channel。带内容指纹去重（`_fingerprint_content` + `_origin_reply_fingerprints`），抑制对同一 origin 的重复回复。
- **指数退避重试** `_send_with_retry`：失败按 `_SEND_RETRY_DELAYS = (1, 2, 4)` 秒退避重试，上限由 `config.channels.send_max_retries` 控制；`CancelledError` 一律上抛以支持优雅关停。

### 自动发现

[registry.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/registry.py) 提供三层发现：

- `discover_channel_names()`：用 `pkgutil.iter_modules` 扫描 `biscuitbot.channels` 包，排除 `base`/`manager`/`registry` 内部模块，**不触发第三方 SDK 导入**。
- `load_channel_class(module_name)`：导入模块并返回首个 `BaseChannel` 子类。
- `discover_plugins(enabled_names)`：经 `importlib.metadata.entry_points(group="biscuitbot.channels")` 加载外部插件；内置 channel 优先，插件不得遮蔽同名内置项。
- `discover_enabled` / `discover_all`：合并内置与外部结果。

## 10. Channel 清单

| 名称 | 文件 | 说明 |
|------|------|------|
| dingtalk | [dingtalk.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/dingtalk.py) | 钉钉机器人 |
| discord | [discord.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/discord.py) | Discord |
| email | [email.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/email.py) | 电子邮件（IMAP/SMTP） |
| feishu | [feishu.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/feishu.py) | 飞书 |
| matrix | [matrix.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/matrix.py) | Matrix 协议 |
| mochat | [mochat.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/mochat.py) | MoChat |
| msteams | [msteams.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/msteams.py) | Microsoft Teams |
| napcat | [napcat.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/napcat.py) | NapCat（QQ 协议） |
| qq | [qq.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/qq.py) | QQ |
| signal | [signal.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/signal.py) | Signal |
| slack | [slack.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/slack.py) | Slack |
| telegram | [telegram.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/telegram.py) | Telegram |
| websocket | [websocket.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/websocket.py) | WebSocket（WebUI 网关） |
| wecom | [wecom.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/wecom.py) | 企业微信 |
| weixin | [weixin.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/weixin.py) | 微信 |
| whatsapp | [whatsapp.py](file:///Volumes/data/hczkAgent/nanobot/biscuitbot/channels/whatsapp.py) | WhatsApp |
