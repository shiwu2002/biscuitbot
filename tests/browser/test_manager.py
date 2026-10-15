"""内置浏览器服务（xianaibot.browser）单元测试。

全部离线：不真的启动浏览器，CDP 探测通过 monkeypatch 替身。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from xianaibot.browser.manager import BrowserServiceManager, get_browser_manager


@pytest.fixture(autouse=True)
def _hermetic_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """隔离内嵌端口文件与桌面壳环境，保证测试不读本机真实状态。"""
    port_file = tmp_path / "browser-cdp-port"
    monkeypatch.setattr("xianaibot.browser.manager._EMBEDDED_PORT_FILE", port_file)
    monkeypatch.delenv("XIANAIBOT_PARENT_PID", raising=False)
    return port_file


def _write_port_file(path: Path, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")


def test_get_browser_manager_is_process_singleton() -> None:
    assert get_browser_manager() is get_browser_manager()


def test_initial_status_is_stopped() -> None:
    manager = BrowserServiceManager()
    status = asyncio.run(manager.status())
    assert status["running"] is False
    assert status["mode"] == "stopped"
    assert status["port"] is None
    assert status["endpoint"] is None


def test_status_detects_external_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """9222 上有 CDP 服务 → external，端口被记住。"""
    manager = BrowserServiceManager()
    monkeypatch.setattr(
        "xianaibot.browser.manager._probe_cdp",
        _probe_stub({9222: True}),
    )
    status = asyncio.run(manager.status())
    assert status["running"] is True
    assert status["mode"] == "external"
    assert status["port"] == 9222
    assert status["endpoint"] == "http://127.0.0.1:9222"


def test_status_prefers_env_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """XIANAIBOT_BROWSER_CDP_PORT 优先于默认探测列表。"""
    manager = BrowserServiceManager()
    monkeypatch.setenv("XIANAIBOT_BROWSER_CDP_PORT", "9333")
    monkeypatch.setattr(
        "xianaibot.browser.manager._probe_cdp",
        _probe_stub({9333: True}),
    )
    status = asyncio.run(manager.status())
    assert status["port"] == 9333


def test_stop_keeps_external_browser_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() 只关自己拉起的实例：external 模式下不清端口、不报错。"""
    manager = BrowserServiceManager()
    manager._mode = "external"
    manager._port = 9222
    status = asyncio.run(manager.stop())
    assert status["running"] is True
    assert status["mode"] == "external"
    assert status["port"] == 9222


def test_stop_kills_managed_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """managed 模式：进程被 terminate，状态回到 stopped。"""

    class FakeProc:
        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

    proc = FakeProc()
    manager = BrowserServiceManager()
    manager._proc = proc  # type: ignore[assignment]
    manager._mode = "managed"
    manager._port = 9222
    status = asyncio.run(manager.stop())
    assert proc.terminated is True
    assert manager._proc is None
    assert status["running"] is False
    assert status["mode"] == "stopped"


def test_ensure_started_reuses_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测到外部浏览器：不拉起进程，直接标记 external。"""
    manager = BrowserServiceManager()
    launched: list[str] = []

    monkeypatch.setattr(
        "xianaibot.browser.manager._probe_cdp",
        _probe_stub({9222: True}),
    )
    monkeypatch.setattr(
        manager, "_launch_managed", _record_launch(launched, port=None)
    )
    monkeypatch.setattr(
        manager,
        "_attach_best_effort",
        _noop_async(),
    )
    status = asyncio.run(manager.ensure_started())
    assert status["mode"] == "external"
    assert status["port"] == 9222
    assert launched == []  # 没有拉起托管实例


def test_ensure_started_launches_when_no_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测不到外部浏览器：走 _launch_managed 托管路径。"""
    manager = BrowserServiceManager()
    monkeypatch.setattr("xianaibot.browser.manager._probe_cdp", _probe_stub({}))
    monkeypatch.setattr(manager, "_launch_managed", _record_launch(launched := [], port=9222))
    monkeypatch.setattr(manager, "_attach_best_effort", _noop_async())
    status = asyncio.run(manager.ensure_started())
    assert launched == ["managed"]
    assert status["mode"] == "managed"
    assert status["port"] == 9222


def test_dispatch_input_unknown_action_raises() -> None:
    manager = BrowserServiceManager()
    manager._page_ws = object()  # 绕过未连接守卫，专测未知动作分支
    with pytest.raises(Exception, match="unknown_input_action"):
        asyncio.run(manager.dispatch_input("teleport", {}))


