"""Route-level regression coverage for best-effort run-journal admission.

The run journal must not become a brick-class dependency for starting work.
Only a valid retired authority record blocks admission; corrupt, unreadable, or
unwritable authority degrades the run to an unjournaled execution.
"""

from __future__ import annotations

import copy
import gc
import json
import threading
import weakref
from pathlib import Path

import pytest

from api import models, routes, run_journal, turn_journal


_REAL_THREAD = threading.Thread
_ORIGINAL_PREPARE_CHAT_START = routes._prepare_chat_start_session_for_stream


class _Session:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.active_stream_id = None
        self.workspace = "/tmp/workspace"
        self.model = "test-model"
        self.model_provider = None
        self.profile = None
        self.messages = []
        self.title = "Untitled"
        self.pending_started_at = 1.0

    def save(self):
        return None


class _Thread:
    created = []

    def __init__(self, *, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.__class__.created.append(self)

    def start(self):
        return None


class _PersistentSession(_Session):
    """Small sidecar stand-in that keeps each save observable."""

    _STATE_FIELDS = (
        "active_stream_id",
        "pending_user_message",
        "pending_attachments",
        "pending_started_at",
        "pending_user_source",
        "title",
        "messages",
        "truncation_watermark",
        "workspace",
        "model",
        "model_provider",
        "post_compression_context_tokens_estimate",
        "updated_at",
    )

    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.active_stream_id = None
        self.pending_user_message = "old pending"
        self.pending_attachments = [{"name": "old.txt"}]
        self.pending_started_at = 42.0
        self.pending_user_source = "old-source"
        self.title = "Untitled"
        self.messages = [{"role": "assistant", "content": "before"}]
        self.truncation_watermark = 123.0
        self.workspace = "/old/workspace"
        self.model = "old-model"
        self.model_provider = "old-provider"
        self.post_compression_context_tokens_estimate = 77
        self.updated_at = 10.0
        self.save_calls = []
        self.fail_save_call = None
        self.persisted = self.snapshot()

    def snapshot(self):
        return {
            field: copy.deepcopy(getattr(self, field, None))
            for field in self._STATE_FIELDS
        }

    def save(self, touch_updated_at=True, **_kwargs):
        if self.fail_save_call is not None and len(self.save_calls) + 1 == self.fail_save_call:
            raise OSError("session rollback write failed")
        if touch_updated_at:
            self.updated_at = 100.0 + len(self.save_calls)
        persisted = self.snapshot()
        self.save_calls.append(persisted)
        self.persisted = copy.deepcopy(persisted)


def _authority_path(root: Path, session_id: str) -> Path:
    return root / "_run_journal" / ".incarnations" / f"{session_id}.json"


def _install_authority_failure(monkeypatch, root: Path, session_id: str, mode: str):
    path = _authority_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "corrupt":
        path.write_text("{not-json", encoding="ascii")
    elif mode == "unreadable":
        path.write_text("present", encoding="ascii")
        real_read_text = Path.read_text

        def deny_authority_read(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("authority denied")
            return real_read_text(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", deny_authority_read)
    elif mode == "unwritable":
        def deny_authority_write(_path, _incarnation, *, state):
            raise OSError("authority write denied")

        monkeypatch.setattr(
            run_journal,
            "_write_run_journal_incarnation",
            deny_authority_write,
        )
    elif mode == "retired":
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "session_id": session_id,
                    "state": "retired",
                    "incarnation": "0" * 32,
                }
            ),
            encoding="ascii",
        )
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown authority mode: {mode}")


@pytest.fixture
def route_harness(monkeypatch, tmp_path):
    original_sessions = list(models.SESSIONS.items())
    models.SESSIONS.clear()
    _Thread.created = []
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _session: False)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "STREAMS", {})
    monkeypatch.setattr(routes.threading, "Thread", _Thread)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        routes,
        "_prepare_chat_start_session_for_stream",
        lambda session, **kwargs: setattr(session, "active_stream_id", kwargs["stream_id"]),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: {**payload, "_status": status},
    )
    yield tmp_path
    models.SESSIONS.clear()
    models.SESSIONS.update(original_sessions)


@pytest.fixture
def real_session_store(route_harness, monkeypatch):
    session_dir = route_harness / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    original_sessions = list(models.SESSIONS.items())
    models.SESSIONS.clear()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    yield session_dir, index_file
    models.SESSIONS.clear()
    models.SESSIONS.update(original_sessions)


def _use_real_writeback_owner_registry(monkeypatch):
    owners = {}
    monkeypatch.setattr(
        routes,
        "register_session_writeback_owner",
        lambda session_id, stream_id: owners.__setitem__(session_id, stream_id),
    )
    monkeypatch.setattr(routes, "session_writeback_owner", lambda session_id: owners.get(session_id))
    monkeypatch.setattr(
        routes,
        "clear_session_writeback_owner_if_owned",
        lambda session_id, stream_id: owners.pop(session_id, None)
        if owners.get(session_id) == stream_id
        else None,
    )
    return owners


@pytest.mark.parametrize("mode", ["corrupt", "unreadable", "unwritable"])
def test_send_degrades_authority_failure_to_unjournaled_execution(
    route_harness, monkeypatch, mode
):
    session = _Session("send-authority")
    _install_authority_failure(monkeypatch, route_harness, session.session_id, mode)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace=session.workspace,
        model=session.model,
        external_runtime_owned=False,
    )

    assert response.get("_status", 200) == 200
    assert _Thread.created[-1].kwargs["run_journal_incarnation"] is None


def test_send_valid_retired_authority_still_blocks(route_harness, monkeypatch):
    session = _Session("send-retired")
    _install_authority_failure(monkeypatch, route_harness, session.session_id, "retired")

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace=session.workspace,
        model=session.model,
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert not _Thread.created


