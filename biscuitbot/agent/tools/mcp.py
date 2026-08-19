"""MCP 客户端：连接 MCP 服务器并将其工具包装为原生 biscuitbot 工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统与 Model Context
Protocol (MCP) 生态的集成层。它负责：
- 连接配置的 MCP 服务器（支持 stdio / SSE / streamableHttp 三种传输）。
- 将 MCP 服务器暴露的 tools / resources / prompts 包装为 biscuitbot 原生
  ``Tool`` 子类，注册到工具注册表。
- 处理会话终止、超时、瞬时错误的重连与重试。
- 支持运行时热重载（WebUI 修改 MCP 配置后无需重启）。
- 处理 anyio cancel scope 的 task 亲和性约束（栈必须在 owner task 中
  打开与关闭）。
"""

import asyncio  # 异步 IO 与任务管理
import os  # 操作系统接口，用于判断平台与读取环境变量
import re  # 正则表达式，用于工具名净化
import shutil  # 可执行文件查找，用于 Windows 命令解析
import urllib.parse  # URL 解析，用于 HTTP 探测
from collections.abc import Awaitable, Callable  # 可等待对象与可调用对象类型
from contextlib import AsyncExitStack, suppress  # 异步退出栈与异常抑制
from typing import Any, Mapping  # 任意类型与映射类型
from weakref import WeakKeyDictionary  # 弱引用字典，用于按 state 隔离重载锁

import httpx  # HTTP 客户端，用于 SSE/streamableHttp 传输
from loguru import logger  # 日志库

from biscuitbot.agent.tools.base import Tool  # 工具基类
from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表
from biscuitbot.bus.events import (  # 消息总线事件
    INBOUND_META_RUNTIME_CONTROL,  # 运行时控制元数据键
    RUNTIME_CONTROL_ACK,  # 确认 Future 的元数据键
    RUNTIME_CONTROL_MCP_RELOAD,  # MCP 热重载控制信号
    InboundMessage,  # 入站消息
)
from biscuitbot.security.network import validate_url_target  # URL 安全校验

# 值得单次重试的瞬时连接错误。
# 通常发生在 MCP 服务器重启或调用间网络中断时。
_TRANSIENT_EXC_NAMES: frozenset[str] = frozenset((
    "ClosedResourceError",
    "BrokenResourceError",
    "EndOfStream",
    "BrokenPipeError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "ConnectionAbortedError",
    "ConnectionError",
))

# Windows 上需要通过 cmd 包装才能可靠启动的 shell 启动器
_WINDOWS_SHELL_LAUNCHERS: frozenset[str] = frozenset(("npx", "npm", "pnpm", "yarn", "bunx"))

# 模型提供商（Anthropic、OpenAI 等）允许的工具名字符集。
# 将 [a-zA-Z0-9_-] 以外的字符替换为下划线，并合并连续下划线。
_SANITIZE_RE = re.compile(r"_+")
# 按 state 实例隔离的重载锁字典（弱引用，state 回收后自动清理）
_RELOAD_LOCKS: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()
# 重连回调类型：(server_name, tool_name, stale_tool) -> 新工具或 None
_ReconnectCallback = Callable[[str, str, Tool], Awaitable[Tool | None]]


