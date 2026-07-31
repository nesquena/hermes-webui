from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
# Reuse the existing helper from the main test file for consistent mocking
from tests.test_issue5601_silent_provider_retry import _run_stream


# Reuse test agents from the main test file so mocking/patching behaves identically
from tests.test_issue5601_silent_provider_retry import SilentRetryAgent, NoneRetryAgent


class CancelDuringBackoffAgent(SilentRetryAgent):
    runs = 0


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
    CancelDuringBackoffAgent.runs = 0

    with mock.patch("api.streaming.time.sleep"), \
         mock.patch("api.streaming.threading.Event.wait", return_value=True), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        fake_queue = _run_stream(monkeypatch, session, "stream_cancel_backoff", CancelDuringBackoffAgent, workspace=str(tmp_path))

    assert CancelDuringBackoffAgent.runs == 1
    events = [(ev_name, payload) for ev_name, payload in list(fake_queue.queue)]
    assert any(ev_name == "cancel" for ev_name, _ in events)


def test_none_result_is_handled(monkeypatch, tmp_path):
    session = _prepare_session("none_result", "stream_none_result", pending_user_message="hi")
    config.CANCEL_FLAGS["stream_none_result"] = threading.Event()

    with mock.patch("api.streaming.time.sleep"), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        fake_queue = _run_stream(monkeypatch, session, "stream_none_result", NoneRetryAgent, workspace=str(tmp_path))

    assert NoneRetryAgent.runs == 3
    saved = Session.load("none_result")
    assert saved is not None
    assert any(m.get("role") == "assistant" for m in saved.messages)
