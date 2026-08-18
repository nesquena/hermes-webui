"""Transactional, keyed persistence for WebUI sessions.

Legacy JSON sidecars and ``_index.json`` remain importable compatibility
snapshots. SQLite owns incremental updates so a metadata change never rewrites
an entire transcript or global index.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL,
    source_mtime_ns INTEGER,
    source_size INTEGER,
    source_hashes_json TEXT
);
CREATE TABLE IF NOT EXISTS components (
    session_id TEXT NOT NULL,
    component TEXT NOT NULL,
    position INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (session_id, component, position),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS session_index (
    session_id TEXT PRIMARY KEY,
    compact_json TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    sort_timestamp REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_index_order
    ON session_index(pinned DESC, sort_timestamp DESC);
"""

_COMPONENTS = (
    "messages",
    "context_messages",
    "tool_calls",
    "anchor_activity_scenes",
)
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}
_INITIALIZED: dict[Path, tuple[int, int]] = {}


def _store_lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


class IncrementalSessionStore:
    """SQLite session store scoped to one WebUI session directory."""

    def __init__(self, session_dir: Path | str):
        self.session_dir = Path(session_dir)
        self.path = self.session_dir / "_sessions.sqlite3"
        self._lock = _store_lock(self.path)

    def _connect(self) -> sqlite3.Connection:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        stat = self.path.stat()
        signature = (stat.st_dev, stat.st_ino)
        if _INITIALIZED.get(self.path) != signature:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(sessions)")
            }
            if "source_hashes_json" not in columns:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN source_hashes_json TEXT"
                )
            _INITIALIZED[self.path] = signature
        return conn

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(value: str) -> Any:
        return json.loads(value)

    @classmethod
    def _value_hash(cls, value: Any) -> str:
        return hashlib.sha256(cls._encode(value).encode("utf-8")).hexdigest()

    @classmethod
    def _payload_hashes(cls, payload: dict[str, Any]) -> dict[str, str]:
        return {
            key: cls._value_hash(value)
            for key, value in payload.items()
            if not key.startswith("_")
        }

    @staticmethod
    def _component_rows(name: str, value: Any) -> list[Any]:
        if name == "anchor_activity_scenes":
            if not isinstance(value, dict):
                return []
            return [
                {"key": key, "value": scene}
                for key, scene in value.items()
            ]
        return list(value) if isinstance(value, list) else []

    @staticmethod
    def _restore_component(name: str, rows: list[Any]) -> Any:
        if name == "anchor_activity_scenes":
            return {
                str(row["key"]): row.get("value")
                for row in rows
                if isinstance(row, dict) and "key" in row
            }
        return rows

    @staticmethod
    def _source_signature(path: Path) -> tuple[int | None, int | None]:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None, None

    def contains(self, sid: str) -> bool:
        if not self.path.exists():
            return False
        with self._lock, closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone() is not None

    def import_legacy_payload(self, payload: dict[str, Any], source: Path) -> None:
        sid = str(payload.get("session_id") or "")
        if not sid:
            raise ValueError("session payload lacks session_id")
        source_mtime_ns, source_size = self._source_signature(source)
        self.save_payload(
            payload,
            compact=None,
            source_mtime_ns=source_mtime_ns,
            source_size=source_size,
            source_payload=payload,
            force_components=True,
        )

    def legacy_source_is_newer(self, sid: str, source: Path) -> bool:
        if not self.path.exists():
            return True
        source_signature = self._source_signature(source)
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT source_mtime_ns, source_size FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
        if row is None:
            return True
        stored = (row["source_mtime_ns"], row["source_size"])
        return source_signature != (None, None) and source_signature != stored

    def save_payload(
        self,
        payload: dict[str, Any],
        *,
        compact: dict[str, Any] | None,
        source_mtime_ns: int | None = None,
        source_size: int | None = None,
        source_payload: dict[str, Any] | None = None,
        force_components: bool = False,
        component_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sid = str(payload.get("session_id") or "")
        if not sid:
            raise ValueError("session payload lacks session_id")
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in _COMPONENTS
        }
        components = {
            name: self._component_rows(name, payload.get(name))
            for name in _COMPONENTS
        }
        previous = component_snapshot or {}
        source_hashes_json = (
            self._encode(self._payload_hashes(source_payload))
            if source_payload is not None
            else None
        )

        with self._lock, closing(self._connect()) as conn:
            with conn:
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (sid,),
                ).fetchone() is not None
                conn.execute(
                    """
                    INSERT INTO sessions(
                        session_id, metadata_json, source_mtime_ns, source_size,
                        source_hashes_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        metadata_json = excluded.metadata_json,
                        source_mtime_ns = COALESCE(excluded.source_mtime_ns, sessions.source_mtime_ns),
                        source_size = COALESCE(excluded.source_size, sessions.source_size),
                        source_hashes_json = COALESCE(
                            excluded.source_hashes_json,
                            sessions.source_hashes_json
                        )
                    """,
                    (
                        sid,
                        self._encode(metadata),
                        source_mtime_ns,
                        source_size,
                        source_hashes_json,
                    ),
                )
                for name, rows in components.items():
                    old_rows = previous.get(name)
                    if exists and not force_components and old_rows == rows:
                        continue
                    persisted = {
                        int(row["position"]): row["payload_json"]
                        for row in conn.execute(
                            """
                            SELECT position, payload_json
                            FROM components
                            WHERE session_id = ? AND component = ?
                            """,
                            (sid, name),
                        )
                    }
                    for position, value in enumerate(rows):
                        encoded = self._encode(value)
                        if persisted.get(position) == encoded:
                            continue
                        conn.execute(
                            """
                            INSERT INTO components(
                                session_id, component, position, payload_json
                            ) VALUES (?, ?, ?, ?)
                            ON CONFLICT(session_id, component, position)
                            DO UPDATE SET payload_json = excluded.payload_json
                            """,
                            (sid, name, position, encoded),
                        )
                    conn.execute(
                        """
                        DELETE FROM components
                        WHERE session_id = ? AND component = ? AND position >= ?
                        """,
                        (sid, name, len(rows)),
                    )
                if compact is not None:
                    self._upsert_index_row(conn, compact)
        return copy.deepcopy(components)

    def save_metadata(
        self,
        payload: dict[str, Any],
        *,
        compact: dict[str, Any] | None,
    ) -> None:
        """Update metadata/index only; never inspect or copy transcript rows."""
        sid = str(payload.get("session_id") or "")
        if not sid:
            raise ValueError("session payload lacks session_id")
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in _COMPONENTS
        }
        with self._lock, closing(self._connect()) as conn, conn:
            updated = conn.execute(
                "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
                (self._encode(metadata), sid),
            ).rowcount
            if updated != 1:
                raise KeyError(sid)
            if compact is not None:
                existing = conn.execute(
                    "SELECT compact_json FROM session_index WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if existing is not None:
                    compact = {
                        **self._decode(existing["compact_json"]),
                        **compact,
                    }
                self._upsert_index_row(conn, compact)

    def persisted_message_count(self, sid: str) -> int:
        if not self.path.exists():
            return 0
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM components
                WHERE session_id = ? AND component = 'messages'
                """,
                (sid,),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

    def reconcile_legacy_payload(
        self,
        legacy: dict[str, Any],
        source: Path,
    ) -> list[str]:
        """Three-way merge external JSON edits without clobbering newer DB state."""
        sid = str(legacy.get("session_id") or "")
        loaded = self.load_payload(sid)
        if loaded is None:
            self.import_legacy_payload(legacy, source)
            return []
        current, _snapshot = loaded
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT source_hashes_json FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
        baseline = {}
        if row is not None and row["source_hashes_json"]:
            try:
                baseline = self._decode(row["source_hashes_json"])
            except (TypeError, json.JSONDecodeError):
                baseline = {}

        missing_hash = self._value_hash({"__missing__": True})
        merged = copy.deepcopy(current)
        conflicts: list[str] = []
        for key in set(current) | set(legacy):
            legacy_present = key in legacy
            current_present = key in current
            legacy_hash = self._value_hash(legacy[key]) if legacy_present else missing_hash
            current_hash = self._value_hash(current[key]) if current_present else missing_hash
            baseline_hash = baseline.get(key, missing_hash)
            legacy_changed = legacy_hash != baseline_hash
            current_changed = current_hash != baseline_hash
            if not legacy_changed:
                continue
            if current_changed:
                conflicts.append(key)
                continue
            if legacy_present:
                merged[key] = copy.deepcopy(legacy[key])
            else:
                merged.pop(key, None)

        source_mtime_ns, source_size = self._source_signature(source)
        self.save_payload(
            merged,
            compact=None,
            source_mtime_ns=source_mtime_ns,
            source_size=source_size,
            source_payload=legacy,
            force_components=True,
        )
        return conflicts

    def load_payload(self, sid: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not self.path.exists():
            return None
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT metadata_json FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                return None
            payload = self._decode(row["metadata_json"])
            snapshot: dict[str, Any] = {}
            for name in _COMPONENTS:
                values = [
                    self._decode(item["payload_json"])
                    for item in conn.execute(
                        """
                        SELECT payload_json FROM components
                        WHERE session_id = ? AND component = ?
                        ORDER BY position
                        """,
                        (sid, name),
                    )
                ]
                restored = self._restore_component(name, values)
                payload[name] = restored
                snapshot[name] = copy.deepcopy(values)
        return payload, snapshot

    def load_metadata(self, sid: str) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT metadata_json FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                return None
            return self._decode(row["metadata_json"])

    def delete(self, sid: str) -> None:
        if not self.path.exists():
            return
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM session_index WHERE session_id = ?", (sid,))

    def delete_index(self, sid: str) -> None:
        if not self.path.exists():
            return
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM session_index WHERE session_id = ?", (sid,))

    def get_index(self, sid: str) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT compact_json FROM session_index WHERE session_id = ?",
                (sid,),
            ).fetchone()
            return self._decode(row["compact_json"]) if row is not None else None

    @staticmethod
    def _sort_timestamp(row: dict[str, Any]) -> float:
        for key in ("last_message_at", "updated_at", "created_at"):
            try:
                return float(row.get(key) or 0)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _upsert_index_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        sid = str(row.get("session_id") or "")
        if not sid:
            return
        conn.execute(
            """
            INSERT INTO session_index(
                session_id, compact_json, pinned, sort_timestamp
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                compact_json = excluded.compact_json,
                pinned = excluded.pinned,
                sort_timestamp = excluded.sort_timestamp
            """,
            (
                sid,
                self._encode(row),
                int(bool(row.get("pinned"))),
                self._sort_timestamp(row),
            ),
        )

    def update_index(self, rows: list[dict[str, Any]]) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            for row in rows:
                self._upsert_index_row(conn, row)

    def import_legacy_index_if_changed(self, path: Path) -> None:
        signature = self._source_signature(path)
        signature_text = f"{signature[0]}:{signature[1]}"
        with self._lock, closing(self._connect()) as conn:
            current = conn.execute(
                "SELECT value FROM store_meta WHERE key = 'legacy_index_signature'"
            ).fetchone()
            if current is not None and current["value"] == signature_text:
                return
            if signature == (None, None):
                return
            try:
                rows = json.loads(path.read_bytes())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                return
            if not isinstance(rows, list):
                return
            with conn:
                for row in rows:
                    if isinstance(row, dict):
                        self._upsert_index_row(conn, row)
                conn.execute(
                    """
                    INSERT INTO store_meta(key, value)
                    VALUES ('legacy_index_signature', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (signature_text,),
                )

    def list_index(self, *, limit: int = 2_000) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock, closing(self._connect()) as conn:
            encoded = [
                str(row["compact_json"])
                for row in conn.execute(
                    """
                    SELECT compact_json FROM session_index
                    ORDER BY pinned DESC, sort_timestamp DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ]
        if not encoded:
            return []
        # One decoder invocation materially lowers cold-list CPU for thousands
        # of rows while preserving the bounded LIMIT above.
        return self._decode(f"[{','.join(encoded)}]")

    def session_ids(self) -> frozenset[str]:
        if not self.path.exists():
            return frozenset()
        with self._lock, closing(self._connect()) as conn:
            return frozenset(
                str(row["session_id"])
                for row in conn.execute("SELECT session_id FROM sessions")
            )

    def existing_session_ids(self, candidates: list[str]) -> frozenset[str]:
        """Return persisted IDs from a caller-bounded candidate window."""
        normalized = [str(sid) for sid in candidates if str(sid or "")]
        if not normalized or not self.path.exists():
            return frozenset()
        found: set[str] = set()
        with self._lock, closing(self._connect()) as conn:
            for offset in range(0, len(normalized), 500):
                chunk = normalized[offset:offset + 500]
                placeholders = ",".join("?" for _sid in chunk)
                found.update(
                    str(row["session_id"])
                    for row in conn.execute(
                        f"SELECT session_id FROM sessions "
                        f"WHERE session_id IN ({placeholders})",
                        chunk,
                    )
                )
        return frozenset(found)
