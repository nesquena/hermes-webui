"""Process progress is not completion, across both WebUI queue consumers."""
from collections import OrderedDict
import threading
from types import SimpleNamespace

import pytest

from api import background_process as bp, config as cfg, process_event_utils as utils
from api import streaming
from tests._wakeup_helpers import FakeProcessRegistry, install_fake_registry

_REAL_WAKEUP_DISPATCH = bp._start_server_side_wakeup_turn


@pytest.fixture
def delivery(monkeypatch):
    registry = FakeProcessRegistry()
    registry.register("proc_progress", "spawn-key")
    registry._procs["proc_progress"].spawn_session_id = "origin-chat"
    install_fake_registry(monkeypatch, registry)
    monkeypatch.setattr(cfg, "PROCESS_SESSION_INDEX", {"spawn-key": "other-chat"})
    monkeypatch.setattr(cfg, "BG_TASK_COMPLETE_EVENTS_SEEN", {})
    monkeypatch.setattr(cfg, "PENDING_BG_TASK_COMPLETIONS", set())
    monkeypatch.setattr(cfg, "DEFERRED_PROCESS_WAKEUPS", {})
    monkeypatch.setattr(cfg, "ACTIVE_RUNS", {})
    monkeypatch.setattr(cfg, "STREAMS", {})
    monkeypatch.setattr(cfg, "STREAM_SESSION_OWNERS", {})
    monkeypatch.setattr(bp, "SESSION_CHANNELS", {})
    monkeypatch.setattr(utils, "_PROCESS_HEARTBEATS", OrderedDict(), raising=False)
    calls = []
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", lambda sid, msg, **kw: calls.append((sid, msg, kw)))
    completions = []
    monkeypatch.setattr(bp, "_emit_bg_task_complete_events_coalesced", lambda sid, data: completions.append((sid, data)))
    return registry, calls, completions


def heartbeat(**changes):
    # Exact native ProcessRegistry._emit_heartbeat shape (no completed_at).
    evt = {"type": "heartbeat", "session_id": "proc_progress", "session_key": "spawn-key",
           "command": "bounded-build", "seq": 1, "interval": 60, "elapsed": 60,
           "output": "compiled module A", "started_at": 1.0}
    evt.update(changes)
    return evt


def completion():
    return dict(heartbeat(), type="completion", exit_code=0, output="build finished")


def test_formatter_reports_running_not_completed():
    prompt = bp.format_wakeup_prompt(heartbeat())
    assert prompt is not None
    assert "still running" in prompt
    assert "compiled module A" in prompt
    assert "completed" not in prompt and "exit_code" not in prompt


@pytest.mark.parametrize("output", ["", " \n\t"])
def test_no_output_is_not_a_model_wakeup(delivery, output):
    registry, calls, completions = delivery
    bp._process_one(heartbeat(output=output))
    assert calls == [] and completions == []
    assert not registry.is_completion_consumed("proc_progress")
    assert not cfg.PENDING_BG_TASK_COMPLETIONS


def test_idle_heartbeat_then_completion_both_deliver(delivery):
    registry, calls, completions = delivery
    bp._process_one(heartbeat())
    assert len(calls) == 1
    assert calls[0][0] == "origin-chat"
    assert not registry.is_completion_consumed("proc_progress")
    assert completions == [] and not cfg.BG_TASK_COMPLETE_EVENTS_SEEN
    bp._process_one(completion())
    assert len(calls) == 2 and len(completions) == 1
    assert "completed" in calls[1][1]
    assert registry.is_completion_consumed("proc_progress")
    assert not utils._PROCESS_HEARTBEATS
    bp._process_one(heartbeat(seq=2, output="late reader output"))
    bp._process_one(completion())
    assert len(calls) == 2 and len(completions) == 1


def test_busy_progress_does_not_collide_with_deferred_completion(delivery):
    registry, calls, completions = delivery
    cfg.ACTIVE_RUNS["busy"] = {"session_id": "origin-chat", "stream_id": "busy", "phase": "running"}
    bp._process_one(heartbeat())
    bp._process_one(completion())
    assert calls == []
    entries = cfg.DEFERRED_PROCESS_WAKEUPS["origin-chat"]
    assert len(entries) == 2
    assert entries[0]["process_id"] != entries[1]["process_id"]
    assert "still running" in entries[0]["wakeup_prompt"]
    assert "completed" in entries[1]["wakeup_prompt"]
    cfg.ACTIVE_RUNS.clear()
    assert bp.drain_deferred_wakeups_for_session("origin-chat") == 1
    assert "still running" in calls[0][1]
    assert bp.drain_deferred_wakeups_for_session("origin-chat") == 1
    assert "completed" in calls[1][1]
    assert bp.drain_deferred_wakeups_for_session("origin-chat") == 0
    assert registry.is_completion_consumed("proc_progress")


