"""discover_employees 工具单元测试：已安装检索 + 人才市场目录检索。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from biscuitbot.agent.employees import EmployeeStore
from biscuitbot.agent.tools.context import ToolContext
from biscuitbot.agent.tools.employee_discover import DiscoverEmployeesTool
from biscuitbot.webui.talent_market import TalentMarketError


def _store(tmp_path: Path) -> EmployeeStore:
    return EmployeeStore(tmp_path / "ws")


def _catalog() -> dict[str, Any]:
    return {
        "schema": "talent-market.v1",
        "meta": {"updated": "2026-08-14"},
        "employees": [
            {
                "id": "copywriter",
                "name": "文案专员",
                "avatar": "✍️",
                "description": "面向营销文案的资深写手，擅长公众号与短视频脚本",
                "system_prompt": "你是一名文案专员数字人员工。",
                "skills": ["web_search", "browser"],
                "category": "内容创作",
            },
            {
                "id": "data-analyst",
                "name": "数据分析师",
                "avatar": "📊",
                "description": "数据处理与可视化",
                "system_prompt": "你是一名数据分析师数字人员工。",
                "skills": ["python"],
                "category": "数据分析",
            },
        ],
    }


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离人才市场缓存目录到临时目录，避免污染真实 config 目录。"""
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market.get_runtime_subdir",
        lambda name: tmp_path / name,
    )


@pytest.fixture(autouse=True)
def _registry_unconfigured_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认注册表未配置，避免测试触碰真实 config 或发起真实网络请求。"""
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market.read_talent_market_registry_url",
        lambda *a, **k: "",
    )


def _configure_registry(
    monkeypatch: pytest.MonkeyPatch, url: str = "https://example.com/registry.json"
) -> None:
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market.read_talent_market_registry_url",
        lambda *a, **k: url,
    )


def _mock_fetch(monkeypatch: pytest.MonkeyPatch, catalog: dict[str, Any]) -> None:
    monkeypatch.setattr(
        "biscuitbot.webui.talent_market._http_get_json",
        lambda url, **kwargs: catalog,
    )


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


class TestInstalledSearch:
    async def test_search_by_chinese_name(self, tmp_path: Path) -> None:
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("阿伟"))
        assert result["employees"]
        row = result["employees"][0]
        assert row["id"] == "clip-master"
        assert row["installed"] is True
        assert row["source"] == "installed"

    async def test_search_by_skill(self, tmp_path: Path) -> None:
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("seedance"))
        ids = {row["id"] for row in result["employees"]}
        assert "short-video-operator" in ids

    async def test_search_by_title(self, tmp_path: Path) -> None:
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("剪辑"))
        ids = {row["id"] for row in result["employees"]}
        assert "clip-master" in ids
        assert all(row["source"] == "installed" for row in result["employees"])

    async def test_disabled_employee_excluded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create_employee(
            {"id": "writer", "name": "写作助手", "system_prompt": "你是写作助手数字人员工。"}
        )
        store.update_employee("writer", {"enabled": False})
        tool = DiscoverEmployeesTool(employees=store)
        result = _payload(await tool.execute("写作助手"))
        assert result["employees"] == []
        assert "未找到" in result["note"]

    async def test_no_match_returns_empty_with_note(self, tmp_path: Path) -> None:
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("zzzz_不存在"))
        assert result["employees"] == []
        assert "未找到" in result["note"]

    async def test_empty_query_rejected(self, tmp_path: Path) -> None:
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("   "))
        assert result["employees"] == []
        assert "不能为空" in result["note"]

    async def test_limit_clamped(self, tmp_path: Path) -> None:
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        one = _payload(await tool.execute("视频", limit=1))
        assert len(one["employees"]) == 1
        huge = _payload(await tool.execute("视频", limit=999))
        assert len(huge["employees"]) <= 20
        default = _payload(await tool.execute("视频"))
        assert 1 <= len(default["employees"]) <= 10

    async def test_installed_only_when_registry_unconfigured(self, tmp_path: Path) -> None:
        """注册表未配置：只返回已安装员工，绝不发起真实网络请求。"""
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("剪辑"))
        assert result["employees"]
        assert all(row["source"] == "installed" for row in result["employees"])
        assert "未配置" in result["note"]


class TestLifecycle:
    def test_enabled_requires_employees(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with_ctx = ToolContext(config={}, workspace=str(tmp_path), employees=store)
        without_ctx = ToolContext(config={}, workspace=str(tmp_path), employees=None)
        assert DiscoverEmployeesTool.enabled(with_ctx) is True
        assert DiscoverEmployeesTool.enabled(without_ctx) is False

    def test_create_wires_employees(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        ctx = ToolContext(config={}, workspace=str(tmp_path), employees=store)
        tool = DiscoverEmployeesTool.create(ctx)
        assert tool._employees is store

    def test_metadata(self, tmp_path: Path) -> None:
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        assert tool.name == "discover_employees"
        assert tool._always_include is True
        assert tool._scopes == {"core"}
        assert "invoke_employee" in tool.description
        assert tool._capability


class TestMarketplaceSearch:
    async def test_uninstalled_match_is_hint_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_registry(monkeypatch)
        _mock_fetch(monkeypatch, _catalog())
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("文案"))
        rows = [r for r in result["employees"] if r["id"] == "copywriter"]
        assert len(rows) == 1
        row = rows[0]
        assert row["installed"] is False
        assert row["enabled"] is False
        assert row["source"] == "marketplace"
        assert row["category"] == "内容创作"
        assert "文案" in row["description"]
        assert "手动安装" in result["note"]

    async def test_installed_marketplace_employee_deduped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        store.create_employee(
            {
                "id": "copywriter",
                "name": "文案专员",
                "system_prompt": "你是一名文案专员数字人员工。",
            }
        )
        _configure_registry(monkeypatch)
        _mock_fetch(monkeypatch, _catalog())
        tool = DiscoverEmployeesTool(employees=store)
        result = _payload(await tool.execute("文案专员"))
        rows = [r for r in result["employees"] if r["id"] == "copywriter"]
        assert len(rows) == 1
        assert rows[0]["installed"] is True
        assert rows[0]["source"] == "installed"

    async def test_installed_but_disabled_marketplace_employee_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        store.create_employee(
            {
                "id": "copywriter",
                "name": "文案专员",
                "system_prompt": "你是一名文案专员数字人员工。",
            }
        )
        store.update_employee("copywriter", {"enabled": False})
        _configure_registry(monkeypatch)
        _mock_fetch(monkeypatch, _catalog())
        tool = DiscoverEmployeesTool(employees=store)
        result = _payload(await tool.execute("文案"))
        assert all(r["id"] != "copywriter" for r in result["employees"])

    async def test_fetch_failure_degrades_to_installed_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_registry(monkeypatch)

        def _boom(url: str, **kwargs: Any) -> Any:
            raise TalentMarketError("boom", status=502)

        monkeypatch.setattr("biscuitbot.webui.talent_market._http_get_json", _boom)
        tool = DiscoverEmployeesTool(employees=_store(tmp_path))
        result = _payload(await tool.execute("剪辑"))
        assert result["employees"]
        assert all(row["source"] == "installed" for row in result["employees"])
        assert "拉取失败" in result["note"]
