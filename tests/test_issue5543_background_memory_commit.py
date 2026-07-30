"""Tests for issue #5543: fire-and-forget commit_session_memory + shutdown drain.

PR #5543 (@luperrypf) moves commit_session_memory() off the POST
/api/session/new request thread into a daemon "background commit" thread so
"+ New Chat" returns immediately instead of blocking 1-5s on memory-provider
extraction. The gate-certifier required three guarantees this file locks in:

1. Fire-and-forget: spawning the background commit does not block the caller,
   even while the underlying commit is still running.
2. Registry hygiene: a completed background-commit worker unregisters itself so
   ``_background_commit_threads`` does not grow without bound (Finding 2).
3. Shutdown drain: drain_all_on_shutdown() joins in-flight background commit
   threads AND flushes the pending generation, and the server's SIGTERM handler
   routes the managed ``ctl.sh stop`` through the serve_forever() ``finally`` so
   the drain actually runs (Finding 1).
"""

from __future__ import annotations

import ast
import importlib
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _fresh_lifecycle():
    """Import/reload lifecycle module and clear process-global test state."""
    lifecycle = importlib.import_module("api.session_lifecycle")
    lifecycle = importlib.reload(lifecycle)
    reset = getattr(lifecycle, "_reset_for_tests", None)
    if callable(reset):
        reset()
    # Ensure the background-commit registry starts empty for each test.
    with lifecycle._background_commit_threads_lock:
        lifecycle._background_commit_threads.clear()
    return lifecycle


class RecordingAgent:
    """Agent whose commit blocks on an event so we can hold it "in flight"."""

    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def commit_memory_session(self):
        self.calls += 1
        self.entered.set()
        # Block until the test releases us (bounded so a buggy test can't hang).
        self.release.wait(timeout=5)


def _make_background_worker(lifecycle, sid, agent):
    """Mirror the routes.py fire-and-forget worker: commit then self-unregister."""

    def _worker():
        try:
            lifecycle.commit_session_memory(sid, agent=agent)
        finally:
            lifecycle._unregister_background_commit_thread(threading.current_thread())

    t = threading.Thread(target=_worker, daemon=True, name=f"commit-memory-{sid}")
    lifecycle._register_background_commit_thread(t)
    return t


# --------------------------------------------------------------------------- #
# Finding 2: registry hygiene                                                   #
# --------------------------------------------------------------------------- #


def test_register_and_unregister_roundtrip():
    lifecycle = _fresh_lifecycle()
    t = threading.Thread(target=lambda: None)

    # Registration returns True (caller should start) when not draining.
    assert lifecycle._register_background_commit_thread(t) is True
    with lifecycle._background_commit_threads_lock:
        assert t in lifecycle._background_commit_threads

    lifecycle._unregister_background_commit_thread(t)
    with lifecycle._background_commit_threads_lock:
        assert t not in lifecycle._background_commit_threads

    # Idempotent: unregistering an absent thread is a harmless no-op.
    lifecycle._unregister_background_commit_thread(t)


def test_register_refused_once_draining():
    """After drain begins, registration is refused (returns False) and the
    thread is NOT added — the caller must skip start(); the inline generation
    drain commits the pending work instead. This closes Codex's
    register-before-start shutdown window."""
    lifecycle = _fresh_lifecycle()
    # Nothing pending, so drain_all_on_shutdown returns quickly but still sets
    # the _draining flag.
    lifecycle.drain_all_on_shutdown()

    t = threading.Thread(target=lambda: None)
    assert lifecycle._register_background_commit_thread(t) is False
    with lifecycle._background_commit_threads_lock:
        assert t not in lifecycle._background_commit_threads


def test_completed_worker_self_unregisters():
    """A finished background-commit worker must drop itself from the registry."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()  # commit completes immediately
    sid = "self-unregister"
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    t = _make_background_worker(lifecycle, sid, agent)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()

    with lifecycle._background_commit_threads_lock:
        assert t not in lifecycle._background_commit_threads
        assert len(lifecycle._background_commit_threads) == 0
    assert agent.calls == 1


# --------------------------------------------------------------------------- #
# Fire-and-forget: the caller does not block                                    #
# --------------------------------------------------------------------------- #


def test_fire_and_forget_does_not_block_caller():
    """Starting the background commit returns immediately, even mid-commit."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()  # blocks in commit until released
    sid = "non-blocking"
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    t = _make_background_worker(lifecycle, sid, agent)

    start = time.monotonic()
    t.start()
    # Caller "returns" as soon as the thread is started; measure that dispatch.
    elapsed = time.monotonic() - start

    # The commit is genuinely in flight (proves we didn't run it inline).
    assert agent.entered.wait(timeout=2)
    assert elapsed < 1.0, f"caller blocked for {elapsed:.3f}s"
    assert lifecycle.has_uncommitted_work(sid) is True

    # Let the in-flight commit finish and clean up.
    agent.release.set()
    t.join(timeout=5)


# --------------------------------------------------------------------------- #
# Finding 1: shutdown drain flushes the pending commit                          #
# --------------------------------------------------------------------------- #


