import json
import sqlite3
from contextlib import contextmanager

from api.session_recovery import recover_missing_sidecars_from_state_db, audit_session_recovery


def _make_state_db(path, *, sid="state_only_001", source="webui", messages=2):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER, parent_session_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count, parent_session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, source, "Recovered from DB", "openai/gpt-5", 1234.0, messages, "parent-1"),
    )
    for i in range(messages):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, "user" if i % 2 == 0 else "assistant", f"message {i + 1}", 1234.0 + i),
        )
    conn.commit()
    conn.close()
    return sid


def _write_index(path, entries):
    (path / "_index.json").write_text(json.dumps(entries), encoding="utf-8")


def test_recover_missing_sidecars_from_state_db_materializes_webui_row(tmp_path):
    sid = _make_state_db(tmp_path / "state.db")

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")

    assert result["materialized"] == 1
    sidecar = tmp_path / f"{sid}.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert data["title"] == "Recovered from DB"
    assert data["model"] == "openai/gpt-5"
    assert data["parent_session_id"] == "parent-1"
    assert data["source_tag"] == "webui"
    assert data["session_source"] == "webui"
    assert [m["content"] for m in data["messages"]] == ["message 1", "message 2"]


def test_recover_missing_sidecar_rereads_state_db_under_sid_authority(
    tmp_path,
    monkeypatch,
):
    import api.models as models

    state_db = tmp_path / "state.db"
    sid = _make_state_db(state_db, sid="state_changes_before_lock", messages=1)
    real_authority = models._session_sidecar_authority
    updated = False

    @contextmanager
    def update_before_recovery_enters_authority(session_id, *, session_dir=None):
        nonlocal updated
        with real_authority(session_id, session_dir=session_dir):
            if not updated:
                with sqlite3.connect(state_db) as conn:
                    conn.execute(
                        "INSERT INTO messages "
                        "(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                        (sid, "assistant", "committed while recovery waited", 1235.0),
                    )
                    conn.execute(
                        "UPDATE sessions SET message_count = 2 WHERE id = ?",
                        (sid,),
                    )
                updated = True
            yield

    monkeypatch.setattr(
        models,
        "_session_sidecar_authority",
        update_before_recovery_enters_authority,
    )

    result = recover_missing_sidecars_from_state_db(tmp_path, state_db)

    assert result["materialized"] == 1
    data = json.loads((tmp_path / f"{sid}.json").read_text(encoding="utf-8"))
    assert [message["content"] for message in data["messages"]] == [
        "message 1",
        "committed while recovery waited",
    ]


def test_recover_reread_uses_one_sqlite_snapshot_for_metadata_and_messages(
    tmp_path,
    monkeypatch,
):
    from api import session_recovery

    state_db = tmp_path / "state.db"
    sid = _make_state_db(state_db, sid="coherent_snapshot", messages=1)
    real_connect = sqlite3.connect
    with real_connect(state_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

    scans = 0

    class _CursorProxy:
        def __init__(self, cursor, mutate_after_fetch=False):
            self._cursor = cursor
            self._mutate_after_fetch = mutate_after_fetch

        def fetchall(self):
            rows = self._cursor.fetchall()
            if self._mutate_after_fetch:
                with real_connect(state_db) as writer:
                    writer.execute(
                        "UPDATE sessions SET title = ?, message_count = 2 WHERE id = ?",
                        ("Committed replacement", sid),
                    )
                    writer.execute(
                        "INSERT INTO messages "
                        "(session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                        (sid, "assistant", "committed replacement", 1235.0),
                    )
            return rows

    class _ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._connection.row_factory = value

        def execute(self, sql, parameters=()):
            nonlocal scans
            cursor = self._connection.execute(sql, parameters)
            normalized = " ".join(sql.split()).lower()
            mutate = False
            if " from sessions " in f" {normalized} " and "source = 'webui'" in normalized:
                scans += 1
                mutate = scans == 2
            return _CursorProxy(cursor, mutate_after_fetch=mutate)

        def close(self):
            self._connection.close()

    def proxied_connect(*args, **kwargs):
        return _ConnectionProxy(real_connect(*args, **kwargs))

    monkeypatch.setattr(session_recovery.sqlite3, "connect", proxied_connect)

    result = recover_missing_sidecars_from_state_db(tmp_path, state_db)

    assert result["materialized"] == 1
    data = json.loads((tmp_path / f"{sid}.json").read_text(encoding="utf-8"))
    observed = (
        data["title"],
        data["message_count"],
        tuple(message["content"] for message in data["messages"]),
    )
    assert observed in {
        ("Recovered from DB", 1, ("message 1",)),
        ("Committed replacement", 2, ("message 1", "committed replacement")),
    }


def test_recovered_sidecar_fsyncs_temp_before_create_only_publication(
    tmp_path,
    monkeypatch,
):
    from api import models, session_recovery

    state_db = tmp_path / "state.db"
    sid = _make_state_db(state_db, sid="durable_materialization", messages=1)
    events = []
    monkeypatch.setattr(
        session_recovery.os,
        "fsync",
        lambda _fd: events.append("file"),
    )
    monkeypatch.setattr(
        models,
        "_fsync_sidecar_directory",
        lambda _directory: events.append("directory"),
    )

    result = recover_missing_sidecars_from_state_db(tmp_path, state_db)

    assert result["materialized"] == 1
    assert events == ["file", "directory"]
    assert (tmp_path / f"{sid}.json").exists()


def test_hidden_background_cleanup_cannot_be_recreated_from_state_db(
    tmp_path,
    monkeypatch,
):
    from api import models, routes

    state_db = tmp_path / "state.db"
    sid = _make_state_db(state_db, sid="hidden_background_lifecycle", messages=1)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "delete_cli_session", lambda _sid: False)
    session = models.Session(
        session_id=sid,
        title="bg: hidden result",
        messages=[{"role": "assistant", "content": "hidden result"}],
    )
    session.save(skip_index=True)

    routes._delete_hidden_background_session_sidecar(sid)
    result = recover_missing_sidecars_from_state_db(tmp_path, state_db)

    assert result["materialized"] == 0
    assert not (tmp_path / f"{sid}.json").exists()
    assert sid in models._load_webui_deleted_session_tombstone()


