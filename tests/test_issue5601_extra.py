from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session


class CancelDuringBackoffAgent:
    runs = 0

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")

    def run_conversation(self, **kwargs):
        type(self).runs += 1
        if type(self).runs == 1:
            # Simulate silent empty result on first attempt
            return {"messages": []}
        return {"status": "ok", "messages": [{"role": "assistant", "content": "ok"}]}

    def interrupt(self, _):
        pass


def _prepare_session(session_id: str, stream_id: str, *, pending_user_message: str):
    session = Session(session_id=session_id, title="Test Session")
    session.messages = []
    session.context_messages = []
    session.pending_user_message = pending_user_message
    session.pending_attachments = []
    session.pending_started_at = 1234567890.0
    session.pending_user_source = "cli"
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _run_stream_in_thread(session, stream_id, agent_cls, workspace):
    # run in thread because cancel during backoff is observed via wait
    t = threading.Thread(target=streaming._run_agent_streaming, kwargs=dict(
        session_id=session.session_id,
        msg_text=session.pending_user_message,
        model="test-model",
        workspace=workspace,
        stream_id=stream_id,
    ))
    t.start()
    return t


def test_cancel_during_backoff(monkeypatch, tmp_path):
    session = _prepare_session("cancel_backoff", "stream_cancel_backoff", pending_user_message="hi")
    # Create an Event and ensure the streaming worker will see it
    ev = threading.Event()
    config.CANCEL_FLAGS["stream_cancel_backoff"] = ev

    # Simulate cancellation during backoff by making wait() return True
    def _always_true(timeout=None):
        return True
    ev.wait = _always_true

    # Monkeypatch dependencies and run synchronously like other tests
    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=CancelDuringBackoffAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        fake_queue = streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=str(tmp_path),
            stream_id="stream_cancel_backoff",
        )

    # Agent should only have run once (no retry after cancel)
    assert CancelDuringBackoffAgent.runs == 1
    events = [(e[0], e[1]) for e in list(fake_queue.queue)]
    assert any(ev_name == "cancel" for ev_name, _ in events)


def test_none_result_is_handled(monkeypatch, tmp_path):
    # Ensure None results are coerced and don't crash the downstream code
    class NoneFirstAgent:
        runs = 0

        def __init__(self, **kwargs):
            pass

        def run_conversation(self, **kwargs):
            type(self).runs += 1
            if type(self).runs < 3:
                return None
            return {"status": "ok", "messages": [{"role": "assistant", "content": "ok"}]}

        def interrupt(self, _):
            pass

    session = _prepare_session("none_result", "stream_none_result", pending_user_message="hi")
    config.CANCEL_FLAGS["stream_none_result"] = threading.Event()

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=NoneFirstAgent), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        fake_queue = streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=str(tmp_path),
            stream_id="stream_none_result",
        )

    # Agent should have attempted multiple times and eventually produced messages
    assert NoneFirstAgent.runs == 3
    saved = Session.load("none_result")
    assert saved is not None
    assert any(m.get("role") == "assistant" for m in saved.messages)
