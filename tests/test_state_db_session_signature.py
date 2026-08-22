"""Coverage for the commit-reliable state.db display-cache signature.

The display-merge cache key used to embed ``_state_db_rows_fingerprint``, which
serialises every state.db display row with ``json.dumps``. On a 36k-row session
that cost ~1.4s *per request*, which became the floor cost of even a cache HIT --
the cache could never pay for itself.

``_state_db_session_signature`` now reuses the project's DB/WAL/SHM commit key
outside streams and switches to an exact target-session digest while another
turn streams. These tests pin the properties that matter: every committed
target mutation moves the signature; unrelated stream deltas remain stable;
and failures return None so the caller falls back to the exact row fingerprint.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import routes  # noqa: E402

SID = "20260101_120000_abcdef"


def _build_db(path, rows=None):
    """Minimal messages table matching the columns the signature reads."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE messages ("
            "  id INTEGER PRIMARY KEY,"
            "  session_id TEXT,"
            "  role TEXT,"
            "  content TEXT,"
            "  timestamp REAL)"
        )
        conn.execute("CREATE INDEX idx_ms ON messages(session_id, id)")
        payload = rows if rows is not None else [
            (i, SID, "user" if i % 2 else "assistant",
             f"message number {i} with enough text to slice into thirds",
             1700000000.0 + i)
            for i in range(1, 41)
        ]
        conn.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp) "
            "VALUES (?,?,?,?,?)", payload)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = _build_db(tmp_path / "state.db")
    monkeypatch.setattr(
        "api.models._agent_state_db_path", lambda profile=None: path, raising=False)
    monkeypatch.setattr(
        "api.models._cli_sessions_streaming_freeze_marker", lambda: None, raising=False)
    return path


def _sig(db_path):
    return routes._state_db_session_signature(SID, None)


