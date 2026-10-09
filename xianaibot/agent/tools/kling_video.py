"""可灵（Kling）视频生成工具（自包含：客户端 + Tool）。

本模块把可灵官方 API（``api-beijing.klingai.com``）的视频生成/编辑能力封装为
``generate_video_kling`` 工具（厂商自包含架构，方向 B）：``KlingVideoClient``
负责「认证 + 请求构造 + 任务创建/轮询/取 URL」，``KlingVideoTool`` 负责参数
schema、启用门控与视频落盘。启用门控：「模型厂商」页配置了 kling 厂商密钥
（或 ``tools.kling_video.apiKey`` 显式配置）即启用。

与其他视频工具的关系
===========================
- 本模块**不 import** ``seedance_video.py`` / ``minimax_video.py``（避免循环依赖）；
- 落盘、图片/视频引用解析等厂商无关辅助复用 ``_video_common.py``。

可灵 API 要点（官方 klingai.com/document-api）
==============================================
- Base URL：``https://api-beijing.klingai.com``（中国大陆新域名；海外为
  ``api-singapore.klingai.com``）。官方已废弃旧域名 ``api.klingai.com``（会 401）。
- 认证：``Authorization: Bearer <token>``。token 有两种形态：
  1. ``AccessKey:SecretKey`` → 本模块自动生成 HS256 JWT（payload
     ``{iss: <AccessKey>, exp: now+1800, nbf: now-5}``，header
     ``{"alg":"HS256","typ":"JWT"}``，30 分钟过期）；
  2. 控制台「新建 API Key」的单密钥 / 中转网关 token → 直接作为静态 Bearer。
- 端点（可灵 3.0，模型名内嵌在 URL 路径）：
  - 文生视频：``POST /text-to-video/kling-3.0``
  - 图生视频：``POST /image-to-video/kling-3.0``
  - 参考视频：``POST /video-to-video/kling-3.0``
  - 轮询：``GET /v1/videos/{text2video|image2video|video2video}/{task_id}``
    （按任务类型区分；统一 ``/v1/videos/{task_id}`` 对 3.0 任务返回 404）
- 请求体：``contents``（多模态输入数组）+ ``settings``（时长/画幅/清晰度/音频/
  多镜头）+ ``options``（回调/水印）。与经典 ``/v1/videos/image2video`` 的平铺
  ``image_list`` 结构不同，见 ``build_request``。
- 响应：``{code:0, message, request_id, data:{task_id, task_status, task_result:{videos:[{url}]}}}``；
  状态 ``submitted/processing/succeed/failed``。
- 参数差异：可灵不支持随机种子/参考音频/参考图/水印；画幅仅 16:9 / 9:16 / 1:1；
  官方 image2video 仅接受公网图片 URL（base64 仅中转网关兼容，工具仍允许传本地
  路径并自动转 base64——中转网关实测可用）。
"""

from __future__ import annotations

import asyncio  # 轮询间隔
import base64  # JWT base64url 编码
import hashlib  # JWT HMAC-SHA256 签名
import hmac  # JWT HMAC 签名
import json  # JWT 序列化 / 任务响应 / 工具返回结果
import time  # JWT exp/nbf 时间戳
from pathlib import Path  # 工作区路径处理
from typing import Any, Protocol  # 任意类型 / 视频厂商客户端协议

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志
from pydantic import Field  # Pydantic 字段校验

