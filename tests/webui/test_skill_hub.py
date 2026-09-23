"""SkillHub 技能商店后端的单元测试。

全程不联网、不真装技能：``subprocess.run`` 被替换为本地桩，因此测试覆盖到
生产代码里真实的 argv 构造、退出码判定与 JSON 解析路径。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xianaibot.config.schema import SkillHubConfig
from xianaibot.webui import skill_hub


def _cfg(tmp_path: Path, **overrides: Any) -> SkillHubConfig:
    """构造指向假 CLI 脚本的配置（脚本只要存在即可，永不被真正执行）。"""
    cli = tmp_path / "skills_store_cli.py"
    cli.write_text("# fake cli\n", encoding="utf-8")
    return SkillHubConfig(cli_path=str(cli), **overrides)


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    capture: list[list[str]] | None = None,
    raises: Exception | None = None,
) -> None:
    """替换 ``skill_hub.subprocess.run``，保留生产代码的错误映射逻辑。"""

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if capture is not None:
            capture.append(list(argv))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(skill_hub.subprocess, "run", fake_run)


# --------------------------------------------------------------------------- #
# 可用性 / 状态
# --------------------------------------------------------------------------- #


def test_status_reports_missing_cli(tmp_path: Path) -> None:
    cfg = SkillHubConfig(cli_path=str(tmp_path / "nope" / "skills_store_cli.py"))
    status = skill_hub.skillhub_status(cfg, workspace=tmp_path / "ws")

    assert status["available"] is False
    assert status["cli_path"] == ""
    assert status["reason"]
    assert status["skills_dir"] == str(tmp_path / "ws" / "skills")


def test_status_reads_version_file(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cli_dir = Path(cfg.cli_path).parent
    (cli_dir / "version.json").write_text('{"version": "2026.8.5"}', encoding="utf-8")

    status = skill_hub.skillhub_status(cfg, workspace=tmp_path / "ws")

    assert status["available"] is True
    assert status["version"] == "2026.8.5"
    assert status["enabled"] is True


def test_require_cli_raises_when_disabled(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, enable=False)
    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.skill_rankings(cfg=cfg)
    assert excinfo.value.status == 403


# --------------------------------------------------------------------------- #
# 搜索 / 排行榜
# --------------------------------------------------------------------------- #


def test_search_normalizes_rows_and_builds_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_subprocess(
        monkeypatch,
        stdout=json.dumps(
            {
                "query": "calendar",
                "count": 1,
                "results": [
                    {
                        "slug": "@clawhub_x/calendar",
                        "publicSlug": "calendar",
                        "name": "Calendar",
                        "description": "Calendar management",
                        "version": "1.0.0",
                        "source": "community",
                        "namespace": {
                            "canonicalName": "@clawhub_x/calendar",
                            "displayName": "x",
                            "handle": "clawhub_x",
                        },
                    }
                ],
                "warnings": [],
            }
        ),
        capture=calls,
    )

    rows = skill_hub.search_skills("calendar", cfg=cfg, limit=5)

    # 选项在 ``--`` 之前，关键词在其后，避免以 - 开头的查询被当成选项。
    assert calls[0][2:] == [
        "--skip-self-upgrade",
        "search",
        "--json",
        "--search-limit",
        "5",
        "--",
        "calendar",
    ]
    assert rows == [
        {
            "slug": "@clawhub_x/calendar",
            "canonical_name": "@clawhub_x/calendar",
            "name": "Calendar",
            "description": "Calendar management",
            "version": "1.0.0",
            "category": "",
            "icon_url": "",
            "homepage": "",
            "handle": "clawhub_x",
            "namespace": "x",
            "downloads": 0,
            "stars": 0,
            "verified": False,
            "source": "community",
        }
    ]


def test_search_rejects_empty_query(tmp_path: Path) -> None:
    with pytest.raises(skill_hub.SkillHubError):
        skill_hub.search_skills("   ", cfg=_cfg(tmp_path))


def test_search_parses_json_amid_progress_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 的进度行可能与 JSON 混排，解析须宽容。"""
    cfg = _cfg(tmp_path)
    _stub_subprocess(
        monkeypatch,
        stdout='Info: fetching\n{"results": [{"slug": "a", "name": "A"}]}\n',
    )

    rows = skill_hub.search_skills("a", cfg=cfg)

    assert [row["slug"] for row in rows] == ["a"]


