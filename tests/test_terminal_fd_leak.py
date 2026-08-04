"""Regression tests for the embedded-terminal resource leak (#4633).

Two leaks are closed here:

1. **fd + dict entry on shell exit.** When a shell exited on its own (user typed
   `exit`, the process died), `_reader_loop` broke out of its loop but only set
   `closed` and emitted `terminal_closed`; it never closed the pty `master_fd`
   nor removed the `_TERMINALS` entry. Only an explicit `close_terminal` /
   restart / atexit did that, so a self-exited shell leaked its fd and dict
   entry for the rest of the WebUI's uptime. The reader loop now retires the
   session (identity-guarded so a restart's replacement is never touched).

2. **Unbounded accumulation of abandoned terminals.** A client that drops its
   output stream without POSTing /api/terminal/close (tab close, crash, network
   drop) leaves the shell running (no PDEATHSIG, by design). Without a ceiling
   these pile up toward fd/thread exhaustion. `_MAX_TERMINALS` now caps the live
   population, evicting the least-recently-active terminal to make room.

These use fakes for the shell/pty so they run without spawning real processes;
the real-shell path is covered by test_terminal_linux_lifecycle.
"""
import os

import pytest

if os.name != "posix":
    pytest.skip("terminal tests require POSIX terminal support", allow_module_level=True)

import api.terminal as terminal


class _FakeProc:
    """A shell process fake. ``alive`` controls poll()."""

    def __init__(self, pid=424242, alive=True):
        self.pid = pid
        self._alive = alive
        self.wait_calls = []

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


def _make_registered_term(monkeypatch, sid, *, alive=True, last_activity=0.0, real_fd=True):
    """Build a TerminalSession, register it in _TERMINALS, no reader thread."""
    if real_fd:
        r, w = os.pipe()
        os.close(w)  # leave a real, closable fd as master_fd
        master_fd = r
    else:
        master_fd = -1
    term = terminal.TerminalSession(
        session_id=sid,
        workspace="/tmp",
        proc=_FakeProc(alive=alive),
        master_fd=master_fd,
        last_activity=last_activity,
    )
    with terminal._LOCK:
        terminal._TERMINALS[sid] = term
    return term


@pytest.fixture(autouse=True)
def _clean_terminals(monkeypatch):
    # Never kill real process groups in these unit tests.
    monkeypatch.setattr(terminal.os, "killpg", lambda *a, **k: None)
    yield
    with terminal._LOCK:
        sids = list(terminal._TERMINALS)
    for sid in sids:
        try:
            terminal.close_terminal(sid)
        except Exception:
            pass


# ── Leak 1: reader loop retires the session on shell exit ────────────────────

def test_reader_loop_retires_session_and_closes_fd(monkeypatch):
    sid = "leak-reader-exit"
    term = _make_registered_term(monkeypatch, sid, alive=False)
    fd = term.master_fd

    # Run the reader loop directly: a dead proc makes it exit its first iteration.
    terminal._reader_loop(term)

    with terminal._LOCK:
        assert sid not in terminal._TERMINALS, "entry not retired on shell exit"
    with pytest.raises(OSError):
        os.fstat(fd)  # master_fd was closed — no leak


def test_reader_loop_retire_is_identity_guarded_against_restart(monkeypatch):
    """An old reader thread finishing after a restart must not tear down the
    new terminal that replaced its session id."""
    sid = "leak-restart-race"
    old = _make_registered_term(monkeypatch, sid, alive=False)
    # Simulate a restart: a NEW terminal now occupies the same sid.
    new = _make_registered_term(monkeypatch, sid, alive=True)
    assert terminal._TERMINALS[sid] is new

    # The OLD reader loop finishes now.
    terminal._reader_loop(old)

    # The new terminal must still be registered and its fd open.
    assert terminal._TERMINALS.get(sid) is new
    os.fstat(new.master_fd)  # not closed


# ── Leak 2: _MAX_TERMINALS cap evicts the least-recently-active terminal ──────

