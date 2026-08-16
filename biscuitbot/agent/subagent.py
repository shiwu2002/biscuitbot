"""子 Agent（Subagent）管理器，用于后台任务执行。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责“后台子 Agent 执行”的组件。
在项目架构中起到的作用：
- ``SubagentManager`` 管理后台子 Agent 的生命周期：生成（spawn）、运行、取消与状态查询；
- 子 Agent 在独立任务中通过 ``AgentRunner`` 执行工具型 LLM 循环，使用隔离的工具注册表
  与工作区作用域，完成后通过消息总线（MessageBus）将结果注入主 Agent 的会话；
- ``SubagentStatus`` 提供子 Agent 的实时状态（阶段、迭代、工具事件、用量等）；
- ``_SubagentHook`` 是子 Agent 专用的生命周期钩子，负责日志记录与状态更新。
"""

import asyncio  # 异步任务创建、取消与并发收集
import json  # 工具调用参数序列化（日志用）
import time  # 单调时钟（子 Agent 启动时间）
import uuid  # 生成子 Agent 任务 ID
from dataclasses import dataclass, field  # 数据类装饰器与字段默认工厂
from pathlib import Path  # 工作区路径类型
from typing import Any, Callable  # 类型注解支持

from loguru import logger  # 日志记录

from biscuitbot.agent.hook import AgentHook, AgentHookContext  # 生命周期钩子基类与上下文
from biscuitbot.agent.runner import AgentRunner, AgentRunSpec  # Agent 执行器与执行规格
from biscuitbot.agent.tools.context import ToolContext  # 工具上下文
from biscuitbot.agent.tools.file_state import FileStates  # 文件状态存储
from biscuitbot.agent.tools.loader import ToolLoader  # 工具加载器
from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表
from biscuitbot.bus.events import InboundMessage  # 入站消息类型
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.config.schema import AgentDefaults, ToolsConfig  # Agent 默认值与工具配置
from biscuitbot.providers.base import LLMProvider  # LLM 提供商基类
from biscuitbot.security.workspace_access import (  # 工作区访问安全控制
    WorkspaceScope,
    bind_workspace_scope,
    reset_workspace_scope,
    workspace_sandbox_status,
)
from biscuitbot.utils.prompt_templates import render_template  # 模板渲染


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""
    """运行中子 Agent 的实时状态。

    职责与项目角色：
    - 记录子 Agent 的任务 ID、标签、描述、启动时间；
    - 跟踪执行阶段、迭代序号、工具事件、token 用量、停止原因与错误；
    - 由 ``_SubagentHook`` 在迭代结束后更新，供 ``SubagentManager`` 查询。
    """

    task_id: str  # 子 Agent 任务 ID
    label: str  # 显示标签
    task_description: str  # 任务描述
    started_at: float          # time.monotonic()  # 启动时间（单调时钟）
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error  # 执行阶段
    iteration: int = 0  # 当前迭代序号
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]  # 工具调用事件列表
    usage: dict = field(default_factory=dict)          # token usage  # token 用量
    stop_reason: str | None = None  # 停止原因
    error: str | None = None  # 错误信息


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""
    """子 Agent 执行钩子——记录工具调用日志并更新状态。

    职责与项目角色：
    - 在工具执行前记录调试日志（工具名与参数）；
    - 在每次迭代结束后把迭代序号、工具事件、用量与错误同步到 ``SubagentStatus``。
    """

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id  # 子 Agent 任务 ID
        self._status = status  # 可选的状态对象（为 None 时不更新状态）

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """工具执行前：记录每个工具调用的调试日志。"""
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        """迭代结束后：把上下文中的迭代序号、工具事件、用量与错误同步到状态。"""
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


