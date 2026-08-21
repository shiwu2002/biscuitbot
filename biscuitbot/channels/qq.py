"""QQ 渠道实现，基于 botpy SDK。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 QQ 官方机器人平台组件。
在项目架构中起到的作用：通过 QQ botpy SDK 将 QQ 频道和群聊的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：使用 botpy SDK 的 WebSocket 连接接收事件，支持 C2C（私聊）和 Group（群聊）消息。
- 鉴权：通过 QQ 开放平台的 App ID 和 Secret 进行身份认证。
- 入站消息：解析 botpy 消息（C2C/Group），通过分块流式写入下载附件到媒体目录（内存安全），
  内容包含清晰的"已接收文件"列表和本地路径。
- 出站消息：先通过 QQ 富媒体 API 发送附件（base64 上传 + msg_type=7），再发送文本（纯文本或 Markdown）。
  msg.media 支持本地路径、file:// 路径和 http(s) URL。
- 注意事项：QQ 限制多种音视频格式，保守地分类为图片或文件。
  附件结构在不同 botpy 版本间存在差异，尝试多种字段候选。
"""

from __future__ import annotations

import asyncio  # 异步事件循环与并发原语
import base64  # base64 编码（媒体上传）
import mimetypes  # MIME 类型猜测（媒体分类）
import os  # 文件路径处理
import re  # 正则表达式（文件名安全化）
import time  # 时间戳生成（文件名）
from collections import deque  # 固定长度去重队列（消息 ID）
from contextlib import suppress  # 上下文管理器，抑制指定异常
from pathlib import Path  # 路径处理
from typing import TYPE_CHECKING, Any, Literal  # 类型注解支持
from urllib.parse import unquote, urlparse  # URL 解析与解码

import aiohttp  # 异步 HTTP 客户端（附件下载）
from loguru import logger  # 日志记录
from pydantic import Field  # Pydantic 模型字段定义

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.security.network import validate_url_target  # URL 安全校验
from biscuitbot.utils.logging_bridge import redirect_lib_logging  # 第三方库日志桥接

try:
    from biscuitbot.config.paths import get_media_dir  # 媒体文件目录
except Exception:  # pragma: no cover
    get_media_dir = None  # type: ignore

try:
    import botpy  # QQ 官方机器人 SDK
    from botpy.http import Route  # botpy HTTP 路由

    QQ_AVAILABLE = True
except ImportError:  # pragma: no cover
    QQ_AVAILABLE = False
    botpy = None
    Route = None

if TYPE_CHECKING:
    from botpy.message import BaseMessage, C2CMessage, GroupMessage  # botpy 消息类型（仅类型检查时导入）
    from botpy.types.message import Media  # botpy 媒体类型


# QQ 富媒体 file_type：1=图片，4=文件
# （2=语音、3=视频受限；我们仅使用图片和文件）
QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_FILE = 4

_IMAGE_EXTS = {  # 图片扩展名集合
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".ico",
    ".svg",
}

# 将不安全字符替换为 "_"，保留中文和常见安全标点
_SAFE_NAME_RE = re.compile(r"[^\w.\-()\[\]（）【】\u4e00-\u9fff]+", re.UNICODE)


def _sanitize_filename(name: str) -> str:
    """安全化文件名，避免路径遍历和问题字符。"""
    name = (name or "").strip()
    name = Path(name).name
    name = _SAFE_NAME_RE.sub("_", name).strip("._ ")
    return name


def _is_image_name(name: str) -> bool:
    """判断文件名是否为图片类型。"""
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _guess_send_file_type(filename: str) -> int:
    """保守地判断发送类型：图片返回 1，其余返回 4。"""
    ext = Path(filename).suffix.lower()
    mime, _ = mimetypes.guess_type(filename)
    if ext in _IMAGE_EXTS or (mime and mime.startswith("image/")):
        return QQ_FILE_TYPE_IMAGE
    return QQ_FILE_TYPE_FILE


