from __future__ import annotations

import json
import os
import queue

import pytest


def _clear_run(config, stream_id: str) -> None:
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.pop(stream_id, None)


def _join_restart_thread(thread, timeout: float = 15.0) -> None:
    """Join the _schedule_restart daemon thread.

    Waiting for the drain MARKER to disappear is not enough: monkeypatch
    teardown can restore the real _wait_until_restart_safe/env before the
    daemon reaches them (the thread then runs the REAL wait + a full
    REPO_ROOT/agent pycache purge while still holding _apply_lock, and
    exit_restart_drain unlinks the wrong directory). Joining the thread is the
    only wait that covers the entire unwind. The thread is daemon=True, so the
    join is bounded by the thread's own work (wait is mocked or registry-empty;
    execv is the conftest no-op in tests).
    """
    if thread is not None:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "restart thread did not finish in time"


def test_current_process_drain_marker_refuses_new_run(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    marker = tmp_path / f"{os.getpid()}.json"
    marker.write_text(json.dumps({"reason": "revision_change"}) + "\n")
    stream_id = "drain-current-process"

    with pytest.raises(config.RunAdmissionDrainingError):
        config.register_active_run(stream_id, session_id="session-1")

    with config.ACTIVE_RUNS_LOCK:
        assert stream_id not in config.ACTIVE_RUNS


def test_other_process_drain_marker_does_not_refuse_run(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    (tmp_path / f"{os.getpid() + 1}.json").write_text("{}\n")
    stream_id = "drain-other-process"

    try:
        config.register_active_run(stream_id, session_id="session-2")
        with config.ACTIVE_RUNS_LOCK:
            assert config.ACTIVE_RUNS[stream_id]["session_id"] == "session-2"
    finally:
        _clear_run(config, stream_id)


def test_malformed_current_process_marker_still_refuses_run(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    (tmp_path / f"{os.getpid()}.json").write_text("not-json\n")
    stream_id = "drain-malformed-current-process"

    with pytest.raises(config.RunAdmissionDrainingError):
        config.register_active_run(stream_id, session_id="session-3")

    with config.ACTIVE_RUNS_LOCK:
        assert stream_id not in config.ACTIVE_RUNS


def test_chat_start_returns_retryable_maintenance_before_session_mutation(monkeypatch, tmp_path):
    from api import routes

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    (tmp_path / f"{os.getpid()}.json").write_text("{}\n")
    observed = {}

    def fake_json(_handler, payload, status=200):
        observed.update({"payload": payload, "status": status})
        return True

    monkeypatch.setattr(routes, "j", fake_json)
    monkeypatch.setattr(
        routes,
        "_get_or_materialize_session",
        lambda *_args, **_kwargs: pytest.fail("draining request mutated session state"),
    )

    assert routes._handle_chat_start(object(), {"session_id": "session-4", "message": "hello"}) is True
    assert observed["status"] == 503
    assert observed["payload"]["status"] == "restart_draining"
    assert observed["payload"]["retryable"] is True


def test_local_worker_race_emits_terminal_retryable_event(monkeypatch, tmp_path):
    from api import config, streaming

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    (tmp_path / f"{os.getpid()}.json").write_text("{}\n")
    stream_id = "drain-worker-race"
    events = queue.Queue()
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = events

    try:
        streaming._run_agent_streaming(
            session_id="session-5",
            msg_text="hello",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )
        event, payload = events.get_nowait()
        assert event == "apperror"
        assert payload["type"] == "restart_draining"
        assert payload["retryable"] is True
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)


def test_gateway_worker_race_emits_terminal_retryable_event(monkeypatch, tmp_path):
    from api import config, gateway_chat

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    (tmp_path / f"{os.getpid()}.json").write_text("{}\n")
    stream_id = "drain-gateway-worker-race"
    events = queue.Queue()
    with config.STREAMS_LOCK:
        config.STREAMS[stream_id] = events

    try:
        gateway_chat._run_gateway_chat_streaming(
            session_id="session-6",
            msg_text="hello",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )
        event, payload = events.get_nowait()
        assert event == "apperror"
        assert payload["type"] == "restart_draining"
        assert payload["retryable"] is True
    finally:
        with config.STREAMS_LOCK:
            config.STREAMS.pop(stream_id, None)


def test_health_advertises_restart_drain_capability(monkeypatch):
    from api import routes

    observed = {}

    def fake_json(_handler, payload, status=200):
        observed.update({"payload": payload, "status": status})
        return True

    monkeypatch.setattr(routes, "j", fake_json)
    parsed = type("Parsed", (), {"query": ""})()

    assert routes._handle_health(object(), parsed) is True
    assert observed["status"] == 200
    assert observed["payload"]["restart_drain_supported"] is True


def test_schedule_restart_enters_drain_before_wait(monkeypatch, tmp_path):
    """The real restart producer enters drain BEFORE waiting and keeps the
    marker active through the replacement. Manufactured-marker tests above
    prove admission behavior; this one proves the lifecycle edge: marker
    appears at _schedule_restart() call time, is still present through the
    wait/exec handoff, and is rolled back if the restart aborts."""
    from api import config, updates

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    assert not config.restart_drain_active()

    milestones = []

    def fake_wait():
        milestones.append("wait_marker_present=%s" % config.restart_drain_active())
        return {"active_streams": 0, "active_runs": 0, "restart_blocked": False}

    monkeypatch.setattr(updates, "_wait_until_restart_safe", fake_wait)
    monkeypatch.setattr(updates, "_purge_agent_pycache", lambda *_a, **_k: None)

    def fake_execv(*_a, **_k):
        milestones.append("execv_marker_present=%s" % config.restart_drain_active())
        # A real execv never returns; returning here simulates the handoff
        # completing, which routes _do into the finally-rollback (correct:
        # in-test the process survives, so the marker must be cleaned up).

    monkeypatch.setattr(updates.os, "execv", fake_execv)
    monkeypatch.setattr(updates.sys, "frozen", False, raising=False)

    _thread = updates._schedule_restart(delay=0)
    # _do is a daemon thread — poll for the execv milestone.
    import time as _time
    deadline = _time.monotonic() + 10
    while not any(m.startswith("execv_") for m in milestones):
        assert _time.monotonic() < deadline, milestones
        _time.sleep(0.05)
    deadline = _time.monotonic() + 10
    while config.restart_drain_active():
        assert _time.monotonic() < deadline, "drain marker not rolled back after handoff"
        _time.sleep(0.05)
    # Drain was active THROUGH the wait and at the exec handoff...
    assert milestones[0] == "wait_marker_present=True", milestones
    assert milestones[1] == "execv_marker_present=True", milestones
    # ...and the rollback cleaned it up once the handoff returned in-test.
    assert not config.restart_drain_active(), "marker leaked after handoff"
    _join_restart_thread(_thread)


def test_schedule_restart_rolls_back_drain_on_spawn_failure(monkeypatch, tmp_path):
    """If the restart never happens (spawn failure / wait exception), the
    still-running process must resume admitting work — no stuck marker."""
    from api import config, updates

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    assert not config.restart_drain_active()

    def boom():
        raise RuntimeError("simulated wait failure")

    monkeypatch.setattr(updates, "_wait_until_restart_safe", boom)
    _thread = updates._schedule_restart(delay=0)
    import time as _time
    deadline = _time.monotonic() + 10
    while config.restart_drain_active():
        assert _time.monotonic() < deadline, "drain marker never rolled back"
        _time.sleep(0.05)
    _join_restart_thread(_thread)
    # Admission works again after rollback.
    config.register_active_run("post-rollback-run", session_id="s")
    with config.ACTIVE_RUNS_LOCK:
        assert "post-rollback-run" in config.ACTIVE_RUNS
        config.ACTIVE_RUNS.pop("post-rollback-run", None)


def test_enter_and_exit_drain_marker_roundtrip(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setenv("HERMES_WEBUI_RESTART_DRAIN_DIR", str(tmp_path))
    assert not config.restart_drain_active()
    config.enter_restart_drain(reason="test")
    assert config.restart_drain_active()
    marker = tmp_path / f"{os.getpid()}.json"
    payload = json.loads(marker.read_text())
    assert payload["pid"] == os.getpid()
    assert payload["reason"] == "test"
    config.exit_restart_drain()
    assert not config.restart_drain_active()
    # Exit is idempotent.
    config.exit_restart_drain()
    assert not config.restart_drain_active()
