"""音视频语音转写（ASR）工具。

所属模块与项目作用
===================
本文件位于 xianaibot/agent/tools 目录，是工具系统的语音识别组件。
在项目架构中起到的作用：把「把一段音视频里的说话内容变成文字」封装为统一的
``Tool``，让 agent 在需要时（用户问「这段录音说了什么」）按需调用一次。

**为什么是「按需调用」而不是「上传即转写」**：转写按音频秒数计费，用户上传的
视频多半只是「存个档」，自动转写会让每一次无关上传都产生一次付费调用。因此
上下文层只给模型一段 ``[用户附加音频：<路径>]`` 说明（见
``utils/helpers.py::audio_attachment_note``），由模型自己判断要不要花这笔钱。

**成本口径**：本工具只把**音频**送去 ASR，画面永远不进上下文——这与
「把视频逐帧送模型」相差 2–3 个数量级（10 分钟视频逐帧 ≈ 0.6–1.6M token，
10 分钟音频转写 ≈ 2k token）。详见 docs/transcribe_media.md 的「成本与上限」。
"""

from __future__ import annotations

import json  # 序列化工具返回结果
import re  # language 代码格式校验
from dataclasses import replace  # language 覆盖：就地替换 frozen dataclass
from typing import Any  # 任意类型

from loguru import logger  # 结构化日志

from xianaibot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from xianaibot.agent.tools.filesystem import _FsTool  # 复用工作区边界策略
from xianaibot.agent.tools.schema import (  # schema 构造器
    StringSchema,
    tool_parameters_schema,
)
from xianaibot.audio.transcription import (  # 既有转写服务层（本地路径 → 文本）
    resolve_transcription_config,
    transcribe_audio_file,
)
from xianaibot.security.workspace_policy import WorkspaceBoundaryError  # 路径越界异常
from xianaibot.utils.document import (  # 附件类型判定
    is_audio_file,
    is_remote_url,
    is_video_file,
)

# 与 config.schema.TranscriptionConfig.language 的 pattern 对齐（ISO-639-1/2 小写）
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")

# 错误文案里列给模型看的扩展名（这里只是提示，真正的判定在 is_audio_file/is_video_file）
_SUPPORTED_HINT = "mp3 / m4a / wav / ogg / opus / flac / aac / mp4 / mov / webm 等"

# 上限缺失时的兜底体积（与 audio/transcription.py::_MAX_AUDIO_BYTES_FALLBACK 一致）
_MB = 1024 * 1024


