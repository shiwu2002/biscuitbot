"""LLM Provider 工厂 —— 根据配置创建 Provider 实例。

所属模块与项目作用
===================
本文件位于 biscuitbot/providers 目录，是 LLM Provider 层的工厂组件。
在项目架构中起到的作用：
- 依据 :class:`Config` 中的模型预设（preset）与 Provider 注册表，
  实例化具体的 :class:`LLMProvider` 后端（Anthropic / OpenAI 兼容等）。
- 负责生成参数注入、API Key / api_base 解析、Provider spec 查找与
  动态 spec 创建、failover（FallbackProvider）包装，以及用于配置变更
  检测的 provider signature 计算。
- 提供 build_provider_snapshot / load_provider_snapshot 等便捷入口，
  供上层一次性获得 Provider 实例、模型名、上下文窗口与签名。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# 配置层的数据类型：主配置、内联 fallback 配置、模型预设
from biscuitbot.config.schema import Config, InlineFallbackConfig, ModelPresetConfig
from biscuitbot.providers.base import LLMProvider  # Provider 抽象基类
from biscuitbot.providers.fallback_provider import FallbackProvider  # 失败转移包装器
# Provider 注册表：按名查找 spec / 创建动态 spec
from biscuitbot.providers.registry import create_dynamic_spec, find_by_name


@dataclass(frozen=True)
class ProviderSnapshot:
    """Provider 快照：聚合实例、模型、上下文窗口与配置签名。

    用于一次性返回构建 Provider 所需的全部信息，并通过对签名比较判断
    配置是否发生变化（决定是否需要重建 Provider）。
    """
    provider: LLMProvider
    model: str
    context_window_tokens: int
    signature: tuple[object, ...]


def _resolve_model_preset(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ModelPresetConfig:
    """解析模型预设：优先使用显式传入的 preset，否则按名从配置解析。"""
    return preset if preset is not None else config.resolve_preset(preset_name)


def _make_provider_core(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """创建一个不含失败转移包装的纯 LLM Provider。

    流程：解析预设 → 查找 provider_name → 查找 spec（未注册但有 api_base
    时创建动态 spec）→ 校验 transcription-only / API Key → 按 backend
    实例化 AnthropicProvider 或 OpenAICompatProvider → 注入生成参数。
    """
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    model = model or resolved.model
    provider_name = config.get_provider_name(model, preset=resolved)
    p = config.get_provider(model, preset=resolved)
    # 查找已注册的 Provider spec
    spec = find_by_name(provider_name) if provider_name else None
    if provider_name and not spec and p:
        # 未注册但配置了 api_base：按动态 spec 处理（兼容自定义网关）
        if not p.api_base:
            raise ValueError(f"Provider '{provider_name}' requires api_base in config.")
        spec = create_dynamic_spec(provider_name)
    if spec and spec.is_transcription_only:
        # 仅支持转写的 Provider 不能用于聊天
        raise ValueError(f"Provider '{provider_name}' only supports transcription.")
    backend = spec.backend if spec else "openai_compat"

    if backend == "openai_compat" and not model.startswith("bedrock/"):
        # OpenAI 兼容后端校验 API Key（OAuth/本地/直连 Provider 可豁免）
        needs_key = not (p and p.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            raise ValueError(f"No API key configured for provider '{provider_name}'.")

    if backend == "anthropic":
        # 延迟导入，避免在模块加载阶段强依赖 anthropic SDK
        from biscuitbot.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model, preset=resolved),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        # 默认走 OpenAI 兼容后端
        from biscuitbot.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model, preset=resolved),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
            extra_body=p.extra_body if p else None,
            # 仅 openai 官方显式传 api_type，其余走 auto 自动推断
            api_type=p.api_type if p and provider_name == "openai" else "auto",
            extra_query=p.extra_query if p else None,
        )

    # 注入生成参数（temperature/max_tokens/reasoning_effort）
    provider.generation = resolved.to_generation_settings()
    return provider


def _inline_fallback_preset(
    primary: ModelPresetConfig,
    fallback: InlineFallbackConfig,
) -> ModelPresetConfig:
    """将内联 fallback 配置补全为完整的 :class:`ModelPresetConfig`。

    未显式指定的字段（max_tokens / context_window_tokens / temperature）
    会继承主预设的值，保证 fallback 行为一致。
    """
    return ModelPresetConfig(
        model=fallback.model,
        provider=fallback.provider,
        max_tokens=fallback.max_tokens if fallback.max_tokens is not None else primary.max_tokens,
        context_window_tokens=(
            fallback.context_window_tokens
            if fallback.context_window_tokens is not None
            else primary.context_window_tokens
        ),
        temperature=(
            fallback.temperature if fallback.temperature is not None else primary.temperature
        ),
        reasoning_effort=fallback.reasoning_effort,
    )


def _resolve_fallback_presets(config: Config, primary: ModelPresetConfig) -> list[ModelPresetConfig]:
    """解析配置中的 fallback 模型列表为预设列表。

    元素可以是预设名（字符串，按名查找）或内联 fallback 配置（补全为完整预设）。
    """
    presets: list[ModelPresetConfig] = []
    for fallback in config.agents.defaults.fallback_models:
        if isinstance(fallback, str):
            # 字符串：视为预设名
            presets.append(config.model_presets[fallback])
        else:
            # 内联配置：补全后加入
            presets.append(_inline_fallback_preset(primary, fallback))
    return presets


def make_provider(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
    model: str | None = None,
) -> LLMProvider:
    """根据配置创建对应的 LLM Provider。

    当传入 *model* 时，会覆盖解析/预设中的模型名 —— 这条路径用于
    failover 时为各 fallback 模型创建 Provider。
    """
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    # 创建主 Provider（不含 fallback 包装）
    provider = _make_provider_core(config, preset_name=preset_name, preset=preset, model=model)
    fallback_presets = _resolve_fallback_presets(config, resolved)

    if fallback_presets:
        # 存在 fallback：用 FallbackProvider 包装，工厂按需创建各 fallback 的 Provider
        provider = FallbackProvider(
            primary=provider,
            fallback_presets=fallback_presets,
            provider_factory=lambda fb: _make_provider_core(
                config, preset_name=preset_name, preset=fb
            ),
        )

    return provider


def provider_signature(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> tuple[object, ...]:
    """返回影响当前 Provider 链的配置字段组成的签名。

    用于检测配置是否变化（如热重载时判断是否需要重建 Provider），
    包含主模型与各 fallback 的全部关键字段。
    """
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    p = config.get_provider(resolved.model, preset=resolved)
    fallback_presets = _resolve_fallback_presets(config, resolved)

    def _fallback_signature(fallback: ModelPresetConfig) -> tuple[object, ...]:
        """计算单个 fallback 预设的签名片段。"""
        fp = config.get_provider(fallback.model, preset=fallback)
        return (
            fallback.model,
            fallback.provider,
            config.get_provider_name(fallback.model, preset=fallback),
            config.get_api_key(fallback.model, preset=fallback),
            config.get_api_base(fallback.model, preset=fallback),
            fp.extra_headers if fp else None,
            fp.extra_body if fp else None,
            fp.api_type if fp else "auto",
            fp.extra_query if fp else None,
            fallback.max_tokens,
            fallback.temperature,
            fallback.reasoning_effort,
            fallback.context_window_tokens,
        )

    # 主模型签名 + 各 fallback 签名组成的元组
    return (
        resolved.model,
        resolved.provider,
        config.get_provider_name(resolved.model, preset=resolved),
        config.get_api_key(resolved.model, preset=resolved),
        config.get_api_base(resolved.model, preset=resolved),
        p.extra_headers if p else None,
        p.extra_body if p else None,
        p.api_type if p else "auto",
        p.extra_query if p else None,
        resolved.max_tokens,
        resolved.temperature,
        resolved.reasoning_effort,
        resolved.context_window_tokens,
        tuple(_fallback_signature(fallback) for fallback in fallback_presets),
    )


def build_provider_snapshot(
    config: Config,
    *,
    preset_name: str | None = None,
    preset: ModelPresetConfig | None = None,
) -> ProviderSnapshot:
    """构建 Provider 快照：实例 + 模型 + 上下文窗口 + 签名。

    上下文窗口取主模型与各 fallback 中的最小值，保证 token 预算在
    任意 failover 路径下都安全。
    """
    resolved = _resolve_model_preset(config, preset_name=preset_name, preset=preset)
    # 收集所有 fallback 的上下文窗口，取最小值作为安全预算
    fallback_windows = [
        fallback.context_window_tokens
        for fallback in _resolve_fallback_presets(config, resolved)
    ]
    return ProviderSnapshot(
        provider=make_provider(config, preset=resolved),
        model=resolved.model,
        context_window_tokens=min([resolved.context_window_tokens, *fallback_windows]),
        signature=provider_signature(config, preset=resolved),
    )


def build_vision_provider(
    config: Config,
    *,
    vision_model: str | None = None,
) -> LLMProvider | None:
    """为截图理解创建视觉 LLM Provider。

    ``vision_model`` 可以是：
    - **模型预设名**（如 ``"default"``）：使用该预设的 Provider 凭据与模型。
    - **Provider 名**（如 ``"openai"``、``"anthropic"``）：直接使用该
      Provider 的凭据，此时需通过 ``vision_model_override`` 指定实际的
      多模态模型 ID。

    当 *vision_model* 未配置，或传入了 Provider 名但该 Provider 未配置时
    返回 ``None``。
    """
    name = vision_model or config.agents.defaults.vision_model
    if not name:
        return None

    override = (config.agents.defaults.vision_model_override or "").strip()
    # 先按预设名查找
    preset = config.model_presets.get(name)
    if preset is not None:
        if override:
            # 预设 + override：用预设凭据但替换模型
            return _make_provider_core(config, preset=preset, model=override)
        return _make_provider_core(config, preset=preset)

    # 非预设 —— 视为 Provider 名（如 "openai"、"anthropic"）
    from biscuitbot.config.schema import ModelPresetConfig

    provider_config = getattr(config.providers, name, None)
    if provider_config is None:
        # Provider 未配置：返回 None
        return None
    # 临时构造一个预设，复用 _make_provider_core
    temp_preset = ModelPresetConfig(
        model=override or "vision",
        provider=name,
    )
    return _make_provider_core(config, preset=temp_preset, model=override or None)


def load_provider_snapshot(
    config_path: Path | None = None,
    *,
    preset_name: str | None = None,
) -> ProviderSnapshot:
    """从配置文件加载并构建 Provider 快照的便捷入口。

    内部完成配置加载与环境变量解析，再委托 :func:`build_provider_snapshot`。
    """
    from biscuitbot.config.loader import load_config, resolve_config_env_vars

    return build_provider_snapshot(
        resolve_config_env_vars(load_config(config_path)),
        preset_name=preset_name,
    )
