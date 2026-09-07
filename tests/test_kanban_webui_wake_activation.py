import json
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


@pytest.fixture
def activation(monkeypatch, tmp_path):
    from api import background_process as bp
    from api import config as api_config

    state_file = tmp_path / "kanban_webui_wake_state.json"
    monkeypatch.setattr(api_config, "KANBAN_WEBUI_WAKE_STATE_FILE", state_file, raising=False)
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_POLL_LOCK", bp.threading.Lock(), raising=False)
    monkeypatch.setattr(bp, "_KANBAN_WAKE_STATE_LOCK", bp.threading.Lock(), raising=False)
    monkeypatch.setattr(
        "api.profiles.list_profiles_api",
        lambda: [
            {"name": "research", "is_default": False},
            {"name": "default", "is_default": True},
        ],
    )
    monkeypatch.setattr("api.profiles._is_root_profile", lambda name: name == "default")
    return bp, state_file


def test_activation_isolates_leftover_module_drain_stop(monkeypatch, tmp_path, request):
    from api import background_process as bp

    real_stop = bp._DRAIN_STOP
    real_stop.set()
    try:
        bp, state_file = request.getfixturevalue("activation")

        class Conn:
            def execute(self, sql):
                return SimpleNamespace(fetchone=lambda: {"latest": 4})

            def close(self):
                pass

        class KB:
            def list_boards(self, **kwargs):
                return [{"slug": "default", "db_path": str(state_file.parent / "db.sqlite")}]

            def connect(self, **kwargs):
                return Conn()

            def list_notify_subs(self, conn, **_kwargs):
                return [
                    {
                        "task_id": "t",
                        "platform": "webui",
                        "chat_id": "c",
                        "thread_id": "",
                        "last_event_id": 0,
                    }
                ]

            def advance_notify_cursor(self, conn, **kwargs):
                pass

        monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
        result = bp.set_kanban_webui_wake_enabled(True)
        assert result["enabled"] is True
        assert result["activation_id"] == 1
        assert real_stop.is_set()
        assert bp._DRAIN_STOP is not real_stop
        assert not bp._DRAIN_STOP.is_set()
    finally:
        real_stop.clear()


def test_missing_or_corrupt_state_is_disabled(activation):
    bp, state_file = activation

    status = bp.get_kanban_webui_wake_status()
    assert status["enabled"] is False
    assert status["baseline_complete"] is False

    state_file.write_text('{"schema_version": 99}', encoding="utf-8")
    status = bp.get_kanban_webui_wake_status()
    assert status["enabled"] is False
    assert "state_error" in status


def test_enable_baselines_all_webui_rows_at_one_boundary(activation, monkeypatch):
    bp, state_file = activation
    rows = [
        {"task_id": "wake", "platform": "webui", "chat_id": "c1", "thread_id": "", "last_event_id": 2, "delivery_mode": "wake"},
        {"task_id": "notify", "platform": "WEBUI", "chat_id": "c2", "thread_id": "", "last_event_id": 0, "delivery_mode": "notify"},
        {"task_id": "telegram", "platform": "telegram", "chat_id": "c3", "thread_id": "", "last_event_id": 0, "delivery_mode": "wake"},
    ]
    advanced = []

    class Conn:
        def execute(self, sql):
            assert "MAX(id)" in sql
            return SimpleNamespace(fetchone=lambda: {"latest": 7})

        def close(self):
            pass

    class KB:
        def list_boards(self, **kwargs):
            return [{"slug": "default", "db_path": str(state_file.parent / "kanban.db")}]

        def connect(self, **kwargs):
            return Conn()

        def list_notify_subs(self, conn, **_kwargs):
            return list(rows)

        def advance_notify_cursor(self, conn, **kwargs):
            advanced.append(kwargs)
            for row in rows:
                if all(row.get(key) == kwargs[key] for key in ("task_id", "platform", "chat_id", "thread_id")):
                    row["last_event_id"] = kwargs["new_cursor"]

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    result = bp.set_kanban_webui_wake_enabled(True)

    assert result["enabled"] is True
    assert result["baseline_complete"] is True
    assert result["activation_id"] == 1
    assert [item["task_id"] for item in advanced] == ["wake", "notify"]
    assert all(item["new_cursor"] == 7 for item in advanced)
    assert json.loads(state_file.read_text(encoding="utf-8"))["db_boundaries"]


