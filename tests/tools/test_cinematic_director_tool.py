"""Tests for the cinematic_director tool (AI 导演 11 阶段状态机 + 审核 Gate + story + 分目录记忆)."""

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


def _write_script(store: ProjectStore, project_id: str = "demo", story: dict | None = None) -> dict:
    return _act(store, "write_script", project_id=project_id,
                scenes=[{"id": "Scene001", "location": "雪林", "time": "黄昏",
                         "function": "建立危机感", "duration": 10}],
                shots=[{"id": "Shot001", "scene_id": "Scene001", "shot_size": "大全景",
                        "camera": "无人机下降", "lens": "24mm", "action": "一家三口逃亡",
                        "emotion": "恐惧", "sound": "风雪声",
                        "asset_refs": ["CHAR001", "CHAR002", "LOC001"],
                        "spatial": {
                            "camera": {"position": {"x": 30, "y": 20}, "target": {"x": 50, "y": 40}},
                            "characters": [
                                {"asset_id": "CHAR001", "position": {"x": 50, "y": 40}, "facing": 90},
                                {"asset_id": "CHAR002", "position": {"x": 45, "y": 36}, "facing": 0},
                            ],
                        }}],
                story=story)


def _story() -> dict:
    """剧情补充层样例（整体剧情 + 人物/环境描述词 + 空间/大局描述词）。"""
    return {
        "plot": "一家三口在暴雪中逃亡求生",
        "characters": [{"id": "CHAR001", "name": "爸爸", "prompt": "45 岁男人，黑色羽绒服"}],
        "environments": [{"id": "LOC001", "name": "雪林", "prompt": "黄昏暴雪的雪林"}],
        "spatial": "雪林在平面图东北部，人物从东侧入画向西逃亡",
        "worldview": "末日废土，冷色主调",
    }


def _review_script(store: ProjectStore, project_id: str = "demo", score: int = 90) -> dict:
    return _act(store, "review_script", project_id=project_id, score=score)


def _add_char(store: ProjectStore, project_id: str = "demo", asset_id: str = "CHAR001") -> dict:
    return _act(store, "add_asset", project_id=project_id, kind="CHAR", id=asset_id,
                name="爸爸", appearance="45 岁男人，黑色羽绒服，胡茬严肃",
                reference_image=f"assets/{asset_id}.png",
                three_view=True, voice=f"assets/{asset_id}_voice.mp3")


def _add_loc(store: ProjectStore, project_id: str = "demo", asset_id: str = "LOC001") -> dict:
    return _act(store, "add_asset", project_id=project_id, kind="LOC", id=asset_id,
                name="雪林", appearance="黄昏暴雪的雪林，冷色月光",
                reference_image=f"assets/{asset_id}.png",
                position={"x": 50, "y": 40}, footprint={"width": 20, "depth": 16})


def _review_assets(store: ProjectStore, project_id: str = "demo", score: int = 90) -> dict:
    return _act(store, "review_assets", project_id=project_id, score=score)


def _lock_assets(store: ProjectStore, project_id: str = "demo") -> dict:
    return _act(store, "lock_assets", project_id=project_id)


def _setup_asset_lock(store: ProjectStore, project_id: str = "demo") -> None:
    """推进到 asset_lock（lock_assets 之前）。"""
    _create_project(store, project_id)
    _write_script(store, project_id)
    _review_script(store, project_id)
    _act(store, "set_floorplan", project_id=project_id, width=100, length=80)
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
    _act(store, "set_floorplan", project_id="demo", width=100, length=80)
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
    _act(store, "set_floorplan", project_id="demo", width=100, length=80)
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

    with pytest.raises(CinematicDirectorError, match="QC not passed"):
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


# ---------------------------------------------------------------------------
# 空间资产能力（平面图 + 坐标 + 空间一致性硬门）
# ---------------------------------------------------------------------------


