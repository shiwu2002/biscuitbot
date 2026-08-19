"""Tests for the generic repeated-tool-call loop detector."""

from __future__ import annotations

from biscuitbot.utils.runtime import (
    repeated_tool_call_error,
    tool_call_signature,
)


def test_shell_signature_collapses_grep_and_redirect_variations():
    """不同的 grep/head 尾部应坍缩到同一个 find 基命令签名。"""
    a = tool_call_signature(
        "exec",
        {"command": 'find /Applications/biscuitbot.app -name "*.md" 2>/dev/null | grep -i "tool" | head -20'},
    )
    b = tool_call_signature(
        "exec",
        {"command": 'find /Applications/biscuitbot.app -name "*.md" 2>/dev/null | grep -i "usage" | head -20'},
    )
    assert a == b
    assert 'find /Applications/biscuitbot.app -name "*.md"' in a


def test_shell_signature_for_plain_command():
    assert tool_call_signature("exec", {"command": "git status"}) == "exec:git status"


def test_shell_signature_strips_trailing_redirect():
    assert tool_call_signature("exec", {"command": "ls -la 2>/dev/null"}) == "exec:ls -la"


def test_generic_signature_serializes_arguments():
    sig = tool_call_signature("read_file", {"path": "docs/read_file.md", "offset": 1})
    assert sig is not None
    assert sig.startswith("read_file:")


def test_signature_none_for_web_tools():
    """web 查找有专用节流，跳过以免双重计数。"""
    assert tool_call_signature("web_search", {"query": "anything"}) is None
    assert tool_call_signature("web_fetch", {"url": "https://example.com"}) is None


def test_signature_none_for_non_dict_args():
    assert tool_call_signature("exec", ["not", "a", "dict"]) is None


def test_repeated_tool_call_returns_none_within_budget():
    counts: dict[str, int] = {}
    args = {"command": "find /tmp -name '*.md'"}

    assert repeated_tool_call_error("exec", args, counts) is None
    assert repeated_tool_call_error("exec", args, counts) is None
    assert repeated_tool_call_error("exec", args, counts) is None


def test_repeated_tool_call_escalates_after_budget():
    counts: dict[str, int] = {}
    args = {"command": "find /tmp -name '*.md'"}

    repeated_tool_call_error("exec", args, counts)  # 第 1 次
    repeated_tool_call_error("exec", args, counts)  # 第 2 次
    repeated_tool_call_error("exec", args, counts)  # 第 3 次
    fourth = repeated_tool_call_error("exec", args, counts)  # 第 4 次 → 拦截

    assert fourth is not None
    assert "repeated this same tool call" in fourth


def test_repeated_tool_call_collapses_shell_variations():
    """仅改动 grep 词尾的重复 find 也应被识别为同一调用。"""
    counts: dict[str, int] = {}
    repeated_tool_call_error(
        "exec", {"command": 'find /tmp -name "*.md" 2>/dev/null | grep -i "tool"'}, counts,
    )
    repeated_tool_call_error(
        "exec", {"command": 'find /tmp -name "*.md" 2>/dev/null | grep -i "usage"'}, counts,
    )
    repeated_tool_call_error(
        "exec", {"command": 'find /tmp -name "*.md" 2>/dev/null | grep -i "docs"'}, counts,
    )
    fourth = repeated_tool_call_error(
        "exec", {"command": 'find /tmp -name "*.md" 2>/dev/null | grep -i "video"'}, counts,
    )
    assert fourth is not None


def test_repeated_tool_call_independent_per_signature():
    """不同命令各自拥有独立的重复预算。"""
    counts: dict[str, int] = {}
    repeated_tool_call_error("exec", {"command": "ls"}, counts)
    repeated_tool_call_error("exec", {"command": "ls"}, counts)
    repeated_tool_call_error("exec", {"command": "ls"}, counts)

    assert repeated_tool_call_error("exec", {"command": "pwd"}, counts) is None
