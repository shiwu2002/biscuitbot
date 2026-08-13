"""Tests for the headless gateway sidecar used by the Tauri desktop shell.

Covers the port-avoidance logic, first-run runtime initialization, and the
stdout handshake line the Tauri shell parses. The heavy gateway itself is
mocked out; :func:`biscuitbot.desktop.sidecar.main` is exercised with a fake
``start_gateway``.
"""

from __future__ import annotations

import io
import os
import socket
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from biscuitbot.config.loader import load_config, save_config
from biscuitbot.config.schema import Config
from biscuitbot.desktop import app as desktop_app
from biscuitbot.desktop import sidecar


def test_resolve_websocket_endpoint_defaults_to_ws_port(tmp_path, monkeypatch) -> None:
    cfg = Config()
    host, port = desktop_app.resolve_websocket_endpoint(cfg)
    assert host == "127.0.0.1"
    assert port == 8765


def test_resolve_websocket_endpoint_reads_websocket_extra(tmp_path, monkeypatch) -> None:
    cfg = Config()
    setattr(cfg.channels, "websocket", {"enabled": True, "host": "0.0.0.0", "port": 9123})
    assert desktop_app.resolve_websocket_endpoint(cfg) == ("0.0.0.0", 9123)


def test_ensure_websocket_enabled_is_idempotent() -> None:
    cfg = Config()
    assert desktop_app._ensure_websocket_enabled(cfg) is True
    assert desktop_app._ensure_websocket_enabled(cfg) is False
    ws = getattr(cfg.channels, "websocket")
    assert ws.get("enabled") is True


def test_set_websocket_port_writes_extra() -> None:
    cfg = Config()
    setattr(cfg.channels, "websocket", {"enabled": True})
    desktop_app._set_websocket_port(cfg, 8888)
    assert desktop_app.resolve_websocket_endpoint(cfg)[1] == 8888


def test_ensure_runtime_persists_default_config_and_enables_websocket(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    cfg = sidecar.ensure_runtime()

    assert config_path.exists()
    assert cfg.workspace_path.exists() and cfg.workspace_path.is_dir()
    ws = getattr(cfg.channels, "websocket", None)
    assert ws and ws.get("enabled") is True
    # Reload from disk: websocket enable must survive.
    reloaded = load_config(config_path)
    assert getattr(reloaded.channels, "websocket", None) is not None


def test_pick_free_port_prefers_configured_port() -> None:
    assert sidecar.pick_free_port(8765, host="127.0.0.1") == 8765


def test_parent_vanished_detects_reparent(tmp_path, monkeypatch) -> None:
    # 父进程 PID 变为 1（reparent 到 launchd/init）→ 判定为壳已退出
    monkeypatch.setattr("biscuitbot.desktop.sidecar.os.getppid", lambda: 1)
    assert sidecar._parent_vanished(1234) is True
    # 父进程 PID 未变化 → 壳仍存活
    monkeypatch.setattr("biscuitbot.desktop.sidecar.os.getppid", lambda: 1234)
    assert sidecar._parent_vanished(1234) is False


def test_watch_pid_reads_env(monkeypatch) -> None:
    # 未设置环境变量 → 返回 None（退化为 getppid 检测）
    monkeypatch.delenv("BISCUITBOT_PARENT_PID", raising=False)
    assert sidecar._watch_pid() is None
    # 设置后 → 返回壳 PID
    monkeypatch.setenv("BISCUITBOT_PARENT_PID", "4242")
    assert sidecar._watch_pid() == 4242
    # 非法值 → None
    monkeypatch.setenv("BISCUITBOT_PARENT_PID", "abc")
    assert sidecar._watch_pid() is None


def test_process_alive_detects_existence() -> None:
    # 自身进程必然存活
    assert sidecar._process_alive(os.getpid()) is True
    # 一个几乎不可能存在的 PID → 判定已退出
    assert sidecar._process_alive(2_000_000_000) is False
    # 非正 PID → False
    assert sidecar._process_alive(0) is False


def test_parent_vanished_watches_env_shell_pid() -> None:
    # 打包场景：watch_pid 指向已退出的壳 → 判定壳已消失（即使 getppid 未变）
    assert sidecar._parent_vanished(os.getpid(), watch_pid=2_000_000_000) is True
    # 壳仍存活 → 未消失
    assert sidecar._parent_vanished(1234, watch_pid=os.getpid()) is False


def test_pick_free_port_scans_when_preferred_is_busy() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 8765))
    s.listen(1)
    try:
        chosen = sidecar.pick_free_port(8765, host="127.0.0.1")
        assert chosen != 8765
        assert 8765 <= chosen <= 8800
    finally:
        s.close()


def test_main_prints_ready_line(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    class FakeHandle:
        host = "127.0.0.1"
        port = 8765
        errors: list[BaseException] = []

        def wait_until_ready(self, timeout=None) -> bool:
            return True

    with (
        patch("biscuitbot.desktop.app.start_gateway", return_value=FakeHandle()),
        patch.object(sidecar, "_block_until_killed", lambda: None),
        redirect_stdout(io.StringIO()) as buf,
    ):
        sidecar.main(config=load_config(config_path))

    assert "BISCUITBOT_GATEWAY_READY 127.0.0.1 8765" in buf.getvalue()


def test_main_prints_error_line_on_startup_failure(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    class FailingHandle:
        host = "127.0.0.1"
        port = 8765
        errors = [RuntimeError("boom")]

        def wait_until_ready(self, timeout=None) -> bool:
            return False

    with (
        patch("biscuitbot.desktop.app.start_gateway", return_value=FailingHandle()),
        redirect_stdout(io.StringIO()) as buf,
    ):
        with pytest.raises(SystemExit) as excinfo:
            sidecar.main(config=load_config(config_path))

    assert excinfo.value.code == 1
    assert "BISCUITBOT_GATEWAY_ERROR" in buf.getvalue()
