"""Regression (#7188 greptile P1): no delivered guidance may be accepted after
the final Steer drain has begun.

Greptile outside-diff P1 at PR head 9e8c41913e (api/streaming.py:9564-9567):

    "Terminal settlement marks the run as finalizing and drains pending Steer
    guidance before closing the journal's acceptance fence. A Steer that
    passed the ownership check just before that transition can still acquire
    the open journal transaction afterward, call agent.steer(), and be
    recorded as delivered even though the final drain has already run."

The window, precisely: a concurrent Steer passes the ownership check while
the run is still phase=running. Before its journal transaction runs, the
worker enters _settle_pending_steer, publishes phase=finalizing, and blocks
inside the agent's final drain. When the racing Steer then acquires the
journal transaction, the acceptance fence is still open, so
accept_and_append_if_nonterminal accepts it and journals steer_delivered —
guidance the runtime never consumes, reported accepted+durable.

Production-composed schedule (real _run_agent_streaming worker, real
_settle_pending_steer, real RunJournalWriter + acceptance fence):

  1. The racing Steer passes the ownership check while phase=running.
  2. The worker's final drain begins (barrier) — the pre-fix window is open.
  3. The Steer's journal transaction runs. Before the fix this journals
     steer_delivered; after the fix the fence is closed by settlement BEFORE
     the drain, so the transaction is rejected (fence_closed → stream_dead).

The fix preserves the documented lock ordering (per-run journal lock →
fence lock) and journal identity: the fence closes through the same
RunJournalWriter created at run admission, so compression-rotated session
identities cannot split the journal.
"""
import queue
import sys
import threading
import types
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from api import config, models, run_journal, streaming
from api.models import Session


@pytest.fixture
def drain_race_scene(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    # The worker thread resolves the journal path lazily via
    # run_journal._default_session_dir() — point it at the tmp dir directly.
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: session_dir)
    maps = {name: {} for name in (
        "SESSIONS", "STREAMS", "CANCEL_FLAGS", "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT", "STREAM_REASONING_TEXT", "STREAM_LIVE_TOOL_CALLS",
        "STREAM_SESSION_OWNERS", "ACTIVE_RUNS", "SESSION_AGENT_LOCKS",
    )}
    maps["SESSION_AGENT_CACHE"] = OrderedDict()
    maps["SESSIONS"] = OrderedDict()
    for name, value in maps.items():
        for module in (config, models, streaming):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    lock = threading.Lock()
    monkeypatch.setattr(config, "STREAMS_LOCK", lock)
    monkeypatch.setattr(streaming, "STREAMS_LOCK", lock)
    session = Session(session_id="original", title="drain race",
                      workspace=str(tmp_path), model="test-model",
                      messages=[], context_messages=[])
    session.active_stream_id = "run"
    session.pending_user_message = "Do the task."
    session.pending_started_at = 1.0
    session.save()
    maps["SESSIONS"]["original"] = session
    events = queue.Queue()
    maps["STREAMS"]["run"] = events
    maps["STREAM_SESSION_OWNERS"]["run"] = "original"
    scene = SimpleNamespace(
        session=session, events=events, agent=None, result=None,
        in_drain=threading.Event(), release=threading.Event(),
        drain_calls=[], steer_calls=[], live_agent=[],
    )
    state = types.ModuleType("hermes_state")
    object.__setattr__(state, "SessionDB", lambda *a, **kw: object())
    monkeypatch.setitem(sys.modules, "hermes_state", state)

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_cache_read_tokens = self.session_cache_write_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = self.ephemeral_system_prompt = self._last_error = None
            self.pending = []
            self.pending_lock = threading.Lock()
            scene.agent = self
            scene.live_agent.append(self)

        def run_conversation(self, **kwargs):
            if scene.result is not None:
                return scene.result
            if getattr(scene, "pause_in_run", None) is not None:
                # Ordering-guard scenario: park here until the test releases,
                # so settlement cannot race the in-time Steer acceptance.
                assert scene.pause_in_run.wait(30), "run barrier timed out"
            return {"messages": [
                {"role": "user", "content": "Do the task."},
                {"role": "assistant", "content": "Finished."},
            ]}

        def steer(self, text):
            with self.pending_lock:
                self.pending.append(text)
            scene.steer_calls.append(text)
            return True

        def _drain_pending_steer(self):
            scene.drain_calls.append(True)
            # Hold the final drain open while the racing Steer's journal
            # transaction runs, mirroring the adversarial schedule.
            scene.in_drain.set()
            assert scene.release.wait(15), "drain barrier timed out"
            with self.pending_lock:
                text = "\n".join(self.pending)
                self.pending.clear()
            return text or None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider",
                        lambda *a, **kw: ("test-model", "openai", None))
    monkeypatch.setattr(config, "get_config", lambda *a, **kw: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *a, **kw: [])
    return scene


