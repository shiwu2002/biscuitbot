"""WebUI chat fork orchestration."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from biscuitbot.session.manager import SessionManager
from biscuitbot.session.webui_turns import WEBUI_TITLE_METADATA_KEY, clean_generated_title
from biscuitbot.webui.transcript import (
    append_fork_marker,
    delete_webui_transcript,
    fork_transcript_before_user_index,
    read_transcript_lines,
    write_session_messages_as_transcript,
)

_WEBUI_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _valid_webui_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _WEBUI_CHAT_ID_RE.match(value) is not None


def _map_before_user_index_to_session(
    session_manager: SessionManager,
    source_key: str,
    before_user_index: int,
) -> int:
    """Map a transcript-global user index onto the agent session's retained prefix.

    The webui transcript is append-only and keeps the full display history,
    while the agent session can be truncated by AutoCompact/Dream to a recent
    suffix (old turns collapsed into ``_last_summary``). The client computes
    ``before_user_index`` from the transcript, so it can exceed the session's
    own user count and make ``fork_session_before_user_index`` bail out.

    Retained session user rows cover transcript user indexes ``[T-S, T)`` where
    ``T``/``S`` are the transcript/session user counts, so the number of retained
    rows that fall strictly before the fork point is ``min(B, T) - max(0, T-S)``.
    """
    transcript_users = sum(
        1 for rec in read_transcript_lines(source_key) if rec.get("event") == "user"
    )
    info = session_manager.read_session_file(source_key)
    if not info:
        return before_user_index
    session_users = sum(
        1 for msg in info.get("messages", []) if msg.get("role") == "user"
    )
    if session_users <= 0 or transcript_users == session_users:
        return before_user_index
    return max(0, min(before_user_index, transcript_users) - max(0, transcript_users - session_users))


def create_webui_chat_fork(
    session_manager: SessionManager,
    *,
    source_chat_id: str,
    before_user_index: int,
    title: str | None = None,
) -> tuple[str, str] | None:
    """Return ``(chat_id, session_key)`` for a new fork, or ``None`` for bad input."""
    new_id = str(uuid.uuid4())
    source_key = f"websocket:{source_chat_id}"
    target_key = f"websocket:{new_id}"
    try:
        # Map the transcript-global index onto the agent session's retained
        # messages: for compacted sessions the transcript keeps the full history
        # while the session holds only a recent suffix, so the raw index can be
        # out of range (aborting the fork) or land inside the suffix (silently
        # pulling in post-fork turns). The transcript-side fork below supplies
        # the complete display history regardless.
        session_index = _map_before_user_index_to_session(
            session_manager,
            source_key,
            before_user_index,
        )
        forked = session_manager.fork_session_before_user_index(
            source_key,
            target_key,
            session_index,
        )
        if forked is None:
            return None

        transcript_ok = fork_transcript_before_user_index(
            source_key,
            target_key,
            before_user_index,
        )
        if not transcript_ok:
            write_session_messages_as_transcript(target_key, forked.messages)
        append_fork_marker(target_key)

        fork_title = clean_generated_title(title)
        if fork_title:
            forked.metadata[WEBUI_TITLE_METADATA_KEY] = fork_title
            session_manager.save(forked, fsync=True)
    except Exception:
        delete_webui_transcript(target_key)
        session_manager.delete_session(target_key)
        raise
    return new_id, target_key


async def handle_webui_fork_chat(channel: Any, connection: Any, envelope: Mapping[str, Any]) -> None:
    """Handle the WebUI ``fork_chat`` websocket command.

    ``websocket.py`` owns the transport. This module owns WebUI fork semantics:
    validate the request, clone session/transcript state, attach the new chat,
    and hydrate the client.
    """
    source_chat_id = envelope.get("source_chat_id")
    raw_index = envelope.get("before_user_index")
    if not _valid_webui_chat_id(source_chat_id):
        await channel._send_event(connection, "error", detail="invalid source_chat_id")
        return
    if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
        await channel._send_event(connection, "error", detail="invalid before_user_index")
        return

    session_manager = channel.gateway.session_manager
    if session_manager is None:
        await channel._send_event(connection, "error", detail="session_manager_unavailable")
        return

    try:
        forked = create_webui_chat_fork(
            session_manager,
            source_chat_id=source_chat_id,
            before_user_index=raw_index,
            title=envelope.get("title") if isinstance(envelope.get("title"), str) else None,
        )
        if forked is None:
            await channel._send_event(connection, "error", detail="invalid fork source or index")
            return
        fork_id, fork_key = forked
    except Exception as exc:
        channel.logger.warning("fork_chat failed: {}", exc)
        await channel._send_event(connection, "error", detail="fork_chat_failed")
        return

    scope = channel._workspaces.scope_for_session_key(fork_key)
    channel._attach(connection, fork_id)
    await channel._send_event(connection, "attached", chat_id=fork_id)
    await channel._send_event(
        connection,
        "session_updated",
        chat_id=fork_id,
        scope="metadata",
        workspace_scope=scope.payload(),
    )
    await channel._hydrate_after_subscribe(fork_id)
