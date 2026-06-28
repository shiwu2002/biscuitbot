from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from hczkbot.providers.image_generation import (
    GeminiImageGenerationClient,
    ImageGenerationError,
    OllamaImageGenerationClient,
    OpenAIImageGenerationClient,
    ZhipuImageGenerationClient,
)

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdacd\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 12


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        status_code: int = 200,
        content: bytes = b"",
        sse_lines: list[str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.content = content
        self.request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        self._sse_lines = sse_lines

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request, text=self.text)
            raise httpx.HTTPStatusError("failed", request=self.request, response=response)

    async def aiter_lines(self):
        if self._sse_lines is not None:
            for line in self._sse_lines:
                yield line
            return
        # Fallback: treat response text as SSE lines
        for line in self.text.split("\n"):
            yield line


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.get_response = response
        self.calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self.get_response


@pytest.mark.asyncio
async def test_ollama_image_generation_payload_and_response() -> None:
    raw_b64 = PNG_DATA_URL.removeprefix("data:image/png;base64,")
    fake = FakeClient(FakeResponse({"image": raw_b64}))
    client = OllamaImageGenerationClient(
        api_key="ollama-test",
        api_base="http://localhost:11434/v1/",
        extra_headers={"X-Test": "1"},
        extra_body={"seed": 123},
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(
        prompt="a sunset",
        model="x/z-image-turbo",
        aspect_ratio="16:9",
        image_size="1K",
    )

    assert response.images == [PNG_DATA_URL]
    assert response.content == ""

    call = fake.calls[0]
    assert call["url"] == "http://localhost:11434/api/generate"
    assert call["headers"]["Authorization"] == "Bearer ollama-test"
    assert call["headers"]["X-Test"] == "1"
    body = call["json"]
    assert body["model"] == "x/z-image-turbo"
    assert body["prompt"] == "a sunset"
    assert body["width"] == 1024
    assert body["height"] == 576
    assert body["steps"] == 0
    assert body["stream"] is False
    assert body["seed"] == 123


@pytest.mark.asyncio
async def test_ollama_image_generation_rejects_reference_images() -> None:
    client = OllamaImageGenerationClient(api_key=None)

    with pytest.raises(ImageGenerationError, match="reference images"):
        await client.generate(
            prompt="edit this",
            model="x/z-image-turbo",
            reference_images=["ref.png"],
        )


RAW_B64 = PNG_DATA_URL.removeprefix("data:image/png;base64,")


@pytest.mark.asyncio
async def test_gemini_imagen_payload_and_response() -> None:
    fake = FakeClient(
        FakeResponse({"predictions": [{"bytesBase64Encoded": RAW_B64, "mimeType": "image/png"}]})
    )
    client = GeminiImageGenerationClient(
        api_key="AIza-test",
        api_base="https://generativelanguage.googleapis.com/v1beta",
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(
        prompt="a sunset",
        model="imagen-4.0-generate-001",
        aspect_ratio="16:9",
    )

    assert response.images == [PNG_DATA_URL]
    assert response.content == ""
    call = fake.calls[0]
    assert call["url"].endswith(":predict")
    assert call["headers"]["x-goog-api-key"] == "AIza-test"
    assert "params" not in call
    body = call["json"]
    assert body["instances"] == [{"prompt": "a sunset"}]
    assert body["parameters"]["sampleCount"] == 1
    assert body["parameters"]["aspectRatio"] == "16:9"


@pytest.mark.asyncio
async def test_gemini_imagen_ignores_unsupported_aspect_ratio() -> None:
    fake = FakeClient(
        FakeResponse({"predictions": [{"bytesBase64Encoded": RAW_B64, "mimeType": "image/png"}]})
    )
    client = GeminiImageGenerationClient(api_key="AIza-test", client=fake)  # type: ignore[arg-type]

    await client.generate(prompt="a sunset", model="imagen-4.0-generate-001", aspect_ratio="2:3")

    body = fake.calls[0]["json"]
    assert "aspectRatio" not in body["parameters"]


@pytest.mark.asyncio
async def test_gemini_flash_payload_and_response() -> None:
    fake = FakeClient(
        FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "here is your image"},
                                {"inlineData": {"mimeType": "image/png", "data": RAW_B64}},
                            ]
                        }
                    }
                ]
            }
        )
    )
    client = GeminiImageGenerationClient(
        api_key="AIza-test",
        api_base="https://generativelanguage.googleapis.com/v1beta",
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(
        prompt="draw a cat",
        model="gemini-2.0-flash-preview-image-generation",
    )

    assert response.images == [PNG_DATA_URL]
    assert response.content == "here is your image"
    call = fake.calls[0]
    assert call["url"].endswith(":generateContent")
    assert call["headers"]["x-goog-api-key"] == "AIza-test"
    assert "params" not in call
    body = call["json"]
    assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
    assert body["contents"][0]["parts"][-1] == {"text": "draw a cat"}


@pytest.mark.asyncio
async def test_gemini_flash_reference_images(tmp_path: Path) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG_BYTES)
    fake = FakeClient(
        FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"inlineData": {"mimeType": "image/png", "data": RAW_B64}}]
                        }
                    }
                ]
            }
        )
    )
    client = GeminiImageGenerationClient(api_key="AIza-test", client=fake)  # type: ignore[arg-type]

    response = await client.generate(
        prompt="edit this",
        model="gemini-2.0-flash-preview-image-generation",
        reference_images=[str(ref)],
    )

    assert response.images == [PNG_DATA_URL]
    parts = fake.calls[0]["json"]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "image/png"
    assert parts[0]["inlineData"]["data"].startswith("iVBOR")
    assert parts[1] == {"text": "edit this"}


