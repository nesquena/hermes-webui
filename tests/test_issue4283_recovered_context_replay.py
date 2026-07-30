"""Regression tests for #4283: interrupted-turn user message replay.

Three shapes must be covered:

1. **Pure cancel** — recovered user followed only by an _error marker.
   The recovered user must be stripped (no assistant to anchor).

2. **Interrupted-with-saved-partial** — recovered user followed by a kept
   _partial assistant.  The user must be retained to preserve role
   alternation (adjacent assistants → 400 on strict providers).

3. **Orphaned-tool-calls divergence** — the forward-scan anchor check must
   operate on the post-sanitized list, not the raw input, so it doesn't
   keep a recovered user based on an assistant that pass 3 will drop
   (Nesquena review on PR #4393).
"""
from __future__ import annotations

from api.streaming import (
    _sanitize_messages_for_api,
    _api_safe_message_positions,
    _materialize_pending_user_turn_before_error,
)


# ── Shape 1: pure cancel — recovered user stripped ─────────────────────────

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


def test_recovered_user_stripped_at_end_of_list():
    """Recovered user at the very end — nothing follows, no anchor.
    Should be stripped.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale", "_recovered": True},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant"]


# ── Shape 2: interrupted-with-saved-partial — recovered user kept ──────────

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


# ── Shape 3: orphaned-tool-calls divergence (Nesquena review) ──────────────

def test_stale_user_dropped_when_anchor_is_orphaned_empty_assistant():
    """Shape (a) from Nesquena's review: an empty assistant with orphaned
    tool_calls looks like a valid anchor to a forward scan, but pass 3
    strips it (all tool_calls orphaned, no content → dropped).  The
    recovered user must then also be dropped — operating on the
    post-sanitized list catches this.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale", "_recovered": True},
        # Assistant with tool_calls but no matching tool response → orphaned
        {"role": "assistant", "content": "", "tool_calls": [{"id": "tc_orphan", "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "user", "content": "Q3"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    # The orphaned assistant is dropped → recovered user has no anchor → dropped
    assert roles == ["user", "assistant", "user"]
    assert not any("stale" in m.get("content", "") for m in result)


def test_no_adjacent_assistants_when_orphan_tool_between():
    """Shape (b) from Nesquena's review: recovered user, orphan tool row,
    then a real assistant.  The orphan tool is dropped by pass 2, which
    would leave adjacent [recovered_user, assistant] — but since the
    recovered user DOES anchor the assistant, it's kept, and the roles
    are correct: [user, assistant, user, assistant, user].
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        # Orphan tool (no matching assistant tool_call in history)
        {"role": "tool", "content": "orphan result", "tool_call_id": "tc_missing"},
        {"role": "assistant", "content": "A2"},
        {"role": "user", "content": "Q3"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    # Orphan tool dropped, recovered user anchors A2 → kept
    assert roles == ["user", "assistant", "user", "assistant", "user"]


def _assert_no_adjacent_same_role(roles):
    pairs = [(roles[i], roles[i + 1]) for i in range(len(roles) - 1) if roles[i] == roles[i + 1]]
    assert not pairs, f"adjacent same-role messages would 400 on strict providers: {pairs} in {roles}"


def test_recovered_user_dropped_when_prev_kept_is_user_orphan_tool_calls_assistant():
    """Regression (Codex/Opus gate on #4393): the pass-4 keep decision must use
    the ACTUAL kept neighbours, not just 'an assistant follows'.

    Shape: U_prev, A1, recovered U, assistant(orphaned tool_calls, empty content),
    A2.  Pass 3 drops the orphaned-tool-calls assistant.  The recovered user is
    correctly KEPT here because its kept neighbours are A1 (prev) and A2 (next) —
    it separates two assistants.  Result must alternate with no adjacent users.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc_orphan", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "assistant", "content": "A2"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    _assert_no_adjacent_same_role(roles)
    assert roles == ["user", "assistant", "user", "assistant"]
    assert not any("_recovered" in m for m in result)


def test_recovered_user_dropped_when_orphan_assistant_leaves_user_prev():
    """The bug case: dropping the orphaned-tool assistant makes the recovered
    user's previous kept neighbour a USER.  Keeping the recovered user would then
    produce [user, user, assistant] → strict-provider 400.  It must be dropped.

    Shape: U_prev, assistant(orphaned tool_calls, empty), recovered U, A_next.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc_orphan", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "A2"},
    ]
    result = _sanitize_messages_for_api(messages)
    roles = [m["role"] for m in result]
    _assert_no_adjacent_same_role(roles)
    # orphan assistant dropped (pass 3); recovered user would fuse to [user, assistant]
    # cleanly if kept, but prev-kept is a user → it's a stale unanswered prompt → drop.
    assert roles == ["user", "assistant"]
    assert not any(m.get("role") == "user" and m.get("content") == "Q2" for m in result)


def test_api_safe_positions_no_adjacent_users_with_orphan_tool_calls():
    """The _api_safe_message_positions mirror must make the same neighbour-aware
    decision (Codex/Opus said mirror the fix in both functions)."""
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tc_orphan", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "A2"},
    ]
    positions = _api_safe_message_positions(messages)
    kept_roles = [messages[idx]["role"] for idx, _ in positions]
    _assert_no_adjacent_same_role(kept_roles)


# ── _api_safe_message_positions parity ─────────────────────────────────────

def test_api_safe_positions_strips_recovered_user():
    """Same as sanitize but via _api_safe_message_positions."""
    ctx = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "stale", "_recovered": True, "timestamp": 123},
    ]
    positions = _api_safe_message_positions(ctx)
    roles = [msg["role"] for _, msg in positions]
    assert roles == ["user", "assistant"]


def test_api_safe_positions_keeps_recovered_user_with_partial():
    """_api_safe_message_positions mirrors sanitize for the anchor case."""
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "Partial…", "_partial": True},
    ]
    positions = _api_safe_message_positions(messages)
    roles = [msg["role"] for _, msg in positions]
    assert roles == ["user", "assistant", "user", "assistant"]


# ── context_messages mirror (Fix 2) ────────────────────────────────────────

class _DummySession:
    """Minimal session for _materialize_pending_user_turn_before_error tests."""
    def __init__(self, messages=None, context_messages=None, pending_msg=""):
        self.messages = messages or []
        self.context_messages = context_messages
        self.pending_user_message = pending_msg
        self.pending_attachments = []
        self.pending_started_at = 1778098700.0
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
    ctx_users = [m for m in s.context_messages if m.get("role") == "user"]
    assert any(m.get("_recovered") for m in ctx_users)
    assert any("restart the gateway" in m.get("content", "") for m in ctx_users)
    assert any(m.get("timestamp") == 1778098700 for m in ctx_users)


def test_materialize_does_not_duplicate_context_messages():
    """Repeated calls must not grow context_messages unboundedly."""
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

    s.pending_user_message = "restart the gateway"
    _materialize_pending_user_turn_before_error(s)
    assert len(s.context_messages) == ctx_len_after_first


def test_materialize_skips_mirror_when_context_messages_empty():
    """When context_messages is empty/None (first-turn error), the mirror
    should be skipped — prefer_context falls back to session.messages.
    """
    s = _DummySession(
        messages=[],
        context_messages=None,
        pending_msg="first turn error",
    )
    appended = _materialize_pending_user_turn_before_error(s)

    assert appended is True
    assert s.context_messages is None
    assert s.messages[-1].get("_recovered") is True


def test_sanitize_strips_recovered_user_from_context_messages():
    """End-to-end: recovered user mirrored to context_messages with
    _recovered flag → _sanitize_messages_for_api strips it (pure cancel).
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


# ── No _recovered marker leaks into API output ─────────────────────────────

def test_no_recovered_marker_in_output():
    """The temporary _recovered marker must be stripped from all messages
    in the sanitized output — it must never reach the API.
    """
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2", "_recovered": True},
        {"role": "assistant", "content": "Partial…", "_partial": True},
        {"role": "user", "content": "Q3"},
    ]
    result = _sanitize_messages_for_api(messages)
    for msg in result:
        assert "_recovered" not in msg, f"_recovered leaked into sanitized output: {msg}"

    # Also verify the positions path strips the marker (#4393 review note).
    from api.streaming import _api_safe_message_positions
    pos_result = _api_safe_message_positions(messages)
    for idx, msg in pos_result:
        assert "_recovered" not in msg, f"_recovered leaked into positions output: {msg}"
