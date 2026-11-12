"""Regression: GatewayWatcher must not burn CPU when nobody is listening.

Maint #3035 / production evidence: with zero inbound traffic the daemon still
re-fingerprinted every sessions row every 5s and woke 10×/s on ``time.sleep(0.1)``
stop-checks, holding ~7% of a core forever.

The poll loop must:
  1. park (no fingerprint / no projection) while subscriber count is zero, and
  2. wait on ``threading.Event.wait`` for the poll interval — not a spin of
     short sleeps — so a quiet process costs essentially nothing.
"""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path


def _make_db(tmp_path: Path):
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
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('tg1', 'user', 'x', ?)",
        (time.time(),),
    )
    conn.commit()
    return db, conn


def test_poll_loop_uses_blocking_event_wait_not_spin_sleep():
    """Source contract: the idle sleep must be one Event.wait, not 50× sleep(0.1)."""
    src = (
        Path(__file__).resolve().parents[1] / "api" / "gateway_watcher.py"
    ).read_text(encoding="utf-8")
    start = src.index("def _poll_loop(self)")
    end = src.index("\n# ── Module-level watcher registry", start)
    body = src[start:end]
    # Strip docstrings so a historical mention of the old spin cannot false-fail.
    code = body
    while '"""' in code:
        a = code.index('"""')
        b = code.index('"""', a + 3) + 3
        code = code[:a] + code[b:]
    assert "time.sleep(0.1)" not in code, (
        "0.1s stop-check spin burns syscalls forever; use Event.wait(POLL_INTERVAL)"
    )
    assert "self._stop_event.wait(" in code, (
        "poll interval must block on _stop_event.wait so stop() wakes immediately"
    )
    assert "_idle_wake" in code and "_has_subscribers" in code, (
        "zero-subscriber path must park on _idle_wake instead of fingerprinting"
    )


def test_poll_loop_parks_without_subscribers(tmp_path, monkeypatch):
    """Zero subscribers → zero fingerprint work; subscribe wakes and polls resume."""
    gw = importlib.import_module("api.gateway_watcher")
    db, _conn = _make_db(tmp_path)

    polls: list[float] = []
    real_poll_once = gw.GatewayWatcher._poll_once

    def tracing_poll_once(self, *args, **kwargs):
        polls.append(time.monotonic())
        return real_poll_once(self, *args, **kwargs)

    monkeypatch.setattr(gw.GatewayWatcher, "_poll_once", tracing_poll_once)

    watcher = gw.GatewayWatcher(state_db_path=db)
    # Keep the active interval short so the subscribed phase is fast to assert.
    watcher.POLL_INTERVAL = 0.05
    watcher.start()
    try:
        time.sleep(0.25)
        assert polls == [], (
            "with no SSE subscribers the watcher must not fingerprint/poll state.db; "
            f"got {len(polls)} poll(s)"
        )

        q = watcher.subscribe()
        deadline = time.monotonic() + 2.0
        while not polls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert polls, "first subscribe must wake the parked poll loop promptly"

        # Let at least one more subscribed cycle land, then detach.
        before_unsub = len(polls)
        deadline = time.monotonic() + 2.0
        while len(polls) <= before_unsub and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(polls) > before_unsub, "subscribed watcher must keep polling"

        watcher.unsubscribe(q)
        # Drain any in-flight poll that started before unsubscribe, then prove
        # the loop re-parks (no further growth).
        time.sleep(watcher.POLL_INTERVAL + 0.05)
        settled = len(polls)
        time.sleep(0.3)
        assert len(polls) == settled, (
            "after the last subscriber leaves, polling must park again; "
            f"grew from {settled} to {len(polls)}"
        )
    finally:
        watcher.stop()
        assert not watcher.is_alive() or (
            watcher._thread is None or not watcher._thread.is_alive()
        )


def test_stop_unparks_idle_watcher_promptly(tmp_path):
    """stop() must release a watcher parked with no subscribers within join timeout."""
    gw = importlib.import_module("api.gateway_watcher")
    db, _conn = _make_db(tmp_path)
    watcher = gw.GatewayWatcher(state_db_path=db)
    watcher.POLL_INTERVAL = 30  # would hang badly if stop relied on the interval alone
    watcher.start()
    try:
        # Confirm we are parked (thread alive, no work).
        assert watcher.is_alive()
        time.sleep(0.05)
        t0 = time.monotonic()
        watcher.stop()
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"stop() blocked {elapsed:.2f}s on an idle parked watcher"
        assert not watcher.is_alive()
    finally:
        watcher.stop()
