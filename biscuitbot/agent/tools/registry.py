"""Tool registry with progressive discovery.

All tools are registered here, but only those marked ``_always_include``
have their full JSON schema sent to the model by default.  Every other
tool is listed in the generated ``INDEX.md`` (name + capability +
usage-doc path) so the model can discover it, read its usage doc via
``read_file``, and then call ``discover_tools(name)`` to load the full
schema on demand.

The registry also supports runtime registration of custom tools created
by the agent itself (see :meth:`validate_tool_file` and
:meth:`register_custom_tool`).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from typing import TYPE_CHECKING, Any

from hczkbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from hczkbot.agent.tools.usage_stats import UsageStats


class ToolRegistry:
    """Registry for agent tools with progressive discovery."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None
        self._cached_index: str | None = None
        self._cached_skills_entries: list[dict[str, str]] | None = None
        # Custom tools registered at runtime by the agent.
        # name -> {"file_path": str, "docs_md_path": str}
        self._custom_tools: dict[str, dict[str, str]] = {}
        # Usage statistics for cold-storage rotation (set by AgentLoop).
        self._usage_stats: UsageStats | None = None

    def set_usage_stats(self, stats: UsageStats) -> None:
        """Attach a usage-stats tracker for cold-storage rotation."""
        self._usage_stats = stats
        self._cached_index = None
        self._cached_skills_entries = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """Register a tool and invalidate caches."""
        self._tools[tool.name] = tool
        self._cached_definitions = None
        self._cached_index = None
        self._cached_skills_entries = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._custom_tools.pop(name, None)
        self._cached_definitions = None
        self._cached_index = None
        self._cached_skills_entries = None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    # ------------------------------------------------------------------
    # Schema definitions (sent to the model)
    # ------------------------------------------------------------------

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get **all** tool definitions with stable builtins-then-MCP ordering."""
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

    def get_always_include_definitions(self) -> list[dict[str, Any]]:
        """Return full schemas only for ``_always_include`` tools.

        This is the default set sent to the model every turn.  All other
        tools require ``discover_tools(name)`` to load their schema.
        """
        all_defs = self.get_definitions()
        return [d for d in all_defs if self._tool_is_always_include(self._schema_name(d))]

    def _tool_is_always_include(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and getattr(tool, "_always_include", False))

    # ------------------------------------------------------------------
    # INDEX.md generation (Layer 1 — injected into system prompt)
    # ------------------------------------------------------------------

    def generate_index(self, skills_entries: list[dict[str, str]] | None = None) -> str:
        """Generate the ``INDEX.md`` content from registered tools + skills.

        Always-include tools are **omitted** from the index because their
        full schema is already sent via the ``tools`` parameter — listing
        them here would be redundant.

        Cold tools (rotated to cold storage by the nightly maintenance job)
        are also excluded; the model can search them via ``cold_storage``.

        Sections:
        1. **On-demand Discovery** — tools requiring ``discover_tools``.
        2. **Skills** — capabilities the model uses by reading ``SKILL.md``.

        Each row: ``| name | capability | usage_md |``.

        Cached until the next register/unregister/set_usage_stats call,
        or until skills_entries changes.
        """
        if self._cached_index is not None and self._cached_skills_entries == skills_entries:
            return self._cached_index

        on_demand_rows: list[str] = []
        for name in sorted(self._tools):
            tool = self._tools[name]
            # Skip always-include tools (schema already sent via tools parameter)
            if getattr(tool, "_always_include", False):
                continue
            # Skip cold tools (rotated to cold storage)
            if self._usage_stats is not None and self._usage_stats.is_cold(name):
                continue
            row = f"| {name} | {tool.capability} | {getattr(tool, '_usage_md', '') or '(missing)'} |"
            on_demand_rows.append(row)

        parts: list[str] = [
            "# Tools & Skills Index",
            "",
            "> 已加载工具的完整 schema 已在 tools 参数中发送，如需了解参数细节请 read_file(对应 docs/<name>.md)。",
            "> 按需工具使用流程：1. ``read_file(usage_md)`` 学习参数  2. ``discover_tools(\"name\")`` 加载 schema  3. 调用工具",
            "> 如果当前工具和技能不满足需求，请调用 ``cold_storage`` 搜索冷门仓库中是否有合适的工具。",
            "",
            "## On-demand Discovery (call ``discover_tools`` to load schema)",
            "",
            "| Name | Capability | Usage Doc |",
            "|------|-----------|-----------|",
        ]
        parts.extend(on_demand_rows or ["| _(none)_ | | |"])

        if skills_entries:
            parts.extend([
                "",
                "## Skills (read SKILL.md to use)",
                "",
                "| Name | Capability | Usage Doc |",
                "|------|-----------|-----------|",
            ])
            for s in skills_entries:
                parts.append(f"| {s['name']} | {s['capability']} | {s['usage_md']} |")

        index = "\n".join(parts)
        self._cached_index = index
        self._cached_skills_entries = skills_entries
        return index

    # ------------------------------------------------------------------
    # Fuzzy search (used by discover_tools meta-tool)
    # ------------------------------------------------------------------

    @staticmethod
    def _fuzzy_tokens(text: str) -> set[str]:
        return {tok for tok in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(tok) >= 2}

    def fuzzy_search(self, query: str, *, limit: int = 5) -> list[Tool]:
        """Return tools whose name or capability matches *query*."""
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
                if overlap == len(query_tokens):
                    score += 5.0
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tool for _, _, tool in scored[:limit]]

    # ------------------------------------------------------------------
    # Custom tool registration (agent self-extension)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_tool_file(file_path: str, docs_md_path: str) -> tuple[Tool | None, str | None]:
        """Validate a Python tool file and its usage doc.

        Returns ``(tool_instance, None)`` on success or
        ``(None, error_message)`` on failure.  The error message explains
        **why** the tool cannot be installed so the agent can fix it.
        """
        # 1. File existence
        if not file_path or not os.path.isfile(file_path):
            return None, (
                f"工具文件不存在：{file_path}。请先用 write_file 创建工具 Python 文件。"
                f"工具必须继承 hczkbot.agent.tools.base.Tool 并实现 name/description/parameters/execute。"
            )
        if not docs_md_path or not os.path.isfile(docs_md_path):
            return None, (
                f"使用说明 md 不存在：{docs_md_path}。"
                f"每个工具必须有对应的 docs/<name>.md 使用说明文件，"
                f"说明参数、调用示例和注意事项。"
            )

        # 2. Dynamic import
        tool_name_from_file = os.path.splitext(os.path.basename(file_path))[0]
        spec = importlib.util.spec_from_file_location(f"_custom_tool_{tool_name_from_file}", file_path)
        if spec is None or spec.loader is None:
            return None, f"无法加载工具模块：{file_path}。请检查文件路径和权限。"
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            return None, (
                f"工具文件导入失败：{e}。"
                f"请检查 Python 语法、导入语句（需要 from hczkbot.agent.tools.base import Tool）。"
            )

        # 3. Find exactly one Tool subclass defined in this module
        tool_classes = [
            obj for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, Tool)
            and obj is not Tool
            and not obj.__name__.startswith("_")
            and getattr(obj, "__module__", "") == module.__name__
        ]
        if not tool_classes:
            return None, (
                "未找到 Tool 子类。工具类必须继承 hczkbot.agent.tools.base.Tool，"
                "且类名不以 _ 开头。示例：class MyTool(Tool): ..."
            )
        if len(tool_classes) > 1:
            names = [c.__name__ for c in tool_classes]
            return None, (
                f"找到多个 Tool 子类：{names}。每个工具文件只能定义一个 Tool 子类。"
            )

        tool_cls = tool_classes[0]

        # 4. Abstract methods must be implemented
        if tool_cls.__abstractmethods__:
            return None, (
                f"未实现抽象方法：{sorted(tool_cls.__abstractmethods__)}。"
                f"必须实现 name、description、parameters、execute。"
            )

        # 5. Instantiate and validate metadata
        try:
            instance = tool_cls()
        except Exception as e:
            return None, f"工具实例化失败：{e}。请检查 __init__ 是否需要参数。"

        if not instance.name or not isinstance(instance.name, str):
            return None, "工具 name 属性返回空值。必须返回非空字符串。"
        if not instance.description or not isinstance(instance.description, str):
            return None, "工具 description 属性返回空值。必须返回非空字符串。"
        # _capability must be declared explicitly on the class — the
        # ``capability`` property falls back to description, so we check the
        # raw class attribute to enforce the progressive-discovery contract.
        if not getattr(tool_cls, "_capability", ""):
            return None, (
                "_capability 未声明。请在工具类中显式设置 _capability 属性，"
                "用一句话描述工具能力边界。示例：_capability = 'Search files by regex pattern.'"
            )
        if not getattr(tool_cls, "_usage_md", ""):
            return None, (
                "_usage_md 未声明。请设置 _usage_md 指向使用说明 md 路径。"
                "示例：_usage_md = 'docs/my_tool.md'"
            )

        return instance, None

    def register_custom_tool(self, file_path: str, docs_md_path: str) -> tuple[bool, str]:
        """Validate and register a custom tool created by the agent.

        Returns ``(True, success_message)`` or ``(False, error_message)``.
        On success the tool is immediately callable via ``discover_tools``.
        """
        tool, error = self.validate_tool_file(file_path, docs_md_path)
        if error:
            return False, error

        if tool.name in self._tools:
            existing = self._tools[tool.name]
            is_custom = getattr(existing, "_custom", False)
            kind = "自定义工具" if is_custom else "内置工具"
            return False, (
                f"工具名 '{tool.name}' 已被{kind}占用。"
                f"请更换工具名，或先用 unregister_tool('{tool.name}') 卸载旧工具。"
            )

        # Mark as custom and register
        object.__setattr__(tool, "_custom", True)
        self.register(tool)
        self._custom_tools[tool.name] = {
            "file_path": os.path.abspath(file_path),
            "docs_md_path": os.path.abspath(docs_md_path),
        }
        return True, (
            f"工具 '{tool.name}' 注册成功。"
            f"模型现在可以通过 discover_tools('{tool.name}') 加载其 schema 并调用。"
        )

    def unregister_custom_tool(self, name: str) -> tuple[bool, str]:
        """Remove a custom tool registered at runtime."""
        if name not in self._custom_tools:
            if name in self._tools:
                return False, (
                    f"工具 '{name}' 是内置工具，无法卸载。"
                    f"只能卸载由 register_tool 注册的自定义工具。"
                )
            return False, f"工具 '{name}' 未注册。"
        self.unregister(name)
        return True, f"工具 '{name}' 已卸载。"

    def custom_tools_manifest(self) -> list[dict[str, str]]:
        """Return the list of custom tools for persistence."""
        return [
            {"name": name, **info}
            for name, info in self._custom_tools.items()
        ]

    def load_custom_tools_from_manifest(self, manifest: list[dict[str, str]]) -> list[str]:
        """Re-register custom tools from a persisted manifest.

        Returns a list of error messages for tools that failed to load
        (e.g. file deleted).  Successfully loaded tools are registered
        silently.
        """
        errors: list[str] = []
        for entry in manifest:
            name = entry.get("name", "")
            file_path = entry.get("file_path", "")
            docs_md_path = entry.get("docs_md_path", "")
            ok, msg = self.register_custom_tool(file_path, docs_md_path)
            if not ok:
                errors.append(f"{name}: {msg}")
        return errors

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

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
                f"Error: Tool '{name}' not found.{hint} "
                f"Call discover_tools('{name}') to load its schema first, "
                f"or check the Tools & Skills Index in the system prompt."
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

    @staticmethod
    def _lookup_key(name: str) -> str:
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
            # Record usage for cold-storage rotation (auto-recovers cold tools).
            if self._usage_stats is not None:
                was_cold = self._usage_stats.is_cold(name)
                self._usage_stats.record_call(name)
                if was_cold:
                    self._cached_index = None
                    self._cached_skills_entries = None
            if isinstance(result, str) and result.startswith("Error"):
                return result + hint
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + hint

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
