"""Agent 工具的基类与参数 Schema 抽象。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的核心基础组件。
在项目架构中起到的作用：定义所有工具的抽象基类 ``Tool`` 与参数描述基类
``Schema``，并提供 ``@tool_parameters`` 装饰器，使具体工具能够声明式地
描述参数 JSON Schema 并获得统一的参数类型转换与校验能力。
"""
from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from typing import Any, TypeVar

if typing.TYPE_CHECKING:
    from pydantic import BaseModel

    from biscuitbot.agent.tools.context import ToolContext

_ToolT = TypeVar("_ToolT", bound="Tool")  # 工具类型变量，绑定到 Tool 基类

# JSON Schema 类型名到 Python 类型的映射，供 :meth:`Tool._cast_value` /
# :meth:`Schema.validate_json_schema_value` 共享使用
_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class Schema(ABC):
    """Abstract base for JSON Schema fragments describing tool parameters.

    Concrete types live in :mod:`biscuitbot.agent.tools.schema`; all implement
    :meth:`to_json_schema` and :meth:`validate_value`. Class methods
    :meth:`validate_json_schema_value` and :meth:`fragment` are the shared validation and normalization entry points.

    中文说明：描述工具参数的 JSON Schema 片段的抽象基类，具体子类位于
    ``schema`` 模块，统一通过 ``to_json_schema`` 与 ``validate_value`` 实现
    序列化与校验。
    """

    @staticmethod
    def resolve_jsonschema_type(t: Any) -> str | None:
        """Resolve the non-null type name from JSON Schema ``type`` (e.g. ``['string','null']`` -> ``'string'``).

        中文说明：从 JSON Schema 的 type 字段中解析出非 null 的类型名。
        """
        if isinstance(t, list):
            return next((x for x in t if x != "null"), None)
        return t  # type: ignore[return-value]

    @staticmethod
    def subpath(path: str, key: str) -> str:
        """拼接路径片段，用于在校验错误信息中定位嵌套字段。

        参数:
            path: 当前已累积的路径（可为空）。
            key: 待追加的字段名。

        返回:
            形如 ``path.key`` 或 ``key`` 的字符串。
        """
        return f"{path}.{key}" if path else key

    @staticmethod
    def validate_json_schema_value(val: Any, schema: dict[str, Any], path: str = "") -> list[str]:
        """Validate ``val`` against a JSON Schema fragment; returns error messages (empty means valid).

        Used by :class:`Tool` and each concrete Schema's :meth:`validate_value`.

        中文说明：依据 JSON Schema 片段校验 ``val``，返回错误信息列表（空表示通过）。
        供 :class:`Tool` 与各具体 Schema 的 :meth:`validate_value` 共享使用。
        """
        raw_type = schema.get("type")
        nullable = (isinstance(raw_type, list) and "null" in raw_type) or schema.get("nullable", False)
        t = Schema.resolve_json_schema_type(raw_type)
        label = path or "parameter"

        if nullable and val is None:
            return []  # 允许为 null 时直接通过
        if t == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
            return [f"{label} should be integer"]
        if t == "number" and (
            not isinstance(val, _JSON_TYPE_MAP["number"]) or isinstance(val, bool)
        ):
            return [f"{label} should be number"]
        if t in _JSON_TYPE_MAP and t not in ("integer", "number") and not isinstance(val, _JSON_TYPE_MAP[t]):
            return [f"{label} should be {t}"]

        errors: list[str] = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        # 数值范围校验
        if t in ("integer", "number"):
            if "minimum" in schema and val < schema["minimum"]:
                errors.append(f"{label} must be >= {schema['minimum']}")
            if "maximum" in schema and val > schema["maximum"]:
                errors.append(f"{label} must be <= {schema['maximum']}")
        # 字符串长度校验
        if t == "string" and isinstance(val, str):
            if "minLength" in schema and len(val) < schema["minLength"]:
                errors.append(f"{label} must be at least {schema['minLength']} chars")
            if "maxLength" in schema and len(val) > schema["maxLength"]:
                errors.append(f"{label} must be at most {schema['maxLength']} chars")
        # 对象类型：递归校验每个属性
        if t == "object" and isinstance(val, dict):
            props = schema.get("properties", {})
            for k in schema.get("required", []):
                if k not in val:
                    errors.append(f"missing required {Schema.subpath(path, k)}")
            for k, v in val.items():
                if k in props:
                    errors.extend(Schema.validate_json_schema_value(v, props[k], Schema.subpath(path, k)))
        # 数组类型：校验元素数量并递归校验每个元素
        if t == "array" and isinstance(val, list):
            if "minItems" in schema and len(val) < schema["minItems"]:
                errors.append(f"{label} must have at least {schema['minItems']} items")
            if "maxItems" in schema and len(val) > schema["maxItems"]:
                errors.append(f"{label} must be at most {schema['maxItems']} items")
            if "items" in schema:
                prefix = f"{path}[{{}}]" if path else "[{}]"
                for i, item in enumerate(val):
                    errors.extend(
                        Schema.validate_json_schema_value(item, schema["items"], prefix.format(i))
                    )
        return errors

    @staticmethod
    def fragment(value: Any) -> dict[str, Any]:
        """Normalize a Schema instance or an existing JSON Schema dict to a fragment dict.

        中文说明：将 Schema 实例或已存在的 JSON Schema 字典统一规范化为片段字典。
        """
        # 优先尝试 to_json_schema：需区分 Schema 实例与已是 JSON Schema 的字典
        to_js = getattr(value, "to_json_schema", None)
        if callable(to_js):
            return to_js()
        if isinstance(value, dict):
            return value
        raise TypeError(f"Expected schema object or dict, got {type(value).__name__}")

    @abstractmethod
    def to_json_schema(self) -> dict[str, Any]:
        """Return a fragment dict compatible with :meth:`validate_json_schema_value`.

        中文说明：返回与 :meth:`validate_json_schema_value` 兼容的片段字典。
        """
        ...

    def validate_value(self, value: Any, path: str = "") -> list[str]:
        """Validate a single value; returns error messages (empty means pass). Subclasses may override for extra rules.

        中文说明：校验单个值，返回错误信息列表（空表示通过）。子类可重写以增加额外规则。
        """
        return Schema.validate_json_schema_value(value, self.to_json_schema(), path)


