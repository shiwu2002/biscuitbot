"""Provider 级语音合成（TTS）适配器。

本模块只负责"如何调用外部 TTS API"（如 OpenAI /audio/speech、Edge TTS）。
产品层的配置兜底、输出路径处理、工具/WebUI 集成等逻辑位于
``biscuitbot.audio.tts``。
"""

import asyncio  # 异步休眠（退避重试）
import os  # 读取环境变量（默认 API Key/端点）
from pathlib import Path  # 跨平台路径处理
from typing import Any  # 动态类型标注

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志

# 语音合成接口的 URL 路径后缀
_SPEECH_PATH = "audio/speech"


class TtsError(RuntimeError):
    """TTS 合成失败（配置缺失 / 请求失败 / 依赖缺失）。"""


def _resolve_speech_url(api_base: str | None, default_url: str) -> str:
    """解析完整的语音合成端点 URL。

    接受两种形式：
    1. 对话风格的 base（如 ``https://api.openai.com/v1``）——
       会自动拼接 ``/audio/speech`` 路径；
    2. 已以 ``/audio/speech`` 结尾的完整 URL——原样返回。
    """
    if not api_base:
        return default_url
    base = api_base.rstrip("/")
    if base.endswith(_SPEECH_PATH):
        return base
    return f"{base}/{_SPEECH_PATH}"


def _rate_to_speed(rate: str | None) -> float:
    """把 "-50%" / "+10%" / "20%" 形式的语速转成 OpenAI speed（0.25~4.0）。"""
    if not rate:
        return 1.0
    value = rate.strip()
    if value.endswith("%"):
        value = value[:-1]
    try:
        num = float(value)
    except ValueError:
        return 1.0
    speed = 1.0 + num / 100.0
    return max(0.25, min(4.0, speed))


# 最多重试 3 次（共 4 次尝试），失败后指数退避。
# TTS 端点在高负载下偶发 502/503，网络不佳时也会遇到零星的连接/读取错误。
# 与转录不同，TTS 失败必须抛错（上层需要给模型可解释的错误信息），
# 因此这里在重试耗尽后抛 TtsError 而非返回空串。
_MAX_RETRIES = 3
_BACKOFF_S = (1.0, 2.0, 4.0)  # 每次重试前的退避秒数
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}  # 可重试的 HTTP 状态码
_RETRYABLE_EXCEPTIONS = (  # 可重试的网络异常类型
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


async def _post_audio_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    api_key: str,
    provider_label: str,
    payload: dict[str, Any],
) -> bytes:
    """POST JSON 并返回音频字节；瞬时错误指数退避重试，其余错误抛 TtsError。"""
    headers = {"Authorization": f"Bearer {api_key}"}
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await client.post(url, headers=headers, json=payload, timeout=120.0)
        except _RETRYABLE_EXCEPTIONS as e:
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "{} TTS transient error (attempt {}/{}): {}",
                    provider_label,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    e,
                )
                await asyncio.sleep(_BACKOFF_S[attempt])
                continue
            raise TtsError(f"{provider_label} TTS 请求失败：{e}") from e

        # 可重试的状态码且仍有重试机会：退避后继续
        if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            logger.warning(
                "{} TTS transient HTTP {} (attempt {}/{})",
                provider_label,
                response.status_code,
                attempt + 1,
                _MAX_RETRIES + 1,
            )
            await asyncio.sleep(_BACKOFF_S[attempt])
            continue

        if response.status_code >= 400:
            body = response.text.strip().replace("\n", " ")[:500]
            raise TtsError(
                f"{provider_label} TTS HTTP {response.status_code}"
                f"{f': {body}' if body else ''}"
            )
        return response.content
    raise TtsError(f"{provider_label} TTS 请求重试耗尽")


