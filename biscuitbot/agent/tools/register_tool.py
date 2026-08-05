"""Meta-tools: agent self-extension via runtime tool registration.

These meta-tools let the agent create, install, and uninstall custom
tools at runtime.  A custom tool is a Python file that inherits from
``Tool`` plus a ``docs/<name>.md`` usage doc.  The registry validates
both files before installation — if validation fails the error message
explains **why** so the agent can fix the problem and retry.

Custom tools are persisted to ``workspace/.agent_tools/manifest.json``
so they survive restarts.
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
        "file_path": {
            "type": "string",
            "description": (
                "Path to the Python file defining the tool. The file must "
                "contain exactly one Tool subclass with all abstract methods "
                "implemented, _capability set, and _usage_md pointing to the "
                "docs md path."
            ),
            "minLength": 1,
            "maxLength": 500,
        },
        "docs_md_path": {
            "type": "string",
            "description": (
                "Path to the usage-doc markdown file. Must describe "
                "parameters, call examples, and caveats."
            ),
            "minLength": 1,
            "maxLength": 500,
        },
    },
    "required": ["file_path", "docs_md_path"],
})
class RegisterToolTool(Tool):
    """Let the agent install a custom tool at runtime."""

    _capability = (
        "Register a custom tool (Python file + usage md) so it becomes "
        "discoverable and callable via discover_tools."
    )
    _always_include = True
    _usage_md = "docs/register_tool.md"
    _scopes = {"core"}

    def __init__(self) -> None:
        self._registry: "ToolRegistry | None" = None

    def bind_registry(self, registry: "ToolRegistry") -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "register_tool"

    @property
    def description(self) -> str:
        return (
            "Register a custom tool from a Python file and its usage-doc md. "
            "The tool must inherit from Tool, implement name/description/"
            "parameters/execute, set _capability and _usage_md. On success "
            "the tool is immediately discoverable via discover_tools."
        )

    async def execute(self, file_path: str, docs_md_path: str) -> str:
        if self._registry is None:
            return json.dumps(
                {"error": "tool registry not bound to register_tool"},
                ensure_ascii=False,
            )
        ok, msg = self._registry.register_custom_tool(file_path, docs_md_path)
        return json.dumps(
            {"ok": ok, "message": msg},
            ensure_ascii=False,
        )


@tool_parameters({
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Name of the custom tool to uninstall.",
            "minLength": 1,
            "maxLength": 100,
        },
    },
    "required": ["name"],
})
class UnregisterToolTool(Tool):
    """Let the agent uninstall a previously-registered custom tool."""

    _capability = "Uninstall a custom tool that was registered via register_tool."
    _always_include = True
    _usage_md = "docs/unregister_tool.md"
    _scopes = {"core"}

    def __init__(self) -> None:
        self._registry: "ToolRegistry | None" = None

    def bind_registry(self, registry: "ToolRegistry") -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "unregister_tool"

    @property
    def description(self) -> str:
        return (
            "Uninstall a custom tool registered via register_tool. "
            "Built-in tools cannot be uninstalled."
        )

    async def execute(self, name: str) -> str:
        if self._registry is None:
            return json.dumps(
                {"error": "tool registry not bound to unregister_tool"},
                ensure_ascii=False,
            )
        ok, msg = self._registry.unregister_custom_tool(name)
        return json.dumps(
            {"ok": ok, "message": msg},
            ensure_ascii=False,
        )
