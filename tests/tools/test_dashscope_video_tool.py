"""通义万相（阿里灵积 DashScope）视频生成工具 / 客户端测试。

覆盖：base URL 归一化（含兼容模式 base 剥离）、build_request 的模式判定与
「清晰度+比例→像素对」映射、按时长按模型收敛、create_task 的异步头与 task_id
提取、poll 的状态词表容错、extract_video_url，以及厂商自包含的
``generate_video_dashscope`` 工具（注册、启用门控、密钥解析、模型名残留回退、
execute 全流程与错误路径）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianaibot.agent.tools.dashscope_video import (
    _DEFAULT_BASE_URL,
    _DEFAULT_MODEL,
    _RESOLUTION_DEFAULT,
    _SUBMIT_PATH,
    _TASK_PATH,
    DashScopeVideoClient,
    DashScopeVideoError,
    DashScopeVideoTool,
    DashScopeVideoToolConfig,
    _normalize_api_base,
    _resolve_duration,
)
from xianaibot.agent.tools.kling_video import (
    get_video_gen_provider,
    video_gen_provider_names,
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

    ``get_responses`` 按调用序依次弹出（末项会被重复返回），用于覆盖轮询的
    HTTP 错误重试与「先 RUNNING 后 SUCCEEDED」这类多轮场景。
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


def _tool(tmp_path: Path, **overrides) -> DashScopeVideoTool:
    config = DashScopeVideoToolConfig(api_key="ds-test", **overrides)
    return DashScopeVideoTool(workspace=tmp_path, config=config)


# ---- 注册与 base URL ---------------------------------------------------------


def test_registry_exposes_dashscope() -> None:
    assert "dashscope" in video_gen_provider_names()
    assert get_video_gen_provider("dashscope") is DashScopeVideoClient


def test_default_base_url_callable_classmethod_style() -> None:
    """settings_api 以 ``cls._default_base_url(cls)`` 调用，故必须是普通实例方法。"""
    assert DashScopeVideoClient._default_base_url(DashScopeVideoClient) == _DEFAULT_BASE_URL
    assert DashScopeVideoClient(api_key="k")._default_base_url() == _DEFAULT_BASE_URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, _DEFAULT_BASE_URL),
        ("", _DEFAULT_BASE_URL),
        ("   ", _DEFAULT_BASE_URL),
        (_DEFAULT_BASE_URL, _DEFAULT_BASE_URL),
        (_DEFAULT_BASE_URL + "/", _DEFAULT_BASE_URL),
        # 「模型厂商」页 dashscope 的 LLM 兼容模式 base 必须收敛到裸域
        (_DEFAULT_BASE_URL + "/compatible-mode/v1", _DEFAULT_BASE_URL),
        (_DEFAULT_BASE_URL + "/api/v1", _DEFAULT_BASE_URL),
        (_DEFAULT_BASE_URL + _SUBMIT_PATH, _DEFAULT_BASE_URL),
        (_DEFAULT_BASE_URL + _TASK_PATH, _DEFAULT_BASE_URL),
        # 自定义网关：未知路径前缀原样保留
        ("https://relay.example/proxy", "https://relay.example/proxy"),
    ],
)
def test_normalize_api_base(raw: str | None, expected: str) -> None:
    assert _normalize_api_base(raw) == expected


def test_init_uses_normalized_base() -> None:
    compat = _DEFAULT_BASE_URL + "/compatible-mode/v1"
    assert DashScopeVideoClient(api_key="k", api_base=compat).api_base == _DEFAULT_BASE_URL
    assert DashScopeVideoClient(api_key="k", api_base="").api_base == _DEFAULT_BASE_URL


def test_authorization_bearer_and_missing_key() -> None:
    assert DashScopeVideoClient(api_key="ds-1")._authorization() == "Bearer ds-1"
    # client 只认显式传入的 key，不读环境变量
    with pytest.raises(DashScopeVideoError, match="未配置"):
        DashScopeVideoClient(api_key=None)._authorization()


# ---- build_request：模式判定与参数映射 ----------------------------------------


def test_build_request_t2v_defaults() -> None:
    client = DashScopeVideoClient(api_key="k")
    endpoint, body = client.build_request(prompt="一只橘猫弹钢琴")
    assert endpoint == _SUBMIT_PATH
    assert body["model"] == _DEFAULT_MODEL
    assert body["input"] == {"prompt": "一只橘猫弹钢琴"}
    assert body["parameters"]["size"] == "1280*720"  # 720P + 16:9
    assert body["parameters"]["duration"] == 5
    assert body["parameters"]["prompt_extend"] is True
    assert body["parameters"]["watermark"] is False
    assert "seed" not in body["parameters"]


def test_build_request_i2v_single_frame_and_size() -> None:
    client = DashScopeVideoClient(api_key="k")
    _, body = client.build_request(
        prompt="让照片动起来",
        image_urls=["https://a.example/one.png"],
        ratio="9:16",
        resolution="1080P",
    )
    assert body["input"]["img_url"] == "https://a.example/one.png"
    assert body["parameters"]["size"] == "1080*1920"


def test_build_request_rejects_multiple_frames() -> None:
    client = DashScopeVideoClient(api_key="k")
    with pytest.raises(DashScopeVideoError, match="单张首帧"):
        client.build_request(
            prompt="p",
            image_urls=["https://a.example/a.png", "https://a.example/b.png"],
        )


@pytest.mark.parametrize("ratio", [None, "", "adaptive", "16:10"])
def test_build_request_ratio_falls_back_to_16_9(ratio: str | None) -> None:
    client = DashScopeVideoClient(api_key="k")
    _, body = client.build_request(prompt="p", ratio=ratio)
    assert body["parameters"]["size"].endswith("*720")


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        (None, "1280*720"),
        ("", "1280*720"),
        ("720P", "1280*720"),
        ("720p", "1280*720"),
        ("480P", "832*480"),
        ("1080p", "1920*1080"),
        ("4K", "1920*1080"),  # 4K 归入 1080P
    ],
)
def test_build_request_resolution_aliases(resolution: str | None, expected: str) -> None:
    client = DashScopeVideoClient(api_key="k")
    _, body = client.build_request(prompt="p", resolution=resolution)
    assert body["parameters"]["size"] == expected


def test_build_request_unknown_resolution_raises() -> None:
    client = DashScopeVideoClient(api_key="k")
    with pytest.raises(DashScopeVideoError, match="不支持的清晰度"):
        client.build_request(prompt="p", resolution="8K")


def test_build_request_unknown_ratio_falls_back_within_resolution() -> None:
    """清晰度已知但比例未收录（如 4:3）→ 回退该清晰度的 16:9，不臆造尺寸。"""
    client = DashScopeVideoClient(api_key="k")
    _, body = client.build_request(prompt="p", ratio="4:3", resolution="480P")
    assert body["parameters"]["size"] == "832*480"


def test_build_request_seed_and_flags_passthrough() -> None:
    client = DashScopeVideoClient(api_key="k")
    _, body = client.build_request(
        prompt="p", seed=42, watermark=True, prompt_extend=False
    )
    assert body["parameters"]["seed"] == 42
    assert body["parameters"]["watermark"] is True
    assert body["parameters"]["prompt_extend"] is False


@pytest.mark.parametrize(
    ("model", "duration", "expected"),
    [
        # wan2.6：收敛到 2–15
        ("wan2.6-t2v", None, 5),
        ("wan2.6-t2v", 1, 2),
        ("wan2.6-t2v", 15, 15),
        ("wan2.6-t2v", 30, 15),
        # wan2.5：只支持 5 / 10（就近）
        ("wan2.5-t2v-preview", 6, 5),
        ("wan2.5-t2v-preview", 8, 10),
        ("wan2.5-t2v-preview", 30, 10),
        # wan2.2：固定 5
        ("wan2.2-t2v-plus", None, 5),
        ("wan2.2-t2v-plus", 10, 5),
    ],
)
def test_resolve_duration_per_model(model: str, duration: int | None, expected: int) -> None:
    assert _resolve_duration(model, duration) == expected


def test_default_resolution_constant() -> None:
    assert _RESOLUTION_DEFAULT == "720P"


# ---- create_task -------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_ok_nested_output_task_id() -> None:
    client = DashScopeVideoClient(api_key="k")
    http = FakeHttp(post_response=FakeResponse(200, {"output": {"task_id": "t-1"}}))
    task_id = await client.create_task(http, _SUBMIT_PATH, {"model": "wan2.6-t2v"})
    assert task_id == "t-1"
    url, headers, body = http.posted
    assert url == f"{_DEFAULT_BASE_URL}{_SUBMIT_PATH}"
    assert headers["Authorization"] == "Bearer k"
    assert headers["Content-Type"] == "application/json"
    # 异步合成接口必须带该头
    assert headers["X-DashScope-Async"] == "enable"
    assert body == {"model": "wan2.6-t2v"}


@pytest.mark.asyncio
async def test_create_task_ok_top_level_task_id() -> None:
    client = DashScopeVideoClient(api_key="k")
    http = FakeHttp(post_response=FakeResponse(200, {"task_id": "t-2"}))
    assert await client.create_task(http, _SUBMIT_PATH, {}) == "t-2"


@pytest.mark.asyncio
async def test_create_task_missing_task_id_raises() -> None:
    client = DashScopeVideoClient(api_key="k")
    http = FakeHttp(post_response=FakeResponse(200, {"output": {}}))
    with pytest.raises(DashScopeVideoError, match="未返回 task_id"):
        await client.create_task(http, _SUBMIT_PATH, {})


@pytest.mark.asyncio
async def test_create_task_http_error_surfaces_message() -> None:
    client = DashScopeVideoClient(api_key="k")
    http = FakeHttp(
        post_response=FakeResponse(
            401, {"code": "InvalidApiKey", "message": "Invalid API-key provided."}
        )
    )
    with pytest.raises(DashScopeVideoError, match="Invalid API-key"):
        await client.create_task(http, _SUBMIT_PATH, {})


# ---- poll --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUCCEEDED", "succeeded", "Success", "finished"])
async def test_poll_accepts_success_statuses(status: str) -> None:
    client = DashScopeVideoClient(api_key="k", poll_interval_sec=0.001)
    http = FakeHttp(
        get_responses=[
            FakeResponse(200, {"output": {"task_status": status, "video_url": "https://cdn/a.mp4"}})
        ]
    )
    data = await client.poll(http, "t-1")
    assert data["output"]["video_url"] == "https://cdn/a.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "queued"])
async def test_poll_keeps_polling_on_pending_status(status: str) -> None:
    client = DashScopeVideoClient(api_key="k", poll_interval_sec=0.001, max_poll_attempts=3)
    http = FakeHttp(
        get_responses=[
            FakeResponse(200, {"output": {"task_status": status}}),
            FakeResponse(200, {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn/a.mp4"}}),
        ]
    )
    data = await client.poll(http, "t-1")
    assert data["output"]["video_url"] == "https://cdn/a.mp4"
    assert http.getted[0][0] == f"{_DEFAULT_BASE_URL}{_TASK_PATH}/t-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FAILED", "CANCELED"])
async def test_poll_failed_status_raises(status: str) -> None:
    client = DashScopeVideoClient(api_key="k", poll_interval_sec=0.001)
    http = FakeHttp(
        get_responses=[
            FakeResponse(200, {"output": {"task_status": status, "message": "内容违规"}})
        ]
    )
    with pytest.raises(DashScopeVideoError, match="内容违规"):
        await client.poll(http, "t-1")


@pytest.mark.asyncio
async def test_poll_http_error_is_tolerated_then_succeeds() -> None:
    client = DashScopeVideoClient(api_key="k", poll_interval_sec=0.001, max_poll_attempts=3)
    http = FakeHttp(
        get_responses=[
            FakeResponse(500, {"message": "internal error"}),
            FakeResponse(200, {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn/a.mp4"}}),
        ]
    )
    data = await client.poll(http, "t-1")
    assert data["output"]["video_url"] == "https://cdn/a.mp4"


@pytest.mark.asyncio
async def test_poll_unknown_status_then_timeout() -> None:
    """未知状态一律继续轮询（宁可超时也不误判为终点）。"""
    client = DashScopeVideoClient(api_key="k", poll_interval_sec=0.001, max_poll_attempts=2)
    http = FakeHttp(get_responses=[FakeResponse(200, {"output": {"task_status": "weird-new-state"}})])
    with pytest.raises(DashScopeVideoError, match="超时"):
        await client.poll(http, "t-1")


@pytest.mark.asyncio
async def test_poll_missing_status_keeps_polling_then_timeout() -> None:
    client = DashScopeVideoClient(api_key="k", poll_interval_sec=0.001, max_poll_attempts=2)
    http = FakeHttp(get_responses=[FakeResponse(200, {"output": {}})])
    with pytest.raises(DashScopeVideoError, match="超时"):
        await client.poll(http, "t-1")


# ---- extract_video_url -------------------------------------------------------


def test_extract_video_url_shapes() -> None:
    assert DashScopeVideoClient.extract_video_url({"output": {"video_url": "https://cdn/a.mp4"}}) == (
        "https://cdn/a.mp4"
    )
    assert DashScopeVideoClient.extract_video_url({"output": {"url": "https://cdn/b.mp4"}}) == (
        "https://cdn/b.mp4"
    )
    nested = {"output": {"results": [{"url": "https://cdn/c.mp4"}]}}
    assert DashScopeVideoClient.extract_video_url(nested) == "https://cdn/c.mp4"


def test_extract_video_url_missing_raises() -> None:
    with pytest.raises(DashScopeVideoError, match="未返回 video_url"):
        DashScopeVideoClient.extract_video_url({"output": {"task_status": "SUCCEEDED"}})


# ---- Tool：generate_video_dashscope（厂商自包含）-----------------------------


def test_dashscope_tool_metadata_and_config(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    assert tool.name == "generate_video_dashscope"
    assert tool.config_key == "dashscope_video"
    assert DashScopeVideoTool.config_cls() is DashScopeVideoToolConfig


def test_dashscope_config_registered_in_schema() -> None:
    from xianaibot.config.schema import ToolsConfig

    assert "dashscope_video" in ToolsConfig.model_fields


def test_dashscope_enabled_gating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启用门控：显式 apiKey / 「模型厂商」页 dashscope 密钥 / DASHSCOPE_API_KEY。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(dashscope_video=DashScopeVideoToolConfig()),
        provider_configs={},
    )
    assert DashScopeVideoTool.enabled(ctx) is False
    ctx.config.dashscope_video = DashScopeVideoToolConfig(api_key="ds-explicit")
    assert DashScopeVideoTool.enabled(ctx) is True
    ctx.config.dashscope_video = DashScopeVideoToolConfig()
    ctx.provider_configs = {"dashscope": SimpleNamespace(api_key="ds-from-provider")}
    assert DashScopeVideoTool.enabled(ctx) is True
    # 其他厂商（volcengine）的密钥不启用本工具
    ctx.provider_configs = {"volcengine": SimpleNamespace(api_key="ark-xxx")}
    assert DashScopeVideoTool.enabled(ctx) is False
    # 环境变量兜底
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-env")
    assert DashScopeVideoTool.enabled(ctx) is True


def test_dashscope_model_falls_back_on_foreign_vendor_names(tmp_path: Path) -> None:
    """残留其他厂商模型名（旧配置切厂商未切模型）时回退默认 wan2.6-t2v。"""
    tool = _tool(tmp_path)
    assert tool._resolve_model(None) == _DEFAULT_MODEL
    assert tool._resolve_model("doubao-seedance-2-5-260628") == _DEFAULT_MODEL
    assert tool._resolve_model("kling-3.0") == _DEFAULT_MODEL
    assert tool._resolve_model("MiniMax-H3") == _DEFAULT_MODEL
    # 显式自定义模型保留（大小写敏感）
    assert tool._resolve_model("wan2.2-i2v-plus") == "wan2.2-i2v-plus"


def test_dashscope_key_prefers_provider_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切厂商后 config.api_key 可能残留他厂 key，必须优先用「模型厂商」页的密钥。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    tool = DashScopeVideoTool(
        workspace=tmp_path,
        config=DashScopeVideoToolConfig(api_key="stale"),
        provider_api_key="ds-fresh",
    )
    assert tool._api_key() == "ds-fresh"
    bare = DashScopeVideoTool(workspace=tmp_path, config=DashScopeVideoToolConfig(api_key="stale"))
    assert bare._api_key() == "stale"
    empty = DashScopeVideoTool(workspace=tmp_path, config=DashScopeVideoToolConfig())
    with pytest.raises(DashScopeVideoError, match="未配置"):
        empty._api_key()


def test_dashscope_api_base_left_empty_for_client_normalization(tmp_path: Path) -> None:
    """apiBase 留空交给客户端归一化，兼容模式 base（带 /compatible-mode/v1）收敛到裸域。"""
    compat = _DEFAULT_BASE_URL + "/compatible-mode/v1"
    tool = DashScopeVideoTool(
        workspace=tmp_path,
        config=DashScopeVideoToolConfig(),
        provider_api_base=compat,
    )
    # 工具层只 rstrip 尾斜杠，后缀剥离统一收敛在 DashScopeVideoClient
    assert tool._api_base() == compat
    bare = DashScopeVideoTool(workspace=tmp_path, config=DashScopeVideoToolConfig())
    assert bare._api_base() == ""
    assert DashScopeVideoClient(api_key="k", api_base=tool._api_base()).api_base == (
        _DEFAULT_BASE_URL
    )
    assert DashScopeVideoClient(api_key="k", api_base=bare._api_base()).api_base == (
        _DEFAULT_BASE_URL
    )


def test_create_takes_dashscope_key_and_api_base(tmp_path: Path) -> None:
    ctx = SimpleNamespace(
        workspace=tmp_path,
        config=SimpleNamespace(dashscope_video=DashScopeVideoToolConfig()),
        provider_configs={
            "dashscope": SimpleNamespace(
                api_key="ds-1", api_base=_DEFAULT_BASE_URL + "/compatible-mode/v1"
            )
        },
    )
    tool = DashScopeVideoTool.create(ctx)
    assert tool._provider_api_key == "ds-1"
    assert tool.provider_api_base == _DEFAULT_BASE_URL + "/compatible-mode/v1"
    assert tool.config is ctx.config.dashscope_video


def _fake_dashscope_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """屏蔽 DashScope HTTP 与落盘，捕获 create_task 请求体与下载 URL。"""
    captured: dict = {}

    async def fake_create(self, client, endpoint, body):
        captured["endpoint"] = endpoint
        captured["body"] = body
        return "ds-task"

    async def fake_poll(self, client, task_id):
        return {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.example/out.mp4"}}

    async def fake_download(client, video_url, *, workspace, save_dir, default_model, model=None):
        captured["download_url"] = video_url
        return {"path": str(tmp_path / "out.mp4"), "model": model or default_model}

    monkeypatch.setattr(DashScopeVideoClient, "create_task", fake_create)
    monkeypatch.setattr(DashScopeVideoClient, "poll", fake_poll)
    monkeypatch.setattr(
        "xianaibot.agent.tools.dashscope_video.download_and_store", fake_download
    )
    return captured


@pytest.mark.asyncio
async def test_execute_dashscope_full_flow(tmp_path: Path, monkeypatch) -> None:
    """dashscope 全流程：建任务 → 轮询 → 取 video_url → 落盘，返回 JSON 元数据。"""
    tool = _tool(tmp_path)
    captured = _fake_dashscope_flow(monkeypatch, tmp_path)

    result = await tool.execute(prompt="一只橘猫弹钢琴", ratio="9:16", duration=10)
    data = json.loads(result)
    assert data["task_id"] == "ds-task"
    assert data["model"] == _DEFAULT_MODEL
    assert data["video"]["model"] == _DEFAULT_MODEL
    assert captured["endpoint"] == _SUBMIT_PATH
    assert captured["download_url"] == "https://cdn.example/out.mp4"
    assert captured["body"]["input"] == {"prompt": "一只橘猫弹钢琴"}
    assert captured["body"]["parameters"]["size"] == "720*1280"  # 720P + 9:16
    assert captured["body"]["parameters"]["duration"] == 10


@pytest.mark.asyncio
async def test_execute_dashscope_local_image_auto_base64(tmp_path: Path, monkeypatch) -> None:
    """本地首帧图片自动转 base64 data URL 放进 input.img_url。"""
    tool = _tool(tmp_path)
    img = tmp_path / "cat.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64)  # JPEG 魔数
    captured = _fake_dashscope_flow(monkeypatch, tmp_path)

    result = await tool.execute(prompt="让照片动起来", image_urls=[str(img)])
    assert json.loads(result)["video"]["model"] == _DEFAULT_MODEL
    assert captured["body"]["input"]["img_url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_execute_dashscope_multi_frame_error_string(tmp_path: Path) -> None:
    """超过单张首帧的校验失败必须返回 ``Error: ...`` 字符串而不是抛异常。"""
    tool = _tool(tmp_path)
    result = await tool.execute(
        prompt="p",
        image_urls=["https://a.example/a.png", "https://a.example/b.png"],
    )
    assert result.startswith("Error:")
    assert "单张首帧" in result


@pytest.mark.asyncio
async def test_execute_dashscope_missing_key_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    tool = DashScopeVideoTool(workspace=tmp_path, config=DashScopeVideoToolConfig())
    result = await tool.execute(prompt="hello")
    assert result.startswith("Error: DashScope API key 未配置")


@pytest.mark.asyncio
async def test_execute_dashscope_poll_failure_returns_error(tmp_path: Path, monkeypatch) -> None:
    tool = _tool(tmp_path)

    async def fake_create(self, client, endpoint, body):
        return "ds-task"

    async def fake_poll(self, client, task_id):
        raise DashScopeVideoError("通义万相任务失败：内容违规")

    monkeypatch.setattr(DashScopeVideoClient, "create_task", fake_create)
    monkeypatch.setattr(DashScopeVideoClient, "poll", fake_poll)

    result = await tool.execute(prompt="hello")
    assert result.startswith("Error:")
    assert "内容违规" in result
