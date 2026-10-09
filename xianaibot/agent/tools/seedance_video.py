"""Seedance（火山方舟）视频生成工具。

所属模块与项目作用
===================
本文件位于 xianaibot/agent/tools 目录，是视频生成能力的**火山方舟 Seedance** 组件。
把方舟（Volcengine Ark）Seedance 视频大模型封装为独立的 ``Tool``，让 agent 通过
``generate_video_seedance`` 完成文生视频 / 图生视频 / 参考视频+参考音频编辑，并在
工具内部完成异步任务轮询与视频落盘，返回视频文件路径供后续引用。

厂商自包含架构（方向 B）：可灵走 ``kling_video.py`` 的 ``generate_video_kling``、
MiniMax H3 走 ``minimax_video.py`` 的 ``generate_video_minimax``，三家各自自包含
（可独立阅读、各自独立启用），共享的素材解析与落盘辅助在 ``_video_common.py``。
启用门控：只要「模型厂商」页配置了 volcengine 厂商（或 ARK_API_KEY 环境变量 /
``tools.seedance_video.apiKey`` 显式配置），本工具即可被智能体调用。

相较早期的 ``seedance`` 技能（教 agent 写脚本 + exec），本工具把「创建任务 →
轮询 → 取视频 → 下载」收敛进原生 Python 代码，避免多轮脚本往返与 SDK 环境依赖。
HTTP 直接调用方舟 OpenAI 兼容接口（``httpx``），无需 ``volcengine-python-sdk``。
"""

from __future__ import annotations

import asyncio  # 异步轮询
import json  # 序列化工具返回结果
import os  # 读取 ARK_API_KEY 环境变量
from pathlib import Path  # 路径处理
from typing import Any  # 任意类型

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志
from pydantic import Field  # Pydantic 字段校验

