"""WebUI 兼容 WebSocket 会话的 turn 辅助工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/session 目录（虽名称含 webui，但属于 session 模块）。
在项目架构中起到的作用：为具备 WebUI 能力的 WebSocket 会话提供 turn 级辅助，
包括会话标题生成、运行状态推送、turn 结束通知、目标状态同步与全链路追踪事件转发，
将通用运行时事件转换为 WebSocket 线上消息。
"""

from __future__ import annotations

import re  # 标题清洗正则
import time  # 墙钟时间记录
from collections.abc import Awaitable, Callable  # 可调用与可等待类型标注
from dataclasses import dataclass, field  # 协调器数据类定义
from typing import Any  # 任意类型标注

from loguru import logger  # 日志输出

from biscuitbot.bus import progress as bus_progress  # 通用进度回调构造
from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 消息数据类
from biscuitbot.bus.queue import MessageBus  # 异步消息总线
from biscuitbot.bus.runtime_events import (  # 运行时事件类型与总线
    AgentTraceEvent,
    GoalStateChanged,
    RuntimeEventBus,
    RuntimeEventContext,
    RuntimeModelChanged,
    SessionTurnStarted,
    TurnCompleted,
    TurnRunStatusChanged,
)
from biscuitbot.cron.session_turns import CRON_HISTORY_META  # cron 历史消息元数据键
from biscuitbot.providers.base import LLMProvider  # LLM 提供商基类
from biscuitbot.session.goal_state import goal_state_ws_blob  # 目标状态 WebSocket 快照
from biscuitbot.session.manager import Session, SessionManager  # 会话与管理器
from biscuitbot.utils.helpers import strip_think, truncate_text  # 文本处理辅助
from biscuitbot.utils.llm_runtime import LLMRuntime  # LLM 运行时封装

WEBUI_SESSION_METADATA_KEY = "webui"  # WebUI 会话标记键
WEBUI_TITLE_METADATA_KEY = "title"  # 会话标题元数据键
WEBUI_TITLE_USER_EDITED_METADATA_KEY = "title_user_edited"  # 用户已编辑标题标记键
TITLE_MAX_CHARS = 60  # 标题最大字符数
TITLE_GENERATION_MAX_TOKENS = 96  # 标题生成最大 token 数
TITLE_GENERATION_REASONING_EFFORT = "none"  # 标题生成推理力度

# 每个 ``chat_id`` 的墙钟 turn 起始时间（仅 websocket）。在网关进程存活期间
# 跨浏览器刷新保留；在空闲/停止时清除，重启时隐式丢弃。
_WEBSOCKET_TURN_WALL_STARTED_AT: dict[str, float] = {}


def mark_webui_session(session: Session, metadata: dict[str, Any]) -> bool:
    """仅当入站 websocket 帧选择启用时持久化 WebUI 标记。"""
    if metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    session.metadata[WEBUI_SESSION_METADATA_KEY] = True
    return True


