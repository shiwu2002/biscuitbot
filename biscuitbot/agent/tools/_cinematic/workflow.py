"""AI 导演工作流：11 阶段状态机 + 审核 Gate + Prompt 编译器。

职责与项目角色：
- 每个动作对应一个模块级函数（函数名 = action 名），由工具门面分派；
- 所有函数遵守 11 阶段硬门：阶段顺序、审核门槛、数据冻结均由代码校验；
- ``compile_prompt`` 按固定规则编译带 ``@ID`` 锚点的 Seedance prompt（禁止
  LLM 直接生成最终视频提示词）。

审核 Gate：
- ``review_script``（剧本审核，自动打分）：script_analysis → spatial_planning；
- ``request_user_review`` + ``approve_script``（可选用户审核）：script_analysis
  → script_review → spatial_planning / 打回 script_analysis；
- ``review_assets``（资产审核）：character_design → asset_lock；
- ``record_qc``（视频审核）：quality_check → final_edit（不过则回退重生成）。
"""

from __future__ import annotations

from datetime import datetime  # 项目创建时间戳
from typing import Any

from .constants import _QC_THRESHOLD
from .spatial import (
    bearing_word,
    compass_word,
    footprint_aabb,
    point_in_aabb,
    relative_position_word,
    validate_floorplan,
    validate_loc_placement,
    validate_shot_spatial,
)
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


def _require_point(value: Any, label: str) -> dict[str, Any]:
    """校验坐标点为 ``{x, y}`` 数字对象，返回规范化 dict。"""
    require(
        isinstance(value, dict)
        and isinstance(value.get("x"), (int, float))
        and isinstance(value.get("y"), (int, float)),
        f"{label} must be an object with numeric x and y",
    )
    return {"x": float(value["x"]), "y": float(value["y"])}


def _require_footprint(value: Any, label: str) -> dict[str, Any] | None:
    """校验占地为 ``{width, depth}`` 正数对象；None 返回 None。"""
    if value is None:
        return None
    require(
        isinstance(value, dict)
        and isinstance(value.get("width"), (int, float))
        and isinstance(value.get("depth"), (int, float))
        and value["width"] > 0
        and value["depth"] > 0,
        f"{label} must be an object with positive numeric width and depth",
    )
    return {"width": float(value["width"]), "depth": float(value["depth"])}


def _require_orientation(value: Any, label: str) -> float | None:
    """校验朝向角为 0-360 数字；None 返回 None。"""
    if value is None:
        return None
    require(
        isinstance(value, (int, float)) and 0 <= value <= 360,
        f"{label} must be a number in 0-360",
    )
    return float(value)


def _parse_spatial_entity(ent: dict[str, Any], prefix: str, refs: list, shot_id: str) -> dict[str, Any]:
    """规范化单个站位实体（人物 / 道具）。"""
    aid = ent.get("asset_id")
    require(
        isinstance(aid, str) and aid.startswith(prefix),
        f"shot {shot_id} spatial {prefix} entity asset_id {aid!r} must start with {prefix}",
    )
    require(aid in refs, f"spatial entity {aid!r} is not in asset_refs of shot {shot_id}")
    norm: dict[str, Any] = {
        "asset_id": aid,
        "position": _require_point(ent.get("position"), f"{aid} position"),
    }
    if ent.get("facing") is not None:
        norm["facing"] = _require_orientation(ent.get("facing"), f"{aid} facing")
    if ent.get("motion_to") is not None:
        norm["motion_to"] = _require_point(ent.get("motion_to"), f"{aid} motion_to")
    return norm


def _parse_shot_spatial(sp: Any, refs: list, shot_id: str) -> dict[str, Any]:
    """规范化并校验镜头空间布局（人物 / 道具 / 机位）；缺省返回空结构。"""
    sp = sp or {}
    require(isinstance(sp, dict), f"shot {shot_id} 'spatial' must be an object")

    characters: list[dict[str, Any]] = []
    for ent in sp.get("characters") or []:
        require(isinstance(ent, dict), f"shot {shot_id} spatial character must be an object")
        characters.append(_parse_spatial_entity(ent, "CHAR", refs, shot_id))
    props: list[dict[str, Any]] = []
    for ent in sp.get("props") or []:
        require(isinstance(ent, dict), f"shot {shot_id} spatial prop must be an object")
        props.append(_parse_spatial_entity(ent, "PROP", refs, shot_id))

    camera: dict[str, Any] = {}
    cam = sp.get("camera")
    if cam is not None:
        require(isinstance(cam, dict), f"shot {shot_id} spatial.camera must be an object")
        if cam.get("position") is not None:
            camera["position"] = _require_point(cam.get("position"), "camera position")
        if cam.get("target") is not None:
            camera["target"] = _require_point(cam.get("target"), "camera target")
        if cam.get("facing") is not None:
            camera["facing"] = _require_orientation(cam.get("facing"), "camera facing")

    return {"characters": characters, "props": props, "camera": camera}


