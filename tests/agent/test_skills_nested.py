"""技能商店（SkillHub）的 ``@<handle>/<slug>`` 嵌套布局在 SkillsLoader 下的行为。

纯文件系统测试，不联网、不涉及 CLI。覆盖三件事：
1. 嵌套技能能否被发现并按**扁平名**解析（所有 name→路径解析都汇聚到 load_skill）；
2. 别名是 ``(handle, slug)`` 的纯函数——卸载同名技能不会让已有引用悄悄改指别人；
3. 第三方商店内容的两道边界：不参与 always 常驻注入、依赖声明（clawdbot 键）被识别。
"""

from __future__ import annotations

import json
from pathlib import Path

from xianaibot.agent.skills import SkillsLoader

_FAKE_BIN = "xianaibot-definitely-missing-bin"


def _write_skill(
    base: Path,
    name: str,
    *,
    metadata_json: dict | None = None,
    body: str = "# Skill\n",
) -> Path:
    """在 ``base / name / SKILL.md`` 写入一个技能，可带 metadata JSON。"""
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    lines = ["---"]
    if metadata_json is not None:
        # ensure_ascii=False：真实的 SkillHub SKILL.md 里是字面 emoji。若转成
        # \uXXXX 代理对，会被 YAML 双引号标量二次解释而失真。
        payload = json.dumps(metadata_json, separators=(",", ":"), ensure_ascii=False)
        lines.append(f"metadata: {payload}")
    lines.extend(["---", "", body])
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _loader(tmp_path: Path) -> tuple[SkillsLoader, Path, Path]:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    return SkillsLoader(workspace, builtin_skills_dir=builtin), skills_root, builtin


def test_namespaced_skill_is_discovered_with_flat_name(tmp_path: Path) -> None:
    loader, skills_root, _ = _loader(tmp_path)
    nested = _write_skill(skills_root / "@alice", "calendar", body="# Calendar")

    entries = loader.list_skills(filter_unavailable=False)

    assert [entry["name"] for entry in entries] == ["alice-calendar"]
    entry = entries[0]
    assert entry["source"] == "skillhub"
    assert Path(entry["path"]) == nested
    assert Path(entry["path"]).parent.name == "calendar"


def test_namespaced_skill_resolves_by_flat_name_everywhere(tmp_path: Path) -> None:
    """扁平名必须能被所有走 load_skill 的入口解析，这是本设计的核心不变量。"""
    loader, skills_root, _ = _loader(tmp_path)
    _write_skill(
        skills_root / "@alice",
        "calendar",
        metadata_json={"xianaibot": {"requires": {"env": ["XIANAIBOT_NOPE_ENV"]}}},
        body="# Calendar",
    )

    assert loader.load_skill("alice-calendar") is not None
    # 反证：目录名本身不是技能名，不应能被解析。
    assert loader.load_skill("calendar") is None

    metadata = loader.get_skill_metadata("alice-calendar")
    assert metadata is not None

    requirements = loader.get_skill_requirements("alice-calendar")
    assert requirements["env"] == ["XIANAIBOT_NOPE_ENV"]

    available, reason = loader.get_skill_availability("alice-calendar")
    assert available is False
    assert "XIANAIBOT_NOPE_ENV" in reason

    capability = loader.get_skill_capability("alice-calendar")
    assert capability["runtime"] == "prompt"


def test_same_slug_in_two_namespaces_keeps_distinct_names(tmp_path: Path) -> None:
    loader, skills_root, _ = _loader(tmp_path)
    _write_skill(skills_root / "@alice", "cal", body="# Alice Cal")
    _write_skill(skills_root / "@bob", "cal", body="# Bob Cal")

    names = {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}

    assert names == {"alice-cal", "bob-cal"}
    assert loader.load_skill("alice-cal") == "# Alice Cal" or "Alice Cal" in (
        loader.load_skill("alice-cal") or ""
    )
    assert "Bob Cal" in (loader.load_skill("bob-cal") or "")


