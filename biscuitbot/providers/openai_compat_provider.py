"""OpenAI 兼容 Provider——所有非 Anthropic LLM API 的统一实现。

本模块是 biscuitbot 对接各家 LLM 服务（OpenAI、DeepSeek、阿里通义、智谱、
月之暗面、阶跃星辰、Ollama 等）的核心适配层：对外暴露统一的 chat/chat_stream 接口，
对内处理消息净化、思考模式注入、工具调用规范化、Responses API 熔断与回退等差异。
"""

from __future__ import annotations

import asyncio  # 锁与超时控制
import hashlib  # 工具调用 ID 归一化（sha1 截断）
import importlib.util  # 探测可选依赖（langfuse）
import json  # JSON 序列化与深拷贝辅助
import os  # 环境变量读写
import secrets  # 生成安全的短工具 ID
import string  # 字符集（alnum）
import time  # 单调时钟（熔断计时）
import uuid  # 会话亲和性 header
from collections import deque  # 工具 ID 队列（先进先出映射）
from collections.abc import Awaitable, Callable  # 回调类型标注
from ipaddress import ip_address  # 本地/局域网端点判定
from typing import TYPE_CHECKING, Any  # 动态类型与仅类型检查期导入
from urllib.parse import urlparse  # URL 解析

from loguru import logger  # 结构化日志
from pydantic.alias_generators import to_snake  # 名称转 snake_case

from biscuitbot.providers.base import (  # 抽象基类与共享数据结构
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    parse_tool_arguments,
    resolve_stream_idle_timeout_s,
    tool_arguments_json_for_replay,
)
from biscuitbot.providers.openai_responses import (  # Responses API 转换与解析工具
    consume_sdk_stream,
    convert_messages,
    convert_tools,
    parse_response_output,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI as AsyncOpenAIType  # 仅用于类型标注

    from biscuitbot.providers.registry import ProviderSpec  # Provider 元数据

# 模块级占位符——首次真正调用时由 _ensure_client 惰性设置，
# 或被测试通过 ``patch(...)`` 替换。保留为普通名以便 ``unittest.mock.patch`` 能找到并替换。
AsyncOpenAI: Any = None

# 消息允许保留的字段白名单，其余非标准字段会被净化掉
_ALLOWED_MSG_KEYS = frozenset({
    "role", "content", "tool_calls", "tool_call_id", "name",
    "reasoning_content", "extra_content",
})
# 字母+数字字符集，用于生成短工具 ID
_ALNUM = string.ascii_letters + string.digits

# 工具调用对象的标准字段（其余视为 provider_specific_fields）
_STANDARD_TC_KEYS = frozenset({"id", "type", "index", "function"})
# function 对象的标准字段
_STANDARD_FN_KEYS = frozenset({"name", "arguments"})
# OpenRouter 归因 header，默认加到 OpenRouter 请求上
_DEFAULT_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/HKUDS/biscuitbot",
    "X-OpenRouter-Title": "biscuitbot",
    "X-OpenRouter-Categories": "cli-agent,personal-agent",
}
# Kimi 支持思考模式的模型集合
_KIMI_THINKING_MODELS: frozenset[str] = frozenset({
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7",
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
    "k2.6-code-preview",
})
# 即使关闭思考也必须保留思考参数的 Kimi 模型（代码模型始终思考）
_KIMI_ALWAYS_THINKING_MODELS: frozenset[str] = frozenset({
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
})
# 支持思考的小米 MiMo 模型（见 tests/providers/test_xiaomi_mimo_thinking.py）。
# mimo-v2-flash 不支持思考，故未列入。
_MIMO_THINKING_MODELS: frozenset[str] = frozenset({
    "mimo-v2.5-pro",
    "mimo-v2.5",
    "mimo-v2-pro",
    "mimo-v2-omni",
})
# DeepSeek 视觉模型子串匹配：命中（如 deepseek-v4-flash-vision-exp）即保留
# image_url 内容块；其余 DeepSeek 模型只接受字符串 content，强转时丢弃图像块。
_DEEPSEEK_VISION_MODEL_MARKERS: tuple[str, ...] = ("vision",)

# OpenAI 兼容请求默认超时（秒）
_OPENAI_COMPAT_REQUEST_TIMEOUT_S = 120.0

# ProviderSpec.thinking_style → extra_body 构造器映射表。
# 每个构造器接收一个 bool（是否开启思考），返回要合并进 extra_body 的 dict，
# 把"风格→线格式"映射集中在一处维护。
_THINKING_STYLE_MAP: dict[str, Any] = {
    "thinking_type": lambda on: {"thinking": {"type": "enabled" if on else "disabled"}},
    "enable_thinking": lambda on: {"enable_thinking": on},
    "reasoning_split": lambda on: {"reasoning_split": on},
}
# 模型名 → 思考风格映射（Kimi 与 MiMo 系列均用 thinking_type）
_MODEL_THINKING_STYLES: dict[str, str] = {
    **dict.fromkeys(_KIMI_THINKING_MODELS, "thinking_type"),
    **dict.fromkeys(_MIMO_THINKING_MODELS, "thinking_type"),
}


def _model_slug(model_name: str) -> str:
    """取模型名最后一段并转小写，用于匹配模型级风格/限制（忽略 provider 前缀）。"""
    return model_name.lower().rsplit("/", 1)[-1]


def _provider_prefix_key(name: str) -> str:
    """把 provider 前缀归一化为 snake_case 小写形式，便于在配置前缀表中匹配。"""
    return to_snake(name.replace("-", "_")).lower()


def _requires_max_completion_tokens(model_name: str) -> bool:
    """判断模型是否只接受 ``max_completion_tokens`` 而拒绝 ``max_tokens``。

    GPT-5 系列与 o 系列（o1/o3/o4）推理模型不再支持旧字段 ``max_tokens``，
    必须改用 ``max_completion_tokens``，否则会 400。
    """
    slug = _model_slug(model_name)
    return "gpt-5" in slug or any(
        slug == p or slug.startswith((p + "-", p + ".")) for p in ("o1", "o3", "o4")
    )


def _model_thinking_style(model_name: str) -> str:
    """返回模型对应的思考风格键（如 ``"thinking_type"``），未配置则返回空串。"""
    return _MODEL_THINKING_STYLES.get(_model_slug(model_name), "")


def _thinking_styles_for(spec: ProviderSpec | None, model_name: str) -> list[str]:
    """合并 spec 级与模型级的思考风格，去重后返回列表。

    spec 级风格优先，模型级风格补齐；两者都有的情况只保留一份。
    """
    styles: list[str] = []
    if spec and spec.thinking_style:
        styles.append(spec.thinking_style)
    model_style = _model_thinking_style(model_name)
    if model_style and model_style not in styles:
        styles.append(model_style)
    return styles


def _thinking_extra_body(style: str, thinking_enabled: bool) -> dict[str, Any] | None:
    """按思考风格构造 ``extra_body`` 片段（如 ``{"thinking": {"type": "enabled"}}``）。

    风格未注册时返回 ``None``，由调用方跳过合并。
    """
    builder = _THINKING_STYLE_MAP.get(style)
    return builder(thinking_enabled) if builder else None


def _openai_compat_timeout_s() -> float:
    """读取 OpenAI 兼容请求超时（秒），可由环境变量覆盖。"""
    return _float_env("BISCUITBOT_OPENAI_COMPAT_TIMEOUT_S", _OPENAI_COMPAT_REQUEST_TIMEOUT_S)


