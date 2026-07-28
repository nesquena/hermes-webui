"""Scale regression for bounded WebUI session persistence (#0644)."""

from __future__ import annotations

import builtins
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import api.models as models
import api.routes as routes
from api.incremental_session_store import IncrementalSessionStore


@pytest.fixture
def scaled_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", {})
    monkeypatch.setattr(
        models,
        "_PERSISTED_SESSION_IDS_CACHE",
        (None, None, frozenset()),
    )

    rows = [
        {
            "session_id": f"historical_{index}",
            "title": f"Historical {index}",
            "workspace": str(tmp_path),
            "model": "test",
            "created_at": float(index),
            "updated_at": float(index),
            "message_count": 1,
            "pinned": False,
            "archived": False,
            "profile": "default",
            "padding": "x" * 2048,
        }
        for index in range(1_700)
    ]
    index_file.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    for row in rows:
        (session_dir / f"{row['session_id']}.json").write_text(
            json.dumps(row),
            encoding="utf-8",
        )

    session = models.Session(
        session_id="active_session",
        title="Active",
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "x" * (5 * 1024 * 1024)},
            {"role": "assistant", "content": "y" * (5 * 1024 * 1024)},
        ],
        profile="default",
    )
    session.save()
    return session, session_dir, index_file


def test_pending_save_avoids_full_sidecar_and_index_io(
    scaled_session_store,
    monkeypatch,
):
    session, _session_dir, index_file = scaled_session_store
    sidecar = session.path
    reads: list[Path] = []
    writes: list[tuple[Path, int]] = []
    encoded_sizes: list[int] = []
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    real_open = builtins.open
    real_encode = IncrementalSessionStore._encode

    def tracked_read_text(path, *args, **kwargs):
        reads.append(Path(path))
        return real_read_text(path, *args, **kwargs)

    def tracked_read_bytes(path, *args, **kwargs):
        reads.append(Path(path))
        return real_read_bytes(path, *args, **kwargs)

    class TrackedWriter:
        def __init__(self, handle, path):
            self._handle = handle
            self._path = Path(path)

        def write(self, value):
            writes.append((self._path, len(value.encode("utf-8"))))
            return self._handle.write(value)

        def __getattr__(self, name):
            return getattr(self._handle, name)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

    def tracked_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return TrackedWriter(handle, path) if "w" in mode else handle

    def tracked_encode(value):
        encoded = real_encode(value)
        encoded_sizes.append(len(encoded.encode("utf-8")))
        return encoded

    db_path = _session_dir / "_sessions.sqlite3"
    with sqlite3.connect(db_path) as conn:
        components_before = conn.execute(
            """
            SELECT component, position, payload_json
            FROM components WHERE session_id = ?
            ORDER BY component, position
            """,
            (session.session_id,),
        ).fetchall()

    monkeypatch.setattr(Path, "read_text", tracked_read_text)
    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    monkeypatch.setattr(builtins, "open", tracked_open)
    monkeypatch.setattr(IncrementalSessionStore, "_encode", staticmethod(tracked_encode))

    session.pending_user_message = "next turn"
    session.pending_started_at = time.time()
    session.active_stream_id = "stream_1"
    session.save(metadata_only=True)

    assert sidecar not in reads
    assert index_file not in reads
    assert sum(size for _path, size in writes) < 128 * 1024
    assert max(encoded_sizes) < 128 * 1024
    assert all(path != sidecar for path, _size in writes)
    assert all(path != index_file for path, _size in writes)
    with sqlite3.connect(db_path) as conn:
        components_after = conn.execute(
            """
            SELECT component, position, payload_json
            FROM components WHERE session_id = ?
            ORDER BY component, position
            """,
            (session.session_id,),
        ).fetchall()
    assert components_after == components_before


def test_committed_pending_state_survives_cache_clear(
    scaled_session_store,
):
    session, _session_dir, _index_file = scaled_session_store
    session.pending_user_message = "durable pending"
    session.pending_started_at = 12345.0
    session.active_stream_id = "stream_durable"
    session.save()

    models.SESSIONS.clear()
    loaded = models.Session.load(session.session_id)

    assert loaded is not None
    assert loaded.pending_user_message == "durable pending"
    assert loaded.pending_started_at == 12345.0
    assert loaded.active_stream_id == "stream_durable"
    assert loaded.messages == session.messages


