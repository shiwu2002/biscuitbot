"""Microsoft Teams 渠道 MVP 实现，使用内置的小型 HTTP webhook 服务器。

所属模块与项目作用
====================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 Microsoft Teams 平台组件。
在项目架构中起到的作用：通过内置 HTTP webhook 服务器接收 Bot Framework 活动事件，
将 Teams 的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：内置 ThreadingHTTPServer 监听 Bot Framework webhook（默认 /api/messages）。
- 鉴权：可选校验入站 Bot Framework bearer token（JWT，通过 JWKS 验证签名）；
  出站使用 client_credentials 流获取 access_token 调用 Bot Framework REST API。
- 会话引用持久化：ConversationRef 存储 service_url/conversation_id 等，持久化到工作区 state 目录，
  支持跨进程文件锁、TTL 过期清理、原子写入。
- 当前范围（MVP）：聚焦 DM（个人会话），文本入站/出站，暂不支持附件/卡片/投票。
- 安全：trusted_service_url_hosts 白名单防止 SSRF；可选裁剪 webchat 和非 personal 会话引用。
- 线程回复：支持 reply_in_thread，回复时携带 replyToId 形成话题线程。
- 文本归一化：剥离 <at> mention 标记，归一化 Teams 引用回复引用块。
"""

from __future__ import annotations

import asyncio  # 异步事件循环与跨线程调度
import html  # HTML 实体反转义（Teams 文本归一化）
import importlib.util  # 运行时检测可选依赖
import json  # JSON 序列化/反序列化
import os  # 文件描述符与原子替换
import re  # 正则表达式（mention 剥离、空白归一化）
import tempfile  # 临时文件（原子写入）
import threading  # 线程（HTTP 服务器、文件锁）
import time  # 时间戳（TTL、token 过期）
from contextlib import contextmanager, suppress  # 上下文管理器与异常抑制
from dataclasses import dataclass  # 数据类装饰器
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # 内置 HTTP 服务器
from typing import TYPE_CHECKING, Any  # 类型注解支持
from urllib.parse import urlparse  # URL 解析（service_url 校验）

try:  # pragma: no cover - Windows fallback path
    import fcntl  # Unix 文件锁（Windows 无此模块）
except ImportError:  # pragma: no cover
    fcntl = None

import httpx  # 异步 HTTP 客户端
from pydantic import Field  # Pydantic 模型字段定义

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_workspace_path  # 工作区路径
from biscuitbot.config.schema import Base  # 配置模型基类

# 检测 PyJWT 和 cryptography 是否安装（用于入站 token 校验）
MSTEAMS_AVAILABLE = (
    importlib.util.find_spec("jwt") is not None
    and importlib.util.find_spec("cryptography") is not None
)

if TYPE_CHECKING:
    import jwt  # 仅类型检查时导入

if MSTEAMS_AVAILABLE:
    import jwt

MSTEAMS_REF_TTL_DAYS = 30  # 会话引用默认 TTL（天）
MSTEAMS_WEBCHAT_HOST = "webchat.botframework.com"  # Web Chat 主机名（不支持，默认裁剪）
MSTEAMS_DEFAULT_TRUSTED_SERVICE_URL_HOSTS = [  # 默认受信 service_url 主机白名单（防 SSRF）
    "smba.trafficmanager.net",
    "smba.infra.gcc.teams.microsoft.com",
    "smba.infra.gov.teams.microsoft.us",
    "smba.infra.dod.teams.microsoft.us",
    "*.botframework.com",
]
MSTEAMS_REF_META_FILENAME = "msteams_conversations_meta.json"  # 会话引用元数据文件名（存储 updated_at）
MSTEAMS_REF_LOCK_FILENAME = "msteams_conversations.lock"  # 跨进程文件锁文件名
MSTEAMS_REF_TOUCH_INTERVAL_S = 300  # 会话引用活跃刷新最小间隔（秒）


