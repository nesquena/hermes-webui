"""
Hermes Web UI -- SSE streaming engine and agent thread runner.
Includes Sprint 10 cancel support via CANCEL_FLAGS.
"""
import base64
import contextlib
import json
import logging
import mimetypes
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
    get_config,
    STREAMS, STREAMS_LOCK, CANCEL_FLAGS, AGENT_INSTANCES, STREAM_PARTIAL_TEXT,
    STREAM_REASONING_TEXT, STREAM_LIVE_TOOL_CALLS,
    STREAM_GOAL_RELATED, PENDING_GOAL_CONTINUATION,
    LOCK, SESSIONS, SESSION_DIR,
    _get_session_agent_lock, _set_thread_env, _clear_thread_env,
    register_active_run, update_active_run, unregister_active_run,
    SESSION_AGENT_LOCKS, SESSION_AGENT_LOCKS_LOCK,
    resolve_model_provider,
    resolve_custom_provider_connection,
    model_with_provider_context,
)
from api.helpers import redact_session_data, _redact_text
from api.compression_anchor import visible_messages_for_anchor
from api.metering import meter
from api.turn_journal import append_turn_journal_event_for_stream

# Global lock for os.environ writes. Per-session locks (_agent_lock) prevent
# concurrent runs of the SAME session, but two DIFFERENT sessions can still
# interleave their os.environ writes. This global lock serializes the env
# save/restore — held only briefly across the env-mutation critical section,
# NOT for the entire agent run. The agent runs outside the lock; the finally
# block re-acquires to atomically restore env vars. See narrow-lock pattern
# in _run_agent_streaming (line ~2719) and profile_env_for_background_worker
# (api/profiles.py:715).
_ENV_LOCK = threading.Lock()


def _prewarm_skill_tool_modules():
    """Import tools.skills_tool and tools.skill_manager_tool outside any lock.

    First-time module imports can trigger heavy initialisation (disk I/O,
    transitive imports, plugin discovery).  Performing those imports while
    holding ``_ENV_LOCK`` serialises every concurrent session behind the
    slowest import.  Prewarming ensures the modules are already in
    ``sys.modules`` before the lock is acquired, so the lock body only
    does lightweight attribute patching.

    We cannot place these at module top-level because ``tools.*`` lives
    in the hermes-agent package which may not be on ``sys.path`` at
    import time (Docker volume-mount ordering).  A dedicated helper
    keeps the lazy-import try/except in one place and makes the intent
    explicit.
    """
    for _mod_name in ('tools.skills_tool', 'tools.skill_manager_tool'):
        try:
            __import__(_mod_name)
        except ImportError:
            pass


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


def _is_quota_error_text(err_text: str) -> bool:
    """Return True when provider text looks like quota/usage exhaustion."""
    _err_lower = str(err_text or '').lower()
    return (
        'insufficient credit' in _err_lower
        or 'credit balance' in _err_lower
        or 'credits exhausted' in _err_lower
        or 'more credits' in _err_lower
        or 'can only afford' in _err_lower
        or 'fewer max_tokens' in _err_lower
        or 'quota_exceeded' in _err_lower
        or 'quota exceeded' in _err_lower
        or 'exceeded your current quota' in _err_lower
        # OpenAI Codex OAuth usage-exhaustion shapes (#1765).
        or 'plan limit reached' in _err_lower
        or 'usage_limit_exceeded' in _err_lower
        or 'usage limit exceeded' in _err_lower
        or 'reached the limit of messages' in _err_lower
        or 'used up your usage' in _err_lower
        or ('plan' in _err_lower and 'limit' in _err_lower and 'reached' in _err_lower)
    )


def _clarify_timeout_seconds(default: int = 120) -> int:
    """Resolve clarify timeout from config, with bounded fallback."""
    try:
        cfg = get_config()
        raw = cfg.get("clarify", {}).get("timeout", default)
        timeout_seconds = int(raw)
        if timeout_seconds <= 0:
            return default
        return timeout_seconds
    except Exception:
        return default


_CANCEL_MARKER_PATTERNS = ('task cancelled', 'task canceled', 'response interrupted')


def _has_new_assistant_reply(all_messages: list, prev_count: int) -> bool:
    """Return True if *new* messages (beyond ``prev_count``) contain an
    assistant message with non-empty content.

    ``all_messages`` is ``result.get('messages')`` which includes the full
    conversation history.  ``prev_count`` is ``len(_previous_context_messages)``
    — the number of messages present before the current turn started.  Only
    messages at index >= prev_count are inspected so that historical assistant
    replies don't mask a silent failure on the current turn.

    If ``len(all_messages) < prev_count`` (an edge-case shrink), there is no
    reliable new-message slice to inspect. Treat that as "no new assistant
    reply" so stale historical assistant replies cannot mask a silent failure.
    When ``len == prev_count``, there are no new messages and we return False.
    """
    if len(all_messages) > prev_count:
        # Normal case: new messages appended beyond the pre-turn history.
        candidates = all_messages[prev_count:]
    elif len(all_messages) < prev_count:
        return False
    else:
        # Same length. In production this means no new messages were appended.
        # However, some test fixtures replace the entire message list rather
        # than appending, so check whether the tail changed.
        return False
    return any(
        m.get('role') == 'assistant' and str(m.get('content') or '').strip()
        for m in candidates
    )


def _classify_provider_error(err_str: str, exc=None, *, silent_failure: bool = False) -> dict:
    """Classify provider/agent failure text for WebUI apperror UX.

    Keep this string-based until hermes-agent exposes stable structured
    provider error classes for Codex OAuth plan limits.
    """
    err_str = str(err_str or '')
    _err_lower = err_str.lower()
    _exc_name = type(exc).__name__ if exc is not None else ''
    _is_cancelled = (
        'cancelled by user' in _err_lower
        or 'canceled by user' in _err_lower
        or 'user cancelled' in _err_lower
        or 'user canceled' in _err_lower
        or 'task cancelled' in _err_lower
        or 'task canceled' in _err_lower
        or 'cancellederror' in _err_lower
        or (exc is not None and _exc_name in ('CancelledError', 'CanceledError'))
    )
    _is_interrupted = (
        not _is_cancelled
        and (
            'interrupted by user' in _err_lower
            or 'response interrupted' in _err_lower
            or 'operation interrupted' in _err_lower
            or 'operation was interrupted' in _err_lower
            or 'operation aborted' in _err_lower
            or 'request was aborted' in _err_lower
            or 'aborterror' in _err_lower
            or (exc is not None and type(exc).__name__ in ('KeyboardInterrupt', 'AbortError'))
        )
    )
    if _is_cancelled:
        return {
            'label': 'Task cancelled',
            'type': 'cancelled',
            'hint': 'The run was cancelled by the user before Skyly finished. No provider failure occurred.',
        }
    if _is_interrupted:
        return {
            'label': 'Response interrupted',
            'type': 'interrupted',
            'hint': 'The run stopped before a provider response completed. If you did not cancel it, try again.',
        }
    _is_quota = _is_quota_error_text(err_str)
    _is_auth = (
        not _is_quota and (
            '401' in err_str
            or (exc is not None and 'AuthenticationError' in _exc_name)
            or 'authentication' in _err_lower
            or 'unauthorized' in _err_lower
            or 'invalid api key' in _err_lower
            or 'invalid_api_key' in _err_lower
            or 'no cookie auth credentials' in _err_lower
        )
    )
    _is_not_found = (
        # model_not_found hints mention Settings / `hermes model` below.
        '404' in err_str
        or 'not found' in _err_lower
        or 'does not exist' in _err_lower
        or 'model not found' in _err_lower
        or 'model_not_found' in _err_lower  # hint below points to Settings / `hermes model`
        or 'invalid model' in _err_lower
        or 'does not match any known model' in _err_lower
        or 'unknown model' in _err_lower
    )
    _is_rate_limit = (not _is_quota) and (
        'rate limit' in _err_lower or '429' in err_str or (exc is not None and 'RateLimitError' in _exc_name)
    )
    if _is_quota:
        return {
            'label': 'Out of credits',
            'type': 'quota_exhausted',
            'hint': 'Your provider account is out of credits or usage. Top up, wait for the plan window to reset, or switch providers via `hermes model`.',
        }
    if _is_rate_limit:
        return {
            'label': 'Rate limit reached',
            'type': 'rate_limit',
            'hint': 'Rate limit reached. The fallback model (if configured) was also exhausted. Try again in a moment.',
        }
    if _is_auth:
        return {
            'label': 'Authentication failed',
            'type': 'auth_mismatch',
            'hint': 'The selected model may not be supported by your configured provider or your API key is invalid. Run `hermes model` in your terminal to update credentials, then restart the WebUI.',
        }
    if _is_not_found:
        return {
            'label': 'Model not found',
            'type': 'model_not_found',
            'hint': 'The selected model was not found by the provider. Check the model ID in Settings or run `hermes model` to verify it exists for your provider.',
        }
    if silent_failure:
        return {
            'label': 'No response from provider',
            # Preserve the existing no_response event type (#373) while making
            # the catch-all silent-failure message more specific for #1765.
            'type': 'no_response',
            'hint': 'The provider returned no content and no error. This often means a usage/rate limit was hit silently. Check provider status, switch providers via `hermes model`, or try again in a moment.',
        }
    return {'label': 'Error', 'type': 'error', 'hint': ''}


def _provider_error_payload(message: str, err_type: str, hint: str = '') -> dict:
    """Build a bounded, redacted apperror payload with provider details."""
    _message = str(message or '')
    _safe_message = _redact_text(_message).strip() if _message else ''
    payload: dict = {'message': _safe_message or _message, 'type': err_type}
    if hint:
        payload['hint'] = hint
    if _safe_message:
        _details = _safe_message
        if len(_details) > 1200:
            _details = _details[:1197].rstrip() + '…'
        if _details:
            payload['details'] = _details
    return payload


def _session_has_cancel_marker(session) -> bool:
    """Return True if a visible cancel/interrupted marker is already persisted."""
    for msg in reversed(getattr(session, 'messages', None) or []):
        if not isinstance(msg, dict):
            continue
        if msg.get('role') == 'user':
            return False
        if msg.get('role') != 'assistant':
            continue
        content = msg.get('content')
        text = ''
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get('text') or part.get('content') or ''))
            text = '\n'.join(parts)
        normalized = text.strip().lower()
        if any(pattern in normalized for pattern in _CANCEL_MARKER_PATTERNS):
            return True
    return False


def _cancelled_turn_content(message: str = 'Task cancelled.') -> str:
    """Return cancelled-turn copy matching the verbose provider-error layout."""
    _message = str(message or 'Task cancelled.').strip()
    if not _message.endswith('.'):
        _message += '.'
    return (
        f"**Task cancelled:** {_message}\n\n"
        "*The run was cancelled by the user before Skyly finished. No provider failure occurred.*"
    )


def _persist_cancelled_turn(session, *, message: str = 'Task cancelled.') -> None:
    """Persist a user-cancelled terminal state without provider-error wording.

    cancel_stream() usually writes this marker first, but the streaming thread can
    later unwind through the silent-failure or exception path. Those paths must
    not append a misleading provider no-response error after an explicit cancel.
    """
    _materialize_pending_user_turn_before_error(session)
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    if not _session_has_cancel_marker(session):
        session.messages.append({
            'role': 'assistant',
            'content': _cancelled_turn_content(message),
            '_error': True,
            'provider_details': str(message or 'Task cancelled.').strip(),
            'provider_details_label': 'Cancellation details',
            'timestamp': int(time.time()),
        })


def _cleanup_ephemeral_cancelled_turn(session) -> None:
    """Remove transient /btw session state after a cancel without saving it."""
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    try:
        import pathlib
        pathlib.Path(session.path).unlink(missing_ok=True)
    except Exception:
        logger.debug("Failed to clean up ephemeral cancelled session", exc_info=True)


def _finalize_cancelled_turn(session, *, ephemeral: bool = False, message: str = 'Task cancelled.') -> None:
    """Finalize a cancelled turn for persistent or ephemeral sessions."""
    if ephemeral:
        _cleanup_ephemeral_cancelled_turn(session)
        return
    _persist_cancelled_turn(session, message=message)
    try:
        session.save()
    except Exception:
        logger.debug("Failed to persist cancelled turn", exc_info=True)


def _aiagent_import_error_detail() -> str:
    """Return a multi-line diagnostic string for the "AIAgent not available" path.

    The bare ImportError ("AIAgent not available -- check that hermes-agent is
    on sys.path") leaves users guessing at which python is running, where it's
    looking, and what to fix. We assemble the same evidence a maintainer would
    ask for first (issue #1695): the python that's running, the agent_dir env
    var if set, the sys.path entries that mention 'hermes', and the most-common
    fix (`pip install -e .` in the agent dir).

    Kept as a separate helper so it stays out of the hot path until we actually
    need to raise — building it on every successful import would be wasted work.
    """
    import os as _os
    import sys as _sys

    lines = ["AIAgent not available -- check that hermes-agent is on sys.path"]
    lines.append("")
    lines.append(f"  python:  {_sys.executable}")
    agent_dir = _os.environ.get("HERMES_WEBUI_AGENT_DIR")
    if agent_dir:
        lines.append(f"  HERMES_WEBUI_AGENT_DIR: {agent_dir}")
    else:
        lines.append("  HERMES_WEBUI_AGENT_DIR: (not set)")

    # Show only the sys.path entries that look relevant — full sys.path is noisy.
    relevant = [p for p in _sys.path if "hermes" in p.lower() or "agent" in p.lower()]
    if relevant:
        lines.append("  sys.path entries mentioning hermes/agent:")
        for entry in relevant[:6]:
            lines.append(f"    - {entry}")
        if len(relevant) > 6:
            lines.append(f"    ... and {len(relevant) - 6} more")
    else:
        lines.append("  sys.path: (no entries mention hermes or agent)")

    lines.append("")
    lines.append("  Most common fix: install the agent in editable mode so its modules")
    lines.append("  appear on sys.path:")
    lines.append("")
    lines.append("    cd /path/to/hermes-agent")
    lines.append("    pip install -e .")
    lines.append("")
    lines.append("  Then restart the WebUI.")
    lines.append("")
    lines.append('  Full troubleshooting: docs/troubleshooting.md ("AIAgent not available")')
    return "\n".join(lines)
from api.models import get_session, title_from
from api.workspace import set_last_workspace

# Fields that are safe to send to LLM provider APIs.
# Everything else (attachments, timestamp, _ts, etc.) is display-only
# metadata added by the webui and must be stripped before the API call.
_API_SAFE_MSG_KEYS = {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'refusal', 'reasoning_content'}

_NATIVE_IMAGE_MAX_BYTES = 20 * 1024 * 1024

_GATEWAY_ROUTING_TOP_LEVEL_KEYS = {
    'used_provider',
    'used_model',
    'requested_provider',
    'requested_model',
}
_GATEWAY_ROUTING_CONTAINER_KEYS = (
    'llm_gateway',
    'gateway',
    'metadata',
    'response_metadata',
    'routing_metadata',
    'usage',
)
_GATEWAY_ROUTING_ATTEMPT_KEYS = {
    'provider', 'model', 'status', 'reason', 'selection_reason', 'score',
    'latency_ms', 'error', 'timestamp', 'selected', 'attempt', 'attempt_index',
}


