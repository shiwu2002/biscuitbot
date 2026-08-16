"""AI 导演工作流：9 阶段状态机 + 三级审核 Gate + Prompt 编译器。

职责与项目角色：
- 每个动作对应一个模块级函数（函数名 = action 名），由工具门面分派；
- 所有函数遵守 9 阶段硬门：阶段顺序、审核门槛、数据冻结均由代码校验；
- ``compile_prompt`` 按固定规则编译带 ``@ID`` 锚点的 Seedance prompt（禁止
  LLM 直接生成最终视频提示词）。

三级审核 Gate：
- ``review_script``（剧本审核）：script_analysis → world_building；
- ``review_assets``（资产审核）：character_design → asset_lock；
- ``record_qc``（视频审核）：quality_check → final_edit（不过则回退重生成）。
"""

from __future__ import annotations

from datetime import datetime  # 项目创建时间戳
from typing import Any

from .constants import _QC_THRESHOLD
from .state import ProjectStore
from .validators import (
    CinematicDirectorError,
    check_role,
    require,
    require_stage,
    validate_kind_id,
    validate_project_id,
)

# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _get_shot(data: dict[str, Any], shot_id: Any) -> dict[str, Any]:
    """按 ID 取镜头；不存在时报错。"""
    require(bool(shot_id), "shot_id is required")
    for shot in data.get("shots", []):
        if shot.get("id") == shot_id:
            return shot
    raise CinematicDirectorError(f"shot {shot_id!r} not found in script")


def _all_refs(data: dict[str, Any]) -> set[str]:
    """脚本中全部镜头引用的资产 ID 集合。"""
    refs: set[str] = set()
    for shot in data.get("shots", []):
        refs.update(shot.get("asset_refs", []))
    return refs


def _all_world_refs_locked(data: dict[str, Any]) -> bool:
    """world_building 阶段完成条件：所有非 CHAR 引用资产均已添加。"""
    world = [r for r in _all_refs(data) if not r.startswith("CHAR")]
    return all(r in data["assets"] for r in world)


def _all_shots_planned(data: dict[str, Any]) -> bool:
    """storyboard 阶段完成条件：所有镜头都有非空摄影参数。"""
    shots = data.get("shots", [])
    return bool(shots) and all(s.get("cinematography") for s in shots)


def _all_shots_prompted(data: dict[str, Any]) -> bool:
    """video_generation 阶段完成条件：所有镜头均已编译 prompt。"""
    shots = data.get("shots", [])
    return bool(shots) and all(s.get("status") == "prompted" for s in shots)


def _all_shots_qc_pass(data: dict[str, Any]) -> bool:
    """quality_check 阶段完成条件：所有镜头质检通过。"""
    shots = data.get("shots", [])
    return bool(shots) and all(s.get("status") == "qc_pass" for s in shots)


def _review_gate(kwargs: dict[str, Any]) -> tuple[int, int, bool]:
    """解析审核分数/阈值/通过判定（剧本审核与资产审核共用）。

    通过 = score >= threshold 且未显式否决（passed 非 False）。
    """
    score = kwargs.get("score")
    require(isinstance(score, int) and 0 <= score <= 100, "review requires 'score' as integer 0-100")
    threshold = kwargs.get("threshold") or _QC_THRESHOLD
    passed = kwargs.get("passed")
    if passed is not None:
        require(isinstance(passed, bool), "'passed' must be boolean when provided")
    effective = score >= threshold and (passed is not False)
    return score, threshold, effective


# ---------------------------------------------------------------------------
# actions（函数名 = action 名）
# ---------------------------------------------------------------------------


