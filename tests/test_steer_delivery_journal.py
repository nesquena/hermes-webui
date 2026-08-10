"""Durability coverage for accepted mid-run steer deliveries."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from api import run_journal


@pytest.fixture
def isolated_steer_state():
    from api.config import (
        SESSION_AGENT_CACHE,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS,
        STREAMS_LOCK,
    )

    with SESSION_AGENT_CACHE_LOCK:
        cache_snapshot = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snapshot = dict(STREAMS)
        STREAMS.clear()
    try:
        yield SESSION_AGENT_CACHE, STREAMS
    finally:
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()
            SESSION_AGENT_CACHE.update(cache_snapshot)
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(streams_snapshot)


def _handler():
    handler = MagicMock()
    handler.wfile = MagicMock()
    handler.headers = MagicMock()
    handler.headers.get = MagicMock(return_value="")
    return handler


def _response(handler):
    raw = handler.wfile.write.call_args_list[-1][0][0]
    return json.loads(raw.decode("utf-8"))


def test_accepted_steer_uses_one_journal_identity_for_live_broadcast(
    isolated_steer_state,
):
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams = isolated_steer_state
    sid = "steer_journal_sid"
    stream_id = "steer_journal_run"
    agent = MagicMock()
    agent.steer.return_value = True
    stream = create_stream_channel()
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream

    session = MagicMock(active_stream_id=stream_id)
    journal_event = {
        "event_id": f"{stream_id}:7",
        "seq": 7,
        "run_id": stream_id,
        "session_id": sid,
        "created_at": 123.0,
    }
    captured = {}

    def accept_and_append(_writer, event_name, payload, accept):
        captured["event_name"] = event_name
        captured["payload"] = payload
        assert accept() is True
        return True, journal_event, None, None

    with patch.object(streaming, "get_session", return_value=session), patch.object(
        streaming.RunJournalWriter,
        "accept_and_append_if_nonterminal",
        autospec=True,
        side_effect=accept_and_append,
    ) as transaction:
        handler = _handler()
        streaming._handle_chat_steer(
            handler,
            {"session_id": sid, "text": "keep this steer"},
        )

    agent.steer.assert_called_once_with("keep this steer")
    transaction.assert_called_once()
    assert captured["event_name"] == "steer_delivered"
    assert captured["payload"]["text"] == "keep this steer"
    assert captured["payload"]["status"] == "delivered"

    subscriber, snapshot = stream.subscribe_with_snapshot()
    event_name, payload, event_id = subscriber.get_nowait()
    assert event_name == "steer_delivered"
    assert payload["text"] == "keep this steer"
    assert payload["created_at"] == 123.0
    assert event_id == f"{stream_id}:7"
    assert snapshot["last_event_id"] == event_id
    assert snapshot["offline_buffered_events"] == 1
    assert _response(handler) == {
        "accepted": True,
        "fallback": None,
        "stream_id": stream_id,
    }


def test_rejected_steer_does_not_create_a_delivery_event(isolated_steer_state):
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams = isolated_steer_state
    sid = "steer_rejected_sid"
    stream_id = "steer_rejected_run"
    agent = MagicMock()
    agent.steer.return_value = False
    stream = create_stream_channel()
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream

    def reject(_writer, _event_name, _payload, accept):
        assert accept() is False
        return False, None, "rejected", None

    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ), patch.object(
        streaming.RunJournalWriter,
        "accept_and_append_if_nonterminal",
        autospec=True,
        side_effect=reject,
    ) as transaction:
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "no"})

    transaction.assert_called_once()
    subscriber, snapshot = stream.subscribe_with_snapshot()
    assert subscriber.empty()
    assert snapshot["offline_buffered_events"] == 0
    assert _response(handler)["accepted"] is False


def test_journal_failure_does_not_turn_runtime_acceptance_into_http_failure(
    isolated_steer_state,
):
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams = isolated_steer_state
    sid = "steer_journal_failure_sid"
    stream_id = "steer_journal_failure_run"
    agent = MagicMock()
    agent.steer.return_value = True
    stream = create_stream_channel()
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream

    persistence_error = OSError("disk unavailable")

    def fail_after_accept(_writer, _event_name, _payload, accept):
        assert accept() is True
        return True, None, "persistence_error", persistence_error

    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ), patch.object(
        streaming.RunJournalWriter,
        "accept_and_append_if_nonterminal",
        autospec=True,
        side_effect=fail_after_accept,
    ):
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "accepted"})

    subscriber, snapshot = stream.subscribe_with_snapshot()
    assert subscriber.empty()
    assert snapshot["offline_buffered_events"] == 0
    assert _response(handler) == {
        "accepted": True,
        "fallback": None,
        "stream_id": stream_id,
    }


def test_terminal_journal_wins_race_without_late_delivery_event(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, STREAMS_LOCK, create_stream_channel

    cache, streams = isolated_steer_state
    sid = "steer_terminal_race_sid"
    stream_id = "steer_terminal_race_run"
    stream = create_stream_channel()

    agent = MagicMock()
    agent.steer.return_value = True
    run_journal.append_run_event(
        sid,
        stream_id,
        "done",
        {"session": {}},
        session_dir=tmp_path,
    )

    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream

    def test_writer(session_id, run_id):
        return run_journal.RunJournalWriter(
            session_id,
            run_id,
            session_dir=tmp_path,
        )

    monkeypatch.setattr(streaming, "RunJournalWriter", test_writer)
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "too late"})

    journal = run_journal.read_run_events(sid, stream_id, session_dir=tmp_path)
    assert [event["event"] for event in journal["events"]] == ["done"]
    subscriber, snapshot = stream.subscribe_with_snapshot()
    assert subscriber.empty()
    assert snapshot["offline_buffered_events"] == 0
    agent.steer.assert_not_called()
    assert _response(handler) == {
        "accepted": False,
        "fallback": "stream_dead",
        "stream_id": stream_id,
    }