def test_search_raises_on_non_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _stub_subprocess(monkeypatch, stdout="not json at all")

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.search_skills("a", cfg=cfg)
    assert excinfo.value.status == 502


def test_rankings_prefers_chinese_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_subprocess(
        monkeypatch,
        stdout=json.dumps(
            {
                "section": "hot_downloads",
                "skills": [
                    {
                        "slug": "self-improving-agent",
                        "name": "self-improving agent",
                        "description": "English text",
                        "description_zh": "中文简介",
                        "category": "ai-agent",
                        "iconUrl": "https://example.invalid/i.png",
                        "downloads": 1202645,
                        "stars": 4493,
                        "verified": True,
                        "namespace": {"handle": "clawhub_pskoett", "displayName": "pskoett"},
                    }
                ],
            }
        ),
        capture=calls,
    )

    rows = skill_hub.skill_rankings(cfg=cfg, ranking_type="hot")

    assert calls[0][2:] == ["--skip-self-upgrade", "skill", "rankings", "--type", "hot"]
    assert rows[0]["description"] == "中文简介"
    assert rows[0]["downloads"] == 1202645
    assert rows[0]["verified"] is True
    assert rows[0]["icon_url"] == "https://example.invalid/i.png"


def test_rankings_rejects_unknown_type(tmp_path: Path) -> None:
    with pytest.raises(skill_hub.SkillHubError):
        skill_hub.skill_rankings(cfg=_cfg(tmp_path), ranking_type="bogus")


