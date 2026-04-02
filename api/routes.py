"""
Hermes Web UI -- Route handlers for GET and POST endpoints.
Extracted from server.py (Sprint 11) so server.py is a thin shell.
"""
import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from api.config import (
    STATE_DIR, SESSION_DIR, DEFAULT_WORKSPACE, DEFAULT_MODEL,
    SESSIONS, SESSIONS_MAX, LOCK, STREAMS, STREAMS_LOCK, CANCEL_FLAGS,
    SERVER_START_TIME, CLI_TOOLSETS, _INDEX_HTML_PATH, get_available_models,
    IMAGE_EXTS, MD_EXTS, MIME_MAP, MAX_FILE_BYTES, MAX_UPLOAD_BYTES,
    CHAT_LOCK, load_settings, save_settings, cfg,
)
from api.helpers import require, bad, safe_resolve, j, t, read_body
from api.models import (
    Session, get_session, new_session, all_sessions, title_from,
    _write_session_index, SESSION_INDEX_FILE,
)
from api.workspace import (
    load_workspaces, save_workspaces, get_last_workspace, set_last_workspace,
    list_dir, read_file_content, safe_resolve_ws,
)
from api.upload import handle_upload
from api.streaming import _sse, _run_agent_streaming, cancel_stream

# Approval system (optional -- graceful fallback if agent not available)
try:
    from tools.approval import (
        has_pending, pop_pending, submit_pending,
        approve_session, approve_permanent, save_permanent_allowlist,
        is_approved, _pending, _lock, _permanent_approved,
    )
except ImportError:
    has_pending = lambda *a, **k: False
    pop_pending = lambda *a, **k: None
    submit_pending = lambda *a, **k: None
    approve_session = lambda *a, **k: None
    approve_permanent = lambda *a, **k: None
    save_permanent_allowlist = lambda *a, **k: None
    is_approved = lambda *a, **k: True
    _pending = {}
    _lock = threading.Lock()
    _permanent_approved = set()


# ── GET routes ────────────────────────────────────────────────────────────────

def handle_get(handler, parsed):
    """Handle all GET routes. Returns True if handled, False for 404."""

    if parsed.path in ('/', '/index.html'):
        return t(handler, _INDEX_HTML_PATH.read_text(encoding='utf-8'),
                 content_type='text/html; charset=utf-8')

    if parsed.path == '/favicon.ico':
        handler.send_response(204); handler.end_headers(); return True

    if parsed.path == '/health':
        with STREAMS_LOCK: n_streams = len(STREAMS)
        return j(handler, {
            'status': 'ok', 'sessions': len(SESSIONS),
            'active_streams': n_streams,
            'uptime_seconds': round(time.time() - SERVER_START_TIME, 1),
        })

    if parsed.path == '/api/models':
        return j(handler, get_available_models())

    if parsed.path == '/api/settings':
        return j(handler, load_settings())

    if parsed.path.startswith('/static/'):
        return _serve_static(handler, parsed)

    if parsed.path == '/api/session':
        sid = parse_qs(parsed.query).get('session_id', [''])[0]
        if not sid:
            return j(handler, {'error': 'session_id is required'}, status=400)
        s = get_session(sid)
        return j(handler, {'session': s.compact() | {
            'messages': s.messages,
            'tool_calls': getattr(s, 'tool_calls', []),
        }})

    if parsed.path == '/api/sessions':
        return j(handler, {'sessions': all_sessions()})

    if parsed.path == '/api/session/export':
        return _handle_session_export(handler, parsed)

    if parsed.path == '/api/workspaces':
        return j(handler, {'workspaces': load_workspaces(), 'last': get_last_workspace()})

    if parsed.path == '/api/sessions/search':
        return _handle_sessions_search(handler, parsed)

    if parsed.path == '/api/list':
        return _handle_list_dir(handler, parsed)

    if parsed.path == '/api/chat/stream/status':
        stream_id = parse_qs(parsed.query).get('stream_id', [''])[0]
        return j(handler, {'active': stream_id in STREAMS, 'stream_id': stream_id})

    if parsed.path == '/api/chat/cancel':
        stream_id = parse_qs(parsed.query).get('stream_id', [''])[0]
        if not stream_id:
            return bad(handler, 'stream_id required')
        cancelled = cancel_stream(stream_id)
        return j(handler, {'ok': True, 'cancelled': cancelled, 'stream_id': stream_id})

    if parsed.path == '/api/chat/stream':
        return _handle_sse_stream(handler, parsed)

    if parsed.path == '/api/file/raw':
        return _handle_file_raw(handler, parsed)

    if parsed.path == '/api/file':
        return _handle_file_read(handler, parsed)

    if parsed.path == '/api/approval/pending':
        return _handle_approval_pending(handler, parsed)

    if parsed.path == '/api/approval/inject_test':
        return _handle_approval_inject(handler, parsed)

    # ── Cron API (GET) ──
    if parsed.path == '/api/crons':
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from cron.jobs import list_jobs
        return j(handler, {'jobs': list_jobs(include_disabled=True)})

    if parsed.path == '/api/crons/output':
        return _handle_cron_output(handler, parsed)

    if parsed.path == '/api/crons/recent':
        return _handle_cron_recent(handler, parsed)

    # ── Skills API (GET) ──
    if parsed.path == '/api/skills':
        from tools.skills_tool import skills_list as _skills_list
        raw = _skills_list()
        data = json.loads(raw) if isinstance(raw, str) else raw
        return j(handler, {'skills': data.get('skills', [])})

    if parsed.path == '/api/skills/content':
        from tools.skills_tool import skill_view as _skill_view
        name = parse_qs(parsed.query).get('name', [''])[0]
        if not name: return j(handler, {'error': 'name required'}, status=400)
        raw = _skill_view(name)
        data = json.loads(raw) if isinstance(raw, str) else raw
        return j(handler, data)

    # ── Memory API (GET) ──
    if parsed.path == '/api/memory':
        return _handle_memory_read(handler)

    return False  # 404


