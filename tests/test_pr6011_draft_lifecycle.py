"""Lifecycle regressions for PR #6011 composer-draft sidecars."""

from __future__ import annotations

import json
from collections import OrderedDict
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.requires_agent_modules


@pytest.fixture
def session_env(monkeypatch, tmp_path):
    from api import config, models, routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    index_file.write_text("[]", encoding="utf-8")
    sessions = OrderedDict()

    for module in (config, models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir, raising=False)
        monkeypatch.setattr(module, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(models, "SESSIONS", sessions, raising=False)
    monkeypatch.setattr(routes, "SESSIONS", sessions, raising=False)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None, raising=False)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    models._DRAFT_SIDECAR_CACHE.clear()
    yield session_dir, sessions
    models._DRAFT_SIDECAR_CACHE.clear()


def _post_draft(monkeypatch, payload):
    from api import routes

    raw = json.dumps(payload).encode("utf-8")
    captured = {}

    def fake_j(_handler, body, status=200, extra_headers=None):
        captured.update(payload=body, status=status, extra_headers=extra_headers)
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: fake_j(handler, {"error": message}, status=status),
    )
    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(raw))},
        rfile=BytesIO(raw),
        _safe_webui_print=lambda *_args, **_kwargs: None,
    )
    assert routes.handle_post(handler, SimpleNamespace(path="/api/session/draft")) is True
    return captured



def _post_session_delete(monkeypatch, sid):
    from api import routes

    raw = json.dumps({"session_id": sid}).encode("utf-8")
    captured = {}

    def fake_j(_handler, body, status=200, extra_headers=None):
        captured.update(payload=body, status=status, extra_headers=extra_headers)
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: fake_j(handler, {"error": message}, status=status),
    )
    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(raw))},
        rfile=BytesIO(raw),
        _safe_webui_print=lambda *_args, **_kwargs: None,
    )
    assert routes.handle_post(handler, SimpleNamespace(path="/api/session/delete")) is True
    return captured


def test_first_nonempty_draft_persists_restartable_session_record(session_env, monkeypatch):
    from api import models

    session_dir, sessions = session_env
    session = models.new_session()
    sid = session.session_id
    assert not (session_dir / f"{sid}.json").exists()

    response = _post_draft(
        monkeypatch,
        {"session_id": sid, "text": "survive restart", "files": []},
    )

    assert response["status"] == 200
    assert models.composer_draft_sidecar_path(sid).exists()
    assert (session_dir / f"{sid}.json").exists(), "first payload draft must anchor the session"

    sessions.clear()
    restarted = models.Session.load(sid)
    assert restarted is not None
    assert models.resolve_composer_draft(sid, restarted.composer_draft) == {
        "text": "survive restart",
        "files": [],
    }



def test_delete_restores_owner_when_authoritative_draft_unlink_fails(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "draft-delete-sidecar-failure"
    session = models.Session(session_id=sid, title="Retain draft")
    session.save(skip_index=True)
    draft = {"text": "latest durable text", "files": []}
    models.write_composer_draft_sidecar(sid, draft)
    owner_path = session_dir / f"{sid}.json"

    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)
    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert owner_path.exists()
    assert models.read_composer_draft_sidecar(sid) == draft



def test_delete_without_owner_or_sidecar_is_idempotent(session_env, monkeypatch):
    """A never-persisted session has no draft state to checkpoint before delete."""
    response = _post_session_delete(monkeypatch, "draft-delete-never-persisted")

    assert response["status"] == 200
    assert response["payload"]["ok"] is True



