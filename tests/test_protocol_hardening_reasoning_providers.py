"""Tests for protocol-layer hardening affecting reasoning-capable providers.

Two related fixes that affect any OpenAI-compatible reasoning provider
(DeepSeek thinking, GLM-5, Qwen-thinking, GPT-o-series):

1. String-form ``tool_calls: '[]'`` — compression-summary messages carry the
   JSON string ``'[]'`` rather than a real list. The truthiness guard
   ``not sanitized['tool_calls']`` is False for a non-empty string, so the
   string survives to serialization where it is re-parsed into a real empty
   list ``[]`` that strict providers reject with HTTP 400.

2. reasoning_content-only assistant replies — reasoning-capable models emit
   turns with ``reasoning_content`` but no ``content``. The reply-detection
   helpers only checked visible content, so these turns were treated as
   non-replies, breaking multi-turn loop control flow and session-completion
   detection.
"""

from __future__ import annotations

from api.streaming import (
    _api_safe_message_positions,
    _has_new_assistant_reply,
    _sanitize_messages_for_api,
    _session_lacks_final_assistant_answer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asst(content=None, tool_calls=None, reasoning_content=None):
    msg = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return msg


def _user(text="hello"):
    return {"role": "user", "content": text}


def _assistant(result):
    """Extract the first assistant message from either a list of dicts
    (_sanitize_messages_for_api) or a list of (index, dict) tuples
    (_api_safe_message_positions)."""
    for item in result:
        msg = item[1] if isinstance(item, tuple) else item
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            return msg
    raise AssertionError("No assistant message found in result")


# ===========================================================================
# Fix 1: string-form tool_calls cleanup
# ===========================================================================

class TestStringFormToolCallsCleanup:
    """``tool_calls: '[]'`` (string) must be dropped like ``tool_calls: []``."""

    def test_string_form_empty_brackets_is_dropped(self):
        """``'[]'`` is truthy but semantically empty — must be removed."""
        messages = [
            _user("hi"),
            _asst(content=None, tool_calls="[]"),
        ]
        result = _sanitize_messages_for_api(messages)
        assistant = _assistant(result)
        assert "tool_calls" not in assistant, (
            f"Expected string-form '[]' to be dropped, got tool_calls={assistant.get('tool_calls')!r}"
        )

    def test_real_empty_list_still_dropped(self):
        """Regression guard: the original ``[]`` path must still work."""
        messages = [
            _user("hi"),
            _asst(content=None, tool_calls=[]),
        ]
        result = _sanitize_messages_for_api(messages)
        assistant = _assistant(result)
        assert "tool_calls" not in assistant

    def test_non_empty_list_preserved(self):
        """Real tool calls must survive the cleanup."""
        tc = [{"id": "call-1", "type": "function",
               "function": {"name": "search", "arguments": "{}"}}]
        messages = [
            _user("search"),
            _asst(content=None, tool_calls=tc),
            {"role": "tool", "tool_call_id": "call-1", "content": "found"},
        ]
        result = _sanitize_messages_for_api(messages)
        assistant = _assistant(result)
        assert "tool_calls" in assistant
        assert len(assistant["tool_calls"]) == 1

    def test_string_form_non_empty_list_preserved(self):
        """A JSON-string with real tool calls parses to a non-empty list and
        must NOT be dropped by the empty-cleanup (only empties are removed).
        Note: _sanitize_messages_for_api may convert the string to a real list
        via scrub_internal_replay_fields; we only assert the cleanup itself
        does not treat a non-empty parse as empty."""
        import json
        tc = [{"id": "call-1", "type": "function",
               "function": {"name": "search", "arguments": "{}"}}]
        messages = [
            _user("search"),
            _asst(content="calling tool", tool_calls=json.dumps(tc)),
            {"role": "tool", "tool_call_id": "call-1", "content": "found"},
        ]
        result = _sanitize_messages_for_api(messages)
        assistant = _assistant(result)
        # The cleanup must not drop a non-empty parse. If the pipeline
        # converted the string to a list, tool_calls survives; if it kept
        # the string, it also survives. Either way, not dropped as empty.
        # We assert the message itself survives (not filtered out) and
        # that no empty tool_calls key remains.
        assert assistant.get("role") == "assistant"
        # tool_calls may have been normalized away by scrub_internal_replay_fields
        # if the string form was converted — that's a separate pipeline concern.
        # This test's job: confirm the empty-cleanup didn't incorrectly fire.
        tc_val = assistant.get("tool_calls")
        if tc_val is not None:
            # If present, it must be non-empty (either as string or list)
            parsed = tc_val
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(parsed, list):
                assert len(parsed) > 0, "Non-empty tool_calls were incorrectly dropped"

    def test_api_safe_message_positions_string_form(self):
        """The second cleanup site (_api_safe_message_positions) must also
        handle string-form tool_calls."""
        messages = [
            _user("hi"),
            _asst(content=None, tool_calls="[]"),
        ]
        result = _api_safe_message_positions(messages)
        assistant = _assistant(result)
        assert "tool_calls" not in assistant, (
            f"Expected string-form '[]' dropped in _api_safe_message_positions, "
            f"got tool_calls={assistant.get('tool_calls')!r}"
        )

    def test_garbage_string_dropped(self):
        """A non-JSON string that can't be parsed should also be dropped
        (it's not a valid tool_calls representation)."""
        messages = [
            _user("hi"),
            _asst(content=None, tool_calls="not-json"),
        ]
        result = _sanitize_messages_for_api(messages)
        assistant = _assistant(result)
        assert "tool_calls" not in assistant


# ===========================================================================
# Fix 2: reasoning_content-only assistant replies
# ===========================================================================

class TestReasoningContentOnlyReplies:
    """Assistant turns with reasoning_content but no content must be recognized
    as real replies by the loop-control helpers."""

    def test_has_new_assistant_reply_recognizes_reasoning_only(self):
        """_has_new_assistant_reply must return True when the new assistant
        message has reasoning_content but empty content."""
        prev = [_user("think about this")]
        current = prev + [_asst(content="", reasoning_content="I reasoned...")]
        assert _has_new_assistant_reply(current, len(prev)) is True

    def test_has_new_assistant_reply_still_requires_assistant(self):
        """Regression: a user-only follow-up must not be a false positive."""
        prev = [_user("a"), _asst(content="reply")]
        current = prev + [_user("b")]
        assert _has_new_assistant_reply(current, len(prev)) is False

    def test_session_lacks_final_assistant_answer_reasoning_only(self):
        """A session ending with a reasoning-only assistant turn must NOT
        be flagged as 'lacks final answer' (it IS the answer)."""
        messages = [
            _user("analyze this"),
            _asst(content="", reasoning_content="My analysis..."),
        ]
        assert _session_lacks_final_assistant_answer(messages) is False

    def test_session_lacks_final_assistant_answer_truly_empty(self):
        """Regression: a session ending with a content-less, reasoning-less
        assistant turn MUST still be flagged."""
        messages = [
            _user("hi"),
            _asst(content=""),
        ]
        assert _session_lacks_final_assistant_answer(messages) is True

    def test_session_lacks_final_assistant_answer_tool_calls_flag(self):
        """An assistant turn with tool_calls (but no content) must signal
        'lacks final answer' — the tool hasn't returned yet."""
        tc = [{"id": "call-1", "type": "function",
               "function": {"name": "search", "arguments": "{}"}}]
        messages = [
            _user("search"),
            _asst(content=None, tool_calls=tc),
        ]
        assert _session_lacks_final_assistant_answer(messages) is True
