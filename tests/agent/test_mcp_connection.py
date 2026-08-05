"""Tests for MCP connection lifecycle in AgentLoop."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp import types as mcp_types
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from biscuitbot.agent.loop import AgentLoop
from biscuitbot.agent.tools import mcp as mcp_runtime
from biscuitbot.agent.tools.base import Tool
from biscuitbot.agent.tools.mcp import MCPResourceWrapper, MCPToolWrapper
from biscuitbot.bus.queue import MessageBus
from biscuitbot.config.loader import load_config, save_config
from biscuitbot.config.schema import MCPServerConfig


class _FakeMcpTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake MCP tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_kwargs: Any) -> str:
        return "ok"


def _make_loop(tmp_path, *, mcp_servers: dict | None = None) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation.max_tokens = 4096
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        mcp_servers=mcp_servers or {"test": object()},
    )


@pytest.mark.asyncio
async def test_connect_mcp_retries_when_no_servers_connect(tmp_path, monkeypatch: pytest.MonkeyPatch):
    loop = _make_loop(tmp_path)
    attempts = 0

    async def _fake_connect(_servers, _registry):
        nonlocal attempts
        attempts += 1
        return {}

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)

    await loop._connect_mcp()
    await loop._connect_mcp()

    assert attempts == 2
    assert loop._mcp_connected is False
    assert loop._mcp_stacks == {}


@pytest.mark.asyncio
async def test_reload_mcp_servers_adds_and_removes_tools_without_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    config = load_config()
    config.tools.mcp_servers["browserbase"] = MCPServerConfig(
        type="stdio",
        command="browserbase-mcp",
    )
    save_config(config)

    closed: list[str] = []

    async def _mark_closed(name: str) -> None:
        closed.append(name)

    async def _fake_connect(servers, registry):
        stacks = {}
        for name in servers:
            registry.register(_FakeMcpTool(f"mcp_{name}_navigate"))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stack.push_async_callback(_mark_closed, name)
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)
    loop = _make_loop(tmp_path, mcp_servers={})

    added = await mcp_runtime.reload_servers(loop, loop.tools)

    assert added["ok"] is True
    assert added["added"] == ["browserbase"]
    assert loop.tools.has("mcp_browserbase_navigate")
    assert "browserbase" in loop._mcp_stacks

    config = load_config()
    del config.tools.mcp_servers["browserbase"]
    save_config(config)

    removed = await mcp_runtime.reload_servers(loop, loop.tools)

    assert removed["ok"] is True
    assert removed["removed"] == ["browserbase"]
    assert not loop.tools.has("mcp_browserbase_navigate")
    assert "browserbase" not in loop._mcp_stacks
    assert closed == ["browserbase"]


@pytest.mark.asyncio
async def test_request_mcp_reload_reaches_runtime_control_without_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    config = load_config()
    config.tools.mcp_servers["browserbase"] = MCPServerConfig(
        type="stdio",
        command="browserbase-mcp",
    )
    save_config(config)

    closed: list[str] = []

    async def _mark_closed(name: str) -> None:
        closed.append(name)

    async def _fake_connect(servers, registry):
        stacks = {}
        for name in servers:
            registry.register(_FakeMcpTool(f"mcp_{name}_navigate"))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stack.push_async_callback(_mark_closed, name)
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)
    loop = _make_loop(tmp_path, mcp_servers={})

    async def _handle_one_runtime_control() -> None:
        msg = await loop.bus.consume_inbound()
        handled = await mcp_runtime.handle_runtime_control(loop, msg, loop.tools)
        assert handled is True

    consumer = asyncio.create_task(_handle_one_runtime_control())
    result = await mcp_runtime.request_mcp_reload(loop.bus, timeout=2.0)
    await consumer

    assert result["ok"] is True
    assert result["added"] == ["browserbase"]
    assert result["requires_restart"] is False
    assert loop.tools.has("mcp_browserbase_navigate")

    config = load_config()
    del config.tools.mcp_servers["browserbase"]
    save_config(config)

    consumer = asyncio.create_task(_handle_one_runtime_control())
    result = await mcp_runtime.request_mcp_reload(loop.bus, timeout=2.0)
    await consumer

    assert result["ok"] is True
    assert result["removed"] == ["browserbase"]
    assert result["requires_restart"] is False
    assert not loop.tools.has("mcp_browserbase_navigate")
    assert closed == ["browserbase"]


@pytest.mark.asyncio
async def test_reload_mcp_servers_retries_configured_server_without_live_stack(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)
    config = load_config()
    config.tools.mcp_servers["browserbase"] = MCPServerConfig(
        type="stdio",
        command="browserbase-mcp",
    )
    save_config(config)

    async def _fake_connect(servers, registry):
        stacks = {}
        for name in servers:
            registry.register(_FakeMcpTool(f"mcp_{name}_navigate"))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)
    loop = _make_loop(tmp_path, mcp_servers={"browserbase": config.tools.mcp_servers["browserbase"]})

    result = await mcp_runtime.reload_servers(loop, loop.tools)

    assert result["ok"] is True
    assert result["added"] == []
    assert result["changed"] == []
    assert result["retried"] == ["browserbase"]
    assert loop.tools.has("mcp_browserbase_navigate")
    await loop.close_mcp()


@pytest.mark.asyncio
async def test_mcp_tool_reconnects_after_session_terminated(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    loop = _make_loop(tmp_path, mcp_servers={"remote": object()})
    closed: list[str] = []
    sessions: list[Any] = []
    connect_count = 0

    async def _mark_closed(name: str) -> None:
        closed.append(name)

    class _FakeSession:
        def __init__(self, index: int) -> None:
            self.index = index
            self.call_count = 0

        async def call_tool(self, _name: str, arguments: dict[str, Any]) -> Any:
            self.call_count += 1
            assert arguments == {"symbol": "AAPL"}
            if self.index == 1:
                raise McpError(ErrorData(code=-32000, message="Session terminated"))
            return SimpleNamespace(
                content=[mcp_types.TextContent(type="text", text="recovered")]
            )

    async def _fake_connect(servers, registry):
        nonlocal connect_count
        stacks = {}
        for name in servers:
            connect_count += 1
            session = _FakeSession(connect_count)
            sessions.append(session)
            tool_def = SimpleNamespace(
                name="quote",
                description="quote tool",
                inputSchema={"type": "object", "properties": {}},
            )
            registry.register(MCPToolWrapper(session, name, tool_def, tool_timeout=5))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stack.push_async_callback(_mark_closed, name)
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)

    await loop._connect_mcp()
    old_tool = loop.tools.get("mcp_remote_quote")
    assert isinstance(old_tool, MCPToolWrapper)

    output = await old_tool.execute(symbol="AAPL")

    assert output == "recovered"
    assert connect_count == 2
    assert closed == ["remote"]
    assert sessions[0].call_count == 1
    assert sessions[1].call_count == 1
    assert "remote" in loop._mcp_stacks
    assert loop.tools.get("mcp_remote_quote") is not old_tool


@pytest.mark.asyncio
async def test_mcp_reconnect_handler_uses_sanitized_server_prefix(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    loop = _make_loop(tmp_path, mcp_servers={"remote_": object()})
    connect_count = 0

    class _FakeSession:
        def __init__(self, index: int) -> None:
            self.index = index

        async def call_tool(self, _name: str, arguments: dict[str, Any]) -> Any:
            assert arguments == {}
            if self.index == 1:
                raise McpError(ErrorData(code=-32000, message="Session terminated"))
            return SimpleNamespace(
                content=[mcp_types.TextContent(type="text", text="recovered")]
            )

    async def _fake_connect(servers, registry):
        nonlocal connect_count
        stacks = {}
        for name in servers:
            connect_count += 1
            tool_def = SimpleNamespace(
                name="quote",
                description="quote tool",
                inputSchema={"type": "object", "properties": {}},
            )
            registry.register(MCPToolWrapper(_FakeSession(connect_count), name, tool_def))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)

    await loop._connect_mcp()
    old_tool = loop.tools.get("mcp_remote_quote")
    assert isinstance(old_tool, MCPToolWrapper)

    output = await old_tool.execute()

    assert output == "recovered"
    assert connect_count == 2
    assert loop.tools.get("mcp_remote_quote") is not old_tool


@pytest.mark.asyncio
async def test_concurrent_mcp_reconnect_reuses_fresh_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    loop = _make_loop(tmp_path, mcp_servers={"remote": object()})
    closed: list[str] = []
    connect_count = 0

    async def _mark_closed(name: str) -> None:
        closed.append(name)

    class _DeadSession:
        async def read_resource(self, _uri: str) -> Any:
            raise McpError(ErrorData(code=-32000, message="Session terminated"))

    class _LiveSession:
        async def read_resource(self, uri: str) -> Any:
            await asyncio.sleep(0)
            return SimpleNamespace(
                contents=[
                    mcp_types.TextResourceContents(
                        uri=uri,
                        text=f"fresh:{uri.rsplit('/', maxsplit=1)[-1]}",
                    )
                ]
            )

    async def _fake_connect(servers, registry):
        nonlocal connect_count
        stacks = {}
        for name in servers:
            connect_count += 1
            session = _DeadSession() if connect_count == 1 else _LiveSession()
            for resource_name in ("alpha", "beta"):
                resource_def = SimpleNamespace(
                    name=resource_name,
                    uri=f"file:///{resource_name}",
                    description=f"{resource_name} resource",
                )
                registry.register(MCPResourceWrapper(session, name, resource_def))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stack.push_async_callback(_mark_closed, name)
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)

    await loop._connect_mcp()
    old_alpha = loop.tools.get("mcp_remote_resource_alpha")
    old_beta = loop.tools.get("mcp_remote_resource_beta")
    assert isinstance(old_alpha, MCPResourceWrapper)
    assert isinstance(old_beta, MCPResourceWrapper)

    outputs = await asyncio.gather(old_alpha.execute(), old_beta.execute())

    assert outputs == ["fresh:alpha", "fresh:beta"]
    assert connect_count == 2
    assert closed == ["remote"]


@pytest.mark.asyncio
async def test_mcp_reconnect_deferred_to_owner_task_when_called_from_subtask(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A reconnect triggered from a sub-task must run in the owner task.

    connect_mcp_servers enters anyio cancel scopes (via stdio_client) that are
    task-local. If the reconnect ran in the _dispatch sub-task, the new stack's
    scopes would be affined to that sub-task, and close_mcp() in the owner task
    couldn't exit them — raising "Attempted to exit cancel scope in a different
    task". The reconnect closure defers to the owner via a Future; this test
    verifies the deferral handshake and that connect_mcp_servers ends up
    running in the owner task.
    """
    loop = _make_loop(tmp_path, mcp_servers={"remote": object()})
    connect_count = 0
    connect_callers: list[Any] = []  # asyncio.Task objects that ran connect_mcp_servers

    class _FakeSession:
        def __init__(self, index: int) -> None:
            self.index = index

        async def call_tool(self, _name: str, arguments: dict[str, Any]) -> Any:
            assert arguments == {"symbol": "AAPL"}
            if self.index == 1:
                raise McpError(ErrorData(code=-32000, message="Session terminated"))
            return SimpleNamespace(
                content=[mcp_types.TextContent(type="text", text="recovered")]
            )

    async def _fake_connect(servers, registry):
        nonlocal connect_count
        connect_callers.append(asyncio.current_task())
        stacks = {}
        for name in servers:
            connect_count += 1
            tool_def = SimpleNamespace(
                name="quote",
                description="quote tool",
                inputSchema={"type": "object", "properties": {}},
            )
            registry.register(MCPToolWrapper(_FakeSession(connect_count), name, tool_def))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)

    # Initial connect runs in the test (owner) task.
    await loop._connect_mcp()
    owner_task = asyncio.current_task()
    loop._mcp_owner_task = owner_task

    old_tool = loop.tools.get("mcp_remote_quote")
    assert isinstance(old_tool, MCPToolWrapper)

    # Run the tool in a SUB-task (simulating _dispatch). It hits
    # "Session terminated", triggers reconnect, and — because the current task
    # is not the owner — defers via a Future and blocks until the owner drains.
    async def _run_in_subtask():
        return await old_tool.execute(symbol="AAPL")

    subtask = asyncio.create_task(_run_in_subtask(), name="dispatch-sub")

    # As the owner, wait for the deferred request to land, then drain it.
    # _process_mcp_reconnects runs _refresh_terminated_server in THIS task.
    for _ in range(500):  # ~5s ceiling
        if loop._mcp_reconnect_requests:
            break
        await asyncio.sleep(0.01)
    assert loop._mcp_reconnect_requests, "reconnect request was not deferred to the owner"
    await loop._process_mcp_reconnects()

    output = await subtask

    assert output == "recovered"
    assert connect_count == 2
    # The reconnect's connect_mcp_servers ran in the OWNER task, not the sub-task.
    assert connect_callers[1] is owner_task
    assert connect_callers[1] is not subtask
    # No leftover deferred requests.
    assert loop._mcp_reconnect_requests == []
    await loop.close_mcp()


@pytest.mark.asyncio
async def test_close_mcp_cancels_pending_reconnect_futures(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """close_mcp() must cancel pending reconnect futures so blocked sub-tasks unwind."""
    loop = _make_loop(tmp_path, mcp_servers={"remote": object()})

    async def _fake_connect(servers, registry):
        stacks = {}
        for name in servers:
            tool_def = SimpleNamespace(
                name="quote",
                description="quote tool",
                inputSchema={"type": "object", "properties": {}},
            )
            registry.register(MCPToolWrapper(MagicMock(), name, tool_def))
            stack = AsyncExitStack()
            await stack.__aenter__()
            stacks[name] = stack
        return stacks

    monkeypatch.setattr("biscuitbot.agent.tools.mcp.connect_mcp_servers", _fake_connect)
    await loop._connect_mcp()

    # Enqueue a fake pending reconnect request with a Future, as a sub-task would.
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    loop._mcp_reconnect_requests.append(("remote", "mcp_remote_quote", None, future))

    await loop.close_mcp()

    assert future.cancelled()
    assert loop._mcp_reconnect_requests == []
