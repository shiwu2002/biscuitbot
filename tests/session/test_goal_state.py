"""Tests for ``goal_state`` session metadata helpers."""

from __future__ import annotations

from biscuitbot.session.goal_state import (
    GOAL_STATE_KEY,
    _MAX_TASKS,
    _MAX_TASKS_IN_RUNTIME,
    _TASK_STATUSES,
    _next_task_id,
    _normalize_tasks,
    discard_legacy_goal_state_key,
    goal_state_runtime_lines,
    goal_state_ws_blob,
    parse_goal_state,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from biscuitbot.session.manager import SessionManager


def test_runtime_lines_empty_when_no_metadata():
    assert goal_state_runtime_lines(None) == []
    assert goal_state_runtime_lines({}) == []


def test_runtime_lines_empty_when_completed():
    meta = {
        GOAL_STATE_KEY: {"status": "completed", "objective": "was doing X"},
    }
    assert goal_state_runtime_lines(meta) == []


def test_runtime_lines_include_objective_when_active():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Ship the fix.",
            "ui_summary": "fix",
        },
    }
    lines = goal_state_runtime_lines(meta)
    assert "Goal (active):" in lines
    assert "Ship the fix." in lines
    assert any("Summary: fix" in ln for ln in lines)


def test_runtime_lines_read_legacy_thread_goal_key():
    meta = {"thread_goal": {"status": "active", "objective": "Legacy key.", "ui_summary": "L"}}
    lines = goal_state_runtime_lines(meta)
    assert "Legacy key." in lines


def test_goal_state_key_takes_precedence_over_legacy():
    meta = {
        GOAL_STATE_KEY: {"status": "active", "objective": "New key wins.", "ui_summary": "n"},
        "thread_goal": {"status": "active", "objective": "Ignored.", "ui_summary": "o"},
    }
    lines = goal_state_runtime_lines(meta)
    assert "New key wins." in lines
    assert "Ignored." not in "".join(lines)


def test_discard_legacy_goal_state_key():
    meta: dict = {"thread_goal": {"x": 1}, GOAL_STATE_KEY: {"status": "active"}}
    discard_legacy_goal_state_key(meta)
    assert "thread_goal" not in meta
    assert GOAL_STATE_KEY in meta


def test_parse_goal_state_accepts_json_string():
    assert parse_goal_state('{"status":"active","objective":"x"}') == {
        "status": "active",
        "objective": "x",
    }


def test_goal_state_ws_blob_inactive_when_missing_or_completed():
    assert goal_state_ws_blob(None) == {"active": False}
    assert goal_state_ws_blob({}) == {"active": False}
    assert goal_state_ws_blob({GOAL_STATE_KEY: {"status": "completed", "objective": "x"}}) == {
        "active": False,
    }


def test_goal_state_ws_blob_active_shape():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Build feature.",
            "ui_summary": "feat",
        },
    }
    assert goal_state_ws_blob(meta) == {
        "active": True,
        "ui_summary": "feat",
        "objective": "Build feature.",
    }


def test_sustained_goal_active_false_when_missing_or_completed():
    assert sustained_goal_active(None) is False
    assert sustained_goal_active({}) is False
    assert sustained_goal_active({GOAL_STATE_KEY: {"status": "completed", "objective": "x"}}) is False


def test_sustained_goal_active_true_when_active():
    meta = {GOAL_STATE_KEY: {"status": "active", "objective": "Run long task."}}
    assert sustained_goal_active(meta) is True


def test_sustained_goal_active_respects_legacy_thread_goal_key():
    meta = {"thread_goal": {"status": "active", "objective": "Legacy."}}
    assert sustained_goal_active(meta) is True


def test_runner_wall_llm_timeout_uses_metadata_override(tmp_path):
    sm = SessionManager(tmp_path)
    assert (
        runner_wall_llm_timeout_s(
            sm,
            "cli:test",
            metadata={GOAL_STATE_KEY: {"status": "active", "objective": "x"}},
        )
        == 0.0
    )
    assert runner_wall_llm_timeout_s(sm, "cli:test", metadata={}) is None


def test_runner_wall_llm_timeout_reads_session_when_metadata_missing(tmp_path):
    sm = SessionManager(tmp_path)
    sess = sm.get_or_create("c:d")
    sess.metadata = {GOAL_STATE_KEY: {"status": "active", "objective": "z"}}
    assert runner_wall_llm_timeout_s(sm, "c:d") == 0.0
    sess.metadata = {}
    assert runner_wall_llm_timeout_s(sm, "c:d") is None


