"""图像生成 Provider 辅助工具。

本模块在 biscuitbot 项目中承担"多模态图像生成"职责：
- 定义统一的图像生成 Provider 抽象基类（``ImageGenerationProvider``）；
- 实现多家图像生成服务（Ollama、Gemini/Imagen、OpenAI、智谱、阿里灵积/万相、AIHubMix、火山方舟/Seedream）的异步客户端；
- 提供图像数据 URL 转换、Provider 注册表、尺寸映射等基础工具。

产品层的配置兜底、WebUI 上传校验、频道集成等逻辑位于 ``biscuitbot.audio`` 等模块，
本模块仅关注"如何调用各家图像生成 API 并把结果统一成 data URL"。
"""

from __future__ import annotations

import asyncio  # 异步事件循环（轮询、并发请求）
import base64  # 二进制数据与 base64 互转（图片传输）
import binascii  # base64 解码异常类型
import os  # 环境变量（ARK_API_KEY 等密钥回退）
import re  # 正则表达式（尺寸、宽高比解析）
from abc import ABC, abstractmethod  # 抽象基类支持
from dataclasses import dataclass  # 不可变数据类（响应结构）
from pathlib import Path  # 跨平台路径处理
from typing import Any  # 动态类型标注
from urllib.parse import urlparse  # URL 解析（提取域名/路径）

import httpx  # 异步 HTTP 客户端
from loguru import logger  # 结构化日志

from biscuitbot.providers.registry import find_by_name  # 按 name 查找 Provider 元数据
from biscuitbot.utils.helpers import detect_image_mime  # 通过魔术字节识别图片 MIME 类型


