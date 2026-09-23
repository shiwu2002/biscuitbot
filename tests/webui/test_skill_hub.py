"""SkillHub 技能商店后端的单元测试。

全程不联网、不真装技能：``subprocess.run`` 被替换为本地桩，因此测试覆盖到
生产代码里真实的 argv 构造、退出码判定与 JSON 解析路径。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from xianaibot.config.schema import SkillHubConfig
from xianaibot.webui import skill_hub


@pytest.fixture(autouse=True)
def _offline_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认让「商店 HTTP 直连」不可达。

    不这样做的话，所有走 CLI 的既有用例都会先去连真实商店。需要验证直连的用例
    自行 monkeypatch ``skill_hub.httpx.get`` 覆盖本桩。
    """

    def _offline(*_args: Any, **_kwargs: Any) -> Any:
        raise skill_hub.httpx.ConnectError("offline store")

    monkeypatch.setattr(skill_hub.httpx, "get", _offline)


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


def test_status_stays_available_without_cli(tmp_path: Path) -> None:
    """没装 CLI 也仍然可用：浏览/搜索/安装退化为商店 HTTP 直连。"""
    cfg = SkillHubConfig(cli_path=str(tmp_path / "nope" / "skills_store_cli.py"))
    status = skill_hub.skillhub_status(cfg, workspace=tmp_path / "ws")

    assert status["available"] is True
    assert status["enabled"] is True
    assert status["mode"] == "http"
    assert status["cli_available"] is False
    assert status["cli_path"] == ""
    assert status["reason"] == ""
    assert status["skills_dir"] == str(tmp_path / "ws" / "skills")


def test_status_reports_disabled_store(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, enable=False)

    status = skill_hub.skillhub_status(cfg, workspace=tmp_path / "ws")

    assert status["available"] is False
    assert status["reason"]


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


# --------------------------------------------------------------------------- #
# 商店 HTTP 直连（没装 CLI 时的浏览 / 搜索 / 安装通道）
# --------------------------------------------------------------------------- #

_SHOWCASE_ROW: dict[str, Any] = {
    "slug": "dev-expert",
    "name": "编程专家",
    "description_zh": "全栈编程助手",
    "version": "1.21.9",
    "category": "dev-programming",
    "iconUrl": "https://example.invalid/i.png",
    "downloads": 1317277,
    "stars": 12,
    "verified": True,
    "source": "community",
    "namespace": {
        "canonicalName": "@indiv-ebandao/dev-expert",
        "displayName": "智慧半岛",
        "handle": "indiv-ebandao",
        "publicSlug": "dev-expert",
    },
}

_RANKING_SECTIONS = ("featured", "hot", "newest", "paid", "recommended", "trending")


class _FakeResponse:
    """最小 httpx 响应替身（只用到 json / content / status_code）。"""

    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else b""
        self.text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""

    def json(self) -> Any:
        return self._payload


def _stub_store_http(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, Any],
    capture: list[tuple[str, dict[str, Any]]] | None = None,
) -> None:
    """按 URL 后缀匹配返回预置响应；未登记的 URL 直接失败，免得测试偷偷联网。"""

    def fake_get(url: str, *, params: Any = None, **_kwargs: Any) -> _FakeResponse:
        if capture is not None:
            capture.append((url, dict(params or {})))
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(skill_hub.httpx, "get", fake_get)


def _showcase_routes(**overrides: Any) -> dict[str, Any]:
    """六个榜单端点全部登记（默认空），再按需覆盖其中几个。"""
    routes = {f"/api/v1/showcase/{kind}": {"skills": []} for kind in _RANKING_SECTIONS}
    routes.update({f"/api/v1/showcase/{kind}": value for kind, value in overrides.items()})
    return routes


def _no_cli_cfg(tmp_path: Path) -> SkillHubConfig:
    """模拟「装了客户端但没装商店 CLI」：CLI 路径指向不存在的文件。"""
    return SkillHubConfig(cli_path=str(tmp_path / "nope" / "skills_store_cli.py"))


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _staging_leftovers(skills_root: Path) -> list[Path]:
    """解压暂存目录（``.<slug>.<pid>.installing``）不该留在技能根下。"""
    if not skills_root.is_dir():
        return []
    return [item for item in skills_root.iterdir() if item.name.endswith(".installing")]


