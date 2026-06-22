"""平台规范 API 路由。

实现桓宸智科 AI 平台 v4.1.0 接入规范的所有端点：
- GET  /api/health              健康检测（无需 JWT）
- POST /api/chat                同步对话
- POST /api/chat/stream         SSE 流式对话
- GET  /api/agent/info          智能体元信息（无需 JWT）
- GET  /api/chat/history/{id}   会话历史
- DELETE /api/chat/sessions/{id} 清除会话
- POST /api/documents           文档上传
"""
from __future__ import annotations

import asyncio
import contextlib
import json as _json
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from hczkbot.platform.auth import PlatformAuth, PlatformUser
from hczkbot.platform.config import PlatformConfig
from hczkbot.platform.skills_client import PlatformSkillsClient
from hczkbot.platform.skills_tool import build_skill_tools
from hczkbot.utils.helpers import safe_filename


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _error_response(status: int, message: str, code: str = "", session_id: str | None = None) -> web.Response:
    body: dict[str, Any] = {"error": message}
    if code:
        body["code"] = code
    if session_id:
        body["session_id"] = session_id
    return web.json_response(body, status=status)


def _sse_event(event_type: str, **fields: Any) -> bytes:
    """构造 SSE 事件，格式：data: {JSON}\\n\\n。"""
    payload = {"type": event_type, **fields}
    return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _session_key_for(user_id: str, session_id: str | None) -> str:
    """生成平台会话隔离 key。

    格式：platform:{user_id}:{session_id}，确保不同用户会话隔离。
    """
    sid = session_id or "default"
    return f"platform:{user_id}:{sid}"


def _response_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


# ---------------------------------------------------------------------------
# 路由处理器
# ---------------------------------------------------------------------------


async def handle_platform_health(request: web.Request) -> web.Response:
    """GET /api/health — 健康检测（无需 JWT）。"""
    agent_loop = request.app["agent_loop"]
    start_time = request.app.get("start_time", time.time())

    components: dict[str, Any] = {}
    # LLM provider 状态
    try:
        provider = getattr(agent_loop, "provider", None)
        if provider:
            components["llm"] = {"status": "available"}
    except Exception:
        components["llm"] = {"status": "unavailable"}

    return _json_response({
        "status": "ok",
        "version": request.app.get("agent_version", "1.0.0"),
        "uptime": int(time.time() - start_time),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "components": components,
        "runtime": {
            "active_sessions": len(getattr(agent_loop, "_session_locks", {})),
        },
    })


async def handle_platform_agent_info(request: web.Request) -> web.Response:
    """GET /api/agent/info — 智能体元信息（无需 JWT）。"""
    config: PlatformConfig = request.app["platform_config"]
    agent_loop = request.app["agent_loop"]

    # 收集已注册工具名（排除平台动态工具）
    tools_list: list[dict[str, str]] = []
    try:
        registry = agent_loop.tools
        for name in registry.tool_names:
            if not name.startswith("platform_"):
                tool = registry.get(name)
                if tool:
                    tools_list.append({"name": name, "description": tool.description[:100]})
    except Exception:
        pass

    # 获取当前模型名
    model_name = request.app.get("model_name", "hczkbot")

    return _json_response({
        "name": config.agent_name,
        "version": config.agent_version,
        "description": config.agent_description,
        "type": config.agent_type or None,
        "capabilities": {
            "knowledge_retrieval": True,  # 通过平台 Skills 支持
            "tool_calling": True,
            "multi_turn": True,
            "streaming": True,
            "thinking": True,
        },
        "model": model_name,
        "tools": tools_list,
    })


async def handle_platform_chat(request: web.Request) -> web.Response:
    """POST /api/chat — 同步对话。"""
    auth: PlatformAuth = request.app["platform_auth"]
    user: PlatformUser = auth.verify_request(request)

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body")

    message = body.get("message", "").strip()
    if not message:
        return _error_response(400, "message 字段不能为空")

    session_id = body.get("session_id")
    user_id_from_body = body.get("user_id", user.user_id)
    available_skills = body.get("available_skills") or []
    tools_discovery_endpoint = body.get("tools_discovery_endpoint") or ""

    agent_loop = request.app["agent_loop"]
    timeout_s: float = request.app.get("request_timeout", 120.0)
    session_key = _session_key_for(user.user_id, session_id)

    # 构建平台 Skills 工具（按需加载）
    platform_tools = await _build_platform_tools(
        request, user, available_skills, tools_discovery_endpoint
    )

    # 构建临时 ToolRegistry（合并默认工具 + 平台工具）
    tools_registry = await _build_merged_registry(agent_loop, platform_tools)

    session_locks: dict[str, asyncio.Lock] = request.app["session_locks"]
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    logger.info(
        "平台对话 session_key={} user_id={} tools={} text={}",
        session_key, user.user_id, len(platform_tools), message[:80],
    )

    try:
        async with session_lock:
            response = await asyncio.wait_for(
                agent_loop.process_direct(
                    content=message,
                    session_key=session_key,
                    channel="platform",
                    chat_id=session_id or user.user_id,
                    tools=tools_registry,
                ),
                timeout=timeout_s,
            )
            reply = _response_text(response)
            if not reply.strip():
                reply = "（智能体未返回内容）"
    except asyncio.TimeoutError:
        return _error_response(504, f"请求超时（{timeout_s}s）", "TIMEOUT", session_id)
    except Exception as e:
        logger.exception("平台对话处理失败 session={}", session_key)
        return _error_response(500, "内部服务错误", "INTERNAL", session_id)

    # 收集元数据
    metadata = _build_metadata(agent_loop, platform_tools)

    return _json_response({
        "reply": reply,
        "session_id": session_id,
        "metadata": metadata,
    })