def extract_domain(url: str) -> str:
    """从 URL 中提取 ``scheme://hostname[:port]``，丢弃路径部分。

    用于把用户填写的完整 API 地址收敛成纯域名，
    例如把 DashScope 兼容模式地址还原为 ``https://dashscope.aliyuncs.com``。

    示例：
        extract_domain("https://dashscope.aliyuncs.com/compatible-mode/v1")
        → "https://dashscope.aliyuncs.com"
        extract_domain("https://api.openai.com/v1")
        → "https://api.openai.com"
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return url.rstrip("/")
    result = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        result += f":{parsed.port}"
    return result

# 图像生成请求默认超时时间（秒），覆盖大多数云端 API
_DEFAULT_TIMEOUT_S = 120.0
# Gemini 图像生成默认超时时间（秒）
_GEMINI_DEFAULT_TIMEOUT_S = 120.0
# Gemini Imagen 模型支持的宽高比集合
_GEMINI_IMAGEN_ASPECT_RATIOS = {"1:1", "9:16", "16:9", "3:4", "4:3"}


def _url_has_path(url: str) -> bool:
    """判断 *url* 在域名之后是否包含非空路径。"""
    parsed = urlparse(url)
    return bool(parsed.path and parsed.path.strip("/"))
# Ollama 默认长边像素，当未指定尺寸时使用
_OLLAMA_DEFAULT_SIDE = 1024
# Ollama 尺寸预设（按长边像素），支持 "1K"/"2K"/"4K" 快捷写法
_OLLAMA_SIZE_PRESETS = {
    "1K": 1024,
    "2K": 2048,
    "4K": 4096,
}
# 匹配显式尺寸 "WIDTHxHEIGHT"（如 "1024x768"），大小写 x 均可
_OLLAMA_EXPLICIT_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")
# 匹配宽高比 "W:H"（如 "16:9"）
_OLLAMA_ASPECT_RATIO_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


class ImageGenerationError(RuntimeError):
    """当图像生成 Provider 无法返回图片时抛出。"""


@dataclass(frozen=True)
class GeneratedImageResponse:
    """Provider 返回的图片列表及可选文本。

    - ``images``：以 data URL 形式表示的图片，可直接嵌入 Markdown 或前端展示；
    - ``content``：Provider 附带的文字说明（部分模型会输出描述）；
    - ``raw``：原始响应体，便于调试或上游扩展使用。
    """

    images: list[str]
    content: str
    raw: dict[str, Any]


def _read_image_b64(path: str | Path) -> tuple[str, str]:
    """读取 ``path`` 指向的图片，返回 ``(mime, base64)``。"""
    p = Path(path).expanduser()
    raw = p.read_bytes()
    mime = detect_image_mime(raw)
    if mime is None:
        raise ImageGenerationError(f"unsupported reference image: {p}")
    return mime, base64.b64encode(raw).decode("ascii")


def image_path_to_data_url(path: str | Path) -> str:
    """把本地图片路径转换为 ``data:<mime>;base64,...`` 形式的 data URL。"""
    mime, encoded = _read_image_b64(path)
    return f"data:{mime};base64,{encoded}"


def image_path_to_inline_data(path: str | Path) -> dict[str, str]:
    """把本地图片路径转换为 Gemini ``inlineData`` 载荷字典。"""
    mime, encoded = _read_image_b64(path)
    return {"mimeType": mime, "data": encoded}


def _b64_image_data_url(value: str) -> str:
    """把裸 base64 字符串转换为带 MIME 的 data URL。

    先解码校验合法性，再用魔术字节识别真实 MIME，
    避免把错误数据透传到上游或前端。
    """
    encoded = "".join(value.split())
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ImageGenerationError("generated image payload was not valid base64") from exc
    mime = detect_image_mime(raw)
    if mime is None:
        raise ImageGenerationError("generated image payload was not a supported image")
    return f"data:{mime};base64,{encoded}"



async def _download_image_data_url(
    client: httpx.AsyncClient,
    url: str,
) -> str:
    """下载远端图片 URL 并重新编码为 data URL。

    部分 Provider（如智谱、阿里万相）只返回临时图片 URL，
    这里统一下载并转成 base64 data URL，避免外链失效。
    """
    response = await client.get(url)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text[:500]
        raise ImageGenerationError(f"failed to download generated image: {detail}") from exc
    raw = response.content
    mime = detect_image_mime(raw)
    if mime is None:
        raise ImageGenerationError("generated image URL did not return a supported image")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


# ---------------------------------------------------------------------------
# Registry —— 图像生成 Provider 注册表
# ---------------------------------------------------------------------------

# 全局图像 Provider 注册表：name → Provider 类。在模块导入期由 register_image_gen_provider 填充。
_IMAGE_GEN_PROVIDERS: dict[str, type[ImageGenerationProvider]] = {}


def register_image_gen_provider(cls: type[ImageGenerationProvider]) -> None:
    """仅在导入期注册一个图像 Provider。

    注册表由模块副作用填充，保证 Provider 发现过程惰性且进程内一致：
    只要该模块被导入，对应的 Provider 即被登记。
    """
    name = cls.provider_name
    if not name:
        raise ValueError(f"{cls.__name__} must set provider_name")
    _IMAGE_GEN_PROVIDERS[name] = cls


def get_image_gen_provider(name: str) -> type[ImageGenerationProvider] | None:
    """按 name 取得已注册的图像 Provider 类，未注册返回 None。"""
    return _IMAGE_GEN_PROVIDERS.get(name)


def image_gen_provider_names() -> tuple[str, ...]:
    """按注册顺序返回所有图像生成 Provider 名称。"""
    return tuple(_IMAGE_GEN_PROVIDERS)


def image_gen_provider_configs(config: Any) -> dict[str, Any]:
    """从全局配置中提取已注册 Provider 对应的配置项。

    仅返回在注册表中存在且配置中确实填写的 Provider，便于上层逐个初始化。

    能力专用厂商（volcengine / gemini / aihubmix 等）不是 ``ProvidersConfig``
    的固定字段，「模型厂商」页首次配置时会把它们挂到 ``providers.model_extra``
    （见 settings_api 的保存逻辑），因此除固定字段外还要回查 ``model_extra``，
    否则这些厂商的密钥取不到，导致文生图/文生视频报「无 api key」。
    """
    providers_cfg = config.providers
    model_extra = providers_cfg.model_extra or {}
    result: dict[str, Any] = {}
    for name in _IMAGE_GEN_PROVIDERS:
        pc = getattr(providers_cfg, name, None)
        if pc is None:
            pc = model_extra.get(name)
        if pc is not None:
            result[name] = pc
    return result


def unified_provider_configs(config: Any) -> dict[str, Any]:
    """从统一 providers 配置提取全部厂商（固定字段 + model_extra 自定义厂商）。

    与 :func:`image_gen_provider_configs` 不同，本函数不按图像能力过滤，而是返回
    providers 下所有已声明的厂商配置，供各能力工具（文生图 / 文生视频 / 视觉 /
    TTS / 转写等）以自身 ``provider`` 字段按厂商名取用密钥、base_url 与模型，
    实现「能力」与「厂商」解耦——同一厂商（如 volcengine）可同时服务多种能力，
    不同能力也能指向不同厂商。
    """
    providers_cfg = config.providers
    result: dict[str, Any] = {}
    for name in type(providers_cfg).model_fields:
        pc = getattr(providers_cfg, name, None)
        if pc is not None:
            result[name] = pc
    for extra_name, pc in (providers_cfg.model_extra or {}).items():
        if pc is not None:
            result[extra_name] = pc
    return result


# ---------------------------------------------------------------------------
# Base class —— 图像生成 Provider 抽象基类
# ---------------------------------------------------------------------------


class ImageGenerationProvider(ABC):
    """图像生成 Provider 客户端的抽象基类。

    各家图像服务（Ollama、Gemini、OpenAI、智谱、阿里灵积、AIHubMix）均继承本类，
    通过实现 :meth:`generate` 提供统一的"提示词 → 图片"调用入口。
    """

    # Provider 唯一标识，子类必须设置（如 "ollama"）
    provider_name: str = ""
    # API Key 缺失时的提示文案
    missing_key_message: str = ""
    # 默认请求超时（秒），子类可覆盖
    default_timeout: float = _DEFAULT_TIMEOUT_S

    def __init__(
        self,
        *,
        api_key: str | None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_base = self._resolve_base_url(api_base)
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self.timeout = timeout if timeout is not None else self.default_timeout
        self._client = client

    def _base_path(self) -> str:
        """返回该 Provider 的默认 URL 路径前缀。

        当调用方只传域名（例如 ``https://api.openai.com``）时，
        :meth:`_resolve_base_url` 会把本路径拼到域名后形成完整 base URL
        （例如 ``https://api.openai.com/v1``）。

        使用非空路径前缀的子类应覆盖此方法。
        """
        return ""

    def _resolve_base_url(self, api_base: str | None) -> str:
        """解析最终的 base URL：优先用调用方传入的地址，其次查注册表，最后用默认值。"""
        if api_base:
            base = api_base.rstrip("/")
            # 调用方只传了域名（没有路径）时，拼接 Provider 默认路径前缀，
            # 例如 "https://api.openai.com" → "https://api.openai.com/v1"
            base_path = self._base_path()
            if base_path and not _url_has_path(base):
                base = f"{base}{base_path}"
            return base
        spec = find_by_name(self.provider_name)
        if spec and spec.default_api_base:
            return spec.default_api_base.rstrip("/")
        return self._default_base_url()

    def _default_base_url(self) -> str:
        """子类可覆盖：当既无 api_base 也无注册表项时的兜底 base URL。"""
        return ""

    @abstractmethod
    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        """生成图片，子类必须实现。

        参数说明：
        - ``prompt``：文本提示词；
        - ``model``：模型名（如 ``dall-e-3``、``wan2.6-t2i``）；
        - ``reference_images``：参考图（部分 Provider 不支持）；
        - ``aspect_ratio``：宽高比（如 ``"16:9"``）；
        - ``image_size``：显式尺寸（如 ``"1024x1024"``）。
        """

    def _require_images(self, images: list[str], data: dict[str, Any]) -> None:
        """校验返回的图片列表非空，否则抛出 :class:`ImageGenerationError`。"""
        if images:
            return
        provider_error = data.get("error") if isinstance(data, dict) else None
        label = self.provider_name
        if provider_error:
            raise ImageGenerationError(f"{label} returned no images: {provider_error}")
        raise ImageGenerationError(f"{label} returned no images for this request")

    async def _http_post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        """统一 HTTP POST 入口：优先复用传入/共享 client，否则临时创建并自动关闭。"""
        if client is not None:
            return await client.post(url, headers=headers, json=body)
        if self._client is not None:
            return await self._client.post(url, headers=headers, json=body)
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            return await c.post(url, headers=headers, json=body)




def _http_error_detail(response: httpx.Response) -> str:
    """从 HTTP 错误响应中提取可读的错误信息。

    优先解析 JSON 中的 ``error.message``，失败则回退到响应文本前 500 字。
    """
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return err.get("message") or str(err)
            if err:
                return str(err)
    except Exception:
        logger.debug("Failed to extract HTTP error detail", exc_info=True)
    return response.text[:500] or "<empty response body>"


def _round_to_multiple(value: float, multiple: int = 8) -> int:
    """把像素值四舍五入到 ``multiple`` 的倍数（Ollama 要求 8 的倍数）。"""
    rounded = int(round(value / multiple) * multiple)
    return max(multiple, rounded)


def _ollama_dimensions(aspect_ratio: str | None, image_size: str | None) -> tuple[int, int]:
    """根据宽高比/尺寸预设，计算 Ollama 的 ``(width, height)``。

    规则：
    1. 若 ``image_size`` 形如 ``1024x768``，直接解析为宽高；
    2. 若 ``image_size`` 是 ``"1K"/"2K"/"4K"``，作为长边像素；
    3. 否则使用默认长边 ``_OLLAMA_DEFAULT_SIDE``；
    4. 再结合 ``aspect_ratio`` 推算短边，并按 8 的倍数取整。
    """
    if image_size:
        size = image_size.strip()
        explicit = _OLLAMA_EXPLICIT_SIZE_RE.fullmatch(size)
        if explicit:
            return int(explicit.group(1)), int(explicit.group(2))
        long_side = _OLLAMA_SIZE_PRESETS.get(size.upper(), _OLLAMA_DEFAULT_SIDE)
    else:
        long_side = _OLLAMA_DEFAULT_SIDE

    if not aspect_ratio:
        return long_side, long_side

    ratio = _OLLAMA_ASPECT_RATIO_RE.fullmatch(aspect_ratio.strip())
    if ratio is None:
        return long_side, long_side

    width_ratio = int(ratio.group(1))
    height_ratio = int(ratio.group(2))
    if width_ratio <= 0 or height_ratio <= 0:
        return long_side, long_side

    if width_ratio >= height_ratio:
        # 横向：宽为长边，高按比例缩放
        width = long_side
        height = _round_to_multiple(long_side * height_ratio / width_ratio)
    else:
        # 纵向：高为长边，宽按比例缩放
        height = long_side
        width = _round_to_multiple(long_side * width_ratio / height_ratio)
    return max(8, width), max(8, height)


def _ollama_image_data_url(value: str) -> str:
    """把 Ollama 返回的图片字段统一成 data URL：已是 data URL 则原样返回，否则按 base64 转换。"""
    if value.startswith("data:image/"):
        return value
    return _b64_image_data_url(value)


def _ollama_images_from_payload(payload: dict[str, Any]) -> list[str]:
    """从 Ollama 响应体中递归收集图片字段（兼容 ``image`` 和 ``images`` 两种结构）。"""
    images: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str) and value:
            images.append(_ollama_image_data_url(value))
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload.get("image"))
    collect(payload.get("images"))
    return images


class OllamaImageGenerationClient(ImageGenerationProvider):
    """Ollama 原生图像生成模型的异步客户端。"""

    provider_name = "ollama"
    default_timeout = 300.0

    def _default_base_url(self) -> str:
        return "http://localhost:11434/api"

    def _base_path(self) -> str:
        return "/api"

    def _resolve_base_url(self, api_base: str | None) -> str:
        """覆盖基类：把 OpenAI 风格的 ``/v1`` 自动改写成 Ollama 的 ``/api``。"""
        if api_base:
            base = api_base.rstrip("/")
            if base.endswith("/v1"):
                return f"{base[:-3]}/api"
            if not _url_has_path(base):
                return f"{base}/api"
            return base
        return self._default_base_url()

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if reference_images:
            raise ImageGenerationError(
                "Ollama image generation does not support reference images"
            )

        width, height = _ollama_dimensions(aspect_ratio, image_size)
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "steps": 0,  # steps=0 表示让模型自行决定采样步数
        }
        body.update(self.extra_body)
        body["stream"] = False  # 关闭流式，一次性返回结果

        headers = {
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.api_base}/generate"
        response = await self._http_post(url, headers=headers, body=body)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _http_error_detail(response)
            logger.error(
                "Ollama image generation failed (HTTP {}): {}",
                response.status_code,
                detail,
            )
            raise ImageGenerationError(
                f"Ollama image generation failed (HTTP {response.status_code}): {detail}"
            ) from exc

        data = response.json()
        images = _ollama_images_from_payload(data)

        self._require_images(images, data)

        response_text = data.get("response")
        content = response_text if isinstance(response_text, str) else ""

        return GeneratedImageResponse(images=images, content=content, raw=data)


class GeminiImageGenerationClient(ImageGenerationProvider):
    """通过 Generative Language API 调用 Gemini/Imagen 的异步客户端。"""

    provider_name = "gemini"
    missing_key_message = (
        "Gemini API key is not configured. Set providers.gemini.apiKey."
    )
    default_timeout = _GEMINI_DEFAULT_TIMEOUT_S

    def _default_base_url(self) -> str:
        return "https://generativelanguage.googleapis.com/v1beta"

    def _base_path(self) -> str:
        return "/v1beta"

    def _resolve_base_url(self, api_base: str | None) -> str:
        # Gemini 的对话补全走注册表里的 OpenAI 兼容 shim；
        # 但图像生成必须直连原生 Generative Language API，因此这里刻意跳过注册表查找。
        if api_base:
            base = api_base.rstrip("/")
            if not _url_has_path(base):
                return f"{base}/v1beta"
            return base
        return self._default_base_url()

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)
        # 根据模型名分流：Imagen 走 predict 接口，Gemini Flash 走 generateContent
        if "imagen" in model.lower():
            if reference_images:
                logger.warning(
                    "Imagen models do not support reference images; "
                    "ignoring {} reference image(s) for {}",
                    len(reference_images),
                    model,
                )
            return await self._generate_imagen(
                prompt=prompt, model=model, aspect_ratio=aspect_ratio
            )
        return await self._generate_gemini_flash(
            prompt=prompt, model=model, reference_images=reference_images or []
        )

    async def _generate_imagen(
        self,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str | None,
    ) -> GeneratedImageResponse:
        """调用 Imagen 的 ``:predict`` 接口生成图片。"""
        parameters: dict[str, Any] = {"sampleCount": 1}
        if aspect_ratio in _GEMINI_IMAGEN_ASPECT_RATIOS:
            parameters["aspectRatio"] = aspect_ratio
        body: dict[str, Any] = {
            "instances": [{"prompt": prompt}],
            "parameters": parameters,
        }
        body.update(self.extra_body)

        url = f"{self.api_base}/models/{model}:predict"
        headers = {
            "x-goog-api-key": self.api_key or "",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        response = await self._http_post(url, headers=headers, body=body)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _http_error_detail(response)
            logger.error("Gemini Imagen generation failed (HTTP {}): {}", response.status_code, detail)
            raise ImageGenerationError(
                f"Gemini Imagen generation failed (HTTP {response.status_code}): {detail}"
            ) from exc

        data = response.json()
        images: list[str] = []
        # Imagen 响应：predictions[].bytesBase64Encoded + mimeType
        for prediction in data.get("predictions") or []:
            if not isinstance(prediction, dict):
                continue
            b64 = prediction.get("bytesBase64Encoded")
            mime = prediction.get("mimeType", "image/png")
            if isinstance(b64, str) and b64:
                images.append(f"data:{mime};base64,{b64}")

        self._require_images(images, data)

        return GeneratedImageResponse(images=images, content="", raw=data)

    async def _generate_gemini_flash(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str],
    ) -> GeneratedImageResponse:
        """调用 Gemini Flash 的 ``:generateContent`` 接口生成图片（支持参考图）。"""
        parts: list[dict[str, Any]] = [
            {"inlineData": image_path_to_inline_data(path)} for path in reference_images
        ]
        parts.append({"text": prompt})

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            # 要求同时返回文本与图像
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        body.update(self.extra_body)

        url = f"{self.api_base}/models/{model}:generateContent"
        headers = {
            "x-goog-api-key": self.api_key or "",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        response = await self._http_post(url, headers=headers, body=body)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _http_error_detail(response)
            logger.error("Gemini image generation failed (HTTP {}): {}", response.status_code, detail)
            raise ImageGenerationError(
                f"Gemini image generation failed (HTTP {response.status_code}): {detail}"
            ) from exc

        data = response.json()
        images: list[str] = []
        text_parts: list[str] = []
        # Gemini 响应：candidates[].content.parts[]，part 可能是 text 或 inlineData
        for candidate in data.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if "text" in part:
                    text_parts.append(part["text"])
                inline = part.get("inlineData")
                if isinstance(inline, dict):
                    mime = inline.get("mimeType", "image/png")
                    b64 = inline.get("data", "")
                    if b64:
                        images.append(f"data:{mime};base64,{b64}")

        self._require_images(images, data)

        return GeneratedImageResponse(
            images=images,
            content="\n".join(t for t in text_parts if t).strip(),
            raw=data,
        )




# ---------------------------------------------------------------------------
# OpenAI image generation —— OpenAI Images API（DALL-E、GPT-Image）
# ---------------------------------------------------------------------------

# DALL-E 2 支持的显式尺寸集合
_OPENAI_DALLE2_SUPPORTED_SIZES = {"256x256", "512x512", "1024x1024"}
# DALL-E 3 支持的显式尺寸集合
_OPENAI_DALLE3_SUPPORTED_SIZES = {"1024x1024", "1792x1024", "1024x1792"}
# GPT-Image 系列支持的显式尺寸集合（含 "auto"）
_OPENAI_GPT_IMAGE_SUPPORTED_SIZES = {
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "auto",
}
# DALL-E 2 宽高比 → 尺寸映射（DALL-E 2 仅支持 1:1，其余统一回退到 1024x1024）
_OPENAI_DALLE2_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1024x1024",
    "9:16": "1024x1024",
    "3:4": "1024x1024",
    "4:3": "1024x1024",
}
# DALL-E 3 宽高比 → 尺寸映射
_OPENAI_DALLE3_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "3:4": "1024x1792",
    "4:3": "1792x1024",
}
# GPT-Image 宽高比 → 尺寸映射
_OPENAI_GPT_IMAGE_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
}


class OpenAIImageGenerationClient(ImageGenerationProvider):
    """使用 API Key 调用 OpenAI Images API 的客户端（``providers.openai.apiKey``）。"""

    provider_name = "openai"
    missing_key_message = (
        "OpenAI API key is not configured. Set providers.openai.apiKey."
    )

    def _default_base_url(self) -> str:
        return "https://api.openai.com/v1"

    def _base_path(self) -> str:
        return "/v1"

    @staticmethod
    def _strip_model_prefix(model: str) -> str:
        """去掉 ``openai/`` 前缀（OpenRouter 约定，便于复用同一模型名）。"""
        if model.startswith("openai/"):
            return model.split("/", 1)[1]
        return model

    def _size_for(
        self,
        model: str,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> str:
        """把宽高比/显式尺寸解析为 Images API 的 size 字符串。

        默认沿用 OpenAI 各模型家族的尺寸规则；子类可覆盖（如火山 Seedream
        有自己的一套宽高比→像素尺寸映射）。
        """
        return _openai_size(model, aspect_ratio, image_size)

    def _reference_images_body(
        self,
        reference_images: list[str] | None,
        model: str,
    ) -> Any:
        """把参考图转换为请求载荷字段值；默认不支持参考图，仅告警后忽略。

        返回值会写入 ``body["image"]``（如火山 Seedream 传 data URL 字符串
        或数组），返回 ``None`` 表示该模型不支持参考图。
        """
        if reference_images:
            logger.warning(
                "{} does not support reference images; "
                "ignoring {} reference image(s)",
                model,
                len(reference_images),
            )
        return None

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        clean_model = self._strip_model_prefix(model)
        body: dict[str, Any] = {
            "model": clean_model,
            "prompt": prompt,
        }

        # GPT-Image 系列不支持 response_format/n，仅旧版 DALL-E 需要
        if not _openai_is_gpt_image_model(clean_model):
            body["response_format"] = "b64_json"
            body["n"] = 1

        size = self._size_for(clean_model, aspect_ratio, image_size)
        if size:
            body["size"] = size

        # 参考图（图生图）：默认不支持返回 None；支持的子类返回 image 字段值
        reference_payload = self._reference_images_body(reference_images, clean_model)
        if reference_payload is not None:
            body["image"] = reference_payload

        body.update(self.extra_body)
        # 剔除值为 None 的字段，便于 extraBody 主动关闭默认参数（如 response_format）
        body = {key: value for key, value in body.items() if value is not None}

        logger.info("OpenAI Images API request: POST {}/images/generations body={}", self.api_base, body)

        response = await self._http_post(
            f"{self.api_base}/images/generations",
            headers=headers,
            body=body,
        )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:1000]
            logger.error("OpenAI Images API error ({}): {}", response.status_code, detail)
            raise ImageGenerationError(
                f"OpenAI image generation failed (HTTP {response.status_code}): {detail}"
            ) from exc

        payload = response.json()
        # 日志中刻意剔除 data 字段（体积大），只打印元信息
        logger.info("OpenAI Images API response ({}): {}", response.status_code,
                       {k: v for k, v in payload.items() if k != "data"})

        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=self.timeout)
        try:
            images = await _openai_images_from_payload(client, payload)
        finally:
            if owns_client:
                await client.aclose()

        self._require_images(images, payload)

        return GeneratedImageResponse(images=images, content="", raw=payload)



def _openai_size(
    model: str,
    aspect_ratio: str | None,
    image_size: str | None,
) -> str:
    """把宽高比/显式尺寸解析为 OpenAI Images API 的 size 字符串。"""
    sizes, supported_sizes = _openai_size_options(model)
    explicit_size = _normalize_openai_image_size(image_size)
    if explicit_size and _openai_explicit_size_supported(
        explicit_size,
        supported_sizes=supported_sizes,
    ):
        return explicit_size
    if explicit_size:
        logger.warning(
            "OpenAI image size '{}' is not supported by {}; using aspect ratio/default size",
            explicit_size,
            model,
        )
    if aspect_ratio and aspect_ratio in sizes:
        return sizes[aspect_ratio]
    return "1024x1024"


def _openai_is_gpt_image_model(model: str) -> bool:
    """判断是否为 GPT-Image 系列（gpt-image-*、chatgpt-image-*）。"""
    normalized = model.lower()
    return normalized.startswith(("gpt-image", "chatgpt-image"))


def _openai_size_options(model: str) -> tuple[dict[str, str], set[str] | None]:
    """根据模型名返回 (宽高比映射, 支持的显式尺寸集合)。

    返回的 ``supported_sizes`` 为 ``None`` 表示接受任意 ``WIDTHxHEIGHT``。
    """
    normalized = model.lower()
    if normalized.startswith("dall-e-2"):
        return _OPENAI_DALLE2_ASPECT_RATIO_SIZES, _OPENAI_DALLE2_SUPPORTED_SIZES
    if normalized.startswith("dall-e-3"):
        return _OPENAI_DALLE3_ASPECT_RATIO_SIZES, _OPENAI_DALLE3_SUPPORTED_SIZES
    if normalized.startswith("gpt-image-2"):
        return _OPENAI_GPT_IMAGE_ASPECT_RATIO_SIZES, None
    return _OPENAI_GPT_IMAGE_ASPECT_RATIO_SIZES, _OPENAI_GPT_IMAGE_SUPPORTED_SIZES


def _normalize_openai_image_size(image_size: str | None) -> str | None:
    """规范化显式尺寸：去空白并转小写。"""
    if not image_size:
        return None
    normalized = image_size.strip().lower()
    return normalized or None


def _openai_explicit_size_supported(
    size: str,
    *,
    supported_sizes: set[str] | None,
) -> bool:
    """判断显式尺寸是否被当前模型支持。

    ``supported_sizes`` 为 ``None`` 时，只要形如 ``WIDTHxHEIGHT`` 即视为支持。
    """
    if supported_sizes is not None:
        return size in supported_sizes
    width, sep, height = size.partition("x")
    return bool(sep and width.isdecimal() and height.isdecimal())


async def _openai_images_from_payload(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
) -> list[str]:
    """从 OpenAI Images API 响应中提取图片。

    优先取 ``b64_json``（直接 base64），否则下载 ``url`` 并转成 data URL。
    """
    images: list[str] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            images.append(_b64_image_data_url(b64))
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            images.append(await _download_image_data_url(client, url))
    return images



# ---------------------------------------------------------------------------
# Zhipu (智谱) image generation —— 智谱 CogView/GLM-Image
# ---------------------------------------------------------------------------

# 智谱图像生成默认超时（秒），其图像生成通常较慢
_ZHIPU_TIMEOUT_S = 300.0

# 智谱 glm-image 宽高比 → 尺寸映射
_ZHIPU_ASPECT_RATIO_SIZES = {
    "1:1": "1280x1280",
    "16:9": "1728x960",
    "9:16": "960x1728",
    "3:4": "1088x1472",
    "4:3": "1472x1088",
}


class ZhipuImageGenerationClient(ImageGenerationProvider):
    """智谱（BigModel）图像生成 API 的异步客户端。

    支持：
    - 通过 glm-image、cogview-4、cogview-3-flash 等模型文生图；
    - 宽高比选择；
    - 水印控制（通过 extraBody 透传）。
    """

    provider_name = "zhipu"
    missing_key_message = "Zhipu API key is not configured. Set providers.zhipu.apiKey."
    default_timeout = _ZHIPU_TIMEOUT_S

    def _default_base_url(self) -> str:
        return "https://open.bigmodel.cn/api/paas/v4"

    def _base_path(self) -> str:
        return "/api/paas/v4"

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)

        if reference_images:
            raise ImageGenerationError(
                "Zhipu image generation does not support reference images"
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
        }

        size = _zhipu_size(aspect_ratio, image_size)
        if size:
            body["size"] = size

        body.update(self.extra_body)

        url = f"{self.api_base}/images/generations"

        # 复用共享 client 或临时创建一个，并在结束时关闭临时 client
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            return await self._generate_with_client(
                client,
                headers=headers,
                body=body,
                url=url,
            )
        finally:
            if self._client is None:
                await client.aclose()

    async def _generate_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        headers: dict[str, str],
        body: dict[str, Any],
        url: str,
    ) -> GeneratedImageResponse:
        """使用给定 client 执行智谱图像生成请求并解析响应。"""
        try:
            response = await self._http_post(url, headers=headers, body=body, client=client)
        except httpx.TimeoutException as exc:
            raise ImageGenerationError("Zhipu image generation timed out") from exc
        except httpx.RequestError as exc:
            raise ImageGenerationError(f"Zhipu image generation request failed: {exc}") from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:500]
            raise ImageGenerationError(f"Zhipu image generation failed: {detail}") from exc

        payload = response.json()
        images = await _zhipu_images_from_payload(client, payload)

        self._require_images(images, payload)

        return GeneratedImageResponse(images=images, content="", raw=payload)


def _zhipu_size(
    aspect_ratio: str | None,
    image_size: str | None,
) -> str:
    """把宽高比/显式尺寸解析为智谱 size 字符串。

    智谱 glm-image 支持：1280x1280（默认）、1568x1056、1056x1568、
    1472x1088、1088x1472、1728x960、960x1728。
    """
    if image_size and "x" in image_size.lower():
        return image_size
    if aspect_ratio and aspect_ratio in _ZHIPU_ASPECT_RATIO_SIZES:
        return _ZHIPU_ASPECT_RATIO_SIZES[aspect_ratio]
    return "1280x1280"


async def _zhipu_images_from_payload(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
) -> list[str]:
    """从智谱响应中提取图片 data URL。

    智谱返回的是 30 天有效的临时 URL，这里统一下载并重新编码为 base64 data URL。
    """
    images: list[str] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            images.append(await _download_image_data_url(client, url))
    return images


# ---------------------------------------------------------------------------
# DashScope (阿里灵积/万相) image generation —— 阿里通义万相
# ---------------------------------------------------------------------------

# DashScope 宽高比 → 尺寸映射（注意用星号 ``*`` 分隔，非 ``x``）
_DASHSCOPE_ASPECT_RATIO_SIZES = {
    "1:1": "1024*1024",
    "16:9": "1696*960",
    "9:16": "960*1696",
    "3:4": "1104*1472",
    "4:3": "1472*1104",
}


def _dashscope_size(
    aspect_ratio: str | None,
    image_size: str | None,
) -> str:
    """把宽高比/显式尺寸解析为 DashScope size 字符串。

    DashScope 使用 ``WIDTH*HEIGHT`` 格式（星号分隔，非 ``x``）。
    """
    if image_size:
        size = image_size.strip().lower().replace("x", "*")
        if "*" in size:
            return size
    if aspect_ratio and aspect_ratio in _DASHSCOPE_ASPECT_RATIO_SIZES:
        return _DASHSCOPE_ASPECT_RATIO_SIZES[aspect_ratio]
    return "1024*1024"


# DashScope 默认域名
_DASHSCOPE_DEFAULT_API_BASE = "https://dashscope.aliyuncs.com"
# 旧版万相（wanx）异步任务提交路径（prompt 格式）
_DASHSCOPE_SUBMIT_PATH_OLD = "/api/v1/services/aigc/text2image/image-synthesis"
# 新版万相2.x/千问异步任务提交路径（messages 格式）
_DASHSCOPE_SUBMIT_PATH_NEW = "/api/v1/services/aigc/multimodal-generation/generation"
# 异步任务结果轮询路径
_DASHSCOPE_TASK_PATH = "/api/v1/tasks"
# 轮询间隔（秒）
_DASHSCOPE_POLL_INTERVAL_S = 2.0
# 最大轮询次数（约 2 分钟）
_DASHSCOPE_MAX_POLL_ATTEMPTS = 60

# 使用新版 messages 格式 API 的模型（wan2.6+、qwen-image*）
_DASHSCOPE_NEW_MODELS = frozenset({
    "wan2.6-t2i",
    "wan2.6-image",
    "wan2.5-t2i-preview",
})

# 支持同步直接返回结果的模型（无需轮询）：
# qwen-image 与 wan2.6 系列会在 POST 响应中直接返回图片 URL
_DASHSCOPE_SYNC_MODELS = frozenset({
    "qwen-image-2.0-pro",
    "qwen-image-2.0",
    "qwen-image-plus",
    "qwen-image-max",
    "qwen-image-edit",
    "wan2.6-t2i",
    "wan2.6-image",
})


def _dashscope_model_mode(model: str) -> str:
    """判断 DashScope 图像模型的 API 模式。

    返回值：
        - ``"sync"``：qwen-image / wan2.6 直接同步返回（千问/万相2.6同步模式）；
        - ``"async_new"``：新版 wan2.x 走 messages 格式（万相V2异步模式）；
        - ``"async_old"``：旧版 wanx 走 prompt 格式（万相V1异步模式）。
    """
    if model in _DASHSCOPE_SYNC_MODELS or model.startswith("qwen-image"):
        return "sync"
    if model.startswith("wan2."):
        return "async_new"
    return "async_old"


class DashScopeImageGenerationClient(ImageGenerationProvider):
    """阿里灵积 DashScope 图像生成 API 的异步客户端。

    支持两种文生图模式：

    - **千问模式 (qwen-image)**：同步响应。
      模型：qwen-image-2.0-pro、qwen-image-plus、qwen-image-max 等。
      走 ``/api/v1/services/aigc/image-generation/generation``，
      使用 ``input.messages`` 格式，图片 URL 直接在 POST 响应中的
      ``output.choices[].message.content[].image`` 返回。

    - **万相模式 (wanx / wan2.x)**：异步任务式。
      模型：wanx2.1-t2i-turbo、wanx2.1-t2i-plus、wanx-v1、wan2.6-t2i 等。
      提交任务（带 ``X-DashScope-Async: enable``），随后轮询
      ``/api/v1/tasks/{task_id}`` 直到结果就绪。
      - 新版 wan2.x 使用 ``input.messages`` 格式；
      - 旧版 wanx 使用 ``input.prompt`` 格式。
    """

    provider_name = "dashscope"
    missing_key_message = "DashScope API key is not configured. Set providers.dashscope.apiKey."
    default_timeout = 300.0

    def _default_base_url(self) -> str:
        return _DASHSCOPE_DEFAULT_API_BASE

    def _base_path(self) -> str:
        # DashScope 图像路径在请求时按模型模式动态决定，而非构造期。
        # 这里返回空串，保证裸域名（如 "https://dashscope.aliyuncs.com"）原样保留。
        return ""

    def _resolve_base_url(self, api_base: str | None) -> str:
        """覆盖基类：跳过 LLM 注册表里的 default_api_base。

        DashScope 图像生成 API 用的基础路径（``/api/v1/...``）与
        LLM 兼容模式端点（``/compatible-mode/v1``）不同，
        因此不能回退到注册表的 ``default_api_base``。

        若用户传入的 api_base 已包含 DashScope 已知路径
        （如完整端点 URL），则剥离该路径只保留域名——
        最终端点路径始终由模型模式决定。
        """
        if api_base:
            base = api_base.rstrip("/")
            # 剥离 DashScope 已知路径，保证 api_base 只剩域名
            for suffix in (
                _DASHSCOPE_SUBMIT_PATH_NEW,
                _DASHSCOPE_SUBMIT_PATH_OLD,
                "/compatible-mode/v1",
                "/api/v1",
            ):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            result = base.rstrip("/") or self._default_base_url()
            logger.debug("DashScope _resolve_base_url: input={}, result={}", api_base, result)
            return result
        result = self._default_base_url()
        logger.debug("DashScope _resolve_base_url: input=None, result={}", result)
        return result

    async def generate(
        self,
        *,
        prompt: str,
        model: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> GeneratedImageResponse:
        if not self.api_key:
            raise ImageGenerationError(self.missing_key_message)

        if reference_images:
            raise ImageGenerationError(
                "DashScope image generation does not support reference images"
            )

        size = _dashscope_size(aspect_ratio, image_size)
        mode = _dashscope_model_mode(model)

        # 根据模型模式构造请求体
        if mode == "async_old":
            # 旧版万相：prompt 格式
            body: dict[str, Any] = {
                "model": model,
                "input": {
                    "prompt": prompt,
                },
                "parameters": {
                    "n": 1,
                    "size": size,
                },
            }
            submit_path = _DASHSCOPE_SUBMIT_PATH_OLD
        else:
            # 同步千问 与 新版万相2.x 均使用 messages 格式
            body = {
                "model": model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"text": prompt}],
                        }
                    ],
                },
                "parameters": {
                    "n": 1,
                    "size": size,
                },
            }
            submit_path = _DASHSCOPE_SUBMIT_PATH_NEW

        body.update(self.extra_body)

        # 同步模式不需要 async 头
        is_sync = mode == "sync"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **({"X-DashScope-Async": "enable"} if not is_sync else {}),
            **self.extra_headers,
        }

        submit_url = f"{self.api_base}{submit_path}"
        logger.info("DashScope image generation: mode={}, model={}, url={}", mode, model, submit_url)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # 提交请求
            try:
                submit_resp = await client.post(submit_url, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                raise ImageGenerationError("DashScope image generation request timed out") from exc
            except httpx.RequestError as exc:
                raise ImageGenerationError(f"DashScope image generation request failed: {exc}") from exc

            try:
                submit_resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = submit_resp.text[:500]
                raise ImageGenerationError(
                    f"DashScope image generation failed (HTTP {submit_resp.status_code}): {detail}"
                ) from exc

            submit_data = submit_resp.json()

            # --- 同步模式：图片 URL 直接在响应中 ---
            if is_sync:
                return await self._handle_sync_response(client, submit_data)

            # --- 异步模式：轮询获取结果 ---
            task_id = (submit_data.get("output") or {}).get("task_id")
            if not task_id:
                err_msg = (submit_data.get("output") or {}).get("message") or submit_data.get("message") or "no task_id returned"
                raise ImageGenerationError(f"DashScope image generation submit failed: {err_msg}")

            return await self._poll_until_done(client, task_id)

    async def _handle_sync_response(
        self,
        client: httpx.AsyncClient,
        data: dict[str, Any],
    ) -> GeneratedImageResponse:
        """解析同步 DashScope 响应（千问模式）。"""
        output = data.get("output") or {}
        images: list[str] = []

        choices = output.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                content = (choice.get("message") or {}).get("content") or []
                for item in content:
                    url = item.get("image")
                    if isinstance(url, str) and url:
                        images.append(await _download_image_data_url(client, url))

        self._require_images(images, data)
        return GeneratedImageResponse(images=images, content="", raw=data)

    async def _poll_until_done(
        self,
        client: httpx.AsyncClient,
        task_id: str,
    ) -> GeneratedImageResponse:
        """轮询 DashScope 异步任务直到完成（万相模式）。"""
        poll_url = f"{self.api_base}{_DASHSCOPE_TASK_PATH}/{task_id}"
        poll_headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        for attempt in range(_DASHSCOPE_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(_DASHSCOPE_POLL_INTERVAL_S)

            try:
                poll_resp = await client.get(poll_url, headers=poll_headers)
            except httpx.RequestError as exc:
                # 轮询网络错误不致命，记录后继续重试
                logger.warning("DashScope poll error (attempt {}): {}", attempt + 1, exc)
                continue

            try:
                poll_resp.raise_for_status()
            except httpx.HTTPStatusError:
                logger.warning("DashScope poll HTTP {} (attempt {})", poll_resp.status_code, attempt + 1)
                continue

            poll_data = poll_resp.json()
            output = poll_data.get("output") or {}
            task_status = output.get("task_status", "")

            if task_status == "SUCCEEDED":
                images: list[str] = []

                # 新版模型：output.choices[].message.content[].image
                choices = output.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        content = (choice.get("message") or {}).get("content") or []
                        for item in content:
                            url = item.get("image")
                            if isinstance(url, str) and url:
                                images.append(await _download_image_data_url(client, url))

                # 旧版模型：output.results[].url
                results = output.get("results")
                if isinstance(results, list):
                    for result in results:
                        url = result.get("url")
                        if isinstance(url, str) and url:
                            images.append(await _download_image_data_url(client, url))

                self._require_images(images, poll_data)
                return GeneratedImageResponse(images=images, content="", raw=poll_data)

            if task_status in ("FAILED", "UNKNOWN"):
                err_msg = output.get("message") or "task failed"
                err_code = output.get("code") or ""
                raise ImageGenerationError(
                    f"DashScope image generation failed: {err_code} {err_msg}".strip()
                )

            # 仍处于 PENDING / RUNNING，继续轮询
            if task_status not in ("PENDING", "RUNNING"):
                logger.warning("DashScope unknown task status: {}", task_status)

        raise ImageGenerationError(
            f"DashScope image generation timed out after {_DASHSCOPE_MAX_POLL_ATTEMPTS} polls"
        )


# ---------------------------------------------------------------------------
# AIHubMix image generation —— OpenAI 兼容网关（复用 OpenAI 客户端逻辑）
# ---------------------------------------------------------------------------


class AIHubMixImageGenerationClient(OpenAIImageGenerationClient):
    """AIHubMix 图像生成客户端（复用 OpenAI 兼容的 Images API）。"""

    provider_name = "aihubmix"
    missing_key_message = (
        "AIHubMix API key is not configured. Set providers.aihubmix.apiKey."
    )

    def _default_base_url(self) -> str:
        return "https://aihubmix.com/v1"

    def _base_path(self) -> str:
        return "/v1"

    @staticmethod
    def _strip_model_prefix(model: str) -> str:
        """去掉已知 Provider 前缀（openai/、aihubmix/）。"""
        for prefix in ("openai/", "aihubmix/"):
            if model.startswith(prefix):
                return model.split("/", 1)[1]
        return model


# ---------------------------------------------------------------------------
# Volcano Engine ARK (火山方舟) image generation —— Seedream 系列
# ---------------------------------------------------------------------------

# 火山方舟 Seedream 宽高比 → 像素尺寸映射（Seedream 5.0/4.5/4.0 文档尺寸）
_VOLCENGINE_ASPECT_RATIO_SIZES = {
    "1:1": "2048x2048",
    "3:4": "1536x2048",
    "4:3": "2048x1536",
    "9:16": "1152x2048",
    "16:9": "2048x1152",
}


class VolcanoImageGenerationClient(OpenAIImageGenerationClient):
    """火山方舟 ARK 图像生成客户端（OpenAI 兼容 Images API，Seedream 系列）。"""

    provider_name = "volcengine"
    missing_key_message = (
        "未配置火山方舟（ARK）密钥。请在 config.json 设置 providers.volcengine.apiKey，"
        "或设置环境变量 ARK_API_KEY。"
    )

    def __init__(self, **kwargs: Any) -> None:
        """构造客户端；未显式传 api_key 时回退环境变量 ARK_API_KEY。"""
        if not kwargs.get("api_key"):
            kwargs["api_key"] = os.environ.get("ARK_API_KEY")
        super().__init__(**kwargs)

    def _default_base_url(self) -> str:
        return "https://ark.cn-beijing.volces.com/api/v3"

    def _base_path(self) -> str:
        return "/api/v3"

    @staticmethod
    def _strip_model_prefix(model: str) -> str:
        """去掉已知 Provider 前缀（volcengine/、volcano/、ark/）。"""
        for prefix in ("volcengine/", "volcano/", "ark/"):
            if model.startswith(prefix):
                return model.split("/", 1)[1]
        return model

    def _size_for(
        self,
        model: str,
        aspect_ratio: str | None,
        image_size: str | None,
    ) -> str:
        """Seedream 尺寸规则：显式 WxH 直通，否则按宽高比映射，最后兜底方形。"""
        explicit = _normalize_openai_image_size(image_size)
        if explicit and _openai_explicit_size_supported(explicit, supported_sizes=None):
            return explicit  # Seedream 接受任意 WIDTHxHEIGHT
        if aspect_ratio and aspect_ratio in _VOLCENGINE_ASPECT_RATIO_SIZES:
            return _VOLCENGINE_ASPECT_RATIO_SIZES[aspect_ratio]
        return "2048x2048"  # 未覆盖宽高比（3:2/2:3/21:9）兜底

    def _reference_images_body(
        self,
        reference_images: list[str] | None,
        model: str,
    ) -> Any:
        """把参考图转成 ``image`` 字段：单张传字符串，多张传数组（data URL）。

        Seedream 系列支持图生图，``image`` 接受 base64 data URL 或 URL，
        单张为字符串、多张为数组（最多 14 张）。无法读取/不支持的图片
        由 :func:`image_path_to_data_url` 抛 ``ImageGenerationError``。
        """
        if not reference_images:
            return None
        try:
            data_urls = [image_path_to_data_url(path) for path in reference_images]
        except (FileNotFoundError, OSError) as exc:
            raise ImageGenerationError(f"参考图读取失败：{exc}") from exc
        return data_urls[0] if len(data_urls) == 1 else data_urls


# ---------------------------------------------------------------------------
# Provider registration —— 模块导入期注册全部图像 Provider
# ---------------------------------------------------------------------------

register_image_gen_provider(AIHubMixImageGenerationClient)
register_image_gen_provider(VolcanoImageGenerationClient)
register_image_gen_provider(DashScopeImageGenerationClient)
register_image_gen_provider(GeminiImageGenerationClient)
register_image_gen_provider(OllamaImageGenerationClient)
register_image_gen_provider(OpenAIImageGenerationClient)
register_image_gen_provider(ZhipuImageGenerationClient)
