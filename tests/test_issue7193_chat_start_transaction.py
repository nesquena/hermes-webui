"""Behavioral coverage for the shared chat-start admission transaction."""

import copy
import json
import threading
import time
from pathlib import Path

import pytest

import api.config as config
import api.models as models
import api.routes as routes
from api.models import new_session


@pytest.fixture
def transaction_env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: config.get_webui_session_save_mode(config.cfg))
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _session_id: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_run_gateway_chat_streaming", lambda *args, **kwargs: None)
    config.STREAMS.clear()
    config.STREAM_GOAL_RELATED.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    routes.PENDING_GOAL_CONTINUATION.clear()
    routes.PENDING_BG_TASK_COMPLETIONS.clear()
    yield session_dir
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.STREAM_GOAL_RELATED.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    routes.PENDING_GOAL_CONTINUATION.clear()
    routes.PENDING_BG_TASK_COMPLETIONS.clear()


def _start(session, **overrides):
    values = {
        "msg": "retry me",
        "attachments": [],
        "workspace": "/tmp/workspace",
        "model": session.model,
        "model_provider": session.model_provider,
        "external_runtime_owned": False,
    }
    values.update(overrides)
    return routes._start_chat_stream_for_session(session, **values)


def _users(session):
    return [row for row in session.messages if row.get("role") == "user"]


def _regeneration_session(transaction_env, *, backup_state="six"):
    session = new_session(workspace=str(transaction_env.parent), profile="profile-a")
    rows = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "second answer"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "third answer"},
    ]
    session.messages = copy.deepcopy(rows)
    session.context_messages = copy.deepcopy(rows)
    session.save(touch_updated_at=False)
    session.messages = copy.deepcopy(rows[:4])
    session.context_messages = copy.deepcopy(rows[:4])
    session.save(touch_updated_at=False)
    backup = session.path.with_suffix(".json.bak")
    if backup_state == "absent":
        backup.unlink()
    elif backup_state == "empty":
        backup.write_bytes(b"")
    return session


def _rejected_regeneration(session, monkeypatch, worker_calls=None):
    from api.session_ops import plan_regeneration

    plan = plan_regeneration(session)
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )
    if worker_calls is not None:
        monkeypatch.setattr(
            routes,
            "_run_agent_streaming",
            lambda *args, **kwargs: worker_calls.append((args, kwargs)),
        )
    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session, regeneration=plan.turn)


def test_eager_rejected_start_retry_reload_has_one_user_prompt(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent))
    original_thread_start = threading.Thread.start
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("reject")),
    )
    with pytest.raises(RuntimeError, match="reject"):
        _start(session)
    assert _users(session) == []
    assert not session.path.exists()
    if models.SESSION_INDEX_FILE.exists():
        assert all(row.get("session_id") != session.session_id for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8")))
    monkeypatch.setattr(threading.Thread, "start", original_thread_start)
    _start(session)
    reloaded = models.Session.load(session.session_id)
    assert [row["content"] for row in _users(reloaded)] == ["retry me"]


def test_fresh_session_thread_start_failure_removes_sidecar_and_index(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent))
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session)

    assert not session.path.exists()
    if models.SESSION_INDEX_FILE.exists():
        assert all(row.get("session_id") != session.session_id for row in json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8")))


def test_rejected_start_preserves_persisted_composer_draft(transaction_env, monkeypatch):
    events = []
    session = new_session(workspace=str(transaction_env.parent))
    session.composer_draft = {"text": "keep this draft", "files": []}
    session.save(touch_updated_at=False, skip_index=True)
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session)

    reloaded = models.Session.load(session.session_id)
    assert reloaded.composer_draft == {"text": "keep this draft", "files": []}
    assert _users(reloaded) == []
    assert events == []


def test_rejected_first_send_preserves_preexisting_empty_sidecar_and_index(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent), profile="profile-a", project_id="project-a")
    session.enabled_toolsets = ["workspace"]
    session.save(touch_updated_at=False)
    before_sidecar = session.path.read_bytes()
    before_index = models.SESSION_INDEX_FILE.read_bytes()
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session)

    assert session.path.read_bytes() == before_sidecar
    assert models.SESSION_INDEX_FILE.read_bytes() == before_index
    reloaded = models.Session.load(session.session_id)
    assert reloaded.profile == "profile-a"
    assert reloaded.project_id == "project-a"
    assert reloaded.enabled_toolsets == ["workspace"]
    assert _users(reloaded) == []


