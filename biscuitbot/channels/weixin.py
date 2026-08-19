"""个人微信（WeChat）渠道实现，使用 HTTP 长轮询 API。

所属模块与项目作用
==================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的个人微信平台组件。
在项目架构中起到的作用：通过 ilinkai.weixin.qq.com API 将个人微信的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：使用 HTTP 长轮询（long-poll），无需 WebSocket，也无需本地微信客户端。
- 鉴权：通过扫码登录获取 bot token，后续使用该 token 进行 API 调用。
- 协议来源：逆向自 ``@tencent-weixin/openclaw-weixin`` v1.0.3。
- 媒体处理：支持图片、语音、视频、文件的下载（AES-128-ECB 解密）与上传（AES-128-ECB 加密）。
- 打字指示器：支持打字状态推送与保活循环，提升用户体验。
- 会话管理：支持 context_token 缓存与过期刷新，防止长时间无活动后消息丢失。
- 消息去重：基于 message_id 的 OrderedDict 去重，防止重复处理。
- 速率限制：工具提示合并发送，避免触及微信 iLink 速率限制（约 7 条/5 分钟）。
"""

from __future__ import annotations  # 延迟注解求值，允许类型注解引用尚未定义的类型

import asyncio  # 异步事件循环与并发原语
import base64  # base64 编解码（AES 密钥处理、微信 UIN 生成）
import hashlib  # 哈希计算（文件 MD5 校验）
import json  # JSON 序列化/反序列化（状态持久化、消息解析）
import os  # 操作系统接口（随机数生成、路径处理）
import random  # 随机数（打字票据 TTL 抖动）
import re  # 正则表达式（AES 密钥格式校验）
import time  # 时间相关（令牌过期、打字保活）
import uuid  # UUID 生成（客户端 ID、消息标识）
from collections import OrderedDict  # 有序字典（消息去重 LRU 缓存）
from contextlib import suppress  # 上下文管理器：忽略指定异常
from pathlib import Path  # 路径处理（媒体文件、状态文件）
from typing import Any  # 类型注解支持
from urllib.parse import quote  # URL 编码（CDN 下载参数）

import httpx  # 异步 HTTP 客户端（API 调用、文件下载/上传）
from loguru import logger  # 日志记录器（模块级函数用）
from pydantic import Field  # Pydantic 模型字段定义

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_media_dir, get_runtime_subdir  # 媒体目录与运行时子目录获取
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.pairing import clear_channel  # 重新扫码登录后清空配对授权
from biscuitbot.utils.helpers import split_message  # 消息分块工具

# ---------------------------------------------------------------------------
# 协议常量（来自 openclaw-weixin types.ts）
# ---------------------------------------------------------------------------

# 消息项类型（MessageItemType）
ITEM_TEXT = 1  # 文本
ITEM_IMAGE = 2  # 图片
ITEM_VOICE = 3  # 语音
ITEM_FILE = 4  # 文件
ITEM_VIDEO = 5  # 视频

# 消息类型（MessageType：1 = 用户发送的入站消息，2 = 机器人发送的出站消息）
MESSAGE_TYPE_BOT = 2

# 消息状态（MessageState）
MESSAGE_STATE_FINISH = 2  # 完成

WEIXIN_MAX_MESSAGE_LEN = 4000  # 微信单条消息最大长度（留安全余量）
WEIXIN_CHANNEL_VERSION = "2.1.1"  # 渠道协议版本
ILINK_APP_ID = "bot"  # iLink 应用 ID


def _build_client_version(version: str) -> int:
    """将语义化版本编码为 0x00MMNNPP（主版本/次版本/补丁合入一个 uint32）。"""
    parts = version.split(".")

    def _as_int(idx: int) -> int:
        try:
            return int(parts[idx])
        except Exception:
            return 0

    major = _as_int(0)
    minor = _as_int(1)
    patch = _as_int(2)
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)

ILINK_APP_CLIENT_VERSION = _build_client_version(WEIXIN_CHANNEL_VERSION)  # 编码后的客户端版本号
BASE_INFO: dict[str, str] = {"channel_version": WEIXIN_CHANNEL_VERSION}  # 每个请求携带的基础信息

# 会话过期错误码
ERRCODE_SESSION_EXPIRED = -14
SESSION_PAUSE_DURATION_S = 60 * 60  # 会话过期后的暂停时长（1 小时）

# iLink context_token 在代理无活动约 90-160 秒后会在服务端过期
# （openclaw/openclaw#61174）。在发送前若缓存的令牌超过此阈值则主动刷新。
CONTEXT_TOKEN_MAX_AGE_S = 60


# 重试常量（匹配参考插件的 monitor.ts）
MAX_CONSECUTIVE_FAILURES = 3  # 最大连续失败次数，超过后进入退避
BACKOFF_DELAY_S = 30  # 退避延迟（秒）
RETRY_DELAY_S = 2  # 普通重试延迟（秒）
MAX_QR_REFRESH_COUNT = 3  # 二维码最大刷新次数
TYPING_STATUS_TYPING = 1  # 打字状态：正在输入
TYPING_STATUS_CANCEL = 2  # 打字状态：取消输入
TYPING_TICKET_TTL_S = 24 * 60 * 60  # 打字票据有效期（24 小时）
TYPING_KEEPALIVE_INTERVAL_S = 5  # 打字保活间隔（秒）
CONFIG_CACHE_INITIAL_RETRY_S = 2  # 配置缓存初始重试延迟（秒）
CONFIG_CACHE_MAX_RETRY_S = 60 * 60  # 配置缓存最大重试延迟（1 小时）

# 默认长轮询超时；可被服务端通过 longpolling_timeout_ms 覆盖
DEFAULT_LONG_POLL_TIMEOUT_S = 35

# getuploadurl 的媒体类型代码（1=图片, 2=视频, 3=文件, 4=语音）
UPLOAD_MEDIA_IMAGE = 1
UPLOAD_MEDIA_VIDEO = 2
UPLOAD_MEDIA_FILE = 3
UPLOAD_MEDIA_VOICE = 4

# 出站媒体按扩展名分类的图片/视频/语音集合
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".ico", ".svg"}
_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"}
_VOICE_EXTS = {".mp3", ".wav", ".amr", ".silk", ".ogg", ".m4a", ".aac", ".flac"}

# 登录代数：每次「扫码连接」重新登录（换账号/换设备）时 +1。
# 主渠道实例据此检测到新登录并重新加载 account.json，避免继续用旧账号 token 轮询；
# 登录会话实例据此避免旧账号状态覆盖磁盘上刚写入的新 token。
_session_generation = 0


def _has_downloadable_media_locator(media: dict[str, Any] | None) -> bool:
    """检查媒体字典是否包含可下载的定位信息（encrypt_query_param 或 full_url）。"""
    if not isinstance(media, dict):
        return False
    return bool(str(media.get("encrypt_query_param", "") or "") or str(media.get("full_url", "") or "").strip())


class _TokenInvalidated(Exception):
    """内部信号：微信 bot token 已被服务端作废（errcode -14），需重新扫码登录。"""


class WeixinConfig(Base):
    """个人微信渠道配置。"""

    enabled: bool = False  # 是否启用
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    base_url: str = "https://ilinkai.weixin.qq.com"  # iLink API 基础 URL
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"  # CDN 基础 URL（媒体下载/上传）
    route_tag: str | int | None = None  # 路由标签（SKRouteTag 头）
    token: str = ""  # 手动设置的 token，或通过扫码登录获取
    state_dir: str = ""  # 状态目录（默认：~/.biscuitbot/weixin/）
    poll_timeout: int = DEFAULT_LONG_POLL_TIMEOUT_S  # 长轮询超时（秒）