class MSTeamsConfig(Base):
    """Microsoft Teams 渠道配置。"""

    enabled: bool = False
    app_id: str = ""  # Azure Bot 应用 ID
    app_password: str = ""  # Azure Bot 应用密码
    tenant_id: str = ""  # 租户 ID（为空时使用 botframework.com）
    host: str = "0.0.0.0"  # webhook 监听地址
    port: int = 3978  # webhook 监听端口
    path: str = "/api/messages"  # webhook 路径
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    reply_in_thread: bool = True  # 是否以线程回复形式发送
    mention_only_response: str = "Hi — what can I help with?"  # 仅@无文本时的默认回复
    validate_inbound_auth: bool = True  # 是否校验入站 bearer token
    ref_ttl_days: int = Field(default=MSTEAMS_REF_TTL_DAYS, ge=1)  # 会话引用 TTL（天）
    prune_web_chat_refs: bool = True  # 是否裁剪 Web Chat 会话引用
    prune_non_personal_refs: bool = True  # 是否裁剪非 personal 会话引用
    ref_touch_interval_s: int = Field(default=MSTEAMS_REF_TOUCH_INTERVAL_S, ge=0)  # 引用活跃刷新间隔
    trusted_service_url_hosts: list[str] = Field(
        default_factory=lambda: MSTEAMS_DEFAULT_TRUSTED_SERVICE_URL_HOSTS.copy()
    )  # 受信 service_url 主机白名单


@dataclass
class ConversationRef:
    """用于回复的最小化会话引用存储。"""

    service_url: str
    conversation_id: str
    bot_id: str | None = None
    activity_id: str | None = None
    conversation_type: str | None = None
    tenant_id: str | None = None
    updated_at: float | None = None


