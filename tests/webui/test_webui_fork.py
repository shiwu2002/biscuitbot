"""WebUI fork regression: forking a session whose agent messages were compacted.

AutoCompact/Dream truncate the agent session to a recent suffix (old turns
collapsed into ``_last_summary``) while the append-only webui transcript keeps
the full display history. The client computes ``before_user_index`` from the
transcript, so for old sessions that index can exceed the session's own user
count and previously aborted the fork. These tests pin the fallback that maps
the transcript index onto the retained prefix.
"""

from __future__ import annotations

from pathlib import Path

import biscuitbot.webui.transcript as transcript_mod
from biscuitbot.session.manager import SessionManager
from biscuitbot.webui.forking import create_webui_chat_fork
from biscuitbot.webui.transcript import (
    fork_boundary_message_count,
    read_transcript_lines,
    webui_transcript_path,
)

# user -> assistant turn texts
TURNS = [
    ("u1", "a1"),
    ("u2", "a2"),
    ("u3", "a3"),
    ("u4", "a4"),
]

SOURCE_KEY = "websocket:src"


def _write_full_transcript(webui_dir: Path) -> None:
    path = webui_transcript_path(SOURCE_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for user_text, assistant_text in TURNS:
        rows.append(
            f'{{"event":"user","chat_id":"src","text":"{user_text}"}}'
        )
        rows.append(
            f'{{"event":"message","chat_id":"src","text":"{assistant_text}"}}'
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _make_compacted_source(tmp_path: Path) -> SessionManager:
    """Session keeps only the last two turns (compacted suffix); transcript is full."""
    manager = SessionManager(tmp_path)
    source = manager.get_or_create(SOURCE_KEY)
    source.metadata["_last_summary"] = "summarized earlier turns"
    # Retained suffix: u3/a3 + u4/a4
    for user_text, assistant_text in TURNS[-2:]:
        source.add_message("user", user_text)
        source.add_message("assistant", assistant_text)
    manager.save(source)
    return manager


def test_fork_last_reply_of_compacted_session(tmp_path: Path, monkeypatch) -> None:
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    monkeypatch.setattr(transcript_mod, "get_webui_dir", lambda: webui_dir)
    _write_full_transcript(webui_dir)
    manager = _make_compacted_source(tmp_path)

    result = create_webui_chat_fork(
        manager,
        source_chat_id="src",
        before_user_index=len(TURNS),  # fork the final assistant reply
        title="Fork title",
    )

    assert result is not None
    _, target_key = result

    # Agent session context = the full retained suffix (both users are pre-fork).
    saved = manager.read_session_file(target_key)
    assert saved is not None
    assert [m["content"] for m in saved["messages"]] == ["u3", "a3", "u4", "a4"]
    assert saved["metadata"].get("_last_summary") == "summarized earlier turns"

    # Display transcript = the complete 4-turn history, fork boundary after it.
    lines = read_transcript_lines(target_key)
    assert fork_boundary_message_count(lines) == 8


def test_fork_mid_history_of_compacted_session_maps_to_retained_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    monkeypatch.setattr(transcript_mod, "get_webui_dir", lambda: webui_dir)
    _write_full_transcript(webui_dir)
    manager = _make_compacted_source(tmp_path)

    # Fork the third reply (transcript user index 2 -> before_user_index=3).
    # The retained session suffix covers transcript users 2..3, so only user 2's
    # turn belongs to the pre-fork context.
    result = create_webui_chat_fork(
        manager,
        source_chat_id="src",
        before_user_index=3,
    )

    assert result is not None
    _, target_key = result

    saved = manager.read_session_file(target_key)
    assert saved is not None
    assert [m["content"] for m in saved["messages"]] == ["u3", "a3"]

    lines = read_transcript_lines(target_key)
    assert fork_boundary_message_count(lines) == 6


def test_fork_before_retained_history_leaves_empty_agent_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    monkeypatch.setattr(transcript_mod, "get_webui_dir", lambda: webui_dir)
    _write_full_transcript(webui_dir)
    manager = _make_compacted_source(tmp_path)

    # Fork the first reply (before_user_index=1): all retained messages are
    # post-fork, so the agent context is empty while the display keeps turn 1.
    result = create_webui_chat_fork(
        manager,
        source_chat_id="src",
        before_user_index=1,
    )

    assert result is not None
    _, target_key = result

    saved = manager.read_session_file(target_key)
    assert saved is not None
    assert [m["content"] for m in saved["messages"]] == []

    lines = read_transcript_lines(target_key)
    assert fork_boundary_message_count(lines) == 2
