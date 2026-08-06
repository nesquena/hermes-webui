"""Regression coverage for index-only ghost session cleanup (#5331).

#5331: sessions created in ``_index.json`` without a backing ``.json`` sidecar
file accumulate in the sidebar and cannot be removed via normal UI. The existing
``_handle_sessions_cleanup`` only iterates ``SESSION_DIR.glob('*.json')`` —
structurally blind to index-only entries.

Phase 2 (new) reads the index and prunes entries that have no backing file
and no in-memory session.  A legitimate session always has either a file on
disk or an in-memory ``SESSIONS`` entry (or both).
"""

from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@pytest.fixture
def mock_env(tmp_path, monkeypatch):
    """Set up an isolated SESSION_DIR with monkeypatched globals.

    Patches at the ``api.config`` level so that ``api.routes`` AND
    ``api.models`` (used by ``Session.load`` in Phase 1) both read
    the test temp paths.
    """
    import api.config as config_mod
    import api.routes as routes
    import api.models as models

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    index_file = sessions_dir / "_index.json"

    # Patch at the config source so all importers see the same test paths.
    monkeypatch.setattr(config_mod, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(config_mod, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config_mod, "SESSIONS", {})
    monkeypatch.setattr(routes, "LOCK", threading.Lock())
    # Also refresh the module-level aliases in routes and models so they
    # pick up the patched config values.
    monkeypatch.setattr(routes, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSIONS", {})
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    return sessions_dir, index_file


def _fake_handler():
    """Minimal handler mock for ``_handle_sessions_cleanup``.

    ``j()`` writes the JSON response to ``handler.wfile`` (a ``BytesIO``),
    so callers read the result via ``handler.wfile.getvalue()``.
    """
    handler = type("FakeHandler", (), {})()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda status: None
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    return handler


def _fake_handler_result(handler):
    """Parse the JSON written to ``handler.wfile`` by ``j()``."""
    return json.loads(handler.wfile.getvalue())


def _make_session_file(sessions_dir, sid, title="Untitled", **extra):
    """Write a ``.json`` session file and return the compact entry dict.

    Uses the minimal Session-serializable format: ``session_id`` is required,
    all other fields have defaults.  ``**extra`` is folded directly into
    the JSON so callers can set ``message_count``, ``created_at``, etc.
    """
    entry = {"session_id": sid, "title": title, **extra}
    (sessions_dir / f"{sid}.json").write_text(json.dumps(entry))
    return entry


def _write_index(index_file, entries):
    """Write the session index file."""
    index_file.write_text(json.dumps(entries, indent=2))


def _read_index(index_file):
    """Read the session index file, returning empty list on missing/corrupt."""
    if not index_file.exists():
        return []
    try:
        return json.loads(index_file.read_text(encoding="utf-8"))
    except Exception:
        return []


# ── Phase 2: index-only ghost sweep (#5331) ────────────────────────────────


def test_cleanup_prunes_index_only_ghosts(mock_env):
    """Index-only Untitled entries with no backing file are pruned."""
    sessions_dir, index_file = mock_env

    # Legitimate sessions with backing files — should survive.
    _make_session_file(sessions_dir, "sess-a", "Legit")
    _make_session_file(sessions_dir, "sess-b", "Production")

    # Ghost entry: exists only in index, no backing file.
    _write_index(index_file, [
        {"session_id": "sess-a", "title": "Legit", "message_count": 5},
        {"session_id": "sess-b", "title": "Production", "message_count": 42},
        {"session_id": "sess-ghost-1", "title": "Untitled", "message_count": 3},
    ])

    import api.routes as routes
    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)

    assert result["ok"] is True
    assert result["cleaned"] == 1  # only the ghost

    updated = _read_index(index_file)
    sids = {e["session_id"] for e in updated}
    assert "sess-ghost-1" not in sids
    assert "sess-a" in sids
    assert "sess-b" in sids


def test_cleanup_keeps_in_memory_ghosts(mock_env):
    """Index-only entries still in memory (SESSIONS) are kept."""
    sessions_dir, index_file = mock_env
    import api.routes as routes

    # One entry in-memory but no backing file — should survive Phase 2.
    routes.SESSIONS["sess-mem"] = "placeholder"
    _write_index(index_file, [
        {"session_id": "sess-mem", "title": "Untitled", "message_count": 2},
    ])

    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)
    assert result["cleaned"] == 0

    updated = _read_index(index_file)
    assert any(e["session_id"] == "sess-mem" for e in updated)


