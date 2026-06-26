"""Tests for usage stats tracking and cold-storage rotation."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hczkbot.agent.tools.base import Tool, tool_parameters
from hczkbot.agent.tools.cold_storage import ColdStorageTool
from hczkbot.agent.tools.registry import ToolRegistry
from hczkbot.agent.tools.usage_stats import ColdEntry, UsageStats


def _make_tool(tool_name: str, *, always_include: bool = False) -> Tool:
    @tool_parameters({"type": "object", "properties": {}})
    class _T(Tool):
        _capability = f"Tool {tool_name} capability"
        _always_include = always_include
        _usage_md = f"docs/{tool_name}.md"

        @property
        def name(self) -> str:
            return tool_name

        @property
        def description(self) -> str:
            return tool_name

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    return _T()


# ---------------------------------------------------------------------------
# UsageStats basics
# ---------------------------------------------------------------------------

def test_record_call_increments_count(tmp_path):
    stats = UsageStats(base_dir=tmp_path)
    stats.record_call("exec")
    stats.record_call("exec")
    assert stats._stats["exec"].call_count == 2
    assert stats._stats["exec"].last_called_at > 0


def test_record_call_persists_to_disk(tmp_path):
    stats = UsageStats(base_dir=tmp_path)
    stats.record_call("exec")
    # Reload from disk
    stats2 = UsageStats(base_dir=tmp_path)
    assert stats2._stats["exec"].call_count == 1


# ---------------------------------------------------------------------------
# Cold storage rotation
# ---------------------------------------------------------------------------

def test_rotate_cold_moves_stale_tools(tmp_path):
    reg = ToolRegistry()
    reg.register(_make_tool("stale_tool"))
    reg.register(_make_tool("active_tool"))
    reg.register(_make_tool("core_tool", always_include=True))

    stats = UsageStats(base_dir=tmp_path)
    # Record a call for active_tool, but set it to 20 days ago
    stats.record_call("active_tool")
    stats._stats["active_tool"].last_called_at = time.time() - 20 * 86400

    # Record a call for stale_tool, also 20 days ago
    stats.record_call("stale_tool")
    stats._stats["stale_tool"].last_called_at = time.time() - 20 * 86400

    # Record a call for core_tool recently
    stats.record_call("core_tool")

    newly_cold = stats.rotate_cold(reg, threshold_days=14)
    assert "stale_tool" in newly_cold
    assert "active_tool" in newly_cold  # also stale
    assert "core_tool" not in newly_cold  # always-include never rotated


def test_rotate_cold_skips_never_called_tools(tmp_path):
    reg = ToolRegistry()
    reg.register(_make_tool("never_used"))
    stats = UsageStats(base_dir=tmp_path)
    # No call recorded — should be skipped (given a chance)
    newly_cold = stats.rotate_cold(reg, threshold_days=14)
    assert newly_cold == []


def test_rotate_cold_disabled_when_threshold_zero(tmp_path):
    reg = ToolRegistry()
    reg.register(_make_tool("x"))
    stats = UsageStats(base_dir=tmp_path)
    stats.record_call("x")
    stats._stats["x"].last_called_at = time.time() - 365 * 86400
    assert stats.rotate_cold(reg, threshold_days=0) == []


# ---------------------------------------------------------------------------
# Auto-recovery on call
# ---------------------------------------------------------------------------

def test_calling_cold_tool_auto_recovers(tmp_path):
    reg = ToolRegistry()
    reg.register(_make_tool("cold_one"))
    stats = UsageStats(base_dir=tmp_path)
    reg.set_usage_stats(stats)

    # Make it cold
    stats.record_call("cold_one")
    stats._stats["cold_one"].last_called_at = time.time() - 30 * 86400
    stats.rotate_cold(reg, threshold_days=14)
    assert stats.is_cold("cold_one")

    # Record a new call → auto-recover
    stats.record_call("cold_one")
    assert not stats.is_cold("cold_one")


# ---------------------------------------------------------------------------
# Cold storage search
# ---------------------------------------------------------------------------

def test_search_cold_returns_matching_entries(tmp_path):
    stats = UsageStats(base_dir=tmp_path)
    stats._cold["weather"] = ColdEntry(
        name="weather", capability="Get weather forecast",
        usage_md="docs/weather.md", cold_since=time.time(),
    )
    stats._cold["calc"] = ColdEntry(
        name="calc", capability="Calculator tool",
        usage_md="docs/calc.md", cold_since=time.time(),
    )
    results = stats.search_cold("weather")
    assert len(results) == 1
    assert results[0].name == "weather"


def test_search_cold_returns_empty_when_no_match(tmp_path):
    stats = UsageStats(base_dir=tmp_path)
    stats._cold["x"] = ColdEntry(name="x", capability="x", usage_md="")
    assert stats.search_cold("nonexistent") == []


# ---------------------------------------------------------------------------
# ColdStorageTool
# ---------------------------------------------------------------------------

def test_cold_storage_tool_metadata():
    assert ColdStorageTool().name == "cold_storage"
    assert ColdStorageTool._always_include is True
    assert ColdStorageTool._usage_md == "docs/cold_storage.md"


@pytest.mark.asyncio
async def test_cold_storage_returns_empty_message_when_no_cold(tmp_path):
    stats = UsageStats(base_dir=tmp_path)
    tool = ColdStorageTool()
    tool.bind_usage_stats(stats)
    import json
    result = json.loads(await tool.execute(query="anything"))
    assert result["cold_count"] == 0
    assert "为空" in result["message"]


@pytest.mark.asyncio
async def test_cold_storage_returns_error_when_not_bound():
    import json
    tool = ColdStorageTool()
    result = json.loads(await tool.execute(query="test"))
    assert "error" in result


@pytest.mark.asyncio
async def test_cold_storage_finds_cold_tool(tmp_path):
    stats = UsageStats(base_dir=tmp_path)
    stats._cold["weather"] = ColdEntry(
        name="weather", capability="Get weather forecast for city",
        usage_md="docs/weather.md", cold_since=time.time(),
    )
    tool = ColdStorageTool()
    tool.bind_usage_stats(stats)
    import json
    result = json.loads(await tool.execute(query="weather"))
    assert result["cold_count"] == 1
    assert result["matched"] == 1
    assert result["tools"][0]["name"] == "weather"
    assert "discover_tools" in result["tools"][0]["hint"]


# ---------------------------------------------------------------------------
# INDEX.md excludes cold tools
# ---------------------------------------------------------------------------

def test_generate_index_excludes_cold_tools(tmp_path):
    reg = ToolRegistry()
    reg.register(_make_tool("normal"))
    reg.register(_make_tool("frozen"))
    stats = UsageStats(base_dir=tmp_path)
    reg.set_usage_stats(stats)

    # Make "frozen" cold
    stats._cold["frozen"] = ColdEntry(
        name="frozen", capability="frozen tool", usage_md="docs/frozen.md",
    )

    index = reg.generate_index()
    assert "normal" in index
    assert "frozen" not in index
