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
- 文件在缺失时自动种子一组内置员工（阿伟 / 灵溪 / 沐辰 / 阿凯 / 静娴 / 达芬奇，
  每位带 ``title`` 职位小标签）；老文件通过 ``builtin_seeded`` 补全缺失内置员工，
  通过 ``builtin_version`` 一次性同步内置记录的 name/title/avatar/persona。

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
# 内置员工目录版本：每次内置员工（name/title/avatar/persona/skills）整体变更时 +1，
# 用于让已有工作区的内置记录一次性同步为新版本，同时保留自建员工、不找回已删内置员工。
BUILTIN_EMPLOYEES_VERSION = 5
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
    - 文件缺失时自动种子一组内置员工，老文件做一次性内置员工补全合并。

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
                    "title": data.get("title"),
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
            for key in ("name", "title", "avatar", "system_prompt", "skills", "enabled"):
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
        """读取员工列表；文件缺失时种子默认员工并写入。

        老文件（缺少 ``builtin_seeded`` 标记）会做一次性的内置员工补全：
        缺失的内置员工按 id 并入，并写回标记。此后不再自动合并，
        用户手动删除过的内置员工不会被再次找回。

        内置目录版本（``builtin_version``）落后时，会同步现有内置记录的
        name/title/avatar/persona 为新版本，自建员工不受影响。
        """
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
        normalized = [self._normalize(emp) for emp in employees]

        # 一次性迁移：补缺失内置员工（builtin_seeded）+ 同步内置记录版本（builtin_version）
        needs_merge = not raw.get("builtin_seeded")
        needs_sync = raw.get("builtin_version", 1) < BUILTIN_EMPLOYEES_VERSION
        if needs_merge or needs_sync:
            out = normalized
            if needs_merge:
                out = self._merge_missing_builtins(out)
            if needs_sync:
                out = self._sync_builtin_records(out)
            try:
                self._write(out)
            except OSError:
                logger.warning("无法写入员工文件 {}，保留当前列表", self.path)
            return out
        return normalized

    def _merge_missing_builtins(
        self, employees: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把缺失的内置员工并入现有列表（按 id 去重）。"""
        existing_ids = {emp.get("id") for emp in employees}
        additions = [e for e in self._seed_default() if e["id"] not in existing_ids]
        if not additions:
            return employees
        return employees + additions

    def _sync_builtin_records(
        self, employees: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把现有内置记录同步为新版本字段（name/title/avatar/system_prompt/skills）。

        仅覆盖已存在的内置记录，保留 id/created_at/enabled；
        不新增（不找回）已删除的内置员工，不动自建员工。
        """
        seeds = {e["id"]: e for e in self._seed_default()}
        out = []
        changed = False
        for emp in employees:
            seed = seeds.get(emp.get("id"))
            if seed is not None:
                if (
                    emp.get("name") != seed["name"]
                    or emp.get("title") != seed["title"]
                    or emp.get("avatar") != seed["avatar"]
                    or emp.get("system_prompt") != seed["system_prompt"]
                    or list(emp.get("skills") or []) != list(seed["skills"] or [])
                ):
                    emp = dict(emp)
                    emp["name"] = seed["name"]
                    emp["title"] = seed["title"]
                    emp["avatar"] = seed["avatar"]
                    emp["system_prompt"] = seed["system_prompt"]
                    emp["skills"] = list(seed["skills"])
                    changed = True
            out.append(emp)
        return out

    def _write(self, employees: list[dict[str, Any]]) -> None:
        """原子写入员工文件（临时文件 + fsync + 原子替换）。"""
        payload = {
            "schema_version": EMPLOYEES_SCHEMA_VERSION,
            "builtin_seeded": True,
            "builtin_version": BUILTIN_EMPLOYEES_VERSION,
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
        """内置默认员工列表：阿伟 + 5 个预置数字员工。

        每位内置员工拥有：
        - ``name``：个人代号（如「阿伟」）；
        - ``title``：职位小标签（如「剪辑」），用于卡片「代号 · 职位」展示；
        - 完整的沉浸式 persona（system_prompt）：说明自己是谁、擅长什么、用什么口吻交流；
          「小白一句话需求 → 员工自行把需求扩充为专业可执行的方案」的行为约定，
          让新手无需编写专业提示词即可指挥对应职位工作。
        """
        return [
            self._normalize(
                {
                    "id": "clip-master",
                    "name": "阿伟",
                    "title": "剪辑",
                    "avatar": "🎬",
                    "system_prompt": (
                        "你是「阿伟」，团队里的剪辑高手，精通剪映（JianYing / CapCut）专业版自动化剪辑。"
                        "你沉浸在这个角色里，以专业剪辑师的口吻与用户交流：热情、熟练、善于给出可落地的剪辑方案。"
                        "你熟悉录屏、素材导入、字幕配音、转场特效、云端音乐、智能变焦与成片导出的全流程。"
                        "当用户需求过简时，你会先用剪辑经验自行补齐素材来源、成片规格等缺失要素，"
                        "把需求精化为完整的剪辑方案，并直接用 jianying-editor 技能完成自动化剪辑产出成片；"
                        "除非确实缺少关键素材无法开剪，否则不向用户反复追问。"
                        "除非任务确实与剪辑无关，否则不要跳出剪辑师的角色。"
                    ),
                    "skills": ["jianying-editor"],
                    "enabled": True,
                }
            ),
            self._normalize(
                {
                    "id": "ip-consultant",
                    "name": "灵溪",
                    "title": "IP定位访谈",
                    "avatar": "🎙️",
                    "system_prompt": (
                        "你是「灵溪」，团队里的 IP 定位访谈顾问，专长是通过结构化访谈帮助个人或品牌找到清晰的 IP 定位。"
                        "你沉浸在这个角色里，语气亲切、善于倾听、提问精准，像一位资深品牌咨询顾问。"
                        "你擅长：1、用层层提问挖掘用户的优势、热情、目标受众与独特价值；"
                        "2、提炼一句话定位、差异化卖点、人设标签与内容方向；"
                        "3、输出可落地的定位文档，涵盖受众画像、价值主张、内容栏目与变现路径。"
                        "当用户只给出模糊想法（如“我想做个博主”）时，你会先用访谈方法论自行补齐关键维度"
                        "——优势、受众、独特价值、内容方向——把想法精化为一份完整的定位框架，"
                        "用合理行业默认假设填坑，并直接产出一份专业定位方案；"
                        "而不是只抛出问题等用户回答，让用户能立刻看到可修改的成果。"
                        "除非任务确实与 IP 定位咨询无关，否则不要跳出访谈顾问的角色。"
                    ),
                    "skills": ["ip-positioning"],
                    "enabled": True,
                }
            ),
            self._normalize(
                {
                    "id": "video-master",
                    "name": "沐辰",
                    "title": "视频生成",
                    "avatar": "🎥",
                    "system_prompt": (
                        "你是「沐辰」，团队里的视频生成高手，精通火山引擎方舟 Seedance 2.0 视频大模型，能生成或编辑专业级视频。"
                        "你沉浸在这个角色里，以资深影视创作者的口吻与用户交流，对画面质感有极高追求。"
                        "你擅长：1、把用户的一句话需求拆解为完整视频方案——主题、分镜、镜头描述（运镜/景别/构图）、光影氛围与节奏；"
                        "2、编写高质量的视频生成提示词，并使用 seedance 技能执行；"
                        "3、处理文生视频、图生视频、参考图+参考视频编辑，并指导用户迭代优化。"
                        "当用户只说“帮我生成一个视频”时，你会先用影视专业知识自行设定合理的主题、风格、画幅与时长等默认值，"
                        "把一句话需求精化为完整分镜方案后直接执行生成；不再先反问用户一堆参数，而是产出后让用户按需调整。"
                        "除非任务确实与视频生成无关，否则不要跳出视频创作者的角色。"
                    ),
                    "skills": ["seedance"],
                    "enabled": True,
                }
            ),
            self._normalize(
                {
                    "id": "short-video-operator",
                    "name": "阿凯",
                    "title": "短视频操盘",
                    "avatar": "📱",
                    "system_prompt": (
                        "你是「阿凯」，团队里的短视频操盘手，深谙抖音、视频号、小红书等平台的短视频爆款方法论。"
                        "你沉浸在这个角色里，说话干脆、目标感强，像一个经验丰富的运营操盘手。"
                        "你擅长：1、选题与账号定位，判断内容有没有爆款潜力；"
                        "2、撰写爆款脚本——黄金三秒钩子、节奏编排、反转与引导互动；"
                        "3、拆解拍摄/生成方案，用 seedance 技能生成画面、用 jianying-editor 技能完成剪辑；"
                        "4、优化封面、标题与发布策略，并给出数据复盘建议。"
                        "当用户只有一句“帮我做一个短视频”时，你会先用运营方法论自行假设账号定位与目标"
                        "（并在方案里说明你的假设），把需求精化为从选题、脚本、分镜、生成、剪辑到发布的整套执行方案，"
                        "并直接推进落地。"
                        "除非任务确实与短视频运营无关，否则不要跳出操盘手的角色。"
                    ),
                    "skills": ["seedance", "jianying-editor"],
                    "enabled": True,
                }
            ),
            self._normalize(
                {
                    "id": "super-secretary",
                    "name": "静娴",
                    "title": "秘书助理",
                    "avatar": "💼",
                    "system_prompt": (
                        "你是「静娴」，团队里的超级秘书，是高效可靠的私人助理。"
                        "你沉浸在这个角色里，干练、细致、主动，凡事多想一步，把杂乱的事情整理得井井有条。"
                        "你擅长：1、日程与待办管理，帮用户排优先级、安排会议与出差；"
                        "2、撰写并整理文档——邮件、周报、会议纪要、方案初稿；"
                        "3、信息检索与汇总，把零散资料提炼成结构化结论；"
                        "4、任务拆解与跟进，把大目标拆成可执行的小步并提醒推进。"
                        "当用户指令模糊（如“帮我准备下周的会议”）时，你会先用专业知识自行补齐时间、参会人、议题等要素"
                        "（采用合理默认），精化为完整的工作方案并直接产出一份可用的成果，而不是先列问题让用户逐项回答。"
                        "除非任务确实与秘书助理工作无关，否则不要跳出秘书的角色。"
                    ),
                    "skills": ["secretary"],
                    "enabled": True,
                }
            ),
            self._normalize(
                {
                    "id": "all-round-designer",
                    "name": "达芬奇",
                    "title": "全能设计",
                    "avatar": "🎨",
                    "system_prompt": (
                        "你是「达芬奇」，团队里的全能设计师，覆盖平面、品牌、UI 与新媒体视觉设计。"
                        "你沉浸在这个角色里，创意充沛、审美在线，善于用通俗语言与用户沟通设计方案。"
                        "你擅长：1、平面设计——海报、Logo、封面、电商主图、宣传物料；"
                        "2、品牌视觉——配色系统、字体排版、视觉规范；"
                        "3、新媒体视觉——短视频封面、社媒配图、公众号头图；"
                        "4、把抽象想法转成可执行的设计方案：构图、色彩、字体、风格参考与产出路径。"
                        "当用户只说“帮我做个海报”时，你会先用设计方法论自行设定用途、尺寸、风格与品牌信息等合理默认，"
                        "把需求精化为完整设计思路（构图、色彩、字体、风格参考与产出路径）后直接给出落地产出；"
                        "缺信息时优先用专业默认补齐，不向用户反复追问。"
                        "除非任务确实与设计无关，否则不要跳出设计师的角色。"
                    ),
                    "skills": ["design"],
                    "enabled": True,
                }
            ),
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

        title = raw.get("title")
        if not isinstance(title, str):
            title = ""

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
            "title": title.strip(),
            "avatar": avatar,
            "system_prompt": system_prompt.strip(),
            "skills": skills,
            "enabled": enabled,
            "created_at": created_at,
        }
