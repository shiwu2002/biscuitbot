"""``normalize_skill_mentions`` 的清洗规则测试。

这里只覆盖**语法**层面：类型、长度、字符集、条数、去重、字段白名单。「这个名字是
否真是现有技能」由 ``agent/skill_attachment`` 按真实技能表核对（另有用例覆盖）。
"""

from __future__ import annotations

from typing import Any

from xianaibot.webui.skills_api import normalize_skill_mentions


def test_keeps_allowlisted_fields_only() -> None:
    rows = normalize_skill_mentions(
        [
            {
                "name": "Demo-Skill",
                "display_name": "演示技能",
                "source": "workspace",
                "description": "演示用",
                "path": "C:/secret/SKILL.md",  # 不在白名单，必须丢弃
                "raw_markdown": "# 不该带上",  # 不在白名单，必须丢弃
            }
        ]
    )

    assert rows == [
        {
            "name": "demo-skill",
            "display_name": "演示技能",
            "source": "workspace",
            "description": "演示用",
        }
    ]


def test_rejects_illegal_names_and_non_dicts() -> None:
    rows = normalize_skill_mentions(
        [
            "demo-skill",  # 裸字符串不接受（与 cli_apps 同款：只认结构化条目）
            {"name": ""},
            {"name": "  "},
            {"name": "../../etc/passwd"},
            {"name": "C:/windows/system32"},
            {"name": "技能"},  # 非合法字符集
            {"name": "-leading-dash"},
            {"display_name": "没有名字"},
            None,
        ]
    )

    assert rows == []


def test_dedupes_case_insensitively_and_keeps_order() -> None:
    rows = normalize_skill_mentions(
        [{"name": "b-skill"}, {"name": "A-Skill"}, {"name": "B-SKILL"}]
    )

    assert [row["name"] for row in rows] == ["b-skill", "a-skill"]


def test_keeps_at_most_eight_entries() -> None:
    rows = normalize_skill_mentions([{"name": f"skill-{i}"} for i in range(20)])

    assert len(rows) == 8
    assert rows[0]["name"] == "skill-0"
    assert rows[-1]["name"] == "skill-7"


def test_clips_long_field_values() -> None:
    rows = normalize_skill_mentions([{"name": "demo-skill", "description": "长" * 500}])

    assert len(rows[0]["description"]) == 160


def test_non_list_input_returns_empty() -> None:
    for raw in (None, {}, "demo-skill", 42, [{"name": "demo-skill"}][0]):
        assert normalize_skill_mentions(raw) == []


def test_blank_optional_fields_are_dropped() -> None:
    rows: list[dict[str, Any]] = normalize_skill_mentions(
        [{"name": "demo-skill", "display_name": "   ", "source": None}]
    )

    assert rows == [{"name": "demo-skill"}]
