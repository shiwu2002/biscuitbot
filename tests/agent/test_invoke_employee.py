"""invoke_employee 工具与 SubagentManager.run_employee_inline 的单元测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
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

    async def test_chinese_name_resolves_to_employee(self, tmp_path: Path) -> None:
        """主智能体用中文姓名点名也能正确解析（阿伟 → clip-master）。"""
        tool = self._tool(tmp_path, "这是中文姓名调用的成果")
        out = await tool.execute("阿伟", "帮我剪辑一段视频")
        assert "数字员工「阿伟（AI视频剪辑总监）」" in out
        assert "这是中文姓名调用的成果" in out
        assert "没有可用的数字员工" not in out

    async def test_chinese_name_with_title_resolves(self, tmp_path: Path) -> None:
        """「姓名（职位）」组合形式同样可解析。"""
        tool = self._tool(tmp_path, "成果x")
        out = await tool.execute("灵溪（个人IP战略顾问）", "做定位咨询")
        assert "数字员工「灵溪（个人IP战略顾问）」" in out
        assert "没有可用的数字员工" not in out

    async def test_chinese_name_for_other_employee(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path, "成果y")
        out = await tool.execute("阿凯", "短视频增长")
        assert "数字员工「阿凯（短视频增长操盘手）」" in out

    async def test_unknown_chinese_name_still_errors(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        out = await tool.execute("王五", "干活")
        assert "没有可用的数字员工" in out
        assert "王五" in out

    async def test_disabled_employee_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create_employee(
            {"id": "writer", "name": "写作助手", "system_prompt": "你是写作助手数字人员工。"}
        )
        store.update_employee("writer", {"enabled": False})
        manager = _manager(tmp_path)
        manager.run_employee_inline = AsyncMock(return_value="x")
        tool = InvokeEmployeeTool(subagent_manager=manager, employees=store)
        out = await tool.execute("writer", "干活")
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

    async def test_long_result_persisted_with_pointer(self, tmp_path: Path) -> None:
        """绑定工作区时，超限成果落盘到 tool-results，内联文本保留精华并附路径。"""
        from biscuitbot.security.workspace_access import (
            bind_workspace_scope,
            build_workspace_scope,
            reset_workspace_scope,
        )

        ws = tmp_path / "ws"
        tool = self._tool(tmp_path, "长" * 30000)
        token = bind_workspace_scope(build_workspace_scope(ws, "restricted"))
        try:
            out = await tool.execute("clip-master", "干活")
        finally:
            reset_workspace_scope(token)
        assert "（成果已截断" in out
        assert "完整成果已保存至" in out
        assert "数字员工" in out
        saved = list((ws / ".biscuitbot" / "tool-results" / "employees").glob("*.txt"))
        assert len(saved) == 1
        assert len(saved[0].read_text(encoding="utf-8")) == 30000  # 完整成果未丢

    async def test_long_result_no_scope_falls_back_to_hard_truncate(self, tmp_path: Path) -> None:
        """无工作区作用域时回退硬截断，不落盘、不报错。"""
        tool = self._tool(tmp_path, "长" * 30000)
        out = await tool.execute("clip-master", "干活")
        assert "（成果已截断）" in out
        assert "完整成果已保存至" not in out

    def test_enabled_gating(self) -> None:
        assert InvokeEmployeeTool.enabled(MagicMock(subagent_manager=None, employees=None)) is False
        assert InvokeEmployeeTool.enabled(MagicMock(subagent_manager=object(), employees=object())) is True


class TestRunEmployeeInline:
    def _manager_with_fake_result(self, tmp_path: Path, result: object) -> SubagentManager:
        mgr = _manager(tmp_path)
        mgr._build_tools = MagicMock(return_value=MagicMock())
        mgr.runner = MagicMock()  # runner 整体替换，run 为 AsyncMock，便于断言调用
        mgr.runner.run = AsyncMock(return_value=result)
        return mgr

    async def test_build_subagent_prompt_includes_persona(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mgr = _manager(tmp_path)
        prompt = mgr._build_subagent_prompt(employee=store.get_employee("clip-master"))
        # 内置员工已切换为图片头像：文件名不进子 Agent 文本上下文
        assert "# Persona — 阿伟（AI视频剪辑总监）" in prompt
        assert "img_" not in prompt
        assert "以数字员工「阿伟」的身份执行任务" in prompt

    async def test_include_skills_scopes_skills_summary(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        mgr = _manager(tmp_path)
        scoped = mgr._build_subagent_prompt(
            employee=store.get_employee("clip-master"),
            include_skills={"jianying-editor"},
        )
        assert "jianying-editor" in scoped
        assert "ip-positioning" not in scoped  # 白名单外技能不出现在摘要
        all_skills = mgr._build_subagent_prompt(employee=store.get_employee("clip-master"))
        assert "ip-positioning" in all_skills  # include=None 表示全部

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

    async def test_inline_contract_appended_when_cap_passed(self, tmp_path: Path) -> None:
        """传入 max_result_chars 时，系统提示追加内联返回约定（含上限数字）。"""
        mgr = self._manager_with_fake_result(
            tmp_path,
            SimpleNamespace(
                final_content="ok",
                stop_reason="end_turn",
                error=None,
                tool_events=[],
            ),
        )
        await mgr.run_employee_inline(
            {"id": "x", "name": "剪影", "title": "剪辑"},
            "任务",
            max_result_chars=12000,
        )
        run_mock: Any = mgr.runner.run  # 运行时是 AsyncMock
        spec = run_mock.call_args.args[0]
        system_content = spec.initial_messages[0]["content"]
        assert "内联返回约定" in system_content
        assert "12000" in system_content

    async def test_no_contract_when_cap_not_passed(self, tmp_path: Path) -> None:
        """不传 max_result_chars 时，系统提示不含内联返回约定。"""
        mgr = self._manager_with_fake_result(
            tmp_path,
            SimpleNamespace(
                final_content="ok",
                stop_reason="end_turn",
                error=None,
                tool_events=[],
            ),
        )
        await mgr.run_employee_inline({"id": "x", "name": "剪影", "title": "剪辑"}, "任务")
        run_mock: Any = mgr.runner.run  # 运行时是 AsyncMock
        spec = run_mock.call_args.args[0]
        system_content = spec.initial_messages[0]["content"]
        assert "内联返回约定" not in system_content

    async def test_inline_run_uses_streaming_hook(self, tmp_path: Path) -> None:
        """内联执行必须挂流式 hook：豁免外层 300s 墙钟超时（宫本超时回归）。"""
        mgr = self._manager_with_fake_result(
            tmp_path,
            SimpleNamespace(
                final_content="ok",
                stop_reason="end_turn",
                error=None,
                tool_events=[],
            ),
        )
        await mgr.run_employee_inline({"id": "x", "name": "剪影", "title": "剪辑"}, "任务")
        run_mock: Any = mgr.runner.run  # 运行时是 AsyncMock
        spec = run_mock.call_args.args[0]
        assert spec.hook is not None
        assert spec.hook.wants_streaming() is True

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
