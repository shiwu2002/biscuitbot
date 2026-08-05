"""Tests for ScreenshotTool."""

from __future__ import annotations

from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from biscuitbot.agent.tools.screenshot import ScreenshotTool, ScreenshotToolConfig


def _make_png(width: int = 10, height: int = 10) -> bytes:
    """Create a minimal valid PNG image."""
    try:
        from PIL import Image

        img = Image.new("RGB", (width, height), color="red")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        # Fallback: 1x1 red PNG
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
            b"\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT"
            b"x\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00"
            b"\x00IEND\xaeB`\x82"
        )


class _FakeProvider:
    """Minimal LLMProvider mock that returns a canned response."""

    def __init__(self, response: str = "I see a red rectangle."):
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        resp = MagicMock()
        resp.content = self._response
        return resp

    def get_default_model(self):
        return "test-vision-model"


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestScreenshotToolConfig:
    def test_default_disabled(self):
        config = ScreenshotToolConfig()
        assert config.enable is False

    def test_default_dimensions(self):
        config = ScreenshotToolConfig()
        assert config.max_width == 1920
        assert config.max_height == 1080
        assert config.quality == 85


# ---------------------------------------------------------------------------
# Tool property tests
# ---------------------------------------------------------------------------


class TestScreenshotToolProperties:
    def test_name(self):
        tool = ScreenshotTool()
        assert tool.name == "screenshot"

    def test_read_only(self):
        tool = ScreenshotTool()
        assert tool.read_only is True

    def test_description_mentions_vision(self):
        tool = ScreenshotTool()
        assert "vision" in tool.description.lower()

    def test_enabled_default_off(self):
        ctx = MagicMock()
        ctx.config.screenshot.enable = False
        assert ScreenshotTool.enabled(ctx) is False

    def test_enabled_when_configured(self):
        ctx = MagicMock()
        ctx.config.screenshot.enable = True
        assert ScreenshotTool.enabled(ctx) is True


# ---------------------------------------------------------------------------
# Execute tests
# ---------------------------------------------------------------------------


class TestScreenshotToolExecute:
    @pytest.mark.asyncio
    async def test_no_vision_provider_returns_error(self):
        tool = ScreenshotTool(vision_provider_loader=None)
        result = await tool.execute(question="What's on screen?")
        assert "vision model is not configured" in result

    @pytest.mark.asyncio
    async def test_vision_provider_loader_returns_none(self):
        tool = ScreenshotTool(vision_provider_loader=lambda: None)
        result = await tool.execute(question="What's on screen?")
        assert "vision model is not configured" in result

    @pytest.mark.asyncio
    async def test_screenshot_capture_failure(self):
        provider = _FakeProvider()
        tool = ScreenshotTool(
            vision_provider_loader=lambda: provider,
        )
        # Patch _capture_screenshot to return None (simulating unsupported platform)
        with patch.object(tool, "_capture_screenshot", return_value=None):
            result = await tool.execute(question="What's on screen?")
            # Error message varies by environment (headless vs misconfigured);
            # both paths must report a capture failure.
            assert "screenshot" in result.lower() and "failed" in result.lower()

    @pytest.mark.asyncio
    async def test_successful_screenshot_and_vision_call(self):
        png_data = _make_png()
        provider = _FakeProvider(response="I see a red rectangle.")
        tool = ScreenshotTool(
            vision_provider_loader=lambda: provider,
        )

        async def _fake_capture():
            import tempfile

            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(png_data)
            tmp.close()
            return tmp.name

        with patch.object(tool, "_capture_screenshot", side_effect=_fake_capture):
            result = await tool.execute(question="What do you see?")

        assert "red rectangle" in result
        # Verify the vision model was called with image + text
        assert len(provider.calls) == 1
        messages = provider.calls[0]["messages"]
        assert len(messages) == 1
        content = messages[0]["content"]
        assert isinstance(content, list)
        types = [block["type"] for block in content]
        assert "image_url" in types
        assert "text" in types

    @pytest.mark.asyncio
    async def test_empty_question_returns_error(self):
        tool = ScreenshotTool()
        result = await tool.execute(question="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_vision_model_failure_returns_error(self):
        provider = _FakeProvider()
        provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("API error"))

        png_data = _make_png()
        tool = ScreenshotTool(
            vision_provider_loader=lambda: provider,
        )

        async def _fake_capture():
            import tempfile

            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(png_data)
            tmp.close()
            return tmp.name

        with patch.object(tool, "_capture_screenshot", side_effect=_fake_capture):
            result = await tool.execute(question="What do you see?")

        assert "Error" in result
        assert "API error" in result

    @pytest.mark.asyncio
    async def test_temp_file_cleaned_up(self):
        png_data = _make_png()
        provider = _FakeProvider()
        tool = ScreenshotTool(
            vision_provider_loader=lambda: provider,
        )

        captured_path: str | None = None

        async def _fake_capture():
            import tempfile

            nonlocal captured_path
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(png_data)
            tmp.close()
            captured_path = tmp.name
            return tmp.name

        with patch.object(tool, "_capture_screenshot", side_effect=_fake_capture):
            await tool.execute(question="What do you see?")

        import os

        assert captured_path is not None
        assert not os.path.exists(captured_path), "Temp screenshot file should be cleaned up"


# ---------------------------------------------------------------------------
# Image resize tests
# ---------------------------------------------------------------------------


class TestScreenshotToolResize:
    def test_read_and_resize_no_pil(self):
        """Without PIL, raw bytes are returned as-is."""
        png_data = _make_png()
        tool = ScreenshotTool()
        with patch.dict("sys.modules", {"PIL": None}):
            # If PIL import fails, fall back to raw
            import tempfile

            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(png_data)
            tmp.close()
            try:
                data, mime = tool._read_and_resize(tmp.name)
                assert data == png_data
                assert mime == "image/png"
            finally:
                import os

                os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


class TestBuildVisionProvider:
    def test_returns_none_when_not_configured(self):
        from biscuitbot.config.schema import Config

        config = Config()
        config.agents.defaults.vision_model = None
        from biscuitbot.providers.factory import build_vision_provider

        assert build_vision_provider(config) is None

    def test_returns_none_for_unknown_preset(self):
        from biscuitbot.config.schema import Config

        config = Config()
        config.agents.defaults.vision_model = "nonexistent-preset"
        from biscuitbot.providers.factory import build_vision_provider

        assert build_vision_provider(config) is None
