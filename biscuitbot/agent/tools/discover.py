"""元工具：按需发现工具的完整 schema。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统渐进式发现架构的第二层组件。
在项目架构中起到的作用：模型在系统提示中只能看到 INDEX.md 表格（名称+能力+
文档路径），要实际调用某个按需工具，需先通过 ``discover_tools(name)`` 加载
该工具的完整 JSON schema。同时返回 ``usage_md`` 路径，鼓励模型先阅读详细
文档再调用。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器

if TYPE_CHECKING:
    from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表，仅用于类型提示


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Tool name or capability keyword. Examples: 'screenshot', "
                "'search files', 'generate image'. Matched against tool "
                "names and capability summaries."
            ),
            "minLength": 1,
            "maxLength": 200,
        },
        "limit": {
            "type": ["integer", "null"],
            "description": "Maximum number of tool schemas to return (default 5, max 10).",
            "minimum": 1,
            "maximum": 10,
            "default": 5,
        },
    },
    "required": ["query"],
})
class DiscoverToolsTool(Tool):
    """元工具：让模型按需请求工具的完整 schema。

    职责：根据关键词或工具名模糊搜索工具注册表，返回匹配工具的完整
    JSON schema 与使用文档路径，使模型能在下一次响应中调用这些工具。

    用法：由 agent 调用，传入查询关键词与可选的返回数量上限。
    """

    _capability = (
        "Request full tool definitions by name or keyword when a needed "
        "tool is not in your current tool set."
    )
    _always_include = True  # 该工具的完整 schema 始终发送给模型
    _usage_md = "docs/discover_tools.md"  # 工具使用说明文档路径
    _scopes = {"core"}  # 工具可用作用域

    def __init__(self) -> None:
        """初始化发现工具，注册表初始为空。"""
        self._registry: "ToolRegistry | None" = None

    def bind_registry(self, registry: "ToolRegistry") -> None:
        """绑定工具注册表实例。

        参数:
            registry: 工具注册表对象。
        """
        self._registry = registry

    @property
    def name(self) -> str:
        """工具名称。"""
        return "discover_tools"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Request full tool definitions by name or capability keyword. "
            "Use when you need a tool that is not in your current tool set. "
            "The returned schemas become callable in your next response."
        )

    async def execute(self, query: str, limit: int | None = None) -> str:
        """执行工具发现查询。

        参数:
            query: 查询关键词或工具名。
            limit: 最大返回数量，默认 5，上限 10。

        返回:
            JSON 字符串，包含匹配工具的 schema 列表与使用提示；无匹配时返回错误与建议。
        """
        if self._registry is None:
            return json.dumps(
                {"error": "tool registry not bound to discover_tools"},
                ensure_ascii=False,
            )
        effective_limit = 5 if limit is None else max(1, min(10, int(limit)))
        matches = self._registry.fuzzy_search(query, limit=effective_limit)
        if not matches:
            return json.dumps(
                {
                    "error": "no matching tools found",
                    "query": query,
                    "hint": (
                        "Try a broader keyword or check the Tools & Skills "
                        "Index in the system prompt for the exact name."
                    ),
                },
                ensure_ascii=False,
            )
        # 返回完整 schema 与 usage_md 提示，便于模型查阅详细示例与注意事项
        tools_payload = []
        for t in matches:
            schema = t.to_schema()
            usage_md = getattr(t, "_usage_md", "") or ""
            if usage_md:
                schema["_usage_md"] = usage_md
            tools_payload.append(schema)
        payload = {
            "tools": tools_payload,
            "note": (
                "These tools are now available. Call them in your next "
                "response using the exact name and parameters shown. "
                "For detailed usage examples, read the _usage_md file "
                "with read_file."
            ),
        }
        return json.dumps(payload, ensure_ascii=False)
