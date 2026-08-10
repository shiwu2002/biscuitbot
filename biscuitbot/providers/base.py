"""LLM Provider 基础接口与共享实现。

所属模块与项目作用
===================
本文件位于 biscuitbot/providers 目录，是 LLM Provider 层的核心抽象组件。
在项目架构中起到的作用：
- 定义所有 Provider 后端必须遵循的抽象基类 :class:`LLMProvider`，以及
  跨后端共享的数据结构（:class:`LLMResponse`、:class:`ToolCallRequest`、
  :class:`GenerationSettings`）。
- 集中实现 Provider 层通用能力：消息规范化、错误归一化、可重试错误识别、
  429 配额/限流区分、角色交替约束、流式空闲超时、带心跳的重试编排等，
  避免在各具体后端中重复实现。
- 通过统一接口屏蔽不同 LLM 厂商协议差异，供上层（agent/runner）以一致
  方式调用模型。
"""

import asyncio  # 异步等待、超时与重试休眠
import json  # 解析工具入参 JSON
import os  # 读取流式空闲超时环境变量
import re  # 从错误文本中解析 Retry-After
from abc import ABC, abstractmethod  # 定义抽象基类与抽象方法
from collections.abc import Awaitable, Callable  # 回调类型签名
from contextlib import suppress  # 容忍解析失败
from dataclasses import dataclass, field  # 数据类装饰器
from datetime import datetime, timezone  # 处理 HTTP 日期格式的 Retry-After
from email.utils import parsedate_to_datetime  # 解析 HTTP 日期
from typing import Any

import json_repair  # 容错修复历史回放中的畸形 JSON
from loguru import logger

from biscuitbot.utils.helpers import image_placeholder_text  # 图片占位文本生成

# 流式空闲超时的环境变量名
STREAM_IDLE_TIMEOUT_ENV = "BISCUITBOT_STREAM_IDLE_TIMEOUT_S"
DEFAULT_STREAM_IDLE_TIMEOUT_S = 90.0  # 默认空闲超时（秒）
MAX_STREAM_IDLE_TIMEOUT_S = 3600.0  # 最大空闲超时上限（秒）


def resolve_stream_idle_timeout_s(
    *,
    env_value: str | None = None,
    default: float = DEFAULT_STREAM_IDLE_TIMEOUT_S,
    maximum: float = MAX_STREAM_IDLE_TIMEOUT_S,
) -> float:
    """从环境变量/配置文本解析出一个安全的流式空闲超时秒数。

    解析规则：
    - 缺失或空白则返回默认值。
    - 非数字则忽略并警告，返回默认值。
    - 非正数则忽略并警告，返回默认值。
    - 超过上限则截断到上限并警告。
    """
    raw = os.environ.get(STREAM_IDLE_TIMEOUT_ENV) if env_value is None else env_value
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid {}={!r}; using {}", STREAM_IDLE_TIMEOUT_ENV, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive {}={!r}; using {}", STREAM_IDLE_TIMEOUT_ENV, raw, default)
        return default
    if value > maximum:
        logger.warning("Clamping {}={!r} to {}", STREAM_IDLE_TIMEOUT_ENV, raw, maximum)
        return maximum
    return value


@dataclass
class ToolCallRequest:
    """LLM 发起的一次工具调用请求。

    封装工具调用 ID、名称与入参，以及若干 Provider 特有字段（用于在
    回放历史时保留原始协议信息）。
    """
    id: str
    name: str
    arguments: Any
    extra_content: dict[str, Any] | None = None
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None

    def to_openai_tool_call(self) -> dict[str, Any]:
        """序列化为 OpenAI 风格的 tool_call 载荷字典。"""
        arguments = (
            self.arguments
            if isinstance(self.arguments, str)
            else json.dumps(self.arguments, ensure_ascii=False)
        )
        tool_call = {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": arguments,
            },
        }
        if self.extra_content:
            tool_call["extra_content"] = self.extra_content
        if self.provider_specific_fields:
            tool_call["provider_specific_fields"] = self.provider_specific_fields
        if self.function_provider_specific_fields:
            tool_call["function"]["provider_specific_fields"] = self.function_provider_specific_fields
        return tool_call


