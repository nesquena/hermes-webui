"""
Hermes Web UI -- Session model backed by Hermes' SQLite SessionDB.

Previously: separate JSON file storage in ~/.hermes/webui-mvp/sessions/
Now: unified SQLite storage in ~/.hermes/state.db (shared with CLI)

This enables:
- Sessions created in WebUI are visible to CLI (hermes session_search)
- Sessions created in CLI are visible in WebUI sidebar
- Single source of truth for session metadata and messages
- Inherited message persistence and FTS5 search

The Session class wraps Hermes' SessionDB to present a compatible API
for WebUI routes that expect get_session(), new_session(), all_sessions().
"""
import collections
import json
import time
import uuid
from pathlib import Path

import api.config as _cfg
from api.config import (
    SESSION_DIR, SESSION_INDEX_FILE, SESSIONS, SESSIONS_MAX,
    LOCK, DEFAULT_WORKSPACE, DEFAULT_MODEL, PROJECTS_FILE, HOME
)
from api.workspace import get_last_workspace

try:
    from hermes_state import SessionDB
except ImportError:
    SessionDB = None

# Global SessionDB instance (lazy-initialized on first use)
_session_db = None


def _get_session_db() -> SessionDB:
    """Get or initialize the global SessionDB instance."""
    global _session_db
    if _session_db is None:
        if SessionDB is None:
            raise RuntimeError(
                "hermes_state module not available. "
                "Ensure hermes-agent is in PYTHONPATH."
            )
        _session_db = SessionDB()
    return _session_db


def _reload_session_db():
    """Force re-initialization of SessionDB (useful after hermes updates)."""
    global _session_db
    if _session_db:
        try:
            _session_db.close()
        except Exception:
            pass
    _session_db = None


class Session:
    """
    Wraps a Hermes SessionDB entry to present a WebUI-compatible API.

    Bridges the gap between:
    - WebUI's view: session_id, title, workspace, model, messages, created_at, updated_at, pinned, archived
    - Hermes' view: id, source='webui', title, model, message_count, started_at, ended_at

    Note: Hermes stores full message objects in the messages table; WebUI loads them
    separately via get_messages(). This class composes them on read.
    """

    def __init__(
        self,
        session_id=None,
        title="Untitled",
        workspace=None,
        model=DEFAULT_MODEL,
        messages=None,
        created_at=None,
        updated_at=None,
        tool_calls=None,
        pinned=False,
        archived=False,
        project_id=None,
        profile=None,
        input_tokens=0, output_tokens=0, estimated_cost=None,
        **kwargs,
    ):
        db = _get_session_db()
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.title = title
        self.workspace = str(Path(workspace or DEFAULT_WORKSPACE).expanduser().resolve())
        self.model = model
        self.messages = messages or []
        self.tool_calls = tool_calls or []
        self.created_at = created_at or time.time()
        self.updated_at = updated_at or time.time()
        self.pinned = bool(pinned)
        self.archived = bool(archived)
        self.project_id = project_id or None
        self.profile = profile
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0
        self.estimated_cost = estimated_cost

    def save(self, touch_updated_at=True):
        """Persist session to SessionDB.

        Pass touch_updated_at=False when saving metadata-only changes (e.g.
        auto-generated title) that should not move the session to the top of
        the list or change its date group.
        """
        db = _get_session_db()
        if touch_updated_at:
            self.updated_at = time.time()

        # Ensure session exists in SessionDB (create if not present)
        session_row = db.get_session(self.session_id)
        if not session_row:
            db.create_session(
                session_id=self.session_id,
                source="webui",
                model=self.model,
            )

        # Set title if not Untitled
        if self.title != "Untitled":
            try:
                db.set_session_title(self.session_id, self.title)
            except (ValueError, Exception):
                pass  # Title conflict or validation failure; keep existing

        # Store workspace and metadata in model_config (JSON)
        try:
            config = {
                "workspace": self.workspace,
                "pinned": self.pinned,
                "archived": self.archived,
            }
            # SessionDB doesn't directly support custom fields, so we store in model_config
            db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE sessions SET model_config = ? WHERE id = ?",
                    (json.dumps(config), self.session_id),
                )
            )
        except Exception:
            pass  # Non-critical; session still persists even if metadata fails

        # Sync messages to SessionDB (add missing ones)
        existing_messages = db.get_messages(self.session_id)
        existing_count = len(existing_messages)

        # If we have more messages than SessionDB knows about, add the new ones
        if len(self.messages) > existing_count:
            for msg in self.messages[existing_count:]:
                try:
                    db.append_message(
                        session_id=self.session_id,
                        role=msg.get("role", ""),
                        content=msg.get("content", ""),
                        tool_call_id=msg.get("tool_call_id"),
                        tool_calls=msg.get("tool_calls"),
                        tool_name=msg.get("tool_name"),
                        token_count=msg.get("token_count"),
                        finish_reason=msg.get("finish_reason"),
                    )
                except Exception:
                    pass  # Skip any problematic messages

    @classmethod
    def load(cls, sid):
        """Load a session from SessionDB by ID."""
        try:
            db = _get_session_db()
            session_row = db.get_session(sid)
            if not session_row:
                return None

            # Parse metadata from model_config
            metadata = {}
            try:
                if session_row.get("model_config"):
                    metadata = json.loads(session_row["model_config"])
            except (json.JSONDecodeError, TypeError):
                pass

            # Load messages
            messages = db.get_messages(sid)

            return cls(
                session_id=sid,
                title=session_row.get("title") or "Untitled",
                workspace=metadata.get("workspace", DEFAULT_WORKSPACE),
                model=session_row.get("model") or DEFAULT_MODEL,
                messages=messages,
                created_at=session_row.get("started_at", time.time()),
                updated_at=session_row.get("started_at", time.time()),
                pinned=metadata.get("pinned", False),
                archived=metadata.get("archived", False),
            )
        except Exception:
            return None

    def compact(self):
        """Return a compact dict for list views."""
        return {
            "session_id": self.session_id,
            "title": self.title,
            "workspace": self.workspace,
            "model": self.model,
            "message_count": len(self.messages),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
            "archived": self.archived,
            "project_id": self.project_id,
            "profile": self.profile,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
        }

