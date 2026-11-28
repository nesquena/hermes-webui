"""#9397: Stop-then-resend on a cached Agent must not corrupt the shared Agent.

Real worker harness: ``api.streaming._run_agent_streaming`` and the real Stop
primitive (``api.streaming.cancel_stream``) on the shared ``worker_scene``
fixture (tests/test_steer_worker_boundaries.py). Only the Agent's provider
call is synthetic — ``run_conversation`` never reaches a network provider —
so the two pre-existing weaknesses from the issue are exercised at worker
level (real HTTP symptom was never confirmed upstream):

1. A cache hit used to rebind callbacks, adopt the per-request SessionDB and
   reset counters BEFORE the worker was admitted. A Stop landing in that
   window left the shared cached Agent mutated even though admission was then
   refused — ``_register_agent_if_current`` deliberately does not interrupt a
   borrowed cache hit.
2. A Stop landing after admission but before ``run_conversation`` left the
   Agent's pending interrupt request set (the turn finalizer never ran) and
   the reuse path reset only the legacy ``_interrupted`` /
   ``_interrupt_message`` attributes. The next turn on the reused Agent then
   failed with "No response from provider".
"""
import threading

import pytest

from api import config, streaming
from tests.test_steer_worker_boundaries import worker_scene as worker_scene


# The fixture instance the module-level Agent fakes talk to; each test sets it.
_SCENE = {"ref": None}


class _FakeSessionDB:
    """Fake SessionDB harness for handle-identity/adoption evidence.

    Tracks which per-request handle the worker would adopt or close; it is not
    a reproduction of a live SessionDB leak (no real state.db is involved).
    """

    def __init__(self):
        self._conn = object()  # `_session_db_is_open` treats this as an open handle
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._conn = None


class _BaseAgent:
    """Common Agent surface; the generation-specific interrupt API is added below."""

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.context_compressor = None
        self.persistence = None
        self.pending = []
        self.pending_lock = threading.Lock()
        self._api_call_count = 1
        self._last_error = None
        self._interrupt_requested = False
        self._interrupt_message = None
        self._interrupted = False
        self._session_db = kwargs.get("session_db")
        self.stream_delta_callback = kwargs.get("stream_delta_callback")
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
        self.status_callback = kwargs.get("status_callback")
        self.tool_start_callback = kwargs.get("tool_start_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")
        self.reasoning_callback = kwargs.get("reasoning_callback")
        self.clarify_callback = kwargs.get("clarify_callback")
        self.callback_at_run = None
        self._turn_session_db_at_run = None
        _SCENE["ref"].agent = self
        _SCENE["ref"].on_init()

    def interrupt(self, reason):
        self._interrupt_requested = True
        self._interrupt_message = reason
        self._interrupted = True
        _SCENE["ref"].calls.append("interrupt")

    def run_conversation(self, **kwargs):
        _SCENE["ref"].calls.append("run")
        self.callback_at_run = self.stream_delta_callback
        self._turn_session_db_at_run = self._session_db
        self._record_interrupt_state()
        _SCENE["ref"].on_run()
        return {
            "messages": [
                {"role": "user", "content": kwargs.get("persist_user_message") or "msg"},
                {"role": "assistant", "content": "Finished."},
            ],
            "completed": True,
        }

    def _record_interrupt_state(self):
        pass

    def _drain_pending_steer(self):
        return None

    def steer(self, text):
        with self.pending_lock:
            self.pending.append(text)
        return True


class _ModernAgent(_BaseAgent):
    """Modern Agent: pending request in ``_interrupt_requested`` + clear_interrupt()."""

    def __init__(self, **kwargs):
        self.clear_interrupt_calls = 0
        self.interrupt_pending_at_run = None
        super().__init__(**kwargs)

    def clear_interrupt(self, **kwargs):
        self.clear_interrupt_calls += 1
        self._interrupt_requested = False
        self._interrupt_message = None
        return True

    def _record_interrupt_state(self):
        self.interrupt_pending_at_run = self._interrupt_requested


class _LegacyAgent(_BaseAgent):
    """Old generation: no ``clear_interrupt()``; only legacy interrupt attrs."""

    def __init__(self, **kwargs):
        self.legacy_interrupted_at_run = None
        super().__init__(**kwargs)

    def _record_interrupt_state(self):
        self.legacy_interrupted_at_run = self._interrupted


def _arm_resend(scene, stream_id="run"):
    """Re-register the stream the route layer would for a resend."""
    config.CANCEL_FLAGS.pop(stream_id, None)
    config.STREAMS[stream_id] = scene.events
    config.STREAM_SESSION_OWNERS[stream_id] = "original"
    scene.session.active_stream_id = stream_id
    scene.session.pending_user_message = "Do the task."
    scene.session.pending_started_at = 2.0
    scene.session.save()


def _install_fake_session_db_factory(monkeypatch):
    handles = []

    def factory(_state_db_path):
        handle = _FakeSessionDB()
        handles.append(handle)
        return handle

    monkeypatch.setattr(streaming, "_build_session_db_for_stream", factory)
    return handles