def _loc_spatial_suffix(asset: dict[str, Any]) -> str:
    """LOC 资产的空间后缀（平面图坐标 + 朝向）。"""
    pos = asset.get("position")
    if not pos:
        return ""
    parts = [f"位于平面图 ({pos['x']:g},{pos['y']:g})"]
    if asset.get("orientation") is not None:
        parts.append(f"朝向{compass_word(asset['orientation'])}")
    return "（" + "，".join(parts) + "）"


def _char_spatial_suffix(ent: dict[str, Any], shot: dict[str, Any], assets: dict[str, Any]) -> str:
    """人物站位后缀（相对所在 LOC 的 9 格方位 + 朝向）。"""
    pos = ent["position"]
    parts: list[str] = []
    for r in (r for r in shot.get("asset_refs", []) if r.startswith("LOC")):
        a = assets.get(r)
        aabb = footprint_aabb(a) if a else None
        if aabb is not None and point_in_aabb(pos, aabb):
            parts.append(f"站在 @{r} {relative_position_word(pos, a)}")
            break
    if ent.get("facing") is not None:
        parts.append(f"面向{compass_word(ent['facing'])}")
    return "（" + "，".join(parts) + "）" if parts else ""


def _camera_spatial_suffix(camera: dict[str, Any], shot: dict[str, Any], assets: dict[str, Any]) -> str:
    """机位空间后缀（相对首个引用 LOC 的方位 + 朝向）。"""
    cam_pos = camera.get("position")
    if not cam_pos:
        return ""
    loc_refs = [r for r in shot.get("asset_refs", []) if r.startswith("LOC")]
    parts: list[str] = []
    if loc_refs and assets.get(loc_refs[0], {}).get("position"):
        parts.append(
            f"机位在 @{loc_refs[0]} {bearing_word(assets[loc_refs[0]]['position'], cam_pos)}"
        )
    if camera.get("facing") is not None:
        parts.append(f"朝向{compass_word(camera['facing'])}")
    return "（" + "，".join(parts) + "）" if parts else ""


def _fmt_point(p: Any) -> str:
    """坐标点格式化为 ``(x,y)``；缺坐标返回 ``(?,?)``。"""
    if isinstance(p, dict) and isinstance(p.get("x"), (int, float)) and isinstance(p.get("y"), (int, float)):
        return f"({p['x']:g},{p['y']:g})"
    return "(?,?)"


def _render_script_md(data: dict[str, Any]) -> str:
    """把项目剧本渲染为可读 Markdown（剧本文档，写入 script.md 供用户查阅剧情）。"""
    bible = data.get("bible") or {}
    story = data.get("story") or {}
    lines: list[str] = []
    lines.append(f"# {data.get('name') or data.get('project_id')}（{data.get('project_id')}）")
    lines.append("")

    meta = [f"logline: {bible.get('logline') or ''}"]
    for label, key in (("风格", "style"), ("画幅", "ratio"), ("帧率", "fps"),
                       ("色彩体系", "color_palette"), ("对标作品", "reference_works")):
        if bible.get(key):
            meta.append(f"{label}: {bible[key]}")
    lines.append(" | ".join(meta))
    lines.append("")

    if story.get("plot"):
        lines.append("## 整体剧情")
        lines.append("")
        lines.append(str(story["plot"]))
        lines.append("")
    if story.get("worldview"):
        lines.append("## 大局描述词")
        lines.append("")
        lines.append(str(story["worldview"]))
        lines.append("")
    if story.get("spatial"):
        lines.append("## 空间描述词")
        lines.append("")
        lines.append(str(story["spatial"]))
        lines.append("")

    characters = story.get("characters") or []
    if characters:
        lines.append("## 人物")
        lines.append("")
        for c in characters:
            if isinstance(c, dict):
                lines.append(f"- **{c.get('id')} {c.get('name') or ''}**：{c.get('prompt') or ''}".rstrip())
        lines.append("")

    environments = story.get("environments") or []
    if environments:
        lines.append("## 环境")
        lines.append("")
        for e in environments:
            if isinstance(e, dict):
                lines.append(f"- **{e.get('id')} {e.get('name') or ''}**：{e.get('prompt') or ''}".rstrip())
        lines.append("")

    scenes = data.get("scenes") or []
    if scenes:
        lines.append("## 场景")
        lines.append("")
        for s in scenes:
            if isinstance(s, dict):
                head = " · ".join(
                    p for p in (str(s.get("id") or ""), str(s.get("location") or ""),
                                str(s.get("time") or ""), f"{s['duration']}s" if s.get("duration") else "")
                    if p
                )
                func = s.get("function") or ""
                lines.append(f"- **{head}**" + (f"：{func}" if func else ""))
        lines.append("")

    shots = data.get("shots") or []
    if shots:
        lines.append("## 分镜")
        lines.append("")
        for sh in shots:
            if not isinstance(sh, dict):
                continue
            lines.append(f"- **{sh.get('id')}**（{sh.get('scene_id')} · {sh.get('shot_size') or ''}）：{sh.get('action') or ''}")
            detail: list[str] = []
            if sh.get("emotion"):
                detail.append(f"情绪: {sh['emotion']}")
            if sh.get("sound"):
                detail.append(f"声音: {sh['sound']}")
            refs = sh.get("asset_refs") or []
            if refs:
                detail.append("资产: " + ", ".join(refs))
            spatial = sh.get("spatial") or {}
            cam = spatial.get("camera") or {}
            if cam.get("position") and cam.get("target"):
                detail.append(f"机位: {_fmt_point(cam['position'])} → {_fmt_point(cam['target'])}")
            chars = spatial.get("characters") or []
            if chars:
                bits = []
                for ent in chars:
                    facing = ent.get("facing")
                    s = f"{ent.get('asset_id')}@{_fmt_point(ent.get('position'))}"
                    if facing is not None:
                        s += f" 面向{compass_word(facing)}"
                    bits.append(s)
                detail.append("人物站位: " + "；".join(bits))
            for d in detail:
                lines.append(f"  - {d}")
    return "\n".join(lines) + "\n"


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
        "story": {},
        "assets": {},
        "shots": [],
        "reviews": {},
        "floorplan": None,
        "created_at": now,
    }
    store.save(project_id, data)
    return {"project_id": project_id, "stage": "init", "message": "project created; next: write_script"}


