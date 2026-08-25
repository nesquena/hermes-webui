import queue
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _enable_webui_wake_consumer_for_legacy_tests(monkeypatch):
    from api import background_process as bp

    monkeypatch.setattr(
        bp,
        "_load_kanban_webui_wake_state",
        lambda: (
            {
                "schema_version": 1,
                "enabled": True,
                "baseline_complete": True,
                "activation_id": 1,
                "baseline_completed_at": 0,
                "db_boundaries": {"board:default": 0},
            },
            None,
        ),
    )
    monkeypatch.setattr(bp, "_KANBAN_WAKE_STATE_LOCK", threading.Lock())


def test_webui_kanban_prompt_contains_terminal_fields_and_handoff():
    from api import background_process as bp

    task = SimpleNamespace(
        id="task-1",
        status="blocked",
        summary="",
        result="Needs credentials",
        idempotency_key="idem-1",
        session_id="worker-session",
    )
    event = SimpleNamespace(
        id=42,
        task_id="task-1",
        kind="blocked",
        payload={"decision_needed": "Choose provider", "next_hint": "Review task"},
    )

    prompt = bp._format_kanban_wakeup_prompt(task, event)

    assert 'task_id: "task-1"' in prompt
    assert 'event_id: 42' in prompt
    assert 'kind: "blocked"' in prompt
    assert 'status: "blocked"' in prompt
    assert 'summary: "Needs credentials"' in prompt
    assert 'decision_needed: "Choose provider"' in prompt
    assert 'next_hint: "Review task"' in prompt
    assert 'idempotency_key: "idem-1"' in prompt
    assert "Inspect the existing Kanban task" in prompt
    assert "kanban_create" in prompt
    assert "worker-session" not in prompt


def test_webui_kanban_subscription_filter_accepts_only_webui_wake_modes():
    from api import background_process as bp

    rows = [
        {"platform": "webui", "delivery_mode": "notify+wake"},
        {"platform": "WEBUI", "delivery_mode": "wake"},
        {"platform": "webui", "delivery_mode": "notify"},
        {"platform": "telegram", "delivery_mode": "notify+wake"},
        {"platform": "gateway", "delivery_mode": "notify+wake"},
        {"platform": "tui", "delivery_mode": "wake"},
    ]

    assert [bp._webui_wake_subscription(row) for row in rows] == [
        True, True, False, False, False, False
    ]


def test_webui_kanban_wakeup_reports_409_without_deferred_queue(monkeypatch):
    from api import background_process as bp
    import api.routes as routes

    result = {}
    monkeypatch.setattr(routes, "start_session_turn", lambda *args, **kwargs: {"_status": 409, "error": "active"})
    monkeypatch.setattr(bp, "record_deferred_wakeup", lambda *args: (_ for _ in ()).throw(AssertionError("deferred")))
    monkeypatch.setattr(bp.threading, "Thread", lambda **kwargs: type("T", (), {"start": lambda self: kwargs["target"]()})())

    bp._start_server_side_wakeup_turn(
        "chat-1", "prompt", profile="research",
        on_result=lambda status, resp, error=None: result.update(status=status, resp=resp, error=error),
    )

    assert result["status"] == 409
    assert result["resp"]["error"] == "active"


@pytest.mark.parametrize("status", [200, 201, 204, 409])
def test_webui_kanban_poll_ack_or_rewinds_claim(monkeypatch, tmp_path, status):
    from api import background_process as bp

    task = SimpleNamespace(
        id="task-1",
        status="completed",
        result="done",
        idempotency_key="idem-1",
        session_id="worker-session",
    )
    event = SimpleNamespace(
        id=7,
        task_id="task-1",
        kind="completed",
        payload={"summary": "done"},
    )
    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "origin-chat",
        "thread_id": "",
        "delivery_mode": "notify+wake",
        "notifier_profile": "research",
    }
    rewinds = []
    starts = []

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            return 3, 7, [event]

        def get_task(self, _conn, _task_id):
            return task

    fake_kb = FakeKB()
    monkeypatch.setattr("api.kanban_bridge._kb", lambda: fake_kb)
    monkeypatch.setattr("api.models.get_session", lambda _sid, metadata_only=False: SimpleNamespace(profile="research"))
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(bp, "_rewind_webui_kanban_claim", lambda *args: rewinds.append(args))

    def fake_start(session_id, prompt, **kwargs):
        starts.append((session_id, prompt, kwargs))
        kwargs["on_result"](status, {"_status": status}, None)

    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", fake_start)
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())

    bp._poll_webui_kanban_wakeups()

    assert len(starts) == 1
    assert starts[0][0] == "origin-chat"
    assert "worker-session" not in starts[0][1]
    assert starts[0][2]["profile"] == "research"
    if status == 200:
        assert rewinds == []
    else:
        assert len(rewinds) == 1
        assert rewinds[0][1]["chat_id"] == "origin-chat"
        assert rewinds[0][2:] == (3, 7)


