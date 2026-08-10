"""Discord 渠道实现，基于 discord.py 库。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 Discord 平台组件。
在项目架构中起到的作用：将 Discord 机器人的收发消息能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：通过 ``discord.py`` 库以 WebSocket 网关长连接接入 Discord，收发均走网关，
  无需公网回调地址。
- 鉴权：使用 Bot Token 登录。
- 群聊策略：通过 ``group_policy`` 配置为 ``mention``（仅在被 @ 时响应）或 ``open``（群内
  所有消息都响应），支持频道白名单 ``allow_channels``。
- 斜杠命令：注册了 ``/new``、``/stop``、``/model``、``/help`` 等应用命令。
- 流式输出：支持流式渐进式编辑消息（先发送再多次 edit），并支持「正在输入」指示器与
  表情回应（已读 👀 / 处理中 🔧）。
- 线程支持：自动识别 Discord 线程，按「父频道 + 线程」维度建立会话隔离。
- 代理：支持 HTTP/HTTPS 代理及代理认证。
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from biscuitbot.bus.events import OutboundMessage
from biscuitbot.bus.queue import MessageBus
from biscuitbot.channels.base import BaseChannel
from biscuitbot.command.builtin import build_help_text  # 构建帮助文本
from biscuitbot.config.paths import get_media_dir
from biscuitbot.config.schema import Base
from biscuitbot.utils.helpers import safe_filename, split_message  # 文件名安全化、消息分片

DISCORD_AVAILABLE = importlib.util.find_spec("discord") is not None  # 是否安装了 discord.py
if TYPE_CHECKING:
    import aiohttp
    import discord
    from discord import app_commands
    from discord.abc import Messageable

if DISCORD_AVAILABLE:
    import discord
    from discord import app_commands
    from discord.abc import Messageable

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 附件大小上限（20MB）
MAX_MESSAGE_LEN = 2000  # Discord 单条消息字符数上限
TYPING_INTERVAL_S = 8  # 「正在输入」指示器刷新间隔（秒）


@dataclass
class _StreamBuf:
    """每个会话的流式缓冲区，用于 Discord 渐进式消息编辑。"""

    text: str = ""  # 已累积的文本
    message: Any | None = None  # 已发送的消息对象（用于后续 edit）
    last_edit: float = 0.0  # 上次 edit 的时间戳（用于节流）
    stream_id: str | None = None  # 当前流标识，用于区分并发流


class DiscordConfig(Base):
    """Discord 渠道配置。"""

    enabled: bool = False
    token: str = ""  # Discord Bot Token
    allow_from: list[str] = Field(default_factory=list)  # 允许使用的用户白名单
    allow_channels: list[str] = Field(default_factory=list)  # 允许的频道 ID（空表示全部）
    intents: int = 37377  # 网关 intents 位掩码
    group_policy: Literal["mention", "open"] = "mention"  # 群聊响应策略
    read_receipt_emoji: str = "👀"  # 已读回执表情
    working_emoji: str = "🔧"  # 处理中表情
    working_emoji_delay: float = 2.0  # 处理中表情延迟显示时间（秒）
    streaming: bool = True  # 是否启用流式输出
    proxy: str | None = None  # 代理地址
    proxy_username: str | None = None  # 代理认证用户名
    proxy_password: str | None = None  # 代理认证密码


if DISCORD_AVAILABLE:

    class DiscordBotClient(discord.Client):
        """discord.py 客户端，将事件转发给渠道处理。"""

        def __init__(
            self,
            channel: DiscordChannel,
            *,
            intents: discord.Intents,
            proxy: str | None = None,
            proxy_auth: aiohttp.BasicAuth | None = None,
        ) -> None:
            """初始化客户端并注册应用命令树。"""
            super().__init__(intents=intents, proxy=proxy, proxy_auth=proxy_auth)
            self._channel = channel
            self.tree = app_commands.CommandTree(self)  # 应用命令树
            self._register_app_commands()

        async def on_ready(self) -> None:
            """机器人就绪回调：记录身份并同步应用命令。"""
            self._channel._bot_user_id = str(self.user.id) if self.user else None
            self._channel.logger.info("bot connected as user {}", self._channel._bot_user_id)
            try:
                synced = await self.tree.sync()
                self._channel.logger.info("app commands synced: {}", len(synced))
            except Exception as e:
                self._channel.logger.warning("app command sync failed: {}", e)

        async def on_message(self, message: discord.Message) -> None:
            """入站消息回调，转交渠道处理。"""
            await self._channel._handle_discord_message(message)

        async def on_thread_delete(self, thread: discord.Thread) -> None:
            """线程被删除时清理缓存。"""
            self._channel._forget_channel(thread)

        async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
            """线程归档时清理缓存，否则重新记住。"""
            if getattr(after, "archived", False):
                self._channel._forget_channel(after)
            else:
                self._channel._remember_channel(after)

        async def _reply_ephemeral(self, interaction: discord.Interaction, text: str) -> bool:
            """发送仅对调用者可见的临时交互响应，返回是否成功。"""
            try:
                await interaction.response.send_message(text, ephemeral=True)
                return True
            except Exception as e:
                self._channel.logger.warning("interaction response failed: {}", e)
                return False

        async def _resolve_interaction_channel(
            self,
            interaction: discord.Interaction,
        ) -> Any | None:
            """解析交互所在的频道对象（先缓存后网络拉取）。"""
            channel_id = interaction.channel_id
            if channel_id is None:
                return None
            channel = getattr(interaction, "channel", None) or self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except Exception as e:
                    self._channel.logger.warning("interaction channel {} unavailable: {}", channel_id, e)
                    return None
            self._channel._remember_channel(channel)
            return channel

        async def _interaction_channel_allowed(
            self,
            interaction: discord.Interaction,
            channel: Any | None,
        ) -> bool:
            """判断交互所在频道是否在允许列表内。"""
            allow_channels = self._channel.config.allow_channels
            if not allow_channels:
                return True
            if channel is None:
                channel_id = interaction.channel_id
                return channel_id is not None and str(channel_id) in allow_channels
            channel_ids = self._channel._channel_allow_keys(channel)
            return not channel_ids.isdisjoint(allow_channels)

        async def _forward_slash_command(
            self,
            interaction: discord.Interaction,
            command_text: str,
        ) -> None:
            """将斜杠命令转发为普通入站消息处理。"""
            sender_id = str(interaction.user.id)
            channel_id = interaction.channel_id

            if channel_id is None:
                self._channel.logger.warning("slash command missing channel_id: {}", command_text)
                return

            if not self._channel.is_allowed(sender_id):
                await self._reply_ephemeral(interaction, "You are not allowed to use this bot.")
                return

            channel = await self._resolve_interaction_channel(interaction)
            if not await self._interaction_channel_allowed(interaction, channel):
                await self._reply_ephemeral(interaction, "This channel is not allowed for this bot.")
                return

            await self._reply_ephemeral(interaction, f"Processing {command_text}...")

            metadata: dict[str, Any] = {
                "interaction_id": str(interaction.id),
                "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
                "is_slash_command": True,
            }
            session_key = None
            if channel is not None:
                parent_channel_id = self._channel._channel_parent_key(channel)
                if parent_channel_id is not None:
                    metadata["parent_channel_id"] = parent_channel_id
                    metadata["context_chat_id"] = parent_channel_id
                    metadata["thread_id"] = str(channel_id)
                    session_key = f"{self._channel.name}:{parent_channel_id}:thread:{channel_id}"

            await self._channel._handle_message(
                sender_id=sender_id,
                chat_id=str(channel_id),
                content=command_text,
                metadata=metadata,
                session_key=session_key,
            )

        def _register_app_commands(self) -> None:
            """注册所有应用命令（/new、/stop、/restart、/status、/history、/model、/help）。"""
            commands = (
                ("new", "Stop current task and start a new conversation", "/new"),
                ("stop", "Stop the current task", "/stop"),
                ("restart", "Restart the bot", "/restart"),
                ("status", "Show bot status", "/status"),
                ("history", "Show recent conversation messages", "/history"),
            )

            for name, description, command_text in commands:

                @self.tree.command(name=name, description=description)
                async def command_handler(
                    interaction: discord.Interaction,
                    _command_text: str = command_text,
                ) -> None:
                    await self._forward_slash_command(interaction, _command_text)

            @self.tree.command(name="model", description="Show or switch runtime model preset")
            @app_commands.describe(preset="Optional model preset name, such as default")
            async def model_command(
                interaction: discord.Interaction,
                preset: str | None = None,
            ) -> None:
                preset = (preset or "").strip()
                command_text = f"/model {preset}" if preset else "/model"
                await self._forward_slash_command(interaction, command_text)

            @self.tree.command(name="help", description="Show available commands")
            async def help_command(interaction: discord.Interaction) -> None:
                sender_id = str(interaction.user.id)
                if not self._channel.is_allowed(sender_id):
                    await self._reply_ephemeral(interaction, "You are not allowed to use this bot.")
                    return
                channel = await self._resolve_interaction_channel(interaction)
                if not await self._interaction_channel_allowed(interaction, channel):
                    await self._reply_ephemeral(interaction, "This channel is not allowed for this bot.")
                    return
                await self._reply_ephemeral(interaction, build_help_text())

            @self.tree.error
            async def on_app_command_error(
                interaction: discord.Interaction,
                error: app_commands.AppCommandError,
            ) -> None:
                command_name = interaction.command.qualified_name if interaction.command else "?"
                self._channel.logger.warning(
                    "app command failed user={} channel={} cmd={} error={}",
                    interaction.user.id,
                    interaction.channel_id,
                    command_name,
                    error,
                )

        async def send_outbound(self, msg: OutboundMessage) -> None:
            """按 Discord 投递规则发送一条出站消息。"""
            channel_id = int(msg.chat_id)

            channel = self._channel._known_channels.get(msg.chat_id) or self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except Exception as e:
                    self._channel.logger.warning("channel {} unavailable: {}", msg.chat_id, e)
                    return

            reference, mention_settings = self._build_reply_context(channel, msg.reply_to)
            sent_media = False
            failed_media: list[str] = []

            for index, media_path in enumerate(msg.media or []):
                if await self._send_file(
                    channel,
                    media_path,
                    reference=reference if index == 0 else None,
                    mention_settings=mention_settings,
                ):
                    sent_media = True
                else:
                    failed_media.append(Path(media_path).name)

            for index, chunk in enumerate(
                self._build_chunks(msg.content or "", failed_media, sent_media)
            ):
                kwargs: dict[str, Any] = {"content": chunk}
                if index == 0 and reference is not None and not sent_media:
                    kwargs["reference"] = reference
                    kwargs["allowed_mentions"] = mention_settings
                await channel.send(**kwargs)

        async def _send_file(
            self,
            channel: Messageable,
            file_path: str,
            *,
            reference: discord.PartialMessage | None,
            mention_settings: discord.AllowedMentions,
        ) -> bool:
            """通过 discord.py 发送一个文件附件。"""
            path = Path(file_path)
            if not path.is_file():
                self._channel.logger.warning("file not found, skipping: {}", file_path)
                return False

            if path.stat().st_size > MAX_ATTACHMENT_BYTES:
                self._channel.logger.warning("file too large (>20MB), skipping: {}", path.name)
                return False

            try:
                kwargs: dict[str, Any] = {"file": discord.File(path)}
                if reference is not None:
                    kwargs["reference"] = reference
                    kwargs["allowed_mentions"] = mention_settings
                await channel.send(**kwargs)
                self._channel.logger.info("file sent: {}", path.name)
                return True
            except Exception:
                self._channel.logger.exception("Error sending file {}", path.name)
                return False

        @staticmethod
        def _build_chunks(content: str, failed_media: list[str], sent_media: bool) -> list[str]:
            """构建出站文本分片，包含附件失败时的回退文本。"""
            chunks = split_message(content, MAX_MESSAGE_LEN)
            if chunks or not failed_media or sent_media:
                return chunks
            fallback = "\n".join(f"[attachment: {name} - send failed]" for name in failed_media)
            return split_message(fallback, MAX_MESSAGE_LEN)

        def _build_reply_context(
            self,
            channel: Messageable,
            reply_to: str | None,
        ) -> tuple[discord.PartialMessage | None, discord.AllowedMentions]:
            """为出站消息构建回复上下文（引用目标消息）。"""
            mention_settings = discord.AllowedMentions(replied_user=False)
            if not reply_to:
                return None, mention_settings
            try:
                message_id = int(reply_to)
            except ValueError:
                self._channel.logger.warning("Invalid reply target: {}", reply_to)
                return None, mention_settings

            return channel.get_partial_message(message_id), mention_settings


class DiscordChannel(BaseChannel):
    """Discord 渠道（基于 discord.py）。"""

    name = "discord"
    display_name = "Discord"
    _STREAM_EDIT_INTERVAL = 0.8  # 流式消息 edit 节流间隔（秒）

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置。"""
        return DiscordConfig().model_dump(by_alias=True)

    @staticmethod
    def _channel_key(channel_or_id: Any) -> str:
        """将频道对象或 ID 归一化为稳定的字符串键。"""
        channel_id = getattr(channel_or_id, "id", channel_or_id)
        return str(channel_id)

    @classmethod
    def _channel_allow_keys(cls, channel: Any) -> set[str]:
        """返回可用于满足 allow_channels 校验的频道 ID 集合（含父频道）。"""
        keys = {cls._channel_key(channel)}
        if parent_key := cls._channel_parent_key(channel):
            keys.add(parent_key)
        return keys

    @classmethod
    def _channel_parent_key(cls, channel: Any) -> str | None:
        """返回线程类频道的父频道键。"""
        parent_id = getattr(channel, "parent_id", None)
        if parent_id is not None:
            return cls._channel_key(parent_id)
        parent = getattr(channel, "parent", None)
        if parent is not None:
            return cls._channel_key(parent)
        return None

    def __init__(self, config: Any, bus: MessageBus):
        """初始化 Discord 渠道及各类运行时状态字典。"""
        if isinstance(config, dict):
            config = DiscordConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: DiscordConfig = config
        self._client: DiscordBotClient | None = None
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}  # 各频道的「正在输入」任务
        self._bot_user_id: str | None = None  # 本机器人用户 ID（用于自回环防护）
        self._pending_reactions: dict[str, Any] = {}  # chat_id -> 待清理表情的消息对象
        self._working_emoji_tasks: dict[str, asyncio.Task[None]] = {}  # 延迟处理中表情任务
        self._stream_bufs: dict[str, _StreamBuf] = {}  # 各会话流式缓冲区
        self._known_channels: dict[str, Any] = {}  # 已知频道缓存

    def _remember_channel(self, channel: Any) -> None:
        """将频道加入缓存。"""
        self._known_channels[self._channel_key(channel)] = channel

    def _forget_channel(self, channel_or_id: Any) -> None:
        """将频道从缓存移除。"""
        self._known_channels.pop(self._channel_key(channel_or_id), None)

    async def start(self) -> None:
        """启动 Discord 客户端。"""
        if not DISCORD_AVAILABLE:
            self.logger.error("discord.py not installed. Run: pip install biscuitbot[discord]")
            return

        if not self.config.token:
            self.logger.error("bot token not configured")
            return

        try:
            intents = discord.Intents.none()
            intents.value = self.config.intents

            proxy_auth = None
            has_user = bool(self.config.proxy_username)
            has_pass = bool(self.config.proxy_password)
            if has_user and has_pass:
                import aiohttp

                proxy_auth = aiohttp.BasicAuth(
                    login=self.config.proxy_username,
                    password=self.config.proxy_password,
                )
            elif has_user != has_pass:
                self.logger.warning(
                    "proxy auth incomplete: both proxy_username and "
                    "proxy_password must be set; ignoring partial credentials",
                )

            self._client = DiscordBotClient(
                self,
                intents=intents,
                proxy=self.config.proxy,
                proxy_auth=proxy_auth,
            )
        except Exception:
            self.logger.exception("Failed to initialize client")
            self._client = None
            self._running = False
            return

        self._running = True
        self.logger.info("Starting client via discord.py...")

        try:
            await self._client.start(self.config.token)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("client startup failed")
        finally:
            self._running = False
            await self._reset_runtime_state(close_client=True)

    async def stop(self) -> None:
        """停止 Discord 渠道。"""
        self._running = False
        await self._reset_runtime_state(close_client=True)

    async def send(self, msg: OutboundMessage) -> None:
        """通过 discord.py 发送一条消息。"""
        client = self._client
        if client is None or not client.is_ready():
            self.logger.warning("client not ready; dropping outbound message")
            return

        is_progress = bool((msg.metadata or {}).get("_progress"))

        try:
            await client.send_outbound(msg)
        except Exception:
            self.logger.exception("Error sending message")
            raise
        finally:
            if not is_progress:
                await self._stop_typing(msg.chat_id)
                await self._clear_reactions(msg.chat_id)

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """渐进式 Discord 投递：先发送一次，随后在流结束前不断 edit。"""
        client = self._client
        if client is None or not client.is_ready():
            self.logger.warning("client not ready; dropping stream delta")
            return

        meta = metadata or {}
        stream_id = meta.get("_stream_id")

        if meta.get("_stream_end"):
            buf = self._stream_bufs.get(chat_id)
            if not buf or buf.message is None or not buf.text:
                return
            if stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id:
                return
            await self._finalize_stream(chat_id, buf)
            return

        buf = self._stream_bufs.get(chat_id)
        if buf is None or (
            stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id
        ):
            buf = _StreamBuf(stream_id=stream_id)
            self._stream_bufs[chat_id] = buf
        elif buf.stream_id is None:
            buf.stream_id = stream_id

        buf.text += delta
        if not buf.text.strip():
            return

        target = await self._resolve_channel(chat_id)
        if target is None:
            self.logger.warning("stream target {} unavailable", chat_id)
            return

        now = time.monotonic()
        if buf.message is None:
            try:
                buf.message = await target.send(content=buf.text)
                buf.last_edit = now
            except Exception as e:
                self.logger.warning("stream initial send failed: {}", e)
                raise
            return

        if (now - buf.last_edit) < self._STREAM_EDIT_INTERVAL:
            return

        try:
            await buf.message.edit(content=DiscordBotClient._build_chunks(buf.text, [], False)[0])
            buf.last_edit = now
        except Exception as e:
            self.logger.warning("stream edit failed: {}", e)
            raise

    async def _handle_discord_message(self, message: discord.Message) -> None:
        """处理来自 discord.py 的入站消息。

        自回环防护：仅丢弃本机器人自身账号发出的消息，允许其他机器人的消息通过，
        以支持多智能体协作（一个机器人 @ 另一个机器人等）。机器人间循环仍按实例
        防护（每个机器人忽略自己的出站消息）。(#3217)
        """
        if self._bot_user_id is not None and str(message.author.id) == self._bot_user_id:
            return
        if self._is_system_message(message):
            return

        sender_id = str(message.author.id)
        channel_id = self._channel_key(message.channel)
        self._remember_channel(message.channel)
        content = message.content or ""

        if not self._should_accept_inbound(message, sender_id, content):
            return

        media_paths, attachment_markers = await self._download_attachments(message.attachments)
        full_content = self._compose_inbound_content(content, attachment_markers)
        metadata = self._build_inbound_metadata(message)
        parent_channel_id = self._channel_parent_key(message.channel)
        session_key = None
        if parent_channel_id is not None:
            metadata["parent_channel_id"] = parent_channel_id
            metadata["context_chat_id"] = parent_channel_id
            metadata["thread_id"] = channel_id
            session_key = f"{self.name}:{parent_channel_id}:thread:{channel_id}"

        await self._start_typing(message.channel)

        # 立即添加已读回执表情，处理中表情延迟显示
        try:
            await message.add_reaction(self.config.read_receipt_emoji)
            self._pending_reactions[channel_id] = message
        except Exception as e:
            self.logger.debug("Failed to add read receipt reaction: {}", e)

        # 延迟的处理中指示器（仅为视觉提示，不与子代理生命周期绑定）
        async def _delayed_working_emoji() -> None:
            await asyncio.sleep(self.config.working_emoji_delay)
            with suppress(Exception):
                await message.add_reaction(self.config.working_emoji)

        self._working_emoji_tasks[channel_id] = asyncio.create_task(_delayed_working_emoji())

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=channel_id,
                content=full_content,
                media=media_paths,
                metadata=metadata,
                session_key=session_key,
                is_dm=message.guild is None,
            )
        except Exception:
            await self._clear_reactions(channel_id)
            await self._stop_typing(channel_id)
            raise

    async def _on_message(self, message: discord.Message) -> None:
        """向后兼容的别名，供旧测试/调用方使用。"""
        await self._handle_discord_message(message)

    async def _resolve_channel(self, chat_id: str) -> Any | None:
        """解析 Discord 频道：先查缓存，再网络拉取。"""
        client = self._client
        if client is None or not client.is_ready():
            return None
        channel = self._known_channels.get(chat_id)
        if channel is not None:
            return channel
        channel_id = int(chat_id)
        channel = client.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await client.fetch_channel(channel_id)
        except Exception as e:
            self.logger.warning("channel {} unavailable: {}", chat_id, e)
            return None

    async def _finalize_stream(self, chat_id: str, buf: _StreamBuf) -> None:
        """提交最终流式内容并刷新溢出分片。"""
        chunks = DiscordBotClient._build_chunks(buf.text, [], False)
        if not chunks:
            self._stream_bufs.pop(chat_id, None)
            return

        try:
            await buf.message.edit(content=chunks[0])
        except Exception as e:
            self.logger.warning("final stream edit failed: {}", e)
            raise

        target = getattr(buf.message, "channel", None) or await self._resolve_channel(chat_id)
        if target is None:
            self.logger.warning("stream follow-up target {} unavailable", chat_id)
            self._stream_bufs.pop(chat_id, None)
            return

        for extra_chunk in chunks[1:]:
            await target.send(content=extra_chunk)

        self._stream_bufs.pop(chat_id, None)
        await self._stop_typing(chat_id)
        await self._clear_reactions(chat_id)

    def _should_accept_inbound(
        self,
        message: discord.Message,
        sender_id: str,
        content: str,
    ) -> bool:
        """判断入站 Discord 消息是否应被处理。"""
        if not self.is_allowed(sender_id):
            return False
        # 基于频道的过滤：仅在允许的频道中响应
        allow_channels = self.config.allow_channels
        if allow_channels:
            channel_ids = self._channel_allow_keys(message.channel)
            if channel_ids.isdisjoint(allow_channels):
                return False
        if message.guild is not None and not self._should_respond_in_group(message, content):
            return False
        return True

    async def _download_attachments(
        self,
        attachments: list[discord.Attachment],
    ) -> tuple[list[str], list[str]]:
        """下载受支持的附件，返回本地路径列表与展示标记列表。"""
        media_paths: list[str] = []
        markers: list[str] = []
        media_dir = get_media_dir("discord")

        for attachment in attachments:
            filename = attachment.filename or "attachment"
            if attachment.size and attachment.size > MAX_ATTACHMENT_BYTES:
                markers.append(f"[attachment: {filename} - too large]")
                continue
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                safe_name = safe_filename(filename)
                file_path = media_dir / f"{attachment.id}_{safe_name}"
                await attachment.save(file_path)
                media_paths.append(str(file_path))
                markers.append(f"[attachment: {file_path.name}]")
            except Exception as e:
                self.logger.warning("Failed to download attachment: {}", e)
                markers.append(f"[attachment: {filename} - download failed]")

        return media_paths, markers

    @staticmethod
    def _compose_inbound_content(content: str, attachment_markers: list[str]) -> str:
        """将消息文本与附件标记组合为完整内容。"""
        content_parts = [content] if content else []
        content_parts.extend(attachment_markers)
        return "\n".join(part for part in content_parts if part) or "[empty message]"

    @staticmethod
    def _is_system_message(message: discord.Message) -> bool:
        """判断是否为不携带用户提示的 Discord 系统消息。"""
        message_type = getattr(message, "type", discord.MessageType.default)
        return message_type not in {discord.MessageType.default, discord.MessageType.reply}

    @staticmethod
    def _build_inbound_metadata(message: discord.Message) -> dict[str, str | None]:
        """为入站消息构建元数据。"""
        reply_to = (
            str(message.reference.message_id)
            if message.reference and message.reference.message_id
            else None
        )
        return {
            "message_id": str(message.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "reply_to": reply_to,
        }

    def _should_respond_in_group(self, message: discord.Message, content: str) -> bool:
        """根据群聊策略判断是否在群频道中响应。"""
        if self.config.group_policy == "open":
            return True

        if self.config.group_policy == "mention":
            bot_user_id = self._bot_user_id
            if bot_user_id is None and self._client and self._client.user:
                bot_user_id = str(self._client.user.id)
            if bot_user_id is None:
                self.logger.debug(
                    "message in {} ignored (bot identity unavailable)", message.channel.id
                )
                return False

            if any(str(user.id) == bot_user_id for user in message.mentions):
                return True
            if bot_user_id in {str(user_id) for user_id in getattr(message, "raw_mentions", [])}:
                return True
            if f"<@{bot_user_id}>" in content or f"<@!{bot_user_id}>" in content:
                return True
            if self._references_bot_message(message, bot_user_id):
                return True

            self.logger.debug("message in {} ignored (bot not mentioned)", message.channel.id)
            return False

        return True

    @staticmethod
    def _references_bot_message(message: discord.Message, bot_user_id: str) -> bool:
        """判断一条回复是否针对本机器人发出的消息。"""
        reference = getattr(message, "reference", None)
        if reference is None:
            return False
        referenced_message = getattr(reference, "resolved", None) or getattr(
            reference, "cached_message", None
        )
        author = getattr(referenced_message, "author", None)
        return str(getattr(author, "id", "")) == bot_user_id

    async def _start_typing(self, channel: Messageable) -> None:
        """为频道启动周期性「正在输入」指示器。"""
        channel_id = self._channel_key(channel)
        await self._stop_typing(channel_id)

        async def typing_loop() -> None:
            while self._running:
                try:
                    async with channel.typing():
                        await asyncio.sleep(TYPING_INTERVAL_S)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    self.logger.debug("typing indicator failed for {}: {}", channel_id, e)
                    return

        self._typing_tasks[channel_id] = asyncio.create_task(typing_loop())

    async def _stop_typing(self, channel_id: str) -> None:
        """停止频道的「正在输入」指示器。"""
        task = self._typing_tasks.pop(self._channel_key(channel_id), None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _clear_reactions(self, chat_id: str) -> None:
        """机器人回复后清除所有待处理表情。"""
        # 若延迟处理中表情尚未触发，则取消之
        task = self._working_emoji_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

        msg_obj = self._pending_reactions.pop(chat_id, None)
        if msg_obj is None:
            return
        bot_user = self._client.user if self._client else None
        for emoji in (self.config.read_receipt_emoji, self.config.working_emoji):
            with suppress(Exception):
                await msg_obj.remove_reaction(emoji, bot_user)

    async def _cancel_all_typing(self) -> None:
        """停止所有「正在输入」任务。"""
        channel_ids = list(self._typing_tasks)
        for channel_id in channel_ids:
            await self._stop_typing(channel_id)

    async def _reset_runtime_state(self, close_client: bool) -> None:
        """重置客户端与输入状态。"""
        await self._cancel_all_typing()
        self._stream_bufs.clear()
        self._known_channels.clear()
        if close_client and self._client is not None and not self._client.is_closed():
            try:
                await self._client.close()
            except Exception as e:
                self.logger.warning("client close failed: {}", e)
        self._client = None
        self._bot_user_id = None
