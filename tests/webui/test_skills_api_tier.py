"""Tests for skill tier (system/agent/user) in the skills API."""

from __future__ import annotations

from pathlib import Path

from hczkbot.webui.skills_api import _tier, webui_skills_payload


class TestTierHelper:
    def test_none_metadata_returns_user(self):
        assert _tier(None) == "user"

    def test_missing_tier_returns_user(self):
        assert _tier({"description": "x"}) == "user"

    def test_system(self):
        assert _tier({"tier": "system"}) == "system"

    def test_agent(self):
        assert _tier({"tier": "agent"}) == "agent"

    def test_user(self):
        assert _tier({"tier": "user"}) == "user"

    def test_case_insensitive_and_trimmed(self):
        assert _tier({"tier": "SYSTEM"}) == "system"
        assert _tier({"tier": " Agent "}) == "agent"

    def test_invalid_falls_back_to_user(self):
        assert _tier({"tier": "bogus"}) == "user"

    def test_non_string_falls_back_to_user(self):
        assert _tier({"tier": 123}) == "user"


class TestWebuiSkillsPayloadTier:
    def test_workspace_skill_tier_returned(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        skills_root = workspace / "skills"
        skills_root.mkdir(parents=True)
        (skills_root / "my-sys").mkdir()
        (skills_root / "my-sys" / "SKILL.md").write_text(
            "---\nname: my-sys\ntier: system\ndescription: a sys skill\n---\n# Body\n",
            encoding="utf-8",
        )
        payload = webui_skills_payload(workspace)
        skill = next(s for s in payload["skills"] if s["name"] == "my-sys")
        assert skill["tier"] == "system"

    def test_missing_tier_defaults_to_user(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        skills_root = workspace / "skills"
        skills_root.mkdir(parents=True)
        (skills_root / "plain").mkdir()
        (skills_root / "plain" / "SKILL.md").write_text(
            "---\nname: plain\ndescription: no tier\n---\n# Body\n",
            encoding="utf-8",
        )
        payload = webui_skills_payload(workspace)
        skill = next(s for s in payload["skills"] if s["name"] == "plain")
        assert skill["tier"] == "user"


class TestBuiltinSkillsTier:
    """Verify the real built-in skills declare the expected tiers."""

    def _payload(self, tmp_path: Path) -> dict:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        return webui_skills_payload(workspace)

    def test_system_io_is_system_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next((s for s in payload["skills"] if s["name"] == "system-io"), None)
        assert skill is not None, "system-io skill not discovered"
        assert skill["tier"] == "system"

    def test_update_setup_is_system_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next((s for s in payload["skills"] if s["name"] == "update-setup"), None)
        assert skill is not None
        assert skill["tier"] == "system"

    def test_my_is_agent_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next(s for s in payload["skills"] if s["name"] == "my")
        assert skill["tier"] == "agent"

    def test_memory_is_agent_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next(s for s in payload["skills"] if s["name"] == "memory")
        assert skill["tier"] == "agent"

    def test_cron_is_agent_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next(s for s in payload["skills"] if s["name"] == "cron")
        assert skill["tier"] == "agent"

    def test_long_goal_is_agent_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next(s for s in payload["skills"] if s["name"] == "long-goal")
        assert skill["tier"] == "agent"

    def test_weather_is_user_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next(s for s in payload["skills"] if s["name"] == "weather")
        assert skill["tier"] == "user"

    def test_douyin_windows_is_user_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next((s for s in payload["skills"] if s["name"] == "douyin-windows"), None)
        assert skill is not None, "douyin-windows skill not discovered"
        assert skill["tier"] == "user"

    def test_douyin_macos_is_user_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        skill = next((s for s in payload["skills"] if s["name"] == "douyin-macos"), None)
        assert skill is not None, "douyin-macos skill not discovered"
        assert skill["tier"] == "user"

    def test_all_builtin_skills_have_valid_tier(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path)
        valid = {"system", "agent", "user"}
        assert payload["skills"], "no builtin skills discovered"
        for skill in payload["skills"]:
            assert skill["tier"] in valid, (
                f"{skill['name']} has invalid tier {skill['tier']!r}"
            )
