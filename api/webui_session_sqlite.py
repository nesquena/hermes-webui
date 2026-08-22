"""SQLite-backed SessionDB for Hermes WebUI sessions.

Drop-in replacement for api.webui_session_db.WebUIJsonSessionDB.
Uses only the Python stdlib (sqlite3) so it works inside the WebUI container.

Schema decisions:
- One row per session in ``sessions`` for metadata and hot fields.
- ``messages``, ``tool_calls``, and ``context_messages`` are stored as
  normalized rows with a JSON blob payload. This preserves the WebUI's
  evolving message shape without requiring schema churn, while keeping
  immutable history out of the hot write path.
- ``anchor_activity_scenes`` is a separate key/value table.
- ``composer_draft`` is stored as a small JSON blob on the sessions row;
  updating it does not touch the message/tool history.
- WAL mode enables concurrent readers while a write is in progress.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

# Keep JSON blobs compact.
_json_dump = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)

# Top-level scalar-ish fields that live on the sessions table.
# Anything not listed here (messages, tool_calls, etc.) goes in a child table
# or is stored as a JSON blob.
_SESSION_SCALAR_FIELDS: tuple[str, ...] = (
    "title",
    "workspace",
    "model",
    "model_provider",
    "model_explicit_pick_signature",
    "created_at",
    "updated_at",
    "pinned",
    "archived",
    "project_id",
    "profile",
    "input_tokens",
    "output_tokens",
    "estimated_cost",
    "cache_read_tokens",
    "cache_write_tokens",
    "personality",
    "active_stream_id",
    "pending_user_message",
    "pending_started_at",
    "pending_user_source",
    "compression_anchor_visible_idx",
    "compression_anchor_summary",
    "pre_compression_snapshot",
    "context_engine",
    "compression_anchor_engine",
    "compression_anchor_mode",
    "context_length",
    "threshold_tokens",
    "last_prompt_tokens",
    "post_compression_context_tokens_estimate",
    "recommended_recovery_action",
    "compression_recovery_source_session_id",
    "compression_recovery_action",
    "truncation_watermark",
    "truncation_boundary",
    "gateway_routing",
    "llm_title_generated",
    "manual_title",
    "clear_generation",
    "parent_session_id",
    "worktree_path",
    "worktree_branch",
    "worktree_repo_root",
    "worktree_created_at",
    "is_cli_session",
    "source_tag",
    "raw_source",
    "session_source",
    "source_label",
    "read_only",
    "message_count",
    "share_token",
    "share_created_at",
    # JSON blob fields
    "compression_anchor_message_key",
    "compression_anchor_details",
    "context_engine_state",
    "compression_recovery",
    "gateway_routing_history",
    "anchor_scene_index",
    "pending_attachments",
    "enabled_toolsets",
    "process_wakeup_pause",
    "composer_draft",
)

# Fields stored as JSON text in the sessions table.
_SESSION_JSON_FIELDS: set[str] = {
    "compression_anchor_message_key",
    "compression_anchor_details",
    "context_engine_state",
    "compression_recovery",
    "gateway_routing_history",
    "anchor_scene_index",
    "pending_attachments",
    "enabled_toolsets",
    "process_wakeup_pause",
    "composer_draft",
}

# Boolean fields stored as INTEGER 0/1 in SQLite.
_SESSION_BOOL_FIELDS: set[str] = {
    "pinned",
    "archived",
    "pre_compression_snapshot",
    "llm_title_generated",
    "manual_title",
    "is_cli_session",
    "read_only",
}

# Fields that are always emitted in metadata/session output even when None.
_ALWAYS_PRESENT_FIELDS: set[str] = {
    "session_id",
    "title",
    "workspace",
    "model",
    "model_provider",
    "created_at",
    "updated_at",
    "pinned",
    "archived",
    "messages",
    "tool_calls",
    "context_messages",
    "anchor_activity_scenes",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    workspace TEXT,
    model TEXT,
    model_provider TEXT,
    model_explicit_pick_signature TEXT,
    created_at REAL,
    updated_at REAL,
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    project_id TEXT,
    profile TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    estimated_cost REAL DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    personality TEXT,
    active_stream_id TEXT,
    pending_user_message TEXT,
    pending_started_at REAL,
    pending_user_source TEXT,
    pending_attachments TEXT,
    compression_anchor_visible_idx INTEGER,
    compression_anchor_message_key TEXT,
    compression_anchor_summary TEXT,
    pre_compression_snapshot INTEGER DEFAULT 0,
    context_engine TEXT,
    compression_anchor_engine TEXT,
    compression_anchor_mode TEXT,
    compression_anchor_details TEXT,
    context_engine_state TEXT,
    context_length INTEGER,
    threshold_tokens INTEGER,
    last_prompt_tokens INTEGER,
    post_compression_context_tokens_estimate INTEGER,
    compression_recovery TEXT,
    recommended_recovery_action TEXT,
    compression_recovery_source_session_id TEXT,
    compression_recovery_action TEXT,
    truncation_watermark REAL,
    truncation_boundary REAL,
    gateway_routing TEXT,
    gateway_routing_history TEXT,
    llm_title_generated INTEGER DEFAULT 0,
    manual_title INTEGER DEFAULT 0,
    clear_generation TEXT,
    parent_session_id TEXT,
    worktree_path TEXT,
    worktree_branch TEXT,
    worktree_repo_root TEXT,
    worktree_created_at REAL,
    is_cli_session INTEGER DEFAULT 0,
    source_tag TEXT,
    raw_source TEXT,
    session_source TEXT,
    source_label TEXT,
    read_only INTEGER DEFAULT 0,
    enabled_toolsets TEXT,
    composer_draft TEXT,
    process_wakeup_pause TEXT,
    share_token TEXT,
    share_created_at REAL,
    message_count INTEGER DEFAULT 0,
    anchor_scene_index TEXT,
    anchor_scene_index_hash TEXT,
    anchor_activity_scenes_json TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    message_json TEXT NOT NULL,
    UNIQUE(session_id, idx)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    tool_call_json TEXT NOT NULL,
    UNIQUE(session_id, idx)
);

CREATE TABLE IF NOT EXISTS context_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    message_json TEXT NOT NULL,
    UNIQUE(session_id, idx)
);

CREATE TABLE IF NOT EXISTS anchor_scenes (
    scene_hash TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    scene_json TEXT NOT NULL,
    PRIMARY KEY (session_id, scene_hash)
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_archived ON sessions(archived, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, idx);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, idx);
CREATE INDEX IF NOT EXISTS idx_context_messages_session ON context_messages(session_id, idx);
"""


