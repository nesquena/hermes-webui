"""Regression tests for lightweight pending/run-state persistence."""

import json
import queue
import threading
from types import SimpleNamespace

import api.models as models
import api.routes as routes
from api import draft_store, session_runtime_state


def test_runtime_state_sidecar_does_not_rewrite_large_transcript(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    sid = "runtime_session_001"
    session_path = session_dir / f"{sid}.json"
    session_path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [
                    {"role": "user", "content": f"message {i}"}
                    for i in range(1000)
                ],
                "pending_user_message": None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    before = session_path.read_bytes()

    state = {
        "active_stream_id": "stream-1",
        "pending_user_message": "continue this task",
        "pending_attachments": [],
        "pending_started_at": 123.0,
        "pending_user_source": "webui",
        "workspace": "/tmp/workspace",
        "model": "openai/gpt-5",
        "model_provider": "openai",
        "title": "Runtime session",
    }
    saved = session_runtime_state.save_runtime_state(sid, state)

    assert saved == state
    assert session_path.read_bytes() == before
    assert session_runtime_state.load_runtime_state(sid) == state
    assert session_runtime_state.runtime_state_path(sid).stat().st_size < 1000


def test_session_load_overlays_runtime_state_and_terminal_save_clears_it(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)

    sid = "runtime_overlay_001"
    session_path = session_dir / f"{sid}.json"
    session_path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "Old title",
                "messages": [{"role": "user", "content": "history"}],
                "active_stream_id": None,
                "pending_user_message": None,
            }
        ),
        encoding="utf-8",
    )
    session_runtime_state.save_runtime_state(
        sid,
        {
            "active_stream_id": "stream-2",
            "pending_user_message": "pending",
            "pending_attachments": [],
            "pending_started_at": 456.0,
            "pending_user_source": "webui",
            "workspace": "/tmp/new-workspace",
            "model": "openai/gpt-5.6",
            "model_provider": "openai-codex",
            "title": "New title",
        },
    )

    loaded = models.Session.load(sid)

    assert loaded is not None
    assert loaded.active_stream_id == "stream-2"
    assert loaded.pending_user_message == "pending"
    assert loaded.title == "New title"
    loaded.active_stream_id = None
    loaded.pending_user_message = None
    loaded.pending_attachments = []
    loaded.pending_started_at = None
    loaded.pending_user_source = None
    loaded.save(touch_updated_at=False, skip_index=True)

    assert not session_runtime_state.runtime_state_path(sid).exists()
    reloaded = models.Session.load(sid)
    assert reloaded is not None
    assert reloaded.active_stream_id is None
    assert reloaded.pending_user_message is None


def test_deferred_chat_start_state_does_not_save_existing_transcript(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")

    sid = "deferred_large_001"
    session = models.Session(
        session_id=sid,
        title="Large session",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "history"}],
    )
    session.save()
    original_bytes = session.path.read_bytes()

    def fail_save(*args, **kwargs):
        raise AssertionError("deferred chat start must not rewrite an existing transcript")

    monkeypatch.setattr(session, "save", fail_save)
    routes._prepare_chat_start_session_for_stream(
        session,
        msg="new prompt",
        attachments=[],
        workspace=str(tmp_path),
        model="openai/gpt-5",
        model_provider="openai",
        stream_id="stream-deferred",
        started_at=789.0,
    )

    assert session.path.read_bytes() == original_bytes
    state = session_runtime_state.load_runtime_state(sid)
    assert state["active_stream_id"] == "stream-deferred"
    assert state["pending_user_message"] == "new prompt"


def test_turn_journal_is_submitted_before_pending_state_and_worker(tmp_path, monkeypatch):
    events = []
    sid = "journal_order_001"
    session = SimpleNamespace(
        session_id=sid,
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        title="Journal order",
    )
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kwargs: None)
    monkeypatch.setattr(routes, "_active_stream_blocks_chat_start", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda sid: None)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: threading.Lock())
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", lambda *args, **kwargs: events.append("prepare"))
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: queue.Queue())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: events.append("worker"))
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda session: False)

    import api.turn_journal as turn_journal

    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *args, **kwargs: (events.append("journal") or {"turn_id": "turn-1"}),
    )

    response = routes._start_chat_stream_for_session(
        session,
        msg="journal first",
        attachments=[],
        workspace=str(tmp_path),
        model="openai/gpt-5",
        model_provider="openai",
        external_runtime_owned=False,
    )

    assert response["turn_id"] == "turn-1"
    assert events[:2] == ["journal", "prepare"]
    assert "worker" in events
    routes.STREAMS.pop(response["stream_id"], None)


