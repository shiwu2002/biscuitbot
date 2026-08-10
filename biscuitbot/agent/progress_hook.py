"""将 Runner 事件适配为渠道进度 UI 信号的 Agent 钩子。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责“进度/流式 UI 适配”的组件。
在项目架构中起到的作用：
- ``AgentProgressHook`` 继承自 ``AgentHook``，把 Runner 在迭代过程中产生的
  生命周期事件（流式增量、推理内容、工具开始/结束、迭代开始等）翻译为
  面向渠道（CLI、Slack、飞书等）的进度回调信号；
- 处理 think 标签的剥离与增量提取，确保只有“答案正文”进入流式回调，
  而推理内容通过独立的 ``reasoning`` 通道输出；
- 在工具执行前更新工具上下文（渠道/聊天/会话），保证工具能正确路由回执。
"""

from __future__ import annotations

import inspect  # 用于检查进度回调的签名，判断是否接受特定参数
import json  # 工具调用参数的序列化（日志用）
from typing import Any, Awaitable, Callable  # 类型注解支持

from loguru import logger  # 日志记录

from biscuitbot.agent.hook import AgentHook, AgentHookContext  # 钩子基类与迭代上下文
from biscuitbot.utils.helpers import IncrementalThinkExtractor, strip_think  # think 增量提取与剥离
from biscuitbot.utils.progress_events import (  # 进度事件构建与调用辅助
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    invoke_on_progress,
    on_progress_accepts_tool_events,
)
from biscuitbot.utils.tool_hints import format_tool_hints  # 工具提示格式化


class AgentProgressHook(AgentHook):
    """将 Runner 生命周期事件翻译为用户可见的进度信号。

    职责与项目角色：
    - 桥接 Runner 与渠道层：把流式正文、推理内容、工具事件等以统一回调形式输出；
    - 维护流式缓冲与 think 提取器，分离“推理”与“答案”两条输出通道；
    - 在工具执行前刷新工具上下文，确保回执路由正确。

    典型用法：由 ``AgentLoop._run_agent_loop`` 构造并作为 Runner 的钩子传入。
    """

    def __init__(
        self,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        tool_hint_max_length: int = 40,
        set_tool_context: Callable[..., None] | None = None,
        on_iteration: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(reraise=True)  # 进度钩子异常向上抛出，避免静默丢失
        self._on_progress = on_progress  # 进度回调（工具事件、推理、思考等）
        self._on_stream = on_stream  # 流式正文增量回调
        self._on_stream_end = on_stream_end  # 流式结束回调
        self._channel = channel  # 渠道标识
        self._chat_id = chat_id  # 聊天 ID
        self._message_id = message_id  # 消息 ID
        self._metadata = metadata or {}  # 消息元数据
        self._session_key = session_key  # 会话 key
        self._tool_hint_max_length = tool_hint_max_length  # 工具提示最大长度
        self._set_tool_context = set_tool_context  # 工具上下文刷新回调
        self._on_iteration = on_iteration  # 迭代序号回调
        self._stream_buf = ""  # 流式正文缓冲（含 think 标签）
        self._think_extractor = IncrementalThinkExtractor()  # think 增量提取器
        self._reasoning_open = False  # 推理通道是否处于打开状态

    def wants_streaming(self) -> bool:
        """是否需要流式增量：仅当设置了流式回调时为 True。"""
        return self._on_stream is not None

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """剥离 think 标签，返回纯文本（空则返回 None）。"""
        if not text:
            return None
        return strip_think(text) or None

    def _tool_hint(self, tool_calls: list[Any]) -> str:
        """格式化工具调用为简短提示文本。"""
        return format_tool_hints(tool_calls, max_length=self._tool_hint_max_length)

    @staticmethod
    def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
        """判断进度回调是否接受名为 *name* 的参数（或接受 **kwargs）。"""
        try:
            sig = inspect.signature(cb)
        except (TypeError, ValueError):
            return False
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return True
        return name in sig.parameters

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """处理流式正文增量：剥离 think，分离推理与答案通道。

        参数:
            context: 迭代上下文；
            delta: 本次增量文本。
        """
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]  # 本次新增的纯答案文本

        if await self._think_extractor.feed(self._stream_buf, self.emit_reasoning):
            context.streamed_reasoning = True

        if incremental:
            # 答案文本已开始；关闭推理段，使 UI 能在答案渲染前锁定气泡。
            await self.emit_reasoning_end()
            if self._on_stream:
                await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        """流式输出结束：关闭推理段并重置缓冲。

        参数:
            context: 迭代上下文；
            resuming: 是否即将继续（工具调用后将重启流式）。
        """
        await self.emit_reasoning_end()
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""
        self._think_extractor.reset()

    async def before_iteration(self, context: AgentHookContext) -> None:
        """迭代开始前：回写迭代序号并记录调试日志。"""
        if self._on_iteration:
            self._on_iteration(context.iteration)
        logger.debug(
            "Starting agent loop iteration {} for session {}",
            context.iteration,
            self._session_key,
        )

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """执行工具前：发布思考/工具提示进度，并刷新工具上下文。"""
        if self._on_progress:
            # 非流式且尚未流式输出正文时，先把思考内容作为进度发出
            if not self._on_stream and not context.streamed_content:
                thought = self._strip_think(context.response.content if context.response else None)
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._strip_think(self._tool_hint(context.tool_calls))
            tool_events = [build_tool_event_start_payload(tc) for tc in context.tool_calls]
            await invoke_on_progress(
                self._on_progress,
                tool_hint, # type: ignore
                tool_hint=True,
                tool_events=tool_events,
            )
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        # 刷新工具上下文，确保工具回执能正确路由到对应渠道/会话
        if self._set_tool_context:
            self._set_tool_context(
                self._channel,
                self._chat_id,
                self._message_id,
                self._metadata,
                session_key=self._session_key,
            )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """发布一个推理分片；是否渲染由渠道插件决定。"""
        if (
            self._on_progress
            and reasoning_content
            and self._on_progress_accepts(self._on_progress, "reasoning")
        ):
            self._reasoning_open = True
            await self._on_progress(reasoning_content, reasoning=True)

    async def emit_reasoning_end(self) -> None:
        """关闭当前推理流段（若处于打开状态）。"""
        if self._reasoning_open and self._on_progress:
            self._reasoning_open = False
            await self._on_progress("", reasoning_end=True)
        else:
            self._reasoning_open = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        """迭代结束后：发布工具完成事件并记录 token 用量。"""
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events = build_tool_event_finish_payloads(context)
            if tool_events:
                await invoke_on_progress(
                    self._on_progress,
                    "",
                    tool_hint=False,
                    tool_events=tool_events,
                )
        u = context.usage or {}
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            u.get("prompt_tokens", 0),
            u.get("completion_tokens", 0),
            u.get("cached_tokens", 0),
        )

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """对最终正文做后处理：剥离 think 标签后返回。"""
        return self._strip_think(content)
