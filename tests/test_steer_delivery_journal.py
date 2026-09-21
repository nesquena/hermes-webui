"""Durability coverage for accepted mid-run steer deliveries."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import run_journal


@pytest.fixture
def isolated_steer_state():
    from api.config import (
        AGENT_INSTANCES,
        ACTIVE_RUNS,
        ACTIVE_RUNS_LOCK,
        SESSION_AGENT_CACHE,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS,
        STREAMS_LOCK,
        STREAM_LIVE_SESSION_LINEAGE,
        STREAM_SESSION_OWNERS,
        STREAM_SESSION_OWNERS_LOCK,
    )

    with SESSION_AGENT_CACHE_LOCK:
        cache_snapshot = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snapshot = dict(STREAMS)
        agent_instances_snapshot = dict(AGENT_INSTANCES)
        lineage_snapshot = {
            stream_id: set(lineage)
            for stream_id, lineage in STREAM_LIVE_SESSION_LINEAGE.items()
        }
        STREAMS.clear()
        AGENT_INSTANCES.clear()
        STREAM_LIVE_SESSION_LINEAGE.clear()
    with ACTIVE_RUNS_LOCK:
        active_runs_snapshot = {
            stream_id: dict(entry)
            for stream_id, entry in ACTIVE_RUNS.items()
        }
        ACTIVE_RUNS.clear()
    with STREAM_SESSION_OWNERS_LOCK:
        owner_snapshot = dict(STREAM_SESSION_OWNERS)
        STREAM_SESSION_OWNERS.clear()
    try:
        yield SESSION_AGENT_CACHE, STREAMS
    finally:
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.clear()
            SESSION_AGENT_CACHE.update(cache_snapshot)
        with STREAMS_LOCK:
            STREAMS.clear()
            STREAMS.update(streams_snapshot)
            AGENT_INSTANCES.clear()
            AGENT_INSTANCES.update(agent_instances_snapshot)
            STREAM_LIVE_SESSION_LINEAGE.clear()
            STREAM_LIVE_SESSION_LINEAGE.update(lineage_snapshot)
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS.clear()
            ACTIVE_RUNS.update(active_runs_snapshot)
        with STREAM_SESSION_OWNERS_LOCK:
            STREAM_SESSION_OWNERS.clear()
            STREAM_SESSION_OWNERS.update(owner_snapshot)


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
    from api.config import (
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

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
    register_stream_owner(stream_id, sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

    session = MagicMock(active_stream_id=stream_id)
    journal_event = {
        "event_id": f"{stream_id}:7",
        "seq": 7,
        "run_id": stream_id,
        "session_id": sid,
        "created_at": 123.0,
    }
    captured = {}

    def accept_and_append(_writer, owner_lock, event_name, payload, accept, publish):
        captured["event_name"] = event_name
        captured["payload"] = payload
        owner_lock.release()
        assert accept() is True
        publish(journal_event)
        return True, journal_event, None, None

    with patch.object(streaming, "get_session", return_value=session), patch.object(
        streaming.RunJournalWriter,
        "accept_append_and_publish_with_owner_lock",
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
    from api.config import (
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

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
    register_stream_owner(stream_id, sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

    def reject(_writer, owner_lock, _event_name, _payload, accept, _publish):
        owner_lock.release()
        assert accept() is False
        return False, None, "rejected", None

    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ), patch.object(
        streaming.RunJournalWriter,
        "accept_append_and_publish_with_owner_lock",
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
    from api.config import (
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

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
    register_stream_owner(stream_id, sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

    persistence_error = OSError("disk unavailable")

    def fail_after_accept(_writer, owner_lock, _event_name, _payload, accept, _publish):
        owner_lock.release()
        assert accept() is True
        return True, None, "persistence_error", persistence_error

    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ), patch.object(
        streaming.RunJournalWriter,
        "accept_append_and_publish_with_owner_lock",
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
        "fallback": "persistence_error",
        "stream_id": stream_id,
        "durable": False,
    }


def test_frontend_warns_when_accepted_steer_is_not_durable():
    root = Path(__file__).resolve().parents[1]
    commands = (root / "static" / "commands.js").read_text(encoding="utf-8")
    i18n = (root / "static" / "i18n.js").read_text(encoding="utf-8")
    start = commands.index("async function _trySteer(")
    end = commands.index("\nasync function cmdTitle", start)
    body = commands[start:end]

    assert "result.durable===false||result.fallback==='persistence_error'" in body
    assert "showToast(t('steer_delivery_not_durable'),5000,'warning')" in body
    assert "else showToast(t('cmd_steer_delivered'),2500)" in body
    assert i18n.count("steer_delivery_not_durable:") >= 15


def test_non_english_persistence_warning_is_translated():
    import re

    source = (Path(__file__).resolve().parents[1] / "static" / "i18n.js").read_text(encoding="utf-8")
    openers = list(re.finditer(r"^  (?P<q>'?)(?P<name>[A-Za-z-]+)(?P=q): \{$", source, re.MULTILINE))
    values = {}
    for index, opener in enumerate(openers):
        end = openers[index + 1].start() if index + 1 < len(openers) else len(source)
        block = source[opener.start():end]
        match = re.search(
            r"^\s+steer_delivery_not_durable:\s*(['\"])(.*?)\1,\s*$",
            block,
            re.MULTILINE,
        )
        assert match is not None, f"missing steer persistence warning in {opener.group('name')}"
        values[opener.group("name")] = match.group(2)
    untranslated = [
        locale for locale, value in values.items()
        if locale != "en" and value == values["en"]
    ]
    assert untranslated == []


def test_terminal_journal_wins_race_without_late_delivery_event(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    from api import streaming
    from api.config import (
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

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
    register_stream_owner(stream_id, sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

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


def test_failed_terminal_append_still_closes_late_steer_acceptance(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    from api import streaming
    from api.config import (
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

    cache, streams = isolated_steer_state
    sid = "steer_failed_terminal_sid"
    stream_id = "steer_failed_terminal_run"
    agent = MagicMock()
    agent.steer.return_value = True
    stream = create_stream_channel()
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
    register_stream_owner(stream_id, sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

    writer = run_journal.RunJournalWriter(sid, stream_id, session_dir=tmp_path)
    real_open = run_journal.os.open

    def fail_terminal_open(path, flags, *args, **kwargs):
        if str(path) == str(writer._path) and flags & run_journal.os.O_WRONLY:
            raise OSError("fault-injected terminal append failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(run_journal.os, "open", fail_terminal_open)
    with pytest.raises(OSError, match="terminal append failure"):
        writer.append_terminal_sse_event("done", {"session": {}})
    assert run_journal.read_run_events(
        sid, stream_id, session_dir=tmp_path
    )["events"] == []

    monkeypatch.setattr(
        streaming,
        "RunJournalWriter",
        lambda _sid, _run: writer,
    )
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(
            handler,
            {"session_id": sid, "text": "too late after failed terminal"},
        )

    assert _response(handler) == {
        "accepted": False,
        "fallback": "stream_dead",
        "stream_id": stream_id,
    }
    agent.steer.assert_not_called()
    subscriber, snapshot = stream.subscribe_with_snapshot()
    assert subscriber.empty()
    assert snapshot["offline_buffered_events"] == 0


def test_rotation_lineage_lookup_does_not_hold_stream_registry_lock(
    isolated_steer_state,
    monkeypatch,
):
    from api import streaming
    from api.config import (
        AGENT_INSTANCES,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        STREAM_LIVE_SESSION_LINEAGE,
        create_stream_channel,
        register_stream_owner,
    )

    cache, streams = isolated_steer_state
    origin_sid = "steer_rotation_lock_origin"
    continuation_sid = "steer_rotation_lock_continuation"
    stream_id = "steer_rotation_lock_run"
    agent = MagicMock(session_id=continuation_sid)
    agent.steer.return_value = True
    stream = create_stream_channel()
    with SESSION_AGENT_CACHE_LOCK:
        cache[continuation_sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
        AGENT_INSTANCES[stream_id] = agent
        STREAM_LIVE_SESSION_LINEAGE[stream_id] = {origin_sid, continuation_sid}
    register_stream_owner(stream_id, origin_sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=continuation_sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

    lock_states = []

    def match_lineage(candidate, requested_sid, *, stream_id=None):
        acquired = STREAMS_LOCK.acquire(blocking=False)
        lock_states.append(acquired)
        if acquired:
            STREAMS_LOCK.release()
        return (
            candidate is agent
            and requested_sid == continuation_sid
            and stream_id == "steer_rotation_lock_run"
        )

    monkeypatch.setattr(
        streaming,
        "_agent_matches_live_rotation_session",
        match_lineage,
    )
    monkeypatch.setattr(
        streaming,
        "_accept_and_publish_steer_event",
        lambda candidate, sid, run, text: (candidate is agent, None, True),
    )
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(
            handler,
            {"session_id": continuation_sid, "text": "after rotation"},
        )

    assert lock_states == [True]
    assert _response(handler) == {
        "accepted": True,
        "fallback": None,
        "stream_id": stream_id,
    }


def test_retired_origin_owner_does_not_match_an_unrelated_reused_agent(
    isolated_steer_state,
    monkeypatch,
):
    from api import streaming
    from api.config import (
        AGENT_INSTANCES,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

    _cache, streams = isolated_steer_state
    origin_sid = "steer_retired_origin"
    unrelated_sid = "steer_unrelated_live_session"
    stream_id = "steer_reused_run"
    agent = MagicMock(session_id=unrelated_sid)
    agent.steer.return_value = True
    with STREAMS_LOCK:
        streams[stream_id] = create_stream_channel()
        AGENT_INSTANCES[stream_id] = agent
    register_stream_owner(stream_id, origin_sid)

    monkeypatch.setattr(
        streaming,
        "_accept_and_publish_steer_event",
        lambda *_args, **_kwargs: pytest.fail("stale owner revived an unrelated run"),
    )
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=None),
    ):
        handler = _handler()
        streaming._handle_chat_steer(
            handler,
            {"session_id": origin_sid, "text": "must fail closed"},
        )

    agent.steer.assert_not_called()
    assert _response(handler) == {
        "accepted": False,
        "fallback": "no_cached_agent",
        "stream_id": None,
    }


def test_identityless_agent_does_not_match_arbitrary_global_live_session(
    isolated_steer_state,
    monkeypatch,
):
    from api import streaming
    from api.config import AGENT_INSTANCES, STREAMS_LOCK, create_stream_channel

    _cache, streams = isolated_steer_state
    requested_sid = "request-controlled-session"
    stream_id = "identityless-live-run"
    agent = MagicMock(spec=["steer"])
    agent.steer.return_value = True
    with STREAMS_LOCK:
        streams[stream_id] = create_stream_channel()
        AGENT_INSTANCES[stream_id] = agent

    monkeypatch.setattr(
        streaming,
        "_accept_and_publish_steer_event",
        lambda *_args, **_kwargs: pytest.fail("identityless live agent accepted arbitrary SID"),
    )
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=None),
    ):
        handler = _handler()
        streaming._handle_chat_steer(
            handler,
            {"session_id": requested_sid, "text": "must fail closed"},
        )

    agent.steer.assert_not_called()
    assert _response(handler) == {
        "accepted": False,
        "fallback": "no_cached_agent",
        "stream_id": None,
    }


def test_compressed_continuation_uses_immutable_stream_journal_owner(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    from api import streaming
    from api.config import (
        AGENT_INSTANCES,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        STREAM_LIVE_SESSION_LINEAGE,
        create_stream_channel,
        register_stream_owner,
    )

    cache, streams = isolated_steer_state
    old_sid = "steer_compression_origin"
    new_sid = "steer_compression_continuation"
    stream_id = "steer_compression_run"
    agent = MagicMock()
    agent.session_id = new_sid
    agent.steer.return_value = True
    stream = create_stream_channel()

    with SESSION_AGENT_CACHE_LOCK:
        cache[new_sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
        AGENT_INSTANCES[stream_id] = agent
        STREAM_LIVE_SESSION_LINEAGE[stream_id] = {old_sid, new_sid}
    register_stream_owner(stream_id, old_sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=new_sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )
    run_journal.append_run_event(
        old_sid,
        stream_id,
        "token",
        {"text": "before compression"},
        session_dir=tmp_path,
    )

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
        streaming._handle_chat_steer(
            handler,
            {"session_id": new_sid, "text": "continue after compression"},
        )

    terminal = run_journal.RunJournalWriter(
        old_sid, stream_id, session_dir=tmp_path
    ).append_sse_event("done", {"session": {}})
    assert agent.steer.call_count == 1
    assert _response(handler) == {
        "accepted": True,
        "fallback": None,
        "stream_id": stream_id,
    }
    old_journal = run_journal.read_run_events(
        old_sid, stream_id, session_dir=tmp_path
    )
    assert [item["event"] for item in old_journal["events"]] == [
        "token",
        "steer_delivered",
        "done",
    ]
    assert [item["seq"] for item in old_journal["events"]] == [1, 2, 3]
    assert terminal["event_id"] == f"{stream_id}:3"
    assert (tmp_path / "_run_journal" / old_sid / f"{stream_id}.jsonl").exists()
    assert not (tmp_path / "_run_journal" / new_sid / f"{stream_id}.jsonl").exists()


def test_settled_continuation_replays_last_run_steer_control_row(
    tmp_path,
    monkeypatch,
):
    from api import models, routes
    from api.run_journal import RunJournalWriter

    owner_sid = "settled_rotation_owner"
    continuation_sid = "settled_rotation_child"
    stream_id = "settled_rotation_run"
    marker = "continue with the revised constraints"
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    writer = RunJournalWriter(owner_sid, stream_id, session_dir=session_dir)
    steered = writer.append_sse_event("steer_delivered", {"text": marker, "status": "delivered"})
    writer.append_sse_event("done", {"session": {}})

    continuation = models.Session(
        session_id=continuation_sid,
        parent_session_id=owner_sid,
        last_run_stream_id=stream_id,
    )
    continuation.save()
    loaded = models.Session.load(continuation_sid)

    assert loaded.last_run_stream_id == stream_id
    snapshot = routes._run_journal_live_snapshot(loaded.last_run_stream_id)
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    steer_rows = [row for row in rows if row.get("source_event_type") == "steer_delivered"]
    assert len(steer_rows) == 1
    assert steer_rows[0]["text"] == marker
    assert steer_rows[0]["event_id"] == steered["event_id"]


@pytest.mark.parametrize("request_sid", ["steer_rotation_origin", "steer_rotation_continuation"])
@pytest.mark.parametrize("cache_key", ["steer_rotation_origin", None])
def test_steer_resolves_live_agent_during_compression_rotation_window(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
    request_sid,
    cache_key,
):
    """Both browser-visible lineage IDs must reach the rotating live Agent.

    The Agent commits its continuation ID inside ``run_conversation()`` while
    the WebUI worker is still awaiting that call.  Until it returns, the cache
    remains keyed by the origin ID and the continuation sidecar has no
    ``active_stream_id``.  The live stream/Agent registries are the only
    authoritative runtime binding in this window.
    """
    from api import streaming
    from api.config import (
        AGENT_INSTANCES,
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        STREAM_LIVE_SESSION_LINEAGE,
        create_stream_channel,
        register_stream_owner,
    )

    cache, streams = isolated_steer_state
    old_sid = "steer_rotation_origin"
    new_sid = "steer_rotation_continuation"
    stream_id = "steer_rotation_run"
    agent = MagicMock()
    agent.session_id = new_sid
    agent._parent_session_id = "fork-origin"
    agent._live_rotation_parent_session_id = old_sid
    agent._session_db = MagicMock()
    agent._session_db.get_session.return_value = {
        "id": new_sid,
        "parent_session_id": old_sid,
    }
    agent.steer.return_value = True
    stream = create_stream_channel()

    # This is the real mid-rotation shape: cache/owner still point at the
    # origin, while the live Agent has already adopted the continuation ID.
    if cache_key:
        with SESSION_AGENT_CACHE_LOCK:
            cache[cache_key] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
        AGENT_INSTANCES[stream_id] = agent
        STREAM_LIVE_SESSION_LINEAGE[stream_id] = {old_sid, new_sid}
    register_stream_owner(stream_id, old_sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=new_sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )
    run_journal.append_run_event(
        old_sid,
        stream_id,
        "compressing",
        {"old_session_id": old_sid},
        session_dir=tmp_path,
    )

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
        return_value=MagicMock(active_stream_id=None),
    ):
        handler = _handler()
        streaming._handle_chat_steer(
            handler,
            {"session_id": request_sid, "text": f"steer via {request_sid}"},
        )

    assert _response(handler) == {
        "accepted": True,
        "fallback": None,
        "stream_id": stream_id,
    }
    agent.steer.assert_called_once_with(f"steer via {request_sid}")
    journal = run_journal.read_run_events(
        old_sid,
        stream_id,
        session_dir=tmp_path,
    )
    assert [item["event"] for item in journal["events"]] == [
        "compressing",
        "steer_delivered",
    ]
    assert (tmp_path / "_run_journal" / old_sid / f"{stream_id}.jsonl").exists()
    assert not (tmp_path / "_run_journal" / new_sid / f"{stream_id}.jsonl").exists()


def test_rotation_binding_fails_closed_when_session_matches_multiple_live_runs(
    isolated_steer_state,
):
    from api import streaming
    from api.config import (
        AGENT_INSTANCES,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

    _cache, streams = isolated_steer_state
    sid = "ambiguous_rotation_origin"
    agents = []
    for stream_id in ("ambiguous_rotation_run_a", "ambiguous_rotation_run_b"):
        agent = MagicMock()
        agent.session_id = "same_continuation"
        agent.steer.return_value = True
        agents.append(agent)
        with STREAMS_LOCK:
            streams[stream_id] = create_stream_channel()
            AGENT_INSTANCES[stream_id] = agent
        register_stream_owner(stream_id, sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

    handler = _handler()
    streaming._handle_chat_steer(
        handler,
        {"session_id": sid, "text": "must not pick by registry order"},
    )

    assert _response(handler) == {
        "accepted": False,
        "fallback": "no_cached_agent",
        "stream_id": None,
    }
    for agent in agents:
        agent.steer.assert_not_called()


def test_stream_journal_owner_falls_back_for_legacy_unregistered_run(tmp_path):
    from api.config import stream_journal_owner_session_id

    assert stream_journal_owner_session_id("legacy_unregistered_run", "legacy_session") == "legacy_session"


def test_stream_journal_owner_remains_original_across_compression_aliases():
    from api.config import (
        register_stream_owner,
        stream_journal_owner_session_id,
        unregister_stream_owner,
    )

    stream_id = "steer_immutable_owner_run"
    try:
        register_stream_owner(stream_id, "compression_origin")
        register_stream_owner(stream_id, "compression_continuation")
        assert stream_journal_owner_session_id(stream_id, "compression_continuation") == "compression_origin"
    finally:
        unregister_stream_owner(stream_id)


def test_teardown_cannot_remove_owner_before_steer_acquires_journal_lock(tmp_path):
    import threading

    from api import config

    old_sid = "steer_teardown_origin"
    stream_id = "steer_teardown_run"
    config.register_stream_owner(stream_id, old_sid)
    writer = run_journal.RunJournalWriter(old_sid, stream_id, session_dir=tmp_path)
    journal_acquire_started = threading.Event()
    allow_journal_acquire = threading.Event()
    teardown_started = threading.Event()
    teardown_done = threading.Event()
    transaction_done = threading.Event()
    result = {}

    class BlockingJournalLock:
        def acquire(self):
            journal_acquire_started.set()
            assert allow_journal_acquire.wait(timeout=5)
            return True

        def release(self):
            return None

    writer._lock = BlockingJournalLock()

    def teardown():
        teardown_started.set()
        config.unregister_stream_owner(stream_id)
        teardown_done.set()

    def transact():
        result["value"] = writer.accept_append_and_publish_with_owner_lock(
            config.STREAM_SESSION_OWNERS_LOCK,
            "steer_delivered",
            {"text": "atomic owner handoff"},
            lambda: True,
        )
        transaction_done.set()

    config.STREAM_SESSION_OWNERS_LOCK.acquire()
    try:
        teardown_thread = threading.Thread(target=teardown)
        transaction_thread = threading.Thread(target=transact)
        teardown_thread.start()
        transaction_thread.start()
        assert teardown_started.wait(timeout=5)
        assert journal_acquire_started.wait(timeout=5)
        assert not teardown_done.wait(timeout=0.1), (
            "teardown removed owner while journal-lock acquisition was still blocked"
        )
        allow_journal_acquire.set()
        assert transaction_done.wait(timeout=5)
        teardown_thread.join(timeout=5)
        transaction_thread.join(timeout=5)
        assert teardown_done.is_set()
        accepted, event, reason, error = result["value"]
        assert accepted is True and event is not None and reason is None and error is None
    finally:
        allow_journal_acquire.set()
        if config.STREAM_SESSION_OWNERS_LOCK.locked():
            config.STREAM_SESSION_OWNERS_LOCK.release()
        config.unregister_stream_owner(stream_id)


def test_two_steers_and_teardown_do_not_form_stream_owner_journal_lock_cycle(
    tmp_path,
    monkeypatch,
):
    import threading

    from api import config, streaming

    sid = "steer_deadlock_origin"
    stream_id = "steer_deadlock_run"
    owner_lock = threading.Lock()

    class RecordingStreamsLock:
        def __init__(self):
            self._inner = threading.Lock()
            self.contender = threading.Event()

        def __enter__(self):
            if not self._inner.acquire(blocking=False):
                self.contender.set()
                assert self._inner.acquire(timeout=5)
            return self

        def __exit__(self, exc_type, exc, tb):
            self._inner.release()
            return False

    streams_lock = RecordingStreamsLock()
    owners = {stream_id: sid}
    published = []
    first_accept_entered = threading.Event()
    release_first_accept = threading.Event()
    teardown_done = threading.Event()
    results = []

    class Stream:
        def put_nowait(self, item):
            published.append(item)

        def note_last_event_id(self, _event_id):
            return None

    stream = Stream()
    monkeypatch.setattr(config, "STREAMS_LOCK", streams_lock)
    monkeypatch.setattr(config, "STREAMS", {stream_id: stream})
    monkeypatch.setattr(config, "AGENT_INSTANCES", {})
    monkeypatch.setattr(config, "ACTIVE_RUNS", {})
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS_LOCK", owner_lock)
    monkeypatch.setattr(config, "STREAM_SESSION_OWNERS", owners)

    class FirstAgent:
        session_id = sid

        def steer(self, _text):
            first_accept_entered.set()
            assert release_first_accept.wait(timeout=5)
            return True

    class SecondAgent:
        session_id = sid

        def steer(self, _text):
            return True

    def steer(agent, text):
        results.append(
            streaming._accept_and_publish_steer_event(
                agent,
                sid,
                stream_id,
                text,
            )
        )

    def teardown():
        with streams_lock:
            with owner_lock:
                owners.pop(stream_id, None)
                config.STREAMS.pop(stream_id, None)
        teardown_done.set()

    first = threading.Thread(target=steer, args=(FirstAgent(), "first"), daemon=True)
    second = threading.Thread(target=steer, args=(SecondAgent(), "second"), daemon=True)
    teardown_thread = threading.Thread(target=teardown, daemon=True)
    first.start()
    assert first_accept_entered.wait(timeout=5)
    second.start()
    assert streams_lock.contender.wait(timeout=5), (
        "second steer did not wait at the shared stream ownership edge"
    )
    teardown_thread.start()
    release_first_accept.set()

    first.join(timeout=5)
    second.join(timeout=5)
    teardown_thread.join(timeout=5)
    assert not first.is_alive(), "first steer deadlocked while publishing"
    assert not second.is_alive(), "second steer deadlocked at the stream edge"
    assert teardown_done.is_set(), "teardown deadlocked at the stream/owner edge"
    assert results[0] == (True, None, True)
    assert results[1] in {(True, None, True), (False, "stream_dead", False)}
    assert [item[1]["text"] for item in published] in (["first"], ["first", "second"])
    assert stream_id not in owners


def test_missing_registered_owner_after_liveness_check_rejects_steer(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    from api import streaming
    from api.config import SESSION_AGENT_CACHE_LOCK, unregister_stream_owner

    cache, _streams = isolated_steer_state
    origin_sid = "steer_teardown_origin"
    continuation_sid = "steer_teardown_continuation"
    stream_id = "steer_teardown_after_liveness"
    agent = MagicMock()
    agent.steer.return_value = True
    with SESSION_AGENT_CACHE_LOCK:
        cache[continuation_sid] = (agent, "sig")
    run_journal.append_run_event(
        origin_sid,
        stream_id,
        "done",
        {"terminal_state": "completed"},
        session_dir=tmp_path,
    )
    real_writer = run_journal.RunJournalWriter
    monkeypatch.setattr(
        streaming,
        "RunJournalWriter",
        lambda sid, run: real_writer(sid, run, session_dir=tmp_path),
    )

    def pass_liveness_then_finish(_stream_id):
        unregister_stream_owner(stream_id)
        return MagicMock()

    with patch.object(streaming, "get_session", return_value=MagicMock(active_stream_id=stream_id)), patch(
        "api.config.peek_stream",
        side_effect=pass_liveness_then_finish,
    ):
        handler = _handler()
        streaming._handle_chat_steer(
            handler,
            {"session_id": continuation_sid, "text": "too late"},
        )

    response = _response(handler)
    assert response == {
        "accepted": False,
        "fallback": "stream_dead",
        "stream_id": None,
    }
    agent.steer.assert_not_called()
    assert [
        event["event"]
        for event in run_journal.read_run_events(
            origin_sid,
            stream_id,
            session_dir=tmp_path,
        )["events"]
    ] == ["done"]
    assert (tmp_path / "_run_journal" / origin_sid / f"{stream_id}.jsonl").exists()
    assert not (tmp_path / "_run_journal" / continuation_sid / f"{stream_id}.jsonl").exists()


def test_durable_append_and_live_publication_share_transaction_lock(
    isolated_steer_state,
    tmp_path,
    monkeypatch,
):
    from api import streaming
    from api.config import (
        SESSION_AGENT_CACHE_LOCK,
        STREAMS_LOCK,
        create_stream_channel,
        register_stream_owner,
    )

    cache, streams = isolated_steer_state
    sid = "steer_transaction_sid"
    stream_id = "steer_transaction_run"
    agent = MagicMock()
    agent.steer.return_value = True
    stream = create_stream_channel()
    with SESSION_AGENT_CACHE_LOCK:
        cache[sid] = (agent, "sig")
    with STREAMS_LOCK:
        streams[stream_id] = stream
    register_stream_owner(stream_id, sid)
    from api.config import register_active_run
    register_active_run(
        stream_id,
        session_id=sid,
        backend=streaming.WEBUI_LOCAL_CHAT_BACKEND,
        phase="running",
    )

    class RecordingLock:
        def __init__(self):
            import threading

            self._inner = threading.Lock()
            self.active = False

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.release()
            return False

        def acquire(self):
            self._inner.acquire()
            self.active = True
            return True

        def release(self):
            self.active = False
            self._inner.release()

    transaction_lock = RecordingLock()
    monkeypatch.setattr(run_journal, "_lock_for", lambda _path: transaction_lock)
    real_writer = run_journal.RunJournalWriter(sid, stream_id, session_dir=tmp_path)
    monkeypatch.setattr(streaming, "RunJournalWriter", lambda *_args, **_kwargs: real_writer)
    publication_lock_states = []
    original_put = stream.put_nowait

    def record_transaction_ownership(item):
        publication_lock_states.append(transaction_lock.active)
        return original_put(item)

    monkeypatch.setattr(stream, "put_nowait", record_transaction_ownership)
    with patch.object(
        streaming,
        "get_session",
        return_value=MagicMock(active_stream_id=stream_id),
    ):
        handler = _handler()
        streaming._handle_chat_steer(handler, {"session_id": sid, "text": "atomic"})

    assert _response(handler)["accepted"] is True
    assert publication_lock_states == [True]