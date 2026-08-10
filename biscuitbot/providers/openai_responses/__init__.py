"""OpenAI Responses API Provider 的共享工具集（Codex、Azure OpenAI 等）。

本包把"消息/工具转换"与"SSE/响应解析"两类逻辑分别放在
:mod:`biscuitbot.providers.openai_responses.converters` 与
:mod:`biscuitbot.providers.openai_responses.parsing`，
本 ``__init__`` 仅做统一再导出，方便上层（如 ``openai_compat_provider``）一次性导入。
"""

# 消息与工具格式转换：把 Chat Completions 风格转换为 Responses API 所需的扁平结构
from biscuitbot.providers.openai_responses.converters import (
    convert_messages,
    convert_tools,
    convert_user_message,
    split_tool_call_id,
)
# SSE 流与 SDK 响应对象解析：把 Responses API 的输出统一成 LLMResponse
from biscuitbot.providers.openai_responses.parsing import (
    FINISH_REASON_MAP,
    consume_sdk_stream,
    consume_sse,
    consume_sse_with_reasoning,
    iter_sse,
    map_finish_reason,
    parse_response_output,
)

__all__ = [
    "convert_messages",
    "convert_tools",
    "convert_user_message",
    "split_tool_call_id",
    "iter_sse",
    "consume_sse",
    "consume_sse_with_reasoning",
    "consume_sdk_stream",
    "map_finish_reason",
    "parse_response_output",
    "FINISH_REASON_MAP",
]