def test_no_steer_delivered_after_final_drain_begins(drain_race_scene, tmp_path):
    """A Steer whose journal transaction runs after the final drain has begun
    must be REJECTED (fence closed before drain), never journaled as delivered
    guidance the runtime never consumes."""
    scene = drain_race_scene

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(streaming._run_agent_streaming,
                             "original", "Do the task.", "test-model",
                             str(tmp_path), "run")
        try:
            # Deterministic adversarial schedule (no polling, no timing luck):
            # 1. Park the worker inside run_conversation so the run stays
            #    phase=running while the racing Steer performs its ownership
            #    check — exactly the pre-check window greptile describes.
            scene.pause_in_run = threading.Event()
            assert scene.pause_in_run.wait is not None
            # Wait for the agent to register (worker initialization).
            agent, owner, run = None, None, {}
            for _ in range(400):
                with config.STREAMS_LOCK:
                    agent = config.AGENT_INSTANCES.get("run")
                    owner = config.stream_owner_session_id("run")
                    with config.ACTIVE_RUNS_LOCK:
                        run = dict(config.ACTIVE_RUNS.get("run") or {})
                if agent is not None and run.get("phase") in ("starting", "running"):
                    break
                threading.Event().wait(0.02)
            check_passed = (
                agent is not None
                and "run" in config.STREAMS
                and owner == "original"
                and run.get("session_id") == "original"
                and run.get("phase") in ("starting", "running")
            )
            assert check_passed, (
                f"scenario setup: ownership check did not pass: {run}")

            # 2. Unpark the worker and let it reach the final drain.
            scene.pause_in_run.set()
            assert scene.in_drain.wait(20), "worker never reached the final drain"

            # 3. The racing Steer's journal transaction runs now.
            outcome = streaming._accept_and_publish_steer_event(
                scene.live_agent[0], "original", "run", "racing guidance",
                display_text="racing guidance")
        finally:
            if getattr(scene, "pause_in_run", None) is not None:
                scene.pause_in_run.set()
            scene.release.set()
        worker.result(timeout=30)

    # The runtime must never consume the racing guidance...
    assert scene.steer_calls == [], (
        f"agent.steer() ran after the final drain began: {scene.steer_calls}")

    # ...and the acceptance transaction must report it as rejected. The owned
    # stream is still live in phase=finalizing at this point (settlement only
    # closed admission), so the fence_closed rejection surfaces as not_running
    # (Greptile P1 r4060416268) — never as stream_dead, which would send the
    # client down its dead-stream recovery path.
    assert outcome["accepted"] is False, (
        f"Steer accepted after the final drain started: {outcome}")
    assert outcome["fallback"] == "not_running", outcome

    # The journal must not record the racing guidance as delivered.
    journal = run_journal.read_run_events(
        "original", "run", session_dir=tmp_path / "sessions")
    delivered = [e for e in journal["events"] if e["event"] == "steer_delivered"]
    assert delivered == [], (
        "steer_delivered journaled after the final drain: "
        f"{[e['event_id'] for e in delivered]}")


def test_steer_accepted_before_settlement_still_drains(drain_race_scene, tmp_path, monkeypatch):
    """Ordering guard: a Steer accepted BEFORE settlement must still be
    consumed by the final drain — closing the fence early must not break the
    legitimate drain-first ordering for guidance that arrived in time."""
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from tests.test_real_steer import _captured_response, _make_handler

    scene = drain_race_scene
    monkeypatch.setattr(streaming, "get_session", lambda sid: scene.session)
    # Park the worker inside run_conversation so settlement cannot race the
    # acceptance — this isolates the ordering guard from the settlement race
    # the other test covers.
    release_run = threading.Event()
    scene.pause_in_run = release_run

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(streaming._run_agent_streaming,
                             "original", "Do the task.", "test-model",
                             str(tmp_path), "run")
        try:
            # Wait until the stream-bound path is steerable AND sampled as
            # phase=starting/running, then drive the FULL production steer
            # HTTP path while the worker is parked mid-turn (run_conversation
            # in flight), so settlement cannot race the acceptance.
            steerable = False
            phase = None
            for _ in range(500):
                with config.STREAMS_LOCK:
                    registered = "run" in config.AGENT_INSTANCES
                    with config.ACTIVE_RUNS_LOCK:
                        phase = dict(config.ACTIVE_RUNS.get("run") or {}).get("phase")
                if registered and phase == "running":
                    steerable = True
                    break
                threading.Event().wait(0.01)
            assert steerable, f"run never became steerable: phase={phase}"
            handler = _make_handler()
            streaming._handle_chat_steer(
                handler, {"session_id": "original", "text": "in-time guidance"})
            result = _captured_response(handler)
            assert result["accepted"] is True, result
            assert result.get("durable") is True, result
        finally:
            release_run.set()
            scene.release.set()
        worker.result(timeout=30)

    # The in-time guidance was consumed by the final drain.
    assert scene.steer_calls == ["in-time guidance"], scene.steer_calls
    assert scene.drain_calls, "final drain never ran"