def _setup_spatial_locked(store: ProjectStore, project_id: str = "demo", *, loc2: bool = False) -> dict:
    """推进到 storyboard，带平面图 + LOC 坐标 + 镜头空间站位。"""
    _create_project(store, project_id)
    refs = ["CHAR001", "CHAR002", "LOC001"] + (["LOC002"] if loc2 else [])
    shots = [{
        "id": "Shot001", "scene_id": "Scene001", "shot_size": "大全景",
        "action": "逃亡", "emotion": "恐惧", "sound": "风雪声",
        "asset_refs": refs,
        "spatial": {
            "camera": {"position": {"x": 30, "y": 20}, "target": {"x": 50, "y": 40}},
            "characters": [
                {"asset_id": "CHAR001", "position": {"x": 50, "y": 40}, "facing": 90},
                {"asset_id": "CHAR002", "position": {"x": 45, "y": 36}, "facing": 0},
            ],
        },
    }]
    _act(store, "write_script", project_id=project_id,
         scenes=[{"id": "Scene001", "location": "雪林", "time": "黄昏",
                  "function": "建立危机感", "duration": 10}],
         shots=shots)
    _review_script(store, project_id)
    _act(store, "set_floorplan", project_id=project_id, width=100, length=80)
    _act(store, "add_asset", project_id=project_id, kind="LOC", id="LOC001", name="雪林",
         appearance="黄昏暴雪的雪林", reference_image="assets/LOC001.png",
         position={"x": 50, "y": 40}, footprint={"width": 20, "depth": 16})
    if loc2:
        _act(store, "add_asset", project_id=project_id, kind="LOC", id="LOC002", name="木屋",
             appearance="木屋", reference_image="assets/LOC002.png",
             position={"x": 55, "y": 42}, footprint={"width": 10, "depth": 10})
    _add_char(store, project_id, "CHAR001")
    _add_char(store, project_id, "CHAR002")
    _review_assets(store, project_id)
    return _lock_assets(store, project_id)


def test_set_floorplan_persists_map_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    result = _act(store, "set_floorplan", project_id="demo", width=100, length=80)
    assert result["floorplan"]["width"] == 100
    assert result["floorplan"]["unit"] == "meter"
    assert result["stage"] == "world_building"  # 空间规划完成即放行资产生成
    data = store.load("demo")
    assert data["floorplan"]["length"] == 80
    assert (tmp_path / "cinematic" / "demo" / "map.json").exists()


def test_set_floorplan_rejects_missing_dims(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    with pytest.raises(CinematicDirectorError, match="width"):
        _act(store, "set_floorplan", project_id="demo", width=0, length=80)


def test_set_floorplan_rejected_before_spatial_planning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)  # 仍在 script_analysis
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _act(store, "set_floorplan", project_id="demo", width=100, length=80)


def test_add_loc_requires_position(tmp_path: Path) -> None:
    """空间规划强制后，LOC 资产必须带坐标。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)
    _act(store, "set_floorplan", project_id="demo", width=100, length=80)
    with pytest.raises(CinematicDirectorError, match="position"):
        _act(store, "add_asset", project_id="demo", kind="LOC", id="LOC001",
             name="雪林", appearance="黄昏暴雪的雪林", reference_image="assets/LOC001.png")


def test_compile_prompt_spatial_hard_gate_character_out_of_loc(tmp_path: Path) -> None:
    """人物站位越出引用 LOC 占地 → compile_prompt 硬门拒绝。"""
    store = _store(tmp_path)
    _setup_spatial_locked(store)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    # 篡改 CHAR001 站位到 LOC001 占地之外
    data = store.load("demo")
    data["shots"][0]["spatial"]["characters"][0]["position"] = {"x": 5, "y": 5}
    store.save("demo", data)
    with pytest.raises(CinematicDirectorError, match="spatial inconsistency"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")


def test_compile_prompt_rejects_overlapping_locs(tmp_path: Path) -> None:
    """两个 LOC 占地重叠 → compile_prompt 硬门拒绝（建筑不叠放）。"""
    store = _store(tmp_path)
    _setup_spatial_locked(store, loc2=True)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    with pytest.raises(CinematicDirectorError, match="overlap"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")


def test_compile_prompt_rejects_motion_wall_cross(tmp_path: Path) -> None:
    """人物 motion_to 直线穿过另一 LOC 占地 → 穿墙拒绝。"""
    store = _store(tmp_path)
    _setup_spatial_locked(store, loc2=True)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    # 重新布局，避免「建筑不叠放」与「人物越界」干扰，仅触发穿墙
    data = store.load("demo")
    data["assets"]["LOC001"]["position"] = {"x": 20, "y": 40}   # AABB [10,30]x[32,48]
    data["assets"]["LOC002"]["position"] = {"x": 80, "y": 10}   # AABB [75,85]x[5,15]
    chars = data["shots"][0]["spatial"]["characters"]
    chars[0]["position"] = {"x": 20, "y": 40}   # CHAR001 在 LOC001 内
    chars[0]["motion_to"] = {"x": 90, "y": 10}  # 直线穿过 LOC002 占地
    chars[1]["position"] = {"x": 20, "y": 36}   # CHAR002 也在 LOC001 内
    store.save("demo", data)
    with pytest.raises(CinematicDirectorError, match="motion path crosses"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")


def test_compile_prompt_spatial_phrase(tmp_path: Path) -> None:
    """合法空间布局 → prompt 注入坐标与方位短语。"""
    store = _store(tmp_path)
    _setup_spatial_locked(store)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    prompt = result["prompt"]
    assert "位于平面图 (50,40)" in prompt
    assert "西南角" in prompt
    assert "面向东" in prompt
    assert "机位在 @LOC001" in prompt
    assert result["stage"] == "quality_check"


# ---------------------------------------------------------------------------
# story 剧情补充层 + 可选用户审核门
# ---------------------------------------------------------------------------


def test_write_script_persists_story(tmp_path: Path) -> None:
    """write_script 的 story 落盘到 story.json 并写回 data。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store, story=_story())
    data = store.load("demo")
    assert data["story"]["plot"] == "一家三口在暴雪中逃亡求生"
    assert data["story"]["characters"][0]["prompt"].startswith("45 岁")
    assert (tmp_path / "cinematic" / "demo" / "story.json").exists()