def test_drain_joins_inflight_background_commit_and_flushes():
    """drain_all_on_shutdown() waits for the in-flight worker, then commits."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()  # held in flight until we release it
    sid = "drain-inflight"
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    t = _make_background_worker(lifecycle, sid, agent)
    t.start()
    assert agent.entered.wait(timeout=2)  # commit is in flight

    drained = threading.Event()

    def _do_drain():
        lifecycle.drain_all_on_shutdown()
        drained.set()

    dt = threading.Thread(target=_do_drain, daemon=True)
    dt.start()

    # While the background commit is still in flight, drain must be blocked
    # joining it — it cannot have finished yet.
    time.sleep(0.1)
    assert not drained.is_set()

    # Release the commit; drain should now join the worker and finish.
    agent.release.set()
    assert drained.wait(timeout=5)
    dt.join(timeout=2)

    assert agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is False
    with lifecycle._background_commit_threads_lock:
        assert t not in lifecycle._background_commit_threads


def test_sigterm_style_shutdown_runs_finally_drain():
    """The SIGTERM handler mechanism drives serve_forever() through its finally.

    Replicates server.py's approach: a signal handler requests shutdown by
    dispatching httpd.shutdown() from a short-lived helper thread (you cannot
    call it from serve_forever()'s own thread). serve_forever() returns, the
    ``finally`` runs, and drain_all_on_shutdown() flushes the pending commit.
    """
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()
    agent.release.set()  # commit completes immediately during drain
    sid = "sigterm-drain"
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test noise
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    finally_ran = threading.Event()

    def _serve():
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
            lifecycle.drain_all_on_shutdown()
            finally_ran.set()

    serve_thread = threading.Thread(target=_serve, daemon=True)
    serve_thread.start()

    # Wait until the server is actually accepting connections before shutdown.
    addr = httpd.server_address
    for _ in range(100):
        try:
            with socket.create_connection(addr, timeout=0.1):
                break
        except OSError:
            time.sleep(0.02)

    # --- exact handler mechanism from server.py ---
    shutdown_requested = threading.Event()

    def _request_shutdown():
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    _request_shutdown()
    _request_shutdown()  # idempotent: a repeated signal must not double-shutdown

    serve_thread.join(timeout=5)
    assert not serve_thread.is_alive(), "serve_forever() did not exit on shutdown"
    assert finally_ran.wait(timeout=5), "finally block / drain did not run"
    assert agent.calls == 1
    assert lifecycle.has_uncommitted_work(sid) is False


# --------------------------------------------------------------------------- #
# Static wiring guards (the inline handler in main() cannot be imported)        #
# --------------------------------------------------------------------------- #


def test_server_installs_sigterm_handler_and_drains_in_finally():
    """server.py must wire a SIGTERM handler and drain in serve_forever's finally."""
    src = (REPO / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # A SIGTERM handler is installed via signal.signal(signal.SIGTERM, ...).
    installs_sigterm = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "signal"
        and node.args
        and ast.dump(node.args[0]).find("SIGTERM") != -1
        for node in ast.walk(tree)
    )
    assert installs_sigterm, "server.py does not install a SIGTERM handler"

    # httpd.shutdown() is dispatched (from a helper thread) rather than the
    # default SIGTERM behavior that would skip the finally.
    assert "httpd.shutdown" in src
    # The existing shutdown drain is still wired.
    assert "drain_all_on_shutdown" in src
    # SIGPIPE handling must be preserved.
    assert "SIGPIPE" in src


# --------------------------------------------------------------------------- #
# Deadline enforcement: a wedged worker must not hang the managed-stop drain     #
# --------------------------------------------------------------------------- #


def test_drain_deadline_returns_despite_stuck_worker():
    """A background commit wedged in commit_memory_session() (e.g. a hung memory
    provider) must NOT hang drain_all_on_shutdown() past its deadline. The inline
    drain runs each commit in a budget-bounded daemon worker, so a stuck in_flight
    session cannot block the drain. Regression for the dual-gate finding that the
    top-of-loop deadline check never fired while blocked inside a stuck commit."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()  # NEVER released → stays wedged in commit
    sid = "stuck-drain"
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)

    # Put the worker in flight, but keep it OUT of the registry so the 5s join
    # phase does not dominate the timing — we are isolating the inline-drain
    # deadline path.
    worker = threading.Thread(
        target=lambda: lifecycle.commit_session_memory(sid, agent=agent),
        daemon=True,
        name="stuck-commit",
    )
    worker.start()
    assert agent.entered.wait(timeout=2)  # worker is now wedged holding in_flight

    started = time.monotonic()
    lifecycle.drain_all_on_shutdown(deadline=0.5)
    elapsed = time.monotonic() - started

    # Without the timeout fix this blocks until the worker is released (never);
    # with it, the drain returns within roughly the deadline budget.
    assert elapsed < 3.0, f"drain hung {elapsed:.1f}s past its 0.5s deadline"

    # Let the wedged worker unwind so the test process exits cleanly.
    agent.release.set()
    worker.join(timeout=5)


def test_drain_deadline_returns_when_inline_provider_hangs_no_prior_worker():
    """Deeper case (2nd-round gate finding): a DIRTY session with NO pre-existing
    in_flight worker, whose provider call hangs when the inline drain owns the
    segment and calls commit_memory_session() DIRECTLY. The timeout= param only
    bounds waiting on an already-in_flight commit; the direct provider call must
    also be budget-bounded (run in a joined daemon worker), or the drain hangs."""
    lifecycle = _fresh_lifecycle()
    agent = RecordingAgent()  # NEVER released → the DIRECT provider call wedges
    sid = "inline-hang"
    lifecycle.register_agent(sid, agent)
    lifecycle.mark_turn_completed(sid, agent=agent)
    # No background worker started: the session is simply dirty
    # (generation > committed_generation) with in_flight=False, so the inline
    # drain itself acquires the segment and calls the hanging provider directly.

    started = time.monotonic()
    lifecycle.drain_all_on_shutdown(deadline=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"inline-drain hung {elapsed:.1f}s past its 0.5s deadline"

    # Let the wedged inline commit worker unwind cleanly.
    agent.release.set()