def create_project(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """建项目 + 导演圣经定位 → stage=init。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    require(not store.exists(project_id), f"project {project_id!r} already exists")
    require(bool(kwargs.get("name")), "create_project requires 'name'")
    require(bool(kwargs.get("logline")), "create_project requires 'logline'")
    require(bool(kwargs.get("style")), "create_project requires 'style'")

    now = datetime.now().isoformat()
    data: dict[str, Any] = {
        "schema_version": 2,
        "project_id": project_id,
        "name": kwargs.get("name"),
        "stage": "init",
        "completed": False,
        "scenes": [],
        "bible": {
            "logline": kwargs.get("logline"),
            "style": kwargs.get("style"),
            "ratio": kwargs.get("ratio") or "16:9",
            "fps": kwargs.get("fps") or 24,
            "color_palette": kwargs.get("color_palette") or "",
            "reference_works": kwargs.get("reference_works") or "",
        },
        "assets": {},
        "shots": [],
        "reviews": {},
        "created_at": now,
    }
    store.save(project_id, data)
    return {"project_id": project_id, "stage": "init", "message": "project created; next: write_script"}


def write_script(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """写场景 + 分镜（叙事层）→ stage=script_analysis。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "write_script")
    data = store.load(project_id)
    require_stage(data["stage"], "write_script", "init", "script_analysis")

    scenes = kwargs.get("scenes") or []
    shots = kwargs.get("shots") or []
    require(isinstance(scenes, list) and len(scenes) >= 1, "write_script requires at least one scene")
    require(isinstance(shots, list) and len(shots) >= 1, "write_script requires at least one shot")

    scene_ids = {s.get("id") for s in scenes if isinstance(s, dict) and s.get("id")}
    require(len(scene_ids) == len(scenes), "each scene must have a unique non-empty 'id'")

    for shot in shots:
        require(isinstance(shot, dict), "each shot must be an object")
        require(bool(shot.get("id")), "each shot requires 'id'")
        require(shot.get("scene_id") in scene_ids, f"shot {shot.get('id')!r} references unknown scene_id")
        refs = shot.get("asset_refs") or []
        require(isinstance(refs, list) and refs, f"shot {shot.get('id')!r} requires non-empty asset_refs")
        has_char = any(isinstance(r, str) and r.startswith("CHAR") for r in refs)
        has_loc = any(isinstance(r, str) and r.startswith("LOC") for r in refs)
        require(has_char, f"shot {shot.get('id')!r} asset_refs must include at least one CHAR###")
        require(has_loc, f"shot {shot.get('id')!r} asset_refs must include at least one LOC###")

    data["scenes"] = scenes
    data["shots"] = [
        {
            "id": s.get("id"),
            "scene_id": s.get("scene_id"),
            "shot_size": s.get("shot_size") or "",
            "camera": s.get("camera") or "",
            "lens": s.get("lens") or "",
            "action": s.get("action") or "",
            "emotion": s.get("emotion") or "",
            "sound": s.get("sound") or "",
            "asset_refs": list(s.get("asset_refs") or []),
            "status": "pending",
            "cinematography": {},
            "prompt": "",
            "image_urls": [],
            "video_path": "",
        }
        for s in shots
    ]
    data["stage"] = "script_analysis"
    store.save(project_id, data)
    return {
        "project_id": project_id,
        "stage": "script_analysis",
        "scenes": len(scenes),
        "shots": len(shots),
        "message": "script written; next: review_script",
    }


