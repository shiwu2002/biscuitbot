"""Image generation provider helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from hczkbot.providers.registry import find_by_name
from hczkbot.utils.helpers import detect_image_mime


def extract_domain(url: str) -> str:
    """Extract scheme://hostname[:port] from a URL, discarding the path.

    Examples:
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

_DEFAULT_TIMEOUT_S = 120.0
_GEMINI_DEFAULT_TIMEOUT_S = 120.0
_GEMINI_IMAGEN_ASPECT_RATIOS = {"1:1", "9:16", "16:9", "3:4", "4:3"}


def _url_has_path(url: str) -> bool:
    """Return True if *url* contains a non-empty path after the domain."""
    parsed = urlparse(url)
    return bool(parsed.path and parsed.path.strip("/"))
_OLLAMA_DEFAULT_SIDE = 1024
_OLLAMA_SIZE_PRESETS = {
    "1K": 1024,
    "2K": 2048,
    "4K": 4096,
}
_OLLAMA_EXPLICIT_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$")
_OLLAMA_ASPECT_RATIO_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


class ImageGenerationError(RuntimeError):
    """Raised when the image generation provider cannot return images."""


@dataclass(frozen=True)
class GeneratedImageResponse:
    """Images and optional text returned by the provider."""

    images: list[str]
    content: str
    raw: dict[str, Any]


def _read_image_b64(path: str | Path) -> tuple[str, str]:
    """Return ``(mime, base64)`` for the image at ``path``."""
    p = Path(path).expanduser()
    raw = p.read_bytes()
    mime = detect_image_mime(raw)
    if mime is None:
        raise ImageGenerationError(f"unsupported reference image: {p}")
    return mime, base64.b64encode(raw).decode("ascii")


def image_path_to_data_url(path: str | Path) -> str:
    """Convert a local image path to an image data URL."""
    mime, encoded = _read_image_b64(path)
    return f"data:{mime};base64,{encoded}"


def image_path_to_inline_data(path: str | Path) -> dict[str, str]:
    """Convert a local image path to a Gemini ``inlineData`` payload dict."""
    mime, encoded = _read_image_b64(path)
    return {"mimeType": mime, "data": encoded}


def _b64_image_data_url(value: str) -> str:
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
# Registry
# ---------------------------------------------------------------------------

_IMAGE_GEN_PROVIDERS: dict[str, type[ImageGenerationProvider]] = {}


def register_image_gen_provider(cls: type[ImageGenerationProvider]) -> None:
    """Register an image provider at import time only.

    The registry is populated by module side effects so provider discovery
    stays lazy and consistent across the process.
    """
    name = cls.provider_name
    if not name:
        raise ValueError(f"{cls.__name__} must set provider_name")
    _IMAGE_GEN_PROVIDERS[name] = cls


def get_image_gen_provider(name: str) -> type[ImageGenerationProvider] | None:
    return _IMAGE_GEN_PROVIDERS.get(name)


def image_gen_provider_names() -> tuple[str, ...]:
    """Return registered image generation provider names in registry order."""
    return tuple(_IMAGE_GEN_PROVIDERS)


def image_gen_provider_configs(config: Any) -> dict[str, Any]:
    providers_cfg = config.providers
    return {
        name: pc
        for name in _IMAGE_GEN_PROVIDERS
        if (pc := getattr(providers_cfg, name, None)) is not None
    }


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class ImageGenerationProvider(ABC):
    """Base class for image generation provider clients."""

    provider_name: str = ""
    missing_key_message: str = ""
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
        """Return the default URL path prefix for this provider.

        Used by ``_resolve_base_url`` when the caller supplies only a domain
        (e.g. ``https://api.openai.com``).  The domain is combined with this
        path to form the full base URL (e.g. ``https://api.openai.com/v1``).

        Subclasses that use a non-empty path prefix should override this.
        """
        return ""

    def _resolve_base_url(self, api_base: str | None) -> str:
        if api_base:
            base = api_base.rstrip("/")
            # If the caller supplied only a domain (no path), append the
            # provider's default path prefix so the URL is usable directly.
            # e.g. "https://api.openai.com" → "https://api.openai.com/v1"
            base_path = self._base_path()
            if base_path and not _url_has_path(base):
                base = f"{base}{base_path}"
            return base
        spec = find_by_name(self.provider_name)
        if spec and spec.default_api_base:
            return spec.default_api_base.rstrip("/")
        return self._default_base_url()

    def _default_base_url(self) -> str:
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
    ) -> GeneratedImageResponse: ...

    def _require_images(self, images: list[str], data: dict[str, Any]) -> None:
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
        if client is not None:
            return await client.post(url, headers=headers, json=body)
        if self._client is not None:
            return await self._client.post(url, headers=headers, json=body)
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            return await c.post(url, headers=headers, json=body)




