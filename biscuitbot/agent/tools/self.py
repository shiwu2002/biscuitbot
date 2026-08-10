"""MyTool：agent 循环的运行时状态自省与配置工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中的"自省"组件。MyTool
允许 agent 检查和修改自身的运行时配置（如模型、最大迭代次数、上下文窗口大
小），查看当前状态（迭代进度、token 用量、子 agent 状态），并将笔记存储到
跨轮次持久化的 scratchpad 中。

该工具通过 ``RuntimeState`` 协议与 ``AgentLoop`` 交互，并实现了多层安全防护：
- ``BLOCKED``：完全禁止访问的核心基础设施属性
- ``READ_ONLY``：可读但不可写的子系统属性
- ``_DENIED_ATTRS``：禁止访问的 Python 反射属性
- ``_SENSITIVE_NAMES``：敏感字段名（如 api_key、token）
- ``RESTRICTED``：可修改但有类型与范围限制的属性
"""

from __future__ import annotations

import time  # 时间计算，用于子 agent 运行时长显示
from typing import TYPE_CHECKING, Any  # 类型注解

from loguru import logger  # 日志记录

from biscuitbot.agent.tools.base import Tool  # 工具基类
from biscuitbot.agent.tools.context import ContextAware, RequestContext  # 上下文感知 mixin 与请求上下文
from biscuitbot.agent.tools.runtime_state import RuntimeState  # 运行时状态协议
from biscuitbot.config_base import Base  # 配置基类

if TYPE_CHECKING:  # 仅类型检查时导入，避免循环依赖
    from biscuitbot.agent.subagent import SubagentStatus


class MyToolConfig(Base):
    """自省工具配置。"""
    enable: bool = True  # 是否启用该工具
    allow_set: bool = False  # 是否允许 agent 修改运行时配置


def _has_real_attr(obj: Any, key: str) -> bool:
    """检查对象是否真实拥有某属性（排除 mock 自动生成的属性）。

    用于防止 mock 对象在 ``getattr`` 时返回自动生成的虚假属性。
    """
    if isinstance(obj, dict):
        return key in obj
    d = getattr(obj, "__dict__", None)
    if d is not None and key in d:
        return True
    for cls in type(obj).__mro__:
        if key in cls.__dict__:
            return True
    return False


def _is_subagent_status(value: Any) -> bool:
    """判断值是否为 SubagentStatus 实例（延迟导入避免循环依赖）。"""
    from biscuitbot.agent.subagent import SubagentStatus

    return isinstance(value, SubagentStatus)