def test_rankings_via_store_http_without_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 CLI 时排行榜直接读商店公开接口。"""
    capture: list[tuple[str, dict[str, Any]]] = []
    _stub_store_http(
        monkeypatch,
        _showcase_routes(hot={"section": "hot_downloads", "skills": [_SHOWCASE_ROW], "total": 1}),
        capture,
    )

    rows = skill_hub.skill_rankings(cfg=_no_cli_cfg(tmp_path))

    assert capture[0][0] == "https://api.skillhub.cn/api/v1/showcase/hot"
    assert rows == [
        {
            "slug": "dev-expert",
            "canonical_name": "@indiv-ebandao/dev-expert",
            "name": "编程专家",
            "description": "全栈编程助手",
            "version": "1.21.9",
            "category": "dev-programming",
            "icon_url": "https://example.invalid/i.png",
            "homepage": "",
            "handle": "indiv-ebandao",
            "namespace": "智慧半岛",
            "downloads": 1317277,
            "stars": 12,
            "verified": True,
            "source": "community",
        }
    ]


def test_rankings_all_merges_sections_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``all`` 榜单：合并各分节并按技能去重（对齐 CLI 的 ``--type all``）。"""
    other = {
        **_SHOWCASE_ROW,
        "slug": "other",
        "name": "另一个",
        "namespace": {
            **_SHOWCASE_ROW["namespace"],
            "canonicalName": "@x/other",
            "publicSlug": "other",
        },
    }
    _stub_store_http(
        monkeypatch,
        _showcase_routes(
            hot={"skills": [_SHOWCASE_ROW]},
            newest={"skills": [dict(_SHOWCASE_ROW), other]},
        ),
    )

    rows = skill_hub.skill_rankings(cfg=_no_cli_cfg(tmp_path), ranking_type="all")

    assert [row["slug"] for row in rows] == ["dev-expert", "other"]


def test_search_via_store_http_maps_search_row_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """搜索接口的字段名与榜单不同：名字在 ``displayName``，图标在 ``icon_url``。"""
    capture: list[tuple[str, dict[str, Any]]] = []
    _stub_store_http(
        monkeypatch,
        {
            "/api/v1/search": {
                "results": [
                    {
                        "slug": "meeting-notes",
                        "displayName": "会议纪要",
                        "description_zh": "把会议内容整理成结构化纪要",
                        "version": "1.0.0",
                        "icon_url": "https://example.invalid/n.png",
                        "owner_name": "user_2c08c7ce",
                        "source": "community",
                        "namespace": {
                            "canonicalName": "@user_2c08c7ce/meeting-notes",
                            "handle": "user_2c08c7ce",
                        },
                    }
                ]
            }
        },
        capture,
    )

    rows = skill_hub.search_skills("会议", cfg=_no_cli_cfg(tmp_path), limit=7)

    assert capture[0] == (
        "https://api.skillhub.cn/api/v1/search",
        {"q": "会议", "limit": 7},
    )
    assert rows[0]["name"] == "会议纪要"
    assert rows[0]["canonical_name"] == "@user_2c08c7ce/meeting-notes"
    assert rows[0]["icon_url"] == "https://example.invalid/n.png"
    assert rows[0]["handle"] == "user_2c08c7ce"
    assert rows[0]["namespace"] == "user_2c08c7ce"


def test_search_falls_back_to_cli_when_store_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """直连不通时回落到 CLI（``httpx`` 由 autouse 夹具置为离线）。"""
    cfg = _cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_subprocess(
        monkeypatch,
        stdout=json.dumps({"results": [{"slug": "a", "name": "A"}]}),
        capture=calls,
    )

    rows = skill_hub.search_skills("a", cfg=cfg)

    assert [row["slug"] for row in rows] == ["a"]
    assert calls[0][3] == "search"


