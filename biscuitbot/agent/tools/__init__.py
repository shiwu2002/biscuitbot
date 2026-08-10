"""Agent 工具系统模块入口。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的对外导出入口。
在项目架构中起到的作用：将工具系统的核心抽象（Schema、Tool、ToolContext、
ToolLoader、ToolRegistry）及各类 JSON Schema 构造器集中暴露，供 agent
运行时及其他模块统一导入使用。
"""

from biscuitbot.agent.tools.base import Schema, Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.context import ToolContext  # 工具执行上下文
from biscuitbot.agent.tools.loader import ToolLoader  # 工具加载器
from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表
from biscuitbot.agent.tools.schema import (  # JSON Schema 构造器集合
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

__all__ = [
    "Schema",
    "ArraySchema",
    "BooleanSchema",
    "IntegerSchema",
    "NumberSchema",
    "ObjectSchema",
    "StringSchema",
    "Tool",
    "ToolContext",
    "ToolLoader",
    "ToolRegistry",
    "tool_parameters",
    "tool_parameters_schema",
]
