"""解析 Responses API 的 SSE 流与 SDK 响应对象。

本模块负责把 OpenAI Responses API 的两种返回形态统一成 biscuitbot 内部的
``LLMResponse`` / ``ToolCallRequest``：
- SSE 流（``iter_sse`` / ``consume_sse`` / ``consume_sse_with_reasoning``）；
- SDK 异步流（``consume_sdk_stream``）；
- 非流式 SDK 对象（``parse_response_output``）。
"""

from __future__ import annotations

import json  # 解析 SSE 中的 JSON 事件数据
from collections.abc import Awaitable, Callable  # 回调类型标注
from typing import Any, AsyncGenerator  # 动态类型与异步生成器标注

import httpx  # HTTP 响应类型（SSE 流来源）
from loguru import logger  # 结构化日志

from biscuitbot.providers.base import LLMResponse, ToolCallRequest, parse_tool_arguments  # 统一响应结构与参数解析

# Responses API 的 status → Chat Completions 风格 finish_reason 映射
FINISH_REASON_MAP = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "error",
    "cancelled": "error",
}


def map_finish_reason(status: str | None) -> str:
    """把 Responses API 的 status 字符串映射为 Chat Completions 风格的 finish_reason。"""
    return FINISH_REASON_MAP.get(status or "completed", "stop")


