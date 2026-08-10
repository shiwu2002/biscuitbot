"""Matrix（Element）渠道实现 —— 入站同步 + 出站消息/媒体投递。

所属模块与项目作用
====================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 Matrix 平台组件。
在项目架构中起到的作用：通过 matrix-nio SDK 将 Matrix 协议的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：基于 matrix-nio SDK 的长轮询（long-polling）sync 同步循环接收事件。
- 鉴权：支持两种方式 —— 密码登录（生成并持久化 access_token）或直接配置 access_token + device_id。
- 端到端加密（E2EE）：默认开启，支持加密房间；可选 SAS（短认证字符串）交互式设备验证。
- 消息格式：支持纯文本与 Matrix 自定义 HTML（org.matrix.custom.html），通过 mistune 渲染 Markdown 并用 nh3 清理。
- 媒体处理：入站媒体通过 mxc:// URL 下载（支持加密媒体解密），出站媒体通过 Matrix content repository 上传。
- 流式回复：支持 LLM 流式输出，通过编辑同一条消息实现增量更新（受 _STREAM_EDIT_INTERVAL 限流）。
- 线程支持：解析并构建 m.thread 关系，使回复保持在同一话题线程内。
- 输入指示器：处理长任务时持续刷新 typing 状态（每 20 秒保活，避免 30 秒超时过期）。
- 群聊策略：支持 open（全部响应）、mention（仅@时响应）、allowlist（白名单房间）三种模式。
"""

import asyncio  # 异步事件循环与并发原语
import json  # JSON 序列化/反序列化（会话持久化）
import mimetypes  # MIME 类型猜测（媒体分类）
import time  # 时间戳生成（启动时间、流式编辑限流）
from contextlib import suppress  # 上下文管理器，抑制指定异常
from dataclasses import dataclass  # 数据类装饰器（流式缓冲区）
from pathlib import Path  # 路径处理
from typing import Any, Literal, TypeAlias  # 类型注解支持
from urllib.parse import quote, urlparse  # URL 解析与编码（mxc:// 处理）

from pydantic import Field  # Pydantic 模型字段定义

from biscuitbot.security.workspace_policy import is_path_within  # 工作区路径校验

try:
    import aiohttp  # 异步 HTTP 客户端（媒体下载）
    import nh3  # HTML 清理库（Ammonia 的 Python 绑定）
    from mistune import HTMLRenderer, create_markdown  # Markdown 渲染器
    from nio import (  # matrix-nio SDK：Matrix 协议异步客户端
        AsyncClient,
        AsyncClientConfig,
        InviteEvent,
        JoinError,
        KeyVerificationCancel,
        KeyVerificationEvent,
        KeyVerificationKey,
        KeyVerificationMac,
        KeyVerificationStart,
        LoginResponse,
        MatrixRoom,
        RoomEncryptedMedia,
        RoomMessage,
        RoomMessageMedia,
        RoomMessageText,
        RoomSendError,
        RoomSendResponse,
        RoomTypingError,
        SyncError,
        ToDeviceError,
        UploadError,
    )
    from nio.crypto.attachments import decrypt_attachment  # 加密附件解密
    from nio.exceptions import EncryptionError  # 加密异常
except ImportError as e:
    raise ImportError(
        "Matrix dependencies not installed. Run: pip install biscuitbot[matrix]"
    ) from e

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_data_dir, get_media_dir  # 数据目录与媒体目录
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.utils.helpers import safe_filename  # 文件名安全化工具
from biscuitbot.utils.logging_bridge import redirect_lib_logging  # 第三方库日志桥接

TYPING_NOTICE_TIMEOUT_MS = 30_000  # 输入指示器超时时间（30 秒）
# 保活间隔必须小于 TYPING_NOTICE_TIMEOUT_MS，避免指示器在处理过程中过期。
TYPING_KEEPALIVE_INTERVAL_MS = 20_000  # 输入指示器保活间隔（20 秒）
MATRIX_HTML_FORMAT = "org.matrix.custom.html"  # Matrix 自定义 HTML 格式标识
_ATTACH_MARKER = "[attachment: {}]"  # 附件占位符（已保存到本地）
_ATTACH_TOO_LARGE = "[attachment: {} - too large]"  # 附件过大占位符
_ATTACH_FAILED = "[attachment: {} - download failed]"  # 附件下载失败占位符
_ATTACH_UPLOAD_FAILED = "[attachment: {} - upload failed]"  # 附件上传失败占位符
_DEFAULT_ATTACH_NAME = "attachment"  # 默认附件名
_MSGTYPE_MAP = {"m.image": "image", "m.audio": "audio", "m.video": "video", "m.file": "file"}  # Matrix msgtype 到内部类型映射

MATRIX_MEDIA_EVENT_FILTER = (RoomMessageMedia, RoomEncryptedMedia)  # 媒体事件类型过滤器
MatrixMediaEvent: TypeAlias = RoomMessageMedia | RoomEncryptedMedia  # 媒体事件类型别名


class _MediaTooLargeError(Exception):
    """当入站 Matrix 媒体下载超过配置上限时抛出。"""

# Matrix Markdown 渲染器：转义 HTML，允许 mxc:// 协议（Matrix 媒体 URL）
MATRIX_MARKDOWN = create_markdown(
    escape=True,
    renderer=HTMLRenderer(escape=True, allow_harmful_protocols=["mxc://"]),
    plugins=["table", "strikethrough", "url", "superscript", "subscript"],
)