# ── POST routes ───────────────────────────────────────────────────────────────

def handle_post(handler, parsed):
    """Handle all POST routes. Returns True if handled, False for 404."""

    if parsed.path == '/api/upload':
        return handle_upload(handler)

    body = read_body(handler)

    if parsed.path == '/api/session/new':
        s = new_session(workspace=body.get('workspace'), model=body.get('model'))
        return j(handler, {'session': s.compact() | {'messages': s.messages}})

    if parsed.path == '/api/sessions/cleanup':
        return _handle_sessions_cleanup(handler, body, zero_only=False)

    if parsed.path == '/api/sessions/cleanup_zero_message':
        return _handle_sessions_cleanup(handler, body, zero_only=True)

    if parsed.path == '/api/session/rename':
        try: require(body, 'session_id', 'title')
        except ValueError as e: return bad(handler, str(e))
        try: s = get_session(body['session_id'])
        except KeyError: return bad(handler, 'Session not found', 404)
        s.title = str(body['title']).strip()[:80] or 'Untitled'
        s.save()
        return j(handler, {'session': s.compact()})

    if parsed.path == '/api/session/update':
        try: require(body, 'session_id')
        except ValueError as e: return bad(handler, str(e))
        try: s = get_session(body['session_id'])
        except KeyError: return bad(handler, 'Session not found', 404)
        new_ws = str(Path(body.get('workspace', s.workspace)).expanduser().resolve())
        s.workspace = new_ws; s.model = body.get('model', s.model); s.save()
        set_last_workspace(new_ws)
        return j(handler, {'session': s.compact() | {'messages': s.messages}})

    if parsed.path == '/api/session/delete':
        sid = body.get('session_id', '')
        if not sid: return bad(handler, 'session_id is required')
        with LOCK: SESSIONS.pop(sid, None)
        p = SESSION_DIR / f'{sid}.json'
        try: p.unlink(missing_ok=True)
        except Exception: pass
        try: SESSION_INDEX_FILE.unlink(missing_ok=True)
        except Exception: pass
        return j(handler, {'ok': True})

    if parsed.path == '/api/session/clear':
        try: require(body, 'session_id')
        except ValueError as e: return bad(handler, str(e))
        try: s = get_session(body['session_id'])
        except KeyError: return bad(handler, 'Session not found', 404)
        s.messages = []; s.tool_calls = []; s.title = 'Untitled'; s.save()
        return j(handler, {'ok': True, 'session': s.compact()})

    if parsed.path == '/api/session/truncate':
        try: require(body, 'session_id')
        except ValueError as e: return bad(handler, str(e))
        if body.get('keep_count') is None:
            return bad(handler, 'Missing required field(s): keep_count')
        try: s = get_session(body['session_id'])
        except KeyError: return bad(handler, 'Session not found', 404)
        keep = int(body['keep_count'])
        s.messages = s.messages[:keep]; s.save()
        return j(handler, {'ok': True, 'session': s.compact() | {'messages': s.messages}})

    if parsed.path == '/api/chat/start':
        return _handle_chat_start(handler, body)

    if parsed.path == '/api/chat':
        return _handle_chat_sync(handler, body)

    # ── Cron API (POST) ──
    if parsed.path == '/api/crons/create':
        return _handle_cron_create(handler, body)

    if parsed.path == '/api/crons/update':
        return _handle_cron_update(handler, body)

    if parsed.path == '/api/crons/delete':
        return _handle_cron_delete(handler, body)

    if parsed.path == '/api/crons/run':
        return _handle_cron_run(handler, body)

    if parsed.path == '/api/crons/pause':
        return _handle_cron_pause(handler, body)

    if parsed.path == '/api/crons/resume':
        return _handle_cron_resume(handler, body)

    # ── File ops (POST) ──
    if parsed.path == '/api/file/delete':
        return _handle_file_delete(handler, body)

    if parsed.path == '/api/file/save':
        return _handle_file_save(handler, body)

    if parsed.path == '/api/file/create':
        return _handle_file_create(handler, body)

    if parsed.path == '/api/file/rename':
        return _handle_file_rename(handler, body)

    if parsed.path == '/api/file/create-dir':
        return _handle_create_dir(handler, body)

    # ── Workspace management (POST) ──
    if parsed.path == '/api/workspaces/add':
        return _handle_workspace_add(handler, body)

    if parsed.path == '/api/workspaces/remove':
        return _handle_workspace_remove(handler, body)

    if parsed.path == '/api/workspaces/rename':
        return _handle_workspace_rename(handler, body)

    # ── Approval (POST) ──
    if parsed.path == '/api/approval/respond':
        return _handle_approval_respond(handler, body)

    # ── Skills (POST) ──
    if parsed.path == '/api/skills/save':
        return _handle_skill_save(handler, body)

    if parsed.path == '/api/skills/delete':
        return _handle_skill_delete(handler, body)

    # ── Memory (POST) ──
    if parsed.path == '/api/memory/write':
        return _handle_memory_write(handler, body)

    # ── Settings (POST) ──
    if parsed.path == '/api/settings':
        return j(handler, save_settings(body))

    # ── Session pin (POST) ──
    if parsed.path == '/api/session/pin':
        try: require(body, 'session_id')
        except ValueError as e: return bad(handler, str(e))
        try: s = get_session(body['session_id'])
        except KeyError: return bad(handler, 'Session not found', 404)
        s.pinned = bool(body.get('pinned', True))
        s.save()
        return j(handler, {'ok': True, 'session': s.compact()})

    # ── Session archive (POST) ──
    if parsed.path == '/api/session/archive':
        try: require(body, 'session_id')
        except ValueError as e: return bad(handler, str(e))
        try: s = get_session(body['session_id'])
        except KeyError: return bad(handler, 'Session not found', 404)
        s.archived = bool(body.get('archived', True))
        s.save()
        return j(handler, {'ok': True, 'session': s.compact()})

    # ── Session import from JSON (POST) ──
    if parsed.path == '/api/session/import':
        return _handle_session_import(handler, body)

    return False  # 404


