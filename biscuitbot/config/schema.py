"""基于 Pydantic 的配置数据模型。

所属模块与项目作用
===================
本文件位于 biscuitbot/config 目录，定义 biscuitbot 全部配置的数据模型（schema）。
在项目架构中起到的作用：以 Pydantic 模型形式描述渠道、提供商、智能体、工具、网关、
定时任务等配置项，提供校验、别名兼容、字段迁移与默认值，并内置提供商匹配逻辑，
是配置加载与运行时访问配置的权威数据结构来源。
"""
from __future__ import annotations

from pathlib import Path  # 跨平台路径处理
from typing import TYPE_CHECKING, Any, Literal  # 类型标注工具

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator  # Pydantic 模型构建组件
from pydantic_settings import BaseSettings  # 支持环境变量注入的设置基类

from biscuitbot.config_base import Base  # 项目内 Pydantic 模型基类
from biscuitbot.cron.types import CronSchedule  # 定时任务调度类型

if TYPE_CHECKING:
    # 仅类型检查时导入的工具配置类型，避免运行时循环依赖
    from biscuitbot.agent.tools.cli_apps import CliAppsToolConfig
    from biscuitbot.agent.tools.filesystem import FileToolsConfig
    from biscuitbot.agent.tools.image_generation import ImageGenerationToolConfig
    from biscuitbot.agent.tools.screenshot import ScreenshotToolConfig
    from biscuitbot.agent.tools.seedance_video import SeedanceVideoToolConfig
    from biscuitbot.agent.tools.self import MyToolConfig
    from biscuitbot.agent.tools.shell import ExecToolConfig
    from biscuitbot.agent.tools.system_io import SystemIoToolConfig
    from biscuitbot.agent.tools.web import WebToolsConfig


class ChannelsConfig(Base):
    """聊天渠道配置。

    内置与插件渠道配置以额外字段（dict）形式存储。每个渠道在自身 __init__ 中解析
    自己的配置。渠道级 "streaming": true 启用流式输出（需实现 send_delta）。
    """

    model_config = ConfigDict(extra="allow")  # 允许额外字段，便于扩展渠道配置

    send_progress: bool = True  # 将智能体文本进度流式发送到渠道
    send_tool_hints: bool = False  # 流式发送工具调用提示（如 read_file("…")）
    show_reasoning: bool = True  # 当渠道实现时展示模型推理过程
    extract_document_text: bool = True  # 发送给模型前从文档附件中提取文本
    send_max_retries: int = Field(default=3, ge=0, le=10)  # 最大投递尝试次数（含首次发送）
    transcription_provider: str = "groq"  # 已废弃：改用顶层 transcription.provider
    transcription_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")  # 已废弃：改用顶层 transcription.language


class TranscriptionConfig(Base):
    """跨渠道音频转写配置。"""

    enabled: bool = True
    provider: str | None = None  # 由 biscuitbot.audio.transcription_registry 校验
    model: str | None = None
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    max_duration_sec: int = Field(default=120, ge=1, le=600)
    max_upload_mb: int = Field(default=25, ge=1, le=100)


class DreamConfig(Base):
    """Dream 记忆整合配置。"""

    _HOUR_MS = 3_600_000  # 一小时对应的毫秒数

    enabled: bool = True  # 启动时注册周期性 Dream 整合任务
    interval_h: int = Field(default=2, ge=1)  # 默认每 2 小时执行一次
    cron: str | None = Field(default=None, exclude=True)  # 遗留 cron 表达式覆盖
    model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("modelOverride", "model", "model_override"),
    )  # 覆盖 Dream 会话使用的模型（待实现）

    def build_schedule(self, timezone: str) -> CronSchedule:
        """构建运行时调度，优先使用遗留 cron 覆盖。"""
        if self.cron:
            return CronSchedule(kind="cron", expr=self.cron, tz=timezone)
        return CronSchedule(kind="every", every_ms=self.interval_h * self._HOUR_MS)

    def describe_schedule(self) -> str:
        """返回用于日志与启动输出的人类可读调度摘要。"""
        if self.cron:
            return f"cron {self.cron} (legacy)"
        hours = self.interval_h
        return f"every {hours}h"