def _clean_gateway_routing_scalar(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        if not text:
            return None
        return value if isinstance(value, (int, float, bool)) else text[:240]
    return None


def _find_gateway_metadata_payload(payload):
    if not isinstance(payload, dict):
        return None
    if any(k in payload for k in _GATEWAY_ROUTING_TOP_LEVEL_KEYS) or isinstance(payload.get('routing'), list):
        return payload
    for key in _GATEWAY_ROUTING_CONTAINER_KEYS:
        nested = payload.get(key)
        found = _find_gateway_metadata_payload(nested)
        if found:
            return found
    return None


def _normalize_gateway_routing_metadata(payload, requested_model=None, requested_provider=None):
    """Return safe LLM Gateway routing metadata, or None when absent.

    LLM Gateway response metadata can contain provider/model routing details,
    but WebUI must only persist display-safe scalars and a bounded routing list.
    Secrets or provider-specific request objects are deliberately ignored.
    """
    src = _find_gateway_metadata_payload(payload)
    if not src:
        return None

    normalized = {}
    for key in _GATEWAY_ROUTING_TOP_LEVEL_KEYS:
        value = _clean_gateway_routing_scalar(src.get(key))
        if value is not None:
            normalized[key] = value

    if 'requested_model' not in normalized:
        fallback_model = _clean_gateway_routing_scalar(requested_model)
        if fallback_model is not None:
            normalized['requested_model'] = fallback_model
    if 'requested_provider' not in normalized:
        fallback_provider = _clean_gateway_routing_scalar(requested_provider)
        if fallback_provider is not None:
            normalized['requested_provider'] = fallback_provider

    routing = []
    raw_routing = src.get('routing')
    if isinstance(raw_routing, list):
        for attempt in raw_routing[:12]:
            if not isinstance(attempt, dict):
                continue
            clean_attempt = {}
            for key in _GATEWAY_ROUTING_ATTEMPT_KEYS:
                value = _clean_gateway_routing_scalar(attempt.get(key))
                if value is not None:
                    clean_attempt[key] = value
            if clean_attempt:
                routing.append(clean_attempt)
    if routing:
        normalized['routing'] = routing

    used_provider = str(normalized.get('used_provider') or '').strip().lower()
    requested_provider_norm = str(normalized.get('requested_provider') or '').strip().lower()
    used_model = str(normalized.get('used_model') or '').strip().lower()
    requested_model_norm = str(normalized.get('requested_model') or '').strip().lower()
    provider_changed = bool(used_provider and requested_provider_norm and used_provider != requested_provider_norm)
    model_changed = bool(used_model and requested_model_norm and used_model != requested_model_norm)
    attempted_providers = [
        str(a.get('provider') or '').strip().lower()
        for a in routing
        if a.get('provider')
    ]
    distinct_attempted_providers = {p for p in attempted_providers if p}
    failed_before_selection = any(
        str(a.get('status') or '').strip().lower() in {'failed', 'error', 'timeout', 'rejected'}
        for a in routing
    )
    has_failover = bool(provider_changed or len(distinct_attempted_providers) > 1 or failed_before_selection)

    if not (
        normalized.get('used_provider') or normalized.get('used_model') or routing or provider_changed or model_changed
    ):
        return None
    normalized['provider_changed'] = provider_changed
    normalized['model_changed'] = model_changed
    normalized['has_failover'] = has_failover
    return normalized


def _extract_gateway_routing_metadata(agent, result, requested_model=None, requested_provider=None):
    candidates = []
    if isinstance(result, dict):
        candidates.extend([
            result.get('llm_gateway'),
            result.get('gateway'),
            result.get('metadata'),
            result.get('response_metadata'),
            result.get('routing_metadata'),
            result.get('usage'),
            result,
        ])
    for attr in (
        'llm_gateway_metadata',
        'gateway_metadata',
        'last_response_metadata',
        'response_metadata',
        'routing_metadata',
        'last_usage',
    ):
        if agent is not None:
            candidates.append(getattr(agent, attr, None))
    for candidate in candidates:
        normalized = _normalize_gateway_routing_metadata(
            candidate,
            requested_model=requested_model,
            requested_provider=requested_provider,
        )
        if normalized:
            return normalized
    return None


def _build_agent_thread_env(profile_runtime_env: dict | None, workspace: str, session_id: str, profile_home: str) -> dict:
    """Build thread-local agent env with per-run values overriding profile defaults.

    Profile runtime env may include TERMINAL_CWD from config.yaml. Passing it as
    **kwargs alongside an explicit TERMINAL_CWD raises TypeError before the
    agent starts, so merge into one dict first and let the active workspace win.
    """
    env = dict(profile_runtime_env or {})
    env.update({
        'TERMINAL_CWD': str(workspace),
        'HERMES_EXEC_ASK': '1',
        'HERMES_SESSION_KEY': session_id,
        'HERMES_SESSION_ID': session_id,
        'HERMES_SESSION_PLATFORM': 'webui',
        'HERMES_HOME': profile_home,
    })
    return env


def _format_process_notification(evt: dict) -> str:
    """Format a completed background process notification for agent input."""
    if not isinstance(evt, dict):
        return ''
    if evt.get('type') != 'completion':
        return ''
    _sid = evt.get('session_id', '')
    _cmd = evt.get('command', '')
    _exit = evt.get('exit_code', '')
    _out = evt.get('output') or ''
    if len(_out) > 4000:
        _out = _out[:4000] + '\n... (truncated)'
    return (
        f"[IMPORTANT: Background process {_sid} completed (exit code {_exit}).\n"
        f"Command: {_cmd}\n"
        f"Output:\n{_out}]"
    )


def _mark_process_completion_consumed(process_registry, process_id: str) -> None:
    """Best-effort bridge to the agent registry's private completion marker."""
    try:
        with process_registry._lock:
            process_registry._completion_consumed.add(process_id)
    except Exception:
        logger.debug("Failed to mark process completion consumed", exc_info=True)


def _drain_webui_process_notifications(session_id: str) -> list[str]:
    """Return completion notifications that belong to this WebUI session.

    The agent registry completion queue is process-wide and events do not carry
    the WebUI session key directly. Look up the live process session before
    delivery so completions from other tabs remain queued for their owners.
    """
    if not session_id:
        return []
    try:
        from tools.process_registry import process_registry
    except Exception:
        return []

    notifications: list[str] = []
    skipped_events: list[dict] = []
    completion_queue = getattr(process_registry, 'completion_queue', None)
    if completion_queue is None:
        return []

    while True:
        try:
            evt = completion_queue.get_nowait()
        except queue.Empty:
            break
        except Exception:
            logger.debug("Failed to drain process completion queue", exc_info=True)
            break

        evt_sid = str(evt.get('session_id') or '') if isinstance(evt, dict) else ''
        if not evt_sid:
            skipped_events.append(evt)
            continue
        try:
            if process_registry.is_completion_consumed(evt_sid):
                continue
            proc = process_registry.get(evt_sid)
        except Exception:
            proc = None
        if getattr(proc, 'session_key', None) != session_id:
            skipped_events.append(evt)
            continue

        notification = _format_process_notification(evt)
        if notification:
            notifications.append(notification)
            _mark_process_completion_consumed(process_registry, evt_sid)

    for evt in skipped_events:
        try:
            completion_queue.put(evt)
        except Exception:
            logger.debug("Failed to requeue process completion event", exc_info=True)
            break
    return notifications


def _attachment_name(att) -> str:
    if isinstance(att, dict):
        return str(att.get('name') or att.get('filename') or att.get('path') or '').strip()
    return str(att or '').strip()


_IMAGE_MAGIC: dict[bytes | None, frozenset[str]] = {
    b'\x89PNG\r\n\x1a\n': frozenset({'image/png'}),
    b'\xff\xd8\xff': frozenset({'image/jpeg'}),
    b'GIF87a': frozenset({'image/gif'}),
    b'GIF89a': frozenset({'image/gif'}),
    b'RIFF': frozenset({'image/webp'}),
    b'BM': frozenset({'image/bmp'}),
    None: frozenset({'image/svg+xml'}),
}


def _is_valid_image(path: Path, mime: str) -> bool:
    """Check that the file's first bytes match the expected image MIME type.

    Uses simple magic-number detection (no external dependency). SVG is
    allowed through because it is text-based and has no binary signature.
    """
    if not mime.startswith('image/'):
        return False
    mime_base = mime.split(';', 1)[0]
    if mime_base == 'image/svg+xml':
        return True
    try:
        with path.open('rb') as fh:
            head = fh.read(16)
    except OSError:
        return False
    for magic, mimes in _IMAGE_MAGIC.items():
        if magic is not None and head.startswith(magic) and mime_base in mimes:
            return True
    return False


def _resolve_image_input_mode(cfg: dict) -> str:
    """Return ``"native"`` or ``"text"`` based on config, mirroring
    ``agent/image_routing.py:decide_image_input_mode``.

    The agent has this logic, but the WebUI's ``_build_native_multimodal_message``
    was unconditionally embedding images as native ``image_url`` parts, completely
    bypassing ``image_input_mode``.  This caused silent failures when the main model
    does not support images and the fallback model is also text-only (#21160-related).
    """
    agent_cfg = cfg.get("agent") or {}
    mode = str(agent_cfg.get("image_input_mode", "auto") or "auto").strip().lower()
    if mode not in ("auto", "native", "text"):
        mode = "auto"

    if mode == "native":
        return "native"
    if mode == "text":
        return "text"

    # auto: if auxiliary.vision is explicitly configured → text mode
    # (user opted into a dedicated vision backend)
    aux = cfg.get("auxiliary") or {}
    vision = aux.get("vision") or {}
    provider = str(vision.get("provider") or "").strip().lower()
    model_name = str(vision.get("model") or "").strip()
    base_url = str(vision.get("base_url") or "").strip()
    if provider not in ("", "auto") or model_name or base_url:
        return "text"

    # No explicit vision config, no model-capability lookup available in WebUI.
    # Default to native — the agent's ``_strip_images_from_messages`` guard will
    # strip images on rejection and retry as text.
    return "native"


def _build_native_multimodal_message(workspace_ctx: str, msg_text: str, attachments, workspace: str, *, cfg: dict = None):
    """Build native multimodal content parts for current-turn image uploads.

    WebUI uploads files into the active workspace. For image files, pass the
    bytes to Hermes as OpenAI-style image_url data URLs so vision-capable main
    models can consume them in the same request. Non-image files intentionally
    stay as text path attachments so the agent can inspect them with file tools.

    When *cfg* is provided, respects ``agent.image_input_mode`` — if the resolved
    mode is ``"text"``, returns a plain string (attachments are not embedded) so
    the agent's text-mode pipeline (``vision_analyze``) handles images.
    """
    if not attachments:
        return workspace_ctx + msg_text

    # ── Check image_input_mode before embedding anything ──
    if cfg is not None and _resolve_image_input_mode(cfg) == "text":
        return workspace_ctx + msg_text

    parts = [{'type': 'text', 'text': workspace_ctx + msg_text}]
    workspace_root = Path(workspace).expanduser().resolve()
    image_count = 0

    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        raw_path = str(att.get('path') or '').strip()
        if not raw_path:
            continue
        try:
            path = Path(raw_path).expanduser().resolve()
            # Uploads should live inside the selected workspace. Do not read
            # arbitrary paths from client-provided attachment metadata.
            path.relative_to(workspace_root)
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size <= 0 or size > _NATIVE_IMAGE_MAX_BYTES:
                continue
            mime = str(att.get('mime') or '').strip() or (mimetypes.guess_type(path.name)[0] or '')
            if not mime.startswith('image/') or not _is_valid_image(path, mime):
                continue
            data = base64.b64encode(path.read_bytes()).decode('ascii')
        except Exception:
            continue
        parts.append({
            'type': 'image_url',
            'image_url': {'url': f'data:{mime};base64,{data}'},
        })
        image_count += 1

    return parts if image_count else workspace_ctx + msg_text


def _strip_thinking_markup(text: str) -> str:
    """Remove common reasoning/thinking wrappers from model text."""
    if not text:
        return ''
    s = str(text)
    # Treat provider thinking wrappers as metadata only when they lead the
    # response. Literal discussion of these tags later in normal prose should
    # stay visible (#2152).
    s = re.sub(r'^\s*<think>.*?</think>\s*', ' ', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'^\s*<\|channel\|?>thought\n?.*?<channel\|>\s*', ' ', s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r'^\s*<\|turn\|>thinking\n.*?<turn\|>\s*', ' ', s, flags=re.IGNORECASE | re.DOTALL)  # Gemma 4
    s = re.sub(r'^\s*(the|ther)\s+user\s+is\s+asking[^\n]*(?:\n|$)', ' ', s, flags=re.IGNORECASE)
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
    Also handles variants like <｜DSML｜function_calls> from DeepSeek on Bedrock.
    """
    if not text:
        return text
    s = str(text)
    # Check if contains any function_calls/DSML marker (case-insensitive)
    _lo = s.lower()
    if 'function_calls' not in _lo and 'dsml' not in _lo:
        return text
    
    _dsml_prefix = r'(?:\s*｜\s*DSML\s*[｜|]\s*)?'
    open_tag = rf'<{_dsml_prefix}function_calls'
    close_tag = rf'</{_dsml_prefix}function_calls>'
    # Strip complete blocks for both <function_calls> and <｜DSML｜function_calls>.
    s = re.sub(
        rf'{open_tag}>.*?{close_tag}',
        '',
        s,
        flags=re.IGNORECASE | re.DOTALL
    )
    # Strip orphaned/truncated opening tags, including missing ">" at stream tail.
    s = re.sub(
        rf'{open_tag}(?:>|$).*$',
        '',
        s,
        flags=re.IGNORECASE | re.DOTALL
    )
    # Remove malformed DSML fragments like "<｜DSML |" that can leak in tokens.
    s = re.sub(r'<\s*｜\s*DSML\s*[｜|]\s*', '', s, flags=re.IGNORECASE)
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


_WORKSPACE_PREFIX_RE = re.compile(r'^\s*\[Workspace::v1:\s*(?:\\.|[^\]\\])+\]\s*')
_LEGACY_WORKSPACE_PREFIX_RE = re.compile(r'^\s*\[Workspace:[^\]]+\]\s*')


def _escape_workspace_prefix_path(path: str) -> str:
    return str(path or '').replace('\\', '\\\\').replace(']', '\\]')


def _workspace_context_prefix(path: str) -> str:
    return f"[Workspace::v1: {_escape_workspace_prefix_path(path)}]\n"


def _strip_workspace_prefix(text: str, *, include_legacy: bool = False) -> str:
    """Remove WebUI-injected workspace tags without eating user-typed text."""
    value = str(text or '')
    stripped = _WORKSPACE_PREFIX_RE.sub('', value, count=1)
    if include_legacy and stripped == value:
        stripped = _LEGACY_WORKSPACE_PREFIX_RE.sub('', value, count=1)
    return stripped.strip()


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


def _latest_exchange_snippets(messages):
    """Return (last_user_text, last_assistant_text) snippets for title refresh.

    Walks the message list backwards to find the last user+assistant pair,
    skipping empty or tool-call-only assistant messages.
    """
    user_text = ''
    asst_text = ''
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        if role == 'assistant' and not asst_text:
            candidate = _message_text(m.get('content'))
            # Skip tool-call-only preambles
            if m.get('tool_calls') and (not candidate or _looks_invalid_generated_title(candidate)):
                continue
            if candidate:
                asst_text = candidate
        elif role == 'user' and not user_text:
            candidate = _message_text(m.get('content'))
            if candidate:
                user_text = candidate
        if user_text and asst_text:
            break
    return user_text[:500], asst_text[:500]


def _count_exchanges(messages):
    """Count the number of user messages (rough exchange count)."""
    count = 0
    for m in messages or []:
        if isinstance(m, dict) and m.get('role') == 'user':
            content = m.get('content', '')
            if isinstance(content, list):
                content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
            if str(content).strip():
                count += 1
    return count


def _get_title_refresh_interval() -> int:
    """Read the auto_title_refresh_every setting (0 = disabled)."""
    try:
        from api.config import load_settings
        settings = load_settings()
        val = settings.get('auto_title_refresh_every', '0')
        return int(val) if str(val).strip().isdigit() and int(val) > 0 else 0
    except Exception:
        return 0


def _is_provisional_title(current_title: str, messages) -> bool:
    """Heuristic: title equals first-message substring placeholder."""
    derived = title_from(messages, '') or ''
    if not derived:
        return False
    current = re.sub(r'\s+', ' ', str(current_title or '')).strip()
    candidate = re.sub(r'\s+', ' ', str(derived[:64] or '')).strip()
    if not current or not candidate:
        return False
    return current == candidate


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
    # Title generation is a small auxiliary task, but reasoning models may
    # spend a surprising amount of the completion budget before emitting final
    # content.  Keep the budget high enough for MiniMax/Kimi-style reasoning
    # responses without making title generation depend on provider-specific
    # one-off branches.
    return 512


def _title_retry_completion_budget(provider: str = '', model: str = '', base_url: str = '') -> int:
    return max(1024, _title_completion_budget(provider, model, base_url) * 2)


def _title_retry_status(status: str) -> bool:
    # Whether to grant a second budget attempt within the same prompt+model
    # combination.  ``llm_length`` indicates the model would have produced
    # content with more headroom, so doubling the budget can help.
    #
    # ``llm_empty_reasoning`` historically also triggered a retry, but for
    # reasoning models (Qwen3-thinking, DeepSeek-R1, Kimi-K2, etc.) that
    # status means the model burned its entire budget on hidden reasoning
    # tokens and emitted nothing visible.  Doubling the budget in that case
    # just doubles the GPU/credit cost without changing the outcome — the
    # next attempt produces the same shape.  We skip the retry for empty-
    # reasoning statuses and let the title path fall through to the local
    # fallback summary.  See issue #2083 for the LM Studio + Qwen3 repro.
    return status in {
        'llm_length',
        'llm_length_aux',
    }


def _title_should_skip_remaining_attempts(status: str) -> bool:
    """Statuses where re-issuing the next prompt against the same model
    produces the same failing shape (model burned its budget on hidden
    reasoning, hit a hard provider gate, etc.).

    Short-circuit the prompt-iteration loop so we don't issue a second
    full-budget LLM call (and twice the GPU/credit burn) only to land in
    the same fallback path. See issue #2083.

    Add a status here only when retrying the next prompt is provably
    wasted work (single-call signal already establishes that the next
    call will return the same shape). Length-truncation WITHOUT
    reasoning is NOT in the set — that's legitimately recoverable by
    a larger budget on a different prompt and stays in
    :func:`_title_retry_status`.
    """
    return status in {
        'llm_empty_reasoning',
        'llm_empty_reasoning_aux',
    }


def _safe_obj_value(obj, key: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    value = getattr(obj, key, None)
    # Missing MagicMock attrs stringify as mock reprs and look truthy.  Treat
    # them as absent so tests model real provider objects accurately.
    if value.__class__.__module__.startswith('unittest.mock'):
        return None
    return value


def _safe_text_value(value) -> str:
    if value is None:
        return ''
    if value.__class__.__module__.startswith('unittest.mock'):
        return ''
    return str(value or '').strip()


def _extract_title_response(resp, *, aux: bool = False) -> tuple[str, str]:
    """Return (content, empty_status) from an OpenAI-compatible response."""
    suffix = '_aux' if aux else ''
    try:
        choices = _safe_obj_value(resp, 'choices') or []
        choice = choices[0] if choices else None
        message = _safe_obj_value(choice, 'message')
        content = _safe_text_value(_safe_obj_value(message, 'content'))
        if content:
            return content, ''
        finish_reason = _safe_text_value(_safe_obj_value(choice, 'finish_reason')).lower()
        reasoning = (
            _safe_text_value(_safe_obj_value(message, 'reasoning'))
            or _safe_text_value(_safe_obj_value(message, 'reasoning_content'))
            or _safe_text_value(_safe_obj_value(message, 'thinking'))
        )
        # When the model emitted reasoning tokens but no visible content, it
        # burned its budget on hidden thinking — retrying with a larger budget
        # almost never recovers a useful title (see issue #2083: Qwen3-thinking
        # via LM Studio loops indefinitely on auto-title generation).  Report
        # this case distinctly so callers can short-circuit instead of double-
        # billing the GPU/credit on a near-certain repeat.
        if reasoning:
            return '', f'llm_empty_reasoning{suffix}'
        if finish_reason == 'length':
            return '', f'llm_length{suffix}'
        return '', f'llm_empty{suffix}'
    except Exception:
        return '', f'llm_empty{suffix}'


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
    base_max_tokens = _title_completion_budget(provider, model, base_url)
    reasoning_extra = {"reasoning": {"enabled": False}}
    if _is_minimax_route(provider, model, base_url):
        reasoning_extra["reasoning_split"] = True
    try:
        _timeout = _aux_title_timeout()
        from agent.auxiliary_client import call_llm
        last_status = 'llm_error_aux'
        for idx, prompt in enumerate(prompts):
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": qa},
            ]
            budgets = [base_max_tokens]
            try:
                for budget_idx, max_tokens in enumerate(budgets):
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
                    raw, empty_status = _extract_title_response(resp, aux=True)
                    if raw:
                        return raw, ('llm_aux' if idx == 0 and budget_idx == 0 else 'llm_aux_retry')
                    last_status = empty_status or 'llm_empty_aux'
                    if budget_idx == 0 and _title_retry_status(last_status):
                        budgets.append(_title_retry_completion_budget(provider, model, base_url))
            except Exception as e:
                last_status = 'llm_error_aux'
                logger.debug("Aux title generation attempt %s failed: %s", idx + 1, e)
            # If the model just burned its budget on hidden reasoning, retrying
            # the next prompt against the same model produces the same shape.
            # Short-circuit to the local fallback path (#2083).
            if _title_should_skip_remaining_attempts(last_status):
                logger.debug(
                    "Aux title generation short-circuiting after %s (reasoning-only response).",
                    last_status,
                )
                break
        return None, last_status
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
    base_max_tokens = _title_completion_budget(
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
            budgets = [base_max_tokens]
            try:
                last_status = 'llm_empty'
                for budget_idx, max_tokens in enumerate(budgets):
                    raw = ""
                    empty_status = ''
                    if getattr(agent, 'api_mode', '') == 'codex_responses':
                        codex_kwargs = agent._build_api_kwargs(api_messages)
                        codex_kwargs.pop('tools', None)
                        if 'max_output_tokens' in codex_kwargs:
                            codex_kwargs['max_output_tokens'] = max_tokens
                        resp = agent._run_codex_stream(codex_kwargs)
                        assistant_message, _ = agent._normalize_codex_response(resp)
                        raw = (assistant_message.content or '') if assistant_message else ''
                        if not raw:
                            empty_status = 'llm_empty'
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
                        if not raw:
                            empty_status = 'llm_empty'
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
                        raw, empty_status = _extract_title_response(resp)
                    raw = str(raw or '').strip()
                    if raw:
                        return raw, ('llm' if idx == 0 and budget_idx == 0 else 'llm_retry')
                    last_status = empty_status or 'llm_empty'
                    if budget_idx == 0 and _title_retry_status(last_status):
                        budgets.append(_title_retry_completion_budget(
                            getattr(agent, 'provider', ''),
                            getattr(agent, 'model', ''),
                            getattr(agent, 'base_url', ''),
                        ))
            except Exception as e:
                last_status = 'llm_error'
                logger.debug(
                    "Agent title generation attempt %s failed: provider=%s model=%s error=%s",
                    idx + 1,
                    getattr(agent, 'provider', None),
                    getattr(agent, 'model', None),
                    e,
                )
            # If the model just burned its budget on hidden reasoning, retrying
            # the next prompt against the same model produces the same shape.
            # Short-circuit to the local fallback path (#2083).
            if _title_should_skip_remaining_attempts(last_status):
                logger.debug(
                    "Agent title generation short-circuiting after %s (reasoning-only response).",
                    last_status,
                )
                break
        return None, last_status
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
    user_text = _strip_workspace_prefix(user_text)
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
    # Unicode-aware Latin tokenization: keep the old "no leading underscore"
    # and non-Latin placeholder behavior while allowing letters such as ä/ö/ü/ß.
    # The previous ASCII-only pattern turned "führe" into "f" + "hre"; the short
    # "f" was filtered and the broken "hre" became part of the title.
    latin_word = r'A-Za-z0-9À-ÖØ-öø-ÿ'
    tokens = re.findall(rf'[{latin_word}][{latin_word}_./+-]*', head)
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


def _is_generic_fallback_title(title: str) -> bool:
    """Return True for low-information fallback labels that should not be persisted."""
    return str(title or '').strip().lower() in {'conversation topic'}


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
        from api import profiles as profiles_api

        with profiles_api.profile_env_for_background_worker(s, "background title", logger_override=logger):
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
                fallback_title = _fallback_title_from_exchange(user_text, assistant_text)
                if fallback_title and not _is_generic_fallback_title(fallback_title):
                    logger.debug("Using local fallback for session title generation")
                    next_title = fallback_title
                    source = 'fallback'
                elif fallback_title:
                    logger.debug("Skipping generic local fallback for session title generation: %r", fallback_title)
        fallback_reason = (
            f'local_summary:{llm_status}'
            if source == 'fallback' and llm_status
            else 'local_summary'
        )
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
                _put_title_status(put_event, session_id, source, fallback_reason, effective_title, raw_preview)
            else:
                _put_title_status(put_event, session_id, source, llm_status, effective_title, raw_preview)
            put_event('title', {'session_id': session_id, 'title': effective_title})
        else:
            _put_title_status(put_event, session_id, 'skipped', source or 'unchanged', effective_title, raw_preview)
    finally:
        put_event('stream_end', {'session_id': session_id})


def _run_background_title_refresh(session_id: str, user_text: str, assistant_text: str, current_title: str, put_event, agent=None):
    """Refresh an existing LLM-generated title using the latest exchange text.

    Unlike _run_background_title_update, this does NOT guard on
    llm_title_generated — it assumes the title was already LLM-generated
    and the session has progressed enough to warrant a refresh.
    It does NOT emit stream_end (the caller already did).
    """
    try:
        try:
            s = get_session(session_id)
        except KeyError:
            return
        # Safety: skip if user manually renamed since the check
        effective = str(s.title or '').strip()
        if effective != current_title:
            _put_title_status(put_event, session_id, 'skipped', 'manual_title', effective)
            return
        if not effective or effective in ('Untitled', 'New Chat'):
            return
        from api import profiles as profiles_api

        with profiles_api.profile_env_for_background_worker(s, "background title", logger_override=logger):
            aux_title_configured = _aux_title_configured()
            if agent and not aux_title_configured:
                next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
                if not next_title and llm_status in ('llm_error', 'llm_invalid'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text, agent=agent, use_agent_model=True)
            else:
                next_title, llm_status, raw_preview = _generate_llm_session_title_via_aux(user_text, assistant_text)
                if not next_title and agent and llm_status in ('llm_error_aux', 'llm_invalid_aux'):
                    next_title, llm_status, raw_preview = _generate_llm_session_title_for_agent(agent, user_text, assistant_text)
        if not next_title:
            _put_title_status(put_event, session_id, 'refresh_skipped', llm_status or 'empty', effective, raw_preview)
            return
        # Skip if the new title is essentially the same (after normalization)
        normalized_current = re.sub(r'\s+', ' ', effective).strip().lower()
        normalized_new = re.sub(r'\s+', ' ', next_title).strip().lower()
        if normalized_current == normalized_new:
            _put_title_status(put_event, session_id, 'refresh_skipped', 'same_title', effective, raw_preview)
            return
        with _get_session_agent_lock(session_id):
            with LOCK:
                s = SESSIONS.get(session_id, s)
                # Re-check: user may have renamed while we were generating
                if str(s.title or '').strip() != current_title:
                    _put_title_status(put_event, session_id, 'skipped', 'manual_title', str(s.title or '').strip())
                    return
                s.title = next_title
                s.llm_title_generated = True
                effective_title = s.title
            # Session.save() calls _write_session_index(), which acquires LOCK.
            # Keep the per-session agent lock for mutation serialization, but
            # release the global session LOCK before persisting to avoid a
            # self-deadlock in the background title-refresh thread.
            s.save(touch_updated_at=False)
        _put_title_status(put_event, session_id, 'refreshed', llm_status, effective_title, raw_preview)
        put_event('title', {'session_id': session_id, 'title': effective_title})
        logger.info("Adaptive title refresh: session=%s new_title=%r", session_id, effective_title)
    except Exception:
        logger.debug("Background title refresh failed for session %s", session_id, exc_info=True)


def _preserve_pre_compression_snapshot(s, old_sid: str) -> None:
    """Persist old_sid as a read-only pre-compression snapshot.

    Context compression rotates the active WebUI session id from old_sid to the
    agent's new continuation id. The old JSON must remain on disk for lineage
    traversal, but it should not continue to appear as an active sidebar row.
    """
    old_path = SESSION_DIR / f'{old_sid}.json'
    if not old_path.exists():
        return
    try:
        existing_text = old_path.read_text(encoding='utf-8')
        try:
            existing = json.loads(existing_text)
            existing_msgs = len(existing.get('messages') or [])
            existing_snapshot = bool(existing.get('pre_compression_snapshot'))
        except (json.JSONDecodeError, ValueError):
            # Treat corrupt/malformed old JSON as missing history and rewrite it
            # from the in-memory pre-compression messages below. That is safer
            # than leaving an unreadable recovery snapshot behind.
            existing_msgs = -1
            existing_snapshot = False
        if len(s.messages) <= existing_msgs and existing_snapshot:
            return
        if len(s.messages) > existing_msgs:
            # In-memory messages are newer than the file; save the full old
            # snapshot from the current session object while preserving its
            # pre-existing parent_session_id lineage.
            saved_sid = s.session_id
            saved_snapshot = bool(getattr(s, 'pre_compression_snapshot', False))
            s.session_id = old_sid
            s.pre_compression_snapshot = True
            # Stage-359 / PR #2295: clear runtime stream-state fields on the
            # archived snapshot so the sidebar does not reopen the parent as
            # a permanently-running session while the child already holds the
            # completed answer. The continuation session's live state is
            # restored from saved_* locals in the finally block.
            saved_active_stream_id = getattr(s, 'active_stream_id', None)
            saved_pending_user_message = getattr(s, 'pending_user_message', None)
            saved_pending_attachments = list(getattr(s, 'pending_attachments', []) or [])
            saved_pending_started_at = getattr(s, 'pending_started_at', None)
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = []
            s.pending_started_at = None
            try:
                # skip_index=False so the snapshot appears in _index.json with
                # the pre_compression_snapshot marker. The sidebar projection
                # (#2285) reads that marker to hide the snapshot from active
                # rows while keeping the JSON discoverable for lineage traversal.
                s.save(touch_updated_at=False, skip_index=False)
                logger.info(
                    "Preserved pre-compression session %s (%d messages) to disk",
                    old_sid, len(s.messages),
                )
            finally:
                s.session_id = saved_sid
                s.pre_compression_snapshot = saved_snapshot
                s.active_stream_id = saved_active_stream_id
                s.pending_user_message = saved_pending_user_message
                s.pending_attachments = saved_pending_attachments
                s.pending_started_at = saved_pending_started_at
            return
        # Existing file is already at least as complete as memory; stamp only
        # the snapshot marker so index/sidebar projection can hide it without
        # rewriting a shorter messages array over a fuller transcript.
        from api.models import Session
        snapshot = Session.load(old_sid)
        if snapshot:
            snapshot.pre_compression_snapshot = True
            # Stage-359 Opus SHOULD-FIX: clear runtime fields on the loaded
            # snapshot too. If the disk snapshot was last persisted while the
            # parent was live, it could carry a stale active_stream_id /
            # pending_* over to disk. The sidebar projection filters snapshot
            # rows so this is latent today, but the contract should match the
            # primary branch above so future readers can trust snapshot files
            # to never contain live runtime state.
            snapshot.active_stream_id = None
            snapshot.pending_user_message = None
            snapshot.pending_attachments = []
            snapshot.pending_started_at = None
            snapshot.save(touch_updated_at=False, skip_index=False)
            logger.info(
                "Marked pre-compression session %s as sidebar-hidden snapshot",
                old_sid,
            )
    except OSError:
        logger.debug("Could not read old session file before preservation")
    except Exception:
        logger.debug("Failed to preserve pre-compression session file", exc_info=True)


def _maybe_schedule_title_refresh(session, put_event, agent):
    """Check if the session is due for an adaptive title refresh and schedule it."""
    refresh_interval = _get_title_refresh_interval()
    if refresh_interval <= 0:
        return
    current_title = str(session.title or '').strip()
    if not current_title or current_title in ('Untitled', 'New Chat'):
        return
    if not getattr(session, 'llm_title_generated', False):
        return
    exchange_count = _count_exchanges(session.messages)
    if exchange_count <= 0 or exchange_count % refresh_interval != 0:
        return
    last_u, last_a = _latest_exchange_snippets(session.messages)
    if not last_u and not last_a:
        return
    threading.Thread(
        target=_run_background_title_refresh,
        args=(session.session_id, last_u, last_a, current_title, put_event, agent),
        daemon=True,
    ).start()


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


def _session_context_messages(session):
    """Return model-facing history without assuming it matches the UI transcript."""
    context_messages = getattr(session, 'context_messages', None)
    if isinstance(context_messages, list) and context_messages:
        return context_messages
    return session.messages or []


def _message_identity(msg):
    if not isinstance(msg, dict):
        return None
    role = str(msg.get('role') or '')
    content = msg.get('content', '')
    text = _message_text(content)
    if role == 'user':
        # WebUI sends the model a workspace-prefixed user_message while the
        # visible optimistic bubble contains only the human text. Treat them as
        # the same turn for merge/dedup purposes; otherwise compaction results
        # render two adjacent user bubbles ("Ok" and "[Workspace...]\nOk").
        text = _strip_workspace_prefix(text, include_legacy=True)
    if not text and not msg.get('tool_call_id') and not msg.get('tool_calls'):
        return None
    return (
        role,
        " ".join(str(text or '').split())[:500],
        str(msg.get('tool_call_id') or ''),
        json.dumps(msg.get('tool_calls') or [], sort_keys=True, ensure_ascii=False),
    )


def _messages_have_prefix(messages, prefix):
    if len(messages or []) < len(prefix or []):
        return False
    for idx, expected in enumerate(prefix or []):
        if _message_identity((messages or [])[idx]) != _message_identity(expected):
            return False
    return True


def _is_context_compression_marker(msg):
    if not isinstance(msg, dict):
        return False
    text = _message_text(msg.get('content', '')).lower()
    return (
        'context compaction' in text
        or 'context compression' in text
        or 'context was auto-compressed' in text
        or 'active task list was preserved across context compression' in text
    )


def _compact_summary_text(raw_text: str | None, limit: int = 320) -> str | None:
    """Normalize a text blob used in compression summary cards."""
    if not isinstance(raw_text, str):
        return None
    txt = raw_text.strip()
    if not txt:
        return None
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > limit:
        txt = f"{txt[: limit - 6]}…"
    return txt


def _compression_anchor_message_key(message):
    if not isinstance(message, dict):
        return None
    role = str(message.get('role') or '')
    if not role or role == 'tool':
        return None
    content = message.get('content', '')
    text = _message_text(content)
    if len(text) > 160:
        text = text[:160]
    ts = message.get('_ts') or message.get('timestamp')
    attachments = message.get('attachments')
    attach_count = len(attachments) if isinstance(attachments, list) else 0
    if not text and not attach_count and not ts:
        return None
    return {'role': role, 'ts': ts, 'text': text, 'attachments': attach_count}


def _compression_summary_from_messages(messages):
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if not _is_context_compression_marker(m):
            continue
        text = _message_text(m.get('content'))
        if text:
            return text
    return None


def _find_current_user_turn(messages, msg_text):
    needle = " ".join(str(msg_text or '').split())
    fallback = None
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        fallback = idx
        text = " ".join(
            _strip_workspace_prefix(
                _message_text(msg.get('content', '')),
                include_legacy=True,
            ).split()
        )
        if needle and (needle in text or text in needle):
            return idx
    return fallback


def _drop_checkpointed_current_user_from_context(messages, msg_text):
    """Return model history without an eager-checkpointed current user turn."""
    history = list(messages or [])
    if not history:
        return history
    current_user_key = _message_identity({'role': 'user', 'content': msg_text})
    if current_user_key and _message_identity(history[-1]) == current_user_key:
        return history[:-1]
    return history


def _save_pre_compression_snapshot(session, old_session_id):
    """Persist the archived pre-compression session without live turn state.

    During context compression the same ``Session`` object is reused for the new
    continuation id.  Before the final continuation save clears
    ``active_stream_id`` and ``pending_*``, we also preserve an old-id snapshot so
    the full pre-compression transcript remains recoverable.  That archived
    parent must not keep the current stream bookkeeping, otherwise the sidebar can
    reopen the parent as a permanently running session while the child already
    contains the completed answer.
    """
    saved_sid = session.session_id
    saved_active_stream_id = getattr(session, 'active_stream_id', None)
    saved_pending_user_message = getattr(session, 'pending_user_message', None)
    saved_pending_attachments = list(getattr(session, 'pending_attachments', []) or [])
    saved_pending_started_at = getattr(session, 'pending_started_at', None)
    session.session_id = old_session_id
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = []
    session.pending_started_at = None
    try:
        session.save(touch_updated_at=False, skip_index=True)
    finally:
        session.session_id = saved_sid
        session.active_stream_id = saved_active_stream_id
        session.pending_user_message = saved_pending_user_message
        session.pending_attachments = saved_pending_attachments
        session.pending_started_at = saved_pending_started_at


def _stream_writeback_is_current(session, stream_id):
    """Return True only while a worker still owns the session writeback.

    cancel_stream() intentionally clears ``active_stream_id`` early so the UI can
    accept a follow-up turn while the old worker is unwinding. That old worker
    must not later persist its stale result over the newer transcript.
    """
    return bool(stream_id) and getattr(session, 'active_stream_id', None) == stream_id


def _merge_display_messages_after_agent_result(previous_display, previous_context, result_messages, msg_text):
    """Keep UI transcript durable while allowing model context to compact.

    If Hermes Agent returns a normal append-only history, append that delta to
    the UI transcript. If the model/context history was compacted and no longer
    has the prior context as a prefix, keep the previous UI transcript and append
    only compaction marker messages plus the current user turn onward.
    """
    previous_display = list(previous_display or [])
    previous_context = list(previous_context or [])
    result_messages = list(result_messages or [])
    if not result_messages:
        return previous_display

    if _messages_have_prefix(result_messages, previous_context):
        candidates = result_messages[len(previous_context):]
    else:
        current_user_idx = _find_current_user_turn(result_messages, msg_text)
        marker_candidates = [
            m for m in result_messages[:current_user_idx if current_user_idx is not None else len(result_messages)]
            if _is_context_compression_marker(m)
        ]
        turn_candidates = result_messages[current_user_idx:] if current_user_idx is not None else []
        candidates = marker_candidates + turn_candidates

    merged = previous_display[:]
    seen = {_message_identity(m) for m in merged}
    current_user_key = _message_identity({'role': 'user', 'content': msg_text})
    current_user_in_candidates = any(
        _message_identity(m) == current_user_key for m in candidates
    )
    current_user_already_checkpointed = bool(
        merged and _message_identity(merged[-1]) == current_user_key
    )
    if (
        current_user_key is not None
        and not current_user_in_candidates
        and not current_user_already_checkpointed
        and any(
            isinstance(m, dict) and m.get('role') in ('assistant', 'tool')
            for m in candidates
        )
    ):
        # Some provider retry/fallback paths can return an assistant/tool delta
        # without echoing the current user turn. In deferred session-save mode
        # the prompt exists only in pending_user_message, so appending that delta
        # directly would make the assistant bubble appear attached to the prior
        # exchange and then clear the pending prompt. Materialize the current
        # turn at the transcript boundary before the assistant/tool response.
        current_user_msg = {'role': 'user', 'content': msg_text}
        insert_at = 0
        while insert_at < len(candidates) and _is_context_compression_marker(candidates[insert_at]):
            insert_at += 1
        candidates = candidates[:insert_at] + [current_user_msg] + candidates[insert_at:]

    for msg in candidates:
        key = _message_identity(msg)
        if (
            key is not None
            and key == current_user_key
            and merged
            and _message_identity(merged[-1]) == key
        ):
            # Eager session-save mode can checkpoint the current user turn
            # before the agent runs. When the agent returns that same user turn
            # in result_messages, keep the durable checkpoint and append only
            # the assistant/tool delta.
            continue
        if (
            key is not None
            and isinstance(msg, dict)
            and msg.get('role') == 'assistant'
            and merged
            and _message_identity(merged[-1]) == key
        ):
            # Some provider/result replay paths can include the same assistant
            # message twice in the current delta. Treat only adjacent identity
            # matches as replay duplicates so identical answers in separate
            # user turns remain visible.
            continue
        if _is_context_compression_marker(msg) and key is not None and key in seen:
            continue
        display_msg = msg
        if key is not None and key == current_user_key and isinstance(msg, dict) and msg.get('role') == 'user':
            display_msg = copy.deepcopy(msg)
            display_msg['content'] = msg_text
        merged.append(copy.deepcopy(display_msg))
        if key is not None:
            seen.add(key)
    return merged


_TOOL_RESULT_SNIPPET_MAX = 4000


def _tool_result_snippet(raw, limit: int = _TOOL_RESULT_SNIPPET_MAX) -> str:
    """Extract a bounded result preview from a stored tool message payload."""
    if limit <= 0:
        return ''
    text = str(raw or '')
    try:
        data = raw if isinstance(raw, dict) else json.loads(text)
        if isinstance(data, dict):
            preview = data.get('output') or data.get('result') or data.get('error') or text
            text = str(preview)
    except Exception:
        pass
    return text[:limit]


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


def _materialize_pending_user_turn_before_error(session) -> bool:
    """Persist the pending user prompt before clearing runtime stream state.

    Error paths often clear ``pending_user_message`` before appending an assistant
    error marker. In deferred session-save mode that pending field can be the
    only durable copy of the user's current turn, so clearing it makes the user
    bubble disappear on reload/reconcile. Return True when a recovered user turn
    was appended.
    """
    pending_text = str(getattr(session, 'pending_user_message', None) or '')
    if not pending_text:
        return False
    normalized_pending = " ".join(pending_text.split())
    if normalized_pending:
        for existing in reversed(list(getattr(session, 'messages', None) or [])[-8:]):
            if not isinstance(existing, dict) or existing.get('role') != 'user':
                continue
            existing_text = " ".join(str(existing.get('content') or '').split())
            if existing_text == normalized_pending:
                return False
    recovered_ts = int(time.time())
    pending_started_at = getattr(session, 'pending_started_at', None)
    if isinstance(pending_started_at, (int, float)) and pending_started_at > 0:
        recovered_ts = int(pending_started_at)
    recovered = {
        'role': 'user',
        'content': pending_text,
        'timestamp': recovered_ts,
        '_recovered': True,
    }
    pending_attachments = getattr(session, 'pending_attachments', None)
    if pending_attachments:
        recovered['attachments'] = list(pending_attachments)
    session.messages.append(recovered)
    return True


def _last_resort_sync_from_core(session, stream_id, agent_lock):
    """Final-exit guard: if the stream exits with pending_user_message still set,
    sync messages from the core transcript or add an error marker.
    Called from the outer finally block of _run_agent_streaming.
    Must never raise.
    """
    from api.models import _get_profile_home, _apply_core_sync_or_error_marker
    try:
        # Guard: if a cancel was already requested, bail out — cancel_stream() has
        # already saved partial content and we must not double-append error markers.
        if stream_id in CANCEL_FLAGS and CANCEL_FLAGS[stream_id].is_set():
            return

        profile_home = _get_profile_home(session.profile)
        core_path = profile_home / 'sessions' / f'session_{session.session_id}.json'

        _lock_ctx = agent_lock if agent_lock is not None else contextlib.nullcontext()
        with _lock_ctx:
            _apply_core_sync_or_error_marker(
                session,
                core_path,
                stream_id_for_recheck=stream_id,
                require_stream_dead=False,
            )
    except Exception:
        logger.exception(
            "_last_resort_sync_from_core failed for session %s",
            getattr(session, 'session_id', '?'),
        )


def _attempt_credential_self_heal(
    provider_id, session_id, _agent_lock_ref,
):
    """Try to silently refresh credentials after a 401/auth error (#1401).

    Returns a new ``(agent, rt_dict)`` tuple on success so the caller can
    retry the conversation.  Returns ``None`` when self-heal is not
    applicable (e.g. auth.json unchanged, provider unresolvable).

    Steps:
    1. Re-read ``~/.hermes/auth.json`` to pick up fresh credentials that
       may have been written by a concurrent ``hermes model`` CLI invocation.
    2. Evict the session's cached agent so it is rebuilt with fresh keys.
    3. Evict the provider's credential-pool cache entry.
    4. Re-resolve the runtime provider.
    5. Return a new agent + resolved-provider dict (the caller must
       re-invoke ``run_conversation`` with these).
    """
    try:
        from api.oauth import (
            read_auth_json,
            resolve_runtime_provider_with_anthropic_env_lock,
        )
        from api.config import (
            SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK,
            invalidate_credential_pool_cache,
        )
        from hermes_cli.runtime_provider import resolve_runtime_provider

        # 1. Re-read auth.json (triggers a fresh credential scan)
        _fresh_auth = read_auth_json()
        if not _fresh_auth:
            logger.debug('[webui] self-heal: auth.json empty or missing, skipping')
            return None

        # 2. Evict the cached agent for this session
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE.pop(session_id, None)

        # 3. Invalidate the credential pool for this provider
        invalidate_credential_pool_cache(provider_id)

        # 4. Re-resolve runtime provider with fresh credentials
        _new_rt = resolve_runtime_provider_with_anthropic_env_lock(
            resolve_runtime_provider,
            requested=provider_id,
        )

        logger.info(
            '[webui] self-heal: credential refresh succeeded for provider=%s session=%s',
            provider_id, session_id,
        )
        return _new_rt
    except Exception as _heal_err:
        logger.warning(
            '[webui] self-heal: failed for provider=%s session=%s: %s',
            provider_id, session_id, _heal_err,
        )
        return None


def _run_agent_streaming(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    ephemeral=False,
    model_provider=None,
    goal_related=False,
):
    """Run agent in background thread, writing SSE events to STREAMS[stream_id].

    When ephemeral=True, session mutations are skipped — used by /btw to get
    a streaming answer without persisting to the parent session.
    """
    q = STREAMS.get(stream_id)
    if q is None:
        return
    register_active_run(
        stream_id,
        session_id=session_id,
        started_at=time.time(),
        phase="starting",
        workspace=str(workspace),
        model=model,
        provider=model_provider,
        ephemeral=bool(ephemeral),
    )
    if not ephemeral:
        try:
            append_turn_journal_event_for_stream(
                session_id,
                stream_id,
                {"event": "worker_started", "created_at": time.time()},
            )
        except Exception:
            logger.debug("Failed to append worker_started turn journal event", exc_info=True)
    s = None
    _rt = {}
    old_cwd = None
    old_exec_ask = None
    old_session_key = None
    old_session_id = None
    old_session_platform = None
    old_hermes_home = None
    old_profile_env = {}

    # MCP discovery moved to AFTER the per-profile HERMES_HOME mutation below
    # (was here at v0.51.30) — the previous placement always read the default
    # profile's mcp_servers because os.environ['HERMES_HOME'] hadn't been
    # rewritten yet.  See https://github.com/nesquena/hermes-webui/issues/1968.

    # Sprint 10: create a cancel event for this stream
    cancel_event = threading.Event()
    with STREAMS_LOCK:
        CANCEL_FLAGS[stream_id] = cancel_event
        STREAM_PARTIAL_TEXT[stream_id] = ''  # start accumulating partial text (#893)
        STREAM_REASONING_TEXT[stream_id] = ''  # start accumulating reasoning trace (#1361 §A)
        STREAM_LIVE_TOOL_CALLS[stream_id] = []  # start accumulating tool calls (#1361 §B)

    agent = None
    _live_prompt_estimate_tokens = [0]
    _live_prompt_exact_tokens = [0]
    _live_prompt_estimate_seen_ids = set()

    def _seed_live_prompt_estimate() -> int:
        """Capture the latest exact prompt size before adding live tool deltas."""
        if _live_prompt_estimate_tokens[0] > 0:
            return _live_prompt_estimate_tokens[0]
        _base = 0
        _agent = agent
        if _agent is not None:
            try:
                _cc = getattr(_agent, 'context_compressor', None)
                if _cc:
                    _base = getattr(_cc, 'last_prompt_tokens', 0) or 0
            except Exception:
                _base = 0
        if not _base:
            try:
                _session_obj = get_session(session_id)
                _base = getattr(_session_obj, 'last_prompt_tokens', 0) or 0
            except Exception:
                _base = 0
        _live_prompt_estimate_tokens[0] = int(_base or 0)
        _live_prompt_exact_tokens[0] = _live_prompt_estimate_tokens[0]
        return _live_prompt_estimate_tokens[0]

    def _bump_live_prompt_estimate(messages) -> int:
        """Increment a rough next-prompt estimate from live tool activity."""
        if not messages:
            return _live_prompt_estimate_tokens[0]
        try:
            from agent.model_metadata import estimate_messages_tokens_rough
            _delta = int(estimate_messages_tokens_rough(messages) or 0)
        except Exception:
            _delta = 0
        if _delta > 0:
            _seed_live_prompt_estimate()
            _live_prompt_estimate_tokens[0] += _delta
        return _live_prompt_estimate_tokens[0]

    def _live_usage_snapshot():
        """Best-effort live usage payload for mid-stream UI updates.

        During tool execution the final `done` event has not fired yet, but the
        frontend still benefits from seeing the latest known token / context
        values. These are exact for the most recent model call and a truthful
        lower bound for the pending next call after a tool result is appended.
        """
        _usage = {
            'input_tokens': 0,
            'output_tokens': 0,
            'estimated_cost': 0,
            'context_length': 0,
            'threshold_tokens': 0,
            'last_prompt_tokens': 0,
        }
        try:
            _session_obj = get_session(session_id)
        except Exception:
            _session_obj = None

        _agent = agent
        if _agent is not None:
            try:
                _usage['input_tokens'] = getattr(_agent, 'session_prompt_tokens', 0) or 0
                _usage['output_tokens'] = getattr(_agent, 'session_completion_tokens', 0) or 0
                _usage['estimated_cost'] = getattr(_agent, 'session_estimated_cost_usd', 0) or 0
            except Exception:
                pass
            try:
                _cc = getattr(_agent, 'context_compressor', None)
                if _cc:
                    _usage['context_length'] = getattr(_cc, 'context_length', 0) or 0
                    _usage['threshold_tokens'] = getattr(_cc, 'threshold_tokens', 0) or 0
                    _usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
            except Exception:
                pass

        if _session_obj is not None:
            for _field in ('input_tokens', 'output_tokens', 'estimated_cost', 'context_length', 'threshold_tokens', 'last_prompt_tokens'):
                if not _usage.get(_field):
                    try:
                        _usage[_field] = getattr(_session_obj, _field, 0) or 0
                    except Exception:
                        pass

        _real_prompt_tokens = int(_usage.get('last_prompt_tokens') or 0)
        if _real_prompt_tokens and _real_prompt_tokens != _live_prompt_exact_tokens[0]:
            _live_prompt_exact_tokens[0] = _real_prompt_tokens
            _live_prompt_estimate_tokens[0] = _real_prompt_tokens
        elif _live_prompt_estimate_tokens[0] > _real_prompt_tokens:
            _usage['last_prompt_tokens'] = _live_prompt_estimate_tokens[0]

        return _usage

    # Register this stream with the global streaming meter
    meter().begin_session(stream_id)

    # Metering ticker — emits a metering event at 1 Hz while sessions are active.
    # When get_interval() returns >= 10.0 (no active sessions), the ticker exits
    # so no idle readings are emitted and the SSE consumer sees nothing.
    _metering_stop = threading.Event()

    def _metering_ticker():
        while True:
            interval = meter().get_interval()
            if interval >= 10.0:
                break  # nothing active — stop the ticker
            if _metering_stop.wait(interval):
                break  # stream was cancelled or ended — exit
            stats = meter().get_stats()
            stats['session_id'] = session_id
            stats['usage'] = _live_usage_snapshot()
            put('metering', stats)

    _metering_thread = threading.Thread(target=_metering_ticker, daemon=True)
    _metering_thread.start()

    def put(event, data):
        # If cancelled, drop all further events except the cancel event itself
        if cancel_event.is_set() and event not in ('cancel', 'error'):
            return
        try:
            q.put_nowait((event, data))
        except Exception:
            logger.debug("Failed to put event to queue")

    def _agent_status_callback(kind, message):
        """Bridge Agent lifecycle compression status into WebUI SSE."""
        _message = str(message or '').strip()
        _kind = str(kind or '').strip().lower()
        if not _message:
            return
        _lower = _message.lower()
        _is_compression_start = (
            _kind == 'lifecycle'
            and (
                'preflight compression' in _lower
                or 'compressing' in _lower
                or 'compacting context' in _lower
                or 'context too large' in _lower
            )
        )
        if not _is_compression_start:
            return
        put('compressing', {
            'session_id': session_id,
            'message': 'Auto-compressing context to continue...',
        })

    # Initialised here (before any code that may raise) so the outer `finally`
    # block can safely check `if _checkpoint_stop is not None` even when an
    # exception fires before the checkpoint thread is created (Issue #765).
    _checkpoint_stop = None
    _ckpt_thread = None
    _agent_lock = None
    try:
        s = get_session(session_id)
        update_active_run(stream_id, phase="running", session_id=session_id)
        s.workspace = str(Path(workspace).expanduser().resolve())
        s.model = model
        provider_context = (
            str(model_provider).strip().lower()
            if model_provider is not None
            else getattr(s, "model_provider", None)
        )
        s.model_provider = provider_context or None

        _agent_lock = _get_session_agent_lock(session_id)
        # TD1: set thread-local env context so concurrent sessions don't clobber globals
        # Check for pre-flight cancel (user cancelled before agent even started)
        if cancel_event.is_set():
            with _agent_lock:
                _finalize_cancelled_turn(s, ephemeral=ephemeral, message='Task cancelled before start.')
            put('cancel', {'message': 'Cancelled before start'})
            return

        # Resolve profile home for this agent run — use the session's own profile
        # (stamped at new_session() time from the client's S.activeProfile) so that
        # two concurrent tabs on different profiles don't clobber each other via the
        # process-level active-profile global.  Falls back gracefully.
        try:
            from api.profiles import (
                patch_skill_home_modules,
                get_hermes_home_for_profile,
                get_profile_runtime_env,
            )
            _profile_home_path = get_hermes_home_for_profile(getattr(s, 'profile', None))
            _profile_home = str(_profile_home_path)
            _profile_runtime_env = get_profile_runtime_env(_profile_home_path)
        except ImportError:
            _profile_home = os.environ.get('HERMES_HOME', '')
            _profile_runtime_env = {}
            patch_skill_home_modules = None
        
        # Capture the resolved profile name now, while profile context is
        # reliable. Used in the compression migration block to stamp s.profile
        # on the continuation session. We resolve it here rather than calling
        # get_active_profile_name() at compression time because that function
        # reads thread-local storage (_tls.profile) set by set_request_profile()
        # on the HTTP handler thread. The streaming thread is a separate
        # threading.Thread and does not inherit TLS. At compression time,
        # get_active_profile_name() would fall back to the process-global
        # _active_profile, which may belong to a different concurrent tab.
        _resolved_profile_name = getattr(s, 'profile', None)
        if not _resolved_profile_name:
            try:
                from api.profiles import get_active_profile_name
                _resolved_profile_name = get_active_profile_name()
            except Exception:
                _resolved_profile_name = None
        
        _thread_env = _build_agent_thread_env(
            _profile_runtime_env,
            str(s.workspace),
            session_id,
            _profile_home,
        )
        _set_thread_env(**_thread_env)
        # Prewarm skill-tool imports *before* acquiring the lock so that
        # first-time module initialisation (which can be slow) does not
        # block other concurrent sessions waiting on _ENV_LOCK (#2024).
        _prewarm_skill_tool_modules()
        # Still set process-level env as fallback for tools that bypass thread-local
        # Acquire lock only for the env mutation, then release before the agent runs.
        # The finally block re-acquires to restore — keeping critical sections short
        # and preventing a deadlock where the restore would re-enter the same lock.
        with _ENV_LOCK:
            old_profile_env = {key: os.environ.get(key) for key in _profile_runtime_env}
            old_cwd = os.environ.get('TERMINAL_CWD')
            old_exec_ask = os.environ.get('HERMES_EXEC_ASK')
            old_session_key = os.environ.get('HERMES_SESSION_KEY')
            old_session_id = os.environ.get('HERMES_SESSION_ID')
            old_session_platform = os.environ.get('HERMES_SESSION_PLATFORM')
            old_hermes_home = os.environ.get('HERMES_HOME')
            os.environ.update(_profile_runtime_env)
            os.environ['TERMINAL_CWD'] = str(s.workspace)
            os.environ['HERMES_EXEC_ASK'] = '1'
            os.environ['HERMES_SESSION_KEY'] = session_id
            os.environ['HERMES_SESSION_ID'] = session_id
            os.environ['HERMES_SESSION_PLATFORM'] = 'webui'
            if _profile_home:
                os.environ['HERMES_HOME'] = _profile_home
                # Patch module-level caches to match the active profile.
                # _set_hermes_home() does this for process-wide switches
                # but per-request switches skip it (#1700).
                # Modules were prewarmed by _prewarm_skill_tool_modules()
                # above, so we only do lightweight sys.modules lookups and
                # attribute assignments here — no first-time import under
                # the lock (#2024).
                if patch_skill_home_modules is not None:
                    patch_skill_home_modules(Path(_profile_home))
        # Lock released — agent runs without holding it
        # ── MCP Server Discovery (lazy import, idempotent) ──
        # MUST run AFTER the HERMES_HOME mutation above — `discover_mcp_tools()`
        # reads `~/.hermes/config.yaml` via `get_hermes_home()`, which uses
        # `os.environ['HERMES_HOME']`.  Calling it before the mutation always
        # loaded the default profile's `mcp_servers`, even when the session
        # was stamped with a non-default profile.  See issue #1968.
        #
        # NOTE: `_servers` in `tools/mcp_tool.py` is a process-global registry
        # keyed by server name.  This means once profile A registers a server
        # named e.g. `postgres`, profile B's discovery sees it as already
        # connected and skips it — even if B's config points at a different
        # binary.  Fully fixing multi-profile concurrent use requires keying
        # `_servers` by `(profile_home, name)` upstream in hermes-agent; that
        # lives outside this WebUI repo.  This change fixes the headline bug
        # for users who run a single non-default profile per WebUI process.
        try:
            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()
        except Exception:
            pass  # MCP not available or not configured — non-fatal

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
            timeout = _clarify_timeout_seconds()
            choices_list = [str(choice) for choice in (choices or [])]
            data = {
                'question': str(question or ''),
                'choices_offered': choices_list,
                'session_id': sid,
                'kind': 'clarify',
                'requested_at': time.time(),
                'timeout_seconds': timeout,
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
            _self_healed = False  # (#1401) prevents infinite self-heal retries
            _reasoning_text = ''  # accumulates reasoning/thinking trace for persistence
            _live_tool_calls = []  # tool progress fallback when final messages omit tool IDs

            # Throttle: emit metering events at most every 100 ms so the per-message
            # TPS label feels live during fast token streams without flooding SSE.
            _metering_last_emit = [time.monotonic() - 1]  # fire immediately on first token
            _metering_output_deltas = [0]
            _metering_reasoning_deltas = [0]

            def _emit_metering():
                now = time.monotonic()
                if now - _metering_last_emit[0] < 0.1:
                    return
                _metering_last_emit[0] = now
                stats = meter().get_stats()
                stats['session_id'] = session_id
                stats['usage'] = _live_usage_snapshot()
                stats.setdefault('tps_available', False)
                stats.setdefault('estimated', False)
                put('metering', stats)

            def on_token(text):
                nonlocal _token_sent
                if text is None:
                    return  # end-of-stream sentinel
                _token_sent = True
                # Accumulate partial text so cancel_stream() can persist it (#893)
                if stream_id in STREAM_PARTIAL_TEXT:
                    STREAM_PARTIAL_TEXT[stream_id] += str(text)
                put('token', {'text': text})
                # Update live throughput from stream delta callbacks, not from
                # byte/character length. If a backend cannot provide live deltas,
                # the frontend hides TPS rather than showing an estimate.
                _metering_output_deltas[0] += 1
                meter().record_token(stream_id, _metering_output_deltas[0])
                _emit_metering()

            def on_reasoning(text):
                nonlocal _reasoning_text
                if text is None:
                    return
                _reasoning_text += str(text)
                # Mirror to shared dict so cancel_stream() can persist it (#1361 §A)
                if stream_id in STREAM_REASONING_TEXT:
                    STREAM_REASONING_TEXT[stream_id] += str(text)
                put('reasoning', {'text': str(text)})
                # Track reasoning deltas in the meter so live TPS reflects all AI output.
                _metering_reasoning_deltas[0] += 1
                meter().record_reasoning(stream_id, _metering_reasoning_deltas[0])
                _emit_metering()

            def on_interim_assistant(text, **cb_kwargs):
                if text is None:
                    return
                visible = str(text).strip()
                if not visible:
                    return
                put('interim_assistant', {
                    'text': visible,
                    'already_streamed': bool(cb_kwargs.get('already_streamed', False)),
                })

            # Pre-initialise the activity counter here so on_tool (which
            # closes over it) never captures an unbound name even if this
            # block is reordered later (Issue #765).
            _checkpoint_activity = [0]

            def _record_live_tool_start(tool_call_id, name, args):
                if not tool_call_id or tool_call_id in _live_prompt_estimate_seen_ids:
                    return
                _live_prompt_estimate_seen_ids.add(tool_call_id)
                _tool_call = {
                    'id': tool_call_id,
                    'type': 'function',
                    'function': {
                        'name': str(name or ''),
                        'arguments': json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, sort_keys=True),
                    },
                }
                _bump_live_prompt_estimate([{
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [_tool_call],
                }])

            def _record_live_tool_complete(tool_call_id, name, function_result):
                if not tool_call_id:
                    return
                _result_text = _tool_result_snippet(function_result)
                _bump_live_prompt_estimate([{
                    'role': 'tool',
                    'name': str(name or ''),
                    'tool_call_id': tool_call_id,
                    'content': _result_text,
                }])

            def on_tool(*cb_args, **cb_kwargs):
                nonlocal _reasoning_text
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
                        _reasoning_text += str(reason_text)
                        # Mirror to shared dict so cancel_stream() can persist it (#1361 §A)
                        if stream_id in STREAM_REASONING_TEXT:
                            STREAM_REASONING_TEXT[stream_id] += str(reason_text)
                        put('reasoning', {'text': str(reason_text)})
                        _metering_reasoning_deltas[0] += 1
                        meter().record_reasoning(stream_id, _metering_reasoning_deltas[0])
                        _emit_metering()
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
                    # Mirror to shared dict so cancel_stream() can persist it (#1361 §B)
                    if stream_id in STREAM_LIVE_TOOL_CALLS:
                        STREAM_LIVE_TOOL_CALLS[stream_id].append({
                            'name': name,
                            'args': args if isinstance(args, dict) else {},
                            'done': False,
                        })
                    put('tool', {
                        'event_type': event_type or 'tool.started',
                        'name': name,
                        'preview': preview,
                        'args': args_snap,
                    })
                    _tool_stats = meter().get_stats()
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
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
                    # Mirror done state to shared dict (#1361 §B)
                    if stream_id in STREAM_LIVE_TOOL_CALLS:
                        for shared_tc in reversed(STREAM_LIVE_TOOL_CALLS[stream_id]):
                            if shared_tc.get('done'):
                                continue
                            if not name or shared_tc.get('name') == name:
                                shared_tc['done'] = True
                                shared_tc['duration'] = cb_kwargs.get('duration')
                                shared_tc['is_error'] = bool(cb_kwargs.get('is_error', False))
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
                    _tool_stats = meter().get_stats()
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
                    return

            def on_tool_start(tool_call_id, name, args):
                try:
                    _record_live_tool_start(tool_call_id, name, args)
                    _tool_stats = meter().get_stats()
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
                except Exception:
                    logger.debug('Failed to update live prompt estimate on tool start', exc_info=True)

            def on_tool_complete(tool_call_id, name, args, function_result):
                try:
                    _record_live_tool_complete(tool_call_id, name, function_result)
                    _tool_stats = meter().get_stats()
                    _tool_stats['session_id'] = session_id
                    _tool_stats['usage'] = _live_usage_snapshot()
                    put('metering', _tool_stats)
                except Exception:
                    logger.debug('Failed to update live prompt estimate on tool completion', exc_info=True)

            _AIAgent = _get_ai_agent()
            if _AIAgent is None:
                raise ImportError(_aiagent_import_error_detail())

            # Initialize SessionDB so session_search works in WebUI sessions
            _session_db = None
            try:
                from hermes_state import SessionDB
                _session_db = SessionDB()
            except Exception as _db_err:
                print(f"[webui] WARNING: SessionDB init failed — session_search will be unavailable: {_db_err}", flush=True)
            resolved_model, resolved_provider, resolved_base_url = resolve_model_provider(
                model_with_provider_context(model, provider_context)
            )

            # Resolve API key via Hermes runtime provider (matches gateway behaviour).
            # Pass the resolved provider so non-default providers get their own credentials.
            resolved_api_key = None
            try:
                from api.oauth import resolve_runtime_provider_with_anthropic_env_lock
                from hermes_cli.runtime_provider import resolve_runtime_provider
                _rt = resolve_runtime_provider_with_anthropic_env_lock(
                    resolve_runtime_provider,
                    requested=resolved_provider,
                )
                resolved_api_key = _rt.get("api_key")
                if not resolved_provider:
                    resolved_provider = _rt.get("provider")
                if not resolved_base_url:
                    resolved_base_url = _rt.get("base_url")
            except Exception as _e:
                print(f"[webui] WARNING: resolve_runtime_provider failed: {_e}", flush=True)

            # Named custom providers (custom:slug) may not be resolvable by
            # hermes_cli.runtime_provider directly. Fall back to config.yaml
            # custom_providers[] so WebUI can pass explicit creds/base_url.
            if isinstance(resolved_provider, str) and resolved_provider.startswith("custom:"):
                _cp_key, _cp_base = resolve_custom_provider_connection(resolved_provider)
                if not resolved_api_key and _cp_key:
                    resolved_api_key = _cp_key
                if not resolved_base_url and _cp_base:
                    resolved_base_url = _cp_base

            # Read per-profile config at call time (not module-level snapshot)
            from api.config import get_config as _get_config
            _cfg = _get_config()

            # Per-profile toolsets — use _resolve_cli_toolsets() so MCP
            # server toolsets are included, matching native CLI behaviour.
            from api.config import _resolve_cli_toolsets
            _toolsets = _resolve_cli_toolsets(_cfg)

            # Per-session toolset override (#493): if the session has
            # enabled_toolsets set, use that instead of the global config.
            try:
                from api.models import Session, SESSION_DIR
                _session_path = SESSION_DIR / f"{session_id}.json"
                if _session_path.exists():
                    _session_meta = Session.load_metadata_only(session_id)
                    # load_metadata_only returns a Session INSTANCE, not a dict.
                    # The previous .get('enabled_toolsets') raised AttributeError
                    # which was swallowed by the bare except below — the entire
                    # per-session toolset override silently no-op'd. Use
                    # getattr() to read the attribute correctly.
                    # (Opus pre-release advisor finding for v0.50.257.)
                    _override = getattr(_session_meta, 'enabled_toolsets', None) if _session_meta else None
                    if _override:
                        _toolsets = _override
            except Exception as _ts_err:
                print(f"[webui] WARNING: failed to read per-session toolsets for {session_id}: {_ts_err}", flush=True)

            # Fallback model from profile config (e.g. for rate-limit recovery)
            _fallback = _cfg.get('fallback_model') or _cfg.get('fallback_providers') or None
            _fallback_resolved = None
            if _fallback:
                # Normalize: support both single dict (legacy) and list (chained fallback).
                # Use the first valid entry as the fallback passed to AIAgent.
                _fb_entry = None
                if isinstance(_fallback, list):
                    for _entry in _fallback:
                        if isinstance(_entry, dict) and _entry.get('model'):
                            _fb_entry = _entry
                            break
                elif isinstance(_fallback, dict) and _fallback.get('model'):
                    _fb_entry = _fallback
                if _fb_entry:
                    _fallback_resolved = {
                        'model': _fb_entry.get('model', ''),
                        'provider': _fb_entry.get('provider', ''),
                        'base_url': _fb_entry.get('base_url'),
                        'api_key': _fb_entry.get('api_key'),
                        'key_env': _fb_entry.get('key_env'),
                    }

            # Build kwargs defensively — guard newer params so the WebUI
            # degrades gracefully when run against an older hermes-agent build.
            # (fixes: TypeError: AIAgent.__init__() got an unexpected keyword
            # argument 'credential_pool' — issue #772)
            import inspect as _inspect
            _agent_params = set(_inspect.signature(_AIAgent.__init__).parameters)

            # CLI-parity max-iteration budget: read config.yaml's
            # agent.max_turns and pass it to AIAgent when supported. Without
            # this WebUI-created agents silently use AIAgent's constructor
            # default (90), so long browser-originated tasks hit the
            # "maximum number of tool-calling iterations" summary path even
            # after the operator raises Hermes' global turn budget.
            _max_iterations_cfg = None
            try:
                _raw_max_iterations = None
                _agent_cfg_for_iterations = _cfg.get('agent', {}) if isinstance(_cfg, dict) else {}
                if isinstance(_agent_cfg_for_iterations, dict):
                    _raw_max_iterations = _agent_cfg_for_iterations.get('max_turns')
                if _raw_max_iterations is None and isinstance(_cfg, dict):
                    # Back-compat for older Hermes config files that used a
                    # root-level max_turns key.
                    _raw_max_iterations = _cfg.get('max_turns')
                if _raw_max_iterations is not None:
                    _parsed_max_iterations = int(_raw_max_iterations)
                    if _parsed_max_iterations > 0:
                        _max_iterations_cfg = _parsed_max_iterations
            except Exception:
                _max_iterations_cfg = None

            # CLI-parity max output cap: read config.yaml's max_tokens and pass
            # it to AIAgent when supported. Without this WebUI-created agents use
            # provider-native output ceilings (e.g. Claude via OpenRouter can
            # request 64k), which may turn an otherwise usable fallback into a
            # 402 "more credits / fewer max_tokens" failure.
            _max_tokens_cfg = None
            try:
                _raw_max_tokens = _cfg.get('max_tokens')
                if _raw_max_tokens is None:
                    _agent_cfg_for_tokens = _cfg.get('agent', {})
                    if isinstance(_agent_cfg_for_tokens, dict):
                        _raw_max_tokens = _agent_cfg_for_tokens.get('max_tokens')
                if _raw_max_tokens is not None:
                    _parsed_max_tokens = int(_raw_max_tokens)
                    if _parsed_max_tokens > 0:
                        _max_tokens_cfg = _parsed_max_tokens
            except Exception:
                _max_tokens_cfg = None

            # CLI-parity reasoning effort: read agent.reasoning_effort from the
            # active profile's config.yaml (the same key the CLI writes via
            # `/reasoning <level>`) and hand the parsed dict to AIAgent.  When
            # the key is absent or invalid, pass None → agent uses its default.
            try:
                from api.config import parse_reasoning_effort as _parse_reff
                _effort_cfg = _cfg.get('agent', {}) if isinstance(_cfg, dict) else {}
                _effort_raw = _effort_cfg.get('reasoning_effort') if isinstance(_effort_cfg, dict) else None
                _reasoning_config = _parse_reff(_effort_raw)
            except Exception:
                _reasoning_config = None

            _agent_kwargs = dict(
                model=resolved_model,
                provider=resolved_provider,
                base_url=resolved_base_url,
                api_key=resolved_api_key,
                # Identify browser-originated sessions as WebUI so Hermes Agent
                # does not inject CLI-specific terminal/output guidance.
                platform='webui',
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
            if 'interim_assistant_callback' in _agent_params:
                _agent_kwargs['interim_assistant_callback'] = on_interim_assistant
            if 'tool_start_callback' in _agent_params:
                _agent_kwargs['tool_start_callback'] = on_tool_start
            if 'tool_complete_callback' in _agent_params:
                _agent_kwargs['tool_complete_callback'] = on_tool_complete
            if 'status_callback' in _agent_params:
                _agent_kwargs['status_callback'] = _agent_status_callback
            if 'max_iterations' in _agent_params and _max_iterations_cfg is not None:
                _agent_kwargs['max_iterations'] = _max_iterations_cfg
            if 'max_tokens' in _agent_params and _max_tokens_cfg is not None:
                _agent_kwargs['max_tokens'] = _max_tokens_cfg
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

            # ── Agent cache: reuse across messages in the same session ──
            # Mirrors gateway _agent_cache.  Keeps _user_turn_count alive so
            # injectionFrequency: "first-turn" actually suppresses after turn 1.
            if ephemeral:
                agent = _AIAgent(**_agent_kwargs)
                logger.debug('[webui] Created ephemeral agent for session %s', session_id)
            else:
                import hashlib as _hashlib
                import json as _json
                from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
                _sig_blob = _json.dumps([
                    resolved_model or '',
                    _hashlib.sha256((resolved_api_key or '').encode()).hexdigest()[:16],
                    resolved_base_url or '',
                    resolved_provider or '',
                    _max_iterations_cfg or '',
                    _max_tokens_cfg or '',
                    _fallback_resolved or {},
                    sorted(_toolsets) if _toolsets else [],
                    _reasoning_config or {},
                    # #1897: profile_home is part of the agent's identity because
                    # AIAgent caches `_cached_system_prompt` from `load_soul_md()`
                    # at construction time, sourced from HERMES_HOME. Same-session
                    # profile switches keep `session_id` stable, so without this
                    # field the cached agent silently retains the previous
                    # profile's SOUL.md (and any other profile-scoped context).
                    _profile_home or '',
                ], sort_keys=True)
                _agent_sig = _hashlib.sha256(_sig_blob.encode()).hexdigest()[:16]

                agent = None
                with SESSION_AGENT_CACHE_LOCK:
                    _cached = SESSION_AGENT_CACHE.get(session_id)
                    if _cached and _cached[1] == _agent_sig:
                        agent = _cached[0]
                        SESSION_AGENT_CACHE.move_to_end(session_id)  # LRU: mark as recently used
                        logger.debug('[webui] Reusing cached agent for session %s', session_id)

                if agent is not None:
                    # Refresh per-turn callbacks — these close over request-scoped
                    # objects (put queue, cancel_event) that are new each request.
                    agent.stream_delta_callback = _agent_kwargs.get('stream_delta_callback')
                    agent.tool_progress_callback = _agent_kwargs.get('tool_progress_callback')
                    if hasattr(agent, 'tool_start_callback'):
                        agent.tool_start_callback = _agent_kwargs.get('tool_start_callback')
                    if hasattr(agent, 'tool_complete_callback'):
                        agent.tool_complete_callback = _agent_kwargs.get('tool_complete_callback')
                    if hasattr(agent, 'status_callback'):
                        agent.status_callback = _agent_kwargs.get('status_callback')
                    if hasattr(agent, 'interim_assistant_callback'):
                        agent.interim_assistant_callback = _agent_kwargs.get('interim_assistant_callback')
                    if hasattr(agent, 'reasoning_callback'):
                        agent.reasoning_callback = _agent_kwargs.get('reasoning_callback')
                    if hasattr(agent, 'clarify_callback'):
                        agent.clarify_callback = _agent_kwargs.get('clarify_callback')
                    if _session_db is not None:
                        # Close any previously held SessionDB connection before
                        # replacing it. Without this, each streaming request creates
                        # a new SessionDB whose WAL handles leak indefinitely,
                        # eventually causing EMFILE crashes (#streaming FD leak).
                        if hasattr(agent, '_session_db') and agent._session_db is not None:
                            try:
                                agent._session_db.close()
                            except Exception:
                                pass
                        agent._session_db = _session_db
                    if hasattr(agent, '_api_call_count'):
                        agent._api_call_count = 0
                    # Reset interrupt state from a prior cancel so the reused
                    # agent does not think it is still interrupted.
                    if hasattr(agent, '_interrupted'):
                        agent._interrupted = False
                    if hasattr(agent, '_interrupt_message'):
                        agent._interrupt_message = None
                else:
                    agent = _AIAgent(**_agent_kwargs)
                    with SESSION_AGENT_CACHE_LOCK:
                        SESSION_AGENT_CACHE[session_id] = (agent, _agent_sig)
                        SESSION_AGENT_CACHE.move_to_end(session_id)  # LRU: mark as recently used
                        from api.config import SESSION_AGENT_CACHE_MAX
                        while len(SESSION_AGENT_CACHE) > SESSION_AGENT_CACHE_MAX:
                            evicted_sid, evicted_entry = SESSION_AGENT_CACHE.popitem(last=False)
                            # Same FD-leak shape as the cached-agent reuse path
                            # in #1421: the evicted agent's _session_db won't be
                            # released until GC finalizes the agent, which on a
                            # long-running server may be never. Close it
                            # explicitly so the WAL handles release immediately.
                            # (Opus pre-release follow-up to #1421.)
                            try:
                                _evicted_agent = evicted_entry[0] if isinstance(evicted_entry, tuple) else None
                                if _evicted_agent is not None and getattr(_evicted_agent, '_session_db', None) is not None:
                                    _evicted_agent._session_db.close()
                            except Exception:
                                pass
                            logger.debug('[webui] Evicted LRU agent from cache: %s', evicted_sid)
                    logger.debug('[webui] Created new agent for session %s', session_id)

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
                    with _agent_lock:
                        _finalize_cancelled_turn(s, ephemeral=ephemeral, message='Task cancelled before start.')
                    put('cancel', {'message': 'Cancelled by user'})
                    return

            # Prepend workspace context so the agent always knows which directory
            # to use for file operations, regardless of session age or AGENTS.md defaults.
            workspace_ctx = _workspace_context_prefix(str(s.workspace))
            workspace_system_msg = (
                f"Active workspace at session start: {s.workspace}\n"
                "Every user message is prefixed with [Workspace::v1: /absolute/path] indicating the "
                "workspace the user has selected in the web UI at the time they sent that message. "
                "This tag is the single authoritative source of the active workspace and updates "
                "with every message. It overrides any prior workspace mentioned in this system "
                "prompt, memory, or conversation history. Always use the value from the most recent "
                "[Workspace::v1: ...] tag as your default working directory for ALL file operations: "
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
            _pending_started_at = getattr(s, 'pending_started_at', None)
            # Normal chat-start sets pending_started_at before spawning this thread;
            # fallback to now only for recovered/legacy flows where that marker is absent
            # or has been zeroed out (e.g. via a buggy migration / manual file edit).
            # Truthy-check covers None, missing-attr, and 0 uniformly.
            _turn_started_at = _pending_started_at if _pending_started_at else time.time()
            _previous_messages = list(s.messages or [])
            _previous_context_messages = _drop_checkpointed_current_user_from_context(
                _session_context_messages(s),
                msg_text,
            )
            _pre_compression_count = getattr(
                getattr(agent, 'context_compressor', None),
                'compression_count', 0,
            )

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
            # Persist the user message BEFORE streaming starts so it's durable even if
            # the server crashes before the first checkpoint fires (every 15s).
            with _agent_lock:
                s.save(touch_updated_at=True, skip_index=False)

            _ckpt_thread = threading.Thread(
                target=_periodic_checkpoint, daemon=True,
                name=f"ckpt-{session_id[:8]}",
            )
            _ckpt_thread.start()

            _process_notifications = _drain_webui_process_notifications(session_id)
            _agent_msg_text = msg_text
            if _process_notifications:
                _agent_msg_text = "\n\n".join([*_process_notifications, msg_text]).strip()
            user_message = _build_native_multimodal_message(workspace_ctx, _agent_msg_text, attachments, workspace, cfg=_cfg)
            result = agent.run_conversation(
                user_message=user_message,
                system_message=workspace_system_msg,
                conversation_history=_sanitize_messages_for_api(_previous_context_messages),
                task_id=session_id,
                persist_user_message=msg_text,
            )
            if cancel_event.is_set():
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()
                if _ckpt_thread is not None:
                    _ckpt_thread.join(timeout=15)
                if ephemeral:
                    _cleanup_ephemeral_cancelled_turn(s)
                else:
                    with _agent_lock:
                        _finalize_cancelled_turn(s, ephemeral=False)
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "interrupted",
                                    "created_at": time.time(),
                                    "reason": "cancelled",
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                put('cancel', {'message': 'Cancelled by user'})
                return
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
            if cancel_event.is_set():
                with _agent_lock:
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                put('cancel', {'message': 'Cancelled by user'})
                return
            with _agent_lock:
                if not ephemeral and not _stream_writeback_is_current(s, stream_id):
                    logger.info(
                        "Skipping stale stream writeback for session %s stream %s; active_stream_id=%s",
                        getattr(s, 'session_id', session_id),
                        stream_id,
                        getattr(s, 'active_stream_id', None),
                    )
                    return
                _result_messages = result.get('messages') or _previous_context_messages
                if cancel_event.is_set():
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                    put('cancel', {'message': 'Cancelled by user'})
                    return
                _next_context_messages = _restore_reasoning_metadata(
                    _previous_context_messages,
                    _result_messages,
                )
                s.context_messages = _next_context_messages
                s.messages = _merge_display_messages_after_agent_result(
                    _previous_messages,
                    _previous_context_messages,
                    _restore_reasoning_metadata(_previous_messages, _result_messages),
                    msg_text,
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
                #
                # Only check NEW messages added by this turn — result.get('messages')
                # includes the full conversation history, so checking all of them would
                # match assistant content from prior turns and mask the real failure.
                _all_result_messages = result.get('messages') or []
                _prev_len = len(_previous_context_messages)
                _assistant_added = _has_new_assistant_reply(_all_result_messages, _prev_len)
                # _token_sent tracks whether on_token() was called (any streamed text)
                if not _assistant_added and not _token_sent:
                    if cancel_event.is_set():
                        _finalize_cancelled_turn(s, ephemeral=ephemeral)
                        if not ephemeral:
                            try:
                                append_turn_journal_event_for_stream(
                                    s.session_id,
                                    stream_id,
                                    {
                                        "event": "interrupted",
                                        "created_at": time.time(),
                                        "reason": "cancelled",
                                    },
                                )
                            except Exception:
                                logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                        put('cancel', {'message': 'Cancelled by user'})
                        return
                    _last_err = getattr(agent, '_last_error', None) or result.get('error') or ''
                    _err_str = str(_last_err) if _last_err else ''
                    _classification = _classify_provider_error(
                        _err_str,
                        _last_err,
                        silent_failure=not bool(_err_str),
                    )
                    _is_quota = _classification['type'] == 'quota_exhausted'
                    _is_auth = _classification['type'] == 'auth_mismatch'
                    if _is_quota:
                        _err_label = _classification['label']
                        _err_type = _classification['type']
                        _err_hint = _classification['hint']
                    elif _is_auth and not _self_healed:
                        # ── Credential self-heal on 401 (#1401) ──
                        # Before emitting the error, try re-reading credentials
                        # and retrying once with a fresh agent.
                        _heal_result = None
                        _heal_rt = _attempt_credential_self_heal(
                            resolved_provider or '', session_id, _agent_lock,
                        )
                        if _heal_rt is not None:
                            logger.info('[webui] self-heal: retrying stream after credential refresh')
                            # Rebuild runtime variables from the refreshed resolve
                            _rt = _heal_rt
                            resolved_api_key = _heal_rt.get('api_key')
                            if not resolved_provider:
                                resolved_provider = _heal_rt.get('provider')
                            if not resolved_base_url:
                                resolved_base_url = _heal_rt.get('base_url')
                            if isinstance(resolved_provider, str) and resolved_provider.startswith('custom:'):
                                _cp_key, _cp_base = resolve_custom_provider_connection(resolved_provider)
                                if not resolved_api_key and _cp_key:
                                    resolved_api_key = _cp_key
                                if not resolved_base_url and _cp_base:
                                    resolved_base_url = _cp_base
                            # Rebuild agent kwargs and create a fresh agent
                            _agent_kwargs['api_key'] = resolved_api_key
                            _agent_kwargs['base_url'] = resolved_base_url
                            _agent_kwargs['model'] = resolved_model
                            _agent_kwargs['provider'] = resolved_provider
                            if 'credential_pool' in _agent_params:
                                _agent_kwargs['credential_pool'] = _heal_rt.get('credential_pool')
                            agent = _AIAgent(**_agent_kwargs)
                            with STREAMS_LOCK:
                                AGENT_INSTANCES[stream_id] = agent
                            from api.config import SESSION_AGENT_CACHE as _SAC, SESSION_AGENT_CACHE_LOCK as _SAC_L
                            with _SAC_L:
                                _SAC[session_id] = (agent, _agent_sig)
                                _SAC.move_to_end(session_id)
                            # Retry the conversation once with fresh credentials
                            _self_healed = True
                            _token_sent = False
                            try:
                                _heal_result = agent.run_conversation(
                                    user_message=user_message,
                                    system_message=workspace_system_msg,
                                    conversation_history=_sanitize_messages_for_api(_previous_context_messages),
                                    task_id=session_id,
                                    persist_user_message=msg_text,
                                )
                                _heal_all_msgs = _heal_result.get('messages') or []
                                _heal_ok = _has_new_assistant_reply(_heal_all_msgs, _prev_len) or _token_sent
                            except Exception as _retry_exc:
                                logger.warning(
                                    '[webui] self-heal: retry also failed: %s', _retry_exc,
                                )
                                _heal_ok = False
                            if _heal_ok and _heal_result is not None:
                                # Retry succeeded — replace result and skip error
                                result = _heal_result
                                # Fall through past the error-emission block;
                                # the post-result persistence code below will
                                # process ``result`` normally.  We jump past
                                # the ``put('apperror', ...)`` + ``return`` by
                                # NOT entering the ``if not _assistant_added``
                                # guard again — but we are already inside it.
                                # Solution: set _assistant_added so the guard
                                # evaluates False on next conceptual pass.
                                # Since we're in a flat block, directly run the
                                # post-result merge logic here.
                                _result_messages = result.get('messages') or _previous_context_messages
                                _next_context_messages = _restore_reasoning_metadata(
                                    _previous_context_messages,
                                    _result_messages,
                                )
                                s.context_messages = _next_context_messages
                                s.messages = _merge_display_messages_after_agent_result(
                                    _previous_messages,
                                    _previous_context_messages,
                                    _restore_reasoning_metadata(_previous_messages, _result_messages),
                                    msg_text,
                                )
                                # Skip the error block — jump directly to the
                                # normal post-result persistence path by
                                # leaving _assistant_added truthy (set below).
                                _assistant_added = True  # prevent re-entering guard
                        if not _assistant_added:
                            # Self-heal didn't apply or retry failed — emit error
                            _err_label = 'Authentication failed'
                            _err_type = 'auth_mismatch'
                            _err_hint = (
                                'The selected model may not be supported by your configured provider or '
                                'your API key is invalid. Run `hermes model` in your terminal to '
                                'update credentials, then restart the WebUI.'
                            )
                    elif _is_auth:
                        _err_label = 'Authentication failed'
                        _err_type = 'auth_mismatch'
                        _err_hint = (
                            'The selected model may not be supported by your configured provider or '
                            'your API key is invalid. Run `hermes model` in your terminal to '
                            'update credentials, then restart the WebUI.'
                        )
                    else:
                        _err_label = _classification['label']
                        _err_type = _classification['type']
                        _err_hint = _classification['hint']
                    # Skip error emission if credential self-heal succeeded
                    # (#1401) — _assistant_added is set True on successful retry.
                    if _assistant_added:
                        # Self-heal succeeded: messages are already merged into s,
                        # fall through to normal post-result persistence below.
                        pass
                    else:
                        _error_payload = _provider_error_payload(
                            _err_str or f'{_err_label}.',
                            _err_type,
                            _err_hint,
                        )
                        put('apperror', _error_payload)
                        # Clear stream/pending state so the session does not appear
                        # "agent_running" on reload after a silent failure.
                        # Persist the error so it survives page reload.
                        # _error=True ensures _sanitize_messages_for_api excludes it from
                        # subsequent API calls so the LLM never sees its own error as prior context.
                        _materialize_pending_user_turn_before_error(s)
                        s.active_stream_id = None
                        s.pending_user_message = None
                        s.pending_attachments = []
                        s.pending_started_at = None
                        _error_message = {
                            'role': 'assistant',
                            'content': f'**{_err_label}:** {_error_payload.get("message") or _err_label}\n\n*{_err_hint}*',
                            'timestamp': int(time.time()),
                            '_error': True,
                        }
                        if _error_payload.get('details'):
                            _error_message['provider_details'] = _error_payload['details']
                        if _err_type == 'cancelled':
                            _error_message['provider_details_label'] = 'Cancellation details'
                        elif _err_type == 'interrupted':
                            _error_message['provider_details_label'] = 'Interruption details'
                        s.messages.append(_error_message)
                        try:
                            s.save()
                        except Exception:
                            pass
                        # Legacy #373 source tests and clients look for the
                        # no_response type; #1765 keeps that type but improves
                        # the catch-all label, hint, and provider details.
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
                    s.session_id = new_sid
                    # Carry profile identity across the compression boundary.
                    # Without this, s.profile stays None on the continuation
                    # session. On the next request, _run_agent_streaming calls
                    # get_hermes_home_for_profile(getattr(s, 'profile', None))
                    # which falls back to the default profile's HERMES_HOME.
                    # Memory writes then land in the wrong profile's MEMORY.md.
                    # Stamping here also ensures s.save() persists a non-null
                    # profile field to the continuation session's JSON file,
                    # covering the case where the session is later evicted from
                    # SESSIONS and reconstructed from disk via Session.load().
                    if not s.profile and _resolved_profile_name:
                        s.profile = _resolved_profile_name
                        logger.info(
                            "Stamped profile=%r on continuation session %s after compression",
                            _resolved_profile_name, new_sid,
                        )
                    # Preserve the original session file so the full pre-compression
                    # history survives even when summarisation fails.  The previous
                    # implementation renamed old_sid.json → new_sid.json, which
                    # destroyed the only persistent copy of the uncompressed history
                    # before the new (possibly summary-only) session had been saved.
                    # If the LLM summariser also failed, the user was left with zero
                    # recoverable messages.  (#2223)
                    # ---
                    # Archive the old session: write its current state to disk so
                    # the full conversation history survives even when context
                    # compression removes messages from the model's context.  Skip
                    # the write when the file already contains up-to-date data
                    # (i.e. it was just saved by a checkpoint).
                    _preserve_pre_compression_snapshot(s, old_sid)
                    # Always link the continuation session to its immediate predecessor
                    # (the preserved snapshot).  This OVERRIDES any prior
                    # parent_session_id because the new continuation IS the next link
                    # in the chain: traversal walks new → old → old.parent → ... root.
                    # Stage-353 Opus SHOULD-FIX: previous `if not s.parent_session_id`
                    # guard skipped this stamp on fork-of-fork compressions, so a
                    # subsequent traversal from the new continuation would jump
                    # over the just-preserved snapshot back to the original fork
                    # parent, losing access to the recoverable history in old_sid.json.
                    s.parent_session_id = old_sid
                    with LOCK:
                        if old_sid in SESSIONS:
                            SESSIONS[new_sid] = SESSIONS.pop(old_sid)
                    # Migrate the per-session lock: alias new_sid to the held
                    # _agent_lock reference directly (not via old_sid lookup),
                    # then remove the old_sid entry to prevent a leak.
                    with SESSION_AGENT_LOCKS_LOCK:
                        SESSION_AGENT_LOCKS[new_sid] = _agent_lock
                        SESSION_AGENT_LOCKS.pop(old_sid, None)
                    # Migrate cached agent to the new session ID so the turn
                    # count survives context compression.
                    from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
                    with SESSION_AGENT_CACHE_LOCK:
                        _cached_entry = SESSION_AGENT_CACHE.pop(old_sid, None)
                        if _cached_entry:
                            SESSION_AGENT_CACHE[new_sid] = _cached_entry
                    _compressed = True
                # Also detect compression via the result dict or compressor state
                if not _compressed:
                    _compressor = getattr(agent, 'context_compressor', None)
                    if _compressor and getattr(_compressor, 'compression_count', 0) > _pre_compression_count:
                        _compressed = True
                # Notify the frontend that compression happened
                if _compressed:
                    visible_after = visible_messages_for_anchor(s.messages, auto_compression=True)
                    s.compression_anchor_visible_idx = (
                        max(0, len(visible_after) - 1) if visible_after else None
                    )
                    s.compression_anchor_message_key = (
                        _compression_anchor_message_key(visible_after[-1]) if visible_after else None
                    )
                    s.compression_anchor_summary = _compact_summary_text(
                        _compression_summary_from_messages(s.messages)
                        or _compression_summary_from_messages(s.context_messages)
                    )
                    put('compressed', {
                        'session_id': s.session_id,
                        'message': 'Context auto-compressed to continue the conversation',
                        'usage': _live_usage_snapshot(),
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
                # Read token/cost usage from the agent object (if available).
                # Per-turn overwrite (#1857): replace cumulative session totals with the
                # agent's most recent values, which already represent the current turn's
                # full prompt+completion (input_tokens are the entire context, not delta).
                # Defensive: only overwrite when the agent reports non-zero / non-None
                # values. A rebuilt-from-cache-miss agent (post-restart, post-LRU-eviction)
                # starts at zero; without this guard, the next turn would zero out the
                # persisted disk total before any new tokens were spent. Per Opus advisor
                # on stage-320: prevents restart-induced regression of session usage data.
                input_tokens = getattr(agent, 'session_prompt_tokens', 0) or 0
                output_tokens = getattr(agent, 'session_completion_tokens', 0) or 0
                estimated_cost = getattr(agent, 'session_estimated_cost_usd', None)
                if input_tokens > 0:
                    s.input_tokens = input_tokens
                if output_tokens > 0:
                    s.output_tokens = output_tokens
                if estimated_cost is not None:
                    s.estimated_cost = estimated_cost
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
                    display_attachments = [_attachment_name(a) for a in attachments if _attachment_name(a)]
                    for m in reversed(s.messages):
                        if m.get('role') == 'user':
                            content = str(m.get('content', ''))
                            # Match if content is part of the sent message or vice-versa
                            base_text = msg_text.split('\n\n[Attached files:')[0].strip() if '\n\n[Attached files:' in msg_text else msg_text
                            if base_text[:60] in content or content[:60] in msg_text:
                                m['attachments'] = display_attachments
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
                try:
                    _turn_duration_seconds = max(0.0, time.time() - float(_turn_started_at))
                except Exception:
                    _turn_duration_seconds = 0.0
                _turn_tps = None
                if output_tokens and _turn_duration_seconds > 0:
                    _turn_tps = round(float(output_tokens) / _turn_duration_seconds, 1)
                _gateway_routing = _extract_gateway_routing_metadata(
                    agent,
                    result,
                    requested_model=resolved_model or model,
                    requested_provider=resolved_provider,
                )
                if _gateway_routing:
                    s.gateway_routing = _gateway_routing
                    _history = list(getattr(s, 'gateway_routing_history', None) or [])
                    _history.append(_gateway_routing)
                    s.gateway_routing_history = _history[-50:]
                if s.messages:
                    for _dm in reversed(s.messages):
                        if isinstance(_dm, dict) and _dm.get('role') == 'assistant':
                            _dm['_turnDuration'] = round(_turn_duration_seconds, 3)
                            if _turn_tps is not None:
                                _dm['_turnTps'] = _turn_tps
                            if _gateway_routing:
                                _dm['_gatewayRouting'] = _gateway_routing
                            break
                # Persist context window data on the session so the context-ring
                # indicator survives a page reload (#1318). Must run BEFORE
                # s.save() for the same reason as the reasoning trace above.
                # The fields are captured into the SSE usage payload below; this
                # block writes them to the session itself so GET /api/session
                # returns them on reload instead of falling back to 0.
                _cc_for_save = getattr(agent, 'context_compressor', None)
                if _cc_for_save:
                    s.context_length = getattr(_cc_for_save, 'context_length', 0) or 0
                    s.threshold_tokens = getattr(_cc_for_save, 'threshold_tokens', 0) or 0
                    s.last_prompt_tokens = getattr(_cc_for_save, 'last_prompt_tokens', 0) or 0
                # Fallback: if the compressor didn't report a context_length
                # (fresh agent, interrupted stream, or compressor missing the
                # attribute), resolve it from the model's static metadata so
                # the indicator can still show a meaningful percentage.
                # Sourced from PR #1344 (@jasonjcwu) — extracted to a focused
                # follow-up after PR #1344 was closed as superseded by #1341.
                #
                # #1896: pass config_context_length, provider, and
                # custom_providers so explicit config overrides win over the
                # 256K default fallback. Without these, users on 1M-context
                # models who set `model.context_length: 1048576` (or rely on
                # a `custom_providers` per-model override) get a 256K
                # window in the persisted session and the SSE payload —
                # which then trips LCM auto-compress at ~25% of the wrong
                # value, cascading into 429 floods.
                if not getattr(s, 'context_length', 0):
                    try:
                        from agent.model_metadata import get_model_context_length
                        _cfg_ctx_len = None
                        _cfg_custom_providers = None
                        try:
                            _model_cfg_for_ctx = _cfg.get('model', {}) if isinstance(_cfg, dict) else {}
                            if isinstance(_model_cfg_for_ctx, dict):
                                _raw_cfg_ctx = _model_cfg_for_ctx.get('context_length')
                                if _raw_cfg_ctx is not None:
                                    try:
                                        _parsed_cfg_ctx = int(_raw_cfg_ctx)
                                        if _parsed_cfg_ctx > 0:
                                            _cfg_ctx_len = _parsed_cfg_ctx
                                    except (TypeError, ValueError):
                                        # Invalid config — let the resolver fall
                                        # through to provider/registry probing.
                                        pass
                            _raw_cp = _cfg.get('custom_providers') if isinstance(_cfg, dict) else None
                            if isinstance(_raw_cp, list):
                                _cfg_custom_providers = _raw_cp
                        except Exception:
                            pass
                        _resolved_cl = get_model_context_length(
                            getattr(agent, 'model', resolved_model or '') or '',
                            getattr(agent, 'base_url', '') or '',
                            config_context_length=_cfg_ctx_len,
                            provider=resolved_provider or '',
                            custom_providers=_cfg_custom_providers,
                        )
                        if _resolved_cl:
                            s.context_length = _resolved_cl
                    except TypeError:
                        # Older hermes-agent builds whose get_model_context_length
                        # signature pre-dates the config_context_length /
                        # custom_providers kwargs. Retry with the legacy 2-arg
                        # form so the indicator still resolves *something*.
                        try:
                            from agent.model_metadata import get_model_context_length as _legacy_cl
                            _resolved_cl = _legacy_cl(
                                getattr(agent, 'model', resolved_model or '') or '',
                                getattr(agent, 'base_url', '') or '',
                            )
                            if _resolved_cl:
                                s.context_length = _resolved_cl
                        except Exception:
                            pass
                    except Exception:
                        # Older hermes-agent builds may not expose this helper.
                        # Better to leave context_length=0 than crash the save.
                        pass
                if not ephemeral and s.messages:
                    _latest_assistant_idx = next(
                        (idx for idx in range(len(s.messages) - 1, -1, -1)
                         if isinstance(s.messages[idx], dict) and s.messages[idx].get('role') == 'assistant'),
                        None,
                    )
                    if _latest_assistant_idx is not None:
                        _latest_assistant = s.messages[_latest_assistant_idx]
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "assistant_started",
                                    "created_at": float(_latest_assistant.get('timestamp') or time.time()),
                                    "assistant_message_index": _latest_assistant_idx,
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append assistant_started turn journal event", exc_info=True)
                if cancel_event.is_set():
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                    put('cancel', {'message': 'Cancelled by user'})
                    return
                s.save()
                if cancel_event.is_set():
                    _finalize_cancelled_turn(s, ephemeral=False)
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": "cancelled",
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append cancelled turn journal event", exc_info=True)
                    put('cancel', {'message': 'Cancelled by user'})
                    return
                if not ephemeral:
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "completed",
                                "created_at": time.time(),
                                "assistant_message_index": next(
                                    (idx for idx in range(len(s.messages) - 1, -1, -1)
                                     if isinstance(s.messages[idx], dict) and s.messages[idx].get('role') == 'assistant'),
                                    None,
                                ),
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append completed turn journal event", exc_info=True)
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
            usage = {
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'estimated_cost': estimated_cost,
                'duration_seconds': round(_turn_duration_seconds, 3),
            }
            if _turn_tps is not None:
                usage['tps'] = _turn_tps
            if _gateway_routing:
                usage['gateway_routing'] = _gateway_routing
            # Include context window data from the agent's compressor for the UI indicator.
            # The session-level persistence happens above (before s.save()) so the values
            # survive a page reload; this block only populates the live SSE usage payload.
            _cc = getattr(agent, 'context_compressor', None)
            if _cc:
                usage['context_length'] = getattr(_cc, 'context_length', 0) or 0
                usage['threshold_tokens'] = getattr(_cc, 'threshold_tokens', 0) or 0
                usage['last_prompt_tokens'] = getattr(_cc, 'last_prompt_tokens', 0) or 0
            # Fallback: when the compressor is absent or reports context_length=0,
            # resolve the model's context window from metadata so the UI indicator
            # shows the correct percentage rather than overflowing against the 128K
            # JS default.  Mirrors the session-save fallback above (lines ~2205-2217).
            #
            # #1896: pass config_context_length, provider, and custom_providers so
            # explicit config overrides win over the 256K default fallback. The
            # SSE payload's `context_length` is what feeds the live token-usage
            # indicator, so a stale 256K here surfaces as the same wrong-window
            # display that motivates this fix.
            if not usage.get('context_length'):
                try:
                    from agent.model_metadata import get_model_context_length as _get_cl
                    _cfg_ctx_len = None
                    _cfg_custom_providers = None
                    try:
                        _model_cfg_for_ctx = _cfg.get('model', {}) if isinstance(_cfg, dict) else {}
                        if isinstance(_model_cfg_for_ctx, dict):
                            _raw_cfg_ctx = _model_cfg_for_ctx.get('context_length')
                            if _raw_cfg_ctx is not None:
                                try:
                                    _parsed_cfg_ctx = int(_raw_cfg_ctx)
                                    if _parsed_cfg_ctx > 0:
                                        _cfg_ctx_len = _parsed_cfg_ctx
                                except (TypeError, ValueError):
                                    pass
                        _raw_cp = _cfg.get('custom_providers') if isinstance(_cfg, dict) else None
                        if isinstance(_raw_cp, list):
                            _cfg_custom_providers = _raw_cp
                    except Exception:
                        pass
                    try:
                        _fb_cl = _get_cl(
                            getattr(agent, 'model', resolved_model or '') or '',
                            getattr(agent, 'base_url', '') or '',
                            config_context_length=_cfg_ctx_len,
                            provider=resolved_provider or '',
                            custom_providers=_cfg_custom_providers,
                        )
                    except TypeError:
                        # Older hermes-agent builds: fall back to legacy 2-arg form.
                        _fb_cl = _get_cl(
                            getattr(agent, 'model', resolved_model or '') or '',
                            getattr(agent, 'base_url', '') or '',
                        )
                    if _fb_cl:
                        usage['context_length'] = _fb_cl
                except Exception:
                    pass
            # Fallback: when last_prompt_tokens is missing (no compressor), use the
            # session-persisted value rather than letting the frontend fall back to
            # the cumulative input_tokens counter, which overflows for long sessions.
            if not usage.get('last_prompt_tokens'):
                _sess_lpt = getattr(s, 'last_prompt_tokens', 0) or 0
                if _sess_lpt:
                    usage['last_prompt_tokens'] = _sess_lpt
            # (reasoning trace already attached + saved above, before s.save())
            # Leftover-steer delivery: if a /steer was queued (via
            # api/chat/steer) but the agent finished its turn before
            # reaching a tool-result boundary that would consume it,
            # the text is still stashed in agent._pending_steer. Drain
            # it now and emit a pending_steer_leftover SSE event so the
            # frontend can queue it for the next turn — same fallback
            # path as the CLI in cli.py:8788-8794.
            try:
                _drain_pending_steer = getattr(agent, '_drain_pending_steer', None)
                _leftover = _drain_pending_steer() if _drain_pending_steer else None
                if _leftover:
                    put('pending_steer_leftover', {
                        'session_id': session_id,
                        'text': str(_leftover),
                    })
            except Exception:
                logger.debug("Failed to drain pending steer for session %s", session_id)
            # /goal parity: after a successful assistant turn, run the Hermes
            # GoalManager judge before terminal done/stream_end events. The
            # frontend surfaces the status line and queues continuation_prompt as
            # a normal next user message so /queue and user input keep priority.
            # #1932: only evaluate when the turn was goal-related (set via
            # STREAM_GOAL_RELATED or goal_related parameter).
            try:
                from api.goals import evaluate_goal_after_turn, has_active_goal

                if not goal_related or not has_active_goal(session_id, profile_home=_profile_home):
                    _goal_decision = {}
                else:
                    _last_goal_response = ''
                    for _goal_msg in reversed(s.messages or []):
                        if not isinstance(_goal_msg, dict) or _goal_msg.get('role') != 'assistant':
                            continue
                        _goal_content = _goal_msg.get('content', '')
                        if isinstance(_goal_content, list):
                            _goal_parts = []
                            for _goal_part in _goal_content:
                                if isinstance(_goal_part, dict):
                                    _goal_text = _goal_part.get('text') or _goal_part.get('content')
                                    if _goal_text:
                                        _goal_parts.append(str(_goal_text))
                            _last_goal_response = '\n'.join(_goal_parts)
                        else:
                            _last_goal_response = str(_goal_content or '')
                        break
                    put('goal', {
                        'session_id': session_id,
                        'state': 'evaluating',
                        'message': 'Evaluating goal progress…',
                        'message_key': 'goal_evaluating_progress',
                    })
                    _goal_decision = evaluate_goal_after_turn(
                        session_id,
                        _last_goal_response,
                        user_initiated=True,
                        profile_home=_profile_home,
                    )
                decision = _goal_decision or {}
                _goal_message = str(decision.get('message') or '').strip()
                if _goal_message:
                    put('goal', {
                        'session_id': session_id,
                        'state': 'continuing' if decision.get('should_continue') else 'idle',
                        'message': _goal_message,
                        'message_key': decision.get('message_key') or ('goal_continuing' if _goal_message else ''),
                        'message_args': decision.get('message_args') or [],
                        'decision': decision,
                    })
                if decision.get('should_continue'):
                    continuation_prompt = str(decision.get('continuation_prompt') or '').strip()
                    if continuation_prompt:
                        # #1932: mark this session as pending a goal continuation
                        # so the next /chat/start creates a goal-related stream.
                        PENDING_GOAL_CONTINUATION.add(session_id)
                        put('goal_continue', {
                            'session_id': session_id,
                            'continuation_prompt': continuation_prompt,
                            'text': continuation_prompt,
                            'message': _goal_message,
                            'message_key': decision.get('message_key') or 'goal_continuing',
                            'message_args': decision.get('message_args') or [],
                            'decision': decision,
                        })
            except Exception as _goal_exc:
                logger.debug("Goal continuation hook failed for session %s: %s", session_id, _goal_exc)
            raw_session = s.compact() | {'messages': s.messages, 'tool_calls': tool_calls}
            put('done', {'session': redact_session_data(raw_session), 'usage': usage})
            # Emit one last metering packet for the live message-header TPS label.
            meter_stats = meter().get_stats()
            meter_stats['session_id'] = session_id
            meter_stats.setdefault('tps_available', False)
            meter_stats.setdefault('estimated', False)
            put('metering', meter_stats)
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
                # Adaptive title refresh: re-generate title from latest exchange
                # every N exchanges (when enabled in settings). Runs after stream_end
                # so it doesn't block the stream.
                _maybe_schedule_title_refresh(s, put, agent)
        finally:
            # Stop the live metering ticker
            _metering_stop.set()
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
                for _key, _old_value in old_profile_env.items():
                    if _old_value is None: os.environ.pop(_key, None)
                    else: os.environ[_key] = _old_value
                if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
                else: os.environ['TERMINAL_CWD'] = old_cwd
                if old_exec_ask is None: os.environ.pop('HERMES_EXEC_ASK', None)
                else: os.environ['HERMES_EXEC_ASK'] = old_exec_ask
                if old_session_key is None: os.environ.pop('HERMES_SESSION_KEY', None)
                else: os.environ['HERMES_SESSION_KEY'] = old_session_key
                if old_session_id is None: os.environ.pop('HERMES_SESSION_ID', None)
                else: os.environ['HERMES_SESSION_ID'] = old_session_id
                if old_session_platform is None: os.environ.pop('HERMES_SESSION_PLATFORM', None)
                else: os.environ['HERMES_SESSION_PLATFORM'] = old_session_platform
                if old_hermes_home is None: os.environ.pop('HERMES_HOME', None)
                else: os.environ['HERMES_HOME'] = old_hermes_home

    except Exception as e:
        print('[webui] stream error:\n' + traceback.format_exc(), flush=True)
        err_str = str(e)
        # Sanitize HTML from provider error responses — some providers return
        # full HTML pages (e.g. nginx "404 page not found") instead of JSON errors.
        # Strip HTML tags to avoid rendering raw markup in the chat message.
        _stripped = re.sub(r'<[^>]+>', ' ', err_str)
        _stripped = re.sub(r'\s+', ' ', _stripped).strip()
        if _stripped != err_str:
            err_str = _stripped
        _exc_lower = err_str.lower()
        _classification = _classify_provider_error(err_str, e)
        if cancel_event.is_set():
            if s is not None:
                if _checkpoint_stop is not None:
                    _checkpoint_stop.set()
                if _ckpt_thread is not None:
                    _ckpt_thread.join(timeout=15)
                _lock_ctx = _agent_lock if _agent_lock is not None else contextlib.nullcontext()
                with _lock_ctx:
                    _finalize_cancelled_turn(s, ephemeral=ephemeral)
                    if not ephemeral:
                        try:
                            append_turn_journal_event_for_stream(
                                s.session_id,
                                stream_id,
                                {
                                    "event": "interrupted",
                                    "created_at": time.time(),
                                    "reason": "cancelled",
                                },
                            )
                        except Exception:
                            logger.debug("Failed to append cancelled turn journal event", exc_info=True)
            put('cancel', {'message': 'Cancelled by user'})
            return
        _exc_is_quota = _classification['type'] == 'quota_exhausted'
        # Exception quota text still includes: 'more credits' in _exc_lower, 'can only afford' in _exc_lower, 'fewer max_tokens' in _exc_lower.
        # Rate-limit detection remains guarded as: (not _exc_is_quota).
        _exc_is_rate_limit = (_classification['type'] == 'rate_limit') and (not _exc_is_quota)
        _exc_is_auth = _classification['type'] == 'auth_mismatch'  # detects '401' and 'unauthorized' via _classify_provider_error.
        _exc_is_not_found = _classification['type'] == 'model_not_found'  # detects '404', 'not found', 'does not exist', and 'invalid model'.
        _exc_is_cancelled = _classification['type'] == 'cancelled'
        _exc_is_interrupted = _classification['type'] == 'interrupted'

        # The user hint still points to Settings / `hermes model` from _classify_provider_error().
        if _exc_is_quota:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_rate_limit:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_auth:
            if not _self_healed:
                # ── Credential self-heal on 401 (#1401) ──
                _heal_rt = _attempt_credential_self_heal(
                    resolved_provider or '', session_id, _agent_lock,
                )
                if _heal_rt is not None:
                    logger.info('[webui] self-heal (except path): retrying stream after credential refresh')
                    _self_healed = True
                    # Rebuild runtime variables
                    _rt = _heal_rt
                    resolved_api_key = _heal_rt.get('api_key')
                    if not resolved_provider:
                        resolved_provider = _heal_rt.get('provider')
                    if not resolved_base_url:
                        resolved_base_url = _heal_rt.get('base_url')
                    if isinstance(resolved_provider, str) and resolved_provider.startswith('custom:'):
                        _cp_key, _cp_base = resolve_custom_provider_connection(resolved_provider)
                        if not resolved_api_key and _cp_key:
                            resolved_api_key = _cp_key
                        if not resolved_base_url and _cp_base:
                            resolved_base_url = _cp_base
                    # Build a fresh agent with the new credentials
                    _heal_kwargs = dict(_agent_kwargs) if '_agent_kwargs' in dir() else {}
                    _heal_kwargs['api_key'] = resolved_api_key
                    _heal_kwargs['base_url'] = resolved_base_url
                    _heal_kwargs['model'] = resolved_model
                    _heal_kwargs['provider'] = resolved_provider
                    if 'credential_pool' in _agent_params:
                        _heal_kwargs['credential_pool'] = _heal_rt.get('credential_pool')
                    _heal_agent = _AIAgent(**_heal_kwargs)
                    with STREAMS_LOCK:
                        AGENT_INSTANCES[stream_id] = _heal_agent
                    from api.config import SESSION_AGENT_CACHE as _SAC2, SESSION_AGENT_CACHE_LOCK as _SAC2_L
                    with _SAC2_L:
                        _SAC2[session_id] = (_heal_agent, _agent_sig)
                        _SAC2.move_to_end(session_id)
                    # Retry the conversation
                    _token_sent = False
                    try:
                        _heal_result = _heal_agent.run_conversation(
                            user_message=user_message,
                            system_message=workspace_system_msg,
                            conversation_history=_sanitize_messages_for_api(_previous_context_messages),
                            task_id=session_id,
                            persist_user_message=msg_text,
                        )
                        # Retry succeeded — persist the result normally
                        if s is not None:
                            if _checkpoint_stop is not None:
                                _checkpoint_stop.set()
                            if _ckpt_thread is not None:
                                _ckpt_thread.join(timeout=15)
                            _lock_ctx = _agent_lock if _agent_lock is not None else contextlib.nullcontext()
                            with _lock_ctx:
                                if not ephemeral and not _stream_writeback_is_current(s, stream_id):
                                    logger.info(
                                        "Skipping stale stream self-heal writeback for session %s stream %s; active_stream_id=%s",
                                        getattr(s, 'session_id', session_id),
                                        stream_id,
                                        getattr(s, 'active_stream_id', None),
                                    )
                                    return
                                _result_messages = _heal_result.get('messages') or _previous_context_messages
                                _next_context_messages = _restore_reasoning_metadata(
                                    _previous_context_messages, _result_messages,
                                )
                                s.context_messages = _next_context_messages
                                s.messages = _merge_display_messages_after_agent_result(
                                    _previous_messages,
                                    _previous_context_messages,
                                    _restore_reasoning_metadata(_previous_messages, _result_messages),
                                    msg_text,
                                )
                                s.save()
                        logger.info('[webui] self-heal (except path): retry succeeded')
                        return  # skip error emission
                    except Exception as _retry_exc2:
                        logger.warning('[webui] self-heal (except path): retry failed: %s', _retry_exc2)
                        # Fall through to emit the original error
            # Self-heal didn't apply or retry failed — emit the auth error
            _exc_label, _exc_type, _exc_hint = (
                'Authentication error', 'auth_mismatch',
                'The selected model may not be supported by your configured provider. '
                'Run `hermes model` in your terminal to switch providers, then restart the WebUI.',
            )
        elif _exc_is_not_found:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        elif _exc_is_cancelled or _exc_is_interrupted:
            _exc_label, _exc_type, _exc_hint = (
                _classification['label'], _classification['type'], _classification['hint'],
            )
        else:
            _exc_label, _exc_type, _exc_hint = 'Error', 'error', ''

        _error_payload = _provider_error_payload(err_str, _exc_type, _exc_hint)
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
                if not ephemeral and not _stream_writeback_is_current(s, stream_id):
                    logger.info(
                        "Skipping stale stream error writeback for session %s stream %s; active_stream_id=%s",
                        getattr(s, 'session_id', session_id),
                        stream_id,
                        getattr(s, 'active_stream_id', None),
                    )
                    return
                _materialize_pending_user_turn_before_error(s)
                s.active_stream_id = None
                s.pending_user_message = None
                s.pending_attachments = []
                s.pending_started_at = None
                _error_message = {
                    'role': 'assistant',
                    'content': f'**{_exc_label}:** {_error_payload.get("message") or err_str}' + (f'\n\n*{_exc_hint}*' if _exc_hint else ''),
                    'timestamp': int(time.time()),
                    '_error': True,
                }
                if _error_payload.get('details'):
                    _error_message['provider_details'] = _error_payload['details']
                if _exc_type == 'cancelled':
                    _error_message['provider_details_label'] = 'Cancellation details'
                elif _exc_type == 'interrupted':
                    _error_message['provider_details_label'] = 'Interruption details'
                s.messages.append(_error_message)
                try:
                    s.save()
                except Exception:
                    pass
                if not ephemeral:
                    try:
                        append_turn_journal_event_for_stream(
                            s.session_id,
                            stream_id,
                            {
                                "event": "interrupted",
                                "created_at": time.time(),
                                "reason": _exc_type,
                            },
                        )
                    except Exception:
                        logger.debug("Failed to append interrupted turn journal event", exc_info=True)
        put('apperror', _error_payload)
    finally:
        # Stop the periodic checkpoint thread before the final recovery path.
        # The checkpoint thread also uses the per-session lock; joining it first
        # avoids contending with checkpoint writes during stale-pending repair.
        if _checkpoint_stop is not None:
            _checkpoint_stop.set()
        if _ckpt_thread is not None:
            _ckpt_thread.join(timeout=15)
        if (s is not None
                and getattr(s, 'active_stream_id', None) == stream_id
                and getattr(s, 'pending_user_message', None)):
            update_active_run(stream_id, phase="finalizing")
            _last_resort_sync_from_core(s, stream_id, _agent_lock)
        _clear_thread_env()  # TD1: always clear thread-local context
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)
            AGENT_INSTANCES.pop(stream_id, None)  # Clean up agent instance reference
            STREAM_PARTIAL_TEXT.pop(stream_id, None)  # Clean up partial text buffer (#893)
            STREAM_REASONING_TEXT.pop(stream_id, None)  # Clean up reasoning trace (#1361 §A)
            STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)  # Clean up tool calls (#1361 §B)
            STREAM_GOAL_RELATED.pop(stream_id, None)  # Clean up goal-related flag (#1932)
            unregister_active_run(stream_id)
            # NOTE: do NOT discard PENDING_GOAL_CONTINUATION here. The marker
            # is set by goal_continue (line ~3328) inside the SAME function
            # call and consumed atomically by `_start_chat_stream_for_session`
            # in routes.py (around line 6522) when the next stream starts.
            # Discarding here in the streaming worker's `finally` would
            # almost always race ahead of the frontend's SSE-receive →
            # POST /api/chat/start round-trip and erase the marker before
            # the next stream can read it, breaking the goal-continuation
            # chain. Stage-326 critical fix per Opus advisor review.

# ============================================================
# SECTION: HTTP Request Handler
# do_GET: read-only API endpoints + SSE stream + static HTML
# do_POST: mutating endpoints (session CRUD, chat, upload, approval)
# Routing is a flat if/elif chain. See ARCHITECTURE.md section 4.1.
# ============================================================


def _handle_chat_steer(handler, body: dict) -> bool:
    """Inject a /steer payload into the active agent for a session.

    Mirrors the CLI's `/steer <text>` command (cli.py:6140-6155):
      - Look up the cached AIAgent for the session (PR #1051's
        SESSION_AGENT_CACHE).
      - Verify a stream is currently active for this session.
      - Call agent.steer(text) — thread-safe, stashes text in
        _pending_steer for application at the next tool-result boundary.

    The agent's loop calls _apply_pending_steer_to_tool_results() at the
    end of every tool batch and appends the steer text to the last tool
    result's content with a marker, so the model sees the steer as part
    of the tool output on its next iteration. The user's stream is NOT
    interrupted.

    If no agent is cached, the agent is too old to support steer, or no
    stream is active, return {"accepted": False, "fallback": "<reason>"}
    so the frontend can fall back to interrupt or queue mode. The
    fallback path is the existing behaviour from PR #1062.

    Returns 200 with {"accepted": bool, "fallback": str|None,
    "stream_id": str|None}.
    """
    from api.helpers import j, bad
    from api import config as _cfg

    sid = str((body or {}).get("session_id", "") or "").strip()
    text = str((body or {}).get("text", "") or "").strip()
    if not sid:
        return bad(handler, "session_id required")
    if not text:
        return bad(handler, "text required")

    with _cfg.SESSION_AGENT_CACHE_LOCK:
        cached = _cfg.SESSION_AGENT_CACHE.get(sid)
    if not cached:
        # No active agent for this session — caller falls back to interrupt
        return j(handler, {"accepted": False, "fallback": "no_cached_agent",
                           "stream_id": None})
    agent = cached[0]
    if not hasattr(agent, "steer"):
        # Older hermes-agent that pre-dates the steer() method
        return j(handler, {"accepted": False, "fallback": "agent_lacks_steer",
                           "stream_id": None})

    # Verify the agent is currently running. Use the session's
    # active_stream_id rather than calling load_session_locked() which
    # would block on the streaming thread's lock.
    try:
        s = get_session(sid)
    except KeyError:
        return j(handler, {"accepted": False, "fallback": "session_not_found",
                           "stream_id": None})
    active_stream_id = getattr(s, "active_stream_id", None) or None
    if not active_stream_id:
        return j(handler, {"accepted": False, "fallback": "not_running",
                           "stream_id": None})
    with _cfg.STREAMS_LOCK:
        stream_alive = active_stream_id in _cfg.STREAMS
    if not stream_alive:
        # Active stream id is stale — stream has ended; caller falls back
        return j(handler, {"accepted": False, "fallback": "stream_dead",
                           "stream_id": None})

    try:
        accepted = bool(agent.steer(text))
    except Exception as exc:
        logger.debug("agent.steer() raised for session=%s: %s", sid, exc)
        return j(handler, {"accepted": False, "fallback": "steer_error",
                           "stream_id": active_stream_id})

    return j(handler, {"accepted": accepted, "fallback": None,
                       "stream_id": active_stream_id})


def cancel_stream(stream_id: str) -> bool:
    """Signal an in-flight stream to cancel. Returns True if the stream existed.

    Eagerly releases the session lock (pops STREAMS/CANCEL_FLAGS/AGENT_INSTANCES
    and clears session.active_stream_id) so new /api/chat/start requests succeed
    immediately after cancel, even if the agent thread is still blocked.

    The worker thread's finally block uses .pop(key, None), so the double-pop is
    a safe no-op. Session cleanup runs outside STREAMS_LOCK to preserve lock
    ordering (streaming thread does LOCK → STREAMS_LOCK; inverting would deadlock).
    """
    from api import config as _live_config

    # Use module-level aliases (imported from api.config at startup).
    # In production these are always the same objects as api.config.STREAMS etc.
    # The fallback below handles a hypothetical future case where api.config's
    # state dicts are replaced at runtime (e.g. a future profile-reload path).
    # No production code currently does this; the fallback is defensive only.
    streams = STREAMS
    cancel_flags = CANCEL_FLAGS
    agent_instances = AGENT_INSTANCES
    partial_texts = STREAM_PARTIAL_TEXT
    streams_lock = STREAMS_LOCK
    if stream_id not in streams and getattr(_live_config, 'STREAMS', streams) is not streams:
        streams = _live_config.STREAMS
        cancel_flags = _live_config.CANCEL_FLAGS
        agent_instances = _live_config.AGENT_INSTANCES
        partial_texts = _live_config.STREAM_PARTIAL_TEXT
        streams_lock = _live_config.STREAMS_LOCK

    with streams_lock:
        if stream_id not in streams:
            return False

        # Set WebUI layer cancel flag
        flag = cancel_flags.get(stream_id)
        if flag:
            flag.set()

        # Interrupt the AIAgent instance to stop tool execution
        agent = agent_instances.get(stream_id)
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

        # Capture the queue while the stream still exists, but do not emit the
        # terminal cancel event until the session cleanup below confirms the turn
        # is still active. Otherwise a late Stop click can race with a successful
        # worker save and show cancel in the client while persistence says done.
        q = streams.get(stream_id)
        _emit_cancel_event = True

        # ── Eager session lock release (fixes #653) ──────────────────────────
        # Pop stream state now so the 409 guard in routes.py sees the session
        # as idle and allows new /api/chat/start immediately after cancel,
        # even if the agent thread is still blocked in a C-level syscall.
        # The worker thread's finally block uses .pop(key, None) too, so a
        # double-pop here is safe (no-op).
        streams.pop(stream_id, None)
        cancel_flags.pop(stream_id, None)
        agent_instances.pop(stream_id, None)
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
        _cancel_partial_text = partial_texts.get(stream_id, '')
        # Fallback: check the live config's partial text map if we used an alias
        # and the text wasn't found in the alias (defensive, matches streams fallback above).
        if not _cancel_partial_text:
            live_partials = getattr(_live_config, 'STREAM_PARTIAL_TEXT', partial_texts)
            if live_partials is not partial_texts:
                _cancel_partial_text = live_partials.get(stream_id, '')
        # Capture reasoning trace and live tool calls (#1361 §A + §B)
        _cancel_reasoning = STREAM_REASONING_TEXT.get(stream_id, '')
        if not _cancel_reasoning:
            live_reasoning = getattr(_live_config, 'STREAM_REASONING_TEXT', STREAM_REASONING_TEXT)
            if live_reasoning is not STREAM_REASONING_TEXT:
                _cancel_reasoning = live_reasoning.get(stream_id, '')
        _cancel_tool_calls = STREAM_LIVE_TOOL_CALLS.get(stream_id, [])
        if not _cancel_tool_calls:
            live_tools = getattr(_live_config, 'STREAM_LIVE_TOOL_CALLS', STREAM_LIVE_TOOL_CALLS)
            if live_tools is not STREAM_LIVE_TOOL_CALLS:
                _cancel_tool_calls = live_tools.get(stream_id, [])

    # Session cleanup outside STREAMS_LOCK to preserve lock ordering.
    # Acquire the per-session _agent_lock too, mirroring every other session
    # writer (streaming success/error paths, periodic checkpoint, POST endpoints)
    # so the cancel-path mutation races neither the checkpoint thread nor
    # concurrent undo/retry calls.
    if _cancel_session_id:
        with _get_session_agent_lock(_cancel_session_id):
            try:
                _cs = get_session(_cancel_session_id)
                if not isinstance(getattr(_cs, 'messages', None), list):
                    _cs.messages = []
                if not _stream_writeback_is_current(_cs, stream_id):
                    # The stream has rotated to a different stream id (newer
                    # turn started, or the worker already finalized this one).
                    # Skip the cancel-marker append AND suppress the terminal
                    # cancel event so we don't contradict a possibly-already-
                    # delivered done payload (#2151 + #2154 / PR #2136).
                    logger.info(
                        "Skipping stale cancel writeback for session %s stream %s; active_stream_id=%s",
                        _cancel_session_id,
                        stream_id,
                        getattr(_cs, 'active_stream_id', None),
                    )
                    _emit_cancel_event = False
                    return True
                # ── Preserve the user's typed message before clearing pending state (#1298) ──
                # The agent's internal messages list (where the user message was appended at
                # the start of run_conversation()) may not have been merged back into
                # _cs.messages yet — cancel_stream() races with the streaming thread's final
                # _merge_display_messages_after_agent_result() call. Without this guard, the
                # user's message is lost: pending_user_message gets cleared below, and
                # _cs.messages still only contains messages from prior turns. The reporter
                # of #1298 sees their typed text vanish from chat after clicking Stop.
                #
                # Recovery rule: if pending_user_message is set AND the latest message in
                # _cs.messages isn't already a matching user turn, synthesize one. The
                # match check guards against double-append when the streaming thread DID
                # reach its merge step before cancel_stream() got the session lock.
                #
                # Wrapped in its own try/except so an unexpected _cs.messages shape (e.g.
                # in unit tests using Mock sessions) cannot escape and skip the rest of
                # the cleanup.
                try:
                    _pending_user = getattr(_cs, 'pending_user_message', None)
                    _pending_atts_raw = getattr(_cs, 'pending_attachments', None)
                    _pending_atts = list(_pending_atts_raw) if isinstance(_pending_atts_raw, (list, tuple)) else []
                    _pending_started = getattr(_cs, 'pending_started_at', None) or 0
                    _msgs_for_recovery = _cs.messages if isinstance(_cs.messages, list) else None
                    if _pending_user and _msgs_for_recovery is not None:
                        _last_user = None
                        for _m in reversed(_msgs_for_recovery):
                            if isinstance(_m, dict) and _m.get('role') == 'user':
                                _last_user = _m
                                break
                        _already_persisted = False
                        if _last_user is not None:
                            _last_content = _last_user.get('content')
                            _last_ts = _last_user.get('timestamp') or 0
                            # Only treat as already-persisted if the latest user turn
                            # was created AT OR AFTER the current turn's pending_started_at.
                            # An earlier turn whose content happens to be a substring
                            # (e.g. prior reply was "ok", user now types "ok please continue")
                            # must NOT short-circuit synthesis — that would re-introduce
                            # the data-loss bug this guard is supposed to prevent.
                            if isinstance(_last_content, str) and _last_ts >= _pending_started:
                                # Tolerate the workspace prefix the streaming thread prepends.
                                if _pending_user == _last_content or _pending_user in _last_content:
                                    _already_persisted = True
                        if not _already_persisted:
                            _user_turn: dict = {
                                'role': 'user',
                                'content': _pending_user,
                                'timestamp': int(time.time()),
                            }
                            if _pending_atts:
                                _user_turn['attachments'] = _pending_atts
                            _msgs_for_recovery.append(_user_turn)
                except Exception:
                    logger.debug(
                        "Failed to recover pending user message on cancel for %s",
                        _cancel_session_id,
                    )
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
                #
                # #1361: Also persist reasoning trace and live tool calls that were
                # accumulated in thread-local variables but invisible to the cancel path.
                # This prevents paid-token data loss when cancelling mid-reasoning or
                # mid-tool-execution.
                partial_text = _cancel_partial_text.strip() if _cancel_partial_text else ''
                _stripped = ''
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
                # Determine whether there is anything to preserve beyond just the
                # cancel marker.  Content text, reasoning trace, or tool calls all
                # count (#1361 §C — previously only _stripped was checked, so a
                # reasoning-only or tool-only stream produced NO partial message).
                _has_reasoning = bool(_cancel_reasoning and _cancel_reasoning.strip())
                _has_tools = bool(_cancel_tool_calls)
                _cancel_marker_exists = _session_has_cancel_marker(_cs)
                _cancel_marker_idx = len(_cs.messages)
                if _cancel_marker_exists:
                    for _idx in range(len(_cs.messages) - 1, -1, -1):
                        _m = _cs.messages[_idx]
                        if not isinstance(_m, dict) or _m.get('role') != 'assistant':
                            continue
                        _content = str(_m.get('content') or '').strip().lower()
                        if any(pattern in _content for pattern in _CANCEL_MARKER_PATTERNS):
                            _cancel_marker_idx = _idx
                            break
                _partial_already_present = False
                if _stripped:
                    for _m in _cs.messages:
                        # Stage-350 Opus SHOULD-FIX (#2151): only dedup
                        # against actual prior _partial markers from the
                        # same stream, with exact content match. The original
                        # substring check (`_stripped in _existing or
                        # _existing in _stripped`) was too broad — any short
                        # prior assistant reply (e.g. "OK", "Here is the
                        # answer:") becomes a substring of many later partial
                        # bodies and could silently drop the new partial,
                        # resurrecting the #893 data-loss bug on long sessions.
                        if not isinstance(_m, dict) or not _m.get('_partial'):
                            continue
                        if str(_m.get('content') or '').strip() == _stripped:
                            _partial_already_present = True
                            break
                if (_stripped or _has_reasoning or _has_tools) and not _partial_already_present:
                    _partial_msg: dict = {
                        'role': 'assistant',
                        'content': _stripped,  # may be empty for reasoning/tool-only turns
                        '_partial': True,
                        'timestamp': int(time.time()),
                    }
                    if _has_reasoning:
                        _partial_msg['reasoning'] = _cancel_reasoning.strip()
                    if _has_tools:
                        # NOTE: store under the private '_partial_tool_calls' key
                        # (NOT 'tool_calls'). The captured entries use the WebUI
                        # internal shape {name, args, done, duration, is_error}
                        # — they do NOT carry the OpenAI/Anthropic API id +
                        # function: {name, arguments} envelope. If we put them
                        # under 'tool_calls', `_sanitize_messages_for_api`
                        # (which whitelists 'tool_calls' via _API_SAFE_MSG_KEYS)
                        # would forward them to the next-turn LLM call and
                        # strict providers (OpenAI, Anthropic, Z.AI/GLM) would
                        # 400 on the malformed entries — turning a "data lost
                        # on cancel" bug into a "next message returns 400"
                        # bug, which is worse. The underscore-prefixed key is
                        # not in the whitelist, so sanitize strips it. The UI
                        # reads it via static/messages.js and renders it
                        # alongside the regular tool_calls path.
                        # (Opus pre-release review pass 2 of v0.50.251.)
                        _partial_msg['_partial_tool_calls'] = list(_cancel_tool_calls)
                    _cs.messages.insert(_cancel_marker_idx, _partial_msg)
                # Cancel marker — flagged _error=True so it is stripped from conversation
                # history on the next turn (prevents model from seeing "Task cancelled."
                # as a prior assistant reply).
                if not _cancel_marker_exists:
                    _cs.messages.append({
                        'role': 'assistant',
                        'content': _cancelled_turn_content('Task cancelled.'),
                        '_error': True,
                        'provider_details': 'Task cancelled.',
                        'provider_details_label': 'Cancellation details',
                        'timestamp': int(time.time()),
                    })
                _cs.save()
            except Exception:
                logger.debug("Failed to clear session state on cancel for %s", _cancel_session_id)

    if _emit_cancel_event and q:
        try:
            q.put_nowait(('cancel', {'message': 'Cancelled by user'}))
        except Exception:
            logger.debug("Failed to put cancel event to queue")

    return True