class MyTool(Tool, ContextAware):
    """检查和设置 agent 循环的运行时配置。"""

    _capability = (
        "Inspect and update the agent's runtime configuration and active goals."
    )
    _usage_md = "docs/my.md"  # 使用说明文档路径

    _plugin_discoverable = False  # 需要 AgentLoop 引用，手动注册
    config_key = "my"

    @classmethod
    def config_cls(cls):
        return MyToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.my.enable

    # 完全禁止访问的属性集合（检查与修改均被拒绝）
    BLOCKED = frozenset({
        # 核心基础设施
        "bus", "provider", "_running", "tools",
        # 配置管理
        "_runtime_vars",
        # 子系统
        "runner", "sessions", "consolidator",
        "dream", "auto_compact", "context", "commands",
        # 敏感运行时状态（凭据、消息路由、任务跟踪）
        "_mcp_servers", "_mcp_stacks", "_pending_queues",
        "_session_locks", "_active_tasks", "_background_tasks",
        # 安全边界（检查与修改均被拒绝）
        "restrict_to_workspace", "channels_config",
        "_concurrency_gate", "_unified_session", "_extra_hooks",
    })

    # 只读属性集合：可检查但不可修改
    READ_ONLY = frozenset({
        "subagents",  # 可观察但替换会破坏系统
        "_current_iteration",  # 仅由 runner 更新
        "exec_config",  # 允许检查（如查看沙箱），禁止修改
        "web_config",  # 允许检查（如查看启用状态），禁止修改
        "workspace_sandbox",  # 工作区强制等级的只读视图
    })

    # 禁止访问的 Python 反射属性（防止逃逸沙箱）
    _DENIED_ATTRS = frozenset({
        "__class__", "__dict__", "__bases__", "__subclasses__", "__mro__",
        "__init__", "__new__", "__reduce__", "__getstate__", "__setstate__",
        "__del__", "__call__", "__getattr__", "__setattr__", "__delattr__",
        "__code__", "__globals__", "func_globals", "func_code",
        "__wrapped__", "__closure__",
    })

    # 敏感子字段名：无论父路径如何均视为敏感
    _SENSITIVE_NAMES = frozenset({
        "api_key", "secret", "password", "token", "credential",
        "private_key", "access_token", "refresh_token", "auth",
    })

    @classmethod
    def _is_sensitive_field_name(cls, name: str) -> bool:
        """判断字段名是否敏感（含下划线拆分后的子词匹配）。"""
        lowered = name.lower()
        return lowered in cls._SENSITIVE_NAMES or any(
            part in cls._SENSITIVE_NAMES for part in lowered.split("_")
        )

    # 可修改但有类型与范围限制的属性
    RESTRICTED: dict[str, dict[str, Any]] = {
        "max_iterations":        {"type": int, "min": 1,   "max": 100},  # 单轮最大迭代次数
        "context_window_tokens": {"type": int, "min": 4096, "max": 1_000_000},  # 上下文窗口 token 数
        "model":                 {"type": str, "min_len": 1},  # 模型名称
    }

    _MAX_RUNTIME_KEYS = 64  # scratchpad 最大键数

    def __init__(self, runtime_state: RuntimeState, modify_allowed: bool = True) -> None:
        self._runtime_state = runtime_state  # 运行时状态提供者（AgentLoop）
        self._modify_allowed = modify_allowed  # 是否允许修改
        self._channel = ""  # 当前请求来源渠道
        self._chat_id = ""  # 当前请求会话 ID

    def __deepcopy__(self, memo: dict[int, Any]) -> MyTool:
        """自定义深拷贝：共享 runtime_state 引用，避免复制整个 AgentLoop。"""
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        result._runtime_state = self._runtime_state
        result._modify_allowed = self._modify_allowed
        result._channel = self._channel
        result._chat_id = self._chat_id
        return result

    def set_context(self, ctx: RequestContext) -> None:
        """设置当前请求上下文（渠道与会话 ID），用于审计日志。"""
        self._channel = ctx.channel
        self._chat_id = ctx.chat_id

    @property
    def name(self) -> str:
        return "my"

    @property
    def description(self) -> str:
        base = (
            "Check and set your own runtime state.\n"
            "Actions: check, set.\n"
            "- check (no key): full config overview — start here.\n"
            "- check (key): drill into a value. Dot-paths allowed "
            "(e.g. '_last_usage.prompt_tokens', 'web_config.enable').\n"
            "- set (key, value): change config or store notes in your scratchpad. "
            "Scratchpad keys persist across turns but not restarts.\n"
            "Key values: _current_iteration (current progress), "
            "max_iterations - _current_iteration = remaining iterations.\n"
            "Note: web_config and exec_config are readable but read-only.\n"
            "\n"
            "When to use:\n"
            "- User asks about your model, settings, or token usage → check that key.\n"
            "- A tool fails or behaves unexpectedly → check the related config to diagnose.\n"
            "- User asks you to remember a preference for this session → set to store it in your scratchpad.\n"
            "- About to start a large task → check context_window_tokens and max_iterations first."
        )
        if not self._modify_allowed:
            base += "\nREAD-ONLY MODE: set is disabled."
        else:
            base += (
                "\nIMPORTANT: Before setting state, predict the potential impact. "
                "If the operation could cause crashes or instability "
                "(e.g. changing model), warn the user first."
            )
        return base

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["check", "set"],
                    "description": "Action to perform",
                },
                "key": {
                    "type": "string",
                    "description": "Dot-path for check/set. Examples: 'max_iterations', 'workspace', 'provider_retry_mode'. "
                    "For check without key, shows all config values.",
                },
                "value": {"description": "New value (for set). Type must match target (int for max_iterations/context_window_tokens, str for model)."},
            },
            "required": ["action"],
        }

    def _audit(self, action: str, detail: str) -> None:
        """记录审计日志：操作类型、详情与会话标识。"""
        session = f"{self._channel}:{self._chat_id}" if self._channel else "unknown"
        logger.info("self.{} | {} | session:{}", action, detail, session)

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------

    def _resolve_path(self, path: str) -> tuple[Any, str | None]:
        """解析点分隔的属性路径，返回 (目标对象, 错误信息)。

        逐级访问属性，每级都检查是否在禁止/敏感集合中。支持字典与对象的
        混合访问。
        """
        parts = path.split(".")
        obj = self._runtime_state
        for part in parts:
            if part in self._DENIED_ATTRS or part.startswith("__"):
                return None, f"'{part}' is not accessible"
            if part in self.BLOCKED:
                return None, f"'{part}' is not accessible"
            if part.lower() in self._SENSITIVE_NAMES:
                return None, f"'{part}' is not accessible"
            try:
                if isinstance(obj, dict):
                    if part in obj:
                        obj = obj[part]
                    else:
                        return None, f"'{part}' not found in dict"
                else:
                    obj = getattr(obj, part)
            except (KeyError, AttributeError) as e:
                return None, f"'{part}' not found: {e}"
        return obj, None

    @staticmethod
    def _validate_key(key: str | None, label: str = "key") -> str | None:
        """校验键名非空；返回错误信息或 None。"""
        if not key or not key.strip():
            return f"Error: '{label}' cannot be empty or whitespace"
        return None

    # ------------------------------------------------------------------
    # 智能格式化
    # ------------------------------------------------------------------

    @staticmethod
    def _format_status(st: "SubagentStatus", indent: str = "  ") -> str:
        """格式化子 agent 状态为可读文本。"""
        elapsed = time.monotonic() - st.started_at
        tool_summary = ", ".join(
            f"{e.get('name', '?')}({e.get('status', '?')})" for e in st.tool_events[-5:]
        ) or "none"
        lines = [
            f"{indent}phase: {st.phase}, iteration: {st.iteration}, elapsed: {elapsed:.1f}s",
            f"{indent}tools: {tool_summary}",
            f"{indent}usage: {st.usage or 'n/a'}",
        ]
        if st.error:
            lines.append(f"{indent}error: {st.error}")
        if st.stop_reason:
            lines.append(f"{indent}stop_reason: {st.stop_reason}")
        return "\n".join(lines)

    @staticmethod
    def _format_value(val: Any, key: str = "") -> str:
        """将值格式化为人类可读的字符串。

        根据值类型采用不同策略：子 agent 状态展开为详情；字典/列表按大小
        展示内容或键名；标量直接 repr；Pydantic 模型展示字段值。
        """
        if _is_subagent_status(val):
            header = f"Subagent [{val.task_id}] '{val.label}'"
            detail = MyTool._format_status(val, "  ")
            return f"{header}\n  task: {val.task_description}\n{detail}"
        # SubagentManager: delegate to its _task_statuses dict
        if hasattr(val, "_task_statuses") and isinstance(val._task_statuses, dict):
            return MyTool._format_value(val._task_statuses, key)
        if isinstance(val, dict) and val and _is_subagent_status(next(iter(val.values()))):
            prefix = f"{key}: " if key else ""
            lines = [f"{prefix}{len(val)} subagent(s):"]
            for tid, st in val.items():
                detail = MyTool._format_status(st, "    ")
                lines.append(f"  [{tid}] '{st.label}'\n{detail}")
            return "\n".join(lines)
        if hasattr(val, "tool_names"):
            return f"tools: {len(val.tool_names)} registered — {val.tool_names}"
        # Scalar types — repr is fine
        if isinstance(val, (str, int, float, bool, type(None))):
            r = repr(val)
            return f"{key}: {r}" if key else r
        # Dict — small: show content; large: show keys for dot-path navigation
        if isinstance(val, dict):
            ks = list(val.keys())
            if not ks:
                return f"{key}: {{}}" if key else "{}"
            if len(ks) <= 5:
                r = repr(val)
                if len(r) <= 200:
                    return f"{key}: {r}" if key else r
            preview = ", ".join(str(k) for k in ks[:15])
            suffix = ", ..." if len(ks) > 15 else ""
            return f"{key}: {{{preview}{suffix}}}" if key else f"{{{preview}{suffix}}}"
        # List/tuple — count for large, repr for small
        if isinstance(val, (list, tuple)):
            if len(val) > 20:
                return f"{key}: [{len(val)} items]" if key else f"[{len(val)} items]"
            r = repr(val)
            return f"{key}: {r}" if key else r
        # Complex object — small Pydantic models: show values; others: show field names for navigation
        cls_name = type(val).__name__
        model_fields = getattr(type(val), "model_fields", None)
        if model_fields:
            fields = list(model_fields.keys())
            if len(fields) <= 8:
                # Small config objects: show field=value pairs
                pairs = []
                for f in fields:
                    fv = getattr(val, f, "?")
                    if MyTool._is_sensitive_field_name(f):
                        continue
                    if isinstance(fv, (str, int, float, bool, type(None))):
                        pairs.append(f"{f}={fv!r}")
                    else:
                        pairs.append(f"{f}=<{type(fv).__name__}>")
                preview = ", ".join(pairs)
                return f"{key}: {preview}" if key else preview
        else:
            fields = [a for a in getattr(val, "__dict__", {}) if not a.startswith("__")]
        if fields:
            preview = ", ".join(str(f) for f in fields[:20])
            suffix = ", ..." if len(fields) > 20 else ""
            return f"{key}: <{cls_name}> [{preview}{suffix}]" if key else f"<{cls_name}> [{preview}{suffix}]"
        r = repr(val)
        return f"{key}: {r}" if key else r

    # ------------------------------------------------------------------
    # 动作分发
    # ------------------------------------------------------------------

    async def execute(
        self,
        action: str,
        key: str | None = None,
        value: Any = None,
        **_kwargs: Any,
    ) -> str:
        """执行检查或修改动作。

        参数:
            action: 'check'（检查）或 'set'（修改）。
            key: 点分隔的属性路径；检查时为空表示查看全部配置。
            value: 修改时的新值。

        返回:
            操作结果文本或错误信息。
        """
        if action in ("inspect", "check"):
            return self._inspect(key)
        if not self._modify_allowed:
            return "Error: set is disabled (tools.my.allow_set is false)"
        if action in ("modify", "set"):
            return self._modify(key, value)
        return f"Unknown action: {action}"

    # -- 检查 --

    def _inspect(self, key: str | None) -> str:
        """检查指定 key 的值；key 为空时返回全部配置概览。

        支持点路径、scratchpad 别名、以及回退到 _runtime_vars 中存储的键。
        """
        if not key:
            return self._inspect_all()
        top = key.split(".")[0]
        if top in self._DENIED_ATTRS or top.startswith("__"):
            return f"Error: '{top}' is not accessible"
        obj, err = self._resolve_path(key)
        if err:
            # "scratchpad" 是 _runtime_vars 的别名
            if key == "scratchpad":
                rv = self._runtime_state._runtime_vars
                return self._format_value(rv, "scratchpad") if rv else "scratchpad is empty"
            # 回退：检查 _runtime_vars 中由 modify 存储的简单键
            if "." not in key and key in self._runtime_state._runtime_vars:
                return self._format_value(self._runtime_state._runtime_vars[key], key)
            return f"Error: {err}"
        # 防止 mock 自动生成的属性
        if "." not in key and not _has_real_attr(self._runtime_state, key):
            if key in self._runtime_state._runtime_vars:
                return self._format_value(self._runtime_state._runtime_vars[key], key)
            return f"Error: '{key}' not found"
        return self._format_value(obj, key)

    def _inspect_all(self) -> str:
        """返回运行时配置的完整概览。"""
        state = self._runtime_state
        parts: list[str] = []
        # 受限键（有范围限制的配置项）
        for k in self.RESTRICTED:
            parts.append(self._format_value(getattr(state, k, None), k))
        parts.append(self._format_value(state.model_preset, "model_preset"))
        # 描述中提及的其他有用顶层键
        for k in ("workspace", "provider_retry_mode", "max_tool_result_chars", "_current_iteration", "web_config", "exec_config", "workspace_sandbox", "subagents"):
            if _has_real_attr(state, k):
                parts.append(self._format_value(getattr(state, k, None), k))
        # token 用量
        usage = state._last_usage
        if usage:
            parts.append(self._format_value(usage, "_last_usage"))
        rv = state._runtime_vars
        if rv:
            parts.append(self._format_value(rv, "scratchpad"))
        return "\n".join(parts)

    # -- 修改 --

    def _modify(self, key: str | None, value: Any) -> str:
        """修改运行时配置或存储 scratchpad 笔记。

        按键名分派到不同处理路径：受限键走 ``_modify_restricted``，
        自由键走 ``_modify_free``，点路径直接设置叶子属性。
        """
        if err := self._validate_key(key):
            return err
        top = key.split(".")[0]
        if top in self.BLOCKED or top in self._DENIED_ATTRS or top.startswith("__") or top.lower() in self._SENSITIVE_NAMES:
            self._audit("modify", f"BLOCKED {key}")
            return f"Error: '{key}' is protected and cannot be modified"
        if top in self.READ_ONLY:
            self._audit("modify", f"READ_ONLY {key}")
            return f"Error: '{key}' is read-only and cannot be modified"
        if "." in key:
            # 点路径：解析父对象后设置叶子属性
            parent_path, leaf = key.rsplit(".", 1)
            if leaf in self._DENIED_ATTRS or leaf.startswith("__"):
                self._audit("modify", f"BLOCKED leaf '{leaf}'")
                return f"Error: '{leaf}' is not accessible"
            if leaf.lower() in self._SENSITIVE_NAMES:
                self._audit("modify", f"BLOCKED sensitive leaf '{leaf}'")
                return f"Error: '{leaf}' is not accessible"
            parent, err = self._resolve_path(parent_path)
            if err:
                return f"Error: {err}"
            if isinstance(parent, dict):
                parent[leaf] = value
            else:
                setattr(parent, leaf, value)
            self._audit("modify", f"{key} = {value!r}")
            return f"Set {key} = {value!r}"
        if key in self.RESTRICTED:
            return self._modify_restricted(key, value)
        return self._modify_free(key, value)

    def _modify_restricted(self, key: str, value: Any) -> str:
        """修改受范围限制的配置项（如 max_iterations、model）。

        执行类型转换、范围校验，并在修改后触发副作用（如重置预设、同步子
        agent 限制）。
        """
        spec = self.RESTRICTED[key]
        expected = spec["type"]
        if expected is int and isinstance(value, bool):
            return f"Error: '{key}' must be {expected.__name__}, got bool"
        if not isinstance(value, expected):
            try:
                value = expected(value)
            except (ValueError, TypeError):
                return f"Error: '{key}' must be {expected.__name__}, got {type(value).__name__}"
        old = getattr(self._runtime_state, key)
        if "min" in spec and value < spec["min"]:
            return f"Error: '{key}' must be >= {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return f"Error: '{key}' must be <= {spec['max']}"
        if "min_len" in spec and len(str(value)) < spec["min_len"]:
            return f"Error: '{key}' must be at least {spec['min_len']} characters"
        setattr(self._runtime_state, key, value)
        # 修改 model 时清除活跃预设标记
        if key == "model":
            self._runtime_state._active_preset = None
        # 修改 max_iterations 时同步子 agent 的运行时限制
        if key == "max_iterations" and hasattr(self._runtime_state, "_sync_subagent_runtime_limits"):
            self._runtime_state._sync_subagent_runtime_limits()
        self._audit("modify", f"{key}: {old!r} -> {value!r}")
        return f"Set {key} = {value!r} (was {old!r})"

    def _modify_free(self, key: str, value: Any) -> str:
        """修改自由键：已有属性走类型检查后 setattr，新键存入 scratchpad。

        scratchpad 值必须是 JSON 安全的（可序列化为 str/int/float/bool/
        None/list/dict），且总键数不超过 ``_MAX_RUNTIME_KEYS``。
        """
        if _has_real_attr(self._runtime_state, key):
            old = getattr(self._runtime_state, key)
            if isinstance(old, (str, int, float, bool)):
                old_t, new_t = type(old), type(value)
                if old_t is float and new_t is int:
                    pass  # 允许 int → float 隐式转换
                elif old_t is not new_t:
                    self._audit(
                        "modify",
                        f"REJECTED type mismatch {key}: expects {old_t.__name__}, got {new_t.__name__}",
                    )
                    return f"Error: '{key}' expects {old_t.__name__}, got {new_t.__name__}"
            try:
                setattr(self._runtime_state, key, value)
            except (ValueError, KeyError) as e:
                self._audit("modify", f"REJECTED {key}: {e}")
                return f"Error: {e}"
            self._audit("modify", f"{key}: {old!r} -> {value!r}")
            return f"Set {key} = {value!r} (was {old!r})"
        if callable(value):
            self._audit("modify", f"REJECTED callable {key}")
            return "Error: cannot store callable values"
        err = self._validate_json_safe(value)
        if err:
            self._audit("modify", f"REJECTED {key}: {err}")
            return f"Error: {err}"
        if key not in self._runtime_state._runtime_vars and len(self._runtime_state._runtime_vars) >= self._MAX_RUNTIME_KEYS:
            self._audit("modify", f"REJECTED {key}: max keys ({self._MAX_RUNTIME_KEYS}) reached")
            return f"Error: scratchpad is full (max {self._MAX_RUNTIME_KEYS} keys). Remove unused keys first."
        old = self._runtime_state._runtime_vars.get(key)
        self._runtime_state._runtime_vars[key] = value
        self._audit("modify", f"scratchpad.{key}: {old!r} -> {value!r}")
        return f"Set scratchpad.{key} = {value!r}"

    @classmethod
    def _validate_json_safe(cls, value: Any, depth: int = 0) -> str | None:
        """校验值是否可安全序列化为 JSON（递归检查嵌套结构）。

        允许的类型：str、int、float、bool、None、list、dict（键必须为 str）。
        嵌套深度不超过 10 层。
        """
        if depth > 10:
            return "value nesting too deep (max 10 levels)"
        if isinstance(value, (str, int, float, bool, type(None))):
            return None
        if isinstance(value, list):
            for i, item in enumerate(value):
                if err := cls._validate_json_safe(item, depth + 1):
                    return f"list[{i}] contains {err}"
            return None
        if isinstance(value, dict):
            for k, v in value.items():
                if not isinstance(k, str):
                    return f"dict key must be str, got {type(k).__name__}"
                if err := cls._validate_json_safe(v, depth + 1):
                    return f"dict key '{k}' contains {err}"
            return None
        return f"unsupported type {type(value).__name__}"