def review_script(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """剧本审核 Gate：通过 → world_building；不过 → 停留改剧本。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "review_script")
    data = store.load(project_id)
    require_stage(data["stage"], "review_script", "script_analysis")

    score, threshold, effective = _review_gate(kwargs)
    data["reviews"]["script_review"] = {
        "kind": "script", "score": score, "passed": effective,
        "note": kwargs.get("note") or "", "threshold": threshold,
        "reviewer": kwargs.get("reviewer") or "",
    }
    if effective:
        data["stage"] = "world_building"
        store.save(project_id, data)
        return {
            "project_id": project_id, "stage": "world_building", "passed": True,
            "message": "script review passed; next: add_asset (LOC/PROP) for world_building",
        }
    store.save(project_id, data)
    return {
        "project_id": project_id, "stage": "script_analysis", "passed": False,
        "message": "script review failed; revise write_script then review_script again",
    }


def add_asset(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """锁定单个资产（LOC/PROP 在 world_building，CHAR 在 character_design）。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "add_asset")
    data = store.load(project_id)
    kind = kwargs.get("kind")
    asset_id = kwargs.get("id")
    validate_kind_id(kind, asset_id)
    require(bool(kwargs.get("name")), "add_asset requires 'name'")
    require(bool(kwargs.get("appearance")), "add_asset requires 'appearance' (stable descriptive phrase)")
    require(bool(kwargs.get("reference_image")), "add_asset requires 'reference_image' (asset must be visually locked)")
    require(asset_id not in data["assets"], f"asset {asset_id!r} already exists; use a new id")

    # 阶段硬门：world 资产只能在 world_building，CHAR 只能在 character_design
    stage = data["stage"]
    if kind == "CHAR":
        require_stage(stage, f"add_asset({kind})", "character_design")
    else:
        require_stage(stage, f"add_asset({kind})", "world_building")

    data["assets"][asset_id] = {
        "id": asset_id,
        "kind": kind,
        "name": kwargs.get("name"),
        "appearance": kwargs.get("appearance"),
        "reference_image": kwargs.get("reference_image"),
        "locked": False,
    }
    # 自动推进：world_building 的所有非 CHAR 引用齐备 → character_design
    if stage == "world_building" and _all_world_refs_locked(data):
        data["stage"] = "character_design"
    store.save(project_id, data)
    return {
        "project_id": project_id,
        "asset_id": asset_id,
        "stage": data["stage"],
        "message": f"asset {asset_id} added; stage: {data['stage']}",
    }


def review_assets(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """资产审核 Gate：通过 → asset_lock；不过 → 停留补资产。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "review_assets")
    data = store.load(project_id)
    require_stage(data["stage"], "review_assets", "character_design")

    chars = [r for r in sorted(_all_refs(data)) if r.startswith("CHAR")]
    missing = [r for r in chars if r not in data["assets"]]
    require(not missing, f"cannot review assets; missing character assets: {missing}")

    score, threshold, effective = _review_gate(kwargs)
    data["reviews"]["asset_review"] = {
        "kind": "assets", "score": score, "passed": effective,
        "note": kwargs.get("note") or "", "threshold": threshold,
        "reviewer": kwargs.get("reviewer") or "",
    }
    if effective:
        data["stage"] = "asset_lock"
        store.save(project_id, data)
        return {
            "project_id": project_id, "stage": "asset_lock", "passed": True,
            "message": "asset review passed; next: lock_assets",
        }
    store.save(project_id, data)
    return {
        "project_id": project_id, "stage": "character_design", "passed": False,
        "message": "asset review failed; adjust assets then review_assets again",
    }


def lock_assets(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """资产锁定：校验引用齐备 + 参考图 → stage=storyboard（资产冻结）。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "lock_assets")
    data = store.load(project_id)
    require_stage(data["stage"], "lock_assets", "asset_lock")

    refs = _all_refs(data)
    missing = [r for r in sorted(refs) if r not in data["assets"]]
    no_image = [
        r for r in sorted(refs)
        if r in data["assets"] and not data["assets"][r].get("reference_image")
    ]
    if missing or no_image:
        parts = []
        if missing:
            parts.append(f"missing assets: {missing}")
        if no_image:
            parts.append(f"assets without reference_image: {no_image}")
        raise CinematicDirectorError(
            "assets are not fully locked; " + "; ".join(parts) + ". Add them with action=add_asset."
        )

    for r in refs:
        data["assets"][r]["locked"] = True
    data["stage"] = "storyboard"
    store.save(project_id, data)
    return {
        "project_id": project_id,
        "stage": "storyboard",
        "assets": sorted(refs),
        "message": "all assets locked; next: plan_shot for every shot",
    }


