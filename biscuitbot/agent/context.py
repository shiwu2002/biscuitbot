"""上下文构建器，用于组装 Agent 的提示词（系统提示 + 消息列表）。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责上下文装配的核心组件。
在项目架构中起到的作用：
- ``ContextBuilder`` 负责将身份信息、引导文件（AGENTS.md 等）、记忆、技能、
  运行时元数据等组装为 LLM 调用所需的系统提示与消息列表；
- 提供一系列模块级辅助函数，桥接 CLI 应用工具与 MCP 工具的运行时能力
  （如会话附加参数、运行时注解行、MCP 连接管理）；
- 处理图片附件的 base64 编码、最近历史格式化、运行时上下文注入等细节，
  是连接“会话状态/工具状态”与“LLM 输入”的适配层。
"""

import base64  # 用于将图片附件编码为 base64，以便在多模态消息中内联传递
import mimetypes  # 用于在没有显式 MIME 时推测文件类型
import platform  # 用于获取运行时平台信息，注入身份提示
import re  # 用于提炼员工一句话能力简介
from pathlib import Path  # 文件路径处理
from typing import Any, Mapping, Sequence  # 类型注解支持

from biscuitbot.agent.employees import EmployeeStore  # 数字人员工目录存储，按会话注入 persona
from biscuitbot.agent.memory import MemoryStore  # 记忆存储，提供长期记忆与历史读取
from biscuitbot.agent.skills import SkillsLoader  # 技能加载器，提供技能内容与摘要
from biscuitbot.agent.tools import mcp as mcp_tools  # MCP 工具相关运行时能力桥接
from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表，管理可用工具
from biscuitbot.apps.cli import utils as cli_app_utils  # CLI 应用层工具，提供会话附加参数与运行时注解
from biscuitbot.bus.events import InboundMessage  # 入站消息事件类型
from biscuitbot.session.goal_state import goal_state_runtime_lines  # 目标状态的运行时注解行生成
from biscuitbot.utils.helpers import (  # 通用辅助函数集合
    current_time_str,  # 当前时间字符串生成
    detect_image_mime,  # 图片 MIME 类型探测
    load_bundled_template,  # 加载内置模板
    truncate_text,  # 文本截断
    truncate_text_to_tokens,  # 按 token 数截断文本
)
from biscuitbot.utils.prompt_templates import render_template  # 模板渲染


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回与轮次附加能力相关的持久化参数。

    合并 CLI 应用层与 MCP 工具层各自从会话元数据中提取的附加参数，
    供后续运行时注解与连接恢复使用。
    """
    return cli_app_utils.session_extra(metadata) | mcp_tools.session_extra(metadata)


def runtime_lines(state: Any, msg: Any, workspace: Path, *, skip: bool = False) -> list[str]:
    """返回模型可见的运行时注解行（与轮次附加能力相关）。

    参数:
        state: 运行时状态对象，需含 ``_mcp_servers`` 与 ``_mcp_stacks`` 属性；
        msg: 入站消息；
        workspace: 工作目录；
        skip: 是否跳过注解生成。

    返回:
        注解行列表，将拼接进运行时上下文块。
    """
    return [
        *cli_app_utils.runtime_lines(msg, workspace, skip=skip),
        *mcp_tools.runtime_lines(
            msg,
            configured_server_names=set(state._mcp_servers),
            connected_server_names=set(state._mcp_stacks),
            skip=skip,
        ),
    ]


async def connect_mcp(state: Any, tools: ToolRegistry) -> None:
    """连接尚未建立的 MCP 服务器。"""
    await mcp_tools.connect_missing_servers(state, tools)


async def handle_runtime_control(state: Any, msg: InboundMessage, tools: ToolRegistry) -> bool:
    """处理运行时控制消息（如 MCP 相关指令），返回是否被处理。"""
    return await mcp_tools.handle_runtime_control(state, msg, tools)


async def process_pending_reconnects(state: Any, tools: ToolRegistry) -> None:
    """在所属任务中执行被 _dispatch 子任务延迟的重连请求。

    将 anyio 取消作用域保持在 MCP 所属任务上。由
    ``AgentLoop._run_main_loop`` 的空闲分支调用。
    """
    await mcp_tools.process_pending_reconnects(state, tools)


class ContextBuilder:
    """为 Agent 构建上下文（系统提示 + 消息列表）。

    职责与项目角色：
    - 持有工作目录、时区、护栏等级等配置，并初始化记忆与技能加载器；
    - 提供系统提示构建（``build_system_prompt``）与完整消息列表构建
      （``build_messages``）两条主路径，供 AgentLoop 在每次 LLM 调用前使用；
    - 负责将身份、引导文件、记忆、技能、运行时元数据等片段拼接为最终提示，
      并处理多模态图片、历史截断、运行时上下文注入等细节。

    典型用法：在 Agent 启动时构造一次，随后每次调用复用。
    """

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md"]  # 工作目录下的引导文件，按顺序加载
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"  # 运行时上下文块的起始标记（向模型声明仅为元数据，非指令）
    _MAX_RECENT_HISTORY = 50  # 注入提示的最近历史条目上限
    _MAX_HISTORY_TOKENS = 8_000  # 最近历史区段的 token 硬上限
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"  # 运行时上下文块的结束标记

    def __init__(
        self,
        workspace: Path,
        timezone: str | None = None,
        disabled_skills: list[str] | None = None,
        guard_level: str = "standard",
    ):
        self.workspace = workspace  # 工作目录，用于加载引导文件与记忆
        self.timezone = timezone  # 时区，用于运行时上下文中的当前时间
        self.guard_level = guard_level  # 护栏等级，注入身份提示以约束行为
        self.memory = MemoryStore(workspace)  # 记忆存储实例
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)  # 技能加载器实例
        self.employees = EmployeeStore(workspace)  # 数字人员工目录存储，用于按会话解析 persona

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        tool_index: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """从身份、引导文件、记忆与技能构建系统提示。

        参数:
            skill_names: 指定激活的技能名列表（可选）；
            channel: 渠道标识，注入身份提示；
            session_summary: 会话压缩摘要，作为归档上下文追加；
            workspace: 覆盖默认工作目录；
            include_memory_recent_history: 是否包含最近历史区段；
            session_key: 会话 key，用于按会话过滤历史；
            unified_session: 是否使用统一会话视角读取历史；
            tool_index: 工具索引文本，追加到提示中；
            session_metadata: 会话元数据，若绑定数字人员工则注入其 persona（技能不手动分配，员工自主选用）。

        返回:
            拼接完成的系统提示字符串，各片段以分隔线连接。
        """
        root = workspace or self.workspace
        parts = [self._get_identity(channel=channel, workspace=root)]  # 身份段始终在最前

        # 数字员工团队板块：让主智能体知道团队构成，可调用 invoke_employee
        roster = self._employee_roster_section()
        if roster:
            parts.append(roster)

        # 数字人员工 persona：紧跟身份段、置于引导文件之前，使其足够醒目
        employee = self._resolve_employee(session_metadata)
        if employee is not None:
            parts.append(self._persona_section(employee))

        bootstrap = self._load_bootstrap_files(root)
        if bootstrap:
            parts.append(bootstrap)

        parts.append(render_template("agent/tool_contract.md"))  # 工具契约模板

        if tool_index:
            parts.append(tool_index)

        memory = self.memory.get_memory_context()
        # 仅当用户自定义过 MEMORY.md（与内置模板不同）时才注入记忆段
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        # 技能不手动分配：数字员工与主智能体一样可发现全部技能，由员工自主选用
        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        if include_memory_recent_history:
            entries = self.memory.read_recent_history_for_prompt(
                since_cursor=self.memory.get_last_dream_cursor(),
                session_key=session_key,
                unified_session=unified_session,
            )
            if entries:
                history_text = self._format_recent_history(entries)
                history_text = truncate_text_to_tokens(history_text, self._MAX_HISTORY_TOKENS)  # 按 token 上限截断
                parts.append("# Recent History\n\n" + history_text)

        if session_summary:
            parts.append(f"[Archived Context Summary]\n\n{session_summary}")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self, channel: str | None = None, workspace: Path | None = None) -> str:
        """获取核心身份段。

        参数:
            channel: 渠道标识；
            workspace: 覆盖默认工作目录。

        返回:
            渲染后的身份提示字符串，包含工作目录、运行时环境与平台策略。
        """
        root = workspace or self.workspace
        workspace_path = str(root.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
            guard_level=self.guard_level,
        )

    # ---- 数字人员工（persona）辅助 ------------------------------------------

    def _resolve_employee(self, session_metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """按会话元数据解析**已启用**的数字人员工；未绑定或未启用时返回 None。

        session_metadata 由 ``session.metadata`` 携带（``new_chat`` 时持久化的 employee id）。
        """
        if not session_metadata:
            return None
        employee_id = session_metadata.get("employee")
        if not isinstance(employee_id, str) or not employee_id.strip():
            return None
        return self.employees._enabled_employee(employee_id.strip())

    @staticmethod
    def _persona_section(employee: dict[str, Any]) -> str:
        """渲染员工 persona 段：标题（代号 + 职位）+ 头像 + 角色提示词。"""
        name = employee.get("name", "")
        title = employee.get("title", "")
        avatar = employee.get("avatar", "")
        system_prompt = employee.get("system_prompt", "").strip()
        heading = f"# Persona — {name}" + (f"（{title}）" if title else "") + (f" {avatar}" if avatar else "")
        return f"{heading}\n\n{system_prompt}"

    def _employee_roster_section(self) -> str:
        """渲染「数字员工团队」板块：列出启用员工，供主智能体调用 invoke_employee。"""
        enabled = [e for e in self.employees.list_employees() if e.get("enabled", True)]
        if not enabled:
            return ""
        lines = [
            "## 数字员工团队（可调用）\n",
            "你的团队有以下数字员工，需要时可使用 invoke_employee 工具点名一位协助完成任务：",
        ]
        for emp in enabled:
            name = emp.get("name", "")
            title = emp.get("title", "")
            avatar = emp.get("avatar", "")
            tag = f"{name}" + (f"（{title}）" if title else "")
            line = f"- {avatar} {tag} — {self._employee_summary(emp)}" if avatar else f"- {tag} — {self._employee_summary(emp)}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _employee_summary(employee: dict[str, Any]) -> str:
        """从 persona 首句提炼一句话能力简介（去掉「你是…」自称前缀，≤48 字）。"""
        persona = (employee.get("system_prompt") or "").strip()
        if not persona:
            return ""
        first = persona
        for sep in ("。", "！", "？"):
            if sep in first:
                first = first.split(sep, 1)[0]
        first = re.sub(r"^你是[「『]?[^，,]+[」』]?[，,]?\s*", "", first)
        return first[:48]

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        timezone: str | None = None,
        sender_id: str | None = None,
        supplemental_lines: Sequence[str] | None = None,
    ) -> str:
        """构建追加在用户内容之后的“不可信运行时元数据”块。

        参数:
            channel: 渠道标识；
            chat_id: 会话/聊天 ID；
            timezone: 时区，用于生成当前时间；
            sender_id: 发送者 ID；
            supplemental_lines: 额外注解行（如目标状态、MCP 状态）。

        返回:
            被运行时上下文标记包裹的元数据块字符串。
        """
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if sender_id:
            lines += [f"Sender ID: {sender_id}"]
        if supplemental_lines:
            lines.extend(supplemental_lines)
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        """合并两条同角色消息的内容。

        参数:
            left: 原消息内容（字符串或分块列表）；
            right: 待合并内容（字符串或分块列表）。

        返回:
            合并后的内容。若两侧均为字符串则拼接为字符串；否则统一转为分块列表后拼接。
        """
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self, workspace: Path | None = None) -> str:
        """从工作目录加载所有引导文件。

        参数:
            workspace: 覆盖默认工作目录。

        返回:
            拼接后的引导文件内容；无文件时返回空字符串。
        """
        parts = []
        root = workspace or self.workspace

        for filename in self.BOOTSTRAP_FILES:
            file_path = root / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """判断 *content* 是否与内置模板完全一致（即用户未做自定义）。

        参数:
            content: 待检查的内容；
            template_path: 内置模板路径。

        返回:
            若内容与模板一致返回 True，否则 False。
        """
        tpl = load_bundled_template(template_path)
        if tpl is not None:
            return content.strip() == tpl.strip()
        return False

    def _format_recent_history(self, entries: list[dict[str, Any]]) -> str:
        """格式化最近历史条目，当未处理历史超过 ``_MAX_RECENT_HISTORY`` 时保留 backlog 摘要。

        若不这样做，被上限静默丢弃的条目在 Dream 处理前对 Agent 不可见，
        用户会感觉 Agent“遗忘”了最近上下文。因此超过上限的较早条目会被
        压缩为简短的 ``[Backlog]`` 前缀，至少让 Agent 知道它们的存在。
        """
        if len(entries) <= self._MAX_RECENT_HISTORY:
            return "\n".join(
                f"- [{e['timestamp']}] {e['content']}" for e in entries
            )

        backlog = entries[:-self._MAX_RECENT_HISTORY]
        recent = entries[-self._MAX_RECENT_HISTORY:]
        backlog_count = len(backlog)
        # 压缩 backlog：保留最接近最近窗口的 20 条（时间上最相关），每条截断到 100 字符。
        snippets = []
        for e in backlog[-20:]:
            snippet = truncate_text(e.get("content", ""), 100)
            snippets.append(f"  - [{e['timestamp']}] {snippet}")
        backlog_block = (
            f"- [Backlog: {backlog_count} earlier entries not shown in full]\n"
            + "\n".join(snippets)
        )
        recent_block = "\n".join(
            f"- [{e['timestamp']}] {e['content']}" for e in recent
        )
        return f"{backlog_block}\n{recent_block}"

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        sender_id: str | None = None,
        session_summary: str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
        current_runtime_lines: Sequence[str] | None = None,
        workspace: Path | None = None,
        runtime_state: Any | None = None,
        inbound_message: Any | None = None,
        skip_runtime_lines: bool = False,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
        tool_index: str | None = None,
    ) -> list[dict[str, Any]]:
        """为一次 LLM 调用构建完整的消息列表。

        参数:
            history: 历史消息列表；
            current_message: 本轮用户文本；
            skill_names: 激活技能列表；
            media: 图片附件路径列表；
            channel: 渠道标识；
            chat_id: 聊天 ID；
            current_role: 本轮消息角色（默认 user）；
            sender_id: 发送者 ID；
            session_summary: 会话压缩摘要；
            session_metadata: 会话元数据，用于生成目标状态注解；
            current_runtime_lines: 额外运行时注解行；
            workspace: 覆盖默认工作目录；
            runtime_state: 运行时状态对象，用于生成 MCP 注解；
            inbound_message: 入站消息对象；
            skip_runtime_lines: 是否跳过运行时注解生成；
            include_memory_recent_history: 是否包含最近历史；
            session_key: 会话 key；
            unified_session: 是否使用统一会话视角；
            tool_index: 工具索引文本。

        返回:
            供 LLM 调用的消息列表（含 system 与历史/当前消息）。
        """
        root = workspace or self.workspace
        extra = [
            *goal_state_runtime_lines(session_metadata),
        ]
        if runtime_state is not None and inbound_message is not None:
            extra.extend(runtime_lines(runtime_state, inbound_message, root, skip=skip_runtime_lines))
        if current_runtime_lines:
            extra.extend(line for line in current_runtime_lines if line)
        runtime_ctx = self._build_runtime_context(
            channel,
            chat_id,
            self.timezone,
            sender_id=sender_id,
            supplemental_lines=extra or None,
        )
        user_content = self._build_user_content(current_message, media)

        # 将运行时上下文与用户内容合并为单条用户消息，
        # 避免出现某些提供商拒绝的连续同角色消息。
        # 运行时上下文追加在后，以保持用户内容前缀稳定，
        # 利于命中提示缓存（上下文每轮因时间变化而不同）。
        if isinstance(user_content, str):
            merged = f"{user_content}\n\n{runtime_ctx}"
        else:
            merged = user_content + [{"type": "text", "text": runtime_ctx}]
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    channel=channel,
                    session_summary=session_summary,
                    workspace=root,
                    include_memory_recent_history=include_memory_recent_history,
                    session_key=session_key,
                    unified_session=unified_session,
                    tool_index=tool_index,
                    session_metadata=session_metadata,
                ),
            },
            *history,
        ]
        # 若历史最后一条已是当前角色，则合并内容，避免连续同角色消息
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """构建用户消息内容，可选附加 base64 编码的图片。

        参数:
            text: 用户文本；
            media: 图片路径列表（可选）。

        返回:
            无图片时返回文本字符串；有图片时返回图片块 + 文本块的内容列表。
        """
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