def test_plain_skill_wins_and_nested_falls_back(tmp_path: Path) -> None:
    """工作区手写技能优先，商店技能退化一次；退化后仍可解析。"""
    loader, skills_root, _ = _loader(tmp_path)
    _write_skill(skills_root, "alice-cal", body="# Handwritten")
    _write_skill(skills_root / "@alice", "cal", body="# From Store")

    entries = {entry["name"]: entry for entry in loader.list_skills(filter_unavailable=False)}

    assert set(entries) == {"alice-cal", "alice--cal"}
    assert entries["alice-cal"]["source"] == "workspace"
    assert entries["alice--cal"]["source"] == "skillhub"
    assert "Handwritten" in (loader.load_skill("alice-cal") or "")
    assert "From Store" in (loader.load_skill("alice--cal") or "")


def test_names_do_not_drift_when_sibling_namespace_is_removed(tmp_path: Path) -> None:
    """卸载 ``@alice/cal`` 不得让 ``bob-cal`` 改名。

    技能名是持久化标识（写在 employees.json 的 skills、skill_owners 的 owners 键
    与 URL 路径段里）。若别名是「裸 slug 优先」，这里 bob 的技能会从 ``bob-cal``
    变成 ``cal``，所有已有引用被静默改指。
    """
    import shutil

    loader, skills_root, _ = _loader(tmp_path)
    _write_skill(skills_root / "@alice", "cal", body="# Alice")
    _write_skill(skills_root / "@bob", "cal", body="# Bob")

    assert {entry["name"] for entry in loader.list_skills(filter_unavailable=False)} == {
        "alice-cal",
        "bob-cal",
    }

    shutil.rmtree(skills_root / "@alice")

    assert [entry["name"] for entry in loader.list_skills(filter_unavailable=False)] == [
        "bob-cal"
    ]


def test_store_skill_cannot_self_declare_always(tmp_path: Path) -> None:
    """第三方商店技能不得靠 frontmatter 把自己常驻注入 agent 上下文。"""
    loader, skills_root, _ = _loader(tmp_path)
    _write_skill(skills_root / "@alice", "sticky", metadata_json={"xianaibot": {"always": True}})
    _write_skill(skills_root, "local-sticky", metadata_json={"xianaibot": {"always": True}})

    always = loader.get_always_skills()

    assert "local-sticky" in always
    assert "alice-sticky" not in always


def test_store_skill_requires_are_read_from_clawdbot_key(tmp_path: Path) -> None:
    """SkillHub 技能用 metadata.clawdbot 声明依赖，不识别会让缺依赖的技能仍显示可用。"""
    loader, skills_root, _ = _loader(tmp_path)
    _write_skill(
        skills_root / "@alice",
        "needs-bin",
        metadata_json={"clawdbot": {"emoji": "🧰", "requires": {"bins": [_FAKE_BIN]}}},
    )

    assert loader.list_skills(filter_unavailable=True) == []
    available, reason = loader.get_skill_availability("alice-needs-bin")
    assert available is False
    assert _FAKE_BIN in reason
    # emoji 也应能读到，供能力页显示图标。
    assert loader.get_skill_capability("alice-needs-bin")["icon"] == "🧰"


def test_plain_nested_directory_is_not_treated_as_namespace(tmp_path: Path) -> None:
    """只有 ``@handle`` 形状的目录才下钻一层，其它嵌套目录维持原行为。"""
    loader, skills_root, _ = _loader(tmp_path)
    _write_skill(skills_root / "group", "inner", body="# Inner")

    assert loader.list_skills(filter_unavailable=False) == []


def test_namespace_dir_without_skill_md_is_ignored(tmp_path: Path) -> None:
    loader, skills_root, _ = _loader(tmp_path)
    (skills_root / "@empty" / "stray").mkdir(parents=True)
    (skills_root / "@empty" / "stray" / "README.md").write_text("x", encoding="utf-8")

    assert loader.list_skills(filter_unavailable=False) == []


def test_builtin_namespaced_skill_is_also_supported(tmp_path: Path) -> None:
    """内置目录（随包分发）同样可能带命名空间技能。"""
    loader, _, builtin = _loader(tmp_path)
    _write_skill(builtin / "@xianaibot", "helper", body="# Helper")

    entries = loader.list_skills(filter_unavailable=False)

    assert [(entry["name"], entry["source"]) for entry in entries] == [
        ("xianaibot-helper", "skillhub")
    ]
