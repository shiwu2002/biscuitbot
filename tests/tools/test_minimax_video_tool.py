"""MiniMax H3 视频生成工具 / 客户端测试。

覆盖：base URL 归一化、build_request 的模式判定与角色映射、三道互斥/上限校验、
create_task 的 base_resp 业务错误、poll 的双路径双词表容错、file_id → download_url
换取，以及 generate_video 工具在 ``provider == "minimax"`` 时的分流路径
（volcengine / kling 原路径零改动）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianaibot.agent.tools import minimax_video
from xianaibot.agent.tools.kling_video import (
    get_video_gen_provider,
    video_gen_provider_names,
)
from xianaibot.agent.tools.minimax_video import (
    _DEFAULT_BASE_URL,
    _MINIMAX_DEFAULT_MODEL,
    MiniMaxVideoClient,
    MiniMaxVideoError,
    _normalize_api_base,
)
from xianaibot.agent.tools.seedance_video import (
    SeedanceVideoTool,
    SeedanceVideoToolConfig,
)

# ---- Fake HTTP 层 ------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8") if payload is not None else b""

    def json(self) -> dict:
        return self._payload or {}


class FakeHttp:
    """记录 post/get 调用的极简 async httpx 替身。

    ``get_responses`` 按调用序依次弹出（末项会被重复返回），用于覆盖
    轮询的 404 回退与「先 Preparing 后 Success」这类多轮场景。
    """

    def __init__(self, post_response=None, get_responses=None) -> None:
        self.post_response = post_response
        self.get_responses = list(get_responses or [])
        self.posted: tuple | None = None
        self.getted: list[tuple] = []
        self._index = 0

    async def post(self, url, headers=None, json=None):
        self.posted = (url, headers, json)
        return self.post_response

    async def get(self, url, headers=None):
        self.getted.append((url, headers))
        if not self.get_responses:
            return None
        index = min(self._index, len(self.get_responses) - 1)
        self._index += 1
        return self.get_responses[index]


def _tool(tmp_path: Path, **overrides) -> SeedanceVideoTool:
    config = SeedanceVideoToolConfig(
        enabled=True, provider="minimax", api_key="mm-test", **overrides
    )
    return SeedanceVideoTool(workspace=tmp_path, config=config)


# ---- 注册与 base URL ---------------------------------------------------------


def test_registry_exposes_minimax() -> None:
    assert "minimax" in video_gen_provider_names()
    assert get_video_gen_provider("minimax") is MiniMaxVideoClient


def test_default_base_url_callable_classmethod_style() -> None:
    """settings_api 以 ``cls._default_base_url(cls)`` 调用，故必须是普通实例方法。"""
    assert MiniMaxVideoClient._default_base_url(MiniMaxVideoClient) == _DEFAULT_BASE_URL
    assert MiniMaxVideoClient(api_key="k")._default_base_url() == _DEFAULT_BASE_URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, _DEFAULT_BASE_URL),
        ("", _DEFAULT_BASE_URL),
        ("   ", _DEFAULT_BASE_URL),
        ("https://api.minimaxi.com", "https://api.minimaxi.com"),
        ("https://api.minimaxi.com/", "https://api.minimaxi.com"),
        ("https://api.minimaxi.com/v1", "https://api.minimaxi.com"),
        ("https://api.minimax.io/anthropic/v1", "https://api.minimax.io"),
        ("https://relay.example/proxy/v1", "https://relay.example/proxy"),
    ],
)
def test_normalize_api_base(raw: str | None, expected: str) -> None:
    assert _normalize_api_base(raw) == expected


def test_init_uses_normalized_base() -> None:
    assert MiniMaxVideoClient(api_key="k", api_base="https://api.minimaxi.com/v1").api_base == (
        _DEFAULT_BASE_URL
    )
    assert MiniMaxVideoClient(api_key="k", api_base="").api_base == _DEFAULT_BASE_URL


def test_authorization_bearer_and_missing_key() -> None:
    assert MiniMaxVideoClient(api_key="mm-1")._authorization() == "Bearer mm-1"
    with pytest.raises(MiniMaxVideoError, match="未配置"):
        MiniMaxVideoClient(api_key=None)._authorization()


# ---- build_request：模式判定与参数映射 ----------------------------------------


def test_build_request_t2va_defaults_ratio_16_9_and_rejects_adaptive() -> None:
    client = MiniMaxVideoClient(api_key="k")
    endpoint, body = client.build_request(prompt="一只橘猫弹钢琴")
    assert endpoint == "/v2/video_generation"
    assert body["model"] == _MINIMAX_DEFAULT_MODEL
    assert body["content"] == [{"type": "text", "text": "一只橘猫弹钢琴"}]
    # 文生视频必填且不接受 adaptive → 回落到 16:9
    assert body["ratio"] == "16:9"
    assert body["resolution"] == "768P"
    assert body["aigc_watermark"] is False
    assert "_" not in body  # 无多余字段（如 seed / generate_audio）
    _, adaptive = client.build_request(prompt="p", ratio="adaptive")
    assert adaptive["ratio"] == "16:9"
    _, explicit = client.build_request(prompt="p", ratio="9:16")
    assert explicit["ratio"] == "9:16"


def test_build_request_i2va_single_image_is_first_frame_and_omits_ratio() -> None:
    client = MiniMaxVideoClient(api_key="k")
    _, body = client.build_request(
        prompt="让照片动起来",
        image_urls=["https://a.example/one.png"],
        ratio="9:16",
    )
    assert body["content"] == [
        {"type": "text", "text": "让照片动起来"},
        {"type": "image_url", "image_url": {"url": "https://a.example/one.png"}, "role": "first_frame"},
    ]
    # 图生视频由首帧推导画幅，传了也忽略
    assert "ratio" not in body


def test_build_request_i2va_two_images_are_first_and_last_frame() -> None:
    client = MiniMaxVideoClient(api_key="k")
    _, body = client.build_request(
        prompt="p",
        image_urls=["https://a.example/first.png", "https://a.example/last.png"],
    )
    roles = [item.get("role") for item in body["content"][1:]]
    assert roles == ["first_frame", "last_frame"]


def test_build_request_r2va_roles_and_ratio_passthrough() -> None:
    client = MiniMaxVideoClient(api_key="k")
    _, body = client.build_request(
        prompt="p",
        reference_images=["https://a.example/char.png"],
        video_urls=["https://v.example/motion.mp4"],
        audio_urls=["https://a.example/voice.mp3"],
        ratio="21:9",
    )
    assert body["content"][1:] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://a.example/char.png"},
            "role": "reference_image",
        },
        {
            "type": "video_url",
            "video_url": {"url": "https://v.example/motion.mp4"},
            "role": "reference_video",
        },
        {
            "type": "audio_url",
            "audio_url": {"url": "https://a.example/voice.mp3"},
            "role": "reference_audio",
        },
    ]
    # r2va 下 ratio 可选，合法值原样下发
    assert body["ratio"] == "21:9"


def test_build_request_r2va_drops_unsupported_ratio() -> None:
    client = MiniMaxVideoClient(api_key="k")
    _, body = client.build_request(
        prompt="p", reference_images=["https://a.example/a.png"], ratio="16:10"
    )
    assert "ratio" not in body


def test_build_request_frames_and_references_mutually_exclusive() -> None:
    client = MiniMaxVideoClient(api_key="k")
    with pytest.raises(MiniMaxVideoError, match="互斥"):
        client.build_request(
            prompt="p",
            image_urls=["https://a.example/first.png"],
            reference_images=["https://a.example/ref.png"],
        )
    with pytest.raises(MiniMaxVideoError, match="互斥"):
        client.build_request(
            prompt="p",
            image_urls=["https://a.example/first.png"],
            video_urls=["https://v.example/a.mp4"],
        )


def test_build_request_audio_cannot_be_used_alone() -> None:
    client = MiniMaxVideoClient(api_key="k")
    with pytest.raises(MiniMaxVideoError, match="参考音频不能单独使用"):
        client.build_request(prompt="p", audio_urls=["https://a.example/v.mp3"])


def test_build_request_enforces_per_kind_and_total_limits() -> None:
    client = MiniMaxVideoClient(api_key="k")
    with pytest.raises(MiniMaxVideoError, match="首帧/尾帧最多 2 张"):
        client.build_request(prompt="p", image_urls=[f"https://a.example/{i}.png" for i in range(3)])
    with pytest.raises(MiniMaxVideoError, match="参考图最多 9 张"):
        client.build_request(
            prompt="p", reference_images=[f"https://a.example/{i}.png" for i in range(10)]
        )
    with pytest.raises(MiniMaxVideoError, match="参考视频最多 3 段"):
        client.build_request(
            prompt="p",
            reference_images=["https://a.example/a.png"],
            video_urls=[f"https://v.example/{i}.mp4" for i in range(4)],
        )
    with pytest.raises(MiniMaxVideoError, match="参考音频最多 3 段"):
        client.build_request(
            prompt="p",
            reference_images=["https://a.example/a.png"],
            audio_urls=[f"https://a.example/{i}.mp3" for i in range(4)],
        )
    # 9 图 + 3 视频 + 3 音频 = 15 个文件 > 12
    with pytest.raises(MiniMaxVideoError, match="文件总数最多 12 个"):
        client.build_request(
            prompt="p",
            reference_images=[f"https://a.example/{i}.png" for i in range(9)],
            video_urls=[f"https://v.example/{i}.mp4" for i in range(3)],
            audio_urls=[f"https://a.example/{i}.mp3" for i in range(3)],
        )


def test_build_request_body_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求体超 64MB 上限时提前报错（不发 HTTP）。"""
    monkeypatch.setattr(minimax_video, "_MAX_BODY_BYTES", 10)
    client = MiniMaxVideoClient(api_key="k")
    with pytest.raises(MiniMaxVideoError, match="请求体过大"):
        client.build_request(prompt="x" * 100)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "768P"),
        ("", "768P"),
        ("768P", "768P"),
        ("2K", "2K"),
        ("480p", "768P"),
        ("720p", "768P"),
        ("1080p", "2K"),
        ("4K", "2K"),
    ],
)
def test_build_request_resolution_aliases(raw: str | None, expected: str) -> None:
    client = MiniMaxVideoClient(api_key="k")
    _, body = client.build_request(prompt="p", resolution=raw)
    assert body["resolution"] == expected


