"""SQLite connection helpers for Hermes state.db access.

Most WebUI reads should not open the agent-owned state.db in read/write mode.
These helpers centralise read-only URI mode and busy_timeout so concurrent
agent writers do not produce avoidable immediate ``database is locked`` errors.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5000


def _apply_busy_timeout(conn: sqlite3.Connection, timeout_ms: int) -> sqlite3.Connection:
    conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    return conn


def connect_state_db_ro(db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    path = Path(db_path)
    conn = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=max(timeout_ms, 0) / 1000,
    )
    conn.row_factory = sqlite3.Row
    return _apply_busy_timeout(conn, timeout_ms)


def connect_state_db_rw(db_path: str | Path, *, timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(db_path)), timeout=max(timeout_ms, 0) / 1000)
    return _apply_busy_timeout(conn, timeout_ms)
