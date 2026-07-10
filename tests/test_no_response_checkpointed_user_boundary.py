"""Regression tests: checkpointed current-user row must not trigger no_response.

Incident: a long-running session (repeated context compaction + mid-turn
checkpoints) had the CURRENT user prompt already persisted as the last row of
``previous_display`` when the turn finalized. ``_turn_transcript_lacks_final_
assistant_answer`` treated any ``current_user_idx < len(previous_display)`` as
"active turn missing from transcript", appended a phantom duplicate of the
prompt AFTER the real assistant answer, and concluded the completed turn was a
silent provider failure — rendering a false "No response from provider" card
below a valid answer.

The fix moves the boundary back by one when the last snapshot row IS the
current user turn (same checkpoint detection the display merge uses via
``current_user_already_checkpointed``).
"""

from __future__ import annotations

import api.streaming as streaming


def _lacks(merged, previous_display, msg_text, **kw):
    kw.setdefault("source", "webui")
    kw.setdefault("drop_replayed_assistant", False)
    return streaming._turn_transcript_lacks_final_assistant_answer(
        merged, previous_display, msg_text, **kw
    )


def test_checkpointed_current_user_with_valid_answer_is_not_silent_failure():
    # Exact incident geometry: snapshot already ends with the current prompt,
    # merged transcript has that same row followed by a real answer.
    msg_text = "Continue"
    previous_display = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": msg_text},  # eager checkpoint of CURRENT turn
    ]
    merged = list(previous_display) + [
        {"role": "assistant", "content": "the real completed answer"},
    ]
    assert _lacks(merged, previous_display, msg_text) is False


def test_checkpointed_current_user_with_no_answer_still_flags_failure():
    # Genuine silent failure in the checkpointed state must still be caught.
    msg_text = "Continue"
    previous_display = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": msg_text},
    ]
    merged = list(previous_display)  # agent produced nothing
    assert _lacks(merged, previous_display, msg_text) is True


def test_checkpointed_current_user_with_replayed_stale_answer_still_flags_failure():
    # A provider replaying a PRIOR assistant answer as the "new" tail must not
    # satisfy the current turn (drop_replayed_assistant path).
    msg_text = "Continue"
    stale_answer = {"role": "assistant", "content": "earlier answer"}
    previous_display = [
        {"role": "user", "content": "earlier question"},
        dict(stale_answer),
        {"role": "user", "content": msg_text},
    ]
    merged = list(previous_display) + [dict(stale_answer)]
    assert (
        _lacks(merged, previous_display, msg_text, drop_replayed_assistant=True)
        is True
    )


def test_older_duplicate_user_row_keeps_original_boundary_semantics():
    # Original intent of the boundary check is preserved: when the matched user
    # row is an OLDER duplicate inside the snapshot (not the last row), the
    # pending prompt is still materialized and an unanswered turn is flagged.
    msg_text = "same question"
    previous_display = [
        {"role": "user", "content": msg_text},  # older row, same text
        {"role": "assistant", "content": "old answer"},
    ]
    merged = list(previous_display)  # current turn produced nothing new
    assert _lacks(merged, previous_display, msg_text) is True


def test_workspace_prefixed_checkpoint_row_is_recognized():
    # WebUI persists the model-facing prompt with a workspace prefix while the
    # bare text is what finalization compares against; the checkpoint detection
    # must match through _looks_like_current_user_turn.
    msg_text = "Continue"
    prefixed = "[Workspace::v1: /home/user/workspace]\n" + msg_text
    previous_display = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": prefixed},
    ]
    merged = list(previous_display) + [
        {"role": "assistant", "content": "the real completed answer"},
    ]
    assert _lacks(merged, previous_display, msg_text) is False


def test_no_checkpoint_normal_turn_unchanged():
    # Normal (non-checkpointed) turns: snapshot does NOT contain the current
    # prompt; behavior must be identical to before the fix in both directions.
    msg_text = "new question"
    previous_display = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    answered = list(previous_display) + [
        {"role": "user", "content": msg_text},
        {"role": "assistant", "content": "fresh answer"},
    ]
    unanswered = list(previous_display) + [
        {"role": "user", "content": msg_text},
    ]
    assert _lacks(answered, previous_display, msg_text) is False
    assert _lacks(unanswered, previous_display, msg_text) is True
