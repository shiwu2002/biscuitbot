"""本轮指定技能（对话界面 ``/`` 选技能）的注入测试。

覆盖三件事：正文真的进了模型可见的运行时块、名字要先过现有技能白名单、以及
超长正文会被截断（技能正文整段注入是这一功能的成本所在）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from xianaibot.agent.skill_attachment import (
    _MAX_SKILL_BODY_CHARS,
    runtime_lines,
    session_extra,
)


def _workspace(tmp_path: Path, *, body: str = "第一步：先问清需求。\n第二步：交付。") -> Path:
    """建一个带工作区技能的工作目录（内置技能目录指向空目录，保持用例自洽）。"""
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示技能\n---\n\n" f"# 演示技能\n\n{body}\n",
        encoding="utf-8",
    )
    (tmp_path / "builtin").mkdir(exist_ok=True)
    return tmp_path


def _message(metadata: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(content="帮我处理一下", metadata=metadata)


def test_session_extra_returns_skills_only_when_present() -> None:
    skills = [{"name": "demo-skill"}]
    assert session_extra({"skills": skills}) == {"skills": skills}
    assert session_extra({}) == {}
    assert session_extra(None) == {}
    assert session_extra({"skills": "demo-skill"}) == {}


def test_runtime_lines_injects_skill_body_and_mandate(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = {"skills": [{"name": "demo-skill", "display_name": "演示技能"}]}

    lines = runtime_lines(_message(metadata), workspace)

    joined = "\n".join(lines)
    assert "Skill Attachment: demo-skill" in joined
    assert "MANDATORY" in joined
    # 正文（frontmatter 已剥离）必须真的进来，否则「选中即使用」无从谈起
    assert "第一步：先问清需求。" in joined
    assert "### Skill: demo-skill" in joined
    assert "演示技能\n---" not in joined  # frontmatter 不该留在注入内容里
    assert str(workspace / "skills" / "demo-skill" / "SKILL.md") in joined


def test_runtime_lines_skips_when_asked(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    metadata = {"skills": [{"name": "demo-skill"}]}

    assert runtime_lines(_message(metadata), workspace, skip=True) == []


def test_runtime_lines_without_attachments_is_empty(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    assert runtime_lines(_message({}), workspace) == []
    assert runtime_lines(_message({"skills": []}), workspace) == []
    assert runtime_lines(_message({"skills": [{"display_name": "没有名字"}]}), workspace) == []


def test_runtime_lines_drops_unknown_and_illegal_names(tmp_path: Path) -> None:
    """越界名字必须被丢掉——它们是路径片段的候选，绝不能拼进文件路径。"""
    workspace = _workspace(tmp_path)
    evil = workspace / "skills" / ".." / "secret"
    evil.mkdir(parents=True, exist_ok=True)
    (evil / "SKILL.md").write_text("---\nname: secret\n---\n\n不要读我\n", encoding="utf-8")
    metadata = {
        "skills": [
            {"name": "不存在的技能"},
            {"name": "../../secret"},
            {"name": "demo-skill"},  # 只有这个是合法且存在的
        ]
    }

    lines = runtime_lines(_message(metadata), workspace)

    joined = "\n".join(lines)
    assert "不要读我" not in joined
    assert "secret" not in joined
    assert "Skill Attachment: demo-skill" in joined


def test_runtime_lines_truncates_long_skill_body(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, body="长" * (_MAX_SKILL_BODY_CHARS + 5_000))
    metadata = {"skills": [{"name": "demo-skill"}]}

    joined = "\n".join(runtime_lines(_message(metadata), workspace))

    assert "truncated" in joined
    assert len(joined) < _MAX_SKILL_BODY_CHARS + 1_000


def test_runtime_lines_keeps_at_most_eight_skills(tmp_path: Path) -> None:
    workspace = tmp_path
    names = [f"skill-{index}" for index in range(12)]
    for name in names:
        skill_dir = workspace / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n\n{name} 正文\n", encoding="utf-8")
    metadata = {"skills": [{"name": name} for name in names]}

    joined = "\n".join(runtime_lines(_message(metadata), workspace))

    assert "Skill Attachment: skill-0" in joined
    assert "Skill Attachment: skill-7" in joined
    assert "Skill Attachment: skill-8" not in joined
