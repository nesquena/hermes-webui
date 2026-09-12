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


# ── Gate follow-ups: tri-state reads, honored deletions, ordering, routing ──


def _break_sidecar_read(monkeypatch, models, *, sid, exc=None):
    """Make exactly *sid*'s sidecar unreadable, leaving every other path alone."""
    exc = exc if exc is not None else OSError("EIO")
    target = models.composer_draft_sidecar_path(sid)
    real_read_text = type(target).read_text

    def read_text(self, *args, **kwargs):
        if self == target:
            raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(target), "read_text", read_text)
    models._DRAFT_SIDECAR_CACHE.clear()


def test_unreadable_sidecar_is_not_reported_as_absent(session_env, monkeypatch):
    """`None` used to mean both "no draft" and "I could not read it"."""
    from api import models

    sid = "tri-state-read"
    models.write_composer_draft_sidecar(sid, {"text": "precious", "files": []})
    _break_sidecar_read(monkeypatch, models, sid=sid)

    draft, status, _redirect = models.read_composer_draft_sidecar_status(sid)
    assert draft is None
    assert status == models.DRAFT_UNREADABLE

    _resolved, readable = models.resolve_composer_draft_status(sid, {"text": "legacy"})
    assert readable is False, "an unreadable sidecar must not read as a known-empty draft"


def test_corrupt_sidecar_is_unreadable_not_absent(session_env, monkeypatch):
    from api import models

    sid = "tri-state-corrupt"
    path = models.composer_draft_sidecar_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"draft": {"text": "half', encoding="utf-8")
    models._DRAFT_SIDECAR_CACHE.clear()

    _draft, status, _redirect = models.read_composer_draft_sidecar_status(sid)
    assert status == models.DRAFT_UNREADABLE


def test_absent_sidecar_stays_absent(session_env):
    from api import models

    draft, status, _redirect = models.read_composer_draft_sidecar_status("never-written")
    assert draft is None
    assert status == models.DRAFT_ABSENT


def test_zero_message_cleanup_keeps_owner_when_sidecar_is_unreadable(
    session_env, monkeypatch
):
    """A transient read error must not delete a valid owner and its draft."""
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "cleanup-unreadable"
    session = models.Session(session_id=sid, title="Untitled")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "recoverable", "files": []})
    _break_sidecar_read(monkeypatch, models, sid=sid)

    kept = routes._prune_orphaned_webui_zero_message_sessions([
        {"session_id": sid, "message_count": 0, "source": "webui"},
    ])

    assert [row["session_id"] for row in kept] == [sid], (
        "an unreadable sidecar was treated as no draft and the row was pruned"
    )
    assert models.composer_draft_sidecar_path(sid).exists()
    assert (session_dir / f"{sid}.json").exists()


def test_zero_message_cleanup_keeps_owner_when_sidecar_unlink_fails(
    session_env, monkeypatch
):
    """Reporting a clean prune while the sidecar survives leaves an orphan."""
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "cleanup-unlink-fails"
    session = models.Session(session_id=sid, title="Untitled")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "", "files": []})

    monkeypatch.setattr(models, "delete_composer_draft_sidecar", lambda _sid: False)
    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)

    kept = routes._prune_orphaned_webui_zero_message_sessions([
        {"session_id": sid, "message_count": 0, "source": "webui"},
    ])

    assert [row["session_id"] for row in kept] == [sid]
    assert (session_dir / f"{sid}.json").exists()


def test_session_delete_reports_failure_when_sidecar_survives(session_env, monkeypatch):
    """`/api/session/delete` must not answer ok while an orphan sidecar remains."""
    from api import models, routes

    _session_dir, _sessions = session_env
    sid = "delete-orphan-sidecar"
    session = models.Session(session_id=sid, title="Doomed")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "leftover", "files": []})

    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)

    captured = {}

    def fake_j(_handler, body, status=200, extra_headers=None):
        captured.update(payload=body, status=status)
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        routes, "bad",
        lambda handler, message, status=400: fake_j(handler, {"error": message}, status=status),
    )
    raw = json.dumps({"session_id": sid}).encode("utf-8")
    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(raw))},
        rfile=BytesIO(raw),
        _safe_webui_print=lambda *_a, **_k: None,
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/delete"))

    assert captured["status"] == 500, captured
    assert captured["payload"].get("ok") is False


