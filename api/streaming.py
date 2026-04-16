"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
import json
import logging
import os
import queue
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from api.config import (
    STREAMS, STREAMS_LOCK, CANCEL_FLAGS, AGENT_INSTANCES,
    LOCK, SESSIONS, SESSION_DIR,
    _get_session_agent_lock, _set_thread_env, _clear_thread_env,
    resolve_model_provider,
)
from api.helpers import redact_session_data

# Global lock for os.environ writes. Per-session locks (_agent_lock) prevent
# concurrent runs of the SAME session, but two DIFFERENT sessions can still
# interleave their os.environ writes. This global lock serializes the env
# save/restore around the entire agent run.
_ENV_LOCK = threading.Lock()

# Lazy import to avoid circular deps -- hermes-agent is on sys.path via api/config.py
try:
    from run_agent import AIAgent
except ImportError:
    AIAgent = None

def _get_ai_agent():
    """Return AIAgent class, retrying the import if the initial attempt failed.

    auto_install_agent_deps() in server.py may install missing packages after
    this module is first imported (common in Docker with a volume-mounted agent).
    Re-attempting the import here picks up the newly installed packages without
    requiring a server restart.
    """
    global AIAgent
    if AIAgent is None:
        try:
            from run_agent import AIAgent as _cls  # noqa: PLC0415
            AIAgent = _cls
        except ImportError:
            pass
    return AIAgent
from api.models import get_session, title_from
from api.workspace import set_last_workspace

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
_API_SAFE_MSG_KEYS = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'refusal'}


def _strip_thinking_markup(text: str) -> str:
    """Remove common reasoning/thinking wrappers from model text."""
    if not text:
        return ''
    s = str(text)
    s = re.sub(r'<think>.*?</think>', ' ', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'<\|channel\|>thought.*?<channel\|>', ' ', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'^\s*(the|ther)\s+user\s+is\s+asking.*$', ' ', s, flags=re.IGNORECASE | re.MULTILINE)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _sanitize_generated_title(text: str) -> str:
    """Sanitize LLM-generated title text before persisting to session."""
    s = _strip_thinking_markup(text or '')
    s = re.sub(r'^\s*title\s*:\s*', '', s, flags=re.IGNORECASE)
    s = s.strip(" \t\r\n\"'`")
    s = re.sub(r'\s+', ' ', s).strip()
    # Guard against chain-of-thought leakage and meta-reasoning patterns.
    if _looks_invalid_generated_title(s):
        return ''
    return s[:80]


def _looks_invalid_generated_title(text: str) -> bool:
    s = str(text or '')
    if not s.strip():
        return True
    return bool(
        re.search(r'<think>|<\|channel\|>thought', s, flags=re.IGNORECASE)
        or re.search(r'^\s*(the|ther)\s+user\s+', s, flags=re.IGNORECASE)
        or re.search(r'^\s*user\s+\w+\s+', s, flags=re.IGNORECASE)
        or re.search(r'\b(they|user)\s+want(s)?\s+me\s+to\b', s, flags=re.IGNORECASE)
        or re.search(r'^\s*(i|we)\s+(should|need to|will|can)\b', s, flags=re.IGNORECASE)
        or re.search(r'^\s*let me\b', s, flags=re.IGNORECASE)
        or re.search(r'用户(要求|希望|想让|让我)', s)
        or re.search(r'请只?回复', s)
        or re.search(r'^\s*(ok|okay|done|all set|complete|completed|finished)\b[\s.!?]*$', s, flags=re.IGNORECASE)
        or re.search(r'^\s*(好的|好啦|完成了|已完成|测试完成|测试已完成|可以了|没问题)\s*[！!。\.\s]*$', s)
    )


def _message_text(value) -> str:
    """Extract plain text from mixed message content payloads."""
    if isinstance(value, list):
        parts = []
        for p in value:
            if not isinstance(p, dict):
                continue
            ptype = str(p.get('type') or '').lower()
            if ptype in ('', 'text', 'input_text', 'output_text'):
                parts.append(str(p.get('text') or p.get('content') or ''))
        return _strip_thinking_markup('\n'.join(parts).strip())
    return _strip_thinking_markup(str(value or '').strip())


def _first_exchange_snippets(messages):
    """Return (first_user_text, first_assistant_text) snippets for title generation."""
    user_text = ''
    asst_text = ''
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'user' and not user_text:
            user_text = _message_text(m.get('content'))
        elif role == 'assistant' and not asst_text:
            asst_text = _message_text(m.get('content'))
        if user_text and asst_text:
            break
    return user_text[:500], asst_text[:500]


def _is_provisional_title(current_title: str, messages) -> bool:
    """Heuristic: title equals first-message substring placeholder."""
    derived = title_from(messages, '') or ''
    if not derived:
        return False
    return (str(current_title or '').strip() == derived[:64])


def _title_prompts(user_text: str, assistant_text: str) -> tuple[str, list[str]]:
    qa = f"User question:\n{user_text[:500]}\n\nAssistant answer:\n{assistant_text[:500]}"
    prompts = [
        (
            "Generate a short session title from this conversation start.\n"
            "Use BOTH the user's question and the assistant's visible answer.\n"
            "Return only the title text, 3-8 words, as a topic label.\n"
            "Do not output a full sentence.\n"
            "Do not output acknowledgements or completion phrases like OK, done, all set, 测试完成.\n"
            "Do not describe internal reasoning.\n"
            "Bad: The user is asking..., OK, 好的，测试完成！\n"
            "Good: 自动标题生成测试, Clarify Dialog Layout, GitHub Issue Triage"
        ),
        (
            "Rewrite this conversation start as a concise noun-phrase title.\n"
            "Use the actual topic, not the task outcome.\n"
            "Return title text only.\n"
            "Never output acknowledgements, completion status, or meta commentary."
        ),
    ]
    return qa, prompts