class InlineFallbackConfig(Base):
    """单个内联回退模型配置。"""

    model: str
    provider: str
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


# 回退模型候选项：可以是预设名（字符串）或内联配置
FallbackCandidate = str | InlineFallbackConfig


class ModelPresetConfig(Base):
    """命名的模型 + 生成参数集合，便于快速切换。"""

    label: str | None = None
    model: str
    provider: str = "auto"
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    temperature: float = 0.1
    reasoning_effort: str | None = None

    def to_generation_settings(self) -> Any:
        """转换为 GenerationSettings 对象。"""
        from biscuitbot.providers.base import GenerationSettings
        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class AgentDefaults(Base):
    """智能体默认配置。"""

    workspace: str = "~/.biscuitbot/workspace"
    model_preset: str | None = None  # 激活的预设名——优先级高于下方字段
    model: str = "deepseek/deepseek-v4-pro"
    provider: str = (
        "auto"  # 提供商名（如 "anthropic"、"openrouter"）或 "auto" 表示自动检测
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65_536
    context_block_limit: int | None = None
    temperature: float = 0.1
    fallback_models: list[FallbackCandidate] = Field(default_factory=list)
    max_tool_iterations: int = 200
    max_concurrent_subagents: int = Field(default=1, ge=1)
    max_tool_result_chars: int = 16_000
    provider_retry_mode: Literal["standard", "persistent"] = "standard"
    tool_hint_max_length: int = Field(
        default=40,
        ge=20,
        le=500,
        validation_alias=AliasChoices("toolHintMaxLength"),
        serialization_alias="toolHintMaxLength",
    )  # 工具提示显示的最大字符数（如 "$ cd …/project && npm test"）
    reasoning_effort: str | None = None  # low / medium / high / adaptive / none — LLM 思考力度；None 保留提供商默认
    timezone: str = "UTC"  # IANA 时区，如 "Asia/Shanghai"、"America/New_York"
    bot_name: str = "biscuitbot"  # CLI 提示中显示的名称（如 "{name} is thinking..."）
    bot_icon: str = "🍪"  # CLI 中显示在名称旁的短图标（emoji 或文本），"" 表示省略
    unified_session: bool = False  # 跨所有渠道共享同一会话（单用户多设备）
    disabled_skills: list[str] = Field(default_factory=list)  # 排除加载的技能名（如 ["summarize", "skill-creator"]）
    session_ttl_minutes: int = Field(
        default=15,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes"),
        serialization_alias="idleCompactAfterMinutes",
    )  # 空闲自动压缩阈值（分钟，0 = 禁用）
    max_messages: int = Field(
        default=120,
        ge=0,
    )  # 从会话历史回放的最大消息数（0 = 使用默认 120，受 token 预算约束）
    consolidation_ratio: float = Field(
        default=0.5,
        ge=0.1,
        le=0.95,
        validation_alias=AliasChoices("consolidationRatio"),
        serialization_alias="consolidationRatio",
    )  # 整合目标比例（0.5 = 压缩后保留 50% 预算）
    dream: DreamConfig = Field(default_factory=DreamConfig)
    vision_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("visionModel", "vision_model"),
        serialization_alias="visionModel",
    )  # 截图理解用的视觉模型预设名（如 "qwen-vl"）；None = 禁用
    vision_model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("visionModelOverride", "vision_model_override"),
        serialization_alias="visionModelOverride",
    )  # 覆盖预设模型的自定义多模态模型名（如 "gpt-4o"、"qwen-vl-max"）；None = 使用预设模型


