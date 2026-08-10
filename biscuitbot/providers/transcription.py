"""Provider 级语音转写适配器。

本模块只负责"如何调用外部转写 API"（如 OpenAI Whisper）。
产品层的配置兜底、WebUI 上传校验、频道集成等逻辑位于 ``biscuitbot.audio.transcription``。
"""

import asyncio  # 异步休眠（退避重试）
import mimetypes  # 根据 MIME 数据库推断音频类型
import os  # 读取环境变量（默认 API Key/端点）
from collections.abc import Callable  # 可调用对象类型标注
from pathlib import Path  # 跨平台路径处理
from typing import Any  # 动态类型标注

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志

# 转写接口的 URL 路径后缀
_TRANSCRIPTIONS_PATH = "audio/transcriptions"
# 扩展名 → MIME 覆盖表：修正 mimetypes 模块对部分音频格式的误判
_AUDIO_MIME_OVERRIDES = {
    ".m4a": "audio/mp4",
    ".mpga": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".weba": "audio/webm",
    ".webm": "audio/webm",
}

def _resolve_transcription_url(api_base: str | None, default_url: str) -> str:
    """解析完整的转写端点 URL。

    接受两种形式：
    1. 对话风格的 base（如 ``https://api.groq.com/openai/v1``）——
       会自动拼接 ``/audio/transcriptions`` 路径；
    2. 已以 ``/audio/transcriptions`` 结尾的完整 URL——原样返回。

    对话风格的 base 是用户从 LLM Provider 配置里直接复制过来的常见形式，
    若不拼接路径直接 POST 会 404（#3637）。
    """
    if not api_base:
        return default_url
    base = api_base.rstrip("/")
    if base.endswith(_TRANSCRIPTIONS_PATH):
        return base
    return f"{base}/{_TRANSCRIPTIONS_PATH}"


def _resolve_api_path(api_base: str | None, default_base: str, path: str) -> str:
    """把 api_base/default_base 与 path 拼成完整请求路径。"""
    base = (api_base or default_base).rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _audio_mime_type(path: Path) -> str:
    """根据文件扩展名推断音频 MIME 类型：优先用覆盖表，其次 mimetypes，最后兜底二进制流。"""
    return (
        _AUDIO_MIME_OVERRIDES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


# 最多重试 3 次（共 4 次尝试），失败后指数退避。
# Whisper 端点在高负载下偶发 502/503，移动网络转写调用方也会遇到零星的连接/读取错误。
# 若不重试，语音消息会静默变成空字符串。
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


async def _request_json_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider_label: str,
    **kwargs: object,
) -> dict[str, Any] | None:
    """带重试的通用 JSON 请求（早期实现，保留以兼容旧调用方）。"""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            request = getattr(client, method.lower(), None)
            if request is None:
                response = await client.request(method, url, **kwargs)
            else:
                response = await request(url, **kwargs)
        except _RETRYABLE_EXCEPTIONS as e:
            if attempt < _MAX_RETRIES:
                logger.warning(
                    "{} transcription transient error (attempt {}/{}): {}",
                    provider_label,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                    e,
                )
                await asyncio.sleep(_BACKOFF_S[attempt])
                continue
            logger.exception(
                "{} transcription error after {} attempts: {}",
                provider_label,
                _MAX_RETRIES + 1,
                e,
            )
            return None
        except Exception as e:
            logger.exception("{} transcription error: {}", provider_label, e)
            return None

        if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            logger.warning(
                "{} transcription transient HTTP {} (attempt {}/{})",
                provider_label,
                response.status_code,
                attempt + 1,
                _MAX_RETRIES + 1,
            )
            await asyncio.sleep(_BACKOFF_S[attempt])
            continue

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            body = response.text.strip().replace("\n", " ")[:500]
            logger.error(
                "{} transcription HTTP {}{}{}",
                provider_label,
                response.status_code,
                f" {response.reason_phrase}" if response.reason_phrase else "",
                f": {body}" if body else "",
            )
            return None
        except Exception as e:
            logger.exception("{} transcription error: {}", provider_label, e)
            return None

        try:
            payload = response.json()
        except Exception as e:
            logger.exception(
                "{} transcription error: malformed response body: {}",
                provider_label,
                e,
            )
            return None
        if not isinstance(payload, dict):
            logger.error(
                "{} transcription error: unexpected response shape: {!r}",
                provider_label,
                type(payload).__name__,
            )
            return None
        return payload
    return None