@pytest.mark.parametrize("first", ["background", "next-turn"])
def test_cross_consumer_idempotency_and_completion_survival(delivery, first):
    registry, calls, completions = delivery
    evt = heartbeat()
    if first == "background":
        bp._process_one(evt)
        registry.completion_queue.put(evt)
        assert streaming._drain_webui_process_notifications("origin-chat") == []
        assert len(calls) == 1
    else:
        registry.completion_queue.put(evt)
        notes = streaming._drain_webui_process_notifications("origin-chat")
        assert len(notes) == 1 and "still running" in notes[0]
        bp._process_one(evt)
        assert calls == []
    assert not registry.is_completion_consumed("proc_progress")
    registry.completion_queue.put(completion())
    notes = streaming._drain_webui_process_notifications("origin-chat")
    assert len(notes) == 1 and "completed" in notes[0]
    assert registry.is_completion_consumed("proc_progress")


def test_native_registry_produces_progress_delta_without_arming_timers(monkeypatch):
    native = pytest.importorskip("tools.process_registry")
    if not hasattr(native, "ProcessSession") or not hasattr(native.ProcessRegistry, "_emit_heartbeat"):
        pytest.skip("Hermes Agent build does not expose process heartbeats")
    registry = native.ProcessRegistry()
    proc = native.ProcessSession(id="native-progress", command="bounded-build", session_key="native-chat",
                                 started_at=1.0, heartbeat_seconds=60)
    proc.spawn_session_id = "native-chat"
    proc.append_output("compiled module A")
    monkeypatch.setattr(native, "process_registry", registry)
    monkeypatch.setattr(cfg, "PROCESS_SESSION_INDEX", {"native-chat": "native-chat"})
    monkeypatch.setattr(utils, "_PROCESS_HEARTBEATS", OrderedDict())
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: False)
    calls = []
    stop = threading.Event()
    monkeypatch.setattr(bp, "_DRAIN_STOP", stop)

    def wake(sid, msg, **kw):
        calls.append((sid, msg))
        if len(calls) == 2:
            stop.set()

    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", wake)
    registry._emit_heartbeat(proc, 61.0)
    evt = registry.completion_queue.get_nowait()
    assert evt["type"] == "heartbeat" and evt["seq"] == 1 and evt["output"] == "compiled module A"
    first = evt
    registry._emit_heartbeat(proc, 121.0)
    evt = registry.completion_queue.get_nowait()
    assert evt["output"] == ""
    registry.completion_queue.put(first)
    registry.completion_queue.put(evt)
    proc.append_output("compiled module B")
    registry._emit_heartbeat(proc, 181.0)
    bp._drain_loop()
    assert len(calls) == 2 and calls[0][0] == "native-chat"
    assert "compiled module A" in calls[0][1] and "compiled module B" in calls[1][1]
    assert registry.completion_queue.empty()
    assert not registry.is_completion_consumed(proc.id)


def test_next_turn_keeps_foreign_heartbeat_for_exact_owner(delivery):
    registry, calls, completions = delivery
    evt = heartbeat(origin_ui_session_id="origin-chat")
    registry.completion_queue.put(evt)
    assert streaming._drain_webui_process_notifications("other-chat") == []
    assert registry.completion_queue.qsize() == 1
    notes = streaming._drain_webui_process_notifications("origin-chat")
    assert len(notes) == 1 and "still running" in notes[0]
    assert not registry.is_completion_consumed("proc_progress")


def test_only_new_nonempty_output_wakes_model(delivery):
    registry, calls, completions = delivery
    bp._process_one(heartbeat())
    bp._process_one(heartbeat())
    bp._process_one(heartbeat(seq=2))  # repeated text, not meaningful progress
    bp._process_one(heartbeat(seq=3, output=""))  # elapsed time alone is not progress
    bp._process_one(heartbeat(seq=2, output="out of order"))
    bp._process_one(heartbeat(seq=4, output="compiled module B"))
    assert len(calls) == 2
    assert "compiled module B" in calls[1][1]
    assert completions == []
    assert not registry.is_completion_consumed("proc_progress")


