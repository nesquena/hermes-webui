"""Vault prompt state for the WebUI.

Mirrors the clarify flow structure (api/clarify.py), but the payload is a
machine-intended credential answer for the browser vault (``browser_vault``):
an unlock master password, a one-time code, or a save-login
``{identifier, password}`` pair.

SECURITY: the values a user types into the vault card are secrets. They are
held only in the entry's ``values`` slot, passed straight to
``agent.vault_backends.unlock`` inside the agent process, and must never be
journalled, logged, echoed into the transcript, or included in SSE payloads.
The pending card only ever carries the harmless request metadata (which
manager, which site).

Hermes Agent owns the prompt surface contract (per-thread
``set_unlock_prompt_callback`` / ``set_save_login_prompt_callback`` /
``set_code_prompt_callback``); api/streaming.py registers callbacks backed by
this module for the duration of one agent turn.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Optional

from api.session_events import publish_session_list_changed

UNLOCK_TIMEOUT_SECONDS = 120
SAVE_LOGIN_TIMEOUT_SECONDS = 180
CODE_TIMEOUT_SECONDS = 180

_lock = threading.Lock()
_pending: dict[str, list] = {}

_gateway_notify_cbs: dict[str, object] = {}

_vault_sse_subscribers: dict[str, list] = {}


def register_gateway_notify(session_key: str, cb) -> None:
    """Register a per-session callback for pushing vault prompts to the UI."""
    with _lock:
        _gateway_notify_cbs[session_key] = cb


def unregister_gateway_notify(session_key: str) -> None:
    with _lock:
        _gateway_notify_cbs.pop(session_key, None)


def _current_notify_cb_locked(session_key: str):
    return _gateway_notify_cbs.get(session_key)


class _VaultEntry:
    """One pending vault request: the values arrive via respond_vault()."""

    __slots__ = ("event", "data", "values", "prompt_id")

    def __init__(self, data: dict):
        self.event = threading.Event()
        self.data = dict(data)
        self.prompt_id = str(data.get("prompt_id", "") or uuid.uuid4().hex[:12])
        self.data["prompt_id"] = self.prompt_id
        self.values: Optional[dict] = None


def _vault_sse_notify(session_id: str, head: Optional[dict], total: int) -> None:
    payload = {"pending": dict(head) if head else None, "pending_count": total}
    for q in _vault_sse_subscribers.get(session_id, ()):
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass  # drop if subscriber is slow; the poll fallback re-syncs


def sse_subscribe(session_id: str) -> queue.Queue:
    """Register a bounded Queue for SSE push to a given session."""
    q: queue.Queue = queue.Queue(maxsize=16)
    with _lock:
        _vault_sse_subscribers.setdefault(session_id, []).append(q)
    return q


def sse_unsubscribe(session_id: str, q: queue.Queue) -> None:
    """Remove a subscriber Queue; clean up empty session entries."""
    with _lock:
        subs = _vault_sse_subscribers.get(session_id)
        if subs:
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                _vault_sse_subscribers.pop(session_id, None)


def submit_pending(session_key: str, data: dict) -> _VaultEntry:
    """Queue one pending vault request for a session and notify the UI."""
    item = dict(data or {})
    item["requested_at"] = float(item.get("requested_at") or time.time())
    kind = str(item.get("kind") or "")
    if kind == "save_login":
        timeout = float(item.get("timeout_seconds") or SAVE_LOGIN_TIMEOUT_SECONDS)
    elif kind == "code":
        timeout = float(item.get("timeout_seconds") or CODE_TIMEOUT_SECONDS)
    else:
        timeout = float(item.get("timeout_seconds") or UNLOCK_TIMEOUT_SECONDS)
    item["timeout_seconds"] = timeout
    item["expires_at"] = item["requested_at"] + timeout
    with _lock:
        entry = _VaultEntry(item)
        _pending.setdefault(session_key, []).append(entry)
        head = _pending[session_key][0].data
        _vault_sse_notify(session_key, dict(head), len(_pending[session_key]))
        cb = _current_notify_cb_locked(session_key)
    publish_session_list_changed("attention_pending")
    if cb:
        try:
            cb(dict(entry.data))
        except Exception:
            pass
    return entry


def get_pending(session_key: str) -> Optional[dict]:
    """Return the oldest pending vault request for this session, if any."""
    with _lock:
        items = _pending.get(session_key) or []
        if items:
            return dict(items[0].data)
        return None


def pending_count(session_key: str) -> int:
    with _lock:
        return len(_pending.get(session_key) or [])


def _pop_locked(session_key: str, entry: _VaultEntry) -> None:
    """Remove one entry from the pending queue; notify on the new head."""
    items = _pending.get(session_key)
    if not items:
        return
    try:
        items.remove(entry)
    except ValueError:
        return
    if items:
        _vault_sse_notify(session_key, dict(items[0].data), len(items))
    else:
        _pending.pop(session_key, None)
        _vault_sse_notify(session_key, None, 0)


def respond_vault(session_key: str, prompt_id: str, values: dict) -> bool:
    """Deliver the user's answer to one pending vault request.

    ``values`` is the secret payload (e.g. ``{"password": ...}`` or
    ``{"identifier": ..., "password": ...}``) and is stored only on the
    entry; it never lands in journal or SSE state. Empty values are valid:
    they resolve the entry with no answer (the user skipped the card).
    """
    with _lock:
        items = _pending.get(session_key)
        if not items:
            return False
        for entry in list(items):
            if entry.prompt_id != prompt_id:
                continue
            _pop_locked(session_key, entry)
            publish_session_list_changed("attention_resolved")
            entry.values = dict(values or {})
            entry.event.set()
            return True
    return False


def clear_pending(session_key: str) -> int:
    """Clear any pending vault requests for a session (run end / timeout).

    Every entry resolves with an empty answer so a cancelled wait returns
    immediately instead of hanging until its own timeout.
    """
    with _lock:
        entries = _pending.pop(session_key, [])
    _vault_sse_notify(session_key, None, 0)
    if entries:
        publish_session_list_changed("attention_cleared")
    for entry in entries:
        entry.values = {}
        entry.event.set()
    return len(entries)
