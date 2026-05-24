"""Lightweight in-process invalidation events for session sidebar state."""

import queue
import threading

_SESSION_EVENTS_LOCK = threading.Lock()
_SESSION_EVENTS_SUBSCRIBERS: set[queue.Queue] = set()
_SESSION_EVENTS_VERSION = 0


def publish_session_list_changed(reason: str = "session_changed") -> None:
    """Notify connected browsers that the session sidebar may be stale."""
    global _SESSION_EVENTS_VERSION
    with _SESSION_EVENTS_LOCK:
        _SESSION_EVENTS_VERSION += 1
        payload = {
            "type": "sessions_changed",
            "version": _SESSION_EVENTS_VERSION,
            "reason": reason,
        }
        subscribers = list(_SESSION_EVENTS_SUBSCRIBERS)
    for q in subscribers:
        try:
            q.put_nowait(payload)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


def subscribe_session_events() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=1)
    with _SESSION_EVENTS_LOCK:
        _SESSION_EVENTS_SUBSCRIBERS.add(q)
    return q


def unsubscribe_session_events(q: queue.Queue) -> None:
    with _SESSION_EVENTS_LOCK:
        _SESSION_EVENTS_SUBSCRIBERS.discard(q)
