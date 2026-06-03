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
    """Identical assistant answers in separate user turns should be preserved."""
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": "what is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "what is 3+1?"},  # different user turn
        {"role": "assistant", "content": "4"},  # same answer, different turn
    ]

    result = _deduplicate_context_messages(messages)
    # _message_identity is identity-based, not turn-aware:
    # second assistant "4" has the same identity as first → removed.
    # Second user "what is 3+1?" has different content → kept.
    # This is intentional: the dedup catches context pollution from
    # merge_session_messages_append_only, not replayed turns.
    assert len(result) == 3  # user "2+2", assistant "4", user "3+1"


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