def test_search_http_failure_without_cli_surfaces_error(tmp_path: Path) -> None:
    """没有 CLI 兜底时，直连失败必须如实报错，不能静默返回空列表。"""
    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.search_skills("a", cfg=_no_cli_cfg(tmp_path))
    assert excinfo.value.status == 502


def test_install_via_store_http_extracts_and_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """直连安装：解压到 ``skills/@handle/slug`` 并登记进 CLI 的锁文件。"""
    workspace = tmp_path / "ws"
    capture: list[tuple[str, dict[str, Any]]] = []
    _stub_store_http(
        monkeypatch,
        {
            "/api/v1/download": _zip_bytes(
                {"SKILL.md": "---\nname: dev-expert\n---\n", "hooks/x.py": "print(1)\n"}
            ),
            "/api/v1/search": {"results": [dict(_SHOWCASE_ROW)]},
        },
        capture,
    )

    payload = skill_hub.install_skill(
        "dev-expert", "indiv-ebandao", workspace=workspace, cfg=_no_cli_cfg(tmp_path)
    )

    target = workspace / "skills" / "@indiv-ebandao" / "dev-expert"
    assert payload["installed"] is True
    assert payload["mode"] == "http"
    assert payload["files"] == 2
    assert (target / "SKILL.md").is_file()
    assert (target / "hooks" / "x.py").is_file()
    # 下载走商店的 primary download 端点，参数与官方 CLI 一致
    assert capture[0] == (
        "https://api.skillhub.cn/api/v1/download",
        {"slug": "dev-expert", "namespace": "indiv-ebandao"},
    )
    lock = json.loads(
        (workspace / "skills" / ".skills_store_lock.json").read_text(encoding="utf-8")
    )
    entry = lock["skills"]["@indiv-ebandao/dev-expert"]
    assert entry["name"] == "编程专家"
    assert entry["version"] == "1.21.9"
    assert entry["installDir"] == str(target)
    # 登记后「已安装」列表能认出它，说明锁文件结构与 CLI 兼容
    assert skill_hub.installed_skills(workspace=workspace)[0]["canonical_name"] == (
        "@indiv-ebandao/dev-expert"
    )
    assert _staging_leftovers(workspace / "skills") == []


def test_install_via_store_http_rejects_zip_slip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """压缩包里带 ``..`` 的成员必须被丢弃，不能写出目标目录之外。"""
    workspace = tmp_path / "ws"
    _stub_store_http(
        monkeypatch,
        {
            "/api/v1/download": _zip_bytes(
                {"../evil.md": "pwned\n", "SKILL.md": "---\nname: x\n---\n"}
            ),
            "/api/v1/search": {"results": []},
        },
    )

    payload = skill_hub.install_skill(
        "dev-expert", "indiv-ebandao", workspace=workspace, cfg=_no_cli_cfg(tmp_path)
    )

    assert payload["files"] == 1
    assert not (workspace / "skills" / "@indiv-ebandao" / "evil.md").exists()
    assert not (workspace / "skills" / "evil.md").exists()


def test_install_via_store_http_conflicts_without_force(tmp_path: Path) -> None:
    """目标目录已存在且没给 force：报 409，且不发起下载。"""
    workspace = tmp_path / "ws"
    (workspace / "skills" / "@indiv-ebandao" / "dev-expert").mkdir(parents=True)

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.install_skill(
            "dev-expert", "indiv-ebandao", workspace=workspace, cfg=_no_cli_cfg(tmp_path)
        )
    assert excinfo.value.status == 409


def test_install_via_store_http_maps_non_zip_body_to_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """slug 不存在时下载端点回 JSON 错误体而非 zip，应报「没有下载包」。"""
    _stub_store_http(monkeypatch, {"/api/v1/download": b'{"message":"skill not found"}'})

    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.install_skill(
            "nope", "indiv-ebandao", workspace=tmp_path / "ws", cfg=_no_cli_cfg(tmp_path)
        )
    assert excinfo.value.status == 404
    assert "下载包" in excinfo.value.message


