"""渠道管理器，负责协调各聊天渠道的生命周期与消息路由。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的核心协调组件。
在项目架构中起到的作用：管理所有已启用的渠道实例，负责渠道的初始化、启动、停止，
并将出站消息从消息总线分发到正确的目标渠道。

平台特点与接入方式
------------------
- 渠道发现：通过 pkgutil 扫描内置渠道模块 + entry_points 加载外部插件。
- 生命周期：统一管理各渠道的启动与停止，支持优雅关闭。
- 消息路由：从消息总线消费出站消息，按渠道名称分发到对应实例。
- 重试机制：出站消息发送失败时采用指数退避重试（1s、2s、4s）。
- 流式合并：将同一会话的连续流式 delta 消息合并，减少 API 调用。
- 去重抑制：基于内容指纹对重复回复进行抑制，避免重复发送。
- 布尔覆盖：支持渠道级别覆盖全局的 send_progress/send_tool_hints/show_reasoning 配置。
"""

from __future__ import annotations

import asyncio  # 异步事件循环与任务管理
import hashlib  # 内容指纹计算（用于去重）
from collections.abc import Callable  # 可调用对象类型注解
from contextlib import suppress  # 上下文管理器，抑制指定异常
from pathlib import Path  # 路径处理（webui 静态资源目录）
from typing import TYPE_CHECKING, Any  # 类型注解支持

from loguru import logger  # 日志记录

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.channels.deps import (  # 渠道 SDK 依赖检测/自动安装
    channel_sdk_available,
    ensure_channel_deps,
)
from biscuitbot.config.schema import Config  # 全局配置模型
from biscuitbot.utils.restart import (  # 重启通知处理
    consume_restart_notice_from_env,
    format_restart_completed_message,
)

if TYPE_CHECKING:
    from biscuitbot.session.manager import SessionManager  # 会话管理器（仅类型检查时导入）


def _default_webui_dist() -> Path | None:
    """返回内置 webui dist 目录的绝对路径（如存在）。"""
    try:
        import biscuitbot.web as web_pkg  # type: ignore[import-not-found]
    except ImportError:
        return None
    candidate = Path(web_pkg.__file__).resolve().parent / "dist"
    return candidate if candidate.is_dir() else None


# 消息发送重试延迟（指数退避：1秒、2秒、4秒）
_SEND_RETRY_DELAYS = (1, 2, 4)

# 原始消息回复指纹缓存上限（超过时淘汰最旧条目，防内存无限增长）
_MAX_ORIGIN_REPLY_FINGERPRINTS = 4096

# 布尔配置项的驼峰命名别名映射（用于兼容 JSON/TOML 原始配置）
_BOOL_CAMEL_ALIASES: dict[str, str] = {
    "send_progress": "sendProgress",
    "send_tool_hints": "sendToolHints",
    "show_reasoning": "showReasoning",
}

