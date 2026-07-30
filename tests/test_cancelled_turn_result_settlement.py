from __future__ import annotations

from copy import deepcopy

from api.streaming import _preserve_cancelled_turn_tool_context, _sanitize_messages_for_api


class _DummySession:
    def __init__(
        self,
        *,
        context_messages=None,
        messages=None,
        active_stream_id="stream-active",
        pending_user_message=None,
        pending_attachments=None,
    ):
        self.session_id = "cancelled-context-session"
        self.path = ""
        self.active_stream_id = active_stream_id
        self.pending_user_message = pending_user_message
        self.pending_attachments = [] if pending_attachments is None else list(pending_attachments)
        self.pending_started_at = 1_700_000_000
        self.pending_user_source = "webui"
        self.messages = list(messages or [])
        self.context_messages = deepcopy(context_messages or [])

    def save(self, *args, **kwargs):
        return None


def _turn_identity(msg_text, current_turn_user_idx, *, token="tok"):
    return {"token": token, "source": "webui", "text": msg_text, "timestamp": 1_700_000_000, "attachments": [], "current_turn_user_idx": current_turn_user_idx, "turn_id": "turn"}


def _interrupted_user(msg_text):
    return {"role": "user", "content": msg_text, "_recovered": True}


def _cancelled_marker():
    return {"role": "assistant", "content": "**Task cancelled:** Task cancelled.", "_error": True}


def _compression_marker(content="[context compaction — summary]"):
    return {"role": "assistant", "content": content, "_compressed_summary": True}


def _settle(
    *, session, msg_text, result_messages, result_identity, ephemeral=False, previous_context_messages=None
):
    if previous_context_messages is None:
        previous_context_messages = list(session.context_messages or [])
    return _preserve_cancelled_turn_tool_context(session, "stream-active", result_messages, list(previous_context_messages), msg_text, "webui", result_identity, ephemeral=ephemeral)


