from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from biscuitbot.agent.tools.kling_video import _DEFAULT_BASE_URL as _KLING_DEFAULT_BASE_URL
from biscuitbot.agent.tools.seedance_video import (
    _AIGC_CHARACTER_DISCLAIMER,
    SeedanceVideoError,
    SeedanceVideoTool,
    SeedanceVideoToolConfig,
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
    for key in ("ratio", "duration", "resolution", "generate_audio", "seed", "watermark", "model"):
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


def test_create_resolves_key_from_unified_provider_config(tmp_path: Path) -> None:
    """视频密钥按 seedance_video.provider 从统一 providers 配置取用，与文生图解耦。"""
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(
            seedance_video=SeedanceVideoToolConfig(enabled=True, provider="volcengine")
        ),
        provider_configs={"volcengine": SimpleNamespace(api_key="ark-from-provider")},
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key == "ark-from-provider"


def test_create_supports_different_provider_than_image_gen(tmp_path: Path) -> None:
    """文生视频可指向与文生图不同的厂商，只要该厂商已在「模型厂商」页配置。"""
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(
            seedance_video=SeedanceVideoToolConfig(enabled=True, provider="my_video_vendor")
        ),
        provider_configs={"my_video_vendor": SimpleNamespace(api_key="video-vendor-key")},
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key == "video-vendor-key"


def test_create_missing_provider_falls_back_to_none(tmp_path: Path) -> None:
    """厂商未配置时 create 不抛错，密钥解析交给 _api_key 的环境变量兜底。"""
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(
            seedance_video=SeedanceVideoToolConfig(enabled=True, provider="volcengine")
        ),
        provider_configs={},
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key is None


def test_kling_key_prefers_provider_key_over_stale_tool_key(tmp_path: Path) -> None:
    """可灵路径取 key 必须优先「模型厂商」页 kling 厂商密钥。

    切厂商到可灵后工具级 apiKey 常残留方舟 ``ark-`` 前缀 key，若优先用它会拿 ark key
    去鉴权可灵 API → 必然 401。回归：两者并存时取 kling 厂商 key。
    """
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(
            enabled=True, provider="kling", api_key="ark-cb6b6da8-stale-volcengine-key"
        ),
        ark_api_key="AK123:SK456",
    )
    assert tool._resolve_kling_key() == "AK123:SK456"


def test_kling_key_falls_back_to_tool_key_when_no_provider(tmp_path: Path) -> None:
    """未在「模型厂商」页配 kling 厂商时，工具级 apiKey 仍可作兜底。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(
            enabled=True, provider="kling", api_key="relay-token-abc"
        ),
    )
    assert tool._resolve_kling_key() == "relay-token-abc"


def test_kling_api_base_ignores_stale_tool_base_url(tmp_path: Path) -> None:
    """可灵 base URL 不读工具级 baseUrl（可能残留方舟地址），用模型厂商 apiBase → 可灵默认。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(
            enabled=True,
            provider="kling",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
        ),
    )
    assert tool._kling_api_base() == _KLING_DEFAULT_BASE_URL
    # 模型厂商页自定义 apiBase 优先于默认
    tool_with_relay = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(
            enabled=True,
            provider="kling",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
        ),
        provider_api_base="https://relay.example/v1",
    )
    assert tool_with_relay._kling_api_base() == "https://relay.example/v1"


@pytest.mark.asyncio
async def test_execute_drops_resolution_in_r2v_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2v（带参考素材）模式下不发送 resolution；纯文生视频才保留。"""
    tool = _tool(tmp_path, api_key="ark-test")
    captured: dict[str, object] = {}

    async def fake_create(self, client, body):
        captured["body"] = body
        return "task-1"

    async def fake_poll(self, client, task_id):
        return {"content": {"video_url": "https://example.com/out.mp4"}}

    async def fake_download(self, client, video_url):
        return {"path": str(tmp_path / "out.mp4")}

    monkeypatch.setattr(SeedanceVideoTool, "_create_task", fake_create)
    monkeypatch.setattr(SeedanceVideoTool, "_poll_until_done", fake_poll)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    # r2v：带参考视频，显式传 resolution=720p 应被丢弃
    await tool.execute(
        prompt="保持运镜不变",
        video_urls=["https://example.com/v.mp4"],
        resolution="720p",
    )
    body = captured["body"]
    assert body["content"][1]["role"] == "reference_video"
    assert "resolution" not in body

    # 纯文生视频：resolution 保留
    await tool.execute(prompt="一只橘猫弹钢琴", resolution="720p")
    body = captured["body"]
    assert body["resolution"] == "720p"


@pytest.mark.asyncio
async def test_execute_defaults_generate_audio_true_in_r2v(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式传 generate_audio 时一律默认开启音效（纯文生/图生同样默认开）。"""
    tool = _tool(tmp_path, api_key="ark-test")
    captured: dict[str, object] = {}

    async def fake_create(self, client, body):
        captured["body"] = body
        return "task-1"

    async def fake_poll(self, client, task_id):
        return {"content": {"video_url": "https://example.com/out.mp4"}}

    async def fake_download(self, client, video_url):
        return {"path": str(tmp_path / "out.mp4")}

    monkeypatch.setattr(SeedanceVideoTool, "_create_task", fake_create)
    monkeypatch.setattr(SeedanceVideoTool, "_poll_until_done", fake_poll)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    # 带参考视频：默认开启
    await tool.execute(prompt="保持运镜", video_urls=["https://example.com/v.mp4"])
    assert captured["body"]["generate_audio"] is True

    # 带参考音频：默认开启
    await tool.execute(prompt="保持运镜", audio_urls=["https://example.com/a.mp3"])
    assert captured["body"]["generate_audio"] is True

    # 纯文生视频：未传参也默认开启音效
    await tool.execute(prompt="一只橘猫弹钢琴")
    assert captured["body"]["generate_audio"] is True

    # 显式关闭：覆盖 r2v 默认
    await tool.execute(
        prompt="保持运镜",
        video_urls=["https://example.com/v.mp4"],
        generate_audio=False,
    )
    assert captured["body"]["generate_audio"] is False


@pytest.mark.asyncio
async def test_execute_drops_default_resolution_in_r2v_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2v 模式同样忽略配置里的 default_resolution。"""
    tool = _tool(tmp_path, api_key="ark-test", default_resolution="720p")
    captured: dict[str, object] = {}

    async def fake_create(self, client, body):
        captured["body"] = body
        return "task-1"

    async def fake_poll(self, client, task_id):
        return {"content": {"video_url": "https://example.com/out.mp4"}}

    async def fake_download(self, client, video_url):
        return {"path": str(tmp_path / "out.mp4")}

    monkeypatch.setattr(SeedanceVideoTool, "_create_task", fake_create)
    monkeypatch.setattr(SeedanceVideoTool, "_poll_until_done", fake_poll)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    await tool.execute(
        prompt="保持运镜不变",
        image_urls=["https://example.com/ref.jpg"],
    )
    body = captured["body"]
    assert body["content"][1]["role"] == "reference_image"
    assert "resolution" not in body
