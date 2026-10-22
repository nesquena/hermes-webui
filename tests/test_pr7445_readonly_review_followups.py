"""Review follow-ups for the strictly read-only ``state.db`` readers (PR #7445).

Contract under test:

* every missing-index projection (default sidebar, webhook, kanban, …), not
  only the cron view, orders its candidate window through the read-only
  pre-aggregated ``latest_messages`` CTE — never through the correlated
  ``MAX(mx.timestamp)`` scalar subquery that rescans ``messages`` per session;
  the selected rows and their order stay identical to the indexed listing;
* ``state_db_readonly_uri`` builds a ``file:`` URI SQLite accepts on every
  supported platform: POSIX unchanged, Windows drive letters as
  ``file:///C:/...``, UNC shares with an *empty* authority
  (``file:////server/share/...``) because CPython's bundled SQLite rejects any
  non-local authority, extended-length ``\\\\?\\`` prefixes stripped; the
  query string is always the strict ``mode=ro``;
* ``scripts/ensure_state_db_read_indexes.py`` imports and runs its no-lock path
  on a platform without ``fcntl`` (native Windows), and refuses ``--lock-file``
  there with a clear error instead of an ``ImportError`` at import time;
* on the ``msvcrt`` branch the tool locks byte 0 of the lock file only after
  making sure that byte exists, so a fresh (empty) ``--lock-file`` is acquired
  without relying on the CRT's beyond-EOF locking behaviour; that byte is
  written as exactly one byte even under the ``\\n`` → ``\\r\\n`` translation
  native Windows applies to text-mode handles.
"""
import importlib
import os
import sqlite3
import sys
from contextlib import closing

import pytest

import api.agent_sessions as agent_sessions


# --------------------------------------------------------------------------
# 1. Missing-index projections use the pre-aggregated candidate ordering.
# --------------------------------------------------------------------------

def _make_state_db(path, *, sessions, source, with_index):
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions(
                id TEXT PRIMARY KEY, title TEXT, model TEXT, message_count INTEGER,
                started_at REAL, source TEXT, session_source TEXT,
                parent_session_id TEXT, ended_at REAL, end_reason TEXT);
            CREATE TABLE messages(
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                content TEXT, timestamp REAL);
            """
        )
        for i in range(sessions):
            sid = f"{source}-{i:04d}"
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (sid, f"Session {i}", "m", 3, 1000.0 + i, source, source),
            )
            for j in range(3):
                conn.execute(
                    "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (sid, "user" if j == 0 else "assistant", "x", 1000.0 + i + j / 10),
                )
        # A resumed old session: started first, but its last message is newest.
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)",
            (f"{source}-resumed", "Resumed", "m", 2, 1.0, source, source),
        )
        conn.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
            (f"{source}-resumed", "user", "old", 1.0),
        )
        conn.execute(
            "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
            (f"{source}-resumed", "assistant", "recent", 5000.0),
        )
        if with_index:
            conn.execute("CREATE INDEX idx_messages_session ON messages(session_id, timestamp)")
        conn.commit()


def _capture_listing_sql(monkeypatch):
    """Record the SQL statements the listing runs (trace callback, read-only)."""
    statements = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", tracing_connect)
    return statements


def _candidate_query_plan(db_path, statements):
    listing_sql = [s for s in statements if "FROM sessions s" in s]
    assert len(listing_sql) == 1, listing_sql
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + listing_sql[0])]


@pytest.mark.parametrize(
    ("source", "kwargs"),
    [
        ("cli", {}),  # default /api/sessions projection
        ("webhook", {"exclude_sources": None, "include_sources": ("webhook",)}),
        ("kanban", {"exclude_sources": None, "include_sources": ("kanban",)}),
        ("cli", {"exclude_sources": None}),  # gateway watcher / diagnostic view
    ],
)
def test_missing_index_projection_uses_preaggregated_ordering(tmp_path, monkeypatch, source, kwargs):
    indexed = tmp_path / "indexed.db"
    unindexed = tmp_path / "unindexed.db"
    _make_state_db(indexed, sessions=60, source=source, with_index=True)
    _make_state_db(unindexed, sessions=60, source=source, with_index=False)

    expected = agent_sessions.read_importable_agent_session_rows(indexed, limit=20, **kwargs)
    assert expected[0]["id"] == f"{source}-resumed"
    assert len(expected) == 20

    statements = _capture_listing_sql(monkeypatch)
    actual = agent_sessions.read_importable_agent_session_rows(unindexed, limit=20, **kwargs)

    # Same rows, same order as the indexed listing.
    assert [r["id"] for r in actual] == [r["id"] for r in expected]
    assert [r["actual_message_count"] for r in actual] == [r["actual_message_count"] for r in expected]
    assert [r["last_activity"] for r in actual] == [r["last_activity"] for r in expected]

    plan = _candidate_query_plan(unindexed, statements)
    # One pre-aggregation pass over messages, never a per-candidate rescan.
    assert not any("CORRELATED SCALAR SUBQUERY" in step for step in plan), plan
    assert any("latest_messages" in step for step in plan), plan
    # The listing stayed a pure read: no index appeared.
    with closing(sqlite3.connect(str(unindexed))) as conn:
        assert conn.execute("PRAGMA index_list(messages)").fetchall() == []


def test_indexed_projection_keeps_the_correlated_index_seek(tmp_path, monkeypatch):
    """With ``idx_messages_session`` present the indexed seek stays in place."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=10, source="cli", with_index=True)
    statements = _capture_listing_sql(monkeypatch)
    rows = agent_sessions.read_importable_agent_session_rows(db, limit=20)
    assert rows[0]["id"] == "cli-resumed"
    plan = _candidate_query_plan(db, statements)
    assert not any("latest_messages" in step for step in plan), plan
    assert any("idx_messages_session" in step for step in plan), plan


