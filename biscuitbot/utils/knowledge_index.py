"""知识库全文索引：上传时建索引，检索时用 CJK 二元分词匹配。

设计要点
========
- 文档在**上传时**抽取文本（``extract_text``）建索引；图片由云端多模态
  模型生成一句中文描述（``caption_image``），描述文本入索引。
- 中文用**二元分词（2 字滑动窗口）**切词后再交给 FTS5 的 ``unicode61``
  分词器——因为 trigram 分词器无法匹配最常见的中文两字查询（如「违约」），
  而二元分词把「违约金」切成 ``违约 约金``，两字查询即可命中。
- 索引存放在 ``workspace/knowledge_index.db``（知识库目录之外），避免
  智能体的 ``list_dir`` / ``grep`` 扫描到二进制索引文件。
"""

from __future__ import annotations

import base64
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from biscuitbot.utils.document import extract_text, is_image_file
from biscuitbot.utils.helpers import detect_image_mime, safe_filename

KNOWLEDGE_DIR_NAME = "knowledge"
_INDEX_DB_NAME = "knowledge_index.db"

# CJK 统一表意文字区段（含扩展 A / 兼容表意文字）。
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")

_CAPTION_PROMPT = (
    "请用一两句中文客观描述这张图片的内容，作为检索索引。"
    "例如：「一家人在海边合影」。只输出描述，不要多余说明。"
)


def _bigrams(text: str) -> str:
    """把文本切成 FTS5 可直接索引的 token 串。

    CJK 连续段切成 2 字重叠元并空格分隔（「违约金」→ ``违约 约金``），
    其余（ASCII/数字等）原样保留，交由 ``unicode61`` 按词切分。
    """
    parts: list[str] = []
    pos = 0
    for match in _CJK_RUN_RE.finditer(text):
        if match.start() > pos:
            parts.append(text[pos : match.start()])
        run = match.group()
        if len(run) == 1:
            parts.append(run)
        else:
            parts.append(" ".join(run[i : i + 2] for i in range(len(run) - 1)))
        pos = match.end()
    if pos < len(text):
        parts.append(text[pos:])
    return " ".join(parts)


def knowledge_dir(workspace: Path) -> Path:
    """知识库文件目录（上传的原始文件存放于此）。"""
    return Path(workspace) / KNOWLEDGE_DIR_NAME


def _index_db_path(workspace: Path) -> Path:
    return Path(workspace) / _INDEX_DB_NAME


def _connect(workspace: Path) -> sqlite3.Connection:
    db_path = _index_db_path(workspace)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
        "doc_id UNINDEXED,"
        "title,"
        "content,"
        "kind UNINDEXED,"
        "caption UNINDEXED,"
        "raw_content UNINDEXED,"
        "size UNINDEXED,"
        "indexed_at UNINDEXED,"
        "tokenize = 'unicode61')"
    )
    return conn


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _preview(text: str, max_chars: int = 160) -> str:
    """生成单行预览，供检索结果展示。"""
    text = re.sub(r"\s+", " ", (text or "").replace("\x00", " ")).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def index_document(
    workspace: Path, filename: str, *, caption: str | None = None
) -> dict[str, Any]:
    """为已落盘的文件抽取文本（或用描述）并写入 FTS 索引。

    图片：以 ``caption``（或文件名）作为检索文本；文档：以 ``extract_text``
    抽取的正文作为检索文本。
    """
    kdir = knowledge_dir(workspace)
    path = kdir / filename
    kind = "image" if is_image_file(str(path)) else "document"

    if kind == "image":
        text = caption or filename
        raw = caption or ""
    else:
        extracted = extract_text(path)
        if extracted is None or extracted.startswith(("[error:", "[image:")):
            extracted = filename  # 无法抽取时退回文件名，保证可被检索到
        text = extracted
        raw = "" if extracted == filename else extracted

    conn = _connect(workspace)
    try:
        with conn:
            conn.execute("DELETE FROM docs WHERE doc_id = ?", (filename,))
            conn.execute(
                "INSERT INTO docs("
                "doc_id, title, content, kind, caption, raw_content, size, indexed_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    filename,
                    _bigrams(filename),
                    _bigrams(text),
                    kind,
                    caption or "",
                    raw,
                    path.stat().st_size if path.exists() else 0,
                    _now(),
                ),
            )
    finally:
        conn.close()

    return {"doc_id": filename, "kind": kind, "caption": caption or ""}


def search_knowledge(
    workspace: Path, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """按查询检索知识库，返回按相关度排序的结果（含预览）。"""
    query = (query or "").strip()
    if not query:
        return []

    tokens = [t for t in _bigrams(query).split() if t.strip()]
    if not tokens:
        return []
    # 每个 token 加引号，避免 FTS5 把查询中的特殊字符当作语法。
    match_expr = " AND ".join(f'"{t.replace(chr(34), "")}"' for t in tokens)

    conn = _connect(workspace)
    try:
        rows = conn.execute(
            "SELECT doc_id, kind, caption, raw_content, size, indexed_at "
            "FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
            (match_expr, max(1, min(limit, 50))),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "doc_id": row["doc_id"],
            "kind": row["kind"],
            "caption": row["caption"] or "",
            "preview": _preview(row["raw_content"] or row["caption"] or ""),
            "size": _int(row["size"]),
            "indexed_at": row["indexed_at"],
        }
        for row in rows
    ]


def list_documents(workspace: Path) -> list[dict[str, Any]]:
    """列出知识库中的文档（仅保留磁盘上仍存在的文件）。"""
    kdir = knowledge_dir(workspace)
    conn = _connect(workspace)
    try:
        rows = conn.execute(
            "SELECT doc_id, kind, caption, size, indexed_at "
            "FROM docs ORDER BY indexed_at DESC"
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "doc_id": row["doc_id"],
            "kind": row["kind"],
            "caption": row["caption"] or "",
            "size": _int(row["size"]),
            "indexed_at": row["indexed_at"],
        }
        for row in rows
        if (kdir / row["doc_id"]).is_file()
    ]


def delete_document(workspace: Path, filename: str) -> bool:
    """删除知识库文件及其索引条目。返回是否确实删除了磁盘文件。"""
    safe = safe_filename(filename)
    path = knowledge_dir(workspace) / safe
    removed = path.is_file()
    if removed:
        path.unlink()

    conn = _connect(workspace)
    try:
        with conn:
            conn.execute("DELETE FROM docs WHERE doc_id = ?", (safe,))
    finally:
        conn.close()
    return removed


async def caption_image(config: Any, image_path: Path) -> str | None:
    """用配置的多模态模型（缺失时回退主模型）为图片生成一句中文描述。

    返回描述文本；无可用视觉模型或调用失败时返回 ``None``。
    """
    from biscuitbot.providers.factory import build_vision_provider

    provider = build_vision_provider(config)
    if provider is None:
        return None

    raw = image_path.read_bytes()
    mime = detect_image_mime(raw) or "image/jpeg"
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": _CAPTION_PROMPT},
            ],
        }
    ]
    try:
        response = await provider.chat_with_retry(
            messages=messages, tools=None, tool_choice=None
        )
    except Exception:
        return None

    content = (response.content or "").strip() if response else ""
    return content or None