# ── GET route helpers ─────────────────────────────────────────────────────────

def _serve_static(handler, parsed):
    static_file = Path(__file__).parent.parent / parsed.path.lstrip('/')
    if not static_file.exists() or not static_file.is_file():
        return j(handler, {'error': 'not found'}, status=404)
    ext = static_file.suffix.lower()
    ct = {'css': 'text/css', 'js': 'application/javascript',
          'html': 'text/html'}.get(ext.lstrip('.'), 'text/plain')
    handler.send_response(200)
    handler.send_header('Content-Type', f'{ct}; charset=utf-8')
    handler.send_header('Cache-Control', 'no-store')
    raw = static_file.read_bytes()
    handler.send_header('Content-Length', str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)
    return True


def _handle_session_export(handler, parsed):
    sid = parse_qs(parsed.query).get('session_id', [''])[0]
    if not sid: return bad(handler, 'session_id is required')
    try: s = get_session(sid)
    except KeyError: return bad(handler, 'Session not found', 404)
    payload = json.dumps(s.__dict__, ensure_ascii=False, indent=2)
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Disposition', f'attachment; filename="hermes-{sid}.json"')
    handler.send_header('Content-Length', str(len(payload.encode('utf-8'))))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.wfile.write(payload.encode('utf-8'))
    return True


