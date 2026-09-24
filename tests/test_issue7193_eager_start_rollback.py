"""Regression coverage for rejected ordinary eager chat starts."""

import copy
import json
import threading

import pytest

import api.config as config
import api.models as models
import api.routes as routes
from api.models import Session, new_session


@pytest.fixture
def issue7193_env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _session_id: None)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_run_gateway_chat_streaming", lambda *_args, **_kwargs: None)
    models.SESSIONS.clear()
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


def _start(session, *, workspace, **overrides):
    values = {
        "msg": "retry me",
        "attachments": [],
        "workspace": str(workspace),
        "model": session.model,
        "model_provider": session.model_provider,
        "external_runtime_owned": False,
    }
    values.update(overrides)
    return routes._start_chat_stream_for_session(session, **values)


def _user_rows(session):
    return [row for row in session.messages if row.get("role") == "user"]


def _saved_retry_session(issue7193_env):
    session = new_session(workspace=str(issue7193_env.parent), profile="profile-a")
    session.title = "Existing retry"
    session.messages = [
        {"role": "user", "content": "retry me", "timestamp": 1.0},
        {"role": "assistant", "content": "old answer", "timestamp": 2.0},
    ]
    session.context_messages = copy.deepcopy(session.messages)
    session.save(touch_updated_at=False)
    return session


def test_eager_rejected_before_stream_registration_retry_reload_has_one_new_prompt(
    issue7193_env, monkeypatch
):
    session = _saved_retry_session(issue7193_env)
    real_create_stream_channel = routes.create_stream_channel
    monkeypatch.setattr(
        routes,
        "create_stream_channel",
        lambda: (_ for _ in ()).throw(RuntimeError("stream registration rejected")),
    )

    with pytest.raises(RuntimeError, match="stream registration rejected"):
        _start(session, workspace=issue7193_env / "workspace")

    rejected = Session.load(session.session_id)
    assert [row["content"] for row in _user_rows(rejected)] == ["retry me"]

    monkeypatch.setattr(routes, "create_stream_channel", real_create_stream_channel)
    _start(session, workspace=issue7193_env / "workspace")

    reloaded = Session.load(session.session_id)
    assert [row["content"] for row in _user_rows(reloaded)] == [
        "retry me",
        "retry me",
    ]


def test_rejected_start_waits_for_concurrent_draft_writer_until_cleanup(
    issue7193_env, monkeypatch
):
    session = _saved_retry_session(issue7193_env)
    stream_creation_started = threading.Event()
    release_stream_creation = threading.Event()
    writer_started = threading.Event()
    writer_saved = threading.Event()

    def pause_stream_creation():
        stream_creation_started.set()
        assert release_stream_creation.wait(2)
        raise RuntimeError("stream registration rejected")

    def write_draft():
        writer_started.set()
        with routes._get_session_agent_lock(session.session_id):
            session.composer_draft = {"text": "draft saved during admission"}
            session.save(touch_updated_at=False)
            writer_saved.set()

    monkeypatch.setattr(routes, "create_stream_channel", pause_stream_creation)
    admission_error = []

    def start_chat():
        try:
            _start(session, workspace=issue7193_env / "workspace")
        except RuntimeError as exc:
            admission_error.append(exc)

    admission_thread = threading.Thread(target=start_chat)
    admission_thread.start()
    assert stream_creation_started.wait(2)

    writer_thread = threading.Thread(target=write_draft)
    writer_thread.start()
    assert writer_started.wait(2)
    writer_saved_before_release = writer_saved.wait(0.2)

    release_stream_creation.set()
    admission_thread.join(2)
    writer_thread.join(2)

    assert admission_error and str(admission_error[0]) == "stream registration rejected"
    assert writer_saved.is_set()
    reloaded = Session.load(session.session_id)
    assert reloaded.composer_draft == {"text": "draft saved during admission"}
    assert [row["content"] for row in _user_rows(reloaded)] == ["retry me"]
    assert not writer_saved_before_release


