"""工具注册表，支持渐进式发现。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统渐进式发现架构的核心
组件。所有工具都注册到此处，但默认只有标记为 ``_always_include`` 的工具
会把完整 JSON schema 发送给模型。其他工具仅列在生成的 ``INDEX.md``
（名称 + 能力 + 使用文档路径）中，模型可以通过阅读使用文档、再调用
``discover_tools(name)`` 按需加载完整 schema。

注册表还支持运行时注册由 agent 自己创建的自定义工具（见
:meth:`validate_tool_file` 与 :meth:`register_custom_tool`）。
"""

from __future__ import annotations

import importlib.util  # 动态导入工具，用于加载自定义工具文件
import json  # JSON 解析，用于参数强转
import os  # 文件路径处理
import re  # 正则表达式，用于模糊搜索分词
from typing import TYPE_CHECKING, Any  # 类型检查与任意类型

from biscuitbot.agent.tools.base import Tool  # 工具基类

if TYPE_CHECKING:  # 仅类型检查时导入，避免循环依赖
    from biscuitbot.agent.tools.usage_stats import UsageStats  # 使用统计，用于冷存储轮换


class ToolRegistry:
    """Agent 工具注册表，支持渐进式发现。

    职责：集中管理所有已注册工具，提供 schema 生成、索引生成、模糊搜索、
    自定义工具校验注册、调用准备与执行等功能。缓存 schema 定义与索引，
    在注册/注销时失效。
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}  # 工具名 -> 工具实例
        self._cached_definitions: list[dict[str, Any]] | None = None  # 缓存的完整 schema 定义列表
        self._cached_index: str | None = None  # 缓存的 INDEX.md 内容
        self._cached_skills_entries: list[dict[str, str]] | None = None  # 缓存生成索引时使用的技能条目
        # 运行时由 agent 注册的自定义工具。
        # 名称 -> {"file_path": str, "docs_md_path": str}
        self._custom_tools: dict[str, dict[str, str]] = {}
        # 使用统计，用于冷存储轮换（由 AgentLoop 设置）。
        self._usage_stats: UsageStats | None = None

    def set_usage_stats(self, stats: UsageStats) -> None:
        """附加使用统计追踪器，用于冷存储轮换。

        参数:
            stats: 使用统计实例。
        """
        self._usage_stats = stats
        # 索引内容可能因冷热状态变化而改变，需失效缓存
        self._cached_index = None
        self._cached_skills_entries = None

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """注册工具并使缓存失效。

        参数:
            tool: 要注册的工具实例。
        """
        self._tools[tool.name] = tool
        self._cached_definitions = None
        self._cached_index = None
        self._cached_skills_entries = None

    def unregister(self, name: str) -> None:
        """按名称注销工具。

        参数:
            name: 工具名称。
        """
        self._tools.pop(name, None)
        self._custom_tools.pop(name, None)
        self._cached_definitions = None
        self._cached_index = None
        self._cached_skills_entries = None

    def get(self, name: str) -> Tool | None:
        """按名称获取工具实例。"""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """判断工具是否已注册。"""
        return name in self._tools

    # ------------------------------------------------------------------
    # Schema 定义（发送给模型）
    # ------------------------------------------------------------------

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """从 schema 字典中提取工具名称。

        兼容两种格式：OpenAI 风格的 ``{"function": {"name": ...}}`` 与
        扁平的 ``{"name": ...}``。
        """
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """获取所有工具定义，按"内置在前、MCP 在后"的稳定顺序排列。"""
        if self._cached_definitions is not None:
            return self._cached_definitions
        definitions = [tool.to_schema() for tool in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        # 按 mcp_ 前缀分组，确保内置工具与 MCP 工具各自稳定排序
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
        """仅返回 ``_always_include`` 工具的完整 schema。

        这是每轮默认发送给模型的工具集合。其他工具需要通过
        ``discover_tools(name)`` 加载其 schema。
        """
        all_defs = self.get_definitions()
        return [d for d in all_defs if self._tool_is_always_include(self._schema_name(d))]

    def _tool_is_always_include(self, name: str) -> bool:
        """判断指定工具是否标记为始终包含。"""
        tool = self._tools.get(name)
        return bool(tool and getattr(tool, "_always_include", False))

    # ------------------------------------------------------------------
    # INDEX.md 生成（第 1 层 —— 注入系统提示）
    # ------------------------------------------------------------------

    def generate_index(self, skills_entries: list[dict[str, str]] | None = None) -> str:
        """根据已注册工具与技能生成 ``INDEX.md`` 内容。

        always-include 工具会从索引中**省略**，因为其完整 schema 已通过
        ``tools`` 参数发送，在此列出会冗余。

        冷工具（由夜间维护任务轮换到冷存储）也会排除；模型可通过
        ``cold_storage`` 搜索它们。

        分区：
        1. **On-demand Discovery** —— 需要 ``discover_tools`` 的工具。
        2. **Skills** —— 模型通过阅读 ``SKILL.md`` 使用的技能。

        每行格式：``| name | capability | usage_md |``。

        缓存直到下一次 register/unregister/set_usage_stats 调用，或
        skills_entries 变化。
        """
        if self._cached_index is not None and self._cached_skills_entries == skills_entries:
            return self._cached_index

        on_demand_rows: list[str] = []
        for name in sorted(self._tools):
            tool = self._tools[name]
            # 跳过 always-include 工具（schema 已通过 tools 参数发送）
            if getattr(tool, "_always_include", False):
                continue
            # 跳过冷工具（已轮换到冷存储）
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
    # 模糊搜索（discover_tools 元工具使用）
    # ------------------------------------------------------------------

    @staticmethod
    def _fuzzy_tokens(text: str) -> set[str]:
        """将文本拆分为小写 token 集合，用于模糊匹配。"""
        return {tok for tok in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(tok) >= 2}

    def fuzzy_search(self, query: str, *, limit: int = 5) -> list[Tool]:
        """返回名称或能力匹配 *query* 的工具。

        参数:
            query: 查询关键词或工具名。
            limit: 最大返回数量。

        返回:
            按相关性得分降序排列的工具列表。
        """
        query_tokens = self._fuzzy_tokens(query)
        query_lower = (query or "").strip().lower()
        scored: list[tuple[float, str, Tool]] = []
        for name, tool in self._tools.items():
            score = 0.0
            # 精确名称匹配得分最高
            if query_lower and name.lower() == query_lower:
                score += 100.0
            # 部分名称匹配次之
            elif query_lower and query_lower in name.lower():
                score += 20.0
            tool_tokens = self._fuzzy_tokens(f"{name} {tool.capability}")
            if query_tokens:
                overlap = len(query_tokens & tool_tokens)
                score += overlap * 4.0
                # 全部 token 命中给予额外加分
                if overlap == len(query_tokens):
                    score += 5.0
            if score > 0:
                scored.append((score, name, tool))
        # 按得分降序、名称升序排列
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [tool for _, _, tool in scored[:limit]]

    # ------------------------------------------------------------------
    # 自定义工具注册（agent 自我扩展）
    # ------------------------------------------------------------------

    @staticmethod
    def validate_tool_file(file_path: str, docs_md_path: str) -> tuple[Tool | None, str | None]:
        """校验 Python 工具文件及其使用文档。

        成功返回 ``(tool_instance, None)``，失败返回
        ``(None, error_message)``。错误信息会说明**为何**无法安装，便于
        agent 修复。
        """
        # 1. 文件存在性检查
        if not file_path or not os.path.isfile(file_path):
            return None, (
                f"工具文件不存在：{file_path}。请先用 write_file 创建工具 Python 文件。"
                f"工具必须继承 biscuitbot.agent.tools.base.Tool 并实现 name/description/parameters/execute。"
            )
        if not docs_md_path or not os.path.isfile(docs_md_path):
            return None, (
                f"使用说明 md 不存在：{docs_md_path}。"
                f"每个工具必须有对应的 docs/<name>.md 使用说明文件，"
                f"说明参数、调用示例和注意事项。"
            )

        # 2. 动态导入
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
                f"请检查 Python 语法、导入语句（需要 from biscuitbot.agent.tools.base import Tool）。"
            )

        # 3. 查找该模块中定义的唯一一个 Tool 子类
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
                "未找到 Tool 子类。工具类必须继承 biscuitbot.agent.tools.base.Tool，"
                "且类名不以 _ 开头。示例：class MyTool(Tool): ..."
            )
        if len(tool_classes) > 1:
            names = [c.__name__ for c in tool_classes]
            return None, (
                f"找到多个 Tool 子类：{names}。每个工具文件只能定义一个 Tool 子类。"
            )

        tool_cls = tool_classes[0]

        # 4. 抽象方法必须已实现
        if tool_cls.__abstractmethods__:
            return None, (
                f"未实现抽象方法：{sorted(tool_cls.__abstractmethods__)}。"
                f"必须实现 name、description、parameters、execute。"
            )

        # 5. 实例化并校验元数据
        try:
            instance = tool_cls()
        except Exception as e:
            return None, f"工具实例化失败：{e}。请检查 __init__ 是否需要参数。"

        if not instance.name or not isinstance(instance.name, str):
            return None, "工具 name 属性返回空值。必须返回非空字符串。"
        if not instance.description or not isinstance(instance.description, str):
            return None, "工具 description 属性返回空值。必须返回非空字符串。"
        # _capability 必须在类上显式声明 —— ``capability`` 属性会回退到
        # description，因此检查原始类属性以强制执行渐进式发现契约。
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
        """校验并注册由 agent 创建的自定义工具。

        返回 ``(True, success_message)`` 或 ``(False, error_message)``。
        成功后工具立即可通过 ``discover_tools`` 调用。
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

        # 标记为自定义并注册
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
        """移除运行时注册的自定义工具。"""
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
        """返回自定义工具列表，用于持久化。"""
        return [
            {"name": name, **info}
            for name, info in self._custom_tools.items()
        ]

    def load_custom_tools_from_manifest(self, manifest: list[dict[str, str]]) -> list[str]:
        """从持久化的清单重新注册自定义工具。

        返回加载失败工具的错误信息列表（例如文件已删除）。成功加载的
        工具会被静默注册。
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
    # 执行
    # ------------------------------------------------------------------

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """解析、强转并校验一次工具调用。

        返回 ``(tool, params, None)`` 表示可执行，或
        ``(None|tool, params, error_message)`` 表示出错。
        """
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
        """生成名称的查找键（小写并去除非字母数字字符），用于名称建议。"""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        """根据查找键为错误名称建议一个已注册的近似名称。"""
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        # 唯一匹配时才给出建议，避免歧义
        if len(matches) == 1:
            return matches[0]
        return None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        """将单个参数值强转为合适类型。

        空值返回空字典；字符串若以 ``{`` 或 ``[`` 开头则尝试 JSON 解析。
        """
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
        """强转工具参数，并解包可能的 arguments 外层封装。"""
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        """解包 ``{"arguments": ...}`` 外层封装（部分模型会多包一层）。"""
        if not isinstance(params, dict) or set(params) != {"arguments"}:
            return params
        properties = (tool.parameters or {}).get("properties", {})
        # 若工具确实有 arguments 字段，则不解包
        if isinstance(properties, dict) and "arguments" in properties:
            return params
        return cls._coerce_argument_value(params.get("arguments"))

    async def execute(self, name: str, params: Any) -> Any:
        """按名称与参数执行工具。"""
        hint = "\n\n[Analyze the error above and try a different approach.]"
        tool, params, error = self.prepare_call(name, params)
        if error:
            return error + hint
        try:
            assert tool is not None  # 由 prepare_call() 保证非空
            result = await tool.execute(**params)
            # 记录使用情况用于冷存储轮换（自动恢复冷工具）。
            if self._usage_stats is not None:
                was_cold = self._usage_stats.is_cold(name)
                self._usage_stats.record_call(name)
                # 冷工具被调用后需失效索引缓存，使其重新出现在 INDEX.md
                if was_cold:
                    self._cached_index = None
                    self._cached_skills_entries = None
            # 错误结果附加分析提示，引导模型换思路
            if isinstance(result, str) and result.startswith("Error"):
                return result + hint
            return result
        except Exception as e:
            # 保留异常类名：空消息异常（如无参 TimeoutError）否则会输出成空串
            return f"Error executing {name}: {type(e).__name__}: {e}" + hint

    @property
    def tool_names(self) -> list[str]:
        """已注册工具名称列表。"""
        return list(self._tools.keys())

    def __len__(self) -> int:
        """已注册工具数量。"""
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """支持 ``name in registry`` 语法判断工具是否已注册。"""
        return name in self._tools
