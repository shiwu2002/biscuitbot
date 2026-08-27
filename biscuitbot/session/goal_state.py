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
from biscuitbot.utils.helpers import truncate_text  # 文本截断，用于任务清单渲染与 WebSocket 推送

GOAL_STATE_KEY = "goal_state"  # 当前版本存储目标状态的元数据键
# 旧版本构建将相同 JSON blob 存储在该键下
_LEGACY_GOAL_STATE_SESSION_KEY = "thread_goal"
_MAX_OBJECTIVE_IN_RUNTIME = 4000  # 运行时上下文块中目标文本的最大长度
_MAX_OBJECTIVE_WS = 600  # WebSocket 推送时目标文本的最大长度

# 任务清单（task checklist）相关常量：扁平结构，每项 {id, text, status}。
_TASK_STATUSES = ("pending", "in_progress", "done")  # 合法状态枚举
_MAX_TASKS = 50  # 存储的任务数硬上限（超出 add 拒绝，避免 blob 无限膨胀）
_MAX_TASKS_IN_RUNTIME = 20  # 每轮注入 Runtime Context 的任务显示上限
_MAX_TASK_TEXT_IN_RUNTIME = 160  # 每条任务在 Runtime Context 中的文本截断
_MAX_TASKS_WS = 10  # WebSocket 推送的任务数上限
_MAX_TASK_TEXT_WS = 80  # WebSocket 推送时单条任务文本截断


def _task_marker(status: str) -> str:
    """将任务状态映射为紧凑的单字符 marker（用于 Runtime Context 渲染）。"""
    if status == "done":
        return "[x]"
    if status == "in_progress":
        return "[~]"
    return "[ ]"


def _next_task_id(tasks: list[dict[str, Any]]) -> str:
    """按现有任务的数字后缀生成下一个单调递增的 id（``t1``、``t2``、…）。

    删除任务不会复用其编号；新 id = 现有最大值 + 1。id 存于 blob，天然跨压缩稳定，
    避免模型在压缩后编造一个内存里没有的 id 导致对账失败。
    """
    n = 0
    for t in tasks:
        tid = str(t.get("id") or "")
        if tid.startswith("t") and tid[1:].isdigit():
            n = max(n, int(tid[1:]))
    return f"t{n + 1}"


def _normalize_tasks(goal: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """将目标 blob 中的任务清单归一化，返回有序、干净的任务列表。

    清洗规则：
    - ``goal["tasks"]`` 不是 list 时返回 ``[]``（旧 blob 无 tasks，天然兼容）；
    - 跳过非 dict 项；``text`` 强转 str 并 strip，为空则跳过；
    - ``status`` 不在 ``_TASK_STATUSES`` 内时归一为 ``pending``；
    - 缺失 id 用 ``_next_task_id`` 兜底生成。

    所有读路径（runtime lines / ws blob / update_task）都先经此归一化，确保脏数据不流出。
    """
    if not isinstance(goal, Mapping):
        return []
    raw = goal.get("tasks")
    if not isinstance(raw, list):
        return []
    tasks: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        status = str(item.get("status") or "pending")
        if status not in _TASK_STATUSES:
            status = "pending"
        tid = str(item.get("id") or "").strip()
        if not tid:
            # 始终基于已追加的任务现算，避免连续缺 id 的任务拿到重复编号
            # （上一版用缓存导致第二个缺 id 项复用第一个的编号）。
            tid = _next_task_id(tasks)
        tasks.append({"id": tid, "text": text, "status": status})
    return tasks


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
    """当目标激活时追加到运行时上下文块的文本行。

    当目标携带任务清单时，追加 ``Tasks (n/m done):`` 段，逐行带 id 与状态
    marker（done=``[x]``、in_progress=``[~]``、pending=``[ ]``），并在结尾提示
    用 ``update_task`` 维护——使模型每轮都能看到「做了哪些、还剩哪些」，避免
    长对话中遗忘分析出的子步骤。清单存于 metadata，压缩不会移除它。
    """
    if not metadata:
        return []
    goal = parse_goal_state(_session_goal_raw(metadata))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return []
    objective = str(goal.get("objective") or "").strip()
    hint = str(goal.get("ui_summary") or "").strip()
    tasks = _normalize_tasks(goal)
    if not objective and not tasks:
        return ["Goal: active (no objective text stored)."]
    # 超长目标文本截断
    if len(objective) > _MAX_OBJECTIVE_IN_RUNTIME:
        objective = objective[:_MAX_OBJECTIVE_IN_RUNTIME].rstrip() + "\n… (truncated)"
    out = ["Goal (active):"]
    out.append(objective if objective else "(no objective text stored)")
    if hint:
        out.append(f"Summary: {hint}")
    if tasks:
        done = sum(1 for t in tasks if t["status"] == "done")
        shown = tasks[:_MAX_TASKS_IN_RUNTIME]
        out.append(f"Tasks ({done}/{len(tasks)} done):")
        for t in shown:
            text = truncate_text(t["text"], _MAX_TASK_TEXT_IN_RUNTIME)
            out.append(f"{_task_marker(t['status'])} ({t['id']}) {text}")
        if len(tasks) > _MAX_TASKS_IN_RUNTIME:
            out.append(f"… {len(tasks) - _MAX_TASKS_IN_RUNTIME} more tasks")
        out.append("Update task status with update_task after each completed step.")
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
        # 仅当归一化后确有任务时才携带 tasks 键，保持"无任务不带该键"
        # （否则破坏现有 blob 精确相等断言）。
        tasks = _normalize_tasks(goal)
        if tasks:
            blob["tasks"] = [
                {
                    "id": t["id"],
                    "text": truncate_text(t["text"], _MAX_TASK_TEXT_WS),
                    "status": t["status"],
                }
                for t in tasks[:_MAX_TASKS_WS]
            ]
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
