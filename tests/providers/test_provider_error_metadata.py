from types import SimpleNamespace

import pytest

from xianaibot.providers.anthropic_provider import AnthropicProvider
from xianaibot.providers.base import LLMProvider, LLMResponse
from xianaibot.providers.openai_compat_provider import OpenAICompatProvider


def _fake_response(
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
    text: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        headers=headers or {},
        text=text,
    )


def test_openai_handle_error_extracts_structured_metadata() -> None:
    class FakeStatusError(Exception):
        pass

    err = FakeStatusError("boom")
    err.status_code = 409
    err.response = _fake_response(
        status_code=409,
        headers={"retry-after-ms": "250", "x-should-retry": "false"},
        text='{"error":{"type":"rate_limit_exceeded","code":"rate_limit_exceeded"}}',
    )
    err.body = {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}}

    response = OpenAICompatProvider._handle_error(err)

    assert response.finish_reason == "error"
    assert response.error_status_code == 409
    assert response.error_type == "rate_limit_exceeded"
    assert response.error_code == "rate_limit_exceeded"
    assert response.error_retry_after_s == 0.25
    assert response.error_should_retry is False


def test_openai_handle_error_marks_timeout_kind() -> None:
    class FakeTimeoutError(Exception):
        pass

    response = OpenAICompatProvider._handle_error(FakeTimeoutError("timeout"))

    assert response.finish_reason == "error"
    assert response.error_kind == "timeout"


def test_anthropic_handle_error_extracts_structured_metadata() -> None:
    class FakeStatusError(Exception):
        pass

    err = FakeStatusError("boom")
    err.status_code = 408
    err.response = _fake_response(
        status_code=408,
        headers={"retry-after": "1.5", "x-should-retry": "true"},
    )
    err.body = {"type": "error", "error": {"type": "rate_limit_error"}}

    response = AnthropicProvider._handle_error(err)

    assert response.finish_reason == "error"
    assert response.error_status_code == 408
    assert response.error_type == "rate_limit_error"
    assert response.error_retry_after_s == 1.5
    assert response.error_should_retry is True


def test_anthropic_handle_error_marks_connection_kind() -> None:
    class FakeConnectionError(Exception):
        pass

    response = AnthropicProvider._handle_error(FakeConnectionError("connection"))

    assert response.finish_reason == "error"
    assert response.error_kind == "connection"


@pytest.mark.parametrize("expected, kwargs", [
    (True, {"error_status_code": 402}),  # HTTP 402
    (True, {"error_type": "insufficient_quota"}),  # billing token
    (True, {"content": "429 You exceeded your current quota"}),  # text marker
    (False, {"error_status_code": 429, "error_type": "rate_limit_exceeded"}),  # plain rate limit
])
def test_is_arrearage_response(expected: bool, kwargs: dict) -> None:
    response = LLMResponse(finish_reason="error", **{"content": "boom", **kwargs})
    assert LLMProvider.is_arrearage_response(response) is expected


@pytest.mark.parametrize("expected, kwargs", [
    (True, {"error_status_code": 413}),  # HTTP 413 Payload Too Large
    (True, {"error_type": "context_length_exceeded"}),  # OpenAI 语义 type
    (True, {"error_code": "request_too_large"}),  # 语义 code
    (True, {"content": "This model's maximum context length is 262144 tokens"}),  # OpenAI 文本
    (True, {"content": "prompt is too long: 300000 tokens > 200000 maximum"}),  # Anthropic
    (True, {"content": "The input token count (300000) exceeds the maximum"}),  # Gemini
    (True, {"content": "输入token总数超过上限"}),  # 中文网关
    (False, {"error_status_code": 400, "error_type": "invalid_request_error", "content": "bad param"}),  # 普通参数错误
    (False, {"content": "429 rate limit exceeded"}),  # 限流不误判
])
def test_is_context_overflow_response(expected: bool, kwargs: dict) -> None:
    response = LLMResponse(finish_reason="error", **{"content": "boom", **kwargs})
    assert LLMProvider.is_context_overflow_response(response) is expected