def test_enable_baselines_only_owned_webui_rows(activation, monkeypatch):
    bp, state_file = activation
    rows = [
        {
            "task_id": "blank",
            "platform": "webui",
            "chat_id": "c1",
            "thread_id": "",
            "last_event_id": 2,
            "notifier_profile": "",
        },
        {
            "task_id": "default",
            "platform": "webui",
            "chat_id": "c2",
            "thread_id": "",
            "last_event_id": 0,
            "notifier_profile": "default",
        },
        {
            "task_id": "other",
            "platform": "webui",
            "chat_id": "c3",
            "thread_id": "",
            "last_event_id": 0,
            "notifier_profile": "other",
        },
        {
            "task_id": "telegram",
            "platform": "telegram",
            "chat_id": "c4",
            "thread_id": "",
            "last_event_id": 0,
            "notifier_profile": "default",
        },
    ]
    advanced = []
    list_kwargs = []

    class Conn:
        def execute(self, sql):
            assert "MAX(id)" in sql
            return SimpleNamespace(fetchone=lambda: {"latest": 9})

        def close(self):
            pass

    class KB:
        def list_boards(self, **kwargs):
            return [{"slug": "default", "db_path": str(state_file.parent / "kanban.db")}]

        def connect(self, **kwargs):
            return Conn()

        def list_notify_subs(self, conn, **kwargs):
            list_kwargs.append(dict(kwargs))
            return list(rows)

        def advance_notify_cursor(self, conn, **kwargs):
            advanced.append(kwargs)
            for row in rows:
                if all(
                    row.get(key) == kwargs[key]
                    for key in ("task_id", "platform", "chat_id", "thread_id")
                ):
                    row["last_event_id"] = kwargs["new_cursor"]

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    result = bp.set_kanban_webui_wake_enabled(True)

    assert result["enabled"] is True
    assert list_kwargs
    assert set(list_kwargs[0]["notifier_profiles"]) >= {"default", "research"}
    assert list_kwargs[0]["include_unowned"] is True
    assert [item["task_id"] for item in advanced] == ["blank", "default"]
    assert all(item["new_cursor"] == 9 for item in advanced)


def test_enable_is_idempotent_and_disable_reenable_baselines_again(activation, monkeypatch):
    bp, state_file = activation
    boundaries = iter([3, 8])
    snapshots = []

    class Conn:
        def execute(self, sql):
            return SimpleNamespace(fetchone=lambda: {"latest": next(boundaries)})

        def close(self):
            pass

    rows = [{"task_id": "t", "platform": "webui", "chat_id": "c", "thread_id": "", "last_event_id": 0}]

    class KB:
        def list_boards(self, **kwargs):
            return [{"slug": "default", "db_path": str(state_file.parent / "db.sqlite")}]

        def connect(self, **kwargs):
            return Conn()

        def list_notify_subs(self, conn, **_kwargs):
            return rows

        def advance_notify_cursor(self, conn, **kwargs):
            snapshots.append(kwargs["new_cursor"])
            rows[0]["last_event_id"] = kwargs["new_cursor"]

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    assert bp.set_kanban_webui_wake_enabled(True)["activation_id"] == 1
    assert bp.set_kanban_webui_wake_enabled(True)["activation_id"] == 1
    assert snapshots == [3]
    assert bp.set_kanban_webui_wake_enabled(False)["enabled"] is False
    assert bp.set_kanban_webui_wake_enabled(True)["activation_id"] == 2
    assert snapshots == [3, 8]