def _http_error_detail(response: httpx.Response) -> str:
    """Extract a readable error message from an HTTP error response."""
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
    rounded = int(round(value / multiple) * multiple)
    return max(multiple, rounded)


def _ollama_dimensions(aspect_ratio: str | None, image_size: str | None) -> tuple[int, int]:
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
        width = long_side
        height = _round_to_multiple(long_side * height_ratio / width_ratio)
    else:
        height = long_side
        width = _round_to_multiple(long_side * width_ratio / height_ratio)
    return max(8, width), max(8, height)


def _ollama_image_data_url(value: str) -> str:
    if value.startswith("data:image/"):
        return value
    return _b64_image_data_url(value)


def _ollama_images_from_payload(payload: dict[str, Any]) -> list[str]:
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
    """Async client for Ollama native image generation models."""

    provider_name = "ollama"
    default_timeout = 300.0

    def _default_base_url(self) -> str:
        return "http://localhost:11434/api"

    def _base_path(self) -> str:
        return "/api"

    def _resolve_base_url(self, api_base: str | None) -> str:
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
            "steps": 0,
        }
        body.update(self.extra_body)
        body["stream"] = False

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
    """Async client for Gemini/Imagen image generation via the Generative Language API."""

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
        # Gemini chat completions use the registry's OpenAI-compatible shim.
        # Image generation must hit the native Generative Language API, so we
        # intentionally bypass the shared registry lookup here.
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
        parts: list[dict[str, Any]] = [
            {"inlineData": image_path_to_inline_data(path)} for path in reference_images
        ]
        parts.append({"text": prompt})

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
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
# OpenAI image generation
# ---------------------------------------------------------------------------

_OPENAI_DALLE2_SUPPORTED_SIZES = {"256x256", "512x512", "1024x1024"}
_OPENAI_DALLE3_SUPPORTED_SIZES = {"1024x1024", "1792x1024", "1024x1792"}
_OPENAI_GPT_IMAGE_SUPPORTED_SIZES = {
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "auto",
}
_OPENAI_DALLE2_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1024x1024",
    "9:16": "1024x1024",
    "3:4": "1024x1024",
    "4:3": "1024x1024",
}
_OPENAI_DALLE3_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "3:4": "1024x1792",
    "4:3": "1792x1024",
}
_OPENAI_GPT_IMAGE_ASPECT_RATIO_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "3:4": "1024x1536",
    "4:3": "1536x1024",
}


class OpenAIImageGenerationClient(ImageGenerationProvider):
    """OpenAI Images API using an API key (``providers.openai.apiKey``)."""

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
        """Remove ``openai/`` prefix if present (OpenRouter convention)."""
        if model.startswith("openai/"):
            return model.split("/", 1)[1]
        return model

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
            logger.warning(
                "DALL-E models do not support reference images; "
                "ignoring {} reference image(s) for {}",
                len(reference_images),
                model,
            )

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

        if not _openai_is_gpt_image_model(clean_model):
            body["response_format"] = "b64_json"
            body["n"] = 1

        size = _openai_size(clean_model, aspect_ratio, image_size)
        if size:
            body["size"] = size

        body.update(self.extra_body)
        # Drop null-valued params so extraBody can opt out of defaults like response_format.
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
    """Resolve aspect ratio or image_size to an OpenAI Images API size string."""
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
    normalized = model.lower()
    return normalized.startswith(("gpt-image", "chatgpt-image"))


def _openai_size_options(model: str) -> tuple[dict[str, str], set[str] | None]:
    normalized = model.lower()
    if normalized.startswith("dall-e-2"):
        return _OPENAI_DALLE2_ASPECT_RATIO_SIZES, _OPENAI_DALLE2_SUPPORTED_SIZES
    if normalized.startswith("dall-e-3"):
        return _OPENAI_DALLE3_ASPECT_RATIO_SIZES, _OPENAI_DALLE3_SUPPORTED_SIZES
    if normalized.startswith("gpt-image-2"):
        return _OPENAI_GPT_IMAGE_ASPECT_RATIO_SIZES, None
    return _OPENAI_GPT_IMAGE_ASPECT_RATIO_SIZES, _OPENAI_GPT_IMAGE_SUPPORTED_SIZES


