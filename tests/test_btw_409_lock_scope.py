"""POST /api/btw must write its duplicate-stream 409 outside STREAMS_LOCK.

#9031: ``_handle_btw`` decided "session already has an active stream" and wrote
the response with ``return j(...)`` INSIDE ``with STREAMS_LOCK``. ``j`` writes
the status line, headers and body to the client socket, so a client that
stopped reading (full send buffer) would keep every stream start, Stop, steer
and SSE registration waiting on ``STREAMS_LOCK`` for the duration of that
write. AGENTS.md: "Keep HTTP response writes outside runtime registry locks."
``chat/start`` already decides under the lock and responds after releasing it.

The regression asserted here is the lock SCOPE, not the source text: ``j`` is
wrapped with a recorder that samples ``STREAMS_LOCK.locked()`` at the moment of
the write and then calls the real helper, so the assertion fails on any future
edit that moves the write back under the lock. The response contract (status
409, identical body) and the success path (no active stream keeps going toward
the ephemeral session) are pinned alongside it.
"""
from __future__ import annotations

import json
import types

import pytest

from api import routes

QUESTION = "what changed in this file?"
BUSY_ERROR = {"error": "session already has an active stream"}


class _RecordingHandler:
    """Minimal handler that captures what the real ``j`` writes."""

    def __init__(self):
        self.headers = {}
        self.wfile = self
        self.status = None
        self.sent_headers = []
        self.body = b""

    def send_response(self, status, *_args, **_kwargs):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data

    def flush(self):
        pass

    def header(self, name):
        for sent_name, sent_value in self.sent_headers:
            if sent_name.lower() == name.lower():
                return sent_value
        return None


def _disable_btw_preconditions(monkeypatch, session):
    """Neutralise every refusal that sits before the duplicate-stream guard."""
    monkeypatch.setattr(routes, "refuse_on_gateway_backend", lambda *_a, **_k: False)
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)


def _record_write_lock_states(monkeypatch):
    """Wrap the real ``j`` and sample STREAMS_LOCK at each response write."""
    original_j = routes.j
    observed = []

    def recording_j(handler, payload, status=200, **kwargs):
        observed.append(routes.STREAMS_LOCK.locked())
        return original_j(handler, payload, status=status, **kwargs)

    monkeypatch.setattr(routes, "j", recording_j)
    return observed


def _session(**overrides):
    fields = {
        "session_id": "btw-parent-session",
        "workspace": "/tmp/workspace",
        "model": "test-model",
        "model_provider": None,
        "profile": None,
        "reasoning_effort": None,
        "messages": [],
        "active_stream_id": None,
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def test_duplicate_stream_409_is_written_outside_streams_lock(monkeypatch):
    """A live stream must 409 without holding STREAMS_LOCK during the write."""
    stream_id = "btw-busy-stream"
    session = _session(active_stream_id=stream_id)
    _disable_btw_preconditions(monkeypatch, session)
    observed_lock_states = _record_write_lock_states(monkeypatch)

    with routes.STREAMS_LOCK:
        routes.STREAMS[stream_id] = object()
    handler = _RecordingHandler()
    try:
        routes._handle_btw(
            handler, {"session_id": session.session_id, "question": QUESTION}
        )
    finally:
        with routes.STREAMS_LOCK:
            routes.STREAMS.pop(stream_id, None)

    assert handler.status == 409
    assert json.loads(handler.body.decode("utf-8")) == BUSY_ERROR
    assert observed_lock_states, "the 409 must travel through the real j() write"
    assert observed_lock_states == [False] * len(observed_lock_states), (
        "STREAMS_LOCK was held while the duplicate-stream 409 was written; "
        "decide under the lock and respond after releasing it"
    )
    # Refusing must not clear the id of the stream that is still alive.
    assert session.active_stream_id == stream_id


def test_no_active_stream_still_reaches_ephemeral_session_creation(monkeypatch):
    """The success path is unchanged: no 409, flow continues to the new session."""
    session = _session()
    _disable_btw_preconditions(monkeypatch, session)
    observed_lock_states = _record_write_lock_states(monkeypatch)

    class _ReachedEphemeralCreation(Exception):
        pass

    creation_kwargs = []

    def _new_session(**kwargs):
        creation_kwargs.append(kwargs)
        raise _ReachedEphemeralCreation("reached ephemeral session creation")

    import api.models as models

    monkeypatch.setattr(models, "new_session", _new_session)

    handler = _RecordingHandler()
    with pytest.raises(_ReachedEphemeralCreation):
        routes._handle_btw(
            handler, {"session_id": session.session_id, "question": QUESTION}
        )

    assert creation_kwargs, "the guard must not stop a session without a stream"
    assert handler.status is None, "no response may be written before the session exists"
    assert observed_lock_states == []


def test_stale_stream_id_is_cleared_and_flow_continues(monkeypatch):
    """A recorded id whose stream is gone is cleared, and the request proceeds."""
    session = _session(active_stream_id="btw-stream-already-gone")
    _disable_btw_preconditions(monkeypatch, session)
    observed_lock_states = _record_write_lock_states(monkeypatch)

    class _ReachedEphemeralCreation(Exception):
        pass

    import api.models as models

    monkeypatch.setattr(
        models,
        "new_session",
        lambda **_kwargs: (_ for _ in ()).throw(_ReachedEphemeralCreation("reached")),
    )

    handler = _RecordingHandler()
    with pytest.raises(_ReachedEphemeralCreation):
        routes._handle_btw(
            handler, {"session_id": session.session_id, "question": QUESTION}
        )

    assert session.active_stream_id is None
    assert handler.status is None
    assert observed_lock_states == []