async def handle_platform_chat_stream(request: web.Request) -> web.StreamResponse:
    """POST /api/chat/stream — SSE 流式对话。"""
    auth: PlatformAuth = request.app["platform_auth"]
    user: PlatformUser = auth.verify_request(request)

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body")

    message = body.get("message", "").strip()
    if not message:
        return _error_response(400, "message 字段不能为空")

    session_id = body.get("session_id")
    available_skills = body.get("available_skills") or []
    tools_discovery_endpoint = body.get("tools_discovery_endpoint") or ""

    agent_loop = request.app["agent_loop"]
    timeout_s: float = request.app.get("request_timeout", 120.0)
    session_key = _session_key_for(user.user_id, session_id)

    platform_tools = await _build_platform_tools(
        request, user, available_skills, tools_discovery_endpoint
    )
    tools_registry = await _build_merged_registry(agent_loop, platform_tools)

    session_locks: dict[str, asyncio.Lock] = request.app["session_locks"]
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    await resp.prepare(request)

    # 发送思考事件
    await resp.write(_sse_event("thinking", content="正在分析您的请求..."))

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    stream_failed = False
    emitted_content = False

    async def _on_stream(token: str) -> None:
        nonlocal emitted_content
        if token:
            emitted_content = True
        await queue.put(token)

    async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
        return None

    async def _run() -> None:
        nonlocal stream_failed
        try:
            async with session_lock:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=message,
                        session_key=session_key,
                        channel="platform",
                        chat_id=session_id or user.user_id,
                        tools=tools_registry,
                        on_stream=_on_stream,
                        on_stream_end=_on_stream_end,
                    ),
                    timeout=timeout_s,
                )
                if not emitted_content:
                    text = _response_text(response)
                    if text.strip():
                        await queue.put(text)
        except Exception:
            stream_failed = True
            logger.exception("平台流式对话异常 session={}", session_key)
        finally:
            await queue.put(None)

    task = asyncio.create_task(_run())
    try:
        while True:
            token = await queue.get()
            if token is None:
                break
            await resp.write(_sse_event("content", content=token))
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    if not stream_failed:
        metadata = _build_metadata(agent_loop, platform_tools)
        await resp.write(_sse_event("done", **metadata))
    return resp


async def handle_platform_chat_history(request: web.Request) -> web.Response:
    """GET /api/chat/history/{session_id} — 会话历史。"""
    auth: PlatformAuth = request.app["platform_auth"]
    user: PlatformUser = auth.verify_request(request)

    session_id = request.match_info.get("session_id")
    if not session_id:
        return _error_response(400, "缺少 session_id")

    agent_loop = request.app["agent_loop"]
    session_key = _session_key_for(user.user_id, session_id)

    try:
        session_manager = getattr(agent_loop, "sessions", None)
        if not session_manager:
            return _error_response(404, "会话管理器不可用")

        session = await session_manager.get_session(session_key)
        if not session:
            return _json_response({
                "session_id": session_id,
                "messages": [],
                "message_count": 0,
            })

        messages: list[dict[str, str]] = []
        for msg in session.messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                # 提取文本内容
                content = " ".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            messages.append({"role": role, "content": str(content)})

        return _json_response({
            "session_id": session_id,
            "messages": messages,
            "message_count": len(messages),
        })
    except Exception as e:
        logger.exception("获取会话历史失败 session={}", session_key)
        return _error_response(500, f"获取历史失败: {e}")


async def handle_platform_clear_session(request: web.Request) -> web.Response:
    """DELETE /api/chat/sessions/{session_id} — 清除会话。"""
    auth: PlatformAuth = request.app["platform_auth"]
    user: PlatformUser = auth.verify_request(request)

    session_id = request.match_info.get("session_id")
    if not session_id:
        return _error_response(400, "缺少 session_id")

    agent_loop = request.app["agent_loop"]
    session_key = _session_key_for(user.user_id, session_id)

    try:
        session_manager = getattr(agent_loop, "sessions", None)
        if session_manager:
            await session_manager.delete_session(session_key)
        return _json_response({"status": "ok", "session_id": session_id})
    except Exception as e:
        logger.exception("清除会话失败 session={}", session_key)
        return _error_response(500, f"清除会话失败: {e}")


