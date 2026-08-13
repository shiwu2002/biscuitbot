"""调用数字员工的内联工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中的数字员工调用组件。
``InvokeEmployeeTool``（invoke_employee）允许主智能体在对话中点名某位已启用的
数字员工（数字人员工）执行一项任务，员工以自身人设（persona）运行一个完整的
工具循环，并把成果**内联**返回给主智能体，由主智能体整合进回复。

员工名单与分工见系统提示中的「数字员工团队」板块；员工列表来自
``ToolContext.employees``（``EmployeeStore``），执行依赖
``ToolContext.subagent_manager.run_employee_inline``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any  # 类型注解

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import StringSchema, tool_parameters_schema  # JSON Schema 类型
from biscuitbot.security.workspace_access import current_workspace_scope  # 当前工作区作用域

if TYPE_CHECKING:  # 仅类型检查时导入，避免循环依赖
    from biscuitbot.agent.employees import EmployeeStore
    from biscuitbot.agent.subagent import SubagentManager

# 内联返回成果的最大字符数（防御性截断，避免撑爆主会话上下文）
_MAX_RESULT_CHARS = 12000


@tool_parameters(
    tool_parameters_schema(
        employee_id=StringSchema("要调用的数字员工 id（见系统提示「数字员工团队」板块）"),
        task=StringSchema("交给该数字员工的任务描述，越具体越好"),
        required=["employee_id", "task"],
    )
)
class InvokeEmployeeTool(Tool):
    """调用某位数字员工，让其以自身人设执行任务并内联返回成果。"""

    _capability = "Invoke a digital employee to complete a task and return the result inline."
    _always_include = True  # 主智能体应随时可点名数字员工，无需额外发现步骤

    def __init__(
        self,
        subagent_manager: "SubagentManager | None" = None,
        employees: "EmployeeStore | None" = None,
    ):
        self._manager = subagent_manager  # 子智能体管理器（提供内联员工执行）
        self._employees = employees  # 数字员工目录存储

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            subagent_manager=ctx.subagent_manager,
            employees=ctx.employees,
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """同时具备员工目录与子智能体执行器时才启用。"""
        return bool(ctx.subagent_manager and ctx.employees)

    @property
    def name(self) -> str:
        return "invoke_employee"

    @property
    def description(self) -> str:
        return (
            "调用某位数字员工（数字人员工），让 TA 以自身人设执行一项任务并返回成果。"
            "适合把用户交给你的一项工作分派给更专业的团队成员（如生成视频、剪辑、定位咨询、"
            "写脚本、做设计等）。员工名单与分工见系统提示中的『数字员工团队』板块；"
            "调用后等待员工完成，把返回的成果整理后转述给用户。"
        )

    def _employee_label(self, employee: dict[str, Any]) -> str:
        """生成员工展示标签：代号（职位）。"""
        name = employee.get("name", "")
        title = employee.get("title", "")
        return f"{name}（{title}）" if title else name

    def _available_roster(self) -> str:
        """列出已启用员工，供 id 填错时引导主智能体。"""
        if self._employees is None:
            return ""
        enabled = [
            e for e in self._employees.list_employees() if e.get("enabled", True)
        ]
        return "\n".join(
            f"- {e.get('id')}（{self._employee_label(e)}）" for e in enabled
        )

    async def execute(
        self,
        employee_id: str,
        task: str,
        **kwargs: Any,
    ) -> str:
        """执行数字员工调用，内联返回成果。

        参数:
            employee_id: 员工 id（见系统提示「数字员工团队」板块）；
            task: 交给员工的任务描述。

        返回:
            员工执行成果文本；员工不存在/禁用/任务为空时返回引导性错误。
        """
        employee_id = (employee_id or "").strip()
        task = (task or "").strip()
        if not employee_id:
            return "调用失败：缺少 employee_id。可用员工：\n" + (self._available_roster() or "（无）")
        if not task:
            return f"调用失败：缺少任务描述 task。请为数字员工「{employee_id}」明确任务。"
        if self._manager is None or self._employees is None:
            return "调用失败：数字员工调用当前不可用（缺少员工目录或执行器）。"

        employee = self._employees.get_employee(employee_id)
        if employee is None or not employee.get("enabled", True):
            return (
                f"调用失败：没有可用的数字员工 id 为「{employee_id}」。"
                "可用员工：\n" + (self._available_roster() or "（无）")
            )

        scope = current_workspace_scope()
        workspace = scope.project_path if scope is not None else None
        # 技能归属自己：员工内联执行时只用该员工自己的技能，而非共享全部
        employee_skills = set(employee.get("skills") or [])
        content = await self._manager.run_employee_inline(
            employee,
            task,
            include_skills=employee_skills,
            workspace=workspace,
        )
        if len(content) > _MAX_RESULT_CHARS:
            content = content[:_MAX_RESULT_CHARS] + "\n…（成果已截断）"
        return f"数字员工「{self._employee_label(employee)}」的成果：\n\n{content}"
