"""Tests for LLMProvider._enforce_role_alternation."""

from biscuitbot.providers.base import _SYNTHETIC_USER_CONTENT, LLMProvider


class TestEnforceRoleAlternation:
    """Verify trailing-assistant removal and consecutive same-role merging."""

    def test_empty_messages(self):
        assert LLMProvider._enforce_role_alternation([]) == []

    def test_no_change_needed(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Bye"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 4
        assert result[-1]["role"] == "user"

    def test_trailing_assistant_removed(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_multiple_trailing_assistants_removed(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_consecutive_user_messages_merged(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 1
        assert "Hello" in result[0]["content"]
        assert "How are you?" in result[0]["content"]

    def test_consecutive_assistant_messages_merged(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "assistant", "content": "How can I help?"},
            {"role": "user", "content": "Thanks"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 3
        assert "Hello!" in result[1]["content"]
        assert "How can I help?" in result[1]["content"]

    def test_system_messages_not_merged(self):
        msgs = [
            {"role": "system", "content": "System A"},
            {"role": "system", "content": "System B"},
            {"role": "user", "content": "Hi"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 3
        assert result[0]["content"] == "System A"
        assert result[1]["content"] == "System B"

    def test_tool_messages_not_merged(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result1", "tool_call_id": "1"},
            {"role": "tool", "content": "result2", "tool_call_id": "2"},
            {"role": "user", "content": "Next"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        tool_msgs = [m for m in result if m["role"] == "tool"]
        assert len(tool_msgs) == 2

    def test_consecutive_assistant_keeps_later_tool_call_message(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Previous reply"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result1", "tool_call_id": "1"},
            {"role": "user", "content": "Next"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert result[1]["role"] == "assistant"
        assert result[1]["tool_calls"] == [{"id": "1"}]
        assert result[1]["content"] is None
        assert result[2]["role"] == "tool"

    def test_consecutive_assistant_does_not_overwrite_existing_tool_call_message(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "assistant", "content": "Later plain assistant"},
            {"role": "tool", "content": "result1", "tool_call_id": "1"},
            {"role": "user", "content": "Next"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert result[1]["role"] == "assistant"
        assert result[1]["tool_calls"] == [{"id": "1"}]
        assert result[1]["content"] is None
        assert result[2]["role"] == "tool"

    def test_consecutive_assistant_with_tool_calls_merges_both(self):
        """Consecutive assistants both carrying tool_calls must merge their
        calls into one message — replacing wholesale would orphan the tool
        results of the first call (strict gateways return 400)."""
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
            {"role": "tool", "content": "result1", "tool_call_id": "1"},
            {"role": "tool", "content": "result2", "tool_call_id": "2"},
            {"role": "user", "content": "Next"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assistants = [m for m in result if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert [tc["id"] for tc in assistants[0]["tool_calls"]] == ["1", "2"]
        assert [m["role"] for m in result] == ["user", "assistant", "tool", "tool", "user"]

    def test_consecutive_assistant_tool_calls_concat_nonempty_contents(self):
        """When both assistants have non-empty text and the latter has tool_calls,
        contents are concatenated and calls merged."""
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "thinking out loud"},
            {"role": "assistant", "content": "doing it", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result1", "tool_call_id": "1"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert result[1]["role"] == "assistant"
        assert [tc["id"] for tc in result[1]["tool_calls"]] == ["1"]
        assert "thinking out loud" in result[1]["content"]
        assert "doing it" in result[1]["content"]

    def test_consecutive_assistant_tool_calls_merge_does_not_mutate_originals(self):
        msgs = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
        ]
        first, second = dict(msgs[0]), dict(msgs[1])
        result = LLMProvider._enforce_role_alternation(msgs)
        # Trailing assistant is stripped, so nothing survives — but the
        # original message dicts must not have been mutated.
        assert result == []
        assert msgs[0] == first and msgs[1] == second

    def test_orphan_prev_tool_calls_dropped_not_merged(self):
        """When the first assistant's tool results were lost entirely (the common
        cause of two adjacent assistant messages), its orphan calls must NOT be
        merged into the second assistant — that would produce an unresolved
        tool_call_id rejected by strict gateways. Falls back to keeping the
        latter assistant only."""
        msgs = [
            {"role": "user", "content": "Hi"},
            # prev：调用结果已彻底丢失（历史修复/转换副产物）
            {"role": "assistant", "content": None, "tool_calls": [{"id": "lost"}]},
            # curr：自己的调用有后续结果
            {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
            {"role": "tool", "content": "result2", "tool_call_id": "2"},
            {"role": "user", "content": "Next"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assistants = [m for m in result if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert [tc["id"] for tc in assistants[0]["tool_calls"]] == ["2"]
        assert [m["role"] for m in result] == ["user", "assistant", "tool", "user"]

    def test_orphan_prev_dropped_but_resolved_prev_kept_in_chain(self):
        """Three adjacent assistants: the first's result was lost, but the
        middle's result survived and appears after the last assistant. Only the
        lost first call is dropped; the resolvable middle call is retained."""
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "lost"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "mid"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "3"}]},
            {"role": "tool", "content": "r_mid", "tool_call_id": "mid"},
            {"role": "tool", "content": "r3", "tool_call_id": "3"},
            {"role": "user", "content": "Next"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assistants = [m for m in result if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert [tc["id"] for tc in assistants[0]["tool_calls"]] == ["mid", "3"]

    def test_duplicate_tool_call_ids_deduped_after_merge(self):
        """Two adjacent assistants carrying the same raw call id (duplicate-message
        class history damage) must not emit duplicate ids in the merged message."""
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "dup"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "dup"}]},
            {"role": "tool", "content": "result", "tool_call_id": "dup"},
            {"role": "user", "content": "Next"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assistants = [m for m in result if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["tool_calls"] == [{"id": "dup"}]

    def test_non_string_content_merges_blocks(self):
        """Merging user messages must preserve the former's list blocks
        (e.g. images) instead of discarding them."""
        image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
        msgs = [
            {"role": "user", "content": [image_block, {"type": "text", "text": "A"}]},
            {"role": "user", "content": "B"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert result[0]["content"] == [
            image_block,
            {"type": "text", "text": "A"},
            {"type": "text", "text": "B"},
        ]

    def test_mixed_str_and_list_content_merged_as_blocks(self):
        """A string content merged with a list content becomes a text block
        appended to the list, not a wholesale replacement."""
        msgs = [
            {"role": "user", "content": "A"},
            {"role": "user", "content": [{"type": "text", "text": "B"}]},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert result[0]["content"] == [
            {"type": "text", "text": "A"},
            {"type": "text", "text": "B"},
        ]

    def test_original_messages_not_mutated(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "World"},
        ]
        original_first = dict(msgs[0])
        LLMProvider._enforce_role_alternation(msgs)
        assert msgs[0] == original_first
        assert len(msgs) == 2

    def test_trailing_assistant_recovered_as_user_when_only_system_remains(self):
        """Subagent result injected as assistant message must not be silently dropped.

        When build_messages(current_role="assistant") produces [system, assistant],
        _enforce_role_alternation would drop the assistant, leaving only [system].
        Most providers (e.g. Zhipu/GLM error 1214) reject such requests.
        The trailing assistant should be recovered as a user message instead.
        """
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "assistant", "content": "Subagent completed successfully."},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert "Subagent completed successfully." in result[1]["content"]

    def test_trailing_assistant_not_recovered_when_user_message_present(self):
        """Recovery should NOT happen when a user message already exists."""
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 2
        assert result[-1]["role"] == "user"

    def test_trailing_assistant_recovered_with_tool_result_preceding(self):
        """When only [system, tool, assistant] remains, recovery is not needed
        because tool messages are valid non-system content."""
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "tool", "content": "result", "tool_call_id": "1"},
            {"role": "assistant", "content": "Done."},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 2
        assert result[-1]["role"] == "tool"

    def test_only_assistant_messages(self):
        msgs = [
            {"role": "assistant", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert result == []

    def test_realistic_conversation(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "And 3+3?"},
            {"role": "user", "content": "(please be quick)"},
            {"role": "assistant", "content": "6"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert len(result) == 4
        assert result[2]["role"] == "assistant"
        assert result[3]["role"] == "user"
        assert "And 3+3?" in result[3]["content"]
        assert "(please be quick)" in result[3]["content"]

    def test_leading_assistant_after_system_inserts_synthetic_user(self):
        """When the first non-system message is assistant (no tool_calls), a
        synthetic user message is inserted to prevent GLM error 1214."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "previous reply"},
            {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
            {"role": "assistant", "content": "after tool"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        non_system = [m for m in result if m["role"] != "system"]
        assert non_system[0]["role"] == "user"
        assert non_system[0]["content"] == _SYNTHETIC_USER_CONTENT
        # The original assistant should follow.
        assert non_system[1]["role"] == "assistant"

    def test_leading_assistant_with_tool_calls_not_patched(self):
        """An assistant message with tool_calls at the start is left as-is
        because tool messages will follow and some providers accept this."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc_1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "tc_1", "content": "result"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        non_system = [m for m in result if m["role"] != "system"]
        # The assistant has tool_calls so it should NOT be patched.
        assert non_system[0]["role"] == "assistant"
        assert non_system[0].get("tool_calls") is not None

    def test_user_after_system_not_patched(self):
        """Normal system→user sequence is not modified."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = LLMProvider._enforce_role_alternation(msgs)
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "hello"
