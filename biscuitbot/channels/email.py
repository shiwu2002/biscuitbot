"""邮件（Email）渠道实现，入站使用 IMAP 轮询，出站使用 SMTP 回复。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的邮件平台组件。
在项目架构中起到的作用：将电子邮件的收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：入站通过 IMAP 协议轮询指定邮箱的未读邮件；出站通过 SMTP 协议回复发件人。
  无需长连接，适合低频、异步的对话场景。
- 鉴权：使用 IMAP/SMTP 账号密码登录邮箱服务器，支持 SSL/TLS。
- 反伪造：校验邮件 ``Authentication-Results`` 头中的 SPF/DKIM 通过状态，防止伪造发件人。
- 会话：以发件人邮箱地址作为 chat_id，回复时自动带 ``In-Reply-To``/``References`` 头。
- 附件：支持按 MIME 类型白名单提取附件，限制单文件大小与数量。
- 后处理：可配置邮件处理后的动作（删除 / 移动到指定邮箱），支持 UID STORE/EXPUNGE。
- 历史检索：提供按日期区间拉取邮件的能力，用于「昨天的邮件」等历史总结任务。
- 去重：通过 UID 集合与 ``\\Seen`` 标记双重去重，避免重复处理。
"""

import asyncio
import html
import imaplib
import mimetypes
import re
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import Field

from biscuitbot.bus.events import OutboundMessage
from biscuitbot.bus.queue import MessageBus
from biscuitbot.channels.base import BaseChannel
from biscuitbot.config.paths import get_media_dir
from biscuitbot.config.schema import Base
from biscuitbot.utils.helpers import safe_filename


class EmailConfig(Base):
    """邮件渠道配置（入站 IMAP + 出站 SMTP）。"""

    enabled: bool = False
    consent_granted: bool = False  # 用户是否明确授权（必须为 True 才会真正启用）

    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"  # 轮询的邮箱文件夹
    imap_use_ssl: bool = True

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True  # 是否使用 STARTTLS
    smtp_use_ssl: bool = False  # 是否使用隐式 SSL
    from_address: str = ""  # 发件人地址

    auto_reply_enabled: bool = True  # 是否允许自动回复
    poll_interval_seconds: int = 30  # 轮询间隔（秒）
    mark_seen: bool = True  # 处理后是否标记为已读
    post_action: Literal["delete", "move"] | None = None  # 处理后动作：删除/移动
    post_action_move_mailbox: str | None = None  # 移动目标邮箱
    post_action_expunge: bool = False  # 是否在移动/删除后执行 EXPUNGE
    post_action_ignore_skipped: bool = True  # 被跳过的邮件是否也应用后处理动作
    max_body_chars: int = 12000  # 邮件正文最大字符数
    subject_prefix: str = "Re: "  # 回复主题前缀
    allow_from: list[str] = Field(default_factory=list)  # 允许的发件人白名单

    # 邮件认证校验（防伪造）
    verify_dkim: bool = True   # 要求 Authentication-Results 中 dkim=pass
    verify_spf: bool = True    # 要求 Authentication-Results 中 spf=pass

    # 附件处理 —— 设置允许的 MIME 类型以启用（如 ["application/pdf", "image/*"]，或 ["*"] 表示全部）
    allowed_attachment_types: list[str] = Field(default_factory=list)
    max_attachment_size: int = 2_000_000  # 单个附件大小上限（2MB）
    max_attachments_per_email: int = 5  # 每封邮件附件数量上限


@dataclass
class _ServerFeatures:
    """IMAP 服务器能力探测结果。"""

    move: bool  # 是否支持 MOVE 扩展
    uidplus: bool  # 是否支持 UIDPLUS 扩展
    uid_store: bool | None = None  # 会话级：UID STORE 是否可用（None 表示尚未探测）