def set_floorplan(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """声明全局 2D 平面图（空间规划强制阶段，做完才允许进入资产生成）。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "set_floorplan")
    data = store.load(project_id)
    require_stage(data["stage"], "set_floorplan", "spatial_planning")

    floorplan: dict[str, Any] = {
        "unit": kwargs.get("unit") or "meter",
        "width": kwargs.get("width"),
        "length": kwargs.get("length"),
        "north": "up",
    }
    issues = validate_floorplan(floorplan)
    require(not issues, "set_floorplan: " + "; ".join(issues))

    data["floorplan"] = floorplan
    data["stage"] = "world_building"  # 空间规划完成 → 放行资产生成
    store.save(project_id, data)
    return {
        "project_id": project_id,
        "stage": "world_building",
        "floorplan": floorplan,
        "message": "floorplan set; next: add_asset (LOC with position) for world_building",
    }


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
        # 规范化空间布局（可选）；校验实体 asset_id 前缀与引用关系
        shot["spatial"] = _parse_shot_spatial(shot.get("spatial"), refs, shot.get("id"))

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
            "spatial": s.get("spatial") or {"characters": [], "props": [], "camera": {}},
            "status": "pending",
            "cinematography": {},
            "prompt": "",
            "image_urls": [],
            "video_path": "",
        }
        for s in shots
    ]
    story = kwargs.get("story") or {}
    require(isinstance(story, dict), "'story' must be an object")
    data["story"] = story
    data["stage"] = "script_analysis"
    store.save(project_id, data)
    # 生成可读的剧本资产文档（script.md），供用户后续查阅剧情
    store.write_text(project_id, "script.md", _render_script_md(data))
    return {
        "project_id": project_id,
        "stage": "script_analysis",
        "scenes": len(scenes),
        "shots": len(shots),
        "script_doc": "script.md",
        "message": (
            "script written (script.md generated); next: review_script, or "
            "request_user_review for an optional human review before proceeding"
        ),
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
        data["stage"] = "spatial_planning"
        store.save(project_id, data)
        return {
            "project_id": project_id, "stage": "spatial_planning", "passed": True,
            "message": "script review passed; next: set_floorplan (mandatory spatial planning)",
        }
    store.save(project_id, data)
    return {
        "project_id": project_id, "stage": "script_analysis", "passed": False,
        "message": "script review failed; revise write_script then review_script again",
    }


def request_user_review(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """进入可选用户审核（script_analysis → script_review），返回完整剧情供呈现。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "request_user_review")
    data = store.load(project_id)
    require_stage(data["stage"], "request_user_review", "script_analysis")

    data["stage"] = "script_review"
    data.setdefault("reviews", {})["user_review"] = {
        "kind": "user", "status": "pending", "approved": None, "note": "",
    }
    store.save(project_id, data)
    return {
        "project_id": project_id,
        "stage": "script_review",
        "story": data.get("story", {}),
        "scenes": data.get("scenes", []),
        "shots": [
            {"id": s.get("id"), "scene_id": s.get("scene_id"),
             "action": s.get("action") or "", "asset_refs": s.get("asset_refs") or []}
            for s in data.get("shots", [])
        ],
        "message": (
            "awaiting user review; present story to the user, then call "
            "approve_script with approved=true/false"
        ),
    }


