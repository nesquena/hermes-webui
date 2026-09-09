"""SQLite-backed session store regression tests."""
from __future__ import annotations

import json
import tempfile
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _tok(store, sid: str) -> dict:
    """The current durable (generation, incarnation) writer token for sid."""
    ver = store.read_row_version(sid)
    assert ver is not None, f"no live row for {sid}"
    return {
        "expected_generation": ver["generation"],
        "expected_incarnation": ver["incarnation"],
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

    store.update_metadata(
        "sid-2", {"composer_draft": {"text": "draft", "files": []}}, **_tok(store, "sid-2")
    )
    loaded = store.read_session("sid-2")
    assert loaded["composer_draft"]["text"] == "draft"
    assert len(loaded["messages"]) == 2


def test_sqlite_round_trip_preserves_all_session_fields():
    """Gate finding: SQLite round-trips must be lossless."""
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-full")
    payload.update(
        {
            "created_workspace": "/ws/created",
            "intentional_shrink_generation": "gen-7",
            "user_id": "user-1",
            "chat_id": "chat-9",
            "session_key": "key-abc",
            "platform": "discord",
            "enabled_toolsets": ["fs", "shell"],
            "process_wakeup_pause": {"paused": True},
        }
    )
    store.write_session(payload)
    loaded = store.read_session("sid-full")
    for k, v in payload.items():
        assert loaded.get(k) == v, f"field {k!r} did not round-trip: {loaded.get(k)!r} != {v!r}"


def test_sqlite_round_trip_preserves_unknown_top_level_keys():
    """extra_json: fields this schema version does not know survive."""
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-unknown")
    payload["future_feature_flag"] = {"enabled": True, "tier": 3}
    payload["brand_new_scalar"] = "hello-future"
    store.write_session(payload)
    loaded = store.read_session("sid-unknown")
    assert loaded["future_feature_flag"] == {"enabled": True, "tier": 3}
    assert loaded["brand_new_scalar"] == "hello-future"
    # And via update_metadata merge.
    store.update_metadata("sid-unknown", {"another_future_key": [1, 2]}, **_tok(store, "sid-unknown"))
    assert store.read_session("sid-unknown")["another_future_key"] == [1, 2]
    assert store.read_session("sid-unknown")["brand_new_scalar"] == "hello-future"


def test_session_level_unknown_keys_round_trip(monkeypatch):
    """Session(**data) with unknown keys -> save() -> store keeps them."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-bag"))

    data = store.read_session("sid-bag")
    data["custom_messaging_field"] = {"channel": "tg", "thread": 42}
    s = models.Session(**data)
    s.save()

    loaded = store.read_session("sid-bag")
    assert loaded["custom_messaging_field"] == {"channel": "tg", "thread": 42}


def test_schema_v2_migration_adds_columns_to_legacy_db():
    """A db created by the pre-v2 schema gains the new columns on open."""
    import re as _re
    import sqlite3 as _sq

    d = _tmp_session_dir()
    db_path = d / "sessions.db"
    # Simulate a v1 database: the current schema minus the v2 columns.
    v2_cols = set(sqlite_db._SESSIONS_V2_COLUMNS)
    v1_lines = []
    for line in sqlite_db.SCHEMA_SQL.splitlines():
        stripped = line.strip()
        if stripped and stripped.split()[0].rstrip(",") in v2_cols:
            continue
        v1_lines.append(line)
    v1_schema = _re.sub(r",(\s*\);)", r"\1", "\n".join(v1_lines))
    conn = _sq.connect(str(db_path))
    conn.executescript(v1_schema)
    conn.execute(
        "INSERT INTO sessions (session_id, title, updated_at) VALUES ('s1', 'legacy', 1.0)"
    )
    conn.commit()
    conn.close()

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    loaded = store.read_session("s1")
    assert loaded is not None
    assert loaded["title"] == "legacy"

    # The legacy v1 row was backfilled to incarnation 1 by the v5 authority
    # seed; its generation is still 0, so the token exercises a
    # generation-0 legacy row.
    store.update_metadata("s1", {"user_id": "u1", "new_stuff": {"x": 1}}, **_tok(store, "s1"))
    reloaded = store.read_session("s1")
    assert reloaded["user_id"] == "u1"
    assert reloaded["new_stuff"] == {"x": 1}

    conn = _sq.connect(str(db_path))
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    conn.close()
    assert version == sqlite_db._SCHEMA_VERSION


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
    monkeypatch.setattr(models, "_PERSISTED_SESSION_IDS_CACHE", (None, None, None, frozenset()))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-4"))

    ids = models._persisted_session_ids_snapshot()
    assert "sid-4" in ids


def test_generation_cas_refuses_stale_writers():
    """Gate finding: the fence is a durable per-session generation CAS.

    updated_at cannot fence real stale Session objects (save() stamps
    time.time() before writing); only the generation the object was loaded
    with can. Direct writes without lineage to an existing row are refused;
    matching lineage bumps; stale lineage is refused; force heals.
    """
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)

    store.write_session(_sample_session_dict("sid-fence"))  # generation 1

    # No lineage (expected_generation=None) onto an existing row: refused.
    try:
        store.write_session(_sample_session_dict("sid-fence"))
    except sqlite_db.StaleSessionWriteError:
        pass
    else:
        raise AssertionError("lineage-less overwrite must be refused")

    # Matching lineage: compare-and-bump (the token is the pair).
    store.write_session(
        _sample_session_dict("sid-fence"), expected_generation=1, expected_incarnation=1
    )
    assert store.read_session("sid-fence")["generation"] == 2

    # Stale lineage: refused.
    try:
        store.write_session(
            _sample_session_dict("sid-fence"), expected_generation=1, expected_incarnation=1
        )
    except sqlite_db.StaleSessionWriteError:
        pass
    else:
        raise AssertionError("stale generation must be refused")

    # force bypasses for deliberate heals and bumps anyway.
    store.write_session(_sample_session_dict("sid-fence"), force=True)
    assert store.read_session("sid-fence")["generation"] == 3

    # Right generation + wrong incarnation: refused — the fence is the pair,
    # not the generation alone.
    try:
        store.write_session(
            _sample_session_dict("sid-fence"), expected_generation=3, expected_incarnation=9
        )
    except sqlite_db.StaleSessionWriteError:
        pass
    else:
        raise AssertionError("right generation + wrong incarnation must be refused")
    assert store.read_session("sid-fence")["generation"] == 3


def test_stale_save_after_delete_is_rejected(monkeypatch):
    """Per-SID incarnation authority: delete retires the SID durably, so a
    Session loaded before the delete cannot resurrect the transcript by
    saving with its pre-delete generation (previously an unconditional
    generation-1 insert)."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-stale-del"))  # generation 1
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    s = models.Session.load("sid-stale-del")
    assert s is not None
    assert s._persisted_generation == 1

    assert store.delete_session("sid-stale-del") is True

    # The stale in-memory Session must be refused, not resurrect the row.
    with pytest.raises(sqlite_db.DeletedSessionWriteError):
        s.save()
    assert store.session_exists("sid-stale-del") is False
    assert store.read_session("sid-stale-del") is None

    # The delete retired the SID in the durable incarnation authority.
    row = store._conn().execute(
        "SELECT retired, retired_generation FROM session_incarnations "
        "WHERE session_id = ?",
        ("sid-stale-del",),
    ).fetchone()
    assert row is not None
    assert row["retired"] == 1
    assert row["retired_generation"] == 1

    # A direct write carrying pre-delete lineage is rejected the same way.
    with pytest.raises(sqlite_db.DeletedSessionWriteError):
        store.write_session(
            _sample_session_dict("sid-stale-del"), expected_generation=1
        )
    assert store.session_exists("sid-stale-del") is False


