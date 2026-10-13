"""会话台账（session_deliveries）测试。

台账是**只读派生**的：从 WebUI 会话记录里挑出出站消息的 ``media``（智能体交付给
用户的文件）与 ``file_edit`` 的 ``absolute_path``（写过的文件），不新增任何持久化
状态。这里覆盖目录/分段发现、入站排除、去重与解析。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xianaibot.session.manager import SessionManager
from xianaibot.webui.session_deliveries import iter_session_paths

KEY = "websocket:chat-1"


def _delivered(key: str, workspace: Path) -> list[Path]:
    return iter_session_paths(key, workspace=workspace).delivered


def _written(key: str, workspace: Path) -> list[Path]:
    return iter_session_paths(key, workspace=workspace).written


@pytest.fixture()
def webui_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "webui"
    directory.mkdir()
    monkeypatch.setattr("xianaibot.webui.transcript.get_webui_dir", lambda: directory)
    return directory


def _transcript_path(webui_dir: Path, key: str = KEY) -> Path:
    return webui_dir / f"{SessionManager.safe_key(key)}.jsonl"


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _outbound(*media: str) -> dict:
    return {"event": "message", "chat_id": "chat-1", "text": "给你", "media": list(media)}


def test_collects_outbound_media_and_dedups(webui_dir: Path, tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    first, second = ws / "a.png", ws / "b.html"
    _write_rows(
        _transcript_path(webui_dir),
        [
            {"event": "user", "chat_id": "chat-1", "text": "hi"},
            _outbound(str(first), str(second)),
            {"event": "delta", "chat_id": "chat-1", "text": "……"},
            _outbound(str(first)),  # 同一文件再次交付 → 去重
        ],
    )

    assert _delivered(KEY, ws) == [first, second]


def test_ignores_user_uploads_and_http_links(webui_dir: Path, tmp_path: Path) -> None:
    """入站 ``media_paths``（用户上传）不算交付；``http(s)`` 直链不是本地文件。"""
    ws = tmp_path / "workspace"
    local = ws / "out.mp4"
    _write_rows(
        _transcript_path(webui_dir),
        [
            {"event": "user", "chat_id": "chat-1", "text": "看图", "media_paths": [str(ws / "up.png")]},
            _outbound("https://cdn.example/legacy.mp4"),
            _outbound(str(local)),
        ],
    )

    assert _delivered(KEY, ws) == [local]


def test_resolves_relative_and_normalizes_paths(webui_dir: Path, tmp_path: Path) -> None:
    """相对路径按工作区解析；``..`` 归一后与直接写的绝对路径算同一个文件。"""
    ws = tmp_path / "workspace"
    target = ws / "sub" / "报告.html"
    _write_rows(
        _transcript_path(webui_dir),
        [
            _outbound("sub/报告.html"),
            _outbound(str(ws / "sub" / ".." / "sub" / "报告.html")),
        ],
    )

    assert _delivered(KEY, ws) == [target]


def test_reads_rotated_segments(webui_dir: Path, tmp_path: Path) -> None:
    """记录会按大小分段轮转，已轮转的分段同样要读。"""
    ws = tmp_path / "workspace"
    old, new = ws / "old.png", ws / "new.png"
    stem = SessionManager.safe_key(KEY)
    _write_rows(
        webui_dir / f"{stem}.segments" / "seg-0001.jsonl",
        [_outbound(str(old))],
    )
    _write_rows(_transcript_path(webui_dir), [_outbound(str(new))])

    assert sorted(_delivered(KEY, ws)) == sorted([old, new])


def test_missing_transcript_is_empty(tmp_path: Path, webui_dir: Path) -> None:
    assert _delivered("websocket:nope", tmp_path / "ws") == []


def test_cap_keeps_the_newest_deliveries(
    webui_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """触顶时丢开头而不是结尾：记录追加写，最新的交付必须留下。"""
    ws = tmp_path / "workspace"
    monkeypatch.setattr(
        "xianaibot.webui.session_deliveries._MAX_DELIVERED",
        3,
    )
    rows = [_outbound(str(ws / f"f{index}.png")) for index in range(5)]
    _write_rows(_transcript_path(webui_dir), rows)

    delivered = _delivered(KEY, ws)

    assert delivered == [ws / "f2.png", ws / "f3.png", ws / "f4.png"]


def test_tolerates_broken_lines(webui_dir: Path, tmp_path: Path) -> None:
    """坏行不能拖垮整份台账：跳过它，其余照收。"""
    ws = tmp_path / "workspace"
    good = ws / "good.png"
    path = _transcript_path(webui_dir)
    _write_rows(path, [_outbound(str(good))])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"media": [truncated\n')
        handle.write('{"event": "message", "media": "not-a-list"}\n')
        handle.write('{"event": "message", "media": [1, null, ""]}\n')

    assert _delivered(KEY, ws) == [good]


# ── 写过的文件（file_edit） ───────────────────────────────────────────


def _edit(*paths: str, relative_only: bool = False) -> dict:
    edits: list[dict] = []
    for path in paths:
        if relative_only:
            edits.append({"path": path})
        else:
            edits.append({"path": Path(path).name, "absolute_path": path})
    return {"event": "file_edit", "chat_id": "chat-1", "edits": edits}


def test_collects_written_files_from_file_edit_rows(webui_dir: Path, tmp_path: Path) -> None:
    """``write_file``/``apply_patch`` 落盘时记录下来的路径 = 这个会话写过什么。"""
    ws = tmp_path / "workspace"
    report = ws / "mem-report" / "index.html"
    _write_rows(
        _transcript_path(webui_dir),
        [
            _edit(str(report)),
            _edit(str(report)),  # 同一个文件多次编辑 → 去重
            _edit(str(ws / "mem-report" / "parse.py")),
        ],
    )

    assert _written(KEY, ws) == [report, ws / "mem-report" / "parse.py"]
    # 交付与写过是两条独立的台账，互不串味。
    assert _delivered(KEY, ws) == []


def test_written_paths_fall_back_to_relative_path(webui_dir: Path, tmp_path: Path) -> None:
    """老记录/缺字段时按工作区解析 ``path``（相对写法）。"""
    ws = tmp_path / "workspace"
    _write_rows(_transcript_path(webui_dir), [_edit("sub/报告.html", relative_only=True)])

    assert _written(KEY, ws) == [ws / "sub" / "报告.html"]


def test_delivered_and_written_rows_are_dispatched_by_event(
    webui_dir: Path, tmp_path: Path
) -> None:
    """两类行在同一趟扫描里分派：``file_edit`` 的 ``path`` 不当成交付，反之亦然。"""
    ws = tmp_path / "workspace"
    delivered = ws / "报告.docx"
    written = ws / "报告.html"
    _write_rows(
        _transcript_path(webui_dir),
        [_edit(str(written)), _outbound(str(delivered))],
    )

    assert _delivered(KEY, ws) == [delivered]
    assert _written(KEY, ws) == [written]


def test_written_cap_keeps_the_newest(webui_dir: Path, tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "workspace"
    monkeypatch.setattr("xianaibot.webui.session_deliveries._MAX_WRITTEN", 2)
    _write_rows(
        _transcript_path(webui_dir),
        [_edit(str(ws / f"f{index}.html")) for index in range(4)],
    )

    assert _written(KEY, ws) == [ws / "f2.html", ws / "f3.html"]