def test_build_request_unknown_resolution_raises() -> None:
    client = MiniMaxVideoClient(api_key="k")
    with pytest.raises(MiniMaxVideoError, match="不支持的清晰度"):
        client.build_request(prompt="p", resolution="8K")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 5), (1, 4), (4, 4), (15, 15), (30, 15)],
)
def test_build_request_duration_clamped(raw: int | None, expected: int) -> None:
    client = MiniMaxVideoClient(api_key="k")
    _, body = client.build_request(prompt="p", duration=raw)
    assert body["duration"] == expected


def test_build_request_watermark_maps_to_aigc_watermark() -> None:
    client = MiniMaxVideoClient(api_key="k")
    _, body = client.build_request(prompt="p", watermark=True)
    assert body["aigc_watermark"] is True


# ---- create_task -------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_ok_top_level_task_id() -> None:
    client = MiniMaxVideoClient(api_key="k")
    http = FakeHttp(post_response=FakeResponse(200, {"task_id": "t-1"}))
    task_id = await client.create_task(http, "/v2/video_generation", {"model": "MiniMax-H3"})
    assert task_id == "t-1"
    url, headers, body = http.posted
    assert url == f"{_DEFAULT_BASE_URL}/v2/video_generation"
    assert headers["Authorization"] == "Bearer k"
    assert headers["Content-Type"] == "application/json"
    assert body == {"model": "MiniMax-H3"}