def parse_tool_arguments(arguments: Any) -> Any:
    """解析 Provider 返回的工具入参，但不擅自推断可执行参数。

    解析规则：
    - 有效的 JSON 对象字符串转为 dict。
    - 空字符串转为无参调用（空 dict）。
    - 畸形 JSON 与 JSON 数组/标量原样保留，交由 ToolRegistry 在执行前拒绝。
    """
    if arguments is None:
        return {}
    if not isinstance(arguments, str):
        return arguments

    stripped = arguments.strip()
    if not stripped:
        return {}

    try:
        parsed = json.loads(stripped)
    except Exception:
        # 解析失败：原样返回，由注册表在执行前校验
        return arguments
    return arguments if parsed is None else parsed


def tool_arguments_object_for_replay(arguments: Any) -> dict[str, Any]:
    """仅用于 Provider 历史回放时，返回对象形式的工具入参。

    这条兼容路径可能会修复畸形 JSON，因为它只负责把已有对话历史整形
    为 Provider 协议所需结构。请勿用于即将执行的新生成的工具调用。
    """
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str):
        return {}

    stripped = arguments.strip()
    if not stripped:
        return {}

    try:
        parsed = json.loads(stripped)
    except Exception:
        # 标准解析失败时，尝试用 json_repair 容错修复
        try:
            parsed = json_repair.loads(stripped)
        except Exception:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_arguments_json_for_replay(arguments: Any) -> str:
    """仅用于 Provider 历史回放时，返回 JSON 对象字符串形式的工具入参。"""
    return json.dumps(tool_arguments_object_for_replay(arguments), ensure_ascii=False)


@dataclass
class LLMResponse:
    """LLM Provider 的统一响应数据结构。

    同时承载正常响应（content/tool_calls/usage）与错误响应
    （finish_reason == "error" 时的结构化错误元数据），供重试策略决策。
    """
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    retry_after: float | None = None  # Provider 指定的重试等待秒数
    reasoning_content: str | None = None  # Kimi、DeepSeek-R1、MiMo 等的推理内容
    thinking_blocks: list[dict] | None = None  # Anthropic 扩展思考块
    # finish_reason == "error" 时供重试策略使用的结构化错误元数据
    error_status_code: int | None = None
    error_kind: str | None = None  # 错误大类，如 "timeout"、"connection"
    error_type: str | None = None  # 语义化错误类型，如 insufficient_quota
    error_code: str | None = None  # 语义化错误码，如 rate_limit_exceeded
    error_retry_after_s: float | None = None
    error_should_retry: bool | None = None

    @property
    def has_tool_calls(self) -> bool:
        """判断响应是否包含工具调用。"""
        return len(self.tool_calls) > 0

    @property
    def should_execute_tools(self) -> bool:
        """是否应执行工具：仅当存在工具调用且 finish_reason 属于可执行工具的停止原因。

        会拦截 ``refusal`` / ``content_filter`` / ``error`` 下的网关注入式
        工具调用（#3220）。
        """
        if not self.has_tool_calls:
            return False
        return self.finish_reason in ("tool_calls", "function_call", "stop")


@dataclass(frozen=True)
class GenerationSettings:
    """默认生成参数。"""

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None


# 角色交替修复时使用的合成 user 消息内容
_SYNTHETIC_USER_CONTENT = "(conversation continued)"


