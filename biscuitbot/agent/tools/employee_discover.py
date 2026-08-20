"""按技能/分类/姓名检索数字员工的内联工具（调用 invoke_employee 前先检索）。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是数字员工调用链路的检索组件。
``DiscoverEmployeesTool``（discover_employees）让主智能体在调用
``invoke_employee`` 之前，先按技能/分类/姓名检索最合适的数字员工，
检索范围同时覆盖：已安装员工（``EmployeeStore``）与人才市场目录
（``biscuitbot.webui.talent_market`` 拉取的注册表目录）。

人才市场中未安装的匹配项仅作为「未安装」提示返回，绝不自动安装；
安装仍由用户在 WebUI 数字员工管理页面手动完成。
"""

from __future__ import annotations

import asyncio  # 把同步网络拉取放到线程池，避免阻塞事件循环
import json  # 结果序列化
from typing import TYPE_CHECKING, Any  # 类型注解

from loguru import logger  # 日志记录

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema  # JSON Schema 类型

if TYPE_CHECKING:  # 仅类型检查时导入，避免循环依赖
    from biscuitbot.agent.employees import EmployeeStore

# 单条人才市场描述返回的最大长度（防御性截断，控制上下文 token）
_MAX_DESCRIPTION_CHARS = 80
# 未安装提示文案（仅提示，不自动安装）
_NOTE_INSTALL_HINT = (
    "人才市场中的未安装员工需先在 WebUI 数字员工管理页面手动安装后才能调用"
    "（本工具不提供自动安装）。"
)


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema(
            "检索关键词，可匹配员工英文代号、中文姓名、职位或技能"
            "（如 clip-master、阿伟、剪辑、jianying-editor）",
            min_length=1,
            max_length=200,
        ),
        limit=IntegerSchema(
            description="返回结果数量上限（默认 10，最大 20）",
            minimum=1,
            maximum=20,
            nullable=True,
        ),
        required=["query"],
    )
)
class DiscoverEmployeesTool(Tool):
    """检索可调用的数字员工：先按技能/分类/姓名查找，再决定调用谁。"""

    _capability = (
        "Search installed digital employees and the talent-marketplace catalog "
        "by skill, category, or name before invoking them."
    )
    _always_include = True  # 主智能体先检索再点名，完整 schema 每轮发送
    _usage_md = "docs/employee_discover.md"  # 工具使用说明文档路径

    def __init__(self, employees: "EmployeeStore | None" = None):
        self._employees = employees  # 数字人员工目录存储

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(employees=ctx.employees)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """有员工目录时才启用（主会话注入 employees）。"""
        return bool(ctx.employees)

    @property
    def name(self) -> str:
        return "discover_employees"

    @property
    def description(self) -> str:
        return (
            "在调用 invoke_employee 之前，先用本工具按技能/分类/姓名检索最合适的数字员工："
            "同时覆盖已安装员工与人才市场目录。人才市场中的未安装匹配项会以「未安装」提示返回，"
            "需在 WebUI 手动安装后才能调用（本工具不会自动安装）。返回 JSON，"
            "包含员工 id/name/title/skills、是否已安装及来源。"
        )

    @staticmethod
    def _matches(query: str, record: dict[str, Any], fields: list[str]) -> bool:
        """大小写不敏感的模糊匹配：query 是任一字段（或技能元素）的子串即命中。"""
        q = (query or "").strip().lower()
        if not q:
            return False
        for field in fields:
            value = record.get(field)
            if isinstance(value, str) and q in value.lower():
                return True
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and q in item.lower():
                        return True
        return False

    def _installed_hits(self, query: str) -> dict[str, dict[str, Any]]:
        """在已安装且已启用的员工中检索，返回 id -> 记录（已按 id 去重）。"""
        hits: dict[str, dict[str, Any]] = {}
        if self._employees is None:
            return hits
        for emp in self._employees.list_employees():
            if not emp.get("enabled", True):
                continue
            if self._matches(query, emp, ["id", "name", "title", "skills"]):
                hits[emp["id"]] = emp
        return hits

    async def execute(self, query: str, limit: int | None = None, **kwargs: Any) -> str:
        """检索数字员工，返回 JSON：``{"employees": [...], "note": "..."}``。"""
        query = (query or "").strip()
        if not query:
            return json.dumps({"employees": [], "note": "检索关键词不能为空。"}, ensure_ascii=False)
        if self._employees is None:
            return json.dumps({"employees": [], "note": "员工目录不可用。"}, ensure_ascii=False)
        effective_limit = 10 if limit is None else max(1, min(20, int(limit)))

        installed_hits = self._installed_hits(query)

        # 人才市场部分：尽力而为，绝不抛给模型
        market_rows: list[dict[str, Any]] = []
        note_parts: list[str] = []
        try:
            from biscuitbot.webui import talent_market as tm  # 懒加载，避免工具包与 webui 隐式耦合
        except Exception:  # noqa: BLE001
            logger.warning("discover_employees: 人才市场模块导入失败", exc_info=True)
            tm = None
            note_parts.append("人才市场模块不可用，本次仅检索已安装员工。")

        if tm is not None:
            try:
                url = tm.read_talent_market_registry_url()
            except Exception:  # noqa: BLE001
                logger.warning("discover_employees: 读取人才市场注册表配置失败", exc_info=True)
                url = ""
                note_parts.append("读取人才市场注册表配置失败，本次仅检索已安装员工。")
            if not url:
                note_parts.append("未配置人才市场注册表，本次仅检索已安装员工。")
            else:
                try:
                    catalog = await asyncio.to_thread(
                        tm.talent_catalog_payload, url, self._employees
                    )
                    market_rows = [
                        row
                        for row in catalog.get("employees", [])
                        if self._matches(
                            query, row, ["id", "name", "title", "description", "skills", "category"]
                        )
                    ]
                except Exception:  # noqa: BLE001 - 拉取/解析失败只降级，不报错
                    logger.warning("discover_employees: 拉取人才市场目录失败", exc_info=True)
                    note_parts.append("人才市场目录拉取失败，本次仅检索已安装员工。")

        enabled_ids = {
            e.get("id")
            for e in self._employees.list_employees()
            if e.get("enabled", True)
        }

        # 合并结果：已安装优先、按 id 去重
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for eid, rec in installed_hits.items():
            results.append(
                {
                    "id": rec.get("id", eid),
                    "name": rec.get("name", ""),
                    "title": rec.get("title", ""),
                    "skills": rec.get("skills", []),
                    "installed": True,
                    "enabled": True,
                    "source": "installed",
                }
            )
            seen.add(eid)

        uninstalled_count = 0
        for row in market_rows:
            rid = row.get("id")
            if not rid or rid in seen:
                continue
            if row.get("installed"):
                # 已安装但被停用 → 不可调用，跳过
                if rid not in enabled_ids:
                    continue
                results.append(
                    {
                        "id": rid,
                        "name": row.get("name", ""),
                        "title": row.get("title", ""),
                        "skills": row.get("skills", []),
                        "category": row.get("category", ""),
                        "description": (row.get("description") or "")[:_MAX_DESCRIPTION_CHARS],
                        "installed": True,
                        "enabled": True,
                        "source": "marketplace",
                    }
                )
                seen.add(rid)
                continue
            uninstalled_count += 1
            results.append(
                {
                    "id": rid,
                    "name": row.get("name", ""),
                    "title": row.get("title", ""),
                    "skills": row.get("skills", []),
                    "category": row.get("category", ""),
                    "description": (row.get("description") or "")[:_MAX_DESCRIPTION_CHARS],
                    "installed": False,
                    "enabled": False,
                    "source": "marketplace",
                }
            )
            seen.add(rid)

        if uninstalled_count:
            note_parts.append(_NOTE_INSTALL_HINT)
        if not results:
            note_parts.append("未找到匹配的数字员工，可尝试更换关键词（技能/分类/姓名）。")

        return json.dumps(
            {"employees": results[:effective_limit], "note": " ".join(note_parts) or "检索完成。"},
            ensure_ascii=False,
        )