async def handle_platform_documents(request: web.Request) -> web.Response:
    """POST /api/documents — 文档上传。"""
    auth: PlatformAuth = request.app["platform_auth"]
    user: PlatformUser = auth.verify_request(request)

    config: PlatformConfig = request.app["platform_config"]
    workspace = request.app.get("workspace", "")
    documents_dir = config.resolve_documents_dir(workspace)
    documents_dir.mkdir(parents=True, exist_ok=True)

    try:
        reader = await request.multipart()
        file_data: bytes | None = None
        file_name: str | None = None
        collection_name: str = "default"
        document_type: str | None = None

        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                file_data = await part.read()
                file_name = part.filename or "upload.bin"
            elif part.name == "collection_name":
                collection_name = (await part.read()).decode("utf-8").strip()
            elif part.name == "user_id":
                # 规范要求：使用 JWT 中的 user_id，忽略 body 中的
                await part.read()
            elif part.name == "document_type":
                document_type = (await part.read()).decode("utf-8").strip()

        if not file_data or not file_name:
            return _error_response(400, "缺少 file 字段")

        # 保存文档
        safe_name = safe_filename(file_name)
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        dest = documents_dir / f"{doc_id}_{safe_name}"
        dest.write_bytes(file_data)

        logger.info(
            "平台文档上传 user_id={} collection={} file={} size={}",
            user.user_id, collection_name, safe_name, len(file_data),
        )

        return _json_response({
            "status": "ok",
            "document_id": doc_id,
            "collection_name": collection_name,
            "chunk_count": 0,  # 实际分块由平台知识库处理
            "message": "文档已接收，请通过知识库工具摄入",
        })
    except Exception as e:
        logger.exception("文档上传失败")
        return _error_response(500, f"文档上传失败: {e}")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


async def _build_platform_tools(
    request: web.Request,
    user: PlatformUser,
    available_skills: list[dict[str, Any]],
    tools_discovery_endpoint: str,
) -> list[Any]:
    """构建平台 Skills 工具列表（按需加载）。"""
    if not available_skills or not tools_discovery_endpoint:
        return []

    config: PlatformConfig = request.app["platform_config"]
    client = PlatformSkillsClient(
        jwt_token=user.token,
        request_timeout=config.skills_request_timeout,
        tool_timeout=config.skills_tool_timeout,
    )

    try:
        return await build_skill_tools(
            client=client,
            tools_discovery_endpoint=tools_discovery_endpoint,
            available_skills=available_skills,
            user_id=user.user_id,
        )
    except Exception as e:
        logger.warning("构建平台工具失败: {}", e)
        return []


async def _build_merged_registry(agent_loop: Any, platform_tools: list[Any]) -> Any:
    """构建合并的 ToolRegistry：默认工具 + 平台工具。

    创建一个新的 ToolRegistry，复制默认工具并追加平台工具，
    避免污染全局 registry。
    """
    from hczkbot.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    # 复制默认工具
    try:
        for name in agent_loop.tools.tool_names:
            tool = agent_loop.tools.get(name)
            if tool:
                registry.register(tool)
    except Exception:
        pass

    # 追加平台工具
    for tool in platform_tools:
        try:
            registry.register(tool)
        except Exception as e:
            logger.warning("注册平台工具失败: {}", e)

    return registry


def _build_metadata(agent_loop: Any, platform_tools: list[Any]) -> dict[str, Any]:
    """构建响应元数据。"""
    metadata: dict[str, Any] = {}
    try:
        usage = getattr(agent_loop, "_last_usage", None)
        if usage:
            metadata["input_tokens"] = usage.get("prompt_tokens", 0)
            metadata["output_tokens"] = usage.get("completion_tokens", 0)
    except Exception:
        pass

    if platform_tools:
        metadata["tool_calls"] = len(platform_tools)
        metadata["retrieval_used"] = True

    return metadata


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------


def register_platform_routes(app: web.Application) -> None:
    """将平台规范路由注册到 aiohttp 应用。"""
    app.router.add_get("/api/health", handle_platform_health)
    app.router.add_post("/api/chat", handle_platform_chat)
    app.router.add_post("/api/chat/stream", handle_platform_chat_stream)
    app.router.add_get("/api/agent/info", handle_platform_agent_info)
    app.router.add_get("/api/chat/history/{session_id}", handle_platform_chat_history)
    app.router.add_delete("/api/chat/sessions/{session_id}", handle_platform_clear_session)
    app.router.add_post("/api/documents", handle_platform_documents)