def test_concurrent_enable_serializes_one_baseline(activation, monkeypatch):
    bp, state_file = activation
    entered = threading.Event()
    second_attempted = threading.Event()
    release = threading.Event()
    baseline_calls = 0
    advanced = []
    results = []

    class Conn:
        def execute(self, _sql):
            return SimpleNamespace(fetchone=lambda: {"latest": 5})

        def close(self):
            pass

    rows = [{"task_id": "t", "platform": "webui", "chat_id": "c", "thread_id": "", "last_event_id": 0}]

    class KB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default", "db_path": str(state_file.parent / "db.sqlite")}]

        def connect(self, **_kwargs):
            nonlocal baseline_calls
            baseline_calls += 1
            if baseline_calls == 1:
                entered.set()
                assert release.wait(timeout=1.0)
            return Conn()

        def list_notify_subs(self, _conn, **_kwargs):
            return rows

        def advance_notify_cursor(self, _conn, **kwargs):
            advanced.append(kwargs["new_cursor"])
            rows[0]["last_event_id"] = kwargs["new_cursor"]

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    first = threading.Thread(
        target=lambda: results.append(bp.set_kanban_webui_wake_enabled(True))
    )
    second = threading.Thread(
        target=lambda: (
            second_attempted.set(),
            results.append(bp.set_kanban_webui_wake_enabled(True)),
        )
    )
    first.start()
    assert entered.wait(timeout=1.0)
    second.start()
    assert second_attempted.wait(timeout=1.0)
    assert second.is_alive()
    release.set()
    workers = [first, second]
    for worker in workers:
        worker.join(timeout=1.0)

    assert all(not worker.is_alive() for worker in workers)
    assert baseline_calls == 1
    assert advanced == [5]
    assert [result["activation_id"] for result in results] == [1, 1]


def test_failed_baseline_does_not_write_enabled_marker(activation, monkeypatch):
    bp, state_file = activation

    class KB:
        def list_boards(self, **kwargs):
            return [{"slug": "default", "db_path": str(state_file.parent / "db.sqlite")}]

        def connect(self, **kwargs):
            raise OSError("database unavailable")

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    with pytest.raises(RuntimeError):
        bp.set_kanban_webui_wake_enabled(True)
    assert not state_file.exists()
    assert bp.get_kanban_webui_wake_status()["enabled"] is False


def test_poll_is_gated_while_state_is_missing(activation, monkeypatch):
    bp, _state_file = activation
    monkeypatch.setattr(bp, "_DRAIN_STOP", bp.threading.Event())
    monkeypatch.setattr("api.kanban_bridge._kb", lambda: pytest.fail("poll must be gated"))
    bp._poll_webui_kanban_wakeups()


def test_poll_pauses_when_owned_db_topology_changes(activation, monkeypatch):
    bp, state_file = activation
    state_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "baseline_complete": True,
                "activation_id": 1,
                "baseline_completed_at": 1,
                "db_boundaries": {str(state_file.parent / "old.db"): 2},
            }
        ),
        encoding="utf-8",
    )

    class KB:
        def list_boards(self, **kwargs):
            return [{"slug": "default", "db_path": str(state_file.parent / "new.db")}]

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    monkeypatch.setattr(bp, "_DRAIN_STOP", bp.threading.Event())
    bp._poll_webui_kanban_wakeups()


@pytest.mark.parametrize("missing", ["session", "profile"])
def test_poll_rewinds_missing_session_or_profile(activation, monkeypatch, missing):
    bp, state_file = activation
    state_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "baseline_complete": True,
                "activation_id": 1,
                "baseline_completed_at": 1,
                "db_boundaries": {"board:default": 0},
            }
        ),
        encoding="utf-8",
    )
    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "chat-1",
        "thread_id": "",
        "notifier_profile": "research",
        "delivery_mode": "notify+wake",
    }
    task = SimpleNamespace(id="task-1", status="completed", result="done")
    event = SimpleNamespace(id=7, task_id="task-1", kind="completed", payload={})
    rewinds = []

    class FakeKB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn, **_kwargs):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            return 0, 7, [event]

        def get_task(self, _conn, _task_id):
            return task

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: FakeKB())
    monkeypatch.setattr(
        "api.models.get_session",
        lambda *_args, **_kwargs: None if missing == "session" else object(),
    )
    monkeypatch.setattr(
        "api.profiles.get_hermes_home_for_profile",
        lambda _profile: state_file.parent / "missing-profile"
        if missing == "profile"
        else state_file.parent,
    )
    monkeypatch.setattr(
        "api.profiles.profile_env_for_background_worker",
        lambda *_args: nullcontext(),
    )
    monkeypatch.setattr(bp, "_rewind_webui_kanban_claim", lambda *args: rewinds.append(args))
    monkeypatch.setattr(bp, "_DRAIN_STOP", threading.Event())
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_CLAIMS", {})
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda *_args, **_kwargs: pytest.fail("missing wake target must not start a turn"),
    )

    bp._poll_webui_kanban_wakeups()

    assert len(rewinds) == 1
    assert rewinds[0][2:] == (0, 7)


