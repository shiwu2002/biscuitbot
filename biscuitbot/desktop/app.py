"""Headless gateway runtime for the desktop shell.

Starts the biscuitbot gateway in a daemon thread without opening any window;
the Tauri sidecar (:mod:`biscuitbot.desktop.sidecar`) reuses this runtime to
serve the WebUI inside the shell's system WebView.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

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


# 桌面网关文件日志 sink 的全局 ID；仅安装一次（loguru add 是全局副作用）。
_FILE_LOG_SINK_ID: int | None = None


def _install_gateway_file_logging() -> None:
    """把 loguru 日志同时写入数据目录 logs/gateway.log。

    桌面 sidecar 是 ``--windowed`` 无控制台进程，loguru 默认的 stderr sink 会被
    系统丢弃，导致「系统 IO」面板的打开日志/导出诊断拿不到任何日志。这里补一个
    文件 sink（旋转 + 保留），供排障与诊断报告使用。CLI 网关因有控制台不受影响。
    """
    global _FILE_LOG_SINK_ID
    if _FILE_LOG_SINK_ID is not None:
        return
    from biscuitbot.config.paths import get_logs_dir
    from loguru import logger

    logs_dir = get_logs_dir()
    _FILE_LOG_SINK_ID = logger.add(
        str(logs_dir / "gateway.log"),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <5} | "
            "{extra[channel]} | {message}"
        ),
        rotation="10 MB",
        retention="10 days",
        level="INFO",
        enqueue=True,  # 后台线程写盘，避免阻塞网关主循环
        filter=lambda record: record["extra"].setdefault("channel", "-") or True,
    )


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

    供 Tauri sidecar 复用：不打开浏览器、WebUI 走静态 dist、运行时表面
    标记为 ``native``、不启用健康检查服务器。调用方需要自行
    ``wait_until_ready()`` 并最终退出进程以终止 daemon 线程。
    """
    from biscuitbot.cli.commands import _run_gateway

    # 桌面无控制台：先把日志落盘，供「系统 IO」面板的打开日志 / 导出诊断使用。
    _install_gateway_file_logging()

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