def test_explicit_same_id_recreation_requires_lease(monkeypatch):
    """A deleted SID is retired: plain writes and force cannot recreate it;
    only an explicit fresh-incarnation lease starts a new incarnation at
    generation 1 — and a pre-delete Session stays stale across the lease."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-lease"))  # generation 1
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    held = models.Session.load("sid-lease")
    assert held is not None
    # A second owner stays at generation 1 / incarnation 1: the
    # equal-generation ordinary-path leg below never advances it.
    owner = models.Session.load("sid-lease")
    assert owner is not None
    assert owner._persisted_generation == 1
    assert owner._persisted_incarnation == 1
    held.title = "advanced before delete"
    held.save()  # generation 2; held._persisted_generation == 2

    assert store.delete_session("sid-lease") is True

    # No lease: a lineage-less recreate of a retired SID is refused.
    with pytest.raises(sqlite_db.RetiredSessionWriteError):
        store.write_session(_sample_session_dict("sid-lease"))
    # force never inserts an absent row either.
    with pytest.raises(sqlite_db.StaleSessionWriteError):
        store.write_session(_sample_session_dict("sid-lease"), force=True)
    assert store.session_exists("sid-lease") is False

    # Leased recreate: new incarnation, row starts at generation 1.
    recreated = _sample_session_dict("sid-lease")
    recreated["title"] = "recreated incarnation"
    store.write_session(recreated, fresh_incarnation=True)
    loaded = store.read_session("sid-lease")
    assert loaded is not None
    assert loaded["generation"] == 1
    row = store._conn().execute(
        "SELECT incarnation, retired FROM session_incarnations WHERE session_id = ?",
        ("sid-lease",),
    ).fetchone()
    assert row["incarnation"] == 2
    assert row["retired"] == 0

    # Equal-generation stale writer: owner holds (generation 1, incarnation 1)
    # and the recreated row is ALSO generation 1 — only the incarnation half
    # of the token discriminates the old owner from the new incarnation.
    owner.title = "stale equal-generation write"
    with pytest.raises(sqlite_db.StaleSessionWriteError):
        owner.save()
    # The recreated incarnation's row is intact.
    assert store.read_session("sid-lease")["title"] == "recreated incarnation"
    assert store.read_row_version("sid-lease") == {"generation": 1, "incarnation": 2}
    fresh = models.Session.load("sid-lease")
    assert fresh is not None
    assert fresh.title == "recreated incarnation"

    # A still-held pre-delete Session remains a stale writer across the lease.
    held.title = "stale pre-delete write"
    with pytest.raises(sqlite_db.StaleSessionWriteError):
        held.save()
    assert store.read_session("sid-lease")["title"] == "recreated incarnation"


def test_two_readers_stale_writer_is_rejected(monkeypatch):
    """Gate finding: load A and B, save A, then stale B must be rejected."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-2readers"))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    a = models.Session.load("sid-2readers")
    b = models.Session.load("sid-2readers")
    assert a is not None and b is not None
    assert a._persisted_generation == 1
    assert b._persisted_generation == 1

    a.title = "saved by A"
    a.save()
    assert a._persisted_generation == 2
    assert store.read_session("sid-2readers")["title"] == "saved by A"

    b.title = "stale B write"
    try:
        b.save()
    except sqlite_db.StaleSessionWriteError:
        pass
    else:
        raise AssertionError("stale reader B must be rejected")

    assert store.read_session("sid-2readers")["title"] == "saved by A"

    # A remains writeable after its successful save (generation updated).
    a.title = "saved again by A"
    a.save()
    assert store.read_session("sid-2readers")["title"] == "saved again by A"


def test_null_valued_keys_round_trip_with_presence(monkeypatch):
    """Key-presence contract: an explicitly-None field reads back as a
    present None, matching the JSON backend's native behavior."""
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)

    payload = _sample_session_dict("sid-nulls")
    payload["personality"] = None
    payload["project_id"] = None
    payload["user_id"] = None
    store.write_session(payload)

    loaded = store.read_session("sid-nulls")
    assert "personality" in loaded and loaded["personality"] is None
    assert "project_id" in loaded and loaded["project_id"] is None
    assert "user_id" in loaded and loaded["user_id"] is None

    # update_metadata maintains presence in both directions.
    store.update_metadata(
        "sid-nulls",
        {"personality": "now-set", "threshold_tokens": None},
        **_tok(store, "sid-nulls"),
    )
    reloaded = store.read_session("sid-nulls")
    assert reloaded["personality"] == "now-set"
    assert "threshold_tokens" in reloaded and reloaded["threshold_tokens"] is None


def test_marked_save_does_not_launder_stale_sidecar_over_sql(monkeypatch):
    """A marked (unreadable) sid's save() must fail closed: never write the
    stale sidecar state into the SQL row, keep the unreadable mark, and only
    rewrite the sidecar (the marked authority)."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-nolaunder"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    # Differing transcripts: SQL row is newer with distinct children.
    row_payload = _sample_session_dict(sid)
    row_payload["messages"] = [
        {"role": "user", "content": "old", "timestamp": 1000.0},
        {"role": "assistant", "content": "NEWER SQL CHILD", "timestamp": 1001.0},
    ]
    store.write_session(row_payload)
    sidecar_payload = _sample_session_dict(sid)
    sidecar_payload["messages"] = [
        {"role": "user", "content": "stale sidecar only", "timestamp": 1000.0},
    ]
    (d / f"{sid}.json").write_text(json.dumps(sidecar_payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    # Capture original message bodies, then corrupt the SQL transcript so the
    # next read fails and the sid is marked.
    conn = _sq.connect(str(d / "sessions.db"))
    originals = [
        r[0] for r in conn.execute(
            "SELECT message_json FROM messages WHERE session_id = ? ORDER BY idx",
            (sid,),
        ).fetchall()
    ]
    conn.execute("UPDATE messages SET message_json = 'bad' WHERE session_id = ?", (sid,))
    conn.commit()
    conn.close()

    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids
    generation_at_mark = store.read_metadata_only(sid)["generation"]

    s.save()  # must not raise StaleSessionWriteError and must not touch SQL

    # Fail-closed: the mark is retained and the SQL row generation is unmoved.
    assert sid in store.unreadable_sids
    assert store.read_metadata_only(sid)["generation"] == generation_at_mark

    # Repair the transcript and prove the SQL children survived — no stale
    # sidecar body was laundered over them.
    conn = _sq.connect(str(d / "sessions.db"))
    for idx, original in enumerate(originals):
        conn.execute(
            "UPDATE messages SET message_json = ? WHERE session_id = ? AND idx = ?",
            (original, sid, idx),
        )
    conn.commit()
    conn.close()
    children = [m["content"] for m in store.read_session(sid)["messages"]]
    assert children == ["old", "NEWER SQL CHILD"]

    # The sidecar (the marked authority) may be rewritten from the in-memory
    # object — assert its contents independently of SQL.
    on_disk = json.loads((d / f"{sid}.json").read_text(encoding="utf-8"))
    assert [m["content"] for m in on_disk["messages"]] == ["stale sidecar only"]


def test_incomplete_migration_does_not_activate_store(monkeypatch):
    """Gate finding: sessions.db existence alone must not grant authority."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.set_meta("created_by", "migration")
    store.set_meta("migration_complete", "0")
    store.write_session(_sample_session_dict("sid-migrated-partial"))
    store.close()

    # Interrupted migration: the store must NOT take authority.
    assert models._get_sqlite_session_store() is False

    # Stamping completion activates it.
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.set_meta("migration_complete", "1")
    store.close()
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    assert models._get_sqlite_session_store()


