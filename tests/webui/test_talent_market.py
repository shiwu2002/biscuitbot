"""数字员工人才市场后端逻辑测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from biscuitbot.agent.employees import EmployeeStore, EmployeeValidationError
from biscuitbot.webui.talent_market import (
    TalentMarketError,
    _http_get_json,
    _validate_registry_url,
    install_talent_employee,
    talent_catalog_payload,
)


def _store(tmp_path: Path) -> EmployeeStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return EmployeeStore(workspace)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试把人才市场缓存目录隔离到临时目录，避免污染真实缓存。"""
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market.get_runtime_subdir",
        lambda name: tmp_path / name,
    )


def _catalog_registry() -> dict:
    return {
        "schema": "talent-market.v1",
        "meta": {"updated": "2026-08-13"},
        "employees": [
            {
                "id": "copywriter",
                "name": "文案专员",
                "avatar": "✍️",
                "description": "面向营销文案",
                "system_prompt": "你是一名文案专员数字人员工。",
                "skills": ["web_search", "browser"],
                "extra_field": "ignored",
            },
            {
                # 无显式 id → 由名称生成 slug
                "name": "数据分析师",
                "avatar": "📊",
                "description": "数据处理",
                "system_prompt": "你是一名数据分析师数字人员工。",
                "skills": ["python"],
            },
            {"name": "", "description": "空名称应被跳过"},
        ],
    }


# ---- URL 校验 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/x.json", "ssh://host/path", "", "   "],
)
def test_invalid_url_schemes_rejected(url: str) -> None:
    with pytest.raises(TalentMarketError) as exc:
        _validate_registry_url(url)
    assert exc.value.status == 400


def test_valid_urls_accepted() -> None:
    assert _validate_registry_url("https://example.com/registry.json") == (
        "https://example.com/registry.json"
    )
    assert _validate_registry_url("http://127.0.0.1:8000/employees.json") == (
        "http://127.0.0.1:8000/employees.json"
    )


# ---- 目录拉取与规范化 -------------------------------------------------------


def test_catalog_fetch_and_normalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.create_employee(
        {
            "id": "copywriter",
            "name": "文案专员",
            "avatar": "✍️",
            "system_prompt": "你是一名文案专员数字人员工。",
            "skills": ["web_search"],
        }
    )
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json",
        lambda url, **kwargs: _catalog_registry(),
    )
    payload = talent_catalog_payload("https://example.com/registry.json", store)

    assert payload["source_url"] == "https://example.com/registry.json"
    assert payload["catalog_updated_at"] == "2026-08-13"
    assert payload["installed_count"] == 1
    assert len(payload["employees"]) == 2

    copywriter = next(e for e in payload["employees"] if e["id"] == "copywriter")
    assert copywriter["installed"] is True
    assert "extra_field" not in copywriter
    assert copywriter["skills"] == ["web_search", "browser"]

    analyst = next(e for e in payload["employees"] if e["name"] == "数据分析师")
    assert analyst["installed"] is False
    # 纯中文名被 _slugify 剥离 → 回退 "employee"
    assert analyst["id"] == "employee"


def test_catalog_installed_when_store_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json",
        lambda url, **kwargs: _catalog_registry(),
    )
    payload = talent_catalog_payload("https://example.com/registry.json", None)
    assert all(e["installed"] is False for e in payload["employees"])
    assert payload["installed_count"] == 0


def test_catalog_fallback_talent_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    registry = {"talent": [{"id": "a", "name": "A", "system_prompt": "p", "skills": []}]}
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json", lambda url, **kwargs: registry
    )
    payload = talent_catalog_payload("https://example.com/registry.json", store)
    assert len(payload["employees"]) == 1
    assert payload["employees"][0]["id"] == "a"


# ---- 缓存与 stale 回退 ------------------------------------------------------


