"""Settings REST helpers for the WebUI HTTP surface.

The WebSocket channel owns transport/authentication. This module owns the
settings payload shape and the allowlisted config mutations exposed to WebUI.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

from biscuitbot import __version__
from biscuitbot.audio.transcription import resolve_transcription_config
from biscuitbot.audio.transcription_registry import (
    get_transcription_provider,
    resolve_transcription_provider,
    transcription_provider_names,
)
from biscuitbot.audio.tts import resolve_tts_config
from biscuitbot.audio.tts_registry import (
    get_tts_provider,
    resolve_tts_provider,
    tts_provider_names,
)
from biscuitbot.config.loader import get_config_path, load_config, save_config
from biscuitbot.config.schema import ModelPresetConfig, ProviderConfig
from biscuitbot.providers.image_generation import (
    get_image_gen_provider,
    image_gen_provider_names,
)
from biscuitbot.providers.registry import PROVIDERS, create_dynamic_spec, find_by_name
from biscuitbot.security.workspace_access import workspace_sandbox_status
from biscuitbot.utils.knowledge_index import delete_document, list_documents
from biscuitbot.webui.token_usage import token_usage_payload
from biscuitbot.webui.workspaces import (
    read_webui_default_access_mode,
    write_webui_default_access_mode,
)

QueryParams = dict[str, list[str]]
RuntimeSurface = Literal["browser", "native"]


def _version_payload() -> dict[str, Any]:
    """Return version info for the settings payload."""
    return {
        "current": __version__,
    }

_RUNTIME_CAPABILITIES = {
    "can_restart_engine": False,
    "can_pick_folder": False,
    "can_open_logs": False,
    "can_export_diagnostics": False,
}

_NATIVE_RUNTIME_CAPABILITIES = {
    **_RUNTIME_CAPABILITIES,
    "can_restart_engine": True,
    "can_pick_folder": True,
    "can_open_logs": True,
    "can_export_diagnostics": True,
}

_BROWSER_RESTART_BEHAVIOR_BY_SECTION = {
    "appearance": "none",
    "models": "none",
    "providers": "none",
    "runtime": "engineRestart",
    "browser": "engineRestart",
    "image": "engineRestart",
    "video": "engineRestart",
    "apps": "engineRestart",
    "systemIo": "engineRestart",
    "advanced": "appRestart",
}

_NATIVE_RESTART_BEHAVIOR_BY_SECTION = {
    **_BROWSER_RESTART_BEHAVIOR_BY_SECTION,
    "runtime": "engineRestart",
    "browser": "engineRestart",
    "image": "engineRestart",
    "apps": "engineRestart",
    "systemIo": "engineRestart",
}

# System IO tool actions exposed in the settings payload. ``write`` flags
# actions that mutate host state so the UI can group/sort them.
_SYSTEM_IO_ACTIONS: list[dict[str, Any]] = [
    {"name": "clipboard_read", "label": "Read clipboard", "write": False},
    {"name": "clipboard_write", "label": "Write clipboard", "write": True},
    {"name": "key_tap", "label": "Key tap (chord)", "write": True},
    {"name": "key_type", "label": "Key type (text)", "write": True},
    {"name": "mouse_move", "label": "Mouse move", "write": True},
    {"name": "mouse_click", "label": "Mouse click", "write": True},
    {"name": "mouse_scroll", "label": "Mouse scroll", "write": True},
    {"name": "usb_list", "label": "List USB devices", "write": False},
    {"name": "serial_list", "label": "List serial ports", "write": False},
    {"name": "serial_write", "label": "Serial write", "write": True},
    {"name": "serial_read", "label": "Serial read", "write": True},
]
_SYSTEM_IO_VALID_ACTIONS = {item["name"] for item in _SYSTEM_IO_ACTIONS}

_WEB_SEARCH_PROVIDER_OPTIONS: tuple[dict[str, str], ...] = (
    {"name": "duckduckgo", "label": "DuckDuckGo", "credential": "none"},
    {"name": "brave", "label": "Brave Search", "credential": "api_key"},
    {"name": "tavily", "label": "Tavily", "credential": "api_key"},
    {"name": "searxng", "label": "SearXNG", "credential": "base_url"},
    {"name": "jina", "label": "Jina", "credential": "api_key"},
    {"name": "kagi", "label": "Kagi", "credential": "api_key"},
    {"name": "exa", "label": "Exa", "credential": "api_key"},
    {"name": "olostep", "label": "Olostep", "credential": "api_key"},
    {"name": "bocha", "label": "Bocha", "credential": "api_key"},
    {"name": "volcengine", "label": "Volcengine Search", "credential": "api_key"},
)
_WEB_SEARCH_PROVIDER_BY_NAME = {
    provider["name"]: provider for provider in _WEB_SEARCH_PROVIDER_OPTIONS
}

_IMAGE_GENERATION_ASPECT_RATIOS = {
    "1:1",
    "3:4",
    "9:16",
    "4:3",
    "16:9",
    "3:2",
    "2:3",
    "21:9",
}

# 视频生成（Seedance）可选项 —— 与 seedance_video.py 的约束保持一致。
_VIDEO_RATIO_OPTIONS = {
    "16:9",
    "9:16",
    "1:1",
    "4:3",
    "3:4",
    "21:9",
    "adaptive",
}
_VIDEO_RESOLUTION_OPTIONS = {"480p", "720p", "1080p", "4K"}
_VIDEO_DURATION_MIN = 4
_VIDEO_DURATION_MAX = 30

_CONTEXT_WINDOW_TOKEN_OPTIONS = {65_536, 262_144}
_MODEL_CONFIGURATION_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_MODEL_LIST_UNSUPPORTED_BACKENDS = {
    "anthropic",
}

_MODEL_LIST_CATALOG_PROVIDERS = {
    "aihubmix",
    "byteplus",
    "byteplus_coding_plan",
    "huggingface",
    "novita",
    "openrouter",
    "siliconflow",
}

_MODEL_LIST_OFFICIAL_PROVIDERS = {
    "ant_ling",
    "dashscope",
    "deepseek",
    "gemini",
    "groq",
    "longcat",
    "minimax",
    "minimax_anthropic",
    "mistral",
    "moonshot",
    "nvidia",
    "openai",
    "qianfan",
    "skywork",
    "stepfun",
    "volcengine",
    "volcengine_coding_plan",
    "xiaomi_mimo",
    "zhipu",
}


class WebUISettingsError(ValueError):
    """User-facing settings validation failure."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _normalize_surface(surface: str | None) -> RuntimeSurface:
    return "native" if surface in {"native", "desktop"} else "browser"