def test_delete_at_tombstone_cap_retains_current_sid_and_blocks_state_db_recovery(
    tmp_path,
    monkeypatch,
):
    from api import models

    state_db = tmp_path / "state.db"
    sid = _make_state_db(
        state_db,
        sid="a-target-deleted-session",
        messages=1,
    )
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", tmp_path / "_index.json")
    models._save_webui_deleted_session_tombstone(
        {f"z-{index:04d}" for index in range(models.WEBUI_DELETED_SESSION_TOMBSTONE_CAP)}
    )
    session = models.Session(
        session_id=sid,
        messages=[{"role": "user", "content": "deleted but retained in state.db"}],
    )
    session.save(skip_index=True)

    with models._session_sidecar_authority(sid):
        deleted = models._delete_session_sidecar_artifacts_locked(sid)

    retained = models._load_webui_deleted_session_tombstone()
    expected_retained = {
        sid,
        *(f"z-{index:04d}" for index in range(1, models.WEBUI_DELETED_SESSION_TOMBSTONE_CAP)),
    }
    assert deleted is True
    assert retained == frozenset(expected_retained)
    assert not (tmp_path / f"{sid}.json").exists()

    result = recover_missing_sidecars_from_state_db(tmp_path, state_db)

    assert result["materialized"] == 0
    assert not (tmp_path / f"{sid}.json").exists()


def test_recover_missing_sidecars_from_state_db_skips_deleted_webui_tombstone(tmp_path, monkeypatch):
    import api.models as _m
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    sid = _make_state_db(tmp_path / "state.db", sid="deleted_webui_001")
    _write_index(tmp_path, [
        {
            "session_id": sid,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
        }
    ])
    # A genuine delete records the DURABLE tombstone; only that suppresses repair.
    _m._record_webui_deleted_session_tombstone(sid)
    try:
        result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")
        assert result["materialized"] == 0
        assert not (tmp_path / f"{sid}.json").exists()
    finally:
        _m._clear_webui_deleted_session_tombstone(sid)


def test_recover_missing_sidecars_index_only_no_tombstone_is_repairable(tmp_path, monkeypatch):
    """A crash that loses the sidecar (index intact, NO durable tombstone) must
    still be recovered from state.db — the index heuristic alone must not
    suppress repair (#5504 Codex/Opus finding; matches origin/master behavior)."""
    import api.models as _m
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    sid = _make_state_db(tmp_path / "state.db", sid="crashed_webui_001")
    _write_index(tmp_path, [
        {
            "session_id": sid,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
        }
    ])
    # No durable tombstone recorded (this is a crash, not a delete).
    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")
    assert result["materialized"] == 1
    assert (tmp_path / f"{sid}.json").exists()


