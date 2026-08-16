"""Per-message delete (companion to /api/session/message/delete, #6737).

PR #6740 added the truncate-keep contract (POST /api/session/truncate with
keep_count). This PR adds the per-message ID-based splice primitive
(POST /api/session/message/delete), which deletes a single row by identity
(or its adjacent turn-pair) while preserving the rest of the conversation.

The backend in api/session_ops.py uses the same _row_signature matcher as
truncate_context_for_display_keep so s.messages and s.context_messages stay
aligned after the splice. The mutation is a splice, not a slice — there is
no prefix that must line up at a turn boundary.

These tests cover the three scope/scope-collapse cases called out in the
design comment on #6737, plus the not-found path. They build Sessions
in-memory (no get_session / save round-trip) so the lock ordering is
exercised by the public entry point — we only call delete_message_at_signature
(the LOCK-holding wrapper just creates overhead).
"""
from api.models import Session
from api.session_ops import delete_message_at_signature


def _make_session():
    """3-turn session with matching ids + context rows."""
    return Session(
        session_id="t-per-message-delete",
        messages=[
            {"id": "u1", "role": "user", "content": "first prompt", "timestamp": 1.0},
            {"id": "a1", "role": "assistant", "content": "first reply", "timestamp": 1.5},
            {"id": "u2", "role": "user", "content": "second prompt", "timestamp": 2.0},
            {"id": "a2", "role": "assistant", "content": "second reply", "timestamp": 2.5},
            {"id": "u3", "role": "user", "content": "third prompt", "timestamp": 3.0},
            {"id": "a3", "role": "assistant", "content": "third reply", "timestamp": 3.5},
        ],
        context_messages=[
            {"id": "u1", "role": "user", "content": "first prompt", "timestamp": 1.0},
            {"id": "a1", "role": "assistant", "content": "first reply", "timestamp": 1.5},
            {"id": "u2", "role": "user", "content": "second prompt", "timestamp": 2.0},
            {"id": "a2", "role": "assistant", "content": "second reply", "timestamp": 2.5},
            {"id": "u3", "role": "user", "content": "third prompt", "timestamp": 3.0},
            {"id": "a3", "role": "assistant", "content": "third reply", "timestamp": 3.5},
        ],
    )


def test_pair_scope_on_user_message_drops_turn_pair():
    """Pair scope on a user prompt removes the user + the next assistant row.

    This is the common case — the assistant response to the deleted prompt is
    no longer in context for the next resend, so the next turn lands on the
    previous user prompt (a clean "edit-resend" semantics). Both display and
    context lists collapse by the same indices.
    """
    s = _make_session()
    result = delete_message_at_signature(s, "u2", scope="pair")
    assert sorted(result["removed_message_ids"]) == ["a2", "u2"]
    assert result["removed_context_count"] == 2
    assert result["old_message_count"] == 6
    assert result["new_message_count"] == 4
    contents = [m["content"] for m in s.messages]
    assert "second prompt" not in contents
    assert "second reply" not in contents
    assert contents == ["first prompt", "first reply", "third prompt", "third reply"]
    ctx_contents = [m["content"] for m in s.context_messages]
    assert "second prompt" not in ctx_contents
    assert "second reply" not in ctx_contents


def test_single_scope_on_user_message_leaves_dangling_assistant():
    """Single scope on a user prompt removes ONLY the user row.

    The assistant response stays, leaving a "dangling user turn" on the NEXT
    assistant row — the same pathology some providers reject on the next
    resend (called out in #7001). This test documents the consequence so
    anyone tightening single-scope semantics sees the impact; the default
    frontend scope is "pair" precisely so this default-path visibility is
    dim.
    """
    s = _make_session()
    result = delete_message_at_signature(s, "u2", scope="single")
    assert result["removed_message_ids"] == ["u2"]
    assert result["removed_context_count"] == 1
    assert result["new_message_count"] == 5
    contents = [m["content"] for m in s.messages]
    assert "second prompt" not in contents
    # Dangling: the assistant response that used to follow u2 is now after a1.
    assert "second reply" in contents


def test_orphan_assistant_keeps_pair_consistency():
    """Pair scope on a terminal assistant drops the user+assistant pair.

    The user→assistant pair is one turn; deleting either side of the pair
    drops both rows. This test exercises the path where the deletion target
    is the assistant role: pair drops u1 + a1, leaving the session empty.
    The "pair collapses to single" case only fires when the sibling is
    missing (terminal assistant with no preceding user — pathological in
    imported/forked sessions) — that case is covered separately.
    """
    s = Session(
        session_id="t-orphan",
        messages=[
            {"id": "u1", "role": "user", "content": "only prompt", "timestamp": 1.0},
            {"id": "a1", "role": "assistant", "content": "only reply", "timestamp": 1.5},
        ],
        context_messages=[
            {"id": "u1", "role": "user", "content": "only prompt", "timestamp": 1.0},
            {"id": "a1", "role": "assistant", "content": "only reply", "timestamp": 1.5},
        ],
    )
    result = delete_message_at_signature(s, "a1", scope="pair")
    assert sorted(result["removed_message_ids"]) == ["a1", "u1"]
    assert result["new_message_count"] == 0
    assert s.messages == []