def _usage_from_response_obj(response: Any) -> dict[str, int]:
    """从 Responses API 响应对象中提取 token 用量。

    同时兼容 dict（原始 JSON）与 SDK Pydantic 对象：
    Responses API 用 ``input_tokens``/``output_tokens``，
    而 Chat Completions 用 ``prompt_tokens``/``completion_tokens``，
    这里统一成后者。
    """
    usage_raw = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if not usage_raw:
        return {}
    if not isinstance(usage_raw, dict):
        # SDK 对象：尝试 model_dump() 或 vars() 转 dict
        dump = getattr(usage_raw, "model_dump", None)
        usage_raw = dump() if callable(dump) else vars(usage_raw)
    prompt_tokens = int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 0)
    completion_tokens = int(
        usage_raw.get("output_tokens") or usage_raw.get("completion_tokens") or 0
    )
    total_tokens = int(usage_raw.get("total_tokens") or prompt_tokens + completion_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _parse_tool_call_arguments(args_raw: Any, name: str | None) -> Any:
    """解析工具调用参数，解析失败时告警但保留原值。"""
    parsed = parse_tool_arguments(args_raw)
    if parsed == args_raw and isinstance(args_raw, str) and args_raw.strip():
        logger.warning(
            "Failed to parse tool call arguments for '{}': {}",
            name,
            args_raw[:200],
        )
    return parsed


def _tool_arguments_source(*values: Any) -> Any:
    """按顺序返回第一个非空（非 None、非空白串）的值，全为空则返回 ``"{}"``。

    用于在多个候选来源（流式 buffer、最终 item）之间挑选真实的工具参数。
    """
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return "{}"


async def iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    """逐条产出 Responses API SSE 流中的 JSON 事件。

    按 SSE 规范：空行分隔事件，``data:`` 前缀承载 JSON。
    本函数把同一事件的多行 ``data:`` 合并后再解析，
    并在流末尾刷新剩余 buffer（#10）。
    """
    buffer: list[str] = []

    def _flush() -> dict[str, Any] | None:
        # 合并同一事件的多行 data，去掉 "data:" 前缀
        data_lines = [line[5:].strip() for line in buffer if line.startswith("data:")]
        buffer.clear()
        if not data_lines:
            return None
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            return None
        try:
            return json.loads(data)
        except Exception:
            logger.warning("Failed to parse SSE event JSON: {}", data[:200])
            return None

    async for line in response.aiter_lines():
        if line == "":
            # 空行表示一个事件结束，刷新 buffer
            if buffer:
                event = _flush()
                if event is not None:
                    yield event
            continue
        buffer.append(line)

    # 流结束时刷新剩余 buffer（#10）
    if buffer:
        event = _flush()
        if event is not None:
            yield event


async def consume_sse(
    response: httpx.Response,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str]:
    """消费 Responses API 的 SSE 流，返回 ``(content, tool_calls, finish_reason)``。

    不含推理摘要——如需推理内容请用 :func:`consume_sse_with_reasoning`。
    """
    content, tool_calls, finish_reason, _, _ = await consume_sse_with_reasoning(
        response,
        on_content_delta=on_content_delta,
        on_tool_call_delta=on_tool_call_delta,
    )
    return content, tool_calls, finish_reason


async def consume_sse_with_reasoning(
    response: httpx.Response,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int], str | None]:
    """消费 Responses API 的 SSE 流，并提取可见的推理摘要。

    返回 ``(content, tool_calls, finish_reason, usage, reasoning_content)``。
    通过三类回调把增量实时透传给上层（内容、工具调用、推理）。
    """
    content = ""
    tool_calls: list[ToolCallRequest] = []
    # 工具调用缓冲：call_id → {id, name, arguments}，用于累积流式参数
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    # 记录哪些 call_id 已通过 .done 事件发出过完整参数，避免重复推送
    tool_call_args_emitted: set[str] = set()
    finish_reason = "stop"
    usage: dict[str, int] = {}
    reasoning_content: str | None = None
    # 是否已通过 delta 流式收到推理（优先于一次性 done 事件）
    streamed_reasoning = False

    async for event in iter_sse(response):
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            # 新输出项加入：若是函数调用，初始化缓冲并通知上层
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                arguments = item.get("arguments")
                tool_call_buffers[call_id] = {
                    "id": item.get("id") or "fc_0",
                    "name": item.get("name"),
                    "arguments": "" if arguments is None else arguments,
                }
                if on_tool_call_delta:
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(item.get("name") or ""),
                        "arguments_delta": "",
                    })
        elif event_type == "response.output_text.delta":
            # 文本增量：累加并回调
            delta_text = event.get("delta") or ""
            content += delta_text
            if on_content_delta and delta_text:
                await on_content_delta(delta_text)
        elif event_type == "response.reasoning_summary_text.delta":
            # 推理摘要增量：累加并回调，标记已走流式
            delta_text = event.get("delta") or ""
            if delta_text:
                reasoning_content = (reasoning_content or "") + delta_text
                streamed_reasoning = True
                if on_reasoning_delta:
                    await on_reasoning_delta(delta_text)
        elif event_type == "response.reasoning_summary_text.done":
            # 推理摘要一次性完成事件：仅在未走流式时采用
            text = event.get("text") or ""
            if text and not streamed_reasoning and not reasoning_content:
                reasoning_content = text
                if on_reasoning_delta:
                    await on_reasoning_delta(text)
        elif event_type == "response.reasoning_summary_part.done":
            # 推理摘要分片完成事件：同上优先级
            part = event.get("part") or {}
            text = part.get("text") if part.get("type") == "summary_text" else None
            if text and not streamed_reasoning and not reasoning_content:
                reasoning_content = text
                if on_reasoning_delta:
                    await on_reasoning_delta(text)
        elif event_type == "response.function_call_arguments.delta":
            # 工具参数增量：累加到对应缓冲并回调
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                delta = event.get("delta") or ""
                current = tool_call_buffers[call_id].get("arguments")
                if not isinstance(current, str):
                    current = ""
                tool_call_buffers[call_id]["arguments"] = current + delta
                if on_tool_call_delta and delta:
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(tool_call_buffers[call_id].get("name") or ""),
                        "arguments_delta": str(delta),
                    })
        elif event_type == "response.function_call_arguments.done":
            # 工具参数整体完成：用最终值覆盖缓冲并回调完整参数
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                arguments = event.get("arguments")
                tool_call_buffers[call_id]["arguments"] = arguments
                if on_tool_call_delta:
                    tool_call_args_emitted.add(str(call_id))
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(tool_call_buffers[call_id].get("name") or ""),
                        "arguments": "" if arguments is None else str(arguments),
                    })
        elif event_type == "response.output_item.done":
            # 输出项完成：若是函数调用，组装最终的 ToolCallRequest
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = _tool_arguments_source(buf.get("arguments"), item.get("arguments"))
                # 若未通过 .done 推过完整参数，这里补推一次
                if on_tool_call_delta and str(call_id) not in tool_call_args_emitted:
                    tool_call_args_emitted.add(str(call_id))
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(buf.get("name") or item.get("name") or ""),
                        "arguments": str(args_raw),
                    })
                args = _parse_tool_call_arguments(
                    args_raw,
                    buf.get("name") or item.get("name"),
                )
                # id 用 "call_id|item_id" 复合形式，便于回放时拆分
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                        name=buf.get("name") or item.get("name") or "",
                        arguments=args,
                    )
                )
            elif item.get("type") == "reasoning" and not reasoning_content:
                # 非流式推理项兜底：从 output 中提取摘要
                summary = _extract_reasoning_summary_from_output([item])
                if summary:
                    reasoning_content = summary
                    if on_reasoning_delta:
                        await on_reasoning_delta(summary)
        elif event_type == "response.completed":
            # 响应完成：取最终状态与用量
            response_obj = event.get("response") or {}
            status = response_obj.get("status")
            finish_reason = map_finish_reason(status)
            usage = _usage_from_response_obj(response_obj) or usage
            # 若全程未拿到推理，从完成事件的 output 兜底提取
            if not reasoning_content:
                summary = _extract_reasoning_summary_from_output(response_obj.get("output") or [])
                if summary:
                    reasoning_content = summary
                    if on_reasoning_delta:
                        await on_reasoning_delta(summary)
        elif event_type in {"error", "response.failed"}:
            # 错误事件：抛出运行时异常，截断前 500 字避免日志过长
            detail = event.get("error") or event.get("message") or event
            raise RuntimeError(f"Response failed: {str(detail)[:500]}")

    return content, tool_calls, finish_reason, usage, reasoning_content