def _sanitize_name(name: str) -> str:
    """净化 MCP 来源的名称，使其兼容模型 API。"""
    return _SANITIZE_RE.sub("_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _is_transient(exc: BaseException) -> bool:
    """判断异常是否看起来像瞬时连接错误。"""
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


def _is_session_terminated(exc: BaseException) -> bool:
    """当 MCP SDK 报告客户端会话已死亡时返回 True。"""
    messages = [str(exc)]
    error = getattr(exc, "error", None)
    if error is not None:
        messages.append(str(getattr(error, "message", "")))
    return any(
        marker in message.lower()
        for marker in ("session terminated", "connection closed")
        for message in messages
    )


async def _drain_incoming_messages(session: Any, server_name: str) -> None:
    """持续排空 MCP 会话的 ``incoming_messages`` 通道。

    MCP SDK 将服务器端通知与 stdout 解析异常路由到一个容量为 0 的 anyio
    通道。若无人读取，SDK 的 ``_receive_loop`` 会在下一次 ``send()`` 时永久
    阻塞，从而卡死整个会话：后续每个 ``call_tool`` 都在等待永远不到的响应。
    排空通道可保持 ``_receive_loop`` 存活，使工具调用响应仍能送达。响应本身
    走独立的 id 键流，因此此处排空不会窃取结果。
    """
    stream = getattr(session, "incoming_messages", None)
    if stream is None:
        return
    while True:
        try:
            await stream.receive()
        except Exception:
            # EndOfStream / ClosedResourceError / 任何关闭错误 → 停止。
            break


def _start_incoming_drainer(stack: AsyncExitStack, session: Any, server_name: str) -> None:
    """启动后台任务排空 ``session.incoming_messages``，并将其生命周期绑定到
    ``stack``，在服务器断开时取消。"""
    task = asyncio.create_task(
        _drain_incoming_messages(session, server_name),
        name=f"mcp-drain-{server_name}",
    )

    async def _stop_drain(exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        return False

    stack.push_async_exit(_stop_drain)


async def _probe_http_url(url: str, timeout: float = 3.0) -> bool:
    """快速 TCP 探测，检查 HTTP MCP 服务器是否可达。

    避免在端口关闭时进入 ``streamable_http_client`` / ``sse_client`` ——
    这些传输使用 anyio 任务组，其清理可能抛出 ``RuntimeError`` /
    ``ExceptionGroup``，逃出调用方的 try/except 并使事件循环崩溃。
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port
    # 无端口时按 scheme 推断默认端口
    if not port:
        port = 443 if parsed.scheme == "https" else 80
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        with suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _validate_mcp_request_url(request: httpx.Request) -> None:
    """校验每个发出的 MCP HTTP 请求，包括重定向目标。"""
    ok, error = validate_url_target(str(request.url))
    if not ok:
        raise httpx.RequestError(
            f"Blocked unsafe MCP URL {request.url} ({error})",
            request=request,
        )


def _windows_command_basename(command: str) -> str:
    """返回 Windows 命令或路径的小写 basename。"""
    return command.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _normalize_windows_stdio_command(
    command: str,
    args: list[str] | None,
    env: dict[str, str] | None,
) -> tuple[str, list[str], dict[str, str] | None]:
    """包装 Windows shell 启动器，使 MCP stdio 服务器可靠启动。"""
    normalized_args = list(args or [])
    # 非 Windows 平台无需包装
    if os.name != "nt":
        return command, normalized_args, env

    basename = _windows_command_basename(command)
    # 已是 shell 或显式可执行文件，无需包装
    if basename in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return command, normalized_args, env

    if basename.endswith((".exe", ".com")):
        return command, normalized_args, env

    resolved = shutil.which(command, path=(env or {}).get("PATH")) or command
    resolved_basename = _windows_command_basename(resolved)
    should_wrap = (
        basename in _WINDOWS_SHELL_LAUNCHERS
        or basename.endswith((".cmd", ".bat"))
        or resolved_basename.endswith((".cmd", ".bat"))
    )
    if not should_wrap:
        return command, normalized_args, env

    # 通过 cmd /d /c 包装，确保 .cmd/.bat 启动器正确执行
    comspec = (env or {}).get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    return comspec, ["/d", "/c", command, *normalized_args], env


def _extract_nullable_branch(options: Any) -> tuple[dict[str, Any], bool] | None:
    """为可空联合类型返回单一的非 null 分支。"""
    if not isinstance(options, list):
        return None

    non_null: list[dict[str, Any]] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)

    # 仅当恰好一个非 null 分支且存在 null 时才提取
    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """仅为工具定义规范化可空的 JSON Schema 模式。

    将 ``["string", "null"]`` 这类可空类型与 ``oneOf/anyOf`` 可空联合
    转换为 OpenAI 兼容的 ``nullable: true`` 形式。
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]
            normalized["nullable"] = True

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            normalized["nullable"] = True
            break

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    # object 类型确保有 properties 与 required 字段
    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class _MCPWrapperBase(Tool):
    """绑定到单个 MCP 服务器会话的包装器共享的重连处理基类。

    职责：为 MCPToolWrapper / MCPResourceWrapper / MCPPromptWrapper 提供共享
    的会话引用、重连回调与超时/终止后的会话刷新逻辑。
    """

    _plugin_discoverable = False  # 不参与插件自动发现
    _name: str  # 由子类在 __init__ 中设置

    def _set_mcp_connection(self, session: Any, server_name: str) -> None:
        """设置 MCP 会话与服务器名称。"""
        self._session = session
        self._server_name = server_name
        self._reconnect: _ReconnectCallback | None = None

    def set_reconnect_handler(self, reconnect: _ReconnectCallback) -> None:
        """设置重连回调，用于会话终止或超时后重建连接。"""
        self._reconnect = reconnect

    async def _refresh_session_after_termination(
        self,
        exc: BaseException,
        already_refreshed: bool,
        capability_kind: str,
    ) -> bool:
        """会话被报告终止后，重连服务器并刷新会话。

        参数:
            exc: 触发刷新的异常。
            already_refreshed: 本轮是否已刷新过会话（避免无限重连）。
            capability_kind: 能力类型（tool/resource/prompt），用于日志。

        返回:
            成功刷新会话返回 True，否则 False。
        """
        if already_refreshed or not _is_session_terminated(exc) or self._reconnect is None:
            return False
        logger.warning(
            "MCP {} '{}' session terminated; reconnecting server '{}' before retry",
            capability_kind,
            self._name,
            self._server_name,
        )
        refreshed_tool = await self._reconnect(self._server_name, self._name, self)
        refreshed_session = getattr(refreshed_tool, "_session", None)
        if refreshed_session is None:
            logger.warning(
                "MCP {} '{}' could not refresh session for server '{}'",
                capability_kind,
                self._name,
                self._server_name,
            )
            return False
        self._session = refreshed_session
        return True

    async def _reconnect_after_timeout(self, capability_kind: str) -> bool:
        """将超时的调用视为陈旧会话，重连服务器。

        超时通常意味着 SDK 的 receive_loop 卡死——例如 MCP 服务器的浏览器
        子进程挂起，或非 JSON 的 stdout 行污染了协议流。在同一死会话上重试
        只会再次超时，因此我们拆除并重连服务器，然后在新会话上重试一次。
        安装了新会话时返回 True。
        """
        if self._reconnect is None:
            return False
        logger.warning(
            "MCP {} '{}' timed out; treating session as stale, reconnecting server '{}'",
            capability_kind,
            self._name,
            self._server_name,
        )
        refreshed_tool = await self._reconnect(self._server_name, self._name, self)
        new_session = getattr(refreshed_tool, "_session", None)
        if new_session is None:
            logger.warning(
                "MCP {} '{}' could not refresh session for server '{}'",
                capability_kind,
                self._name,
                self._server_name,
            )
            return False
        self._session = new_session
        return True


class MCPToolWrapper(_MCPWrapperBase):
    """将单个 MCP 服务器工具包装为 biscuitbot Tool。

    职责：持有 MCP 会话与工具定义，提供 name/description/parameters 属性，
    在 execute 中调用 MCP ``call_tool``，并处理超时、取消、终止与瞬时错误
    的重连/重试。
    """

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._original_name = tool_def.name  # MCP 原始工具名
        self._name = _sanitize_name(f"mcp_{server_name}_{tool_def.name}")  # 净化后的工具名
        self._description = tool_def.description or tool_def.name  # 工具描述
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._parameters = _normalize_schema_for_openai(raw_schema)  # 规范化后的参数 schema
        self._tool_timeout = tool_timeout  # 单次调用超时秒数

    @property
    def name(self) -> str:
        """工具名称。"""
        return self._name

    @property
    def description(self) -> str:
        """工具描述。"""
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数 schema。"""
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        """调用 MCP 工具并返回文本结果。

        处理超时（重连后重试一次）、取消（仅在外部取消时上抛）、会话终止
        （重连后重试）、瞬时错误（退避 1 秒后重试一次）。
        """
        from mcp import types

        retried_transient = False  # 是否已重试过瞬时错误
        refreshed_session = False  # 是否已刷新过会话
        while True:
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(self._original_name, arguments=kwargs),
                    timeout=self._tool_timeout,
                )
            except asyncio.TimeoutError:
                # 超时且未刷新过会话 → 重连后重试
                if not refreshed_session and await self._reconnect_after_timeout("tool"):
                    refreshed_session = True
                    continue
                logger.warning(
                    "MCP tool '{}' timed out after {}s", self._name, self._tool_timeout
                )
                return f"(MCP 工具 '{self._name}' 调用超时（{self._tool_timeout}s）：请检查 MCP 服务器是否响应正常)"
            except asyncio.CancelledError:
                # MCP SDK 的 anyio cancel scope 可能在超时/失败时泄漏 CancelledError。
                # 仅当任务被外部取消（如 /stop）时才上抛。
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
                return f"(MCP 工具 '{self._name}' 调用被服务器/取消信号中断)"
            except Exception as exc:
                # 会话终止 → 重连后重试
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "tool",
                ):
                    refreshed_session = True
                    continue
                if _is_transient(exc):
                    if not retried_transient:
                        retried_transient = True
                        logger.warning(
                            "MCP tool '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)  # 重试前短暂退避
                        continue
                    # 第二次瞬时失败 —— 放弃并返回重试相关消息
                    logger.exception(
                        "MCP tool '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP 工具 '{self._name}' 调用失败：{type(exc).__name__}: {exc}。请检查 MCP 服务器日志/配置)"
                logger.exception(
                    "MCP tool '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP 工具 '{self._name}' 调用失败：{type(exc).__name__}: {exc}。请检查 MCP 服务器日志/配置)"
            else:
                # 成功 —— 提取结果文本
                parts = []
                for block in result.content:
                    if isinstance(block, types.TextContent):
                        parts.append(block.text)
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP tool call failed)"  # 不可达，仅为满足类型检查器


class MCPResourceWrapper(_MCPWrapperBase):
    """将 MCP 资源 URI 包装为只读的 biscuitbot Tool。

    职责：持有 MCP 会话与资源定义，在 execute 中调用 MCP
    ``read_resource`` 读取资源内容，并处理超时、取消、终止与瞬时错误。
    """

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, resource_def, resource_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._uri = resource_def.uri  # 资源 URI
        self._name = _sanitize_name(f"mcp_{server_name}_resource_{resource_def.name}")
        desc = resource_def.description or resource_def.name
        self._description = f"[MCP Resource] {desc}\nURI: {self._uri}"
        # 资源读取无参数
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        self._resource_timeout = resource_timeout  # 读取超时秒数

    @property
    def name(self) -> str:
        """工具名称。"""
        return self._name

    @property
    def description(self) -> str:
        """工具描述。"""
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数 schema（资源读取无参数）。"""
        return self._parameters

    @property
    def read_only(self) -> bool:
        """资源读取为只读。"""
        return True

    async def execute(self, **kwargs: Any) -> str:
        """读取 MCP 资源并返回文本/二进制摘要。

        处理超时（重连后重试一次）、取消（仅在外部取消时上抛）、会话终止
        （重连后重试）、瞬时错误（退避 1 秒后重试一次）。
        """
        from mcp import types

        retried_transient = False
        refreshed_session = False
        while True:
            try:
                result = await asyncio.wait_for(
                    self._session.read_resource(self._uri),
                    timeout=self._resource_timeout,
                )
            except asyncio.TimeoutError:
                if not refreshed_session and await self._reconnect_after_timeout("resource"):
                    refreshed_session = True
                    continue
                logger.warning(
                    "MCP resource '{}' timed out after {}s", self._name, self._resource_timeout
                )
                return f"(MCP 资源 '{self._name}' 读取超时（{self._resource_timeout}s）：请检查 MCP 服务器是否响应正常)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP resource '{}' was cancelled by server/SDK", self._name)
                return f"(MCP 资源 '{self._name}' 读取被服务器/取消信号中断)"
            except Exception as exc:
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "resource",
                ):
                    refreshed_session = True
                    continue
                if _is_transient(exc):
                    if not retried_transient:
                        retried_transient = True
                        logger.warning(
                            "MCP resource '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.exception(
                        "MCP resource '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP 资源 '{self._name}' 读取失败：{type(exc).__name__}: {exc}。请检查 MCP 服务器日志/配置)"
                logger.exception(
                    "MCP resource '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP 资源 '{self._name}' 读取失败：{type(exc).__name__}: {exc}。请检查 MCP 服务器日志/配置)"
            else:
                # 提取资源内容：文本直接拼接，二进制输出字节摘要
                parts: list[str] = []
                for block in result.contents:
                    if isinstance(block, types.TextResourceContents):
                        parts.append(block.text)
                    elif isinstance(block, types.BlobResourceContents):
                        parts.append(f"[Binary resource: {len(block.blob)} bytes]")
                    else:
                        parts.append(str(block))
                return "\n".join(parts) or "(no output)"

        return "(MCP resource read failed)"  # 不可达


class MCPPromptWrapper(_MCPWrapperBase):
    """将 MCP prompt 包装为只读的 biscuitbot Tool。

    职责：持有 MCP 会话与 prompt 定义，根据 prompt 参数构造工具 schema，
    在 execute 中调用 MCP ``get_prompt`` 获取填充后的模板，并处理超时、
    取消、McpError、终止与瞬时错误。
    """

    _plugin_discoverable = False

    def __init__(self, session, server_name: str, prompt_def, prompt_timeout: int = 30):
        self._set_mcp_connection(session, server_name)
        self._prompt_name = prompt_def.name  # MCP prompt 原始名
        self._name = _sanitize_name(f"mcp_{server_name}_prompt_{prompt_def.name}")
        desc = prompt_def.description or prompt_def.name
        self._description = (
            f"[MCP Prompt] {desc}\n"
            "Returns a filled prompt template that can be used as a workflow guide."
        )
        self._prompt_timeout = prompt_timeout

        # 根据 prompt 参数构造工具参数 schema
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in prompt_def.arguments or []:
            prop: dict[str, Any] = {"type": "string"}
            if getattr(arg, "description", None):
                prop["description"] = arg.description
            properties[arg.name] = prop
            if arg.required:
                required.append(arg.name)
        self._parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    @property
    def name(self) -> str:
        """工具名称。"""
        return self._name

    @property
    def description(self) -> str:
        """工具描述。"""
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数 schema（由 prompt 参数构造）。"""
        return self._parameters

    @property
    def read_only(self) -> bool:
        """prompt 读取为只读。"""
        return True

    async def execute(self, **kwargs: Any) -> str:
        """获取填充后的 MCP prompt 模板并返回文本。

        处理超时（重连后重试一次）、取消（仅在外部取消时上抛）、McpError
        （重连后重试或返回错误码/消息）、会话终止（重连后重试）、瞬时错误
        （退避 1 秒后重试一次）。
        """
        from mcp import types
        from mcp.shared.exceptions import McpError

        retried_transient = False
        refreshed_session = False
        while True:
            try:
                result = await asyncio.wait_for(
                    self._session.get_prompt(self._prompt_name, arguments=kwargs),
                    timeout=self._prompt_timeout,
                )
            except asyncio.TimeoutError:
                if not refreshed_session and await self._reconnect_after_timeout("prompt"):
                    refreshed_session = True
                    continue
                logger.warning(
                    "MCP prompt '{}' timed out after {}s", self._name, self._prompt_timeout
                )
                return f"(MCP 提示 '{self._name}' 调用超时（{self._prompt_timeout}s）：请检查 MCP 服务器是否响应正常)"
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling() > 0:
                    raise
                logger.warning("MCP prompt '{}' was cancelled by server/SDK", self._name)
                return f"(MCP 提示 '{self._name}' 调用被服务器/取消信号中断)"
            except McpError as exc:
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "prompt",
                ):
                    refreshed_session = True
                    continue
                err = exc.args[0] if exc.args else exc
                # McpError 可能将 ErrorData.message 作为字符串传入 args[0]，
                # 也可能直接传 ErrorData 对象，需兼容两种情况
                if isinstance(err, str):
                    err_code = "unknown"
                    err_message = err
                else:
                    err_code = getattr(err, "code", "unknown")
                    err_message = getattr(err, "message", str(err))
                logger.exception(
                    "MCP prompt '{}' failed: code={} message={}",
                    self._name,
                    err_code,
                    err_message,
                )
                return f"(MCP 提示 '{self._name}' 调用失败：{err_message} [code {err_code}])"
            except Exception as exc:
                if await self._refresh_session_after_termination(
                    exc,
                    refreshed_session,
                    "prompt",
                ):
                    refreshed_session = True
                    continue
                if _is_transient(exc):
                    if not retried_transient:
                        retried_transient = True
                        logger.warning(
                            "MCP prompt '{}' hit transient error ({}), retrying once...",
                            self._name,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(1)
                        continue
                    logger.exception(
                        "MCP prompt '{}' failed after retry: {}",
                        self._name,
                        type(exc).__name__,
                    )
                    return f"(MCP 提示 '{self._name}' 调用失败：{type(exc).__name__}: {exc}。请检查 MCP 服务器日志/配置)"
                logger.exception(
                    "MCP prompt '{}' failed: {}: {}",
                    self._name,
                    type(exc).__name__,
                    exc,
                )
                return f"(MCP 提示 '{self._name}' 调用失败：{type(exc).__name__}: {exc}。请检查 MCP 服务器日志/配置)"
            else:
                # 提取 prompt 消息内容
                parts: list[str] = []
                for message in result.messages:
                    content = message.content
                    if isinstance(content, types.TextContent):
                        parts.append(content.text)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, types.TextContent):
                                parts.append(block.text)
                            else:
                                parts.append(str(block))
                    else:
                        parts.append(str(content))
                return "\n".join(parts) or "(no output)"

        return "(MCP prompt call failed)"  # 不可达


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry
) -> dict[str, AsyncExitStack]:
    """连接配置的 MCP 服务器并注册其工具、资源、prompt。

    返回服务器名 -> 其专属 AsyncExitStack 的字典。每个服务器拥有独立的栈，
    以避免配置多个 MCP 服务器时发生 cancel scope 冲突。
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    try:
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        streamable_http_client = None  # mcp<1.0 不支持 streamableHttp

    async def connect_single_server(name: str, cfg) -> tuple[str, AsyncExitStack | None]:
        """连接单个 MCP 服务器并注册其能力。"""
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            transport_type = cfg.type
            # 未显式指定传输类型时，按 command/url 推断
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    await server_stack.aclose()
                    return name, None

            # HTTP 类传输先做 URL 安全校验
            if transport_type in {"sse", "streamableHttp"}:
                ok, error = validate_url_target(cfg.url)
                if not ok:
                    logger.warning(
                        "MCP server '{}': blocked unsafe URL {} ({})",
                        name,
                        cfg.url,
                        error,
                    )
                    await server_stack.aclose()
                    return name, None

            if transport_type == "stdio":
                # stdio 传输：通过子进程 stdin/stdout 通信
                command, args, env = _normalize_windows_stdio_command(
                    cfg.command,
                    cfg.args,
                    cfg.env or None,
                )
                params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=env,
                    cwd=cfg.cwd or None,
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                # SSE 传输：先探测端口可达性，避免 anyio 清理异常崩溃事件循环
                if not await _probe_http_url(cfg.url):
                    logger.warning("MCP server '{}': {} unreachable, skipping", name, cfg.url)
                    await server_stack.aclose()
                    return name, None

                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {
                        "Accept": "application/json, text/event-stream",
                        **(cfg.headers or {}),
                        **(headers or {}),
                    }
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        event_hooks={"request": [_validate_mcp_request_url]},
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await server_stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                # streamableHttp 传输：需要 mcp>=1.0
                if streamable_http_client is None:
                    logger.warning(
                        "MCP server '{}': streamableHttp transport requires mcp>=1.0, skipping",
                        name,
                    )
                    await server_stack.aclose()
                    return name, None

                if not await _probe_http_url(cfg.url):
                    logger.warning("MCP server '{}': {} unreachable, skipping", name, cfg.url)
                    await server_stack.aclose()
                    return name, None

                http_client = await server_stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        event_hooks={"request": [_validate_mcp_request_url]},
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await server_stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                await server_stack.aclose()
                return name, None

            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            # 排空服务器通知 / stdout 解析异常，避免 SDK 的 receive_loop
            # 因无人消费容量为 0 的通道而阻塞（会卡死后续所有工具调用直到超时）。
            _start_incoming_drainer(server_stack, session, name)

            tools = await session.list_tools()
            enabled_tools = set(cfg.enabled_tools)
            allow_all_tools = "*" in enabled_tools
            registered_count = 0
            matched_enabled_tools: set[str] = set()
            available_raw_names = [tool_def.name for tool_def in tools.tools]
            available_wrapped_names = [_sanitize_name(f"mcp_{name}_{tool_def.name}") for tool_def in tools.tools]
            for tool_def in tools.tools:
                wrapped_name = _sanitize_name(f"mcp_{name}_{tool_def.name}")
                # 按 enabledTools 白名单过滤（"*" 表示全部启用）
                if (
                    not allow_all_tools
                    and tool_def.name not in enabled_tools
                    and wrapped_name not in enabled_tools
                ):
                    logger.debug(
                        "MCP: skipping tool '{}' from server '{}' (not in enabledTools)",
                        wrapped_name,
                        name,
                    )
                    continue
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)
                registered_count += 1
                if enabled_tools:
                    if tool_def.name in enabled_tools:
                        matched_enabled_tools.add(tool_def.name)
                    if wrapped_name in enabled_tools:
                        matched_enabled_tools.add(wrapped_name)

            # 提示未匹配的 enabledTools 条目，便于排查配置错误
            if enabled_tools and not allow_all_tools:
                unmatched_enabled_tools = sorted(enabled_tools - matched_enabled_tools)
                if unmatched_enabled_tools:
                    logger.warning(
                        "MCP server '{}': enabledTools entries not found: {}. Available raw names: {}. "
                        "Available wrapped names: {}",
                        name,
                        ", ".join(unmatched_enabled_tools),
                        ", ".join(available_raw_names) or "(none)",
                        ", ".join(available_wrapped_names) or "(none)",
                    )

            # 注册资源（服务器不支持时静默跳过）
            try:
                resources_result = await session.list_resources()
                for resource in resources_result.resources:
                    wrapper = MCPResourceWrapper(
                        session, name, resource, resource_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug(
                        "MCP: registered resource '{}' from server '{}'", wrapper.name, name
                    )
            except Exception as e:
                logger.debug("MCP server '{}': resources not supported or failed: {}", name, e)

            # 注册 prompt（服务器不支持时静默跳过）
            try:
                prompts_result = await session.list_prompts()
                for prompt in prompts_result.prompts:
                    wrapper = MCPPromptWrapper(
                        session, name, prompt, prompt_timeout=cfg.tool_timeout
                    )
                    registry.register(wrapper)
                    registered_count += 1
                    logger.debug("MCP: registered prompt '{}' from server '{}'", wrapper.name, name)
            except Exception as e:
                logger.debug("MCP server '{}': prompts not supported or failed: {}", name, e)

            logger.info(
                "MCP server '{}': connected, {} capabilities registered", name, registered_count
            )
            return name, server_stack

        except Exception as e:
            # 检测 stdio 协议污染（非 JSON 输出到 stdout）并给出修复提示
            hint = ""
            text = str(e).lower()
            if any(
                marker in text
                for marker in (
                    "parse error",
                    "invalid json",
                    "unexpected token",
                    "jsonrpc",
                    "content-length",
                )
            ):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )
            logger.exception("MCP server '{}': failed to connect: {}", name, hint)
            with suppress(Exception):
                await server_stack.aclose()
            return name, None

    server_stacks: dict[str, AsyncExitStack] = {}

    for name, cfg in mcp_servers.items():
        try:
            result = await connect_single_server(name, cfg)
        except Exception as e:
            logger.exception("MCP server '{}' connection failed: {}", name, e)
            continue
        if result is not None and result[1] is not None:
            server_stacks[result[0]] = result[1]

    return server_stacks


def session_extra(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回用于 MCP 预设附加的持久化会话 kwargs。"""
    mcp_presets = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    return {"mcp_presets": mcp_presets} if isinstance(mcp_presets, list) and mcp_presets else {}


def runtime_lines(
    message: Any,
    *,
    available_server_names: set[str] | None = None,
    configured_server_names: set[str] | None = None,
    connected_server_names: set[str] | None = None,
    skip: bool = False,
) -> list[str]:
    """返回当前轮次模型可见的 MCP 预设标注。

    根据消息元数据中的 mcp_presets 列表，结合已配置/已连接的服务器名集合，
    生成提示行：未加载配置、连接未存活、或正常可用三种情况。
    """
    if skip:
        return []
    if configured_server_names is None:
        configured_server_names = available_server_names
    if connected_server_names is None:
        connected_server_names = available_server_names
    metadata = message.metadata if isinstance(getattr(message, "metadata", None), Mapping) else None
    structured = metadata.get("mcp_presets") if isinstance(metadata, Mapping) else None
    if not isinstance(structured, list):
        return []

    lines: list[str] = []
    # 最多取前 8 个预设，避免上下文过长
    for item in structured[:8]:
        if not isinstance(item, Mapping):
            continue
        raw_name = str(item.get("name") or "").strip().lower()
        if not raw_name:
            continue
        display = str(item.get("display_name") or raw_name).strip() or raw_name
        transport = str(item.get("transport") or "mcp").strip() or "mcp"
        prefix = f"mcp_{raw_name}_"
        # 情况 1：WebUI 已配置但网关未加载最新设置
        if configured_server_names is not None and raw_name not in configured_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured in WebUI Settings, "
                "but this gateway has not loaded the latest MCP settings yet. "
                f"Tools with prefix `{prefix}` may not be available yet; if they are missing, "
                "tell the user to restart biscuitbot."
            )
            continue
        # 情况 2：已配置但连接未存活
        if connected_server_names is not None and raw_name not in connected_server_names:
            lines.append(
                "MCP Preset Attachment: "
                f"@{raw_name} ({display}; transport={transport}) is configured, "
                "but its MCP connection is not currently live. "
                f"Tools with prefix `{prefix}` may be unavailable; tell the user to open Settings, "
                "run the preset test, and restart biscuitbot only if hot reload is unavailable."
            )
            continue
        # 情况 3：正常可用
        lines.append(
            "MCP Preset Attachment: "
            f"@{raw_name} ({display}; transport={transport}; tool_prefix={prefix}). "
            f"Prefer available tools whose names start with `{prefix}` for this request; "
            "do not substitute shell commands for this MCP integration unless the user asks."
        )
    return lines


async def connect_missing_servers(state: Any, registry: ToolRegistry) -> None:
    """连接当前未存活的已配置 MCP 服务器。"""
    missing_servers = {
        name: cfg for name, cfg in state._mcp_servers.items() if name not in state._mcp_stacks
    }
    # 正在连接中或无缺失服务器时直接返回
    if state._mcp_connecting or not missing_servers:
        return
    state._mcp_connecting = True
    try:
        connected = await connect_mcp_servers(missing_servers, registry)
        state._mcp_stacks.update(connected)
        _attach_reconnect_handlers(state, registry, connected)
        state._mcp_connected = bool(state._mcp_stacks)
        if connected:
            logger.info("MCP connected servers: {}", sorted(connected))
        else:
            logger.warning("No MCP servers connected successfully (will retry next message)")
    except asyncio.CancelledError:
        logger.warning("MCP connection cancelled (will retry next message)")
        state._mcp_connected = bool(state._mcp_stacks)
    except Exception as e:
        logger.warning("Failed to connect MCP servers (will retry next message): {}", e)
        state._mcp_connected = bool(state._mcp_stacks)
    finally:
        state._mcp_connecting = False


async def reload_servers(state: Any, registry: ToolRegistry) -> dict[str, Any]:
    """将活跃 MCP 连接与当前配置文件对账（热重载）。

    比较 current 与 next 配置，关闭已移除/变更的服务器，连接新增/变更/缺失
    的服务器，返回操作结果字典。
    """
    async with _reload_lock(state):
        try:
            from biscuitbot.config.loader import load_config, resolve_config_env_vars

            config = resolve_config_env_vars(load_config())
            next_servers = dict(config.tools.mcp_servers)
        except Exception as exc:
            logger.warning("MCP hot reload could not read config: {}", exc)
            return {
                "ok": False,
                "message": "Could not reload MCP config. Restart biscuitbot to pick up changes.",
                "requires_restart": True,
                "error": str(exc),
            }

        current_servers = dict(state._mcp_servers)
        current_names = set(current_servers)
        next_names = set(next_servers)
        removed = sorted(current_names - next_names)  # 已移除的服务器
        added = sorted(next_names - current_names)  # 新增的服务器
        changed = sorted(  # 配置签名变化的服务器
            name
            for name in current_names & next_names
            if _server_signature(current_servers[name]) != _server_signature(next_servers[name])
        )

        tools_removed = 0
        # 关闭已移除与变更的服务器
        for name in [*removed, *changed]:
            tools_removed += _unregister_server_tools(state, registry, name)
            await _close_server(state, name)

        state._mcp_servers = next_servers
        # 重试之前未连接成功的服务器（非新增/变更）
        retry_missing = sorted(
            name
            for name in next_names
            if name not in state._mcp_stacks and name not in set(added) | set(changed)
        )
        to_connect_names = sorted(set(added) | set(changed) | set(retry_missing))
        to_connect = {name: next_servers[name] for name in to_connect_names}
        connected: dict[str, AsyncExitStack] = {}
        if to_connect:
            connected = await connect_mcp_servers(to_connect, registry)
            state._mcp_stacks.update(connected)
            _attach_reconnect_handlers(state, registry, connected)

        state._mcp_connected = bool(state._mcp_stacks)
        failed = sorted(set(to_connect) - set(connected))
        unchanged = not removed and not added and not changed and not retry_missing
        ok = not failed
        if failed:
            message = "MCP config reloaded, but some servers did not connect: " + ", ".join(failed)
        elif unchanged:
            message = "MCP config is already live."
        elif retry_missing and not added and not changed and not removed:
            message = "MCP connections refreshed without restarting biscuitbot."
        else:
            message = "MCP config reloaded without restarting biscuitbot."

        logger.info(
            "MCP hot reload: added={} changed={} removed={} retried={} connected={} failed={} tools_removed={}",
            added,
            changed,
            removed,
            retry_missing,
            sorted(connected),
            failed,
            tools_removed,
        )
        return {
            "ok": ok,
            "message": message,
            "added": added,
            "changed": changed,
            "removed": removed,
            "retried": retry_missing,
            "connected": sorted(state._mcp_stacks),
            "configured": sorted(state._mcp_servers),
            "failed": failed,
            "tools_removed": tools_removed,
            "requires_restart": False,
        }


async def request_mcp_reload(bus: Any, *, timeout: float = 15.0) -> dict[str, Any]:
    """请求运行中的 agent 循环对账活跃 MCP 连接。

    通过消息总线发布运行时控制信号，等待 agent 循环处理并返回结果。
    超时则返回需要重启的提示。
    """
    loop = asyncio.get_running_loop()
    ack: asyncio.Future[dict[str, Any]] = loop.create_future()
    await bus.publish_inbound(
        InboundMessage(
            channel="system",
            sender_id="webui-settings",
            chat_id="runtime",
            content=RUNTIME_CONTROL_MCP_RELOAD,
            metadata={
                INBOUND_META_RUNTIME_CONTROL: RUNTIME_CONTROL_MCP_RELOAD,
                RUNTIME_CONTROL_ACK: ack,
            },
        )
    )
    try:
        result = await asyncio.wait_for(ack, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": "MCP hot reload timed out. Restart biscuitbot to pick up changes.",
            "requires_restart": True,
        }
    return result if isinstance(result, dict) else {
        "ok": False,
        "message": "MCP hot reload returned an unexpected response.",
        "requires_restart": True,
    }


async def handle_runtime_control(state: Any, msg: InboundMessage, registry: ToolRegistry) -> bool:
    """处理运行时控制消息（MCP 热重载）。

    返回 True 表示已处理该控制消息。通过 ack Future 将结果回传给请求方。
    """
    metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
    control = metadata.get(INBOUND_META_RUNTIME_CONTROL)
    if control != RUNTIME_CONTROL_MCP_RELOAD:
        return False

    ack = metadata.get(RUNTIME_CONTROL_ACK)
    try:
        result = await reload_servers(state, registry)
    except Exception as exc:
        logger.exception("MCP hot reload failed")
        result = {
            "ok": False,
            "message": "MCP hot reload failed. Restart biscuitbot to pick up changes.",
            "requires_restart": True,
            "error": str(exc),
        }
    # 将结果通过 ack Future 回传
    if isinstance(ack, asyncio.Future) and not ack.done():
        ack.set_result(result)
    return True


def _reload_lock(state: Any) -> asyncio.Lock:
    """获取（或创建）按 state 实例隔离的重载锁。"""
    try:
        return _RELOAD_LOCKS[state]
    except KeyError:
        lock = asyncio.Lock()
        _RELOAD_LOCKS[state] = lock
        return lock


def _attach_reconnect_handlers(
    state: Any,
    registry: ToolRegistry,
    server_names: Mapping[str, Any] | set[str] | list[str] | tuple[str, ...],
) -> None:
    """为新连接的服务器的工具附加重连处理器。

    重连处理器处理 anyio cancel scope 的 task 亲和性：在子任务中触发重连时，
    延迟到 owner task 执行，避免 cancel scope 跨任务导致泄漏。
    """
    async def reconnect(server_name: str, tool_name: str, stale_tool: Tool) -> Tool | None:
        # anyio cancel scope（由 connect_mcp_servers 通过 stdio_client 进入）
        # 是 task 局部的：必须在同一 task 中进入并退出。此闭包运行在
        # _dispatch 子任务（工具执行）中，但 MCP 栈由 run() 任务
        # （_mcp_owner_task）拥有。在此调用 _refresh_terminated_server 会把
        # 新栈的 cancel scope 关联到子任务；子任务结束时，owner 任务的
        # close_mcp() 无法退出它们，anyio 抛出 "Attempted to exit cancel
        # scope in a different task"，导致 stdio_client 生成器泄漏并产生
        # 嘈杂的 GC 终结器回溯。
        #
        # 当不是 owner（且 owner 仍存活）时，通过 Future 将整个重连推迟到
        # owner 任务。owner 从主循环空闲分支（process_pending_reconnects）
        # 排空队列并自行运行 _refresh_terminated_server，保持 cancel scope
        # 关联到 owner。子任务阻塞在 Future 上（≤1s + 连接时间），对已陈旧
        # 的会话来说是可接受的。
        owner = getattr(state, "_mcp_owner_task", None)
        current = asyncio.current_task()
        requests = getattr(state, "_mcp_reconnect_requests", None)
        if (
            owner is not None
            and current is not None
            and current is not owner
            and not owner.done()
            and requests is not None
        ):
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            requests.append((server_name, tool_name, stale_tool, future))
            logger.debug(
                "MCP server '{}' reconnect deferred to owner task (from sub-task {})",
                server_name,
                current.get_name(),
            )
            return await future
        return await _refresh_terminated_server(
            state,
            registry,
            server_name,
            tool_name,
            stale_tool,
        )

    for server_name in server_names:
        prefix = _tool_prefix(server_name)
        for tool_name in list(registry.tool_names):
            if not tool_name.startswith(prefix):
                continue
            tool = registry.get(tool_name)
            if isinstance(tool, _MCPWrapperBase):
                tool.set_reconnect_handler(reconnect)


async def _refresh_terminated_server(
    state: Any,
    registry: ToolRegistry,
    server_name: str,
    tool_name: str,
    stale_tool: Tool,
) -> Tool | None:
    """重连已终止会话的服务器并返回新工具实例。"""
    async with _reload_lock(state):
        cfg = state._mcp_servers.get(server_name)
        if cfg is None:
            logger.warning(
                "MCP server '{}' session terminated but is no longer configured",
                server_name,
            )
            return None

        # 若已有其他任务重连过，直接返回当前工具
        current_tool = registry.get(tool_name)
        if (
            current_tool is not None
            and current_tool is not stale_tool
            and server_name in state._mcp_stacks
        ):
            return current_tool

        logger.warning("MCP server '{}' session terminated; refreshing connection", server_name)
        _unregister_server_tools(state, registry, server_name)
        await _close_server(state, server_name)

        connected = await connect_mcp_servers({server_name: cfg}, registry)
        state._mcp_stacks.update(connected)
        _attach_reconnect_handlers(state, registry, connected)
        state._mcp_connected = bool(state._mcp_stacks)
        if server_name not in connected:
            logger.warning("MCP server '{}' reconnect failed after session termination", server_name)
            return None
        return registry.get(tool_name)


async def process_pending_reconnects(state: Any, registry: ToolRegistry) -> None:
    """在 owner task 中运行被 _dispatch 子任务推迟的重连请求。

    ``_attach_reconnect_handlers`` 的重连闭包检测到自身运行在子任务（非
    MCP owner task）时，不直接调用 ``_refresh_terminated_server``（那会在
    错误的 task 中进入 anyio cancel scope），而是将请求连同 ``Future``
    入队 ``state._mcp_reconnect_requests`` 并阻塞等待。

    本函数排空该队列，**在 owner task 中**运行
    ``_refresh_terminated_server``，使新栈的 cancel scope 在后续会关闭它
    的同一 task 中进入。必须从 owner task（AgentLoop._run_main_loop 空闲
    分支）调用。
    """
    requests = getattr(state, "_mcp_reconnect_requests", None)
    if not requests:
        return
    # 排空快照；处理期间到达的请求等待下一次空闲 tick（最多约 1s 后，
    # 因为主循环以 1s 超时轮询）。
    pending = list(requests)
    requests.clear()
    for server_name, tool_name, stale_tool, future in pending:
        if future.done():
            # 请求方子任务已被取消/超时；跳过。
            continue
        try:
            tool = await _refresh_terminated_server(
                state, registry, server_name, tool_name, stale_tool
            )
            if not future.done():
                future.set_result(tool)
        except Exception as exc:
            # 不让单次重连失败崩溃 owner task。
            # 解析 future 使等待的子任务能继续；若 future 已被取消，则抑制
            # 以避免泄漏未检索的异常。
            if not future.done():
                future.set_exception(exc)
            else:
                logger.debug(
                    "MCP server '{}' reconnect failed after future resolved: {}",
                    server_name,
                    exc,
                )


def _server_signature(cfg: Any) -> Any:
    """返回服务器配置的可比较签名（用于检测配置变更）。"""
    if hasattr(cfg, "model_dump"):
        return cfg.model_dump(mode="json")
    return cfg


def _tool_prefix(server_name: str) -> str:
    """返回某服务器的工具名前缀（如 ``mcp_myserver_``）。"""
    return _sanitize_name(f"mcp_{server_name}_")


def _unregister_server_tools(state: Any, registry: ToolRegistry, server_name: str) -> int:
    """注销某服务器的所有工具，返回注销数量。"""
    prefix = _tool_prefix(server_name)
    removed = 0
    for tool_name in list(registry.tool_names):
        if tool_name.startswith(prefix):
            registry.unregister(tool_name)
            removed += 1
    return removed


async def _close_server(state: Any, server_name: str) -> None:
    """关闭某服务器的连接栈。

    处理 anyio cancel scope 的 task 亲和性：栈必须在打开它的同一 task
    （run() 任务，即 _mcp_owner_task）中关闭。当 _dispatch 子任务触发
    重连时，无法直接关闭旧栈——将其推迟到 owner task 通过
    _close_deferred_mcp_stacks() 安全关闭。这避免了 "Attempted to exit
    cancel scope in a different task" 及由此泄漏的 stdio_client 生成器
    产生的嘈杂 GC 终结器回溯。
    """
    stack = state._mcp_stacks.pop(server_name, None)
    if stack is None:
        return
    owner = getattr(state, "_mcp_owner_task", None)
    deferred = getattr(state, "_mcp_deferred_stacks", None)
    # 子任务中关闭需推迟到 owner task
    if (
        owner is not None
        and deferred is not None
        and not owner.done()
        and asyncio.current_task() is not owner
    ):
        deferred.append((server_name, stack))
        logger.debug(
            "MCP server '{}' stack deferred to owner task for safe close",
            server_name,
        )
        return
    try:
        await stack.aclose()
    except (RuntimeError, BaseExceptionGroup):
        logger.debug("MCP server '{}' cleanup error (can be ignored)", server_name)