@pytest.mark.parametrize("save_mode", ["deferred", "eager"])
def test_retired_activation_rolls_back_prepared_stream_and_allows_retry(
    route_harness, monkeypatch, save_mode
):
    """A retired authority after prepare must leave no abandoned pending turn."""
    session = _PersistentSession(f"send-retired-after-prepare-{save_mode}")
    before = session.snapshot()
    owners = {}

    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: save_mode)
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(
        routes,
        "register_session_writeback_owner",
        lambda session_id, stream_id: owners.__setitem__(session_id, stream_id),
    )
    monkeypatch.setattr(
        routes,
        "clear_session_writeback_owner_if_owned",
        lambda session_id, stream_id: owners.pop(session_id, None)
        if owners.get(session_id) == stream_id
        else None,
    )
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    activation_results = [
        run_journal.RunJournalRetiredAuthorityError("retired"),
        "incarnation-after-retry",
    ]

    def activate(_sid):
        result = activation_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(run_journal, "activate_run_journal_session", activate)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert session.snapshot() == before
    assert session.persisted == before
    assert owners == {}

    retry = routes._start_chat_stream_for_session(
        session,
        msg="hello again",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert retry["stream_id"]
    assert retry["session_id"] == session.session_id
    assert len(_Thread.created) == 1


def test_retired_activation_rollback_failure_fails_closed(route_harness, monkeypatch):
    session = _PersistentSession("send-retired-rollback-failure")
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    monkeypatch.setattr(
        run_journal,
        "activate_run_journal_session",
        lambda _sid: (_ for _ in ()).throw(
            run_journal.RunJournalRetiredAuthorityError("retired")
        ),
    )
    # Prepare performs save #1; force the compensating persistence write to fail.
    session.fail_save_call = 2

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] != 409
    assert response.get("retryable") is not True
    assert response["type"] == "run_journal_authority_rollback_failed"


@pytest.mark.parametrize("rotation", ["active_stream", "writeback_owner"])
def test_retired_rollback_does_not_clobber_successor_rotation(
    route_harness, monkeypatch, rotation
):
    session = _PersistentSession(f"send-retired-successor-{rotation}")
    owners = {}
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(
        routes,
        "register_session_writeback_owner",
        lambda session_id, stream_id: owners.__setitem__(session_id, stream_id),
    )
    monkeypatch.setattr(routes, "session_writeback_owner", lambda session_id: owners.get(session_id))
    monkeypatch.setattr(
        routes,
        "clear_session_writeback_owner_if_owned",
        lambda session_id, stream_id: owners.pop(session_id, None)
        if owners.get(session_id) == stream_id
        else None,
    )
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)

    def activate(_sid):
        successor_stream_id = "successor-stream"
        if rotation == "active_stream":
            session.active_stream_id = successor_stream_id
        owners[session.session_id] = successor_stream_id
        session.pending_user_message = "successor pending"
        session.save(touch_updated_at=False)
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", activate)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert owners[session.session_id] == "successor-stream"
    assert session.pending_user_message == "successor pending"
    if rotation == "active_stream":
        assert session.active_stream_id == "successor-stream"
    else:
        assert session.active_stream_id
    assert session.persisted["pending_user_message"] == "successor pending"


def _configure_retired_activation_after_real_prepare(monkeypatch):
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    monkeypatch.setattr(
        run_journal,
        "activate_run_journal_session",
        lambda _sid: (_ for _ in ()).throw(
            run_journal.RunJournalRetiredAuthorityError("retired")
        ),
    )


def test_real_eager_rollback_restores_sidecar_and_existing_backup(
    real_session_store, monkeypatch
):
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(
        session_id="real-eager-existing",
        title="Existing title",
        workspace="/old/workspace",
        model="old-model",
        model_provider="old-provider",
        messages=[{"role": "assistant", "content": "before"}],
        pending_user_message="old pending",
        pending_attachments=[{"name": "old.txt"}],
        pending_started_at=42.0,
        pending_user_source="old-source",
        truncation_watermark=123.0,
        post_compression_context_tokens_estimate=77,
    )
    session.save(touch_updated_at=False)
    sidecar_before = session.path.read_bytes()
    backup_before = b'{"preexisting":true}'
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(backup_before)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert session.path.read_bytes() == sidecar_before
    assert backup_path.read_bytes() == backup_before
    index_rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    row = next(item for item in index_rows if item["session_id"] == session.session_id)
    assert row["title"] == "Existing title"
    assert row["message_count"] == 1


def test_real_eager_rollback_removes_fresh_sidecar_and_backup(
    real_session_store, monkeypatch
):
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(session_id="real-eager-fresh", title="Untitled", messages=[])
    assert not session.path.exists()
    backup_path = session.path.with_suffix(".json.bak")

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert not session.path.exists()
    assert not backup_path.exists()
    assert json.loads(real_session_store[1].read_text(encoding="utf-8")) == []


def test_real_rollback_refuses_external_sidecar_rotation(real_session_store, monkeypatch):
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(
        session_id="real-eager-rotated",
        title="Existing title",
        messages=[{"role": "assistant", "content": "before"}],
    )
    session.save(touch_updated_at=False)
    backup_path = session.path.with_suffix(".json.bak")

    def rotate_then_retire(_sid):
        session.path.write_bytes(b"external-sidecar-rotation")
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", rotate_then_retire)
    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert session.path.read_bytes() == b"external-sidecar-rotation"
    assert not backup_path.exists()


