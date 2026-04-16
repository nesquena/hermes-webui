"""Clarify prompt state for the WebUI.

This mirrors the approval flow structure, but the response is a free-form
clarification string instead of an approval decision.
"""

from __future__ import annotations

import threading
from typing import Optional


_lock = threading.Lock()
_pending: dict[str, dict] = {}
_gateway_queues: dict[str, list] = {}
_gateway_notify_cbs: dict[str, object] = {}


class _ClarifyEntry:
    """One pending clarify request inside a session."""

    __slots__ = ("event", "data", "result")

    def __init__(self, data: dict):
        self.event = threading.Event()
        self.data = data
        self.result: Optional[str] = None


def register_gateway_notify(session_key: str, cb) -> None:
    """Register a per-session callback for sending clarify requests to the UI."""
    with _lock:
        _gateway_notify_cbs[session_key] = cb


def _clear_queue_locked(session_key: str) -> list[_ClarifyEntry]:
    entries = _gateway_queues.pop(session_key, [])
    _pending.pop(session_key, None)
    return entries


def unregister_gateway_notify(session_key: str) -> None:
    """Unregister the per-session callback and unblock any waiting clarify prompt."""
    with _lock:
        _gateway_notify_cbs.pop(session_key, None)
        entries = _clear_queue_locked(session_key)
    for entry in entries:
        entry.event.set()


def clear_pending(session_key: str) -> int:
    """Clear any pending clarify prompts for the session without removing the callback."""
    with _lock:
        entries = _clear_queue_locked(session_key)
    for entry in entries:
        entry.event.set()
    return len(entries)


def submit_pending(session_key: str, data: dict) -> _ClarifyEntry:
    """Queue a pending clarify request and notify the UI callback if registered."""
    with _lock:
        queue = _gateway_queues.setdefault(session_key, [])
        # De-duplicate while unresolved: if the most recent pending clarify is
        # semantically identical, reuse it instead of stacking duplicates.
        if queue:
            last = queue[-1]
            if (
                str(last.data.get("question", "")) == str(data.get("question", ""))
                and list(last.data.get("choices_offered") or [])
                == list(data.get("choices_offered") or [])
            ):
                entry = last
                cb = _gateway_notify_cbs.get(session_key)
                # Keep _pending aligned to the oldest unresolved entry.
                _pending[session_key] = queue[0].data
                if cb:
                    try:
                        cb(dict(entry.data))
                    except Exception:
                        pass
                return entry

        entry = _ClarifyEntry(data)
        queue.append(entry)
        _pending[session_key] = queue[0].data
        cb = _gateway_notify_cbs.get(session_key)
    if cb:
        try:
            cb(data)
        except Exception:
            pass
    return entry


def get_pending(session_key: str) -> dict | None:
    """Return the oldest pending clarify request for this session, if any."""
    with _lock:
        queue = _gateway_queues.get(session_key) or []
        if queue:
            return dict(queue[0].data)
        pending = _pending.get(session_key)
        return dict(pending) if pending else None


def has_pending(session_key: str) -> bool:
    with _lock:
        return bool(_gateway_queues.get(session_key))


def resolve_clarify(session_key: str, response: str, resolve_all: bool = False) -> int:
    """Resolve the oldest pending clarify request for a session."""
    with _lock:
        queue = _gateway_queues.get(session_key)
        if not queue:
            _pending.pop(session_key, None)
            return 0
        entries = list(queue) if resolve_all else [queue.pop(0)]
        if queue:
            _pending[session_key] = queue[0].data
        else:
            _clear_queue_locked(session_key)
    count = 0
    for entry in entries:
        entry.result = response
        entry.event.set()
        count += 1
    return count
