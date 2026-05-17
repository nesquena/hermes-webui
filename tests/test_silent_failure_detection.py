"""Tests for the silent-failure detection fix in api/streaming.py.

The core logic lives in the module-level helper ``_has_new_assistant_reply``,
which decides whether *new* messages (beyond the pre-turn history) contain an
assistant message with non-empty content.

These tests cover the 8 scenarios specified in the task description to ensure
that historical assistant messages don't mask a silent provider failure.
"""

import pytest

from api.streaming import _has_new_assistant_reply


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
    def test_result_shorter_than_prev_len_fallback(self):
        """Edge-case: result messages < prev_count → fallback to scanning all.

        The helper falls back to scanning all messages when the slice would
        be empty.  In this scenario, if an assistant message exists in the
        (shorter) result it should still be detected.
        """
        prev_count = 5
        # Only 3 messages in result — shorter than prev_count
        all_msgs = [
            _msg("user", "a"),
            _msg("assistant", "b"),
            _msg("user", "c"),
        ]
        # Fallback scans all → assistant with content "b" is found
        assert _has_new_assistant_reply(all_msgs, prev_count) is True

        # But if no assistant content in the shorter result → False
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