class MSTeamsChannel(BaseChannel):
    """Microsoft Teams 渠道（DM 优先 MVP）。"""

    name = "msteams"
    display_name = "Microsoft Teams"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return MSTeamsConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = MSTeamsConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: MSTeamsConfig = config
        self._loop: asyncio.AbstractEventLoop | None = None  # 主事件循环引用（跨线程调度用）
        self._server: ThreadingHTTPServer | None = None  # HTTP webhook 服务器
        self._server_thread: threading.Thread | None = None  # 服务器线程
        self._http: httpx.AsyncClient | None = None  # 出站 HTTP 客户端
        self._token: str | None = None  # Bot Framework access_token 缓存
        self._token_expires_at: float = 0.0  # access_token 过期时间
        self._botframework_openid_config_url = (
            "https://login.botframework.com/v1/.well-known/openidconfiguration"
        )  # Bot Framework OpenID 配置 URL
        self._botframework_openid_config: dict[str, Any] | None = None  # OpenID 配置缓存
        self._botframework_openid_config_expires_at: float = 0.0  # OpenID 配置过期时间
        self._botframework_jwks: dict[str, Any] | None = None  # JWKS（签名密钥）缓存
        self._botframework_jwks_expires_at: float = 0.0  # JWKS 过期时间
        self._refs_path = get_workspace_path() / "state" / "msteams_conversations.json"  # 会话引用存储路径
        self._refs_path.parent.mkdir(parents=True, exist_ok=True)
        self._refs_meta_path = self._refs_path.parent / MSTEAMS_REF_META_FILENAME  # 元数据路径
        self._refs_lock_path = self._refs_path.parent / MSTEAMS_REF_LOCK_FILENAME  # 文件锁路径
        self._refs_guard = threading.RLock()  # 进程内会话引用读写锁
        self._conversation_refs: dict[str, ConversationRef] = self._load_refs()  # 会话引用字典
        # 初始化时清理过期引用
        with self._refs_guard:
            if self._prune_conversation_refs():
                self._save_refs_locked(prune=True)

    async def start(self) -> None:
        """启动 Teams webhook 监听服务器。"""
        if not MSTEAMS_AVAILABLE:
            self.logger.error("PyJWT not installed. Run: pip install biscuitbot[msteams]")
            return

        if not self.config.app_id or not self.config.app_password:
            self.logger.error("app_id/app_password not configured")
            return

        if not self.config.validate_inbound_auth:
            self.logger.warning(
                "Inbound auth validation was explicitly DISABLED in config. "
                "Anyone who knows the webhook URL can send messages as any user. "
                "Only disable this for local development or controlled testing."
            )

        self._loop = asyncio.get_running_loop()
        self._http = httpx.AsyncClient(timeout=30.0)
        self._running = True

        channel = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                # 路径不匹配返回 404
                if self.path != channel.config.path:
                    self.send_response(404)
                    self.end_headers()
                    return

                # 解析请求体
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length > 0 else b"{}"
                    payload = json.loads(raw.decode("utf-8"))
                except Exception as e:
                    channel.logger.warning("Invalid request body: {}", e)
                    self.send_response(400)
                    self.end_headers()
                    return

                # 校验入站 bearer token（可选）
                auth_header = self.headers.get("Authorization", "")
                if channel.config.validate_inbound_auth:
                    try:
                        fut = asyncio.run_coroutine_threadsafe(
                            channel._validate_inbound_auth(auth_header, payload),
                            channel._loop,
                        )
                        fut.result(timeout=15)
                    except Exception as e:
                        channel.logger.warning("Inbound auth validation failed: {}", e)
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"unauthorized"}')
                        return
                # 处理活动事件（跨线程调度到主事件循环）
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        channel._handle_activity(payload),
                        channel._loop,
                    )
                    fut.result(timeout=15)
                except Exception as e:
                    channel.logger.warning("Activity handling failed: {}", e)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format: str, *args: Any) -> None:
                # 抑制默认的 HTTP 访问日志
                return

        self._server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="biscuitbot-msteams",
            daemon=True,
        )
        self._server_thread.start()

        self.logger.info(
            "Webhook listening on http://{}:{}{}",
            self.config.host,
            self.config.port,
            self.config.path,
        )

        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止渠道，关闭 webhook 服务器和 HTTP 客户端。"""
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=2)
        self._server_thread = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        """向已存在的 Teams 会话发送纯文本回复。"""
        if not self._http:
            raise RuntimeError("MSTeams HTTP client not initialized")

        ref = self._conversation_refs.get(str(msg.chat_id))
        if not ref:
            raise RuntimeError(f"MSTeams conversation ref not found for chat_id={msg.chat_id}")

        # 安全校验：service_url 必须在白名单内，防止 SSRF
        if not self._is_trusted_service_url(ref.service_url):
            raise RuntimeError(
                f"MSTeams conversation ref has untrusted service_url for chat_id={msg.chat_id}"
            )

        token = await self._get_access_token()
        base_url = f"{ref.service_url.rstrip('/')}/v3/conversations/{ref.conversation_id}/activities"
        use_thread_reply = self.config.reply_in_thread and bool(ref.activity_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "type": "message",
            "text": msg.content or " ",
        }
        # 线程回复：携带 replyToId
        if use_thread_reply:
            payload["replyToId"] = ref.activity_id

        try:
            resp = await self._http.post(base_url, headers=headers, json=payload)
            resp.raise_for_status()
            self.logger.info("Message sent to {}", ref.conversation_id)
            # 刷新会话引用活跃时间，避免 TTL 过期
            self._touch_conversation_ref(str(msg.chat_id), persist=True)
        except Exception:
            self.logger.exception("Send failed")
            raise

    async def _handle_activity(self, activity: dict[str, Any]) -> None:
        """处理入站 Teams/Bot Framework 活动。"""
        if activity.get("type") != "message":
            return

        conversation = activity.get("conversation") or {}
        from_user = activity.get("from") or {}
        recipient = activity.get("recipient") or {}
        channel_data = activity.get("channelData") or {}

        sender_id = str(from_user.get("aadObjectId") or from_user.get("id") or "").strip()
        conversation_id = str(conversation.get("id") or "").strip()
        service_url = str(activity.get("serviceUrl") or "").strip()
        activity_id = str(activity.get("id") or "").strip()
        conversation_type = str(conversation.get("conversationType") or "").strip()

        if not sender_id or not conversation_id or not service_url:
            return

        # 安全校验：service_url 主机必须受信
        if not self._is_trusted_service_url(service_url):
            self.logger.warning(
                "Ignoring MSTeams activity with untrusted serviceUrl host: {}",
                service_url,
            )
            return

        # 跳过机器人自身发的消息
        if recipient.get("id") and from_user.get("id") == recipient.get("id"):
            return

        # DM-only MVP：忽略群组/频道消息
        if conversation_type and conversation_type not in ("personal", ""):
            self.logger.debug("Ignoring non-DM conversation {}", conversation_type)
            return

        text = self._sanitize_inbound_text(activity)
        # 无文本时使用默认回复（如仅@机器人）
        if not text:
            text = self.config.mention_only_response.strip()
            if not text:
                self.logger.debug("Ignoring empty message after Teams text sanitization")
                return

        if not self.is_allowed(sender_id):
            self.logger.warning(
                "Access denied for sender {} on channel {}. "
                "Add them to allowFrom list in config to grant access.",
                sender_id, self.name,
            )
            return

        # 保存会话引用，用于后续回复
        with self._refs_guard:
            self._conversation_refs[conversation_id] = ConversationRef(
                service_url=service_url,
                conversation_id=conversation_id,
                bot_id=str(recipient.get("id") or "") or None,
                activity_id=activity_id or None,
                conversation_type=conversation_type or None,
                tenant_id=str((channel_data.get("tenant") or {}).get("id") or "") or None,
                updated_at=time.time(),
            )
            self._save_refs_locked()

        await self._handle_message(
            sender_id=sender_id,
            chat_id=conversation_id,
            content=text,
            metadata={
                "msteams": {
                    "activity_id": activity_id,
                    "conversation_id": conversation_id,
                    "conversation_type": conversation_type or "personal",
                    "from_name": from_user.get("name"),
                }
            },
        )

    def _sanitize_inbound_text(self, activity: dict[str, Any]) -> str:
        """从 Teams 活动中提取用户编写的文本（剥离 mention 和引用块）。"""
        text = str(activity.get("text") or "")
        text = self._strip_possible_bot_mention(text)
        text = self._normalize_html_whitespace(text)

        channel_data = activity.get("channelData") or {}
        reply_to_id = str(activity.get("replyToId") or "").strip()
        normalized_preview = html.unescape(text).replace("&rsquo", "’").strip()
        normalized_preview = normalized_preview.replace("\xa0", " ")
        normalized_preview = normalized_preview.replace("\r\n", "\n").replace("\r", "\n")
        preview_lines = [line.strip() for line in normalized_preview.split("\n")]
        while preview_lines and not preview_lines[0]:
            preview_lines.pop(0)
        first_line = preview_lines[0] if preview_lines else ""
        looks_like_quote_wrapper = first_line.lower().startswith("replying to ") or first_line.startswith("Reply wrapper")

        # 若为回复消息，归一化引用块
        if reply_to_id or channel_data.get("messageType") == "reply" or looks_like_quote_wrapper:
            text = self._normalize_teams_reply_quote(text)

        return text.strip()

    def _strip_possible_bot_mention(self, text: str) -> str:
        """从消息文本中移除简单的 Teams mention 标记（<at>...</at>）。"""
        cleaned = re.sub(r"<at\b[^>]*>.*?</at>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"[^\S\r\n]+", " ", cleaned)
        cleaned = re.sub(r"(?:\r?\n){3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _normalize_html_whitespace(self, text: str) -> str:
        """将 Teams 常见的 HTML 空白/实体归一化为纯文本空格。"""
        normalized = html.unescape(text).replace("&rsquo", "’")
        normalized = normalized.replace("\xa0", " ")
        return normalized

    def _normalize_teams_reply_quote(self, text: str) -> str:
        """将 Teams 引用回复归一化为紧凑的结构化形式。

        处理多种观察到的回复包装格式：
        - "Replying to <name>\n<reply>" 原生格式
        - "Reply wrapper" 头 + 引用块 + 回复
        - 紧凑单行回退格式
        """
        cleaned = self._normalize_html_whitespace(text).strip()
        if not cleaned:
            return ""

        normalized_newlines = cleaned.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in normalized_newlines.split("\n")]
        while lines and not lines[0]:
            lines.pop(0)

        # 原生 Teams 回复包装：首行 "Replying to <name>"，后续为实际回复
        if len(lines) >= 2 and lines[0].lower().startswith("replying to "):
            quoted = lines[0][len("replying to ") :].strip(" :")
            reply = "\n".join(lines[1:]).strip()
            return self._format_reply_with_quote(quoted, reply)

        # "Reply wrapper" 头格式：引用内容在头之后，可能含空行分隔
        if lines and lines[0].strip().startswith("Reply wrapper"):
            body = normalized_newlines.split("\n", 1)[1] if "\n" in normalized_newlines else ""
            body = body.lstrip()
            # 尝试按空行分隔引用与回复
            parts = re.split(r"\n\s*\n", body, maxsplit=1)
            if len(parts) == 2:
                quoted = re.sub(r"\s+", " ", parts[0]).strip()
                reply = re.sub(r"\s+", " ", parts[1]).strip()
                if quoted or reply:
                    return self._format_reply_with_quote(quoted, reply)

            # 回退：最后一行为回复，其余为引用
            body_lines = [line.strip() for line in body.split("\n") if line.strip()]
            if body_lines:
                quoted = " ".join(body_lines[:-1]).strip()
                reply = body_lines[-1].strip()
                if quoted and reply:
                    return self._format_reply_with_quote(quoted, reply)

        # 紧凑单行回退：引用与回复被压平到一行
        compact = re.sub(r"\s+", " ", normalized_newlines).strip()
        if compact.startswith("Reply wrapper "):
            compact = compact[len("Reply wrapper ") :].strip()
            # 按句末标点切分引用与回复
            for boundary in (". ", "! ", "? ", "… "):
                idx = compact.rfind(boundary)
                if idx == -1:
                    continue
                quoted = compact[: idx + 1].strip()
                reply = compact[idx + len(boundary) :].strip()
                if quoted and reply and len(reply) <= 160:
                    return self._format_reply_with_quote(quoted, reply)

        return cleaned

    def _format_reply_with_quote(self, quoted: str, reply: str) -> str:
        """格式化带上下文的回复消息（去除 Teams 包装噪声，供模型理解）。"""
        quoted = quoted.strip()
        reply = reply.strip()
        if quoted and reply:
            return f"User is replying to: {quoted}\nUser reply: {reply}"
        if reply:
            return reply
        return quoted

    async def _validate_inbound_auth(self, auth_header: str, activity: dict[str, Any]) -> None:
        """校验入站 Bot Framework bearer token（JWT）。"""
        if not MSTEAMS_AVAILABLE:
            raise RuntimeError("PyJWT not installed. Run: pip install biscuitbot[msteams]")

        if not auth_header.lower().startswith("bearer "):
            raise ValueError("missing bearer token")

        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            raise ValueError("empty bearer token")

        # 从 token 头取 kid，匹配 JWKS 中的签名密钥
        header = jwt.get_unverified_header(token)
        kid = str(header.get("kid") or "").strip()
        if not kid:
            raise ValueError("missing token kid")

        jwks = await self._get_botframework_jwks()
        keys = jwks.get("keys") or []
        jwk = next((key for key in keys if key.get("kid") == kid), None)
        if not jwk:
            raise ValueError(f"signing key not found for kid={kid}")

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=self.config.app_id,
            issuer="https://api.botframework.com",
            options={
                "require": ["exp", "nbf", "iss", "aud"],
            },
        )

        # 校验 serviceUrl 声明与活动一致
        claim_service_url = str(
            claims.get("serviceurl") or claims.get("serviceUrl") or "",
        ).strip()
        activity_service_url = str(activity.get("serviceUrl") or "").strip()
        if claim_service_url and activity_service_url and claim_service_url != activity_service_url:
            raise ValueError("serviceUrl claim mismatch")

    async def _get_botframework_openid_config(self) -> dict[str, Any]:
        """获取并缓存 Bot Framework OpenID 配置。"""

        now = time.time()
        if self._botframework_openid_config and now < self._botframework_openid_config_expires_at:
            return self._botframework_openid_config

        if not self._http:
            raise RuntimeError("MSTeams HTTP client not initialized")

        resp = await self._http.get(self._botframework_openid_config_url)
        resp.raise_for_status()
        self._botframework_openid_config = resp.json()
        self._botframework_openid_config_expires_at = now + 3600  # 缓存 1 小时
        return self._botframework_openid_config

    async def _get_botframework_jwks(self) -> dict[str, Any]:
        """获取并缓存 Bot Framework JWKS（JSON Web Key Set）。"""

        now = time.time()
        if self._botframework_jwks and now < self._botframework_jwks_expires_at:
            return self._botframework_jwks

        if not self._http:
            raise RuntimeError("MSTeams HTTP client not initialized")

        openid_config = await self._get_botframework_openid_config()
        jwks_uri = str(openid_config.get("jwks_uri") or "").strip()
        if not jwks_uri:
            raise RuntimeError("Bot Framework OpenID config missing jwks_uri")

        resp = await self._http.get(jwks_uri)
        resp.raise_for_status()
        self._botframework_jwks = resp.json()
        self._botframework_jwks_expires_at = now + 3600  # 缓存 1 小时
        return self._botframework_jwks

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """安全转换为正浮点数，失败返回 None。"""
        try:
            out = float(value)
            if out > 0:
                return out
        except (TypeError, ValueError):
            return None
        return None

    def _normalize_ref_record(self, value: Any) -> ConversationRef | None:
        """将存储的 ref 记录从旧/新 schema 归一化为 ConversationRef。"""
        if not isinstance(value, dict):
            return None
        service_url = str(value.get("service_url") or "").strip()
        conversation_id = str(value.get("conversation_id") or "").strip()
        if not service_url or not conversation_id:
            return None
        return ConversationRef(
            service_url=service_url,
            conversation_id=conversation_id,
            bot_id=str(value.get("bot_id") or "") or None,
            activity_id=str(value.get("activity_id") or "") or None,
            conversation_type=str(value.get("conversation_type") or "") or None,
            tenant_id=str(value.get("tenant_id") or "") or None,
            updated_at=self._safe_float(value.get("updated_at")),
        )

    def _load_refs_raw(self) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """加载原始 refs 主数据 + 元数据 JSON payload。"""
        main_data: dict[str, Any] = {}
        meta_data: dict[str, Any] = {}
        meta_exists = self._refs_meta_path.exists()

        if self._refs_path.exists():
            try:
                loaded = json.loads(self._refs_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    main_data = loaded
            except Exception as e:
                self.logger.warning("Failed to load conversation refs: {}", e)

        if meta_exists:
            try:
                loaded_meta = json.loads(self._refs_meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded_meta, dict):
                    meta_data = loaded_meta
            except Exception as e:
                self.logger.warning("Failed to load conversation refs metadata: {}", e)

        return main_data, meta_data, meta_exists

    def _load_refs_from_disk(self) -> dict[str, ConversationRef]:
        """从磁盘加载 refs，兼容旧版布局。"""
        main_data, meta_data, meta_exists = self._load_refs_raw()
        if not main_data:
            return {}

        out: dict[str, ConversationRef] = {}
        now = time.time()
        for key, value in main_data.items():
            ref = self._normalize_ref_record(value)
            if not ref:
                continue

            # 元数据中可能存有更精确的 updated_at
            meta_entry = meta_data.get(key) if isinstance(meta_data, dict) else None
            meta_ts = None
            if isinstance(meta_entry, dict):
                meta_ts = self._safe_float(meta_entry.get("updated_at"))
            elif meta_entry is not None:
                meta_ts = self._safe_float(meta_entry)

            if meta_ts is not None:
                ref.updated_at = meta_ts
            elif not meta_exists:
                # 首次引入元数据 sidecar 后：用 "now" 初始化旧 ref 的时间戳，
                # 避免立即被清理。
                ref.updated_at = now
            elif ref.updated_at is None:
                ref.updated_at = now

            out[key] = ref
        return out

    def _load_refs(self) -> dict[str, ConversationRef]:
        """加载已存储的会话引用。"""
        return self._load_refs_from_disk()

    @contextmanager
    def _refs_file_lock(self):
        """跨进程文件锁（合并并写入 refs 状态时使用）。"""
        self._refs_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fp = self._refs_lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            finally:
                lock_fp.close()

    def _is_webchat_service_url(self, service_url: str) -> bool:
        """判断 service_url 是否指向不支持的 Bot Framework Web Chat。"""
        normalized = service_url.strip()
        if not normalized:
            return False
        host = (urlparse(normalized).hostname or "").strip().lower()
        if host:
            return host == MSTEAMS_WEBCHAT_HOST or host.endswith(f".{MSTEAMS_WEBCHAT_HOST}")
        return MSTEAMS_WEBCHAT_HOST in normalized.lower()

    def _is_trusted_service_url(self, service_url: str) -> bool:
        """判断是否为受信的 HTTPS Bot Framework service_url（允许 bearer 回复）。

        支持通配符模式（如 *.botframework.com）。
        """
        parsed = urlparse(service_url.strip())
        if parsed.scheme.lower() != "https":
            return False

        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False

        for pattern in self.config.trusted_service_url_hosts:
            trusted_host = str(pattern or "").strip().lower().rstrip(".")
            if not trusted_host:
                continue
            # 通配符 *.example.com 匹配子域
            if trusted_host.startswith("*."):
                suffix = trusted_host[1:]
                if host.endswith(suffix) and host != suffix.lstrip("."):
                    return True
                continue
            if host == trusted_host:
                return True
        return False

    def _prune_conversation_refs(self, *, now: float | None = None) -> bool:
        """从内存中移除过期和不支持的会话引用。"""
        if not self._conversation_refs:
            return False

        now_ts = time.time() if now is None else now
        ttl_days = int(self.config.ref_ttl_days)
        stale_before = now_ts - (ttl_days * 24 * 60 * 60)
        keys_to_drop: list[str] = []

        for key, ref in self._conversation_refs.items():
            # 不受信的 service_url 直接清理
            if not self._is_trusted_service_url(ref.service_url):
                keys_to_drop.append(key)
                continue

            # Web Chat 引用按配置清理
            if self.config.prune_web_chat_refs and self._is_webchat_service_url(ref.service_url):
                keys_to_drop.append(key)
                continue

            # 非 personal 会话引用按配置清理
            conv_type = str(ref.conversation_type or "").strip().lower()
            if self.config.prune_non_personal_refs and conv_type and conv_type != "personal":
                keys_to_drop.append(key)
                continue

            # TTL 过期清理
            try:
                updated_at = float(ref.updated_at) if ref.updated_at is not None else 0.0
            except (TypeError, ValueError):
                updated_at = 0.0
            if updated_at <= 0 or updated_at < stale_before:
                keys_to_drop.append(key)

        if not keys_to_drop:
            return False

        for key in keys_to_drop:
            self._conversation_refs.pop(key, None)
        self.logger.info(
            "Pruned {} stale/unsupported conversation refs (ttl={} days)",
            len(keys_to_drop),
            ttl_days,
        )
        return True

    def _merge_refs_from_disk_locked(self) -> None:
        """将磁盘 refs 合并到内存，减少跨进程更新丢失。"""
        disk_refs = self._load_refs_from_disk()
        for key, disk_ref in disk_refs.items():
            mem_ref = self._conversation_refs.get(key)
            if mem_ref is None:
                self._conversation_refs[key] = disk_ref
                continue
            # 以 updated_at 较新者为准
            disk_ts = self._safe_float(disk_ref.updated_at) or 0.0
            mem_ts = self._safe_float(mem_ref.updated_at) or 0.0
            if disk_ts > mem_ts:
                self._conversation_refs[key] = disk_ref

    def _touch_conversation_ref(self, chat_id: str, *, persist: bool = False) -> None:
        """刷新活跃 ref 的 updated_at，避免使用中过期。"""
        with self._refs_guard:
            ref = self._conversation_refs.get(str(chat_id))
            if not ref:
                return
            now = time.time()
            prev = self._safe_float(ref.updated_at) or 0.0
            min_interval = max(0, int(self.config.ref_touch_interval_s))
            # 限流：距离上次刷新不足间隔则跳过
            if min_interval > 0 and prev > 0 and now - prev < min_interval:
                return
            ref.updated_at = now
            if persist:
                self._save_refs_locked()

    def _write_json_atomically(self, path, data: dict[str, Any]) -> None:
        """原子写入 refs JSON，降低崩溃导致损坏的风险。"""
        payload = json.dumps(data, indent=2)
        tmp_path: str | None = None
        try:
            # 写入临时文件后原子替换
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=f"{path.name}.",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                with suppress(OSError):
                    os.unlink(tmp_path)

    def _save_refs_locked(self, *, prune: bool = True) -> None:
        """持久化会话引用（调用方必须持有 _refs_guard）。"""
        try:
            with self._refs_file_lock():
                # 合并磁盘最新内容，避免覆盖其他进程的更新
                self._merge_refs_from_disk_locked()
                if prune:
                    self._prune_conversation_refs()
                refs_data = {
                    key: {
                        "service_url": ref.service_url,
                        "conversation_id": ref.conversation_id,
                        "bot_id": ref.bot_id,
                        "activity_id": ref.activity_id,
                        "conversation_type": ref.conversation_type,
                        "tenant_id": ref.tenant_id,
                    }
                    for key, ref in self._conversation_refs.items()
                }
                # 元数据单独存储（updated_at），与主数据分离
                refs_meta = {
                    key: {
                        "updated_at": self._safe_float(ref.updated_at),
                    }
                    for key, ref in self._conversation_refs.items()
                }
                self._write_json_atomically(self._refs_path, refs_data)
                self._write_json_atomically(self._refs_meta_path, refs_meta)
        except Exception as e:
            self.logger.warning("Failed to save conversation refs: {}", e)

    def _save_refs(self, *, prune: bool = True) -> None:
        """持久化会话引用。"""
        with self._refs_guard:
            self._save_refs_locked(prune=prune)

    async def _get_access_token(self) -> str:
        """通过 client_credentials 流获取 Bot Framework / Azure Bot 的 access_token。"""

        now = time.time()
        # 缓存有效（提前 60 秒刷新）
        if self._token and now < self._token_expires_at - 60:
            return self._token

        if not self._http:
            raise RuntimeError("MSTeams HTTP client not initialized")

        tenant = (self.config.tenant_id or "").strip() or "botframework.com"
        token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.config.app_id,
            "client_secret": self.config.app_password,
            "scope": "https://api.botframework.com/.default",
        }
        resp = await self._http.post(token_url, data=data)
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = now + int(payload.get("expires_in", 3600))
        return self._token