def _handle_sessions_search(handler, parsed):
    qs = parse_qs(parsed.query)
    q = qs.get('q', [''])[0].lower().strip()
    content_search = qs.get('content', ['1'])[0] == '1'
    depth = int(qs.get('depth', ['5'])[0])
    if not q: return j(handler, {'sessions': all_sessions()})
    results = []
    for s in all_sessions():
        title_match = q in (s.get('title') or '').lower()
        if title_match:
            results.append(dict(s, match_type='title'))
            continue
        if content_search:
            try:
                sess = get_session(s['session_id'])
                msgs = sess.messages[:depth] if depth else sess.messages
                for m in msgs:
                    c = m.get('content') or ''
                    if isinstance(c, list):
                        c = ' '.join(p.get('text', '') for p in c
                                     if isinstance(p, dict) and p.get('type') == 'text')
                    if q in str(c).lower():
                        results.append(dict(s, match_type='content'))
                        break
            except (KeyError, Exception):
                pass
    return j(handler, {'sessions': results, 'query': q, 'count': len(results)})


def _handle_list_dir(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get('session_id', [''])[0]
    if not sid: return bad(handler, 'session_id is required')
    try: s = get_session(sid)
    except KeyError: return bad(handler, 'Session not found', 404)
    try:
        return j(handler, {
            'entries': list_dir(Path(s.workspace), qs.get('path', ['.'])[0]),
            'path': qs.get('path', ['.'])[0],
        })
    except (FileNotFoundError, ValueError) as e:
        return bad(handler, str(e), 404)


def _handle_sse_stream(handler, parsed):
    stream_id = parse_qs(parsed.query).get('stream_id', [''])[0]
    q = STREAMS.get(stream_id)
    if q is None: return j(handler, {'error': 'stream not found'}, status=404)
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'keep-alive')
    handler.end_headers()
    try:
        while True:
            try:
                event, data = q.get(timeout=30)
            except queue.Empty:
                handler.wfile.write(b': heartbeat\n\n')
                handler.wfile.flush()
                continue
            _sse(handler, event, data)
            if event in ('done', 'error', 'cancel'):
                break
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True


def _handle_file_raw(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get('session_id', [''])[0]
    if not sid: return bad(handler, 'session_id is required')
    try: s = get_session(sid)
    except KeyError: return bad(handler, 'Session not found', 404)
    rel = qs.get('path', [''])[0]
    force_download = qs.get('download', [''])[0] == '1'
    target = safe_resolve(Path(s.workspace), rel)
    if not target.exists() or not target.is_file():
        return j(handler, {'error': 'not found'}, status=404)
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, 'application/octet-stream')
    raw_bytes = target.read_bytes()
    import urllib.parse as _up
    safe_name = _up.quote(target.name, safe='')
    handler.send_response(200)
    handler.send_header('Content-Type', mime)
    handler.send_header('Content-Length', str(len(raw_bytes)))
    handler.send_header('Cache-Control', 'no-store')
    if force_download:
        handler.send_header('Content-Disposition',
            f'attachment; filename="{target.name}"; filename*=UTF-8\'\'{safe_name}')
    handler.end_headers()
    handler.wfile.write(raw_bytes)
    return True


