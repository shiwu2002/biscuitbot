"""内部 turn 续接辅助工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/session 目录，提供预算边界（budget-boundary）的 turn 续接策略。
在项目架构中起到的作用：将续接策略从 ``AgentLoop`` 中剥离出来。智能体循环调用本模块
的少量辅助函数；这些函数决定是否允许内部续接，并在允许时直接将下一个 turn 入队，
从而在工具调用预算耗尽时仍能继续推进持久化目标。
"""

from __future__ import annotations

import dataclasses  # 用于替换入站消息字段构造续接消息
from typing import Any, Mapping, MutableMapping  # 类型标注

from loguru import logger  # 日志输出

from biscuitbot.session.goal_state import (  # 持久化目标状态辅助
    goal_state_runtime_lines,
    sustained_goal_active,
    sustained_goal_turn,
)

INTERNAL_CONTINUATION_META = "_internal_continuation"  # 标记入站消息由内部续接策略生成
INTERNAL_CONTINUATION_KIND_META = "_internal_continuation_kind"  # 续接类型
INTERNAL_CONTINUATION_PENDING_META = "_internal_continuation_pending"  # 当前 turn 已调度续接切片
INTERNAL_CONTINUATION_RUN_STARTED_AT_META = "_internal_continuation_run_started_at"  # 用户可见运行起始时间
SKIP_USER_PERSIST_META = "_skip_user_persist"  # 跳过将入站消息作为用户输入持久化

_GOAL_CONTINUATION_KIND = "sustained_goal"  # 持久化目标续接类型标识
_GOAL_CONTINUATION_SENDER = "system:continuation"  # 续接消息的发送者标识
_GOAL_CONTINUATION_ROUNDS_KEY = "_sustained_goal_continuation_rounds"  # 已续接轮数计数键
_MAX_GOAL_CONTINUATION_ROUNDS = 12  # 单个目标最大续接轮数
# 续接元数据中需要剥离的入站消息键（避免携带过期的流式状态）
_STRIPPED_INBOUND_META_KEYS = {
    "_stream_id",
    "_stream_delta",
    "_stream_end",
    "_resuming",
    INTERNAL_CONTINUATION_PENDING_META,
}


def internal_continuation_inbound(metadata: Mapping[str, Any] | None) -> bool:
    """判断入站消息是否由内部续接策略生成。"""
    return bool(metadata and metadata.get(INTERNAL_CONTINUATION_META) is True)


def internal_continuation_pending(metadata: Mapping[str, Any] | None) -> bool:
    """判断当前 turn 是否已调度不可见的续接切片。"""
    return bool(metadata and metadata.get(INTERNAL_CONTINUATION_PENDING_META) is True)


def internal_continuation_run_started_at(metadata: Mapping[str, Any] | None) -> float | None:
    """返回跨续接切片传递的用户可见运行起始时间。"""
    if not metadata:
        return None
    value = metadata.get(INTERNAL_CONTINUATION_RUN_STARTED_AT_META)
    if not isinstance(value, int | float):
        return None
    started_at = float(value)
    return started_at if started_at > 0 else None


def should_persist_user_message(metadata: Mapping[str, Any] | None) -> bool:
    """判断入站消息是否应作为用户输入持久化。"""
    if metadata and metadata.get(SKIP_USER_PERSIST_META) is True:
        return False
    return not internal_continuation_inbound(metadata)


