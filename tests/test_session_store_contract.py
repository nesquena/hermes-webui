"""Backend-agnostic SessionStore contract tests.

Every backend that implements api.session_store.SessionStore must pass this
same suite — that is what proves the abstraction holds. Currently exercised:

- JSON sidecar adapter (api.webui_session_db.WebUIJsonSessionDB)
- SQLite store (api.webui_session_sqlite.WebUISqliteSessionDB)
- Postgres store (api.webui_session_postgres.WebUIPostgresSessionDB), when
  HERMES_TEST_PG_DSN is set and reachable; skipped otherwise.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from api.webui_session_db import WebUIJsonSessionDB
from api.webui_session_sqlite import (
    DeletedSessionWriteError,
    WebUISqliteSessionDB,
    StaleSessionWriteError,
)


def _tmp_dir():
    return Path(tempfile.mkdtemp())


def _tok(store, sid: str) -> dict:
    """The current durable (generation, incarnation) writer token for sid.

    SQL backends require both halves on versioned writes; the JSON backend
    has no durable version (last-writer-wins), so it gets no token kwargs.
    """
    if not getattr(store, "supports_generation", False):
        return {}
    ver = store.read_row_version(sid)
    assert ver is not None, f"no live row for {sid}"
    return {
        "expected_generation": ver["generation"],
        "expected_incarnation": ver["incarnation"],
    }


def _sample(sid: str) -> dict:
    return {
        "session_id": sid,
        "title": "Contract Session",
        "workspace": "/workspace",
        "model": "gpt-4",
        "created_at": 1000.0,
        "updated_at": 1001.0,
        "messages": [
            {"role": "user", "content": "hello", "timestamp": 1000.0},
            {"role": "assistant", "content": "hi", "timestamp": 1001.0},
        ],
        "tool_calls": [{"name": "fs.read", "args": {"path": "/x"}}],
        "context_messages": [{"role": "system", "content": "ctx"}],
        "anchor_activity_scenes": {},
    }


def _pg_store():
    dsn = os.environ.get("HERMES_TEST_PG_DSN")
    if not dsn:
        pytest.skip("HERMES_TEST_PG_DSN not set")
    # A CONFIGURED backend that fails to come up is a test failure, not a
    # skip: silently dropping the backend would hide contract violations.
    from api.webui_session_postgres import WebUIPostgresSessionDB

    store = WebUIPostgresSessionDB(dsn)
    conn = store._conn()
    with conn:
        conn.execute(
            "TRUNCATE sessions, messages, tool_calls, context_messages, anchor_scenes, session_incarnations"
        )
    return store


@pytest.fixture(params=["json", "sqlite", "postgres"])
def store(request):
    if request.param == "json":
        yield WebUIJsonSessionDB(session_dir=_tmp_dir())
    elif request.param == "sqlite":
        yield WebUISqliteSessionDB(session_dir=_tmp_dir())
    else:
        s = _pg_store()
        yield s
        s.close()


# SQL backends only (the JSON adapter predates these capabilities).
@pytest.fixture(params=["sqlite", "postgres"])
def sql_store(request):
    if request.param == "sqlite":
        yield WebUISqliteSessionDB(session_dir=_tmp_dir())
    else:
        s = _pg_store()
        yield s
        s.close()


def test_pg_legs_execute_when_dsn_set():
    """PG-legs non-skip guarantee: when HERMES_TEST_PG_DSN is configured,
    the Postgres backend must actually execute — a configured-but-broken
    DSN surfaces as a FAILURE (via _pg_store), never a silent skip that
    would let the [postgres] contract legs evaporate from a run. Direct
    write/read guard only; skips only when the DSN is genuinely unset."""
    dsn = os.environ.get("HERMES_TEST_PG_DSN")
    if not dsn:
        pytest.skip("HERMES_TEST_PG_DSN not set")
    store = _pg_store()
    try:
        sid = "pg-guard"
        store.write_session(_sample(sid))
        loaded = store.read_session(sid)
        assert loaded is not None and loaded["session_id"] == sid
        store.update_metadata(sid, {"title": "pg guard updated"}, **_tok(store, sid))
        assert store.read_session(sid)["title"] == "pg guard updated"
        store.delete_session(sid)
        assert store.read_session(sid) is None
    finally:
        store.close()


def test_write_read_round_trip(store):
    store.write_session(_sample("c1"))
    loaded = store.read_session("c1")
    assert loaded is not None
    assert loaded["session_id"] == "c1"
    assert loaded["title"] == "Contract Session"
    assert [m["role"] for m in loaded["messages"]] == ["user", "assistant"]
    assert loaded["tool_calls"][0]["name"] == "fs.read"
    assert loaded["context_messages"][0]["content"] == "ctx"


def test_session_exists(store):
    store.write_session(_sample("c2"))
    assert store.session_exists("c2") is True
    assert store.session_exists("nope") is False
    assert store.session_exists("../escape") is False


def test_update_metadata_preserves_transcript(store):
    store.write_session(_sample("c3"))
    store.update_metadata("c3", {"composer_draft": {"text": "draft", "files": []}}, **_tok(store, "c3"))
    loaded = store.read_session("c3")
    assert loaded["composer_draft"]["text"] == "draft"
    assert len(loaded["messages"]) == 2


def test_update_metadata_rejects_unsafe_fields(store):
    store.write_session(_sample("c4"))
    with pytest.raises(ValueError):
        store.update_metadata("c4", {"messages": []})


def test_delete_session_removes_everything(store):
    store.write_session(_sample("c5"))
    assert store.delete_session("c5") is True
    assert store.session_exists("c5") is False
    assert store.read_session("c5") is None
    assert store.delete_session("c5") is False


def test_list_sessions_ordering(store):
    a = _sample("c-a")
    a["updated_at"] = 100.0
    b = _sample("c-b")
    b["updated_at"] = 200.0
    store.write_session(a)
    store.write_session(b)
    rows = store.list_sessions()
    ids = [r["session_id"] for r in rows]
    assert ids[0] == "c-b"  # newer updated_at first


def test_unknown_top_level_keys_round_trip(store):
    payload = _sample("c6")
    payload["future_field"] = {"nested": [1, 2, 3]}
    store.write_session(payload)
    loaded = store.read_session("c6")
    assert loaded["future_field"] == {"nested": [1, 2, 3]}


def test_is_active(store):
    assert store.is_active() is True


def test_revision_bumps_on_writes_and_deletes(sql_store):
    r0 = sql_store.get_revision()
    sql_store.write_session(_sample("c7"))
    r1 = sql_store.get_revision()
    assert r1 > r0
    sql_store.update_metadata("c7", {"title": "renamed"}, **_tok(sql_store, "c7"))
    r2 = sql_store.get_revision()
    assert r2 > r1
    sql_store.delete_session("c7")
    assert sql_store.get_revision() > r2


def test_metadata_only_read_has_count_without_transcript(sql_store):
    sql_store.write_session(_sample("c8"))
    meta = sql_store.read_metadata_only("c8")
    assert meta is not None
    assert meta["message_count"] == 2
    assert "messages" not in meta


def test_generation_cas(sql_store):
    """Durable per-session generation CAS on every SQL backend."""
    sql_store.write_session(_sample("c9"))  # generation 1
    with pytest.raises(StaleSessionWriteError):
        sql_store.write_session(_sample("c9"))  # no lineage
    sql_store.write_session(_sample("c9"), expected_generation=1, expected_incarnation=1)  # bump -> 2
    assert sql_store.read_session("c9")["generation"] == 2
    with pytest.raises(StaleSessionWriteError):
        sql_store.write_session(_sample("c9"), expected_generation=1, expected_incarnation=1)  # stale
    sql_store.write_session(_sample("c9"), force=True)  # deliberate heal
    assert sql_store.read_session("c9")["generation"] == 3


def test_update_metadata_advances_generation(sql_store):
    """Metadata-only writers move the same version fence as full writes."""
    sql_store.write_session(_sample("g1"))  # generation 1
    assert sql_store.read_session("g1")["generation"] == 1
    meta = sql_store.update_metadata(
        "g1", {"composer_draft": {"text": "x", "files": []}}, **_tok(sql_store, "g1")
    )
    assert meta["generation"] == 2
    assert meta["incarnation"] == 1
    row = sql_store.read_session("g1")
    assert row["generation"] == 2
    # Row content otherwise preserved.
    assert row["title"] == "Contract Session"
    assert len(row["messages"]) == 2
    assert row["tool_calls"][0]["name"] == "fs.read"


def test_update_metadata_rejects_generation_field(sql_store):
    """A metadata writer must never forge the version fence."""
    sql_store.write_session(_sample("g2"))
    with pytest.raises(ValueError):
        sql_store.update_metadata("g2", {"generation": 9})
    assert sql_store.read_session("g2")["generation"] == 1


def test_read_row_version_sql(sql_store):
    """Durable (generation, incarnation) of the live row, or None."""
    assert sql_store.read_row_version("rv1") is None  # absent
    sql_store.write_session(_sample("rv1"))
    assert sql_store.read_row_version("rv1") == {"generation": 1, "incarnation": 1}
    sql_store.update_metadata("rv1", {"title": "renamed"}, **_tok(sql_store, "rv1"))
    assert sql_store.read_row_version("rv1") == {"generation": 2, "incarnation": 1}
    sql_store.delete_session("rv1")
    assert sql_store.read_row_version("rv1") is None  # retired
    sql_store.write_session(_sample("rv1"), fresh_incarnation=True)
    assert sql_store.read_row_version("rv1") == {"generation": 1, "incarnation": 2}
    # Path-unsafe ids are refused, not queried.
    assert sql_store.read_row_version("../escape") is None


def test_write_session_incarnation_token_fences_equal_generation_recreate(sql_store):
    """The (incarnation, generation) pair — not the generation alone — is
    the writer token. A delete + leased recreate restarts the row at
    generation 1, so a stale owner of the old incarnation presents an
    EQUAL generation; only the incarnation half refuses it."""
    sql_store.write_session(_sample("eq1"))  # gen 1, inc 1
    assert sql_store.read_row_version("eq1") == {"generation": 1, "incarnation": 1}
    assert sql_store.delete_session("eq1") is True
    sql_store.write_session(_sample("eq1"), fresh_incarnation=True)  # gen 1, inc 2
    # Parent-discriminating prelude: the equal-generation CAS alone must not
    # let the old-incarnation writer through.
    stale = None
    try:
        stale = sql_store.write_session(_sample("eq1"), expected_generation=1)
    except StaleSessionWriteError:
        pass
    assert stale is None  # parent fails HERE (the write succeeded)
    with pytest.raises(StaleSessionWriteError):
        sql_store.write_session(_sample("eq1"), expected_generation=1, expected_incarnation=1)
    out = sql_store.write_session(_sample("eq1"), expected_generation=1, expected_incarnation=2)
    assert out["generation"] == 2
    assert sql_store.read_row_version("eq1") == {"generation": 2, "incarnation": 2}


def test_update_metadata_requires_and_validates_version_token(sql_store):
    """update_metadata is a versioned writer: the (generation, incarnation)
    token is required and both halves are validated against the live row +
    non-retired authority."""
    sql_store.write_session(_sample("um1"))  # gen 1, inc 1

    # Unversioned metadata write: refused (fail closed, not lenient).
    with pytest.raises(StaleSessionWriteError):
        sql_store.update_metadata("um1", {"title": "unversioned"})
    assert sql_store.read_session("um1")["title"] == "Contract Session"

    # Correct token: applies, bumps generation, returns both token halves.
    out = sql_store.update_metadata(
        "um1", {"title": "versioned"}, expected_generation=1, expected_incarnation=1
    )
    assert out["title"] == "versioned"
    assert out["generation"] == 2
    assert out["incarnation"] == 1

    # Wrong generation: refused, row unchanged.
    with pytest.raises(StaleSessionWriteError):
        sql_store.update_metadata(
            "um1", {"title": "wrong-gen"}, expected_generation=1, expected_incarnation=1
        )
    # Right generation, wrong incarnation: refused.
    with pytest.raises(StaleSessionWriteError):
        sql_store.update_metadata(
            "um1", {"title": "wrong-inc"}, expected_generation=2, expected_incarnation=9
        )
    row = sql_store.read_session("um1")
    assert row["title"] == "versioned"
    assert row["generation"] == 2

    # Tokens against an absent row: DeletedSessionWriteError.
    with pytest.raises(DeletedSessionWriteError):
        sql_store.update_metadata(
            "um-missing", {"title": "x"}, expected_generation=1, expected_incarnation=1
        )
    # Tokens against a deleted (retired) row: DeletedSessionWriteError.
    sql_store.delete_session("um1")
    with pytest.raises(DeletedSessionWriteError):
        sql_store.update_metadata(
            "um1", {"title": "x"}, expected_generation=2, expected_incarnation=1
        )


def test_read_paths_expose_incarnation(sql_store):
    """read_session / read_metadata_only / update_metadata return rows carry
    the incarnation half of the writer token; it advances only on a leased
    recreate. The sidebar list path is deliberately not joined."""
    sql_store.write_session(_sample("ri1"))  # gen 1, inc 1
    full = sql_store.read_session("ri1")
    assert full["incarnation"] == 1
    meta = sql_store.read_metadata_only("ri1")
    assert meta["incarnation"] == 1
    out = sql_store.update_metadata(
        "ri1", {"title": "t2"}, expected_generation=1, expected_incarnation=1
    )
    assert out["incarnation"] == 1  # metadata writes bump generation only
    assert out["generation"] == 2
    # list_sessions does not JOIN the authority: list rows are unchanged.
    listed = sql_store.list_sessions()
    assert listed and all("incarnation" not in r for r in listed)
    # The incarnation advances only on a fresh_incarnation=True recreate.
    sql_store.delete_session("ri1")
    sql_store.write_session(_sample("ri1"), fresh_incarnation=True)
    assert sql_store.read_session("ri1")["incarnation"] == 2
    assert sql_store.read_metadata_only("ri1")["incarnation"] == 2
    assert sql_store.read_row_version("ri1") == {"generation": 1, "incarnation": 2}


def test_reconcile_marked_write_overlays_only_given_fields(sql_store):
    """The atomic reconcile applies ONLY the given fields, bumps the fence,
    and returns the authoritative full row for caller rehydration."""
    payload = _sample("rc1")
    payload["pinned"] = True
    sql_store.write_session(payload)  # generation 1, incarnation 1
    row = sql_store.reconcile_marked_write(
        "rc1",
        expected_generation=1,
        expected_incarnation=1,
        fields={"title": "overlaid", "updated_at": 2000.0},
    )
    assert row is not None
    assert row["title"] == "overlaid"
    assert row["pinned"] is True  # row's own value survived
    assert row["generation"] == 2
    # Full-row shape: transcript children included for rehydration.
    assert len(row["messages"]) == 2
    assert row["tool_calls"][0]["name"] == "fs.read"
    assert row["context_messages"][0]["content"] == "ctx"
    # Unknown keys ride extra_json, same policy as update_metadata.
    row2 = sql_store.reconcile_marked_write(
        "rc1",
        expected_generation=2,
        expected_incarnation=1,
        fields={"future_flag": {"on": True}},
    )
    assert row2["future_flag"] == {"on": True}
    assert row2["generation"] == 3


def test_reconcile_marked_write_fails_closed_on_version_mismatch(sql_store):
    """Any durable-version mismatch refuses with None and writes nothing."""
    sql_store.write_session(_sample("rc2"))  # generation 1, incarnation 1
    # Wrong generation.
    assert sql_store.reconcile_marked_write(
        "rc2", expected_generation=7, expected_incarnation=1,
        fields={"title": "x"},
    ) is None
    # Wrong incarnation.
    assert sql_store.reconcile_marked_write(
        "rc2", expected_generation=1, expected_incarnation=9,
        fields={"title": "x"},
    ) is None
    # Absent row.
    assert sql_store.reconcile_marked_write(
        "rc-missing", expected_generation=1, expected_incarnation=1,
        fields={"title": "x"},
    ) is None
    # Nothing was modified by the refusals.
    row = sql_store.read_session("rc2")
    assert row["title"] == "Contract Session"
    assert row["generation"] == 1
    # Retired row (deleted): fail closed even with the pre-delete version.
    sql_store.delete_session("rc2")
    assert sql_store.reconcile_marked_write(
        "rc2", expected_generation=1, expected_incarnation=1,
        fields={"title": "x"},
    ) is None
    # Path-unsafe ids are refused, not queried.
    assert sql_store.reconcile_marked_write(
        "../escape", expected_generation=1, expected_incarnation=1,
        fields={"title": "x"},
    ) is None


def test_reconcile_marked_write_rejects_unsafe_fields(sql_store):
    """Same unsafe-field guard as update_metadata, plus the fence itself."""
    sql_store.write_session(_sample("rc3"))
    for bad in ({"generation": 5}, {"messages": []}, {"session_id": "y"},
                {"tool_calls": []}, {"message_count": 0}):
        with pytest.raises(ValueError):
            sql_store.reconcile_marked_write(
                "rc3", expected_generation=1, expected_incarnation=1, fields=bad
            )
    assert sql_store.read_session("rc3")["generation"] == 1


def test_reconcile_contract_inert_without_generation_capability(store):
    """JSON backend: no durable version, no reconcile — both methods are
    inert None (the contract is SQL-only; production gates the reconcile on
    supports_generation)."""
    if store.supports_generation:
        pytest.skip("SQL backends are covered by the sql_store reconcile tests")
    store.write_session(_sample("jr1"))
    assert store.read_row_version("jr1") is None
    assert store.reconcile_marked_write(
        "jr1", expected_generation=1, expected_incarnation=1,
        fields={"title": "x"},
    ) is None
    # Inert means inert: the write did not land.
    assert store.read_session("jr1")["title"] == "Contract Session"


def test_null_key_presence(sql_store):
    """Explicitly-None fields read back as present None (JSON parity)."""
    payload = _sample("c10")
    payload["personality"] = None
    payload["project_id"] = None
    sql_store.write_session(payload)
    loaded = sql_store.read_session("c10")
    assert "personality" in loaded and loaded["personality"] is None
    assert "project_id" in loaded and loaded["project_id"] is None
    sql_store.update_metadata(
        "c10", {"personality": "set", "threshold_tokens": None}, **_tok(sql_store, "c10")
    )
    reloaded = sql_store.read_session("c10")
    assert reloaded["personality"] == "set"
    assert "threshold_tokens" in reloaded and reloaded["threshold_tokens"] is None


def test_session_model_runs_on_postgres_backend(monkeypatch, tmp_path):
    """Runtime substitution at the model layer: Session.save/load against
    the Postgres backend with generation CAS, index, and reloads."""
    import os as _os

    dsn = _os.environ.get("HERMES_TEST_PG_DSN")
    if not dsn:
        pytest.skip("HERMES_TEST_PG_DSN not set")

    import api.models as models
    from api.webui_session_postgres import WebUIPostgresSessionDB

    store = WebUIPostgresSessionDB(dsn)
    conn = store._conn()
    with conn:
        conn.execute(
            "TRUNCATE sessions, messages, tool_calls, context_messages, anchor_scenes, session_incarnations"
        )
    monkeypatch.setattr(models, "_pg_session_store_instance", store)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", {})

    s = models.Session(
        session_id="pg-model", title="PG", messages=[{"role": "user", "content": "hi"}]
    )
    s.save()
    assert s._persisted_generation == 1

    loaded = models.Session.load("pg-model")
    assert loaded is not None
    assert loaded.title == "PG"
    assert loaded._persisted_generation == 1

    loaded.title = "PG2"
    loaded.save()
    assert store.read_session("pg-model")["generation"] == 2

    stale = models.Session(
        session_id="pg-model",
        title="stale",
        messages=[{"role": "user", "content": "hi"}],
    )
    stale._persisted_generation = 1
    stale._persisted_incarnation = 1
    try:
        stale.save()
    except StaleSessionWriteError:
        pass
    else:
        raise AssertionError("stale model-layer writer must be rejected on PG")

    assert store.read_session("pg-model")["title"] == "PG2"


def test_session_equal_generation_recreate_fenced_on_postgres(monkeypatch, tmp_path):
    """The T1 schedule through models.Session on a real Postgres store: an
    owner loaded at (generation 1, incarnation 1) survives a delete +
    leased recreate (the new row is ALSO generation 1); its ordinary save()
    must be fenced by the incarnation half of the writer token."""
    import os as _os

    dsn = _os.environ.get("HERMES_TEST_PG_DSN")
    if not dsn:
        pytest.skip("HERMES_TEST_PG_DSN not set")

    import api.models as models
    from api.webui_session_postgres import WebUIPostgresSessionDB

    store = WebUIPostgresSessionDB(dsn)
    conn = store._conn()
    with conn:
        conn.execute(
            "TRUNCATE sessions, messages, tool_calls, context_messages, anchor_scenes, session_incarnations"
        )
    monkeypatch.setattr(models, "_pg_session_store_instance", store)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", {})

    store.write_session(_sample("pg-eq"))  # gen 1, inc 1
    owner = models.Session.load("pg-eq")
    assert owner is not None
    assert owner._persisted_generation == 1
    assert owner._persisted_incarnation == 1

    assert store.delete_session("pg-eq") is True
    recreated = _sample("pg-eq")
    recreated["title"] = "recreated incarnation"
    store.write_session(recreated, fresh_incarnation=True)  # gen 1, inc 2

    # The stale owner's ordinary save() must be refused: the generation
    # alone (1 == 1) cannot discriminate it from the new incarnation.
    owner.title = "stale equal-generation write"
    with pytest.raises(StaleSessionWriteError):
        owner.save()

    row = store.read_session("pg-eq")
    assert row["title"] == "recreated incarnation"
    assert store.read_row_version("pg-eq") == {"generation": 1, "incarnation": 2}
    fresh = models.Session.load("pg-eq")
    assert fresh is not None
    assert fresh.title == "recreated incarnation"
    assert fresh._persisted_generation == 1
    assert fresh._persisted_incarnation == 2
    store.close()


def test_cutover_marker_blocks_incomplete_migration(sql_store):
    sql_store.set_meta("created_by", "migration")
    sql_store.set_meta("migration_complete", "0")
    assert sql_store.is_active() is False
    sql_store.set_meta("migration_complete", "1")
    assert sql_store.is_active() is True


def test_update_metadata_preserves_unknown_keys(store):
    """One metadata policy: unknown top-level keys persist on every backend
    (JSON sidecar dict, SQL extra_json); only _UNSAFE_FIELDS are rejected."""
    store.write_session(_sample("c11"))
    store.update_metadata("c11", {"future_field": {"nested": [1, 2]}}, **_tok(store, "c11"))
    loaded = store.read_session("c11")
    assert loaded["future_field"] == {"nested": [1, 2]}
    assert len(loaded["messages"]) == 2
    with pytest.raises(ValueError):
        store.update_metadata("c11", {"messages": []})


def test_is_active_fails_closed_on_marker_read_error(sql_store):
    """A store whose cutover markers cannot be read has no authority."""
    original_conn = sql_store._conn

    def _boom():
        raise RuntimeError("connection lost")

    sql_store._conn = _boom
    try:
        assert sql_store.is_active() is False
    finally:
        sql_store._conn = original_conn


def test_configured_postgres_lifecycle_paths(monkeypatch, tmp_path):
    """Configured Postgres drives the production lifecycle paths end to end:
    store selection (fail-closed), Session.save/load, workspace-binding
    recovery on a DB-only SID, index existence/rebuild, compression-parent
    lookup, and lifecycle_delete. Skips only when no DSN is configured; a
    configured-but-unreachable DSN is a failure, not a skip."""
    dsn = os.environ.get("HERMES_TEST_PG_DSN")
    if not dsn:
        pytest.skip("HERMES_TEST_PG_DSN not set")

    import api.models as models

    monkeypatch.setenv("HERMES_SESSION_STORE_PG_DSN", dsn)
    monkeypatch.setattr(models, "_pg_session_store_instance", None)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", {})

    store = models.get_session_store()
    assert store.persists_without_sidecar is True

    conn = store._conn()
    with conn:
        conn.execute(
            "TRUNCATE sessions, messages, tool_calls, context_messages, anchor_scenes, session_incarnations"
        )

    # Session.save/load through the model layer on the configured PG store.
    s = models.Session(
        session_id="pg-life",
        title="PG lifecycle",
        workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}],
    )
    s.save()
    loaded = models.Session.load("pg-life")
    assert loaded is not None and loaded.title == "PG lifecycle"

    # Workspace-binding recovery on a DB-only SID (no sidecar exists).
    current_ws = str(loaded.workspace)
    new_ws = str((tmp_path / "recovered").resolve())
    models.persist_recovered_workspace_binding(
        loaded, new_ws, expected_workspace=current_ws
    )
    assert store.read_metadata_only("pg-life")["workspace"] == new_ws

    # Index existence + full rebuild see the DB-only session.
    assert models._index_entry_exists("pg-life", in_memory_ids=set()) is True
    with models.LOCK:
        models.SESSIONS.clear()
    models._write_session_index(updates=None)
    index_entries = json.loads(models.SESSION_INDEX_FILE.read_text())
    assert any(e.get("session_id") == "pg-life" for e in index_entries)

    # Compression-parent lookup via the store's lineage rows.
    child = models.Session(
        session_id="pg-child",
        title="child",
        parent_session_id="pg-life",
        messages=[{"role": "user", "content": "c"}],
    )
    child.save()
    # Isolate the store-scan path: drop in-memory and index evidence.
    with models.LOCK:
        models.SESSIONS.clear()
    models.SESSION_INDEX_FILE.unlink(missing_ok=True)
    parent_view = models.Session.load("pg-life")
    assert models._has_compression_continuation(parent_view) is True

    # lifecycle_delete with durable retry ownership.
    result = store.lifecycle_delete("pg-child", owner="test-lifecycle")
    assert result["ok"] is True and result["existed"] is True
    assert store.session_exists("pg-child") is False

    # is_active fails closed when the markers cannot be read.
    original_conn = store._conn

    def _boom():
        raise RuntimeError("connection lost")

    store._conn = _boom
    try:
        assert store.is_active() is False
    finally:
        store._conn = original_conn

    # The selector never returns an inactive PG store: a configured DSN with
    # incomplete migration markers refuses instead of falling back.
    store.set_meta("created_by", "migration")
    store.set_meta("migration_complete", "0")
    monkeypatch.setattr(models, "_pg_session_store_instance", None)
    try:
        with pytest.raises(RuntimeError):
            models.get_session_store()
    finally:
        # Restore a clean app database for other tests sharing this DSN.
        store.set_meta("created_by", "app")
        conn = store._conn()
        with conn:
            conn.execute("DELETE FROM meta WHERE key = 'migration_complete'")
        store.close()


# ── Durable residual-cleanup phases (cleanup_phases ledger) ──────────────────


def test_lifecycle_delete_records_pending_phases_atomically(sql_store):
    """The residual phase rows commit in the SAME transaction as the
    authoritative delete: when lifecycle_delete returns ok, the ledger is
    already observable (no interleaving window), and per-phase outcomes
    clear or annotate rows. Leases and phases never coexist."""
    sql_store.write_session(_sample("ph1"))
    result = sql_store.lifecycle_delete(
        "ph1", owner="t", pending_phases=["sidecar", "state_db"]
    )
    assert result["ok"] is True and result["existed"] is True
    assert sql_store.session_exists("ph1") is False
    rows = sql_store.list_cleanup_phases("ph1")
    assert [r["phase"] for r in rows] == ["sidecar", "state_db"]
    assert all(r["owner"] == "t" for r in rows)
    assert all(r["attempts"] == 0 for r in rows)  # recorded, not yet attempted
    assert sql_store.list_cleanup_leases() == []

    # ok=True clears one row.
    sql_store.finish_cleanup_phase("ph1", "sidecar", ok=True)
    assert [r["phase"] for r in sql_store.list_cleanup_phases("ph1")] == ["state_db"]

    # ok=False records the error and bumps attempts.
    sql_store.finish_cleanup_phase("ph1", "state_db", ok=False, error="boom")
    (row,) = sql_store.list_cleanup_phases("ph1")
    assert row["attempts"] == 1
    assert row["error"] == "boom"

    # Re-recording (a retried delete) preserves attempts, refreshes owner.
    result = sql_store.lifecycle_delete("ph1", owner="t2", pending_phases=["state_db"])
    assert result["ok"] is True and result["existed"] is False
    (row,) = sql_store.list_cleanup_phases("ph1")
    assert row["attempts"] == 1
    assert row["owner"] == "t2"

    # The unfiltered janitor sweep sees the row; final clear empties it.
    assert [r["session_id"] for r in sql_store.list_cleanup_phases()] == ["ph1"]
    sql_store.finish_cleanup_phase("ph1", "state_db", ok=True)
    assert sql_store.list_cleanup_phases() == []


def test_lifecycle_delete_pending_phases_json_backend_inert():
    """The JSON backend accepts pending_phases and ignores them: no durable
    ledger exists in a last-writer-wins file world (the same capability
    boundary as cleanup leases)."""
    store = WebUIJsonSessionDB(session_dir=_tmp_dir())
    store.write_session(_sample("j1"))
    result = store.lifecycle_delete("j1", owner="t", pending_phases=["sidecar"])
    assert result["ok"] is True and result["existed"] is True
    store.finish_cleanup_phase("j1", "sidecar", ok=False, error="boom")  # no-op
    assert store.list_cleanup_phases() == []
    assert store.list_cleanup_phases("j1") == []
