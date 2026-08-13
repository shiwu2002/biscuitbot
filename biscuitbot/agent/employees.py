"""数字人员工（Digital Employee）存储层。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，负责“数字人员工”目录数据的加载、校验与持久化。
在项目架构中起到的作用：
- ``EmployeeStore`` 以 JSON 文件（``workspace/employees.json``）保存员工的目录数据，
  每个员工由 名称/头像/角色提示词/绑定技能/启用状态 组成；
- 员工是带专属 persona 的拟人化代理——与主智能体不同，与其对话时 LLM 会沉浸在该角色中；
- 员工提示词由 ``ContextBuilder.build_system_prompt`` 在组装系统提示时按会话注入，
  绑定技能则用于限制该员工会话的技能可见范围（详见 ``context.py`` / ``loop.py``）；
- 文件在缺失时自动种子一个默认员工「剪辑高手」（绑定 ``jianying-editor`` 剪映技能）。

写入约定：
- 读按需进行；只有 WebUI 侧（``EmployeeStore`` 的 HTTP 句柄）执行写操作；
- 写入使用“临时文件 + 原子替换 + fsync”策略，避免半写文件。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

# 员工数据文件 schema 版本
EMPLOYEES_SCHEMA_VERSION = 1
# 单次读取的最大文件字节数（防御性上限）
_MAX_EMPLOYEES_FILE_BYTES = 512 * 1024

# id 允许的字符：小写字母、数字、-、_（slug）
_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class EmployeeValidationError(Exception):
    """员工数据校验失败时抛出（含 HTTP 状态码）。"""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _slugify(name: str) -> str:
    """把名称转成 ASCII 小写 slug（中文等非 ASCII 字符会被剥离）。

    中文名称剥离后可能为空字符串，由调用方回退到 ``employee`` 并追加去重后缀。
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or ""


