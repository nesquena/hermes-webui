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
    models._COMPOSER_DRAFT_LOCKS.clear()
    yield session_dir, sessions
    models._DRAFT_SIDECAR_CACHE.clear()
    models._COMPOSER_DRAFT_LOCKS.clear()


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