def test_install_via_store_http_rejects_empty_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空压缩包按失败处理，并且不留半个技能目录。"""
    workspace = tmp_path / "ws"
    _stub_store_http(monkeypatch, {"/api/v1/download": _zip_bytes({})})

    with pytest.raises(skill_hub.SkillHubError):
        skill_hub.install_skill(
            "dev-expert", "indiv-ebandao", workspace=workspace, cfg=_no_cli_cfg(tmp_path)
        )
    assert not (workspace / "skills" / "@indiv-ebandao" / "dev-expert").exists()
    assert _staging_leftovers(workspace / "skills") == []


def test_install_prefers_cli_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装了 CLI 就走官方路径（含签名校验），完全不碰商店直连。"""
    calls: list[list[str]] = []
    _stub_subprocess(monkeypatch, stdout=json.dumps({"success": True}), capture=calls)

    payload = skill_hub.install_skill(
        "calendar", "clawhub_x", workspace=tmp_path / "ws", cfg=_cfg(tmp_path)
    )

    assert payload["mode"] == "cli"
    assert calls[0][3] == "install"


def test_install_disabled_store_raises_403(tmp_path: Path) -> None:
    with pytest.raises(skill_hub.SkillHubError) as excinfo:
        skill_hub.install_skill(
            "calendar",
            "clawhub_x",
            workspace=tmp_path / "ws",
            cfg=_cfg(tmp_path, enable=False),
        )
    assert excinfo.value.status == 403


def test_check_updates_without_cli_returns_empty_summary(tmp_path: Path) -> None:
    """缺 CLI 时升级检查给空结果 + 说明，而不是报错（浏览/安装不受影响）。"""
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / ".skills_store_lock.json").write_text(
        json.dumps({"version": 1, "skills": {"@x/a": {"name": "A"}}}), encoding="utf-8"
    )

    payload = skill_hub.check_updates(workspace=workspace, cfg=_no_cli_cfg(tmp_path))

    assert payload["checked"] == 0
    assert payload["upgradable"] == 0
    assert payload["details"] == []
    assert "CLI" in payload["summary"]


# --------------------------------------------------------------------------- #
# 内置 CLI：解析顺序
# --------------------------------------------------------------------------- #


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把 ``Path.home()`` 挪进临时目录，隔离本机可能存在的 ``~/.skillhub``。"""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_resolve_cli_falls_back_to_bundled_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没装 CLI 时定位到随包内置的副本（顺带看守 vendor 目录没被打包漏掉）。"""
    _isolate_home(monkeypatch, tmp_path)
    cfg = SkillHubConfig()

    resolved = skill_hub.resolve_cli(cfg)

    assert resolved is not None
    path, source = resolved
    assert source == "bundled"
    assert path.name == "skills_store_cli.py"
    assert path.parent.name == "skillhub"
    assert path.is_file()
    # CLI 按固定文件名读版本戳与端点清单，缺了会静默退回内置默认值。
    assert (path.parent / "version.json").is_file()
    assert (path.parent / "metadata.json").is_file()
    assert (path.parent / "skills_upgrade.py").is_file()

    status = skill_hub.skillhub_status(cfg, workspace=tmp_path / "ws")
    assert status["mode"] == "cli"
    assert status["cli_source"] == "bundled"
    assert status["version"]