def test_delete_retains_owner_when_backup_unlink_fails(session_env, monkeypatch):
    from api import models

    session_dir, _sessions = session_env
    sid = "draft-delete-backup-unlink-failure"
    owner = models.Session(session_id=sid, title="Retain after backup failure")
    owner.save(skip_index=True)
    owner_path = session_dir / f"{sid}.json"
    backup_path = owner_path.with_suffix(".json.bak")
    backup_path.write_text(owner_path.read_text(encoding="utf-8"), encoding="utf-8")
    original_unlink = type(backup_path).unlink

    def fail_backup_unlink(path, *args, **kwargs):
        if path == backup_path:
            raise OSError("simulated backup unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(backup_path), "unlink", fail_backup_unlink)
    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert owner_path.exists()
    assert backup_path.exists()



def test_deleted_session_rejects_stale_save(session_env, monkeypatch):
    from api import models

    session_dir, _sessions = session_env
    sid = "draft-delete-stale-save"
    owner = models.Session(session_id=sid, title="Must not resurrect")
    owner.save(skip_index=True)

    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 200
    with pytest.raises(RuntimeError, match="deleted"):
        owner.save(skip_index=True)
    assert not (session_dir / f"{sid}.json").exists()



def test_delete_restores_owner_when_tombstone_write_fails(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "draft-delete-tombstone-failure"
    owner = models.Session(session_id=sid, title="Retain without tombstone")
    owner.save(skip_index=True)
    monkeypatch.setattr(routes, "_record_webui_deleted_session_tombstone", lambda _sid: False)

    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert (session_dir / f"{sid}.json").exists()



def test_delete_fsync_failure_after_sidecar_unlink_keeps_legacy_draft(session_env, monkeypatch):
    from api import models

    session_dir, _sessions = session_env
    sid = "delete-sidecar-fsync-failure"
    owner = models.Session(session_id=sid, title="Retain latest draft")
    owner.save(skip_index=True)
    draft = {"text": "latest durable draft", "files": []}
    models.write_composer_draft_sidecar(sid, draft)
    monkeypatch.setattr(
        models,
        "_fsync_composer_draft_parent",
        lambda _path: (_ for _ in ()).throw(OSError("simulated directory EIO")),
    )

    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert (session_dir / f"{sid}.json").exists()
    assert models.Session.load(sid).composer_draft == draft


def test_delete_refuses_sidecar_unlink_when_checkpoint_directory_fsync_fails(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "delete-checkpoint-directory-fsync-failure"
    owner = models.Session(session_id=sid, title="Retain draft")
    owner.save(skip_index=True)
    draft = {"text": "draft remains authoritative", "files": []}
    models.write_composer_draft_sidecar(sid, draft)
    monkeypatch.setattr(
        routes,
        "_fsync_composer_draft_directory",
        lambda _path: (_ for _ in ()).throw(OSError("simulated checkpoint EIO")),
    )
    unlink_attempted = []
    monkeypatch.setattr(
        routes,
        "delete_composer_draft_sidecar",
        lambda _sid: unlink_attempted.append(_sid) or True,
    )

    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert unlink_attempted == []
    assert (session_dir / f"{sid}.json").exists()
    assert models.read_composer_draft_sidecar(sid) == draft


def test_delete_rollback_failure_is_recovered_by_next_session_load(session_env, monkeypatch):
    from api import models, routes

    session_dir, sessions = session_env
    sid = "delete-rollback-load-recovery"
    owner = models.Session(session_id=sid, title="Retain after rollback failure")
    owner.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "durable", "files": []})
    owner_path = session_dir / f"{sid}.json"
    staged_prefix = str(session_dir / f".{sid}.json.deleting-")
    original_replace = routes.os.replace
    failed_restore = []

    def fail_first_rollback(source, destination):
        if (
            str(source).startswith(staged_prefix)
            and str(destination) == str(owner_path)
            and not failed_restore
        ):
            failed_restore.append(True)
            raise OSError("simulated delete rollback failure")
        return original_replace(source, destination)

    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)
    monkeypatch.setattr(routes.os, "replace", fail_first_rollback)

    response = _post_session_delete(monkeypatch, sid)
    sessions.clear()
    listed = models.all_sessions()
    metadata = models.Session.load_metadata_only(sid)
    reloaded = models.Session.load(sid)

    assert response["status"] == 500
    assert failed_restore == [True]
    assert owner_path.exists()
    assert sid in {entry["session_id"] for entry in listed}
    assert metadata is not None
    assert metadata.session_id == sid
    assert reloaded is not None
    assert reloaded.session_id == sid