def _make_bot_class(channel: QQChannel) -> type[botpy.Client]:
    """创建绑定到指定渠道的 botpy Client 子类。"""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            # 禁用 botpy 的文件日志 —— biscuitbot 使用 loguru；默认的 "botpy.log" 在只读文件系统上会失败
            super().__init__(intents=intents, ext_handlers=False)

        async def on_ready(self):
            logger.info("QQ bot ready: {}", self.robot.name)

        async def on_c2c_message_create(self, message: C2CMessage):
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: GroupMessage):
            await channel._on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            await channel._on_message(message, is_group=False)

    return _Bot


class QQConfig(Base):
    """QQ 渠道配置（基于 botpy SDK）。"""

    enabled: bool = False
    app_id: str = ""  # QQ 开放平台应用 ID
    secret: str = ""  # QQ 开放平台应用密钥
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    allow_all: bool = False  # 静默放行：为 True 时所有用户可直接私聊，无需白名单或配对码
    msg_format: Literal["plain", "markdown"] = "plain"  # 消息格式：纯文本或 Markdown
    ack_message: str = "⏳ Processing..."  # 收到消息后的确认回复

    # 可选：入站附件保存目录。为空时使用 biscuitbot 的 get_media_dir("channels/qq")
    media_dir: str = ""

    # 下载调优参数
    download_chunk_size: int = 1024 * 256  # 256KB 分块大小
    download_max_bytes: int = 1024 * 1024 * 200  # 200MB 安全上限