def _tool_result_messages(msg_text):
    return [
        {"role": "user", "content": msg_text},
        {
            "role": "assistant",
            "tool_calls": [{"id": "tc-commit", "type": "function", "function": {"name": "git_commit", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "commit abc1234", "tool_call_id": "tc-commit"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "tc-status", "type": "function", "function": {"name": "git_status", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "clean", "tool_call_id": "tc-status"},
        {"role": "assistant", "tool_calls": [{"id": "tc-unused", "type": "function", "function": {"name": "noop", "arguments": "{}"}}]},
        {"role": "assistant", "content": "trailing prose"},
    ]


def _repeated_prompt_with_stale_index_messages(prompt):
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": "old"},
        {"role": "assistant", "tool_calls": [{"id": "tc-old", "type": "function", "function": {"name": "noop", "arguments": "{}"}}]},
        {"role": "tool", "content": "old ok", "tool_call_id": "tc-old"},
        {
            "role": "user",
            "content": prompt,
            "_active_turn_token": "turn-current",
        },
        {"role": "assistant", "tool_calls": [{"id": "tc-new", "type": "function", "function": {"name": "noop", "arguments": "{}"}}]},
        {"role": "tool", "content": "new ok", "tool_call_id": "tc-new"},
        {"role": "assistant", "content": "tail"},
    ]


def test_preserve_completed_tool_rows_and_block_newer_turn():
    msg_text = "remove last commit"
    base_context = [{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"}]
    result_payload = {"messages": _tool_result_messages(msg_text), "final_response": "ok"}
    identity = _turn_identity(msg_text, current_turn_user_idx=0)

    session = _DummySession(
        context_messages=deepcopy(base_context),
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id=None,
    )
    assert _settle(session=session, msg_text=msg_text, result_messages=result_payload, result_identity=identity) is True
    settled = _sanitize_messages_for_api(session.context_messages)
    assert [m.get("role") for m in settled] == ["user", "assistant", "user", "assistant", "tool", "assistant", "tool"]
    assert not any(m.get("content") == "trailing prose" for m in settled)
    assert not any(m.get("role") == "assistant" and any(tc.get("id") == "tc-unused" for tc in (m.get("tool_calls") or [])) for m in settled)

    session_with_tail = _DummySession(
        context_messages=base_context
        + _tool_result_messages(msg_text)
        + [{"role": "assistant", "content": "late trailing prose"}],
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id=None,
    )
    assert _settle(
        session=session_with_tail,
        msg_text=msg_text,
        result_messages=result_payload,
        result_identity=identity,
        previous_context_messages=deepcopy(base_context),
    ) is True
    late_settled = _sanitize_messages_for_api(session_with_tail.context_messages)
    assert late_settled == [
        {"role": "user", "content": "seed"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": msg_text},
        {"role": "assistant", "tool_calls": [{"id": "tc-commit", "type": "function", "function": {"name": "git_commit", "arguments": "{}"}}]},
        {"role": "tool", "content": "commit abc1234", "tool_call_id": "tc-commit"},
        {"role": "assistant", "tool_calls": [{"id": "tc-status", "type": "function", "function": {"name": "git_status", "arguments": "{}"}}]},
        {"role": "tool", "content": "clean", "tool_call_id": "tc-status"},
    ]

    assert _settle(
        session=session,
        msg_text=msg_text,
        result_messages=result_payload,
        result_identity=identity,
        previous_context_messages=deepcopy(base_context),
    ) is True
    assert settled == _sanitize_messages_for_api(session.context_messages)

    session_empty_snapshot = _DummySession(
        context_messages=base_context
        + _tool_result_messages(msg_text)
        + [{"role": "assistant", "content": "late trailing prose"}],
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id=None,
    )
    assert _settle(
        session=session_empty_snapshot,
        msg_text=msg_text,
        result_messages=result_payload,
        result_identity=identity,
        previous_context_messages=[],
    ) is True
    empty_snapshot_settled = _sanitize_messages_for_api(session_empty_snapshot.context_messages)
    assert empty_snapshot_settled == [
        {"role": "user", "content": msg_text},
        {"role": "assistant", "tool_calls": [{"id": "tc-commit", "type": "function", "function": {"name": "git_commit", "arguments": "{}"}}]},
        {"role": "tool", "content": "commit abc1234", "tool_call_id": "tc-commit"},
        {"role": "assistant", "tool_calls": [{"id": "tc-status", "type": "function", "function": {"name": "git_status", "arguments": "{}"}}]},
        {"role": "tool", "content": "clean", "tool_call_id": "tc-status"},
    ]

    stale = _DummySession(
        context_messages=deepcopy(base_context),
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id="other",
    )
    assert not _settle(session=stale, msg_text=msg_text, result_messages=result_payload, result_identity=identity)
    assert stale.context_messages == base_context

    newer = _DummySession(
        context_messages=deepcopy(base_context),
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker(), {"role": "assistant", "content": "later"}, {"role": "user", "content": "next"}],
        active_stream_id=None,
    )
    assert not _settle(session=newer, msg_text=msg_text, result_messages=result_payload, result_identity=identity)
    assert newer.context_messages == base_context

    pending_tail = _DummySession(
        context_messages=deepcopy(base_context),
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id=None,
        pending_user_message="next",
    )
    assert not _settle(session=pending_tail, msg_text=msg_text, result_messages=result_payload, result_identity=identity)
    assert pending_tail.context_messages == base_context


def test_preserve_short_circuit_for_compressed_live_context():
    msg_text = "remove last commit"
    identity = _turn_identity(msg_text, current_turn_user_idx=0)
    result_payload = {"messages": _tool_result_messages(msg_text), "final_response": "ok"}
    base_context = [{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"}]
    first_compression_session = _DummySession(
        context_messages=base_context + [_compression_marker("[context compaction — replacement marker]")],
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id=None,
    )
    assert _settle(
        session=first_compression_session,
        msg_text=msg_text,
        result_messages=result_payload,
        result_identity=identity,
        previous_context_messages=base_context,
    ) is True
    assert first_compression_session.context_messages == base_context + [_compression_marker("[context compaction — replacement marker]")]

    pre_turn_context = base_context + [_compression_marker("[context compaction — old marker]")]
    session = _DummySession(
        context_messages=base_context + [_compression_marker("[context compaction — replacement marker]")],
        messages=pre_turn_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id=None,
    )
    assert _settle(
        session=session,
        msg_text=msg_text,
        result_messages=result_payload,
        result_identity=identity,
        previous_context_messages=pre_turn_context,
    ) is True
    assert session.context_messages == base_context + [_compression_marker("[context compaction — replacement marker]")]

    unchanged_marker_session = _DummySession(
        context_messages=base_context + [_compression_marker("[context compaction — replacement marker]")],
        messages=base_context + [_interrupted_user(msg_text), _cancelled_marker()],
        active_stream_id=None,
    )
    assert _settle(
        session=unchanged_marker_session,
        msg_text=msg_text,
        result_messages=result_payload,
        result_identity=identity,
        previous_context_messages=base_context + [_compression_marker("[context compaction — replacement marker]")],
    ) is True
    assert _sanitize_messages_for_api(unchanged_marker_session.context_messages) == [
        {"role": "user", "content": "seed"},
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "[context compaction — replacement marker]"},
        {"role": "user", "content": msg_text},
        {"role": "assistant", "tool_calls": [{"id": "tc-commit", "type": "function", "function": {"name": "git_commit", "arguments": "{}"}}]},
        {"role": "tool", "content": "commit abc1234", "tool_call_id": "tc-commit"},
        {"role": "assistant", "tool_calls": [{"id": "tc-status", "type": "function", "function": {"name": "git_status", "arguments": "{}"}}]},
        {"role": "tool", "content": "clean", "tool_call_id": "tc-status"},
    ]


def test_repeated_prompt_uses_active_turn_identity():
    msg_text = "remove last commit"
    result_messages = _repeated_prompt_with_stale_index_messages(msg_text)
    identity = _turn_identity(msg_text, current_turn_user_idx=0, token="turn-current")
    session = _DummySession(
        context_messages=[{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"}],
        messages=[
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "ok"},
            _interrupted_user(msg_text),
            _cancelled_marker(),
        ],
    )

    assert _settle(
        session=session,
        msg_text=msg_text,
        result_messages=result_messages,
        result_identity=identity,
    ) is True

    settled = _sanitize_messages_for_api(session.context_messages)
    assert [m.get("role") for m in settled] == ["user", "assistant", "user", "assistant", "tool"]
    assert not any(m.get("content") == "old ok" for m in settled)
    assert not any(m.get("content") == "tail" for m in settled)


def test_ephemeral_settlement_is_noop():
    session = _DummySession(context_messages=[{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"}])
    msg_text = "remove last commit"
    assert _settle(
        session=session,
        msg_text=msg_text,
        result_messages=_tool_result_messages(msg_text),
        result_identity=_turn_identity(msg_text, current_turn_user_idx=0),
        ephemeral=True,
    ) is True
    assert session.context_messages == [{"role": "user", "content": "seed"}, {"role": "assistant", "content": "ok"}]
