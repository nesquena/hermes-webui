"""Stop must not lose to cache publication after an Agent was registered.

Run the real worker and Stop primitive. Pause immediately after the registration
lock is released, not inside the constructor covered by the older tests. Only
external Agent/provider calls are synthetic; no live credentials are used.
"""
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import pytest

from api import config, session_lifecycle, streaming
from tests.test_steer_worker_boundaries import worker_scene as worker_scene


@pytest.mark.parametrize("path", ["initial", "cached", "returned", "exception"])
@pytest.mark.parametrize("cancellation", ["stop", "event-only", "detached-only", "successor", "same-agent-successor", "same-agent-completed", "none"])
def test_stop_after_registration_before_cache_or_invocation(worker_scene, monkeypatch, path, cancellation):
    scene = worker_scene
    reached, release = threading.Event(), threading.Event()
    created, publications, finalized = [], [], []
    worker_id = [None]
    paused = [False]
    successor = object()
    stop_interrupt_seen = threading.Event()
    successor_interrupt_count = None
    monkeypatch.setattr(session_lifecycle, "_sessions", {})
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    config.register_session_writeback_owner("original", "run")

    def on_init():
        created.append(scene.agent)
        interrupt = scene.agent.interrupt
        def observed_interrupt(reason):
            result = interrupt(reason)
            stop_interrupt_seen.set()
            return result
        scene.agent.interrupt = observed_interrupt

    def on_run():
        assert scene.lock.owner != threading.get_ident(), "provider invocation under stream lock"
        if path in ("returned", "exception") and len(created) == 1:
            if path == "exception":
                raise RuntimeError("401 unauthorized")
            scene.result = {"error": "401 unauthorized", "messages": []}
        else:
            scene.result = None

    scene.on_init, scene.on_run = on_init, on_run
    if path == "cached":
        scene.run()
        assert scene.calls.count("run") == 1
        assert config.SESSION_AGENT_CACHE["original"][0] is created[0]
        scene.calls.clear()
        config.STREAMS["run"] = scene.events
        config.STREAM_SESSION_OWNERS["run"] = "original"
        scene.session.active_stream_id = "run"
        scene.session.pending_user_message = "Do the task."
        scene.session.pending_started_at = 2.0
        scene.session.save()
        config.register_session_writeback_owner("original", "run")
        stop_interrupt_seen.clear()

    class ObservedCache(OrderedDict):
        def __setitem__(self, key, value):
            if key == "original":
                publications.append((value[0], scene.lock.owner == threading.get_ident(),
                                     "run" in config.STREAMS))
            super().__setitem__(key, value)

    cache = ObservedCache()
    # Preserve an existing cache hit without counting fixture setup as publication.
    for key, value in config.SESSION_AGENT_CACHE.items():
        OrderedDict.__setitem__(cache, key, value)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", cache)
    if hasattr(streaming, "SESSION_AGENT_CACHE"):
        monkeypatch.setattr(streaming, "SESSION_AGENT_CACHE", cache)
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **kw: {
        "provider": "openai", "api_key": "synthetic-publication-fixture",
        "base_url": "http://127.0.0.1:1/v1",
    })

    target_count = 2 if path in ("returned", "exception") else 1

    def after_registration_unlock():
        if (worker_id[0] != threading.get_ident() or paused[0]
                or len(created) != target_count
                or config.AGENT_INSTANCES.get("run") is not created[-1]):
            return
        paused[0] = True
        reached.set()
        assert release.wait(8), "post-registration barrier timed out"

    scene.lock.after_release = after_registration_unlock
    finalize = streaming._finalize_cancelled_turn

    def observed_finalize(*args, **kwargs):
        assert scene.lock.owner != threading.get_ident(), "finalize under stream lock"
        finalized.append(True)
        return finalize(*args, **kwargs)

    monkeypatch.setattr(streaming, "_finalize_cancelled_turn", observed_finalize)

    def run():
        worker_id[0] = threading.get_ident()
        scene.run()

    with ThreadPoolExecutor(max_workers=2) as pool:
        stop_future = None
        future = pool.submit(run)
        try:
            assert reached.wait(8), "worker never released target registration lock"
            retained = config.CANCEL_FLAGS["run"]
            if cancellation in ("stop", "successor", "same-agent-successor", "same-agent-completed"):
                stop_future = pool.submit(streaming.cancel_stream, "run")
                assert retained.wait(5), "Stop did not publish cancellation"
                assert stop_interrupt_seen.wait(5), "Stop has not finished its own interrupt"
                with config.STREAMS_LOCK:
                    assert "run" not in config.STREAMS
                    if cancellation in ("successor", "same-agent-successor", "same-agent-completed"):
                        if cancellation.startswith("same-agent"):
                            successor = created[-1]
                        config.register_session_writeback_owner("original", "successor")
                        with config.SESSION_AGENT_CACHE_LOCK:
                            OrderedDict.__setitem__(cache, "original", (successor, "new-signature"))
                        session_lifecycle.register_agent("original", successor)
                        scene.session.active_stream_id = "successor"
                        scene.session.pending_user_message = "Successor request"
                        if cancellation == "same-agent-completed":
                            config.clear_session_writeback_owner_if_owned("original", "successor")
                            scene.session.active_stream_id = None
                            scene.session.pending_user_message = None
                        else:
                            config.AGENT_INSTANCES["successor"] = successor
                            config.register_active_run("successor", session_id="original", backend="legacy")
                        if cancellation == "same-agent-successor":
                            with successor.pending_lock:
                                successor.pending.append("Successor-only guidance")
                        successor_interrupt_count = scene.calls.count("interrupt")
            elif cancellation == "event-only":
                with config.STREAMS_LOCK:
                    retained.set()
                    config.CANCEL_FLAGS.pop("run")
            elif cancellation == "detached-only":
                with config.STREAMS_LOCK:
                    config.STREAMS.pop("run")
                assert not retained.is_set()
        finally:
            release.set()
        future.result(timeout=12)
        if stop_future is not None:
            assert stop_future.result(timeout=12)["cancelled"] is True

    prior_runs = 1 if path in ("returned", "exception") else 0
    if cancellation == "none":
        assert scene.calls.count("run") == prior_runs + 1
        assert config.SESSION_AGENT_CACHE["original"][0] is created[-1]
    else:
        assert scene.calls.count("run") == prior_runs, "cancelled candidate was invoked"
        cached = config.SESSION_AGENT_CACHE.get("original")
        if not cancellation.startswith("same-agent"):
            assert not cached or cached[0] is not created[-1], "cancelled candidate stayed reusable"
        assert finalized, "cancellation did not reach terminal settlement"
    if successor_interrupt_count is not None:
        assert scene.calls.count("interrupt") == successor_interrupt_count, "stale worker interrupted its successor"
        assert config.SESSION_AGENT_CACHE["original"][0] is successor
        assert session_lifecycle._sessions["original"]["agent"] is successor
        if cancellation == "same-agent-successor":
            assert successor.pending == ["Successor-only guidance"], "stale terminal drain stole successor guidance"
        if cancellation == "same-agent-completed":
            assert scene.session.active_stream_id is None
        else:
            assert config.AGENT_INSTANCES["successor"] is successor
            assert scene.session.active_stream_id == "successor"
            assert scene.session.pending_user_message == "Successor request"
    assert all(held and live for _, held, live in publications), (
        "Agent cache publication escaped the stream/Stop admission boundary", publications)
    assert "run" not in config.AGENT_INSTANCES
    assert "run" not in config.ACTIVE_RUNS


