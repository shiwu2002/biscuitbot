"""通义万相（阿里灵积 DashScope）视频生成工具（自包含：客户端 + Tool）。

本模块把阿里云百炼 / 灵积（DashScope，``dashscope.aliyuncs.com``）的通义万相
视频生成能力封装为 ``generate_video_dashscope`` 工具（厂商自包含架构）：
``DashScopeVideoClient`` 负责「认证 + 请求构造 + 任务提交/轮询/取下载地址」，
``DashScopeVideoTool`` 负责参数 schema、启用门控与视频落盘。启用门控：
「模型厂商」页配置了 dashscope 厂商密钥（或 ``tools.dashscope_video.apiKey``
显式配置）即启用。

与其他视频工具的关系
===========================
- 本模块**不 import** ``seedance_video.py`` / ``kling_video.py`` /
  ``minimax_video.py``（避免循环依赖）；厂商注册表复用 ``kling_video.py`` 的
  全局唯一实现（``register_video_gen_provider``）。
- 落盘、图片/视频引用解析等厂商无关辅助复用 ``_video_common.py``。
- 卡片元数据用 ``_video_common.VideoVendorSpec`` 声明在工具类上（``vendor_spec``），
  WebUI「视频生成」页与 ``/v1/models`` 均由它派生，无需在别处登记厂商。

DashScope 视频 API 要点
===================
- Base URL：``https://dashscope.aliyuncs.com``。视频走**原生路径**
  （``/api/v1/...``），与「模型厂商」页 dashscope 的 LLM 兼容模式端点
  （``/compatible-mode/v1``）不同，故 :func:`_normalize_api_base` 会把用户填的
  兼容模式 base 剥回裸域——这样「DashScope 同时当 LLM 用」时填同一个 apiBase
  也能跑视频。
- 认证：``Authorization: Bearer <api_key>``。
- 异步任务式：提交必须带 ``X-DashScope-Async: enable``（**不支持同步**），
  ``POST /api/v1/services/aigc/video-generation/video-synthesis``，请求体
  ``{"model", "input": {"prompt", "img_url"?}, "parameters": {...}}``，
  返回 ``output.task_id``；随后轮询 ``GET /api/v1/tasks/{task_id}``，直到
  ``output.task_status`` 为 ``SUCCEEDED``（取 ``output.video_url``）或
  ``FAILED`` / ``CANCELED``。
- 图生视频：``input.img_url`` 只收**单张**图片，接受公网 HTTP(S) URL 或
  ``data:image/png;base64,...`` 内联数据 URL（故 ``resolve_image_ref`` 的产物可直接用）。
- 画幅与清晰度：DashScope 用 ``parameters.size``（``"宽*高"`` 像素对），
  本模块按「清晰度 + 比例」映射为像素对（见 :func:`_resolve_size`）；映射表刻意只
  收录官方明确文档化的组合，未知组合回退到该清晰度的 16:9，不臆造尺寸。
- 时长因模型而异：``wan2.6-*`` 为 2–15 秒；``wan2.5-*`` 只支持 5 / 10 秒；
  ``wan2.2-*`` 固定 5 秒（见 :func:`_resolve_duration`）。
"""

from __future__ import annotations

import asyncio  # 轮询间隔
import json  # 请求体序列化 / 工具返回结果
import os  # 环境变量（DASHSCOPE_API_KEY 兜底）
from pathlib import Path  # 工作区路径处理
from typing import Any  # 任意类型

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志
from pydantic import Field  # Pydantic 字段校验

