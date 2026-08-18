"""
Provider 注册表 —— LLM Provider 元数据的唯一事实来源（single source of truth）。

新增一个 Provider 只需两步：
  1. 在下方 PROVIDERS 中新增一个 ProviderSpec；
  2. 在 config/schema.py 的 ProvidersConfig 中新增对应字段。
环境变量、配置匹配、状态展示等逻辑均从此处派生。

注册顺序很重要——它决定了模型匹配优先级与兜底顺序，网关类 Provider 排在前面。
每个条目都完整列出所有字段，便于直接复制作为模板。
"""

from __future__ import annotations

from dataclasses import dataclass  # 不可变数据类（ProviderSpec）
from typing import Any  # 动态类型标注

from pydantic.alias_generators import to_snake  # 把名称转换为 snake_case


@dataclass(frozen=True)
class ProviderSpec:
    """单个 LLM Provider 的元数据。具体示例见下方 PROVIDERS。

    env_extras 值中可使用的占位符：
      {api_key}  — 用户的 API Key
      {api_base} — 来自配置的 api_base，或本 spec 的 default_api_base
    """

    # --- 标识信息 ---
    name: str  # 配置字段名，例如 "dashscope"
    keywords: tuple[str, ...]  # 用于匹配模型名的关键字（小写）
    env_key: str  # API Key 对应的环境变量名，例如 "DASHSCOPE_API_KEY"
    display_name: str = ""  # 在 `biscuitbot status` 中展示的名称

    # --- 使用哪种 Provider 实现 ---
    # "openai_compat"（OpenAI 兼容） | "anthropic"（原生 Anthropic SDK）
    backend: str = "openai_compat"

    # 额外环境变量，例如 (("ZHIPUAI_API_KEY", "{api_key}"),)
    env_extras: tuple[tuple[str, str], ...] = ()

    # --- 网关 / 本地部署识别 ---
    is_gateway: bool = False  # 是否为网关型 Provider（可路由任意模型，如 OpenRouter、AiHubMix）
    is_local: bool = False  # 是否为本地部署（如 vLLM、Ollama）
    detect_by_key_prefix: str = ""  # 通过 api_key 前缀识别，例如 "sk-or-"
    detect_by_base_keyword: str = ""  # 通过 api_base URL 子串识别
    default_api_base: str = ""  # 该 Provider 的 OpenAI 兼容 base URL

    # --- 网关行为 ---
    strip_model_prefix: bool = False  # 是否在发送给网关前去掉 "provider/" 前缀
    strip_model_prefixes: tuple[str, ...] = ()  # 仅当模型首段匹配这些前缀时才剥离
    supports_max_completion_tokens: bool = False

    # --- 按模型覆盖参数，例如 (("kimi-k2.5", {"temperature": 1.0}),) ---
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()

    # --- OAuth 型 Provider（如 OpenAI Codex）不使用 API Key ---
    is_oauth: bool = False

    # --- 直连 Provider：跳过 API Key 校验（用户自行提供全部参数）---
    is_direct: bool = False

    # --- 仅用于共享凭据但不能提供对话补全的 Provider（如只做转写）---
    is_transcription_only: bool = False

    # --- 是否支持 content 块上的 cache_control（如 Anthropic 提示缓存）---
    supports_prompt_caching: bool = False

    # --- 如何把思考开关注入 extra_body ---
    # ""              — 无需 extra_body（默认）
    # "thinking_type" — {"thinking": {"type": "enabled"/"disabled"}}
    #                   （DeepSeek、火山引擎、BytePlus）
    # "enable_thinking" — {"enable_thinking": true/false}  （DashScope）
    # "reasoning_split" — {"reasoning_split": true/false}  （MiniMax）
    thinking_style: str = ""

    # --- 网关原生的推理控制，与模型级 thinking_style 配合使用 ---
    # "reasoning_effort" — {"reasoning": {"effort": <none|minimal|...>}}
    #                      （OpenRouter）
    gateway_reasoning_style: str = ""

    # --- 为真时，当 "content" 为空，把响应中的 "reasoning" 字段当作正文 ---
    # 仅对部分 Provider（如 StepFun）设置：其 API 把真正答案放在 "reasoning" 而非 "content"。
    reasoning_as_content: bool = False

    @property
    def label(self) -> str:
        """展示标签：优先用 display_name，否则用 name 首字母大写形式。"""
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS —— 注册表本体。顺序即优先级。可直接复制任一条目作为模板。
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === Custom (直接 OpenAI 兼容端点) ====================================
    ProviderSpec(
        name="custom",
        keywords=(),
        env_key="",
        display_name="Custom",
        backend="openai_compat",
        is_direct=True,
    ),

    # === 三大核心提供商 ==================================================
    # Anthropic: 原生 Anthropic SDK（Claude 系列）
    ProviderSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        backend="anthropic",
        supports_prompt_caching=True,
    ),
    # OpenAI: OpenAI 官方 API 或兼容端点（GPT、o 系列等）
    # 通过 apiBase 可指向代理地址，兼容所有 OpenAI 格式的模型服务
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        backend="openai_compat",
        supports_max_completion_tokens=True,
        strip_model_prefixes=("openai",),
    ),
    # DeepSeek: OpenAI 兼容 API（DeepSeek-V3/R1 等）
    # 通过 apiBase 可指向代理地址
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        backend="openai_compat",
        default_api_base="https://api.deepseek.com",
        thinking_style="thinking_type",
        strip_model_prefixes=("deepseek",),
    ),

    # === 国内提供商 =======================================================
    # DashScope (阿里通义): Qwen 系列，OpenAI 兼容
    # 同时提供图片生成（万相 wanx）和语音识别（Paraformer）能力
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope", "tongyi"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope (阿里通义)",
        backend="openai_compat",
        default_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        thinking_style="enable_thinking",
        strip_model_prefixes=("dashscope", "qwen"),
    ),
    # Zhipu (智谱): GLM 系列，OpenAI 兼容
    # 提供图片生成（CogView/GLM-Image）能力
    ProviderSpec(
        name="zhipu",
        keywords=("zhipu", "glm", "zai"),
        env_key="ZHIPU_API_KEY",
        display_name="Zhipu AI (智谱)",
        backend="openai_compat",
        default_api_base="https://open.bigmodel.cn/api/paas/v4",
        strip_model_prefixes=("zhipu",),
    ),
    # Moonshot (月之暗面): Kimi 系列，OpenAI 兼容
    ProviderSpec(
        name="moonshot",
        keywords=("moonshot", "kimi"),
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot (月之暗面/Kimi)",
        backend="openai_compat",
        default_api_base="https://api.moonshot.ai/v1",
        strip_model_prefixes=("moonshot",),
    ),
    # Step Fun (阶跃星辰): Step 系列，OpenAI 兼容
    # 同时提供图片生成和语音识别（ASR）能力
    ProviderSpec(
        name="stepfun",
        keywords=("stepfun", "step"),
        env_key="STEPFUN_API_KEY",
        display_name="Step Fun (阶跃星辰)",
        backend="openai_compat",
        default_api_base="https://api.stepfun.com/v1",
        reasoning_as_content=True,
        strip_model_prefixes=("stepfun",),
    ),

    # === 本地部署 ========================================================
    # Ollama: 本地模型服务，OpenAI 兼容
    ProviderSpec(
        name="ollama",
        keywords=("ollama",),
        env_key="OLLAMA_API_KEY",
        display_name="Ollama",
        backend="openai_compat",
        is_local=True,
        detect_by_base_keyword="11434",
        default_api_base="http://localhost:11434/v1",
    ),

    # === 中转站 / 网关 ====================================================
    # New API：开源 OpenAI 兼容中转站（new-api / one-api 一系）。
    # 可路由任意 OpenAI 兼容模型，并提供 /images/generations、/audio/speech、
    # /audio/transcriptions 等能力。域名由用户在 providers.newapi.apiBase 指定。
    ProviderSpec(
        name="newapi",
        keywords=(),
        env_key="NEWAPI_API_KEY",
        display_name="New API 中转",
        backend="openai_compat",
        is_gateway=True,
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers —— 查找辅助函数
# ---------------------------------------------------------------------------


def find_by_name(name: str) -> ProviderSpec | None:
    """按配置字段名查找 ProviderSpec，例如 "dashscope"。未找到返回 None。"""
    normalized = to_snake(name.replace("-", "_"))
    for spec in PROVIDERS:
        if spec.name == normalized:
            return spec
    return None


def create_dynamic_spec(name: str) -> ProviderSpec:
    """为用户自定义的 Provider 创建动态 ProviderSpec。

    用于在运行时按用户输入的名称生成一个直连型（is_direct=True）的 OpenAI 兼容 spec，
    同时自动生成需要剥离的模型前缀（保留原始名与归一化名，去重保序）。
    """
    normalized = to_snake(name.replace("-", "_"))
    strip_prefixes = tuple(dict.fromkeys((name, normalized)))
    return ProviderSpec(
        name=normalized,
        keywords=(),
        env_key="",
        display_name=name.title(),
        backend="openai_compat",
        is_direct=True,
        strip_model_prefixes=strip_prefixes,
    )
