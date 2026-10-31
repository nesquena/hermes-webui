"""Cancellation must fence both credential self-heal constructors.

Reuse the real worker/Stop fixture; only the external Agent and credential
refresh are synthetic. No provider or tool operation is executed.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api import config, streaming
from tests.test_steer_worker_boundaries import worker_scene as worker_scene


@pytest.mark.parametrize("failure_path", ["exception", "returned"])
@pytest.mark.parametrize("cancellation", ["stop", "event-only", "detached-only", "none"])
def test_self_heal_registration_obeys_retained_stop(worker_scene, monkeypatch, failure_path, cancellation):
    scene = worker_scene
    entered, release = threading.Event(), threading.Event()
    created, interrupted, finalized = [], [], []

    def on_init():
        candidate = scene.agent
        created.append(candidate)
        original_interrupt = candidate.interrupt

        def interrupt(reason):
            interrupted.append(candidate)
            return original_interrupt(reason)

        candidate.interrupt = interrupt
        if len(created) == 2:
            entered.set()
            assert release.wait(8), "self-heal constructor barrier timed out"

    def on_run():
        if len(created) == 1:
            if failure_path == "exception":
                raise RuntimeError("401 unauthorized")
            scene.result = {"error": "401 unauthorized", "messages": []}
        else:
            scene.result = None

    scene.on_init, scene.on_run = on_init, on_run
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *args, **kwargs: {
        "provider": "openai", "api_key": "synthetic-self-heal-fixture",
        "base_url": "http://127.0.0.1:1/v1",
    })
    finalize = streaming._finalize_cancelled_turn

    def observed_finalize(*args, **kwargs):
        assert scene.lock.owner != threading.get_ident(), "finalize held STREAMS_LOCK"
        finalized.append(True)
        return finalize(*args, **kwargs)

    monkeypatch.setattr(streaming, "_finalize_cancelled_turn", observed_finalize)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stop_future = None
        future = pool.submit(scene.run)
        try:
            assert entered.wait(8), "real worker did not enter credential self-healing"
            assert len(created) == 2
            retained = config.CANCEL_FLAGS["run"]
            if cancellation == "stop":
                # Stop can wait for the session lock after its atomic cancel
                # edge. Do not block constructor release on that later write.
                stop_future = pool.submit(streaming.cancel_stream, "run")
                assert retained.wait(5), "Stop did not publish cancellation"
                with config.STREAMS_LOCK:
                    assert "run" not in config.STREAMS
                    assert "run" not in config.CANCEL_FLAGS
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
            assert stop_future.result(timeout=12) is True

    if cancellation == "none":
        assert scene.calls.count("run") == 2, "valid credential refresh must still retry"
    else:
        assert scene.calls.count("run") == 1, "cancelled self-heal executed the replacement Agent"
        cached = config.SESSION_AGENT_CACHE.get("original")
        assert not cached or cached[0] is not created[-1], "cancelled replacement entered the reusable cache"
        assert created[-1] in interrupted
        assert finalized
    assert "run" not in config.AGENT_INSTANCES
    assert "run" not in config.ACTIVE_RUNS


def test_a_stop_during_the_heal_retry_is_a_cancel_not_a_saved_answer(
    worker_scene, monkeypatch,
):
    """A Stop that lands while the retry runs must not be saved as its answer.

    The constructor fence above covers a Stop before the replacement Agent runs.
    Nothing looked at the flag after ``run_conversation`` returned, so the
    healed answer was persisted as a completed turn while ``put`` dropped the
    ``done`` frame — the client got no terminal event and the user's Stop was
    lost.
    """
    scene = worker_scene
    stop = threading.Event()
    config.CANCEL_FLAGS["run"] = stop
    built = []

    def on_init():
        built.append(scene.agent)

    def on_run():
        if len(built) == 1:
            raise RuntimeError("401 unauthorized")
        stop.set()
        scene.result = {
            "messages": [
                {"role": "user", "content": "Do the task."},
                {"role": "assistant", "content": "Healed answer."},
            ],
        }

    scene.on_init, scene.on_run = on_init, on_run
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *args, **kwargs: {
        "provider": "openai", "api_key": "synthetic-self-heal-fixture",
        "base_url": "http://127.0.0.1:1/v1",
    })

    scene.run()

    assert scene.calls.count("run") == 2, "the heal retry must have run"
    emitted = [event for event, _payload in list(scene.events.queue)]
    assert "cancel" in emitted, "a stopped heal retry must settle as a cancel"
    assert "done" not in emitted
    assert "Healed answer." not in "\n".join(
        str(row.get("content") or "") for row in scene.session.messages
    ), "the cancelled turn must not persist the retry's answer"
