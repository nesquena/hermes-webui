"""Regression coverage for notify_on_complete across a WebUI restart."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from api import background_process as bp
from api import config, models, routes
from api.models import Session
from api.session_lineage import (
    CompletionDeliveryRestartError,
    build_completion_delivery_context,
    claim_completion_delivery,
    mark_completion_incorporated,
    pending_completion_delivery_contexts,
    read_completion_delivery_receipt,
    record_lineage_transition,
    release_completion_delivery_claim,
    release_turn_admission,
    verify_completion_incorporation_artifacts,
)
from api.turn_journal import read_turn_journal
from tests._wakeup_helpers import FakeProcessRegistry, install_fake_registry


class _FakeThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def is_alive(self):
        return self.started

    def start(self):
        self.started = True


class _FakeProcessSession:
    def __init__(self, session_key):
        self.session_key = session_key


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _completion_event(session_id, process_id):
    return {
        "type": "completion",
        "session_id": process_id,
        "session_key": session_id,
        "origin_ui_session_id": session_id,
        "command": "true",
        "exit_code": 0,
        "output": "done",
    }


def _install_local_completion_case(monkeypatch, session_id, process_id):
    fake_registry = FakeProcessRegistry()
    fake_registry.register(process_id, session_id)
    install_fake_registry(monkeypatch, fake_registry)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(
        bp,
        "_resolve_session_lineage",
        lambda _sid: SimpleNamespace(
            root_session_id=session_id,
            delivery_session_id=session_id,
            profile="default",
        ),
    )
    monkeypatch.setattr(
        bp,
        "_emit_bg_task_complete_events_coalesced",
        lambda *_args, **_kwargs: 0,
    )
    bp.register_process_session(session_id, session_id)
    return fake_registry, _completion_event(session_id, process_id)


def _cleanup_local_completion_case(session_id, process_id):
    bp.unregister_process_session(session_id)
    bp.forget_bg_task_completion_dedup(session_id)
    bp._PREINCORPORATION_COMPLETION_IDS.discard(process_id)
    config.PENDING_BG_TASK_COMPLETIONS.discard(session_id)
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config.DEFERRED_PROCESS_WAKEUPS.pop(session_id, None)


def test_start_drain_thread_invokes_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    monkeypatch.setattr(bp, "recover_processes_for_webui", lambda: calls.append("recover") or 0)
    monkeypatch.setattr(bp.threading, "Thread", _FakeThread)

    assert bp.start_drain_thread() is True
    assert calls == ["recover"]


def test_start_drain_thread_survives_recovery_failure(monkeypatch):
    def fail_recovery():
        raise OSError("corrupt checkpoint")

    monkeypatch.setattr(bp, "_DRAIN_THREAD", None)
    monkeypatch.setattr(bp, "recover_processes_for_webui", fail_recovery)
    monkeypatch.setattr(bp.threading, "Thread", _FakeThread)

    assert bp.start_drain_thread() is True
    assert bp._DRAIN_THREAD is not None
    assert bp._DRAIN_THREAD.is_alive()


def test_recovery_runs_once_and_rebuilds_session_mapping(monkeypatch):
    calls = {"recover": 0, "registered": []}

    class FakeRegistry:
        def recover_from_checkpoint(self):
            calls["recover"] += 1
            return 1

        def list_sessions(self):
            return [{
                "session_id": "proc_recovered",
                "detached": True,
            }]

        def get(self, process_id):
            assert process_id == "proc_recovered"
            return _FakeProcessSession("webui-session")

    fake_registry = FakeRegistry()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr(
        bp,
        "register_process_session",
        lambda key, sid: calls["registered"].append((key, sid)),
    )

    def get_session(sid, metadata_only=False):
        return SimpleNamespace(id=sid)

    assert bp.recover_processes_for_webui(fake_registry, get_session) == 1
    assert bp.recover_processes_for_webui(fake_registry, get_session) == 0
    assert calls == {
        "recover": 1,
        "registered": [("webui-session", "webui-session")],
    }


def test_partial_recovery_retry_does_not_repeat_checkpoint_adoption(monkeypatch):
    calls = {"recover": 0, "list": 0}

    class FlakyRegistry:
        def recover_from_checkpoint(self):
            calls["recover"] += 1
            return 1

        def list_sessions(self):
            calls["list"] += 1
            if calls["list"] == 1:
                raise OSError("transient list failure")
            return []

    registry = FlakyRegistry()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)

    with pytest.raises(OSError, match="transient list failure"):
        bp.recover_processes_for_webui(registry, lambda *_args, **_kwargs: None)

    assert bp.recover_processes_for_webui(registry, lambda *_args, **_kwargs: None) == 0
    assert calls == {"recover": 1, "list": 2}


def test_concurrent_direct_recovery_runs_once(monkeypatch):
    calls = {"recover": 0}

    class FakeRegistry:
        def recover_from_checkpoint(self):
            time.sleep(0.02)
            calls["recover"] += 1
            return 1

        def list_sessions(self):
            return []

    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    registry = FakeRegistry()
    barrier = threading.Barrier(8)
    results = []

    def recover():
        barrier.wait()
        results.append(
            bp.recover_processes_for_webui(registry, lambda *_args, **_kwargs: None)
        )

    workers = [threading.Thread(target=recover) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert calls["recover"] == 1
    assert sorted(results) == [0] * 7 + [1]


def test_recovery_is_fail_soft_without_agent(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "tools.process_registry":
            raise ImportError("Hermes Agent not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    assert bp.recover_processes_for_webui() == 0
    assert bp._PROCESS_RECOVERY_DONE is False


def test_duplicate_completion_starts_one_turn_before_incorporation(monkeypatch):
    """A process-local retry cannot wake twice while durable ACK is pending."""
    process_id = "proc-pre-incorporation-dedupe"
    session_id = "session-pre-incorporation-dedupe"
    calls = []
    turn_started = threading.Event()

    fake_registry = FakeProcessRegistry()
    install_fake_registry(monkeypatch, fake_registry)
    monkeypatch.setattr(bp, "_REAPER_INTERVAL_SECS", 0.01)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(
        bp,
        "_resolve_session_lineage",
        lambda _sid: SimpleNamespace(
            root_session_id=session_id,
            delivery_session_id=session_id,
            profile="default",
        ),
    )
    monkeypatch.setattr(
        bp,
        "_emit_bg_task_complete_events_coalesced",
        lambda *_args, **_kwargs: 0,
    )

    def start_without_incorporation(
        target_session_id,
        message,
        *,
        source,
        completion_context,
        completion_acceptance,
    ):
        calls.append(
            {
                "session_id": target_session_id,
                "message": message,
                "source": source,
                "completion_context": completion_context,
            }
        )
        turn_started.set()
        # Deliberately never call completion_acceptance: this models a turn
        # start that never reaches durable completion incorporation.
        return {"stream_id": "parked", "_status": 200}

    monkeypatch.setattr(routes, "start_session_turn", start_without_incorporation)
    dispatch_attempts = []
    real_dispatch = bp._start_server_side_wakeup_turn

    def fail_once_then_dispatch(*args, **kwargs):
        dispatch_attempts.append(None)
        if len(dispatch_attempts) == 1:
            raise RuntimeError("synthetic thread-start failure")
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", fail_once_then_dispatch)
    evt = {
        "type": "completion",
        "session_id": process_id,
        "session_key": session_id,
        "origin_ui_session_id": session_id,
        "command": "sleep 1",
        "exit_code": 0,
        "output": "done",
    }

    bp.register_process_session(session_id, session_id)
    try:
        # A synchronous dispatch failure owns no turn. Its requeued event must
        # be able to reclaim the pre-incorporation gate exactly once.
        bp._process_one(dict(evt))
        bp._process_one(dict(evt))
        assert turn_started.wait(timeout=2)

        # PENDING is absent on the idle path. Exercise the real reaper to prove
        # it cannot erase a pre-incorporation gate merely because no durable
        # ACK was ever produced.
        assert bp.start_session_channel_reaper() is True
        time.sleep(0.05)
        bp.stop_session_channel_reaper()

        bp._process_one(dict(evt))
        time.sleep(0.05)

        assert len(calls) == 1
        assert len(dispatch_attempts) == 2
        assert calls[0]["completion_context"].completion_key == f"process:{process_id}"
        assert fake_registry.is_completion_consumed(process_id) is False
    finally:
        bp.stop_session_channel_reaper()
        bp.unregister_process_session(session_id)
        bp.forget_bg_task_completion_dedup(session_id)


def test_nonretryable_wakeup_response_defers_until_one_recovery(monkeypatch):
    """A rejected attempt keeps one deferred owner instead of a local black hole."""
    process_id = "proc-nonretryable-recovery"
    session_id = "session-nonretryable-recovery"
    attempts = []
    first_attempt = threading.Event()
    recovered_attempt = threading.Event()

    fake_registry = FakeProcessRegistry()
    fake_registry.register(process_id, session_id)
    install_fake_registry(monkeypatch, fake_registry)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(
        bp,
        "_resolve_session_lineage",
        lambda _sid: SimpleNamespace(
            root_session_id=session_id,
            delivery_session_id=session_id,
            profile="default",
        ),
    )
    monkeypatch.setattr(
        bp,
        "_emit_bg_task_complete_events_coalesced",
        lambda *_args, **_kwargs: 0,
    )

    def reject_then_accept(
        _target_session_id,
        _message,
        *,
        source,
        completion_context,
        completion_acceptance,
    ):
        attempts.append((source, completion_context.completion_key))
        if len(attempts) == 1:
            first_attempt.set()
            return {"_status": 500, "error": "workspace unavailable"}
        completion_acceptance()
        recovered_attempt.set()
        return {"_status": 200, "stream_id": "recovered"}

    monkeypatch.setattr(routes, "start_session_turn", reject_then_accept)
    event = {
        "type": "completion",
        "session_id": process_id,
        "session_key": session_id,
        "origin_ui_session_id": session_id,
        "command": "true",
        "exit_code": 0,
        "output": "done",
    }

    bp.register_process_session(session_id, session_id)
    try:
        bp._process_one(dict(event))
        assert first_attempt.wait(timeout=2)
        assert _wait_until(
            lambda: len(config.DEFERRED_PROCESS_WAKEUPS.get(session_id, [])) == 1
        )
        assert fake_registry.is_completion_consumed(process_id) is False
        assert process_id not in bp._PREINCORPORATION_COMPLETION_IDS

        # The deferred owner, not another queue copy, suppresses a duplicate.
        bp._process_one(dict(event))
        time.sleep(0.05)
        assert len(attempts) == 1
        assert fake_registry.completion_queue.empty()

        assert bp.drain_deferred_wakeups_for_session(session_id) == 1
        assert recovered_attempt.wait(timeout=2)
        assert fake_registry.is_completion_consumed(process_id) is True
        assert process_id not in bp._PREINCORPORATION_COMPLETION_IDS
        assert bp.drain_deferred_wakeups_for_session(session_id) == 0

        bp._process_one(dict(event))
        time.sleep(0.05)
        assert attempts == [
            ("process_wakeup", f"process:{process_id}"),
            ("process_wakeup", f"process:{process_id}"),
        ]
    finally:
        bp.unregister_process_session(session_id)
        bp.forget_bg_task_completion_dedup(session_id)
        config.PENDING_BG_TASK_COMPLETIONS.discard(session_id)
        with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
            config.DEFERRED_PROCESS_WAKEUPS.pop(session_id, None)


def test_concurrent_duplicates_are_suppressed_while_wakeup_is_in_flight(monkeypatch):
    process_id = "proc-concurrent-inflight"
    session_id = "session-concurrent-inflight"
    fake_registry, event = _install_local_completion_case(
        monkeypatch, session_id, process_id
    )
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def blocking_start(
        _target_session_id,
        _message,
        *,
        source,
        completion_context,
        completion_acceptance,
    ):
        calls.append((source, completion_context.completion_key))
        entered.set()
        assert release.wait(timeout=2)
        completion_acceptance()
        return {"_status": 200, "stream_id": "accepted"}

    monkeypatch.setattr(routes, "start_session_turn", blocking_start)
    try:
        bp._process_one(dict(event))
        assert entered.wait(timeout=2)

        barrier = threading.Barrier(3)
        errors = []

        def deliver_duplicate():
            try:
                barrier.wait(timeout=2)
                bp._process_one(dict(event))
            except BaseException as exc:
                errors.append(exc)

        duplicates = [threading.Thread(target=deliver_duplicate) for _ in range(2)]
        for duplicate in duplicates:
            duplicate.start()
        barrier.wait(timeout=2)
        for duplicate in duplicates:
            duplicate.join(timeout=2)

        assert errors == []
        assert len(calls) == 1
        release.set()
        assert _wait_until(lambda: fake_registry.is_completion_consumed(process_id))
        assert len(calls) == 1
    finally:
        release.set()
        _cleanup_local_completion_case(session_id, process_id)


def test_duplicate_after_durable_incorporation_starts_no_wakeup(monkeypatch):
    process_id = "proc-incorporated-no-replay"
    session_id = "session-incorporated-no-replay"
    fake_registry, event = _install_local_completion_case(
        monkeypatch, session_id, process_id
    )
    calls = []
    accepted = threading.Event()

    def accepting_start(
        _target_session_id,
        _message,
        *,
        source,
        completion_context,
        completion_acceptance,
    ):
        calls.append((source, completion_context.completion_key))
        completion_acceptance()
        accepted.set()
        return {"_status": 200, "stream_id": "accepted"}

    monkeypatch.setattr(routes, "start_session_turn", accepting_start)
    try:
        bp._process_one(dict(event))
        assert accepted.wait(timeout=2)
        assert fake_registry.is_completion_consumed(process_id) is True

        for _ in range(3):
            bp._process_one(dict(event))
        time.sleep(0.05)

        assert calls == [("process_wakeup", f"process:{process_id}")]
        assert fake_registry.completion_queue.empty()
    finally:
        _cleanup_local_completion_case(session_id, process_id)


def test_wakeup_exception_releases_attempt_into_one_deferred_owner(monkeypatch):
    process_id = "proc-exception-recovery"
    session_id = "session-exception-recovery"
    fake_registry, event = _install_local_completion_case(
        monkeypatch, session_id, process_id
    )
    attempts = []
    first_attempt = threading.Event()
    recovered_attempt = threading.Event()

    def raise_then_accept(
        _target_session_id,
        _message,
        *,
        source,
        completion_context,
        completion_acceptance,
    ):
        attempts.append((source, completion_context.completion_key))
        if len(attempts) == 1:
            first_attempt.set()
            raise RuntimeError("synchronous turn-start failure")
        completion_acceptance()
        recovered_attempt.set()
        return {"_status": 200, "stream_id": "recovered"}

    monkeypatch.setattr(routes, "start_session_turn", raise_then_accept)
    try:
        bp._process_one(dict(event))
        assert first_attempt.wait(timeout=2)
        assert _wait_until(
            lambda: len(config.DEFERRED_PROCESS_WAKEUPS.get(session_id, [])) == 1
        )
        assert process_id not in bp._PREINCORPORATION_COMPLETION_IDS
        assert config.BG_TASK_COMPLETE_EVENTS_SEEN[session_id] == {process_id}
        assert fake_registry.is_completion_consumed(process_id) is False
        assert fake_registry.completion_queue.empty()

        bp._process_one(dict(event))
        time.sleep(0.05)
        assert len(attempts) == 1

        assert bp.drain_deferred_wakeups_for_session(session_id) == 1
        assert recovered_attempt.wait(timeout=2)
        assert fake_registry.is_completion_consumed(process_id) is True
        assert bp.drain_deferred_wakeups_for_session(session_id) == 0
        assert len(attempts) == 2
    finally:
        _cleanup_local_completion_case(session_id, process_id)


def test_repeated_nonretryable_response_has_one_deferred_owner_and_no_spin(monkeypatch):
    process_id = "proc-deterministic-500"
    session_id = "session-deterministic-500"
    fake_registry, event = _install_local_completion_case(
        monkeypatch, session_id, process_id
    )
    attempts = []

    def always_reject(*_args, **_kwargs):
        attempts.append(None)
        return {"_status": 500, "error": "deterministic rejection"}

    monkeypatch.setattr(routes, "start_session_turn", always_reject)
    try:
        bp._process_one(dict(event))
        assert _wait_until(lambda: len(attempts) == 1)
        assert _wait_until(
            lambda: len(config.DEFERRED_PROCESS_WAKEUPS.get(session_id, [])) == 1
        )
        assert process_id not in bp._PREINCORPORATION_COMPLETION_IDS

        for _ in range(5):
            bp._process_one(dict(event))
        time.sleep(0.1)
        assert len(attempts) == 1
        assert len(config.DEFERRED_PROCESS_WAKEUPS[session_id]) == 1
        assert fake_registry.completion_queue.empty()

        # A deliberate recovery that gets the same response returns to the
        # same single deferred owner; it does not self-retry.
        assert bp.drain_deferred_wakeups_for_session(session_id) == 1
        assert _wait_until(lambda: len(attempts) == 2)
        assert _wait_until(
            lambda: len(config.DEFERRED_PROCESS_WAKEUPS.get(session_id, [])) == 1
        )
        time.sleep(0.1)
        assert len(attempts) == 2
        assert len(config.DEFERRED_PROCESS_WAKEUPS[session_id]) == 1
        assert fake_registry.completion_queue.empty()
    finally:
        _cleanup_local_completion_case(session_id, process_id)


def test_failed_deferred_record_releases_claim_and_requeues_once(monkeypatch):
    process_id = "proc-defer-fallback"
    session_id = "session-defer-fallback"
    fake_registry, event = _install_local_completion_case(
        monkeypatch, session_id, process_id
    )
    attempted = threading.Event()

    def rejecting_start(*_args, **_kwargs):
        attempted.set()
        return {"_status": 500, "error": "rejected before defer fallback"}

    monkeypatch.setattr(routes, "start_session_turn", rejecting_start)
    monkeypatch.setattr(bp, "record_deferred_wakeup", lambda *_args, **_kwargs: False)
    try:
        bp._process_one(dict(event))
        assert attempted.wait(timeout=2)
        assert _wait_until(lambda: fake_registry.completion_queue.qsize() == 1)
        assert fake_registry.completion_queue.get_nowait() == event
        assert fake_registry.completion_queue.empty()
        assert process_id not in bp._PREINCORPORATION_COMPLETION_IDS
        assert process_id not in config.BG_TASK_COMPLETE_EVENTS_SEEN.get(session_id, set())
        assert fake_registry.is_completion_consumed(process_id) is False
        assert config.DEFERRED_PROCESS_WAKEUPS.get(session_id) is None
    finally:
        _cleanup_local_completion_case(session_id, process_id)


def _configure_completion_recovery(monkeypatch, session_dir):
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)
    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()


def _completion_context(session_id, process_id, session_dir):
    return build_completion_delivery_context(
        {
            "type": "process_complete",
            "process_id": process_id,
            "session_key": f"ui:{session_id}",
            "origin_ui_session_id": session_id,
            "origin_profile": "default",
        },
        session_id,
        session_dir=session_dir,
    )


def _persist_accepted_prompt(session, context, prompt):
    session.active_stream_id = context.correlation_id[:32]
    session.pending_user_message = prompt
    session.pending_attachments = []
    session.pending_started_at = 123.0
    session.pending_user_source = "process_wakeup"
    session.pending_turn_id = context.turn_id
    session.pending_completion_key = context.completion_key
    session.pending_completion_correlation_sha256 = context.correlation_id
    session.save()


def _install_counting_completion_core(
    monkeypatch,
    calls,
    finished,
    *,
    attachment_calls=None,
):
    """Run admission normally but replace provider execution with one durable turn."""
    from api import streaming

    def counting_core(
        session_id,
        msg_text,
        _model,
        _workspace,
        stream_id,
        _attachments=None,
        **_kwargs,
    ):
        calls.append((session_id, msg_text, stream_id))
        if attachment_calls is not None:
            attachment_calls.append(list(_attachments or []))
        current = Session.load(session_id)
        assert current is not None
        setattr(current, "active_stream_id", None)
        setattr(current, "pending_user_message", None)
        current.pending_attachments = []
        current.pending_started_at = None
        setattr(current, "pending_user_source", None)
        current.save()
        with config.LOCK:
            config.SESSIONS[session_id] = current
        with routes.STREAMS_LOCK:
            routes.STREAMS.pop(stream_id, None)
        finished.set()
        return {"executed": True}

    monkeypatch.setattr(streaming, "_run_agent_streaming_core", counting_core)


def _leave_completion_pending_after_acceptance_failure(
    monkeypatch,
    session,
    context,
    *,
    prompt,
    attachments=None,
):
    """Drive the production pre-gate failure without leaving a real worker thread."""
    captured = {}

    class ParkedThread:
        def __init__(self, *_args, **kwargs):
            captured["admission"] = kwargs["kwargs"]["admission"]

        def start(self):
            captured["admission"].admitted.set()

    with monkeypatch.context() as isolated:
        isolated.setattr(routes.threading, "Thread", ParkedThread)
        response = routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            attachments=list(attachments or []),
            workspace=str(models.SESSION_DIR),
            model="test-model",
            model_provider="test-provider",
            source="process_wakeup",
            external_runtime_owned=False,
            completion_context=context,
            completion_acceptance=lambda: (_ for _ in ()).throw(
                OSError("synthetic acceptance failure")
            ),
        )

    assert response["_status"] == 503
    assert response["type"] == "completion_incorporation_failed"
    return captured["admission"]


def test_incorporated_acceptance_exception_retries_and_executes_once(monkeypatch, tmp_path):
    """C5: incorporation is not terminal until the parked worker actually executes."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    session = Session(session_id="acceptance-retry", title="acceptance-retry", profile="default")
    session.save()
    context = _completion_context("acceptance-retry", "proc-acceptance-retry", tmp_path)
    executions = []
    finished = threading.Event()
    _install_counting_completion_core(monkeypatch, executions, finished)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)

    def fail_acceptance():
        raise OSError("synthetic acceptance failure")

    first = routes._start_chat_stream_for_session(
        session,
        msg="resume this completion",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        source="process_wakeup",
        external_runtime_owned=False,
        completion_context=context,
        completion_acceptance=fail_acceptance,
    )
    assert first["_status"] == 503
    assert first["type"] == "completion_incorporation_failed"
    assert executions == []
    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None and receipt["state"] == "incorporated"

    accepted = []
    second = routes._start_chat_stream_for_session(
        session,
        msg="resume this completion",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        source="process_wakeup",
        external_runtime_owned=False,
        completion_context=context,
        completion_acceptance=lambda: accepted.append("accepted"),
    )
    assert int(second.get("_status") or 200) == 200
    assert second.get("completion_duplicate") is not True
    assert finished.wait(timeout=2)
    assert executions == [
        ("acceptance-retry", "resume this completion", context.correlation_id[:32])
    ]
    assert accepted == ["accepted"]
    assert _wait_until(
        lambda: (
            read_completion_delivery_receipt(context, session_dir=tmp_path) or {}
        ).get("execution_state")
        == "delivered"
    )

    replay = routes._start_chat_stream_for_session(
        session,
        msg="resume this completion",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        source="process_wakeup",
        external_runtime_owned=False,
        completion_context=context,
        completion_acceptance=lambda: accepted.append("duplicate"),
    )
    assert replay["_status"] == 200
    assert replay["completion_duplicate"] is True
    assert executions == [
        ("acceptance-retry", "resume this completion", context.correlation_id[:32])
    ]
    assert accepted == ["accepted", "duplicate"]
    document = json.loads(
        (tmp_path / "_completion_delivery_receipts.json").read_text(encoding="utf-8")
    )
    assert list(document["receipts"]) == [context.completion_key]
    persisted = json.loads((tmp_path / "acceptance-retry.json").read_text(encoding="utf-8"))
    assert len(
        [
            row
            for row in persisted["context_messages"]
            if (row.get("_completion_delivery") or {}).get("completion_key")
            == context.completion_key
        ]
    ) == 1