# --------------------------------------------------------------------------
# 2. Read-only URI construction is platform-aware and strictly mode=ro.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("platform", "path", "expected"),
    [
        ("linux", "/home/me/.hermes/state.db", "file:///home/me/.hermes/state.db?mode=ro"),
        ("darwin", "/Users/me/state dir #1/state?.db",
         "file:///Users/me/state%20dir%20%231/state%3F.db?mode=ro"),
        ("win32", r"C:\Users\me\.hermes\state.db", "file:///C:/Users/me/.hermes/state.db?mode=ro"),
        ("win32", r"C:\a b\state#?.db", "file:///C:/a%20b/state%23%3F.db?mode=ro"),
        ("win32", r"\\server\share\hermes\state.db", "file:////server/share/hermes/state.db?mode=ro"),
        ("win32", r"\\server\share\a b\state#.db", "file:////server/share/a%20b/state%23.db?mode=ro"),
        ("win32", r"\\?\C:\x\state.db", "file:///C:/x/state.db?mode=ro"),
        ("win32", r"\\?\UNC\server\share\state.db", "file:////server/share/state.db?mode=ro"),
    ],
)
def test_state_db_readonly_uri_by_platform(platform, path, expected):
    assert agent_sessions.state_db_readonly_uri(path, platform=platform) == expected


def test_state_db_readonly_uri_never_carries_a_host_authority():
    uri = agent_sessions.state_db_readonly_uri(r"\\server\share\state.db", platform="win32")
    assert uri.startswith("file:////")
    assert "file://server" not in uri
    assert uri.endswith("?mode=ro")


