"""进度回调辅助工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/bus 目录，提供将智能体执行进度回调转换为出站聊天消息的工具。
在项目架构中起到的作用：把智能体在处理过程中产生的进度（工具调用提示、推理片段、
文件编辑事件等）封装为 OutboundMessage 发布到消息总线，供渠道实时展示给用户。
运行时状态通知（如 turn 生命周期、模型切换）则位于 ``biscuitbot.bus.runtime_events``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable  # 用于标注可调用对象与可等待对象
from typing import Any  # 用于任意类型标注

from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 消息数据类
from biscuitbot.bus.queue import MessageBus  # 异步消息总线


def build_bus_progress_callback(
    bus: MessageBus,
    msg: InboundMessage,
) -> Callable[..., Awaitable[None]]:
    """构造一个将进度发布为出站消息的回调函数。

    根据传入的总线和入站消息上下文，返回一个异步回调。智能体调用该回调即可
    将进度内容通过总线推送回原始渠道，并附带进度相关的元数据标记。
    """

    async def _publish_progress(
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        """实际发布进度消息的内部函数，组装元数据并调用总线发布。"""
        # 复制原始入站消息的元数据作为基础，避免修改原始对象
        meta = dict(msg.metadata or {})
        meta["_progress"] = True  # 标记为进度消息
        meta["_tool_hint"] = tool_hint  # 是否为工具调用提示
        if reasoning:
            meta["_reasoning_delta"] = True  # 标记为推理增量片段
        if reasoning_end:
            meta["_reasoning_end"] = True  # 标记推理结束
        if tool_events:
            meta["_tool_events"] = tool_events  # 工具调用事件列表
        if file_edit_events:
            meta["_file_edit_events"] = file_edit_events  # 文件编辑事件列表
        await bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=meta,
            )
        )

    async def _bus_progress(
        content: str,
        *,
        tool_hint: bool = False,
        tool_events: list[dict[str, Any]] | None = None,
        file_edit_events: list[dict[str, Any]] | None = None,
        reasoning: bool = False,
        reasoning_end: bool = False,
    ) -> None:
        """对外暴露的进度回调，转发参数给内部发布函数。"""
        await _publish_progress(
            content,
            tool_hint=tool_hint,
            tool_events=tool_events,
            file_edit_events=file_edit_events,
            reasoning=reasoning,
            reasoning_end=reasoning_end,
        )

    return _bus_progress
