"""把 Chat Completions 风格的消息/工具转换为 Responses API 格式。

Responses API 与 Chat Completions 在请求结构上有较大差异：
- 系统提示通过顶层 ``instructions`` 传递，而非放在 messages 里；
- 历史消息、工具调用、工具结果统一拍平成 ``input`` 数组；
- 工具描述使用扁平的 ``{type, name, description, parameters}`` 而非嵌套 ``function``。
本模块负责完成这层结构翻译，供 ``openai_compat_provider`` 在走 Responses API 时调用。
"""

from __future__ import annotations

import json  # 把非字符串的 tool 结果序列化为 JSON 文本
from typing import Any  # 动态类型标注

from biscuitbot.providers.base import tool_arguments_json_for_replay  # 规范化工具参数为 JSON 字符串


def convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """把 Chat Completions 消息列表转换为 Responses API 的输入项。

    返回 ``(system_prompt, input_items)``：
    - *system_prompt* 抽取自 ``system`` 角色消息，作为顶层 ``instructions``；
    - *input_items* 是 Responses API 的 ``input`` 数组，按顺序包含
      user/assistant/tool 各角色的等价表达。
    """
    system_prompt = ""
    input_items: list[dict[str, Any]] = []
    used_item_ids: set[str] = set()  # 记录已用 item id，保证全局唯一

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            # 系统消息抽取为顶层 instructions
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(convert_user_message(content))
            continue

        if role == "assistant":
            # 助手文本消息 → Responses 的 message 项
            if isinstance(content, str) and content:
                message_id = _unique_item_id(f"msg_{idx}", used_item_ids)
                input_items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                    "status": "completed", "id": message_id,
                })
            # 助手发起的工具调用 → function_call 项
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = split_tool_call_id(tool_call.get("id"))
                response_item_id = _unique_item_id(item_id or f"fc_{idx}", used_item_ids)
                input_items.append({
                    "type": "function_call",
                    "id": response_item_id,
                    "call_id": call_id or f"call_{idx}",
                    "name": fn.get("name"),
                    "arguments": tool_arguments_json_for_replay(fn.get("arguments")),
                })
            continue

        if role == "tool":
            # 工具返回结果 → function_call_output 项
            call_id, _ = split_tool_call_id(msg.get("tool_call_id"))
            output_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            input_items.append({"type": "function_call_output", "call_id": call_id, "output": output_text})

    return system_prompt, input_items


def convert_user_message(content: Any) -> dict[str, Any]:
    """把单条 user 消息的 content 转换为 Responses API 格式。

    支持三种形态：
    - 纯字符串 → ``input_text``；
    - ``text`` 块 → ``input_text``；
    - ``image_url`` 块 → ``input_image``。
    """
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    # 兜底：空内容也返回一个 input_text 项，保证消息结构完整
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 OpenAI 函数调用工具 schema 转换为 Responses API 的扁平格式。

    Chat Completions 用 ``{type:"function", function:{name, ...}}`` 嵌套结构，
    Responses API 则拍平为 ``{type:"function", name, description, parameters}``。
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": params if isinstance(params, dict) else {},
        })
    return converted


def _unique_item_id(item_id: str, used: set[str]) -> str:
    """在单次请求内生成唯一的 Responses input item id。

    若 ``item_id`` 未被占用则直接使用；否则追加 ``_2``、``_3`` 后缀直到唯一。
    """
    if item_id not in used:
        used.add(item_id)
        return item_id

    suffix = 2
    while f"{item_id}_{suffix}" in used:
        suffix += 1
    unique = f"{item_id}_{suffix}"
    used.add(unique)
    return unique


def split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    """拆分复合的 ``call_id|item_id`` 字符串。

    返回 ``(call_id, item_id)``，其中 *item_id* 可能为 ``None``。
    biscuitbot 把 Responses API 需要的 call_id 与 item_id 用 ``|`` 拼接存储，
    这里负责还原；无 ``|`` 时认为整体就是 call_id。
    """
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None