class EmployeeStore:
    """数字人员工目录的 JSON 文件存储。

    职责与项目角色：
    - 读取/写入 ``workspace/employees.json``；
    - 提供增删改查与校验（id slug 且唯一、名称/提示词非空、技能为字符串数组）；
    - 文件缺失时自动种子默认员工「剪辑高手」。

    典型用法：
    - ``ContextBuilder`` 持有实例用于按会话解析员工并注入 persona；
    - WebUI 网关（``GatewayHTTPHandler``）持有实例用于员工目录的 HTTP 增删改查。
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace  # 工作区根目录
        self.path = workspace / "employees.json"  # 员工数据文件
        self._lock = threading.Lock()  # 进程内写锁（当前仅 HTTP 侧写，锁作为防御）

    # ---- 读接口 ------------------------------------------------------------

    def list_employees(self) -> list[dict[str, Any]]:
        """返回全部员工（已归一化）。"""
        return self._load()

    def get_employee(self, employee_id: str) -> dict[str, Any] | None:
        """按 id 返回员工，不存在时返回 None。"""
        for emp in self._load():
            if emp.get("id") == employee_id:
                return emp
        return None

    def _enabled_employee(self, employee_id: str) -> dict[str, Any] | None:
        """按 id 返回**已启用**的员工；未启用/不存在返回 None。"""
        emp = self.get_employee(employee_id)
        if emp is not None and emp.get("enabled", True):
            return emp
        return None

    # ---- 写接口 ------------------------------------------------------------

    def create_employee(self, data: dict[str, Any]) -> dict[str, Any]:
        """新增员工。

        参数:
            data: 员工字段（name 必填；id 可选，缺省按 name 生成 slug；其余有默认值）。

        返回:
            归一化后的完整员工记录。

        异常:
            EmployeeValidationError: 校验失败或 id 重复。
        """
        data = dict(data or {})
        name = self._require_nonempty(data, "name")
        with self._lock:
            employees = self._load()
            existing_ids = {emp.get("id") for emp in employees}

            employee_id = data.get("id")
            if employee_id is None or not str(employee_id).strip():
                # 自动生成：按名称生成 ASCII slug；空或冲突时追加去重后缀
                base = _slugify(str(name)) or "employee"
                employee_id = base
                n = 2
                while employee_id in existing_ids:
                    employee_id = f"{base}-{n}"
                    n += 1
            else:
                employee_id = str(employee_id).strip()
                if employee_id in existing_ids:
                    raise EmployeeValidationError(409, f"员工 id 已存在：{employee_id}")
            self._validate_id(employee_id)

            employee = self._normalize(
                {
                    "id": employee_id,
                    "name": name,
                    "avatar": data.get("avatar"),
                    "system_prompt": data.get("system_prompt"),
                    "skills": data.get("skills"),
                    "enabled": data.get("enabled", True),
                }
            )
            employees.append(employee)
            self._write(employees)
        return employee

    def update_employee(self, employee_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """更新员工（部分字段合并；id 不可变更）。

        异常:
            EmployeeValidationError: 员工不存在或校验失败。
        """
        employee_id = str(employee_id or "").strip()
        data = dict(data or {})
        with self._lock:
            employees = self._load()
            idx = next(
                (i for i, emp in enumerate(employees) if emp.get("id") == employee_id),
                None,
            )
            if idx is None:
                raise EmployeeValidationError(404, f"员工不存在：{employee_id}")

            merged = dict(employees[idx])
            # 仅合并白名单字段，忽略 id/created_at 等不可变字段
            for key in ("name", "avatar", "system_prompt", "skills", "enabled"):
                if key in data:
                    merged[key] = data[key]
            self._require_nonempty(merged, "name")
            self._require_nonempty(merged, "system_prompt")
            merged = self._normalize(merged)
            employees[idx] = merged
            self._write(employees)
        return merged

    def delete_employee(self, employee_id: str) -> dict[str, Any]:
        """删除员工。已删除员工对应的历史会话不受影响（回退全局行为）。

        异常:
            EmployeeValidationError: 员工不存在。
        """
        employee_id = str(employee_id or "").strip()
        with self._lock:
            employees = self._load()
            remaining = [emp for emp in employees if emp.get("id") != employee_id]
            if len(remaining) == len(employees):
                raise EmployeeValidationError(404, f"员工不存在：{employee_id}")
            self._write(remaining)
        return {"deleted": True, "id": employee_id}

    # ---- 内部实现 ----------------------------------------------------------

    def _load(self) -> list[dict[str, Any]]:
        """读取员工列表；文件缺失时种子默认员工并写入。"""
        if not self.path.is_file():
            seeded = self._seed_default()
            try:
                self._write(seeded)
            except OSError:
                logger.warning("无法写入员工文件 {}，使用内存默认值", self.path)
            return seeded
        try:
            if self.path.stat().st_size > _MAX_EMPLOYEES_FILE_BYTES:
                logger.warning("员工文件过大，忽略：{}", self.path)
                return self._seed_default()
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取员工文件失败 {}：{}", self.path, e)
            return self._seed_default()
        employees = raw.get("employees") if isinstance(raw, dict) else None
        if not isinstance(employees, list):
            logger.warning("员工文件格式异常，使用默认值：{}", self.path)
            return self._seed_default()
        return [self._normalize(emp) for emp in employees]

    def _write(self, employees: list[dict[str, Any]]) -> None:
        """原子写入员工文件（临时文件 + fsync + 原子替换）。"""
        payload = {
            "schema_version": EMPLOYEES_SCHEMA_VERSION,
            "employees": employees,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "wb") as f:
            f.write(encoded)
            f.write(b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _seed_default(self) -> list[dict[str, Any]]:
        """内置默认员工「剪辑高手」，绑定剪映技能。"""
        return [
            self._normalize(
                {
                    "id": "clip-master",
                    "name": "剪辑高手",
                    "avatar": "🎬",
                    "system_prompt": (
                        "你是一名「剪辑高手」数字人员工，精通剪映（JianYing / CapCut）专业版自动化剪辑。"
                        "你沉浸在这个角色里，以专业剪辑师的口吻与用户交流：热情、熟练、善于给出可落地的剪辑方案。"
                        "你熟悉录屏、素材导入、字幕配音、转场特效、云端音乐、智能变焦与成片导出的全流程。"
                        "当用户提出剪辑需求时，你会主动确认素材来源与成片规格，并使用 jianying-editor 技能完成自动化剪辑。"
                        "除非任务确实与剪辑无关，否则不要跳出剪辑师的角色。"
                    ),
                    "skills": ["jianying-editor"],
                    "enabled": True,
                }
            )
        ]

    # ---- 校验与归一化 ------------------------------------------------------

    @staticmethod
    def _require_nonempty(data: dict[str, Any], key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise EmployeeValidationError(400, f"员工字段不能为空：{key}")
        return value.strip()

    @classmethod
    def _validate_id(cls, employee_id: str) -> None:
        if not employee_id or not _VALID_ID.match(employee_id):
            raise EmployeeValidationError(400, f"无效的员工 id：{employee_id!r}")

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把原始字段归一化为规范员工记录（填充默认值、强制类型）。"""
        employee_id = raw.get("id")
        if not isinstance(employee_id, str) or not employee_id.strip():
            raise EmployeeValidationError(400, "员工缺少 id")
        employee_id = employee_id.strip()
        self._validate_id(employee_id)

        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise EmployeeValidationError(400, "员工名称不能为空")

        system_prompt = raw.get("system_prompt")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise EmployeeValidationError(400, "员工提示词不能为空")

        avatar = raw.get("avatar")
        if not isinstance(avatar, str):
            avatar = ""

        skills_raw = raw.get("skills")
        if not isinstance(skills_raw, list):
            skills: list[str] = []
        else:
            skills = [
                str(s).strip() for s in skills_raw if isinstance(s, str) and s.strip()
            ]

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = True

        created_at = raw.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        return {
            "id": employee_id,
            "name": name.strip(),
            "avatar": avatar,
            "system_prompt": system_prompt.strip(),
            "skills": skills,
            "enabled": enabled,
            "created_at": created_at,
        }