def test_audit_reports_deleted_webui_tombstone_is_unsafe(tmp_path, monkeypatch):
    import api.models as _m
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    sid = _make_state_db(tmp_path / "state.db", sid="deleted_webui_001")
    _write_index(tmp_path, [
        {
            "session_id": sid,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
        }
    ])
    _m._record_webui_deleted_session_tombstone(sid)
    try:
        report = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")

        assert any(
            item["session_id"] == sid
            and item["kind"] == "state_db_deleted_webui_tombstone"
            and item["category"] == "unsafe_to_repair"
            and item["recommendation"] == "deleted_session_skipped"
            for item in report["items"]
        )
        assert not any(
            item["session_id"] == sid and item["kind"] == "state_db_missing_sidecar"
            for item in report["items"]
        )
        assert not any(
            item["session_id"] == sid and item["category"] == "repairable"
            for item in report["items"]
        )
    finally:
        _m._clear_webui_deleted_session_tombstone(sid)


def test_audit_no_double_count_when_bak_and_state_db_row_both_survive(tmp_path, monkeypatch):
    """A deleted session with BOTH a surviving .json.bak AND a state.db row must
    yield EXACTLY ONE deleted-webui-tombstone audit item, not two (#5504 SILENT
    finding — the orphan-.bak branch and the state.db missing-sidecar loop both
    used to emit it)."""
    import api.models as _m
    import json as _json
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    sid = _make_state_db(tmp_path / "state.db", sid="deleted_both_001")
    # Plant a surviving orphan .bak (no live sidecar) for the same sid.
    (tmp_path / f"{sid}.json.bak").write_text(
        _json.dumps({"session_id": sid, "messages": [{"role": "user", "content": "x"}]}),
        encoding="utf-8",
    )
    _m._record_webui_deleted_session_tombstone(sid)
    try:
        report = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")
        tombstone_items = [
            item for item in report["items"]
            if item["session_id"] == sid
            and item["kind"] == "state_db_deleted_webui_tombstone"
        ]
        assert len(tombstone_items) == 1, (
            f"expected exactly one tombstone audit item, got {len(tombstone_items)}"
        )
    finally:
        _m._clear_webui_deleted_session_tombstone(sid)


def test_recover_missing_sidecars_skips_durable_delete_tombstone_without_index(tmp_path, monkeypatch):
    import api.models as _m

    sid = _make_state_db(tmp_path / "state.db", sid="durable_deleted_001")
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    _m._record_webui_deleted_session_tombstone(sid)

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")
    report = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")

    assert result["materialized"] == 0
    assert not (tmp_path / f"{sid}.json").exists()
    assert any(
        item["session_id"] == sid
        and item["kind"] == "state_db_deleted_webui_tombstone"
        and item["category"] == "unsafe_to_repair"
        and item["recommendation"] == "deleted_session_skipped"
        for item in report["items"]
    )
    assert not any(
        item["session_id"] == sid and item["category"] == "repairable"
        for item in report["items"]
    )


def test_audit_skips_index_missing_file_when_durable_delete_tombstone_survives(tmp_path, monkeypatch):
    import api.models as _m

    sid = "durable_deleted_index_001"
    _write_index(tmp_path, [
        {
            "session_id": sid,
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
        }
    ])
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    _m._record_webui_deleted_session_tombstone(sid)

    report = audit_session_recovery(tmp_path)

    assert not any(
        item["session_id"] == sid and item["kind"] == "index_missing_file"
        for item in report["items"]
    )
    assert not any(
        item["session_id"] == sid and item["category"] == "repairable"
        for item in report["items"]
    )


