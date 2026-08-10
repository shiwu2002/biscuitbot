"""主 Agent 上的持续目标工具（Codex 风格）。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中负责"长任务目标管理"
的组件。它将一个持续目标（sustained goal）登记到会话元数据中，使模型
能够在多个常规轮次中持续推进同一目标，而不会被上下文压缩（compaction）
遗忘。

核心机制：
- ``long_task`` 把目标写入会话元数据（JSON 可序列化）。
- 每一轮都会把活跃目标镜像到 Runtime Context 块
  （见 ``biscuitbot.session.goal_state.goal_state_runtime_lines``），因此
  压缩无法隐藏它。
- 实际工作在普通 agent 轮次中推进（同一 runner，按配置压缩）。
- ``complete_goal`` 在目标应停止追踪时调用：成功完成、取消、被取代或
  重定向——无论哪种情况，回顾说明（recap）都应与实际情况一致。

设计要点：**没有**子 Agent 编排器，**没有**特殊的 WebSocket ``agent_ui``
流。请遵循内置 **long-goal** 技能的生命周期规则与目标措辞要求（尤其是
**幂等**、压缩安全的目标写法）。
"""

from __future__ import annotations

from contextvars import ContextVar  # 上下文变量，用于在异步任务间隔离请求上下文
from datetime import datetime  # 日期时间处理，用于生成 ISO 时间戳
from typing import TYPE_CHECKING, Any  # 类型检查与任意类型

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.context import ContextAware, RequestContext  # 上下文感知混入与请求上下文
from biscuitbot.agent.tools.schema import StringSchema, tool_parameters_schema  # 字符串 schema 与参数 schema 构造器
from biscuitbot.bus.runtime_events import GoalStateChanged, RuntimeEventBus, RuntimeEventContext  # 运行时事件总线与目标状态变更事件
from biscuitbot.session.goal_state import (  # 会话目标状态存取工具
    GOAL_STATE_KEY,  # 会话元数据中存储目标状态的键名
    discard_legacy_goal_state_key,  # 丢弃遗留的目标状态键
    goal_state_raw,  # 读取原始目标状态
    parse_goal_state,  # 解析目标状态为字典
)

if TYPE_CHECKING:  # 仅类型检查时导入，避免运行时循环依赖
    from biscuitbot.session.manager import SessionManager  # 会话管理器


def _iso_now() -> str:
    """返回当前时间的 ISO 8601 字符串。"""
    return datetime.now().isoformat()