class WeixinChannel(BaseChannel):
    """个人微信渠道，使用 HTTP 长轮询。

    连接 ilinkai.weixin.qq.com API 收发个人微信消息。
    通过扫码登录获取 bot token 进行鉴权。
    """

    name = "weixin"
    display_name = "微信"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典，用于渠道注册时生成默认配置。"""
        return WeixinConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WeixinConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WeixinConfig = config

        # 运行时状态
        self._client: httpx.AsyncClient | None = None  # 异步 HTTP 客户端
        self._get_updates_buf: str = ""  # 长轮询游标（增量拉取位置）
        self._context_tokens: dict[str, str] = {}  # from_user_id -> context_token（回复必需）
        self._processed_ids: OrderedDict[str, None] = OrderedDict()  # 已处理消息 ID 去重（LRU）
        self._state_dir: Path | None = None  # 状态目录路径
        self._token: str = ""  # 微信 bot token
        self._poll_task: asyncio.Task | None = None  # 轮询任务
        self._next_poll_timeout_s: int = DEFAULT_LONG_POLL_TIMEOUT_S  # 下次轮询超时（可被服务端覆盖）
        self._session_pause_until: float = 0.0  # 会话暂停截止时间（过期后暂停轮询）
        self._failed_token: str = ""  # 最近一次因 errcode -14 失效的 token，用于识别磁盘上是否有新 token
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> 打字保活任务
        self._typing_tickets: dict[str, dict[str, Any]] = {}  # chat_id -> 打字票据缓存
        self._context_token_at: dict[str, float] = {}  # chat_id -> context_token 缓存时间戳
        self._pending_tool_hints: dict[str, list[str]] = {}  # chat_id -> 待刷新的工具提示列表
        self._seen_generation: int = _session_generation  # 本实例已同步到的登录代数

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _get_state_dir(self) -> Path:
        """获取（必要时创建）状态目录，用于保存 token 等账户状态。"""
        if self._state_dir:
            return self._state_dir
        if self.config.state_dir:
            d = Path(self.config.state_dir).expanduser()
        else:
            d = get_runtime_subdir("weixin")
        d.mkdir(parents=True, exist_ok=True)
        self._state_dir = d
        return d

    def _load_state(self) -> bool:
        """从磁盘加载已保存的账户状态。若找到有效 token 则返回 True。"""
        state_file = self._get_state_dir() / "account.json"
        if not state_file.exists():
            return False
        try:
            data = json.loads(state_file.read_text())
            self._token = data.get("token", "")
            self._get_updates_buf = data.get("get_updates_buf", "")
            context_tokens = data.get("context_tokens", {})
            if isinstance(context_tokens, dict):
                self._context_tokens = {
                    str(user_id): str(token)
                    for user_id, token in context_tokens.items()
                    if str(user_id).strip() and str(token).strip()
                }
            else:
                self._context_tokens = {}
            typing_tickets = data.get("typing_tickets", {})
            if isinstance(typing_tickets, dict):
                self._typing_tickets = {
                    str(user_id): ticket
                    for user_id, ticket in typing_tickets.items()
                    if str(user_id).strip() and isinstance(ticket, dict)
                }
            else:
                self._typing_tickets = {}
            base_url = data.get("base_url", "")
            if base_url:
                self.config.base_url = base_url
            return bool(self._token)
        except Exception:
            self.logger.error("Failed to load Weixin account state", exc_info=True)
            return False

    def _save_state(self) -> None:
        """将账户状态（token、游标、context_token 等）持久化到磁盘。"""
        if self._seen_generation != _session_generation:
            # 一次新的扫码登录正在进行/已完成，本实例持有的还是旧账号状态，
            # 不要用它覆盖磁盘上刚写入的新 token（否则换账号后仍停留在旧账号）。
            return
        state_file = self._get_state_dir() / "account.json"
        with suppress(Exception):
            data = {
                "token": self._token,
                "get_updates_buf": self._get_updates_buf,
                "context_tokens": self._context_tokens,
                "typing_tickets": self._typing_tickets,
                "base_url": self.config.base_url,
            }
            state_file.write_text(json.dumps(data, ensure_ascii=False))
            # 限制文件权限：该文件保存了微信访问 token
            try:
                state_file.chmod(0o600)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # HTTP 辅助方法（对应 api.ts 的 buildHeaders / apiFetch）
    # ------------------------------------------------------------------

    @staticmethod
    def _random_wechat_uin() -> str:
        """X-WECHAT-UIN：随机 uint32 → 十进制字符串 → base64。

        匹配参考插件 api.ts 中的 ``randomWechatUin()``。
        每次请求都重新生成（与参考实现一致）。
        """
        uint32 = int.from_bytes(os.urandom(4), "big")
        return base64.b64encode(str(uint32).encode()).decode()

    def _make_headers(self, *, auth: bool = True) -> dict[str, str]:
        """构建每次请求的 HTTP 头（每次调用生成新的 UIN，与参考实现一致）。"""
        headers: dict[str, str] = {
            "X-WECHAT-UIN": self._random_wechat_uin(),
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self.config.route_tag is not None and str(self.config.route_tag).strip():
            headers["SKRouteTag"] = str(self.config.route_tag).strip()
        return headers

    @staticmethod
    def _is_retryable_media_download_error(err: Exception) -> bool:
        """判断媒体下载错误是否可重试（超时、传输错误、5xx 服务端错误）。"""
        if isinstance(err, httpx.TimeoutException | httpx.TransportError):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            status_code = err.response.status_code if err.response is not None else 0
            return status_code >= 500
        return False

    async def _api_get(
        self,
        endpoint: str,
        params: dict | None = None,
        *,
        auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """发送 GET 请求到 iLink API（使用配置的 base_url）。"""
        assert self._client is not None
        url = f"{self.config.base_url}/{endpoint}"
        hdrs = self._make_headers(auth=auth)
        if extra_headers:
            hdrs.update(extra_headers)
        resp = await self._client.get(url, params=params, headers=hdrs)
        resp.raise_for_status()
        return resp.json()

    async def _api_get_with_base(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict | None = None,
        auth: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """GET 辅助方法，允许覆盖 base_url（用于二维码重定向轮询）。"""
        assert self._client is not None
        url = f"{base_url.rstrip('/')}/{endpoint}"
        hdrs = self._make_headers(auth=auth)
        if extra_headers:
            hdrs.update(extra_headers)
        resp = await self._client.get(url, params=params, headers=hdrs)
        resp.raise_for_status()
        return resp.json()

    async def _api_post(
        self,
        endpoint: str,
        body: dict | None = None,
        *,
        auth: bool = True,
    ) -> dict:
        """发送 POST 请求到 iLink API，自动注入 base_info。"""
        assert self._client is not None
        url = f"{self.config.base_url}/{endpoint}"
        payload = body or {}
        if "base_info" not in payload:
            payload["base_info"] = BASE_INFO
        resp = await self._client.post(url, json=payload, headers=self._make_headers(auth=auth))
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 二维码登录（对应 login-qr.ts）
    # ------------------------------------------------------------------

    async def _fetch_qr_code(self) -> tuple[str, str]:
        """获取新的二维码。返回 (qrcode_id, scan_url)。"""
        data = await self._api_get(
            "ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            auth=False,
        )
        qrcode_img_content = data.get("qrcode_img_content", "")
        qrcode_id = data.get("qrcode", "")
        if not qrcode_id:
            raise RuntimeError(f"Failed to get QR code from WeChat API: {data}")
        return qrcode_id, (qrcode_img_content or qrcode_id)

    async def _qr_login(self) -> bool:
        """执行二维码登录流程。成功返回 True。"""
        try:
            refresh_count = 0
            qrcode_id, scan_url = await self._fetch_qr_code()
            self._print_qr_code(scan_url)
            current_poll_base_url = self.config.base_url

            while self._running:
                try:
                    status_data = await self._api_get_with_base(
                        base_url=current_poll_base_url,
                        endpoint="ilink/bot/get_qrcode_status",
                        params={"qrcode": qrcode_id},
                        auth=False,
                    )
                except Exception as e:
                    if self._is_retryable_qr_poll_error(e):
                        await asyncio.sleep(1)
                        continue
                    raise

                if not isinstance(status_data, dict):
                    await asyncio.sleep(1)
                    continue

                status = status_data.get("status", "")
                if status == "confirmed":
                    # 扫码确认成功，提取 token 并保存
                    token = status_data.get("bot_token", "")
                    bot_id = status_data.get("ilink_bot_id", "")
                    base_url = status_data.get("baseurl", "")
                    user_id = status_data.get("ilink_user_id", "")
                    if token:
                        self._token = token
                        if base_url:
                            self.config.base_url = base_url
                        self._save_state()
                        # 全新登录：清空配对授权，让下一次私聊重新进入配对码流程。
                        clear_channel(self.name)
                        self.logger.info(
                            "login successful! bot_id={} user_id={}",
                            bot_id,
                            user_id,
                        )
                        return True
                    else:
                        self.logger.error("Login confirmed but no bot_token in response")
                        return False
                elif status == "scaned_but_redirect":
                    # 已扫码但需要重定向到新的主机
                    redirect_host = str(status_data.get("redirect_host", "") or "").strip()
                    if redirect_host:
                        if redirect_host.startswith("http://") or redirect_host.startswith("https://"):
                            redirected_base = redirect_host
                        else:
                            redirected_base = f"https://{redirect_host}"
                        if redirected_base != current_poll_base_url:
                            current_poll_base_url = redirected_base
                elif status == "expired":
                    # 二维码过期，刷新新二维码
                    refresh_count += 1
                    if refresh_count > MAX_QR_REFRESH_COUNT:
                        self.logger.warning(
                            "QR code expired too many times ({}/{}), giving up.",
                            refresh_count - 1,
                            MAX_QR_REFRESH_COUNT,
                        )
                        return False
                    qrcode_id, scan_url = await self._fetch_qr_code()
                    current_poll_base_url = self.config.base_url
                    self._print_qr_code(scan_url)
                    continue
                # status == "wait" — 继续轮询等待扫码

                await asyncio.sleep(1)

        except Exception:
            self.logger.exception("QR login failed")

        return False

    @staticmethod
    def _is_retryable_qr_poll_error(err: Exception) -> bool:
        """判断二维码轮询错误是否可重试（超时、传输错误、5xx 服务端错误）。"""
        if isinstance(err, httpx.TimeoutException | httpx.TransportError):
            return True
        if isinstance(err, httpx.HTTPStatusError):
            status_code = err.response.status_code if err.response is not None else 0
            if status_code >= 500:
                return True
        return False

    @staticmethod
    def _print_qr_code(url: str) -> None:
        """在终端打印 ASCII 二维码供用户扫描（依赖 qrcode 库）。"""
        try:
            import qrcode as qr_lib

            qr = qr_lib.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"\nLogin URL: {url}\n")

    # ------------------------------------------------------------------
    # WebUI 扫码登录（非阻塞，供引导界面逐次轮询）
    # ------------------------------------------------------------------

    async def fetch_login_qr(self) -> dict[str, str]:
        """获取一张登录二维码，返回待编码进二维码的字符串（不进入阻塞轮询）。

        供 WebUI 引导界面调用：一次性拿到 ``qrcode_id`` 与 ``qr_content``，
        前端用任意二维码库把 ``qr_content`` 渲染成二维码供用户扫描。

        每次调用都会先清除旧 token（含磁盘 account.json），确保这是一次全新登录，
        用于「切换设备 / 重新连接」场景：点「扫码连接」即重新生成 token 连接新设备。
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(6, connect=15),
                follow_redirects=True,
            )
        qrcode_id, qr_content = await self._fetch_qr_code()
        # 拿到新二维码后再推进登录代数并清除旧 token：每次「扫码连接」都是一次全新登录
        # （连接新设备）。主渠道实例据此停止用旧账号轮询、等待新 token 写入。
        global _session_generation
        _session_generation += 1
        self._seen_generation = _session_generation
        self._invalidate_token()
        return {"qrcode_id": qrcode_id, "qr_content": qr_content}

    async def poll_login_qr(self, qrcode_id: str) -> dict[str, Any]:
        """单次轮询登录状态（供 WebUI 前端定时调用）。

        返回字典：``status`` 为 wait / scaned_but_redirect / confirmed / expired，
        confirmed 时附带 ``bot_id`` / ``user_id`` 并已持久化 token；expired 时附带
        ``expired=True``。scaned_but_redirect 会就地更新 base_url 供后续轮询。

        ``get_qrcode_status`` 是长轮询端点：状态有变化时立即返回，无变化时挂起
        直至超时（服务端也可能直接断开）。超时/断连都归一化为 ``wait``，由前端
        继续下一轮轮询。
        """
        if self._client is None:
            # 确认后 close() 会清掉临时 client；前端此时可能还有一次在途轮询，
            # 直接视为「等待」而非抛错，避免 502。
            return {"status": "wait"}
        try:
            status_data = await self._api_get_with_base(
                base_url=self.config.base_url,
                endpoint="ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode_id},
                auth=False,
            )
        except (httpx.TimeoutException, httpx.TransportError, ValueError):
            # 长轮询无状态变化时超时，或服务端关闭连接/返回空体：视为仍等待扫码。
            return {"status": "wait"}
        status = str(status_data.get("status", ""))
        result: dict[str, Any] = {"status": status}

        if status == "confirmed":
            token = status_data.get("bot_token", "")
            base_url = status_data.get("baseurl", "")
            if token:
                self._token = token
                if base_url:
                    self.config.base_url = base_url
                self._save_state()
                # 再推进一次登录代数：通知主渠道实例「新 token 已落盘」，促其立即
                # 重载 account.json 并续接新账号，无需重启网关。
                global _session_generation
                _session_generation += 1
                self._seen_generation = _session_generation
                # 全新登录（扫码连接新设备）：清空该渠道的配对授权，让下一次
                # 私聊重新进入配对码流程（否则沿用旧 token 时代已授权的用户，
                # 换新设备后收不到验证码）。
                clear_channel(self.name)
                result.update({
                    "confirmed": True,
                    "bot_id": status_data.get("ilink_bot_id", ""),
                    "user_id": status_data.get("ilink_user_id", ""),
                })
            else:
                result.update({"confirmed": False, "error": "no bot_token in response"})
        elif status == "scaned_but_redirect":
            redirect_host = str(status_data.get("redirect_host", "") or "").strip()
            if redirect_host:
                if not (redirect_host.startswith("http://") or redirect_host.startswith("https://")):
                    redirect_host = f"https://{redirect_host}"
                if redirect_host != self.config.base_url:
                    self.config.base_url = redirect_host
        elif status == "expired":
            result["expired"] = True

        return result

    async def close_login_client(self) -> None:
        """关闭扫码登录用的临时 HTTP 客户端。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # 渠道生命周期
    # ------------------------------------------------------------------

    async def login(self, force: bool = False) -> bool:
        """执行二维码登录并保存 token。成功返回 True。"""
        if force:
            # 强制重新登录：清除已有 token 和游标
            self._token = ""
            self._get_updates_buf = ""
            state_file = self._get_state_dir() / "account.json"
            if state_file.exists():
                state_file.unlink()
        if self._token or self._load_state():
            return True

        # 为登录流程初始化 HTTP 客户端
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=30),
            follow_redirects=True,
        )
        self._running = True  # 启用 _qr_login() 中的轮询循环
        try:
            return await self._qr_login()
        finally:
            self._running = False
            if self._client:
                await self._client.aclose()
                self._client = None

    async def start(self) -> None:
        """启动渠道：初始化客户端、加载/登录 token、开始长轮询循环。

        token 失效（errcode -14）后会自动清除并回到「等待重新扫码」状态，
        重新扫码成功后无需重启网关即可续接长轮询。
        """
        self._running = True
        self._next_poll_timeout_s = self.config.poll_timeout
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._next_poll_timeout_s + 10, connect=30),
            follow_redirects=True,
        )

        # 外层循环：token 失效后回到这里，等待重新扫码再续接长轮询。
        while self._running:
            if self.config.token:
                self._token = self.config.token
            elif not self._token:
                self._load_state()

            # 若重新加载到的仍是刚失效的旧 token（磁盘上无新 token），删除
            # account.json 并进入「等待重新扫码」，避免反复用失效 token 长轮询。
            if self._failed_token and self._token == self._failed_token:
                self.logger.warning(
                    "reloaded the same invalidated token; clearing account.json and waiting for re-login"
                )
                self._invalidate_token()
                self._failed_token = ""

            if not self._token:
                # 无 token：不在此处阻塞式终端扫码（会挂起网关约 8 分钟，且与 WebUI
                # 扫码登录流程冲突）。改为等待 WebUI 扫码登录 / CLI 登录把 token 写入
                # account.json，随后自动续接长轮询，无需重启网关。
                self.logger.info(
                    "weixin has no token; waiting for WebUI/CLI login to save one"
                )
                while self._running and not self._token:
                    await asyncio.sleep(2)
                    if self.config.token:
                        self._token = self.config.token
                    else:
                        self._load_state()
                if not self._running:
                    return
                self._session_pause_until = 0.0

            self.logger.info("channel starting with long-poll...")

            consecutive_failures = 0
            while self._running and self._token:
                if self._seen_generation != _session_generation:
                    # 检测到新的扫码登录（换账号/换设备）：重载 account.json 续接新 token。
                    self._seen_generation = _session_generation
                    self._clear_session_state()
                    self._load_state()
                    if not self._token:
                        # 登录尚未完成（或 token 已清除）：回到外层等待扫码写入新 token。
                        break
                try:
                    await self._poll_once()
                    consecutive_failures = 0
                except _TokenInvalidated:
                    # token 已失效并清除，回到外层等待重新扫码。
                    self.logger.warning(
                        "weixin token invalidated; waiting for a fresh QR login"
                    )
                    break
                except httpx.TimeoutException:
                    # 长轮询超时是正常行为，直接重试
                    continue
                except Exception:
                    if not self._running:
                        break
                    self.logger.exception("WeChat poll loop error")
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        # 连续失败次数过多，进入退避
                        consecutive_failures = 0
                        await asyncio.sleep(BACKOFF_DELAY_S)
                    else:
                        await asyncio.sleep(RETRY_DELAY_S)

    async def stop(self) -> None:
        """停止渠道：取消轮询任务、停止打字指示器、关闭客户端、保存状态。"""
        self._running = False
        self._pending_tool_hints.clear()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        for chat_id in list(self._typing_tasks):
            await self._stop_typing(chat_id, clear_remote=False)
        if self._client:
            await self._client.aclose()
            self._client = None
        self._save_state()
    # ------------------------------------------------------------------
    # 轮询（对应 monitor.ts 的 monitorWeixinProvider）
    # ------------------------------------------------------------------

    def _clear_session_state(self) -> None:
        """清空内存中的 token 与会话状态（不触碰磁盘上的 account.json）。"""
        self._token = ""
        self._get_updates_buf = ""
        self._context_tokens.clear()
        self._typing_tickets.clear()
        self._session_pause_until = 0.0

    def _invalidate_token(self) -> None:
        """清除失效 token：清空内存并删除磁盘上的 account.json。

        微信 token 被作废（errcode -14）后调用，确保后续回到「等待重新扫码」
        状态时不会反复加载同一个已失效的 token。
        """
        self._clear_session_state()
        state_file = self._get_state_dir() / "account.json"
        with suppress(Exception):
            state_file.unlink()

    def _pause_session(self, duration_s: int = SESSION_PAUSE_DURATION_S) -> None:
        """暂停会话（会话过期后暂停轮询一段时间）。"""
        self._session_pause_until = time.time() + duration_s

    def _session_pause_remaining_s(self) -> int:
        """返回会话暂停剩余秒数；已过期则返回 0 并重置。"""
        remaining = int(self._session_pause_until - time.time())
        if remaining <= 0:
            self._session_pause_until = 0.0
            return 0
        return remaining

    def _assert_session_active(self) -> None:
        """断言会话处于活跃状态；若已暂停则抛出 RuntimeError。"""
        remaining = self._session_pause_remaining_s()
        if remaining > 0:
            remaining_min = max((remaining + 59) // 60, 1)
            raise RuntimeError(
                f"WeChat session paused, {remaining_min} min remaining (errcode {ERRCODE_SESSION_EXPIRED})"
            )

    async def _poll_once(self) -> None:
        """执行一次长轮询：发送 getupdates 请求并处理返回的消息。"""
        remaining = self._session_pause_remaining_s()
        if remaining > 0:
            await asyncio.sleep(remaining)
            return

        body: dict[str, Any] = {
            "get_updates_buf": self._get_updates_buf,
            "base_info": BASE_INFO,
        }

        # 调整 httpx 超时以匹配当前轮询超时
        assert self._client is not None
        self._client.timeout = httpx.Timeout(self._next_poll_timeout_s + 10, connect=30)

        data = await self._api_post("ilink/bot/getupdates", body)

        # 检查 API 级别错误（monitor.ts 同时检查 ret 和 errcode）
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)

        is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)

        if is_error:
            if errcode == ERRCODE_SESSION_EXPIRED or ret == ERRCODE_SESSION_EXPIRED:
                # 会话失效（errcode -14）：token 已被微信服务端作废（如多端抢登）。
                # 不再盲等 60 分钟——清除内存 token 并抛出专用信号，由 start() 回到
                # 「等待重新扫码」状态。磁盘上的 account.json 暂不删除：若用户正在
                # 「扫码连接」并已写入新 token，start() 会直接续接新 token（切换设备）。
                self.logger.warning(
                    "session expired (errcode {}); clearing token and waiting for re-login",
                    errcode,
                )
                self._failed_token = self._token
                self._clear_session_state()
                raise _TokenInvalidated()
            raise RuntimeError(
                f"getUpdates failed: ret={ret} errcode={errcode} errmsg={data.get('errmsg', '')}"
            )

        # 遵循服务端建议的轮询超时（monitor.ts:102-105）
        server_timeout_ms = data.get("longpolling_timeout_ms")
        if server_timeout_ms and server_timeout_ms > 0:
            self._next_poll_timeout_s = max(server_timeout_ms // 1000, 5)

        # 更新游标
        new_buf = data.get("get_updates_buf", "")
        if new_buf:
            self._get_updates_buf = new_buf
            self._save_state()

        # 处理消息（WeixinMessage[]，来自 types.ts）
        msgs: list[dict] = data.get("msgs", []) or []
        for msg in msgs:
            try:
                await self._process_message(msg)
            except Exception:
                self.logger.exception("Failed to process WeChat message")

    # ------------------------------------------------------------------
    # 入站消息处理（对应 inbound.ts + process-message.ts）
    # ------------------------------------------------------------------

    async def _process_message(self, msg: dict) -> None:
        """处理来自 getUpdates 的单条 WeixinMessage。"""
        # 跳过机器人自身发送的消息（message_type 2 = BOT）
        if msg.get("message_type") == MESSAGE_TYPE_BOT:
            return

        msg_id = str(msg.get("message_id", "") or msg.get("seq", ""))
        if not msg_id:
            msg_id = f"{msg.get('from_user_id', '')}_{msg.get('create_time_ms', '')}"

        from_user_id = msg.get("from_user_id", "") or ""
        if not from_user_id:
            return

        # 基于 message_id 去重
        if msg_id in self._processed_ids:
            return
        self._processed_ids[msg_id] = None
        while len(self._processed_ids) > 1000:
            self._processed_ids.popitem(last=False)

        ctx_token = msg.get("context_token", "")
        if not self.is_allowed(from_user_id):
            # 群聊消息：直接交给消息总线处理配对
            if from_user_id.endswith("@chatroom"):
                await self._handle_message(
                    sender_id=from_user_id,
                    chat_id=from_user_id,
                    content="",
                    metadata={"message_id": msg_id},
                    is_dm=False,
                )
                return

            # 未授权的私聊用户：需要 context_token 才能发送配对码
            if not ctx_token:
                self.logger.warning(
                    "Access denied for sender {}; cannot send WeChat pairing code without context_token",
                    from_user_id,
                )
                return

            # 临时设置 context_token 以便发送配对码，处理完毕后恢复原值
            had_ctx_token = from_user_id in self._context_tokens
            previous_ctx_token = self._context_tokens.get(from_user_id, "")
            had_ctx_token_at = from_user_id in self._context_token_at
            previous_ctx_token_at = self._context_token_at.get(from_user_id, 0.0)
            self._context_tokens[from_user_id] = ctx_token
            self._context_token_at[from_user_id] = time.time()
            try:
                await self._handle_message(
                    sender_id=from_user_id,
                    chat_id=from_user_id,
                    content="",
                    metadata={"message_id": msg_id},
                    is_dm=True,
                )
            finally:
                # 恢复或清除临时 context_token
                if had_ctx_token:
                    self._context_tokens[from_user_id] = previous_ctx_token
                else:
                    self._context_tokens.pop(from_user_id, None)
                if had_ctx_token_at:
                    self._context_token_at[from_user_id] = previous_ctx_token_at
                else:
                    self._context_token_at.pop(from_user_id, None)
            return

        # 缓存 context_token（所有回复都需要——inbound.ts:23-27）
        if ctx_token:
            self._context_tokens[from_user_id] = ctx_token
            self._context_token_at[from_user_id] = time.time()
            self._save_state()

        # 解析 item_list（WeixinMessage.item_list——types.ts:161）
        item_list: list[dict] = msg.get("item_list") or []
        content_parts: list[str] = []
        media_paths: list[str] = []
        has_top_level_downloadable_media = False

        for item in item_list:
            item_type = item.get("type", 0)

            if item_type == ITEM_TEXT:
                text = (item.get("text_item") or {}).get("text", "")
                if text:
                    # 处理引用/转发消息（inbound.ts:86-98）
                    ref = item.get("ref_msg")
                    if ref:
                        ref_item = ref.get("message_item")
                        # 若被引用消息是媒体类型，仅传递文本
                        if ref_item and ref_item.get("type", 0) in (
                            ITEM_IMAGE,
                            ITEM_VOICE,
                            ITEM_FILE,
                            ITEM_VIDEO,
                        ):
                            content_parts.append(text)
                        else:
                            # 拼接引用标题和被引用文本
                            parts: list[str] = []
                            if ref.get("title"):
                                parts.append(ref["title"])
                            if ref_item:
                                ref_text = (ref_item.get("text_item") or {}).get("text", "")
                                if ref_text:
                                    parts.append(ref_text)
                            if parts:
                                content_parts.append(f"[引用: {' | '.join(parts)}]\n{text}")
                            else:
                                content_parts.append(text)
                    else:
                        content_parts.append(text)

            elif item_type == ITEM_IMAGE:
                # 下载图片并 AES 解密
                image_item = item.get("image_item") or {}
                if _has_downloadable_media_locator(image_item.get("media")):
                    has_top_level_downloadable_media = True
                file_path = await self._download_media_item(image_item, "image")
                if file_path:
                    content_parts.append(f"[image]\n[Image: source: {file_path}]")
                    media_paths.append(file_path)
                else:
                    content_parts.append("[image]")

            elif item_type == ITEM_VOICE:
                # 语音消息：优先使用微信提供的语音转文字
                voice_item = item.get("voice_item") or {}
                voice_text = voice_item.get("text", "")  # 微信语音转文字（inbound.ts:101-103）
                if voice_text:
                    content_parts.append(f"[voice] {voice_text}")
                else:
                    # 无转文字时下载语音文件并自行转录
                    if _has_downloadable_media_locator(voice_item.get("media")):
                        has_top_level_downloadable_media = True
                    file_path = await self._download_media_item(voice_item, "voice")
                    if file_path:
                        transcription = await self.transcribe_audio(file_path)
                        if transcription:
                            content_parts.append(f"[voice] {transcription}")
                        else:
                            content_parts.append(f"[voice]\n[Audio: source: {file_path}]")
                        media_paths.append(file_path)
                    else:
                        content_parts.append("[voice]")

            elif item_type == ITEM_FILE:
                # 下载文件并 AES 解密
                file_item = item.get("file_item") or {}
                if _has_downloadable_media_locator(file_item.get("media")):
                    has_top_level_downloadable_media = True
                file_name = file_item.get("file_name", "unknown")
                file_path = await self._download_media_item(
                    file_item,
                    "file",
                    file_name,
                )
                if file_path:
                    content_parts.append(f"[file: {file_name}]\n[File: source: {file_path}]")
                    media_paths.append(file_path)
                else:
                    content_parts.append(f"[file: {file_name}]")

            elif item_type == ITEM_VIDEO:
                # 下载视频并 AES 解密
                video_item = item.get("video_item") or {}
                if _has_downloadable_media_locator(video_item.get("media")):
                    has_top_level_downloadable_media = True
                file_path = await self._download_media_item(video_item, "video")
                if file_path:
                    content_parts.append(f"[video]\n[Video: source: {file_path}]")
                    media_paths.append(file_path)
                else:
                    content_parts.append("[video]")

        # 回退：当顶层未下载到媒体时，尝试引用消息中的媒体。
        # 这与参考插件行为一致：当 item_list 无可下载媒体时检查 ref_msg.message_item。
        if not media_paths and not has_top_level_downloadable_media:
            ref_media_item: dict[str, Any] | None = None
            for item in item_list:
                if item.get("type", 0) != ITEM_TEXT:
                    continue
                ref = item.get("ref_msg") or {}
                candidate = ref.get("message_item") or {}
                if candidate.get("type", 0) in (ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO):
                    ref_media_item = candidate
                    break

            if ref_media_item:
                ref_type = ref_media_item.get("type", 0)
                if ref_type == ITEM_IMAGE:
                    image_item = ref_media_item.get("image_item") or {}
                    file_path = await self._download_media_item(image_item, "image")
                    if file_path:
                        content_parts.append(f"[image]\n[Image: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == ITEM_VOICE:
                    voice_item = ref_media_item.get("voice_item") or {}
                    file_path = await self._download_media_item(voice_item, "voice")
                    if file_path:
                        transcription = await self.transcribe_audio(file_path)
                        if transcription:
                            content_parts.append(f"[voice] {transcription}")
                        else:
                            content_parts.append(f"[voice]\n[Audio: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == ITEM_FILE:
                    file_item = ref_media_item.get("file_item") or {}
                    file_name = file_item.get("file_name", "unknown")
                    file_path = await self._download_media_item(file_item, "file", file_name)
                    if file_path:
                        content_parts.append(f"[file: {file_name}]\n[File: source: {file_path}]")
                        media_paths.append(file_path)
                elif ref_type == ITEM_VIDEO:
                    video_item = ref_media_item.get("video_item") or {}
                    file_path = await self._download_media_item(video_item, "video")
                    if file_path:
                        content_parts.append(f"[video]\n[Video: source: {file_path}]")
                        media_paths.append(file_path)

        content = "\n".join(content_parts)
        if not content:
            return

        self.logger.info(
            "inbound: from={} items={} bodyLen={}",
            from_user_id,
            ",".join(str(i.get("type", 0)) for i in item_list),
            len(content),
        )

        await self._start_typing(from_user_id, ctx_token)

        await self._handle_message(
            sender_id=from_user_id,
            chat_id=from_user_id,
            content=content,
            media=media_paths or None,
            metadata={"message_id": msg_id},
        )

    # ------------------------------------------------------------------
    # 媒体下载（对应 media-download.ts + pic-decrypt.ts）
    # ------------------------------------------------------------------

    async def _download_media_item(
        self,
        typed_item: dict,
        media_type: str,
        filename: str | None = None,
    ) -> str | None:
        """下载并 AES 解密媒体项。返回本地文件路径或 None。"""
        try:
            media = typed_item.get("media") or {}
            encrypt_query_param = str(media.get("encrypt_query_param", "") or "")
            full_url = str(media.get("full_url", "") or "").strip()

            if not encrypt_query_param and not full_url:
                return None

            # 解析 AES 密钥（media-download.ts:43-45, pic-decrypt.ts:40-52）
            # image_item.aeskey 是原始十六进制字符串（16 字节 → 32 个十六进制字符）。
            # media.aes_key 始终是 base64 编码。
            # 图片优先使用 image_item.aeskey；其他类型使用 media.aes_key。
            raw_aeskey_hex = typed_item.get("aeskey", "")
            media_aes_key_b64 = media.get("aes_key", "")

            aes_key_b64: str = ""
            if raw_aeskey_hex:
                # 十六进制 → 原始字节 → base64（匹配 media-download.ts:43-44）
                aes_key_b64 = base64.b64encode(bytes.fromhex(raw_aeskey_hex)).decode()
            elif media_aes_key_b64:
                aes_key_b64 = media_aes_key_b64

            # 参考协议行为：语音/文件/视频需要 aes_key；
            # 仅图片在缺少密钥时可作为普通字节下载。
            if media_type != "image" and not aes_key_b64:
                return None

            assert self._client is not None
            fallback_url = ""
            if encrypt_query_param:
                # 构造 CDN 下载回退 URL
                fallback_url = (
                    f"{self.config.cdn_base_url}/download"
                    f"?encrypted_query_param={quote(encrypt_query_param)}"
                )

            # 下载候选列表：优先 full_url，回退到 encrypt_query_param
            download_candidates: list[tuple[str, str]] = []
            if full_url:
                download_candidates.append(("full_url", full_url))
            if fallback_url and (not full_url or fallback_url != full_url):
                download_candidates.append(("encrypt_query_param", fallback_url))

            data = b""
            for idx, (download_source, cdn_url) in enumerate(download_candidates):
                try:
                    resp = await self._client.get(cdn_url)
                    resp.raise_for_status()
                    data = resp.content
                    break
                except Exception as e:
                    # full_url 失败且错误可重试时，回退到 encrypt_query_param
                    has_more_candidates = idx + 1 < len(download_candidates)
                    should_fallback = (
                        download_source == "full_url"
                        and has_more_candidates
                        and self._is_retryable_media_download_error(e)
                    )
                    if should_fallback:
                        self.logger.warning(
                            "media download failed via full_url, falling back to encrypt_query_param: type={} err={}",
                            media_type,
                            e,
                        )
                        continue
                    raise

            # 有 AES 密钥时解密数据
            if aes_key_b64 and data:
                data = _decrypt_aes_ecb(data, aes_key_b64)

            if not data:
                return None

            # 保存到媒体目录
            media_dir = get_media_dir("channels/weixin")
            ext = _ext_for_type(media_type)
            if not filename:
                # 未提供文件名时，基于时间戳和哈希生成
                ts = int(time.time())
                hash_seed = encrypt_query_param or full_url
                h = abs(hash(hash_seed)) % 100000
                filename = f"{media_type}_{ts}_{h}{ext}"
            safe_name = os.path.basename(filename)  # 防止路径遍历
            file_path = media_dir / safe_name
            file_path.write_bytes(data)
            return str(file_path)

        except Exception:
            self.logger.exception("Error downloading media")
            return None

    # ------------------------------------------------------------------
    # 出站消息（对应 send.ts 的 buildTextMessageReq + sendMessageWeixin）
    # ------------------------------------------------------------------

    async def _get_typing_ticket(self, user_id: str, context_token: str = "") -> str:
        """获取打字票据，支持按用户刷新和失败退避缓存。"""
        now = time.time()
        entry = self._typing_tickets.get(user_id)
        # 缓存未过期时直接返回缓存的票据
        if entry and now < float(entry.get("next_fetch_at", 0)):
            return str(entry.get("ticket", "") or "")

        body: dict[str, Any] = {
            "ilink_user_id": user_id,
            "context_token": context_token or None,
            "base_info": BASE_INFO,
        }
        data = await self._api_post("ilink/bot/getconfig", body)
        if data.get("ret", 0) == 0:
            # 成功：缓存票据，设置随机 TTL（抖动避免同时刷新）
            ticket = str(data.get("typing_ticket", "") or "")
            self._typing_tickets[user_id] = {
                "ticket": ticket,
                "ever_succeeded": True,
                "next_fetch_at": now + (random.random() * TYPING_TICKET_TTL_S),
                "retry_delay_s": CONFIG_CACHE_INITIAL_RETRY_S,
            }
            return ticket

        # 失败：指数退避，下次延迟翻倍（上限 CONFIG_CACHE_MAX_RETRY_S）
        prev_delay = float(entry.get("retry_delay_s", CONFIG_CACHE_INITIAL_RETRY_S)) if entry else CONFIG_CACHE_INITIAL_RETRY_S
        next_delay = min(prev_delay * 2, CONFIG_CACHE_MAX_RETRY_S)
        if entry:
            # 已有缓存条目：更新退避延迟，返回可能为空的旧票据
            entry["next_fetch_at"] = now + next_delay
            entry["retry_delay_s"] = next_delay
            return str(entry.get("ticket", "") or "")

        # 首次失败：创建缓存条目
        self._typing_tickets[user_id] = {
            "ticket": "",
            "ever_succeeded": False,
            "next_fetch_at": now + CONFIG_CACHE_INITIAL_RETRY_S,
            "retry_delay_s": CONFIG_CACHE_INITIAL_RETRY_S,
        }
        return ""

    async def _refresh_context_token_if_stale(
        self, chat_id: str, context_token: str
    ) -> str:
        """若缓存的 context_token 过期则返回刷新后的新令牌。

        iLink context_token 在短时间无活动后会在服务端过期（经验值约 90 秒）。
        在发送前主动刷新可防止长轮次或定时推送时消息静默丢失。
        """
        if not context_token:
            return context_token

        now = time.time()
        cached_at = self._context_token_at.get(chat_id, 0)
        age = now - cached_at

        # 未超过最大年龄，直接返回缓存的令牌
        if age < CONTEXT_TOKEN_MAX_AGE_S:
            return context_token

        self.logger.debug(
            "WeChat context_token for {} is {:.0f}s old; refreshing via getconfig",
            chat_id,
            age,
        )

        body: dict[str, Any] = {
            "ilink_user_id": chat_id,
            "context_token": context_token,
            "base_info": BASE_INFO,
        }
        try:
            data = await self._api_post("ilink/bot/getconfig", body)
        except Exception as e:
            self.logger.warning("WeChat getconfig failed for {}: {}", chat_id, e)
            return context_token

        if data.get("ret", 0) != 0:
            self.logger.warning(
                "WeChat getconfig returned ret={} for {}: {}",
                data.get("ret"),
                chat_id,
                data.get("errmsg", ""),
            )
            return context_token

        new_token = str(data.get("context_token", "") or "")
        if new_token and new_token != context_token:
            # 获取到新令牌：更新缓存并持久化
            self.logger.info(
                "WeChat context_token refreshed for {} (age {:.0f}s -> fresh)",
                chat_id,
                age,
            )
            self._context_tokens[chat_id] = new_token
            self._context_token_at[chat_id] = now
            self._save_state()
            return new_token

        return context_token

    async def _flush_tool_hints(self, chat_id: str) -> None:
        """将缓冲的工具提示合并为单条消息发送。

        工具提示合并发送以减少消息数量，避免触及微信 iLink 速率限制
        （约 7 条/5 分钟）。失败仅记录日志不抛出，确保主消息发送不被阻塞。
        """
        hints = self._pending_tool_hints.pop(chat_id, None)
        if not hints:
            return

        self.logger.info(
            "Flushing {} buffered tool hint(s) for {}",
            len(hints),
            chat_id,
        )

        ctx_token = self._context_tokens.get(chat_id, "")
        ctx_token = await self._refresh_context_token_if_stale(chat_id, ctx_token)
        if not ctx_token:
            # 无 context_token 无法发送，丢弃缓冲的提示
            self.logger.warning(
                "Dropped {} buffered tool hint(s) for {}: no context_token",
                len(hints),
                chat_id,
            )
            return

        try:
            await self._send_text(chat_id, "\n\n".join(hints), ctx_token)
        except Exception:
            self.logger.exception(
                "Failed to flush buffered tool hints for {}", chat_id
            )

    async def _send_typing(self, user_id: str, typing_ticket: str, status: int) -> None:
        """发送打字状态（尽力而为，失败不阻塞主流程）。"""
        if not typing_ticket:
            return
        body: dict[str, Any] = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": BASE_INFO,
        }
        await self._api_post("ilink/bot/sendtyping", body)

    async def _typing_keepalive_loop(self, user_id: str, typing_ticket: str, stop_event: asyncio.Event) -> None:
        """打字保活循环：定期发送打字状态，直到 stop_event 被设置。"""
        try:
            while not stop_event.is_set():
                await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_S)
                if stop_event.is_set():
                    break
                with suppress(Exception):
                    await self._send_typing(user_id, typing_ticket, TYPING_STATUS_TYPING)
        finally:
            pass

    async def send(self, msg: OutboundMessage) -> None:
        """通过微信发送出站消息。

        处理流程：
        1. 工具提示：缓冲合并，避免速率限制。
        2. 推理增量：跳过（微信无推理 UI）。
        3. 空进度消息：跳过（无可见内容）。
        4. 刷新缓冲的工具提示。
        5. 启动打字指示器。
        6. 发送媒体文件（失败时回退为文本提示）。
        7. 发送文本内容（分块）。
        8. 停止打字指示器。
        """
        if not self._client or not self._token:
            raise RuntimeError("WeChat client not initialized or not authenticated")
        self._assert_session_active()

        is_progress = bool((msg.metadata or {}).get("_progress", False))

        # 缓冲工具提示以合并连续提示，避免消耗微信 iLink 速率配额（约 7 条/5 分钟）
        if is_progress and (msg.metadata or {}).get("_tool_hint"):
            if not self.send_tool_hints:
                return
            self._pending_tool_hints.setdefault(msg.chat_id, []).append(msg.content)
            self.logger.debug(
                "Buffered tool hint for {} (count={})",
                msg.chat_id,
                len(self._pending_tool_hints[msg.chat_id]),
            )
            return

        # 推理增量在微信中不可见（无推理 UI），直接跳过，不发送也不刷新缓冲
        if is_progress and (msg.metadata or {}).get("_reasoning_delta"):
            self.logger.debug(
                "Dropped invisible reasoning delta for {}", msg.chat_id
            )
            return

        content = msg.content.strip()

        # 空的进度消息（如 after_iteration 的 tool_events）不应作为分隔符——
        # 它们没有可见内容。
        if is_progress and not content and not (msg.media or []):
            self.logger.debug(
                "Skipped empty progress message for {} (no visible content)",
                msg.chat_id,
            )
            return

        # 发送任何可见消息前先刷新缓冲的工具提示
        await self._flush_tool_hints(msg.chat_id)

        if not is_progress:
            await self._stop_typing(msg.chat_id, clear_remote=True)

        ctx_token = self._context_tokens.get(msg.chat_id, "")
        ctx_token = await self._refresh_context_token_if_stale(msg.chat_id, ctx_token)
        if not ctx_token:
            raise RuntimeError(
                f"WeChat context_token missing for chat_id={msg.chat_id}, cannot send"
            )

        # 获取打字票据并启动打字状态
        typing_ticket = ""
        with suppress(Exception):
            typing_ticket = await self._get_typing_ticket(msg.chat_id, ctx_token)

        if typing_ticket:
            with suppress(Exception):
                await self._send_typing(msg.chat_id, typing_ticket, TYPING_STATUS_TYPING)

        # 启动打字保活循环
        typing_keepalive_stop = asyncio.Event()
        typing_keepalive_task: asyncio.Task | None = None
        if typing_ticket:
            typing_keepalive_task = asyncio.create_task(
                self._typing_keepalive_loop(msg.chat_id, typing_ticket, typing_keepalive_stop)
            )

        try:
            # --- 先发送媒体文件（遵循 Telegram 渠道的模式）---
            for media_path in (msg.media or []):
                try:
                    await self._send_media_file(msg.chat_id, media_path, ctx_token)
                except (httpx.TimeoutException, httpx.TransportError):
                    # 网络/传输错误：不回退为文本——文本发送也很可能失败，
                    # 外层 except 会重新抛出以便 ChannelManager 正确重试。
                    self.logger.opt(exception=True).warning(
                        "Network error sending media {}",
                        media_path,
                    )
                    raise
                except httpx.HTTPStatusError as http_err:
                    status_code = (
                        http_err.response.status_code
                        if http_err.response is not None
                        else 0
                    )
                    if status_code >= 500:
                        # 服务端/可重试的 HTTP 错误——与网络错误同等处理
                        self.logger.exception(
                            "Server error ({} {}) sending media {}",
                            status_code,
                            http_err.response.reason_phrase
                            if http_err.response is not None
                            else "",
                            media_path,
                        )
                        raise
                    # 4xx 客户端错误不可重试——回退为文本提示
                    filename = Path(media_path).name
                    self.logger.exception("Failed to send media {}", media_path)
                    await self._send_text(
                        msg.chat_id, f"[Failed to send: {filename}]", ctx_token,
                    )
                except Exception:
                    # 非网络错误（格式、文件不存在等）：通过文本提示通知用户
                    filename = Path(media_path).name
                    self.logger.exception("Failed to send media {}", media_path)
                    await self._send_text(
                        msg.chat_id, f"[Failed to send: {filename}]", ctx_token,
                    )

            # --- 发送文本内容 ---
            if not content:
                return

            chunks = split_message(content, WEIXIN_MAX_MESSAGE_LEN)
            for chunk in chunks:
                await self._send_text(msg.chat_id, chunk, ctx_token)
        except Exception:
            self.logger.exception("Error sending message")
            raise
        finally:
            # 停止打字保活循环
            if typing_keepalive_task:
                typing_keepalive_stop.set()
                typing_keepalive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_keepalive_task

            # 非进度消息发送完毕后取消打字状态
            if typing_ticket and not is_progress:
                with suppress(Exception):
                    await self._send_typing(msg.chat_id, typing_ticket, TYPING_STATUS_CANCEL)

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """微信 iLink 不支持原生流式增量。

        仅钩住 ``_stream_end``，使得即使最终答案携带 ``_streamed`` 标志
        且绕过 :meth:`send` 时，缓冲的工具提示仍能被刷新。
        """
        if metadata and metadata.get("_stream_end"):
            await self._flush_tool_hints(chat_id)

    async def _start_typing(self, chat_id: str, context_token: str = "") -> None:
        """收到消息时立即启动打字指示器。"""
        if not self._client or not self._token or not chat_id:
            return
        # 先停止已有的打字任务
        await self._stop_typing(chat_id, clear_remote=False)
        try:
            ticket = await self._get_typing_ticket(chat_id, context_token)
            if not ticket:
                return
            await self._send_typing(chat_id, ticket, TYPING_STATUS_TYPING)
        except Exception as e:
            self.logger.debug("typing indicator start failed for {}: {}", chat_id, e)
            return

        # 创建打字保活循环任务
        stop_event = asyncio.Event()

        async def keepalive() -> None:
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_S)
                    if stop_event.is_set():
                        break
                    with suppress(Exception):
                        await self._send_typing(chat_id, ticket, TYPING_STATUS_TYPING)
            finally:
                pass

        task = asyncio.create_task(keepalive())
        task._typing_stop_event = stop_event  # type: ignore[attr-defined]
        self._typing_tasks[chat_id] = task

    async def _stop_typing(self, chat_id: str, *, clear_remote: bool) -> None:
        """停止指定会话的打字指示器。

        Args:
            chat_id: 会话 ID。
            clear_remote: 若为 True，同时向微信服务端发送取消打字状态。
        """
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            stop_event = getattr(task, "_typing_stop_event", None)
            if stop_event:
                stop_event.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if not clear_remote:
            return
        # 向服务端发送取消打字状态
        entry = self._typing_tickets.get(chat_id)
        ticket = str(entry.get("ticket", "") or "") if isinstance(entry, dict) else ""
        if not ticket:
            return
        try:
            await self._send_typing(chat_id, ticket, TYPING_STATUS_CANCEL)
        except Exception as e:
            self.logger.debug("typing clear failed for {}: {}", chat_id, e)

    async def _send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
    ) -> None:
        """发送文本消息，严格匹配 send.ts 的协议格式。"""
        client_id = f"biscuitbot-{uuid.uuid4().hex[:12]}"

        item_list: list[dict] = []
        if text:
            item_list.append({"type": ITEM_TEXT, "text_item": {"text": text}})

        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
        }
        if item_list:
            weixin_msg["item_list"] = item_list
        if context_token:
            weixin_msg["context_token"] = context_token

        body: dict[str, Any] = {
            "msg": weixin_msg,
            "base_info": BASE_INFO,
        }

        data = await self._api_post("ilink/bot/sendmessage", body)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"WeChat send text error (ret={ret}, errcode={errcode}): {data.get('errmsg', '')}"
            )

    async def _send_media_file(
        self,
        to_user_id: str,
        media_path: str,
        context_token: str,
    ) -> None:
        """上传本地文件到微信 CDN 并发送为媒体消息。

        遵循 ``@tencent-weixin/openclaw-weixin`` v1.0.3 的精确协议：
        1. 客户端生成随机 16 字节 AES 密钥。
        2. 调用 ``getuploadurl`` 获取上传 URL，携带文件元数据和十六进制 AES 密钥。
        3. AES-128-ECB 加密文件并 POST 到 CDN（``{cdnBaseUrl}/upload``）。
        4. 从 CDN 响应头读取 ``x-encrypted-param`` 作为下载参数。
        5. 发送 ``sendmessage``，携带引用上传结果的媒体项。
        """
        p = Path(media_path)
        if not p.is_file():
            raise FileNotFoundError(f"Media file not found: {media_path}")

        raw_data = p.read_bytes()
        raw_size = len(raw_data)
        raw_md5 = hashlib.md5(raw_data).hexdigest()

        # 根据扩展名确定上传媒体类型
        ext = p.suffix.lower()
        if ext in _IMAGE_EXTS:
            upload_type = UPLOAD_MEDIA_IMAGE
            item_type = ITEM_IMAGE
            item_key = "image_item"
        elif ext in _VIDEO_EXTS:
            upload_type = UPLOAD_MEDIA_VIDEO
            item_type = ITEM_VIDEO
            item_key = "video_item"
        elif ext in _VOICE_EXTS:
            upload_type = UPLOAD_MEDIA_VOICE
            item_type = ITEM_VOICE
            item_key = "voice_item"
        else:
            upload_type = UPLOAD_MEDIA_FILE
            item_type = ITEM_FILE
            item_key = "file_item"

        # 生成客户端 AES-128 密钥（16 个随机字节）
        aes_key_raw = os.urandom(16)
        aes_key_hex = aes_key_raw.hex()

        # 计算加密后大小：PKCS7 填充到 16 字节边界
        # 匹配 aesEcbPaddedSize: Math.ceil((size + 1) / 16) * 16
        padded_size = ((raw_size + 1 + 15) // 16) * 16

        # 步骤 1：从服务端获取上传 URL（优先 upload_full_url，回退到 upload_param）
        file_key = os.urandom(16).hex()
        upload_body: dict[str, Any] = {
            "filekey": file_key,
            "media_type": upload_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": padded_size,
            "no_need_thumb": True,
            "aeskey": aes_key_hex,
        }

        assert self._client is not None
        upload_resp = await self._api_post("ilink/bot/getuploadurl", upload_body)

        upload_full_url = str(upload_resp.get("upload_full_url", "") or "").strip()
        upload_param = str(upload_resp.get("upload_param", "") or "")
        if not upload_full_url and not upload_param:
            raise RuntimeError(
                "getuploadurl returned no upload URL "
                f"(need upload_full_url or upload_param): {upload_resp}"
            )

        # 步骤 2：AES-128-ECB 加密并 POST 到 CDN
        aes_key_b64 = base64.b64encode(aes_key_raw).decode()
        encrypted_data = _encrypt_aes_ecb(raw_data, aes_key_b64)

        if upload_full_url:
            cdn_upload_url = upload_full_url
        else:
            # 构造 CDN 上传 URL（使用 upload_param 回退）
            cdn_upload_url = (
                f"{self.config.cdn_base_url}/upload"
                f"?encrypted_query_param={quote(upload_param)}"
                f"&filekey={quote(file_key)}"
            )

        cdn_resp = await self._client.post(
            cdn_upload_url,
            content=encrypted_data,
            headers={"Content-Type": "application/octet-stream"},
        )
        cdn_resp.raise_for_status()

        # 下载用的 encrypted_query_param 来自 CDN 响应头
        download_param = cdn_resp.headers.get("x-encrypted-param", "")
        if not download_param:
            raise RuntimeError(
                "CDN upload response missing x-encrypted-param header; "
                f"status={cdn_resp.status_code} headers={dict(cdn_resp.headers)}"
            )

        # 步骤 3：发送携带媒体项的消息
        # CDNMedia 的 aes_key 是十六进制密钥的 base64 编码
        # （匹配: Buffer.from(uploaded.aeskey).toString("base64")）
        cdn_aes_key_b64 = base64.b64encode(aes_key_hex.encode()).decode()

        media_item: dict[str, Any] = {
            "media": {
                "encrypt_query_param": download_param,
                "aes_key": cdn_aes_key_b64,
                "encrypt_type": 1,
            },
        }

        # 根据媒体类型设置特定字段
        if item_type == ITEM_IMAGE:
            media_item["mid_size"] = padded_size
        elif item_type == ITEM_VIDEO:
            media_item["video_size"] = padded_size
        elif item_type == ITEM_FILE:
            media_item["file_name"] = p.name
            media_item["len"] = str(raw_size)

        # 每个媒体项作为独立消息发送（匹配参考插件行为）
        client_id = f"biscuitbot-{uuid.uuid4().hex[:12]}"
        item_list: list[dict] = [{"type": item_type, item_key: media_item}]

        weixin_msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": item_list,
        }
        if context_token:
            weixin_msg["context_token"] = context_token

        body: dict[str, Any] = {
            "msg": weixin_msg,
            "base_info": BASE_INFO,
        }

        data = await self._api_post("ilink/bot/sendmessage", body)
        ret = data.get("ret", 0)
        errcode = data.get("errcode", 0)
        if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
            raise RuntimeError(
                f"WeChat send media error (ret={ret}, errcode={errcode}): {data.get('errmsg', '')}"
            )


# ---------------------------------------------------------------------------
# AES-128-ECB 加密/解密（对应 pic-decrypt.ts / aes-ecb.ts）
# ---------------------------------------------------------------------------


def _parse_aes_key(aes_key_b64: str) -> bytes:
    """解析 base64 编码的 AES 密钥，处理实际遇到的两种编码格式。

    来自 ``pic-decrypt.ts parseAesKey``：

    * ``base64(原始 16 字节)``        → 图片（media.aes_key）
    * ``base64(16 字节的十六进制字符串)`` → 文件/语音/视频

    第二种情况下，base64 解码得到 32 个 ASCII 十六进制字符，
    需要再按十六进制解析才能恢复实际的 16 字节密钥。
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
        # 十六进制编码的密钥：base64 → 十六进制字符串 → 原始字节
        return bytes.fromhex(decoded.decode("ascii"))
    raise ValueError(
        f"aes_key must decode to 16 raw bytes or 32-char hex string, got {len(decoded)} bytes"
    )


