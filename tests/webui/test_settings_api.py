from __future__ import annotations

import json

import httpx
import pytest

from xianaibot.config.loader import load_config, save_config
from xianaibot.config.schema import Config, ModelPresetConfig
from xianaibot.webui.settings_api import (
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
    update_video_generation_settings,
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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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


def test_declared_capabilities_query_is_ignored(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧客户端仍发 ``capabilities`` 时静默忽略——能力标签已改为纯派生。

    厂商能力不再可声明（「模型厂商」页已无该排复选框），但旧前端/客户端可能
    继续下发这个查询键；它应与其它未知键一样被忽略，而不是 400 或写回配置。
    """
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    update_provider_settings(
        {
            "provider": [DYNAMIC_PROVIDER_NAME],
            "capabilities": ["image"],
        }
    )

    saved = load_config(config_path)
    dynamic_provider = saved.providers.model_extra[DYNAMIC_PROVIDER_NAME]
    # 声明值不再持久化（字段已从 ProviderConfig 删除，加载时被忽略）
    assert getattr(dynamic_provider, "capabilities", None) is None

    payload = settings_payload()
    providers = {row["name"]: row for row in payload["providers"]}
    # payload 里的 capabilities 是注册表派生值：动态自定义厂商无规格 → llm + vision
    assert providers[DYNAMIC_PROVIDER_NAME]["capabilities"] == ["llm", "vision"]


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
            "seedance_video": {"model": DYNAMIC_PROVIDER_NAME},
        },
        "tts": {"provider": DYNAMIC_PROVIDER_NAME},
        "transcription": {"provider": DYNAMIC_PROVIDER_NAME},
    }
    save_config(Config.model_validate(raw_config), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    delete_provider_settings({"provider": [DYNAMIC_PROVIDER_NAME]})

    saved = load_config(config_path)
    assert DYNAMIC_PROVIDER_NAME not in (saved.providers.model_extra or {})
    assert DYNAMIC_PROVIDER_NAME in saved.hidden_providers
    assert saved.agents.defaults.provider == "auto"
    assert saved.model_presets["mine"].provider == "auto"
    assert saved.tools.image_generation.provider == "volcengine"
    # 自定义厂商不是视频厂商，删除时不应触碰视频工具的 model。
    assert saved.tools.seedance_video.model == DYNAMIC_PROVIDER_NAME
    assert saved.tts.provider is None
    assert saved.transcription.provider is None


def test_delete_provider_settings_hides_registry_capability_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置的注册表能力厂商（如 groq）删除后应从统一厂商列表隐藏。"""
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError):
        delete_provider_settings({"provider": ["deepseek"]})


def test_update_image_generation_settings_uses_unified_volcengine_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    payload = update_agent_settings({"context_window_tokens": ["262144"]})

    assert payload["agent"]["context_window_tokens"] == 262144
    saved = load_config(config_path)
    assert saved.agents.defaults.context_window_tokens == 262144


def test_update_agent_settings_accepts_workspace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    workspace = tmp_path / "workspace"
    payload = update_agent_settings({"workspace": [str(workspace)]})

    assert payload["runtime"]["workspace_path"] == str(workspace.resolve())
    saved = load_config(config_path)
    assert saved.agents.defaults.workspace == str(workspace.resolve())
    assert workspace.is_dir()


def test_update_agent_settings_accepts_workspace_alias(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    workspace = tmp_path / "workspace"
    update_agent_settings({"workspacePath": [str(workspace)]})

    saved = load_config(config_path)
    assert saved.agents.defaults.workspace == str(workspace.resolve())


def test_update_agent_settings_rejects_blank_workspace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="workspace is required"):
        update_agent_settings({"workspace": ["   "]})

    saved = load_config(config_path)
    assert saved.agents.defaults.workspace != "   "


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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="context_window_tokens must be one of"):
        update_agent_settings({"context_window_tokens": ["128000"]})