def test_rankings_all_flattens_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--type all`` 返回的是 {"rankings": {分节: {...}}}，需展平并去重。"""
    cfg = _cfg(tmp_path)
    _stub_subprocess(
        monkeypatch,
        stdout=json.dumps(
            {
                "rankings": {
                    "hot_downloads": {
                        "section": "hot_downloads",
                        "skills": [
                            {"slug": "a", "name": "A"},
                            {"slug": "shared", "name": "Shared"},
                        ],
                    },
                    "newest": {
                        "section": "newest",
                        "skills": [
                            {"slug": "b", "name": "B"},
                            {"slug": "shared", "name": "Shared"},
                        ],
                    },
                }
            }
        ),
    )

    rows = skill_hub.skill_rankings(cfg=cfg, ranking_type="all")

    assert [row["slug"] for row in rows] == ["a", "shared", "b"]


def test_rankings_all_tolerates_empty_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _stub_subprocess(monkeypatch, stdout=json.dumps({"rankings": {}}))

    assert skill_hub.skill_rankings(cfg=cfg, ranking_type="all") == []


def test_rankings_all_reads_paid_section_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """付费分节装技能的键是 ``featured_paid_skills``，不能只认 ``skills``。"""
    cfg = _cfg(tmp_path)
    _stub_subprocess(
        monkeypatch,
        stdout=json.dumps(
            {
                "rankings": {
                    "paid": {
                        "featured_merchants": [{"id": "m1"}],
                        "featured_paid_skills": [{"slug": "premium", "name": "Premium"}],
                    }
                }
            }
        ),
    )

    rows = skill_hub.skill_rankings(cfg=cfg, ranking_type="all")

    assert [row["slug"] for row in rows] == ["premium"]


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #


def test_install_points_dir_at_workspace_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    workspace = tmp_path / "ws"
    calls: list[list[str]] = []
    _stub_subprocess(monkeypatch, stdout="Installed: calendar", capture=calls)

    payload = skill_hub.install_skill("calendar", "clawhub_x", workspace=workspace, cfg=cfg)

    assert calls[0][2:] == [
        "--skip-self-upgrade",
        "install",
        "calendar",
        "--namespace",
        "clawhub_x",
        "--dir",
        str(workspace / "skills"),
        "--json",
    ]
    assert payload["installed"] is True
    assert payload["slug"] == "calendar"
    assert payload["namespace"] == "clawhub_x"


def test_install_appends_force_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_subprocess(monkeypatch, stdout="ok", capture=calls)

    skill_hub.install_skill(
        "calendar", "clawhub_x", workspace=tmp_path / "ws", cfg=cfg, force=True
    )

    assert calls[0][-1] == "--force"


def test_install_maps_existing_target_to_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _stub_subprocess(
        monkeypatch,
        stdout="Error: Target exists: /ws/skills/@x/calendar (use --force to overwrite)",
        returncode=1,
    )

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.install_skill("calendar", "clawhub_x", workspace=tmp_path / "ws", cfg=cfg)
    assert excinfo.value.status == 409


def test_install_rejects_option_like_slug(tmp_path: Path) -> None:
    """以 - 开头的值会被 argparse 当作选项，必须挡住。"""
    with pytest.raises(skill_hub.SkillHubError):
        skill_hub.install_skill("--force", "clawhub_x", workspace=tmp_path / "ws", cfg=_cfg(tmp_path))


def test_install_rejects_path_traversal_namespace(tmp_path: Path) -> None:
    with pytest.raises(skill_hub.SkillHubError):
        skill_hub.install_skill("calendar", "../../evil", workspace=tmp_path / "ws", cfg=_cfg(tmp_path))


def test_install_surfaces_cli_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _cfg(tmp_path)
    _stub_subprocess(monkeypatch, stderr="boom", returncode=2)

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.install_skill("calendar", "clawhub_x", workspace=tmp_path / "ws", cfg=cfg)
    assert excinfo.value.status == 502
    assert "boom" in excinfo.value.message


def test_run_cli_timeout_maps_to_gateway_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    _stub_subprocess(
        monkeypatch, raises=subprocess.TimeoutExpired(cmd="skillhub", timeout=1)
    )

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.skill_rankings(cfg=cfg)
    assert excinfo.value.status == 504


# --------------------------------------------------------------------------- #
# 锁文件
# --------------------------------------------------------------------------- #


def _write_lockfile(skills_root: Path, skills: dict[str, Any]) -> None:
    skills_root.mkdir(parents=True, exist_ok=True)
    (skills_root / ".skills_store_lock.json").write_text(
        json.dumps({"version": 1, "skills": skills}), encoding="utf-8"
    )


def test_installed_skills_reads_lockfile(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    _write_lockfile(
        skills_root,
        {
            "@clawhub_x/calendar": {
                "name": "Calendar",
                "version": "1.0.0",
                "source": "clawhub",
                "internalSlug": "calendar",
                "namespace": {"handle": "clawhub_x", "displayName": "x"},
                "installDir": str(skills_root / "@clawhub_x" / "calendar"),
            }
        },
    )

    rows = skill_hub.installed_skills(workspace=workspace)

    assert rows == [
        {
            "canonical_name": "@clawhub_x/calendar",
            "slug": "calendar",
            "handle": "clawhub_x",
            "name": "Calendar",
            "version": "1.0.0",
            "source": "clawhub",
            "install_dir": str(skills_root / "@clawhub_x" / "calendar"),
        }
    ]


def test_installed_skills_tolerates_missing_lockfile(tmp_path: Path) -> None:
    assert skill_hub.installed_skills(workspace=tmp_path / "ws") == []


def test_drop_lockfile_entry_removes_only_target(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_lockfile(skills_root, {"@x/a": {"version": "1"}, "@x/b": {"version": "2"}})

    assert skill_hub.drop_lockfile_entry(skills_root, "@x/a") is True

    remaining = json.loads((skills_root / ".skills_store_lock.json").read_text(encoding="utf-8"))
    assert remaining["skills"] == {"@x/b": {"version": "2"}}


def test_drop_lockfile_entry_is_idempotent(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_lockfile(skills_root, {"@x/a": {"version": "1"}})

    assert skill_hub.drop_lockfile_entry(skills_root, "@x/a") is True
    assert skill_hub.drop_lockfile_entry(skills_root, "@x/a") is False
    assert skill_hub.drop_lockfile_entry(skills_root, "") is False


def test_drop_lockfile_entry_tolerates_no_lockfile(tmp_path: Path) -> None:
    assert skill_hub.drop_lockfile_entry(tmp_path / "skills", "@x/a") is False


# --------------------------------------------------------------------------- #
# 升级检查 / 签名校验
# --------------------------------------------------------------------------- #


def test_check_updates_short_circuits_without_lockfile(tmp_path: Path) -> None:
    payload = skill_hub.check_updates(workspace=tmp_path / "ws", cfg=_cfg(tmp_path))

    assert payload["checked"] == 0
    assert payload["summary"] == "尚未安装任何商店技能"


def test_check_updates_parses_summary_and_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    _write_lockfile(workspace / "skills", {"@x/a": {"version": "1"}})
    cfg = _cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_subprocess(
        monkeypatch,
        stdout=(
            "[@x/a] skip: config.json not found\n"
            "upgrade done: checked=1 upgraded=0 skipped=1 failed=0 dir=/ws/skills\n"
        ),
        capture=calls,
    )

    payload = skill_hub.check_updates(workspace=workspace, cfg=cfg)

    # 仍须落在正确的技能根上（写锁由 test_check_updates_holds_write_lock 覆盖）。
    assert calls[0][2:] == [
        "--skip-self-upgrade",
        "upgrade",
        "--check-only",
        "--dir",
        str(workspace / "skills"),
    ]
    assert payload["checked"] == 1
    assert payload["upgradable"] == 0
    # 多数社区技能没有 config.json 会被 skip，UI 不应据此宣称「始终最新」。
    assert payload["skipped"] == 1
    assert payload["details"] == ["[@x/a] skip: config.json not found"]


def test_check_updates_holds_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``upgrade --check-only`` 仍会重写整个锁文件，必须与安装/删除串行。

    CLI 的 ``save_lockfile`` 在循环外无条件执行且非原子写；不串行的话，刚被
    ``drop_lockfile_entry`` 摘掉的条目会被用旧内容写回，产生幽灵条目。
    """
    workspace = tmp_path / "ws"
    _write_lockfile(workspace / "skills", {"@x/a": {"version": "1"}})
    observed: list[bool] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append(skill_hub._WRITE_LOCK.locked())
        return subprocess.CompletedProcess(
            argv, 0, stdout="upgrade done: checked=1 upgraded=0 skipped=1 failed=0\n", stderr=""
        )

    monkeypatch.setattr(skill_hub.subprocess, "run", fake_run)
    skill_hub.check_updates(workspace=workspace, cfg=_cfg(tmp_path))

    assert observed == [True]