from xianaibot.agent.tools._video_common import (  # 厂商无关共享辅助
    VideoToolError,
    VideoVendorSpec,
    download_and_store,
    resolve_image_ref,
)
from xianaibot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from xianaibot.agent.tools.kling_video import (
    register_video_gen_provider,  # 全局唯一的视频厂商注册表（勿另建）
)
from xianaibot.agent.tools.schema import (  # schema 构造器
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from xianaibot.config_base import Base  # 配置基类

# DashScope 官方默认域名（裸域：端点路径自带 /api/v1 前缀）
_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
# 默认模型：通义万相 2.6 文生视频（原生音频、支持多镜头）
_DEFAULT_MODEL = "wan2.6-t2v"
# 「视频模型」下拉的建议清单（**仅建议，不参与校验**，实际取值由配置/入参决定）。
# 全部为官方在售、且与本模块请求形状一致（``input.img_url`` + ``parameters.size``）
# 的型号。刻意**不收录**：
# - ``wan2.x-r2v``：参考生视频，本模块未实现多参考素材（只收单张首帧）；
# - ``wan2.7-*``：改用 ``input.media`` + ``parameters.resolution``，请求形状不同；
# - ``wan2.6-t2v-flash``：不存在（实测 ``Model not exist``）。
_KNOWN_MODEL_SUGGESTIONS = (
    _DEFAULT_MODEL,
    "wan2.6-i2v",
    "wan2.6-i2v-flash",
    "wan2.5-t2v-preview",
    "wan2.5-i2v-preview",
    "wan2.2-t2v-plus",
    "wan2.2-i2v-plus",
    "wan2.2-i2v-flash",
)
# 提交端点（异步任务；模型名放在请求体）
_SUBMIT_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
# 任务结果轮询端点前缀（拼 task_id）
_TASK_PATH = "/api/v1/tasks"
# 轮询间隔 / 上限（视频生成耗时较长，默认约 30 分钟）
_POLL_INTERVAL_S = 10.0
_MAX_POLL_ATTEMPTS = 180

# 画幅比例（本模块只声明官方有明确像素映射的组合，见 _VIDEO_SIZES）
_RATIOS = ("16:9", "9:16", "1:1")
# 清晰度档位（大写；工具/WebUI 沿用同一词表）
_RESOLUTIONS = ("480P", "720P", "1080P")
_RESOLUTION_DEFAULT = "720P"
# 清晰度别名 → 档位（工具/WebUI 可能照其他厂商词表传 480p/1080p/4K）
_RESOLUTION_ALIASES = {
    "480p": "480P",
    "720p": "720P",
    "1080p": "1080P",
    "4k": "1080P",
}
# 「清晰度 + 比例」→ parameters.size 像素对。刻意只收录官方文档明确给出的组合，
# 未知组合回退到该清晰度的 16:9（见 _resolve_size），不臆造像素尺寸。
_VIDEO_SIZES: dict[str, dict[str, str]] = {
    "480P": {"16:9": "832*480", "9:16": "480*832", "1:1": "624*624"},
    "720P": {"16:9": "1280*720", "9:16": "720*1280", "1:1": "960*960"},
    "1080P": {"16:9": "1920*1080", "9:16": "1080*1920", "1:1": "1440*1440"},
}
# 时长边界（wan2.6 系列；更早的模型见 _resolve_duration 的收敛规则）
_DURATION_MIN = 2
_DURATION_MAX = 15
_DURATION_DEFAULT = 5
# wan2.5-* 只支持 5 / 10 秒
_DURATION_CHOICES_WAN25 = (5, 10)
# 图生视频只收单张首帧
_MAX_FRAME_IMAGES = 1
# 轮询状态词表
_SUCCESS_STATUSES = frozenset({"succeeded", "success", "completed", "finished"})
_FAILED_STATUSES = frozenset({"failed", "canceled", "cancelled", "unknown"})
_PENDING_STATUSES = frozenset({"pending", "running", "queued", "submitted", "in_progress"})


class DashScopeVideoError(VideoToolError):
    """通义万相视频生成失败时抛出。"""


def _normalize_api_base(raw: str | None) -> str:
    """把用户配置的 apiBase 归一化为裸域，空值回退官方默认。

    剥掉 DashScope 已知路径后缀（提交端点、任务端点、LLM 兼容模式
    ``/compatible-mode/v1``、``/api/v1``），使聊天式 base 与本模块的原生
    ``/api/v1/...`` 路径都收敛到裸域。用户填自定义网关（含其他路径前缀）时
    只剥这些后缀，其余路径原样保留。
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return _DEFAULT_BASE_URL
    for suffix in (
        _SUBMIT_PATH,
        _TASK_PATH,
        "/compatible-mode/v1",
        "/api/v1",
    ):
        if base.lower().endswith(suffix.lower()):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base or _DEFAULT_BASE_URL


def _normalize_resolution(resolution: str | None) -> str:
    """把清晰度归一到 :data:`_RESOLUTIONS` 之一，空值回退默认档。

    工具/WebUI 的清晰度词表沿用了其他厂商（480p/720p/1080p/4K），模型很可能
    照原样传进来，故按别名映射并记日志；真正不认识的取值才报错。
    """
    raw = (resolution or "").strip()
    if not raw:
        return _RESOLUTION_DEFAULT
    if raw in _RESOLUTIONS:
        return raw
    alias = _RESOLUTION_ALIASES.get(raw.lower())
    if alias:
        logger.info("通义万相清晰度 {} 映射为 {}", raw, alias)
        return alias
    raise DashScopeVideoError(
        f"通义万相不支持的清晰度：{resolution}。支持 {' / '.join(_RESOLUTIONS)}"
        "（1080p/4K 归入 1080P）。"
    )


def _normalize_ratio(ratio: str | None) -> str:
    """把画幅比例归一到 :data:`_RATIOS` 之一，空值/adaptive 回退 16:9。"""
    value = (ratio or "").strip()
    if value in _RATIOS:
        return value
    if value:
        logger.info("通义万相不接受 ratio={}，改用 16:9", value)
    return "16:9"


def _resolve_size(resolution: str, ratio: str) -> str:
    """按「清晰度 + 比例」查像素对；该组合未收录时回退该清晰度的 16:9。"""
    bucket = _VIDEO_SIZES.get(resolution) or _VIDEO_SIZES[_RESOLUTION_DEFAULT]
    size = bucket.get(ratio)
    if size:
        return size
    logger.info("通义万相清晰度 {} 未收录比例 {}，改用 16:9", resolution, ratio)
    return bucket["16:9"]


def _resolve_duration(model: str, duration: int | None) -> int:
    """按时长收敛到模型实际支持的取值（不同万相模型规则不同）。

    - ``wan2.2-*``：固定 5 秒；
    - ``wan2.5-*``：只支持 5 / 10 秒（就近取档）；
    - 其余（``wan2.6-*`` 及以后）：收敛到 2–15 秒闭区间。
    """
    raw = _DURATION_DEFAULT if duration is None else int(duration)
    lowered = model.lower()
    if lowered.startswith("wan2.2"):
        if raw != _DURATION_DEFAULT:
            logger.info("通义万相 {} 固定 5 秒，忽略 duration={}", model, raw)
        return _DURATION_DEFAULT
    if lowered.startswith("wan2.5"):
        snapped = min(_DURATION_CHOICES_WAN25, key=lambda c: abs(c - raw))
        if snapped != raw:
            logger.info("通义万相 {} 只支持 5/10 秒，duration {} → {}", model, raw, snapped)
        return snapped
    clamped = min(max(raw, _DURATION_MIN), _DURATION_MAX)
    if clamped != raw:
        logger.info(
            "通义万相时长收敛：{} → {} 秒（支持 {}-{}）",
            raw, clamped, _DURATION_MIN, _DURATION_MAX,
        )
    return clamped


class DashScopeVideoClient:
    """通义万相视频生成客户端：Bearer 认证 + 请求构造 + 任务提交/轮询/取下载地址。

    不依赖任何 SDK，直接用 ``httpx``。与可灵/ MiniMax 一样是异步任务式，
    但请求体是 DashScope 原生的 ``{"model", "input", "parameters"}`` 三段结构。
    """

    provider_name = "dashscope"

    def __init__(
        self,
        *,
        api_key: str | None,
        api_base: str | None = None,
        poll_interval_sec: float = _POLL_INTERVAL_S,
        max_poll_attempts: int = _MAX_POLL_ATTEMPTS,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.api_base = _normalize_api_base(api_base)
        self.poll_interval_sec = poll_interval_sec
        self.max_poll_attempts = max_poll_attempts
        self.timeout = timeout

    def _default_base_url(self) -> str:
        """DashScope 官方 API 默认 base URL（裸域）。

        必须是普通实例方法（而非 staticmethod）：settings_api 的
        ``_image_default_base_url`` 以 ``cls._default_base_url(cls)`` 调用，
        写成 staticmethod 会因多余入参而 TypeError。
        """
        return _DEFAULT_BASE_URL

    def _authorization(self) -> str:
        """构造 Authorization 头（DashScope 为静态 Bearer）。"""
        if not self.api_key:
            raise DashScopeVideoError(
                "DashScope API key 未配置：请在「模型厂商」页配置 dashscope 厂商的 "
                "API Key，或在 config.json 设置 tools.dashscope_video.apiKey。"
            )
        return f"Bearer {self.api_key}"

    def build_request(
        self,
        *,
        prompt: str,
        image_urls: list[str] | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
        seed: int | None = None,
        watermark: bool = False,
        prompt_extend: bool = True,
        model: str = _DEFAULT_MODEL,
    ) -> tuple[str, dict[str, Any]]:
        """构造通义万相请求体，返回 ``(endpoint_path, body)``。

        模式判定：有 ``image_urls`` → 图生视频（取第 1 张作首帧 ``img_url``）；
        否则 → 文生视频。DashScope 图生视频只收单张首帧，多于 1 张直接抛错
        而不是静默丢弃。
        """
        frames = [v for v in (image_urls or []) if v]
        if len(frames) > _MAX_FRAME_IMAGES:
            raise DashScopeVideoError(
                f"通义万相图生视频只支持单张首帧（最多 {_MAX_FRAME_IMAGES} 张），"
                f"当前传入 {len(frames)} 张；多镜头请分次生成。"
            )

        resolved_ratio = _normalize_ratio(ratio)
        resolved_resolution = _normalize_resolution(resolution)
        resolved_duration = _resolve_duration(model, duration)

        input_payload: dict[str, Any] = {"prompt": prompt}
        if frames:
            input_payload["img_url"] = frames[0]

        parameters: dict[str, Any] = {
            "size": _resolve_size(resolved_resolution, resolved_ratio),
            "duration": resolved_duration,
            "prompt_extend": bool(prompt_extend),
            "watermark": bool(watermark),
        }
        if seed is not None:
            parameters["seed"] = int(seed)

        body: dict[str, Any] = {
            "model": model,
            "input": input_payload,
            "parameters": parameters,
        }
        return _SUBMIT_PATH, body

    @staticmethod
    def _output(data: Any) -> dict[str, Any]:
        """取响应里的 ``output`` 子对象；非 dict 返回空 dict。"""
        if not isinstance(data, dict):
            return {}
        output = data.get("output")
        return output if isinstance(output, dict) else {}

    @staticmethod
    def _error_message(data: Any) -> str:
        """提取 DashScope 的错误信息（HTTP 层 ``code``/``message`` 或 ``output`` 内）。"""
        if not isinstance(data, dict):
            return ""
        output = DashScopeVideoClient._output(data)
        for source in (output, data):
            message = source.get("message")
            if isinstance(message, str) and message.strip():
                code = source.get("code") or ""
                return f"[{code}] {message.strip()}".strip("[] ")
        return ""

    async def create_task(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> str:
        """提交通义万相视频任务并返回 task_id（必须带 X-DashScope-Async 头）。"""
        url = f"{self.api_base}{endpoint}"
        headers = {
            "Authorization": self._authorization(),
            "Content-Type": "application/json",
            # 视频合成为异步接口，缺少该头会直接被拒。
            "X-DashScope-Async": "enable",
        }
        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise DashScopeVideoError(
                f"提交通义万相任务请求失败：{exc}。请检查网络或 baseUrl 配置"
            ) from exc
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            detail = self._error_message(data) or f"HTTP {response.status_code}"
            raise DashScopeVideoError(f"提交通义万相任务失败：{detail}（{data}）")
        task_id = self._output(data).get("task_id") or data.get("task_id")
        if not task_id:
            raise DashScopeVideoError(
                f"提交通义万相任务未返回 task_id：{data}"
            )
        return str(task_id)

    async def poll(self, client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
        """轮询任务直至成功，返回完整的成功响应 JSON；失败/超时抛错。

        task_id 直接拼进 URL 字符串而非用 ``params=``，便于测试的手写 HTTP 替身
        （``async def get(url, headers=None)``）无需改动即可复用。
        """
        url = f"{self.api_base}{_TASK_PATH}/{task_id}"
        headers = {"Authorization": self._authorization()}
        warned_unknown = False
        for attempt in range(self.max_poll_attempts):
            await asyncio.sleep(self.poll_interval_sec)
            try:
                response = await client.get(url, headers=headers)
            except httpx.RequestError as exc:
                # 轮询网络错误不致命，继续重试。
                logger.warning("通义万相轮询请求失败（第 {} 次）：{}", attempt + 1, exc)
                continue
            data = response.json() if response.content else {}
            if response.status_code >= 400:
                logger.warning(
                    "通义万相轮询返回 HTTP {}（第 {} 次）：{}",
                    response.status_code, attempt + 1, data,
                )
                continue
            output = self._output(data)
            status = str(output.get("task_status") or "").strip().lower()
            if not status:
                logger.warning("通义万相轮询响应缺少 task_status（第 {} 次）：{}", attempt + 1, data)
                continue
            if status in _SUCCESS_STATUSES:
                return data
            if status in _FAILED_STATUSES:
                raise DashScopeVideoError(
                    f"通义万相任务失败：{self._error_message(data) or status}（{data}）"
                )
            if status not in _PENDING_STATUSES and not warned_unknown:
                # 未知状态一律继续轮询（宁可超时也不误判为终点），只提醒一次。
                warned_unknown = True
                logger.warning(
                    "通义万相返回未知任务状态 {!r}，继续轮询。原始响应：{}", status, data
                )
        raise DashScopeVideoError(
            f"通义万相任务超时（超过 {self.max_poll_attempts} 次轮询）。"
            "长时长任务耗时较长，可调大 tools.dashscope_video.maxPollAttempts。"
        )

    @staticmethod
    def extract_video_url(data: dict[str, Any]) -> str:
        """从成功响应中提取视频下载地址（``output.video_url`` 优先）。"""
        output = DashScopeVideoClient._output(data)
        for key in ("video_url", "url"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        results = output.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    value = item.get("url") or item.get("video_url")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        raise DashScopeVideoError(
            f"通义万相任务成功但未返回 video_url，无法下载：{data}"
        )


register_video_gen_provider(DashScopeVideoClient)


# ---------------------------------------------------------------------------
# Tool —— generate_video_dashscope（厂商自包含，与 client 同文件）
# ---------------------------------------------------------------------------


class DashScopeVideoToolConfig(Base):
    """通义万相视频生成工具配置。

    职责：承载通义万相视频生成工具的运行时配置项。无 ``enabled`` / ``provider``
    字段——启用与否由「模型厂商」页是否配置 dashscope 密钥决定（或本段 ``api_key``
    显式覆盖）。base URL 刻意不设字段：留空交给 ``DashScopeVideoClient`` 归一化
    （兼容 LLM 兼容模式 base）。
    """

    api_key: str | None = None  # 显式 API Key；缺省回退「模型厂商」页 dashscope 密钥
    model: str = _DEFAULT_MODEL  # 默认模型（通义万相 2.6 文生视频）
    default_ratio: str = "16:9"  # 默认画幅比例
    default_duration: int = Field(
        default=_DURATION_DEFAULT, ge=_DURATION_MIN, le=_DURATION_MAX
    )  # 默认时长（秒，wan2.6 支持 2–15）
    default_resolution: str = _RESOLUTION_DEFAULT  # 默认清晰度（480P/720P/1080P）
    seed: int | None = None  # 随机种子（None = 随机）
    watermark: bool = False  # 是否添加 AIGC 水印（映射 parameters.watermark）
    prompt_extend: bool = True  # 是否让通义万相自动扩写提示词（映射 parameters.prompt_extend）
    save_dir: str = "generated_video"  # artifact 保存子目录名
    poll_interval_sec: float = Field(default=_POLL_INTERVAL_S, ge=1.0, le=60.0)  # 轮询间隔（秒）
    max_poll_attempts: int = Field(default=_MAX_POLL_ATTEMPTS, ge=1, le=600)  # 最大轮询次数（约 30 分钟）
    timeout_sec: float = Field(default=120.0, ge=10.0, le=600.0)  # 单次 HTTP 超时（秒）


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "视频生成的文本提示词（必填）。描述主体、动作、运镜、景别、构图、光影与"
            "氛围越具体越好。",
            min_length=1,
        ),
        image_urls=ArraySchema(
            StringSchema(
                "首帧图片：本地文件路径（含微信/渠道收到的图片路径）、"
                "可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description=(
                "可选首帧（图生视频）：通义万相只支持**单张**首帧，传多张会报错；"
                "多镜头请分次生成。"
            ),
        ),
        ratio=StringSchema(
            "画幅比例，取值 16:9 / 9:16 / 1:1，缺省 16:9。",
            enum=list(_RATIOS),
        ),
        duration=IntegerSchema(
            description=(
                "视频时长（秒）：wan2.6 系列支持 2–15；wan2.5 系列只支持 5/10；"
                "wan2.2 系列固定 5 秒（超出会按模型规则收敛）。"
            ),
            minimum=_DURATION_MIN,
            maximum=_DURATION_MAX,
        ),
        resolution=StringSchema(
            "清晰度：480P / 720P / 1080P，缺省 720P（1080p、4K 归入 1080P）。",
            enum=list(_RESOLUTIONS),
        ),
        seed=IntegerSchema(
            description="可选随机种子；不传则随机。",
            minimum=0,
        ),
        model=StringSchema(
            "可选模型覆盖（默认 wan2.6-t2v）。常用：wan2.6-t2v、wan2.6-i2v、"
            "wan2.5-t2v-preview、wan2.5-i2v-preview、wan2.2-t2v-plus、wan2.2-i2v-plus。",
        ),
        required=["prompt"],
    )
)
class DashScopeVideoTool(Tool):
    """通过通义万相（阿里灵积 DashScope）生成视频，并持久化为本地文件。

    职责：把通义万相的视频生成能力封装为 agent 可调用的工具。模式由输入自动
    判定：传 ``image_urls`` 走图生视频（单张首帧），否则文生视频。异步任务式，
    提交后轮询直至返回 ``output.video_url`` 再下载落盘。
    """

    _capability = (
        "Generate videos from text or a single first-frame image with Alibaba "
        "DashScope Tongyi Wanxiang (通义万相), returning the downloaded file path."
    )
    _usage_md = "docs/generate_video_dashscope.md"  # 工具使用说明文档路径

    config_key = "dashscope_video"  # 配置键名

    # 厂商卡片元数据（WebUI「视频生成」页与 /v1/models 由它派生；见 _video_common）
    vendor_spec = VideoVendorSpec(
        key="dashscope",
        display_name="通义万相（灵积）",
        provider="dashscope",
        tool_name="generate_video_dashscope",
        config_key="dashscope_video",
        support=("t2v", "i2v"),
        ratio_options=_RATIOS,
        resolution_options=_RESOLUTIONS,
        duration_range=(_DURATION_MIN, _DURATION_MAX),
        model_suggestions=_KNOWN_MODEL_SUGGESTIONS,
        resolution_optional=True,
        api_key_env=("DASHSCOPE_API_KEY",),
    )

    @classmethod
    def config_cls(cls):
        """返回该工具使用的配置类。"""
        return DashScopeVideoToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """「模型厂商」页配置了 dashscope 密钥（或显式 apiKey / 环境变量）即启用。"""
        config = ctx.config.dashscope_video
        if (config.api_key or "").strip():
            return True
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("dashscope")
        if (getattr(provider_cfg, "api_key", None) or "").strip():
            return True
        return bool((os.environ.get("DASHSCOPE_API_KEY") or "").strip())

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例（密钥/地址优先取「模型厂商」页 dashscope 厂商配置）。"""
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("dashscope")
        return cls(
            workspace=ctx.workspace,
            config=ctx.config.dashscope_video,
            provider_api_key=getattr(provider_cfg, "api_key", None),
            provider_api_base=getattr(provider_cfg, "api_base", None),
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: DashScopeVideoToolConfig,
        provider_api_key: str | None = None,
        provider_api_base: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~
        self.config = config  # 工具配置
        self._provider_api_key = provider_api_key  # 模型厂商页配置的 dashscope 密钥
        self.provider_api_base = provider_api_base  # 模型厂商页配置的 dashscope apiBase

    @property
    def name(self) -> str:
        """工具名称。"""
        return "generate_video_dashscope"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Generate a video with Alibaba DashScope Tongyi Wanxiang (通义万相). "
            "Text-to-video, or image-to-video from a single first-frame image "
            "(image_urls accepts a local path, a public HTTP(S) URL, or a base64 "
            "data URL; at most 1 image). Ratio: 16:9 / 9:16 / 1:1. Resolution: "
            "480P / 720P / 1080P. Duration depends on the model (wan2.6: 2-15s, "
            "wan2.5: 5 or 10s, wan2.2: fixed 5s). Runs asynchronously and returns "
            "the downloaded video file path. Other video vendors: use "
            "generate_video_kling / generate_video_minimax / generate_video_seedance."
        )

    # ---- 内部实现 ----------------------------------------------------------

    def _api_key(self) -> str:
        """解析 DashScope 密钥：模型厂商页 dashscope 密钥优先，其次配置显式值，最后环境变量。

        优先级理由同可灵/MiniMax：工具级 ``config.api_key`` 可能残留其他厂商的值。
        DashScope 视频与聊天共用同一个静态 API Key（无需签名）。
        """
        key = (
            (self._provider_api_key or "").strip()
            or (self.config.api_key or "").strip()
            or (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
        )
        if not key:
            raise DashScopeVideoError(
                "DashScope API key 未配置：请在「模型厂商」页配置 dashscope 厂商的 "
                "API Key，或在 config.json 设置 tools.dashscope_video.apiKey。"
            )
        return key

    def _api_base(self) -> str:
        """返回「模型厂商」页 dashscope 厂商的 apiBase，留空返回空串。

        刻意**不在此回退默认常量**：留空即交给 ``DashScopeVideoClient`` 归一化，
        这样「DashScope 同时当 LLM 用」时填的兼容模式 base（带
        ``/compatible-mode/v1``）只有一处收敛逻辑。
        """
        return (self.provider_api_base or "").rstrip("/")

    def _resolve_model(self, model: str | None) -> str:
        """解析模型名：残留其他厂商模型名（旧配置切厂商未切模型）回退默认。"""
        candidate = (model or "").strip() or (self.config.model or "").strip()
        lowered = candidate.lower()
        if not candidate or lowered.startswith(("doubao", "kling", "minimax")):
            return _DEFAULT_MODEL
        return candidate

    async def execute(
        self,
        prompt: str,
        image_urls: list[str] | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
        seed: int | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """执行通义万相视频生成。

        参数:
            prompt: 文本提示词（必填）。
            image_urls: 可选首帧图片列表（只支持单张）。
            ratio: 画幅比例（16:9 / 9:16 / 1:1）。
            duration: 时长（秒，按模型规则收敛）。
            resolution: 清晰度（480P / 720P / 1080P）。
            seed: 随机种子（不传则随机）。
            model: 模型覆盖（默认 wan2.6-t2v）。

        返回:
            包含视频本地路径与元数据的 JSON 字符串；出错时返回错误说明。
        """
        try:
            resolved_model = self._resolve_model(model)
            frames = [resolve_image_ref(v) for v in image_urls or []]

            client = DashScopeVideoClient(
                api_key=self._api_key(),
                api_base=self._api_base(),
                poll_interval_sec=self.config.poll_interval_sec,
                max_poll_attempts=self.config.max_poll_attempts,
                timeout=self.config.timeout_sec,
            )
            endpoint, body = client.build_request(
                prompt=prompt,
                image_urls=frames,
                ratio=ratio or self.config.default_ratio,
                duration=duration if duration is not None else self.config.default_duration,
                resolution=resolution or self.config.default_resolution,
                seed=seed if seed is not None else self.config.seed,
                watermark=self.config.watermark,
                prompt_extend=self.config.prompt_extend,
                model=resolved_model,
            )

            async with httpx.AsyncClient(timeout=self.config.timeout_sec) as http:
                task_id = await client.create_task(http, endpoint, body)
                logger.info(
                    "通义万相任务已创建：{}（model={}，endpoint={}）",
                    task_id,
                    resolved_model,
                    endpoint,
                )
                data = await client.poll(http, task_id)
                video_url = client.extract_video_url(data)
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