def test_app_created_store_is_active_by_default(monkeypatch):
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    sqlite_db.WebUISqliteSessionDB(session_dir=d)  # creates sessions.db (created_by=app)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)
    assert models._get_sqlite_session_store()


def test_persisted_session_ids_snapshot_tracks_sqlite_revision(monkeypatch):
    """Gate finding: WAL commits do not move the directory mtime, so the
    persisted-ids cache must key on the store revision instead."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "_PERSISTED_SESSION_IDS_CACHE", (None, None, None, frozenset()))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-r1"))
    first = models._persisted_session_ids_snapshot()
    assert "sid-r1" in first

    # Second write: the directory mtime does not move for a WAL commit to an
    # existing sessions.db, but the revision bump must refresh the snapshot.
    store.write_session(_sample_session_dict("sid-r2"))
    second = models._persisted_session_ids_snapshot()
    assert "sid-r2" in second
    assert second is not first

    # Deletes bump the revision too.
    store.delete_session("sid-r2")
    third = models._persisted_session_ids_snapshot()
    assert "sid-r2" not in third
    assert "sid-r1" in third


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
    assert sid not in store.unreadable_sids

    # DB recovered: loads and draft autosaves use SQLite normally.
    s = models.Session.load(sid)
    assert s is not None
    s.save_metadata({"composer_draft": {"text": "ok after recovery", "files": []}})
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "ok after recovery"


def test_recovery_demotes_mark_and_carries_draft(monkeypatch):
    """Greptile P1 lifecycle: after the row recovers, the next load demotes
    the mark and carries marked-window drafts into the row; stale
    sidecar-loaded writers are refused by the generation CAS."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-recover2"
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
    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids

    # While marked, the draft autosave routes to the sidecar.
    s.save_metadata({"composer_draft": {"text": "typed during outage", "files": []}})

    # Recovery: the next load demotes the mark and carries the draft.
    s2 = models.Session.load(sid)
    assert s2 is not None
    assert sid not in store.unreadable_sids
    assert s2.composer_draft["text"] == "typed during outage"
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "typed during outage"
    # The demote's draft carry is a metadata write, and metadata writers move
    # the generation fence (1 -> 2); the demoted load is constructed at the
    # current generation so its own saves still pass the CAS.
    assert s2._persisted_generation == 2

    # The stale sidecar-loaded object is now a stale reader: refused loudly
    # instead of rolling the recovered row back.
    try:
        s.title = "stale write"
        s.save()
    except sqlite_db.StaleSessionWriteError:
        pass
    else:
        raise AssertionError("stale sidecar-loaded writer must be refused")

    assert store.read_session(sid)["title"] == "Test Session"


def test_recovered_row_keeps_its_own_draft_on_demote(monkeypatch):
    """Demote-on-recovery never reconciles an unchanged sidecar draft, so
    the row's own newer draft survives."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-noclobber"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    store.update_metadata(
        sid, {"composer_draft": {"text": "newer in sqlite", "files": []}}, **_tok(store, sid)
    )
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
    assert sid in store.unreadable_sids

    # DB recovered: the next load demotes the mark. The sidecar draft never
    # moved while marked, so nothing is carried — the row keeps its draft.
    assert models.Session.load(sid) is not None
    assert sid not in store.unreadable_sids
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "newer in sqlite"


def test_marked_sid_without_sidecar_falls_back_to_healthy_row(monkeypatch):
    """If the sidecar disappears while marked and the row reads healthy,
    the mark clears — there is nothing left to protect."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

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
    assert sid in store.unreadable_sids

    (d / f"{sid}.json").unlink()
    s = models.Session.load(sid)
    assert s is not None
    assert sid not in store.unreadable_sids


def test_full_save_does_not_heal_corrupt_sqlite_row(monkeypatch):
    """A full save() fails closed on a marked sid: the corrupt row is not
    force-overwritten, the unreadable mark is retained, and only the sidecar
    (the marked authority) is rewritten."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    sid = "sid-heal"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
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
    assert sid in store.unreadable_sids
    generation_at_mark = store.read_metadata_only(sid)["generation"]

    s.save()

    # Mark retained: no force-heal was performed and the row generation did
    # not move — the corrupt child rows are still exactly as seeded.
    assert sid in store.unreadable_sids
    assert store.read_metadata_only(sid)["generation"] == generation_at_mark
    conn = _sq.connect(str(d / "sessions.db"))
    raw = conn.execute(
        "SELECT message_json FROM messages WHERE session_id = ?", (sid,)
    ).fetchall()
    conn.close()
    assert raw and all(r[0] == "not-json" for r in raw)

    # The sidecar remains the authority and carries the in-memory messages.
    on_disk = json.loads((d / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(on_disk["messages"]) == 2


def test_marked_recovered_save_reconciles_metadata_only(monkeypatch):
    """Greptile P1: a marked sid whose row has recovered must not fail every
    save on the generation CAS. When the sidecar-loaded object's transcript
    matches the recovered row (metadata-only deltas), save() reconciles: the
    CAS generation is reseated from the probe, the row's own transcript
    children are kept (never rolled back), the metadata edit persists, and
    the mark clears."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-reconcile"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    # SQL row at generation 1 with transcript T; the sidecar carries the SAME
    # transcript T but different metadata.
    store.write_session(_sample_session_dict(sid))
    sidecar_payload = _sample_session_dict(sid)
    sidecar_payload["title"] = "sidecar title"
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

    # Transient failure with a sidecar: the fallback load succeeds, the sid is
    # marked, and the object carries no persisted lineage.
    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids
    assert s._persisted_generation is None
    assert s.title == "sidecar title"

    # Metadata-only edit; save()'s probe reads the recovered row and must
    # reconcile instead of raising StaleSessionWriteError.
    s.title = "edited while marked"
    s.save()

    # The mark cleared, the generation bumped off the probe's (1 -> 2), the
    # metadata edit persisted, and the transcript was never rolled back.
    assert sid not in store.unreadable_sids
    row = store.read_session(sid)
    assert row["title"] == "edited while marked"
    assert row["generation"] == 2
    assert [m["content"] for m in row["messages"]] == ["hello", "hi"]
    assert s._persisted_generation == 2

    # Rehydration: the in-memory owner adopted the authoritative row —
    # transcript children, scenes, and the post-bump generation — so a
    # second save() is a clean plain-CAS write, not another reconcile.
    assert s.messages == row["messages"]
    assert s.tool_calls == row["tool_calls"]
    assert s.context_messages == row["context_messages"]
    assert s.anchor_activity_scenes == row["anchor_activity_scenes"]
    s.save()
    row2 = store.read_session(sid)
    assert row2["generation"] == 3
    assert row2["title"] == "edited while marked"
    assert [m["content"] for m in row2["messages"]] == ["hello", "hi"]


