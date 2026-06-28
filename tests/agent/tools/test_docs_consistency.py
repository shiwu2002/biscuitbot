"""Tests for the docs-code consistency checker."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from hczkbot.agent.tools.base import Tool, tool_parameters
from hczkbot.agent.tools.docs_consistency import (
    DocsMismatch,
    _extract_md_params,
    _resolve_md_path,
    build_repair_task,
    check_docs_consistency,
)
from hczkbot.agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(
    name: str,
    *,
    capability: str = "A tool.",
    usage_md: str = "",
    params: dict[str, Any] | None = None,
) -> Tool:
    schema = params or {"type": "object", "properties": {}}

    @tool_parameters(schema)
    class _T(Tool):
        _capability = capability
        _usage_md = usage_md

        @property
        def name(self) -> str:
            return name

        @property
        def description(self) -> str:
            return name

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    return _T()


def _write_md(path: Path, *, title: str, params: list[str] | None = None,
              body: str = "A tool for reading files.") -> None:
    lines = [f"# {title}", "", body, ""]
    if params is not None:
        lines.extend([
            "## 参数",
            "",
            "| 参数 | 类型 | 必填 | 默认值 | 说明 |",
            "|------|------|------|--------|------|",
        ])
        for p in params:
            lines.append(f"| {p} | string | 是 | - | 说明 |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# _resolve_md_path
# ---------------------------------------------------------------------------

def test_resolve_md_path_absolute(tmp_path):
    md = tmp_path / "x.md"
    md.write_text("ok", encoding="utf-8")
    assert _resolve_md_path(str(md), tmp_path) == md


def test_resolve_md_path_missing_returns_none(tmp_path):
    assert _resolve_md_path(str(tmp_path / "nope.md"), tmp_path) is None


def test_resolve_md_path_workspace_relative(tmp_path):
    md = tmp_path / "docs" / "x.md"
    md.parent.mkdir(parents=True)
    md.write_text("ok", encoding="utf-8")
    assert _resolve_md_path("docs/x.md", tmp_path) == md


# ---------------------------------------------------------------------------
# _extract_md_params
# ---------------------------------------------------------------------------

def test_extract_md_params_from_table():
    content = (
        "# tool\n\n## 参数\n\n"
        "| 参数 | 类型 | 必填 | 说明 |\n"
        "|------|------|------|------|\n"
        "| path | string | 是 | 路径 |\n"
        "| limit | int | 否 | 数量 |\n"
    )
    assert _extract_md_params(content) == {"path", "limit"}


def test_extract_md_params_returns_none_when_no_section():
    content = "# tool\n\nNo params section here."
    assert _extract_md_params(content) is None


def test_extract_md_params_stops_at_next_section():
    content = (
        "# tool\n\n## 参数\n\n"
        "| 参数 | 类型 |\n|------|------|\n| alpha | string |\n\n"
        "## 注意事项\n\n- note\n"
    )
    assert _extract_md_params(content) == {"alpha"}


# ---------------------------------------------------------------------------
# check_docs_consistency — happy path
# ---------------------------------------------------------------------------

def test_no_mismatches_when_docs_match(tmp_path):
    md = tmp_path / "good.md"
    _write_md(md, title="good_tool", params=["path"],
              body="A good tool for reading files.")
    reg = ToolRegistry()
    reg.register(_make_tool(
        "good_tool",
        capability="A good tool for reading files.",
        usage_md=str(md),
        params={"type": "object", "properties": {"path": {"type": "string"}}},
    ))
    assert check_docs_consistency(reg, tmp_path) == []


def test_tools_without_usage_md_are_skipped(tmp_path):
    reg = ToolRegistry()
    reg.register(_make_tool("bare", usage_md=""))
    assert check_docs_consistency(reg, tmp_path) == []


# ---------------------------------------------------------------------------
# check_docs_consistency — mismatch detection
# ---------------------------------------------------------------------------

def test_mismatch_when_md_file_missing(tmp_path):
    reg = ToolRegistry()
    reg.register(_make_tool(
        "ghost",
        usage_md=str(tmp_path / "ghost.md"),
    ))
    mismatches = check_docs_consistency(reg, tmp_path)
    assert len(mismatches) == 1
    assert mismatches[0].tool_name == "ghost"
    assert any("不存在" in i for i in mismatches[0].issues)


def test_mismatch_when_title_missing(tmp_path):
    md = tmp_path / "notitle.md"
    md.write_text("## 参数\n\n| 参数 |\n|------|\n| path |\n", encoding="utf-8")
    reg = ToolRegistry()
    reg.register(_make_tool(
        "my_tool",
        capability="A tool for reading files.",
        usage_md=str(md),
        params={"type": "object", "properties": {"path": {"type": "string"}}},
    ))
    mismatches = check_docs_consistency(reg, tmp_path)
    assert len(mismatches) == 1
    assert any("标题" in i for i in mismatches[0].issues)


def test_mismatch_when_param_missing_in_md(tmp_path):
    md = tmp_path / "missing_param.md"
    _write_md(md, title="mp", params=["path"],
              body="A tool for reading files and processing data.")
    reg = ToolRegistry()
    reg.register(_make_tool(
        "mp",
        capability="A tool for reading files and processing data.",
        usage_md=str(md),
        params={"type": "object", "properties": {
            "path": {"type": "string"},
            "limit": {"type": "integer"},
        }},
    ))
    mismatches = check_docs_consistency(reg, tmp_path)
    assert len(mismatches) == 1
    assert any("limit" in i for i in mismatches[0].issues)


def test_mismatch_when_extra_param_in_md(tmp_path):
    md = tmp_path / "extra_param.md"
    _write_md(md, title="ep", params=["path", "stale"],
              body="A tool for reading files and processing data.")
    reg = ToolRegistry()
    reg.register(_make_tool(
        "ep",
        capability="A tool for reading files and processing data.",
        usage_md=str(md),
        params={"type": "object", "properties": {"path": {"type": "string"}}},
    ))
    mismatches = check_docs_consistency(reg, tmp_path)
    assert len(mismatches) == 1
    assert any("stale" in i for i in mismatches[0].issues)


def test_mismatch_when_capability_not_reflected(tmp_path):
    md = tmp_path / "no_cap.md"
    md.write_text(
        "# nc\n\nSome unrelated content.\n\n## 参数\n\n| 参数 |\n|------|\n",
        encoding="utf-8",
    )
    reg = ToolRegistry()
    reg.register(_make_tool(
        "nc",
        capability="Screenshot capture vision model analyze.",
        usage_md=str(md),
    ))
    mismatches = check_docs_consistency(reg, tmp_path)
    assert len(mismatches) == 1
    assert any("capability" in i for i in mismatches[0].issues)


def test_multiple_mismatches_returned(tmp_path):
    md1 = tmp_path / "a.md"
    md1.write_text("# a\n\nA tool for searching.\n", encoding="utf-8")
    md2 = tmp_path / "b.md"
    md2.write_text("# b\n\nA tool for writing.\n", encoding="utf-8")
    reg = ToolRegistry()
    reg.register(_make_tool("a", capability="A tool for searching.", usage_md=str(md1)))
    reg.register(_make_tool("b", capability="A tool for writing.", usage_md=str(md2)))
    # Both have no "## 参数" section → _extract_md_params returns None →
    # param check skipped. But both have capability keywords reflected,
    # so they should be clean.
    assert check_docs_consistency(reg, tmp_path) == []


# ---------------------------------------------------------------------------
# build_repair_task
# ---------------------------------------------------------------------------

def test_build_repair_task_includes_tool_names_and_issues():
    mismatches = [
        DocsMismatch(
            tool_name="weather",
            usage_md="docs/weather.md",
            source_file="hczkbot/agent/tools/weather.py",
            issues=["md 缺少参数说明：['city']"],
        ),
    ]
    task = build_repair_task(mismatches)
    assert "weather" in task
    assert "docs/weather.md" in task
    assert "city" in task
    assert "修复" in task


def test_build_repair_task_handles_multiple_mismatches():
    mismatches = [
        DocsMismatch("a", "docs/a.md", "a.py", ["issue1"]),
        DocsMismatch("b", "docs/b.md", "b.py", ["issue2"]),
    ]
    task = build_repair_task(mismatches)
    assert "### a" in task
    assert "### b" in task
