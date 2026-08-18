"""AI 导演工作流常量与权限矩阵。

职责与项目角色：
- 定义资产类型、ID/项目 ID 正则、11 阶段、镜头状态、画幅、质检阈值；
- 定义全部动作（action）与角色（role），以及角色→动作的权限矩阵。
"""

from __future__ import annotations

import re  # 资产 ID 与项目 ID 的 slug 校验

# 资产类型：角色 / 场景 / 道具（风格归入 bible.style，不再单列资产）
_ASSET_KINDS = ("CHAR", "LOC", "PROP")
# 资产 ID 格式：前缀 + 3 位数字，如 CHAR001、LOC001
_ASSET_ID_RE = re.compile(r"^(CHAR|LOC|PROP)(\d{3})$")
# 项目 ID：小写 slug（字母/数字/连字符/下划线），杜绝路径穿越
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# 项目 11 阶段状态机（文档规定的宏观流水线）
_STAGES = (
    "init",
    "script_analysis",
    "script_review",
    "spatial_planning",
    "world_building",
    "character_design",
    "asset_lock",
    "storyboard",
    "video_generation",
    "quality_check",
    "final_edit",
)

# 镜头状态机（镜头级，贯穿 video_generation → final_edit）
_SHOT_STATUSES = ("pending", "prompted", "qc_pass", "qc_fail", "final")

# 质检默认阈值：任一一致性分数低于此值即判「不通过」
_QC_THRESHOLD = 85

# 画幅可选值（与 generate_video 对齐）
_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")

# 全部 action（供 schema enum 与分派使用）
_ACTIONS = (
    "create_project",
    "set_floorplan",
    "write_script",
    "review_script",
    "request_user_review",
    "approve_script",
    "add_asset",
    "review_assets",
    "lock_assets",
    "plan_shot",
    "compile_prompt",
    "record_qc",
    "record_shot_result",
    "status",
)

# 角色（文档第六节「Agent 权限隔离」的映射）
_ROLES = ("director", "script", "world", "character", "asset", "video", "editor")

# 权限矩阵：角色 -> 允许的动作集合。
# 注意：这是审计 / 软校验边界（调用者可自报角色），**不是安全边界**；
# 真正的隔离靠 _scopes={"core"}（子代理无法调用本工具）与数据冻结门。
_ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "director": frozenset(_ACTIONS),
    "script": frozenset({"write_script", "review_script", "request_user_review", "approve_script", "status"}),
    "world": frozenset({"set_floorplan", "add_asset", "status"}),
    "character": frozenset({"add_asset", "status"}),
    "asset": frozenset({"add_asset", "review_assets", "lock_assets", "status"}),
    "video": frozenset({"plan_shot", "compile_prompt", "record_qc", "status"}),
    "editor": frozenset({"record_shot_result", "status"}),
}