def test_cap_evicts_least_recently_active(monkeypatch):
    monkeypatch.setattr(terminal, "_MAX_TERMINALS", 3)
    # Fill to the cap with alive terminals of increasing activity.
    terms = {}
    for i in range(3):
        terms[i] = _make_registered_term(
            monkeypatch, f"cap-{i}", alive=True, last_activity=100.0 + i
        )
    # cap-0 is least-recently-active. Enforcing the cap for a NEW sid evicts it.
    terminal._enforce_terminal_cap(exclude_sid="cap-new")

    with terminal._LOCK:
        live = set(terminal._TERMINALS)
    assert "cap-0" not in live, "least-recently-active terminal was not evicted"
    assert {"cap-1", "cap-2"} <= live
    assert len(live) < 3  # room was made for the new terminal


def test_cap_prefers_dead_terminals_for_eviction(monkeypatch):
    monkeypatch.setattr(terminal, "_MAX_TERMINALS", 3)
    # A dead terminal that is NOT the least-recently-active must still go first.
    _make_registered_term(monkeypatch, "cap-dead", alive=False, last_activity=999.0)
    _make_registered_term(monkeypatch, "cap-live-a", alive=True, last_activity=1.0)
    _make_registered_term(monkeypatch, "cap-live-b", alive=True, last_activity=2.0)

    terminal._enforce_terminal_cap(exclude_sid="cap-new")

    with terminal._LOCK:
        live = set(terminal._TERMINALS)
    assert "cap-dead" not in live, "dead terminal not preferred for eviction"
    assert {"cap-live-a", "cap-live-b"} <= live


def test_cap_reuse_of_existing_sid_evicts_nothing(monkeypatch):
    monkeypatch.setattr(terminal, "_MAX_TERMINALS", 2)
    _make_registered_term(monkeypatch, "keep-a", alive=True, last_activity=1.0)
    _make_registered_term(monkeypatch, "keep-b", alive=True, last_activity=2.0)

    # Reusing an already-registered sid replaces in place — no growth, no evict.
    terminal._enforce_terminal_cap(exclude_sid="keep-a")

    with terminal._LOCK:
        assert {"keep-a", "keep-b"} <= set(terminal._TERMINALS)


# ── F1: writes/resizes are serialized against close (no fd-reuse injection) ───

def test_write_after_close_raises_and_never_touches_fd(monkeypatch):
    """A write racing a teardown must not reach os.write once the terminal is
    closed — otherwise the recycled fd number could receive foreign input."""
    sid = "io-write-after-close"
    term = _make_registered_term(monkeypatch, sid, alive=True)

    calls = []
    monkeypatch.setattr(terminal.os, "write", lambda fd, data: calls.append(fd))

    # Simulate the teardown having marked the terminal closed.
    term.closed.set()
    with pytest.raises(KeyError):
        terminal.write_terminal(sid, "rm -rf /\n")
    assert calls == [], "write reached os.write after close — fd-reuse risk"


def test_close_acquires_io_lock_before_closing_fd(monkeypatch):
    """close_terminal must take io_lock around os.close so a concurrent writer
    holding io_lock finishes (or bails) first — proving the two are serialized."""
    import threading

    sid = "io-close-serialized"
    term = _make_registered_term(monkeypatch, sid, alive=False)
    fd = term.master_fd

    closed_fd = []
    monkeypatch.setattr(terminal.os, "close", lambda f: closed_fd.append(f))

    # Hold io_lock, then kick off a close in another thread and confirm it blocks
    # on the fd-close until we release.
    with term.io_lock:
        t = threading.Thread(target=lambda: terminal.close_terminal(sid, expected=term))
        t.start()
        t.join(timeout=0.3)
        assert closed_fd == [], "close closed the fd without waiting for io_lock"
    t.join(timeout=2.0)
    assert closed_fd == [fd], "close did not close the fd after io_lock released"


# ── F2: cap eviction is identity-guarded ─────────────────────────────────────

def test_cap_eviction_uses_expected_guard(monkeypatch):
    monkeypatch.setattr(terminal, "_MAX_TERMINALS", 1)
    victim = _make_registered_term(monkeypatch, "cap-victim", alive=True, last_activity=1.0)

    seen = {}

    real_close = terminal.close_terminal

    def _spy_close(sid, *, expected=None):
        seen["sid"] = sid
        seen["expected"] = expected
        return real_close(sid, expected=expected)

    monkeypatch.setattr(terminal, "close_terminal", _spy_close)
    terminal._enforce_terminal_cap(exclude_sid="cap-new")

    assert seen.get("sid") == "cap-victim"
    assert seen.get("expected") is victim, "cap eviction must pass expected= for identity safety"