def test_post_baseline_event_is_claimed_by_the_wake_consumer(activation, monkeypatch):
    bp, state_file = activation
    row = {
        "task_id": "task",
        "platform": "webui",
        "chat_id": "chat",
        "thread_id": "",
        "notifier_profile": "research",
        "delivery_mode": "notify+wake",
        "last_event_id": 0,
    }
    event = SimpleNamespace(id=5, task_id="task", kind="completed", payload={})
    task = SimpleNamespace(id="task", status="completed", result="done")
    started = []

    class Conn:
        def execute(self, sql):
            return SimpleNamespace(fetchone=lambda: {"latest": 4})

        def close(self):
            pass

    class KB:
        def list_boards(self, **kwargs):
            return [{"slug": "default", "db_path": str(state_file.parent / "db.sqlite")}]

        def connect(self, **kwargs):
            return Conn()

        def list_notify_subs(self, conn, **_kwargs):
            return [row]

        def advance_notify_cursor(self, conn, **kwargs):
            row["last_event_id"] = kwargs["new_cursor"]

        def claim_unseen_events_for_sub(self, conn, **kwargs):
            assert row["last_event_id"] == 4
            return 4, 5, [event]

        def get_task(self, conn, task_id):
            return task

    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    bp.set_kanban_webui_wake_enabled(True)
    monkeypatch.setattr("api.models.get_session", lambda *args, **kwargs: object())
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda profile: state_file.parent)
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *args: nullcontext())
    monkeypatch.setattr(bp, "_DRAIN_STOP", bp.threading.Event())
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda session_id, prompt, **kwargs: (
            started.append((session_id, prompt)),
            kwargs["on_result"](200, {"_status": 200}, None),
        ),
    )

    bp._poll_webui_kanban_wakeups()
    assert len(started) == 1
    assert started[0][0] == "chat"
    assert "event_id: 5" in started[0][1]


