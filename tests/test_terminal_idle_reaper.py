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
import threading
import time

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


# ── Selection → reconnect → close TOCTOU ────────────────────────────────────
# `_terminals_to_reap()` snapshots victims under `_LOCK` and then RELEASES it.
# `subscribe()` clears `unwatched_since` under the terminal's own `_sub_lock`,
# which the selection pass never held, so a viewer can attach in that gap. The
# old close path re-checked object identity only — still true — and killed a
# terminal that now had a live viewer. The claim must re-establish the whole
# selection predicate under both locks.


def _expired_since():
    """A wall-clock stamp old enough to select a terminal for reaping."""
    return time.time() - terminal._TERMINAL_IDLE_GRACE_SECONDS - 5


def test_reconnect_between_selection_and_close_keeps_the_terminal(monkeypatch):
    """Attach a viewer after selection but before the claim: no reap.

    Drives the claim with the *stale snapshot* the selection pass produced.
    Re-running the full `_reap_idle_terminals()` here would not pin anything:
    its own second selection pass no longer sees the terminal as idle, so the
    test would pass even with an identity-only claim — the exact defect.
    """
    sid = "toctou-reconnect"
    now = time.time()
    term = _make_registered_term(monkeypatch, sid, unwatched_since=_expired_since())

    victims = terminal._terminals_to_reap(now)
    assert [s for s, _ in victims] == [sid], "the terminal was not even selected"

    # The gap: a reconnecting EventSource attaches through the production path.
    attached = terminal.attach_terminal(sid)
    assert attached is not None
    _term, output = attached
    assert term.unwatched_since is None

    # The claim runs against the snapshot taken BEFORE the reconnect.
    for victim_sid, victim_term in victims:
        assert terminal._claim_reap_victim(victim_sid, victim_term, now) is None

    with terminal._LOCK:
        assert terminal._TERMINALS.get(sid) is term, "a watched terminal was reaped"
    assert not term.closed.is_set()

    # And it still receives output on that viewer's queue.
    term.put_output("terminal_output", {"data": "hello"})
    assert output.get(timeout=1)[1] == "terminal_output"


def test_reconnect_racing_the_claim_under_a_barrier_keeps_the_terminal(monkeypatch):
    """Same race, driven deterministically across two threads.

    The reaper is paused *after* it has selected its victim and is about to
    claim it; the viewer attaches through the production path in that window;
    then the reaper resumes.
    """
    sid = "toctou-barrier"
    term = _make_registered_term(monkeypatch, sid, unwatched_since=_expired_since())

    selected = threading.Event()
    attached_done = threading.Event()
    real_select = terminal._terminals_to_reap

    def paused_select(now):
        victims = real_select(now)
        selected.set()
        # Hold the reaper here until the viewer is attached.
        assert attached_done.wait(timeout=5)
        return victims

    monkeypatch.setattr(terminal, "_terminals_to_reap", paused_select)

    result = {}

    def reap():
        result["reaped"] = terminal._reap_idle_terminals(time.time())

    reaper = threading.Thread(target=reap)
    reaper.start()
    try:
        assert selected.wait(timeout=5)
        attached = terminal.attach_terminal(sid)
        assert attached is not None, "attach lost to a reaper that had not claimed yet"
        _term, output = attached
    finally:
        attached_done.set()
        reaper.join(timeout=10)
    assert not reaper.is_alive()

    assert result["reaped"] == 0
    with terminal._LOCK:
        assert terminal._TERMINALS.get(sid) is term
    assert not term.closed.is_set()
    term.put_output("terminal_output", {"data": "still alive"})
    assert output.get(timeout=1)[1] == "terminal_output"


def test_reap_that_claims_first_makes_attach_report_not_running(monkeypatch):
    """The inverse: once the reaper has claimed, an attach must fail cleanly.

    It must not hand back a queue for a terminal that is being torn down —
    the caller would commit a 200 and stream from a corpse forever.
    """
    sid = "toctou-reap-wins"
    term = _make_registered_term(monkeypatch, sid, unwatched_since=_expired_since())

    assert terminal._reap_idle_terminals(time.time()) == 1
    assert term.closed.is_set()
    with terminal._LOCK:
        assert sid not in terminal._TERMINALS

    assert terminal.attach_terminal(sid) is None