def approve_script(store: ProjectStore, kwargs: dict[str, Any]) -> dict[str, Any]:
    """用户审核裁决：通过 → spatial_planning；打回 → script_analysis 重写。"""
    project_id = validate_project_id(kwargs.get("project_id"))
    check_role(kwargs.get("role"), "approve_script")
    data = store.load(project_id)
    require_stage(data["stage"], "approve_script", "script_review")

    approved = kwargs.get("approved")
    require(isinstance(approved, bool), "approve_script requires 'approved' as boolean")

    data.setdefault("reviews", {})["user_review"] = {
        "kind": "user", "approved": approved, "note": kwargs.get("note") or "",
    }
    if approved:
        data["stage"] = "spatial_planning"
    else:
        data["stage"] = "script_analysis"
    store.save(project_id, data)
    if approved:
        return {
            "project_id": project_id, "stage": "spatial_planning", "approved": True,
            "message": "user approved script; next: set_floorplan (mandatory spatial planning)",
        }
    return {
        "project_id": project_id, "stage": "script_analysis", "approved": False,
        "message": "user rejected script; revise write_script then request_user_review again",
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

    asset: dict[str, Any] = {
        "id": asset_id,
        "kind": kind,
        "name": kwargs.get("name"),
        "appearance": kwargs.get("appearance"),
        "reference_image": kwargs.get("reference_image"),
        "locked": False,
    }
    # LOC 空间字段：空间规划已强制，LOC 必须有坐标；朝向/占地/入口可选
    if kind == "LOC":
        require(
            kwargs.get("position") is not None,
            f"LOC {asset_id!r} requires 'position' (spatial planning is mandatory)",
        )
        asset["position"] = _require_point(kwargs.get("position"), f"{asset_id} position")
        if kwargs.get("orientation") is not None:
            asset["orientation"] = _require_orientation(kwargs.get("orientation"), f"{asset_id} orientation")
        if kwargs.get("footprint") is not None:
            asset["footprint"] = _require_footprint(kwargs.get("footprint"), f"{asset_id} footprint")
        if kwargs.get("entrance") is not None:
            asset["entrance"] = _require_point(kwargs.get("entrance"), f"{asset_id} entrance")
    data["assets"][asset_id] = asset
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

    # 空间一致性硬门：设了平面图后，LOC 落位 + 镜头站位必须在界内且不越界/穿墙
    floorplan = data.get("floorplan")
    if floorplan:
        loc_assets = [a for a in assets.values() if a.get("kind") == "LOC"]
        issues = validate_loc_placement(loc_assets, floorplan)
        issues += validate_shot_spatial(shot, assets, floorplan)
        require(not issues, "spatial inconsistency: " + "; ".join(issues))

    char_refs = [r for r in refs if r.startswith("CHAR")]
    loc_refs = [r for r in refs if r.startswith("LOC")]
    prop_refs = [r for r in refs if r.startswith("PROP")]

    cin = shot.get("cinematography") or {}
    bible = data.get("bible") or {}
    spatial = shot.get("spatial") or {}
    spatial_chars = {e["asset_id"]: e for e in spatial.get("characters", [])}

    lines: list[str] = []
    if char_refs:
        parts = []
        for r in char_refs:
            phrase = f"@{r} {assets[r]['appearance']}"
            ent = spatial_chars.get(r)
            if ent:
                phrase += _char_spatial_suffix(ent, shot, assets)
            parts.append(phrase)
        lines.append(" and ".join(parts))
    if loc_refs:
        lines.append(
            " and ".join(f"@{r} {assets[r]['appearance']}{_loc_spatial_suffix(assets[r])}" for r in loc_refs)
        )
    if prop_refs:
        lines.append("with " + " and ".join(f"@{r} {assets[r]['appearance']}" for r in prop_refs))
    lines.append(
        f"Camera: {cin.get('movement') or shot.get('camera') or ''}, "
        f"{cin.get('fps') or bible.get('fps') or 24}fps{_camera_spatial_suffix(spatial.get('camera') or {}, shot, assets)}"
    )
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
    """根据当前 11 阶段状态机，代码推算出下一步动作（供模型参考，而非自由发挥）。"""
    if data.get("completed"):
        return "done (all shots final)"
    stage = data.get("stage")
    if stage == "init":
        return "write_script"
    if stage == "script_analysis":
        return "review_script (or request_user_review for optional human review)"
    if stage == "script_review":
        return "approve_script (waiting for user review)"
    if stage == "spatial_planning":
        return "set_floorplan (mandatory spatial planning)"
    if stage == "world_building":
        return "add_asset (LOC/PROP with position) for every world asset"
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