def test_catalog_ttl_cache_hit_and_stale_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []

    def fake_fetch(url: str, **kwargs: object) -> dict:
        calls.append(url)
        return _catalog_registry()

    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json", fake_fetch
    )

    url = "https://example.com/registry.json"
    assert talent_catalog_payload(url, store)["installed_count"] == 0
    assert len(calls) == 1  # 首次拉取

    # TTL 内第二次 → 命中缓存，不再拉取
    assert talent_catalog_payload(url, store)["installed_count"] == 0
    assert len(calls) == 1

    # 拉取失败但有旧缓存 → stale 回退
    def fail_fetch(url: str, **kwargs: object) -> dict:
        raise TalentMarketError("boom", status=502)

    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json", fail_fetch
    )
    payload = talent_catalog_payload(url, store)
    assert payload["employees"]  # stale 数据可用


def test_catalog_no_cache_no_fetch_raises_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json",
        lambda url, **kwargs: (_ for _ in ()).throw(
            TalentMarketError("boom", status=502)
        ),
    )
    with pytest.raises(TalentMarketError) as exc:
        talent_catalog_payload("https://example.com/registry.json", store)
    assert exc.value.status == 502


def test_force_refresh_bypasses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json",
        lambda url, **kwargs: (calls.append(url) or _catalog_registry()),
    )
    url = "https://example.com/registry.json"
    talent_catalog_payload(url, store)
    talent_catalog_payload(url, store, force_refresh=True)
    assert len(calls) == 2


# ---- HTTP 层（大小上限 / 非对象 JSON）---------------------------------------


class _FakeStreamResponse:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self._content = content
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise httpx.HTTPStatusError(
                "error", request=httpx.Request("GET", "https://x"), response=httpx.Response(self._status)
            )

    def iter_bytes(self, chunk_size: int):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


class _FakeClient:
    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs
        self._content = b"{}"
        self._status = 200

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def stream(self, method: str, url: str) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._content, self._status)


def test_http_get_json_rejects_non_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client._content = b"[1,2,3]"
    monkeypatch.setattr("biscuitbot.webui.talent_market.httpx.Client", lambda **kw: client)
    with pytest.raises(TalentMarketError) as exc:
        _http_get_json("https://example.com/registry.json")
    assert exc.value.status == 502


def test_http_get_json_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    client._content = b"x" * (2 * 1024 * 1024 + 1)
    monkeypatch.setattr("biscuitbot.webui.talent_market.httpx.Client", lambda **kw: client)
    with pytest.raises(TalentMarketError) as exc:
        _http_get_json("https://example.com/registry.json")
    assert exc.value.status == 502


# ---- 安装落库 ---------------------------------------------------------------


def test_install_creates_employee(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = install_talent_employee(
        {
            "id": "copywriter",
            "name": "文案专员",
            "system_prompt": "你是一名文案专员。",
            "skills": ["web_search"],
        },
        store,
        source_url="https://example.com/registry.json",
    )
    assert result["already_existed"] is False
    employee = result["employee"]
    assert employee["id"] == "copywriter"
    assert employee["enabled"] is True
    assert store.get_employee("copywriter") == employee


def test_install_duplicate_returns_already_existed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    values = {
        "id": "copywriter",
        "name": "文案专员",
        "system_prompt": "你是一名文案专员。",
        "skills": ["web_search"],
    }
    first = install_talent_employee(values, store)
    second = install_talent_employee(values, store)
    assert first["already_existed"] is False
    assert second["already_existed"] is True
    assert second["employee"]["id"] == "copywriter"


def test_install_missing_system_prompt_raises_400(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(EmployeeValidationError) as exc:
        install_talent_employee({"id": "x", "name": "X"}, store)
    assert exc.value.status == 400


def test_install_ignores_non_whitelisted_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = install_talent_employee(
        {
            "id": "safe",
            "name": "安全员工",
            "system_prompt": "p",
            "skills": [],
            "description": "不应写入员工文件",
            "source": "https://evil.example",
        },
        store,
    )
    employee = result["employee"]
    assert "description" not in employee
    assert "source" not in employee