@pytest.mark.asyncio
async def test_create_task_ok_nested_data_task_id() -> None:
    client = MiniMaxVideoClient(api_key="k")
    http = FakeHttp(post_response=FakeResponse(200, {"data": {"task_id": "t-2"}}))
    assert await client.create_task(http, "/v2/video_generation", {}) == "t-2"


@pytest.mark.asyncio
async def test_create_task_http_200_with_base_resp_error_raises() -> None:
    """HTTP 200 也可能带业务错误体（如 2013 参数非法），必须单独检查。"""
    client = MiniMaxVideoClient(api_key="k")
    http = FakeHttp(
        post_response=FakeResponse(
            200,
            {"base_resp": {"status_code": 2013, "status_msg": "invalid parameter"}},
        )
    )
    with pytest.raises(MiniMaxVideoError, match="2013"):
        await client.create_task(http, "/v2/video_generation", {})


@pytest.mark.asyncio
async def test_create_task_base_resp_zero_is_ok_and_http_error_raises() -> None:
    client = MiniMaxVideoClient(api_key="k")
    ok = FakeHttp(
        post_response=FakeResponse(
            200,
            {
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "task_id": "t-3",
            },
        )
    )
    assert await client.create_task(ok, "/v2/video_generation", {}) == "t-3"

    bad = FakeHttp(
        post_response=FakeResponse(
            401, {"base_resp": {"status_code": 1004, "status_msg": "invalid api key"}}
        )
    )
    with pytest.raises(MiniMaxVideoError, match="invalid api key"):
        await client.create_task(bad, "/v2/video_generation", {})


