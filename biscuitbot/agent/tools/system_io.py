"""系统级 IO 工具：键盘/鼠标模拟、剪贴板、USB 与串口设备操作。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中的系统级 IO 组件。
``SystemIoTool``（system_io）默认禁用，因为这些能力直接操作宿主操作系统
（模拟输入、读取剪贴板、操作硬件设备），需要通过 ``tools.system_io.enable``
显式启用。

实现策略：
- 剪贴板与 USB 列表使用平台原生 CLI 工具（无需第三方依赖）。
- 键盘/鼠标模拟优先使用 ``pynput``（若已安装），Linux 回退到 ``xdotool``，
  macOS 回退到 ``osascript``。Windows 需要 ``pynput``。
- 串口读写需要 ``pyserial``（可选依赖）。
- 所有子进程调用均为异步，并包装在每操作超时中。
"""

from __future__ import annotations

import asyncio  # 异步 IO，用于子进程与阻塞操作
import os  # 操作系统接口
import platform  # 平台信息
import sys  # 系统相关（平台判断）
from typing import Any  # 类型注解

from loguru import logger  # 日志记录
from pydantic import Field  # Pydantic 字段

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import (  # JSON Schema 类型
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from biscuitbot.config_base import Base  # 配置基类

_IS_WINDOWS = sys.platform == "win32"  # 是否为 Windows 平台
_IS_MACOS = sys.platform == "darwin"  # 是否为 macOS 平台
_IS_LINUX = sys.platform.startswith("linux")  # 是否为 Linux 平台

# 动作分组。只读动作对宿主无副作用；写动作会修改宿主状态（剪贴板、输入、
# 串口总线），绝不允许多个写动作并行执行。
_READ_ACTIONS: frozenset[str] = frozenset({"clipboard_read", "usb_list", "serial_list"})
_WRITE_ACTIONS: frozenset[str] = frozenset({
    "clipboard_write", "key_tap", "key_type",
    "mouse_move", "mouse_click", "mouse_scroll",
    "serial_write", "serial_read",  # serial_read 会打开/独占端口
})
_ALL_ACTIONS: frozenset[str] = _READ_ACTIONS | _WRITE_ACTIONS

_DEFAULT_TIMEOUT_MS = 3000  # 默认每操作超时（毫秒）
_MAX_TIMEOUT_MS = 30000  # 最大每操作超时（毫秒）
_MAX_OUTPUT_CHARS = 8000  # 输出最大字符数


class SystemIoToolConfig(Base):
    """系统级 IO 工具配置。

    默认禁用。启用后，agent 可以在宿主操作系统上模拟键盘/鼠标输入、
    读写剪贴板、列出 USB 设备以及读写串口。使用 ``allow_actions`` 可限制
    为子集。
    """

    enable: bool = False
    # 可选的动作允许列表。空列表 = 启用时允许所有动作。
    # 可用于锁定为只读操作，如 ["clipboard_read", "usb_list", "serial_list"]。
    allow_actions: list[str] = Field(default_factory=list)


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "The system IO action to perform. Read the usage doc for per-action "
            "parameters and platform requirements.",
            enum=sorted(_ALL_ACTIONS),
        ),
        text=StringSchema(
            "Text payload for clipboard_write, key_type, or serial_write.",
            nullable=True,
        ),
        keys=StringSchema(
            "Key sequence for key_tap. Use '+' for chords, e.g. 'cmd+c', "
            "'ctrl+shift+esc', 'enter', 'f5', 'space'.",
            nullable=True,
        ),
        x=IntegerSchema(
            description="X screen coordinate for mouse_move / mouse_click.",
            nullable=True,
        ),
        y=IntegerSchema(
            description="Y screen coordinate for mouse_move / mouse_click.",
            nullable=True,
        ),
        button=StringSchema(
            "Mouse button for mouse_click.",
            enum=["left", "right", "middle"],
            nullable=True,
        ),
        scroll_dx=IntegerSchema(
            description="Horizontal scroll delta for mouse_scroll.",
            nullable=True,
        ),
        scroll_dy=IntegerSchema(
            description="Vertical scroll delta for mouse_scroll (positive = down).",
            nullable=True,
        ),
        port=StringSchema(
            "Serial port device name, e.g. '/dev/ttyUSB0' or 'COM3'.",
            nullable=True,
        ),
        baudrate=IntegerSchema(
            description="Serial port baud rate (default 9600).",
            minimum=1,
            nullable=True,
        ),
        bytes_to_read=IntegerSchema(
            description="Number of bytes to read from serial port (default 128).",
            minimum=1,
            nullable=True,
        ),
        timeout_ms=IntegerSchema(
            description=f"Per-action timeout in milliseconds (default {_DEFAULT_TIMEOUT_MS}).",
            minimum=100,
            maximum=_MAX_TIMEOUT_MS,
            nullable=True,
        ),
        required=["action"],
    )
)
class SystemIoTool(Tool):
    """系统级 IO：键盘/鼠标模拟、剪贴板、USB 与串口设备操作。

    默认禁用；通过配置中的 ``tools.system_io.enable`` 启用。
    直接操作宿主操作系统，需要用户显式选择启用。
    """

    _scopes = {"core"}  # 工具可用作用域：仅核心
    _capability = (
        "Simulate keyboard/mouse input, read/write clipboard, and list/operate "
        "USB & serial devices on the host OS."
    )
    _usage_md = "docs/system_io.md"  # 使用说明文档路径
    config_key = "system_io"  # 配置键名

    @classmethod
    def config_cls(cls):
        return SystemIoToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.system_io.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(config=ctx.config.system_io)

    def __init__(self, *, config: SystemIoToolConfig | None = None) -> None:
        self.config = config or SystemIoToolConfig()

    @property
    def name(self) -> str:
        return "system_io"

    @property
    def description(self) -> str:
        return (
            "Perform system-level IO on the host OS: simulate keyboard/mouse input, "
            "read/write the clipboard, list USB devices, and read/write serial ports. "
            "Disabled by default; enable via tools.system_io.enable. Select the "
            "operation with the 'action' parameter. Read docs/system_io.md for "
            "per-action parameters and platform requirements before first use."
        )

    @property
    def exclusive(self) -> bool:
        # 硬件输入有真实世界副作用，绝不并行执行
        return True

    async def execute(self, action: str, **kwargs: Any) -> str:
        """执行系统级 IO 动作。

        参数:
            action: 要执行的动作名称（见 _ALL_ACTIONS）。
            **kwargs: 动作特定参数（如 text、keys、x、y 等）。

        返回:
            动作结果文本；超时或错误时返回错误信息。
        """
        if action not in _ALL_ACTIONS:
            return f"Error: unknown action '{action}'. Valid: {sorted(_ALL_ACTIONS)}"
        if self.config.allow_actions and action not in self.config.allow_actions:
            return (
                f"Error: action '{action}' is not in the configured allow_actions "
                f"allowlist. Allowed: {self.config.allow_actions}"
            )

        timeout_ms = kwargs.get("timeout_ms") or _DEFAULT_TIMEOUT_MS
        try:
            result = await asyncio.wait_for(
                self._dispatch(action, kwargs),
                timeout=timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            return f"Error: action '{action}' timed out after {timeout_ms}ms"
        except Exception as exc:  # noqa: BLE001 — 作为工具结果返回，绝不抛出
            logger.exception("system_io action '{}' failed", action)
            return f"Error: 系统 IO 操作 '{action}' 失败：{exc}。请检查设备/端口是否可用、权限是否足够"
        return self._truncate(result)

    async def _dispatch(self, action: str, kwargs: dict[str, Any]) -> str:
        """按动作名分派到对应的 _action_<name> 处理器。"""
        handler = getattr(self, f"_action_{action}")
        return await handler(kwargs)

    @staticmethod
    def _truncate(text: str) -> str:
        """截断输出到 _MAX_OUTPUT_CHARS，保留首尾各一半。"""
        if len(text) <= _MAX_OUTPUT_CHARS:
            return text
        half = _MAX_OUTPUT_CHARS // 2
        return (
            text[:half]
            + f"\n\n... ({len(text) - _MAX_OUTPUT_CHARS:,} chars truncated) ...\n\n"
            + text[-half:]
        )

    # ------------------------------------------------------------------
    # Subprocess helper
    # ------------------------------------------------------------------

    @staticmethod
    async def _run(
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, bytes, bytes]:
        """异步运行子进程，返回 (退出码, stdout, stderr)。"""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            with suppress_ctx():
                await proc.wait()
            raise
        return proc.returncode or 0, stdout, stderr

    @staticmethod
    def _missing_tool(name: str, hint: str) -> str:
        return (
            f"Error: required dependency '{name}' is not available. {hint} "
            "Install it via: pip install pynput pyserial"
        )

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    async def _action_clipboard_read(self, kw: dict[str, Any]) -> str:
        if _IS_MACOS:
            rc, out, err = await self._run(["pbpaste"], timeout=5.0)
        elif _IS_LINUX:
            cmd = self._which("xclip") or self._which("xsel")
            if cmd is None:
                return self._missing_tool(
                    "xclip/xsel", "Linux clipboard needs xclip or xsel installed.",
                )
            args = [cmd, "-selection", "clipboard", "-o"] if cmd.endswith("xclip") \
                else [cmd, "--clipboard", "--output"]
            rc, out, err = await self._run(args, timeout=5.0)
        elif _IS_WINDOWS:
            rc, out, err = await self._run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                timeout=5.0,
            )
        else:
            return f"Error: clipboard_read unsupported on platform {platform.system()}"
        if rc != 0:
            return f"Error: clipboard read failed (exit {rc}): {err.decode(errors='replace').strip()}"
        return out.decode("utf-8", errors="replace")

    async def _action_clipboard_write(self, kw: dict[str, Any]) -> str:
        text = kw.get("text")
        if text is None:
            return "Error: clipboard_write requires the 'text' parameter."
        data = text.encode("utf-8")
        if _IS_MACOS:
            rc, out, err = await self._run(["pbcopy"], input_bytes=data, timeout=5.0)
        elif _IS_LINUX:
            cmd = self._which("xclip") or self._which("xsel")
            if cmd is None:
                return self._missing_tool(
                    "xclip/xsel", "Linux clipboard needs xclip or xsel installed.",
                )
            args = [cmd, "-selection", "clipboard", "-i"] if cmd.endswith("xclip") \
                else [cmd, "--clipboard", "--input"]
            rc, out, err = await self._run(args, input_bytes=data, timeout=5.0)
        elif _IS_WINDOWS:
            # PowerShell Set-Clipboard with -Value to avoid encoding issues.
            ps = (
                "$ErrorActionPreference='Stop'; "
                "$text = [Console]::In.ReadToEnd(); "
                "Set-Clipboard -Value $text"
            )
            rc, out, err = await self._run(
                ["powershell", "-NoProfile", "-Command", ps],
                input_bytes=data,
                timeout=5.0,
            )
        else:
            return f"Error: clipboard_write unsupported on platform {platform.system()}"
        if rc != 0:
            return f"Error: clipboard write failed (exit {rc}): {err.decode(errors='replace').strip()}"
        return f"Clipboard updated ({len(text)} chars)."

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    async def _action_key_tap(self, kw: dict[str, Any]) -> str:
        keys = kw.get("keys")
        if not keys:
            return "Error: key_tap requires the 'keys' parameter (e.g. 'cmd+c', 'enter')."
        backend = self._keyboard_backend()
        if backend == "pynput":
            return await self._key_tap_pynput(keys)
        if _IS_LINUX and backend == "xdotool":
            return await self._key_tap_xdotool(keys)
        if _IS_MACOS and backend == "osascript":
            return await self._key_tap_osascript(keys)
        if _IS_WINDOWS and backend == "sendkeys":
            return await self._key_tap_sendkeys(keys)
        return self._missing_tool(
            "pynput",
            "Keyboard simulation on this platform requires pynput.",
        )

    async def _action_key_type(self, kw: dict[str, Any]) -> str:
        text = kw.get("text")
        if text is None:
            return "Error: key_type requires the 'text' parameter."
        if text == "":
            return "Typed empty string (no-op)."
        backend = self._keyboard_backend()
        if backend == "pynput":
            return await self._key_type_pynput(text)
        if _IS_LINUX and backend == "xdotool":
            rc, out, err = await self._run(
                ["xdotool", "type", "--clearmodifiers", "--", text], timeout=10.0,
            )
            if rc != 0:
                return f"Error: xdotool type failed: {err.decode(errors='replace').strip()}"
            return f"Typed {len(text)} chars."
        if _IS_MACOS and backend == "osascript":
            # Escape double quotes for AppleScript string literal.
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            rc, out, err = await self._run(
                ["osascript", "-e", f'tell application "System Events" to keystroke "{escaped}"'],
                timeout=10.0,
            )
            if rc != 0:
                return f"Error: osascript keystroke failed: {err.decode(errors='replace').strip()}"
            return f"Typed {len(text)} chars."
        if _IS_WINDOWS and backend == "sendkeys":
            return await self._key_type_sendkeys(text)
        return self._missing_tool(
            "pynput",
            "Keyboard simulation on this platform requires pynput.",
        )

    def _keyboard_backend(self) -> str:
        """为当前平台选择最佳可用的键盘后端。"""
        try:
            import pynput  # noqa: F401
            return "pynput"
        except ImportError:
            pass
        if _IS_LINUX and self._which("xdotool"):
            return "xdotool"
        if _IS_MACOS and self._which("osascript"):
            return "osascript"
        if _IS_WINDOWS and self._which("powershell"):
            return "sendkeys"
        return "none"

    async def _key_tap_pynput(self, keys: str) -> str:
        from pynput import keyboard  # type: ignore[import-not-found]

        chord = [k.strip() for k in keys.split("+") if k.strip()]
        if not chord:
            return "Error: keys parsed to empty chord."
        # Run in a thread: pynput's Controller is synchronous.
        def _press() -> None:
            kb = keyboard.Controller()
            mods: list[str] = []
            for token in chord[:-1]:
                mod = self._pynput_mod(token)
                if mod is None:
                    raise ValueError(f"unknown modifier key: {token!r}")
                mods.append(mod)
            final = self._pynput_key(chord[-1])
            if final is None:
                raise ValueError(f"unknown key: {chord[-1]!r}")
            for m in mods:
                kb.press(m)
            try:
                kb.press(final)
                kb.release(final)
            finally:
                for m in reversed(mods):
                    kb.release(m)

        await asyncio.to_thread(_press)
        return f"Tapped keys: {keys}"

    async def _key_tap_xdotool(self, keys: str) -> str:
        # xdotool expects '+'-joined chords, e.g. "ctrl+shift+esc".
        rc, out, err = await self._run(["xdotool", "key", keys], timeout=5.0)
        if rc != 0:
            return f"Error: xdotool key failed: {err.decode(errors='replace').strip()}"
        return f"Tapped keys: {keys}"

    async def _key_tap_osascript(self, keys: str) -> str:
        chord = [k.strip() for k in keys.split("+") if k.strip()]
        if not chord:
            return "Error: keys parsed to empty chord."
        # macOS: map common keys to AppleScript key codes / keystroke forms.
        # For a single printable key, use 'keystroke'. For chords or special
        # keys, fall back to key code lookup table (best-effort).
        if len(chord) == 1 and len(chord[0]) == 1 and chord[0].isprintable():
            escaped = chord[0].replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "System Events" to keystroke "{escaped}"'
        else:
            # Build a modifier-aware keystroke. Map modifier names to AppleScript.
            mod_map = {
                "cmd": "command down", "command": "command down",
                "ctrl": "control down", "control": "control down",
                "alt": "option down", "option": "option down",
                "shift": "shift down",
            }
            mods = [mod_map[m.lower()] for m in chord[:-1] if m.lower() in mod_map]
            final = chord[-1]
            if len(final) == 1 and final.isprintable():
                escaped = final.replace("\\", "\\\\").replace('"', '\\"')
                using = ", ".join(mods) if mods else ""
                script = (
                    f'tell application "System Events" to keystroke "{escaped}"'
                    + (f" using {{{using}}}" if mods else "")
                )
            else:
                return (
                    "Error: osascript key_tap only supports single printable keys "
                    f"or simple modifier+printable chords. Got: {keys!r}. "
                    "Install pynput for full key support."
                )
        rc, out, err = await self._run(["osascript", "-e", script], timeout=5.0)
        if rc != 0:
            return f"Error: osascript key_tap failed: {err.decode(errors='replace').strip()}"
        return f"Tapped keys: {keys}"

    # SendKeys 特殊键名映射（Windows PowerShell fallback）
    _SENDKEYS_SPECIAL = {
        "enter": "{ENTER}", "return": "{ENTER}",
        "tab": "{TAB}", "esc": "{ESC}", "escape": "{ESC}",
        "backspace": "{BACKSPACE}", "bs": "{BACKSPACE}",
        "delete": "{DELETE}", "del": "{DELETE}",
        "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
        "home": "{HOME}", "end": "{END}",
        "pageup": "{PGUP}", "pagedown": "{PGDN}",
        "space": " ",
        "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}",
        "f5": "{F5}", "f6": "{F6}", "f7": "{F7}", "f8": "{F8}",
        "f9": "{F9}", "f10": "{F10}", "f11": "{F11}", "f12": "{F12}",
    }

    _SENDKEYS_MOD_MAP = {
        "ctrl": "^", "control": "^",
        "alt": "%", "option": "%",
        "shift": "+",
        "cmd": "^", "command": "^",  # Windows 上 cmd 映射为 Ctrl
    }

    async def _key_tap_sendkeys(self, keys: str) -> str:
        """Windows PowerShell SendKeys fallback for key_tap."""
        chord = [k.strip() for k in keys.split("+") if k.strip()]
        if not chord:
            return "Error: keys parsed to empty chord."
        # 构建 SendKeys 字符串
        parts: list[str] = []
        for token in chord[:-1]:
            mod = self._SENDKEYS_MOD_MAP.get(token.lower())
            if mod is None:
                return f"Error: unknown modifier key: {token!r}"
            parts.append(mod)
        final = chord[-1].lower()
        special = self._SENDKEYS_SPECIAL.get(final)
        if special is not None:
            parts.append(special)
        elif len(chord[-1]) == 1 and chord[-1].isprintable():
            # 单个可打印字符：需转义 SendKeys 特殊字符 {}[]()+^%~{}
            ch = chord[-1]
            if ch in "{}[]()+^%~":
                parts.append("{" + ch + "}")
            else:
                parts.append(ch)
        else:
            return (
                f"Error: sendkeys key_tap does not support key: {chord[-1]!r}. "
                "Install pynput for full key support."
            )
        send_str = "".join(parts)
        # 转义 PowerShell 字符串中的特殊字符
        send_str_ps = send_str.replace("'", "''")
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            f"[System.Windows.Forms.SendKeys]::SendWait('{send_str_ps}');"
        )
        rc, out, err = await self._run(
            ["powershell", "-NoProfile", "-Command", ps_script], timeout=5.0,
        )
        if rc != 0:
            return f"Error: SendKeys key_tap failed: {err.decode(errors='replace').strip()}"
        return f"Tapped keys: {keys}"

    async def _key_type_sendkeys(self, text: str) -> str:
        """Windows PowerShell SendKeys fallback for key_type."""
        # 转义 SendKeys 特殊字符
        special = set("{}[]()+^%~")
        escaped = []
        for ch in text:
            if ch in special:
                escaped.append("{" + ch + "}")
            else:
                escaped.append(ch)
        send_str = "".join(escaped)
        # 转义 PowerShell 字符串中的单引号
        send_str_ps = send_str.replace("'", "''")
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            f"[System.Windows.Forms.SendKeys]::SendWait('{send_str_ps}');"
        )
        rc, out, err = await self._run(
            ["powershell", "-NoProfile", "-Command", ps_script], timeout=30.0,
        )
        if rc != 0:
            return f"Error: SendKeys key_type failed: {err.decode(errors='replace').strip()}"
        return f"Typed {len(text)} chars."

    @staticmethod
    def _pynput_mod(name: str) -> Any:
        from pynput import keyboard  # type: ignore[import-not-found]
        n = name.lower()
        return {
            "cmd": keyboard.Key.cmd, "command": keyboard.Key.cmd,
            "cmd_l": keyboard.Key.cmd_l, "cmd_r": keyboard.Key.cmd_r,
            "ctrl": keyboard.Key.ctrl, "control": keyboard.Key.ctrl,
            "ctrl_l": keyboard.Key.ctrl_l, "ctrl_r": keyboard.Key.ctrl_r,
            "alt": keyboard.Key.alt, "option": keyboard.Key.alt,
            "alt_l": keyboard.Key.alt_l, "alt_r": keyboard.Key.alt_r,
            "shift": keyboard.Key.shift,
            "shift_l": keyboard.Key.shift_l, "shift_r": keyboard.Key.shift_r,
            "fn": keyboard.Key.fn,
        }.get(n)

    @staticmethod
    def _pynput_key(name: str) -> Any:
        from pynput import keyboard  # type: ignore[import-not-found]
        n = name.lower()
        special = {
            "enter": keyboard.Key.enter, "return": keyboard.Key.enter,
            "tab": keyboard.Key.tab, "space": keyboard.Key.space,
            "backspace": keyboard.Key.backspace, "bs": keyboard.Key.backspace,
            "delete": keyboard.Key.delete, "del": keyboard.Key.delete,
            "esc": keyboard.Key.esc, "escape": keyboard.Key.esc,
            "up": keyboard.Key.up, "down": keyboard.Key.down,
            "left": keyboard.Key.left, "right": keyboard.Key.right,
            "home": keyboard.Key.home, "end": keyboard.Key.end,
            "page_up": keyboard.Key.page_up, "pageup": keyboard.Key.page_up,
            "page_down": keyboard.Key.page_down, "pagedown": keyboard.Key.page_down,
            "caps_lock": keyboard.Key.caps_lock, "capslock": keyboard.Key.caps_lock,
            "f1": keyboard.Key.f1, "f2": keyboard.Key.f2, "f3": keyboard.Key.f3,
            "f4": keyboard.Key.f4, "f5": keyboard.Key.f5, "f6": keyboard.Key.f6,
            "f7": keyboard.Key.f7, "f8": keyboard.Key.f8, "f9": keyboard.Key.f9,
            "f10": keyboard.Key.f10, "f11": keyboard.Key.f11, "f12": keyboard.Key.f12,
        }
        if n in special:
            return special[n]
        if len(name) == 1:
            return name  # literal character
        return None

    async def _key_type_pynput(self, text: str) -> str:
        from pynput import keyboard  # type: ignore[import-not-found]

        def _type() -> None:
            kb = keyboard.Controller()
            kb.type(text)

        await asyncio.to_thread(_type)
        return f"Typed {len(text)} chars."

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    async def _action_mouse_move(self, kw: dict[str, Any]) -> str:
        x = kw.get("x")
        y = kw.get("y")
        if x is None or y is None:
            return "Error: mouse_move requires 'x' and 'y' parameters."
        backend = self._mouse_backend()
        if backend == "pynput":
            from pynput import mouse  # type: ignore[import-not-found]
            await asyncio.to_thread(lambda: mouse.Controller().position(x, y))
            return f"Mouse moved to ({x}, {y})."
        if _IS_LINUX and backend == "xdotool":
            rc, out, err = await self._run(
                ["xdotool", "mousemove", str(x), str(y)], timeout=5.0,
            )
            if rc != 0:
                return f"Error: xdotool mousemove failed: {err.decode(errors='replace').strip()}"
            return f"Mouse moved to ({x}, {y})."
        return self._missing_tool(
            "pynput",
            "Mouse simulation on this platform requires pynput.",
        )

    async def _action_mouse_click(self, kw: dict[str, Any]) -> str:
        x = kw.get("x")
        y = kw.get("y")
        button = (kw.get("button") or "left").lower()
        if button not in ("left", "right", "middle"):
            return f"Error: button must be left/right/middle, got {button!r}."
        backend = self._mouse_backend()
        if backend == "pynput":
            from pynput import mouse  # type: ignore[import-not-found]
            btn_map = {
                "left": mouse.Button.left,
                "right": mouse.Button.right,
                "middle": mouse.Button.middle,
            }
            btn = btn_map[button]

            def _click() -> None:
                m = mouse.Controller()
                if x is not None and y is not None:
                    m.position(x, y)
                m.click(btn)

            await asyncio.to_thread(_click)
            pos = f" at ({x}, {y})" if x is not None and y is not None else ""
            return f"Clicked {button} mouse button{pos}."
        if _IS_LINUX and backend == "xdotool":
            btn_num = {"left": "1", "middle": "2", "right": "3"}[button]
            args = ["xdotool", "click", btn_num]
            if x is not None and y is not None:
                args = ["xdotool", "mousemove", str(x), str(y), "click", btn_num]
            rc, out, err = await self._run(args, timeout=5.0)
            if rc != 0:
                return f"Error: xdotool click failed: {err.decode(errors='replace').strip()}"
            return f"Clicked {button} mouse button."
        return self._missing_tool(
            "pynput",
            "Mouse simulation on this platform requires pynput.",
        )

    async def _action_mouse_scroll(self, kw: dict[str, Any]) -> str:
        dx = kw.get("scroll_dx") or 0
        dy = kw.get("scroll_dy") or 0
        if dx == 0 and dy == 0:
            return "Error: mouse_scroll requires non-zero scroll_dx or scroll_dy."
        backend = self._mouse_backend()
        if backend == "pynput":
            from pynput import mouse  # type: ignore[import-not-found]

            def _scroll() -> None:
                m = mouse.Controller()
                if dy != 0:
                    m.scroll(0, dy)
                if dx != 0:
                    m.scroll(dx, 0)

            await asyncio.to_thread(_scroll)
            return f"Scrolled dx={dx}, dy={dy}."
        if _IS_LINUX and backend == "xdotool":
            # xdotool uses button 4 (up) / 5 (down) for vertical scroll.
            args: list[str] = []
            if dy != 0:
                args.extend(["xdotool", "click", "5" if dy > 0 else "4"])
            rc, out, err = await self._run(args, timeout=5.0) if args else (0, b"", b"")
            if rc != 0:
                return f"Error: xdotool scroll failed: {err.decode(errors='replace').strip()}"
            return f"Scrolled dx={dx}, dy={dy}."
        return self._missing_tool(
            "pynput",
            "Mouse scroll on this platform requires pynput.",
        )

    def _mouse_backend(self) -> str:
        """为当前平台选择最佳可用的鼠标后端。"""
        try:
            import pynput  # noqa: F401
            return "pynput"
        except ImportError:
            pass
        if _IS_LINUX and self._which("xdotool"):
            return "xdotool"
        return "none"

    # ------------------------------------------------------------------
    # USB
    # ------------------------------------------------------------------

    async def _action_usb_list(self, kw: dict[str, Any]) -> str:
        if _IS_MACOS:
            rc, out, err = await self._run(
                ["system_profiler", "SPUSBDataType"], timeout=10.0,
            )
            if rc != 0:
                return f"Error: system_profiler failed: {err.decode(errors='replace').strip()}"
            return out.decode("utf-8", errors="replace")
        if _IS_LINUX:
            cmd = self._which("lsusb")
            if cmd is None:
                return self._missing_tool(
                    "lsusb", "Linux USB listing needs lsusb (usbutils) installed.",
                )
            rc, out, err = await self._run([cmd], timeout=10.0)
            if rc != 0:
                return f"Error: lsusb failed: {err.decode(errors='replace').strip()}"
            return out.decode("utf-8", errors="replace")
        if _IS_WINDOWS:
            ps = (
                "Get-PnpDevice -Present | "
                "Where-Object { $_.Class -in @('USB','Bluetooth','Mouse','Keyboard') } | "
                "Select-Object Status,Class,FriendlyName,InstanceId | "
                "Format-Table -AutoSize | Out-String -Width 4096"
            )
            rc, out, err = await self._run(
                ["powershell", "-NoProfile", "-Command", ps], timeout=10.0,
            )
            if rc != 0:
                return f"Error: Get-PnpDevice failed: {err.decode(errors='replace').strip()}"
            return out.decode("utf-8", errors="replace")
        return f"Error: usb_list unsupported on platform {platform.system()}"

    # ------------------------------------------------------------------
    # Serial
    # ------------------------------------------------------------------

    async def _action_serial_list(self, kw: dict[str, Any]) -> str:
        # Prefer pyserial's accurate enumeration when available.
        try:
            from serial.tools import list_ports  # type: ignore[import-not-found]
        except ImportError:
            list_ports = None

        ports: list[str] = []
        if list_ports is not None:
            for info in await asyncio.to_thread(list_ports.comports):
                ports.append(f"{info.device}\t{info.description}\t{info.hwid}")
        else:
            # Fallback: scan common device patterns.
            candidates: list[str] = []
            if _IS_WINDOWS:
                # Best-effort without registry access: COM1..COM8
                candidates = [f"COM{i}" for i in range(1, 9)]
            else:
                import glob
                patterns = [
                    "/dev/ttyUSB*", "/dev/ttyACM*",
                    "/dev/ttyS*", "/dev/tty.*", "/dev/cu.*",
                ]
                for pat in patterns:
                    candidates.extend(glob.glob(pat))
            for dev in candidates:
                if os.path.exists(dev):
                    ports.append(dev)

        if not ports:
            return (
                "No serial ports found. If pyserial is not installed, only common "
                "device patterns are scanned; install pyserial (pip install pyserial) "
                "for accurate enumeration."
            )
        return "Serial ports:\n" + "\n".join(ports)

    async def _action_serial_write(self, kw: dict[str, Any]) -> str:
        port = kw.get("port")
        text = kw.get("text")
        if not port:
            return "Error: serial_write requires the 'port' parameter."
        if text is None:
            return "Error: serial_write requires the 'text' parameter."
        baudrate = kw.get("baudrate") or 9600
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError:
            return self._missing_tool(
                "pyserial", "Serial port IO requires pyserial installed.",
            )

        data = text.encode("utf-8")

        def _write() -> int:
            with serial.Serial(port, baudrate, timeout=1) as s:
                return s.write(data)

        written = await asyncio.to_thread(_write)
        return f"Wrote {written} bytes to {port}@{baudrate}."

    async def _action_serial_read(self, kw: dict[str, Any]) -> str:
        port = kw.get("port")
        if not port:
            return "Error: serial_read requires the 'port' parameter."
        baudrate = kw.get("baudrate") or 9600
        n = kw.get("bytes_to_read") or 128
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError:
            return self._missing_tool(
                "pyserial", "Serial port IO requires pyserial installed.",
            )

        def _read() -> bytes:
            with serial.Serial(port, baudrate, timeout=1) as s:
                return s.read(n)

        data = await asyncio.to_thread(_read)
        # Return as a hex + ascii preview for safety.
        if not data:
            return f"Read 0 bytes from {port}@{baudrate}."
        hex_preview = data[:64].hex(" ")
        try:
            ascii_preview = data[:64].decode("utf-8", errors="replace")
        except Exception:
            ascii_preview = ""
        return (
            f"Read {len(data)} bytes from {port}@{baudrate}.\n"
            f"hex: {hex_preview}\n"
            f"ascii: {ascii_preview}"
        )

    # ------------------------------------------------------------------
    # Utils
    # ------------------------------------------------------------------

    @staticmethod
    def _which(name: str) -> str | None:
        """查找可执行文件路径的本地包装器，便于测试时 patch。"""
        # 本地包装器，便于测试 patch 发现逻辑
        import shutil
        return shutil.which(name)


def suppress_ctx():
    """返回一个抑制 asyncio.TimeoutError 与 CancelledError 的上下文管理器。

    定义为函数（非内联）以保持调用点可读性。
    """
    from contextlib import suppress
    return suppress(asyncio.TimeoutError, asyncio.CancelledError)