def test_cleanup_mixed_ghosts_and_legit(mock_env):
    """Mix of ghost, file-backed, and in-memory entries — only ghosts pruned."""
    sessions_dir, index_file = mock_env
    import api.routes as routes

    # File-backed (survive Phase 1 — not Untitled, or has messages)
    _make_session_file(sessions_dir, "sess-1", "Chat")
    _make_session_file(sessions_dir, "sess-2", "Chat2")

    # In-memory (survive)
    routes.SESSIONS["sess-3"] = "placeholder"

    # Ghosts (pruned)
    ghost_1 = {"session_id": "sess-ghost-a", "title": "Untitled", "message_count": 5}
    ghost_2 = {"session_id": "sess-ghost-b", "title": "Something", "message_count": 0}

    _write_index(index_file, [
        {"session_id": "sess-1", "title": "Chat", "message_count": 10},
        {"session_id": "sess-2", "title": "Untitled", "message_count": 1},
        {"session_id": "sess-3", "title": "Untitled", "message_count": 2},
        ghost_1,
        ghost_2,
    ])

    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)
    assert result["cleaned"] == 2

    updated = _read_index(index_file)
    sids = {e["session_id"] for e in updated}
    assert "sess-ghost-a" not in sids
    assert "sess-ghost-b" not in sids
    assert "sess-1" in sids
    assert "sess-2" in sids
    assert "sess-3" in sids


def test_cleanup_empty_index_does_not_crash(mock_env):
    """An index with no entries leaves Phase 2 a no-op."""
    sessions_dir, index_file = mock_env
    _write_index(index_file, [])

    import api.routes as routes
    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)
    assert result["cleaned"] == 0


def test_cleanup_no_index_file_does_not_crash(mock_env):
    """No index file at all — Phase 2 is skipped, no error."""
    sessions_dir, index_file = mock_env
    assert not index_file.exists()

    import api.routes as routes
    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)
    assert result["cleaned"] == 0


def test_cleanup_corrupt_index_does_not_crash(mock_env):
    """A corrupt index file is caught by the try/except in Phase 2."""
    sessions_dir, index_file = mock_env
    index_file.write_text("not valid json")

    import api.routes as routes
    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)
    assert result["cleaned"] == 0  # Phase 2 gracefully skipped

    # Index file still exists and still invalid.
    assert index_file.exists()
    assert index_file.read_text() == "not valid json"


def test_cleanup_ghost_without_session_id_field(mock_env):
    """An index entry missing ``session_id`` is skipped (not treated as ghost)."""
    sessions_dir, index_file = mock_env
    _make_session_file(sessions_dir, "sess-real", "Real")

    _write_index(index_file, [
        {"session_id": "sess-real", "title": "Real", "message_count": 1},
        {"title": "NoId", "message_count": 3},  # missing session_id
    ])

    import api.routes as routes
    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)
    assert result["cleaned"] == 0

    updated = _read_index(index_file)
    assert len(updated) == 2  # both kept


def test_cleanup_index_only_empty_ghosts_without_explicit_messages(mock_env):
    """Index-only ghost with no ``messages`` field is still pruned.

    The minimal index entry ``{session_id, title}`` works — Phase 2 does not
    try to load the session (there's no file), so it just checks sid presence
    in ``live_ids`` and ``in_memory_ids``.
    """
    sessions_dir, index_file = mock_env
    _make_session_file(sessions_dir, "sess-real", "Real")

    _write_index(index_file, [
        {"session_id": "sess-real", "title": "Real", "message_count": 1},
        {"session_id": "sess-ghost-minimal"},
    ])

    import api.routes as routes
    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)
    assert result["cleaned"] == 1

    updated = _read_index(index_file)
    assert len(updated) == 1
    assert updated[0]["session_id"] == "sess-real"


# ── Phase 3: index cache thrash fix ──────────────────────────────────────────


def test_cleanup_deletes_index_only_when_phase1_touched(mock_env):
    """Phase 3 only unlinks _index.json when Phase 1 removed files.

    Phase 2 always writes the index in-place.  The post-cleanup
    ``unlink(missing_ok=True)`` should only fire when Phase 1 removed
    actual session files, because that's the only case where the index
    was NOT already written in-place by Phase 2.
    """
    sessions_dir, index_file = mock_env
    import api.routes as routes

    # Only ghosts in index — Phase 1 touches nothing, Phase 2 writes in-place.
    _write_index(index_file, [
        {"session_id": "sess-ghost", "title": "Untitled", "message_count": 1},
    ])

    routes._handle_sessions_cleanup(_fake_handler(), {})

    # After Phase-2-write, index should exist (Phase 3 should not have unlinked it).
    assert index_file.exists()

    # Verify it was cleaned too
    updated = _read_index(index_file)
    assert len(updated) == 0