# Matrix 允许的 HTML 标签白名单（符合 org.matrix.custom.html 规范）
MATRIX_ALLOWED_HTML_TAGS = {
    "p", "a", "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "br", "table", "thead", "tbody", "tr", "th", "td",
    "caption", "sup", "sub", "img",
}
# Matrix 允许的 HTML 属性白名单（按标签分组）
MATRIX_ALLOWED_HTML_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href"}, "code": {"class"}, "ol": {"start"},
    "img": {"src", "alt", "title", "width", "height"},
}
MATRIX_ALLOWED_URL_SCHEMES = {"https", "http", "matrix", "mailto", "mxc"}  # 允许的 URL 协议


def _filter_matrix_html_attribute(tag: str, attr: str, value: str) -> str | None:
    """过滤属性值到 Matrix 兼容的安全子集。"""
    if tag == "a" and attr == "href":
        return value if value.lower().startswith(("https://", "http://", "matrix:", "mailto:")) else None
    if tag == "img" and attr == "src":
        return value if value.lower().startswith("mxc://") else None
    if tag == "code" and attr == "class":
        # 仅保留 language-* 代码语言类，排除 language-_ 开头的无效类
        classes = [c for c in value.split() if c.startswith("language-") and not c.startswith("language-_")]
        return " ".join(classes) if classes else None
    return value


# HTML 清理器：基于 nh3，使用上述白名单和属性过滤器
MATRIX_HTML_CLEANER = nh3.Cleaner(
    tags=MATRIX_ALLOWED_HTML_TAGS,
    attributes=MATRIX_ALLOWED_HTML_ATTRIBUTES,
    attribute_filter=_filter_matrix_html_attribute,
    url_schemes=MATRIX_ALLOWED_URL_SCHEMES,
    strip_comments=True,
    link_rel="noopener noreferrer",
)

@dataclass
class _StreamBuf:
    """LLM 响应流数据缓冲区。

    用于流式回复场景，累积增量文本并记录首次发送的事件 ID，
    后续通过编辑该事件实现增量更新。

    :ivar text: 缓冲区累积的文本内容。
    :ivar event_id: 关联的事件 ID（首次发送后设置）；None 表示尚未发送。
    :ivar last_edit: 最近一次编辑的时间戳（monotonic），用于限流。
    """
    text: str = ""
    event_id: str | None = None
    last_edit: float = 0.0

def _render_markdown_html(text: str) -> str | None:
    """将 Markdown 渲染为清理后的 HTML；纯文本时返回 None。"""
    try:
        formatted = MATRIX_HTML_CLEANER.clean(MATRIX_MARKDOWN(text)).strip()
    except Exception:
        return None
    if not formatted:
        return None
    # 纯 <p>text</p> 时跳过 formatted_body，保持 payload 简洁。
    if formatted.startswith("<p>") and formatted.endswith("</p>"):
        inner = formatted[3:-4]
        if "<" not in inner and ">" not in inner:
            return None
    return formatted


def _build_matrix_text_content(
    text: str,
    event_id: str | None = None,
    thread_relates_to: dict[str, object] | None = None,
) -> dict[str, object]:
    """构建 Matrix 文本消息内容 payload，支持可选的 HTML 格式和事件替换（编辑）。

    :param text: 纯文本消息内容。
    :param event_id: 要替换的事件 ID（提供时表示编辑/替换已有消息）。
    :param thread_relates_to: 可选的 Matrix 线程关系元数据。编辑场景下存入 m.new_content，
        使替换后的消息仍留在同一线程内。
    :return: Matrix 兼容的文本内容字典。
    """
    content: dict[str, object] = {"msgtype": "m.text", "body": text, "m.mentions": {}}
    if html := _render_markdown_html(text):
        content["format"] = MATRIX_HTML_FORMAT
        content["formatted_body"] = html
    if event_id:
        # 编辑场景：将新内容放入 m.new_content，并通过 m.relates_to 指定替换目标
        content["m.new_content"] = {
            "body": text,
            "msgtype": "m.text",
        }
        content["m.relates_to"] = {
            "rel_type": "m.replace",
            "event_id": event_id,
        }
        if thread_relates_to:
            content["m.new_content"]["m.relates_to"] = thread_relates_to
    elif thread_relates_to:
        content["m.relates_to"] = thread_relates_to

    return content


class MatrixConfig(Base):
    """Matrix（Element）渠道配置。"""

    enabled: bool = False
    homeserver: str = "https://matrix.org"  # Matrix 主服务器 URL
    user_id: str = ""  # Matrix 用户 ID（如 @bot:matrix.org）
    password: str = ""  # 登录密码（与 access_token 二选一）
    access_token: str = ""  # 直接配置的访问令牌（与 password 二选一）
    device_id: str = ""  # 设备 ID（使用 access_token 时必填）
    e2ee_enabled: bool = Field(default=True, alias="e2eeEnabled")  # 是否启用端到端加密
    sas_verification: bool = Field(default=False, alias="sasVerification")  # 是否启用 SAS 交互式设备验证
    sync_stop_grace_seconds: int = 2  # 停止时等待 sync 循环的宽限期（秒）
    max_media_bytes: int = 20 * 1024 * 1024  # 媒体文件大小上限（默认 20MB）
    max_concurrent_media_downloads: int = 2  # 并发媒体下载数上限
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    group_policy: Literal["open", "mention", "allowlist"] = "open"  # 群聊响应策略
    group_allow_from: list[str] = Field(default_factory=list)  # 群聊白名单（allowlist 策略下生效）
    allow_room_mentions: bool = False  # 是否响应房间全体 @mention
    streaming: bool = False  # 是否启用流式回复