def plan_shot(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """补摄影参数（storyboard 阶段）；全部分镜齐备后自动 → video_generation。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "plan_shot")
    data = store.load(project_id)
    require_stage(data["stage"], "plan_shot", "storyboard")

    shot_id = kwargs.get("shot_id")
    shot = _get_shot(data, shot_id)
    cin = dict(shot.get("cinematography") or {})
    for field in ("camera", "lens", "fps", "movement", "depth", "lighting"):
        if kwargs.get(field) is not None:
            cin[field] = kwargs[field]
    shot["cinematography"] = cin

    if _all_shots_planned(data):
        data["stage"] = "video_generation"
    store.save(project_id, data)
    return {
        "project_id": project_id,
        "shot_id": shot_id,
        "stage": data["stage"],
        "cinematography": cin,
        "message": f"shot {shot_id} planned; next: compile_prompt",
    }


def compile_prompt(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """编译规范化 Seedance prompt + 返回参考图列表（禁止 LLM 直接编最终提示词）。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "compile_prompt")
    data = store.load(project_id)
    stage = data["stage"]
    require_stage(stage, "compile_prompt", "video_generation", "quality_check")

    shot_id = kwargs.get("shot_id")
    shot = _get_shot(data, shot_id)
    assets: dict[str, Any] = data["assets"]
    refs = shot.get("asset_refs", [])
    require(refs, f"shot {shot_id!r} has no asset_refs")
    for r in refs:
        require(r in assets, f"asset {r!r} referenced by shot {shot_id!r} is not locked")

    char_refs = [r for r in refs if r.startswith("CHAR")]
    loc_refs = [r for r in refs if r.startswith("LOC")]
    prop_refs = [r for r in refs if r.startswith("PROP")]

    cin = shot.get("cinematography") or {}
    bible = data.get("bible") or {}

    lines: list[str] = []
    if char_refs:
        lines.append(" and ".join(f"@{r} {assets[r]['appearance']}" for r in char_refs))
    if loc_refs:
        lines.append(" and ".join(f"@{r} {assets[r]['appearance']}" for r in loc_refs))
    if prop_refs:
        lines.append("with " + " and ".join(f"@{r} {assets[r]['appearance']}" for r in prop_refs))
    lines.append(f"Camera: {cin.get('movement') or shot.get('camera') or ''}, {cin.get('fps') or bible.get('fps') or 24}fps")
    lines.append(f"Action: {shot.get('action') or ''}")
    if cin.get("lighting"):
        lines.append(f"Lighting: {cin['lighting']}")
    lines.append(f"Style: {bible.get('style') or ''}")
    prompt = "\n".join(line for line in lines if line.strip())

    image_urls = [assets[r]["reference_image"] for r in refs]

    shot["prompt"] = prompt
    shot["image_urls"] = image_urls
    shot["status"] = "prompted"
    if stage == "video_generation" and _all_shots_prompted(data):
        data["stage"] = "quality_check"
    store.save(project_id, data)
    return {
        "project_id": project_id,
        "shot_id": shot_id,
        "stage": data["stage"],
        "prompt": prompt,
        "image_urls": image_urls,
        "message": "pass prompt + image_urls to generate_video, then record_qc",
    }