class ChannelManager:
    """渠道管理器，负责管理聊天渠道并协调消息路由。

    职责：
    - 初始化已启用的渠道（Telegram、WhatsApp 等）
    - 启动/停止渠道
    - 路由出站消息到目标渠道
    """

    def __init__(
        self,
        config: Config,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        cron_service: Any | None = None,
        webui_runtime_model_name: Callable[[], str | None] | None = None,
        webui_cron_pending_job_ids: Callable[[str], set[str]] | None = None,
        webui_static_dist: bool = True,
        webui_runtime_surface: str = "browser",
        webui_runtime_capabilities: dict[str, Any] | None = None,
    ):
        self.config = config
        self.bus = bus
        self._session_manager = session_manager  # 会话管理器（可选）
        self._cron_service = cron_service  # 定时任务服务（可选）
        self._webui_runtime_model_name = webui_runtime_model_name  # webui 运行时模型名回调
        self._webui_cron_pending_job_ids = webui_cron_pending_job_ids  # webui 待处理定时任务 ID 回调
        self._webui_static_dist = webui_static_dist  # 是否使用内置 webui 静态资源
        self._webui_runtime_surface = webui_runtime_surface  # webui 运行时展示方式
        self._webui_runtime_capabilities = dict(webui_runtime_capabilities or {})  # webui 运行时能力
        self.channels: dict[str, BaseChannel] = {}  # 已初始化的渠道实例映射
        self._dispatch_task: asyncio.Task | None = None  # 出站消息分发任务
        self._bg_tasks: set[asyncio.Task] = set()  # 后台任务引用（防 GC；stop 时取消）
        self._origin_reply_fingerprints: dict[tuple[str, str, str], str] = {}  # 原始消息回复指纹缓存（去重，有上限）

        self._init_channels()

    def _init_channels(self) -> None:
        """通过 pkgutil 扫描 + entry_points 插件发现并初始化已启用的渠道。"""
        from biscuitbot.channels.registry import discover_channel_names, discover_enabled

        # Collect enabled module names first, then only import those.
        # Channel configs live in ChannelsConfig's extra fields (via
        # extra="allow"), so we enumerate candidates from pkgutil scan
        # (cheap, no imports) and any plugin keys in __pydantic_extra__.
        names = discover_channel_names()
        candidate_names = set(names)
        extra = getattr(self.config.channels, "__pydantic_extra__", None) or {}
        candidate_names.update(extra.keys())

        enabled_names: set[str] = set()
        for name in candidate_names:
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            if (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            ):
                enabled_names.add(name)

        for name, cls in discover_enabled(enabled_names, _names=names).items():
            section = getattr(self.config.channels, name, None)
            if section is None:
                continue
            try:
                kwargs: dict[str, Any] = {}
                if cls.name == "websocket":
                    from biscuitbot.channels.websocket import WebSocketConfig
                    from biscuitbot.webui.gateway_services import build_gateway_services

                    parsed = WebSocketConfig.model_validate(section)
                    static_path = _default_webui_dist() if self._webui_static_dist else None
                    workspace = Path(self.config.workspace_path)
                    gateway = build_gateway_services(
                        config=parsed,
                        bus=self.bus,
                        session_manager=self._session_manager,
                        static_dist_path=static_path,
                        workspace_path=workspace,
                        default_restrict_to_workspace=self.config.tools.restrict_to_workspace,
                        disabled_skills=set(self.config.agents.defaults.disabled_skills),
                        runtime_model_name=self._webui_runtime_model_name,
                        runtime_surface=self._webui_runtime_surface,
                        runtime_capabilities_overrides=self._webui_runtime_capabilities,
                        cron_service=self._cron_service,
                        cron_pending_job_ids=self._webui_cron_pending_job_ids,
                        logger=logger,
                    )
                    kwargs["gateway"] = gateway
                channel = cls(section, self.bus, **kwargs)
                channel.send_progress = self._resolve_bool_override(
                    section, "send_progress", self.config.channels.send_progress,
                )
                channel.send_tool_hints = self._resolve_bool_override(
                    section, "send_tool_hints", self.config.channels.send_tool_hints,
                )
                channel.show_reasoning = self._resolve_bool_override(
                    section, "show_reasoning", self.config.channels.show_reasoning,
                )
                self.channels[name] = channel
                logger.info("{} channel enabled", cls.display_name)
                # 已启用但 SDK 缺失：后台自动安装，避免私聊静默无响应。
                if not channel_sdk_available(cls) and ensure_channel_deps(cls):
                    logger.info("{} SDK 缺失，已触发后台自动安装", cls.display_name)
            except Exception as e:
                logger.warning("{} channel not available: {}", name, e)

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        """校验各渠道的 allowFrom 配置，未配置时进入配对模式。"""
        for name, ch in self.channels.items():
            cfg = ch.config
            if isinstance(cfg, dict):
                allow_all = cfg.get("allow_all")
                if "allow_from" in cfg:
                    allow = cfg.get("allow_from")
                else:
                    allow = cfg.get("allowFrom")
            else:
                allow_all = getattr(cfg, "allow_all", None)
                allow = getattr(cfg, "allow_from", None)
            if allow is None and not allow_all:
                # allowFrom omitted → pairing-only mode.  Unapproved senders
                # receive a pairing code instead of being silently ignored.
                logger.info(
                    '"{}" has no allowFrom; unapproved users will receive a pairing code',
                    name,
                )

    def _should_send_progress(self, channel_name: str, *, tool_hint: bool = False) -> bool:
        """判断是否允许向 *channel_name* 发送进度（或工具提示）消息。"""
        ch = self.channels.get(channel_name)
        if ch is None:
            logger.warning("Progress check for unknown channel: {}", channel_name)
            return False
        return ch.send_tool_hints if tool_hint else ch.send_progress

    def _resolve_bool_override(self, section: Any, key: str, default: bool) -> bool:
        """从 *section* 中返回 *key* 的布尔值，否则返回 *default*。

        对于字典配置，还会检查驼峰命名别名（如 ``sendProgress``
        对应 ``send_progress``），使原始 JSON/TOML 配置能与
        Pydantic 模型协同工作。
        """
        if isinstance(section, dict):
            value = section.get(key)
            if value is None:
                camel = _BOOL_CAMEL_ALIASES.get(key)
                if camel:
                    value = section.get(camel)
            return value if isinstance(value, bool) else default
        value = getattr(section, key, None)
        return value if isinstance(value, bool) else default

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """启动单个渠道并记录异常。"""
        try:
            await channel.start()
        except Exception:
            logger.exception("Failed to start channel {}", name)

    async def start_all(self) -> None:
        """启动所有渠道及出站消息分发器。"""
        if not self.channels:
            logger.warning("No channels enabled")
            return

        # Start outbound dispatcher
        self._dispatch_task = asyncio.create_task(self._dispatch_outbound())

        # Start channels
        tasks = []
        for name, channel in self.channels.items():
            logger.info("Starting {} channel...", name)
            tasks.append(asyncio.create_task(self._start_channel(name, channel)))

        self._notify_restart_done_if_needed()

        # Wait for all to complete (they should run forever)
        await asyncio.gather(*tasks, return_exceptions=True)

    def _notify_restart_done_if_needed(self) -> None:
        """当运行时环境标记存在时，发送重启完成通知消息。"""
        notice = consume_restart_notice_from_env()
        if not notice:
            return
        target = self.channels.get(notice.channel)
        if not target:
            return
        # 持有任务引用，防止协程未执行完就被 GC 回收
        task = asyncio.create_task(self._send_with_retry(
            target,
            OutboundMessage(
                channel=notice.channel,
                chat_id=notice.chat_id,
                content=format_restart_completed_message(notice.started_at_raw),
                metadata=dict(notice.metadata or {}),
            ),
        ))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def stop_all(self) -> None:
        """停止所有渠道及分发器。"""
        logger.info("Stopping all channels...")

        # 取消后台任务（如重启通知发送），等待其结束
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatch_task

        # Stop all channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                logger.info("Stopped {} channel", name)
            except Exception:
                logger.exception("Error stopping {}", name)

    @staticmethod
    def _fingerprint_content(content: str) -> str:
        """计算消息内容的归一化 SHA-1 指纹（用于去重判断）。"""
        normalized = " ".join(content.split())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest() if normalized else ""

    def _should_suppress_outbound(self, msg: OutboundMessage) -> bool:
        """判断出站消息是否应被抑制（重复内容去重）。"""
        metadata = msg.metadata or {}
        if metadata.get("_progress"):
            return False
        fingerprint = self._fingerprint_content(msg.content)
        if not fingerprint:
            return False

        origin_message_id = metadata.get("origin_message_id")
        if isinstance(origin_message_id, str) and origin_message_id:
            key = (msg.channel, msg.chat_id, origin_message_id)
            if self._origin_reply_fingerprints.get(key) == fingerprint:
                return True
            self._origin_reply_fingerprints[key] = fingerprint

        message_id = metadata.get("message_id")
        if isinstance(message_id, str) and message_id:
            key = (msg.channel, msg.chat_id, message_id)
            self._origin_reply_fingerprints[key] = fingerprint

        self._trim_origin_reply_fingerprints()
        return False

    def _trim_origin_reply_fingerprints(self) -> None:
        """限制指纹缓存大小，超过上限时按插入顺序淘汰最旧条目。"""
        while len(self._origin_reply_fingerprints) > _MAX_ORIGIN_REPLY_FINGERPRINTS:
            oldest = next(iter(self._origin_reply_fingerprints))
            self._origin_reply_fingerprints.pop(oldest, None)

    async def _dispatch_outbound(self) -> None:
        """将出站消息分发到对应的渠道。"""
        logger.info("Outbound dispatcher started")

        # Buffer for messages that couldn't be processed during delta coalescing
        # (since asyncio.Queue doesn't support push_front)
        pending: list[OutboundMessage] = []

        while True:
            try:
                # First check pending buffer before waiting on queue
                if pending:
                    msg = pending.pop(0)
                else:
                    msg = await asyncio.wait_for(
                        self.bus.consume_outbound(),
                        timeout=1.0
                    )

                if (
                    msg.metadata.get("_reasoning_delta")
                    or msg.metadata.get("_reasoning_end")
                    or msg.metadata.get("_reasoning")
                ):
                    # Reasoning rides its own plugin channel: only delivered
                    # when the destination channel opts in via ``show_reasoning``
                    # and overrides the streaming primitives. Channels without
                    # a low-emphasis UI affordance keep the base no-op and the
                    # content silently drops here. ``_reasoning`` (one-shot)
                    # is accepted for backward compatibility with hooks that
                    # haven't migrated to delta/end yet.
                    channel = self.channels.get(msg.channel)
                    if channel is not None and channel.show_reasoning:
                        await self._send_with_retry(channel, msg)
                    continue

                if msg.metadata.get("_progress"):
                    if msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel, tool_hint=True,
                    ):
                        continue
                    if not msg.metadata.get("_tool_hint") and not self._should_send_progress(
                        msg.channel, tool_hint=False,
                    ):
                        continue

                if msg.metadata.get("_retry_wait"):
                    continue

                if (
                    msg.metadata.get("_runtime_model_updated")
                    and msg.channel == "websocket"
                    and "websocket" not in self.channels
                ):
                    continue

                # Coalesce consecutive _stream_delta messages for the same (channel, chat_id)
                # to reduce API calls and improve streaming latency
                if msg.metadata.get("_stream_delta") and not msg.metadata.get("_stream_end"):
                    msg, extra_pending = self._coalesce_stream_deltas(msg)
                    pending.extend(extra_pending)

                channel = self.channels.get(msg.channel)
                if channel:
                    # Duplicate suppression is scoped to a known source message
                    # so repeated content from separate turns is still delivered.
                    if (
                        not msg.metadata.get("_stream_delta")
                        and not msg.metadata.get("_stream_end")
                        and not msg.metadata.get("_streamed")
                    ):
                        if self._should_suppress_outbound(msg):
                            logger.info("Suppressing duplicate outbound message to {}:{}", msg.channel, msg.chat_id)
                            continue
                    await self._send_with_retry(channel, msg)
                else:
                    logger.warning("Unknown channel: {}", msg.channel)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    @staticmethod
    async def _send_once(channel: BaseChannel, msg: OutboundMessage) -> None:
        """发送单条出站消息，不应用重试策略。"""
        if msg.metadata.get("_reasoning_end"):
            await channel.send_reasoning_end(msg.chat_id, msg.metadata)
        elif msg.metadata.get("_reasoning_delta"):
            await channel.send_reasoning_delta(msg.chat_id, msg.content, msg.metadata)
        elif msg.metadata.get("_reasoning"):
            # Back-compat: one-shot reasoning. BaseChannel translates this
            # to a single delta + end pair so plugins only implement the
            # streaming primitives.
            await channel.send_reasoning(msg)
        elif msg.metadata.get("_file_edit_events"):
            edits = msg.metadata.get("_file_edit_events")
            await channel.send_file_edit_events(
                msg.chat_id,
                edits if isinstance(edits, list) else [],
                msg.metadata,
            )
        elif msg.metadata.get("_stream_delta") or msg.metadata.get("_stream_end"):
            await channel.send_delta(msg.chat_id, msg.content, msg.metadata)
        elif not msg.metadata.get("_streamed"):
            await channel.send(msg)

    def _coalesce_stream_deltas(
        self, first_msg: OutboundMessage
    ) -> tuple[OutboundMessage, list[OutboundMessage]]:
        """合并同一 (channel, chat_id) 的连续 _stream_delta 消息。

        当队列中累积了多个 delta 时（LLM 生成速度超过渠道处理速度时发生），
        此方法可减少 API 调用次数。

        Returns:
            (合并后的消息, 非匹配消息列表) 的元组
        """
        target_key = (first_msg.channel, first_msg.chat_id)
        combined_content = first_msg.content
        final_metadata = dict(first_msg.metadata or {})
        non_matching: list[OutboundMessage] = []

        # Only merge consecutive deltas. As soon as we hit any other message,
        # stop and hand that boundary back to the dispatcher via `pending`.
        while True:
            try:
                next_msg = self.bus.outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Check if this message belongs to the same stream
            same_target = (next_msg.channel, next_msg.chat_id) == target_key
            is_delta = next_msg.metadata and next_msg.metadata.get("_stream_delta")
            is_end = next_msg.metadata and next_msg.metadata.get("_stream_end")

            if same_target and is_delta and not final_metadata.get("_stream_end"):
                # Accumulate content
                combined_content += next_msg.content
                # If we see _stream_end, remember it and stop coalescing this stream
                if is_end:
                    final_metadata["_stream_end"] = True
                    # Stream ended - stop coalescing this stream
                    break
            else:
                # First non-matching message defines the coalescing boundary.
                non_matching.append(next_msg)
                break

        merged = OutboundMessage(
            channel=first_msg.channel,
            chat_id=first_msg.chat_id,
            content=combined_content,
            metadata=final_metadata,
        )
        return merged, non_matching

    async def _send_with_retry(self, channel: BaseChannel, msg: OutboundMessage) -> None:
        """发送消息，失败时使用指数退避重试。

        注意：CancelledError 会被重新抛出以支持优雅关闭。
        """
        max_attempts = max(self.config.channels.send_max_retries, 1)

        for attempt in range(max_attempts):
            try:
                await self._send_once(channel, msg)
                return  # Send succeeded
            except asyncio.CancelledError:
                raise  # Propagate cancellation for graceful shutdown
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.exception(
                        "Failed to send to {} after {} attempts",
                        msg.channel, max_attempts
                    )
                    return
                delay = _SEND_RETRY_DELAYS[min(attempt, len(_SEND_RETRY_DELAYS) - 1)]
                logger.warning(
                    "Send to {} failed (attempt {}/{}): {}, retrying in {}s",
                    msg.channel, attempt + 1, max_attempts, type(e).__name__, delay
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise  # Propagate cancellation during sleep

    def get_channel(self, name: str) -> BaseChannel | None:
        """按名称获取渠道实例。"""
        return self.channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """获取所有渠道的运行状态。"""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self.channels.items()
        }

    @property
    def enabled_channels(self) -> list[str]:
        """获取已启用的渠道名称列表。"""
        return list(self.channels.keys())