def test_resolve_cli_prefers_user_install_over_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户按官方文档自装的版本可能比内置副本新，优先用它。"""
    home = _isolate_home(monkeypatch, tmp_path)
    user_cli = home / ".skillhub" / "skills_store_cli.py"
    user_cli.parent.mkdir(parents=True)
    user_cli.write_text("# user cli\n", encoding="utf-8")

    assert skill_hub.resolve_cli(SkillHubConfig()) == (user_cli, "user")


def test_status_reports_configured_cli_source(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    status = skill_hub.skillhub_status(cfg, workspace=tmp_path / "ws")

    assert status["cli_source"] == "config"
    assert status["cli_path"] == cfg.cli_path


def test_configured_cli_path_that_is_missing_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式配置指向不存在的文件：判定不可用，既不用内置副本顶替也不去下载。

    配置写错时应当看得见，而不是被内置副本悄悄接管，或者触发一次网络下载。
    """
    _isolate_home(monkeypatch, tmp_path)
    cfg = SkillHubConfig(cli_path=str(tmp_path / "nope" / "skills_store_cli.py"))
    downloads: list[str] = []
    monkeypatch.setattr(skill_hub, "_download_cli_kit", lambda: downloads.append("called"))

    assert skill_hub.resolve_cli(cfg) is None
    assert skill_hub.ensure_cli_available(cfg) is None
    assert downloads == []
    assert skill_hub.skillhub_status(cfg, workspace=tmp_path / "ws")["mode"] == "http"


# --------------------------------------------------------------------------- #
# 内置 CLI：调用方式
# --------------------------------------------------------------------------- #


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换 ``subprocess.run``，记下 argv 与 env。"""
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        seen["env"] = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(skill_hub.subprocess, "run", fake_run)
    return seen


def test_run_cli_disables_self_upgrade_and_workspace_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑内置副本时必须关掉自我升级与 workspace 技能注入。

    自我升级会往可能只读的安装目录写、并就地换上未审阅的版本；workspace 技能
    注入会把商店自带的 SKILL.md 塞进用户工作区。
    """
    cfg = _cfg(tmp_path)
    seen = _capture_run(monkeypatch)

    skill_hub._run_cli(["skill", "list"], cfg=cfg, cli=Path(cfg.cli_path))

    assert seen["argv"][1] == cfg.cli_path
    assert seen["argv"][2] == "--skip-self-upgrade"
    assert seen["argv"][3:] == ["skill", "list"]
    assert seen["env"]["SKILLHUB_SKIP_SELF_UPGRADE"] == "1"
    assert seen["env"]["SKILLHUB_SKIP_WORKSPACE_SKILLS"] == "1"
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"
    # 子进程环境是在父环境上叠加，不能把 PATH 之类丢掉。
    assert seen["env"].get("PATH") == os.environ.get("PATH")


def test_run_cli_in_frozen_app_uses_bundled_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """桌面端（PyInstaller 冻结）没有独立 python，改用 sidecar 的解释器模式。

    ``sys.executable`` 此时是宿主程序本身，直接拿去跑脚本会失败。
    """
    cfg = _cfg(tmp_path)  # 未指定 python_path
    seen = _capture_run(monkeypatch)
    monkeypatch.setattr(skill_hub.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        skill_hub,
        "bundled_interpreter_prefix",
        lambda: [r"C:\app\sidecar.exe", "__xianaibot_python__"],
    )
    monkeypatch.setattr(skill_hub.shutil, "which", lambda _name: None)

    skill_hub._run_cli(["skill", "list"], cfg=cfg, cli=Path(cfg.cli_path))

    assert seen["argv"][:2] == [r"C:\app\sidecar.exe", "__xianaibot_python__"]
    assert seen["argv"][2] == cfg.cli_path


# --------------------------------------------------------------------------- #
# 内置 CLI：兜底下载
# --------------------------------------------------------------------------- #

_KIT_FILES = {
    "cli/skills_store_cli.py": "# cli\n",
    "cli/skills_upgrade.py": "# upgrade\n",
    "cli/version.json": '{"version": "2026.8.5"}',
    "cli/metadata.json": '{"skills_search_url": "https://example.invalid"}',
}