@tool_parameters(
    tool_parameters_schema(
        path=StringSchema(
            "要转写的本地音视频文件路径（必填）。用户附件在消息里以 "
            "[用户附加音频：<路径>] / [用户附加视频：<路径>] 给出，该路径可直接使用。",
            min_length=1,
        ),
        language=StringSchema(
            "音频语言的 ISO-639-1 小写代码（可省略），如 zh / en / ja。"
            "给了能显著减少同音字错字，但如果语言不确定就别猜——猜错比不猜更差。",
        ),
        required=["path"],
    )
)
class TranscribeMediaTool(_FsTool):
    """把本地音视频里的说话内容转写为文本。

    职责：做前置检查（配置、路径边界、类型、体积），再把文件交给既有的
    ``audio.transcription`` 服务链路（OpenAI Whisper 兼容接口）转写，返回
    文字稿。不做本地解码、不抽帧、不接受 URL、不缓存结果。
    """

    _capability = (
        "Transcribe the speech in a local audio or video file with an ASR model "
        "(Whisper-compatible) and return the transcript text."
    )
    _usage_md = "docs/transcribe_media.md"  # 工具使用说明文档路径

    # 由 create() 挂上的根配置（转写配置从顶层 config.transcription 读）
    _root_config: Any = None

    @classmethod
    def _resolve_config(cls, ctx: Any) -> Any:
        """取根配置（root_config），回退到 config（兼容测试/直接构造）。"""
        return getattr(ctx, "root_config", None) or getattr(ctx, "config", None)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """**恒为 True：本工具不按配置门控。**

        上下文层的附件说明会点名 ``transcribe_media``。若因为「没配 Key / 被关闭」
        就把它从工具表里摘掉，模型拿到的是 ``Tool 'transcribe_media' not found``——
        从它的视角不可修复（它改不了 config），结果只会反复重试或对用户含糊其辞。
        未配置时由 :meth:`execute` 返回**可照着改**的中文错误，用户因此能看到
        「这个能力存在、该怎么打开」。``image_generation`` 同此策略。

        注意「不门控」≠「不看配置」：``transcription.enabled=false`` 是用户显式
        关闭，:meth:`execute` 第一步就拦下来。
        """
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例，并挂上根配置。

        路径/沙箱策略刻意**复用** ``_FsTool.create``，不在这里重写：边界规则一旦
        与文件工具分叉，越权风险就出现在分叉的那一侧。
        """
        tool = super().create(ctx)
        if isinstance(tool, TranscribeMediaTool):
            tool._root_config = cls._resolve_config(ctx)
        return tool

    @property
    def name(self) -> str:
        """工具名称。"""
        return "transcribe_media"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Transcribe speech from a local audio or video file into text using an "
            "ASR model. Use it when the user sends a recording/video and asks what "
            "was said, or when you need the spoken content to answer. Billed per "
            "audio second, so call it only when the transcript is actually needed. "
            "Local files only — no URLs, no local decoding, no frame extraction."
        )

    @property
    def read_only(self) -> bool:
        """只读：有网络调用但没有本地副作用，可与其他只读工具并行。"""
        return True

    async def execute(
        self,
        path: str = "",
        language: str | None = None,
        **kwargs: Any,
    ) -> str:
        """转写本地音视频文件中的语音。

        参数:
            path: 本地音视频文件路径（必填）。
            language: 可选的 ISO-639-1 语言代码，覆盖配置里的默认语言。

        返回:
            包含文字稿与元数据的 JSON 字符串；出错时返回 ``Error: 中文说明``。
        """
        # ---- 环境错误优先于参数错误：路径换一个也没用，先把「能不能做」说清 ----
        eff = resolve_transcription_config(self._root_config)

        if not eff.enabled:
            return (
                "Error: 语音转写功能已被关闭（transcription.enabled=false）。"
                "请让用户在「设置 → 语音转写」里打开后再试；模型无法自行开启。"
            )
        if not eff.configured:
            return (
                f"Error: 语音转写未配置 API Key（当前生效的 provider 是 "
                f"'{eff.provider}'）。两条修复路径：①在「模型厂商」页给该厂商填 Key；"
                "②在「设置 → 语音转写」把 provider 换成已配置 Key 的厂商。"
                "注意默认值 'groq' 是历史遗留：它只读 providers.groq.apiKey、"
                "不读环境变量，所以「看起来配了」却仍不可用是常见现象。"
                "请把这段原因转达用户，不要换着路径反复重试。"
            )

        # ---- 参数校验 ----
        raw_path = (path or "").strip()
        if not raw_path:
            return (
                "Error: 缺少 path 参数。请传入要转写的本地音视频文件路径，"
                "例如用户附件说明里的 [用户附加音频：<路径>]。"
            )

        if is_remote_url(raw_path):
            return (
                f"Error: 不支持 http(s) 直链：{raw_path}。本工具只转写本地文件，"
                "服务端不会代你下载（避免引入 SSRF 面）。若确实需要转写直链，"
                "请先用 shell 工具把文件下载进工作区（如 curl -o 到工作区路径），"
                "再传本地路径调用本工具。"
            )

        lang_override: str | None = None
        if language is not None and str(language).strip():
            candidate = str(language).strip().lower()
            if not _LANGUAGE_RE.match(candidate):
                return (
                    f"Error: language 必须是 ISO-639-1 小写代码（如 zh / en / ja），"
                    f"收到 {language!r}。可以省略该参数，让服务端自动识别。"
                )
            lang_override = candidate

        try:
            target = self._resolve(raw_path)
        except WorkspaceBoundaryError as exc:
            return f"Error: 路径越界（{exc}）。工作区边界是硬性安全策略，请改用工作区内的路径"

        if target.is_dir():
            return f"Error: {raw_path} 是目录而不是文件。请传入具体的音频/视频文件路径。"

        if not target.is_file():
            return (
                f"Error: 文件不存在：{raw_path}。"
                "若这是用户附件，直接使用消息里 [用户附加音频：…] / [用户附加视频：…] "
                "给出的路径即可；其他情况先用 list_dir / find_files 定位实际文件。"
            )

        if not (is_audio_file(str(target)) or is_video_file(str(target))):
            return (
                f"Error: 不支持的文件类型：{target.name}。"
                f"本工具只处理音频/视频（{_SUPPORTED_HINT}）。"
                "文档类内容请用 read_file / extract_documents。"
            )

        try:
            size = target.stat().st_size
        except OSError as exc:
            return f"Error: 无法读取文件信息：{target.name}（{exc}）"

        limit_mb = eff.max_upload_mb or 25
        if size > limit_mb * _MB:
            return (
                f"Error: 文件过大：{target.name} 为 {size / _MB:.1f}MB，"
                f"超过转写上限 {limit_mb}MB（配置项 transcription.max_upload_mb）。"
                "本工具不做本地抽取音轨：请按 video-understanding 技能的「只抽音轨」一步，"
                '先用 exec 跑 ffmpeg -y -i "…" -vn -ac 1 -ar 16000 -c:a aac -b:a 32k '
                f"把音频压到 {limit_mb}MB 以内，再用本工具转写那个音频文件。"
                "若本机没有 ffmpeg（先确认一次），请如实告诉用户当前无法转写这个文件，"
                "不要反复重试同一路径。"
            )

        # ---- 调用既有转写链路 ----
        if lang_override is not None:
            eff = replace(eff, language=lang_override)

        logger.info(
            "transcribe_media: path={} bytes={} provider={} model={} language={}",
            target,
            size,
            eff.provider,
            eff.model,
            eff.language,
        )

        try:
            text = await transcribe_audio_file(target, eff)
        except Exception as exc:  # 服务层约定不抛异常，这里只兜底，避免打断 agent 循环
            logger.exception("transcribe_media: unexpected failure: {}", exc)
            return f"Error: 转写调用失败：{exc}。请把失败原因告诉用户，不要用别的工具重试。"

        if not text or not text.strip():
            # 服务层把所有失败折叠成空串（4xx/超时/无人声一律如此），这里给
            # 可诊断的上下文：日志里有状态码与响应体，模型有的可说。
            logger.warning(
                "transcribe_media: empty transcript: path={} bytes={} provider={} model={}",
                target,
                size,
                eff.provider,
                eff.model,
            )
            return (
                f"Error: 转写返回空文本（provider={eff.provider} model={eff.model} "
                f"file={target.name} {size / _MB:.1f}MB）。常见原因：服务端拒绝"
                "（如体积/格式/密钥权限）、请求超时，或音频里确实没有人声。"
                "本次调用的状态码与响应体已写入 xianaibot 日志（搜 'transcription HTTP'），"
                "可据此判断。不要用其他工具对同一文件重复调用；"
                "请把失败原因与文件信息如实告诉用户。"
            )

        return json.dumps(
            {
                "transcription": {
                    "path": str(target),
                    "text": text,
                    "chars": len(text),
                    "provider": eff.provider,
                    "model": eff.model,
                    "language": eff.language,
                    "bytes": size,
                },
                "next_step": (
                    "以上已是完整文字稿，可直接据此回答用户；同一文件不要重复调用本工具。"
                    "需要交付文本文件时，用 write_file 把 text 写入工作区再发送。"
                ),
            },
            ensure_ascii=False,
        )
