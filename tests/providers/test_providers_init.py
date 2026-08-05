"""Tests for lazy provider exports from biscuitbot.providers."""

from __future__ import annotations

import importlib
import sys


def test_importing_providers_package_is_lazy(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "biscuitbot.providers", raising=False)
    monkeypatch.delitem(sys.modules, "biscuitbot.providers.anthropic_provider", raising=False)
    monkeypatch.delitem(sys.modules, "biscuitbot.providers.openai_compat_provider", raising=False)

    providers = importlib.import_module("biscuitbot.providers")

    assert "biscuitbot.providers.anthropic_provider" not in sys.modules
    assert "biscuitbot.providers.openai_compat_provider" not in sys.modules
    assert providers.__all__ == [
        "LLMProvider",
        "LLMResponse",
        "AnthropicProvider",
        "OpenAICompatProvider",
    ]


def test_explicit_provider_import_still_works(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "biscuitbot.providers", raising=False)
    monkeypatch.delitem(sys.modules, "biscuitbot.providers.anthropic_provider", raising=False)

    namespace: dict[str, object] = {}
    exec("from biscuitbot.providers import AnthropicProvider", namespace)

    assert namespace["AnthropicProvider"].__name__ == "AnthropicProvider"
    assert "biscuitbot.providers.anthropic_provider" in sys.modules
