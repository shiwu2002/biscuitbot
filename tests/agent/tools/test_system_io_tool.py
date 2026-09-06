"""Tests for SystemIoTool — system-level IO (keyboard/mouse/clipboard/USB/serial)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from biscuitbot.agent.tools.system_io import (
    _ALL_ACTIONS,
    SystemIoTool,
    SystemIoToolConfig,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestSystemIoToolConfig:
    def test_default_disabled(self):
        c = SystemIoToolConfig()
        assert c.enable is False

    def test_default_empty_allowlist(self):
        c = SystemIoToolConfig()
        assert c.allow_actions == []

    def test_enable_with_allowlist(self):
        c = SystemIoToolConfig(enable=True, allow_actions=["clipboard_read", "usb_list"])
        assert c.enable is True
        assert c.allow_actions == ["clipboard_read", "usb_list"]

    def test_camel_case_alias(self):
        # JSON config uses camelCase; Base model accepts both.
        c = SystemIoToolConfig.model_validate({"enable": True, "allowActions": ["x"]})
        assert c.enable is True
        assert c.allow_actions == ["x"]


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class TestSystemIoToolMetadata:
    def test_name(self):
        assert SystemIoTool().name == "system_io"

    def test_capability_mentions_keyboard_mouse_clipboard(self):
        cap = SystemIoTool().capability.lower()
        assert "keyboard" in cap
        assert "mouse" in cap
        assert "clipboard" in cap

    def test_description_mentions_enable(self):
        d = SystemIoTool().description.lower()
        assert "enable" in d
        assert "action" in d

    def test_exclusive_true(self):
        # Hardware input has real-world side effects; must never parallelize.
        assert SystemIoTool().exclusive is True

    def test_read_only_false(self):
        assert SystemIoTool().read_only is False

    def test_usage_md_path(self):
        assert SystemIoTool._usage_md == "docs/system_io.md"

    def test_scopes_core_only(self):
        assert SystemIoTool._scopes == {"core"}

    def test_config_key(self):
        assert SystemIoTool.config_key == "system_io"

    def test_parameters_action_enum_covers_all(self):
        params = SystemIoTool().parameters
        action_prop = params["properties"]["action"]
        assert set(action_prop["enum"]) == set(_ALL_ACTIONS)

    def test_parameters_required_action(self):
        params = SystemIoTool().parameters
        assert params["required"] == ["action"]

    def test_parameters_has_optional_fields(self):
        props = SystemIoTool().parameters["properties"]
        for field in (
            "text", "keys", "x", "y", "button", "scroll_dx", "scroll_dy",
            "port", "baudrate", "bytes_to_read", "timeout_ms",
        ):
            assert field in props, f"missing parameter: {field}"

    def test_docs_file_exists(self):
        # docs_consistency check requires the usage doc to exist on disk.
        docs_path = Path(__file__).resolve().parents[3] / "biscuitbot" / "agent" / "tools" / "docs" / "system_io.md"
        assert docs_path.is_file(), f"missing usage doc: {docs_path}"


# ---------------------------------------------------------------------------
# enabled() / create() factory
# ---------------------------------------------------------------------------


def _ctx(*, enable: bool, allow_actions: list[str] | None = None) -> Any:
    ctx = MagicMock()
    ctx.config.system_io = SystemIoToolConfig(
        enable=enable, allow_actions=allow_actions or [],
    )
    return ctx


class TestSystemIoToolEnabled:
    def test_enabled_false_by_default(self):
        assert SystemIoTool.enabled(_ctx(enable=False)) is False

    def test_enabled_true_when_enabled(self):
        assert SystemIoTool.enabled(_ctx(enable=True)) is True

    def test_create_returns_tool_with_config(self):
        ctx = _ctx(enable=True, allow_actions=["clipboard_read"])
        tool = SystemIoTool.create(ctx)
        assert isinstance(tool, SystemIoTool)
        assert tool.config.enable is True
        assert tool.config.allow_actions == ["clipboard_read"]

    def test_config_cls(self):
        assert SystemIoTool.config_cls() is SystemIoToolConfig


# ---------------------------------------------------------------------------
# Action dispatch & allowlist
# ---------------------------------------------------------------------------


class TestSystemIoToolDispatch:
    async def test_unknown_action_rejected(self):
        tool = SystemIoTool()
        result = await tool.execute(action="bogus")
        assert result.startswith("Error")
        assert "bogus" in result
        assert "Valid" in result

    async def test_allowlist_blocks_unlisted_action(self):
        tool = SystemIoTool(config=SystemIoToolConfig(
            enable=True, allow_actions=["clipboard_read"],
        ))
        result = await tool.execute(action="clipboard_write", text="x")
        assert result.startswith("Error")
        assert "allow_actions" in result or "allowlist" in result

    async def test_allowlist_permits_listed_action(self, monkeypatch):
        tool = SystemIoTool(config=SystemIoToolConfig(
            enable=True, allow_actions=["clipboard_read"],
        ))
        monkeypatch.setattr(tool, "_which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(tool, "_run", _fake_run(0, b"clip-content", b""))
        result = await tool.execute(action="clipboard_read")
        assert "clip-content" in result
        assert not result.startswith("Error")

    async def test_empty_allowlist_allows_all(self, monkeypatch):
        # Empty allowlist = all actions permitted (when enabled).
        tool = SystemIoTool(config=SystemIoToolConfig(enable=True))
        monkeypatch.setattr(tool, "_which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(tool, "_run", _fake_run(0, b"clip-content", b""))
        result = await tool.execute(action="clipboard_read")
        assert "clip-content" in result


# ---------------------------------------------------------------------------
# Per-action parameter validation
# ---------------------------------------------------------------------------


class TestSystemIoToolParamValidation:
    async def test_clipboard_write_requires_text(self):
        result = await SystemIoTool().execute(action="clipboard_write")
        assert _err(result) and "text" in result

    async def test_key_tap_requires_keys(self):
        result = await SystemIoTool().execute(action="key_tap")
        assert _err(result) and "keys" in result

    async def test_key_type_requires_text(self):
        result = await SystemIoTool().execute(action="key_type")
        assert _err(result) and "text" in result

    async def test_mouse_move_requires_x_y(self):
        result = await SystemIoTool().execute(action="mouse_move")
        assert _err(result) and "x" in result and "y" in result

    async def test_mouse_click_invalid_button(self):
        result = await SystemIoTool().execute(action="mouse_click", button="sideways")
        assert _err(result) and "button" in result

    async def test_mouse_scroll_requires_nonzero_delta(self):
        result = await SystemIoTool().execute(action="mouse_scroll")
        assert _err(result)

    async def test_serial_write_requires_port(self):
        result = await SystemIoTool().execute(action="serial_write", text="x")
        assert _err(result) and "port" in result

    async def test_serial_write_requires_text(self):
        result = await SystemIoTool().execute(action="serial_write", port="/dev/x")
        assert _err(result) and "text" in result

    async def test_serial_read_requires_port(self):
        result = await SystemIoTool().execute(action="serial_read")
        assert _err(result) and "port" in result


# ---------------------------------------------------------------------------
# Happy-path actions (mocked subprocess / backend)
# ---------------------------------------------------------------------------


class TestSystemIoToolHappyPaths:
    async def test_clipboard_read_returns_stdout(self, monkeypatch):
        tool = SystemIoTool()
        monkeypatch.setattr(tool, "_which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(tool, "_run", _fake_run(0, b"hello-clip", b""))
        result = await tool.execute(action="clipboard_read")
        assert result == "hello-clip"

    async def test_clipboard_read_reports_nonzero_exit(self, monkeypatch):
        tool = SystemIoTool()
        monkeypatch.setattr(tool, "_which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(tool, "_run", _fake_run(1, b"", b"no clipboard"))
        result = await tool.execute(action="clipboard_read")
        assert _err(result)

    async def test_clipboard_write_passes_text_as_stdin(self, monkeypatch):
        tool = SystemIoTool()
        monkeypatch.setattr(tool, "_which", lambda name: f"/usr/bin/{name}")
        captured: dict[str, Any] = {}

        async def fake_run(args, *, input_bytes=None, timeout=5.0):
            captured["input"] = input_bytes
            captured["args"] = args
            return 0, b"", b""

        monkeypatch.setattr(tool, "_run", fake_run)
        result = await tool.execute(action="clipboard_write", text="payload")
        assert "Clipboard updated" in result
        assert captured["input"] == b"payload"

    async def test_usb_list_returns_stdout(self, monkeypatch):
        tool = SystemIoTool()
        monkeypatch.setattr(tool, "_run", _fake_run(0, b"USB device list", b""))
        result = await tool.execute(action="usb_list")
        assert "USB device list" in result

    async def test_key_tap_via_xdotool_backend(self, monkeypatch):
        tool = SystemIoTool()
        monkeypatch.setattr(tool, "_keyboard_backend", lambda: "xdotool")
        monkeypatch.setattr(tool, "_run", _fake_run(0, b"", b""))
        # Force the Linux branch even on macOS test host.
        import biscuitbot.agent.tools.system_io as mod
        monkeypatch.setattr(mod, "_IS_LINUX", True)
        monkeypatch.setattr(mod, "_IS_MACOS", False)
        result = await tool.execute(action="key_tap", keys="ctrl+c")
        assert "Tapped keys: ctrl+c" in result

    async def test_key_type_empty_string_noop(self):
        result = await SystemIoTool().execute(action="key_type", text="")
        assert "no-op" in result or "empty" in result.lower()

    async def test_mouse_move_via_mocked_pynput(self, monkeypatch):
        import sys
        tool = SystemIoTool()
        monkeypatch.setattr(tool, "_mouse_backend", lambda: "pynput")
        # Inject fake pynput + pynput.mouse modules so the deferred import
        # inside the handler resolves to our stub.
        fake_mouse = MagicMock()
        controller = MagicMock()
        fake_mouse.Controller.return_value = controller
        fake_pynput = MagicMock()
        fake_pynput.mouse = fake_mouse
        monkeypatch.setitem(sys.modules, "pynput", fake_pynput)
        monkeypatch.setitem(sys.modules, "pynput.mouse", fake_mouse)
        result = await tool.execute(action="mouse_move", x=100, y=200)
        assert "Mouse moved to (100, 200)" in result
        # pynput 的 Controller.position 是属性（get/set），须赋值而非调用。
        assert controller.position == (100, 200)

    async def test_mouse_click_via_mocked_pynput_sets_position(self, monkeypatch):
        import sys
        tool = SystemIoTool()
        monkeypatch.setattr(tool, "_mouse_backend", lambda: "pynput")
        fake_mouse = MagicMock()
        controller = MagicMock()
        fake_mouse.Controller.return_value = controller
        fake_pynput = MagicMock()
        fake_pynput.mouse = fake_mouse
        monkeypatch.setitem(sys.modules, "pynput", fake_pynput)
        monkeypatch.setitem(sys.modules, "pynput.mouse", fake_mouse)
        result = await tool.execute(action="mouse_click", x=5, y=5, button="left")
        assert "Clicked left mouse button at (5, 5)" in result
        assert controller.position == (5, 5)
        controller.click.assert_called_once()

    async def test_serial_list_no_ports_returns_message(self, monkeypatch):
        tool = SystemIoTool()
        # Force the fallback path (no pyserial) and no matching devices.
        import sys
        monkeypatch.setitem(sys.modules, "serial", None)
        monkeypatch.setitem(sys.modules, "serial.tools", None)
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        result = await tool.execute(action="serial_list")
        assert "No serial ports" in result


# ---------------------------------------------------------------------------
# Timeout & exception handling
# ---------------------------------------------------------------------------


class TestSystemIoToolTimeoutAndErrors:
    async def test_timeout_returns_error(self, monkeypatch):
        tool = SystemIoTool()

        async def slow_dispatch(action, kwargs):
            await asyncio.sleep(5.0)
            return "should not reach"

        monkeypatch.setattr(tool, "_dispatch", slow_dispatch)
        result = await tool.execute(action="clipboard_read", timeout_ms=200)
        assert _err(result)
        assert "timed out" in result.lower()

    async def test_exception_caught_and_returned(self, monkeypatch):
        tool = SystemIoTool()

        async def boom(action, kwargs):
            raise RuntimeError("hardware on fire")

        monkeypatch.setattr(tool, "_dispatch", boom)
        result = await tool.execute(action="clipboard_read")
        assert _err(result)
        assert "hardware on fire" in result

    async def test_truncates_long_output(self, monkeypatch):
        tool = SystemIoTool()

        async def long_dispatch(action, kwargs):
            return "x" * 20_000

        monkeypatch.setattr(tool, "_dispatch", long_dispatch)
        result = await tool.execute(action="clipboard_read")
        assert len(result) < 20_000
        assert "truncated" in result


# ---------------------------------------------------------------------------
# Config schema integration
# ---------------------------------------------------------------------------


class TestSystemIoConfigIntegration:
    def test_tools_config_has_system_io_field(self):
        from biscuitbot.config.schema import ToolsConfig
        cfg = ToolsConfig()
        assert hasattr(cfg, "system_io")
        assert cfg.system_io.enable is False
        assert cfg.system_io.allow_actions == []

    def test_tools_config_loads_from_camel_case(self):
        from biscuitbot.config.schema import ToolsConfig
        cfg = ToolsConfig.model_validate({
            "systemIo": {"enable": True, "allowActions": ["clipboard_read"]},
        })
        assert cfg.system_io.enable is True
        assert cfg.system_io.allow_actions == ["clipboard_read"]

    def test_system_io_config_reexported_from_schema(self):
        from biscuitbot.config.schema import SystemIoToolConfig as Reexported
        assert Reexported is SystemIoToolConfig


# ---------------------------------------------------------------------------
# Loader discovery
# ---------------------------------------------------------------------------


class TestSystemIoToolDiscovery:
    def test_loader_discovers_system_io_class(self):
        from biscuitbot.agent.tools.loader import ToolLoader
        classes = ToolLoader().discover()
        names = [cls.__name__ for cls in classes]
        assert "SystemIoTool" in names

    def test_not_loaded_when_disabled(self):
        from biscuitbot.agent.tools.loader import ToolLoader
        from biscuitbot.agent.tools.registry import ToolRegistry
        ctx = _ctx(enable=False)
        registry = ToolRegistry()
        ToolLoader().load(ctx, registry)
        assert not registry.has("system_io")

    def test_loaded_when_enabled(self):
        from biscuitbot.agent.tools.loader import ToolLoader
        from biscuitbot.agent.tools.registry import ToolRegistry
        ctx = _ctx(enable=True)
        registry = ToolRegistry()
        ToolLoader().load(ctx, registry)
        assert registry.has("system_io")
        tool = registry.get("system_io")
        assert tool is not None
        assert tool.config.enable is True

    def test_registry_executes_via_dispatch(self):
        from biscuitbot.agent.tools.loader import ToolLoader
        from biscuitbot.agent.tools.registry import ToolRegistry
        ctx = _ctx(enable=True)
        registry = ToolRegistry()
        ToolLoader().load(ctx, registry)
        result = asyncio.run(registry.execute("system_io", {"action": "bogus"}))
        assert _err(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err(s: str) -> bool:
    return isinstance(s, str) and s.startswith("Error")


def _fake_run(rc: int, out: bytes, err: bytes) -> Any:
    async def _runner(args, *, input_bytes=None, timeout=5.0):
        return rc, out, err
    return _runner