def _handle_file_read(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get('session_id', [''])[0]
    if not sid: return bad(handler, 'session_id is required')
    try: s = get_session(sid)
    except KeyError: return bad(handler, 'Session not found', 404)
    rel = qs.get('path', [''])[0]
    if not rel: return bad(handler, 'path is required')
    try: return j(handler, read_file_content(Path(s.workspace), rel))
    except (FileNotFoundError, ValueError) as e: return bad(handler, str(e), 404)


def _handle_approval_pending(handler, parsed):
    sid = parse_qs(parsed.query).get('session_id', [''])[0]
    if has_pending(sid):
        with _lock:
            p = dict(_pending.get(sid, {}))
        return j(handler, {'pending': p})
    return j(handler, {'pending': None})


def _handle_approval_inject(handler, parsed):
    qs = parse_qs(parsed.query)
    sid = qs.get('session_id', [''])[0]
    key = qs.get('pattern_key', ['test_pattern'])[0]
    cmd = qs.get('command', ['rm -rf /tmp/test'])[0]
    if sid:
        submit_pending(sid, {
            'command': cmd, 'pattern_key': key,
            'pattern_keys': [key], 'description': 'test pattern',
        })
        return j(handler, {'ok': True, 'session_id': sid})
    return j(handler, {'error': 'session_id required'}, status=400)


def _handle_cron_output(handler, parsed):
    from cron.jobs import OUTPUT_DIR as CRON_OUT
    qs = parse_qs(parsed.query)
    job_id = qs.get('job_id', [''])[0]
    limit = int(qs.get('limit', ['5'])[0])
    if not job_id: return j(handler, {'error': 'job_id required'}, status=400)
    out_dir = CRON_OUT / job_id
    outputs = []
    if out_dir.exists():
        files = sorted(out_dir.glob('*.md'), reverse=True)[:limit]
        for f in files:
            try:
                txt = f.read_text(encoding='utf-8', errors='replace')
                outputs.append({'filename': f.name, 'content': txt[:8000]})
            except Exception:
                pass
    return j(handler, {'job_id': job_id, 'outputs': outputs})


def _handle_cron_recent(handler, parsed):
    """Return cron jobs that have completed since a given timestamp."""
    import datetime
    qs = parse_qs(parsed.query)
    since = float(qs.get('since', ['0'])[0])
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from cron.jobs import list_jobs
        jobs = list_jobs(include_disabled=True)
        completions = []
        for job in jobs:
            last_run = job.get('last_run_at')
            if not last_run:
                continue
            if isinstance(last_run, str):
                try:
                    ts = datetime.datetime.fromisoformat(last_run.replace('Z', '+00:00')).timestamp()
                except (ValueError, TypeError):
                    continue
            else:
                ts = float(last_run)
            if ts > since:
                completions.append({
                    'job_id': job.get('id', ''),
                    'name': job.get('name', 'Unknown'),
                    'status': job.get('last_status', 'unknown'),
                    'completed_at': ts,
                })
        return j(handler, {'completions': completions, 'since': since})
    except ImportError:
        return j(handler, {'completions': [], 'since': since})


def _handle_memory_read(handler):
    mem_dir = Path.home() / '.hermes' / 'memories'
    mem_file = mem_dir / 'MEMORY.md'
    user_file = mem_dir / 'USER.md'
    memory = mem_file.read_text(encoding='utf-8', errors='replace') if mem_file.exists() else ''
    user = user_file.read_text(encoding='utf-8', errors='replace') if user_file.exists() else ''
    return j(handler, {
        'memory': memory, 'user': user,
        'memory_path': str(mem_file), 'user_path': str(user_file),
        'memory_mtime': mem_file.stat().st_mtime if mem_file.exists() else None,
        'user_mtime': user_file.stat().st_mtime if user_file.exists() else None,
    })


# ── POST route helpers ────────────────────────────────────────────────────────

def _handle_sessions_cleanup(handler, body, zero_only=False):
    cleaned = 0
    for p in SESSION_DIR.glob('*.json'):
        if p.name.startswith('_'): continue
        try:
            s = Session.load(p.stem)
            if zero_only:
                should_delete = s and len(s.messages) == 0
            else:
                should_delete = s and s.title == 'Untitled' and len(s.messages) == 0
            if should_delete:
                with LOCK: SESSIONS.pop(p.stem, None)
                p.unlink(missing_ok=True)
                cleaned += 1
        except Exception:
            pass
    if SESSION_INDEX_FILE.exists():
        SESSION_INDEX_FILE.unlink(missing_ok=True)
    return j(handler, {'ok': True, 'cleaned': cleaned})


def _handle_chat_start(handler, body):
    try: require(body, 'session_id')
    except ValueError as e: return bad(handler, str(e))
    try: s = get_session(body['session_id'])
    except KeyError: return bad(handler, 'Session not found', 404)
    msg = str(body.get('message', '')).strip()
    if not msg: return bad(handler, 'message is required')
    attachments = [str(a) for a in (body.get('attachments') or [])][:20]
    workspace = str(Path(body.get('workspace') or s.workspace).expanduser().resolve())
    model = body.get('model') or s.model
    s.workspace = workspace; s.model = model; s.save()
    set_last_workspace(workspace)
    
    # Read base_url from config.yaml for this model
    # cfg is a global variable loaded at module load time
    model_cfg = cfg.get('model', {})
    base_url = model_cfg.get('base_url', '')
    
    # If a model is selected that matches our custom endpoint, use that base_url AND local provider
    if base_url and model:
        # Check if the model name contains "qwen" (your local model)
        if 'qwen' in model.lower():
            # Use the local endpoint with "local" provider
            if base_url.endswith('/v1'):
                effective_base_url = base_url[:-3]
            else:
                effective_base_url = base_url + '/v1'
            effective_provider = 'custom'
            print(f"DEBUG: Using custom base_url for {model}: {effective_base_url}", file=sys.stderr)
    
    stream_id = uuid.uuid4().hex
    q = queue.Queue()
    with STREAMS_LOCK: STREAMS[stream_id] = q
    kwargs = {}
    if 'effective_base_url' in locals():
        kwargs['base_url'] = effective_base_url
    if 'effective_provider' in locals():
        kwargs['provider'] = effective_provider
    thr = threading.Thread(
        target=_run_agent_streaming,
        args=(s.session_id, msg, model, workspace, stream_id, attachments),
        kwargs=kwargs,
        daemon=True,
    )
    thr.start()
    return j(handler, {'stream_id': stream_id, 'session_id': s.session_id})


def _handle_chat_sync(handler, body):
    """Fallback synchronous chat endpoint (POST /api/chat). Not used by frontend."""
    from api.config import _get_session_agent_lock
    s = get_session(body['session_id'])
    msg = str(body.get('message', '')).strip()
    if not msg: return j(handler, {'error': 'empty message'}, status=400)
    workspace = Path(body.get('workspace') or s.workspace).expanduser().resolve()
    s.workspace = str(workspace); s.model = body.get('model') or s.model
    old_cwd = os.environ.get('TERMINAL_CWD')
    os.environ['TERMINAL_CWD'] = str(workspace)
    old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
    old_session_key = os.environ.get('HERMES_SESSION_KEY')
    os.environ['HERMES_EXEC_ASK'] = '1'
    os.environ['HERMES_SESSION_KEY'] = s.session_id
    try:
        from run_agent import AIAgent
        with CHAT_LOCK:
            from api.config import resolve_model_provider
            _model, _provider, _base_url = resolve_model_provider(s.model)
            agent = AIAgent(model=_model, provider=_provider, base_url=_base_url,
                           platform='cli', quiet_mode=True,
                           enabled_toolsets=CLI_TOOLSETS, session_id=s.session_id)
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
                user_message=workspace_ctx + msg,
                system_message=workspace_system_msg,
                conversation_history=s.messages,
                task_id=s.session_id,
                persist_user_message=msg,
            )
    finally:
        if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
        else: os.environ['TERMINAL_CWD'] = old_cwd
        if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
        else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
        if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
        else: os.environ['HERMES_SESSION_KEY'] = old_session_key
    s.messages = result.get('messages') or s.messages
    s.title = title_from(s.messages, s.title); s.save()
    return j(handler, {
        'answer': result.get('final_response') or '',
        'status': 'done' if result.get('completed', True) else 'partial',
        'session': s.compact() | {'messages': s.messages},
        'result': {k: v for k, v in result.items() if k != 'messages'},
    })