def test_delete_returns_error_when_final_owner_unlink_directory_fsync_fails(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "delete-final-owner-fsync-failure"
    owner = models.Session(session_id=sid, title="Delete only after durable unlink")
    owner.save(skip_index=True)
    monkeypatch.setattr(
        routes,
        "_fsync_composer_draft_directory",
        lambda _path: (_ for _ in ()).throw(OSError("simulated final unlink EIO")),
    )

    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert (session_dir / f"{sid}.json").exists()
    assert models.Session.load(sid) is not None


def test_delete_final_owner_unlink_failure_restores_staged_owner(session_env, monkeypatch):
    from api import models

    session_dir, _sessions = session_env
    sid = "delete-final-owner-unlink-failure"
    owner = models.Session(session_id=sid, title="Retain after unlink failure")
    owner.save(skip_index=True)
    owner_path = session_dir / f"{sid}.json"
    original_unlink = type(owner_path).unlink

    def fail_staged_unlink(path, *args, **kwargs):
        if path.name.startswith(f".{sid}.json.deleting-"):
            raise OSError("simulated staged owner unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(owner_path), "unlink", fail_staged_unlink)
    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert owner_path.exists()
    assert not list(session_dir.glob(f".{sid}.json.deleting-*"))


def test_delete_refuses_to_remove_unreadable_authoritative_draft(session_env, monkeypatch):
    from api import models

    session_dir, _sessions = session_env
    sid = "draft-delete-unreadable"
    session = models.Session(session_id=sid, title="Retain unreadable draft")
    session.save(skip_index=True)
    owner_path = session_dir / f"{sid}.json"
    sidecar_path = models.composer_draft_sidecar_path(sid)
    assert sidecar_path is not None
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text("{malformed", encoding="utf-8")

    response = _post_session_delete(monkeypatch, sid)

    assert response["status"] == 500
    assert owner_path.exists()
    assert sidecar_path.read_text(encoding="utf-8") == "{malformed"


def test_compression_rotation_moves_draft_to_continuation_owner(session_env):
    from api import models, streaming

    _session_dir, _sessions = session_env
    old_sid = "draft-rotation-old"
    new_sid = "draft-rotation-new"
    session = models.Session(session_id=old_sid, title="Before compression")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(
        old_sid,
        {"text": "continue after compression", "files": [{"name": "notes.txt"}]},
    )

    session.session_id = new_sid
    streaming._preserve_pre_compression_snapshot(session, old_sid)

    assert models.read_composer_draft_sidecar(old_sid) is None
    assert models.read_composer_draft_sidecar(new_sid) == {
        "text": "continue after compression",
        "files": [{"name": "notes.txt"}],
    }



def test_draft_migration_handles_old_and_new_ids_on_same_lock_stripe(session_env, monkeypatch):
    from api import models

    old_sid = "draft-same-stripe-old"
    new_sid = "draft-same-stripe-new"
    models.write_composer_draft_sidecar(old_sid, {"text": "move me", "files": []})
    # Force the pair onto one lock. RLock makes the existing ordered nested
    # migration protocol safe even when two ids share a stripe.
    monkeypatch.setattr(models, "_COMPOSER_DRAFT_LOCK_STRIPES", (models.threading.RLock(),))

    models.migrate_composer_draft_sidecar(old_sid, new_sid)

    assert models.read_composer_draft_sidecar(old_sid) is None
    assert models.read_composer_draft_sidecar(new_sid) == {"text": "move me", "files": []}



def test_draft_migration_orders_distinct_stripes_independent_of_sid_order(session_env, monkeypatch):
    from api import models

    acquired = []

    class RecordingLock:
        def __init__(self, stripe_index):
            self.stripe_index = stripe_index

        def __enter__(self):
            acquired.append(self.stripe_index)
            return self

        def __exit__(self, *_args):
            return False

    # Invert the session-id order relative to stripe order. Both migrations must
    # still acquire 0 then 1, preventing cross-migration lock-order inversion.
    monkeypatch.setattr(models, "_COMPOSER_DRAFT_LOCK_STRIPES", (RecordingLock(0), RecordingLock(1)))
    monkeypatch.setattr(
        models,
        "_composer_draft_lock_stripe_index",
        lambda sid: {"draft-a": 1, "draft-b": 0}[sid],
    )

    models.migrate_composer_draft_sidecar("draft-a", "draft-b")
    models.migrate_composer_draft_sidecar("draft-b", "draft-a")

    assert acquired == [0, 1, 0, 1]


def test_delete_race_cannot_leave_orphan_drafts(session_env, monkeypatch):
    from api import models, routes

    session_dir, sessions = session_env
    sid = "draft-delete-race"
    session = models.Session(session_id=sid, title="Delete race")
    session.save(skip_index=True)

    real_lock = models.get_composer_draft_lock(sid)

    @contextmanager
    def delete_wins_before_draft_lock(_sid):
        with real_lock:
            sessions.pop(sid, None)
            (session_dir / f"{sid}.json").unlink(missing_ok=True)
            models.delete_composer_draft_sidecar(sid)
            yield

    monkeypatch.setattr(routes, "get_composer_draft_lock", delete_wins_before_draft_lock)
    response = _post_draft(
        monkeypatch,
        {"session_id": sid, "text": "must not resurrect", "files": []},
    )
    assert response["status"] == 404
    assert models.read_composer_draft_sidecar(sid) is None


def test_bulk_zero_message_prune_preserves_nonempty_draft_owner(session_env, monkeypatch):
    from api import models, routes

    _session_dir, sessions = session_env
    sid = "draft-bulk-owner"
    owner = models.Session(session_id=sid, title="Draft-only conversation")
    owner.save(skip_index=True)
    draft = {"text": "keep this durable draft", "files": []}
    models.write_composer_draft_sidecar(sid, draft)
    sessions.clear()

    pruned = []
    tombstoned = []
    monkeypatch.setattr(routes, "agent_session_zero_message_sids", lambda *_a, **_k: {sid})
    monkeypatch.setattr(routes, "_load_webui_zero_message_orphan_tombstone", lambda: set())
    monkeypatch.setattr(routes, "prune_session_from_index", pruned.append)
    monkeypatch.setattr(routes, "_record_webui_zero_message_orphan_tombstone", tombstoned.append)

    rows = [{
        "session_id": sid,
        "title": "Draft-only conversation",
        "message_count": 1,
        "session_source": "webui",
        "source_tag": "webui",
    }]
    assert routes._prune_orphaned_webui_zero_message_sessions(rows) == rows
    restarted = models.Session.load(sid)
    assert restarted is not None
    assert models.resolve_composer_draft(sid, restarted.composer_draft) == draft
    assert pruned == []
    assert tombstoned == []


def test_bulk_zero_message_prune_retains_corrupt_durable_owner(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "draft-bulk-corrupt-owner"
    sidecar = {"text": "keep this sidecar despite corrupt owner", "files": []}
    (session_dir / f"{sid}.json").write_text("{not valid json", encoding="utf-8")
    models.write_composer_draft_sidecar(sid, sidecar)

    pruned = []
    tombstoned = []
    monkeypatch.setattr(routes, "agent_session_zero_message_sids", lambda *_a, **_k: {sid})
    monkeypatch.setattr(routes, "_load_webui_zero_message_orphan_tombstone", lambda: set())
    monkeypatch.setattr(routes, "prune_session_from_index", pruned.append)
    monkeypatch.setattr(routes, "_record_webui_zero_message_orphan_tombstone", tombstoned.append)

    rows = [{
        "session_id": sid,
        "title": "Corrupt durable owner",
        "message_count": 1,
        "session_source": "webui",
        "source_tag": "webui",
    }]
    assert routes._prune_orphaned_webui_zero_message_sessions(rows) == rows
    assert models.read_composer_draft_sidecar(sid) == sidecar
    assert pruned == []
    assert tombstoned == []


def test_bulk_zero_message_prune_removes_empty_owner_and_tombstones(session_env, monkeypatch):
    from api import models, routes

    _session_dir, _sessions = session_env
    sid = "draft-bulk-empty-owner"
    owner = models.Session(session_id=sid, title="Empty stale conversation")
    owner.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "", "files": []})

    pruned = []
    tombstoned = []
    monkeypatch.setattr(routes, "agent_session_zero_message_sids", lambda *_a, **_k: {sid})
    monkeypatch.setattr(routes, "_load_webui_zero_message_orphan_tombstone", lambda: set())
    monkeypatch.setattr(routes, "prune_session_from_index", pruned.append)
    monkeypatch.setattr(routes, "_record_webui_zero_message_orphan_tombstone", tombstoned.append)

    rows = [{
        "session_id": sid,
        "title": "Empty stale conversation",
        "message_count": 1,
        "session_source": "webui",
        "source_tag": "webui",
    }]
    assert routes._prune_orphaned_webui_zero_message_sessions(rows) == []
    assert models.read_composer_draft_sidecar(sid) is None
    assert pruned == [sid]
    assert tombstoned == [sid]



def test_draft_post_refuses_to_overwrite_unreadable_authoritative_sidecar(session_env, monkeypatch):
    from api import models

    session_dir, _sessions = session_env
    sid = "draft-unreadable-sidecar"
    session = models.Session(session_id=sid, title="Corrupt draft")
    session.save(skip_index=True)
    draft_path = models.composer_draft_sidecar_path(sid)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text("{not json", encoding="utf-8")

    response = _post_draft(monkeypatch, {"session_id": sid, "text": "must not overwrite", "files": []})

    assert response["status"] == 500
    assert draft_path.read_text(encoding="utf-8") == "{not json"


def test_draft_cache_rejects_same_size_mtime_atomic_replacement(session_env):
    from api import models
    import os

    sid = "draft-cache-identity"
    models.write_composer_draft_sidecar(sid, {"text": "old", "files": []})
    path = models.composer_draft_sidecar_path(sid)
    original = path.stat()
    assert models.read_composer_draft_sidecar(sid)["text"] == "old"

    replacement = path.with_suffix(".replacement")
    replacement.write_text(
        path.read_text(encoding="utf-8").replace('"old"', '"new"'),
        encoding="utf-8",
    )
    assert replacement.stat().st_size == original.st_size
    os.replace(replacement, path)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert models.read_composer_draft_sidecar(sid) == {"text": "new", "files": []}




def test_draft_preflight_copy_keeps_source_until_continuation_owner_is_saved(session_env):
    from api import models

    old_sid = "draft-preflight-old"
    new_sid = "draft-preflight-new"
    draft = {"text": "do not orphan", "files": []}
    models.write_composer_draft_sidecar(old_sid, draft)

    assert models.migrate_composer_draft_sidecar(old_sid, new_sid, finalize_source=False) is True
    assert models.read_composer_draft_sidecar(old_sid) == draft
    assert models.read_composer_draft_sidecar(new_sid) == draft

    assert models.migrate_composer_draft_sidecar(old_sid, new_sid) is True
    assert models.read_composer_draft_sidecar(old_sid) is None


def test_draft_migration_rolls_back_new_sidecar_when_source_delete_fails(session_env, monkeypatch):
    from api import models

    old_sid = "draft-rollback-old"
    new_sid = "draft-rollback-new"
    draft = {"text": "keep old ownership", "files": []}
    models.write_composer_draft_sidecar(old_sid, draft)
    real_delete = models.delete_composer_draft_sidecar

    def fail_only_source_delete(sid):
        return False if sid == old_sid else real_delete(sid)

    monkeypatch.setattr(models, "delete_composer_draft_sidecar", fail_only_source_delete)

    assert models.migrate_composer_draft_sidecar(old_sid, new_sid) is False
    assert models.read_composer_draft_sidecar(old_sid) == draft
    assert models.read_composer_draft_sidecar(new_sid) is None



def test_draft_migration_refreshes_stale_destination_after_failed_rollback(session_env, monkeypatch):
    from api import models
    import os

    old_sid = "draft-stale-source"
    new_sid = "draft-stale-destination"
    models.write_composer_draft_sidecar(old_sid, {"text": "first", "files": []})
    original_delete = models.delete_composer_draft_sidecar

    monkeypatch.setattr(models, "delete_composer_draft_sidecar", lambda _sid: False)
    assert models.migrate_composer_draft_sidecar(old_sid, new_sid) is False
    old_path = models.composer_draft_sidecar_path(old_sid)
    new_path = models.composer_draft_sidecar_path(new_sid)
    assert old_path is not None and new_path is not None
    models.write_composer_draft_sidecar(old_sid, {"text": "newest", "files": []})
    newer_ns = max(old_path.stat().st_mtime_ns, new_path.stat().st_mtime_ns + 1)
    os.utime(old_path, ns=(newer_ns, newer_ns))

    monkeypatch.setattr(models, "delete_composer_draft_sidecar", original_delete)
    assert models.migrate_composer_draft_sidecar(old_sid, new_sid) is True
    assert models.read_composer_draft_sidecar(new_sid) == {"text": "newest", "files": []}
    assert models.read_composer_draft_sidecar(old_sid) is None


def test_draft_migration_fails_closed_when_old_sidecar_is_unreadable(session_env):
    from api import models

    old_sid = "draft-corrupt-source"
    new_sid = "draft-corrupt-destination"
    old_path = models.composer_draft_sidecar_path(old_sid)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text("{not json", encoding="utf-8")

    assert models.migrate_composer_draft_sidecar(old_sid, new_sid) is False
    assert old_path.exists()
    assert models.composer_draft_sidecar_path(new_sid).exists() is False


def test_clear_is_canonical_durable_and_does_not_clobber_newer_draft(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    sid = "draft-clear"
    old_draft = {"text": "submitted", "files": [{"name": "old.txt"}]}
    session = models.Session(session_id=sid, title="Clear", composer_draft=dict(old_draft))
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, old_draft)

    response = _post_draft(
        monkeypatch,
        {"session_id": sid, "clear": True, "expected": old_draft},
    )
    assert response["status"] == 200
    assert response["payload"]["draft"] == {"text": "", "files": []}
    assert models.read_composer_draft_sidecar(sid) is None
    assert models.Session.load(sid).composer_draft == {"text": "", "files": []}

    newer = {"text": "typed after submit", "files": [{"name": "new.txt"}]}
    models.write_composer_draft_sidecar(sid, newer)
    response = _post_draft(
        monkeypatch,
        {"session_id": sid, "clear": True, "expected": old_draft},
    )
    assert response["status"] == 200
    assert response["payload"]["draft"] == newer
    assert response["payload"]["unchanged"] is True
    assert models.read_composer_draft_sidecar(sid) == newer


def test_clear_returns_error_when_authoritative_draft_sidecar_cannot_be_removed(
    session_env, monkeypatch
):
    from api import models, routes

    _session_dir, _sessions = session_env
    sid = "draft-clear-unlink-failure"
    old_draft = {"text": "must not be falsely cleared", "files": []}
    session = models.Session(session_id=sid, title="Clear unlink failure")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, old_draft)

    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)
    response = _post_draft(
        monkeypatch,
        {"session_id": sid, "clear": True, "expected": old_draft},
    )

    assert response["status"] == 500
    assert "clear" in response["payload"]["error"].lower()
    assert models.read_composer_draft_sidecar(sid) == old_draft
    # A failed clear must retain a durable legacy fallback as well as the
    # authoritative sidecar; a later sidecar loss must not discard the draft.
    assert models.Session.load(sid).composer_draft == old_draft


