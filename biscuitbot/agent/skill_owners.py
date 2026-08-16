"""数字人员工「自带技能」（bundled skills）的归属关系与文件落盘。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，负责记录哪些工作区技能是「随数字人员工
一起从人才市场下载」的，以及把这些技能的文件安全落盘 / 删除。

背景与规则：
- 数字人员工目录（``employees.py``）里的 ``skills`` 字段只是一串技能名，
  无法区分「员工引用的内置技能」与「员工自带、随员工一起下载的技能」；
- 人才市场下载一名员工时，若该员工携带技能（含 SKILL.md 等文件），需要一并
  写入 ``workspace/skills/<name>/``，并记录该技能归属于这名员工；
- 业务规则：员工被删除时，其自带技能应自动删除；员工未删除时，其自带技能
  不可被单独删除（避免员工仍在却失去其专属技能）。

归属关系持久化在 ``workspace/skill_owners.json``，形如：:

    {"schema_version": 1, "owners": {"<skill_name>": "<employee_id>"}}

仅记录「自带技能」的归属；未在 registry 中出现的技能视为普通工作区技能。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

# 技能名允许的字符：小写字母、数字、-、_（slug），与 skills 目录命名约定一致。
_VALID_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# 技能文件相对路径允许的字符（单个路径段内）：字母数字、点、下划线、连字符。
_VALID_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# 归属注册表文件名（位于工作区根目录）
_SKILL_OWNERS_FILE = "skill_owners.json"
# 工作区技能目录名
_SKILLS_DIR = "skills"


class SkillOwnershipError(Exception):
    """技能归属 / 落盘校验失败（含 HTTP 状态码）。"""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def is_safe_skill_name(name: str) -> bool:
    """技能名是否为安全 slug（无路径分隔符 / 非法字符）。"""
    return bool(name) and _VALID_SKILL_NAME.match(name) is not None


def _skills_dir(workspace: Path) -> Path:
    return workspace / _SKILLS_DIR


class SkillOwnershipStore:
    """工作区技能归属注册表（skill_name -> employee_id）。

    典型用法：
    - 人才市场安装员工后 ``set_owner`` 记录其自带技能；
    - ``delete_workspace_skill`` 通过 ``owner_of`` 判断技能是否被某位在职员工占用；
    - ``EmployeeStore.delete_employee`` 通过 ``clear_employee`` 级联清理并删除自带技能。
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace  # 工作区根目录
        self.path = workspace / _SKILL_OWNERS_FILE
        self._lock = threading.Lock()

    # ---- 读 ---------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        owners = data.get("owners") if isinstance(data, dict) else None
        if not isinstance(owners, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in owners.items()
            if isinstance(k, str) and k and isinstance(v, str) and v
        }

    # ---- 写 ---------------------------------------------------------------

    def _write(self, owners: dict[str, str]) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "owners": owners}
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(encoded)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ---- 查询 / 修改 ------------------------------------------------------

    def owner_of(self, skill_name: str) -> str | None:
        """返回技能归属的员工 id；未记录归属时返回 None。"""
        with self._lock:
            return self._load().get(skill_name)

    def set_owner(self, skill_name: str, employee_id: str) -> None:
        """记录技能归属（idempotent）。"""
        with self._lock:
            owners = self._load()
            owners[skill_name] = employee_id
            self._write(owners)

    def set_owners(self, employee_id: str, skill_names: list[str]) -> None:
        """批量记录多名技能归属到同一员工。"""
        with self._lock:
            owners = self._load()
            for name in skill_names:
                owners[name] = employee_id
            self._write(owners)

    def remove_owner(self, skill_name: str) -> None:
        """移除某技能的归属记录（不删除技能文件）。"""
        with self._lock:
            owners = self._load()
            if skill_name in owners:
                del owners[skill_name]
                self._write(owners)

    def skills_for(self, employee_id: str) -> list[str]:
        """返回某员工拥有的技能名列表（排序）。"""
        with self._lock:
            owners = self._load()
            return sorted(k for k, v in owners.items() if v == employee_id)

    def clear_employee(self, employee_id: str) -> list[str]:
        """移除某员工的所有归属记录，返回被移除的技能名列表。"""
        with self._lock:
            owners = self._load()
            removed = [k for k, v in owners.items() if v == employee_id]
            if not removed:
                return []
            for k in removed:
                del owners[k]
            self._write(owners)
            return removed


def write_skill_files(workspace: Path, name: str, files: dict[str, Any]) -> Path:
    """把人才市场随员工下载的技能文件写入 ``workspace/skills/<name>/``。

    校验：
    - 技能名必须是安全 slug；
    - ``files`` 至少包含 ``SKILL.md``；
    - 每个相对路径只能由 [A-Za-z0-9._-] 段组成，禁止 ``..``、空段、绝对路径，
      落盘前解析确认不越出技能目录。

    返回技能目录 Path；非法输入抛 ``SkillOwnershipError``。
    """
    if not is_safe_skill_name(name):
        raise SkillOwnershipError(400, f"invalid skill name: {name!r}")
    if not isinstance(files, dict):
        raise SkillOwnershipError(400, "skill files must be an object")
    if "SKILL.md" not in files:
        raise SkillOwnershipError(400, f"skill {name!r} missing SKILL.md")

    skills_root = _skills_dir(workspace).resolve()
    skill_dir = (_skills_dir(workspace) / name).resolve()
    if not skill_dir.is_relative_to(skills_root):
        raise SkillOwnershipError(400, "skill directory escapes workspace skills")

    normalized: list[tuple[Path, str]] = []
    for rel, content in files.items():
        if not isinstance(rel, str) or not rel.strip():
            raise SkillOwnershipError(400, "invalid skill file path")
        rel_norm = rel.replace("\\", "/")
        parts = rel_norm.split("/")
        if any(p in {"", ".", ".."} for p in parts):
            raise SkillOwnershipError(400, f"invalid skill file path: {rel!r}")
        if not all(_VALID_PATH_SEGMENT.match(p) for p in parts):
            raise SkillOwnershipError(400, f"invalid skill file path: {rel!r}")
        if not isinstance(content, str):
            raise SkillOwnershipError(400, f"skill file content must be text: {rel!r}")
        target = (skill_dir / Path(*parts)).resolve()
        if not target.is_relative_to(skill_dir):
            raise SkillOwnershipError(400, f"skill file escapes skill dir: {rel!r}")
        normalized.append((target, content))

    # 全部校验通过后再落盘，避免半写
    skill_dir.mkdir(parents=True, exist_ok=True)
    for target, content in normalized:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


def delete_skill_directory(workspace: Path, name: str) -> bool:
    """安全删除 ``workspace/skills/<name>/`` 目录；返回是否曾存在。

    仅删除位于工作区 skills 目录下的技能目录（解析后校验，防符号链接逃逸）。
    用于员工删除时的级联清理。
    """
    if not is_safe_skill_name(name):
        return False
    skills_root = _skills_dir(workspace).resolve()
    skill_dir = (_skills_dir(workspace) / name).resolve()
    if not skill_dir.is_relative_to(skills_root):
        return False
    if not skill_dir.is_dir():
        return False
    shutil.rmtree(skill_dir)
    return True
