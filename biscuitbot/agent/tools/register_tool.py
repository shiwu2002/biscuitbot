"""元工具：通过运行时工具注册实现 agent 自我扩展。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的"元工具"组件。它让
agent 能够在运行时创建、安装和卸载自定义工具。一个自定义工具由一个继承
``Tool`` 的 Python 文件加一个 ``docs/<name>.md`` 使用文档组成。注册表在
安装前会校验这两个文件——若校验失败，错误信息会说明**原因**，便于 agent
修复后重试。

自定义工具会持久化到 ``workspace/.agent_tools/manifest.json``，重启后仍可
恢复。
"""

from __future__ import annotations

import json  # JSON 序列化，用于返回结构化结果
from typing import TYPE_CHECKING  # 仅类型检查时导入

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器

if TYPE_CHECKING:  # 仅类型检查时导入，避免循环依赖
    from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表


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
    """让 agent 在运行时安装自定义工具。

    职责：校验并注册由 agent 创建的自定义工具（Python 文件 + 使用文档 md）。
    成功后该工具立即可通过 ``discover_tools`` 发现并调用。
    """

    _capability = (
        "Register a custom tool (Python file + usage md) so it becomes "
        "discoverable and callable via discover_tools."
    )
    _always_include = True  # 该工具的完整 schema 始终发送给模型
    _usage_md = "docs/register_tool.md"  # 工具使用说明文档路径
    _scopes = {"core"}  # 工具可用作用域

    def __init__(self) -> None:
        self._registry: "ToolRegistry | None" = None  # 工具注册表，初始为空

    def bind_registry(self, registry: "ToolRegistry") -> None:
        """绑定工具注册表实例。"""
        self._registry = registry

    @property
    def name(self) -> str:
        """工具名称。"""
        return "register_tool"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Register a custom tool from a Python file and its usage-doc md. "
            "The tool must inherit from Tool, implement name/description/"
            "parameters/execute, set _capability and _usage_md. On success "
            "the tool is immediately discoverable via discover_tools."
        )

    async def execute(self, file_path: str, docs_md_path: str) -> str:
        """执行工具注册。

        参数:
            file_path: 工具 Python 文件路径。
            docs_md_path: 使用文档 md 路径。

        返回:
            JSON 字符串，包含 ok 标志与消息；若注册表未绑定返回错误。
        """
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
    """让 agent 卸载之前注册的自定义工具。

    职责：按名称卸载由 ``register_tool`` 注册的自定义工具。内置工具无法
    卸载。
    """

    _capability = "Uninstall a custom tool that was registered via register_tool."
    _always_include = True  # 该工具的完整 schema 始终发送给模型
    _usage_md = "docs/unregister_tool.md"  # 工具使用说明文档路径
    _scopes = {"core"}  # 工具可用作用域

    def __init__(self) -> None:
        self._registry: "ToolRegistry | None" = None  # 工具注册表，初始为空

    def bind_registry(self, registry: "ToolRegistry") -> None:
        """绑定工具注册表实例。"""
        self._registry = registry

    @property
    def name(self) -> str:
        """工具名称。"""
        return "unregister_tool"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Uninstall a custom tool registered via register_tool. "
            "Built-in tools cannot be uninstalled."
        )

    async def execute(self, name: str) -> str:
        """执行工具卸载。

        参数:
            name: 要卸载的自定义工具名称。

        返回:
            JSON 字符串，包含 ok 标志与消息；若注册表未绑定返回错误。
        """
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