def test_verify_places_dir_before_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dir`` 是全局选项，必须在子命令之前，否则 CLI 找不到已安装目录。"""
    cfg = _cfg(tmp_path)
    workspace = tmp_path / "ws"
    calls: list[list[str]] = []
    _stub_subprocess(monkeypatch, stdout='{"ok": true}', capture=calls)

    payload = skill_hub.verify_skill("calendar", "clawhub_x", workspace=workspace, cfg=cfg)

    # argv 前缀固定为 [解释器, CLI 脚本, --skip-self-upgrade]。
    args = calls[0][3:]
    assert args[:2] == ["--dir", str(workspace / "skills")]
    assert args[2:4] == ["verify", "calendar"]
    assert args.index("--dir") < args.index("verify")
    assert payload["ok"] is True
    assert payload["exit_code"] == 0


def test_verify_reports_failed_check_as_result_not_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """退出码 2 表示「签名校验不通过」，是业务结果，不能报成 CLI 故障。"""
    cfg = _cfg(tmp_path)
    _stub_subprocess(
        monkeypatch,
        stdout=json.dumps({"ok": False, "reason": "signature mismatch"}),
        returncode=2,
    )

    payload = skill_hub.verify_skill(
        "calendar", "clawhub_x", workspace=tmp_path / "ws", cfg=cfg
    )

    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["reason"] == "signature mismatch"


def test_verify_raises_when_no_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只有拿不到任何输出时才算调用失败。"""
    cfg = _cfg(tmp_path)
    _stub_subprocess(monkeypatch, stderr="cli exploded", returncode=1)

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.verify_skill("calendar", "clawhub_x", workspace=tmp_path / "ws", cfg=cfg)
    assert excinfo.value.status == 502


def test_verify_requires_slug(tmp_path: Path) -> None:
    with pytest.raises(skill_hub.SkillHubError):
        skill_hub.verify_skill("", "clawhub_x", workspace=tmp_path / "ws", cfg=_cfg(tmp_path))
