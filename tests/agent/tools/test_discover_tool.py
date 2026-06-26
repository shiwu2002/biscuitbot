"""Tests for the DiscoverToolsTool meta-tool (Layer 3 of dynamic tool selection)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hczkbot.agent.tools.base import Tool, tool_parameters
from hczkbot.agent.tools.discover import DiscoverToolsTool
from hczkbot.agent.tools.registry import ToolRegistry


def _make_tool(name: str, description: str, *, capability: str = "") -> Tool:
    @tool_parameters({"type": "object", "properties": {}})
    class _T(Tool):
        _capability = capability

        @property
        def name(self) -> str:
            return name

        @property
        def description(self) -> str:
            return description

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    return _T()


def _registry_with_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_make_tool(
        "screenshot",
        "Capture a screenshot and analyze it with vision model",
        capability="Capture a screenshot and analyze it with the configured vision model.",
    ))
    reg.register(_make_tool(
        "grep",
        "Search file contents by regex pattern",
        capability="Search file contents by regex pattern.",
    ))
    reg.register(_make_tool(
        "exec",
        "Execute shell commands",
        capability="Execute shell commands with timeout and sandbox support.",
    ))
    return reg


# --- enabled() gating ---

class _Ctx:
    def __init__(self, mode: str):
        self.config = type("Cfg", (), {"tool_selection_mode": mode})()


def test_discover_tools_enabled_only_in_dynamic_mode():
    assert DiscoverToolsTool.enabled(_Ctx("dynamic")) is True
    assert DiscoverToolsTool.enabled(_Ctx("all")) is False


# --- bind_registry contract ---

@pytest.mark.asyncio
async def test_discover_tools_returns_error_when_registry_not_bound():
    tool = DiscoverToolsTool()
    result = await tool.execute(query="screenshot")
    payload = json.loads(result)
    assert "error" in payload
    assert "not bound" in payload["error"]


@pytest.mark.asyncio
async def test_discover_tools_returns_full_schema_for_keyword_match():
    reg = _registry_with_tools()
    tool = DiscoverToolsTool()
    tool.bind_registry(reg)
    result = await tool.execute(query="screenshot")
    payload = json.loads(result)
    assert "tools" in payload
    assert len(payload["tools"]) >= 1
    assert payload["tools"][0]["function"]["name"] == "screenshot"
    # Schema should include parameters block
    assert "parameters" in payload["tools"][0]["function"]


@pytest.mark.asyncio
async def test_discover_tools_exact_name_match_ranks_first():
    reg = _registry_with_tools()
    tool = DiscoverToolsTool()
    tool.bind_registry(reg)
    # Query matches a tool name exactly
    result = await tool.execute(query="exec")
    payload = json.loads(result)
    assert payload["tools"][0]["function"]["name"] == "exec"


@pytest.mark.asyncio
async def test_discover_tools_returns_error_when_no_match():
    reg = _registry_with_tools()
    tool = DiscoverToolsTool()
    tool.bind_registry(reg)
    result = await tool.execute(query="xyzzy_unrelated_keyword")
    payload = json.loads(result)
    assert payload["error"] == "no matching tools found"
    assert payload["query"] == "xyzzy_unrelated_keyword"
    assert "hint" in payload


@pytest.mark.asyncio
async def test_discover_tools_respects_limit_parameter():
    reg = _registry_with_tools()
    tool = DiscoverToolsTool()
    tool.bind_registry(reg)
    # 's' matches screenshot's name + capability
    result = await tool.execute(query="s", limit=1)
    payload = json.loads(result)
    assert len(payload["tools"]) == 1


@pytest.mark.asyncio
async def test_discover_tools_limit_clamped_to_max_10():
    reg = _registry_with_tools()
    tool = DiscoverToolsTool()
    tool.bind_registry(reg)
    # limit=999 should be clamped to 10
    result = await tool.execute(query="s", limit=999)
    payload = json.loads(result)
    assert len(payload["tools"]) <= 10


# --- Tool metadata ---

def test_discover_tools_metadata():
    assert DiscoverToolsTool().name == "discover_tools"
    assert "request full tool definitions" in DiscoverToolsTool().description.lower()
    assert DiscoverToolsTool._always_include is True
    assert DiscoverToolsTool().capability  # non-empty fallback works


# --- Registry.fuzzy_search ---

def test_registry_fuzzy_search_returns_tools_in_score_order():
    reg = _registry_with_tools()
    # 'screenshot' matches screenshot's name exactly + capability
    matches = reg.fuzzy_search("screenshot", limit=5)
    assert matches[0].name == "screenshot"


def test_registry_fuzzy_search_returns_empty_for_no_match():
    reg = _registry_with_tools()
    matches = reg.fuzzy_search("zzzzz_no_match", limit=5)
    assert matches == []


# --- get_compact_summary ---

def test_compact_summary_lists_every_tool():
    reg = _registry_with_tools()
    summary = reg.get_compact_summary()
    assert "- screenshot:" in summary
    assert "- grep:" in summary
    assert "- exec:" in summary


def test_compact_summary_marks_loaded_tools():
    reg = _registry_with_tools()
    summary = reg.get_compact_summary(selected_names={"exec", "grep"})
    # Loaded tools get the (loaded) tag
    exec_line = next(line for line in summary.splitlines() if line.startswith("- exec:"))
    grep_line = next(line for line in summary.splitlines() if line.startswith("- grep:"))
    screenshot_line = next(line for line in summary.splitlines() if line.startswith("- screenshot:"))
    assert "(loaded)" in exec_line
    assert "(loaded)" in grep_line
    assert "(loaded)" not in screenshot_line


def test_compact_summary_cached_until_registry_changes():
    reg = _registry_with_tools()
    first = reg.get_compact_summary()
    second = reg.get_compact_summary()
    assert first is second or first == second  # Same content; cache hit
    # Register a new tool — cache should be invalidated
    reg.register(_make_tool("new_tool", "newcomer", capability="newcomer"))
    third = reg.get_compact_summary()
    assert "- new_tool:" in third
    assert "- new_tool:" not in first