def test_rejected_start_waits_for_same_value_metadata_writer_until_cleanup(
    issue7193_env, monkeypatch
):
    session = _saved_retry_session(issue7193_env)
    stream_creation_started = threading.Event()
    release_stream_creation = threading.Event()
    writer_started = threading.Event()
    writer_saved = threading.Event()

    def pause_stream_creation():
        stream_creation_started.set()
        assert release_stream_creation.wait(2)
        raise RuntimeError("stream registration rejected")

    def write_metadata():
        writer_started.set()
        with routes._get_session_agent_lock(session.session_id):
            session.model = "prepared-model"
            session.model_provider = "prepared-provider"
            session.save(touch_updated_at=False)
            writer_saved.set()

    monkeypatch.setattr(routes, "create_stream_channel", pause_stream_creation)
    admission_error = []

    def start_chat():
        try:
            _start(
                session,
                workspace=issue7193_env / "workspace",
                model="prepared-model",
                model_provider="prepared-provider",
            )
        except RuntimeError as exc:
            admission_error.append(exc)

    admission_thread = threading.Thread(target=start_chat)
    admission_thread.start()
    assert stream_creation_started.wait(2)

    writer_thread = threading.Thread(target=write_metadata)
    writer_thread.start()
    assert writer_started.wait(2)
    writer_saved_before_release = writer_saved.wait(0.2)

    release_stream_creation.set()
    admission_thread.join(2)
    writer_thread.join(2)

    assert admission_error and str(admission_error[0]) == "stream registration rejected"
    assert writer_saved.is_set()
    reloaded = Session.load(session.session_id)
    assert reloaded.model == "prepared-model"
    assert reloaded.model_provider == "prepared-provider"
    assert [row["content"] for row in _user_rows(reloaded)] == ["retry me"]
    assert not writer_saved_before_release


def test_rejected_start_preserves_entry_recovery_backup_bytes(
    issue7193_env, monkeypatch
):
    from api.session_recovery import inspect_session_recovery_status

    session = new_session(workspace=str(issue7193_env.parent), profile="profile-a")
    session.title = "Recoverable session"
    six_messages = [
        {"role": "user", "content": f"prompt {index}", "timestamp": float(index)}
        for index in range(1, 4)
    ] + [
        {"role": "assistant", "content": "answer 1", "timestamp": 4.0},
        {"role": "user", "content": "prompt 4", "timestamp": 5.0},
        {"role": "assistant", "content": "answer 2", "timestamp": 6.0},
    ]
    session.messages = copy.deepcopy(six_messages)
    session.context_messages = copy.deepcopy(six_messages)
    session.save(touch_updated_at=False)
    four_messages = copy.deepcopy(six_messages[:4])
    session.messages = four_messages
    session.context_messages = copy.deepcopy(four_messages)
    session.save(touch_updated_at=False)
    backup_path = session.path.with_suffix(".json.bak")
    entry_backup = backup_path.read_bytes()
    assert inspect_session_recovery_status(session.path)["recommend"] == "restore"

    monkeypatch.setattr(
        routes,
        "create_stream_channel",
        lambda: (_ for _ in ()).throw(RuntimeError("stream registration rejected")),
    )
    with pytest.raises(RuntimeError, match="stream registration rejected"):
        _start(session, workspace=issue7193_env / "workspace")

    assert backup_path.read_bytes() == entry_backup
    status = inspect_session_recovery_status(session.path)
    assert status["recommend"] == "restore"
    assert status["bak_messages"] == 6


@pytest.mark.parametrize("existing_session", [True, False], ids=["existing", "hidden-empty"])
def test_rejected_eager_start_keeps_existing_recovery_contract(
    issue7193_env, monkeypatch, existing_session
):
    from api.session_recovery import (
        inspect_session_recovery_status,
        recover_all_sessions_on_startup,
    )

    session = (
        _saved_retry_session(issue7193_env)
        if existing_session
        else new_session(workspace=str(issue7193_env.parent), profile="profile-a")
    )
    original_messages = copy.deepcopy(session.messages)
    backup_path = session.path.with_suffix(".json.bak")
    entry_backup = backup_path.read_bytes() if backup_path.exists() else None
    monkeypatch.setattr(
        routes,
        "create_stream_channel",
        lambda: (_ for _ in ()).throw(RuntimeError("stream registration rejected")),
    )

    with pytest.raises(RuntimeError, match="stream registration rejected"):
        _start(session, workspace=issue7193_env / "workspace")

    live = Session.load(session.session_id)
    assert [row["content"] for row in _user_rows(live)] == (
        ["retry me"] if existing_session else []
    )
    assert not live.intentional_shrink_generation
    status = inspect_session_recovery_status(live.path)
    assert status["recommend"] == ("restore" if entry_backup is not None else "no_backup")
    if entry_backup is not None:
        assert backup_path.read_bytes() == entry_backup

    recovery = recover_all_sessions_on_startup(issue7193_env)

    assert recovery["restored"] == 0
    reloaded = Session.load(session.session_id)
    assert reloaded.messages == original_messages
    assert reloaded.pending_user_message is None