def test_crash_boundaries_before_incorporation_recover_once(monkeypatch, tmp_path):
    _configure_completion_recovery(monkeypatch, tmp_path)
    session = Session(session_id="recover", title="recover", profile="default")
    session.save()
    context = _completion_context("recover", "proc-recover", tmp_path)
    claim = claim_completion_delivery(context, session_dir=tmp_path)
    assert claim is not None
    release_completion_delivery_claim(claim)
    _persist_accepted_prompt(session, context, "recovered result")

    class ForbiddenThread:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("accepted-only recovery must not execute a worker")

    monkeypatch.setattr(routes.threading, "Thread", ForbiddenThread)
    assert routes.recover_accepted_completion_delivery(context) is True
    assert routes.recover_accepted_completion_delivery(context) is False

    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None and receipt["state"] == "incorporated"
    persisted = json.loads((tmp_path / "recover.json").read_text(encoding="utf-8"))
    metadata = persisted["messages"][0]["_completion_delivery"]
    assert len(
        [row for row in persisted["messages"] if row.get("_completion_delivery") == metadata]
    ) == 1
    assert len(
        [row for row in persisted["context_messages"] if row.get("_completion_delivery") == metadata]
    ) == 1
    assert len(
        [
            row
            for row in read_turn_journal("recover", session_dir=tmp_path)["events"]
            if row.get("_completion_delivery") == metadata
        ]
    ) == 1


