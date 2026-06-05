"""Regression tests for PR #3665: tool_calls included in merge/dedup/visible keys.

Verifies that _session_message_merge_key, _session_message_dedup_key,
_session_message_visible_key, and _matching_visible_duplicate correctly
distinguish messages with different tool_calls arrays.

Without tool_calls in the key, assistant messages that invoke different
tools (but share empty content and same-second timestamp) collapse into
a single key, losing tool calls during merge.
"""
from __future__ import annotations

from api.models import (
    _matching_visible_duplicate,
    _session_message_dedup_key,
    _session_message_merge_key,
    _session_message_visible_key,
    merge_session_messages_append_only,
)


def _assistant_tc(tc_id: str, fn_name: str, timestamp=1000) -> dict:
    """Assistant message with tool_calls but empty content."""
    return {
        "role": "assistant",
        "content": "",
        "timestamp": timestamp,
        "tool_calls": [
            {"id": tc_id, "function": {"name": fn_name, "arguments": "{}"}},
        ],
    }


def _tool_result(tc_id: str, name: str, content: str = "ok") -> dict:
    return {
        "role": "tool",
        "tool_call_id": tc_id,
        "name": name,
        "content": content,
    }


# ── _session_message_merge_key ──────────────────────────────────────────────


class TestMergeKeyToolCalls:
    def test_same_tool_calls_produce_same_key(self):
        a = _assistant_tc("call_1", "read_file")
        b = _assistant_tc("call_1", "read_file")
        assert _session_message_merge_key(a) == _session_message_merge_key(b)

    def test_different_tool_calls_produce_different_keys(self):
        a = _assistant_tc("call_1", "read_file")
        b = _assistant_tc("call_2", "terminal")
        assert _session_message_merge_key(a) != _session_message_merge_key(b)

    def test_empty_vs_nonempty_tool_calls_differ(self):
        empty = {"role": "assistant", "content": "", "timestamp": 1000}
        with_tc = _assistant_tc("call_1", "read_file")
        assert _session_message_merge_key(empty) != _session_message_merge_key(with_tc)


# ── _session_message_dedup_key ──────────────────────────────────────────────


class TestDedupKeyToolCalls:
    def test_same_tool_calls_produce_same_key(self):
        a = _assistant_tc("call_1", "read_file")
        b = _assistant_tc("call_1", "read_file")
        assert _session_message_dedup_key(a) == _session_message_dedup_key(b)

    def test_different_tool_calls_produce_different_keys(self):
        a = _assistant_tc("call_1", "read_file")
        b = _assistant_tc("call_2", "terminal")
        assert _session_message_dedup_key(a) != _session_message_dedup_key(b)


# ── _session_message_visible_key + _matching_visible_duplicate ──────────────


class TestVisibleKeyToolCalls:
    def test_same_tool_calls_match(self):
        a = _assistant_tc("call_1", "read_file")
        b = _assistant_tc("call_1", "read_file")
        ka = _session_message_visible_key(a)
        kb = _session_message_visible_key(b)
        assert ka == kb
        assert _matching_visible_duplicate(ka, {kb}) is not None

    def test_different_tool_calls_no_match(self):
        a = _assistant_tc("call_1", "read_file")
        b = _assistant_tc("call_2", "terminal")
        ka = _session_message_visible_key(a)
        kb = _session_message_visible_key(b)
        assert ka != kb
        assert _matching_visible_duplicate(ka, {kb}) is None


# ── merge_session_messages_append_only end-to-end ───────────────────────────


class TestMergeToolCallsEndToEnd:
    def test_same_tool_calls_sidecar_and_state_merge_to_one(self):
        """Sidecar and state.db have the same assistant message with identical
        tool_calls → merge must produce exactly one message (deduplicated)."""
        msg = _assistant_tc("call_1", "read_file", timestamp=1000)
        sidecar = [msg]
        state = [msg]
        result = merge_session_messages_append_only(sidecar, state)
        assert len(result) == 1, f"expected 1 (deduped), got {len(result)}"

    def test_different_tool_calls_sidecar_and_state_both_preserved(self):
        """Sidecar and state.db have assistant messages with different
        tool_calls → merge must preserve both (they are distinct turns)."""
        msg_a = _assistant_tc("call_1", "read_file", timestamp=1000)
        msg_b = _assistant_tc("call_2", "terminal", timestamp=1000)
        sidecar = [msg_a]
        state = [msg_b]
        result = merge_session_messages_append_only(sidecar, state)
        assert len(result) == 2, f"expected 2 (distinct), got {len(result)}"
        tc_ids = {
            m["tool_calls"][0]["id"]
            for m in result
            if m.get("tool_calls")
        }
        assert tc_ids == {"call_1", "call_2"}

    def test_no_tool_calls_still_deduped(self):
        """Messages without tool_calls are deduplicated by legacy key as before."""
        msg = {"role": "assistant", "content": "hello", "timestamp": 1000}
        result = merge_session_messages_append_only([msg], [msg])
        assert len(result) == 1