class EmailChannel(BaseChannel):
    """邮件渠道。

    入站：轮询 IMAP 邮箱获取未读邮件，将每封邮件转换为入站事件。
    出站：通过 SMTP 将回复发送回发件人地址。
    """

    name = "email"
    display_name = "Email"
    _IMAP_MONTHS = (  # IMAP 日期搜索使用的英文月份缩写
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    _IMAP_RECONNECT_MARKERS = (  # 触发重连的过期连接错误特征字符串
        "disconnected for inactivity",
        "eof occurred in violation of protocol",
        "socket error",
        "connection reset",
        "broken pipe",
        "bye",
    )
    _IMAP_MISSING_MAILBOX_MARKERS = (  # 邮箱不存在的错误特征字符串
        "mailbox doesn't exist",
        "select failed",
        "no such mailbox",
        "can't open mailbox",
        "does not exist",
    )

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置。"""
        return EmailConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        """初始化邮件渠道，收集本账号地址并准备去重与上下文缓存。"""
        if isinstance(config, dict):
            config = EmailConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: EmailConfig = config
        self._self_addresses = self._collect_self_addresses()  # 本机器人拥有的邮箱地址集合
        self._last_subject_by_chat: dict[str, str] = {}  # 各会话最近主题（用于回复主题）
        self._last_message_id_by_chat: dict[str, str] = {}  # 各会话最近 Message-ID（用于邮件线程头）
        self._processed_uids: set[str] = set()  # 已处理 UID 集合，有上限以防无限增长
        self._MAX_PROCESSED_UIDS = 100000  # 已处理 UID 集合的容量上限

    async def start(self) -> None:
        """启动 IMAP 轮询循环，拉取未读邮件并处理。"""
        if not self.config.consent_granted:
            self.logger.warning(
                "Email channel disabled: consent_granted is false. "
                "Set channels.email.consentGranted=true after explicit user permission."
            )
            return

        if not self._validate_config():
            return

        self._running = True
        if not self.config.verify_dkim and not self.config.verify_spf:
            self.logger.warning(
                "DKIM and SPF verification are both DISABLED. "
                "Emails with spoofed From headers will be accepted. "
                "Set verify_dkim=true and verify_spf=true for anti-spoofing protection."
            )
        self.logger.info("Starting Email channel (IMAP polling mode)...")

        poll_seconds = max(5, int(self.config.poll_interval_seconds))
        while self._running:
            try:
                inbound_items, skipped_uids = await asyncio.to_thread(self._fetch_new_messages)
                should_apply_post_action = self._should_apply_post_action()
                post_actions_uids: set[str] = set()
                for item in inbound_items:
                    sender = item["sender"]
                    subject = item.get("subject", "")
                    message_id = item.get("message_id", "")

                    if subject:
                        self._last_subject_by_chat[sender] = subject
                    if message_id:
                        self._last_message_id_by_chat[sender] = message_id

                    try:
                        await self._handle_message(
                            sender_id=sender,
                            chat_id=sender,
                            content=item["content"],
                            media=item.get("media") or None,
                            metadata=item.get("metadata", {}),
                        )
                    except Exception:
                        self.logger.exception("Error delivering email from {}", sender)
                        continue

                    uid = str((item.get("metadata") or {}).get("uid") or "")
                    if uid and should_apply_post_action:
                        post_actions_uids.add(uid)

                if should_apply_post_action and not self.config.post_action_ignore_skipped:
                    post_actions_uids.update(skipped_uids)

                if post_actions_uids:
                    await asyncio.to_thread(self._apply_post_actions_batch, sorted(post_actions_uids))
            except Exception:
                self.logger.exception("Polling error")

            await asyncio.sleep(poll_seconds)

    async def stop(self) -> None:
        """停止轮询循环。"""
        self._running = False

    async def send(self, msg: OutboundMessage) -> None:
        """通过 SMTP 发送邮件。"""
        if not self.config.consent_granted:
            self.logger.warning("Skip email send: consent_granted is false")
            return

        if not self.config.smtp_host:
            self.logger.warning("SMTP host not configured")
            return

        # 跳过进度消息，避免每次工具调用后发送空邮件
        if (msg.metadata or {}).get("_progress"):
            self.logger.debug("Skip progress message to {}", msg.chat_id)
            return

        to_addr = msg.chat_id.strip()
        if not to_addr:
            self.logger.warning("Missing recipient address")
            return

        # 判断是否为回复（收件人此前给我们发过邮件）
        is_reply = to_addr in self._last_subject_by_chat
        force_send = bool((msg.metadata or {}).get("force_send"))

        # autoReplyEnabled 仅控制自动回复，不影响主动发送
        if is_reply and not self.config.auto_reply_enabled and not force_send:
            self.logger.info("Skip automatic reply to {}: auto_reply_enabled is false", to_addr)
            return

        base_subject = self._last_subject_by_chat.get(to_addr, "biscuitbot reply")
        subject = self._reply_subject(base_subject)
        if msg.metadata and isinstance(msg.metadata.get("subject"), str):
            override = msg.metadata["subject"].strip()
            if override:
                subject = override

        attachments: list[tuple[bytes, str, str, str]] = []
        failed_attachments: list[str] = []
        max_attachment_size = max(0, int(self.config.max_attachment_size))
        max_attachment_count = max(0, int(self.config.max_attachments_per_email))
        for media_path in msg.media or []:
            path = Path(media_path)
            filename = path.name or "attachment"
            if len(attachments) >= max_attachment_count:
                failed_attachments.append(f"[attachment: {filename} - too many attachments]")
                self.logger.warning("Attachment count limit reached, skipping: {}", media_path)
                continue
            if not path.is_file():
                failed_attachments.append(f"[attachment: {filename} - send failed]")
                self.logger.warning("Attachment not found, skipping: {}", media_path)
                continue
            try:
                size = path.stat().st_size
                if max_attachment_size <= 0 or size > max_attachment_size:
                    failed_attachments.append(f"[attachment: {filename} - too large]")
                    self.logger.warning(
                        "Attachment too large, skipping: {} ({} > {} bytes)",
                        media_path,
                        size,
                        max_attachment_size,
                    )
                    continue
                data = path.read_bytes()
                ctype, _ = mimetypes.guess_type(str(path))
                if ctype is None:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                attachments.append((data, maintype, subtype, filename))
                self.logger.info("Attached file: {}", filename)
            except Exception:
                failed_attachments.append(f"[attachment: {filename} - send failed]")
                self.logger.exception("Failed to attach file {}", media_path)

        content = msg.content or ""
        if failed_attachments:
            fallback = "\n".join(failed_attachments)
            content = f"{content.rstrip()}\n\n{fallback}" if content.strip() else fallback

        email_msg = EmailMessage()
        email_msg["From"] = self.config.from_address or self.config.smtp_username or self.config.imap_username
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject
        email_msg.set_content(content)

        for data, maintype, subtype, filename in attachments:
            email_msg.add_attachment(
                data,
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

        in_reply_to = self._last_message_id_by_chat.get(to_addr)
        if in_reply_to:
            email_msg["In-Reply-To"] = in_reply_to  # 邮件线程头，便于客户端归类
            email_msg["References"] = in_reply_to

        try:
            await asyncio.to_thread(self._smtp_send, email_msg)
        except Exception:
            self.logger.exception("Error sending to {}", to_addr)
            raise

    def _validate_config(self) -> bool:
        """校验必需配置项是否齐全，返回是否通过。"""
        missing = []
        if not self.config.imap_host:
            missing.append("imap_host")
        if not self.config.imap_username:
            missing.append("imap_username")
        if not self.config.imap_password:
            missing.append("imap_password")
        if not self.config.smtp_host:
            missing.append("smtp_host")
        if not self.config.smtp_username:
            missing.append("smtp_username")
        if not self.config.smtp_password:
            missing.append("smtp_password")

        if self.config.post_action == "move" and not (self.config.post_action_move_mailbox or "").strip():
            missing.append("post_action_move_mailbox")

        if missing:
            self.logger.error("Channel not configured, missing: {}", ', '.join(missing))
            return False
        return True

    def _smtp_send(self, msg: EmailMessage) -> None:
        """通过 SMTP 发送邮件（同步实现，供 ``asyncio.to_thread`` 调用）。"""
        timeout = 30
        if self.config.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
            ) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=timeout) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(msg)

    def _fetch_new_messages(self) -> tuple[list[dict[str, Any]], set[str]]:
        """轮询 IMAP，返回解析后的未读邮件列表与被跳过邮件的 UID 集合。"""
        return self._fetch_messages(
            search_criteria=("UNSEEN",),
            mark_seen=self.config.mark_seen,
            dedupe=True,
            limit=0,
        )

    def fetch_messages_between_dates(
        self,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """按 IMAP 日期搜索拉取 [start_date, end_date) 区间内的邮件。

        用于历史总结任务（如「昨天的邮件」）。
        """
        if end_date <= start_date:
            return []

        messages, _ = self._fetch_messages(
            search_criteria=(
                "SINCE",
                self._format_imap_date(start_date),
                "BEFORE",
                self._format_imap_date(end_date),
            ),
            mark_seen=False,
            dedupe=False,
            limit=max(1, int(limit)),
        )
        return messages

    def _fetch_messages(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """拉取邮件（带过期连接重试一次）。"""
        messages: list[dict[str, Any]] = []
        skipped_uids: set[str] = set()
        cycle_uids: set[str] = set()

        for attempt in range(2):
            try:
                self._fetch_messages_once(
                    search_criteria,
                    mark_seen,
                    dedupe,
                    limit,
                    messages,
                    skipped_uids,
                    cycle_uids,
                )
                return messages, skipped_uids
            except Exception as exc:
                if attempt == 1 or not self._is_stale_imap_error(exc):
                    raise
                self.logger.warning("IMAP connection went stale, retrying once: {}", exc)

        return messages, skipped_uids

    def _fetch_messages_once(
        self,
        search_criteria: tuple[str, ...],
        mark_seen: bool,
        dedupe: bool,
        limit: int,
        messages: list[dict[str, Any]],
        skipped_uids: set[str],
        cycle_uids: set[str],
    ) -> None:
        """按任意 IMAP 搜索条件拉取邮件（单次尝试）。"""
        mailbox = self.config.imap_mailbox or "INBOX"

        client = self._open_imap_client(mailbox=mailbox, missing_mailbox_ok=True)
        if client is None:
            return messages

        try:
            status, data = client.search(None, *search_criteria)
            if status != "OK" or not data:
                return messages

            ids = data[0].split()
            if limit > 0 and len(ids) > limit:
                ids = ids[-limit:]  # 只取最新的 limit 封
            for imap_id in ids:
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue

                raw_bytes = self._extract_message_bytes(fetched)
                if raw_bytes is None:
                    continue

                uid = self._extract_uid(fetched)
                if uid and uid in cycle_uids:  # 本轮已处理，跳过
                    continue
                if dedupe and uid and uid in self._processed_uids:  # 历史已处理，跳过
                    continue

                parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
                if not sender:
                    continue
                if self._is_self_address(sender):  # 自己发的邮件，忽略
                    self.logger.info("From {} ignored: matches bot-owned address", sender)
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    if uid:
                        skipped_uids.add(uid)
                    continue

                # --- 反伪造：校验 Authentication-Results ---
                spf_pass, dkim_pass = self._check_authentication_results(parsed)
                if self.config.verify_spf and not spf_pass:
                    self.logger.warning(
                        "From {} rejected: SPF verification failed "
                        "(no 'spf=pass' in Authentication-Results header)",
                        sender,
                    )
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if uid:
                        skipped_uids.add(uid)
                    continue
                if self.config.verify_dkim and not dkim_pass:
                    self.logger.warning(
                        "From {} rejected: DKIM verification failed "
                        "(no 'dkim=pass' in Authentication-Results header)",
                        sender,
                    )
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if uid:
                        skipped_uids.add(uid)
                    continue

                if not self.is_allowed(sender):  # 不在白名单内
                    self._remember_processed_uid(uid, dedupe, cycle_uids)
                    if mark_seen:
                        client.store(imap_id, "+FLAGS", "\\Seen")
                    if uid:
                        skipped_uids.add(uid)
                    continue

                subject = self._decode_header_value(parsed.get("Subject", ""))
                date_value = parsed.get("Date", "")
                message_id = parsed.get("Message-ID", "").strip()
                body = self._extract_text_body(parsed)

                if not body:
                    body = "(empty email body)"

                body = body[: self.config.max_body_chars]  # 截断超长正文
                content = (
                    f"[EMAIL-CONTEXT] Email received.\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date_value}\n\n"
                    f"{body}"
                )

                # --- 附件提取 ---
                attachment_paths: list[str] = []
                if self.config.allowed_attachment_types:
                    saved = self._extract_attachments(
                        parsed,
                        uid or "noid",
                        allowed_types=self.config.allowed_attachment_types,
                        max_size=self.config.max_attachment_size,
                        max_count=self.config.max_attachments_per_email,
                    )
                    for p in saved:
                        attachment_paths.append(str(p))
                        content += f"\n[attachment: {p.name} — saved to {p}]"

                metadata = {
                    "message_id": message_id,
                    "subject": subject,
                    "date": date_value,
                    "sender_email": sender,
                    "uid": uid,
                }
                messages.append(
                    {
                        "sender": sender,
                        "subject": subject,
                        "message_id": message_id,
                        "content": content,
                        "metadata": metadata,
                        "media": attachment_paths,
                    }
                )

                self._remember_processed_uid(uid, dedupe, cycle_uids)

                if mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")
        finally:
            self._close_imap_client(client)

    def _open_imap_client(self, mailbox: str, *, missing_mailbox_ok: bool = False) -> Any | None:
        """打开并登录 IMAP 客户端，选择指定邮箱。"""
        if self.config.imap_use_ssl:
            client: Any = imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port)
        else:
            client = imaplib.IMAP4(self.config.imap_host, self.config.imap_port)

        try:
            client.login(self.config.imap_username, self.config.imap_password)
            try:
                status, _ = client.select(mailbox)
            except Exception as exc:
                if missing_mailbox_ok and self._is_missing_mailbox_error(exc):
                    self.logger.warning("Mailbox unavailable, skipping poll for {}: {}", mailbox, exc)
                    self._close_imap_client(client)
                    return None
                raise

            if status != "OK":
                self.logger.warning("Mailbox select returned {}, skipping poll for {}", status, mailbox)
                self._close_imap_client(client)
                return None
        except Exception:
            self._close_imap_client(client)
            raise

        return client

    @staticmethod
    def _close_imap_client(client: Any) -> None:
        """安全关闭 IMAP 客户端。"""
        with suppress(Exception):
            client.logout()

    def _collect_self_addresses(self) -> set[str]:
        """返回本渠道实例拥有的已归一化邮箱地址集合。"""
        candidates = (
            self.config.from_address,
            self.config.smtp_username,
            self.config.imap_username,
        )
        normalized = {
            addr
            for candidate in candidates
            if (addr := self._normalize_address(candidate))
        }
        return normalized

    @staticmethod
    def _normalize_address(value: str) -> str:
        """归一化邮箱地址或类邮箱标识，便于比较。"""
        raw = (value or "").strip()
        if not raw:
            return ""
        parsed = parseaddr(raw)[1].strip().lower()
        if parsed:
            return parsed
        if "@" in raw:
            return raw.lower()
        return ""

    def _is_self_address(self, sender: str) -> bool:
        """判断入站发件人是否属于本机器人自身。"""
        normalized_sender = self._normalize_address(sender)
        return bool(normalized_sender) and normalized_sender in self._self_addresses

    def _remember_processed_uid(self, uid: str, dedupe: bool, cycle_uids: set[str]) -> None:
        """记录已拉取的 UID，避免被跳过的邮件被反复处理。"""
        if not uid:
            return
        cycle_uids.add(uid)
        if dedupe:
            self._processed_uids.add(uid)
            # mark_seen 是主要去重手段，此集合为安全网
            if len(self._processed_uids) > self._MAX_PROCESSED_UIDS:
                # 超限时随机淘汰一半以控制内存；mark_seen 仍为主要去重
                self._processed_uids = set(list(self._processed_uids)[len(self._processed_uids) // 2:])

    def _should_apply_post_action(self) -> bool:
        """是否配置了有效的后处理动作。"""
        return self.config.post_action in {"delete", "move"}

    def _apply_post_actions_batch(self, post_actions_uids: list[str]) -> None:
        """在一个 IMAP 会话内批量应用后处理动作。"""
        if not self._should_apply_post_action() or not post_actions_uids:
            return

        mailbox = self.config.imap_mailbox or "INBOX"
        client = self._open_imap_client(mailbox=mailbox)
        if client is None:
            return

        try:
            features = self._server_features(client)
            # 在单个 IMAP 会话内应用所有后处理动作。``features`` 还携带会话内习得的行为
            # （如 UID STORE 是否可用），使后续 UID 可跳过已知不可用的路径。
            for uid in post_actions_uids:
                if uid:
                    self._apply_post_action(client, uid, features)
        finally:
            self._close_imap_client(client)

    def _apply_post_action(
        self,
        client: Any,
        uid: str,
        features: _ServerFeatures,
    ) -> None:
        """对单个 UID 应用后处理动作（删除或移动）。"""
        action = self.config.post_action

        if action == "delete":
            if not self._uid_store_deleted(client, uid, features):
                return
            self._uid_expunge_or_fallback(client, uid, features)
            return

        if action == "move":
            target = (self.config.post_action_move_mailbox or "").strip()
            if features.move:
                status, _ = client.uid("MOVE", uid, target)
                if status != "OK":
                    self.logger.warning("Post-action move failed (UID MOVE) for UID {} to mailbox {}", uid, target)
                return

            # 不支持 MOVE 时用 COPY + STORE \Deleted 模拟
            status, _ = client.uid("COPY", uid, target)
            if status != "OK":
                self.logger.warning("Post-action move failed (UID COPY) for UID {} to mailbox {}", uid, target)
                return
            if not self._uid_store_deleted(client, uid, features):
                return
            self._uid_expunge_or_fallback(client, uid, features)

    @staticmethod
    def _server_features(client: Any) -> _ServerFeatures:
        """探测 IMAP 服务器能力（MOVE / UIDPLUS）。"""
        caps: set[str] = set()
        with suppress(Exception):
            status, data = client.capability()
            if status == "OK" and data:
                for raw in data:
                    if isinstance(raw, (bytes, bytearray)):
                        caps.update(token.upper() for token in raw.decode("utf-8", errors="ignore").split())
                    elif isinstance(raw, str):
                        caps.update(token.upper() for token in raw.split())
        return _ServerFeatures(move="MOVE" in caps, uidplus="UIDPLUS" in caps)

    @staticmethod
    def _lookup_imap_id_by_uid(client: Any, uid: str) -> bytes | None:
        # IMAP 暴露两种消息标识：UID（稳定）与序列号（会话内本地）。
        # 优先按 UID 定位，但部分服务器可能拒绝 UID STORE。此时根据 UID 解析当前
        # 序列号，再用该序列号通过 STORE 重试。
        status, data = client.search(None, "UID", uid)
        if status != "OK" or not data or not data[0]:
            return None
        return data[0].split()[0]

    def _uid_store_deleted(self, client: Any, uid: str, features: _ServerFeatures) -> bool:
        # 乐观路径：优先尝试 UID STORE，因为 UID 稳定且无需序列号查找。
        # 若会话内首次失败，则记下并对其余 UID 直接使用序列号 STORE 回退。
        if features.uid_store is not False:
            status, _ = client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            if status == "OK":
                features.uid_store = True
                return True
            features.uid_store = False

        # 兼容回退：针对不支持或不可靠的 UID STORE 服务器，按 UID 解析当前序列号后用 STORE。
        imap_id = self._lookup_imap_id_by_uid(client, uid)
        if not imap_id:
            self.logger.warning("Post-action skipped: UID {} not found", uid)
            return False

        status, _ = client.store(imap_id, "+FLAGS", "\\Deleted")
        if status != "OK":
            self.logger.warning("Post-action failed: could not mark UID {} as deleted", uid)
            return False
        return True

    def _uid_expunge_or_fallback(self, client: Any, uid: str, features: _ServerFeatures) -> None:
        # 支持时优先使用 UID 范围 EXPUNGE，避免清除本邮箱中其他已标记 \Deleted 的无关邮件。
        if features.uidplus:
            status, _ = client.uid("EXPUNGE", uid)
            if status == "OK":
                return
            self.logger.warning("UID EXPUNGE failed for UID {}, falling back to EXPUNGE", uid)
        if self.config.post_action_expunge:
            client.expunge()

    @classmethod
    def _is_stale_imap_error(cls, exc: Exception) -> bool:
        """判断异常是否为可重试的过期连接错误。"""
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_RECONNECT_MARKERS)

    @classmethod
    def _is_missing_mailbox_error(cls, exc: Exception) -> bool:
        """判断异常是否为邮箱不存在错误。"""
        message = str(exc).lower()
        return any(marker in message for marker in cls._IMAP_MISSING_MAILBOX_MARKERS)

    @classmethod
    def _format_imap_date(cls, value: date) -> str:
        """格式化为 IMAP 搜索所用的日期（始终使用英文月份缩写）。"""
        month = cls._IMAP_MONTHS[value.month - 1]
        return f"{value.day:02d}-{month}-{value.year}"

    @staticmethod
    def _extract_message_bytes(fetched: list[Any]) -> bytes | None:
        """从 fetch 结果中提取邮件原始字节。"""
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return None

    @staticmethod
    def _extract_uid(fetched: list[Any]) -> str:
        """从 fetch 结果中提取 UID。"""
        for item in fetched:
            if isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                head = bytes(item[0]).decode("utf-8", errors="ignore")
                m = re.search(r"UID\s+(\d+)", head)
                if m:
                    return m.group(1)
        return ""

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """解码邮件头值（处理编码字）。"""
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def _extract_text_body(cls, msg: Any) -> str:
        """尽力提取可读的邮件正文文本。"""
        if msg.is_multipart():
            plain_parts: list[str] = []
            html_parts: list[str] = []
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                content_type = part.get_content_type()
                try:
                    payload = part.get_content()
                except Exception:
                    payload_bytes = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    payload = payload_bytes.decode(charset, errors="replace")
                if not isinstance(payload, str):
                    continue
                if content_type == "text/plain":
                    plain_parts.append(payload)
                elif content_type == "text/html":
                    html_parts.append(payload)
            if plain_parts:
                return "\n\n".join(plain_parts).strip()
            if html_parts:
                return cls._html_to_text("\n\n".join(html_parts)).strip()
            return ""

        try:
            payload = msg.get_content()
        except Exception:
            payload_bytes = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = payload_bytes.decode(charset, errors="replace")
        if not isinstance(payload, str):
            return ""
        if msg.get_content_type() == "text/html":
            return cls._html_to_text(payload).strip()
        return payload.strip()

    @staticmethod
    def _check_authentication_results(parsed_msg: Any) -> tuple[bool, bool]:
        """解析 Authentication-Results 头中的 SPF 与 DKIM 判定结果。

        Returns:
            (spf_pass, dkim_pass) 布尔元组。
        """
        spf_pass = False
        dkim_pass = False
        for ar_header in parsed_msg.get_all("Authentication-Results") or []:
            ar_lower = ar_header.lower()
            if re.search(r"\bspf\s*=\s*pass\b", ar_lower):
                spf_pass = True
            if re.search(r"\bdkim\s*=\s*pass\b", ar_lower):
                dkim_pass = True
        return spf_pass, dkim_pass

    @classmethod
    def _extract_attachments(
        cls,
        msg: Any,
        uid: str,
        *,
        allowed_types: list[str],
        max_size: int,
        max_count: int,
    ) -> list[Path]:
        """提取邮件附件并保存到媒体目录，返回已保存文件路径列表。"""
        if not msg.is_multipart():
            return []

        saved: list[Path] = []
        media_dir = get_media_dir("email")

        for part in msg.walk():
            if len(saved) >= max_count:
                break
            if part.get_content_disposition() != "attachment":
                continue

            content_type = part.get_content_type()
            if not any(fnmatch(content_type, pat) for pat in allowed_types):
                logger.debug("Attachment skipped (type {}): not in allowed list", content_type)
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            if len(payload) > max_size:
                logger.warning(
                    "Attachment skipped: size {} exceeds limit {}",
                    len(payload),
                    max_size,
                )
                continue

            raw_name = part.get_filename() or "attachment"
            sanitized = safe_filename(raw_name) or "attachment"
            dest = media_dir / f"{uid}_{sanitized}"

            try:
                dest.write_bytes(payload)
                saved.append(dest)
                logger.info("Attachment saved: {}", dest)
            except Exception as exc:
                logger.warning("Failed to save attachment {}: {}", dest, exc)

        return saved

    @staticmethod
    def _html_to_text(raw_html: str) -> str:
        """将 HTML 粗略转换为纯文本。"""
        text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text)

    def _reply_subject(self, base_subject: str) -> str:
        """生成回复主题，已有 Re: 前缀则保留，否则加上前缀。"""
        subject = (base_subject or "").strip() or "biscuitbot reply"
        prefix = self.config.subject_prefix or "Re: "
        if subject.lower().startswith("re:"):
            return subject
        return f"{prefix}{subject}"