class LLMProvider(ABC):
    """所有 LLM Provider 后端的抽象基类。

    定义统一的 chat / chat_stream / get_default_model 抽象接口，并集中实现
    重试、错误识别、消息规范化等跨后端共享逻辑。
    """

    supports_progress_deltas = False  # 子类可覆写：是否支持进度增量回调

    _CHAT_RETRY_DELAYS = (1, 2, 4)  # 标准重试的退避延迟序列（秒）
    _PERSISTENT_MAX_DELAY = 60  # 持久重试模式下的单次最大延迟（秒）
    _PERSISTENT_IDENTICAL_ERROR_LIMIT = 10  # 持久重试遇到相同错误的最大次数
    _RETRY_HEARTBEAT_CHUNK = 30  # 重试休眠中心跳回调的分片时长（秒）
    # 用于文本匹配的瞬时错误标记（任意命中即视为可重试）
    _TRANSIENT_ERROR_MARKERS = (
        "429",
        "rate limit",
        "500",
        "502",
        "503",
        "504",
        "overloaded",
        "timeout",
        "timed out",
        "connection",
        "server error",
        "temporarily unavailable",
        "速率限制",
        "访问量过大",
    )
    _RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})  # 默认可重试的 HTTP 状态码
    _TRANSIENT_ERROR_KINDS = frozenset({"timeout", "connection"})  # 可重试的错误大类
    # 不可重试的 429 错误 token：通常是配额/计费类，重试也无法恢复
    _NON_RETRYABLE_429_ERROR_TOKENS = frozenset({
        "insufficient_quota",
        "quota_exceeded",
        "quota_exhausted",
        "billing_hard_limit_reached",
        "insufficient_balance",
        "credit_balance_too_low",
        "billing_not_active",
        "payment_required",
    })
    # 可重试的 429 错误 token：通常是真正的限流
    _RETRYABLE_429_ERROR_TOKENS = frozenset({
        "rate_limit_exceeded",
        "rate_limit_error",
        "too_many_requests",
        "request_limit_exceeded",
        "requests_limit_exceeded",
        "overloaded_error",
    })
    # 不可重试的 429 文本标记（用于 content 文本匹配）
    _NON_RETRYABLE_429_TEXT_MARKERS = (
        "insufficient_quota",
        "insufficient quota",
        "quota exceeded",
        "quota exhausted",
        "billing hard limit",
        "billing_hard_limit_reached",
        "billing not active",
        "insufficient balance",
        "insufficient_balance",
        "credit balance too low",
        "payment required",
        "out of credits",
        "out of quota",
        "exceeded your current quota",
    )
    # 可重试的 429 文本标记
    _RETRYABLE_429_TEXT_MARKERS = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "retry after",
        "try again in",
        "temporarily unavailable",
        "overloaded",
        "concurrency limit",
        "速率限制",
    )

    _SENTINEL = object()  # 用于区分"未传参"与"显式传 None"的哨兵对象

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        """初始化 Provider 基础状态。

        :param api_key: API 密钥，可选。
        :param api_base: 自定义 API 基址，可选。
        """
        self.api_key = api_key
        self.api_base = api_base
        self.generation: GenerationSettings = GenerationSettings()

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清洗消息内容：修复空内容块、剥离内部 ``_meta`` 字段。

        - 空字符串 content：assistant 带 tool_calls 时置 None，否则置占位文本。
        - list content：移除空文本块与 ``_meta`` 字段；清洗后为空时按上述规则兜底。
        - dict content：包装为单元素 list。
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str) and not content:
                # 空字符串：assistant 带 tool_calls 时 content 应为 None，否则用占位符
                clean = dict(msg)
                clean["content"] = None if (msg.get("role") == "assistant" and msg.get("tool_calls")) else "(empty)"
                result.append(clean)
                continue

            if isinstance(content, list):
                new_items: list[Any] = []
                changed = False
                for item in content:
                    # 跳过空文本块（type 为 text/input_text/output_text 但 text 为空）
                    if (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    ):
                        changed = True
                        continue
                    # 剥离内部 _meta 字段，避免泄露给 Provider
                    if isinstance(item, dict) and "_meta" in item:
                        new_items.append({k: v for k, v in item.items() if k != "_meta"})
                        changed = True
                    else:
                        new_items.append(item)
                if changed:
                    clean = dict(msg)
                    if new_items:
                        clean["content"] = new_items
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean["content"] = None
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            if isinstance(content, dict):
                # 裸 dict content 包装为单元素 list
                clean = dict(msg)
                clean["content"] = [content]
                result.append(clean)
                continue

            result.append(msg)
        return result

    @staticmethod
    def _tool_name(tool: dict[str, Any]) -> str:
        """从 OpenAI 或 Anthropic 风格的工具定义中提取工具名。"""
        name = tool.get("name")
        if isinstance(name, str):
            return name
        fn = tool.get("function")
        if isinstance(fn, dict):
            fname = fn.get("name")
            if isinstance(fname, str):
                return fname
        return ""

    @classmethod
    def _tool_cache_marker_indices(cls, tools: list[dict[str, Any]]) -> list[int]:
        """返回需要插入缓存标记的工具索引：内置/MCP 边界索引与尾部索引。

        用于 Anthropic prompt 缓存：在内置工具与 MCP 工具的边界处设置缓存
        断点，使两段工具定义都能被缓存命中。
        """
        if not tools:
            return []

        tail_idx = len(tools) - 1
        # 从尾部向前找最后一个非 mcp_ 前缀的工具（即最后一个内置工具）
        last_builtin_idx: int | None = None
        for i in range(tail_idx, -1, -1):
            if not cls._tool_name(tools[i]).startswith("mcp_"):
                last_builtin_idx = i
                break

        # 去重保序地收集边界索引与尾部索引
        ordered_unique: list[int] = []
        for idx in (last_builtin_idx, tail_idx):
            if idx is not None and idx not in ordered_unique:
                ordered_unique.append(idx)
        return ordered_unique

    @staticmethod
    def _sanitize_request_messages(
        messages: list[dict[str, Any]],
        allowed_keys: frozenset[str],
    ) -> list[dict[str, Any]]:
        """只保留 Provider 安全的消息字段，并规范化 assistant 消息的 content。"""
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in allowed_keys}
            if clean.get("role") == "assistant" and "content" not in clean:
                # OpenAI 兼容协议要求 assistant 消息显式带 content 字段
                clean["content"] = None
            sanitized.append(clean)
        return sanitized

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """发送一次聊天补全请求（抽象方法，由具体 Provider 实现）。

        :param messages: 消息列表，每条包含 ``role`` 与 ``content``。
        :param tools: 可选的工具定义列表。
        :param model: 模型标识（Provider 特定）。
        :param max_tokens: 响应最大 token 数。
        :param temperature: 采样温度。
        :param tool_choice: 工具选择策略（``"auto"`` / ``"required"`` / 指定工具 dict）。

        :returns: 包含正文和/或工具调用的 :class:`LLMResponse`。
        """
        pass

    @classmethod
    def _is_transient_error(cls, content: str | None) -> bool:
        """通过文本标记判断是否为瞬时错误（兼容旧 Provider 文本兜底）。"""
        err = (content or "").lower()
        return any(marker in err for marker in cls._TRANSIENT_ERROR_MARKERS)

    @classmethod
    def _is_transient_response(cls, response: LLMResponse) -> bool:
        """判断响应是否对应瞬时错误（可重试）。

        优先使用结构化错误元数据（error_should_retry / error_status_code /
        error_kind），退化到文本标记（用于旧 Provider）。
        """
        if response.error_should_retry is not None:
            return bool(response.error_should_retry)

        if response.error_status_code is not None:
            status = int(response.error_status_code)
            if status == 429:
                # 429 需进一步区分限流（可重试）与配额（不可重试）
                return cls._is_retryable_429_response(response)
            if status in cls._RETRYABLE_STATUS_CODES or status >= 500:
                return True

        kind = (response.error_kind or "").strip().lower()
        if kind in cls._TRANSIENT_ERROR_KINDS:
            return True

        return cls._is_transient_error(response.content)

    @classmethod
    def is_arrearage_response(cls, response: LLMResponse) -> bool:
        """检测 API Key 欠费 / 配额 / 计费类错误（重试也无法恢复）。

        这些错误以 HTTP 402 或计费语义 token（如 ``insufficient_quota``、
        ``payment_required``）出现；复用 429 重试策略中视为不可重试的
        token 与文本标记。
        """
        if response.error_status_code is not None and int(response.error_status_code) == 402:
            return True

        type_token = cls._normalize_error_token(response.error_type)
        code_token = cls._normalize_error_token(response.error_code)
        if any(
            token in cls._NON_RETRYABLE_429_ERROR_TOKENS
            for token in (type_token, code_token)
            if token is not None
        ):
            return True

        content = (response.content or "").lower()
        return any(marker in content for marker in cls._NON_RETRYABLE_429_TEXT_MARKERS)

    @staticmethod
    def _normalize_error_token(value: Any) -> str | None:
        """将错误 token 规范化为小写字符串，空值返回 None。"""
        if value is None:
            return None
        token = str(value).strip().lower()
        return token or None

    @classmethod
    def _extract_error_type_code(cls, payload: Any) -> tuple[str | None, str | None]:
        """从错误载荷中提取语义化的 error_type 与 error_code。

        支持 dict 形式与 JSON 字符串形式，兼容顶层与 ``error`` 子对象中
        的 ``type`` / ``code`` 字段。
        """
        data: dict[str, Any] | None = None
        if isinstance(payload, dict):
            data = payload
        elif isinstance(payload, str):
            text = payload.strip()
            if text:
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed
        if not isinstance(data, dict):
            return None, None

        error_obj = data.get("error")
        type_value = data.get("type")
        code_value = data.get("code")
        if isinstance(error_obj, dict):
            # 优先取 error 子对象中的 type/code
            type_value = error_obj.get("type") or type_value
            code_value = error_obj.get("code") or code_value

        return cls._normalize_error_token(type_value), cls._normalize_error_token(code_value)

    @classmethod
    def _is_retryable_429_response(cls, response: LLMResponse) -> bool:
        """判断 429 响应是否可重试。

        先排除配额/计费类不可重试 token，再判断是否命中可重试的限流 token，
        未知 429 默认可重试（WATT+retry）。
        """
        type_token = cls._normalize_error_token(response.error_type)
        code_token = cls._normalize_error_token(response.error_code)
        semantic_tokens = {
            token for token in (type_token, code_token)
            if token is not None
        }
        # 配额/计费类：不可重试
        if any(token in cls._NON_RETRYABLE_429_ERROR_TOKENS for token in semantic_tokens):
            return False

        content = (response.content or "").lower()
        if any(marker in content for marker in cls._NON_RETRYABLE_429_TEXT_MARKERS):
            return False

        # 限流类：可重试
        if any(token in cls._RETRYABLE_429_ERROR_TOKENS for token in semantic_tokens):
            return True
        if any(marker in content for marker in cls._RETRYABLE_429_TEXT_MARKERS):
            return True
        # 未知 429 默认采用等待+重试策略
        return True

    @staticmethod
    def _enforce_role_alternation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """合并连续同角色消息并丢弃尾部 assistant 消息。

        部分 Provider（OpenAI 兼容、Azure、vLLM、Ollama 等）会拒绝最后一条
        为 ``assistant``（不支持 prefill）或连续两条非系统消息同角色的请求。
        """
        if not messages:
            return messages

        merged: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            # 仅对 user/assistant 的连续同角色做合并
            if (
                merged
                and role != "system"
                and role not in ("tool",)
                and merged[-1].get("role") == role
                and role in ("user", "assistant")
            ):
                prev = merged[-1]
                if role == "assistant":
                    prev_has_tools = bool(prev.get("tool_calls"))
                    curr_has_tools = bool(msg.get("tool_calls"))
                    # 当前后者带 tool_calls：用后者替换前者
                    if curr_has_tools:
                        merged[-1] = dict(msg)
                        continue
                    # 前者带 tool_calls：保留前者，丢弃后者
                    if prev_has_tools:
                        continue
                # 拼接纯文本 content
                prev_content = prev.get("content") or ""
                curr_content = msg.get("content") or ""
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    prev["content"] = (prev_content + "\n\n" + curr_content).strip()
                else:
                    merged[-1] = dict(msg)
            else:
                merged.append(dict(msg))

        # 剥离尾部的 assistant 消息（多数 Provider 不支持 prefill）
        last_popped = None
        while merged and merged[-1].get("role") == "assistant":
            last_popped = merged.pop()

        # 若剥离后只剩 system 消息，请求对多数 Provider 非法（如智谱/GLM
        # 错误 1214）。恢复策略：把最后弹出的 assistant 转为 user，使 LLM
        # 仍能看到内容。
        if (
            merged
            and last_popped is not None
            and not any(m.get("role") in ("user", "tool") for m in merged)
        ):
            recovered = dict(last_popped)
            recovered["role"] = "user"
            merged.append(recovered)

        # 安全网：确保首条非系统消息不是裸 assistant。GLM 等会拒绝
        # system→assistant（错误 1214）。这种情况可能发生在上游截断
        # （如 _snip_history）丢掉了唯一的 user 消息时，这里插入一条
        # 合成 user 消息保持序列合法。
        for i, msg in enumerate(merged):
            if msg.get("role") != "system":
                if msg.get("role") == "assistant" and not msg.get("tool_calls"):
                    merged.insert(i, {"role": "user", "content": _SYNTHETIC_USER_CONTENT})
                break

        return merged

    @staticmethod
    def _strip_image_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """将 image_url 块替换为文本占位符；未发现图片时返回 None。"""
        found = False
        result = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        path = (b.get("_meta") or {}).get("path", "")
                        placeholder = image_placeholder_text(path, empty="[image omitted]")
                        new_content.append({"type": "text", "text": placeholder})
                        found = True
                    else:
                        new_content.append(b)
                result.append({**msg, "content": new_content})
            else:
                result.append(msg)
        return result if found else None

    @staticmethod
    def _strip_image_content_inplace(messages: list[dict[str, Any]]) -> bool:
        """将 image_url 块就地替换为文本占位符。

        直接修改原始消息 dict 的 content 列表，使持有该 dict 引用的调用方
        也能看到剥离后的版本。
        """
        found = False
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for i, b in enumerate(content):
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        path = (b.get("_meta") or {}).get("path", "")
                        placeholder = image_placeholder_text(path, empty="[image omitted]")
                        content[i] = {"type": "text", "text": placeholder}
                        found = True
        return found

    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        """调用 chat() 并将未预期异常转换为错误响应。"""
        try:
            return await self.chat(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """流式聊天补全，对每个文本块调用 *on_content_delta*。

        *on_thinking_delta* 保留给在协议上暴露增量思考/推理的 Provider；
        默认回退实现对原生增量不触发任何回调（仅在 :meth:`chat` 之后触发
        可选的单次 *on_content_delta*）。

        返回与 :meth:`chat` 相同的 :class:`LLMResponse`。默认实现回退到
        非流式调用，并将完整正文作为单个 delta 投递。支持原生流式的
        Provider 应覆写本方法。
        """
        _ = on_thinking_delta, on_tool_call_delta
        response = await self.chat(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
        )
        if on_content_delta and response.content:
            await on_content_delta(response.content)
        return response

    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        """调用 chat_stream() 并将未预期异常转换为错误响应。"""
        try:
            return await self.chat_stream(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_stream_recover: Callable[[], Awaitable[None]] | None = None,
        retry_mode: str = "standard",
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """调用 chat_stream() 并在瞬时 Provider 失败时重试。

        通过 *should_retry_guard* 跟踪是否已流式输出正文：已输出则默认不
        再重试（避免重复输出），但超时场景下可通过 *on_stream_recover*
        在新流式段中继续。
        """
        # 未显式传参时回退到 generation 默认值
        if max_tokens is self._SENTINEL or max_tokens is None:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL or temperature is None:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        has_streamed_content = False  # 跟踪是否已向调用方投递过正文

        async def _tracking_delta(text: str) -> None:
            """包装 on_content_delta，同时记录是否已输出正文。"""
            nonlocal has_streamed_content
            if text:
                has_streamed_content = True
            if on_content_delta:
                await on_content_delta(text)

        async def _recover_stream() -> None:
            """流式段恢复：通知调用方关闭当前段并重置已输出标记。"""
            nonlocal has_streamed_content
            if on_stream_recover:
                await on_stream_recover()
            has_streamed_content = False

        kw: dict[str, Any] = dict(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
            on_content_delta=_tracking_delta if on_content_delta is not None else None,
            on_thinking_delta=on_thinking_delta,
            on_tool_call_delta=on_tool_call_delta,
        )
        if on_stream_recover and getattr(self, "supports_stream_recover_callback", False):
            kw["on_stream_recover"] = _recover_stream
        return await self._run_with_retry(
            self._safe_chat_stream,
            kw,
            messages,
            retry_mode=retry_mode,
            on_retry_wait=on_retry_wait,
            should_retry_guard=lambda: not has_streamed_content,
            on_stream_recover=_recover_stream if on_stream_recover else None,
        )

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        retry_mode: str = "standard",
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """调用 chat() 并在瞬时 Provider 失败时重试。

        未显式传参时参数默认取 ``self.generation``，使调用方无需逐层透传
        temperature / max_tokens / reasoning_effort。显式 ``None`` 也会被
        归一化为 Provider 的 generation 默认值，避免下游 ``_build_kwargs``
        见到 ``None`` 的 max_tokens / temperature（会使
        ``max(1, max_tokens)`` 崩溃）。
        """
        if max_tokens is self._SENTINEL or max_tokens is None:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL or temperature is None:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        kw: dict[str, Any] = dict(
            messages=messages, tools=tools, model=model,
            max_tokens=max_tokens, temperature=temperature,
            reasoning_effort=reasoning_effort, tool_choice=tool_choice,
        )
        return await self._run_with_retry(
            self._safe_chat,
            kw,
            messages,
            retry_mode=retry_mode,
            on_retry_wait=on_retry_wait,
        )

    @classmethod
    def _extract_retry_after(cls, content: str | None) -> float | None:
        """从错误文本中解析 Retry-After 等待秒数。

        支持多种文案变体（retry after / try again in / wait ... before retry /
        retry_after 字段形式），并解析可选的时间单位。
        """
        text = (content or "").lower()
        patterns = (
            r"retry after\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)?",
            r"try again in\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)",
            r"wait\s+(\d+(?:\.\d+)?)\s*(ms|milliseconds|s|sec|secs|seconds|m|min|minutes)\s*before retry",
            r"retry[_-]?after[\"'\s:=]+(\d+(?:\.\d+)?)",
        )
        for idx, pattern in enumerate(patterns):
            match = re.search(pattern, text)
            if not match:
                continue
            value = float(match.group(1))
            # 第 4 个模式无单位捕获组，默认按秒处理
            unit = match.group(2) if idx < 3 else "s"
            return cls._to_retry_seconds(value, unit)
        return None

    @classmethod
    def _to_retry_seconds(cls, value: float, unit: str | None = None) -> float:
        """将给定值与单位转换为秒，最小不低于 0.1 秒。"""
        normalized_unit = (unit or "s").lower()
        if normalized_unit in {"ms", "milliseconds"}:
            return max(0.1, value / 1000.0)
        if normalized_unit in {"m", "min", "minutes"}:
            return max(0.1, value * 60.0)
        return max(0.1, value)

    @classmethod
    def _extract_retry_after_from_headers(cls, headers: Any) -> float | None:
        """从 HTTP 响应头中解析 Retry-After 等待秒数。

        优先解析 ``Retry-After-Ms``（毫秒），其次解析 ``Retry-After``
        （数字秒数或 HTTP 日期）。
        """
        if not headers:
            return None

        def _header_value(name: str) -> Any:
            """兼容 Mapping.get 与 dict 大小写不敏感查找。"""
            if hasattr(headers, "get"):
                value = headers.get(name) or headers.get(name.title())
                if value is not None:
                    return value
            if isinstance(headers, dict):
                for key, value in headers.items():
                    if isinstance(key, str) and key.lower() == name.lower():
                        return value
            return None

        # 优先解析 retry-after-ms（毫秒精度）
        with suppress(TypeError, ValueError):
            retry_ms = _header_value("retry-after-ms")
            if retry_ms is not None:
                value = float(retry_ms) / 1000.0
                if value > 0:
                    return value

        retry_after = _header_value("retry-after")
        if retry_after is None:
            return None
        retry_after_text = str(retry_after).strip()
        if not retry_after_text:
            return None
        # 数字形式：按秒解析
        if re.fullmatch(r"\d+(?:\.\d+)?", retry_after_text):
            return cls._to_retry_seconds(float(retry_after_text), "s")
        # HTTP 日期形式：计算剩余秒数
        try:
            retry_at = parsedate_to_datetime(retry_after_text)
        except Exception:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        remaining = (retry_at - datetime.now(retry_at.tzinfo)).total_seconds()
        return max(0.1, remaining)

    @classmethod
    def _extract_retry_after_from_response(cls, response: LLMResponse) -> float | None:
        """从响应中综合提取重试等待秒数。

        优先级：结构化 ``error_retry_after_s`` > ``retry_after`` > 文本解析。
        """
        if response.error_retry_after_s is not None and response.error_retry_after_s > 0:
            return response.error_retry_after_s
        if response.retry_after is not None and response.retry_after > 0:
            return response.retry_after
        return cls._extract_retry_after(response.content)

    async def _sleep_with_heartbeat(
        self,
        delay: float,
        *,
        attempt: int,
        persistent: bool,
        on_retry_wait: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """带心跳回调的异步休眠。

        将总延迟按 ``_RETRY_HEARTBEAT_CHUNK`` 分片，每个分片前向调用方
        投递一次等待进度，避免长延迟期间调用方失去反馈。
        """
        remaining = max(0.0, delay)
        while remaining > 0:
            if on_retry_wait:
                kind = "persistent retry" if persistent else "retry"
                await on_retry_wait(
                    f"Model request failed, {kind} in {max(1, int(round(remaining)))}s "
                    f"(attempt {attempt})."
                )
            chunk = min(remaining, self._RETRY_HEARTBEAT_CHUNK)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def _run_with_retry(
        self,
        call: Callable[..., Awaitable[LLMResponse]],
        kw: dict[str, Any],
        original_messages: list[dict[str, Any]],
        *,
        retry_mode: str,
        on_retry_wait: Callable[[str], Awaitable[None]] | None,
        should_retry_guard: Callable[[], bool] | None = None,
        on_stream_recover: Callable[[], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """统一的带重试调用编排。

        支持 ``standard``（固定退避序列）与 ``persistent``（持续重试）两种
        模式；处理流式已输出后的重试抑制、超时恢复、相同错误累积上限、
        非瞬时错误下剥离图片重试等策略。
        """
        attempt = 0
        delays = list(self._CHAT_RETRY_DELAYS)
        persistent = retry_mode == "persistent"
        last_response: LLMResponse | None = None
        last_error_key: str | None = None
        identical_error_count = 0
        while True:
            attempt += 1
            response = await call(**kw)
            if response.finish_reason != "error":
                return response
            last_response = response
            # 流式已输出正文后的重试守卫
            if should_retry_guard is not None and not should_retry_guard():
                is_timeout = (response.error_kind or "").lower() == "timeout"
                if is_timeout:
                    # 超时：可在新流式段中继续
                    if on_stream_recover:
                        logger.warning(
                            "LLM stream stalled after content was emitted; "
                            "starting a new stream segment and retrying"
                        )
                        await on_stream_recover()
                    else:
                        # 无恢复回调：抑制增量回调后继续重试，避免重复输出
                        logger.warning(
                            "LLM stream stalled after content was emitted; "
                            "suppressing delta callbacks and retrying"
                        )
                        kw.setdefault("on_content_delta", None)
                        kw["on_content_delta"] = None
                        kw["on_thinking_delta"] = None
                        kw["on_tool_call_delta"] = None
                        should_retry_guard = None
                else:
                    # 非超时错误且已输出正文：跳过重试，避免重复输出
                    logger.warning(
                        "LLM stream failed after content was emitted; skipping retry"
                    )
                    return response
            # 统计相同错误连续出现次数（用于持久重试的上限判断）
            error_key = ((response.content or "").strip().lower() or None)
            if error_key and error_key == last_error_key:
                identical_error_count += 1
            else:
                last_error_key = error_key
                identical_error_count = 1 if error_key else 0

            if not self._is_transient_response(response):
                # 非瞬时错误：若消息含图片，尝试剥离图片后重试一次
                stripped = self._strip_image_content(original_messages)
                if stripped is not None and stripped != kw["messages"]:
                    logger.warning(
                        "Non-transient LLM error with image content, retrying without images"
                    )
                    retry_kw = dict(kw)
                    retry_kw["messages"] = stripped
                    result = await call(**retry_kw)
                    # 成功则永久剥离原始消息中的图片，避免后续重复触发错误-重试循环
                    if result.finish_reason != "error":
                        self._strip_image_content_inplace(original_messages)
                    return result
                return response

            # 持久重试：相同瞬时错误达到上限则停止
            if persistent and identical_error_count >= self._PERSISTENT_IDENTICAL_ERROR_LIMIT:
                logger.warning(
                    "Stopping persistent retry after {} identical transient errors: {}",
                    identical_error_count,
                    (response.content or "")[:120].lower(),
                )
                if on_retry_wait:
                    await on_retry_wait(
                        f"Persistent retry stopped after {identical_error_count} identical errors."
                    )
                return response

            # 标准重试：超过退避序列长度则放弃
            if not persistent and attempt > len(delays):
                logger.warning(
                    "LLM request failed after {} retries, giving up: {}",
                    attempt,
                    (response.content or "")[:120].lower(),
                )
                if on_retry_wait:
                    await on_retry_wait(
                        f"Model request failed after {attempt} retries, giving up."
                    )
                break

            # 计算本次延迟：优先用响应中的 Retry-After，否则用退避序列
            base_delay = delays[min(attempt - 1, len(delays) - 1)]
            delay = self._extract_retry_after_from_response(response) or base_delay
            if persistent:
                delay = min(delay, self._PERSISTENT_MAX_DELAY)

            logger.warning(
                "LLM transient error (attempt {}{}), retrying in {}s: {}",
                attempt,
                "+" if persistent and attempt > len(delays) else f"/{len(delays)}",
                int(round(delay)),
                (response.content or "")[:120].lower(),
            )
            await self._sleep_with_heartbeat(
                delay,
                attempt=attempt,
                persistent=persistent,
                on_retry_wait=on_retry_wait,
            )

        return last_response if last_response is not None else await call(**kw)

    @abstractmethod
    def get_default_model(self) -> str:
        """获取该 Provider 的默认模型名（抽象方法，由具体 Provider 实现）。"""
        pass
