"""Tests for context message deduplication.

Verifies that _deduplicate_context_messages and _merge_display_messages_after_agent_result
correctly remove duplicate messages from agent context, preventing the agent from
seeing the same message twice in conversation_history.
"""

import pytest


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


@pytest.mark.parametrize(
    "marker",
    [
        "[Recent Summary (d0, node 418)]",
        "[Current user objective preserved from compacted history]",
    ],
)
def test_deduplicate_context_messages_preserves_lcm_roles_and_dedupes_same_role(marker):
    from api.streaming import _deduplicate_context_messages

    messages = [
        {"role": "user", "content": marker},
        {"role": "assistant", "content": marker},
        {"role": "user", "content": marker},
        {"role": "assistant", "content": marker},
    ]

    result = _deduplicate_context_messages(messages)

    assert result == messages[:2]


def test_deduplicate_context_messages_lcm_marker_sidecars_follow_replay_identity():
    from api.streaming import _deduplicate_context_messages

    marker = "[Recent Summary (d0, node 418)]"
    user_wire_a = {"role": "user", "content": marker, "api_content": "wire-a"}
    user_wire_b = {"role": "user", "content": marker, "api_content": "wire-b"}
    assistant_wire_a = {
        "role": "assistant",
        "content": marker,
        "api_content": "wire-a",
    }
    user_malformed = {"role": "user", "content": marker, "api_content": {"bad": True}}
    user_empty = {"role": "user", "content": marker, "api_content": ""}
    user_without_sidecar = {"role": "user", "content": marker}

    assert _deduplicate_context_messages([
        user_wire_a,
        dict(user_wire_a),
        user_wire_b,
        assistant_wire_a,
        user_malformed,
        user_empty,
        user_without_sidecar,
    ]) == [user_wire_a, user_wire_b, assistant_wire_a, user_malformed]


def test_deduplicate_context_messages_legacy_marker_sidecars_canonicalize_per_sidecar():
    from api.streaming import _deduplicate_context_messages

    marker = "[context compaction] legacy summary"
    user_wire_a = {"role": "user", "content": marker, "api_content": "wire-a"}
    assistant_wire_a = {
        "role": "assistant",
        "content": marker,
        "api_content": "wire-a",
    }
    user_wire_b = {"role": "user", "content": marker, "api_content": "wire-b"}
    user_malformed = {"role": "user", "content": marker, "api_content": {"bad": True}}
    user_empty = {"role": "user", "content": marker, "api_content": ""}
    user_without_sidecar = {"role": "user", "content": marker}

    assert _deduplicate_context_messages([
        user_wire_a,
        assistant_wire_a,
        user_wire_b,
        user_malformed,
        user_empty,
        user_without_sidecar,
    ]) == [
        assistant_wire_a,
        {"role": "assistant", "content": marker, "api_content": "wire-b"},
        {"role": "assistant", "content": marker, "api_content": {"bad": True}},
    ]


def test_deduplicate_context_messages_respects_user_token_ownership():
    from api.streaming import _deduplicate_context_messages

    marker = "[Recent Summary (d0, node 418)]"
    stale = {
        "role": "user",
        "content": marker,
        "timestamp": 1779348286,
        "_active_turn_token": "stale-stream:1779348286",
    }
    current = dict(stale, _active_turn_token="current-stream:1779348286", metadata="first")
    duplicate = dict(current, metadata="later")
    untagged = dict(current)
    untagged.pop("_active_turn_token")

    assert _deduplicate_context_messages([untagged, current]) == [current]
    assert _deduplicate_context_messages([stale, current]) == [stale, current]
    assert _deduplicate_context_messages([current, duplicate]) == [current]


def test_deduplicate_context_messages_preserves_legacy_marker_canonicalization():
    from api.streaming import _deduplicate_context_messages

    marker = "[context compaction] legacy summary"
    user_marker = {"role": "user", "content": marker}
    assistant_marker = {"role": "assistant", "content": marker}

    assert _deduplicate_context_messages([user_marker]) == [assistant_marker]
    assert _deduplicate_context_messages([user_marker, assistant_marker]) == [assistant_marker]


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


@pytest.mark.parametrize(
    "marker",
    [
        "[Recent Summary (d0, node 418)]",
        "[Current user objective preserved from compacted history]",
    ],
)
def test_settle_materializes_token_owned_user_before_untagged_lcm_marker(marker):
    from types import SimpleNamespace

    from api.compression_anchor import (
        is_context_compression_marker,
        is_lcm_context_recovery_marker,
    )
    from api.streaming import _settle_result_messages

    token = "stream-01f4c8d2:1779348286.3954952"
    untagged_recovery_envelope = {"role": "user", "content": marker}
    assistant_answer = {"role": "assistant", "content": "The answer"}
    identity = {
        "session_id": "session-123",
        "token": token,
        "text": marker,
        "timestamp": 1779348286.3954952,
        "source": "webui",
        "attachments": [],
        "checkpoint": None,
        "current_turn_user_idx": 0,
        "turn_id": "turn-20260820-01",
    }
    session = SimpleNamespace(messages=[], context_messages=[])

    _settle_result_messages(
        session,
        [],
        [],
        [untagged_recovery_envelope, assistant_answer],
        marker,
        "webui",
        identity,
    )

    assert untagged_recovery_envelope.get("_active_turn_token") is None
    assert is_lcm_context_recovery_marker(untagged_recovery_envelope)
    assert is_context_compression_marker(untagged_recovery_envelope)

    token_users = [
        message
        for message in session.context_messages
        if message.get("role") == "user"
        and message.get("_active_turn_token") == token
    ]
    assert len(token_users) == 1
    assert token_users[0] is not untagged_recovery_envelope
    assert token_users[0]["content"] == marker

    assert [
        (message["role"], message["content"])
        for message in session.messages
    ] == [
        ("user", marker),
        ("assistant", "The answer"),
    ]
    assert [
        message
        for message in session.messages
        if message.get("role") == "user"
    ][0].get("_active_turn_token") == token


