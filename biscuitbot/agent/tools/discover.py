"""Meta-tool: on-demand discovery of full tool schemas.

Implements Layer 2 of the progressive-discovery architecture.  The model
sees the ``INDEX.md`` table in the system prompt (name + capability +
usage_doc).  To actually call an on-demand tool it must first load the
tool's full JSON schema via ``discover_tools(name)``.

``discover_tools`` also returns the ``usage_md`` path for each matched
tool, encouraging the model to read the detailed doc first when it needs
parameter examples or caveats.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from biscuitbot.agent.tools.base import Tool, tool_parameters

if TYPE_CHECKING:
    from biscuitbot.agent.tools.registry import ToolRegistry


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
    """Meta-tool: let the model request full schemas on demand."""

    _capability = (
        "Request full tool definitions by name or keyword when a needed "
        "tool is not in your current tool set."
    )
    _always_include = True
    _usage_md = "docs/discover_tools.md"
    _scopes = {"core"}

    def __init__(self) -> None:
        self._registry: "ToolRegistry | None" = None

    def bind_registry(self, registry: "ToolRegistry") -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "discover_tools"

    @property
    def description(self) -> str:
        return (
            "Request full tool definitions by name or capability keyword. "
            "Use when you need a tool that is not in your current tool set. "
            "The returned schemas become callable in your next response."
        )

    async def execute(self, query: str, limit: int | None = None) -> str:
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
        # Return full schema + usage_md hint so the model knows where to
        # find detailed examples and caveats.
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