def test_pair_collapse_when_sibling_missing():
    """Pair collapses to single when the deletion target has no pair-partner.

    Edge case: a session has a lone user row (no following assistant). The
    pair-collapse heuristic has no `next` row to drop, so the splice is the
    same as single — only the user row itself is removed. This is the "the
    entire session is the orphan" case, common in freshly-imported sessions
    the user wants to clean up.
    """
    s = Session(
        session_id="t-lone-user",
        messages=[
            {"id": "u1", "role": "user", "content": "lone prompt", "timestamp": 1.0},
        ],
        context_messages=[
            {"id": "u1", "role": "user", "content": "lone prompt", "timestamp": 1.0},
        ],
    )
    result = delete_message_at_signature(s, "u1", scope="pair")
    assert result["removed_message_ids"] == ["u1"]
    assert result["new_message_count"] == 0
    assert s.messages == []


def test_message_id_not_found_raises():
    """An unknown message_id raises ValueError (handler maps to 400)."""
    s = _make_session()
    raised = False
    try:
        delete_message_at_signature(s, "does-not-exist", scope="pair")
    except ValueError as e:
        raised = True
        assert "not found" in str(e)
    assert raised, "expected ValueError for unknown message_id"


def test_unknown_scope_raises():
    """An unknown scope raises ValueError before any mutation."""
    s = _make_session()
    old_contents = [m["content"] for m in s.messages]
    raised = False
    try:
        delete_message_at_signature(s, "u2", scope="triple")
    except ValueError as e:
        raised = True
        assert "scope" in str(e)
    assert raised, "expected ValueError for unknown scope"
    # Mutation must not have happened.
    assert [m["content"] for m in s.messages] == old_contents


def test_context_messages_with_trimming_already_absent():
    """If the context row is already gone (large-session trimming), no-op.

    A display row whose signature-counterpart is missing in context_messages
    is silently skipped — the row is already absent from the model context
    so the splice there is a no-op. This test fixture has an artifact row
    only in the display list to mimic the trimming case.
    """
    s = Session(
        session_id="t-trimmed",
        messages=[
            {"id": "u1", "role": "user", "content": "keep", "timestamp": 1.0},
            {"id": "a1", "role": "assistant", "content": "keep", "timestamp": 1.5},
            {"id": "u2", "role": "user", "content": "drop", "timestamp": 2.0},
            {"id": "a2", "role": "assistant", "content": "drop", "timestamp": 2.5},
        ],
        # context_messages was trimmed at 2 by the context engine — only u1/a1
        # remain in the model context. The full display still has u2/a2.
        context_messages=[
            {"id": "u1", "role": "user", "content": "keep", "timestamp": 1.0},
            {"id": "a1", "role": "assistant", "content": "keep", "timestamp": 1.5},
        ],
    )
    result = delete_message_at_signature(s, "u2", scope="pair")
    assert sorted(result["removed_message_ids"]) == ["a2", "u2"]
    # Context is trimmed → 0 removed rows (u2/a2 already absent from context).
    assert result["removed_context_count"] == 0
    assert result["new_message_count"] == 2
    assert [m["content"] for m in s.messages] == ["keep", "keep"]
    assert [m["content"] for m in s.context_messages] == ["keep", "keep"]


def test_truncation_watermark_stamped_after_shrink():
    """The shrink-generation + watermark fields are stamped after a delete.

    These fields are how the periodic checkpoint thread distinguishes a
    legitimate prefix from a deleted suffix during recovery — the same
    contract that truncate_session_at_keep upholds. delete_message must
    stamp them too so a delete is not mistaken for a corruption.
    """
    s = _make_session()
    # Initial state: no watermark / no generation.
    s.truncation_watermark = 0.0
    s.truncation_boundary = 0.0
    s.intentional_shrink_generation = None
    delete_message_at_signature(s, "u2", scope="pair")
    # After shrink: watermark must point at the last kept message's timestamp
    # (3.5 = a3's timestamp, since a3 is the new tail).
    assert s.truncation_watermark == 3.5
    assert s.truncation_boundary == 3.5
    assert s.intentional_shrink_generation is not None
    assert isinstance(s.intentional_shrink_generation, str)
    assert len(s.intentional_shrink_generation) > 0
