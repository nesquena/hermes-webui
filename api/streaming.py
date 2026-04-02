"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
import json
import os
import queue
import threading
import time
import traceback
from pathlib import Path

from api.config import (
    STREAMS, STREAMS_LOCK, CANCEL_FLAGS, CLI_TOOLSETS,
    _get_session_agent_lock, _set_thread_env, _clear_thread_env,
    DEFAULT_PROVIDER, DEFAULT_BASE_URL,
)

# Lazy import to avoid circular deps -- hermes-agent is on sys.path via api/config.py
try:
    from run_agent import AIAgent
except ImportError:
    AIAgent = None
from api.models import get_session, title_from
from api.workspace import set_last_workspace


def _sse(handler, event, data):
    """Write one SSE event to the response stream."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()


def _run_agent_streaming(session_id, msg_text, model, workspace, stream_id, attachments=None):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id]."""
    q = STREAMS.get(stream_id)
    if q is None:
        return

    # Sprint 10: create a cancel event for this stream
    cancel_event = threading.Event()
    with STREAMS_LOCK:
        CANCEL_FLAGS[stream_id] = cancel_event

    def put(event, data):
        # If cancelled, drop all further events except the cancel event itself
        if cancel_event.is_set() and event not in ('cancel', 'error'):
            return
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
        # Check for pre-flight cancel (user cancelled before agent even started)
        if cancel_event.is_set():
            put('cancel', {'message': 'Cancelled before start'})
            return

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
                args_snap = {}
                if isinstance(args, dict):
                    for k, v in list(args.items())[:4]:
                        s2 = str(v); args_snap[k] = s2[:120]+('...' if len(s2)>120 else '')
                put('tool', {'name': name, 'preview': preview, 'args': args_snap})
                # also check for pending approval and surface it immediately
                from tools.approval import has_pending as _has_pending, _pending, _lock
                if _has_pending(session_id):
                    with _lock:
                        p = dict(_pending.get(session_id, {}))
                    if p:
                        put('approval', p)

            if AIAgent is None:
                raise ImportError("AIAgent not available -- check that hermes-agent is on sys.path")
            agent = AIAgent(
                model=model,
                provider=DEFAULT_PROVIDER or None,
                base_url=DEFAULT_BASE_URL or None,
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
            # Extract tool call metadata grouped by assistant message index
            # Each tool call gets assistant_msg_idx so the client can render
            # cards inline with the assistant bubble that triggered them.
            tool_calls = []
            pending_names = {}   # tool_call_id -> name
            pending_asst_idx = {} # tool_call_id -> index in s.messages
            for msg_idx, m in enumerate(s.messages):
                if m.get('role') == 'assistant':
                    c = m.get('content', '')
                    if isinstance(c, list):
                        for p in c:
                            if isinstance(p, dict) and p.get('type') == 'tool_use':
                                tid = p.get('id', '')
                                pending_names[tid] = p.get('name', 'tool')
                                pending_asst_idx[tid] = msg_idx
                elif m.get('role') == 'tool':
                    tid = m.get('tool_call_id') or m.get('tool_use_id', '')
                    name = pending_names.get(tid, 'tool')
                    asst_idx = pending_asst_idx.get(tid, -1)
                    raw = str(m.get('content', ''))
                    try:
                        import json as _j2
                        rd = _j2.loads(raw)
                        snippet = str(rd.get('output') or rd.get('result') or rd.get('error') or raw)[:200]
                    except Exception:
                        snippet = raw[:200]
                    tool_calls.append({
                        'name': name, 'snippet': snippet, 'tid': tid,
                        'assistant_msg_idx': asst_idx,
                    })
            s.tool_calls = tool_calls
            # Tag the matching user message with attachment filenames for display on reload
            # Only tag a user message whose content relates to this turn's text
            # (msg_text is the full message including the [Attached files: ...] suffix)
            if attachments:
                for m in reversed(s.messages):
                    if m.get('role') == 'user':
                        content = str(m.get('content', ''))
                        # Match if content is part of the sent message or vice-versa
                        base_text = msg_text.split('\n\n[Attached files:')[0].strip()
                        if base_text[:60] in content or content[:60] in msg_text:
                            m['attachments'] = attachments
                            break
            # Extract usage stats from the agent result
            usage_stats = {
                'prompt_tokens': result.get('prompt_tokens', 0),
                'completion_tokens': result.get('completion_tokens', 0),
                'total_tokens': result.get('total_tokens', 0),
                'input_tokens': result.get('input_tokens', 0),
                'output_tokens': result.get('output_tokens', 0),
                'cache_read_tokens': result.get('cache_read_tokens', 0),
                'cache_write_tokens': result.get('cache_write_tokens', 0),
                'reasoning_tokens': result.get('reasoning_tokens', 0),
                'estimated_cost_usd': result.get('estimated_cost_usd', 0),
                'api_calls': result.get('api_calls', 0),
            }
            # Accumulate usage on the session object for /usage endpoint
            if not hasattr(s, 'usage_stats') or not isinstance(s.usage_stats, dict):
                s.usage_stats = {'prompt_tokens': 0, 'completion_tokens': 0,
                                 'total_tokens': 0, 'input_tokens': 0,
                                 'output_tokens': 0, 'cache_read_tokens': 0,
                                 'cache_write_tokens': 0, 'reasoning_tokens': 0,
                                 'estimated_cost_usd': 0, 'api_calls': 0}
            for k in list(s.usage_stats):
                s.usage_stats[k] = s.usage_stats.get(k, 0) + usage_stats.get(k, 0)
            s.save()
            put('done', {
                'session': s.compact() | {'messages': s.messages, 'tool_calls': tool_calls},
                'usage': usage_stats,
            })
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
            CANCEL_FLAGS.pop(stream_id, None)

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================


def cancel_stream(stream_id: str) -> bool:
    """Signal an in-flight stream to cancel. Returns True if the stream existed."""
    with STREAMS_LOCK:
        if stream_id not in STREAMS:
            return False
        flag = CANCEL_FLAGS.get(stream_id)
        if flag:
            flag.set()
        # Put a cancel sentinel into the queue so the SSE handler wakes up
        q = STREAMS.get(stream_id)
        if q:
            try:
                q.put_nowait(('cancel', {'message': 'Cancelled by user'}))
            except Exception:
                pass
    return True