def test_webui_kanban_poll_claims_terminal_batch_only_for_webui(monkeypatch, tmp_path):
    from api import background_process as bp

    terminal_kinds = [
        "completed",
        "blocked",
        "gave_up",
        "crashed",
        "timed_out",
        "review_requested",
        "block_loop_detected",
    ]
    task = SimpleNamespace(
        id="task-1", status="done", result="finished", idempotency_key="idem-1"
    )
    events = [
        SimpleNamespace(id=index, task_id="task-1", kind=kind, payload={})
        for index, kind in enumerate(terminal_kinds, 1)
    ]
    rows = [
        {
            "task_id": "task-1",
            "platform": platform,
            "chat_id": f"{platform}-chat",
            "thread_id": "",
            "delivery_mode": "notify+wake",
            "notifier_profile": "research",
        }
        for platform in ("webui", "telegram", "gateway", "tui")
    ] + [
        {
            "task_id": "task-1",
            "platform": "webui",
            "chat_id": "notify-only",
            "thread_id": "",
            "delivery_mode": "notify",
            "notifier_profile": "research",
        }
    ]
    claims = []
    starts = []

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn):
            return rows

        def claim_unseen_events_for_sub(self, _conn, **kwargs):
            claims.append(kwargs)
            return 0, len(events), events

        def get_task(self, _conn, _task_id):
            return task

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: FakeKB())
    monkeypatch.setattr(
        "api.models.get_session",
        lambda _sid, metadata_only=False: SimpleNamespace(profile="research"),
    )
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", lambda *args, **kwargs: (starts.append((args, kwargs)), kwargs["on_result"](200, {"_status": 200}, None)))
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_CLAIMS", {})

    bp._poll_webui_kanban_wakeups()

    assert len(claims) == 1
    assert claims[0]["platform"] == "webui"
    assert set(claims[0]["kinds"]) == set(terminal_kinds)
    assert len(starts) == 1
    assert starts[0][0][0] == "webui-chat"
    assert all(str(event.id) in starts[0][0][1] for event in events)
    assert all(event.kind in starts[0][0][1] for event in events)


@pytest.mark.parametrize(
    "failure", ["status_4xx", "status_5xx", "exception", "malformed", "missing"]
)
def test_webui_kanban_poll_rewinds_failures_and_retries_next_poll(
    monkeypatch, tmp_path, failure
):
    from api import background_process as bp

    task = SimpleNamespace(id="task-1", status="completed", result="done")
    event = SimpleNamespace(id=7, task_id="task-1", kind="completed", payload={})
    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "origin-chat",
        "thread_id": "",
        "delivery_mode": "notify+wake",
        "notifier_profile": "research",
    }
    rewinds = []
    starts = []
    poll_count = 0
    claim_count = 0

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            nonlocal claim_count
            claim_count += 1
            return 3, 7, [None if failure == "malformed" and claim_count == 1 else event]

        def get_task(self, _conn, _task_id):
            return None if failure == "missing" and claim_count == 1 else task

    def fake_start(_session_id, _prompt, **kwargs):
        nonlocal poll_count
        starts.append(kwargs)
        if failure == "exception" and poll_count == 0:
            poll_count += 1
            raise RuntimeError("wake failed")
        status = (
            400
            if failure == "status_4xx" and poll_count == 0
            else 500
            if failure == "status_5xx" and poll_count == 0
            else 200
        )
        poll_count += 1
        kwargs["on_result"](status, {"_status": status}, None)

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: FakeKB())
    monkeypatch.setattr("api.models.get_session", lambda *_args, **_kwargs: SimpleNamespace(profile="research"))
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(bp, "_rewind_webui_kanban_claim", lambda *args: rewinds.append(args))
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", fake_start)
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_CLAIMS", {})

    bp._poll_webui_kanban_wakeups()
    bp._poll_webui_kanban_wakeups()

    assert len(rewinds) == 1
    assert len(starts) == (1 if failure in {"malformed", "missing"} else 2)


