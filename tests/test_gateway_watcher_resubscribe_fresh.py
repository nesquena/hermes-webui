"""Regression: a re-subscribing SSE client gets a fresh watcher projection.

``_poll_once`` resets the parity timestamp when it runs with no subscribers
("force fresh projection on the next subscription"), but since the idle-park
change (#7694) the poll loop parks *before* calling ``_poll_once`` once the
last subscriber leaves, so that reset is not reached on a normal disconnect.
A tab that reconnects inside the parity interval then resumes with the stale
cheap fingerprint and skips the projection. Final removal invalidates the
cache, and an epoch fences projections already in flight.
"""
from __future__ import annotations

import importlib
import sqlite3
import threading
import time
from pathlib import Path

import pytest


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            session_source TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            parent_session_id TEXT,
            message_count INTEGER DEFAULT 0,
            title TEXT,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, message_count, title) "
        "VALUES ('tg1', 'telegram', 'm', ?, 1, 'Chat')",
        (time.time(),),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('tg1', 'user', 'x', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()
    return db


def test_resubscribe_after_last_unsubscribe_forces_fresh_projection(tmp_path):
    gw = importlib.import_module("api.gateway_watcher")
    watcher = gw.GatewayWatcher(state_db_path=_make_db(tmp_path))
    q = watcher.subscribe()
    assert watcher._poll_once(now=1.0) is True

    watcher.unsubscribe(q)
    assert watcher._last_cheap_fp == ""
    assert watcher._last_full_projection_at is None

    watcher.subscribe()
    # Well inside PROJECTION_PARITY_INTERVAL and with an unchanged state.db.
    assert watcher._poll_once(now=2.0) is True


def test_unsubscribe_keeps_fingerprint_while_other_subscribers_remain(tmp_path):
    gw = importlib.import_module("api.gateway_watcher")
    watcher = gw.GatewayWatcher(state_db_path=_make_db(tmp_path))
    first = watcher.subscribe()
    watcher.subscribe()
    assert watcher._poll_once(now=1.0) is True
    fingerprint = watcher._last_cheap_fp
    assert fingerprint

    watcher.unsubscribe(first)
    assert watcher._last_cheap_fp == fingerprint
    assert watcher._last_full_projection_at == 1.0
    assert watcher._poll_once(now=2.0) is False


def test_unsubscribe_of_unknown_queue_does_not_reset(tmp_path):
    import queue as _queue

    gw = importlib.import_module("api.gateway_watcher")
    watcher = gw.GatewayWatcher(state_db_path=_make_db(tmp_path))
    watcher.subscribe()
    assert watcher._poll_once(now=1.0) is True
    fingerprint = watcher._last_cheap_fp

    watcher.unsubscribe(_queue.Queue())
    assert watcher._last_cheap_fp == fingerprint
    assert watcher._last_full_projection_at == 1.0


def test_slow_consumer_eviction_invalidates_populated_cache(tmp_path):
    gw = importlib.import_module("api.gateway_watcher")
    watcher = gw.GatewayWatcher(state_db_path=_make_db(tmp_path))
    q = watcher.subscribe()
    assert watcher._poll_once(now=1.0) is True
    assert watcher._last_cheap_fp
    assert watcher._last_full_projection_at == 1.0

    for _ in range(q.maxsize - q.qsize()):
        q.put_nowait({"type": "existing"})
    watcher._notify_subscribers([])
    assert not watcher._has_subscribers()
    assert watcher._last_cheap_fp == ""
    assert watcher._last_full_projection_at is None
    watcher.unsubscribe(q)  # The late SSE cleanup cannot undo the reset.
    watcher.subscribe()
    assert watcher._poll_once(now=2.0) is True


@pytest.mark.parametrize("removal", ["unsubscribe", "slow_consumer"])
@pytest.mark.parametrize("reconnect_while_blocked", [False, True])
def test_inflight_projection_cannot_restore_cache_after_last_removal(
    tmp_path, monkeypatch, removal, reconnect_while_blocked,
):
    """The DB read must not serialize final removal or commit across its epoch."""
    gw = importlib.import_module("api.gateway_watcher")
    watcher = gw.GatewayWatcher(state_db_path=_make_db(tmp_path))
    q = watcher.subscribe()
    entered = threading.Event()
    release = threading.Event()
    projections = []
    errors = []
    real_projection = gw._get_agent_sessions_from_db

    def blocked_projection(path):
        projections.append(path)
        if len(projections) == 1:
            entered.set()
            assert release.wait(5), "test never released the DB read"
        return real_projection(path)

    def poll():
        try:
            watcher._poll_once(now=1.0)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(gw, "_get_agent_sessions_from_db", blocked_projection)
    worker = threading.Thread(target=poll, daemon=True)
    worker.start()
    new_q = None
    try:
        assert entered.wait(5), "poll never reached the DB read"
        if removal == "unsubscribe":
            watcher.unsubscribe(q)
        else:
            for _ in range(q.maxsize):
                q.put_nowait({"type": "existing"})
            watcher._notify_subscribers([])
            # Handler teardown of an evicted queue must be harmless.
            watcher.unsubscribe(q)
        assert not watcher._has_subscribers()
        assert watcher._last_cheap_fp == ""
        assert watcher._last_full_projection_at is None
        if reconnect_while_blocked:
            new_q = watcher.subscribe()
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive(), "poll did not finish"
    assert not errors, errors
    assert watcher._last_cheap_fp == ""
    assert watcher._last_full_projection_at is None
    if new_q is not None:
        assert new_q.empty(), "old projection must not notify a new subscriber"
    else:
        watcher.subscribe()
    assert watcher._poll_once(now=2.0) is True
    assert len(projections) == 2
    assert watcher._last_cheap_fp
    assert watcher._last_full_projection_at == 2.0


def test_poll_loop_reprojects_for_reconnecting_subscriber(tmp_path, monkeypatch):
    """Through the real loop: disconnect, park, reconnect -> projection runs again."""
    gw = importlib.import_module("api.gateway_watcher")
    db = _make_db(tmp_path)
    projections: list[float] = []
    real_projection = gw._get_agent_sessions_from_db

    def tracing_projection(path):
        projections.append(time.monotonic())
        return real_projection(path)

    monkeypatch.setattr(gw, "_get_agent_sessions_from_db", tracing_projection)
    watcher = gw.GatewayWatcher(state_db_path=db)
    watcher.POLL_INTERVAL = 0.05
    watcher.start()
    try:
        q = watcher.subscribe()
        deadline = time.monotonic() + 2.0
        while not projections and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(projections) == 1

        watcher.unsubscribe(q)
        time.sleep(watcher.POLL_INTERVAL * 4)  # loop parks; no further projection
        assert len(projections) == 1

        watcher.subscribe()
        deadline = time.monotonic() + 2.0
        while len(projections) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(projections) == 2, "reconnecting subscriber must get a fresh projection"
    finally:
        watcher.stop()