class AgentsConfig(Base):
    """智能体配置。"""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM 提供商配置。"""

    api_key: str | None = Field(default=None, repr=False)  # repr=False 避免日志泄露密钥
    api_base: str | None = None
    api_type: Literal["auto", "chat_completions", "responses"] = "auto"  # 请求 API 形态
    extra_headers: dict[str, str] | None = None  # 自定义请求头（如 AiHubMix 的 APP-Code）
    extra_body: dict[str, Any] | None = None  # 额外提供商请求字段；结构随提供商/API 形态变化
    extra_query: dict[str, str] | None = None  # 额外查询参数（如 Azure 风格网关的 api-version）


class ProvidersConfig(Base):
    """Configuration for LLM providers.

    三大核心提供商 + 自定义 OpenAI 兼容端点：
      - openai:    OpenAI 官方或兼容 API（GPT、o 系列等）
      - anthropic: Anthropic 官方 API（Claude 系列）
      - deepseek:  DeepSeek 官方 API（DeepSeek-V3/R1 等）
      - ollama:    本地 Ollama 模型服务
      - custom:    任意 OpenAI 兼容端点

    通过 apiBase 可将三家官方 API 指向兼容代理地址，例如：
      - openai.apiBase = "https://your-proxy.com/v1"  → 代理访问 OpenAI 模型
      - anthropic.apiBase = "https://your-proxy.com"  → 代理访问 Claude 模型
      - deepseek.apiBase = "https://your-proxy.com"   → 代理访问 DeepSeek 模型

    也支持通过 extra 字段添加自定义提供商，例如：
      "my-gateway": { "apiKey": "sk-xxx", "apiBase": "https://..." }
    """

    model_config = ConfigDict(extra="allow")

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # 任意 OpenAI 兼容端点
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)  # Anthropic (Claude)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI (GPT) 或兼容 API
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)  # DeepSeek (V3/R1)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里通义 (Qwen)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)  # 智谱 (GLM)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)  # 月之暗面 (Kimi)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)  # 阶跃星辰
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama 本地模型

    @model_validator(mode="after")
    def convert_extra_providers(self):
        """将额外字段（自定义提供商）转换为 ProviderConfig 对象。"""
        if self.model_extra:
            from biscuitbot.providers.registry import find_by_name

            for key, value in self.model_extra.items():
                # 与内置提供商名冲突则报错
                if spec := find_by_name(key):
                    raise ValueError(
                        f"providers.{key} conflicts with built-in provider {spec.name!r}; "
                        "use the built-in provider key or choose a different custom provider name"
                    )
                if isinstance(value, dict):
                    self.model_extra[key] = ProviderConfig.model_validate(value)
        return self

    @model_validator(mode="after")
    def _validate_api_type_scope(self) -> "ProvidersConfig":
        """校验 api_type 仅对 providers.openai 生效，其余提供商不允许显式指定。"""
        for name in self.__class__.model_fields:
            if name == "openai":
                continue
            provider = getattr(self, name, None)
            if isinstance(provider, ProviderConfig) and provider.api_type != "auto":
                raise ValueError("providers.<name>.api_type is only supported for providers.openai")
        for provider in (self.model_extra or {}).values():
            if isinstance(provider, ProviderConfig) and provider.api_type != "auto":
                raise ValueError("providers.<name>.api_type is only supported for providers.openai")
        return self


class HeartbeatConfig(Base):
    """心跳服务配置（现由 cron 支持）。"""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 分钟
    keep_recent_messages: int = 8


class ApiConfig(Base):
    """OpenAI 兼容 API 服务器配置。"""

    host: str = "127.0.0.1"  # 更安全的默认值：仅本地绑定
    port: int = 8900
    timeout: float = 120.0  # 单请求超时（秒）


class GatewayConfig(Base):
    """网关/服务器配置。"""

    host: str = "127.0.0.1"  # 更安全的默认值：仅本地绑定
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    # 人才市场注册表 URL：后台写死在配置文件里，只能通过 CLI 修改
    # （`biscuitbot talent-market set <url>`），WebUI/桌面应用不允许更改。
    talent_market_registry_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "talentMarketRegistryUrl", "talent_market_registry_url"
        ),
    )


class MCPServerConfig(Base):
    """MCP 服务器连接配置（stdio 或 HTTP）。"""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # 省略时自动检测
    command: str = ""  # Stdio：要运行的命令（如 "npx"）
    args: list[str] = Field(default_factory=list)  # Stdio：命令参数
    env: dict[str, str] = Field(default_factory=dict)  # Stdio：额外环境变量
    cwd: str = ""  # Stdio：MCP 服务器运行时工件的工作目录
    url: str = ""  # HTTP/SSE：端点 URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE：自定义请求头
    tool_timeout: int = 30  # 工具调用超时取消秒数
    enabled_tools: list[str] = Field(default_factory=lambda: ["*"])  # 仅注册这些工具；接受原始 MCP 名或包装后的 mcp_<server>_<tool> 名；["*"] = 全部工具；[] = 无工具


def _lazy_default(module_path: str, class_name: str) -> Any:
    """ToolsConfig 默认工厂的延迟导入辅助函数。"""
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


class ToolsConfig(Base):
    """工具配置。

    工具专属子配置的字段类型通过本文件底部的 model_rebuild() 解析，使工具配置类
    可以保留在各自工具实现旁边，避免循环导入。
    """

    web: WebToolsConfig = Field(default_factory=lambda: _lazy_default("biscuitbot.agent.tools.web", "WebToolsConfig"))
    exec: ExecToolConfig = Field(default_factory=lambda: _lazy_default("biscuitbot.agent.tools.shell", "ExecToolConfig"))
    file: FileToolsConfig = Field(default_factory=lambda: _lazy_default("biscuitbot.agent.tools.filesystem", "FileToolsConfig"))
    cli_apps: CliAppsToolConfig = Field(default_factory=lambda: _lazy_default("biscuitbot.agent.tools.cli_apps", "CliAppsToolConfig"))
    my: MyToolConfig = Field(default_factory=lambda: _lazy_default("biscuitbot.agent.tools.self", "MyToolConfig"))
    image_generation: ImageGenerationToolConfig = Field(
        default_factory=lambda: _lazy_default("biscuitbot.agent.tools.image_generation", "ImageGenerationToolConfig"),
    )
    screenshot: ScreenshotToolConfig = Field(
        default_factory=lambda: _lazy_default("biscuitbot.agent.tools.screenshot", "ScreenshotToolConfig"),
    )
    seedance_video: SeedanceVideoToolConfig = Field(
        default_factory=lambda: _lazy_default("biscuitbot.agent.tools.seedance_video", "SeedanceVideoToolConfig"),
    )
    system_io: SystemIoToolConfig = Field(
        default_factory=lambda: _lazy_default("biscuitbot.agent.tools.system_io", "SystemIoToolConfig"),
    )
    restrict_to_workspace: bool = False  # 策略意图：尽可能将工具访问限制在工作区内
    guard_level: str = Field(
        default="standard",
        validation_alias=AliasChoices("guardLevel", "guard_level"),
        description="Prompt-injection / shell-interception guard level: standard|minimal|off. "
        "standard=full deny-list + banners; minimal=catastrophic shell blocks only; off=no injection/shell interception.",
    )  # 可配置的防护级别；结构性约束（SSRF、工作区）在所有级别下始终开启
    webui_allow_local_service_access: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "webuiAllowLocalServiceAccess",
            "webui_allow_local_service_access",
            "allowLocalPreviewAccess",
            "allow_local_preview_access",
        ),
    )  # 允许 WebUI Full Access 的 shell 检查访问本地服务；遗留 allowLocalPreviewAccess 仍可读取
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    ssrf_whitelist: list[str] = Field(default_factory=list)  # 豁免 SSRF 拦截的 CIDR 范围（如 ["100.64.0.0/10"] 用于 Tailscale）
    cold_storage_days: int = Field(
        default=14,
        validation_alias=AliasChoices("coldStorageDays", "cold_storage_days"),
        description="工具/技能多少天未被调用即转入冷门仓库（不发送 schema）。0=禁用冷门轮转。",
    )
    duplicate_similarity_threshold: float = Field(
        default=0.6,
        validation_alias=AliasChoices("duplicateSimilarityThreshold", "duplicate_similarity_threshold"),
        description="重复检测的 token 重叠率阈值（0.0-1.0）。夜间维护任务据此报告潜在重复。",
    )

    @field_validator("guard_level", mode="before")
    @classmethod
    def _normalize_guard_level(cls, value: Any) -> Any:
        """小写化并校验 guard_level；未知值回退为 'standard'。"""
        from biscuitbot.security.guard_level import normalize_guard_level

        if isinstance(value, str):
            return normalize_guard_level(value)
        return value


class Config(BaseSettings):
    """biscuitbot 根配置。"""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("modelPresets", "model_presets"),
    )

    def __init__(self, **values: Any) -> None:
        # 若模型尚未完成（工具配置前置引用未解析），先触发解析
        if not type(self).__pydantic_complete__:
            _resolve_tool_config_refs()
        super().__init__(**values)

    @model_validator(mode="after")
    def _validate_model_preset(self) -> "Config":
        """校验模型预设名：'default' 保留给 agents.defaults，引用的预设必须存在。"""
        if "default" in self.model_presets:
            raise ValueError("model_preset name 'default' is reserved for agents.defaults")
        name = self.agents.defaults.model_preset
        if name and name != "default" and name not in self.model_presets:
            raise ValueError(f"model_preset {name!r} not found in model_presets")
        # 回退模型若为字符串，必须是已存在的预设名
        for fallback in self.agents.defaults.fallback_models:
            if isinstance(fallback, str) and fallback not in self.model_presets:
                raise ValueError(f"fallback_models entry {fallback!r} not found in model_presets")
        return self

    def resolve_default_preset(self) -> ModelPresetConfig:
        """从 agents.defaults 字段返回隐式 `default` 预设。"""
        d = self.agents.defaults
        return ModelPresetConfig(
            model=d.model, provider=d.provider, max_tokens=d.max_tokens,
            context_window_tokens=d.context_window_tokens,
            temperature=d.temperature, reasoning_effort=d.reasoning_effort,
        )

    def resolve_preset(self, name: str | None = None) -> ModelPresetConfig:
        """返回命名预设或隐式默认预设对应的有效模型参数。"""
        name = self.agents.defaults.model_preset if name is None else name
        if not name or name == "default":
            return self.resolve_default_preset()
        if name not in self.model_presets:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return self.model_presets[name]

    @property
    def workspace_path(self) -> Path:
        """获取展开后的工作区路径。"""
        return Path(self.agents.defaults.workspace).expanduser()

    def _match_provider(
        self, model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> tuple["ProviderConfig | None", str | None]:
        """匹配提供商配置及其注册表名。返回 (config, spec_name)。"""
        from biscuitbot.providers.registry import (
            PROVIDERS,
            find_by_name,
        )

        resolved = preset or self.resolve_preset()
        forced = resolved.provider

        def _custom_provider_by_name(name: str) -> tuple[ProviderConfig, str] | None:
            """按名称在自定义提供商中查找，归一化连字符与大小写。"""
            normalized = name.replace("-", "_").lower()
            for attr_name, provider in (self.providers.model_extra or {}).items():
                if not isinstance(provider, ProviderConfig):
                    continue
                if attr_name.replace("-", "_").lower() == normalized:
                    return provider, attr_name
            return None

        # 显式指定的提供商（非 auto）优先匹配
        if forced != "auto":
            spec = find_by_name(forced)
            if spec:
                p = getattr(self.providers, spec.name, None)
                return (p, spec.name) if p else (None, None)
            custom = _custom_provider_by_name(forced)
            if custom is not None:
                return custom
            return None, None

        # 模型名预处理：小写化、归一化连字符、提取前缀
        model_lower = (model or resolved.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            """判断关键词是否匹配模型名。"""
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # 显式提供商前缀优先——防止 `github-copilot/...codex` 误匹配 openai_codex
        for spec in PROVIDERS:
            if spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # 按前缀查找自定义提供商（如 "companyProxy/gpt-4"）。
        # 即使 apiBase 缺失也返回匹配的提供商，使格式错误的显式前缀直接失败，
        # 而不是回落到其他自定义提供商。
        if model_prefix:
            custom = _custom_provider_by_name(normalized_prefix)
            if custom is not None:
                return custom

        # 按关键词匹配（顺序遵循 PROVIDERS 注册表）
        for spec in PROVIDERS:
            if spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or spec.is_direct or p.api_key:
                    return p, spec.name

        # 回退：已配置的本地提供商可路由无提供商关键词的模型
        # （例如 Ollama 上的纯 "llama3.2"）。
        # 优先选择 detect_by_base_keyword 匹配已配置 api_base 的提供商
        # （如 Ollama 的 "11434" 出现在 "http://localhost:11434" 中），而非单纯注册表顺序。
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # 回退：先网关后其他（遵循注册表顺序）
        # OAuth 提供商不是有效回退——它们要求显式模型选择
        for spec in PROVIDERS:
            if spec.is_oauth or spec.is_transcription_only:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name

        # 最终回退：检查任意已配置的自定义提供商
        for attr_name, p in (self.providers.model_extra or {}).items():
            if isinstance(p, ProviderConfig) and p.api_base:
                return p, attr_name

        return None, None

    def get_provider(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> ProviderConfig | None:
        """获取匹配的提供商配置（api_key、api_base、extra_headers），回退到首个可用。"""
        p, _ = self._match_provider(model, preset=preset)
        return p

    def get_provider_name(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """获取匹配提供商的注册表名（如 "deepseek"、"openrouter"）。"""
        _, name = self._match_provider(model, preset=preset)
        return name

    def get_api_key(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """获取给定模型的 API Key，回退到首个可用 Key。"""
        p = self.get_provider(model, preset=preset)
        return p.api_key if p else None

    def get_api_base(
        self,
        model: str | None = None,
        *,
        preset: ModelPresetConfig | None = None,
    ) -> str | None:
        """获取给定模型的 API base URL，缺失时回退到提供商默认值。"""
        from biscuitbot.providers.registry import find_by_name

        p, name = self._match_provider(model, preset=preset)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="BISCUITBOT_", env_nested_delimiter="__")


def _resolve_tool_config_refs() -> None:
    """通过导入工具配置类来解析 ToolsConfig 中的前置引用。

    必须在所有模块加载完成后调用（打破循环导入）。
    将这些类重新导出到本模块命名空间，使既有导入
    ``from biscuitbot.config.schema import ExecToolConfig`` 继续可用。
    """
    import sys

    from biscuitbot.agent.tools.cli_apps import CliAppsToolConfig
    from biscuitbot.agent.tools.filesystem import FileToolsConfig
    from biscuitbot.agent.tools.image_generation import ImageGenerationToolConfig
    from biscuitbot.agent.tools.screenshot import ScreenshotToolConfig
    from biscuitbot.agent.tools.seedance_video import SeedanceVideoToolConfig
    from biscuitbot.agent.tools.self import MyToolConfig
    from biscuitbot.agent.tools.shell import ExecToolConfig
    from biscuitbot.agent.tools.system_io import SystemIoToolConfig
    from biscuitbot.agent.tools.web import WebFetchConfig, WebSearchConfig, WebToolsConfig

    # 将工具配置类重新导出到本模块命名空间
    mod = sys.modules[__name__]
    mod.ExecToolConfig = ExecToolConfig  # type: ignore[attr-defined]
    mod.FileToolsConfig = FileToolsConfig  # type: ignore[attr-defined]
    mod.CliAppsToolConfig = CliAppsToolConfig  # type: ignore[attr-defined]
    mod.WebToolsConfig = WebToolsConfig  # type: ignore[attr-defined]
    mod.WebSearchConfig = WebSearchConfig  # type: ignore[attr-defined]
    mod.WebFetchConfig = WebFetchConfig  # type: ignore[attr-defined]
    mod.MyToolConfig = MyToolConfig  # type: ignore[attr-defined]
    mod.ImageGenerationToolConfig = ImageGenerationToolConfig  # type: ignore[attr-defined]
    mod.ScreenshotToolConfig = ScreenshotToolConfig  # type: ignore[attr-defined]
    mod.SeedanceVideoToolConfig = SeedanceVideoToolConfig  # type: ignore[attr-defined]
    mod.SystemIoToolConfig = SystemIoToolConfig  # type: ignore[attr-defined]

    ToolsConfig.model_rebuild()
    Config.model_rebuild()


# 当导入链允许时（此时无循环依赖）尽早解析。若失败（首次导入触发循环），
# 则在运行时首次使用 Config/ToolsConfig 时惰性 rebuild。
try:
    _resolve_tool_config_refs()
except ImportError:
    pass