def get_session(sid):
    """Load a session by ID, with LRU caching."""
    with LOCK:
        if sid in SESSIONS:
            SESSIONS.move_to_end(sid)
            return SESSIONS[sid]

    s = Session.load(sid)
    if s:
        with LOCK:
            SESSIONS[sid] = s
            SESSIONS.move_to_end(sid)
            while len(SESSIONS) > SESSIONS_MAX:
                SESSIONS.popitem(last=False)
        return s
    raise KeyError(sid)


def new_session(workspace=None, model=None):
    """Create a new session in SessionDB and cache it."""
    # Use _cfg.DEFAULT_MODEL (not the import-time snapshot) so save_settings() changes take effect
    try:
        from api.profiles import get_active_profile_name
        _profile = get_active_profile_name()
    except ImportError:
        _profile = None
    s = Session(
        workspace=workspace or get_last_workspace(),
        model=model or _cfg.DEFAULT_MODEL,
        profile=_profile,
    )
    with LOCK:
        SESSIONS[s.session_id] = s
        SESSIONS.move_to_end(s.session_id)
        while len(SESSIONS) > SESSIONS_MAX:
            SESSIONS.popitem(last=False)
    s.save()
    return s


def all_sessions():
    """List all sessions from SessionDB, sorted by updated_at (descending).
    
    Includes sessions from CLI, WebUI, telegram gateway, and other sources.
    Excludes cron (automated, not user-initiated) and whatsapp/other platform UIs.
    """
    try:
        db = _get_session_db()

        # Query SessionDB for CLI + WebUI + telegram sessions.
        # Exclude cron (automated, not user-initiated) and whatsapp/other platform UIs.
        with db._lock:
            cursor = db._conn.execute(
                "SELECT id, title, model, started_at, message_count, source, parent_session_id FROM sessions "
                "WHERE source IN ('cli', 'webui', 'telegram') OR source IS NULL "
                "ORDER BY started_at DESC"
            )
            rows = cursor.fetchall()

        result = []
        for row in rows:
            sid = row["id"]
            row_source = row["source"] if "source" in row.keys() else None
            row_parent = row["parent_session_id"] if "parent_session_id" in row.keys() else None
            # Check in-memory cache first
            with LOCK:
                if sid in SESSIONS:
                    s = SESSIONS[sid]
                    c = s.compact()
                    c["source"] = row_source
                    c["parent_session_id"] = row_parent
                    result.append(c)
                    continue

            # Load from DB if not cached
            s = Session.load(sid)
            if s:
                with LOCK:
                    SESSIONS[sid] = s
                    SESSIONS.move_to_end(sid)
                    while len(SESSIONS) > SESSIONS_MAX:
                        SESSIONS.popitem(last=False)
                # Hide empty Untitled sessions
                if not (s.title == "Untitled" and len(s.messages) == 0):
                    c = s.compact()
                    c["source"] = row_source
                    c["parent_session_id"] = row_parent
                    result.append(c)

        # Add any in-memory sessions not yet persisted
        with LOCK:
            for s in SESSIONS.values():
                if not any(c["session_id"] == s.session_id for c in result):
                    if not (s.title == "Untitled" and len(s.messages) == 0):
                        result.append(s.compact())

        # Sort by pinned, then created_at (descending).
        # Using created_at (== started_at from DB) rather than updated_at so
        # that auto-title, pin, and other metadata writes don't shuffle sessions
        # into the wrong date group. Sessions stay in chronological order.
        result.sort(
            key=lambda s: (s.get("pinned", False), s["created_at"]),
            reverse=True,
        )
        # Backfill: sessions created before Sprint 22 have no profile tag.
        # Attribute them to 'default' so the client profile filter works correctly.
        for s in result:
            if not s.get('profile'):
                s['profile'] = 'default'
        return result
    except Exception as e:
        # Fallback: return in-memory sessions only
        result = []
        with LOCK:
            for s in SESSIONS.values():
                if not (s.title == "Untitled" and len(s.messages) == 0):
                    result.append(s.compact())
        result.sort(
            key=lambda s: (s.get("pinned", False), s["created_at"]),
            reverse=True,
        )
        for s in result:
            if not s.get('profile'):
                s['profile'] = 'default'
        return result


