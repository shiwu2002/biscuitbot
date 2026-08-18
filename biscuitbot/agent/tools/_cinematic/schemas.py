"""AI 导演工作流 JSON Schema 片段。

职责与项目角色：
- 定义场景 / 分镜的嵌套结构 schema；
- 组装 ``@tool_parameters`` 需要的根参数 schema（action 枚举 + 各动作字段）。
"""

from __future__ import annotations

from typing import Any

from biscuitbot.agent.tools.schema import (  # schema 构造器
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)

from .constants import _ACTIONS, _ASSET_KINDS, _RATIOS, _ROLES

# 场景元素结构
_SCENE_SCHEMA = ObjectSchema(
    id=StringSchema("场景 ID，如 Scene001"),
    location=StringSchema("地点"),
    time=StringSchema("时间"),
    function=StringSchema("功能（叙事作用）"),
    duration=IntegerSchema(description="时长（秒）", minimum=1, nullable=True),
    required=["id"],
)

# 空间子结构（2D 俯视平面图，单位米；原点 (0,0) 西南角，+x 东、+y 北）
_POINT_SCHEMA = ObjectSchema(
    x=NumberSchema(description="x 坐标（米，东向为正）"),
    y=NumberSchema(description="y 坐标（米，北向为正）"),
    required=["x", "y"],
)

_FOOTPRINT_SCHEMA = ObjectSchema(
    width=NumberSchema(description="占地宽度（米）", minimum=0),
    depth=NumberSchema(description="占地深度（米）", minimum=0),
    required=["width", "depth"],
)

# 镜头内人物 / 道具站位（position 为站位坐标，facing 为朝向角，motion_to 为运动终点）
_SPATIAL_ENTITY_SCHEMA = ObjectSchema(
    asset_id=StringSchema("资产 ID，如 CHAR001"),
    position=_POINT_SCHEMA,
    facing=NumberSchema(description="朝向角（0=北，90=东）", minimum=0, maximum=360, nullable=True),
    motion_to=_POINT_SCHEMA,
    required=["asset_id", "position"],
)

_SPATIAL_CAMERA_SCHEMA = ObjectSchema(
    position=_POINT_SCHEMA,
    target=_POINT_SCHEMA,
    facing=NumberSchema(description="机位朝向角（0=北，90=东）", minimum=0, maximum=360, nullable=True),
)

# 镜头空间布局（人物 / 道具 / 机位坐标，供空间一致性校验与 prompt 方位短语）
_SHOT_SPATIAL_SCHEMA = ObjectSchema(
    characters=ArraySchema(_SPATIAL_ENTITY_SCHEMA, description="本镜头人物站位"),
    props=ArraySchema(_SPATIAL_ENTITY_SCHEMA, description="本镜头道具站位"),
    camera=_SPATIAL_CAMERA_SCHEMA,
)

# 分镜元素结构（叙事层，摄影参数由 plan_shot 在 storyboard 阶段补充）
_SHOT_SCHEMA = ObjectSchema(
    id=StringSchema("镜头 ID，如 Shot001"),
    scene_id=StringSchema("所属场景 ID"),
    shot_size=StringSchema("景别（大全景/特写/中景…）"),
    camera=StringSchema("镜头/运镜（无人机下降/跟随/手持…）"),
    lens=StringSchema("焦段，如 24mm"),
    action=StringSchema("动作"),
    emotion=StringSchema("情绪"),
    sound=StringSchema("声音"),
    asset_refs=ArraySchema(
        StringSchema("资产 ID，如 CHAR001 / LOC001"),
        description="本镜头引用的资产 ID 列表（需含至少一个 CHAR 与一个 LOC）",
    ),
    spatial=_SHOT_SPATIAL_SCHEMA,
    required=["id", "scene_id", "asset_refs"],
)

# 故事/剧本补充层（write_script 的 story 字段：整体剧情 + 各类描述词）
_STORY_ITEM_SCHEMA = ObjectSchema(
    id=StringSchema("资产 ID，如 CHAR001 / LOC001"),
    name=StringSchema("名称"),
    prompt=StringSchema("稳定描述提示词（后续 add_asset 的 appearance 来源）"),
    required=["id", "prompt"],
)

_STORY_SCHEMA = ObjectSchema(
    plot=StringSchema("整体剧情（含分幕/情节梗概）"),
    characters=ArraySchema(_STORY_ITEM_SCHEMA, description="人物描述提示词列表"),
    environments=ArraySchema(_STORY_ITEM_SCHEMA, description="环境描述提示词列表"),
    spatial=StringSchema("空间描述词（整体空间布局文字描述）"),
    worldview=StringSchema("大局描述词（世界观/风格/氛围/主题）"),
)


