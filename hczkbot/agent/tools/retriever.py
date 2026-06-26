"""Lightweight tool retrieval for dynamic tool selection.

This module implements Layer 2 of the dynamic-tool-selection design: a
pre-turn retrieval that picks which tools should be sent with full JSON
schema vs. only listed in the compact summary.

The retrieval is intentionally lightweight — TF-IDF weighted token overlap
without any embedding model — because:

1. The tool corpus is small (10-50 tools), so simple keyword matching is
   sufficient and explains well to humans.
2. Avoiding a network/embedding dependency keeps the agent loop latency
   predictable and the deployment footprint small.
3. The :class:`~hczkbot.agent.tools.discover.DiscoverToolsTool` meta-tool
   acts as a safety net for the long tail.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hczkbot.agent.tools.registry import ToolRegistry


# Common English stopwords that should not skew tool retrieval scores.
# Kept short on purpose — too many stopwords hurt tool name matching.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "has", "have", "in", "is", "it", "its", "of", "on", "or", "that",
        "the", "this", "to", "use", "with", "you", "your", "i", "me",
        "my", "we", "our",
    }
)


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, length >= 2, with stopwords removed."""
    raw = re.split(r"[^a-z0-9]+", (text or "").lower())
    return [t for t in raw if len(t) >= 2 and t not in _STOPWORDS]


class ToolRetriever:
    """Lightweight tool retrieval using TF-IDF + token overlap.

    The retriever builds an inverted index over each tool's
    ``name`` + ``capability`` + ``description`` + parameter names/descriptions,
    computes IDF scores, and ranks tools by summed TF-IDF overlap with the
    query.

    Tools marked ``_always_include = True`` are always returned regardless
    of the score (e.g. ``exec``, ``read_file``, ``discover_tools``).
    """

    def __init__(self, registry: "ToolRegistry", *, max_tools: int = 10):
        self._registry = registry
        self._max_tools = max_tools
        # tool_name -> list of tokens (with repetition for TF)
        self._doc_tokens: dict[str, list[str]] = {}
        # token -> idf score
        self._idf: dict[str, float] = {}
        # tool_name -> document length (token count) for length normalization
        self._doc_len: dict[str, int] = {}
        self._built = False

    def _collect_tool_text(self, tool) -> str:
        """Concatenate all searchable text from a tool definition."""
        parts: list[str] = [tool.name, tool.capability, tool.description]
        schema = tool.parameters or {}
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for prop_name, prop_schema in props.items():
                parts.append(prop_name)
                if isinstance(prop_schema, dict):
                    desc = prop_schema.get("description")
                    if isinstance(desc, str):
                        parts.append(desc)
        return " ".join(parts)

    def _build_index(self) -> None:
        """Build the TF-IDF index over all registered tools."""
        self._doc_tokens = {}
        self._doc_len = {}
        # Document frequency: token -> number of tools containing it
        df: dict[str, int] = {}
        for name, tool in self._registry._tools.items():
            tokens = _tokenize(self._collect_tool_text(tool))
            self._doc_tokens[name] = tokens
            self._doc_len[name] = len(tokens)
            for tok in set(tokens):
                df[tok] = df.get(tok, 0) + 1
        n_docs = len(self._doc_tokens) or 1
        self._idf = {
            tok: math.log((1 + n_docs) / (1 + df_count)) + 1.0
            for tok, df_count in df.items()
        }
        self._built = True

    def rebuild(self) -> None:
        """Force a rebuild on the next :meth:`select` call.

        Should be called after tools are registered/unregistered.  The
        registry normally triggers this implicitly through the
        ``_cached_definitions`` invalidation, but exposing it explicitly
        makes the contract obvious.
        """
        self._built = False

    def _score_tool(self, name: str, query_tokens: list[str]) -> float:
        """TF-IDF weighted overlap score for one tool against the query."""
        doc = self._doc_tokens.get(name, [])
        if not doc or not query_tokens:
            return 0.0
        tf = Counter(doc)
        doc_len = self._doc_len.get(name, 1) or 1
        score = 0.0
        # Use set for query tokens to avoid double counting
        for tok in set(query_tokens):
            if tok not in tf:
                continue
            idf = self._idf.get(tok, 1.0)
            # Normalized TF: (1 + log(tf)) / doc_len, weighted by idf
            score += (1.0 + math.log(tf[tok])) * idf / math.sqrt(doc_len)
            # Bonus for token appearing in tool name (strongest signal)
            if tok in name.lower():
                score += idf * 2.0
        return score

    def select(self, query: str) -> list[str]:
        """Return tool names relevant to *query*, plus always-include tools.

        The result is ordered: always-include tools first (in their
        registration-stable order), then top-K by score.  The total length
        is capped at :attr:`_max_tools`.
        """
        if not self._built:
            self._build_index()
        query_tokens = _tokenize(query)
        # 1. Always-include tools
        selected: list[str] = []
        seen: set[str] = set()
        for name, tool in self._registry._tools.items():
            if getattr(tool, "_always_include", False) and name not in seen:
                selected.append(name)
                seen.add(name)
        # 2. TF-IDF scored retrieval
        scored: list[tuple[float, str]] = []
        for name in self._registry._tools:
            if name in seen:
                continue
            score = self._score_tool(name, query_tokens)
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        for _, name in scored:
            if len(selected) >= self._max_tools:
                break
            selected.append(name)
            seen.add(name)
        return selected