def test_marked_reconcile_overlays_only_proven_dirty_metadata(monkeypatch):
    """Finding 1: transcript equality is not metadata authorization.

    A stale sidecar (predating SQL-side metadata edits) that loads during an
    outage must have only its PROVEN-dirty fields overlaid onto the recovered
    row — the row's newer pinned/workspace/token metadata must survive.
    On the parent tree the reconcile writes the entire flattened sidecar
    payload over the row, rolling pinned/workspace/input_tokens back.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-dirty-overlay"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    # SQL row at generation 1 with NEWER metadata (post-sidecar edits).
    row_payload = _sample_session_dict(sid)
    row_payload.update(
        {"pinned": True, "workspace": "/sql", "input_tokens": 1234}
    )
    store.write_session(row_payload)
    # Stale sidecar: same transcript, same (stale) token counter, but the
    # older pinned=False / workspace="/json" / title.
    sidecar_payload = _sample_session_dict(sid)
    sidecar_payload.update(
        {
            "pinned": False,
            "workspace": "/json",
            "title": "sidecar title",
            "input_tokens": 1000,
        }
    )
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

    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids

    s.title = "edited while marked"
    s.save()

    # Only the proven-dirty fields (title, updated_at) were overlaid: the
    # row's newer SQL-side metadata survived the reconcile.
    assert sid not in store.unreadable_sids
    row = store.read_session(sid)
    assert row["title"] == "edited while marked"
    assert row["pinned"] is True
    assert row["workspace"] == "/sql"
    assert row["input_tokens"] == 1234
    assert row["generation"] == 2
    assert s._persisted_generation == 2


def test_marked_reconcile_sparse_sidecar_does_not_launder_constructor_defaults(monkeypatch):
    """Task 010: absent legacy sidecar keys are not dirty.

    A sparse legacy sidecar omits pinned/input_tokens entirely; the fallback
    load materializes constructor defaults (pinned=False, input_tokens=0)
    into the in-memory owner. On the parent tree the baseline is the RAW
    sidecar dict, so every defaulted field diffs dirty and the reconcile
    launders the defaults over the recovered row's newer SQL-side values.
    With the post-construction baseline the defaults appear on BOTH sides
    and cancel: only the genuine edit (title, updated_at) overlays.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-sparse-baseline"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    # SQL row at generation 1 with NEWER metadata (post-sidecar SQL edits).
    row_payload = _sample_session_dict(sid)
    row_payload.update({"pinned": True, "workspace": "/sql", "input_tokens": 1234})
    store.write_session(row_payload)
    # Sparse legacy sidecar: SAME transcript, only title/workspace present —
    # pinned/input_tokens (and every other later-era key) OMITTED.
    sidecar_payload = _sample_session_dict(sid)
    sidecar_payload.update({"title": "sidecar title", "workspace": "/json"})
    assert "pinned" not in sidecar_payload and "input_tokens" not in sidecar_payload
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

    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids
    assert s._persisted_generation is None
    assert s.pinned is False and s.input_tokens == 0  # constructor defaults

    s.title = "edited"
    s.save()

    # Absent sidecar keys were NOT dirty: the row's newer SQL-side values
    # survived; only the proven edit (title) + updated_at overlaid.
    assert sid not in store.unreadable_sids
    row = store.read_session(sid)
    assert row["title"] == "edited"
    assert row["pinned"] is True          # parent: False (default laundered) → FAILS
    assert row["input_tokens"] == 1234    # parent: 0 (default laundered) → FAILS
    assert row["workspace"] == "/sql"
    assert row["generation"] == 2
    assert s._persisted_generation == 2
    # Rehydration adopted the authoritative row values.
    assert s.pinned is True and s.input_tokens == 1234


def test_marked_reconcile_fenced_against_concurrent_update_metadata(monkeypatch):
    """Finding 4: metadata-only writers move the version fence.

    A metadata write landing between the reconcile probe and the reconcile
    write must invalidate the reconcile: on the parent tree update_metadata
    leaves the generation untouched, so the probe-reseated CAS passes and
    the sidecar payload silently rolls the metadata write back.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-meta-fence"
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

    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids

    # Inject a concurrent metadata write between save()'s probe and the
    # reconcile: the probe reads the row, THEN the row's generation moves.
    injected = {"done": False}

    def inject_concurrent_write(sid_):
        row = real_read(sid_)
        if row is not None and not injected["done"]:
            injected["done"] = True
            store.update_metadata(sid_, {"pinned": True}, **_tok(store, sid_))
        return row

    monkeypatch.setattr(store, "read_session", inject_concurrent_write)

    s.title = "edited"
    with pytest.raises(sqlite_db.StaleSessionWriteError):
        s.save()

    # The concurrent metadata write survived; the stale sidecar metadata
    # was NOT laundered over it; the mark is retained for self-healing.
    row = store.read_session(sid)
    assert row["pinned"] is True
    assert row["title"] == "Test Session"
    assert sid in store.unreadable_sids


def test_marked_reconcile_fails_closed_across_incarnation_recreate(monkeypatch):
    """Finding 2: generation equality is not enough — the incarnation
    authority is the sole discriminator for delete + same-SID recreate.

    The recreated row starts again at generation 1 with an equal transcript,
    so on the parent tree the generation CAS passes and the stale marked
    owner's write lands on the NEW incarnation. The store-owned reconcile
    validates session_incarnations and refuses.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-incarnation"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    # Row at generation 1, incarnation 1, transcript T; sidecar same T.
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

    # Mark with baseline (generation 1, incarnation 1).
    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids

    # Delete + explicit same-SID recreate: live row is generation 1 again
    # (the generation check passes — 1 == 1) but incarnation 2.
    assert store.delete_session(sid) is True
    recreated = _sample_session_dict(sid)
    recreated["title"] = "recreated title"
    store.write_session(recreated, fresh_incarnation=True)

    # The stale marked owner's save must fail closed: the incarnation check
    # is the ONLY discriminator here.
    s.title = "stale owner edit"
    with pytest.raises(sqlite_db.StaleSessionWriteError):
        s.save()

    row = store.read_session(sid)
    assert row["title"] == "recreated title"
    assert sid in store.unreadable_sids

    # The next load demotes the mark and returns the recreated row.
    s2 = models.Session.load(sid)
    assert s2 is not None
    assert sid not in store.unreadable_sids
    assert s2.title == "recreated title"


