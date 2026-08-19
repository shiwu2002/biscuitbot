"""冷门仓库元工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的冷门仓库检索组件。
在项目架构中起到的作用：让 agent 能够搜索因长时间未被调用而从活跃
INDEX.md 中轮转出去的工具/技能。当 agent 在冷门仓库中找到所需工具并调用
时，该工具会自动恢复到活跃索引中，实现工具的渐进式发现与回收。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器

if TYPE_CHECKING:
    from biscuitbot.agent.tools.usage_stats import UsageStats  # 使用统计，仅用于类型提示


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
    """在冷门仓库中搜索不在活跃索引中的工具。

    职责：检索因长期未使用而被轮转到冷门仓库的工具/技能，并在被调用后
    自动将其恢复到活跃索引。

    用法：由 agent 调用，传入搜索关键词与可选的返回数量上限。
    """

    _capability = (
        "Search cold storage for tools/skills rotated out of the active index. "
        "When a cold tool is found and called, it auto-restores to the index."
    )
    _always_include = True  # 该工具的完整 schema 始终发送给模型
    _usage_md = "docs/cold_storage.md"  # 工具使用说明文档路径

    def __init__(self) -> None:
        """初始化冷门仓库工具，使用统计初始为空。"""
        self._stats: UsageStats | None = None

    def bind_usage_stats(self, stats: "UsageStats") -> None:
        """绑定使用统计实例，用于查询冷门工具。

        参数:
            stats: 使用统计对象。
        """
        self._stats = stats

    @property
    def name(self) -> str:
        """工具名称。"""
        return "cold_storage"

    @property
    def description(self) -> str:
        """工具描述。"""
        return "Search cold storage for tools/skills not in the active index."

    async def execute(self, **kwargs: Any) -> Any:
        """执行冷门仓库搜索。

        参数:
            query: 搜索关键词或工具名，支持 'list all' 查看全部。
            limit: 最大返回数量，默认 5，上限 10。

        返回:
            JSON 字符串，包含匹配到的冷门工具列表；无匹配或无统计时返回提示信息。
        """
        query = kwargs.get("query", "")
        limit = kwargs.get("limit") or 5
        # 校验并限制返回数量
        if not isinstance(limit, int) or limit < 1:
            limit = 5
        limit = min(limit, 10)

        if self._stats is None:
            return json.dumps({"error": "冷门仓库未初始化，请稍后重试或重新发起搜索"}, ensure_ascii=False)

        cold_names = self._stats.cold_tool_names()
        if not cold_names:
            # 冷门仓库为空
            return json.dumps({
                "message": "冷门仓库为空。所有工具都在活跃索引中。",
                "cold_count": 0,
            }, ensure_ascii=False)

        entries = self._stats.search_cold(query, limit=limit)
        if not entries:
            # 未匹配到任何冷门工具
            return json.dumps({
                "message": f"未找到匹配 '{query}' 的冷门工具。",
                "cold_count": len(cold_names),
                "hint": "尝试用更通用的关键词，或用 cold_storage(query='list all') 查看全部。",
            }, ensure_ascii=False)

        # 若请求列出全部，则返回所有冷门条目
        if query.lower().strip() in ("list all", "all", "list", "全部", "所有"):
            entries = [self._stats.get_cold_entry(n) for n in cold_names]
            entries = [e for e in entries if e is not None]

        # 组装工具信息列表
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
