"""技能商店端到端：假 CLI + 真落盘 + 真 SkillsLoader + 真删除。

不联网、不装真技能，但走完整的真实调用链：``install_skill`` 会真的以子进程执行
一个本地假 CLI（复刻官方 SkillHub 的落盘布局与锁文件语义），随后由真实的
``SkillsLoader`` 去发现它，最后由真实的 ``delete_workspace_skill`` 删除，并断言
技能目录与锁文件条目**双双消失**。

这一条同时覆盖四个环节：argv 组装、``@handle/slug`` 布局约定、加载器识别、
以及「删除技能与 lockfile 一致性」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xianaibot.agent.skills import SkillsLoader
from xianaibot.config.schema import SkillHubConfig
from xianaibot.webui import skill_hub
from xianaibot.webui.skills_api import delete_workspace_skill

# 复刻官方 CLI 的关键行为：@handle/slug 落盘、锁文件语义、目标已存在时报错退出。
_FAKE_CLI = r'''
import json
import sys
from pathlib import Path


def arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def lock_path(root):
    return Path(root) / ".skills_store_lock.json"


def read_lock(root):
    p = lock_path(root)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": 1, "skills": {}}


def write_lock(root, lock):
    Path(root).mkdir(parents=True, exist_ok=True)
    lock_path(root).write_text(json.dumps(lock, indent=2), encoding="utf-8")


argv = sys.argv[1:]
while argv and argv[0] == "--skip-self-upgrade":
    argv = argv[1:]
cmd = argv[0] if argv else ""

if cmd == "install":
    slug = argv[1]
    ns = arg("--namespace", "")
    root = arg("--dir", "./skills")
    target = Path(root) / ("@" + ns) / slug
    if target.exists() and "--force" not in argv:
        print("Error: Target exists: " + str(target) + " (use --force to overwrite)", file=sys.stderr)
        sys.exit(1)
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        "---\nname: " + slug + "\ndescription: fake skill\n---\n\n# " + slug + "\n",
        encoding="utf-8",
    )
    (target / "_meta.json").write_text(
        json.dumps({"slug": slug, "version": "1.0.0"}), encoding="utf-8"
    )
    lock = read_lock(root)
    lock.setdefault("skills", {})["@" + ns + "/" + slug] = {
        "name": slug,
        "version": "1.0.0",
        "source": "clawhub",
        "internalSlug": slug,
        "namespace": {"handle": ns, "displayName": ns},
        "installDir": str(target),
    }
    write_lock(root, lock)
    print(json.dumps({"success": True, "slug": slug}))
    sys.exit(0)

if cmd == "list":
    root = arg("--dir", "./skills")
    skills = read_lock(root).get("skills", {})
    if not skills:
        print("No installed skills.")
    for key, value in skills.items():
        print(key + "  " + str(value.get("version", "")))
    sys.exit(0)

sys.exit(0)
'''


def _fixture(tmp_path: Path) -> tuple[SkillHubConfig, Path]:
    cli = tmp_path / "fake_skillhub.py"
    cli.write_text(_FAKE_CLI, encoding="utf-8")
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    return SkillHubConfig(cli_path=str(cli)), workspace


def test_end_to_end_install_discover_delete(tmp_path: Path) -> None:
    cfg, workspace = _fixture(tmp_path)

    payload = skill_hub.install_skill("calendar", "alice", workspace=workspace, cfg=cfg)
    assert payload["installed"] is True

    # 1) 落盘遵循 SkillHub 原生命名空间布局（不搬动，保住官方 upgrade/verify）。
    skill_dir = workspace / "skills" / "@alice" / "calendar"
    assert (skill_dir / "SKILL.md").is_file()
    assert "@alice/calendar" in json.loads(
        (workspace / "skills" / ".skills_store_lock.json").read_text(encoding="utf-8")
    )["skills"]

    # 2) 真实加载器能发现它，并以扁平名暴露。
    loader = SkillsLoader(workspace, builtin_skills_dir=tmp_path / "builtin")
    entries = loader.list_skills(filter_unavailable=False)
    assert [(entry["name"], entry["source"]) for entry in entries] == [
        ("alice-calendar", "skillhub")
    ]
    assert "fake skill" in (loader.load_skill("alice-calendar") or "")

    # 3) 商店视图能列出已安装技能。
    assert [row["canonical_name"] for row in skill_hub.installed_skills(workspace=workspace)] == [
        "@alice/calendar"
    ]

    # 4) 删除技能目录后，锁文件条目必须同步消失，否则 skillhub list/upgrade 会
    #    继续把它当作已安装（CLI 没有 uninstall 子命令）。
    assert delete_workspace_skill(workspace, "alice-calendar")["deleted"] is True
    assert not skill_dir.exists()
    assert skill_hub.installed_skills(workspace=workspace) == []
    assert loader.list_skills(filter_unavailable=False) == []


def test_end_to_end_duplicate_install_reports_conflict(tmp_path: Path) -> None:
    cfg, workspace = _fixture(tmp_path)
    skill_hub.install_skill("calendar", "alice", workspace=workspace, cfg=cfg)

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.install_skill("calendar", "alice", workspace=workspace, cfg=cfg)
    assert excinfo.value.status == 409


def test_end_to_end_force_reinstall_succeeds(tmp_path: Path) -> None:
    cfg, workspace = _fixture(tmp_path)
    skill_hub.install_skill("calendar", "alice", workspace=workspace, cfg=cfg)

    payload = skill_hub.install_skill(
        "calendar", "alice", workspace=workspace, cfg=cfg, force=True
    )

    assert payload["installed"] is True


def test_end_to_end_flattened_names_keep_working_after_upgrade_check(tmp_path: Path) -> None:
    """升级检查会重写锁文件；重写后技能仍应可发现、可列出。"""
    cfg, workspace = _fixture(tmp_path)
    skill_hub.install_skill("calendar", "alice", workspace=workspace, cfg=cfg)

    # 假 CLI 不支持 upgrade 子命令，直接退化为空输出，不应破坏既有状态。
    skill_hub.check_updates(workspace=workspace, cfg=cfg)

    assert [row["canonical_name"] for row in skill_hub.installed_skills(workspace=workspace)] == [
        "@alice/calendar"
    ]
