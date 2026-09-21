"""Run real streaming lifecycle code at the two control-plane boundaries.

Only the external Agent/provider is replaced; barriers exercise the real worker,
Stop, Steer, registry transitions and leftover SSE, without provider/tool calls.
"""
import queue
import sys
import threading
import types
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from api import config, models, streaming
from api.models import Session
from tests.test_real_steer import _captured_response, _make_handler
from tests.test_steer_cancel_ordering import ObservedLock


@pytest.fixture
def worker_scene(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
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
    lock = ObservedLock()
    monkeypatch.setattr(config, "STREAMS_LOCK", lock)
    monkeypatch.setattr(streaming, "STREAMS_LOCK", lock)
    session = Session(session_id="original", title="Control boundary",
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
    scene = SimpleNamespace(session=session, events=events, lock=lock, agent=None,
                            on_init=lambda: None, on_run=lambda: None,
                            on_drain=lambda: None, calls=[], drained=[], result=None)

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
            scene.on_init()

        def run_conversation(self, **kwargs):
            scene.calls.append("run")
            scene.on_run()
            if scene.result is not None:
                return scene.result
            return {"messages": [
                {"role": "user", "content": "Do the task."},
                {"role": "assistant", "content": "Finished."},
            ]}

        def interrupt(self, reason):
            assert lock.owner != threading.get_ident(), "interrupt under stream lock"
            scene.calls.append("interrupt")

        def steer(self, text):
            with self.pending_lock:
                self.pending.append(text)
            scene.calls.append("steer")
            return True

        def _drain_pending_steer(self):
            with self.pending_lock:
                text = "\n".join(self.pending)
                self.pending.clear()
            scene.drained.append(text)
            scene.on_drain()
            return text or None

    monkeypatch.setattr(streaming, "get_session", lambda sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *a, **kw: ("test-model", "openai", None))
    monkeypatch.setattr(config, "get_config", lambda *a, **kw: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *a, **kw: [])
    state = types.ModuleType("hermes_state")
    state.SessionDB = lambda *a, **kw: object()
    monkeypatch.setitem(sys.modules, "hermes_state", state)
    scene.run = lambda: streaming._run_agent_streaming(
        "original", "Do the task.", "test-model", str(tmp_path), "run")
    return scene


def test_stop_between_stream_lookup_and_run_registration(worker_scene, monkeypatch):
    scene = worker_scene
    looked_up, release = threading.Event(), threading.Event()
    peek, register = streaming.peek_stream, streaming.register_active_run
    registrations = []

    def paused_peek(stream_id):
        q = peek(stream_id)
        looked_up.set()
        assert release.wait(5), "stream lookup was not released"
        return q

    def observed_register(*args, **kwargs):
        registrations.append(kwargs)
        return register(*args, **kwargs)

    monkeypatch.setattr(streaming, "peek_stream", paused_peek)
    monkeypatch.setattr(streaming, "register_active_run", observed_register)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(scene.run)
        try:
            assert looked_up.wait(5)
            assert streaming.cancel_stream("run")
        finally:
            release.set()
        future.result(timeout=10)
    assert registrations == [], "cancelled worker republished active-run state"
    assert scene.agent is None
    assert "run" not in config.ACTIVE_RUNS
    assert "run" not in config.CANCEL_FLAGS


def test_initial_run_registration_is_fenced_with_cancel_flag(worker_scene, monkeypatch):
    scene = worker_scene
    register = streaming.register_active_run
    registrations = []

    def observed_register(stream_id, **metadata):
        assert scene.lock.owner == threading.get_ident()
        assert stream_id in config.STREAMS
        flag = config.CANCEL_FLAGS[stream_id]
        assert not flag.is_set()
        registrations.append(flag)
        return register(stream_id, **metadata)

    monkeypatch.setattr(streaming, "register_active_run", observed_register)
    # Stop just after registration, before journal setup used to recreate flags.
    def cancel_at_journal(*args, **kwargs):
        assert scene.lock.owner != threading.get_ident()
        assert streaming.cancel_stream("run")
        return None

    monkeypatch.setattr(streaming, "RunJournalWriter", cancel_at_journal)
    scene.run()
    assert len(registrations) == 1
    assert registrations[0].is_set()
    assert scene.agent is None, "cancel event was replaced before preflight"
    assert "run" not in config.CANCEL_FLAGS
    assert "run" not in config.ACTIVE_RUNS


@pytest.mark.parametrize("cancellation", ["stop", "event-only", "detached-only"])
def test_stop_during_agent_creation_prevents_provider_run(worker_scene, monkeypatch, cancellation):
    scene = worker_scene
    reached, release = threading.Event(), threading.Event()

    def during_init():
        reached.set()
        assert release.wait(5), "initialization barrier timed out"

    scene.on_init = during_init
    finalize = streaming._finalize_cancelled_turn
    finalized = []

    def finalize_outside_lock(*args, **kwargs):
        assert scene.lock.owner != threading.get_ident(), "finalization under stream lock"
        finalized.append(True)
        return finalize(*args, **kwargs)

    monkeypatch.setattr(streaming, "_finalize_cancelled_turn", finalize_outside_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(scene.run)
        try:
            assert reached.wait(5), "worker never reached agent creation"
            event = config.CANCEL_FLAGS["run"]
            if cancellation == "stop":
                assert streaming.cancel_stream("run") is True
                assert event.is_set()
                assert "run" not in config.CANCEL_FLAGS
                assert "run" not in config.STREAMS
            elif cancellation == "event-only":
                with config.STREAMS_LOCK:
                    event.set()
                    config.CANCEL_FLAGS.pop("run")
            else:
                with config.STREAMS_LOCK:
                    config.STREAMS.pop("run")
                assert not event.is_set()
        finally:
            release.set()
        worker.result(timeout=10)
    assert "run" not in scene.calls, "accepted Stop must prevent the provider/tool run"
    assert finalized, "cancelled turn was not finalized"
    assert "run" not in config.AGENT_INSTANCES
    assert "run" not in config.ACTIVE_RUNS


@pytest.mark.parametrize("registered,rotated", [(True, True), (True, False), (False, False)])
@pytest.mark.parametrize("first", ["steer", "drain"])
def test_final_drain_fences_steer(worker_scene, monkeypatch, registered, rotated, first):
    scene = worker_scene
    reached, release = threading.Event(), threading.Event()
    request_thread = threading.get_ident()
    # An already-resolved request can retain the pre-settlement session pointer.
    # Registries must reject late delivery even if this projection is stale.
    request_session = SimpleNamespace(active_stream_id="run")
    monkeypatch.setattr(streaming, "get_session", lambda sid:
                        request_session if threading.get_ident() == request_thread else scene.session)
    update = streaming.update_active_run
    finalizing_edges = []

    def observe_update(*args, **kwargs):
        if kwargs.get("phase") == "finalizing":
            finalizing_edges.append(scene.lock.owner == threading.get_ident())
        return update(*args, **kwargs)

    monkeypatch.setattr(streaming, "update_active_run", observe_update)

    def pause():
        reached.set()
        assert release.wait(5), "drain/steer barrier timed out"

    def running():
        if rotated:
            scene.agent.session_id = "compressed-child"
            config.STREAM_LIVE_SESSION_LINEAGE["run"] = {
                "original",
                "compressed-child",
            }
        if not registered:
            with config.STREAMS_LOCK:
                config.AGENT_INSTANCES.pop("run")
        if first == "steer":
            pause()

    scene.on_run = running
    if first == "drain":
        scene.on_drain = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(scene.run)
        try:
            assert reached.wait(5), "worker never reached the requested boundary"
            handler = _make_handler()
            streaming._handle_chat_steer(handler, {"session_id": "original", "text": "guidance"})
            result = _captured_response(handler)
            if first == "steer":
                assert result == {"accepted": True, "fallback": None, "stream_id": "run"}
            else:
                assert result == {"accepted": False, "fallback": "not_running", "stream_id": "run"}
                assert scene.agent.pending == []
        finally:
            release.set()
        worker.result(timeout=10)
    emitted = list(scene.events.queue)
    normalized = [item[:2] for item in emitted]
    assert not [payload for event, payload in normalized if event == "apperror"]
    leftovers = [
        payload["text"]
        for event, payload in normalized
        if event == "pending_steer_leftover"
    ]
    assert leftovers == (["guidance"] if first == "steer" else [])
    assert scene.drained == (["guidance"] if first == "steer" else [""])
    assert scene.agent.pending == []
    assert finalizing_edges == [True], "final drain admission must close under the stream lock"
    assert "run" not in config.ACTIVE_RUNS