def test_cas_before_execution_gate_never_executes_accepted_only(monkeypatch, tmp_path):
    _configure_completion_recovery(monkeypatch, tmp_path)
    session = Session(session_id="gate", title="gate", profile="default")
    session.save()
    context = _completion_context("gate", "proc-gate", tmp_path)
    captured = {}

    class ParkedThread:
        def __init__(self, *args, **kwargs):
            captured["admission"] = kwargs["kwargs"]["admission"]

        def start(self):
            captured["admission"].admitted.set()

    monkeypatch.setattr(routes.threading, "Thread", ParkedThread)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "api.session_lineage.mark_completion_incorporated",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("crash before CAS")),
    )

    response = routes._start_chat_stream_for_session(
        session,
        msg="accepted only",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        source="process_wakeup",
        external_runtime_owned=False,
        completion_context=context,
    )

    assert response["type"] == "completion_incorporation_failed"
    assert response["_status"] == 503
    assert captured["admission"].admitted.is_set()
    assert captured["admission"].abort.is_set()
    assert captured["admission"].gate.is_set()
    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None and receipt["state"] == "accepted"


def test_incorporated_restart_resumes_exact_turn_once(monkeypatch, tmp_path):
    """C5: fresh-process repair must execute a durable ungated checkpoint once."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    session = Session(session_id="incorporated", title="incorporated", profile="default")
    session.save()
    context = _completion_context("incorporated", "proc-incorporated", tmp_path)
    stream_id = context.correlation_id[:32]
    admission = routes._reserve_turn_admission(session, stream_id)
    claim = claim_completion_delivery(
        context,
        session_dir=tmp_path,
        reservation_id=stream_id,
    )
    assert claim is not None
    prepared = routes._prepare_chat_start_session_for_stream(
        session,
        msg="resume after restart",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        stream_id=stream_id,
        source="process_wakeup",
        admission=admission,
        defer_journal=True,
        completion_context=context,
    )
    prepared = routes._append_prepared_chat_turn_journal(
        session,
        prepared,
        msg="resume after restart",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
    )
    admission.admitted.set()
    verify_completion_incorporation_artifacts(
        claim,
        turn_admission=admission,
        message="resume after restart",
        session_dir=tmp_path,
    )
    mark_completion_incorporated(
        claim,
        session_dir=tmp_path,
        execution_pending=True,
    )
    release_completion_delivery_claim(claim)
    release_turn_admission(admission)

    # Process exit drops only in-memory owners. Startup repair must rely on the
    # sidecar, journal, checkpoint, and incorporated receipt just read from disk.
    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    executions = []
    finished = threading.Event()
    _install_counting_completion_core(monkeypatch, executions, finished)

    class RecoveryRegistry(FakeProcessRegistry):
        def recover_from_checkpoint(self):
            return 0

        def list_sessions(self):
            return []

    registry = RecoveryRegistry()
    install_fake_registry(monkeypatch, registry)
    candidates = pending_completion_delivery_contexts(session_dir=tmp_path)
    assert [candidate.completion_key for candidate in candidates] == [
        context.completion_key
    ]

    assert bp.recover_processes_for_webui(
        registry,
        lambda *_args, **_kwargs: None,
    ) == 0
    assert finished.wait(timeout=2)
    assert executions == [("incorporated", "resume after restart", stream_id)]
    assert registry.is_completion_consumed("proc-incorporated") is True
    assert _wait_until(
        lambda: (
            read_completion_delivery_receipt(context, session_dir=tmp_path) or {}
        ).get("execution_state")
        == "delivered"
    )
    assert _wait_until(lambda: not config.ACTIVE_RUNS)
    with config.LOCK:
        config.SESSIONS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    assert bp.recover_processes_for_webui(
        registry,
        lambda *_args, **_kwargs: None,
    ) == 0
    assert executions == [("incorporated", "resume after restart", stream_id)]
    document = json.loads(
        (tmp_path / "_completion_delivery_receipts.json").read_text(encoding="utf-8")
    )
    assert list(document["receipts"]) == [context.completion_key]


def test_acceptance_failure_restart_preserves_and_executes_exact_turn_once(
    monkeypatch,
    tmp_path,
):
    """F1-A: abort live owners without deleting an incorporated replay source."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    session = Session(session_id="acceptance-restart", title="restart", profile="default")
    session.save()
    context = _completion_context(
        "acceptance-restart",
        "proc-acceptance-restart",
        tmp_path,
    )
    prompt = "resume this exact completion after restart"
    attachments = [{"name": "result.txt", "path": "/tmp/result.txt"}]
    captured = []
    original_reserve = routes._reserve_turn_admission

    def capture_reservation(*args, **kwargs):
        admission = original_reserve(*args, **kwargs)
        captured.append(admission)
        return admission

    monkeypatch.setattr(routes, "_reserve_turn_admission", capture_reservation)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)

    response = routes._start_chat_stream_for_session(
        session,
        msg=prompt,
        attachments=attachments,
        workspace=str(tmp_path),
        model="test-model",
        model_provider="test-provider",
        source="process_wakeup",
        external_runtime_owned=False,
        completion_context=context,
        completion_acceptance=lambda: (_ for _ in ()).throw(
            OSError("synthetic acceptance failure")
        ),
    )

    assert response["_status"] == 503
    assert response["type"] == "completion_incorporation_failed"
    assert len(captured) == 1
    failed_admission = captured[0]
    assert failed_admission.abort.is_set()
    assert failed_admission.gate.is_set()
    assert failed_admission.permit.acquired is False
    assert _wait_until(lambda: not config.ACTIVE_RUNS)
    with routes.STREAMS_LOCK:
        assert context.correlation_id[:32] not in routes.STREAMS

    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None
    assert (receipt["state"], receipt["execution_state"]) == (
        "incorporated",
        "pending",
    )
    persisted = json.loads(
        (tmp_path / "acceptance-restart.json").read_text(encoding="utf-8")
    )
    assert persisted["active_stream_id"] is None
    assert persisted["pending_user_message"] == prompt
    assert persisted["pending_attachments"] == attachments
    assert persisted["pending_turn_id"] == context.turn_id
    assert persisted["pending_completion_key"] == context.completion_key
    assert (
        persisted["pending_completion_correlation_sha256"]
        == context.correlation_id
    )
    submitted = [
        event
        for event in read_turn_journal(
            "acceptance-restart",
            session_dir=tmp_path,
        )["events"]
        if event.get("event") == "submitted"
    ]
    assert len(submitted) == 1
    assert submitted[0]["content"] == prompt
    assert submitted[0]["attachments"] == attachments
    assert submitted[0]["completion_key"] == context.completion_key

    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    executions = []
    replayed_attachments = []
    finished = threading.Event()
    _install_counting_completion_core(
        monkeypatch,
        executions,
        finished,
        attachment_calls=replayed_attachments,
    )

    class RecoveryRegistry(FakeProcessRegistry):
        def recover_from_checkpoint(self):
            return 0

        def list_sessions(self):
            return []

    registry = RecoveryRegistry()
    install_fake_registry(monkeypatch, registry)

    assert bp.recover_processes_for_webui(
        registry,
        lambda *_args, **_kwargs: None,
    ) == 0
    assert finished.wait(timeout=2)
    assert executions == [
        (
            "acceptance-restart",
            prompt,
            context.correlation_id[:32],
        )
    ]
    assert replayed_attachments == [attachments]
    assert _wait_until(
        lambda: (
            read_completion_delivery_receipt(context, session_dir=tmp_path) or {}
        ).get("execution_state")
        == "delivered"
    )

    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    assert bp.recover_processes_for_webui(
        registry,
        lambda *_args, **_kwargs: None,
    ) == 0
    assert executions == [
        (
            "acceptance-restart",
            prompt,
            context.correlation_id[:32],
        )
    ]


