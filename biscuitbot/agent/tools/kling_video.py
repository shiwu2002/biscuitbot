"""可灵（Kling）视频生成客户端。

本模块把可灵官方 API（``api-beijing.klingai.com``）的视频生成/编辑能力封装为轻量客户端，
供 ``generate_video`` 工具（``seedance_video.py``）在 ``provider == "kling"`` 时调用。

与 seedance_video.py 的关系
===========================
- 本模块**不 import** ``seedance_video.py``（否则工具加载/配置 schema 会触发循环依赖）；
  ``seedance_video.py`` 单向 import 本模块。
- 落盘、图片/视频引用解析等工具职责仍由 ``seedance_video.py`` 的 ``_execute_kling``
  复用其现有方法完成，本模块只负责「认证 + 请求构造 + 任务创建/轮询/取 URL」。

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
  - 轮询：``GET /v1/videos/{task_id}``
- 请求体：``contents``（多模态输入数组）+ ``settings``（时长/画幅/清晰度/音频/
  多镜头）+ ``options``（回调/水印）。与经典 ``/v1/videos/image2video`` 的平铺
  ``image_list`` 结构不同，见 ``build_request``。
- 响应：``{code:0, message, request_id, data:{task_id, task_status, task_result:{videos:[{url}]}}}``；
  状态 ``submitted/processing/succeed/failed``。
- 参数差异：可灵不支持随机种子/参考音频；官方 image2video 仅接受公网图片 URL
  （base64 仅中转网关兼容）。
"""

from __future__ import annotations

import asyncio  # 轮询间隔
import base64  # JWT base64url 编码
import hashlib  # JWT HMAC-SHA256 签名
import hmac  # JWT HMAC 签名
import json  # JWT 序列化 / 任务响应
import time  # JWT exp/nbf 时间戳
from typing import Any  # 任意类型

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志

# 可灵官方 API 默认 base URL（中国大陆新域名，不含 /v1，端点路径自带完整前缀）
_DEFAULT_BASE_URL = "https://api-beijing.klingai.com"
# 默认模型：可灵 3.0（模型名内嵌在 URL 路径，如 /image-to-video/kling-3.0）
_KLING_DEFAULT_MODEL = "kling-3.0"
# 可灵支持的画幅比例（subset，其余丢弃）
_KLING_RATIOS = ("16:9", "9:16", "1:1")
# 时长边界：text/image 生视频 3–15s；带参考视频时上限 10s
_KLING_DURATION_MIN = 3
_KLING_DURATION_MAX = 15


class KlingVideoError(RuntimeError):
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
                "tools.seedance_video.apiKey。"
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

        请求体采用可灵 3.0 的 ``contents`` + ``settings`` + ``options`` 结构：
        ``contents`` 是「提示词 + 首尾帧 + 参考视频」的多模态输入数组；``settings``
        放时长/画幅/清晰度/音频/多镜头。ratio 不在可灵支持集合内时丢弃；duration
        截断到 3–15（带参考视频为 3–10）。
        """
        contents: list[dict[str, Any]] = [{"type": "prompt", "text": prompt}]
        max_duration = _KLING_DURATION_MAX
        if video_urls:
            endpoint = f"/video-to-video/{model}"
            # 可灵每次任务最多 1 段参考视频。contents 里参考视频类型为 base_video
            # （待编辑/变换的底视频）；具体类型名以官方文档为准。
            for url in video_urls[:1]:
                contents.append({"type": "base_video", "url": url})
            max_duration = 10  # 带参考视频仅支持 3–10s
        elif image_urls:
            endpoint = f"/image-to-video/{model}"
            # 单张参考图作为首帧；多图时前段作首帧参考、最后一张作尾帧锚点。
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
            "contents": contents,
            "settings": settings,
            "options": {"watermark_info": {"enabled": False}},
        }
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
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise KlingVideoError(f"创建可灵任务未返回 task_id：{data}")
        return str(task_id)

    async def poll(self, client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
        """轮询可灵任务直至 succeed/failed，返回完整响应 JSON。"""
        url = f"{self.api_base.rstrip('/')}/v1/videos/{task_id}"
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

_VIDEO_GEN_PROVIDERS: dict[str, type[KlingVideoClient]] = {}


def register_video_gen_provider(cls: type[KlingVideoClient]) -> None:
    """仅在导入期注册一个视频生成 Provider。"""
    name = cls.provider_name
    if not name:
        raise ValueError(f"{cls.__name__} must set provider_name")
    _VIDEO_GEN_PROVIDERS[name] = cls


def get_video_gen_provider(name: str) -> type[KlingVideoClient] | None:
    """按 name 取得已注册的视频生成 Provider 类，未注册返回 None。"""
    return _VIDEO_GEN_PROVIDERS.get(name)


def video_gen_provider_names() -> tuple[str, ...]:
    """按注册顺序返回所有视频生成 Provider 名称。"""
    return tuple(_VIDEO_GEN_PROVIDERS)


register_video_gen_provider(KlingVideoClient)
