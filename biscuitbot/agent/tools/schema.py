"""JSON Schema 片段类型：所有类型都继承自 :class:`~biscuitbot.agent.tools.base.Schema`，
用于描述工具参数的文档与约束。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的 schema 定义层组件。
它为工具参数提供类型化的 schema 构造器（字符串、整数、数字、布尔、数组、
对象），最终通过 ``to_json_schema()`` 输出符合 JSON Schema 规范的字典，
供 :class:`~biscuitbot.agent.tools.base.Tool` 发送给模型并校验入参。

主要 API：
- ``to_json_schema()``：返回与
  :meth:`~biscuitbot.agent.tools.base.Schema.validate_json_schema_value` /
  :class:`~biscuitbot.agent.tools.base.Tool` 兼容的字典。
- ``validate_value(value, path)``：根据本 schema 校验单个值，返回错误信息
  列表（空列表表示有效）。

共享的校验与片段规范化逻辑位于 :class:`~biscuitbot.agent.tools.base.Schema`
的类方法上。

注意：Python 不允许子类化 ``bool``，因此布尔值使用 :class:`BooleanSchema`。
"""

from __future__ import annotations

from collections.abc import Mapping  # 映射类型，用于对象 schema 的属性字典
from typing import Any  # 任意类型

from biscuitbot.agent.tools.base import Schema  # Schema 基类，提供共享校验与片段规范化


class StringSchema(Schema):
    """字符串参数：``description`` 描述字段用途；可选长度边界与枚举。

    职责：构造 JSON Schema 中的 ``string`` 类型片段，支持最小/最大长度、
    枚举值与可空标记。
    """

    def __init__(
        self,
        description: str = "",
        *,
        min_length: int | None = None,
        max_length: int | None = None,
        enum: tuple[Any, ...] | list[Any] | None = None,
        nullable: bool = False,
    ) -> None:
        self._description = description  # 字段描述
        self._min_length = min_length  # 最小长度
        self._max_length = max_length  # 最大长度
        self._enum = tuple(enum) if enum is not None else None  # 枚举值元组
        self._nullable = nullable  # 是否允许 null

    def to_json_schema(self) -> dict[str, Any]:
        """转换为 JSON Schema 字典。"""
        t: Any = "string"
        # 可空时类型变为 ["string", "null"]
        if self._nullable:
            t = ["string", "null"]
        d: dict[str, Any] = {"type": t}
        if self._description:
            d["description"] = self._description
        if self._min_length is not None:
            d["minLength"] = self._min_length
        if self._max_length is not None:
            d["maxLength"] = self._max_length
        if self._enum is not None:
            d["enum"] = list(self._enum)
        return d


class IntegerSchema(Schema):
    """整数参数：可选占位整数（遗留构造签名）、描述与边界。

    职责：构造 JSON Schema 中的 ``integer`` 类型片段，支持最小/最大值、
    枚举值与可空标记。首个位置参数 ``value`` 仅为兼容旧构造签名而保留。
    """

    def __init__(
        self,
        value: int = 0,
        *,
        description: str = "",
        minimum: int | None = None,
        maximum: int | None = None,
        enum: tuple[int, ...] | list[int] | None = None,
        nullable: bool = False,
    ) -> None:
        self._value = value  # 遗留占位值
        self._description = description  # 字段描述
        self._minimum = minimum  # 最小值
        self._maximum = maximum  # 最大值
        self._enum = tuple(enum) if enum is not None else None  # 枚举值元组
        self._nullable = nullable  # 是否允许 null

    def to_json_schema(self) -> dict[str, Any]:
        """转换为 JSON Schema 字典。"""
        t: Any = "integer"
        if self._nullable:
            t = ["integer", "null"]
        d: dict[str, Any] = {"type": t}
        if self._description:
            d["description"] = self._description
        if self._minimum is not None:
            d["minimum"] = self._minimum
        if self._maximum is not None:
            d["maximum"] = self._maximum
        if self._enum is not None:
            d["enum"] = list(self._enum)
        return d


class NumberSchema(Schema):
    """数字参数（JSON number）：描述与可选边界。

    职责：构造 JSON Schema 中的 ``number`` 类型片段（浮点数），支持
    最小/最大值、枚举值与可空标记。
    """

    def __init__(
        self,
        value: float = 0.0,
        *,
        description: str = "",
        minimum: float | None = None,
        maximum: float | None = None,
        enum: tuple[float, ...] | list[float] | None = None,
        nullable: bool = False,
    ) -> None:
        self._value = value  # 遗留占位值
        self._description = description  # 字段描述
        self._minimum = minimum  # 最小值
        self._maximum = maximum  # 最大值
        self._enum = tuple(enum) if enum is not None else None  # 枚举值元组
        self._nullable = nullable  # 是否允许 null

    def to_json_schema(self) -> dict[str, Any]:
        """转换为 JSON Schema 字典。"""
        t: Any = "number"
        if self._nullable:
            t = ["number", "null"]
        d: dict[str, Any] = {"type": t}
        if self._description:
            d["description"] = self._description
        if self._minimum is not None:
            d["minimum"] = self._minimum
        if self._maximum is not None:
            d["maximum"] = self._maximum
        if self._enum is not None:
            d["enum"] = list(self._enum)
        return d