def _mutate(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Detection matrix: every one of these must change the signature, otherwise a
# stale transcript can be served from the display-merge cache.
# --------------------------------------------------------------------------

MUTATIONS = [
    pytest.param(
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES (?, 'user', 'brand new row', 1700009999.0)", (SID,),
        id="append_row"),
    pytest.param(
        "DELETE FROM messages WHERE rowid = "
        "(SELECT MAX(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="delete_tail_row"),
    pytest.param(
        "DELETE FROM messages WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="delete_head_row"),
    pytest.param(
        "UPDATE messages SET content = 'short' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="edit_content_different_length"),
    pytest.param(
        "UPDATE messages SET timestamp = 1.0 WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="edit_timestamp"),
    pytest.param(
        "UPDATE messages SET role = 'tool' WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="edit_role_only"),
    pytest.param(
        "UPDATE messages SET content = 'Z' || SUBSTR(content, 2) WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="edit_first_char_same_length"),
    pytest.param(
        "UPDATE messages SET content = "
        "SUBSTR(content,1,LENGTH(content)/2-1) || 'Z' || SUBSTR(content,LENGTH(content)/2+1) "
        "WHERE rowid = (SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="edit_middle_char_same_length"),
    pytest.param(
        "UPDATE messages SET content = "
        "SUBSTR(content,1,LENGTH(content)/3-1) || 'Z' || SUBSTR(content,LENGTH(content)/3+1) "
        "WHERE rowid = (SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="edit_third_char_same_length"),
    pytest.param(
        "UPDATE messages SET content = "
        "SUBSTR(content,1,LENGTH(content)-2) || 'Z' || SUBSTR(content,LENGTH(content)) "
        "WHERE rowid = (SELECT MIN(rowid) FROM messages WHERE session_id = ?)", (SID,),
        id="edit_tail_char_same_length"),
]


@pytest.mark.parametrize("sql,params", MUTATIONS)
def test_signature_detects_mutation(db, sql, params):
    """A stale cache is only safe if the key moves on every visible mutation."""
    before = _sig(db)
    assert before is not None
    _mutate(db, sql, params)
    after = _sig(db)
    assert after is not None
    assert after != before, "signature did not move -> stale transcript risk"


def test_signature_is_stable_without_mutation(db):
    """No spurious invalidation: repeated reads of an unchanged DB must match."""
    first = _sig(db)
    assert first is not None
    for _ in range(3):
        assert _sig(db) == first


def test_streaming_signature_is_scoped_to_target_session(db, monkeypatch):
    """Unrelated stream writes stay stable, but target edits invalidate immediately."""
    marker = ("streaming", ("active-run-1",))
    monkeypatch.setattr(
        "api.models._cli_sessions_streaming_freeze_marker",
        lambda: marker,
        raising=False,
    )
    first = _sig(db)
    assert first is not None

    _mutate(
        db,
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('20260202_000000_other', 'user', 'unrelated delta', 1700001111.0)",
    )
    assert _sig(db) == first

    _mutate(
        db,
        "UPDATE messages SET content = 'Z' || SUBSTR(content, 2) WHERE rowid = "
        "(SELECT MIN(rowid) FROM messages WHERE session_id = ?)",
        (SID,),
    )
    assert _sig(db) != first, "target session edit was hidden by the global stream marker"


def test_signature_invalidates_on_other_session_write(db):
    """The DB-wide key favors correctness over per-session cache locality.

    A write to another session intentionally invalidates this session's cache.
    This false invalidation is safe; missing a write to an optional column or a
    continuation parent would not be.
    """
    before = _sig(db)
    _mutate(
        db,
        "INSERT INTO messages (session_id, role, content, timestamp) "
        "VALUES ('20260202_000000_other', 'user', 'unrelated', 1700001111.0)")
    assert _sig(db) != before


# --------------------------------------------------------------------------
# Fail-closed behaviour: the caller must be able to fall back safely.
# --------------------------------------------------------------------------

def test_signature_returns_none_for_unsafe_session_id(db):
    assert routes._state_db_session_signature("../../etc/passwd", None) is None
    assert routes._state_db_session_signature("", None) is None
    assert routes._state_db_session_signature(None, None) is None


def test_signature_returns_none_when_db_missing(tmp_path, monkeypatch):
    missing = tmp_path / "absent.db"
    monkeypatch.setattr(
        "api.models._agent_state_db_path", lambda profile=None: missing, raising=False)
    monkeypatch.setattr(
        "api.models._cli_sessions_streaming_freeze_marker",
        lambda: ("streaming", ("other-active-run",)),
        raising=False,
    )
    assert routes._state_db_session_signature(SID, None) is None


def test_signature_uses_file_fallback_on_incomplete_schema(tmp_path, monkeypatch):
    """File stamps remain a valid key when content summaries are unavailable."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        "api.models._agent_state_db_path", lambda profile=None: path, raising=False)
    first = routes._state_db_session_signature(SID, None)
    assert first is not None
    assert routes._state_db_session_signature(SID, None) == first


def test_signature_returns_none_when_all_fingerprints_fail(db, monkeypatch):
    monkeypatch.setattr(
        "api.models._sqlite_file_stat_cache_key", lambda path: None, raising=False)
    assert routes._state_db_session_signature(SID, None) is None


def test_signature_returns_none_when_resolver_raises(monkeypatch):
    def boom(profile=None):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr("api.models._agent_state_db_path", boom, raising=False)
    assert routes._state_db_session_signature(SID, None) is None


def test_cache_key_falls_back_to_exact_fingerprint(tmp_path, monkeypatch):
    """When the bounded signature is unavailable, the key must still be built.

    This is the fail-closed contract: degraded to the old (slow but exact)
    fingerprint rather than disabling the cache or trusting a partial key.
    """
    monkeypatch.setattr(
        routes, "_state_db_session_signature", lambda *a, **k: None)

    calls = []
    real = routes._state_db_rows_fingerprint

    def spy(rows):
        calls.append(len(rows or []))
        return real(rows)

    monkeypatch.setattr(routes, "_state_db_rows_fingerprint", spy)

    class _S:
        session_id = SID
        profile = None
        truncation_watermark = None
        truncation_boundary = None

    monkeypatch.setattr(
        "api.models._sidecar_stat_signature", lambda p: ("sig", 1, 2, 3), raising=False)

    rows = [{"role": "user", "content": "x", "timestamp": 1.0}]
    key = routes._display_merge_cache_key(_S(), [{"timestamp": 1.0}], rows)

    assert calls, "exact fingerprint was not used as fallback"
    assert key is not None