# ---- poll --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_falls_back_to_second_path_on_404_and_remembers_it() -> None:
    """轮询端点有两种文档说法：首个 404 时改试第二个，并固定使用胜出者。"""
    client = MiniMaxVideoClient(api_key="k", poll_interval_sec=0.001)
    http = FakeHttp(
        get_responses=[
            FakeResponse(404, {"base_resp": {"status_code": 1002, "status_msg": "not found"}}),
            FakeResponse(200, {"task_id": "t-1", "status": "Processing"}),
            FakeResponse(200, {"task_id": "t-1", "status": "Success", "file_id": "f-1"}),
        ]
    )
    data = await client.poll(http, "t-1")
    assert data["file_id"] == "f-1"
    urls = [url for url, _ in http.getted]
    assert urls[0].endswith("/v1/query/video_generation?task_id=t-1")
    # 命中后固定用第二个候选，不再回退探测
    assert urls[1].endswith("/v2/query/video_generation/t-1")
    assert urls[2].endswith("/v2/query/video_generation/t-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Success", "succeeded", "COMPLETED", "finished"])
async def test_poll_accepts_both_status_vocabularies(status: str) -> None:
    client = MiniMaxVideoClient(api_key="k", poll_interval_sec=0.001)
    http = FakeHttp(get_responses=[FakeResponse(200, {"status": status, "file_id": "f-1"})])
    assert (await client.poll(http, "t-1"))["file_id"] == "f-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Preparing", "queued", "running", "in_progress"])
async def test_poll_keeps_polling_on_pending_status(status: str) -> None:
    client = MiniMaxVideoClient(api_key="k", poll_interval_sec=0.001, max_poll_attempts=2)
    http = FakeHttp(
        get_responses=[
            FakeResponse(200, {"status": status}),
            FakeResponse(200, {"status": "Success", "file_id": "f-1"}),
        ]
    )
    assert (await client.poll(http, "t-1"))["file_id"] == "f-1"


@pytest.mark.asyncio
async def test_poll_unknown_status_keeps_polling_then_times_out() -> None:
    """未知状态一律继续轮询（宁可超时也不误判为终点）。"""
    client = MiniMaxVideoClient(api_key="k", poll_interval_sec=0.001, max_poll_attempts=2)
    http = FakeHttp(get_responses=[FakeResponse(200, {"status": "weird-new-state"})])
    with pytest.raises(MiniMaxVideoError, match="超时"):
        await client.poll(http, "t-1")


@pytest.mark.asyncio
async def test_poll_failed_raises_with_status_msg() -> None:
    client = MiniMaxVideoClient(api_key="k", poll_interval_sec=0.001)
    http = FakeHttp(
        get_responses=[
            FakeResponse(200, {"data": {"status": "Failed", "status_msg": "内容违规"}})
        ]
    )
    with pytest.raises(MiniMaxVideoError, match="内容违规"):
        await client.poll(http, "t-1")


# ---- file_id → download_url --------------------------------------------------


def test_extract_file_id_nested_shapes() -> None:
    assert MiniMaxVideoClient.extract_file_id({"file_id": "f-1"}) == "f-1"
    assert MiniMaxVideoClient.extract_file_id({"data": {"file": {"file_id": "f-2"}}}) == "f-2"
    with pytest.raises(MiniMaxVideoError, match="未返回 file_id"):
        MiniMaxVideoClient.extract_file_id({"data": {"status": "Success"}})


def test_extract_direct_url_ignores_echoed_reference_urls() -> None:
    """任务响应常回显输入素材；裸 url 不在候选 key 内，避免下载到参考图/参考视频。"""
    assert MiniMaxVideoClient.extract_direct_url({"download_url": "https://cdn/a.mp4"}) == (
        "https://cdn/a.mp4"
    )
    echo = {
        "content": [
            {"type": "video_url", "video_url": {"url": "https://ref.example/in.mp4"}},
            {"type": "image_url", "image_url": {"url": "https://ref.example/in.png"}},
        ]
    }
    assert MiniMaxVideoClient.extract_direct_url(echo) is None
    # 非 HTTP 值也不接受
    assert MiniMaxVideoClient.extract_direct_url({"download_url": "not-a-url"}) is None


