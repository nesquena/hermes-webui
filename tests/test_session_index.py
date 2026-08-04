"""
Tests for the incremental session index in api/models.py.

Validates:
  - Incremental patch correctness (existing entries preserved, updated)
  - New session appended to existing index
  - First call (no index file) triggers full rebuild
  - Corrupt index triggers fallback to full rebuild
  - Concurrent saves don't lose data
  - Atomic write leaves no .tmp file behind
  - Deadlock guard on fallback path
"""
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import api.models as models
from api.models import Session, _write_session_index, prune_session_from_index


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR and SESSION_INDEX_FILE to a temp directory
    so tests don't touch the real session store.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    # Also patch the module-level references that Session uses
    monkeypatch.setattr(models.Session, "__module__", models.__name__)

    # Clear the in-memory SESSIONS and persisted-id caches to avoid bleed.
    models.SESSIONS.clear()
    if hasattr(models, "_PERSISTED_SESSION_IDS_CACHE"):
        models._PERSISTED_SESSION_IDS_CACHE = (None, None, frozenset())

    yield session_dir, index_file

    models.SESSIONS.clear()
    if hasattr(models, "_PERSISTED_SESSION_IDS_CACHE"):
        models._PERSISTED_SESSION_IDS_CACHE = (None, None, frozenset())


def _make_session(session_id, title="Untitled", updated_at=None):
    """Helper to create a Session with a known ID and title."""
    s = Session(session_id=session_id, title=title, messages=[{"role": "user", "content": "hi"}])
    if updated_at is not None:
        s.updated_at = updated_at
    return s


def _write_index_file(index_file, entries):
    """Write entries list to the index file atomically."""
    tmp = index_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(index_file))


def _read_index(index_file):
    """Read and parse the session index file."""
    return json.loads(index_file.read_text(encoding="utf-8"))


def test_compact_exposes_last_message_at_from_message_timestamp():
    s = Session(
        session_id="sess_time",
        title="Time",
        updated_at=300.0,
        messages=[
            {"role": "user", "content": "old", "_ts": 100.0},
            {"role": "tool", "content": "ignore", "timestamp": 400.0},
            {"role": "assistant", "content": "latest", "timestamp": 200.0},
        ],
    )

    compact = s.compact()

    assert compact["updated_at"] == 300.0
    assert compact["last_message_at"] == 200.0


def test_compact_ignores_empty_partial_activity_for_last_message_at():
    s = Session(
        session_id="sess_partial_tail",
        title="Partial tail",
        updated_at=300.0,
        messages=[
            {"role": "user", "content": "today question", "timestamp": 200.0},
            {"role": "assistant", "content": "today answer", "timestamp": 201.0},
            {
                "role": "assistant",
                "content": "",
                "_partial": True,
                "timestamp": 100.0,
                "reasoning": "old cancelled thinking",
                "_partial_tool_calls": [{"name": "terminal", "done": True}],
            },
        ],
    )

    compact = s.compact()

    assert compact["updated_at"] == 300.0
    assert compact["last_message_at"] == 201.0


def test_session_load_allows_hyphenated_safe_ids_but_rejects_traversal():
    sid = "api-182894de593468b6"
    s = _make_session(sid, "API session", updated_at=100)
    s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    assert Session.load(sid) is not None
    assert Session.load_metadata_only(sid) is not None
    assert Session.load("bad/../id") is None
    assert Session.load_metadata_only("bad.id") is None


def test_full_index_rebuild_includes_hyphenated_sessions():
    sid = "reachy-voice-20260513-1131-d5542adf"
    s = _make_session(sid, "Reachy voice", updated_at=100)
    s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=None)

    ids = [entry["session_id"] for entry in _read_index(models.SESSION_INDEX_FILE)]
    assert sid in ids


def test_prune_session_from_index_removes_requested_row_only():
    index_file = models.SESSION_INDEX_FILE
    s_a = _make_session("sess_a", "A", updated_at=100)
    s_b = _make_session("sess_b", "B", updated_at=200)
    s_a.save()
    s_b.save()

    prune_session_from_index("sess_a")

    index = _read_index(index_file)
    ids = [entry["session_id"] for entry in index]
    assert ids == ["sess_b"]
    assert index_file.exists()
    assert s_a.path.exists()
    assert s_b.path.exists()


def test_all_sessions_backfills_last_message_at_for_legacy_index_rows():
    index_file = models.SESSION_INDEX_FILE
    s = Session(
        session_id="sess_legacy_index",
        title="Legacy Index",
        updated_at=300.0,
        messages=[{"role": "assistant", "content": "reply", "_ts": 100.0}],
    )
    s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_index_file(
        index_file,
        [
            {
                "session_id": s.session_id,
                "title": s.title,
                "updated_at": s.updated_at,
                "workspace": s.workspace,
                "model": s.model,
                "message_count": 1,
                "created_at": s.created_at,
                "pinned": False,
                "archived": False,
            }
        ],
    )

    rows = models.all_sessions()

    assert rows[0]["session_id"] == s.session_id
    assert rows[0]["last_message_at"] == 100.0

    # Backfill must also be persisted to the index so subsequent /api/sessions
    # polls don't re-read every legacy session file.  Without this, a 5-second
    # poll cycle re-loads every legacy session JSON on every tick until each
    # session is independently saved.
    persisted = _read_index(index_file)
    assert persisted[0]["session_id"] == s.session_id
    assert persisted[0].get("last_message_at") == 100.0


def test_all_sessions_prune_batches_persisted_id_snapshot(monkeypatch):
    """Index pruning should not probe each backing file through the helper."""
    index_file = models.SESSION_INDEX_FILE
    entries = [
        {
            "session_id": "sess_a",
            "title": "Alpha",
            "updated_at": 200.0,
            "last_message_at": 200.0,
            "workspace": "/tmp",
            "model": "test",
            "message_count": 1,
            "created_at": 100.0,
            "pinned": False,
            "archived": False,
        },
        {
            "session_id": "sess_b",
            "title": "Bravo",
            "updated_at": 150.0,
            "last_message_at": 150.0,
            "workspace": "/tmp",
            "model": "test",
            "message_count": 1,
            "created_at": 90.0,
            "pinned": False,
            "archived": False,
        },
    ]
    for entry in entries:
        (models.SESSION_DIR / f"{entry['session_id']}.json").write_text(
            "{}",
            encoding="utf-8",
        )
    _write_index_file(index_file, entries)

    def _assert_not_called(session_id, in_memory_ids=None):
        raise AssertionError("all_sessions should batch persisted ids before pruning")

    monkeypatch.setattr(models, "_index_entry_exists", _assert_not_called)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == ["sess_a", "sess_b"]


