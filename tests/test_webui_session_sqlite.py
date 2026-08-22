"""SQLite-backed session store regression tests."""
from __future__ import annotations

import json
import tempfile
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import api.models as models
import api.webui_session_sqlite as sqlite_db


def _tmp_session_dir():
    return Path(tempfile.mkdtemp())


def _sample_session_dict(sid: str = "test-session") -> dict:
    return {
        "session_id": sid,
        "title": "Test Session",
        "workspace": "/workspace",
        "model": "gpt-4",
        "created_at": 1000.0,
        "updated_at": 1001.0,
        "messages": [
            {"role": "user", "content": "hello", "timestamp": 1000.0},
            {"role": "assistant", "content": "hi", "timestamp": 1001.0},
        ],
        "tool_calls": [],
        "context_messages": [],
        "anchor_activity_scenes": {},
    }


def test_sqlite_store_round_trip():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-1")
    store.write_session(payload)

    loaded = store.read_session("sid-1")
    assert loaded is not None
    assert loaded["session_id"] == "sid-1"
    assert len(loaded["messages"]) == 2
    assert loaded["messages"][0]["role"] == "user"


def test_sqlite_store_update_metadata():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-2"))

    store.update_metadata("sid-2", {"composer_draft": {"text": "draft", "files": []}})
    loaded = store.read_session("sid-2")
    assert loaded["composer_draft"]["text"] == "draft"
    assert len(loaded["messages"]) == 2


def test_session_load_uses_sqlite_when_db_exists(monkeypatch):
    d = _tmp_session_dir()
    # Patch global session dir for this test.
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-3"))

    s = models.Session.load("sid-3")
    assert s is not None
    assert s.session_id == "sid-3"
    assert len(s.messages) == 2


def test_persisted_session_ids_includes_sqlite(monkeypatch):
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_PERSISTED_SESSION_IDS_CACHE", (None, None, frozenset()))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-4"))

    ids = models._persisted_session_ids_snapshot()
    assert "sid-4" in ids


def test_index_entry_exists_for_sqlite_only(monkeypatch):
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-5"))

    assert models._index_entry_exists("sid-5", in_memory_ids=set()) is True
    assert models._index_entry_exists("missing", in_memory_ids=set()) is False


def test_session_save_metadata_sqlite_only(monkeypatch):
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-6"))

    s = models.Session.load("sid-6")
    s.save_metadata({"composer_draft": {"text": "quick draft", "files": []}})

    reloaded = models.Session.load("sid-6")
    assert reloaded.composer_draft["text"] == "quick draft"
    assert len(reloaded.messages) == 2


def test_session_load_falls_back_to_json_when_sqlite_misses_row(monkeypatch):
    """If sessions.db exists but a session was not migrated, JSON sidecar is used."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    # Create a JSON-only session.
    sid = "sid-json-only"
    payload = _sample_session_dict(sid)
    (d / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    # Activating SQLite must not hide the JSON-only session.
    _ = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    s = models.Session.load(sid)
    assert s is not None
    assert s.session_id == sid
    assert len(s.messages) == 2


def test_session_load_falls_back_to_json_on_sqlite_read_error(monkeypatch):
    """A corrupt SQLite row must not block the JSON sidecar fallback.

    Greptile P1: read_session() raising (unreadable message_json, DB read
    error) propagated out of Session.load(), failing mutation requests for
    sessions whose sidecar is still valid. load_metadata_only() already
    degrades on store errors; Session.load() now does the same.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    sid = "sid-corrupt"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    # Sidecar holds the intact copy (e.g. pre-migration backup).
    (d / f"{sid}.json").write_text(
        json.dumps(_sample_session_dict(sid)), encoding="utf-8"
    )

    # Corrupt the SQLite message payload.
    conn = _sq.connect(str(d / "sessions.db"))
    conn.execute(
        "UPDATE messages SET message_json = 'not-json' WHERE session_id = ?",
        (sid,),
    )
    conn.commit()
    conn.close()

    s = models.Session.load(sid)
    assert s is not None
    assert s.session_id == sid
    assert len(s.messages) == 2