@pytest.mark.asyncio
async def test_retrieve_file_url_returns_download_url() -> None:
    client = MiniMaxVideoClient(api_key="k")
    http = FakeHttp(
        get_responses=[
            FakeResponse(
                200,
                {
                    "base_resp": {"status_code": 0},
                    "file": {"download_url": "https://cdn.example/out.mp4"},
                },
            )
        ]
    )
    url = await client.retrieve_file_url(http, "f-1")
    assert url == "https://cdn.example/out.mp4"
    assert http.getted[0][0].endswith("/v1/files/retrieve?file_id=f-1")


@pytest.mark.asyncio
async def test_retrieve_file_url_base_resp_error_raises() -> None:
    client = MiniMaxVideoClient(api_key="k")
    http = FakeHttp(
        get_responses=[
            FakeResponse(
                200, {"base_resp": {"status_code": 1002, "status_msg": "file not found"}}
            )
        ]
    )
    with pytest.raises(MiniMaxVideoError, match="file not found"):
        await client.retrieve_file_url(http, "f-1")


# ---- 工具分流：minimax vs volcengine / kling ---------------------------------


def test_minimax_model_fallback_off_seedance_names(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    assert tool._minimax_model(None) == _MINIMAX_DEFAULT_MODEL
    assert tool._minimax_model("doubao-seedance-2-5-260628") == _MINIMAX_DEFAULT_MODEL
    assert tool._minimax_model("custom-h3") == "custom-h3"


def test_minimax_key_prefers_provider_page_over_stale_tool_key(tmp_path: Path) -> None:
    """切厂商后 config.api_key 可能残留方舟 key，必须优先用「模型厂商」页的密钥。"""
    config = SeedanceVideoToolConfig(enabled=True, provider="minimax", api_key="ark-stale")
    tool = SeedanceVideoTool(
        workspace=tmp_path, config=config, ark_api_key="mm-fresh", provider_api_base=""
    )
    assert tool._resolve_minimax_key() == "mm-fresh"
    # 无「模型厂商」页密钥时回退配置值
    bare = SeedanceVideoTool(workspace=tmp_path, config=config)
    assert bare._resolve_minimax_key() == "ark-stale"
    # 两者皆空 → 报错
    empty = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="minimax"),
    )
    with pytest.raises(MiniMaxVideoError, match="未配置"):
        empty._resolve_minimax_key()


def test_minimax_api_base_ignores_stale_ark_base_url(tmp_path: Path) -> None:
    """config.base_url 是方舟工具级地址，MiniMax 路径必须忽略它。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(
            enabled=True, provider="minimax", base_url="https://ark.cn-beijing.volces.com/api/v3"
        ),
        provider_api_base="https://api.minimaxi.com/v1",
    )
    assert tool._minimax_api_base() == "https://api.minimaxi.com/v1"
    # 未配置 apiBase → 空串，由客户端归一化为官方默认
    bare = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="minimax"),
    )
    assert bare._minimax_api_base() == ""
    assert MiniMaxVideoClient(api_key="k", api_base=bare._minimax_api_base()).api_base == (
        _DEFAULT_BASE_URL
    )


def test_create_takes_minimax_key_and_api_base(tmp_path: Path) -> None:
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(
            seedance_video=SeedanceVideoToolConfig(enabled=True, provider="minimax")
        ),
        provider_configs={
            "minimax": SimpleNamespace(api_key="mm-1", api_base="https://api.minimaxi.com/v1")
        },
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key == "mm-1"
    assert tool.provider_api_base == "https://api.minimaxi.com/v1"


@pytest.mark.asyncio
async def test_execute_dispatches_to_minimax_path(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path)
    captured: dict = {}

    async def fake_minimax(self, **kwargs):
        captured.update(kwargs)
        return '{"video": "ok"}'

    monkeypatch.setattr(SeedanceVideoTool, "_execute_minimax", fake_minimax)
    result = await tool.execute(
        prompt="hello",
        image_urls=["https://a.example/first.png"],
        reference_images=["https://a.example/ref.png"],
        watermark=True,
        seed=7,
    )
    assert json.loads(result) == {"video": "ok"}
    assert captured["prompt"] == "hello"
    assert captured["reference_images"] == ["https://a.example/ref.png"]
    assert captured["seed"] == 7
    assert captured["watermark"] is True


@pytest.mark.asyncio
async def test_volcengine_path_does_not_call_minimax(tmp_path: Path, monkeypatch) -> None:
    """默认厂商走方舟路径，_execute_minimax 不应被调用（防回归）。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(
            enabled=True, provider="volcengine", api_key="ark-test"
        ),
    )

    def fail(**kwargs):
        raise AssertionError("_execute_minimax 不应在 volcengine 路径被调用")

    async def fake_create(self, client, body):
        return "task-1"

    async def fake_poll(self, client, task_id):
        return {"content": {"video_url": "https://example.com/out.mp4"}}

    async def fake_download(self, client, video_url, **kwargs):
        return {"path": str(tmp_path / "out.mp4")}

    monkeypatch.setattr(SeedanceVideoTool, "_execute_minimax", fail)
    monkeypatch.setattr(SeedanceVideoTool, "_create_task", fake_create)
    monkeypatch.setattr(SeedanceVideoTool, "_poll_until_done", fake_poll)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    result = await tool.execute(prompt="hello")
    assert result.startswith('{"video":')