def _is_minimax_route(provider: str = '', model: str = '', base_url: str = '') -> bool:
    text = ' '.join([
        str(provider or '').lower(),
        str(model or '').lower(),
        str(base_url or '').lower(),
    ])
    return 'minimax' in text or 'minimaxi.com' in text


def _title_completion_budget(provider: str = '', model: str = '', base_url: str = '') -> int:
    if _is_minimax_route(provider, model, base_url):
        return 384
    return 160


def generate_title_raw_via_aux(
    user_text: str,
    assistant_text: str,
    provider: str = '',
    model: str = '',
    base_url: str = '',
) -> tuple[Optional[str], str]:
    """Return (raw_text, status) via auxiliary LLM route."""
    if not user_text or not assistant_text:
        return None, 'missing_exchange'
    qa, prompts = _title_prompts(user_text, assistant_text)
    max_tokens = _title_completion_budget(provider, model, base_url)
    reasoning_extra = {"reasoning": {"enabled": False}}
    if _is_minimax_route(provider, model, base_url):
        reasoning_extra["reasoning_split"] = True
    try:
        from agent.auxiliary_client import call_llm
        for idx, prompt in enumerate(prompts):
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": qa},
            ]
            try:
                resp = call_llm(
                    task='title_generation',
                    provider=provider or None,
                    model=model or None,
                    base_url=base_url or None,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.2,
                    timeout=15.0,
                    extra_body=reasoning_extra,
                )
                raw = ''
                try:
                    raw = resp.choices[0].message.content or ''
                except Exception:
                    raw = ''
                raw = str(raw or '').strip()
                if raw:
                    return raw, ('llm_aux' if idx == 0 else 'llm_aux_retry')
            except Exception as e:
                logger.debug("Aux title generation attempt %s failed: %s", idx + 1, e)
        return None, 'llm_error_aux'
    except Exception as e:
        logger.debug("Aux title generation failed: %s", e)
        return None, 'llm_error_aux'


def generate_title_raw_via_agent(agent, user_text: str, assistant_text: str) -> tuple[Optional[str], str]:
    """Return (raw_text, status) via active-agent route."""
    if not user_text or not assistant_text:
        return None, 'missing_exchange'
    if agent is None:
        return None, 'missing_agent'

    qa, prompts = _title_prompts(user_text, assistant_text)
    max_tokens = _title_completion_budget(
        getattr(agent, 'provider', ''),
        getattr(agent, 'model', ''),
        getattr(agent, 'base_url', ''),
    )
    disabled_reasoning = {"enabled": False}
    prev_reasoning = getattr(agent, 'reasoning_config', None)
    try:
        agent.reasoning_config = disabled_reasoning
        for idx, prompt in enumerate(prompts):
            api_messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": qa},
            ]
            try:
                raw = ""
                if getattr(agent, 'api_mode', '') == 'codex_responses':
                    codex_kwargs = agent._build_api_kwargs(api_messages)
                    codex_kwargs.pop('tools', None)
                    if 'max_output_tokens' in codex_kwargs:
                        codex_kwargs['max_output_tokens'] = max_tokens
                    resp = agent._run_codex_stream(codex_kwargs)
                    assistant_message, _ = agent._normalize_codex_response(resp)
                    raw = (assistant_message.content or '') if assistant_message else ''
                elif getattr(agent, 'api_mode', '') == 'anthropic_messages':
                    from agent.anthropic_adapter import build_anthropic_kwargs, normalize_anthropic_response
                    ant_kwargs = build_anthropic_kwargs(
                        model=agent.model,
                        messages=api_messages,
                        tools=None,
                        max_tokens=max_tokens,
                        reasoning_config=disabled_reasoning,
                        is_oauth=getattr(agent, '_is_anthropic_oauth', False),
                        preserve_dots=agent._anthropic_preserve_dots(),
                        base_url=getattr(agent, '_anthropic_base_url', None),
                    )
                    resp = agent._anthropic_messages_create(ant_kwargs)
                    assistant_message, _ = normalize_anthropic_response(
                        resp, strip_tool_prefix=getattr(agent, '_is_anthropic_oauth', False)
                    )
                    raw = (assistant_message.content or '') if assistant_message else ''
                else:
                    api_kwargs = agent._build_api_kwargs(api_messages)
                    api_kwargs.pop('tools', None)
                    api_kwargs['temperature'] = 0.1
                    api_kwargs['timeout'] = 15.0
                    if _is_minimax_route(getattr(agent, 'provider', ''), getattr(agent, 'model', ''), getattr(agent, 'base_url', '')):
                        extra_body = dict(api_kwargs.get('extra_body') or {})
                        extra_body['reasoning_split'] = True
                        api_kwargs['extra_body'] = extra_body
                    if 'max_completion_tokens' in api_kwargs:
                        api_kwargs['max_completion_tokens'] = max_tokens
                    else:
                        api_kwargs['max_tokens'] = max_tokens
                    resp = agent._ensure_primary_openai_client(reason='title_generation').chat.completions.create(
                        **api_kwargs,
                    )
                    try:
                        raw = resp.choices[0].message.content or ""
                    except Exception:
                        raw = ""
                raw = str(raw or '').strip()
                if raw:
                    return raw, ('llm' if idx == 0 else 'llm_retry')
            except Exception as e:
                logger.debug(
                    "Agent title generation attempt %s failed: provider=%s model=%s error=%s",
                    idx + 1,
                    getattr(agent, 'provider', None),
                    getattr(agent, 'model', None),
                    e,
                )
        return None, 'llm_error'
    except Exception as e:
        logger.debug("Agent title generation failed: %s", e)
        return None, 'llm_error'
    finally:
        agent.reasoning_config = prev_reasoning


