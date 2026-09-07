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
import runpy
import socket
import sys
import threading
import time
import traceback
from typing import Any

from loguru import logger

# WebUI 由 websocket 频道提供；端口被占用时在此区间避让（8765 起向后扫描）。
_DEFAULT_WS_PORT = 8765
_PORT_SCAN_START = 8765
_PORT_SCAN_END = 8800

# sidecar 等待 gateway 绑定端口的时间上限
_READY_TIMEOUT_S = 30.0

# 解释器模式的魔术首参：exec 生成的 python/pip shim 以
# ``<本可执行> __biscuitbot_python__ <原始参数>`` 调用，命中后本进程充当
# 通用 Python 解释器（复用打包进 bundle 的标准库与第三方依赖），而非启动网关。
_PYTHON_MODE_MARKER = "__biscuitbot_python__"


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


def _system_exit_code(exc: SystemExit) -> int:
    """把 SystemExit 归一为进程退出码（None → 0，str → 1）。"""
    if isinstance(exc.code, int):
        return exc.code
    return 0 if exc.code is None else 1


def _run_python_interpreter(argv: list[str]) -> int:
    """以通用 Python 解释器模式执行请求（桌面 exec 落到打包解释器）。

    行为对齐 CPython 常见调用形态：
      - ``-c <code>``      执行代码片段
      - ``-m <module>``    运行模块（pip 等）
      - ``-V/--version``   打印版本
      - 其余             视为脚本路径（runpy.run_path）
    异常打 traceback 到 stderr 并返回 1，正常返回 0。
    """
    if not argv or argv[0] in ("-V", "--version"):
        print(f"Python {sys.version.split()[0]}")
        return 0

    if argv[0] == "-c":
        if len(argv) < 2:
            print("python: -c requires an argument", file=sys.stderr)
            return 2
        sys.argv = [""] + argv[2:]
        try:
            exec(
                compile(argv[1], "<string>", "exec"),
                {"__name__": "__main__", "__file__": "<string>"},
            )
        except SystemExit as exc:
            return _system_exit_code(exc)
        except BaseException:
            traceback.print_exc()
            return 1
        return 0

    if argv[0] in ("-m", "--module"):
        if len(argv) < 2:
            print("python: -m requires an argument", file=sys.stderr)
            return 2
        sys.argv = argv
        try:
            runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        except SystemExit as exc:
            return _system_exit_code(exc)
        except BaseException:
            traceback.print_exc()
            return 1
        return 0

    # 其余：视为脚本路径
    sys.argv = argv
    try:
        runpy.run_path(argv[0], run_name="__main__")
    except SystemExit as exc:
        return _system_exit_code(exc)
    except BaseException:
        traceback.print_exc()
        return 1
    return 0


def main(config: Any = None) -> None:
    """无头 sidecar 主入口：启动网关并打印握手行。"""
    if len(sys.argv) > 1 and sys.argv[1] == _PYTHON_MODE_MARKER:
        sys.exit(_run_python_interpreter(sys.argv[2:]))

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

    打包场景：Tauri 壳通过环境变量 ``BISCUITBOT_PARENT_PID`` 传入自身 PID，
    看门狗据此在壳被强杀时自清理。onefile 打包时这是必须的——它是
    “bootstrap 父进程 → 运行时子进程”两级结构，本进程的 ``getppid()`` 指向
    bootstrap 而非壳，单纯靠 reparent 检测永远不触发。onedir 打包下单级进程，
    ``getppid()`` 已直接指向壳，但保留该环境变量可避免依赖具体打包方式，更稳。
    未设置（``biscuitbot sidecar`` 直接跑）时返回 None，退化为 getppid 检测。
    """
    raw = os.environ.get("BISCUITBOT_PARENT_PID")
    if raw and raw.isdigit():
        return int(raw)
    return None


def _windows_process_alive(pid: int) -> bool:
    """Windows 存活探测，不依赖调用进程是否持有控制台。

    打包后的 sidecar 是 ``--windowed``（无控制台）进程。``os.kill(pid, 0)`` 在
    Windows 上 signal 0 即 ``CTRL_C_EVENT``，最终走 ``GenerateConsoleCtrlEvent``，
    在无控制台进程里对**任意** PID（无论死活）都会失败 ``ERROR_INVALID_HANDLE``，
    因此不能用作存活探测。这里改用 ``OpenProcess(SYNCHRONIZE)`` +
    ``WaitForSingleObject``，与控制台无关。

    仅在进程**确定**已退出（``OpenProcess`` 报 ``ERROR_INVALID_PARAMETER``，或句柄
    已置信号）时返回 False；任何含糊的失败都返回 True——对看门狗而言，误判“已死”
    会提前杀掉 sidecar，而误判“仍活”最多留下无害的孤儿进程。
    """
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    ERROR_INVALID_PARAMETER = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            return False
        # 访问被拒或其它错误：保守视为存活。
        return True
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == WAIT_OBJECT_0:
            return False
        return True  # WAIT_TIMEOUT（仍在运行）或 WAIT_FAILED（保守视为存活）
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    """返回 PID 对应进程是否仍存活。

    POSIX 下 ``os.kill(pid, 0)`` 是标准存活探测；Windows 下不能用它（signal 0
    是 ``CTRL_C_EVENT``，无控制台进程必然失败），改走 :func:`_windows_process_alive`。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
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

    每 5 秒检查宿主 PID：壳进程**存活**（包括关闭窗口后最小化到托盘、仍在后台
    跑自动化任务）时保持运行；仅当壳被强杀/真正退出（无法走优雅退出清理）时，
    本进程也随之退出，保证不留后台进程。

    启动后 15 秒为宽限期：sidecar 刚打印 READY 时，Tauri 壳可能还在初始化
    WebView / 托盘 / 窗口等组件，PID 探测（尤其 Windows 上 ``OpenProcess``
    或 macOS 上 ``os.kill(pid,0)``）在极少数竞态下可能瞬时误判壳已死。
    如果此时立即 return，sidecar 退出 → 壳收到 Terminated → ready=true →
    触发 Respawn → 新 sidecar 又 15s 内误判 → 无限循环（CPU 飙升）。
    宽限期内跳过父进程检测，确保壳初始化完成后再开始看门狗轮询。
    """
    _keep_alive = threading.Event()
    original_parent_pid = os.getppid()
    watch_pid = _watch_pid()
    startup_deadline = time.monotonic() + 15.0
    try:
        while not _keep_alive.wait(5.0):
            # 启动宽限期内不检测父进程存活，避免壳初始化阶段的瞬时竞态
            if time.monotonic() < startup_deadline:
                continue
            if _parent_vanished(original_parent_pid, watch_pid):
                return
    except KeyboardInterrupt:
        pass
