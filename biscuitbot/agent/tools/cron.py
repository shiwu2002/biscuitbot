"""定时任务调度工具，用于安排提醒与周期性任务。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的定时任务组件。
在项目架构中起到的作用：提供 ``cron`` 工具，让 agent 能够添加、列出、移除
定时任务（提醒或周期性任务），支持按秒间隔、cron 表达式与一次性时间点
三种调度方式，并将任务关联到当前会话，保证任务触发时能回到正确的会话。
"""

from __future__ import annotations

from contextvars import ContextVar  # 上下文变量，用于在异步链中传递会话信息
from datetime import datetime
from typing import Any

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.context import ContextAware, RequestContext  # 上下文感知协议与请求上下文
from biscuitbot.agent.tools.schema import (  # Schema 构造器
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from biscuitbot.cron.service import CronService  # 定时任务服务
from biscuitbot.cron.types import CronJob, CronJobState, CronSchedule  # 定时任务类型定义
from biscuitbot.session.keys import UNIFIED_SESSION_KEY  # 统一会话键常量

# cron 工具参数 schema：顶层不使用 oneOf/anyOf，以兼容各模型供应商
_CRON_PARAMETERS = tool_parameters_schema(
    action=StringSchema("Action to perform", enum=["add", "list", "remove"]),
    name=StringSchema(
        "Optional short human-readable label for the job "
        "(e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message."
    ),
    message=StringSchema(
        "REQUIRED when action='add'. Instruction for the agent to execute when the job triggers "
        "(e.g., 'Send a reminder to WeChat: xxx' or 'Check system status and report'). "
        "Not used for action='list' or action='remove'."
    ),
    every_seconds=IntegerSchema(0, description="Interval in seconds (for recurring tasks)"),
    cron_expr=StringSchema("Cron expression like '0 9 * * *' (for scheduled tasks)"),
    tz=StringSchema(
        "Optional IANA timezone for cron expressions (e.g. 'America/Vancouver'). "
        "When omitted with cron_expr, the tool's default timezone applies."
    ),
    at=StringSchema(
        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
        "Naive values use the tool's default timezone."
    ),
    job_id=StringSchema("REQUIRED when action='remove'. Job ID to remove (obtain via action='list')."),
    required=["action"],
    description=(
        "Action-specific parameters: add requires a non-empty message plus one schedule "
        "(every_seconds, cron_expr, or at); remove requires job_id; list only needs action. "
        "Per-action requirements are enforced at runtime (see field descriptions) so the "
        "top-level schema stays compatible with providers (e.g. OpenAI Codex/Responses) that "
        "reject oneOf/anyOf/allOf/enum/not at the root of function parameters."
    ),
)


@tool_parameters(_CRON_PARAMETERS)
class CronTool(Tool, ContextAware):
    """定时任务调度工具，支持添加、列出、移除提醒与周期性任务。

    职责：管理 cron 任务的完整生命周期，将任务与会话绑定，确保任务触发时
    能在正确的会话上下文中执行。同时实现 ContextAware 协议以接收请求上下文。

    用法：由 agent 调用，传入 action（add/list/remove）及对应参数。
    """

    _capability = (
        "Schedule reminders and recurring cron-driven tasks (add/list/remove)."
    )
    _usage_md = "docs/cron.md"  # 工具使用说明文档路径

    def __init__(self, cron_service: CronService, default_timezone: str = "UTC"):
        """初始化 cron 工具。

        参数:
            cron_service: 定时任务服务实例。
            default_timezone: 默认时区，用于无显式时区时的调度与展示。
        """
        self._cron = cron_service
        self._default_timezone = default_timezone
        # 以下 ContextVar 用于在异步链中传递当前会话与来源信息
        self._session_key: ContextVar[str] = ContextVar("cron_session_key", default="")
        self._origin_channel: ContextVar[str] = ContextVar("cron_origin_channel", default="")
        self._origin_chat_id: ContextVar[str] = ContextVar("cron_origin_chat_id", default="")
        self._origin_metadata: ContextVar[dict[str, Any] | None] = ContextVar(
            "cron_origin_metadata",
            default=None,
        )
        # 标记是否处于 cron 回调执行上下文中（禁止在回调中新建任务）
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """当上下文中存在 cron_service 时启用。"""
        return ctx.cron_service is not None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """工厂方法：依据上下文创建工具实例。"""
        return cls(cron_service=ctx.cron_service, default_timezone=ctx.timezone)

    def set_context(self, ctx: RequestContext) -> None:
        """设置当前会话上下文，用于定时任务的所有权归属。

        参数:
            ctx: 请求上下文，包含渠道、会话 ID 等信息。
        """
        raw_key = f"{ctx.channel}:{ctx.chat_id}" if ctx.channel and ctx.chat_id else ""
        # 统一会话键时使用 channel:chat_id 作为实际键
        self._session_key.set(
            raw_key if ctx.session_key == UNIFIED_SESSION_KEY else (ctx.session_key or "")
        )
        self._origin_channel.set(ctx.channel or "")
        self._origin_chat_id.set(ctx.chat_id or "")
        self._origin_metadata.set(dict(ctx.metadata or {}))

    def set_cron_context(self, active: bool):
        """标记当前是否在 cron 回调中执行。

        参数:
            active: 是否处于 cron 回调上下文。

        返回:
            ContextVar 令牌，用于后续重置。
        """
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """恢复之前的 cron 上下文状态。

        参数:
            token: set_cron_context 返回的令牌。
        """
        self._in_cron_context.reset(token)

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        """校验时区是否有效。

        参数:
            tz: IANA 时区名。

        返回:
            无效时返回错误信息，有效时返回 None。
        """
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"Error: unknown timezone '{tz}'"
        return None

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """选择最适合展示的时区。"""
        return schedule.tz or self._default_timezone

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        """将毫秒时间戳格式化为带时区的可读字符串。

        参数:
            ms: 毫秒时间戳。
            tz_name: 时区名。

        返回:
            ISO 格式带时区名的时间字符串。
        """
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    @property
    def name(self) -> str:
        """工具名称。"""
        return "cron"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."
        )

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """参数校验，补充各 action 的专属必填项检查。

        参数:
            params: 待校验的参数字典。

        返回:
            错误信息列表，空表示通过。
        """
        errors = super().validate_params(params)
        action = params.get("action")
        if action == "add" and not str(params.get("message") or "").strip():
            errors.append("message is required when action='add'")
        if action == "remove" and not str(params.get("job_id") or "").strip():
            errors.append("job_id is required when action='remove'")
        return errors

    async def execute(
        self,
        action: str,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        deliver: bool = True,
        **kwargs: Any,
    ) -> str:
        """执行 cron 操作。

        参数:
            action: 操作类型（add/list/remove）。
            name: 任务标签。
            message: 触发时执行的指令（add 必填）。
            every_seconds: 周期任务的间隔秒数。
            cron_expr: cron 表达式。
            tz: 时区。
            at: 一次性执行的 ISO 时间。
            job_id: 待移除的任务 ID（remove 必填）。
            deliver: 是否投递结果。

        返回:
            操作结果字符串。
        """
        if action == "add":
            # 禁止在 cron 回调中新建任务，避免无限递归
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(name, message, every_seconds, cron_expr, tz, at)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        name: str | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
    ) -> str:
        """添加定时任务。

        参数:
            name: 任务标签，为空时取 message 前 30 字符。
            message: 触发时执行的指令。
            every_seconds: 周期间隔秒数。
            cron_expr: cron 表达式。
            tz: 时区。
            at: 一次性 ISO 时间。

        返回:
            创建成功或错误的描述字符串。
        """
        if not message:
            return (
                "Error: cron action='add' requires a non-empty 'message' parameter "
                "describing what to do when the job triggers "
                "(e.g. the reminder text). Retry including message=\"...\"."
            )
        session_key = self._session_key.get()
        if not session_key:
            return "Error: scheduled cron jobs must be created from a chat session"
        origin_channel = self._origin_channel.get()
        origin_chat_id = self._origin_chat_id.get()
        if not origin_channel or not origin_chat_id:
            return "Error: scheduled cron jobs must be created from a chat session"
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            if err := self._validate_timezone(tz):
                return err

        # 构建调度计划：根据传入参数选择三种调度方式之一
        delete_after = False  # 一次性任务执行后是否自动删除
        if every_seconds:
            # 周期任务：按秒间隔
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            # cron 表达式调度
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return err
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            # 一次性时间点调度
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            # 无时区信息时使用默认时区
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return err
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        job = self._cron.add_job(
            name=name or message[:30],
            schedule=schedule,
            message=message,
            delete_after_run=delete_after,
            session_key=session_key,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_metadata=dict(self._origin_metadata.get() or {}),
        )
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """将调度计划格式化为人类可读的时间描述字符串。"""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            # 优先以小时、分钟、秒为单位展示
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """将任务运行状态格式化为展示行列表。"""
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        """返回系统任务的用途描述。"""
        if job.name == "dream":
            return "Dream memory consolidation for long-term memory."
        return "System-managed internal job."

    def _list_jobs(self) -> str:
        """列出所有定时任务及其状态。"""
        jobs = self._cron.list_jobs()
        if not jobs:
            return "No scheduled jobs."
        lines = []
        for j in jobs:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            # 系统事件任务受保护，仅可查看不可移除
            if j.payload.kind == "system_event":
                parts.append(f"  Purpose: {self._system_job_purpose(j)}")
                parts.append("  Protected: visible for inspection, but cannot be removed.")
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        """移除指定任务。

        参数:
            job_id: 待移除的任务 ID。

        返回:
            移除结果描述字符串。
        """
        if not job_id:
            return "Error: job_id is required for remove"
        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"Removed job {job_id}"
        if result == "protected":
            # 受保护的系统任务不可移除
            job = self._cron.get_job(job_id)
            if job and job.name == "dream":
                return (
                    "Cannot remove job `dream`.\n"
                    "This is a system-managed Dream memory consolidation job for long-term memory.\n"
                    "It remains visible so you can inspect it, but it cannot be removed."
                )
            return (
                f"Cannot remove job `{job_id}`.\n"
                "This is a protected system-managed cron job."
            )
        return f"Job {job_id} not found"