def _generate_llm_session_title_for_agent(agent, user_text: str, assistant_text: str) -> tuple[Optional[str], str, str]:
    """Generate a title via active-agent route, then sanitize/validate result."""
    raw, status = generate_title_raw_via_agent(agent, user_text, assistant_text)
    if not raw:
        return None, status, ''
    title = _sanitize_generated_title(raw)
    if title:
        return title, status, ''
    return None, 'llm_invalid', str(raw)[:120]


def _generate_llm_session_title_via_aux(user_text: str, assistant_text: str, agent=None) -> tuple[Optional[str], str, str]:
    """Generate a title via dedicated auxiliary LLM route, then sanitize/validate result."""
    raw, status = generate_title_raw_via_aux(
        user_text,
        assistant_text,
        provider=getattr(agent, 'provider', '') if agent else '',
        model=getattr(agent, 'model', '') if agent else '',
        base_url=getattr(agent, 'base_url', '') if agent else '',
    )
    if not raw:
        return None, status, ''
    title = _sanitize_generated_title(raw)
    if title:
        return title, status, ''
    return None, 'llm_invalid_aux', str(raw)[:120]


def _put_title_status(put_event, session_id: str, status: str, reason: str = '', title: str = '', raw_preview: str = '') -> None:
    payload = {'session_id': session_id, 'status': status}
    if reason:
        payload['reason'] = reason
    if title:
        payload['title'] = title
    if raw_preview:
        payload['raw_preview'] = raw_preview
    put_event('title_status', payload)
    logger.info(
        "title_status session=%s status=%s reason=%s title=%r raw_preview=%r",
        session_id,
        status,
        reason or '-',
        title or '',
        (raw_preview or '')[:120],
    )


def _fallback_title_from_exchange(user_text: str, assistant_text: str) -> Optional[str]:
    """Generate a readable local fallback title when LLM title generation fails."""
    user_text = (user_text or '').strip()
    assistant_text = _strip_thinking_markup(assistant_text or '').strip()
    if not user_text:
        return None
    user_text = re.sub(r'^\[Workspace:[^\]]+\]\s*', '', user_text)
    user_text = re.sub(r'\s+', ' ', user_text).strip()
    assistant_text = re.sub(r'\s+', ' ', assistant_text).strip()
    combined = f"{user_text} {assistant_text}".strip().lower()
    combined_raw = f"{user_text} {assistant_text}".strip()

    def _extract_named_topic(text: str) -> str:
        m = re.search(r'《([^》]{2,24})》', text)
        if m:
            return (m.group(1) or '').strip()
        m = re.search(r'"([^"\n]{2,24})"', text)
        if m:
            return (m.group(1) or '').strip()
        m = re.search(r'“([^”\n]{2,24})”', text)
        if m:
            return (m.group(1) or '').strip()
        return ''

    topic_name = _extract_named_topic(combined_raw)
    if topic_name:
        if any(k in combined for k in ('时间', 'time', '安排', '效率', '怎么办', '健身', '唱歌', '写毛笔', '不够用了')):
            return f'{topic_name}与时间管理'
        if any(k in combined for k in ('hermes', 'codex', 'ai')):
            return f'{topic_name}与AI效率'
        return f'{topic_name}讨论'

    if any(k in combined for k in ('title', '标题')) and any(k in combined for k in ('summary', 'summar', '摘要', '短标题')):
        if any(k in combined for k in ('test', '测试', 'ok', '回复ok')):
            return '会话标题自动摘要测试'
        return '会话标题自动摘要'
    if any(k in combined for k in ('clarify', '澄清')) and any(k in combined for k in ('dialog', 'card', '对话', '卡片')):
        return 'Clarify 对话卡片'
    if any(k in combined for k in ('issue', 'github', 'pr')) and any(k in combined for k in ('triage', 'bug', 'review', '问题')):
        return 'GitHub Issue Triage'

    head = re.split(r'[。！？.!?\n]', user_text)[0].strip()
    if not head:
        return None

    stop_cjk = {
        '我们', '看看', '一下', '这个', '标题', '是否', '可以', '用户', '理解', '这里', '测试', '一下',
        '你只', '需要', '回复', '就可', '可以', '不需', '需要做', '什么', '自动', '成用户', '短标题',
    }
    stop_en = {
        'the', 'this', 'that', 'with', 'from', 'into', 'just', 'reply', 'please',
        'need', 'needs', 'want', 'wants', 'user', 'assistant', 'could', 'would',
        'should', 'about', 'there', 'here', 'test', 'testing', 'title', 'summary',
    }
    tokens = re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Za-z0-9][A-Za-z0-9_./+-]*', head)
    if not tokens:
        return head[:64]

    picked = []
    for tok in tokens:
        lower_tok = tok.lower()
        if re.search(r'[\u4e00-\u9fff]', tok):
            if tok in stop_cjk:
                continue
        else:
            if lower_tok in stop_en or len(lower_tok) < 3:
                continue
        if tok not in picked:
            picked.append(tok)
        if len(picked) >= 4:
            break

    if picked:
        if any(re.search(r'[\u4e00-\u9fff]', t) for t in picked):
            return ''.join(picked)[:20]
        return ' '.join(picked)[:60]
    return head[:24]


