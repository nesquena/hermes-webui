"""#7293 Codex r2 finding 1 — a settle must not return before a run whose worker
holds the session lock is cancelled and persisted.

The returned-error credential retry runs ``run_conversation()`` while the
worker holds the owning session's lock. A restart settle that waited for that
lock longer than the settle budget returned (and the restart reached ``execv``)
with the run's cancellation unpublished, its agent never interrupted and
neither its partial output nor a server marker on disk.

These tests drive the REAL streaming worker into that branch (only the Agent
and the credential refresh are synthetic: the retry's provider call blocks
until it is interrupted) and settle it through the real
``run_drain.settle_webui_owned_runs``. Everything is checked at the moment the
settle returns, which is the moment a restart would ``execv``.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from api import config, run_drain, streaming
from tests.test_steer_worker_boundaries import worker_scene as worker_scene

RETRY_PARTIAL = "Partial answer streamed by the credential retry"


def _drive_into_lock_holding_credential_retry(scene, monkeypatch):
    """Fake Agent: a 401 first, then a retry that streams and hangs until interrupted."""
    created: list = []
    interrupts: list = []
    retry_entered = threading.Event()
    interrupted = threading.Event()

    def on_init():
        candidate = scene.agent
        created.append(candidate)

        def interrupt(reason):
            interrupts.append((candidate, reason))
            interrupted.set()

        candidate.interrupt = interrupt

    def on_run():
        if len(created) == 1:
            scene.result = {"error": "401 unauthorized", "messages": []}
            return
        # The credential retry: output streams, then the provider call hangs.
        config.STREAM_PARTIAL_TEXT["run"] = RETRY_PARTIAL
        retry_entered.set()
        interrupted.wait(30)
        scene.result = {"messages": [], "interrupted": True, "error": "Interrupted"}

    scene.on_init, scene.on_run = on_init, on_run
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **kw: {
        "provider": "openai", "api_key": "synthetic-self-heal-fixture",
        "base_url": "http://127.0.0.1:1/v1",
    })
    # chat/start registers the turn's writeback owner before the worker runs;
    # the worker's cancelled-turn finalizer is gated on it (#6623). setitem so
    # teardown restores the registry whatever the test does.
    monkeypatch.setitem(config.SESSION_WRITEBACK_OWNERS, "original", "run")
    return created, interrupts, retry_entered, interrupted


def _on_disk(scene) -> dict:
    return json.loads(pathlib.Path(scene.session.path).read_text(encoding="utf-8"))


def _session_lock_is_held_elsewhere() -> bool:
    lock = config._get_session_agent_lock("original")
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True


def _drain_events(scene) -> list:
    out = []
    while not scene.events.empty():
        out.append(scene.events.get_nowait())
    return out


def test_restart_settle_waits_for_a_lock_holding_credential_retry(worker_scene, monkeypatch):
    scene = worker_scene
    created, interrupts, retry_entered, interrupted = _drive_into_lock_holding_credential_retry(
        scene, monkeypatch,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scene.run)
        try:
            assert retry_entered.wait(10), "worker never reached the credential retry"
            assert _session_lock_is_held_elsewhere(), "the retry must be running under the session lock"
            retained_flag = config.CANCEL_FLAGS["run"]

            started = time.monotonic()
            outcome = run_drain.settle_webui_owned_runs(run_drain.RESTART_SETTLE_REASON)
            elapsed = time.monotonic() - started
            # The restart would execv right here: capture what it would leave behind.
            cancel_published = retained_flag.is_set()
            interrupts_at_return = list(interrupts)
            disk = _on_disk(scene)
        finally:
            interrupted.set()  # never leave the fake provider call hanging
        future.result(timeout=15)

    assert outcome["settled"] == ["run"] and outcome["pending"] == [], (outcome, elapsed)
    assert elapsed < run_drain.SHUTDOWN_SETTLE_SECONDS, elapsed
    assert cancel_published, "settle returned before publishing the cancellation"
    assert interrupts_at_return == [(created[-1], run_drain.RESTART_SETTLE_REASON)], interrupts_at_return

    msgs = disk["messages"]
    assistant = [m for m in msgs if m.get("role") == "assistant"]
    assert [m.get("content") for m in assistant if m.get("_partial")] == [RETRY_PARTIAL], msgs
    marker = assistant[-1]
    assert marker["content"].startswith(f"**Task cancelled:** {run_drain.RESTART_SETTLE_REASON}."), msgs
    assert marker.get("cancel_initiator") == "server", msgs
    assert not any("cancelled by the user" in str(m.get("content") or "").lower() for m in msgs), msgs
    assert not any("authentication failed" in str(m.get("content") or "").lower() for m in msgs), msgs
    assert any(m.get("role") == "user" and m.get("content") == "Do the task." for m in msgs), msgs
    assert disk.get("active_stream_id") is None
    assert disk.get("pending_user_message") is None

    # The worker unwound as cancelled (not as an auth failure) and told the client so.
    events = _drain_events(scene)
    assert not [p for kind, p in events if kind == "apperror"], events
    cancels = [p for kind, p in events if kind == "cancel"]
    assert cancels and cancels[-1].get("cancel_initiator") == "server", events
    assert "run" not in config.ACTIVE_RUNS


def test_user_stop_during_a_lock_holding_credential_retry_keeps_partials(worker_scene, monkeypatch):
    """The same branch under a user Stop: the worker finalizes the turn as a
    user cancel (not "Authentication failed") and keeps the streamed partial."""
    scene = worker_scene
    created, interrupts, retry_entered, interrupted = _drive_into_lock_holding_credential_retry(
        scene, monkeypatch,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(scene.run)
        try:
            assert retry_entered.wait(10), "worker never reached the credential retry"
            stop = pool.submit(streaming.cancel_stream, "run")
            assert stop.result(timeout=10) is True
        finally:
            interrupted.set()
        future.result(timeout=15)

    assert interrupts and interrupts[0][0] is created[-1]
    msgs = _on_disk(scene)["messages"]
    assistant = [m for m in msgs if m.get("role") == "assistant"]
    assert [m.get("content") for m in assistant if m.get("_partial")] == [RETRY_PARTIAL], msgs
    markers = [m for m in assistant if str(m.get("content") or "").startswith("**Task cancelled:**")]
    assert len(markers) == 1 and "cancel_initiator" not in markers[0], msgs
    assert "cancelled by the user" in markers[0]["content"]
    assert not any("authentication failed" in str(m.get("content") or "").lower() for m in msgs), msgs


def test_settle_lock_wait_leaves_budget_for_the_interrupted_worker():
    """The settle's bounded wait for the session lock must end well inside the
    settle budget: the remainder is what the interrupt-first fallback has for
    the released worker to persist the turn before the restart proceeds."""
    assert streaming._SERVER_SETTLE_SESSION_LOCK_TIMEOUT + 1.0 <= run_drain.SHUTDOWN_SETTLE_SECONDS