def test_stale_pre_clear_write_is_rejected_not_applied(session_env, monkeypatch):
    """A write queued before the clear must not resurrect the sent text."""
    from api import models

    _session_dir, _sessions = session_env
    sid = "reorder-clear"
    models.Session(session_id=sid, title="Reorder").save(skip_index=True)

    first = _post_draft(monkeypatch, {"session_id": sid, "text": "queued text", "files": []})
    assert first["status"] == 200
    stale_rev = first["payload"]["rev"]

    cleared = _post_draft(monkeypatch, {"session_id": sid, "clear": True})
    assert cleared["status"] == 200
    assert cleared["payload"]["draft"] == {"text": "", "files": []}
    assert cleared["payload"]["rev"] > stale_rev

    # The autosave that was already in flight when the user hit send.
    late = _post_draft(
        monkeypatch,
        {"session_id": sid, "text": "queued text", "files": [], "rev": stale_rev},
    )

    assert late["status"] == 409, late
    assert late["payload"].get("stale") is True
    # The clear removed the sidecar and canonicalized the owner's legacy field;
    # neither may carry the resurrected text.
    assert models.read_composer_draft_sidecar(sid) is None, (
        "a pre-clear write resurrected the draft sidecar"
    )
    owner = models.Session.load(sid)
    assert models.resolve_composer_draft(sid, owner.composer_draft) == {
        "text": "", "files": [],
    }


def test_a_write_quoting_the_current_revision_still_applies(session_env, monkeypatch):
    """The fence must not block ordinary typing."""
    from api import models

    _session_dir, _sessions = session_env
    sid = "reorder-current"
    models.Session(session_id=sid, title="Reorder").save(skip_index=True)

    first = _post_draft(monkeypatch, {"session_id": sid, "text": "one", "files": []})
    second = _post_draft(
        monkeypatch,
        {"session_id": sid, "text": "two", "files": [], "rev": first["payload"]["rev"]},
    )

    assert second["status"] == 200
    assert models.resolve_composer_draft(sid)["text"] == "two"


def test_a_write_without_a_revision_still_applies(session_env, monkeypatch):
    """Old clients (and the first write of a session) send no revision."""
    from api import models

    _session_dir, _sessions = session_env
    sid = "reorder-norev"
    models.Session(session_id=sid, title="Reorder").save(skip_index=True)

    _post_draft(monkeypatch, {"session_id": sid, "text": "one", "files": []})
    _post_draft(monkeypatch, {"session_id": sid, "clear": True})
    late = _post_draft(monkeypatch, {"session_id": sid, "text": "two", "files": []})

    assert late["status"] == 200
    assert models.resolve_composer_draft(sid)["text"] == "two"


def test_rotation_installs_durable_continuation_routing(session_env, monkeypatch):
    """A late old-sid write must be told which sid is live, not accepted."""
    from api import models, streaming

    _session_dir, _sessions = session_env
    old_sid = "routing-old"
    new_sid = "routing-new"
    session = models.Session(session_id=old_sid, title="Before compression")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(old_sid, {"text": "carry me", "files": []})

    session.session_id = new_sid
    session.save(skip_index=True)
    streaming._preserve_pre_compression_snapshot(session, old_sid)

    assert models.composer_draft_redirect_target(old_sid) == new_sid
    assert models.resolve_composer_draft(new_sid)["text"] == "carry me"

    late = _post_draft(
        monkeypatch,
        {"session_id": old_sid, "text": "newest text typed after rotation", "files": []},
    )

    assert late["status"] == 409, late
    assert late["payload"]["session_id"] == new_sid
    # And nothing was stranded on the archived parent.
    assert models.read_composer_draft_sidecar(old_sid) is None


def test_continuation_routing_survives_a_restart(session_env):
    """The marker is on disk, not in process memory."""
    from api import models, streaming

    _session_dir, _sessions = session_env
    old_sid = "routing-restart-old"
    new_sid = "routing-restart-new"
    session = models.Session(session_id=old_sid, title="Before compression")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(old_sid, {"text": "carry me", "files": []})
    session.session_id = new_sid
    session.save(skip_index=True)
    streaming._preserve_pre_compression_snapshot(session, old_sid)

    models._DRAFT_SIDECAR_CACHE.clear()
    models._COMPOSER_DRAFT_REVISIONS.clear()

    assert models.composer_draft_redirect_target(old_sid) == new_sid


def test_migration_refuses_to_overwrite_an_unreadable_old_sidecar(
    session_env, monkeypatch
):
    from api import models

    _session_dir, _sessions = session_env
    old_sid = "migrate-unreadable-old"
    new_sid = "migrate-unreadable-new"
    models.write_composer_draft_sidecar(old_sid, {"text": "precious", "files": []})
    _break_sidecar_read(monkeypatch, models, sid=old_sid)

    models.migrate_composer_draft_sidecar(old_sid, new_sid)

    assert models.composer_draft_sidecar_path(old_sid).exists()
    assert not models.composer_draft_sidecar_path(new_sid).exists()


# ── Adversarial-review follow-ups ───────────────────────────────────────────