def clean_generated_title(raw: str | None) -> str:
    """清洗生成的标题：去除前缀、引号、think 标签与尾部标点，并截断超长。"""
    text = (raw or "").strip()
    if not text:
        return ""
    # 去除可能的 "title:" / "标题：" 前缀
    text = re.sub(r"^\s*(title|标题)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip().strip("\"'`“”‘’")
    text = strip_think(text)
    text = re.sub(r"\s+", " ", text).strip()
    # 去除尾部标点
    text = text.rstrip("。.!！?？,，;；:")
    if len(text) > TITLE_MAX_CHARS:
        text = text[: TITLE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _title_inputs(session: Session) -> tuple[str, str]:
    """从会话中提取首条用户文本与首条助手文本，用于生成标题。"""
    user_text = ""
    assistant_text = ""
    for message in session.messages:
        # 跳过命令消息与 cron 历史消息
        if message.get("_command") is True:
            continue
        if message.get(CRON_HISTORY_META) is True:
            continue
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        content = strip_think(content)
        if not content:
            continue
        if role == "user" and not user_text:
            user_text = content.strip()
        elif role == "assistant" and not assistant_text:
            assistant_text = content.strip()
        # 同时获取到用户与助手文本即可提前退出
        if user_text and assistant_text:
            break
    return user_text, assistant_text


async def maybe_generate_webui_title(
    *,
    sessions: SessionManager,
    session_key: str,
    provider: LLMProvider,
    model: str,
) -> bool:
    """仅为 WebUI 拥有的会话生成并持久化简短标题。"""
    session = sessions.get_or_create(session_key)
    # 非 WebUI 会话不生成标题
    if session.metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    # 用户已编辑标题则跳过
    if session.metadata.get(WEBUI_TITLE_USER_EDITED_METADATA_KEY) is True:
        return False
    current_title = session.metadata.get(WEBUI_TITLE_METADATA_KEY)
    if isinstance(current_title, str) and current_title.strip():
        cleaned_current_title = clean_generated_title(current_title)
        if cleaned_current_title:
            # 标题需清洗时更新并保存
            if cleaned_current_title != current_title:
                session.metadata[WEBUI_TITLE_METADATA_KEY] = cleaned_current_title
                sessions.save(session)
            return False
        # 现有标题清洗后为空则移除
        session.metadata.pop(WEBUI_TITLE_METADATA_KEY, None)

    user_text, assistant_text = _title_inputs(session)
    if not user_text:
        return False

    prompt = (
        "Generate a concise title for this chat.\n"
        "Rules:\n"
        "- Use the same language as the user when practical.\n"
        "- 3 to 8 words.\n"
        "- No quotes.\n"
        "- No punctuation at the end.\n"
        "- Return only the title.\n\n"
        f"User: {truncate_text(user_text, 1_000)}"
    )
    if assistant_text:
        prompt += f"\nAssistant: {truncate_text(assistant_text, 1_000)}"

    try:
        response = await provider.chat_with_retry(
            [
                {
                    "role": "system",
                    "content": (
                        "You write short, neutral chat titles. "
                        "Return only the title text."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            tools=None,
            model=model,
            max_tokens=TITLE_GENERATION_MAX_TOKENS,
            temperature=0.2,
            reasoning_effort=TITLE_GENERATION_REASONING_EFFORT,
            retry_mode="standard",
        )
    except Exception:
        logger.debug("Failed to generate webui session title for {}", session_key, exc_info=True)
        return False

    title = clean_generated_title(response.content)
    # 过滤无效标题
    if not title or title.lower().startswith("error"):
        logger.debug(
            "WebUI title generation returned no usable title for {} (finish_reason={})",
            session_key,
            response.finish_reason,
        )
        return False
    session.metadata[WEBUI_TITLE_METADATA_KEY] = title
    sessions.save(session)
    return True


async def maybe_generate_webui_title_after_turn(
    *,
    channel: str,
    metadata: dict[str, Any],
    sessions: SessionManager,
    session_key: str,
    provider: LLMProvider,
    model: str,
) -> bool:
    """turn 结束后按需生成 WebUI 标题（仅 websocket 渠道且启用 webui 标记）。"""
    if channel != "websocket" or metadata.get(WEBUI_SESSION_METADATA_KEY) is not True:
        return False
    return await maybe_generate_webui_title(
        sessions=sessions,
        session_key=session_key,
        provider=provider,
        model=model,
    )


def websocket_turn_wall_started_at(chat_id: str) -> float | None:
    """返回活跃用户 turn 开始的 ``time.time()``（若仍在运行）。"""
    return _WEBSOCKET_TURN_WALL_STARTED_AT.get(chat_id)


def build_bus_progress_callback(
    bus: MessageBus,
    msg: InboundMessage,
) -> Callable[..., Awaitable[None]]:
    """通用总线进度回调的兼容包装。"""
    return bus_progress.build_bus_progress_callback(bus, msg)


async def publish_turn_run_status(
    bus: MessageBus,
    msg: InboundMessage,
    status: str,
    *,
    started_at: float | None = None,
) -> None:
    """在用户 turn 执行期间通知 WebSocket 客户端（含计时信息）。"""
    if msg.channel != "websocket":
        return
    cid = str(msg.chat_id)
    meta: dict[str, Any] = {
        **dict(msg.metadata or {}),
        "_goal_status": True,
        "goal_status": status,
    }
    if status == "running":
        # running 状态记录起始时间
        if isinstance(started_at, int | float) and started_at > 0:
            t0 = float(started_at)
        else:
            t0 = time.time()
        meta["started_at"] = t0
        _WEBSOCKET_TURN_WALL_STARTED_AT[cid] = t0
    else:
        # 非 running 状态清除起始时间
        _WEBSOCKET_TURN_WALL_STARTED_AT.pop(cid, None)
    await bus.publish_outbound(
        OutboundMessage(
            channel=msg.channel,
            chat_id=cid,
            content="",
            metadata=meta,
        ),
    )

@dataclass
class WebuiTurnCoordinator:
    """将通用运行时事件转换为 WebUI/WebSocket 线上消息。

    订阅 RuntimeEventBus 的各类事件，按渠道过滤后转换为出站消息推送到总线，
    并在 turn 结束时调度后台标题生成任务。
    """

    bus: MessageBus
    sessions: SessionManager
    schedule_background: Callable[[Awaitable[None]], None]  # 后台任务调度器
    _title_contexts: dict[str, LLMRuntime] = field(default_factory=dict)  # 待生成标题的会话上下文

    def subscribe(self, runtime_events: RuntimeEventBus) -> Callable[[], None]:
        """将本协调器订阅到运行时事件总线。"""
        unsubscribe = [
            runtime_events.subscribe(
                self._handle_session_turn_started,
                SessionTurnStarted,
            ),
            runtime_events.subscribe(
                self._handle_run_status_changed,
                TurnRunStatusChanged,
            ),
            runtime_events.subscribe(
                self._handle_turn_completed_event,
                TurnCompleted,
            ),
            runtime_events.subscribe(
                self._handle_goal_state_changed,
                GoalStateChanged,
            ),
            runtime_events.subscribe(
                self._handle_runtime_model_changed,
                RuntimeModelChanged,
            ),
            runtime_events.subscribe(
                self._handle_agent_trace,
                AgentTraceEvent,
            ),
        ]

        def _unsubscribe() -> None:
            """按订阅的逆序取消所有订阅。"""
            for fn in reversed(unsubscribe):
                fn()

        return _unsubscribe

    @staticmethod
    def _ctx_msg(ctx: RuntimeEventContext) -> InboundMessage:
        """从运行时事件上下文构造等价的入站消息。"""
        return InboundMessage(
            channel=ctx.channel,
            sender_id="runtime",
            chat_id=ctx.chat_id,
            content="",
            metadata=dict(ctx.metadata or {}),
            session_key_override=ctx.session_key,
        )

    @staticmethod
    def _is_websocket_event(ctx: RuntimeEventContext) -> bool:
        """判断事件是否来自 websocket 渠道。"""
        return ctx.channel == "websocket"

    def _handle_session_turn_started(self, event: SessionTurnStarted) -> None:
        """处理 turn 启动事件：为 websocket 会话标记 WebUI。"""
        if not self._is_websocket_event(event.context):
            return
        session = self.sessions.get_or_create(event.context.session_key)
        mark_webui_session(session, event.context.metadata)

    async def _handle_run_status_changed(self, event: TurnRunStatusChanged) -> None:
        """处理运行状态变更事件：推送状态到 websocket 客户端。"""
        if not self._is_websocket_event(event.context):
            return
        await publish_turn_run_status(
            self.bus,
            self._ctx_msg(event.context),
            event.status,
            started_at=event.started_at,
        )

    async def _handle_turn_completed_event(self, event: TurnCompleted) -> None:
        """处理 turn 完成事件：发送结束通知并调度标题生成。"""
        if not self._is_websocket_event(event.context):
            return
        msg = self._ctx_msg(event.context)
        await self.handle_turn_end(
            msg,
            session_key=event.context.session_key,
            latency_ms=event.latency_ms,
        )
        self._schedule_title_update_from_event(event)

    async def _handle_goal_state_changed(self, event: GoalStateChanged) -> None:
        """处理目标状态变更事件：同步目标状态快照到 websocket。"""
        if not self._is_websocket_event(event.context):
            return
        cid = str(event.context.chat_id or "").strip()
        if not cid:
            return
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=event.context.channel,
                chat_id=cid,
                content="",
                metadata={
                    "_goal_state_sync": True,
                    "goal_state": goal_state_ws_blob(event.session_metadata),
                },
            ),
        )

    async def _handle_runtime_model_changed(self, event: RuntimeModelChanged) -> None:
        """处理运行时模型变更事件：广播到所有 websocket 客户端。"""
        await self.bus.publish_outbound(
            OutboundMessage(
                channel="websocket",
                chat_id="*",
                content="",
                metadata={
                    "_runtime_model_updated": True,
                    "model": event.model,
                    "model_preset": event.model_preset,
                },
            )
        )

    async def _handle_agent_trace(self, event: AgentTraceEvent) -> None:
        """推送全链路追踪事件到 WebSocket 前端。"""
        if not self._is_websocket_event(event.context):
            return
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=event.context.channel,
                chat_id=event.context.chat_id,
                content="",
                metadata={
                    "_agent_trace": True,
                    "turn_id": event.turn_id,
                    "phase": event.phase,
                    "step": event.step,
                    "status": event.status,
                    "duration_ms": event.duration_ms,
                    "detail": event.detail,
                },
            )
        )

    def capture_title_context(
        self,
        session_key: str,
        msg: InboundMessage,
        llm: LLMRuntime,
    ) -> None:
        """为 WebUI 会话捕获标题生成所需的 LLM 上下文。"""
        if msg.channel == "websocket" and msg.metadata.get("webui") is True:
            self._title_contexts[session_key] = llm

    def discard(self, session_key: str) -> None:
        """丢弃指定会话的标题生成上下文。"""
        self._title_contexts.pop(session_key, None)

    async def publish_run_status(
        self,
        msg: InboundMessage,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """发布运行状态到 websocket。"""
        await publish_turn_run_status(self.bus, msg, status, started_at=started_at)

    async def handle_turn_end(
        self,
        msg: InboundMessage,
        *,
        session_key: str,
        latency_ms: int | None,
    ) -> None:
        """处理 turn 结束：发送结束通知并调度标题更新。"""
        if msg.channel != "websocket":
            return

        turn_metadata: dict[str, Any] = {**msg.metadata, "_turn_end": True}
        if latency_ms is not None:
            turn_metadata["latency_ms"] = int(latency_ms)
        session = self.sessions.get_or_create(session_key)
        # 附带目标状态快照
        turn_metadata["goal_state"] = goal_state_ws_blob(session.metadata)
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            metadata=turn_metadata,
        ))
        self._schedule_title_update(msg, session_key=session_key)

    def _schedule_title_update(self, msg: InboundMessage, *, session_key: str) -> None:
        """调度后台标题生成任务（基于入站消息上下文）。"""
        title_context = self._title_contexts.pop(session_key, None)
        if msg.metadata.get("webui") is not True or title_context is None:
            return

        async def _generate_title_and_notify(
            title_llm: LLMRuntime = title_context,
        ) -> None:
            """生成标题并通知前端会话元数据已更新。"""
            generated = await maybe_generate_webui_title_after_turn(
                channel=msg.channel,
                metadata=msg.metadata,
                sessions=self.sessions,
                session_key=session_key,
                provider=title_llm.provider,
                model=title_llm.model,
            )
            if generated:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="",
                    metadata={
                        **msg.metadata,
                        "_session_updated": True,
                        "_session_update_scope": "metadata",
                    },
                ))

        self.schedule_background(_generate_title_and_notify())

    def _schedule_title_update_from_event(self, event: TurnCompleted) -> None:
        """调度后台标题生成任务（基于 turn 完成事件）。"""
        title_context = event.runtime
        if (
            event.context.metadata.get("webui") is not True
            or title_context is None
            or not isinstance(title_context, LLMRuntime)
        ):
            return

        async def _generate_title_and_notify(
            title_llm: LLMRuntime = title_context,
        ) -> None:
            """生成标题并通知前端会话元数据已更新。"""
            generated = await maybe_generate_webui_title_after_turn(
                channel=event.context.channel,
                metadata=event.context.metadata,
                sessions=self.sessions,
                session_key=event.context.session_key,
                provider=title_llm.provider,
                model=title_llm.model,
            )
            if generated:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=event.context.channel,
                    chat_id=event.context.chat_id,
                    content="",
                    metadata={
                        **event.context.metadata,
                        "_session_updated": True,
                        "_session_update_scope": "metadata",
                    },
                ))

        self.schedule_background(_generate_title_and_notify())