def test_rejected_start_does_not_leave_rejected_prompt_in_sidecar_backup(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent), profile="profile-a")
    session.messages = [
        {"role": "user", "content": "previous"},
        {"role": "assistant", "content": "answer"},
    ]
    session.save(touch_updated_at=False)
    session.messages = [{"role": "user", "content": "previous"}]
    session.save(touch_updated_at=False)
    backup = session.path.with_suffix(".json.bak")
    before_backup = backup.read_bytes()
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session)

    assert backup.read_bytes() == before_backup
    assert "retry me" not in backup.read_text(encoding="utf-8")


def test_rejected_regeneration_restores_backup_before_image_and_index(transaction_env, monkeypatch):
    session = _regeneration_session(transaction_env)
    before_sidecar = session.path.read_bytes()
    backup = session.path.with_suffix(".json.bak")
    before_backup = backup.read_bytes()
    before_index = models.SESSION_INDEX_FILE.read_bytes()
    worker_calls = []

    _rejected_regeneration(session, monkeypatch, worker_calls)

    assert session.path.read_bytes() == before_sidecar
    assert backup.read_bytes() == before_backup
    assert models.SESSION_INDEX_FILE.read_bytes() == before_index
    assert worker_calls == []


@pytest.mark.parametrize("backup_state", ["absent", "empty"])
def test_rejected_regeneration_restores_backup_physical_prestate(
    transaction_env, monkeypatch, backup_state
):
    session = _regeneration_session(transaction_env, backup_state=backup_state)
    before_sidecar = session.path.read_bytes()
    before_index = models.SESSION_INDEX_FILE.read_bytes()
    backup = session.path.with_suffix(".json.bak")

    _rejected_regeneration(session, monkeypatch)

    assert session.path.read_bytes() == before_sidecar
    assert models.SESSION_INDEX_FILE.read_bytes() == before_index
    if backup_state == "absent":
        assert not backup.exists()
    else:
        assert backup.exists()
        assert backup.read_bytes() == b""


def test_successful_regeneration_keeps_normal_backup_rotation(transaction_env, monkeypatch):
    from api.session_ops import plan_regeneration

    session = _regeneration_session(transaction_env)
    before_sidecar = session.path.read_bytes()
    plan = plan_regeneration(session)
    worker_done = threading.Event()
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: worker_done.set())

    result = _start(session, regeneration=plan.turn)

    assert result["session_id"] == session.session_id
    assert worker_done.wait(2)
    assert len(session.messages) == 3
    assert session.path.with_suffix(".json.bak").read_bytes() == before_sidecar