class MatrixChannel(BaseChannel):
    """Matrix（Element）渠道，使用长轮询同步循环。

    通过 matrix-nio SDK 的 sync_forever 接收事件，支持端到端加密、
    媒体收发、流式回复和线程化对话。
    """

    name = "matrix"
    display_name = "Matrix"
    _STREAM_EDIT_INTERVAL = 2 # 流式编辑最小间隔（秒），避免频繁调用 edit_message_text
    monotonic_time = time.monotonic  # 单调时钟，用于流式限流（避免系统时间回拨影响）

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return MatrixConfig().model_dump(by_alias=True)

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        restrict_to_workspace: bool = False,
        workspace: str | Path | None = None,
    ):
        if isinstance(config, dict):
            config = MatrixConfig.model_validate(config)
        super().__init__(config, bus)
        self.client: AsyncClient | None = None  # matrix-nio 异步客户端
        self._sync_task: asyncio.Task | None = None  # sync 同步循环任务
        self._typing_tasks: dict[str, asyncio.Task] = {}  # 各房间的 typing 保活任务（room_id → Task）
        self._restrict_to_workspace = bool(restrict_to_workspace)  # 是否限制媒体路径到工作区
        self._workspace = (
            Path(workspace).expanduser().resolve(strict=False) if workspace is not None else None
        )  # 工作区根路径（用于媒体路径校验）
        self._server_upload_limit_bytes: int | None = None  # 服务器上传大小上限（懒查询）
        self._server_upload_limit_checked = False  # 是否已查询过服务器上传上限
        self._stream_bufs: dict[str, _StreamBuf] = {}  # 流式回复缓冲区（chat_id → _StreamBuf）
        self._started_at_ms: int = 0  # 渠道启动时间戳（毫秒），用于过滤启动前的事件
        self._media_download_semaphore = asyncio.Semaphore(
            max(1, int(self.config.max_concurrent_media_downloads))
        )  # 媒体下载并发信号量


    async def start(self) -> None:
        """启动 Matrix 客户端并开始 sync 同步循环。"""
        self._running = True
        self._started_at_ms = int(time.time() * 1000)
        redirect_lib_logging("nio", level="WARNING")

        # 初始化加密存储目录
        self.store_path = get_data_dir() / "matrix-store"
        self.store_path.mkdir(parents=True, exist_ok=True)
        self.session_path = self.store_path / "session.json"

        # 将 ':' 替换为 '_'，生成 Windows 安全的文件名
        safe_store_name = self.config.user_id.replace(":", "_") + f"_{self.config.device_id}.db"

        self.client = AsyncClient(
            homeserver=self.config.homeserver,
            user=self.config.user_id,
            store_path=self.store_path,
            config=AsyncClientConfig(
                store_sync_tokens=True,
                encryption_enabled=self.config.e2ee_enabled,
                store_name=safe_store_name,
            ),
        )

        self._register_event_callbacks()
        self._register_to_device_callbacks()
        self._register_response_callbacks()

        if not self.config.e2ee_enabled:
            self.logger.warning("E2EE disabled; encrypted rooms may be undecryptable.")

        # 优先级：password 登录 > access_token + device_id
        if self.config.password:
            if self.config.access_token or self.config.device_id:
                self.logger.warning("Password-based login active; access_token and device_id fields will be ignored.")

            create_new_session = True
            # 尝试复用已持久化的会话
            if self.session_path.exists():
                self.logger.info("Found session.json at {}; attempting to use existing session...", self.session_path)
                try:
                    with open(self.session_path, "r", encoding="utf-8") as f:
                        session = json.load(f)
                    self.client.user_id = self.config.user_id
                    self.client.access_token = session["access_token"]
                    self.client.device_id = session["device_id"]
                    self.client.load_store()
                    self.logger.info("Successfully loaded from existing session")
                    create_new_session = False
                except Exception as e:
                    self.logger.warning("Failed to load from existing session: {}", e)
                    self.logger.info("Falling back to password login...")

            if create_new_session:
                self.logger.info("Using password login...")
                resp = await self.client.login(self.config.password)
                if isinstance(resp, LoginResponse):
                    self.logger.info("Logged in using a password; saving details to disk")
                    self._write_session_to_disk(resp)
                else:
                    self.logger.error("Failed to log in: {}", resp)
                    return

        elif self.config.access_token and self.config.device_id:
            try:
                self.client.user_id = self.config.user_id
                self.client.access_token = self.config.access_token
                self.client.device_id = self.config.device_id
                self.client.load_store()
                self.logger.info("Successfully loaded from existing session")
            except Exception as e:
                self.logger.warning("Failed to load from existing session: {}", e)

        else:
            self.logger.warning("Unable to load a session due to missing password, access_token, or device_id; encryption may not work")
            return

        self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        """停止 Matrix 渠道，优雅关闭 sync 循环。"""
        self._running = False
        # 停止所有房间的 typing 保活任务
        for room_id in list(self._typing_tasks):
            await self._stop_typing_keepalive(room_id, clear_typing=False)
        if self.client:
            self.client.stop_sync_forever()
        # 等待 sync 循环退出，超时则取消
        if self._sync_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._sync_task),
                                       timeout=self.config.sync_stop_grace_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._sync_task
        if self.client:
            await self.client.close()

    def _write_session_to_disk(self, resp: LoginResponse) -> None:
        """将登录会话持久化到磁盘，以便重启后复用。"""
        session = {
            "access_token": resp.access_token,
            "device_id": resp.device_id,
        }
        try:
            with open(self.session_path, "w", encoding="utf-8") as f:
                json.dump(session, f, indent=2)
            # 限制文件权限：该文件包含长期有效的 Matrix access token。
            try:
                self.session_path.chmod(0o600)
            except OSError:
                pass
            self.logger.info("Session saved to {}", self.session_path)
        except Exception as e:
            self.logger.warning("Failed to save session: {}", e)

    def _is_workspace_path_allowed(self, path: Path) -> bool:
        """检查路径是否在工作区内（启用限制时）。"""
        if not self._restrict_to_workspace or not self._workspace:
            return True
        return is_path_within(path, self._workspace)

    def _collect_outbound_media_candidates(self, media: list[str]) -> list[Path]:
        """去重并解析出站附件路径。"""
        seen: set[str] = set()
        candidates: list[Path] = []
        for raw in media:
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = Path(raw.strip()).expanduser()
            try:
                key = str(path.resolve(strict=False))
            except OSError:
                key = str(path)
            if key not in seen:
                seen.add(key)
                candidates.append(path)
        return candidates

    @staticmethod
    def _build_outbound_attachment_content(
        *, filename: str, mime: str, size_bytes: int,
        mxc_url: str, encryption_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建已上传文件/图片/音频/视频的 Matrix 内容 payload。"""
        prefix = mime.split("/")[0]
        msgtype = {"image": "m.image", "audio": "m.audio", "video": "m.video"}.get(prefix, "m.file")
        content: dict[str, Any] = {
            "msgtype": msgtype, "body": filename, "filename": filename,
            "info": {"mimetype": mime, "size": size_bytes}, "m.mentions": {},
        }
        # 加密附件：将加密信息与 url 一并放入 file 字段
        if encryption_info:
            content["file"] = {**encryption_info, "url": mxc_url}
        else:
            content["url"] = mxc_url
        return content

    def _is_encrypted_room(self, room_id: str) -> bool:
        """判断指定房间是否为加密房间。"""
        if not self.client:
            return False
        room = getattr(self.client, "rooms", {}).get(room_id)
        return bool(getattr(room, "encrypted", False))

    async def _send_room_content(self, room_id: str,
                                 content: dict[str, Any]) -> None | RoomSendResponse | RoomSendError:
        """发送 m.room.message 事件，根据 E2EE 配置选择加密选项。"""
        if not self.client:
            return None
        kwargs: dict[str, Any] = {"room_id": room_id, "message_type": "m.room.message", "content": content}

        if self.config.e2ee_enabled:
            kwargs["ignore_unverified_devices"] = True
        response = await self.client.room_send(**kwargs)
        return response

    async def _resolve_server_upload_limit_bytes(self) -> int | None:
        """查询主服务器的上传大小上限（每个渠道生命周期内仅查询一次）。"""
        if self._server_upload_limit_checked:
            return self._server_upload_limit_bytes
        self._server_upload_limit_checked = True
        if not self.client:
            return None
        try:
            response = await self.client.content_repository_config()
        except Exception:
            self.logger.error("Failed to fetch server upload limit", exc_info=True)
            return None
        upload_size = getattr(response, "upload_size", None)
        if isinstance(upload_size, int) and upload_size > 0:
            self._server_upload_limit_bytes = upload_size
            return upload_size
        return None

    async def _effective_media_limit_bytes(self) -> int:
        """返回有效媒体大小上限：min(本地配置, 服务器声明)；0 表示阻止所有上传。"""
        local_limit = max(int(self.config.max_media_bytes), 0)
        server_limit = await self._resolve_server_upload_limit_bytes()
        if server_limit is None:
            return local_limit
        return min(local_limit, server_limit) if local_limit else 0

    async def _upload_and_send_attachment(
        self, room_id: str, path: Path, limit_bytes: int,
        relates_to: dict[str, Any] | None = None,
    ) -> str | None:
        """上传一个本地文件到 Matrix 并作为媒体消息发送。返回失败标记或 None（成功）。"""
        if not self.client:
            return _ATTACH_UPLOAD_FAILED.format(path.name or _DEFAULT_ATTACH_NAME)

        resolved = path.expanduser().resolve(strict=False)
        filename = safe_filename(resolved.name) or _DEFAULT_ATTACH_NAME
        fail = _ATTACH_UPLOAD_FAILED.format(filename)

        if not resolved.is_file() or not self._is_workspace_path_allowed(resolved):
            return fail
        try:
            size_bytes = resolved.stat().st_size
        except OSError:
            return fail
        if limit_bytes <= 0 or size_bytes > limit_bytes:
            return _ATTACH_TOO_LARGE.format(filename)

        mime = mimetypes.guess_type(filename, strict=False)[0] or "application/octet-stream"
        try:
            with resolved.open("rb") as f:
                upload_result = await self.client.upload(
                    f, content_type=mime, filename=filename,
                    encrypt=self.config.e2ee_enabled and self._is_encrypted_room(room_id),
                    filesize=size_bytes,
                )
        except Exception:
            self.logger.error("Matrix media upload failed for %s", filename, exc_info=True)
            return fail

        # upload_result 可能是 (response, encryption_info) 元组或单独 response
        upload_response = upload_result[0] if isinstance(upload_result, tuple) else upload_result
        encryption_info = upload_result[1] if isinstance(upload_result, tuple) and isinstance(upload_result[1], dict) else None
        if isinstance(upload_response, UploadError):
            return fail
        mxc_url = getattr(upload_response, "content_uri", None)
        if not isinstance(mxc_url, str) or not mxc_url.startswith("mxc://"):
            return fail

        content = self._build_outbound_attachment_content(
            filename=filename, mime=mime, size_bytes=size_bytes,
            mxc_url=mxc_url, encryption_info=encryption_info,
        )
        if relates_to:
            content["m.relates_to"] = relates_to
        try:
            await self._send_room_content(room_id, content)
        except Exception:
            self.logger.error("Matrix room content send failed for room_id=%s", room_id, exc_info=True)
            return fail
        return None

    async def send(self, msg: OutboundMessage) -> None:
        """发送出站内容；非进度消息发送后清除 typing 状态。"""
        if not self.client:
            return
        text = msg.content or ""
        candidates = self._collect_outbound_media_candidates(msg.media)
        relates_to = self._build_thread_relates_to(msg.metadata)
        is_progress = bool((msg.metadata or {}).get("_progress"))
        try:
            failures: list[str] = []
            # 先发送附件，收集失败标记
            if candidates:
                limit_bytes = await self._effective_media_limit_bytes()
                for path in candidates:
                    if fail := await self._upload_and_send_attachment(
                        room_id=msg.chat_id,
                        path=path,
                        limit_bytes=limit_bytes,
                        relates_to=relates_to,
                    ):
                        failures.append(fail)
            # 将失败标记追加到文本
            if failures:
                text = f"{text.rstrip()}\n{chr(10).join(failures)}" if text.strip() else "\n".join(failures)
            # 发送文本消息
            if text.strip():
                content = _build_matrix_text_content(text)
                if relates_to:
                    content["m.relates_to"] = relates_to
                await self._send_room_content(msg.chat_id, content)
        finally:
            # 非进度消息发送完成后清除 typing 状态
            if not is_progress:
                await self._stop_typing_keepalive(msg.chat_id, clear_typing=True)

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """发送流式增量：累积到缓冲区，限流编辑同一条消息；流结束时发送最终版本。"""
        meta = metadata or {}
        relates_to = self._build_thread_relates_to(metadata)

        # 流结束：发送最终编辑版本并清理缓冲区
        if meta.get("_stream_end"):
            buf = self._stream_bufs.pop(chat_id, None)
            if not buf or not buf.event_id or not buf.text:
                return

            await self._stop_typing_keepalive(chat_id, clear_typing=True)

            content = _build_matrix_text_content(
                buf.text,
                buf.event_id,
                thread_relates_to=relates_to,
            )
            await self._send_room_content(chat_id, content)
            return

        # 累积增量到缓冲区
        buf = self._stream_bufs.get(chat_id)
        if buf is None:
            buf = _StreamBuf()
            self._stream_bufs[chat_id] = buf
        buf.text += delta

        if not buf.text.strip():
            return

        now = self.monotonic_time()

        # 限流编辑：首次发送或距离上次编辑超过间隔时才发送
        if not buf.last_edit or (now - buf.last_edit) >= self._STREAM_EDIT_INTERVAL:
            try:
                content = _build_matrix_text_content(
                    buf.text,
                    buf.event_id,
                    thread_relates_to=relates_to,
                )
                response = await self._send_room_content(chat_id, content)
                buf.last_edit = now
                if not buf.event_id:
                    # 始终编辑同一条消息，因此仅首次需要记录 event_id
                    buf.event_id = response.event_id
            except Exception:
                self.logger.error("Stream send/edit failed for chat_id=%s", chat_id, exc_info=True)
                await self._stop_typing_keepalive(chat_id, clear_typing=True)


    def _register_event_callbacks(self) -> None:
        """注册入站事件回调（文本消息、媒体消息、房间邀请）。"""
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_media_message, MATRIX_MEDIA_EVENT_FILTER)
        self.client.add_event_callback(self._on_room_invite, InviteEvent)

    def _register_to_device_callbacks(self) -> None:
        """注册 to-device 事件回调（SAS 设备验证）。"""
        if self.config.e2ee_enabled and self.config.sas_verification:
            self.client.add_to_device_callback(
                self._on_key_verification_event,
                (KeyVerificationEvent,),
            )

    def _register_response_callbacks(self) -> None:
        """注册响应错误回调（sync 错误、join 错误、send 错误）。"""
        self.client.add_response_callback(self._on_sync_error, SyncError)
        self.client.add_response_callback(self._on_join_error, JoinError)
        self.client.add_response_callback(self._on_send_error, RoomSendError)

    def _is_sas_sender_allowed(self, sender: str) -> bool:
        """检查 SAS 验证发起者是否在允许列表内。"""
        return bool(sender and self.is_allowed(sender))

    async def _on_key_verification_event(self, event: KeyVerificationEvent) -> None:
        """SAS 设备验证事件入口（异常隔离包装）。"""
        try:
            await self._handle_key_verification_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Matrix SAS verification handling failed")

    async def _handle_key_verification_event(self, event: KeyVerificationEvent) -> None:
        """处理 SAS（短认证字符串）设备验证流程。

        支持的验证阶段：
        - KeyVerificationStart：接受验证请求（要求支持 emoji）
        - KeyVerificationKey：发送 device key 并确认 SAS
        - KeyVerificationMac：验证完成确认
        - KeyVerificationCancel：验证取消
        """
        if not (self.config.e2ee_enabled and self.config.sas_verification):
            return
        if not self.client:
            return

        sender = str(getattr(event, "sender", "") or "")
        transaction_id = str(getattr(event, "transaction_id", "") or "")
        if not transaction_id or not self._is_sas_sender_allowed(sender):
            return

        if isinstance(event, KeyVerificationStart):
            # 仅处理支持 emoji 显示的验证请求
            if "emoji" not in (getattr(event, "short_authentication_string", None) or []):
                self.logger.info(
                    "Ignoring Matrix SAS verification from {} without emoji support",
                    sender,
                )
                return

            response = await self.client.accept_key_verification(transaction_id)
            if isinstance(response, ToDeviceError):
                self.logger.warning("Matrix SAS accept failed for {}: {}", sender, response)
            return

        if isinstance(event, KeyVerificationKey):
            responses = await self.client.send_to_device_messages()
            if any(isinstance(response, ToDeviceError) for response in responses):
                self.logger.warning("Matrix SAS key share failed for {}", sender)
                return

            response = await self.client.confirm_short_auth_string(transaction_id)
            if isinstance(response, ToDeviceError):
                self.logger.warning("Matrix SAS confirm failed for {}: {}", sender, response)
            return

        if isinstance(event, KeyVerificationMac):
            sas = getattr(self.client, "key_verifications", {}).get(transaction_id)
            if sas is not None and getattr(sas, "verified", False):
                self.logger.info("Matrix SAS verification completed for {}", sender)
            return

        if isinstance(event, KeyVerificationCancel):
            self.logger.info(
                "Matrix SAS verification cancelled by {}: {}",
                sender,
                getattr(event, "reason", ""),
            )

    def _is_fatal_auth_response(self, response: Any) -> bool:
        """判断响应是否为不可恢复的认证错误。"""
        code = getattr(response, "status_code", None)
        is_auth = code in {"M_UNKNOWN_TOKEN", "M_FORBIDDEN", "M_UNAUTHORIZED"}
        return is_auth or bool(getattr(response, "soft_logout", False))

    def _log_response_error(self, label: str, response: Any) -> None:
        """记录 Matrix 响应错误 —— 认证错误用 ERROR 级别，其余用 WARNING。"""
        is_fatal = self._is_fatal_auth_response(response)
        (self.logger.error if is_fatal else self.logger.warning)("{} failed: {}", label, response)

    async def _on_sync_error(self, response: SyncError) -> None:
        """处理 sync 同步错误。认证错误时停止 sync 循环，避免每 2 秒重试轰炸主服务器。"""
        self._log_response_error("sync", response)
        if self._is_fatal_auth_response(response):
            # 认证错误无法通过重试恢复；停止 sync 循环，避免每 2 秒轰炸主服务器 (#1851)。
            self.logger.error("Authentication failed irrecoverably; stopping sync loop")
            self._running = False
            if self.client:
                with suppress(Exception):
                    self.client.stop_sync_forever()

    async def _on_join_error(self, response: JoinError) -> None:
        """处理加入房间错误。"""
        self._log_response_error("join", response)

    async def _on_send_error(self, response: RoomSendError) -> None:
        """处理消息发送错误。"""
        self._log_response_error("send", response)

    async def _set_typing(self, room_id: str, typing: bool) -> None:
        """尽力更新输入指示器状态（失败不影响主流程）。"""
        if not self.client:
            return
        with suppress(Exception):
            response = await self.client.room_typing(room_id=room_id, typing_state=typing,
                                                     timeout=TYPING_NOTICE_TIMEOUT_MS)
            if isinstance(response, RoomTypingError):
                self.logger.debug("typing failed for {}: {}", room_id, response)

    async def _start_typing_keepalive(self, room_id: str) -> None:
        """启动周期性 typing 刷新（规范推荐的保活机制）。"""
        await self._stop_typing_keepalive(room_id, clear_typing=False)
        await self._set_typing(room_id, True)
        if not self._running:
            return

        async def loop() -> None:
            with suppress(asyncio.CancelledError):
                while self._running:
                    await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_MS / 1000)
                    await self._set_typing(room_id, True)

        self._typing_tasks[room_id] = asyncio.create_task(loop())

    async def _stop_typing_keepalive(self, room_id: str, *, clear_typing: bool) -> None:
        """停止 typing 保活任务，可选清除 typing 状态。"""
        if task := self._typing_tasks.pop(room_id, None):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if clear_typing:
            await self._set_typing(room_id, False)

    async def _sync_loop(self) -> None:
        """sync 同步循环，带指数退避重试。"""
        backoff = 2.0
        while self._running:
            try:
                await self.client.sync_forever(timeout=30000, full_state=True)
                backoff = 2.0
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._running:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _on_room_invite(self, room: MatrixRoom, event: InviteEvent) -> None:
        """处理房间邀请：允许列表内的邀请者自动加入房间。"""
        if self.is_allowed(event.sender):
            await self.client.join(room.room_id)

    def _is_direct_room(self, room: MatrixRoom) -> bool:
        """判断是否为直聊房间（成员数 <= 2）。"""
        count = getattr(room, "member_count", None)
        return isinstance(count, int) and count <= 2

    def _is_bot_mentioned(self, event: RoomMessage) -> bool:
        """检查 m.mentions payload 中是否包含对机器人的 @mention。"""
        source = getattr(event, "source", None)
        if not isinstance(source, dict):
            return False
        mentions = (source.get("content") or {}).get("m.mentions")
        if not isinstance(mentions, dict):
            return False
        user_ids = mentions.get("user_ids")
        if isinstance(user_ids, list) and self.config.user_id in user_ids:
            return True
        # 允许响应房间全体 @mention（需配置 allow_room_mentions）
        return bool(self.config.allow_room_mentions and mentions.get("room") is True)

    def _is_pre_startup_event(self, event: RoomMessage) -> bool:
        """跳过本进程启动前已落入时间线的事件。

        Matrix sync 在每次启动/重启时会重放房间时间线；若不过滤，
        旧消息会被当作新消息重新处理 (#3553)。
        """
        ts = getattr(event, "server_timestamp", None)
        return isinstance(ts, int) and ts < self._started_at_ms

    def _should_process_message(self, room: MatrixRoom, event: RoomMessage) -> bool:
        """应用发送者和房间策略检查，决定是否处理该消息。"""
        if not self.is_allowed(event.sender):
            return False
        # 直聊房间始终处理
        if self._is_direct_room(room):
            return True
        # 群聊按策略处理
        policy = self.config.group_policy
        if policy == "open":
            return True
        if policy == "allowlist":
            return room.room_id in (self.config.group_allow_from or [])
        if policy == "mention":
            return self._is_bot_mentioned(event)
        return False

    def _media_dir(self) -> Path:
        """返回 Matrix 媒体文件保存目录。"""
        return get_media_dir("matrix")

    @staticmethod
    def _event_source_content(event: RoomMessage) -> dict[str, Any]:
        """从事件 source 中提取 content 字典。"""
        source = getattr(event, "source", None)
        if not isinstance(source, dict):
            return {}
        content = source.get("content")
        return content if isinstance(content, dict) else {}

    def _event_thread_root_id(self, event: RoomMessage) -> str | None:
        """提取事件的线程根 event_id（若属于 m.thread）。"""
        relates_to = self._event_source_content(event).get("m.relates_to")
        if not isinstance(relates_to, dict) or relates_to.get("rel_type") != "m.thread":
            return None
        root_id = relates_to.get("event_id")
        return root_id if isinstance(root_id, str) and root_id else None

    def _thread_metadata(self, event: RoomMessage) -> dict[str, str] | None:
        """构建线程元数据（根 event_id 和回复目标 event_id）。"""
        if not (root_id := self._event_thread_root_id(event)):
            return None
        meta: dict[str, str] = {"thread_root_event_id": root_id}
        if isinstance(reply_to := getattr(event, "event_id", None), str) and reply_to:
            meta["thread_reply_to_event_id"] = reply_to
        return meta

    @staticmethod
    def _build_thread_relates_to(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        """根据元数据构建 m.thread 关系 payload（用于回复保持在同一线程）。"""
        if not metadata:
            return None
        root_id = metadata.get("thread_root_event_id")
        if not isinstance(root_id, str) or not root_id:
            return None
        reply_to = metadata.get("thread_reply_to_event_id") or metadata.get("event_id")
        if not isinstance(reply_to, str) or not reply_to:
            return None
        return {"rel_type": "m.thread", "event_id": root_id,
                "m.in_reply_to": {"event_id": reply_to}, "is_falling_back": True}

    def _event_attachment_type(self, event: MatrixMediaEvent) -> str:
        """根据 msgtype 判断附件类型（image/audio/video/file）。"""
        msgtype = self._event_source_content(event).get("msgtype")
        return _MSGTYPE_MAP.get(msgtype, "file")

    @staticmethod
    def _is_encrypted_media_event(event: MatrixMediaEvent) -> bool:
        """判断是否为加密媒体事件（包含 key/hashes/iv 字段）。"""
        return (isinstance(getattr(event, "key", None), dict)
                and isinstance(getattr(event, "hashes", None), dict)
                and isinstance(getattr(event, "iv", None), str))

    def _event_declared_size_bytes(self, event: MatrixMediaEvent) -> int | None:
        """从事件 info 中提取声明的大小（字节）。"""
        info = self._event_source_content(event).get("info")
        size = info.get("size") if isinstance(info, dict) else None
        return size if type(size) is int and size >= 0 else None

    def _event_mime(self, event: MatrixMediaEvent) -> str | None:
        """提取媒体事件的 MIME 类型。"""
        info = self._event_source_content(event).get("info")
        if isinstance(info, dict) and isinstance(m := info.get("mimetype"), str) and m:
            return m
        m = getattr(event, "mimetype", None)
        return m if isinstance(m, str) and m else None

    def _event_filename(self, event: MatrixMediaEvent, attachment_type: str) -> str:
        """提取媒体事件的文件名，回退到附件类型默认名。"""
        body = getattr(event, "body", None)
        if isinstance(body, str) and body.strip():
            if candidate := safe_filename(Path(body).name):
                return candidate
        return _DEFAULT_ATTACH_NAME if attachment_type == "file" else attachment_type

    def _build_attachment_path(self, event: MatrixMediaEvent, attachment_type: str,
                               filename: str, mime: str | None) -> Path:
        """构建附件保存路径，包含 event_id 前缀以避免冲突。"""
        safe_name = safe_filename(Path(filename).name) or _DEFAULT_ATTACH_NAME
        suffix = Path(safe_name).suffix
        # 无扩展名时根据 MIME 推断
        if not suffix and mime:
            if guessed := mimetypes.guess_extension(mime, strict=False):
                safe_name, suffix = f"{safe_name}{guessed}", guessed
        # 限制文件名长度，避免文件系统限制
        stem = (Path(safe_name).stem or attachment_type)[:72]
        suffix = suffix[:16]
        event_id = safe_filename(str(getattr(event, "event_id", "") or "evt").lstrip("$"))
        event_prefix = (event_id[:24] or "evt").strip("_")
        return self._media_dir() / f"{event_prefix}_{stem}{suffix}"

    async def _download_media_bytes(self, mxc_url: str, limit_bytes: int) -> bytes | None:
        """通过 Matrix media download API 下载媒体字节，超过限制时抛出 _MediaTooLargeError。"""
        if not self.client or limit_bytes <= 0:
            raise _MediaTooLargeError

        # 解析 mxc:// URL，构造 media download 路径
        parsed = urlparse(mxc_url)
        if parsed.scheme != "mxc" or not parsed.netloc or not parsed.path.strip("/"):
            return None

        homeserver = str(getattr(self.client, "homeserver", "") or self.config.homeserver).rstrip("/")
        media_url = (
            f"{homeserver}/_matrix/client/v1/media/download/"
            f"{quote(parsed.netloc, safe='')}/{quote(parsed.path.strip('/'), safe='')}"
        )
        token = getattr(self.client, "access_token", None) or self.config.access_token
        headers = {"Authorization": f"Bearer {token}"} if token else None
        timeout = aiohttp.ClientTimeout(total=None)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(media_url, params={"allow_remote": "true"}) as response:
                    if response.status >= 400:
                        self.logger.warning("download failed for {}: HTTP {}", mxc_url, response.status)
                        return None
                    # 先检查 Content-Length，提前拒绝过大文件
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            if int(content_length) > limit_bytes:
                                raise _MediaTooLargeError
                        except ValueError:
                            pass

                    # 流式读取并累积，超限时抛出异常
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        chunks.extend(chunk)
                        if len(chunks) > limit_bytes:
                            raise _MediaTooLargeError
                    return bytes(chunks)
        except _MediaTooLargeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            self.logger.warning("download failed for {}", mxc_url, exc_info=True)
            return None

    def _decrypt_media_bytes(self, event: MatrixMediaEvent, ciphertext: bytes) -> bytes | None:
        """解密加密媒体字节（使用 nio 的 decrypt_attachment）。"""
        key_obj, hashes, iv = getattr(event, "key", None), getattr(event, "hashes", None), getattr(event, "iv", None)
        key = key_obj.get("k") if isinstance(key_obj, dict) else None
        sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
        if not all(isinstance(v, str) for v in (key, sha256, iv)):
            return None
        try:
            return decrypt_attachment(ciphertext, key, sha256, iv)
        except (EncryptionError, ValueError, TypeError):
            self.logger.warning("decrypt failed for event {}", getattr(event, "event_id", ""))
            return None

    async def _fetch_media_attachment(
        self, room: MatrixRoom, event: MatrixMediaEvent,
    ) -> tuple[dict[str, Any] | None, str]:
        """下载（必要时解密）并持久化 Matrix 附件，返回 (附件元数据, 占位标记)。"""
        atype = self._event_attachment_type(event)
        mime = self._event_mime(event)
        filename = self._event_filename(event, atype)
        mxc_url = getattr(event, "url", None)
        fail = _ATTACH_FAILED.format(filename)

        if not isinstance(mxc_url, str) or not mxc_url.startswith("mxc://"):
            return None, fail

        # 提前根据声明大小过滤，避免无谓下载
        limit_bytes = await self._effective_media_limit_bytes()
        declared = self._event_declared_size_bytes(event)
        if declared is None or declared > limit_bytes:
            return None, _ATTACH_TOO_LARGE.format(filename)

        try:
            # 通过信号量限制并发下载数
            async with self._media_download_semaphore:
                downloaded = await self._download_media_bytes(mxc_url, limit_bytes)
        except _MediaTooLargeError:
            return None, _ATTACH_TOO_LARGE.format(filename)
        if downloaded is None:
            return None, fail

        # 加密媒体需要解密
        encrypted = self._is_encrypted_media_event(event)
        data = downloaded
        if encrypted:
            if (data := self._decrypt_media_bytes(event, downloaded)) is None:
                return None, fail

        if len(data) > limit_bytes:
            return None, _ATTACH_TOO_LARGE.format(filename)

        path = self._build_attachment_path(event, atype, filename, mime)
        try:
            path.write_bytes(data)
        except OSError:
            return None, fail

        attachment = {
            "type": atype, "mime": mime, "filename": filename,
            "event_id": str(getattr(event, "event_id", "") or ""),
            "encrypted": encrypted, "size_bytes": len(data),
            "path": str(path), "mxc_url": mxc_url,
        }
        return attachment, _ATTACH_MARKER.format(path)

    def _base_metadata(self, room: MatrixRoom, event: RoomMessage) -> dict[str, Any]:
        """构建文本和媒体处理器共用的基础元数据。"""
        meta: dict[str, Any] = {"room": getattr(room, "display_name", room.room_id)}
        if isinstance(eid := getattr(event, "event_id", None), str) and eid:
            meta["event_id"] = eid
        if thread := self._thread_metadata(event):
            meta.update(thread)
        return meta

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """处理入站文本消息。"""
        # 跳过自身消息、启动前事件、不满足策略的消息
        if (
            event.sender == self.config.user_id
            or self._is_pre_startup_event(event)
            or not self._should_process_message(room, event)
        ):
            return
        await self._start_typing_keepalive(room.room_id)
        try:
            await self._handle_message(
                sender_id=event.sender, chat_id=room.room_id,
                content=event.body, metadata=self._base_metadata(room, event),
                is_dm=self._is_direct_room(room),
            )
        except Exception:
            await self._stop_typing_keepalive(room.room_id, clear_typing=True)
            raise

    async def _on_media_message(self, room: MatrixRoom, event: MatrixMediaEvent) -> None:
        """处理入站媒体消息（图片/音频/视频/文件，含加密媒体）。"""
        if (
            event.sender == self.config.user_id
            or self._is_pre_startup_event(event)
            or not self._should_process_message(room, event)
        ):
            return
        attachment, marker = await self._fetch_media_attachment(room, event)
        parts: list[str] = []
        if isinstance(body := getattr(event, "body", None), str) and body.strip():
            parts.append(body.strip())

        # 音频附件尝试转写，失败则使用占位标记
        if attachment and attachment.get("type") == "audio":
            transcription = await self.transcribe_audio(attachment["path"])
            if transcription:
                parts.append(f"[transcription: {transcription}]")
            else:
                parts.append(marker)
        elif marker:
            parts.append(marker)

        await self._start_typing_keepalive(room.room_id)
        try:
            meta = self._base_metadata(room, event)
            meta["attachments"] = []
            if attachment:
                meta["attachments"] = [attachment]
            await self._handle_message(
                sender_id=event.sender, chat_id=room.room_id,
                content="\n".join(parts),
                media=[attachment["path"]] if attachment else [],
                metadata=meta,
                is_dm=self._is_direct_room(room),
            )
        except Exception:
            await self._stop_typing_keepalive(room.room_id, clear_typing=True)
            raise
