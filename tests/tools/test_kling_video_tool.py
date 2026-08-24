"""可灵（Kling）视频生成工具 / 客户端测试。

覆盖：JWT 构造、Authorization 两种形态、build_request 端点与参数映射、
create_task/poll/extract_video_url 的响应处理，以及 generate_video 工具在
``provider == "kling"`` 时的分流路径（volcengine 原路径零改动）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from biscuitbot.agent.tools.kling_video import (
    _DEFAULT_BASE_URL,
    _KLING_DEFAULT_MODEL,
    KlingVideoClient,
    KlingVideoError,
    build_kling_jwt,
    get_video_gen_provider,
    video_gen_provider_names,
)
from biscuitbot.agent.tools.seedance_video import (
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
    """记录 post/get 调用的极简 async httpx 替身。"""

    def __init__(self, post_response=None, get_response=None) -> None:
        self.post_response = post_response
        self.get_response = get_response
        self.posted: tuple | None = None

    async def post(self, url, headers=None, json=None):
        self.posted = (url, headers, json)
        return self.post_response

    async def get(self, url, headers=None):
        return self.get_response


# ---- JWT 与认证 --------------------------------------------------------------


def test_build_kling_jwt_three_parts_header_and_signature() -> None:
    ak, sk = "AK123", "SK456"
    token = build_kling_jwt(ak, sk)
    parts = token.split(".")
    assert len(parts) == 3

    def _b64url_decode(seg: str) -> dict:
        padded = seg + "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    header = _b64url_decode(parts[0])
    payload = _b64url_decode(parts[1])
    assert header == {"alg": "HS256", "typ": "JWT"}
    assert payload["iss"] == ak
    assert payload["exp"] - payload["nbf"] == 1805  # exp=now+1800, nbf=now-5

    # 签名必须能用 SecretKey 独立重算（服务端校验依据）
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    expected_sig = (
        base64.urlsafe_b64encode(
            hmac.new(sk.encode("utf-8"), signing_input, hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    assert parts[2] == expected_sig


def test_authorization_static_token_when_no_colon() -> None:
    client = KlingVideoClient(api_key="relay-token-abc")
    assert client._authorization() == "Bearer relay-token-abc"


def test_authorization_jwt_when_ak_sk_colon() -> None:
    client = KlingVideoClient(api_key="AK123:SK456")
    auth = client._authorization()
    assert auth.startswith("Bearer ")
    token = auth[len("Bearer "):]
    assert len(token.split(".")) == 3


def test_authorization_missing_key_raises() -> None:
    client = KlingVideoClient(api_key=None)
    with pytest.raises(KlingVideoError, match="未配置"):
        client._authorization()


# ---- build_request 端点与参数映射 ---------------------------------------------


def test_build_request_text2video() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    endpoint, body = client.build_request(
        prompt="一只橘猫弹钢琴",
        generate_audio=True,
        model=_KLING_DEFAULT_MODEL,
    )
    assert endpoint == f"/text-to-video/{_KLING_DEFAULT_MODEL}"
    assert body == {
        "contents": [{"type": "prompt", "text": "一只橘猫弹钢琴"}],
        "settings": {"audio": "on", "multi_shot": False},
        "options": {"watermark_info": {"enabled": False}},
    }


def test_build_request_image2video_first_and_end_frame() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    endpoint, body = client.build_request(
        prompt="让照片动起来",
        image_urls=["https://a.example/start.jpg", "https://a.example/end.jpg"],
    )
    assert endpoint == "/image-to-video/kling-3.0"
    assert body["contents"] == [
        {"type": "prompt", "text": "让照片动起来"},
        {"type": "first_frame", "url": "https://a.example/start.jpg"},
        {"type": "end_frame", "url": "https://a.example/end.jpg"},
    ]


def test_build_request_single_image_is_first_frame_only() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    _, body = client.build_request(prompt="动起来", image_urls=["https://a.example/one.jpg"])
    assert body["contents"] == [
        {"type": "prompt", "text": "动起来"},
        {"type": "first_frame", "url": "https://a.example/one.jpg"},
    ]


def test_build_request_video2video_uses_first_video_only() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    endpoint, body = client.build_request(
        prompt="把视频替换风格",
        video_urls=["https://v.example/a.mp4", "https://v.example/b.mp4"],
    )
    assert endpoint == "/video-to-video/kling-3.0"
    # 可灵每次任务最多 1 段参考视频
    assert body["contents"] == [
        {"type": "prompt", "text": "把视频替换风格"},
        {"type": "base_video", "url": "https://v.example/a.mp4"},
    ]


def test_build_request_discards_unsupported_ratio_and_clamps_duration() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    # 可灵不支持 4:3 → 丢弃 aspect_ratio；时长 99 → 截断到 15
    _, body = client.build_request(prompt="p", ratio="4:3", duration=99)
    assert "aspect_ratio" not in body["settings"]
    assert body["settings"]["duration"] == 15
    # 支持的 1:1 保留；时长 1 → 抬到 3
    _, body = client.build_request(prompt="p", ratio="1:1", duration=1)
    assert body["settings"]["aspect_ratio"] == "1:1"
    assert body["settings"]["duration"] == 3
    # 带参考视频时长上限 10
    _, body = client.build_request(prompt="p", video_urls=["https://v.example/a.mp4"], duration=99)
    assert body["settings"]["duration"] == 10
    # 音频开关与清晰度透传（转小写）
    _, body = client.build_request(
        prompt="p", generate_audio=False, resolution="4K"
    )
    assert body["settings"]["audio"] == "off"
    assert body["settings"]["resolution"] == "4k"


# ---- create_task / poll / extract_video_url ----------------------------------


@pytest.mark.asyncio
async def test_create_task_ok_builds_url_and_auth() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    http = FakeHttp(
        post_response=FakeResponse(
            200,
            {"code": 0, "message": "ok", "data": {"task_id": "t-1"}},
        )
    )
    task_id = await client.create_task(http, "/v1/videos/text2video", {"model_name": "m"})
    assert task_id == "t-1"
    url, headers, body = http.posted
    assert url == f"{_DEFAULT_BASE_URL}/v1/videos/text2video"
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"].startswith("Bearer ")
    assert body == {"model_name": "m"}


@pytest.mark.asyncio
async def test_create_task_nonzero_code_raises() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    http = FakeHttp(
        post_response=FakeResponse(200, {"code": 1001, "message": "invalid model"})
    )
    with pytest.raises(KlingVideoError, match="invalid model"):
        await client.create_task(http, "/v1/videos/text2video", {})


@pytest.mark.asyncio
async def test_create_task_http_error_raises() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    http = FakeHttp(
        post_response=FakeResponse(401, {"code": 1000, "message": "unauthorized"})
    )
    with pytest.raises(KlingVideoError, match="unauthorized"):
        await client.create_task(http, "/v1/videos/text2video", {})


@pytest.mark.asyncio
async def test_poll_succeed_and_extract_video_url() -> None:
    client = KlingVideoClient(api_key="AK:SK", poll_interval_sec=0.001)
    http = FakeHttp(
        get_response=FakeResponse(
            200,
            {
                "code": 0,
                "message": "ok",
                "data": {
                    "task_id": "t-1",
                    "task_status": "succeed",
                    "task_result": {"videos": [{"url": "https://cdn.example/out.mp4"}]},
                },
            },
        )
    )
    data = await client.poll(http, "t-1")
    assert data["data"]["task_status"] == "succeed"
    assert client.extract_video_url(data) == "https://cdn.example/out.mp4"


@pytest.mark.asyncio
async def test_poll_failed_raises_with_status_msg() -> None:
    client = KlingVideoClient(api_key="AK:SK", poll_interval_sec=0.001)
    http = FakeHttp(
        get_response=FakeResponse(
            200,
            {
                "code": 0,
                "message": "ok",
                "data": {"task_id": "t-1", "task_status": "failed", "task_status_msg": "内容违规"},
            },
        )
    )
    with pytest.raises(KlingVideoError, match="内容违规"):
        await client.poll(http, "t-1")


def test_extract_video_url_missing_raises() -> None:
    client = KlingVideoClient(api_key="AK:SK")
    with pytest.raises(KlingVideoError, match="未返回视频 URL"):
        client.extract_video_url(
            {"code": 0, "data": {"task_id": "t", "task_result": {"videos": []}}}
        )


# ---- 工具分流：kling vs volcengine -------------------------------------------


def test_registry_exposes_kling() -> None:
    assert "kling" in video_gen_provider_names()
    assert get_video_gen_provider("kling") is KlingVideoClient


def test_kling_model_fallback_off_seedance_names(tmp_path: Path) -> None:
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="kling"),
    )
    # 未传 model 且配置里仍是 Seedance 默认模型 → 回退可灵默认
    assert tool._kling_model(None) == _KLING_DEFAULT_MODEL
    assert tool._kling_model("doubao-seedance-2-0-260128") == _KLING_DEFAULT_MODEL
    # 显式自定义模型保留
    assert tool._kling_model("custom-kling-model") == "custom-kling-model"


def test_create_takes_kling_key_and_api_base(tmp_path: Path) -> None:
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(
            seedance_video=SeedanceVideoToolConfig(enabled=True, provider="kling")
        ),
        provider_configs={
            "kling": SimpleNamespace(
                api_key="AK123:SK456", api_base="https://relay.example/v1"
            )
        },
    )
    tool = SeedanceVideoTool.create(ctx)
    assert tool._ark_api_key == "AK123:SK456"
    assert tool.provider_api_base == "https://relay.example/v1"


@pytest.mark.asyncio
async def test_execute_dispatches_to_kling_path(tmp_path: Path, monkeypatch) -> None:
    """provider=kling 时 execute 走 _execute_kling，其余参数透传。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="kling", api_key="AK:SK"),
    )
    captured: dict = {}

    async def fake_kling(self, **kwargs):
        captured.update(kwargs)
        return '{"video": "ok"}'

    monkeypatch.setattr(SeedanceVideoTool, "_execute_kling", fake_kling)
    result = await tool.execute(prompt="hello", image_urls=["https://a.example/img.jpg"])
    assert json.loads(result) == {"video": "ok"}
    assert captured["prompt"] == "hello"
    assert captured["image_urls"] == ["https://a.example/img.jpg"]