def _float_env(name: str, default: float) -> float:
    """从环境变量读取浮点数；缺失/非法/非正数时回退到默认值并告警。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid {}={!r}; using {}", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive {}={!r}; using {}", name, raw, default)
        return default
    return value


def _short_tool_id() -> str:
    """生成 9 位字母数字工具 ID，兼容所有 provider（含 Mistral）。"""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


def _get(obj: Any, key: str) -> Any:
    """从 dict 或对象属性取值，键不存在时返回 ``None``。

    用于兼容原始 JSON dict 与 SDK Pydantic 对象两种响应形态。
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _coerce_dict(value: Any) -> dict[str, Any] | None:
    """把任意值尽量转成 dict；无法转换或为空则返回 ``None``。

    支持 dict 原样返回、SDK 对象通过 ``model_dump()`` 转换。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value if value else None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict) and dumped:
            return dumped
    return None


def _extract_tc_extras(tc: Any) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """提取工具调用携带的扩展字段，返回三元组。

    返回 ``(extra_content, provider_specific_fields, fn_provider_specific_fields)``：
    - ``extra_content``：Gemini 等 provider 在工具调用上附带的额外内容，原样保留；
    - ``provider_specific_fields``：工具调用对象上非标准字段（除 id/type/index/function）；
    - ``fn_provider_specific_fields``：function 子对象上非标准字段（除 name/arguments）。

    同时兼容 SDK 对象与 dict：优先用 ``model_dump()``/dict 形态提取，
    回退到 ``provider_specific_fields`` 属性。
    """
    extra_content = _coerce_dict(_get(tc, "extra_content"))

    tc_dict = _coerce_dict(tc)
    prov = None
    fn_prov = None
    if tc_dict is not None:
        # dict 形态：收集非标准字段为 provider_specific_fields
        leftover = {k: v for k, v in tc_dict.items()
                    if k not in _STANDARD_TC_KEYS and k != "extra_content" and v is not None}
        if leftover:
            prov = leftover
        fn = _coerce_dict(tc_dict.get("function"))
        if fn is not None:
            # function 子对象同样收集非标准字段
            fn_leftover = {k: v for k, v in fn.items()
                          if k not in _STANDARD_FN_KEYS and v is not None}
            if fn_leftover:
                fn_prov = fn_leftover
    else:
        # SDK 对象形态：直接读取 provider_specific_fields 属性
        prov = _coerce_dict(_get(tc, "provider_specific_fields"))
        fn_obj = _get(tc, "function")
        if fn_obj is not None:
            fn_prov = _coerce_dict(_get(fn_obj, "provider_specific_fields"))

    return extra_content, prov, fn_prov


def _uses_openrouter_attribution(spec: "ProviderSpec | None", api_base: str | None) -> bool:
    """判断是否需要为请求附加 biscuitbot 归因 header（OpenRouter 默认附加）。

    spec 名为 ``openrouter`` 或 api_base 中包含 ``openrouter`` 字样时返回 ``True``。
    """
    if spec and spec.name == "openrouter":
        return True
    return bool(api_base and "openrouter" in api_base.lower())


# Responses API 熔断阈值：连续失败次数达到该值即打开熔断器
_RESPONSES_FAILURE_THRESHOLD = 3
# 熔断打开后的探测间隔（秒）：超过该时长后放行一次半开探测请求
_RESPONSES_PROBE_INTERVAL_S = 300  # 5 minutes


def _is_local_endpoint(
    spec: "ProviderSpec | None",
    api_base: str | None,
) -> bool:
    """判断端点是否为本地或局域网模型服务。

    命中条件：
    - spec 显式标记 ``is_local``；或
    - api_base 主机名为 ``localhost`` / ``host.docker.internal``；
    - 或主机 IP 属于回环/私有网段（127.x、10.x、192.168.x、172.16-31.x）。
    """
    if spec and spec.is_local:
        return True
    if not api_base:
        return False
    raw = api_base.strip().lower()
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    try:
        host = parsed.hostname
    except ValueError:
        return False
    if host in {"localhost", "host.docker.internal"}:
        return True
    if not host:
        return False
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def _is_direct_openai_base(api_base: str | None) -> bool:
    """判断是否为 OpenAI 官方端点（非 OpenRouter 等兼容网关）。

    无 api_base 视为官方端点；含 ``api.openai.com`` 且不含 ``openrouter`` 才为 ``True``。
    """
    if not api_base:
        return True
    normalized = api_base.strip().lower().rstrip("/")
    return "api.openai.com" in normalized and "openrouter" not in normalized


def _responses_circuit_key(
    model: str | None,
    default_model: str,
    reasoning_effort: str | None,
) -> str:
    """生成 Responses API 熔断器的键：``模型名:推理强度``。

    同一模型同一推理强度共享一个熔断状态，避免不同配置互相干扰。
    """
    model_name = (model or default_model).lower()
    effort = reasoning_effort.lower() if isinstance(reasoning_effort, str) else ""
    return f"{model_name}:{effort}"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并 ``override`` 到 ``base``，返回新 dict（不修改入参）。

    嵌套 dict 逐键合并；其余类型由 ``override`` 直接覆盖 ``base`` 对应键。
    用于合并用户配置的 ``extra_body`` 与默认值，避免兄弟键被误覆盖。
    """
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_unique_list(base: Any, override: Any) -> Any:
    """合并两个列表，保留顺序并去重（基于 JSON 序列化比较）。"""
    if not isinstance(base, list) or not isinstance(override, list):
        return override
    result: list[Any] = []
    seen: set[str] = set()
    for value in [*base, *override]:
        try:
            key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        except Exception:
            key = repr(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _merge_responses_extra_body(
    body: dict[str, Any],
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    """合并用户配置的 Responses API body 字段，避免覆盖 tools/include。

    ``include`` 与 ``tools`` 是保留字段，需要特殊处理：
    - ``include`` 用 ``_merge_unique_list`` 合并去重；
    - ``tools`` 在双方都是列表时拼接，否则由配置覆盖；
    - 其余字段走 ``_deep_merge`` 递归合并。
    """
    reserved = {"include", "tools"}
    regular_extra = {key: value for key, value in extra_body.items() if key not in reserved}
    merged = _deep_merge(body, regular_extra)

    if "include" in extra_body:
        merged["include"] = _merge_unique_list(body.get("include"), extra_body["include"])

    if "tools" in extra_body:
        current_tools = body.get("tools")
        configured_tools = extra_body["tools"]
        if isinstance(current_tools, list) and isinstance(configured_tools, list):
            merged["tools"] = [*current_tools, *configured_tools]
        else:
            merged["tools"] = configured_tools

    return merged


class OpenAICompatProvider(LLMProvider):
    """所有 OpenAI 兼容 API 的统一 Provider 实现。

    由调用方传入已解析的 ``ProviderSpec``，内部不再做注册表查找。
    负责构造请求、净化消息、归一化工具调用、Responses API 熔断与回退、
    以及流式/非流式响应解析。
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "gpt-4o",
        extra_headers: dict[str, str] | None = None,
        spec: ProviderSpec | None = None,
        extra_body: dict[str, Any] | None = None,
        api_type: str = "auto",
        extra_query: dict[str, str] | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self._spec = spec
        self._extra_body = extra_body or {}
        # api_type 仅对 openai 官方 spec 生效（responses/chat_completions/auto）
        self._api_type = api_type if spec and spec.name == "openai" else "auto"
        self._extra_query = extra_query or {}

        if api_key and spec and spec.env_key:
            self._setup_env(api_key, api_base)

        # 计算实际生效的 base_url：参数 > spec 默认值
        effective_base = api_base or (spec.default_api_base if spec else None) or None
        self._effective_base = effective_base
        # 默认 header：会话亲和性 ID（同一 client 的请求尽量落到同一后端）
        self._default_headers = {"x-session-affinity": uuid.uuid4().hex}
        if _uses_openrouter_attribution(spec, effective_base):
            self._default_headers.update(_DEFAULT_OPENROUTER_HEADERS)
        if extra_headers:
            self._default_headers.update(extra_headers)
        # 无 key 时用占位串，避免 SDK 报错（本地模型常见）
        self._api_key_for_client = api_key or "no-key"
        self._is_local = _is_local_endpoint(spec, effective_base)

        # 惰性初始化：OpenAI client 与其 httpx transport 构造代价较高
        # （Windows 上约 700ms），延迟到首次调用时再创建。
        self._client: AsyncOpenAIType | None = None
        self._client_lock = asyncio.Lock()

        # Responses API 熔断器：连续失败后跳过，超过 _RESPONSES_PROBE_INTERVAL_S 后再探测一次
        self._responses_failures: dict[str, int] = {}
        self._responses_tripped_at: dict[str, float] = {}

    def _build_client(self) -> None:
        """使用当前模块级 AsyncOpenAI 构造底层 SDK 客户端。"""
        import httpx

        timeout_s = _openai_compat_timeout_s()
        http_client: httpx.AsyncClient | None = None
        if self._is_local:
            # 本地模型服务（Ollama、llama.cpp、vLLM）常在客户端 keepalive 到期前
            # 就关闭空闲连接。两次 LLM 调用间隔几秒（如心跳 _decide 后紧跟
            # process_direct）时，第二次会拿到已死的连接池连接，每次首请求都会
            # 触发瞬时 APIConnectionError。对本地端点禁用 keepalive 可以规避该问题：
            # 每次请求新建连接，局域网内代价很低。云端 provider 受益于 keepalive，
            # 因此保留默认连接池设置。
            http_client = httpx.AsyncClient(
                limits=httpx.Limits(keepalive_expiry=0),
                timeout=timeout_s,
            )
        self._client = AsyncOpenAI(
            api_key=self._api_key_for_client,
            base_url=self._effective_base,
            default_headers=self._default_headers,
            default_query=self._extra_query or None,
            max_retries=0,  # 重试由上层 LLMProvider 统一处理
            timeout=timeout_s,
            http_client=http_client,
        )

    async def _ensure_client(self):
        """返回共享的 OpenAI 客户端，首次调用时惰性创建。

        加锁防止并发首次调用重复构造；模块级 AsyncOpenAI 占位符在首次使用时
        解析为真实类（langfuse 包装版或原生 openai 版）。
        """
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            global AsyncOpenAI
            if AsyncOpenAI is None:
                # 优先使用 langfuse 包装版（若配置了密钥且已安装）
                if os.environ.get("LANGFUSE_SECRET_KEY") and importlib.util.find_spec("langfuse"):
                    from langfuse.openai import AsyncOpenAI as _AsyncOpenAI
                else:
                    if os.environ.get("LANGFUSE_SECRET_KEY"):
                        logger.warning(
                            "LANGFUSE_SECRET_KEY is set but langfuse is not installed; "
                            "install with `pip install langfuse` to enable tracing"
                        )
                    from openai import AsyncOpenAI as _AsyncOpenAI
                AsyncOpenAI = _AsyncOpenAI

            self._build_client()
            return self._client

    def _setup_env(self, api_key: str, api_base: str | None) -> None:
        """根据 provider spec 写入对应环境变量（SDK 内部会读取）。"""
        spec = self._spec
        if not spec or not spec.env_key:
            return
        if spec.is_gateway:
            # 网关型 spec：直接覆盖，确保切换 provider 时生效
            os.environ[spec.env_key] = api_key
        else:
            # 普通 spec：仅在未设置时回填，避免覆盖用户已有环境变量
            os.environ.setdefault(spec.env_key, api_key)
        effective_base = api_base or spec.default_api_base
        # 渲染 spec.env_extras 中的占位符（{api_key}/{api_base}）
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key).replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    @classmethod
    def _apply_cache_control(
        cls,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """为支持 prompt caching 的 provider 注入 ``cache_control`` 标记。

        标记位置：系统消息、倒数第二条消息、以及部分工具——
        让 provider 把这些稳定前缀缓存起来，降低重复请求的 token 成本。
        """
        cache_marker = {"type": "ephemeral"}
        new_messages = list(messages)

        def _mark(msg: dict[str, Any]) -> dict[str, Any]:
            # 给消息 content 末尾追加 cache_control 标记
            content = msg.get("content")
            if isinstance(content, str):
                return {**msg, "content": [
                    {"type": "text", "text": content, "cache_control": cache_marker},
                ]}
            if isinstance(content, list) and content:
                nc = list(content)
                nc[-1] = {**nc[-1], "cache_control": cache_marker}
                return {**msg, "content": nc}
            return msg

        # 标记系统消息（如果存在）
        if new_messages and new_messages[0].get("role") == "system":
            new_messages[0] = _mark(new_messages[0])
        # 标记倒数第二条消息（通常是最后一条用户消息前的前缀）
        if len(new_messages) >= 3:
            new_messages[-2] = _mark(new_messages[-2])

        # 标记部分工具（具体索引由 _tool_cache_marker_indices 决定）
        new_tools = tools
        if tools:
            new_tools = list(tools)
            for idx in cls._tool_cache_marker_indices(new_tools):
                new_tools[idx] = {**new_tools[idx], "cache_control": cache_marker}
        return new_messages, new_tools

    @staticmethod
    def _normalize_tool_call_id(tool_call_id: Any) -> Any:
        """把工具调用 ID 归一化为 provider 安全的 9 位字母数字形式。

        Mistral 等 provider 拒绝 OpenAI 标准 ``call_xxx`` 形式的 ID，
        需要用 sha1 截断成 9 位字母数字。
        """
        if not isinstance(tool_call_id, str):
            return tool_call_id
        if len(tool_call_id) == 9 and tool_call_id.isalnum():
            return tool_call_id
        return hashlib.sha1(tool_call_id.encode()).hexdigest()[:9]

    def _should_normalize_tool_call_ids(self) -> bool:
        """判断是否需要归一化工具调用 ID（Mistral 需要）。"""
        return bool(self._spec and self._spec.name == "mistral")

    @staticmethod
    def _coerce_content_to_string(content: Any) -> str | None:
        """把块/列表形式的 content 强转为纯文本，适配只接受字符串的 API（如 DeepSeek）。"""
        if content is None or isinstance(content, str):
            return content
        text = OpenAICompatProvider._extract_text_content(content)
        if isinstance(text, str) and text:
            return text
        # 无法提取文本时退化为 JSON 字符串，避免 None 被拒绝
        try:
            dumped = json.dumps(content, ensure_ascii=False)
        except Exception:
            dumped = str(content)
        return dumped or "(empty)"

    def _deepseek_supports_vision(self, model_name: str | None) -> bool:
        """判断当前 DeepSeek 模型是否支持视觉输入。

        DeepSeek 只有视觉模型（如 ``deepseek-v4-flash-vision-exp``）接受 ``image_url``
        内容块；其余模型（``deepseek-chat``/``deepseek-reasoner`` 等）对 list content
        直接返回 400，需强转为纯文本字符串。命中 ``vision`` 标记即视为支持视觉。
        """
        if not self._spec or self._spec.name != "deepseek" or not model_name:
            return False
        name = model_name.lower()
        return any(marker in name for marker in _DEEPSEEK_VISION_MODEL_MARKERS)

    def _sanitize_messages(
        self,
        messages: list[dict[str, Any]],
        model_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """净化消息列表：剔除非标准字段、归一化工具调用 ID、强制字符串 content。

        主要工作：
        - 调用基类白名单过滤；
        - 对 Mistral 等需要归一化 ID 的 provider，做旧→新 ID 映射并应用到 tool_call_id；
        - DeepSeek 强制 content 为字符串（视觉模型除外，保留 image_url 块）；
        - 工具调用参数统一为可回放的 JSON 字符串；
        - 强制角色交替（assistant/tool/user 顺序合法）。
        """
        sanitized = LLMProvider._sanitize_request_messages(messages, _ALLOWED_MSG_KEYS)
        id_map: dict[str, str] = {}  # 原始 ID → 归一化 ID 的缓存
        pending_tool_ids: dict[str, deque[str]] = {}  # 原始 ID → 归一化 ID 队列（FIFO 映射）
        force_string_content = bool(
            self._spec
            and self._spec.name == "deepseek"
            and not self._deepseek_supports_vision(model_name)
        )
        normalize_tool_ids = self._should_normalize_tool_call_ids()

        def map_id(value: Any) -> Any:
            """把单个 ID 归一化并缓存映射（同一原始 ID 多次出现只算一次）。"""
            if not isinstance(value, str):
                return value
            if not normalize_tool_ids:
                return value
            return id_map.setdefault(value, self._normalize_tool_call_id(value))

        def unique_tool_id(value: Any, used_ids: set[str], idx: int) -> str:
            """生成不重复的归一化 ID：若映射后冲突，则加盐重哈希。"""
            if isinstance(value, str) and value:
                base = map_id(value)
            else:
                base = _short_tool_id()
            if not isinstance(base, str) or not base:
                base = _short_tool_id()
            if base not in used_ids:
                return base
            # 冲突时用 "原值:索引:盐" 重新哈希，直到不重复
            seed = value if isinstance(value, str) and value else base
            salt = 1
            while True:
                candidate = self._normalize_tool_call_id(f"{seed}:{idx}:{salt}")
                if isinstance(candidate, str) and candidate not in used_ids:
                    return candidate
                salt += 1

        def map_tool_result_id(value: Any) -> Any:
            """映射工具结果消息的 tool_call_id，按 FIFO 顺序匹配对应的归一化 ID。"""
            if not isinstance(value, str):
                return value
            queue = pending_tool_ids.get(value)
            if queue:
                mapped = queue.popleft()
                if not queue:
                    pending_tool_ids.pop(value, None)
                return mapped
            return map_id(value)

        for clean in sanitized:
            if isinstance(clean.get("tool_calls"), list):
                normalized = []
                used_ids: set[str] = set()
                for idx, tc in enumerate(clean["tool_calls"]):
                    if not isinstance(tc, dict):
                        normalized.append(tc)
                        continue
                    tc_clean = dict(tc)
                    raw_id = tc_clean.get("id")
                    mapped_id = unique_tool_id(raw_id, used_ids, idx)
                    tc_clean["id"] = mapped_id
                    used_ids.add(mapped_id)
                    # 记录原始 ID → 归一化 ID 的映射，供后续 tool 结果消息匹配
                    if isinstance(raw_id, str) and raw_id:
                        pending_tool_ids.setdefault(raw_id, deque()).append(mapped_id)
                    function = tc_clean.get("function")
                    if isinstance(function, dict):
                        function_clean = dict(function)
                        # 参数统一成可回放的 JSON 字符串
                        if "arguments" in function_clean:
                            function_clean["arguments"] = tool_arguments_json_for_replay(
                                function_clean.get("arguments")
                            )
                        else:
                            function_clean["arguments"] = "{}"
                        tc_clean["function"] = function_clean
                    normalized.append(tc_clean)
                clean["tool_calls"] = normalized
                if clean.get("role") == "assistant":
                    # 部分 OpenAI 兼容网关拒绝 assistant 消息同时携带非空 content 与 tool_calls，
                    # 这里把 content 置空以兼容。
                    clean["content"] = None
            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_tool_result_id(clean["tool_call_id"])
            if (
                force_string_content
                and not (clean.get("role") == "assistant" and clean.get("tool_calls"))
            ):
                clean["content"] = self._coerce_content_to_string(clean.get("content"))
        return self._enforce_role_alternation(sanitized)

    # ------------------------------------------------------------------
    # Build kwargs  构造请求参数
    # ------------------------------------------------------------------

    def _request_model_name(self, model_name: str) -> str:
        """根据 spec 配置剥离模型名前缀（如 ``openai/gpt-4o`` → ``gpt-4o``）。

        - spec.strip_model_prefix=True：无条件剥离最后一段；
        - 否则按 spec.strip_model_prefixes 白名单匹配，命中前缀才剥离；
        - 都不命中则原样返回。
        """
        spec = self._spec
        if not spec or "/" not in model_name:
            return model_name
        if spec.strip_model_prefix:
            return model_name.split("/")[-1]

        route_prefixes = getattr(spec, "strip_model_prefixes", ())
        if not isinstance(route_prefixes, tuple) or not route_prefixes:
            return model_name
        model_prefix, routed_model = model_name.split("/", 1)
        model_prefix_key = _provider_prefix_key(model_prefix)
        if any(_provider_prefix_key(prefix) == model_prefix_key for prefix in route_prefixes):
            return routed_model
        return model_name

    @staticmethod
    def _supports_temperature(
        model_name: str,
        reasoning_effort: str | None = None,
    ) -> bool:
        """判断模型是否接受 ``temperature`` 参数。

        GPT-5 系列与 o 系列（o1/o3/o4）推理模型在 reasoning_effort 非 none 时
        会拒绝 temperature；其余情况返回 ``True``。
        """
        if reasoning_effort and reasoning_effort.lower() != "none":
            return False
        name = model_name.lower()
        return not any(token in name for token in ("gpt-5", "o1", "o3", "o4"))

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """构造 Chat Completions API 的请求参数字典。

        职责包括：模型名前缀剥离、prompt cache 标记、temperature/max_tokens 选择、
        spec 级模型覆盖、reasoning_effort 语义化与线格式转换、思考模式注入、
        工具配置、DeepSeek reasoning_content 回填、以及用户 extra_body 递归合并。
        """
        model_name = model or self.default_model
        spec = self._spec

        # Anthropic 模型经 OpenRouter 等网关访问时，按 spec 标记 prompt cache
        if spec and spec.supports_prompt_caching:
            model_name = model or self.default_model
            if any(model_name.lower().startswith(k) for k in ("anthropic/", "claude")):
                messages, tools = self._apply_cache_control(messages, tools)

        model_name = self._request_model_name(model_name)

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": self._sanitize_messages(
                self._sanitize_empty_content(messages), model_name=model_name
            ),
        }

        # GPT-5 与 o 系列在 reasoning_effort 激活时拒绝 temperature，仅在安全时下发
        if self._supports_temperature(model_name, reasoning_effort):
            kwargs["temperature"] = temperature

        # 部分模型只接受 max_completion_tokens，其余用 max_tokens
        if (
            spec and getattr(spec, "supports_max_completion_tokens", False)
        ) or _requires_max_completion_tokens(model_name):
            kwargs["max_completion_tokens"] = max(1, max_tokens)
        else:
            kwargs["max_tokens"] = max(1, max_tokens)

        # spec 级模型覆盖：命中 pattern 则用 overrides 覆盖部分参数
        if spec:
            model_lower = model_name.lower()
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    break

        # reasoning_effort 归一化：
        # - semantic_effort：内部语义形式（OpenAI 词汇，用于决策）；
        # - wire_effort：实际下发的线格式。
        # "minimum" 作为 DashScope 原生别名，归一化为 "minimal"。
        semantic_effort: str | None = None
        if isinstance(reasoning_effort, str):
            semantic_effort = reasoning_effort.lower()
            if semantic_effort == "minimum":
                semantic_effort = "minimal"

        wire_effort = reasoning_effort
        if spec and spec.name == "dashscope" and semantic_effort == "minimal":
            # DashScope 接受 none/minimum/low/medium/high/xhigh，"minimal" 会 400
            wire_effort = "minimum"

        if wire_effort and semantic_effort != "none":
            kwargs["reasoning_effort"] = wire_effort

        # 仅在 reasoning_effort 显式给出时下发思考控制，
        # 这样省略配置时保留各 provider 默认行为
        if reasoning_effort is not None:
            slug = _model_slug(model_name)
            thinking_enabled = semantic_effort not in ("none", "minimal")
            for thinking_style in _thinking_styles_for(spec, model_name):
                # Kimi 代码模型即使关闭思考也跳过（它们始终思考）
                if not thinking_enabled and slug in _KIMI_ALWAYS_THINKING_MODELS:
                    continue
                extra = _thinking_extra_body(thinking_style, thinking_enabled)
                if extra:
                    kwargs.setdefault("extra_body", {}).update(extra)

            # Moonshot 拒绝同时携带 'reasoning_effort' 和原生 'thinking' 参数。
            # 用户的意图已经通过 provider 原生形式表达，这里删掉冗余的线级 kwarg。
            # 仅 kimi 模型需要如此处理——小米 API 可同时接受两个参数。
            if slug in _KIMI_THINKING_MODELS:
                kwargs.pop("reasoning_effort", None)

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        # 为缺失 reasoning_content 的 assistant 消息回填空串：
        # DeepSeek 思考模式否则会拒绝历史消息（#3554, #3584）；空串表示"该轮无思考"。
        # DeepSeek-V4/reasoner 原生推理，即使未显式 reasoning_effort 也回填。
        explicit_thinking = (
            reasoning_effort is not None
            and semantic_effort not in ("none", "minimal")
            and (
                (spec and spec.thinking_style)
                or _model_thinking_style(model_name)
            )
        )
        implicit_deepseek_thinking = (
            spec is not None
            and spec.name == "deepseek"
            and semantic_effort not in ("none", "minimal", "minimum")
            and any(t in model_name.lower() for t in ("deepseek-v4", "deepseek-reasoner"))
        )
        if explicit_thinking or implicit_deepseek_thinking:
            for msg in kwargs["messages"]:
                if msg.get("role") == "assistant" and "reasoning_content" not in msg:
                    msg["reasoning_content"] = ""

        # 最后合并用户配置的 extra_body，使其能覆盖或扩展 provider 默认值
        # （如 chat_template_kwargs、guided_json、repetition_penalty）。
        # 用递归合并避免嵌套 dict（如 {"chat_template_kwargs": {"enable_thinking": false}}）
        # 覆盖掉思考风格逻辑已设置的兄弟键。
        if self._extra_body:
            existing = kwargs.get("extra_body", {})
            kwargs["extra_body"] = _deep_merge(existing, self._extra_body)

        return kwargs

    def _should_use_responses_api(
        self,
        model: str | None,
        reasoning_effort: str | None,
    ) -> bool:
        """判断是否应使用 Responses API（仅 OpenAI 官方端点且能受益时）。

        判定逻辑：
        - 显式配置 ``chat_completions`` → ``False``；
        - 非 openai spec → ``False``；
        - 显式配置 ``responses`` → ``True``（强制使用，不查熔断器、不回退）；
        - 自动模式：仅 OpenAI 官方端点 + 推理模型/推理强度非 none 时才用，
          并需通过熔断器探测。
        """
        if self._api_type == "chat_completions":
            return False
        if self._spec and self._spec.name != "openai":
            return False
        if self._api_type == "responses":
            # 显式配置意味着 Responses 强制使用，不查熔断器、不回退到 Chat Completions
            return True
        if not _is_direct_openai_base(self._effective_base):
            return False

        model_name = (model or self.default_model).lower()
        wants = False
        # 推理强度非 none，或 GPT-5/o 系列模型才考虑用 Responses API
        if reasoning_effort and reasoning_effort.lower() != "none":
            wants = True
        elif any(token in model_name for token in ("gpt-5", "o1", "o3", "o4")):
            wants = True
        if not wants:
            return False

        return self._responses_circuit_allows_probe(model, reasoning_effort)

    def _responses_circuit_allows_probe(
        self,
        model: str | None,
        reasoning_effort: str | None,
    ) -> bool:
        """熔断器是否允许放行（关闭/半开探测时返回 ``True``，打开时返回 ``False``）。"""
        key = _responses_circuit_key(model, self.default_model, reasoning_effort)
        failures = self._responses_failures.get(key, 0)
        if failures >= _RESPONSES_FAILURE_THRESHOLD:
            tripped = self._responses_tripped_at.get(key, 0.0)
            if (time.monotonic() - tripped) < _RESPONSES_PROBE_INTERVAL_S:
                return False
            # 半开状态：放行一次探测请求
        return True

    def _record_responses_failure(self, model: str | None, reasoning_effort: str | None) -> None:
        """记录一次 Responses API 失败，达到阈值则打开熔断器并告警。"""
        key = _responses_circuit_key(model, self.default_model, reasoning_effort)
        count = self._responses_failures.get(key, 0) + 1
        self._responses_failures[key] = count
        if count >= _RESPONSES_FAILURE_THRESHOLD:
            self._responses_tripped_at[key] = time.monotonic()
            logger.warning(
                "Responses API circuit open for {} — falling back to Chat Completions",
                key,
            )

    def _record_responses_success(self, model: str | None, reasoning_effort: str | None) -> None:
        """记录一次 Responses API 成功，清空该 key 的熔断状态。"""
        key = _responses_circuit_key(model, self.default_model, reasoning_effort)
        self._responses_failures.pop(key, None)
        self._responses_tripped_at.pop(key, None)

    @staticmethod
    def _should_fallback_from_responses_error(e: Exception) -> bool:
        """判断是否应从 Responses API 回退到 Chat Completions。

        仅在"疑似 Responses API 兼容性错误"时回退：
        - 状态码为 400/404/422；
        - 错误体命中兼容性标记（如 "unsupported"、"unknown parameter" 等）。
        """
        response = getattr(e, "response", None)
        status_code = getattr(e, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        if status_code not in {400, 404, 422}:
            return False

        body = (
            getattr(e, "body", None)
            or getattr(e, "doc", None)
            or getattr(response, "text", None)
        )
        body_text = str(body).lower() if body is not None else ""
        # 兼容性错误标记：命中任一即认为是 Responses API 不被支持
        compatibility_markers = (
            "responses",
            "response api",
            "max_output_tokens",
            "instructions",
            "previous_response",
            "unsupported",
            "not supported",
            "unknown parameter",
            "unrecognized request argument",
        )
        return any(marker in body_text for marker in compatibility_markers)

    @staticmethod
    def _is_stream_options_rejection(e: Exception) -> bool:
        """判断异常是否为网关拒绝 ``stream_options`` 参数的 400 错误。

        部分旧网关不认识 ``stream_options={"include_usage": True}``，
        直接返回 400；剥掉该参数重试一次即可恢复。
        """
        response = getattr(e, "response", None)
        status_code = getattr(e, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)
        if status_code != 400:
            return False
        body = (
            getattr(e, "body", None)
            or getattr(e, "doc", None)
            or getattr(response, "text", None)
        )
        return "stream_options" in str(body).lower()

    def _build_responses_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> dict[str, Any]:
        """构造 Responses API 的请求 body（用于 OpenAI 官方端点）。

        与 Chat Completions 不同：使用 ``input``/``instructions`` 而非 ``messages``，
        用 ``max_output_tokens`` 而非 ``max_tokens``，并显式关闭服务端存储。
        """
        model_name = model or self.default_model
        model_name = self._request_model_name(model_name)
        sanitized_messages = self._sanitize_messages(
            self._sanitize_empty_content(messages), model_name=model_name
        )
        # 把消息拆成 instructions（系统提示）与 input_items（对话历史）
        instructions, input_items = convert_messages(sanitized_messages)

        body: dict[str, Any] = {
            "model": model_name,
            "instructions": instructions or None,
            "input": input_items,
            "max_output_tokens": max(1, max_tokens),
            "store": False,  # 不在服务端持久化响应
            "stream": False,
        }

        if self._supports_temperature(model_name, reasoning_effort):
            body["temperature"] = temperature

        if reasoning_effort and reasoning_effort.lower() != "none":
            body["reasoning"] = {"effort": reasoning_effort}
            # 请求返回加密的推理内容，便于回放
            body["include"] = ["reasoning.encrypted_content"]

        if tools:
            body["tools"] = convert_tools(tools)
            body["tool_choice"] = tool_choice or "auto"

        extra_body = getattr(self, "_extra_body", {})
        if extra_body:
            body = _merge_responses_extra_body(body, extra_body)

        return body

    # ------------------------------------------------------------------
    # Response parsing  响应解析
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_mapping(value: Any) -> dict[str, Any] | None:
        """把 value 尽量转成 dict；非 dict 且无 ``model_dump()`` 时返回 ``None``。

        用于统一处理原始 JSON dict 与 SDK Pydantic 对象两种响应形态。
        """
        if isinstance(value, dict):
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        return None

    @classmethod
    def _extract_text_content(cls, value: Any) -> str | None:
        """从多种 content 形态（str / list / 块对象）中提取纯文本。

        - str：原样返回；
        - list：逐项取 ``text`` 字段拼接；
        - 其他：``str(value)`` 兜底。
        全空时返回 ``None``。
        """
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                # 优先按 dict 取 text
                item_map = cls._maybe_mapping(item)
                if item_map:
                    text = item_map.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                        continue
                # 回退到属性取 text
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                    continue
                # 字符串项直接拼接
                if isinstance(item, str):
                    parts.append(item)
            return "".join(parts) or None
        return str(value)

    @classmethod
    def _extract_usage(cls, response: Any) -> dict[str, int]:
        """从 OpenAI 兼容响应中提取 token 用量。

        同时处理 dict（原始 JSON）与对象（SDK Pydantic）两种形态。
        provider 各异的 ``cached_tokens`` 字段会被归一化到统一键名，
        详见内部优先级链。
        """
        # --- 解析 usage 对象 ---
        usage_obj = None
        response_map = cls._maybe_mapping(response)
        if response_map is not None:
            usage_obj = response_map.get("usage")
        elif hasattr(response, "usage") and response.usage:
            usage_obj = response.usage

        usage_map = cls._maybe_mapping(usage_obj)
        if usage_map is not None:
            result = {
                "prompt_tokens": int(usage_map.get("prompt_tokens") or 0),
                "completion_tokens": int(usage_map.get("completion_tokens") or 0),
                "total_tokens": int(usage_map.get("total_tokens") or 0),
            }
        elif usage_obj:
            # SDK 对象形态：用属性读取
            result = {
                "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
                "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
            }
        else:
            return {}

        # --- cached_tokens（跨 provider 归一化）---
        # 优先按嵌套路径取（dict），回退到属性（SDK 对象）。
        # 顺序保证最具体的字段胜出。
        for path in (
            ("prompt_tokens_details", "cached_tokens"),  # OpenAI/Zhipu/MiniMax/Qwen/Mistral/xAI
            ("cached_tokens",),                          # StepFun/Moonshot（顶层）
            ("prompt_cache_hit_tokens",),                # DeepSeek/SiliconFlow
        ):
            cached = cls._get_nested_int(usage_map, path)
            if not cached and usage_obj:
                cached = cls._get_nested_int(usage_obj, path)
            if cached:
                result["cached_tokens"] = cached
                break

        return result

    @staticmethod
    def _merge_usage(
        current: dict[str, int],
        candidate: dict[str, int] | None,
    ) -> dict[str, int]:
        """仅当候选 usage 携带非零数值时才覆盖当前累计 usage。

        部分网关在流式中间 chunk（甚至带 choices 的 chunk）上返回全零 usage；
        ``dict or dict`` 恒取前者，全零 dict 为真值会覆盖之前累计的真实用量。
        """
        if candidate and any(candidate.values()):
            return candidate
        return current

    @staticmethod
    def _get_nested_int(obj: Any, path: tuple[str, ...]) -> int:
        """按 ``path`` 段逐层下钻取值并返回 ``int``。

        同时支持 dict 键访问与对象属性访问，
        以便统一处理原始 JSON dict 与 SDK Pydantic 模型。
        """
        current = obj
        for segment in path:
            if current is None:
                return 0
            if isinstance(current, dict):
                current = current.get(segment)
            else:
                current = getattr(current, segment, None)
        return int(current or 0) if current is not None else 0

    def _parse(self, response: Any) -> LLMResponse:
        """把非流式响应解析为 ``LLMResponse``。

        兼容三类形态：
        - 纯字符串：直接作为 content；
        - dict（原始 JSON）：从 choices/usage 中提取；
        - SDK 对象：用属性访问 choices/message/tool_calls。

        同时处理 StepFun 等 provider 把内容放在 ``reasoning`` 字段的情况，
        以及多 choice 合并工具调用的场景。
        """
        if isinstance(response, str):
            return LLMResponse(content=response, finish_reason="stop")

        response_map = self._maybe_mapping(response)
        if response_map is not None:
            # dict 形态
            choices = response_map.get("choices") or []
            if not choices:
                # 无 choices：尝试从顶层 content/output_text 取正文
                content = self._extract_text_content(
                    response_map.get("content") or response_map.get("output_text")
                )
                reasoning_content = self._extract_text_content(
                    response_map.get("reasoning_content")
                )
                if content is not None:
                    return LLMResponse(
                        content=content,
                        reasoning_content=reasoning_content,
                        finish_reason=str(response_map.get("finish_reason") or "stop"),
                        usage=self._extract_usage(response_map),
                    )
                return LLMResponse(content="Error: API returned empty choices.", finish_reason="error")

            choice0 = self._maybe_mapping(choices[0]) or {}
            msg0 = self._maybe_mapping(choice0.get("message")) or {}
            content = self._extract_text_content(msg0.get("content"))
            finish_reason = str(choice0.get("finish_reason") or "stop")

            raw_tool_calls: list[Any] = []
            # StepFun：content 为空时回退到 reasoning 字段
            if not content and msg0.get("reasoning") and self._spec and self._spec.reasoning_as_content:
                content = self._extract_text_content(msg0.get("reasoning"))
            reasoning_content = msg0.get("reasoning_content")
            if reasoning_content is None and msg0.get("reasoning"):
                # 部分 provider 把推理内容放在 reasoning 而非 reasoning_content
                reasoning_content = self._extract_text_content(msg0.get("reasoning"))
            # 遍历所有 choice，合并工具调用并补齐 content/finish_reason
            for ch in choices:
                ch_map = self._maybe_mapping(ch) or {}
                m = self._maybe_mapping(ch_map.get("message")) or {}
                tool_calls = m.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    raw_tool_calls.extend(tool_calls)
                    if ch_map.get("finish_reason") in ("tool_calls", "stop"):
                        finish_reason = str(ch_map["finish_reason"])
                if not content:
                    content = self._extract_text_content(m.get("content"))
                if reasoning_content is None:
                    reasoning_content = m.get("reasoning_content")

            # 组装工具调用，提取扩展字段
            parsed_tool_calls = []
            for tc in raw_tool_calls:
                tc_map = self._maybe_mapping(tc) or {}
                fn = self._maybe_mapping(tc_map.get("function")) or {}
                args = parse_tool_arguments(fn.get("arguments", {}))
                ec, prov, fn_prov = _extract_tc_extras(tc)
                parsed_tool_calls.append(ToolCallRequest(
                    id=str(tc_map.get("id") or _short_tool_id()),
                    name=str(fn.get("name") or ""),
                    arguments=args,
                    extra_content=ec,
                    provider_specific_fields=prov,
                    function_provider_specific_fields=fn_prov,
                ))

            return LLMResponse(
                content=content,
                tool_calls=parsed_tool_calls,
                finish_reason=finish_reason,
                usage=self._extract_usage(response_map),
                reasoning_content=reasoning_content if isinstance(reasoning_content, str) else None,
            )

        # SDK 对象形态
        if not response.choices:
            return LLMResponse(content="Error: API returned empty choices.", finish_reason="error")

        choice = response.choices[0]
        msg = choice.message
        content = msg.content
        finish_reason = choice.finish_reason

        raw_tool_calls: list[Any] = []
        for ch in response.choices:
            m = ch.message
            if hasattr(m, "tool_calls") and m.tool_calls:
                raw_tool_calls.extend(m.tool_calls)
                if ch.finish_reason in ("tool_calls", "stop"):
                    finish_reason = ch.finish_reason
            if not content and m.content:
                content = m.content
            if not content and getattr(m, "reasoning", None) and self._spec and self._spec.reasoning_as_content:
                content = m.reasoning

        tool_calls = []
        for tc in raw_tool_calls:
            args = parse_tool_arguments(tc.function.arguments)
            ec, prov, fn_prov = _extract_tc_extras(tc)
            tool_calls.append(ToolCallRequest(
                id=str(getattr(tc, "id", None) or _short_tool_id()),
                name=tc.function.name,
                arguments=args,
                extra_content=ec,
                provider_specific_fields=prov,
                function_provider_specific_fields=fn_prov,
            ))

        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is None and getattr(msg, "reasoning", None):
            reasoning_content = msg.reasoning

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=self._extract_usage(response),
            reasoning_content=reasoning_content,
        )

    @classmethod
    def _parse_chunks(cls, chunks: list[Any]) -> LLMResponse:
        """把流式 chunk 列表累积解析为 ``LLMResponse``。

        逐个 chunk 处理：
        - 文本 delta 拼接到 content_parts；
        - 推理 delta 拼接到 reasoning_parts；
        - 工具调用 delta 按 index 累积到 tc_bufs；
        - usage/finish_reason 持续更新直到结束。

        兼容 dict chunk 与 SDK 对象 chunk 两种形态，
        以及 legacy ``delta.function_call`` 形式。
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # 工具调用缓冲：index → {id, name, arguments, extras}
        tc_bufs: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        def _accum_tc(tc: Any, idx_hint: int) -> None:
            """把单个流式工具调用 delta 累积到 ``tc_bufs``。"""
            tc_index: int = _get(tc, "index") if _get(tc, "index") is not None else idx_hint
            buf = tc_bufs.setdefault(tc_index, {
                "id": "", "name": "", "arguments": "",
                "extra_content": None, "prov": None, "fn_prov": None,
            })
            tc_id = _get(tc, "id")
            if tc_id:
                buf["id"] = str(tc_id)
            fn = _get(tc, "function")
            if fn is not None:
                fn_name = _get(fn, "name")
                if fn_name:
                    buf["name"] = str(fn_name)
                fn_args = _get(fn, "arguments")
                if fn_args:
                    # 流式参数是增量字符串，需拼接
                    buf["arguments"] += str(fn_args)
            ec, prov, fn_prov = _extract_tc_extras(tc)
            if ec:
                buf["extra_content"] = ec
            if prov:
                buf["prov"] = prov
            if fn_prov:
                buf["fn_prov"] = fn_prov

        def _accum_legacy_function_call(function_call: Any) -> None:
            """累积 legacy ``delta.function_call`` 流式 chunk（旧式工具调用）。"""
            if not function_call:
                return
            buf = tc_bufs.setdefault(0, {
                "id": "", "name": "", "arguments": "",
                "extra_content": None, "prov": None, "fn_prov": None,
            })
            fn_name = _get(function_call, "name")
            if fn_name:
                buf["name"] = str(fn_name)
            fn_args = _get(function_call, "arguments")
            if fn_args:
                buf["arguments"] += str(fn_args)

        for chunk in chunks:
            if isinstance(chunk, str):
                content_parts.append(chunk)
                continue

            chunk_map = cls._maybe_mapping(chunk)
            if chunk_map is not None:
                # dict 形态 chunk
                choices = chunk_map.get("choices") or []
                if not choices:
                    # 无 choices 的 chunk 通常只携带 usage
                    usage = cls._merge_usage(usage, cls._extract_usage(chunk_map))
                    text = cls._extract_text_content(
                        chunk_map.get("content") or chunk_map.get("output_text")
                    )
                    if text:
                        content_parts.append(text)
                    continue
                choice = cls._maybe_mapping(choices[0]) or {}
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                delta = cls._maybe_mapping(choice.get("delta")) or {}
                text = cls._extract_text_content(delta.get("content"))
                if text:
                    content_parts.append(text)
                # 推理内容：优先 reasoning_content，回退 reasoning
                text = cls._extract_text_content(delta.get("reasoning_content"))
                if not text:
                    text = cls._extract_text_content(delta.get("reasoning"))
                if text:
                    reasoning_parts.append(text)
                for idx, tc in enumerate(delta.get("tool_calls") or []):
                    _accum_tc(tc, idx)
                _accum_legacy_function_call(delta.get("function_call"))
                usage = cls._merge_usage(usage, cls._extract_usage(chunk_map))
                continue

            # SDK 对象形态 chunk
            if not chunk.choices:
                usage = cls._merge_usage(usage, cls._extract_usage(chunk))
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta and delta.content:
                content_parts.append(delta.content)
            if delta:
                reasoning = getattr(delta, "reasoning_content", None)
                if not reasoning:
                    reasoning = getattr(delta, "reasoning", None)
                if reasoning:
                    reasoning_parts.append(reasoning)
            for tc in (getattr(delta, "tool_calls", None) or []) if delta else []:
                _accum_tc(tc, getattr(tc, "index", 0))
            if delta:
                _accum_legacy_function_call(getattr(delta, "function_call", None))

        # 部分 provider（如 Zhipu/GLM）在流式模式下会为并行工具调用复用同一 id，
        # 这里去重，避免下游工具消息冲突。
        _seen_tc_ids: set[str] = set()
        for b in tc_bufs.values():
            if not b["id"] or b["id"] in _seen_tc_ids:
                b["id"] = _short_tool_id()
            _seen_tc_ids.add(b["id"])

        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=[
                ToolCallRequest(
                    id=b["id"] or _short_tool_id(),
                    name=b["name"],
                    arguments=parse_tool_arguments(b["arguments"]),
                    extra_content=b.get("extra_content"),
                    provider_specific_fields=b.get("prov"),
                    function_provider_specific_fields=b.get("fn_prov"),
                )
                for b in tc_bufs.values()
            ],
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content="".join(reasoning_parts) or None,
        )

    @classmethod
    def _extract_error_metadata(cls, e: Exception) -> dict[str, Any]:
        """从异常对象中提取错误元数据（状态码、类型、是否可重试等）。

        返回字段：
        - ``error_status_code``：HTTP 状态码；
        - ``error_kind``：错误类别（timeout/connection/None）；
        - ``error_type``/``error_code``：从响应体解析的 provider 错误信息；
        - ``error_retry_after_s``：建议的重试等待秒数；
        - ``error_should_retry``：根据 ``x-should-retry`` header 判断是否可重试。
        """
        response = getattr(e, "response", None)
        headers = getattr(response, "headers", None)
        # 优先从异常属性取响应体，回退到 response.text
        payload = (
            getattr(e, "body", None)
            or getattr(e, "doc", None)
            or getattr(response, "text", None)
        )
        if payload is None and response is not None:
            # 最后尝试调用 response.json()
            response_json = getattr(response, "json", None)
            if callable(response_json):
                try:
                    payload = response_json()
                except Exception:
                    payload = None
        error_type, error_code = LLMProvider._extract_error_type_code(payload)

        status_code = getattr(e, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        # 读取 x-should-retry header 决定是否可重试
        should_retry: bool | None = None
        if headers is not None:
            raw = headers.get("x-should-retry")
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered == "true":
                    should_retry = True
                elif lowered == "false":
                    should_retry = False

        # 根据异常类名推断错误类别
        error_kind: str | None = None
        error_name = e.__class__.__name__.lower()
        if "timeout" in error_name:
            error_kind = "timeout"
        elif "connection" in error_name:
            error_kind = "connection"

        return {
            "error_status_code": int(status_code) if status_code is not None else None,
            "error_kind": error_kind,
            "error_type": error_type,
            "error_code": error_code,
            "error_retry_after_s": cls._extract_retry_after_from_headers(headers),
            "error_should_retry": should_retry,
        }

    @staticmethod
    def _handle_error(
        e: Exception,
        *,
        spec: ProviderSpec | None = None,
        api_base: str | None = None,
    ) -> LLMResponse:
        """把异常转换成 ``LLMResponse``（finish_reason="error"）。

        会从异常中提取错误体、状态码、retry-after 等；
        对本地端点的连接类错误会追加排查提示。
        """
        body = (
            getattr(e, "doc", None)
            or getattr(e, "body", None)
            or getattr(getattr(e, "response", None), "text", None)
        )
        body_text = body if isinstance(body, str) else str(body) if body is not None else ""
        # 截断到 500 字避免日志过长
        msg = f"Error: {body_text.strip()[:500]}" if body_text.strip() else f"Error calling LLM: {e}"

        # 本地端点的连接类错误：追加排查提示
        text = f"{body_text} {e}".lower()
        if spec and spec.is_local and ("502" in text or "connection" in text or "refused" in text):
            msg += (
                "\nHint: this is a local model endpoint. Check that the local server is reachable at "
                f"{api_base or spec.default_api_base}, and if you are using a proxy/tunnel, make sure it "
                "can reach your local Ollama/vLLM service instead of routing localhost through the remote host."
            )

        response = getattr(e, "response", None)
        # retry-after 优先从 header 取，回退到从消息体解析
        retry_after = LLMProvider._extract_retry_after_from_headers(getattr(response, "headers", None))
        if retry_after is None:
            retry_after = LLMProvider._extract_retry_after(msg)
        return LLMResponse(
            content=msg,
            finish_reason="error",
            retry_after=retry_after,
            **OpenAICompatProvider._extract_error_metadata(e),
        )

    # ------------------------------------------------------------------
    # Public API  对外接口
    # ------------------------------------------------------------------

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
        """非流式聊天接口。

        优先尝试 Responses API（若适用），失败且非强制模式时回退到 Chat Completions。
        所有异常统一转成 ``LLMResponse(finish_reason="error")``，不向上抛出。
        """
        await self._ensure_client()
        try:
            if self._should_use_responses_api(model, reasoning_effort):
                try:
                    body = self._build_responses_body(
                        messages, tools, model, max_tokens, temperature,
                        reasoning_effort, tool_choice,
                    )
                    result = parse_response_output(await self._client.responses.create(**body))
                    self._record_responses_success(model, reasoning_effort)
                    return result
                except Exception as responses_error:
                    # 强制 responses 模式：不回退，直接抛
                    if self._api_type == "responses":
                        raise
                    # 非兼容性错误：直接抛，由外层捕获
                    if not self._should_fallback_from_responses_error(responses_error):
                        raise
                    # 兼容性错误：记录熔断并回退到 Chat Completions
                    self._record_responses_failure(model, reasoning_effort)

            kwargs = self._build_kwargs(
                messages, tools, model, max_tokens, temperature,
                reasoning_effort, tool_choice,
            )
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return self._handle_error(e, spec=self._spec, api_base=self.api_base)

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
        """流式聊天接口。

        通过三类回调实时推送增量：``on_content_delta``（正文）、
        ``on_thinking_delta``（推理）、``on_tool_call_delta``（工具调用）。
        每个 chunk 之间有 idle 超时保护，超时则返回 timeout 错误。

        与 ``chat`` 类似：优先 Responses API 流式，失败回退 Chat Completions 流式。
        """
        await self._ensure_client()
        idle_timeout_s = resolve_stream_idle_timeout_s()
        try:
            if self._should_use_responses_api(model, reasoning_effort):
                try:
                    body = self._build_responses_body(
                        messages, tools, model, max_tokens, temperature,
                        reasoning_effort, tool_choice,
                    )
                    body["stream"] = True
                    stream = await self._client.responses.create(**body)

                    async def _timed_stream():
                        # 给每个 chunk 包一层 idle 超时，避免流卡死
                        stream_iter = stream.__aiter__()
                        while True:
                            try:
                                yield await asyncio.wait_for(
                                    stream_iter.__anext__(),
                                    timeout=idle_timeout_s,
                                )
                            except StopAsyncIteration:
                                break

                    (
                        content,
                        tool_calls,
                        finish_reason,
                        usage,
                        reasoning_content,
                    ) = await consume_sdk_stream(
                        _timed_stream(),
                        on_content_delta,
                        on_tool_call_delta=on_tool_call_delta,
                    )
                    self._record_responses_success(model, reasoning_effort)
                    return LLMResponse(
                        content=content or None,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                        usage=usage,
                        reasoning_content=reasoning_content,
                    )
                except Exception as responses_error:
                    if self._api_type == "responses":
                        raise
                    if not self._should_fallback_from_responses_error(responses_error):
                        raise
                    self._record_responses_failure(model, reasoning_effort)

            # Chat Completions 流式
            kwargs = self._build_kwargs(
                messages, tools, model, max_tokens, temperature,
                reasoning_effort, tool_choice,
            )
            if self._spec and self._spec.name == "zhipu" and tools and on_tool_call_delta:
                # Z.AI/GLM 需要显式 provider flag 才会流式下发工具调用参数。
                # 通过 OpenAI SDK 的 extra_body 透传，让 delta.tool_calls
                # 能实时反映文件编辑进度。
                kwargs.setdefault("extra_body", {})["tool_stream"] = True
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            if self._client is None:
                raise RuntimeError("OpenAI-compatible client is not initialized")
            try:
                stream = await self._client.chat.completions.create(**kwargs)
            except Exception as e:
                if not self._is_stream_options_rejection(e):
                    raise
                # 部分旧网关不认识 stream_options 参数（400 提及该字段）：
                # 剥掉后重试一次，仅损失 usage 统计，不影响流式内容。
                logger.warning("Gateway rejected stream_options; retrying without it")
                kwargs.pop("stream_options", None)
                stream = await self._client.chat.completions.create(**kwargs)
            chunks: list[Any] = []
            stream_iter = stream.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=idle_timeout_s,
                    )
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
                if chunk.choices:
                    delta_obj = chunk.choices[0].delta
                    # 正文增量回调
                    if on_content_delta:
                        text = getattr(delta_obj, "content", None)
                        if text:
                            await on_content_delta(text)
                    # 推理增量回调（reasoning_content 优先，回退 reasoning）
                    if on_thinking_delta:
                        reasoning = getattr(delta_obj, "reasoning_content", None) or getattr(
                            delta_obj, "reasoning", None,
                        )
                        r_text = self._extract_text_content(reasoning)
                        if r_text:
                            await on_thinking_delta(r_text)
                    # 工具调用增量回调
                    if on_tool_call_delta:
                        for idx, tool_delta in enumerate(
                            getattr(delta_obj, "tool_calls", None) or []
                        ):
                            fn = _get(tool_delta, "function")
                            tool_index = _get(tool_delta, "index")
                            await on_tool_call_delta({
                                "index": tool_index if tool_index is not None else idx,
                                "call_id": str(_get(tool_delta, "id") or ""),
                                "name": str(_get(fn, "name") or "") if fn is not None else "",
                                "arguments_delta": (
                                    str(_get(fn, "arguments") or "") if fn is not None else ""
                                ),
                            })
                        # legacy function_call 形式
                        function_call = getattr(delta_obj, "function_call", None)
                        if function_call:
                            await on_tool_call_delta({
                                "index": 0,
                                "call_id": "",
                                "name": str(_get(function_call, "name") or ""),
                                "arguments_delta": str(_get(function_call, "arguments") or ""),
                            })
            return self._parse_chunks(chunks)
        except asyncio.TimeoutError:
            # 流 idle 超时：返回 timeout 错误而非抛异常
            return LLMResponse(
                content=(
                    f"Error calling LLM: stream stalled for more than "
                    f"{idle_timeout_s:g} seconds"
                ),
                finish_reason="error",
                error_kind="timeout",
            )
        except Exception as e:
            return self._handle_error(e, spec=self._spec, api_base=self.api_base)

    def get_default_model(self) -> str:
        """返回该 provider 的默认模型名。"""
        return self.default_model