class Tool(ABC):
    """Agent capability: read files, run commands, etc.

    中文说明：所有 agent 工具的抽象基类。子类需实现 ``name``、``description``、
    ``parameters`` 与 ``execute``，并可重写 ``read_only``、``exclusive`` 等
    属性以声明并发安全性。提供统一的参数类型转换与 JSON Schema 校验能力。
    """

    _TYPE_MAP = _JSON_TYPE_MAP  # 类型映射，复用模块级常量
    _BOOL_TRUE = frozenset(("true", "1", "yes"))  # 布尔真值的字符串集合
    _BOOL_FALSE = frozenset(("false", "0", "no"))  # 布尔假值的字符串集合

    @staticmethod
    def _resolve_type(t: Any) -> str | None:
        """Pick first non-null type from JSON Schema unions like ``['string','null']``.

        中文说明：从 JSON Schema 联合类型中选取首个非 null 的类型名。
        """
        return Schema.resolve_json_schema_type(t)

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls.

        中文说明：函数调用时使用的工具名称。
        """
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Description of what the tool does.

        中文说明：工具功能描述，供模型理解工具能力。
        """
        ...

    @property
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters.

        Subclasses must either override this or apply the
        ``@tool_parameters(...)`` class decorator.  Intentionally not
        ``@abstractmethod``: static checkers cannot see the decorator's
        runtime injection, so marking it abstract would flag every
        decorated subclass as uninstantiable.

        中文说明：工具参数的 JSON Schema。子类必须重写此属性或使用
        ``@tool_parameters(...)`` 装饰器。故意不标记为 ``@abstractmethod``：
        静态检查器无法看到装饰器的运行时注入，标记为抽象会使每个被装饰
        的子类都无法实例化。
        """
        raise NotImplementedError(
            f"{type(self).__name__} must define `parameters` "
            "or apply the @tool_parameters decorator"
        )

    @property
    def capability(self) -> str:
        """One-sentence capability boundary for retrieval and compact summary.

        Subclasses may set ``_capability``; otherwise the first 120 chars of
        :attr:`description` are used as a sensible default.

        中文说明：用于检索与精简摘要的单句能力边界。子类可设置 ``_capability``，
        未设置时取 :attr:`description` 的前 120 字符作为默认值。
        """
        return self._capability or self.description[:120]

    @property
    def read_only(self) -> bool:
        """Whether this tool is side-effect free and safe to parallelize.

        中文说明：该工具是否无副作用且可安全并行。
        """
        return False

    @property
    def concurrency_safe(self) -> bool:
        """Whether this tool can run alongside other concurrency-safe tools.

        中文说明：该工具能否与其他并发安全工具并行执行。
        """
        return self.read_only and not self.exclusive

    @property
    def exclusive(self) -> bool:
        """Whether this tool should run alone even if concurrency is enabled.

        中文说明：即使启用并发，该工具是否仍需独占执行。
        """
        return False

    # --- Plugin metadata ---

    config_key: str = ""  # 配置键名，用于从全局配置中读取该工具的配置段
    _plugin_discoverable: bool = True  # 是否可被插件发现机制扫描到
    _scopes: set[str] = {"core"}  # 工具可用作用域集合（如 core、subagent、memory）

    # --- Progressive discovery metadata ---

    #: Short capability boundary (1 sentence) shown in the INDEX.md table.
    #: Falls back to ``description[:120]`` when empty.
    #: 中文说明：在 INDEX.md 表格中展示的单句能力边界，为空时回退到 description 前 120 字符。
    _capability: str = ""
    #: When ``True`` the tool's full schema is always sent to the model.
    #: Other tools require ``discover_tools(name)`` to load their schema.
    #: 中文说明：为 True 时工具的完整 schema 始终发送给模型；其他工具需通过 discover_tools(name) 加载。
    _always_include: bool = False
    #: Path (relative to workspace or absolute) to the usage-doc markdown file.
    #: The model reads this via ``read_file`` before calling ``discover_tools``.
    #: Convention: ``docs/<tool_name>.md`` under the tools package.
    #: 中文说明：使用说明文档路径（工作区相对或绝对），模型在调用 discover_tools 前通过 read_file 读取。
    _usage_md: str = ""
    #: When ``True`` the tool was registered at runtime by the agent via
    #: ``register_tool`` (not a built-in).  Used for persistence.
    #: 中文说明：为 True 表示该工具由 agent 在运行时通过 register_tool 注册（非内置），用于持久化。
    _custom: bool = False

    @classmethod
    def config_cls(cls) -> type[BaseModel] | None:
        """返回该工具的配置模型类，无配置时返回 None。"""
        return None

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        """判断该工具在给定上下文下是否启用，默认始终启用。"""
        return True

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        """工厂方法：根据上下文创建工具实例，默认直接实例化。"""
        return cls()

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """Run the tool; returns a string or list of content blocks.

        中文说明：执行工具逻辑，返回字符串或内容块列表。
        """
        ...

    def _cast_object(self, obj: Any, schema: dict[str, Any]) -> dict[str, Any]:
        """递归地对对象内的每个属性进行类型转换。

        参数:
            obj: 待转换的对象。
            schema: 对象的 JSON Schema。

        返回:
            转换后的字典；非字典对象原样返回。
        """
        if not isinstance(obj, dict):
            return obj
        props = schema.get("properties", {})
        return {k: self._cast_value(v, props[k]) if k in props else v for k, v in obj.items()}

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply safe schema-driven casts before validation.

        中文说明：在校验前依据 Schema 对参数做安全的类型转换（如字符串转数字）。
        """
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return params
        return self._cast_object(params, schema)

    def _cast_value(self, val: Any, schema: dict[str, Any]) -> Any:
        """依据 Schema 将单个值转换为期望类型。

        参数:
            val: 原始值。
            schema: 值的 JSON Schema。

        返回:
            转换后的值；无法转换时原样返回。
        """
        t = self._resolve_type(schema.get("type"))

        # 类型已匹配则直接返回
        if t == "boolean" and isinstance(val, bool):
            return val
        if t == "integer" and isinstance(val, int) and not isinstance(val, bool):
            return val
        if t in self._TYPE_MAP and t not in ("boolean", "integer", "array", "object"):
            expected = self._TYPE_MAP[t]
            if isinstance(val, expected):
                return val

        # 字符串转数值
        if isinstance(val, str) and t in ("integer", "number"):
            try:
                return int(val) if t == "integer" else float(val)
            except ValueError:
                return val

        if t == "string":
            return val if val is None else str(val)

        # 字符串转布尔
        if t == "boolean" and isinstance(val, str):
            low = val.lower()
            if low in self._BOOL_TRUE:
                return True
            if low in self._BOOL_FALSE:
                return False
            return val

        # 数组：递归转换每个元素
        if t == "array" and isinstance(val, list):
            items = schema.get("items")
            return [self._cast_value(x, items) for x in val] if items else val

        # 对象：递归转换
        if t == "object" and isinstance(val, dict):
            return self._cast_object(val, schema)

        return val

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate against JSON schema; empty list means valid.

        中文说明：依据 JSON Schema 校验参数，返回错误信息列表（空表示通过）。
        """
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return Schema.validate_json_schema_value(params, {**schema, "type": "object"}, "")

    def to_schema(self) -> dict[str, Any]:
        """OpenAI function schema.

        中文说明：生成 OpenAI 函数调用格式的 schema 字典。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool_parameters(schema: dict[str, Any]) -> Callable[[type[_ToolT]], type[_ToolT]]:
    """Class decorator: attach JSON Schema and inject a concrete ``parameters`` property.

    Use on ``Tool`` subclasses instead of writing ``@property def parameters``. The
    schema is stored on the class and returned as a fresh copy on each access.

    中文说明：类装饰器，将 JSON Schema 附加到类上并注入具体的 ``parameters``
    属性。用于 ``Tool`` 子类，替代手写 ``@property def parameters``。schema 存储
    在类上，每次访问返回全新副本。

    示例::

        @tool_parameters({
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        })
        class ReadFileTool(Tool):
            ...
    """

    def decorator(cls: type[_ToolT]) -> type[_ToolT]:
        frozen = deepcopy(schema)  # 冻结 schema 副本，避免运行时被修改

        @property
        def parameters(self: Any) -> dict[str, Any]:
            return deepcopy(frozen)  # 每次访问返回全新副本，防止外部修改污染

        cls.parameters = parameters  # type: ignore[assignment]

        # 从抽象方法集合中移除 parameters，使被装饰的子类可被实例化
        abstract = getattr(cls, "__abstractmethods__", None)
        if abstract is not None and "parameters" in abstract:
            cls.__abstractmethods__ = frozenset(abstract - {"parameters"})  # type: ignore[misc]

        return cls

    return decorator