@pytest.mark.asyncio
async def test_volcengine_path_does_not_call_kling(tmp_path: Path, monkeypatch) -> None:
    """默认厂商走方舟路径，_execute_kling 不应被调用（防回归）。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="volcengine", api_key="ark-test"),
    )

    def fail(**kwargs):
        raise AssertionError("_execute_kling 不应在 volcengine 路径被调用")

    async def fake_create(self, client, body):
        return "task-1"

    async def fake_poll(self, client, task_id):
        return {"content": {"video_url": "https://example.com/out.mp4"}}

    async def fake_download(self, client, video_url, **kwargs):
        return {"path": str(tmp_path / "out.mp4")}

    monkeypatch.setattr(SeedanceVideoTool, "_execute_kling", fail)
    monkeypatch.setattr(SeedanceVideoTool, "_create_task", fake_create)
    monkeypatch.setattr(SeedanceVideoTool, "_poll_until_done", fake_poll)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    result = await tool.execute(prompt="hello")
    assert result.startswith('{"video":')


@pytest.mark.asyncio
async def test_execute_kling_full_flow(tmp_path: Path, monkeypatch) -> None:
    """kling 全流程：建任务 → 轮询 → 取 URL → 落盘，返回 JSON 元数据。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="kling", api_key="AK:SK"),
    )
    captured: dict = {}

    async def fake_create(self, client, endpoint, body):
        captured["endpoint"] = endpoint
        captured["body"] = body
        return "kling-task"

    async def fake_poll(self, client, task_id):
        return {
            "code": 0,
            "data": {
                "task_id": task_id,
                "task_status": "succeed",
                "task_result": {"videos": [{"url": "https://cdn.example/out.mp4"}]},
            },
        }

    async def fake_download(self, client, video_url, *, model=None):
        return {"path": str(tmp_path / "out.mp4"), "model": model}

    monkeypatch.setattr(KlingVideoClient, "create_task", fake_create)
    monkeypatch.setattr(KlingVideoClient, "poll", fake_poll)
    monkeypatch.setattr(SeedanceVideoTool, "_download_and_store", fake_download)

    result = await tool.execute(
        prompt="让照片动起来",
        image_urls=["https://a.example/img.jpg"],
        ratio="16:9",
        duration=8,
        generate_audio=True,
    )
    data = json.loads(result)
    assert data["task_id"] == "kling-task"
    assert data["model"] == _KLING_DEFAULT_MODEL
    assert data["video"]["model"] == _KLING_DEFAULT_MODEL
    assert captured["endpoint"] == f"/image-to-video/{_KLING_DEFAULT_MODEL}"
    assert captured["body"]["contents"][1] == {
        "type": "first_frame",
        "url": "https://a.example/img.jpg",
    }
    assert captured["body"]["settings"]["aspect_ratio"] == "16:9"
    assert captured["body"]["settings"]["duration"] == 8
    assert captured["body"]["settings"]["audio"] == "on"


