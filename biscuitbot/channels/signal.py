"""Signal 渠道实现，使用 signal-cli 守护进程的 JSON-RPC 接口。

所属模块与项目作用
==================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 Signal 平台组件。
在项目架构中起到的作用：通过 signal-cli 守护进程的 HTTP JSON-RPC 接口将 Signal 的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：基于 signal-cli 守护进程的 HTTP 模式（需使用 -a 标志指定账户），通过 SSE 接收入站消息。
- 鉴权：依赖 signal-cli 已配置的 Signal 账户，无需额外令牌（通过守护进程认证）。
- 消息格式：支持 Markdown 到 Signal 文本样式的转换（粗体、斜体、删除线、代码块、表格等）。
- 媒体处理：入站附件从 signal-cli 存储目录复制到项目媒体目录；出站附件通过守护进程上传。
- 群组支持：支持群组消息缓冲、@提及检测和基于策略的响应控制。
- 打字指示器：支持周期性发送输入状态，并在消息发送后自动停止。
- 策略配置：分别针对 DM 和群组消息的响应策略（白名单、@提及要求等）。
"""

from __future__ import annotations

import asyncio  # 异步事件循环与并发原语
import json  # JSON 序列化/反序列化（SSE 事件解析）
import re  # 正则表达式（Markdown 解析与样式提取）
import shutil  # 文件复制（入站附件处理）
import unicodedata  # Unicode 字符属性（表格渲染中的东亚字符宽度计算）
from collections import deque  # 固定长度队列（群组消息缓冲）
from collections.abc import AsyncIterator, Callable  # 异步迭代器与可调用对象类型
from contextlib import asynccontextmanager  # 异步上下文管理器装饰器
from dataclasses import dataclass, field  # 数据类装饰器与字段
from pathlib import Path  # 路径处理（附件目录）
from typing import Any  # 类型注解支持

import httpx  # 异步 HTTP 客户端
from pydantic import Field, computed_field, field_validator  # Pydantic 模型字段与校验器

from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 入站/出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_media_dir  # 媒体目录获取
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.pairing import is_approved  # 配对授权检查
from biscuitbot.utils.helpers import safe_filename, split_message  # 安全文件名与消息分块


@dataclass
class _Run:
    """Markdown 解析过程中的文本片段（run），携带样式集合与不透明标记。

    用于 _markdown_to_signal 的中间表示：将输入文本切分为多个 run，
    每个 run 记录其文本内容、累积的样式（BOLD/ITALIC 等）以及是否为
    不透明内容（代码块/表格，跳过后续行内样式处理）。
    """

    text: str
    styles: frozenset[str] = field(default_factory=frozenset)
    opaque: bool = False  # 代码块/表格内容——跳过后续行内模式处理


# 以下正则用于将 Markdown 转换为 Signal 纯文本 + textStyle 区间
_SIG_CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\n?([\s\S]*?)```")  # 围栏代码块
_SIG_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")  # 行内代码
_SIG_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)  # ATX 标题
_SIG_BLOCKQUOTE_RE = re.compile(r"^>\s*(.*)$", re.MULTILINE)  # 引用块
_SIG_BULLET_RE = re.compile(r"^[-*]\s+", re.MULTILINE)  # 无序列表项
_SIG_OLIST_RE = re.compile(r"^(\d+)\.\s+", re.MULTILINE)  # 有序列表项
_SIG_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")  # 链接 [text](url)
_SIG_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)  # 粗体 ** 或 __
_SIG_ITALIC_RE = re.compile(  # 斜体 * 或 _（避免与粗体冲突）
    r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<![a-zA-Z0-9_])_([^_\n]+)_(?![a-zA-Z0-9_])"
)
_SIG_STRIKE_RE = re.compile(r"~~(.+?)~~|(?<![~\w])~([^~\n]+)~(?![~\w])", re.DOTALL)  # 删除线 ~~ 或 ~
_SIG_TOKEN_RE = re.compile(r"\x00C(\d+)\x00")  # 代码块/表格占位符 token

# 用于在表格单元格渲染为纯文本时剥离行内 Markdown 的模式集合。
# 与上面的样式正则分开定义，因为单元格剥离需要固定且狭窄的子集
# （不支持单星号斜体、单波浪号删除线），且要求每个模式的 group 1 即为内容。
_SIG_CELL_STRIP_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),  # 剥离粗体
    (re.compile(r"__(.+?)__"), r"\1"),  # 剥离下划线粗体
    (re.compile(r"~~(.+?)~~"), r"\1"),  # 剥离删除线
    (re.compile(r"`([^`]+)`"), r"\1"),  # 剥离行内代码
)


def _utf16_len(s: str) -> int:
    """返回字符串的 UTF-16 码元长度，与 Signal BodyRange 语义一致。

    Signal 的 textStyle 区间以 UTF-16 码元为单位计算偏移与长度，
    而 Python 的 len() 计算的是 Unicode 码点数量。对于包含非 BMP 字符
    （如 emoji）的文本，二者会不一致，因此需要按 UTF-16 编码长度计算。
    """
    return len(s.encode("utf-16-le")) // 2


def _sig_strip_cell(s: str) -> str:
    """剥离表格单元格中的行内 Markdown 标记，返回纯文本。

    用于表格渲染时将单元格内容归一化为纯文本（保留内容，去除样式标记）。
    """
    for pattern, repl in _SIG_CELL_STRIP_PATTERNS:
        s = pattern.sub(repl, s)
    return s.strip()


def _sig_render_table(table_lines: list[str]) -> str:
    """将 Markdown 管道表格渲染为等宽纯文本。

    解析管道表格的各行，按列对齐（考虑东亚字符双倍宽度），
    用横线分隔表头与表体，输出固定宽度的纯文本表示。
    """

    def dw(s: str) -> int:
        # 计算字符串的显示宽度：东亚宽字符（W/F）占 2，其余占 1
        return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)

    rows: list[list[str]] = []
    has_sep = False
    for line in table_lines:
        cells = [_sig_strip_cell(c) for c in line.strip().strip("|").split("|")]
        # 检测分隔行（如 |---|---|）
        if all(re.match(r"^:?-+:?$", c) for c in cells if c):
            has_sep = True
            continue
        rows.append(cells)
    if not rows or not has_sep:
        return "\n".join(table_lines)

    ncols = max(len(r) for r in rows)
    # 补齐每行的列数，使所有行列数一致
    for r in rows:
        r.extend([""] * (ncols - len(r)))
    # 计算每列的最大显示宽度
    widths = [max(dw(r[c]) for r in rows) for c in range(ncols)]

    def dr(cells: list[str]) -> str:
        # 按列宽对齐渲染一行
        return "  ".join(f"{c}{' ' * (w - dw(c))}" for c, w in zip(cells, widths))

    out = [dr(rows[0])]  # 表头
    out.append("  ".join("─" * w for w in widths))  # 分隔线
    for row in rows[1:]:
        out.append(dr(row))  # 表体
    return "\n".join(out)