async def _post_transcription_with_retry(
    url: str,
    *,
    api_key: str | None,
    path: Path,
    model: str,
    provider_label: str,
    language: str | None = None,
) -> str:
    """以 multipart 形式 POST 音频文件进行转写，遇到瞬时错误自动重试。

    - 网络类异常（连接/读取/超时）与 408/429/5xx 响应会重试；
    - 其他错误（如 401/403 等 4xx）立即返回空串——
      这类通常是调用方配置错误，重试只会浪费配额。

    当传入 ``language`` 时，每次请求都会带上 ``language`` 字段
    （每次重试都重建请求字典，保证该字段在重试时仍然存在）。
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.exception("{} transcription error: cannot read audio file: {}", provider_label, e)
        return ""
    headers = {"Authorization": f"Bearer {api_key}"}

    def build_request() -> dict[str, Any]:
        # 每次重试都重新构造，保证文件句柄/字段一致
        files = {
            "file": (path.name, data, _audio_mime_type(path)),
            "model": (None, model),
        }
        if language:
            files["language"] = (None, language)
        return {"url": url, "headers": headers, "files": files, "timeout": 60.0}

    return await _post_with_retry(build_request, provider_label, _text_from_transcription_payload)


async def _post_with_retry(
    build_request: Callable[[], dict[str, Any]],
    provider_label: str,
    extract_text: Callable[[dict[str, Any]], str],
) -> str:
    """通用重试 POST：按 ``build_request`` 构造请求，遇瞬时错误指数退避重试。

    成功时调用 ``extract_text`` 从 JSON 响应中提取文本；
    任何不可重试的错误或重试耗尽后均返回空串（不抛异常），
    以便上层把"转写失败"与"无文本"统一处理为空串。
    """
    async with httpx.AsyncClient() as client:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await client.post(**build_request())
            except _RETRYABLE_EXCEPTIONS as e:
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "{} transcription transient error (attempt {}/{}): {}",
                        provider_label,
                        attempt + 1,
                        _MAX_RETRIES + 1,
                        e,
                    )
                    await asyncio.sleep(_BACKOFF_S[attempt])
                    continue
                logger.exception(
                    "{} transcription error after {} attempts: {}",
                    provider_label,
                    _MAX_RETRIES + 1,
                    e,
                )
                return ""
            except Exception as e:
                logger.exception("{} transcription error: {}", provider_label, e)
                return ""

            # 可重试的状态码且仍有重试机会：退避后继续
            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                logger.warning(
                    "{} transcription transient HTTP {} (attempt {}/{})",
                    provider_label,
                    response.status_code,
                    attempt + 1,
                    _MAX_RETRIES + 1,
                )
                await asyncio.sleep(_BACKOFF_S[attempt])
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                body = response.text.strip().replace("\n", " ")[:500]
                logger.error(
                    "{} transcription HTTP {}{}{}",
                    provider_label,
                    response.status_code,
                    f" {response.reason_phrase}" if response.reason_phrase else "",
                    f": {body}" if body else "",
                )
                return ""
            except Exception as e:
                logger.exception("{} transcription error: {}", provider_label, e)
                return ""

            try:
                payload = response.json()
            except Exception as e:
                logger.exception(
                    "{} transcription error: malformed response body: {}",
                    provider_label,
                    e,
                )
                return ""
            if not isinstance(payload, dict):
                logger.error(
                    "{} transcription error: unexpected response shape: {!r}",
                    provider_label,
                    type(payload).__name__,
                )
                return ""
            return extract_text(payload)
    return ""


def _text_from_transcription_payload(payload: dict[str, Any]) -> str:
    """从 Whisper 风格的响应中提取 ``text`` 字段。"""
    text = payload.get("text")
    return text if isinstance(text, str) else ""


class OpenAITranscriptionProvider:
    """使用 OpenAI Whisper API 的语音转写 Provider。"""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        language: str | None = None,
        model: str | None = None,
    ):
        # API Key 优先用参数传入，其次回退到 OPENAI_API_KEY 环境变量
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_url = _resolve_transcription_url(
            api_base or os.environ.get("OPENAI_TRANSCRIPTION_BASE_URL"),
            "https://api.openai.com/v1/audio/transcriptions",
        )
        self.language = language or None
        self.model = model or "whisper-1"  # 默认使用 whisper-1 模型
        logger.debug("OpenAI transcription endpoint: {}", self.api_url)

    async def transcribe(self, file_path: str | Path) -> str:
        """转写指定音频文件，返回文本；失败或未配置 Key 时返回空串。"""
        if not self.api_key:
            logger.warning("OpenAI API key not configured for transcription")
            return ""
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        return await _post_transcription_with_retry(
            self.api_url,
            api_key=self.api_key,
            path=path,
            model=self.model,
            provider_label="OpenAI",
            language=self.language,
        )