def test_session_listing_is_backed_by_bounded_compact_rows(
    scaled_session_store,
    monkeypatch,
):
    session, _session_dir, index_file = scaled_session_store

    real_read_bytes = Path.read_bytes

    def reject_legacy_index_read(path, *args, **kwargs):
        if Path(path) == index_file:
            raise AssertionError("session listing read legacy global index")
        return real_read_bytes(path, *args, **kwargs)

    persisted = frozenset(
        {session.session_id, *(f"historical_{index}" for index in range(1_700))}
    )
    monkeypatch.setattr(models, "_persisted_session_ids_snapshot", lambda: persisted)
    IncrementalSessionStore(_session_dir).import_legacy_index_if_changed(index_file)
    monkeypatch.setattr(Path, "read_bytes", reject_legacy_index_read)
    durations = []
    rows = []
    for _index in range(20):
        started = time.perf_counter()
        rows = models.all_sessions(include_lineage_metadata=False)
        durations.append(time.perf_counter() - started)

    assert any(row["session_id"] == session.session_id for row in rows)
    assert len(rows) == 1_000
    p95 = sorted(durations)[18]
    assert p95 < 0.25, f"bounded session listing p95 was {p95:.3f}s"
    assert index_file.exists()


def test_sqlite_commit_recovers_after_uncommitted_writer_crash(
    scaled_session_store,
):
    session, session_dir, _index_file = scaled_session_store
    session.pending_user_message = "committed"
    session.save()

    db_path = session_dir / "_sessions.sqlite3"
    assert db_path.exists()
    crash_writer = """
import os
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
    ('{"session_id":"active_session","pending_user_message":"uncommitted"}', "active_session"),
)
os._exit(91)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", crash_writer, str(db_path)],
        check=False,
    )
    assert crashed.returncode == 91

    models.SESSIONS.clear()
    loaded = models.Session.load(session.session_id)

    assert loaded is not None
    assert loaded.pending_user_message == "committed"


def test_metadata_only_save_latency_does_not_scale_with_message_count(
    scaled_session_store,
):
    session, _session_dir, _index_file = scaled_session_store
    session.messages = [
        {"role": "user", "content": "x" * 200}
        for _index in range(50_000)
    ]

    durations = []
    for index in range(20):
        session.pending_user_message = f"pending {index}"
        started = time.perf_counter()
        session.save(metadata_only=True)
        durations.append(time.perf_counter() - started)

    p95 = sorted(durations)[18]
    assert p95 < 0.1, f"metadata-only save p95 was {p95:.3f}s"


def test_external_legacy_metadata_edit_preserves_newer_incremental_transcript(
    scaled_session_store,
):
    session, _session_dir, _index_file = scaled_session_store
    session.messages.append({"role": "assistant", "content": "new database tail"})
    session.save()

    legacy = json.loads(session.path.read_text(encoding="utf-8"))
    legacy["title"] = "Externally renamed"
    session.path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = models.Session.load(session.session_id)

    assert loaded is not None
    assert loaded.title == "Externally renamed"
    assert loaded.messages[-1]["content"] == "new database tail"


def test_empty_active_snapshot_cannot_erase_large_unmirrored_transcript(
    scaled_session_store,
):
    session, _session_dir, _index_file = scaled_session_store
    session.messages = []
    session.active_stream_id = "stream_guard"
    session.pending_user_message = "still pending"

    session.save()
    loaded = models.Session.load(session.session_id)

    assert loaded is not None
    assert len(loaded.messages) == 2


def test_corrupt_incremental_store_falls_back_to_legacy_sidecar(
    scaled_session_store,
):
    session, session_dir, _index_file = scaled_session_store
    db_path = session_dir / "_sessions.sqlite3"
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    db_path.write_bytes(b"not sqlite")

    loaded = models.Session.load(session.session_id)

    assert loaded is not None
    assert loaded.messages == session.messages


def test_listing_and_point_lookup_remain_bounded_beyond_two_thousand(
    tmp_path,
    monkeypatch,
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", {})
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    rows = [
        {
            "session_id": f"bounded_{index}",
            "title": f"Bounded {index}",
            "created_at": float(index),
            "updated_at": float(index),
            "message_count": 1,
            "source_tag": "webui",
        }
        for index in range(2_101)
    ]
    store = IncrementalSessionStore(session_dir)
    store.update_index(rows)
    with sqlite3.connect(store.path) as conn:
        conn.executemany(
            """
            INSERT INTO sessions(session_id, metadata_json)
            VALUES (?, ?)
            """,
            [(row["session_id"], json.dumps(row)) for row in rows],
        )

    listed = models.all_sessions(include_lineage_metadata=False)

    assert len(listed) == 1_000
    assert routes._session_index_marks_was_webui("bounded_0") is True


def test_incremental_session_lifecycle_delete_removes_payload_and_index(
    scaled_session_store,
):
    session, session_dir, _index_file = scaled_session_store
    models.delete_incremental_session(session.session_id)
    store = IncrementalSessionStore(session_dir)

    assert store.load_payload(session.session_id) is None
    assert store.get_index(session.session_id) is None