@pytest.mark.parametrize("backup_state", ["absent", "empty", "six"])
def test_rejected_regeneration_retains_backup_when_sidecar_compensation_fails(
    transaction_env, monkeypatch, backup_state
):
    from api.session_ops import plan_regeneration

    journal = __import__("api.turn_journal", fromlist=["read_turn_journal"])
    session = _regeneration_session(transaction_env, backup_state=backup_state)
    before_sidecar = session.path.read_bytes()
    before_messages = copy.deepcopy(session.messages)
    before_context_messages = copy.deepcopy(session.context_messages)
    before_index = models.SESSION_INDEX_FILE.read_bytes()
    plan = plan_regeneration(session)
    backup = session.path.with_suffix(".json.bak")
    backup_replace_calls = []
    backup_unlink_calls = []
    checkpoint_sidecar = []
    sidecar_failure_seen = False
    real_replace = models._safe_replace

    def record_backup_replace(source, destination):
        if Path(destination) == backup:
            backup_replace_calls.append(sidecar_failure_seen)
        return real_replace(source, destination)

    monkeypatch.setattr(models, "_safe_replace", record_backup_replace)
    real_unlink = Path.unlink

    def record_backup_unlink(path, *args, **kwargs):
        if path == backup:
            backup_unlink_calls.append(sidecar_failure_seen)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", record_backup_unlink)
    real_save = models.Session.save
    save_calls = []
    worker_calls = []

    def fail_sidecar_compensation(self, *args, **kwargs):
        nonlocal sidecar_failure_seen
        save_calls.append(kwargs)
        if kwargs.get("skip_backup") is True:
            sidecar_failure_seen = True
            raise OSError("sidecar compensation failed")
        result = real_save(self, *args, **kwargs)
        checkpoint_sidecar.append(self.path.read_bytes())
        return result

    monkeypatch.setattr(models.Session, "save", fail_sidecar_compensation)
    monkeypatch.setattr(
        routes,
        "_run_agent_streaming",
        lambda *args, **kwargs: worker_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError) as exc_info:
        _start(session, regeneration=plan.turn)

    message = str(exc_info.value)
    assert "thread start rejected" in message
    assert "sidecar compensation failed" in message
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "thread start rejected"
    assert len(save_calls) == 2
    assert save_calls[0].get("skip_backup") is not True
    assert save_calls[1].get("skip_backup") is True
    assert checkpoint_sidecar and checkpoint_sidecar[0] != before_sidecar
    assert backup_replace_calls == [False]
    assert backup_unlink_calls == []
    assert worker_calls == []
    assert session.path.read_bytes() == checkpoint_sidecar[0]
    assert models.SESSION_INDEX_FILE.read_bytes() == before_index
    assert backup.exists()
    assert backup.read_bytes() == before_sidecar
    backup_payload = json.loads(backup.read_text(encoding="utf-8"))
    live_payload = json.loads(session.path.read_text(encoding="utf-8"))
    assert backup_payload["messages"] == before_messages
    assert backup_payload["context_messages"] == before_context_messages
    assert live_payload["messages"] != before_messages
    assert live_payload["context_messages"] != before_context_messages
    assert not config.STREAMS
    assert not config.STREAM_GOAL_RELATED
    assert not config.STREAM_SESSION_OWNERS
    assert not config.SESSION_WRITEBACK_OWNERS
    assert [event["event"] for event in journal.read_turn_journal(session.session_id)["events"]] == [
        "submitted",
        "interrupted",
    ]
    assert not list(backup.parent.glob(f".{backup.name}.*.tmp"))


@pytest.mark.parametrize("failure_point", ["write", "flush", "fsync", "replace"])
def test_backup_restore_tempfile_failures_settle_journal_and_clean_up(
    transaction_env, monkeypatch, failure_point
):
    from api.session_ops import plan_regeneration

    journal = __import__("api.turn_journal", fromlist=["read_turn_journal"])
    session = _regeneration_session(transaction_env)
    before_sidecar = session.path.read_bytes()
    plan = plan_regeneration(session)
    backup = session.path.with_suffix(".json.bak")
    restore_fds = set()
    real_named_temporary_file = routes.tempfile.NamedTemporaryFile

    class FailingTemporaryFile:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.name = wrapped.name

        def __enter__(self):
            self._wrapped.__enter__()
            return self

        def __exit__(self, *args):
            fd = self._wrapped.fileno()
            try:
                return self._wrapped.__exit__(*args)
            finally:
                restore_fds.discard(fd)

        def write(self, data):
            if failure_point == "write":
                raise OSError("backup restore write failed")
            return self._wrapped.write(data)

        def flush(self):
            if failure_point == "flush":
                raise OSError("backup restore flush failed")
            return self._wrapped.flush()

        def fileno(self):
            return self._wrapped.fileno()

    def named_temporary_file(*args, **kwargs):
        wrapped = real_named_temporary_file(*args, **kwargs)
        restore_fds.add(wrapped.fileno())
        return FailingTemporaryFile(wrapped)

    monkeypatch.setattr(routes.tempfile, "NamedTemporaryFile", named_temporary_file)
    real_fsync = routes.os.fsync

    def fsync(fd):
        if failure_point == "fsync" and fd in restore_fds:
            raise OSError("backup restore fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(routes.os, "fsync", fsync)
    if failure_point == "replace":
        real_safe_replace = models._safe_replace
        backup_replace_calls = []

        def safe_replace(source, destination):
            if Path(destination) == backup:
                backup_replace_calls.append((Path(source), Path(destination)))
                if len(backup_replace_calls) == 2:
                    raise OSError("backup restore replace failed")
            return real_safe_replace(source, destination)

        monkeypatch.setattr(models, "_safe_replace", safe_replace)
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError) as exc_info:
        _start(session, regeneration=plan.turn)

    message = str(exc_info.value)
    assert "thread start rejected" in message
    assert f"backup restore {failure_point} failed" in message
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "thread start rejected"
    assert session.path.read_bytes() == before_sidecar
    assert [event["event"] for event in journal.read_turn_journal(session.session_id)["events"]] == [
        "submitted",
        "interrupted",
    ]
    if failure_point == "replace":
        assert len(backup_replace_calls) == 2
    assert not list(backup.parent.glob(f".{backup.name}.*.tmp"))