def test_marked_reconcile_rehydrates_owner_children_for_second_save(monkeypatch):
    """Finding 3: the reconcile must rehydrate the in-memory owner.

    On the parent tree the child-swap patched only the write payload, so the
    owner's stale sidecar children (here: anchor_activity_scenes) were written
    back over the row by the SECOND save(). With store-owned reconcile +
    rehydration the owner adopts the row's children atomically.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-rehydrate"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    row_payload = _sample_session_dict(sid)
    row_payload.update(
        {
            "tool_calls": [{"name": "fs.read", "args": {"path": "/x"}}],
            "context_messages": [{"role": "system", "content": "ctx"}],
            "anchor_activity_scenes": {"scene-1": {"updated_at": 1.0, "body": "live"}},
        }
    )
    store.write_session(row_payload)
    # Sidecar: equal messages/tool_calls/context_messages, but STALE scenes.
    sidecar_payload = _sample_session_dict(sid)
    sidecar_payload.update(
        {
            "tool_calls": [{"name": "fs.read", "args": {"path": "/x"}}],
            "context_messages": [{"role": "system", "content": "ctx"}],
            "anchor_activity_scenes": {"scene-old": {"updated_at": 0.5, "body": "stale"}},
        }
    )
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

    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids
    assert set(s.anchor_activity_scenes) == {"scene-old"}  # sidecar authority while marked

    s.title = "edited"
    s.save()  # reconcile: store owns the children; the owner rehydrates

    # The owner adopted the row's children and the post-bump generation.
    assert sid not in store.unreadable_sids
    assert set(s.anchor_activity_scenes) == {"scene-1"}
    assert s.tool_calls == [{"name": "fs.read", "args": {"path": "/x"}}]
    assert s.context_messages == [{"role": "system", "content": "ctx"}]
    assert [m["content"] for m in s.messages] == ["hello", "hi"]
    assert s._persisted_generation == 2

    # The second save is a clean plain-CAS write and must NOT write the
    # sidecar's stale scenes back over the row.
    s.save()
    row = store.read_session(sid)
    assert set(row["anchor_activity_scenes"]) == {"scene-1"}
    assert row["generation"] == 3
    assert row["title"] == "edited"
    assert [m["content"] for m in row["messages"]] == ["hello", "hi"]
    assert row["tool_calls"] == [{"name": "fs.read", "args": {"path": "/x"}}]


def test_save_metadata_reseats_generation_for_subsequent_save(monkeypatch):
    """Fence blast-radius mitigation: the WebUI's single cached object per
    sid performs both the draft autosave and the later full save();
    save_metadata must reseat _persisted_generation from the bumped row so
    the full save still passes the CAS and the draft survives it."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-reseat"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))  # generation 1
    store.update_metadata(sid, {"title": "t2"}, **_tok(store, sid))  # generation 2
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    s = models.Session.load(sid)
    assert s is not None
    assert s._persisted_generation == 2

    s.save_metadata({"composer_draft": {"text": "d", "files": []}})
    assert store.read_metadata_only(sid)["generation"] == 3
    assert s._persisted_generation == 3

    # The subsequent full save must not raise StaleSessionWriteError, and
    # the draft written by the metadata path survives it.
    s.title = "t3"
    s.save()
    row = store.read_session(sid)
    assert row["title"] == "t3"
    assert row["composer_draft"]["text"] == "d"
    assert row["generation"] == 4


def test_marked_recovered_save_fails_closed_on_divergent_transcript(monkeypatch):
    """The reconcile path must never launder a divergent sidecar transcript
    over the recovered row: when the transcripts differ, save() still fails
    closed with StaleSessionWriteError, the row's transcript is untouched,
    and the mark is retained."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-divergent"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    # SQL row transcript T1 (2 messages); sidecar transcript T2 (1 different
    # message).
    store.write_session(_sample_session_dict(sid))
    sidecar_payload = _sample_session_dict(sid)
    sidecar_payload["messages"] = [
        {"role": "user", "content": "different sidecar message", "timestamp": 1000.0},
    ]
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

    # Load fails over to the sidecar (transcript T2) and marks the sid.
    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids

    # The probe recovers, the transcripts diverge: fail closed.
    with pytest.raises(sqlite_db.StaleSessionWriteError):
        s.save()

    # The SQL row transcript is still T1 and the mark is retained.
    row = store.read_session(sid)
    assert [m["content"] for m in row["messages"]] == ["hello", "hi"]
    assert sid in store.unreadable_sids


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

    store.update_metadata(
        "sid-draft", {"composer_draft": {"text": "draft", "files": []}}, **_tok(store, "sid-draft")
    )

    reloaded = store.read_session("sid-draft")
    assert reloaded["composer_draft"]["text"] == "draft"
    assert reloaded["updated_at"] == original_updated_at


def test_update_metadata_advances_updated_at_when_requested():
    d = _tmp_session_dir()
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-touch"))

    new_time = 9999.0
    store.update_metadata("sid-touch", {"updated_at": new_time}, **_tok(store, "sid-touch"))

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

    def _boom(*args, **kwargs):
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

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_update(*args, **kwargs)

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


def test_delete_route_fails_closed_when_sqlite_delete_fails(monkeypatch):
    """Gate finding: deletion must fail closed at the store boundary.

    When the SQLite delete raises, the route must NOT unlink the sidecar,
    prune the index, or report success — the transcript stays visible and
    the delete stays retryable.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    sid = "sid-failclosed"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid_: True)

    real_delete = store.delete_session

    def _boom(sid_):
        raise _sq.OperationalError("database is locked")

    monkeypatch.setattr(store, "delete_session", _boom)

    captured = _drive_delete_post(monkeypatch, {"session_id": sid})
    assert captured.get("status") == 500
    assert "error" in captured
    # Nothing was torn down: the row survives and the session stays listed.
    assert store.session_exists(sid) is True

    # Recovery: the delete succeeds once the store works again.
    monkeypatch.setattr(store, "delete_session", real_delete)
    captured = _drive_delete_post(monkeypatch, {"session_id": sid})
    assert captured.get("payload", {}).get("ok") is True
    assert store.session_exists(sid) is False


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