@pytest.mark.asyncio
async def test_gemini_requires_api_key() -> None:
    client = GeminiImageGenerationClient(api_key=None)

    with pytest.raises(ImageGenerationError, match="API key"):
        await client.generate(prompt="draw", model="imagen-4.0-generate-001")


def test_gemini_image_client_uses_native_api_base_by_default() -> None:
    client = GeminiImageGenerationClient(api_key="AIza-test")
    assert client.api_base == "https://generativelanguage.googleapis.com/v1beta"


@pytest.mark.asyncio
async def test_gemini_no_images_raises() -> None:
    fake = FakeClient(FakeResponse({"candidates": [{"content": {"parts": [{"text": "sorry"}]}}]}))
    client = GeminiImageGenerationClient(api_key="AIza-test", client=fake)  # type: ignore[arg-type]

    with pytest.raises(ImageGenerationError, match="returned no images"):
        await client.generate(prompt="draw", model="gemini-2.0-flash-preview-image-generation")


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_payload_and_response() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        api_base="https://api.openai.com/v1",
        extra_headers={"X-Test": "1"},
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(
        prompt="a cat on the moon",
        model="dall-e-3",
        aspect_ratio="16:9",
    )

    assert response.images == [PNG_DATA_URL]
    call = fake.calls[0]
    assert call["url"] == "https://api.openai.com/v1/images/generations"
    assert call["headers"]["Authorization"] == "Bearer sk-openai-test"
    assert call["headers"]["X-Test"] == "1"
    body = call["json"]
    assert body["model"] == "dall-e-3"
    assert body["prompt"] == "a cat on the moon"
    assert body["response_format"] == "b64_json"
    assert body["n"] == 1
    assert body["size"] == "1792x1024"


@pytest.mark.asyncio
async def test_openai_extra_body_null_drops_default_params_only() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        extra_body={
            "response_format": None,
            "seed": 0,
            "safety_checker": False,
        },
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(prompt="draw", model="dall-e-3")

    body = fake.calls[0]["json"]
    assert "response_format" not in body
    assert body["n"] == 1
    assert body["seed"] == 0
    assert body["safety_checker"] is False


