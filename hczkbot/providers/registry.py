"""
Provider Registry — single source of truth for LLM provider metadata.

Adding a new provider:
  1. Add a ProviderSpec to PROVIDERS below.
  2. Add a field to ProvidersConfig in config/schema.py.
  Done. Env vars, config matching, status display all derive from here.

Order matters — it controls match priority and fallback. Gateways first.
Every entry writes out all fields so you can copy-paste as a template.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic.alias_generators import to_snake


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's metadata. See PROVIDERS below for real examples.

    Placeholders in env_extras values:
      {api_key}  — the user's API key
      {api_base} — api_base from config, or this spec's default_api_base
    """

    # identity
    name: str  # config field name, e.g. "dashscope"
    keywords: tuple[str, ...]  # model-name keywords for matching (lowercase)
    env_key: str  # env var for API key, e.g. "DASHSCOPE_API_KEY"
    display_name: str = ""  # shown in `hczkbot status`

    # which provider implementation to use
    # "openai_compat" | "anthropic"
    backend: str = "openai_compat"

    # extra env vars, e.g. (("ZHIPUAI_API_KEY", "{api_key}"),)
    env_extras: tuple[tuple[str, str], ...] = ()

    # gateway / local detection
    is_gateway: bool = False  # routes any model (OpenRouter, AiHubMix)
    is_local: bool = False  # local deployment (vLLM, Ollama)
    detect_by_key_prefix: str = ""  # match api_key prefix, e.g. "sk-or-"
    detect_by_base_keyword: str = ""  # match substring in api_base URL
    default_api_base: str = ""  # OpenAI-compatible base URL for this provider

    # gateway behavior
    strip_model_prefix: bool = False  # strip "provider/" before sending to gateway
    strip_model_prefixes: tuple[str, ...] = ()  # strip only when the first model segment matches
    supports_max_completion_tokens: bool = False

    # per-model param overrides, e.g. (("kimi-k2.5", {"temperature": 1.0}),)
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()

    # OAuth-based providers (e.g., OpenAI Codex) don't use API keys
    is_oauth: bool = False

    # Direct providers skip API-key validation (user supplies everything)
    is_direct: bool = False

    # Provider is listed for shared credentials but cannot serve chat completions.
    is_transcription_only: bool = False

    # Provider supports cache_control on content blocks (e.g. Anthropic prompt caching)
    supports_prompt_caching: bool = False

    # How to inject the thinking on/off toggle into extra_body.
    # ""              — no extra_body needed (default)
    # "thinking_type" — {"thinking": {"type": "enabled"/"disabled"}}
    #                   (DeepSeek, VolcEngine, BytePlus)
    # "enable_thinking" — {"enable_thinking": true/false}  (DashScope)
    # "reasoning_split" — {"reasoning_split": true/false}  (MiniMax)
    thinking_style: str = ""

    # Gateway-native reasoning control to pair with model-level thinking styles.
    # "reasoning_effort" — {"reasoning": {"effort": <none|minimal|...>}}
    #                      (OpenRouter)
    gateway_reasoning_style: str = ""

    # When True, treat the "reasoning" response field as formal content
    # when "content" is empty.  Only set this for providers (e.g. StepFun)
    # whose API returns the actual answer in "reasoning" instead of "content".
    reasoning_as_content: bool = False

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS — the registry. Order = priority. Copy any entry as template.
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
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_by_name(name: str) -> ProviderSpec | None:
    """Find a provider spec by config field name, e.g. "dashscope"."""
    normalized = to_snake(name.replace("-", "_"))
    for spec in PROVIDERS:
        if spec.name == normalized:
            return spec
    return None


def create_dynamic_spec(name: str) -> ProviderSpec:
    """Create a dynamic ProviderSpec for custom user-defined providers."""
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
