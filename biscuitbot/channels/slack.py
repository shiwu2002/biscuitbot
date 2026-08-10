"""Slack 渠道实现，使用 Socket Mode。

所属模块与项目作用
==================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 Slack 平台组件。
在项目架构中起到的作用：通过 Slack Socket Mode（WebSocket 长连接）将 Slack 的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：Slack Socket Mode（WebSocket），无需公网 webhook，适合本地与受限网络环境。
- 鉴权：使用 bot_token（xoxb-）进行 Web API 调用，app_token（xapp-）建立 Socket Mode 连接。
- 消息格式：将 Markdown 转换为 Slack mrkdwn（含表格转换为列表、粗体/标题修正等）。
- 线程支持：支持在触发消息的线程内回复；DM 中不自动开线程，但尊重用户手动开启的线程。
- 表情回应：收到消息时添加 :eyes: 表情，回复完成后替换为 :white_check_mark:。
- 线程上下文：首次被拉入线程时，拉取线程历史作为上下文（去重，避免重复拉取）。
- 文件处理：下载 Slack 私有文件到本地媒体目录（带 bot_token 鉴权与 HTML 内容检测）。
- 目标解析：支持 #channel、@user、<#C123>、<@U123> 等多种目标格式解析为具体 ID。
- 策略配置：DM 与群组/频道分别配置响应策略（open/mention/allowlist）。
"""

import asyncio  # 异步事件循环与超时控制
import re  # 正则表达式（ID 解析、Markdown 修正、机器人提及剥离）
from pathlib import Path  # 路径处理（文件下载）
from typing import Any  # 类型注解支持

import httpx  # 异步 HTTP 客户端（文件下载）
from pydantic import Field  # Pydantic 模型字段定义
from slack_sdk.socket_mode.request import SocketModeRequest  # Socket Mode 请求
from slack_sdk.socket_mode.response import SocketModeResponse  # Socket Mode 响应
from slack_sdk.socket_mode.websockets import SocketModeClient  # Socket Mode WebSocket 客户端
from slack_sdk.web.async_client import AsyncWebClient  # Slack Web API 异步客户端
from slackify_markdown import slackify_markdown  # Markdown 转 Slack mrkdwn

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_media_dir  # 媒体目录获取
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.pairing import is_approved  # 配对授权检查
from biscuitbot.utils.helpers import safe_filename, split_message  # 安全文件名与消息分块


class SlackDMConfig(Base):
    """Slack 私聊（DM）策略配置。"""

    enabled: bool = True
    policy: str = "open"  # "open"（开放）或 "allowlist"（白名单）
    allow_from: list[str] = Field(default_factory=list)  # 白名单策略下允许的用户 ID 列表


class SlackConfig(Base):
    """Slack 渠道配置。"""

    enabled: bool = False
    mode: str = "socket"  # 运行模式（当前仅支持 "socket"）
    webhook_path: str = "/slack/events"  # webhook 路径（保留字段）
    bot_token: str = ""  # Slack bot token（xoxb-）
    app_token: str = ""  # Slack app token（xapp-），用于 Socket Mode
    user_token_read_only: bool = True  # 用户 token 是否只读
    reply_in_thread: bool = True  # 是否在线程内回复
    react_emoji: str = "eyes"  # 处理中表情
    done_emoji: str = "white_check_mark"  # 完成表情
    include_thread_context: bool = True  # 是否包含线程上下文
    thread_context_limit: int = 20  # 线程上下文消息数量上限
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单（兼容字段）
    group_policy: str = "mention"  # 群组策略："open"/"mention"/"allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # 群组白名单
    # 当 group_policy 为 "allowlist" 时，是否还要求 @提及才响应
    # （使机器人仅回复已批准频道中的提及，而非每条消息）。
    # 对 "mention"/"open" 策略无影响。
    group_require_mention: bool = False
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)  # DM 策略