def test_clear_preserves_fallback_when_empty_legacy_save_fails(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    sid = "draft-clear-empty-save-failure"
    old_draft = {"text": "must survive empty save failure", "files": []}
    session = models.Session(session_id=sid, title="Clear empty save failure")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, old_draft)
    real_save = models.Session.save

    def fail_only_empty_draft_save(self, *args, **kwargs):
        if self.session_id == sid and self.composer_draft == {"text": "", "files": []}:
            raise OSError("simulated empty legacy draft save failure")
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", fail_only_empty_draft_save)
    response = _post_draft(
        monkeypatch,
        {"session_id": sid, "clear": True, "expected": old_draft},
    )

    assert response["status"] == 500
    assert "clear" in response["payload"]["error"].lower()
    # The sidecar was removed, so the pre-unlink legacy checkpoint is now the
    # only durable copy and must restore the submitted draft.
    assert models.read_composer_draft_sidecar(sid) is None
    assert models.Session.load(sid).composer_draft == old_draft


def test_clear_skips_index_write_to_keep_checkpoint_and_final_save_durable(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    sid = "draft-clear-index-failure"
    old_draft = {"text": "must not depend on index write", "files": []}
    session = models.Session(session_id=sid, title="Clear index failure")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, old_draft)

    def fail_index_write(*_args, **_kwargs):
        raise OSError("simulated session index write failure")

    monkeypatch.setattr(models, "_write_session_index", fail_index_write)
    response = _post_draft(
        monkeypatch,
        {"session_id": sid, "clear": True, "expected": old_draft},
    )

    assert response["status"] == 200
    assert models.read_composer_draft_sidecar(sid) is None
    assert models.Session.load(sid).composer_draft == {"text": "", "files": []}