def _run_background_title_update(session_id: str, user_text: str, assistant_text: str, placeholder_title: str, put_event, agent=None):
    """Generate and publish a better title after `done`, then end the stream."""
    try:
        try:
            s = get_session(session_id)
        except KeyError:
            _put_title_status(put_event, session_id, 'skipped', 'missing_session')
            return
        # Allow self-heal when a previously generated title leaked thinking text.
        _invalid_existing = _looks_invalid_generated_title(s.title)
        if getattr(s, 'llm_title_generated', False) and not _invalid_existing:
            _put_title_status(put_event, session_id, 'skipped', 'already_generated', str(s.title or ''))
            return
        current = str(s.title or '').strip()
        still_auto = (
            current == placeholder_title
            or current in ('Untitled', 'New Chat', '')
            or _is_provisional_title(current, s.messages)
            or _invalid_existing
        )
        if not still_auto:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', current)
            return
        # Prefer the active session model when available so title generation
        # matches the user's chosen runtime and can use provider-specific fixes.
        if agent:
            next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
            if not next_title and llm_status in ('llm_error', 'llm_invalid'):
                next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent)
        else:
            next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent)
        source = llm_status
        if not next_title:
            next_title = _fallback_title_from_exchange(user_text, assistant_text)
            if next_title:
                logger.debug("Using local fallback for session title generation")
                source = 'fallback'
        if next_title and next_title != current:
            s.title = next_title
            s.llm_title_generated = True
            # Keep chronological ordering stable in the sidebar.
            s.save(touch_updated_at=False)
            if source == 'fallback':
                _put_title_status(put_event, session_id, source, 'local_summary', s.title, raw_preview)
            else:
                _put_title_status(put_event, session_id, source, llm_status, s.title, raw_preview)
            put_event('title', {'session_id': s.session_id, 'title': s.title})
        else:
            _put_title_status(put_event, session_id, 'skipped', source or 'unchanged', current, raw_preview)
    finally:
        put_event('stream_end', {'session_id': session_id})