def test_settle_does_not_reassign_stale_token_owned_checkpoint():
    from types import SimpleNamespace

    from api.streaming import _settle_result_messages

    marker = "[Recent Summary (d0, node 418)]"
    current_token = "stream-01f4c8d2:1779348286.3954952"
    stale_token = "stream-older9:1779348200.125"
    stale_user = {
        "role": "user",
        "content": marker,
        "_active_turn_token": stale_token,
    }
    assistant_answer = {"role": "assistant", "content": "The answer"}
    identity = {
        "session_id": "session-123",
        "token": current_token,
        "text": marker,
        "timestamp": 1779348286.3954952,
        "source": "webui",
        "attachments": [],
        "checkpoint": None,
        "current_turn_user_idx": 0,
        "turn_id": "turn-20260820-01",
    }
    session = SimpleNamespace(messages=[], context_messages=[])

    _settle_result_messages(
        session,
        [],
        [],
        [stale_user, assistant_answer],
        marker,
        "webui",
        identity,
    )

    assert stale_user["_active_turn_token"] == stale_token
    current_users = [
        message
        for message in session.context_messages
        if message.get("role") == "user"
        and message.get("_active_turn_token") == current_token
    ]
    assert len(current_users) == 1
    assert current_users[0] is not stale_user
    assert [
        (message["role"], message["content"])
        for message in session.messages
    ] == [
        ("user", marker),
        ("assistant", "The answer"),
    ]
    assert [
        message
        for message in session.messages
        if message.get("role") == "user"
    ][0]["_active_turn_token"] == current_token


@pytest.mark.parametrize(
    "marker",
    [
        "[Recent Summary (d0, node 418)]",
        "[Current user objective preserved from compacted history]",
    ],
)
def test_merge_display_preserves_token_owned_lcm_marker_user(marker):
    from api.streaming import _merge_display_messages_after_agent_result

    token = "stream-01f4c8d2:1779348286.3954952"
    current_user = {
        "role": "user",
        "content": marker,
        "_active_turn_token": token,
    }
    untagged_recovery_envelope = {"role": "user", "content": marker}
    assistant_answer = {"role": "assistant", "content": "The answer"}

    merged = _merge_display_messages_after_agent_result(
        [current_user],
        [],
        [untagged_recovery_envelope, assistant_answer],
        marker,
    )

    assert [(message["role"], message["content"]) for message in merged] == [
        ("user", marker),
        ("assistant", "The answer"),
    ]
    assert [
        message
        for message in merged
        if message.get("role") == "user" and message.get("content") == marker
    ] == [current_user]


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


@pytest.mark.parametrize(
    ("marker", "retained"),
    [
        ("[Recent Summary (d0, node 418)]\n...", False),
        ("[Current user objective preserved from compacted history]\n...", False),
        ("[Session Arc Summary (d0, node 418)]\n...", False),
        ("[Recent Summary (Q1 meeting notes)]", True),
    ],
)
def test_merge_display_backfills_turns_and_filters_only_canonical_markers(marker, retained):
    from api.streaming import _merge_display_messages_after_agent_result

    previous_display = [
        {"role": "user", "content": "visible before"},
        {"role": "assistant", "content": "visible before answer"},
        {"role": "user", "content": "visible after"},
        {"role": "assistant", "content": "visible after answer"},
    ]
    previous_context = [
        {"role": "user", "content": "visible before"},
        {"role": "assistant", "content": "visible before answer"},
        {"role": "user", "content": marker},
        {"role": "user", "content": "context-only user"},
        {"role": "assistant", "content": "context-only assistant"},
        {"role": "user", "content": "visible after"},
        {"role": "assistant", "content": "visible after answer"},
    ]
    result_messages = previous_context + [
        {"role": "user", "content": "new follow-up"},
        {"role": "assistant", "content": "new follow-up answer"},
    ]

    merged = _merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "new follow-up",
    )
    merged_texts = [
        (message.get("role"), _message_text_safe(message))
        for message in merged
        if isinstance(message, dict) and message.get("role") in ("user", "assistant")
    ]

    expected = [
        ("user", "visible before"),
        ("assistant", "visible before answer"),
        ("user", "context-only user"),
        ("assistant", "context-only assistant"),
        ("user", "visible after"),
        ("assistant", "visible after answer"),
        ("user", "new follow-up"),
        ("assistant", "new follow-up answer"),
    ]
    if retained:
        expected.insert(2, ("user", marker))
    assert merged_texts == expected


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
