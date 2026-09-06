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
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from biscuitbot.config.loader import load_config, save_config
from biscuitbot.config.schema import Config
from biscuitbot.desktop import app as desktop_app
from biscuitbot.desktop import sidecar


def _free_port() -> int:
    """Return an ephemeral port that is currently free on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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
    free = _free_port()
    assert sidecar.pick_free_port(free, host="127.0.0.1", start=free, end=free + 5) == free


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
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    try:
        chosen = sidecar.pick_free_port(port, host="127.0.0.1", start=port, end=port + 10)
        assert chosen != port
        assert port <= chosen <= port + 10
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


def test_install_gateway_file_logging_writes_gateway_log(tmp_path, monkeypatch) -> None:
    """桌面网关应把日志写入数据目录 logs/gateway.log（sidecar 无控制台）。

    否则「系统 IO」面板的打开日志 / 导出诊断拿不到任何日志。验证文件确实落盘，
    并在测试结束后移除 sink、重置全局状态，避免污染后续用例的 loguru logger。
    """
    from loguru import logger as loguru_logger

    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    desktop_app._FILE_LOG_SINK_ID = None
    desktop_app._install_gateway_file_logging()
    assert desktop_app._FILE_LOG_SINK_ID is not None

    loguru_logger.info("desktop-io-test-marker")
    loguru_logger.complete()  # 等待 enqueue 队列落盘

    log_file = tmp_path / "logs" / "gateway.log"
    assert log_file.exists()
    assert "desktop-io-test-marker" in log_file.read_text(encoding="utf-8")

    # 清理：移除 sink 并复位全局状态。
    loguru_logger.remove(desktop_app._FILE_LOG_SINK_ID)
    desktop_app._FILE_LOG_SINK_ID = None


class TestPythonInterpreterMode:
    """桌面 exec 解释器模式：-c / -m / 脚本路径 / -V，异常归一为退出码。"""

    def test_version_flag(self, capsys) -> None:
        assert sidecar._run_python_interpreter(["-V"]) == 0
        assert "Python" in capsys.readouterr().out

    def test_c_executes_code(self, capsys) -> None:
        assert sidecar._run_python_interpreter(["-c", "print(6*7)"]) == 0
        assert capsys.readouterr().out.strip() == "42"

    def test_c_sets_argv(self, capsys) -> None:
        assert (
            sidecar._run_python_interpreter(
                ["-c", "import sys; print(sys.argv[1])", "hello"]
            )
            == 0
        )
        assert capsys.readouterr().out.strip() == "hello"

    def test_c_syntax_error_returns_1(self, capsys) -> None:
        assert sidecar._run_python_interpreter(["-c", "x = "]) == 1
        assert "SyntaxError" in capsys.readouterr().err

    def test_c_system_exit_code_passthrough(self) -> None:
        assert sidecar._run_python_interpreter(["-c", "raise SystemExit(3)"]) == 3

    def test_module_mode(self, capsys) -> None:
        # stdlib json.tool 的 --help 走 argparse → SystemExit(0)
        assert sidecar._run_python_interpreter(["-m", "json.tool", "--help"]) == 0
        assert "usage:" in capsys.readouterr().out

    def test_script_path(self, tmp_path, capsys) -> None:
        script = tmp_path / "t.py"
        script.write_text("print('script-ok')", encoding="utf-8")
        assert sidecar._run_python_interpreter([str(script)]) == 0
        assert capsys.readouterr().out.strip() == "script-ok"

    def test_missing_script_returns_1(self, capsys) -> None:
        assert sidecar._run_python_interpreter(["/no/such/file.py"]) == 1

    def test_marker_intercepts_before_gateway(self, monkeypatch, capsys) -> None:
        """main() 命中魔术标记时直接解释执行，不启动网关。"""
        monkeypatch.setattr(
            sys,
            "argv",
            ["biscuitbot-sidecar", sidecar._PYTHON_MODE_MARKER, "-c", "print('early')"],
        )
        with pytest.raises(SystemExit) as excinfo:
            sidecar.main()
        assert excinfo.value.code == 0
        assert "early" in capsys.readouterr().out

    def test_system_exit_code_helper(self) -> None:
        assert sidecar._system_exit_code(SystemExit(0)) == 0
        assert sidecar._system_exit_code(SystemExit(7)) == 7
        assert sidecar._system_exit_code(SystemExit(None)) == 0
        assert sidecar._system_exit_code(SystemExit("msg")) == 1
