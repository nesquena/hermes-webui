"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
import contextlib
import json
import logging
import os
import queue
import re
import threading
import time
import traceback
import copy
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from api.config import (
    STREAMS, STREAMS_LOCK, CANCEL_FLAGS, AGENT_INSTANCES, STREAM_PARTIAL_TEXT,
    LOCK, SESSIONS, SESSION_DIR,
    _get_session_agent_lock, _set_thread_env, _clear_thread_env,
    SESSION_AGENT_LOCKS, SESSION_AGENT_LOCKS_LOCK,
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
    s = re.sub(r'<\|turn\|>thinking\n.*?<turn\|>', ' ', s, flags=re.IGNORECASE | re.DOTALL)  # Gemma 4
    s = re.sub(r'^\s*(the|ther)\s+user\s+is\s+asking.*$', ' ', s, flags=re.IGNORECASE | re.MULTILINE)
    # Strip plain-text thinking preambles from models that don't use <think> tags (e.g. Qwen3).
    # These appear as the very first sentence of the assistant response and are not useful as titles.
    s = re.sub(
        r"^\s*(?:here(?:'s| is) (?:a |my )?(?:thinking|thought) (?:process|trace|through)\b[^\n]*\n?"
        r"|let me (?:think|work|reason|analyze|walk) (?:through|about|this|step)\b[^\n]*\n?"
        r"|i(?:'ll| will) (?:think|work|reason|analyze|break this down)\b[^\n]*\n?"
        r"|(?:okay|alright|sure|of course),?\s+let me\b[^\n]*\n?)",
        ' ', s, flags=re.IGNORECASE
    )
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _strip_xml_tool_calls(text: str) -> str:
    """Strip XML-style function_calls blocks that DeepSeek and similar models
    emit in their raw response text.  These blocks are processed separately as
    tool calls; leaving them in the assistant content causes them to render
    visibly in the chat bubble.

    Handles both complete blocks (<function_calls>…</function_calls>) and
    partial/orphaned opening tags that may appear at the tail of a stream.
    """
    if not text or '<function_calls>' not in text.lower():
        return text
    s = str(text)
    # Strip complete blocks (possibly multiple)
    s = re.sub(r'<function_calls>.*?</function_calls>', '', s, flags=re.IGNORECASE | re.DOTALL)
    # Strip orphaned opening tags (stream cut off before closing tag)
    s = re.sub(r'<function_calls>.*$', '', s, flags=re.IGNORECASE | re.DOTALL)
    return s.strip()


def _sanitize_generated_title(text: str) -> str:
    """Sanitize LLM-generated title text before persisting to session."""
    s = _strip_thinking_markup(text or '')
    s = re.sub(
        r'^\s*(?:[*_`~]+\s*)?(?:session\s+title|title)\s*:\s*(?:[*_`~]+\s*)?',
        '',
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r'^\s*title\s*:\s*', '', s, flags=re.IGNORECASE)
    s = s.strip(" \t\r\n\"'`*_~")
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
        re.search(r'<think>|<\|channel\|>thought|<\|turn\|>thinking', s, flags=re.IGNORECASE)
        or re.search(r'^\s*(the|ther)\s+user\s+', s, flags=re.IGNORECASE)
        or re.search(r'^\s*user\s+\w+\s+', s, flags=re.IGNORECASE)
        or re.search(r'\b(they|user)\s+want(s)?\s+me\s+to\b', s, flags=re.IGNORECASE)
        or re.search(r'^\s*(i|we)\s+(should|need to|will|can)\b', s, flags=re.IGNORECASE)
        or re.search(r'^\s*let me\b', s, flags=re.IGNORECASE)
        or re.search(r"^\s*here(?:'s| is) (?:a |my )?(?:thinking|thought)", s, flags=re.IGNORECASE)
        or re.search(r'^\s*(ok|okay|done|all set|complete|completed|finished)\b[\s.!?]*$', s, flags=re.IGNORECASE)
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
    """Return (first_user_text, first_assistant_text) snippets for title generation.

    Prefer the first substantive assistant answer in the opening exchange,
    skipping empty placeholders and assistant tool-call preambles.
    """
    user_text = ''
    asst_text = ''
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'user':
            candidate = _message_text(m.get('content'))
            if not user_text and candidate:
                user_text = candidate
                continue
            if user_text and candidate:
                break
        elif role == 'assistant' and user_text:
            candidate = _message_text(m.get('content'))
            # Skip tool-call preambles *only* when content is empty or looks
            # like meta-reasoning ("Let me check my memory first.", "The user
            # is asking...", etc.). Assistant rows that carry tool_calls but
            # also contain a substantive answer text are kept — those are
            # agentic first-turn plans that are legitimate title candidates.
            if m.get('tool_calls') and (not candidate or _looks_invalid_generated_title(candidate)):
                continue
            if candidate:
                asst_text = candidate
        if user_text and asst_text:
            break
    return user_text[:500], asst_text[:500]


def _is_provisional_title(current_title: str, messages) -> bool:
    """Heuristic: title equals first-message substring placeholder."""
    derived = title_from(messages, '') or ''
    if not derived:
        return False
    current = re.sub(r'\s+', ' ', str(current_title or '')).strip()
    candidate = re.sub(r'\s+', ' ', str(derived[:64] or '')).strip()
    if not current or not candidate:
        return False
    return current == candidate or candidate.startswith(current)


def _title_prompts(user_text: str, assistant_text: str) -> tuple[str, list[str]]:
    qa = f"User question:\n{user_text[:500]}\n\nAssistant answer:\n{assistant_text[:500]}"
    prompts = [
        (
            "Generate a short session title from this conversation start.\n"
            "Use BOTH the user's question and the assistant's visible answer.\n"
            "Return only the title text, 3-8 words, as a topic label.\n"
            "Do not use markdown, bullets, labels, or prefixes like Session Title:.\n"
            "Do not output a full sentence.\n"
            "Do not output acknowledgements or completion phrases like OK, done, or all set.\n"
            "Do not describe internal reasoning.\n"
            "Bad: The user is asking..., OK, all set.\n"
            "Good: Title Generation Test, Clarify Dialog Layout, GitHub Issue Triage"
        ),
        (
            "Rewrite this conversation start as a concise noun-phrase title.\n"
            "Use the actual topic, not the task outcome.\n"
            "Return title text only.\n"
            "Do not use markdown, bullets, labels, or prefixes like Session Title:.\n"
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


def _aux_title_configured() -> bool:
    """Return True when any auxiliary title_generation config field is meaningfully set."""
    try:
        from agent.auxiliary_client import _get_auxiliary_task_config
        tg = _get_auxiliary_task_config('title_generation')
        provider = tg.get('provider', '') or ''
        model = tg.get('model', '') or ''
        base_url = tg.get('base_url', '') or ''
        return bool(model or base_url or (provider and provider.lower() != 'auto'))
    except Exception:
        return False

def _aux_title_timeout(default: float = 15.0) -> float:
    """Return the configured timeout (seconds) for auxiliary title generation.

    Only accepts positive numeric values.  Falls back to *default* when the
    value is ``None``, non-numeric, zero, or negative, and emits a debug log
    so mis-configurations are visible in server output.
    """
    try:
        from agent.auxiliary_client import _get_auxiliary_task_config
        tg = _get_auxiliary_task_config('title_generation')
        raw = tg.get('timeout')
        if raw is None:
            return default
        try:
            value = float(raw)
        except (ValueError, TypeError):
            logger.debug("aux title timeout: non-numeric value %r, falling back to %s", raw, default)
            return default
        if value > 0:
            return value
        logger.debug("aux title timeout: non-positive value %s, falling back to %s", value, default)
        return default
    except Exception:
        return default

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
        _timeout = _aux_title_timeout()
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
                    timeout=_timeout,
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


def _generate_llm_session_title_via_aux(user_text: str, assistant_text: str, agent=None, *, use_agent_model: bool = False) -> tuple[Optional[str], str, str]:
    """Generate a title via dedicated auxiliary LLM route, then sanitize/validate result.

    When use_agent_model is False (default), the auxiliary client resolves
    provider/model/base_url from config.yaml auxiliary.title_generation, which
    prevents the session's chat model (e.g. a Chinese model) from overriding
    the dedicated title model.  When True, the agent's attrs are passed through
    (legacy fallback behaviour).
    """
    if use_agent_model and agent:
        provider = getattr(agent, 'provider', '')
        model = getattr(agent, 'model', '')
        base_url = getattr(agent, 'base_url', '')
    else:
        provider = ''
        model = ''
        base_url = ''
    raw, status = generate_title_raw_via_aux(
        user_text,
        assistant_text,
        provider=provider,
        model=model,
        base_url=base_url,
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

    def _contains_latin(text: str) -> bool:
        return bool(re.search(r'[A-Za-z]', text or ''))

    def _extract_named_topic(text: str) -> str:
        m = re.search(r'"([^"\n]{2,24})"', text)
        if m:
            return (m.group(1) or '').strip()
        m = re.search(r'“([^”\n]{2,24})”', text)
        if m:
            return (m.group(1) or '').strip()
        return ''

    topic_name = _extract_named_topic(combined_raw)
    if topic_name:
        if not _contains_latin(topic_name):
            if any(k in combined for k in ('time', 'schedule', 'efficiency', 'manage', 'fitness', 'singing', 'calligraphy')):
                return 'Time management discussion'
            if any(k in combined for k in ('hermes', 'codex', 'ai')):
                return 'AI productivity discussion'
            return 'Conversation topic'
        if any(k in combined for k in ('time', 'schedule', 'efficiency', 'manage', 'fitness', 'singing', 'calligraphy')):
            return f'{topic_name} time management'
        if any(k in combined for k in ('hermes', 'codex', 'ai')):
            return f'{topic_name} AI productivity'
        return f'{topic_name} discussion'

    if any(k in combined for k in ('title', 'session title')) and any(k in combined for k in ('summary', 'summar', 'short title')):
        if any(k in combined for k in ('test', 'ok', 'reply ok')):
            return 'Session title auto-summary test'
        return 'Session title auto-summary'
    if any(k in combined for k in ('clarify', 'clarification')) and any(k in combined for k in ('dialog', 'card')):
        return 'Clarify dialog card'
    if any(k in combined for k in ('issue', 'github', 'pr')) and any(k in combined for k in ('triage', 'bug', 'review')):
        return 'GitHub Issue Triage'

    head = re.split(r'[.!?\n]', user_text)[0].strip()
    if not head:
        return None

    stop_en = {
        'the', 'this', 'that', 'with', 'from', 'into', 'just', 'reply', 'please',
        'need', 'needs', 'want', 'wants', 'user', 'assistant', 'could', 'would',
        'should', 'about', 'there', 'here', 'test', 'testing', 'title', 'summary',
    }
    tokens = re.findall(r'[A-Za-z0-9][A-Za-z0-9_./+-]*', head)
    if not tokens:
        return 'Conversation topic'

    picked = []
    for tok in tokens:
        lower_tok = tok.lower()
        if lower_tok in stop_en or len(lower_tok) < 3:
            continue
        if tok not in picked:
            picked.append(tok)
        if len(picked) >= 4:
            break

    if picked:
        return ' '.join(picked)[:60]
    return 'Conversation topic'


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
        aux_title_configured = _aux_title_configured()
        if agent and not aux_title_configured:
            next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
            if not next_title and llm_status in ('llm_error', 'llm_invalid'):
                next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent, use_agent_model=True)
        else:
            next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text)
            if not next_title and agent and llm_status in ('llm_error_aux', 'llm_invalid_aux'):
                next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
        source = llm_status
        if not next_title:
            next_title = _fallback_title_from_exchange(user_text, assistant_text)
            if next_title:
                logger.debug("Using local fallback for session title generation")
                source = 'fallback'
        wrote_title = False
        effective_title = current
        if next_title:
            with _get_session_agent_lock(session_id):
                with LOCK:
                    s = SESSIONS.get(session_id, s)
                    effective_title = str(s.title or '').strip()
                    invalid_existing_now = _looks_invalid_generated_title(s.title)
                    still_auto = (
                        effective_title == placeholder_title
                        or effective_title in ('Untitled', 'New Chat', '')
                        or _is_provisional_title(effective_title, s.messages)
                        or invalid_existing_now
                    )
                if not still_auto:
                    _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective_title)
                    return
                if next_title != effective_title:
                    s.title = next_title
                    s.llm_title_generated = True
                    # Keep chronological ordering stable in the sidebar.
                    s.save(touch_updated_at=False)
                    effective_title = s.title
                    wrote_title = True

        if wrote_title:
            if source == 'fallback':
                _put_title_status(put_event, session_id, source, 'local_summary', effective_title, raw_preview)
            else:
                _put_title_status(put_event, session_id, source, llm_status, effective_title, raw_preview)
            put_event('title', {'session_id': session_id, 'title': effective_title})
        else:
            _put_title_status(put_event, session_id, 'skipped', source or 'unchanged', effective_title, raw_preview)
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
        # Skip persisted error markers — never send them to the LLM as prior context.
        if msg.get('_error'):
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


def _api_safe_message_positions(messages):
    """Return [(original_index, sanitized_message)] for API-safe messages."""
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

    out = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get('role')
        if role == 'tool':
            tid = msg.get('tool_call_id') or ''
            if not tid or tid not in valid_tool_call_ids:
                continue
        sanitized = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
        if sanitized.get('role'):
            out.append((idx, sanitized))
    return out


def _restore_reasoning_metadata(previous_messages, updated_messages):
    """Carry forward display-only metadata lost during API-safe history sanitization.

    The provider-facing history strips WebUI-only fields like `reasoning`. When the
    agent returns its new full message history, prior assistant messages come back
    without that metadata unless we merge it back in by API-history position.

    This also preserves existing timestamps for unchanged historical messages.
    Without that, older turns that come back from the agent without `_ts` /
    `timestamp` can be re-stamped with the current time on every new assistant
    response, making prior messages appear to "move" in time.
    """
    if not previous_messages or not updated_messages:
        return updated_messages
    updated_messages = list(updated_messages)
    prev_safe = _api_safe_message_positions(previous_messages)

    def _safe_projection(msg):
        if not isinstance(msg, dict):
            return None
        return {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS and msg.get('role')}

    def _reasoning_only_assistant(msg):
        if not isinstance(msg, dict) or msg.get('role') != 'assistant' or not msg.get('reasoning'):
            return False
        if msg.get('tool_calls'):
            return False
        return not _message_text(msg.get('content'))

    safe_pos = 0
    while safe_pos < len(prev_safe):
        prev_idx, _ = prev_safe[safe_pos]
        prev_msg = previous_messages[prev_idx]
        cur_msg = updated_messages[safe_pos] if safe_pos < len(updated_messages) else None

        if isinstance(prev_msg, dict) and isinstance(cur_msg, dict) and _safe_projection(prev_msg) == _safe_projection(cur_msg):
            if prev_msg.get('role') == 'assistant' and prev_msg.get('reasoning') and not cur_msg.get('reasoning'):
                cur_msg['reasoning'] = prev_msg['reasoning']
            if prev_msg.get('timestamp') and not cur_msg.get('timestamp'):
                cur_msg['timestamp'] = prev_msg['timestamp']
            elif prev_msg.get('_ts') and not cur_msg.get('_ts') and not cur_msg.get('timestamp'):
                cur_msg['_ts'] = prev_msg['_ts']
            safe_pos += 1
            continue

        if _reasoning_only_assistant(prev_msg):
            updated_messages.insert(safe_pos, copy.deepcopy(prev_msg))
            safe_pos += 1
            continue

        safe_pos += 1
    return updated_messages


def _tool_result_snippet(raw) -> str:
    """Extract a compact result preview from a stored tool message payload."""
    text = str(raw or '')
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return str(data.get('output') or data.get('result') or data.get('error') or text)[:200]
    except Exception:
        pass
    return text[:200]


def _truncate_tool_args(args, limit: int = 6) -> dict:
    """Truncate tool args for compact session persistence."""
    out = {}
    if not isinstance(args, dict):
        return out
    for k, v in list(args.items())[:limit]:
        s = str(v)
        out[k] = s[:120] + ('...' if len(s) > 120 else '')
    return out


def _nearest_assistant_msg_idx(messages, msg_idx: int) -> int:
    """Find the closest preceding assistant message index for a tool result."""
    for idx in range(msg_idx - 1, -1, -1):
        msg = messages[idx]
        if isinstance(msg, dict) and msg.get('role') == 'assistant':
            return idx
    return -1


def _extract_tool_calls_from_messages(messages, live_tool_calls=None):
    """Build persisted tool-call summaries from final messages plus live progress fallback."""
    tool_calls = []
    pending_names = {}
    pending_args = {}
    pending_asst_idx = {}
    tool_msg_sequence = []

    for msg_idx, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'assistant':
            content = m.get('content', '')
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'tool_use':
                        tid = part.get('id', '')
                        if tid:
                            pending_names[tid] = part.get('name', '')
                            pending_args[tid] = part.get('input', {})
                            pending_asst_idx[tid] = msg_idx
            for tc in m.get('tool_calls', []):
                if not isinstance(tc, dict):
                    continue
                tid = tc.get('id', '') or tc.get('call_id', '')
                fn = tc.get('function', {})
                name = fn.get('name', '')
                try:
                    args = json.loads(fn.get('arguments', '{}') or '{}')
                except Exception:
                    args = {}
                if tid and name:
                    pending_names[tid] = name
                    pending_args[tid] = args
                    pending_asst_idx[tid] = msg_idx
        elif role == 'tool':
            tid = m.get('tool_call_id') or m.get('tool_use_id', '')
            raw = m.get('content', '')
            seq = {'msg_idx': msg_idx, 'raw': raw, 'resolved': False}
            if tid:
                name = pending_names.get(tid, '')
                if name and name != 'tool':
                    tool_calls.append({
                        'name': name,
                        'snippet': _tool_result_snippet(raw),
                        'tid': tid,
                        'assistant_msg_idx': pending_asst_idx.get(tid, -1),
                        'args': _truncate_tool_args(pending_args.get(tid, {})),
                    })
                    seq['resolved'] = True
            tool_msg_sequence.append(seq)

    live = [tc for tc in (live_tool_calls or []) if isinstance(tc, dict) and tc.get('name') and tc.get('name') != 'clarify']
    if live:
        for seq_idx, seq in enumerate(tool_msg_sequence):
            if seq.get('resolved'):
                continue
            if seq_idx >= len(live):
                break
            live_tc = live[seq_idx]
            tool_calls.append({
                'name': live_tc.get('name', 'tool'),
                'snippet': _tool_result_snippet(seq.get('raw', '')),
                'tid': live_tc.get('tid', '') or '',
                'assistant_msg_idx': _nearest_assistant_msg_idx(messages, seq.get('msg_idx', -1)),
                'args': _truncate_tool_args(live_tc.get('args', {}), limit=4),
            })

    return tool_calls


def _sse(handler, event, data):
    """Write one SSE event to the response stream."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    handler.wfile.write(payload.encode('utf-8'))
    handler.wfile.flush()


def _run_agent_streaming(session_id, msg_text, model, workspace, stream_id, attachments=None, *, ephemeral=False):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id].

    When ephemeral=True, session mutations are skipped — used by /btw to get
    a streaming answer without persisting to the parent session.
    """
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
        STREAM_PARTIAL_TEXT[stream_id] = ''  # start accumulating partial text (#893)

    def put(event, data):
        # If cancelled, drop all further events except the cancel event itself
        if cancel_event.is_set() and event not in ('cancel', 'error'):
            return
        try:
            q.put_nowait((event, data))
        except Exception:
            logger.debug("Failed to put event to queue")

    # Initialised here (before any code that may raise) so the outer `finally`
    # block can safely check `if _checkpoint_stop is not None` even when an
    # exception fires before the checkpoint thread is created (Issue #765).
    _checkpoint_stop = None
    _ckpt_thread = None
    _agent_lock = None
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

        # Resolve profile home for this agent run — use the session's own profile
        # (stamped at new_session() time from the client's S.activeProfile) so that
        # two concurrent tabs on different profiles don't clobber each other via the
        # process-level active-profile global.  Falls back gracefully.
        try:
            from api.profiles import get_hermes_home_for_profile
            _profile_home = str(get_hermes_home_for_profile(getattr(s, 'profile', None)))
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
            _live_tool_calls = []  # tool progress fallback when final messages omit tool IDs

            def on_token(text):
                nonlocal _token_sent
                if text is None:
                    return  # end-of-stream sentinel
                _token_sent = True
                # Accumulate partial text so cancel_stream() can persist it (#893)
                if stream_id in STREAM_PARTIAL_TEXT:
                    STREAM_PARTIAL_TEXT[stream_id] += str(text)
                put('token', {'text': text})

            def on_reasoning(text):
                nonlocal _reasoning_text
                if text is None:
                    return
                _reasoning_text += str(text)
                put('reasoning', {'text': str(text)})

            # Pre-initialise the activity counter here so on_tool (which
            # closes over it) never captures an unbound name even if this
            # block is reordered later (Issue #765).
            _checkpoint_activity = [0]

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
                    _live_tool_calls.append({
                        'name': name,
                        'args': args if isinstance(args, dict) else {},
                    })
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
                    for live_tc in reversed(_live_tool_calls):
                        if live_tc.get('done'):
                            continue
                        if not name or live_tc.get('name') == name:
                            live_tc['done'] = True
                            live_tc['duration'] = cb_kwargs.get('duration')
                            live_tc['is_error'] = bool(cb_kwargs.get('is_error', False))
                            break
                    # Signal the checkpoint thread that new work has completed (Issue #765).
                    # Each completed tool call is a meaningful unit of progress worth persisting.
                    _checkpoint_activity[0] += 1
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

            # Build kwargs defensively — guard newer params so the WebUI
            # degrades gracefully when run against an older hermes-agent build.
            # (fixes: TypeError: AIAgent.__init__() got an unexpected keyword
            # argument 'credential_pool' — issue #772)
            import inspect as _inspect
            _agent_params = set(_inspect.signature(_AIAgent.__init__).parameters)

            # CLI-parity reasoning effort: read agent.reasoning_effort from the
            # active profile's config.yaml (the same key the CLI writes via
            # `/reasoning <level>`) and hand the parsed dict to AIAgent.  When
            # the key is absent or invalid, pass None → agent uses its default.
            try:
                from api.config import parse_reasoning_effort as _parse_reff
                _effort_cfg = _cfg.cfg.get('agent', {}) if isinstance(_cfg.cfg, dict) else {}
                _effort_raw = _effort_cfg.get('reasoning_effort') if isinstance(_effort_cfg, dict) else None
                _reasoning_config = _parse_reff(_effort_raw)
            except Exception:
                _reasoning_config = None

            _agent_kwargs = dict(
                model=resolved_model,
                provider=resolved_provider,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
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
            # reasoning_config has been an AIAgent param for several releases,
            # but guard defensively to avoid TypeError on an older agent build.
            if 'reasoning_config' in _agent_params and _reasoning_config is not None:
                _agent_kwargs['reasoning_config'] = _reasoning_config
            # Params added in newer hermes-agent — skip if not supported
            if 'api_mode' in _agent_params:
                _agent_kwargs['api_mode'] = _rt.get('api_mode')
            if 'acp_command' in _agent_params:
                _agent_kwargs['acp_command'] = _rt.get('command')
            if 'acp_args' in _agent_params:
                _agent_kwargs['acp_args'] = _rt.get('args')
            if 'credential_pool' in _agent_params:
                _agent_kwargs['credential_pool'] = _rt.get('credential_pool')
            # Pin Honcho memory sessions to the stable WebUI session ID.
            # Without this, 'per-session' Honcho strategy creates a new Honcho
            # session on every streaming request because HonchoSessionManager is
            # re-instantiated fresh each turn (#855).
            if 'gateway_session_key' in _agent_params:
                _agent_kwargs['gateway_session_key'] = session_id

            agent = _AIAgent(**_agent_kwargs)

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
            _previous_messages = list(s.messages or [])

            # ── Periodic checkpoint during streaming (Issue #765) ──
            # The agent works on an internal copy of s.messages during run_conversation()
            # so we cannot watch s.messages for growth. Instead, on_tool() increments
            # _checkpoint_activity[0] each time a tool call completes — that is the real
            # signal that progress has been made worth persisting.
            #
            # What gets saved on each checkpoint:
            #   - s.pending_user_message (already written before run starts)
            #   - s.pending_started_at / s.active_stream_id (turn bookkeeping)
            # On a server restart the UI will see a session with a pending message and no
            # response — better than a silent loss of the entire conversation turn.
            # The final s.save() at task completion handles the full session update + index.
            # (_checkpoint_stop is pre-initialised at the top of the outer try.)
            # (_checkpoint_activity is already initialised before on_tool().)

            def _periodic_checkpoint():
                last_saved_activity = 0
                while not _checkpoint_stop.wait(15):
                    try:
                        cur = _checkpoint_activity[0]
                        if cur > last_saved_activity:
                            with _agent_lock:
                                s.save(skip_index=True)
                            last_saved_activity = cur
                    except Exception as e:
                        logger.debug("Periodic checkpoint save failed: %s", e)

            _checkpoint_stop = threading.Event()
            _ckpt_thread = threading.Thread(
                target=_periodic_checkpoint, daemon=True,
                name=f"ckpt-{session_id[:8]}",
            )
            _ckpt_thread.start()

            result = agent.run_conversation(
                user_message=workspace_ctx + msg_text,
                system_message=workspace_system_msg,
                conversation_history=_sanitize_messages_for_api(s.messages),
                task_id=session_id,
                persist_user_message=msg_text,
            )
            # ── Ephemeral mode (/btw): deliver answer, skip persistence, cleanup ──
            if ephemeral:
                _answer = ''
                for _m in reversed(result.get('messages') or []):
                    if isinstance(_m, dict) and _m.get('role') == 'assistant':
                        _answer = str(_m.get('content', ''))
                        break
                put('done', {
                    'session': {'session_id': session_id, 'messages': result.get('messages', [])},
                    'usage': {'input_tokens': 0, 'output_tokens': 0},
                    'ephemeral': True,
                    'answer': _answer,
                })
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()
                try:
                    import pathlib
                    pathlib.Path(s.path).unlink(missing_ok=True)
                except Exception:
                    pass
                return  # skip all normal persistence for ephemeral sessions
            if _checkpoint_stop is not None:
                _checkpoint_stop.set()
            if _ckpt_thread is not None:
                _ckpt_thread.join(timeout=15)
            with _agent_lock:
                s.messages = _restore_reasoning_metadata(
                    _previous_messages,
                    result.get('messages') or s.messages,
                )
                # Strip XML tool-call blocks from assistant message content.
                # DeepSeek and some other providers emit <function_calls>...</function_calls>
                # in the raw response text; this must be removed before the content is
                # saved to the session and displayed in the chat bubble. (#702)
                for _m in s.messages:
                    if isinstance(_m, dict) and _m.get('role') == 'assistant':
                        _raw_content = _m.get('content')
                        if isinstance(_raw_content, str):
                            _cleaned = _strip_xml_tool_calls(_raw_content)
                            if _cleaned != _raw_content:
                                _m['content'] = _cleaned
                        elif isinstance(_raw_content, list):
                            for _part in _raw_content:
                                if isinstance(_part, dict) and isinstance(_part.get('text'), str):
                                    _part['text'] = _strip_xml_tool_calls(_part['text'])

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
                    _err_lower = _err_str.lower()
                    _is_quota = (
                        'insufficient credit' in _err_lower
                        or 'credit balance' in _err_lower
                        or 'credits exhausted' in _err_lower
                        or 'quota_exceeded' in _err_lower
                        or 'quota exceeded' in _err_lower
                        or 'exceeded your current quota' in _err_lower
                    )
                    _is_auth = (
                        not _is_quota and (
                            '401' in _err_str
                            or (_last_err and 'AuthenticationError' in type(_last_err).__name__)
                            or 'authentication' in _err_lower
                            or 'unauthorized' in _err_lower
                            or 'invalid api key' in _err_lower
                            or 'invalid_api_key' in _err_lower
                        )
                    )
                    if _is_quota:
                        _err_label = 'Out of credits'
                        _err_type = 'quota_exhausted'
                        _err_hint = 'Your provider account is out of credits. Top up your balance or switch providers via `hermes model`.'
                    elif _is_auth:
                        _err_label = 'Authentication failed'
                        _err_type = 'auth_mismatch'
                        _err_hint = (
                            'The selected model may not be supported by your configured provider or '
                            'your API key is invalid. Run `hermes model` in your terminal to '
                            'update credentials, then restart the WebUI.'
                        )
                    else:
                        _err_label = 'No response received'
                        _err_type = 'no_response'
                        _err_hint = 'Verify your API key is valid and the selected model is available for your account.'
                    put('apperror', {
                        'message': _err_str or f'{_err_label}.',
                        'type': _err_type,
                        'hint': _err_hint,
                    })
                    # Clear stream/pending state so the session does not appear
                    # "agent_running" on reload after a silent failure.
                    # Persist the error so it survives page reload.
                    # _error=True ensures _sanitize_messages_for_api excludes it from
                    # subsequent API calls so the LLM never sees its own error as prior context.
                    s.active_stream_id = None
                    s.pending_user_message = None
                    s.pending_attachments = []
                    s.pending_started_at = None
                    s.messages.append({
                        'role': 'assistant',
                        'content': f'**{_err_label}:** {_err_str or _err_label}\n\n*{_err_hint}*',
                        'timestamp': int(time.time()),
                        '_error': True,
                    })
                    try:
                        s.save()
                    except Exception:
                        pass
                    return  # apperror already closes the stream on the client side

                # ── Handle context compression side effects ──
                # If compression fired inside run_conversation, the agent may have
                # rotated its session_id. Detect and fix the mismatch so the WebUI
                # continues writing to the correct session file.
                #
                # Lock migration: when session_id rotates, we alias the new ID to
                # the *same* Lock object under SESSION_AGENT_LOCKS so that
                # subsequent callers using _get_session_agent_lock(new_sid) get the
                # same Lock the streaming thread is already holding.  We then pop
                # the old-id entry to prevent a leak.  This is safe because we
                # already hold _agent_lock (the Lock object itself), so the
                # reference stays alive even after the dict entry is removed.
                # Concurrent readers that already looked up the old ID will still
                # see the same Lock object until they release it.
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
                    # Migrate the per-session lock: alias new_sid to the held
                    # _agent_lock reference directly (not via old_sid lookup),
                    # then remove the old_sid entry to prevent a leak.
                    with SESSION_AGENT_LOCKS_LOCK:
                        SESSION_AGENT_LOCKS[new_sid] = _agent_lock
                        SESSION_AGENT_LOCKS.pop(old_sid, None)
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
                # Persist tool-call summaries even when the final message history only
                # kept bare tool rows and omitted explicit assistant tool_call IDs.
                tool_calls = _extract_tool_calls_from_messages(
                    s.messages,
                    live_tool_calls=_live_tool_calls,
                )
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
                # Persist reasoning trace in the session so it survives reload.
                # Must run BEFORE s.save() — otherwise the mutation lives only in
                # memory until the next turn's save, and the last-turn thinking card
                # is lost when the user reloads immediately after a response.
                if _reasoning_text and s.messages:
                    for _rm in reversed(s.messages):
                        if isinstance(_rm, dict) and _rm.get('role') == 'assistant':
                            _rm['reasoning'] = _reasoning_text
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
            # (reasoning trace already attached + saved above, before s.save())
            raw_session = s.compact() | {'messages': s.messages, 'tool_calls': tool_calls}
            put('done', {'session': redact_session_data(raw_session), 'usage': usage})
            if _should_bg_title and _u0 and _a0:
                threading.Thread(
                    target=_run_background_title_update,
                    args=(s.session_id, _u0, _a0, str(s.title or '').strip(), put, agent),
                    daemon=True,
                ).start()
            else:
                # Use the original session_id parameter (never reassigned), not s.session_id
                # which may be rotated during context compression. The client captured
                # activeSid = original session_id so they must match for stream_end to close.
                put('stream_end', {'session_id': session_id})
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
        err_str = str(e)
        _exc_lower = err_str.lower()
        # Classify before saving so the error message can be persisted to the session.
        # Check quota exhaustion first — OpenAI billing 429s use insufficient_quota which
        # also matches rate-limit patterns, so order matters.
        _exc_is_quota = (
            'insufficient credit' in _exc_lower
            or 'credit balance' in _exc_lower
            or 'credits exhausted' in _exc_lower
            or 'quota_exceeded' in _exc_lower
            or 'quota exceeded' in _exc_lower
            or 'exceeded your current quota' in _exc_lower
        )
        _exc_is_rate_limit = (not _exc_is_quota) and (
            'rate limit' in _exc_lower or '429' in err_str or 'RateLimitError' in type(e).__name__
        )
        _exc_is_auth = (
            '401' in err_str
            or 'AuthenticationError' in type(e).__name__
            or 'authentication' in _exc_lower
            or 'unauthorized' in _exc_lower
            or 'invalid api key' in _exc_lower
            or 'no cookie auth credentials' in _exc_lower
        )
        if _exc_is_quota:
            _exc_label, _exc_type, _exc_hint = (
                'Out of credits', 'quota_exhausted',
                'Your provider account is out of credits. Top up your balance or switch providers via `hermes model`.',
            )
        elif _exc_is_rate_limit:
            _exc_label, _exc_type, _exc_hint = (
                'Rate limit reached', 'rate_limit',
                'Rate limit reached. The fallback model (if configured) was also exhausted. Try again in a moment.',
            )
        elif _exc_is_auth:
            _exc_label, _exc_type, _exc_hint = (
                'Authentication error', 'auth_mismatch',
                'The selected model may not be supported by your configured provider. '
                'Run `hermes model` in your terminal to switch providers, then restart the WebUI.',
            )
        else:
            _exc_label, _exc_type, _exc_hint = 'Error', 'error', ''
        if s is not None:
            if _checkpoint_stop is not None:
                _checkpoint_stop.set()
            if _ckpt_thread is not None:
                _ckpt_thread.join(timeout=15)
            # Persist the error so it survives page reload.
            # _error=True ensures _sanitize_messages_for_api excludes it from subsequent
            # API calls so the LLM never sees its own error as prior context on the next turn.
            _lock_ctx = _agent_lock if _agent_lock is not None else contextlib.nullcontext()
            with _lock_ctx:
                s.active_stream_id = None
                s.pending_user_message = None
                s.pending_attachments = []
                s.pending_started_at = None
                s.messages.append({
                    'role': 'assistant',
                    'content': f'**{_exc_label}:** {err_str}' + (f'\n\n*{_exc_hint}*' if _exc_hint else ''),
                    'timestamp': int(time.time()),
                    '_error': True,
                })
                try:
                    s.save()
                except Exception:
                    pass
        _apperror_payload: dict = {'message': err_str, 'type': _exc_type}
        if _exc_hint:
            _apperror_payload['hint'] = _exc_hint
        put('apperror', _apperror_payload)
    finally:
        # Stop periodic checkpoint thread if it was started (Issue #765)
        if _checkpoint_stop is not None:
            _checkpoint_stop.set()
        if _ckpt_thread is not None:
            _ckpt_thread.join(timeout=15)
        _clear_thread_env()  # TD1: always clear thread-local context
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)
            AGENT_INSTANCES.pop(stream_id, None)  # Clean up agent instance reference
            STREAM_PARTIAL_TEXT.pop(stream_id, None)  # Clean up partial text buffer (#893)

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================


def cancel_stream(stream_id: str) -> bool:
    """Signal an in-flight stream to cancel. Returns True if the stream existed.

    Eagerly releases the session lock (pops STREAMS/CANCEL_FLAGS/AGENT_INSTANCES
    and clears session.active_stream_id) so new /api/chat/start requests succeed
    immediately after cancel, even if the agent thread is still blocked.

    The worker thread's finally block uses .pop(key, None), so the double-pop is
    a safe no-op. Session cleanup runs outside STREAMS_LOCK to preserve lock
    ordering (streaming thread does LOCK → STREAMS_LOCK; inverting would deadlock).
    """
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

        # ── Eager session lock release (fixes #653) ──────────────────────────
        # Pop stream state now so the 409 guard in routes.py sees the session
        # as idle and allows new /api/chat/start immediately after cancel,
        # even if the agent thread is still blocked in a C-level syscall.
        # The worker thread's finally block uses .pop(key, None) too, so a
        # double-pop here is safe (no-op).
        STREAMS.pop(stream_id, None)
        CANCEL_FLAGS.pop(stream_id, None)
        AGENT_INSTANCES.pop(stream_id, None)
        # STREAM_PARTIAL_TEXT is intentionally NOT popped here — the agent thread may
        # still be appending tokens. We capture the snapshot two lines below; the
        # streaming finally block handles the cleanup when the thread exits.

        # Capture partial text and session_id while holding STREAMS_LOCK (avoids a
        # race where the agent thread deallocates the agent object or clears the
        # partial text after we release).
        # Session cleanup (get_session + save) must happen OUTSIDE the lock —
        # get_session() acquires LOCK, and the streaming thread does LOCK first
        # then STREAMS_LOCK, so inverting the order here would cause deadlock.
        _cancel_session_id = getattr(agent, 'session_id', None) if agent else None
        _cancel_partial_text = STREAM_PARTIAL_TEXT.get(stream_id, '')

    # Session cleanup outside STREAMS_LOCK to preserve lock ordering.
    # Acquire the per-session _agent_lock too, mirroring every other session
    # writer (streaming success/error paths, periodic checkpoint, POST endpoints)
    # so the cancel-path mutation races neither the checkpoint thread nor
    # concurrent undo/retry calls.
    if _cancel_session_id:
        with _get_session_agent_lock(_cancel_session_id):
            try:
                _cs = get_session(_cancel_session_id)
                _cs.active_stream_id = None
                _cs.pending_user_message = None
                _cs.pending_attachments = []
                _cs.pending_started_at = None
                # Persist any partial assistant text that was streamed before cancel (#893).
                # Preserving partial content means the user sees what the agent had
                # produced rather than losing it entirely.  The marker is _partial=True
                # (for session/UI identification only) — NOT _error=True — so the partial
                # content IS kept in the history sent to the agent on the next user
                # message, letting the model continue from where it was cut off.
                # See the inner comment on the append call below for the rationale.
                partial_text = _cancel_partial_text.strip() if _cancel_partial_text else ''
                if partial_text:
                    import re as _re
                    # Strip thinking/reasoning markup from partial content before saving.
                    # First pass: remove complete <thinking>...</thinking> blocks.
                    _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>',
                                        '', partial_text,
                                        flags=_re.DOTALL | _re.IGNORECASE).strip()
                    # Second pass: strip trailing UNCLOSED think/thinking block (the common
                    # cancel case — user stops mid-reasoning before the close tag appears).
                    _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*',
                                        '', _stripped,
                                        flags=_re.DOTALL | _re.IGNORECASE).strip()
                    if _stripped:
                        # Mark _partial=True for session/UI identification only.
                        # Deliberately NOT _error=True — the partial content is real model
                        # output and should be visible in conversation history so the model
                        # can continue from it on the next turn (#893).
                        _cs.messages.append({
                            'role': 'assistant',
                            'content': _stripped,
                            '_partial': True,
                            'timestamp': int(time.time()),
                        })
                # Cancel marker — flagged _error=True so it is stripped from conversation
                # history on the next turn (prevents model from seeing "Task cancelled."
                # as a prior assistant reply).
                _cs.messages.append({
                    'role': 'assistant',
                    'content': '*Task cancelled.*',
                    '_error': True,
                    'timestamp': int(time.time()),
                })
                _cs.save()
            except Exception:
                logger.debug("Failed to clear session state on cancel for %s", _cancel_session_id)

    return True