def record_qc(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """视频审核 Gate：三项分数任一低于阈值 → qc_fail，强制重生成。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "record_qc")
    data = store.load(project_id)
    require_stage(data["stage"], "record_qc", "quality_check")

    shot_id = kwargs.get("shot_id")
    shot = _get_shot(data, shot_id)
    require(
        shot.get("status") in ("prompted", "qc_fail"),
        f"shot {shot_id!r} is not ready for QC (current: {shot.get('status')})",
    )

    threshold = kwargs.get("threshold") or _QC_THRESHOLD
    scores = {
        "character": kwargs.get("character_score"),
        "scene": kwargs.get("scene_score"),
        "action": kwargs.get("action_score"),
    }
    for key, value in scores.items():
        require(isinstance(value, int) and 0 <= value <= 100, f"record_qc requires '{key}_score' as integer 0-100")

    passed = all(value is not None and value >= threshold for value in scores.values())
    data["reviews"][f"shot_{shot_id}_review"] = {
        "kind": "video", "shot_id": shot_id,
        "character": scores["character"], "scene": scores["scene"], "action": scores["action"],
        "threshold": threshold, "passed": passed, "note": kwargs.get("note") or "",
        "reviewer": kwargs.get("reviewer") or "",
    }
    shot["status"] = "qc_pass" if passed else "qc_fail"
    if passed and _all_shots_qc_pass(data):
        data["stage"] = "final_edit"
    store.save(project_id, data)

    if not passed:
        below = [k for k, v in scores.items() if isinstance(v, (int, float)) and v < threshold]
        return {
            "project_id": project_id, "shot_id": shot_id, "stage": data["stage"],
            "passed": False, "below_threshold": below,
            "message": f"QC failed on {below}; regenerate. Adjust compile_prompt and generate_video again, then record_qc.",
        }
    return {
        "project_id": project_id, "shot_id": shot_id, "stage": data["stage"],
        "passed": True, "scores": scores, "message": "QC passed; next: record_shot_result",
    }


def record_shot_result(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """记录成片（final_edit 阶段）；全部 final → completed。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "record_shot_result")
    data = store.load(project_id)
    require_stage(data["stage"], "record_shot_result", "final_edit")

    shot_id = kwargs.get("shot_id")
    shot = _get_shot(data, shot_id)
    require(
        shot.get("status") == "qc_pass",
        f"cannot record result for shot {shot_id!r}: QC not passed (current: {shot.get('status')})",
    )
    require(bool(kwargs.get("video_path")), "record_shot_result requires 'video_path'")

    shot["video_path"] = kwargs.get("video_path")
    shot["status"] = "final"
    if all(s.get("status") == "final" for s in data["shots"]):
        data["completed"] = True
    store.save(project_id, data)
    return {
        "project_id": project_id, "shot_id": shot_id,
        "stage": data["stage"], "completed": data["completed"],
        "message": f"shot {shot_id} recorded; completed: {data['completed']}",
    }


def status(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """查看项目状态 + 代码算出的下一步。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    data = store.load(project_id)
    return {
        "project_id": project_id,
        "name": data.get("name"),
        "stage": data["stage"],
        "completed": data.get("completed", False),
        "scenes": len(data.get("scenes", [])),
        "shots": [
            {"id": s.get("id"), "status": s.get("status")}
            for s in data.get("shots", [])
        ],
        "assets": len(data.get("assets", {})),
        "assets_locked": sum(1 for a in data.get("assets", {}).values() if a.get("locked")),
        "reviews": {k: v.get("passed") for k, v in (data.get("reviews") or {}).items()},
        "next_action": next_action(data),
    }


def next_action(data: dict[str, Any]) -> str:
    """根据当前 9 阶段状态机，代码推算出下一步动作（供模型参考，而非自由发挥）。"""
    if data.get("completed"):
        return "done (all shots final)"
    stage = data.get("stage")
    if stage == "init":
        return "write_script"
    if stage == "script_analysis":
        return "review_script (or write_script to revise)"
    if stage == "world_building":
        return "add_asset (LOC/PROP) for every world asset"
    if stage == "character_design":
        return "add_asset (CHAR) for every character, then review_assets"
    if stage == "asset_lock":
        return "lock_assets"
    if stage == "storyboard":
        return "plan_shot for every shot"
    if stage == "video_generation":
        return "compile_prompt for each shot"
    if stage == "quality_check":
        for shot in data.get("shots", []):
            if shot.get("status") in ("prompted", "qc_fail"):
                return f"record_qc for {shot.get('id')}"
        return "record_qc for remaining shots"
    if stage == "final_edit":
        return "record_shot_result for qc_pass shots"
    return "unknown stage"
