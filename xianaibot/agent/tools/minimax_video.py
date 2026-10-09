"""MiniMax H3 视频生成工具（自包含：客户端 + Tool）。

本模块把 MiniMax 开放平台（``api.minimaxi.com``，中国大陆；海外
``api.minimax.io``）的 MiniMax H3 多模态视频生成能力封装为
``generate_video_minimax`` 工具（厂商自包含架构，方向 B）：
``MiniMaxVideoClient`` 负责「认证 + 请求构造 + 任务创建/轮询/取下载地址」，
``MiniMaxVideoTool`` 负责参数 schema、启用门控与视频落盘。启用门控：
「模型厂商」页配置了 minimax 厂商密钥（或 ``tools.minimax_video.apiKey``
显式配置）即启用。

与其他视频工具的关系
===========================
- 本模块**不 import** ``seedance_video.py``（避免循环依赖）；厂商注册表复用
  ``kling_video.py`` 的全局唯一实现（``register_video_gen_provider``）。
- 落盘、图片/视频/音频引用解析等厂商无关辅助复用 ``_video_common.py``。

MiniMax H3 API 要点
===================
- Base URL：``https://api.minimaxi.com``（中国大陆；海外 ``api.minimax.io``）。
  视频端点路径自带 ``/v1`` / ``/v2`` 前缀，故本模块只保存**裸域**——
  :func:`_normalize_api_base` 会把用户在「模型厂商」页填的聊天式 base
  （如 ``https://api.minimaxi.com/v1``）剥回裸域，这样「MiniMax 同时当 LLM 用」
  时填同一个 apiBase 也能跑视频。
- 认证：``Authorization: Bearer <api_key>``（与 MiniMax 聊天 API 同一个 Key，
  **不需要**可灵那样的 JWT 签名）。
- 创建任务：``POST /v2/video_generation``，请求体为 ``content[]`` 多模态数组
  （``text`` / ``image_url`` / ``video_url`` / ``audio_url``，媒体项各带 ``role``：
  ``first_frame`` / ``last_frame`` / ``reference_image`` / ``reference_video`` /
  ``reference_audio``），外加 ``model`` / ``resolution`` / ``duration`` /
  ``ratio``（模式相关）/ ``aigc_watermark``。
- 模型名必须精确为 ``MiniMax-H3``（大小写敏感，**勿小写**）。
- ``resolution`` 取值 ``768P`` / ``2K``（大写），且**各模式都必须下发**
  （与 Seedance 的 r2v「勿传 resolution」规则相反）；``duration`` 为整数 4–15。
- ``ratio`` 是**模式相关**的：文生视频（t2va）必填且不接受 ``adaptive``；
  图生视频（i2va）由首帧推导、传了也会被忽略；参考生视频（r2va）可选。
- 业务错误不一定体现在 HTTP 状态码上：HTTP 200 也可能携带
  ``base_resp.status_code != 0``（如 2013 参数非法），必须单独检查。
- 成功响应返回的是 ``file_id`` 而非视频 URL，需再调
  ``GET /v1/files/retrieve`` 换取 ``download_url``。
- 上限：首帧 ≤1、尾帧 ≤1、参考图 ≤9、参考视频 ≤3、参考音频 ≤3；素材总数 ≤12、
  请求体 ≤64MB；首帧/尾帧与 ``reference_*`` **互斥**；参考音频**不能单独使用**。
"""

from __future__ import annotations

import asyncio  # 轮询间隔
import json  # 请求体序列化与体积核算 / 工具返回结果
from pathlib import Path  # 工作区路径处理
from typing import Any  # 任意类型

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志
from pydantic import Field  # Pydantic 字段校验