def test_open_state_db_readonly_uses_platform_uri_builder(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE t(x)")
    calls = []
    real_connect = sqlite3.connect

    def recording(target, *args, **kwargs):
        calls.append((str(target), kwargs))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(agent_sessions.sqlite3, "connect", recording)
    with closing(agent_sessions.open_state_db_readonly(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
    assert calls == [(agent_sessions.state_db_readonly_uri(path.resolve()), {"uri": True})]
    assert calls[0][0].endswith("?mode=ro")


def test_empty_authority_uri_opens_on_this_platform(tmp_path):
    """SQLite accepts the empty-authority form the UNC path relies on."""
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE t(x)")
    uri = agent_sessions.state_db_readonly_uri(path.resolve())
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX filesystem-bytes path")
def test_non_utf8_posix_path_still_opens_readonly(tmp_path):
    """A non-UTF-8 path component must not make every agent session vanish.

    Python carries undecodable POSIX filename bytes as ``surrogateescape``
    code points; ``quote(str)`` rejects those with UnicodeEncodeError, which
    the listing path swallows as "no sessions". ``Path.as_uri()`` (the pre-PR
    shape) percent-encoded the fsencode() bytes, so the URI builder must too.
    """
    try:
        odd_dir = tmp_path / os.fsdecode(b"hermes-\xff-home")
        odd_dir.mkdir()
    except (OSError, UnicodeError):
        pytest.skip("filesystem rejects non-UTF-8 names")
    path = odd_dir / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    uri = agent_sessions.state_db_readonly_uri(path.resolve())
    assert "%FF" in uri
    with closing(agent_sessions.open_state_db_readonly(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (2)")


# --------------------------------------------------------------------------
# 3. The index maintenance script imports and runs without fcntl.
# --------------------------------------------------------------------------

_AGENT_MARKER_SQL = """
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version) VALUES (30);
"""


def _tool_indexes(path):
    """Names of the indexes this tool manages that exist in ``path``.

    SQLite auto-creates ``sqlite_autoindex_*`` for a ``TEXT PRIMARY KEY``, so a
    raw index count is the wrong oracle for "the tool created nothing".
    """
    with closing(sqlite3.connect(str(path))) as conn:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            if not r[0].startswith("sqlite_autoindex_")
        }


def _maintenance_schema(path):
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            _AGENT_MARKER_SQL
            + """
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, message_count INTEGER, last_activity_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, role TEXT);
            """
        )


def test_maintenance_skips_indexes_an_older_schema_cannot_hold(tmp_path, maintenance_without_fcntl):
    """A legacy ``sessions`` table without ``last_activity_at`` must not roll back
    the message indexes the schema does support."""
    module = maintenance_without_fcntl
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            _AGENT_MARKER_SQL
            + """
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, message_count INTEGER);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, role TEXT);
            """
        )
    result = module.ensure_read_indexes(path, confirmed_drained=True)
    assert result == {
        "idx_messages_session": "created",
        "idx_messages_session_role": "created",
        "idx_sessions_webui_fingerprint": "skipped",
    }
    with closing(sqlite3.connect(str(path))) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_messages_session", "idx_messages_session_role"} <= names
    assert "idx_sessions_webui_fingerprint" not in names


def test_maintenance_fails_loud_on_a_database_without_agent_tables(tmp_path, maintenance_without_fcntl):
    """``skipped`` is for a missing column only. A database with no ``messages``
    table is not an agent state.db, so the tool must raise, not report success."""
    module = maintenance_without_fcntl
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(_AGENT_MARKER_SQL + "CREATE TABLE sessions(id TEXT PRIMARY KEY); CREATE TABLE unrelated(x);")
    with pytest.raises(RuntimeError, match="no 'messages' table"):
        module.ensure_read_indexes(path, confirmed_drained=True)
    assert _tool_indexes(path) == set()


def test_maintenance_fails_closed_on_same_named_tables_without_agent_columns(tmp_path, maintenance_without_fcntl):
    """Tables named ``messages``/``sessions`` with foreign columns are not an Agent
    state.db: raise, create nothing, never report every index ``skipped``."""
    module = maintenance_without_fcntl
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            _AGENT_MARKER_SQL
            + "CREATE TABLE messages(body TEXT); CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT);"
        )
    with pytest.raises(RuntimeError, match="not an Agent state.db"):
        module.ensure_read_indexes(path, confirmed_drained=True)
    assert _tool_indexes(path) == set()


def test_maintenance_rejects_a_look_alike_chat_database_without_the_agent_marker(tmp_path, maintenance_without_fcntl):
    """Codex round-3 repro: a non-Agent chat schema that happens to carry all seven
    indexed column names must not be indexed. The Agent ``schema_version`` marker
    is the identity check; column names alone are not."""
    module = maintenance_without_fcntl
    path = tmp_path / "chat.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, message_count INTEGER, last_activity_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, role TEXT);
            """
        )
    with pytest.raises(RuntimeError, match="no Agent schema_version marker"):
        module.ensure_read_indexes(path, confirmed_drained=True)
    assert _tool_indexes(path) == set()


def test_maintenance_rejects_a_sessions_table_not_keyed_on_id(tmp_path, maintenance_without_fcntl):
    """Marker present but ``sessions`` isn't keyed on ``id``: not the Agent shape."""
    module = maintenance_without_fcntl
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            _AGENT_MARKER_SQL
            + """
            CREATE TABLE sessions(source TEXT, id TEXT, message_count INTEGER, last_activity_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, role TEXT);
            """
        )
    with pytest.raises(RuntimeError, match="not keyed on id"):
        module.ensure_read_indexes(path, confirmed_drained=True)
    assert _tool_indexes(path) == set()


