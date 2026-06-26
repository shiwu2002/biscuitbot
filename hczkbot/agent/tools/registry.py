"""Tool registry for dynamic tool management."""

import json
import re
from typing import Any

from hczkbot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None
        self._cached_compact_summary: str | None = None

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        self._cached_definitions = None
        self._cached_compact_summary = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._cached_definitions = None
        self._cached_compact_summary = None

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Normalize names for suggestions only; never for execution."""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended.  The result is cached until the next
        register/unregister call.
        """
        if self._cached_definitions is not None:
            return self._cached_definitions

        definitions = [tool.to_schema() for tool in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)

        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    # --- Dynamic tool selection helpers ---

    @staticmethod
    def _fuzzy_tokens(text: str) -> set[str]:
        """Lowercase alphanumeric tokens (length >= 2) used for fuzzy matching."""
        return {tok for tok in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(tok) >= 2}

    def fuzzy_search(self, query: str, *, limit: int = 5) -> list[Tool]:
        """Return tools whose name or capability matches *query*.

        Scoring is a simple token-overlap heuristic — no embeddings required.
        Used by the ``discover_tools`` meta-tool to surface full schemas on
        demand.  Exact name matches always rank first.
        """
        query_tokens = self._fuzzy_tokens(query)
        query_lower = (query or "").strip().lower()
        scored: list[tuple[float, str, Tool]] = []
        for name, tool in self._tools.items():
            score = 0.0
            if query_lower and name.lower() == query_lower:
                score += 100.0
            elif query_lower and query_lower in name.lower():
                score += 20.0
            tool_tokens = self._fuzzy_tokens(f"{name} {tool.capability}")
            if query_tokens:
                overlap = len(query_tokens & tool_tokens)
                score += overlap * 4.0
                # Bonus for matching all query tokens
                if overlap == len(query_tokens):
                    score += 5.0
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tool for _, _, tool in scored[:limit]]

    def get_compact_summary(self, selected_names: set[str] | None = None) -> str:
        """One-line ``name: capability`` summary for every registered tool.

        When *selected_names* is provided, tools already loaded with full
        schema are still listed but flagged with ``(loaded)``.  Cached until
        the next register/unregister call.
        """
        if self._cached_compact_summary is None:
            lines: list[str] = []
            for name in sorted(self._tools):
                tool = self._tools[name]
                lines.append(f"- {name}: {tool.capability}")
            self._cached_compact_summary = "\n".join(lines)
        base = self._cached_compact_summary
        if selected_names is None:
            return base
        # Annotate which tools are already loaded with full schema
        out: list[str] = []
        for line in base.splitlines():
            prefix = "- "
            if not line.startswith(prefix):
                out.append(line)
                continue
            name = line[len(prefix):].split(":", 1)[0].strip()
            if name in selected_names:
                out.append(f"{line} (loaded)")
            else:
                out.append(line)
        return "\n".join(out)

    def get_definitions_for_turn(
        self,
        query: str = "",
        *,
        mode: str = "all",
        max_tools: int = 10,
    ) -> list[dict[str, Any]]:
        """Return tool definitions for a single model turn.

        ``mode='all'`` (default) returns every registered tool's full schema —
        the legacy behavior, identical to :meth:`get_definitions`.

        ``mode='dynamic'`` returns only the tools selected by
        :class:`~hczkbot.agent.tools.retriever.ToolRetriever` for *query*,
        plus tools marked ``_always_include = True``.  All other tools are
        still discoverable via the ``discover_tools`` meta-tool and listed in
        the compact summary injected into the system prompt.

        The returned list preserves the cache-friendly builtins-then-MCP
        ordering of :meth:`get_definitions` so prompt-prefix caching is not
        disrupted by tool rotation.
        """
        if mode != "dynamic":
            return self.get_definitions()
        from hczkbot.agent.tools.retriever import ToolRetriever

        retriever = ToolRetriever(self, max_tools=max_tools)
        selected = set(retriever.select(query or ""))
        all_defs = self.get_definitions()
        return [d for d in all_defs if self._schema_name(d) in selected]

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """Resolve, cast, and validate one tool call."""
        tool = self._tools.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
            )

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                f"Error: Tool '{name}' parameters must be a JSON object, got "
                f"{type(params).__name__}. Use named parameters like "
                'tool_name(param1="value1", param2="value2") matching the tool schema.'
            )

        cast_params = tool.cast_params(params)
        errors = tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
            )
        return tool, cast_params, None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}

        if not stripped.startswith(("{", "[")):
            return value

        try:
            parsed = json.loads(stripped)
        except Exception:
            return value

        return parsed

    @classmethod
    def _coerce_params(cls, tool: Tool, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict) or set(params) != {"arguments"}:
            return params
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return params
        return cls._coerce_argument_value(params.get("arguments"))

    async def execute(self, name: str, params: Any) -> Any:
        """Execute a tool by name with given parameters."""
        hint = "\n\n[Analyze the error above and try a different approach.]"
        tool, params, error = self.prepare_call(name, params)
        if error:
            return error + hint

        try:
            assert tool is not None  # guarded by prepare_call()
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + hint
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + hint

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
