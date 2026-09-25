"""Shared helpers for decoding ``data:...;base64,...`` URLs to disk.

Historically lived in ``xianaibot.api.server``; now shared by the WebSocket
channel so the ``api`` + ``websocket`` ingress paths apply the same parsing,
size guard, and filesystem layout.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import uuid
from pathlib import Path

from loguru import logger

from xianaibot.utils.helpers import safe_filename

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
MAX_FILE_SIZE = DEFAULT_MAX_BYTES

_DATA_URL_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)*;base64,(.+)$", re.DOTALL)
_MIME_EXTENSION_OVERRIDES = {
    # Python's ``mimetypes`` maps browser-recorded audio/webm to ``.weba`` and
    # audio/ogg to ``.oga`` on macOS. Some transcription APIs validate by the
    # file extension and accept the canonical container extensions instead.
    "application/ogg": ".ogg",
    "audio/ogg": ".ogg",
    "audio/mpga": ".mpga",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "video/webm": ".webm",
    # ``mimetypes`` 对这几个平台依赖的映射不稳定（Windows 读注册表、Linux 读
    # 内置表），而下游 ``utils.document.extract_text`` 完全按扩展名分派解析器，
    # 扩展名错了附件就读不出来。显式钉住。
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
}

# 客户端文件名里保留的「可读 slug」最大长度。落盘名形如
# ``<uuid12>_<slug><ext>``；Windows 的路径总长度上限是 260 字符，而 media
# 目录本身已占约 45 字符，不设上限时一个超长客户端文件名会让 ``write_bytes``
# 直接抛 OSError，最终表现为前端看到「附件解码失败」——用户无法理解。
_MAX_SLUG_CHARS = 48

# 落盘名的随机前缀（``<uuid12>_``）。前缀由 ``save_base64_data_url`` 拼出，
# 由 :func:`readable_media_name` 剥掉；这个模式是两边的唯一约定，改前缀格式
# 时只需改这里。
_GENERATED_NAME_RE = re.compile(r"^[0-9a-f]{12}_(?=\S)")

# 允许采用客户端文件名扩展名的白名单。**只用来决定扩展名**（可读部分的处理
# 见 :func:`_client_slug`）：浏览器常把 docx 报成 application/octet-stream，
# 不看客户端扩展名就会落成 ``.bin``，下游按扩展名分派的解析器全都读不出来。
# 不在此列表中的扩展名一律回退到 MIME 映射。
_CLIENT_EXT_ALLOWED: frozenset[str] = frozenset({
    # 文档
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml", ".log",
    ".zip",
    # 图片
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    # 视频
    ".mp4", ".mov", ".webm", ".m4v",
    # 音频
    ".mp3", ".wav", ".m4a", ".ogg", ".mpga", ".aac", ".flac",
})


class FileSizeExceededError(Exception):
    """Raised when a decoded payload exceeds the caller's size limit."""


FileSizeExceeded = FileSizeExceededError


def estimate_decoded_size(b64_payload: str) -> int:
    """按 base64 长度估算解码后的字节数（每 4 字符约 3 字节）。

    用于**解码前**的大小预检：先 ``b64decode`` 再比长度会让超限载荷
    在内存里完整展开一次（几十 MB 的 str + bytes 同时在世），是一个
    廉价的 DoS 放大点。
    """
    return len(b64_payload) * 3 // 4 + 1


def _resolve_extension(name: str | None, mime_type: str) -> str:
    """决定落盘扩展名：优先白名单内的客户端扩展名，否则回退 MIME 映射。"""
    if name:
        ext = Path(name).suffix.lower()
        if ext in _CLIENT_EXT_ALLOWED:
            return ext
    return (
        _MIME_EXTENSION_OVERRIDES.get(mime_type)
        or mimetypes.guess_extension(mime_type)
        or ".bin"
    )


