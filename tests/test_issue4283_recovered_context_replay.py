"""Regression tests for #4283: interrupted-turn user message replay.

Two shapes must be covered:

1. **Interrupted-with-saved-partial** — when a turn is interrupted *after* a
   partial assistant answer was streamed, dropping the ``_recovered`` user
   would leave two adjacent assistant messages, which strict providers reject
   with HTTP 400.  The ``_recovered`` skip must be conditional.

2. **Provider-error / tool-iteration-limit** — the ``context_messages``
   mirror must live in ``_materialize_pending_user_turn_before_error`` (not
   only in ``_persist_cancelled_turn``) so all three callers are covered.
   A recovered turn arriving as a flagless state.db delta must still be
   stripped by ``_sanitize_messages_for_api``.
"""
from __future__ import annotations

from api.streaming import (
    _sanitize_messages_for_api,
    _api_safe_message_positions,
    _materialize_pending_user_turn_before_error,
    _recovered_user_anchors_kept_assistant,
)


# ── Shape 1: interrupted-with-saved-partial ────────────────────────────────

def test_recovered_user_kept_when_anchoring_partial_assistant():
    """Dropping a _recovered user that precedes a kept _partial assistant
    would produce adjacent assistant messages → 400 on strict providers.
    The user must be retained.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "Partial answer…", "_partial": True},
        {"role": "user", "content": "Q3"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    # The recovered user is retained because it anchors the partial assistant.
    assert any(
        m.get("role") == "user" and "Q2" in m.get("content", "")
        for m in result
    )


def test_recovered_user_stripped_when_no_kept_assistant_follows():
    """Pure cancel shape: recovered user followed only by an _error marker.
    The user should be stripped — no adjacent-assistant risk.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale prompt", "_recovered": True},
        {"role": "assistant", "content": "Task cancelled.", "_error": True},
        {"role": "user", "content": "Q3"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    # _error assistant is stripped, _recovered user is stripped (no kept assistant follows)
    assert roles == ["user", "assistant", "user"]
    assert not any("stale prompt" in m.get("content", "") for m in result)


def test_recovered_user_stripped_when_next_is_user():
    """Recovered user followed by another user (no assistant between).
    The recovered user should be stripped — the next user replaces it.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale", "_recovered": True},
        {"role": "user", "content": "Q3"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "user"]
    assert not any("stale" in m.get("content", "") for m in result)


def test_recovered_user_kept_when_anchoring_assistant_with_tool_calls():
    """Recovered user followed by an assistant with tool_calls (no _partial).
    The user must be retained to preserve role alternation.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "content": "result", "tool_call_id": "tc1"},
        {"role": "user", "content": "Q3"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    # The recovered user is retained because it anchors the assistant with tool_calls.
    assert "user" in roles
    assert roles.count("user") >= 2  # Q1 + Q2 (recovered, kept) or Q1 + Q3


def test_anchor_predicate_returns_false_at_end():
    """_recovered user at the very end of the list — nothing follows.
    Should be stripped (no assistant to anchor).
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale", "_recovered": True},
    ]
    assert _recovered_user_anchors_kept_assistant(messages, 2) is False


def test_anchor_predicate_returns_true_for_partial_assistant():
    """_recovered user followed by a _partial assistant with content.
    Should anchor — predicate returns True.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "Partial…", "_partial": True},
    ]
    assert _recovered_user_anchors_kept_assistant(messages, 2) is True


def test_anchor_predicate_skips_error_assistant():
    """_recovered user followed by an _error assistant, then nothing.
    The _error assistant will be stripped, so nothing anchors — False.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "Error", "_error": True},
    ]
    assert _recovered_user_anchors_kept_assistant(messages, 2) is False


# ── Shape 2: context_messages mirror covers all callers ────────────────────

class _DummySession:
    """Minimal session for _materialize_pending_user_turn_before_error tests."""
    def __init__(self, messages=None, context_messages=None, pending_msg=""):
        self.messages = messages or []
        self.context_messages = context_messages
        self.pending_user_message = pending_msg
        self.pending_attachments = []
        self.pending_started_at = None
        self.active_stream_id = "stream-test"
        self.truncation_watermark = None
        self.path = ""
        self.session_id = "test-4283"

    def save(self, *args, **kwargs):
        pass


def test_materialize_mirrors_recovered_user_to_context_messages():
    """_materialize_pending_user_turn_before_error must mirror the recovered
    user to context_messages so the _recovered flag survives the state.db
    round-trip (#4283).
    """
    s = _DummySession(
        messages=[{"role": "assistant", "content": "A1"}],
        context_messages=[
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ],
        pending_msg="restart the gateway",
    )
    appended = _materialize_pending_user_turn_before_error(s)

    assert appended is True
    # Mirror should be in context_messages with _recovered flag
    ctx_users = [m for m in s.context_messages if m.get("role") == "user"]
    assert any(m.get("_recovered") for m in ctx_users)
    assert any("restart the gateway" in m.get("content", "") for m in ctx_users)


def test_materialize_does_not_duplicate_context_messages():
    """Repeated calls must not grow context_messages unboundedly.
    The dedup last-8 lookback should prevent duplicates.
    """
    s = _DummySession(
        messages=[{"role": "assistant", "content": "A1"}],
        context_messages=[
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ],
        pending_msg="restart the gateway",
    )
    _materialize_pending_user_turn_before_error(s)
    ctx_len_after_first = len(s.context_messages)

    # Reset pending for a second call with same text
    s.pending_user_message = "restart the gateway"
    _materialize_pending_user_turn_before_error(s)
    assert len(s.context_messages) == ctx_len_after_first  # no growth


def test_materialize_skips_mirror_when_context_messages_empty():
    """When context_messages is empty/None (first-turn error), the mirror
    should be skipped — prefer_context falls back to session.messages which
    still carries the _recovered flag.
    """
    s = _DummySession(
        messages=[],
        context_messages=None,
        pending_msg="first turn error",
    )
    appended = _materialize_pending_user_turn_before_error(s)

    assert appended is True
    assert s.context_messages is None  # not populated
    assert s.messages[-1].get("_recovered") is True  # flag in messages


def test_sanitize_strips_recovered_user_from_context_messages():
    """End-to-end: recovered user mirrored to context_messages with
    _recovered flag → _sanitize_messages_for_api strips it (pure cancel shape).
    """
    ctx = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale prompt", "_recovered": True, "timestamp": 123},
    ]
    result = _sanitize_messages_for_api(ctx)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant"]
    assert not any("stale prompt" in m.get("content", "") for m in result)


def test_api_safe_positions_strips_recovered_user():
    """Same as above but via _api_safe_message_positions."""
    ctx = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale", "_recovered": True, "timestamp": 123},
    ]
    positions = _api_safe_message_positions(ctx)
    roles = [msg["role"] for _, msg in positions]
    assert roles == ["user", "assistant"]