def test_update_model_configuration_rejects_default_preset(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="model configuration is required"):
        update_model_configuration({"name": ["default"], "model": ["openai/gpt-4.1"]})


def test_settings_payload_includes_dynamic_custom_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(defaults=True), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("xianaibot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("xianaibot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="TTS provider"):
        update_tts_settings({"provider": ["nope"]})


def test_update_transcription_settings_validates_language(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    with pytest.raises(WebUISettingsError, match="transcription language"):
        update_transcription_settings({"language": ["en-US"]})


def test_settings_payload_includes_token_usage_summary(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    save_config(config, config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("xianaibot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    from xianaibot.webui.token_usage import record_token_usage

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("xianaibot.webui.token_usage.get_webui_dir", lambda: tmp_path / "webui")

    from xianaibot.webui.token_usage import record_token_usage

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("xianaibot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("xianaibot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    payload = update_network_safety_settings({"webui_default_access_mode": ["restricted"]})

    assert payload["advanced"]["webui_default_access_mode"] == "default"


def test_update_network_safety_settings_default_access_is_webui_only(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    before = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    monkeypatch.setattr("xianaibot.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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

    monkeypatch.setattr("xianaibot.webui.settings_api.httpx.get", fake_get)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    def fake_get(url: str, **kwargs):
        assert url == f"{DYNAMIC_PROVIDER_API_BASE}/models"
        assert "Authorization" not in kwargs["headers"]
        return httpx.Response(
            200,
            json={"data": [{"id": "custom-gpt", "owned_by": "example"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("xianaibot.webui.settings_api.httpx.get", fake_get)

    payload = provider_models_payload({"provider": [DYNAMIC_PROVIDER_NAME]})

    assert payload["provider"] == DYNAMIC_PROVIDER_NAME
    assert payload["status"] == "available"
    assert payload["catalog_kind"] == "custom"
    assert payload["models"][0]["id"] == "custom-gpt"


def test_provider_models_payload_kling_returns_known_list_without_network(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可灵没有 /models 端点：不发起任何 HTTP 请求，直接返回内置已知模型列表。

    未配置密钥也应能列出模型（前端在配置后拉取，后端不依赖密钥即可给出候选）。
    """
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    def fail_get(url: str, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError(f"可灵不应请求 /models：{url}")

    monkeypatch.setattr("xianaibot.webui.settings_api.httpx.get", fail_get)

    payload = provider_models_payload({"provider": ["kling"]})

    assert payload["provider"] == "kling"
    assert payload["status"] == "available"
    assert payload["catalog_kind"] == "official"
    assert payload["model_count"] >= 3
    ids = [model["id"] for model in payload["models"]]
    assert "kling-3.0" in ids
    assert "kling-v3-omni" in ids
    assert any(model["label"] for model in payload["models"])


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


def test_provider_models_payload_configured_volcengine_uses_ark_fallback_base(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：「模型厂商」页配置的火山方舟（model_extra 自定义项）仍能拉模型列表。

    动态 spec 既无 default_api_base，is_direct=True 还会被误判免密钥；
    此前该路径误报 missing_api_base（"Configure an API base URL to load
    models."），卡死文生图页的模型选择。
    """
    config_path = tmp_path / "config.json"
    config = Config.model_validate({"providers": {"volcengine": {"apiKey": "sk-ark"}}})
    save_config(config, config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    resolved = _resolve_model_list_provider(config, "volcengine")
    assert resolved is not None
    spec, name, provider_config = resolved
    assert name == "volcengine"
    assert spec.default_api_base == "https://ark.cn-beijing.volces.com/api/v3"
    assert spec.is_direct is False
    assert provider_config.api_key == "sk-ark"

    def fake_get(url: str, **kwargs):
        assert url == "https://ark.cn-beijing.volces.com/api/v3/models"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-ark"
        return httpx.Response(
            200,
            json={"data": [{"id": "doubao-seedream-4-0", "owned_by": "volcengine"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("xianaibot.webui.settings_api.httpx.get", fake_get)

    payload = provider_models_payload({"provider": ["volcengine"]})

    assert payload["provider"] == "volcengine"
    assert payload["status"] == "available"
    assert payload["models"][0]["id"] == "doubao-seedream-4-0"


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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

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
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    update_channel_settings({"channel": ["wecom"], "allow_all": ["true"]})
    saved = load_config(config_path)
    section = getattr(saved.channels, "wecom")
    assert section.get("allow_all") is True





# ---- 可灵（Kling）视频厂商 ------------------------------------------------


def test_unified_provider_rows_includes_kling_video_provider() -> None:
    """可灵出现在单一厂商来源：label=可灵、capability=[video]、configured 随 key。"""
    config = Config.model_validate({"providers": {"kling": {"apiKey": "AK:SK"}}})
    rows = {row["name"]: row for row in _unified_provider_rows(config)}

    row = rows.get("kling")
    assert row is not None
    assert row["label"] == "可灵"
    assert row["capabilities"] == ["video"]
    assert row["configured"] is True
    assert row["model_selectable"] is False

    # 未配 key → 未配置
    empty = {r["name"]: r for r in _unified_provider_rows(Config())}
    assert empty["kling"]["configured"] is False
    assert empty["kling"]["capabilities"] == ["video"]
    assert empty["kling"]["default_api_base"] == "https://api-beijing.klingai.com"


def test_resolve_model_list_provider_kling_video_capability() -> None:
    """可灵解析为视频能力厂商：default_api_base 为官方地址，且需要密钥。"""
    config = Config()
    resolved = _resolve_model_list_provider(config, "kling")
    assert resolved is not None
    spec, name, _ = resolved
    assert name == "kling"
    assert spec.default_api_base == "https://api-beijing.klingai.com"
    assert spec.is_direct is False


def test_provider_capabilities_kling_video_only() -> None:
    config = Config()
    assert _provider_capabilities("kling", config) == ["video"]


def test_resolve_model_list_provider_keeps_llm_base_for_dual_video_provider() -> None:
    """灵积同时是 LLM 与视频厂商：模型枚举必须走 LLM 兼容模式 base，不能被视频裸域顶替。

    视频客户端的 ``_default_base_url`` 是视频 API 的裸域（DashScope 为
    ``https://dashscope.aliyuncs.com``），而无条件覆盖会让
    ``/api/settings/provider-models?provider=dashscope`` 去请求
    ``https://dashscope.aliyuncs.com/models``（404）。
    """
    spec, name, _ = _resolve_model_list_provider(Config(), "dashscope")  # type: ignore[misc]
    assert name == "dashscope"
    assert spec.default_api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 纯视频厂商（无自带默认 base）仍补视频客户端的 base URL。
    kling_spec, _, _ = _resolve_model_list_provider(Config(), "kling")  # type: ignore[misc]
    assert kling_spec.default_api_base == "https://api-beijing.klingai.com"




def test_update_video_generation_settings_kling_routes_to_kling_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vendor=kling 路由到 tools.kling_video；密钥在「模型厂商」页配置后判定已配置。"""
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    # 未配任何密钥 → 工具未启用，但默认参数仍可保存。
    payload = update_video_generation_settings(
        {
            "vendor": ["kling"],
            "model": ["kling-v3-omni"],
            "default_ratio": ["9:16"],
            "default_duration": ["9"],
        }
    )
    kling = payload["video_generation"]["vendors"]["kling"]
    assert kling["configured"] is False
    assert kling["model"] == "kling-v3-omni"
    assert kling["default_ratio"] == "9:16"
    assert kling["default_duration"] == 9

    # 在「模型厂商」页配置可灵密钥后 → 判定已配置
    update_provider_settings({"provider": ["kling"], "apiKey": ["AK123:SK456"]})
    payload = update_video_generation_settings({"vendor": ["kling"]})
    assert payload["video_generation"]["vendors"]["kling"]["configured"] is True

    saved = load_config(config_path)
    assert saved.tools.kling_video.model == "kling-v3-omni"
    assert saved.tools.kling_video.default_ratio == "9:16"
    assert saved.tools.kling_video.default_duration == 9
    assert saved.providers.model_extra["kling"].api_key == "AK123:SK456"


def test_update_video_generation_settings_requires_vendor() -> None:
    """缺少 vendor 参数直接拒绝（无法确定写入哪家厂商）。"""
    with pytest.raises(WebUISettingsError, match="vendor is required"):
        update_video_generation_settings({"model": ["kling-v3-omni"]})


# ---- MiniMax H3 视频厂商 --------------------------------------------------


def test_unified_provider_rows_includes_minimax_video_provider() -> None:
    """MiniMax 出现在单一厂商来源：label=MiniMax、裸域默认地址、configured 随 key。"""
    config = Config.model_validate({"providers": {"minimax": {"apiKey": "mm-1"}}})
    rows = {row["name"]: row for row in _unified_provider_rows(config)}

    row = rows.get("minimax")
    assert row is not None
    assert row["label"] == "MiniMax"
    assert row["default_api_base"] == "https://api.minimaxi.com"
    assert row["configured"] is True

    empty = {r["name"]: r for r in _unified_provider_rows(Config())}
    assert empty["minimax"]["configured"] is False
    assert empty["minimax"]["capabilities"] == ["video"]
    assert empty["minimax"]["default_api_base"] == "https://api.minimaxi.com"


def test_provider_capabilities_minimax_video_only_without_llm_config() -> None:
    """未配 minimax 时它是纯视频厂商。"""
    assert _provider_capabilities("minimax", Config()) == ["video"]


def test_provider_capabilities_minimax_keeps_llm_when_configured() -> None:
    """回归守卫（陷阱 1）：已当 LLM 厂商配置的 minimax 不能因注册视频能力而丢 llm/vision。

    否则它会从 LLM 模型下拉框里消失，而前端救不回来——capabilities 只在复选框被
    实际改动时才提交，没动过的行走的正是这里的自动推断。
    """
    config = Config.model_validate(
        {"providers": {"minimax": {"apiKey": "mm-1", "apiBase": "https://api.minimaxi.com/v1"}}}
    )
    caps = _provider_capabilities("minimax", config)
    assert "llm" in caps
    assert "vision" in caps
    assert "video" in caps


def test_resolve_model_list_provider_minimax_video_capability() -> None:
    """minimax 解析为能力厂商：default_api_base 为裸域，且需要密钥。"""
    resolved = _resolve_model_list_provider(Config(), "minimax")
    assert resolved is not None
    spec, name, _ = resolved
    assert name == "minimax"
    assert spec.default_api_base == "https://api.minimaxi.com"
    assert spec.is_direct is False


def test_provider_models_payload_minimax_requests_v1_models(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裸域默认值下模型枚举必须打到 /v1/models（否则 404）。"""
    config_path = tmp_path / "config.json"
    save_config(
        Config.model_validate(
            {
                "providers": {
                    "minimax": {
                        "apiKey": "mm-1",
                        "apiBase": "https://api.minimaxi.com",
                    }
                }
            }
        ),
        config_path,
    )
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    seen: dict = {}

    def fake_get(url: str, **kwargs):
        seen["url"] = url
        return httpx.Response(
            200,
            json={"data": [{"id": "MiniMax-Text-01"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr("xianaibot.webui.settings_api.httpx.get", fake_get)

    payload = provider_models_payload({"provider": ["minimax"]})
    assert seen["url"] == "https://api.minimaxi.com/v1/models"
    assert payload["provider"] == "minimax"
    assert payload["models"][0]["id"] == "MiniMax-Text-01"


def test_update_video_generation_settings_minimax_routes_to_minimax_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vendor=minimax 路由到 tools.minimax_video；密钥在「模型厂商」页配置后判定已配置。"""
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    payload = update_video_generation_settings(
        {
            "vendor": ["minimax"],
            "model": ["MiniMax-H3"],
            "default_duration": ["12"],
            "watermark": ["true"],
        }
    )
    minimax = payload["video_generation"]["vendors"]["minimax"]
    assert minimax["configured"] is False
    assert minimax["model"] == "MiniMax-H3"
    assert minimax["default_duration"] == 12
    assert minimax["watermark"] is True

    update_provider_settings({"provider": ["minimax"], "apiKey": ["mm-secret"]})
    payload = update_video_generation_settings({"vendor": ["minimax"]})
    assert payload["video_generation"]["vendors"]["minimax"]["configured"] is True

    saved = load_config(config_path)
    assert saved.tools.minimax_video.model == "MiniMax-H3"
    assert saved.tools.minimax_video.default_duration == 12
    assert saved.tools.minimax_video.watermark is True
    assert saved.providers.model_extra["minimax"].api_key == "mm-secret"


def test_update_video_generation_settings_dashscope_routes_to_dashscope_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vendor=dashscope 路由到 tools.dashscope_video；密钥在「模型厂商」页配置后判定已配置。"""
    config_path = tmp_path / "config.json"
    save_config(Config.model_validate({}), config_path)
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)
    # 屏蔽环境变量兜底密钥，确保 configured 只反映配置页写入的密钥
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    payload = update_video_generation_settings(
        {
            "vendor": ["dashscope"],
            "model": ["wan2.6-i2v"],
            "default_ratio": ["9:16"],
            "default_duration": ["5"],
            "default_resolution": ["1080P"],
        }
    )
    dashscope = payload["video_generation"]["vendors"]["dashscope"]
    assert dashscope["configured"] is False
    assert dashscope["model"] == "wan2.6-i2v"
    assert dashscope["default_ratio"] == "9:16"
    assert dashscope["default_duration"] == 5
    assert dashscope["default_resolution"] == "1080P"

    # 在「模型厂商」页配置灵积密钥后 → 判定已配置（dashscope 同时是 LLM 厂商）
    update_provider_settings({"provider": ["dashscope"], "apiKey": ["sk-ds-1"]})
    payload = update_video_generation_settings({"vendor": ["dashscope"]})
    assert payload["video_generation"]["vendors"]["dashscope"]["configured"] is True

    saved = load_config(config_path)
    assert saved.tools.dashscope_video.model == "wan2.6-i2v"
    assert saved.tools.dashscope_video.default_resolution == "1080P"


def test_update_video_generation_settings_per_vendor_validation() -> None:
    """选项按厂商校验：kling 不接受 4:3，seedance 才接受 adaptive。"""
    with pytest.raises(WebUISettingsError, match="aspect ratio"):
        update_video_generation_settings({"vendor": ["kling"], "default_ratio": ["4:3"]})
    with pytest.raises(WebUISettingsError, match="aspect ratio"):
        update_video_generation_settings({"vendor": ["kling"], "default_ratio": ["adaptive"]})
    with pytest.raises(WebUISettingsError, match="between 3 and 15"):
        update_video_generation_settings({"vendor": ["kling"], "default_duration": ["20"]})
    with pytest.raises(WebUISettingsError, match="resolution"):
        update_video_generation_settings({"vendor": ["seedance"], "default_resolution": ["2K"]})
    # 通义万相：只收 16:9 / 9:16 / 1:1，时长 2–15，清晰度只认 480P/720P/1080P
    with pytest.raises(WebUISettingsError, match="aspect ratio"):
        update_video_generation_settings({"vendor": ["dashscope"], "default_ratio": ["adaptive"]})
    with pytest.raises(WebUISettingsError, match="between 2 and 15"):
        update_video_generation_settings({"vendor": ["dashscope"], "default_duration": ["20"]})
    with pytest.raises(WebUISettingsError, match="resolution"):
        update_video_generation_settings({"vendor": ["dashscope"], "default_resolution": ["768P"]})


def test_settings_payload_video_generation_per_vendor_shape() -> None:
    """payload.video_generation 新结构：vendors + support + 选项下发。"""
    payload = settings_payload()
    video = payload["video_generation"]
    # 厂商清单由各工具类的 vendor_spec 派生（新增厂商无需改设置层）
    assert set(video["vendors"]) == {"seedance", "kling", "minimax", "dashscope"}
    # 卡片展示信息（前端标题/品牌/清晰度必填标记）由后端下发
    assert video["vendors"]["dashscope"]["display_name"] == "通义万相（灵积）"
    assert video["vendors"]["dashscope"]["provider"] == "dashscope"
    assert video["vendors"]["dashscope"]["resolution_optional"] is True
    assert video["vendors"]["minimax"]["resolution_optional"] is False
    # kling 不支持 seed / watermark；minimax 不支持 seed / generate_audio。
    assert "seed" not in video["vendors"]["kling"]
    assert "watermark" not in video["vendors"]["kling"]
    assert "seed" not in video["vendors"]["minimax"]
    assert "generate_audio" not in video["vendors"]["minimax"]
    assert "seed" in video["vendors"]["seedance"]
    assert "watermark" in video["vendors"]["seedance"]
    assert video["support"]["kling"] == ["t2v", "i2v", "v2v"]
    assert "ref_audio" in video["support"]["minimax"]
    assert video["ratio_options"]["kling"] == ["16:9", "9:16", "1:1"]
    assert video["duration_ranges"]["kling"] == [3, 15]
    assert video["model_suggestions"]["minimax"] == ["MiniMax-H3", "MiniMax-H3-Max"]
    assert "kling-v3-omni" in video["model_suggestions"]["kling"]
    assert "doubao-seedance-2-0-fast-260128" in video["model_suggestions"]["seedance"]
    # 通义万相：能力徽章与选项
    assert video["support"]["dashscope"] == ["t2v", "i2v"]
    assert video["ratio_options"]["dashscope"] == ["16:9", "9:16", "1:1"]
    assert video["resolution_options"]["dashscope"] == ["480P", "720P", "1080P"]
    assert video["duration_ranges"]["dashscope"] == [2, 15]
    assert "wan2.6-t2v" in video["model_suggestions"]["dashscope"]
    assert "wan2.6-i2v-flash" in video["model_suggestions"]["dashscope"]
    assert "wan2.2-i2v-flash" in video["model_suggestions"]["dashscope"]


def test_delete_provider_settings_resets_matching_video_vendor_model(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除视频厂商（kling）时把对应工具的 model 复位为默认值。"""
    config_path = tmp_path / "config.json"
    save_config(
        Config.model_validate(
            {
                "providers": {"kling": {"apiKey": "AK:SK"}},
                "tools": {"kling_video": {"model": "kling-v3-omni"}},
            }
        ),
        config_path,
    )
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    delete_provider_settings({"provider": ["kling"]})

    saved = load_config(config_path)
    assert saved.tools.kling_video.model == "kling-3.0"


def test_delete_provider_settings_resets_video_model_when_provider_differs_from_key(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider ≠ 卡片键时级联仍生效：删除 volcengine → 复位 seedance_video.model。

    灵积（dashscope）本身是内置 LLM 厂商、不可删除，故用 seedance↔volcengine
    这一对（spec.provider 与 spec.key 不同）覆盖级联的泛化分支。
    """
    config_path = tmp_path / "config.json"
    save_config(
        Config.model_validate(
            {
                "providers": {"volcengine": {"apiKey": "AK:SK"}},
                "tools": {"seedance_video": {"model": "doubao-seedance-2-0-pro"}},
            }
        ),
        config_path,
    )
    monkeypatch.setattr("xianaibot.config.loader._current_config_path", config_path)

    delete_provider_settings({"provider": ["volcengine"]})

    saved = load_config(config_path)
    assert saved.tools.seedance_video.model == type(
        saved.tools.seedance_video
    ).model_fields["model"].default