def _markdown_to_signal(text: str) -> tuple[str, list[str]]:
    """将 Markdown 文本转换为 Signal 纯文本 + textStyle 区间。

    返回 ``(plain_text, text_styles)``，其中 ``text_styles`` 是
    ``"start:length:STYLE"`` 格式的字符串列表，用于 signal-cli 的
    ``textStyle`` 参数。

    转换分三个阶段：
    1. 文本级：提取代码块和表格为占位符，保护其内容不被行内样式处理。
    2. run 级：基于 run 列表依次应用行内模式（代码、标题、引用、列表、
       链接、粗体、斜体、删除线），累积样式到每个 run。
    3. 组装：将 run 拼接为纯文本，并以 UTF-16 码元为单位输出样式区间。
    """
    if not text:
        return text, []

    # 阶段 1（文本级）：用占位符 token 提取代码块和表格，
    # 使其内容免受后续行内样式处理的影响。
    protected: list[str] = []

    def save_code(m: re.Match) -> str:
        # 将代码块内容存入 protected 列表，返回占位符 token
        protected.append(m.group(1))
        return f"\x00C{len(protected) - 1}\x00"

    text = _SIG_CODE_BLOCK_RE.sub(save_code, text)

    # 逐行检测并渲染管道表格
    lines = text.split("\n")
    rebuilt: list[str] = []
    i = 0
    while i < len(lines):
        if re.match(r"^\s*\|.+\|", lines[i]):
            tbl: list[str] = []
            # 收集连续的表格行
            while i < len(lines) and re.match(r"^\s*\|.+\|", lines[i]):
                tbl.append(lines[i])
                i += 1
            rendered = _sig_render_table(tbl)
            if rendered != "\n".join(tbl):
                # 渲染结果与原文本不同，存入 protected 并用占位符替换
                protected.append(rendered)
                rebuilt.append(f"\x00C{len(protected) - 1}\x00")
            else:
                rebuilt.extend(tbl)
        else:
            rebuilt.append(lines[i])
            i += 1
    text = "\n".join(rebuilt)

    # 阶段 2（run 级）：基于 run 列表处理行内模式
    runs: list[_Run] = [_Run(text)]

    def transform(
        pattern: re.Pattern,
        make_runs: Callable[[re.Match, frozenset[str]], list[_Run]],
    ) -> None:
        # 对每个非不透明 run 应用 pattern，将匹配项替换为 make_runs 返回的 run
        new_runs: list[_Run] = []
        for run in runs:
            if run.opaque:
                new_runs.append(run)
                continue
            pos = 0
            for m in pattern.finditer(run.text):
                if m.start() > pos:
                    # 匹配前的普通文本，保留原样式
                    new_runs.append(_Run(run.text[pos : m.start()], run.styles))
                new_runs.extend(make_runs(m, run.styles))
                pos = m.end()
            if pos < len(run.text):
                # 末尾的普通文本
                new_runs.append(_Run(run.text[pos:], run.styles))
        runs[:] = new_runs

    # 恢复代码块/表格占位符为不透明 MONOSPACE run
    transform(
        _SIG_TOKEN_RE,
        lambda m, s: [_Run(protected[int(m.group(1))], s | {"MONOSPACE"}, opaque=True)],
    )

    # 行内代码（不透明）
    transform(_SIG_INLINE_CODE_RE, lambda m, s: [_Run(m.group(1), s | {"MONOSPACE"}, opaque=True)])

    # 标题 → 粗体纯文本
    transform(_SIG_HEADER_RE, lambda m, s: [_Run(m.group(1), s | {"BOLD"})])

    # 引用块 → 剥离 > 标记
    transform(_SIG_BLOCKQUOTE_RE, lambda m, s: [_Run(m.group(1), s)])

    # 无序列表 → 项目符号
    transform(_SIG_BULLET_RE, lambda m, s: [_Run("• ", s)])

    # 有序列表 → 归一化编号间距
    transform(_SIG_OLIST_RE, lambda m, s: [_Run(m.group(1) + ". ", s)])

    # 链接 → "文本 (url)"，或当文本与 url 相同时仅保留 url
    def _link_runs(m: re.Match, s: frozenset) -> list[_Run]:
        link_text, url = m.group(1), m.group(2)

        def _norm(u: str) -> str:
            # 归一化 url 用于比较：去除协议和 www 前缀，小写
            return re.sub(r"^https?://(www\.)?", "", u).rstrip("/").lower()

        if _norm(url) == _norm(link_text):
            return [_Run(url, s)]
        return [_Run(f"{link_text} ({url})", s)]

    transform(_SIG_LINK_RE, _link_runs)

    # 粗体（在斜体之前处理，避免 ** 干扰）
    transform(_SIG_BOLD_RE, lambda m, s: [_Run(m.group(1) or m.group(2), s | {"BOLD"})])

    # 斜体（单 * 或 _）
    transform(_SIG_ITALIC_RE, lambda m, s: [_Run(m.group(1) or m.group(2), s | {"ITALIC"})])

    # 删除线：~~text~~（标准）或 ~text~（单波浪号变体）
    transform(_SIG_STRIKE_RE, lambda m, s: [_Run(m.group(1) or m.group(2), s | {"STRIKETHROUGH"})])

    # 阶段 3：组装输出。偏移与长度以 UTF-16 码元为单位输出，
    # 因为 Signal 的 BodyRange（通过 signal-cli 的 textStyle）按 UTF-16 解释；
    # Python 的 len() 计算码点，会导致每个前置的非 BMP 字符使区间左移 1 单位。
    plain_text = ""
    text_styles: list[str] = []
    utf16_offset = 0
    for run in runs:
        if not run.text:
            continue
        plain_text += run.text
        start = utf16_offset
        length = _utf16_len(run.text)
        utf16_offset += length
        for style in sorted(run.styles):
            text_styles.append(f"{start}:{length}:{style}")

    return plain_text, text_styles