@pytest.mark.asyncio
async def test_execute_kling_missing_key_returns_error(tmp_path: Path) -> None:
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="kling"),
    )
    result = await tool.execute(prompt="hello")
    assert result.startswith("Error: 可灵 API key 未配置")


@pytest.mark.asyncio
async def test_execute_kling_rejects_local_image_path(tmp_path: Path) -> None:
    """可灵官方 image2video 不接受 base64/本地路径，需公网 URL，直接报错提示。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="kling", api_key="AK:SK"),
    )
    result = await tool.execute(prompt="hello", image_urls=[str(tmp_path / "cat.jpg")])
    assert result.startswith("Error:")
    assert "公网" in result


@pytest.mark.asyncio
async def test_execute_kling_poll_failure_returns_error(tmp_path: Path, monkeypatch) -> None:
    """轮询阶段失败时错误被捕获并以 Error: 前缀返回。"""
    tool = SeedanceVideoTool(
        workspace=tmp_path,
        config=SeedanceVideoToolConfig(enabled=True, provider="kling", api_key="AK:SK"),
    )

    async def fake_create(self, client, endpoint, body):
        return "kling-task"

    async def fake_poll(self, client, task_id):
        raise KlingVideoError("可灵任务失败：内容违规")

    monkeypatch.setattr(KlingVideoClient, "create_task", fake_create)
    monkeypatch.setattr(KlingVideoClient, "poll", fake_poll)

    result = await tool.execute(prompt="hello", image_urls=["https://a.example/img.jpg"])
    assert result.startswith("Error:")
    assert "内容违规" in result