def test_draft_directory_fsync_is_noop_on_windows(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    open_calls = []
    monkeypatch.setattr(models.os, "name", "nt")
    monkeypatch.setattr(models.os, "open", lambda *_args, **_kwargs: open_calls.append(True) or 0)

    models._fsync_composer_draft_directory(models.SESSION_DIR)

    assert open_calls == []


def test_write_draft_rejects_parent_directory_fsync_failure(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    monkeypatch.setattr(
        models,
        "_fsync_composer_draft_parent",
        lambda _path: (_ for _ in ()).throw(OSError("simulated directory EIO")),
    )

    with pytest.raises(OSError, match="directory EIO"):
        models.write_composer_draft_sidecar("draft-parent-fsync-eio", {"text": "x", "files": []})


def test_delete_draft_reports_parent_directory_fsync_failure(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    sid = "draft-delete-parent-fsync-eio"
    models.write_composer_draft_sidecar(sid, {"text": "x", "files": []})
    monkeypatch.setattr(
        models,
        "_fsync_composer_draft_parent",
        lambda _path: (_ for _ in ()).throw(OSError("simulated directory EIO")),
    )

    assert models.delete_composer_draft_sidecar(sid) is False


def test_migration_uses_retained_source_when_drafts_differ_with_equal_mtime(session_env):
    from api import models
    import os

    _session_dir, _sessions = session_env
    old_sid = "draft-equal-time-source"
    new_sid = "draft-equal-time-destination"
    models.write_composer_draft_sidecar(old_sid, {"text": "source", "files": []})
    models.write_composer_draft_sidecar(new_sid, {"text": "destination", "files": []})
    old_path = models.composer_draft_sidecar_path(old_sid)
    new_path = models.composer_draft_sidecar_path(new_sid)
    assert old_path is not None and new_path is not None
    timestamp = 1_700_000_000_000_000_000
    os.utime(old_path, ns=(timestamp, timestamp))
    os.utime(new_path, ns=(timestamp, timestamp))

    assert models.migrate_composer_draft_sidecar(old_sid, new_sid) is True
    assert models.read_composer_draft_sidecar(old_sid) is None
    assert models.read_composer_draft_sidecar(new_sid) == {"text": "source", "files": []}


def test_delete_draft_sidecar_reports_unlink_failure(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    sid = "draft-sidecar-unlink-failure"
    models.write_composer_draft_sidecar(sid, {"text": "preserve me", "files": []})
    sidecar_path = models.composer_draft_sidecar_path(sid)
    assert sidecar_path is not None
    original_unlink = type(sidecar_path).unlink

    def fail_sidecar_unlink(path, *args, **kwargs):
        if path == sidecar_path:
            raise OSError("simulated draft-sidecar unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(sidecar_path), "unlink", fail_sidecar_unlink)

    assert models.delete_composer_draft_sidecar(sid) is False
    assert sidecar_path.exists()



def test_cleanup_does_not_count_failed_orphan_sidecar_delete(session_env, monkeypatch):
    from api import models, routes

    session_dir, sessions = session_env
    sid = "cleanup-orphan-unlink-failure"
    models.write_composer_draft_sidecar(sid, {"text": "orphan", "files": []})
    sessions.pop(sid, None)
    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.update(payload) or True)
    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)

    assert routes._handle_sessions_cleanup(SimpleNamespace(), {}, zero_only=False) is True

    assert captured == {"ok": True, "cleaned": 0}
    assert (session_dir / "_drafts" / f"{sid}.json").exists()



def test_cleanup_retains_owner_when_draft_sidecar_delete_fails(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "cleanup-owner-sidecar-delete-failure"
    owner = models.Session(session_id=sid, title="Untitled")
    owner.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "", "files": []})
    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.update(payload) or True)
    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)

    assert routes._handle_sessions_cleanup(SimpleNamespace(), {}, zero_only=False) is True

    assert captured == {"ok": True, "cleaned": 0}
    assert (session_dir / f"{sid}.json").exists()
    assert models.read_composer_draft_sidecar(sid) == {"text": "", "files": []}


