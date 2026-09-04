"""钉钉（DingTalk/DingDing）渠道实现，基于 Stream Mode（长连接流模式）。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的钉钉平台组件。
在项目架构中起到的作用：将钉钉机器人的收发消息能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：通过 ``dingtalk-stream`` SDK 以 WebSocket 长连接（Stream Mode）接收事件，
  无需公网回调地址，部署在内网也可使用。
- 收发分离：入站消息由 Stream SDK 推送；出站消息通过钉钉开放平台 HTTP API 发送
  （SDK 主要用于接收，发送走直接 HTTP 调用）。
- 鉴权：使用 ``client_id`` / ``client_secret`` 换取 Access Token 后调用发送接口。
- 会话类型：支持单聊（private）与群聊（group）；群聊 chat_id 以 ``group:`` 前缀存储，
  以便回复时路由回对应群会话。
- 媒体：支持图片/文件/语音/视频的上传与下载，并内置 SSRF 校验、重定向控制与体积限制。
"""

import asyncio
import json
import mimetypes
import os
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx  # 异步 HTTP 客户端，用于调用钉钉开放平台 API 与下载媒体
from pydantic import Field

from biscuitbot.bus.events import OutboundMessage
from biscuitbot.bus.queue import MessageBus
from biscuitbot.channels.base import BaseChannel
from biscuitbot.config.schema import Base
from biscuitbot.security.network import validate_resolved_url, validate_url_target  # SSRF 防护校验
from biscuitbot.utils.helpers import safe_filename  # 文件名安全化工具（防路径遍历）

DINGTALK_MAX_REMOTE_MEDIA_BYTES = 20 * 1024 * 1024  # 远程媒体下载体积上限（20MB）
DINGTALK_MAX_REMOTE_MEDIA_REDIRECTS = 3  # 远程媒体下载最大重定向次数

try:
    from dingtalk_stream import (  # 钉钉 Stream SDK，可选依赖
        AckMessage,
        CallbackHandler,
        CallbackMessage,
        Credential,
        DingTalkStreamClient,
    )
    from dingtalk_stream.chatbot import ChatbotMessage

    DINGTALK_AVAILABLE = True  # SDK 可用标志
except ImportError:
    DINGTALK_AVAILABLE = False  # SDK 未安装时降级，避免模块级导入崩溃
    # 回退占位符，保证类定义在模块级别不会因缺少 SDK 而崩溃
    CallbackHandler = object  # type: ignore[assignment,misc]
    CallbackMessage = None  # type: ignore[assignment,misc]
    AckMessage = None  # type: ignore[assignment,misc]
    ChatbotMessage = None  # type: ignore[assignment,misc]