def _kit_tar_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _hide_bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟「打包漏带了内置副本」——否则仓库里那份永远命中，走不到下载。"""
    monkeypatch.setattr(
        skill_hub, "_bundled_cli_path", lambda: tmp_path / "no-bundle" / "skills_store_cli.py"
    )


def _stub_kit_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]
) -> Path:
    """让兜底下载返回预置工具包，并把 CLI 安装目录挪进临时目录。

    真实实现落在实例数据目录里，测试不能往那儿写。
    """
    install_dir = tmp_path / "data" / "skillhub" / "cli"
    monkeypatch.setattr(skill_hub, "cli_install_dir", lambda: install_dir)
    _hide_bundled_cli(tmp_path, monkeypatch)
    _stub_store_http(monkeypatch, {"/install/latest.tar.gz": _kit_tar_bytes(files)})
    return install_dir


def test_ensure_cli_available_downloads_official_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内置副本缺失时按需下载官方工具包，只取白名单内的四个文件。"""
    _isolate_home(monkeypatch, tmp_path)
    install_dir = _stub_kit_download(
        tmp_path,
        monkeypatch,
        {
            **_KIT_FILES,
            "cli/install.sh": "#!/bin/sh\n",  # 脚本
            "cli/skill/SKILL.md": "# 商店自带技能\n",  # 不该落进用户工作区
            "cli/plugin/index.ts": "export {};\n",  # 编辑器插件
        },
    )

    resolved = skill_hub.ensure_cli_available(SkillHubConfig())

    assert resolved is not None
    path, source = resolved
    assert source == "downloaded"
    assert path == install_dir / "skills_store_cli.py"
    assert sorted(item.name for item in install_dir.iterdir()) == [
        "metadata.json",
        "skills_store_cli.py",
        "skills_upgrade.py",
        "version.json",
    ]
    # 再次解析时走同一个副本，不再重复下载。
    assert skill_hub.resolve_cli(SkillHubConfig()) == (path, "downloaded")


def test_ensure_cli_available_rejects_incomplete_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具包缺文件时不落位（避免半份安装被当成可用 CLI），并优雅退场。"""
    _isolate_home(monkeypatch, tmp_path)
    incomplete = dict(_KIT_FILES)
    incomplete.pop("cli/skills_upgrade.py")
    install_dir = _stub_kit_download(tmp_path, monkeypatch, incomplete)

    assert skill_hub.ensure_cli_available(SkillHubConfig()) is None
    assert not install_dir.exists()
    assert not [item for item in install_dir.parent.iterdir() if item.name.endswith(".installing")]


def test_ensure_cli_available_survives_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载失败不抛错：退化为商店 HTTP 直连（调用方看 None 即可）。"""
    _isolate_home(monkeypatch, tmp_path)
    _hide_bundled_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(skill_hub, "cli_install_dir", lambda: tmp_path / "data" / "cli")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise skill_hub.httpx.ConnectError("offline")

    monkeypatch.setattr(skill_hub.httpx, "get", _boom)

    assert skill_hub.ensure_cli_available(SkillHubConfig()) is None


def test_download_cli_kit_rejects_oversized_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具包异常大时直接拒绝（正常仅 64KB）。"""
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(skill_hub, "cli_install_dir", lambda: tmp_path / "data" / "cli")
    monkeypatch.setattr(skill_hub, "_CLI_KIT_MAX_BYTES", 8)
    _stub_store_http(monkeypatch, {"/install/latest.tar.gz": _kit_tar_bytes(_KIT_FILES)})

    with pytest.raises(skill_hub.SkillHubError):
        skill_hub._download_cli_kit()


def test_install_prefers_downloaded_cli_over_store_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兜底下载成功后，安装应走官方 CLI 路径而不是商店直连。"""
    _isolate_home(monkeypatch, tmp_path)
    _stub_kit_download(tmp_path, monkeypatch, _KIT_FILES)
    cfg = SkillHubConfig()
    calls: list[list[str]] = []
    _stub_subprocess(monkeypatch, stdout='{"success": true}', capture=calls)

    payload = skill_hub.install_skill("calendar", "alice", workspace=tmp_path / "ws", cfg=cfg)

    assert payload["mode"] == "cli"
    argv = calls[0]
    assert argv[argv.index("install") + 1] == "calendar"
    assert argv[argv.index("--namespace") + 1] == "alice"
    # 走的是下载下来的那份副本，不是内置的（内置在这条用例里被模拟成缺失）。
    assert argv[1] == str(skill_hub.cli_install_dir() / "skills_store_cli.py")
