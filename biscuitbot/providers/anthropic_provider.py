"""Anthropic Provider —— Claude 模型的原生 SDK 集成实现。

所属模块与项目作用
===================
本文件位于 biscuitbot/providers 目录，是 LLM Provider 层的 Anthropic
后端组件。在项目架构中起到的作用：
- 通过官方 anthropic SDK 直连 Claude 系列模型，承担消息格式转换
  （OpenAI chat 格式 → Anthropic Messages API）、prompt 缓存、
  extended thinking、工具调用以及流式响应等核心能力。
- 作为 :class:`LLMProvider` 的具体实现，向上层提供统一的 chat /
  chat_stream 接口，屏蔽 Anthropic 协议细节。
"""

from __future__ import annotations

import asyncio  # 流式读取与超时控制均基于 asyncio 协程实现
import re  # 用于解析 data: URL 形式的 base64 图片
import secrets  # 生成高熵随机工具调用 ID
import string  # 构造工具 ID 使用的字母数字字符集
from collections.abc import Awaitable, Callable  # 流式回调类型签名
from typing import Any

# 从 base 模块引入 Provider 基类、响应数据类及若干工具函数
from biscuitbot.providers.base import (
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    resolve_stream_idle_timeout_s,
    tool_arguments_object_for_replay,
)

# 工具 ID 随机字符表：字母+数字
_ALNUM = string.ascii_letters + string.digits


def _gen_tool_id() -> str:
    """生成 Anthropic 格式的工具调用 ID（前缀 ``toolu_`` + 22 位随机字符）。"""
    return "toolu_" + "".join(secrets.choice(_ALNUM) for _ in range(22))