def derive_tool_calls(messages):
    """Extract tool call metadata from conversation messages.

    Walks through messages pairing assistant tool invocations with their
    tool-result messages.  Handles both Anthropic format (content blocks
    with type=tool_use) and OpenAI format (top-level tool_calls list).
    Returns a list of dicts with name, snippet, tid, assistant_msg_idx.
    """
    tool_calls = []
    pending_names = {}  # tool_call_id -> name
    pending_asst_idx = {}  # tool_call_id -> index in messages
    pending_args = {}  # tool_call_id -> args dict
    for msg_idx, m in enumerate(messages):
        if m.get("role") == "assistant":
            # Anthropic format: content is a list of blocks
            c = m.get("content", "")
            if isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and p.get("type") == "tool_use":
                        tid = p.get("id", "")
                        pending_names[tid] = p.get("name", "tool")
                        pending_asst_idx[tid] = msg_idx
                        pending_args[tid] = p.get("input", {})
            # OpenAI format: top-level tool_calls key
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict):
                    tid = tc.get("id") or tc.get("call_id", "")
                    fn = tc.get("function", {})
                    pending_names[tid] = fn.get("name", "tool")
                    pending_asst_idx[tid] = msg_idx
                    # OpenAI stores arguments as JSON string
                    raw_args = fn.get("arguments", "{}")
                    try:
                        pending_args[tid] = (
                            json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        )
                    except (json.JSONDecodeError, TypeError):
                        pending_args[tid] = {}
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id") or m.get("tool_use_id", "")
            name = pending_names.get(tid, "tool")
            asst_idx = pending_asst_idx.get(tid, -1)
            args = pending_args.get(tid, {})
            raw = str(m.get("content", ""))
            snippet = raw[:4000]
            tool_calls.append(
                {
                    "name": name,
                    "snippet": snippet,
                    "tid": tid,
                    "assistant_msg_idx": asst_idx,
                    "args": args,
                }
            )
    return tool_calls