def test_preserve_pre_compression_snapshot_marks_sqlite_only_session(tmp_path, monkeypatch):
    """Gate finding: compression snapshot preservation must not return early
    just because the old session has no JSON sidecar."""
    import api.streaming as streaming

    monkeypatch.setattr(streaming, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")

    store = sqlite_db.WebUISqliteSessionDB(session_dir=tmp_path)
    old = _sample_session_dict("old_sqlite")
    old["messages"] = [{"role": "user", "content": f"m{i}"} for i in range(5)]
    store.write_session(old)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    # Continuation session holds fewer messages than the stored old session,
    # so the load-and-mark branch runs and must stamp the marker in the store.
    s = models.Session(session_id="new_cont")
    s.messages = [{"role": "user", "content": "x"}]
    s.parent_session_id = "original_parent"

    streaming._preserve_pre_compression_snapshot(s, "old_sqlite")

    meta = store.read_metadata_only("old_sqlite")
    assert meta is not None
    assert bool(meta["pre_compression_snapshot"]) is True
    # The fuller stored transcript is untouched.
    assert len(store.read_session("old_sqlite")["messages"]) == 5


def test_preserve_pre_compression_snapshot_rewrites_sqlite_snapshot_from_memory(
    tmp_path, monkeypatch
):
    """Rewrite branch: in-memory messages are newer than the stored row."""
    import api.streaming as streaming

    monkeypatch.setattr(streaming, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")

    store = sqlite_db.WebUISqliteSessionDB(session_dir=tmp_path)
    old = _sample_session_dict("old_sqlite2")
    old["messages"] = [{"role": "user", "content": "m0"}]
    store.write_session(old)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    s = models.Session(session_id="new_cont2")
    s.messages = [{"role": "user", "content": f"m{i}"} for i in range(5)]
    s.parent_session_id = "original_parent"

    streaming._preserve_pre_compression_snapshot(s, "old_sqlite2")

    full = store.read_session("old_sqlite2")
    assert full is not None
    assert len(full["messages"]) == 5
    assert full["pre_compression_snapshot"] is True
    # Continuation state restored.
    assert s.session_id == "new_cont2"
    assert s.pre_compression_snapshot is False


def _drive_clear_post(monkeypatch, body):
    """Run POST /api/session/clear through routes.handle_post."""
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
    assert routes.handle_post(handler, SimpleNamespace(path="/api/session/clear")) is True
    return captured


def test_clear_route_verifies_persisted_state_via_store(monkeypatch):
    """Clear verification reads the active store, not a missing sidecar."""
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    sid = "sid-clear"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict(sid))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    captured = _drive_clear_post(monkeypatch, {"session_id": sid})
    assert captured.get("payload", {}).get("ok") is True, captured

    persisted = store.read_session(sid)
    assert persisted is not None
    assert persisted["messages"] == []
    assert persisted.get("active_stream_id") is None
    assert persisted.get("pending_user_message") is None


def test_metadata_only_demote_carries_marked_draft(monkeypatch):
    """Greptile P1: the metadata-only recovery path must carry sidecar-saved
    drafts too, or demoting strands them."""
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-meta-carry"
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

    s = models.Session.load(sid)  # transient fail -> sidecar fallback, marked
    assert s is not None
    assert sid in store.unreadable_sids

    s.save_metadata({"composer_draft": {"text": "meta-carried", "files": []}})

    # Row recovered: the metadata-only read demotes and carries the draft.
    meta = models.Session.load_metadata_only(sid)
    assert meta is not None
    assert sid not in store.unreadable_sids
    assert store.read_metadata_only(sid)["composer_draft"]["text"] == "meta-carried"


def test_demote_does_not_clobber_concurrent_draft(monkeypatch):
    """Demote-vs-draft race: a draft saved while the demote is between its
    sidecar read and its mark-pop must not be lost. The per-sid demote lock
    serializes the demote's sidecar-read → carry → mark-pop against
    save_metadata's routing + write, so the newer draft routes into SQLite
    and survives the mark popping (on the unfixed tree the demote pops the
    mark after its stale read and the newer draft is stranded on the
    sidecar)."""
    import sqlite3 as _sq
    import threading

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-demote-race"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict(sid)
    payload["composer_draft"] = {"text": "D1", "files": []}
    store.write_session(payload)
    sidecar = d / f"{sid}.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_read = store.read_session
    calls = {"n": 0}

    def fail_once(sid_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_read(sid_)

    monkeypatch.setattr(store, "read_session", fail_once)

    # Transient failure with a sidecar: fallback load succeeds, sid marked
    # with the sidecar's D1 draft snapshot.
    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids

    # Instrument the demote's sidecar read: the FIRST read of this sidecar
    # (the demote's) reports entry and then blocks until released; a later
    # read (the concurrent save_metadata's JSON-fallback read on the unfixed
    # tree) reports that the draft writer committed to the sidecar route.
    entered = threading.Event()
    writer_committed = threading.Event()
    proceed = threading.Event()
    real_read_text = Path.read_text
    gate = {"armed": True}

    def gated_read_text(self_path, *args, **kwargs):
        content = real_read_text(self_path, *args, **kwargs)
        if self_path == sidecar:
            if gate["armed"]:
                gate["armed"] = False
                entered.set()
                proceed.wait(timeout=30)
            elif entered.is_set():
                writer_committed.set()
        return content

    monkeypatch.setattr(Path, "read_text", gated_read_text)

    # On the fixed tree, save_metadata takes the per-sid demote lock before
    # routing; signal when it reaches for the lock so the demote can be
    # released while the writer is serialized behind it. Absent on the
    # parent tree, where the sidecar-read signal above fires instead.
    real_lock_for_sid = getattr(models, "_draft_demote_lock_for_sid", None)
    if real_lock_for_sid is not None:
        def releasing_lock_for_sid(sid_):
            lock = real_lock_for_sid(sid_)
            writer_committed.set()
            return lock

        monkeypatch.setattr(
            models, "_draft_demote_lock_for_sid", releasing_lock_for_sid
        )

    errors = []

    def _demote():
        try:
            models.Session.load(sid)
        except Exception as exc:  # pragma: no cover - diagnostic only
            errors.append(exc)

    def _save_draft():
        try:
            s.save_metadata({"composer_draft": {"text": "D2", "files": []}})
        except Exception as exc:  # pragma: no cover - diagnostic only
            errors.append(exc)

    t1 = threading.Thread(target=_demote, daemon=True)
    t1.start()
    t2 = None
    try:
        assert entered.wait(timeout=30), "demote never reached the sidecar read"
        t2 = threading.Thread(target=_save_draft, daemon=True)
        t2.start()
        assert writer_committed.wait(timeout=30), (
            "concurrent draft save never engaged"
        )
    finally:
        proceed.set()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not t1.is_alive(), "demote thread did not finish"
    assert not t2.is_alive(), "draft writer thread did not finish"
    assert not errors

    # The mark is cleared and the NEWER draft wins: on the unfixed tree the
    # demote pops the mark after its stale D1 read, stranding D2 on the
    # sidecar (SQLite still holds D1); with the per-sid lock the writer is
    # serialized after the pop and routes D2 into SQLite.
    assert sid not in store.unreadable_sids
    final = store.read_metadata_only(sid)
    assert final["composer_draft"]["text"] == "D2"


def test_demote_carry_fenced_against_interleaved_writer(monkeypatch):
    """REGRESSION GUARD — passes on parent 933d25f by design.

    The fenced demote carry landed with the (generation, incarnation)
    writer-token work (task 009), so the parent tree already fences the
    carry and this test PASSES there: it is NOT a parent-failing
    discriminator. It closes the missing fenced-FAILURE leg of the
    marked-draft-carry schedule (the success path and the demote-vs-draft
    race are covered elsewhere; the suite's existing discriminators carry
    the parent-fail criterion for this re-gate).

    Scenario: the demote probe (read_session) returns healthy, then an
    interleaved writer lands a fenced update_metadata that advances the
    row generation BEFORE the demote's carry runs. The carry's conditional
    update_metadata (tokens captured by the now-stale probe) must raise
    StaleSessionWriteError; the demote swallows it, RETAINS the mark, and
    leaves the interleaved write intact. A subsequent load (probe healthy
    again, no interleaving) retries the carry and completes the demote —
    the sidecar draft lands and the interleaved writer's title survives.
    """
    import sqlite3 as _sq

    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")

    sid = "sid-demote-fenced-carry"
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict(sid)
    payload["composer_draft"] = {"text": "D0", "files": []}
    store.write_session(payload)
    (d / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_read = store.read_session
    calls = {"n": 0}

    def fail_once(sid_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("database is locked")
        return real_read(sid_)

    monkeypatch.setattr(store, "read_session", fail_once)

    # Transient failure with a sidecar: fallback load succeeds, sid marked
    # with the sidecar's D0 draft snapshot.
    s = models.Session.load(sid)
    assert s is not None
    assert sid in store.unreadable_sids

    # While marked, the draft autosave routes to the sidecar.
    s.save_metadata({"composer_draft": {"text": "typed during outage", "files": []}})

    # Arm the interleaved writer: the demote's probe is the next successful
    # read_session; right after it returns, a fenced writer advances the row
    # generation so the carry's probe-stale tokens must fail the CAS.
    armed = {"fire": True}

    def probe_then_interleave(sid_):
        data = real_read(sid_)
        if armed["fire"] and data is not None:
            armed["fire"] = False
            store.update_metadata(
                sid_, {"title": "interleaved writer"}, **_tok(store, sid_)
            )
        return data

    monkeypatch.setattr(store, "read_session", probe_then_interleave)

    # Demote attempt: the probe is healthy, but the carry is fenced out by
    # the interleaved writer. The demote swallows the StaleSessionWriteError,
    # retains the mark, and the load falls back to the sidecar.
    s2 = models.Session.load(sid)
    assert s2 is not None
    assert s2.composer_draft["text"] == "typed during outage"
    assert sid in store.unreadable_sids, "fenced-out carry must retain the mark"
    row = store.read_metadata_only(sid)
    assert row["title"] == "interleaved writer"
    assert row["composer_draft"]["text"] == "D0", "the fenced-out carry must not land"

    # Probe healthy again (no more interleaving): the retry completes the
    # demote, carries the sidecar draft, and keeps the interleaved title.
    monkeypatch.setattr(store, "read_session", real_read)
    s3 = models.Session.load(sid)
    assert s3 is not None
    assert sid not in store.unreadable_sids
    assert s3.composer_draft["text"] == "typed during outage"
    final = store.read_metadata_only(sid)
    assert final["composer_draft"]["text"] == "typed during outage"
    assert final["title"] == "interleaved writer"
    # The fence stayed consistent through the failed + retried carry: the
    # demoted session is constructed at the live generation.
    ver = store.read_row_version(sid)
    assert s3._persisted_generation == ver["generation"]


def test_ephemeral_cancel_cleanup_deletes_store_row(monkeypatch):
    """DB-only ephemeral cancel cleanup must delete the row (gate finding)."""
    import api.streaming as streaming

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-ephem"))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
    monkeypatch.setattr(streaming, "SESSION_DIR", d)

    class FakeEphemeralSession:
        session_id = "sid-ephem"
        path = d / "sid-ephem.json"
        active_stream_id = "live"
        pending_user_message = "x"
        pending_attachments = []
        pending_started_at = 1.0
        pending_user_source = None

    streaming._cleanup_ephemeral_cancelled_turn(FakeEphemeralSession())
    assert store.session_exists("sid-ephem") is False


def test_ephemeral_cancel_cleanup_retains_row_on_store_failure(monkeypatch):
    """Failed authoritative deletion retains the record for retry."""
    import sqlite3 as _sq

    import api.streaming as streaming

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-ephem2"))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
    monkeypatch.setattr(streaming, "SESSION_DIR", d)

    def _boom(sid_):
        raise _sq.OperationalError("database is locked")

    monkeypatch.setattr(store, "delete_session", _boom)

    class FakeEphemeralSession:
        session_id = "sid-ephem2"
        path = d / "sid-ephem2.json"
        active_stream_id = "live"
        pending_user_message = "x"
        pending_attachments = []
        pending_started_at = 1.0
        pending_user_source = None

    streaming._cleanup_ephemeral_cancelled_turn(FakeEphemeralSession())
    assert store.session_exists("sid-ephem2") is True


def test_recovered_workspace_binding_patches_store_row(monkeypatch):
    """DB-only workspace recovery must patch the row, not demand a sidecar."""
    d = _tmp_session_dir()
    monkeypatch.setattr(models, "SESSION_DIR", d)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", d / "_index.json")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", None)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-ws"))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    s = models.Session.load("sid-ws")
    assert s is not None
    models.persist_recovered_workspace_binding(
        s, "/recovered/ws", expected_workspace=s.workspace
    )
    assert store.read_metadata_only("sid-ws")["workspace"] == "/recovered/ws"

    # A mismatched expected_workspace fails closed, like the sidecar path.
    try:
        models.persist_recovered_workspace_binding(
            s, "/another/ws", expected_workspace="/different"
        )
    except models.WorkspaceBindingPersistenceError:
        pass
    else:
        raise AssertionError("workspace-changed mismatch must fail closed")


def test_sessions_cleanup_ghost_sweep_keeps_sqlite_rows(monkeypatch):
    """DB-only index rows are live sessions, not ghosts (gate finding)."""
    import api.routes as routes

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", d / "_index.json")
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-live-db")
    payload["title"] = "Real DB Session"
    store.write_session(payload)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    index_file = d / "_index.json"
    index_file.write_text(
        json.dumps(
            [
                {"session_id": "sid-live-db", "title": "Real DB Session"},
                {"session_id": "sid-ghost", "title": "Ghost"},
            ]
        ),
        encoding="utf-8",
    )

    captured = {}
    monkeypatch.setattr(
        routes, "j", lambda handler, payload, **kwargs: captured.update(payload) or True
    )
    handler = SimpleNamespace(_safe_webui_print=lambda *_a, **_k: None)
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)

    survivors = json.loads(index_file.read_text(encoding="utf-8"))
    survivor_ids = [e["session_id"] for e in survivors]
    assert "sid-live-db" in survivor_ids  # DB-only row is live
    assert "sid-ghost" not in survivor_ids  # real ghost removed
    assert store.session_exists("sid-live-db") is True


def test_sessions_cleanup_removes_db_only_zero_message_untitled(monkeypatch):
    """Phase 1b: DB-only zero-message Untitled sessions are cleaned."""
    import api.routes as routes

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-zero")
    payload["title"] = "Untitled"
    payload["messages"] = []
    store.write_session(payload)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    captured = {}
    monkeypatch.setattr(
        routes, "j", lambda handler, payload, **kwargs: captured.update(payload) or True
    )
    handler = SimpleNamespace(_safe_webui_print=lambda *_a, **_k: None)
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)

    assert store.session_exists("sid-zero") is False


def test_cleanup_delete_failure_is_truthful_and_durable(monkeypatch):
    """A failed store delete must report ok=False, retain the row, and
    record a durable cleanup lease owned by sessions_cleanup — never an
    unconditional ok:true after failure."""
    import sqlite3 as _sq

    import api.routes as routes

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-boom")
    payload["title"] = "Untitled"
    payload["messages"] = []
    store.write_session(payload)
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    real_delete = store.delete_session

    def _boom(sid_):
        raise _sq.OperationalError("database is locked")

    monkeypatch.setattr(store, "delete_session", _boom)

    captured = {}
    monkeypatch.setattr(
        routes, "j", lambda handler, payload, **kwargs: captured.update(payload) or True
    )
    handler = SimpleNamespace(_safe_webui_print=lambda *_a, **_k: None)
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)

    assert captured["ok"] is False
    assert "sid-boom" in captured["failed"]
    assert captured["cleaned"] == 0
    # Row retained — the retry owner is durable, not just logged.
    assert store.session_exists("sid-boom") is True
    leases = store.list_cleanup_leases()
    assert [l["session_id"] for l in leases] == ["sid-boom"]
    assert leases[0]["owner"] == "sessions_cleanup"
    assert leases[0]["attempts"] == 1

    # A later sweep with a healthy store clears the lease and the row.
    monkeypatch.setattr(store, "delete_session", real_delete)
    captured.clear()
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)
    assert captured["ok"] is True
    assert "sid-boom" in captured["failed"] or captured["failed"] == []
    assert store.session_exists("sid-boom") is False
    assert store.list_cleanup_leases() == []