from xianaibot.agent.tools._video_common import (  # 厂商无关共享辅助
    AIGC_CHARACTER_DISCLAIMER,
    VideoToolError,
    VideoVendorSpec,
    download_and_store,
    resolve_audio_ref,
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

# 方舟 Seedance 默认 base URL（OpenAI 兼容，内容生成任务走 /contents/generations/tasks）
_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
# 支持的模型 ID：2.0 / 2.0 fast / 2.0 mini / 2.5（也支持已开通的 Endpoint ID ep-...）
_MODEL_2_0 = "doubao-seedance-2-0-260128"
_MODEL_2_0_FAST = "doubao-seedance-2-0-fast-260128"
_MODEL_2_0_MINI = "doubao-seedance-2-0-mini-260615"
_MODEL_2_5 = "doubao-seedance-2-5-260628"
# 支持的画幅比例
_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")
# 支持的清晰度（480p/720p/1080p 通用，4K 仅 2.5）
_RESOLUTIONS = ("480p", "720p", "1080p", "4K")
# 视频时长边界：2.0 支持 4–15s，2.5 支持到 30s
_MIN_DURATION = 4
_MAX_DURATION = 30


class SeedanceVideoError(VideoToolError):
    """Seedance 视频生成/编辑失败时抛出。"""


class SeedanceVideoToolConfig(Base):
    """Seedance 视频生成工具配置。

    职责：承载 Seedance 视频生成工具的运行时配置项，包括 API Key、模型、
    默认画幅/时长/清晰度、水印与音画设置、轮询参数与 artifact 保存目录。

    厂商自包含重构后本段只服务火山方舟：不再有 ``enabled`` / ``provider``
    字段——启用与否由「模型厂商」页是否配置 volcengine 密钥决定（或本段
    ``api_key`` / 环境变量 ``ARK_API_KEY``）。旧 config.json 残留的这两个键
    会被 Base 忽略，不影响加载。
    """

    api_key: str | None = None  # 显式 API Key；缺省回退「模型厂商」页 volcengine 密钥或环境变量 ARK_API_KEY
    base_url: str = _DEFAULT_BASE_URL  # 方舟 base URL
    model: str = _MODEL_2_0  # 默认模型（2.0），可配成 2.5 或 Endpoint ID
    default_ratio: str = "16:9"  # 默认画幅
    default_duration: int = Field(default=5, ge=_MIN_DURATION, le=_MAX_DURATION)  # 默认时长（秒）
    default_resolution: str | None = None  # 默认清晰度，None 表示交给模型
    generate_audio: bool = True  # 是否默认开启音画同步生成音频（未显式传参时默认开音效）
    seed: int | None = None  # 随机种子（None 表示随机）；固定可复现/微调结果
    watermark: bool = False  # 是否默认添加水印（默认关闭 = 去水印）
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
                "参考图片：本地文件路径（含微信/渠道收到的图片路径，可直接用，无需上传公网）、"
                "可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description=(
                "可选参考图（首帧 / 尾帧 / 参考画面）。本地路径会自动转 base64 上传；"
                "微信/其他渠道收到的参考图本地路径可直接传入，无需先上传到公网图床。"
            ),
        ),
        video_urls=ArraySchema(
            StringSchema("参考视频的可公开访问 HTTP(S) URL。"),
            description="可选参考视频。仅支持公网 HTTP(S) URL，不支持本地文件。",
        ),
        audio_urls=ArraySchema(
            StringSchema(
                "参考音频：本地文件路径、可公开访问的 HTTP(S) URL，或 base64 data URL。"
            ),
            description="可选参考音频（音画同步 / 口型参考等）。本地路径会自动转 base64 上传。",
        ),
        ratio=StringSchema("画幅比例。", enum=_RATIOS),
        duration=IntegerSchema(
            description="视频时长（秒），2.0 支持 4–15，2.5 支持到 30。",
            minimum=_MIN_DURATION,
            maximum=_MAX_DURATION,
        ),
        resolution=StringSchema(
            "清晰度（4K 仅 2.5 支持）。仅文生/图生视频可用；带参考素材（r2v）时模型自动定分辨率，勿传此参数。",
            enum=_RESOLUTIONS,
        ),
        generate_audio=BooleanSchema(
            description=(
                "是否开启音画同步生成音频（默认开启：未传参时自动打开音效，传 false 可关闭）。"
            ),
        ),
        seed=IntegerSchema(
            description="随机种子（-1 或省略为随机）。固定 seed 可让相似输入得到可复现/可微调的结果。",
            minimum=-1,
            maximum=4294967295,
        ),
        watermark=BooleanSchema(description="是否添加水印（默认关闭，即去水印）。"),
        model=StringSchema(
            "可选模型覆盖（默认用配置里的 model，可切到 doubao-seedance-2-0-260128 / "
            "doubao-seedance-2-0-fast-260128 / doubao-seedance-2-0-mini-260615 / "
            "doubao-seedance-2-5-260628 或 Endpoint ID ep-...）。",
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

    _capability = "Generate or edit videos from text/image/video/audio with Seedance (Volcengine Ark, returns file path)."
    _usage_md = "docs/generate_video_seedance.md"  # 工具使用说明文档路径

    config_key = "seedance_video"  # 配置键名

    # 厂商卡片元数据（WebUI「视频生成」页与 /v1/models 由它派生；见 _video_common）。
    # key 是卡片键（seedance），provider 是密钥来源（火山方舟 volcengine），二者刻意不同。
    vendor_spec = VideoVendorSpec(
        key="seedance",
        display_name="Seedance",
        provider="volcengine",
        tool_name="generate_video_seedance",
        config_key="seedance_video",
        support=("t2v", "i2v", "v2v", "ref_image", "ref_audio"),
        ratio_options=_RATIOS,
        resolution_options=_RESOLUTIONS,
        duration_range=(_MIN_DURATION, _MAX_DURATION),
        model_suggestions=(_MODEL_2_5, _MODEL_2_0, _MODEL_2_0_FAST, _MODEL_2_0_MINI),
        resolution_optional=True,
        api_key_env=("ARK_API_KEY",),
    )

    @classmethod
    def config_cls(cls):
        """返回该工具使用的配置类。"""
        return SeedanceVideoToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """「模型厂商」页配置了 volcengine 密钥（或显式 apiKey / ARK_API_KEY）即启用。"""
        config = ctx.config.seedance_video
        if (config.api_key or "").strip():
            return True
        if os.environ.get("ARK_API_KEY", "").strip():
            return True
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("volcengine")
        return bool((getattr(provider_cfg, "api_key", None) or "").strip())

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        # 密钥/地址从统一 providers 配置取 volcengine 厂商的值（模型厂商页配置），
        # 与文生图解耦：厂商密钥统一走「模型厂商」页。
        provider_configs = getattr(ctx, "provider_configs", None) or {}
        provider_cfg = provider_configs.get("volcengine")
        ark_api_key = getattr(provider_cfg, "api_key", None)
        provider_api_base = getattr(provider_cfg, "api_base", None)
        return cls(
            workspace=ctx.workspace,
            config=ctx.config.seedance_video,
            ark_api_key=ark_api_key,
            provider_api_base=provider_api_base,
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: SeedanceVideoToolConfig,
        ark_api_key: str | None = None,
        provider_api_base: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~
        self.config = config  # 工具配置
        self._ark_api_key = ark_api_key  # 模型厂商页配置的厂商密钥
        self.provider_api_base = provider_api_base  # 模型厂商页配置的厂商 apiBase

    @property
    def name(self) -> str:
        """工具名称。"""
        return "generate_video_seedance"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Generate or edit a video with Seedance (Volcengine Ark, 火山方舟). "
            "Accepts text, image, video and audio inputs. Runs asynchronously and returns the "
            "downloaded video file path. Pass local paths or public URLs for image/audio reference "
            "(local files are auto base64-encoded — including images received via chat channels "
            "like WeChat: their local paths work directly, no public upload needed); "
            "for video reference pass a public HTTP(S) URL only. "
            "Other video vendors: use generate_video_kling (可灵) or generate_video_minimax instead. "
            "Sound effects/audio is generated by default (audio on): omit generate_audio to enable it; "
            "pass generate_audio=false to turn it off."
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

    def _api_base(self) -> str:
        """解析方舟 base URL：模型厂商页 volcengine 的 apiBase 优先，其次工具级 baseUrl。"""
        if self.provider_api_base:
            return self.provider_api_base.rstrip("/")
        return self.config.base_url.rstrip("/")

    def _resolve_model(self, model: str | None) -> str:
        """解析 Seedance 模型名：残留其他厂商模型名（旧配置切厂商未切模型）时回退默认。

        旧统一入口时代的 config 可能残留 ``kling-3.0`` / ``MiniMax-H3`` 等模型名，
        直接发给方舟必然报错，统一回退 ``_MODEL_2_0``。
        """
        candidate = (model or "").strip() or (self.config.model or "").strip()
        if not candidate:
            return _MODEL_2_0
        lowered = candidate.lower()
        # 合法 Seedance 模型：doubao-seedance-* 或 Endpoint ID ep-...
        if "seedance" in lowered or candidate.startswith("ep-"):
            return candidate
        return _MODEL_2_0

    def _build_content(
        self,
        prompt: str,
        image_urls: list[str] | None,
        video_urls: list[str] | None,
        audio_urls: list[str] | None,
    ) -> list[dict[str, Any]]:
        """把文本 + 可选参考素材组装为方舟 content 数组。"""
        # 仅有参考图时才前置 AIGC 虚拟角色声明；纯文生视频（t2v，无参考图）
        # 不拼，避免「我上传的图片」这类文案与无图场景不符。
        text = (
            f"{AIGC_CHARACTER_DISCLAIMER}{prompt}"
            if image_urls
            else prompt
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for value in image_urls or []:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": resolve_image_ref(value)},
                    "role": "reference_image",
                }
            )
        for value in video_urls or []:
            content.append(
                {
                    "type": "video_url",
                    "video_url": {"url": resolve_video_ref(value)},
                    "role": "reference_video",
                }
            )
        for value in audio_urls or []:
            content.append(
                {
                    "type": "audio_url",
                    "audio_url": {"url": resolve_audio_ref(value)},
                    "role": "reference_audio",
                }
            )
        return content

    async def _create_task(self, client: httpx.AsyncClient, body: dict[str, Any]) -> str:
        """创建内容生成任务并返回 task id。"""
        url = f"{self._api_base()}/contents/generations/tasks"
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }
        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise SeedanceVideoError(f"创建任务请求失败：{exc}。请检查网络连接或 baseUrl 配置") from exc
        if response.status_code >= 400:
            raise SeedanceVideoError(
                f"创建任务失败（HTTP {response.status_code}）：{response.text[:500]}"
                "。请检查 API key 是否有效、模型是否已开通"
            )
        data = response.json()
        task_id = data.get("id")
        if not task_id:
            raise SeedanceVideoError(f"创建任务未返回 task id：{data}")
        return str(task_id)

    async def _poll_until_done(self, client: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
        """轮询任务直至成功或失败，返回最终任务对象。"""
        url = f"{self._api_base()}/contents/generations/tasks/{task_id}"
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
                raise SeedanceVideoError(f"Seedance 任务失败：{error}（可重试，或检查提示词/参考素材是否合规）")
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
        seed: int | None = None,
        watermark: bool | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """执行 Seedance 视频生成/编辑。

        参数:
            prompt: 文本提示词（必填）。
            image_urls: 可选参考图列表（本地路径 / URL / data URL）。
            video_urls: 可选参考视频列表（仅公网 URL）。
            audio_urls: 可选参考音频列表（本地路径 / URL / data URL）。
            ratio: 画幅比例。
            duration: 时长（秒）。
            resolution: 清晰度。
            generate_audio: 是否音画同步生成音频（默认开启；未传参时自动打开音效，传 false 关闭）。
            seed: 随机种子（None 表示随机）。
            watermark: 是否加水印。
            model: 模型覆盖。

        返回:
            包含视频本地路径与元数据的 JSON 字符串；出错时返回错误说明。
        """
        try:
            resolved_model = self._resolve_model(model)
            # 音效默认开启：未显式传参时自动打开（带参考视频/音频时始终开启）；
            # 显式传入 generate_audio 时以传入值为准（false 可关闭）。
            if generate_audio is None:
                generate_audio = True if (audio_urls or video_urls) else self.config.generate_audio
            body: dict[str, Any] = {
                "model": resolved_model,
                "content": self._build_content(prompt, image_urls, video_urls, audio_urls),
                "ratio": ratio or self.config.default_ratio,
                "duration": duration or self.config.default_duration,
                "generate_audio": generate_audio,
                "watermark": watermark if watermark is not None else self.config.watermark,
            }
            # 随机种子：仅在显式指定时下发，None 交给方舟默认随机
            resolved_seed = seed if seed is not None else self.config.seed
            if resolved_seed is not None:
                body["seed"] = resolved_seed
            # r2v（参考转视频）：带参考图/视频/音频时，模型按参考素材自动推导分辨率，
            # 显式传 resolution 会被方舟拒绝（"not valid ... in r2v"），故在此丢弃。
            is_r2v = bool(image_urls or video_urls or audio_urls)
            if is_r2v:
                if resolution or self.config.default_resolution:
                    logger.info(
                        "Seedance r2v 模式忽略 resolution：{}",
                        resolution or self.config.default_resolution,
                    )
            elif resolution or self.config.default_resolution:
                body["resolution"] = resolution or self.config.default_resolution

            async with httpx.AsyncClient(timeout=self.config.timeout_sec) as client:
                task_id = await self._create_task(client, body)
                logger.info("Seedance 任务已创建：{}（model={}）", task_id, body["model"])
                task = await self._poll_until_done(client, task_id)
                video_url = self._extract_video_url(task)
                artifact = await download_and_store(
                    client,
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
                    "model": body["model"],
                    "next_step": (
                        "视频已生成并保存到本地。可把 path 作为后续剪辑工具的输入，"
                        "或通过 message 工具把视频文件交付给用户。"
                    ),
                },
                ensure_ascii=False,
            )
        except VideoToolError as exc:
            return f"Error: {exc}"
