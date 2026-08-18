"""Registry for text-to-speech providers.

Provider-specific adapters live in ``biscuitbot.providers.tts``.
This module is the app-level source of truth for provider names, aliases,
default models, default voices, and adapter class paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol


class TtsProviderAdapter(Protocol):
    """Runtime protocol implemented by provider-specific TTS adapters."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        rate: str | None = None,
    ) -> None: ...

    async def synthesize(self, text: str, output_path: str | Path) -> str: ...


@dataclass(frozen=True)
class TtsProviderSpec:
    name: str
    default_model: str
    default_voice: str
    adapter: str
    aliases: tuple[str, ...] = ()
    default_api_base: str | None = None
    requires_api_key: bool = True

    def load_adapter(self) -> type[TtsProviderAdapter]:
        module_name, _, class_name = self.adapter.partition(":")
        if not module_name or not class_name:
            raise RuntimeError(f"Invalid TTS adapter path: {self.adapter}")
        adapter = getattr(import_module(module_name), class_name)
        return adapter


TTS_PROVIDERS: tuple[TtsProviderSpec, ...] = (
    TtsProviderSpec(
        name="openai",
        default_model="gpt-4o-mini-tts",
        default_voice="alloy",
        adapter="biscuitbot.providers.tts:OpenAITtsProvider",
        default_api_base="https://api.openai.com/v1",
    ),
    TtsProviderSpec(
        name="dashscope",
        default_model="cosyvoice-v2",
        default_voice="longxiaochun",
        adapter="biscuitbot.providers.tts:OpenAITtsProvider",
        aliases=("aliyun", "tongyi"),
        default_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    TtsProviderSpec(
        name="edge-tts",
        default_model="",
        default_voice="zh-CN-XiaoxiaoNeural",
        adapter="biscuitbot.providers.tts:EdgeTtsProvider",
        requires_api_key=False,
    ),
    TtsProviderSpec(
        name="newapi",
        default_model="gpt-4o-mini-tts",
        default_voice="alloy",
        adapter="biscuitbot.providers.tts:OpenAITtsProvider",
    ),
)

_BY_NAME = {spec.name: spec for spec in TTS_PROVIDERS}
_BY_ALIAS = {alias: spec for spec in TTS_PROVIDERS for alias in spec.aliases}

_DEFAULT_PROVIDER = "edge-tts"


def tts_provider_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in TTS_PROVIDERS)


def get_tts_provider(name: str) -> TtsProviderSpec | None:
    return _BY_NAME.get(name)


def generic_openai_tts_spec(name: str) -> TtsProviderSpec:
    """为声明了 tts 能力但无专用适配器的厂商合成通用 OpenAI 兼容 TTS 规格。"""
    return TtsProviderSpec(
        name=name,
        default_model="gpt-4o-mini-tts",
        default_voice="alloy",
        adapter="biscuitbot.providers.tts:OpenAITtsProvider",
        requires_api_key=True,
    )


def resolve_tts_provider(value: Any) -> TtsProviderSpec:
    """解析 provider 名；空值默认 edge-tts，未知值抛 ValueError。"""
    if not isinstance(value, str) or not value.strip():
        return _BY_NAME[_DEFAULT_PROVIDER]
    name = value.strip().lower()
    spec = _BY_NAME.get(name) or _BY_ALIAS.get(name)
    if spec is None:
        raise ValueError(f"未知的 TTS provider: {value}")
    return spec


def resolve_tts_spec(value: Any) -> TtsProviderSpec:
    """解析 provider 名；空值默认 edge-tts，未知值合成通用 OpenAI 兼容规格。"""
    if not isinstance(value, str) or not value.strip():
        return _BY_NAME[_DEFAULT_PROVIDER]
    name = value.strip().lower()
    spec = _BY_NAME.get(name) or _BY_ALIAS.get(name)
    if spec is None:
        return generic_openai_tts_spec(name)
    return spec