def should_stream_budget_response(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """判断预算边界响应是否应发送给用户。"""
    if stop_reason != "max_iterations":
        return True
    return should_finalize_on_max_iterations(
        pending_queue_available=pending_queue_available,
        session_metadata=session_metadata,
        message_metadata=message_metadata,
    )


def should_finalize_on_max_iterations(
    *,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """判断 max-iteration 边界是否应产生最终响应。

    当持久化目标可继续内部续接时，当前 runner 切片应停止且不额外发起无工具的
    最终化调用。最终的用户可见响应由下一个入队的续接切片负责。
    """
    return not (
        pending_queue_available
        and _goal_continuation_available(
            session_metadata,
            message_metadata=message_metadata,
        )
    )


async def maybe_continue_turn(ctx: Any) -> bool:
    """当策略允许时为 *ctx* 入队一个内部续接。"""
    if ctx.session is None or ctx.pending_queue is None:
        return False
    if not _continuation_available(
        stop_reason=ctx.stop_reason,
        pending_queue_available=True,
        session_metadata=ctx.session.metadata,
        message_metadata=ctx.msg.metadata,
    ):
        return False

    # 构造续接消息的元数据，继承用户可见运行起始时间
    metadata = _internal_continuation_metadata(
        ctx.msg.metadata,
        run_started_at=getattr(ctx, "visible_run_started_at", None),
    )
    content = _goal_continuation_prompt(ctx.session.metadata)
    messages = _strip_terminal_assistant(ctx.all_messages, ctx.final_content)
    _increment_goal_continuation_round(ctx.session.metadata)

    logger.info("Turn budget reached; scheduling internal continuation")
    # 标记当前 turn 已挂起续接，并清空最终内容与抑制响应
    ctx.msg.metadata[INTERNAL_CONTINUATION_PENDING_META] = True
    ctx.final_content = ""
    ctx.all_messages = messages
    ctx.suppress_response = True
    await ctx.pending_queue.put(
        dataclasses.replace(
            ctx.msg,
            sender_id=_GOAL_CONTINUATION_SENDER,
            content=content,
            media=[],
            metadata=metadata,
            session_key_override=ctx.session_key,
        )
    )
    return True


def prepare_save_boundary(ctx: Any) -> None:
    """准备续接记账与历史追加边界。"""
    if ctx.session is not None:
        clear_internal_continuation_state(ctx.session.metadata)

    ctx.save_skip = _save_skip_for_turn(
        message_metadata=ctx.msg.metadata,
        initial_message_count=len(ctx.initial_messages),
        history_count=len(ctx.history),
        user_persisted_early=ctx.user_persisted_early,
    )


def _continuation_available(
    *,
    stop_reason: str,
    pending_queue_available: bool,
    session_metadata: Mapping[str, Any] | None,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """判断当前是否允许内部续接。"""
    if stop_reason != "max_iterations" or not pending_queue_available:
        return False
    return _goal_continuation_available(
        session_metadata,
        message_metadata=message_metadata,
    )


def clear_internal_continuation_state(metadata: MutableMapping[str, Any]) -> None:
    """当所属运行时模式不再激活时重置策略记账。"""
    if not sustained_goal_active(metadata):
        metadata.pop(_GOAL_CONTINUATION_ROUNDS_KEY, None)


def _save_skip_for_turn(
    *,
    message_metadata: Mapping[str, Any] | None,
    initial_message_count: int,
    history_count: int,
    user_persisted_early: bool,
) -> int:
    """返回本 turn 持久化消息的追加边界索引。"""
    if message_metadata and message_metadata.get(SKIP_USER_PERSIST_META) is True:
        return initial_message_count
    if internal_continuation_inbound(message_metadata):
        return initial_message_count
    # build_messages 可能将当前消息合并到同角色的历史尾部。
    # 无论何种形态，runner 追加的消息都从 initial_message_count 开始。
    has_standalone_current = initial_message_count > 1 + history_count
    if has_standalone_current and not user_persisted_early:
        return initial_message_count - 1
    return initial_message_count


def _goal_continuation_available(
    session_metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
    max_rounds: int = _MAX_GOAL_CONTINUATION_ROUNDS,
) -> bool:
    """判断持久化目标是否仍可续接（满足 turn 条件、激活状态且未超轮数上限）。"""
    if not sustained_goal_turn(session_metadata, message_metadata=message_metadata):
        return False
    if not sustained_goal_active(session_metadata):
        return False
    try:
        rounds = int((session_metadata or {}).get(_GOAL_CONTINUATION_ROUNDS_KEY) or 0)
    except (TypeError, ValueError):
        rounds = 0
    return rounds < max(0, max_rounds)


def _increment_goal_continuation_round(session_metadata: MutableMapping[str, Any]) -> None:
    """将持久化目标的续接轮数加一。"""
    try:
        rounds = int(session_metadata.get(_GOAL_CONTINUATION_ROUNDS_KEY) or 0)
    except (TypeError, ValueError):
        rounds = 0
    session_metadata[_GOAL_CONTINUATION_ROUNDS_KEY] = rounds + 1


def _internal_continuation_metadata(
    message_metadata: Mapping[str, Any] | None,
    *,
    run_started_at: float | None = None,
) -> dict[str, Any]:
    """构造续接消息的元数据：标记为内部续接并剥离过期的流式状态键。"""
    metadata = dict(message_metadata or {})
    metadata[INTERNAL_CONTINUATION_META] = True
    metadata[INTERNAL_CONTINUATION_KIND_META] = _GOAL_CONTINUATION_KIND
    if run_started_at is not None:
        metadata[INTERNAL_CONTINUATION_RUN_STARTED_AT_META] = float(run_started_at)
    for key in _STRIPPED_INBOUND_META_KEYS:
        metadata.pop(key, None)
    return metadata


def _goal_continuation_prompt(metadata: Mapping[str, Any] | None) -> str:
    """生成持久化目标续接的提示词，包含目标运行时摘要（若存在）。"""
    lines = goal_state_runtime_lines(metadata)
    if lines:
        goal = "\n".join(lines)
        return (
            "Continue the active sustained goal after the previous turn reached "
            "its tool-call budget.\n\n"
            f"{goal}\n\n"
            "Continue from the saved context. Do not mention the continuation "
            "boundary to the user. Use tools as needed, and call complete_goal "
            "when the objective is truly finished."
        )
    return (
        "Continue the active sustained goal after the previous turn reached "
        "its tool-call budget. Continue from the saved context. Do not mention "
        "the continuation boundary to the user. Use tools as needed, and call "
        "complete_goal when the objective is truly finished."
    )


def _strip_terminal_assistant(
    messages: list[dict[str, Any]],
    final_content: str | None,
) -> list[dict[str, Any]]:
    """在保存历史前丢弃合成的 max-iteration 助手消息。"""
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") != "assistant":
        return messages
    if final_content is None or last.get("content") != final_content:
        return messages
    # 末尾助手消息若带 tool_calls 则不丢弃
    if last.get("tool_calls"):
        return messages
    return messages[:-1]
