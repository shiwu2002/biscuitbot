"""Duplicate detection for tools and skills.

All tools are exposed to the model via the INDEX.md directory index.
This module detects functionally similar tools (or skills that overlap
with tools) so redundant entries can be merged before they confuse
retrieval and tool selection.

Similarity is the Jaccard coefficient over tokens extracted from each
entry's ``name`` + ``capability`` text.  Tokens are lowercased, split on
non-alphanumeric characters, and filtered against a stopword list and a
minimum length of 3.  Including the name in the token set means two
tools with similar names (even if the capability wording differs) are
also treated as duplicate candidates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hczkbot.agent.tools.base import Tool
    from hczkbot.agent.tools.registry import ToolRegistry


# Duplicate-pair categories.  TOOL_DUPLICATE means two registered tools
# overlap; SKILL_DUPLICATE means a skill entry overlaps with a tool.
TOOL_DUPLICATE = "TOOL_DUPLICATE"
SKILL_DUPLICATE = "SKILL_DUPLICATE"


@dataclass
class DuplicatePair:
    """A pair of tools (or skill-vs-tool) whose capabilities overlap."""

    tool_a: str  # name
    tool_b: str  # name
    similarity: float  # 0.0-1.0
    shared_tokens: list[str] = field(default_factory=list)
    # Discriminator used by build_duplicate_report to pick the right
    # merge suggestion.  Defaults to TOOL_DUPLICATE so positional
    # construction of a tool-tool pair still works.
    kind: str = TOOL_DUPLICATE


# Stopwords filtered out during tokenization.  Intentionally small and
# English-only since capabilities are written in English.
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
    """Lowercase, split on non-alphanumerics, drop stopwords and tokens shorter than 3."""
    tokens = _TOKEN_SPLIT.split((text or "").lower())
    return {t for t in tokens if len(t) >= 3 and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> tuple[float, set[str]]:
    """Return ``(Jaccard similarity, intersection)`` for two token sets.

    Two empty sets are treated as having zero similarity (no signal to
    compare), avoiding a division-by-zero.
    """
    if not a and not b:
        return 0.0, set()
    intersection = a & b
    union = a | b
    if not union:
        return 0.0, intersection
    return len(intersection) / len(union), intersection


def _tool_tokens(tool: "Tool") -> set[str]:
    """Tokens from a tool's name + capability (name included so similarly-named tools count)."""
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
    """Merge direction for one duplicate pair, based on its kind."""
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