from xianaibot.agent.tools._video_common import (  # 厂商无关共享辅助
    VideoToolError,
    VideoVendorSpec,
    download_and_store,
    resolve_image_ref,
    resolve_video_ref,
)
from xianaibot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from xianaibot.agent.tools.schema import (  # schema 构造器
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from xianaibot.config_base import Base  # 配置基类

# 可灵官方 API 默认 base URL（中国大陆新域名，不含 /v1，端点路径自带完整前缀）
_DEFAULT_BASE_URL = "https://api-beijing.klingai.com"
# 默认模型：可灵 3.0（模型名内嵌在 URL 路径，如 /image-to-video/kling-3.0）
_KLING_DEFAULT_MODEL = "kling-3.0"
# 可灵支持的画幅比例（subset，其余丢弃）
_KLING_RATIOS = ("16:9", "9:16", "1:1")
# 可灵支持的清晰度（WebUI「视频生成」页下拉选项）
_KLING_RESOLUTIONS = ("720p", "1080p", "4k")
# 时长边界：text/image 生视频 3–15s；带参考视频时上限 10s
_KLING_DURATION_MIN = 3
_KLING_DURATION_MAX = 15
# 已知模型列表（id, 标签）。可灵不提供 OpenAI 风格的 /models 枚举接口，模型名内嵌在
# URL 路径（如 /image-to-video/kling-3.0），故内嵌一份：id 同时作为「模型厂商」页模型
# 下拉与「视频生成」页卡片的建议模型来源（单一事实来源）。
KLING_KNOWN_MODELS: tuple[tuple[str, str], ...] = (
    ("kling-3.0", "可灵 3.0（默认）"),
    ("kling-3.0-pro", "可灵 3.0 Pro"),
    ("kling-3.0-turbo", "可灵 3.0 Turbo"),
    ("kling-v3-omni", "可灵 3.0 Omni"),
    ("kling-video-o1", "可灵 Video O1"),
    ("kling-v2.1-master", "可灵 2.1 Master"),
    ("kling-v2.1-turbo", "可灵 2.1 Turbo"),
)


class KlingVideoError(VideoToolError):
    """可灵视频生成/编辑失败时抛出。"""


def build_kling_jwt(access_key: str, secret_key: str) -> str:
    """构建可灵 API 的 HS256 JWT（官方认证方式）。

    payload：``{iss: <AccessKey>, exp: now+1800, nbf: now-5}``；
    header：``{"alg":"HS256","typ":"JWT"}``；用 SecretKey 做 HMAC-SHA256 签名。
    base64url 编码统一不带 padding。
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": access_key,
        "exp": now + 1800,
        "nbf": now - 5,
    }

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    signature = _b64url(
        hmac.new(
            secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
    )
    return f"{signing_input}.{signature}"


class KlingVideoClient:
    """可灵视频生成客户端：JWT 认证 + 请求构造 + 任务创建/轮询/取 URL。

    不依赖任何 SDK，直接用 ``httpx``。``api_key`` 支持两种形式：

    - ``"AccessKey:SecretKey"``（官方，冒号分隔）→ 自动生成 JWT；
    - 普通 token（中转网关）→ 直接作为 ``Bearer`` 静态 token。
    """

    provider_name = "kling"

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
        self.api_base = (api_base or "").rstrip("/") or self._default_base_url()
        self.poll_interval_sec = poll_interval_sec
        self.max_poll_attempts = max_poll_attempts
        self.timeout = timeout

    def _default_base_url(self) -> str:
        """可灵官方 API 默认 base URL。

        必须是普通实例方法（而非 staticmethod）：settings_api 的
        ``_image_default_base_url`` 以 ``cls._default_base_url(cls)`` 调用，
        写成 staticmethod 会因多余入参而 TypeError。
        """
        return _DEFAULT_BASE_URL

    def _authorization(self) -> str:
        """构造 Authorization 头：``AccessKey:SecretKey`` → JWT，否则静态 Bearer。"""
        if not self.api_key:
            raise KlingVideoError(
                "可灵 API key 未配置：请在「模型厂商」页配置 kling 厂商的 "
                "AccessKey:SecretKey（或中转网关 token），或在 config.json 设置 "
                "tools.kling_video.apiKey。"
            )
        if ":" in self.api_key:
            access_key, _, secret_key = self.api_key.partition(":")
            return f"Bearer {build_kling_jwt(access_key.strip(), secret_key.strip())}"
        return f"Bearer {self.api_key}"

    def build_request(
        self,
        *,
        prompt: str,
        image_urls: list[str] | None = None,
        video_urls: list[str] | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
        generate_audio: bool = True,
        model: str = _KLING_DEFAULT_MODEL,
    ) -> tuple[str, dict[str, Any]]:
        """按输入类型选择端点并构造可灵 3.0 请求体，返回 ``(endpoint_path, body)``。

        端点（模型名内嵌在 URL 路径）：
        - 有参考视频 → ``/video-to-video/{model}``；
        - 有参考图 → ``/image-to-video/{model}``（单图作首帧，多图末张作尾帧）；
        - 否则 → ``/text-to-video/{model}``。

        请求体采用可灵 3.0 的 ``settings`` + ``options`` + （``contents`` 或顶层
        ``prompt``）：图/视频生视频时提示词与参考素材放在 ``contents`` 多模态输入
        数组；文生视频时提示词在**顶层 ``prompt`` 字段**（官方不接受 contents 里的
        提示词，会报 code 1201 "prompt cannot be empty"）。``settings`` 放时长/画幅/
        清晰度/音频/多镜头。ratio 不在可灵支持集合内时丢弃；duration 截断到
        3–15（带参考视频为 3–10）。
        """
        contents: list[dict[str, Any]] | None = None
        max_duration = _KLING_DURATION_MAX
        if video_urls:
            endpoint = f"/video-to-video/{model}"
            # 可灵每次任务最多 1 段参考视频。contents 里参考视频类型为 base_video
            # （待编辑/变换的底视频）；具体类型名以官方文档为准。
            contents = [{"type": "prompt", "text": prompt}]
            for url in video_urls[:1]:
                contents.append({"type": "base_video", "url": url})
            max_duration = 10  # 带参考视频仅支持 3–10s
        elif image_urls:
            endpoint = f"/image-to-video/{model}"
            # 单张参考图作为首帧；多图时前段作首帧参考、最后一张作尾帧锚点。
            contents = [{"type": "prompt", "text": prompt}]
            for i, url in enumerate(image_urls):
                contents.append(
                    {
                        "type": (
                            "end_frame"
                            if len(image_urls) > 1 and i == len(image_urls) - 1
                            else "first_frame"
                        ),
                        "url": url,
                    }
                )
        else:
            # 官方文生视频接口的提示词在顶层 prompt 字段，不放进 contents
            # （contents 仅供图/视频生视频）；否则返回 code 1201
            # "prompt cannot be empty"。
            endpoint = f"/text-to-video/{model}"

        settings: dict[str, Any] = {
            # 官方 settings.audio 枚举为 "native"（生成原生音频）/ "off"；"on" 会被
            # API 拒绝（code 1201 settings.audio value 'on' is invalid）。
            "audio": "native" if generate_audio else "off",
            "multi_shot": False,
        }
        if ratio and ratio in _KLING_RATIOS:
            settings["aspect_ratio"] = ratio
        if duration:
            settings["duration"] = min(
                max(int(duration), _KLING_DURATION_MIN), max_duration
            )
        if resolution:
            settings["resolution"] = str(resolution).lower()

        body: dict[str, Any] = {
            "settings": settings,
            "options": {"watermark_info": {"enabled": False}},
        }
        if contents is not None:
            body["contents"] = contents
        else:
            body["prompt"] = prompt
        return endpoint, body

    async def create_task(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> str:
        """创建可灵视频任务并返回 task_id。"""
        url = f"{self.api_base.rstrip('/')}{endpoint}"
        headers = {
            "Authorization": self._authorization(),
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise KlingVideoError(f"创建可灵任务请求失败：{exc}。请检查网络或 baseUrl 配置") from exc
        data = response.json() if response.content else {}
        if response.status_code >= 400 or data.get("code") != 0:
            msg = data.get("message") or f"HTTP {response.status_code}"
            raise KlingVideoError(f"创建可灵任务失败：{msg}（{data}）")
        data_obj = data.get("data") or {}
        # 官方 3.0 创建响应返回 data.id；经典 v1 形态返回 data.task_id，两者兼容。
        task_id = data_obj.get("id") or data_obj.get("task_id")
        if not task_id:
            raise KlingVideoError(f"创建可灵任务未返回 task_id：{data}")
        return str(task_id)

    @staticmethod
    def poll_path_for(endpoint: str) -> str:
        """从创建端点派生统一轮询路径。

        官方 3.0 的轮询路径按任务类型区分：``/v1/videos/text2video/{id}``、
        ``/v1/videos/image2video/{id}``、``/v1/videos/video2video/{id}``；
        统一的 ``/v1/videos/{id}`` 对 3.0 任务返回 404。无匹配时回退 ``/v1/videos``。
        """
        kind = next(
            (
                kind
                for marker, kind in (
                    ("text-to-video", "text2video"),
                    ("image-to-video", "image2video"),
                    ("video-to-video", "video2video"),
                )
                if marker in endpoint
            ),
            None,
        )
        return f"/v1/videos/{kind}" if kind else "/v1/videos"

    async def poll(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        *,
        poll_path: str | None = None,
    ) -> dict[str, Any]:
        """轮询可灵任务直至 succeed/failed，返回完整响应 JSON。

        ``poll_path`` 建议由 :meth:`poll_path_for` 从创建端点派生；缺省时用经典
        ``/v1/videos/{task_id}``（兼容旧版/中转形态）。
        """
        poll_path = poll_path or "/v1/videos"
        url = f"{self.api_base.rstrip('/')}{poll_path}/{task_id}"
        headers = {"Authorization": self._authorization()}
        for attempt in range(self.max_poll_attempts):
            await asyncio.sleep(self.poll_interval_sec)
            try:
                response = await client.get(url, headers=headers)
            except httpx.RequestError as exc:
                logger.warning("可灵轮询错误（第 {} 次）：{}", attempt + 1, exc)
                continue
            data = response.json() if response.content else {}
            if data.get("code") != 0:
                logger.warning("可灵轮询响应异常（第 {} 次）：{}", attempt + 1, data)
                continue
            payload = data.get("data") or {}
            status = str(payload.get("task_status", "")).lower()
            if status == "succeed":
                return data
            if status == "failed":
                raise KlingVideoError(
                    f"可灵任务失败：{payload.get('task_status_msg') or '任务失败'}"
                )
            # submitted / processing 继续轮询
        raise KlingVideoError(f"可灵任务超时（超过 {self.max_poll_attempts} 次轮询）")

    @staticmethod
    def extract_video_url(data: dict[str, Any]) -> str:
        """从已完成任务响应中提取第一个视频 URL。"""
        payload = data.get("data") or {}
        task_result = payload.get("task_result") or {}
        videos = task_result.get("videos") or []
        if not videos:
            raise KlingVideoError(f"可灵任务成功但未返回视频 URL：{data}")
        url = videos[0].get("url")
        if not url:
            raise KlingVideoError(f"可灵任务返回的视频缺少 url 字段：{data}")
        return str(url)


# ---------------------------------------------------------------------------
# Registry —— 视频生成 Provider 注册表（仿 image_generation.py 的模块副作用填充）
# ---------------------------------------------------------------------------


class VideoGenProvider(Protocol):
    """视频生成厂商客户端协议（注册表仅依赖这两个成员）。

    各厂商客户端刻意**不共享基类**——按 ``.agent/design.md``「重复优于过早抽象」，
    每个厂商文件自包含、可独立阅读，只在此处满足同一结构：
    ``provider_name`` 供按名解析，``_default_base_url`` 供「模型厂商」页
    apiBase 留空时的默认地址展示（``settings_api._image_default_base_url``
    以 ``cls._default_base_url(cls)`` 调用，故它必须是普通实例方法）。
    """

    provider_name: str

    def _default_base_url(self) -> str: ...


_VIDEO_GEN_PROVIDERS: dict[str, type[VideoGenProvider]] = {}


def register_video_gen_provider(cls: type[VideoGenProvider]) -> None:
    """仅在导入期注册一个视频生成 Provider。

    本注册表是**全局唯一**的：其他厂商模块（如 ``minimax_video.py``）应 import
    本函数并调用，而**不要**再建一个同名注册表，否则
    :func:`video_gen_provider_names` 看不到它们。
    """
    name = cls.provider_name
    if not name:
        raise ValueError(f"{cls.__name__} must set provider_name")
    _VIDEO_GEN_PROVIDERS[name] = cls


def get_video_gen_provider(name: str) -> type[VideoGenProvider] | None:
    """按 name 取得已注册的视频生成 Provider 类，未注册返回 None。"""
    return _VIDEO_GEN_PROVIDERS.get(name)


def video_gen_provider_names() -> tuple[str, ...]:
    """按注册顺序返回所有视频生成 Provider 名称。"""
    return tuple(_VIDEO_GEN_PROVIDERS)


register_video_gen_provider(KlingVideoClient)


# ---------------------------------------------------------------------------
# Tool —— generate_video_kling（厂商自包含，与 client 同文件）
# ---------------------------------------------------------------------------


class KlingVideoToolConfig(Base):
    """可灵视频生成工具配置。

    职责：承载可灵视频生成工具的运行时配置项。厂商自包含重构后无
    ``enabled`` / ``provider`` 字段——启用与否由「模型厂商」页是否配置
    kling 密钥决定（或本段 ``api_key`` 显式覆盖）。base URL 刻意不设字段：
    自定义网关地址请填「模型厂商」页 kling 厂商的 apiBase。
    """

    api_key: str | None = None  # 显式 API Key；缺省回退「模型厂商」页 kling 厂商密钥
    model: str = _KLING_DEFAULT_MODEL  # 默认模型（可灵 3.0，模型名内嵌 URL 路径）
    default_ratio: str = "16:9"  # 默认画幅（可灵仅 16:9 / 9:16 / 1:1）
    default_duration: int = Field(default=5, ge=_KLING_DURATION_MIN, le=_KLING_DURATION_MAX)  # 默认时长（秒）
    default_resolution: str | None = None  # 默认清晰度（720p/1080p/4k），None 表示不下发
    generate_audio: bool = True  # 是否默认开启原生音频（settings.audio = native）
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
                "参考图片：本地文件路径（含微信/渠道收到的图片路径）、"
                "可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description=(
                "可选参考图（首帧 / 尾帧）。单张作首帧；多张时前段作首帧参考、"
                "最后一张作尾帧锚点。本地路径会自动转 base64 上传（中转网关实测可用）。"
            ),
        ),
        video_urls=ArraySchema(
            StringSchema("参考视频的可公开访问 HTTP(S) URL。"),
            description=(
                "可选参考视频（视频生视频）。仅支持公网 HTTP(S) URL，不支持本地文件；"
                "每次任务最多 1 段，且此时长上限降为 10 秒。"
            ),
        ),
        ratio=StringSchema(
            "画幅比例（可灵仅支持 16:9 / 9:16 / 1:1，其余取值会被丢弃）。",
            enum=_KLING_RATIOS,
        ),
        duration=IntegerSchema(
            description="视频时长（秒）3–15；带参考视频时 3–10，超出自动截断。",
            minimum=_KLING_DURATION_MIN,
            maximum=_KLING_DURATION_MAX,
        ),
        resolution=StringSchema(
            "清晰度：720p / 1080p / 4k（可灵 3.0 支持）。",
            enum=("720p", "1080p", "4k"),
        ),
        generate_audio=BooleanSchema(
            description=(
                "是否开启原生音频（settings.audio=native，默认开启：未传参时自动打开，"
                "传 false 可关闭）。"
            ),
        ),
        model=StringSchema(
            "可选模型覆盖（默认用配置里的 model，如 kling-3.0 / kling-3.0-pro / "
            "kling-3.0-turbo / kling-v3-omni / kling-video-o1；模型名内嵌在请求 URL 路径）。",
        ),
        required=["prompt"],
    )
)
class KlingVideoTool(Tool):
    """通过可灵（Kling）生成/编辑视频，并持久化为本地文件。

    职责：把可灵的视频生成/编辑能力封装为 agent 可调用的工具。调用时传入
    prompt（必填）与可选参考图/参考视频、画幅、时长、清晰度等，工具内部创建
    异步任务并轮询直至完成，随后把生成的视频下载到媒体目录，返回视频文件
    路径与元数据。不支持的能力（参考音频/参考图风格/随机种子/水印）不在参数
    schema 中，传入会被 ``**kwargs`` 静默吸收。
    """

    _capability = "Generate or edit videos from text/image/video with Kling 可灵 (returns file path)."
    _usage_md = "docs/generate_video_kling.md"  # 工具使用说明文档路径

    config_key = "kling_video"  # 配置键名

    # 厂商卡片元数据（WebUI「视频生成」页与 /v1/models 由它派生；见 _video_common）
    vendor_spec = VideoVendorSpec(
        key="kling",
        display_name="可灵",
        provider="kling",
        tool_name="generate_video_kling",
        config_key="kling_video",
        support=("t2v", "i2v", "v2v"),
        ratio_options=_KLING_RATIOS,
        resolution_options=_KLING_RESOLUTIONS,
        duration_range=(_KLING_DURATION_MIN, _KLING_DURATION_MAX),
        model_suggestions=tuple(model_id for model_id, _label in KLING_KNOWN_MODELS),
        resolution_optional=True,
    )

    @classmethod
    def config_cls(cls):
        """返回该工具使用的配置类。"""
        return KlingVideoToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """「模型厂商」页配置了 kling 密钥（或显式 apiKey）即启用。"""
        config = ctx.config.kling_video
        if (config.api_key or "").strip():
            return True
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("kling")
        return bool((getattr(provider_cfg, "api_key", None) or "").strip())

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例（密钥/地址优先取「模型厂商」页 kling 厂商配置）。"""
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("kling")
        return cls(
            workspace=ctx.workspace,
            config=ctx.config.kling_video,
            provider_api_key=getattr(provider_cfg, "api_key", None),
            provider_api_base=getattr(provider_cfg, "api_base", None),
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: KlingVideoToolConfig,
        provider_api_key: str | None = None,
        provider_api_base: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~
        self.config = config  # 工具配置
        self._provider_api_key = provider_api_key  # 模型厂商页配置的 kling 密钥
        self.provider_api_base = provider_api_base  # 模型厂商页配置的 kling apiBase

    @property
    def name(self) -> str:
        """工具名称。"""
        return "generate_video_kling"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Generate or edit a video with Kling (可灵, default model kling-3.0). "
            "Accepts text, image and video inputs (no audio reference / no seed). "
            "Runs asynchronously and returns the downloaded video file path. "
            "Pass local paths or public URLs for image reference (local files are auto "
            "base64-encoded); for video reference pass a public HTTP(S) URL only. "
            "Ratio supports 16:9 / 9:16 / 1:1 only; duration 3-15s (3-10s with video "
            "reference). Other video vendors: use generate_video_seedance or "
            "generate_video_minimax instead. Native audio is generated by default; "
            "pass generate_audio=false to turn it off."
        )

    # ---- 内部实现 ----------------------------------------------------------

    def _api_key(self) -> str:
        """解析可灵密钥：模型厂商页 kling 密钥优先，其次配置显式值。

        优先级不能反过来：``config.api_key`` 是工具级 key，历史配置可能残留
        其他厂商的值（如方舟 ``ark-`` 前缀），用它鉴权可灵必然 401。
        可灵官方密钥为 ``AccessKey:SecretKey``（冒号分隔，自动生成 JWT）；
        也可填中转网关的静态 token。
        """
        key = (self._provider_api_key or "").strip() or (self.config.api_key or "").strip()
        if not key:
            raise KlingVideoError(
                "可灵 API key 未配置：请在「模型厂商」页配置 kling 厂商的 "
                "AccessKey:SecretKey（或中转网关 token），或在 config.json 设置 "
                "tools.kling_video.apiKey。"
            )
        return key

    def _api_base(self) -> str:
        """解析可灵 base URL：模型厂商页 kling 厂商 apiBase → 可灵官方默认。"""
        if self.provider_api_base:
            return self.provider_api_base.rstrip("/")
        return _DEFAULT_BASE_URL

    def _resolve_model(self, model: str | None) -> str:
        """解析可灵模型名：残留其他厂商模型名（旧配置切厂商未切模型）时回退默认。"""
        candidate = (model or "").strip() or (self.config.model or "").strip()
        lowered = candidate.lower()
        if not candidate or "seedance" in lowered or "minimax" in lowered:
            return _KLING_DEFAULT_MODEL
        return candidate

    async def execute(
        self,
        prompt: str,
        image_urls: list[str] | None = None,
        video_urls: list[str] | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
        generate_audio: bool | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """执行可灵视频生成/编辑。

        参数:
            prompt: 文本提示词（必填）。
            image_urls: 可选参考图列表（本地路径 / URL / data URL；单张首帧、多张末张尾帧）。
            video_urls: 可选参考视频列表（仅公网 URL，最多 1 段）。
            ratio: 画幅比例（16:9 / 9:16 / 1:1）。
            duration: 时长（秒），3–15（带参考视频 3–10）。
            resolution: 清晰度（720p / 1080p / 4k）。
            generate_audio: 是否生成原生音频（默认开启）。
            model: 模型覆盖。

        返回:
            包含视频本地路径与元数据的 JSON 字符串；出错时返回错误说明。
        """
        try:
            if generate_audio is None:
                generate_audio = self.config.generate_audio
            resolved_model = self._resolve_model(model)

            # 参考图：本地路径自动转 base64 data URL（中转网关实测接受 base64），
            # 公网 HTTP(S) URL / 已有 data URL 原样透传；参考视频：仅公网 URL。
            kling_images = [resolve_image_ref(v) for v in image_urls or []]
            kling_videos = [resolve_video_ref(v) for v in video_urls or []]

            client = KlingVideoClient(
                api_key=self._api_key(),
                api_base=self._api_base(),
                poll_interval_sec=self.config.poll_interval_sec,
                max_poll_attempts=self.config.max_poll_attempts,
                timeout=self.config.timeout_sec,
            )
            endpoint, body = client.build_request(
                prompt=prompt,
                image_urls=kling_images,
                video_urls=kling_videos,
                ratio=ratio,
                duration=duration if duration is not None else self.config.default_duration,
                resolution=resolution or self.config.default_resolution,
                generate_audio=generate_audio,
                model=resolved_model,
            )

            async with httpx.AsyncClient(timeout=self.config.timeout_sec) as http:
                task_id = await client.create_task(http, endpoint, body)
                logger.info(
                    "可灵任务已创建：{}（model={}，endpoint={}）", task_id, resolved_model, endpoint
                )
                data = await client.poll(
                    http, task_id, poll_path=client.poll_path_for(endpoint)
                )
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
