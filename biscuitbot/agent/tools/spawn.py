"""生成后台子 agent 的 spawn 工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中的子 agent 生成组件。
``SpawnTool``（spawn）允许 agent 在后台启动一个子 agent 来处理复杂或耗时
的任务，实现并行执行。子 agent 完成任务后会自动汇报结果。

通过 ``ContextVar`` 跟踪原始请求的渠道、会话 ID 和消息 ID，确保子 agent
的汇报消息能正确路由回发起者。
"""

from __future__ import annotations

from contextvars import ContextVar  # 上下文变量，用于跨异步任务传递请求来源
from typing import TYPE_CHECKING, Any  # 类型注解

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.context import ContextAware, RequestContext  # 上下文感知 mixin 与请求上下文
from biscuitbot.agent.tools.schema import NumberSchema, StringSchema, tool_parameters_schema  # JSON Schema 类型
from biscuitbot.security.workspace_access import current_workspace_scope  # 当前工作区作用域

if TYPE_CHECKING:  # 仅类型检查时导入，避免循环依赖
    from biscuitbot.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        label=StringSchema("Optional short label for the task (for display)"),
        temperature=NumberSchema(
            description=(
                "Optional sampling temperature for the subagent "
                "(0.0 = deterministic, higher = more creative). "
                "Defaults to the provider's configured temperature."
            ),
            minimum=0.0,
            maximum=2.0,
        ),
        required=["task"],
    )
)
class SpawnTool(Tool, ContextAware):
    """生成子 agent 以在后台执行任务。"""

    _capability = (
        "Launch a background subagent for long-running or parallel tasks."
    )
    _usage_md = "docs/spawn.md"  # 使用说明文档路径

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager  # 子 agent 管理器
        # 以下 ContextVar 用于跟踪原始请求来源，确保子 agent 汇报能路由回发起者
        self._origin_channel: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "spawn_origin_message_id",
            default=None,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    def set_context(self, ctx: RequestContext) -> None:
        """设置原始请求上下文，用于子 agent 完成后的汇报路由。"""
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful."
        )

    async def execute(
        self,
        task: str,
        label: str | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> str:
        """生成子 agent 执行指定任务。

        参数:
            task: 子 agent 要完成的任务描述。
            label: 可选的任务短标签（用于显示）。
            temperature: 可选的采样温度（0.0 = 确定性，越高越有创造性）。

        返回:
            成功时返回子 agent 启动信息；达到并发上限时返回错误信息。
        """
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before spawning a new one."
            )
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            origin_message_id=self._origin_message_id.get(),
            temperature=temperature,
            workspace_scope=current_workspace_scope(),
        )