def test_write_script_generates_script_doc(tmp_path: Path) -> None:
    """write_script 生成可读的剧本资产文档 script.md，含剧情与人物。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store, story=_story())
    doc = (tmp_path / "cinematic" / "demo" / "script.md").read_text(encoding="utf-8")
    assert "## 整体剧情" in doc
    assert "一家三口在暴雪中逃亡求生" in doc
    assert "CHAR001" in doc and "爸爸" in doc
    assert "## 分镜" in doc


def test_request_user_review_enters_script_review(tmp_path: Path) -> None:
    """request_user_review 进入 script_review 并返回完整剧情供呈现。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store, story=_story())
    result = _act(store, "request_user_review", project_id="demo")
    assert result["stage"] == "script_review"
    assert result["story"]["plot"] == "一家三口在暴雪中逃亡求生"
    assert store.load("demo")["stage"] == "script_review"


def test_approve_script_true_advances_to_spatial_planning(tmp_path: Path) -> None:
    """用户批准 → spatial_planning，可继续 set_floorplan。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store, story=_story())
    _act(store, "request_user_review", project_id="demo")
    result = _act(store, "approve_script", project_id="demo", approved=True)
    assert result["stage"] == "spatial_planning"
    _act(store, "set_floorplan", project_id="demo", width=100, length=80)
    assert store.load("demo")["stage"] == "world_building"


def test_approve_script_false_returns_to_script_analysis(tmp_path: Path) -> None:
    """用户打回 → script_analysis，且后续 set_floorplan 仍被审核门挡住。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store, story=_story())
    _act(store, "request_user_review", project_id="demo")
    result = _act(store, "approve_script", project_id="demo", approved=False, note="剧情太单薄")
    assert result["stage"] == "script_analysis"
    assert store.load("demo")["stage"] == "script_analysis"
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _act(store, "set_floorplan", project_id="demo", width=100, length=80)


