"""Tests for the generic repeated-tool-call loop detector."""

from __future__ import annotations

from biscuitbot.utils.runtime import (
    build_repeated_error_reminder_message,
    error_signature,
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


def test_shell_signature_keeps_and_chain_distinct():
    """``&&`` 串联的不同操作（cd 进目录 + 不同命令）不应被折叠成同一签名。

    回归：此前 ``&&`` 也被折叠，所有 ``cd <workspace> && ffprobe/ffmpeg...`` 命令
    都坍缩成 ``cd <workspace>``，超过重复预算后合法命令链全被误伤拦截。
    """
    a = tool_call_signature(
        "exec",
        {"command": "cd /Users/x/ws && ffprobe -v error vo15.mp3 && ffmpeg -y -i promo.mp4 out.mp4"},
    )
    b = tool_call_signature(
        "exec",
        {"command": "cd /Users/x/ws && ffmpeg -y -i a.mp4 -frames:v 1 f.png"},
    )
    assert a != b
    assert "ffprobe" in a
    assert "ffmpeg" in b


def test_shell_signature_repeated_and_chain_still_escalates():
    """完全相同的 ``&&`` 链仍应被识别为重复调用（死循环兜底不失效）。"""
    counts: dict[str, int] = {}
    cmd = {"command": "cd /Users/x/ws && ffmpeg -y -i a.mp4 out.mp4"}
    repeated_tool_call_error("exec", cmd, counts)  # 第 1 次
    repeated_tool_call_error("exec", cmd, counts)  # 第 2 次
    repeated_tool_call_error("exec", cmd, counts)  # 第 3 次
    fourth = repeated_tool_call_error("exec", cmd, counts)  # 第 4 次 → 拦截
    assert fourth is not None


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


# ---- error_signature / build_repeated_error_reminder_message（连续同类错误提醒） ----


def test_error_signature_collapses_transient_details():
    """URL、路径、引号、数字/ID 差异应坍缩，本质相同的错误映射到同一签名。"""
    a = error_signature("web_search", "Error: TimeoutError: https://cn.bing.com/search?q=foo timed out")
    b = error_signature("web_search", "Error: TimeoutError: https://cn.bing.com/search?q=bar timed out")
    assert a == b
    assert a.startswith("web_search:error:")
    assert "timeouterror" in a
    assert "<url>" in a


def test_error_signature_strips_quoted_and_ids():
    """引号内容与数字 ID 是易变信息，不应区分不同错误。"""
    a = error_signature("read_file", "Error: FileNotFoundError: no such file '/a/b/c.txt' (id 123)")
    b = error_signature("read_file", "Error: FileNotFoundError: no such file '/x/y/z.md' (id 456)")
    assert a == b


def test_error_signature_keeps_error_type_distinct():
    """不同错误类型（类型名不同）应保持不同签名。"""
    a = error_signature("web_fetch", "Error: TimeoutError: request timed out")
    b = error_signature("web_fetch", "Error: HTTPStatusError: server returned 500")
    assert a != b


def test_error_signature_different_tool_stays_distinct():
    """工具名参与签名：同名错误发生在不同工具上不算「同一个错误」。"""
    a = error_signature("web_search", "Error: TimeoutError: timed out")
    b = error_signature("web_fetch", "Error: TimeoutError: timed out")
    assert a != b


def test_error_signature_truncates_long_text():
    sig = error_signature("exec", "Error: RuntimeError: " + "x" * 500)
    assert sig.startswith("exec:")
    assert len(sig) <= 240 + len("exec:")


def test_repeated_error_reminder_message_mentions_threshold():
    msg = build_repeated_error_reminder_message("Error: TimeoutError: down", 3)
    assert "3" in msg
    assert "换一种完全不同的思路" in msg
    assert "系统提示：" in msg


def test_repeated_error_reminder_message_truncates_snippet():
    long_err = "boom " * 50
    msg = build_repeated_error_reminder_message(long_err, 3)
    # 片段截断到 120 字符 + 省略号
    assert "…" in msg
    assert "boom" in msg
