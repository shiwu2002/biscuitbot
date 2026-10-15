"""浏览器服务管理器：探测/附着常驻浏览器，并提供极简 CDP 客户端。

设计要点：

- **内嵌优先（桌面端）**：Tauri 壳的「内置浏览器」窗口（WebView2 独立环境）
  会把 CDP 调试端口写入 ``~/.xianaibot/browser-cdp-port``。管理器优先读该
  文件附着内嵌窗口（mode=embedded）：用户与智能体同屏共享同一个原生渲染
  的浏览器窗口与登录态，零截图流开销。桌面端内嵌窗口未打开时，经
  ``set_embedded_request`` 回调广播请求，WebUI 收到后 invoke 壳命令打开
  窗口，管理器轮询端口文件直至就绪。
- **复用优先**：探测本机调试端口（默认 9222）上是否已有浏览器的 CDP
  服务；有就直接挂上去（mode=external），绝不关掉用户自己开的浏览器。
- **常驻托管**：都没有时拉起本应用专属实例（独立
  ``~/.xianaibot/browser-profile``，默认 headless），mode=managed。登录态
  持久化在专属 profile 里，gateway 重启后浏览器仍在，直接重连。
- **单页面连接**：管理器同一时刻只连一个"活动标签页"的 CDP WebSocket，
  标签页切换就是换一条连接；``Page.startScreencast`` 仅在有人订阅时开启
  （无头托管模式的监控降级，桌面端不用截图流）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import websockets
from loguru import logger

DEFAULT_CDP_PORT = 9222
_PROBE_PORTS = [9222, 9223, 9224, 9225]
_CDP_READY_TIMEOUT_S = 15.0
_PROFILE_DIR = Path.home() / ".xianaibot" / "browser-profile"
_ACTIVITY_THROTTLE_S = 3.0
# Tauri 壳「内置浏览器」窗口写入的 CDP 端口文件（窗口关闭时由壳删除）。
_EMBEDDED_PORT_FILE = Path.home() / ".xianaibot" / "browser-cdp-port"
# 请求 WebUI 打开内嵌浏览器窗口后，等待端口文件出现的超时。
_EMBEDDED_REQUEST_TIMEOUT_S = 15.0


def _read_embedded_port() -> int | None:
    """读取内嵌浏览器窗口的 CDP 端口；文件缺失/非法返回 None。"""
    try:
        raw = _EMBEDDED_PORT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _is_desktop_shell() -> bool:
    """是否运行在桌面壳（Tauri sidecar）下：壳会注入 XIANAIBOT_PARENT_PID。"""
    return bool(os.environ.get("XIANAIBOT_PARENT_PID", "").strip())


def _cdp_port_env() -> int | None:
    raw = os.environ.get("XIANAIBOT_BROWSER_CDP_PORT", "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _browser_service_settings() -> tuple[int | None, bool]:
    """读取 (cdp_port, headless)：环境变量优先于 config.json 的 browser_service 段。"""
    port = _cdp_port_env()
    headless: bool | None = None
    raw_headless = os.environ.get("XIANAIBOT_BROWSER_HEADLESS", "").strip().lower()
    if raw_headless:
        headless = raw_headless not in {"0", "false", "no"}
    try:
        from xianaibot.config.loader import load_config

        cfg = load_config().tools.browser_service
        if port is None and cfg.cdp_port:
            port = int(cfg.cdp_port)
        if headless is None:
            headless = bool(cfg.headless)
    except Exception:  # noqa: BLE001 - 配置读不到时回退默认值
        pass
    return port, headless if headless is not None else True


def _find_browser_executable() -> str | None:
    """按优先级找本机的 Chrome/Edge 可执行文件。"""
    override = os.environ.get("XIANAIBOT_BROWSER_EXECUTABLE", "").strip()
    if override and Path(override).is_file():
        return override
    candidates: list[str] = []
    if sys.platform == "win32":
        program_dirs = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LocalAppData", ""),
        ]
        for base in filter(None, program_dirs):
            candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
            candidates.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
    else:
        candidates.extend(["google-chrome", "google-chrome-stable", "microsoft-edge"])
    for path in candidates:
        if Path(path).is_file():
            return path
    for name in ("chrome", "google-chrome", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


async def _probe_cdp(port: int) -> bool:
    """探测 127.0.0.1:{port} 是否是活的 CDP 服务。"""
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/version")
            return resp.status_code == 200 and "Browser" in resp.text
    except Exception:
        return False


class BrowserError(Exception):
    """面向 WebUI 的浏览器服务错误（message 为稳定错误码）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _CdpCommandError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class BrowserServiceManager:
    """管理常驻浏览器实例与活动标签页的 CDP 连接。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._monitor_lock = asyncio.Lock()
        self._proc: subprocess.Popen[Any] | None = None
        self._mode: str = "stopped"  # stopped | embedded | external | managed
        self._port: int | None = None
        self._page_ws: Any | None = None
        self._page_id: str | None = None
        self._page_url: str | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._cmd_seq = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._listeners: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._latest_frame: dict[str, Any] | None = None
        self._screencast_on = False
        # 浏览器级监视连接（Target 域）：感知智能体在共享浏览器里的页面活动
        self._monitor_ws: Any | None = None
        self._monitor_port: int | None = None
        self._activity_notifier: Any | None = None  # 同步回调 callable(dict)
        self._last_activity: tuple[str, float] = ("", 0.0)
        # 内嵌窗口打开请求回调（同步 callable，无参）：由 websocket 通道注册，
        # 广播 browser_request 事件给 WebUI，前端 invoke 壳命令打开窗口。
        self._embedded_request: Any | None = None

    # -- 状态 -----------------------------------------------------------------

    def _status_payload(self, *, type_field: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "running": self._mode in {"embedded", "external", "managed"},
            "mode": self._mode,
            "port": self._port,
            "endpoint": f"http://127.0.0.1:{self._port}" if self._port else None,
            "url": self._page_url,
            "has_frame": self._latest_frame is not None,
        }
        if type_field:
            payload["type"] = "status"
        return payload

    async def status(self) -> dict[str, Any]:
        """当前状态（只读探测，不拉起浏览器）。"""
        # 内嵌窗口优先：端口文件存在且 CDP 活着 → embedded
        embedded_port = _read_embedded_port()
        if embedded_port is not None and await _probe_cdp(embedded_port):
            if self._port != embedded_port or self._mode != "embedded":
                self._teardown_connection()
                self._mode = "embedded"
                self._port = embedded_port
                await self._attach_best_effort()
            self._spawn(self._ensure_monitor())
            return self._status_payload()
        if self._port is not None and await _probe_cdp(self._port):
            self._spawn(self._ensure_monitor())
            return self._status_payload()
        # 未连接：看用户是否新开了带调试端口的浏览器（并行探测，串行最坏 5×timeout）
        alive = await self._probe_ports_parallel()
        if alive is not None:
            self._mode = "external"
            self._port = alive
            self._spawn(self._ensure_monitor())
            return self._status_payload()
        self._mode = "stopped"
        self._port = None
        self._spawn(self._close_monitor())
        return self._status_payload()

    async def _probe_ports_parallel(self) -> int | None:
        """并行探测候选端口，返回第一个活着的 CDP 端口（按优先序）。"""
        results = await asyncio.gather(
            *(_probe_cdp(port) for port in self._probe_ports()),
            return_exceptions=True,
        )
        for port, ok in zip(self._probe_ports(), results):
            if ok is True:
                return port
        return None

    def _probe_ports(self) -> list[int]:
        configured_port, _ = _browser_service_settings()
        preferred = configured_port or DEFAULT_CDP_PORT
        if preferred in _PROBE_PORTS:
            return list(_PROBE_PORTS)
        return [preferred, *_PROBE_PORTS]

    # -- 启动 / 停止 -----------------------------------------------------------

    def set_embedded_request(self, callback: Any) -> None:
        """注册内嵌窗口打开请求回调（同步 callable，无参）。

        桌面端 WebUI 收到对应的 browser_request 事件后 invoke 壳命令
        ``open_browser_window``，壳创建 WebView2 窗口并写端口文件。
        """
        self._embedded_request = callback

    def _request_embedded_open(self) -> None:
        if self._embedded_request is None:
            return
        try:
            self._embedded_request()
        except Exception:  # noqa: BLE001 - 请求失败走 managed 兜底
            pass

    async def _wait_embedded_port(self, timeout_s: float) -> int | None:
        """请求打开内嵌窗口后，轮询端口文件直至 CDP 就绪。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            await asyncio.sleep(0.5)
            port = _read_embedded_port()
            if port is not None and await _probe_cdp(port):
                return port
        return None

    async def ensure_started(self) -> dict[str, Any]:
        """确保有可用的浏览器：内嵌窗口 → 请求打开 → 复用外部 → 拉起常驻实例。"""
        async with self._lock:
            if self._port is not None and await _probe_cdp(self._port):
                await self._attach_best_effort()
                self._spawn(self._ensure_monitor())
                return self._status_payload()

            self._teardown_connection()
            self._mode = "stopped"
            self._port = None

            # 1) 内嵌浏览器窗口（桌面端 WebView2，原生渲染、与用户同屏共享）
            embedded_port = _read_embedded_port()
            if embedded_port is not None and await _probe_cdp(embedded_port):
                self._mode = "embedded"
                self._port = embedded_port
                logger.info("browser service: attached to embedded browser on port {}", embedded_port)
                await self._attach_best_effort()
                self._spawn(self._ensure_monitor())
                return self._status_payload()

            # 2) 桌面端内嵌窗口未开：请 WebUI 打开（agent 触发浏览器自动化时
            #    自动弹出内嵌窗口），等待端口文件出现
            if _is_desktop_shell():
                self._request_embedded_open()
                embedded_port = await self._wait_embedded_port(_EMBEDDED_REQUEST_TIMEOUT_S)
                if embedded_port is not None:
                    self._mode = "embedded"
                    self._port = embedded_port
                    logger.info("browser service: embedded browser opened on port {}", embedded_port)
                    await self._attach_best_effort()
                    self._spawn(self._ensure_monitor())
                    return self._status_payload()
                logger.info("browser service: embedded browser request timed out, falling back")

            # 3) 复用用户自己开的带调试端口的浏览器
            external_port = await self._find_external_port()
            if external_port is not None:
                self._mode = "external"
                self._port = external_port
                logger.info("browser service: reusing user browser on CDP port {}", external_port)
                await self._attach_best_effort()
                self._spawn(self._ensure_monitor())
                return self._status_payload()

            # 4) 拉起专属常驻实例
            port = await self._launch_managed()
            if port is None:
                return self._status_payload()
            self._mode = "managed"
            self._port = port
            await self._attach_best_effort()
            self._spawn(self._ensure_monitor())
            return self._status_payload()

    async def _attach_best_effort(self) -> None:
        try:
            await self._ensure_page_attached()
        except Exception as exc:
            logger.warning("browser service: attach page failed: {}", exc)

    async def _find_external_port(self) -> int | None:
        return await self._probe_ports_parallel()

    async def _launch_managed(self) -> int | None:
        """拉起专属常驻实例；返回就绪的 CDP 端口，失败返回 None。"""
        exe = _find_browser_executable()
        if exe is None:
            logger.warning("browser service: no Chrome/Edge executable found")
            return None
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        configured_port, headless = _browser_service_settings()
        preferred = configured_port or DEFAULT_CDP_PORT
        for port in range(preferred, preferred + 6):
            if await _probe_cdp(port):
                # 探测与启动之间刚有服务占了端口：直接复用
                return port
            args = [
                exe,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--window-size=1440,900",
            ]
            if headless:
                args.append("--headless=new")
            args.append("about:blank")
            try:
                self._proc = subprocess.Popen(  # noqa: S603
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except OSError as exc:
                logger.warning("browser service: failed to launch {}: {}", exe, exc)
                continue
            # 等待 CDP 就绪
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _CDP_READY_TIMEOUT_S
            while loop.time() < deadline:
                if self._proc.poll() is not None:
                    logger.warning(
                        "browser service: process exited with code {}", self._proc.returncode
                    )
                    self._proc = None
                    break
                if await _probe_cdp(port):
                    logger.info("browser service: managed browser ready on port {}", port)
                    return port
                await asyncio.sleep(0.25)
            if self._proc is not None and await _probe_cdp(port):
                return port
        logger.warning("browser service: could not start managed browser")
        return None

    async def stop(self) -> dict[str, Any]:
        """停止服务：只终止自己拉起的实例；内嵌窗口与外部浏览器仅断开连接。

        - embedded：窗口属于 Tauri 壳，gateway 只断开 CDP 连接（关闭窗口由
          WebUI/壳侧 close_browser_window 命令负责）。
        - external：用户自己的浏览器，绝不关闭。
        - managed：terminate 自拉起的常驻实例。
        """
        async with self._lock:
            proc = self._proc
            self._teardown_connection()
            await self._close_monitor()
            if proc is not None:
                self._proc = None
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except OSError:
                    pass
                self._mode = "stopped"
                self._port = None
            elif self._mode == "embedded":
                # 内嵌窗口仍在，仅断开；状态回到未附着
                self._mode = "stopped"
                self._port = None
            # external：用户浏览器仍在运行，保留 mode/port，状态依旧可用
            return self._status_payload()

    # -- 浏览器级活动监视（Target 域） -----------------------------------------

    def set_activity_notifier(self, callback: Any) -> None:
        """注册活动回调（同步 callable，收到 ``{"type": "activity", "url": ...}``）。"""
        self._activity_notifier = callback

    def _broadcast_activity(self, url: str) -> None:
        """广播一次页面活动（带节流：同 URL 3 秒内只发一次）。"""
        now = time.monotonic()
        last_url, last_t = self._last_activity
        if url == last_url and now - last_t < _ACTIVITY_THROTTLE_S:
            return
        self._last_activity = (url, now)
        if self._activity_notifier is None:
            return
        try:
            self._activity_notifier({"type": "activity", "url": url})
        except Exception:  # noqa: BLE001 - 通知失败不影响浏览器服务
            pass

    async def _close_monitor(self) -> None:
        ws = self._monitor_ws
        self._monitor_ws = None
        self._monitor_port = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _ensure_monitor(self) -> None:
        """维持一条浏览器级 CDP 连接，监听 Target 事件以感知页面活动。"""
        if self._port is None:
            return
        async with self._monitor_lock:
            if self._monitor_ws is not None and self._monitor_port == self._port:
                return
            await self._close_monitor()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"http://127.0.0.1:{self._port}/json/version")
                ws_url = str(resp.json()["webSocketDebuggerUrl"])
            self._monitor_ws = await websockets.connect(ws_url, open_timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - 监视失败只影响自动弹出面板
            logger.debug("browser monitor connect failed: {}", exc)
            self._monitor_ws = None
            return
        self._monitor_port = self._port
        try:
            await self._monitor_ws.send(
                json.dumps({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}})
            )
        except Exception:  # noqa: BLE001
            pass
        asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        try:
            async for raw in self._monitor_ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                method = message.get("method")
                if method not in ("Target.targetCreated", "Target.targetInfoChanged"):
                    continue
                info = (message.get("params") or {}).get("targetInfo") or {}
                if info.get("type") != "page":
                    continue
                url = str(info.get("url") or "")
                if not url or url.startswith(
                    ("about:blank", "devtools://", "chrome-extension://", "chrome://")
                ):
                    continue
                # 托管实例（专为智能体拉起的常驻浏览器）与内嵌窗口（桌面端
                # 与用户同屏）都广播活动；external（用户自己的浏览器）不弹
                # 提示打扰。
                if self._mode not in ("managed", "embedded"):
                    continue
                self._broadcast_activity(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("browser monitor closed: {}", exc)
        finally:
            if self._monitor_ws is not None:
                self._monitor_ws = None
                self._monitor_port = None

    async def reload(self) -> dict[str, Any]:
        """刷新活动页面。"""
        await self._ensure_page_attached()
        await self._cdp("Page.reload", {"ignoreCache": False})
        return self._status_payload()

    def _teardown_connection(self) -> None:
        """断开页面 CDP 连接并清空订阅状态（不动浏览器进程）。"""
        if self._recv_task is not None:
            self._recv_task.cancel()
            self._recv_task = None
        if self._page_ws is not None:
            try:
                self._page_ws.close()
            except Exception:
                pass
            self._page_ws = None
        self._page_id = None
        self._page_url = None
        self._screencast_on = False
        self._latest_frame = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._broadcast({"type": "status", "running": False, "mode": "stopped"})

    # -- 标签页 -----------------------------------------------------------------

    async def list_pages(self) -> list[dict[str, Any]]:
        if self._port is None:
            return []
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"http://127.0.0.1:{self._port}/json/list")
                targets = resp.json()
        except Exception as exc:
            logger.warning("browser service: list pages failed: {}", exc)
            return []
        pages = [
            {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
            for t in targets
            if isinstance(t, dict)
            and t.get("type") == "page"
            and not str(t.get("url", "")).startswith(("devtools://", "chrome-extension://"))
        ]
        # 活动页排最前（稳定排序，其余保持 MRU 顺序）
        pages.sort(key=lambda p: p["id"] != self._page_id)
        return pages

    async def open_page(self, url: str = "about:blank") -> dict[str, Any]:
        if self._port is None:
            raise BrowserError("browser_not_running")
        if self._mode == "embedded":
            # 内嵌窗口：直接在当前页导航（WebView2 环境下 /json/new 的新标签
            # 行为不可控，且用户视角"地址栏跳转"更符合预期）。
            return await self.navigate(url)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.put(f"http://127.0.0.1:{self._port}/json/new?{url}")
                target = resp.json()
        except Exception as exc:
            logger.warning("browser service: open page failed: {}", exc)
            raise BrowserError("open_page_failed") from exc
        target_id = target.get("id")
        if target_id:
            await self.select_page(target_id)
        return self._status_payload()

    async def select_page(self, target_id: str) -> dict[str, Any]:
        await self._attach_best_effort(prefer_target_id=target_id)
        return self._status_payload()

    # -- 页面 CDP 连接 -----------------------------------------------------------

    async def _attach_best_effort(self, prefer_target_id: str | None = None) -> None:
        try:
            await self._ensure_page_attached(prefer_target_id)
        except Exception as exc:
            logger.warning("browser service: attach page failed: {}", exc)

    async def _ensure_page_attached(self, prefer_target_id: str | None = None) -> None:
        """确保已连到一个活动页面的 CDP WebSocket；有订阅者时开启推流。"""
        if (
            prefer_target_id
            and self._page_id == prefer_target_id
            and self._page_ws is not None
        ):
            return
        if not prefer_target_id and self._page_ws is not None:
            return
        pages = await self.list_pages()
        if not pages:
            raise BrowserError("no_page")
        target = next(
            (p for p in pages if prefer_target_id and p["id"] == prefer_target_id),
            pages[0],
        )

        self._teardown_connection()

        ws_url = await self._page_ws_url(target["id"])
        self._page_ws = await websockets.connect(ws_url, max_size=32 * 1024 * 1024)
        self._page_id = target["id"]
        self._page_url = target.get("url")
        self._recv_task = asyncio.create_task(self._recv_loop())
        await self._cdp("Runtime.enable")
        await self._cdp("Page.enable")
        # 初始 URL 以页面实际值为准
        try:
            result = await self._cdp(
                "Runtime.evaluate", {"expression": "location.href", "returnByValue": True}
            )
            href = (result or {}).get("result", {}).get("value")
            if isinstance(href, str) and href:
                self._page_url = href
        except _CdpCommandError:
            pass
        if self._listeners:
            await self._start_screencast()

    async def _page_ws_url(self, target_id: str) -> str:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://127.0.0.1:{self._port}/json/list")
            for t in resp.json():
                if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                    return str(t["webSocketDebuggerUrl"])
        raise BrowserError("page_not_found")

    async def _cdp(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> Any:
        """发送页面级 CDP 命令并等待结果。"""
        if self._page_ws is None:
            raise BrowserError("page_not_connected")
        self._cmd_seq += 1
        cmd_id = self._cmd_seq
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            await self._page_ws.send(
                json.dumps({"id": cmd_id, "method": method, "params": params or {}})
            )
            result = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(cmd_id, None)
        return result

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._page_ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if message.get("id") is not None:
                    fut = self._pending.pop(message["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in message:
                            fut.set_exception(
                                _CdpCommandError(
                                    str(message["error"].get("message", "cdp error"))
                                )
                            )
                        else:
                            fut.set_result(message.get("result"))
                    continue
                self._handle_event(message.get("method"), message.get("params") or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("browser CDP connection closed: {}", exc)
        finally:
            self._page_ws = None
            self._page_id = None
            self._screencast_on = False

    def _handle_event(self, method: str | None, params: dict[str, Any]) -> None:
        if method == "Page.screencastFrame":
            frame = {
                "type": "frame",
                "data": params.get("data"),
                "metadata": params.get("metadata") or {},
            }
            self._latest_frame = frame
            self._broadcast(frame)
            # 必须 ack，否则浏览器停止推新帧
            ack_session = params.get("sessionId")
            if ack_session is not None:
                self._spawn(self._safe_cdp("Page.screencastFrameAck", {"sessionId": ack_session}))
            return
        if method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if not frame.get("parentId") and frame.get("url"):
                self._page_url = frame["url"]
                self._broadcast(self._status_payload(type_field=True))

    def _spawn(self, coro: Any) -> None:
        async def _run() -> None:
            try:
                await coro
            except Exception:
                pass

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            pass

    async def _safe_cdp(self, method: str, params: dict[str, Any] | None = None) -> None:
        try:
            await self._cdp(method, params)
        except Exception:
            pass

    # -- 推流订阅 -----------------------------------------------------------------

    def _broadcast(self, item: dict[str, Any]) -> None:
        for queue in tuple(self._listeners):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                # 订阅者消费不过来：丢旧帧保实时
                try:
                    queue.get_nowait()
                    queue.put_nowait(item)
                except Exception:
                    pass

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=2)
        self._listeners.add(queue)
        self._spawn(self._start_screencast())
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._listeners.discard(queue)
        if not self._listeners:
            self._spawn(self._stop_screencast())

    def latest_frame(self) -> dict[str, Any] | None:
        return self._latest_frame

    async def _start_screencast(self) -> None:
        if self._screencast_on or self._page_ws is None:
            return
        try:
            await self._cdp(
                "Page.startScreencast",
                {"format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 900},
            )
            self._screencast_on = True
        except Exception as exc:
            logger.warning("browser service: start screencast failed: {}", exc)

    async def _stop_screencast(self) -> None:
        if not self._screencast_on or self._page_ws is None:
            return
        try:
            await self._cdp("Page.stopScreencast")
        except Exception:
            pass
        self._screencast_on = False

    # -- 导航 / 输入 -----------------------------------------------------------------

    async def navigate(self, url: str) -> dict[str, Any]:
        await self._ensure_page_attached()
        url = url if "://" in url else f"https://{url}"
        await self._cdp("Page.navigate", {"url": url}, timeout=15.0)
        self._page_url = url
        return self._status_payload()

    async def dispatch_input(self, action: str, params: dict[str, Any]) -> None:
        if self._page_ws is None:
            raise BrowserError("page_not_connected")
        if action == "mouse":
            await self._cdp("Input.dispatchMouseEvent", params)
        elif action == "wheel":
            await self._cdp(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseWheel",
                    "x": params.get("x", 0),
                    "y": params.get("y", 0),
                    "deltaX": params.get("deltaX", 0),
                    "deltaY": params.get("deltaY", 0),
                },
            )
        elif action == "key":
            await self._cdp("Input.dispatchKeyEvent", params)
        elif action == "insertText":
            await self._cdp("Input.insertText", {"text": params.get("text", "")})
        else:
            raise BrowserError("unknown_input_action")


_MANAGER: BrowserServiceManager | None = None


def get_browser_manager() -> BrowserServiceManager:
    """进程级单例：gateway 与 agent loop 共享同一个浏览器服务。"""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = BrowserServiceManager()
    return _MANAGER
