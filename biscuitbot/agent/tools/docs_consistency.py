"""文档-代码一致性检查器，服务于渐进式发现机制。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的文档一致性检查组件。
在项目架构中起到的作用：每个工具都有 ``_usage_md`` 指向 ``docs/<name>.md``
使用说明文档，本模块负责检查这些文档与实际工具代码保持同步：

1. md 文件存在。
2. md 包含 ``# <tool_name>`` 标题。
3. md 中的参数表与 ``tool.parameters`` schema 一致。
4. ``_capability`` 描述在 md 正文中有所体现。

由系统定时任务（``docs_consistency_check``）每夜运行，发现不一致时
派生子代理重新生成过时文档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biscuitbot.agent.tools.base import Tool  # 工具基类，仅用于类型提示
    from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表，仅用于类型提示


@dataclass
class DocsMismatch:
    """单个工具的文档与代码不一致的记录。

    用于汇总某个工具文档检查中发现的所有问题。
    """

    tool_name: str  # 工具名称
    usage_md: str  # 使用说明文档路径
    source_file: str  # 工具源码文件路径
    issues: list[str] = field(default_factory=list)  # 发现的问题列表


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------

_TOOLS_DIR = Path(__file__).resolve().parent  # 工具包目录


def _resolve_md_path(usage_md: str, workspace: Path) -> Path | None:
    """将 ``_usage_md`` 解析为实际文件路径。

    内置工具使用相对于工具包的 ``docs/<name>.md``，
    自定义工具可能使用工作区相对路径或绝对路径。

    参数:
        usage_md: 使用说明路径字符串。
        workspace: 工作区路径。

    返回:
        文件存在时返回 Path，否则返回 None。
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
    """尽力获取工具 Python 模块的源码文件路径。"""
    module = getattr(tool.__class__, "__module__", "")
    if module and module.startswith("biscuitbot."):
        parts = module.split(".")
        return str(Path(_TOOLS_DIR.parent.parent.parent, *parts)) + ".py"
    return ""


# ---------------------------------------------------------------------------
# 参数提取
# ---------------------------------------------------------------------------

def _extract_schema_params(tool: "Tool") -> set[str] | None:
    """从工具的 JSON schema 中提取参数名集合，不适用时返回 None。"""
    params = tool.parameters
    if not isinstance(params, dict):
        return None
    properties = params.get("properties")
    if not isinstance(properties, dict):
        return None
    return set(properties.keys()) if properties else set()


_SECTION_HEADERS = ("参数", "Parameters", "参数说明", "参数列表")  # 支持的参数章节标题


def _extract_md_params(content: str) -> set[str] | None:
    """从 md 的参数表中提取参数名。

    未找到参数章节时返回 None（以便检查器跳过参数比较，而非误报不一致）。

    参数:
        content: md 文件内容。

    返回:
        参数名集合；无参数章节时返回 None。
    """
    lines = content.splitlines()
    start = -1
    # 定位参数章节起始行
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
    seen_separator = False  # 是否已遇到表格分隔行
    in_table = False  # 是否已进入数据行区域
    for line in lines[start:]:
        stripped = line.strip()
        # 遇到下一个章节标题则停止
        if stripped.startswith("## ") or stripped.startswith("# "):
            break
        # 解析表格行：| param_name | type | ... |
        if not stripped.startswith("|"):
            # 数据行之后遇到非表格行则结束主参数表（避免误读嵌套子表）
            if in_table:
                break
            continue
        cells = [c.strip() for c in stripped.split("|")]
        # cells[0] 为首个 | 前的空串，cells[1] 为第一列
        if len(cells) < 2:
            continue
        first_col = cells[1].strip("`").strip("**").strip()
        if not first_col:
            continue
        # 分隔行（如 |------|------|）
        if first_col.startswith("---") or first_col.startswith(":-"):
            seen_separator = True
            continue
        # 分隔行之前为表头行，跳过
        if not seen_separator:
            continue
        # 分隔行之后的数据行
        params.add(first_col)
        in_table = True
    return params if params else None


# ---------------------------------------------------------------------------
# 能力描述检查
# ---------------------------------------------------------------------------

# 英文停用词集合，用于能力关键词提取时过滤无意义词
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this", "its",
    "are", "was", "were", "has", "have", "not", "but", "via", "can",
    "all", "any", "new", "old", "use", "using", "used", "tool", "file",
    "returns", "return", "when", "will", "they", "them", "their",
})


def _contains_cjk(text: str) -> bool:
    """判断文本是否包含 CJK（中日韩）字符。"""
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _capability_keywords(capability: str) -> list[str]:
    """从能力描述字符串中提取有意义的关键词。"""
    tokens = re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", capability.lower())
    return [t for t in tokens if len(t) >= 4 and t not in _STOPWORDS]


# ---------------------------------------------------------------------------
# 单工具检查
# ---------------------------------------------------------------------------

def _check_one_tool(tool: "Tool", workspace: Path) -> list[str]:
    """检查单个工具的文档一致性，返回问题列表（空表示一致）。

    参数:
        tool: 待检查的工具实例。
        workspace: 工作区路径。

    返回:
        问题描述列表。
    """
    issues: list[str] = []
    usage_md = getattr(tool, "_usage_md", "")
    if not usage_md:
        return issues  # 无 _usage_md 的工具跳过检查

    md_path = _resolve_md_path(usage_md, workspace)
    if md_path is None:
        issues.append(f"使用说明文件不存在：{usage_md}")
        return issues

    try:
        content = md_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        issues.append(f"读取使用说明失败（{e}），请检查文件编码或权限：{usage_md}")
        return issues

    # 1. 标题检查
    title_expected = f"# {tool.name}"
    if title_expected not in content:
        issues.append(f"md 缺少工具名标题 '{title_expected}'")

    # 2. 参数一致性检查
    schema_params = _extract_schema_params(tool)
    md_params = _extract_md_params(content)
    if schema_params is not None and md_params is not None:
        missing_in_md = schema_params - md_params
        extra_in_md = md_params - schema_params
        if missing_in_md:
            issues.append(f"md 缺少参数说明：{sorted(missing_in_md)}")
        if extra_in_md:
            issues.append(f"md 多余参数说明：{sorted(extra_in_md)}")

    # 3. 能力描述体现检查
    capability = getattr(tool, "_capability", "")
    if capability:
        keywords = _capability_keywords(capability)
        content_lower = content.lower()
        matched = [kw for kw in keywords if kw in content_lower]
        # 要求至少一个关键词出现在文档正文中
        # 对中文文档跳过此检查：英文能力关键词自然无法匹配中文文本
        # 仅检查参数表之前的正文部分——能力描述应位于此处
        if keywords and not matched:
            body_only = content
            # 查找首个中文参数章节标题，截取其前的正文
            for hdr in _SECTION_HEADERS:
                hdr_match = re.search(rf"^## {re.escape(hdr)}", content, re.MULTILINE)
                if hdr_match:
                    body_only = content[:hdr_match.start()]
                    break
            if not _contains_cjk(body_only):
                issues.append(f"capability 描述未在 md 中体现：{capability}")

    return issues


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def check_docs_consistency(
    registry: "ToolRegistry",
    workspace: Path,
) -> list[DocsMismatch]:
    """检查所有已注册工具的文档与代码一致性。

    参数:
        registry: 工具注册表。
        workspace: 工作区路径。

    返回:
        文档不一致的工具列表（:class:`DocsMismatch`）；空列表表示全部一致。
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
    """构建文档修复子代理的任务提示词。

    参数:
        mismatches: 不匹配的工具列表。

    返回:
        供子代理使用的任务提示词字符串。
    """
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