class SubagentManager:
    """Manages background subagent execution."""
    """管理后台子 Agent 执行。

    职责与项目角色：
    - 子 Agent 的生命周期管理：生成（spawn）、运行、取消与状态查询；
    - 为每个子 Agent 构建隔离的工具注册表与系统提示；
    - 子 Agent 完成后通过消息总线将结果注入主 Agent 会话；
    - 支持按会话批量取消与并发数限制。

    典型用法：由主 Agent（``AgentLoop``）在需要后台执行任务时调用 ``spawn``。
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        image_generation_provider_configs: dict[str, Any] | None = None,
    ):
        defaults = AgentDefaults()  # Agent 默认配置
        self.provider = provider  # LLM 提供商
        self.workspace = workspace  # 工作区路径
        self.bus = bus  # 消息总线
        self.model = model or provider.get_default_model()  # 模型名
        self.tools_config = tools_config or ToolsConfig()  # 工具配置
        self.image_generation_provider_configs = dict(image_generation_provider_configs or {})  # 图像生成供应商配置
        self.max_tool_result_chars = max_tool_result_chars  # 工具结果最大字符数
        self.restrict_to_workspace = restrict_to_workspace  # 是否限制在工作区内
        self.disabled_skills = set(disabled_skills or [])  # 被禁用的技能名集合
        self.max_iterations = (  # 子 Agent 最大迭代次数
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = (  # 最大并发子 Agent 数
            max_concurrent_subagents
            if max_concurrent_subagents is not None
            else defaults.max_concurrent_subagents
        )
        self.runner = AgentRunner(provider)  # Agent 执行器
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session  # 按会话获取 LLM 超时的回调
        self._running_tasks: dict[str, asyncio.Task[None]] = {}  # 运行中的任务：task_id -> asyncio.Task
        self._task_statuses: dict[str, SubagentStatus] = {}  # 任务状态：task_id -> SubagentStatus
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}  # 会话到任务 ID 的映射

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        """构建子 Agent 专用的工具配置（基于主配置的 exec/web/file 子集）。"""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            file=self.tools_config.file,
            image_generation=self.tools_config.image_generation,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        """通过 ToolLoader 构建隔离的子 Agent 工具注册表。

        参数:
            workspace: 工作区路径（默认为主工作区）；
            tools_config: 工具配置（默认为子 Agent 配置）。

        返回:
            加载了 subagent 作用域工具的 ``ToolRegistry``。
        """
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            image_generation_provider_configs=self.image_generation_provider_configs,
            file_state_store=FileStates(),
            workspace_sandbox=workspace_sandbox_status(
                restrict_to_workspace=cfg.restrict_to_workspace,
                workspace=root,
            ),
        )
        ToolLoader().load(ctx, registry, scope="subagent")  # 加载 subagent 作用域工具
        return registry

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        """热更新 LLM 提供商与模型名。"""
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        """生成一个子 Agent 在后台执行任务。

        参数:
            task: 任务描述；
            label: 显示标签（默认截取任务前 30 字符）；
            origin_channel: 来源渠道；
            origin_chat_id: 来源聊天 ID；
            session_key: 会话标识（用于结果路由与按会话取消）；
            origin_message_id: 来源消息 ID；
            temperature: 采样温度；
            workspace_scope: 工作区作用域（限定子 Agent 的工作目录与沙箱）。

        返回:
            面向用户的启动提示文本。
        """
        task_id = str(uuid.uuid4())[:8]  # 生成 8 位短 ID
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")  # 默认标签
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}  # 来源信息

        status = SubagentStatus(  # 创建初始状态
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(  # 创建后台任务
            self._run_subagent(
                task_id,
                task,
                display_label,
                origin,
                status,
                origin_message_id,
                temperature,
                workspace_scope,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:  # 记录会话到任务的映射
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            """任务结束后的清理：移除运行中任务、状态与会话映射。"""
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)  # 注册清理回调

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        temperature: float | None = None,
        workspace_scope: WorkspaceScope | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        """执行子 Agent 任务并公布结果。

        流程：构建工具与系统提示 → 绑定工作区作用域 → 调用 AgentRunner →
        根据停止原因公布结果（成功/工具错误/错误/异常）。

        参数:
            task_id: 任务 ID；
            task: 任务描述；
            label: 显示标签；
            origin: 来源信息字典；
            status: 子 Agent 状态对象；
            origin_message_id: 来源消息 ID；
            temperature: 采样温度；
            workspace_scope: 工作区作用域。
        """
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict) -> None:
            """检查点回调：更新状态的阶段与迭代序号。"""
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            root = workspace_scope.project_path if workspace_scope is not None else self.workspace
            cfg = None
            if workspace_scope is not None:  # 按工作区作用域调整工具配置
                cfg = self._subagent_tools_config()
                cfg.restrict_to_workspace = workspace_scope.restrict_to_workspace
            tools = self._build_tools(workspace=root, tools_config=cfg)
            system_prompt = self._build_subagent_prompt(workspace=root)
            messages: list[dict[str, Any]] = [  # 构造初始消息
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            sess_key = origin.get("session_key")
            llm_timeout = (  # 获取会话级 LLM 超时
                self._llm_wall_timeout_for_session(sess_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            token = bind_workspace_scope(workspace_scope) if workspace_scope is not None else None  # 绑定工作区作用域
            try:
                result = await self.runner.run(AgentRunSpec(  # 执行子 Agent 循环
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    temperature=temperature,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=_SubagentHook(task_id, status),
                    max_iterations_message="Task completed but no final response was generated.",
                    finalize_on_max_iterations=False,  # 子 Agent 不尝试收尾
                    error_message=None,
                    fail_on_tool_error=True,  # 工具出错即中止
                    checkpoint_callback=_on_checkpoint,
                    session_key=sess_key,
                    workspace=root,
                    llm_timeout_s=llm_timeout,
                ))
            finally:
                if token is not None:  # 解绑工作区作用域
                    reset_workspace_scope(token)
            status.phase = "done"
            status.stop_reason = result.stop_reason

            if result.stop_reason == "tool_error":  # 工具错误：公布部分进度
                status.tool_events = list(result.tool_events)
                await self._announce_result(
                    task_id, label, task,
                    self._format_partial_progress(result),
                    origin, "error", origin_message_id,
                )
            elif result.stop_reason == "error":  # LLM 错误：公布错误信息
                await self._announce_result(
                    task_id, label, task,
                    result.error or "Error: subagent execution failed.",
                    origin, "error", origin_message_id,
                )
            else:  # 成功：公布最终结果
                final_result = result.final_content or "Task completed but no final response was generated."
                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(task_id, label, task, final_result, origin, "ok", origin_message_id)

        except Exception as e:  # 异常：公布错误
            status.phase = "error"
            status.error = str(e)
            logger.exception("Subagent [{}] failed", task_id)
            await self._announce_result(task_id, label, task, f"Error: {e}", origin, "error", origin_message_id)

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        """通过消息总线向主 Agent 公布子 Agent 结果。

        以 system 渠道发布 InboundMessage，使用 session_key_override 对齐主 Agent 的
        有效会话 key，使结果路由到正确的待注入队列（回合内注入）而非作为竞争性
        独立任务分发。

        参数:
            task_id: 任务 ID；
            label: 显示标签；
            task: 任务描述；
            result: 结果文本；
            origin: 来源信息字典；
            status: 状态（"ok" 或 "error"）；
            origin_message_id: 来源消息 ID。
        """
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(  # 渲染公告模板
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        # 以 system 消息注入以触发主 Agent。使用 session_key_override 对齐主 Agent 的
        # 有效会话 key（考虑统一会话），使结果路由到正确的待注入队列（回合内注入），
        # 而非作为竞争性独立任务分发。
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    @staticmethod
    def _format_partial_progress(result) -> str:
        """格式化部分进度：最近完成的步骤与失败信息。

        参数:
            result: AgentRunResult。

        返回:
            格式化的进度文本。
        """
        completed = [e for e in result.tool_events if e["status"] == "ok"]  # 已完成的工具事件
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)  # 最近的失败事件
        lines: list[str] = []
        if completed:  # 列出最近 3 个完成步骤
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:  # 列出失败步骤
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:  # 无失败事件但有错误：列出错误
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(
        self,
        workspace: Path | None = None,
        *,
        employee: dict[str, Any] | None = None,
        include_skills: set[str] | None = None,
    ) -> str:
        """Build a focused system prompt for the subagent."""
        """为子 Agent 构建聚焦的系统提示。

        包含运行时上下文、工作区路径、技能摘要与防护等级；
        传入 ``employee`` 时追加其 persona，并按 ``include_skills`` 收窄技能摘要。

        参数:
            workspace: 工作区路径（默认为主工作区）；
            employee: 数字员工记录；提供时以该员工人设执行任务；
            include_skills: 技能 allowlist（``None`` 表示全部，与主会话语义一致）。

        返回:
            渲染后的系统提示字符串。
        """
        from biscuitbot.agent.context import ContextBuilder
        from biscuitbot.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)  # 运行时上下文
        root = workspace or self.workspace
        loader = SkillsLoader(  # 技能摘要
            root,
            disabled_skills=self.disabled_skills,
        )
        if employee is not None:
            skills_summary = loader.build_skills_summary(include=include_skills)
        else:
            skills_summary = loader.build_skills_summary()
        prompt = render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(root),
            skills_summary=skills_summary or "",
            guard_level=self.tools_config.guard_level,
        )
        if employee is not None:
            name = employee.get("name", "")
            prompt += "\n\n" + ContextBuilder._persona_section(employee)
            prompt += f"\n\n你现在以数字员工「{name}」的身份执行任务，完成用户的请求。"
        return prompt

    async def run_employee_inline(
        self,
        employee: dict[str, Any],
        task: str,
        *,
        temperature: float | None = None,
        workspace: Path | None = None,
        include_skills: set[str] | None = None,
        max_result_chars: int | None = None,
    ) -> str:
        """以指定数字员工的人设执行一次任务，**内联返回**成果文本。

        与 ``_run_subagent`` 同构：构建员工提示与消息 → 调用 AgentRunner，
        但不发布消息总线，直接把 ``final_content`` 返回给调用方（主智能体的工具）。

        参数:
            employee: 员工记录（已启用）；
            task: 交给员工的用户任务；
            temperature: 采样温度；
            workspace: 工作目录（默认主工作区）；
            include_skills: 技能 allowlist（``None`` 表示全部）；
            max_result_chars: 内联返回约定的文本上限（``> 0`` 时在系统提示中告知员工
                前置结论与产出文件路径；是否截断/落盘由调用方决定）。

        返回:
            员工执行完成的最终文本；出错时返回面向主智能体的错误描述。
        """
        root = workspace or self.workspace
        tools = self._build_tools(workspace=root)
        system_prompt = self._build_subagent_prompt(
            root,
            employee=employee,
            include_skills=include_skills,
        )
        if max_result_chars and max_result_chars > 0:
            system_prompt += (
                "\n\n【内联返回约定】你的最终回复会被完整保存，但内联回主智能体的文本"
                f"有 {max_result_chars} 字符上限。请把最重要的结论、交付物和产出文件路径"
                "放在最前面；产出的文件请写入当前工作区，并在回复中给出文件路径，"
                "方便主智能体按需读取完整成果。"
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        try:
            result = await self.runner.run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    temperature=temperature,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    max_iterations_message="Task completed but no final response was generated.",
                    finalize_on_max_iterations=False,
                    error_message=None,
                    fail_on_tool_error=True,
                    workspace=root,
                )
            )
        except Exception as e:  # noqa: BLE001 - 员工执行异常应转为文本返回，不让主智能体崩溃
            logger.warning("Employee inline run failed: {}", e)
            return f"数字员工执行失败：{e}"
        if result.stop_reason == "tool_error":
            return self._format_partial_progress(result) or "数字员工执行工具出错。"
        if result.error:
            return f"数字员工执行失败：{result.error}"
        return (result.final_content or "").strip() or "数字员工未返回内容。"

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        """取消指定会话下的所有子 Agent，返回已取消数量。

        参数:
            session_key: 会话标识。

        返回:
            已取消的子 Agent 数量。
        """
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)  # 等待所有取消完成
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        """返回当前运行中的子 Agent 数量。"""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        """返回指定会话下当前运行中的子 Agent 数量。"""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
