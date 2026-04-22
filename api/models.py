"""Hermes Web UI -- Session model and in-memory session store."""
import collections
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import api.config as _cfg
from api.config import (
    SESSION_DIR, SESSION_INDEX_FILE, SESSIONS, SESSIONS_MAX,
    LOCK, DEFAULT_WORKSPACE, DEFAULT_MODEL, PROJECTS_FILE, HOME,
    get_effective_default_model,
)
from api.workspace import get_last_workspace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stale temp-file cleanup
# ---------------------------------------------------------------------------
# Both Session.save() and _write_session_index() use the atomic-write pattern:
#   write to  <path>.tmp.<pid>.<tid>  →  os.replace() to final path
# If the process crashes between write and replace the .tmp file is left
# behind.  Because the name embeds pid + tid, leftover files can never be
# reused by a different process/thread, so they are safe to remove on the
# next startup.  _cleanup_stale_tmp_files() is called from the full-rebuild
# path of _write_session_index (i.e. at first index access / startup) and
# removes any *.tmp.* file whose mtime is older than one hour.
# ---------------------------------------------------------------------------

_STALE_TMP_AGE_SECONDS = 3600  # 1 hour


def _cleanup_stale_tmp_files() -> None:
    """Best-effort removal of stale ``*.tmp.*`` files from SESSION_DIR.

    Only files whose mtime is older than ``_STALE_TMP_AGE_SECONDS`` are
    removed so that in-flight writes from a long-running sibling process
    are not disturbed.  Errors are logged and swallowed — this must never
    prevent startup.
    """
    cutoff = time.time() - _STALE_TMP_AGE_SECONDS
    try:
        for p in SESSION_DIR.glob('*.tmp.*'):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    logger.debug("Cleaned up stale tmp file: %s", p.name)
            except OSError:
                pass  # best-effort
    except Exception:
        pass  # SESSION_DIR may not exist yet; that's fine


def _index_entry_exists(session_id: str, in_memory_ids=None) -> bool:
    """Return True if an index entry still has backing state.

    A session can legitimately exist either as a persisted JSON file or as an
    in-memory Session object that has not been flushed yet.  This helper is used
    to prune stale `_index.json` rows left behind after session-id rotation or
    file removal.
    """
    if not session_id:
        return False
    if in_memory_ids is None:
        with LOCK:
            in_memory_ids = set(SESSIONS.keys())
    if session_id in in_memory_ids:
        return True
    p = SESSION_DIR / f'{session_id}.json'
    return p.exists()