class BooleanSchema(Schema):
    """布尔参数（独立类，因 Python 禁止子类化 ``bool``）。

    职责：构造 JSON Schema 中的 ``boolean`` 类型片段，支持默认值与可空标记。
    """

    def __init__(
        self,
        *,
        description: str = "",
        default: bool | None = None,
        nullable: bool = False,
    ) -> None:
        self._description = description  # 字段描述
        self._default = default  # 默认值
        self._nullable = nullable  # 是否允许 null

    def to_json_schema(self) -> dict[str, Any]:
        """转换为 JSON Schema 字典。"""
        t: Any = "boolean"
        if self._nullable:
            t = ["boolean", "null"]
        d: dict[str, Any] = {"type": t}
        if self._description:
            d["description"] = self._description
        if self._default is not None:
            d["default"] = self._default
        return d


class ArraySchema(Schema):
    """数组参数：元素 schema 由 ``items`` 指定。

    职责：构造 JSON Schema 中的 ``array`` 类型片段，支持元素 schema、
    最小/最大元素数与可空标记。``items`` 可为 Schema 实例或 JSON Schema 字典。
    """

    def __init__(
        self,
        items: Any | None = None,
        *,
        description: str = "",
        min_items: int | None = None,
        max_items: int | None = None,
        nullable: bool = False,
    ) -> None:
        # 默认元素 schema 为空描述的 StringSchema
        self._items_schema: Any = items if items is not None else StringSchema("")
        self._description = description  # 字段描述
        self._min_items = min_items  # 最小元素数
        self._max_items = max_items  # 最大元素数
        self._nullable = nullable  # 是否允许 null

    def to_json_schema(self) -> dict[str, Any]:
        """转换为 JSON Schema 字典。"""
        t: Any = "array"
        if self._nullable:
            t = ["array", "null"]
        d: dict[str, Any] = {
            "type": t,
            "items": Schema.fragment(self._items_schema),  # 规范化元素 schema
        }
        if self._description:
            d["description"] = self._description
        if self._min_items is not None:
            d["minItems"] = self._min_items
        if self._max_items is not None:
            d["maxItems"] = self._max_items
        return d


class ObjectSchema(Schema):
    """对象参数：``properties`` 或关键字参数为字段名，值为子 Schema 或 JSON Schema 字典。

    职责：构造 JSON Schema 中的 ``object`` 类型片段，支持必填字段列表、
    根描述、additionalProperties 与可空标记。
    """

    def __init__(
        self,
        properties: Mapping[str, Any] | None = None,
        *,
        required: list[str] | None = None,
        description: str = "",
        additional_properties: bool | dict[str, Any] | None = None,
        nullable: bool = False,
        **kwargs: Any,
    ) -> None:
        # 合并位置参数 properties 与关键字参数 kwargs
        self._properties = dict(properties or {}, **kwargs)
        self._required = list(required or [])  # 必填字段名列表
        self._root_description = description  # 对象级描述
        self._additional_properties = additional_properties  # additionalProperties 设置
        self._nullable = nullable  # 是否允许 null

    def to_json_schema(self) -> dict[str, Any]:
        """转换为 JSON Schema 字典。"""
        t: Any = "object"
        if self._nullable:
            t = ["object", "null"]
        # 对每个属性值进行片段规范化
        props = {k: Schema.fragment(v) for k, v in self._properties.items()}
        out: dict[str, Any] = {"type": t, "properties": props}
        if self._required:
            out["required"] = self._required
        if self._root_description:
            out["description"] = self._root_description
        if self._additional_properties is not None:
            out["additionalProperties"] = self._additional_properties
        return out


def tool_parameters_schema(
    *,
    required: list[str] | None = None,
    description: str = "",
    **properties: Any,
) -> dict[str, Any]:
    """构建根工具参数 ``{"type": "object", "properties": ...}``，供 :meth:`Tool.parameters` 使用。

    参数:
        required: 必填参数名列表。
        description: 对象级描述。
        **properties: 字段名到 Schema 实例或 JSON Schema 字典的映射。

    返回:
        JSON Schema 字典。
    """
    return ObjectSchema(
        required=required,
        description=description,
        **properties,
    ).to_json_schema()