def test_cleanup_fsync_failure_after_sidecar_unlink_keeps_legacy_draft(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "cleanup-sidecar-fsync-failure"
    owner = models.Session(session_id=sid, title="Untitled")
    owner.save(skip_index=True)
    draft = {"text": "", "files": []}
    models.write_composer_draft_sidecar(sid, draft)
    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.update(payload) or True)
    monkeypatch.setattr(
        models,
        "_fsync_composer_draft_parent",
        lambda _path: (_ for _ in ()).throw(OSError("simulated directory EIO")),
    )

    assert routes._handle_sessions_cleanup(SimpleNamespace(), {}, zero_only=False) is True

    assert captured == {"ok": True, "cleaned": 0}
    assert (session_dir / f"{sid}.json").exists()
    assert models.Session.load(sid).composer_draft == draft


def test_cleanup_recovers_staged_owner_after_sidecar_delete_rollback_failure(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "cleanup-sidecar-delete-rollback-failure"
    owner = models.Session(session_id=sid, title="Untitled")
    owner.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "", "files": []})
    owner_path = session_dir / f"{sid}.json"
    staged_prefix = str(session_dir / f".{sid}.json.deleting-")
    original_replace = routes.os.replace
    failed_restore = []

    def fail_first_rollback(source, destination):
        if (
            str(source).startswith(staged_prefix)
            and str(destination) == str(owner_path)
            and not failed_restore
        ):
            failed_restore.append(True)
            raise OSError("simulated cleanup rollback failure")
        return original_replace(source, destination)

    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.update(payload) or True)
    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)
    monkeypatch.setattr(routes.os, "replace", fail_first_rollback)

    assert routes._handle_sessions_cleanup(SimpleNamespace(), {}, zero_only=False) is True

    assert failed_restore == [True]
    assert captured == {"ok": True, "cleaned": 0}
    assert owner_path.exists()
    assert models.read_composer_draft_sidecar(sid) == {"text": "", "files": []}


