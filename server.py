#!/usr/bin/env python3
from __future__ import annotations

import collections
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import email.parser
import queue
import yaml

# Allow import of run_agent from parent dir
sys.path.insert(0, str(Path(__file__).parent.parent))
from run_agent import AIAgent
from tools.approval import (
    has_pending, pop_pending, submit_pending,
    approve_session, approve_permanent, save_permanent_allowlist,
    is_approved,
)

# ============================================================
# SECTION: Constants and Configuration
# ============================================================
HOST = os.getenv('HERMES_WEBUI_HOST', '127.0.0.1')
PORT = int(os.getenv('HERMES_WEBUI_PORT', '8787'))
HOME = Path.home()
CONFIG_PATH = Path(os.getenv('HERMES_CONFIG_PATH', str(HOME / '.hermes' / 'config.yaml')))
STATE_DIR = Path(os.getenv('HERMES_WEBUI_STATE_DIR', str(HOME / '.hermes' / 'webui-mvp')))
SESSION_DIR = STATE_DIR / 'sessions'
WORKSPACES_FILE = STATE_DIR / 'workspaces.json'
SESSION_INDEX_FILE = SESSION_DIR / '_index.json'  # Phase C: O(1) session list
LAST_WORKSPACE_FILE = STATE_DIR / 'last_workspace.txt'