@pytest.mark.asyncio
async def test_openai_b64_json_response_uses_detected_mime() -> None:
    raw_b64 = base64.b64encode(JPEG_BYTES).decode("ascii")
    fake = FakeClient(FakeResponse({"data": [{"b64_json": raw_b64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(prompt="draw", model="dall-e-3")

    assert response.images == [f"data:image/jpeg;base64,{raw_b64}"]


@pytest.mark.asyncio
async def test_openai_url_download_fallback() -> None:
    fake = FakeClient(FakeResponse({"data": [{"url": "https://cdn.example/image.png"}]}))
    fake.get_response = FakeResponse({}, content=PNG_BYTES)
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(prompt="draw", model="dall-e-3")

    assert response.images[0].startswith("data:image/png;base64,")
    assert fake.get_calls[0]["url"] == "https://cdn.example/image.png"


@pytest.mark.asyncio
async def test_openai_multiple_images() -> None:
    fake = FakeClient(FakeResponse({
        "data": [
            {"b64_json": RAW_B64},
            {"b64_json": RAW_B64},
        ]
    }))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(prompt="draw", model="dall-e-3")

    assert len(response.images) == 2
    assert response.images == [PNG_DATA_URL, PNG_DATA_URL]


@pytest.mark.asyncio
async def test_openai_aspect_ratio_to_size() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(prompt="draw", model="dall-e-3", aspect_ratio="1:1")
    assert fake.calls[0]["json"]["size"] == "1024x1024"


@pytest.mark.asyncio
async def test_openai_dalle3_uses_supported_orientation_sizes() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(prompt="draw", model="dall-e-3", aspect_ratio="3:4")
    await client.generate(prompt="draw", model="dall-e-3", aspect_ratio="4:3")

    assert fake.calls[0]["json"]["size"] == "1024x1792"
    assert fake.calls[1]["json"]["size"] == "1792x1024"


@pytest.mark.asyncio
async def test_openai_dalle2_uses_square_size_for_non_square_ratios() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(prompt="draw", model="dall-e-2", aspect_ratio="16:9")

    assert fake.calls[0]["json"]["size"] == "1024x1024"


@pytest.mark.asyncio
async def test_openai_gpt_image_uses_supported_landscape_size() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(prompt="draw", model="gpt-image-1", aspect_ratio="16:9")

    assert fake.calls[0]["json"]["size"] == "1536x1024"


@pytest.mark.asyncio
async def test_openai_gpt_image_uses_supported_orientation_sizes() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(prompt="draw", model="gpt-image-1", aspect_ratio="3:4")
    await client.generate(prompt="draw", model="gpt-image-1", aspect_ratio="4:3")

    assert fake.calls[0]["json"]["size"] == "1024x1536"
    assert fake.calls[1]["json"]["size"] == "1536x1024"


@pytest.mark.asyncio
async def test_openai_default_size_when_no_aspect_ratio() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(prompt="draw", model="dall-e-3")

    body = fake.calls[0]["json"]
    assert body["size"] == "1024x1024"


@pytest.mark.asyncio
async def test_openai_ignores_explicit_size_unsupported_by_model_family() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(
        prompt="draw",
        model="dall-e-3",
        aspect_ratio="16:9",
        image_size="1536x1024",
    )

    body = fake.calls[0]["json"]
    assert body["size"] == "1792x1024"


@pytest.mark.asyncio
async def test_openai_uses_explicit_image_size() -> None:
    fake = FakeClient(FakeResponse({"data": [{"b64_json": RAW_B64}]}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(
        prompt="draw",
        model="dall-e-3",
        aspect_ratio="16:9",
        image_size="1024x1024",
    )

    body = fake.calls[0]["json"]
    assert body["size"] == "1024x1024"


@pytest.mark.asyncio
async def test_openai_requires_api_key() -> None:
    client = OpenAIImageGenerationClient(api_key=None)

    with pytest.raises(ImageGenerationError, match="API key"):
        await client.generate(prompt="draw", model="dall-e-3")


@pytest.mark.asyncio
async def test_openai_no_images_raises() -> None:
    fake = FakeClient(FakeResponse({"data": []}))
    client = OpenAIImageGenerationClient(
        api_key="sk-openai-test",
        client=fake,  # type: ignore[arg-type]
    )

    with pytest.raises(ImageGenerationError, match="returned no images"):
        await client.generate(prompt="draw", model="dall-e-3")


# ---------------------------------------------------------------------------
# Zhipu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zhipu_image_generation_payload_and_response() -> None:
    fake = FakeClient(FakeResponse({"data": [{"url": "https://cdn.example/image.png"}]}))
    fake.get_response = FakeResponse({}, content=PNG_BYTES)
    client = ZhipuImageGenerationClient(
        api_key="sk-zhipu-test",
        api_base="https://open.bigmodel.cn/api/paas/v4",
        extra_headers={"X-Test": "1"},
        extra_body={"watermark_enabled": False},
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(
        prompt="a sunset over the ocean",
        model="glm-image",
        aspect_ratio="16:9",
        image_size="2K",
    )

    assert response.images[0].startswith("data:image/png;base64,")
    call = fake.calls[0]
    assert call["url"] == "https://open.bigmodel.cn/api/paas/v4/images/generations"
    assert call["headers"]["Authorization"] == "Bearer sk-zhipu-test"
    assert call["headers"]["X-Test"] == "1"
    body = call["json"]
    assert body["model"] == "glm-image"
    assert body["prompt"] == "a sunset over the ocean"
    assert body["size"] == "1728x960"
    assert body["watermark_enabled"] is False


@pytest.mark.asyncio
async def test_zhipu_image_generation_with_explicit_size() -> None:
    fake = FakeClient(FakeResponse({"data": [{"url": "https://cdn.example/image.png"}]}))
    fake.get_response = FakeResponse({}, content=PNG_BYTES)
    client = ZhipuImageGenerationClient(
        api_key="sk-zhipu-test",
        client=fake,  # type: ignore[arg-type]
    )

    await client.generate(
        prompt="a cat",
        model="cogview-4",
        image_size="1024x1024",
    )

    body = fake.calls[0]["json"]
    assert body["size"] == "1024x1024"


@pytest.mark.asyncio
async def test_zhipu_image_generation_downloads_url_response() -> None:
    fake = FakeClient(FakeResponse({"data": [{"url": "https://cdn.example/image.png"}]}))
    fake.get_response = FakeResponse({}, content=PNG_BYTES)
    client = ZhipuImageGenerationClient(
        api_key="sk-zhipu-test",
        client=fake,  # type: ignore[arg-type]
    )

    response = await client.generate(prompt="draw", model="glm-image")

    assert response.images[0].startswith("data:image/png;base64,")
    assert fake.get_calls[0]["url"] == "https://cdn.example/image.png"


@pytest.mark.asyncio
async def test_zhipu_image_generation_requires_api_key() -> None:
    client = ZhipuImageGenerationClient(api_key=None)

    with pytest.raises(ImageGenerationError, match="API key"):
        await client.generate(prompt="draw", model="glm-image")


@pytest.mark.asyncio
async def test_zhipu_image_generation_no_images_raises() -> None:
    fake = FakeClient(FakeResponse({"data": [{"text": "sorry"}]}))
    client = ZhipuImageGenerationClient(api_key="sk-zhipu-test", client=fake)  # type: ignore[arg-type]

    with pytest.raises(ImageGenerationError, match="returned no images"):
        await client.generate(prompt="draw", model="glm-image")


@pytest.mark.asyncio
async def test_zhipu_image_generation_rejects_reference_images() -> None:
    client = ZhipuImageGenerationClient(api_key="sk-zhipu-test")

    with pytest.raises(ImageGenerationError, match="reference images"):
        await client.generate(
            prompt="edit this",
            model="glm-image",
            reference_images=["ref.png"],
        )
