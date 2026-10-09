from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianaibot.agent.tools._video_common import (
    AIGC_CHARACTER_DISCLAIMER,
    VideoToolError,
    resolve_audio_ref,
    resolve_image_ref,
    resolve_video_ref,
)
from xianaibot.agent.tools.seedance_video import (
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
        config=SeedanceVideoToolConfig(**cfg),
    )


def test_tool_metadata_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _tool(tmp_path)
    assert tool.name == "generate_video_seedance"
    assert tool.config_key == "seedance_video"
    assert SeedanceVideoTool.config_cls() is SeedanceVideoToolConfig

    # 启用门控（厂商自包含后不再有 enabled/provider 字段）：
    # 显式 apiKey / 环境变量 ARK_API_KEY / 模型厂商页 volcengine 密钥任一即启用。
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(seedance_video=SeedanceVideoToolConfig()),
        provider_configs={},
    )
    assert SeedanceVideoTool.enabled(ctx) is False

    ctx.config.seedance_video = SeedanceVideoToolConfig(api_key="ark-explicit")
    assert SeedanceVideoTool.enabled(ctx) is True

    ctx.config.seedance_video = SeedanceVideoToolConfig()
    monkeypatch.setenv("ARK_API_KEY", "ark-env")
    assert SeedanceVideoTool.enabled(ctx) is True

    monkeypatch.delenv("ARK_API_KEY", raising=False)
    ctx.provider_configs = {"volcengine": SimpleNamespace(api_key="ark-from-provider")}
    assert SeedanceVideoTool.enabled(ctx) is True

    # 其他厂商（kling / minimax）的密钥不启用 Seedance 工具
    ctx.provider_configs = {"kling": SimpleNamespace(api_key="AK:SK")}
    assert SeedanceVideoTool.enabled(ctx) is False


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
    assert resolve_image_ref(PNG_DATA_URL) == PNG_DATA_URL
    assert resolve_image_ref("https://example.com/a.jpg") == "https://example.com/a.jpg"


def test_resolve_image_ref_local_path_to_base64(tmp_path: Path) -> None:
    img = tmp_path / "cat.png"
    img.write_bytes(PNG_BYTES)
    out = resolve_image_ref(str(img))
    assert out == "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")


def test_resolve_image_ref_missing_file(tmp_path: Path) -> None:
    with pytest.raises(VideoToolError):
        resolve_image_ref(str(tmp_path / "nope.png"))