def test_maintenance_rejects_a_same_named_index_on_the_legacy_gap(tmp_path, maintenance_without_fcntl):
    """On the legacy schema a pre-existing ``idx_sessions_webui_fingerprint`` can't
    be the expected shape (it would key on the absent column): report it as
    incompatible instead of skipping past it, and roll back the message indexes."""
    module = maintenance_without_fcntl
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executescript(
            _AGENT_MARKER_SQL
            + """
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, message_count INTEGER);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, role TEXT);
            CREATE INDEX idx_sessions_webui_fingerprint ON messages(role);
            """
        )
    with pytest.raises(RuntimeError, match="Incompatible index: idx_sessions_webui_fingerprint"):
        module.ensure_read_indexes(path, confirmed_drained=True)
    assert _tool_indexes(path) == {"idx_sessions_webui_fingerprint"}


@pytest.fixture
def maintenance_without_fcntl(monkeypatch):
    """Import the maintenance module as a platform without ``fcntl`` sees it."""
    name = "scripts.ensure_state_db_read_indexes"
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.delitem(sys.modules, name, raising=False)  # restores a prior real import
    module = importlib.import_module(name)
    try:
        yield module
    finally:
        # Drop the fcntl-less copy before monkeypatch restores the original entry.
        if sys.modules.get(name) is module:
            del sys.modules[name]


def test_maintenance_imports_and_runs_without_fcntl(tmp_path, maintenance_without_fcntl):
    module = maintenance_without_fcntl
    path = tmp_path / "state.db"
    _maintenance_schema(path)
    result = module.ensure_read_indexes(path, confirmed_drained=True)
    assert set(result.values()) == {"created"}
    with closing(sqlite3.connect(str(path))) as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list(messages)")}
    assert "idx_messages_session" in names


def test_maintenance_refuses_lock_file_without_a_lock_primitive(tmp_path, maintenance_without_fcntl, monkeypatch):
    module = maintenance_without_fcntl
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    path = tmp_path / "state.db"
    _maintenance_schema(path)
    with pytest.raises(RuntimeError, match="--lock-file"):
        module.ensure_read_indexes(path, confirmed_drained=True, lock_file=tmp_path / "turns.lock")
    with closing(sqlite3.connect(str(path))) as conn:
        assert conn.execute("PRAGMA index_list(messages)").fetchall() == []


def test_maintenance_cli_without_lock_file_works_without_fcntl(tmp_path, maintenance_without_fcntl, monkeypatch, capsys):
    module = maintenance_without_fcntl
    path = tmp_path / "state.db"
    _maintenance_schema(path)
    monkeypatch.setattr(sys, "argv", ["ensure_state_db_read_indexes.py", "--db", str(path), "--confirm-drained"])
    module.main()
    out = capsys.readouterr().out
    assert '"idx_messages_session": "created"' in out


