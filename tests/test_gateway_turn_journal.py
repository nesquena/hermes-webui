from __future__ import annotations

import threading
from unittest.mock import MagicMock


def test_gateway_worker_records_turn_acceptance_before_external_request(monkeypatch):
    from api import gateway_chat
    from api.config import STREAMS, STREAMS_LOCK

    stream_id = "gateway-journal-stream"
    session_id = "gateway-journal-session"
    events = []
    queue = MagicMock()

    with STREAMS_LOCK:
        STREAMS[stream_id] = queue

    monkeypatch.setattr(
        gateway_chat,
        "RunJournalWriter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("run journal unavailable")),
    )
    monkeypatch.setattr(
        gateway_chat,
        "append_turn_journal_event_for_stream",
        lambda sid, stream, event: events.append((sid, stream, dict(event))) or event,
    )
    monkeypatch.setattr(
        gateway_chat,
        "get_session",
        lambda _sid: (_ for _ in ()).throw(RuntimeError("stop before external request")),
    )

    try:
        gateway_chat._run_gateway_chat_streaming(
            session_id=session_id,
            msg_text="wake up",
            model="test-model",
            workspace="/tmp/workspace",
            stream_id=stream_id,
        )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    assert [event[2]["event"] for event in events] == [
        "worker_started",
        "interrupted",
    ]
    sid, stream, event = events[0]
    assert sid == session_id
    assert stream == stream_id
    assert event["event"] == "worker_started"
    assert isinstance(event["created_at"], float)


def test_gateway_worker_rejected_acceptance_never_reaches_external_execution(monkeypatch):
    from api import gateway_chat
    from api.config import STREAMS, STREAMS_LOCK

    stream_id = "gateway-journal-rejected"
    session_id = "gateway-journal-session"
    queue = MagicMock()
    acceptance_gate = threading.Event()
    acceptance_state = {"accepted": False}
    acceptance_gate.set()

    with STREAMS_LOCK:
        STREAMS[stream_id] = queue

    monkeypatch.setattr(
        gateway_chat,
        "get_session",
        lambda _sid: (_ for _ in ()).throw(
            AssertionError("rejected gateway worker reached session or external execution")
        ),
    )

    gateway_chat._run_gateway_chat_streaming(
        session_id=session_id,
        msg_text="wake up",
        model="test-model",
        workspace="/tmp/workspace",
        stream_id=stream_id,
        acceptance_gate=acceptance_gate,
        acceptance_state=acceptance_state,
    )

    with STREAMS_LOCK:
        assert stream_id not in STREAMS


def test_gateway_worker_records_completed_after_successful_writeback(monkeypatch):
    from api import gateway_chat, streaming
    from api.config import STREAMS, STREAMS_LOCK

    stream_id = "gateway-journal-completed"
    session_id = "gateway-journal-session"
    queue = MagicMock()
    journal_events = []
    session = MagicMock()
    session.active_stream_id = stream_id
    session.workspace = "/tmp/workspace"
    session.model = "test-model"
    session.model_provider = None
    session.profile = None
    session.context_messages = []
    session.messages = []
    session.pending_user_message = None
    session.pending_attachments = None
    session.pending_started_at = None
    session.pending_user_source = None
    session.process_wakeup_pause = {}

    sse_body = (
        'data: {"choices":[{"delta":{"content":"Done"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    def fake_urlopen(_req, *, timeout=None):
        response = MagicMock()
        response.__iter__ = lambda current: iter(sse_body.split(b"\n"))
        response.__enter__ = lambda current: current
        response.__exit__ = lambda current, *_args: None
        return response

    with STREAMS_LOCK:
        STREAMS[stream_id] = queue

    monkeypatch.setattr(
        gateway_chat,
        "append_turn_journal_event_for_stream",
        lambda sid, stream, event: journal_events.append((sid, stream, dict(event))) or event,
    )
    monkeypatch.setattr(gateway_chat, "get_session", lambda _sid: session)
    monkeypatch.setattr(gateway_chat, "gateway_supports_approval", lambda *_args: False)
    monkeypatch.setattr(gateway_chat, "gateway_approval_unavailable_reason", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "_gateway_use_runs_api_enabled", lambda *_args: False)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_chat, "_stream_writeback_is_current", lambda *_args: True)
    monkeypatch.setattr(gateway_chat, "merge_session_messages_append_only", lambda *_args: [])
    monkeypatch.setattr(
        streaming,
        "_session_payload_with_full_messages",
        lambda *_args, **_kwargs: {"session_id": session_id, "messages": []},
    )

    try:
        gateway_chat._run_gateway_chat_streaming(
            session_id=session_id,
            msg_text="wake up",
            model="test-model",
            workspace="/tmp/workspace",
            stream_id=stream_id,
        )
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

    assert [event[2]["event"] for event in journal_events] == [
        "worker_started",
        "completed",
    ]
