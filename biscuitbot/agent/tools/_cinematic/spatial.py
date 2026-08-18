"""AI 导演空间几何 / 一致性校验 / 方位短语。

职责与项目角色：
- 定义 2D 俯视平面图的几何原语（点、占地 AABB、重叠/包含/线段相交）；
- 把坐标 / 朝向角翻译成自然语言方位短语（供 ``compile_prompt`` 注入 prompt）；
- 提供空间一致性硬校验（界内、建筑不叠放、人物/道具不悬空、运动不穿墙），
  返回问题列表（空列表 = 通过），由 ``compile_prompt`` 决定是否拒绝。

坐标系约定：2D 俯视平面图，原点 ``(0,0)`` 在西南角，``+x`` 向东、``+y`` 向北
（北在上）。朝向角 ``0=北 / 90=东 / 180=南 / 270=西``，逆时针减小。

注意：本模块为纯函数，无 I/O、无状态，可被 workflow 与测试直接调用。
"""

from __future__ import annotations

import math  # 距离 / 角度计算
from typing import Any

# 8 方位词（按 45° 分扇区，索引 0=北，顺时针递增）
_DIRECTIONS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


# ---------------------------------------------------------------------------
# 方位短语（坐标 / 朝向 → 自然语言）
# ---------------------------------------------------------------------------


def compass_word(deg: float | int | None) -> str:
    """朝向角 → 8 方位词（0=北，90=东）。``None`` 返回空串。"""
    if deg is None:
        return ""
    angle = float(deg) % 360.0
    idx = int(angle / 45.0 + 0.5) % 8
    return _DIRECTIONS[idx]


def relative_position_word(point: dict[str, Any], loc: dict[str, Any]) -> str:
    """人物坐标相对 LOC 占地中心的 9 格方位（中央 / 北侧 / 东北角 / …）。"""
    cx = float(loc["position"]["x"])
    cy = float(loc["position"]["y"])
    fp = loc.get("footprint") or {}
    w = float(fp.get("width", 0) or 0)
    d = float(fp.get("depth", 0) or 0)
    dx = float(point["x"]) - cx
    dy = float(point["y"]) - cy
    if w <= 0 or d <= 0:
        return "中央"  # 无占地信息，退化为中央
    hw, hd = w / 2.0, d / 2.0
    ns = "" if abs(dy) <= hd / 3.0 else ("北" if dy > 0 else "南")
    ew = "" if abs(dx) <= hw / 3.0 else ("东" if dx > 0 else "西")
    if not ns and not ew:
        return "中央"
    if not ns:
        return ew + "侧"
    if not ew:
        return ns + "侧"
    return ew + ns + "角"  # 中文方位词序：东南/东北/西南/西北（东西在前）


def bearing_word(origin: dict[str, Any], target: dict[str, Any]) -> str:
    """从 origin 到 target 的方位 + 距离，如「东南方 15 米」。"""
    dx = float(target["x"]) - float(origin["x"])
    dy = float(target["y"]) - float(origin["y"])
    dist = math.hypot(dx, dy)
    deg = math.degrees(math.atan2(dx, dy))  # atan2(dx, dy)：0=北，90=东
    return f"{compass_word(deg)}方 {dist:.0f} 米"


# ---------------------------------------------------------------------------
# 几何原语
# ---------------------------------------------------------------------------


def point_in_bounds(point: dict[str, Any], floorplan: dict[str, Any]) -> bool:
    """点是否落在平面图用地范围内。"""
    x = float(point["x"])
    y = float(point["y"])
    return 0.0 <= x <= float(floorplan["width"]) and 0.0 <= y <= float(floorplan["length"])