def test_cleanup_retains_unreadable_orphan_sidecar(session_env, monkeypatch):
    from api import models, routes

    session_dir, sessions = session_env
    sid = "cleanup-unreadable-orphan"
    sidecar_path = models.composer_draft_sidecar_path(sid)
    assert sidecar_path is not None
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text("{malformed", encoding="utf-8")
    sessions.pop(sid, None)
    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.update(payload) or True)

    assert routes._handle_sessions_cleanup(SimpleNamespace(), {}, zero_only=False) is True

    assert captured == {"ok": True, "cleaned": 0}
    assert sidecar_path.read_text(encoding="utf-8") == "{malformed"


def test_cleanup_retains_owner_when_sidecar_is_unreadable(session_env, monkeypatch):
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "cleanup-unreadable-owner"
    owner = models.Session(session_id=sid, title="Untitled")
    owner.save(skip_index=True)
    sidecar_path = models.composer_draft_sidecar_path(sid)
    assert sidecar_path is not None
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text("{malformed", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.update(payload) or True)

    assert routes._handle_sessions_cleanup(SimpleNamespace(), {}, zero_only=False) is True

    assert captured == {"ok": True, "cleaned": 0}
    assert (session_dir / f"{sid}.json").exists()
    assert sidecar_path.read_text(encoding="utf-8") == "{malformed"