def _write_session_index():
    """Phase C: Rebuild the session index from in-memory SESSIONS + disk files."""
    try:
        out = []
        seen = set()
        # In-memory first (most up to date)
        with LOCK:
            for s in SESSIONS.values():
                out.append(s.compact())
                seen.add(s.session_id)
        # Disk files for anything not in memory
        if SESSION_DIR.exists():
            for p in SESSION_DIR.glob('*.json'):
                if p.stem.startswith('_') or p.stem in seen:
                    continue
                try:
                    s = Session.load(p.stem)
                    if s:
                        out.append(s.compact())
                except Exception:
                    pass
        out.sort(key=lambda s: s['updated_at'], reverse=True)
        SESSION_INDEX_FILE.write_text(
            json.dumps(out, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass  # index write failure is non-fatal

def load_workspaces() -> list:
    """Load configured workspace list from disk."""
    if not WORKSPACES_FILE.exists():
        return [{'path': str(DEFAULT_WORKSPACE), 'name': DEFAULT_WORKSPACE.name}]
    try:
        return json.loads(WORKSPACES_FILE.read_text(encoding='utf-8'))
    except Exception:
        return [{'path': str(DEFAULT_WORKSPACE), 'name': DEFAULT_WORKSPACE.name}]

def save_workspaces(workspaces: list):
    """Persist workspace list to disk."""
    WORKSPACES_FILE.write_text(json.dumps(workspaces, ensure_ascii=False, indent=2), encoding='utf-8')

def get_last_workspace() -> str:
    """Return the last workspace path used (for new session inheritance)."""
    if LAST_WORKSPACE_FILE.exists():
        p = LAST_WORKSPACE_FILE.read_text(encoding='utf-8').strip()
        if p and Path(p).is_dir():
            return p
    return str(DEFAULT_WORKSPACE)

def set_last_workspace(path: str):
    """Persist the last used workspace path so new sessions inherit it."""
    LAST_WORKSPACE_FILE.write_text(path, encoding='utf-8')
DEFAULT_WORKSPACE = Path(os.getenv('HERMES_WEBUI_DEFAULT_WORKSPACE', str(HOME / '.hermes' / 'webui-mvp' / 'test-workspace'))).expanduser().resolve()
DEFAULT_MODEL = os.getenv('HERMES_WEBUI_DEFAULT_MODEL', 'openai/gpt-5.4-mini')
MAX_FILE_BYTES = 200_000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB

# File type classification for preview routing
IMAGE_EXTS  = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp'}
MD_EXTS     = {'.md', '.markdown', '.mdown'}
CODE_EXTS   = {'.py', '.js', '.ts', '.jsx', '.tsx', '.css', '.html', '.json',
               '.yaml', '.yml', '.toml', '.sh', '.bash', '.zsh', '.env',
               '.txt', '.log', '.csv', '.xml', '.sql', '.rs', '.go', '.rb',
               '.java', '.c', '.cpp', '.h', '.php', '.swift', '.kt'}
MIME_MAP = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
    '.ico': 'image/x-icon', '.bmp': 'image/bmp',
}
LOCK = threading.Lock()
SESSIONS_MAX = 100  # TD2: LRU cap -- evict oldest accessed session when full
SESSIONS: collections.OrderedDict = collections.OrderedDict()

cfg = {}
if CONFIG_PATH.exists():
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        cfg = {}
CLI_TOOLSETS = cfg.get('platform_toolsets', {}).get('cli', [
    'browser','clarify','code_execution','cronjob','delegation','file',
    'image_gen','memory','session_search','skills','terminal','todo',
    'tts','vision','web',
])

# ============================================================
# SECTION: Frontend (HTML/CSS/JS -- served as single page app)
# All rendering is client-side. This string is served verbatim
# at GET / and GET /index.html.
# ============================================================
# Phase E: HTML served from disk -- see static/index.html
_INDEX_HTML_PATH = Path(__file__).parent / "static" / "index.html"

# ============================================================
# SECTION: Server Globals
# CHAT_LOCK: legacy lock for the sync /api/chat endpoint.
# STREAMS: active SSE stream queues, keyed by stream_id.
# SESSIONS: in-memory session cache, keyed by session_id.
#   All SESSIONS accesses must hold LOCK to prevent race conditions.
# ============================================================
CHAT_LOCK = threading.Lock()
# Active SSE streams: stream_id -> queue.Queue
STREAMS = {}
STREAMS_LOCK = threading.Lock()
SERVER_START_TIME = time.time()  # Phase G: uptime tracking

# TD1: Thread-local context for per-request env isolation
# When _thread_ctx.env is set, _get_env_val() returns from it instead of os.environ,
# so concurrent requests for different sessions don't clobber each other's TERMINAL_CWD.
_thread_ctx = threading.local()

def _set_thread_env(**kwargs):
    """Set thread-local env overrides for the current agent run."""
    _thread_ctx.env = kwargs

def _clear_thread_env():
    """Clear thread-local env overrides."""
    _thread_ctx.env = {}
# Phase B: per-session agent lock so two concurrent requests for the same
# session cannot clobber each other's TERMINAL_CWD / HERMES_EXEC_ASK env vars.
SESSION_AGENT_LOCKS: dict = {}
SESSION_AGENT_LOCKS_LOCK = threading.Lock()

def _get_session_agent_lock(session_id: str) -> threading.Lock:
    with SESSION_AGENT_LOCKS_LOCK:
        if session_id not in SESSION_AGENT_LOCKS:
            SESSION_AGENT_LOCKS[session_id] = threading.Lock()
        return SESSION_AGENT_LOCKS[session_id]

# ============================================================
# SECTION: Helper Functions (path safety, JSON response, etc.)
# ============================================================

def require(body: dict, *fields):
    """Phase D: Validate required fields. Raises ValueError with clean message on missing."""
    missing = [f for f in fields if not body.get(f)]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")

def bad(handler, msg, status=400):
    """Return a clean 400 Bad Request with a message. Use instead of letting KeyError 500."""
    return j(handler, {'error': msg}, status=status)

def safe_resolve(root: Path, requested: str) -> Path:
    root = root.expanduser().resolve(); requested = (requested or '.').strip()
    candidate = root if requested in ('', '.') else (root / requested if not os.path.isabs(requested) else Path(requested)).expanduser().resolve()
    candidate.relative_to(root)
    return candidate

def j(handler, payload, status=200):
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    handler.send_response(status); handler.send_header('Content-Type','application/json; charset=utf-8'); handler.send_header('Content-Length', str(len(raw))); handler.send_header('Cache-Control','no-store'); handler.end_headers(); handler.wfile.write(raw)

def t(handler, payload, status=200, content_type='text/plain; charset=utf-8'):
    raw = payload.encode('utf-8'); handler.send_response(status); handler.send_header('Content-Type', content_type); handler.send_header('Content-Length', str(len(raw))); handler.send_header('Cache-Control','no-store'); handler.end_headers(); handler.wfile.write(raw)

def read_body(handler):
    length = int(handler.headers.get('Content-Length','0') or 0)
    return json.loads(handler.rfile.read(length) or b'{}') if length else {}

# ============================================================
# SECTION: Session Model
# Sessions are persisted as JSON files in SESSION_DIR.
# In-memory cache in SESSIONS dict (hold LOCK to access).
# ============================================================
class Session:
    def __init__(self, session_id=None, title='Untitled', workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL, messages=None, created_at=None, updated_at=None):
        self.session_id = session_id or uuid.uuid4().hex[:12]; self.title = title; self.workspace = str(Path(workspace).expanduser().resolve()); self.model = model; self.messages = messages or []; self.created_at = created_at or time.time(); self.updated_at = updated_at or time.time()
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

# ============================================================
# SECTION: File Operations
# ============================================================
def list_dir(workspace: Path, rel='.'):
    target = safe_resolve(workspace, rel)
    if not target.exists() or not target.is_dir(): raise FileNotFoundError(rel)
    rows = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rows.append({'name': item.name, 'path': str(item.relative_to(workspace)), 'type': 'dir' if item.is_dir() else 'file', 'size': None if item.is_dir() else item.stat().st_size})
    return rows[:200]

def read_file_content(workspace: Path, rel):
    target = safe_resolve(workspace, rel)
    if not target.exists() or not target.is_file(): raise FileNotFoundError(rel)
    if target.stat().st_size > MAX_FILE_BYTES: raise ValueError(f'File too large: {target.stat().st_size} bytes')
    text = target.read_text(encoding='utf-8', errors='replace')
    return {'path': str(target.relative_to(workspace)), 'content': text, 'size': target.stat().st_size, 'lines': text.count('\n') + 1}

def title_from(messages, fallback='Untitled'):
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content','')
            if isinstance(c, list): c = '\n'.join((p.get('text') or '') for p in c if isinstance(p, dict))
            txt = str(c).strip().replace('\n',' ')
            return txt[:64] if txt else fallback
    return fallback


# ============================================================
# SECTION: File Upload (multipart parser + handler)
# Uses manual multipart parser instead of deprecated cgi.FieldStorage.
# See ARCHITECTURE.md section 4.6 for rationale.
# ============================================================
def parse_multipart(rfile, content_type, content_length):
    import re as _re, email.parser as _ep
    m = _re.search(r'boundary=([^;\s]+)', content_type)
    if not m:
        raise ValueError('No boundary in Content-Type')
    boundary = m.group(1).strip('"').encode()
    raw = rfile.read(content_length)
    fields = {}
    files = {}
    delimiter = b'--' + boundary
    end_marker = b'--' + boundary + b'--'
    parts = raw.split(delimiter)
    for part in parts[1:]:
        stripped = part.lstrip(b'\r\n')
        if stripped.startswith(b'--'):
            break
        sep = b'\r\n\r\n' if b'\r\n\r\n' in part else b'\n\n'
        if sep not in part:
            continue
        header_raw, body = part.split(sep, 1)
        if body.endswith(b'\r\n'):
            body = body[:-2]
        elif body.endswith(b'\n'):
            body = body[:-1]
        header_text = header_raw.lstrip(b'\r\n').decode('utf-8', errors='replace')
        msg = _ep.HeaderParser().parsestr(header_text)
        disp = msg.get('Content-Disposition', '')
        name_m = _re.search(r'name="([^"]*)"', disp)
        file_m = _re.search(r'filename="([^"]*)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        if file_m:
            files[name] = (file_m.group(1), body)
        else:
            fields[name] = body.decode('utf-8', errors='replace')
    return fields, files


def handle_upload(handler):
    import re as _re, traceback as _tb
    try:
        content_type = handler.headers.get('Content-Type', '')
        content_length = int(handler.headers.get('Content-Length', 0) or 0)
        if content_length > MAX_UPLOAD_BYTES:
            return j(handler, {'error': f'File too large (max {MAX_UPLOAD_BYTES//1024//1024}MB)'}, status=413)
        fields, files = parse_multipart(handler.rfile, content_type, content_length)
        session_id = fields.get('session_id', '')
        if 'file' not in files:
            return j(handler, {'error': 'No file field in request'}, status=400)
        filename, file_bytes = files['file']
        if not filename:
            return j(handler, {'error': 'No filename in upload'}, status=400)
        try:
            s = get_session(session_id)
        except KeyError:
            return j(handler, {'error': 'Session not found'}, status=404)
        workspace = Path(s.workspace)
        safe_name = _re.sub(r'[^\w.\-]', '_', Path(filename).name)[:200]
        dest = workspace / safe_name
        dest.write_bytes(file_bytes)
        return j(handler, {'filename': safe_name, 'path': str(dest), 'size': dest.stat().st_size})
    except Exception as e:
        return j(handler, {'error': str(e), 'trace': _tb.format_exc()}, status=500)



# ============================================================
# SECTION: SSE Streaming Engine
# _sse(): writes one SSE event frame to the HTTP response.
# _run_agent_streaming(): daemon thread that runs the agent and
#   emits token/tool/approval/done/error events to the queue.
# ============================================================
def _sse(handler, event, data):
    """Write one SSE event to the response stream."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()


def _run_agent_streaming(session_id, msg_text, model, workspace, stream_id):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id]."""
    q = STREAMS.get(stream_id)
    if q is None:
        return

    def put(event, data):
        try:
            q.put_nowait((event, data))
        except Exception:
            pass

    try:
        s = get_session(session_id)
        s.workspace = str(Path(workspace).expanduser().resolve())
        s.model = model

        _agent_lock = _get_session_agent_lock(session_id)
        # TD1: set thread-local env context so concurrent sessions don't clobber globals
        _set_thread_env(
            TERMINAL_CWD=str(s.workspace),
            HERMES_EXEC_ASK='1',
            HERMES_SESSION_KEY=session_id,
        )
        # Still set process-level env as fallback for tools that bypass thread-local
        with _agent_lock:
          old_cwd = os.environ.get('TERMINAL_CWD')
          old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
          old_session_key = os.environ.get('HERMES_SESSION_KEY')
          os.environ['TERMINAL_CWD'] = str(s.workspace)
          os.environ['HERMES_EXEC_ASK'] = '1'
          os.environ['HERMES_SESSION_KEY'] = session_id

          try:
            def on_token(text):
                if text is None:
                    return  # end-of-stream sentinel
                put('token', {'text': text})

            def on_tool(name, preview, args):
                put('tool', {'name': name, 'preview': preview})
                # also check for pending approval and surface it immediately
                if has_pending(session_id):
                    from tools.approval import _pending, _lock
                    with _lock:
                        p = dict(_pending.get(session_id, {}))
                    if p:
                        put('approval', p)

            agent = AIAgent(
                model=model,
                platform='cli',
                quiet_mode=True,
                enabled_toolsets=CLI_TOOLSETS,
                session_id=session_id,
                stream_delta_callback=on_token,
                tool_progress_callback=on_tool,
            )
            # Prepend workspace context so the agent always knows which directory
            # to use for file operations, regardless of session age or AGENTS.md defaults.
            workspace_ctx = f"[Workspace: {s.workspace}]\n"
            workspace_system_msg = (
                f"Active workspace at session start: {s.workspace}\n"
                "Every user message is prefixed with [Workspace: /absolute/path] indicating the "
                "workspace the user has selected in the web UI at the time they sent that message. "
                "This tag is the single authoritative source of the active workspace and updates "
                "with every message. It overrides any prior workspace mentioned in this system "
                "prompt, memory, or conversation history. Always use the value from the most recent "
                "[Workspace: ...] tag as your default working directory for ALL file operations: "
                "write_file, read_file, search_files, terminal workdir, and patch. "
                "Never fall back to a hardcoded path when this tag is present."
            )
            result = agent.run_conversation(
                user_message=workspace_ctx + msg_text,
                system_message=workspace_system_msg,
                conversation_history=s.messages,
                task_id=session_id,
                persist_user_message=msg_text,
            )
            s.messages = result.get('messages') or s.messages
            s.title = title_from(s.messages, s.title)
            s.save()
            put('done', {'session': s.compact() | {'messages': s.messages}})
          finally:
            if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
            else: os.environ['TERMINAL_CWD'] = old_cwd
            if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
            else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
            if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
            else: os.environ['HERMES_SESSION_KEY'] = old_session_key

    except Exception as e:
        put('error', {'message': str(e), 'trace': traceback.format_exc()})
    finally:
        _clear_thread_env()  # TD1: always clear thread-local context
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================
class Handler(BaseHTTPRequestHandler):
    server_version = 'HermesCoWorkMVP/0.2'
    def log_message(self, fmt, *args): pass  # suppress default Apache-style log

    def log_request(self, code='-', size='-'):
        """Override BaseHTTPRequestHandler.log_request to emit structured JSON logs."""
        import json as _json
        duration_ms = round((time.time() - getattr(self, '_req_t0', time.time())) * 1000, 1)
        record = _json.dumps({
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'method': self.command or '-',
            'path': self.path or '-',
            'status': int(code) if str(code).isdigit() else code,
            'ms': duration_ms,
        })
        print(f'[webui] {record}', flush=True)

    def _log_request(self, method, path, status, duration_ms):
        pass  # kept for backward compat with error path calls; log_request handles it now
    def do_GET(self):
        _t0 = time.time()
        self._req_t0 = _t0
        try:
            parsed = urlparse(self.path)
            if parsed.path in ('/', '/index.html'): return t(self, _INDEX_HTML_PATH.read_text(encoding='utf-8'), content_type='text/html; charset=utf-8')
            if parsed.path == '/health':
                with STREAMS_LOCK: n_streams = len(STREAMS)
                return j(self, {'status':'ok','sessions':len(SESSIONS),'active_streams':n_streams,'uptime_seconds':round(time.time()-SERVER_START_TIME,1)})
            if parsed.path.startswith('/static/'):
                # Phase A: serve static assets from disk
                static_file = Path(__file__).parent / parsed.path.lstrip('/')
                if not static_file.exists() or not static_file.is_file():
                    return j(self, {'error': 'not found'}, status=404)
                ext = static_file.suffix.lower()
                ct = {'css': 'text/css', 'js': 'application/javascript', 'html': 'text/html'}.get(ext.lstrip('.'), 'text/plain')
                self.send_response(200)
                self.send_header('Content-Type', f'{ct}; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                raw = static_file.read_bytes()
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if parsed.path == '/api/session':
                sid = parse_qs(parsed.query).get('session_id', [''])[0]
                if not sid:
                    return j(self, {'error': 'session_id is required'}, status=400)
                s = get_session(sid); return j(self, {'session': s.compact() | {'messages': s.messages}})
            if parsed.path == '/api/sessions': return j(self, {'sessions': all_sessions()})
            if parsed.path == '/api/session/export':
                sid = parse_qs(parsed.query).get('session_id', [''])[0]
                if not sid: return bad(self, 'session_id is required')
                try: s = get_session(sid)
                except KeyError: return bad(self, 'Session not found', 404)
                import json as _json_exp
                payload = _json_exp.dumps(s.__dict__, ensure_ascii=False, indent=2)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Disposition', f'attachment; filename="hermes-{sid}.json"')
                self.send_header('Content-Length', str(len(payload.encode('utf-8'))))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload.encode('utf-8'))
                return
            if parsed.path == '/api/workspaces':
                return j(self, {'workspaces': load_workspaces(), 'last': get_last_workspace()})
            if parsed.path == '/api/sessions/search':
                qs2 = parse_qs(parsed.query)
                q = qs2.get('q', [''])[0].lower().strip()
                content_search = qs2.get('content', ['1'])[0] == '1'  # default: search message content too
                depth = int(qs2.get('depth', ['5'])[0])  # max messages per session to scan
                if not q: return j(self, {'sessions': all_sessions()})
                results = []
                for s in all_sessions():
                    title_match = q in (s.get('title') or '').lower()
                    if title_match:
                        results.append(dict(s, match_type='title'))
                        continue
                    if content_search:
                        # Load full session to search message content
                        try:
                            sess = get_session(s['session_id'])
                            msgs = sess.messages[:depth] if depth else sess.messages
                            for m in msgs:
                                c = m.get('content') or ''
                                if isinstance(c, list):
                                    c = ' '.join(p.get('text','') for p in c if isinstance(p,dict) and p.get('type')=='text')
                                if q in str(c).lower():
                                    results.append(dict(s, match_type='content'))
                                    break
                        except (KeyError, Exception):
                            pass
                return j(self, {'sessions': results, 'query': q, 'count': len(results)})
            if parsed.path == '/api/list':
                qs2 = parse_qs(parsed.query)
                sid2 = qs2.get('session_id', [''])[0]
                if not sid2: return bad(self, 'session_id is required')
                try: s = get_session(sid2)
                except KeyError: return bad(self, 'Session not found', 404)
                try: return j(self, {'entries': list_dir(Path(s.workspace), qs2.get('path', ['.'])[0]), 'path': qs2.get('path', ['.'])[0]})
                except (FileNotFoundError, ValueError) as e: return bad(self, str(e), 404)
            if parsed.path == '/api/chat/stream/status':
                stream_id = parse_qs(parsed.query).get('stream_id', [''])[0]
                active = stream_id in STREAMS
                return j(self, {'active': active, 'stream_id': stream_id})
            if parsed.path == '/api/chat/stream':
                stream_id = parse_qs(parsed.query).get('stream_id', [''])[0]
                q = STREAMS.get(stream_id)
                if q is None: return j(self, {'error': 'stream not found'}, status=404)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                try:
                    while True:
                        try:
                            event, data = q.get(timeout=30)
                        except queue.Empty:
                            self.wfile.write(b': heartbeat\n\n'); self.wfile.flush(); continue
                        _sse(self, event, data)
                        if event in ('done', 'error'): break
                except (BrokenPipeError, ConnectionResetError): pass
                return
            if parsed.path == '/api/file/raw':
                # Serve raw file bytes (for images). No MAX_FILE_BYTES limit for images.
                qs = parse_qs(parsed.query)
                _raw_sid = qs.get('session_id', [''])[0]
                if not _raw_sid: return bad(self, 'session_id is required')
                try: s = get_session(_raw_sid)
                except KeyError: return bad(self, 'Session not found', 404)
                rel = qs.get('path', [''])[0]
                target = safe_resolve(Path(s.workspace), rel)
                if not target.exists() or not target.is_file():
                    return j(self, {'error': 'not found'}, status=404)
                ext = target.suffix.lower()
                mime = MIME_MAP.get(ext, 'application/octet-stream')
                raw_bytes = target.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(raw_bytes)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw_bytes)
                return
            if parsed.path == '/api/file':
                qs3 = parse_qs(parsed.query)
                sid3 = qs3.get('session_id', [''])[0]
                if not sid3: return bad(self, 'session_id is required')
                try: s = get_session(sid3)
                except KeyError: return bad(self, 'Session not found', 404)
                rel3 = qs3.get('path', [''])[0]
                if not rel3: return bad(self, 'path is required')
                try: return j(self, read_file_content(Path(s.workspace), rel3))
                except (FileNotFoundError, ValueError) as e: return bad(self, str(e), 404)
            if parsed.path == '/api/approval/pending':
                sid = parse_qs(parsed.query).get('session_id', [''])[0]
                if has_pending(sid):
                    # peek without removing
                    import threading as _t
                    from tools.approval import _pending, _lock
                    with _lock:
                        p = dict(_pending.get(sid, {}))
                    return j(self, {'pending': p})
                return j(self, {'pending': None})
            # Test-only: inject a pending approval entry directly (no agent needed)
            if parsed.path == '/api/approval/inject_test':
                qs2 = parse_qs(parsed.query)
                sid = qs2.get('session_id', [''])[0]
                key = qs2.get('pattern_key', ['test_pattern'])[0]
                cmd = qs2.get('command', ['rm -rf /tmp/test'])[0]
                if sid:
                    submit_pending(sid, {
                        'command': cmd, 'pattern_key': key,
                        'pattern_keys': [key], 'description': 'test pattern',
                    })
                    return j(self, {'ok': True, 'session_id': sid})
                return j(self, {'error': 'session_id required'}, status=400)
            self._log_request(self.command, self.path, 404, (time.time()-_t0)*1000)
            # ── Cron API ──
            if parsed.path == '/api/crons':
                sys.path.insert(0, str(Path(__file__).parent.parent))
                from cron.jobs import list_jobs, OUTPUT_DIR as CRON_OUT
                jobs = list_jobs(include_disabled=True)
                return j(self, {'jobs': jobs})
            if parsed.path == '/api/crons/output':
                from cron.jobs import OUTPUT_DIR as CRON_OUT
                job_id = parse_qs(parsed.query).get('job_id', [''])[0]
                limit = int(parse_qs(parsed.query).get('limit', ['5'])[0])
                if not job_id: return j(self, {'error': 'job_id required'}, status=400)
                out_dir = CRON_OUT / job_id
                outputs = []
                if out_dir.exists():
                    files = sorted(out_dir.glob('*.md'), reverse=True)[:limit]
                    for f in files:
                        try:
                            txt_content = f.read_text(encoding='utf-8', errors='replace')
                            outputs.append({'filename': f.name, 'content': txt_content[:8000]})
                        except Exception: pass
                return j(self, {'job_id': job_id, 'outputs': outputs})
            # ── Skills API ──
            if parsed.path == '/api/skills':
                from tools.skills_tool import skills_list as _skills_list
                import json as _j
                raw = _skills_list()
                data = _j.loads(raw) if isinstance(raw, str) else raw
                return j(self, {'skills': data.get('skills', [])})
            if parsed.path == '/api/skills/content':
                from tools.skills_tool import skill_view as _skill_view
                import json as _j
                name = parse_qs(parsed.query).get('name', [''])[0]
                if not name: return j(self, {'error': 'name required'}, status=400)
                raw = _skill_view(name)
                data = _j.loads(raw) if isinstance(raw, str) else raw
                return j(self, data)
            # ── Memory API ──
            if parsed.path == '/api/memory':
                mem_dir = Path.home() / '.hermes' / 'memories'
                mem_file = mem_dir / 'MEMORY.md'
                user_file = mem_dir / 'USER.md'
                memory = mem_file.read_text(encoding='utf-8', errors='replace') if mem_file.exists() else ''
                user = user_file.read_text(encoding='utf-8', errors='replace') if user_file.exists() else ''
                return j(self, {
                    'memory': memory, 'user': user,
                    'memory_path': str(mem_file), 'user_path': str(user_file),
                    'memory_mtime': mem_file.stat().st_mtime if mem_file.exists() else None,
                    'user_mtime': user_file.stat().st_mtime if user_file.exists() else None,
                })
            if parsed.path == '/api/crons/run':
                job_id = body.get('job_id', '')
                if not job_id: return bad(self, 'job_id required')
                from cron.jobs import get_job
                from cron.scheduler import run_job
                import threading as _threading
                job = get_job(job_id)
                if not job: return bad(self, 'Job not found', 404)
                # Run in a background thread so the request returns immediately
                _threading.Thread(target=run_job, args=(job,), daemon=True).start()
                return j(self, {'ok': True, 'job_id': job_id, 'status': 'triggered'})
            if parsed.path == '/api/crons/pause':
                job_id = body.get('job_id', '')
                if not job_id: return bad(self, 'job_id required')
                from cron.jobs import pause_job
                result = pause_job(job_id, reason=body.get('reason'))
                if result: return j(self, {'ok': True, 'job': result})
                return bad(self, 'Job not found', 404)
            if parsed.path == '/api/crons/resume':
                job_id = body.get('job_id', '')
                if not job_id: return bad(self, 'job_id required')
                from cron.jobs import resume_job
                result = resume_job(job_id)
                if result: return j(self, {'ok': True, 'job': result})
                return bad(self, 'Job not found', 404)
            self._log_request(self.command, self.path, 404, (time.time()-_t0)*1000)
            if parsed.path == '/api/skills/save':
                # Create or update a skill's SKILL.md content
                try: require(body, 'name', 'content')
                except ValueError as e: return bad(self, str(e))
                skill_name = body['name'].strip().lower().replace(' ', '-')
                if not skill_name or '/' in skill_name or '..' in skill_name:
                    return bad(self, 'Invalid skill name')
                category = body.get('category', '').strip()
                from tools.skills_tool import SKILLS_DIR
                if category:
                    skill_dir = SKILLS_DIR / category / skill_name
                else:
                    skill_dir = SKILLS_DIR / skill_name
                skill_dir.mkdir(parents=True, exist_ok=True)
                skill_file = skill_dir / 'SKILL.md'
                skill_file.write_text(body['content'], encoding='utf-8')
                return j(self, {'ok': True, 'name': skill_name, 'path': str(skill_file)})
            if parsed.path == '/api/skills/delete':
                try: require(body, 'name')
                except ValueError as e: return bad(self, str(e))
                from tools.skills_tool import SKILLS_DIR
                import shutil as _shutil
                # Search for the skill directory by name
                matches = list(SKILLS_DIR.rglob(f'{body["name"]}/SKILL.md'))
                if not matches: return bad(self, 'Skill not found', 404)
                skill_dir = matches[0].parent
                _shutil.rmtree(str(skill_dir))
                return j(self, {'ok': True, 'name': body['name']})
            if parsed.path == '/api/memory/write':
                # Write to MEMORY.md or USER.md
                try: require(body, 'section', 'content')
                except ValueError as e: return bad(self, str(e))
                mem_dir = Path.home() / '.hermes' / 'memories'
                mem_dir.mkdir(parents=True, exist_ok=True)
                section = body['section']
                if section == 'memory':
                    target = mem_dir / 'MEMORY.md'
                elif section == 'user':
                    target = mem_dir / 'USER.md'
                else:
                    return bad(self, 'section must be "memory" or "user"')
                target.write_text(body['content'], encoding='utf-8')
                return j(self, {'ok': True, 'section': section, 'path': str(target)})
            return j(self, {'error':'not found'}, status=404)
        except Exception as e:
            self._log_request(self.command, self.path, 500, (time.time()-_t0)*1000)
            return j(self, {'error': str(e), 'trace': traceback.format_exc()}, status=500)
    def do_POST(self):
        _t0 = time.time()
        self._req_t0 = _t0
        try:
            parsed = urlparse(self.path)
            if parsed.path == '/api/upload':
                return handle_upload(self)
            body = read_body(self)
            if parsed.path == '/api/session/new':
                s = new_session(workspace=body.get('workspace'), model=body.get('model')); return j(self, {'session': s.compact() | {'messages': s.messages}})
            if parsed.path == '/api/sessions/cleanup':
                # Delete all sessions with no messages and title == Untitled (legacy)
                cleaned = 0
                for p in SESSION_DIR.glob('*.json'):
                    if p.name.startswith('_'): continue
                    try:
                        s = Session.load(p.stem)
                        if s and s.title == 'Untitled' and len(s.messages) == 0:
                            with LOCK: SESSIONS.pop(p.stem, None)
                            p.unlink(missing_ok=True)
                            cleaned += 1
                    except Exception: pass
                if SESSION_INDEX_FILE.exists():
                    SESSION_INDEX_FILE.unlink(missing_ok=True)
                return j(self, {'ok': True, 'cleaned': cleaned})
            if parsed.path == '/api/sessions/cleanup_zero_message':
                # Delete ALL sessions with 0 messages (used by test teardown)
                cleaned = 0
                for p in SESSION_DIR.glob('*.json'):
                    if p.name.startswith('_'): continue
                    try:
                        s = Session.load(p.stem)
                        if s and len(s.messages) == 0:
                            with LOCK: SESSIONS.pop(p.stem, None)
                            p.unlink(missing_ok=True)
                            cleaned += 1
                    except Exception: pass
                if SESSION_INDEX_FILE.exists():
                    SESSION_INDEX_FILE.unlink(missing_ok=True)
                return j(self, {'ok': True, 'cleaned': cleaned})
            if parsed.path == '/api/session/rename':
                try: require(body, 'session_id', 'title')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                s.title = str(body['title']).strip()[:80] or 'Untitled'
                s.save()
                return j(self, {'session': s.compact()})
            if parsed.path == '/api/session/update':
                try: require(body, 'session_id')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                new_ws = str(Path(body.get('workspace', s.workspace)).expanduser().resolve())
                s.workspace = new_ws; s.model = body.get('model', s.model); s.save()
                set_last_workspace(new_ws)  # persist for new session inheritance
                return j(self, {'session': s.compact() | {'messages': s.messages}})
            if parsed.path == '/api/session/delete':
                sid = body.get('session_id','')
                if not sid: return bad(self, 'session_id is required')
                with LOCK: SESSIONS.pop(sid, None)
                p = SESSION_DIR / f'{sid}.json'
                try: p.unlink(missing_ok=True)
                except Exception: pass
                return j(self, {'ok': True})
            if parsed.path == '/api/session/clear':
                # Wipe all messages from a session, keep session metadata
                try: require(body, 'session_id')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                s.messages = []
                s.title = 'Untitled'
                s.save()
                return j(self, {'ok': True, 'session': s.compact()})
            if parsed.path == '/api/session/truncate':
                # Truncate messages at a given index (keep messages[:index])
                # Used by edit+regenerate: trim everything from the edited message onward
                try: require(body, 'session_id')
                except ValueError as e: return bad(self, str(e))
                if body.get('keep_count') is None: return bad(self, 'Missing required field(s): keep_count')
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                keep = int(body['keep_count'])
                s.messages = s.messages[:keep]
                s.save()
                return j(self, {'ok': True, 'session': s.compact() | {'messages': s.messages}})
            if parsed.path == '/api/chat/start':
                try: require(body, 'session_id')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                msg = str(body.get('message', '')).strip()
                if not msg: return bad(self, 'message is required')
                workspace = str(Path(body.get('workspace') or s.workspace).expanduser().resolve())
                model = body.get('model') or s.model
                s.workspace = workspace; s.model = model; s.save()
                set_last_workspace(workspace)  # persist for new session inheritance
                stream_id = uuid.uuid4().hex
                q = queue.Queue()
                with STREAMS_LOCK: STREAMS[stream_id] = q
                t = threading.Thread(target=_run_agent_streaming,
                    args=(s.session_id, msg, model, workspace, stream_id), daemon=True)
                t.start()
                return j(self, {'stream_id': stream_id, 'session_id': s.session_id})
            if parsed.path == '/api/chat':
                s = get_session(body['session_id']); msg = str(body.get('message', '')).strip()
                if not msg: return j(self, {'error':'empty message'}, status=400)
                workspace = Path(body.get('workspace') or s.workspace).expanduser().resolve(); s.workspace = str(workspace); s.model = body.get('model') or s.model
                old_cwd = os.environ.get('TERMINAL_CWD'); os.environ['TERMINAL_CWD'] = str(workspace)
                old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
                old_session_key = os.environ.get('HERMES_SESSION_KEY')
                os.environ['HERMES_EXEC_ASK'] = '1'
                os.environ['HERMES_SESSION_KEY'] = s.session_id
                try:
                    with CHAT_LOCK:
                        agent = AIAgent(model=s.model, platform='cli', quiet_mode=True, enabled_toolsets=CLI_TOOLSETS, session_id=s.session_id)
                        workspace_ctx = f"[Workspace: {s.workspace}]\n"
                        workspace_system_msg = (
                            f"Active workspace at session start: {s.workspace}\n"
                            "Every user message is prefixed with [Workspace: /absolute/path] indicating the "
                            "workspace the user has selected in the web UI at the time they sent that message. "
                            "This tag is the single authoritative source of the active workspace and updates "
                            "with every message. It overrides any prior workspace mentioned in this system "
                            "prompt, memory, or conversation history. Always use the value from the most recent "
                            "[Workspace: ...] tag as your default working directory for ALL file operations: "
                            "write_file, read_file, search_files, terminal workdir, and patch. "
                            "Never fall back to a hardcoded path when this tag is present."
                        )
                        result = agent.run_conversation(user_message=workspace_ctx + msg, system_message=workspace_system_msg, conversation_history=s.messages, task_id=s.session_id, persist_user_message=msg)
                finally:
                    if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
                    else: os.environ['TERMINAL_CWD'] = old_cwd
                    if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
                    else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
                    if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
                    else: os.environ['HERMES_SESSION_KEY'] = old_session_key
                s.messages = result.get('messages') or s.messages; s.title = title_from(s.messages, s.title); s.save()
                return j(self, {'answer': result.get('final_response') or '', 'status': 'done' if result.get('completed', True) else 'partial', 'session': s.compact() | {'messages': s.messages}, 'result': {k:v for k,v in result.items() if k != 'messages'}})
            if parsed.path == '/api/crons/create':
                try: require(body, 'prompt', 'schedule')
                except ValueError as e: return bad(self, str(e))
                try:
                    from cron.jobs import create_job
                    job = create_job(
                        prompt=body['prompt'],
                        schedule=body['schedule'],
                        name=body.get('name') or None,
                        deliver=body.get('deliver') or 'local',
                        skills=body.get('skills') or [],
                        model=body.get('model') or None,
                    )
                    return j(self, {'ok': True, 'job': job})
                except Exception as e:
                    return j(self, {'error': str(e)}, status=400)
            if parsed.path == '/api/crons/update':
                try: require(body, 'job_id')
                except ValueError as e: return bad(self, str(e))
                from cron.jobs import update_job
                updates = {k: v for k, v in body.items() if k != 'job_id' and v is not None}
                job = update_job(body['job_id'], updates)
                if not job: return bad(self, 'Job not found', 404)
                return j(self, {'ok': True, 'job': job})
            if parsed.path == '/api/crons/delete':
                try: require(body, 'job_id')
                except ValueError as e: return bad(self, str(e))
                from cron.jobs import remove_job
                ok = remove_job(body['job_id'])
                if not ok: return bad(self, 'Job not found', 404)
                return j(self, {'ok': True, 'job_id': body['job_id']})
            if parsed.path == '/api/file/delete':
                try: require(body, 'session_id', 'path')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                try:
                    target = safe_resolve(Path(s.workspace), body['path'])
                    if not target.exists(): return bad(self, 'File not found', 404)
                    if target.is_dir(): return bad(self, 'Cannot delete directories via this endpoint')
                    target.unlink()
                    return j(self, {'ok': True, 'path': body['path']})
                except (ValueError, PermissionError) as e: return bad(self, str(e))
            if parsed.path == '/api/file/save':
                try: require(body, 'session_id', 'path')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                try:
                    target = safe_resolve(Path(s.workspace), body['path'])
                    if not target.exists(): return bad(self, 'File not found', 404)
                    if target.is_dir(): return bad(self, 'Cannot save: path is a directory')
                    target.write_text(body.get('content', ''), encoding='utf-8')
                    return j(self, {'ok': True, 'path': body['path'], 'size': target.stat().st_size})
                except (ValueError, PermissionError) as e: return bad(self, str(e))
            if parsed.path == '/api/file/create':
                try: require(body, 'session_id', 'path')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                try:
                    target = safe_resolve(Path(s.workspace), body['path'])
                    if target.exists(): return bad(self, 'File already exists')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body.get('content', ''), encoding='utf-8')
                    return j(self, {'ok': True, 'path': str(target.relative_to(Path(s.workspace)))})
                except (ValueError, PermissionError) as e: return bad(self, str(e))
            if parsed.path == '/api/workspaces/add':
                path_str = body.get('path', '').strip()
                name = body.get('name', '').strip()
                if not path_str: return bad(self, 'path is required')
                p = Path(path_str).expanduser().resolve()
                if not p.exists(): return bad(self, f'Path does not exist: {p}')
                if not p.is_dir(): return bad(self, f'Path is not a directory: {p}')
                wss = load_workspaces()
                if any(w['path'] == str(p) for w in wss):
                    return bad(self, 'Workspace already in list')
                wss.append({'path': str(p), 'name': name or p.name})
                save_workspaces(wss)
                return j(self, {'ok': True, 'workspaces': wss})
            if parsed.path == '/api/workspaces/remove':
                path_str = body.get('path', '').strip()
                if not path_str: return bad(self, 'path is required')
                wss = load_workspaces()
                wss = [w for w in wss if w['path'] != path_str]
                save_workspaces(wss)
                return j(self, {'ok': True, 'workspaces': wss})
            if parsed.path == '/api/workspaces/rename':
                path_str = body.get('path', '').strip()
                name = body.get('name', '').strip()
                if not path_str or not name: return bad(self, 'path and name are required')
                wss = load_workspaces()
                for w in wss:
                    if w['path'] == path_str:
                        w['name'] = name; break
                else:
                    return bad(self, 'Workspace not found', 404)
                save_workspaces(wss)
                return j(self, {'ok': True, 'workspaces': wss})
            if parsed.path == '/api/approval/respond':
                sid = body.get('session_id', '')
                if not sid: return bad(self, 'session_id is required')
                choice = body.get('choice', 'deny')
                if choice not in ('once','session','always','deny'):
                    return bad(self, f'Invalid choice: {choice}')
                from tools.approval import _pending, _lock, _permanent_approved
                with _lock:
                    pending = _pending.pop(sid, None)
                if pending:
                    keys = pending.get('pattern_keys') or [pending.get('pattern_key', '')]
                    if choice in ('once', 'session'):
                        for k in keys: approve_session(sid, k)
                    elif choice == 'always':
                        for k in keys:
                            approve_session(sid, k); approve_permanent(k)
                        save_permanent_allowlist(_permanent_approved)
                return j(self, {'ok': True, 'choice': choice})
            if parsed.path == '/api/skills/save':
                # Create or update a skill's SKILL.md content
                try: require(body, 'name', 'content')
                except ValueError as e: return bad(self, str(e))
                skill_name = body['name'].strip().lower().replace(' ', '-')
                if not skill_name or '/' in skill_name or '..' in skill_name:
                    return bad(self, 'Invalid skill name')
                category = body.get('category', '').strip()
                from tools.skills_tool import SKILLS_DIR
                if category:
                    skill_dir = SKILLS_DIR / category / skill_name
                else:
                    skill_dir = SKILLS_DIR / skill_name
                skill_dir.mkdir(parents=True, exist_ok=True)
                skill_file = skill_dir / 'SKILL.md'
                skill_file.write_text(body['content'], encoding='utf-8')
                return j(self, {'ok': True, 'name': skill_name, 'path': str(skill_file)})
            if parsed.path == '/api/skills/delete':
                try: require(body, 'name')
                except ValueError as e: return bad(self, str(e))
                from tools.skills_tool import SKILLS_DIR
                import shutil as _shutil
                matches = list(SKILLS_DIR.rglob(f'{body["name"]}/SKILL.md'))
                if not matches: return bad(self, 'Skill not found', 404)
                skill_dir = matches[0].parent
                _shutil.rmtree(str(skill_dir))
                return j(self, {'ok': True, 'name': body['name']})
            if parsed.path == '/api/memory/write':
                # Write to MEMORY.md or USER.md
                try: require(body, 'section', 'content')
                except ValueError as e: return bad(self, str(e))
                mem_dir = Path.home() / '.hermes' / 'memories'
                mem_dir.mkdir(parents=True, exist_ok=True)
                section = body['section']
                if section == 'memory':
                    target = mem_dir / 'MEMORY.md'
                elif section == 'user':
                    target = mem_dir / 'USER.md'
                else:
                    return bad(self, 'section must be "memory" or "user"')
                target.write_text(body['content'], encoding='utf-8')
                return j(self, {'ok': True, 'section': section, 'path': str(target)})
            return j(self, {'error':'not found'}, status=404)
        except Exception as e:
            self._log_request(self.command, self.path, 500, (time.time()-_t0)*1000)
            return j(self, {'error': str(e), 'trace': traceback.format_exc()}, status=500)

def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True); SESSION_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Hermes Co-Work MVP listening on http://{HOST}:{PORT}')
    print(f'Default workspace: {DEFAULT_WORKSPACE}')
    print(f'Default model: {DEFAULT_MODEL}')
    httpd.serve_forever()

if __name__ == '__main__':
    main()
