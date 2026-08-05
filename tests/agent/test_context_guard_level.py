"""Tests for guard_level-gated untrusted-content snippet in system prompts.

Verifies that the identity.md and subagent_system.md templates include the
untrusted-content snippet at standard/minimal and exclude it at off.
"""

from __future__ import annotations

from biscuitbot.agent.context import ContextBuilder
from biscuitbot.agent.subagent import SubagentManager
from biscuitbot.config.schema import ToolsConfig
from biscuitbot.providers.base import LLMProvider

_UNTRUSTED_MARKER = "untrusted external data"


class _MockProvider(LLMProvider):
    def get_default_model(self):
        return "test-model"

    async def chat(self, **kwargs):
        pass


def test_identity_includes_untrusted_snippet_at_standard(tmp_path):
    cb = ContextBuilder(tmp_path, guard_level="standard")
    prompt = cb.build_system_prompt(channel="cli")
    assert _UNTRUSTED_MARKER in prompt


def test_identity_includes_untrusted_snippet_at_minimal(tmp_path):
    cb = ContextBuilder(tmp_path, guard_level="minimal")
    prompt = cb.build_system_prompt(channel="cli")
    assert _UNTRUSTED_MARKER in prompt


def test_identity_excludes_untrusted_snippet_at_off(tmp_path):
    cb = ContextBuilder(tmp_path, guard_level="off")
    prompt = cb.build_system_prompt(channel="cli")
    assert _UNTRUSTED_MARKER not in prompt


def test_identity_defaults_to_standard(tmp_path):
    """ContextBuilder without explicit guard_level defaults to standard (snippet present)."""
    cb = ContextBuilder(tmp_path)
    prompt = cb.build_system_prompt(channel="cli")
    assert _UNTRUSTED_MARKER in prompt


def test_subagent_prompt_includes_untrusted_snippet_at_standard(tmp_path):
    tc = ToolsConfig(guard_level="standard")
    sm = SubagentManager(
        provider=_MockProvider(),
        workspace=tmp_path,
        bus=None,
        max_tool_result_chars=1000,
        tools_config=tc,
    )
    prompt = sm._build_subagent_prompt()
    assert _UNTRUSTED_MARKER in prompt


def test_subagent_prompt_includes_untrusted_snippet_at_minimal(tmp_path):
    tc = ToolsConfig(guard_level="minimal")
    sm = SubagentManager(
        provider=_MockProvider(),
        workspace=tmp_path,
        bus=None,
        max_tool_result_chars=1000,
        tools_config=tc,
    )
    prompt = sm._build_subagent_prompt()
    assert _UNTRUSTED_MARKER in prompt


def test_subagent_prompt_excludes_untrusted_snippet_at_off(tmp_path):
    tc = ToolsConfig(guard_level="off")
    sm = SubagentManager(
        provider=_MockProvider(),
        workspace=tmp_path,
        bus=None,
        max_tool_result_chars=1000,
        tools_config=tc,
    )
    prompt = sm._build_subagent_prompt()
    assert _UNTRUSTED_MARKER not in prompt