def test_backup_restore_unlink_failure_reports_compensation_and_settles_journal(
    transaction_env, monkeypatch
):
    from api.session_ops import plan_regeneration

    journal = __import__("api.turn_journal", fromlist=["read_turn_journal"])
    session = _regeneration_session(transaction_env, backup_state="absent")
    plan = plan_regeneration(session)
    backup = session.path.with_suffix(".json.bak")
    real_unlink = Path.unlink

    def fail_backup_unlink(path, *args, **kwargs):
        if path == backup:
            raise OSError("backup restore unlink failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_backup_unlink)
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError) as exc_info:
        _start(session, regeneration=plan.turn)

    message = str(exc_info.value)
    assert "thread start rejected" in message
    assert "backup restore unlink failed" in message
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "thread start rejected"
    assert [event["event"] for event in journal.read_turn_journal(session.session_id)["events"]] == [
        "submitted",
        "interrupted",
    ]


def test_backup_read_failure_aborts_before_admission_mutation(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent))
    before = copy.deepcopy(session.__dict__)
    backup = session.path.with_suffix(".json.bak")
    real_read_bytes = Path.read_bytes
    prepare_calls = []

    def fail_backup_read(path):
        if path == backup:
            raise OSError("backup unreadable")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_backup_read)

    with pytest.raises(OSError, match="backup unreadable"):
        routes._commit_chat_start_admission(
            session,
            prepare=lambda *_args: prepare_calls.append(True),
            workspace="/tmp/workspace",
            model=session.model,
            model_provider=session.model_provider,
            normalized_model=False,
            goal_related=False,
            backend_is_gateway=False,
            moa_config=None,
            diag=None,
        )

    assert session.__dict__ == before
    assert prepare_calls == []
    assert not session.path.exists()


@pytest.mark.parametrize("prestate", ["both", "sidecar_only", "index_only", "neither"])
def test_rejected_start_uses_physical_sidecar_prestate(transaction_env, monkeypatch, prestate):
    session = new_session(workspace=str(transaction_env.parent), profile="profile-a", project_id="project-a")
    session.enabled_toolsets = ["workspace"]
    if prestate in {"both", "sidecar_only", "index_only"}:
        session.save(touch_updated_at=False, skip_index=prestate == "sidecar_only")
    if prestate == "index_only":
        session.path.unlink()
    before_sidecar = session.path.read_bytes() if session.path.exists() else None
    before_index = models.SESSION_INDEX_FILE.read_bytes() if models.SESSION_INDEX_FILE.exists() else None
    monkeypatch.setattr(
        threading.Thread,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("thread start rejected")),
    )

    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session)

    if before_sidecar is None:
        assert not session.path.exists()
    else:
        assert session.path.read_bytes() == before_sidecar
    if before_index is None:
        assert not models.SESSION_INDEX_FILE.exists()
    else:
        assert models.SESSION_INDEX_FILE.read_bytes() == before_index


def test_ordinary_start_survives_turn_journal_append_failure(transaction_env, monkeypatch):
    journal = __import__("api.turn_journal", fromlist=["append_turn_journal_event"])
    monkeypatch.setattr(
        journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )
    done = threading.Event()
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: done.set())
    session = new_session(workspace=str(transaction_env.parent))

    response = _start(session)

    assert response["session_id"] == session.session_id
    assert response["turn_id"] is None
    assert done.wait(2)
    assert session.path.exists()
    assert [row["content"] for row in _users(models.Session.load(session.session_id))] == ["retry me"]
    assert config.session_writeback_owner(session.session_id) == response["stream_id"]


