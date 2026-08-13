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
                "builtin_version": 2,
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
        for codename in ("剪影", "探微", "光影", "阿爆", "得力", "绘野"):
            assert codename in prompt

    def test_roster_hidden_when_no_enabled_employees(self, tmp_path: Path) -> None:
        _write_empty_current(tmp_path)
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt()
        assert "数字员工团队" not in prompt

    def test_roster_shows_title_tag(self, tmp_path: Path) -> None:
        cb = ContextBuilder(tmp_path)
        prompt = cb.build_system_prompt()
        assert "剪影（剪辑）" in prompt


class TestPersonaHeading:
    def test_persona_heading_includes_title(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        clip = store.get_employee("clip-master")
        section = ContextBuilder._persona_section(clip)
        assert section.splitlines()[0] == "# Persona — 剪影（剪辑） 🎬"
        assert "你是「剪影」" in section

    def test_persona_heading_without_title_omits_brackets(self) -> None:
        emp = {"name": "无名", "title": "", "avatar": "", "system_prompt": "随便。"}
        section = ContextBuilder._persona_section(emp)
        assert section.splitlines()[0] == "# Persona — 无名"


class TestEmployeeSummary:
    def test_strips_self_prefix(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        summary = ContextBuilder._employee_summary(store.get_employee("clip-master"))
        assert summary.startswith("团队里的剪辑高手")
        assert len(summary) <= 48

    def test_empty_persona_yields_empty_summary(self) -> None:
        assert ContextBuilder._employee_summary({"system_prompt": "  "}) == ""
