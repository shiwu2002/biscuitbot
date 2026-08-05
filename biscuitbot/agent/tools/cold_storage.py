"""Cold storage meta-tool.

Lets the agent search for tools/skills that have been rotated out of the
active INDEX.md because they were not called for ``cold_storage_days``.
When the agent finds a needed tool here and calls it, the tool is
automatically restored to the active index.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from biscuitbot.agent.tools.base import Tool, tool_parameters

if TYPE_CHECKING:
    from biscuitbot.agent.tools.usage_stats import UsageStats


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "关键词或工具名，用于搜索冷门仓库中的工具/技能",
        },
        "limit": {
            "type": "integer",
            "description": "最大返回数量（默认 5，上限 10）",
        },
    },
    "required": ["query"],
})
class ColdStorageTool(Tool):
    """Search cold storage for tools not in the active index."""

    _capability = (
        "Search cold storage for tools/skills rotated out of the active index. "
        "When a cold tool is found and called, it auto-restores to the index."
    )
    _always_include = True
    _usage_md = "docs/cold_storage.md"

    def __init__(self) -> None:
        self._stats: UsageStats | None = None

    def bind_usage_stats(self, stats: "UsageStats") -> None:
        self._stats = stats

    @property
    def name(self) -> str:
        return "cold_storage"

    @property
    def description(self) -> str:
        return "Search cold storage for tools/skills not in the active index."

    async def execute(self, **kwargs: Any) -> Any:
        query = kwargs.get("query", "")
        limit = kwargs.get("limit") or 5
        if not isinstance(limit, int) or limit < 1:
            limit = 5
        limit = min(limit, 10)

        if self._stats is None:
            return json.dumps({"error": "usage stats not bound"}, ensure_ascii=False)

        cold_names = self._stats.cold_tool_names()
        if not cold_names:
            return json.dumps({
                "message": "冷门仓库为空。所有工具都在活跃索引中。",
                "cold_count": 0,
            }, ensure_ascii=False)

        entries = self._stats.search_cold(query, limit=limit)
        if not entries:
            return json.dumps({
                "message": f"未找到匹配 '{query}' 的冷门工具。",
                "cold_count": len(cold_names),
                "hint": "尝试用更通用的关键词，或用 cold_storage(query='list all') 查看全部。",
            }, ensure_ascii=False)

        # If query asks to list all, return all cold entries
        if query.lower().strip() in ("list all", "all", "list", "全部", "所有"):
            entries = [self._stats.get_cold_entry(n) for n in cold_names]
            entries = [e for e in entries if e is not None]

        tools = []
        for e in entries:
            tools.append({
                "name": e.name,
                "capability": e.capability,
                "usage_md": e.usage_md,
                "source_file": e.source_file,
                "hint": f"用 discover_tools(\"{e.name}\") 加载 schema 后即可调用，调用后自动恢复到活跃索引。",
            })

        return json.dumps({
            "cold_count": len(cold_names),
            "matched": len(tools),
            "tools": tools,
        }, ensure_ascii=False)
