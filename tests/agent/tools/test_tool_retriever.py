"""Tests for the dynamic tool retriever (Layer 2 of dynamic tool selection)."""

from __future__ import annotations

from typing import Any

import pytest

from hczkbot.agent.tools.base import Tool, tool_parameters
from hczkbot.agent.tools.registry import ToolRegistry
from hczkbot.agent.tools.retriever import ToolRetriever, _tokenize


def _make_tool(name: str, description: str, *, capability: str = "", always_include: bool = False) -> Tool:
    """Build a minimal Tool instance with the given metadata."""

    @tool_parameters({"type": "object", "properties": {}})
    class _T(Tool):
        _capability = capability
        _always_include = always_include

        @property
        def name(self) -> str:
            return name

        @property
        def description(self) -> str:
            return description

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    return _T()


def _registry_with(tools: list[Tool]) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


# --- Tokenizer ---

def test_tokenize_strips_short_tokens_and_stopwords():
    tokens = _tokenize("I am a Tool with the word 'exec' and 1-letter bits")
    assert "exec" in tokens
    assert "i" not in tokens  # single-letter
    assert "the" not in tokens  # stopword
    assert "with" not in tokens  # stopword
    assert "tool" in tokens


# --- Always-include ---

def test_select_returns_always_include_first_even_with_unrelated_query():
    always = _make_tool("exec", "shell stuff", capability="Run shell commands", always_include=True)
    other = _make_tool("grep", "regex search", capability="Search file contents")
    reg = _registry_with([other, always])
    retriever = ToolRetriever(reg, max_tools=10)
    # Query that doesn't match any tool — only always-include should return
    selected = retriever.select("xyzzy unrelated")
    assert "exec" in selected
    # 'grep' may or may not appear (zero score) but exec always comes first
    assert selected[0] == "exec"


def test_select_respects_max_tools_cap():
    always = _make_tool("exec", "shell", capability="Run shell", always_include=True)
    tools = [always] + [_make_tool(f"tool_{i}", f"do thing {i}", capability=f"thing {i}") for i in range(20)]
    reg = _registry_with(tools)
    retriever = ToolRetriever(reg, max_tools=5)
    selected = retriever.select("thing 0 1 2 3 4 5 6 7")
    assert len(selected) == 5
    assert "exec" in selected  # always-include counted against cap


# --- TF-IDF ranking ---

def test_select_ranks_keyword_match_above_non_matches():
    screenshot = _make_tool("screenshot", "Capture screen and analyze with vision model")
    grep = _make_tool("grep", "Search file contents by regex pattern")
    message = _make_tool("message", "Send messages to users", always_include=True)
    reg = _registry_with([screenshot, grep, message])
    retriever = ToolRetriever(reg, max_tools=10)
    selected = retriever.select("take a screenshot of the desktop")
    assert "screenshot" in selected
    # grep should not match a screenshot query strongly
    # Order: message (always-include) first, then screenshot, then maybe grep
    assert selected.index("screenshot") < selected.index("grep") if "grep" in selected else True


def test_select_returns_empty_for_zero_match_when_no_always_include():
    tool = _make_tool("grep", "search file contents by regex")
    reg = _registry_with([tool])
    retriever = ToolRetriever(reg, max_tools=10)
    selected = retriever.select("xyzzy unrelated query")
    assert selected == []


def test_select_with_empty_query_returns_only_always_include():
    always = _make_tool("exec", "shell", capability="Run shell", always_include=True)
    other = _make_tool("grep", "regex search", capability="Search files")
    reg = _registry_with([other, always])
    retriever = ToolRetriever(reg, max_tools=10)
    selected = retriever.select("")
    assert selected == ["exec"]


def test_rebuild_picks_up_newly_registered_tools():
    reg = _registry_with([_make_tool("exec", "shell", capability="Run shell", always_include=True)])
    retriever = ToolRetriever(reg, max_tools=10)
    retriever.select("shell")  # builds index
    # Register a new tool after initial build
    reg.register(_make_tool("screenshot", "capture screen"))
    retriever.rebuild()
    selected = retriever.select("capture screen")
    assert "screenshot" in selected


# --- get_definitions_for_turn ---

def test_get_definitions_for_turn_all_mode_returns_everything():
    a = _make_tool("a", "alpha")
    b = _make_tool("b", "beta")
    reg = _registry_with([a, b])
    defs = reg.get_definitions_for_turn("", mode="all")
    assert {d["function"]["name"] for d in defs} == {"a", "b"}


def test_get_definitions_for_turn_dynamic_mode_returns_subset():
    always = _make_tool("exec", "shell", capability="Run shell", always_include=True)
    screenshot = _make_tool("screenshot", "Capture screen")
    grep = _make_tool("grep", "Search file contents")
    reg = _registry_with([always, screenshot, grep])
    defs = reg.get_definitions_for_turn("take a screenshot", mode="dynamic", max_tools=5)
    names = {d["function"]["name"] for d in defs}
    assert "exec" in names  # always-include
    assert "screenshot" in names  # keyword match
    assert "grep" not in names  # not matched


def test_get_definitions_for_turn_preserves_cache_friendly_order():
    """Builtins first, then MCP, sorted alphabetically — same as get_definitions."""
    always = _make_tool("exec", "shell", capability="Run shell", always_include=True)
    zebra = _make_tool("zebra_tool", "zzz", capability="zebra")
    apple = _make_tool("apple_tool", "aaa", capability="apple")
    reg = _registry_with([zebra, apple, always])
    defs = reg.get_definitions_for_turn("apple zebra", mode="dynamic", max_tools=5)
    names = [d["function"]["name"] for d in defs]
    # Sorted alphabetically (all builtins, no mcp_ prefix)
    assert names == sorted(names)
