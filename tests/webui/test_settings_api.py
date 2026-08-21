from __future__ import annotations

import json

import httpx
import pytest

from biscuitbot.config.loader import load_config, save_config
from biscuitbot.config.schema import Config, ModelPresetConfig
from biscuitbot.webui.settings_api import (
    WebUISettingsError,
    _provider_capabilities,
    _resolve_model_list_provider,
    _unified_provider_rows,
    channels_payload,
    create_model_configuration,
    delete_provider_settings,
    provider_models_payload,
    settings_payload,
    settings_usage_payload,
    update_agent_settings,
    update_channel_settings,
    update_image_generation_settings,
    update_model_configuration,
    update_network_safety_settings,
    update_provider_settings,
    update_system_io_settings,
    update_transcription_settings,
    update_tts_settings,
)

DYNAMIC_PROVIDER_NAME = "my-company-api"
DYNAMIC_PROVIDER_API_BASE = "https://example.test/v1"


def _dynamic_provider_config(
    *,
    api_base: str = DYNAMIC_PROVIDER_API_BASE,
    defaults: bool = False,
) -> Config:
    raw_config = {
        "providers": {
            DYNAMIC_PROVIDER_NAME: {
                "apiBase": api_base,
            }
        }
    }
    if defaults:
        raw_config["agents"] = {
            "defaults": {
                "provider": DYNAMIC_PROVIDER_NAME,
                "model": "gpt-4o-mini",
            }
        }
    return Config.model_validate(raw_config)


def test_create_model_configuration_writes_label_and_selects(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.agents.defaults.provider = "openai"
    config.providers.openai.api_key = "sk-test"
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = create_model_configuration(
        {
            "label": ["Fast writing"],
            "provider": ["openai"],
            "model": ["openai/gpt-4.1-mini"],
        }
    )

    assert payload["agent"]["model_preset"] == "fast-writing"
    assert payload["agent"]["model"] == "openai/gpt-4.1-mini"
    rows = {row["name"]: row for row in payload["model_presets"]}
    assert rows["fast-writing"]["label"] == "Fast writing"

    saved = load_config(config_path)
    assert saved.agents.defaults.model_preset == "fast-writing"
    assert saved.model_presets["fast-writing"].label == "Fast writing"
    assert saved.model_presets["fast-writing"].model == "openai/gpt-4.1-mini"
    assert saved.model_presets["fast-writing"].provider == "openai"

    with pytest.raises(WebUISettingsError) as duplicate:
        create_model_configuration(
            {
                "label": ["Fast writing"],
                "provider": ["openai"],
                "model": ["openai/gpt-4.1-mini"],
            }
        )
    assert duplicate.value.status == 409


def test_create_model_configuration_accepts_dynamic_custom_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = create_model_configuration(
        {
            "label": ["Tenant model"],
            "provider": [DYNAMIC_PROVIDER_NAME],
            "model": ["gpt-4o-mini"],
        }
    )

    assert payload["agent"]["model_preset"] == "tenant-model"
    assert payload["agent"]["provider"] == DYNAMIC_PROVIDER_NAME
    saved = load_config(config_path)
    assert saved.model_presets["tenant-model"].provider == DYNAMIC_PROVIDER_NAME
    assert saved.model_presets["tenant-model"].model == "gpt-4o-mini"


def test_create_model_configuration_rejects_dynamic_custom_provider_without_api_base(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({
        "providers": {
            DYNAMIC_PROVIDER_NAME: {
                "apiKey": "sk-test",
            }
        }
    })
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="provider is not configured"):
        create_model_configuration(
            {
                "label": ["Tenant model"],
                "provider": [DYNAMIC_PROVIDER_NAME],
                "model": ["gpt-4o-mini"],
            }
        )


def test_create_model_configuration_rejects_unconfigured_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="provider is not configured"):
        create_model_configuration(
            {
                "label": ["Deep"],
                "provider": ["openai"],
                "model": ["openai/gpt-4.1"],
            }
        )