def _extract_reasoning_summary_from_output(output: Any) -> str | None:
    """从 Responses API 的 output 数组中提取推理摘要文本。

    遍历 ``type == "reasoning"`` 的项，拼接其 ``summary`` 中
    ``type == "summary_text"`` 的 ``text`` 字段。
    """
    parts: list[str] = []
    for item in output or []:
        if not isinstance(item, dict):
            dump = getattr(item, "model_dump", None)
            item = dump() if callable(dump) else vars(item)
        if item.get("type") != "reasoning":
            continue
        for summary in item.get("summary") or []:
            if not isinstance(summary, dict):
                dump = getattr(summary, "model_dump", None)
                summary = dump() if callable(dump) else vars(summary)
            if summary.get("type") == "summary_text" and summary.get("text"):
                parts.append(summary["text"])
    return "".join(parts) or None


def parse_response_output(response: Any) -> LLMResponse:
    """把 SDK 的 ``Response`` 对象解析为 ``LLMResponse``（非流式）。

    处理三类输出项：
    - ``message``：取 ``output_text`` 块作为正文；
    - ``reasoning``：取 ``summary_text`` 作为推理内容；
    - ``function_call``：组装为 :class:`ToolCallRequest`。
    """
    if not isinstance(response, dict):
        # SDK Pydantic 对象转 dict
        dump = getattr(response, "model_dump", None)
        response = dump() if callable(dump) else vars(response)

    output = response.get("output") or []
    content_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    reasoning_content: str | None = None

    for item in output:
        if not isinstance(item, dict):
            dump = getattr(item, "model_dump", None)
            item = dump() if callable(dump) else vars(item)

        item_type = item.get("type")
        if item_type == "message":
            # 正文消息：收集 output_text 块
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    dump = getattr(block, "model_dump", None)
                    block = dump() if callable(dump) else vars(block)
                if block.get("type") == "output_text":
                    content_parts.append(block.get("text") or "")
        elif item_type == "reasoning":
            # 推理项：累加 summary_text
            for s in item.get("summary") or []:
                if not isinstance(s, dict):
                    dump = getattr(s, "model_dump", None)
                    s = dump() if callable(dump) else vars(s)
                if s.get("type") == "summary_text" and s.get("text"):
                    reasoning_content = (reasoning_content or "") + s["text"]
        elif item_type == "function_call":
            # 函数调用：id 用复合形式
            call_id = item.get("call_id") or ""
            item_id = item.get("id") or "fc_0"
            args_raw = _tool_arguments_source(item.get("arguments"))
            args = _parse_tool_call_arguments(args_raw, item.get("name"))
            tool_calls.append(ToolCallRequest(
                id=f"{call_id}|{item_id}",
                name=item.get("name") or "",
                arguments=args,
            ))

    usage = _usage_from_response_obj(response)

    status = response.get("status")
    finish_reason = map_finish_reason(status)

    return LLMResponse(
        content="".join(content_parts) or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        reasoning_content=reasoning_content if isinstance(reasoning_content, str) else None,
    )