def _normalize_openai_image_size(image_size: str | None) -> str | None:
    if not image_size:
        return None
    normalized = image_size.strip().lower()
    return normalized or None


def _openai_explicit_size_supported(
    size: str,
    *,
    supported_sizes: set[str] | None,
) -> bool:
    if supported_sizes is not None:
        return size in supported_sizes
    width, sep, height = size.partition("x")
    return bool(sep and width.isdecimal() and height.isdecimal())


async def _openai_images_from_payload(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
) -> list[str]:
    """Extract images from OpenAI Images API response.

    Handles both ``b64_json`` (preferred) and ``url`` (downloaded) formats.
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
# Zhipu (智谱) image generation
# ---------------------------------------------------------------------------

_ZHIPU_TIMEOUT_S = 300.0

_ZHIPU_ASPECT_RATIO_SIZES = {
    "1:1": "1280x1280",
    "16:9": "1728x960",
    "9:16": "960x1728",
    "3:4": "1088x1472",
    "4:3": "1472x1088",
}


class ZhipuImageGenerationClient(ImageGenerationProvider):
    """Async client for Zhipu (智谱) image generation API.

    Supports:
    - Text-to-image via glm-image, cogview-4, cogview-3-flash, etc.
    - Aspect ratio selection
    - Watermark control
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
    """Resolve aspect ratio / image_size to Zhipu size string.

    Zhipu glm-image model supports: 1280x1280 (default), 1568x1056,
    1056x1568, 1472x1088, 1088x1472, 1728x960, 960x1728.
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
    """Extract image data URLs from Zhipu API response.

    Zhipu returns images as temporary URLs that expire after 30 days.
    We download and re-encode as base64 data URLs.
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
# DashScope (阿里灵积/万相) image generation
# ---------------------------------------------------------------------------

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
    """Resolve aspect ratio / image_size to DashScope size string.

    DashScope uses ``WIDTH*HEIGHT`` format (asterisk, not ``x``).
    """
    if image_size:
        size = image_size.strip().lower().replace("x", "*")
        if "*" in size:
            return size
    if aspect_ratio and aspect_ratio in _DASHSCOPE_ASPECT_RATIO_SIZES:
        return _DASHSCOPE_ASPECT_RATIO_SIZES[aspect_ratio]
    return "1024*1024"


_DASHSCOPE_DEFAULT_API_BASE = "https://dashscope.aliyuncs.com"
_DASHSCOPE_SUBMIT_PATH_OLD = "/api/v1/services/aigc/text2image/image-synthesis"
_DASHSCOPE_SUBMIT_PATH_NEW = "/api/v1/services/aigc/multimodal-generation/generation"
_DASHSCOPE_TASK_PATH = "/api/v1/tasks"
_DASHSCOPE_POLL_INTERVAL_S = 2.0
_DASHSCOPE_MAX_POLL_ATTEMPTS = 60

# Models that use the newer messages-based API (wan2.6+, qwen-image*)
_DASHSCOPE_NEW_MODELS = frozenset({
    "wan2.6-t2i",
    "wan2.6-image",
    "wan2.5-t2i-preview",
})

# Models that support synchronous (direct) response — no polling needed.
# qwen-image and wan2.6 models return the image URL directly in the POST response.
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
    """Determine the API mode for a DashScope image model.

    Returns:
        "sync"  — qwen-image/wan2.6 models that respond directly (千问/万相2.6同步模式)
        "async_new" — newer wan2.x models using messages format (万相V2异步模式)
        "async_old" — older wanx models using prompt format (万相V1异步模式)
    """
    if model in _DASHSCOPE_SYNC_MODELS or model.startswith("qwen-image"):
        return "sync"
    if model.startswith("wan2."):
        return "async_new"
    return "async_old"