def test_a_write_racing_a_rotation_cannot_destroy_the_marker(session_env, monkeypatch):
    """The redirect probe above the lock can miss a marker installed under it.

    Both the draft POST and `migrate_composer_draft_sidecar` take the SAME
    per-session lock, so "compression wins the lock first" is ordinary
    scheduling, not an exotic race. Falling through then wrote a plain draft
    over the continuation marker and reopened the stranded-draft bug.
    """
    from api import models, routes

    _session_dir, _sessions = session_env
    old_sid = "race-rotation-old"
    new_sid = "race-rotation-new"
    session = models.Session(session_id=old_sid, title="Before compression")
    session.save(skip_index=True)
    models.write_composer_draft_sidecar(old_sid, {"text": "carry me", "files": []})
    models.Session(session_id=new_sid, title="After compression").save(skip_index=True)

    calls = {"n": 0}
    real_probe = models.composer_draft_redirect_target

    def probe_that_loses_the_race(sid):
        calls["n"] += 1
        if calls["n"] == 1 and sid == old_sid:
            # The compression thread wins the lock right after we looked.
            models.migrate_composer_draft_sidecar(old_sid, new_sid)
            return None
        return real_probe(sid)

    monkeypatch.setattr(routes, "composer_draft_redirect_target", probe_that_loses_the_race)

    response = _post_draft(
        monkeypatch,
        {"session_id": old_sid, "text": "typed after rotation", "files": []},
    )

    assert response["status"] == 409, response
    assert response["payload"]["session_id"] == new_sid
    assert models.composer_draft_redirect_target(old_sid) == new_sid, (
        "a draft write destroyed the continuation marker"
    )


def test_a_failed_sidecar_delete_leaves_the_session_intact_and_retryable(
    session_env, monkeypatch
):
    """Failing closed must not be worse than the problem it prevents.

    Unlinking the session file first and only then bailing out left the
    conversation gone while skipping the index prune, the tombstone, the
    attachment directory and the turn/run journal deletion — with no way to
    make progress on a retry.
    """
    from api import models, routes

    session_dir, _sessions = session_env
    sid = "delete-keeps-everything"
    models.Session(session_id=sid, title="Doomed").save(skip_index=True)
    models.write_composer_draft_sidecar(sid, {"text": "leftover", "files": []})

    pruned = []
    monkeypatch.setattr(routes, "delete_composer_draft_sidecar", lambda _sid: False)
    monkeypatch.setattr(routes, "prune_session_from_index", pruned.append)

    captured = {}

    def fake_j(_handler, body, status=200, extra_headers=None):
        captured.update(payload=body, status=status)
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        routes, "bad",
        lambda handler, message, status=400: fake_j(handler, {"error": message}, status=status),
    )
    raw = json.dumps({"session_id": sid}).encode("utf-8")
    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(raw))},
        rfile=BytesIO(raw),
        _safe_webui_print=lambda *_a, **_k: None,
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/delete"))

    assert captured["status"] == 500
    # Nothing was destroyed: the operation is fully retryable.
    assert (session_dir / f"{sid}.json").exists(), "the session file was deleted anyway"
    assert models.read_composer_draft_sidecar(sid) is not None
    assert pruned == [], "the index was pruned for a session that still exists"


def test_the_orphan_sweep_keeps_continuation_markers(session_env, monkeypatch):
    """A redirect marker is never an orphan, whatever happened to its parent."""
    from api import models, routes

    _session_dir, _sessions = session_env
    old_sid = "sweep-marker-old"
    new_sid = "sweep-marker-new"
    assert models.write_composer_draft_redirect(old_sid, new_sid)
    assert models.composer_draft_redirect_target(old_sid) == new_sid

    captured = {}
    monkeypatch.setattr(
        routes, "j",
        lambda _h, body, status=200, extra_headers=None: captured.update(
            payload=body, status=status
        ) or True,
    )
    raw = json.dumps({}).encode("utf-8")
    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(raw))},
        rfile=BytesIO(raw),
        _safe_webui_print=lambda *_a, **_k: None,
    )
    routes._handle_sessions_cleanup(handler, {})

    assert models.composer_draft_redirect_target(old_sid) == new_sid, (
        "the cleanup sweep deleted a continuation marker"
    )


def test_the_revision_fence_is_documented_as_process_scoped():
    """The limitation must be stated where a reader will find it."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "api" / "models.py").read_text(
        encoding="utf-8"
    )
    idx = src.index("_COMPOSER_DRAFT_REVISIONS: dict = {}")
    preamble = src[max(0, idx - 1400):idx]
    assert "WITHIN ONE PROCESS" in preamble
    assert "restart" in preamble