def test_claim_refuses_a_terminal_whose_grace_was_refreshed(monkeypatch):
    """`unwatched_since` moved forward after selection → not idle any more."""
    sid = "toctou-refreshed"
    term = _make_registered_term(monkeypatch, sid, unwatched_since=_expired_since())
    now = time.time()
    assert [s for s, _ in terminal._terminals_to_reap(now)] == [sid]

    # A viewer attached and detached again inside the gap: no subscribers, but
    # the grace restarted.
    term.unwatched_since = time.time()

    assert terminal._claim_reap_victim(sid, term, now) is None
    with terminal._LOCK:
        assert terminal._TERMINALS.get(sid) is term


def test_claim_refuses_a_replaced_registry_entry(monkeypatch):
    """A restart replaced the sid: the old snapshot must not kill the new one."""
    sid = "toctou-replaced"
    old = _make_registered_term(monkeypatch, sid, unwatched_since=_expired_since())
    now = time.time()
    assert [s for s, _ in terminal._terminals_to_reap(now)] == [sid]

    replacement = _make_registered_term(monkeypatch, sid, unwatched_since=None)
    assert terminal._claim_reap_victim(sid, old, now) is None
    with terminal._LOCK:
        assert terminal._TERMINALS.get(sid) is replacement


def test_dead_terminal_is_reaped_even_with_an_attached_viewer(monkeypatch):
    """A viewer watching a corpse must not keep the entry alive forever."""
    sid = "toctou-dead"
    term = _make_registered_term(monkeypatch, sid, alive=False, unwatched_since=None)
    term.subscribe()

    assert terminal._reap_idle_terminals(time.time()) == 1
    with terminal._LOCK:
        assert sid not in terminal._TERMINALS


def test_teardown_runs_without_holding_the_registry_lock(monkeypatch):
    """Process teardown can block for seconds; it must not pin `_LOCK`.

    Otherwise a single hung shell stalls every attach and spawn for the whole
    kill/wait budget.
    """
    sid = "toctou-teardown-lock"
    _make_registered_term(monkeypatch, sid, unwatched_since=_expired_since())

    observed = {}

    def slow_killpg(*_a, **_k):
        # `_LOCK` is an RLock, so probing it from the reaping thread itself
        # would succeed even while held. Probe from a separate thread, and
        # acquire/release there — an RLock may only be released by its owner.
        def probe():
            acquired = terminal._LOCK.acquire(timeout=0.5)
            observed["free"] = acquired
            if acquired:
                terminal._LOCK.release()

        holder = threading.Thread(target=probe)
        holder.start()
        holder.join(timeout=5)

    monkeypatch.setattr(terminal.os, "killpg", slow_killpg)
    terminal._reap_idle_terminals(time.time())

    assert observed.get("free") is True, "_LOCK was held across process teardown"


def test_a_header_write_failure_after_attach_still_unsubscribes(monkeypatch):
    """Attaching before the headers must not be able to leak the subscriber.

    `attach_terminal()` subscribes, and only then are the response headers
    written. A client that dropped in that instant makes `end_headers()` raise
    BrokenPipeError; if that escaped before the try/finally, the queue would
    stay in `_subscribers` forever, pin `unwatched_since` at None, and make the
    terminal permanently unreapable — the very leak this reaper exists to
    prevent, self-inflicted.
    """
    import io
    from types import SimpleNamespace

    import api.routes as routes

    sid = "sse-header-failure"
    term = _make_registered_term(monkeypatch, sid, unwatched_since=_expired_since())

    class _Handler:
        headers = {}

        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            raise ConnectionResetError("client vanished")

    monkeypatch.setattr(routes, "_embedded_terminal_gate_allows", lambda _handler: True)
    monkeypatch.setattr(routes, "_sse_set_write_deadline", lambda _handler: None)

    routes._handle_terminal_output(
        _Handler(), SimpleNamespace(query=f"session_id={sid}")
    )

    assert term._subscribers == [], "the subscriber leaked on a header-write failure"
    assert term.unwatched_since is not None, "the terminal was pinned as watched forever"
    # The grace restarts from the detach, which is correct — but the terminal
    # is reapable again once it elapses, instead of being pinned forever.
    assert terminal._reap_idle_terminals(
        term.unwatched_since + terminal._TERMINAL_IDLE_GRACE_SECONDS + 1
    ) == 1
