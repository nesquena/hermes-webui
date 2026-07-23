"""Regression tests — the idle-terminal reaper closes abandoned shells (#4633).

The fd-leak fix retires a terminal whose shell EXITS, and the cap bounds the
worst case, but an interactive shell abandoned by its client (tab closed without
POSTing /api/terminal/close, browser crash, network drop) keeps running because
there is deliberately no PDEATHSIG. The reaper closes a terminal that has had
zero attached output streams for longer than the idle grace, plus any dead-proc
terminal as a belt-and-suspenders. A tab refresh / brief drop re-attaches within
the grace and keeps the session.
"""
import os

import pytest

if os.name != "posix":
    pytest.skip("terminal tests require POSIX terminal support", allow_module_level=True)

import api.terminal as terminal


class _FakeProc:
    def __init__(self, pid=515151, alive=True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        return 0


def _make_registered_term(monkeypatch, sid, *, alive=True, unwatched_since=None):
    r, w = os.pipe()
    os.close(w)
    term = terminal.TerminalSession(
        session_id=sid, workspace="/tmp", proc=_FakeProc(alive=alive), master_fd=r
    )
    term.unwatched_since = unwatched_since
    with terminal._LOCK:
        terminal._TERMINALS[sid] = term
    return term


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(terminal.os, "killpg", lambda *a, **k: None)
    yield
    with terminal._LOCK:
        sids = list(terminal._TERMINALS)
    for sid in sids:
        try:
            terminal.close_terminal(sid)
        except Exception:
            pass


# ── unwatched_since tracking via subscribe/unsubscribe ───────────────────────

def test_unwatched_since_tracks_viewer_attachment():
    term = _make_registered_term_local()
    # Born unwatched.
    assert term.unwatched_since is not None

    a = term.subscribe()
    assert term.unwatched_since is None  # a viewer attached

    b = term.subscribe()
    term.unsubscribe(a)
    assert term.unwatched_since is None  # b still attached

    term.unsubscribe(b)
    assert term.unwatched_since is not None  # last viewer left


def _make_registered_term_local():
    class _P:
        pid = 1

        def poll(self):
            return None

    return terminal.TerminalSession(session_id="x", workspace="/tmp", proc=_P(), master_fd=-1)


# ── _terminals_to_reap selection ─────────────────────────────────────────────

def test_reaps_unwatched_beyond_grace(monkeypatch):
    now = 10_000.0
    grace = terminal._TERMINAL_IDLE_GRACE_SECONDS
    _make_registered_term(monkeypatch, "old", alive=True, unwatched_since=now - grace - 1)
    victims = {sid for sid, _ in terminal._terminals_to_reap(now)}
    assert "old" in victims


def test_keeps_watched_terminal(monkeypatch):
    now = 10_000.0
    # unwatched_since None => a viewer is attached => never reaped, however old.
    _make_registered_term(monkeypatch, "watched", alive=True, unwatched_since=None)
    victims = {sid for sid, _ in terminal._terminals_to_reap(now)}
    assert "watched" not in victims


def test_keeps_recently_unwatched_within_grace(monkeypatch):
    now = 10_000.0
    # Detached 5s ago — within grace, so a reconnecting tab is not killed.
    _make_registered_term(monkeypatch, "reconnecting", alive=True, unwatched_since=now - 5)
    victims = {sid for sid, _ in terminal._terminals_to_reap(now)}
    assert "reconnecting" not in victims


def test_reaps_dead_process_regardless(monkeypatch):
    now = 10_000.0
    # Dead proc but "watched" — still swept (belt-and-suspenders).
    _make_registered_term(monkeypatch, "dead", alive=False, unwatched_since=None)
    victims = {sid for sid, _ in terminal._terminals_to_reap(now)}
    assert "dead" in victims


# ── _reap_idle_terminals effect ──────────────────────────────────────────────

def test_reap_closes_and_removes(monkeypatch):
    now = 10_000.0
    grace = terminal._TERMINAL_IDLE_GRACE_SECONDS
    term = _make_registered_term(monkeypatch, "reapme", alive=True, unwatched_since=now - grace - 1)
    fd = term.master_fd

    n = terminal._reap_idle_terminals(now)

    assert n == 1
    with terminal._LOCK:
        assert "reapme" not in terminal._TERMINALS
    with pytest.raises(OSError):
        os.fstat(fd)  # fd closed


def test_reaper_thread_starts_idempotently(monkeypatch):
    # Reset reaper state so the ensure actually starts a (dummy) thread.
    monkeypatch.setattr(terminal, "_terminal_reaper_started", False)
    monkeypatch.setattr(terminal, "_terminal_reaper_thread", None)
    started = []

    class _DummyThread:
        def __init__(self, *a, **k):
            self._k = k

        def start(self):
            started.append(1)

        def is_alive(self):
            return True

    monkeypatch.setattr(terminal.threading, "Thread", _DummyThread)
    terminal._ensure_terminal_reaper()
    terminal._ensure_terminal_reaper()  # second call is a no-op (already alive)
    assert started == [1]