class AnthropicProvider(LLMProvider):
    """基于 Anthropic 原生 SDK 的 Claude 模型 Provider 实现。

    负责处理 OpenAI chat 消息格式到 Anthropic Messages API 的转换、
    prompt 缓存、extended thinking（扩展思考）、工具调用以及流式响应。
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "claude-sonnet-4-20250514",
        extra_headers: dict[str, str] | None = None,
    ):
        """初始化 Anthropic Provider。

        :param api_key: Anthropic API 密钥，为空时由 SDK 自行从环境变量读取。
        :param api_base: 自定义 API 基址（兼容代理/网关）。
        :param default_model: 默认模型名，调用方未指定 model 时使用。
        :param extra_headers: 附加请求头（如 beta 特性开关）。
        """
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        from anthropic import AsyncAnthropic  # 延迟导入 SDK，避免在模块加载阶段强依赖

        client_kw: dict[str, Any] = {}
        if api_key:
            client_kw["api_key"] = api_key
        if api_base:
            client_kw["base_url"] = self._normalize_base_url(api_base)
        if extra_headers:
            client_kw["default_headers"] = extra_headers
        # 重试逻辑集中在 LLMProvider._run_with_retry 中统一处理，避免 SDK 内置重试与上层重试叠加放大。
        client_kw["max_retries"] = 0
        self._client = AsyncAnthropic(**client_kw)

    @staticmethod
    def _normalize_base_url(api_base: str) -> str:
        """规范化 API 基址。

        Anthropic SDK 内部会自动给请求路径追加 ``/v1``，因此如果调用方
        传入的基址已经包含 ``/v1``，需要先剥离，避免最终路径出现 ``/v1/v1``。
        """
        normalized = api_base.rstrip("/")
        if normalized.endswith("/v1"):
            return normalized[: -len("/v1")]
        return normalized

    @classmethod
    def _handle_error(cls, e: Exception) -> LLMResponse:
        """将 Anthropic SDK 抛出的异常归一化为 :class:`LLMResponse` 错误响应。

        会从异常对象上提取 HTTP 响应、响应头、响应体、状态码、``x-should-retry``
        头以及重试等待时间，并按异常类名推断 ``error_kind``（timeout /
        connection），最终交由 :meth:`LLMProvider._extract_error_type_code`
        解析语义化的 ``error_type`` / ``error_code``，供上层重试策略使用。
        """
        response = getattr(e, "response", None)
        headers = getattr(response, "headers", None)
        # 优先从异常的 body/doc 取响应体，再退化到 response.text
        payload = (
            getattr(e, "body", None)
            or getattr(e, "doc", None)
            or getattr(response, "text", None)
        )
        if payload is None and response is not None:
            # 某些 SDK 版本响应体需显式调用 .json() 才能拿到字典
            response_json = getattr(response, "json", None)
            if callable(response_json):
                try:
                    payload = response_json()
                except Exception:
                    payload = None
        payload_text = payload if isinstance(payload, str) else str(payload) if payload is not None else ""
        msg = f"Error: {payload_text.strip()[:500]}" if payload_text.strip() else f"Error calling LLM: {e}"
        # 优先从响应头 Retry-After 提取重试等待，其次从消息文本解析
        retry_after = cls._extract_retry_after_from_headers(headers)
        if retry_after is None:
            retry_after = LLMProvider._extract_retry_after(msg)

        status_code = getattr(e, "status_code", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        # 解析 Anthropic 的 x-should-retry 响应头，决定是否应重试
        should_retry: bool | None = None
        if headers is not None:
            raw = headers.get("x-should-retry")
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered == "true":
                    should_retry = True
                elif lowered == "false":
                    should_retry = False

        # 按异常类名推断错误大类：超时 / 连接错误
        error_kind: str | None = None
        error_name = e.__class__.__name__.lower()
        if "timeout" in error_name:
            error_kind = "timeout"
        elif "connection" in error_name:
            error_kind = "connection"
        # 提取语义化错误类型与错误码（如 insufficient_quota / rate_limit_exceeded）
        error_type, error_code = LLMProvider._extract_error_type_code(payload)

        return LLMResponse(
            content=msg,
            finish_reason="error",
            retry_after=retry_after,
            error_status_code=int(status_code) if status_code is not None else None,
            error_kind=error_kind,
            error_type=error_type,
            error_code=error_code,
            error_retry_after_s=retry_after,
            error_should_retry=should_retry,
        )

    @staticmethod
    def _strip_prefix(model: str) -> str:
        """剥离模型名前缀 ``anthropic/``，返回 Anthropic SDK 可识别的纯模型名。"""
        if model.startswith("anthropic/"):
            return model[len("anthropic/"):]
        return model

    # ------------------------------------------------------------------
    # 消息格式转换：OpenAI chat 格式 → Anthropic Messages API
    # ------------------------------------------------------------------

    def _convert_messages(
        self, messages: list[dict[str, Any]],
    ) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]]]:
        """将 OpenAI 风格消息列表转换为 Anthropic Messages API 所需结构。

        返回二元组 ``(system, anthropic_messages)``：
        - ``system`` 单独抽出（Anthropic 用独立字段传递系统提示）。
        - 其余消息按 user/assistant/tool 角色归并，最后调用
          :meth:`_merge_consecutive` 处理 Anthropic 的角色交替约束。
        """
        system_parts: list[str | list[dict[str, Any]]] = []
        raw: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")

            if role == "system":
                # 系统消息：Anthropic 单独接收，不在 messages 数组中。
                # 多条 system 全部累积，避免只留最后一条丢内容。
                system_parts.append(
                    content if isinstance(content, (str, list)) else str(content or "")
                )
                continue

            if role == "tool":
                # 工具结果消息：转换为 Anthropic 的 tool_result 块，并归并到上一个 user 轮次
                block = self._tool_result_block(msg)
                if raw and raw[-1]["role"] == "user":
                    prev_c = raw[-1]["content"]
                    if isinstance(prev_c, list):
                        prev_c.append(block)
                    else:
                        # 上一条是纯文本 user，升级为 list 形式以容纳 tool_result
                        raw[-1]["content"] = [
                            {"type": "text", "text": prev_c or ""}, block,
                        ]
                else:
                    raw.append({"role": "user", "content": [block]})
                continue

            if role == "assistant":
                raw.append({"role": "assistant", "content": self._assistant_blocks(msg)})
                continue

            if role == "user":
                raw.append({
                    "role": "user",
                    "content": self._convert_user_content(content),
                })
                continue

        return self._merge_system_parts(system_parts), self._merge_consecutive(raw)

    @staticmethod
    def _merge_system_parts(
        parts: list[str | list[dict[str, Any]]],
    ) -> str | list[dict[str, Any]]:
        """把多条 system 消息合并为单个 ``system`` 字段。

        - 全部为字符串：用 ``"\n\n"`` 拼接；
        - 含列表块：统一转为块列表，字符串部分转为 text 块，
          保持与单条 list 形式 system 一致的传递方式。
        """
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        if all(isinstance(part, str) for part in parts):
            str_parts = [part for part in parts if isinstance(part, str) and part]
            return "\n\n".join(str_parts)
        blocks: list[dict[str, Any]] = []
        for part in parts:
            if isinstance(part, str):
                if part:
                    blocks.append({"type": "text", "text": part})
            elif isinstance(part, list):
                blocks.extend(block for block in part if isinstance(block, dict))
            else:
                blocks.append({"type": "text", "text": str(part)})
        return blocks

    @staticmethod
    def _tool_result_block(msg: dict[str, Any]) -> dict[str, Any]:
        """将 OpenAI 的 tool 角色消息转换为 Anthropic ``tool_result`` 内容块。"""
        content = msg.get("content")
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": msg.get("tool_call_id", ""),
        }
        if isinstance(content, list):
            block["content"] = AnthropicProvider._convert_user_content(content)
        elif isinstance(content, str):
            block["content"] = content
        else:
            block["content"] = str(content) if content else ""
        return block

    @staticmethod
    def _assistant_blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
        """将 OpenAI assistant 消息转换为 Anthropic 的 content 块列表。

        依次组装 thinking 块（扩展思考）、text 块（正文）以及 tool_use 块
        （工具调用），保留 Anthropic 协议所需的 signature 与 input 字段。
        """
        blocks: list[dict[str, Any]] = []
        content = msg.get("content")

        # 先回放 thinking_blocks（扩展思考），保持 thinking/signature 配对
        for tb in msg.get("thinking_blocks") or []:
            if isinstance(tb, dict) and tb.get("type") == "thinking":
                blocks.append({
                    "type": "thinking",
                    "thinking": tb.get("thinking", ""),
                    "signature": tb.get("signature", ""),
                })

        if isinstance(content, str) and content:
            blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for item in content:
                blocks.append(item if isinstance(item, dict) else {"type": "text", "text": str(item)})

        # 工具调用转换为 Anthropic 的 tool_use 块
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            args = func.get("arguments", "{}")
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id") or _gen_tool_id(),
                "name": func.get("name", ""),
                "input": tool_arguments_object_for_replay(args),
            })

        return blocks or [{"type": "text", "text": ""}]

    @staticmethod
    def _convert_user_content(content: Any) -> Any:
        """转换 user 消息内容，重点是把 OpenAI 的 image_url 块翻译为 Anthropic image 块。"""
        if isinstance(content, str) or content is None:
            return content or "(empty)"
        if not isinstance(content, list):
            return str(content)

        result: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                result.append({"type": "text", "text": str(item)})
                continue
            if item.get("type") == "image_url":
                converted = AnthropicProvider._convert_image_block(item)
                if converted:
                    result.append(converted)
                continue
            if not item.get("type"):
                # Anthropic 要求每个 content 块必须声明 type 字段。
                # 当工具返回了裸 dict（或 dict 列表）时会落到这里；
                # 强制转为 text 块，避免 API 报错 "content.0.type: Field required"。
                result.append({"type": "text", "text": str(item)})
                continue
            result.append(item)
        return result or "(empty)"

    @staticmethod
    def _convert_image_block(block: dict[str, Any]) -> dict[str, Any] | None:
        """将 OpenAI 的 image_url 块转换为 Anthropic image 块。

        支持 base64 data URL（解析为 base64 源）与普通 http(s) URL（url 源）。
        """
        url = (block.get("image_url") or {}).get("url", "")
        if not url:
            return None
        m = re.match(r"data:(image/\w+);base64,(.+)", url, re.DOTALL)
        if m:
            # base64 内联图片
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)},
            }
        # 远程图片 URL
        return {
            "type": "image",
            "source": {"type": "url", "url": url},
        }

    @staticmethod
    def _has_tool_use(msg: dict[str, Any]) -> bool:
        """判断 ``msg.content`` 中是否携带 ``tool_use`` 块。

        Anthropic 禁止在 ``user`` 轮次中出现 ``tool_use``，因此在调整消息
        角色时，已发起过工具调用的消息不能被简单地重路由为 user。
        """
        content = msg.get("content")
        if not isinstance(content, list):
            return False
        return any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        )

    @staticmethod
    def _merge_consecutive(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对消息序列做规范化，以适配 Anthropic ``/messages`` 端点的约束。

        Anthropic 的协议比 OpenAI 更严格：

        1. 连续相同角色的消息必须合并为一条。
        2. 会话不能以 ``assistant`` 轮次结尾 —— Anthropic 不支持 assistant
           消息预填充（prefill），会返回 400。
        3. 会话不能以 ``assistant`` 轮次起始 —— 首条消息必须是 ``user``。

        规则 2、3 与 ``base.py`` 中 ``LLMProvider._enforce_role_alternation``
        针对 OpenAI 兼容 Provider 的约束保持一致。Anthropic 的特殊之处在于：
        ``tool_use`` 块位于 ``content`` 内部（而非独立的 ``tool_calls`` 字段），
        且不允许出现在 ``user`` 轮次中，因此下方的恢复路径必须跳过任何携带
        ``tool_use`` 的消息，而不是静默产生畸形请求。
        """
        merged: list[dict[str, Any]] = []
        for msg in msgs:
            # 规则 1：连续同角色合并（将 content 统一为 list 后拼接）
            if merged and merged[-1]["role"] == msg["role"]:
                prev_c = merged[-1]["content"]
                cur_c = msg["content"]
                if isinstance(prev_c, str):
                    prev_c = [{"type": "text", "text": prev_c}]
                if isinstance(cur_c, str):
                    cur_c = [{"type": "text", "text": cur_c}]
                if isinstance(cur_c, list):
                    prev_c.extend(cur_c)
                merged[-1]["content"] = prev_c
            else:
                merged.append(msg)

        # 规则 2：剥离尾部的 assistant 轮次 —— Anthropic 不支持 prefill
        last_popped: dict[str, Any] | None = None
        while merged and merged[-1].get("role") == "assistant":
            last_popped = merged.pop()

        # 规则 2 的恢复：若剥离后已无任何轮次，则把最后被弹出的 assistant
        # 重路由为 user，使上游仍能拿到合法请求，避免触发 "messages array empty" 400。
        # 若消息携带 tool_use 块则跳过（参见 _has_tool_use）。
        if (
            not merged
            and last_popped is not None
            and not AnthropicProvider._has_tool_use(last_popped)
        ):
            merged.append({"role": "user", "content": last_popped.get("content")})

        # 规则 3：若首条幸存轮次是 assistant（例如上游历史截断丢弃了原始
        # user 请求），则在前面插入一条合成的 user 开场消息。
        # 携带 tool_use 的 assistant 不处理 —— 该消息本身仍会校验失败，
        # 但在它前面插入开场消息会使后续的 tool_use/tool_result 配对孤立，
        # 把可恢复的 400 变成更难诊断的错误。
        if (
            merged
            and merged[0].get("role") == "assistant"
            and not AnthropicProvider._has_tool_use(merged[0])
        ):
            merged.insert(0, {"role": "user", "content": "(conversation continued)"})

        return merged

    # ------------------------------------------------------------------
    # 工具定义转换
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """将 OpenAI 风格工具定义转换为 Anthropic 工具定义。

        OpenAI 使用 ``function.parameters``，Anthropic 使用 ``input_schema``；
        同时保留可选的 ``description`` 与 ``cache_control``（prompt 缓存标记）。
        """
        if not tools:
            return None
        result = []
        for tool in tools:
            func = tool.get("function", tool)
            entry: dict[str, Any] = {
                "name": func.get("name", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            }
            desc = func.get("description")
            if desc:
                entry["description"] = desc
            if "cache_control" in tool:
                entry["cache_control"] = tool["cache_control"]
            result.append(entry)
        return result

    @staticmethod
    def _convert_tool_choice(
        tool_choice: str | dict[str, Any] | None,
        thinking_enabled: bool = False,
    ) -> dict[str, Any] | None:
        """将 OpenAI 的 tool_choice 语义映射到 Anthropic 的 tool_choice。

        - 开启扩展思考时强制 ``auto``（Anthropic 限制）。
        - ``auto`` / ``required`` / ``none`` / 指定工具 分别映射到 Anthropic
          的 ``auto`` / ``any`` / ``None`` / ``tool``。
        """
        if thinking_enabled:
            return {"type": "auto"}
        if tool_choice is None or tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return None
        if isinstance(tool_choice, dict):
            name = tool_choice.get("function", {}).get("name")
            if name:
                return {"type": "tool", "name": name}
        return {"type": "auto"}

    # ------------------------------------------------------------------
    # Prompt 缓存
    # ------------------------------------------------------------------

    @classmethod
    def _apply_cache_control(
        cls,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]] | None]:
        """为 system、倒数第二条消息、工具定义添加 prompt 缓存标记。

        Anthropic 的 prompt 缓存以 ``cache_control: {"type": "ephemeral"}``
        标记缓存断点。这里在 system、对话尾部倒数第二条消息，以及工具列表
        的边界索引处插入断点，最大化缓存命中率（system + 历史 + 工具定义
        是最稳定的可缓存前缀）。
        """
        marker = {"type": "ephemeral"}  # ephemeral 表示短期缓存

        # system：字符串形式包装为带 cache_control 的 text 块；列表形式则标记最后一个块
        if isinstance(system, str) and system:
            system = [{"type": "text", "text": system, "cache_control": marker}]
        elif isinstance(system, list) and system:
            system = list(system)
            system[-1] = {**system[-1], "cache_control": marker}

        # 倒数第二条消息：缓存稳定的对话前缀（最后一条通常是新输入）
        new_msgs = list(messages)
        if len(new_msgs) >= 3:
            m = new_msgs[-2]
            c = m.get("content")
            if isinstance(c, str):
                new_msgs[-2] = {**m, "content": [{"type": "text", "text": c, "cache_control": marker}]}
            elif isinstance(c, list) and c:
                nc = list(c)
                nc[-1] = {**nc[-1], "cache_control": marker}
                new_msgs[-2] = {**m, "content": nc}

        # 工具定义：在内置/MCP 边界与尾部添加缓存断点
        new_tools = tools
        if tools:
            new_tools = list(tools)
            for idx in cls._tool_cache_marker_indices(new_tools):
                new_tools[idx] = {**new_tools[idx], "cache_control": marker}

        return system, new_msgs, new_tools

    # ------------------------------------------------------------------
    # 构造 API 调用参数
    # ------------------------------------------------------------------

    def _build_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
        supports_caching: bool = True,
    ) -> dict[str, Any]:
        """根据上层入参组装 Anthropic SDK 的请求参数字典。

        内部完成模型名前缀剥离、消息格式转换、prompt 缓存标记注入、
        扩展思考（adaptive/enabled）预算计算、temperature 在部分模型上
        的省略处理，以及工具定义与 tool_choice 的映射。
        """
        model_name = self._strip_prefix(model or self.default_model)
        # 先做空内容清洗，再转换为 Anthropic 消息结构
        system, anthropic_msgs = self._convert_messages(self._sanitize_empty_content(messages))
        anthropic_tools = self._convert_tools(tools)

        if supports_caching:
            # 注入 prompt 缓存断点
            system, anthropic_msgs, anthropic_tools = self._apply_cache_control(
                system, anthropic_msgs, anthropic_tools,
            )

        max_tokens = max(1, max_tokens)
        # 是否启用扩展思考：reasoning_effort 非空且不为 "none"
        thinking_enabled = bool(reasoning_effort) and reasoning_effort.lower() != "none"

        # 部分模型（opus-4-7、opus-4-8、fable）已废弃 temperature 参数，
        # 传入会导致 API 返回 400，需要识别后省略。
        _model_lower = model_name.lower()
        omit_temperature = any(m in _model_lower for m in ("opus-4-7", "opus-4-8", "fable"))

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": anthropic_msgs,
            "max_tokens": max_tokens,
        }

        if system:
            kwargs["system"] = system

        if reasoning_effort == "adaptive":
            # 自适应思考：由模型自行决定何时思考及思考多少。
            # 支持 claude-sonnet-4-6 与 claude-opus-4-6，并自动开启工具调用间的交错思考。
            kwargs["thinking"] = {"type": "adaptive"}
            if not omit_temperature:
                kwargs["temperature"] = 1.0
        elif thinking_enabled:
            # 固定预算思考：按 low/medium/high 映射到 token 预算
            budget_map = {"low": 1024, "medium": 4096, "high": max(8192, max_tokens)}
            budget = budget_map.get((reasoning_effort or "").lower(), 4096)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # max_tokens 需要容纳思考预算，额外预留 4096 给正文输出
            kwargs["max_tokens"] = max(max_tokens, budget + 4096)
            if not omit_temperature:
                kwargs["temperature"] = 1.0
        elif not omit_temperature:
            kwargs["temperature"] = temperature

        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
            tc = self._convert_tool_choice(tool_choice, thinking_enabled)
            if tc:
                kwargs["tool_choice"] = tc

        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        return kwargs

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(response: Any) -> LLMResponse:
        """将 Anthropic SDK 返回的响应对象解析为统一的 :class:`LLMResponse`。

        遍历 ``response.content`` 中的各个块，按类型分别收集正文文本、
        工具调用与扩展思考块；将 ``stop_reason`` 映射为统一的 finish_reason；
        并把 usage 中的 prompt 缓存 token 项归一化为 ``cached_tokens``。
        """
        content_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        thinking_blocks: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCallRequest(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))
            elif block.type == "thinking":
                # 保留扩展思考块及其签名，便于在多轮对话中回放
                thinking_blocks.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": getattr(block, "signature", ""),
                })

        # 将 Anthropic 的 stop_reason 映射为统一的 finish_reason
        stop_map = {"tool_use": "tool_calls", "end_turn": "stop", "max_tokens": "length"}
        finish_reason = stop_map.get(response.stop_reason or "", response.stop_reason or "stop")

        usage: dict[str, int] = {}
        if response.usage:
            # prompt token 总数 = 原始输入 + 缓存创建 + 缓存命中
            input_tokens = response.usage.input_tokens
            cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            total_prompt_tokens = input_tokens + cache_creation + cache_read
            usage = {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": total_prompt_tokens + response.usage.output_tokens,
            }
            for attr in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                val = getattr(response.usage, attr, 0)
                if val:
                    usage[attr] = val
            # 归一化为 cached_tokens，便于下游统一处理缓存命中统计
            if cache_read:
                usage["cached_tokens"] = cache_read

        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            thinking_blocks=thinking_blocks or None,
        )

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    @staticmethod
    def _is_streaming_required_error(e: Exception) -> bool:
        """判断是否为 Anthropic SDK 拒绝非流式长请求的 ``ValueError``。

        SDK 在 max_tokens（加扩展思考预算）可能超过 10 分钟服务端超时
        时，会以 'Streaming is required' 开头的 ValueError 拒绝非流式调用。
        这里用子串匹配做防御性判断，避免 SDK 文案调整导致检测失效。
        """
        return isinstance(e, ValueError) and "streaming is required" in str(e).lower()

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
        """非流式聊天补全请求。

        若 SDK 因请求过长而拒绝非流式调用（'Streaming is required'），
        会自动回退到流式路径，使调用方无需感知 Provider 特有的限制。
        其他异常统一交由 :meth:`_handle_error` 归一化。
        """
        kwargs = self._build_kwargs(
            messages, tools, model, max_tokens, temperature,
            reasoning_effort, tool_choice,
        )
        try:
            response = await self._client.messages.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            if self._is_streaming_required_error(e):
                # Anthropic SDK 在 max_tokens（加扩展思考预算）可能超过
                # 10 分钟服务端超时时会拒绝非流式调用（#2709）。
                # 这里透明地回退到流式路径，使调用方无需感知 Provider 的限制。
                return await self.chat_stream(
                    messages=messages,
                    tools=tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                )
            return self._handle_error(e)

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
        """流式聊天补全请求，逐块回调正文 / 思考 / 工具调用的增量。

        通过 ``asyncio.wait_for`` 为每个 SSE chunk 设置空闲超时，避免连接
        假死时无限等待。超时或异常时分别返回 timeout / 错误响应。
        """
        kwargs = self._build_kwargs(
            messages, tools, model, max_tokens, temperature,
            reasoning_effort, tool_choice,
        )
        idle_timeout_s = resolve_stream_idle_timeout_s()
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                if on_content_delta or on_thinking_delta or on_tool_call_delta:
                    # 空闲超时必须覆盖任意 SSE chunk（thinking_delta、
                    # tool JSON delta 等），而不能只盯 text_stream 的 token。
                    # 否则扩展思考可能让 text_stream 长时间停滞而连接其实
                    # 是健康的（例如 MiniMax Anthropic）。
                    tool_blocks: dict[int, dict[str, str]] = {}
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                stream.__anext__(),
                                timeout=idle_timeout_s,
                            )
                        except StopAsyncIteration:
                            break
                        if chunk.type == "content_block_start":
                            # 工具调用块起始：记录 call_id 与 name，供后续 input_json_delta 回调使用
                            block = getattr(chunk, "content_block", None)
                            if getattr(block, "type", None) == "tool_use":
                                index = int(getattr(chunk, "index", 0) or 0)
                                state = {
                                    "call_id": str(getattr(block, "id", "") or ""),
                                    "name": str(getattr(block, "name", "") or ""),
                                }
                                tool_blocks[index] = state
                                if on_tool_call_delta:
                                    await on_tool_call_delta({
                                        "index": index,
                                        **state,
                                        "arguments_delta": "",
                                    })
                        elif (
                            chunk.type == "content_block_delta"
                            and getattr(chunk.delta, "type", None) == "thinking_delta"
                        ):
                            # 扩展思考增量
                            piece = getattr(chunk.delta, "thinking", None) or ""
                            if piece and on_thinking_delta:
                                await on_thinking_delta(piece)
                        elif (
                            chunk.type == "content_block_delta"
                            and getattr(chunk.delta, "type", None) == "text_delta"
                        ):
                            # 正文增量
                            text = getattr(chunk.delta, "text", None) or ""
                            if text and on_content_delta:
                                await on_content_delta(text)
                        elif (
                            chunk.type == "content_block_delta"
                            and getattr(chunk.delta, "type", None) == "input_json_delta"
                        ):
                            # 工具调用入参 JSON 的增量片段
                            partial = getattr(chunk.delta, "partial_json", None) or ""
                            if partial and on_tool_call_delta:
                                index = int(getattr(chunk, "index", 0) or 0)
                                state = tool_blocks.get(index, {})
                                await on_tool_call_delta({
                                    "index": index,
                                    "call_id": state.get("call_id", ""),
                                    "name": state.get("name", ""),
                                    "arguments_delta": partial,
                                })
                # 获取流式终态消息（同样受空闲超时约束）
                response = await asyncio.wait_for(
                    stream.get_final_message(),
                    timeout=idle_timeout_s,
                )
            return self._parse_response(response)
        except asyncio.TimeoutError:
            # 流式空闲超时：返回 timeout 错误响应
            return LLMResponse(
                content=(
                    f"Error calling LLM: stream stalled for more than "
                    f"{idle_timeout_s:g} seconds"
                ),
                finish_reason="error",
                error_kind="timeout",
            )
        except Exception as e:
            return self._handle_error(e)

    def get_default_model(self) -> str:
        """返回该 Provider 的默认模型名。"""
        return self.default_model