# ── 6. test_incremental_patch_correctness ─────────────────────────────────

def test_incremental_patch_correctness():
    """Pre-write an index with 3 sessions (A, B, C). Create an updated
    Session for B with a new title. Call _write_session_index(updates=[B]).
    Verify A and C are unchanged, B has the new title, sort order preserved.
    """


    # We need to get the fixture values — but since it's autouse, the monkeypatch
    # has already been applied. Access the patched values directly.
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # Create 3 sessions with different timestamps
    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sB = _make_session("sess_b", "Bravo", updated_at=200.0)
    sC = _make_session("sess_c", "Charlie", updated_at=300.0)

    # Write session files to disk (so full rebuild can find them)
    for s in (sA, sB, sC):
        s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Build initial index
    _write_session_index(updates=None)
    index = _read_index(index_file)
    assert len(index) == 3

    # Now update B with a new title
    sB_updated = _make_session("sess_b", "Bravo Updated", updated_at=250.0)
    sB_updated.path.write_text(
        json.dumps(sB_updated.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Incremental update
    _write_session_index(updates=[sB_updated])

    # Verify
    index = _read_index(index_file)
    index_map = {e["session_id"]: e for e in index}

    assert index_map["sess_a"]["title"] == "Alpha", "A should be unchanged"
    assert index_map["sess_c"]["title"] == "Charlie", "C should be unchanged"
    assert index_map["sess_b"]["title"] == "Bravo Updated", "B should have new title"

    # Sort order: Charlie (300) > Bravo Updated (250) > Alpha (100)
    assert index[0]["session_id"] == "sess_c"
    assert index[1]["session_id"] == "sess_b"
    assert index[2]["session_id"] == "sess_a"


# ── 7. test_new_session_appended_to_index ─────────────────────────────────

def test_new_session_appended_to_index():
    """Pre-write index with sessions A, B. Call _write_session_index(updates=[C])
    where C is not in the existing index. Verify C appears in the index.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sB = _make_session("sess_b", "Bravo", updated_at=200.0)

    for s in (sA, sB):
        s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=None)

    # Create a new session C not in the index
    sC = _make_session("sess_c", "Charlie", updated_at=300.0)
    sC.path.write_text(json.dumps(sC.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=[sC])

    index = _read_index(index_file)
    ids = {e["session_id"] for e in index}
    assert "sess_c" in ids, "New session C should appear in the index"
    assert "sess_a" in ids
    assert "sess_b" in ids


def test_incremental_update_prunes_stale_entries():
    """Ghost rows whose backing JSON file is gone must be dropped on the fast path.

    This covers session-id rotation paths (e.g. compression) where the old id can
    linger in `_index.json` after the file has been renamed.
    """
    index_file = models.SESSION_INDEX_FILE

    stale = {
        "session_id": "ghost_sid",
        "title": "Ghost",
        "updated_at": 150.0,
        "workspace": "/tmp",
        "model": "test",
        "message_count": 1,
        "created_at": 100.0,
        "pinned": False,
        "archived": False,
    }
    _write_index_file(index_file, [stale])

    sA = _make_session("sess_a", "Alpha", updated_at=200.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=[sA])

    index = _read_index(index_file)
    ids = {e["session_id"] for e in index}
    assert "sess_a" in ids
    assert "ghost_sid" not in ids, "stale entry with no backing file must be pruned"


def test_load_metadata_only_does_not_parse_large_message_body():
    """Large sessions must keep the metadata-only path cheap."""
    s = Session(
        session_id="sess_large",
        title="Large Session",
        messages=[{"role": "assistant", "content": "x" * 200_000}],
        tool_calls=[{"id": "tool_1", "name": "read_file", "result": "y" * 10_000}],
        input_tokens=123,
        output_tokens=45,
    )
    s.save()

    with patch.object(Session, "load", side_effect=AssertionError("full load should not run")):
        meta = Session.load_metadata_only("sess_large")

    assert meta is not None
    assert meta.session_id == "sess_large"
    assert meta.title == "Large Session"
    assert meta.input_tokens == 123
    assert meta.output_tokens == 45
    assert meta.messages == []
    assert meta.tool_calls == []
    assert meta.compact()["message_count"] == 1


def test_metadata_only_get_session_does_not_poison_full_session_cache():
    s = Session(
        session_id="sess_cache",
        title="Cache Guard",
        messages=[{"role": "user", "content": "hi"}],
    )
    s.save(skip_index=True)

    meta = models.get_session("sess_cache", metadata_only=True)
    assert meta.messages == []
    assert "sess_cache" not in models.SESSIONS

    full = models.get_session("sess_cache")
    assert full.messages == [{"role": "user", "content": "hi"}]
    assert models.SESSIONS["sess_cache"] is full


def test_pre_compression_snapshot_marker_is_persisted_and_compact():
    """Pre-compression snapshots keep a distinct marker from manual archived state."""
    s = Session(
        session_id="sess_snapshot",
        title="Before Compression",
        messages=[{"role": "user", "content": "hi"}],
        pre_compression_snapshot=True,
    )

    s.save()

    payload = json.loads(s.path.read_text(encoding="utf-8"))
    assert payload["pre_compression_snapshot"] is True
    compact = s.compact()
    assert compact["pre_compression_snapshot"] is True
    assert compact["archived"] is False


def test_pre_compression_snapshot_hidden_from_active_sidebar_but_file_remains(monkeypatch):
    """Preserved compression snapshots should not appear as active sidebar rows."""
    snapshot = Session(
        session_id="old_sid",
        title="Long Conversation",
        messages=[{"role": "user", "content": "pre-compression history"}],
        pre_compression_snapshot=True,
        updated_at=100.0,
    )
    continuation = Session(
        session_id="new_sid",
        title="Long Conversation",
        messages=[{"role": "user", "content": "compressed continuation"}],
        parent_session_id="old_sid",
        updated_at=200.0,
    )
    snapshot.save(touch_updated_at=False)
    continuation.save(touch_updated_at=False)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert snapshot.path.exists(), "snapshot JSON must stay available for lineage traversal"
    assert [row["session_id"] for row in rows] == ["new_sid"]


def test_forked_child_of_snapshot_stays_visible_when_snapshot_is_fuller(monkeypatch):
    """A manual fork should not be grouped into a snapshot's hidden continuation lineage.

    Even when the parent snapshot has a fuller transcript and a newer
    timestamp, a `/branch` fork is independently discoverable and should stay in
    the active sidebar rows as its own root.
    """
    snapshot = Session(
        session_id="snapshot_parent",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "root"},
            {"role": "assistant", "content": "compressed context"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ],
        pre_compression_snapshot=True,
        parent_session_id="snapshot_origin",
        updated_at=300.0,
        last_message_at=300.0,
    )
    fork = Session(
        session_id="manual_fork_child",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "new branch"},
            {"role": "assistant", "content": "reply"},
        ],
        parent_session_id="snapshot_parent",
        session_source="fork",
        updated_at=200.0,
        last_message_at=200.0,
    )
    snapshot.save(touch_updated_at=False)
    fork.save(touch_updated_at=False)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert snapshot.path.exists(), "snapshot JSON must stay available for lineage traversal"
    assert rows[0]["session_id"] == "manual_fork_child"
    assert rows[0]["session_source"] == "fork"
    assert models._sidebar_lineage_root_id(
        {
            "session_id": "lineage_child",
            "_lineage_root_id": "lineage_root",
            "parent_session_id": "snapshot_parent",
        },
        {
            "snapshot_parent": {
                "session_id": "snapshot_parent",
                "parent_session_id": "snapshot_origin",
            }
        },
    ) == "lineage_root"
    assert models._sidebar_lineage_root_id(
        {
            "session_id": "child_session_sid",
            "relationship_type": "child_session",
            "parent_session_id": "snapshot_parent",
        },
        {
            "snapshot_parent": {
                "session_id": "snapshot_parent",
                "parent_session_id": "snapshot_origin",
            }
        },
    ) == "child_session_sid"


def test_fuller_pre_compression_snapshot_replaces_shorter_visible_segment(monkeypatch):
    """If the hidden snapshot has the fuller transcript, keep it reachable.

    Auto-compression can leave a visible continuation segment in the sidebar
    while the fuller transcript remains on disk marked as a pre-compression
    snapshot. In that case the default session list should prefer the fuller
    transcript so the conversation does not look like recent messages vanished.
    """
    snapshot = Session(
        session_id="full_parent",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "latest user"},
            {"role": "assistant", "content": "latest answer"},
        ],
        pre_compression_snapshot=True,
        updated_at=300.0,
        last_message_at=300.0,
    )
    continuation = Session(
        session_id="short_child",
        title="Long Conversation",
        messages=[{"role": "user", "content": "first"}],
        parent_session_id="full_parent",
        updated_at=250.0,
        last_message_at=250.0,
    )
    snapshot.save(touch_updated_at=False)
    continuation.save(touch_updated_at=False)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == ["full_parent"]
    assert rows[0]["message_count"] == 4
    assert rows[0]["pre_compression_snapshot"] is True


def test_complete_snapshot_refresh_ids_do_not_follow_mtime_when_sidebar_metadata_is_complete(monkeypatch):
    """Complete snapshot metadata should stay on the index fastpath.

    The stale-snapshot rescue path is for incomplete legacy rows. Modern index
    rows include user_message_count plus last_message_at; refreshing every such
    snapshot whose sidecar mtime is newer creates O(history) sidecar fan-out.
    """
    rows = [
        {
            "session_id": "snapshot_complete",
            "title": "Complete Snapshot",
            "message_count": 4,
            "user_message_count": 2,
            "updated_at": 100.0,
            "last_message_at": 100.0,
            "pre_compression_snapshot": True,
            "parent_session_id": "root_sid",
        },
        {
            "session_id": "visible_child",
            "title": "Visible Child",
            "message_count": 4,
            "user_message_count": 2,
            "updated_at": 120.0,
            "last_message_at": 120.0,
            "parent_session_id": "snapshot_complete",
        },
    ]
    monkeypatch.setattr(models, "_sidecar_mtime_after_index_timestamp", lambda _row: True)

    assert models._stale_snapshot_metadata_refresh_ids(rows) == set()


def test_stale_zero_message_snapshot_refresh_ids_follow_mtime(monkeypatch):
    """A snapshot with message_count=0 but user messages is not complete metadata."""
    rows = [
        {
            "session_id": "snapshot_stale_zero",
            "title": "Stale Zero Snapshot",
            "message_count": 0,
            "user_message_count": 2,
            "updated_at": 100.0,
            "last_message_at": 100.0,
            "pre_compression_snapshot": True,
            "parent_session_id": "root_sid",
        },
        {
            "session_id": "visible_child",
            "title": "Visible Child",
            "message_count": 1,
            "user_message_count": 1,
            "updated_at": 120.0,
            "last_message_at": 120.0,
            "parent_session_id": "snapshot_stale_zero",
        },
    ]
    monkeypatch.setattr(models, "_sidecar_mtime_after_index_timestamp", lambda _row: True)

    assert models._stale_snapshot_metadata_refresh_ids(rows) == {"snapshot_stale_zero"}


def test_stale_index_fuller_pre_compression_snapshot_uses_sidecar_metadata(monkeypatch):
    """A stale index must not hide the fuller pre-compression sidecar.

    Compression can leave _index.json with the snapshot's old count/timestamp
    while the sidecar later contains more transcript rows than the visible
    continuation. all_sessions() must refresh snapshot metadata before deciding
    whether to hide it, or the sidebar makes messages look lost.
    """
    snapshot = Session(
        session_id="stale_full_parent",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 101.0},
            {"role": "user", "content": "latest user", "timestamp": 300.0},
            {"role": "assistant", "content": "latest answer", "timestamp": 301.0},
        ],
        pre_compression_snapshot=True,
        parent_session_id="root_sid",
        updated_at=301.0,
    )
    continuation = Session(
        session_id="stale_short_child",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 101.0},
        ],
        parent_session_id="stale_full_parent",
        updated_at=250.0,
    )
    snapshot.save(touch_updated_at=False)
    continuation.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "stale_full_parent",
                "title": "Long Conversation",
                "message_count": 2,
                "created_at": 100.0,
                "updated_at": 200.0,
                "last_message_at": 200.0,
                "pinned": False,
                "archived": False,
                "pre_compression_snapshot": True,
                "parent_session_id": "root_sid",
            },
            {
                "session_id": "stale_short_child",
                "title": "Long Conversation",
                "message_count": 2,
                "created_at": 250.0,
                "updated_at": 250.0,
                "last_message_at": 250.0,
                "pinned": False,
                "archived": False,
                "parent_session_id": "stale_full_parent",
            },
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == ["stale_full_parent"]
    assert rows[0]["message_count"] == 4
    assert rows[0]["last_message_at"] == 301.0
    assert rows[0]["pre_compression_snapshot"] is True


def test_indexed_fuller_pre_compression_snapshot_does_not_refresh_sidecar(monkeypatch):
    """A truthful index row must not read the snapshot sidecar on every poll."""
    snapshot = Session(
        session_id="indexed_full_parent",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 101.0},
            {"role": "user", "content": "latest user", "timestamp": 300.0},
            {"role": "assistant", "content": "latest answer", "timestamp": 301.0},
        ],
        pre_compression_snapshot=True,
        parent_session_id="root_sid",
        updated_at=301.0,
    )
    continuation = Session(
        session_id="indexed_short_child",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 101.0},
        ],
        parent_session_id="indexed_full_parent",
        updated_at=250.0,
    )
    snapshot.save(touch_updated_at=False)
    continuation.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "indexed_full_parent",
                "title": "Long Conversation",
                "message_count": 4,
                "created_at": 100.0,
                "updated_at": 301.0,
                "last_message_at": 301.0,
                "pinned": False,
                "archived": False,
                "pre_compression_snapshot": True,
                "parent_session_id": "root_sid",
            },
            {
                "session_id": "indexed_short_child",
                "title": "Long Conversation",
                "message_count": 2,
                "created_at": 250.0,
                "updated_at": 250.0,
                "last_message_at": 250.0,
                "pinned": False,
                "archived": False,
                "parent_session_id": "indexed_full_parent",
            },
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    with patch.object(Session, "load_metadata_only", side_effect=AssertionError("truthful snapshot index should not refresh sidecar")):
        rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == ["indexed_full_parent"]
    assert rows[0]["message_count"] == 4


def test_orphan_pre_compression_snapshot_does_not_refresh_sidecar(monkeypatch):
    """Snapshot refresh stays scoped to lineages with a visible continuation."""
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "orphan_snapshot",
                "title": "Archived Segment",
                "message_count": 3,
                "created_at": 100.0,
                "updated_at": 100.0,
                "last_message_at": 100.0,
                "pinned": False,
                "archived": False,
                "pre_compression_snapshot": True,
                "parent_session_id": "root_sid",
            },
        ],
    )
    (models.SESSION_DIR / "orphan_snapshot.json").write_text(
        json.dumps(
            {
                "session_id": "orphan_snapshot",
                "title": "Archived Segment",
                "messages": [{"role": "user", "content": "sidecar"}],
                "message_count": 99,
                "updated_at": 999.0,
                "pre_compression_snapshot": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    with patch.object(Session, "load_metadata_only", side_effect=AssertionError("orphan snapshots must not refresh sidecars")):
        rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == ["orphan_snapshot"]
    assert rows[0]["message_count"] == 3


def test_newer_continuation_stays_visible_alongside_older_fuller_snapshot(monkeypatch):
    """Do not hide either side when recency and completeness disagree.

    Compression snapshots can have a higher message count while still being
    older than the continuation that contains the latest user-visible turns.
    The sidebar should keep the newer continuation visible and also expose the
    fuller snapshot so neither side of a split lineage looks lost.
    """
    snapshot = Session(
        session_id="older_full_parent",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"},
        ],
        pre_compression_snapshot=True,
        updated_at=300.0,
        last_message_at=300.0,
    )
    continuation = Session(
        session_id="newer_short_child",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "latest task"},
            {"role": "assistant", "content": "latest result"},
        ],
        parent_session_id="older_full_parent",
        updated_at=450.0,
        last_message_at=450.0,
    )
    snapshot.save(touch_updated_at=False)
    continuation.save(touch_updated_at=False)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == [
        "newer_short_child",
        "older_full_parent",
    ]
    assert rows[0]["pre_compression_snapshot"] is False
    assert rows[0]["message_count"] == 2
    assert rows[1]["pre_compression_snapshot"] is True
    assert rows[1]["message_count"] == 4


def test_all_sessions_uses_sidecar_metadata_for_runtime_rows_when_index_message_count_is_stale(monkeypatch):
    """Runtime-shaped rows may refresh from fuller sidecar metadata."""
    session = Session(
        session_id="stale_index_sid",
        title="Reachy Mini Integration",
        messages=[
            {"role": "user", "content": "one", "timestamp": 100.0},
            {"role": "assistant", "content": "two", "timestamp": 101.0},
            {"role": "user", "content": "three", "timestamp": 102.0},
        ],
        updated_at=102.0,
    )
    session.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "stale_index_sid",
                "title": "Reachy Mini Integration",
                "message_count": 1,
                "created_at": 100.0,
                "updated_at": 100.0,
                "last_message_at": 100.0,
                "pinned": False,
                "archived": False,
                "active_stream_id": "stream-stale-index",
            }
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert rows[0]["session_id"] == "stale_index_sid"
    assert rows[0]["message_count"] == 3
    assert rows[0]["last_message_at"] == 102.0


def test_all_sessions_sidecar_refresh_stays_metadata_only(monkeypatch):
    """Refreshing runtime sidebar rows must not hydrate large sidecar messages."""
    session = Session(
        session_id="metadata_refresh_sid",
        title="Metadata Refresh",
        messages=[
            {"role": "user", "content": "one", "timestamp": 100.0},
            {"role": "assistant", "content": "x" * 200_000, "timestamp": 101.0},
            {"role": "user", "content": "three", "timestamp": 102.0},
        ],
        parent_session_id="parent_sid",
        updated_at=102.0,
    )
    session.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "metadata_refresh_sid",
                "title": "Metadata Refresh",
                "message_count": 1,
                "created_at": 100.0,
                "updated_at": 100.0,
                "last_message_at": 100.0,
                "pinned": False,
                "archived": False,
                "active_stream_id": "stream-metadata-refresh",
            }
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    with patch.object(Session, "load", side_effect=AssertionError("full sidecar load should not run")):
        rows = models.all_sessions()

    assert rows[0]["session_id"] == "metadata_refresh_sid"
    assert rows[0]["message_count"] == 3
    assert rows[0]["last_message_at"] == 102.0


def test_all_sessions_does_not_refresh_fresh_lineage_rows_from_sidecars(monkeypatch):
    """Fresh lineage rows are enriched from state.db; do not read every sidecar per poll."""
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "lineage_sid",
                "title": "Lineage Row",
                "message_count": 7,
                "created_at": 100.0,
                "updated_at": time.time() + 60.0,
                "last_message_at": time.time() + 60.0,
                "pinned": False,
                "archived": False,
                "parent_session_id": "parent_sid",
                "_lineage_root_id": "root_sid",
                "_compression_segment_count": 2,
            }
        ],
    )
    (models.SESSION_DIR / "lineage_sid.json").write_text(
        json.dumps(
            {
                "session_id": "lineage_sid",
                "title": "Lineage Row",
                "messages": [{"role": "user", "content": "sidecar"}],
                "message_count": 99,
                "updated_at": 200.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    with patch.object(Session, "load_metadata_only", side_effect=AssertionError("fresh lineage rows must not refresh sidecars")):
        rows = models.all_sessions()

    assert rows[0]["session_id"] == "lineage_sid"
    assert rows[0]["message_count"] == 7


def test_complete_lineage_refresh_gate_ignores_mtime_when_sidebar_metadata_is_complete(monkeypatch):
    """Filesystem mtime alone must not force sidecar refresh for complete lineage rows."""
    row = {
        "session_id": "complete_lineage_gate",
        "title": "Complete Lineage Gate",
        "message_count": 8,
        "user_message_count": 4,
        "updated_at": 200.0,
        "last_message_at": 200.0,
        "parent_session_id": "snapshot_parent",
        "_lineage_root_id": "snapshot_parent",
        "_compression_segment_count": 2,
    }
    monkeypatch.setattr(models, "_sidecar_mtime_after_index_timestamp", lambda _row: True)

    assert models._row_may_need_sidecar_metadata_refresh(row) is False


def test_all_sessions_does_not_refresh_complete_lineage_rows_with_newer_sidecar_mtime(monkeypatch):
    """Complete indexed lineage rows should not fan out into sidecar reads per poll.

    Real WebUI histories can have hundreds of compression-linked rows whose
    sidecar file mtime is newer than the logical last_message_at. When the index
    already has complete sidebar counters/timestamps, state.db lineage
    enrichment owns the lineage fields; /api/sessions must not re-hydrate every
    sidecar just because its filesystem mtime is newer.
    """
    session = Session(
        session_id="complete_lineage_child",
        title="Complete Lineage Child",
        messages=[
            {"role": "user", "content": "a", "timestamp": 100.0},
            {"role": "assistant", "content": "b", "timestamp": 101.0},
            {"role": "user", "content": "c", "timestamp": 102.0},
            {"role": "assistant", "content": "d", "timestamp": 103.0},
        ],
        parent_session_id="snapshot_parent",
        updated_at=103.0,
        last_message_at=103.0,
    )
    session.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "complete_lineage_child",
                "title": "Complete Lineage Child",
                "message_count": 4,
                "user_message_count": 2,
                "created_at": 100.0,
                "updated_at": 103.0,
                "last_message_at": 103.0,
                "pinned": False,
                "archived": False,
                "parent_session_id": "snapshot_parent",
                "_lineage_root_id": "snapshot_parent",
                "_compression_segment_count": 2,
            }
        ],
    )
    # Ensure the filesystem mtime still looks newer than the logical timestamp,
    # which is the production fan-out trigger we are guarding against.
    assert models._sidecar_mtime_after_index_timestamp({
        "session_id": "complete_lineage_child",
        "last_message_at": 103.0,
        "updated_at": 103.0,
    })
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    with patch.object(Session, "load_metadata_only", side_effect=AssertionError("complete lineage rows must stay on the index fastpath")):
        rows = models.all_sessions()

    assert rows[0]["session_id"] == "complete_lineage_child"
    assert rows[0]["message_count"] == 4


def test_all_sessions_refreshes_stale_visible_continuation_metadata(monkeypatch):
    """A visible continuation whose sidecar advanced after _index.json must refresh metadata.

    Compression lineage rows can remain the active sidebar representative while
    their sidecar gains the latest assistant turn. If the index row stays stale,
    the sidebar/topbar reports an old message count and the UI can look like the
    newest messages disappeared.
    """
    session = Session(
        session_id="stale_visible_child",
        title="Long Conversation",
        messages=[
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 101.0},
            {"role": "user", "content": "latest", "timestamp": 102.0},
            {"role": "assistant", "content": "latest answer", "timestamp": 103.0},
        ],
        parent_session_id="snapshot_parent",
        updated_at=103.0,
        last_message_at=103.0,
    )
    session.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "stale_visible_child",
                "title": "Long Conversation",
                "message_count": 2,
                "created_at": 100.0,
                "updated_at": 100.0,
                "last_message_at": 100.0,
                "pinned": False,
                "archived": False,
                "parent_session_id": "snapshot_parent",
                "_lineage_root_id": "snapshot_parent",
                "_compression_segment_count": 2,
            }
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert rows[0]["session_id"] == "stale_visible_child"
    assert rows[0]["message_count"] == 4
    assert rows[0]["last_message_at"] == 103.0


def test_all_sessions_refreshes_stale_zero_count_row_from_sidecar(monkeypatch):
    """A zero-message indexed row can still have real transcript content on disk."""
    session = Session(
        session_id="stale_zero_count",
        title="Recovered Session",
        messages=[
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 101.0},
        ],
        updated_at=101.0,
        last_message_at=101.0,
    )
    session.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "stale_zero_count",
                "title": "Recovered Session",
                "message_count": 0,
                "user_message_count": 1,
                "created_at": 100.0,
                "updated_at": 1.0,
                "last_message_at": 1.0,
                "pinned": False,
                "archived": False,
            },
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == ["stale_zero_count"]
    assert rows[0]["message_count"] == 2
    assert rows[0]["last_message_at"] == 101.0


def test_all_sessions_refreshes_stale_zero_count_snapshot_row_from_sidecar(monkeypatch):
    """Snapshot rows follow the same stale-zero sidecar refresh path."""
    session = Session(
        session_id="stale_zero_snapshot_count",
        title="Recovered Snapshot Session",
        messages=[
            {"role": "user", "content": "first", "timestamp": 100.0},
            {"role": "assistant", "content": "second", "timestamp": 101.0},
        ],
        updated_at=101.0,
        last_message_at=101.0,
        pre_compression_snapshot=True,
    )
    session.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "stale_zero_snapshot_count",
                "title": "Recovered Snapshot Session",
                "message_count": 0,
                "user_message_count": 1,
                "created_at": 100.0,
                "updated_at": 1.0,
                "last_message_at": 1.0,
                "pinned": False,
                "archived": False,
                "pre_compression_snapshot": True,
            },
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    rows = models.all_sessions()

    assert [row["session_id"] for row in rows] == ["stale_zero_snapshot_count"]
    assert rows[0]["message_count"] == 2
    assert rows[0]["last_message_at"] == 101.0


def test_all_sessions_skips_refresh_for_real_empty_untitled_drafts(monkeypatch):
    """Keep genuine empty drafts on the cheap path when they have no user turns."""
    draft = Session(
        session_id="untitled_empty_draft",
        title="Untitled",
        messages=[],
        updated_at=100.0,
        last_message_at=100.0,
    )
    draft.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "untitled_empty_draft",
                "title": "Untitled",
                "message_count": 0,
                "user_message_count": 0,
                "created_at": 100.0,
                "updated_at": 100.0,
                "last_message_at": 100.0,
                "pinned": False,
                "archived": False,
            },
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    with patch.object(Session, "load_metadata_only", side_effect=AssertionError("empty draft should not refresh sidecar")):
        rows = models.all_sessions()

    assert rows == []


def test_all_sessions_does_not_refresh_plain_branch_fork_from_sidecar(monkeypatch):
    """A plain /branch fork (session_source='fork') must NOT trigger a sidecar refresh.

    Forks carry parent_session_id (#1342) but have no compression sidecar drift
    to correct. Including them in the continuation refresh gate would call
    load_metadata_only() on every fork row on every /api/sessions poll (the
    molasses #3770 guards against). The gate must exclude session_source='fork'
    so a fork's stale-looking index row is left alone.
    """
    session = Session(
        session_id="plain_fork_child",
        title="Forked Conversation",
        messages=[
            {"role": "user", "content": "a", "timestamp": 100.0},
            {"role": "assistant", "content": "b", "timestamp": 101.0},
            {"role": "user", "content": "c", "timestamp": 102.0},
            {"role": "assistant", "content": "d", "timestamp": 103.0},
        ],
        parent_session_id="some_parent",
        session_source="fork",
        updated_at=103.0,
        last_message_at=103.0,
    )
    session.save(touch_updated_at=False)
    _write_index_file(
        models.SESSION_INDEX_FILE,
        [
            {
                "session_id": "plain_fork_child",
                "title": "Forked Conversation",
                "message_count": 2,
                "created_at": 100.0,
                "updated_at": 100.0,
                "last_message_at": 100.0,
                "pinned": False,
                "archived": False,
                "parent_session_id": "some_parent",
                "session_source": "fork",
            }
        ],
    )
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    # A fork that has not been hydrated must not be promoted from the (stale)
    # indexed count — the row stays as the index reports it (no sidecar refresh).
    def _fail_load(_sid):
        raise AssertionError("plain fork must not trigger load_metadata_only refresh")

    monkeypatch.setattr(models.Session, "load_metadata_only", staticmethod(_fail_load))

    rows = models.all_sessions()
    fork_row = next(r for r in rows if r["session_id"] == "plain_fork_child")
    assert fork_row["message_count"] == 2  # left at the indexed value, not refreshed


def test_load_metadata_only_skips_index_read_when_sidecar_has_message_count(monkeypatch):
    """Modern sidecars already carry message_count; avoid an _index.json read per row."""
    session = Session(
        session_id="metadata_count_sid",
        title="Metadata Count",
        messages=[{"role": "user", "content": "hi"}],
    )
    session.save(touch_updated_at=False)

    def _fail_index_lookup(_sid):
        raise AssertionError("message_count sidecar should not need index lookup")

    monkeypatch.setattr(models, "_lookup_index_message_count", _fail_index_lookup)

    meta = Session.load_metadata_only("metadata_count_sid")

    assert meta is not None
    assert meta.compact()["message_count"] == 1




def test_all_sessions_reuses_loaded_index_counts_for_legacy_sidecar_refresh(monkeypatch):
    """Refreshing multiple legacy lineage rows must not parse _index.json per row."""
    index_file = models.SESSION_INDEX_FILE
    rows = []
    for sid, count in (("legacy_lineage_a", 3), ("legacy_lineage_b", 4)):
        payload = {
            "session_id": sid,
            "title": sid,
            "workspace": "/tmp",
            "model": "test",
            "created_at": 100.0,
            "updated_at": 200.0,
            "pinned": False,
            "archived": False,
            "parent_session_id": "lineage_parent",
            "messages": [{"role": "user", "content": "legacy"}],
            "tool_calls": [],
        }
        # Deliberately bypass Session.save(): pre-fix legacy sidecars do not have
        # a persisted message_count field in their metadata prefix.
        (models.SESSION_DIR / f"{sid}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        rows.append({
            "session_id": sid,
            "title": sid,
            "workspace": "/tmp",
            "model": "test",
            "created_at": 100.0,
            "updated_at": 100.0,
            "last_message_at": 100.0,
            "message_count": count,
            "pinned": False,
            "archived": False,
            "parent_session_id": "lineage_parent",
        })
    _write_index_file(index_file, rows)

    # The index is parsed from raw bytes (json.loads decodes UTF-8 in one pass),
    # so count read_bytes rather than read_text.
    original_read_bytes = Path.read_bytes
    index_reads = 0

    def _counting_read_bytes(self, *args, **kwargs):
        nonlocal index_reads
        if self == index_file:
            index_reads += 1
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _counting_read_bytes)
    monkeypatch.setattr(models, "_enrich_sidebar_lineage_metadata", lambda _sessions: None)

    result = models.all_sessions()

    counts = {row["session_id"]: row["message_count"] for row in result}
    assert counts["legacy_lineage_a"] == 3
    assert counts["legacy_lineage_b"] == 4
    assert index_reads == 1


def test_session_save_does_not_persist_metadata_message_count_hint():
    s = Session(
        session_id="sess_private_hint",
        title="Private Hint",
        messages=[{"role": "user", "content": "hi"}],
    )
    s._metadata_message_count = 10
    s.save(skip_index=True)

    payload = json.loads(s.path.read_text(encoding="utf-8"))
    assert "_metadata_message_count" not in payload


# ── 8. test_first_call_full_rebuild ──────────────────────────────────────

def test_first_call_full_rebuild():
    """When no index file exists, calling _write_session_index(updates=[session])
    should fall back to full rebuild and create the index.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # No index file yet
    assert not index_file.exists()

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Call with updates — should trigger full rebuild since index doesn't exist
    _write_session_index(updates=[sA])

    # Index should now exist
    assert index_file.exists(), "Index file should be created"

    index = _read_index(index_file)
    ids = {e["session_id"] for e in index}
    assert "sess_a" in ids, "Session A should appear in the rebuilt index"


# ── 9. test_corrupt_index_fallback ────────────────────────────────────────

def test_corrupt_index_fallback():
    """Write garbage/invalid JSON to SESSION_INDEX_FILE. Call
    _write_session_index(updates=[session]). Verify it falls back to
    full rebuild and the result is valid JSON with correct entries.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # Write corrupt data
    index_file.write_text("THIS IS NOT JSON {{{", encoding="utf-8")

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Should not raise; should fall back to full rebuild
    _write_session_index(updates=[sA])

    # Index should now be valid JSON
    assert index_file.exists()
    index = _read_index(index_file)
    assert isinstance(index, list), "Index should be a list"

    ids = {e["session_id"] for e in index}
    assert "sess_a" in ids, "Session A should appear after fallback rebuild"


# ── 10. test_concurrent_saves_dont_lose_data ────────────────────────────

def test_concurrent_saves_dont_lose_data():
    """Create 2 threads, each calling Session.save() on different sessions
    with a pre-existing index. Use a threading.Event barrier to force them
    to run concurrently. Assert both updates are present in the final index.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sB = _make_session("sess_b", "Bravo", updated_at=200.0)

    for s in (sA, sB):
        s.path.write_text(json.dumps(s.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Build initial index
    _write_session_index(updates=None)

    # Now update both sessions concurrently
    barrier = threading.Event()
    errors = []

    def _update_session(session, new_title, new_updated_at):
        try:
            barrier.wait(timeout=5)
            session.title = new_title
            session.updated_at = new_updated_at
            session.save()
        except Exception as e:
            errors.append(e)

    sA.title = "Alpha V2"
    sA.updated_at = 150.0
    sB.title = "Bravo V2"
    sB.updated_at = 250.0

    t1 = threading.Thread(target=_update_session, args=(sA, "Alpha V2", 150.0))
    t2 = threading.Thread(target=_update_session, args=(sB, "Bravo V2", 250.0))

    t1.start()
    t2.start()

    # Release both threads simultaneously
    barrier.set()

    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Errors during concurrent saves: {errors}"

    # Verify both updates are in the final index
    index = _read_index(index_file)
    index_map = {e["session_id"]: e for e in index}

    assert "sess_a" in index_map, "Session A should be in index"
    assert "sess_b" in index_map, "Session B should be in index"
    assert index_map["sess_a"]["title"] == "Alpha V2", "Session A title should be updated"
    assert index_map["sess_b"]["title"] == "Bravo V2", "Session B title should be updated"


# ── 11. test_atomic_write_no_tmp_remains ─────────────────────────────────

def test_atomic_write_no_tmp_remains():
    """After _write_session_index completes, no .tmp file should remain
    in SESSION_DIR.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_session_index(updates=[sA])

    # Check for any .tmp files in SESSION_DIR
    tmp_files = list(session_dir.glob("*.tmp"))
    assert len(tmp_files) == 0, f"Unexpected .tmp files remain: {tmp_files}"

    # Also test incremental path
    sA.title = "Alpha V2"
    sA.updated_at = 200.0
    _write_session_index(updates=[sA])

    tmp_files = list(session_dir.glob("*.tmp"))
    assert len(tmp_files) == 0, f"Unexpected .tmp files after incremental write: {tmp_files}"


# ── 12. test_deadlock_guard_on_fallback ──────────────────────────────────

def test_deadlock_guard_on_fallback():
    """Mock the index file read to raise an exception, then verify
    _write_session_index(updates=[session]) completes without hanging.

    This tests that the fallback path (corrupt index -> full rebuild)
    is called outside the LOCK, so it doesn't deadlock.
    """
    session_dir = models.SESSION_DIR
    index_file = models.SESSION_INDEX_FILE

    # Create a valid index file so the incremental path is attempted
    _write_index_file(index_file, [
        {"session_id": "sess_a", "title": "Alpha", "updated_at": 100.0,
         "workspace": "/tmp", "model": "test", "message_count": 0,
         "created_at": 100.0, "pinned": False, "archived": False},
    ])

    sB = _make_session("sess_b", "Bravo", updated_at=200.0)
    sB.path.write_text(json.dumps(sB.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    # Make the index file read raise an exception to trigger fallback
    original_read_text = Path.read_text
    call_count = 0

    def _broken_read_text(self, *args, **kwargs):
        nonlocal call_count
        # Only break the index file read, not the session file reads
        if str(self) == str(index_file) and call_count == 0:
            call_count += 1
            raise OSError("Simulated corrupt index read")
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", _broken_read_text):
        # This should complete without hanging (deadlock guard)
        # Use a timeout to detect deadlock
        done = threading.Event()
        result = [None]
        exc = [None]

        def _run():
            try:
                _write_session_index(updates=[sB])
                result[0] = "done"
            except Exception as e:
                exc[0] = e
            finally:
                done.set()

        t = threading.Thread(target=_run)
        t.start()
        finished = done.wait(timeout=10)

        assert finished, "_write_session_index hung — likely deadlock in fallback path"
        assert exc[0] is None, f"Unexpected exception: {exc[0]}"

    # The index should still be valid after fallback
    index = _read_index(index_file)
    assert isinstance(index, list)


def test_incremental_index_disk_io_runs_outside_lock(monkeypatch):
    """Fast-path disk I/O (fsync/replace) must run after releasing LOCK."""
    index_file = models.SESSION_INDEX_FILE

    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_session_index(updates=None)  # seed index

    sA.title = "Alpha V2"
    sA.updated_at = 200.0

    fsync_lock_states = []
    original_fsync = models.os.fsync

    def _observing_fsync(fd):
        fsync_lock_states.append(models.LOCK.locked())
        return original_fsync(fd)

    monkeypatch.setattr(models.os, "fsync", _observing_fsync)

    _write_session_index(updates=[sA])

    assert fsync_lock_states, "Expected at least one fsync call during index write"
    assert not any(fsync_lock_states), (
        "_write_session_index fast path must not hold LOCK during fsync/disk I/O"
    )


def test_full_rebuild_index_disk_io_runs_outside_lock(monkeypatch):
    """Full-rebuild disk I/O (fsync/replace) must run after releasing LOCK."""
    sA = _make_session("sess_a", "Alpha", updated_at=100.0)
    sA.path.write_text(json.dumps(sA.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    fsync_lock_states = []
    original_fsync = models.os.fsync

    def _observing_fsync(fd):
        fsync_lock_states.append(models.LOCK.locked())
        return original_fsync(fd)

    monkeypatch.setattr(models.os, "fsync", _observing_fsync)

    _write_session_index(updates=None)

    assert fsync_lock_states, "Expected at least one fsync call during index write"
    assert not any(fsync_lock_states), (
        "_write_session_index full rebuild must not hold LOCK during fsync/disk I/O"
    )


def test_all_sessions_ignores_stale_index_entries():
    """Reading via all_sessions() must not surface ghost rows from _index.json."""
    index_file = models.SESSION_INDEX_FILE

    valid_session = _make_session("sess_a", "Alpha", updated_at=200.0)
    valid_session.path.write_text(
        json.dumps(valid_session.__dict__, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    valid = valid_session.compact()
    stale = {
        "session_id": "ghost_sid",
        "title": "Ghost",
        "updated_at": 150.0,
        "workspace": "/tmp",
        "model": "test",
        "message_count": 1,
        "created_at": 100.0,
        "pinned": False,
        "archived": False,
    }
    _write_index_file(index_file, [stale, valid])

    rows = models.all_sessions()
    ids = {e["session_id"] for e in rows}
    assert "sess_a" in ids
    assert "ghost_sid" not in ids


def test_background_index_rebuild_skips_after_session_dir_switch(tmp_path, monkeypatch):
    """A delayed rebuild thread must not write into a newer isolated session dir."""
    original_session_dir = models.SESSION_DIR
    original_index_file = models.SESSION_INDEX_FILE
    new_session_dir = tmp_path / "other-sessions"
    new_session_dir.mkdir()
    new_index_file = new_session_dir / "_index.json"

    monkeypatch.setattr(models, "_SESSION_INDEX_REBUILD_THREAD", object())
    monkeypatch.setattr(models, "_SESSION_INDEX_REBUILD_THREAD_TARGET", (
        original_session_dir,
        original_index_file,
    ))
    monkeypatch.setattr(models, "SESSION_DIR", new_session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", new_index_file)

    models._rebuild_session_index_background(
        original_session_dir,
        original_index_file,
    )

    assert not new_index_file.exists()
    monkeypatch.setattr(models, "SESSION_DIR", original_session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", original_index_file)
    session = _make_session("late_switch_sid", "Late switch", updated_at=100.0)
    session.save(skip_index=True)

    original_write_session_index = models._write_session_index

    def _switch_globals_then_write(*args, **kwargs):
        monkeypatch.setattr(models, "SESSION_DIR", new_session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", new_index_file)
        return original_write_session_index(*args, **kwargs)

    monkeypatch.setattr(models, "_write_session_index", _switch_globals_then_write)

    models._rebuild_session_index_background(
        original_session_dir,
        original_index_file,
    )

    assert original_index_file.exists()
    assert not new_index_file.exists()
    rows = _read_index(original_index_file)
    assert [row["session_id"] for row in rows] == ["late_switch_sid"]


def test_background_rebuild_old_thread_finally_preserves_new_same_target_owner(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(exist_ok=True)
    index_file = session_dir / "_index.json"
    target = (session_dir, index_file)

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    old_thread = object()
    new_thread = object()
    monkeypatch.setattr(models, "_SESSION_INDEX_REBUILD_THREAD", old_thread)
    monkeypatch.setattr(models, "_SESSION_INDEX_REBUILD_THREAD_TARGET", target)
    monkeypatch.setattr(models.threading, "current_thread", lambda: old_thread)

    def _handoff_then_write(*args, **kwargs):
        monkeypatch.setattr(models, "_SESSION_INDEX_REBUILD_THREAD", new_thread)
        monkeypatch.setattr(models, "_SESSION_INDEX_REBUILD_THREAD_TARGET", target)

    monkeypatch.setattr(models, "_write_session_index", _handoff_then_write)

    models._rebuild_session_index_background(*target)

    assert models._SESSION_INDEX_REBUILD_THREAD is new_thread
    assert models._SESSION_INDEX_REBUILD_THREAD_TARGET == target
