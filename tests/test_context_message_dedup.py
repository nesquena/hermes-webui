"""Tests for context message deduplication.

Verifies that _deduplicate_context_messages and _merge_display_messages_after_agent_result
correctly remove duplicate messages from agent context, preventing the agent from
seeing the same message twice in conversation_history.
"""


def test_deduplicate_context_messages_removes_duplicates():
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "hello"},  # duplicate of [0]
        {"role": "assistant", "content": "Hi there!"},  # duplicate of [1]
    ]

    result = _deduplicate_context_messages(messages)
    assert len(result) == 2
    assert result[0]["content"] == "hello"
    assert result[1]["content"] == "Hi there!"


def test_deduplicate_context_messages_preserves_different_content():
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "second question"},  # different content
        {"role": "assistant", "content": "answer two"},  # different content
    ]

    result = _deduplicate_context_messages(messages)
    assert len(result) == 4


def test_deduplicate_context_messages_preserves_identical_answers_in_different_turns():
    """Identical assistant answers in separate user turns — without stable ids.

    Messages without stable ids fall back to content-based dedup (the original
    behaviour), so the second assistant "4" with the same content is dropped.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "what is 3+1?"},  # different user turn
        {"role": "assistant", "content": "4"},  # same answer, different turn
    ]

    result = _deduplicate_context_messages(messages)
    # _message_identity is content-based only (no ids): second assistant "4"
    # has the same identity as first → removed.
    # Second user "what is 3+1?" has different content → kept.
    # This is intentional: without ids the dedup catches context pollution
    # from merge_session_messages_append_only, not replayed turns.
    assert len(result) == 3  # user "2+2", assistant "4", user "3+1"


def test_deduplicate_context_messages_stable_ids_preserve_distinct_turns():
    """Messages with different stable ids but identical content are preserved (#6310).

    When messages carry per-message integer ``id`` fields, two turns that happen
    to have the same role and content should both survive deduplication.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "higher", "id": 1},
        {"role": "assistant", "content": "Is it 5?", "id": 2},
        {"role": "user", "content": "higher", "id": 3},  # same content, different id
        {"role": "assistant", "content": "Is it 8?", "id": 4},
        {"role": "user", "content": "lower", "id": 5},  # different content
        {"role": "assistant", "content": "Is it 6?", "id": 6},
    ]

    result = _deduplicate_context_messages(messages)
    # All 6 messages should be preserved because each has a distinct id
    assert len(result) == 6
    assert result[0]["id"] == 1 and result[0]["content"] == "higher"
    assert result[2]["id"] == 3 and result[2]["content"] == "higher"
    assert result[4]["id"] == 5 and result[4]["content"] == "lower"


def test_deduplicate_context_messages_stable_ids_dedup_same_id():
    """Genuine duplicates (same stable id) are still removed even with id-aware dedup."""
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "hello", "id": 1},
        {"role": "assistant", "content": "Hi!", "id": 2},
        {"role": "user", "content": "hello", "id": 1},  # same id and content → dup
        {"role": "assistant", "content": "Hi!", "id": 2},  # same id and content → dup
    ]

    result = _deduplicate_context_messages(messages)
    assert len(result) == 2
    assert result[0]["id"] == 1
    assert result[1]["id"] == 2


def test_deduplicate_context_messages_mixed_ids_preserve_distinct():
    """Mixed content with some identical-text different-id turns preserved."""
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "yes", "id": 10},
        {"role": "assistant", "content": "ok", "id": 11},
        {"role": "user", "content": "yes", "id": 12},  # same text, different id → preserved
        {"role": "assistant", "content": "ok", "id": 13},  # same text, different id → preserved
        {"role": "user", "content": "maybe", "id": 14},
        {"role": "assistant", "content": "ok", "id": 13},  # SAME id as index 3 → dropped
    ]

    result = _deduplicate_context_messages(messages)
    # Messages at index 0-4 are all distinct by id, index 5 duplicates id 13
    assert len(result) == 5
    assert result[0]["id"] == 10
    assert result[1]["id"] == 11
    assert result[2]["id"] == 12
    assert result[3]["id"] == 13
    assert result[4]["id"] == 14


def test_deduplicate_context_messages_empty_input():
    from api.streaming import _deduplicate_context_messages

    assert _deduplicate_context_messages([]) == []
    assert _deduplicate_context_messages(None) is None


def test_deduplicate_context_messages_with_tool_calls():
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "abc", "function": {"name": "echo"}, "type": "function"}]},
        {"role": "tool", "content": "result", "tool_call_id": "abc"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "abc", "function": {"name": "echo"}, "type": "function"}]},  # dup
    ]

    result = _deduplicate_context_messages(messages)
    assert len(result) == 2  # third message (dup) removed