def test_webui_kanban_paused_409_preserves_process_wakeup_source(monkeypatch):
    from contextlib import nullcontext

    from api import background_process as bp
    import api.profiles as profiles
    import api.routes as routes

    seen = {}
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda session_id, prompt, **kwargs: seen.update(
            session_id=session_id, prompt=prompt, kwargs=kwargs
        )
        or {"_status": 409, "error": "process_wakeup_paused"},
    )
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", lambda *_args: nullcontext())
    monkeypatch.setattr(bp.threading, "Thread", lambda **kwargs: type("T", (), {"start": lambda self: kwargs["target"]()})())
    monkeypatch.setattr(bp, "record_deferred_wakeup", lambda *_args: (_ for _ in ()).throw(AssertionError("deferred")))

    result = {}
    bp._start_server_side_wakeup_turn(
        "origin-chat",
        "prompt",
        profile="research",
        on_result=lambda status, response, error: result.update(status=status, error=error),
    )

    assert seen["session_id"] == "origin-chat"
    assert seen["kwargs"]["source"] == "process_wakeup"
    assert result["status"] == 409


def test_webui_kanban_stop_rewinds_inflight_claim_and_blocks_new_claims(monkeypatch, tmp_path):
    from api import background_process as bp

    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "origin-chat",
        "thread_id": "",
        "delivery_mode": "notify+wake",
        "notifier_profile": "research",
    }
    task = SimpleNamespace(id="task-1", status="completed", result="done")
    event = SimpleNamespace(id=7, task_id="task-1", kind="completed", payload={})
    rewinds = []
    starts = []
    callbacks = []
    claim_calls = 0

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            nonlocal claim_calls
            claim_calls += 1
            return 3, 7, [event]

        def get_task(self, _conn, _task_id):
            return task

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: FakeKB())
    monkeypatch.setattr(bp, "_rewind_webui_kanban_claim", lambda *args: rewinds.append(args))
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_CLAIMS", {})
    monkeypatch.setattr("api.models.get_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda *_args, **kwargs: (starts.append(True), callbacks.append(kwargs["on_result"])),
    )

    bp._poll_webui_kanban_wakeups()
    bp.stop_drain_thread()
    bp._poll_webui_kanban_wakeups()

    assert len(rewinds) == 1
    assert rewinds[0][2:] == (3, 7)
    assert claim_calls == 1
    assert len(starts) == 1
    callbacks[0](500, {"_status": 500}, None)
    assert len(rewinds) == 1


def test_webui_kanban_poll_does_not_start_wake_after_stop(monkeypatch, tmp_path):
    from api import background_process as bp

    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "origin-chat",
        "thread_id": "",
        "delivery_mode": "notify+wake",
        "notifier_profile": "research",
    }
    task = SimpleNamespace(id="task-1", status="completed", result="done")
    event = SimpleNamespace(id=7, task_id="task-1", kind="completed", payload={})
    starts = []
    original_claim = bp._claim_webui_kanban_events

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            return 3, 7, [event]

        def get_task(self, _conn, _task_id):
            return task

    def claim_then_stop(*args, **kwargs):
        claimed = original_claim(*args, **kwargs)
        bp._DRAIN_STOP.set()
        return claimed

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: FakeKB())
    monkeypatch.setattr(bp, "_claim_webui_kanban_events", claim_then_stop)
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_CLAIMS", {})
    monkeypatch.setattr("api.models.get_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda *_args, **_kwargs: starts.append(True),
    )

    bp._poll_webui_kanban_wakeups()

    assert starts == []


