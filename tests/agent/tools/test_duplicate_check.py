"""Tests for the tool/skill duplicate detector."""

from __future__ import annotations

from typing import Any

import pytest

from biscuitbot.agent.tools.base import Tool, tool_parameters
from biscuitbot.agent.tools.duplicate_check import (
    SKILL_DUPLICATE,
    TOOL_DUPLICATE,
    DuplicatePair,
    build_duplicate_report,
    check_duplicates,
    check_skill_duplicates,
)
from biscuitbot.agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str, *, capability: str = "A tool.") -> Tool:
    """Build a minimal registered-style tool for testing."""
    @tool_parameters({"type": "object", "properties": {}})
    class _T(Tool):
        _capability = capability

        @property
        def name(self) -> str:
            return name

        @property
        def description(self) -> str:
            return capability

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    return _T()


# ---------------------------------------------------------------------------
# 1. No duplicates -> empty list
# ---------------------------------------------------------------------------

def test_no_duplicates_returns_empty():
    reg = ToolRegistry()
    reg.register(_make_tool("weather", capability="Get current weather for a city."))
    reg.register(_make_tool("web_search", capability="Search the web for information."))
    assert check_duplicates(reg) == []


# ---------------------------------------------------------------------------
# 2. High-similarity pair is detected
# ---------------------------------------------------------------------------

def test_high_similarity_pair_detected():
    reg = ToolRegistry()
    reg.register(_make_tool(
        "read_file",
        capability="Read the contents of a file from disk.",
    ))
    reg.register(_make_tool(
        "read_document",
        capability="Read the contents of a document from disk.",
    ))

    dups = check_duplicates(reg)
    assert len(dups) == 1
    d = dups[0]
    # Names are compared in sorted order: "read_document" < "read_file"
    assert d.tool_a == "read_document"
    assert d.tool_b == "read_file"
    # Tokens: {read, contents, disk} vs {read, contents, document, disk}
    # Jaccard = 3 / 4 = 0.75
    assert d.similarity == pytest.approx(0.75)
    assert d.shared_tokens == ["contents", "disk", "read"]
    assert d.kind == TOOL_DUPLICATE


# ---------------------------------------------------------------------------
# 3. Low-similarity pair is not reported
# ---------------------------------------------------------------------------

def test_low_similarity_not_reported():
    reg = ToolRegistry()
    reg.register(_make_tool(
        "search_files",
        capability="Search files by name pattern.",
    ))
    reg.register(_make_tool(
        "search_web",
        capability="Search the web for information.",
    ))
    # Share only the token "search": 1 / 6 ~= 0.167, below the 0.6 threshold.
    assert check_duplicates(reg) == []


# ---------------------------------------------------------------------------
# 4. Skill-vs-tool duplicate detection
# ---------------------------------------------------------------------------

def test_skill_tool_duplicate_detected():
    reg = ToolRegistry()
    reg.register(_make_tool(
        "read_file",
        capability="Read the contents of a file from disk.",
    ))
    skills = [
        {
            "name": "file_reader",
            "capability": "Read the contents of a file from disk.",
            "usage_md": "docs/file_reader.md",
        }
    ]

    dups = check_skill_duplicates(skills, reg)
    assert len(dups) == 1
    d = dups[0]
    # Skill name lands in tool_a, tool name in tool_b.
    assert d.tool_a == "file_reader"
    assert d.tool_b == "read_file"
    assert d.kind == SKILL_DUPLICATE
    # skill tokens {reader, read, contents, disk} vs tool {read, contents, disk}
    # Jaccard = 3 / 4 = 0.75
    assert d.similarity == pytest.approx(0.75)
    assert d.shared_tokens == ["contents", "disk", "read"]


def test_skill_tool_no_duplicate_returns_empty():
    reg = ToolRegistry()
    reg.register(_make_tool("weather", capability="Get current weather for a city."))
    skills = [
        {
            "name": "file_reader",
            "capability": "Read the contents of a file from disk.",
            "usage_md": "docs/file_reader.md",
        }
    ]
    assert check_skill_duplicates(skills, reg) == []


# ---------------------------------------------------------------------------
# 5. build_duplicate_report output format
# ---------------------------------------------------------------------------

def test_build_duplicate_report_tool_duplicate():
    duplicates = [
        DuplicatePair(
            tool_a="read_document",
            tool_b="read_file",
            similarity=0.75,
            shared_tokens=["contents", "disk", "read"],
            kind=TOOL_DUPLICATE,
        ),
    ]
    report = build_duplicate_report(duplicates)
    assert "工具/技能重复检测报告" in report
    assert "共检测到 1 对重复项" in report
    assert "read_document" in report
    assert "read_file" in report
    assert "TOOL_DUPLICATE" in report
    assert "0.75" in report
    assert "contents" in report
    assert "disk" in report
    assert "read" in report
    assert "保留功能更全面" in report


def test_build_duplicate_report_skill_duplicate_suggests_merge_into_tool():
    duplicates = [
        DuplicatePair(
            tool_a="file_reader",  # skill
            tool_b="read_file",    # tool
            similarity=0.75,
            shared_tokens=["contents", "disk", "read"],
            kind=SKILL_DUPLICATE,
        ),
    ]
    report = build_duplicate_report(duplicates)
    assert "file_reader" in report
    assert "read_file" in report
    assert "SKILL_DUPLICATE" in report
    # Merge direction: skill content -> tool.
    assert "合并到工具" in report
    assert "'read_file'" in report


def test_build_duplicate_report_empty():
    assert "未检测到重复" in build_duplicate_report([])


# ---------------------------------------------------------------------------
# 6. threshold parameter filters pairs
# ---------------------------------------------------------------------------

def test_threshold_filters_pairs():
    reg = ToolRegistry()
    reg.register(_make_tool(
        "read_file",
        capability="Read the contents of a file from disk.",
    ))
    reg.register(_make_tool(
        "read_document",
        capability="Read the contents of a document from disk.",
    ))
    # Similarity is 0.75.
    assert len(check_duplicates(reg, threshold=0.6)) == 1
    assert len(check_duplicates(reg, threshold=0.7)) == 1  # 0.75 >= 0.7
    assert check_duplicates(reg, threshold=0.8) == []      # 0.75 < 0.8


def test_threshold_applies_to_skill_duplicates():
    reg = ToolRegistry()
    reg.register(_make_tool(
        "read_file",
        capability="Read the contents of a file from disk.",
    ))
    skills = [
        {
            "name": "file_reader",
            "capability": "Read the contents of a file from disk.",
            "usage_md": "docs/file_reader.md",
        }
    ]
    # Similarity is 0.75.
    assert len(check_skill_duplicates(skills, reg, threshold=0.6)) == 1
    assert check_skill_duplicates(skills, reg, threshold=0.8) == []
