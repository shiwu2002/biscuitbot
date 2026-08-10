"""持久化目标（sustained goal）的会话元数据辅助工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/session 目录，提供持久化目标（如 ``long_task`` / ``complete_goal``）
相关的会话元数据读写辅助。
在项目架构中起到的作用：工具通过 ``metadata[GOAL_STATE_KEY]`` 设置目标状态；读取时兼容
旧版本会话键 ``thread_goal``。对外暴露 ``goal_state_runtime_lines``、``goal_state_ws_blob``
与 ``runner_wall_llm_timeout_s``，使调用方无需导入工具实现即可获取目标状态摘要与运行时限。
"""

from __future__ import annotations

import json  # 解析可能为字符串形式的目标状态 JSON
from typing import Any, Mapping, MutableMapping  # 类型标注

from biscuitbot.session.manager import SessionManager  # 会话管理器，用于按需读取会话元数据

GOAL_STATE_KEY = "goal_state"  # 当前版本存储目标状态的元数据键
# 旧版本构建将相同 JSON blob 存储在该键下
_LEGACY_GOAL_STATE_SESSION_KEY = "thread_goal"
_MAX_OBJECTIVE_IN_RUNTIME = 4000  # 运行时上下文块中目标文本的最大长度
_MAX_OBJECTIVE_WS = 600  # WebSocket 推送时目标文本的最大长度


def _session_goal_raw(metadata: Mapping[str, Any] | None) -> Any:
    """读取目标状态的原始 blob，优先用新键，回退到遗留键。"""
    if not metadata:
        return None
    if GOAL_STATE_KEY in metadata:
        return metadata.get(GOAL_STATE_KEY)
    return metadata.get(_LEGACY_GOAL_STATE_SESSION_KEY)


def discard_legacy_goal_state_key(metadata: MutableMapping[str, Any]) -> None:
    """在迁移写入到 :data:`GOAL_STATE_KEY` 后移除遗留元数据键。"""
    metadata.pop(_LEGACY_GOAL_STATE_SESSION_KEY, None)


def goal_state_raw(metadata: Mapping[str, Any] | None) -> Any:
    """返回 :data:`GOAL_STATE_KEY` 或遗留键下的会话目标 blob。"""
    return _session_goal_raw(metadata)


def sustained_goal_active(metadata: Mapping[str, Any] | None) -> bool:
    """当本会话存在激活的持久化目标时返回 True（``long_task`` 记账）。"""
    goal = parse_goal_state(goal_state_raw(metadata))
    return isinstance(goal, dict) and goal.get("status") == "active"


def sustained_goal_turn(
    metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """当本 turn 应使用持久化目标运行时限时返回 True。"""
    if sustained_goal_active(metadata):
        return True
    if not message_metadata:
        return False
    # 当 turn 由 /goal 命令触发时也视为持久化目标 turn
    return str(message_metadata.get("original_command") or "").strip() == "/goal"


def parse_goal_state(blob: Any) -> dict[str, Any] | None:
    """将目标状态 blob 解析为字典；接受 dict 或 JSON 字符串，无法解析返回 None。"""
    if blob is None:
        return None
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def goal_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """当目标激活时追加到运行时上下文块的文本行。"""
    if not metadata:
        return []
    goal = parse_goal_state(_session_goal_raw(metadata))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return []
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        return ["Goal: active (no objective text stored)."]
    # 超长目标文本截断
    if len(objective) > _MAX_OBJECTIVE_IN_RUNTIME:
        objective = objective[:_MAX_OBJECTIVE_IN_RUNTIME].rstrip() + "\n… (truncated)"
    out = ["Goal (active):", objective]
    hint = str(goal.get("ui_summary") or "").strip()
    if hint:
        out.append(f"Summary: {hint}")
    return out


def goal_state_ws_blob(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """用于 WebSocket ``goal_state`` 事件的 JSON 安全快照（每帧一个 chat_id）。"""
    goal = parse_goal_state(_session_goal_raw(metadata)) if metadata else None
    if isinstance(goal, dict) and goal.get("status") == "active":
        objective = str(goal.get("objective") or "").strip()
        # WebSocket 推送的目标文本截断阈值更小
        if len(objective) > _MAX_OBJECTIVE_WS:
            objective = objective[:_MAX_OBJECTIVE_WS].rstrip() + "…"
        summary = str(goal.get("ui_summary") or "").strip()[:120]
        blob: dict[str, Any] = {"active": True}
        if summary:
            blob["ui_summary"] = summary
        if objective:
            blob["objective"] = objective
        return blob
    return {"active": False}


def runner_wall_llm_timeout_s(
    sessions: SessionManager,
    session_key: str | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    message_metadata: Mapping[str, Any] | None = None,
) -> float | None:
    """:class:`~biscuitbot.agent.runner.AgentRunner` 流式调用 LLM 时的墙钟时限。

    当为持久化目标 turn 时返回 ``0.0`` 表示禁用请求外的 ``asyncio.wait_for``；
    返回 ``None`` 表示使用 ``BISCUITBOT_LLM_TIMEOUT_S``。当调用方已持有本 turn 的
    :attr:`~biscuitbot.session.manager.Session.metadata` 时可直接传入内存中的 ``metadata``。
    """
    meta: Mapping[str, Any] | None = metadata
    # 未显式传入元数据时从会话管理器按 session_key 获取
    if meta is None and session_key:
        meta = sessions.get_or_create(session_key).metadata
    return 0.0 if sustained_goal_turn(meta, message_metadata=message_metadata) else None