class _GoalToolsMixin(ContextAware):
    """目标工具共享的混入：路由上下文 + 会话查找。

    职责：为 LongTaskTool 与 CompleteGoalTool 提供共享的请求上下文管理
    与会话查找逻辑，确保两个工具能在并发任务中各自隔离地访问当前会话。
    """

    def __init__(
        self,
        sessions: SessionManager,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        self._sessions = sessions  # 会话管理器实例
        self._runtime_events = runtime_events  # 运行时事件总线，可为空
        # 每个子类拥有独立的 ContextVar，确保不同工具类型（LongTaskTool 与
        # CompleteGoalTool）的并发任务互不干扰。
        self._request_ctx: ContextVar[RequestContext | None] = ContextVar(
            f"{self.__class__.__name__}_request_ctx",
            default=None,
        )

    def set_context(self, ctx: RequestContext) -> None:
        """设置当前请求上下文。

        参数:
            ctx: 当前轮次的请求上下文，包含会话键、渠道、chat_id 等。
        """
        self._request_ctx.set(ctx)

    def _session(self):
        """根据当前请求上下文查找或创建会话。

        返回:
            当前会话对象；若无请求上下文或会话键则返回 None。
        """
        request_ctx = self._request_ctx.get()
        if request_ctx is None:
            return None
        key = request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    async def _publish_goal_state_changed(self, metadata: dict[str, Any]) -> None:
        """将权威的目标元数据作为运行时事件发布。

        参数:
            metadata: 会话元数据，包含目标状态。
        """
        runtime_events = self._runtime_events
        rc = self._request_ctx.get()
        # 缺少事件总线或请求上下文时静默跳过
        if runtime_events is None or rc is None:
            return
        cid = (rc.chat_id or "").strip()
        # 无 chat_id 时无法定位会话，跳过发布
        if not cid:
            return
        await runtime_events.publish(
            GoalStateChanged(
                context=RuntimeEventContext(
                    channel=rc.channel,
                    chat_id=cid,
                    session_key=rc.session_key or f"{rc.channel}:{cid}",
                    metadata=dict(rc.metadata or {}),
                ),
                session_metadata=dict(metadata),
            )
        )


@tool_parameters(
    tool_parameters_schema(
        goal=StringSchema(
            "Sustained objective for this chat thread. First read the built-in **long-goal** skill, "
            "especially its Start fast section, then call this promptly once the user's intent is clear. "
            "The goal must still be idempotent, self-contained, bounded, and explicit about done-ness; "
            "do not delay this tool call to over-plan, research, or decide execution details.",
            max_length=12_000,
        ),
        ui_summary=StringSchema(
            "Optional one-line label for session lists / logs (≤120 chars).",
            max_length=120,
            nullable=True,
        ),
        required=["goal"],
    )
)
class LongTaskTool(Tool, _GoalToolsMixin):
    """开始或替换存储在会话上的长运行目标。

    职责：将一个持续目标登记到会话元数据中，使主 Agent 在后续轮次中
    持续追踪该目标。目标会被镜像到 Runtime Context 块，避免被压缩遗忘。

    用法：当用户意图清晰时尽快调用；若已有活跃目标，需先 complete_goal
    或询问用户后再替换。
    """

    _capability = (
        "Begin or replace a long-running sustained objective tracked on the session."
    )
    _usage_md = "docs/long_task.md"  # 工具使用说明文档路径

    def __init__(
        self,
        sessions: Any,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        _GoalToolsMixin.__init__(self, sessions, runtime_events)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        sess = getattr(ctx, "sessions", None)
        assert sess is not None  # 由 enabled() 保证非空
        return cls(
            sessions=sess,
            runtime_events=getattr(ctx, "runtime_events", None),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """仅当上下文提供 sessions 时启用。"""
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        """工具名称。"""
        return "long_task"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何与何时调用。"""
        return (
            "Mark this thread as a sustained long-running task. "
            "First read the built-in **long-goal** skill, especially its Start fast section; then call this "
            "as soon as the user's intent is clear. Write a good idempotent goal, but do not delay the tool "
            "call with long planning, research, or execution-detail thinking. "
            "The active goal is mirrored in Runtime Context each turn. Use normal tools until done, then call "
            "complete_goal when the objective is satisfied, cancelled, or replaced. "
            "If a goal is already active, finish it or call complete_goal before registering another."
        )

    async def execute(self, goal: str, ui_summary: str | None = None, **kwargs: Any) -> str:
        """登记长任务目标。

        参数:
            goal: 持续目标文本，需幂等、自包含、有界且明确完成条件。
            ui_summary: 可选的一行摘要，用于会话列表/日志（≤120 字符）。

        返回:
            操作结果字符串；若会话缺失或已有活跃目标，返回错误说明。
        """
        sess = self._session()
        if sess is None:
            return (
                "Error: long_task requires an active chat session (missing routing context)."
            )
        prior = parse_goal_state(goal_state_raw(sess.metadata))
        # 已有活跃目标时拒绝重复登记，避免覆盖进行中的目标
        if isinstance(prior, dict) and prior.get("status") == "active":
            return (
                "Error: a sustained goal is already active. "
                "Use complete_goal when finished, or ask the user before replacing it."
            )

        summary = (ui_summary or "").strip()[:120]
        blob = {
            "status": "active",
            "objective": goal.strip(),
            "ui_summary": summary,
            "started_at": _iso_now(),
        }
        sess.metadata[GOAL_STATE_KEY] = blob
        discard_legacy_goal_state_key(sess.metadata)  # 清理遗留键
        self._sessions.save(sess)
        await self._publish_goal_state_changed(sess.metadata)
        extra = f"\nSummary line: {summary}" if summary else ""
        return (
            "Goal recorded. Keep working toward the objective using ordinary tools. "
            "When fully done (verified against what was asked), call complete_goal with a "
            f"short recap.{extra}"
        )


@tool_parameters(
    tool_parameters_schema(
        recap=StringSchema(
            "Brief recap for the user (plain text). When the goal succeeded, confirm outcomes; "
            "if the user cancelled, pivoted, or replaced the objective, say so honestly.",
            max_length=8000,
            nullable=True,
        ),
        required=[],
    )
)
class CompleteGoalTool(Tool, _GoalToolsMixin):
    """在所有必需工作验证完成后，标记活跃持续目标为已完成。

    职责：结束对活跃持续目标的记账。当目标已完全达成并验证、或被用户
    取消、重定向、替换时调用。recap 必须如实反映实际发生的情况（未必
    是成功）。若无活跃目标，则报告情况且不修改元数据。
    """

    _capability = (
        "Mark the active sustained goal as finished after verifying required work."
    )
    _usage_md = "docs/complete_goal.md"  # 工具使用说明文档路径

    def __init__(
        self,
        sessions: Any,
        runtime_events: RuntimeEventBus | None = None,
    ) -> None:
        _GoalToolsMixin.__init__(self, sessions, runtime_events)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        sess = getattr(ctx, "sessions", None)
        assert sess is not None
        return cls(
            sessions=sess,
            runtime_events=getattr(ctx, "runtime_events", None),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """仅当上下文提供 sessions 时启用。"""
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        """工具名称。"""
        return "complete_goal"

    @property
    def description(self) -> str:
        """工具描述，指导模型何时调用。"""
        return (
            "End bookkeeping for the active sustained goal. "
            "Use when the objective is fully achieved and verified—recap what was delivered. "
            "Also call when the user cancels, redirects, or replaces the goal: recap must reflect "
            "what actually happened (not necessarily success). "
            "If no goal is active, the tool reports that and leaves metadata unchanged."
        )

    async def execute(self, recap: str | None = None, **kwargs: Any) -> str:
        """完成活跃目标。

        参数:
            recap: 给用户的简要回顾（纯文本）。成功时确认成果；取消/转向/
                替换时如实说明情况。

        返回:
            操作结果字符串；无活跃目标或会话缺失时返回相应说明。
        """
        sess = self._session()
        if sess is None:
            return "Error: complete_goal requires an active chat session."
        prior = parse_goal_state(goal_state_raw(sess.metadata))
        # 无活跃目标时直接返回，不修改元数据
        if not isinstance(prior, dict) or prior.get("status") != "active":
            return "No active goal to complete."

        ended = _iso_now()
        sess.metadata[GOAL_STATE_KEY] = {
            **prior,  # 保留原有字段（如 objective、started_at）
            "status": "completed",
            "completed_at": ended,
            "recap": (recap or "").strip(),
        }
        discard_legacy_goal_state_key(sess.metadata)  # 清理遗留键
        self._sessions.save(sess)
        await self._publish_goal_state_changed(sess.metadata)
        tail = (recap or "").strip()
        if tail:
            return f"Goal marked complete ({ended}). Recap:\n{tail}"
        return f"Goal marked complete ({ended})."
