"""企业微信（WeCom）渠道实现，基于 wecom_aibot_sdk 的 WebSocket 长连接。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的企业微信平台组件。
在项目架构中起到的作用：将企业微信 AI 机器人的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：使用 WebSocket 长连接接收事件，无需公网 IP 或 Webhook 配置。
- 鉴权：通过企业微信 AI 机器人平台的 Bot ID 和 Secret 进行身份认证。
- 消息类型：支持文本、图片、语音、文件、混合内容五种消息类型。
- 媒体处理：入站媒体通过 AES 解密下载，出站媒体通过 WebSocket 三步分块上传（base64）。
- 语音消息：企业微信已内置语音转文字，直接使用转写内容。
- 欢迎消息：支持用户进入聊天时自动发送欢迎语。
- 无限重连：断线后自动重连，心跳间隔 30 秒。
"""

import asyncio  # 异步事件循环与并发原语
import base64  # base64 编码（媒体分块上传）
import hashlib  # MD5 哈希（文件完整性校验）
import importlib.util  # 运行时检测 SDK 是否安装
import os  # 文件路径处理
import re  # 正则表达式（文件名安全化）
from collections import OrderedDict  # 有序去重缓存（消息 ID 去重）
from pathlib import Path  # 路径处理
from typing import Any  # 类型注解支持

from pydantic import Field  # Pydantic 模型字段定义

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_media_dir  # 媒体文件目录
from biscuitbot.config.schema import Base  # 配置模型基类

WECOM_AVAILABLE = importlib.util.find_spec("wecom_aibot_sdk") is not None  # 检测 wecom_aibot_sdk 是否已安装

# 上传安全限制（与 QQ 渠道默认值一致）
WECOM_UPLOAD_MAX_BYTES = 1024 * 1024 * 200  # 200MB

# 将不安全字符替换为 "_"，保留中文和常见安全标点
_SAFE_NAME_RE = re.compile(r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE)


def _sanitize_filename(name: str) -> str:
    """安全化文件名，避免路径遍历和问题字符。"""
    name = (name or "").strip()
    name = Path(name).name
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    return name


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}  # 图片扩展名集合
_VIDEO_EXTS = {".mp4", ".avi", ".mov"}  # 视频扩展名集合
_AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg"}  # 音频扩展名集合


def _guess_wecom_media_type(filename: str) -> str:
    """根据文件扩展名分类为企业微信的 media_type 字符串。"""
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "voice"
    return "file"

class WecomConfig(Base):
    """企业微信（WeCom）AI 机器人渠道配置。"""

    enabled: bool = False
    bot_id: str = ""  # 企业微信 AI 机器人 ID
    secret: str = ""  # 企业微信 AI 机器人密钥
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    welcome_message: str = ""  # 用户进入聊天时的欢迎消息


# 消息类型到展示文本的映射（非文本消息在入站时转换为占位符文本）
MSG_TYPE_MAP = {
    "image": "[image]",
    "voice": "[voice]",
    "file": "[file]",
    "mixed": "[mixed content]",
}