def test_fence_closed_while_finalizing_surfaces_not_running(
    drain_race_scene, tmp_path
):
    """Greptile P1 (discussion_r4060416268): once settlement has published
    phase=finalizing and closed the acceptance fence, the owned stream is
    still LIVE. A Steer rejected by the closed fence must therefore be
    classified as ``not_running`` (guidance arrived after admission closed)
    — not ``stream_dead``, which would send the client down its dead-stream
    recovery path for an orderly lifecycle transition.

    Deterministic schedule (same production-composed fixture as the
    drain-race regression above):

      1. The worker reaches its final drain (barrier) — settlement has
         already published phase=finalizing and closed the fence.
      2. While the drain is parked, the racing Steer's journal transaction
         runs and is rejected fence_closed.
      3. While the drain is STILL parked (finalizing not yet over), the
         classification is observed: not_running, because the stream is
         live, owned, local-backend, and in the finalizing phase.
    """
    scene = drain_race_scene

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(streaming._run_agent_streaming,
                             "original", "Do the task.", "test-model",
                             str(tmp_path), "run")
        try:
            # Let the worker reach the final drain: settlement has published
            # phase=finalizing and closed the acceptance fence before the
            # drain blocks on the barrier.
            assert scene.in_drain.wait(20), "worker never reached the final drain"

            with config.STREAMS_LOCK:
                live = "run" in config.STREAMS
                with config.ACTIVE_RUNS_LOCK:
                    run = dict(config.ACTIVE_RUNS.get("run") or {})
            assert live, "owned stream must still be live during finalizing"
            assert run.get("phase") == "finalizing", run
            assert run.get("session_id") == "original", run
            assert run.get("backend") == streaming.WEBUI_LOCAL_CHAT_BACKEND, run

            # The racing Steer's journal transaction runs now — the fence is
            # closed, so acceptance is rejected without calling agent.steer().
            outcome = streaming._accept_and_publish_steer_event(
                scene.live_agent[0], "original", "run", "late guidance",
                display_text="late guidance")

            # Still inside the finalizing phase: the classification must
            # already be not_running, not stream_dead.
            assert outcome["accepted"] is False, outcome
            assert outcome["fallback"] == "not_running", outcome
        finally:
            scene.release.set()
        worker.result(timeout=30)

    # The runtime never consumed the late guidance and the journal never
    # recorded it as delivered.
    assert scene.steer_calls == [], scene.steer_calls
    journal = run_journal.read_run_events(
        "original", "run", session_dir=tmp_path / "sessions")
    delivered = [e for e in journal["events"] if e["event"] == "steer_delivered"]
    assert delivered == [], (
        "steer_delivered journaled after the fence closed: "
        f"{[e['event_id'] for e in delivered]}")


def test_fence_closed_after_teardown_surfaces_stream_dead(
    drain_race_scene, tmp_path
):
    """Fail-closed counterpart: the same fence_closed reason must still
    classify as stream_dead once the owned stream is gone from STREAMS
    (cancel/error teardown), so a genuinely vanished stream never gets the
    softer not_running response."""
    scene = drain_race_scene

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(streaming._run_agent_streaming,
                             "original", "Do the task.", "test-model",
                             str(tmp_path), "run")
        try:
            assert scene.in_drain.wait(20), "worker never reached the final drain"

            with config.STREAMS_LOCK:
                config.STREAMS.pop("run", None)
            with config.ACTIVE_RUNS_LOCK:
                config.ACTIVE_RUNS.pop("run", None)

            outcome = streaming._accept_and_publish_steer_event(
                scene.live_agent[0], "original", "run", "late guidance",
                display_text="late guidance")

            assert outcome["accepted"] is False, outcome
            assert outcome["fallback"] == "stream_dead", outcome
        finally:
            scene.release.set()
        worker.result(timeout=30)

    assert scene.steer_calls == [], scene.steer_calls