def test_sidecar_cleanup_removes_draft_runtime_and_cached_transient_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()

    sid = "sidecar_delete_001"
    session = models.Session(
        session_id=sid,
        title="Delete me",
        messages=[{"role": "user", "content": "history"}],
    )
    session.active_stream_id = "stream-delete"
    session.pending_user_message = "stale pending"
    session.pending_attachments = [{"name": "file.txt"}]
    session.pending_started_at = 123.0
    session.pending_user_source = "webui"
    session.save()
    models.SESSIONS[sid] = session
    draft_store.save_draft(sid, {"text": "stale draft", "files": []})
    session_runtime_state.save_runtime_state(
        sid,
        session_runtime_state.runtime_state_from_session(session),
    )

    routes._delete_session_sidecars(sid)

    assert not draft_store.draft_path(sid).exists()
    assert not session_runtime_state.runtime_state_path(sid).exists()
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.pending_attachments == []
    assert session.pending_started_at is None
    assert session.pending_user_source is None


def test_cli_delete_helper_removes_webui_sidecars(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    sid = "cli_sidecar_delete_001"
    session = models.Session(
        session_id=sid,
        title="CLI delete",
        messages=[{"role": "user", "content": "history"}],
    )
    session.save()
    draft_store.save_draft(sid, {"text": "draft", "files": []})
    session_runtime_state.save_runtime_state(
        sid,
        {"active_stream_id": "stream", "pending_user_message": "pending"},
    )

    models._delete_webui_sidecars_for_session(sid)

    assert not draft_store.draft_path(sid).exists()
    assert not session_runtime_state.runtime_state_path(sid).exists()
    assert not routes._session_owner_present(sid)


def test_cached_session_refresh_holds_runtime_lock_through_cache_insert(monkeypatch):
    sid = "cache_refresh_lock_001"
    cached = SimpleNamespace(session_id=sid)
    refreshed = SimpleNamespace(session_id=sid)
    monkeypatch.setitem(models.SESSIONS, sid, cached)
    monkeypatch.setattr(models, "_cached_session_lags_disk", lambda _session: True)
    monkeypatch.setattr(models, "_inactive_cache_tail_needs_disk_check", lambda _session: False)
    observed = []

    def load(_sid):
        observed.append(session_runtime_state._lock_for(_sid)._is_owned())
        return refreshed

    monkeypatch.setattr(models.Session, "load", load)

    assert models.get_session(sid) is refreshed
    assert observed == [True]


def test_cached_refresh_can_reenter_runtime_lock_for_both_checks(monkeypatch):
    sid = "cache_refresh_both_001"
    cached = SimpleNamespace(session_id=sid)
    first_disk = SimpleNamespace(session_id=sid)
    second_disk = SimpleNamespace(session_id=sid)
    loads = []
    monkeypatch.setitem(models.SESSIONS, sid, cached)
    monkeypatch.setattr(models, "_cached_session_lags_disk", lambda _session: True)
    monkeypatch.setattr(models, "_inactive_cache_tail_needs_disk_check", lambda _session: True)
    monkeypatch.setattr(models, "_cache_has_stale_unsaved_user_tail", lambda *_args: False)
    monkeypatch.setattr(models, "_session_has_pending_journal_retry", lambda _session: False)
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda _session: None)

    def load(_sid):
        loads.append(_sid)
        return first_disk if len(loads) == 1 else second_disk

    monkeypatch.setattr(models.Session, "load", load)

    assert models.get_session(sid) is first_disk
    assert loads == [sid, sid]


def test_direct_session_load_holds_runtime_lock_through_overlay(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    sid = "direct_load_lock_001"
    models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "history"}],
    ).save()
    observed = []

    def load_runtime_state_sidecar(session_id):
        observed.append(session_runtime_state._lock_for(session_id)._is_owned())
        return {}

    monkeypatch.setattr(models, "_load_runtime_state_sidecar", load_runtime_state_sidecar)
    assert models.Session.load(sid) is not None
    assert observed == [True]


