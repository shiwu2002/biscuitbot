"""会话台账：从 WebUI 会话记录里取「本会话产生/交付的文件」。

资产列表（``assets_api``）原先只扫会话工作区，于是**会话**这个维度完全没有留痕：
两个会话只要用同一个工作区（默认工作区就是共用目录），看到的就是同一份清单 ——
文件系统里没有「会话」可依据。这里复用已有的会话记录当台账，不新增任何持久化状态：

- 出站消息的 ``media``（``channels/websocket.py`` 在发送时写入 transcript JSONL，
  与聊天里附件 chip 用的是同一份路径）＝ 智能体**交付**给用户的文件；
- ``file_edit`` 事件的 ``absolute_path``（``write_file`` / ``apply_patch`` 落盘时写入）
  ＝ 智能体**写**过的文件；
- 入站消息的 ``media_paths`` ＝ 用户自己上传的 —— **不算台账**，这里不读。

transcript 是 append-only 且按大小分段轮转（``<key>.segments/*.jsonl``），所以这里
流式扫一遍所有分片，一趟同时收两类。只有含 ``"media"`` / ``"file_edit"`` 的行才解析
JSON，其余整行跳过 —— 长会话（实测单会话 3.9 MB）也只为少量台账行付 json 解析代价。

台账是**只读派生**的：不复制文件、不写新状态，历史会话也能追溯。已知覆盖缺口：
``exec`` 里跑的脚本写出的文件（如用 python 生成 docx）不产生 ``file_edit``，只有被
交付过（进 ``media``）才收录。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from xianaibot.webui.transcript import (
    webui_transcript_path,
    webui_transcript_segments_dir,
)

# 只有这两类行会让一行进入 json 解析。``"media_paths"``（入站上传）不含 ``"media"``
# 子串，因此被精确排除。
_MEDIA_FIELD = '"media"'
_FILE_EDIT_FIELD = '"file_edit"'
_WATCHED_FIELDS = (_MEDIA_FIELD, _FILE_EDIT_FIELD)

# 病理会话的兜底：台账条数远小于工作区文件数，这里只是防止记录被写爆后拖慢列表。
# 触顶时保留**最新**的一批（见 iter_session_paths）。
_MAX_DELIVERED = 5_000
_MAX_WRITTEN = 5_000


class SessionPaths(NamedTuple):
    """本会话的两条台账：交付给用户的文件、智能体写过的文件。"""

    delivered: list[Path]
    written: list[Path]


def _chunk_paths(session_key: str) -> list[Path]:
    """会话记录的全部片段文件（已轮转的分段 + 活动文件）。"""
    paths: list[Path] = []
    segments = webui_transcript_segments_dir(session_key)
    if segments.is_dir():
        paths.extend(sorted(segments.glob("*.jsonl")))
    active = webui_transcript_path(session_key)
    if active.is_file():
        paths.append(active)
    return paths


def _iter_watched_rows(session_key: str) -> Iterator[dict]:
    """逐行扫会话记录，产出含台账字段的行（已解析，坏行跳过）。"""
    for path in _chunk_paths(session_key):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if not any(field in line for field in _WATCHED_FIELDS):
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError as exc:
            logger.warning("read session transcript failed {}: {}", path, exc)
            continue


def _media_of(row: dict) -> Iterator[str]:
    """出站消息里交付给用户的文件路径。"""
    media = row.get("media")
    if not isinstance(media, list):
        return
    for entry in media:
        if isinstance(entry, str) and entry:
            yield entry


def _writes_of(row: dict) -> Iterator[str]:
    """``file_edit`` 事件里被写过的文件路径。

    优先 ``absolute_path``：``path`` 是工作区相对写法，拿它当准可能把工作区外的文件
    错误地拉回工作区里解析。``path`` 只作为老记录/缺字段时的回落。
    """
    edits = row.get("edits")
    if not isinstance(edits, list):
        return
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        raw = edit.get("absolute_path") or edit.get("path")
        if isinstance(raw, str) and raw:
            yield raw


def _push(raw: str, workspace: Path, out: list[Path], seen: set[str]) -> None:
    """把一条记录路径按真实路径去重后收进 ``out``（``http(s)://`` 直链直接跳过）。"""
    if "://" in raw:
        return
    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else workspace / candidate
    try:
        key = os.path.normcase(os.path.realpath(path))
    except OSError:
        key = os.path.normcase(str(path))
    if key in seen:
        return
    seen.add(key)
    out.append(path)


def _capped(paths: list[Path], limit: int) -> list[Path]:
    """触顶时保留**最新**的一批。

    记录是追加写的（旧在前），所以在触顶处直接停止读取就会留下最旧的一批、丢掉刚
    产生的那批 —— 与 :func:`xianaibot.webui.assets_api._iter_workspace_asset_paths`
    的取舍一致：丢开头，不丢结尾。
    """
    return paths[-limit:] if len(paths) > limit else paths


def iter_session_paths(session_key: str, *, workspace: Path) -> SessionPaths:
    """该会话的台账：``delivered``（交付给用户的）与 ``written``（写过的）。

    一趟扫完会话记录（两类行各自成表，所以必须单遍分派），各自按真实路径去重、各自
    限量。已经不存在的路径不做过滤 —— 是否真的存在由资产列表判断。
    """
    delivered: list[Path] = []
    written: list[Path] = []
    seen_delivered: set[str] = set()
    seen_written: set[str] = set()
    for row in _iter_watched_rows(session_key):
        if row.get("event") == "file_edit":
            for raw in _writes_of(row):
                _push(raw, workspace, written, seen_written)
        else:
            for raw in _media_of(row):
                _push(raw, workspace, delivered, seen_delivered)
    return SessionPaths(
        delivered=_capped(delivered, _MAX_DELIVERED),
        written=_capped(written, _MAX_WRITTEN),
    )