async def consume_sdk_stream(
    stream: Any,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int], str | None]:
    """消费 ``client.responses.create(stream=True)`` 返回的 SDK 异步流。

    与 :func:`consume_sse_with_reasoning` 逻辑等价，
    区别在于事件来源是 SDK 对象（用 ``getattr`` 取属性）而非 dict（用 ``.get``）。
    返回 ``(content, tool_calls, finish_reason, usage, reasoning_content)``。
    """
    content = ""
    tool_calls: list[ToolCallRequest] = []
    # 工具调用缓冲：call_id → {id, name, arguments}
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    tool_call_args_emitted: set[str] = set()
    finish_reason = "stop"
    usage: dict[str, int] = {}
    reasoning_content: str | None = None

    async for event in stream:
        event_type = getattr(event, "type", None)
        if event_type == "response.output_item.added":
            # 新函数调用项：初始化缓冲并通知
            item = getattr(event, "item", None)
            if item and getattr(item, "type", None) == "function_call":
                call_id = getattr(item, "call_id", None)
                if not call_id:
                    continue
                arguments = getattr(item, "arguments", None)
                tool_call_buffers[call_id] = {
                    "id": getattr(item, "id", None) or "fc_0",
                    "name": getattr(item, "name", None),
                    "arguments": "" if arguments is None else arguments,
                }
                if on_tool_call_delta:
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(getattr(item, "name", None) or ""),
                        "arguments_delta": "",
                    })
        elif event_type == "response.output_text.delta":
            # 文本增量
            delta_text = getattr(event, "delta", "") or ""
            content += delta_text
            if on_content_delta and delta_text:
                await on_content_delta(delta_text)
        elif event_type == "response.function_call_arguments.delta":
            # 工具参数增量
            call_id = getattr(event, "call_id", None)
            if call_id and call_id in tool_call_buffers:
                delta = getattr(event, "delta", "") or ""
                current = tool_call_buffers[call_id].get("arguments")
                if not isinstance(current, str):
                    current = ""
                tool_call_buffers[call_id]["arguments"] = current + delta
                if on_tool_call_delta and delta:
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(tool_call_buffers[call_id].get("name") or ""),
                        "arguments_delta": str(delta),
                    })
        elif event_type == "response.function_call_arguments.done":
            # 工具参数整体完成
            call_id = getattr(event, "call_id", None)
            if call_id and call_id in tool_call_buffers:
                arguments = getattr(event, "arguments", None)
                tool_call_buffers[call_id]["arguments"] = arguments
                if on_tool_call_delta:
                    tool_call_args_emitted.add(str(call_id))
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(tool_call_buffers[call_id].get("name") or ""),
                        "arguments": "" if arguments is None else str(arguments),
                    })
        elif event_type == "response.output_item.done":
            # 输出项完成：组装最终 ToolCallRequest
            item = getattr(event, "item", None)
            if item and getattr(item, "type", None) == "function_call":
                call_id = getattr(item, "call_id", None)
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = _tool_arguments_source(
                    buf.get("arguments"),
                    getattr(item, "arguments", None),
                )
                if on_tool_call_delta and str(call_id) not in tool_call_args_emitted:
                    tool_call_args_emitted.add(str(call_id))
                    await on_tool_call_delta({
                        "call_id": str(call_id),
                        "name": str(buf.get("name") or getattr(item, "name", None) or ""),
                        "arguments": str(args_raw),
                    })
                args = _parse_tool_call_arguments(
                    args_raw,
                    buf.get("name") or getattr(item, "name", None),
                )
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or getattr(item, 'id', None) or 'fc_0'}",
                        name=buf.get("name") or getattr(item, "name", None) or "",
                        arguments=args,
                    )
                )
        elif event_type == "response.completed":
            # 响应完成：取状态、用量、推理摘要
            resp = getattr(event, "response", None)
            status = getattr(resp, "status", None) if resp else None
            finish_reason = map_finish_reason(status)
            if resp:
                usage_obj = getattr(resp, "usage", None)
                if usage_obj:
                    usage = {
                        "prompt_tokens": int(getattr(usage_obj, "input_tokens", 0) or 0),
                        "completion_tokens": int(getattr(usage_obj, "output_tokens", 0) or 0),
                        "total_tokens": int(getattr(usage_obj, "total_tokens", 0) or 0),
                    }
                # 从完成事件的 output 兜底提取推理摘要
                for out_item in getattr(resp, "output", None) or []:
                    if getattr(out_item, "type", None) == "reasoning":
                        for s in getattr(out_item, "summary", None) or []:
                            if getattr(s, "type", None) == "summary_text":
                                text = getattr(s, "text", None)
                                if text:
                                    reasoning_content = (reasoning_content or "") + text
        elif event_type in {"error", "response.failed"}:
            # 错误事件：抛出异常
            detail = getattr(event, "error", None) or getattr(event, "message", None) or event
            raise RuntimeError(f"Response failed: {str(detail)[:500]}")

    return content, tool_calls, finish_reason, usage, reasoning_content