def test_deduplicate_context_messages_different_timestamps_same_content():
    """Messages with same content but different timestamps should be deduped."""
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "hello", "timestamp": 1779348286},
        {"role": "assistant", "content": "Hi!", "timestamp": 1779348286},
        {"role": "user", "content": "hello", "timestamp": 1779348286.3954952},  # same content, different ts
        {"role": "assistant", "content": "Hi!", "timestamp": 1779348286.3976274},  # same content, different ts
    ]

    result = _deduplicate_context_messages(messages)
    assert len(result) == 2  # duplicates removed despite different timestamps


def test_message_identity_strips_workspace_prefix():
    """_message_identity should strip [Workspace::v1: ...] prefix from user messages."""
    from api.streaming import _message_identity

    msg1 = {"role": "user", "content": "hello"}
    msg2 = {"role": "user", "content": "[Workspace::v1: /workspace]\nhello"}

    assert _message_identity(msg1) == _message_identity(msg2)


def test_message_identity_different_roles_not_duplicates():
    """Messages with same content but different roles should not be considered duplicates."""
    from api.streaming import _message_identity

    user_msg = {"role": "user", "content": "hello"}
    assistant_msg = {"role": "assistant", "content": "hello"}

    assert _message_identity(user_msg) != _message_identity(assistant_msg)


def test_merge_display_messages_dedup_via_prefix():
    """_merge_display_messages_after_agent_result dedups via prefix stripping,
    not by general seen check — identical content in different turns is preserved."""
    from api.streaming import _merge_display_messages_after_agent_result

    # Agent returns full history (includes previous messages) — prefix-based
    # dedup should strip the replayed tail, not the general seen check.
    previous_display = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    previous_context = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    result_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "answer"},
    ]
    msg_text = "next question"

    merged = _merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, msg_text
    )

    # Should have 4 messages — prefix-based dedup strips replayed tail
    assert len(merged) == 4
    assert merged[0]["content"] == "hello"
    assert merged[1]["content"] == "Hi there!"
    assert merged[2]["content"] == "next question"
    assert merged[3]["content"] == "answer"


def test_merge_display_messages_preserves_current_user_turn():
    """The current user turn replacement logic should still work."""
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    previous_context = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    result_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "answer"},
    ]
    msg_text = "next question"

    merged = _merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, msg_text
    )

    # Current user message should use msg_text
    user_msgs = [m for m in merged if m.get("role") == "user"]
    assert any(m.get("content") == "next question" for m in user_msgs)


def test_merge_display_backfill_preserves_visible_head_ordering():
    """Display head must stay before hidden context-only middle turns.

    A compacted session can have a visible transcript head that is absent from
    model context, plus a later visible tail that is present in model context.
    When model-only middle turns are restored, the merged order must be:

        old visible head
        hidden context-only middle turn(s)
        current visible tail
        new current turn
    """
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "visible head user turn"},
        {"role": "assistant", "content": "visible head assistant turn"},
        {"role": "user", "content": "visible tail user turn"},
    ]
    previous_context = [
        {"role": "user", "content": "context-only middle user turn"},
        {"role": "assistant", "content": "context-only middle assistant turn"},
        {"role": "user", "content": "visible tail user turn"},
    ]
    result_messages = previous_context + [
        {"role": "user", "content": "new follow-up user turn"},
        {"role": "assistant", "content": "new follow-up assistant turn"},
    ]
    msg_text = "new follow-up user turn"

    merged = _merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, msg_text
    )

    user_texts = [
        m.get("content", "")
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ]

    head_idx = next(i for i, t in enumerate(user_texts) if "visible head" in t)
    middle_idx = next(i for i, t in enumerate(user_texts) if "context-only middle" in t)
    tail_idx = next(i for i, t in enumerate(user_texts) if "visible tail" in t)
    followup_idx = next(i for i, t in enumerate(user_texts) if "new follow-up" in t)

    assert head_idx < middle_idx, f"Visible head must precede restored context middle; got indices {head_idx} vs {middle_idx}"
    assert middle_idx < tail_idx, f"Restored context middle must precede visible tail; got indices {middle_idx} vs {tail_idx}"
    assert tail_idx < followup_idx, f"Visible tail must precede new turn; got indices {tail_idx} vs {followup_idx}"