def _ensure_output_path(output_path: str | Path) -> Path:
    """规范化输出路径：无扩展名时补 .mp3，并确保父目录存在。"""
    path = Path(output_path)
    if not path.suffix:
        path = path.with_suffix(".mp3")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class OpenAITtsProvider:
    """使用 OpenAI 兼容 /audio/speech 接口的语音合成 Provider。"""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        rate: str | None = None,
        env_key: str = "OPENAI_API_KEY",
    ):
        # API Key 优先用参数传入，其次回退到环境变量
        self.api_key = api_key or os.environ.get(env_key)
        self.api_url = _resolve_speech_url(
            api_base or os.environ.get("OPENAI_TTS_BASE_URL"),
            "https://api.openai.com/v1/audio/speech",
        )
        self.model = model or "gpt-4o-mini-tts"
        self.voice = voice or "alloy"
        self.rate = rate
        logger.debug("OpenAI TTS endpoint: {}", self.api_url)

    async def synthesize(self, text: str, output_path: str | Path) -> str:
        """合成语音并写入 *output_path*，返回实际文件路径。"""
        if not self.api_key:
            raise TtsError("OpenAI API key not configured for TTS")
        if not text.strip():
            raise TtsError("TTS 输入文本为空")
        path = _ensure_output_path(output_path)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "mp3",
            "speed": _rate_to_speed(self.rate),
        }
        async with httpx.AsyncClient() as client:
            content = await _post_audio_with_retry(
                client,
                self.api_url,
                api_key=self.api_key,
                provider_label="OpenAI",
                payload=payload,
            )
        path.write_bytes(content)
        return str(path)


class EdgeTtsProvider:
    """使用微软 Edge 在线 TTS（edge-tts）的免费语音合成 Provider。"""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        rate: str | None = None,
    ):
        self.voice = voice or "zh-CN-XiaoxiaoNeural"
        self.rate = rate or "+0%"

    async def synthesize(self, text: str, output_path: str | Path) -> str:
        """合成语音并写入 *output_path*，返回实际文件路径。"""
        if not text.strip():
            raise TtsError("TTS 输入文本为空")
        try:
            import edge_tts  # 懒导入：edge-tts 是可选依赖
        except ImportError as exc:
            raise TtsError(
                "未安装 edge-tts，请执行 pip install 'biscuitbot[tts]'"
            ) from exc
        path = _ensure_output_path(output_path)
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            await communicate.save(str(path))
        except Exception as exc:
            raise TtsError(f"Edge TTS 合成失败：{exc}") from exc
        return str(path)


def _dashscope_rate(rate: str | None) -> float:
    """把 "+10%" 形式的语速转成 DashScope speech_rate（0.5~2.0 浮点倍率）。"""
    return max(0.5, min(2.0, _rate_to_speed(rate)))


class DashScopeTtsProvider:
    """使用阿里云 DashScope 官方 SDK 的语音合成 Provider（CosyVoice 等）。

    DashScope 的 OpenAI 兼容模式（``compatible-mode/v1``）不提供 ``/audio/speech``，
    因此本适配器改走 DashScope 原生 SDK（内部使用 WebSocket），复用同一个 API Key。
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        rate: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        self.model = model or "cosyvoice-v3-flash"
        self.voice = voice or "longxiaochun_v3"
        self.rate = rate

    async def synthesize(self, text: str, output_path: str | Path) -> str:
        """合成语音并写入 *output_path*，返回实际文件路径。"""
        if not self.api_key:
            raise TtsError("DashScope API key not configured for TTS")
        if not text.strip():
            raise TtsError("TTS 输入文本为空")
        try:
            import dashscope  # 懒导入：dashscope 是可选依赖
            from dashscope.audio.tts_v2 import SpeechSynthesizer
        except ImportError as exc:
            raise TtsError(
                "未安装 dashscope SDK，请执行 pip install 'biscuitbot[tts]'"
            ) from exc

        path = _ensure_output_path(output_path)

        def _call() -> bytes:
            # SDK 从全局 dashscope.api_key 读取密钥，构造参数不接受 api_key。
            dashscope.api_key = self.api_key
            # SDK 要求每次 call 前重建实例；async_call=False 使 call() 阻塞并
            # 返回完整音频字节（否则默认走回调并返回 None）。
            synthesizer = SpeechSynthesizer(
                model=self.model,
                voice=self.voice,
                speech_rate=_dashscope_rate(self.rate),
            )
            synthesizer.async_call = False
            audio = synthesizer.call(text, timeout_millis=120_000)
            if not audio:
                raise TtsError("DashScope TTS 返回空音频")
            return audio

        try:
            audio = await asyncio.to_thread(_call)
        except TtsError:
            raise
        except Exception as exc:
            raise TtsError(f"DashScope TTS 合成失败：{exc}") from exc

        path.write_bytes(audio)
        return str(path)