def test_stop_before_run_then_resend_clears_new_agent_interrupt(worker_scene, monkeypatch):
    """Defect 2: a Stop before run_conversation must not fail the next turn.

    Stop lands after admission (the agent is registered and interrupted), the
    worker exits before run_conversation, and the reuse path must clear the
    pending interrupt — through ``clear_interrupt()`` on newer Agents — before
    the resend runs.
    """
    scene = worker_scene
    _SCENE["ref"] = scene
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _ModernAgent)
    _install_fake_session_db_factory(monkeypatch)
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})

    # Round 1: prime the agent cache.
    scene.run()
    agent = scene.agent
    assert scene.calls.count("run") == 1
    assert config.SESSION_AGENT_CACHE.get("original") is not None

    # Round 2: Stop lands right after admission, before run_conversation.
    _arm_resend(scene)
    real_memory_status = streaming._put_session_memory_status

    def stop_after_admission(put, session_id, stream_id, profile_home):
        assert streaming.cancel_stream(stream_id)
        return real_memory_status(put, session_id, stream_id, profile_home)

    monkeypatch.setattr(streaming, "_put_session_memory_status", stop_after_admission)
    scene.run()
    assert scene.calls.count("run") == 1, "cancelled worker must never reach run_conversation"
    assert agent._interrupt_requested is True, "Stop left the interrupt request pending"
    assert "interrupt" in scene.calls

    # Round 3: resend reuses the cached Agent and must see a clean Agent.
    monkeypatch.setattr(streaming, "_put_session_memory_status", real_memory_status)
    _arm_resend(scene)
    scene.run()
    assert agent.interrupt_pending_at_run is False, (
        "stale Stop interrupt request reached the reused Agent's run_conversation "
        "(real Agent fails this turn with 'No response from provider')"
    )
    assert agent.clear_interrupt_calls >= 1, "modern Agent must be cleared via clear_interrupt()"
    assert scene.calls.count("run") == 2


def test_stop_during_cached_startup_leaves_borrowed_agent_untouched(worker_scene, monkeypatch):
    """Defect 1: a cancelled cache-hit worker must not mutate the borrowed Agent.

    Stop lands while the cached agent's runtime refresh is happening, i.e.
    before callbacks / SessionDB adoption / counter resets and before the
    worker is admitted. The cancelled worker must exit without rebinding
    callbacks, adopting its per-request SessionDB or resetting counters on the
    shared object a successor may already be using.
    """
    scene = worker_scene
    _SCENE["ref"] = scene
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _ModernAgent)
    _install_fake_session_db_factory(monkeypatch)
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})

    # Round 1: prime the cache.
    scene.run()
    agent = scene.agent
    round1_callback = agent.stream_delta_callback
    round1_db = agent._session_db
    round1_db.close()  # worker closes its per-request handle at turn end
    agent._api_call_count = 5
    assert round1_callback is not None

    # Round 2: Stop fires inside the cached runtime refresh, before any
    # mutation of the borrowed agent (defect-1 window).
    _arm_resend(scene)
    real_refresh = streaming._refresh_cached_agent_runtime

    def stop_during_refresh(agent_obj, agent_kwargs):
        assert streaming.cancel_stream("run")
        return True

    monkeypatch.setattr(streaming, "_refresh_cached_agent_runtime", stop_during_refresh)
    scene.run()
    assert scene.calls.count("run") == 1, "cancelled worker must never reach run_conversation"
    assert agent.stream_delta_callback is round1_callback, (
        "cancelled cache-hit worker rebound the shared Agent's callbacks before admission"
    )
    assert agent._session_db is round1_db, (
        "cancelled cache-hit worker adopted its per-request SessionDB into the shared Agent"
    )
    assert agent._api_call_count == 5, (
        "cancelled cache-hit worker reset the shared Agent's counters before admission"
    )
    assert not config.STREAMS.get("run"), "Stop must keep the stream detached"

    # Round 3: the resend that Stop let in reuses the untouched cached Agent.
    monkeypatch.setattr(streaming, "_refresh_cached_agent_runtime", real_refresh)
    _arm_resend(scene)
    scene.run()
    assert scene.calls.count("run") == 2, "resend after Stop did not reach the Agent"
    assert agent.stream_delta_callback is not round1_callback, (
        "resend's callbacks were not installed on the reused Agent"
    )
    assert agent._session_db is not round1_db, (
        "resend did not adopt its per-request SessionDB on the reused Agent"
    )


