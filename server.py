"""
Hermes Co-Work Web UI -- Main server entry point.
HTTP Handler (routing) + startup. All business logic lives in api/*.
"""
import json
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── API modules ───────────────────────────────────────────────────────────────
from api.config import (
    HOST, PORT, STATE_DIR, SESSION_DIR, DEFAULT_WORKSPACE, DEFAULT_MODEL,
    SESSIONS, SESSIONS_MAX, LOCK, STREAMS, STREAMS_LOCK, CANCEL_FLAGS,
    SERVER_START_TIME, CLI_TOOLSETS, _INDEX_HTML_PATH,
    IMAGE_EXTS, MD_EXTS, MIME_MAP, MAX_FILE_BYTES, MAX_UPLOAD_BYTES,
    _get_session_agent_lock, SESSION_AGENT_LOCKS, SESSION_AGENT_LOCKS_LOCK,
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
from api.upload import parse_multipart, handle_upload
from api.streaming import _sse, _run_agent_streaming, cancel_stream

# Approval system
try:
    from tools.approval import (
        has_pending, pop_pending, submit_pending,
        approve_session, approve_permanent, save_permanent_allowlist,
        is_approved,
    )
except ImportError:
    def has_pending(*a, **k): return False
    def pop_pending(*a, **k): return None
    def submit_pending(*a, **k): pass
    def approve_session(*a, **k): pass
    def approve_permanent(*a, **k): pass
    def save_permanent_allowlist(*a, **k): pass
    def is_approved(*a, **k): return True


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
            if parsed.path == '/favicon.ico':
                self.send_response(204); self.end_headers(); return
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
                s = get_session(sid); return j(self, {'session': s.compact() | {'messages': s.messages, 'tool_calls': getattr(s, 'tool_calls', [])}})
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
            if parsed.path == '/api/chat/cancel':
                # Sprint 10: cancel an in-flight stream
                stream_id = parse_qs(parsed.query).get('stream_id', [''])[0]
                if not stream_id:
                    return bad(self, 'stream_id required')
                cancelled = cancel_stream(stream_id)
                return j(self, {'ok': True, 'cancelled': cancelled, 'stream_id': stream_id})
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
                        if event in ('done', 'error', 'cancel'): break
                except (BrokenPipeError, ConnectionResetError): pass
                return
            if parsed.path == '/api/file/raw':
                # Serve raw file bytes (for images and downloads).
                # Pass ?download=1 to force Content-Disposition: attachment (save to disk).
                qs = parse_qs(parsed.query)
                _raw_sid = qs.get('session_id', [''])[0]
                if not _raw_sid: return bad(self, 'session_id is required')
                try: s = get_session(_raw_sid)
                except KeyError: return bad(self, 'Session not found', 404)
                rel = qs.get('path', [''])[0]
                force_download = qs.get('download', [''])[0] == '1'
                target = safe_resolve(Path(s.workspace), rel)
                if not target.exists() or not target.is_file():
                    return j(self, {'error': 'not found'}, status=404)
                ext = target.suffix.lower()
                mime = MIME_MAP.get(ext, 'application/octet-stream')
                raw_bytes = target.read_bytes()
                import urllib.parse as _up
                safe_name = _up.quote(target.name, safe='')
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(raw_bytes)))
                self.send_header('Cache-Control', 'no-store')
                if force_download:
                    self.send_header('Content-Disposition', f'attachment; filename="{target.name}"; filename*=UTF-8\'\'{safe_name}')
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
                # Invalidate index so the deleted session stops appearing in lists
                try: SESSION_INDEX_FILE.unlink(missing_ok=True)
                except Exception: pass
                return j(self, {'ok': True})
            if parsed.path == '/api/session/clear':
                # Wipe all messages from a session, keep session metadata
                try: require(body, 'session_id')
                except ValueError as e: return bad(self, str(e))
                try: s = get_session(body['session_id'])
                except KeyError: return bad(self, 'Session not found', 404)
                s.messages = []
                s.tool_calls = []
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
                attachments = [str(a) for a in (body.get('attachments') or [])][:20]
                workspace = str(Path(body.get('workspace') or s.workspace).expanduser().resolve())
                model = body.get('model') or s.model
                s.workspace = workspace; s.model = model; s.save()
                set_last_workspace(workspace)  # persist for new session inheritance
                stream_id = uuid.uuid4().hex
                q = queue.Queue()
                with STREAMS_LOCK: STREAMS[stream_id] = q
                t = threading.Thread(target=_run_agent_streaming,
                    args=(s.session_id, msg, model, workspace, stream_id, attachments), daemon=True)
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
    from api.config import print_startup_config, verify_hermes_imports, _HERMES_FOUND
    print_startup_config()

    if not _HERMES_FOUND:
        ok, missing = verify_hermes_imports()
    else:
        ok, missing = verify_hermes_imports()
        if not ok:
            print(f'[!!] Warning: Hermes agent found but missing modules: {missing}', flush=True)
            print('     Agent features may not work correctly.', flush=True)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'  Hermes Co-Work listening on http://{HOST}:{PORT}', flush=True)
    if HOST == '127.0.0.1':
        print(f'  Remote access: ssh -N -L {PORT}:127.0.0.1:{PORT} <user>@<your-server>', flush=True)
    print(f'  Then open:     http://localhost:{PORT}', flush=True)
    print('', flush=True)
    httpd.serve_forever()

if __name__ == '__main__':
    main()
