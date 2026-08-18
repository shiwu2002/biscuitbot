"""Agent 主循环：核心处理引擎。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块的核心处理引擎。
在项目架构中起到的作用：
- ``AgentLoop`` 是整个 Agent 的中枢，负责从消息总线消费入站消息、按会话串行/
  跨会话并发地派发任务，并驱动一次轮次的完整状态机流转；
- 一次轮次经过 RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE
  的状态机，依次完成会话恢复、自动压缩、命令分发、上下文构建、LLM 迭代执行、
  结果持久化与响应装配；
- 管理 MCP 服务器连接（含跨任务 anyio 取消作用域的处理）、子代理、工具注册表、
  会话锁、并发闸门、模型预设切换、运行时事件发布等子系统能力；
- 通过 ``AutoCompact``/``Consolidator``/``CronTurnCoordinator`` 等协作组件，
  实现空闲会话压缩、按 token 阈值合并、定时轮次调度等高级行为。
"""

from __future__ import annotations

import asyncio  # 异步任务、锁、信号量与队列支持
import dataclasses  # 用于不可变消息的 replace，构造带 override 的副本
import os  # 读取环境变量（如最大并发请求数）
import time  # 时间测量与时间戳生成
from contextlib import AsyncExitStack, nullcontext, suppress  # 异步退出栈与上下文工具
from dataclasses import dataclass, field  # 数据类定义
from enum import Enum, auto  # 枚举类型，用于轮次状态机
from functools import partial  # 偏函数，绑定回调参数
from pathlib import Path  # 路径处理
from typing import TYPE_CHECKING, Any, Awaitable, Callable  # 类型注解支持

from loguru import logger  # 日志记录