class WebUISqliteSessionDB:
    """SQLite-backed WebUI session store.

    Mirrors the public surface of ``api.webui_session_db.WebUIJsonSessionDB``
    so it can be swapped in without changing route code.
    """

    def __init__(self, session_dir: Path | str | None = None, db_name: str = "sessions.db"):
        self._session_dir = Path(session_dir).expanduser().resolve() if session_dir else None
        self._db_name = db_name
        self._local = threading.local()
        self._ensure_schema()

    @property
    def session_dir(self) -> Path:
        if self._session_dir is not None:
            return self._session_dir
        # When running inside the WebUI container, this would import api.models.
        return Path.home() / ".hermes" / "webui-mvp" / "sessions"

    @property
    def db_path(self) -> Path:
        return self.session_dir / self._db_name

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # WAL mode: readers do not block the writer, and vice versa.
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        self._conn().executescript(SCHEMA_SQL)
        self._conn().commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cur = self._conn().execute(
            "SELECT * FROM sessions ORDER BY pinned DESC, updated_at DESC, created_at DESC"
        )
        for row in cur.fetchall():
            rows.append(self._row_to_metadata(dict(row)))
        return rows

    def read_session(self, sid: str) -> dict[str, Any] | None:
        if not _is_safe_session_id(sid):
            return None
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        if row is None:
            return None
        session = self._row_to_session(dict(row))
        session["messages"] = self._read_messages(sid)
        session["tool_calls"] = self._read_tool_calls(sid)
        session["context_messages"] = self._read_context_messages(sid)
        return session

    def session_exists(self, sid: str) -> bool:
        if not _is_safe_session_id(sid):
            return False
        row = self._conn().execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        return row is not None

    def read_metadata_only(self, sid: str) -> dict[str, Any] | None:
        """Load only metadata fields; do not touch message/tool tables."""
        if not _is_safe_session_id(sid):
            return None
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(dict(row), include_computed=False)

    def _read_messages(self, sid: str) -> list[dict[str, Any]]:
        cur = self._conn().execute(
            "SELECT message_json FROM messages WHERE session_id = ? ORDER BY idx", (sid,)
        )
        return [json.loads(row["message_json"]) for row in cur.fetchall()]

    def _read_tool_calls(self, sid: str) -> list[dict[str, Any]]:
        cur = self._conn().execute(
            "SELECT tool_call_json FROM tool_calls WHERE session_id = ? ORDER BY idx", (sid,)
        )
        return [json.loads(row["tool_call_json"]) for row in cur.fetchall()]

    def _read_context_messages(self, sid: str) -> list[dict[str, Any]]:
        cur = self._conn().execute(
            "SELECT message_json FROM context_messages WHERE session_id = ? ORDER BY idx", (sid,)
        )
        return [json.loads(row["message_json"]) for row in cur.fetchall()]

    def _read_anchor_scenes(self, sid: str) -> dict[str, Any]:
        cur = self._conn().execute(
            "SELECT scene_hash, scene_json FROM anchor_scenes WHERE session_id = ?", (sid,)
        )
        return {row["scene_hash"]: json.loads(row["scene_json"]) for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def write_session(self, session: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(session, dict):
            raise TypeError("session must be a dict")
        sid = session.get("session_id")
        if not _is_safe_session_id(sid):
            raise ValueError(f"Unsafe session_id {sid!r}")

        payload = copy.deepcopy(session)
        messages = payload.pop("messages", [])
        tool_calls = payload.pop("tool_calls", [])
        context_messages = payload.pop("context_messages", [])
        # Keep anchor_activity_scenes in payload so _write_session_row can store
        # the exact JSON blob for round-trip fidelity.
        # Only set message_count if the payload is missing it entirely.
        if "message_count" not in payload:
            payload["message_count"] = len(messages)

        conn = self._conn()
        with conn:
            self._write_session_row(conn, sid, payload)
            self._write_messages(conn, sid, messages)
            self._write_tool_calls(conn, sid, tool_calls)
            self._write_context_messages(conn, sid, context_messages)
            self._write_anchor_scenes(conn, sid, payload.get("anchor_activity_scenes", {}))
        return self.read_session(sid) or {}

    def update_metadata(self, sid: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(fields, dict):
            raise TypeError("fields must be a dict")
        unsafe = set(fields) & {"session_id", "messages", "tool_calls", "message_count"}
        if unsafe:
            raise ValueError(f"Unsafe session metadata fields: {', '.join(sorted(unsafe))}")

        conn = self._conn()
        with conn:
            set_parts: list[str] = []
            values: list[Any] = []
            for k, v in fields.items():
                if k in _SESSION_JSON_FIELDS:
                    set_parts.append(f"{k} = ?")
                    values.append(None if v is None else _json_dump(v))
                else:
                    set_parts.append(f"{k} = ?")
                    values.append(v)
            # Only bump updated_at when the caller explicitly provides it.
            # Draft autosave calls save_metadata() without updated_at so that
            # typing does not reorder the session in the sidebar or trigger
            # activity-poll reloads.
            if "updated_at" in fields:
                set_parts.append("updated_at = ?")
                values.append(fields["updated_at"])
            values.append(sid)
            conn.execute(
                f"UPDATE sessions SET {', '.join(set_parts)} WHERE session_id = ?",
                values,
            )
        return self._metadata_row(sid)

    def delete_session(self, sid: str) -> bool:
        """Remove a session and all its rows. Returns True if a row existed.

        Required by /api/session/delete: migrated sessions have no JSON
        sidecar, so unlinking the sidecar alone leaves the SQLite rows
        behind — and a full session-index rebuild would then resurrect the
        deleted session in the sidebar (and keep the transcript on disk).
        """
        if not _is_safe_session_id(sid):
            return False
        conn = self._conn()
        with conn:
            cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM context_messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM anchor_scenes WHERE session_id = ?", (sid,))
        return cur.rowcount > 0

    def archive(self, sid: str, archived: bool = True) -> dict[str, Any]:
        return self.update_metadata(sid, {"archived": bool(archived)})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _write_session_row(self, conn: sqlite3.Connection, sid: str, payload: dict[str, Any]) -> None:
        cols = ["session_id"]
        vals: list[Any] = [sid]
        for field in _SESSION_SCALAR_FIELDS:
            if field == "anchor_scene_index_hash":
                continue
            cols.append(field)
            v = payload.get(field)
            if v is None and field in _SESSION_BOOL_FIELDS:
                vals.append(0)
            elif v is None:
                vals.append(None)
            elif field in _SESSION_JSON_FIELDS:
                vals.append(_json_dump(v))
            elif field in _SESSION_BOOL_FIELDS:
                vals.append(1 if v else 0)
            else:
                vals.append(v)
        cols.append("anchor_scene_index_hash")
        idx = payload.get("anchor_scene_index")
        vals.append(_json_dump(idx) if isinstance(idx, dict) else None)
        cols.append("anchor_activity_scenes_json")
        scenes = payload.get("anchor_activity_scenes")
        vals.append(_json_dump(scenes) if scenes is not None else None)

        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders}) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols),
            vals,
        )

    def _write_messages(self, conn: sqlite3.Connection, sid: str, messages: list[Any]) -> None:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            conn.execute(
                "INSERT INTO messages (session_id, idx, message_json) VALUES (?, ?, ?)",
                (sid, idx, _json_dump(msg)),
            )

    def _write_tool_calls(self, conn: sqlite3.Connection, sid: str, tool_calls: list[Any]) -> None:
        conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (sid,))
        for idx, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            conn.execute(
                "INSERT INTO tool_calls (session_id, idx, tool_call_json) VALUES (?, ?, ?)",
                (sid, idx, _json_dump(tc)),
            )

    def _write_context_messages(self, conn: sqlite3.Connection, sid: str, context_messages: list[Any]) -> None:
        conn.execute("DELETE FROM context_messages WHERE session_id = ?", (sid,))
        for idx, msg in enumerate(context_messages):
            if not isinstance(msg, dict):
                continue
            conn.execute(
                "INSERT INTO context_messages (session_id, idx, message_json) VALUES (?, ?, ?)",
                (sid, idx, _json_dump(msg)),
            )

    def _write_anchor_scenes(self, conn: sqlite3.Connection, sid: str, scenes: Any) -> None:
        conn.execute("DELETE FROM anchor_scenes WHERE session_id = ?", (sid,))
        if not isinstance(scenes, dict):
            return
        for hsh, scene in scenes.items():
            conn.execute(
                "INSERT INTO anchor_scenes (scene_hash, session_id, scene_json) VALUES (?, ?, ?)",
                (hsh, sid, _json_dump(scene)),
            )

    def _row_to_metadata(self, d: dict[str, Any], *, include_computed: bool = True) -> dict[str, Any]:
        row: dict[str, Any] = {"session_id": d["session_id"]}
        for field in _SESSION_SCALAR_FIELDS:
            if field == "anchor_scene_index_hash":
                continue
            v = d.get(field)
            # Only emit fields that were actually stored, plus a few
            # universal ones. This keeps round-trips tight for older JSON
            # files that omitted many optional keys.
            if v is None and field not in _ALWAYS_PRESENT_FIELDS:
                continue
            if field in _SESSION_JSON_FIELDS and isinstance(v, str):
                v = json.loads(v)
            elif field in _SESSION_BOOL_FIELDS and v is not None:
                v = bool(v)
            row[field] = v
        if include_computed:
            row["last_message_at"] = d.get("updated_at") or d.get("created_at")
        return row

    def _metadata_row(self, sid: str) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        if row is None:
            raise KeyError(sid)
        return self._row_to_metadata(dict(row))

    def _row_to_session(self, d: dict[str, Any]) -> dict[str, Any]:
        session = self._row_to_metadata(d, include_computed=False)
        scenes_json = d.get("anchor_activity_scenes_json")
        if scenes_json is not None:
            session["anchor_activity_scenes"] = json.loads(scenes_json)
        return session


def _is_safe_session_id(sid: Any) -> bool:
    if not isinstance(sid, str):
        return False
    if not sid:
        return False
    if sid.startswith(".") or "/" in sid or "\\" in sid:
        return False
    return bool(sid)


# Convenience drop-in module-level helpers.
def make_store(session_dir: Path | str | None = None) -> WebUISqliteSessionDB:
    return WebUISqliteSessionDB(session_dir=session_dir)


if __name__ == "__main__":
    import sys
    sd = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    db = WebUISqliteSessionDB(session_dir=sd)
    print("SQLite session store ready at", db.db_path)
    print("Sessions:", len(db.list_sessions()))