def test_next_turn_empty_progress_preserves_following_completion(delivery):
    registry, calls, completions = delivery
    registry.completion_queue.put(heartbeat(output=""))
    registry.completion_queue.put(completion())
    notes = streaming._drain_webui_process_notifications("origin-chat")
    assert len(notes) == 1 and "completed" in notes[0]
    assert registry.is_completion_consumed("proc_progress")
    assert not utils._PROCESS_HEARTBEATS


def test_running_prompt_uses_raw_notice_not_completed_card():
    assert utils.wakeup_display_meta(bp.format_wakeup_prompt(heartbeat())) is None


@pytest.mark.parametrize("seq", [None, 0, -1, "bad", True])
def test_unidentified_heartbeat_is_dropped_without_consuming_completion(delivery, seq):
    registry, calls, completions = delivery
    bp._process_one(heartbeat(seq=seq))
    assert calls == [] and completions == []
    assert not registry.is_completion_consumed("proc_progress")


def test_shared_progress_claim_is_atomic_and_bounded(delivery, monkeypatch):
    monkeypatch.setattr(utils, "PROCESS_HEARTBEAT_DEDUPE_MAX", 2)
    barrier = threading.Barrier(3)
    results = []

    def claim():
        barrier.wait()
        results.append(utils.claim_process_heartbeat(heartbeat(), "origin-chat"))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
    assert sorted(results) == [False, True]
    assert utils.claim_process_heartbeat(heartbeat(), "another-profile-chat")
    assert utils.claim_process_heartbeat(heartbeat(session_id="another-proc"), "origin-chat")
    assert len(utils._PROCESS_HEARTBEATS) == 2


def test_heartbeat_uses_real_server_turn_and_existing_session_channel(delivery, monkeypatch):
    """Real start_session_turn/fanout with isolated session, model and worker."""
    from api import routes
    registry, calls, completions = delivery
    session = SimpleNamespace(session_id="origin-chat", profile="mobile-control", model="test-model",
                              model_provider="test-provider")
    monkeypatch.setattr(routes, "get_session", lambda sid: session if sid == session.session_id else None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda s, w: "/isolated-workspace")
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda s, p: (p, s.model, {}))
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", lambda *a, **kw: (session.model, session.model_provider, False))
    monkeypatch.setattr(routes, "clear_process_wakeup_pause_if_model_changed", lambda *a, **kw: False)
    monkeypatch.setattr(routes, "process_wakeup_pause_credential_state_changed", lambda *a, **kw: False)
    monkeypatch.setattr(routes, "process_wakeup_pause_matches", lambda *a, **kw: False)
    monkeypatch.setattr(routes, "suppress_process_wakeup_for_provider_pause", lambda *a, **kw: None)
    worker_calls = []
    def run(s, **kw):
        worker_calls.append((s, kw))
        return {"stream_id": "heartbeat-stream", "_status": 200, "pending_started_at": 10.0}

    monkeypatch.setattr(routes, "_start_run", run)
    # Restore the actual wakeup dispatcher instead of the fixture recorder.
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", _REAL_WAKEUP_DISPATCH)
    channel, subscriber = bp.subscribe_to_session_channel("origin-chat")
    other, foreign_subscriber = bp.subscribe_to_session_channel("other-chat")
    try:
        bp._process_one(heartbeat())
        event, data = subscriber.get(timeout=3)
        assert event == "server_turn_started"
        assert data["session_id"] == "origin-chat" and data["stream_id"] == "heartbeat-stream"
        assert data["source"] == "process_wakeup"
        assert foreign_subscriber.empty()
        assert len(worker_calls) == 1
        s, kw = worker_calls[0]
        assert s.profile == "mobile-control"
        assert kw["workspace"] == "/isolated-workspace"
        assert kw["model"] == "test-model" and kw["model_provider"] == "test-provider"
        assert "still running" in kw["msg"]
        assert not registry.is_completion_consumed("proc_progress")
        assert completions == []
    finally:
        channel.unsubscribe(subscriber)
        other.unsubscribe(foreign_subscriber)
