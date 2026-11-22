"""Clarify prompt state for the WebUI.

This mirrors the approval flow structure, but the response is a free-form
clarification string instead of an approval decision.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Optional

from api.session_events import publish_session_list_changed


DEFAULT_TIMEOUT_SECONDS = 120
_lock = threading.Lock()
_pending: dict[str, dict] = {}
_gateway_queues: dict[str, list] = {}
_gateway_notify_cbs: dict[str, object] = {}

# ── SSE subscriber registry ─────────────────────────────────────────────
_clarify_sse_subscribers: dict[str, list[queue.Queue]] = {}


class _ClarifyEntry:
    """One pending clarify request inside a session."""

    __slots__ = ("event", "data", "result", "clarify_id")

    def __init__(self, data: dict):
        self.event = threading.Event()
        self.data = data
        self.result: Optional[str] = None
        self.clarify_id: str = data.get("clarify_id", "") or uuid.uuid4().hex[:12]


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
    if entries:
        publish_session_list_changed("attention_cleared")
    for entry in entries:
        entry.event.set()


def clear_pending(session_key: str) -> int:
    """Clear any pending clarify prompts for the session without removing the callback.

    Emits an SSE notify (``pending=None, total=0``) so any browser subscribed to
    the clarify stream takes down its visible card and unlocks the composer.
    Without this notify, the silent-timeout path in
    ``streaming.py::_clarify_callback_impl`` cleared server state but left the
    browser with a stuck card and a locked composer that 409'd on any submit
    (#4504).
    """
    with _lock:
        entries = _clear_queue_locked(session_key)
        # Notify from inside _lock for ordering consistency with submit_pending
        # and resolve_clarify*, which also publish under the same lock.
        if entries:
            _clarify_sse_notify(session_key, None, 0)
    if entries:
        publish_session_list_changed("attention_cleared")
    for entry in entries:
        entry.event.set()
    return len(entries)


def _with_timeout_metadata(data: dict) -> dict:
    item = dict(data or {})
    requested_at = float(item.get("requested_at") or time.time())
    raw_timeout = item.get("timeout_seconds")
    timeout_seconds = int(raw_timeout) if raw_timeout is not None else DEFAULT_TIMEOUT_SECONDS
    item["requested_at"] = requested_at
    item["timeout_seconds"] = timeout_seconds
    if timeout_seconds <= 0:
        # Unlimited: no expiry. The frontend renders no countdown and the card
        # waits until the user answers (or the run is cancelled).
        item["expires_at"] = 0
        return item
    item["expires_at"] = float(item.get("expires_at") or requested_at + timeout_seconds)
    return item


def _clarify_sse_notify(session_id: str, head: dict | None, total: int) -> None:
    """Push a clarify event to all SSE subscribers for a session."""
    payload = {"pending": dict(head) if head else None, "pending_count": total}
    for q in _clarify_sse_subscribers.get(session_id, ()):
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass  # drop if subscriber is slow


def sse_subscribe(session_id: str) -> queue.Queue:
    """Register a bounded Queue for SSE push to a given session."""
    q: queue.Queue = queue.Queue(maxsize=16)
    with _lock:
        _clarify_sse_subscribers.setdefault(session_id, []).append(q)
    return q


def sse_unsubscribe(session_id: str, q: queue.Queue) -> None:
    """Remove a subscriber Queue; clean up empty session entries."""
    with _lock:
        subs = _clarify_sse_subscribers.get(session_id)
        if subs:
            try:
                subs.remove(q)
            except ValueError:
                pass
            if not subs:
                _clarify_sse_subscribers.pop(session_id, None)


def submit_pending(session_key: str, data: dict) -> _ClarifyEntry:
    """Queue a pending clarify request and notify the UI callback if registered."""
    data = _with_timeout_metadata(data)
    with _lock:
        gw_queue = _gateway_queues.setdefault(session_key, [])
        # De-duplicate while unresolved: if the most recent pending clarify is
        # semantically identical, reuse it instead of stacking duplicates.
        if gw_queue:
            last = gw_queue[-1]
            if (
                str(last.data.get("question", "")) == str(data.get("question", ""))
                and list(last.data.get("choices_offered") or [])
                == list(data.get("choices_offered") or [])
                and (not data.get("clarify_id") or data["clarify_id"] == last.clarify_id)
            ):
                entry = last
                # Dedup re-uses the existing entry with its original clarify_id.
                # If a future caller pre-populates clarify_id in data, it is
                # silently discarded here — the original entry's id wins.
                # Today no caller sets clarify_id (it's generated by __init__),
                # so this is a non-issue.
                cb = _gateway_notify_cbs.get(session_key)
                # Keep _pending aligned to the oldest unresolved entry.
                _pending[session_key] = gw_queue[0].data
                if cb:
                    try:
                        cb(dict(entry.data))
                    except Exception:
                        pass
                # Safe to call while holding _lock: publish() only takes the
                # leaf _SESSION_EVENTS_LOCK and never re-acquires this lock.
                publish_session_list_changed("attention_pending")
                return entry

        entry = _ClarifyEntry(data)
        # Ensure clarify_id is present in the serialised data the frontend receives.
        entry.data["clarify_id"] = entry.clarify_id
        gw_queue.append(entry)
        _pending[session_key] = gw_queue[0].data
        cb = _gateway_notify_cbs.get(session_key)
        # Notify SSE subscribers from inside _lock for ordering guarantees.
        _clarify_sse_notify(session_key, dict(gw_queue[0].data), len(gw_queue))
    publish_session_list_changed("attention_pending")
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


def pending_count(session_key: str) -> int:
    """Return the number of unresolved clarify prompts for a session."""
    with _lock:
        queue = _gateway_queues.get(session_key) or []
        if queue:
            return len(queue)
        return 1 if _pending.get(session_key) else 0


def resolve_clarify(session_key: str, response: str, resolve_all: bool = False) -> int:
    """Resolve the oldest pending clarify request for a session."""
    with _lock:
        q = _gateway_queues.get(session_key)
        if not q:
            _pending.pop(session_key, None)
            return 0
        entries = list(q) if resolve_all else [q.pop(0)]
        if q:
            _pending[session_key] = q[0].data
            _clarify_sse_notify(session_key, dict(q[0].data), len(q))
        else:
            _clear_queue_locked(session_key)
            _clarify_sse_notify(session_key, None, 0)
    publish_session_list_changed("attention_resolved")
    count = 0
    for entry in entries:
        entry.result = response
        entry.event.set()
        count += 1
    return count


def resolve_clarify_by_id(session_key: str, clarify_id: str, response: str) -> bool:
    """Resolve a specific pending clarify request by its stable id.

    Returns True if the id was found and resolved, False otherwise.
    """
    with _lock:
        q = _gateway_queues.get(session_key)
        if not q:
            _pending.pop(session_key, None)
            return False
        for i, entry in enumerate(q):
            if entry.clarify_id == clarify_id:
                q.pop(i)
                if q:
                    _pending[session_key] = q[0].data
                    _clarify_sse_notify(session_key, dict(q[0].data), len(q))
                else:
                    _clear_queue_locked(session_key)
                    _clarify_sse_notify(session_key, None, 0)
                # Safe to call while holding _lock: publish() only takes the
                # leaf _SESSION_EVENTS_LOCK and never re-acquires this lock.
                publish_session_list_changed("attention_resolved")
                entry.result = response
                entry.event.set()
                return True
        return False


# ── Gateway Runs API clarify mirrors ─────────────────────────────────────
# A Runs API clarify prompt is owned by the Gateway run that parked it: the
# WebUI entry is only a presentation mirror. It carries the run id, the
# Gateway prompt id and the integer execution generation so
# /api/clarify/respond can answer POST /v1/runs/{run_id}/clarify, and so the
# run's clarify.settled frame or terminal end can retire exactly its cards.

GATEWAY_RUN_CLARIFY_PREFIX = "gw-run:"


def gateway_run_clarify_id(run_id: str, prompt_id: str, execution_generation: int) -> str:
    """Stable WebUI clarify id for one Gateway run prompt at one generation."""
    return f"{GATEWAY_RUN_CLARIFY_PREFIX}{run_id}:{execution_generation}:{prompt_id}"


def submit_gateway_run_pending(session_key: str, data: dict) -> tuple[dict | None, int]:
    """Mirror one Gateway run clarify prompt; a replay of a mirrored id is a no-op.

    Returns ``(head, total)`` for the session's clarify queue after the call.
    """
    clarify_id = str(data.get("clarify_id") or "")
    with _lock:
        q = _gateway_queues.get(session_key) or []
        if any(entry.clarify_id == clarify_id for entry in q):
            return dict(q[0].data), len(q)
    submit_pending(session_key, data)
    with _lock:
        q = _gateway_queues.get(session_key) or []
        return (dict(q[0].data) if q else None), len(q)


def gateway_run_pending(session_key: str, clarify_id: str) -> dict | None:
    """The Gateway-run-backed pending entry with this id, or None."""
    if not str(clarify_id or "").startswith(GATEWAY_RUN_CLARIFY_PREFIX):
        return None
    with _lock:
        for entry in _gateway_queues.get(session_key) or []:
            if entry.clarify_id == clarify_id and entry.data.get("_gateway_run_id"):
                return dict(entry.data)
    return None


def gateway_run_prompt_ids(session_key: str, run_id: str) -> set[str]:
    """Gateway prompt ids of the run's clarify mirrors still queued for this session."""
    with _lock:
        return {
            str(entry.data.get("_gateway_prompt_id") or "")
            for entry in _gateway_queues.get(session_key) or []
            if str(entry.data.get("_gateway_run_id") or "") == str(run_id or "")
        }


def settle_gateway_run_pending(session_key: str, run_id: str, prompt_id: str | None = None) -> int:
    """Retire the Gateway run's clarify mirrors (one prompt, or all of the run's).

    Notifies clarify SSE subscribers so the visible card is taken down.
    Local (in-process) clarify entries are never touched.
    """
    run_id = str(run_id or "").strip()
    if not run_id:
        return 0
    with _lock:
        q = _gateway_queues.get(session_key)
        if not q:
            return 0
        targets = [
            entry for entry in q
            if str(entry.data.get("_gateway_run_id") or "") == run_id
            and (prompt_id is None or str(entry.data.get("_gateway_prompt_id") or "") == str(prompt_id))
        ]
        if not targets:
            return 0
        remaining = [entry for entry in q if entry not in targets]
        if remaining:
            _gateway_queues[session_key] = remaining
            _pending[session_key] = remaining[0].data
            _clarify_sse_notify(session_key, dict(remaining[0].data), len(remaining))
        else:
            _clear_queue_locked(session_key)
            _clarify_sse_notify(session_key, None, 0)
    publish_session_list_changed("attention_resolved")
    for entry in targets:
        entry.event.set()
    return len(targets)
