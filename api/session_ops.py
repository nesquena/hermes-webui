"""Session-mutation operations for slash commands (/retry, /undo) and
read-only aggregators (/status, /usage). Operates on the webui's own
JSON Session store (api/models.py), not on hermes-agent's SQLite.

Behavior parity reference: gateway/run.py:_handle_*_command in
the hermes-agent repo.
"""
from __future__ import annotations
import logging
from typing import Any

from api.config import LOCK, _get_session_agent_lock
from api.models import get_session, SESSIONS

logger = logging.getLogger(__name__)


def _truncate_at_last_user(messages):
    history = messages or []
    last_user_idx = None
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], dict) and history[i].get('role') == 'user':
            last_user_idx = i
            break
    if last_user_idx is None:
        return None
    return history[:last_user_idx]


def retry_last(session_id: str) -> dict[str, Any]:
    """Truncate the session to before the last user message, return its text.

    Mirrors gateway/run.py:_handle_retry_command. Caller (webui frontend)
    is expected to put the returned text back in the composer and call
    send() to resume the conversation -- the agent's gateway calls its own
    _handle_message; the webui has no equivalent in-process pipeline.

    Raises:
        KeyError: session not found
        ValueError: no user message in transcript
    """
    # Acquire the per-session agent lock as the outermost lock so that the
    # read-modify-write of s.messages is serialised with the periodic
    # checkpoint thread, cancel_stream, and all other session writers.
    # Lock ordering: _agent_lock → LOCK → _write_session_index (LOCK).
    with _get_session_agent_lock(session_id):
        # get_session() and Session.save() both acquire the module-level LOCK
        # internally (the latter via _write_session_index()), and LOCK is a
        # non-reentrant threading.Lock — so they MUST be called outside our
        # own `with LOCK:` block to avoid self-deadlocking.
        #
        # The race we close is the read-modify-write of s.messages: two
        # concurrent /api/session/retry calls could otherwise both compute the
        # same last_user_idx from the same history and double-truncate. We
        # serialize just the in-memory mutation; persistence happens inside
        # the per-session lock so the checkpoint thread cannot race us.
        #
        # Stale-object guard: on a cache miss, two concurrent get_session()
        # calls can each load and cache a *different* Session instance for the
        # same session_id (the second store clobbers the first). Re-bind to
        # the canonical cached instance inside the lock so the mutation lands
        # on the object the next reader will see, not a stale parallel copy.
        s = get_session(session_id)  # raises KeyError if missing
        with LOCK:
            s = SESSIONS.get(session_id, s)
            history = s.messages or []
            last_user_idx = None
            for i in range(len(history) - 1, -1, -1):
                if history[i].get('role') == 'user':
                    last_user_idx = i
                    break
            if last_user_idx is None:
                raise ValueError('No previous message to retry.')

            last_user_text = _extract_text(history[last_user_idx].get('content', ''))
            removed_count = len(history) - last_user_idx
            s.messages = history[:last_user_idx]
            if isinstance(getattr(s, 'context_messages', None), list) and s.context_messages:
                truncated_context = _truncate_at_last_user(s.context_messages)
                if truncated_context is not None:
                    s.context_messages = truncated_context
        s.save()
    return {'last_user_text': last_user_text, 'removed_count': removed_count}


def undo_last(session_id: str) -> dict[str, Any]:
    """Remove the most recent user message and everything after it.

    Mirrors gateway/run.py:_handle_undo_command. Returns a preview of the
    removed text so the UI can confirm to the user.

    Raises:
        KeyError: session not found
        ValueError: no user message in transcript
    """
    # Acquire the per-session agent lock as the outermost lock so that the
    # read-modify-write of s.messages is serialised with the periodic
    # checkpoint thread, cancel_stream, and all other session writers.
    # Lock ordering: _agent_lock → LOCK → _write_session_index (LOCK).
    with _get_session_agent_lock(session_id):
        s = get_session(session_id)  # acquires LOCK transiently
        with LOCK:
            # Stale-object guard — see retry_last for the rationale.
            s = SESSIONS.get(session_id, s)
            history = s.messages or []
            last_user_idx = None
            for i in range(len(history) - 1, -1, -1):
                if history[i].get('role') == 'user':
                    last_user_idx = i
                    break
            if last_user_idx is None:
                raise ValueError('Nothing to undo.')

            removed_text = _extract_text(history[last_user_idx].get('content', ''))
            removed_count = len(history) - last_user_idx
            s.messages = history[:last_user_idx]
            if isinstance(getattr(s, 'context_messages', None), list) and s.context_messages:
                truncated_context = _truncate_at_last_user(s.context_messages)
                if truncated_context is not None:
                    s.context_messages = truncated_context
        s.save()  # outside LOCK -- save() re-acquires LOCK via _write_session_index()
    preview = (removed_text[:40] + '...') if len(removed_text) > 40 else removed_text
    return {
        'removed_count': removed_count,
        'removed_preview': preview,
    }


def session_status(session_id: str) -> dict[str, Any]:
    """Return a snapshot of session state for /status.

    Webui equivalent of gateway/run.py:_handle_status_command. The agent's
    "agent_running" comes from `session_key in self._running_agents`; the
    webui equivalent is whether the session has an active stream
    (active_stream_id is set).
    """
    s = get_session(session_id)
    inp = int(s.input_tokens or 0)
    out = int(s.output_tokens or 0)
    return {
        'session_id': s.session_id,
        'title': s.title,
        'model': s.model,
        'workspace': s.workspace,
        'personality': s.personality,
        'message_count': len(s.messages or []),
        'created_at': s.created_at,
        'updated_at': s.updated_at,
        'agent_running': bool(getattr(s, 'active_stream_id', None)),
        'input_tokens': inp,
        'output_tokens': out,
        'total_tokens': inp + out,
        'estimated_cost': s.estimated_cost,
    }


def session_usage(session_id: str) -> dict[str, Any]:
    """Return token usage and cost for /usage.

    Mirrors gateway/run.py:_handle_usage_command's basic counters. The
    agent shows additional fields (rate-limit headroom etc.) that depend
    on provider API responses we don't have in webui -- those are deferred.
    """
    s = get_session(session_id)
    inp = int(s.input_tokens or 0)
    out = int(s.output_tokens or 0)
    return {
        'input_tokens': inp,
        'output_tokens': out,
        'total_tokens': inp + out,
        'estimated_cost': s.estimated_cost,
        'model': s.model,
    }


def _extract_text(content: Any) -> str:
    """Flatten message content to plain text. Agent stores either a string
    or a list of {type, text|...} parts; webui needs the user-typed text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get('type') == 'text':
                parts.append(p.get('text', ''))
        return ' '.join(parts)
    return str(content)