class BiscuitbotDingTalkHandler(CallbackHandler):
    """钉钉 Stream SDK 标准回调处理器。

    解析入站消息并将其转发给 Biscuitbot 的钉钉渠道（``DingTalkChannel``）处理。
    """

    def __init__(self, channel: "DingTalkChannel"):
        """绑定所属渠道实例，以便回调时调用渠道方法。"""
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """处理一条入站流消息：解析文本/图片/文件/富文本，并转发给渠道。"""
        try:
            # 优先使用 SDK 的 ChatbotMessage 进行稳健解析
            chatbot_msg = ChatbotMessage.from_dict(message.data)

            # 提取文本内容；若 SDK 对象为空则回退到原始字典
            content = ""
            if chatbot_msg.text:
                content = chatbot_msg.text.content.strip()
            elif chatbot_msg.extensions.get("content", {}).get("recognition"):
                content = chatbot_msg.extensions["content"]["recognition"].strip()
            if not content:
                content = message.data.get("text", {}).get("content", "").strip()

            # 处理文件/图片类消息
            file_paths = []
            if chatbot_msg.message_type == "picture" and chatbot_msg.image_content:
                download_code = chatbot_msg.image_content.download_code
                if download_code:
                    sender_uid = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown"
                    fp = await self.channel._download_dingtalk_file(download_code, "image.jpg", sender_uid)
                    if fp:
                        file_paths.append(fp)
                        content = content or "[Image]"

            elif chatbot_msg.message_type == "file":
                download_code = message.data.get("content", {}).get("downloadCode") or message.data.get("downloadCode")
                fname = message.data.get("content", {}).get("fileName") or message.data.get("fileName") or "file"
                if download_code:
                    sender_uid = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown"
                    fp = await self.channel._download_dingtalk_file(download_code, fname, sender_uid)
                    if fp:
                        file_paths.append(fp)
                        content = content or "[File]"

            elif chatbot_msg.message_type == "richText" and chatbot_msg.rich_text_content:
                rich_list = chatbot_msg.rich_text_content.rich_text_list or []
                for item in rich_list:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        t = item.get("text", "").strip()
                        if t:
                            content = (content + " " + t).strip() if content else t
                    elif item.get("downloadCode"):
                        dc = item["downloadCode"]
                        fname = item.get("fileName") or "file"
                        sender_uid = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or "unknown"
                        fp = await self.channel._download_dingtalk_file(dc, fname, sender_uid)
                        if fp:
                            file_paths.append(fp)
                            content = content or "[File]"

            if file_paths:
                file_list = "\n".join("- " + p for p in file_paths)
                content = content + "\n\nReceived files:\n" + file_list

            if not content:
                self.channel.logger.warning(
                    "Received empty or unsupported message type: {}",
                    chatbot_msg.message_type,
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"

            conversation_type = message.data.get("conversationType")
            conversation_id = (
                message.data.get("conversationId")
                or message.data.get("openConversationId")
            )

            self.channel.logger.info("Received message from {} ({}): {}", sender_name, sender_id, content)

            # 通过 _on_message 转发给 Biscuitbot（非阻塞）。
            # 保存任务引用以防止其在完成前被 GC 回收。
            task = asyncio.create_task(
                self.channel._on_message(
                    content,
                    sender_id,
                    sender_name,
                    conversation_type,
                    conversation_id,
                )
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception:
            self.channel.logger.exception("Error processing message")
            # 返回 OK 以避免钉钉服务端反复重试
            return AckMessage.STATUS_OK, "Error"


class DingTalkConfig(Base):
    """钉钉渠道配置（Stream 模式）。"""

    enabled: bool = False
    client_id: str = ""  # 钉钉应用 AppKey
    client_secret: str = ""  # 钉钉应用 AppSecret
    allow_from: list[str] = Field(default_factory=list)  # 允许使用的发送者白名单
    allow_all: bool = False  # 静默放行：为 True 时所有用户可直接私聊，无需白名单或配对码
    allow_remote_media_redirects: bool = False  # 是否允许媒体下载跟随重定向
    remote_media_redirect_allowed_hosts: list[str] = Field(default_factory=list)  # 允许重定向的目标主机白名单
    group_user_isolation: bool = False  # 为 True 时，群聊中每个用户拥有独立会话


class DingTalkChannel(BaseChannel):
    """钉钉渠道（基于 Stream Mode）。

    使用 WebSocket（经 ``dingtalk-stream`` SDK）接收事件，使用直接 HTTP API 发送消息
    （SDK 主要用于接收）。支持单聊与群聊：群聊 chat_id 以 ``group:`` 前缀存储，便于回复路由。
    """

    name = "dingtalk"
    display_name = "钉钉"
    requires_module = "dingtalk_stream"  # 必需 SDK 模块（缺失时自动安装）
    pip_requires = ["dingtalk-stream>=0.24.0"]
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}  # 图片扩展名集合
    _AUDIO_EXTS = {".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac"}  # 音频扩展名集合
    _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}  # 视频扩展名集合
    _ZIP_BEFORE_UPLOAD_EXTS = {".htm", ".html"}  # 钉钉不接受原始 HTML，需先打包为 zip

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置（用于 onboarding 自动填充 config.json）。"""
        return DingTalkConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        """初始化钉钉渠道：解析配置并准备运行时状态。"""
        if isinstance(config, dict):
            config = DingTalkConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: DingTalkConfig = config
        self._client: Any = None  # dingtalk-stream 客户端实例
        self._http: httpx.AsyncClient | None = None  # 共享异步 HTTP 客户端

        # 发送消息所需的 Access Token 管理
        self._access_token: str | None = None
        self._token_expiry: float = 0  # token 过期时间戳

        # 持有后台任务引用，防止被 GC 回收
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """以 Stream Mode 启动钉钉机器人，并在断连时自动重连。"""
        try:
            if not DINGTALK_AVAILABLE:
                self.logger.error(
                    "Stream SDK not installed. Run: pip install dingtalk-stream"
                )
                return

            if not self.config.client_id or not self.config.client_secret:
                self.logger.error("client_id and client_secret not configured")
                return

            self._running = True
            self._http = httpx.AsyncClient()

            self.logger.info(
                "Initializing Stream Client with Client ID: {}...",
                self.config.client_id,
            )
            credential = Credential(self.config.client_id, self.config.client_secret)
            self._client = DingTalkStreamClient(credential)

            # 注册标准回调处理器
            handler = BiscuitbotDingTalkHandler(self)
            self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

            self.logger.info("bot started with Stream Mode")

            # 重连循环：SDK 退出或崩溃时重启流连接
            while self._running:
                try:
                    await self._client.start()
                except Exception as e:
                    self.logger.warning("stream error: {}", e)
                if self._running:
                    self.logger.info("Reconnecting stream in 5 seconds...")
                    await asyncio.sleep(5)

        except Exception:
            self.logger.exception("Failed to start channel")

    async def stop(self) -> None:
        """停止钉钉机器人，关闭 HTTP 客户端并取消未完成的后台任务。"""
        self._running = False
        # 关闭共享 HTTP 客户端
        if self._http:
            await self._http.aclose()
            self._http = None
        # 取消未完成的后台任务
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

    async def _get_access_token(self) -> str | None:
        """获取或刷新 Access Token，带本地缓存与提前过期保护。"""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            self.logger.warning("HTTP client not initialized, cannot refresh token")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # 提前 60 秒过期，留出安全余量
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception:
            self.logger.exception("Failed to get access token")
            return None

    @staticmethod
    def _is_http_url(value: str) -> bool:
        """判断给定字符串是否为 http/https URL。"""
        return urlparse(value).scheme in ("http", "https")

    def _guess_upload_type(self, media_ref: str) -> str:
        """根据扩展名推断媒体上传类型（image/voice/video/file）。"""
        ext = Path(urlparse(media_ref).path).suffix.lower()
        if ext in self._IMAGE_EXTS:
            return "image"
        if ext in self._AUDIO_EXTS:
            return "voice"
        if ext in self._VIDEO_EXTS:
            return "video"
        return "file"

    def _guess_filename(self, media_ref: str, upload_type: str) -> str:
        """从媒体引用推断文件名，无法推断时按类型给出默认名。"""
        name = os.path.basename(urlparse(media_ref).path)
        return name or {"image": "image.jpg", "voice": "audio.amr", "video": "video.mp4"}.get(upload_type, "file.bin")

    @staticmethod
    def _zip_bytes(filename: str, data: bytes) -> tuple[bytes, str, str]:
        """将原始字节打包为 zip，返回 (zip 数据, zip 文件名, content_type)。"""
        stem = Path(filename).stem or "attachment"
        safe_name = filename or "attachment.bin"
        zip_name = f"{stem}.zip"
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(safe_name, data)
        return buffer.getvalue(), zip_name, "application/zip"

    def _normalize_upload_payload(
        self,
        filename: str,
        data: bytes,
        content_type: str | None,
    ) -> tuple[bytes, str, str | None]:
        """归一化上传内容：HTML 类附件需先打包为 zip，其余原样返回。"""
        ext = Path(filename).suffix.lower()
        if ext in self._ZIP_BEFORE_UPLOAD_EXTS or content_type == "text/html":
            self.logger.info(
                "DingTalk does not accept raw HTML attachments, zipping {} before upload",
                filename,
            )
            return self._zip_bytes(filename, data)
        return data, filename, content_type

    def _validate_remote_media_url(self, media_ref: str) -> bool:
        """校验远程媒体 URL 是否符合 SSRF 防护策略。"""
        ok, err = validate_url_target(media_ref)
        if not ok:
            self.logger.warning("remote media URL blocked ref={} reason={}", media_ref, err)
            return False
        return True

    def _redirect_host_allowed(self, current_url: str, next_url: str) -> bool:
        """判断重定向目标主机是否被允许（同主机或在白名单中）。"""
        current_host = (urlparse(current_url).hostname or "").lower()
        next_host = (urlparse(next_url).hostname or "").lower()
        if not next_host:
            return False
        if next_host == current_host:
            return True
        allowed_hosts = {host.lower() for host in self.config.remote_media_redirect_allowed_hosts}
        return next_host in allowed_hosts

    def _next_remote_media_url(self, current_url: str, location: str | None) -> str | None:
        """计算重定向后的下一个 URL，校验主机与 SSRF 策略，不通过则返回 None。"""
        if not self.config.allow_remote_media_redirects:
            self.logger.warning("media download redirect refused ref={}", current_url)
            return None
        if not location:
            self.logger.warning("media download redirect without Location ref={}", current_url)
            return None
        next_url = urljoin(current_url, location)
        if not self._redirect_host_allowed(current_url, next_url):
            self.logger.warning(
                "media download cross-host redirect refused ref={} next={}",
                current_url,
                next_url,
            )
            return None
        if not self._validate_remote_media_url(next_url):
            return None
        return next_url

    async def _fetch_remote_media_bytes(
        self,
        media_ref: str,
    ) -> tuple[bytes | None, str | None]:
        """下载远程媒体 URL，内置 SSRF、重定向与体积校验。"""
        if not self._http:
            return None, None

        if not self._validate_remote_media_url(media_ref):
            return None, None

        try:
            # 优先以流式方式下载并累计字节数上限，避免大响应在限额生效前被完整加载。
            # 测试桩可能只实现 get()，因此下方保留一个小的兼容回退。
            stream = getattr(self._http, "stream", None)
            if stream is not None:
                current_url = media_ref
                for _ in range(DINGTALK_MAX_REMOTE_MEDIA_REDIRECTS + 1):
                    async with stream("GET", current_url, follow_redirects=False) as resp:
                        final_ok, final_err = validate_resolved_url(str(resp.url))
                        if not final_ok:
                            self.logger.warning(
                                "remote media redirect blocked ref={} final={} reason={}",
                                media_ref,
                                resp.url,
                                final_err,
                            )
                            return None, None
                        if 300 <= resp.status_code < 400:
                            next_url = self._next_remote_media_url(
                                str(resp.url), resp.headers.get("location")
                            )
                            if not next_url:
                                return None, None
                            current_url = next_url
                            continue
                        if resp.status_code >= 400:
                            self.logger.warning(
                                "media download failed status={} ref={}",
                                resp.status_code,
                                current_url,
                            )
                            return None, None
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > DINGTALK_MAX_REMOTE_MEDIA_BYTES:
                                self.logger.warning(
                                    "media download too large ref={} bytes>{}",
                                    current_url,
                                    DINGTALK_MAX_REMOTE_MEDIA_BYTES,
                                )
                                return None, None
                            chunks.append(chunk)
                        return b"".join(chunks), (resp.headers.get("content-type") or "")
                self.logger.warning("media download exceeded redirect limit ref={}", media_ref)
                return None, None

            current_url = media_ref
            for _ in range(DINGTALK_MAX_REMOTE_MEDIA_REDIRECTS + 1):
                resp = await self._http.get(current_url, follow_redirects=False)
                final_ok, final_err = validate_resolved_url(str(getattr(resp, "url", current_url)))
                if not final_ok:
                    self.logger.warning(
                        "remote media redirect blocked ref={} final={} reason={}",
                        media_ref,
                        getattr(resp, "url", current_url),
                        final_err,
                    )
                    return None, None
                if 300 <= resp.status_code < 400:
                    next_url = self._next_remote_media_url(
                        str(getattr(resp, "url", current_url)), resp.headers.get("location")
                    )
                    if not next_url:
                        return None, None
                    current_url = next_url
                    continue
                if resp.status_code >= 400:
                    self.logger.warning(
                        "media download failed status={} ref={}",
                        resp.status_code,
                        current_url,
                    )
                    return None, None
                if len(resp.content) > DINGTALK_MAX_REMOTE_MEDIA_BYTES:
                    self.logger.warning(
                        "media download too large ref={} bytes>{}",
                        current_url,
                        DINGTALK_MAX_REMOTE_MEDIA_BYTES,
                    )
                    return None, None
                return resp.content, (resp.headers.get("content-type") or "")
            self.logger.warning("media download exceeded redirect limit ref={}", media_ref)
            return None, None
        except httpx.TransportError:
            self.logger.exception("media download network error ref={}", media_ref)
            raise
        except Exception:
            self.logger.exception("media download error ref={}", media_ref)
            return None, None

    async def _read_media_bytes(
        self,
        media_ref: str,
    ) -> tuple[bytes | None, str | None, str | None]:
        """读取媒体字节：支持 http(s) URL、file:// 与本地路径，返回 (数据, 文件名, content_type)。"""
        if not media_ref:
            return None, None, None

        if self._is_http_url(media_ref):
            data, raw_content_type = await self._fetch_remote_media_bytes(media_ref)
            if data is None:
                return None, None, None
            content_type = (raw_content_type or "").split(";")[0].strip()
            filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
            return data, filename, content_type or None

        try:
            if media_ref.startswith("file://"):
                parsed = urlparse(media_ref)
                local_path = Path(unquote(parsed.path))
            else:
                local_path = Path(os.path.expanduser(media_ref))
            if not local_path.is_file():
                self.logger.warning("media file not found: {}", local_path)
                return None, None, None
            data = await asyncio.to_thread(local_path.read_bytes)
            content_type = mimetypes.guess_type(local_path.name)[0]
            return data, local_path.name, content_type
        except Exception:
            self.logger.exception("media read error ref={}", media_ref)
            return None, None, None

    async def _upload_media(
        self,
        token: str,
        data: bytes,
        media_type: str,
        filename: str,
        content_type: str | None,
    ) -> str | None:
        """将媒体字节上传到钉钉换取 media_id。"""
        if not self._http:
            return None
        url = f"https://oapi.dingtalk.com/media/upload?access_token={token}&type={media_type}"
        mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        files = {"media": (filename, data, mime)}

        try:
            resp = await self._http.post(url, files=files)
            text = resp.text
            result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code >= 400:
                self.logger.error("media upload failed status={} type={} body={}", resp.status_code, media_type, text[:500])
                return None
            errcode = result.get("errcode", 0)
            if errcode != 0:
                self.logger.error("media upload api error type={} errcode={} body={}", media_type, errcode, text[:500])
                return None
            sub = result.get("result") or {}
            media_id = result.get("media_id") or result.get("mediaId") or sub.get("media_id") or sub.get("mediaId")
            if not media_id:
                self.logger.error("media upload missing media_id body={}", text[:500])
                return None
            return str(media_id)
        except httpx.TransportError:
            self.logger.exception("media upload network error type={}", media_type)
            raise
        except Exception:
            self.logger.exception("media upload error type={}", media_type)
            return None

    async def _send_batch_message(
        self,
        token: str,
        chat_id: str,
        msg_key: str,
        msg_param: dict[str, Any],
    ) -> bool:
        """通过钉钉批量消息接口发送消息，自动区分群聊与单聊路由。"""
        if not self._http:
            self.logger.warning("HTTP client not initialized, cannot send")
            return False

        headers = {"x-acs-dingtalk-access-token": token}
        if chat_id.startswith("group:"):
            # 群聊
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            payload = {
                "robotCode": self.config.client_id,
                "openConversationId": chat_id[6:],  # 去掉 "group:" 前缀
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
        else:
            # 单聊
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            payload = {
                "robotCode": self.config.client_id,
                "userIds": [chat_id],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
            body = resp.text
            if resp.status_code != 200:
                self.logger.error("send failed msgKey={} status={} body={}", msg_key, resp.status_code, body[:500])
                return False
            try:
                result = resp.json()
            except Exception:
                result = {}
            errcode = result.get("errcode")
            if errcode not in (None, 0):
                self.logger.error("send api error msgKey={} errcode={} body={}", msg_key, errcode, body[:500])
                return False
            self.logger.debug("message sent to {} with msgKey={}", chat_id, msg_key)
            return True
        except httpx.TransportError:
            self.logger.exception("network error sending message msgKey={}", msg_key)
            raise
        except Exception:
            self.logger.exception("Error sending message msgKey={}", msg_key)
            return False

    async def _send_markdown_text(self, token: str, chat_id: str, content: str) -> bool:
        """以 Markdown 文本形式发送消息。"""
        return await self._send_batch_message(
            token,
            chat_id,
            "sampleMarkdown",
            {"text": content, "title": "Biscuitbot Reply"},
        )

    async def _send_media_ref(self, token: str, chat_id: str, media_ref: str) -> bool:
        """发送一个媒体引用：图片 URL 优先直发，否则下载后上传再发送。"""
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return True

        upload_type = self._guess_upload_type(media_ref)
        if upload_type == "image" and self._is_http_url(media_ref):
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_ref},
            )
            if ok:
                return True
            self.logger.warning("image url send failed, trying upload fallback: {}", media_ref)

        data, filename, content_type = await self._read_media_bytes(media_ref)
        if not data:
            self.logger.error("media read failed: {}", media_ref)
            return False

        filename = filename or self._guess_filename(media_ref, upload_type)
        data, filename, content_type = self._normalize_upload_payload(filename, data, content_type)
        file_type = Path(filename).suffix.lower().lstrip(".")
        if not file_type:
            guessed = mimetypes.guess_extension(content_type or "")
            file_type = (guessed or ".bin").lstrip(".")
        if file_type == "jpeg":
            file_type = "jpg"

        media_id = await self._upload_media(
            token=token,
            data=data,
            media_type=upload_type,
            filename=filename,
            content_type=content_type,
        )
        if not media_id:
            return False

        if upload_type == "image":
            # 生产环境验证：sampleImageMsg 的 photoURL 可接收 media_id
            ok = await self._send_batch_message(
                token,
                chat_id,
                "sampleImageMsg",
                {"photoURL": media_id},
            )
            if ok:
                return True
            self.logger.warning("image media_id send failed, falling back to file: {}", media_ref)

        return await self._send_batch_message(
            token,
            chat_id,
            "sampleFile",
            {"mediaId": media_id, "fileName": filename, "fileType": file_type},
        )

    async def send(self, msg: OutboundMessage) -> None:
        """通过钉钉发送一条出站消息（文本 + 媒体）。"""
        token = await self._get_access_token()
        if not token:
            return

        if msg.content and msg.content.strip():
            await self._send_markdown_text(token, msg.chat_id, msg.content.strip())

        for media_ref in msg.media or []:
            ok = await self._send_media_ref(token, msg.chat_id, media_ref)
            if ok:
                continue
            self.logger.error("media send failed for {}", media_ref)
            # 发送可见的回退提示，使失败对用户可见
            filename = self._guess_filename(media_ref, self._guess_upload_type(media_ref))
            await self._send_markdown_text(
                token,
                msg.chat_id,
                f"[Attachment send failed: {filename}]",
            )

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        conversation_type: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """处理入站消息（由 ``BiscuitbotDingTalkHandler`` 调用）。

        委托给 ``BaseChannel._handle_message()``，由后者在发布到消息总线前执行
        ``allow_from`` 权限校验。
        """
        try:
            self.logger.info("inbound: {} from {}", content, sender_name)
            is_group = conversation_type == "2" and conversation_id
            chat_id = f"group:{conversation_id}" if is_group else sender_id
            session_key = None
            if is_group and self.config.group_user_isolation:
                session_key = f"{self.name}:group:{conversation_id}:{sender_id}"
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=str(content),
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                    "conversation_type": conversation_type,
                },
                session_key=session_key,
                is_dm=not is_group,  # 私聊传 True，便于未授权用户收到配对码
            )
        except Exception:
            self.logger.exception("Error publishing message")

    async def _download_dingtalk_file(
        self,
        download_code: str,
        filename: str,
        sender_id: str,
    ) -> str | None:
        """将钉钉文件下载到媒体目录，返回本地路径。"""
        from biscuitbot.config.paths import get_media_dir

        try:
            token = await self._get_access_token()
            if not token or not self._http:
                self.logger.error("file download: no token or http client")
                return None

            # 第一步：用 downloadCode 换取临时下载 URL
            api_url = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
            headers = {"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"}
            payload = {"downloadCode": download_code, "robotCode": self.config.client_id}
            resp = await self._http.post(api_url, json=payload, headers=headers)
            if resp.status_code != 200:
                self.logger.error("get download URL failed: status={}, body={}", resp.status_code, resp.text)
                return None

            result = resp.json()
            download_url = result.get("downloadUrl")
            if not download_url:
                self.logger.error("download URL not found in response: {}", result)
                return None

            # 第二步：下载文件内容。
            # filename 来自发送者可控字段，必须先净化以杜绝路径遍历；
            # 同时对齐 _fetch_remote_media_bytes 的 20MB 体积上限：
            # 先查 content-length 预检，下载后校验实际长度。
            file_resp = await self._http.get(download_url, follow_redirects=True)
            if file_resp.status_code != 200:
                self.logger.error("file download failed: status={}", file_resp.status_code)
                return None

            declared_length = (file_resp.headers.get("content-length") or "").strip()
            if declared_length.isdigit() and int(declared_length) > DINGTALK_MAX_REMOTE_MEDIA_BYTES:
                self.logger.warning(
                    "file download too large: content-length={} limit={}",
                    declared_length,
                    DINGTALK_MAX_REMOTE_MEDIA_BYTES,
                )
                return None
            if len(file_resp.content) > DINGTALK_MAX_REMOTE_MEDIA_BYTES:
                self.logger.warning(
                    "file download too large: bytes={} limit={}",
                    len(file_resp.content),
                    DINGTALK_MAX_REMOTE_MEDIA_BYTES,
                )
                return None

            # 保存到媒体目录（工作区可访问）
            download_dir = get_media_dir("channels/dingtalk") / sender_id
            download_dir.mkdir(parents=True, exist_ok=True)
            file_path = download_dir / (safe_filename(filename) or "attachment")
            await asyncio.to_thread(file_path.write_bytes, file_resp.content)
            self.logger.info("file saved: {}", file_path)
            return str(file_path)
        except Exception:
            self.logger.exception("file download error")
            return None