def test_strict_regeneration_callsite_requires_journal(transaction_env, monkeypatch):
    captured = {}
    turn = type("Turn", (), {"revision": "revision", "message_text": "retry me", "attachments": []})()
    plan = type("Plan", (), {"turn": turn})()
    monkeypatch.setattr("api.session_ops.plan_regeneration", lambda *args, **kwargs: plan)
    monkeypatch.setattr(
        routes,
        "_commit_chat_start_admission",
        lambda _session, **kwargs: captured.update(kwargs) or {"ok": True},
    )
    session = new_session(workspace=str(transaction_env.parent))

    result = routes._start_regeneration_stream_locked(
        session,
        turn=turn,
        workspace="/tmp/workspace",
        model=session.model,
        model_provider=session.model_provider,
        normalized_model=False,
        diag=None,
        goal_related=False,
        source="webui",
        moa_config=None,
        backend_is_gateway=False,
    )

    assert result == {"ok": True}
    assert captured["journal_required"] is True


def test_partial_submitted_journal_is_interrupted_on_strict_failure(transaction_env, monkeypatch):
    journal = __import__("api.turn_journal", fromlist=["append_turn_journal_event"])
    real_append = journal.append_turn_journal_event

    def append_then_raise(session_id, event):
        real_append(session_id, event)
        raise OSError("journal fsync unavailable")

    monkeypatch.setattr(journal, "append_turn_journal_event", append_then_raise)
    session = new_session(workspace=str(transaction_env.parent))

    with pytest.raises(OSError, match="journal fsync unavailable"):
        routes._commit_chat_start_admission(
            session,
            prepare=lambda _stream_id, _goal_related: ("retry me", []),
            workspace="/tmp/workspace",
            model=session.model,
            model_provider=session.model_provider,
            normalized_model=False,
            goal_related=False,
            backend_is_gateway=False,
            moa_config=None,
            diag=None,
            journal_required=True,
        )

    events = journal.read_turn_journal(session.session_id)["events"]
    assert [event["event"] for event in events] == ["submitted", "interrupted"]


def test_strict_regeneration_journal_failure_compensates(transaction_env, monkeypatch):
    journal = __import__("api.turn_journal", fromlist=["append_turn_journal_event"])
    monkeypatch.setattr(
        journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )
    session = new_session(workspace=str(transaction_env.parent))

    def prepare(stream_id, _effective_goal_related):
        session.active_stream_id = stream_id
        session.pending_user_message = "regenerate me"
        session.pending_started_at = 1.0
        return "regenerate me", []

    with pytest.raises(OSError, match="journal unavailable"):
        routes._commit_chat_start_admission(
            session,
            prepare=prepare,
            workspace="/tmp/workspace",
            model=session.model,
            model_provider=session.model_provider,
            normalized_model=False,
            goal_related=False,
            backend_is_gateway=False,
            moa_config=None,
            diag=None,
            journal_required=True,
        )

    assert session.active_stream_id is None
    assert config.session_writeback_owner(session.session_id) is None
    assert not config.STREAMS


def test_worker_gate_is_untimed_and_does_not_leave_accepted_start_busy(transaction_env, monkeypatch):
    invoked = threading.Event()
    real_thread = threading.Thread

    class RecordingEvent:
        def __init__(self):
            self._event = threading.Event()

        def wait(self, *args, **kwargs):
            waits.append((args, kwargs))
            return self._event.wait(*args, **kwargs)

        def is_set(self):
            return self._event.is_set()

        def set(self):
            self._event.set()

    class DelayedThread:
        def __init__(self, *, target, args, daemon, kwargs):
            self._thread = real_thread(target=target, args=args, daemon=daemon)

        def start(self):
            self._thread.start()
            time.sleep(5.25)

        def join(self, timeout=None):
            self._thread.join(timeout)

    monkeypatch.setattr(routes.threading, "Thread", DelayedThread)
    waits = []
    monkeypatch.setattr(routes, "_ThreadEvent", RecordingEvent)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: invoked.set())
    session = new_session(workspace=str(transaction_env.parent))

    response = _start(session)

    assert response["session_id"] == session.session_id
    assert invoked.wait(2)
    assert waits == [((), {})]
    assert config.session_writeback_owner(session.session_id) == session.active_stream_id