# --------------------------------------------------------------------------
# 4. The Windows lock branch acquires a fresh (empty) lock file.
# --------------------------------------------------------------------------

class _StrictMsvcrt:
    """Stand-in for ``msvcrt`` that, like Windows, refuses to lock past EOF.

    Records every ``locking`` call with the file size at the current offset so
    the test can prove byte 0 existed when the lock was taken.
    """

    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self):
        self.calls = []

    def locking(self, fd, mode, nbytes):
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        size = os.fstat(fd).st_size
        self.calls.append((mode, offset, nbytes, size))
        if mode == self.LK_NBLCK and offset + nbytes > size:
            raise OSError(36, "Resource deadlock avoided: lock region beyond end of file")


@pytest.fixture
def windows_text_mode(maintenance_without_fcntl, monkeypatch):
    """Make the tool's text-mode ``open`` translate ``\\n`` to ``\\r\\n`` as on native Windows."""
    real_open = open

    def crlf_text_open(file, mode="r", *args, **kwargs):
        if "b" not in mode:
            kwargs.setdefault("newline", "\r\n")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(maintenance_without_fcntl, "open", crlf_text_open, raising=False)


def test_windows_lock_branch_acquires_a_fresh_empty_lock_file(tmp_path, maintenance_without_fcntl, windows_text_mode, monkeypatch):
    module = maintenance_without_fcntl
    assert module.fcntl is None  # the fixture already routes the tool to the msvcrt branch
    fake = _StrictMsvcrt()
    monkeypatch.setattr(module, "msvcrt", fake)

    path = tmp_path / "state.db"
    _maintenance_schema(path)
    lock = tmp_path / "turns.lock"
    assert not lock.exists()

    result = module.ensure_read_indexes(path, confirmed_drained=True, lock_file=lock)

    assert set(result.values()) == {"created"}
    # Exactly one non-blocking lock on byte 0, taken while that byte existed
    # and was the only one: no newline translation may inflate the file.
    assert fake.calls == [(fake.LK_NBLCK, 0, 1, 1)]
    assert lock.stat().st_size == 1
    with closing(sqlite3.connect(str(path))) as conn:
        names = {row[1] for row in conn.execute("PRAGMA index_list(messages)")}
    assert "idx_messages_session" in names


def test_windows_lock_branch_keeps_an_existing_lock_file_intact(tmp_path, maintenance_without_fcntl, monkeypatch):
    module = maintenance_without_fcntl
    fake = _StrictMsvcrt()
    monkeypatch.setattr(module, "msvcrt", fake)

    path = tmp_path / "state.db"
    _maintenance_schema(path)
    lock = tmp_path / "turns.lock"
    lock.write_bytes(b"deployment-owned\n")

    module.ensure_read_indexes(path, confirmed_drained=True, lock_file=lock)

    assert fake.calls == [(fake.LK_NBLCK, 0, 1, len(b"deployment-owned\n"))]
    assert lock.read_bytes() == b"deployment-owned\n"


def test_windows_lock_branch_reports_a_held_lock_without_touching_the_db(tmp_path, maintenance_without_fcntl, windows_text_mode, monkeypatch):
    module = maintenance_without_fcntl

    class _HeldMsvcrt(_StrictMsvcrt):
        def locking(self, fd, mode, nbytes):
            super().locking(fd, mode, nbytes)
            raise PermissionError(13, "Permission denied: lock held")

    fake = _HeldMsvcrt()
    monkeypatch.setattr(module, "msvcrt", fake)
    path = tmp_path / "state.db"
    _maintenance_schema(path)
    lock = tmp_path / "turns.lock"

    with pytest.raises(PermissionError):
        module.ensure_read_indexes(path, confirmed_drained=True, lock_file=lock)

    assert fake.calls == [(fake.LK_NBLCK, 0, 1, 1)]
    with closing(sqlite3.connect(str(path))) as conn:
        assert conn.execute("PRAGMA index_list(messages)").fetchall() == []
