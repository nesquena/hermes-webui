"""
Hermes Web UI -- SSE streaming engine and agent thread runner.

Uses hermes_cli bootstrap layer for credential resolution, fallback model,
reasoning config, and all other AIAgent parameters -- so the WebUI stays
in sync with the CLI as Hermes evolves.

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
    resolve_model_provider, cfg,
)

# ---------------------------------------------------------------------------
# Per-stream clarify state -- maps stream_id to a dict with question/choices
# and a threading.Event + response slot so the agent thread can block.
# ---------------------------------------------------------------------------
CLARIFY_PENDING: dict = {}   # stream_id -> {question, choices, response, event}
CLARIFY_LOCK = threading.Lock()

# Lazy import to avoid circular deps -- hermes-agent is on sys.path via api/config.py
try:
    from run_agent import AIAgent
except ImportError:
    AIAgent = None
try:
    from hermes_state import SessionDB
    _session_db = SessionDB()
except Exception:
    _session_db = None
from api.models import get_session, title_from, derive_tool_calls
from api.workspace import set_last_workspace

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
_API_SAFE_MSG_KEYS = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'refusal'}


def _sanitize_messages_for_api(messages):
    """Return a deep copy of messages with only API-safe fields.

    The webui stores extra metadata on messages (attachments, timestamp, _ts)
    for display purposes. Some providers (e.g. Z.AI/GLM) reject unknown fields
    instead of ignoring them, causing HTTP 400 errors on subsequent messages.
    """
    clean = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sanitized = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
        if sanitized.get('role'):
            clean.append(sanitized)
    return clean


# ---------------------------------------------------------------------------
# Bootstrap helpers -- import hermes_cli modules for proper credential
# resolution, config merging, and fallback handling.  These are the same
# code paths the CLI uses, so the WebUI inherits all provider support
# (OAuth, credential pools, custom base URLs, codex, etc.) automatically.
# ---------------------------------------------------------------------------

def _resolve_credentials(provider_hint: str = "auto"):
    """Resolve runtime credentials via hermes_cli.runtime_provider.

    Returns a dict with api_key, base_url, provider, api_mode, command,
    args, credential_pool -- or None if resolution fails.
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        return resolve_runtime_provider(requested=provider_hint)
    except Exception:
        return None


def _load_hermes_config():
    """Load config via hermes_cli.config.load_config (with DEFAULT_CONFIG merge).

    Falls back to the raw cfg dict from api/config.py on import failure.
    """
    try:
        from hermes_cli.config import load_config
        return load_config()
    except Exception:
        return cfg or {}


def _get_fallback_model(hermes_cfg):
    """Extract fallback_providers / fallback_model from hermes config."""
    fb = hermes_cfg.get("fallback_providers") or hermes_cfg.get("fallback_model") or []
    if isinstance(fb, dict):
        fb = [fb] if fb.get("provider") and fb.get("model") else []
    return fb


def _get_reasoning_config(hermes_cfg):
    """Parse reasoning_effort from config into an OpenRouter reasoning dict."""
    effort = (hermes_cfg.get("agent") or {}).get("reasoning_effort", "")
    if not effort:
        return None
    try:
        from hermes_constants import parse_reasoning_effort
        return parse_reasoning_effort(effort)
    except Exception:
        return None


def _make_clarify_callback(stream_id, put_fn):
    """Create a clarify_callback that sends an SSE event and blocks until the
    frontend responds via POST /api/clarify/respond.

    Timeout: 120s (same as CLI default).  On timeout, returns a polite
    "decide yourself" message so the agent doesn't hang forever.
    """
    def _clarify_cb(question: str, choices=None) -> str:
        event = threading.Event()
        with CLARIFY_LOCK:
            CLARIFY_PENDING[stream_id] = {
                'question': question,
                'choices': choices,
                'response': None,
                'event': event,
            }
        # Send SSE event to frontend
        put_fn('clarify', {
            'question': question,
            'choices': choices or [],
            'stream_id': stream_id,
        })
        # Block until frontend responds or timeout
        answered = event.wait(timeout=120)
        with CLARIFY_LOCK:
            state = CLARIFY_PENDING.pop(stream_id, {})
        if answered and state.get('response') is not None:
            return state['response']
        return (
            "The user did not provide a response within the time limit. "
            "Use your best judgement to make the choice and proceed."
        )
    return _clarify_cb