def test_precommit_launch_then_raise_unconditionally_signals_abort_and_release(transaction_env, monkeypatch):
    events = []
    real_thread = threading.Thread

    class LaunchThenRaiseThread:
        def __init__(self, *, target, args, daemon, kwargs):
            self._target = target
            self._args = args
            self._thread = None

        def start(self):
            self._thread = real_thread(target=self._target, args=self._args, daemon=True)
            self._thread.start()
            raise RuntimeError("thread start reported failure after launch")

        def join(self, timeout=None):
            self._thread.join(timeout)

    original_event = routes._ThreadEvent

    class RecordingEvent:
        def __init__(self):
            self._event = original_event()
            self.set_calls = 0
            events.append(self)

        def wait(self, *args, **kwargs):
            return self._event.wait(*args, **kwargs)

        def is_set(self):
            return self._event.is_set()

        def set(self):
            self.set_calls += 1
            self._event.set()

    monkeypatch.setattr(routes.threading, "Thread", LaunchThenRaiseThread)
    monkeypatch.setattr(routes, "_ThreadEvent", RecordingEvent)
    executed = []
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: executed.append(True))
    session = new_session(workspace=str(transaction_env.parent))

    with pytest.raises(RuntimeError, match="thread start reported failure after launch"):
        _start(session)

    assert len(events) == 2
    assert all(event.is_set() for event in events)
    assert all(event.set_calls >= 1 for event in events)
    assert not executed
    assert not config.STREAMS
    assert config.session_writeback_owner(session.session_id) is None


@pytest.mark.parametrize("backend", [False, True])
def test_worker_waits_for_durable_acceptance_matrix(transaction_env, monkeypatch, backend):
    observed = []
    done = threading.Event()
    session = new_session(workspace=str(transaction_env.parent))

    def worker(*args, **kwargs):
        observed.append((json.loads(session.path.read_text(encoding="utf-8")), kwargs))
        done.set()

    target = "_run_gateway_chat_streaming" if backend else "_run_agent_streaming"
    monkeypatch.setattr(routes, target, worker)
    _start(session, external_runtime_owned=backend)
    assert done.wait(2)
    assert observed and [row["content"] for row in observed[0][0]["messages"] if row.get("role") == "user"] == ["retry me"]
    assert "start_gate" not in observed[0][1]


def test_deferred_mode_leaves_messages_uncheckpointed_until_worker(transaction_env, monkeypatch):
    config.cfg = {"webui": {"session_save_mode": "deferred"}}
    observed = []
    done = threading.Event()
    session = new_session(workspace=str(transaction_env.parent))
    def worker(*args, **kwargs):
        observed.append(json.loads(session.path.read_text(encoding="utf-8")))
        done.set()
    monkeypatch.setattr(routes, "_run_agent_streaming", worker)
    _start(session)
    assert done.wait(2)
    assert observed[0]["messages"] == []


@pytest.mark.parametrize("marker", ["goal", "background"])
def test_marker_claim_rollback_is_additive(transaction_env, monkeypatch, marker):
    session = new_session(workspace=str(transaction_env.parent))
    marker_set = routes.PENDING_GOAL_CONTINUATION if marker == "goal" else routes.PENDING_BG_TASK_COMPLETIONS
    marker_set.add(session.session_id)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: (_ for _ in ()).throw(RuntimeError("reject")))
    with pytest.raises(RuntimeError, match="reject"):
        _start(session)
    assert session.session_id in marker_set


def test_explicit_goal_related_start_does_not_claim_existing_goal_marker(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent))
    config.add_pending_goal_continuation(session.session_id)
    seen = []
    done = threading.Event()
    def worker(*args, **kwargs):
        seen.append(kwargs)
        done.set()
    monkeypatch.setattr(routes, "_run_agent_streaming", worker)
    _start(session, goal_related=True)
    assert session.session_id in routes.PENDING_GOAL_CONTINUATION
    assert done.wait(2)
    assert seen[0]["goal_related"] is True