def test_cleanup_index_rewritten_when_phase1_removed_files(mock_env):
    """Phase 2 rewrites the index when Phase 1 removes files.

    Phase 2 handles the stale index entries left by Phase 1 removals,
    so Phase 3 does NOT delete the index — it's already clean.
    """
    sessions_dir, index_file = mock_env
    import api.routes as routes

    # A file-backed Untitled-0-message session — Phase 1 target.
    _make_session_file(sessions_dir, "sess-orphan", "Untitled")
    _write_index(index_file, [
        {"session_id": "sess-orphan", "title": "Untitled", "message_count": 0},
    ])

    routes._handle_sessions_cleanup(_fake_handler(), {}, zero_only=False)

    # Phase 1 unlinked the file.  Phase 2 removed the stale index entry.
    # Phase 3 skipped because Phase 2 already cleaned the index.
    # Index exists with zero entries (clean).
    assert index_file.exists()
    assert _read_index(index_file) == []


def test_cleanup_phase3_deletes_when_phase2_could_not_run(mock_env):
    """Phase 3 still deletes the index when Phase 2 couldn't run.

    When the index is corrupt (or missing), Phase 2 gracefully skips.
    Phase 1 has removed files, leaving no way to clean the stale index,
    so Phase 3 deletes it for a fresh rebuild.
    """
    sessions_dir, index_file = mock_env
    import api.routes as routes

    # Phase 1 target: file-backed session with zero messages.
    _make_session_file(sessions_dir, "sess-orphan", "Untitled")
    # Corrupt index — Phase 2 will catch the exception and skip.
    index_file.write_text("corrupt json")

    routes._handle_sessions_cleanup(_fake_handler(), {}, zero_only=False)

    # Phase 1 removed the file.  Phase 2 caught the corrupt-json exception.
    # Phase 3: phase1_touched=True, phase2_rewrote_index=False → unlink.
    assert not index_file.exists()


def test_cleanup_no_double_count_when_phase1_and_phase2_overlap(mock_env):
    """Phase 2 does not double-count sessions already removed by Phase 1.

    When Phase 1 removes a file-backed session, the corresponding index
    entry becomes stale.  Phase 2 sees it (no file, not in memory) but
    recognises it as a Phase-1 removal via ``phase1_removed_ids`` and
    skips counting it — preventing ``cleaned`` from inflating.
    """
    sessions_dir, index_file = mock_env
    import api.routes as routes

    # Phase 1 target: file-backed session with zero messages.
    _make_session_file(sessions_dir, "sess-zero", "Untitled")
    # Phase 2 target: index-only ghost.
    _write_index(index_file, [
        {"session_id": "sess-zero", "title": "Untitled", "message_count": 0},
        {"session_id": "sess-ghost", "title": "Untitled", "message_count": 1},
    ])

    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {})
    result = _fake_handler_result(handler)

    # Phase 1: 1 (sess-zero).  Phase 2: 1 (sess-ghost).  Total = 2.
    assert result["cleaned"] == 2

    # Index exists (Phase 2 rewrote it, Phase 3 skipped).
    assert index_file.exists()
    assert _read_index(index_file) == []


# ── zero_only mode preserves Phase 2 ─────────────────────────────────────────


def test_cleanup_zero_only_still_runs_phase2(mock_env):
    """Phase 2 (ghost sweep) runs regardless of ``zero_only`` flag.

    Phase 2 handles the stale index entry from Phase 1's removal, so the
    index survives in a clean state (empty) and no double-counting occurs.
    """
    sessions_dir, index_file = mock_env

    # Phase-1 target: a real file with zero messages
    _make_session_file(sessions_dir, "sess-zero", "SomeTitle")

    # Ghost in index only
    _write_index(index_file, [
        {"session_id": "sess-zero", "title": "SomeTitle", "message_count": 0},
        {"session_id": "sess-ghost", "title": "Untitled", "message_count": 1},
    ])

    import api.routes as routes
    handler = _fake_handler()
    routes._handle_sessions_cleanup(handler, {}, zero_only=True)
    result = _fake_handler_result(handler)

    # Phase 1: 1 (sess-zero, zero messages).  Phase 2: 1 (sess-ghost).  Total = 2.
    assert result["ok"] is True
    assert result["cleaned"] == 2

    # Index exists (Phase 2 rewrote it, Phase 3 skipped).
    assert index_file.exists()
    assert _read_index(index_file) == []