def test_cache_publication_and_registration_exclude_competing_stop(worker_scene, monkeypatch):
    """Block the actual cache write; Stop cannot observe a half-published Agent."""
    scene = worker_scene
    entered, release = threading.Event(), threading.Event()
    interrupted_state = []
    interrupt_observed = threading.Event()
    monkeypatch.setattr(session_lifecycle, "_sessions", {})
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})
    config.register_session_writeback_owner("original", "run")

    class PublicationBarrier(OrderedDict):
        def __setitem__(self, key, value):
            if key == "original":
                entered.set()
                assert release.wait(8), "cache publication was not released"
            super().__setitem__(key, value)

    cache = PublicationBarrier()
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", cache)
    if hasattr(streaming, "SESSION_AGENT_CACHE"):
        monkeypatch.setattr(streaming, "SESSION_AGENT_CACHE", cache)

    def on_init():
        candidate = scene.agent
        interrupt = candidate.interrupt

        def observed_interrupt(reason):
            interrupted_state.append((
                cache.get("original", (None,))[0] is candidate,
                session_lifecycle._sessions.get("original", {}).get("agent") is candidate,
            ))
            result = interrupt(reason)
            interrupt_observed.set()
            return result

        candidate.interrupt = observed_interrupt

    scene.on_init = on_init
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(scene.run)
        stop = None
        try:
            assert entered.wait(8), "worker never reached cache publication"
            retained = config.CANCEL_FLAGS["run"]
            # Wait at the first publication unlock until Stop has won admission,
            # so the provider cannot outrun the controlled schedule.
            worker_ident = scene.lock.owner
            def wait_for_stop():
                if threading.get_ident() == worker_ident:
                    scene.lock.after_release = None
                    assert interrupt_observed.wait(5), "Stop did not interrupt the published Agent"
            scene.lock.after_release = wait_for_stop
            stop = pool.submit(streaming.cancel_stream, "run")
            assert scene.lock.contender.wait(5), "Stop bypassed publication's stream lock"
            assert not retained.is_set(), "Stop observed half-published Agent state"
        finally:
            release.set()
        worker.result(timeout=12)
        if stop is not None:
            assert stop.result(timeout=12)["cancelled"] is True
    assert interrupted_state[0] == (True, True), "Stop did not see coherent cache/lifecycle publication"
    assert "run" not in scene.calls
    assert "original" not in cache
