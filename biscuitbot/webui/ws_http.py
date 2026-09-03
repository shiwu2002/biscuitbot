"""HTTP API handler extracted from WebSocketChannel.

Handles all non-WebSocket HTTP routes: bootstrap, sessions, settings,
media, commands, sidebar state, static file serving, and token management.

Also houses shared HTTP utility functions used by both this module and
``websocket.py`` to avoid circular imports.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from loguru import logger
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from biscuitbot.agent.employees import (
    EmployeeStore,
    EmployeeValidationError,
)
from biscuitbot.command.builtin import builtin_command_palette
from biscuitbot.cron.session_turns import is_bound_cron_job
from biscuitbot.cron.types import CronJob, CronSchedule
from biscuitbot.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from biscuitbot.webui.file_preview import WebUIFilePreviewError, file_preview_payload
from biscuitbot.webui.gateway_tokens import GatewayTokenStore, token_response_payload
from biscuitbot.webui.http_utils import (
    case_insensitive_header as _case_insensitive_header,
)
from biscuitbot.webui.http_utils import (
    host_for_url as _host_for_url,
)
from biscuitbot.webui.http_utils import (
    http_error as _http_error,
)
from biscuitbot.webui.http_utils import (
    http_json_response as _http_json_response,
)
from biscuitbot.webui.http_utils import (
    http_response as _http_response,
)
from biscuitbot.webui.http_utils import (
    is_localhost as _is_localhost,
)
from biscuitbot.webui.http_utils import (
    issue_route_secret_matches as _issue_route_secret_matches,
)
from biscuitbot.webui.http_utils import (
    normalize_config_path as _normalize_config_path,
)
from biscuitbot.webui.http_utils import (
    parse_query as _parse_query,
)
from biscuitbot.webui.http_utils import (
    parse_request_path as _parse_request_path,
)
from biscuitbot.webui.http_utils import (
    query_first as _query_first,
)
from biscuitbot.webui.http_utils import (
    safe_host_header as _safe_host_header,
)
from biscuitbot.webui.media_gateway import WebUIMediaGateway
from biscuitbot.webui.session_automations import (
    all_automations_payload,
    serialize_automation_jobs,
    session_automation_jobs,
    session_automations_payload,
)
from biscuitbot.webui.session_list_index import list_webui_sessions
from biscuitbot.webui.sidebar_state import (
    read_webui_sidebar_state,
    write_webui_sidebar_state,
)
from biscuitbot.webui.skills_api import (
    SkillDeletionError,
    delete_workspace_skill,
    webui_skill_detail_payload,
    webui_skills_payload,
)
from biscuitbot.webui.capabilities_api import (
    capabilities_payload,
    capability_detail_payload,
)
from biscuitbot.webui.talent_market import (
    TalentMarketError,
    install_talent_employee,
    read_talent_market_registry_urls,
    talent_catalog_payload,
)
from biscuitbot.webui.thread_disk import delete_webui_thread
from biscuitbot.webui.transcript import build_webui_thread_response
from biscuitbot.webui.workspaces import WebUIWorkspaceController

_SLOW_WEBUI_HTTP_LOG_MS = 1_000
_AUTOMATION_VALUES_HEADER = "X-Biscuitbot-Automation-Values"
_EMPLOYEE_VALUES_HEADER = "X-Biscuitbot-Employee-Values"

if TYPE_CHECKING:
    from biscuitbot.bus.queue import MessageBus
    from biscuitbot.cron.service import CronService
    from biscuitbot.session.manager import SessionManager


def _decode_api_key(raw_key: str) -> str | None:
    key = unquote(raw_key)
    _api_key_re = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")
    if _api_key_re.match(key) is None:
        return None
    return key


_AVATAR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _resolve_avatar_path(name: str) -> Path | None:
    """定位员工头像文件，按优先级：源码 ``images/bot/`` → 打包内置 ``biscuitbot/avatars/``
    → 实例数据目录（人才市场下载的头像，``<config dir>/avatars/``）。

    仅接受简单文件名（拒绝路径分隔符/目录穿越），命中且文件存在才返回物理路径，
    否则返回 ``None``。
    """
    if not _AVATAR_NAME_RE.match(name or ""):
        return None
    here = Path(__file__).resolve().parent  # biscuitbot/webui
    candidates = (
        here.parent.parent / "images" / "bot" / name,  # 源码运行（仓库根 images/bot）
        here.parent / "avatars" / name,  # 打包（--add-data 内置到 biscuitbot/avatars）
        _get_user_avatar_dir() / name,  # 实例数据目录（市场下载的头像）
    )
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _get_user_avatar_dir() -> Path:
    """返回实例级头像目录（``<config dir>/avatars/``），懒导入避免启动期循环依赖。"""
    from biscuitbot.config.paths import get_runtime_subdir

    return get_runtime_subdir("avatars")


def _default_model_name_from_config() -> str | None:
    try:
        from biscuitbot.config.loader import load_config
        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str:
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config() or ""


# ---------------------------------------------------------------------------
# GatewayHTTPHandler
# ---------------------------------------------------------------------------


class GatewayHTTPHandler:
    """Handles all HTTP routes served alongside the WebSocket endpoint.

    Routes HTTP requests and delegates stateful work to explicit gateway
    services owned by the composition layer.
    """

    def __init__(
        self,
        *,
        config: Any,  # WebSocketConfig
        session_manager: SessionManager | None,
        static_dist_path: Path | None,
        runtime_model_name: Callable[[], str | None] | None,
        runtime_surface: str,
        runtime_capabilities_overrides: dict[str, Any] | None,
        bus: MessageBus,
        tokens: GatewayTokenStore,
        media: WebUIMediaGateway,
        workspaces: WebUIWorkspaceController,
        skills_workspace_path: Path,
        disabled_skills: set[str] | None = None,
        cron_service: CronService | None = None,
        cron_pending_job_ids: Callable[[str], set[str]] | None = None,
        employees: EmployeeStore | None = None,
        log: Any = logger,
    ) -> None:
        self.config = config
        self.session_manager = session_manager
        self.static_dist_path = static_dist_path
        self.runtime_model_name = runtime_model_name
        self.bus = bus
        self.tokens = tokens
        self.media = media
        self.workspaces = workspaces
        self.skills_workspace_path = skills_workspace_path
        self.disabled_skills = disabled_skills or set()
        self.employees = employees
        self.cron_service = cron_service
        self.cron_pending_job_ids = cron_pending_job_ids
        self._log = log
        self._runtime_surface = runtime_surface

        from biscuitbot.webui.weixin_login import WeixinLoginManager

        self._weixin_login = WeixinLoginManager()

        from biscuitbot.webui.settings_api import runtime_capabilities as _rc
        from biscuitbot.webui.settings_routes import WebUISettingsRouter

        self._capabilities = _rc(runtime_surface, runtime_capabilities_overrides or {})
        self.settings_routes = WebUISettingsRouter(
            bus=bus,
            logger=self._log,
            check_api_token=self.check_api_token,
            parse_query=_parse_query,
            json_response=_http_json_response,
            error_response=_http_error,
            runtime_surface=runtime_surface,
            runtime_capabilities=self._capabilities,
            sign_media=self.media.sign_media_path,
        )

    def workspace_controls_available(self, connection: Any) -> bool:
        return self._runtime_surface == "native" or _is_localhost(connection)

    # -- Token management ---------------------------------------------------

    def check_api_token(self, request: WsRequest) -> bool:
        return self.tokens.check_api_token(request)

    # -- Main dispatch ------------------------------------------------------

    async def dispatch(self, connection: Any, request: WsRequest) -> Any | None:
        """Route an HTTP request. Returns Response or None."""
        got, _ = _parse_request_path(request.path)
        started = time.perf_counter()
        response: Any | None = None

        try:
            response = await self._dispatch_resolved(connection, request, got)
            return response
        finally:
            self._log_slow_http(got, response, started)

    async def _dispatch_resolved(
        self,
        connection: Any,
        request: WsRequest,
        got: str,
    ) -> Any | None:
        # Health endpoint
        if got == "/health":
            return _http_response(
                json.dumps({"status": "ok"}).encode("utf-8"),
                status=200,
                content_type="application/json",
            )

        # Token issue endpoint
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue(connection, request)

        # Bootstrap
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # Settings routes (delegated)
        response = await self.settings_routes.dispatch(request, got)
        if response is not None:
            return response

        # Session routes
        response = await self._dispatch_session_routes(request, got)
        if response is not None:
            return response

        # Media routes
        response = self._dispatch_media_routes(request, got)
        if response is not None:
            return response

        # Automation routes
        response = await self._dispatch_automation_routes(request, got)
        if response is not None:
            return response

        # Misc routes
        response = await self._dispatch_misc_routes(connection, request, got)
        if response is not None:
            return response

        # API 404 (never serve SPA for /api/ routes)
        if got.startswith("/api/"):
            return _http_error(404, "API route not found")

        # Static SPA serving
        if self.static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    def _log_slow_http(self, path: str, response: Any | None, started: float) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if elapsed_ms < _SLOW_WEBUI_HTTP_LOG_MS:
            return
        if not (path.startswith("/api/") or path == "/webui/bootstrap"):
            return
        status = getattr(response, "status_code", None)
        self._log.warning(
            "slow webui http route path={} status={} duration_ms={}",
            path,
            status if status is not None else "none",
            elapsed_ms,
        )

    # -- Token issue --------------------------------------------------------

    def _handle_token_issue(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        elif not _is_localhost(connection):
            # No secret configured and the request is not local: refuse to hand
            # out connection tokens to remote clients.
            return _http_error(403, "token issue is localhost-only without a configured secret")
        if not self.tokens.can_issue():
            self._log.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self.tokens.issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = self.tokens.issue_token(self.config.token_ttl_s)
        return _http_json_response(token_response_payload(token_value, self.config.token_ttl_s))

    # -- Bootstrap ----------------------------------------------------------

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        from biscuitbot.config.loader import load_config
        from biscuitbot.webui.settings_api import needs_setup

        # needs_setup 判定基于完整 biscuitbot 配置（self.config 只是 websocket 频道配置）
        bootstrap_config = load_config()

        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not _is_localhost(connection):
            return _http_error(403, "bootstrap is localhost-only")

        if not self.tokens.can_issue(include_api_token=True):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = self.tokens.issue_token(self.config.token_ttl_s, api_token=True)

        ws_url = self._bootstrap_ws_url(request)
        expected_path = _normalize_config_path(self.config.path)
        return _http_json_response(
            {
                "token": token,
                "ws_path": expected_path,
                "ws_url": ws_url,
                "expires_in": self.config.token_ttl_s,
                "model_name": _resolve_bootstrap_model_name(self.runtime_model_name),
                "runtime_surface": self._runtime_surface,
                "runtime_capabilities": self._capabilities,
                "needs_setup": needs_setup(bootstrap_config),
            }
        )

    def _bootstrap_ws_url(self, request: Any) -> str:
        headers = getattr(request, "headers", {}) or {}
        host = _safe_host_header(_case_insensitive_header(headers, "Host"))
        if not host:
            host = _host_for_url(self.config.host, self.config.port)
        proto = _case_insensitive_header(headers, "X-Forwarded-Proto")
        proto = proto.split(",", 1)[0].strip().lower()
        secure = proto in {"https", "wss"} or bool(self.config.ssl_certfile.strip())
        scheme = "wss" if secure else "ws"
        expected_path = _normalize_config_path(self.config.path)
        return f"{scheme}://{host}{expected_path}"

    # -- Session routes -----------------------------------------------------

    async def _dispatch_session_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/file-preview$", got)
        if m:
            return self._handle_file_preview(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/automations$", got)
        if m:
            return self._handle_session_automations(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        return None

    async def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        payload = await asyncio.to_thread(self._sessions_list_payload)
        return _http_json_response(payload)

    def _sessions_list_payload(self) -> dict[str, Any]:
        assert self.session_manager is not None
        sessions = list_webui_sessions(self.session_manager)
        from biscuitbot.session.webui_turns import websocket_turn_wall_started_at

        cleaned = []
        for s in sessions:
            key = s.get("key")
            if not (isinstance(key, str) and key.startswith("websocket:")):
                continue
            row = {k: v for k, v in s.items() if k != "path"}
            chat_id = key.split(":", 1)[1]
            started_at = websocket_turn_wall_started_at(chat_id)
            if started_at is not None:
                row["run_started_at"] = started_at
            scope = self.workspaces.scope_for_session_key(key)
            row["workspace_scope"] = scope.payload()
            employee = self.workspaces.employee_for_session_key(key)
            if employee:
                row["employee"] = employee
            cleaned.append(row)
        return {"sessions": cleaned}

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self.session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        self.media.augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        scope = self.workspaces.scope_for_session_key(decoded_key)
        session_messages: list[dict[str, Any]] | None = None
        if self.session_manager is not None:
            session_data = self.session_manager.read_session_file(decoded_key)
            raw_messages = session_data.get("messages") if isinstance(session_data, dict) else None
            if isinstance(raw_messages, list):
                session_messages = [m for m in raw_messages if isinstance(m, dict)]
        query = _parse_query(request.path)
        raw_limit = _query_first(query, "limit")
        limit: int | None = None
        if raw_limit is not None and raw_limit.strip():
            try:
                limit = int(raw_limit)
            except ValueError:
                return _http_error(400, "invalid limit")
        direction = _query_first(query, "direction")
        if direction is not None and direction not in {"latest"}:
            return _http_error(400, "invalid direction")
        before = _query_first(query, "before")
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self.media.augment_transcript_media,
            augment_assistant_media=self.media.augment_transcript_media,
            augment_assistant_text=lambda text: self.media.rewrite_local_markdown_images(
                text,
                workspace_path=scope.project_path,
            ),
            session_messages=session_messages,
            limit=limit,
            direction=direction,
            before=before,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        data["workspace_scope"] = scope.payload()
        return _http_json_response(data)

    def _handle_file_preview(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        path = _query_first(_parse_query(request.path), "path")
        try:
            payload = file_preview_payload(
                path,
                scope=self.workspaces.scope_for_session_key(decoded_key),
            )
        except WebUIFilePreviewError as e:
            return _http_error(e.status, e.message)
        return _http_json_response(payload)

    def _handle_session_automations(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        pending_job_ids: set[str] = set()
        if self.cron_pending_job_ids is not None:
            pending_job_ids = self.cron_pending_job_ids(decoded_key)
        return _http_json_response(
            session_automations_payload(
                self.cron_service,
                decoded_key,
                pending_job_ids=pending_job_ids,
            )
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not _is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        query = _parse_query(request.path)
        delete_automations = (_query_first(query, "delete_automations") or "").lower()
        automation_jobs = session_automation_jobs(self.cron_service, decoded_key)
        if automation_jobs and delete_automations not in {"1", "true", "yes"}:
            return _http_json_response(
                {
                    "deleted": False,
                    "blocked_by_automations": True,
                    "automations": serialize_automation_jobs(automation_jobs),
                }
            )
        if automation_jobs and self.cron_service is not None:
            for job in automation_jobs:
                self.cron_service.remove_job(job.id)
        deleted = self.session_manager.delete_session(decoded_key)
        delete_webui_thread(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    # -- Automation routes --------------------------------------------------

    async def _dispatch_automation_routes(
        self,
        request: WsRequest,
        got: str,
    ) -> Response | None:
        if got == "/api/webui/automations":
            return self._handle_webui_automations(request)
        m = re.match(r"^/api/webui/automations/(enable|disable|delete|run|update)$", got)
        if m:
            return await self._handle_webui_automation_action(request, m.group(1))
        return None

    def _pending_cron_job_ids_for_all(self) -> set[str]:
        if self.cron_service is None or self.cron_pending_job_ids is None:
            return set()
        pending: set[str] = set()
        for job in self.cron_service.list_jobs(include_disabled=True):
            session_key = job.payload.session_key
            if not session_key and job.payload.origin_channel and job.payload.origin_chat_id:
                session_key = f"{job.payload.origin_channel}:{job.payload.origin_chat_id}"
            if session_key:
                pending.update(self.cron_pending_job_ids(session_key))
        return pending

    def _handle_webui_automations(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            all_automations_payload(
                self.cron_service,
                session_manager=self.session_manager,
                pending_job_ids=self._pending_cron_job_ids_for_all(),
            )
        )

    async def _handle_webui_automation_action(
        self,
        request: WsRequest,
        action: str,
    ) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.cron_service is None:
            return _http_error(503, "cron service unavailable")

        query = _parse_query(request.path)
        job_id = (_query_first(query, "id") or _query_first(query, "job_id") or "").strip()
        if not job_id:
            return _http_error(400, "missing automation id")
        job = self.cron_service.get_job(job_id)
        if job is None:
            return _http_error(404, "automation not found")
        if job.payload.kind == "system_event":
            return _http_error(403, "system automation is protected")
        if action in {"enable", "run"} and not is_bound_cron_job(job):
            return _http_error(409, "automation has no linked chat")

        if action == "enable":
            if self.cron_service.enable_job(job_id, enabled=True) is None:
                return _http_error(404, "automation not found")
        elif action == "disable":
            if self.cron_service.enable_job(job_id, enabled=False) is None:
                return _http_error(404, "automation not found")
        elif action == "delete":
            result = self.cron_service.remove_job(job_id)
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        elif action == "run":
            if not job.enabled:
                return _http_error(409, "automation is disabled")
            task = asyncio.create_task(self.cron_service.run_job(job_id, force=False))
            task.add_done_callback(self._log_automation_run_result)
        elif action == "update":
            values = _automation_values_from_request(request)
            if values is None:
                return _http_error(400, "invalid automation update payload")
            parsed = _parse_automation_update(values, current_job=job)
            if isinstance(parsed, str):
                return _http_error(400, parsed)
            try:
                result = self.cron_service.update_job(job_id, **parsed)
            except ValueError as exc:
                return _http_error(400, str(exc))
            if result == "not_found":
                return _http_error(404, "automation not found")
            if result == "protected":
                return _http_error(403, "system automation is protected")
        else:
            return _http_error(404, "unknown automation action")

        return self._handle_webui_automations(request)

    @staticmethod
    def _log_automation_run_result(task: asyncio.Task[bool]) -> None:
        try:
            ran = task.result()
        except Exception:
            logger.exception("WebUI automation run-now task failed")
            return
        if not ran:
            logger.warning("WebUI automation run-now task did not execute")

    # -- Media routes -------------------------------------------------------

    def _dispatch_media_routes(self, request: WsRequest, got: str) -> Response | None:
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2), request)
        return None

    def _handle_media_fetch(
        self, sig: str, payload: str, request: WsRequest | None = None
    ) -> Response:
        return self.media.serve_signed_media(
            sig,
            payload,
            request=request,
        )

    # -- Misc routes --------------------------------------------------------

    async def _dispatch_misc_routes(
        self, connection: Any, request: WsRequest, got: str
    ) -> Response | None:
        if got == "/api/sessions":
            return await self._handle_sessions_list(request)
        if got == "/api/commands":
            return self._handle_commands(request)
        if got == "/api/workspaces":
            return self._handle_workspaces(connection, request)
        if got == "/api/webui/skills":
            return self._handle_webui_skills(request)
        m = re.match(r"^/api/webui/skills/([^/]+)/delete$", got)
        if m:
            return self._handle_webui_skill_delete(request, m.group(1))
        m = re.match(r"^/api/webui/skills/([^/]+)$", got)
        if m:
            return self._handle_webui_skill_detail(request, m.group(1))
        if got == "/api/webui/capabilities":
            return self._handle_webui_capabilities(request)
        m = re.match(r"^/api/webui/capabilities/([^/]+)$", got)
        if m:
            return self._handle_webui_capability_detail(request, m.group(1))
        m = re.match(r"^/api/avatars/([^/]+)$", got)
        if m:
            return self._handle_avatar(m.group(1))
        if got == "/api/webui/employees":
            return self._handle_webui_employees(request)
        if got == "/api/webui/employees/create":
            return self._handle_webui_employee_create(request)
        if got == "/api/webui/employees/update":
            return self._handle_webui_employee_update(request)
        m = re.match(r"^/api/webui/employees/([^/]+)/delete$", got)
        if m:
            return self._handle_webui_employee_delete(request, m.group(1))
        if got == "/api/webui/talent-market/catalog":
            return await self._handle_webui_talent_catalog(request)
        if got == "/api/webui/talent-market/install":
            return self._handle_webui_talent_install(request)
        if got == "/api/webui/sidebar-state":
            return self._handle_webui_sidebar_state(request)
        if got == "/api/webui/sidebar-state/update":
            return self._handle_webui_sidebar_state_update(request)
        if got == "/api/webui/setup/complete":
            return self._handle_webui_setup_complete(request)
        if got == "/api/webui/weixin/login-qr":
            return await self._handle_webui_weixin_login_qr(request)
        if got == "/api/webui/weixin/login-status":
            return await self._handle_webui_weixin_login_status(request)
        if got == "/api/desktop/restart":
            return self._handle_desktop_restart(request)
        if got == "/api/desktop/open-logs":
            return self._handle_desktop_open_logs(request)
        if got == "/api/desktop/export-diagnostics":
            return self._handle_desktop_export_diagnostics(request)
        return None

    def _handle_desktop_restart(self, request: WsRequest) -> Response:
        """打包桌面端：请求重启无头 gateway（引擎重启兜底通道）。

        桌面壳没有注入 ``window.biscuitbotHost``，前端无法直接调用宿主的
        ``restartEngine``，改为走后端 HTTP。响应返回后退出进程，Tauri 壳监听到
        子进程退出会重新拉起 sidecar 并导航回 WebUI（整页刷新）。
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._runtime_surface != "native":
            return _http_error(400, "engine restart is only available in the native desktop runtime")
        # 延迟退出，确保 JSON 响应先写回前端；os._exit 直接结束进程，跳过优雅清理。
        threading.Timer(0.2, os._exit, args=(0,)).start()
        return _http_json_response({"ok": True, "restarting": True})

    def _handle_desktop_open_logs(self, request: WsRequest) -> Response:
        """打包桌面端：在系统文件管理器中打开日志目录。

        桌面壳未注入 ``window.biscuitbotHost``，前端无法直接调用宿主的
        ``openLogs``，改为走后端 HTTP。桌面 sidecar 以 ``native`` runtime surface
        启动，仅在该环境下允许调用（与 ``_handle_desktop_restart`` 一致）。
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._runtime_surface != "native":
            return _http_error(400, "open logs is only available in the native desktop runtime")
        from biscuitbot.config.paths import get_logs_dir

        logs_dir = get_logs_dir()
        try:
            self._open_directory(logs_dir)
        except OSError as exc:
            return _http_error(500, f"failed to open logs directory: {exc}")
        return _http_json_response({"ok": True, "path": str(logs_dir)})

    def _handle_desktop_export_diagnostics(self, request: WsRequest) -> Response:
        """打包桌面端：生成诊断报告文件并返回其绝对路径。

        报告包含运行时元数据、最近日志尾部与少量非敏感配置摘要，便于用户
        粘贴给开发/排障。绝不写入任何密钥或令牌。
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._runtime_surface != "native":
            return _http_error(400, "export diagnostics is only available in the native desktop runtime")
        try:
            report = self._build_diagnostics_report()
        except Exception as exc:  # noqa: BLE001 - 报告生成失败不应向调用方暴露内部堆栈
            return _http_error(500, f"failed to build diagnostics report: {exc}")
        from biscuitbot.config.paths import get_data_dir
        from biscuitbot.utils.helpers import ensure_dir

        diagnostics_dir = ensure_dir(get_data_dir() / "diagnostics")
        path = diagnostics_dir / f"biscuitbot-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path.write_text(report, encoding="utf-8")
        return _http_json_response({"ok": True, "path": str(path)})

    def _build_diagnostics_report(self) -> str:
        """组装诊断报告文本（不含任何密钥/令牌）。"""
        from biscuitbot import __version__
        from biscuitbot.config.loader import load_config
        from biscuitbot.config.paths import (
            get_config_path,
            get_data_dir,
            get_logs_dir,
            get_workspace_path,
        )

        config = load_config()
        defaults = config.agents.defaults
        lines: list[str] = [
            "biscuitbot diagnostics",
            "=" * 48,
            "",
            f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"version: {__version__}",
            f"python: {sys.version.split()[0]} ({sys.platform} / {platform.platform()})",
            f"config_path: {get_config_path()}",
            f"data_dir: {get_data_dir()}",
            f"workspace: {get_workspace_path()}",
            f"logs_dir: {get_logs_dir()}",
            "",
            "config summary (non-sensitive):",
            f"  model_preset: {defaults.model_preset}",
            f"  model: {defaults.model}",
            f"  provider: {defaults.provider}",
            f"  timezone: {defaults.timezone}",
            f"  bot_name: {defaults.bot_name}",
            f"  workspace: {defaults.workspace}",
            "",
            "recent logs:",
        ]
        try:
            lines.extend(self._recent_log_tail(get_logs_dir()))
        except Exception as exc:  # noqa: BLE001 - 日志读取失败不应让整份报告失败
            lines.append(f"  (could not read logs: {exc})")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _recent_log_tail(logs_dir: Path, max_lines: int = 80) -> list[str]:
        """读取日志目录下最近修改的 .log 文件末尾若干行（异常安全）。"""
        latest = max(
            (p for p in logs_dir.glob("*.log") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            default=None,
        )
        if latest is None:
            return ["  (no .log files found)"]
        tail = latest.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
        return [f"  {latest.name}: {line}" for line in tail]

    @staticmethod
    def _open_directory(dir_path: Path) -> None:
        """在系统文件管理器中打开一个目录（跨平台）。

        Windows: ``explorer``；macOS: ``open``；其余: ``xdg-open``。
        """
        target = str(dir_path)
        if sys.platform == "win32":
            subprocess.run(["explorer", target], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", target], check=True)
        else:
            subprocess.run(["xdg-open", target], check=True)

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_workspaces(self, connection: Any, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            self.workspaces.payload(
                controls_available=self.workspace_controls_available(connection)
            )
        )

    def _handle_webui_skills(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(
            webui_skills_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
            )
        )

    def _handle_webui_skill_detail(self, request: WsRequest, raw_name: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        name = unquote(raw_name)
        if not name or "/" in name or "\\" in name:
            return _http_error(400, "invalid skill name")
        payload = webui_skill_detail_payload(
            self.skills_workspace_path,
            name,
            disabled_skills=self.disabled_skills,
        )
        if payload is None:
            return _http_error(404, "skill not found")
        return _http_json_response(payload)

    def _handle_webui_capabilities(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        kind = _query_first(query, "kind")
        return _http_json_response(
            capabilities_payload(
                self.skills_workspace_path,
                disabled_skills=self.disabled_skills,
                kind=kind,
            )
        )

    def _handle_webui_capability_detail(self, request: WsRequest, raw_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        capability_id = unquote(raw_id)
        payload = capability_detail_payload(
            self.skills_workspace_path,
            capability_id,
            disabled_skills=self.disabled_skills,
        )
        if payload is None:
            return _http_error(404, "capability not found")
        return _http_json_response(payload)

    def _handle_webui_skill_delete(self, request: WsRequest, raw_name: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        name = unquote(raw_name)
        try:
            result = delete_workspace_skill(
                self.skills_workspace_path,
                name,
                employee_store=self.employees,
            )
        except SkillDeletionError as e:
            return _http_error(e.status, e.message)
        except Exception:
            logger.exception("failed to delete skill '{}'", name)
            return _http_error(500, "failed to delete skill")
        return _http_json_response(result)

    def _handle_avatar(self, raw_name: str) -> Response:
        """返回员工头像图片字节（无鉴权，等同静态资源；``<img>`` 无法携带 Authorization）。

        文件名来自内置员工 ``avatar`` 字段（如 ``img_dccb1217c046.jpg``），
        经 ``_resolve_avatar_path`` 限定在头像目录内并防目录穿越。
        """
        name = unquote(raw_name)
        path = _resolve_avatar_path(name)
        if path is None:
            return _http_error(404, "Not Found")
        try:
            data = path.read_bytes()
        except OSError:
            return _http_error(404, "Not Found")
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return _http_response(
            data,
            status=200,
            content_type=content_type,
            extra_headers=[("Cache-Control", "public, max-age=86400")],
        )

    def _handle_webui_employees(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.employees is None:
            return _http_error(503, "employees store unavailable")
        try:
            return _http_json_response({"employees": self.employees.list_employees()})
        except Exception:
            logger.exception("failed to list employees")
            return _http_error(500, "failed to list employees")

    def _handle_webui_employee_create(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.employees is None:
            return _http_error(503, "employees store unavailable")
        values = _employee_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid employee values")
        try:
            return _http_json_response(self.employees.create_employee(values))
        except EmployeeValidationError as e:
            return _http_error(e.status, e.message)
        except Exception:
            logger.exception("failed to create employee")
            return _http_error(500, "failed to create employee")

    def _handle_webui_employee_update(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.employees is None:
            return _http_error(503, "employees store unavailable")
        query = _parse_query(request.path)
        employee_id = _query_first(query, "id")
        if not employee_id:
            return _http_error(400, "missing employee id")
        values = _employee_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid employee values")
        try:
            return _http_json_response(self.employees.update_employee(employee_id, values))
        except EmployeeValidationError as e:
            return _http_error(e.status, e.message)
        except Exception:
            logger.exception("failed to update employee '{}'", employee_id)
            return _http_error(500, "failed to update employee")

    def _handle_webui_employee_delete(self, request: WsRequest, raw_id: str) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.employees is None:
            return _http_error(503, "employees store unavailable")
        employee_id = unquote(raw_id)
        try:
            return _http_json_response(self.employees.delete_employee(employee_id))
        except EmployeeValidationError as e:
            return _http_error(e.status, e.message)
        except Exception:
            logger.exception("failed to delete employee '{}'", employee_id)
            return _http_error(500, "failed to delete employee")

    async def _handle_webui_talent_catalog(self, request: WsRequest) -> Response:
        """人才市场目录：拉取配置文件里写死的注册表 URL 列表，返回规范化 + installed 标注。

        注册表 URL 只能由后台 CLI 设置（``biscuitbot talent-market add/set <url>``），
        客户端传入的 ``?url=`` 一律忽略，打包应用也因此无法更改注册表。
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        refresh = (_query_first(query, "refresh") or "").lower() in {"1", "true", "yes"}

        configured_urls = read_talent_market_registry_urls()
        if not configured_urls:
            # 未配置 → 返回 configured:false，前端展示 CLI 引导空态（HTTP 200）
            return _http_json_response(
                {
                    "configured": False,
                    "source_url": "",
                    "catalog_updated_at": None,
                    "employees": [],
                    "installed_count": 0,
                    "sources": [],
                }
            )
        try:
            payload = await asyncio.to_thread(
                talent_catalog_payload,
                configured_urls,
                self.employees,
                force_refresh=refresh,
            )
        except TalentMarketError as e:
            return _http_error(e.status, e.message)
        except Exception:
            logger.exception("failed to load talent catalog")
            return _http_error(500, "failed to load talent catalog")
        payload["configured"] = True
        return _http_json_response(payload)

    def _handle_webui_talent_install(self, request: WsRequest) -> Response:
        """人才市场安装：把注册表条目落库为数字员工（重复 id 幂等）。"""
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self.employees is None:
            return _http_error(503, "employees store unavailable")
        values = _employee_values_from_request(request)
        if values is None:
            return _http_error(400, "invalid employee values")
        query = _parse_query(request.path)
        source_url = _query_first(query, "source_url")
        try:
            return _http_json_response(
                install_talent_employee(values, self.employees, source_url=source_url)
            )
        except EmployeeValidationError as e:
            return _http_error(e.status, e.message)
        except TalentMarketError as e:
            return _http_error(e.status, e.message)
        except Exception:
            logger.exception("failed to install talent employee")
            return _http_error(500, "failed to install talent employee")

    def _handle_webui_setup_complete(self, request: WsRequest) -> Response:
        """首次引导「欢迎设置」保存：provider + api_key(+ base) + 可选 model。

        This is the setup flow used by the desktop first-run welcome page and is
        also available to the browser WebUI settings. It reuses
        ``settings_api.update_provider_settings`` so the persisted result is
        identical to editing the settings page, then persists an optional model
        override on ``config.agents.defaults.model``.
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        from biscuitbot.config.loader import load_config, save_config
        from biscuitbot.webui.settings_api import (
            WebUISettingsError,
            update_provider_settings,
        )

        query = _parse_query(request.path)
        provider = _query_first(query, "provider")
        api_key = _query_first(query, "api_key") or _query_first(query, "apiKey")
        if not provider:
            return _http_error(400, "provider is required")
        if not (api_key or "").strip():
            return _http_error(400, "api_key is required")
        try:
            update_provider_settings(query)
            model = (_query_first(query, "model") or "").strip()
            if model:
                config = load_config()
                config.agents.defaults.model = model
                save_config(config)
        except WebUISettingsError as e:
            return _http_error(400, str(e))
        except Exception:
            logger.exception("setup/complete failed")
            return _http_error(500, "failed to save settings")
        return _http_json_response({"ok": True, "needs_setup": False})

    async def _handle_webui_weixin_login_qr(self, request: WsRequest) -> Response:
        """微信扫码登录：获取一张登录二维码。

        返回 ``{"qrcode_id", "qr_content"}``，前端据此渲染二维码并开始轮询
        :meth:`_handle_webui_weixin_login_status`。
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            result = await self._weixin_login.fetch_qr()
        except Exception:
            logger.exception("failed to fetch weixin login qr")
            return _http_error(502, "failed to fetch weixin login qr")
        return _http_json_response(result)

    async def _handle_webui_weixin_login_status(self, request: WsRequest) -> Response:
        """微信扫码登录：单次轮询登录状态。

        ``?qrcode=<id>`` 对应 :meth:`_handle_webui_weixin_login_qr` 返回的
        ``qrcode_id``。状态机同原生 ``weixin`` 渠道：``wait`` / ``scaned_but_redirect``
        / ``confirmed`` / ``expired``。确认后 token 已由渠道写入
        ``~/.biscuitbot/weixin/account.json``，此处额外把 ``channels.weixin.enabled``
        置真并保存配置，网关重启后微信渠道即自动生效。
        """
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        qrcode_id = _query_first(query, "qrcode")
        if not qrcode_id:
            return _http_error(400, "qrcode is required")
        try:
            result = await self._weixin_login.poll(qrcode_id)
        except Exception:
            logger.exception("failed to poll weixin login status")
            return _http_error(502, "failed to poll weixin login status")

        if result.get("confirmed"):
            from biscuitbot.config.loader import load_config, save_config

            try:
                config = load_config()
                wx = getattr(config.channels, "weixin", None)
                if wx is None:
                    setattr(config.channels, "weixin", {"enabled": True})
                elif isinstance(wx, dict):
                    wx["enabled"] = True
                else:
                    wx.enabled = True
                save_config(config)
            except Exception:
                logger.exception("failed to enable weixin channel")
                result["enabled"] = False
            else:
                result["enabled"] = True
                await self._weixin_login.close()

        return _http_json_response(result)

    def _handle_webui_sidebar_state(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(read_webui_sidebar_state())

    def _handle_webui_sidebar_state_update(self, request: WsRequest) -> Response:
        if not self.check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        raw_state = _query_first(query, "state")
        if raw_state is None:
            return _http_error(400, "missing state")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError:
            return _http_error(400, "state must be JSON")
        if not isinstance(decoded, dict):
            return _http_error(400, "state must be an object")
        try:
            state = write_webui_sidebar_state(decoded)
        except ValueError as e:
            return _http_error(400, str(e))
        except OSError:
            self._log.exception("failed to write webui sidebar state")
            return _http_error(500, "failed to write sidebar state")
        return _http_json_response(state)

    # -- Static file serving ------------------------------------------------

    def _serve_static(self, request_path: str) -> Response | None:
        assert self.static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self.static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self.static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            index = self.static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self._log.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )


def _automation_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    raw = _case_insensitive_header(request.headers, _AUTOMATION_VALUES_HEADER)
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return values if isinstance(values, dict) else None


def _employee_values_from_request(request: WsRequest) -> dict[str, Any] | None:
    """解析 ``X-Biscuitbot-Employee-Values`` 头中的员工字段（前端 encodeURIComponent 编码）。"""
    raw = _case_insensitive_header(request.headers, _EMPLOYEE_VALUES_HEADER)
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except Exception:
        try:
            values = json.loads(unquote(raw))
        except Exception:
            return None
    return values if isinstance(values, dict) else None


def _parse_automation_update(
    values: dict[str, Any],
    *,
    current_job: CronJob | None = None,
) -> dict[str, Any] | str:
    update: dict[str, Any] = {}
    if "name" in values:
        raw_name = values.get("name")
        if not isinstance(raw_name, str):
            return "name must be a string"
        name = raw_name.strip()
        if not name:
            return "name cannot be empty"
        update["name"] = name
    if "message" in values:
        raw_message = values.get("message")
        if not isinstance(raw_message, str):
            return "message must be a string"
        message = raw_message.strip()
        if not message:
            return "message cannot be empty"
        update["message"] = message
    if "schedule" in values:
        raw_schedule = values.get("schedule")
        if not isinstance(raw_schedule, dict):
            return "schedule must be an object"
        parsed_schedule = _parse_automation_schedule(raw_schedule)
        if isinstance(parsed_schedule, str):
            return parsed_schedule
        if current_job is not None and _schedule_matches_job(parsed_schedule, current_job):
            return update
        schedule_error = _validate_automation_schedule(parsed_schedule)
        if schedule_error:
            return schedule_error
        update["schedule"] = parsed_schedule
        update["delete_after_run"] = parsed_schedule.kind == "at"
    return update


def _parse_automation_schedule(values: dict[str, Any]) -> CronSchedule | str:
    raw_kind = values.get("kind")
    if not isinstance(raw_kind, str):
        return "schedule kind must be a string"
    kind = raw_kind.strip()
    if kind == "every":
        every_ms = _positive_int(values.get("every_ms"))
        if every_ms is None:
            return "every schedule requires positive every_ms"
        return CronSchedule(kind="every", every_ms=every_ms)
    if kind == "cron":
        raw_expr = values.get("expr")
        if not isinstance(raw_expr, str):
            return "cron schedule requires expr"
        expr = raw_expr.strip()
        if not expr:
            return "cron schedule requires expr"
        raw_tz = values.get("tz")
        if raw_tz is not None and not isinstance(raw_tz, str):
            return "cron schedule timezone must be a string"
        tz = raw_tz.strip() if isinstance(raw_tz, str) else ""
        return CronSchedule(kind="cron", expr=expr, tz=tz or None)
    if kind == "at":
        at_ms = _positive_int(values.get("at_ms"))
        if at_ms is None:
            return "one-time schedule requires positive at_ms"
        return CronSchedule(kind="at", at_ms=at_ms)
    return "unknown schedule kind"


def _schedule_matches_job(schedule: CronSchedule, job: CronJob) -> bool:
    current = job.schedule
    if schedule.kind != current.kind:
        return False
    if schedule.kind == "at":
        return schedule.at_ms == current.at_ms
    if schedule.kind == "every":
        return schedule.every_ms == current.every_ms
    if schedule.kind == "cron":
        return (schedule.expr or "") == (current.expr or "") and (
            schedule.tz or None
        ) == (current.tz or None)
    return False


def _validate_automation_schedule(schedule: CronSchedule) -> str | None:
    if schedule.kind == "at":
        if not schedule.at_ms or schedule.at_ms <= int(time.time() * 1000):
            return "one-time schedule must be in the future"
        return None
    if schedule.kind != "cron":
        return None

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from croniter import croniter

        tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
        base = datetime.now(tz=tz)
        croniter(schedule.expr, base).get_next(datetime)
    except Exception:
        return "cron schedule is invalid"
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _is_websocket_channel_session_key(key: str) -> bool:
    return key.startswith("websocket:")
