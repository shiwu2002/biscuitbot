"""Screenshot tool: capture the screen and use a vision model to understand it."""

from __future__ import annotations

import base64
import io
import os
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from hczkbot.agent.tools.base import Tool, tool_parameters
from hczkbot.agent.tools.schema import (
    StringSchema,
    tool_parameters_schema,
)
from hczkbot.config_base import Base

if TYPE_CHECKING:
    from hczkbot.providers.base import LLMProvider


class ScreenshotToolConfig(Base):
    """Screenshot tool configuration."""

    enable: bool = False
    max_width: int = 1920  # Downscale screenshots wider than this
    max_height: int = 1080  # Downscale screenshots taller than this
    quality: int = 85  # JPEG quality (1-100)


@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "What you want to know about the current screen state. "
            "The vision model will answer this question based on the screenshot.",
            min_length=1,
        ),
        required=["question"],
    )
)
class ScreenshotTool(Tool):
    """Capture a screenshot and use the configured vision model to understand it.

    Requires ``visionModel`` to be set in the agent config (e.g. ``\"qwen-vl\"``).
    The tool captures the current screen, sends it to the vision model along
    with your question, and returns the model's description.
    """

    _capability = (
        "Capture a screenshot and analyze it with the configured vision model."
    )
    _usage_md = "docs/screenshot.md"

    config_key = "screenshot"
    _plugin_discoverable = False  # Requires vision_provider_loader; registered manually

    @classmethod
    def config_cls(cls):
        return ScreenshotToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.screenshot.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            vision_provider_loader=ctx.vision_provider_loader,
            config=ctx.config.screenshot,
        )

    def __init__(
        self,
        *,
        vision_provider_loader: Any = None,
        config: ScreenshotToolConfig | None = None,
    ) -> None:
        self._vision_provider_loader = vision_provider_loader
        self.config = config or ScreenshotToolConfig()

    @property
    def name(self) -> str:
        return "screenshot"

    @property
    def description(self) -> str:
        return (
            "Capture a screenshot of the current screen and use the vision model to "
            "answer a question about what's visible. Useful for understanding the "
            "current state of the desktop, applications, or any visual content on screen."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        question = kwargs.get("question", "").strip()
        if not question:
            return "Error: question parameter is required."

        # 1. Get the vision provider
        provider = self._get_vision_provider()
        if provider is None:
            return (
                "Error: vision model is not configured. "
                "Set `visionModel` in the agent config to a vision-capable model preset "
                "(e.g. \"qwen-vl\", \"gpt-4o\")."
            )

        # 2. Capture screenshot
        screenshot_path = await self._capture_screenshot()
        if screenshot_path is None:
            return self._format_capture_error()

        try:
            # 3. Read and optionally resize the image
            image_data, mime = self._read_and_resize(screenshot_path)

            # 4. Call the vision model
            result = await self._ask_vision_model(provider, image_data, mime, question)
            return result
        finally:
            # Clean up temp file
            try:
                os.unlink(screenshot_path)
            except OSError:
                pass

    def _get_vision_provider(self) -> LLMProvider | None:
        """Lazily load the vision provider."""
        if self._vision_provider_loader is None:
            return None
        try:
            return self._vision_provider_loader()
        except Exception:
            logger.exception("Failed to load vision provider")
            return None

    @staticmethod
    def _is_headless_environment() -> bool:
        """Detect whether the current environment has no graphical display."""
        system = platform.system()
        if system == "Linux":
            return not os.environ.get("DISPLAY")
        if system == "Darwin":
            # macOS uses WindowServer (not X11) for the GUI.  A Mac is only
            # likely headless when accessed via SSH without a logged-in GUI
            # session.  Checking for X11 is wrong — macOS doesn't ship X11
            # by default, so that check would false-positive every desktop Mac.
            return bool(os.environ.get("SSH_CONNECTION"))
        return False

    def _format_capture_error(self) -> str:
        """Build a descriptive error message when screenshot capture fails."""
        if self._is_headless_environment():
            return (
                "Error: screenshot capture failed — this appears to be a headless "
                f"environment ({platform.system()}) with no graphical display. "
                "The screenshot tool cannot capture a physical screen here. "
                "If you need to inspect a web page, use a browser-automation tool "
                "(e.g. Playwright) or fetch the page content directly instead."
            )
        return (
            "Error: failed to capture screenshot. "
            "The platform's native screenshot utility may be missing, misconfigured, "
            "or lacking permissions (e.g. macOS Screen Recording permission). "
            f"Platform: {platform.system()}."
        )

    async def _capture_screenshot(self) -> str | None:
        """Capture a screenshot and return the temp file path."""
        system = platform.system()
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            if system == "Darwin":
                # macOS: use screencapture
                result = subprocess.run(
                    ["screencapture", "-x", "-t", "png", tmp_path],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    logger.warning("screencapture failed: {}", result.stderr.decode())
                    return None
            elif system == "Linux":
                # Linux: try gnome-screenshot, then scrot, then import (ImageMagick)
                for cmd in [
                    ["gnome-screenshot", "-f", tmp_path],
                    ["scrot", tmp_path],
                    ["import", "-window", "root", tmp_path],
                ]:
                    try:
                        result = subprocess.run(
                            cmd, capture_output=True, timeout=10,
                        )
                        if result.returncode == 0:
                            break
                    except FileNotFoundError:
                        continue
                else:
                    logger.warning("No supported screenshot tool found on Linux")
                    return None
            elif system == "Windows":
                # Windows: use PowerShell with .NET
                ps_script = (
                    'Add-Type -AssemblyName System.Windows.Forms;'
                    '[System.Windows.Forms.Screen]::PrimaryScreen | ForEach-Object {'
                    '$bmp = New-Object System.Drawing.Bitmap($_.Bounds.Width, $_.Bounds.Height);'
                    '$g = [System.Drawing.Graphics]::FromImage($bmp);'
                    '$g.CopyFromScreen($_.Bounds.Location, [System.Drawing.Point]::Empty, $_.Bounds.Size);'
                    f"$bmp.Save('{tmp_path}');"
                    '$g.Dispose(); $bmp.Dispose()'
                    '}'
                )
                result = subprocess.run(
                    ["powershell", "-Command", ps_script],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    logger.warning("PowerShell screenshot failed: {}", result.stderr.decode())
                    return None
            else:
                logger.warning("Unsupported platform for screenshot: {}", system)
                return None

            if not Path(tmp_path).exists() or Path(tmp_path).stat().st_size == 0:
                return None
            return tmp_path
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("Screenshot capture failed: {}", exc)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return None

    def _read_and_resize(self, path: str) -> tuple[bytes, str]:
        """Read the image file and optionally downscale it."""
        raw = Path(path).read_bytes()

        # Try to resize if PIL is available and image exceeds limits
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(raw))
            needs_resize = (
                img.width > self.config.max_width
                or img.height > self.config.max_height
            )
            if needs_resize:
                img.thumbnail(
                    (self.config.max_width, self.config.max_height),
                    Image.LANCZOS,
                )
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.config.quality)
                return buf.getvalue(), "image/jpeg"
            # Convert to JPEG for smaller size even if no resize needed
            if img.format != "JPEG":
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.config.quality)
                return buf.getvalue(), "image/jpeg"
        except ImportError:
            logger.debug("PIL not available; using raw screenshot")
        except Exception:
            logger.debug("Image resize failed; using raw screenshot")

        return raw, "image/png"

    async def _ask_vision_model(
        self,
        provider: LLMProvider,
        image_data: bytes,
        mime: str,
        question: str,
    ) -> str:
        """Send the screenshot + question to the vision model."""
        b64 = base64.b64encode(image_data).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {
                        "type": "text",
                        "text": question,
                    },
                ],
            }
        ]
        try:
            response = await provider.chat_with_retry(
                messages=messages,
                tools=None,
                tool_choice=None,
            )
            if response.content:
                return response.content
            return "Vision model returned an empty response."
        except Exception as exc:
            logger.exception("Vision model call failed")
            return f"Error: vision model call failed: {exc}"
