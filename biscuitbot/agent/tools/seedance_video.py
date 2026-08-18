"""Seedance 视频生成工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的视频生成组件。
在项目架构中起到的作用：把火山引擎方舟（Volcengine Ark）Seedance 视频大模型
封装为统一的 ``Tool``，让 agent 通过一次函数调用即可完成「文生视频 / 图生视频 /
参考图+参考视频+参考音频编辑」，并在工具内部完成异步任务轮询与视频落盘，
返回视频文件路径供后续引用。

相较早期的 ``seedance`` 技能（教 agent 写脚本 + exec），本工具把「创建任务 →
轮询 → 取视频 → 下载」收敛进原生 Python 代码，避免多轮脚本往返与 SDK 环境依赖：
- 文本/图片/视频/音频四种输入通过 JSON Schema 暴露为独立参数；
- 图片、音频的本地文件自动转 base64（方舟支持），视频仅支持公网 URL（方舟限制）；
- 异步轮询在工具内部完成，轮询间隔与最大次数可配置。

HTTP 直接调用方舟 OpenAI 兼容接口（``httpx``），无需 ``volcengine-python-sdk``。
"""

from __future__ import annotations

import asyncio  # 异步轮询
import base64  # 本地图片/音频转 base64
import json  # 序列化工具返回结果
import os  # 读取 ARK_API_KEY 环境变量
import uuid  # 生成唯一 artifact id
from datetime import datetime  # artifact 归档日期
from pathlib import Path  # 路径处理
from typing import Any  # 任意类型

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志
from pydantic import Field  # Pydantic 字段校验

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import (  # schema 构造器
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from biscuitbot.config.paths import get_media_dir  # 媒体目录获取函数
from biscuitbot.config_base import Base  # 配置基类
from biscuitbot.utils.helpers import detect_image_mime, ensure_dir  # MIME 探测与建目录

# 方舟 Seedance 默认 base URL（OpenAI 兼容，内容生成任务走 /contents/generations/tasks）
_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
# 支持的模型 ID：2.0 与 2.5（也支持已开通的 Endpoint ID ep-...）
_MODEL_2_0 = "doubao-seedance-2-0-260128"
_MODEL_2_5 = "doubao-seedance-2-5-260628"
# 支持的画幅比例
_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")
# 支持的清晰度（480p/720p/1080p 通用，4K 仅 2.5）
_RESOLUTIONS = ("480p", "720p", "1080p", "4K")
# 视频时长边界：2.0 支持 4–15s，2.5 支持到 30s
_MIN_DURATION = 4
_MAX_DURATION = 30
# 视频 MIME → 扩展名（用于落盘时确定后缀）
_VIDEO_MIME_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
# 固定前置提示词：声明参考图/主体为 AIGC 生成的虚拟角色（数字插画），
# 避免 Seedance 人脸检测对非真实人物的过度拦截
_AIGC_CHARACTER_DISCLAIMER = (
    "我上传的图片是一个完全由AIGC生成的虚拟人且此为数字插画角色设计，"
    "非真实人物肖像，不涉及任何真人形象。"
)


class SeedanceVideoError(RuntimeError):
    """Seedance 视频生成/编辑失败时抛出。"""


class SeedanceVideoToolConfig(Base):
    """Seedance 视频生成工具配置。

    职责：承载视频生成工具的运行时配置项，包括启停、API Key、模型、
    默认画幅/时长/清晰度、水印与音画设置、轮询参数与 artifact 保存目录。
    """

    enabled: bool = False  # 是否启用视频生成工具
    provider: str = "volcengine"  # 视频厂商（默认火山方舟；预留多厂商扩展）
    api_key: str | None = None  # 显式 API Key；缺省回退到环境变量 ARK_API_KEY
    base_url: str = _DEFAULT_BASE_URL  # 方舟 base URL
    model: str = _MODEL_2_0  # 默认模型（2.0），可配成 2.5 或 Endpoint ID
    default_ratio: str = "16:9"  # 默认画幅
    default_duration: int = Field(default=5, ge=_MIN_DURATION, le=_MAX_DURATION)  # 默认时长（秒）
    default_resolution: str | None = None  # 默认清晰度，None 表示交给模型
    generate_audio: bool = False  # 是否默认开启音画同步生成音频
    watermark: bool = True  # 是否默认添加水印
    save_dir: str = "generated_video"  # artifact 保存子目录名
    poll_interval_sec: float = Field(default=10.0, ge=1.0, le=60.0)  # 轮询间隔（秒）
    max_poll_attempts: int = Field(default=180, ge=1, le=600)  # 最大轮询次数（约 30 分钟）
    timeout_sec: float = Field(default=120.0, ge=10.0, le=600.0)  # 单次 HTTP 超时（秒）


def _detect_video_ext(raw: bytes) -> str:
    """根据视频魔数返回扩展名，无法识别时默认 ``.mp4``。

    - ``ftyp``（偏移 4）→ mp4 / mov 容器；
    - ``\\x1a\\x45\\xdf\\xa3``（EBML）→ webm。
    """
    if raw[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        # 若 brand 是 qt 则视为 mov，否则按 mp4 处理
        brand = raw[8:12]
        return ".mov" if brand == b"qt  " else ".mp4"
    return ".mp4"


def _is_data_url(value: str) -> bool:
    """判断字符串是否为 ``data:...`` 形式的内联数据 URL。"""
    return value.strip().lower().startswith("data:")


def _is_http_url(value: str) -> bool:
    """判断字符串是否为 HTTP(S) URL。"""
    return value.strip().lower().startswith(("http://", "https://"))


def _audio_mime_from_suffix(path: Path) -> str:
    """根据音频文件扩展名推断 MIME，无法识别时默认 ``audio/mpeg``。"""
    suffix = path.suffix.lower()
    mapping = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".pcm": "audio/wav",
    }
    return mapping.get(suffix, "audio/mpeg")


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "视频生成/编辑的文本提示词（必填）。描述主体、运镜、景别、构图、光影、氛围与节奏越具体越好。",
            min_length=1,
        ),
        image_urls=ArraySchema(
            StringSchema(
                "参考图片：本地文件路径、可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description="可选参考图（首帧 / 参考画面）。本地路径会自动转 base64 上传。",
        ),
        video_urls=ArraySchema(
            StringSchema("参考视频的可公开访问 HTTP(S) URL。"),
            description="可选参考视频。Seedance 视频输入仅支持公网 URL，不支持本地文件。",
        ),
        audio_urls=ArraySchema(
            StringSchema(
                "参考音频：本地文件路径、可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description="可选参考音频（音画同步 / 口型参考等）。本地路径会自动转 base64 上传。",
        ),
        ratio=StringSchema(
            "画幅比例。",
            enum=_RATIOS,
        ),
        duration=IntegerSchema(
            description="视频时长（秒），2.0 支持 4–15，2.5 支持到 30。",
            minimum=_MIN_DURATION,
            maximum=_MAX_DURATION,
        ),
        resolution=StringSchema(
            "清晰度（4K 仅 2.5 支持）。",
            enum=_RESOLUTIONS,
        ),
        generate_audio=BooleanSchema(
            description="是否开启音画同步生成音频。",
        ),
        watermark=BooleanSchema(
            description="是否添加水印（默认开启）。",
        ),
        model=StringSchema(
            "可选模型覆盖（默认用配置里的 model，可切到 doubao-seedance-2-0-260128 或 Endpoint ID ep-...）。",
        ),
        required=["prompt"],
    )
)
class SeedanceVideoTool(Tool):
    """通过火山方舟 Seedance 生成/编辑视频，并持久化为本地文件。

    职责：把 Seedance 的视频生成/编辑能力封装为 agent 可调用的工具。调用时
    传入 prompt（必填）与可选的参考图/参考视频/参考音频、画幅、时长、清晰度等，
    工具内部创建异步任务并轮询直至完成，随后把生成的视频下载到媒体目录，
    返回视频文件路径与元数据。
    """

    _capability = (
        "Generate or edit videos from text/image/video/audio with Seedance (returns file path)."
    )
    _usage_md = "docs/generate_video.md"  # 工具使用说明文档路径

    config_key = "seedance_video"  # 配置键名

    @classmethod
    def config_cls(cls):
        """返回该工具使用的配置类。"""
        return SeedanceVideoToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """仅当配置中 seedance_video.enabled 为 True 时启用。"""
        return ctx.config.seedance_video.enabled

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        # 密钥从统一 providers 配置按 seedance_video.provider 取用（默认火山方舟），
        # 与文生图解耦：两者可指向不同厂商，厂商密钥统一走「模型厂商」页配置。
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_name = ctx.config.seedance_video.provider
        provider_cfg = provider_configs.get(provider_name)
        ark_api_key = getattr(provider_cfg, "api_key", None)
        return cls(
            workspace=ctx.workspace,
            config=ctx.config.seedance_video,
            ark_api_key=ark_api_key,
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: SeedanceVideoToolConfig,
        ark_api_key: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~
        self.config = config  # 工具配置
        self._ark_api_key = ark_api_key  # 模型厂商页配置的火山方舟密钥（图像/视频共用）

    @property
    def name(self) -> str:
        """工具名称。"""
        return "generate_video"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Generate or edit a video with Seedance (ByteDance Seed video model). "
            "Accepts text, image, video and audio inputs. Runs asynchronously and returns the "
            "downloaded video file path. Pass local paths or public URLs for image/audio reference "
            "(local files are auto base64-encoded); for video reference pass a public HTTP(S) URL only."
        )

    # ---- 内部实现 ----------------------------------------------------------

    def _api_key(self) -> str:
        """解析 API Key：配置显式值优先，其次模型厂商页的火山方舟密钥，最后环境变量 ARK_API_KEY。"""
        key = (
            (self.config.api_key or "").strip()
            or (self._ark_api_key or "").strip()
            or os.environ.get("ARK_API_KEY", "").strip()
        )
        if not key:
            raise SeedanceVideoError(
                "Seedance API key 未配置：请在「模型厂商」页配置火山方舟密钥，"
                "或在 config.json 设置 tools.seedance_video.apiKey，或导出环境变量 ARK_API_KEY。"
            )
        return key

    def _resolve_image_ref(self, value: str) -> str:
        """解析单个参考图：data URL / HTTP URL 原样返回，本地路径转 base64 data URL。"""
        value = value.strip()
        if _is_data_url(value) or _is_http_url(value):
            return value
        path = Path(value).expanduser()
        if not path.is_file():
            raise SeedanceVideoError(f"参考图片不存在：{value}")
        raw = path.read_bytes()
        mime = detect_image_mime(raw)
        if mime is None:
            raise SeedanceVideoError(f"不支持的图片格式：{value}")
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def _resolve_audio_ref(self, value: str) -> str:
        """解析单个参考音频：data URL / HTTP URL 原样返回，本地路径转 base64 data URL。"""
        value = value.strip()
        if _is_data_url(value) or _is_http_url(value):
            return value
        path = Path(value).expanduser()
        if not path.is_file():
            raise SeedanceVideoError(f"参考音频不存在：{value}")
        raw = path.read_bytes()
        mime = _audio_mime_from_suffix(path)
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def _resolve_video_ref(self, value: str) -> str:
        """解析单个参考视频：仅接受可公开访问的 HTTP(S) URL。"""
        value = value.strip()
        if _is_http_url(value):
            return value
        raise SeedanceVideoError(
            f"参考视频仅支持公网 HTTP(S) URL，本地文件请先上传到可访问地址：{value}"
        )

    def _build_content(
        self,
        prompt: str,
        image_urls: list[str] | None,
        video_urls: list[str] | None,
        audio_urls: list[str] | None,
    ) -> list[dict[str, Any]]:
        """把文本 + 可选参考素材组装为方舟 content 数组。"""
        content: list[dict[str, Any]] = [
            {"type": "text", "text": f"{_AIGC_CHARACTER_DISCLAIMER}{prompt}"}
        ]
        for value in image_urls or []:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._resolve_image_ref(value)},
                    "role": "reference_image",
                }
            )
        for value in video_urls or []:
            content.append(
                {
                    "type": "video_url",
                    "video_url": {"url": self._resolve_video_ref(value)},
                    "role": "reference_video",
                }
            )
        for value in audio_urls or []:
            content.append(
                {
                    "type": "audio_url",
                    "audio_url": {"url": self._resolve_audio_ref(value)},
                    "role": "reference_audio",
                }
            )
        return content

    async def _create_task(self, client: httpx.AsyncClient, body: dict[str, Any]) -> str:
        """创建内容生成任务并返回 task id。"""
        url = f"{self.config.base_url.rstrip('/')}/contents/generations/tasks"
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise SeedanceVideoError(f"创建任务请求失败：{exc}") from exc
        if response.status_code >= 400:
            raise SeedanceVideoError(
                f"创建任务失败（HTTP {response.status_code}）：{response.text[:500]}"
            )
        data = response.json()
        task_id = data.get("id")
        if not task_id:
            raise SeedanceVideoError(f"创建任务未返回 task id：{data}")
        return str(task_id)

    async def _poll_until_done(self, client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
        """轮询任务直至成功或失败，返回最终任务对象。"""
        url = f"{self.config.base_url.rstrip('/')}/contents/generations/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        for attempt in range(self.config.max_poll_attempts):
            await asyncio.sleep(self.config.poll_interval_sec)
            try:
                response = await client.get(url, headers=headers)
            except httpx.RequestError as exc:
                logger.warning("Seedance 轮询错误（第 {} 次）：{}", attempt + 1, exc)
                continue
            if response.status_code >= 400:
                logger.warning(
                    "Seedance 轮询 HTTP {}（第 {} 次）", response.status_code, attempt + 1
                )
                continue
            data = response.json()
            status = str(data.get("status", "")).lower()
            if status == "succeeded":
                return data
            if status in ("failed", "cancelled", "canceled"):
                error = data.get("error") or data.get("message") or "任务失败"
                raise SeedanceVideoError(f"Seedance 任务失败：{error}")
            # queued / running / pending 等继续轮询
        raise SeedanceVideoError(
            f"Seedance 任务超时（超过 {self.config.max_poll_attempts} 次轮询）"
        )

    @staticmethod
    def _extract_video_url(task: dict[str, Any]) -> str:
        """从已完成任务中提取视频 URL。"""
        content = task.get("content") or {}
        url = content.get("video_url") if isinstance(content, dict) else None
        if not url:
            raise SeedanceVideoError(f"任务成功但未返回视频 URL：{task}")
        return str(url)

    async def _download_and_store(self, client: httpx.AsyncClient, video_url: str) -> dict[str, Any]:
        """下载生成的视频并落盘到媒体目录，返回元数据。"""
        try:
            response = await client.get(video_url)
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise SeedanceVideoError(f"下载生成的视频失败：{exc}") from exc
        raw = response.content
        if not raw:
            raise SeedanceVideoError("下载的视频为空")

        ext = _detect_video_ext(raw)
        media_root = get_media_dir().resolve()
        day_dir = ensure_dir(media_root / self.config.save_dir / datetime.now().astimezone().strftime("%Y-%m-%d"))
        artifact_id = f"vid_{uuid.uuid4().hex[:12]}"
        video_path = day_dir / f"{artifact_id}{ext}"
        metadata_path = day_dir / f"{artifact_id}.json"

        video_path.write_bytes(raw)
        metadata: dict[str, Any] = {
            "id": artifact_id,
            "path": str(video_path),
            "ext": ext,
            "source_url": video_url,
            "model": self.config.model,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return metadata

    async def execute(
        self,
        prompt: str,
        image_urls: list[str] | None = None,
        video_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
        generate_audio: bool | None = None,
        watermark: bool | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """执行视频生成/编辑。

        参数:
            prompt: 文本提示词（必填）。
            image_urls: 可选参考图列表（本地路径 / URL / data URL）。
            video_urls: 可选参考视频列表（仅公网 URL）。
            audio_urls: 可选参考音频列表（本地路径 / URL / data URL）。
            ratio: 画幅比例。
            duration: 时长（秒）。
            resolution: 清晰度。
            generate_audio: 是否音画同步生成音频。
            watermark: 是否加水印。
            model: 模型覆盖。

        返回:
            包含视频本地路径与元数据的 JSON 字符串；出错时返回错误说明。
        """
        try:
            body: dict[str, Any] = {
                "model": model or self.config.model,
                "content": self._build_content(prompt, image_urls, video_urls, audio_urls),
                "ratio": ratio or self.config.default_ratio,
                "duration": duration or self.config.default_duration,
                "generate_audio": generate_audio if generate_audio is not None else self.config.generate_audio,
                "watermark": watermark if watermark is not None else self.config.watermark,
            }
            if resolution or self.config.default_resolution:
                body["resolution"] = resolution or self.config.default_resolution

            async with httpx.AsyncClient(timeout=self.config.timeout_sec) as client:
                task_id = await self._create_task(client, body)
                logger.info("Seedance 任务已创建：{}（model={}）", task_id, body["model"])
                task = await self._poll_until_done(client, task_id)
                video_url = self._extract_video_url(task)
                artifact = await self._download_and_store(client, video_url)

            return json.dumps(
                {
                    "video": artifact,
                    "task_id": task_id,
                    "model": body["model"],
                    "next_step": (
                        "视频已生成并保存到本地。可把 path 作为后续剪辑工具的输入，"
                        "或通过 message 工具把视频文件交付给用户。"
                    ),
                },
                ensure_ascii=False,
            )
        except SeedanceVideoError as exc:
            return f"Error: {exc}"
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            return f"Error: Seedance 请求失败：{exc}"