def test_moved_pending_receipt_is_quarantined_without_starving_healthy_restart(
    monkeypatch,
    tmp_path,
    caplog,
):
    """F1-B: one poison receipt cannot abort a distinct healthy pending turn."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)

    stale = Session(session_id="poison-root", title="poison", profile="default")
    stale.save()
    stale_context = _completion_context(
        "poison-root",
        "proc-poison",
        tmp_path,
    )
    stale_admission = _leave_completion_pending_after_acceptance_failure(
        monkeypatch,
        stale,
        stale_context,
        prompt="poison prompt must never execute",
    )
    assert stale_admission.permit.acquired is False
    stale_payload = json.loads(
        (tmp_path / "poison-root.json").read_text(encoding="utf-8")
    )
    stale_payload["pre_compression_snapshot"] = True
    (tmp_path / "poison-root.json").write_text(
        json.dumps(stale_payload),
        encoding="utf-8",
    )
    continuation_payload = dict(stale_payload)
    continuation_payload["session_id"] = "poison-tip"
    continuation_payload["parent_session_id"] = "poison-root"
    continuation_payload["pre_compression_snapshot"] = False
    (tmp_path / "poison-tip.json").write_text(
        json.dumps(continuation_payload),
        encoding="utf-8",
    )
    record_lineage_transition(
        root_session_id="poison-root",
        previous_tip_session_id="poison-root",
        delivery_session_id="poison-tip",
        profile="default",
        state="committed",
        session_dir=tmp_path,
    )

    healthy = Session(session_id="healthy-root", title="healthy", profile="default")
    healthy.save()
    healthy_context = _completion_context(
        "healthy-root",
        "proc-healthy",
        tmp_path,
    )
    healthy_admission = _leave_completion_pending_after_acceptance_failure(
        monkeypatch,
        healthy,
        healthy_context,
        prompt="healthy prompt executes exactly once",
    )
    assert healthy_admission.permit.acquired is False

    executions = []
    finished = threading.Event()
    _install_counting_completion_core(monkeypatch, executions, finished)

    class RecoveryRegistry(FakeProcessRegistry):
        def recover_from_checkpoint(self):
            return 0

        def list_sessions(self):
            return []

    registry = RecoveryRegistry()
    install_fake_registry(monkeypatch, registry)
    first_diagnostic = None
    for round_index in range(4):
        with config.LOCK:
            config.SESSIONS.clear()
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
        with routes.STREAMS_LOCK:
            routes.STREAMS.clear()
        monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
        monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)

        assert bp.recover_processes_for_webui(
            registry,
            lambda *_args, **_kwargs: None,
        ) == 0
        assert bp._PROCESS_RECOVERY_DONE is True
        if round_index == 0:
            assert finished.wait(timeout=2)
            assert _wait_until(
                lambda: (
                    read_completion_delivery_receipt(
                        healthy_context,
                        session_dir=tmp_path,
                    )
                    or {}
                ).get("execution_state")
                == "delivered"
            )
        document = json.loads(
            (tmp_path / "_completion_delivery_receipts.json").read_text(
                encoding="utf-8"
            )
        )
        diagnostic = document["receipts"][stale_context.completion_key].get(
            "restart_diagnostic"
        )
        assert diagnostic is not None
        assert diagnostic["code"] == "delivery_tip_moved"
        if first_diagnostic is None:
            first_diagnostic = diagnostic
        else:
            assert diagnostic == first_diagnostic

    assert executions == [
        (
            "healthy-root",
            "healthy prompt executes exactly once",
            healthy_context.correlation_id[:32],
        )
    ]
    stale_receipt = read_completion_delivery_receipt(
        stale_context,
        session_dir=tmp_path,
    )
    assert stale_receipt is not None
    assert (stale_receipt["state"], stale_receipt["execution_state"]) == (
        "incorporated",
        "pending",
    )
    assert "poison prompt must never execute" not in [row[1] for row in executions]
    assert any(
        stale_context.completion_key in record.getMessage()
        and "quarantined" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "corruption",
    ["malformed", "foreign_identity", "nonobject"],
)
def test_poison_pending_receipt_does_not_starve_healthy_restart(
    monkeypatch,
    tmp_path,
    caplog,
    corruption,
):
    """T2: isolate one bad receipt row while an unrelated turn executes once."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)

    poison = Session(session_id="poison-row", title="poison", profile="default")
    poison.save()
    poison_context = _completion_context(
        "poison-row",
        f"proc-poison-{corruption}",
        tmp_path,
    )
    _leave_completion_pending_after_acceptance_failure(
        monkeypatch,
        poison,
        poison_context,
        prompt="poison row must never execute",
    )

    healthy = Session(session_id="healthy-row", title="healthy", profile="default")
    healthy.save()
    healthy_context = _completion_context(
        "healthy-row",
        f"proc-healthy-{corruption}",
        tmp_path,
    )
    _leave_completion_pending_after_acceptance_failure(
        monkeypatch,
        healthy,
        healthy_context,
        prompt="healthy row executes exactly once",
    )

    receipt_path = tmp_path / "_completion_delivery_receipts.json"
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    poison_record = document["receipts"][poison_context.completion_key]
    if corruption == "malformed":
        poison_record["attempt"] = "not-an-integer"
    elif corruption == "foreign_identity":
        poison_record["completion_id"] = "foreign-completion-id"
    else:
        document["receipts"][poison_context.completion_key] = None
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    executions = []
    finished = threading.Event()
    _install_counting_completion_core(monkeypatch, executions, finished)

    class RecoveryRegistry(FakeProcessRegistry):
        def recover_from_checkpoint(self):
            return 0

        def list_sessions(self):
            return []

    registry = RecoveryRegistry()
    install_fake_registry(monkeypatch, registry)
    first_restart_document = None
    for restart_index in range(3):
        with config.LOCK:
            config.SESSIONS.clear()
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
        with routes.STREAMS_LOCK:
            routes.STREAMS.clear()
        monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
        monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)

        assert bp.recover_processes_for_webui(
            registry,
            lambda *_args, **_kwargs: None,
        ) == 0
        assert bp._PROCESS_RECOVERY_DONE is True
        if restart_index == 0:
            assert finished.wait(timeout=2)
            assert _wait_until(
                lambda: (
                    read_completion_delivery_receipt(
                        healthy_context,
                        session_dir=tmp_path,
                    )
                    or {}
                ).get("execution_state")
                == "delivered"
            )
        durable_document = json.loads(receipt_path.read_text(encoding="utf-8"))
        if first_restart_document is None:
            first_restart_document = durable_document
        else:
            assert durable_document == first_restart_document

    assert executions == [
        (
            "healthy-row",
            "healthy row executes exactly once",
            healthy_context.correlation_id[:32],
        )
    ]
    assert first_restart_document is not None
    durable_poison = first_restart_document["receipts"][
        poison_context.completion_key
    ]
    if corruption == "nonobject":
        assert durable_poison is None
    else:
        assert (durable_poison["state"], durable_poison["execution_state"]) == (
            "incorporated",
            "pending",
        )
        assert durable_poison["restart_diagnostic"]["code"] == "receipt_invalid"
    assert any(
        poison_context.completion_key in record.getMessage()
        and "quarantined" in record.getMessage()
        for record in caplog.records
    )