def parameters_schema() -> dict[str, Any]:
    """构建工具根参数 schema（供 ``@tool_parameters`` 使用）。"""
    return tool_parameters_schema(
        action=StringSchema(
            "要执行的工作流动作。先读 docs/cinematic_director.md 了解各动作参数与状态机。",
            enum=list(_ACTIONS),
        ),
        project_id=StringSchema("项目 ID（小写 slug）。", nullable=True),
        role=StringSchema("执行角色（审计用，默认 director）。", enum=list(_ROLES), nullable=True),
        # ---- create_project ----
        name=StringSchema("项目名。", nullable=True),
        logline=StringSchema("一句话梗概。", nullable=True),
        style=StringSchema("视觉风格（写实电影/国漫/3D…）。", nullable=True),
        ratio=StringSchema("画幅比例。", enum=list(_RATIOS), nullable=True),
        fps=IntegerSchema(description="全片基准帧率。", minimum=1, maximum=120, nullable=True),
        color_palette=StringSchema("色彩体系（主色/辅助色/光影规则）。", nullable=True),
        reference_works=StringSchema("对标影视作品。", nullable=True),
        # ---- set_floorplan ----
        unit=StringSchema("坐标单位（默认 meter）。", nullable=True),
        width=NumberSchema(description="用地宽度（米，x 轴范围 [0,width]）。", minimum=0, nullable=True),
        length=NumberSchema(description="用地深度（米，y 轴范围 [0,length]）。", minimum=0, nullable=True),
        # ---- write_script ----
        scenes=ArraySchema(_SCENE_SCHEMA, description="场景列表。", nullable=True),
        shots=ArraySchema(_SHOT_SCHEMA, description="分镜列表。", nullable=True),
        story=_STORY_SCHEMA,
        # ---- review_script / review_assets（审核 Gate）----
        score=IntegerSchema(description="审核分数（0-100）。", minimum=0, maximum=100, nullable=True),
        passed=BooleanSchema(description="是否通过（可选，False 表示一票否决）。", nullable=True),
        note=StringSchema("审核备注 / 重生成建议。", nullable=True),
        reviewer=StringSchema("审核人（员工代号或姓名，记录是谁审的，可选）。", nullable=True),
        threshold=IntegerSchema(description="通过阈值（默认 85）。", minimum=0, maximum=100, nullable=True),
        # ---- request_user_review / approve_script（可选用户审核）----
        approved=BooleanSchema(description="用户是否批准剧本（approve_script 必填）。", nullable=True),
        # ---- add_asset ----
        kind=StringSchema("资产类型。", enum=list(_ASSET_KINDS), nullable=True),
        id=StringSchema("资产 ID，如 CHAR001。", nullable=True),
        appearance=StringSchema("稳定外观短语（prompt 直接引用的、保持一致的描述）。", nullable=True),
        reference_image=StringSchema("参考图路径（generate_image 生成的本地路径）。", nullable=True),
        # ---- add_asset 空间字段（仅 LOC；设平面图后 position 必填）----
        position=_POINT_SCHEMA,
        orientation=NumberSchema(description="建筑朝向角（0=北，90=东）。", minimum=0, maximum=360, nullable=True),
        footprint=_FOOTPRINT_SCHEMA,
        entrance=_POINT_SCHEMA,
        # ---- plan_shot ----
        shot_id=StringSchema("镜头 ID，如 Shot001。", nullable=True),
        camera=StringSchema("摄影机/机位。", nullable=True),
        lens=StringSchema("焦段。", nullable=True),
        movement=StringSchema("运镜（Tracking Shot/Handheld…）。", nullable=True),
        depth=StringSchema("景深（浅景深/深景深）。", nullable=True),
        lighting=StringSchema("光影。", nullable=True),
        # ---- record_qc ----
        character_score=IntegerSchema(description="人物一致度（0-100）。", minimum=0, maximum=100, nullable=True),
        scene_score=IntegerSchema(description="场景一致度（0-100）。", minimum=0, maximum=100, nullable=True),
        action_score=IntegerSchema(description="动作一致度（0-100）。", minimum=0, maximum=100, nullable=True),
        # ---- record_shot_result ----
        video_path=StringSchema("成片本地路径。", nullable=True),
        required=["action"],
    )