def test_save_replaces_sidecar_then_raises_restores_snapshot(issue7193_env, monkeypatch):
    session = _saved_retry_session(issue7193_env)
    before = copy.deepcopy(session.__dict__)
    before_sidecar = session.path.read_bytes()
    real_write_session_index = models._write_session_index
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("index publication failed")
        return real_write_session_index(*args, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", fail_once)

    with pytest.raises(OSError, match="index publication failed"):
        _start(session, workspace=issue7193_env / "workspace")

    state = copy.deepcopy(session.__dict__)
    assert state == before
    reloaded = Session.load(session.session_id)
    for field in (
        "title",
        "workspace",
        "model",
        "model_provider",
        "messages",
        "context_messages",
        "active_stream_id",
        "pending_user_message",
        "pending_attachments",
        "pending_started_at",
        "pending_user_source",
        "truncation_watermark",
        "truncation_boundary",
    ):
        assert getattr(reloaded, field) == before[field]
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    expected_persisted = json.loads(before_sidecar.decode("utf-8"))
    assert persisted == expected_persisted
    assert config.session_writeback_owner(session.session_id) is None
    assert not config.STREAM_SESSION_OWNERS
    assert not config.STREAMS
    assert not config.STREAM_GOAL_RELATED


def test_eager_thread_start_failure_retry_reload_has_one_new_prompt(issue7193_env, monkeypatch):
    session = _saved_retry_session(issue7193_env)
    original_start = threading.Thread.start

    def reject_start(_thread):
        raise RuntimeError("thread launch rejected")

    monkeypatch.setattr(threading.Thread, "start", reject_start)
    with pytest.raises(RuntimeError, match="thread launch rejected"):
        _start(session, workspace=issue7193_env / "workspace")

    rejected = Session.load(session.session_id)
    assert [row["content"] for row in _user_rows(rejected)] == ["retry me"]
    assert config.session_writeback_owner(session.session_id) is None
    assert not config.STREAM_SESSION_OWNERS
    assert not config.STREAMS

    monkeypatch.setattr(threading.Thread, "start", original_start)
    _start(session, workspace=issue7193_env / "workspace")

    reloaded = Session.load(session.session_id)
    assert [row["content"] for row in _user_rows(reloaded)] == ["retry me", "retry me"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Original title"),
        ("workspace", None),
        ("model", "original-model"),
        ("model_provider", "original-provider"),
        ("messages", [{"role": "user", "content": "saved message"}]),
        ("context_messages", [{"role": "assistant", "content": "saved context"}]),
        ("pending_user_message", "older pending"),
        ("pending_attachments", [{"name": "old.txt"}]),
        ("pending_started_at", 7.0),
        ("pending_user_source", "original-source"),
        ("truncation_watermark", 8.0),
        ("truncation_boundary", 2),
        ("post_compression_context_tokens_estimate", 99),
    ],
)
def test_rejected_start_restores_snapshot_field(issue7193_env, monkeypatch, field, value):
    session = _saved_retry_session(issue7193_env)
    if field == "workspace":
        value = str(issue7193_env.parent / "original-workspace")
    setattr(session, field, copy.deepcopy(value))
    session.save(touch_updated_at=False)
    before = copy.deepcopy(session.__dict__)
    monkeypatch.setattr(
        routes,
        "create_stream_channel",
        lambda: (_ for _ in ()).throw(RuntimeError("stream registration rejected")),
    )

    with pytest.raises(RuntimeError, match="stream registration rejected"):
        _start(session, workspace=issue7193_env / "new-workspace")

    assert getattr(session, field) == before[field]
    reloaded = Session.load(session.session_id)
    assert getattr(reloaded, field) == before[field]


def test_accepted_eager_and_deferred_modes_keep_current_sidecar_contract(issue7193_env, monkeypatch):
    eager = new_session(workspace=str(issue7193_env.parent))
    _start(eager, workspace=issue7193_env / "eager-workspace")
    eager_disk = Session.load(eager.session_id)
    assert [row["content"] for row in _user_rows(eager_disk)] == ["retry me"]
    assert eager_disk.pending_user_message == "retry me"

    config.cfg = {"webui": {"session_save_mode": "deferred"}}
    deferred = new_session(workspace=str(issue7193_env.parent))
    _start(deferred, workspace=issue7193_env / "deferred-workspace")
    deferred_disk = Session.load(deferred.session_id)
    assert deferred_disk.messages == []
    assert deferred_disk.pending_user_message == "retry me"