def test_resolve_audio_ref_local_path_to_base64(tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00")
    out = resolve_audio_ref(str(audio))
    assert out.startswith("data:audio/mpeg;base64,")


def test_resolve_video_ref_only_public_url(tmp_path: Path) -> None:
    assert resolve_video_ref("https://example.com/v.mp4") == "https://example.com/v.mp4"
    with pytest.raises(VideoToolError):
        resolve_video_ref(str(tmp_path / "local.mp4"))


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
    assert content[0]["text"] == f"{AIGC_CHARACTER_DISCLAIMER}hello"
    assert content[1]["type"] == "image_url"
    assert content[1]["role"] == "reference_image"
    assert content[1]["image_url"]["url"] == "https://example.com/a.jpg"
    assert content[2]["type"] == "video_url"
    assert content[2]["role"] == "reference_video"
    assert content[3]["type"] == "audio_url"
    assert content[3]["role"] == "reference_audio"


@pytest.mark.parametrize("prompt", ["hello", "", "  只有空格  "])
def test_build_content_prepends_aigc_disclaimer_only_with_references(
    tmp_path: Path, prompt: str
) -> None:
    """免责声明**只在带参考图时**前置。

    声明文案说的是「参考素材为 AI 生成的虚拟角色数字插画」，纯文生视频
    （t2v）没有参考素材，拼上去与场景不符——见 ``_build_content`` 的注释。
    """
    tool = _tool(tmp_path)

    # 无参考图：原样透传提示词，不掺声明（空提示词也保持空）
    t2v = tool._build_content(prompt, None, None, None)
    assert t2v[0]["type"] == "text"
    assert t2v[0]["text"] == prompt

    # 有参考图：声明必须前置，且不影响后续素材块的顺序
    with_ref = tool._build_content(prompt, ["https://example.com/a.jpg"], None, None)
    assert with_ref[0]["text"] == f"{AIGC_CHARACTER_DISCLAIMER}{prompt}"
    assert with_ref[1]["type"] == "image_url"


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
    from xianaibot.config.schema import SeedanceVideoToolConfig as SchemaConfig
    from xianaibot.config.schema import ToolsConfig

    assert "seedance_video" in ToolsConfig.model_fields
    assert SchemaConfig is SeedanceVideoToolConfig


def test_create_resolves_key_from_unified_provider_config(tmp_path: Path) -> None:
    """Seedance 密钥固定从统一 providers 配置的 volcengine 厂商取用。"""
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(seedance_video=SeedanceVideoToolConfig()),
        provider_configs={"volcengine": SimpleNamespace(api_key="ark-from-provider")},
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key == "ark-from-provider"
    assert tool.config is ctx.config.seedance_video


def test_create_ignores_other_vendor_providers(tmp_path: Path) -> None:
    """其他厂商（kling）的密钥不会串到 Seedance 工具上。"""
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(seedance_video=SeedanceVideoToolConfig()),
        provider_configs={"kling": SimpleNamespace(api_key="AK123:SK456")},
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key is None


def test_create_missing_provider_falls_back_to_none(tmp_path: Path) -> None:
    """厂商未配置时 create 不抛错，密钥解析交给 _api_key 的环境变量兜底。"""
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(seedance_video=SeedanceVideoToolConfig()),
        provider_configs={},
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key is None


def test_resolve_model_falls_back_on_foreign_vendor_names(tmp_path: Path) -> None:
    """旧统一入口时代的 config 可能残留可灵/MiniMax 模型名，统一回退默认。"""
    tool = _tool(tmp_path)
    # 残留其他厂商模型名 → 回退默认
    assert tool._resolve_model("kling-3.0") == "doubao-seedance-2-0-260128"
    assert tool._resolve_model("MiniMax-H3") == "doubao-seedance-2-0-260128"
    # 合法 Seedance 模型 / Endpoint ID 保留
    assert tool._resolve_model("doubao-seedance-2-5-260628") == "doubao-seedance-2-5-260628"
    assert tool._resolve_model("ep-20260101-verify") == "ep-20260101-verify"
    # 空值 → 配置默认
    assert tool._resolve_model(None) == "doubao-seedance-2-0-260128"
    assert tool._resolve_model("  ") == "doubao-seedance-2-0-260128"


def _fake_video_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """屏蔽 HTTP 与落盘，仅捕获创建任务的请求体。"""
    captured: dict[str, object] = {}

    async def fake_create(self, client, body):
        captured["body"] = body
        return "task-1"

    async def fake_poll(self, client, task_id):
        return {"content": {"video_url": "https://example.com/out.mp4"}}

    async def fake_download(client, video_url, *, workspace, save_dir, default_model, model=None):
        return {"path": str(tmp_path / "out.mp4"), "model": model or default_model}

    monkeypatch.setattr(SeedanceVideoTool, "_create_task", fake_create)
    monkeypatch.setattr(SeedanceVideoTool, "_poll_until_done", fake_poll)
    monkeypatch.setattr(
        "xianaibot.agent.tools.seedance_video.download_and_store", fake_download
    )
    return captured


@pytest.mark.asyncio
async def test_execute_drops_resolution_in_r2v_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r2v（带参考素材）模式下不发送 resolution；纯文生视频才保留。"""
    tool = _tool(tmp_path, api_key="ark-test")
    captured = _fake_video_flow(monkeypatch, tmp_path)

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
    captured = _fake_video_flow(monkeypatch, tmp_path)

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
    captured = _fake_video_flow(monkeypatch, tmp_path)

    await tool.execute(
        prompt="保持运镜不变",
        image_urls=["https://example.com/ref.jpg"],
    )
    body = captured["body"]
    assert body["content"][1]["role"] == "reference_image"
    assert "resolution" not in body


@pytest.mark.asyncio
async def test_execute_resolves_model_per_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按次传参的 model 覆盖默认模型；残留其他厂商模型名回退默认。"""
    tool = _tool(tmp_path, api_key="ark-test")
    captured = _fake_video_flow(monkeypatch, tmp_path)

    await tool.execute(prompt="一只橘猫弹钢琴", model="doubao-seedance-2-5-260628")
    assert captured["body"]["model"] == "doubao-seedance-2-5-260628"

    await tool.execute(prompt="一只橘猫弹钢琴", model="kling-3.0")
    assert captured["body"]["model"] == "doubao-seedance-2-0-260128"
