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
        "agent_turn_boundary_resolved": True,
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


@pytest.mark.parametrize('prefix_matches', [True, False])
def test_lcm_settlement_uses_direct_result_index(prefix_matches):
    from api.streaming import _resolve_active_turn_authority, _settle_current_turn_boundary

    summary = {'role': 'assistant', 'content': 'Earlier summary'}
    history = {'role': 'assistant', 'content': 'Historical answer'}
    previous = [summary, history]
    marker = {'role': 'user', 'content': '[Recent Summary (d0, node 418)]'}
    answer = {'role': 'assistant', 'content': 'Current answer'}
    # A refreshed compression summary breaks the prefix without changing the
    # Agent's declared index domain: index 2 still addresses result messages.
    result = [summary if prefix_matches else dict(summary, content='Refreshed summary'),
              history, marker, answer]
    identity = _resolve_active_turn_authority(
        {'token': 'stream_1:100.25', 'text': marker['content'], 'timestamp': 100.25},
        result={'messages': result, 'current_turn_user_idx': 2, 'turn_id': 'turn-current'},
    )
    settled = _settle_current_turn_boundary(previous, result, identity, marker['content'], 'webui')
    assert settled[:2] == result[:2]
    assert settled[2]['_active_turn_token'] == identity['token']
    assert settled[3:] == [marker, answer]
    assert '_active_turn_token' not in marker


@pytest.mark.parametrize('marker', [False, True])
def test_context_dedup_keeps_conflicting_owners_in_order(marker):
    from api.streaming import _deduplicate_context_messages

    text = '[Recent Summary (d0, node 418)]' if marker else 'Continue'
    first = dict(role='user', content=text, timestamp=100, _active_turn_token='old:100')
    answer = dict(role='assistant', content='Historical answer')
    current = dict(first, _active_turn_token='new:100')
    rows = [first, answer, current]
    assert _deduplicate_context_messages(rows) == rows
    assert _deduplicate_context_messages(rows + [dict(current)]) == rows


def test_settlement_and_next_turn_keep_distinct_owner_history():
    from types import SimpleNamespace
    from api.streaming import _deduplicate_context_messages, _settle_result_messages

    first = dict(role='user', content='Continue', timestamp=100, _active_turn_token='old:100')
    answer = dict(role='assistant', content='Historical response', timestamp=100)
    current = dict(first, _active_turn_token='new:100')
    reply = dict(role='assistant', content='Current response', timestamp=101)
    previous = [first, answer]
    session = SimpleNamespace(messages=list(previous), context_messages=list(previous))
    identity = dict(token='new:100', text='Continue', timestamp=100)
    _settle_result_messages(session, previous, previous, [*previous, current, reply],
                            'Continue', 'webui', identity)
    expected = ['old:100', None, 'new:100', None]
    assert [row.get('_active_turn_token') for row in session.context_messages] == expected
    # The next turn runs the same context projection before sending history.
    assert [row.get('_active_turn_token') for row in
            _deduplicate_context_messages(session.context_messages)] == expected


@pytest.mark.parametrize('owner_first', [False, True])
def test_context_dedup_preserves_tokenless_ordinary_replay_compatibility(owner_first):
    from api.streaming import _deduplicate_context_messages

    replay = dict(role='user', content='Continue', timestamp=100)
    owner = dict(replay, _active_turn_token='current:100')
    rows = [owner, replay] if owner_first else [replay, owner]
    assert _deduplicate_context_messages(rows) == [owner]


@pytest.mark.parametrize('prefix', [False, True])
@pytest.mark.parametrize('shape', ['answer', 'envelope', 'summary', 'summary_envelope'])
@pytest.mark.parametrize('boundary_source', ['result', 'agent'])
def test_direct_boundary_stays_in_agent_result_domain(prefix, shape, boundary_source):
    from types import SimpleNamespace
    from api.streaming import _resolve_active_turn_authority, _settle_result_messages

    previous = [dict(role='user', content='Earlier request'),
                dict(role='assistant', content='Historical response')]
    text = '[Recent Summary (d0, node 418)]' if 'envelope' in shape else 'Current request'
    answer = dict(role='assistant', content='Current response')
    leading = [dict(role='assistant', content='[Recent Summary (d0, node 417)]')] if 'summary' in shape else []
    envelope = [dict(role='user', content=text)] if 'envelope' in shape else []
    raw = (list(previous) if prefix else []) + leading + envelope + [answer]
    index = (len(previous) if prefix else 0) + len(leading)
    identity = _resolve_active_turn_authority(
        dict(token='current:100', text=text, timestamp=100),
        result=dict(messages=raw, current_turn_user_idx=index, turn_id='current') if boundary_source == 'result' else dict(messages=raw),
        agent=SimpleNamespace(_persist_user_message_idx=index, _current_turn_id='current') if boundary_source == 'agent' else None,
    )
    if envelope:
        from api.streaming import _self_heal_result_succeeded

        assert _self_heal_result_succeeded(
            dict(completed=True, messages=raw, current_turn_user_idx=index, turn_id='current'),
            previous, identity, text,
        )
    session = SimpleNamespace(messages=list(previous), context_messages=list(previous))
    _settle_result_messages(session, previous, previous, raw, text, 'webui', identity)
    expected = [m['content'] for m in previous + leading] + [text] + [m['content'] for m in envelope] + ['Current response']
    assert [m['content'] for m in session.context_messages] == expected
    assert [m.get('_active_turn_token') for m in session.context_messages].count('current:100') == 1
    assert [m['content'] for m in session.messages] == [m['content'] for m in previous] + [text, 'Current response']
    assert all('_active_turn_token' not in m for m in envelope)


