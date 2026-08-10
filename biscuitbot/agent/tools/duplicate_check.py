"""工具与技能的重复检测模块。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的重复检测组件。
在项目架构中起到的作用：所有工具通过 INDEX.md 目录索引暴露给模型，
本模块检测功能相似的工具（或与工具重叠的技能），以便在冗余条目干扰
检索与工具选择之前进行合并。

相似度基于各条目 ``name`` + ``capability`` 文本提取的 token 的 Jaccard
系数。token 经小写化、按非字母数字字符切分，并过滤停用词与长度小于 3
的词。将名称纳入 token 集意味着名称相似的两个工具（即使能力描述措辞
不同）也会被视为重复候选。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from biscuitbot.agent.tools.base import Tool  # 工具基类，仅用于类型提示
    from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表，仅用于类型提示


# 重复对类别。TOOL_DUPLICATE 表示两个已注册工具重叠；
# SKILL_DUPLICATE 表示某个技能条目与工具重叠。
TOOL_DUPLICATE = "TOOL_DUPLICATE"
SKILL_DUPLICATE = "SKILL_DUPLICATE"


@dataclass
class DuplicatePair:
    """A pair of tools (or skill-vs-tool) whose capabilities overlap.

    中文说明：一对能力重叠的工具（或技能-工具对）的记录。
    """

    tool_a: str  # 名称 A
    tool_b: str  # 名称 B
    similarity: float  # 相似度 0.0-1.0
    shared_tokens: list[str] = field(default_factory=list)  # 共享的 token 列表
    # 区分类型，供 build_duplicate_report 选择合适的合并建议。
    # 默认为 TOOL_DUPLICATE，使位置式构造工具-工具对仍可工作。
    kind: str = TOOL_DUPLICATE


# Stopwords filtered out during tokenization.  Intentionally small and
# English-only since capabilities are written in English.
# 中文说明：分词时过滤的停用词集合。故意保持较小且仅含英文，因为能力描述以英文编写。
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this", "its",
    "are", "was", "were", "has", "have", "not", "but", "via", "can",
    "all", "any", "new", "old", "use", "using", "used", "tool", "file",
    "returns", "return", "when", "will", "they", "them", "their",
    "a", "an", "in", "on", "at", "to", "of", "by", "or", "as", "is",
    "be", "it", "do", "if", "so", "no", "up",
})


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords and tokens shorter than 3.

    中文说明：小写化、按非字母数字字符切分，过滤停用词与长度小于 3 的 token。
    """
    tokens = _TOKEN_SPLIT.split((text or "").lower())
    return {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> tuple[float, set[str]]:
    """Return ``(Jaccard similarity, intersection)`` for two token sets.

    Two empty sets are treated as having zero similarity (no signal to
    compare), avoiding a division-by-zero.

    中文说明：返回两个 token 集合的 ``(Jaccard 相似度, 交集)``。
    两个空集视为相似度为零（无比较信号），避免除零错误。
    """
    if not a and not b:
        return 0.0, set()
    intersection = a & b
    union = a | b
    if not union:
        return 0.0, intersection
    return len(intersection) / len(union), intersection


def _tool_tokens(tool: "Tool") -> set[str]:
    """Tokens from a tool's name + capability (name included so similarly-named tools count).

    中文说明：从工具的 name + capability 提取 token（纳入名称使得名称相似的工具也会被计入）。
    """
    return _tokenize(f"{tool.name} {tool.capability}")


def check_duplicates(
    registry: "ToolRegistry",
    threshold: float = 0.6,
) -> list[DuplicatePair]:
    """Detect tool-vs-tool duplicates in *registry*.

    Compares every pair of registered tools by the Jaccard overlap of
    tokens extracted from each tool's ``name`` + ``capability``.  Pairs
    whose similarity is at least *threshold* are returned, sorted by
    descending similarity (ties broken by name for stable output).

    中文说明：检测 *registry* 中工具与工具之间的重复。通过比较每个已注册工具
    从 ``name`` + ``capability`` 提取的 token 的 Jaccard 重叠度，返回相似度
    不低于 *threshold* 的工具对，按相似度降序排列（相同相似度按名称排序以保证稳定输出）。
    """
    names = sorted(registry.tool_names)
    entries: list[tuple[str, set[str]]] = []
    for name in names:
        tool = registry.get(name)
        if tool is None:
            continue
        entries.append((name, _tool_tokens(tool)))

    duplicates: list[DuplicatePair] = []
    for i in range(len(entries)):
        name_a, tokens_a = entries[i]
        for j in range(i + 1, len(entries)):
            name_b, tokens_b = entries[j]
            similarity, shared = _jaccard(tokens_a, tokens_b)
            if similarity >= threshold:
                duplicates.append(DuplicatePair(
                    tool_a=name_a,
                    tool_b=name_b,
                    similarity=similarity,
                    shared_tokens=sorted(shared),
                    kind=TOOL_DUPLICATE,
                ))

    duplicates.sort(key=lambda d: (-d.similarity, d.tool_a, d.tool_b))
    return duplicates


def check_skill_duplicates(
    skills_entries: list[dict[str, str]],
    registry: "ToolRegistry",
    threshold: float = 0.6,
) -> list[DuplicatePair]:
    """Detect skill-vs-tool duplicates.

    ``skills_entries`` is a list of ``{"name", "capability", "usage_md"}``
    dicts (the same shape passed to :meth:`ToolRegistry.generate_index`).
    For each skill, its ``name`` + ``capability`` tokens are compared
    against every registered tool's tokens.  Pairs at or above
    *threshold* are returned, sorted by descending similarity.

    The skill name is placed in ``tool_a`` and the tool name in
    ``tool_b``; ``kind`` is :data:`SKILL_DUPLICATE`.

    中文说明：检测技能与工具之间的重复。``skills_entries`` 是
    ``{"name", "capability", "usage_md"}`` 字典列表（与
    :meth:`ToolRegistry.generate_index` 传入的形状相同）。将每个技能的
    ``name`` + ``capability`` token 与所有已注册工具的 token 进行比较，
    返回相似度不低于 *threshold* 的对，按相似度降序排列。技能名置于
    ``tool_a``，工具名置于 ``tool_b``，``kind`` 为 :data:`SKILL_DUPLICATE`。
    """
    skill_entries: list[tuple[str, set[str]]] = []
    for skill in skills_entries:
        name = skill.get("name", "")
        capability = skill.get("capability", "")
        skill_entries.append((name, _tokenize(f"{name} {capability}")))

    tool_entries: list[tuple[str, set[str]]] = []
    for name in sorted(registry.tool_names):
        tool = registry.get(name)
        if tool is None:
            continue
        tool_entries.append((name, _tool_tokens(tool)))

    duplicates: list[DuplicatePair] = []
    for skill_name, s_tokens in skill_entries:
        for tool_name, t_tokens in tool_entries:
            similarity, shared = _jaccard(s_tokens, t_tokens)
            if similarity >= threshold:
                duplicates.append(DuplicatePair(
                    tool_a=skill_name,
                    tool_b=tool_name,
                    similarity=similarity,
                    shared_tokens=sorted(shared),
                    kind=SKILL_DUPLICATE,
                ))

    duplicates.sort(key=lambda d: (-d.similarity, d.tool_a, d.tool_b))
    return duplicates


def _suggestion(d: DuplicatePair) -> str:
    """Merge direction for one duplicate pair, based on its kind.

    中文说明：根据重复对的类型，给出单个重复对的合并方向建议。
    """
    if d.kind == SKILL_DUPLICATE:
        return (
            f"将 skill '{d.tool_a}' 的内容合并到工具 '{d.tool_b}' 中，"
            f"然后移除该 skill 条目，避免 INDEX.md 中出现重复能力。"
        )
    return (
        "保留功能更全面的工具，将另一个的能力描述与使用场景合并进来，"
        "再卸载冗余的工具。"
    )


def build_duplicate_report(duplicates: list[DuplicatePair]) -> str:
    """Build a human-readable (Chinese) report of duplicate pairs.

    Each entry lists the pair, category, similarity, shared keywords,
    and a merge suggestion:

    * ``SKILL_DUPLICATE`` — merge the skill content into the tool.
    * ``TOOL_DUPLICATE``  — keep the more comprehensive tool and merge
      the other into it.
    """
    if not duplicates:
        return "未检测到重复的工具或技能。"

    lines: list[str] = [
        "# 工具/技能重复检测报告",
        "",
        f"共检测到 {len(duplicates)} 对重复项。",
        "",
    ]
    for idx, d in enumerate(duplicates, start=1):
        lines.append(f"## {idx}. {d.tool_a} ↔ {d.tool_b}")
        lines.append(f"- 类型: {d.kind}")
        lines.append(f"- 相似度: {d.similarity:.2f}")
        if d.shared_tokens:
            lines.append(f"- 共享关键词: {', '.join(d.shared_tokens)}")
        else:
            lines.append("- 共享关键词: (无)")
        lines.append(f"- 建议: {_suggestion(d)}")
        lines.append("")

    return "\n".join(lines)