@pytest.mark.parametrize("initial_claim_state", ["awaiting", "retry_pending"])
@pytest.mark.parametrize("failure", ["baseline", "sidecar"])
def test_failed_enable_preserves_old_claim_rewind_and_future_boundary(
    activation, monkeypatch, initial_claim_state, failure
):
    bp, state_file = activation
    low_cursor = 3
    high_cursor = 7
    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "chat-1",
        "thread_id": "",
        "notifier_profile": "research",
        "delivery_mode": "notify+wake",
    }
    event_a = SimpleNamespace(id=high_cursor, task_id="task-1", kind="completed", payload={})
    events = [event_a]
    cursor = {"value": low_cursor}
    callbacks = []
    started = []
    rewind_attempts = []
    failed_rewind = initial_claim_state == "retry_pending"
    baseline_entered = threading.Event()
    release_baseline = threading.Event()
    callback_waiting = threading.Event()

    class TrackingLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "old-claim-callback":
                callback_waiting.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    class KB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn, **_kwargs):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            old_cursor = cursor["value"]
            unseen = [event for event in events if event.id > old_cursor]
            if not unseen:
                return old_cursor, old_cursor, []
            new_cursor = max(event.id for event in unseen)
            cursor["value"] = new_cursor
            return old_cursor, new_cursor, unseen

        def rewind_notify_cursor(self, _conn, **kwargs):
            nonlocal failed_rewind
            rewind_attempts.append((kwargs["claimed_cursor"], kwargs["old_cursor"]))
            if failed_rewind:
                failed_rewind = False
                return False
            if cursor["value"] != kwargs["claimed_cursor"]:
                return False
            cursor["value"] = kwargs["old_cursor"]
            return True

        def get_task(self, _conn, _task_id):
            return SimpleNamespace(id="task-1", status="completed", result="done")

    state_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "baseline_complete": True,
                "activation_id": 1,
                "baseline_completed_at": 1,
                "db_boundaries": {"board:default": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_LOCK", TrackingLock())
    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    monkeypatch.setattr("api.models.get_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: state_file.parent)
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *_args: nullcontext())
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda _chat_id, prompt, **kwargs: (
            started.append(prompt), callbacks.append(kwargs["on_result"])
        ),
    )

    bp._poll_webui_kanban_wakeups()
    assert len(callbacks) == 1
    claim_key = bp._webui_kanban_claim_key("default", sub)
    old_claim = bp._KANBAN_INFLIGHT_CLAIMS[claim_key]
    assert cursor["value"] == high_cursor
    if initial_claim_state == "retry_pending":
        callbacks[0](500, {"_status": 500}, None)
        assert cursor["value"] == high_cursor
        assert bp._KANBAN_INFLIGHT_CLAIMS[claim_key] is old_claim
        assert old_claim["retry_pending"] is True
        assert rewind_attempts == [(high_cursor, low_cursor)]

    assert bp.set_kanban_webui_wake_enabled(False)["enabled"] is False
    assert bp.get_kanban_webui_wake_status()["activation_id"] == 1

    baseline_calls = 0
    def baseline(_kb):
        nonlocal baseline_calls
        baseline_calls += 1
        if baseline_calls > 1:
            cursor["value"] = high_cursor
            return {"board:default": high_cursor}
        baseline_entered.set()
        assert release_baseline.wait(timeout=1.0)
        if failure == "baseline":
            raise RuntimeError("baseline failed")
        cursor["value"] = high_cursor
        return {"board:default": high_cursor}

    original_write = bp._write_kanban_webui_wake_state
    sidecar_failed = False

    def write(state):
        nonlocal sidecar_failed
        if state["enabled"] and failure == "sidecar" and not sidecar_failed:
            sidecar_failed = True
            raise OSError("sidecar failed")
        original_write(state)

    monkeypatch.setattr(bp, "_baseline_kanban_webui_wake", baseline)
    monkeypatch.setattr(bp, "_write_kanban_webui_wake_state", write)
    enable_errors = []
    enable = threading.Thread(
        target=lambda: _capture_enable_error(bp, enable_errors), name="enable"
    )
    callback = threading.Thread(
        target=lambda: callbacks[0](500, {"_status": 500}, None),
        name="old-claim-callback",
    )
    try:
        enable.start()
        assert baseline_entered.wait(timeout=1.0)
        callback.start()
        assert callback_waiting.wait(timeout=1.0)
        assert callback.is_alive()
        pending_status = bp.get_kanban_webui_wake_status()
        assert pending_status["enabled"] is False
        assert pending_status["activation_id"] == 1
        assert bp._KANBAN_INFLIGHT_CLAIMS[claim_key] is old_claim
        assert cursor["value"] == high_cursor
    finally:
        release_baseline.set()
        enable.join(timeout=1.0)
        callback.join(timeout=1.0)

    assert not enable.is_alive()
    assert not callback.is_alive()
    assert len(enable_errors) == 1
    failed_status = bp.get_kanban_webui_wake_status()
    assert failed_status["enabled"] is False
    assert failed_status["activation_id"] == 1
    assert cursor["value"] == low_cursor
    assert claim_key not in bp._KANBAN_INFLIGHT_CLAIMS
    assert rewind_attempts[-1] == (high_cursor, low_cursor)

    enabled = bp.set_kanban_webui_wake_enabled(True)
    assert enabled["enabled"] is True
    assert enabled["activation_id"] == 2
    assert enabled["db_boundaries"] == {"board:default": high_cursor}
    assert cursor["value"] == high_cursor

    callbacks[0](200, {"_status": 200}, None)
    callbacks[0](500, {"_status": 500}, None)
    assert cursor["value"] == high_cursor
    assert claim_key not in bp._KANBAN_INFLIGHT_CLAIMS

    event_b = SimpleNamespace(id=high_cursor + 1, task_id="task-1", kind="completed", payload={})
    events.append(event_b)
    bp._poll_webui_kanban_wakeups()
    assert len(callbacks) == 2
    assert "event_id: 8" in started[-1]
    assert cursor["value"] == high_cursor + 1
    callbacks[-1](200, {"_status": 200}, None)
    assert claim_key not in bp._KANBAN_INFLIGHT_CLAIMS