def test_resend_after_stop_clears_legacy_agent_interrupt_attrs(worker_scene, monkeypatch):
    """Regression: the legacy-Agent generation (no clear_interrupt()) still works.

    Older Agent builds only carry ``_interrupted`` / ``_interrupt_message``;
    the reuse path keeps resetting those so the round-1 -> stop -> resend
    sequence stays healthy for them too.
    """
    scene = worker_scene
    _SCENE["ref"] = scene
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _LegacyAgent)
    _install_fake_session_db_factory(monkeypatch)
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})

    scene.run()
    agent = scene.agent
    assert config.SESSION_AGENT_CACHE.get("original") is not None

    _arm_resend(scene)
    real_memory_status = streaming._put_session_memory_status

    def stop_after_admission(put, session_id, stream_id, profile_home):
        assert streaming.cancel_stream(stream_id)
        return real_memory_status(put, session_id, stream_id, profile_home)

    monkeypatch.setattr(streaming, "_put_session_memory_status", stop_after_admission)
    scene.run()
    assert scene.calls.count("run") == 1
    assert agent._interrupted is True

    monkeypatch.setattr(streaming, "_put_session_memory_status", real_memory_status)
    _arm_resend(scene)
    scene.run()
    assert agent.legacy_interrupted_at_run is False, (
        "legacy _interrupted was not reset before the reused Agent's run_conversation"
    )
    assert scene.calls.count("run") == 2


def test_cancelled_cache_hit_worker_does_not_drain_successor_steer(worker_scene, monkeypatch):
    """A cache-hit worker cancelled during startup must not consume a successor's Steer.

    Stop eagerly releases the old stream registration, so the outer finally's
    pending-steer fallback (``AGENT_INSTANCES.get(stream_id) or agent``) could
    reach the shared cached Agent after a successor had started on it. The
    cancelled worker must relinquish its borrowed local handle before terminal
    cleanup; it must not interrupt, close or drain the successor's object.
    """
    scene = worker_scene
    _install_fake_session_db_factory(monkeypatch)
    monkeypatch.setattr(config, "SESSION_WRITEBACK_OWNERS", {})

    def run_stream(stream_id):
        return streaming._run_agent_streaming(
            "original", "Do the task.", "test-model",
            str(scene.session.workspace), stream_id,
        )

    # Prime the shared cache.
    scene.run()
    agent = scene.agent
    assert config.SESSION_AGENT_CACHE.get("original") is not None

    # Old worker: hold it inside the cached runtime refresh, after the cache-hit
    # borrow and before the post-refresh cancellation re-check.
    _arm_resend(scene, "run")
    old_paused, release_old = threading.Event(), threading.Event()
    real_refresh = streaming._refresh_cached_agent_runtime
    refresh_calls = []

    def gated_refresh(agent_obj, agent_kwargs):
        refresh_calls.append(1)
        if len(refresh_calls) == 1:
            old_paused.set()
            assert release_old.wait(10), "old worker was never released"
        return real_refresh(agent_obj, agent_kwargs)

    monkeypatch.setattr(streaming, "_refresh_cached_agent_runtime", gated_refresh)
    old_thread = threading.Thread(target=lambda: run_stream("run"), daemon=True)
    old_thread.start()
    assert old_paused.wait(10), "old worker never reached the refresh window"
    assert streaming.cancel_stream("run")

    # Successor: same session, same cached Agent, blocked in run_conversation.
    _arm_resend(scene, "run2")
    successor_running, release_successor = threading.Event(), threading.Event()

    def successor_on_run():
        successor_running.set()
        assert release_successor.wait(10), "successor was never released"

    scene.on_run = successor_on_run
    successor_thread = threading.Thread(target=lambda: run_stream("run2"), daemon=True)
    successor_thread.start()
    assert successor_running.wait(10), "successor never started running"

    # Real Steer handler accepts guidance for the successor only.
    steer = streaming._steer_bound_stream("original", "run2", "successor-only guidance")
    assert steer and steer.get("accepted") is True, steer
    assert agent.pending == ["successor-only guidance"]

    # Release the cancelled old worker. Its terminal cleanup must leave the
    # successor's guidance pending and must not drain it.
    release_old.set()
    old_thread.join(timeout=10)
    assert not old_thread.is_alive()
    leaked = list(agent.pending)
    assert "successor-only guidance" not in "".join(scene.drained), (
        "old cancelled worker drained the successor's pending guidance"
    )

    release_successor.set()
    successor_thread.join(timeout=10)
    assert leaked == ["successor-only guidance"], (
        f"old cancelled worker consumed the successor's guidance: {leaked!r}"
    )


def test_installed_agent_exposes_clear_interrupt():
    """Anchor the Agent API the reuse path now depends on.

    ``clear_interrupt()`` is the only reset for the ``_interrupt_requested``
    generation; if the installed Agent ever drops it, the legacy attribute
    reset cannot clear that pending request and the Stop-then-resend failure
    comes back. Skipped only when the installed Agent cannot be built offline.
    """
    run_agent = pytest.importorskip("run_agent")
    try:
        agent = run_agent.AIAgent(
            model="probe-model",
            provider="custom",
            api_key="dummy-key",
            base_url="http://127.0.0.1:9/v1",
            platform="webui",
            quiet_mode=True,
            enabled_toolsets=[],
            session_id="probe-clear-interrupt",
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"installed Agent cannot be constructed offline: {exc}")
    try:
        clear = getattr(agent, "clear_interrupt", None)
        assert callable(clear), "installed Agent must expose clear_interrupt()"
        agent._interrupt_requested = True
        agent._interrupt_message = "stop"
        assert clear() is True
        assert agent._interrupt_requested is False
        assert agent._interrupt_message is None
    finally:
        close = getattr(agent, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
