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

    assert len(events) == 1
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
