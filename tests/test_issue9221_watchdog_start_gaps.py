"""PR #9221 round 2: watchdog gaps named in review.

The turn-start watchdog from #9221 was reviewed with three findings. Each
one pins a scenario the first cut missed:

1. the watchdog was disarmed at ``update_active_run(phase="running")``,
   *before* the session agent lock wait — a worker parked on another
   turn's lock never triggered the warning the PR promised;
2. only the success path disarmed the watchdog, so a worker that exited
   before reaching running (e.g. ``get_session`` raised) left it armed
   and the deadline produced a false "worker is blocked" warning;
3. the warning was queued with ``put_nowait()`` only, so a browser that
   reconnected after the alert could not replay it from the run journal.

These tests use a sub-second ``HERMES_WEBUI_TURN_START_TIMEOUT`` instead
of the 120 s default.
"""
import queue
import sys
import threading
import time
import types

import pytest

from api import models, streaming
from api.models import Session
from api.run_journal import RunJournalWriter, read_run_events


class _SnapshotChannel:
    """Stream queue stand-in that records journal ids it is handed."""

    def __init__(self):
        self.items = []
        self.event_ids = []

    def subscribe_with_snapshot(self):  # pragma: no cover - shape probe only
        return None

    def note_last_event_id(self, event_id):
        self.event_ids.append(event_id)

    def put_nowait(self, item):
        self.items.append(item)


