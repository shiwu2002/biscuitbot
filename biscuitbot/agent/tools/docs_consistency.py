"""Docs-code consistency checker for progressive discovery.

Every tool has a ``_usage_md`` pointing to a ``docs/<name>.md`` usage doc.
This module checks that those docs stay in sync with the actual tool code:

1. The md file exists.
2. The md has a ``# <tool_name>`` title.
3. The parameters table in the md matches ``tool.parameters`` schema.
4. The ``_capability`` description is reflected in the md body.

A scheduled system cron job (``docs_consistency_check``) runs this checker
nightly; on mismatch it spawns a subagent to regenerate the stale docs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biscuitbot.agent.tools.base import Tool
    from biscuitbot.agent.tools.registry import ToolRegistry


@dataclass
class DocsMismatch:
    """A single tool whose docs are out of sync with its code."""

    tool_name: str
    usage_md: str
    source_file: str
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).resolve().parent


def _resolve_md_path(usage_md: str, workspace: Path) -> Path | None:
    """Resolve ``_usage_md`` to an actual file path.

    Built-in tools use ``docs/<name>.md`` relative to the tools package.
    Custom tools may use a workspace-relative or absolute path.
    """
    p = Path(usage_md)
    if p.is_absolute():
        return p if p.is_file() else None
    candidate = _TOOLS_DIR / usage_md
    if candidate.is_file():
        return candidate
    candidate = workspace / usage_md
    return candidate if candidate.is_file() else None


def _resolve_source_file(tool: "Tool") -> str:
    """Best-effort source-file path for the tool's Python module."""
    module = getattr(tool.__class__, "__module__", "")
    if module and module.startswith("biscuitbot."):
        parts = module.split(".")
        return str(Path(_TOOLS_DIR.parent.parent.parent, *parts)) + ".py"
    return ""


# ---------------------------------------------------------------------------
# Parameter extraction
# ---------------------------------------------------------------------------

def _extract_schema_params(tool: "Tool") -> set[str] | None:
    """Return parameter names from the tool's JSON schema, or None if N/A."""
    params = tool.parameters
    if not isinstance(params, dict):
        return None
    properties = params.get("properties")
    if not isinstance(properties, dict):
        return None
    return set(properties.keys()) if properties else set()


_SECTION_HEADERS = ("参数", "Parameters", "参数说明", "参数列表")


def _extract_md_params(content: str) -> set[str] | None:
    """Extract parameter names from the md's parameter table.

    Returns ``None`` when no parameter section is found (so the checker
    can skip the param comparison instead of flagging a false mismatch).
    """
    lines = content.splitlines()
    start = -1
    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        for header in _SECTION_HEADERS:
            if stripped == f"## {header.lower()}" or stripped.startswith(f"## {header.lower()}"):
                start = i + 1
                break
        if start != -1:
            break
    if start == -1:
        return None

    params: set[str] = set()
    seen_separator = False
    in_table = False
    for line in lines[start:]:
        stripped = line.strip()
        # Stop at the next section header
        if stripped.startswith("## ") or stripped.startswith("# "):
            break
        # Parse table rows: | param_name | type | ... |
        if not stripped.startswith("|"):
            # Non-table line after we've started collecting data rows
            # stops the main parameter table (avoids nested sub-tables).
            if in_table:
                break
            continue
        cells = [c.strip() for c in stripped.split("|")]
        # cells[0] is empty (before first |), cells[1] is first column
        if len(cells) < 2:
            continue
        first_col = cells[1].strip("`").strip("**").strip()
        if not first_col:
            continue
        # Separator row (e.g., |------|------|)
        if first_col.startswith("---") or first_col.startswith(":-"):
            seen_separator = True
            continue
        # Rows before the separator are header rows — skip them
        if not seen_separator:
            continue
        # Data row after the separator
        params.add(first_col)
        in_table = True
    return params if params else None


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this", "its",
    "are", "was", "were", "has", "have", "not", "but", "via", "can",
    "all", "any", "new", "old", "use", "using", "used", "tool", "file",
    "returns", "return", "when", "will", "they", "them", "their",
})