def test_chat_start_rechecks_ownership_before_stream_registration(tmp_path, monkeypatch):
    sid = "chat_delete_race_001"
    owner = {"present": True}
    registered = []
    workers = []
    session = SimpleNamespace(
        session_id=sid,
        path=tmp_path / f"{sid}.json",
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        title="Race",
    )
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kwargs: None)
    monkeypatch.setattr(routes, "_active_stream_blocks_chat_start", lambda *args, **kwargs: False)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: threading.Lock())
    monkeypatch.setattr(routes, "_session_owner_present", lambda _sid: owner["present"])
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _session: False)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: queue.Queue())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *args: registered.append(args))
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *args, **kwargs: workers.append(args))
    monkeypatch.setattr(
        routes,
        "_prepare_chat_start_session_for_stream",
        lambda *args, **kwargs: owner.update(present=False),
    )
    import api.turn_journal as turn_journal
    monkeypatch.setattr(
        turn_journal,
        "append_turn_journal_event",
        lambda *args, **kwargs: {"turn_id": "turn-race"},
    )

    response = routes._start_chat_stream_for_session(
        session,
        msg="race",
        attachments=[],
        workspace=str(tmp_path),
        model="openai/gpt-5",
        model_provider="openai",
        external_runtime_owned=False,
    )

    assert response["_status"] == 404
    assert registered == []
    assert workers == []


def test_sidecar_delete_primitives_report_unlink_failure_and_retry(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    sid = "unlink_failure_001"
    draft_store.save_draft(sid, {"text": "stale draft", "files": []})
    session_runtime_state.save_runtime_state(
        sid, {"active_stream_id": "stale-stream", "pending_user_message": "stale pending"}
    )
    draft_path = draft_store.draft_path(sid)
    runtime_path = session_runtime_state.runtime_state_path(sid)
    real_unlink = type(draft_path).unlink
    failed = {"draft": True, "runtime": True}

    def fail_selected_unlink(path, *args, **kwargs):
        if path == draft_path and failed["draft"]:
            raise OSError("draft unlink injected failure")
        if path == runtime_path and failed["runtime"]:
            raise OSError("runtime unlink injected failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(draft_path), "unlink", fail_selected_unlink)
    assert draft_store.delete_draft(sid) is False
    assert session_runtime_state.clear_runtime_state(sid) is False
    assert draft_path.exists()
    assert runtime_path.exists()

    failed["draft"] = False
    failed["runtime"] = False
    assert draft_store.delete_draft(sid) is True
    assert session_runtime_state.clear_runtime_state(sid) is True
    assert not draft_path.exists()
    assert not runtime_path.exists()


def test_sidecar_cleanup_failure_is_retryable_and_blocks_stale_reuse(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    sid = "sidecar_retry_001"
    draft_store.save_draft(sid, {"text": "old draft", "files": []})
    session_runtime_state.save_runtime_state(
        sid, {"active_stream_id": "old-stream", "pending_user_message": "old pending"}
    )
    draft_path = draft_store.draft_path(sid)
    real_unlink = type(draft_path).unlink
    fail_draft = {"value": True}

    def fail_draft_unlink(path, *args, **kwargs):
        if path == draft_path and fail_draft["value"]:
            raise OSError("draft unlink injected failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(draft_path), "unlink", fail_draft_unlink)
    assert models._delete_webui_sidecars_for_session(sid) is False
    assert sid in models._load_webui_deleted_session_tombstone()
    assert draft_path.exists()

    fail_draft["value"] = False
    assert models._delete_webui_sidecars_for_session(sid) is True
    assert not draft_path.exists()
    assert not session_runtime_state.runtime_state_path(sid).exists()


def test_tombstoned_reused_session_does_not_overlay_old_sidecars(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    sid = "reused_sidecar_001"
    session = models.Session(
        session_id=sid,
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "new owner"}],
    )
    session.save()
    draft_store.save_draft(sid, {"text": "old owner draft", "files": []})
    session_runtime_state.save_runtime_state(
        sid, {"active_stream_id": "old-stream", "pending_user_message": "old pending"}
    )
    models._record_webui_deleted_session_tombstone(sid)

    loaded = models.Session.load(sid)
    assert loaded is not None
    assert loaded.composer_draft == {"text": "", "files": []}
    assert loaded.active_stream_id is None
    assert loaded.pending_user_message is None