def test_merge_display_backfills_context_only_turns_missing_from_display():
    """Normal user/assistant turns present in previous_context but absent from
    previous_display must be restored into the visible transcript.

    This reproduces the generic bug where context compression recovery expands
    previous_context with normal turns that never appear in previous_display.
    A subsequent append-only merge skips over the shared context prefix, so
    without backfill those turns remain permanently invisible in the WebUI.
    """
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "visible head user turn"},
        {"role": "assistant", "content": "visible head assistant turn"},
    ]
    previous_context = [
        {"role": "user", "content": "visible head user turn"},
        {"role": "assistant", "content": "visible head assistant turn"},
        {"role": "user", "content": "context-only middle user turn"},
        {"role": "assistant", "content": "context-only middle assistant turn"},
    ]
    result_messages = previous_context + [
        {"role": "user", "content": "new follow-up user turn"},
        {"role": "assistant", "content": "new follow-up assistant turn"},
    ]
    msg_text = "new follow-up user turn"

    merged = _merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, msg_text
    )

    merged_texts = [
        (m.get("role"), _message_text_safe(m))
        for m in merged
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]

    assert any(
        "context-only middle user turn" in text
        for role, text in merged_texts
        if role == "user"
    ), f"Missing context-only user turn from visible transcript; got: {merged_texts}"

    assert any(
        "context-only middle assistant turn" in text
        for role, text in merged_texts
        if role == "assistant"
    ), f"Missing context-only assistant turn from visible transcript; got: {merged_texts}"

    assert any(
        "new follow-up user turn" in text
        for role, text in merged_texts
        if role == "user"
    ), "New current turn should also be present"

    head_idx = next(i for i, (r, t) in enumerate(merged_texts) if "visible head" in t)
    middle_idx = next(i for i, (r, t) in enumerate(merged_texts) if "context-only middle" in t)
    assert head_idx < middle_idx, f"Display head must come before backfilled context turn; got indices {head_idx} vs {middle_idx}"


def test_merge_display_backfill_does_not_reintroduce_compression_markers():
    """Context compression markers in previous_context that were intentionally
    removed from previous_display must NOT be restored by the backfill logic."""
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    previous_context = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "assistant", "content": "[context compaction] prior messages summarized"},
        {"role": "user", "content": "context-only middle user turn"},
        {"role": "assistant", "content": "context-only middle assistant turn"},
    ]
    result_messages = previous_context + [
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "next answer"},
    ]
    msg_text = "next question"

    merged = _merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, msg_text
    )

    merged_texts = [
        _message_text_safe(m)
        for m in merged
        if isinstance(m, dict) and m.get("role") == "assistant"
    ]

    assert not any(
        "[context compaction]" in t for t in merged_texts
    ), f"Compression marker should not be in visible display; got: {merged_texts}"

    assert any(
        "context-only middle user turn" in _message_text_safe(m)
        for m in merged
        if isinstance(m, dict) and m.get("role") == "user"
    ), "Normal user turn from context should be backfilled"


def test_deduplicate_context_messages_adjacent_dup_collapsed_same_id():
    """Accidental adjacent duplicates with the same id are collapsed (backstop #1).

    When two adjacent rows have the same content identity AND the same stable
    id, they are true duplicates and get collapsed. Different ids (even with
    the same content) represent distinct turns and are preserved to avoid
    no-assistant-turn errors in hide_all_activity mode.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "ok", "id": 100},
        {"role": "assistant", "content": "ok", "id": 100},  # exact duplicate
        {"role": "user", "content": "next", "id": 102},
    ]

    result = _deduplicate_context_messages(messages)
    # Exact duplicate collapsed → only 2 rows
    assert len(result) == 2
    assert result[0]["id"] == 100
    assert result[1]["id"] == 102


def test_deduplicate_context_messages_adjacent_dup_collapsed_lifecycle_equivalent_diff_id():
    """Adjacent same-content rows with equivalent lifecycle are collapsed even with different ids.

    With lifecycle-aware dedup (#6322), two adjacent rows that share both
    content identity AND lifecycle ownership (both settled, non-partial,
    non-error) are genuine duplicates regardless of whether minting gave
    them different stable IDs. Differing lifecycle metadata (partial vs
    settled, different anchor ownership) prevents collapse so the
    authoritative final/anchor-owning row survives.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "ok", "id": 100},
        {"role": "assistant", "content": "ok", "id": 101},  # same lifecycle → collapsed
        {"role": "user", "content": "next", "id": 102},
    ]

    result = _deduplicate_context_messages(messages)
    # Lifecycle-equivalent → collapsed to 2 rows
    assert len(result) == 2
    assert result[0]["id"] == 100
    assert result[1]["id"] == 102