class _BlockingLock:
    """Stands in for the session agent lock held by another turn."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        self.entered.set()
        self.release.wait(10)
        return self

    def __exit__(self, *exc):
        return False


class _FakeAgent:
    def __init__(self, **_kwargs):
        pass

    def run_conversation(self, **_kwargs):
        return {
            "messages": [{"role": "assistant", "content": "done"}],
            "final_response": "done",
            "completed": True,
        }

    def interrupt(self, _message):
        return None


def _seed_session(tmp_path, monkeypatch, sid, stream_id):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    streaming.CANCEL_FLAGS.pop(stream_id, None)
    session = Session(
        session_id=sid,
        title="Watchdog gap",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[sid] = session
    streaming.SESSIONS[sid] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue
    return session, event_queue


def _worker_patches(monkeypatch, session, get_session=None):
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    monkeypatch.setattr(
        streaming, "get_session", get_session or (lambda _sid: session))
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _FakeAgent)
    monkeypatch.setattr(
        streaming, "resolve_model_provider",
        lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
    monkeypatch.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)


def _drain(event_queue):
    frames = []
    while True:
        try:
            frames.append(event_queue.get_nowait())
        except queue.Empty:
            return frames


def _timeout_warnings(frames):
    found = []
    for frame in frames:
        event, payload = frame[0], frame[1]
        if event == "warning" and isinstance(payload, dict):
            if payload.get("type") == "turn_start_timeout":
                found.append(payload)
    return found


def _wait_for_timeout_warning(event_queue, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        warnings = _timeout_warnings(_drain(event_queue))
        if warnings:
            return warnings[0]
        time.sleep(0.05)
    return None


def test_watchdog_covers_session_lock_wait(tmp_path, monkeypatch):
    """F1: a worker parked on the session agent lock still gets the warning."""
    sid = "lockwait-session"
    stream_id = "lockwait-stream-01"
    _session, event_queue = _seed_session(tmp_path, monkeypatch, sid, stream_id)

    lock = _BlockingLock()
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: lock)
    _worker_patches(monkeypatch, _session)
    monkeypatch.setenv("HERMES_WEBUI_TURN_START_TIMEOUT", "0.5")

    worker = threading.Thread(
        target=streaming._run_agent_streaming,
        kwargs={
            "session_id": sid,
            "msg_text": "Do the task.",
            "model": "gpt-4o",
            "workspace": str(tmp_path),
            "stream_id": stream_id,
        },
        daemon=True,
    )
    worker.start()
    try:
        assert lock.entered.wait(5), "worker never reached the session agent lock"
        warning = _wait_for_timeout_warning(event_queue, timeout=5)
    finally:
        lock.release.set()
        worker.join(10)

    assert warning is not None, (
        "watchdog was disarmed before the session agent lock wait; a worker "
        "parked on another turn's lock stayed silent")
    assert warning["timeout_seconds"] == pytest.approx(0.5)
    assert warning["stream_id"] == stream_id


def test_exited_worker_leaves_no_false_warning(tmp_path, monkeypatch):
    """F2: a worker that dies before running must disarm the watchdog."""
    sid = "deadworker-session"
    stream_id = "deadworker-stream-1"
    _session, event_queue = _seed_session(tmp_path, monkeypatch, sid, stream_id)

    def _boom(_sid):
        raise RuntimeError("session does not exist")

    _worker_patches(monkeypatch, _session, get_session=_boom)
    monkeypatch.setenv("HERMES_WEBUI_TURN_START_TIMEOUT", "0.5")

    streaming._run_agent_streaming(
        session_id=sid,
        msg_text="Do the task.",
        model="gpt-4o",
        workspace=str(tmp_path),
        stream_id=stream_id,
    )

    # Outlive the deadline: an armed watchdog would fire here.
    time.sleep(1.2)

    warnings = _timeout_warnings(_drain(event_queue))
    assert warnings == [], (
        "worker exited before running but the watchdog still fired a false "
        "'worker is blocked' warning")


def test_watchdog_warning_is_journal_backed_and_replayable(monkeypatch, tmp_path):
    """F3: the warning lands in the run journal and survives a reconnect."""
    sid = "journal-session"
    stream_id = "journal-stream-01"
    writer = RunJournalWriter(sid, stream_id, session_dir=tmp_path)
    writer.append_sse_event("worker_started", {"session_id": sid})

    channel = _SnapshotChannel()
    monkeypatch.setenv("HERMES_WEBUI_TURN_START_TIMEOUT", "0.2")
    done = streaming._start_turn_start_watchdog(
        stream_id=stream_id, session_id=sid, channel=channel,
        journal_writer=writer)
    assert done is not None

    time.sleep(0.7)

    assert channel.event_ids, "warning carried no journal event id"
    event_id = channel.event_ids[-1]
    assert streaming.STREAM_LAST_EVENT_ID.get(stream_id) == event_id

    # Reconnect: replay the journal from the event id the warning carried.
    journal = read_run_events(sid, stream_id, session_dir=tmp_path)
    warnings = [
        event for event in journal["events"]
        if event.get("event") == "warning"
        and (event.get("payload") or {}).get("type") == "turn_start_timeout"
    ]
    assert warnings, "warning was not journaled for replay"
    assert warnings[-1]["event_id"] == event_id
    seq = int(warnings[-1]["seq"])
    replayed = read_run_events(
        sid, stream_id, session_dir=tmp_path, after_seq=seq - 1)
    assert any(
        (event.get("payload") or {}).get("type") == "turn_start_timeout"
        for event in replayed["events"]
    ), "replaying from the warning's cursor did not recover the warning"
    done.set()


def test_worker_lock_wait_warning_replayed_from_run_journal(tmp_path, monkeypatch):
    """F3 end-to-end: the worker path journals its lock-wait warning."""
    sid = "replay-session"
    stream_id = "replay-stream-001"
    _session, event_queue = _seed_session(tmp_path, monkeypatch, sid, stream_id)

    lock = _BlockingLock()
    monkeypatch.setattr(streaming, "_get_session_agent_lock", lambda _sid: lock)
    _worker_patches(monkeypatch, _session)
    monkeypatch.setenv("HERMES_WEBUI_TURN_START_TIMEOUT", "0.5")

    worker = threading.Thread(
        target=streaming._run_agent_streaming,
        kwargs={
            "session_id": sid,
            "msg_text": "Do the task.",
            "model": "gpt-4o",
            "workspace": str(tmp_path),
            "stream_id": stream_id,
        },
        daemon=True,
    )
    worker.start()
    try:
        assert lock.entered.wait(5), "worker never reached the session agent lock"
        warning = _wait_for_timeout_warning(event_queue, timeout=5)
    finally:
        lock.release.set()
        worker.join(10)

    assert warning is not None, "no timeout warning was produced"
    journal = read_run_events(sid, stream_id)
    warnings = [
        event for event in journal["events"]
        if event.get("event") == "warning"
        and (event.get("payload") or {}).get("type") == "turn_start_timeout"
    ]
    assert warnings, (
        "the worker path did not journal the warning; a reconnect could not "
        "replay it")