def runtime_capabilities(
    surface: str | None = "browser",
    overrides: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Return the capability flags exposed to the WebUI runtime."""
    base = (
        _NATIVE_RUNTIME_CAPABILITIES
        if _normalize_surface(surface) == "native"
        else _RUNTIME_CAPABILITIES
    )
    result = dict(base)
    for key, value in (overrides or {}).items():
        if key in result:
            result[key] = bool(value)
    return result


def restart_behavior_by_section(surface: str | None = "browser") -> dict[str, str]:
    return dict(
        _NATIVE_RESTART_BEHAVIOR_BY_SECTION
        if _normalize_surface(surface) == "native"
        else _BROWSER_RESTART_BEHAVIOR_BY_SECTION
    )


def decorate_settings_payload(
    payload: dict[str, Any],
    *,
    surface: str | None = "browser",
    runtime_capability_overrides: dict[str, Any] | None = None,
    restart_required_sections: list[str] | None = None,
    apply_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach runtime-surface metadata without changing the core settings shape."""
    surface_value = _normalize_surface(surface)
    sections = restart_required_sections
    if sections is None:
        raw_sections = payload.get("restart_required_sections") or []
        sections = [str(section) for section in raw_sections if isinstance(section, str)]
    sections = sorted(dict.fromkeys(sections))
    result = dict(payload)
    result["surface"] = surface_value
    result["runtime_surface"] = surface_value
    result["runtime_capabilities"] = runtime_capabilities(
        surface_value,
        runtime_capability_overrides,
    )
    result["restart_behavior_by_section"] = restart_behavior_by_section(surface_value)
    result["restart_required_sections"] = sections
    if sections:
        result["requires_restart"] = True
    else:
        result["requires_restart"] = bool(result.get("requires_restart", False))
    result["apply_state"] = apply_state or {
        "status": "pending" if result["requires_restart"] else "idle",
        "sections": sections,
    }
    return result


def _query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _query_first_alias(query: QueryParams, snake: str, camel: str) -> str | None:
    value = _query_first(query, snake)
    return _query_first(query, camel) if value is None else value


def _mask_secret_hint(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


def _resolve_env_placeholders(value: str | None) -> str | None:
    if not value:
        return None
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        env_value = os.environ.get(match.group(1))
        if env_value is None:
            missing = True
            return ""
        return env_value

    resolved = _ENV_REF_RE.sub(replace, value).strip()
    if missing and not resolved:
        return None
    return resolved or None


def _provider_requires_api_key(spec: Any) -> bool:
    if spec.is_local or spec.is_direct:
        return False
    return True


def _provider_requires_api_base(spec: Any) -> bool:
    return bool(spec.backend == "openai_compat" and spec.is_direct and not spec.default_api_base)


def _provider_configured_for_settings(spec: Any, provider_config: Any) -> bool:
    if _provider_requires_api_base(spec):
        return bool(provider_config.api_base)
    if _provider_requires_api_key(spec):
        return bool(provider_config.api_key)
    return bool(
        provider_config.api_key
        or provider_config.api_base
        or getattr(provider_config, "region", None)
        or getattr(provider_config, "profile", None)
    )


def _dynamic_provider_items(config: Any) -> list[tuple[str, ProviderConfig]]:
    return [
        (name, provider_config)
        for name, provider_config in (config.providers.model_extra or {}).items()
        if isinstance(provider_config, ProviderConfig)
    ]


def _provider_config_by_name(config: Any, name: str) -> ProviderConfig | None:
    """按名称取 ProviderConfig：先查固定字段，再查 model_extra 自定义厂商。

    能力专用厂商（volcengine / gemini / aihubmix 等）不是 ProvidersConfig 的
    固定字段，「模型厂商」页会把它们存进 model_extra，直接 ``getattr`` 会漏掉。
    """
    pc = getattr(config.providers, name, None)
    if isinstance(pc, ProviderConfig):
        return pc
    for extra_name, extra_pc in _dynamic_provider_items(config):
        if extra_name == name:
            return extra_pc
    return None


def _resolve_settings_provider(
    config: Any,
    provider_name: str,
) -> tuple[Any, str, ProviderConfig] | None:
    spec = find_by_name(provider_name)
    if spec is not None:
        provider_config = getattr(config.providers, spec.name, None)
        if isinstance(provider_config, ProviderConfig):
            return spec, spec.name, provider_config
        return None

    normalized = provider_name.replace("-", "_")
    for extra_name, provider_config in _dynamic_provider_items(config):
        if provider_name == extra_name or normalized == extra_name.replace("-", "_"):
            return create_dynamic_spec(extra_name), extra_name, provider_config
    return None


def _resolve_model_list_provider(
    config: Any,
    provider_name: str,
) -> tuple[Any, str, ProviderConfig] | None:
    """Resolve a provider for model-list fetching, including non-LLM capabilities.

    ``/api/settings/provider-models`` is also used by the image / TTS / transcription
    settings pages. Providers like ``volcengine`` / ``gemini`` / ``groq`` are not LLM
    providers (absent from ``PROVIDERS``), so they need a synthesized spec whose
    ``default_api_base`` and key requirement come from the capability registry.
    """
    resolved = _resolve_settings_provider(config, provider_name)
    if resolved is not None:
        return resolved

    name = provider_name.strip()
    default_api_base: str | None = None
    requires_api_key = True

    tts_spec = get_tts_provider(name)
    transcription_spec = get_transcription_provider(name) if tts_spec is None else None
    image_provider = (
        get_image_gen_provider(name)
        if tts_spec is None and transcription_spec is None
        else None
    )
    if tts_spec is not None:
        default_api_base = tts_spec.default_api_base
        requires_api_key = tts_spec.requires_api_key
    elif transcription_spec is not None:
        default_api_base = transcription_spec.default_api_base
        requires_api_key = True
    elif image_provider is not None:
        default_api_base = None
        requires_api_key = name != "ollama"
    else:
        return None

    spec = create_dynamic_spec(name)
    spec = replace(
        spec,
        default_api_base=default_api_base or "",
        is_direct=not requires_api_key,
    )

    provider_config = getattr(config.providers, name, None)
    if not isinstance(provider_config, ProviderConfig):
        provider_config = next(
            (pc for extra_name, pc in _dynamic_provider_items(config) if extra_name == name),
            ProviderConfig(),
        )
    return spec, name, provider_config


# 厂商可声明/推导的全部能力标签（各能力页据此过滤厂商下拉项）。
_CAPABILITY_KEYS = ("llm", "vision", "image", "video", "tts", "transcription")


def _detect_provider_capabilities(name: str, config: Any) -> list[str]:
    """按注册表/硬编码规则自动推断厂商能力（用户未显式声明时的兜底）。"""
    caps: list[str] = []
    spec = find_by_name(name)
    is_dynamic = any(key == name for key, _ in _dynamic_provider_items(config))
    # 能力专用厂商（图像/TTS/转写/视频）即使存入 model_extra 也不具备 LLM 能力
    is_capability_only = spec is None and (
        get_image_gen_provider(name) is not None
        or name == "volcengine"
        or get_tts_provider(name) is not None
        or get_transcription_provider(name) is not None
    )
    is_llm = spec is not None or (is_dynamic and not is_capability_only)
    if is_llm:
        caps.append("llm")
        if spec is None or not spec.is_transcription_only:
            caps.append("vision")
    if get_image_gen_provider(name) is not None:
        caps.append("image")
    if name == "volcengine":
        caps.append("video")
    if get_tts_provider(name) is not None:
        caps.append("tts")
    if get_transcription_provider(name) is not None:
        caps.append("transcription")
    return caps


def _provider_capabilities(name: str, config: Any) -> list[str]:
    """厂商可服务的能力标签：优先读用户在「模型厂商」页显式声明，否则自动推断。"""
    provider_config = _provider_config_by_name(config, name)
    declared = getattr(provider_config, "capabilities", None) if provider_config else None
    if declared is not None:
        # 只保留合法能力标签并去重，保持稳定顺序
        seen: list[str] = []
        for cap in declared:
            if cap in _CAPABILITY_KEYS and cap not in seen:
                seen.append(cap)
        return seen
    return _detect_provider_capabilities(name, config)


def _capability_provider_label(name: str, spec: Any) -> str:
    """非 LLM 能力厂商的展示名（火山方舟 / Edge TTS 等）。"""
    if find_by_name(name) is not None:
        return spec.label if spec is not None else name
    if name == "volcengine":
        return "火山方舟"
    if name == "edge-tts":
        return "Edge TTS"
    return name


def _provider_settings_row(
    name: str,
    spec: Any,
    provider_config: ProviderConfig,
) -> dict[str, Any]:
    row = {
        "name": name,
        "label": spec.label,
        "configured": _provider_configured_for_settings(spec, provider_config),
        "auth_type": "api_key",
        "api_key_required": _provider_requires_api_key(spec),
        "api_key_hint": _mask_secret_hint(provider_config.api_key),
        "api_base": provider_config.api_base,
        "default_api_base": spec.default_api_base or None,
        "model_selectable": not spec.is_transcription_only,
    }
    if spec.name == "openai":
        row["api_type"] = provider_config.api_type
    return row


def _unified_provider_rows(config: Any) -> list[dict[str, Any]]:
    """单一厂商来源：LLM PROVIDERS + 非 LLM 的图像/TTS/转写厂商 + 动态自定义厂商。

    各能力页不再各自维护厂商列表，统一从此处派生并按 ``capabilities`` 过滤。
    """
    names: list[str] = [spec.name for spec in PROVIDERS]
    for extra in (*image_gen_provider_names(), *tts_provider_names(), *transcription_provider_names()):
        if extra not in names:
            names.append(extra)
    for extra_name, _ in _dynamic_provider_items(config):
        if extra_name not in names:
            names.append(extra_name)

    hidden = {str(name).replace("-", "_") for name in (config.hidden_providers or [])}

    rows: list[dict[str, Any]] = []
    for name in names:
        if name.replace("-", "_") in hidden:
            continue
        resolved = _resolve_model_list_provider(config, name)
        if resolved is None:
            continue
        spec, key, provider_config = resolved
        row = _provider_settings_row(key, spec, provider_config)
        # 覆盖能力厂商特有的「已配置」语义
        if name == "volcengine":
            row["configured"] = bool(
                provider_config.api_key or os.environ.get("ARK_API_KEY", "").strip()
            )
        elif name == "edge-tts":
            row["configured"] = True  # 免密钥，恒可用
        row["capabilities"] = _provider_capabilities(name, config)
        # 非 LLM 能力厂商不进入 LLM 模型预设的厂商下拉
        if "llm" not in row["capabilities"]:
            row["model_selectable"] = False
        row["label"] = _capability_provider_label(name, spec)
        # 非固定 LLM 厂商（能力专用 / 动态自定义厂商）可被删除；固定字段厂商属内置不可删
        row["deletable"] = find_by_name(name) is None
        rows.append(row)
    return rows


def needs_setup(config: Any) -> bool:
    """是否处于「首次使用、未配置任何 LLM provider」状态。

    与设置页的 ``_provider_settings_row`` 判定保持一致：只要有一个
    provider 达到 configured 即视为已就绪。用于应用内欢迎设置页
    （桌面打包版首次启动时引导小白用户配置 API Key）。
    """
    for spec in PROVIDERS:
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None:
            continue
        if _provider_settings_row(spec.name, spec, provider_config)["configured"]:
            return False
    for provider_key, provider_config in _dynamic_provider_items(config):
        if _provider_settings_row(
            provider_key,
            create_dynamic_spec(provider_key),
            provider_config,
        )["configured"]:
            return False
    return True


_CHANNEL_FIELD_LABELS: dict[str, dict[str, str]] = {
    # 钉钉：Stream Mode 机器人
    "dingtalk": {
        "clientId": "AppKey（Client ID）",
        "clientSecret": "AppSecret（Client Secret）",
    },
    # 飞书：开放平台应用
    "feishu": {
        "appId": "应用 ID（App ID）",
        "appSecret": "应用密钥（App Secret）",
        "encryptKey": "事件加密密钥（Encrypt Key）",
        "verificationToken": "事件验证令牌（Verification Token）",
    },
    # 企业微信：AI 机器人
    "wecom": {
        "botId": "机器人 ID（Bot ID）",
        "secret": "机器人密钥（Secret）",
        "welcomeMessage": "欢迎语（可选）",
    },
    # QQ 开放平台
    "qq": {
        "appId": "应用 ID（App ID）",
        "secret": "应用密钥（Secret）",
        "mediaDir": "媒体缓存目录（可选）",
    },
    # NapCat（QQ 协议）
    "napcat": {
        "wsUrl": "WebSocket 地址",
        "accessToken": "访问令牌（Access Token）",
    },
    # MoChat
    "mochat": {
        "socketUrl": "Socket.IO 地址（可选）",
        "clawToken": "鉴权令牌（Token）",
        "agentUserId": "机器人用户 ID",
    },
    # 邮件（IMAP 收 / SMTP 发）
    "email": {
        "consentGranted": "我已授权机器人读取并处理邮箱",
        "imapHost": "IMAP 服务器",
        "imapUsername": "IMAP 用户名",
        "imapPassword": "IMAP 密码",
        "smtpHost": "SMTP 服务器",
        "smtpUsername": "SMTP 用户名",
        "smtpPassword": "SMTP 密码",
        "fromAddress": "发件人地址",
    },
    # WebSocket
    "websocket": {
        "token": "访问令牌（Token，可选）",
        "tokenIssuePath": "令牌签发路径（可选）",
        "tokenIssueSecret": "令牌签发密钥（可选）",
        "unixSocketPath": "Unix Socket 路径（可选）",
        "sslCertfile": "SSL 证书路径（可选）",
        "sslKeyfile": "SSL 私钥路径（可选）",
    },
}


def _is_secret_key(key: str) -> bool:
    """渠道配置字段是否为敏感凭据（前端用密码输入框渲染）。"""
    lowered = key.lower()
    return (
        any(token in lowered for token in ("secret", "token", "password"))
        or lowered.endswith("key")
    )


def _humanize_field_key(key: str) -> str:
    """把 snake_case 字段名转成可读标签（未命中中文标签表时的兜底）。"""
    return key.replace("_", " ").strip()


def _channel_field_schema(name: str, cls: type, section: Any) -> list[dict[str, Any]]:
    """返回渠道需要用户填写的凭据字段（用于 WebUI 配置表单）。

    以 ``default_config()`` 的默认值为基线：仅暴露默认值为空字符串、需要用户
    填写的字段，外加 ``consent_granted``（布尔授权）。``enabled``、嵌套 dict、
    列表白名单、以及已有合理默认值的字段不进入表单（仍可在 config.json 维护）。
    """
    if not hasattr(cls, "default_config"):
        return []
    default = cls.default_config()
    if not isinstance(default, dict):
        return []
    if isinstance(section, dict):
        data = section
    elif hasattr(section, "model_dump"):
        data = section.model_dump(by_alias=True)
    else:
        data = {}

    labels = _CHANNEL_FIELD_LABELS.get(name, {})
    fields: list[dict[str, Any]] = []
    for key, default_value in default.items():
        if key == "enabled":
            continue
        if isinstance(default_value, bool):
            if key not in ("consent_granted", "consentGranted"):
                continue
            field_type = "boolean"
        elif default_value == "":
            field_type = "string"
        else:
            continue  # None / 非空默认值 / 数值 / 列表 / 字典 —— 不进表单

        current = data.get(key, default_value)
        fields.append({
            "key": key,
            "label": labels.get(key, _humanize_field_key(key)),
            "type": field_type,
            "secret": _is_secret_key(key),
            "value": "" if current is None else current,
        })
    return fields


def channels_payload() -> dict[str, Any]:
    """列出所有可连接渠道及其启用/配置状态。

    复用 CLI ``channels_status``（biscuitbot/cli/commands.py）的枚举方式：
    ``discover_all()`` 拿到内置 + 插件渠道类，再从 ``config.channels.<name>``
    读取启用状态。此处不做渠道的实时运行状态（需桥接主进程 ChannelManager）。
    """
    from biscuitbot.channels.registry import discover_all

    config = load_config()
    rows: list[dict[str, Any]] = []
    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        enabled = (
            section.get("enabled", False)
            if isinstance(section, dict)
            else getattr(section, "enabled", False)
        )
        rows.append({
            "name": name,
            "display_name": cls.display_name,
            "enabled": bool(enabled),
            "configured": _channel_configured(section, cls),
            "has_qr_login": name == "weixin",
            # 微信走扫码登录，其余渠道暴露需填写的凭据字段供前端渲染表单。
            "fields": [] if name == "weixin" else _channel_field_schema(name, cls, section),
        })
    return {"channels": rows}


def _channel_configured(section: Any, cls: type) -> bool:
    """渠道是否有任一非默认凭据字段（用于「已配置」状态提示）。

    以渠道 ``default_config()`` 的空值作为基线：只要某个基线为空/缺失的字段
    现在有值，即认为已配置。仅 ``enabled`` 不计入。
    """
    if section is None:
        return False
    if isinstance(section, dict):
        data = section
    elif hasattr(section, "model_dump"):
        data = section.model_dump(by_alias=True)
    else:
        return False
    default = cls.default_config() if hasattr(cls, "default_config") else {}
    empty = (None, "", [], {}, False)
    for key, value in data.items():
        if key == "enabled":
            continue
        if default.get(key) in empty and value not in empty:
            return True
    return False


def _coerce_channel_value(raw: str, default_value: Any, key: str) -> Any:
    """把 query 字符串归一化为渠道配置字段的目标类型。"""
    if isinstance(default_value, bool):
        return _parse_bool(raw, key)
    if isinstance(default_value, int):
        try:
            return int(raw.strip())
        except ValueError:
            raise WebUISettingsError(f"{key} must be an integer") from None
    if isinstance(default_value, list):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw  # str / None


def update_channel_settings(query: QueryParams) -> dict[str, Any]:
    """切换渠道启用状态并写入凭据字段，持久化到 config.json。

    渠道名称校验通过 ``discover_all()``（内置 + 插件），未知渠道报错。除
    ``enabled`` 外，其余 query 参数按渠道 ``default_config()`` 的字段类型归一化
    后写入对应渠道配置。修改需重启网关后由 ``ChannelManager._init_channels``
    生效，故返回 ``requires_restart``。
    """
    from biscuitbot.channels.registry import discover_all

    name = (_query_first(query, "channel") or "").strip().lower()
    if not name:
        raise WebUISettingsError("channel is required")
    all_channels = discover_all()
    if name not in all_channels:
        raise WebUISettingsError("unknown channel")
    cls = all_channels[name]

    config = load_config()
    section = getattr(config.channels, name, None)
    changed = False

    def _current_value(key: str, default: Any) -> Any:
        if isinstance(section, dict):
            return section.get(key, default)
        if section is not None:
            return getattr(section, key, default)
        return default

    # 1) enabled 标志
    enabled_raw = _query_first(query, "enabled")
    if enabled_raw is not None:
        enabled = _parse_bool(enabled_raw, "enabled")
        if isinstance(section, dict):
            if section.get("enabled", False) != enabled:
                section["enabled"] = enabled
                changed = True
        elif section is not None:
            if getattr(section, "enabled", False) != enabled:
                section.enabled = enabled
                changed = True
        else:
            section = {"enabled": enabled}
            setattr(config.channels, name, section)
            changed = True

    # 2) 凭据字段（default_config 中的空字符串 / consent_granted 字段）
    default = cls.default_config() if hasattr(cls, "default_config") else {}
    if isinstance(default, dict):
        for key, default_value in default.items():
            if key == "enabled":
                continue
            raw = _query_first(query, key)
            if raw is None:
                continue
            coerced = _coerce_channel_value(raw, default_value, key)
            if _current_value(key, default_value) != coerced:
                if section is None:
                    section = {}
                    setattr(config.channels, name, section)
                if isinstance(section, dict):
                    section[key] = coerced
                else:
                    setattr(section, key, coerced)
                changed = True

    if changed:
        save_config(config)

    payload = channels_payload()
    payload["requires_restart"] = changed
    return payload


def _model_catalog_kind(spec: Any) -> str:
    if spec.name in _MODEL_LIST_CATALOG_PROVIDERS:
        return "catalog"
    if spec.name in _MODEL_LIST_OFFICIAL_PROVIDERS:
        return "official"
    if spec.is_local:
        return "local"
    if spec.is_direct:
        return "custom"
    if spec.is_gateway:
        return "catalog"
    return "official"


def _model_id_from_row(row: Any) -> str | None:
    if isinstance(row, str):
        return row.strip() or None
    if not isinstance(row, dict):
        return None
    for key in ("id", "name", "model"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _model_context_window(row: Any) -> int | None:
    if not isinstance(row, dict):
        return None
    for key in (
        "context_window",
        "context_length",
        "max_context_length",
        "max_model_len",
        "max_input_tokens",
    ):
        value = row.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, float) and value > 0:
            return int(value)
    return None


def _model_row_payload(row: Any) -> dict[str, Any] | None:
    model_id = _model_id_from_row(row)
    if not model_id:
        return None
    label: str | None = None
    owned_by: str | None = None
    if isinstance(row, dict):
        raw_label = row.get("display_name") or row.get("label") or row.get("name")
        if isinstance(raw_label, str) and raw_label.strip() and raw_label.strip() != model_id:
            label = raw_label.strip()
        raw_owner = row.get("owned_by") or row.get("owner") or row.get("organization")
        if isinstance(raw_owner, str) and raw_owner.strip():
            owned_by = raw_owner.strip()
    return {
        "id": model_id,
        "label": label,
        "owned_by": owned_by,
        "context_window": _model_context_window(row),
    }


def _extract_model_rows(body: Any) -> list[dict[str, Any]]:
    raw_rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(raw_rows, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in raw_rows:
        row = _model_row_payload(raw_row)
        if row is None or row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
    return rows


def provider_models_payload(query: QueryParams) -> dict[str, Any]:
    """Fetch an OpenAI-compatible provider's model list for Settings.

    The result is advisory only: users can always type a custom model id. This
    helper deliberately avoids mutating config so probing model lists never
    changes runtime behavior.
    """
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")

    config = load_config()
    resolved_provider = _resolve_model_list_provider(config, provider_name)
    if resolved_provider is None:
        raise WebUISettingsError("unknown provider")
    spec, provider_key, provider_config = resolved_provider

    base_payload: dict[str, Any] = {
        "provider": provider_key,
        "label": spec.label,
        "catalog_kind": _model_catalog_kind(spec),
        "models": [],
        "model_count": 0,
        "message": None,
        "fetched_at": time.time(),
    }
    if (
        spec.is_transcription_only
        or (
            spec.backend in _MODEL_LIST_UNSUPPORTED_BACKENDS
            and spec.name != "minimax_anthropic"
        )
    ):
        return {
            **base_payload,
            "status": "unsupported",
            "catalog_kind": "unsupported",
            "message": "Model list is not available for this provider. Type a model ID manually.",
        }

    api_base = _resolve_env_placeholders(provider_config.api_base) or spec.default_api_base
    if spec.name == "openai" and not api_base:
        api_base = "https://api.openai.com/v1"
    if not api_base:
        return {
            **base_payload,
            "status": "missing_api_base",
            "message": "Configure an API base URL to load models.",
        }

    api_key = _resolve_env_placeholders(provider_config.api_key)
    if _provider_requires_api_key(spec) and not api_key:
        return {
            **base_payload,
            "status": "not_configured",
            "message": "Configure this provider before loading models.",
        }

    headers = {"Accept": "application/json"}
    if api_key:
        if spec.name == "minimax_anthropic":
            headers["X-Api-Key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    models_url = f"{api_base.rstrip('/')}/models"
    if spec.name == "minimax_anthropic" and not api_base.rstrip("/").endswith("/v1"):
        models_url = f"{api_base.rstrip('/')}/v1/models"

    try:
        response = httpx.get(
            models_url,
            headers=headers,
            timeout=10.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        rows = _extract_model_rows(response.json())
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            return {
                **base_payload,
                "status": "not_configured",
                "message": "The provider rejected the configured credential.",
            }
        return {
            **base_payload,
            "status": "error",
            "message": f"Model list request failed with HTTP {status}.",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            **base_payload,
            "status": "error",
            "message": f"Could not load models: {exc}",
        }

    return {
        **base_payload,
        "status": "available",
        "models": rows,
        "model_count": len(rows),
    }


def _parse_bool(value: str, field: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"1", "0", "true", "false", "yes", "no"}:
        raise WebUISettingsError(f"{field} must be boolean")
    return normalized in {"1", "true", "yes"}


def _parse_context_window_tokens(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise WebUISettingsError("context_window_tokens must be an integer") from None
    if parsed not in _CONTEXT_WINDOW_TOKEN_OPTIONS:
        raise WebUISettingsError("context_window_tokens must be 65536 or 262144")
    return parsed


def _model_configuration_slug(label: str) -> str:
    normalized = _MODEL_CONFIGURATION_SLUG_RE.sub("-", label.strip().lower())
    normalized = normalized.strip("-_")
    if not normalized:
        raise WebUISettingsError("configuration name is required")
    if normalized == "default":
        raise WebUISettingsError("configuration name is reserved")
    if len(normalized) > 48:
        normalized = normalized[:48].rstrip("-_")
    return normalized


def _validate_configured_provider(config: Any, provider: str) -> None:
    if provider == "auto":
        return
    resolved_provider = _resolve_settings_provider(config, provider)
    if resolved_provider is None:
        raise WebUISettingsError("unknown provider")
    spec, _, provider_config = resolved_provider
    if spec.is_transcription_only:
        raise WebUISettingsError("provider does not support chat models")
    if not _provider_configured_for_settings(spec, provider_config):
        raise WebUISettingsError("provider is not configured")


def settings_payload(
    *,
    requires_restart: bool = False,
    surface: str | None = "browser",
    runtime_capability_overrides: dict[str, Any] | None = None,
    restart_required_sections: list[str] | None = None,
    apply_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_config()
    defaults = config.agents.defaults
    active_preset_name = defaults.model_preset or "default"
    try:
        effective_preset = config.resolve_preset()
    except Exception:
        effective_preset = config.resolve_default_preset()
        active_preset_name = "default"

    provider_name = (
        config.get_provider_name(effective_preset.model, preset=effective_preset)
        or effective_preset.provider
    )
    provider = config.get_provider(effective_preset.model, preset=effective_preset)
    selected_provider = provider_name
    if effective_preset.provider != "auto":
        spec = find_by_name(effective_preset.provider)
        selected_provider = spec.name if spec else provider_name

    providers = _unified_provider_rows(config)

    search_config = config.tools.web.search
    image_config = config.tools.image_generation
    video_config = config.tools.seedance_video
    transcription = resolve_transcription_config(config)
    tts = resolve_tts_config(config)
    search_provider = (
        search_config.provider
        if search_config.provider in _WEB_SEARCH_PROVIDER_BY_NAME
        else "duckduckgo"
    )
    selected_image_provider = next(
        (provider for provider in providers if provider["name"] == image_config.provider),
        None,
    )
    model_presets = [
        {
            "name": "default",
            "label": "Default",
            "active": active_preset_name == "default",
            "is_default": True,
            "model": defaults.model,
            "provider": defaults.provider,
            "max_tokens": defaults.max_tokens,
            "context_window_tokens": defaults.context_window_tokens,
            "temperature": defaults.temperature,
            "reasoning_effort": defaults.reasoning_effort,
        }
    ]
    for name, preset in config.model_presets.items():
        model_presets.append(
            {
                "name": name,
                "label": preset.label or name,
                "active": active_preset_name == name,
                "is_default": False,
                "model": preset.model,
                "provider": preset.provider,
                "max_tokens": preset.max_tokens,
                "context_window_tokens": preset.context_window_tokens,
                "temperature": preset.temperature,
                "reasoning_effort": preset.reasoning_effort,
            }
        )

    exec_config = config.tools.exec
    sandbox_status = workspace_sandbox_status(
        restrict_to_workspace=config.tools.restrict_to_workspace,
        workspace=config.workspace_path,
    )
    payload = {
        "agent": {
            "model": effective_preset.model,
            "provider": selected_provider,
            "resolved_provider": provider_name,
            "has_api_key": bool(provider and provider.api_key),
            "model_preset": active_preset_name,
            "max_tokens": effective_preset.max_tokens,
            "context_window_tokens": effective_preset.context_window_tokens,
            "temperature": effective_preset.temperature,
            "reasoning_effort": effective_preset.reasoning_effort,
            "timezone": defaults.timezone,
            "bot_name": defaults.bot_name,
            "bot_icon": defaults.bot_icon,
            "tool_hint_max_length": defaults.tool_hint_max_length,
            "vision_model": defaults.vision_model,
            "vision_model_override": defaults.vision_model_override,
        },
        "model_presets": model_presets,
        "providers": providers,
        "web_search": {
            "provider": search_provider,
            "api_key_hint": _mask_secret_hint(search_config.api_key),
            "base_url": search_config.base_url or None,
            "max_results": search_config.max_results,
            "timeout": search_config.timeout,
            "providers": list(_WEB_SEARCH_PROVIDER_OPTIONS),
        },
        "web": {
            "enable": config.tools.web.enable,
            "proxy": config.tools.web.proxy,
            "user_agent": config.tools.web.user_agent,
            "search": {
                "max_results": search_config.max_results,
                "timeout": search_config.timeout,
            },
            "fetch": {
                "use_jina_reader": config.tools.web.fetch.use_jina_reader,
            },
        },
        "image_generation": {
            "enabled": image_config.enabled,
            "provider": image_config.provider,
            "provider_configured": bool(
                selected_image_provider and selected_image_provider["configured"]
            ),
            "model": image_config.model,
            "default_aspect_ratio": image_config.default_aspect_ratio,
            "default_image_size": image_config.default_image_size,
            "max_images_per_turn": image_config.max_images_per_turn,
            "save_dir": image_config.save_dir,
        },
        "video_generation": {
            "enabled": video_config.enabled,
            "api_key_configured": bool(
                (video_config.api_key or "").strip()
                or (
                    (volcengine_cfg := _provider_config_by_name(config, "volcengine"))
                    and (volcengine_cfg.api_key or "").strip()
                )
                or os.environ.get("ARK_API_KEY", "").strip()
            ),
            "provider": video_config.provider,
            "model": video_config.model,
            "default_ratio": video_config.default_ratio,
            "default_duration": video_config.default_duration,
            "default_resolution": video_config.default_resolution,
            "generate_audio": video_config.generate_audio,
            "watermark": video_config.watermark,
            "save_dir": video_config.save_dir,
        },
        "screenshot": {
            "enabled": config.tools.screenshot.enable,
            "max_width": config.tools.screenshot.max_width,
            "max_height": config.tools.screenshot.max_height,
            "quality": config.tools.screenshot.quality,
            "vision_model": defaults.vision_model,
            "vision_model_override": defaults.vision_model_override,
            "vision_model_configured": bool(
                defaults.vision_model
                and (
                    defaults.vision_model in config.model_presets
                    or getattr(config.providers, defaults.vision_model, None) is not None
                )
            ),
            "resolved_model": (
                (defaults.vision_model_override or "").strip()
                or (
                    config.model_presets[defaults.vision_model].model
                    if defaults.vision_model
                    and defaults.vision_model in config.model_presets
                    else None
                )
            ),
        },
        "system_io": {
            "enabled": config.tools.system_io.enable,
            "allow_actions": list(config.tools.system_io.allow_actions),
            "available_actions": _SYSTEM_IO_ACTIONS,
        },
        "transcription": {
            "enabled": transcription.enabled,
            "provider": transcription.provider,
            "provider_configured": transcription.configured,
            "model": transcription.model,
            "language": transcription.language,
            "max_duration_sec": transcription.max_duration_sec,
            "max_upload_mb": transcription.max_upload_mb,
        },
        "tts": {
            "enabled": tts.enabled,
            "provider": tts.provider,
            "provider_configured": tts.configured,
            "model": tts.model,
            "voice": tts.voice,
            "rate": tts.rate,
            "save_dir": tts.save_dir,
        },
        "runtime": {
            "config_path": str(get_config_path().expanduser()),
            "workspace_path": str(config.workspace_path),
            "gateway_host": config.gateway.host,
            "gateway_port": config.gateway.port,
            "heartbeat": {
                "enabled": config.gateway.heartbeat.enabled,
                "interval_s": config.gateway.heartbeat.interval_s,
                "keep_recent_messages": config.gateway.heartbeat.keep_recent_messages,
            },
            "dream": {
                "schedule": defaults.dream.describe_schedule(),
            },
            "unified_session": defaults.unified_session,
        },
        "usage": token_usage_payload(timezone_name=defaults.timezone),
        "advanced": {
            "restrict_to_workspace": config.tools.restrict_to_workspace,
            "workspace_sandbox": sandbox_status.as_dict(),
            "webui_allow_local_service_access": config.tools.webui_allow_local_service_access,
            "allow_local_preview_access": config.tools.webui_allow_local_service_access,
            "webui_default_access_mode": read_webui_default_access_mode(),
            "private_service_protection_enabled": True,
            "ssrf_whitelist_count": len(config.tools.ssrf_whitelist),
            "guard_level": config.tools.guard_level,
            "cold_storage_days": config.tools.cold_storage_days,
            "duplicate_similarity_threshold": config.tools.duplicate_similarity_threshold,
            "mcp_server_count": len(config.tools.mcp_servers),
            "exec_enabled": exec_config.enable,
            "exec_sandbox": exec_config.sandbox or None,
            "exec_path_prepend_set": bool(exec_config.path_prepend),
            "exec_path_append_set": bool(exec_config.path_append),
        },
        "requires_restart": requires_restart,
        "version": _version_payload(),
    }
    return decorate_settings_payload(
        payload,
        surface=surface,
        runtime_capability_overrides=runtime_capability_overrides,
        restart_required_sections=restart_required_sections,
        apply_state=apply_state,
    )


def settings_usage_payload() -> dict[str, Any]:
    """Return the lightweight token usage slice for Overview refreshes."""
    config = load_config()
    return token_usage_payload(timezone_name=config.agents.defaults.timezone)


def update_agent_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    defaults = config.agents.defaults
    changed = False
    restart_required = False

    if "model_preset" in query or "modelPreset" in query:
        preset = (_query_first_alias(query, "model_preset", "modelPreset") or "").strip()
        preset_value = None if not preset or preset == "default" else preset
        if preset_value is not None and preset_value not in config.model_presets:
            raise WebUISettingsError("unknown model preset")
        if defaults.model_preset != preset_value:
            defaults.model_preset = preset_value
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        if defaults.model != model:
            defaults.model = model
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip()
        if not provider:
            raise WebUISettingsError("provider is required")
        _validate_configured_provider(config, provider)
        if defaults.provider != provider:
            defaults.provider = provider
            changed = True

    context_window_tokens = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )
    if (
        context_window_tokens is not None
        and defaults.context_window_tokens != context_window_tokens
    ):
        defaults.context_window_tokens = context_window_tokens
        changed = True

    timezone = _query_first(query, "timezone")
    if timezone is not None:
        timezone = timezone.strip()
        if not timezone:
            raise WebUISettingsError("timezone is required")
        try:
            ZoneInfo(timezone)
        except Exception:
            raise WebUISettingsError("invalid timezone") from None
        if defaults.timezone != timezone:
            defaults.timezone = timezone
            changed = True
            restart_required = True

    bot_name = _query_first_alias(query, "bot_name", "botName")
    if bot_name is not None:
        bot_name = bot_name.strip()
        if not bot_name:
            raise WebUISettingsError("bot_name is required")
        if defaults.bot_name != bot_name:
            defaults.bot_name = bot_name
            changed = True
            restart_required = True

    bot_icon = _query_first_alias(query, "bot_icon", "botIcon")
    if bot_icon is not None:
        bot_icon = bot_icon.strip()
        if defaults.bot_icon != bot_icon:
            defaults.bot_icon = bot_icon
            changed = True
            restart_required = True

    tool_hint_max_length = _query_first_alias(
        query,
        "tool_hint_max_length",
        "toolHintMaxLength",
    )
    if tool_hint_max_length is not None:
        try:
            parsed = int(tool_hint_max_length)
        except ValueError:
            raise WebUISettingsError("tool_hint_max_length must be an integer") from None
        if parsed < 20 or parsed > 500:
            raise WebUISettingsError("tool_hint_max_length must be between 20 and 500")
        if defaults.tool_hint_max_length != parsed:
            defaults.tool_hint_max_length = parsed
            changed = True
            restart_required = True

    if changed:
        save_config(config)
    return settings_payload(requires_restart=restart_required)


def create_model_configuration(query: QueryParams) -> dict[str, Any]:
    label = (_query_first_alias(query, "label", "displayName") or "").strip()
    raw_name = (_query_first(query, "name") or label).strip()
    model = (_query_first(query, "model") or "").strip()
    provider = (_query_first(query, "provider") or "").strip()

    if not label:
        label = raw_name
    if not model:
        raise WebUISettingsError("model is required")
    if not provider:
        raise WebUISettingsError("provider is required")

    name = _model_configuration_slug(raw_name or label)
    config = load_config()
    if name in config.model_presets:
        raise WebUISettingsError("configuration already exists", status=409)
    _validate_configured_provider(config, provider)

    base = config.resolve_default_preset()
    config.model_presets[name] = ModelPresetConfig(
        label=label,
        model=model,
        provider=provider,
        max_tokens=base.max_tokens,
        context_window_tokens=base.context_window_tokens,
        temperature=base.temperature,
        reasoning_effort=base.reasoning_effort,
    )
    config.agents.defaults.model_preset = name
    save_config(config)
    return settings_payload()


def update_model_configuration(query: QueryParams) -> dict[str, Any]:
    name = (_query_first(query, "name") or "").strip()
    if not name or name == "default":
        raise WebUISettingsError("model configuration is required")

    config = load_config()
    preset = config.model_presets.get(name)
    if preset is None:
        raise WebUISettingsError("unknown model configuration")

    changed = False
    label = _query_first_alias(query, "label", "displayName")
    if label is not None:
        label = label.strip()
        if not label:
            raise WebUISettingsError("label is required")
        if preset.label != label:
            preset.label = label
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("model is required")
        if preset.model != model:
            preset.model = model
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip()
        if not provider:
            raise WebUISettingsError("provider is required")
        _validate_configured_provider(config, provider)
        if preset.provider != provider:
            preset.provider = provider
            changed = True

    context_window_tokens = _parse_context_window_tokens(
        _query_first_alias(query, "context_window_tokens", "contextWindowTokens")
    )
    if (
        context_window_tokens is not None
        and preset.context_window_tokens != context_window_tokens
    ):
        preset.context_window_tokens = context_window_tokens
        changed = True

    if config.agents.defaults.model_preset != name:
        config.agents.defaults.model_preset = name
        changed = True

    if changed:
        save_config(config)
    return settings_payload()


def update_provider_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")

    config = load_config()
    resolved_provider = _resolve_settings_provider(config, provider_name)
    if resolved_provider is None:
        # 非 LLM 能力厂商（volcengine / groq / edge-tts 等）也允许在「模型厂商」页编辑
        resolved_provider = _resolve_model_list_provider(config, provider_name)
    if resolved_provider is None:
        raise WebUISettingsError("unknown provider")
    spec, provider_key, provider_config = resolved_provider

    # 动态 / 能力厂商首次配置时需挂到 model_extra，否则 save_config 不会持久化
    if find_by_name(provider_key) is None and provider_key not in (
        config.providers.model_extra or {}
    ):
        config.providers.model_extra[provider_key] = provider_config

    changed = False
    if "api_key" in query or "apiKey" in query:
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = (api_key or "").strip() or None
        if provider_config.api_key != api_key:
            provider_config.api_key = api_key
            changed = True

    if "api_base" in query or "apiBase" in query:
        api_base = _query_first_alias(query, "api_base", "apiBase")
        api_base = (api_base or "").strip() or None
        if provider_config.api_base != api_base:
            provider_config.api_base = api_base
            changed = True

    if "api_type" in query:
        if spec.name == "openai":
            api_type = (_query_first(query, "api_type") or "").strip()
            try:
                parsed_api_type = type(provider_config)(api_type=api_type).api_type
            except Exception:
                raise WebUISettingsError("api_type must be auto, chat_completions, or responses") from None
            if provider_config.api_type != parsed_api_type:
                provider_config.api_type = parsed_api_type
                changed = True

    if "capabilities" in query:
        raw = _query_first(query, "capabilities") or ""
        requested = [c.strip().lower() for c in raw.split(",") if c.strip()]
        invalid = [c for c in requested if c not in _CAPABILITY_KEYS]
        if invalid:
            raise WebUISettingsError(
                "capabilities must be one of: " + ", ".join(_CAPABILITY_KEYS)
            )
        # 去重并保持稳定顺序
        seen: list[str] = []
        for c in requested:
            if c not in seen:
                seen.append(c)
        if provider_config.capabilities != seen:
            provider_config.capabilities = seen
            changed = True

    if changed:
        save_config(config)
    image_config = config.tools.image_generation
    restart_required = (
        changed
        and image_config.enabled
        and image_config.provider == provider_key
        and get_image_gen_provider(provider_key) is not None
    )
    return settings_payload(requires_restart=restart_required)


def delete_provider_settings(query: QueryParams) -> dict[str, Any]:
    """删除「模型厂商」页中的厂商。

    固定字段厂商（openai / anthropic / deepseek 等内置项）不可删除。其余厂商分两类：
      - 能力专用厂商（volcengine / gemini / edge-tts / groq / aihubmix 等注册表项）：
        加入 ``hidden_providers`` 从列表隐藏，并清除其保存在 model_extra 的密钥配置。
      - 动态自定义厂商（model_extra 项）：从 model_extra 移除并隐藏。
    删除时同步回退所有指向该厂商的引用（LLM 默认/预设、图像/视频/TTS/转写 provider），避免悬空。
    """
    provider_name = (_query_first(query, "provider") or "").strip()
    if not provider_name:
        raise WebUISettingsError("provider is required")

    config = load_config()
    if find_by_name(provider_name) is not None:
        raise WebUISettingsError("provider cannot be deleted")

    normalized = provider_name.replace("-", "_")

    # 1) 清除 model_extra 里的密钥配置（若存在）
    model_extra = config.providers.model_extra or {}
    key = next(
        (
            extra_name
            for extra_name in model_extra
            if extra_name == provider_name
            or extra_name.replace("-", "_") == normalized
        ),
        None,
    )
    if key is not None:
        del model_extra[key]

    # 2) 加入隐藏列表，避免注册表驱动厂商重新出现
    hidden = config.hidden_providers or []
    if normalized not in {str(name).replace("-", "_") for name in hidden}:
        hidden.append(provider_name)
        config.hidden_providers = hidden

    # 3) 回退各处引用，避免删除后留下悬空 provider
    defaults = config.agents.defaults
    if defaults.provider.replace("-", "_") == normalized:
        defaults.provider = "auto"
    if defaults.vision_model and defaults.vision_model.replace("-", "_") == normalized:
        defaults.vision_model = None
    for preset in config.model_presets.values():
        if preset.provider.replace("-", "_") == normalized:
            preset.provider = "auto"
    if config.tools.image_generation.provider.replace("-", "_") == normalized:
        config.tools.image_generation.provider = "volcengine"
    if config.tools.seedance_video.provider.replace("-", "_") == normalized:
        config.tools.seedance_video.provider = "volcengine"
    if config.transcription.provider and config.transcription.provider.replace("-", "_") == normalized:
        config.transcription.provider = None
    if config.tts.provider and config.tts.provider.replace("-", "_") == normalized:
        config.tts.provider = None

    save_config(config)
    return settings_payload()


def update_network_safety_settings(query: QueryParams) -> dict[str, Any]:
    raw_allow = (
        _query_first_alias(query, "webui_allow_local_service_access", "webuiAllowLocalServiceAccess")
        or _query_first_alias(query, "allow_local_preview_access", "allowLocalPreviewAccess")
    )
    raw_default_access_mode = _query_first_alias(query, "webui_default_access_mode", "webuiDefaultAccessMode")
    raw_guard_level = _query_first_alias(query, "guard_level", "guardLevel")
    raw_cold_storage_days = _query_first_alias(query, "cold_storage_days", "coldStorageDays")
    raw_dup_threshold = _query_first_alias(query, "duplicate_similarity_threshold", "duplicateSimilarityThreshold")
    if (
        raw_allow is None
        and raw_default_access_mode is None
        and raw_guard_level is None
        and raw_cold_storage_days is None
        and raw_dup_threshold is None
    ):
        raise WebUISettingsError(
            "webui_allow_local_service_access, webui_default_access_mode, guard_level, "
            "cold_storage_days, or duplicate_similarity_threshold is required"
        )

    config = load_config()
    changed = False
    if raw_allow is not None:
        webui_allow_local_service_access = _parse_bool(raw_allow, "webui_allow_local_service_access")
        if config.tools.webui_allow_local_service_access != webui_allow_local_service_access:
            config.tools.webui_allow_local_service_access = webui_allow_local_service_access
            changed = True

    if raw_guard_level is not None:
        from biscuitbot.security.guard_level import normalize_guard_level
        guard_level = normalize_guard_level(raw_guard_level)
        if config.tools.guard_level != guard_level:
            config.tools.guard_level = guard_level
            changed = True

    if raw_cold_storage_days is not None:
        try:
            cold_storage_days = int(raw_cold_storage_days)
        except (TypeError, ValueError):
            raise WebUISettingsError("cold_storage_days must be an integer")
        if not (0 <= cold_storage_days <= 365):
            raise WebUISettingsError("cold_storage_days must be between 0 and 365")
        if config.tools.cold_storage_days != cold_storage_days:
            config.tools.cold_storage_days = cold_storage_days
            changed = True

    if raw_dup_threshold is not None:
        try:
            dup_threshold = float(raw_dup_threshold)
        except (TypeError, ValueError):
            raise WebUISettingsError("duplicate_similarity_threshold must be a number")
        if not (0.0 <= dup_threshold <= 1.0):
            raise WebUISettingsError("duplicate_similarity_threshold must be between 0.0 and 1.0")
        if config.tools.duplicate_similarity_threshold != dup_threshold:
            config.tools.duplicate_similarity_threshold = dup_threshold
            changed = True

    if changed:
        save_config(config)
    if raw_default_access_mode is not None:
        default_access_mode = raw_default_access_mode.strip().lower()
        if default_access_mode == "restricted":
            default_access_mode = "default"
        if default_access_mode not in {"default", "full"}:
            raise WebUISettingsError("webui_default_access_mode must be default or full")
        try:
            write_webui_default_access_mode(default_access_mode)
        except ValueError as exc:
            raise WebUISettingsError(str(exc)) from exc
    return settings_payload(requires_restart=changed)


def update_web_search_settings(query: QueryParams) -> dict[str, Any]:
    provider_name = (_query_first(query, "provider") or "").strip().lower()
    provider_option = _WEB_SEARCH_PROVIDER_BY_NAME.get(provider_name)
    if provider_option is None:
        raise WebUISettingsError("unknown web search provider")

    config = load_config()
    search_config = config.tools.web.search
    web_config = config.tools.web
    previous_provider = search_config.provider
    changed = False
    restart_required = False

    def set_search_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(search_config, attr) != value:
            setattr(search_config, attr, value)
            changed = True

    def set_fetch_value(attr: str, value: object) -> None:
        nonlocal changed
        if getattr(web_config.fetch, attr) != value:
            setattr(web_config.fetch, attr, value)
            changed = True

    if search_config.provider != provider_name:
        search_config.provider = provider_name
        changed = True

    credential = provider_option["credential"]
    if credential == "none":
        set_search_value("api_key", "")
        set_search_value("base_url", "")
    elif credential == "base_url":
        base_url = _query_first_alias(query, "base_url", "baseUrl")
        base_url = base_url.strip() if base_url is not None else None
        if not base_url and previous_provider == provider_name and search_config.base_url:
            base_url = search_config.base_url
        if not base_url:
            raise WebUISettingsError("base_url is required")
        set_search_value("base_url", base_url)
        set_search_value("api_key", "")
    else:
        api_key = _query_first_alias(query, "api_key", "apiKey")
        api_key = api_key.strip() if api_key is not None else None
        if not api_key and previous_provider == provider_name and search_config.api_key:
            api_key = search_config.api_key
        if not api_key:
            raise WebUISettingsError("api_key is required")
        set_search_value("api_key", api_key)
        set_search_value("base_url", "")

    max_results = _query_first_alias(query, "max_results", "maxResults")
    if max_results is not None:
        try:
            parsed = int(max_results)
        except ValueError:
            raise WebUISettingsError("max_results must be an integer") from None
        if parsed < 1 or parsed > 10:
            raise WebUISettingsError("max_results must be between 1 and 10")
        set_search_value("max_results", parsed)

    timeout = _query_first(query, "timeout")
    if timeout is not None:
        try:
            parsed_timeout = int(timeout)
        except ValueError:
            raise WebUISettingsError("timeout must be an integer") from None
        if parsed_timeout < 1 or parsed_timeout > 120:
            raise WebUISettingsError("timeout must be between 1 and 120")
        set_search_value("timeout", parsed_timeout)

    use_jina_reader = _query_first_alias(query, "use_jina_reader", "useJinaReader")
    if use_jina_reader is not None:
        normalized = use_jina_reader.strip().lower()
        if normalized not in {"1", "0", "true", "false", "yes", "no"}:
            raise WebUISettingsError("use_jina_reader must be boolean")
        previous_jina_reader = web_config.fetch.use_jina_reader
        set_fetch_value("use_jina_reader", normalized in {"1", "true", "yes"})
        if web_config.fetch.use_jina_reader != previous_jina_reader:
            restart_required = True

    if changed:
        save_config(config)
    return settings_payload(requires_restart=restart_required)


def update_image_generation_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    image_config = config.tools.image_generation
    changed = False

    provider_name = _query_first(query, "provider")
    if provider_name is not None:
        provider_name = provider_name.strip().lower()
        if not provider_name:
            raise WebUISettingsError("image generation provider is required")
        if get_image_gen_provider(provider_name) is None:
            raise WebUISettingsError("unknown image generation provider")
        if image_config.provider != provider_name:
            image_config.provider = provider_name
            changed = True

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if image_config.enabled != parsed_enabled:
            image_config.enabled = parsed_enabled
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("image generation model is required")
        if len(model) > 200:
            raise WebUISettingsError("image generation model is too long")
        if image_config.model != model:
            image_config.model = model
            changed = True

    default_aspect_ratio = _query_first_alias(
        query,
        "default_aspect_ratio",
        "defaultAspectRatio",
    )
    if default_aspect_ratio is not None:
        default_aspect_ratio = default_aspect_ratio.strip()
        if default_aspect_ratio not in _IMAGE_GENERATION_ASPECT_RATIOS:
            raise WebUISettingsError("unsupported image generation aspect ratio")
        if image_config.default_aspect_ratio != default_aspect_ratio:
            image_config.default_aspect_ratio = default_aspect_ratio
            changed = True

    default_image_size = _query_first_alias(
        query,
        "default_image_size",
        "defaultImageSize",
    )
    if default_image_size is not None:
        default_image_size = default_image_size.strip()
        if not default_image_size:
            raise WebUISettingsError("default image size is required")
        if len(default_image_size) > 32 or not all(
            char.isascii() and (char.isalnum() or char in {"x", "X", ":", "-", "_"})
            for char in default_image_size
        ):
            raise WebUISettingsError("unsupported image generation size")
        if image_config.default_image_size != default_image_size:
            image_config.default_image_size = default_image_size
            changed = True

    max_images_per_turn = _query_first_alias(
        query,
        "max_images_per_turn",
        "maxImagesPerTurn",
    )
    if max_images_per_turn is not None:
        try:
            parsed_max = int(max_images_per_turn)
        except ValueError:
            raise WebUISettingsError("max_images_per_turn must be an integer") from None
        if parsed_max < 1 or parsed_max > 8:
            raise WebUISettingsError("max_images_per_turn must be between 1 and 8")
        if image_config.max_images_per_turn != parsed_max:
            image_config.max_images_per_turn = parsed_max
            changed = True

    if image_config.enabled:
        selected_provider = next(
            (
                provider
                for provider in _unified_provider_rows(config)
                if provider["name"] == image_config.provider
            ),
            None,
        )
        if not selected_provider or not selected_provider["configured"]:
            raise WebUISettingsError("image generation provider is not configured")

    if changed:
        save_config(config)
    return settings_payload(requires_restart=changed)


def update_video_generation_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    video_config = config.tools.seedance_video
    changed = False

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if video_config.enabled != parsed_enabled:
            video_config.enabled = parsed_enabled
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip()
        if not model:
            raise WebUISettingsError("video generation model is required")
        if len(model) > 200:
            raise WebUISettingsError("video generation model is too long")
        if video_config.model != model:
            video_config.model = model
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip().lower()
        if not provider:
            raise WebUISettingsError("video generation provider is required")
        if len(provider) > 64:
            raise WebUISettingsError("video generation provider is too long")
        if video_config.provider != provider:
            video_config.provider = provider
            changed = True

    default_ratio = _query_first_alias(query, "default_ratio", "defaultRatio")
    if default_ratio is not None:
        default_ratio = default_ratio.strip()
        if default_ratio not in _VIDEO_RATIO_OPTIONS:
            raise WebUISettingsError("unsupported video generation aspect ratio")
        if video_config.default_ratio != default_ratio:
            video_config.default_ratio = default_ratio
            changed = True

    default_duration = _query_first_alias(query, "default_duration", "defaultDuration")
    if default_duration is not None:
        try:
            parsed_duration = int(default_duration)
        except ValueError:
            raise WebUISettingsError("default_duration must be an integer") from None
        if parsed_duration < _VIDEO_DURATION_MIN or parsed_duration > _VIDEO_DURATION_MAX:
            raise WebUISettingsError("default_duration must be between 4 and 30")
        if video_config.default_duration != parsed_duration:
            video_config.default_duration = parsed_duration
            changed = True

    default_resolution = _query_first_alias(query, "default_resolution", "defaultResolution")
    if default_resolution is not None:
        default_resolution = default_resolution.strip()
        if default_resolution:
            if default_resolution not in _VIDEO_RESOLUTION_OPTIONS:
                raise WebUISettingsError("unsupported video generation resolution")
            parsed_resolution = default_resolution
        else:
            # 空值 = 模型自动决定（对应 default_resolution=None）。
            parsed_resolution = None
        if video_config.default_resolution != parsed_resolution:
            video_config.default_resolution = parsed_resolution
            changed = True

    generate_audio = _query_first_alias(query, "generate_audio", "generateAudio")
    if generate_audio is not None:
        parsed_audio = _parse_bool(generate_audio, "generate_audio")
        if video_config.generate_audio != parsed_audio:
            video_config.generate_audio = parsed_audio
            changed = True

    watermark = _query_first_alias(query, "watermark", "watermark")
    if watermark is not None:
        parsed_watermark = _parse_bool(watermark, "watermark")
        if video_config.watermark != parsed_watermark:
            video_config.watermark = parsed_watermark
            changed = True

    save_dir = _query_first_alias(query, "save_dir", "saveDir")
    if save_dir is not None:
        save_dir = save_dir.strip()
        if not save_dir:
            raise WebUISettingsError("video generation save dir is required")
        if len(save_dir) > 200:
            raise WebUISettingsError("video generation save dir is too long")
        if video_config.save_dir != save_dir:
            video_config.save_dir = save_dir
            changed = True

    if video_config.enabled:
        volcengine_cfg = _provider_config_by_name(config, "volcengine")
        has_key = bool(
            (video_config.api_key or "").strip()
            or (volcengine_cfg and (volcengine_cfg.api_key or "").strip())
            or os.environ.get("ARK_API_KEY", "").strip()
        )
        if not has_key:
            raise WebUISettingsError("seedance api key is required to enable video generation")

    if changed:
        save_config(config)
    return settings_payload(requires_restart=changed)


def update_screenshot_settings(query: QueryParams) -> dict[str, Any]:
    """Update screenshot tool configuration."""
    config = load_config()
    screenshot_config = config.tools.screenshot
    defaults = config.agents.defaults
    changed = False

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if screenshot_config.enable != parsed_enabled:
            screenshot_config.enable = parsed_enabled
            changed = True

    vision_model = _query_first_alias(query, "vision_model", "visionModel")
    if vision_model is not None:
        vision_model = vision_model.strip()
        if vision_model:
            is_preset = vision_model in config.model_presets
            is_provider = getattr(config.providers, vision_model, None) is not None
            if not is_preset and not is_provider:
                raise WebUISettingsError(
                    f"vision model '{vision_model}' is not a known preset or provider"
                )
        if defaults.vision_model != (vision_model or None):
            defaults.vision_model = vision_model or None
            changed = True

    vision_model_override = _query_first_alias(
        query, "vision_model_override", "visionModelOverride"
    )
    if vision_model_override is not None:
        override = vision_model_override.strip()
        if defaults.vision_model_override != (override or None):
            defaults.vision_model_override = override or None
            changed = True

    max_width = _query_first_alias(query, "max_width", "maxWidth")
    if max_width is not None:
        try:
            parsed_width = int(max_width)
        except ValueError:
            raise WebUISettingsError("max_width must be an integer") from None
        if parsed_width < 320 or parsed_width > 7680:
            raise WebUISettingsError("max_width must be between 320 and 7680")
        if screenshot_config.max_width != parsed_width:
            screenshot_config.max_width = parsed_width
            changed = True

    max_height = _query_first_alias(query, "max_height", "maxHeight")
    if max_height is not None:
        try:
            parsed_height = int(max_height)
        except ValueError:
            raise WebUISettingsError("max_height must be an integer") from None
        if parsed_height < 240 or parsed_height > 4320:
            raise WebUISettingsError("max_height must be between 240 and 4320")
        if screenshot_config.max_height != parsed_height:
            screenshot_config.max_height = parsed_height
            changed = True

    quality = _query_first(query, "quality")
    if quality is not None:
        try:
            parsed_quality = int(quality)
        except ValueError:
            raise WebUISettingsError("quality must be an integer") from None
        if parsed_quality < 10 or parsed_quality > 100:
            raise WebUISettingsError("quality must be between 10 and 100")
        if screenshot_config.quality != parsed_quality:
            screenshot_config.quality = parsed_quality
            changed = True

    if screenshot_config.enable:
        if not defaults.vision_model:
            raise WebUISettingsError(
                "vision model must be configured to enable screenshot"
            )
        is_preset = defaults.vision_model in config.model_presets
        is_provider = getattr(config.providers, defaults.vision_model, None) is not None
        if not is_preset and not is_provider:
            raise WebUISettingsError(
                f"vision model '{defaults.vision_model}' is not a known preset or provider"
            )

    if changed:
        save_config(config)
    return settings_payload(requires_restart=changed)


def update_transcription_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    transcription = config.transcription
    changed = False

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if transcription.enabled != parsed_enabled:
            transcription.enabled = parsed_enabled
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip().lower()
        provider_spec = resolve_transcription_provider(provider)
        if provider_spec is None:
            raise WebUISettingsError("unknown transcription provider")
        provider = provider_spec.name
        if transcription.provider != provider:
            transcription.provider = provider
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip() or None
        if model is not None and len(model) > 200:
            raise WebUISettingsError("transcription model is too long")
        if transcription.model != model:
            transcription.model = model
            changed = True

    language = _query_first(query, "language")
    if language is not None:
        language = language.strip().lower() or None
        if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
            raise WebUISettingsError("transcription language must be 2-3 lowercase letters")
        if transcription.language != language:
            transcription.language = language
            changed = True

    max_duration_sec = _query_first_alias(query, "max_duration_sec", "maxDurationSec")
    if max_duration_sec is not None:
        try:
            parsed_duration = int(max_duration_sec)
        except ValueError:
            raise WebUISettingsError("max_duration_sec must be an integer") from None
        if parsed_duration < 1 or parsed_duration > 600:
            raise WebUISettingsError("max_duration_sec must be between 1 and 600")
        if transcription.max_duration_sec != parsed_duration:
            transcription.max_duration_sec = parsed_duration
            changed = True

    max_upload_mb = _query_first_alias(query, "max_upload_mb", "maxUploadMb")
    if max_upload_mb is not None:
        try:
            parsed_upload = int(max_upload_mb)
        except ValueError:
            raise WebUISettingsError("max_upload_mb must be an integer") from None
        if parsed_upload < 1 or parsed_upload > 100:
            raise WebUISettingsError("max_upload_mb must be between 1 and 100")
        if transcription.max_upload_mb != parsed_upload:
            transcription.max_upload_mb = parsed_upload
            changed = True

    if changed:
        save_config(config)
    return settings_payload()


def update_tts_settings(query: QueryParams) -> dict[str, Any]:
    config = load_config()
    tts = config.tts
    changed = False

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if tts.enabled != parsed_enabled:
            tts.enabled = parsed_enabled
            changed = True

    provider = _query_first(query, "provider")
    if provider is not None:
        provider = provider.strip().lower()
        try:
            provider_spec = resolve_tts_provider(provider)
        except ValueError as exc:
            raise WebUISettingsError(str(exc)) from exc
        provider = provider_spec.name
        if tts.provider != provider:
            tts.provider = provider
            changed = True

    model = _query_first(query, "model")
    if model is not None:
        model = model.strip() or None
        if model is not None and len(model) > 200:
            raise WebUISettingsError("tts model is too long")
        if tts.model != model:
            tts.model = model
            changed = True

    voice = _query_first(query, "voice")
    if voice is not None:
        voice = voice.strip() or None
        if voice is not None and len(voice) > 200:
            raise WebUISettingsError("tts voice is too long")
        if tts.voice != voice:
            tts.voice = voice
            changed = True

    rate = _query_first(query, "rate")
    if rate is not None:
        rate = rate.strip() or None
        if rate is not None and len(rate) > 32:
            raise WebUISettingsError("tts rate is too long")
        if tts.rate != rate:
            tts.rate = rate
            changed = True

    if changed:
        save_config(config)
    return settings_payload()


def update_system_io_settings(query: QueryParams) -> dict[str, Any]:
    """Update system-level IO tool configuration (enable + action allowlist)."""
    config = load_config()
    system_io_config = config.tools.system_io
    changed = False

    enabled = _query_first(query, "enabled")
    if enabled is not None:
        parsed_enabled = _parse_bool(enabled, "enabled")
        if system_io_config.enable != parsed_enabled:
            system_io_config.enable = parsed_enabled
            changed = True

    allow_actions_raw = _query_first_alias(query, "allow_actions", "allowActions")
    if allow_actions_raw is not None:
        actions = [a.strip() for a in allow_actions_raw.split(",") if a.strip()]
        for action in actions:
            if action not in _SYSTEM_IO_VALID_ACTIONS:
                raise WebUISettingsError(f"unknown system_io action '{action}'")
        if sorted(system_io_config.allow_actions) != sorted(actions):
            system_io_config.allow_actions = actions
            changed = True

    if changed:
        save_config(config)
    return settings_payload(requires_restart=changed)


def knowledge_documents_payload() -> dict[str, Any]:
    """列出知识库文档（由 WebUI 设置页「知识库」区使用）。"""
    config = load_config()
    return {"documents": list_documents(config.workspace_path)}


def delete_knowledge_document(query: QueryParams) -> dict[str, Any]:
    """删除知识库中的一个文档，返回更新后的文档列表。"""
    name = (_query_first(query, "name") or "").strip()
    if not name:
        raise WebUISettingsError("name is required")
    config = load_config()
    delete_document(config.workspace_path, name)
    return {"documents": list_documents(config.workspace_path)}
