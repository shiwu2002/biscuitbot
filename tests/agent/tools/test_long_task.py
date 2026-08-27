"""Tests for sustained goal tools (`long_task`, `complete_goal`)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from biscuitbot.agent.loop import AgentLoop
from biscuitbot.agent.tools.context import RequestContext
from biscuitbot.agent.tools.long_task import (
    CompleteGoalTool,
    LongTaskTool,
    UpdateTaskTool,
)
from biscuitbot.bus.queue import MessageBus
from biscuitbot.bus.runtime_events import RuntimeEventBus
from biscuitbot.session.goal_state import GOAL_STATE_KEY, _MAX_TASKS
from biscuitbot.session.manager import SessionManager
from biscuitbot.session.webui_turns import WebuiTurnCoordinator


def _tools(sm: SessionManager) -> tuple[LongTaskTool, CompleteGoalTool]:
    lt = LongTaskTool(sessions=sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    lt.set_context(rc)
    cg.set_context(rc)
    return lt, cg


@pytest.mark.asyncio
async def test_long_task_records_goal_metadata(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)

    out = await lt.execute(goal="Do the thing", ui_summary="thing")
    assert "Goal recorded" in out

    sess = sm.get_or_create("websocket:c1")
    blob = sess.metadata.get(GOAL_STATE_KEY)
    assert isinstance(blob, dict)
    assert blob["status"] == "active"
    assert blob["objective"] == "Do the thing"
    assert blob["ui_summary"] == "thing"


@pytest.mark.asyncio
async def test_long_task_rejects_second_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)

    await lt.execute(goal="First")
    out = await lt.execute(goal="Second")
    assert "already active" in out


@pytest.mark.asyncio
async def test_complete_goal_closes_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    lt, cg = _tools(sm)

    await lt.execute(goal="X")
    out = await cg.execute(recap="Done.")
    assert "marked complete" in out

    sess = sm.get_or_create("websocket:c1")
    blob = sess.metadata.get(GOAL_STATE_KEY)
    assert blob["status"] == "completed"
    assert blob["recap"] == "Done."


@pytest.mark.asyncio
async def test_goal_tools_keep_request_context_per_task(tmp_path):
    sm = SessionManager(tmp_path)
    lt = LongTaskTool(sessions=sm)
    cg = CompleteGoalTool(sessions=sm)
    ctx_a = RequestContext(channel="websocket", chat_id="a", session_key="websocket:a")
    ctx_b = RequestContext(channel="websocket", chat_id="b", session_key="websocket:b")

    lt.set_context(ctx_a)
    task_a = asyncio.create_task(lt.execute(goal="Goal A"))
    lt.set_context(ctx_b)
    task_b = asyncio.create_task(lt.execute(goal="Goal B"))
    await asyncio.gather(task_a, task_b)

    assert sm.get_or_create("websocket:a").metadata[GOAL_STATE_KEY]["objective"] == "Goal A"
    assert sm.get_or_create("websocket:b").metadata[GOAL_STATE_KEY]["objective"] == "Goal B"

    cg.set_context(ctx_a)
    done_a = asyncio.create_task(cg.execute(recap="Done A"))
    cg.set_context(ctx_b)
    done_b = asyncio.create_task(cg.execute(recap="Done B"))
    await asyncio.gather(done_a, done_b)

    assert sm.get_or_create("websocket:a").metadata[GOAL_STATE_KEY]["recap"] == "Done A"
    assert sm.get_or_create("websocket:b").metadata[GOAL_STATE_KEY]["recap"] == "Done B"


@pytest.mark.asyncio
async def test_goal_tools_context_isolated_across_tool_types(tmp_path):
    """LongTaskTool and CompleteGoalTool must not share routing context."""
    sm = SessionManager(tmp_path)
    lt = LongTaskTool(sessions=sm)
    cg = CompleteGoalTool(sessions=sm)
    ctx = RequestContext(channel="websocket", chat_id="a", session_key="websocket:a")

    lt.set_context(ctx)
    assert cg._request_ctx.get() is None

    cg.set_context(ctx)
    assert lt._request_ctx.get() is ctx
    assert cg._request_ctx.get() is ctx


@pytest.mark.asyncio
async def test_long_task_publishes_goal_state_ws_after_save(tmp_path):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    runtime_events = RuntimeEventBus()
    sm = SessionManager(tmp_path)
    WebuiTurnCoordinator(
        bus=bus,
        sessions=sm,
        schedule_background=lambda _coro: None,
    ).subscribe(runtime_events)
    lt = LongTaskTool(sessions=sm, runtime_events=runtime_events)
    rc = RequestContext(
        channel="websocket",
        chat_id="chat-99",
        session_key="websocket:chat-99",
        metadata={},
    )
    lt.set_context(rc)

    await lt.execute(goal="Objective alpha", ui_summary="alpha")

    bus.publish_outbound.assert_awaited_once()
    call = bus.publish_outbound.await_args.args[0]
    assert call.channel == "websocket"
    assert call.chat_id == "chat-99"
    assert call.metadata.get("_goal_state_sync") is True
    assert call.metadata["goal_state"] == {
        "active": True,
        "ui_summary": "alpha",
        "objective": "Objective alpha",
    }


@pytest.mark.asyncio
async def test_complete_goal_publishes_inactive_goal_state_ws(tmp_path):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    runtime_events = RuntimeEventBus()
    sm = SessionManager(tmp_path)
    WebuiTurnCoordinator(
        bus=bus,
        sessions=sm,
        schedule_background=lambda _coro: None,
    ).subscribe(runtime_events)
    lt = LongTaskTool(sessions=sm, runtime_events=runtime_events)
    cg = CompleteGoalTool(sessions=sm, runtime_events=runtime_events)
    rc = RequestContext(
        channel="websocket",
        chat_id="chat-z",
        session_key="websocket:chat-z",
        metadata={},
    )
    lt.set_context(rc)
    await lt.execute(goal="X")

    bus.publish_outbound.reset_mock()
    cg.set_context(rc)
    await cg.execute(recap="Done.")

    bus.publish_outbound.assert_awaited_once()
    call = bus.publish_outbound.await_args.args[0]
    assert call.metadata["goal_state"] == {"active": False}


@pytest.mark.asyncio
async def test_complete_goal_without_active_is_noop_message(tmp_path):
    sm = SessionManager(tmp_path)
    _lt, cg = _tools(sm)

    out = await cg.execute(recap="n/a")
    assert "No active" in out


@pytest.mark.asyncio
async def test_complete_goal_blocks_when_tasks_pending(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    cg.set_context(rc)

    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    await ut.execute(action="add", text="Run CI")
    await ut.execute(action="set", id="t1", status="done")

    out = await cg.execute(recap="Done.")
    assert "cannot close the goal" in out
    assert "Run CI" in out
    assert "acknowledge_pending=true" in out

    # blob 仍是 active（未关闭）
    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["status"] == "active"


@pytest.mark.asyncio
async def test_complete_goal_allows_acknowledge_pending(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    cg.set_context(rc)

    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    await ut.execute(action="add", text="Run CI")
    await ut.execute(action="set", id="t1", status="done")

    out = await cg.execute(recap="User reduced scope; API only.", acknowledge_pending=True)
    assert "marked complete" in out

    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["status"] == "completed"
    assert blob["tasks"] == [
        {"id": "t1", "text": "Write tests", "status": "done"},
        {"id": "t2", "text": "Run CI", "status": "pending"},
    ]


@pytest.mark.asyncio
async def test_complete_goal_all_done_requires_no_flag(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    cg.set_context(rc)

    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    await ut.execute(action="set", id="t1", status="done")

    out = await cg.execute(recap="All done.")
    assert "marked complete" in out

    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["status"] == "completed"


@pytest.mark.asyncio
async def test_complete_goal_without_tasks_still_closes(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _ut = _goal_tools(sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    cg.set_context(rc)

    await lt.execute(goal="Ship feature")
    out = await cg.execute(recap="No checklist.")
    assert "marked complete" in out
    assert sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]["status"] == "completed"


@pytest.mark.asyncio
async def test_long_task_skips_ws_publish_without_bus(tmp_path):
    sm = SessionManager(tmp_path)
    lt, _cg = _tools(sm)
    out = await lt.execute(goal="Solo", ui_summary="s")
    assert "Goal recorded" in out


@pytest.mark.asyncio
async def test_long_task_and_complete_goal_registered(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    lt = loop.tools.get("long_task")
    cg = loop.tools.get("complete_goal")
    assert lt is not None and lt.name == "long_task"
    assert cg is not None and cg.name == "complete_goal"


# ---------------------------------------------------------------------------
# update_task — structured task checklist
# ---------------------------------------------------------------------------


def _goal_tools(sm: SessionManager) -> tuple[LongTaskTool, UpdateTaskTool]:
    lt = LongTaskTool(sessions=sm)
    ut = UpdateTaskTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    lt.set_context(rc)
    ut.set_context(rc)
    return lt, ut


@pytest.mark.asyncio
async def test_update_task_registered(tmp_path):
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    ut = loop.tools.get("update_task")
    assert ut is not None
    assert ut.name == "update_task"


@pytest.mark.asyncio
async def test_update_task_add_appends_to_blob(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")

    out = await ut.execute(action="add", text="Write tests")
    assert "Updated task checklist: 0/1 tasks done." in out

    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["tasks"] == [{"id": "t1", "text": "Write tests", "status": "pending"}]


@pytest.mark.asyncio
async def test_update_task_add_with_status_defaults_and_increments_id(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")

    await ut.execute(action="add", text="first", status="in_progress")
    await ut.execute(action="add", text="second")
    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert [t["id"] for t in blob["tasks"]] == ["t1", "t2"]
    assert blob["tasks"][0]["status"] == "in_progress"
    assert blob["tasks"][1]["status"] == "pending"


@pytest.mark.asyncio
async def test_update_task_set_status_and_text(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")

    out = await ut.execute(action="set", id="t1", status="done")
    assert "1/1 tasks done." in out

    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["tasks"][0]["status"] == "done"


@pytest.mark.asyncio
async def test_update_task_set_matches_by_unique_text(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    await ut.execute(action="add", text="Run CI")

    out = await ut.execute(action="set", text="Run CI", status="done")
    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["tasks"][1]["status"] == "done"


@pytest.mark.asyncio
async def test_update_task_remove_deletes_entry(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    await ut.execute(action="add", text="Run CI")
    await ut.execute(action="add", text="Deploy")

    out = await ut.execute(action="remove", id="t2")
    assert "Run CI" not in out

    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert [t["text"] for t in blob["tasks"]] == ["Write tests", "Deploy"]


@pytest.mark.asyncio
async def test_update_task_requires_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    _lt, ut = _goal_tools(sm)
    out = await ut.execute(action="add", text="x")
    assert "requires an active sustained goal" in out


@pytest.mark.asyncio
async def test_update_task_add_requires_text(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")
    out = await ut.execute(action="add")
    assert "requires a non-empty 'text'" in out


@pytest.mark.asyncio
async def test_update_task_set_requires_status(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    out = await ut.execute(action="set", id="t1")
    assert "requires a 'status'" in out


@pytest.mark.asyncio
async def test_update_task_unknown_id_returns_current_listing(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    await ut.execute(action="add", text="Run CI")

    out = await ut.execute(action="set", id="t99", status="done")
    assert "task not found" in out
    assert "t1: Write tests" in out
    assert "t2: Run CI" in out


@pytest.mark.asyncio
async def test_update_task_rejects_at_capacity(tmp_path):
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    await lt.execute(goal="Ship feature")
    for i in range(_MAX_TASKS):
        await ut.execute(action="add", text=f"step {i}")

    out = await ut.execute(action="add", text="overflow")
    assert "at capacity" in out
    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert len(blob["tasks"]) == _MAX_TASKS


@pytest.mark.asyncio
async def test_goal_tasks_flow_retained_through_complete_goal(tmp_path):
    """long_task -> update_task(add/set) -> complete_goal keeps the task list in blob."""
    sm = SessionManager(tmp_path)
    lt, ut = _goal_tools(sm)
    cg = CompleteGoalTool(sessions=sm)
    rc = RequestContext(
        channel="websocket",
        chat_id="c1",
        session_key="websocket:c1",
        metadata={},
    )
    cg.set_context(rc)

    await lt.execute(goal="Ship feature")
    await ut.execute(action="add", text="Write tests")
    await ut.execute(action="set", id="t1", status="done")

    await cg.execute(recap="Done.")

    blob = sm.get_or_create("websocket:c1").metadata[GOAL_STATE_KEY]
    assert blob["status"] == "completed"
    assert blob["tasks"] == [{"id": "t1", "text": "Write tests", "status": "done"}]


@pytest.mark.asyncio
async def test_update_task_publishes_ws_blob_with_tasks(tmp_path):
    bus = MagicMock()
    bus.publish_outbound = AsyncMock()
    runtime_events = RuntimeEventBus()
    sm = SessionManager(tmp_path)
    WebuiTurnCoordinator(
        bus=bus,
        sessions=sm,
        schedule_background=lambda _coro: None,
    ).subscribe(runtime_events)
    lt = LongTaskTool(sessions=sm, runtime_events=runtime_events)
    ut = UpdateTaskTool(sessions=sm, runtime_events=runtime_events)
    rc = RequestContext(
        channel="websocket",
        chat_id="chat-task",
        session_key="websocket:chat-task",
        metadata={},
    )
    lt.set_context(rc)
    ut.set_context(rc)

    await lt.execute(goal="Objective", ui_summary="o")
    bus.publish_outbound.reset_mock()
    await ut.execute(action="add", text="first step")

    bus.publish_outbound.assert_awaited_once()
    call = bus.publish_outbound.await_args.args[0]
    assert call.metadata.get("_goal_state_sync") is True
    assert call.metadata["goal_state"]["active"] is True
    assert call.metadata["goal_state"]["tasks"] == [
        {"id": "t1", "text": "first step", "status": "pending"}
    ]