def _encrypt_aes_ecb(data: bytes, aes_key_b64: str) -> bytes:
    """使用 AES-128-ECB 和 PKCS7 填充加密数据，用于 CDN 上传。"""
    try:
        key = _parse_aes_key(aes_key_b64)
    except Exception as e:
        logger.warning("Failed to parse AES key for encryption, sending raw: {}", e)
        return data

    # PKCS7 填充
    pad_len = 16 - len(data) % 16
    padded = data + bytes([pad_len] * pad_len)

    # 优先使用 pycryptodome
    with suppress(ImportError):
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_ECB)
        return cipher.encrypt(padded)

    # 回退到 cryptography 库
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        cipher_obj = Cipher(algorithms.AES(key), modes.ECB())
        encryptor = cipher_obj.encryptor()
        return encryptor.update(padded) + encryptor.finalize()
    except ImportError:
        logger.warning("Cannot encrypt media: install 'pycryptodome' or 'cryptography'")
        return data


def _decrypt_aes_ecb(data: bytes, aes_key_b64: str) -> bytes:
    """解密 AES-128-ECB 媒体数据。

    ``aes_key_b64`` 始终为 base64 编码（调用方需先将十六进制密钥转换）。
    """
    try:
        key = _parse_aes_key(aes_key_b64)
    except Exception as e:
        logger.warning("Failed to parse AES key, returning raw data: {}", e)
        return data

    decrypted: bytes | None = None

    # 优先使用 pycryptodome
    with suppress(ImportError):
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_ECB)
        decrypted = cipher.decrypt(data)

    # 回退到 cryptography 库
    if decrypted is None:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            cipher_obj = Cipher(algorithms.AES(key), modes.ECB())
            decryptor = cipher_obj.decryptor()
            decrypted = decryptor.update(data) + decryptor.finalize()
        except ImportError:
            logger.warning("Cannot decrypt media: install 'pycryptodome' or 'cryptography'")
            return data

    return _pkcs7_unpad_safe(decrypted)


def _pkcs7_unpad_safe(data: bytes, block_size: int = 16) -> bytes:
    """安全移除 PKCS7 填充；若填充无效则返回原始字节。"""
    if not data:
        return data
    if len(data) % block_size != 0:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        return data
    # 验证填充字节是否全部等于 pad_len
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data
    return data[:-pad_len]


def _ext_for_type(media_type: str) -> str:
    """根据媒体类型返回默认文件扩展名。"""
    return {
        "image": ".jpg",
        "voice": ".silk",
        "video": ".mp4",
        "file": "",
    }.get(media_type, "")