def test_marker_added_after_claim_survives_rollback(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent))
    config.add_pending_goal_continuation(session.session_id)

    def reject_and_produce():
        config.add_pending_goal_continuation(session.session_id)
        raise RuntimeError("reject")

    monkeypatch.setattr(routes, "create_stream_channel", reject_and_produce)
    with pytest.raises(RuntimeError, match="reject"):
        _start(session)
    assert session.session_id in routes.PENDING_GOAL_CONTINUATION


def test_regeneration_admission_rejection_does_not_claim_markers(transaction_env, monkeypatch):
    from api.session_ops import RegenerationUnavailable

    session = new_session(workspace=str(transaction_env.parent))
    config.add_pending_goal_continuation(session.session_id)
    monkeypatch.setattr(
        "api.session_ops.plan_regeneration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RegenerationUnavailable("no_regenerable_turn")
        ),
    )
    result = routes._start_regeneration_stream_locked(
        session,
        turn=type("Turn", (), {"revision": "revision"})(),
        workspace="/tmp/workspace",
        model=session.model,
        model_provider=session.model_provider,
        normalized_model=False,
        diag=None,
        goal_related=False,
        source="webui",
        moa_config=None,
        backend_is_gateway=False,
    )
    assert result["_status"] == 409
    assert session.session_id in routes.PENDING_GOAL_CONTINUATION


@pytest.mark.parametrize("boundary", ["prepare", "channel", "owner", "save", "thread"])
def test_precommit_failure_compensation_matrix(transaction_env, monkeypatch, boundary):
    session = new_session(workspace=str(transaction_env.parent))
    if boundary == "prepare":
        monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(boundary)))
    elif boundary == "channel":
        monkeypatch.setattr(routes, "create_stream_channel", lambda: (_ for _ in ()).throw(RuntimeError(boundary)))
    elif boundary == "owner":
        monkeypatch.setattr(routes, "register_stream_owner", lambda *args: (_ for _ in ()).throw(RuntimeError(boundary)))
    elif boundary == "save":
        monkeypatch.setattr(session, "save", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(boundary)))
    else:
        monkeypatch.setattr(threading.Thread, "start", lambda _self: (_ for _ in ()).throw(RuntimeError(boundary)))
    with pytest.raises(RuntimeError, match=boundary):
        _start(session)
    assert session.active_stream_id is None
    assert not config.STREAMS
    assert config.session_writeback_owner(session.session_id) is None


def test_rejected_submission_is_interrupted_once(transaction_env, monkeypatch):
    events = []
    journal = __import__("api.turn_journal", fromlist=["append_turn_journal_event"])
    monkeypatch.setattr(journal, "append_turn_journal_event", lambda _sid, event: events.append(event) or {"turn_id": "turn-1"})
    monkeypatch.setattr(routes, "create_stream_channel", lambda: (_ for _ in ()).throw(RuntimeError("reject")))
    session = new_session(workspace=str(transaction_env.parent))
    with pytest.raises(RuntimeError, match="reject"):
        _start(session)
    assert [event["event"] for event in events] == ["submitted", "interrupted"]


def test_postcommit_failure_preserves_accepted_state(transaction_env, monkeypatch):
    observed = []
    done = threading.Event()
    session = new_session(workspace=str(transaction_env.parent))
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: (observed.append(kwargs), done.set()))
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: (_ for _ in ()).throw(RuntimeError("postcommit")))
    response = _start(session)
    assert response["session_id"] == session.session_id
    assert done.wait(2)
    assert observed
    assert session.active_stream_id
    assert config.session_writeback_owner(session.session_id) == session.active_stream_id


def test_session_list_publication_failure_does_not_reject_durable_start(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent))
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("notify failed")),
    )
    response = _start(session)
    assert response["session_id"] == session.session_id
    assert session.path.exists()