def test_webui_kanban_two_pollers_do_not_duplicate_claim(monkeypatch, tmp_path):
    from api import background_process as bp

    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "origin-chat",
        "thread_id": "",
        "delivery_mode": "notify+wake",
        "notifier_profile": "research",
    }
    task = SimpleNamespace(id="task-1", status="completed", result="done")
    event = SimpleNamespace(id=7, task_id="task-1", kind="completed", payload={})
    claims = 0
    first_claim_started = threading.Event()
    release_first_claim = threading.Event()
    starts = []

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            nonlocal claims
            claims += 1
            if claims == 1:
                first_claim_started.set()
                assert release_first_claim.wait(timeout=1.0)
                return 0, 7, [event]
            return 7, 7, []

        def get_task(self, _conn, _task_id):
            return task

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: FakeKB())
    monkeypatch.setattr("api.models.get_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_CLAIMS", {})
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", lambda *args, **kwargs: (starts.append(args), kwargs["on_result"](200, {"_status": 200}, None)))

    first = threading.Thread(target=bp._poll_webui_kanban_wakeups)
    second = threading.Thread(target=bp._poll_webui_kanban_wakeups)
    first.start()
    assert first_claim_started.wait(timeout=1.0)
    second.start()
    second.join(timeout=1.0)
    assert not second.is_alive()
    assert claims == 1
    release_first_claim.set()
    first.join(timeout=1.0)

    assert not first.is_alive()
    assert len(starts) == 1


def test_webui_kanban_poll_409_rewinds_without_deferred_queue(monkeypatch, tmp_path):
    from contextlib import nullcontext

    from api import background_process as bp
    import api.profiles as profiles
    import api.routes as routes

    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "origin-chat",
        "thread_id": "",
        "delivery_mode": "notify+wake",
        "notifier_profile": "research",
    }
    task = SimpleNamespace(id="task-1", status="blocked", result="busy")
    event = SimpleNamespace(id=7, task_id="task-1", kind="blocked", payload={})
    rewinds = []

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            return 3, 7, [event]

        def get_task(self, _conn, _task_id):
            return task

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: FakeKB())
    monkeypatch.setattr("api.models.get_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(profiles, "profile_env_for_background_worker", lambda *_args: nullcontext())
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *_args, **kwargs: {"_status": 409, "error": "active", "source": kwargs["source"]},
    )
    monkeypatch.setattr(bp, "_rewind_webui_kanban_claim", lambda *args: rewinds.append(args))
    monkeypatch.setattr(bp, "record_deferred_wakeup", lambda *_args: (_ for _ in ()).throw(AssertionError("deferred")))
    monkeypatch.setattr(bp.threading, "Thread", lambda **kwargs: type("T", (), {"start": lambda self: kwargs["target"]()})())
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_CLAIMS", {})

    bp._poll_webui_kanban_wakeups()

    assert len(rewinds) == 1
    assert rewinds[0][2:] == (3, 7)


def test_drain_runs_kanban_poll_off_completion_drain_thread(monkeypatch):
    from tests._wakeup_helpers import install_fake_registry
    from api import background_process as bp

    poll_started = threading.Event()
    release_poll = threading.Event()
    processed = []
    stop = threading.Event()

    class CompletionQueue:
        calls = 0

        def get(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise queue.Empty
            assert poll_started.wait(timeout=1.0)
            return {"type": "completion"}

    def poll():
        poll_started.set()
        release_poll.wait(timeout=1.0)

    install_fake_registry(
        monkeypatch,
        SimpleNamespace(completion_queue=CompletionQueue()),
    )
    monkeypatch.setattr(bp, "_DRAIN_STOP", stop)
    monkeypatch.setattr(bp, "_poll_webui_kanban_wakeups", poll)
    monkeypatch.setattr(
        bp,
        "_process_one",
        lambda event: (processed.append(event), release_poll.set(), stop.set()),
    )

    drain = threading.Thread(target=bp._drain_loop, daemon=True)
    drain.start()
    drain.join(timeout=3.0)

    assert not drain.is_alive()
    assert processed == [{"type": "completion"}]
    assert poll_started.is_set()