def _write_session_index(updates=None):
    """Update the session index file.

    When *updates* is provided (a list of Session objects whose compact
    entries should be refreshed), this does a targeted in-place update of
    the existing index — O(1) for single-session changes.  When *updates*
    is None, a full rebuild is performed (used on startup / first call).
    """
    # Lazy full-rebuild path — used when index doesn't exist yet.
    if updates is None or not SESSION_INDEX_FILE.exists():
        _cleanup_stale_tmp_files()  # best-effort sweep on startup / first call
        entries = []
        for p in SESSION_DIR.glob('*.json'):
            if p.name.startswith('_'): continue
            try:
                s = Session.load(p.stem)
                if s: entries.append(s.compact())
            except Exception:
                logger.debug("Failed to load session from %s", p)
        with LOCK:
            for s in SESSIONS.values():
                if not any(e['session_id'] == s.session_id for e in entries):
                    entries.append(s.compact())
            entries.sort(key=lambda s: s['updated_at'], reverse=True)
            _payload = json.dumps(entries, ensure_ascii=False, indent=2)
            _tmp = SESSION_INDEX_FILE.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')
            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(_tmp, SESSION_INDEX_FILE)
            except Exception:
                # Best-effort cleanup of stale tmp on failure
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        return

    # Fast path: patch existing index with updated sessions.
    # This avoids loading every session file on every single save().
    # LOCK covers the entire read-patch-write to prevent concurrent save() calls
    # from both reading the same baseline and one losing its update.
    _fallback = False
    try:
        with LOCK:
            existing = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
            in_memory_ids = set(SESSIONS.keys())
            existing = [
                e for e in existing
                if _index_entry_exists(e.get('session_id'), in_memory_ids=in_memory_ids)
            ]
            # Build lookup of updated entries
            updated_map = {s.session_id: s.compact() for s in updates}
            existing_ids = {e.get('session_id') for e in existing}
            # Add any updated entries not yet in the index
            for sid, entry in updated_map.items():
                if sid not in existing_ids:
                    existing.append(entry)
            # Replace matching entries in-place
            for i, e in enumerate(existing):
                sid = e.get('session_id')
                if sid in updated_map:
                    existing[i] = updated_map[sid]
            existing.sort(key=lambda s: s.get('updated_at', 0), reverse=True)
            _payload = json.dumps(existing, ensure_ascii=False, indent=2)
            _tmp = SESSION_INDEX_FILE.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')
            try:
                with open(_tmp, 'w', encoding='utf-8') as f:
                    f.write(_payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(_tmp, SESSION_INDEX_FILE)
            except Exception:
                try:
                    _tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
    except Exception:
        _fallback = True
    if _fallback:
        # Corrupt or missing index — fall back to full rebuild (called outside LOCK to avoid deadlock)
        _write_session_index(updates=None)


class Session:
    def __init__(self, session_id: str=None, title: str='Untitled',
                 workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL,
                 messages=None, created_at=None, updated_at=None,
                 tool_calls=None, pinned: bool=False, archived: bool=False,
                 project_id: str=None, profile=None,
                 input_tokens: int=0, output_tokens: int=0, estimated_cost=None,
                 personality=None,
                 active_stream_id: str=None,
                 pending_user_message: str=None,
                 pending_attachments=None,
                 pending_started_at=None,
                 compression_anchor_visible_idx=None,
                 compression_anchor_message_key=None,
                 **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.title = title
        self.workspace = str(Path(workspace).expanduser().resolve())
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
        self.personality = personality
        self.active_stream_id = active_stream_id
        self.pending_user_message = pending_user_message
        self.pending_attachments = pending_attachments or []
        self.pending_started_at = pending_started_at
        self.compression_anchor_visible_idx = compression_anchor_visible_idx
        self.compression_anchor_message_key = compression_anchor_message_key

    @property
    def path(self):
        return SESSION_DIR / f'{self.session_id}.json'

    def save(self, touch_updated_at: bool = True, skip_index: bool = False) -> None:
        if touch_updated_at:
            self.updated_at = time.time()
        payload = json.dumps(self.__dict__, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(f'.tmp.{os.getpid()}.{threading.current_thread().ident}')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        if not skip_index:
            _write_session_index(updates=[self])

    @classmethod
    def load(cls, sid):
        # Validate session ID format to prevent path traversal
        if not sid or not all(c in '0123456789abcdefghijklmnopqrstuvwxyz_' for c in sid):
            return None
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text(encoding='utf-8')))

    def compact(self) -> dict:
        return {
            'session_id': self.session_id,
            'title': self.title,
            'workspace': self.workspace,
            'model': self.model,
            'message_count': len(self.messages),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'pinned': self.pinned,
            'archived': self.archived,
            'project_id': self.project_id,
            'profile': self.profile,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'estimated_cost': self.estimated_cost,
            'personality': self.personality,
            'compression_anchor_visible_idx': self.compression_anchor_visible_idx,
            'compression_anchor_message_key': self.compression_anchor_message_key,
        }

def get_session(sid):
    with LOCK:
        if sid in SESSIONS:
            SESSIONS.move_to_end(sid)  # LRU: mark as recently used
            return SESSIONS[sid]
    s = Session.load(sid)
    if s:
        with LOCK:
            SESSIONS[sid] = s
            SESSIONS.move_to_end(sid)
            while len(SESSIONS) > SESSIONS_MAX:
                SESSIONS.popitem(last=False)  # evict least recently used
        return s
    raise KeyError(sid)

def new_session(workspace=None, model=None, profile=None):
    """Create a new in-memory session and persist it.

    *profile* — when supplied by the caller (e.g. from the request body sent
    by the active browser tab), it is used directly so that concurrent clients
    on different profiles don't fight over a shared process-global.  If not
    supplied, we fall back to the process-level active profile (the pre-#798
    behaviour, preserved for calls that originate outside a request context).
    """
    if profile is None:
        # Fallback: read process-level global (single-client or startup path)
        try:
            from api.profiles import get_active_profile_name
            profile = get_active_profile_name()
        except ImportError:
            profile = None
    effective_model = model or get_effective_default_model()
    s = Session(
        workspace=workspace or get_last_workspace(),
        model=effective_model,
        profile=profile,
    )
    with LOCK:
        SESSIONS[s.session_id] = s
        SESSIONS.move_to_end(s.session_id)
        while len(SESSIONS) > SESSIONS_MAX:
            SESSIONS.popitem(last=False)
    s.save()
    return s

def all_sessions():
    # Phase C: try index first for O(1) read; fall back to full scan
    if SESSION_INDEX_FILE.exists():
        try:
            index = json.loads(SESSION_INDEX_FILE.read_text(encoding='utf-8'))
            index = [
                s for s in index
                if _index_entry_exists(s.get('session_id'))
            ]
            # Overlay any in-memory sessions that may be newer than the index
            index_map = {s['session_id']: s for s in index}
            with LOCK:
                for s in SESSIONS.values():
                    index_map[s.session_id] = s.compact()
            result = sorted(index_map.values(), key=lambda s: (s.get('pinned', False), s['updated_at']), reverse=True)
            # Hide empty Untitled sessions from the UI (created by tests, page refreshes, etc.)
            # Exempt sessions younger than 60 s so a brand-new session stays visible (#789)
            _now = time.time()
            result = [s for s in result if not (
                s.get('title', 'Untitled') == 'Untitled'
                and s.get('message_count', 0) == 0
                and (_now - s.get('updated_at', _now)) > 60
            )]
            # Backfill: sessions created before Sprint 22 have no profile tag.
            # Attribute them to 'default' so the client profile filter works correctly.
            for s in result:
                if not s.get('profile'):
                    s['profile'] = 'default'
            return result
        except Exception:
            logger.debug("Failed to load session index, falling back to full scan")
    # Full scan fallback
    out = []
    for p in SESSION_DIR.glob('*.json'):
        if p.name.startswith('_'): continue
        try:
            s = Session.load(p.stem)
            if s: out.append(s)
        except Exception:
            logger.debug("Failed to load session from %s", p)
    for s in SESSIONS.values():
        if all(s.session_id != x.session_id for x in out): out.append(s)
    out.sort(key=lambda s: (getattr(s, 'pinned', False), s.updated_at), reverse=True)
    _now = time.time()
    result = [s.compact() for s in out if not (
        s.title == 'Untitled'
        and len(s.messages) == 0
        and (_now - s.updated_at) > 60
    )]
    for s in result:
        if not s.get('profile'):
            s['profile'] = 'default'
    return result


def title_from(messages, fallback: str='Untitled'):
    """Derive a session title from the first user message."""
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content', '')
            if isinstance(c, list):
                c = ' '.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') == 'text')
            text = str(c).strip()
            if text:
                return text[:64]
    return fallback


# ── Project helpers ──────────────────────────────────────────────────────────

def load_projects() -> list:
    """Load project list from disk. Returns list of project dicts."""
    if not PROJECTS_FILE.exists():
        return []
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []

def save_projects(projects) -> None:
    """Write project list to disk."""
    PROJECTS_FILE.write_text(json.dumps(projects, ensure_ascii=False, indent=2), encoding='utf-8')


def import_cli_session(
    session_id: str,
    title: str,
    messages,
    model: str='unknown',
    profile=None,
    created_at=None,
    updated_at=None,
):
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
        created_at=created_at,
        updated_at=updated_at,
    )
    s.save(touch_updated_at=False)
    return s


