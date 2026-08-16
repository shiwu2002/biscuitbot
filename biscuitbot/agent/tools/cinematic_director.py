"""AI 导演工作流工具门面（cinematic_director）。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的影视生产编排组件。它只做
**工具门面**（Tool 子类：name/description/parameters/execute/create），真正的
状态机、校验、存储分别由 ``_cinematic/`` 子包的 ``workflow`` / ``validators`` /
``state`` / ``schemas`` 承担。

把「AI 导演」工作流（导演圣经 → 剧本 → 剧本审核 → 世界观/角色资产 → 资产审核
→ 资产锁定 → 分镜 → 视频生成 → 视频审核 → 成片）固化为**代码层面的 9 阶段状态
机与硬校验**，而非靠 Agent 自觉执行：阶段顺序、三级审核 Gate、数据冻结全部由
代码强制，非法操作被拒绝，从而解决 AI 视频「人物/场景/光影漂移」与「流程偏移」。

设计要点：
- 单工具多 action：通过 ``action`` 参数分派，共享同一份项目状态；
- 9 阶段状态机：``init → script_analysis → world_building → character_design
  → asset_lock → storyboard → video_generation → quality_check → final_edit``；
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

    通过 ``action`` 驱动一个受 9 阶段状态机约束的项目：先建项目、写剧本、剧本
    审核、锁定世界观/角色资产、资产审核、资产锁定、分镜、编译 Prompt、生成视频、
    视频审核、记录成片。非法跳阶段或审核不过都会被拒绝。
    """

    _scopes = {"core"}  # 仅主 Agent 可用；子 Agent（subagent）无法调用 → 权限隔离
    _capability = (
        "Orchestrate an AI film production pipeline (bible → script → review → "
        "world/character assets → asset lock → storyboard → video generation → "
        "video review → final) with a hard 9-stage state machine."
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
            "Orchestrate an AI film production project with a hard 9-stage state "
            "machine and three review gates (script/assets/video). Actions: "
            "create_project, write_script, review_script, add_asset, review_assets, "
            "lock_assets, plan_shot, compile_prompt, record_qc, record_shot_result, "
            "status. It blocks skipping stages, requires a reference image for every "
            "asset, freezes script/assets after review/lock, and blocks recording a "
            "shot result until video QC passes. Read docs/cinematic_director.md first."
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