def title_from(messages, fallback="Untitled"):
    """Derive a session title from the first user message."""
    for m in messages:
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    p.get("text", "")
                    for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            text = str(c).strip()
            if text:
                return text[:64]
    return fallback


# ── Project helpers ──────────────────────────────────────────────────────────

def load_projects():
    """Load project list from disk. Returns list of project dicts."""
    if not PROJECTS_FILE.exists():
        return []
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []

def save_projects(projects):
    """Write project list to disk."""
    PROJECTS_FILE.write_text(json.dumps(projects, ensure_ascii=False, indent=2), encoding='utf-8')


def import_cli_session(session_id, title, messages, model='unknown', profile=None):
    """Create a new WebUI session populated with CLI messages.
    Returns the Session object.
    """
    s = Session(
        session_id=session_id,
        title=title,
        workspace=get_last_workspace(),
        model=model,
        messages=messages,
        profile=profile,
    )
    s.save()
    return s


# ── CLI session bridge ──────────────────────────────────────────────────────

def get_cli_sessions():
    """Read CLI sessions from the agent's SQLite store and return them as
    dicts in a format the WebUI sidebar can render alongside local sessions.

    Returns empty list if the SQLite DB is missing, the sqlite3 module is
    unavailable, or any error occurs -- the bridge is purely additive and never
    crashes the WebUI.
    """
    import os
    cli_sessions = []
    try:
        import sqlite3
    except ImportError:
        return cli_sessions

    # Use the active WebUI profile's HERMES_HOME to find state.db.
    # The active profile is determined by what the user has selected in the UI
    # (stored in the server's runtime config). This means:
    #   - default profile  -> ~/.hermes/state.db
    #   - named profile X  -> ~/.hermes/profiles/X/state.db
    # We resolve the active profile's home directory rather than just using
    # HERMES_HOME (which is the server's launch profile, not necessarily the
    # active one after a profile switch).
    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()

    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return cli_sessions

    # Try to resolve the active CLI profile so imported sessions integrate
    # with the WebUI profile filter (available since Sprint 22).
    try:
        from api.profiles import get_active_profile_name
        _cli_profile = get_active_profile_name()
    except ImportError:
        _cli_profile = None  # older agent -- fall back to no profile

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT s.id, s.title, s.model, s.message_count,
                       s.started_at, s.source,
                       MAX(m.timestamp) AS last_activity
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                GROUP BY s.id
                ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC
                LIMIT 200
            """)
            for row in cur.fetchall():
                sid = row['id']
                raw_ts = row['last_activity'] or row['started_at']
                # Prefer the CLI session's own profile from the DB; fall back to
                # the active CLI profile so sidebar filtering works either way.
                profile = _cli_profile  # CLI DB has no profile column; use active profile

                cli_sessions.append({
                    'session_id': sid,
                    'title': row['title'] or 'CLI Session',
                    'workspace': str(get_last_workspace()),
                    'model': row['model'] or 'unknown',
                    'message_count': row['message_count'] or 0,
                    'created_at': row['started_at'],
                    'updated_at': raw_ts,
                    'pinned': False,
                    'archived': False,
                    'project_id': None,
                    'profile': profile,
                    'source_tag': 'cli',
                    'is_cli_session': True,
                })
    except Exception:
        # DB schema changed, locked, or corrupted -- silently degrade
        return []

    return cli_sessions


def get_cli_session_messages(sid):
    """Read messages for a single CLI session from the SQLite store.
    Returns a list of {role, content, timestamp} dicts.
    Returns empty list on any error.
    """
    import os
    try:
        import sqlite3
    except ImportError:
        return []

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return []

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT role, content, timestamp
                FROM messages
                WHERE session_id = ?
                ORDER BY timestamp ASC
            """, (sid,))
            msgs = []
            for row in cur.fetchall():
                msgs.append({
                    'role': row['role'],
                    'content': row['content'],
                    'timestamp': row['timestamp'],
                })
    except Exception:
        return []
    return msgs
