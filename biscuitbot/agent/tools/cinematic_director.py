"""AI 导演工作流工具门面（cinematic_director）。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的影视生产编排组件。它只做
**工具门面**（Tool 子类：name/description/parameters/execute/create），真正的
状态机、校验、存储分别由 ``_cinematic/`` 子包的 ``workflow`` / ``validators`` /
``state`` / ``schemas`` 承担。

把「AI 导演」工作流（导演圣经 → 剧本 → 剧本审核 → **（可选用户审核）** →
**空间规划** → 世界观/角色资产 → 资产审核 → 资产锁定 → 分镜 → 视频生成 →
视频审核 → 成片）固化为**代码层面的 11 阶段状态机与硬校验**，而非靠 Agent
自觉执行：阶段顺序、审核 Gate、数据冻结全部由代码强制，非法操作被拒绝，从而
解决 AI 视频「人物/场景/光影漂移」与「流程偏移」。空间规划是**强制阶段**——
生成资产前必须先声明平面图并锁定坐标。

设计要点：
- 单工具多 action：通过 ``action`` 参数分派，共享同一份项目状态；
- 11 阶段状态机：``init → script_analysis → script_review → spatial_planning
  → world_building → character_design → asset_lock → storyboard
  → video_generation → quality_check → final_edit``；
- 剧本师步骤产出 ``story``（整体剧情 + 人物/环境描述词 + 空间/大局描述词），
  并可经 ``request_user_review``/``approve_script`` 走**可选**用户审核门；
- 三级审核 Gate：剧本审核 / 资产审核 / 视频审核，低于阈值或否决即硬阻塞；
- 权限隔离：``_scopes={"core"}`` 使子 Agent 无法调用本工具（只有主 Agent/导演
  能改项目状态），资产与剧本在审核/锁定后**冻结**，且无「编辑/删除」动作；
- 分目录记忆：``workspace/cinematic/<project_id>/{bible.json, characters/,
  locations/, props/, shots/, videos/, reviews/}``。

边界：本工具**不生成视频/图片/音频**，只产出结构化方案、校验门、规范化
prompt 与记录；视频/图片/配音分别由 ``generate_video`` / ``generate_image`` /
``text_to_speech`` 等工具执行。
"""

from __future__ import annotations

import json  # 结果 JSON 序列化
from pathlib import Path  # 工作区路径
from typing import Any

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器

from ._cinematic import workflow  # 状态机与动作实现
from ._cinematic.constants import _ACTIONS  # 动作枚举
from ._cinematic.schemas import parameters_schema  # 参数 schema
from ._cinematic.state import ProjectStore  # 分目录存储
from ._cinematic.validators import CinematicDirectorError  # 校验错误


@tool_parameters(parameters_schema())
class CinematicDirectorTool(Tool):
    """AI 导演工作流编排与管控工具。

    通过 ``action`` 驱动一个受 11 阶段状态机约束的项目：先建项目、写剧本（含
    story 描述词）、剧本审核、（可选用户审核）、空间规划（强制）、锁定世界观/角色
    资产、资产审核、资产锁定、分镜、编译 Prompt、生成视频、视频审核、记录成片。
    非法跳阶段或审核不过都会被拒绝。
    """

    _scopes = {"core"}  # 仅主 Agent 可用；子 Agent（subagent）无法调用 → 权限隔离
    _capability = (
        "Produce AI short-drama / long-form videos: turn a novel, story, or "
        "script into a filmed multi-scene work (bible → script → optional user "
        "review → spatial planning → world/character assets → storyboard → "
        "video generation → review → final) enforced by a hard 11-stage state "
        "machine, plus optional first-frame continuity across shots."
    )
    _usage_md = "docs/cinematic_director.md"  # 使用说明文档路径

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例，仅需工作区路径。"""
        return cls(workspace=Path(ctx.workspace))

    def __init__(self, *, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~

    @property
    def name(self) -> str:
        """工具名称。"""
        return "cinematic_director"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Use when the user wants to produce an AI short-drama / long-form "
            "video, or to adapt a novel, story, or script into a filmed "
            "multi-scene work. Orchestrate the full production with a hard "
            "11-stage state machine and review gates "
            "(script/optional-user/assets/video). Actions: "
            "create_project, set_floorplan, write_script, review_script, "
            "request_user_review, approve_script, add_asset, review_assets, "
            "lock_assets, plan_shot, compile_prompt, record_qc, record_shot_result, "
            "record_shot_frame, attach_audio, attach_spatial_map, status. It blocks "
            "skipping stages, requires a reference image for every asset, freezes "
            "script/assets after review/lock, and blocks recording a shot result "
            "until video QC passes. write_script should include a rich 'story' "
            "(plot + character/environment prompts + spatial/worldview "
            "descriptions); after writing, ask the user whether they want to review "
            "the story — if yes, request_user_review then approve_script once the "
            "user confirms, else proceed via review_script. A mandatory "
            "spatial_planning stage (set_floorplan) must complete before assets are "
            "generated, so every LOC carries coordinates and every shot blocking is "
            "validated at compile_prompt. compile_prompt assembles the full video "
            "reference set (image_urls = optionally the previous shot's last frame "
            "as first-frame reference when continuity=true, then spatial map image, "
            "then character/scene reference images, plus audio_urls). First-frame "
            "continuity is OPTIONAL: set continuity=true only for shots that "
            "continue directly from the previous shot; for scene cuts / new plot "
            "segments omit it (continuity=false), since reusing the previous frame "
            "would be meaningless. Read docs/cinematic_director.md first."
        )

    # ------------------------------------------------------------------
    # execute 与分派
    # ------------------------------------------------------------------

    async def execute(self, action: str, **kwargs: Any) -> str:
        """执行指定动作，返回 JSON 字符串结果（ok/error + 数据）。"""
        if action not in _ACTIONS:
            return self._result(False, error=f"unknown action '{action}'. Valid: {list(_ACTIONS)}")
        handler = getattr(workflow, action)
        store = ProjectStore(self.workspace)
        try:
            data = handler(store, kwargs)
        except CinematicDirectorError as exc:
            return self._result(False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — 作为工具结果返回，绝不抛出
            return self._result(False, error=f"{type(exc).__name__}: {exc}")
        return self._result(True, **data)

    @staticmethod
    def _result(ok: bool, *, error: str | None = None, **data: Any) -> str:
        """把结果序列化为 JSON 字符串。"""
        payload: dict[str, Any] = {"ok": ok}
        if error is not None:
            payload["error"] = error
        payload.update(data)
        return json.dumps(payload, ensure_ascii=False)
