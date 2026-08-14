"""TTS 服务层 resolve_tts_config 单元测试。"""

from __future__ import annotations

import pytest

from biscuitbot.audio.tts import (
    EffectiveTtsConfig,
    resolve_tts_config,
    resolve_tts_config_with_overrides,
)
from biscuitbot.audio.tts_registry import resolve_tts_provider
from biscuitbot.config.schema import Config


def test_default_resolves_to_edge_tts() -> None:
    eff = resolve_tts_config(Config())
    assert isinstance(eff, EffectiveTtsConfig)
    assert eff.enabled is True
    assert eff.provider == "edge-tts"
    assert eff.voice == "zh-CN-XiaoxiaoNeural"
    assert eff.save_dir == "generated/tts"
    # edge-tts 免 key：恒为已配置
    assert eff.configured is True


def test_provider_alias_resolves() -> None:
    spec = resolve_tts_provider("tongyi")
    assert spec.name == "dashscope"


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError):
        resolve_tts_provider("nope")


def test_openai_provider_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # 其他测试（如 test_runner_fallback）构建 openai client 时会在 _setup_env
    # 里回填 OPENAI_API_KEY 到全局环境，这里显式隔离，避免被污染。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    cfg.tts.provider = "openai"
    eff = resolve_tts_config(cfg)
    assert eff.provider == "openai"
    assert eff.model == "gpt-4o-mini-tts"
    assert eff.voice == "alloy"
    assert eff.configured is False


def test_openai_provider_configured_with_provider_key() -> None:
    cfg = Config()
    cfg.tts.provider = "openai"
    cfg.providers.openai.api_key = "sk-test"
    eff = resolve_tts_config(cfg)
    assert eff.configured is True
    assert eff.api_key == "sk-test"


def test_overrides_apply_on_top_of_config() -> None:
    cfg = Config()
    cfg.tts.provider = "edge-tts"
    eff = resolve_tts_config_with_overrides(
        cfg,
        provider="openai",
        voice="nova",
        rate="+10%",
    )
    assert eff.provider == "openai"
    assert eff.model == "gpt-4o-mini-tts"
    assert eff.voice == "nova"
    assert eff.rate == "+10%"


def test_explicit_top_level_values_preserved_across_override() -> None:
    cfg = Config()
    cfg.tts.provider = "edge-tts"
    cfg.tts.rate = "-20%"
    eff = resolve_tts_config_with_overrides(cfg, provider="dashscope")
    assert eff.provider == "dashscope"
    assert eff.model == "cosyvoice-v2"
    assert eff.voice == "longxiaochun"
    assert eff.rate == "-20%"
