"""Tests for the placeholder provider and degraded gateway startup.

首启场景：网关必须先启动才能承载欢迎设置页，但此时用户尚未配置任何 API Key。
:func:`resolve_provider_snapshot` 在 ``allow_unconfigured`` 时降级为占位 Provider，
配置写入后由对话层按签名变化热替换为真实 Provider。
"""

from __future__ import annotations

import asyncio

import pytest

from biscuitbot.config.schema import Config
from biscuitbot.providers.factory import (
    build_placeholder_snapshot,
    resolve_provider_snapshot,
)
from biscuitbot.providers.placeholder import PlaceholderProvider


def test_build_placeholder_snapshot_uses_default_model() -> None:
    cfg = Config()
    snap = build_placeholder_snapshot(cfg)
    assert isinstance(snap.provider, PlaceholderProvider)
    assert snap.model == cfg.agents.defaults.model
    assert snap.context_window_tokens == cfg.agents.defaults.context_window_tokens


def test_placeholder_chat_raises_guide_hint() -> None:
    provider = PlaceholderProvider()
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))
    assert PlaceholderProvider.NOT_CONFIGURED_HINT in str(excinfo.value)


def test_resolve_provider_snapshot_fails_fast_without_key() -> None:
    cfg = Config()
    with pytest.raises(ValueError):
        resolve_provider_snapshot(cfg, allow_unconfigured=False)


def test_resolve_provider_snapshot_degrades_to_placeholder() -> None:
    cfg = Config()
    snap = resolve_provider_snapshot(cfg, allow_unconfigured=True)
    assert isinstance(snap.provider, PlaceholderProvider)


def test_resolve_provider_snapshot_uses_real_provider_when_configured() -> None:
    cfg = Config()
    cfg.providers.deepseek.api_key = "sk-test"
    snap = resolve_provider_snapshot(cfg, allow_unconfigured=True)
    assert not isinstance(snap.provider, PlaceholderProvider)
    assert snap.provider.api_key == "sk-test"