def test_cleanup_delete_failure_keeps_sidecar_for_dual_rep(monkeypatch):
    """Dual-rep session: a failed SQL delete must NOT have unlinked the
    sidecar first — collection is side-effect free and the sidecar unlink
    only happens after an ok lifecycle_delete."""
    import sqlite3 as _sq

    import api.routes as routes

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-dual")
    payload["title"] = "Untitled"
    payload["messages"] = []
    store.write_session(payload)
    sidecar = d / "sid-dual.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    (d / "sid-dual.json.bak").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    def _boom(sid_):
        raise _sq.OperationalError("database is locked")

    monkeypatch.setattr(store, "delete_session", _boom)

    captured = {}
    monkeypatch.setattr(
        routes, "j", lambda handler, payload, **kwargs: captured.update(payload) or True
    )
    handler = SimpleNamespace(_safe_webui_print=lambda *_a, **_k: None)
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)

    assert captured["ok"] is False
    assert "sid-dual" in captured["failed"]
    assert store.session_exists("sid-dual") is True
    # Both representations survive the failed authoritative delete.
    assert sidecar.exists() is True
    assert (d / "sid-dual.json.bak").exists() is True
    assert store.list_cleanup_leases()[0]["owner"] == "sessions_cleanup"


def test_ephemeral_cancel_cleanup_records_durable_lease(monkeypatch):
    """Failed ephemeral cleanup records a cleanup lease owned by ephemeral."""
    import sqlite3 as _sq

    import api.streaming as streaming

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-ephem3"))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
    monkeypatch.setattr(streaming, "SESSION_DIR", d)

    def _boom(sid_):
        raise _sq.OperationalError("database is locked")

    monkeypatch.setattr(store, "delete_session", _boom)

    class FakeEphemeralSession:
        session_id = "sid-ephem3"
        path = d / "sid-ephem3.json"
        active_stream_id = "live"
        pending_user_message = "x"
        pending_attachments = []
        pending_started_at = 1.0
        pending_user_source = None

    streaming._cleanup_ephemeral_cancelled_turn(FakeEphemeralSession())
    assert store.session_exists("sid-ephem3") is True
    leases = store.list_cleanup_leases()
    assert [l["session_id"] for l in leases] == ["sid-ephem3"]
    assert leases[0]["owner"] == "ephemeral"


