"""Desktop application runner.

Starts the biscuitbot gateway in a daemon thread and opens a native pywebview
window that loads the WebUI.  When the window closes the process exits,
taking the daemon gateway thread with it.

The gateway start is factored out into :func:`start_gateway` so the Tauri
sidecar (:mod:`biscuitbot.desktop.sidecar`) can reuse the exact same runtime
without a window.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
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

# WebUI 由 websocket 频道提供（而非 gateway 健康检查端口），默认端口
_DEFAULT_WS_PORT = 8765


def _wait_for_port(host: str, port: int, timeout: float = _PORT_TIMEOUT_S) -> bool:
    """轮询直到端口可连接，返回是否就绪。

    探测时发送最小 HTTP 请求，让 websocket 服务器以正常响应结束连接，
    避免其在日志里记录“握手失败”的假错误堆栈。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (host, port), timeout=_PORT_CONNECT_TIMEOUT_S
            ) as sock:
                sock.sendall(
                    b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                sock.recv(1)
                return True
        except OSError:
            time.sleep(_PORT_POLL_INTERVAL_S)
    return False


def resolve_websocket_endpoint(config: Any) -> tuple[str, int]:
    """解析内嵌 WebUI 实际绑定的 host:port（由 websocket 频道提供）。

    与 ``biscuitbot gateway`` 命令一致：WebUI 走 ``config.channels.websocket``
    的 host/port，而不是 ``config.gateway`` 的健康检查端口。
    """
    ws_cfg = getattr(config.channels, "websocket", None) or {}
    if isinstance(ws_cfg, dict):
        host = ws_cfg.get("host") or config.gateway.host or "127.0.0.1"
        port = int(ws_cfg.get("port") or _DEFAULT_WS_PORT)
    else:
        host = getattr(ws_cfg, "host", None) or config.gateway.host or "127.0.0.1"
        port = int(getattr(ws_cfg, "port", None) or _DEFAULT_WS_PORT)
    return host, port


def _ensure_websocket_enabled(config: Any) -> bool:
    """确保 websocket 频道启用（它承载内嵌 WebUI）。

    返回是否修改了配置（调用方需要持久化）。
    """
    ws = getattr(config.channels, "websocket", None)
    if ws is None:
        setattr(config.channels, "websocket", {"enabled": True})
        return True
    if isinstance(ws, dict):
        if not ws.get("enabled", False):
            ws["enabled"] = True
            return True
    else:
        if not getattr(ws, "enabled", False):
            ws.enabled = True
            return True
    return False


def _set_websocket_port(config: Any, port: int) -> None:
    """把 websocket 频道端口写入 config（配合端口避让）。"""
    ws = getattr(config.channels, "websocket", None)
    if ws is None:
        setattr(config.channels, "websocket", {"enabled": True, "port": port})
    elif isinstance(ws, dict):
        ws["port"] = port
    else:
        ws.port = port


@dataclass
class GatewayHandle:
    """已启动的网关后台进程句柄，供调用方轮询就绪状态。"""

    host: str
    port: int
    thread: threading.Thread
    errors: list[BaseException] = field(default_factory=list)

    def wait_until_ready(self, timeout: float = _PORT_TIMEOUT_S) -> bool:
        """阻塞直到网关绑定的端口可连接。"""
        return _wait_for_port(self.host, self.port, timeout=timeout)


def start_gateway(config: Any, *, port: int | None = None) -> GatewayHandle:
    """在 daemon 线程中启动 biscuitbot 网关，返回就绪轮询句柄。

    与 ``run_desktop`` 共享同一运行时：不打开浏览器、WebUI 走静态 dist、
    运行时表面标记为 ``native``、不启用健康检查服务器。调用方需要自行
    ``wait_until_ready()`` 并最终退出进程以终止 daemon 线程。
    """
    from biscuitbot.cli.commands import _run_gateway

    host, ws_port = resolve_websocket_endpoint(config)
    gateway_port = port if port is not None else config.gateway.port
    errors: list[BaseException] = []

    def _gateway_target() -> None:
        try:
            _run_gateway(
                config,
                port=gateway_port,
                open_browser_url=None,
                webui_static_dist=True,
                webui_runtime_surface="native",
                health_server_enabled=False,
                allow_unconfigured_provider=True,
            )
        except SystemExit:
            pass  # typer.Exit
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(
        target=_gateway_target,
        name="biscuitbot-gateway",
        daemon=True,
    )
    thread.start()
    return GatewayHandle(host=host, port=ws_port, thread=thread, errors=errors)


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

    handle = start_gateway(config, port=port)
    url = f"http://{handle.host}:{handle.port}"

    if not handle.wait_until_ready():
        if handle.errors:
            exc = handle.errors[0]
            print(f"Gateway failed to start: {exc}", file=sys.stderr)
        else:
            print(
                f"Gateway did not bind within timeout. Try visiting {url} manually.",
                file=sys.stderr,
            )

    # 创建并运行原生窗口
    webview.create_window(
        title="biscuitbot",
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