def _contains_cjk(text: str) -> bool:
    """Return True if *text* contains CJK (Chinese/Japanese/Korean) characters."""
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _capability_keywords(capability: str) -> list[str]:
    """Extract meaningful keywords from the capability string."""
    tokens = re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", capability.lower())
    return [t for t in tokens if len(t) >= 4 and t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# Single-tool check
# ---------------------------------------------------------------------------

def _check_one_tool(tool: "Tool", workspace: Path) -> list[str]:
    """Return a list of issues for *tool*, empty if docs are consistent."""
    issues: list[str] = []
    usage_md = getattr(tool, "_usage_md", "")
    if not usage_md:
        return issues  # skip tools without _usage_md

    md_path = _resolve_md_path(usage_md, workspace)
    if md_path is None:
        issues.append(f"使用说明文件不存在：{usage_md}")
        return issues

    content = md_path.read_text(encoding="utf-8")

    # 1. Title check
    title_expected = f"# {tool.name}"
    if title_expected not in content:
        issues.append(f"md 缺少工具名标题 '{title_expected}'")

    # 2. Parameter consistency
    schema_params = _extract_schema_params(tool)
    md_params = _extract_md_params(content)
    if schema_params is not None and md_params is not None:
        missing_in_md = schema_params - md_params
        extra_in_md = md_params - schema_params
        if missing_in_md:
            issues.append(f"md 缺少参数说明：{sorted(missing_in_md)}")
        if extra_in_md:
            issues.append(f"md 多余参数说明：{sorted(extra_in_md)}")

    # 3. Capability reflection
    capability = getattr(tool, "_capability", "")
    if capability:
        keywords = _capability_keywords(capability)
        content_lower = content.lower()
        matched = [kw for kw in keywords if kw in content_lower]
        # Require at least one keyword to appear in the doc body.
        # Skip the check for Chinese docs: English capability keywords
        # naturally won't match Chinese text.  We only look at the body
        # *before* the parameter table — that's where the capability
        # description belongs.
        if keywords and not matched:
            body_only = content
            # Find first occurrence of a Chinese parameter-section header
            for hdr in _SECTION_HEADERS:
                hdr_match = re.search(rf"^## {re.escape(hdr)}", content, re.MULTILINE)
                if hdr_match:
                    body_only = content[:hdr_match.start()]
                    break
            if not _contains_cjk(body_only):
                issues.append(f"capability 描述未在 md 中体现：{capability}")

    return issues


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_docs_consistency(
    registry: "ToolRegistry",
    workspace: Path,
) -> list[DocsMismatch]:
    """Check all registered tools' docs against their code.

    Returns a list of :class:`DocsMismatch` for tools whose docs are
    stale or missing.  An empty list means everything is in sync.
    """
    mismatches: list[DocsMismatch] = []
    for name in sorted(registry.tool_names):
        tool = registry.get(name)
        if tool is None:
            continue
        usage_md = getattr(tool, "_usage_md", "")
        if not usage_md:
            continue
        issues = _check_one_tool(tool, workspace)
        if issues:
            mismatches.append(DocsMismatch(
                tool_name=name,
                usage_md=usage_md,
                source_file=_resolve_source_file(tool),
                issues=issues,
            ))
    return mismatches


def build_repair_task(mismatches: list[DocsMismatch]) -> str:
    """Build the task prompt for the docs-repair subagent."""
    lines = [
        "你是文档修复智能体。以下工具的使用说明 md 文件与工具代码不匹配，",
        "请逐一修复每个不匹配的文件。",
        "",
        "## 修复流程",
        "1. 用 read_file 读取工具的 Python 源码，了解正确的参数定义和能力描述",
        "2. 用 read_file 读取当前的 docs md 文件（如果存在）",
        "3. 用 write_file 写入修复后的 md，保持以下格式：",
        "   - 第一行是 '# {tool_name}'",
        "   - '## 何时使用' — 简述使用场景",
        "   - '## 参数' — markdown 表格：| 参数 | 类型 | 必填 | 默认值 | 说明 |",
        "   - '## 调用示例' — 代码块示例",
        "   - '## 注意事项' — 列表",
        "4. 参数必须与代码中的 tool_parameters schema 完全一致",
        "5. 内容用中文编写",
        "",
        "## 不匹配列表",
        "",
    ]
    for m in mismatches:
        lines.append(f"### {m.tool_name}")
        lines.append(f"- 使用说明路径: {m.usage_md}")
        if m.source_file:
            lines.append(f"- 工具源码: {m.source_file}")
        lines.append("- 问题:")
        for issue in m.issues:
            lines.append(f"  - {issue}")
        lines.append("")
    return "\n".join(lines)