from xianaibot.agent.tools._video_common import (  # 厂商无关共享辅助
    VideoToolError,
    VideoVendorSpec,
    download_and_store,
    resolve_audio_ref,
    resolve_image_ref,
    resolve_video_ref,
)
from xianaibot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from xianaibot.agent.tools.kling_video import (
    register_video_gen_provider,  # 全局唯一的视频厂商注册表（勿另建）
)
from xianaibot.agent.tools.schema import (  # schema 构造器
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from xianaibot.config_base import Base  # 配置基类

# MiniMax 官方 API 默认 base URL（中国大陆；海外为 https://api.minimax.io）。
# 只保存裸域：所有端点路径自带 /v1、/v2 前缀。
_DEFAULT_BASE_URL = "https://api.minimaxi.com"
# 默认模型：MiniMax H3（模型名大小写敏感，勿小写）
_MINIMAX_DEFAULT_MODEL = "MiniMax-H3"
# v2 视频端点（/v2/video_generation）当前可选模型：H3 与 H3-Max（后者 480P/768P、
# 5–15 秒，不支持 2K）。仅作「视频模型」下拉的建议项，不参与校验——真正的取值
# 由配置/工具入参决定（_resolve_model 对非 Seedance/可灵残留名回退 H3）。
_MINIMAX_KNOWN_MODELS = ("MiniMax-H3", "MiniMax-H3-Max")
# H3 支持的画幅比例（adaptive 仅 r2va 可用，t2va 会被拒绝）
_MINIMAX_RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
# H3 支持的清晰度（大写；仅这两档）
_MINIMAX_RESOLUTIONS = ("768P", "2K")
_MINIMAX_RESOLUTION_DEFAULT = "768P"
# 把工具/WebUI 沿用的 Seedance 清晰度词映射到 H3 的两档（就近取档）
_RESOLUTION_ALIASES = {
    "480p": "768P",
    "720p": "768P",
    "768p": "768P",
    "1080p": "2K",
    "2k": "2K",
    "4k": "2K",
}
# 时长边界（H3 为 4–15 秒，工具/WebUI 允许到 30）
_MINIMAX_DURATION_MIN = 4
_MINIMAX_DURATION_MAX = 15
_MINIMAX_DURATION_DEFAULT = 5
# 素材上限（H3 官方限制）
_MAX_FRAME_IMAGES = 2  # 首帧 + 尾帧
_MAX_REFERENCE_IMAGES = 9
_MAX_REFERENCE_VIDEOS = 3
_MAX_REFERENCE_AUDIOS = 3
_MAX_FILES = 12  # 素材文件总数
_MAX_BODY_BYTES = 64 * 1024 * 1024  # 请求体上限 64MB
# 创建任务端点（模型名放在请求体，不内嵌 URL）
_CREATE_PATH = "/v2/video_generation"
# 轮询端点候选：官方文档对路径有两种说法（/v1/query?task_id= 与 /v2/query/{id}），
# 运行期按顺序探测，命中后固定使用（见 poll）。
_POLL_PATHS = (
    "/v1/query/video_generation?task_id={task_id}",
    "/v2/query/video_generation/{task_id}",
)
# 成功 / 失败状态词表：两套文档说法不同（Preparing/Processing/Success/Failed 与
# queued/running/succeeded/...），统一小写后匹配；不在此列的未知状态继续轮询。
_SUCCESS_STATUSES = frozenset({"success", "succeeded", "completed", "finished"})
_FAILED_STATUSES = frozenset({"failed", "fail", "failure", "cancelled", "canceled"})
_PENDING_STATUSES = frozenset(
    {
        "preparing",
        "processing",
        "queued",
        "queueing",
        "pending",
        "running",
        "waiting",
        "submitted",
        "in_progress",
        "created",
    }
)
# 取下载地址的 key 优先级。刻意**不含**裸 ``url``：任务响应常回显输入素材，
# 裸 url 可能命中参考图/参考视频的地址，导致下载到错误的文件。
_URL_KEYS = ("download_url", "video_url", "file_url")


class MiniMaxVideoError(VideoToolError):
    """MiniMax H3 视频生成/编辑失败时抛出。"""


def _normalize_api_base(raw: str | None) -> str:
    """把用户配置的 apiBase 归一化为裸域，空值回退官方默认。

    剥掉结尾的 ``/v1`` 与 ``/anthropic``，使聊天式 base
    （``https://api.minimaxi.com/v1``）与 Anthropic 式 base
    （``https://api.minimax.io/anthropic/v1``）都收敛到裸域——本模块的端点路径
    自带 ``/v1``、``/v2`` 前缀。用户填自定义网关（含路径前缀）时只剥这两个后缀，
    其余路径原样保留。
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return _DEFAULT_BASE_URL
    while True:
        stripped = base
        for suffix in ("/anthropic", "/v1"):
            if stripped.lower().endswith(suffix):
                stripped = stripped[: -len(suffix)].rstrip("/")
        if stripped == base or not stripped:
            return base
        base = stripped


def _normalize_resolution(resolution: str | None) -> str:
    """把清晰度归一到 H3 的 ``768P`` / ``2K``。

    工具/WebUI 的清晰度词表沿用 Seedance（480p/720p/1080p/4K），模型很可能照原样
    传进来，故按就近取档映射并记日志；真正不认识的取值才报错。
    """
    raw = (resolution or "").strip()
    if not raw:
        return _MINIMAX_RESOLUTION_DEFAULT
    if raw in _MINIMAX_RESOLUTIONS:
        return raw
    alias = _RESOLUTION_ALIASES.get(raw.lower())
    if alias:
        logger.info("MiniMax H3 清晰度 {} 映射为 {}", raw, alias)
        return alias
    raise MiniMaxVideoError(
        f"MiniMax H3 不支持的清晰度：{resolution}。"
        f"支持 {' / '.join(_MINIMAX_RESOLUTIONS)}（480p/720p≈768P，1080p/4K≈2K）。"
    )


def _deep_find_str(data: Any, keys: tuple[str, ...], *, depth: int = 3) -> str | None:
    """在嵌套 dict 中按 key 优先级查找首个非空字符串（每层先按 keys 顺序找，再递归）。

    H3 的响应外层结构在文档间不一致（``file_id`` 可能出现在顶层、``data`` 下、
    或 ``data.file`` 下），故用逐层下钻代替写死路径。
    """
    if depth < 0 or not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in data.values():
        if isinstance(value, dict):
            found = _deep_find_str(value, keys, depth=depth - 1)
            if found:
                return found
    return None


class MiniMaxVideoClient:
    """MiniMax H3 视频生成客户端：Bearer 认证 + 请求构造 + 任务创建/轮询/取下载地址。

    不依赖任何 SDK，直接用 ``httpx``。与可灵客户端不同，H3 用的是与 MiniMax 聊天
    接口相同的静态 API Key（无 JWT）。
    """

    provider_name = "minimax"

    def __init__(
        self,
        *,
        api_key: str | None,
        api_base: str | None = None,
        poll_interval_sec: float = 10.0,
        max_poll_attempts: int = 180,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_base = _normalize_api_base(api_base)
        self.poll_interval_sec = poll_interval_sec
        self.max_poll_attempts = max_poll_attempts
        self.timeout = timeout

    def _default_base_url(self) -> str:
        """MiniMax 官方 API 默认 base URL（裸域）。

        必须是普通实例方法（而非 staticmethod）：settings_api 的
        ``_image_default_base_url`` 以 ``cls._default_base_url(cls)`` 调用，
        写成 staticmethod 会因多余入参而 TypeError。
        """
        return _DEFAULT_BASE_URL

    def _authorization(self) -> str:
        """构造 Authorization 头（H3 为静态 Bearer，无需签名）。"""
        if not self.api_key:
            raise MiniMaxVideoError(
                "MiniMax API key 未配置：请在「模型厂商」页配置 minimax 厂商的 "
                "API Key，或在 config.json 设置 tools.minimax_video.apiKey。"
            )
        return f"Bearer {self.api_key}"

    def build_request(
        self,
        *,
        prompt: str,
        image_urls: list[str] | None = None,
        reference_images: list[str] | None = None,
        video_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
        watermark: bool = False,
        model: str = _MINIMAX_DEFAULT_MODEL,
    ) -> tuple[str, dict[str, Any]]:
        """构造 H3 请求体，返回 ``(endpoint_path, body)``。

        模式判定：
        - 有任意参考素材（``reference_images`` / ``video_urls`` / ``audio_urls``）
          → **r2va**（多模态参考生视频）；
        - 否则有 ``image_urls`` → **i2va**（首帧/尾帧）；
        - 否则 → **t2va**（文生视频）。

        首帧/尾帧与参考素材互斥（H3 硬约束），冲突时直接抛错而不是静默丢一半。
        """
        frames = [v for v in (image_urls or []) if v]
        ref_images = [v for v in (reference_images or []) if v]
        ref_videos = [v for v in (video_urls or []) if v]
        ref_audios = [v for v in (audio_urls or []) if v]

        if frames and (ref_images or ref_videos or ref_audios):
            raise MiniMaxVideoError(
                "首帧/尾帧与参考素材互斥：image_urls 不能与 reference_images / "
                "video_urls / audio_urls 同时传入（H3 限制），请二选一。"
            )
        if ref_audios and not (ref_images or ref_videos):
            raise MiniMaxVideoError(
                "参考音频不能单独使用：audio_urls 需与 reference_images 或 "
                "video_urls 搭配（H3 限制）。"
            )
        if len(frames) > _MAX_FRAME_IMAGES:
            raise MiniMaxVideoError(
                f"首帧/尾帧最多 {_MAX_FRAME_IMAGES} 张（第 1 张首帧、第 2 张尾帧），"
                f"当前传入 {len(frames)} 张。"
            )
        if len(ref_images) > _MAX_REFERENCE_IMAGES:
            raise MiniMaxVideoError(
                f"参考图最多 {_MAX_REFERENCE_IMAGES} 张，当前传入 {len(ref_images)} 张。"
            )
        if len(ref_videos) > _MAX_REFERENCE_VIDEOS:
            raise MiniMaxVideoError(
                f"参考视频最多 {_MAX_REFERENCE_VIDEOS} 段，当前传入 {len(ref_videos)} 段。"
            )
        if len(ref_audios) > _MAX_REFERENCE_AUDIOS:
            raise MiniMaxVideoError(
                f"参考音频最多 {_MAX_REFERENCE_AUDIOS} 段，当前传入 {len(ref_audios)} 段。"
            )
        total_files = len(frames) + len(ref_images) + len(ref_videos) + len(ref_audios)
        if total_files > _MAX_FILES:
            raise MiniMaxVideoError(
                f"参考素材文件总数最多 {_MAX_FILES} 个，当前传入 {total_files} 个。"
            )

        is_r2va = bool(ref_images or ref_videos or ref_audios)
        mode = "r2va" if is_r2va else ("i2va" if frames else "t2va")

        # 多模态输入项。R2：嵌套 {"url": ...} 形状未经真实调用证实——若官方要求
        # 扁平 {"<kind>": url}，只需改这一处。
        def _media_item(kind: str, url: str, role: str) -> dict[str, Any]:
            return {"type": kind, kind: {"url": url}, "role": role}

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if mode == "i2va":
            # 单图作首帧；两图时第 2 张作尾帧（与可灵分支的 index 约定一致）。
            roles = (
                ("first_frame", "last_frame")
                if len(frames) > 1
                else ("first_frame",)
            )
            for role, url in zip(roles, frames, strict=False):
                content.append(_media_item("image_url", url, role))
        for url in ref_images:
            content.append(_media_item("image_url", url, "reference_image"))
        for url in ref_videos:
            content.append(_media_item("video_url", url, "reference_video"))
        for url in ref_audios:
            content.append(_media_item("audio_url", url, "reference_audio"))

        resolved_resolution = _normalize_resolution(resolution)

        raw_duration = _MINIMAX_DURATION_DEFAULT if duration is None else int(duration)
        resolved_duration = min(
            max(raw_duration, _MINIMAX_DURATION_MIN), _MINIMAX_DURATION_MAX
        )
        if duration is not None and resolved_duration != raw_duration:
            logger.info(
                "MiniMax H3 时长截断：{} → {} 秒（支持 {}-{}）",
                raw_duration,
                resolved_duration,
                _MINIMAX_DURATION_MIN,
                _MINIMAX_DURATION_MAX,
            )

        body: dict[str, Any] = {
            "model": model,
            "content": content,
            # H3 各模式都要求 resolution（与 Seedance 的 r2v 规则相反），故总是下发。
            "resolution": resolved_resolution,
            "duration": resolved_duration,
            "aigc_watermark": bool(watermark),
        }
        resolved_ratio = self._resolve_ratio(mode, ratio)
        if resolved_ratio:
            body["ratio"] = resolved_ratio
        self._check_body_size(body)
        return _CREATE_PATH, body

    @staticmethod
    def _resolve_ratio(mode: str, ratio: str | None) -> str | None:
        """按模式决定是否下发 ratio（H3 的 ratio 是模式相关的）。"""
        value = (ratio or "").strip()
        if mode == "t2va":
            # 文生视频必填且不接受 adaptive；缺失或 adaptive 时回落到 16:9。
            if value and value != "adaptive" and value in _MINIMAX_RATIOS:
                return value
            if value:
                logger.info(
                    "MiniMax H3 文生视频不接受 ratio={}（必填且不可为 adaptive），改用 16:9",
                    value,
                )
            return "16:9"
        if mode == "i2va":
            # 图生视频由首帧推导画幅，传了也会被忽略。
            if value:
                logger.info("MiniMax H3 图生视频由首帧推导画幅，忽略 ratio={}", value)
            return None
        # r2va：可选，仅发送合法取值。
        if value and value in _MINIMAX_RATIOS:
            return value
        return None

    @staticmethod
    def _check_body_size(body: dict[str, Any]) -> None:
        """请求体体积预检（在发 HTTP 之前，避免白跑一趟）。

        读取模块级 ``_MAX_BODY_BYTES``（而非类属性），便于测试用 monkeypatch 收紧。
        """
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if size <= _MAX_BODY_BYTES:
            return
        raise MiniMaxVideoError(
            f"请求体过大（{size / 1024 / 1024:.1f} MB），超过 MiniMax H3 的 "
            f"{_MAX_BODY_BYTES // 1024 // 1024} MB 上限。本地文件转 base64 后体积约增大 "
            f"1/3，原始文件合计需小于约 {_MAX_BODY_BYTES * 3 // 4 // 1024 // 1024} MB；"
            "建议改用公网可访问的 URL，或压缩素材后重试。"
        )

    @staticmethod
    def _payload(data: Any) -> dict[str, Any]:
        """取响应里的 ``data`` 子对象（创建/轮询/取文件三处共用）；非 dict 返回空 dict。

        文档对响应外层结构说法不一（``task_id`` / ``status`` 有时在顶层、有时在
        ``data`` 下），故统一「顶层优先，再下钻 data」。
        """
        if not isinstance(data, dict):
            return {}
        payload = data.get("data")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _base_resp_error(data: Any) -> str:
        """提取 ``base_resp`` 里的业务错误；无错误返回空串。

        MiniMax 的 HTTP 200 也可能携带错误体（如 ``status_code`` 2013 参数非法），
        因此不能只看 HTTP 状态码。
        """
        if not isinstance(data, dict):
            return ""
        base_resp = data.get("base_resp")
        if not isinstance(base_resp, dict):
            return ""
        code = base_resp.get("status_code")
        if code in (None, 0, "0"):
            return ""
        return f"[{code}] {base_resp.get('status_msg') or '未知错误'}"

    async def create_task(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> str:
        """创建 MiniMax 视频任务并返回 task_id。"""
        url = f"{self.api_base}{endpoint}"
        headers = {
            "Authorization": self._authorization(),
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise MiniMaxVideoError(
                f"创建 MiniMax 任务请求失败：{exc}。请检查网络或 baseUrl 配置"
            ) from exc
        data = response.json() if response.content else {}
        error = self._base_resp_error(data)
        if response.status_code >= 400 or error:
            msg = error or f"HTTP {response.status_code}"
            raise MiniMaxVideoError(f"创建 MiniMax 任务失败：{msg}（{data}）")
        payload = self._payload(data)
        task_id = data.get("task_id") or payload.get("task_id")
        if not task_id:
            raise MiniMaxVideoError(f"创建 MiniMax 任务未返回 task_id：{data}")
        return str(task_id)

    @staticmethod
    def _extract_status(data: dict[str, Any]) -> str:
        """从轮询响应中取任务状态（小写）。"""
        payload = MiniMaxVideoClient._payload(data)
        for source in (data, payload):
            for key in ("status", "task_status"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().lower()
        return ""

    @staticmethod
    def _extract_status_message(data: dict[str, Any]) -> str:
        """从轮询响应中取失败原因。"""
        payload = MiniMaxVideoClient._payload(data)
        for source in (payload, data):
            for key in ("status_msg", "task_status_msg", "message", "error"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return "任务失败"

    async def poll(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        *,
        poll_path: str | None = None,
    ) -> dict[str, Any]:
        """轮询任务直至成功/失败，返回完整的成功响应 JSON。

        官方文档对轮询端点有两种说法，故首轮按 :data:`_POLL_PATHS` 顺序探测，
        命中后固定使用（并在 INFO 日志里写明，便于一次真实调用即可定案）。
        task_id 直接拼进 URL 字符串而非用 ``params=``，以便测试的手写 HTTP 替身
        （``async def get(url, headers=None)``）无需改动即可复用。
        """
        candidates = (poll_path,) if poll_path else _POLL_PATHS
        headers = {"Authorization": self._authorization()}
        chosen: str | None = None
        warned_unknown = False
        for attempt in range(self.max_poll_attempts):
            await asyncio.sleep(self.poll_interval_sec)
            data: dict[str, Any] | None = None
            probe = (chosen,) if chosen else candidates
            for index, template in enumerate(probe):
                url = f"{self.api_base}{template.format(task_id=task_id)}"
                try:
                    response = await client.get(url, headers=headers)
                except httpx.RequestError as exc:
                    logger.warning(
                        "MiniMax 轮询请求失败（第 {} 次）：{}", attempt + 1, exc
                    )
                    break
                if (
                    response.status_code == 404
                    and chosen is None
                    and index < len(probe) - 1
                ):
                    logger.info(
                        "MiniMax 轮询路径 {} 未命中（404），改试下一个候选", template
                    )
                    continue
                chosen = template
                logger.info("MiniMax 轮询路径已确定：{}", template)
                data = response.json() if response.content else {}
                break
            if data is None:
                # 候选都不通（或请求异常）：固定用第一个候选继续轮询，避免每轮重复探测。
                if chosen is None:
                    chosen = candidates[0]
                continue

            error = self._base_resp_error(data)
            if error:
                # 轮询期的 base_resp 非 0 视为瞬时错误，继续轮询；真正的失败态由
                # 下面的状态词表判定。
                logger.warning(
                    "MiniMax 轮询返回 base_resp 错误（第 {} 次）：{}", attempt + 1, error
                )
                continue
            status = self._extract_status(data)
            if not status:
                logger.warning(
                    "MiniMax 轮询响应缺少状态字段（第 {} 次）：{}", attempt + 1, data
                )
                continue
            if status in _SUCCESS_STATUSES:
                return data
            if status in _FAILED_STATUSES:
                raise MiniMaxVideoError(
                    f"MiniMax 任务失败：{self._extract_status_message(data)}（{data}）"
                )
            if status not in _PENDING_STATUSES and not warned_unknown:
                # 未知状态一律继续轮询（宁可超时也不误判为终点），只提醒一次。
                warned_unknown = True
                logger.warning(
                    "MiniMax 返回未知任务状态 {!r}，继续轮询。原始响应：{}", status, data
                )
        raise MiniMaxVideoError(
            f"MiniMax 任务超时（超过 {self.max_poll_attempts} 次轮询）。"
            "2K / 15 秒任务耗时较长，可调大 tools.seedance_video.maxPollAttempts。"
        )

    @staticmethod
    def extract_direct_url(data: dict[str, Any]) -> str | None:
        """若响应已直接给出视频 URL 则返回它（部分网关形态），否则 None。

        正常流程下 H3 只返回 ``file_id``，需再走 :meth:`retrieve_file_url` 换取。
        """
        url = _deep_find_str(data, _URL_KEYS)
        if url and url.lower().startswith(("http://", "https://")):
            return url
        return None

    @staticmethod
    def extract_file_id(data: dict[str, Any]) -> str:
        """从成功响应中提取 ``file_id``（响应外层结构在文档间不一致，故逐层下钻）。"""
        file_id = _deep_find_str(data, ("file_id",))
        if not file_id:
            raise MiniMaxVideoError(
                f"MiniMax 任务成功但未返回 file_id，无法换取下载地址：{data}"
            )
        return file_id

    async def retrieve_file_url(self, client: httpx.AsyncClient, file_id: str) -> str:
        """把 ``file_id`` 换成可下载的视频 URL（``GET /v1/files/retrieve``）。"""
        url = f"{self.api_base}/v1/files/retrieve?file_id={file_id}"
        headers = {"Authorization": self._authorization()}
        try:
            response = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            raise MiniMaxVideoError(f"获取 MiniMax 文件下载地址失败：{exc}") from exc
        data = response.json() if response.content else {}
        error = self._base_resp_error(data)
        if response.status_code >= 400 or error:
            raise MiniMaxVideoError(
                "获取 MiniMax 文件下载地址失败："
                f"{error or f'HTTP {response.status_code}'}（{data}）"
            )
        download_url = _deep_find_str(data, _URL_KEYS)
        if not download_url:
            raise MiniMaxVideoError(f"MiniMax 文件接口未返回 download_url：{data}")
        return download_url


register_video_gen_provider(MiniMaxVideoClient)


# ---------------------------------------------------------------------------
# Tool —— generate_video_minimax（厂商自包含，与 client 同文件）
# ---------------------------------------------------------------------------


class MiniMaxVideoToolConfig(Base):
    """MiniMax H3 视频生成工具配置。

    职责：承载 MiniMax H3 视频生成工具的运行时配置项。厂商自包含重构后无
    ``enabled`` / ``provider`` 字段——启用与否由「模型厂商」页是否配置
    minimax 密钥决定（或本段 ``api_key`` 显式覆盖）。base URL 刻意不设字段：
    留空交给 ``MiniMaxVideoClient`` 归一化（兼容聊天式 base）。
    """

    api_key: str | None = None  # 显式 API Key；缺省回退「模型厂商」页 minimax 厂商密钥
    model: str = _MINIMAX_DEFAULT_MODEL  # 默认模型（H3，大小写敏感）
    default_ratio: str = "16:9"  # 默认画幅（t2va 必填；i2va 由首帧推导）
    default_duration: int = Field(default=_MINIMAX_DURATION_DEFAULT, ge=_MINIMAX_DURATION_MIN, le=_MINIMAX_DURATION_MAX)  # 默认时长（秒）
    default_resolution: str = _MINIMAX_RESOLUTION_DEFAULT  # 默认清晰度（H3 各模式都必须下发）
    watermark: bool = False  # 是否默认添加 AIGC 水印（映射 aigc_watermark）
    save_dir: str = "generated_video"  # artifact 保存子目录名
    poll_interval_sec: float = Field(default=10.0, ge=1.0, le=60.0)  # 轮询间隔（秒）
    max_poll_attempts: int = Field(default=180, ge=1, le=600)  # 最大轮询次数（约 30 分钟）
    timeout_sec: float = Field(default=120.0, ge=10.0, le=600.0)  # 单次 HTTP 超时（秒）


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "视频生成/编辑的文本提示词（必填）。描述主体、运镜、景别、构图、光影、氛围与节奏越具体越好。",
            min_length=1,
        ),
        image_urls=ArraySchema(
            StringSchema(
                "首帧/尾帧图片：本地文件路径（含微信/渠道收到的图片路径）、"
                "可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description=(
                "可选首帧/尾帧（图生视频 i2va）：传 1 张作首帧、2 张依次作首帧与尾帧，"
                "最多 2 张；与 reference_images（参考图）互斥。"
            ),
        ),
        reference_images=ArraySchema(
            StringSchema(
                "参考图片：本地文件路径、可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description=(
                "可选「参考图」（主体的外观/风格参考，不是首尾帧，最多 9 张）；"
                "与 image_urls（首帧/尾帧）互斥；此时 video_urls 最多 3 段、"
                "audio_urls 最多 3 段，素材文件合计最多 12 个。"
            ),
        ),
        video_urls=ArraySchema(
            StringSchema("参考视频的可公开访问 HTTP(S) URL。"),
            description="可选参考视频（r2va），最多 3 段。仅支持公网 HTTP(S) URL，不支持本地文件。",
        ),
        audio_urls=ArraySchema(
            StringSchema(
                "参考音频：本地文件路径、可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description=(
                "可选参考音频（口型/音色参考等），最多 3 段，且不能单独使用——"
                "必须同时传 reference_images 或 video_urls。"
            ),
        ),
        ratio=StringSchema(
            "画幅比例。文生视频（t2va）必填且不接受 adaptive（缺省用 16:9）；"
            "图生视频由首帧推导、传了也会被忽略；参考生视频可选。",
            enum=_MINIMAX_RATIOS,
        ),
        duration=IntegerSchema(
            description="视频时长（秒）4–15，超出会被截断到区间内。",
            minimum=_MINIMAX_DURATION_MIN,
            maximum=_MINIMAX_DURATION_MAX,
        ),
        resolution=StringSchema(
            "清晰度（MiniMax H3 各模式都需要，取值 768P / 2K；480p、720p 映射为 768P，"
            "1080p、4K 映射为 2K）。",
            enum=_MINIMAX_RESOLUTIONS,
        ),
        watermark=BooleanSchema(
            description="是否添加 AIGC 水印（默认关闭）。",
        ),
        model=StringSchema(
            "可选模型覆盖（默认 MiniMax-H3，大小写敏感，必须精确匹配）。",
        ),
        required=["prompt"],
    )
)
class MiniMaxVideoTool(Tool):
    """通过 MiniMax H3 生成/编辑视频，并持久化为本地文件。

    职责：把 MiniMax H3 的多模态视频生成能力封装为 agent 可调用的工具。
    模式由输入自动判定（见 ``MiniMaxVideoClient.build_request``）：有参考素材 →
    r2va；有 image_urls → i2va（首帧/尾帧）；否则 t2va。不支持的能力（随机
    种子、音画同步开关）不在参数 schema 中，传入会被 ``**kwargs`` 静默吸收。
    """

    _capability = (
        "Generate or edit videos from text/image/video/audio with MiniMax H3 "
        "(first/last frame + style references, returns file path)."
    )
    _usage_md = "docs/generate_video_minimax.md"  # 工具使用说明文档路径

    config_key = "minimax_video"  # 配置键名

    # 厂商卡片元数据（WebUI「视频生成」页与 /v1/models 由它派生；见 _video_common）
    vendor_spec = VideoVendorSpec(
        key="minimax",
        display_name="MiniMax",
        provider="minimax",
        tool_name="generate_video_minimax",
        config_key="minimax_video",
        support=("t2v", "i2v", "v2v", "ref_image", "ref_audio"),
        ratio_options=_MINIMAX_RATIOS,
        resolution_options=_MINIMAX_RESOLUTIONS,
        duration_range=(_MINIMAX_DURATION_MIN, _MINIMAX_DURATION_MAX),
        model_suggestions=_MINIMAX_KNOWN_MODELS,
        # H3 各模式都必须下发清晰度，故不能留空（不提供「模型自动决定」）。
        resolution_optional=False,
    )

    @classmethod
    def config_cls(cls):
        """返回该工具使用的配置类。"""
        return MiniMaxVideoToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """「模型厂商」页配置了 minimax 密钥（或显式 apiKey）即启用。"""
        config = ctx.config.minimax_video
        if (config.api_key or "").strip():
            return True
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("minimax")
        return bool((getattr(provider_cfg, "api_key", None) or "").strip())

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例（密钥/地址优先取「模型厂商」页 minimax 厂商配置）。"""
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("minimax")
        return cls(
            workspace=ctx.workspace,
            config=ctx.config.minimax_video,
            provider_api_key=getattr(provider_cfg, "api_key", None),
            provider_api_base=getattr(provider_cfg, "api_base", None),
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: MiniMaxVideoToolConfig,
        provider_api_key: str | None = None,
        provider_api_base: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~
        self.config = config  # 工具配置
        self._provider_api_key = provider_api_key  # 模型厂商页配置的 minimax 密钥
        self.provider_api_base = provider_api_base  # 模型厂商页配置的 minimax apiBase

    @property
    def name(self) -> str:
        """工具名称。"""
        return "generate_video_minimax"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Generate or edit a video with MiniMax H3 (multimodal: text + first/last "
            "frame + style/subject reference images + reference videos + reference "
            "audios). Mode is auto-detected from inputs: reference materials -> r2va; "
            "image_urls -> i2va (first/last frame, max 2); otherwise t2va. "
            "image_urls (first/last frame) and reference_images (style references) "
            "are mutually exclusive; reference audios cannot be used alone. "
            "Resolution (768P/2K) is required in all modes; ratio is required for "
            "t2va (no adaptive) and derived from first frame in i2va. "
            "Runs asynchronously and returns the downloaded video file path. "
            "Other video vendors: use generate_video_seedance or generate_video_kling "
            "instead."
        )

    # ---- 内部实现 ----------------------------------------------------------

    def _api_key(self) -> str:
        """解析 MiniMax 密钥：模型厂商页 minimax 密钥优先，其次配置显式值。

        优先级理由同可灵：工具级 ``config.api_key`` 可能残留其他厂商的值。
        MiniMax 视频与聊天共用同一个静态 API Key（无需 JWT 签名）。
        """
        key = (self._provider_api_key or "").strip() or (self.config.api_key or "").strip()
        if not key:
            raise MiniMaxVideoError(
                "MiniMax API key 未配置：请在「模型厂商」页配置 minimax 厂商的 "
                "API Key，或在 config.json 设置 tools.minimax_video.apiKey。"
            )
        return key

    def _api_base(self) -> str:
        """返回「模型厂商」页 minimax 厂商的 apiBase，留空返回空串。

        刻意**不在此回退默认常量**：留空即交给 ``MiniMaxVideoClient`` 归一化，这样
        「MiniMax 同时当 LLM 用」时填的聊天式 base（带 ``/v1``）与 Anthropic 式 base
        （带 ``/anthropic``）都只有一处收敛逻辑。
        """
        return (self.provider_api_base or "").rstrip("/")

    def _resolve_model(self, model: str | None) -> str:
        """解析 MiniMax 模型名：残留其他厂商模型名（旧配置切厂商未切模型）回退 H3 默认。

        模型名大小写敏感，必须精确为 ``MiniMax-H3``；显式自定义 ID 原样保留。
        """
        candidate = (model or "").strip() or (self.config.model or "").strip()
        lowered = candidate.lower()
        if not candidate or "seedance" in lowered or "kling" in lowered:
            return _MINIMAX_DEFAULT_MODEL
        return candidate

    async def execute(
        self,
        prompt: str,
        image_urls: list[str] | None = None,
        reference_images: list[str] | None = None,
        video_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
        watermark: bool | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """执行 MiniMax H3 视频生成/编辑。

        参数:
            prompt: 文本提示词（必填）。
            image_urls: 可选首帧/尾帧列表（1 张首帧，2 张首帧+尾帧；与 reference_images 互斥）。
            reference_images: 可选参考图列表（风格/主体参考，≤9；与 image_urls 互斥）。
            video_urls: 可选参考视频列表（仅公网 URL，≤3）。
            audio_urls: 可选参考音频列表（≤3，须搭配 reference_images 或 video_urls）。
            ratio: 画幅比例（t2va 必填且不接受 adaptive）。
            duration: 时长（秒）4–15。
            resolution: 清晰度（768P / 2K，各模式必须下发）。
            watermark: 是否加 AIGC 水印。
            model: 模型覆盖（默认 MiniMax-H3）。

        返回:
            包含视频本地路径与元数据的 JSON 字符串；出错时返回错误说明。
        """
        try:
            resolved_model = self._resolve_model(model)

            minimax_images = [resolve_image_ref(v) for v in image_urls or []]
            minimax_references = [resolve_image_ref(v) for v in reference_images or []]
            # 参考视频：仅公网 URL（与方舟、可灵一致，不额外放开本地文件）。
            minimax_videos = [resolve_video_ref(v) for v in video_urls or []]
            minimax_audios = [resolve_audio_ref(v) for v in audio_urls or []]

            client = MiniMaxVideoClient(
                api_key=self._api_key(),
                api_base=self._api_base(),
                poll_interval_sec=self.config.poll_interval_sec,
                max_poll_attempts=self.config.max_poll_attempts,
                timeout=self.config.timeout_sec,
            )
            endpoint, body = client.build_request(
                prompt=prompt,
                image_urls=minimax_images,
                reference_images=minimax_references,
                video_urls=minimax_videos,
                audio_urls=minimax_audios,
                ratio=ratio,
                duration=duration if duration is not None else self.config.default_duration,
                resolution=resolution or self.config.default_resolution,
                watermark=watermark if watermark is not None else self.config.watermark,
                model=resolved_model,
            )

            async with httpx.AsyncClient(timeout=self.config.timeout_sec) as http:
                task_id = await client.create_task(http, endpoint, body)
                logger.info(
                    "MiniMax H3 任务已创建：{}（model={}，endpoint={}）",
                    task_id,
                    resolved_model,
                    endpoint,
                )
                data = await client.poll(http, task_id)
                # 正常流程只回 file_id，需换一次下载地址；部分网关形态会直接给 URL。
                video_url = client.extract_direct_url(data)
                if not video_url:
                    video_url = await client.retrieve_file_url(
                        http, client.extract_file_id(data)
                    )
                artifact = await download_and_store(
                    http,
                    video_url,
                    workspace=self.workspace,
                    save_dir=self.config.save_dir,
                    default_model=self.config.model,
                    model=resolved_model,
                )

            return json.dumps(
                {
                    "video": artifact,
                    "task_id": task_id,
                    "model": resolved_model,
                    "next_step": (
                        "视频已生成并保存到本地。可把 path 作为后续剪辑工具的输入，"
                        "或通过 message 工具把视频文件交付给用户。"
                    ),
                },
                ensure_ascii=False,
            )
        except VideoToolError as exc:
            return f"Error: {exc}"
