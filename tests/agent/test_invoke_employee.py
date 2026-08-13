"""invoke_employee 工具与 SubagentManager.run_employee_inline 的单元测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from biscuitbot.agent.employees import EmployeeStore
from biscuitbot.agent.subagent import SubagentManager
from biscuitbot.agent.tools.employee import InvokeEmployeeTool
from biscuitbot.bus.queue import MessageBus
from biscuitbot.config.schema import ToolsConfig


def _store(tmp_path: Path) -> EmployeeStore:
    return EmployeeStore(tmp_path / "ws")


def _manager(tmp_path: Path) -> SubagentManager:
    """构造 SubagentManager（_build_tools/runner 由用例 stub）。"""
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return SubagentManager(
        provider=provider,
        workspace=tmp_path / "ws",
        bus=MessageBus(),
        max_tool_result_chars=8000,
        tools_config=ToolsConfig(),
    )


class TestInvokeEmployeeTool:
    def _tool(self, tmp_path: Path, content: str = "员工的成果") -> InvokeEmployeeTool:
        store = _store(tmp_path)
        manager = _manager(tmp_path)
        manager.run_employee_inline = AsyncMock(return_value=content)
        return InvokeEmployeeTool(
            subagent_manager=manager,
            employees=store,
        )

    async def test_execute_returns_employee_result(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path, "这是剪影剪好的成片说明")
        out = await tool.execute("clip-master", "帮我剪辑一段视频")
        assert "阿伟" in out
        assert "这是剪影剪好的成片说明" in out

    async def test_unknown_employee_lists_roster(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute("ghost", "干活")
        assert "没有可用的数字员工" in out
        assert "clip-master" in out  # 名单里有可用员工
        assert "阿伟" in out

    async def test_disabled_employee_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.update_employee("clip-master", {"enabled": False})
        manager = _manager(tmp_path)
        manager.run_employee_inline = AsyncMock(return_value="x")
        tool = InvokeEmployeeTool(subagent_manager=manager, employees=store)
        out = await tool.execute("clip-master", "干活")
        assert "没有可用的数字员工" in out

    async def test_empty_task_rejected(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute("clip-master", "   ")
        assert "缺少任务描述" in out

    async def test_empty_employee_id_rejected(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute("", "干活")
        assert "缺少 employee_id" in out

    async def test_skills_scope_to_employee_own(self, tmp_path: Path) -> None:
        """技能归属自己：调用员工时传入该员工自己的技能 allowlist，而非共享全部。"""
        tool = self._tool(tmp_path)
        await tool.execute("clip-master", "干活")
        assert tool._manager.run_employee_inline.await_args.kwargs["include_skills"] == {"jianying-editor"}

    async def test_skills_own_for_employee_with_bound_skill(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        await tool.execute("super-secretary", "安排日程")
        assert tool._manager.run_employee_inline.await_args.kwargs["include_skills"] == {"secretary"}

    async def test_long_result_is_truncated(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path, "长" * 30000)
        out = await tool.execute("clip-master", "干活")
        assert "（成果已截断）" in out
        assert len(out) < 30000

    def test_enabled_gating(self) -> None:
        assert InvokeEmployeeTool.enabled(MagicMock(subagent_manager=None, employees=None)) is False
        assert InvokeEmployeeTool.enabled(MagicMock(subagent_manager=object(), employees=object())) is True


class TestRunEmployeeInline:
    def _manager_with_fake_result(self, tmp_path: Path, result: object) -> SubagentManager:
        mgr = _manager(tmp_path)
        mgr._build_tools = MagicMock(return_value=MagicMock())
        mgr.runner.run = AsyncMock(return_value=result)
        return mgr

    async def test_build_subagent_prompt_includes_persona(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mgr = _manager(tmp_path)
        prompt = mgr._build_subagent_prompt(employee=store.get_employee("clip-master"))
        assert "# Persona — 阿伟（剪辑） 🎬" in prompt
        assert "以数字员工「阿伟」的身份执行任务" in prompt

    async def test_include_skills_scopes_skills_summary(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mgr = _manager(tmp_path)
        scoped = mgr._build_subagent_prompt(
            employee=store.get_employee("clip-master"),
            include_skills={"jianying-editor"},
        )
        assert "jianying-editor" in scoped
        assert "seedance" not in scoped  # 白名单外技能不出现在摘要
        all_skills = mgr._build_subagent_prompt(employee=store.get_employee("clip-master"))
        assert "seedance" in all_skills  # include=None 表示全部

    async def test_returns_final_content(self, tmp_path: Path) -> None:
        mgr = self._manager_with_fake_result(
            tmp_path,
            SimpleNamespace(
                final_content="视频已完成",
                stop_reason="end_turn",
                error=None,
                tool_events=[],
            ),
        )
        out = await mgr.run_employee_inline({"id": "x", "name": "剪影", "title": "剪辑"}, "任务")
        assert out == "视频已完成"

    async def test_returns_error_text_on_failure(self, tmp_path: Path) -> None:
        mgr = self._manager_with_fake_result(
            tmp_path,
            SimpleNamespace(
                final_content="",
                stop_reason="error",
                error="模型超时",
                tool_events=[],
            ),
        )
        out = await mgr.run_employee_inline({"id": "x", "name": "剪影", "title": "剪辑"}, "任务")
        assert "数字员工执行失败" in out
        assert "模型超时" in out

    async def test_returns_error_text_on_exception(self, tmp_path: Path) -> None:
        mgr = _manager(tmp_path)
        mgr._build_tools = MagicMock(return_value=MagicMock())

        async def _boom(*args, **kwargs):  # noqa: ARG001
            raise RuntimeError("boom")

        mgr.runner.run = _boom
        out = await mgr.run_employee_inline({"id": "x", "name": "剪影", "title": "剪辑"}, "任务")
        assert "数字员工执行失败" in out
        assert "boom" in out