# ---------------------------------------------------------------------------
# Task checklist (structured subtasks)
# ---------------------------------------------------------------------------


def _active_with_tasks(tasks):
    return {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Ship.",
            "ui_summary": "s",
            "tasks": tasks,
        }
    }


def test_runtime_lines_render_task_checklist():
    meta = _active_with_tasks([
        {"id": "t1", "text": "Write tests", "status": "done"},
        {"id": "t2", "text": "Run CI", "status": "in_progress"},
        {"id": "t3", "text": "Deploy", "status": "pending"},
    ])
    lines = goal_state_runtime_lines(meta)
    assert "Tasks (1/3 done):" in lines
    assert "[x] (t1) Write tests" in lines
    assert "[~] (t2) Run CI" in lines
    assert "[ ] (t3) Deploy" in lines
    assert any("update_task" in ln for ln in lines)


def test_runtime_lines_omits_tasks_section_when_no_tasks():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Ship.",
            "ui_summary": "s",
        },
    }
    lines = goal_state_runtime_lines(meta)
    assert not any(ln.startswith("Tasks (") for ln in lines)
    assert not any("update_task" in ln for ln in lines)


def test_runtime_lines_caps_rendered_tasks_and_counts_remaining():
    tasks = [{"id": f"t{i}", "text": f"step {i}", "status": "pending"}
             for i in range(1, _MAX_TASKS_IN_RUNTIME + 6)]
    lines = goal_state_runtime_lines(_active_with_tasks(tasks))
    assert f"… {len(tasks) - _MAX_TASKS_IN_RUNTIME} more tasks" in lines


def test_runtime_lines_keeps_placeholder_when_only_tasks_no_objective():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "tasks": [{"id": "t1", "text": "first", "status": "pending"}],
        },
    }
    lines = goal_state_runtime_lines(meta)
    assert "Goal (active):" in lines
    assert "(no objective text stored)" in lines
    assert "Tasks (0/1 done):" in lines


def test_ws_blob_includes_tasks_when_present():
    meta = _active_with_tasks([
        {"id": "t1", "text": "API", "status": "done"},
        {"id": "t2", "text": "Docs", "status": "pending"},
    ])
    assert goal_state_ws_blob(meta) == {
        "active": True,
        "ui_summary": "s",
        "objective": "Ship.",
        "tasks": [
            {"id": "t1", "text": "API", "status": "done"},
            {"id": "t2", "text": "Docs", "status": "pending"},
        ],
    }


def test_ws_blob_omits_tasks_key_when_none():
    meta = {GOAL_STATE_KEY: {"status": "active", "objective": "Ship."}}
    assert goal_state_ws_blob(meta) == {"active": True, "objective": "Ship."}


def test_normalize_tasks_returns_empty_for_non_list():
    assert _normalize_tasks({"tasks": "nope"}) == []
    assert _normalize_tasks({"tasks": None}) == []
    assert _normalize_tasks({}) == []
    assert _normalize_tasks(None) == []
    assert _normalize_tasks({"tasks": [{}]}) == []


def test_normalize_tasks_cleans_entries():
    raw = [
        "not a dict",
        {"id": "t1", "text": "  Keep me  ", "status": "done"},
        {"id": "t2", "text": "", "status": "done"},              # empty text -> dropped
        {"id": "t3", "text": "bogus status", "status": "bogus"},  # -> pending
        {"id": "t1", "text": "dup id", "status": "pending"},      # duplicate id preserved
    ]
    tasks = _normalize_tasks({"tasks": raw})
    assert tasks == [
        {"id": "t1", "text": "Keep me", "status": "done"},
        {"id": "t3", "text": "bogus status", "status": "pending"},
        {"id": "t1", "text": "dup id", "status": "pending"},
    ]


def test_normalize_tasks_backfills_missing_ids():
    raw = [
        {"text": "first", "status": "pending"},
        {"text": "second", "status": "done"},
    ]
    tasks = _normalize_tasks({"tasks": raw})
    assert [t["id"] for t in tasks] == ["t1", "t2"]
    assert tasks[1]["status"] == "done"


def test_next_task_id_ignores_non_numeric_ids():
    tasks = [{"id": "t1", "text": "a"}, {"id": "t3", "text": "b"}]
    assert _next_task_id(tasks) == "t4"
    # ids without a numeric suffix do not advance the counter
    assert _next_task_id([{"id": "alpha", "text": "a"}]) == "t1"


def test_task_statuses_enumerated():
    assert _TASK_STATUSES == ("pending", "in_progress", "done")
    assert _MAX_TASKS > 0
