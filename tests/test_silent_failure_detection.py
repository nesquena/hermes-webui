"""Tests for the silent-failure detection fix in api/streaming.py.

The core logic lives in the module-level helper ``_has_new_assistant_reply``,
which decides whether *new* messages (beyond the pre-turn history) contain an
assistant message with non-empty content.

These tests cover the 8 scenarios specified in the task description to ensure
that historical assistant messages don't mask a silent provider failure.
"""

import pytest

from api.streaming import (
    _has_new_assistant_reply,
    _should_retry_silent_failure,
    _current_turn_tool_activity,
    _turn_produced_reasoning,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _msg(role: str, content: str) -> dict:
    """Shorthand for building a message dict."""
    return {"role": role, "content": content}


# ── Test scenarios ───────────────────────────────────────────────────────────

class TestHasNewAssistantReply:
    """All 8 scenarios from the task specification."""

    # Scenario 1 ──────────────────────────────────────────────────────────
    def test_history_has_assistant_but_current_turn_failed(self):
        """History has assistant content, but no new assistant was added."""
        prev = [
            _msg("user", "hi"),
            _msg("assistant", "hello"),
            _msg("user", "what's up?"),
        ]
        all_msgs = list(prev)  # same length — nothing new
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False

    # Scenario 2 ──────────────────────────────────────────────────────────
    def test_history_has_assistant_and_new_reply_added(self):
        """New assistant reply was appended this turn → should detect it."""
        prev = [
            _msg("user", "hi"),
            _msg("assistant", "hello"),
            _msg("user", "what's up?"),
        ]
        all_msgs = prev + [_msg("assistant", "not much, you?")]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is True

    # Scenario 3 ──────────────────────────────────────────────────────────
    def test_empty_history_empty_result(self):
        """Completely empty conversation → no assistant reply."""
        assert _has_new_assistant_reply([], 0) is False

    # Scenario 4 ──────────────────────────────────────────────────────────
    def test_new_assistant_with_empty_content(self):
        """New assistant message added but content is empty string."""
        prev = [
            _msg("user", "hello"),
            _msg("assistant", "hi there"),
        ]
        all_msgs = prev + [_msg("assistant", "")]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False

    # Scenario 5 ──────────────────────────────────────────────────────────
    def test_new_assistant_with_whitespace_content(self):
        """New assistant message added but content is only whitespace."""
        prev = [
            _msg("user", "hello"),
            _msg("assistant", "hi there"),
        ]
        all_msgs = prev + [_msg("assistant", "  \n  ")]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False

    # Scenario 6 ──────────────────────────────────────────────────────────
    def test_long_history_new_assistant_at_tail(self):
        """Many historical messages; two new ones at the end, last is assistant."""
        prev = [_msg("user", f"msg {i}") if i % 2 == 0 else _msg("assistant", f"reply {i}")
                for i in range(10)]
        # prev has 10 messages (indices 0..9)
        all_msgs = prev + [
            _msg("user", "new question"),
            _msg("assistant", "new answer with real content"),
        ]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is True

    # Scenario 7 ──────────────────────────────────────────────────────────
    def test_result_length_equals_prev_len(self):
        """No new messages at all — result length == prev length."""
        prev = [
            _msg("user", "hi"),
            _msg("assistant", "hey"),
        ]
        all_msgs = list(prev)
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False

    # Scenario 8 ──────────────────────────────────────────────────────────
    def test_result_shorter_than_prev_len_returns_false(self):
        """Edge-case: result messages < prev_count cannot prove a new reply.

        Shrunken result history has no reliable new-message slice. Scanning
        the shorter list can mistake an older assistant reply for a current
        turn reply, which would hide the silent-failure banner.
        """
        prev_count = 5
        # Only 3 messages in result — shorter than prev_count
        all_msgs = [
            _msg("user", "a"),
            _msg("assistant", "b"),
            _msg("user", "c"),
        ]
        assert _has_new_assistant_reply(all_msgs, prev_count) is False

        all_msgs_no_asst = [
            _msg("user", "a"),
            _msg("user", "b"),
            _msg("user", "c"),
        ]
        assert _has_new_assistant_reply(all_msgs_no_asst, prev_count) is False


# ── Additional edge-case tests ───────────────────────────────────────────────

class TestHasNewAssistantReplyEdgeCases:
    """Extra coverage for content field variants."""

    def test_content_is_none(self):
        """assistant message with content=None should not count."""
        prev = [_msg("user", "hi")]
        all_msgs = prev + [{"role": "assistant", "content": None}]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False

    def test_content_is_missing_key(self):
        """assistant message without 'content' key should not count."""
        prev = [_msg("user", "hi")]
        all_msgs = prev + [{"role": "assistant"}]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False

    def test_non_assistant_role_in_new_messages(self):
        """Only 'assistant' role counts; 'user' or 'system' in new msgs → False."""
        prev = [_msg("user", "hi")]
        all_msgs = prev + [_msg("user", "follow-up")]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False

    def test_prev_count_zero_with_assistant(self):
        """prev_count=0 with a new assistant → scans from index 0, finds it."""
        all_msgs = [_msg("assistant", "hello")]
        assert _has_new_assistant_reply(all_msgs, 0) is True

    def test_prev_count_zero_without_assistant(self):
        """prev_count=0 with only user messages → False."""
        all_msgs = [_msg("user", "hello")]
        assert _has_new_assistant_reply(all_msgs, 0) is False

    def test_multiple_new_assistant_first_empty_second_has_content(self):
        """First new assistant is empty, second has content → True."""
        prev = [_msg("user", "q")]
        all_msgs = prev + [
            _msg("assistant", ""),
            _msg("assistant", "actual content"),
        ]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is True

    def test_multiple_new_assistant_all_empty(self):
        """Multiple new assistant messages, all empty → False."""
        prev = [_msg("user", "q")]
        all_msgs = prev + [
            _msg("assistant", ""),
            _msg("assistant", "   "),
        ]
        assert _has_new_assistant_reply(all_msgs, len(prev)) is False


# ── Silent-failure retry gate ────────────────────────────────────────────────

class TestShouldRetrySilentFailure:
    """The one-shot retry gate for a turn that failed silently.

    A silent turn: no error string, no assistant reply, no streamed text, and
    the classifier fell back to ``no_response``. That is the "provider closed
    the stream with zero content and no error" shape that used to end as a
    dead-end card; it now earns the same single retry as a 401.
    """

    def test_silent_empty_turn_retries(self):
        assert _should_retry_silent_failure(
            last_err='', assistant_added=False, token_sent=False,
            error_type='no_response',
        ) is True

    def test_explicit_error_is_not_silent(self):
        """A classified error keeps its own (non-silent) path."""
        assert _should_retry_silent_failure(
            last_err='provider returned 500', assistant_added=False,
            token_sent=False, error_type='no_response',
        ) is False

    def test_assistant_reply_added_is_not_silent(self):
        assert _should_retry_silent_failure(
            last_err='', assistant_added=True, token_sent=True,
            error_type='no_response',
        ) is False

    def test_streamed_text_is_not_silent(self):
        """A retry after text reached the client would duplicate it."""
        assert _should_retry_silent_failure(
            last_err='', assistant_added=False, token_sent=True,
            error_type='no_response',
        ) is False

    def test_other_classifications_keep_their_own_path(self):
        for error_type in ('auth_mismatch', 'quota_exhausted', 'cancelled', 'interrupted'):
            assert _should_retry_silent_failure(
                last_err='', assistant_added=False, token_sent=False,
                error_type=error_type,
            ) is False

    # ── Work already done must not be replayed ───────────────────────────

    def _retry(self, **overrides):
        kwargs = dict(
            last_err='', assistant_added=False, token_sent=False,
            error_type='no_response',
        )
        kwargs.update(overrides)
        return _should_retry_silent_failure(**kwargs)

    def test_tool_activity_blocks_the_retry_only_when_present(self):
        """A turn that ran a tool already produced side effects."""
        assert self._retry(tool_activity=True) is False
        assert self._retry(tool_activity=False) is True

    def test_reasoning_blocks_the_retry_only_when_present(self):
        assert self._retry(reasoning_produced=True) is False
        assert self._retry(reasoning_produced=False) is True

    def test_tool_limit_exit_blocks_the_retry_only_when_present(self):
        """The tool-iteration limit owns its own card; do not shadow it."""
        assert self._retry(tool_limit_reached=True) is False
        assert self._retry(tool_limit_reached=False) is True

    def test_compression_rotation_blocks_the_retry_only_when_present(self):
        """After a rotation the retry would target the stale parent session."""
        assert self._retry(compression_rotated=True) is False
        assert self._retry(compression_rotated=False) is True

    def test_the_four_new_guards_default_to_permissive(self):
        """The guards are opt-in: the pre-existing gate shape is unchanged."""
        assert self._retry() is True


# ── Current-turn tool activity ───────────────────────────────────────────────

class TestCurrentTurnToolActivity:
    """The retry gate's tool-activity probe."""

    def test_tool_result_row_in_the_tail_is_activity(self):
        previous = [_msg("user", "q")]
        messages = previous + [
            _msg("assistant", ""),
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ]
        assert _current_turn_tool_activity(previous, messages) is True

    def test_assistant_tool_calls_in_the_tail_is_activity(self):
        previous = [_msg("user", "q")]
        messages = previous + [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        ]
        assert _current_turn_tool_activity(previous, messages) is True

    def test_a_fully_consumed_prefix_carries_no_per_turn_evidence(self):
        """Tool rows already inside the previous context are not this turn's.

        A turn whose tool rows are already persisted arrives with an empty
        suffix, and the rows are indistinguishable from an earlier turn's. The
        worker's per-turn guard for that case is the live tool-progress signal
        (`_live_tool_calls`), which the gate ORs in; the message scan must not
        claim the whole transcript as current-turn work, or a silent turn in a
        tool-using session could never be retried.
        """
        previous = [
            _msg("user", "q"),
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ]
        assert _current_turn_tool_activity(previous, list(previous)) is False

    def test_a_clean_tail_reports_no_activity(self):
        previous = [_msg("user", "q")]
        messages = previous + [_msg("assistant", "answer")]
        assert _current_turn_tool_activity(previous, messages) is False

    def test_non_matching_history_falls_back_to_the_whole_list(self):
        """Over-reporting suppresses a retry (safe); under-reporting replays work."""
        previous = [_msg("assistant", "unrelated")]
        messages = [_msg("user", "q"), {"role": "tool", "content": "done"}]
        assert _current_turn_tool_activity(previous, messages) is True

    def test_empty_inputs_report_no_activity(self):
        assert _current_turn_tool_activity([], []) is False
        assert _current_turn_tool_activity(None, None) is False


# ── Reasoning probe ──────────────────────────────────────────────────────────

class TestTurnProducedReasoning:
    """The retry gate's reasoning probe."""

    def test_blank_segments_are_not_reasoning(self):
        assert _turn_produced_reasoning({0: '', 1: '   '}, ['']) is False

    def test_a_non_blank_segment_is_reasoning(self):
        assert _turn_produced_reasoning({0: 'thinking...'}, ['']) is True

    def test_an_unflushed_buffer_reads_as_reasoning(self):
        """The tail of a reasoning stream can still sit in the buffer."""
        assert _turn_produced_reasoning({}, ['pending']) is True

    def test_no_inputs_report_no_reasoning(self):
        assert _turn_produced_reasoning({}, ['']) is False
        assert _turn_produced_reasoning(None, None) is False