def test_save_metadata_routes_to_sidecar_after_sqlite_read_error(monkeypatch):
    """Greptile P1: an unreadable row must not black-hole draft autosaves.

    Session.load() falls back to the sidecar on a corrupt row; without the
    unreadable marker, save_metadata() kept routing drafts to SQLite (the
    row exists) where the next load can never read them — the autosave
    reported success while the draft stayed invisible.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    monkeypatch.setattr(models, "_SQLITE_UNREADABLE_SIDS", {})

    sid = "sid-corrupt-draft"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    (d / f"{sid}.json").write_text(
        json.dumps(_sample_session_dict(sid)), encoding="utf-8"
    )

    conn = _sq.connect(str(d / "sessions.db"))
    conn.execute(
        "UPDATE messages SET message_json = 'not-json' WHERE session_id = ?",
        (sid,),
    )
    conn.commit()
    conn.close()

    # Load detects the corrupt row and falls back to the sidecar.
    s = models.Session.load(sid)
    assert s is not None

    # The draft autosave must go to the sidecar — the store the next load reads.
    s.save_metadata({"composer_draft": {"text": "draft after corruption", "files": []}})

    reloaded = models.Session.load(sid)
    assert reloaded is not None
    assert reloaded.composer_draft["text"] == "draft after corruption"


def test_transient_read_error_without_sidecar_does_not_poison_routing(monkeypatch):
    """Greptile P1: a transient read error must not stick a migrated
    (sidecar-less) session to the JSON write path.

    The unreadable mark means "the sidecar just saved us" — with no sidecar
    there is nothing to route to, and a stuck mark would make save_metadata()
    read a missing sidecar and 500 every autosave after the DB recovers.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_SQLITE_UNREADABLE_SIDS", {})

    sid = "sid-transient"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_read = store.read_session
    calls = {"n": 0}

    def fail_once(sid_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_read(sid_)

    monkeypatch.setattr(store, "read_session", fail_once)

    # Transient failure, no sidecar: the session is unavailable this request,
    # but must NOT be marked unreadable.
    assert models.Session.load(sid) is None
    assert sid not in models._SQLITE_UNREADABLE_SIDS

    # DB recovered: loads and draft autosaves use SQLite normally.
    s = models.Session.load(sid)
    assert s is not None
    s.save_metadata({"composer_draft": {"text": "ok after recovery", "files": []}})
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "ok after recovery"


def test_marked_sid_reads_and_writes_sidecar_until_save_heals(monkeypatch):
    """While marked, the sidecar is authoritative for reads AND writes.

    After SQLite recovers, loads keep reading the sidecar (the store the
    marked-window drafts went to) until a full save() heals the row with
    the sidecar-loaded state; only then does routing return to SQLite.
    Clearing the mark on a bare read would flip loads back to the row and
    strand the sidecar-saved drafts (Greptile P1).
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_SQLITE_UNREADABLE_SIDS", {})

    sid = "sid-recover"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    (d / f"{sid}.json").write_text(
        json.dumps(_sample_session_dict(sid)), encoding="utf-8"
    )
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_read = store.read_session
    calls = {"n": 0}

    def fail_once(sid_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_read(sid_)

    monkeypatch.setattr(store, "read_session", fail_once)

    # Transient failure with a sidecar: fallback load succeeds, sid marked.
    assert models.Session.load(sid) is not None
    assert sid in models._SQLITE_UNREADABLE_SIDS

    # DB recovered, but while marked the sidecar stays authoritative:
    # drafts route to it and loads read from it.
    s = models.Session.load(sid)
    assert s is not None
    assert sid in models._SQLITE_UNREADABLE_SIDS
    s.save_metadata({"composer_draft": {"text": "sidecar draft", "files": []}})
    assert json.loads((d / f"{sid}.json").read_text(encoding="utf-8"))[
        "composer_draft"
    ]["text"] == "sidecar draft"
    assert models.Session.load(sid).composer_draft["text"] == "sidecar draft"

    # A full save() heals the row with the sidecar-loaded state and clears
    # the mark; routing returns to SQLite with the draft intact.
    s.save()
    assert sid not in models._SQLITE_UNREADABLE_SIDS
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "sidecar draft"
    (d / f"{sid}.json").unlink()
    reloaded = models.Session.load(sid)
    assert reloaded is not None
    assert reloaded.composer_draft["text"] == "sidecar draft"


def test_marked_sid_does_not_touch_sqlite_row_until_save(monkeypatch):
    """While marked, no reconcile write may touch the row — so a stale
    sidecar can never clobber a newer SQLite draft automatically."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_SQLITE_UNREADABLE_SIDS", {})

    sid = "sid-noclobber"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    store.update_metadata(sid, {"composer_draft": {"text": "newer in sqlite", "files": []}})
    sidecar_payload = _sample_session_dict(sid)
    sidecar_payload["composer_draft"] = {"text": "older in sidecar", "files": []}
    (d / f"{sid}.json").write_text(json.dumps(sidecar_payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_read = store.read_session
    calls = {"n": 0}

    def fail_once(sid_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_read(sid_)

    monkeypatch.setattr(store, "read_session", fail_once)

    assert models.Session.load(sid) is not None
    assert sid in models._SQLITE_UNREADABLE_SIDS

    # DB recovered; while marked, loads keep reading the sidecar and the
    # row keeps its newer draft — nothing is reconciled or clobbered.
    assert models.Session.load(sid) is not None
    assert sid in models._SQLITE_UNREADABLE_SIDS
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "newer in sqlite"


def test_marked_sid_without_sidecar_falls_back_to_healthy_row(monkeypatch):
    """If the sidecar disappears while marked and the row reads healthy,
    the mark clears — there is nothing left to protect."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_SQLITE_UNREADABLE_SIDS", {})

    sid = "sid-gone-sidecar"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    (d / f"{sid}.json").write_text(
        json.dumps(_sample_session_dict(sid)), encoding="utf-8"
    )
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_read = store.read_session
    calls = {"n": 0}

    def fail_once(sid_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_read(sid_)

    monkeypatch.setattr(store, "read_session", fail_once)

    assert models.Session.load(sid) is not None
    assert sid in models._SQLITE_UNREADABLE_SIDS

    (d / f"{sid}.json").unlink()
    s = models.Session.load(sid)
    assert s is not None
    assert sid not in models._SQLITE_UNREADABLE_SIDS


def test_full_save_heals_corrupt_sqlite_row(monkeypatch):
    """A successful full save() rewrites the row with healthy data and
    clears the unreadable mark, so draft routing returns to SQLite."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    monkeypatch.setattr(models, "_SQLITE_UNREADABLE_SIDS", {})

    sid = "sid-heal"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    (d / f"{sid}.json").write_text(
        json.dumps(_sample_session_dict(sid)), encoding="utf-8"
    )

    conn = _sq.connect(str(d / "sessions.db"))
    conn.execute(
        "UPDATE messages SET message_json = 'not-json' WHERE session_id = ?",
        (sid,),
    )
    conn.commit()
    conn.close()

    s = models.Session.load(sid)
    assert sid in models._SQLITE_UNREADABLE_SIDS

    s.save()
    assert sid not in models._SQLITE_UNREADABLE_SIDS

    # The row is healthy again: loads come from SQLite, drafts route there.
    (d / f"{sid}.json").unlink()
    reloaded = models.Session.load(sid)
    assert reloaded is not None
    assert len(reloaded.messages) == 2


def test_save_metadata_falls_back_to_json_for_unmigrated_session(monkeypatch):
    """sessions.db exists but the session is JSON-only: draft autosave must
    persist to the sidecar, not raise KeyError from a zero-row SQLite UPDATE."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    sid = "sid-json-draft"
    payload = _sample_session_dict(sid)
    (d / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    # Activate the SQLite store without migrating this session.
    _ = sqlite_db.WebUISqliteSessionDB(session_dir=d)

    s = models.Session.load(sid)
    assert s is not None
    s.save_metadata({"composer_draft": {"text": "json draft", "files": []}})

    on_disk = json.loads((d / f"{sid}.json").read_text(encoding="utf-8"))
    assert on_disk["composer_draft"]["text"] == "json draft"

    reloaded = models.Session.load(sid)
    assert reloaded.composer_draft["text"] == "json draft"
    assert len(reloaded.messages) == 2


def test_save_metadata_json_fallback_updates_in_memory_session(monkeypatch):
    """Greptile P1 (r3745331728): the JSON-fallback branch of save_metadata()
    must keep the in-memory Session consistent with the sidecar it just wrote.

    The draft route happens to pre-set ``s.composer_draft`` before calling
    save_metadata(), which masks the asymmetry; this test exercises the method
    directly without that pre-set, so it fails if the JSON branch forgets to
    setattr the object (the original bug left ``s.composer_draft`` as ``{}``).
    """
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    sid = "sid-json-mem"
    payload = _sample_session_dict(sid)
    (d / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    # sessions.db exists but this session was not migrated into it.
    _ = sqlite_db.WebUISqliteSessionDB(session_dir=d)

    s = models.Session.load(sid)
    assert s is not None
    assert s.composer_draft == {}

    # Deliberately do NOT pre-set s.composer_draft: save_metadata() must own
    # keeping the in-memory object consistent with the persisted sidecar.
    s.save_metadata({"composer_draft": {"text": "mem draft", "files": []}})

    # In-memory object must reflect the persisted draft (staleness would leave
    # this as the original empty dict).
    assert s.composer_draft == {"text": "mem draft", "files": []}

    # And the sidecar on disk must match.
    on_disk = json.loads((d / f"{sid}.json").read_text(encoding="utf-8"))
    assert on_disk["composer_draft"]["text"] == "mem draft"


def test_session_exists():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-exists"))

    assert store.session_exists("sid-exists") is True
    assert store.session_exists("sid-missing") is False
    assert store.session_exists("../escape") is False


def test_update_metadata_does_not_advance_updated_at_for_drafts():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-draft"))

    loaded = store.read_session("sid-draft")
    original_updated_at = loaded["updated_at"]

    store.update_metadata("sid-draft", {"composer_draft": {"text": "draft", "files": []}})

    reloaded = store.read_session("sid-draft")
    assert reloaded["composer_draft"]["text"] == "draft"
    assert reloaded["updated_at"] == original_updated_at


def test_update_metadata_advances_updated_at_when_requested():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-touch"))

    new_time = 9999.0
    store.update_metadata("sid-touch", {"updated_at": new_time})

    reloaded = store.read_session("sid-touch")
    assert reloaded["updated_at"] == new_time


def test_save_refuses_metadata_only_sqlite_session(monkeypatch):
    """#1558 P0 guard must also protect the SQLite fast path.

    Session.save() on a session loaded with metadata_only=True would
    otherwise write messages=[] through write_session(), replacing the
    message tables and wiping the transcript.
    """
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-1558"))

    s = models.Session.load_metadata_only("sid-1558")
    assert s is not None
    assert getattr(s, "_loaded_metadata_only", False) is True

    try:
        s.save()
    except RuntimeError:
        pass
    else:
        raise AssertionError("save() must refuse metadata-only sessions on the SQLite path")

    reloaded = store.read_session("sid-1558")
    assert reloaded is not None
    assert len(reloaded["messages"]) == 2


def test_sqlite_store_delete_session_removes_all_rows():
    import sqlite3 as _sq

    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-del")
    payload["anchor_activity_scenes"] = {"h1": {"scene": "data"}}
    store.write_session(payload)
    assert store.session_exists("sid-del")

    assert store.delete_session("sid-del") is True
    assert store.session_exists("sid-del") is False
    assert store.read_session("sid-del") is None

    # No orphan rows left in any child table.
    conn = _sq.connect(str(d / "sessions.db"))
    try:
        for table in ("sessions", "messages", "tool_calls", "context_messages", "anchor_scenes"):
            col = "scene_hash" if table == "anchor_scenes" else "session_id"
            (count,) = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} = ?",
                ("sid-del" if table != "anchor_scenes" else "h1",),
            ).fetchone()
            assert count == 0, f"{table} still has rows for sid-del"
    finally:
        conn.close()

    # Deleting a missing session is a no-op.
    assert store.delete_session("sid-del") is False


def test_save_metadata_sqlite_write_failure_leaves_in_memory_untouched(monkeypatch):
    """A failed SQLite metadata write must not poison the cached Session."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-writefail"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    s = models.Session.load(sid)
    old_draft = dict(getattr(s, "composer_draft", {}) or {})

    def _boom(sid_, fields):
        raise _sq.OperationalError("database is locked")

    monkeypatch.setattr(store, "update_metadata", _boom)

    try:
        s.save_metadata({"composer_draft": {"text": "lost?", "files": []}})
    except _sq.OperationalError:
        pass
    else:
        raise AssertionError("write failure must propagate")

    assert (getattr(s, "composer_draft", {}) or {}) == old_draft


def test_save_metadata_json_write_failure_leaves_in_memory_untouched(monkeypatch):
    """A failed sidecar write must not poison the cached Session either."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    sid = "sid-jsonfail"
    sidecar = d / f"{sid}.json"
    sidecar.write_text(json.dumps(_sample_session_dict(sid)), encoding="utf-8")

    s = models.Session.load(sid)
    old_draft = dict(getattr(s, "composer_draft", {}) or {})

    monkeypatch.setattr(
        models,
        "_safe_replace",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    try:
        s.save_metadata({"composer_draft": {"text": "lost?", "files": []}})
    except OSError:
        pass
    else:
        raise AssertionError("write failure must propagate")

    assert (getattr(s, "composer_draft", {}) or {}) == old_draft
    assert "composer_draft" not in json.loads(sidecar.read_text(encoding="utf-8"))


def test_draft_route_retry_after_sqlite_write_failure_persists(monkeypatch):
    """Greptile P1: a failed write must not poison the draft cache.

    save_metadata() applied the in-memory update before the SQLite write,
    so a failed write left s.composer_draft ahead of disk; the route's
    unchanged fast path then skipped the retry and the draft vanished on
    reload. The route drives POST /api/session/draft through the real
    dispatcher, fail-once the write, and the identical retry must persist.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    sid = "sid-poison"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_update = store.update_metadata
    calls = {"n": 0}

    def fail_once(sid_, fields):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_update(sid_, fields)

    monkeypatch.setattr(store, "update_metadata", fail_once)

    # First attempt: the write error propagates (production turns it into a 500).
    try:
        _drive_draft_post(monkeypatch, {"session_id": sid, "text": "retry me"})
    except Exception:
        pass
    else:
        raise AssertionError("the failed write must surface")

    # The cached draft must NOT have advanced — the unchanged fast path on
    # the retry compares against it.
    s = models.get_session(sid)
    assert (getattr(s, "composer_draft", {}) or {}).get("text", "") != "retry me"

    # Identical retry: must persist this time (not hit the unchanged path).
    captured = _drive_draft_post(monkeypatch, {"session_id": sid, "text": "retry me"})
    assert captured.get("payload", {}).get("ok") is True
    assert "unchanged" not in captured.get("payload", {})
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "retry me"


def test_delete_route_removes_sqlite_rows_and_index_does_not_resurrect(monkeypatch):
    """POST /api/session/delete must remove SQLite-backed sessions too.

    Migrated sessions have no sidecar; previously the route only unlinked
    the sidecar, leaving the sessions.db row — and a full index rebuild then
    resurrected the deleted session in the sidebar.
    """
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-del"))

    import api.routes as routes

    # Keep the CLI state.db out of the test; the WebUI store is under test.
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    captured = _drive_delete_post(monkeypatch, {"session_id": "sid-del"})
    assert captured.get("payload", {}).get("ok") is True
    assert store.session_exists("sid-del") is False

    # A full index rebuild must not resurrect the deleted session.
    index_file = d / "_index.json"
    if index_file.exists():
        index_file.unlink()
    models._write_session_index(updates=None, session_dir=d, session_index_file=index_file)
    entries = json.loads(index_file.read_text())
    assert all(e.get("session_id") != "sid-del" for e in entries)


def test_delete_route_handles_unmigrated_json_session_with_store_active(monkeypatch):
    """Mixed-store delete: sessions.db exists but the session is JSON-only."""
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)  # activates sessions.db
    sidecar = d / "sid-json.json"
    sidecar.write_text(json.dumps(_sample_session_dict("sid-json")), encoding="utf-8")

    import api.routes as routes

    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    captured = _drive_delete_post(monkeypatch, {"session_id": "sid-json"})
    assert captured.get("payload", {}).get("ok") is True
    assert not sidecar.exists()
    assert store.session_exists("sid-json") is False


def test_pre_compression_snapshot_check_reads_sqlite_store(monkeypatch):
    """_is_pre_compression_snapshot_id must work for migrated sessions.

    The sidebar lineage grouping reads the sidecar directly; a migrated
    (SQLite-only) snapshot parent would look like a non-snapshot and its
    continuation rows would lose their grouping.
    """
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    snap = _sample_session_dict("snap_parent")
    snap["pre_compression_snapshot"] = True
    store.write_session(snap)
    store.write_session(_sample_session_dict("plain_parent"))

    import api.routes as routes

    assert routes._is_pre_compression_snapshot_id("snap_parent") is True
    assert routes._is_pre_compression_snapshot_id("plain_parent") is False
    assert routes._is_pre_compression_snapshot_id("missing_parent") is False


def _drive_delete_post(monkeypatch, body):
    """Run POST /api/session/delete through routes.handle_post (CSRF bypassed,
    JSON responders captured) and return the captured response."""
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400, extra_headers=None: captured.update(
            error=msg, status=status
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None, pretty=True: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    handler = SimpleNamespace(command="POST", _safe_webui_print=lambda *_a, **_k: None)
    assert routes.handle_post(handler, SimpleNamespace(path="/api/session/delete")) is True
    return captured


# ── Route-level regressions: POST /api/session/draft through the real
# routes.handle_post dispatcher, the way the composer's 400ms debounced
# auto-save actually reaches this code in production. The unit tests above
# call save_metadata() directly; these prove the store ordering holds
# end-to-end (dispatch → get_session → save_metadata → persistence). ──────


def _patch_route_state(monkeypatch, d):
    """Point models+routes session state at an isolated tmpdir."""
    import api.routes as routes

    sessions = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", sessions)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    monkeypatch.setattr(routes, "SESSION_DIR", d)
    monkeypatch.setattr(routes, "SESSIONS", sessions)


def _drive_draft_post(monkeypatch, body):
    """Run POST /api/session/draft through routes.handle_post (CSRF bypassed,
    JSON responders captured) and return the captured response."""
    import api.routes as routes

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400, extra_headers=None: captured.update(
            error=msg, status=status
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None, pretty=True: captured.update(
            payload=payload, status=status
        )
        or True,
    )
    handler = SimpleNamespace(command="POST", _safe_webui_print=lambda *_a, **_k: None)
    assert routes.handle_post(handler, SimpleNamespace(path="/api/session/draft")) is True
    return captured


def test_draft_route_sqlite_ordering_persists_to_migrated_row(monkeypatch):
    """sessions.db active + session migrated: the draft autosave must update
    only the SQLite sessions row — no JSON sidecar is created, the transcript
    is untouched, and updated_at does not move (a keystroke is not activity)."""
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-route-sqlite"))

    captured = _drive_draft_post(
        monkeypatch,
        {"session_id": "sid-route-sqlite", "text": "sqlite route draft", "files": []},
    )

    assert captured.get("status") == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["draft"]["text"] == "sqlite route draft"

    row = store.read_session("sid-route-sqlite")
    assert row["composer_draft"]["text"] == "sqlite route draft"
    assert len(row["messages"]) == 2
    assert row["updated_at"] == 1001.0
    assert not (d / "sid-route-sqlite.json").exists()


def test_draft_route_json_fallback_ordering_for_unmigrated_session(monkeypatch):
    """Mixed-store ordering (the 3026ecb production failure): sessions.db
    exists but this session was never migrated. Before the session_exists()
    gate, save_metadata() routed the draft to SQLite, the UPDATE matched zero
    rows, the follow-up lookup raised KeyError, and the draft was persisted
    nowhere. The route must return ok and write the JSON sidecar."""
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    sid = "sid-route-json-only"
    (d / f"{sid}.json").write_text(json.dumps(_sample_session_dict(sid)), encoding="utf-8")
    # Activate sessions.db WITHOUT migrating this session into it.
    _ = sqlite_db.WebUISqliteSessionDB(session_dir=d)

    captured = _drive_draft_post(
        monkeypatch, {"session_id": sid, "text": "json route draft", "files": []}
    )

    assert captured.get("status") == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["draft"]["text"] == "json route draft"

    on_disk = json.loads((d / f"{sid}.json").read_text(encoding="utf-8"))
    assert on_disk["composer_draft"]["text"] == "json route draft"
    assert on_disk["updated_at"] == 1001.0
    assert len(on_disk["messages"]) == 2

    # A fresh load (cold cache, e.g. after restart) reads the draft back.
    reloaded = models.Session.load(sid)
    assert reloaded.composer_draft["text"] == "json route draft"
    assert len(reloaded.messages) == 2
