"""Tests for lazy provider exports from xianaibot.providers."""

from __future__ import annotations

import importlib
import sys


def test_importing_providers_package_is_lazy(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "xianaibot.providers", raising=False)
    monkeypatch.delitem(sys.modules, "xianaibot.providers.anthropic_provider", raising=False)
    monkeypatch.delitem(sys.modules, "xianaibot.providers.openai_compat_provider", raising=False)

    providers = importlib.import_module("xianaibot.providers")

    assert "xianaibot.providers.anthropic_provider" not in sys.modules
    assert "xianaibot.providers.openai_compat_provider" not in sys.modules
    assert providers.__all__ == [
        "LLMProvider",
        "LLMResponse",
        "AnthropicProvider",
        "OpenAICompatProvider",
    ]


def test_explicit_provider_import_still_works(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "xianaibot.providers", raising=False)
    monkeypatch.delitem(sys.modules, "xianaibot.providers.anthropic_provider", raising=False)

    namespace: dict[str, object] = {}
    exec("from xianaibot.providers import AnthropicProvider", namespace)

    assert namespace["AnthropicProvider"].__name__ == "AnthropicProvider"
    assert "xianaibot.providers.anthropic_provider" in sys.modules
