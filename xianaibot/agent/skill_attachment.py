"""对话界面「本轮指定技能」的附加与注入。

用户在输入框用 ``/`` 菜单选中技能后，前端把技能名随消息一起发来（envelope 的
``skills`` 字段），经 ``webui.skills_api.normalize_skill_mentions`` 归一化后落到
``InboundMessage.metadata["skills"]``。本模块负责其中两件事：

- :func:`session_extra`：把这份选择持久化进会话记录，回放时仍看得到本轮挂过技能；
- :func:`runtime_lines`：把技能正文（``SKILL.md``，已剥掉 frontmatter）连同
  「本轮必须使用」的声明拼进**用户消息尾部**的运行时上下文块。

为什么是「强制」：``/skill <名字>`` 这条命令式路径只回一段预览、由命令层直接结束
本轮（不经过模型），所以模型并不会真的用上那个技能。这里换成把整段正文喂进本轮
上下文并明确要求按它执行——代价是上下文变长，因此只在用户显式选择时发生，且单
技能正文按 :data:`_MAX_SKILL_BODY_CHARS` 截断。

安全边界：技能名必须先与 :meth:`SkillsLoader.list_skills` 的现有技能名对上，越界
的名字直接丢弃、不参与任何路径拼接。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from xianaibot.agent.skills import SkillsLoader
from xianaibot.utils.helpers import truncate_text

# 技能名白名单：与前端归一化（``webui/skills_api.normalize_skill_mentions``）同款，
# 也正是工作区技能目录名的安全字符集。
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$", re.IGNORECASE)
# 与 normalize_skill_mentions 的上限保持一致：注入端也不接受更多。
_MAX_SKILL_ATTACHMENTS = 8
# 技能正文通常几百到几千字；超长部分截断，避免单个技能把上下文预算吃光。
_MAX_SKILL_BODY_CHARS = 12_000


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回本轮指定技能的持久化参数（供会话记录保留）。"""
    skills = metadata.get("skills") if isinstance(metadata, Mapping) else None
    return {"skills": skills} if isinstance(skills, list) and skills else {}


def runtime_lines(message: Any, workspace: Path, *, skip: bool = False) -> list[str]:
    """返回模型可见的技能注入行（含技能正文），供拼进用户消息尾部。"""
    if skip:
        return []
    metadata = message.metadata if isinstance(getattr(message, "metadata", None), Mapping) else None
    return _skill_attachment_lines(metadata, workspace)


def _requested_skill_names(metadata: Mapping[str, Any] | None) -> list[str]:
    """从会话元数据里取出本轮指定的技能名（过滤非法项并去重，保持选择顺序）。"""
    structured = metadata.get("skills") if isinstance(metadata, Mapping) else None
    if not isinstance(structured, list):
        return []
    names: list[str] = []
    for item in structured:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if not name or not _SKILL_NAME_RE.match(name):
            continue
        lowered = name.lower()
        if lowered not in names:
            names.append(lowered)
        if len(names) >= _MAX_SKILL_ATTACHMENTS:
            break
    return names


def _skill_attachment_lines(
    metadata: Mapping[str, Any] | None,
    workspace: Path,
) -> list[str]:
    names = _requested_skill_names(metadata)
    if not names:
        return []
    try:
        loader = SkillsLoader(workspace)
        # 白名单：只认当前真实存在的技能。名字对不上就丢弃，绝不按名字拼路径去读文件。
        entries = {
            entry["name"]: entry
            for entry in loader.list_skills(filter_unavailable=False)
        }
    except Exception:  # 技能目录异常不该拖垮本轮对话
        logger.debug("skill attachment: 枚举可用技能失败", exc_info=True)
        return []
    lines: list[str] = []
    for name in names:
        entry = entries.get(name)
        if entry is None:
            continue
        # 复用加载器自带的 frontmatter 剥离与 ``### Skill: <name>`` 分节格式，
        # 与「技能正文」在别处注入时的样子保持一致。
        body = loader.load_skills_for_context([name])
        if not body:
            continue
        lines.append(
            "Skill Attachment: "
            f"{name} (user-selected for this turn; file={entry.get('path') or 'unknown'}). "
            "MANDATORY for the current turn: follow the skill body below as the primary "
            "procedure for this request; do not search for or switch to another skill."
            f"\n\n{truncate_text(body, _MAX_SKILL_BODY_CHARS)}"
        )
    return lines
