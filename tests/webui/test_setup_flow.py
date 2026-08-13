"""Tests for the first-run setup flow.

Two halves are covered:

- :func:`biscuitbot.webui.settings_api.needs_setup` — the pure predicate that
  decides whether the app is in the "no configured LLM provider" state (the
  desktop welcome page reads this from bootstrap).
- ``GET /api/webui/setup/complete`` — the endpoint the welcome page calls to
  persist a provider API key (plus an optional model) using the same
  ``update_provider_settings`` path the settings page uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from biscuitbot.config.loader import load_config, save_config
from biscuitbot.config.schema import Config
from biscuitbot.webui.settings_api import needs_setup

DYNAMIC_PROVIDER_NAME = "my-company-api"
DYNAMIC_PROVIDER_API_BASE = "https://example.test/v1"


def _dynamic_provider_config(*, with_api_key: bool = False) -> Config:
    raw_config = {
        "providers": {
            DYNAMIC_PROVIDER_NAME: {
                "apiBase": DYNAMIC_PROVIDER_API_BASE,
            }
        }
    }
    if with_api_key:
        raw_config["providers"][DYNAMIC_PROVIDER_NAME]["apiKey"] = "sk-test"
    return Config.model_validate(raw_config)


# ---------------------------------------------------------------------------
# needs_setup
# ---------------------------------------------------------------------------


def test_needs_setup_is_true_for_fresh_config(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    assert needs_setup(load_config(config_path)) is True


def test_needs_setup_is_false_when_any_provider_has_api_key(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.providers.deepseek.api_key = "sk-test"
    save_config(config, config_path)
    assert needs_setup(load_config(config_path)) is False


def test_needs_setup_stays_true_when_provider_missing_api_key(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config = Config()
    config.providers.openai.api_base = "https://proxy.example/v1"  # no key
    save_config(config, config_path)
    assert needs_setup(load_config(config_path)) is True


def test_needs_setup_is_false_for_configured_dynamic_provider(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_dynamic_provider_config(with_api_key=True), config_path)
    assert needs_setup(load_config(config_path)) is False


def test_needs_setup_is_true_for_unconfigured_dynamic_provider(
    tmp_path, monkeypatch
) -> None:
    # A dynamic provider with only an api_key but no api_base is unconfigured
    # (the settings page treats dynamic providers as requiring api_base).
    config_path = tmp_path / "config.json"
    config = Config.model_validate({
        "providers": {DYNAMIC_PROVIDER_NAME: {"apiKey": "sk-test"}}
    })
    save_config(config, config_path)
    assert needs_setup(load_config(config_path)) is True


# ---------------------------------------------------------------------------
# GET /api/webui/setup/complete
# ---------------------------------------------------------------------------


def _channel(bus: Any, workspace_path: Path) -> Any:
    """Build a WebSocketChannel whose gateway serves the HTTP routes."""
    from biscuitbot.channels.websocket import WebSocketChannel, WebSocketConfig
    from biscuitbot.webui.gateway_services import build_gateway_services

    parsed = WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": 0,
            "path": "/",
            "websocketRequiresToken": False,
        }
    )
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=workspace_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel({}, bus, gateway=gateway)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


def _authed_request(gateway: Any, path: str) -> Any:
    """A fake WsRequest carrying a valid API token for the given path."""
    token = gateway.tokens.issue_token(3600, api_token=True)
    return type("FakeRequest", (), {
        "path": path,
        "headers": {"Authorization": f"Bearer {token}"},
    })()


def test_setup_complete_persists_provider_key_and_model(
    tmp_path, monkeypatch, bus
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    channel = _channel(bus, tmp_path / "workspace")
    resp = channel.gateway.http._handle_webui_setup_complete(
        _authed_request(
            channel.gateway,
            "/api/webui/setup/complete?provider=openai&apiKey=sk-test&model=openai/gpt-4.1-mini",
        )
    )

    assert resp.status_code == 200
    saved = load_config(config_path)
    assert saved.providers.openai.api_key == "sk-test"
    assert saved.agents.defaults.model == "openai/gpt-4.1-mini"
    assert needs_setup(load_config(config_path)) is False


def test_setup_complete_accepts_api_base_and_api_key_alias(
    tmp_path, monkeypatch, bus
) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    channel = _channel(bus, tmp_path / "workspace")
    resp = channel.gateway.http._handle_webui_setup_complete(
        _authed_request(
            channel.gateway,
            "/api/webui/setup/complete?provider=deepseek"
            "&api_key=dk-123&api_base=https%3A%2F%2Fapi.deepseek.com",
        )
    )

    assert resp.status_code == 200
    saved = load_config(config_path)
    assert saved.providers.deepseek.api_key == "dk-123"
    assert saved.providers.deepseek.api_base == "https://api.deepseek.com"
    assert needs_setup(load_config(config_path)) is False


def test_setup_complete_requires_api_key(tmp_path, monkeypatch, bus) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    channel = _channel(bus, tmp_path / "workspace")
    resp = channel.gateway.http._handle_webui_setup_complete(
        _authed_request(channel.gateway, "/api/webui/setup/complete?provider=openai")
    )
    assert resp.status_code == 400


def test_setup_complete_rejects_unknown_provider(tmp_path, monkeypatch, bus) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    channel = _channel(bus, tmp_path / "workspace")
    resp = channel.gateway.http._handle_webui_setup_complete(
        _authed_request(channel.gateway, "/api/webui/setup/complete?provider=nope&apiKey=sk-x")
    )
    assert resp.status_code == 400


def test_setup_complete_requires_auth(tmp_path, monkeypatch, bus) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    channel = _channel(bus, tmp_path / "workspace")
    request = type("FakeRequest", (), {
        "path": "/api/webui/setup/complete?provider=openai&apiKey=sk-test",
        "headers": {},
    })()
    resp = channel.gateway.http._handle_webui_setup_complete(request)
    assert resp.status_code == 401
