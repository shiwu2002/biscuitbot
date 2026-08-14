"""系统提示中的数字员工团队板块与 persona 标题（含职位）测试。"""

from __future__ import annotations

import json
from pathlib import Path

from biscuitbot.agent.context import ContextBuilder
from biscuitbot.agent.employees import EmployeeStore


def _store(tmp_path: Path) -> EmployeeStore:
    return EmployeeStore(tmp_path / "ws")


def _write_empty_current(workspace: Path) -> None:
    """在 ContextBuilder 的员工文件位置写入当前版本、但没有员工的文件。"""
    path = workspace / "employees.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "builtin_seeded": True,
                "builtin_version": 7,
                "employees": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class TestRosterSection:
    def test_system_prompt_lists_employee_team(self, tmp_path: Path) -> None:
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt()
        assert "数字员工团队" in prompt
        assert "invoke_employee" in prompt
        for codename in ("阿伟", "灵溪", "沐辰", "阿凯", "静娴", "达芬奇", "宫本"):
            assert codename in prompt

    def test_roster_hidden_when_no_enabled_employees(self, tmp_path: Path) -> None:
        _write_empty_current(tmp_path)
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt()
        assert "数字员工团队" not in prompt

    def test_roster_shows_title_tag(self, tmp_path: Path) -> None:
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt()
        assert "阿伟（AI视频剪辑总监）" in prompt


class TestPersonaHeading:
    def test_persona_heading_includes_title(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        clip = store.get_employee("clip-master")
        section = ContextBuilder._persona_section(clip)
        assert section.splitlines()[0] == "# Persona — 阿伟（AI视频剪辑总监） 🎬"
        assert "你是「阿伟」" in section

    def test_persona_heading_without_title_omits_brackets(self) -> None:
        emp = {"name": "无名", "title": "", "avatar": "", "system_prompt": "随便。"}
        section = ContextBuilder._persona_section(emp)
        assert section.splitlines()[0] == "# Persona — 无名"


class TestEmployeeSummary:
    def test_strips_self_prefix(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        summary = ContextBuilder._employee_summary(store.get_employee("clip-master"))
        assert summary.startswith("团队里的AI视频剪辑总监、后期导演和内容包装专家")
        assert len(summary) <= 48

    def test_empty_persona_yields_empty_summary(self) -> None:
        assert ContextBuilder._employee_summary({"system_prompt": "  "}) == ""


class TestSkillBelongsToEmployee:
    """技能归属自己：绑定数字员工时技能摘要只列该员工自己的技能，主会话共享全部。"""

    def test_main_session_lists_all_skills(self, tmp_path: Path) -> None:
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt()
        # 主会话（未绑定员工）：摘要包含多个不同技能的目录名
        for skill in ("seedance", "ip-positioning", "secretary", "design"):
            assert skill in prompt, skill

    def test_employee_session_scopes_to_own_skills(self, tmp_path: Path) -> None:
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt(
            session_metadata={"employee": "ip-consultant"}
        )
        # 灵溪：只出现自己的技能，不出现其他员工的技能
        assert "ip-positioning" in prompt
        assert "seedance" not in prompt
        assert "jianying-editor" not in prompt

    def test_video_employee_own_skills(self, tmp_path: Path) -> None:
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt(
            session_metadata={"employee": "video-master"}
        )
        assert "seedance" in prompt
        assert "ip-positioning" not in prompt
        assert "secretary" not in prompt
        assert "design" not in prompt

    def test_employee_without_skills_has_no_skill_summary(self, tmp_path: Path) -> None:
        # 给一个内置员工清空技能，模拟无技能员工：技能摘要应为空（归属自己，不共享全部）
        store = _store(tmp_path)
        store.update_employee("clip-master", {"skills": []})
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt(
            session_metadata={"employee": "clip-master"}
        )
        # 无技能员工：任何技能目录名都不应出现在技能摘要中（选一个不会出现在阿伟 persona 里的）
        assert "secretary" not in prompt
        assert "design" not in prompt