def _generate_title_async(session_id: str, messages: list, put_fn=None):
    """Fire-and-forget: ask Claude (via OAuth) to generate a short session title.

    Uses the same credential resolution path as the main agent so it
    automatically picks up Claude Max OAuth / any configured provider.

    Sends a 'title' SSE event if put_fn is provided (live session).
    Also writes the title back to SessionDB so it persists.
    """
    def _run():
        try:
            # Build a short summary of the conversation for the prompt
            snippets = []
            for m in messages[:6]:  # first 3 exchanges max
                role = m.get('role', '')
                if role not in ('user', 'assistant'):
                    continue
                c = m.get('content', '')
                if isinstance(c, list):
                    c = ' '.join(
                        p.get('text', '') for p in c
                        if isinstance(p, dict) and p.get('type') == 'text'
                    )
                text = str(c).strip()[:300]
                if text:
                    snippets.append(f"{role}: {text}")
            if not snippets:
                return

            conversation = '\n'.join(snippets)
            prompt = (
                f"Generate a concise session title (max 60 chars, no quotes) "
                f"that captures the main topic of this conversation:\n\n{conversation}"
            )

            creds = _resolve_credentials()
            if not creds:
                return

            provider = creds.get('provider', '')
            api_key = creds.get('api_key') or ''
            base_url = creds.get('base_url') or 'https://api.anthropic.com'
            api_mode = creds.get('api_mode', '')

            title = None

            # Anthropic native or OAuth path
            if provider in ('anthropic', 'claude_max', '') or api_mode in ('anthropic', 'oauth'):
                try:
                    import anthropic
                    kwargs = {'base_url': base_url}
                    if api_key:
                        kwargs['api_key'] = api_key
                    elif api_mode == 'oauth':
                        kwargs['auth_token'] = creds.get('auth_token', '')
                    client = anthropic.Anthropic(**kwargs)
                    resp = client.messages.create(
                        model='claude-haiku-4-5',
                        max_tokens=64,
                        messages=[{'role': 'user', 'content': prompt}],
                    )
                    title = resp.content[0].text.strip().strip('"\'')
                except Exception:
                    pass

            # OpenAI-compatible fallback (OpenRouter, custom, etc.)
            if not title:
                try:
                    import openai
                    client = openai.OpenAI(api_key=api_key or 'none', base_url=base_url)
                    resp = client.chat.completions.create(
                        model=creds.get('model', 'gpt-4o-mini'),
                        max_tokens=64,
                        messages=[{'role': 'user', 'content': prompt}],
                    )
                    title = resp.choices[0].message.content.strip().strip('"\'')
                except Exception:
                    pass

            if not title:
                return

            # Clamp to 64 chars
            title = title[:64]

            # Persist to DB -- do NOT bump updated_at, title is metadata only
            try:
                from api.models import get_session
                s = get_session(session_id)
                s.title = title
                s.save(touch_updated_at=False)
            except Exception:
                pass

            # Push SSE event to live stream if available
            if put_fn:
                put_fn('title', {'session_id': session_id, 'title': title})

        except Exception:
            pass  # Never crash the caller

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _resolve_honcho_session_key(workspace: str, session_id: str) -> str:
    """Derive a Honcho session key matching the CLI's per-directory strategy.

    Uses the workspace basename (e.g. 'hermes-webui', 'brand-sharing-platform')
    so that sessions in the same workspace share Honcho memory context, just
    like the CLI does with CWD.
    """
    basename = Path(workspace).name if workspace else ''
    return basename or f"webui:{session_id}"


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

        # Resolve profile home for this agent run (snapshot at start)
        try:
            from api.profiles import get_active_hermes_home
            _profile_home = str(get_active_hermes_home())
        except ImportError:
            _profile_home = os.environ.get('HERMES_HOME', '')

        _set_thread_env(
            TERMINAL_CWD=str(s.workspace),
            HERMES_EXEC_ASK='1',
            HERMES_SESSION_KEY=session_id,
            HERMES_HOME=_profile_home,
        )
        # Set process-level env vars needed by tools (terminal, approval).
        # HERMES_SESSION_KEY is set per-agent-lock so the approval module
        # routes blocking approvals to the right session queue.
        with _agent_lock:
          old_cwd = os.environ.get('TERMINAL_CWD')
          old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
          old_session_key = os.environ.get('HERMES_SESSION_KEY')
          old_hermes_home = os.environ.get('HERMES_HOME')
          os.environ['TERMINAL_CWD'] = str(s.workspace)
          os.environ['HERMES_EXEC_ASK'] = '1'
          os.environ['HERMES_SESSION_KEY'] = session_id
          if _profile_home:
              os.environ['HERMES_HOME'] = _profile_home

          # Register blocking approval callback: when the terminal tool hits a
          # dangerous command, the agent thread blocks on entry.event.wait() and
          # emits an 'approval' SSE event to the frontend. The user clicks a
          # button which calls /api/approval/respond -> resolve_gateway_approval()
          # -> sets entry.event, unblocking the thread so execution continues.
          try:
              from tools.approval import register_gateway_notify, unregister_gateway_notify
              register_gateway_notify(session_id, lambda data: put('approval', data))
              _gateway_notify_registered = True
          except ImportError:
              _gateway_notify_registered = False

          try:
            def on_token(text):
                if text is None:
                    # end-of-stream sentinel: signal that assistant turn is ending
                    # before tools are about to run
                    put('turn_end', {})
                    return
                put('token', {'text': text})

            def on_tool_start(tc_id, name, args):
                """Called when a tool is about to start (has tool_call_id)."""
                args_snap = {}
                if isinstance(args, dict):
                    for k, v in list(args.items())[:8]:
                        if isinstance(v, str) and len(v) > 500:
                            args_snap[k] = v[:500] + '...'
                        else:
                            args_snap[k] = v
                preview = ''
                # Build preview from args for display
                if name == 'execute_code' and 'code' in args:
                    preview = args['code'].split('\n')[0][:80]
                elif name == 'terminal' and 'command' in args:
                    preview = args['command'][:80]
                elif name == 'read_file' and 'path' in args:
                    preview = args['path'][:80]
                elif name == 'search_files' and 'pattern' in args:
                    preview = args['pattern'][:80]
                elif name == 'web_search' and 'query' in args:
                    preview = args['query'][:80]
                put('tool', {
                    'tid': tc_id,
                    'name': name,
                    'preview': preview,
                    'args': args_snap
                })
                # also check for pending approval and surface it immediately
                from tools.approval import has_pending as _has_pending, _pending, _lock
                if _has_pending(session_id):
                    with _lock:
                        p = dict(_pending.get(session_id, {}))
                    if p:
                        put('approval', p)

            def on_tool_complete(tc_id, name, args, result):
                """Called when a tool finishes execution with its result."""
                snippet = str(result)[:4000] if result else ''
                put('tool_done', {
                    'tid': tc_id,
                    'name': name,
                    'snippet': snippet,
                })

            if AIAgent is None:
                raise ImportError("AIAgent not available -- check that hermes-agent is on sys.path")

            # Use upstream's resolve_model_provider for model/provider/base_url
            resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(model)

            # --- hermes_cli bootstrap (same path as the CLI) ---
            # 1. Split provider/model from the UI selector string
            if '/' in model:
                _ui_provider, _model = model.split('/', 1)
            else:
                _model = model
                _ui_provider = ''

            # 2. Load full hermes config (with DEFAULT_CONFIG merge)
            hermes_cfg = _load_hermes_config()

            # Read per-profile config at call time (not module-level snapshot)
            from api.config import get_config as _get_config
            _cfg = _get_config()

            # Per-profile toolsets (fall back to module-level CLI_TOOLSETS)
            _pt = _cfg.get('platform_toolsets', {})
            _toolsets = _pt.get('cli', CLI_TOOLSETS) if isinstance(_pt, dict) else CLI_TOOLSETS

            # 3. Resolve runtime credentials via hermes_cli.runtime_provider
            #    This handles: env vars, OAuth tokens (Nous), Copilot ACP,
            #    credential pools, custom base URLs, etc.
            runtime = _resolve_credentials(provider_hint=_ui_provider or 'auto')

            # 4. Build AIAgent kwargs from resolved runtime + config
            agent_kwargs = dict(
                model=resolved_model,
                platform='cli',
                quiet_mode=True,
                enabled_toolsets=_toolsets,
                session_id=session_id,
                session_db=_session_db,
                stream_delta_callback=on_token,
                tool_start_callback=on_tool_start,
                tool_complete_callback=on_tool_complete,
                clarify_callback=_make_clarify_callback(stream_id, put),
                fallback_model=_get_fallback_model(hermes_cfg),
                reasoning_config=_get_reasoning_config(hermes_cfg),
            )

            # 5. Inject resolved credentials (if available)
            if runtime:
                agent_kwargs.update(
                    api_key=runtime.get('api_key'),
                    base_url=runtime.get('base_url'),
                    provider=runtime.get('provider'),
                    api_mode=runtime.get('api_mode'),
                    acp_command=runtime.get('command'),
                    acp_args=runtime.get('args'),
                    credential_pool=runtime.get('credential_pool'),
                )
            else:
                # Fallback: use upstream's resolved provider/base_url
                agent_kwargs['provider'] = resolved_provider
                agent_kwargs['base_url'] = resolved_base_url

            agent = AIAgent(**agent_kwargs)
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
            # Filter out internal hermes roles that the API doesn't understand
            # (session_meta, etc.) and messages with None content which cause
            # TypeError: 'NoneType' object is not iterable in _build_api_kwargs.
            _PASSTHROUGH_ROLES = {'user', 'assistant', 'tool', 'system'}
            clean_history = [
                m for m in s.messages
                if m and m.get('role') in _PASSTHROUGH_ROLES and m.get('content') is not None
            ]
            result = agent.run_conversation(
                user_message=workspace_ctx + msg_text,
                system_message=workspace_system_msg,
                conversation_history=_sanitize_messages_for_api(clean_history),
                task_id=session_id,
                persist_user_message=msg_text,
            )
            s.messages = result.get('messages') or s.messages
            # Stamp 'timestamp' on any messages that don't have one yet
            _now = time.time()
            for _m in s.messages:
                if isinstance(_m, dict) and not _m.get('timestamp') and not _m.get('_ts'):
                    _m['timestamp'] = int(_now)
            # Only auto-generate title if still untitled - preserve manual renames
            if s.title in ('Untitled', '', None):
                _generate_title_async(session_id, s.messages, put_fn=put)
            # Read token/cost usage from the agent object (if available)
            input_tokens = getattr(agent, 'session_prompt_tokens', 0) or 0
            output_tokens = getattr(agent, 'session_completion_tokens', 0) or 0
            estimated_cost = getattr(agent, 'session_estimated_cost_usd', None)
            s.input_tokens = (s.input_tokens or 0) + input_tokens
            s.output_tokens = (s.output_tokens or 0) + output_tokens
            if estimated_cost:
                s.estimated_cost = (s.estimated_cost or 0) + estimated_cost
            # Extract tool call metadata grouped by assistant message index
            # Each tool call gets assistant_msg_idx so the client can render
            # cards inline with the assistant bubble that triggered them.
            tool_calls = derive_tool_calls(s.messages)
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
            s.save()
            # Include token usage from agent result for context display
            _cc = getattr(agent, 'context_compressor', None)
            usage = {
                'input_tokens': result.get('input_tokens', 0) or input_tokens,
                'output_tokens': result.get('output_tokens', 0) or output_tokens,
                'total_tokens': result.get('total_tokens', 0),
                'cache_read_tokens': result.get('cache_read_tokens', 0),
                'cache_write_tokens': result.get('cache_write_tokens', 0),
                'last_prompt_tokens': result.get('last_prompt_tokens', 0),
                'context_length': getattr(_cc, 'context_length', 0) or 0,
                'threshold_tokens': getattr(_cc, 'threshold_tokens', 0) or 0,
                'estimated_cost_usd': result.get('estimated_cost_usd', 0) or estimated_cost,
            }
            put('done', {
                'session': s.compact() | {'messages': s.messages, 'tool_calls': tool_calls},
                'usage': usage,
            })
          finally:
            if _gateway_notify_registered:
                try: unregister_gateway_notify(session_id)
                except Exception: pass
            if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
            else: os.environ['TERMINAL_CWD'] = old_cwd
            if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
            else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
            if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
            else: os.environ['HERMES_SESSION_KEY'] = old_session_key
            if old_hermes_home is None: os.environ.pop('HERMES_HOME', None)
            else: os.environ['HERMES_HOME'] = old_hermes_home

    except Exception as e:
        print('[webui] stream error:\n' + traceback.format_exc(), flush=True)
        err_str = str(e)
        # Detect rate limit errors specifically so the client can show a helpful card
        # rather than the generic "Connection lost" message
        is_rate_limit = 'rate limit' in err_str.lower() or '429' in err_str or 'RateLimitError' in type(e).__name__
        if is_rate_limit:
            put('apperror', {
                'message': err_str,
                'type': 'rate_limit',
                'hint': 'Rate limit reached. The fallback model (if configured) was also exhausted. Try again in a moment.',
            })
        else:
            put('apperror', {'message': err_str, 'type': 'error'})
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


def respond_clarify(stream_id: str, response: str) -> bool:
    """Submit a user response to a pending clarify question.

    Returns True if a pending clarify was found and answered.
    """
    with CLARIFY_LOCK:
        state = CLARIFY_PENDING.get(stream_id)
        if not state:
            return False
        state['response'] = response
        state['event'].set()
    return True


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
