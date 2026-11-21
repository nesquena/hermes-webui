#!/usr/bin/env python3
"""Create the covering read indexes on the agent ``state.db`` in an explicit,
drained maintenance window.

The WebUI read paths (session listing, lineage reads, gateway watcher, cron
sidebar, insights, health) never create indexes themselves: ``CREATE INDEX``
on a multi-GiB ``messages`` table holds the SQLite writer lock for minutes,
which stalls the agent streaming into the same WAL database. Index
maintenance is therefore an operator action run while the agent is idle.

Usage::

    python scripts/ensure_state_db_read_indexes.py --db ~/.hermes/state.db \\
        --confirm-drained [--lock-file /path/to/agent-activity.lock]

``--confirm-drained`` is mandatory. When ``--lock-file`` is given, the tool
takes an exclusive non-blocking lock on that path (``flock`` on POSIX,
``msvcrt.locking`` on Windows) so that a deployment which serialises agent
turns on a lock file cannot start a turn mid-rebuild. Without ``--lock-file``
no lock primitive is needed, so the script runs on every supported platform.

Existing indexes are verified for table, key shape and collation and must be
covering (``EXPLAIN QUERY PLAN``); an incompatible index is reported, never
silently replaced.
"""
import argparse
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import sys

try:  # POSIX
    import fcntl
except ImportError:  # native Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from api.agent_sessions import state_db_file_uri  # same UNC-aware URI as the readers

INDEXES = {
    "idx_messages_session": ("messages", (("session_id", "BINARY"), ("timestamp", "BINARY"))),
    "idx_messages_session_role": ("messages", (("session_id", "BINARY"), ("role", "NOCASE"))),
    "idx_sessions_webui_fingerprint": ("sessions", tuple((c, "BINARY") for c in
        ("source", "id", "message_count", "last_activity_at"))),
}

# The only schema gap an older Agent is known to have: sessions.last_activity_at
# arrived later than the other keyed columns. An index whose missing columns are
# all listed here is reported "skipped"; any other missing column means this is
# not an Agent state.db (or a damaged one), and the tool fails closed.
_LEGACY_OPTIONAL_COLUMNS = {
    "idx_sessions_webui_fingerprint": frozenset({"last_activity_at"}),
}


def _require_agent_schema(db) -> None:
    """Fail closed unless this is an Agent ``state.db``.

    Matching column names alone would accept a look-alike chat database. Every
    Agent ``state.db`` since the first schema has carried a ``schema_version``
    row and ``sessions.id`` as the primary key, so require both before any
    ``CREATE INDEX``.
    """
    has_marker = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    version = None
    if has_marker:
        cols = {r[1] for r in db.execute("PRAGMA table_info(schema_version)")}
        if "version" in cols:
            row = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            version = row[0] if row else None
    if not isinstance(version, int) or version < 1:
        raise RuntimeError(
            "state.db has no Agent schema_version marker; not an Agent state.db, refusing to continue"
        )
    pk = [r[1] for r in db.execute("PRAGMA table_info(sessions)") if r[5]]
    if pk != ["id"]:
        raise RuntimeError(
            "state.db sessions table is not keyed on id; not an Agent state.db, refusing to continue"
        )


def _exclusive_lock(lock_file):
    if lock_file is None:
        return nullcontext()
    if fcntl is None and msvcrt is None:
        raise RuntimeError("--lock-file needs fcntl (POSIX) or msvcrt (Windows); "
                           "neither is available on this interpreter")
    handle = open(lock_file, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            # Lock one byte at offset 0 without blocking; raises OSError when held.
            # ``a+`` may have just created an empty file, and Windows cannot lock
            # a range beyond EOF, so make sure byte 0 exists first. An existing
            # (deployment-owned) lock file is left untouched. Write the byte on
            # the descriptor: a text-mode ``handle.write("\n")`` becomes two
            # bytes (``\r\n``) on native Windows.
            if os.fstat(handle.fileno()).st_size == 0:
                os.write(handle.fileno(), b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except BaseException:
        handle.close()
        raise
    return closing(handle)


def ensure_read_indexes(db_path, *, confirmed_drained=False, lock_file=None):
    if not confirmed_drained:
        raise RuntimeError("requires --confirm-drained")
    db_path = Path(db_path).resolve(strict=True)
    statuses = {}
    with _exclusive_lock(lock_file):
        # mode=rw (not rwc): never create a ghost database at a mistyped path.
        with closing(sqlite3.connect(state_db_file_uri(db_path) + "?mode=rw", uri=True)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Validate the whole Agent schema before creating anything, so a
                # wrong database fails closed without a partial index set.
                _require_agent_schema(db)
                missing_by_index = {}
                for name, (table, keys) in INDEXES.items():
                    present = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                    if not present:
                        raise RuntimeError(f"state.db has no {table!r} table; refusing to continue")
                    missing = {col for col, _ in keys if col not in present}
                    if missing - _LEGACY_OPTIONAL_COLUMNS.get(name, frozenset()):
                        raise RuntimeError(
                            f"state.db {table!r} table lacks Agent column(s) "
                            f"{sorted(missing)}; not an Agent state.db, refusing to continue"
                        )
                    missing_by_index[name] = missing
                for name, (table, keys) in INDEXES.items():
                    existing = db.execute("SELECT tbl_name FROM sqlite_master WHERE name=?", (name,)).fetchone()
                    if missing_by_index[name]:
                        # Known legacy gap. A same-named index here cannot be the
                        # shape we expect (it keys on the absent column), so it is
                        # incompatible, not something to skip past.
                        if existing:
                            raise RuntimeError(f"Incompatible index: {name}")
                        statuses[name] = "skipped"
                        continue
                    if existing:
                        actual = tuple((r[2], r[4]) for r in db.execute(f"PRAGMA index_xinfo({name})") if r[5])
                        flags = next((r for r in db.execute(f"PRAGMA index_list({table})") if r[1] == name), None)
                        if existing[0] != table or actual != keys or not flags or flags[2] or flags[4]:
                            raise RuntimeError(f"Incompatible index: {name}")
                        statuses[name] = "existing"
                    else:
                        columns = ", ".join(f"{col} COLLATE {collation}" for col, collation in keys)
                        db.execute(f"CREATE INDEX {name} ON {table}({columns})")
                        statuses[name] = "created"
                    columns = ", ".join(col for col, _ in keys)
                    plan = db.execute(f"EXPLAIN QUERY PLAN SELECT {columns} FROM {table} INDEXED BY {name}").fetchall()
                    if not any(f"COVERING INDEX {name}" in row[3] for row in plan):
                        raise RuntimeError(f"Index is not covering: {name}")
                db.commit()
            except BaseException:
                db.rollback()
                raise
    return statuses


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="path to the agent state.db")
    parser.add_argument("--lock-file", type=Path, default=None,
                        help="optional lock file to hold exclusively (flock) while indexes are built")
    parser.add_argument("--confirm-drained", action="store_true",
                        help="assert that no agent turn is running against this database")
    args = parser.parse_args()
    result = ensure_read_indexes(args.db, confirmed_drained=args.confirm_drained,
                                 lock_file=args.lock_file)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