# ── Durable phased deletion (residual cleanup lifecycle) ────────────────────


def test_delete_route_500_with_durable_residuals_when_sidecar_unlink_fails(monkeypatch):
    """A sidecar unlink failure after the committed store delete must 500
    with the residual durably recorded — never ok:true with a silent leak.
    Re-POSTing once healthy drains the phase rows and returns 200."""
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-resid")
    store.write_session(payload)
    sidecar = d / "sid-resid.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    real_unlink = Path.unlink

    def _boom(self, *args, **kwargs):
        if self == sidecar:
            raise OSError("simulated unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _boom)

    captured = _drive_delete_post(monkeypatch, {"session_id": "sid-resid"})
    assert captured.get("status") == 500
    assert "cleanup incomplete" in captured.get("error", "")
    assert "sidecar" in captured.get("error", "")
    # The authoritative delete DID commit — only the residual remains.
    assert store.session_exists("sid-resid") is False
    rows = store.list_cleanup_phases("sid-resid")
    assert [r["phase"] for r in rows] == ["sidecar"]
    assert rows[0]["attempts"] == 1
    assert rows[0]["error"]
    assert sidecar.exists()

    # Recovery: the identical re-POST resumes at the remaining row only.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    captured = _drive_delete_post(monkeypatch, {"session_id": "sid-resid"})
    assert captured.get("payload", {}).get("ok") is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert store.list_cleanup_phases("sid-resid") == []
    assert not sidecar.exists()


def test_sessions_cleanup_does_not_count_residual_sid_and_janitor_completes(monkeypatch):
    """cleaned must not count a session whose residual phase failed: the sid
    lands in `failed` with a durable phase row, and a healthy re-run drains
    it via the janitor pre-pass."""
    import api.routes as routes

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", d / "_index.json")
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-unclean")
    payload["title"] = "Untitled"
    payload["messages"] = []
    store.write_session(payload)
    sidecar = d / "sid-unclean.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    real_unlink = Path.unlink

    def _boom(self, *args, **kwargs):
        if self == sidecar:
            raise OSError("simulated unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _boom)

    captured = {}
    monkeypatch.setattr(
        routes, "j", lambda handler, payload, **kwargs: captured.update(payload) or True
    )
    handler = SimpleNamespace(_safe_webui_print=lambda *_a, **_k: None)
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)

    assert captured["ok"] is False
    assert "sid-unclean" in captured["failed"]
    assert captured["cleaned"] == 0
    assert captured["residuals"].get("sid-unclean") == ["sidecar"]
    rows = store.list_cleanup_phases("sid-unclean")
    assert [r["phase"] for r in rows] == ["sidecar"]
    # The store delete committed; only the sidecar residual remains.
    assert store.session_exists("sid-unclean") is False
    assert sidecar.exists()

    # Healthy re-run: the janitor pre-pass drains the leftover row.
    monkeypatch.setattr(Path, "unlink", real_unlink)
    captured.clear()
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)
    assert captured["ok"] is True
    assert not sidecar.exists()
    assert store.list_cleanup_phases() == []


def test_sessions_cleanup_janitor_drains_orphaned_phase_rows(monkeypatch):
    """A crash between the delete txn and the executor leaves durable phase
    rows; the cleanup pre-pass janitor resumes and drains them without
    counting the sid or reporting it failed."""
    import api.routes as routes

    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", d / "_index.json")
    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    payload = _sample_session_dict("sid-orphan")
    store.write_session(payload)
    sidecar = d / "sid-orphan.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    # Simulate the crash: the delete txn committed the row delete + the
    # phase rows; the executor never ran.
    result = store.lifecycle_delete("sid-orphan", owner="x", pending_phases=["sidecar"])
    assert result["ok"] is True
    assert [r["phase"] for r in store.list_cleanup_phases("sid-orphan")] == ["sidecar"]
    assert sidecar.exists()

    captured = {}
    monkeypatch.setattr(
        routes, "j", lambda handler, payload, **kwargs: captured.update(payload) or True
    )
    handler = SimpleNamespace(_safe_webui_print=lambda *_a, **_k: None)
    routes._handle_sessions_cleanup(handler, {}, zero_only=False)

    assert captured["ok"] is True
    assert "sid-orphan" not in captured["failed"]
    assert not sidecar.exists()
    assert store.list_cleanup_phases() == []


def test_delete_route_state_db_phase_500s_until_cleaned(monkeypatch):
    """state_db cleanup failure is no longer an ok:true payload flag: it is
    a durable residual phase that 500s until a retry drains it."""
    d = _tmp_session_dir()
    _patch_route_state(monkeypatch, d)

    store = sqlite_db.WebUISqliteSessionDB(session_dir=d)
    store.write_session(_sample_session_dict("sid-statedb"))
    monkeypatch.setattr(models, "_sqlite_session_store_instance", store)

    # The state_db actuator short-circuits when no state.db exists, so point
    # it at a real file to exercise the delete_cli_session result path.
    state_db = d / "state.db"
    state_db.touch()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: False)

    captured = _drive_delete_post(monkeypatch, {"session_id": "sid-statedb"})
    assert captured.get("status") == 500
    assert "state_db" in captured.get("error", "")
    assert store.session_exists("sid-statedb") is False
    rows = store.list_cleanup_phases("sid-statedb")
    assert [r["phase"] for r in rows] == ["state_db"]

    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)
    captured = _drive_delete_post(monkeypatch, {"session_id": "sid-statedb"})
    assert captured.get("payload", {}).get("ok") is True
    assert captured["payload"]["state_db_cleanup_failed"] is False
    assert store.list_cleanup_phases("sid-statedb") == []


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

