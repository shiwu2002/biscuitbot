"""Application-level audio transcription service.

This module owns biscuitbot's transcription behavior: config resolution,
legacy channel fallback, upload validation, temporary-file handling, and
dispatch to provider adapters. It deliberately does not know provider-specific
HTTP details; those live in ``biscuitbot.providers.transcription``.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from biscuitbot.audio.transcription_registry import (
    get_transcription_provider,
    resolve_transcription_provider,
    resolve_transcription_spec,
)
from biscuitbot.config.paths import get_media_dir
from biscuitbot.providers.registry import find_by_name
from biscuitbot.utils.media_decode import FileSizeExceeded, save_base64_data_url

TranscriptionProviderName = str

_DEFAULT_PROVIDER: TranscriptionProviderName = "dashscope"
_MAX_AUDIO_BYTES_FALLBACK = 25 * 1024 * 1024
_AUDIO_MIME_ALLOWED: frozenset[str] = frozenset({
    "audio/aac",
    "audio/flac",
    "audio/m4a",
    "audio/mp4",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/x-wav",
})


@dataclass(frozen=True)
class EffectiveTranscriptionConfig:
    enabled: bool
    provider: TranscriptionProviderName
    model: str
    language: str | None
    api_key: str = field(repr=False)
    api_base: str
    max_duration_sec: int
    max_upload_mb: int

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


class TranscriptionIngressError(Exception):
    """Stable transcription upload error surfaced to WebUI clients."""

    def __init__(self, detail: str, **extra: Any):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


def _as_provider(value: Any) -> TranscriptionProviderName | None:
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip().lower()
    spec = resolve_transcription_provider(name)
    return spec.name if spec else name


def _provider_config(config: Any, provider: str) -> Any:
    providers = getattr(config, "providers", None)
    if providers is None:
        return None
    pc = getattr(providers, provider, None)
    if pc is not None:
        return pc
    return (providers.model_extra or {}).get(provider)


def _provider_default_api_base(provider: str) -> str | None:
    spec = find_by_name(provider)
    if spec:
        return spec.default_api_base
    from biscuitbot.audio.transcription_registry import get_transcription_provider
    ts = get_transcription_provider(provider)
    return ts.default_api_base if ts else None


def _resolve_transcription_api_key(provider: str, provider_cfg: Any) -> str:
    api_key = getattr(provider_cfg, "api_key", None) if provider_cfg else None
    if api_key:
        return api_key

    spec = find_by_name(provider)
    if provider == "siliconflow":
        env_key = os.environ.get("SILICONFLOW_API_KEY")
        if env_key:
            return env_key

    env_key = spec.env_key if spec else ""
    return os.environ.get(env_key) if env_key else ""


def _resolve_transcription_api_base(provider: str, provider_cfg: Any) -> str:
    # Resolve api_base: domain from LLM provider > provider default.
    # We only inherit the DOMAIN from the LLM provider (not the full path)
    # because transcription APIs may use different paths than LLM APIs
    # (e.g. DashScope LLM uses /compatible-mode/v1 but transcription uses its own path).
    # Each transcription client appends its own service-specific path.
    llm_api_base = getattr(provider_cfg, "api_base", None) if provider_cfg else None
    if llm_api_base:
        from biscuitbot.providers.image_generation import extract_domain
        return extract_domain(llm_api_base)
    return _provider_default_api_base(provider) or ""


def _extract_data_url_mime(url: str) -> str | None:
    header, _, _ = url.partition(",")
    if not header.startswith("data:") or ";base64" not in header:
        return None
    return header[5:].split(";", 1)[0].strip().lower() or None


def resolve_transcription_config(config: Any) -> EffectiveTranscriptionConfig:
    """Resolve top-level transcription settings with legacy channel fallback."""
    top = getattr(config, "transcription", None)
    channels = getattr(config, "channels", None)
    provider = (
        _as_provider(getattr(top, "provider", None))
        or _as_provider(getattr(channels, "transcription_provider", None))
        or _DEFAULT_PROVIDER
    )
    spec = get_transcription_provider(provider)
    if spec is None:
        # 未知厂商（用户在能力配置里声明了 transcription）→ 通用 OpenAI 兼容规格
        spec = resolve_transcription_spec(provider)
    default_model = spec.default_model if spec else ""
    provider_cfg = _provider_config(config, provider)
    return EffectiveTranscriptionConfig(
        enabled=bool(getattr(top, "enabled", True)),
        provider=provider,
        model=(getattr(top, "model", None) or default_model).strip(),
        language=getattr(top, "language", None) or getattr(channels, "transcription_language", None),
        api_key=_resolve_transcription_api_key(provider, provider_cfg),
        api_base=_resolve_transcription_api_base(provider, provider_cfg),
        max_duration_sec=int(getattr(top, "max_duration_sec", 120)),
        max_upload_mb=int(getattr(top, "max_upload_mb", 25)),
    )


async def transcribe_audio_data_url(
    data_url: Any,
    config: EffectiveTranscriptionConfig,
    *,
    duration_ms: Any = None,
) -> str:
    """Validate, persist, transcribe, and remove a WebUI audio data URL."""
    if not isinstance(data_url, str) or not data_url:
        raise TranscriptionIngressError("missing_audio")
    if not config.enabled:
        raise TranscriptionIngressError("disabled")
    if not config.configured:
        raise TranscriptionIngressError("not_configured", provider=config.provider)
    if (
        isinstance(duration_ms, (int, float))
        and duration_ms > (config.max_duration_sec * 1000 + 1000)
    ):
        raise TranscriptionIngressError("duration")
    if _extract_data_url_mime(data_url) not in _AUDIO_MIME_ALLOWED:
        raise TranscriptionIngressError("mime")

    audio_path: str | None = None
    max_bytes = max(
        1,
        config.max_upload_mb * 1024 * 1024 if config.max_upload_mb else _MAX_AUDIO_BYTES_FALLBACK,
    )
    try:
        audio_path = save_base64_data_url(
            data_url,
            get_media_dir("webui-transcription"),
            max_bytes=max_bytes,
        )
    except FileSizeExceeded as exc:
        raise TranscriptionIngressError("size") from exc
    except Exception as exc:
        logger.warning("transcription audio decode failed: {}", exc)
    if not audio_path:
        raise TranscriptionIngressError("decode")

    try:
        text = await transcribe_audio_file(audio_path, config)
    finally:
        with suppress(OSError):
            Path(audio_path).unlink(missing_ok=True)
    if not text:
        raise TranscriptionIngressError("empty")
    return text


async def transcribe_audio_file(
    file_path: str | Path,
    config: EffectiveTranscriptionConfig,
) -> str:
    """Transcribe *file_path* using the already-resolved transcription config."""
    if not config.enabled or not config.configured:
        return ""
    spec = get_transcription_provider(config.provider) or resolve_transcription_spec(config.provider)
    if spec is None:
        logger.warning("Unknown transcription provider: {}", config.provider)
        return ""
    provider = spec.load_adapter()(
        api_key=config.api_key,
        api_base=config.api_base or None,
        language=config.language,
        model=config.model,
    )
    return await provider.transcribe(file_path)
