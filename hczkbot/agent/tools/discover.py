"""Meta-tool: on-demand discovery of full tool schemas.

This module implements Layer 3 of the dynamic-tool-selection design.  When
``tool_selection_mode == "dynamic"`` only a subset of tools is sent with
full JSON schema.  The compact summary in the system prompt lists every
tool's name + capability, but the model cannot call a tool whose schema it
has not yet seen.

``DiscoverToolsTool`` bridges that gap: the model calls
``discover_tools("screenshot")`` and the tool returns the full schema for
matching tools.  The model can then call the discovered tool in its next
response.

The tool is registered only in dynamic mode and is always included in the
selection so the model can reach for it on any turn.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hczkbot.agent.tools.base import Tool, tool_parameters

if TYPE_CHECKING:
    from hczkbot.agent.tools.registry import ToolRegistry


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Tool name or capability keyword.  Examples: 'screenshot', "
                "'search files', 'generate image'.  Matched against tool "
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

    _capability: str = (
        "Request full tool definitions by name or keyword when a needed "
        "tool is not in your current tool set."
    )
    _always_include: bool = True
    _scopes: set[str] = {"core"}

    def __init__(self) -> None:
        self._registry: "ToolRegistry | None" = None

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """Only registered in dynamic tool-selection mode."""
        return getattr(ctx.config, "tool_selection_mode", "all") == "dynamic"

    def bind_registry(self, registry: "ToolRegistry") -> None:
        """Inject the tool registry so the meta-tool can search it.

        Must be called once after registration.  We avoid passing the
        registry through ``ToolContext`` to keep the context dataclass
        free of tool-internal bookkeeping.
        """
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
                        "Try a broader keyword or check the Available Tools "
                        "list in the system prompt for the exact name."
                    ),
                },
                ensure_ascii=False,
            )
        # Strip the wrapper so the model sees a flat, schema-friendly list.
        payload = {
            "tools": [t.to_schema() for t in matches],
            "note": (
                "These tools are now available. Call them in your next "
                "response using the exact name and parameters shown."
            ),
        }
        return json.dumps(payload, ensure_ascii=False)
