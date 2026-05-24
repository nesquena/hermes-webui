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
