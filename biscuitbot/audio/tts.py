"""Application-level text-to-speech service.

This module owns biscuitbot's TTS behavior: config resolution, output-path
handling, and dispatch to provider adapters. Provider-specific HTTP details
live in ``biscuitbot.providers.tts``.
"""

from __future__ import annotations

import os  # 读取环境变量（默认 API Key）
import uuid  # 生成唯一音频文件名
from dataclasses import dataclass, field  # 有效配置数据类
from datetime import datetime  # artifact 归档日期
from pathlib import Path  # 跨平台路径处理
from typing import Any  # 动态类型标注

from loguru import logger  # 结构化日志

from biscuitbot.audio.tts_registry import (
    get_tts_provider,
    resolve_tts_provider,
    resolve_tts_spec,
)
from biscuitbot.config.paths import get_workspace_path
from biscuitbot.providers.registry import find_by_name

TtsProviderName = str

_DEFAULT_PROVIDER: TtsProviderName = "edge-tts"


class TtsServiceError(Exception):
    """Stable TTS error surfaced to callers (tools / WebUI)."""

    def __init__(self, detail: str, **extra: Any):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


@dataclass(frozen=True)
class EffectiveTtsConfig:
    enabled: bool
    provider: TtsProviderName
    model: str
    voice: str
    rate: str | None
    api_key: str = field(repr=False)
    api_base: str
    save_dir: str

    @property
    def configured(self) -> bool:
        """免 key 的 provider（edge-tts）恒可用；其余需要 api_key。"""
        spec = get_tts_provider(self.provider)
        if spec is not None and not spec.requires_api_key:
            return True
        return bool(self.api_key)


def _provider_config(config: Any, provider: str) -> Any:
    """读取 ``config.providers.<provider>`` 的提供商配置（固定字段 + model_extra）。"""
    providers = getattr(config, "providers", None)
    if providers is None:
        return None
    pc = getattr(providers, provider, None)
    if pc is not None:
        return pc
    return (providers.model_extra or {}).get(provider)


def _resolve_tts_api_key(provider: str, provider_cfg: Any) -> str:
    """解析 API Key：providers.<name>.api_key 优先，其次环境变量。"""
    api_key = getattr(provider_cfg, "api_key", None) if provider_cfg else None
    if api_key:
        return api_key
    spec = get_tts_provider(provider)
    if spec is None or not spec.requires_api_key:
        return ""
    env_key = ""
    llm_spec = find_by_name(provider)
    if llm_spec:
        env_key = llm_spec.env_key
    return os.environ.get(env_key) if env_key else ""


def _resolve_tts_api_base(provider: str, provider_cfg: Any) -> str:
    """解析 API base：providers.<name>.api_base 优先，其次 provider 默认值。"""
    llm_api_base = getattr(provider_cfg, "api_base", None) if provider_cfg else None
    if llm_api_base:
        return llm_api_base
    spec = get_tts_provider(provider)
    return spec.default_api_base if spec else ""


def resolve_tts_config(config: Any) -> EffectiveTtsConfig:
    """解析顶层 TTS 配置，缺省项回退到 provider 默认值。"""
    top = getattr(config, "tts", None)
    spec = resolve_tts_spec(getattr(top, "provider", None))
    provider_cfg = _provider_config(config, spec.name)
    return EffectiveTtsConfig(
        enabled=bool(getattr(top, "enabled", True)),
        provider=spec.name,
        model=(getattr(top, "model", None) or spec.default_model).strip(),
        voice=(getattr(top, "voice", None) or spec.default_voice).strip(),
        rate=getattr(top, "rate", None),
        api_key=_resolve_tts_api_key(spec.name, provider_cfg),
        api_base=_resolve_tts_api_base(spec.name, provider_cfg),
        save_dir=(getattr(top, "save_dir", None) or "generated/tts").strip("/"),
    )


def resolve_tts_config_with_overrides(
    config: Any,
    *,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    rate: str | None = None,
) -> EffectiveTtsConfig:
    """在已解析配置基础上应用单次调用的 provider/model/voice/rate 覆盖。

    未覆盖的字段沿用顶层配置；切换 provider 时按该 provider 的默认
    model/voice 与 ``providers.<name>`` 的 key/base 重新解析。
    """
    eff = resolve_tts_config(config)
    spec = resolve_tts_spec(provider or eff.provider)
    top = getattr(config, "tts", None)
    provider_cfg = _provider_config(config, spec.name)
    return EffectiveTtsConfig(
        enabled=eff.enabled,
        provider=spec.name,
        model=(model or getattr(top, "model", None) or spec.default_model).strip(),
        voice=(voice or getattr(top, "voice", None) or spec.default_voice).strip(),
        rate=rate or getattr(top, "rate", None),
        api_key=_resolve_tts_api_key(spec.name, provider_cfg),
        api_base=_resolve_tts_api_base(spec.name, provider_cfg),
        save_dir=eff.save_dir,
    )


def _default_output_path(workspace: str | Path | None, save_dir: str) -> Path:
    """构造默认输出路径：``<workspace>/<save_dir>/<date>/tts_<uuid>.mp3``。"""
    base = get_workspace_path(workspace)
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    day_dir = base / save_dir / day
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / f"tts_{uuid.uuid4().hex[:12]}.mp3"


async def synthesize_speech_file(
    text: str,
    config: EffectiveTtsConfig,
    output_path: str | Path | None = None,
    *,
    workspace: str | Path | None = None,
) -> str:
    """合成 *text* 为音频文件并返回绝对路径。

    - 显式 *output_path* 优先；
    - 否则回退到 ``<workspace>/<save_dir>/<date>/tts_<uuid>.mp3``。
    """
    if not config.enabled:
        raise TtsServiceError("disabled")
    if not config.configured:
        raise TtsServiceError("not_configured", provider=config.provider)
    if not text or not text.strip():
        raise TtsServiceError("empty")
    spec = get_tts_provider(config.provider) or resolve_tts_spec(config.provider)
    path = (
        Path(output_path).expanduser()
        if output_path
        else _default_output_path(workspace, config.save_dir)
    )
    try:
        adapter = spec.load_adapter()(
            api_key=config.api_key or None,
            api_base=config.api_base or None,
            model=config.model,
            voice=config.voice,
            rate=config.rate,
        )
        return await adapter.synthesize(text, path)
    except TtsServiceError:
        raise
    except Exception as exc:
        # provider 层已给出可解释的错误（如未安装 edge-tts），只记 warning 不记 traceback
        logger.warning("TTS synthesis failed: {}", exc)
        raise TtsServiceError(str(exc)) from exc