@pytest.mark.parametrize("failure_stage", ["sidecar", "backup", "index"])
def test_chat_start_restore_failure_quarantines_exact_real_session(
    real_session_store, monkeypatch, failure_stage
):
    """Any partial durable restore failure quarantines the prepared owner."""
    _configure_retired_activation_after_real_prepare(monkeypatch)
    owners = _use_real_writeback_owner_registry(monkeypatch)
    sid = "chat-start-restore-failure-quarantine"
    session = models.Session(
        session_id=sid,
        title="Existing title",
        workspace="/old/workspace",
        model="old-model",
        messages=[{"role": "assistant", "content": "before"}],
    )
    models.SESSIONS[sid] = session
    session.save(touch_updated_at=False)
    retired_generation = session._persistence_generation
    captured_same_generation = models.Session(session_id=sid, title="captured")
    assert captured_same_generation._persistence_generation is retired_generation
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(b"preexisting-backup")

    def retire_after_prepare(_sid):
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", retire_after_prepare)

    if failure_stage in {"sidecar", "backup"}:
        real_restore = routes._restore_chat_start_file
        restore_calls = []

        def fail_restore(snapshot):
            restore_calls.append(snapshot["path"])
            if len(restore_calls) == (1 if failure_stage == "sidecar" else 2):
                raise OSError(f"rollback {failure_stage} restore denied")
            return real_restore(snapshot)

        monkeypatch.setattr(routes, "_restore_chat_start_file", fail_restore)
    else:
        monkeypatch.setattr(
            routes,
            "_settle_chat_start_session_index_locked",
            lambda *_args, **_kwargs: False,
        )
    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        external_runtime_owned=False,
    )

    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert owners == {}
    assert sid not in models.SESSIONS
    assert session._persistence_revoked is True
    assert "_persistence_revoked" not in session.__dict__
    assert retired_generation.revoked is True
    durable_after_failure = {
        "sidecar": session.path.read_bytes() if session.path.exists() else None,
        "backup": backup_path.read_bytes() if backup_path.exists() else None,
        "index": real_session_store[1].read_bytes()
        if real_session_store[1].exists()
        else None,
    }
    session.title = "stale overwrite attempt"
    with pytest.raises(RuntimeError, match="persistence-revoked"):
        session.save(touch_updated_at=False)
    captured_same_generation.title = "second stale overwrite attempt"
    with pytest.raises(RuntimeError, match="revoked|deleted"):
        captured_same_generation.save(touch_updated_at=False)
    assert (
        session.path.read_bytes() if session.path.exists() else None
    ) == durable_after_failure["sidecar"]
    assert (
        backup_path.read_bytes() if backup_path.exists() else None
    ) == durable_after_failure["backup"]
    assert (
        real_session_store[1].read_bytes()
        if real_session_store[1].exists()
        else None
    ) == durable_after_failure["index"]

    reloaded = models.Session.load(sid)
    assert reloaded is not session
    assert reloaded._persistence_generation is not retired_generation
    assert reloaded._persistence_generation.revoked is False
    assert reloaded.path.read_bytes() == durable_after_failure["sidecar"]