SLACK_MAX_MESSAGE_LEN = 39_000  # Slack API 允许约 40k 字符，留余量
SLACK_DOWNLOAD_TIMEOUT = 30.0  # 文件下载超时（秒）
# Socket Mode WSS 握手超时秒数。REST auth_test 可能成功而 WSS 被阻断
# （防火墙/区域限制）。slack-sdk 不会将 HTTP(S)_PROXY 应用于
# websockets.connect —— 参见 slack_sdk.socket_mode.websockets.SocketModeClient.connect。
SLACK_SOCKET_CONNECT_TIMEOUT_S = 45.0
_HTML_DOWNLOAD_PREFIXES = (b"<!doctype html", b"<html")  # HTML 内容前缀检测（用于识别错误响应）


class SlackChannel(BaseChannel):
    """Slack 渠道，使用 Socket Mode。"""

    name = "slack"
    display_name = "Slack"
    _SLACK_ID_RE = re.compile(r"^[CDGUW][A-Z0-9]{2,}$")  # Slack ID 格式（C=channel, D=DM, G=group, U/W=user）
    _SLACK_CHANNEL_REF_RE = re.compile(r"^<#([A-Z0-9]+)(?:\|[^>]+)?>$")  # 频道引用 <#C123|name>
    _SLACK_USER_REF_RE = re.compile(r"^<@([A-Z0-9]+)(?:\|[^>]+)?>$")  # 用户引用 <@U123|name>

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return SlackConfig().model_dump(by_alias=True)

    _THREAD_CONTEXT_CACHE_LIMIT = 10_000  # 线程上下文去重集合上限

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = SlackConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self._web_client: AsyncWebClient | None = None  # Slack Web API 客户端
        self._socket_client: SocketModeClient | None = None  # Socket Mode 客户端
        self._bot_user_id: str | None = None  # 机器人用户 ID（用于提及处理）
        self._target_cache: dict[str, str] = {}  # 目标解析缓存（名称 -> ID）
        self._thread_context_attempted: set[str] = set()  # 已拉取线程上下文的键集合

    async def start(self) -> None:
        """启动 Slack Socket Mode 客户端。"""
        if not self.config.bot_token or not self.config.app_token:
            self.logger.error("bot/app token not configured")
            return
        if self.config.mode != "socket":
            self.logger.error("Unsupported mode: {}", self.config.mode)
            return

        self._running = True

        self._web_client = AsyncWebClient(token=self.config.bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self._web_client,
        )

        # 注册 Socket Mode 请求监听器
        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # 解析机器人用户 ID，用于提及处理
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            self.logger.info("bot connected as {}", self._bot_user_id)
        except Exception as e:
            self.logger.warning("auth_test failed: {}", e)

        self.logger.info("Starting Socket Mode client...")
        try:
            # 带超时地建立 WSS 连接，避免防火墙阻断时永久阻塞
            await asyncio.wait_for(
                self._socket_client.connect(),
                timeout=SLACK_SOCKET_CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            self.logger.error(
                "Slack Socket Mode WebSocket handshake timed out after {:.0f}s. "
                "auth_test uses HTTPS and may still succeed while WSS is blocked. "
                "Check outbound access to Slack WebSockets; slack-sdk Socket Mode "
                "does not apply HTTP(S)_PROXY to websockets.connect.",
                SLACK_SOCKET_CONNECT_TIMEOUT_S,
            )
            await self.stop()
            raise RuntimeError("Slack Socket Mode WebSocket connect timed out") from None

        self.logger.info("Slack Socket Mode WebSocket connected (events enabled)")

        # 保持运行直到被停止
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止 Slack 客户端。"""
        self._running = False
        if self._socket_client:
            try:
                await self._socket_client.close()
            except Exception as e:
                self.logger.warning("socket close failed: {}", e)
            self._socket_client = None

    async def send(self, msg: OutboundMessage) -> None:
        """通过 Slack 发送消息。"""
        if not self._web_client:
            self.logger.warning("client not running")
            return
        try:
            target_chat_id = await self._resolve_target_chat_id(msg.chat_id)
            slack_meta = msg.metadata.get("slack", {}) if msg.metadata else {}
            thread_ts = slack_meta.get("thread_ts")
            origin_chat_id = str((slack_meta.get("event", {}) or {}).get("channel") or msg.chat_id)
            # 在入站消息所属的同一线程内回复（对频道线程和 DM 线程均适用）。
            # 当代理转发到不同频道时，丢弃 thread_ts，因为它仅在原会话内有意义。
            thread_ts_param = thread_ts if thread_ts and target_chat_id == origin_chat_id else None

            is_progress = (msg.metadata or {}).get("_progress", False)
            if is_progress and not msg.content:
                pass  # 跳过空的进度消息（如仅工具事件更新）
            elif msg.content or not (msg.media or []):
                mrkdwn = self._to_mrkdwn(msg.content) if msg.content else " "
                buttons = getattr(msg, "buttons", None) or []
                chunks = split_message(mrkdwn, SLACK_MAX_MESSAGE_LEN)
                for index, chunk in enumerate(chunks):
                    kwargs: dict[str, Any] = dict(
                        channel=target_chat_id, text=chunk, thread_ts=thread_ts_param,
                    )
                    # 按钮仅在最后一个分块携带
                    if buttons and index == len(chunks) - 1:
                        kwargs["blocks"] = self._build_button_blocks(chunk, buttons)
                    await self._web_client.chat_postMessage(**kwargs)

            # 上传媒体文件
            for media_path in msg.media or []:
                try:
                    await self._web_client.files_upload_v2(
                        channel=target_chat_id,
                        file=media_path,
                        thread_ts=thread_ts_param,
                    )
                except Exception:
                    self.logger.exception("Failed to upload file {}", media_path)

            # 最终（非进度）回复发送后更新表情
            if not (msg.metadata or {}).get("_progress"):
                event = slack_meta.get("event", {})
                await self._update_react_emoji(origin_chat_id, event.get("ts"))

        except Exception:
            self.logger.exception("Error sending message")
            raise

    async def _resolve_target_chat_id(self, target: str) -> str:
        """将人类友好的 Slack 目标解析为具体 ID（必要时）。"""
        if not self._web_client:
            return target

        target = target.strip()
        if not target:
            return target

        # <#C123|name> 频道引用
        if match := self._SLACK_CHANNEL_REF_RE.fullmatch(target):
            return match.group(1)
        # <@U123|name> 用户引用，需打开 DM
        if match := self._SLACK_USER_REF_RE.fullmatch(target):
            return await self._open_dm_for_user(match.group(1))
        # 纯 ID：U/W 开头为用户，需打开 DM；其余直接返回
        if self._SLACK_ID_RE.fullmatch(target):
            if target.startswith(("U", "W")):
                return await self._open_dm_for_user(target)
            return target

        # #channel 名称
        if target.startswith("#"):
            return await self._resolve_channel_name(target[1:])
        # @user handle
        if target.startswith("@"):
            return await self._resolve_user_handle(target[1:])

        # 无前缀：先尝试频道名，失败则尝试用户 handle
        try:
            return await self._resolve_channel_name(target)
        except ValueError:
            return await self._resolve_user_handle(target)

    async def _resolve_channel_name(self, name: str) -> str:
        """通过分页 conversations.list 查找频道名对应的 ID。"""
        normalized = self._normalize_target_name(name)
        if not normalized:
            raise ValueError("Slack target channel name is empty")

        cache_key = f"channel:{normalized}"
        if cache_key in self._target_cache:
            return self._target_cache[cache_key]

        cursor: str | None = None
        while True:
            response = await self._web_client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for channel in response.get("channels", []):
                if self._normalize_target_name(str(channel.get("name") or "")) == normalized:
                    channel_id = str(channel.get("id") or "")
                    if channel_id:
                        self._target_cache[cache_key] = channel_id
                        return channel_id
            cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break

        raise ValueError(
            f"Slack channel '{name}' was not found. Use a joined channel name like "
            f"'#general' or a concrete channel ID."
        )

    async def _resolve_user_handle(self, handle: str) -> str:
        """通过分页 users.list 查找用户 handle 对应的 DM 频道 ID。"""
        normalized = self._normalize_target_name(handle)
        if not normalized:
            raise ValueError("Slack target user handle is empty")

        cache_key = f"user:{normalized}"
        if cache_key in self._target_cache:
            return self._target_cache[cache_key]

        cursor: str | None = None
        while True:
            response = await self._web_client.users_list(limit=200, cursor=cursor)
            for member in response.get("members", []):
                if self._member_matches_handle(member, normalized):
                    user_id = str(member.get("id") or "")
                    if not user_id:
                        continue
                    dm_id = await self._open_dm_for_user(user_id)
                    self._target_cache[cache_key] = dm_id
                    return dm_id
            cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break

        raise ValueError(
            f"Slack user '{handle}' was not found. Use '@name' or a concrete DM/channel ID."
        )

    async def _open_dm_for_user(self, user_id: str) -> str:
        """为用户打开 DM 会话并返回频道 ID。"""
        response = await self._web_client.conversations_open(users=user_id)
        channel_id = str(((response.get("channel") or {}).get("id")) or "")
        if not channel_id:
            raise ValueError(f"Slack DM target for user '{user_id}' could not be opened.")
        return channel_id

    @staticmethod
    def _normalize_target_name(value: str) -> str:
        """归一化目标名称：去除首尾空白、去除 #@ 前缀、转小写。"""
        return value.strip().lstrip("#@").lower()

    @classmethod
    def _member_matches_handle(cls, member: dict[str, Any], normalized: str) -> bool:
        """检查成员的任一名称字段是否匹配归一化后的 handle。"""
        profile = member.get("profile") or {}
        candidates = {
            str(member.get("name") or ""),
            str(profile.get("display_name") or ""),
            str(profile.get("display_name_normalized") or ""),
            str(profile.get("real_name") or ""),
            str(profile.get("real_name_normalized") or ""),
        }
        return normalized in {cls._normalize_target_name(candidate) for candidate in candidates if candidate}

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        req: SocketModeRequest,
    ) -> None:
        """处理入站 Socket Mode 请求。"""
        if req.type == "interactive":
            await self._on_block_action(client, req)
            return
        if req.type != "events_api":
            return

        # 立即确认请求
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        # 仅处理 app_mention 或普通 message 事件
        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        subtype = event.get("subtype")
        # Slack 对带附件的用户消息使用 subtype=file_share。
        # 忽略其他子类型如 bot_message / message_changed / deleted。
        if subtype and subtype != "file_share":
            return
        # 跳过机器人自身发的消息
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        # 避免重复处理：Slack 对频道中的提及同时发送 `message` 和 `app_mention`。
        # 优先使用 `app_mention`。
        text = event.get("text") or ""
        if event_type == "message" and self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            return

        # 调试：记录基本事件形状
        self.logger.debug(
            "event: type={} subtype={} user={} channel={} channel_type={} text={}",
            event_type,
            subtype,
            sender_id,
            chat_id,
            event.get("channel_type"),
            text[:80],
        )
        if not sender_id or not chat_id:
            return

        channel_type = event.get("channel_type") or ""

        # 策略检查
        if not self._is_allowed(sender_id, chat_id, channel_type):
            # 被拒绝的 DM 走基类配对流程
            if channel_type == "im" and self.config.dm.enabled:
                await self._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content="",
                    is_dm=True,
                )
            return

        # 非 DM 频道需满足群组响应策略
        if channel_type != "im" and not self._should_respond_in_channel(event_type, text, chat_id):
            return

        # 剥离机器人提及
        text = self._strip_bot_mention(text)

        event_ts = event.get("ts")
        raw_thread_ts = event.get("thread_ts")
        thread_ts = raw_thread_ts
        # DM 中不自动为顶层消息开线程（否则回复会被折叠到"1 条回复"下）。
        # 但若用户在 DM 内显式开启了线程，raw_thread_ts 会被设置，我们予以尊重。
        if (
            self.config.reply_in_thread
            and not thread_ts
            and channel_type != "im"
        ):
            thread_ts = event_ts
        # 对触发消息添加 :eyes: 表情（尽力而为）
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.react_emoji,
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            self.logger.debug("reactions_add failed: {}", e)

        # 当用户处于真实线程中时（raw_thread_ts 已设置），使用线程作用域的会话键。
        # DM 线程拥有独立会话，与 DM 根会话分离，避免上下文跨线程边界泄漏。
        session_key = (
            f"slack:{chat_id}:{thread_ts}" if thread_ts and raw_thread_ts else None
        )
        # 下载附件文件
        media_paths: list[str] = []
        file_markers: list[str] = []
        for file_info in event.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            file_path, marker = await self._download_slack_file(file_info)
            if file_path:
                media_paths.append(file_path)
            if marker:
                file_markers.append(marker)

        # 斜杠命令不附加线程上下文
        is_slash = text.strip().startswith("/")
        content = text if is_slash else await self._with_thread_context(
            text,
            chat_id=chat_id,
            channel_type=channel_type,
            thread_ts=thread_ts,
            raw_thread_ts=raw_thread_ts,
            current_ts=event_ts,
        )
        if file_markers:
            content = "\n".join(part for part in [content, *file_markers] if part)
        if not content and not media_paths:
            return

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata={
                    "slack": {
                        "event": event,
                        "thread_ts": thread_ts,
                        "channel_type": channel_type,
                    },
                },
                session_key=session_key,
            )
        except Exception:
            self.logger.exception("Error handling message from {}", sender_id)

    async def _download_slack_file(self, file_info: dict[str, Any]) -> tuple[str | None, str]:
        """下载 Slack 私有文件到本地媒体目录。"""
        file_id = str(file_info.get("id") or "file")
        name = str(
            file_info.get("name")
            or file_info.get("title")
            or file_info.get("id")
            or "slack-file"
        )
        marker_type = "image" if str(file_info.get("mimetype") or "").startswith("image/") else "file"
        marker = f"[{marker_type}: {name}]"
        url = str(file_info.get("url_private_download") or file_info.get("url_private") or "")
        if not url:
            return None, self._download_failure_marker(marker_type, name, "missing download url")
        if not self.config.bot_token:
            return None, self._download_failure_marker(marker_type, name, "missing bot token")

        filename = safe_filename(f"{file_id}_{name}")
        path = Path(get_media_dir("slack")) / filename
        try:
            async with httpx.AsyncClient(timeout=SLACK_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                # 私有文件需携带 bot_token 鉴权
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self.config.bot_token}"},
                )
                response.raise_for_status()
            # 检测 Slack 返回 HTML 登录页而非文件内容的情况
            if self._looks_like_html_download(response):
                raise ValueError("Slack returned HTML instead of file content")
            path.write_bytes(response.content)
            return str(path), marker
        except Exception as e:
            self.logger.warning("Failed to download file {}: {}", file_id, e)
            return None, self._download_failure_marker(marker_type, name, "download failed")

    @staticmethod
    def _download_failure_marker(marker_type: str, name: str, reason: str) -> str:
        """构建下载失败标记，提示用户检查权限。"""
        return (
            f"[{marker_type}: {name}: {reason}; not available to biscuitbot. "
            "Check Slack files:read scope, reinstall the Slack app, and ensure the bot can access the file.]"
        )

    @staticmethod
    def _looks_like_html_download(response: httpx.Response) -> bool:
        """检测响应是否为 HTML（而非文件内容），用于识别鉴权失败。"""
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            return True
        preview = response.content[:256].lstrip().lower()
        return preview.startswith(_HTML_DOWNLOAD_PREFIXES)

    async def _on_block_action(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        """处理内联操作按钮的点击事件。"""
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        payload = req.payload or {}
        actions = payload.get("actions") or []
        if not actions:
            return
        value = str(actions[0].get("value") or "")
        user_info = payload.get("user") or {}
        sender_id = str(user_info.get("id") or "")
        channel_info = payload.get("channel") or {}
        chat_id = str(channel_info.get("id") or "")
        if not sender_id or not chat_id or not value:
            return
        message_info = payload.get("message") or {}
        thread_ts = message_info.get("thread_ts") or message_info.get("ts")
        channel_type = self._infer_channel_type(chat_id)
        if not self._is_allowed(sender_id, chat_id, channel_type):
            return
        session_key = f"slack:{chat_id}:{thread_ts}" if thread_ts else None
        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=value,
                metadata={"slack": {"thread_ts": thread_ts, "channel_type": channel_type}},
                session_key=session_key,
            )
        except Exception:
            self.logger.exception("Error handling button click from {}", sender_id)

    async def _with_thread_context(
        self,
        text: str,
        *,
        chat_id: str,
        channel_type: str,
        thread_ts: str | None,
        raw_thread_ts: str | None,
        current_ts: str | None,
    ) -> str:
        """首次将机器人拉入 Slack 线程时，包含线程历史作为上下文。"""
        del channel_type  # DM 和频道线程都通过 conversations.replies 获取
        if (
            not self.config.include_thread_context
            or not self._web_client
            or not raw_thread_ts
            or not thread_ts
            or current_ts == thread_ts
        ):
            return text

        # 去重：每个线程仅拉取一次上下文
        key = f"{chat_id}:{thread_ts}"
        if key in self._thread_context_attempted:
            return text
        if len(self._thread_context_attempted) >= self._THREAD_CONTEXT_CACHE_LIMIT:
            self._thread_context_attempted.clear()
        self._thread_context_attempted.add(key)

        try:
            response = await self._web_client.conversations_replies(
                channel=chat_id,
                ts=thread_ts,
                limit=max(1, self.config.thread_context_limit),
            )
        except Exception as e:
            self.logger.warning("thread context unavailable for {}: {}", key, e)
            return text

        lines = self._format_thread_context(
            response.get("messages", []),
            current_ts=current_ts,
        )
        if not lines:
            return text
        return "Slack thread context before this mention:\n" + "\n".join(lines) + f"\n\nCurrent message:\n{text}"

    def _format_thread_context(self, messages: list[dict[str, Any]], *, current_ts: str | None) -> list[str]:
        """格式化线程历史消息为上下文行列表。"""
        lines: list[str] = []
        for item in messages:
            # 跳过当前消息
            if item.get("ts") == current_ts:
                continue
            # 跳过子类型消息（如 bot_message、文件分享等）
            if item.get("subtype"):
                continue
            sender = str(item.get("user") or item.get("bot_id") or "unknown")
            is_bot = self._bot_user_id is not None and sender == self._bot_user_id
            label = "bot" if is_bot else f"<@{sender}>"
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            text = self._strip_bot_mention(text)
            # 限制每条消息 500 字符
            if len(text) > 500:
                text = text[:500] + "…"
            lines.append(f"- {label}: {text}")
        return lines

    @staticmethod
    def _build_button_blocks(text: str, buttons: list[list[str]]) -> list[dict[str, Any]]:
        """构建带操作按钮的 Slack Block Kit 块。"""
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}},
        ]
        elements = []
        for row in buttons:
            for label in row:
                elements.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": label[:75]},
                    "value": label[:75],
                    "action_id": f"btn_{label[:50]}",
                })
        if elements:
            # Block Kit 限制 actions 块最多 25 个元素
            blocks.append({"type": "actions", "elements": elements[:25]})
        return blocks

    async def _update_react_emoji(self, chat_id: str, ts: str | None) -> None:
        """移除处理中表情，并可选添加完成表情。"""
        if not self._web_client or not ts:
            return
        try:
            await self._web_client.reactions_remove(
                channel=chat_id,
                name=self.config.react_emoji,
                timestamp=ts,
            )
        except Exception as e:
            self.logger.debug("reactions_remove failed: {}", e)
        if self.config.done_emoji:
            try:
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name=self.config.done_emoji,
                    timestamp=ts,
                )
            except Exception as e:
                self.logger.debug("done reaction failed: {}", e)

    def _is_allowed(self, sender_id: str, chat_id: str, channel_type: str) -> bool:
        """渠道感知的访问策略检查（DM 与群组分别处理）。"""
        if channel_type == "im":
            if not self.config.dm.enabled:
                return False
            if self.config.dm.policy == "allowlist":
                return sender_id in self.config.dm.allow_from or is_approved(self.name, sender_id)
            return True

        # 群组/频道消息
        if self.config.group_policy == "allowlist":
            return chat_id in self.config.group_allow_from
        return True

    def _is_mention(self, event_type: str, text: str) -> bool:
        """判断事件是否为对机器人的提及。"""
        if event_type == "app_mention":
            return True
        return self._bot_user_id is not None and f"<@{self._bot_user_id}>" in text

    def _should_respond_in_channel(self, event_type: str, text: str, chat_id: str) -> bool:
        """根据群组策略决定是否在频道中响应。"""
        if self.config.group_policy == "open":
            return True
        if self.config.group_policy == "mention":
            return self._is_mention(event_type, text)
        if self.config.group_policy == "allowlist":
            if chat_id not in self.config.group_allow_from:
                return False
            # allowlist 策略下可选要求提及
            if self.config.group_require_mention:
                return self._is_mention(event_type, text)
            return True
        return False

    def is_allowed(self, sender_id: str) -> bool:
        """基类接口实现：Slack 需要渠道感知的策略检查，
        因此 _on_socket_request 和 _on_block_action 在交给 BaseChannel 前调用 _is_allowed。
        """
        return True

    @staticmethod
    def _infer_channel_type(chat_id: str) -> str:
        """根据 chat_id 前缀推断频道类型。"""
        if chat_id.startswith("D"):
            return "im"
        if chat_id.startswith("G"):
            return "group"
        return "channel"

    def _strip_bot_mention(self, text: str) -> str:
        """从文本中剥离机器人提及标记。"""
        if not text or not self._bot_user_id:
            return text
        return re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

    # Markdown 转 Slack mrkdwn 的正则与处理
    _TABLE_RE = re.compile(r"(?m)^\|.*\|$(?:\n\|[\s:|-]*\|$)(?:\n\|.*\|$)*")  # 管道表格
    _CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")  # 围栏代码块
    _INLINE_CODE_RE = re.compile(r"`[^`]+`")  # 行内代码
    _LEFTOVER_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")  # 残留的 ** 粗体
    _LEFTOVER_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)  # 残留的 ATX 标题
    _BARE_URL_RE = re.compile(r"(?<![|<])(https?://\S+)")  # 裸 URL

    @classmethod
    def _to_mrkdwn(cls, text: str) -> str:
        """将 Markdown 转换为 Slack mrkdwn（含表格转换）。"""
        if not text:
            return ""
        # 先转换表格为列表（Slack 不原生支持表格）
        text = cls._TABLE_RE.sub(cls._convert_table, text)
        return cls._fixup_mrkdwn(slackify_markdown(text)).rstrip("\n")

    @classmethod
    def _fixup_mrkdwn(cls, text: str) -> str:
        """修正 slackify_markdown 遗漏的 Markdown 残留。"""
        code_blocks: list[str] = []

        def _save_code(m: re.Match) -> str:
            # 用占位符保护代码块，避免被后续修正误伤
            code_blocks.append(m.group(0))
            return f"\x00CB{len(code_blocks) - 1}\x00"

        text = cls._CODE_FENCE_RE.sub(_save_code, text)
        text = cls._INLINE_CODE_RE.sub(_save_code, text)
        # ** 粗体 → * 粗体（Slack mrkdwn 使用单 *）
        text = cls._LEFTOVER_BOLD_RE.sub(r"*\1*", text)
        # 标题 → 粗体
        text = cls._LEFTOVER_HEADER_RE.sub(r"*\1*", text)
        # 还原裸 URL 中的 &amp; 实体
        text = cls._BARE_URL_RE.sub(lambda m: m.group(0).replace("&amp;", "&"), text)

        # 恢复代码块占位符
        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x00CB{i}\x00", block)
        return text

    @staticmethod
    def _convert_table(match: re.Match) -> str:
        """将 Markdown 表格转换为 Slack 可读的列表格式。"""
        lines = [ln.strip() for ln in match.group(0).strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return match.group(0)
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        # 跳过分隔行（如 |---|---|）
        start = 2 if re.fullmatch(r"[|\s:\-]+", lines[1]) else 1
        rows: list[str] = []
        for line in lines[start:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            # 补齐列数
            cells = (cells + [""] * len(headers))[: len(headers)]
            parts = [f"**{headers[i]}**: {cells[i]}" for i in range(len(headers)) if cells[i]]
            if parts:
                rows.append(" · ".join(parts))
        return "\n".join(rows)
