"""资产列表/删除 helpers for the WebUI HTTP and message surfaces.

「资产」= 智能体生成的图片/视频/音频/文档，与用户主动上传的知识库文档分开：

- 生成图片 ``media/generated/YYYY-MM-DD/img_<12hex>.png`` + sidecar ``.json``
- 生成视频 ``media/generated_video/YYYY-MM-DD/vid_<12hex>.mp4`` + sidecar ``.json``
- 生成文档 ``media/api/*_report.docx`` 等（外部 API 服务器产出，无 sidecar）
- TTS 音频 ``<workspace>/generated/tts/<date>/tts_<12hex>.mp3``（无 sidecar）

图片/视频/文档都在 media 根内，直接用 ``sign_media`` 签名；TTS 在 media 根外，先
**稳定 staging** 到 ``media/websocket/``（按源路径 sha256 定名，幂等）再签名，
避免 ``sign_or_stage_media_path`` 每次生成新 uuid 文件名导致列表无限复制。

``media/websocket`` 是 staging 区，不属于生成资产，不在扫描范围内。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from biscuitbot.config.loader import load_config
from biscuitbot.config.paths import get_media_dir
from biscuitbot.utils.helpers import safe_filename
from biscuitbot.webui.http_utils import query_first
from biscuitbot.webui.settings_api import WebUISettingsError

QueryParams = dict[str, list[str]]

# 资产 id 来自文件名 stem，本身不含路径分隔符；真实防线是 delete_asset 里的
# ``_safe_unlink`` 的 ``resolve().relative_to(root)``。这里只约束字符集与长度。
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# media 根下要纳入资产列表的子目录 → (kind, 扩展名集合)。
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".ogg"})
_DOCUMENT_EXTS = frozenset({".docx", ".pdf", ".md", ".txt", ".xlsx", ".csv", ".pptx"})
_MEDIA_ASSET_DIRS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("generated", "image", _IMAGE_EXTS),
    ("generated_video", "video", _VIDEO_EXTS),
    ("api", "document", _DOCUMENT_EXTS),
)

_TTS_REL_DIR = Path("generated") / "tts"

SignedMediaPath = Callable[[Path], str | None]


def _iso_from_mtime(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return ""


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load_sidecar(sidecar: Path) -> dict[str, Any]:
    """读取 sidecar JSON；损坏/缺失时返回空 dict。"""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _asset_item(
    asset_id: str,
    name: str,
    kind: str,
    size: int,
    created_at: str,
    caption: str,
    media_url: str,
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "name": name,
        "kind": kind,
        "size": size,
        "created_at": created_at,
        "caption": caption,
        "media_url": media_url,
    }


def _stable_stage_tts(path: Path) -> Path:
    """把 media 根外的 TTS 文件复制到 ``media/websocket`` 供签名（幂等）。

    副本名由源路径 sha256 前缀 + 原文件名组成，同一源文件始终指向同一副本，
    列表重复调用不会产生额外复制。
    """
    target_dir = get_media_dir("websocket")
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    staged = target_dir / f"tts-{digest}-{safe_filename(path.name) or 'audio'}"
    if not staged.is_file():
        shutil.copyfile(path, staged)
    return staged


def _scan_media_assets(
    media_root: Path,
    subdir: str,
    kind: str,
    exts: frozenset[str],
    sign_media: SignedMediaPath | None,
) -> list[dict[str, Any]]:
    root = media_root / subdir
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        sidecar = path.with_suffix(".json")
        meta = _load_sidecar(sidecar) if sidecar.is_file() else {}
        created = meta.get("created_at")
        if not isinstance(created, str) or not created:
            created = _iso_from_mtime(path)
        caption = meta.get("prompt")
        if not isinstance(caption, str):
            caption = ""
        media_url = sign_media(path) if sign_media is not None else None
        out.append(
            _asset_item(
                path.stem,
                path.name,
                kind,
                _file_size(path),
                created,
                caption,
                media_url or "",
            )
        )
    return out


def _scan_tts_assets(
    workspace: Path,
    sign_media: SignedMediaPath | None,
) -> list[dict[str, Any]]:
    root = workspace / _TTS_REL_DIR
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTS:
            continue
        staged = _stable_stage_tts(path)
        media_url = sign_media(staged) if sign_media is not None else None
        out.append(
            _asset_item(
                path.stem,
                path.name,
                "audio",
                _file_size(path),
                _iso_from_mtime(path),
                "",
                media_url or "",
            )
        )
    return out


def assets_payload(sign_media: SignedMediaPath | None = None) -> dict[str, Any]:
    """列出所有生成资产，按 created_at 降序。"""
    config = load_config()
    workspace = config.workspace_path
    media_root = get_media_dir()
    items: list[dict[str, Any]] = []
    for subdir, kind, exts in _MEDIA_ASSET_DIRS:
        items.extend(_scan_media_assets(media_root, subdir, kind, exts, sign_media))
    items.extend(_scan_tts_assets(workspace, sign_media))
    items.sort(key=lambda item: (item["created_at"], item["name"]), reverse=True)
    return {"assets": items}


def _safe_unlink(path: Path, root: Path) -> bool:
    """删除 ``root`` 内的文件；resolve 后必须仍位于 ``root`` 内（防穿越）。"""
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


_PREVIEW_MAX_CHARS = 200_000


def document_preview(query: QueryParams) -> dict[str, Any]:
    """返回文档资产的文本预览。

    docx/pdf/xlsx/pptx/txt 经 ``biscuitbot.utils.document.extract_text`` 提取
    为纯文本（浏览器无法原生渲染 docx，这里给一个可读的文本预览）。
    """
    asset_id = (query_first(query, "id") or "").strip()
    if not _ASSET_ID_RE.fullmatch(asset_id):
        raise WebUISettingsError("invalid asset id")
    media_root = get_media_dir()
    root = media_root / "api"
    if not root.is_dir():
        raise WebUISettingsError("asset not found")
    target: Path | None = None
    for path in root.rglob(f"{asset_id}.*"):
        if path.is_file() and path.suffix.lower() in _DOCUMENT_EXTS:
            target = path
            break
    if target is None:
        raise WebUISettingsError("asset not found")

    from biscuitbot.utils.document import extract_text

    text = extract_text(target) or ""
    truncated = len(text) > _PREVIEW_MAX_CHARS
    if truncated:
        text = text[:_PREVIEW_MAX_CHARS]
    return {
        "id": asset_id,
        "name": target.name,
        "kind": "document",
        "size": _file_size(target),
        "content": text,
        "truncated": truncated,
    }


def _remove_staged_tts(asset_id: str) -> None:
    """按稳定 staging 命名规则删除 ``media/websocket`` 中的 TTS 副本。"""
    target_dir = get_media_dir("websocket")
    if not target_dir.is_dir():
        return
    for staged in target_dir.glob(f"tts-*-{asset_id}.*"):
        try:
            staged.unlink()
        except OSError:
            pass


def delete_asset(
    query: QueryParams,
    sign_media: SignedMediaPath | None = None,
) -> dict[str, Any]:
    """按 id 删除一个生成资产，返回更新后的资产列表。

    id 不区分前缀，在所有 media 根子目录（图片/视频/文档）逐个查找删除，
    同时清理同目录 sidecar ``.json``；``tts_*`` 删源文件 + staging 副本。
    """
    asset_id = (query_first(query, "id") or "").strip()
    if not _ASSET_ID_RE.fullmatch(asset_id):
        raise WebUISettingsError("invalid asset id")

    config = load_config()
    workspace = config.workspace_path
    media_root = get_media_dir()

    # media 根内的生成资产：图片/视频/文档 + sidecar
    for subdir, _, exts in _MEDIA_ASSET_DIRS:
        root = media_root / subdir
        if not root.is_dir():
            continue
        for path in root.rglob(f"{asset_id}.*"):
            if not path.is_file():
                continue
            if path.suffix.lower() in exts or path.suffix.lower() == ".json":
                _safe_unlink(path, root)

    # TTS 音频：源文件 + staging 副本
    root = workspace / _TTS_REL_DIR
    if root.is_dir():
        for path in root.rglob(f"{asset_id}.*"):
            if path.is_file() and path.suffix.lower() in _AUDIO_EXTS:
                _safe_unlink(path, root)
    _remove_staged_tts(asset_id)
    return assets_payload(sign_media)