def test_clear_canonicalizes_legacy_draft_without_files(session_env, monkeypatch):
    from api import models

    _session_dir, _sessions = session_env
    sid = "draft-clear-legacy"
    session = models.Session(
        session_id=sid,
        title="Legacy clear",
        composer_draft={"text": "submitted"},
    )
    session.save(skip_index=True)

    response = _post_draft(
        monkeypatch,
        {
            "session_id": sid,
            "clear": True,
            "expected": {"text": "submitted", "files": []},
        },
    )

    assert response["status"] == 200
    assert response["payload"]["draft"] == {"text": "", "files": []}
    assert "unchanged" not in response["payload"]
    assert models.Session.load(sid).composer_draft == {"text": "", "files": []}
    assert models.read_composer_draft_sidecar(sid) is None


def test_compact_session_json_still_drives_parent_recovery_reader(session_env):
    from api import models

    _session_dir, sessions = session_env
    parent_sid = "compact-parent"
    child = models.Session(session_id="compact-child", parent_session_id=parent_sid)
    child.save(touch_updated_at=False, skip_index=True)
    sessions.clear()

    raw = child.path.read_text(encoding="utf-8")
    assert f'"parent_session_id":"{parent_sid}"' in raw
    assert models._has_compression_continuation(models.Session(session_id=parent_sid)) is True


@pytest.mark.parametrize("zero_only", [False, True], ids=["untitled", "zero-message"])
def test_cleanup_preserves_nonempty_draft_owner_and_removes_empty_owner_and_ghost(
    session_env, monkeypatch, zero_only
):
    """Both cleanup endpoints must treat a nonempty draft as durable user state."""
    from api import models, routes

    session_dir, sessions = session_env
    keep_sid = f"cleanup-keep-{zero_only}"
    keep = models.Session(session_id=keep_sid, title="Untitled")
    keep.save(skip_index=True)
    models.write_composer_draft_sidecar(keep_sid, {"text": "keep me", "files": []})

    remove_sid = f"cleanup-remove-{zero_only}"
    remove = models.Session(session_id=remove_sid, title="Untitled")
    remove.save(skip_index=True)
    models.write_composer_draft_sidecar(remove_sid, {"text": "", "files": []})

    ghost_sid = f"cleanup-ghost-{zero_only}"
    models.write_composer_draft_sidecar(ghost_sid, {"text": "orphan", "files": []})
    sessions.pop(ghost_sid, None)

    captured = {}
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: captured.update(payload) or True)
    assert routes._handle_sessions_cleanup(SimpleNamespace(), {}, zero_only=zero_only) is True

    assert (session_dir / f"{keep_sid}.json").exists()
    sessions.clear()
    restarted = models.Session.load(keep_sid)
    assert restarted is not None
    assert models.resolve_composer_draft(keep_sid, restarted.composer_draft)["text"] == "keep me"
    assert not (session_dir / f"{remove_sid}.json").exists()
    assert models.read_composer_draft_sidecar(remove_sid) is None
    assert models.read_composer_draft_sidecar(ghost_sid) is None
    assert captured["ok"] is True
