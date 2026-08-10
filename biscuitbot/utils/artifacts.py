"""生成媒体工件持久化助手。

所属模块与项目作用
===================
本文件位于 biscuitbot/utils 目录，是工具函数模块的产物存储组件。
在项目架构中起到的作用：
负责把模型生成的图片（base64 data URL 形式）安全地解码、落盘，
并在媒体根目录下按日期归档，同时写入 JSON 元数据 sidecar，
供后续引用、回放与审计使用。
"""

from __future__ import annotations

import base64  # 解码 base64 图片负载
import binascii  # 捕获 base64 解码异常
import json  # 序列化工件元数据
import re  # 匹配 data URL 协议头
import uuid  # 生成唯一工件 ID
from datetime import datetime
from pathlib import Path, PurePosixPath  # 安全拼接相对路径
from typing import Any

from biscuitbot.config.paths import get_media_dir  # 获取媒体根目录
from biscuitbot.utils.helpers import detect_image_mime, ensure_dir  # MIME 嗅探与建目录

# 匹配 base64 图片 data URL，捕获声明 MIME 与编码内容
_DATA_IMAGE_RE = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.DOTALL)
# 支持的图片 MIME 到扩展名映射
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

class ArtifactError(ValueError):
    """当工件无法被安全解码或存储时抛出。"""


def decode_image_data_url(data_url: str) -> tuple[bytes, str]:
    """解码 base64 图片 data URL，返回 ``(原始字节, mime)``。"""
    match = _DATA_IMAGE_RE.match(data_url.strip())
    if match is None:
        raise ArtifactError("expected a base64 image data URL")

    declared_mime, encoded = match.groups()  # 声明 MIME 与编码负载
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ArtifactError("invalid base64 image payload") from exc

    detected_mime = detect_image_mime(raw)  # 用魔数校验真实 MIME
    if detected_mime is None:
        raise ArtifactError("unsupported or unrecognized image data")
    if declared_mime != detected_mime:
        # 声明 MIME 与实际不符时以实际为准，防止伪造
        declared_mime = detected_mime
    return raw, declared_mime


def _safe_relative_dir(save_dir: str) -> Path:
    """校验 save_dir 为安全的相对路径，防止路径穿越。"""
    normalized = save_dir.replace("\\", "/").strip("/")
    if not normalized:
        raise ArtifactError("save_dir must not be empty")
    rel = PurePosixPath(normalized)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ArtifactError("save_dir must be a safe relative path")
    return Path(*rel.parts)


def _artifact_root(save_dir: str) -> Path:
    """计算工件存储根目录，并校验未越出媒体根目录。"""
    media_root = get_media_dir().resolve()
    root = (media_root / _safe_relative_dir(save_dir)).resolve()
    try:
        root.relative_to(media_root)  # 确保结果仍在媒体根之下
    except ValueError as exc:
        raise ArtifactError("artifact directory escapes media root") from exc
    return root


def store_generated_image_artifact(
    data_url: str,
    *,
    prompt: str,
    model: str,
    source_images: list[str] | None = None,
    save_dir: str = "generated",
    provider: str = "openrouter",
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """持久化生成图片及其 sidecar 元数据到媒体根目录下。"""
    raw, mime = decode_image_data_url(data_url)
    ext = _MIME_EXTENSIONS.get(mime)
    if ext is None:
        raise ArtifactError(f"unsupported image MIME type: {mime}")

    now = created_at or datetime.now().astimezone()  # 缺省使用当前本地时区时间
    day_dir = ensure_dir(_artifact_root(save_dir) / now.strftime("%Y-%m-%d"))  # 按日期归档
    artifact_id = f"img_{uuid.uuid4().hex[:12]}"  # 生成唯一工件 ID
    image_path = day_dir / f"{artifact_id}{ext}"
    metadata_path = day_dir / f"{artifact_id}.json"

    image_path.write_bytes(raw)  # 落盘图片
    metadata: dict[str, Any] = {
        "id": artifact_id,
        "path": str(image_path),
        "mime": mime,
        "prompt": prompt,
        "model": model,
        "provider": provider,
        "source_images": list(source_images or []),
        "created_at": now.isoformat(),
    }
    metadata_path.write_text(  # 落盘 JSON 元数据
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata


def generated_image_tool_result(artifacts: list[dict[str, Any]]) -> str:
    """返回暴露给 LLM 的紧凑结构化工具结果字符串。"""
    return json.dumps(
        {
            "artifacts": artifacts,
            "next_step": (
                "Use these artifact paths as reference_images for follow-up edits. "
                "Call the message tool with the artifact paths in the media parameter "
                "to deliver the images to the user. Keep raw paths internal unless the "
                "user asks for debug details."
            ),
        },
        ensure_ascii=False,
    )