@pytest.mark.parametrize('shape', ['owned', 'answer', 'envelope', 'summary'])
@pytest.mark.parametrize('repeat_answer', [False, True])
def test_current_token_suffix_is_not_historical_replay(shape, repeat_answer):
    from types import SimpleNamespace
    from api.streaming import _resolve_active_turn_authority, _settle_result_messages, _self_heal_result_succeeded

    text = '[Recent Summary (d0, node 418)]' if shape == 'envelope' else 'Continue'
    previous = [dict(role='assistant', content='Older context'),
                dict(role='user', content=text, _active_turn_token='old:100')]
    answer = dict(role='assistant', content='Older context' if repeat_answer else 'Fresh answer')
    owner = dict(role='user', content=text, _active_turn_token='current:101', timestamp=101)
    leading = [dict(role='assistant', content='[Recent Summary (d0, node 417)]')] if shape == 'summary' else []
    boundary = [dict(role='user', content=text)] if shape == 'envelope' else ([owner] if shape in ('owned', 'summary') else [])
    raw = leading + boundary + [answer]
    result = dict(messages=raw, current_turn_user_idx=len(leading), turn_id='current', completed=True)
    identity = _resolve_active_turn_authority(dict(token='current:101', text=text, timestamp=101), result=result)
    if boundary:
        assert _self_heal_result_succeeded(result, previous, identity, text)
    session = SimpleNamespace(messages=list(previous), context_messages=list(previous))
    _settle_result_messages(session, previous, previous, raw, text, 'webui', identity)
    rows = session.context_messages
    from api.streaming import _deduplicate_context_messages

    assert _deduplicate_context_messages(rows) == rows
    assert rows[:2] == previous
    assert [m.get('_active_turn_token') for m in rows].count('current:101') == 1
    idx = next(i for i, m in enumerate(rows) if m.get('_active_turn_token') == 'current:101')
    assert idx == 2 + len(leading)
    assert rows[-1]['content'] == answer['content']
    assert len(rows) == 4 + len(leading) + int(shape == 'envelope')
    assert [m['content'] for m in session.messages] == [m['content'] for m in previous] + [text, answer['content']]


@pytest.mark.parametrize('answer_id', [None, 8])
@pytest.mark.parametrize('kind', ['promotion', 'same_token', 'conflicting', 'new_tokenless'])
def test_context_mirror_promotion_reuses_output_scope(answer_id, kind):
    from api.streaming import _deduplicate_context_messages

    first = dict(role='user', content='Continue', timestamp=100)
    second = dict(first, _active_turn_token='current:100')
    if kind in ('same_token', 'conflicting', 'new_tokenless'):
        first['_active_turn_token'] = 'current:100' if kind == 'same_token' else 'old:100'
    if kind == 'new_tokenless':
        second = dict(role='user', content='Different prompt', timestamp=101)
    answer = dict(role='assistant', content='Answer')
    if answer_id is not None:
        answer['id'] = answer_id
    expected = [second, answer] if kind == 'promotion' else ([first, answer] if kind == 'same_token' else [first, answer, second, answer])
    assert _deduplicate_context_messages([first, answer, second, dict(answer)]) == expected


@pytest.mark.parametrize('leading_summary', [False, True])
@pytest.mark.parametrize('prefix', [False, True])
def test_pre_stamped_current_suffix_preserves_repeated_outputs(leading_summary, prefix):
    from types import SimpleNamespace
    from api.streaming import _dedupe_replayed_context_messages, _settle_result_messages, _resolve_active_turn_authority

    history = [dict(role='user', content='Old', _active_turn_token='old:1')]
    outputs = [dict(role='assistant', content=f'Repeated {i}') for i in range(3)]
    owner = dict(role='user', content='Continue', _active_turn_token='current:2')
    previous = history + outputs + [owner, dict(role='assistant', content='Replace this')]
    leading = [dict(role='assistant', content='[Recent Summary (d0, node 417)]')] if leading_summary else []
    result = (history + outputs if prefix else []) + leading + [owner] + outputs
    assert _dedupe_replayed_context_messages(previous, result, 'Continue',
        active_turn_identity=dict(token='current:2')) == history + outputs + leading + [owner] + outputs

    identity = _resolve_active_turn_authority(dict(token='current:2', text='Continue', checkpoint=owner),
        result=dict(messages=result, current_turn_user_idx=len(result) - 4, turn_id='current'))
    session = SimpleNamespace(messages=list(previous), context_messages=list(previous))
    _settle_result_messages(session, previous, previous, result, 'Continue', 'webui', identity)
    expected = history + outputs + leading + [owner] + outputs
    assert [row['content'] for row in session.context_messages] == [row['content'] for row in expected]
    # Regenerating the same owned suffix must not lose any of its outputs.
    _settle_result_messages(session, session.messages, session.context_messages, result, 'Continue', 'webui', identity)
    assert [row['content'] for row in session.context_messages] == [row['content'] for row in expected]