def _partition_styles(
    plain_text: str, chunks: list[str], text_styles: list[str]
) -> list[list[str]]:
    """将 Signal textStyle 区间按消息分块重新分配。

    ``split_message`` 将 ``plain_text`` 切分为多个分块（在边界处可能裁剪空白），
    但 ``_markdown_to_signal`` 产生的样式区间以相对于完整 ``plain_text`` 的
    UTF-16 偏移表示。本函数将样式区间按分块重新分配，偏移重基到各分块的起始。
    跨越边界的区间会被切分到相邻的分块；完全落在裁剪空白处的区间会被丢弃。
    """
    if not chunks:
        return []
    if not text_styles:
        return [[] for _ in chunks]

    # 定位每个分块在 plain_text 中的 UTF-16 起始位置。
    # split_message 在边界处左裁剪（但不在第一个分块前裁剪），
    # 因此这里跳过分块间的空白以镜像该行为。
    chunk_ranges: list[tuple[int, int]] = []
    cursor = 0  # plain_text 中的 Python 码点游标
    for i, chunk in enumerate(chunks):
        if i > 0:
            while cursor < len(plain_text) and plain_text[cursor].isspace():
                cursor += 1
        utf16_start = _utf16_len(plain_text[:cursor])
        utf16_end = utf16_start + _utf16_len(chunk)
        chunk_ranges.append((utf16_start, utf16_end))
        cursor += len(chunk)

    result: list[list[str]] = [[] for _ in chunks]
    for entry in text_styles:
        s, ln, style = entry.split(":", 2)
        r_start = int(s)
        r_end = r_start + int(ln)
        for i, (c_start, c_end) in enumerate(chunk_ranges):
            # 跳过与该分块无交集的区间
            if r_end <= c_start or r_start >= c_end:
                continue
            # 将区间裁剪到分块范围内，并重基偏移
            new_start = max(r_start, c_start) - c_start
            new_end = min(r_end, c_end) - c_start
            new_length = new_end - new_start
            if new_length > 0:
                result[i].append(f"{new_start}:{new_length}:{style}")
    return result


class SignalDMConfig(Base):
    """Signal 私聊（DM）策略配置。"""

    enabled: bool = False
    policy: str = "allowlist"  # "open"（开放）或 "allowlist"（白名单）
    allow_from: list[str] = Field(default_factory=list)  # 允许的电话号码/UUID 列表


class SignalGroupConfig(Base):
    """Signal 群组策略配置。"""

    enabled: bool = False
    policy: str = "allowlist"  # "open" 或 "allowlist"——决定在哪些群组中运作
    allow_from: list[str] = Field(default_factory=list)  # 白名单策略下允许的群组 ID 列表
    require_mention: bool = True  # 是否要求 @提及机器人才响应


class SignalConfig(Base):
    """Signal 渠道配置，使用 signal-cli 守护进程（仅 HTTP 模式 + -a 标志）。"""

    enabled: bool = False
    phone_number: str = ""  # Signal 电话号码（如 "+1234567890"）
    daemon_host: str = "localhost"  # 守护进程主机
    daemon_port: int = 8080  # 守护进程端口
    group_message_buffer_size: int = 20  # 为群组上下文保留的最近消息数量
    # 覆盖 signal-cli 写入入站附件的目录。为 None 时默认为
    # ~/.local/share/signal-cli/attachments（守护进程在 Linux 上的平台默认值）。
    # 若守护进程运行于自定义 XDG_DATA_HOME 下，或在 macOS/Windows 上默认路径不同，则需设置此项。
    attachments_dir: str | None = None
    dm: SignalDMConfig = Field(default_factory=SignalDMConfig)  # 私聊策略
    group: SignalGroupConfig = Field(default_factory=SignalGroupConfig)  # 群组策略

    @field_validator("group_message_buffer_size")
    @classmethod
    def _validate_buffer_size(cls, v: int) -> int:
        # 校验群组消息缓冲大小必须为正数
        if v <= 0:
            raise ValueError("group_message_buffer_size must be > 0")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allow_from(self) -> list[str]:
        """基类 is_allowed() 检查使用的聚合白名单。

        返回 dm.allow_from 与 group.allow_from 的并集，使基类渠道门控在
        任一子策略已配置时都能看到已填充的列表。任一子列表中的 ``"*"``
        通配符会传播为允许全部。
        """
        return list(dict.fromkeys(self.dm.allow_from + self.group.allow_from))