def _handle_cron_create(handler, body):
    try: require(body, 'prompt', 'schedule')
    except ValueError as e: return bad(handler, str(e))
    try:
        from cron.jobs import create_job
        job = create_job(
            prompt=body['prompt'], schedule=body['schedule'],
            name=body.get('name') or None, deliver=body.get('deliver') or 'local',
            skills=body.get('skills') or [], model=body.get('model') or None,
        )
        return j(handler, {'ok': True, 'job': job})
    except Exception as e:
        return j(handler, {'error': str(e)}, status=400)


def _handle_cron_update(handler, body):
    try: require(body, 'job_id')
    except ValueError as e: return bad(handler, str(e))
    from cron.jobs import update_job
    updates = {k: v for k, v in body.items() if k != 'job_id' and v is not None}
    job = update_job(body['job_id'], updates)
    if not job: return bad(handler, 'Job not found', 404)
    return j(handler, {'ok': True, 'job': job})


def _handle_cron_delete(handler, body):
    try: require(body, 'job_id')
    except ValueError as e: return bad(handler, str(e))
    from cron.jobs import remove_job
    ok = remove_job(body['job_id'])
    if not ok: return bad(handler, 'Job not found', 404)
    return j(handler, {'ok': True, 'job_id': body['job_id']})


def _handle_cron_run(handler, body):
    job_id = body.get('job_id', '')
    if not job_id: return bad(handler, 'job_id required')
    from cron.jobs import get_job
    from cron.scheduler import run_job
    job = get_job(job_id)
    if not job: return bad(handler, 'Job not found', 404)
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return j(handler, {'ok': True, 'job_id': job_id, 'status': 'triggered'})