@pytest.mark.parametrize("committed_cursor", [7, 8])
def test_enable_serializes_old_claim_rewind_until_the_sidecar_outcome(
    activation, monkeypatch, committed_cursor
):
    bp, state_file = activation
    low_cursor = 3
    high_cursor = 7
    sub = {
        "task_id": "task-1",
        "platform": "webui",
        "chat_id": "chat-1",
        "thread_id": "",
        "notifier_profile": "research",
        "delivery_mode": "notify+wake",
    }
    event = SimpleNamespace(id=high_cursor, task_id="task-1", kind="completed", payload={})
    cursor = {"value": low_cursor}
    callbacks = []
    rewinds = []
    baseline_entered = threading.Event()
    release_baseline = threading.Event()
    writer_entered = threading.Event()
    release_writer = threading.Event()
    writer_succeeded = threading.Event()
    callback_waiting = threading.Event()
    callback_finished = threading.Event()

    class TrackingLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "old-claim-callback":
                callback_waiting.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    class KB:
        def list_boards(self, **_kwargs):
            return [{"slug": "default"}]

        def connect(self, **_kwargs):
            return SimpleNamespace(close=lambda: None)

        def list_notify_subs(self, _conn, **_kwargs):
            return [sub]

        def claim_unseen_events_for_sub(self, _conn, **_kwargs):
            old_cursor = cursor["value"]
            if event.id <= old_cursor:
                return old_cursor, old_cursor, []
            cursor["value"] = event.id
            return old_cursor, event.id, [event]

        def rewind_notify_cursor(self, _conn, **kwargs):
            rewinds.append((kwargs["claimed_cursor"], kwargs["old_cursor"]))
            if cursor["value"] != kwargs["claimed_cursor"]:
                return False
            cursor["value"] = kwargs["old_cursor"]
            return True

        def get_task(self, _conn, _task_id):
            return SimpleNamespace(id="task-1", status="completed", result="done")

    state_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enabled": True,
                "baseline_complete": True,
                "activation_id": 1,
                "baseline_completed_at": 1,
                "db_boundaries": {"board:default": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bp, "_KANBAN_INFLIGHT_LOCK", TrackingLock())
    monkeypatch.setattr("api.kanban_bridge._kb", lambda: KB())
    monkeypatch.setattr("api.models.get_session", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("api.profiles.get_hermes_home_for_profile", lambda _profile: state_file.parent)
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *_args: nullcontext())
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda *_args, **kwargs: callbacks.append(kwargs["on_result"]),
    )

    bp._poll_webui_kanban_wakeups()
    assert len(callbacks) == 1
    claim_key = bp._webui_kanban_claim_key("default", sub)
    old_claim = bp._KANBAN_INFLIGHT_CLAIMS[claim_key]
    assert cursor["value"] == high_cursor
    assert bp.set_kanban_webui_wake_enabled(False)["enabled"] is False

    def baseline(_kb):
        baseline_entered.set()
        assert release_baseline.wait(timeout=1.0)
        cursor["value"] = committed_cursor
        return {"board:default": committed_cursor}

    original_write = bp._write_kanban_webui_wake_state

    def write(state):
        if state["enabled"]:
            writer_entered.set()
            assert release_writer.wait(timeout=1.0)
        original_write(state)
        if state["enabled"]:
            writer_succeeded.set()

    monkeypatch.setattr(bp, "_baseline_kanban_webui_wake", baseline)
    monkeypatch.setattr(bp, "_write_kanban_webui_wake_state", write)
    enable_errors = []
    enable = threading.Thread(
        target=lambda: _capture_enable_error(bp, enable_errors), name="enable"
    )

    def finish_old_claim():
        try:
            callbacks[0](500, {"_status": 500}, None)
        finally:
            callback_finished.set()

    callback = threading.Thread(target=finish_old_claim, name="old-claim-callback")
    try:
        enable.start()
        assert baseline_entered.wait(timeout=1.0)
        release_baseline.set()
        assert writer_entered.wait(timeout=1.0)
        callback.start()
        assert callback_waiting.wait(timeout=1.0)
        assert callback.is_alive()
        assert not callback_finished.is_set()
        pending_status = bp.get_kanban_webui_wake_status()
        assert pending_status["enabled"] is False
        assert pending_status["activation_id"] == 1
        assert bp._KANBAN_INFLIGHT_CLAIMS[claim_key] is old_claim
        assert cursor["value"] == committed_cursor
    finally:
        release_baseline.set()
        release_writer.set()
        enable.join(timeout=1.0)
        callback.join(timeout=1.0)

    assert not enable.is_alive()
    assert not callback.is_alive()
    assert enable_errors == []
    assert writer_succeeded.is_set()
    assert callback_finished.is_set()
    enabled = bp.get_kanban_webui_wake_status()
    assert enabled["enabled"] is True
    assert enabled["activation_id"] == 2
    assert enabled["db_boundaries"] == {"board:default": committed_cursor}
    assert claim_key not in bp._KANBAN_INFLIGHT_CLAIMS
    assert rewinds == []
    assert cursor["value"] == committed_cursor