@pytest.mark.asyncio
async def test_kling_path_does_not_call_minimax(tmp_path: Path, monkeypatch) -> None:
    """provider=kling 时不应落到 MiniMax 分支，且 reference_images 被忽略。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="kling", api_key="AK:SK"),
    )
    captured: dict = {}

    async def fake_kling(self, **kwargs):
        captured.update(kwargs)
        return '{"video": "kling"}'

    def fail(**kwargs):
        raise AssertionError("_execute_minimax 不应在 kling 路径被调用")

    monkeypatch.setattr(SeedanceVideoTool, "_execute_kling", fake_kling)
    monkeypatch.setattr(SeedanceVideoTool, "_execute_minimax", fail)

    result = await tool.execute(prompt="hello", reference_images=["https://a.example/ref.png"])
    assert json.loads(result) == {"video": "kling"}
    # reference_images 不进入可灵路径（不写进 kling 参数）
    assert "reference_images" not in captured


@pytest.mark.asyncio
async def test_execute_minimax_full_flow(tmp_path: Path, monkeypatch) -> None:
    """minimax 全流程：建任务 → 轮询 → file_id 换 URL → 落盘，返回 JSON 元数据。"""
    tool = _tool(tmp_path)
    captured: dict = {}

    async def fake_create(self, client, endpoint, body):
        captured["endpoint"] = endpoint
        captured["body"] = body
        return "mm-task"

    async def fake_poll(self, client, task_id, **kwargs):
        return {"task_id": task_id, "status": "Success", "file_id": "f-1"}

    async def fake_retrieve(self, client, file_id):
        captured["file_id"] = file_id
        return "https://cdn.example/out.mp4"

    async def fake_download(self, client, video_url, *, model=None):
        captured["download_url"] = video_url
        return {"path": str(tmp_path / "out.mp4"), "model": model}

    monkeypatch.setattr(MiniMaxVideoClient, "create_task", fake_create)
    monkeypatch.setattr(MiniMaxVideoClient, "poll", fake_poll)
    monkeypatch.setattr(MiniMaxVideoClient, "retrieve_file_url", fake_retrieve)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    result = await tool.execute(
        prompt="让照片动起来",
        image_urls=["https://a.example/first.png"],
        duration=8,
    )
    data = json.loads(result)
    assert data["task_id"] == "mm-task"
    assert data["model"] == _MINIMAX_DEFAULT_MODEL
    assert data["video"]["model"] == _MINIMAX_DEFAULT_MODEL
    assert captured["endpoint"] == "/v2/video_generation"
    assert captured["file_id"] == "f-1"
    assert captured["download_url"] == "https://cdn.example/out.mp4"
    assert captured["body"]["content"][1]["role"] == "first_frame"
    assert captured["body"]["duration"] == 8


@pytest.mark.asyncio
async def test_execute_minimax_prefers_direct_url_over_file_id(
    tmp_path: Path, monkeypatch
) -> None:
    """部分网关直接返回视频 URL 时跳过 file_id 换取。"""
    tool = _tool(tmp_path)
    captured: dict = {}

    async def fake_create(self, client, endpoint, body):
        return "mm-task"

    async def fake_poll(self, client, task_id, **kwargs):
        return {"task_id": task_id, "status": "Success", "download_url": "https://cdn.example/d.mp4"}

    async def fake_retrieve(self, client, file_id):
        raise AssertionError("已有直接 URL 时不应再换 file_id")

    async def fake_download(self, client, video_url, *, model=None):
        captured["download_url"] = video_url
        return {"path": str(tmp_path / "out.mp4"), "model": model}

    monkeypatch.setattr(MiniMaxVideoClient, "create_task", fake_create)
    monkeypatch.setattr(MiniMaxVideoClient, "poll", fake_poll)
    monkeypatch.setattr(MiniMaxVideoClient, "retrieve_file_url", fake_retrieve)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    await tool.execute(prompt="hello")
    assert captured["download_url"] == "https://cdn.example/d.mp4"


@pytest.mark.asyncio
async def test_execute_minimax_local_image_and_audio_auto_base64(
    tmp_path: Path, monkeypatch
) -> None:
    """本地参考图/音频自动转 base64 data URL；参考视频仍仅接受公网 URL。"""
    tool = _tool(tmp_path)
    img = tmp_path / "cat.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64)  # JPEG 魔数
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 32)
    captured: dict = {}

    async def fake_create(self, client, endpoint, body):
        captured["body"] = body
        return "mm-task"

    async def fake_poll(self, client, task_id, **kwargs):
        return {"status": "Success", "file_id": "f-1"}

    async def fake_retrieve(self, client, file_id):
        return "https://cdn.example/out.mp4"

    async def fake_download(self, client, video_url, *, model=None):
        return {"path": str(tmp_path / "out.mp4"), "model": model}

    monkeypatch.setattr(MiniMaxVideoClient, "create_task", fake_create)
    monkeypatch.setattr(MiniMaxVideoClient, "poll", fake_poll)
    monkeypatch.setattr(MiniMaxVideoClient, "retrieve_file_url", fake_retrieve)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    result = await tool.execute(
        prompt="hello",
        reference_images=[str(img)],
        audio_urls=[str(audio)],
    )
    assert json.loads(result)["video"]["model"] == _MINIMAX_DEFAULT_MODEL
    items = captured["body"]["content"][1:]
    assert items[0]["role"] == "reference_image"
    assert items[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert items[1]["role"] == "reference_audio"
    assert items[1]["audio_url"]["url"].startswith("data:audio/mpeg;base64,")

    # 本地参考视频仍被拒绝（与另两个厂商一致，不新开能力承诺）
    local_video = tmp_path / "clip.mp4"
    local_video.write_bytes(b"\x00" * 16)
    err = await tool.execute(
        prompt="hello",
        reference_images=[str(img)],
        video_urls=[str(local_video)],
    )
    assert err.startswith("Error:")
    assert "公网 HTTP(S) URL" in err


@pytest.mark.asyncio
async def test_execute_minimax_validation_error_returned_as_error_string(
    tmp_path: Path,
) -> None:
    """互斥/超限校验失败必须返回 ``Error: ...`` 字符串而不是抛异常。"""
    tool = _tool(tmp_path)
    result = await tool.execute(
        prompt="hello",
        image_urls=["https://a.example/first.png"],
        reference_images=["https://a.example/ref.png"],
    )
    assert result.startswith("Error:")
    assert "互斥" in result

    only_audio = await tool.execute(prompt="hello", audio_urls=["https://a.example/v.mp3"])
    assert only_audio.startswith("Error:")
    assert "参考音频不能单独使用" in only_audio


@pytest.mark.asyncio
async def test_execute_minimax_missing_key_returns_error(tmp_path: Path) -> None:
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="minimax"),
    )
    result = await tool.execute(prompt="hello")
    assert result.startswith("Error: MiniMax API key 未配置")


@pytest.mark.asyncio
async def test_execute_minimax_poll_failure_returns_error(
    tmp_path: Path, monkeypatch
) -> None:
    tool = _tool(tmp_path)

    async def fake_create(self, client, endpoint, body):
        return "mm-task"

    async def fake_poll(self, client, task_id, **kwargs):
        raise MiniMaxVideoError("MiniMax 任务失败：内容违规")

    monkeypatch.setattr(MiniMaxVideoClient, "create_task", fake_create)
    monkeypatch.setattr(MiniMaxVideoClient, "poll", fake_poll)

    result = await tool.execute(prompt="hello")
    assert result.startswith("Error:")
    assert "内容违规" in result