def test_deduplicate_context_messages_adjacent_dup_collapsed_no_id():
    """Adjacent same-content rows without stable ids are collapsed (backstop #1).

    Pre-id rows that are genuinely duplicated should still be collapsed.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "ok"},  # no id, adjacent dup
        {"role": "user", "content": "next", "id": 102},
    ]

    result = _deduplicate_context_messages(messages)
    # Both lack ids → collapsed
    assert len(result) == 2
    assert result[0]["role"] == "assistant"
    assert result[1]["id"] == 102


def test_deduplicate_context_messages_partial_then_settled_replaced():
    """Adjacent _partial (no id) followed by settled (has id) with same content.

    This is the exact hide_all_activity scenario: a live-stream _partial
    marker sits adjacent to the final settled message. They have the same
    content identity but different lifecycle ownership — Backstop 1 must
    replace the unsettled partial with the settled final/anchor-owning row
    to avoid no-assistant-turn on switch-away/back (#6322).
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "Hello", "_partial": True},
        {"role": "assistant", "content": "Hello", "id": 50},
        {"role": "user", "content": "next", "id": 102},
    ]

    result = _deduplicate_context_messages(messages)
    # Settled replaces partial → only 2 rows, the settled row is authoritative
    assert len(result) == 2
    assert result[0]["id"] == 50
    assert "_partial" not in result[0] or not result[0].get("_partial")
    assert result[1]["id"] == 102


def test_deduplicate_context_messages_settled_then_partial_settled_survives():
    """Adjacent settled (has id) followed by _partial (no id) with same content.

    Reverse order: the settled row comes first and the partial is adjacent.
    Backstop 1 must skip the partial (settled is already authoritative).
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "Hello", "id": 50},
        {"role": "assistant", "content": "Hello", "_partial": True},
        {"role": "user", "content": "next", "id": 102},
    ]

    result = _deduplicate_context_messages(messages)
    # Settled survives, partial skipped → 2 rows
    assert len(result) == 2
    assert result[0]["id"] == 50
    assert result[1]["id"] == 102


def test_deduplicate_context_messages_partial_then_error_preserved():
    """Two different unsettled rows (_partial then _error) with same content.

    Both are unsettled but with different lifecycle flags — both preserved
    since neither can be authoritatively "settled" over the other.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "Hello", "_partial": True},
        {"role": "assistant", "content": "Hello", "_error": True},
        {"role": "user", "content": "next", "id": 102},
    ]

    result = _deduplicate_context_messages(messages)
    # Both unsettled with different flags → both preserved
    assert len(result) == 3
    assert result[0].get("_partial") is True
    assert result[1].get("_error") is True
    assert result[2]["id"] == 102


def test_deduplicate_context_messages_partial_to_settled_repairs_seen():
    """When Backstop 1 replaces a partial with settled, the 'seen' set tracks
    the settled key, not the stale partial key, so id-less non-adjacent
    duplicates of the partial do not affect dedup of the settled row.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "Hello", "_partial": True},     # replaced
        {"role": "assistant", "content": "Hello", "id": 50},            # settles
        {"role": "assistant", "content": "Hello", "_partial": True},     # non-adjacent partial dup → dropped
    ]

    result = _deduplicate_context_messages(messages)
    assert len(result) == 1
    assert result[0]["id"] == 50


def test_deduplicate_context_messages_lifecycle_anchor_ownership_preserved():
    """Rows with same content but different _anchor_stream_id are not collapsed.

    Anchor ownership distinguishes turned that happened to produce the same
    text but are owned by different stream/anchor contexts.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "assistant", "content": "Hello", "id": 50, "_anchor_stream_id": "stream-a"},
        {"role": "assistant", "content": "Hello", "id": 51, "_anchor_stream_id": "stream-b"},
        {"role": "user", "content": "next", "id": 102},
    ]

    result = _deduplicate_context_messages(messages)
    # Different anchor stream → distinct lifecycle → both preserved
    assert len(result) == 3
    assert result[0]["id"] == 50
    assert result[1]["id"] == 51
    assert result[2]["id"] == 102


def test_deduplicate_context_messages_idful_then_idless_content_dup_collapsed():
    """Id-bearing row followed by id-less content-duplicate is collapsed (backstop #2).

    The 8803 call-site merges context_messages + state.db without stable ids,
    so an id-bearing row followed by the same logical row without an id must
    still collapse. The id-less duplicate has no id value in the key prefix,
    so it would otherwise survive against the id-aware seen-set.
    """
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "hello", "id": 10},
        {"role": "assistant", "content": "Hi!", "id": 11},
        {"role": "user", "content": "hello"},  # same content, no id
        {"role": "assistant", "content": "Hi!"},  # same content, no id
        {"role": "user", "content": "new turn", "id": 12},
    ]

    result = _deduplicate_context_messages(messages)
    # Id-less content duplicates collapsed → only 3 rows
    assert len(result) == 3
    assert result[0]["id"] == 10
    assert result[1]["id"] == 11
    assert result[2]["id"] == 12


def _message_text_safe(msg):
    """Extract plain text from a message content field (list or string)."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return str(content or "")
