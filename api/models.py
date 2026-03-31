"""
Hermes Co-Work Web UI -- Session model and in-memory session store.
"""
import collections
import json
import time
import uuid
from pathlib import Path

from api.config import (
    SESSION_DIR, SESSION_INDEX_FILE, SESSIONS, SESSIONS_MAX,
    LOCK, DEFAULT_WORKSPACE, DEFAULT_MODEL
)
from api.workspace import get_last_workspace


def _write_session_index():
    """Rebuild the session index file for O(1) future reads."""
    entries = []
    for p in SESSION_DIR.glob('*.json'):
        if p.name.startswith('_'): continue
        try:
            s = Session.load(p.stem)
            if s: entries.append(s.compact())
        except Exception:
            pass
    with LOCK:
        for s in SESSIONS.values():
            if not any(e['session_id'] == s.session_id for e in entries):
                entries.append(s.compact())
    entries.sort(key=lambda s: s['updated_at'], reverse=True)
    SESSION_INDEX_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')


class Session:
    def __init__(self, session_id=None, title='Untitled', workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL, messages=None, created_at=None, updated_at=None, tool_calls=None, **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]; self.title = title; self.workspace = str(Path(workspace).expanduser().resolve()); self.model = model; self.messages = messages or []; self.tool_calls = tool_calls or []; self.created_at = created_at or time.time(); self.updated_at = updated_at or time.time()
    @property
    def path(self): return SESSION_DIR / f'{self.session_id}.json'
    def save(self): self.updated_at = time.time(); self.path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding='utf-8'); _write_session_index()
    @classmethod
    def load(cls, sid):
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists(): return None
        return cls(**json.loads(p.read_text(encoding='utf-8')))
    def compact(self): return {'session_id': self.session_id, 'title': self.title, 'workspace': self.workspace, 'model': self.model, 'message_count': len(self.messages), 'created_at': self.created_at, 'updated_at': self.updated_at}

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

def new_session(workspace=None, model=None):
    s = Session(workspace=workspace or get_last_workspace(), model=model or DEFAULT_MODEL)
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
            # Overlay any in-memory sessions that may be newer than the index
            index_map = {s['session_id']: s for s in index}
            with LOCK:
                for s in SESSIONS.values():
                    index_map[s.session_id] = s.compact()
            result = sorted(index_map.values(), key=lambda s: s['updated_at'], reverse=True)
            # Hide empty Untitled sessions from the UI (created by tests, page refreshes, etc.)
            result = [s for s in result if not (s.get('title','Untitled')=='Untitled' and s.get('message_count',0)==0)]
            return result
        except Exception:
            pass  # fall through to full scan
    # Full scan fallback
    out = []
    for p in SESSION_DIR.glob('*.json'):
        if p.name.startswith('_'): continue
        try:
            s = Session.load(p.stem)
            if s: out.append(s)
        except Exception:
            pass
    for s in SESSIONS.values():
        if all(s.session_id != x.session_id for x in out): out.append(s)
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return [s.compact() for s in out if not (s.title=='Untitled' and len(s.messages)==0)]


def title_from(messages, fallback='Untitled'):
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