from biscuitbot.agent import context as agent_context  # Agent 上下文相关辅助函数集合
from biscuitbot.agent import model_presets as preset_helpers  # 模型预设辅助函数
from biscuitbot.agent.autocompact import AutoCompact  # 空闲会话自动压缩器
from biscuitbot.agent.context import ContextBuilder  # 上下文构建器
from biscuitbot.agent.cron_turns import CronTurnCoordinator  # cron 轮次协调器
from biscuitbot.agent.hook import AgentHook, CompositeHook  # 生命周期钩子与组合钩子
from biscuitbot.agent.memory import Consolidator  # 记忆合并器
from biscuitbot.agent.progress_hook import AgentProgressHook  # 进度/流式输出钩子
from biscuitbot.agent.runner import _MAX_INJECTIONS_PER_TURN, AgentRunner, AgentRunSpec  # Runner 与运行规格
from biscuitbot.agent.subagent import SubagentManager  # 子代理管理器
from biscuitbot.agent.tools.context import RequestContext, bind_request_context, reset_request_context  # 请求上下文绑定
from biscuitbot.agent.tools.file_state import FileStateStore, bind_file_states, reset_file_states  # 文件状态跟踪
from biscuitbot.agent.tools.message import MessageTool  # 消息工具（用于抑制响应等判断）
from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表
from biscuitbot.agent.tools.self import MyTool  # 自我状态查询工具
from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 入/出站消息类型
from biscuitbot.bus.progress import build_bus_progress_callback  # 总线进度回调构建
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.bus.runtime_events import (  # 运行时事件总线与发布器
    RuntimeEventBus,
    RuntimeEventPublisher,
    ensure_runtime_event_publisher,
)
from biscuitbot.command import CommandContext, CommandRouter, register_builtin_commands  # 命令路由与内置命令注册
from biscuitbot.config.schema import AgentDefaults, ModelPresetConfig  # Agent 默认配置与模型预设配置
from biscuitbot.cron.session_turns import (  # cron 会话轮次辅助
    cron_history_overrides,
)
from biscuitbot.providers.base import LLMProvider  # LLM 提供商基类
from biscuitbot.providers.factory import ProviderSnapshot  # 提供商快照
from biscuitbot.security.workspace_access import (  # 工作区访问范围解析与绑定
    WorkspaceScopeResolver,
    bind_workspace_scope,
    reset_workspace_scope,
)
from biscuitbot.session import turn_continuation  # 轮次延续相关逻辑
from biscuitbot.session.goal_state import (  # 目标状态相关
    goal_state_runtime_lines,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from biscuitbot.session.keys import UNIFIED_SESSION_KEY, session_key_for_channel  # 会话 key 生成
from biscuitbot.session.manager import Session, SessionManager  # 会话与 会话管理器
from biscuitbot.utils.document import extract_documents, reference_non_image_attachments  # 文档抽取与附件引用
from biscuitbot.utils.helpers import image_placeholder_text  # 图片占位文本
from biscuitbot.utils.helpers import truncate_text as truncate_text_fn  # 文本截断（别名）
from biscuitbot.utils.image_generation_intent import image_generation_prompt  # 图像生成意图识别
from biscuitbot.utils.llm_runtime import LLMRuntime  # LLM 运行时信息
from biscuitbot.utils.runtime import (  # 运行时常量
    EMPTY_FINAL_RESPONSE_MESSAGE,
)

if TYPE_CHECKING:
    from biscuitbot.config.schema import (
        ChannelsConfig,
        ProviderConfig,
        ToolsConfig,
    )
    from biscuitbot.cron.service import CronService


class TurnState(Enum):
    """一次轮次的状态机枚举。

    状态流转顺序：RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE。
    其中 COMMAND 可由 "shortcut" 事件直接跳到 DONE（快捷命令无需走完整流程）。
    """

    RESTORE = auto()  # 恢复会话检查点/待处理用户轮次，并抽取文档
    COMPACT = auto()  # 自动压缩：准备会话与压缩摘要
    COMMAND = auto()  # 命令分发：判断是否为命令并处理
    BUILD = auto()  # 构建上下文：历史、工具索引、初始消息
    RUN = auto()  # 运行 Agent 迭代循环
    SAVE = auto()  # 保存轮次结果到会话
    RESPOND = auto()  # 装配出站响应
    DONE = auto()  # 完成


@dataclass
class StateTraceEntry:
    """状态机单步执行轨迹条目，用于追踪与运行时事件发布。"""

    state: TurnState  # 该步对应的状态
    started_at: float  # 开始时间（perf_counter）
    duration_ms: float  # 耗时（毫秒）
    event: str  # 该步返回的事件名
    error: str | None = None  # 错误标记（异常时为 "exception"）


@dataclass
class TurnContext:
    """单次轮次的可变上下文，贯穿状态机各阶段。

    职责与项目角色：
    - 承载一次轮次处理过程中的全部中间状态（消息、历史、工具结果、回调等）；
    - 由各状态处理器（``_state_*``）读写，驱动状态机推进；
    - 记录运行轨迹（``trace``）与延迟指标，供运行时事件发布与诊断使用。
    """

    msg: InboundMessage  # 触发本轮的入站消息
    session_key: str  # 会话 key
    state: TurnState  # 当前状态机状态
    turn_id: str  # 轮次唯一标识
    session: Session | None = None  # 会话对象（由 _state_restore 填充）

    history: list[dict[str, Any]] = field(default_factory=list)  # 回放的历史消息
    initial_messages: list[dict[str, Any]] = field(default_factory=list)  # 构建的初始消息列表

    final_content: str | None = None  # 最终正文内容
    tools_used: list[str] = field(default_factory=list)  # 本轮使用的工具名
    all_messages: list[dict[str, Any]] = field(default_factory=list)  # Runner 返回的全部消息
    stop_reason: str = ""  # 停止原因
    had_injections: bool = False  # 是否发生过中途消息注入

    user_persisted_early: bool = False  # 用户消息是否已提前持久化
    save_skip: int = 0  # 保存时跳过的消息条数（已持久化的前缀）

    outbound: OutboundMessage | None = None  # 最终出站消息
    suppress_response: bool = False  # 是否抑制响应输出

    on_progress: Callable[..., Awaitable[None]] | None = None  # 进度回调
    on_stream: Callable[[str], Awaitable[None]] | None = None  # 流式增量回调
    on_stream_end: Callable[..., Awaitable[None]] | None = None  # 流式结束回调
    on_retry_wait: Callable[..., Awaitable[None]] | None = None  # 重试等待回调

    pending_queue: asyncio.Queue | None = None  # 中途注入消息队列
    pending_summary: str | None = None  # 自动压缩摘要

    ephemeral: bool = False  # 是否为临时轮次（不持久化）
    tools: ToolRegistry | None = None  # 本轮使用的工具注册表（可覆盖默认）

    turn_wall_started_at: float = field(default_factory=time.time)  # 轮次墙钟开始时间
    visible_run_started_at: float | None = None  # 可见运行开始时间
    turn_latency_ms: int | None = None  # 轮次延迟（毫秒）

    trace: list[StateTraceEntry] = field(default_factory=list)  # 状态机执行轨迹


class AgentLoop:
    """Agent 核心处理引擎。

    职责与项目角色：
    - 作为整个 Agent 的中枢，从消息总线消费入站消息并按会话派发任务；
    - 驱动一次轮次的完整状态机流转（RESTORE → ... → DONE）；
    - 管理 MCP 连接、子代理、工具注册表、会话锁、并发闸门、模型预设切换等；
    - 协作 ``AutoCompact``/``Consolidator``/``CronTurnCoordinator`` 等组件完成
      压缩、合并与定时调度。

    典型流程：
    1. 从总线接收消息；
    2. 用历史、记忆、技能构建上下文；
    3. 调用 LLM；
    4. 执行工具调用；
    5. 发送响应回去。
    """

    @property
    def current_iteration(self) -> int:
        """当前迭代序号（由 Runner 通过钩子回写）。"""
        return self._current_iteration

    @property
    def tool_names(self) -> list[str]:
        """已注册的工具名列表。"""
        return self.tools.tool_names

    def llm_runtime(self) -> LLMRuntime:
        """返回本循环当前持有的 provider/model 对。"""
        self._refresh_provider_snapshot()
        return LLMRuntime(self.provider, self.model)

    _RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"  # 会话元数据中存放运行时检查点的 key
    _PENDING_USER_TURN_KEY = "pending_user_turn"  # 会话元数据中标记“用户消息已提前持久化但未生成响应”的 key

    # 事件驱动的状态转换表。
    # 各处理器返回一个事件字符串；驱动循环据此查表得到下一状态。
    _TRANSITIONS: dict[tuple[TurnState, str], TurnState] = {
        (TurnState.RESTORE, "ok"): TurnState.COMPACT,
        (TurnState.COMPACT, "ok"): TurnState.COMMAND,
        (TurnState.COMMAND, "dispatch"): TurnState.BUILD,
        (TurnState.COMMAND, "shortcut"): TurnState.DONE,
        (TurnState.BUILD, "ok"): TurnState.RUN,
        (TurnState.RUN, "ok"): TurnState.SAVE,
        (TurnState.SAVE, "ok"): TurnState.RESPOND,
        (TurnState.RESPOND, "ok"): TurnState.DONE,
    }

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int | None = None,
        max_concurrent_subagents: int | None = None,
        context_window_tokens: int | None = None,
        context_block_limit: int | None = None,
        max_tool_result_chars: int | None = None,
        provider_retry_mode: str = "standard",
        tool_hint_max_length: int | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        timezone: str | None = None,
        session_ttl_minutes: int = 0,
        consolidation_ratio: float = 0.5,
        max_messages: int = 120,
        hooks: list[AgentHook] | None = None,
        unified_session: bool = False,
        disabled_skills: list[str] | None = None,
        tools_config: ToolsConfig | None = None,
        image_generation_provider_config: ProviderConfig | None = None,
        image_generation_provider_configs: dict[str, ProviderConfig] | None = None,
        provider_configs: dict[str, ProviderConfig] | None = None,
        provider_snapshot_loader: Callable[..., ProviderSnapshot] | None = None,
        provider_signature: tuple[object, ...] | None = None,
        model_presets: dict[str, ModelPresetConfig] | None = None,
        model_preset: str | None = None,
        preset_snapshot_loader: preset_helpers.PresetSnapshotLoader | None = None,
        runtime_events: RuntimeEventBus | None = None,
        runtime_model_publisher: Callable[[str, str | None], None] | None = None,
        vision_provider_loader: Callable[[], LLMProvider | None] | None = None,
        root_config: Any | None = None,
    ):
        from biscuitbot.config.schema import ToolsConfig

        _tc = tools_config or ToolsConfig()  # 工具配置，缺省时使用空配置
        defaults = AgentDefaults()  # Agent 默认值
        self.bus = bus  # 消息总线
        self.runtime_events = runtime_events or RuntimeEventBus()  # 运行时事件总线
        self.runtime_event_publisher = RuntimeEventPublisher(self.runtime_events)  # 运行时事件发布器
        self.channels_config = channels_config  # 渠道配置
        self.provider = provider  # LLM 提供商
        self._provider_snapshot_loader = provider_snapshot_loader  # 提供商快照加载器（运行时热更新）
        self._preset_snapshot_loader = preset_snapshot_loader  # 模型预设快照加载器
        self._runtime_model_publisher = runtime_model_publisher  # 运行时模型变更发布回调
        self._provider_signature = provider_signature  # 当前提供商签名（用于变更检测）
        self._default_selection_signature = preset_helpers.default_selection_signature(provider_signature)  # 默认选择签名
        self.workspace = workspace  # 工作目录
        self.model = model or provider.get_default_model()  # 当前模型名
        self.max_iterations = (
            max_iterations if max_iterations is not None else defaults.max_tool_iterations
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else defaults.context_window_tokens
        )
        self.context_block_limit = context_block_limit
        self.max_tool_result_chars = (
            max_tool_result_chars
            if max_tool_result_chars is not None
            else defaults.max_tool_result_chars
        )
        self.provider_retry_mode = provider_retry_mode
        self.tool_hint_max_length = (
            tool_hint_max_length if tool_hint_max_length is not None
            else defaults.tool_hint_max_length
        )
        self.tools_config = _tc
        self._root_config = root_config  # 根配置，供需要顶层配置（tts/providers）的工具使用
        self.web_config = _tc.web
        self.exec_config = _tc.exec
        self._image_generation_provider_configs = dict(image_generation_provider_configs or {})
        self._provider_configs = dict(provider_configs or {})  # 统一供应商配置（供各能力工具按 provider 字段取用）
        if (
            image_generation_provider_config is not None
            and "openrouter" not in self._image_generation_provider_configs
        ):
            self._image_generation_provider_configs["openrouter"] = image_generation_provider_config
        self._vision_provider_loader = vision_provider_loader
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.workspace_scopes = WorkspaceScopeResolver(
            default_workspace=workspace,
            default_restrict_to_workspace=restrict_to_workspace,
        )
        self._start_time = time.time()
        self._last_usage: dict[str, int] = {}
        self._extra_hooks: list[AgentHook] = hooks or []

        self.context = ContextBuilder(
            workspace,
            timezone=timezone,
            disabled_skills=disabled_skills,
            guard_level=_tc.guard_level if _tc else "standard",
        )
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        # One file-read/write tracker per logical session. The tool registry is
        # shared by this loop, so tools resolve the active state via contextvars.
        self._file_state_store = FileStateStore()
        self.runner = AgentRunner(provider)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            tools_config=_tc,
            image_generation_provider_configs=self._image_generation_provider_configs,
            max_tool_result_chars=self.max_tool_result_chars,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            max_iterations=self.max_iterations,
            max_concurrent_subagents=max_concurrent_subagents,
            llm_wall_timeout_for_session=lambda sk: runner_wall_llm_timeout_s(self.sessions, sk),
        )
        self._unified_session = unified_session  # 是否启用统一会话模式
        self._max_messages = max_messages if max_messages > 0 else 120  # 历史回放最大消息数
        self._running = False  # 主循环运行标志
        self._mcp_servers = mcp_servers or {}  # 配置的 MCP 服务器
        self._mcp_stacks: dict[str, AsyncExitStack] = {}  # 已建立的 MCP 连接栈：server_name -> AsyncExitStack
        # anyio 取消作用域（MCP stdio_client 使用）是任务局部的：必须在进入它们的
        # 同一任务中退出。_mcp_owner_task 记录调用 _connect_mcp 的任务（即 run()）。
        # 当 _dispatch 子任务触发重连时，无法直接关闭旧栈——而是将栈移到
        # _mcp_deferred_stacks，由 owner 任务通过 _close_deferred_mcp_stacks() 关闭。
        self._mcp_owner_task: asyncio.Task | None = None  # MCP owner 任务（建立连接的任务）
        self._mcp_deferred_stacks: list[tuple[str, AsyncExitStack]] = []  # 待 owner 任务关闭的延迟栈
        # 从 _dispatch 子任务延迟的重连请求。每条为 (server_name, tool_name, stale_tool, future)。
        # 子任务入队并 await future；owner 任务从 _run_main_loop 的空闲分支通过
        # _process_mcp_reconnects() 排空队列并自行执行 _refresh_terminated_server，
        # 使新栈的取消作用域在 owner 任务中进入（与关闭处匹配）。
        self._mcp_reconnect_requests: list[tuple[str, str, Any, asyncio.Future]] = []  # 延迟重连请求列表
        self._mcp_connected = False  # MCP 是否已连接
        self._mcp_connecting = False  # MCP 是否正在连接中
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> 活跃任务列表
        self._background_tasks: list[asyncio.Task] = []  # 后台任务列表（关闭时排空）
        self._session_locks: dict[str, asyncio.Lock] = {}  # 会话级锁，保证同会话串行
        # 每会话的中途注入消息队列。
        # 当某会话有活跃任务时，发往该会话的新消息会被路由到这里，
        # 而不是创建一个竞争任务。
        self._pending_queues: dict[str, asyncio.Queue] = {}  # session_key -> 注入队列
        self._cron_turns = CronTurnCoordinator(  # cron 轮次协调器
            publish_inbound=self.bus.publish_inbound,
            dispatch=self._dispatch,
            is_running=lambda: self._running,
        )
        # BISCUITBOT_MAX_CONCURRENT_REQUESTS：<=0 表示不限；默认 3。
        _max = int(os.environ.get("BISCUITBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (  # 跨会话并发闸门
            asyncio.Semaphore(_max) if _max > 0 else None
        )
        self.consolidator = Consolidator(
            store=self.context.memory,
            provider=provider,
            model=self.model,
            sessions=self.sessions,
            context_window_tokens=self.context_window_tokens,
            build_messages=self.context.build_messages,
            get_tool_definitions=self.tools.get_definitions,
            max_completion_tokens=provider.generation.max_tokens,
            consolidation_ratio=consolidation_ratio,
            unified_session=unified_session,
        )
        self.auto_compact = AutoCompact(
            sessions=self.sessions,
            consolidator=self.consolidator,
            session_ttl_minutes=session_ttl_minutes,
        )
        self.model_presets: dict[str, ModelPresetConfig] = model_presets or {}
        self._active_preset: str | None = None
        if model_preset:
            self.set_model_preset(model_preset, publish_update=False)
        self._register_default_tools()
        self._runtime_vars: dict[str, Any] = {}
        self._current_iteration: int = 0
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    @classmethod
    def from_config(
        cls,
        config: Any,
        bus: MessageBus | None = None,
        **extra: Any,
    ) -> AgentLoop:
        """Create an AgentLoop from config with the common parameter set.

        Extra keyword arguments are forwarded to ``AgentLoop.__init__``,
        allowing callers to override or extend the standard config-derived
        parameters (e.g. ``cron_service``, ``session_manager``).
        """
        from biscuitbot.providers.factory import make_provider

        if bus is None:
            bus = MessageBus()
        defaults = config.agents.defaults
        provider = extra.pop("provider", None) or make_provider(config)
        resolved = config.resolve_preset()
        model = extra.pop("model", None) or resolved.model
        context_window_tokens = extra.pop("context_window_tokens", None) or resolved.context_window_tokens
        provider_snapshot_loader = extra.pop("provider_snapshot_loader", None)
        preset_snapshot_loader = extra.pop("preset_snapshot_loader", None) or preset_helpers.make_preset_snapshot_loader(
            config,
            provider_snapshot_loader,
        )
        vision_provider_loader = extra.pop("vision_provider_loader", None)
        if vision_provider_loader is None:
            from biscuitbot.providers.factory import build_vision_provider

            _config_ref = config

            def _default_vision_loader():
                try:
                    return build_vision_provider(_config_ref)
                except Exception:
                    logger.debug("Vision provider build failed, vision disabled", exc_info=True)
                    return None

            vision_provider_loader = _default_vision_loader
        provider_configs = extra.pop("provider_configs", None)
        if provider_configs is None:
            from biscuitbot.providers.image_generation import unified_provider_configs

            provider_configs = unified_provider_configs(config)
        return cls(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            provider_configs=provider_configs,
            model=model,
            max_iterations=defaults.max_tool_iterations,
            max_concurrent_subagents=defaults.max_concurrent_subagents,
            context_window_tokens=context_window_tokens,
            context_block_limit=defaults.context_block_limit,
            max_tool_result_chars=defaults.max_tool_result_chars,
            provider_retry_mode=defaults.provider_retry_mode,
            tool_hint_max_length=defaults.tool_hint_max_length,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            timezone=defaults.timezone,
            unified_session=defaults.unified_session,
            disabled_skills=defaults.disabled_skills,
            session_ttl_minutes=defaults.session_ttl_minutes,
            consolidation_ratio=defaults.consolidation_ratio,
            max_messages=defaults.max_messages,
            tools_config=config.tools,
            model_presets=preset_helpers.configured_model_presets(config),
            model_preset=defaults.model_preset,
            provider_snapshot_loader=provider_snapshot_loader,
            preset_snapshot_loader=preset_snapshot_loader,
            vision_provider_loader=vision_provider_loader,
            root_config=config,
            **extra,
        )

    def _sync_subagent_runtime_limits(self) -> None:
        """Keep subagent runtime limits aligned with mutable loop settings."""
        self.subagents.max_iterations = self.max_iterations

    def _apply_provider_snapshot(
        self,
        snapshot: ProviderSnapshot,
        *,
        publish_update: bool = True,
        model_preset: str | None = None,
    ) -> None:
        """Swap model/provider for future turns without disturbing an active one."""
        provider = snapshot.provider
        model = snapshot.model
        context_window_tokens = snapshot.context_window_tokens
        old_model = self.model
        self.provider = provider
        self.model = model
        self.context_window_tokens = context_window_tokens
        self.runner.provider = provider
        self.subagents.set_provider(provider, model)
        self.consolidator.set_provider(provider, model, context_window_tokens)
        self._provider_signature = snapshot.signature
        if publish_update and self._runtime_model_publisher is not None:
            self._runtime_model_publisher(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        if publish_update:
            self._runtime_events().runtime_model_changed(
                self.model,
                model_preset if model_preset is not None else self.model_preset,
            )
        logger.info("Runtime model switched for next turn: {} -> {}", old_model, model)

    def _refresh_provider_snapshot(self) -> None:
        if self._provider_snapshot_loader is None:
            return
        try:
            snapshot = self._provider_snapshot_loader()
        except Exception:
            logger.exception("Failed to refresh provider config")
            return
        default_selection = preset_helpers.default_selection_signature(snapshot.signature)
        if self._active_preset and self._default_selection_signature in (None, default_selection):
            self._default_selection_signature = default_selection
            try:
                snapshot = self._build_model_preset_snapshot(self._active_preset)
            except Exception:
                logger.exception("Failed to refresh active model preset")
                return
        else:
            self._active_preset = None
            self._default_selection_signature = default_selection
        if snapshot.signature == self._provider_signature:
            return
        self._default_selection_signature = preset_helpers.default_selection_signature(snapshot.signature)
        self._apply_provider_snapshot(snapshot)

    @property
    def model_preset(self) -> str | None:
        return self._active_preset

    @model_preset.setter
    def model_preset(self, name: str | None) -> None:
        self.set_model_preset(name)

    @property
    def workspace_sandbox(self) -> Any:
        """Workspace sandbox status (read-only view for tools)."""
        return self.workspace_scopes.sandbox_status

    def _build_model_preset_snapshot(self, name: str) -> ProviderSnapshot:
        return preset_helpers.build_runtime_preset_snapshot(
            name=name,
            presets=self.model_presets,
            provider=self.provider,
            loader=self._preset_snapshot_loader,
        )

    def set_model_preset(self, name: str | None, *, publish_update: bool = True) -> None:
        """Resolve a preset by name and apply all runtime model dependents."""
        name = preset_helpers.normalize_preset_name(name, self.model_presets)
        snapshot = self._build_model_preset_snapshot(name)
        self._apply_provider_snapshot(snapshot, publish_update=publish_update, model_preset=name)
        self._active_preset = name

    def _register_default_tools(self) -> None:
        """Register the default set of tools via plugin loader."""
        from biscuitbot.agent.tools.context import ToolContext
        from biscuitbot.agent.tools.loader import ToolLoader

        ctx = ToolContext(
            config=self.tools_config,
            root_config=self._root_config,
            workspace=str(self.workspace),
            bus=self.bus,
            subagent_manager=self.subagents,
            employees=getattr(self.context, "employees", None),
            cron_service=self.cron_service,
            sessions=self.sessions,
            provider_snapshot_loader=self._provider_snapshot_loader,
            image_generation_provider_configs=self._image_generation_provider_configs,
            provider_configs=self._provider_configs,
            vision_provider_loader=self._vision_provider_loader,
            timezone=self.context.timezone or "UTC",
            workspace_sandbox=self.workspace_scopes.sandbox_status,
            runtime_events=self.runtime_events,
        )
        loader = ToolLoader()
        registered = loader.load(ctx, self.tools)

        # MyTool needs runtime state reference — manual registration
        if self.tools_config.my.enable:
            self.tools.register(
                MyTool(runtime_state=self, modify_allowed=self.tools_config.my.allow_set)
            )
            registered.append("my")

        # ScreenshotTool needs vision_provider_loader — manual registration
        if self.tools_config.screenshot.enable:
            from biscuitbot.agent.tools.screenshot import ScreenshotTool

            self.tools.register(
                ScreenshotTool(  # type: ignore[abstract]
                    vision_provider_loader=self._vision_provider_loader,
                    config=self.tools_config.screenshot,
                )
            )
            registered.append("screenshot")

        # DiscoverToolsTool is auto-registered in dynamic mode; bind the
        # registry so the meta-tool can search all registered tools.
        discover_tool = self.tools.get("discover_tools")
        if discover_tool is not None and hasattr(discover_tool, "bind_registry"):
            discover_tool.bind_registry(self.tools)  # type: ignore[attr-defined]
            registered.append("discover_tools")

        # Register the register_tool / unregister_tool meta-tools so the
        # agent can install custom tools at runtime.  These are always
        # available (always_include=True) and need the registry bound.
        from biscuitbot.agent.tools.register_tool import (
            RegisterToolTool,
            UnregisterToolTool,
        )

        register_tool = RegisterToolTool()  # type: ignore[abstract]
        register_tool.bind_registry(self.tools)
        self.tools.register(register_tool)
        registered.append("register_tool")

        unregister_tool = UnregisterToolTool()  # type: ignore[abstract]
        unregister_tool.bind_registry(self.tools)
        self.tools.register(unregister_tool)
        registered.append("unregister_tool")

        # Initialize usage stats for cold-storage rotation.  Persists to
        # workspace/.agent_tools/ so cold tools survive restarts.
        from biscuitbot.agent.tools.usage_stats import UsageStats

        stats_dir = self.workspace / ".agent_tools"
        self._usage_stats = UsageStats(base_dir=stats_dir)
        self.tools.set_usage_stats(self._usage_stats)

        # ColdStorageTool lets the agent search tools rotated to cold storage.
        from biscuitbot.agent.tools.cold_storage import ColdStorageTool

        cold_storage = ColdStorageTool()  # type: ignore[abstract]
        cold_storage.bind_usage_stats(self._usage_stats)
        self.tools.register(cold_storage)
        registered.append("cold_storage")

        # Re-load any custom tools persisted from a previous run.
        self._load_custom_tools_manifest()

        logger.info("Registered {} tools: {}", len(registered), registered)

    def _build_tool_index(self, skill_names: set[str] | None = None) -> str:
        """Generate the Tools & Skills Index for the system prompt.

        Merges registered tools with the skills loader so the model sees
        a single unified discovery table.

        参数:
            skill_names: 可选的技能 allowlist；设置后仅列出这些技能（用于按数字人员工限定范围）。
        """
        skills_entries: list[dict[str, str]] | None = None
        try:
            skills_loader = getattr(self.context, "skills", None)
            if skills_loader is not None:
                listed = skills_loader.list_skills()
                if skill_names is not None:
                    listed = [s for s in listed if s["name"] in skill_names]
                skills_entries = [
                    {
                        "name": s["name"],
                        "capability": skills_loader._get_skill_description(s["name"]),
                        "usage_md": s["path"],
                    }
                    for s in listed
                ]
        except Exception:
            logger.debug("Failed to list skills for tool index", exc_info=True)
        return self.tools.generate_index(skills_entries)

    def _load_custom_tools_manifest(self) -> None:
        """Re-register custom tools from ``workspace/.agent_tools/manifest.json``."""
        try:
            manifest_path = self.workspace / ".agent_tools" / "manifest.json"
            if not manifest_path.is_file():
                return
            import json as _json

            data = _json.loads(manifest_path.read_text(encoding="utf-8"))
            tools_list = data.get("tools", []) if isinstance(data, dict) else []
            errors = self.tools.load_custom_tools_from_manifest(tools_list)
            for err in errors:
                logger.warning("custom tool load failed: {}", err)
        except Exception as e:
            logger.warning("Failed to load custom tools manifest: {}", e)

    def _save_custom_tools_manifest(self) -> None:
        """Persist the current custom-tool registry for the next run."""
        try:
            manifest = self.tools.custom_tools_manifest()
            if not manifest:
                return
            manifest_dir = self.workspace / ".agent_tools"
            manifest_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = manifest_dir / "manifest.json"
            import json as _json

            manifest_path.write_text(
                _json.dumps({"tools": manifest}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to save custom tools manifest: {}", e)

    async def _connect_mcp(self) -> None:
        """Connect configured MCP servers."""
        await agent_context.connect_mcp(self, self.tools)

    def _set_tool_context(
        self, channel: str, chat_id: str,
        message_id: str | None = None, metadata: dict | None = None,
        session_key: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        from biscuitbot.agent.tools.context import ContextAware

        effective_key = session_key or session_key_for_channel(
            channel,
            chat_id,
            unified_session=self._unified_session,
        )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=effective_key,
            metadata=dict(metadata or {}),
        )

        for name in self.tools.tool_names:
            tool = self.tools.get(name)
            if tool and isinstance(tool, ContextAware):
                tool.set_context(request_ctx)

    @staticmethod
    def _runtime_chat_id(msg: InboundMessage) -> str:
        """Return the chat id shown in runtime metadata for the model."""
        return str(msg.metadata.get("context_chat_id") or msg.chat_id)

    async def _build_bus_progress_callback(
        self, msg: InboundMessage
    ) -> Callable[..., Awaitable[None]]:
        """Build a progress callback that publishes to the message bus."""
        return build_bus_progress_callback(self.bus, msg)

    async def _build_retry_wait_callback(
        self, msg: InboundMessage
    ) -> Callable[[str], Awaitable[None]]:
        """Build a retry-wait callback that publishes to the message bus."""

        async def _on_retry_wait(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_retry_wait"] = True
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                )
            )

        return _on_retry_wait

    def _runtime_events(self) -> RuntimeEventPublisher:
        return ensure_runtime_event_publisher(self)

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        return await self._cron_turns.submit(msg)

    def pending_cron_job_ids_for_session(self, session_key: str) -> set[str]:
        return self._cron_turns.pending_job_ids_for_session(session_key)

    def _persist_user_message_early(
        self,
        msg: InboundMessage,
        session: Session,
        **kwargs: Any,
    ) -> bool:
        """Persist the triggering user message before the turn starts.

        Returns True if the message was persisted.
        """
        if not turn_continuation.should_persist_user_message(msg.metadata):
            return False
        media_paths = [p for p in (msg.media or []) if isinstance(p, str) and p]
        has_text = isinstance(msg.content, str) and msg.content.strip()
        if has_text or media_paths:
            extra: dict[str, Any] = ({"media": list(media_paths)} if media_paths else {}) | agent_context.session_extra(msg.metadata)
            extra.update(kwargs)
            text = msg.content if isinstance(msg.content, str) else ""
            text_override, cron_extra = cron_history_overrides(msg.metadata)
            if text_override is not None:
                text = text_override
            extra.update(cron_extra)
            session.add_message("user", text, **extra)
            self._mark_pending_user_turn(session)
            self.sessions.save(session)
            return True
        return False

    def _build_initial_messages(
        self,
        msg: InboundMessage,
        session: Session,
        history: list[dict[str, Any]],
        pending_summary: str | None,
        include_memory_recent_history: bool = True,
    ) -> list[dict[str, Any]]:
        """Build the initial message list for the LLM turn."""
        scope = self.workspace_scopes.for_message(msg, session.metadata)
        # Always inject the Tools & Skills Index so the model can discover
        # on-demand tools by name + capability + usage_doc path.
        # 能力归属自己：绑定数字员工时工具索引只列该员工自己的能力；主会话共享全部。
        tool_index = self._build_tool_index(
            skill_names=self.context._employee_capability_allowlist(session.metadata)
        )
        return self.context.build_messages(
            history=history,
            current_message=image_generation_prompt(msg.content, msg.metadata),
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=self._runtime_chat_id(msg),
            sender_id=msg.sender_id,
            session_summary=pending_summary,
            session_metadata=session.metadata,
            workspace=scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            include_memory_recent_history=include_memory_recent_history,
            session_key=session.key,
            unified_session=self._unified_session,
            tool_index=tool_index,
        )

    async def _dispatch_command_inline(
        self,
        msg: InboundMessage,
        key: str,
        raw: str,
        dispatch_fn: Callable[[CommandContext], Awaitable[OutboundMessage | None]],
    ) -> None:
        """Dispatch a command directly from the run() loop and publish the result."""
        ctx = CommandContext(msg=msg, session=None, key=key, raw=raw, loop=self)
        result = await dispatch_fn(ctx)
        if result:
            await self.bus.publish_outbound(result)
        else:
            logger.warning("Command '{}' matched but dispatch returned None", raw)

    async def _cancel_active_tasks(self, key: str) -> int:
        """Cancel and await all active tasks and subagents for *key*.

        Returns the total number of cancelled tasks + subagents.
        """
        tasks = self._active_tasks.pop(key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await t
        sub_cancelled = await self.subagents.cancel_by_session(key)
        return cancelled + sub_cancelled

    def _effective_session_key(self, msg: InboundMessage) -> str:
        """Return the session key used for task routing and mid-turn injections."""
        if self._unified_session and not msg.session_key_override:
            return UNIFIED_SESSION_KEY
        return msg.session_key

    def _replay_token_budget(self) -> int:
        """Derive a token budget for session history replay from the context window."""
        if self.context_window_tokens <= 0:
            return 0
        max_output = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        try:
            reserved_output = int(max_output)
        except (TypeError, ValueError):
            reserved_output = 4096
        budget = self.context_window_tokens - max(1, reserved_output) - 1024
        return budget if budget > 0 else max(128, self.context_window_tokens // 2)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        on_retry_wait: Callable[..., Awaitable[None]] | None = None,
        session: Session | None = None,
        channel: str = "",
        chat_id: str = "",
        message_id: str | None = None,
        metadata: dict | None = None,
        session_key: str | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
        turn_id: str = "",
        inbound_msg: InboundMessage | None = None,
    ) -> tuple[str | None, list[str], list[dict], str, bool]:
        """Run the agent iteration loop.

        *on_stream*: called with each content delta during streaming.
        *on_stream_end(resuming)*: called when a streaming session finishes.
        ``resuming=True`` means tool calls follow (spinner should restart);
        ``resuming=False`` means this is the final response.

        Returns (final_content, tools_used, messages, stop_reason, had_injections).
        """
        self._sync_subagent_runtime_limits()

        loop_hook = AgentProgressHook(
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            metadata=metadata,
            session_key=session_key,
            tool_hint_max_length=self.tool_hint_max_length,
            set_tool_context=self._set_tool_context,
            on_iteration=lambda iteration: setattr(self, "_current_iteration", iteration),
        )
        hook: AgentHook = loop_hook
        if not ephemeral and self._extra_hooks:
            hook = CompositeHook([loop_hook] + self._extra_hooks)

        async def _checkpoint(payload: dict[str, Any]) -> None:
            if session is None:
                return
            self._set_runtime_checkpoint(session, payload)

        async def _drain_pending(*, limit: int = _MAX_INJECTIONS_PER_TURN) -> list[dict[str, Any]]:
            """Drain follow-up messages from the pending queue.

            When no messages are immediately available but sub-agents
            spawned in this dispatch are still running, blocks until at
            least one result arrives (or timeout).  This keeps the runner
            loop alive so subsequent sub-agent completions are consumed
            in-order rather than dispatched separately.
            """
            if pending_queue is None:
                return []

            def _to_user_message(pending_msg: InboundMessage) -> dict[str, Any]:
                content = pending_msg.content
                media = pending_msg.media if pending_msg.media else None
                if media:
                    content, media = self._prepare_message_media(content, media)
                    media = media or None
                user_content = self.context._build_user_content(content, media)
                return {"role": "user", "content": user_content}

            items: list[dict[str, Any]] = []
            while len(items) < limit:
                try:
                    items.append(_to_user_message(pending_queue.get_nowait()))
                except asyncio.QueueEmpty:
                    break

            # Block if nothing drained but sub-agents spawned in this dispatch
            # are still running.  Keeps the runner loop alive so subsequent
            # completions are injected in-order rather than dispatched separately.
            if (not items
                    and session is not None
                    and self.subagents.get_running_count_by_session(session.key) > 0):
                try:
                    msg = await asyncio.wait_for(pending_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout waiting for sub-agent completion in session {}",
                        session.key,
                    )
                    return items
                items.append(_to_user_message(msg))
                while len(items) < limit:
                    try:
                        items.append(_to_user_message(pending_queue.get_nowait()))
                    except asyncio.QueueEmpty:
                        break

            return items

        active_session_key = session.key if session else session_key
        effective_scope = self.workspace_scopes.for_turn(
            channel=channel,
            message_metadata=metadata,
            session_metadata=session.metadata if session is not None else None,
        )
        request_ctx = RequestContext(
            channel=channel,
            chat_id=chat_id,
            message_id=message_id,
            session_key=active_session_key,
            metadata=dict(metadata or {}),
        )
        file_state_token = bind_file_states(self._file_state_store.for_session(active_session_key))
        request_token = bind_request_context(request_ctx)
        workspace_token = bind_workspace_scope(effective_scope)
        # Compute lazily because long_task may create goal metadata during this run.
        def _goal_continue() -> str | None:
            _goal_lines = goal_state_runtime_lines(session.metadata if session is not None else None)
            if not _goal_lines:
                return None
            return (
                "You have an active sustained goal:\n\n"
                + "\n".join(_goal_lines)
                + "\n\nPlease continue working toward the objective using your tools, "
                "or call complete_goal if the work is truly finished."
            )

        session_metadata = session.metadata if session is not None else None
        try:
            result = await self.runner.run(AgentRunSpec(
                initial_messages=initial_messages,
                tools=tools or self.tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=hook,
                error_message="Sorry, I encountered an error calling the AI model.",
                concurrent_tools=True,
                workspace=effective_scope.project_path,
                session_key=session.key if session else None,
                context_window_tokens=self.context_window_tokens,
                context_block_limit=self.context_block_limit,
                provider_retry_mode=self.provider_retry_mode,
                progress_callback=on_progress,
                stream_progress_deltas=on_stream is not None,
                retry_wait_callback=on_retry_wait,
                checkpoint_callback=_checkpoint,
                injection_callback=_drain_pending,
                # Sustained goals may legitimately exceed BISCUITBOT_LLM_TIMEOUT_S; idle stall
                # is still capped by BISCUITBOT_STREAM_IDLE_TIMEOUT_S in streaming providers.
                llm_timeout_s=runner_wall_llm_timeout_s(
                    self.sessions,
                    session.key if session is not None else session_key,
                    metadata=session_metadata,
                    message_metadata=metadata,
                ),
                goal_active_predicate=lambda: sustained_goal_active(session.metadata) if session is not None else False,
                goal_continue_message=_goal_continue,
                finalize_on_max_iterations=turn_continuation.should_finalize_on_max_iterations(
                    pending_queue_available=pending_queue is not None and session is not None,
                    session_metadata=session_metadata,
                    message_metadata=metadata,
                ),
                turn_id=turn_id,
                runtime_publisher=self._runtime_events(),
                inbound_msg=inbound_msg,
            ))
        finally:
            reset_workspace_scope(workspace_token)
            reset_request_context(request_token)
            reset_file_states(file_state_token)
        self._last_usage = result.usage
        if result.stop_reason == "max_iterations":
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            should_stream = turn_continuation.should_stream_budget_response(
                stop_reason=result.stop_reason,
                pending_queue_available=pending_queue is not None and session is not None,
                session_metadata=session_metadata,
                message_metadata=metadata,
            )
            # Push final content through stream so streaming channels (e.g. Feishu)
            # update the card instead of leaving it empty.
            if on_stream and on_stream_end and should_stream:
                await on_stream(result.final_content or "")
                await on_stream_end(resuming=False)
        elif result.stop_reason == "error":
            logger.error("LLM returned error: {}", (result.final_content or "")[:200])
        return result.final_content, result.tools_used, result.messages, result.stop_reason, result.had_injections

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        self._mcp_owner_task = asyncio.current_task()
        try:
            await self._connect_mcp()
            logger.info("Agent loop started")
            await self._run_main_loop()
        finally:
            # MCP stdio servers use anyio cancel scopes, which are task-local:
            # they must be exited in the same task that entered them. Since
            # _connect_mcp ran inside THIS task, close_mcp must too — otherwise
            # anyio raises "Attempted to exit cancel scope in a different task
            # than it was entered in" when the caller (e.g. commands.py) tries
            # to close the stacks from a different task via gather/finally.
            #
            # If this task is being cancelled, uncancel it first so the cleanup
            # awaits below can run to completion instead of being interrupted
            # again before the stacks are closed.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                current.uncancel()
            with suppress(Exception):
                await self.close_mcp()

    async def _run_main_loop(self) -> None:
        """Consume inbound messages and dispatch them as per-session tasks."""
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                # Close any MCP stacks deferred from _dispatch sub-tasks (e.g.
                # reconnect-after-timeout). This must run in the owner task.
                await self._close_deferred_mcp_stacks()
                # Run reconnect requests deferred from _dispatch sub-tasks.
                # _refresh_terminated_server enters anyio cancel scopes via
                # connect_mcp_servers, so it must run in this (owner) task to
                # keep them task-affined — otherwise close_mcp() can't exit
                # them and anyio raises a cross-task cancel-scope error.
                await self._process_mcp_reconnects()
                self.auto_compact.check_expired(
                    self._schedule_background,
                    active_session_keys=self._pending_queues.keys(),
                )
                continue
            except asyncio.CancelledError:
                # Preserve real task cancellation so shutdown can complete cleanly.
                # Only ignore non-task CancelledError signals that may leak from integrations.
                current_task = asyncio.current_task()
                if not self._running or (current_task is not None and current_task.cancelling()):
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            raw = msg.content.strip()
            effective_key = self._effective_session_key(msg)
            if await agent_context.handle_runtime_control(self, msg, self.tools):
                continue
            if self.commands.is_priority(raw):
                await self._dispatch_command_inline(
                    msg, effective_key, raw,
                    self.commands.dispatch_priority,
                )
                continue
            if self._cron_turns.defer_if_active(
                msg,
                session_key=effective_key,
                active_session_keys=self._pending_queues.keys(),
            ):
                logger.info(
                    "Deferred cron turn for active session {}",
                    effective_key,
                )
                continue
            # If this session already has an active pending queue (i.e. a task
            # is processing this session), route the message there for mid-turn
            # injection instead of creating a competing task.
            if effective_key in self._pending_queues:
                # Non-priority commands must not be queued for injection;
                # dispatch them directly (same pattern as priority commands).
                if self.commands.is_dispatchable_command(raw):
                    await self._dispatch_command_inline(
                        msg, effective_key, raw,
                        self.commands.dispatch,
                    )
                    continue
                pending_msg = msg
                if effective_key != msg.session_key:
                    pending_msg = dataclasses.replace(
                        msg,
                        session_key_override=effective_key,
                    )
                try:
                    self._pending_queues[effective_key].put_nowait(pending_msg)
                except asyncio.QueueFull:
                    logger.warning(
                        "Pending queue full for session {}, falling back to queued task",
                        effective_key,
                    )
                else:
                    logger.info(
                        "Routed follow-up message to pending queue for session {}",
                        effective_key,
                    )
                    continue
            # Compute the effective session key before dispatching
            # This ensures /stop command can find tasks correctly when unified session is enabled
            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(effective_key, []).append(task)
            task.add_done_callback(
                lambda t, k=effective_key: self._active_tasks.get(k, [])
                and self._active_tasks[k].remove(t)
                if t in self._active_tasks.get(k, [])
                else None
            )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message: per-session serial, cross-session concurrent."""
        session_key = self._effective_session_key(msg)
        if session_key != msg.session_key:
            msg = dataclasses.replace(msg, session_key_override=session_key)
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()

        pending: asyncio.Queue | None = None
        try:
            async with lock, gate:
                # Only the task that owns the session lock may publish the
                # active mid-turn injection queue for this session.
                pending = asyncio.Queue(maxsize=20)
                self._pending_queues[session_key] = pending
                try:
                    on_stream: Callable[[str], Awaitable[None]] | None = None
                    on_stream_end: Callable[..., Awaitable[None]] | None = None
                    if msg.metadata.get("_wants_stream"):
                        # Split one answer into distinct stream segments.
                        stream_base_id = f"{msg.session_key}:{time.time_ns()}"
                        stream_segment = 0

                        def _current_stream_id() -> str:
                            return f"{stream_base_id}:{stream_segment}"

                        async def _on_stream(delta: str) -> None:
                            meta = dict(msg.metadata or {})
                            meta["_stream_delta"] = True
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content=delta,
                                metadata=meta,
                            ))

                        async def _on_stream_end(*, resuming: bool = False) -> None:
                            nonlocal stream_segment
                            meta = dict(msg.metadata or {})
                            meta["_stream_end"] = True
                            meta["_resuming"] = resuming
                            meta["_stream_id"] = _current_stream_id()
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="",
                                metadata=meta,
                            ))
                            stream_segment += 1

                        on_stream = _on_stream
                        on_stream_end = _on_stream_end

                    response = await self._process_message(
                        msg, on_stream=on_stream, on_stream_end=on_stream_end,
                        pending_queue=pending,
                    )
                    completed_channel = msg.channel
                    completed_chat_id = msg.chat_id
                    if response is not None:
                        await self.bus.publish_outbound(response)
                        completed_channel = response.channel
                        completed_chat_id = response.chat_id
                    elif msg.channel == "cli":
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel, chat_id=msg.chat_id,
                            content="", metadata=msg.metadata or {},
                        ))
                    continuing = turn_continuation.internal_continuation_pending(msg.metadata)
                    if not continuing:
                        await self._runtime_events().turn_completed(
                            channel=completed_channel,
                            chat_id=completed_chat_id,
                            session_key=session_key,
                            metadata=msg.metadata,
                        )
                    self._cron_turns.complete(msg, response=response)
                except asyncio.CancelledError:
                    self._cron_turns.complete(
                        msg,
                        error=asyncio.CancelledError(),
                    )
                    logger.info("Task cancelled for session {}", session_key)
                    # Preserve partial context from the interrupted turn so
                    # the user does not lose tool results and assistant
                    # messages accumulated before /stop.  The checkpoint was
                    # already persisted to session metadata by
                    # _emit_checkpoint during tool execution; materializing
                    # it into session history now makes it visible in the
                    # next conversation turn.
                    try:
                        key = self._effective_session_key(msg)
                        session = self.sessions.get_or_create(key)
                        if self._restore_runtime_checkpoint(session):
                            self._clear_pending_user_turn(session)
                            self.sessions.save(session)
                            logger.info(
                                "Restored partial context for cancelled session {}",
                                key,
                            )
                    except Exception:
                        logger.debug(
                            "Could not restore checkpoint for cancelled session {}",
                            session_key,
                            exc_info=True,
                        )
                    raise
                except Exception as exc:
                    logger.exception("Error processing message for session {}", session_key)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    ))
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        await self._runtime_events().turn_completed(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            session_key=session_key,
                            metadata=msg.metadata,
                        )
                    self._cron_turns.complete(msg, error=exc)
                finally:
                    # Drain any messages still in the pending queue and re-publish
                    # them to the bus so they are processed as fresh inbound messages
                    # rather than silently lost.  Only remove our own queue; a
                    # later task waiting on the lock must not be able to steal
                    # cleanup ownership.
                    queue = None
                    if self._pending_queues.get(session_key) is pending:
                        queue = self._pending_queues.pop(session_key, None)
                    else:
                        queue = pending
                    if queue is not None:
                        leftover = 0
                        while True:
                            try:
                                item = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await self.bus.publish_inbound(item)
                            leftover += 1
                        if leftover:
                            logger.info(
                                "Re-published {} leftover message(s) to bus for session {}",
                                leftover, session_key,
                            )
                    if not turn_continuation.internal_continuation_pending(msg.metadata):
                        await self._runtime_events().run_status_changed(
                            msg, session_key, "idle"
                        )
                        self._runtime_events().clear_turn(session_key)
                    await self._cron_turns.publish_next_deferred(session_key)
        finally:
            if pending is None:
                await self._runtime_events().run_status_changed(
                    msg, session_key, "idle"
                )
                self._runtime_events().clear_turn(session_key)
                await self._cron_turns.publish_next_deferred(session_key)

    async def _close_deferred_mcp_stacks(self) -> None:
        """Close MCP stacks deferred from other tasks (e.g. _dispatch sub-tasks).

        Must run in the owner task — the one that called _connect_mcp and
        entered the anyio cancel scopes. Called periodically from the main
        loop and during shutdown via close_mcp().
        """
        while self._mcp_deferred_stacks:
            name, stack = self._mcp_deferred_stacks.pop()
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' deferred cleanup error", name)

    async def _process_mcp_reconnects(self) -> None:
        """Run MCP reconnect requests deferred from _dispatch sub-tasks.

        Must run in the owner task: ``_refresh_terminated_server`` calls
        ``connect_mcp_servers``, which enters anyio cancel scopes via
        ``stdio_client``. Those scopes are task-local and must be exited in
        the same task, so the reconnect has to happen here (the owner) rather
        than in the sub-task that detected the stale session. See
        ``mcp._attach_reconnect_handlers`` for the deferral handshake.
        """
        await agent_context.process_pending_reconnects(self, self.tools)

    async def close_mcp(self) -> None:
        """Drain pending background archives, then close MCP connections."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        # Cancel any pending reconnect futures so sub-tasks blocked on them
        # don't hang during shutdown. They'll raise CancelledError and unwind.
        for _server, _tool, _stale, future in self._mcp_reconnect_requests:
            if not future.done():
                future.cancel()
        self._mcp_reconnect_requests.clear()
        await self._close_deferred_mcp_stacks()
        for name, stack in self._mcp_stacks.items():
            try:
                await stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                logger.debug("MCP server '{}' cleanup error (can be ignored)", name)
        self._mcp_stacks.clear()

    def _schedule_background(self, coro) -> None:
        """Schedule a coroutine as a tracked background task (drained on shutdown)."""
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        task.add_done_callback(self._background_tasks.remove)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_system_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
    ) -> OutboundMessage | None:
        """Process a system inbound message (e.g. subagent announce)."""
        channel, chat_id = (
            msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
        )
        logger.info("Processing system message from {}", msg.sender_id)
        key = msg.session_key_override or f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        if self._restore_runtime_checkpoint(session):
            self.sessions.save(session)
        if self._restore_pending_user_turn(session):
            self.sessions.save(session)

        session, pending = self.auto_compact.prepare_session(session, key)
        if pending:
            logger.info("Memory compact triggered for session {}", key)

        await self.consolidator.maybe_consolidate_by_tokens(
            session,
            replay_max_messages=self._max_messages,
        )
        is_subagent = msg.sender_id == "subagent"
        if is_subagent and self._persist_subagent_followup(session, msg):
            logger.debug("Subagent result persisted for session {}", key)
            self.sessions.save(session)
        self._set_tool_context(
            channel, chat_id, msg.metadata.get("message_id"),
            msg.metadata, session_key=key,
        )
        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        history = session.get_history(**_hist_kwargs)
        current_role = "assistant" if is_subagent else "user"
        workspace_scope = self.workspace_scopes.for_message(msg, session.metadata)

        # Always inject the Tools & Skills Index (same content as
        # _build_initial_messages — keeps system prompt stable across turns).
        # 能力归属自己：绑定数字员工时工具索引只列该员工自己的能力；主会话共享全部。
        tool_index = self._build_tool_index(
            skill_names=self.context._employee_capability_allowlist(session.metadata)
        )
        messages = self.context.build_messages(
            history=history,
            current_message="" if is_subagent else msg.content,
            channel=channel,
            chat_id=chat_id,
            current_role=current_role,
            sender_id=msg.sender_id,
            session_summary=pending,
            session_metadata=session.metadata,
            workspace=workspace_scope.project_path,
            runtime_state=self,
            inbound_message=msg,
            skip_runtime_lines=is_subagent,
            session_key=key,
            unified_session=self._unified_session,
            tool_index=tool_index,
        )
        t_wall = time.time()
        system_turn_id = f"{key}:{time.time_ns()}"
        final_content, _, all_msgs, stop_reason, _ = await self._run_agent_loop(
            messages, session=session, channel=channel, chat_id=chat_id,
            message_id=msg.metadata.get("message_id"),
            metadata=msg.metadata,
            session_key=key,
            pending_queue=pending_queue,
            turn_id=system_turn_id,
            inbound_msg=msg,
        )
        wall_done = time.time()
        latency_ms = max(0, int((wall_done - t_wall) * 1000))
        self._save_turn(session, all_msgs, 1 + len(history), turn_latency_ms=latency_ms)
        self._runtime_events().record_turn_latency(key, latency_ms)
        session.enforce_file_cap(
            on_archive=partial(self.context.memory.raw_archive, session_key=key)
        )
        self._clear_runtime_checkpoint(session)
        self.sessions.save(session)
        self._schedule_background(
            self.consolidator.maybe_consolidate_by_tokens(
                session,
                replay_max_messages=self._max_messages,
            )
        )
        content = final_content or "Background task completed."
        outbound_metadata: dict[str, Any] = {}
        if origin_message_id := msg.metadata.get("origin_message_id"):
            outbound_metadata["origin_message_id"] = origin_message_id
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata=outbound_metadata,
        )

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        pending_queue: asyncio.Queue | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        self._refresh_provider_snapshot()

        if msg.channel == "system":
            return await self._process_system_message(
                msg,
                session_key=session_key,
                on_progress=on_progress,
                on_stream=on_stream,
                on_stream_end=on_stream_end,
                pending_queue=pending_queue,
            )

        key = session_key or msg.session_key
        t0 = time.time()
        ctx = TurnContext(
            msg=msg,
            session=None,
            session_key=key,
            state=TurnState.RESTORE,
            turn_id=f"{key}:{time.time_ns()}",
            turn_wall_started_at=t0,
            visible_run_started_at=turn_continuation.internal_continuation_run_started_at(
                msg.metadata,
            ),
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
            pending_queue=pending_queue,
            ephemeral=ephemeral,
            tools=tools,
        )

        while ctx.state is not TurnState.DONE:
            handler_name = f"_state_{ctx.state.name.lower()}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                raise RuntimeError(f"Missing state handler for {ctx.state}")

            t0 = time.perf_counter()
            try:
                event = await handler(ctx)
            except Exception:
                duration = (time.perf_counter() - t0) * 1000
                ctx.trace.append(
                    StateTraceEntry(
                        state=ctx.state,
                        started_at=t0,
                        duration_ms=duration,
                        event="",
                        error="exception",
                    )
                )
                self._runtime_events().publish_trace(
                    msg=ctx.msg,
                    session_key=ctx.session_key,
                    turn_id=ctx.turn_id,
                    phase="turn_state",
                    step=ctx.state.name,
                    status="failed",
                    duration_ms=duration,
                    detail={"error": "exception"},
                )
                raise

            duration = (time.perf_counter() - t0) * 1000
            ctx.trace.append(
                StateTraceEntry(
                    state=ctx.state,
                    started_at=t0,
                    duration_ms=duration,
                    event=event,
                )
            )
            logger.debug(
                "[turn {}] State {} took {:.1f}ms -> event {}",
                ctx.turn_id,
                ctx.state.name,
                duration,
                event,
            )
            self._runtime_events().publish_trace(
                msg=ctx.msg,
                session_key=ctx.session_key,
                turn_id=ctx.turn_id,
                phase="turn_state",
                step=ctx.state.name,
                status="completed",
                duration_ms=duration,
                detail={"event": event},
            )

            next_state = self._TRANSITIONS.get((ctx.state, event))
            if next_state is None:
                raise RuntimeError(
                    f"[turn {ctx.turn_id}] No transition from {ctx.state} "
                    f"on event {event!r}"
                )
            ctx.state = next_state

        logger.debug(
            "[turn {}] Turn completed after {} states",
            ctx.turn_id,
            len(ctx.trace),
        )
        return ctx.outbound

    def _assemble_outbound(
        self,
        msg: InboundMessage,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        stop_reason: str,
        had_injections: bool,
        on_stream: Callable[[str], Awaitable[None]] | None,
        *,
        turn_latency_ms: int | None = None,
    ) -> OutboundMessage | None:
        """Assemble the final outbound message from turn results."""
        # MessageTool suppression
        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            if not had_injections or stop_reason == "empty_final_response":
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        meta = dict(msg.metadata or {})
        if on_stream is not None and stop_reason not in {"error", "tool_error"}:
            meta["_streamed"] = True
        if turn_latency_ms is not None:
            meta["latency_ms"] = int(turn_latency_ms)

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    async def _state_restore(self, ctx: TurnContext) -> str:
        """恢复检查点/待处理用户轮次，并抽取文档附件。

        处理媒体附件（按需抽取文档文本或引用非图片附件），获取会话对象，
        恢复运行时检查点与未完成的用户轮次，保证崩溃后上下文不丢失。
        """
        msg = ctx.msg

        if msg.media:
            new_content, image_only = self._prepare_message_media(msg.content, msg.media)
            ctx.msg = dataclasses.replace(msg, content=new_content, media=image_only)
            msg = ctx.msg

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        # Session is already fetched by the caller (_process_message) but
        # ensure it exists in case this handler is invoked independently.
        if ctx.session is None:
            ctx.session = self.sessions.get_or_create(ctx.session_key)
        await self._runtime_events().session_turn_started(msg, ctx.session_key)
        self.workspace_scopes.persist_message_scope(ctx.session, msg)

        if self._restore_runtime_checkpoint(ctx.session):
            self.sessions.save(ctx.session)
        if self._restore_pending_user_turn(ctx.session):
            self.sessions.save(ctx.session)

        return "ok"

    def _prepare_message_media(self, content: str, media: list[str]) -> tuple[str, list[str]]:
        if self._should_extract_document_text():
            return extract_documents(content, media)
        return reference_non_image_attachments(content, media)

    def _should_extract_document_text(self) -> bool:
        if self.channels_config is None:
            return True
        return self.channels_config.extract_document_text

    async def _state_compact(self, ctx: TurnContext) -> str:
        """执行自动压缩准备：获取会话与压缩摘要。

        调用 ``AutoCompact.prepare_session``，若会话曾被打包归档，
        则返回压缩摘要供后续 BUILD 阶段注入。
        """
        assert ctx.session is not None  # set by _state_restore
        ctx.session, pending = self.auto_compact.prepare_session(ctx.session, ctx.session_key)
        ctx.pending_summary = pending
        return "ok"

    async def _state_command(self, ctx: TurnContext) -> str:
        """命令分发：若匹配命令则处理并返回 shortcut，否则返回 dispatch 进入 BUILD。

        快捷命令会跳过 BUILD 与 SAVE，因此在此处直接持久化轮次，
        以便 WebUI 历史水合能看到消息；标记 ``_command`` 便于 get_history
        将其从 LLM 上下文中过滤。``/new`` 例外（它会清空会话）。
        """
        assert ctx.session is not None  # set by _state_restore
        raw = ctx.msg.content.strip()
        cmd_ctx = CommandContext(
            msg=ctx.msg, session=ctx.session, key=ctx.session_key, raw=raw, loop=self
        )
        result = await self.commands.dispatch(cmd_ctx)
        if result is not None:
            ctx.outbound = result
            # Shortcut commands skip BUILD and SAVE, so we must persist the
            # turn here so WebUI history hydration after _turn_end sees the
            # message.  Mark messages with _command so get_history can filter
            # them out of LLM context.  /new is excluded because it
            # intentionally clears the session.
            if raw.lower() != "/new":
                ctx.user_persisted_early = self._persist_user_message_early(
                    ctx.msg, ctx.session, _command=True
                )
                ctx.session.add_message(
                    "assistant", result.content, _command=True
                )
                self.sessions.save(ctx.session)
                self._clear_pending_user_turn(ctx.session)
            return "shortcut"
        return "dispatch"

    async def _state_build(self, ctx: TurnContext) -> str:
        """构建上下文：合并记忆、设置工具上下文、回放历史、构建初始消息。

        非临时轮次会先尝试按 token 阈值合并；随后设置工具上下文、重置
        MessageTool 轮次状态、回放历史并构建初始消息列表；
        若用户消息尚未持久化则提前持久化；最后准备进度与重试等待回调。
        """
        assert ctx.session is not None  # set by _state_restore
        if not ctx.ephemeral:
            await self.consolidator.maybe_consolidate_by_tokens(
                ctx.session,
                replay_max_messages=self._max_messages,
            )
        self._set_tool_context(
            ctx.msg.channel,
            ctx.msg.chat_id,
            ctx.msg.metadata.get("message_id"),
            ctx.msg.metadata,
            session_key=ctx.session_key,
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        _hist_kwargs: dict[str, Any] = {
            "max_messages": self._max_messages,
            "max_tokens": self._replay_token_budget(),
            "include_timestamps": True,
        }
        ctx.history = ctx.session.get_history(**_hist_kwargs)
        self._runtime_events().record_turn_runtime(
            ctx.session_key,
            self.llm_runtime(),
        )

        ctx.initial_messages = self._build_initial_messages(
            ctx.msg,
            ctx.session,
            ctx.history,
            ctx.pending_summary,
            include_memory_recent_history=not ctx.ephemeral,
        )
        ctx.user_persisted_early = self._persist_user_message_early(
            ctx.msg, ctx.session
        )

        if ctx.on_progress is None:
            ctx.on_progress = await self._build_bus_progress_callback(ctx.msg)
        if ctx.on_retry_wait is None:
            ctx.on_retry_wait = await self._build_retry_wait_callback(ctx.msg)

        return "ok"

    async def _state_run(self, ctx: TurnContext) -> str:
        """运行 Agent 迭代循环并收集结果。

        发布 running 状态，调用 ``_run_agent_loop`` 执行 LLM 迭代与工具调用，
        将结果（正文、工具名、消息、停止原因、是否注入）写回 ctx，
        并交由 ``turn_continuation`` 判断是否需要延续轮次。
        """
        if ctx.visible_run_started_at is None:
            ctx.visible_run_started_at = time.time()
        await self._runtime_events().run_status_changed(
            ctx.msg,
            ctx.session_key,
            "running",
            started_at=ctx.visible_run_started_at,
        )
        result = await self._run_agent_loop(
            ctx.initial_messages,
            on_progress=ctx.on_progress,
            on_stream=ctx.on_stream,
            on_stream_end=ctx.on_stream_end,
            on_retry_wait=ctx.on_retry_wait,
            session=ctx.session,
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.metadata.get("message_id"),
            metadata=ctx.msg.metadata,
            session_key=ctx.session_key,
            pending_queue=ctx.pending_queue,
            ephemeral=ctx.ephemeral,
            tools=ctx.tools,
            turn_id=ctx.turn_id,
            inbound_msg=ctx.msg,
        )
        final_content, tools_used, all_msgs, stop_reason, had_injections = result
        ctx.final_content = final_content
        ctx.tools_used = tools_used
        ctx.all_messages = all_msgs
        ctx.stop_reason = stop_reason
        ctx.had_injections = had_injections
        await turn_continuation.maybe_continue_turn(ctx)
        return "ok"

    async def _state_save(self, ctx: TurnContext) -> str:
        """保存轮次结果到会话：处理空响应、计算延迟、持久化与后台合并。

        准备保存边界；若正文为空且未抑制响应则填入默认空响应消息；
        计算轮次延迟并调用 ``_save_turn`` 持久化；非临时轮次会触发文件
        容量限制与后台 token 合并；最后清理检查点并保存会话。
        """
        assert ctx.session is not None  # set by _state_restore
        turn_continuation.prepare_save_boundary(ctx)

        if (
            (ctx.final_content is None or not ctx.final_content.strip())
            and not ctx.suppress_response
        ):
            ctx.final_content = EMPTY_FINAL_RESPONSE_MESSAGE

        latency_started_at = (
            ctx.visible_run_started_at
            if turn_continuation.internal_continuation_inbound(ctx.msg.metadata)
            and ctx.visible_run_started_at is not None
            else ctx.turn_wall_started_at
        )
        ctx.turn_latency_ms = max(0, int((time.time() - latency_started_at) * 1000))
        self._save_turn(
            ctx.session, ctx.all_messages, ctx.save_skip,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        self._runtime_events().record_turn_latency(
            ctx.session_key,
            ctx.turn_latency_ms,
        )
        if not ctx.ephemeral:
            ctx.session.enforce_file_cap(
                on_archive=partial(self.context.memory.raw_archive, session_key=ctx.session_key)
            )
            self._schedule_background(
                self.consolidator.maybe_consolidate_by_tokens(
                    ctx.session,
                    replay_max_messages=self._max_messages,
                )
            )
        self._clear_pending_user_turn(ctx.session)
        self._clear_runtime_checkpoint(ctx.session)
        self.sessions.save(ctx.session)
        return "ok"

    async def _state_respond(self, ctx: TurnContext) -> str:
        """装配出站响应：若抑制响应则返回 None，否则组装 OutboundMessage。

        临时轮次会在出站元数据中附带 ``_stop_reason`` 供调用方判断。
        """
        if ctx.suppress_response:
            ctx.outbound = None
            return "ok"
        # _state_save guarantees final_content is non-None when suppress_response is False
        final_content = ctx.final_content or ""
        ctx.outbound = self._assemble_outbound(
            ctx.msg,
            final_content,
            ctx.all_messages,
            ctx.stop_reason,
            ctx.had_injections,
            ctx.on_stream,
            turn_latency_ms=ctx.turn_latency_ms,
        )
        if ctx.ephemeral and ctx.outbound is not None:
            ctx.outbound.metadata["_stop_reason"] = ctx.stop_reason
        return "ok"

    def _sanitize_persisted_blocks(
        self,
        content: list[dict[str, Any]],
        *,
        should_truncate_text: bool = False,
        drop_runtime: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip volatile multimodal payloads before writing session history."""
        filtered: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                filtered.append(block)
                continue

            if (
                drop_runtime
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
            ):
                continue

            if block.get("type") == "image_url" and block.get("image_url", {}).get(
                "url", ""
            ).startswith("data:image/"):
                path = (block.get("_meta") or {}).get("path", "")
                filtered.append({"type": "text", "text": image_placeholder_text(path)})
                continue

            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if should_truncate_text and len(text) > self.max_tool_result_chars:
                    text = truncate_text_fn(text, self.max_tool_result_chars)
                filtered.append({**block, "text": text})
                continue

            filtered.append(block)

        return filtered

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        turn_latency_ms: int | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        declared_tool_call_ids = {
            str(tc["id"])
            for m in session.messages
            if m.get("role") == "assistant"
            for tc in m.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("id")
        }
        last_assistant_idx: int | None = None
        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool":
                tool_call_id = entry.get("tool_call_id")
                if not tool_call_id or str(tool_call_id) not in declared_tool_call_ids:
                    # Undeclared tool results corrupt future provider requests.
                    logger.warning(
                        "Dropping orphaned tool result {} from session {} during persistence",
                        tool_call_id or "(missing id)",
                        session.key,
                    )
                    continue
                if isinstance(content, str) and len(content) > self.max_tool_result_chars:
                    entry["content"] = truncate_text_fn(content, self.max_tool_result_chars)
                elif isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, should_truncate_text=True)
                    if not filtered:
                        # Preserve the tool_call/result pair after block filtering.
                        filtered = [
                            {"type": "text", "text": "[tool result omitted during persistence]"}
                        ]
                    entry["content"] = filtered
            elif role == "user":
                if isinstance(content, str) and ContextBuilder._RUNTIME_CONTEXT_TAG in content:
                    # Strip the runtime-context block appended at the end.
                    tag_pos = content.find(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    before = content[:tag_pos].rstrip("\n ")
                    if before:
                        entry["content"] = before
                    else:
                        continue
                if isinstance(content, list):
                    filtered = self._sanitize_persisted_blocks(content, drop_runtime=True)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            if role == "assistant":
                last_assistant_idx = len(session.messages) - 1
                declared_tool_call_ids.update(
                    str(tc["id"])
                    for tc in entry.get("tool_calls") or []
                    if isinstance(tc, dict) and tc.get("id")
                )
        if turn_latency_ms is not None and last_assistant_idx is not None:
            session.messages[last_assistant_idx]["latency_ms"] = int(turn_latency_ms)
        session.updated_at = datetime.now()

    def _persist_subagent_followup(self, session: Session, msg: InboundMessage) -> bool:
        """Persist subagent follow-ups before prompt assembly so history stays durable.

        Returns True if a new entry was appended; False if the follow-up was
        deduped (same ``subagent_task_id`` already in session) or carries no
        content worth persisting.
        """
        if not msg.content:
            return False
        task_id = msg.metadata.get("subagent_task_id") if isinstance(msg.metadata, dict) else None
        if task_id and any(
            m.get("injected_event") == "subagent_result" and m.get("subagent_task_id") == task_id
            for m in session.messages
        ):
            return False
        session.add_message(
            "assistant",
            msg.content,
            sender_id=msg.sender_id,
            injected_event="subagent_result",
            subagent_task_id=task_id,
        )
        return True

    def _set_runtime_checkpoint(self, session: Session, payload: dict[str, Any]) -> None:
        """Persist the latest in-flight turn state into session metadata."""
        session.metadata[self._RUNTIME_CHECKPOINT_KEY] = payload
        self.sessions.save(session)

    def _mark_pending_user_turn(self, session: Session) -> None:
        session.metadata[self._PENDING_USER_TURN_KEY] = True

    def _clear_pending_user_turn(self, session: Session) -> None:
        session.metadata.pop(self._PENDING_USER_TURN_KEY, None)

    def _clear_runtime_checkpoint(self, session: Session) -> None:
        if self._RUNTIME_CHECKPOINT_KEY in session.metadata:
            session.metadata.pop(self._RUNTIME_CHECKPOINT_KEY, None)

    @staticmethod
    def _checkpoint_message_key(message: dict[str, Any]) -> tuple[Any, ...]:
        return (
            message.get("role"),
            message.get("content"),
            message.get("tool_call_id"),
            message.get("name"),
            message.get("tool_calls"),
            message.get("reasoning_content"),
            message.get("thinking_blocks"),
        )

    def _restore_runtime_checkpoint(self, session: Session) -> bool:
        """Materialize an unfinished turn into session history before a new request."""
        from datetime import datetime

        checkpoint = session.metadata.get(self._RUNTIME_CHECKPOINT_KEY)
        if not isinstance(checkpoint, dict):
            return False

        assistant_message = checkpoint.get("assistant_message")
        completed_tool_results = checkpoint.get("completed_tool_results") or []
        pending_tool_calls = checkpoint.get("pending_tool_calls") or []

        restored_messages: list[dict[str, Any]] = []
        if isinstance(assistant_message, dict):
            restored = dict(assistant_message)
            restored.setdefault("timestamp", datetime.now().isoformat())
            restored_messages.append(restored)
        for message in completed_tool_results:
            if isinstance(message, dict):
                restored = dict(message)
                restored.setdefault("timestamp", datetime.now().isoformat())
                restored_messages.append(restored)
        for tool_call in pending_tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            name = ((tool_call.get("function") or {}).get("name")) or "tool"
            restored_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": "Error: Task interrupted before this tool finished.",
                    "timestamp": datetime.now().isoformat(),
                }
            )

        overlap = 0
        max_overlap = min(len(session.messages), len(restored_messages))
        for size in range(max_overlap, 0, -1):
            existing = session.messages[-size:]
            restored = restored_messages[:size]
            if all(
                self._checkpoint_message_key(left) == self._checkpoint_message_key(right)
                for left, right in zip(existing, restored)
            ):
                overlap = size
                break
        session.messages.extend(restored_messages[overlap:])

        self._clear_pending_user_turn(session)
        self._clear_runtime_checkpoint(session)
        return True

    def _restore_pending_user_turn(self, session: Session) -> bool:
        """Close a turn that only persisted the user message before crashing."""
        from datetime import datetime

        if not session.metadata.get(self._PENDING_USER_TURN_KEY):
            return False

        if session.messages and session.messages[-1].get("role") == "user":
            session.messages.append(
                {
                    "role": "assistant",
                    "content": "Error: Task interrupted before a response was generated.",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            session.updated_at = datetime.now()

        self._clear_pending_user_turn(session)
        return True

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        media: list[str] | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        ephemeral: bool = False,
        tools: ToolRegistry | None = None,
        persist_user_message: bool = True,
    ) -> OutboundMessage | None:
        """Process a message directly and return the outbound payload."""
        await self._connect_mcp()
        metadata: dict[str, Any] = {}
        if not persist_user_message:
            metadata[turn_continuation.SKIP_USER_PERSIST_META] = True
        msg = InboundMessage(
            channel=channel, sender_id="user", chat_id=chat_id,
            content=content, media=media or [], metadata=metadata,
        )
        # Share the dispatch lock so direct calls serialize with bus turns.
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        try:
            async with lock:
                kwargs: dict[str, Any] = {
                    "session_key": session_key,
                    "on_progress": on_progress,
                    "on_stream": on_stream,
                    "on_stream_end": on_stream_end,
                    "ephemeral": ephemeral,
                }
                if tools is not None:
                    kwargs["tools"] = tools
                return await self._process_message(
                    msg,
                    **kwargs,
                )
        finally:
            await self._runtime_events().run_status_changed(msg, session_key, "idle")
            self._runtime_events().clear_turn(session_key)