class QQChannel(BaseChannel):
    """QQ 渠道，使用 botpy SDK 的 WebSocket 连接。"""

    name = "qq"
    display_name = "QQ"
    requires_module = "botpy"  # 必需 SDK 模块（缺失时自动安装）
    pip_requires = ["qq-botpy>=1.2.0"]

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig = config

        self._client: botpy.Client | None = None  # botpy 客户端实例
        self._http: aiohttp.ClientSession | None = None  # HTTP 会话（附件下载）

        self._processed_ids: deque[str] = deque(maxlen=1000)  # 已处理消息 ID 去重队列
        self._msg_seq: int = 1  # 消息序列号（避免 QQ API 去重）
        self._chat_type_cache: dict[str, str] = {}  # 会话类型缓存（chat_id → "c2c"/"group"）

        self._media_root: Path = self._init_media_root()  # 媒体文件根目录

    # ---------------------------
    # 生命周期管理
    # ---------------------------

    def _init_media_root(self) -> Path:
        """选择入站附件的保存目录。"""
        if self.config.media_dir:
            root = Path(self.config.media_dir).expanduser()
        elif get_media_dir:
            try:
                root = Path(get_media_dir("channels/qq"))
            except Exception:
                root = Path.home() / ".biscuitbot" / "media" / "channels" / "qq"
        else:
            root = Path.home() / ".biscuitbot" / "media" / "channels" / "qq"

        root.mkdir(parents=True, exist_ok=True)
        self.logger.info("media directory: {}", str(root))
        return root

    async def start(self) -> None:
        """启动 QQ 机器人，包含自动重连循环。"""
        redirect_lib_logging("botpy", level="WARNING")
        if not QQ_AVAILABLE:
            self.logger.error("SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            self.logger.error("app_id and secret not configured")
            return

        self._running = True
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        self._client = _make_bot_class(self)()
        self.logger.info("bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """运行机器人连接，包含自动重连。"""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                self.logger.warning("bot error: {}", e)
            if self._running:
                self.logger.info("Reconnecting bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止机器人并清理资源。"""
        self._running = False
        if self._client:
            with suppress(Exception):
                await self._client.close()
        self._client = None

        if self._http:
            with suppress(Exception):
                await self._http.close()
        self._http = None

        self.logger.info("bot stopped")

    # ---------------------------
    # 出站（发送）
    # ---------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """先发送附件，再发送文本消息。"""
        try:
            if not self._client:
                self.logger.warning("client not initialized")
                return

            msg_id = msg.metadata.get("message_id")
            chat_type = self._chat_type_cache.get(msg.chat_id, "c2c")
            is_group = chat_type == "group"

            # 1) Send media
            for media_ref in msg.media or []:
                ok = await self._send_media(
                    chat_id=msg.chat_id,
                    media_ref=media_ref,
                    msg_id=msg_id,
                    is_group=is_group,
                )
                if not ok:
                    filename = (
                        os.path.basename(urlparse(media_ref).path)
                        or os.path.basename(media_ref)
                        or "file"
                    )
                    await self._send_text_only(
                        chat_id=msg.chat_id,
                        is_group=is_group,
                        msg_id=msg_id,
                        content=f"[Attachment send failed: {filename}]",
                    )

            # 2) Send text
            if msg.content and msg.content.strip():
                await self._send_text_only(
                    chat_id=msg.chat_id,
                    is_group=is_group,
                    msg_id=msg_id,
                    content=msg.content.strip(),
                )
        except (aiohttp.ClientError, OSError):
            # Network / transport errors — propagate so ChannelManager can retry
            raise
        except Exception:
            self.logger.exception("Error sending message to chat_id={}", msg.chat_id)

    async def _send_text_only(
        self,
        chat_id: str,
        is_group: bool,
        msg_id: str | None,
        content: str,
    ) -> None:
        """发送纯文本或 Markdown 文本消息。"""
        if not self._client:
            return

        self._msg_seq += 1
        use_markdown = self.config.msg_format == "markdown"
        payload: dict[str, Any] = {
            "msg_type": 2 if use_markdown else 0,
            "msg_id": msg_id,
            "msg_seq": self._msg_seq,
        }
        if use_markdown:
            payload["markdown"] = {"content": content}
        else:
            payload["content"] = content

        if is_group:
            await self._client.api.post_group_message(group_openid=chat_id, **payload)
        else:
            await self._client.api.post_c2c_message(openid=chat_id, **payload)

    async def _send_media(
        self,
        chat_id: str,
        media_ref: str,
        msg_id: str | None,
        is_group: bool,
    ) -> bool:
        """读取字节 → base64 上传 → msg_type=7 发送。"""
        if not self._client:
            return False

        data, filename = await self._read_media_bytes(media_ref)
        if not data or not filename:
            return False

        try:
            file_type = _guess_send_file_type(filename)
            file_data_b64 = base64.b64encode(data).decode()

            media_obj = await self._post_base64file(
                chat_id=chat_id,
                is_group=is_group,
                file_type=file_type,
                file_data=file_data_b64,
                file_name=filename,
                srv_send_msg=False,
            )
            if not media_obj:
                self.logger.error("media upload failed: empty response")
                return False

            self._msg_seq += 1
            if is_group:
                await self._client.api.post_group_message(
                    group_openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=chat_id,
                    msg_type=7,
                    msg_id=msg_id,
                    msg_seq=self._msg_seq,
                    media=media_obj,
                )

            self.logger.info("media sent: {}", filename)
            return True
        except (aiohttp.ClientError, OSError) as e:
            # Network / transport errors — propagate for retry by caller
            self.logger.warning("send media network error filename={} err={}", filename, e)
            raise
        except Exception:
            # API-level or other non-network errors — return False so send() can fallback
            self.logger.exception("send media failed filename={}", filename)
            return False

    async def _read_media_bytes(self, media_ref: str) -> tuple[bytes | None, str | None]:
        """从 http(s) 或本地文件路径读取字节；返回 (data, filename)。"""
        media_ref = (media_ref or "").strip()
        if not media_ref:
            return None, None

        # Local file: plain path or file:// URI
        if not media_ref.startswith("http://") and not media_ref.startswith("https://"):
            try:
                if media_ref.startswith("file://"):
                    parsed = urlparse(media_ref)
                    # Windows: path in netloc; Unix: path in path
                    raw = parsed.path or parsed.netloc
                    local_path = Path(unquote(raw))
                else:
                    local_path = Path(os.path.expanduser(media_ref))

                if not local_path.is_file():
                    self.logger.warning("outbound media file not found: {}", str(local_path))
                    return None, None

                data = await asyncio.to_thread(local_path.read_bytes)
                return data, local_path.name
            except Exception as e:
                self.logger.warning("outbound media read error ref={} err={}", media_ref, e)
                return None, None

        # Remote URL
        ok, err = validate_url_target(media_ref)
        if not ok:
            self.logger.warning("outbound media URL validation failed url={} err={}", media_ref, err)
            return None, None

        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        try:
            async with self._http.get(media_ref, allow_redirects=True) as resp:
                if resp.status >= 400:
                    self.logger.warning(
                        "outbound media download failed status={} url={}",
                        resp.status,
                        media_ref,
                    )
                    return None, None
                data = await resp.read()
                if not data:
                    return None, None
                filename = os.path.basename(urlparse(media_ref).path) or "file.bin"
                return data, filename
        except Exception as e:
            self.logger.warning("outbound media download error url={} err={}", media_ref, e)
            return None, None

    # https://github.com/tencent-connect/botpy/issues/198
    # https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/rich-media.html
    async def _post_base64file(
        self,
        chat_id: str,
        is_group: bool,
        file_type: int,
        file_data: str,
        file_name: str | None = None,
        srv_send_msg: bool = False,
    ) -> Media:
        """上传 base64 编码的文件并返回 Media 对象。"""
        if not self._client:
            raise RuntimeError("QQ client not initialized")

        if is_group:
            endpoint = "/v2/groups/{group_openid}/files"
            id_key = "group_openid"
        else:
            endpoint = "/v2/users/{openid}/files"
            id_key = "openid"

        payload: dict[str, Any] = {
            id_key: chat_id,
            "file_type": file_type,
            "file_data": file_data,
            "srv_send_msg": srv_send_msg,
        }
        # Only pass file_name for non-image types (file_type=4).
        # Passing file_name for images causes QQ client to render them as
        # file attachments instead of inline images.
        if file_type != QQ_FILE_TYPE_IMAGE and file_name:
            payload["file_name"] = file_name

        route = Route("POST", endpoint, **{id_key: chat_id})
        result = await self._client.api._http.request(route, json=payload)

        # Extract only the file_info field to avoid extra fields (file_uuid, ttl, etc.)
        # that may confuse QQ client when sending the media object.
        if isinstance(result, dict) and "file_info" in result:
            return {"file_info": result["file_info"]}
        return result

    # ---------------------------
    # 入站（接收）
    # ---------------------------

    async def _on_message(self, data: C2CMessage | GroupMessage, is_group: bool = False) -> None:
        """解析入站消息，下载附件，并发布到消息总线。"""
        try:
            if is_group:
                chat_id = data.group_openid
                user_id = data.author.member_openid
                chat_type = "group"
            else:
                chat_id = str(
                    getattr(data.author, "id", None)
                    or getattr(data.author, "user_openid", "unknown")
                )
                user_id = chat_id
                chat_type = "c2c"

            content = (data.content or "").strip()

            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)
            self._chat_type_cache[chat_id] = chat_type

            # Early permission check — avoid attachment downloads and ack side effects
            # for unauthorized users. C2C messages can receive pairing codes;
            # group messages remain silently ignored.
            if not self.is_allowed(user_id):
                if not is_group:
                    await self._handle_message(
                        sender_id=user_id,
                        chat_id=chat_id,
                        content="",
                        is_dm=True,
                    )
                return

            # the data used by tests don't contain attachments property
            # so we use getattr with a default of [] to avoid AttributeError in tests
            attachments = getattr(data, "attachments", None) or []
            media_paths, recv_lines, att_meta = await self._handle_attachments(attachments)

            # Compose content that always contains actionable saved paths
            if recv_lines:
                tag = (
                    "[Image]"
                    if any(_is_image_name(Path(p).name) for p in media_paths)
                    else "[File]"
                )
                file_block = "Received files:\n" + "\n".join(recv_lines)
                content = (
                    f"{content}\n\n{file_block}".strip() if content else f"{tag}\n{file_block}"
                )

            if not content and not media_paths:
                return

            if self.config.ack_message:
                try:
                    await self._send_text_only(
                        chat_id=chat_id,
                        is_group=is_group,
                        msg_id=data.id,
                        content=self.config.ack_message,
                    )
                except Exception:
                    self.logger.debug("ack message failed for chat_id={}", chat_id)

            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media_paths if media_paths else None,
                metadata={
                    "message_id": data.id,
                    "attachments": att_meta,
                },
                is_dm=not is_group,
            )
        except Exception:
            self.logger.exception("Error handling inbound message id={}", getattr(data, "id", "?"))

    async def _handle_attachments(
        self,
        attachments: list[BaseMessage._Attachments],
    ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        """提取、下载（分块）并格式化附件，供 agent 使用。"""
        media_paths: list[str] = []
        recv_lines: list[str] = []
        att_meta: list[dict[str, Any]] = []

        if not attachments:
            return media_paths, recv_lines, att_meta

        for att in attachments:
            url = getattr(att, "url", None) or ""
            filename = getattr(att, "filename", None) or ""
            ctype = getattr(att, "content_type", None) or ""

            self.logger.info("Downloading file: {}", filename or url)
            local_path = await self._download_to_media_dir_chunked(url, filename_hint=filename)

            att_meta.append(
                {
                    "url": url,
                    "filename": filename,
                    "content_type": ctype,
                    "saved_path": local_path,
                }
            )

            if local_path:
                media_paths.append(local_path)
                shown_name = filename or os.path.basename(local_path)
                recv_lines.append(f"- {shown_name}\n  saved: {local_path}")
            else:
                shown_name = filename or url
                recv_lines.append(f"- {shown_name}\n  saved: [download failed]")

        return media_paths, recv_lines, att_meta

    async def _download_to_media_dir_chunked(
        self,
        url: str,
        filename_hint: str = "",
    ) -> str | None:
        """使用流式分块写入下载入站附件。

        采用分块流式写入，避免将大文件加载到内存中。
        强制最大下载大小限制，并写入 .part 临时文件，
        成功后原子性重命名为最终文件。
        """
        # Handle protocol-relative URLs (e.g. "//multimedia.nt.qq.com/...")
        if url.startswith("//"):
            url = f"https:{url}"

        if not self._http:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))

        safe = _sanitize_filename(filename_hint)
        ts = int(time.time() * 1000)
        tmp_path: Path | None = None

        try:
            async with self._http.get(
                url,
                timeout=aiohttp.ClientTimeout(total=120),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    self.logger.warning("download failed: status={} url={}", resp.status, url)
                    return None

                ctype = (resp.headers.get("Content-Type") or "").lower()

                # Infer extension: url -> filename_hint -> content-type -> fallback
                ext = Path(urlparse(url).path).suffix
                if not ext:
                    ext = Path(filename_hint).suffix
                if not ext:
                    if "png" in ctype:
                        ext = ".png"
                    elif "jpeg" in ctype or "jpg" in ctype:
                        ext = ".jpg"
                    elif "gif" in ctype:
                        ext = ".gif"
                    elif "webp" in ctype:
                        ext = ".webp"
                    elif "pdf" in ctype:
                        ext = ".pdf"
                    else:
                        ext = ".bin"

                if safe:
                    if not Path(safe).suffix:
                        safe = safe + ext
                    filename = safe
                else:
                    filename = f"qq_file_{ts}{ext}"

                target = self._media_root / filename
                if target.exists():
                    target = self._media_root / f"{target.stem}_{ts}{target.suffix}"

                tmp_path = target.with_suffix(target.suffix + ".part")

                # Stream write
                downloaded = 0
                chunk_size = max(1024, int(self.config.download_chunk_size or 262144))
                max_bytes = max(
                    1024 * 1024, int(self.config.download_max_bytes or (200 * 1024 * 1024))
                )

                def _open_tmp():
                    tmp_path.parent.mkdir(parents=True, exist_ok=True)
                    return open(tmp_path, "wb")  # noqa: SIM115

                f = await asyncio.to_thread(_open_tmp)
                try:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            self.logger.warning(
                                "download exceeded max_bytes={} url={} -> abort",
                                max_bytes,
                                url,
                            )
                            return None
                        await asyncio.to_thread(f.write, chunk)
                finally:
                    await asyncio.to_thread(f.close)

                # Atomic rename
                await asyncio.to_thread(os.replace, tmp_path, target)
                tmp_path = None  # mark as moved
                self.logger.info("file saved: {}", str(target))
                return str(target)

        except Exception:
            self.logger.exception("download error")
            return None
        finally:
            # Cleanup partial file
            if tmp_path is not None:
                with suppress(Exception):
                    tmp_path.unlink(missing_ok=True)