def readable_media_name(filename: str) -> str:
    """把落盘名还原成适合展示的名字（剥掉随机前缀）。

    :func:`save_base64_data_url` 会在用户原始文件名前加 12 位随机十六进制
    前缀保证唯一性——那串东西对用户和模型都是噪音。WebUI 的附件标签
    （``webui/media_api.py``）与注入给模型的文档抬头
    （``utils/document.py::_doc_label``）都该显示 ``学生备注表.xlsx`` 而不是
    ``b92da4cc4d26_学生备注表.xlsx``。

    **只用于展示**：会话语义里的路径、工具读文件用的路径都是真实路径，
    剥前缀会指向不存在的文件。没有前缀（旧附件、纯 uuid 命名、模型自建的
    文件）时原样返回，所以对任意文件名都是安全的。
    """
    return _GENERATED_NAME_RE.sub("", filename or "")


def _client_slug(name: str | None) -> str:
    """取出客户端文件名中不含扩展名的可读部分，用作落盘名的后缀。

    只服务**可读性**，不承担安全职责：``safe_filename`` 会剥掉目录成分并把
    不安全字符替换成 ``_``，长度也在此截断，因此恶意文件名既无法逃出
    ``media_dir``，也顶多污染自己那个文件名的后半段（前半段始终是随机
    uuid，同名文件不会互相覆盖）。返回空串表示没有可用的可读部分，
    调用方应退回纯 uuid 命名。
    """
    if not name:
        return ""
    return safe_filename(Path(name).stem)[:_MAX_SLUG_CHARS].strip("._ ")


def save_base64_data_url(
    data_url: str,
    media_dir: Path,
    *,
    max_bytes: int | None = None,
    name: str | None = None,
) -> str | None:
    """Decode a ``data:<mime>;base64,<payload>`` URL and persist it.

    Returns the absolute path on success, ``None`` when the URL shape or the
    base64 payload itself is malformed. Raises :class:`FileSizeExceeded`
    when the decoded payload is larger than ``max_bytes`` (default 10 MB) —
    the check happens on the base64 length *before* decoding.

    ``name`` is the client-supplied filename. Its extension is used when
    whitelisted (see :data:`_CLIENT_EXT_ALLOWED`) and its readable stem is
    appended to the on-disk name after a random uuid prefix, so downstream
    consumers (the document label handed to the model, the attachment name in
    the WebUI) show something a human recognises instead of ``b92da4cc4d26``.
    The name is passed through :func:`~xianaibot.utils.helpers.safe_filename`
    and truncated, so a hostile name can never escape ``media_dir`` nor
    collide with another upload.
    """
    m = _DATA_URL_RE.match(data_url)
    if not m:
        return None
    mime_type, b64_payload = m.group(1).strip().lower(), m.group(2)
    limit = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    # 预检留 3 字节余量（base64 padding 取整误差），避免恰好等于上限的文件
    # 被估算值误杀；精确判定交给下面解码后的 len(raw) 比较。
    if estimate_decoded_size(b64_payload) > limit + 3:
        raise FileSizeExceeded(f"File exceeds {limit // (1024 * 1024)}MB limit")
    try:
        raw = base64.b64decode(b64_payload)
    except Exception:
        logger.debug("base64 decode failed for data URL", exc_info=True)
        return None
    if len(raw) > limit:
        raise FileSizeExceeded(f"File exceeds {limit // (1024 * 1024)}MB limit")
    ext = _resolve_extension(name, mime_type)
    # 落盘名 = 随机前缀 + 可读 slug + 扩展名。前缀保证唯一（同名文件不互相
    # 覆盖），slug 让模型与 WebUI 都看得见用户的原始文件名——否则附件在对话
    # 里显示成 ``b92da4cc4d26.xlsx``，模型会怀疑注入的摘要不完整，自己再解析
    # 一遍原文件（实测代价：一次问答多 70 秒与 6 次失败的工具调用）。
    prefix = uuid.uuid4().hex[:12]
    slug = _client_slug(name)
    filename = f"{prefix}_{slug}{ext}" if slug else f"{prefix}{ext}"
    dest = media_dir / safe_filename(filename)
    dest.write_bytes(raw)
    return str(dest)