# ── CLI session bridge ──────────────────────────────────────────────────────

def get_cli_sessions() -> list:
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
            # Introspect schema to handle older hermes-agent versions that
            # may not have a 'source' column. Without this check the query raises
            # OperationalError which is silently swallowed, causing the empty-list bug.
            cur.execute("PRAGMA table_info(sessions)")
            _session_cols = {row[1] for row in cur.fetchall()}
            if 'source' not in _session_cols:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "get_cli_sessions(): state.db at %s has no 'source' column "
                    "(older hermes-agent?). CLI sessions unavailable. "
                    "Upgrade hermes-agent to fix this.",
                    db_path,
                )
                return cli_sessions

            cur.execute("""
                SELECT s.id, s.title, s.model, s.message_count,
                       s.started_at, s.source,
                       MAX(m.timestamp) AS last_activity
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                WHERE s.source IS NOT NULL AND s.source != 'webui'
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

                _source = row['source'] or 'cli'
                _display_title = row['title'] or f'{_source.title()} Session'
                cli_sessions.append({
                    'session_id': sid,
                    'title': _display_title,
                    'workspace': str(get_last_workspace()),
                    'model': row['model'] or None,
                    'message_count': row['message_count'] or 0,
                    'created_at': row['started_at'],
                    'updated_at': raw_ts,
                    'pinned': False,
                    'archived': False,
                    'project_id': None,
                    'profile': profile,
                    'source_tag': _source,
                    'is_cli_session': True,
                })
    except Exception as _cli_err:
        # DB schema changed, locked, or corrupted -- log warning so admins can diagnose.
        # Still degrade gracefully (don't crash the WebUI).
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "get_cli_sessions() failed — check state.db schema or path (%s): %s",
            db_path, _cli_err,
        )
        return []

    return cli_sessions


def get_cli_session_messages(sid) -> list:
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


def delete_cli_session(sid) -> bool:
    """Delete a CLI session from state.db (messages + session row).
    Returns True if deleted, False if not found or error.
    """
    import os
    try:
        import sqlite3
    except ImportError:
        return False

    try:
        from api.profiles import get_active_hermes_home
        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = Path(os.getenv('HERMES_HOME', str(HOME / '.hermes'))).expanduser().resolve()
    db_path = hermes_home / 'state.db'
    if not db_path.exists():
        return False

    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            cur.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        return False