def test_chat_start_rollback_refuses_distinct_canonical_owner(
    real_session_store, monkeypatch
):
    """A rejected turn cannot roll back after the in-memory owner rotates."""
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(
        session_id="chat-start-distinct-owner",
        title="Existing title",
        messages=[{"role": "assistant", "content": "before"}],
    )
    models.SESSIONS[session.session_id] = session
    session.save(touch_updated_at=False)
    successor = models.Session(
        session_id=session.session_id,
        title="Successor title",
        messages=[{"role": "assistant", "content": "successor"}],
    )

    def rotate_owner_then_retire(_sid):
        with models.LOCK:
            models.SESSIONS[session.session_id] = successor
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", rotate_owner_then_retire)

    response = routes._start_chat_stream_for_session(
        session,
        msg="rejected prompt",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert models.SESSIONS[session.session_id] is successor
    assert successor.title == "Successor title"
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["pending_user_message"] == "rejected prompt"


def test_chat_start_rollback_checks_index_owner_before_restoring_files(
    real_session_store, monkeypatch
):
    """An externally replaced index row must leave prepared files untouched."""
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(
        session_id="chat-start-rotated-index",
        title="Existing title",
        messages=[{"role": "assistant", "content": "before"}],
    )
    models.SESSIONS[session.session_id] = session
    session.save(touch_updated_at=False)

    def rotate_index_then_retire(_sid):
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        row = next(item for item in rows if item["session_id"] == session.session_id)
        row["title"] = "Externally rotated index title"
        real_session_store[1].write_text(json.dumps(rows), encoding="utf-8")
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", rotate_index_then_retire)

    response = routes._start_chat_stream_for_session(
        session,
        msg="rejected prompt",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert session.pending_user_message == "rejected prompt"
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["pending_user_message"] == "rejected prompt"
    index_rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    index_row = next(item for item in index_rows if item["session_id"] == session.session_id)
    assert index_row["title"] == "Externally rotated index title"


@pytest.mark.parametrize("save_mode", ["deferred", "eager"])
def test_chat_start_cold_resolver_cannot_replace_canonical_rollback_owner(
    real_session_store, monkeypatch, save_mode
):
    """A loader that missed before prepare cannot publish rejected turn state."""
    sid = f"chat-start-cold-owner-{save_mode}"
    seed = models.Session(
        session_id=sid,
        title="Existing title",
        workspace="/old/workspace",
        model="old-model",
        model_provider="old-provider",
        messages=[{"role": "assistant", "content": "before"}],
    )
    seed.save(touch_updated_at=False)
    before_bytes = seed.path.read_bytes()
    before_payload = json.loads(before_bytes)

    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: save_mode)
    monkeypatch.setattr(
        routes,
        "_provisional_title_from_prompt",
        lambda *_args, **_kwargs: "Prompt title",
    )
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    _use_real_writeback_owner_registry(monkeypatch)

    load_observed_miss = threading.Event()
    prepare_persisted = threading.Event()
    cold_resolver_done = threading.Event()
    resolver_errors = []
    resolver_result = {}
    real_load = models.Session.load
    resolver_thread = None

    def gated_load(cls, requested_sid):
        assert requested_sid == sid
        if threading.current_thread() is not resolver_thread:
            return real_load(requested_sid)
        load_observed_miss.set()
        if not prepare_persisted.wait(3):
            raise RuntimeError("timed out waiting for prepared sidecar")
        return real_load(requested_sid)

    monkeypatch.setattr(models.Session, "load", classmethod(gated_load))

    def resolve_cold_session():
        try:
            # Exercise the lower-level publication race directly.  The public
            # resolver now single-flights same-SID full loads, so racing two
            # get_session() calls would test (and deadlock against) the gate
            # rather than the persistence-generation/CAS fence under review.
            resolver_result["session"], _lost_cas = models._load_and_publish_session(sid)
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            resolver_errors.append(exc)
        finally:
            cold_resolver_done.set()

    def retire_after_cold_publish(requested_sid):
        assert requested_sid == sid
        prepare_persisted.set()
        if not cold_resolver_done.wait(3):
            raise RuntimeError("timed out waiting for cold resolver publication")
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", retire_after_cold_publish)
    resolver_thread = _REAL_THREAD(target=resolve_cold_session, name=f"cold-resolver-{save_mode}")
    try:
        resolver_thread.start()
        assert load_observed_miss.wait(3)
        chat_owner = models.get_session(sid)
        assert models.SESSIONS[sid] is chat_owner
        response = routes._start_chat_stream_for_session(
            chat_owner,
            msg="rejected prompt",
            workspace="/new/workspace",
            model="new-model",
            model_provider="new-provider",
            external_runtime_owned=False,
        )
    finally:
        prepare_persisted.set()
        resolver_thread.join(5)

    assert not resolver_thread.is_alive()
    assert not resolver_errors
    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert resolver_result["session"] is chat_owner
    assert models.SESSIONS[sid] is chat_owner
    assert chat_owner.path.read_bytes() == before_bytes

    canonical = models.SESSIONS[sid]
    canonical.title = "Metadata updated after rollback"
    canonical.save(touch_updated_at=False)
    after_metadata_save = json.loads(chat_owner.path.read_text(encoding="utf-8"))
    assert after_metadata_save["active_stream_id"] == before_payload["active_stream_id"]
    assert after_metadata_save["pending_user_message"] == before_payload["pending_user_message"]
    assert after_metadata_save["pending_attachments"] == before_payload["pending_attachments"]
    assert after_metadata_save["messages"] == before_payload["messages"]
    durable_reload = real_load(sid)
    assert durable_reload.active_stream_id == before_payload["active_stream_id"]
    assert durable_reload.pending_user_message == before_payload["pending_user_message"]
    assert durable_reload.pending_attachments == before_payload["pending_attachments"]
    assert durable_reload.messages == before_payload["messages"]


def test_chat_start_rollback_serializes_concurrent_same_session_save(
    real_session_store, monkeypatch
):
    """A save owning persistence must finish before rollback compares or restores."""
    sid = "chat-start-save-first-persistence-barrier"
    stream_id = "rejected-stream"
    session = models.Session(
        session_id=sid,
        title="before",
        messages=[{"role": "assistant", "content": "before"}],
        pending_user_message="before pending",
    )
    models.SESSIONS[sid] = session
    session.save(touch_updated_at=False)
    snapshot = routes._snapshot_chat_start_session_for_rollback(session)

    session.active_stream_id = stream_id
    session.title = "prepared"
    session.pending_user_message = "prepared pending"
    session.save(touch_updated_at=False)
    snapshot["_persistence_after_prepare"] = routes._snapshot_chat_start_persistence(
        session
    )
    owners = _use_real_writeback_owner_registry(monkeypatch)
    owners[sid] = stream_id

    session.title = "successor"
    session.pending_user_message = "successor pending"
    about_to_replace = threading.Event()
    allow_replace = threading.Event()
    rollback_done = threading.Event()
    save_errors = []
    rollback_errors = []
    result = {}
    save_thread = None

    real_safe_replace = models._safe_replace

    def gated_safe_replace(src, dst):
        if threading.current_thread() is save_thread and Path(dst) == session.path:
            about_to_replace.set()
            if not allow_replace.wait(3):
                raise RuntimeError("timed out waiting to replace successor sidecar")
        return real_safe_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", gated_safe_replace)

    def save_successor():
        try:
            session.save(touch_updated_at=False)
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            save_errors.append(exc)

    def rollback_rejected_stream():
        try:
            result["value"] = routes._rollback_chat_start_session_after_authority_failure(
                session,
                stream_id=stream_id,
                snapshot=snapshot,
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            rollback_errors.append(exc)
        finally:
            rollback_done.set()

    save_thread = _REAL_THREAD(target=save_successor, name="chat-start-successor-save")
    rollback_thread = _REAL_THREAD(
        target=rollback_rejected_stream,
        name="chat-start-rollback",
    )
    try:
        save_thread.start()
        assert about_to_replace.wait(3)
        rollback_thread.start()
        rollback_finished_before_save = rollback_done.wait(1)
        allow_replace.set()
    finally:
        allow_replace.set()
        save_thread.join(5)
        rollback_thread.join(5)

    assert not save_thread.is_alive()
    assert not rollback_thread.is_alive()
    assert not save_errors
    assert not rollback_errors
    assert rollback_finished_before_save is False
    assert result["value"] is False
    assert session.title == "successor"
    assert session.pending_user_message == "successor pending"
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    assert persisted["title"] == "successor"
    assert persisted["pending_user_message"] == "successor pending"


@pytest.mark.parametrize("endpoint", ["btw", "background"])
@pytest.mark.parametrize("mode", ["corrupt", "unreadable", "unwritable"])
def test_auxiliary_route_degrades_authority_failure_to_unjournaled_execution(
    route_harness, monkeypatch, endpoint, mode
):
    parent = _Session(f"{endpoint}-parent")
    child = _Session(f"{endpoint}-child")
    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", lambda **_kwargs: child)
    _install_authority_failure(monkeypatch, route_harness, child.session_id, mode)

    if endpoint == "btw":
        from api import background

        monkeypatch.setattr(background, "track_btw", lambda *_args: None)
        response = routes._handle_btw(
            object(), {"session_id": parent.session_id, "question": "side question"}
        )
    else:
        from api import background

        monkeypatch.setattr(background, "track_background", lambda *_args: None)
        monkeypatch.setattr(background, "complete_background", lambda *_args: None)
        response = routes._handle_background(
            object(), {"session_id": parent.session_id, "prompt": "background task"}
        )

    assert response["_status"] == 200
    assert _Thread.created[-1].kwargs["run_journal_incarnation"] is None


@pytest.mark.parametrize("endpoint", ["btw", "background"])
def test_auxiliary_route_valid_retired_authority_still_blocks(
    route_harness, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-retired")
    child = _Session(f"{endpoint}-child-retired")
    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", lambda **_kwargs: child)
    models.SESSIONS[child.session_id] = child
    _install_authority_failure(monkeypatch, route_harness, child.session_id, "retired")

    if endpoint == "btw":
        response = routes._handle_btw(
            object(), {"session_id": parent.session_id, "question": "side question"}
        )
    else:
        response = routes._handle_background(
            object(), {"session_id": parent.session_id, "prompt": "background task"}
        )

    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert not _Thread.created


def _invoke_auxiliary_route(monkeypatch, endpoint, parent, *, trackers):
    from api import background

    if endpoint == "btw":
        monkeypatch.setattr(
            background,
            "track_btw",
            lambda *args: trackers.append(("btw", args)),
        )
        return routes._handle_btw(
            object(), {"session_id": parent.session_id, "question": "side question"}
        )
    monkeypatch.setattr(
        background,
        "track_background",
        lambda *args: trackers.append(("background", args)),
    )
    monkeypatch.setattr(background, "complete_background", lambda *_args: None)
    return routes._handle_background(
        object(), {"session_id": parent.session_id, "prompt": "background task"}
    )


def _install_retired_authority_for_child(root, child):
    path = _authority_path(root, child.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "session_id": child.session_id,
                "state": "retired",
                "incarnation": "0" * 32,
            }
        ),
        encoding="ascii",
    )
    return path, path.read_bytes()


@pytest.mark.parametrize("endpoint", ["btw", "background"])
def test_auxiliary_retired_authority_removes_fresh_child_exactly(
    real_session_store, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-fresh-cleanup")
    parent_before = copy.deepcopy(parent.__dict__)
    created = []
    authority = {}
    real_new_session = models.new_session

    def new_child(**kwargs):
        child = real_new_session(**kwargs)
        created.append(child)
        authority["path"], authority["before"] = _install_retired_authority_for_child(
            real_session_store[0].parent,
            child,
        )
        return child

    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", new_child)
    trackers = []

    response = _invoke_auxiliary_route(
        monkeypatch,
        endpoint,
        parent,
        trackers=trackers,
    )

    child = created[0]
    backup_path = child.path.with_suffix(".json.bak")
    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert not child.path.exists()
    assert not backup_path.exists()
    if real_session_store[1].exists():
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        assert all(row.get("session_id") != child.session_id for row in rows)
    assert child.session_id not in models.SESSIONS
    assert authority["path"].read_bytes() == authority["before"]
    assert not _Thread.created
    assert trackers == []
    assert parent.__dict__ == parent_before


@pytest.mark.parametrize("endpoint", ["btw", "background"])
def test_successful_auxiliary_rollback_revokes_captured_same_generation_alias(
    real_session_store, monkeypatch, endpoint
):
    """A rejected child cannot be recreated by an alias captured pre-rollback."""
    parent = _Session(f"{endpoint}-parent-successful-rollback-alias")
    created = []
    captured_aliases = []
    real_new_session = models.new_session
    real_activate = run_journal.activate_run_journal_session

    def new_child(**kwargs):
        child = real_new_session(**kwargs)
        created.append(child)
        _install_retired_authority_for_child(real_session_store[0].parent, child)
        return child

    def capture_alias_then_activate(session_id):
        alias = models.Session.load(session_id)
        assert alias is not None
        assert alias is not created[0]
        assert alias._persistence_generation is created[0]._persistence_generation
        captured_aliases.append(alias)
        return real_activate(session_id)

    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", new_child)
    monkeypatch.setattr(
        run_journal,
        "activate_run_journal_session",
        capture_alias_then_activate,
    )
    trackers = []

    response = _invoke_auxiliary_route(
        monkeypatch,
        endpoint,
        parent,
        trackers=trackers,
    )

    child = created[0]
    alias = captured_aliases[0]
    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert child._persistence_revoked is True
    assert child._persistence_generation.revoked is True
    assert alias._persistence_generation is child._persistence_generation

    child.title = "rejected canonical child"
    with pytest.raises(RuntimeError, match="persistence-revoked"):
        child.save(touch_updated_at=False)
    alias.title = "rejected captured alias"
    with pytest.raises(RuntimeError, match="revoked|deleted"):
        alias.save(touch_updated_at=False)

    assert not child.path.exists()
    assert not child.path.with_suffix(".json.bak").exists()
    if real_session_store[1].exists():
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        assert all(row.get("session_id") != child.session_id for row in rows)
    assert child.session_id not in models.SESSIONS
    assert not _Thread.created
    assert trackers == []


@pytest.mark.parametrize("endpoint", ["btw", "background"])
@pytest.mark.parametrize(
    "rotation",
    ["sidecar", "backup", "owner", "index_missing", "index_duplicate", "index_rotated"],
)
def test_auxiliary_retired_authority_rotation_fails_closed_without_clobbering(
    real_session_store, monkeypatch, endpoint, rotation
):
    parent = _Session(f"{endpoint}-parent-rotation-{rotation}")
    created = []
    authority = {}
    real_new_session = models.new_session
    successor = object()

    def new_child(**kwargs):
        child = real_new_session(**kwargs)
        created.append(child)
        authority["path"], authority["before"] = _install_retired_authority_for_child(
            real_session_store[0].parent,
            child,
        )
        return child

    def rotate_then_retire(session_id):
        child = created[0]
        authority["post_sidecar"] = child.path.read_bytes()
        authority["post_index"] = real_session_store[1].read_bytes()
        if rotation == "sidecar":
            child.path.write_bytes(b"external-sidecar-successor")
        elif rotation == "backup":
            child.path.with_suffix(".json.bak").write_bytes(b"external-backup-successor")
        elif rotation == "owner":
            models.SESSIONS[session_id] = successor
        else:
            rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
            if rotation == "index_missing":
                real_session_store[1].unlink()
            elif rotation == "index_duplicate":
                rows.append(next(row for row in rows if row.get("session_id") == child.session_id))
                real_session_store[1].write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                next(row for row in rows if row.get("session_id") == child.session_id)["title"] = "rotated child row"
                real_session_store[1].write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", new_child)
    monkeypatch.setattr(run_journal, "activate_run_journal_session", rotate_then_retire)
    trackers = []

    response = _invoke_auxiliary_route(
        monkeypatch,
        endpoint,
        parent,
        trackers=trackers,
    )

    child = created[0]
    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert response["retryable"] is False
    if rotation == "sidecar":
        assert child.path.read_bytes() == b"external-sidecar-successor"
    elif rotation == "backup":
        assert child.path.with_suffix(".json.bak").read_bytes() == b"external-backup-successor"
    elif rotation == "owner":
        assert models.SESSIONS[child.session_id] is successor
        assert child.path.read_bytes() == authority["post_sidecar"]
    elif rotation == "index_missing":
        assert not real_session_store[1].exists()
        assert child.path.read_bytes() == authority["post_sidecar"]
    elif rotation == "index_duplicate":
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        assert sum(row.get("session_id") == child.session_id for row in rows) == 2
        assert child.path.read_bytes() == authority["post_sidecar"]
    elif rotation == "index_rotated":
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        child_rows = [row for row in rows if row.get("session_id") == child.session_id]
        assert len(child_rows) == 1
        assert child_rows[0]["title"] == "rotated child row"
        assert child.path.read_bytes() == authority["post_sidecar"]
    else:
        assert real_session_store[1].read_bytes() == authority["post_index"]
    if rotation != "owner":
        assert child.session_id not in models.SESSIONS
        assert child._persistence_revoked is True
        assert child._persistence_generation.revoked is True
        with pytest.raises(RuntimeError, match="persistence-revoked"):
            child.save(touch_updated_at=False)
    assert authority["path"].read_bytes() == authority["before"]
    assert not _Thread.created
    assert trackers == []


def test_auxiliary_rollback_failure_retires_shared_persistence_generation(
    real_session_store, monkeypatch
):
    """An uncertain rejected child must not remain writable through aliases."""
    sid = "auxiliary-rollback-failure-retires-generation"
    child = models.Session(
        session_id=sid,
        title="prepared child",
        messages=[{"role": "assistant", "content": "prepared"}],
    )
    models.SESSIONS[sid] = child
    before = routes._snapshot_auxiliary_session_persistence(child)
    child.save(touch_updated_at=False)
    prepared = routes._snapshot_auxiliary_session_persistence(child)
    shared_generation = child._persistence_generation
    alias = models.Session(session_id=sid, title="captured alias")
    assert alias._persistence_generation is shared_generation
    durable_before = child.path.read_bytes()

    monkeypatch.setattr(
        routes,
        "_restore_auxiliary_session_persistence",
        lambda **_kwargs: False,
    )

    assert routes._rollback_auxiliary_session_after_authority_failure(
        child,
        before=before,
        prepared=prepared,
    ) is False

    assert sid not in models.SESSIONS
    assert child._persistence_revoked is True
    assert shared_generation.revoked is True
    child.title = "stale child overwrite"
    with pytest.raises(RuntimeError, match="persistence-revoked"):
        child.save(touch_updated_at=False)
    alias.title = "stale alias overwrite"
    with pytest.raises(RuntimeError, match="revoked|deleted"):
        alias.save(touch_updated_at=False)
    assert child.path.read_bytes() == durable_before


@pytest.mark.parametrize("endpoint", ["btw", "background"])
def test_auxiliary_rollback_prunes_only_child_index_row(
    real_session_store, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-index-successor")
    sibling = models.Session(
        session_id="sibling-index-row",
        title="Sibling before",
        messages=[{"role": "assistant", "content": "existing"}],
    )
    sibling.save(touch_updated_at=False)
    created = []
    real_new_session = models.new_session

    def new_child(**kwargs):
        child = real_new_session(**kwargs)
        created.append(child)
        _install_retired_authority_for_child(real_session_store[0].parent, child)
        return child

    def update_index_then_retire(_sid):
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        for row in rows:
            if row.get("session_id") == sibling.session_id:
                row["title"] = "Sibling updated concurrently"
        rows.append(
            {
                "session_id": "new-concurrent-row",
                "title": "Concurrent new row",
                "message_count": 0,
            }
        )
        real_session_store[1].write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", new_child)
    monkeypatch.setattr(run_journal, "activate_run_journal_session", update_index_then_retire)
    trackers = []

    response = _invoke_auxiliary_route(
        monkeypatch,
        endpoint,
        parent,
        trackers=trackers,
    )

    child = created[0]
    rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    rows_by_id = {row["session_id"]: row for row in rows}
    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert child.session_id not in rows_by_id
    assert rows_by_id[sibling.session_id]["title"] == "Sibling updated concurrently"
    assert rows_by_id["new-concurrent-row"]["title"] == "Concurrent new row"
    assert not child.path.exists()
    assert child.session_id not in models.SESSIONS
    assert not _Thread.created
    assert trackers == []


def test_auxiliary_rollback_serializes_same_object_save_barrier(
    real_session_store, monkeypatch
):
    """A waiting save cannot resurrect an auxiliary object revoked by rollback."""
    sid = "auxiliary-same-object-save-barrier"
    child = models.Session(
        session_id=sid,
        title="prepared child",
        messages=[{"role": "assistant", "content": "prepared"}],
    )
    models.SESSIONS[sid] = child
    before = routes._snapshot_auxiliary_session_persistence(child)
    child.save(touch_updated_at=False)
    prepared = routes._snapshot_auxiliary_session_persistence(child)
    child.title = "successor child"

    sidecar_replaced = threading.Event()
    index_write_attempted = threading.Event()
    release_index = threading.Event()
    compare_observed = threading.Event()
    allow_compare = threading.Event()
    save_errors = []
    rollback_errors = []
    result = {}
    save_thread = None

    real_safe_replace = models._safe_replace

    def gated_safe_replace(src, dst):
        if Path(dst) == child.path and not sidecar_replaced.is_set():
            real_safe_replace(src, dst)
            sidecar_replaced.set()
            return
        return real_safe_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", gated_safe_replace)
    real_write_index = models._write_session_index

    def gated_write_index(*args, **kwargs):
        if threading.current_thread() is save_thread:
            index_write_attempted.set()
            if not release_index.wait(3):
                raise RuntimeError("timed out waiting to update session index")
        return real_write_index(*args, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", gated_write_index)
    real_matches = routes._auxiliary_session_persistence_matches

    def gated_matches(snapshot):
        matched = real_matches(snapshot)
        compare_observed.set()
        if not allow_compare.wait(3):
            raise RuntimeError("timed out waiting to compare persistence")
        return matched

    monkeypatch.setattr(routes, "_auxiliary_session_persistence_matches", gated_matches)

    def save_successor():
        try:
            child.save(touch_updated_at=False)
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            save_errors.append(exc)

    def rollback_rejected_child():
        try:
            result["value"] = routes._rollback_auxiliary_session_after_authority_failure(
                child,
                before=before,
                prepared=prepared,
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            rollback_errors.append(exc)

    save_thread = _REAL_THREAD(target=save_successor, name="same-object-save")
    rollback_thread = _REAL_THREAD(target=rollback_rejected_child, name="auxiliary-rollback")
    try:
        rollback_thread.start()
        assert compare_observed.wait(3)
        # The old rollback holds _INDEX_WRITE_LOCK after this compare.  Its
        # concurrent save can replace the sidecar, then block in index write.
        # The fixed rollback holds the persistence lock too, so this same save
        # remains before any sidecar mutation until rollback revokes the object.
        save_thread.start()
        sidecar_replaced_seen = sidecar_replaced.wait(1)
        if sidecar_replaced_seen:
            assert index_write_attempted.wait(3)
        allow_compare.set()
        rollback_thread.join(3)
        release_index.set()
    finally:
        allow_compare.set()
        release_index.set()
        save_thread.join(5)
        rollback_thread.join(5)

    assert not save_thread.is_alive()
    assert not rollback_thread.is_alive()
    assert not rollback_errors
    assert result["value"] is True
    assert sidecar_replaced_seen is False
    assert len(save_errors) == 1
    assert isinstance(save_errors[0], RuntimeError)
    assert "revoked" in str(save_errors[0])
    assert not child.path.exists()
    rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    assert all(row.get("session_id") != sid for row in rows)
    assert sid not in models.SESSIONS


def test_auxiliary_rollback_fails_closed_when_save_replaces_sidecar_first(
    real_session_store, monkeypatch
):
    """A save that owns persistence wins; rollback observes its rotated image."""
    sid = "auxiliary-save-first-rotation-barrier"
    child = models.Session(
        session_id=sid,
        title="prepared child",
        messages=[{"role": "assistant", "content": "prepared"}],
    )
    models.SESSIONS[sid] = child
    before = routes._snapshot_auxiliary_session_persistence(child)
    child.save(touch_updated_at=False)
    prepared = routes._snapshot_auxiliary_session_persistence(child)
    child.title = "successor child"

    about_to_replace = threading.Event()
    allow_replace = threading.Event()
    sidecar_replaced = threading.Event()
    index_paused = threading.Event()
    release_index = threading.Event()
    compare_observed = threading.Event()
    allow_compare = threading.Event()
    successor_bytes = {}
    save_errors = []
    rollback_errors = []
    result = {}
    save_thread = None

    real_safe_replace = models._safe_replace

    def gated_safe_replace(src, dst):
        if Path(dst) == child.path and not sidecar_replaced.is_set():
            about_to_replace.set()
            if not allow_replace.wait(3):
                raise RuntimeError("timed out waiting to replace sidecar")
            successor_bytes["payload"] = Path(src).read_bytes()
            real_safe_replace(src, dst)
            sidecar_replaced.set()
            return
        return real_safe_replace(src, dst)

    monkeypatch.setattr(models, "_safe_replace", gated_safe_replace)
    real_write_index = models._write_session_index

    def gated_write_index(*args, **kwargs):
        if threading.current_thread() is save_thread:
            index_paused.set()
            if not release_index.wait(3):
                raise RuntimeError("timed out waiting to update session index")
        return real_write_index(*args, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", gated_write_index)
    real_matches = routes._auxiliary_session_persistence_matches

    def gated_matches(snapshot):
        matched = real_matches(snapshot)
        compare_observed.set()
        if not allow_compare.wait(3):
            raise RuntimeError("timed out waiting to compare persistence")
        return matched

    monkeypatch.setattr(routes, "_auxiliary_session_persistence_matches", gated_matches)

    def save_successor():
        try:
            child.save(touch_updated_at=False)
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            save_errors.append(exc)

    def rollback_rejected_child():
        try:
            result["value"] = routes._rollback_auxiliary_session_after_authority_failure(
                child,
                before=before,
                prepared=prepared,
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            rollback_errors.append(exc)

    save_thread = _REAL_THREAD(target=save_successor, name="save-first")
    rollback_thread = _REAL_THREAD(target=rollback_rejected_child, name="rollback-after-save")
    try:
        save_thread.start()
        assert about_to_replace.wait(3)
        rollback_thread.start()
        # Old rollback reaches the compare while the save is still before
        # replacement.  The fixed rollback is waiting on the save's RLock.
        compared_before_replace = compare_observed.wait(1)
        allow_replace.set()
        assert sidecar_replaced.wait(3)
        assert index_paused.wait(3)
        allow_compare.set()
        release_index.set()
    finally:
        allow_replace.set()
        allow_compare.set()
        release_index.set()
        save_thread.join(5)
        rollback_thread.join(5)

    assert not save_thread.is_alive()
    assert not rollback_thread.is_alive()
    assert not save_errors
    assert not rollback_errors
    assert compared_before_replace is False
    assert result["value"] is False
    assert child.path.read_bytes() == successor_bytes["payload"]
    rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    row = next(row for row in rows if row.get("session_id") == sid)
    assert row["title"] == "successor child"
    assert sid not in models.SESSIONS
    assert child._persistence_revoked is True
    assert child._persistence_generation.revoked is True
    with pytest.raises(RuntimeError, match="persistence-revoked"):
        child.save(touch_updated_at=False)


def test_auxiliary_rollback_holds_owner_lock_through_cleanup(
    real_session_store, monkeypatch
):
    """A successor cannot enter the owner window while rollback mutates files/index."""
    sid = "auxiliary-owner-replacement-barrier"
    child = models.Session(
        session_id=sid,
        title="prepared child",
        messages=[{"role": "assistant", "content": "prepared"}],
    )
    models.SESSIONS[sid] = child
    before = routes._snapshot_auxiliary_session_persistence(child)
    child.save(touch_updated_at=False)
    prepared = routes._snapshot_auxiliary_session_persistence(child)
    successor = object()
    restore_entered = threading.Event()
    replacement_probe_done = threading.Event()
    allow_restore = threading.Event()
    replacement_entered = threading.Event()
    replacement_thread = None
    order = []
    order_lock = threading.Lock()
    observed = {}
    result = {}
    rollback_errors = []

    def mark(label):
        with order_lock:
            order.append(label)

    def replace_owner():
        nonlocal replacement_thread
        mark("replacement_attempt")
        with routes.LOCK:
            mark("replacement_enter")
            models.SESSIONS[sid] = successor
            replacement_entered.set()

    real_restore = routes._restore_auxiliary_session_persistence

    def gated_restore(**kwargs):
        nonlocal replacement_thread
        mark("restore_enter")
        restore_entered.set()
        replacement_thread = _REAL_THREAD(target=replace_owner, name="owner-successor")
        replacement_thread.start()
        observed["entered_during_restore"] = replacement_entered.wait(1)
        replacement_probe_done.set()
        if not allow_restore.wait(3):
            raise RuntimeError("timed out waiting to restore rejected child")
        restored = real_restore(**kwargs)
        mark("restore_exit")
        return restored

    monkeypatch.setattr(routes, "_restore_auxiliary_session_persistence", gated_restore)

    def rollback_rejected_child():
        try:
            result["value"] = routes._rollback_auxiliary_session_after_authority_failure(
                child,
                before=before,
                prepared=prepared,
            )
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            rollback_errors.append(exc)

    rollback_thread = _REAL_THREAD(target=rollback_rejected_child, name="owner-rollback")
    try:
        rollback_thread.start()
        assert restore_entered.wait(3)
        assert replacement_probe_done.wait(3)
        allow_restore.set()
    finally:
        allow_restore.set()
        rollback_thread.join(5)
        if replacement_thread is not None:
            replacement_thread.join(5)

    assert not rollback_thread.is_alive()
    assert replacement_thread is not None
    assert not replacement_thread.is_alive()
    assert not rollback_errors
    assert result["value"] is True
    assert observed["entered_during_restore"] is False
    assert order.index("replacement_enter") > order.index("restore_exit")
    assert models.SESSIONS[sid] is successor
    assert not child.path.exists()
    rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    assert all(row.get("session_id") != sid for row in rows)


def test_session_persistence_lock_registry_reuses_and_reclaims_weak_lock():
    sid = "weak-persistence-lock"
    with models._SESSION_PERSISTENCE_LOCKS_LOCK:
        models._SESSION_PERSISTENCE_LOCKS.pop(sid, None)

    first = models._get_session_persistence_lock(sid)
    first_ref = weakref.ref(first)
    assert models._get_session_persistence_lock(sid) is first

    holder_started = threading.Event()
    waiter_started = threading.Event()
    release_holder = threading.Event()
    waiter_acquired = threading.Event()
    waiter_result = {}

    def hold_lock(lock):
        with lock:
            holder_started.set()
            assert release_holder.wait(3)

    def wait_for_lock(expected_lock):
        waiter_lock = models._get_session_persistence_lock(sid)
        waiter_result["same"] = waiter_lock is expected_lock
        waiter_started.set()
        with waiter_lock:
            waiter_acquired.set()

    holder_thread = _REAL_THREAD(
        target=hold_lock,
        args=(first,),
        name="persistence-lock-holder",
    )
    waiter_thread = _REAL_THREAD(
        target=wait_for_lock,
        args=(first,),
        name="persistence-lock-waiter",
    )
    try:
        holder_thread.start()
        assert holder_started.wait(3)
        waiter_thread.start()
        assert waiter_started.wait(3)
        assert not waiter_acquired.is_set()
        release_holder.set()
        assert waiter_acquired.wait(3)
    finally:
        release_holder.set()
        holder_thread.join(5)
        waiter_thread.join(5)

    assert not holder_thread.is_alive()
    assert not waiter_thread.is_alive()
    assert waiter_result["same"] is True

    del first
    gc.collect()

    assert first_ref() is None
    with models._SESSION_PERSISTENCE_LOCKS_LOCK:
        assert sid not in models._SESSION_PERSISTENCE_LOCKS


@pytest.mark.parametrize("endpoint", ["send", "btw", "background"])
def test_unexpected_activation_bug_is_not_silenced(
    route_harness, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-bug")
    child = _Session(f"{endpoint}-child-bug")
    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", lambda **_kwargs: child)
    monkeypatch.setattr(
        run_journal,
        "activate_run_journal_session",
        lambda _sid: (_ for _ in ()).throw(ValueError("unexpected programmer bug")),
    )
    monkeypatch.setattr(
        run_journal,
        "validate_run_journal_session_activation",
        lambda _sid: None,
    )

    with pytest.raises(ValueError, match="unexpected programmer bug"):
        if endpoint == "send":
            routes._start_chat_stream_for_session(
                parent,
                msg="hello",
                workspace=parent.workspace,
                model=parent.model,
                external_runtime_owned=False,
            )
        elif endpoint == "btw":
            routes._handle_btw(
                object(), {"session_id": parent.session_id, "question": "side question"}
            )
        else:
            routes._handle_background(
                object(), {"session_id": parent.session_id, "prompt": "background task"}
            )