def _handle_cron_pause(handler, body):
    job_id = body.get('job_id', '')
    if not job_id: return bad(handler, 'job_id required')
    from cron.jobs import pause_job
    result = pause_job(job_id, reason=body.get('reason'))
    if result: return j(handler, {'ok': True, 'job': result})
    return bad(handler, 'Job not found', 404)


def _handle_cron_resume(handler, body):
    job_id = body.get('job_id', '')
    if not job_id: return bad(handler, 'job_id required')
    from cron.jobs import resume_job
    result = resume_job(job_id)
    if result: return j(handler, {'ok': True, 'job': result})
    return bad(handler, 'Job not found', 404)


def _handle_file_delete(handler, body):
    try: require(body, 'session_id', 'path')
    except ValueError as e: return bad(handler, str(e))
    try: s = get_session(body['session_id'])
    except KeyError: return bad(handler, 'Session not found', 404)
    try:
        target = safe_resolve(Path(s.workspace), body['path'])
        if not target.exists(): return bad(handler, 'File not found', 404)
        if target.is_dir(): return bad(handler, 'Cannot delete directories via this endpoint')
        target.unlink()
        return j(handler, {'ok': True, 'path': body['path']})
    except (ValueError, PermissionError) as e: return bad(handler, str(e))


def _handle_file_save(handler, body):
    try: require(body, 'session_id', 'path')
    except ValueError as e: return bad(handler, str(e))
    try: s = get_session(body['session_id'])
    except KeyError: return bad(handler, 'Session not found', 404)
    try:
        target = safe_resolve(Path(s.workspace), body['path'])
        if not target.exists(): return bad(handler, 'File not found', 404)
        if target.is_dir(): return bad(handler, 'Cannot save: path is a directory')
        target.write_text(body.get('content', ''), encoding='utf-8')
        return j(handler, {'ok': True, 'path': body['path'], 'size': target.stat().st_size})
    except (ValueError, PermissionError) as e: return bad(handler, str(e))


def _handle_file_create(handler, body):
    try: require(body, 'session_id', 'path')
    except ValueError as e: return bad(handler, str(e))
    try: s = get_session(body['session_id'])
    except KeyError: return bad(handler, 'Session not found', 404)
    try:
        target = safe_resolve(Path(s.workspace), body['path'])
        if target.exists(): return bad(handler, 'File already exists')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.get('content', ''), encoding='utf-8')
        return j(handler, {'ok': True, 'path': str(target.relative_to(Path(s.workspace)))})
    except (ValueError, PermissionError) as e: return bad(handler, str(e))


def _handle_file_rename(handler, body):
    try: require(body, 'session_id', 'path', 'new_name')
    except ValueError as e: return bad(handler, str(e))
    try: s = get_session(body['session_id'])
    except KeyError: return bad(handler, 'Session not found', 404)
    try:
        source = safe_resolve(Path(s.workspace), body['path'])
        if not source.exists(): return bad(handler, 'File not found', 404)
        new_name = body['new_name'].strip()
        if not new_name or '/' in new_name or '..' in new_name:
            return bad(handler, 'Invalid file name')
        dest = source.parent / new_name
        if dest.exists(): return bad(handler, f'A file named "{new_name}" already exists')
        source.rename(dest)
        new_rel = str(dest.relative_to(Path(s.workspace)))
        return j(handler, {'ok': True, 'old_path': body['path'], 'new_path': new_rel})
    except (ValueError, PermissionError, OSError) as e: return bad(handler, str(e))


def _handle_create_dir(handler, body):
    try: require(body, 'session_id', 'path')
    except ValueError as e: return bad(handler, str(e))
    try: s = get_session(body['session_id'])
    except KeyError: return bad(handler, 'Session not found', 404)
    try:
        target = safe_resolve(Path(s.workspace), body['path'])
        if target.exists(): return bad(handler, 'Path already exists')
        target.mkdir(parents=True)
        return j(handler, {'ok': True, 'path': str(target.relative_to(Path(s.workspace)))})
    except (ValueError, PermissionError, OSError) as e: return bad(handler, str(e))


