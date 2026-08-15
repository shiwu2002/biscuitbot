"""EmployeeStore 内置员工种子与一次性补全合并迁移的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from biscuitbot.agent.employees import EmployeeStore, EmployeeValidationError

BUILTIN_IDS = {
    "clip-master",
    "ip-consultant",
    "short-video-operator",
    "super-secretary",
    "all-round-designer",
    "screenwriter",
}


def _store(tmp_path: Path) -> EmployeeStore:
    return EmployeeStore(tmp_path / "ws")


def _write_old_file(store: EmployeeStore, employees: list[dict]) -> None:
    """写入一个不带 builtin_seeded 标记的老版本员工文件。"""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"schema_version": 1, "employees": employees}, ensure_ascii=False),
        encoding="utf-8",
    )


class TestBuiltinSeed:
    def test_fresh_store_seeds_all_builtins(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        employees = store.list_employees()
        assert {e["id"] for e in employees} == BUILTIN_IDS

    def test_seed_writes_marker_and_schema(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.list_employees()
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["builtin_seeded"] is True
        assert raw["schema_version"] == 1

    def test_each_builtin_has_full_persona_and_avatar(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for emp in store.list_employees():
            assert emp["name"]
            assert emp["avatar"]
            assert len(emp["system_prompt"]) >= 100, emp["id"]
            assert emp["enabled"] is True

    def test_video_roles_bind_expected_skills(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        by_id = {e["id"]: e for e in store.list_employees()}
        assert by_id["clip-master"]["skills"] == ["jianying-editor"]
        assert by_id["short-video-operator"]["skills"] == [
            "seedance",
            "jianying-editor",
        ]
        assert by_id["ip-consultant"]["skills"] == ["ip-positioning"]
        assert by_id["super-secretary"]["skills"] == ["secretary"]
        assert by_id["all-round-designer"]["skills"] == ["design"]

    def test_builtin_personas_refine_before_executing(self, tmp_path: Path) -> None:
        """内置员工 persona 应「主动执行/产出/创作」，而非先反问用户。"""
        store = _store(tmp_path)
        for emp in store.list_employees():
            persona = emp["system_prompt"]
            # 每个员工都应主动产出成果（执行/产出/创造），而非只解释概念
            assert any(kw in persona for kw in ("执行", "产出", "创造")), emp["id"]
        # 旧版「先确认/先追问」类反问措辞不得再出现
        for forbid in ("先提出几个关键问题", "主动追问", "主动确认"):
            for emp in store.list_employees():
                assert forbid not in emp["system_prompt"], (forbid, emp["id"])


class TestOneTimeMerge:
    def test_old_file_without_marker_merges_missing_builtins(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        _write_old_file(
            store,
            [
                {
                    "id": "clip-master",
                    "name": "剪辑高手",
                    "system_prompt": "旧的自定义提示词",
                    "enabled": True,
                }
            ],
        )
        employees = store.list_employees()
        assert {e["id"] for e in employees} == BUILTIN_IDS

    def test_merge_preserves_custom_employees(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_old_file(
            store,
            [
                {
                    "id": "my-custom",
                    "name": "我的专属助理",
                    "system_prompt": "你好，我是自定义员工。",
                    "enabled": True,
                }
            ],
        )
        employees = store.list_employees()
        by_id = {e["id"]: e for e in employees}
        assert by_id["my-custom"]["name"] == "我的专属助理"
        assert {e["id"] for e in employees} == BUILTIN_IDS | {"my-custom"}

    def test_marker_prevents_readding_deleted_builtin(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # 首次加载：种子 + 写标记
        store.list_employees()
        # 用户删除一个内置员工
        store.delete_employee("short-video-operator")
        # 再次加载：标记已存在，不应找回被删的内置员工
        employees = store.list_employees()
        assert "short-video-operator" not in {e["id"] for e in employees}

    def test_old_file_builtin_synced_to_current_version(
        self, tmp_path: Path
    ) -> None:
        # 老 v1 文件：内置记录会被同步为当前版本的代号/职位/提示词
        store = _store(tmp_path)
        _write_old_file(
            store,
            [
                {
                    "id": "clip-master",
                    "name": "剪辑高手",
                    "system_prompt": "旧的剪辑提示词",
                    "enabled": True,
                }
            ],
        )
        employees = store.list_employees()
        clip = next(e for e in employees if e["id"] == "clip-master")
        assert clip["name"] == "阿伟"
        assert clip["title"] == "AI视频剪辑总监"
        assert "阿伟" in clip["system_prompt"]

    def test_builtin_employee_cannot_be_updated(self, tmp_path: Path) -> None:
        # 内置数字人员工不可修改（只能删除），修改返回 403
        store = _store(tmp_path)
        store.list_employees()  # 触发 seed
        with pytest.raises(EmployeeValidationError) as exc:
            store.update_employee("clip-master", {"system_prompt": "自定义提示词"})
        assert exc.value.status == 403
        # 内置记录未被改动
        clip = store.get_employee("clip-master")
        assert clip is not None and clip["name"] == "阿伟"

    def test_marker_is_set_after_merge(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _write_old_file(
            store,
            [
                {
                    "id": "clip-master",
                    "name": "剪辑高手",
                    "system_prompt": "x",
                    "enabled": True,
                }
            ],
        )
        store.list_employees()
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["builtin_seeded"] is True


class TestCrudAgainstBuiltins:
    def test_create_keeps_builtins(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create_employee({"name": "新员工", "system_prompt": "你是我的专属助理。"})
        ids = {e["id"] for e in store.list_employees()}
        assert BUILTIN_IDS <= ids

    def test_update_builtin_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(EmployeeValidationError) as exc:
            store.update_employee("ip-consultant", {"name": "IP 顾问"})
        assert exc.value.status == 403

    def test_builtin_flag_marked(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        by_id = {e["id"]: e for e in store.list_employees()}
        assert by_id["clip-master"]["builtin"] is True
        emp = store.create_employee(
            {"name": "专属助理", "system_prompt": "我是你的专属助理。"}
        )
        assert emp["builtin"] is False

    def test_delete_and_recreate_builtin(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.delete_employee("super-secretary")
        assert store.get_employee("super-secretary") is None
        # 重新创建同名 id 也不应与迁移冲突
        store.create_employee(
            {
                "id": "super-secretary",
                "name": "超级秘书",
                "system_prompt": "重新创建",
            }
        )
        assert store.get_employee("super-secretary") is not None

    def test_duplicate_id_still_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(EmployeeValidationError) as exc:
            store.create_employee({"id": "clip-master", "name": "重复"})
        assert exc.value.status == 409


class TestTitleField:
    def test_seed_records_have_codenames_and_titles(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        by_id = {e["id"]: e for e in store.list_employees()}
        assert by_id["clip-master"]["name"] == "阿伟"
        assert by_id["clip-master"]["title"] == "AI视频剪辑总监"
        assert by_id["short-video-operator"]["title"] == "短视频增长操盘手"
        assert by_id["super-secretary"]["title"] == "AI执行秘书"
        assert by_id["all-round-designer"]["title"] == "AI视觉设计总监"
        assert by_id["screenwriter"]["title"] == "编剧"

    def test_missing_title_defaults_to_empty(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        emp = store.create_employee(
            {"name": "新员工", "system_prompt": "你的职责。", "skills": []}
        )
        assert emp["title"] == ""

    def test_create_and_update_title_roundtrip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        emp = store.create_employee(
            {"name": "新员工", "title": "运营", "system_prompt": "你的职责。"}
        )
        assert emp["title"] == "运营"
        updated = store.update_employee(emp["id"], {"title": "增长"})
        assert updated["title"] == "增长"


class TestBuiltinVersionMigration:
    def test_seed_writes_current_version(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.list_employees()
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["builtin_version"] == 8

    def test_v1_file_syncs_builtin_names_and_keeps_custom(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        _write_old_file(
            store,
            [
                {
                    "id": "clip-master",
                    "name": "剪辑高手",
                    "system_prompt": "旧提示词",
                    "enabled": True,
                },
                {
                    "id": "my-custom",
                    "name": "我的助理",
                    "system_prompt": "自定义提示词",
                    "enabled": True,
                },
            ],
        )
        employees = store.list_employees()
        by_id = {e["id"]: e for e in employees}
        assert by_id["clip-master"]["name"] == "阿伟"
        assert by_id["clip-master"]["title"] == "AI视频剪辑总监"
        assert by_id["my-custom"]["name"] == "我的助理"
        assert by_id["my-custom"]["system_prompt"] == "自定义提示词"
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["builtin_version"] == 8

    def test_v1_sync_updates_builtin_skills(self, tmp_path: Path) -> None:
        # 老 v1 文件：内置记录的空技能会被同步为新版绑定的技能
        store = _store(tmp_path)
        _write_old_file(
            store,
            [
                {
                    "id": "ip-consultant",
                    "name": "探微",
                    "system_prompt": "旧的访谈提示词",
                    "enabled": True,
                },
                {
                    "id": "super-secretary",
                    "name": "得力",
                    "system_prompt": "旧的秘书提示词",
                    "enabled": True,
                },
                {
                    "id": "all-round-designer",
                    "name": "绘野",
                    "system_prompt": "旧的设计提示词",
                    "enabled": True,
                },
            ],
        )
        employees = store.list_employees()
        by_id = {e["id"]: e for e in employees}
        assert by_id["ip-consultant"]["skills"] == ["ip-positioning"]
        assert by_id["super-secretary"]["skills"] == ["secretary"]
        assert by_id["all-round-designer"]["skills"] == ["design"]

    def test_deleted_builtin_stays_deleted_at_current_version(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # 先建当前版本种子，再删除一个内置员工：版本未落后时不并入已删员工
        store.list_employees()
        store.delete_employee("short-video-operator")
        employees = store.list_employees()
        assert "short-video-operator" not in {e["id"] for e in employees}

    def test_stale_version_merges_missing_builtins(self, tmp_path: Path) -> None:
        """版本落后时，缺失的内置员工会被并入（含本次升级新增的）。

        方案1 的既定权衡：内置目录版本落后即触发并入，因此版本被降级时
        用户删除过的内置员工也会被找回。
        """
        store = _store(tmp_path)
        store.list_employees()
        store.delete_employee("short-video-operator")
        # 模拟内置版本降级（文件里直接改旧版本），加载后并入缺失内置员工
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["builtin_version"] = 1
        store.path.write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
        employees = store.list_employees()
        assert "short-video-operator" in {e["id"] for e in employees}

    def test_sync_preserves_user_edits_when_version_current(
        self, tmp_path: Path
    ) -> None:
        # 版本已是最新时，自建员工的编辑不会被同步覆盖
        store = _store(tmp_path)
        store.list_employees()
        emp = store.create_employee(
            {"name": "专属助理", "title": "助理", "system_prompt": "我是你的专属助理。"}
        )
        store.update_employee(emp["id"], {"name": "专属助理改", "title": "助理改"})
        by_id = {e["id"]: e for e in store.list_employees()}
        assert by_id[emp["id"]]["name"] == "专属助理改"
        assert by_id[emp["id"]]["title"] == "助理改"