def test_pending_resume_rejects_tip_rotation_before_mutating_artifacts(
    monkeypatch,
    tmp_path,
):
    """T3: snapshot->lock->re-resolve rejects a moved tip without partial CAS."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    session = Session(session_id="rotation-root", title="root", profile="default")
    session.save()
    context = _completion_context(
        "rotation-root",
        "proc-tip-rotation",
        tmp_path,
    )
    _leave_completion_pending_after_acceptance_failure(
        monkeypatch,
        session,
        context,
        prompt="old tip replay must remain pending",
    )
    [enumerated] = pending_completion_delivery_contexts(session_dir=tmp_path)
    assert enumerated.delivery_session_id == "rotation-root"

    root_path = tmp_path / "rotation-root.json"
    root_payload = json.loads(root_path.read_text(encoding="utf-8"))
    root_payload["pre_compression_snapshot"] = True
    root_path.write_text(json.dumps(root_payload), encoding="utf-8")
    tip_payload = dict(root_payload)
    tip_payload["session_id"] = "rotation-tip"
    tip_payload["parent_session_id"] = "rotation-root"
    tip_payload["pre_compression_snapshot"] = False
    tip_path = tmp_path / "rotation-tip.json"
    tip_path.write_text(json.dumps(tip_payload), encoding="utf-8")
    record_lineage_transition(
        root_session_id="rotation-root",
        previous_tip_session_id="rotation-root",
        delivery_session_id="rotation-tip",
        profile="default",
        state="committed",
        session_dir=tmp_path,
    )
    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()

    receipt_path = tmp_path / "_completion_delivery_receipts.json"
    before = {
        "receipt": receipt_path.read_bytes(),
        "root": root_path.read_bytes(),
        "tip": tip_path.read_bytes(),
    }
    with pytest.raises(CompletionDeliveryRestartError) as raised:
        routes.recover_incorporated_completion_delivery(enumerated)
    assert raised.value.diagnostic_code == "delivery_tip_moved"
    assert receipt_path.read_bytes() == before["receipt"]
    assert root_path.read_bytes() == before["root"]
    assert tip_path.read_bytes() == before["tip"]

    diagnostics = []
    assert pending_completion_delivery_contexts(
        session_dir=tmp_path,
        diagnostics=diagnostics,
    ) == []
    assert diagnostics == [
        {
            "completion_key": context.completion_key,
            "code": "delivery_tip_moved",
            "newly_recorded": True,
        }
    ]
    diagnosed = receipt_path.read_bytes()
    repeated_diagnostics = []
    assert pending_completion_delivery_contexts(
        session_dir=tmp_path,
        diagnostics=repeated_diagnostics,
    ) == []
    assert repeated_diagnostics[0]["newly_recorded"] is False
    assert receipt_path.read_bytes() == diagnosed


@pytest.mark.parametrize("failure_boundary", ["journal", "thread_start"])
def test_pending_resume_pre_gate_failure_preserves_next_restart(
    monkeypatch,
    tmp_path,
    failure_boundary,
):
    """T3: a reclaimed pending receipt survives every owned pre-gate abort."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    session = Session(session_id="resume-abort", title="resume", profile="default")
    session.save()
    context = _completion_context(
        "resume-abort",
        f"proc-resume-{failure_boundary}",
        tmp_path,
    )
    prompt = f"resume after {failure_boundary} failure"
    attachments = [{"name": "checkpoint.txt", "path": "/tmp/checkpoint.txt"}]
    _leave_completion_pending_after_acceptance_failure(
        monkeypatch,
        session,
        context,
        prompt=prompt,
        attachments=attachments,
    )
    [candidate] = pending_completion_delivery_contexts(session_dir=tmp_path)

    with monkeypatch.context() as failure:
        if failure_boundary == "journal":
            failure.setattr(
                routes,
                "_append_prepared_chat_turn_journal",
                lambda *_a, **_k: (_ for _ in ()).throw(
                    OSError("synthetic restart journal failure")
                ),
            )
            assert routes.recover_incorporated_completion_delivery(candidate) is False
        else:
            class BrokenThread:
                def __init__(self, *_args, **_kwargs):
                    raise OSError("synthetic restart thread failure")

            failure.setattr(routes.threading, "Thread", BrokenThread)
            with pytest.raises(OSError, match="restart thread failure"):
                routes.recover_incorporated_completion_delivery(candidate)

    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None
    assert (receipt["state"], receipt["execution_state"]) == (
        "incorporated",
        "pending",
    )
    persisted = json.loads((tmp_path / "resume-abort.json").read_text(encoding="utf-8"))
    assert persisted["active_stream_id"] is None
    assert persisted["pending_user_message"] == prompt
    assert persisted["pending_attachments"] == attachments
    assert persisted["pending_turn_id"] == context.turn_id
    assert persisted["pending_completion_key"] == context.completion_key
    assert (
        persisted["pending_completion_correlation_sha256"]
        == context.correlation_id
    )

    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
    monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
    executions = []
    finished = threading.Event()
    _install_counting_completion_core(monkeypatch, executions, finished)

    class RecoveryRegistry(FakeProcessRegistry):
        def recover_from_checkpoint(self):
            return 0

        def list_sessions(self):
            return []

    registry = RecoveryRegistry()
    install_fake_registry(monkeypatch, registry)
    assert bp.recover_processes_for_webui(
        registry,
        lambda *_args, **_kwargs: None,
    ) == 0
    assert finished.wait(timeout=2)
    assert executions == [
        ("resume-abort", prompt, context.correlation_id[:32])
    ]
    assert _wait_until(
        lambda: (
            read_completion_delivery_receipt(context, session_dir=tmp_path) or {}
        ).get("execution_state")
        == "delivered"
    )


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("no_prompt", "source_prompt_missing"),
        ("identity_drift", "source_identity_mismatch"),
    ],
)
def test_stale_pending_receipt_diagnostic_is_bounded_across_restarts(
    monkeypatch,
    tmp_path,
    case,
    expected_code,
):
    """F1-C: stale pending rows stay fail-closed with one bounded disposition."""
    _configure_completion_recovery(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    session = Session(session_id="stale-source", title="stale", profile="default")
    session.save()
    context = _completion_context("stale-source", f"proc-{case}", tmp_path)
    _leave_completion_pending_after_acceptance_failure(
        monkeypatch,
        session,
        context,
        prompt="stale prompt",
    )
    persisted = Session.load("stale-source")
    assert persisted is not None
    if case == "no_prompt":
        setattr(persisted, "pending_user_message", None)
        persisted.pending_attachments = []
    else:
        persisted.active_stream_id = "different-stream-owner"
    persisted.save(touch_updated_at=False)

    class RecoveryRegistry(FakeProcessRegistry):
        def recover_from_checkpoint(self):
            return 0

        def list_sessions(self):
            return []

    registry = RecoveryRegistry()
    install_fake_registry(monkeypatch, registry)
    first_document = None
    for _ in range(4):
        with config.LOCK:
            config.SESSIONS.clear()
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
        with routes.STREAMS_LOCK:
            routes.STREAMS.clear()
        monkeypatch.setattr(bp, "_PROCESS_CHECKPOINT_RECOVERED", False)
        monkeypatch.setattr(bp, "_PROCESS_RECOVERY_DONE", False)
        assert bp.recover_processes_for_webui(
            registry,
            lambda *_args, **_kwargs: None,
        ) == 0
        assert bp._PROCESS_RECOVERY_DONE is True
        document = json.loads(
            (tmp_path / "_completion_delivery_receipts.json").read_text(
                encoding="utf-8"
            )
        )
        diagnostic = document["receipts"][context.completion_key].get(
            "restart_diagnostic"
        )
        assert diagnostic is not None
        assert diagnostic["code"] == expected_code
        if first_document is None:
            first_document = document
        else:
            assert document == first_document

    receipt = read_completion_delivery_receipt(context, session_dir=tmp_path)
    assert receipt is not None
    assert (receipt["state"], receipt["execution_state"]) == (
        "incorporated",
        "pending",
    )