def test_draft_backed_first_send_publishes_session_new_once(transaction_env, monkeypatch):
    events = []
    session = new_session(workspace=str(transaction_env.parent), profile="profile-a")
    session.composer_draft = {"text": "draft text", "files": []}
    session.save(touch_updated_at=False)
    assert all(row.get("session_id") != session.session_id for row in models.all_sessions())
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    response = _start(session)

    assert response["session_id"] == session.session_id
    session_new_events = [event for event in events if event[0] == ("session_new",)]
    assert session_new_events == [
        (
            ("session_new",),
            {"profile": "profile-a", "session_id": session.session_id},
        )
    ]


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("active_stream_id", "stream-1"), ("pending_user_message", "pending")],
)
def test_active_or_pending_session_is_not_hidden(transaction_env, attribute, value):
    session = new_session(workspace=str(transaction_env.parent))
    setattr(session, attribute, value)

    assert routes._is_hidden_empty_session(session) is False


def test_session_index_publication_failure_does_not_reject_durable_start(transaction_env, monkeypatch):
    done = threading.Event()
    session = new_session(workspace=str(transaction_env.parent))
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: done.set())
    monkeypatch.setattr(
        routes,
        "_write_session_index",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("index unavailable")),
    )
    response = _start(session)
    assert response["session_id"] == session.session_id
    assert session.path.exists()
    assert done.wait(2)
    assert config.session_writeback_owner(session.session_id) == response["stream_id"]


def test_failed_admission_preserves_successor_owner_and_state(transaction_env, monkeypatch):
    session = new_session(workspace=str(transaction_env.parent))
    session.messages = [
        {"role": "user", "content": "previous"},
        {"role": "assistant", "content": "answer"},
    ]
    session.save(touch_updated_at=False)
    session.messages = [{"role": "user", "content": "previous"}]
    session.save(touch_updated_at=False)
    backup = session.path.with_suffix(".json.bak")
    before_backup = backup.read_bytes()
    successor = "successor-stream"
    successor_workspace = str(transaction_env / "successor-workspace")
    failed_stream = {}
    successor_backup = {}

    def install_successor_before_compensation(_self):
        failed_stream["id"] = session.active_stream_id
        config.register_session_writeback_owner(session.session_id, successor)
        session.pending_user_message = "successor"
        session.title = "successor title"
        session.workspace = successor_workspace
        session.successor_only_state = {"kept": True}
        session.messages = [{"role": "user", "content": "successor"}]
        session.context_messages = copy.deepcopy(session.messages)
        session.save()
        successor_backup["bytes"] = backup.read_bytes()
        raise RuntimeError("thread start rejected")

    monkeypatch.setattr(threading.Thread, "start", install_successor_before_compensation)
    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session)
    assert config.session_writeback_owner(session.session_id) == successor
    assert failed_stream["id"]
    assert session.active_stream_id == successor
    assert failed_stream["id"] not in config.STREAMS
    assert session.pending_user_message == "successor"
    assert session.title == "successor title"
    assert session.workspace == successor_workspace
    assert session.successor_only_state == {"kept": True}
    reloaded = models.Session.load(session.session_id)
    assert reloaded.pending_user_message == "successor"
    assert reloaded.title == "successor title"
    assert reloaded.workspace == successor_workspace
    assert reloaded.active_stream_id == successor
    assert successor_backup["bytes"] != before_backup
    assert backup.read_bytes() == successor_backup["bytes"]


def test_pathless_successor_compensation_uses_legacy_save_signature(transaction_env, monkeypatch):
    class PathlessSession(models.Session):
        @property
        def path(self):
            raise AttributeError("path is not available")

        def save(self, touch_updated_at=True):
            return None

    session = PathlessSession(
        session_id="pathless-successor-session",
        workspace=str(transaction_env.parent),
        model="test-model",
    )
    successor = "pathless-successor-stream"
    save_calls = []
    failure_save_count = {}

    def save(touch_updated_at=True):
        save_calls.append(touch_updated_at)

    session.save = save

    def install_successor(_self):
        config.register_session_writeback_owner(session.session_id, successor)
        session.pending_user_message = "successor"
        failure_save_count["count"] = len(save_calls)
        raise RuntimeError("thread start rejected")

    monkeypatch.setattr(threading.Thread, "start", install_successor)
    with pytest.raises(RuntimeError, match="thread start rejected"):
        _start(session)

    assert config.session_writeback_owner(session.session_id) == successor
    assert session.active_stream_id == successor
    assert len(save_calls) == failure_save_count["count"] + 1
