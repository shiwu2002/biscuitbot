from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from biscuitbot.agent.tools.seedance_video import (
    SeedanceVideoError,
    SeedanceVideoTool,
    SeedanceVideoToolConfig,
    _AIGC_CHARACTER_DISCLAIMER,
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


def _tool(tmp_path: Path, **cfg: object) -> SeedanceVideoTool:
    return SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, **cfg),
    )


def test_tool_metadata_and_config(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    assert tool.name == "generate_video"
    assert tool.config_key == "seedance_video"
    assert SeedanceVideoTool.config_cls() is SeedanceVideoToolConfig

    ctx = SimpleNamespace(config=SimpleNamespace(seedance_video=SeedanceVideoToolConfig(enabled=False)))
    assert SeedanceVideoTool.enabled(ctx) is False
    ctx.config.seedance_video.enabled = True
    assert SeedanceVideoTool.enabled(ctx) is True


def test_default_model_is_seedance_2_0() -> None:
    cfg = SeedanceVideoToolConfig()
    assert cfg.model == "doubao-seedance-2-0-260128"


def test_schema_exposes_four_input_interfaces(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    props = tool.parameters["properties"]
    required = tool.parameters["required"]
    assert required == ["prompt"]
    for key in ("prompt", "image_urls", "video_urls", "audio_urls"):
        assert key in props
    # 生成参数也应暴露
    for key in ("ratio", "duration", "resolution", "generate_audio", "watermark", "model"):
        assert key in props
    assert props["duration"]["minimum"] == 4
    assert props["duration"]["maximum"] == 30


def test_resolve_image_ref_passthrough(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    assert tool._resolve_image_ref(PNG_DATA_URL) == PNG_DATA_URL
    assert tool._resolve_image_ref("https://example.com/a.jpg") == "https://example.com/a.jpg"


def test_resolve_image_ref_local_path_to_base64(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    img = tmp_path / "cat.png"
    img.write_bytes(PNG_BYTES)
    out = tool._resolve_image_ref(str(img))
    assert out == "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")


def test_resolve_image_ref_missing_file(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    with pytest.raises(SeedanceVideoError):
        tool._resolve_image_ref(str(tmp_path / "nope.png"))


def test_resolve_audio_ref_local_path_to_base64(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00")
    out = tool._resolve_audio_ref(str(audio))
    assert out.startswith("data:audio/mpeg;base64,")


def test_resolve_video_ref_only_public_url(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    assert tool._resolve_video_ref("https://example.com/v.mp4") == "https://example.com/v.mp4"
    with pytest.raises(SeedanceVideoError):
        tool._resolve_video_ref(str(tmp_path / "local.mp4"))


def test_build_content_orders_text_and_references(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    content = tool._build_content(
        "hello",
        ["https://example.com/a.jpg"],
        ["https://example.com/v.mp4"],
        ["https://example.com/a.mp3"],
    )
    # 文本块始终以 AIGC 虚拟角色免责声明开头，随后才是原始提示词
    assert content[0]["type"] == "text"
    assert content[0]["text"] == f"{_AIGC_CHARACTER_DISCLAIMER}hello"
    assert content[1]["type"] == "image_url"
    assert content[1]["role"] == "reference_image"
    assert content[1]["image_url"]["url"] == "https://example.com/a.jpg"
    assert content[2]["type"] == "video_url"
    assert content[2]["role"] == "reference_video"
    assert content[3]["type"] == "audio_url"
    assert content[3]["role"] == "reference_audio"


@pytest.mark.parametrize("prompt", ["hello", "", "  只有空格  "])
def test_build_content_always_prepends_aigc_disclaimer(tmp_path: Path, prompt: str) -> None:
    tool = _tool(tmp_path)
    content = tool._build_content(prompt, None, None, None)
    assert content[0]["type"] == "text"
    # 无论提示词内容如何，免责声明都必须被前置
    assert content[0]["text"].startswith(_AIGC_CHARACTER_DISCLAIMER)


@pytest.mark.asyncio
async def test_execute_reports_missing_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    tool = _tool(tmp_path)  # config.api_key 默认 None
    result = await tool.execute(prompt="a cat playing piano")
    assert result.startswith("Error: Seedance API key 未配置")


def test_config_registered_in_schema() -> None:
    from biscuitbot.config.schema import SeedanceVideoToolConfig as SchemaConfig
    from biscuitbot.config.schema import ToolsConfig

    assert "seedance_video" in ToolsConfig.model_fields
    assert SchemaConfig is SeedanceVideoToolConfig
