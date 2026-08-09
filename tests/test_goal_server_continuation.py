"""Vertical contract for durable, server-owned /goal continuations."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture()
def continuation_store(tmp_path, monkeypatch):
    from api import goal_continuations as gc

    path = tmp_path / "goal-continuations.json"
    monkeypatch.setattr(gc, "REGISTRY_PATH", path)
    monkeypatch.setattr(gc, "OWNER_ID", "test-owner")
    monkeypatch.setattr(gc, "_REGISTRY", None)
    monkeypatch.setattr(gc, "_WORKER_WAKE", threading.Event())
    monkeypatch.setattr(gc, "_WORKER_THREAD", None)
    monkeypatch.setattr(gc, "_WORKER_LEADER_FD", None, raising=False)
    monkeypatch.setattr(gc, "_WORKER_LEADER_PID", None, raising=False)
    gc._WORKER_STOP.clear()
    yield gc, path
    gc.stop_goal_continuation_worker(timeout=0.2)


def _schedule(gc, sid="session-a", stream="judge-stream-a", turns=1, **kwargs):
    return gc.schedule_goal_continuation(
        sid,
        "continue the approved goal",
        source_stream_id=stream,
        profile_home="/profiles/default",
        goal_turns_used=turns,
        **kwargs,
    )


def test_schedule_is_atomic_durable_and_idempotent(continuation_store):
    gc, path = continuation_store

    first = _schedule(gc)
    duplicate = _schedule(gc)

    assert first["continuation_id"] == duplicate["continuation_id"]
    assert first["status"] == "pending"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text())
    assert list(payload["intents"]) == ["session-a"]
    assert payload["intents"]["session-a"]["prompt"] == "continue the approved goal"


def test_corrupt_registry_is_quarantined_before_new_state_is_written(continuation_store):
    gc, path = continuation_store
    path.write_text("{not-json", encoding="utf-8")
    gc._REGISTRY = None

    _schedule(gc)

    assert json.loads(path.read_text())["intents"]["session-a"]["status"] == "pending"
    quarantined = list(path.parent.glob(f"{path.name}.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{not-json"
    assert quarantined[0].stat().st_mode & 0o777 == 0o600


def test_failed_atomic_replace_does_not_leave_phantom_cached_intent(continuation_store, monkeypatch):
    gc, path = continuation_store
    _schedule(gc)
    original_replace = gc.os.replace

    def fail_registry_replace(src, dst):
        if str(dst) == str(path):
            raise OSError("simulated durable replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr(gc.os, "replace", fail_registry_replace)
    with pytest.raises(OSError, match="simulated durable replace failure"):
        gc.schedule_goal_continuation(
            "session-b",
            "prompt-b",
            source_stream_id="source-b",
            profile_home=None,
            goal_turns_used=1,
        )
    monkeypatch.setattr(gc.os, "replace", original_replace)

    assert gc.get_goal_continuation("session-a") is not None
    assert gc.get_goal_continuation("session-b") is None


def test_drain_starts_exactly_one_server_turn_and_marks_it_running(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)
    calls = []

    def start_turn(session_id, prompt, *, source, continuation_claim_id):
        calls.append((session_id, prompt, source))
        return {"stream_id": "server-stream-1", "_status": 200}

    assert gc.drain_goal_continuations_once(
        start_turn=start_turn,
        is_goal_active=lambda *_args, **_kwargs: True,
        now=time.time() + 1,
    ) == 1
    record = gc.get_goal_continuation("session-a")
    assert calls == [("session-a", "continue the approved goal", "goal_continuation")]
    assert record["status"] == "running"
    assert record["stream_id"] == "server-stream-1"
    assert record["attempts"] == 1


def test_shutdown_stop_refuses_new_default_dispatch_and_releases_claim(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)
    gc._WORKER_STOP.set()

    assert gc.drain_goal_continuations_once(
        is_goal_active=lambda *_args, **_kwargs: True,
    ) == 0
    record = gc.get_goal_continuation("session-a")
    assert record["status"] == "pending"
    assert record["attempts"] == 0
    assert "shutdown" in record["last_error"].lower()


def test_concurrent_drains_cannot_double_start_one_intent(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)
    barrier = threading.Barrier(2)
    calls = []

    def start_turn(session_id, prompt, *, source, continuation_claim_id):
        calls.append((session_id, prompt, source))
        barrier.wait(timeout=2)
        return {"stream_id": "server-stream-1", "_status": 200}

    results = []

    def drain():
        results.append(
            gc.drain_goal_continuations_once(
                start_turn=start_turn,
                is_goal_active=lambda *_args, **_kwargs: True,
                now=time.time() + 1,
            )
        )

    first = threading.Thread(target=drain)
    first.start()
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    second = threading.Thread(target=drain)
    second.start()
    barrier.wait(timeout=2)
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(calls) == 1
    assert sorted(results) == [0, 1]


def test_busy_session_releases_claim_without_consuming_provider_attempt(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)

    assert gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"_status": 409},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=time.time() + 1,
    ) == 0
    record = gc.get_goal_continuation("session-a")
    assert record["status"] == "pending"
    assert record["attempts"] == 0


def test_route_start_failure_after_bind_does_not_consume_provider_attempt(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)

    def failing_start(session_id, _prompt, *, continuation_claim_id, **_kwargs):
        assert gc.bind_goal_continuation_stream(
            session_id,
            "never-started-stream",
            claim_id=continuation_claim_id,
        )
        raise RuntimeError("thread start failed")

    assert gc.drain_goal_continuations_once(
        start_turn=failing_start,
        is_goal_active=lambda *_args, **_kwargs: True,
        now=time.time() + 1,
    ) == 0
    record = gc.get_goal_continuation("session-a")
    assert record["status"] == "pending"
    assert record["attempts"] == 0
    assert record["start_failures"] == 1


def test_bind_requires_current_claim_and_owner(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)
    observed = {}

    def start_turn(session_id, _prompt, *, continuation_claim_id, **_kwargs):
        observed["claim_id"] = continuation_claim_id
        assert gc.bind_goal_continuation_stream(
            session_id,
            "wrong-claim-stream",
            claim_id="stale-claim",
        ) is False
        record = gc.get_goal_continuation(session_id)
        record["owner_id"] = "foreign-owner"
        gc._replace_goal_continuation_for_test(record)
        assert gc.bind_goal_continuation_stream(
            session_id,
            "foreign-owner-stream",
            claim_id=continuation_claim_id,
        ) is False
        record["owner_id"] = gc._current_owner_id()
        gc._replace_goal_continuation_for_test(record)
        assert gc.bind_goal_continuation_stream(
            session_id,
            "owned-stream",
            claim_id=continuation_claim_id,
        ) is True
        return {"stream_id": "owned-stream", "_status": 200}

    assert gc.drain_goal_continuations_once(
        start_turn=start_turn,
        is_goal_active=lambda *_args, **_kwargs: True,
        now=time.time() + 1,
    ) == 1
    assert observed["claim_id"]
    assert gc.get_goal_continuation("session-a")["stream_id"] == "owned-stream"


def test_fast_terminal_settlement_cannot_be_resurrected_by_drain(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)

    def start_turn(session_id, _prompt, *, continuation_claim_id, **_kwargs):
        assert gc.bind_goal_continuation_stream(
            session_id,
            "fast-terminal-stream",
            claim_id=continuation_claim_id,
        )
        assert gc.complete_goal_continuation(session_id, "fast-terminal-stream")
        return {"stream_id": "fast-terminal-stream", "_status": 200}

    assert gc.drain_goal_continuations_once(
        start_turn=start_turn,
        is_goal_active=lambda *_args, **_kwargs: True,
        now=time.time() + 1,
    ) == 1
    assert gc.get_goal_continuation("session-a") is None


def test_mixed_version_browser_can_adopt_only_an_unclaimed_matching_intent(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)

    assert gc.adopt_legacy_browser_goal_stream(
        "session-a", "unrelated-stream", "human override"
    ) is False
    assert gc.get_goal_continuation("session-a")["status"] == "pending"
    assert gc.adopt_legacy_browser_goal_stream(
        "session-a", "browser-stream", "continue the approved goal"
    ) is True
    record = gc.get_goal_continuation("session-a")
    assert record["status"] == "running"
    assert record["stream_id"] == "browser-stream"
    assert record["attempts"] == 1
    assert gc.adopt_legacy_browser_goal_stream(
        "session-a", "duplicate-stream", "continue the approved goal"
    ) is False


def test_unrelated_user_turn_keeps_priority_over_legacy_goal_marker(continuation_store, monkeypatch):
    gc, _path = continuation_store
    from api import routes
    from api.config import PENDING_GOAL_CONTINUATION

    _schedule(gc)
    session = SimpleNamespace(session_id="session-a", active_stream_id=None)
    PENDING_GOAL_CONTINUATION.add(session.session_id)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _session: False)

    def admitted(*_args, **_kwargs):
        raise RuntimeError("ordinary turn reached normal admission")

    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", admitted)

    with pytest.raises(RuntimeError, match="normal admission"):
        routes._start_chat_stream_for_session(
            session,
            msg="human override",
            attachments=[],
            workspace="/tmp",
            model="test-model",
            source="webui",
            external_runtime_owned=False,
        )

    assert gc.get_goal_continuation("session-a")["status"] == "pending"
    assert session.session_id in PENDING_GOAL_CONTINUATION
    assert gc.legacy_browser_goal_prompt_matches("session-a", "human override") is False
    assert gc.legacy_browser_goal_prompt_matches(
        "session-a", "continue the approved goal"
    ) is True


def test_empty_goal_response_is_requeued_with_bounded_backoff(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, max_attempts=3, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "server-stream-1", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )

    assert gc.requeue_goal_continuation_after_no_response(
        "session-a",
        "server-stream-1",
        had_activity=False,
        now=clock + 2,
    ) is True
    record = gc.get_goal_continuation("session-a")
    assert record["status"] == "pending"
    assert record["attempts"] == 1
    assert record["available_at"] > clock + 2


def test_empty_retry_is_refused_after_activity_or_attempt_exhaustion(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, max_attempts=1, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "server-stream-1", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )

    assert gc.requeue_goal_continuation_after_no_response(
        "session-a", "server-stream-1", had_activity=True, now=clock + 2
    ) is False
    assert gc.get_goal_continuation("session-a")["status"] == "failed"

    gc.complete_goal_continuation("session-a")
    _schedule(gc, stream="judge-stream-b", max_attempts=1, now=clock + 3)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "server-stream-2", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 4,
    )
    assert gc.requeue_goal_continuation_after_no_response(
        "session-a", "server-stream-2", had_activity=False, now=clock + 5
    ) is False
    assert gc.get_goal_continuation("session-a")["status"] == "failed"


def test_empty_retry_is_refused_when_cancellation_wins_the_settlement_race(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {
            "stream_id": "cancelled-stream",
            "_status": 200,
        },
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )

    assert gc.requeue_goal_continuation_after_no_response(
        "session-a",
        "cancelled-stream",
        had_activity=False,
        cancellation_check=lambda: True,
        now=clock + 2,
    ) is False
    record = gc.get_goal_continuation("session-a")
    assert record["status"] == "failed"
    assert "cancel" in record["last_error"].lower()


def test_restart_recovers_only_unjudged_active_goal(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, turns=4, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "server-stream-1", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )
    record = gc.get_goal_continuation("session-a")
    record["owner_id"] = "dead-owner"
    gc._replace_goal_continuation_for_test(record)

    assert gc.recover_goal_continuations(
        goal_state_loader=lambda *_args, **_kwargs: {"status": "active", "turns_used": 4},
        run_summary_loader=lambda *_args, **_kwargs: {"terminal_state": "no_response"},
        now=200.0,
    ) == 1
    recovered = gc.get_goal_continuation("session-a")
    assert recovered["status"] == "pending"
    assert recovered["owner_id"] == "test-owner"


def test_restart_fails_closed_when_run_evidence_is_unreadable(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "evidence-stream", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )
    record = gc.get_goal_continuation("session-a")
    record["owner_id"] = "dead-owner"
    gc._replace_goal_continuation_for_test(record)

    def unreadable_summary(*_args, **_kwargs):
        raise PermissionError("journal evidence is unreadable")

    assert gc.recover_goal_continuations(
        goal_state_loader=lambda *_args, **_kwargs: {"status": "active", "turns_used": 1},
        run_summary_loader=unreadable_summary,
        now=clock + 2,
    ) == 1
    recovered = gc.get_goal_continuation("session-a")
    assert recovered["status"] == "failed"
    assert "evidence" in recovered["last_error"].lower()


def test_unreadable_registry_is_not_quarantined_as_corrupt(continuation_store, monkeypatch):
    gc, path = continuation_store
    _schedule(gc)
    gc._REGISTRY = None
    original_read_text = Path.read_text

    def deny_registry_read(self, *args, **kwargs):
        if self == path:
            raise PermissionError("simulated permission failure")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_registry_read)
    with pytest.raises(PermissionError, match="simulated permission failure"):
        gc.get_goal_continuation("session-a")
    assert path.exists(), "valid but unreadable state must not be quarantined or replaced"


def test_restart_refuses_replay_after_observable_activity(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, turns=4, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "server-stream-1", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )
    record = gc.get_goal_continuation("session-a")
    record["owner_id"] = "dead-owner"
    gc._replace_goal_continuation_for_test(record)

    assert gc.recover_goal_continuations(
        goal_state_loader=lambda *_args, **_kwargs: {"status": "active", "turns_used": 4},
        run_summary_loader=lambda *_args, **_kwargs: {
            "terminal_state": "running",
            "observable_activity": True,
        },
        now=200.0,
    ) == 1
    recovered = gc.get_goal_continuation("session-a")
    assert recovered["status"] == "failed"
    assert "observable activity" in recovered["last_error"]


def test_restart_never_replays_a_turn_already_judged(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, turns=4, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "server-stream-1", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )
    record = gc.get_goal_continuation("session-a")
    record["owner_id"] = "dead-owner"
    gc._replace_goal_continuation_for_test(record)

    assert gc.recover_goal_continuations(
        goal_state_loader=lambda *_args, **_kwargs: {
            "status": "active",
            "turns_used": 5,
            "continuation_prompt": "CURRENT_PROMPT_AFTER_JUDGE",
        },
        run_summary_loader=lambda *_args, **_kwargs: {"terminal_state": "completed"},
        now=200.0,
    ) == 1
    recovered = gc.get_goal_continuation("session-a")
    assert recovered["status"] == "pending"
    assert recovered["goal_turns_used"] == 5
    assert recovered["attempts"] == 0
    assert recovered["continuation_id"] != record["continuation_id"]
    assert recovered["prompt"] == "CURRENT_PROMPT_AFTER_JUDGE"


def test_restart_fails_closed_if_goal_advanced_without_current_prompt(continuation_store):
    gc, _path = continuation_store
    _schedule(gc, turns=4)
    record = gc.get_goal_continuation("session-a")
    record.update({"status": "running", "stream_id": "continued-stream", "owner_id": "dead-owner"})
    gc._replace_goal_continuation_for_test(record)

    assert gc.recover_goal_continuations(
        goal_state_loader=lambda *_args, **_kwargs: {"status": "active", "turns_used": 5},
        run_summary_loader=lambda *_args, **_kwargs: {"terminal_state": "completed"},
        now=200.0,
    ) == 1
    recovered = gc.get_goal_continuation("session-a")
    assert recovered["status"] == "failed"
    assert "no current continuation prompt" in recovered["last_error"]


def test_terminal_settlement_is_stream_owned_and_deletion_can_prune_it(continuation_store):
    gc, _path = continuation_store
    clock = time.time()
    _schedule(gc, now=clock)
    gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: {"stream_id": "owned-stream", "_status": 200},
        is_goal_active=lambda *_args, **_kwargs: True,
        now=clock + 1,
    )

    assert gc.fail_goal_continuation("session-a", "other-stream", "wrong owner") is False
    assert gc.fail_goal_continuation("session-a", "owned-stream", "terminal failure") is True
    assert gc.get_goal_continuation("session-a")["status"] == "failed"
    assert gc.complete_goal_continuation("session-a") is True
    assert gc.get_goal_continuation("session-a") is None


def test_inactive_goal_discards_pending_intent(continuation_store):
    gc, _path = continuation_store
    _schedule(gc)

    assert gc.drain_goal_continuations_once(
        start_turn=lambda *_args, **_kwargs: pytest.fail("inactive goal must not start"),
        is_goal_active=lambda *_args, **_kwargs: False,
        now=time.time() + 1,
    ) == 0
    assert gc.get_goal_continuation("session-a") is None


def test_timed_out_worker_stop_blocks_restart_until_old_thread_exits(continuation_store, monkeypatch):
    gc, _path = continuation_store
    entered = threading.Event()
    release = threading.Event()

    def blocked_worker():
        entered.set()
        release.wait(5)

    monkeypatch.setattr(gc, "_worker_loop", blocked_worker)
    assert gc.start_goal_continuation_worker() is True
    assert entered.wait(1)
    assert gc.stop_goal_continuation_worker(timeout=0.01) is False
    assert gc.start_goal_continuation_worker() is False
    release.set()
    assert gc.stop_goal_continuation_worker(timeout=1) is True