def test_status_detects_embedded_browser(
    _hermetic_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口文件存在且 CDP 活着 → embedded 优先于 external 探测。"""
    _write_port_file(_hermetic_env, 9333)
    manager = BrowserServiceManager()
    monkeypatch.setattr(
        "xianaibot.browser.manager._probe_cdp",
        _probe_stub({9222: True, 9333: True}),
    )
    status = asyncio.run(manager.status())
    assert status["running"] is True
    assert status["mode"] == "embedded"
    assert status["port"] == 9333


def test_status_ignores_stale_embedded_port_file(
    _hermetic_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口文件存在但 CDP 已死（窗口被关、文件残留）→ 视为未运行。"""
    _write_port_file(_hermetic_env, 9333)
    manager = BrowserServiceManager()
    monkeypatch.setattr("xianaibot.browser.manager._probe_cdp", _probe_stub({}))
    status = asyncio.run(manager.status())
    assert status["running"] is False
    assert status["mode"] == "stopped"


def test_ensure_started_uses_embedded_file(
    _hermetic_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内嵌窗口端口文件就绪：直接附着 embedded，不走 external/managed。"""
    _write_port_file(_hermetic_env, 9444)
    manager = BrowserServiceManager()
    launched: list[str] = []
    monkeypatch.setattr(
        "xianaibot.browser.manager._probe_cdp",
        _probe_stub({9444: True, 9222: True}),
    )
    monkeypatch.setattr(manager, "_launch_managed", _record_launch(launched, port=9555))
    monkeypatch.setattr(manager, "_attach_best_effort", _noop_async())
    status = asyncio.run(manager.ensure_started())
    assert status["mode"] == "embedded"
    assert status["port"] == 9444
    assert launched == []


def test_ensure_started_requests_embedded_window_on_desktop(
    _hermetic_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """桌面端内嵌窗口未开：广播打开请求，端口文件出现后附着 embedded。"""
    monkeypatch.setenv("XIANAIBOT_PARENT_PID", "12345")
    manager = BrowserServiceManager()
    requests: list[bool] = []

    def _fake_webui_opens_window() -> None:
        """模拟 WebUI 收到 browser_request 后 invoke 壳命令开窗。"""
        requests.append(True)
        _write_port_file(_hermetic_env, 9666)

    monkeypatch.setattr("xianaibot.browser.manager._probe_cdp", _probe_stub({9666: True}))
    monkeypatch.setattr(manager, "_embedded_request", _fake_webui_opens_window)
    monkeypatch.setattr(manager, "_launch_managed", _record_launch([], port=None))
    monkeypatch.setattr(manager, "_attach_best_effort", _noop_async())
    status = asyncio.run(manager.ensure_started())
    assert requests == [True]
    assert status["mode"] == "embedded"
    assert status["port"] == 9666


def test_stop_embedded_only_detaches() -> None:
    """embedded 模式：stop 只断开 CDP，不 kill（窗口归 Tauri 壳管）。"""
    manager = BrowserServiceManager()
    manager._mode = "embedded"
    manager._port = 9333
    status = asyncio.run(manager.stop())
    assert manager._proc is None
    assert status["running"] is False
    assert status["mode"] == "stopped"


def test_dispatch_input_requires_connection() -> None:
    manager = BrowserServiceManager()
    with pytest.raises(Exception, match="page_not_connected"):
        asyncio.run(manager.dispatch_input("mouse", {}))


def test_broadcast_drops_stale_frames_for_slow_subscribers() -> None:
    """订阅队列满时丢最旧的帧，保持实时。"""
    manager = BrowserServiceManager()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1)
    manager._listeners.add(queue)
    manager._broadcast({"type": "frame", "data": "a"})
    manager._broadcast({"type": "frame", "data": "b"})
    assert queue.qsize() == 1
    assert queue.get_nowait()["data"] == "b"


# -- 替身工具 ---------------------------------------------------------------


def _probe_stub(alive: dict[int, bool]) -> Any:
    async def _probe(port: int) -> bool:
        return alive.get(port, False)

    return _probe


def _record_launch(recorder: list[str], *, port: int | None) -> Any:
    async def _launch() -> int | None:
        recorder.append("managed")
        return port

    return _launch


def _noop_async() -> Any:
    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    return _noop