def _sanitize_messages_for_api(messages):
    """Return a deep copy of messages with only API-safe fields.

    The webui stores extra metadata on messages (attachments, timestamp, _ts)
    for display purposes. Some providers (e.g. Z.AI/GLM) reject unknown fields
    instead of ignoring them, causing HTTP 400 errors on subsequent messages.

    Also strips orphaned tool-role messages whose tool_call_id cannot be linked
    to a preceding assistant message with tool_calls. Strictly-conformant providers
    (Mercury-2/Inception, newer OpenAI models) reject histories containing dangling
    tool results with a 400 error: "Message has tool role, but there was no previous
    assistant message with a tool call."
    """
    # First pass: collect all tool_call_ids declared by assistant messages.
    # Handles both OpenAI ('id') and Anthropic ('call_id') field names.
    valid_tool_call_ids: set = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'assistant':
            for tc in msg.get('tool_calls') or []:
                if isinstance(tc, dict):
                    tid = tc.get('id') or tc.get('call_id') or ''
                    if tid:
                        valid_tool_call_ids.add(tid)

    # Second pass: build the sanitized list, dropping orphaned tool messages.
    clean = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        if role == 'tool':
            tid = msg.get('tool_call_id') or ''
            if not tid or tid not in valid_tool_call_ids:
                # Orphaned tool result — skip to avoid 400 from strict providers.
                continue
        sanitized = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
        if sanitized.get('role'):
            clean.append(sanitized)
    return clean


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
    s = None
    _rt = {}
    old_cwd = None
    old_exec_ask = None
    old_session_key = None
    old_hermes_home = None

    # ── MCP Server Discovery (lazy import, idempotent) ──
    # discover_mcp_tools() is called here (rather than at server startup) so that
    # the hermes-agent package is fully initialized before we try to connect.
    # It is safe to call multiple times — already-connected servers are skipped.
    try:
        from tools.mcp_tool import discover_mcp_tools
        discover_mcp_tools()
    except Exception:
        pass  # MCP not available or not configured — non-fatal

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
            logger.debug("Failed to put event to queue")

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
        # Still set process-level env as fallback for tools that bypass thread-local
        # Acquire lock only for the env mutation, then release before the agent runs.
        # The finally block re-acquires to restore — keeping critical sections short
        # and preventing a deadlock where the restore would re-enter the same lock.
        with _ENV_LOCK:
            old_cwd = os.environ.get('TERMINAL_CWD')
            old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
            old_session_key = os.environ.get('HERMES_SESSION_KEY')
            old_hermes_home = os.environ.get('HERMES_HOME')
            os.environ['TERMINAL_CWD'] = str(s.workspace)
            os.environ['HERMES_EXEC_ASK'] = '1'
            os.environ['HERMES_SESSION_KEY'] = session_id
            if _profile_home:
                os.environ['HERMES_HOME'] = _profile_home
        # Lock released — agent runs without holding it
        # Register a gateway-style notify callback so the approval system can
        # push the `approval` SSE event the moment a dangerous command is
        # detected, without waiting for the next on_tool() poll cycle.
        # Without this, the agent thread blocks inside the terminal tool
        # waiting for approval that the UI never knew to ask for, leaving
        # the chat stuck in "Thinking…" forever.
        _approval_registered = False
        _unreg_notify = None
        try:
            from tools.approval import (
                register_gateway_notify as _reg_notify,
                unregister_gateway_notify as _unreg_notify,
            )
            def _approval_notify_cb(approval_data):
                put('approval', approval_data)
            _reg_notify(session_id, _approval_notify_cb)
            _approval_registered = True
        except ImportError:
            logger.debug("Approval module not available, falling back to polling")

        _clarify_registered = False
        _unreg_clarify_notify = None
        try:
            from api.clarify import (
                register_gateway_notify as _reg_clarify_notify,
                unregister_gateway_notify as _unreg_clarify_notify,
            )

            def _clarify_notify_cb(clarify_data):
                put('clarify', clarify_data)

            _reg_clarify_notify(session_id, _clarify_notify_cb)
            _clarify_registered = True
        except ImportError:
            logger.debug("Clarify module not available, falling back to polling")

        def _clarify_callback_impl(question, choices, sid, cancel_evt, put_event):
            """Bridge Hermes clarify prompts to the WebUI."""
            timeout = 120
            choices_list = [str(choice) for choice in (choices or [])]
            data = {
                'question': str(question or ''),
                'choices_offered': choices_list,
                'session_id': sid,
                'kind': 'clarify',
                'requested_at': time.time(),
            }
            try:
                from api.clarify import submit_pending as _submit_clarify_pending, clear_pending as _clear_clarify_pending
            except ImportError:
                return (
                    "The user did not provide a response within the time limit. "
                    "Use your best judgement to make the choice and proceed."
                )

            entry = _submit_clarify_pending(sid, data)
            deadline = time.monotonic() + timeout
            while True:
                if cancel_evt.is_set():
                    _clear_clarify_pending(sid)
                    return (
                        "The user did not provide a response within the time limit. "
                        "Use your best judgement to make the choice and proceed."
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _clear_clarify_pending(sid)
                    return (
                        "The user did not provide a response within the time limit. "
                        "Use your best judgement to make the choice and proceed."
                    )
                if entry.event.wait(timeout=min(1.0, remaining)):
                    response = str(entry.result or "").strip()
                    return (
                        response
                        or "The user did not provide a response within the time limit. "
                           "Use your best judgement to make the choice and proceed."
                    )

        try:
            _token_sent = False  # tracks whether any streamed tokens were sent
            _reasoning_text = ''  # accumulates reasoning/thinking trace for persistence

            def on_token(text):
                nonlocal _token_sent
                if text is None:
                    return  # end-of-stream sentinel
                _token_sent = True
                put('token', {'text': text})

            def on_reasoning(text):
                nonlocal _reasoning_text
                if text is None:
                    return
                _reasoning_text += str(text)
                put('reasoning', {'text': str(text)})

            def on_tool(*cb_args, **cb_kwargs):
                event_type = None
                name = None
                preview = None
                args = None

                if len(cb_args) >= 4:
                    event_type, name, preview, args = cb_args[:4]
                elif len(cb_args) == 3:
                    name, preview, args = cb_args
                    event_type = 'tool.started'
                elif len(cb_args) == 2:
                    event_type, name = cb_args
                elif len(cb_args) == 1:
                    name = cb_args[0]
                    event_type = 'tool.started'

                if event_type in ('reasoning.available', '_thinking'):
                    reason_text = preview if event_type == 'reasoning.available' else name
                    if reason_text:
                        put('reasoning', {'text': str(reason_text)})
                    return

                args_snap = {}
                if isinstance(args, dict):
                    for k, v in list(args.items())[:4]:
                        s2 = str(v)
                        args_snap[k] = s2[:120] + ('...' if len(s2) > 120 else '')

                if event_type in (None, 'tool.started'):
                    put('tool', {
                        'event_type': event_type or 'tool.started',
                        'name': name,
                        'preview': preview,
                        'args': args_snap,
                    })
                    # Fallback: poll for pending approval in case notify_cb wasn't
                    # registered (e.g. older approval module without gateway support).
                    try:
                        from tools.approval import has_pending as _has_pending, _pending, _lock
                        if _has_pending(session_id):
                            with _lock:
                                p = dict(_pending.get(session_id, {}))
                            if p:
                                put('approval', p)
                    except ImportError:
                        pass
                    return

                if event_type == 'tool.completed':
                    put('tool_complete', {
                        'event_type': event_type,
                        'name': name,
                        'preview': preview,
                        'args': args_snap,
                        'duration': cb_kwargs.get('duration'),
                        'is_error': bool(cb_kwargs.get('is_error', False)),
                    })
                    return

            _AIAgent = _get_ai_agent()
            if _AIAgent is None:
                raise ImportError("AIAgent not available -- check that hermes-agent is on sys.path")

            # Initialize SessionDB so session_search works in WebUI sessions
            _session_db = None
            try:
                from hermes_state import SessionDB
                _session_db = SessionDB()
            except Exception as _db_err:
                print(f"[webui] WARNING: SessionDB init failed — session_search will be unavailable: {_db_err}", flush=True)
            resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(model)

            # Resolve API key via Hermes runtime provider (matches gateway behaviour).
            # Pass the resolved provider so non-default providers get their own credentials.
            resolved_api_key = None
            try:
                from hermes_cli.runtime_provider import resolve_runtime_provider
                _rt = resolve_runtime_provider(requested=resolved_provider)
                resolved_api_key = _rt.get("api_key")
                if not resolved_provider:
                    resolved_provider = _rt.get("provider")
                if not resolved_base_url:
                    resolved_base_url = _rt.get("base_url")
            except Exception as _e:
                print(f"[webui] WARNING: resolve_runtime_provider failed: {_e}", flush=True)

            # Read per-profile config at call time (not module-level snapshot)
            from api.config import get_config as _get_config
            _cfg = _get_config()

            # Per-profile toolsets — use _resolve_cli_toolsets() so MCP
            # server toolsets are included, matching native CLI behaviour.
            from api.config import _resolve_cli_toolsets
            _toolsets = _resolve_cli_toolsets(_cfg)

            # Fallback model from profile config (e.g. for rate-limit recovery)
            _fallback = _cfg.get('fallback_model') or None
            if _fallback:
                # Resolve the fallback through our provider logic too
                fb_model = _fallback.get('model', '')
                fb_provider = _fallback.get('provider', '')
                fb_base_url = _fallback.get('base_url')
                _fallback_resolved = {
                    'model': fb_model,
                    'provider': fb_provider,
                    'base_url': fb_base_url,
                }
            else:
                _fallback_resolved = None

            agent = _AIAgent(
                model=resolved_model,
                provider=resolved_provider,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                api_mode=_rt.get('api_mode'),
                acp_command=_rt.get('command'),
                acp_args=_rt.get('args'),
                credential_pool=_rt.get('credential_pool'),
                platform='cli',
                quiet_mode=True,
                enabled_toolsets=_toolsets,
                fallback_model=_fallback_resolved,
                session_id=session_id,
                session_db=_session_db,
                stream_delta_callback=on_token,
                reasoning_callback=on_reasoning,
                tool_progress_callback=on_tool,
                clarify_callback=(
                    lambda question, choices: _clarify_callback_impl(
                        question, choices, session_id, cancel_event, put
                    )
                ),
            )

            # Store agent instance for cancel/interrupt propagation
            with STREAMS_LOCK:
                AGENT_INSTANCES[stream_id] = agent
                # Check if cancel was requested during agent initialization
                if stream_id in CANCEL_FLAGS and CANCEL_FLAGS[stream_id].is_set():
                    # Cancel arrived during agent creation - interrupt immediately
                    try:
                        agent.interrupt("Cancelled before start")
                    except Exception:
                        logger.debug("Failed to interrupt agent before start")
                    put('cancel', {'message': 'Cancelled by user'})
                    return

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
            # Resolve personality prompt from config.yaml agent.personalities
            # (matches hermes-agent CLI behavior — passes via ephemeral_system_prompt)
            _personality_prompt = None
            _pname = getattr(s, 'personality', None)
            if _pname:
                _agent_cfg = _cfg.get('agent', {})
                _personalities = _agent_cfg.get('personalities', {})
                if isinstance(_personalities, dict) and _pname in _personalities:
                    _pval = _personalities[_pname]
                    if isinstance(_pval, dict):
                        _parts = [_pval.get('system_prompt', '') or _pval.get('prompt', '')]
                        if _pval.get('tone'):
                            _parts.append(f'Tone: {_pval["tone"]}')
                        if _pval.get('style'):
                            _parts.append(f'Style: {_pval["style"]}')
                        _personality_prompt = '\n'.join(p for p in _parts if p)
                    else:
                        _personality_prompt = str(_pval)
            # Pass personality via ephemeral_system_prompt (agent's own mechanism)
            if _personality_prompt:
                agent.ephemeral_system_prompt = _personality_prompt
            result = agent.run_conversation(
                user_message=workspace_ctx + msg_text,
                system_message=workspace_system_msg,
                conversation_history=_sanitize_messages_for_api(s.messages),
                task_id=session_id,
                persist_user_message=msg_text,
            )
            s.messages = result.get('messages') or s.messages

            # ── Detect silent agent failure (no assistant reply produced) ──
            # When the agent catches an auth/network error internally it may return
            # an empty final_response without raising — the stream would end with
            # a done event containing zero assistant messages, leaving the user with
            # no feedback. Emit an apperror so the client shows an inline error.
            _assistant_added = any(
                m.get('role') == 'assistant' and str(m.get('content') or '').strip()
                for m in (result.get('messages') or [])
            )
            # _token_sent tracks whether on_token() was called (any streamed text)
            if not _assistant_added and not _token_sent:
                _last_err = getattr(agent, '_last_error', None) or result.get('error') or ''
                _err_str = str(_last_err) if _last_err else ''
                _is_auth = (
                    '401' in _err_str
                    or (_last_err and 'AuthenticationError' in type(_last_err).__name__)
                    or 'authentication' in _err_str.lower()
                    or 'unauthorized' in _err_str.lower()
                    or 'invalid api key' in _err_str.lower()
                    or 'invalid_api_key' in _err_str.lower()
                )
                if _is_auth:
                    put('apperror', {
                        'message': _err_str or 'Authentication failed — check your API key.',
                        'type': 'auth_mismatch',
                        'hint': (
                            'The selected model may not be supported by your configured provider or '
                            'your API key is invalid. Run `hermes model` in your terminal to '
                            'update credentials, then restart the WebUI.'
                        ),
                    })
                else:
                    put('apperror', {
                        'message': _err_str or 'The agent returned no response. Check your API key and model selection.',
                        'type': 'no_response',
                        'hint': 'Verify your API key is valid and the selected model is available for your account.',
                    })
                return  # Don't emit done — the apperror already closes the stream on the client

            # ── Handle context compression side effects ──
            # If compression fired inside run_conversation, the agent may have
            # rotated its session_id. Detect and fix the mismatch so the WebUI
            # continues writing to the correct session file.
            _agent_sid = getattr(agent, 'session_id', None)
            _compressed = False
            if _agent_sid and _agent_sid != session_id:
                old_sid = session_id
                new_sid = _agent_sid
                # Rename the session file
                old_path = SESSION_DIR / f'{old_sid}.json'
                new_path = SESSION_DIR / f'{new_sid}.json'
                s.session_id = new_sid
                with LOCK:
                    if old_sid in SESSIONS:
                        SESSIONS[new_sid] = SESSIONS.pop(old_sid)
                if old_path.exists() and not new_path.exists():
                    try:
                        old_path.rename(new_path)
                    except OSError:
                        logger.debug("Failed to rename session file during compression")
                _compressed = True
            # Also detect compression via the result dict or compressor state
            if not _compressed:
                _compressor = getattr(agent, 'context_compressor', None)
                if _compressor and getattr(_compressor, 'compression_count', 0) > 0:
                    _compressed = True
            # Notify the frontend that compression happened
            if _compressed:
                put('compressed', {
                    'message': 'Context auto-compressed to continue the conversation',
                })

            # Stamp 'timestamp' on any messages that don't have one yet
            _now = time.time()
            for _m in s.messages:
                if isinstance(_m, dict) and not _m.get('timestamp') and not _m.get('_ts'):
                    _m['timestamp'] = int(_now)
            # Only auto-generate title when still default; preserves user renames
            if s.title == 'Untitled' or s.title == 'New Chat' or not s.title:
                s.title = title_from(s.messages, s.title)
            _looks_default = (s.title == 'Untitled' or s.title == 'New Chat' or not s.title)
            _looks_provisional = _is_provisional_title(s.title, s.messages)
            _invalid_existing_title = _looks_invalid_generated_title(s.title)
            _should_bg_title = (
                (_looks_default or _looks_provisional or _invalid_existing_title)
                and (not getattr(s, 'llm_title_generated', False) or _invalid_existing_title)
            )
            _u0 = ''
            _a0 = ''
            if _should_bg_title:
                _u0, _a0 = _first_exchange_snippets(s.messages)
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
            tool_calls = []
            pending_names = {}   # tool_call_id -> name
            pending_args = {}    # tool_call_id -> args dict
            pending_asst_idx = {} # tool_call_id -> index in s.messages
            for msg_idx, m in enumerate(s.messages):
                if m.get('role') == 'assistant':
                    c = m.get('content', '')
                    # Anthropic format: content is a list with type=tool_use blocks
                    if isinstance(c, list):
                        for p in c:
                            if isinstance(p, dict) and p.get('type') == 'tool_use':
                                tid = p.get('id', '')
                                pending_names[tid] = p.get('name', '')
                                pending_args[tid] = p.get('input', {})
                                pending_asst_idx[tid] = msg_idx
                    # OpenAI format: tool_calls as top-level field on the message
                    for tc in m.get('tool_calls', []):
                        if not isinstance(tc, dict):
                            continue
                        tid = tc.get('id', '') or tc.get('call_id', '')
                        fn = tc.get('function', {})
                        name = fn.get('name', '')
                        try:
                            import json as _j
                            args = _j.loads(fn.get('arguments', '{}') or '{}')
                        except Exception:
                            args = {}
                        if tid and name:
                            pending_names[tid] = name
                            pending_args[tid] = args
                            pending_asst_idx[tid] = msg_idx
                elif m.get('role') == 'tool':
                    tid = m.get('tool_call_id') or m.get('tool_use_id', '')
                    name = pending_names.get(tid, '')
                    if not name or name == 'tool':
                        continue  # skip unresolvable tool entries
                    asst_idx = pending_asst_idx.get(tid, -1)
                    args = pending_args.get(tid, {})
                    raw = str(m.get('content', ''))
                    try:
                        rd = json.loads(raw)
                        snippet = str(rd.get('output') or rd.get('result') or rd.get('error') or raw)[:200]
                    except Exception:
                        snippet = raw[:200]
                    # Truncate args values for storage
                    args_snap = {}
                    if isinstance(args, dict):
                        for k, v in list(args.items())[:6]:
                            s2 = str(v)
                            args_snap[k] = s2[:120] + ('...' if len(s2) > 120 else '')
                    tool_calls.append({
                        'name': name, 'snippet': snippet, 'tid': tid,
                        'assistant_msg_idx': asst_idx, 'args': args_snap,
                    })
            s.tool_calls = tool_calls
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            # Tag the matching user message with attachment filenames for display on reload
            # Only tag a user message whose content relates to this turn's text
            # (msg_text is the full message including the [Attached files: ...] suffix)
            if attachments:
                for m in reversed(s.messages):
                    if m.get('role') == 'user':
                        content = str(m.get('content', ''))
                        # Match if content is part of the sent message or vice-versa
                        base_text = msg_text.split('\n\n[Attached files:')[0].strip() if '\n\n[Attached files:' in msg_text else msg_text
                        if base_text[:60] in content or content[:60] in msg_text:
                            m['attachments'] = attachments
                            break
            s.save()
            # Sync to state.db for /insights (opt-in setting)
            try:
                from api.config import load_settings as _load_settings
                if _load_settings().get('sync_to_insights'):
                    from api.state_sync import sync_session_usage
                    sync_session_usage(
                        session_id=s.session_id,
                        input_tokens=s.input_tokens or 0,
                        output_tokens=s.output_tokens or 0,
                        estimated_cost=s.estimated_cost,
                        model=model,
                        title=s.title,
                        message_count=len(s.messages),
                    )
            except Exception:
                logger.debug("Failed to sync session to insights")
            usage = {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'estimated_cost': estimated_cost}
            # Include context window data from the agent's compressor for the UI indicator
            _cc = getattr(agent, 'context_compressor', None)
            if _cc:
                usage['context_length'] = getattr(_cc, 'context_length', 0) or 0
                usage['threshold_tokens'] = getattr(_cc, 'threshold_tokens', 0) or 0
                usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
            # Persist reasoning trace in the session so it survives reload
            if _reasoning_text and s.messages:
                for _rm in reversed(s.messages):
                    if isinstance(_rm, dict) and _rm.get('role') == 'assistant':
                        _rm['reasoning'] = _reasoning_text
                        break
            raw_session = s.compact() | {'messages': s.messages, 'tool_calls': tool_calls}
            put('done', {'session': redact_session_data(raw_session), 'usage': usage})
            if _should_bg_title and _u0 and _a0:
                threading.Thread(
                    target=_run_background_title_update,
                    args=(s.session_id, _u0, _a0, str(s.title or '').strip(), put, agent),
                    daemon=True,
                ).start()
            else:
                put('stream_end', {'session_id': s.session_id})
        finally:
            # Unregister the gateway approval callback and unblock any threads
            # still waiting on approval (e.g. stream cancelled mid-approval).
            if _approval_registered and _unreg_notify is not None:
                try:
                    _unreg_notify(session_id)
                except Exception:
                    logger.debug("Failed to unregister approval callback")
            if _clarify_registered and _unreg_clarify_notify is not None:
                try:
                    _unreg_clarify_notify(session_id)
                except Exception:
                    logger.debug("Failed to unregister clarify callback")
            with _ENV_LOCK:
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
        if s is not None:
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            try:
                s.save()
            except Exception:
                pass
        err_str = str(e)
        # Detect rate limit errors specifically so the client can show a helpful card
        # rather than the generic "Connection lost" message
        is_rate_limit = 'rate limit' in err_str.lower() or '429' in err_str or 'RateLimitError' in type(e).__name__
        is_auth_error = (
            '401' in err_str
            or 'AuthenticationError' in type(e).__name__
            or 'authentication' in err_str.lower()
            or 'unauthorized' in err_str.lower()
            or 'invalid api key' in err_str.lower()
            or 'no cookie auth credentials' in err_str.lower()
        )
        if is_rate_limit:
            put('apperror', {
                'message': err_str,
                'type': 'rate_limit',
                'hint': 'Rate limit reached. The fallback model (if configured) was also exhausted. Try again in a moment.',
            })
        elif is_auth_error:
            put('apperror', {
                'message': err_str,
                'type': 'auth_mismatch',
                'hint': (
                    'The selected model may not be supported by your configured provider. '
                    'Run `hermes model` in your terminal to switch providers, then restart the WebUI.'
                ),
            })
        else:
            put('apperror', {'message': err_str, 'type': 'error'})
    finally:
        _clear_thread_env()  # TD1: always clear thread-local context
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)
            AGENT_INSTANCES.pop(stream_id, None)  # Clean up agent instance reference

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

        # Set WebUI layer cancel flag
        flag = CANCEL_FLAGS.get(stream_id)
        if flag:
            flag.set()

        # Interrupt the AIAgent instance to stop tool execution
        agent = AGENT_INSTANCES.get(stream_id)
        if agent:
            try:
                agent.interrupt("Cancelled by user")
            except Exception as e:
                # Log but don't block the cancel flow
                import logging
                logging.getLogger(__name__).debug(
                    f"Failed to interrupt agent for stream {stream_id}: {e}"
                )
        else:
            # Agent not yet stored - cancel_event flag will be checked by agent thread
            import logging
            logging.getLogger(__name__).debug(
                f"Cancel requested for stream {stream_id} before agent ready - "
                f"cancel_event flag set, will be checked on agent startup"
            )

        # Clear any pending clarify prompt so the blocked tool call can unwind.
        try:
            from api.clarify import clear_pending as _clear_clarify_pending

            if agent and getattr(agent, "session_id", None):
                _clear_clarify_pending(agent.session_id)
        except Exception:
            logger.debug("Failed to clear clarify prompt during cancel")

        # Put a cancel sentinel into the queue so the SSE handler wakes up
        q = STREAMS.get(stream_id)
        if q:
            try:
                q.put_nowait(('cancel', {'message': 'Cancelled by user'}))
            except Exception:
                logger.debug("Failed to put cancel event to queue")
    return True