class WecomChannel(BaseChannel):
    """企业微信（WeCom）渠道，使用 WebSocket 长连接。

    通过 WebSocket 接收事件 —— 无需公网 IP 或 Webhook。

    前置条件：
    - 企业微信 AI 机器人平台的 Bot ID 和 Secret
    """

    name = "wecom"
    display_name = "企业微信"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return WecomConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WecomConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WecomConfig = config
        self._client: Any = None  # wecom_aibot_sdk 客户端
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # 有序去重缓存（消息 ID）
        self._loop: asyncio.AbstractEventLoop | None = None  # 主事件循环引用
        self._generate_req_id = None  # 请求 ID 生成函数
        # 存储各会话的帧头信息，用于回复消息
        self._chat_frames: dict[str, Any] = {}

    async def start(self) -> None:
        """启动企业微信机器人，建立 WebSocket 长连接。"""
        if not WECOM_AVAILABLE:
            self.logger.error("SDK not installed. Run: pip install biscuitbot[wecom]")
            return

        if not self.config.bot_id or not self.config.secret:
            self.logger.error("bot_id and secret not configured")
            return

        from wecom_aibot_sdk import WSClient, generate_req_id

        self._running = True
        self._loop = asyncio.get_running_loop()
        self._generate_req_id = generate_req_id

        # 创建 WebSocket 客户端
        self._client = WSClient({
            "bot_id": self.config.bot_id,
            "secret": self.config.secret,
            "reconnect_interval": 1000,
            "max_reconnect_attempts": -1,  # 无限重连
            "heartbeat_interval": 30000,
        })

        # 注册事件处理器
        self._client.on("connected", self._on_connected)
        self._client.on("authenticated", self._on_authenticated)
        self._client.on("disconnected", self._on_disconnected)
        self._client.on("error", self._on_error)
        self._client.on("message.text", self._on_text_message)
        self._client.on("message.image", self._on_image_message)
        self._client.on("message.voice", self._on_voice_message)
        self._client.on("message.file", self._on_file_message)
        self._client.on("message.mixed", self._on_mixed_message)
        self._client.on("event.enter_chat", self._on_enter_chat)

        self.logger.info("bot starting with WebSocket long connection")
        self.logger.info("No public IP required - using WebSocket to receive events")

        # 建立连接
        await self._client.connect_async()

        # 保持运行直到被停止
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止企业微信机器人。"""
        self._running = False
        if self._client:
            await self._client.disconnect()
        self.logger.info("bot stopped")

    async def _on_connected(self, frame: Any) -> None:
        """处理 WebSocket 连接成功事件。"""
        self.logger.info("WebSocket connected")

    async def _on_authenticated(self, frame: Any) -> None:
        """处理认证成功事件。"""
        self.logger.info("authenticated successfully")

    async def _on_disconnected(self, frame: Any) -> None:
        """处理 WebSocket 断开事件。"""
        reason = frame.body if hasattr(frame, 'body') else str(frame)
        self.logger.warning("WebSocket disconnected: {}", reason)

    async def _on_error(self, frame: Any) -> None:
        """处理错误事件。"""
        self.logger.error("error: {}", frame)

    async def _on_text_message(self, frame: Any) -> None:
        """处理文本消息。"""
        await self._process_message(frame, "text")

    async def _on_image_message(self, frame: Any) -> None:
        """处理图片消息。"""
        await self._process_message(frame, "image")

    async def _on_voice_message(self, frame: Any) -> None:
        """处理语音消息。"""
        await self._process_message(frame, "voice")

    async def _on_file_message(self, frame: Any) -> None:
        """处理文件消息。"""
        await self._process_message(frame, "file")

    async def _on_mixed_message(self, frame: Any) -> None:
        """处理混合内容消息。"""
        await self._process_message(frame, "mixed")

    async def _on_enter_chat(self, frame: Any) -> None:
        """处理用户进入聊天事件（用户打开与机器人的对话）。"""
        try:
            # 从 WsFrame 数据类或字典中提取 body
            if hasattr(frame, 'body'):
                body = frame.body or {}
            elif isinstance(frame, dict):
                body = frame.get("body", frame)
            else:
                body = {}

            chat_id = body.get("chatid", "") if isinstance(body, dict) else ""

            if chat_id and not self.is_allowed(chat_id):
                return

            if chat_id and self.config.welcome_message:
                await self._client.reply_welcome(frame, {
                    "msgtype": "text",
                    "text": {"content": self.config.welcome_message},
                })
        except Exception:
            self.logger.exception("Error handling enter_chat")

    async def _process_message(self, frame: Any, msg_type: str) -> None:
        """处理入站消息并转发到消息总线。"""
        try:
            # Extract body from WsFrame dataclass or dict
            if hasattr(frame, 'body'):
                body = frame.body or {}
            elif isinstance(frame, dict):
                body = frame.get("body", frame)
            else:
                body = {}

            # Ensure body is a dict
            if not isinstance(body, dict):
                self.logger.warning("Invalid body type: {}", type(body))
                return

            # Extract message info
            msg_id = body.get("msgid", "")
            if not msg_id:
                msg_id = f"{body.get('chatid', '')}_{body.get('sendertime', '')}"

            # Extract sender info from "from" field (SDK format)
            from_info = body.get("from", {})
            sender_id = from_info.get("userid", "unknown") if isinstance(from_info, dict) else "unknown"
            if not self.is_allowed(sender_id):
                return

            # Deduplication check
            if msg_id in self._processed_message_ids:
                return
            self._processed_message_ids[msg_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # For single chat, chatid is the sender's userid
            # For group chat, chatid is provided in body
            chat_type = body.get("chattype", "single")
            chat_id = body.get("chatid", sender_id)

            content_parts = []
            media_paths: list[str] = []

            if msg_type == "text":
                text = body.get("text", {}).get("content", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "image":
                image_info = body.get("image", {})
                file_url = image_info.get("url", "")
                aes_key = image_info.get("aeskey", "")

                if file_url and aes_key:
                    file_path = await self._download_and_save_media(file_url, aes_key, "image")
                    if file_path:
                        filename = os.path.basename(file_path)
                        content_parts.append(f"[image: {filename}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append("[image: download failed]")
                else:
                    content_parts.append("[image: download failed]")

            elif msg_type == "voice":
                voice_info = body.get("voice", {})
                # Voice message already contains transcribed content from WeCom
                voice_content = voice_info.get("content", "")
                if voice_content:
                    content_parts.append(f"[voice] {voice_content}")
                else:
                    content_parts.append("[voice]")

            elif msg_type == "file":
                file_info = body.get("file", {})
                file_url = file_info.get("url", "")
                aes_key = file_info.get("aeskey", "")
                file_name = file_info.get("name") or None

                if file_url and aes_key:
                    file_path = await self._download_and_save_media(file_url, aes_key, "file", file_name)
                    if file_path:
                        display_name = os.path.basename(file_path)
                        content_parts.append(f"[file: {display_name}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append(f"[file: {file_name or 'unknown'}: download failed]")
                else:
                    content_parts.append(f"[file: {file_name or 'unknown'}: download failed]")

            elif msg_type == "mixed":
                # Mixed content contains multiple message items
                msg_items = body.get("mixed", {}).get("msg_item", [])
                for item in msg_items:
                    item_type = item.get("msgtype", "")
                    if item_type == "text":
                        text = item.get("text", {}).get("content", "")
                        if text:
                            content_parts.append(text)
                    elif item_type == "image":
                        file_url = item.get("image", {}).get("url", "")
                        aes_key = item.get("image", {}).get("aeskey", "")
                        if file_url and aes_key:
                            file_path = await self._download_and_save_media(file_url, aes_key, "image")
                            if file_path:
                                filename = os.path.basename(file_path)
                                content_parts.append(f"[image: {filename}]")
                                media_paths.append(file_path)
                    else:
                        content_parts.append(MSG_TYPE_MAP.get(item_type, f"[{item_type}]"))

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            if not content:
                return

            # Store frame for this chat to enable replies
            self._chat_frames[chat_id] = frame

            # Forward to message bus
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media_paths or None,
                metadata={
                    "message_id": msg_id,
                    "msg_type": msg_type,
                    "chat_type": chat_type,
                }
            )

        except Exception:
            self.logger.exception("Error processing message")

    async def _download_and_save_media(
        self,
        file_url: str,
        aes_key: str,
        media_type: str,
        filename: str | None = None,
    ) -> str | None:
        """从企业微信下载并解密媒体文件。

        Returns:
            file_path 或下载失败时返回 None
        """
        try:
            data, fname = await self._client.download_file(file_url, aes_key)

            if not data:
                self.logger.warning("Failed to download media")
                return None

            if len(data) > WECOM_UPLOAD_MAX_BYTES:
                self.logger.warning(
                    "inbound media too large: {} bytes (max {})",
                    len(data),
                    WECOM_UPLOAD_MAX_BYTES,
                )
                return None

            media_dir = get_media_dir("wecom")
            if not filename:
                filename = fname or f"{media_type}_{hash(file_url) % 100000}"
            filename = _sanitize_filename(filename)

            file_path = media_dir / filename
            await asyncio.to_thread(file_path.write_bytes, data)
            self.logger.debug("Downloaded {} to {}", media_type, file_path)
            return str(file_path)

        except Exception:
            self.logger.exception("Error downloading media")
            return None

    async def _upload_media_ws(
        self, client: Any, file_path: str,
    ) -> "tuple[str, str] | tuple[None, None]":
        """Upload a local file to WeCom via WebSocket 3-step protocol (base64).

        Uses the WeCom WebSocket upload commands directly via
        ``client._ws_manager.send_reply()``:

          ``aibot_upload_media_init``   → upload_id
          ``aibot_upload_media_chunk`` × N  (≤512 KB raw per chunk, base64)
          ``aibot_upload_media_finish`` → media_id

        Returns (media_id, media_type) on success, (None, None) on failure.
        """
        from wecom_aibot_sdk.utils import generate_req_id as _gen_req_id

        try:
            fname = os.path.basename(file_path)
            media_type = _guess_wecom_media_type(fname)

            # Read file size and data in a thread to avoid blocking the event loop
            def _read_file():
                file_size = os.path.getsize(file_path)
                if file_size > WECOM_UPLOAD_MAX_BYTES:
                    raise ValueError(
                        f"File too large: {file_size} bytes (max {WECOM_UPLOAD_MAX_BYTES})"
                    )
                with open(file_path, "rb") as f:
                    return file_size, f.read()

            file_size, data = await asyncio.to_thread(_read_file)
            # MD5 is used for file integrity only, not cryptographic security
            md5_hash = hashlib.md5(data).hexdigest()

            chunk_size = 512 * 1024  # 512 KB raw (before base64)
            mv = memoryview(data)
            chunk_list = [bytes(mv[i : i + chunk_size]) for i in range(0, file_size, chunk_size)]
            n_chunks = len(chunk_list)
            del mv, data

            # Step 1: init
            req_id = _gen_req_id("upload_init")
            resp = await client._ws_manager.send_reply(req_id, {
                "type": media_type,
                "filename": fname,
                "total_size": file_size,
                "total_chunks": n_chunks,
                "md5": md5_hash,
            }, "aibot_upload_media_init")
            if resp.errcode != 0:
                self.logger.warning("upload init failed ({}): {}", resp.errcode, resp.errmsg)
                return None, None
            upload_id = resp.body.get("upload_id") if resp.body else None
            if not upload_id:
                self.logger.warning("upload init: no upload_id in response")
                return None, None

            # Step 2: send chunks
            for i, chunk in enumerate(chunk_list):
                req_id = _gen_req_id("upload_chunk")
                resp = await client._ws_manager.send_reply(req_id, {
                    "upload_id": upload_id,
                    "chunk_index": i,
                    "base64_data": base64.b64encode(chunk).decode(),
                }, "aibot_upload_media_chunk")
                if resp.errcode != 0:
                    self.logger.warning("upload chunk {} failed ({}): {}", i, resp.errcode, resp.errmsg)
                    return None, None

            # Step 3: finish
            req_id = _gen_req_id("upload_finish")
            resp = await client._ws_manager.send_reply(req_id, {
                "upload_id": upload_id,
            }, "aibot_upload_media_finish")
            if resp.errcode != 0:
                self.logger.warning("upload finish failed ({}): {}", resp.errcode, resp.errmsg)
                return None, None

            media_id = resp.body.get("media_id") if resp.body else None
            if not media_id:
                self.logger.warning("upload finish: no media_id in response body={}", resp.body)
                return None, None

            suffix = "..." if len(media_id) > 16 else ""
            self.logger.debug("uploaded {} ({}) → media_id={}", fname, media_type, media_id[:16] + suffix)
            return media_id, media_type

        except ValueError as e:
            self.logger.warning("upload skipped for {}: {}", file_path, e)
            return None, None
        except Exception:
            self.logger.exception("_upload_media_ws error for {}", file_path)
            return None, None

    async def send(self, msg: OutboundMessage) -> None:
        """通过企业微信发送消息。"""
        if not self._client:
            self.logger.warning("client not initialized")
            return

        try:
            content = (msg.content or "").strip()
            is_progress = bool(msg.metadata.get("_progress"))

            # Get the stored frame for this chat
            frame = self._chat_frames.get(msg.chat_id)

            # Send media files via WebSocket upload
            for file_path in msg.media or []:
                if not os.path.isfile(file_path):
                    self.logger.warning("media file not found: {}", file_path)
                    continue
                media_id, media_type = await self._upload_media_ws(self._client, file_path)
                if media_id:
                    if frame:
                        await self._client.reply(frame, {
                            "msgtype": media_type,
                            media_type: {"media_id": media_id},
                        })
                    else:
                        await self._client.send_message(msg.chat_id, {
                            "msgtype": media_type,
                            media_type: {"media_id": media_id},
                        })
                    self.logger.debug("sent {} → {}", media_type, msg.chat_id)
                else:
                    content += f"\n[file upload failed: {os.path.basename(file_path)}]"

            if not content:
                return

            if frame:
                # Both progress and final messages must use reply_stream (cmd="aibot_respond_msg").
                # The plain reply() uses cmd="reply" which does not support "text" msgtype
                # and causes errcode=40008 from WeCom API.
                stream_id = self._generate_req_id("stream")
                await self._client.reply_stream(
                    frame,
                    stream_id,
                    content,
                    finish=not is_progress,
                )
                self.logger.debug(
                    "{} sent to {}",
                    "progress" if is_progress else "message",
                    msg.chat_id,
                )
            else:
                # No frame (e.g. cron push): proactive send only supports markdown
                await self._client.send_message(msg.chat_id, {
                    "msgtype": "markdown",
                    "markdown": {"content": content},
                })
                self.logger.info("proactive send to {}", msg.chat_id)

        except Exception:
            self.logger.exception("Error sending message to chat_id={}", msg.chat_id)