class SignalChannel(BaseChannel):
    """
    Signal 渠道，使用 signal-cli 守护进程的 HTTP JSON-RPC 接口。

    需要 signal-cli 守护进程运行在 HTTP 模式：
    - signal-cli -a +1234567890 daemon --http localhost:8080

    详见 https://github.com/AsamK/signal-cli 配置说明。
    """

    name = "signal"
    display_name = "Signal"
    _TYPING_REFRESH_SECONDS = 10.0  # 打字指示器刷新间隔（秒）
    _MAX_MESSAGE_LEN = 64_000  # signal-cli 实际限制（协议最大约 64 KB）
    _HTTP_TIMEOUT_SECONDS = 60.0  # HTTP 请求超时（秒）

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return SignalConfig().model_dump(by_alias=True)

    def __init__(self, config: SignalConfig, bus: MessageBus):
        if isinstance(config, dict):
            config = SignalConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: SignalConfig = config
        self._http: httpx.AsyncClient | None = None  # HTTP 客户端
        self._request_id = 0  # JSON-RPC 请求 ID 自增计数器
        self._sse_task: asyncio.Task | None = None  # SSE 接收循环任务
        self._typing_tasks: dict[str, asyncio.Task] = {}  # 各会话的打字指示器任务
        self._typing_uuid_warnings: set[str] = set()  # 已警告过的 UUID-only 接收者集合
        self._account_id_aliases: set[str] = set()  # 机器人账户的已知标识符别名
        self._remember_account_id_alias(self.config.phone_number)

        # 群组上下文的滚动消息缓冲（group_id -> 消息 deque）
        # 每条消息是一个字典，包含：sender_name, sender_number, content, timestamp
        self._group_buffers: dict[str, deque] = {}

    def is_allowed(self, sender_id: str) -> bool:
        """重写基类检查，以归一化和拆分管道连接的标识符。

        来自 Signal 的 ``sender_id`` 是由 ``_collect_sender_id_parts`` 生成的
        管道连接复合标识符；allow_from 条目可以是单个标识符或复合标识符，
        可能使用 ``+`` 前缀变体也可能不使用。委托给 ``_sender_matches_allowlist``
        使基类门控与按策略的 DM 门控保持一致。
        """
        allow_list = self.config.allow_from
        if "*" in allow_list:
            return True
        if self._sender_matches_allowlist(sender_id, allow_list):
            return True
        if self._sender_approved_via_pairing(sender_id):
            return True
        if not allow_list:
            self.logger.warning("allow_from is empty — all access denied")
        return False

    def _sender_approved_via_pairing(self, sender_id: str) -> bool:
        """若 sender_id 的任一归一化变体在配对存储中则返回 True。

        配对授权可能记录在 signal 暴露的任一标识符形式下
        （带/不带 ``+`` 的电话号码、UUID、ACI），因此我们对管道连接
        复合标识符的每一部分检查 ``is_approved``。
        """
        for part in str(sender_id).split("|"):
            for variant in self._normalize_signal_id(part):
                if is_approved(self.name, variant):
                    return True
        return False

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        """处理已通过策略检查的入站消息。

        ``_check_inbound_policy`` 是 DM/群组访问的权威门控，因此这里跳过
        基类的 ``is_allowed()`` 检查，直接发布到消息总线。被拒绝的 DM
        配对路径会调用 ``super()._handle_message``，它会经过 ``is_allowed``
        并下发配对码。
        """
        meta = metadata or {}
        if self.supports_streaming:
            meta = {**meta, "_wants_stream": True}
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=str(sender_id),
                chat_id=str(chat_id),
                content=content,
                media=media or [],
                metadata=meta,
                session_key_override=session_key,
            )
        )

    async def start(self) -> None:
        """启动 Signal 渠道并连接到 signal-cli 守护进程。"""
        if not self.config.phone_number:
            self.logger.error("Signal account not configured")
            return

        self._running = True
        await self._start_http_mode()

    async def _start_http_mode(self) -> None:
        """以 Server-Sent Events 方式启动 Signal 渠道接收消息。"""
        base_url = f"http://{self.config.daemon_host}:{self.config.daemon_port}"
        reconnect_delay_s = 1.0  # 初始重连延迟
        max_reconnect_delay_s = 30.0  # 最大重连延迟

        while self._running:
            try:
                self.logger.info("Connecting to signal-cli daemon at {}...", base_url)

                # 创建 HTTP 客户端
                self._http = httpx.AsyncClient(
                    timeout=self._HTTP_TIMEOUT_SECONDS, base_url=base_url
                )

                # 测试连接
                try:
                    response = await self._http.get("/api/v1/check")
                    if response.status_code == 200:
                        self.logger.info("Connected to signal-cli daemon")
                    else:
                        raise ConnectionRefusedError(
                            f"signal-cli daemon check returned status {response.status_code}"
                        )
                except Exception as e:
                    raise ConnectionRefusedError(f"signal-cli daemon not responding: {e}")

                # 连接检查成功后重置重连延迟
                reconnect_delay_s = 1.0

                # 确保账户级别的打字指示器已启用
                await self._ensure_typing_indicators_enabled()

                # 启动 SSE 接收器并监督它。若它在仍在运行时退出，
                # 视为断开连接并重连。
                self._sse_task = asyncio.create_task(self._sse_receive_loop())
                await self._sse_task
                if self._running:
                    raise ConnectionError("Signal SSE stream ended unexpectedly")

            except asyncio.CancelledError:
                break
            except ConnectionRefusedError as e:
                self.logger.error(
                    "{}. Make sure signal-cli daemon is running: "
                    "signal-cli -a {} daemon --http {}:{}",
                    e,
                    self.config.phone_number,
                    self.config.daemon_host,
                    self.config.daemon_port,
                )
            except Exception as e:
                self.logger.error("Signal channel error: {}", e)
            finally:
                if self._sse_task:
                    if not self._sse_task.done():
                        self._sse_task.cancel()
                    try:
                        await self._sse_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        self.logger.debug("signal: SSE task await failed during stop", exc_info=True)
                    self._sse_task = None
                if self._http:
                    await self._http.aclose()
                    self._http = None

            if self._running:
                self.logger.info(
                    "Reconnecting to signal-cli daemon in {:.0f} seconds...", reconnect_delay_s
                )
                await asyncio.sleep(reconnect_delay_s)
                reconnect_delay_s = min(reconnect_delay_s * 2, max_reconnect_delay_s)

    async def stop(self) -> None:
        """停止 Signal 渠道。"""
        self._running = False

        # 停止 SSE 任务
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass

        # 取消活跃的打字指示器
        for chat_id in list(self._typing_tasks):
            await self._stop_typing(chat_id)

        # 关闭 HTTP 客户端
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        """通过 Signal 发送消息。"""
        is_progress_message = bool(msg.metadata.get("_progress"))
        try:
            plain_text, text_styles = _markdown_to_signal(msg.content)
            if not plain_text and not msg.media:
                return
            recipient_params = self._recipient_params(msg.chat_id)

            chunks = split_message(plain_text, self._MAX_MESSAGE_LEN) if plain_text else [""]
            chunk_styles = _partition_styles(plain_text, chunks, text_styles)
            for i, chunk in enumerate(chunks):
                params: dict[str, Any] = {"message": chunk}
                if chunk_styles[i]:
                    params["textStyle"] = chunk_styles[i]
                params.update(recipient_params)
                # 附件仅在第一个分块携带
                if msg.media and i == 0:
                    params["attachments"] = msg.media

                response = await self._send_request("send", params)

                if "error" in response:
                    self.logger.error("Error sending Signal message: {}", response['error'])
                    raise RuntimeError(f"signal-cli send failed: {response['error']}")
                else:
                    self.logger.debug(
                        f"Signal message sent, timestamp: {response.get('result', {}).get('timestamp')}"
                    )

        except Exception:
            self.logger.exception("Error sending Signal message")
            raise
        finally:
            # 进度消息保持 typing 活跃；最终回复停止 typing
            if not is_progress_message:
                # 避免快速响应时 START->STOP 不可见（某些 Signal 客户端不渲染），
                # 让指示器自然过期（约 15 秒）
                await self._stop_typing(msg.chat_id, send_stop=False)

    async def _sse_receive_loop(self) -> None:
        """通过 Server-Sent Events 接收消息（HTTP 模式）。"""
        if not self._http:
            raise RuntimeError("HTTP client not initialized for Signal SSE stream")

        self.logger.info("Started Signal message receive loop (SSE)")

        try:
            async with self._http.stream("GET", "/api/v1/events") as response:
                if response.status_code != 200:
                    raise ConnectionError(
                        f"SSE connection failed with status {response.status_code}"
                    )

                self.logger.info("Subscribed to Signal messages via SSE")

                # 用于跨多行累积 SSE 数据的缓冲区
                event_buffer = []

                async for line in response.aiter_lines():
                    if not self._running:
                        break

                    # 调试：记录原始 SSE 行（心跳包除外）
                    if line and line != ":":
                        self.logger.debug("SSE line received: {}", line[:200])

                    # SSE 格式处理
                    if isinstance(line, str):
                        # 空行表示事件结束
                        if not line or line == ":":
                            if event_buffer:
                                # 尝试解析累积的数据
                                data_str = ""
                                try:
                                    data_str = "\n".join(event_buffer)
                                    data = json.loads(data_str)
                                    self.logger.debug("SSE event parsed: {}", data)
                                    await self._handle_receive_notification(data)
                                except json.JSONDecodeError as e:
                                    self.logger.warning(
                                        "Invalid JSON in SSE buffer: {}, data: {}",
                                        e,
                                        data_str[:200],
                                    )
                                finally:
                                    event_buffer = []

                        # "data:" 行——累积数据
                        elif line.startswith("data:"):
                            # SSE 规范：去除 "data:" 后一个可选的前导空格
                            event_buffer.append(line[6:] if line[5:6] == " " else line[5:])

                        # "event:" 行——仅记录日志（我们只关心 data）
                        elif line.startswith("event:"):
                            pass  # 暂时忽略事件类型

                if self._running:
                    raise ConnectionError("Signal SSE stream closed by remote endpoint")

        except asyncio.CancelledError:
            self.logger.info("SSE receive loop cancelled")
            raise
        except Exception as e:
            self.logger.error("Error in SSE receive loop: {}", e)
            raise

    @asynccontextmanager
    async def _safe_handle(self, action: str, payload: Any = None) -> AsyncIterator[None]:
        """吞没并记录顶层处理块的任何异常。

        以 `self.logger.error` 记录操作名称、异常，以及有界 ``repr`` 的
        问题载荷，使问题输入可从日志中恢复，无需按时间戳关联。
        """
        try:
            yield
        except Exception as e:
            snippet = repr(payload)[:200] if payload is not None else ""
            text = f"Error in {action}: {e}"
            if snippet:
                text += f" | payload={snippet}"
            self.logger.opt(exception=True).error(text)

    async def _handle_receive_notification(self, params: dict[str, Any]) -> None:
        """处理来自 signal-cli 的入站消息通知。"""
        self.logger.debug("_handle_receive_notification called with: {}", params)
        async with self._safe_handle("receive notification", params):
            # 从 SSE 通知中提取 envelope：{"envelope": {...}}
            envelope = params.get("envelope", {})

            self.logger.debug("Extracted envelope: {}", envelope)

            if not envelope:
                self.logger.debug("No envelope found in params")
                return

            # 提取发送者信息
            sender_parts = self._collect_sender_id_parts(envelope)
            source_name = envelope.get("sourceName")

            if not sender_parts:
                self.logger.debug("Received message without source, skipping")
                return

            sender_number = self._primary_sender_id(sender_parts)
            sender_id = "|".join(sender_parts)

            # 记住机器人账户的别名，用于稳健的提及匹配
            if any(self._id_matches_account(part) for part in sender_parts):
                for part in sender_parts:
                    self._remember_account_id_alias(part)

            # 检查不同的消息类型
            data_message = envelope.get("dataMessage")
            sync_message = envelope.get("syncMessage")
            typing_message = envelope.get("typingMessage")
            receipt_message = envelope.get("receiptMessage")

            # 忽略回执消息（送达/已读回执）
            if receipt_message:
                return

            # 处理数据消息（来自他人的入站消息）
            if data_message:
                await self._handle_data_message(sender_id, sender_number, data_message, source_name)

            # 处理同步消息（从其他设备发出的消息）
            elif sync_message and sync_message.get("sentMessage"):
                sent_msg = sync_message["sentMessage"]
                destination = sent_msg.get("destination") or sent_msg.get("destinationNumber")
                if destination:
                    self.logger.debug(
                        "Sync message sent to {}: {}", destination, sent_msg.get("message", "")[:50]
                    )

            # 处理打字指示器（静默忽略）
            elif typing_message:
                pass  # 忽略打字指示器

    async def _handle_data_message(
        self,
        sender_id: str,
        sender_number: str,
        data_message: dict[str, Any],
        sender_name: str | None,
    ) -> None:
        """处理数据消息（文本、附件等）。"""
        message_text = data_message.get("message") or ""
        attachments = data_message.get("attachments", [])
        mentions = data_message.get("mentions", [])
        timestamp = data_message.get("timestamp")

        self.logger.info(
            "Data message from {}: groupInfo={}, groupV2={}, keys={}",
            sender_number,
            data_message.get("groupInfo"),
            data_message.get("groupV2"),
            list(data_message.keys()),
        )

        # 忽略表情回应消息
        if data_message.get("reaction"):
            self.logger.debug(
                "Ignoring reaction message from {}: {}", sender_number, data_message["reaction"]
            )
            return
        # 忽略空消息
        if not message_text and not attachments:
            self.logger.debug("Ignoring empty message from {}", sender_number)
            return

        group_info = data_message.get("groupInfo")
        group_v2 = data_message.get("groupV2")
        is_group_message = group_info is not None or group_v2 is not None
        group_id = self._extract_group_id(group_info, group_v2)

        allowed, chat_id = self._check_inbound_policy(
            sender_id=sender_id,
            sender_number=sender_number,
            group_id=group_id,
            is_group_message=is_group_message,
            message_text=message_text,
            mentions=mentions,
            sender_name=sender_name,
            timestamp=timestamp,
        )
        if not allowed:
            # 镜像 Slack 行为：让被拒绝的 DM 到达基类 _handle_message，
            # 以便它回复配对码。群组拒绝则直接丢弃。
            if not is_group_message and self.config.dm.enabled:
                await super()._handle_message(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content="",
                    is_dm=True,
                )
            return

        content, media_paths = self._assemble_inbound_content(
            sender_name=sender_name,
            sender_number=sender_number,
            message_text=message_text,
            attachments=attachments,
            mentions=mentions,
            is_group_message=is_group_message,
            chat_id=chat_id,
        )

        self.logger.debug("Signal message from {}: {}...", sender_number, content[:50])

        await self._start_typing(chat_id)
        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media_paths,
                metadata={
                    "timestamp": timestamp,
                    "sender_name": sender_name,
                    "sender_number": sender_number,
                    "is_group": is_group_message,
                    "group_id": group_id,
                },
                is_dm=not is_group_message,
            )
        except Exception:
            await self._stop_typing(chat_id)
            raise

    def _check_inbound_policy(
        self,
        *,
        sender_id: str,
        sender_number: str,
        group_id: str | None,
        is_group_message: bool,
        message_text: str,
        mentions: list,
        sender_name: str | None,
        timestamp: int | None,
    ) -> tuple[bool, str]:
        """决定是否让入站消息通过 DM/群组策略。

        返回 ``(allow, chat_id)``。有一个副作用：当群组消息通过
        启用+白名单门控时，会在提及检查之前将其追加到群组的滚动上下文缓冲。
        """
        if is_group_message:
            chat_id = group_id or sender_number
            if not self.config.group.enabled:
                self.logger.info("Ignoring group message from {} (groups disabled)", chat_id)
                return False, chat_id
            if (
                self.config.group.policy == "allowlist"
                and chat_id not in self.config.group.allow_from
            ):
                self.logger.info(
                    "Ignoring group message from {} (policy: {})",
                    chat_id,
                    self.config.group.policy,
                )
                return False, chat_id

            # 通过门控的群组消息追加到上下文缓冲
            self._add_to_group_buffer(
                group_id=chat_id,
                sender_name=sender_name or sender_number,
                sender_number=sender_number,
                message_text=message_text,
                timestamp=timestamp,
            )

            # 命令总是响应；非命令消息需要满足提及要求
            is_command = bool(message_text and message_text.strip().startswith("/"))
            if not is_command and not self._should_respond_in_group(message_text, mentions):
                self.logger.info(
                    "Ignoring group message (require_mention: {})",
                    self.config.group.require_mention,
                )
                return False, chat_id
            return True, chat_id

        # 私聊消息
        chat_id = sender_number
        if not self.config.dm.enabled:
            self.logger.debug("Ignoring DM from {} (DMs disabled)", sender_id)
            return False, chat_id
        if self.config.dm.policy == "allowlist":
            if not self._sender_matches_allowlist(sender_id, self.config.dm.allow_from):
                self.logger.debug(
                    "Ignoring DM from {} (policy: {})", sender_id, self.config.dm.policy
                )
                return False, chat_id
        return True, chat_id

    def _assemble_inbound_content(
        self,
        *,
        sender_name: str | None,
        sender_number: str,
        message_text: str,
        attachments: list,
        mentions: list,
        is_group_message: bool,
        chat_id: str,
    ) -> tuple[str, list[str]]:
        """为入站消息构建 ``(content, media_paths)``。

        拉取群组上下文，剥离机器人提及，在群组消息前缀发送者显示名，
        并将任何附件从 signal-cli 存储复制到渠道媒体目录。
        """
        content_parts: list[str] = []
        media_paths: list[str] = []

        if is_group_message:
            buffer_context = self._get_group_buffer_context(chat_id)
            if buffer_context:
                content_parts.append(f"[Recent group messages for context:]\n{buffer_context}\n---")

        if message_text:
            if is_group_message:
                # 群组消息：剥离机器人提及，前缀发送者显示名
                message_text = self._strip_bot_mention(message_text, mentions)
                display_name = sender_name or sender_number
                message_text = f"[{display_name}]: {message_text}"
            content_parts.append(message_text)

        if attachments:
            media_dir = get_media_dir("signal")
            for attachment in attachments:
                attachment_id = attachment.get("id")
                content_type = attachment.get("contentType", "")
                filename = attachment.get("filename") or f"attachment_{attachment_id}"
                if not attachment_id:
                    continue
                try:
                    # 从 signal-cli 存储目录复制附件到媒体目录
                    source_path = self._signal_attachments_dir() / attachment_id
                    if source_path.exists():
                        dest_path = media_dir / f"signal_{safe_filename(filename)}"
                        shutil.copy2(source_path, dest_path)
                        media_paths.append(str(dest_path))
                        # 推断媒体类型
                        media_type = content_type.split("/")[0] if "/" in content_type else "file"
                        if media_type not in ("image", "audio", "video"):
                            media_type = "file"
                        content_parts.append(f"[{media_type}: {dest_path}]")
                        self.logger.debug("Downloaded attachment: {} -> {}", filename, dest_path)
                    else:
                        self.logger.warning("Attachment not found: {}", source_path)
                        content_parts.append(f"[attachment: {filename} - not found]")
                except Exception as e:
                    self.logger.warning("Failed to process attachment {}: {}", filename, e)
                    content_parts.append(f"[attachment: {filename} - error]")

        content = "\n".join(content_parts) if content_parts else "[empty message]"
        return content, media_paths

    def _add_to_group_buffer(
        self,
        group_id: str,
        sender_name: str,
        sender_number: str,
        message_text: str,
        timestamp: int | None,
    ) -> None:
        """将消息添加到群组的滚动缓冲。

        Args:
            group_id: 群组 ID
            sender_name: 发送者显示名
            sender_number: 发送者电话号码
            message_text: 消息内容
            timestamp: 消息时间戳
        """
        # 若该群组尚无缓冲则创建（deque 满时自动丢弃最旧消息）
        if group_id not in self._group_buffers:
            self._group_buffers[group_id] = deque(maxlen=self.config.group_message_buffer_size)

        # 将消息添加到缓冲
        self._group_buffers[group_id].append(
            {
                "sender_name": sender_name,
                "sender_number": sender_number,
                "content": message_text,
                "timestamp": timestamp,
            }
        )

        self.logger.debug(
            "Added message to group buffer {}: {}/{}",
            group_id,
            len(self._group_buffers[group_id]),
            self.config.group_message_buffer_size,
        )

    def _get_group_buffer_context(self, group_id: str) -> str:
        """从群组消息缓冲获取格式化的上下文。

        Args:
            group_id: 群组 ID

        Returns:
            最近消息的格式化字符串（排除当前消息）
        """
        if group_id not in self._group_buffers:
            return ""

        buffer = self._group_buffers[group_id]
        if len(buffer) <= 1:  # 仅当前消息，无上下文
            return ""

        # 格式化除最后一条（当前消息）外的所有消息
        # 我们希望在提及之前显示上下文
        context_messages = list(buffer)[:-1]  # 排除最后一条（当前消息）

        lines = []
        for msg in context_messages:
            sender = msg["sender_name"]
            content = msg["content"][:200]  # 每条消息限制 200 字符
            lines.append(f"{sender}: {content}")

        return "\n".join(lines)

    def _signal_attachments_dir(self) -> Path:
        """返回 signal-cli 写入入站附件的目录。

        当 ``config.attachments_dir`` 未设置时，默认为
        ``~/.local/share/signal-cli/attachments``（守护进程在 Linux 上的
        平台默认值）。
        """
        configured = self.config.attachments_dir
        if configured:
            return Path(configured).expanduser()
        return Path.home() / ".local/share/signal-cli/attachments"

    @staticmethod
    def _normalize_signal_id(value: str) -> list[str]:
        """归一化 Signal 标识符（电话/uuid/service-id）用于匹配。

        返回该标识符的多种变体：原始值、小写、带/不带 ``+`` 前缀的电话号码。
        """
        raw = value.strip()
        if not raw:
            return []

        normalized = [raw, raw.lower()]
        if raw.startswith("+") and len(raw) > 1:
            # 带 + 前缀的电话号码，补充不带 + 的变体
            normalized.append(raw[1:])
        elif raw.isdigit():
            # 纯数字，补充带 + 的变体
            normalized.append(f"+{raw}")
        return list(dict.fromkeys(normalized))

    @classmethod
    def _sender_matches_allowlist(cls, sender_id: str, allow_list: list[str]) -> bool:
        """若 sender_id 的任一归一化变体在 allow_list 中则返回 True。

        ``sender_id`` 和 allow_list 的每个条目都可以是单个标识符或多个
        标识符的管道连接复合（如 ``"+1234567890|uuid-abc"``）；两侧都按
        ``|`` 拆分，每部分经过 ``_normalize_signal_id`` 处理，使白名单条目
        如 ``1234567890`` 能匹配发送者 ``+1234567890``（反之亦然），
        UUID/ACI 仅大小写不同的也能匹配。
        """
        if not allow_list:
            return False
        sender_variants: set[str] = set()
        for part in str(sender_id).split("|"):
            sender_variants.update(cls._normalize_signal_id(part))
        if not sender_variants:
            return False
        allow_variants: set[str] = set()
        for entry in allow_list:
            for part in str(entry).split("|"):
                allow_variants.update(cls._normalize_signal_id(part))
        return bool(sender_variants & allow_variants)

    def _remember_account_id_alias(self, value: str | None) -> None:
        """记住已知的机器人标识符，用于提及匹配。"""
        if not value:
            return
        if not isinstance(value, str):
            return
        for candidate in self._normalize_signal_id(value):
            self._account_id_aliases.add(candidate)

    def _id_matches_account(self, value: str | None) -> bool:
        """当标识符指向机器人账户时返回 True。"""
        if not value:
            return False
        if not isinstance(value, str):
            return False
        return any(
            candidate in self._account_id_aliases for candidate in self._normalize_signal_id(value)
        )

    @staticmethod
    def _collect_sender_id_parts(envelope: dict[str, Any]) -> list[str]:
        """从 envelope 中收集所有已知的发送者标识符变体。"""
        parts: list[str] = []
        for key in (
            "sourceNumber",
            "source",
            "sourceUuid",
            "sourceServiceId",
            "sourceAci",
            "sourceACI",
        ):
            value = envelope.get(key)
            if not isinstance(value, str):
                continue
            candidate = value.strip()
            if candidate and candidate not in parts:
                parts.append(candidate)
        return parts

    @staticmethod
    def _primary_sender_id(sender_parts: list[str]) -> str:
        """选择用于路由的最佳发送者标识符（优先电话号码类 ID）。"""
        for part in sender_parts:
            if part.startswith("+") or part.isdigit():
                return part
        return sender_parts[0] if sender_parts else ""

    @staticmethod
    def _extract_group_id(group_info: Any, group_v2: Any) -> str | None:
        """从 groupInfo/groupV2 载荷中提取群组 ID（兼容 signal-cli 各变体）。"""
        for group_obj in (group_info, group_v2):
            if not isinstance(group_obj, dict):
                continue
            for key in ("groupId", "id", "groupID"):
                value = group_obj.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _mention_id_candidates(mention: dict[str, Any]) -> list[str]:
        """从 mention 载荷中提取可能的标识符字段。"""
        ids: list[str] = []

        def _walk(value: dict[str, Any] | Any, depth: int = 0) -> None:
            # 限制递归深度，避免过深嵌套
            if depth > 2:
                return
            if not isinstance(value, dict):
                return
            for key, child in value.items():
                key_lower = str(key).lower()
                if isinstance(child, str) and child:
                    # 键名包含 number/uuid/serviceid/aci 的视为标识符字段
                    if any(token in key_lower for token in ("number", "uuid", "serviceid", "aci")):
                        ids.append(child)
                elif isinstance(child, dict):
                    _walk(child, depth + 1)

        _walk(mention)
        return list(dict.fromkeys(ids))

    @staticmethod
    def _mention_span(mention: dict[str, Any]) -> tuple[int, int] | None:
        """从 mention 中提取安全的 (start, length) 跨度。"""
        try:
            start = int(mention.get("start", 0))
            length = int(mention.get("length", 0))
        except (TypeError, ValueError):
            return None

        if start < 0 or length <= 0:
            return None
        return (start, length)

    @staticmethod
    def _leading_placeholder_span(text: str | None) -> tuple[int, int] | None:
        """当 mention 元数据缺失时，检测前导的 Signal mention 占位符。

        某些客户端/集成将 mention 作为前导占位符字符（通常为 U+FFFC）传递，
        但载荷中省略了 `mentions` 元数据。
        """
        if not text:
            return None

        start = 0
        # 跳过前导空白
        while start < len(text) and text[start].isspace():
            start += 1

        if start >= len(text):
            return None

        marker = text[start]
        # 仅接受对象替换字符、替换字符或 ESC 作为占位符
        if marker not in ("\ufffc", "\ufffd", "\x1b"):
            return None

        next_index = start + 1
        # 占位符后必须紧跟空白或文本末尾
        if next_index < len(text) and not text[next_index].isspace():
            return None

        return (start, 1)

    def _should_respond_in_group(self, message_text: str, mentions: list[dict[str, Any]]) -> bool:
        """决定机器人是否应响应群组消息。

        Args:
            message_text: 消息文本内容
            mentions: Signal 的 mention 列表（格式：[{"number": "+1234567890", "start": 0, "length": 10}]）

        Returns:
            机器人应响应时返回 True，否则返回 False
        """
        # 群组回复行为仅由 group.require_mention 控制
        if not self.config.group.require_mention:
            return True

        # 若要求提及，检查机器人是否被 @mention
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            for mention_id in self._mention_id_candidates(mention):
                if self._id_matches_account(mention_id):
                    return True

        # 某些 Signal 客户端发出不含接收者标识符的 mention 跨度
        # （用于 handle 风格的提及）。将前导无标识符的 mention 视为
        # 对机器人的提及，以避免假阴性。
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            if self._mention_id_candidates(mention):
                continue
            span = self._mention_span(mention)
            if not span:
                continue
            start, _ = span
            if message_text is not None and not message_text[:start].strip():
                self.logger.debug("Accepting identifier-less leading mention as bot mention")
                return True

        # 某些载荷省略 `mentions`，但消息体中仍包含前导 mention 占位符字符
        if not mentions and self._leading_placeholder_span(message_text):
            self.logger.debug("Accepting leading placeholder mention without mention metadata")
            return True

        # 回退：检查纯文本中是否包含配置的电话号码
        if message_text and self.config.phone_number:
            for account_id in self._normalize_signal_id(self.config.phone_number):
                if account_id and account_id in message_text:
                    return True

        return False

    def _strip_bot_mention(self, text: str, mentions: list[dict[str, Any]]) -> str:
        """从消息文本中移除机器人提及。

        Signal 的 mention 嵌入在文本中，因此需要根据 mentions 数组提供的
        起始位置和长度来移除它们。

        Args:
            text: 原始消息文本
            mentions: 包含 start/length 位置的 mention 对象列表

        Returns:
            移除机器人提及后的文本
        """
        if not text:
            return text

        # 构建机器人 mention 的 (start, length) 元组列表
        bot_mentions = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_ids = self._mention_id_candidates(mention)
            span = self._mention_span(mention)
            if not span:
                continue

            # 按 ID 匹配剥离机器人 mention
            if any(self._id_matches_account(mention_id) for mention_id in mention_ids):
                bot_mentions.append(span)
                continue

            # 也剥离无标识符的前导 mention 跨度（handle 风格提及）
            if not mention_ids:
                start, _ = span
                if not text[:start].strip():
                    bot_mentions.append(span)

        if not bot_mentions:
            # 无 mention 元数据时，检测前导占位符
            placeholder_span = self._leading_placeholder_span(text)
            if placeholder_span:
                bot_mentions.append(placeholder_span)

        # 按起始位置降序排序，从末尾向开头移除
        # 这样可避免移除较早 mention 时位置偏移
        bot_mentions.sort(reverse=True)

        # 逐个移除 mention
        for start, length in bot_mentions:
            if start >= len(text):
                continue
            end = min(len(text), start + length)
            text = text[:start] + text[end:]

        return text.strip()

    @staticmethod
    def _is_group_chat_id(chat_id: str) -> bool:
        """当 chat_id 疑似 Signal 群组 ID（base64）时返回 True。"""
        return "=" in chat_id or (len(chat_id) > 40 and "-" not in chat_id)

    def _recipient_params(self, chat_id: str) -> dict[str, Any]:
        """为 signal-cli JSON-RPC 方法构建接收者参数。"""
        if self._is_group_chat_id(chat_id):
            return {"groupId": chat_id}
        return {"recipient": [chat_id]}

    async def _start_typing(self, chat_id: str) -> None:
        """为会话启动周期性打字指示器更新。"""
        await self._stop_typing(chat_id, send_stop=False)
        await self._send_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    async def _stop_typing(self, chat_id: str, send_stop: bool = True) -> None:
        """停止会话的打字指示器更新。"""
        task = self._typing_tasks.pop(chat_id, None)
        had_task = task is not None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if send_stop and had_task:
            await self._send_typing(chat_id, stop=True)

    async def _typing_loop(self, chat_id: str) -> None:
        """周期性发送打字状态，直到被取消。"""
        try:
            while self._running:
                await asyncio.sleep(self._TYPING_REFRESH_SECONDS)
                await self._send_typing(chat_id, quiet_success=True)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.debug("Typing indicator loop stopped for {}: {}", chat_id, e)

    async def _send_typing(
        self, chat_id: str, stop: bool = False, quiet_success: bool = False
    ) -> None:
        """通过 signal-cli 发送打字 START/STOP 消息。"""
        action = "stop" if stop else "start"
        # UUID-only 接收者可能无法渲染打字指示器，仅警告一次
        if (
            not self._is_group_chat_id(chat_id)
            and chat_id.startswith("+") is False
            and chat_id not in self._typing_uuid_warnings
        ):
            self._typing_uuid_warnings.add(chat_id)
            self.logger.warning(
                "Signal DM recipient is UUID-only (no phone number in envelope). "
                "Some Signal clients may not render typing indicators for this recipient form."
            )
        # signal-cli 对群组/接收者参数有标量/列表两种变体，依次尝试
        candidate_params: list[dict[str, Any]]
        if self._is_group_chat_id(chat_id):
            candidate_params = [{"groupId": chat_id}, {"groupId": [chat_id]}]
        else:
            candidate_params = [{"recipient": chat_id}, {"recipient": [chat_id]}]

        last_error: Any | None = None
        for params in candidate_params:
            if stop:
                params["stop"] = True
            try:
                response = await self._send_request("sendTyping", params)
            except Exception as e:
                last_error = str(e)
                continue

            if "error" not in response:
                if not quiet_success:
                    self.logger.info("Signal typing {} sent for {}", action, chat_id)
                return

            last_error = response["error"]

        self.logger.warning(
            "Failed to send Signal typing {} for {}: {}", action, chat_id, last_error
        )

    async def _ensure_typing_indicators_enabled(self) -> None:
        """在机器人账户上启用打字指示器。"""
        response = await self._send_request("updateConfiguration", {"typingIndicators": True})
        if "error" in response:
            self.logger.warning(
                "Failed to enable Signal typing indicators: {}", response["error"]
            )
        else:
            self.logger.info("Signal typing indicators enabled on account configuration")

    async def _send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """通过 HTTP 发送 JSON-RPC 请求并等待响应。"""
        # 生成请求 ID
        self._request_id += 1
        request_id = self._request_id

        # 构建 JSON-RPC 请求
        request = {"jsonrpc": "2.0", "method": method, "id": request_id}

        if params:
            request["params"] = params

        return await self._send_http_request(request)

    async def _send_http_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """通过 HTTP 发送 JSON-RPC 请求。"""
        if not self._http:
            raise RuntimeError("Not connected to signal-cli daemon")

        try:
            response = await self._http.post("/api/v1/rpc", json=request)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error("HTTP request failed: {}", e)
            return {"error": {"message": str(e)}}