def test_audit_skips_orphan_backup_when_durable_delete_tombstone_survives(tmp_path, monkeypatch):
    import api.models as _m

    sid = _make_state_db(tmp_path / "state.db", sid="durable_deleted_backup_001")
    (tmp_path / f"{sid}.json.bak").write_text(
        json.dumps(
            {
                "session_id": sid,
                "messages": [{"role": "user", "content": "deleted"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)
    _m._record_webui_deleted_session_tombstone(sid)

    report = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")

    assert any(
        item["session_id"] == sid
        and item["kind"] == "state_db_deleted_webui_tombstone"
        and item["category"] == "unsafe_to_repair"
        and item["recommendation"] == "deleted_session_skipped"
        for item in report["items"]
    )
    assert not any(
        item["session_id"] == sid and item["kind"] == "orphan_backup"
        for item in report["items"]
    )
    assert not any(
        item["session_id"] == sid and item["category"] == "repairable"
        for item in report["items"]
    )


def test_recover_missing_sidecars_from_state_db_skips_existing_sidecar(tmp_path):
    sid = _make_state_db(tmp_path / "state.db")
    existing = tmp_path / f"{sid}.json"
    existing.write_text(json.dumps({"session_id": sid, "messages": [{"role": "user", "content": "keep"}]}), encoding="utf-8")

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")

    assert result["materialized"] == 0
    assert json.loads(existing.read_text(encoding="utf-8"))["messages"][0]["content"] == "keep"


def test_audit_reports_state_db_row_missing_sidecar(tmp_path):
    sid = _make_state_db(tmp_path / "state.db")

    report = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")

    assert any(
        item["session_id"] == sid
        and item["kind"] == "state_db_missing_sidecar"
        and item["category"] == "repairable"
        and item["recommendation"] == "materialize_from_state_db"
        for item in report["items"]
    )


def test_empty_state_db_webui_row_is_unsafe_not_materialized(tmp_path):
    sid = _make_state_db(tmp_path / "state.db", sid="empty_state_row", messages=0)

    audit = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")

    assert any(
        item["session_id"] == sid
        and item["kind"] == "state_db_orphan_webui_row"
        and item["category"] == "unsafe_to_repair"
        and item["recommendation"] == "manual_review"
        for item in audit["items"]
    )
    assert not any(
        item["session_id"] == sid and item["kind"] == "state_db_missing_sidecar"
        for item in audit["items"]
    )

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")

    assert result["materialized"] == 0
    assert not (tmp_path / f"{sid}.json").exists()


def test_recover_missing_sidecars_from_state_db_ignores_subagent_row(tmp_path):
    sid = _make_state_db(tmp_path / "state.db", sid="subagent_001", source="subagent")

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")

    assert result["materialized"] == 0
    assert not (tmp_path / f"{sid}.json").exists()

    report = audit_session_recovery(tmp_path, state_db_path=tmp_path / "state.db")
    assert not any(item["session_id"] == sid for item in report["items"])


def test_recover_missing_sidecars_from_state_db_ignores_read_only_index_row(tmp_path):
    sid = _make_state_db(tmp_path / "state.db", sid="read_only_index_001")
    _write_index(tmp_path, [
        {
            "session_id": sid,
            "source_tag": "",
            "raw_source": "",
            "session_source": "",
            "read_only": True,
        }
    ])

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")

    assert result["materialized"] == 1
    sidecar = tmp_path / f"{sid}.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["session_id"] == sid
    assert data["source_tag"] == "webui"
    assert data["session_source"] == "webui"


def test_materialized_sidecar_round_trips_through_session_load(tmp_path, monkeypatch):
    """Schema parity guard: a materialized sidecar must be readable by Session.load
    and the resulting Session must have the same messages we put in state.db.

    Catches future schema drift where the hardcoded 35-key dict in
    _state_db_row_to_sidecar() falls out of sync with what Session.__init__
    expects. See Opus review on PR #2041 for context.
    """
    import api.models as _m

    sid = _make_state_db(tmp_path / "state.db", sid="rt_001", messages=3)

    monkeypatch.setattr(_m, "SESSION_DIR", tmp_path)

    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")
    assert result["materialized"] == 1

    loaded = _m.Session.load(sid)
    assert loaded is not None, "Session.load returned None for materialized sidecar"
    assert loaded.session_id == sid
    assert len(loaded.messages) == 3
    assert [m["content"] for m in loaded.messages] == [
        "message 1",
        "message 2",
        "message 3",
    ]
    assert loaded.model == "openai/gpt-5"
    assert loaded.parent_session_id == "parent-1"


def test_recover_missing_sidecars_uses_per_process_tmp_suffix(tmp_path):
    """The tmp filename used during reconciliation must include pid/tid so
    concurrent calls cannot corrupt each other's writes. See Opus review on
    PR #2041 (matches Session.save() pattern at api/models.py:484).
    """
    import os
    import threading

    _make_state_db(tmp_path / "state.db", sid="tmp_suffix_001", messages=1)

    # Snapshot the directory before, run reconciliation, then check no
    # generic ".json.reconcile.tmp" residue exists — it must have a
    # pid.tid suffix and be cleaned up after.
    result = recover_missing_sidecars_from_state_db(tmp_path, tmp_path / "state.db")
    assert result["materialized"] == 1

    # No leftover tmp files
    leftover = list(tmp_path.glob("*.reconcile.tmp*"))
    assert leftover == [], f"Reconciliation left tmp residue: {leftover}"

    # And the source explicitly references pid + tid in the suffix
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "api" / "session_recovery.py").read_text(encoding="utf-8")
    assert "os.getpid()" in src and "threading.current_thread().ident" in src, (
        ".reconcile.tmp suffix must include pid + tid for concurrency safety"
    )