def test_rollback_save_failure_preserves_original_initialization_error(issue7193_env, monkeypatch):
    session = _saved_retry_session(issue7193_env)
    before_sidecar = session.path.read_bytes()
    backup_path = session.path.with_suffix(".json.bak")
    real_save = models.Session.save
    save_calls = []

    def fail_compensation(self, *args, **kwargs):
        save_calls.append(kwargs)
        if len(save_calls) == 2:
            real_save(self, *args, **kwargs)
            raise OSError("rollback save failed")
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", fail_compensation)
    monkeypatch.setattr(
        routes,
        "create_stream_channel",
        lambda: (_ for _ in ()).throw(RuntimeError("stream registration rejected")),
    )

    with pytest.raises(RuntimeError, match="stream registration rejected"):
        _start(session, workspace=issue7193_env / "new-workspace")

    assert len(save_calls) == 2
    assert save_calls[1].get("touch_updated_at") is False
    assert backup_path.exists()
    assert json.loads(backup_path.read_bytes()) == json.loads(before_sidecar)


def test_journal_append_failure_is_best_effort(issue7193_env, monkeypatch):
    import api.turn_journal as turn_journal

    real_open = turn_journal.os.open

    def fail_journal_open(path, *args, **kwargs):
        if "_turn_journal" in str(path):
            raise OSError("journal filesystem unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(turn_journal.os, "open", fail_journal_open)
    session = new_session(workspace=str(issue7193_env.parent))

    response = _start(session, workspace=issue7193_env / "workspace")

    assert response["session_id"] == session.session_id
    assert response["turn_id"] is None
    assert session.active_stream_id == response["stream_id"]
    assert session.pending_user_message == "retry me"


def test_last_workspace_write_failure_is_best_effort(issue7193_env, monkeypatch):
    import api.workspace as workspace

    real_set_last_workspace = workspace.set_last_workspace
    monkeypatch.setattr(routes, "set_last_workspace", real_set_last_workspace)
    real_write_text = workspace.Path.write_text

    def fail_last_workspace(path, *args, **kwargs):
        if path.name == "last_workspace.txt":
            raise OSError("last workspace filesystem unavailable")
        return real_write_text(path, *args, **kwargs)

    monkeypatch.setattr(workspace.Path, "write_text", fail_last_workspace)
    session = new_session(workspace=str(issue7193_env.parent))

    response = _start(session, workspace=issue7193_env / "workspace")

    assert response["session_id"] == session.session_id
    assert session.active_stream_id == response["stream_id"]
    assert session.pending_user_message == "retry me"


def test_session_new_listener_and_full_queue_failures_are_best_effort(issue7193_env, monkeypatch):
    import api.session_events as session_events

    queue = session_events.subscribe_session_events()
    queue.put_nowait({"reason": "already full"})
    listener_calls = []

    def failing_listener(profile):
        listener_calls.append(profile)
        raise RuntimeError("listener failed")

    session_events.add_session_list_changed_listener(failing_listener)
    monkeypatch.setattr(routes, "publish_session_list_changed", session_events.publish_session_list_changed)
    try:
        session = new_session(workspace=str(issue7193_env.parent), profile="profile-a")
        assert routes._is_hidden_empty_session(session)
        response = _start(session, workspace=issue7193_env / "workspace")
        published = queue.get_nowait()
    finally:
        session_events.remove_session_list_changed_listener(failing_listener)
        session_events.unsubscribe_session_events(queue)

    assert response["session_id"] == session.session_id
    assert published["reason"] == "session_new"
    assert session.active_stream_id == response["stream_id"]
    assert session.pending_user_message == "retry me"
    assert listener_calls == ["profile-a"]


def test_response_construction_failure_after_thread_start_does_not_cleanup(issue7193_env, monkeypatch):
    import api.turn_journal as turn_journal

    started = threading.Event()
    cleanup_calls = []

    class RaisingJournalEvent:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("response construction failed")

    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_args, **_kwargs: started.set())
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *_args, **_kwargs: RaisingJournalEvent(),
    )
    monkeypatch.setattr(
        routes,
        "_cleanup_chat_start_launch_failure",
        lambda *args, **kwargs: cleanup_calls.append((args, kwargs)),
    )
    session = new_session(workspace=str(issue7193_env.parent))

    with pytest.raises(RuntimeError, match="response construction failed"):
        _start(session, workspace=issue7193_env / "workspace")

    assert started.wait(2)
    assert cleanup_calls == []
    assert session.active_stream_id
    assert config.session_writeback_owner(session.session_id) == session.active_stream_id
    assert config.STREAM_SESSION_OWNERS[session.active_stream_id] == session.session_id