def test_request_user_review_requires_script_analysis(tmp_path: Path) -> None:
    """尚未写剧本（init）时不可进入用户审核。"""
    store = _store(tmp_path)
    _create_project(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _act(store, "request_user_review", project_id="demo")


def test_approve_script_requires_script_review(tmp_path: Path) -> None:
    """未进入 script_review 阶段不可裁决。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _act(store, "approve_script", project_id="demo", approved=True)


def test_approve_script_requires_approved_bool(tmp_path: Path) -> None:
    """approve_script 必须显式传 approved 布尔值。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _act(store, "request_user_review", project_id="demo")
    with pytest.raises(CinematicDirectorError, match="approved"):
        _act(store, "approve_script", project_id="demo")


def test_review_script_without_user_review_advances(tmp_path: Path) -> None:
    """不请求用户审核时，review_script 直接进 spatial_planning（可选门跳过）。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    result = _review_script(store)
    assert result["stage"] == "spatial_planning"


# ---------------------------------------------------------------------------
# 首尾帧连贯（非首镜头必须用上一镜头尾帧作为首帧）+ 空间图 + 音频
# ---------------------------------------------------------------------------


def _setup_two_shot_locked(store: ProjectStore, project_id: str = "demo") -> dict:
    """推进到 storyboard，含两个镜头（各带合法空间站位，满足空间一致性硬门）。"""
    _create_project(store, project_id)
    shots = [
        {"id": "Shot001", "scene_id": "Scene001", "shot_size": "大全景",
         "action": "逃亡", "emotion": "恐惧", "sound": "风雪声",
         "asset_refs": ["CHAR001", "CHAR002", "LOC001"],
         "spatial": {
             "camera": {"position": {"x": 30, "y": 20}, "target": {"x": 50, "y": 40}},
             "characters": [
                 {"asset_id": "CHAR001", "position": {"x": 50, "y": 40}, "facing": 90},
                 {"asset_id": "CHAR002", "position": {"x": 45, "y": 36}, "facing": 0},
             ],
         }},
        {"id": "Shot002", "scene_id": "Scene001", "shot_size": "中景",
         "action": "反击", "emotion": "坚定", "sound": "风雪声",
         "asset_refs": ["CHAR001", "CHAR002", "LOC001"],
         "spatial": {
             "camera": {"position": {"x": 30, "y": 20}, "target": {"x": 50, "y": 40}},
             "characters": [
                 {"asset_id": "CHAR001", "position": {"x": 50, "y": 40}, "facing": 90},
                 {"asset_id": "CHAR002", "position": {"x": 45, "y": 36}, "facing": 0},
             ],
         }},
    ]
    _act(store, "write_script", project_id=project_id,
         scenes=[{"id": "Scene001", "location": "雪林", "time": "黄昏",
                  "function": "建立危机感", "duration": 10}],
         shots=shots)
    _review_script(store, project_id)
    _act(store, "set_floorplan", project_id=project_id, width=100, length=80)
    _act(store, "add_asset", project_id=project_id, kind="LOC", id="LOC001", name="雪林",
         appearance="黄昏暴雪的雪林", reference_image="assets/LOC001.png",
         position={"x": 50, "y": 40}, footprint={"width": 20, "depth": 16})
    _add_char(store, project_id, "CHAR001")
    _add_char(store, project_id, "CHAR002")
    _review_assets(store, project_id)
    return _lock_assets(store, project_id)


def _plan_all(store: ProjectStore, project_id: str = "demo", shots: tuple[str, ...] = ("Shot001", "Shot002")) -> None:
    for sid in shots:
        _act(store, "plan_shot", project_id=project_id, shot_id=sid, movement="tracking")


def test_first_shot_compile_requires_no_last_frame(tmp_path: Path) -> None:
    """首个镜头编译不要求上一镜头尾帧，image_urls 仅参考图。"""
    store = _store(tmp_path)
    _setup_two_shot_locked(store)
    _plan_all(store)
    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    assert result["image_urls"] == ["assets/CHAR001.png", "assets/CHAR002.png", "assets/LOC001.png"]
    assert result["audio_urls"] == []


def test_non_first_shot_without_continuity_skips_prev_frame(tmp_path: Path) -> None:
    """非首镜头未请求 continuity 时不引入上一镜头尾帧（跨场景/硬切）。"""
    store = _store(tmp_path)
    _setup_two_shot_locked(store)
    _plan_all(store)
    _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot002")
    assert result["image_urls"] == ["assets/CHAR001.png", "assets/CHAR002.png", "assets/LOC001.png"]


def test_continuity_requires_prev_last_frame(tmp_path: Path) -> None:
    """continuity=true 但上一镜头未 record_shot_frame → 拒绝。"""
    store = _store(tmp_path)
    _setup_two_shot_locked(store)
    _plan_all(store)
    _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    with pytest.raises(CinematicDirectorError, match="record_shot_frame"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot002", continuity=True)


def test_continuity_rejected_on_first_shot(tmp_path: Path) -> None:
    """首个镜头请求 continuity → 拒绝（无上一镜头）。"""
    store = _store(tmp_path)
    _setup_two_shot_locked(store)
    _plan_all(store)
    with pytest.raises(CinematicDirectorError, match="first shot"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot001", continuity=True)


def test_non_first_shot_uses_prev_last_frame_as_first_frame(tmp_path: Path) -> None:
    """continuity=true 时，下一镜头 image_urls[0] 即上一镜头尾帧（首帧连贯）。"""
    store = _store(tmp_path)
    _setup_two_shot_locked(store)
    _plan_all(store)
    _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    _act(store, "record_qc", project_id="demo", shot_id="Shot001",
         character_score=95, scene_score=90, action_score=88)
    _act(store, "record_shot_result", project_id="demo", shot_id="Shot001",
         video_path="out/shot001.mp4")
    _act(store, "record_shot_frame", project_id="demo", shot_id="Shot001",
         last_frame="frames/shot001_last.jpg")

    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot002", continuity=True)
    assert result["image_urls"][0] == "frames/shot001_last.jpg"
    assert result["image_urls"][1:] == ["assets/CHAR001.png", "assets/CHAR002.png", "assets/LOC001.png"]


def test_attach_spatial_map_and_audio_flow_into_compile(tmp_path: Path) -> None:
    """空间坐标关系图并入 image_urls，音频资产作为 audio_urls 返回。"""
    store = _store(tmp_path)
    _setup_two_shot_locked(store)
    _act(store, "attach_spatial_map", project_id="demo", image="assets/map.png")
    _act(store, "attach_audio", project_id="demo", shot_id="Shot001", audio="assets/voice001.mp3")
    _plan_all(store)
    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    assert result["image_urls"][0] == "assets/map.png"
    assert result["audio_urls"] == ["assets/voice001.mp3"]


def test_record_shot_frame_requires_final_status(tmp_path: Path) -> None:
    """镜头非 final（未 record_shot_result）时 record_shot_frame 拒绝。"""
    store = _store(tmp_path)
    _setup_two_shot_locked(store)
    _plan_all(store)
    _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    with pytest.raises(CinematicDirectorError, match="not yet final"):
        _act(store, "record_shot_frame", project_id="demo", shot_id="Shot001",
             last_frame="frames/x.jpg")


def test_attach_audio_stage_gate(tmp_path: Path) -> None:
    """storyboard 之前不可 attach_audio。"""
    store = _store(tmp_path)
    _create_project(store)
    with pytest.raises(CinematicDirectorError, match="not allowed at stage"):
        _act(store, "attach_audio", project_id="demo", shot_id="Shot001", audio="x.mp3")


def test_attach_spatial_map_requires_floorplan(tmp_path: Path) -> None:
    """未 set_floorplan 前不可 attach_spatial_map。"""
    store = _store(tmp_path)
    _create_project(store)
    _write_script(store)
    _review_script(store)  # spatial_planning，尚未 set_floorplan
    with pytest.raises(CinematicDirectorError, match="set_floorplan"):
        _act(store, "attach_spatial_map", project_id="demo", image="assets/map.png")


# ---------------------------------------------------------------------------
# 角色强制资产（三视图 + 声线）与音频一致性硬门
# ---------------------------------------------------------------------------


def _to_character_design(store: ProjectStore, project_id: str = "demo") -> None:
    """推进到 character_design（add_asset(CHAR) 之前）。"""
    _create_project(store, project_id)
    _write_script(store, project_id)
    _review_script(store, project_id)
    _act(store, "set_floorplan", project_id=project_id, width=100, length=80)
    _add_loc(store, project_id, "LOC001")  # → character_design


def test_add_char_requires_three_view(tmp_path: Path) -> None:
    """CHAR 资产必须声明 reference_image 为正/侧/背三视图合成图。"""
    store = _store(tmp_path)
    _to_character_design(store)
    with pytest.raises(CinematicDirectorError, match="three_view"):
        _act(store, "add_asset", project_id="demo", kind="CHAR", id="CHAR001",
             name="爸爸", appearance="45 岁男人，黑色羽绒服",
             reference_image="assets/CHAR001.png", voice="assets/CHAR001_voice.mp3")


def test_add_char_requires_voice(tmp_path: Path) -> None:
    """CHAR 资产必须提供声线参考（口型与音色一致）。"""
    store = _store(tmp_path)
    _to_character_design(store)
    with pytest.raises(CinematicDirectorError, match="voice"):
        _act(store, "add_asset", project_id="demo", kind="CHAR", id="CHAR001",
             name="爸爸", appearance="45 岁男人，黑色羽绒服",
             reference_image="assets/CHAR001.png", three_view=True)


def test_lock_assets_rejects_char_without_three_view(tmp_path: Path) -> None:
    """锁定前 CHAR 缺失三视图 → lock_assets 拒绝。"""
    store = _store(tmp_path)
    _setup_asset_lock(store)
    data = store.load("demo")
    data["assets"]["CHAR001"]["three_view"] = False
    store.save("demo", data)
    with pytest.raises(CinematicDirectorError, match="三视图"):
        _lock_assets(store)


def test_lock_assets_rejects_char_without_voice(tmp_path: Path) -> None:
    """锁定前 CHAR 缺失声线 → lock_assets 拒绝。"""
    store = _store(tmp_path)
    _setup_asset_lock(store)
    data = store.load("demo")
    data["assets"]["CHAR001"]["voice"] = ""
    store.save("demo", data)
    with pytest.raises(CinematicDirectorError, match="声线"):
        _lock_assets(store)


def test_compile_prompt_rejects_dialogue_shot_without_audio(tmp_path: Path) -> None:
    """有对白的镜头未 attach_audio → compile_prompt 拒绝（口型 + 音频一致性）。"""
    store = _store(tmp_path)
    _setup_locked(store)
    data = store.load("demo")
    data["shots"][0]["dialogue"] = True
    store.save("demo", data)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    with pytest.raises(CinematicDirectorError, match="dialogue but no audio"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")


def test_compile_prompt_allows_dialogue_shot_with_audio(tmp_path: Path) -> None:
    """有对白的镜头 attach_audio 后 compile_prompt 放行。"""
    store = _store(tmp_path)
    _setup_locked(store)
    data = store.load("demo")
    data["shots"][0]["dialogue"] = True
    store.save("demo", data)
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    _act(store, "attach_audio", project_id="demo", shot_id="Shot001", audio="assets/voice001.mp3")
    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    assert result["audio_urls"] == ["assets/voice001.mp3"]


def test_plan_shot_can_backfill_spatial_after_floorplan(tmp_path: Path) -> None:
    """写剧本未带 spatial 时，compile_prompt 会被「missing spatial.characters」硬阻塞；
    plan_shot 须能在 video_generation 阶段补写 spatial 解开死路。"""
    store = _store(tmp_path)
    _create_project(store)
    # 写剧本：故意不带 spatial（对应智能体真实写法）
    _act(store, "write_script", project_id="demo",
         scenes=[{"id": "Scene001", "location": "雪林", "time": "黄昏",
                  "function": "建立危机感", "duration": 10}],
         shots=[{"id": "Shot001", "scene_id": "Scene001", "shot_size": "大全景",
                 "camera": "无人机下降", "lens": "24mm", "action": "一家三口逃亡",
                 "emotion": "恐惧", "sound": "风雪声",
                 "asset_refs": ["CHAR001", "CHAR002", "LOC001"]}],
         story=_story())
    _review_script(store)
    _act(store, "set_floorplan", project_id="demo", width=100, length=80)
    _add_loc(store, "demo", "LOC001")
    _add_char(store, "demo", "CHAR001")
    _add_char(store, "demo", "CHAR002")
    _review_assets(store)
    _lock_assets(store)
    # 只补摄影参数，不补 spatial → 单镜头全 part 后进入 video_generation
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001", movement="tracking")
    assert store.load("demo")["stage"] == "video_generation"

    # 复现死路：compile_prompt 硬阻塞
    with pytest.raises(CinematicDirectorError, match="missing spatial.characters"):
        _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")

    # 修复：video_generation 阶段 plan_shot 补写 spatial → compile_prompt 放行
    _act(store, "plan_shot", project_id="demo", shot_id="Shot001",
         spatial={"characters": [
             {"asset_id": "CHAR001", "position": {"x": 50, "y": 40}, "facing": 90},
             {"asset_id": "CHAR002", "position": {"x": 45, "y": 36}, "facing": 0},
         ],
                  "camera": {"position": {"x": 30, "y": 20}, "target": {"x": 50, "y": 40}}})
    result = _act(store, "compile_prompt", project_id="demo", shot_id="Shot001")
    assert "error" not in result
    assert result["prompt"].startswith("@CHAR001")
