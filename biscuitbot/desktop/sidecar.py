"""Headless gateway sidecar entry point for the Tauri desktop shell.

Runs the biscuitbot gateway without any window and announces readiness on
stdout with a single handshake line the Tauri shell parses::

    BISCUITBOT_GATEWAY_READY <host> <port>

On startup failure it prints::

    BISCUITBOT_GATEWAY_ERROR <message>

The process then blocks forever; the Tauri shell kills it when the app quits.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any

from loguru import logger

# WebUI 由 websocket 频道提供；端口被占用时在此区间避让（8765 起向后扫描）。
_DEFAULT_WS_PORT = 8765
_PORT_SCAN_START = 8765
_PORT_SCAN_END = 8800

# sidecar 等待 gateway 绑定端口的时间上限
_READY_TIMEOUT_S = 30.0


def _port_free(host: str, port: int) -> bool:
    """尝试绑定以探测端口是否空闲（不真正监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_free_port(
    preferred: int,
    *,
    host: str = "127.0.0.1",
    start: int = _PORT_SCAN_START,
    end: int = _PORT_SCAN_END,
) -> int:
    """优先用 preferred；被占用则在 [start, end] 取第一个空闲端口。"""
    if preferred is not None and _port_free(host, preferred):
        return preferred
    for candidate in range(start, end + 1):
        if candidate == preferred:
            continue
        if _port_free(host, candidate):
            return candidate
    return preferred or start


def ensure_runtime(config: Any = None) -> Any:
    """初始化首启运行时：默认配置落盘、websocket 频道启用、工作区就绪。

    返回实际使用的 config 对象（首启时新建并落盘默认配置）。
    """
    from biscuitbot.config.loader import get_config_path, load_config, save_config
    from biscuitbot.desktop.app import _ensure_websocket_enabled

    if config is None:
        config = load_config()
    path = get_config_path()

    if not path.exists():
        save_config(config, path)  # 首启：把默认配置写入磁盘

    if _ensure_websocket_enabled(config):
        save_config(config, path)

    workspace = config.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    return config


def main(config: Any = None) -> None:
    """无头 sidecar 主入口：启动网关并打印握手行。"""
    from biscuitbot.desktop.app import resolve_websocket_endpoint, start_gateway

    try:
        config = ensure_runtime(config)
        host, preferred_port = resolve_websocket_endpoint(config)
        port = pick_free_port(preferred_port, host=host)

        from biscuitbot.desktop.app import _set_websocket_port

        if port != preferred_port:
            # 端口冲突：让网关绑定我们探测到的空闲端口
            _set_websocket_port(config, port)

        handle = start_gateway(config)
        if handle.wait_until_ready(timeout=_READY_TIMEOUT_S):
            print(f"BISCUITBOT_GATEWAY_READY {handle.host} {handle.port}", flush=True)
        else:
            if handle.errors:
                raise RuntimeError(f"gateway failed to start: {handle.errors[0]}")
            raise RuntimeError(
                f"gateway did not bind within timeout (http://{handle.host}:{handle.port})"
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("sidecar startup failed")
        print(f"BISCUITBOT_GATEWAY_ERROR {exc}", flush=True)
        raise SystemExit(1) from exc

    # 打印 READY 后保持进程存活：daemon gateway 线程运行到被 Tauri 壳杀掉为止。
    _block_until_killed()


def _watch_pid() -> int | None:
    """返回需要监控的宿主进程 PID。

    打包场景：Tauri 壳通过环境变量 ``BISCUITBOT_PARENT_PID`` 传入自身 PID。
    这是必要的——PyInstaller onefile 是“bootstrap 父进程 → 运行时子进程”
    的两级结构，本进程的 ``getppid()`` 指向 bootstrap 而非壳，单纯靠
    reparent 检测永远不触发。环境变量会随 bootstrap 继承给运行时子进程。
    未设置（``biscuitbot sidecar`` 直接跑）时返回 None，退化为 getppid 检测。
    """
    raw = os.environ.get("BISCUITBOT_PARENT_PID")
    if raw and raw.isdigit():
        return int(raw)
    return None


def _process_alive(pid: int) -> bool:
    """以 signal 0 探测进程是否存在（跨平台，无需权限）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存活但归属其他用户
    except OSError:
        return False


def _parent_vanished(original_parent_pid: int, watch_pid: int | None = None) -> bool:
    """判断宿主（Tauri 壳）是否已退出。

    - ``watch_pid`` 非空（打包场景）：直接探测该 PID 是否存活。
    - ``watch_pid`` 为空（未打包 CLI）：壳退出后本进程会被 reparent 到
      launchd/init（getppid() 变为 1），据此判定。
    """
    if watch_pid is not None:
        return not _process_alive(watch_pid)
    try:
        return os.getppid() != original_parent_pid
    except OSError:
        # Windows 下父进程退出后 getppid 可能抛错：视为已退出
        return True


def _block_until_killed() -> None:
    """阻塞主线程直到进程被终止（或收到 KeyboardInterrupt）。

    每 5 秒检查宿主 PID：若 Tauri 壳被强杀（无法走优雅退出清理），
    本进程也应随之退出，保证不留后台进程。
    """
    _keep_alive = threading.Event()
    original_parent_pid = os.getppid()
    watch_pid = _watch_pid()
    try:
        while not _keep_alive.wait(5.0):
            if _parent_vanished(original_parent_pid, watch_pid):
                return
    except KeyboardInterrupt:
        pass
