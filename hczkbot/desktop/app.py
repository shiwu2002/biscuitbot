"""Desktop application runner.

Starts the hczkbot gateway in a daemon thread and opens a native pywebview
window that loads the WebUI.  When the window closes the process exits,
taking the daemon gateway thread with it.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from typing import Any

# 窗口默认尺寸
_DEFAULT_WIDTH = 1200
_DEFAULT_HEIGHT = 800
_DEFAULT_MIN_WIDTH = 800
_DEFAULT_MIN_HEIGHT = 600

# 端口就绪轮询
_PORT_TIMEOUT_S = 20.0
_PORT_POLL_INTERVAL_S = 0.15
_PORT_CONNECT_TIMEOUT_S = 0.5


def _wait_for_port(host: str, port: int, timeout: float = _PORT_TIMEOUT_S) -> bool:
    """轮询直到 gateway 端口可连接，返回是否就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=_PORT_CONNECT_TIMEOUT_S):
                return True
        except OSError:
            time.sleep(_PORT_POLL_INTERVAL_S)
    return False


def run_desktop(
    config: Any,
    *,
    port: int | None = None,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    min_width: int = _DEFAULT_MIN_WIDTH,
    min_height: int = _DEFAULT_MIN_HEIGHT,
) -> None:
    """
    启动 gateway 后台线程，并打开原生窗口加载 WebUI。

    gateway 运行在 daemon 线程中，窗口关闭后主线程退出，
    daemon 线程随之终止。
    """
    import webview

    from hczkbot.cli.commands import _run_gateway

    host = config.gateway.host or "127.0.0.1"
    port = port if port is not None else config.gateway.port

    # gateway 异常捕获
    gateway_errors: list[BaseException] = []

    def _gateway_target() -> None:
        try:
            _run_gateway(
                config,
                port=port,
                open_browser_url=None,
                webui_static_dist=True,
                webui_runtime_surface="native",
                health_server_enabled=False,
            )
        except SystemExit:
            pass  # typer.Exit
        except BaseException as exc:  # noqa: BLE001
            gateway_errors.append(exc)

    gateway_thread = threading.Thread(
        target=_gateway_target,
        name="hczkbot-gateway",
        daemon=True,
    )
    gateway_thread.start()

    # 等待端口就绪
    url = f"http://{host}:{port}"
    if not _wait_for_port(host, port):
        if gateway_errors:
            exc = gateway_errors[0]
            print(f"Gateway failed to start: {exc}", file=sys.stderr)
        else:
            print(
                f"Gateway did not bind within timeout. Try visiting {url} manually.",
                file=sys.stderr,
            )

    # 创建并运行原生窗口
    webview.create_window(
        title="hczkbot",
        url=url,
        width=width,
        height=height,
        min_size=(min_width, min_height),
        text_select=True,
    )

    # webview.start() 阻塞直到窗口关闭
    webview.start()

    # 窗口已关闭 —— 退出进程（daemon gateway 线程随之终止）
    os._exit(0)