def _handle_workspace_add(handler, body):
    path_str = body.get('path', '').strip()
    name = body.get('name', '').strip()
    if not path_str: return bad(handler, 'path is required')
    p = Path(path_str).expanduser().resolve()
    if not p.exists(): return bad(handler, f'Path does not exist: {p}')
    if not p.is_dir(): return bad(handler, f'Path is not a directory: {p}')
    wss = load_workspaces()
    if any(w['path'] == str(p) for w in wss):
        return bad(handler, 'Workspace already in list')
    wss.append({'path': str(p), 'name': name or p.name})
    save_workspaces(wss)
    return j(handler, {'ok': True, 'workspaces': wss})


def _handle_workspace_remove(handler, body):
    path_str = body.get('path', '').strip()
    if not path_str: return bad(handler, 'path is required')
    wss = load_workspaces()
    wss = [w for w in wss if w['path'] != path_str]
    save_workspaces(wss)
    return j(handler, {'ok': True, 'workspaces': wss})


def _handle_workspace_rename(handler, body):
    path_str = body.get('path', '').strip()
    name = body.get('name', '').strip()
    if not path_str or not name: return bad(handler, 'path and name are required')
    wss = load_workspaces()
    for w in wss:
        if w['path'] == path_str:
            w['name'] = name; break
    else:
        return bad(handler, 'Workspace not found', 404)
    save_workspaces(wss)
    return j(handler, {'ok': True, 'workspaces': wss})


def _handle_approval_respond(handler, body):
    sid = body.get('session_id', '')
    if not sid: return bad(handler, 'session_id is required')
    choice = body.get('choice', 'deny')
    if choice not in ('once', 'session', 'always', 'deny'):
        return bad(handler, f'Invalid choice: {choice}')
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
    return j(handler, {'ok': True, 'choice': choice})


def _handle_skill_save(handler, body):
    try: require(body, 'name', 'content')
    except ValueError as e: return bad(handler, str(e))
    skill_name = body['name'].strip().lower().replace(' ', '-')
    if not skill_name or '/' in skill_name or '..' in skill_name:
        return bad(handler, 'Invalid skill name')
    category = body.get('category', '').strip()
    from tools.skills_tool import SKILLS_DIR
    if category:
        skill_dir = SKILLS_DIR / category / skill_name
    else:
        skill_dir = SKILLS_DIR / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / 'SKILL.md'
    skill_file.write_text(body['content'], encoding='utf-8')
    return j(handler, {'ok': True, 'name': skill_name, 'path': str(skill_file)})


def _handle_skill_delete(handler, body):
    try: require(body, 'name')
    except ValueError as e: return bad(handler, str(e))
    from tools.skills_tool import SKILLS_DIR
    import shutil
    matches = list(SKILLS_DIR.rglob(f'{body["name"]}/SKILL.md'))
    if not matches: return bad(handler, 'Skill not found', 404)
    skill_dir = matches[0].parent
    shutil.rmtree(str(skill_dir))
    return j(handler, {'ok': True, 'name': body['name']})


def _handle_memory_write(handler, body):
    try: require(body, 'section', 'content')
    except ValueError as e: return bad(handler, str(e))
    mem_dir = Path.home() / '.hermes' / 'memories'
    mem_dir.mkdir(parents=True, exist_ok=True)
    section = body['section']
    if section == 'memory':
        target = mem_dir / 'MEMORY.md'
    elif section == 'user':
        target = mem_dir / 'USER.md'
    else:
        return bad(handler, 'section must be "memory" or "user"')
    target.write_text(body['content'], encoding='utf-8')
    return j(handler, {'ok': True, 'section': section, 'path': str(target)})


def _handle_session_import(handler, body):
    """Import a session from a JSON export. Creates a new session with a new ID."""
    if not body or not isinstance(body, dict):
        return bad(handler, 'Request body must be a JSON object')
    messages = body.get('messages')
    if not isinstance(messages, list):
        return bad(handler, 'JSON must contain a "messages" array')
    title = body.get('title', 'Imported session')
    workspace = body.get('workspace', str(DEFAULT_WORKSPACE))
    model = body.get('model', DEFAULT_MODEL)
    s = Session(
        title=title, workspace=workspace, model=model,
        messages=messages,
        tool_calls=body.get('tool_calls', []),
    )
    s.pinned = body.get('pinned', False)
    with LOCK:
        SESSIONS[s.session_id] = s
        SESSIONS.move_to_end(s.session_id)
        while len(SESSIONS) > SESSIONS_MAX:
            SESSIONS.popitem(last=False)
    s.save()
    return j(handler, {'ok': True, 'session': s.compact() | {'messages': s.messages}})