@pytest.mark.parametrize(
    "body",
    [
        {"action": None},
        {"action": True},
        {"action": 1},
        {"action": []},
        {"action": {}},
        None,
        True,
        1,
        [],
        {},
        {"action": "enable", "extra": True},
        {"unexpected": "enable"},
    ],
)
def test_webui_wake_post_rejects_invalid_action_before_setter(monkeypatch, body):
    from api import routes

    calls = []
    order = []
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: order.append("csrf") or True)
    monkeypatch.setattr(
        routes,
        "_handle_extension_sidecar_proxy",
        lambda *_args, **_kwargs: order.append("proxy") or False,
    )
    monkeypatch.setattr(routes, "read_body", lambda _handler: order.append("body") or body)
    monkeypatch.setattr(
        routes,
        "_guard_request_session_visibility",
        lambda *_args, **_kwargs: order.append("visibility") or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: {"message": message, "status": status},
    )
    monkeypatch.setattr(
        "api.background_process.set_kanban_webui_wake_enabled",
        lambda enabled: calls.append(enabled),
    )

    result = routes.handle_post(object(), SimpleNamespace(path="/api/kanban/webui-wake"))

    assert result == {"message": "action must be enable or disable", "status": 400}
    assert calls == []
    assert order == ["csrf", "proxy", "body", "visibility"]


@pytest.mark.parametrize(("action", "enabled"), [("enable", True), ("disable", False)])
def test_webui_wake_post_accepts_valid_string_action(monkeypatch, action, enabled):
    from api import routes

    calls = []
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_handle_extension_sidecar_proxy", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(routes, "read_body", lambda _handler: {"action": action})
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200: {"payload": payload, "status": status},
    )
    monkeypatch.setattr(
        "api.background_process.set_kanban_webui_wake_enabled",
        lambda value: calls.append(value) or {"enabled": value},
    )

    result = routes.handle_post(object(), SimpleNamespace(path="/api/kanban/webui-wake"))

    assert result == {"payload": {"enabled": enabled}, "status": 200}
    assert calls == [enabled]


def _capture_enable_error(bp, errors):
    try:
        bp.set_kanban_webui_wake_enabled(True)
    except Exception as exc:
        errors.append(exc)
