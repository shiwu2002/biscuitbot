"""WebUI 知识库上传 envelope 处理。

WebSocket 渠道负责传输与鉴权（复用 ``transcribe_audio`` 的 request/response
模式）；本模块负责把通过 socket 传来的文件落盘到 ``workspace/knowledge/``，
图片用视觉模型生成描述，并写入 FTS 索引。
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

from biscuitbot.config.loader import load_config
from biscuitbot.utils.helpers import safe_filename
from biscuitbot.utils.knowledge_index import (
    caption_image,
    index_document,
    knowledge_dir,
)

_MAX_REQUEST_ID_LENGTH = 80
_MAX_FILENAME_LENGTH = 255
_MAX_KNOWLEDGE_BYTES = 20 * 1024 * 1024  # 20 MB

_DATA_URL_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)*;base64,(.+)$", re.DOTALL)


def _decode_data_url(data_url: str) -> tuple[bytes | None, str | None]:
    """解析 ``data:<mime>;base64,<payload>``，返回 ``(raw, mime)``。"""
    match = _DATA_URL_RE.match(data_url)
    if not match:
        return None, None
    mime = match.group(1).strip().lower()
    try:
        raw = base64.b64decode(match.group(2))
    except Exception:
        return None, None
    return raw, mime


def _unique_path(kdir: Path, filename: str) -> Path:
    """避免重名覆盖：同名文件追加 `` (1)``、`` (2)`` 后缀。"""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = kdir / filename
    index = 1
    while candidate.exists():
        candidate = kdir / f"{stem} ({index}){suffix}"
        index += 1
    return candidate


async def webui_knowledge_upload_event(
    envelope: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """处理一次 WebUI 知识库上传请求，返回 WS 事件名与载荷。"""
    request_id = envelope.get("request_id")
    valid_request_id = (
        isinstance(request_id, str) and 0 < len(request_id) <= _MAX_REQUEST_ID_LENGTH
    )

    def error(detail: str, **extra: Any) -> tuple[str, dict[str, Any]]:
        payload: dict[str, Any] = {"detail": detail, **extra}
        if valid_request_id:
            payload["request_id"] = request_id
        return "knowledge_upload_error", payload

    if not valid_request_id:
        return error("invalid_request")

    filename = envelope.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return error("missing_filename")
    filename = safe_filename(filename.strip())[:_MAX_FILENAME_LENGTH]
    if not filename:
        return error("invalid_filename")

    data_url = envelope.get("data_url")
    if not isinstance(data_url, str) or not data_url:
        return error("missing_data")

    raw, mime = _decode_data_url(data_url)
    if raw is None:
        return error("invalid_data_url")
    if len(raw) > _MAX_KNOWLEDGE_BYTES:
        return error("size")

    config = load_config()
    kdir = knowledge_dir(config.workspace_path)
    kdir.mkdir(parents=True, exist_ok=True)
    path = _unique_path(kdir, filename)

    try:
        path.write_bytes(raw)
    except OSError:
        return error("write_failed")

    caption = None
    if mime and mime.startswith("image/"):
        caption = await caption_image(config, path)

    doc = await asyncio.to_thread(
        index_document, config.workspace_path, path.name, caption=caption
    )
    return "knowledge_upload_result", {"request_id": request_id, "doc": doc}
