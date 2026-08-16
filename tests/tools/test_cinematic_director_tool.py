"""Tests for the cinematic_director tool (AI 导演 9 阶段状态机 + 三级审核 Gate + 分目录记忆)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from biscuitbot.agent.tools._cinematic import workflow
from biscuitbot.agent.tools._cinematic.state import ProjectStore
from biscuitbot.agent.tools._cinematic.validators import CinematicDirectorError
from biscuitbot.agent.tools.cinematic_director import CinematicDirectorTool


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path)


def _act(store: ProjectStore, action: str, **kwargs) -> dict:
    """同步分派到 workflow 函数（直接返回 dict 或抛 CinematicDirectorError）。"""
    return getattr(workflow, action)(store, kwargs)


def _create_project(store: ProjectStore, project_id: str = "demo") -> dict:
    return _act(store, "create_project", project_id=project_id, name="雪林逃亡",
                logline="一家三口在暴雪中逃亡求生", style="realistic movie scene",
                ratio="16:9", fps=24, color_palette="冷色月光，低饱和")


def _write_script(store: ProjectStore, project_id: str = "demo") -> dict:
    return _act(store, "write_script", project_id=project_id,
                scenes=[{"id": "Scene001", "location": "雪林", "time": "黄昏",
                         "function": "建立危机感", "duration": 10}],
                shots=[{"id": "Shot001", "scene_id": "Scene001", "shot_size": "大全景",
                        "camera": "无人机下降", "lens": "24mm", "action": "一家三口逃亡",
                        "emotion": "恐惧", "sound": "风雪声",
                        "asset_refs": ["CHAR001", "CHAR002", "LOC001"]}])


def _review_script(store: ProjectStore, project_id: str = "demo", score: int = 90) -> dict:
    return _act(store, "review_script", project_id=project_id, score=score)


def _add_char(store: ProjectStore, project_id: str = "demo", asset_id: str = "CHAR001") -> dict:
    return _act(store, "add_asset", project_id=project_id, kind="CHAR", id=asset_id,
                name="爸爸", appearance="45 岁男人，黑色羽绒服，胡茬严肃",
                reference_image=f"assets/{asset_id}.png")


def _add_loc(store: ProjectStore, project_id: str = "demo", asset_id: str = "LOC001") -> dict:
    return _act(store, "add_asset", project_id=project_id, kind="LOC", id=asset_id,
                name="雪林", appearance="黄昏暴雪的雪林，冷色月光",
                reference_image=f"assets/{asset_id}.png")


def _review_assets(store: ProjectStore, project_id: str = "demo", score: int = 90) -> dict:
    return _act(store, "review_assets", project_id=project_id, score=score)


def _lock_assets(store: ProjectStore, project_id: str = "demo") -> dict:
    return _act(store, "lock_assets", project_id=project_id)


def _setup_asset_lock(store: ProjectStore, project_id: str = "demo") -> None:
    """推进到 asset_lock（lock_assets 之前）。"""
    _create_project(store, project_id)
    _write_script(store, project_id)
    _review_script(store, project_id)
    _add_loc(store, project_id, "LOC001")
    _add_char(store, project_id, "CHAR001")
    _add_char(store, project_id, "CHAR002")
    _review_assets(store, project_id)


def _setup_locked(store: ProjectStore, project_id: str = "demo") -> dict:
    """推进到 storyboard（lock_assets 之后）。"""
    _setup_asset_lock(store, project_id)
    return _lock_assets(store, project_id)


# ---------------------------------------------------------------------------
# 项目创建与分目录记忆
# ---------------------------------------------------------------------------


def test_create_project_persists_and_directory_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = _create_project(store)
    assert result["stage"] == "init"
    root = tmp_path / "cinematic" / "demo"
    assert (root / "project.json").exists()
    assert (root / "bible.json").exists()
    bible = json.loads((root / "bible.json").read_text(encoding="utf-8"))
    assert bible["style"] == "realistic movie scene"


def test_create_project_rejects_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    with pytest.raises(CinematicDirectorError, match="already exists"):
        _create_project(store)


def test_write_script_validates_shot_scene_ref(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    with pytest.raises(CinematicDirectorError, match="unknown scene_id"):
        _act(store, "write_script", project_id="demo",
             scenes=[{"id": "Scene001"}],
             shots=[{"id": "Shot001", "scene_id": "SceneX", "asset_refs": ["CHAR001", "LOC001"]}])


def test_write_script_requires_char_and_loc_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    with pytest.raises(CinematicDirectorError, match="LOC"):
        _act(store, "write_script", project_id="demo",
             scenes=[{"id": "Scene001"}],
             shots=[{"id": "Shot001", "scene_id": "Scene001", "asset_refs": ["CHAR001"]}])


# ---------------------------------------------------------------------------
# 9 阶段硬门 + 剧本审核 Gate
# ---------------------------------------------------------------------------


def test_add_asset_rejected_before_script_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _add_loc(store)


def test_review_script_fail_blocks_world_building(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    result = _review_script(store, score=50)
    assert result["passed"] is False
    assert result["stage"] == "script_analysis"
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _add_loc(store)


def test_add_loc_auto_advances_to_character_design(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    result = _add_loc(store)
    assert result["stage"] == "character_design"


def test_char_asset_rejected_in_world_building(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _add_char(store)


def test_review_script_freezes_script(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _write_script(store)


def test_review_records_reviewer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _act(store, "review_script", project_id="demo", score=90, reviewer="宫本")
    data = store.load("demo")
    assert data["reviews"]["script_review"]["reviewer"] == "宫本"


# ---------------------------------------------------------------------------
# 资产硬门 + 资产审核 Gate
# ---------------------------------------------------------------------------


def test_add_asset_requires_reference_image(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    with pytest.raises(CinematicDirectorError, match="reference_image"):
        _act(store, "add_asset", project_id="demo", kind="LOC", id="LOC001",
             name="雪林", appearance="黄昏暴雪的雪林")


def test_add_asset_rejects_bad_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    with pytest.raises(CinematicDirectorError, match="invalid asset id"):
        _act(store, "add_asset", project_id="demo", kind="LOC", id="LOC1",
             name="x", appearance="x", reference_image="x.png")


def test_add_asset_rejects_kind_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    with pytest.raises(CinematicDirectorError, match="must start with kind"):
        _act(store, "add_asset", project_id="demo", kind="LOC", id="CHAR001",
             name="x", appearance="x", reference_image="x.png")


def test_review_assets_requires_all_chars(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    _add_loc(store)
    _add_char(store, asset_id="CHAR001")  # CHAR002 缺失
    with pytest.raises(CinematicDirectorError, match="missing character assets"):
        _review_assets(store)


def test_lock_assets_rejects_missing_reference_image(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _setup_asset_lock(store)
    data = store.load("demo")
    data["assets"]["CHAR001"]["reference_image"] = ""
    store.save("demo", data)
    with pytest.raises(CinematicDirectorError, match="without reference_image"):
        _lock_assets(store)


def test_lock_assets_success(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = _setup_locked(store)
    assert result["stage"] == "storyboard"
    assert set(result["assets"]) == {"CHAR001", "CHAR002", "LOC001"}


def test_lock_freezes_add_asset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _setup_locked(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _add_char(store, asset_id="CHAR003")


# ---------------------------------------------------------------------------
# 分镜 + Prompt 编译硬门
# ---------------------------------------------------------------------------


def test_compile_prompt_rejected_before_storyboard(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")


def test_compile_prompt_anchors_and_urls(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _setup_locked(store)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001",
         movement="handheld tracking shot", fps=24, lighting="cold cinematic lighting")
    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")

    assert "@CHAR001" in result["prompt"]
    assert "@CHAR002" in result["prompt"]
    assert "@LOC001" in result["prompt"]
    assert "handheld tracking shot" in result["prompt"]
    assert "realistic movie scene" in result["prompt"]
    assert result["image_urls"] == ["assets/CHAR001.png", "assets/CHAR002.png", "assets/LOC001.png"]
    assert result["stage"] == "quality_check"


# ---------------------------------------------------------------------------
# 视频审核 Gate 硬阻塞
# ---------------------------------------------------------------------------


def test_record_qc_fail_blocks_shot_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _setup_locked(store)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")

    qc = _act(store, "record_qc", project_id="demo", shot_id="Shot001",
              character_score=95, scene_score=90, action_score=80)  # 低于阈值 85
    assert qc["passed"] is False
    assert qc["below_threshold"] == ["action"]
    assert qc["stage"] == "quality_check"

    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _act(store, "record_shot_result", project_id="demo", shot_id="Shot001",
             video_path="out/shot001.mp4")


def test_full_happy_path_to_completed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _setup_locked(store)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    _act(store, "record_qc", project_id="demo", shot_id="Shot001",
         character_score=95, scene_score=90, action_score=88)
    result = _act(store, "record_shot_result", project_id="demo", shot_id="Shot001",
                  video_path="out/shot001.mp4")
    assert result["completed"] is True
    assert result["stage"] == "final_edit"


def test_directory_memory_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _setup_locked(store)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    _act(store, "record_qc", project_id="demo", shot_id="Shot001",
         character_score=95, scene_score=90, action_score=88)

    root = tmp_path / "cinematic" / "demo"
    assert (root / "project.json").exists()
    assert (root / "bible.json").exists()
    assert (root / "characters" / "CHAR001.json").exists()
    assert (root / "characters" / "CHAR002.json").exists()
    assert (root / "locations" / "LOC001.json").exists()
    assert (root / "shots" / "Shot001.json").exists()
    assert (root / "reviews" / "script_review.json").exists()
    assert (root / "reviews" / "asset_review.json").exists()
    assert (root / "reviews" / "shot_Shot001_review.json").exists()


# ---------------------------------------------------------------------------
# 状态查询 + 权限矩阵 + 路径安全
# ---------------------------------------------------------------------------


def test_status_reports_next_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    status = _act(store, "status", project_id="demo")
    assert status["stage"] == "init"
    assert status["next_action"] == "write_script"


def test_role_permission_matrix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    with pytest.raises(CinematicDirectorError, match="not allowed to perform"):
        _act(store, "add_asset", role="video", project_id="demo", kind="LOC", id="LOC001",
             name="雪林", appearance="x", reference_image="x.png")


def test_project_id_path_traversal_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CinematicDirectorError, match="invalid project_id"):
        _create_project(store, project_id="../evil")


def test_project_id_absolute_path_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CinematicDirectorError, match="invalid project_id"):
        _act(store, "status", project_id="/etc/passwd")


# ---------------------------------------------------------------------------
# execute 层（门面 JSON 序列化）
# ---------------------------------------------------------------------------


def test_execute_returns_json_error_on_unknown_action(tmp_path: Path) -> None:
    tool = CinematicDirectorTool(workspace=tmp_path)
    raw = asyncio.run(tool.execute(action="bogus"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "unknown action" in payload["error"]


def test_execute_returns_json_success(tmp_path: Path) -> None:
    tool = CinematicDirectorTool(workspace=tmp_path)
    raw = asyncio.run(tool.execute(
        action="create_project",
        project_id="demo",
        name="雪林逃亡",
        logline="一家三口在暴雪中逃亡求生",
        style="realistic movie scene",
    ))
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["stage"] == "init"


def test_execute_returns_json_error_on_gate_rejection(tmp_path: Path) -> None:
    tool = CinematicDirectorTool(workspace=tmp_path)
    asyncio.run(tool.execute(
        action="create_project",
        project_id="demo",
        name="雪林逃亡",
        logline="一家三口在暴雪中逃亡求生",
        style="realistic movie scene",
    ))
    raw = asyncio.run(tool.execute(action="add_asset", project_id="demo",
                                   kind="LOC", id="LOC001", name="雪林",
                                   appearance="x", reference_image="x.png"))
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "not allowed at stage" in payload["error"]