def footprint_aabb(loc: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """LOC 中心 + 占地尺寸 → AABB ``(minx, miny, maxx, maxy)``；无占地返回 None。"""
    fp = loc.get("footprint")
    if not fp:
        return None
    w = float(fp.get("width", 0) or 0)
    d = float(fp.get("depth", 0) or 0)
    if w <= 0 or d <= 0:
        return None
    cx = float(loc["position"]["x"])
    cy = float(loc["position"]["y"])
    return (cx - w / 2.0, cy - d / 2.0, cx + w / 2.0, cy + d / 2.0)


def aabb_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """两个 AABB 是否相交（建筑不叠放检测）。"""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def point_in_aabb(point: dict[str, Any], aabb: tuple[float, float, float, float]) -> bool:
    """点是否落在 AABB 内（含边界）。"""
    x = float(point["x"])
    y = float(point["y"])
    return aabb[0] <= x <= aabb[2] and aabb[1] <= y <= aabb[3]


def segment_crosses_aabb(
    p1: dict[str, Any], p2: dict[str, Any], aabb: tuple[float, float, float, float]
) -> bool:
    """线段 (p1, p2) 是否与 AABB 相交（Liang-Barsky 裁剪）。"""
    x0, y0 = float(p1["x"]), float(p1["y"])
    x1, y1 = float(p2["x"]), float(p2["y"])
    xmin, ymin, xmax, ymax = aabb
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return False
                if r < t1:
                    t1 = r
    return t0 <= t1


# ---------------------------------------------------------------------------
# 空间一致性硬校验（返回问题列表，空列表 = 通过）
# ---------------------------------------------------------------------------


def validate_floorplan(fp: Any) -> list[str]:
    """校验平面图对象：width/depth 为正数。"""
    if not isinstance(fp, dict):
        return ["floorplan must be an object"]
    issues: list[str] = []
    w, d = fp.get("width"), fp.get("length")
    if not isinstance(w, (int, float)) or w <= 0:
        issues.append("floorplan requires positive numeric 'width'")
    if not isinstance(d, (int, float)) or d <= 0:
        issues.append("floorplan requires positive numeric 'length'")
    return issues


def validate_loc_placement(locs: list[dict[str, Any]], fp: dict[str, Any]) -> list[str]:
    """校验所有 LOC：中心/占地四角在界内、两两占地不重叠（建筑不叠放）。"""
    issues: list[str] = []
    for loc in locs:
        pos = loc.get("position")
        if not isinstance(pos, dict) or "x" not in pos or "y" not in pos:
            issues.append(f"{loc.get('id')} missing position (floorplan set)")
            continue
        if not point_in_bounds(pos, fp):
            issues.append(
                f"{loc.get('id')} position {pos} out of bounds {fp['width']}x{fp['length']}"
            )
        aabb = footprint_aabb(loc)
        if aabb and (aabb[0] < 0 or aabb[1] < 0 or aabb[2] > float(fp["width"]) or aabb[3] > float(fp["length"])):
            issues.append(f"{loc.get('id')} footprint exceeds floorplan bounds")
    placed = [loc for loc in locs if footprint_aabb(loc) is not None]
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            if aabb_overlap(footprint_aabb(placed[i]), footprint_aabb(placed[j])):  # type: ignore[arg-type]
                issues.append(
                    f"LOC footprints overlap: {placed[i]['id']} and {placed[j]['id']}"
                )
    return issues


def _motion_crosses_other_loc(entity: dict[str, Any], locs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """运动路径是否穿过「非起点所在」的 LOC 占地；返回阻挡的 LOC，否则 None。"""
    start = entity["position"]
    end = entity["motion_to"]
    containing = [
        loc
        for loc in locs
        if (aabb := footprint_aabb(loc)) is not None and point_in_aabb(start, aabb)
    ]
    for loc in locs:
        aabb = footprint_aabb(loc)
        if aabb is None or loc in containing:
            continue
        if segment_crosses_aabb(start, end, aabb):
            return loc
    return None


def validate_shot_spatial(
    shot: dict[str, Any], assets: dict[str, Any], fp: dict[str, Any]
) -> list[str]:
    """校验单个镜头：人物/道具/机位在界内、人物/道具落在引用 LOC 占地内、运动不穿墙。"""
    issues: list[str] = []
    spatial = shot.get("spatial") or {}
    refs = shot.get("asset_refs", [])
    loc_refs = [r for r in refs if r.startswith("LOC")]

    # 引用 LOC 的占地；若任一引用 LOC 无占地，视为存在开阔地形，放宽包含性校验
    loc_aabbs = [(r, footprint_aabb(assets[r])) for r in loc_refs if r in assets]
    open_terrain = any(aabb is None for _, aabb in loc_aabbs)
    footprint_aabbs = [aabb for _, aabb in loc_aabbs if aabb is not None]

    characters = spatial.get("characters") or []
    props = spatial.get("props") or []
    entities = list(characters) + list(props)

    if not characters:
        issues.append(f"shot {shot.get('id')} missing spatial.characters (floorplan set)")

    all_locs = [a for a in assets.values() if a.get("kind") == "LOC"]

    for ent in entities:
        aid = ent.get("asset_id")
        pos = ent.get("position")
        if not aid or not isinstance(pos, dict) or "x" not in pos or "y" not in pos:
            issues.append("spatial entity requires 'asset_id' and numeric 'position'")
            continue
        if not point_in_bounds(pos, fp):
            issues.append(f"{aid} position {pos} out of bounds")
        # 包含性：当所有引用 LOC 都带占地时，人物/道具必须落在其一之内
        if footprint_aabbs and not open_terrain:
            if not any(point_in_aabb(pos, aabb) for aabb in footprint_aabbs):
                issues.append(f"{aid} position {pos} is outside every referenced LOC footprint")
        # 穿墙：运动直线不得穿过其它（非起点所在）LOC 占地
        if ent.get("motion_to") and all_locs:
            blocker = _motion_crosses_other_loc(ent, all_locs)
            if blocker:
                issues.append(f"{aid} motion path crosses LOC {blocker['id']} footprint")

    camera = spatial.get("camera")
    if isinstance(camera, dict):
        for key in ("position", "target"):
            p = camera.get(key)
            if isinstance(p, dict) and "x" in p and "y" in p and not point_in_bounds(p, fp):
                issues.append(f"camera {key} out of bounds")

    return issues