class DashScopeImageGenerationClient(ImageGenerationProvider):
    """Async client for DashScope (阿里灵积) image generation API.

    Supports two text-to-image modes under DashScope:

    - **千问模式 (qwen-image)**: Synchronous response.
      Models: qwen-image-2.0-pro, qwen-image-plus, qwen-image-max, etc.
      Uses ``/api/v1/services/aigc/image-generation/generation`` with
      ``input.messages`` format.  The image URL is returned directly in
      the POST response at ``output.choices[].message.content[].image``.

    - **万相模式 (wanx / wan2.x)**: Asynchronous task-based.
      Models: wanx2.1-t2i-turbo, wanx2.1-t2i-plus, wanx-v1, wan2.6-t2i, etc.
      Submits a task (with ``X-DashScope-Async: enable``), then polls
      ``/api/v1/tasks/{task_id}`` until the result is ready.
      - Newer wan2.x models use ``input.messages`` format.
      - Older wanx models use ``input.prompt`` format.
    """

    provider_name = "dashscope"
    missing_key_message = "DashScope API key is not configured. Set providers.dashscope.apiKey."
    default_timeout = 300.0

    def _default_base_url(self) -> str:
        return _DASHSCOPE_DEFAULT_API_BASE

    def _base_path(self) -> str:
        # DashScope image paths are determined by model mode at request time,
        # not at client construction time.  Return empty so that a bare domain
        # like "https://dashscope.aliyuncs.com" is kept as-is.
        return ""

    def _resolve_base_url(self, api_base: str | None) -> str:
        """Override to skip the LLM registry's default_api_base.

        DashScope's image generation API uses a different base path
        (``/api/v1/...``) than the LLM compatible-mode endpoint
        (``/compatible-mode/v1``), so we must not fall back to the
        registry's ``default_api_base``.

        If the user provides an api_base that already includes a known
        DashScope path (e.g. the full endpoint URL), strip it so that
        only the domain remains — the endpoint path is always determined
        by the model mode.
        """
        if api_base:
            base = api_base.rstrip("/")
            # Strip known DashScope paths — api_base should be just the domain
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

        # Build request body based on model mode
        if mode == "async_old":
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
            # Both sync (qwen-image) and async_new (wan2.x) use messages format
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

        # Sync models don't need the async header
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
            # Submit the request
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

            # --- Sync mode: image URL is in the direct response ---
            if is_sync:
                return await self._handle_sync_response(client, submit_data)

            # --- Async mode: poll for the result ---
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
        """Parse a synchronous DashScope response (千问模式)."""
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
        """Poll a DashScope async task until it completes (万相模式)."""
        poll_url = f"{self.api_base}{_DASHSCOPE_TASK_PATH}/{task_id}"
        poll_headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        for attempt in range(_DASHSCOPE_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(_DASHSCOPE_POLL_INTERVAL_S)

            try:
                poll_resp = await client.get(poll_url, headers=poll_headers)
            except httpx.RequestError as exc:
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

                # New models: output.choices[].message.content[].image
                choices = output.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        content = (choice.get("message") or {}).get("content") or []
                        for item in content:
                            url = item.get("image")
                            if isinstance(url, str) and url:
                                images.append(await _download_image_data_url(client, url))

                # Old models: output.results[].url
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

            # Still PENDING or RUNNING, continue polling
            if task_status not in ("PENDING", "RUNNING"):
                logger.warning("DashScope unknown task status: {}", task_status)

        raise ImageGenerationError(
            f"DashScope image generation timed out after {_DASHSCOPE_MAX_POLL_ATTEMPTS} polls"
        )


# ---------------------------------------------------------------------------
# AIHubMix image generation (OpenAI-compatible gateway)
# ---------------------------------------------------------------------------


class AIHubMixImageGenerationClient(OpenAIImageGenerationClient):
    """AIHubMix image generation via its OpenAI-compatible Images API."""

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
        """Remove known provider prefixes (openai/, aihubmix/)."""
        for prefix in ("openai/", "aihubmix/"):
            if model.startswith(prefix):
                return model.split("/", 1)[1]
        return model


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------

register_image_gen_provider(AIHubMixImageGenerationClient)
register_image_gen_provider(DashScopeImageGenerationClient)
register_image_gen_provider(GeminiImageGenerationClient)
register_image_gen_provider(OllamaImageGenerationClient)
register_image_gen_provider(OpenAIImageGenerationClient)
register_image_gen_provider(ZhipuImageGenerationClient)