def test_update_model_configuration_edits_named_preset_and_selects(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.providers.openai.api_key = "sk-test"
    config.model_presets["codex"] = ModelPresetConfig(
        label="Old Codex",
        provider="openai",
        model="openai/gpt-4.1",
    )
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_model_configuration(
        {
            "name": ["codex"],
            "label": ["Codex"],
            "provider": ["openai"],
            "model": ["openai/gpt-4.1"],
        }
    )

    assert payload["agent"]["model_preset"] == "codex"
    assert payload["agent"]["model"] == "openai/gpt-4.1"
    saved = load_config(config_path)
    assert saved.agents.defaults.model_preset == "codex"
    assert saved.model_presets["codex"].label == "Codex"
    assert saved.model_presets["codex"].provider == "openai"
    assert saved.model_presets["codex"].model == "openai/gpt-4.1"


def test_update_provider_settings_updates_dynamic_custom_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(api_base="https://old.example/v1"), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_provider_settings(
        {
            "provider": [DYNAMIC_PROVIDER_NAME],
            "apiBase": ["https://new.example/v1"],
            "apiKey": ["sk-test"],
        }
    )

    providers = {row["name"]: row for row in payload["providers"]}
    assert providers[DYNAMIC_PROVIDER_NAME]["api_base"] == "https://new.example/v1"
    assert providers[DYNAMIC_PROVIDER_NAME]["api_key_hint"] == "••••"
    saved = load_config(config_path)
    dynamic_provider = saved.providers.model_extra[DYNAMIC_PROVIDER_NAME]
    assert dynamic_provider.api_base == "https://new.example/v1"
    assert dynamic_provider.api_key == "sk-test"


def test_update_provider_settings_persists_capabilities(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    update_provider_settings(
        {
            "provider": [DYNAMIC_PROVIDER_NAME],
            "capabilities": ["llm,vision,image"],
        }
    )

    saved = load_config(config_path)
    dynamic_provider = saved.providers.model_extra[DYNAMIC_PROVIDER_NAME]
    assert dynamic_provider.capabilities == ["llm", "vision", "image"]

    payload = settings_payload()
    providers = {row["name"]: row for row in payload["providers"]}
    assert providers[DYNAMIC_PROVIDER_NAME]["capabilities"] == ["llm", "vision", "image"]


def test_provider_capabilities_override_narrows_llm(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户把厂商能力收窄为仅图像时，该厂商应退出 LLM 模型预设下拉。"""
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    update_provider_settings(
        {
            "provider": [DYNAMIC_PROVIDER_NAME],
            "capabilities": ["image"],
        }
    )

    rows = _unified_provider_rows(load_config(config_path))
    row = next(r for r in rows if r["name"] == DYNAMIC_PROVIDER_NAME)
    assert row["capabilities"] == ["image"]
    assert row["model_selectable"] is False


def test_provider_capabilities_empty_declared_not_auto_detected(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户清空全部能力时，应保留空列表而非回退自动推断。"""
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    update_provider_settings(
        {
            "provider": [DYNAMIC_PROVIDER_NAME],
            "capabilities": [""],
        }
    )

    row = next(
        r for r in _unified_provider_rows(load_config(config_path))
        if r["name"] == DYNAMIC_PROVIDER_NAME
    )
    assert row["capabilities"] == []
    assert row["model_selectable"] is False


def test_delete_provider_settings_removes_dynamic_provider_and_resets_refs(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除动态厂商后应移除其 model_extra 项，并把指向它的引用回退到默认。"""
    config_path = tmp_path / "config.json"
    raw_config = {
        "providers": {
            DYNAMIC_PROVIDER_NAME: {
                "apiBase": DYNAMIC_PROVIDER_API_BASE,
                "apiKey": "sk-test",
            }
        },
        "agents": {
            "defaults": {
                "provider": DYNAMIC_PROVIDER_NAME,
                "model": "gpt-4o-mini",
            }
        },
        "model_presets": {
            "mine": {"provider": DYNAMIC_PROVIDER_NAME, "model": "gpt-4o-mini"}
        },
        "tools": {
            "image_generation": {"provider": DYNAMIC_PROVIDER_NAME},
            "seedance_video": {"provider": DYNAMIC_PROVIDER_NAME},
        },
        "tts": {"provider": DYNAMIC_PROVIDER_NAME},
        "transcription": {"provider": DYNAMIC_PROVIDER_NAME},
    }
    save_config(Config.model_validate(raw_config), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    delete_provider_settings({"provider": [DYNAMIC_PROVIDER_NAME]})

    saved = load_config(config_path)
    assert DYNAMIC_PROVIDER_NAME not in (saved.providers.model_extra or {})
    assert DYNAMIC_PROVIDER_NAME in saved.hidden_providers
    assert saved.agents.defaults.provider == "auto"
    assert saved.model_presets["mine"].provider == "auto"
    assert saved.tools.image_generation.provider == "volcengine"
    assert saved.tools.seedance_video.provider == "volcengine"
    assert saved.tts.provider is None
    assert saved.transcription.provider is None


def test_delete_provider_settings_hides_registry_capability_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置的注册表能力厂商（如 groq）删除后应从统一厂商列表隐藏。"""
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    delete_provider_settings({"provider": ["groq"]})

    saved = load_config(config_path)
    assert "groq" in saved.hidden_providers
    rows = _unified_provider_rows(saved)
    assert "groq" not in {row["name"] for row in rows}


def test_delete_provider_settings_rejects_builtin_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定字段厂商（内置项）不可删除。"""
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError):
        delete_provider_settings({"provider": ["deepseek"]})


def test_update_image_generation_settings_uses_unified_volcengine_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    # 密钥统一在「模型厂商」页配置（不再是 image_generation 内联维护）
    update_provider_settings(
        {
            "provider": ["volcengine"],
            "apiKey": ["sk-ark-test"],
        }
    )

    payload = update_image_generation_settings(
        {
            "provider": ["volcengine"],
            "enabled": ["true"],
        }
    )

    assert payload["image_generation"]["provider"] == "volcengine"
    assert payload["image_generation"]["provider_configured"] is True
    saved = load_config(config_path)
    assert saved.tools.image_generation.provider == "volcengine"
    assert saved.tools.image_generation.enabled is True
    assert saved.providers.model_extra["volcengine"].api_key == "sk-ark-test"


def test_update_image_generation_settings_rejects_unconfigured_image_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.delenv("ARK_API_KEY", raising=False)

    with pytest.raises(
        WebUISettingsError,
        match="image generation provider is not configured",
    ):
        update_image_generation_settings(
            {
                "provider": ["volcengine"],
                "enabled": ["true"],
            }
        )


def test_update_agent_settings_accepts_context_window_options(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_agent_settings({"context_window_tokens": ["262144"]})

    assert payload["agent"]["context_window_tokens"] == 262144
    saved = load_config(config_path)
    assert saved.agents.defaults.context_window_tokens == 262144


def test_update_model_configuration_accepts_context_window_options(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.model_presets["codex"] = ModelPresetConfig(
        label="Codex",
        provider="openai",
        model="openai/gpt-4.1",
    )
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_model_configuration(
        {
            "name": ["codex"],
            "context_window_tokens": ["262144"],
        }
    )

    assert payload["agent"]["context_window_tokens"] == 262144
    saved = load_config(config_path)
    assert saved.model_presets["codex"].context_window_tokens == 262144


def test_update_context_window_rejects_unknown_values(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="context_window_tokens must be 65536 or 262144"):
        update_agent_settings({"context_window_tokens": ["128000"]})


def test_update_model_configuration_rejects_default_preset(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="model configuration is required"):
        update_model_configuration({"name": ["default"], "model": ["openai/gpt-4.1"]})


def test_settings_payload_includes_dynamic_custom_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(defaults=True), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = settings_payload()
    providers = {row["name"]: row for row in payload["providers"]}

    assert payload["agent"]["provider"] == DYNAMIC_PROVIDER_NAME
    assert payload["agent"]["resolved_provider"] == DYNAMIC_PROVIDER_NAME
    assert providers[DYNAMIC_PROVIDER_NAME]["configured"] is True
    assert providers[DYNAMIC_PROVIDER_NAME]["api_key_required"] is False
    assert providers[DYNAMIC_PROVIDER_NAME]["api_base"] == DYNAMIC_PROVIDER_API_BASE


def test_settings_payload_marks_dynamic_custom_provider_without_api_base_unconfigured(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({
        "providers": {
            DYNAMIC_PROVIDER_NAME: {
                "apiKey": "sk-test",
            }
        }
    })
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = settings_payload()
    providers = {row["name"]: row for row in payload["providers"]}

    assert providers[DYNAMIC_PROVIDER_NAME]["configured"] is False
    assert providers[DYNAMIC_PROVIDER_NAME]["api_key_hint"] == "••••"
    assert providers[DYNAMIC_PROVIDER_NAME]["api_base"] is None


def test_settings_payload_includes_network_safety_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.tools.webui_allow_local_service_access = False
    config.tools.ssrf_whitelist = ["100.64.0.0/10"]
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("biscuitbot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = settings_payload()

    assert payload["advanced"]["webui_allow_local_service_access"] is False
    assert payload["advanced"]["allow_local_preview_access"] is False
    assert payload["advanced"]["webui_default_access_mode"] == "default"
    assert payload["advanced"]["private_service_protection_enabled"] is True
    assert payload["advanced"]["ssrf_whitelist_count"] == 1


def test_settings_payload_includes_exec_path_flags(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.tools.exec.path_prepend = "/venv/bin"
    config.tools.exec.path_append = "/usr/sbin"
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("biscuitbot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = settings_payload()

    assert payload["advanced"]["exec_path_prepend_set"] is True
    assert payload["advanced"]["exec_path_append_set"] is True


def test_settings_payload_includes_effective_transcription_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.channels.transcription_provider = "openai"
    config.channels.transcription_language = "en"
    config.providers.openai.api_key = "sk-test"
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = settings_payload()

    assert payload["transcription"]["enabled"] is True
    assert payload["transcription"]["provider"] == "openai"
    assert payload["transcription"]["provider_configured"] is True
    assert payload["transcription"]["model"] == "whisper-1"
    assert payload["transcription"]["language"] == "en"


def test_update_transcription_settings_writes_top_level_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({"providers": {"groq": {"apiKey": "gsk-test"}}})
    config.channels.transcription_provider = "openai"
    config.channels.transcription_language = "en"
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_transcription_settings(
        {
            "enabled": ["true"],
            "provider": ["groq"],
            "model": ["whisper-large-v3-turbo"],
            "language": ["ko"],
            "maxDurationSec": ["90"],
            "maxUploadMb": ["20"],
        }
    )

    saved = load_config(config_path)
    assert saved.channels.transcription_provider == "openai"
    assert saved.channels.transcription_language == "en"
    assert saved.transcription.enabled is True
    assert saved.transcription.provider == "groq"
    assert saved.transcription.model == "whisper-large-v3-turbo"
    assert saved.transcription.language == "ko"
    assert saved.transcription.max_duration_sec == 90
    assert saved.transcription.max_upload_mb == 20
    assert payload["transcription"]["provider"] == "groq"
    assert payload["transcription"]["provider_configured"] is True


def test_settings_payload_includes_tts_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.tts.provider = "openai"
    config.providers.openai.api_key = "sk-test"
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = settings_payload()

    tts = payload["tts"]
    assert tts["enabled"] is True
    assert tts["provider"] == "openai"
    assert tts["provider_configured"] is True
    assert tts["model"] == "gpt-4o-mini-tts"
    assert tts["voice"] == "alloy"
    assert tts["save_dir"] == "generated/tts"
    providers = {row["name"]: row for row in payload["providers"]}
    tts_names = {
        row["name"] for row in providers.values() if "tts" in row["capabilities"]
    }
    assert tts_names == {"openai", "dashscope", "edge-tts", "newapi"}
    edge = providers["edge-tts"]
    assert edge["label"] == "Edge TTS"
    assert edge["configured"] is True
    assert "tts" in edge["capabilities"]


def test_update_tts_settings_writes_top_level_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.tts.provider = "edge-tts"
    config.tts.rate = "-20%"
    config.providers.dashscope.api_key = "ds-test"
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_tts_settings(
        {
            "enabled": ["true"],
            "provider": ["dashscope"],
            "model": ["cosyvoice-v2"],
            "voice": ["longwan"],
            "rate": ["+10%"],
        }
    )

    saved = load_config(config_path)
    assert saved.tts.enabled is True
    assert saved.tts.provider == "dashscope"
    assert saved.tts.model == "cosyvoice-v2"
    assert saved.tts.voice == "longwan"
    assert saved.tts.rate == "+10%"
    assert payload["tts"]["provider"] == "dashscope"
    assert payload["tts"]["voice"] == "longwan"


def test_update_tts_settings_unknown_provider_rejected(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="TTS provider"):
        update_tts_settings({"provider": ["nope"]})


def test_update_transcription_settings_validates_language(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="transcription language"):
        update_transcription_settings({"language": ["en-US"]})


def test_settings_payload_includes_token_usage_summary(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("biscuitbot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    from biscuitbot.webui.token_usage import record_token_usage

    record_token_usage({"prompt_tokens": 10, "completion_tokens": 5})

    payload = settings_payload()

    assert payload["usage"]["total_tokens_30d"] == 15
    assert payload["usage"]["total_tokens"] == 15
    assert payload["usage"]["peak_day_tokens"] == 15
    assert payload["usage"]["current_streak_days"] == 1
    assert payload["usage"]["longest_streak_days"] == 1
    assert payload["usage"]["active_days_30d"] == 1
    assert payload["usage"]["requests_30d"] == 1


def test_settings_usage_payload_returns_lightweight_token_usage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("biscuitbot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    from biscuitbot.webui.token_usage import record_token_usage

    record_token_usage({"prompt_tokens": 20, "completion_tokens": 2})

    payload = settings_usage_payload()

    assert payload["total_tokens"] == 22
    assert payload["requests_30d"] == 1
    assert "agent" not in payload


def test_update_network_safety_settings_writes_local_service_flag(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("biscuitbot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = update_network_safety_settings(
        {
            "webui_allow_local_service_access": ["false"],
            "webui_default_access_mode": ["full"],
        }
    )

    saved = load_config(config_path)
    saved_raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved.tools.webui_allow_local_service_access is False
    assert saved_raw["tools"]["webuiAllowLocalServiceAccess"] is False
    assert "allowLocalPreviewAccess" not in saved_raw["tools"]
    assert payload["advanced"]["webui_allow_local_service_access"] is False
    assert payload["advanced"]["webui_default_access_mode"] == "full"
    assert payload["requires_restart"] is True


def test_update_network_safety_settings_accepts_legacy_restricted_default_access(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("biscuitbot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = update_network_safety_settings({"webui_default_access_mode": ["restricted"]})

    assert payload["advanced"]["webui_default_access_mode"] == "default"


def test_update_network_safety_settings_default_access_is_webui_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    before = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("biscuitbot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = update_network_safety_settings({"webui_default_access_mode": ["full"]})

    saved = load_config(config_path)
    assert config_path.read_text(encoding="utf-8") == before
    assert saved.tools.restrict_to_workspace is False
    assert payload["advanced"]["webui_default_access_mode"] == "full"
    assert payload["requires_restart"] is False


def test_provider_models_payload_fetches_openai_compatible_models(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.providers.deepseek.api_key = "sk-test"
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    def fake_get(url: str, **kwargs):
        assert url == "https://api.deepseek.com/models"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "deepseek-chat", "owned_by": "deepseek"},
                    {"id": "deepseek-reasoner", "context_window": 65536},
                ]
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("biscuitbot.webui.settings_api.httpx.get", fake_get)

    payload = provider_models_payload({"provider": ["deepseek"]})

    assert payload["status"] == "available"
    assert payload["catalog_kind"] == "official"
    assert payload["model_count"] == 2
    assert payload["models"][0]["id"] == "deepseek-chat"
    assert payload["models"][1]["context_window"] == 65536


def test_provider_models_payload_fetches_dynamic_custom_provider_models(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    def fake_get(url: str, **kwargs):
        assert url == f"{DYNAMIC_PROVIDER_API_BASE}/models"
        assert "Authorization" not in kwargs["headers"]
        return httpx.Response(
            200,
            json={"data": [{"id": "custom-gpt", "owned_by": "example"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("biscuitbot.webui.settings_api.httpx.get", fake_get)

    payload = provider_models_payload({"provider": [DYNAMIC_PROVIDER_NAME]})

    assert payload["provider"] == DYNAMIC_PROVIDER_NAME
    assert payload["status"] == "available"
    assert payload["catalog_kind"] == "custom"
    assert payload["models"][0]["id"] == "custom-gpt"


def test_resolve_model_list_provider_synthesizes_non_llm_capabilities() -> None:
    """Non-LLM capability providers (image/TTS/transcription) resolve to a spec."""
    config = Config()

    # transcription provider carries a concrete default_api_base and requires a key
    groq = _resolve_model_list_provider(config, "groq")
    assert groq is not None
    groq_spec, groq_name, _ = groq
    assert groq_name == "groq"
    assert groq_spec.default_api_base == "https://api.groq.com/openai/v1"
    assert groq_spec.is_direct is False

    # image providers resolve with their client's built-in default base URL
    # (apiBase 留空时「模型厂商」页展示该默认地址），但不视为 direct
    expected_defaults = {
        "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
        "gemini": "https://generativelanguage.googleapis.com/v1beta",
        "aihubmix": "https://aihubmix.com/v1",
    }
    for provider, default_base in expected_defaults.items():
        resolved = _resolve_model_list_provider(config, provider)
        assert resolved is not None, provider
        spec, name, _ = resolved
        assert name == provider
        assert spec.default_api_base == default_base
        assert spec.is_direct is False

    # unknown names do not resolve
    assert _resolve_model_list_provider(config, "zzz-not-real") is None


def test_provider_capabilities_derives_from_registries() -> None:
    """能力标签从 registry 推导：火山方舟=image+video、edge-tts=tts、groq=transcription。"""
    config = Config()

    assert _provider_capabilities("volcengine", config) == ["image", "video"]
    assert _provider_capabilities("edge-tts", config) == ["tts"]
    assert _provider_capabilities("groq", config) == ["transcription"]
    # LLM 厂商同时具备 llm 与 vision（非 transcription-only）
    assert "llm" in _provider_capabilities("openai", config)
    assert "vision" in _provider_capabilities("openai", config)


def test_unified_provider_rows_includes_capability_providers() -> None:
    """单一厂商来源同时覆盖 LLM 与非 LLM 能力厂商，并写入 capabilities。"""
    config = Config.model_validate({"providers": {"volcengine": {"apiKey": "sk-ark"}}})

    rows = {row["name"]: row for row in _unified_provider_rows(config)}

    # 火山方舟带 image+video，配了密钥即算已配置，且不进入 LLM 模型下拉
    assert rows["volcengine"]["capabilities"] == ["image", "video"]
    assert rows["volcengine"]["configured"] is True
    assert rows["volcengine"]["label"] == "火山方舟"
    assert rows["volcengine"]["model_selectable"] is False

    # edge-tts 免密钥，恒已配置
    assert "tts" in rows["edge-tts"]["capabilities"]
    assert rows["edge-tts"]["configured"] is True

    # groq 是转写厂商，不进入 LLM 模型下拉
    assert "transcription" in rows["groq"]["capabilities"]
    assert rows["groq"]["model_selectable"] is False

    # LLM 厂商仍在，且可被选作 LLM 模型
    assert "llm" in rows["openai"]["capabilities"]
    assert rows["openai"]["model_selectable"] is True


def test_update_provider_settings_writes_volcengine_to_model_extra(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_provider_settings(
        {
            "provider": ["volcengine"],
            "apiKey": ["sk-ark-test"],
        }
    )

    providers = {row["name"]: row for row in payload["providers"]}
    assert providers["volcengine"]["api_key_hint"] == "sk-a••••test"
    assert providers["volcengine"]["label"] == "火山方舟"
    saved = load_config(config_path)
    assert saved.providers.model_extra["volcengine"].api_key == "sk-ark-test"


def test_settings_payload_includes_system_io_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    config.tools.system_io.enable = True
    config.tools.system_io.allow_actions = ["clipboard_read", "usb_list"]
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = settings_payload()
    assert payload["system_io"]["enabled"] is True
    assert payload["system_io"]["allow_actions"] == ["clipboard_read", "usb_list"]
    action_names = {a["name"] for a in payload["system_io"]["available_actions"]}
    assert "clipboard_read" in action_names
    assert "serial_write" in action_names
    assert len(action_names) == 11


def test_update_system_io_settings_toggles_enable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_system_io_settings({"enabled": ["true"]})
    saved = load_config(config_path)
    assert saved.tools.system_io.enable is True
    assert payload["system_io"]["enabled"] is True
    assert payload["requires_restart"] is True


def test_update_system_io_settings_writes_allow_actions(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    config.tools.system_io.enable = True
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_system_io_settings(
        {"allowActions": ["clipboard_read,usb_list,serial_list"]}
    )
    saved = load_config(config_path)
    assert saved.tools.system_io.allow_actions == ["clipboard_read", "usb_list", "serial_list"]
    assert payload["system_io"]["allow_actions"] == ["clipboard_read", "usb_list", "serial_list"]


def test_update_system_io_settings_clears_allow_actions(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    config.tools.system_io.allow_actions = ["clipboard_read"]
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    update_system_io_settings({"allowActions": [""]})
    saved = load_config(config_path)
    assert saved.tools.system_io.allow_actions == []


def test_update_system_io_settings_rejects_unknown_action(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="unknown system_io action"):
        update_system_io_settings({"allowActions": ["clipboard_read,format_c_drive"]})


def test_channels_payload_reports_allow_all(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    setattr(config.channels, "feishu", {"enabled": True, "allow_all": True})
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = channels_payload()
    feishu_row = next(r for r in payload["channels"] if r["name"] == "feishu")
    assert feishu_row["allow_all"] is True


def test_channels_payload_defaults_allow_all_off(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    setattr(config.channels, "feishu", {"enabled": True})
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = channels_payload()
    feishu_row = next(r for r in payload["channels"] if r["name"] == "feishu")
    assert feishu_row["allow_all"] is False


def test_update_channel_settings_toggles_allow_all(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    setattr(config.channels, "feishu", {"enabled": True, "allowFrom": ["alice"]})
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    payload = update_channel_settings(
        {"channel": ["feishu"], "allow_all": ["true"]}
    )
    assert payload["requires_restart"] is True

    saved = load_config(config_path)
    section = getattr(saved.channels, "feishu")
    assert section.get("allow_all") is True
    # allow_all 不覆盖已有白名单。
    assert section.get("allowFrom") == ["alice"]


def test_update_channel_settings_turns_off_allow_all(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    setattr(config.channels, "feishu", {"enabled": True, "allow_all": True})
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    update_channel_settings({"channel": ["feishu"], "allow_all": ["false"]})
    saved = load_config(config_path)
    section = getattr(saved.channels, "feishu")
    assert section.get("allow_all") is False


def test_update_channel_settings_creates_section_for_allow_all(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config.model_validate({})
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    update_channel_settings({"channel": ["wecom"], "allow_all": ["true"]})
    saved = load_config(config_path)
    section = getattr(saved.channels, "wecom")
    assert section.get("allow_all") is True



