"""Regression tests for PR #3665: tool_calls included in merge/dedup/visible keys.

Verifies that _session_message_merge_key, _session_message_dedup_key,
_session_message_visible_key, and _matching_visible_duplicate correctly
distinguish messages with different tool_calls arrays.

Without tool_calls in the key, assistant messages that invoke different
tools (but share empty content and same-second timestamp) collapse into
a single key, losing tool calls during merge.
"""
from __future__ import annotations

from api import models
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

    def test_older_state_tool_call_assistant_stays_before_final_answer(self):
        """Older tool-call-only state.db rows must not become the final tail.

        The WebUI sidecar can already contain a settled final answer while
        state.db still has empty-content assistant rows that carry distinct
        tool_calls from earlier in the turn. Those rows are real activity and
        should be preserved, but appending them after the final answer makes the
        renderer treat the final answer as a non-final assistant segment.
        """
        sidecar_tool = _assistant_tc("call_1", "read_file", timestamp=1000.0)
        final_answer = {
            "role": "assistant",
            "content": "Final answer is complete.",
            "timestamp": 1001.0,
        }
        state_tool = _assistant_tc("call_2", "terminal", timestamp=1000.5)

        result = merge_session_messages_append_only(
            [sidecar_tool, final_answer],
            [state_tool],
        )

        assert result[-1] == final_answer
        assert [m.get("tool_calls", [{}])[0].get("id") for m in result if m.get("tool_calls")] == [
            "call_1",
            "call_2",
        ]

    def test_equal_timestamp_state_tool_call_stays_before_final_answer(self):
        """Same-second tool-call rows must still stay before the final answer."""
        sidecar_tool = _assistant_tc("call_1", "read_file", timestamp=1000)
        final_answer = {
            "role": "assistant",
            "content": "Final answer.",
            "timestamp": 1000,
        }
        state_tool = _assistant_tc("call_2", "terminal", timestamp=1000)

        result = merge_session_messages_append_only(
            [sidecar_tool, final_answer],
            [state_tool],
        )

        assert result[-1] == final_answer
        assert [
            m.get("tool_calls", [{}])[0].get("id")
            for m in result
            if m.get("tool_calls")
        ] == ["call_1", "call_2"]

    def test_multiple_equal_timestamp_state_tool_calls_stay_before_final(self):
        """Several same-second state tool-call rows all stay before the final
        answer, in order (Opus coverage gap: multi-tool tie shape)."""
        sidecar_tool = _assistant_tc("call_1", "read_file", timestamp=1000)
        final_answer = {"role": "assistant", "content": "Final answer.", "timestamp": 1000}
        state_a = _assistant_tc("call_2", "terminal", timestamp=1000)
        state_b = _assistant_tc("call_3", "write_file", timestamp=1000)

        result = merge_session_messages_append_only(
            [sidecar_tool, final_answer],
            [state_a, state_b],
        )

        assert result[-1] == final_answer, result
        assert [
            m.get("tool_calls", [{}])[0].get("id")
            for m in result
            if m.get("tool_calls")
        ] == ["call_1", "call_2", "call_3"]

    def test_tie_insert_does_not_split_tool_call_result_block(self):
        """The equal-timestamp tool-call insert must not land between an
        assistant(tool_calls) and its tool result (Opus coverage gap:
        guard-a block-split interaction)."""
        sidecar_tool = _assistant_tc("call_1", "read_file", timestamp=1000)
        sidecar_result = _tool_result("call_1", "read_file", "file contents")
        final_answer = {"role": "assistant", "content": "Final answer.", "timestamp": 1000}
        state_tool = _assistant_tc("call_2", "terminal", timestamp=1000)

        result = merge_session_messages_append_only(
            [sidecar_tool, sidecar_result, final_answer],
            [state_tool],
        )

        # The tool result must stay immediately after its assistant(tool_calls).
        asst_idx = next(i for i, m in enumerate(result)
                        if m.get("tool_calls") and m["tool_calls"][0]["id"] == "call_1")
        res_idx = next(i for i, m in enumerate(result)
                       if m.get("role") == "tool" and m.get("tool_call_id") == "call_1")
        assert res_idx == asst_idx + 1, f"tool_calls->result block split: {result}"
        assert result[-1] == final_answer, result

    def test_pre_window_state_tool_call_row_is_not_tail_appended(self):
        """Pre-window tool-call resurrection candidates stay dropped."""
        sidecar_tool = _assistant_tc("call_1", "read_file", timestamp=1000)
        final_answer = {
            "role": "assistant",
            "content": "Final answer.",
            "timestamp": 1001,
        }
        state_tool = _assistant_tc("call_2", "terminal", timestamp=999)

        result = merge_session_messages_append_only(
            [sidecar_tool, final_answer],
            [state_tool],
        )

        assert result == [sidecar_tool, final_answer]
        assert result[-1] == final_answer

    def test_no_tool_calls_still_deduped(self):
        """Messages without tool_calls are deduplicated by legacy key as before."""
        msg = {"role": "assistant", "content": "hello", "timestamp": 1000}
        result = merge_session_messages_append_only([msg], [msg])
        assert len(result) == 1


# ── large-payload duplicate matching performance ────────────────────────────


class TestVisibleDuplicateLargePayloadPerformance:
    def test_large_nonmatching_payload_skips_loose_normalizer(self, monkeypatch):
        """Giant tool/log payloads must not be regex-tokenized for fuzzy matching.

        Exact visible-key equality is checked before this path. For non-exact
        multi-hundred-KB payloads, fuzzy substring/token matching is too costly
        for the /api/session hot path and low-value for deduplication.
        """
        def fail_if_called(_content):
            raise AssertionError("large payloads should not hit loose normalizer")

        monkeypatch.setattr(models, "_loose_session_message_content", fail_if_called)

        large_state = ("state output\n" * 25_000).strip()
        large_sidecar = ("sidecar output\n" * 25_000).strip()
        visible_key = ("assistant", large_state, "")
        sidecar_key = ("assistant", large_sidecar, "")

        assert _matching_visible_duplicate(visible_key, {sidecar_key}) is None

    def test_small_nonmatching_payload_keeps_loose_matching(self, monkeypatch):
        """The large-payload guard must not disable legacy fuzzy matching."""
        calls = []

        def counted_loose(content):
            calls.append(content)
            return " ".join(str(content).lower().replace(",", "").replace("!", "").split())

        monkeypatch.setattr(models, "_loose_session_message_content", counted_loose)

        visible_key = ("assistant", "hello world", "")
        sidecar_key = ("assistant", "HELLO, WORLD!!", "")

        assert _matching_visible_duplicate(visible_key, {sidecar_key}) == sidecar_key
        assert len(calls) == 2
