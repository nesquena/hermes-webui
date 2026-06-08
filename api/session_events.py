"""Lightweight in-process invalidation events for session sidebar state."""

import queue
import threading

_SESSION_EVENTS_LOCK = threading.Lock()
_SESSION_EVENTS_SUBSCRIBERS: set[queue.Queue] = set()
_SESSION_EVENTS_VERSION = 0


def _profile_is_root_alias(profile: str | None) -> bool:
    name = str(profile or "").strip()
    if not name:
        return False
    if name == "default":
        return True
    try:
        from api.profiles import _is_root_profile

        return bool(_is_root_profile(name))
    except Exception:
        return False


def _sessions_changed_payload(
    *,
    reason: str,
    version: int,
    profile: str | None = None,
) -> dict:
    payload = {
        "type": "sessions_changed",
        "version": version,
        "reason": reason,
    }
    normalized_profile = str(profile or "").strip()
    # Root/default aliases must stay unscoped: browser tabs cannot infer every
    # renamed-root alias, and an unscoped refresh preserves the old fail-safe.
    if normalized_profile and not _profile_is_root_alias(normalized_profile):
        payload["profile"] = normalized_profile
    return payload


def _payload_profile(payload: dict | None) -> str | None:
    value = payload.get("profile") if isinstance(payload, dict) else None
    value = str(value or "").strip()
    return value or None


def _coalesced_sessions_changed_payload(pending: dict | None, incoming: dict) -> dict:
    """Merge bounded-queue refresh events without dropping profile-relevant work.

    A maxsize=1 queue is safe only while all events are interchangeable. Once
    events can be profile-scoped, replacing profile A with profile B can make
    an A tab ignore the queued event and miss the refresh entirely. On any
    scope mismatch, fall back to an unscoped refresh-all event.
    """
    if pending is None:
        return incoming
    pending_profile = _payload_profile(pending)
    incoming_profile = _payload_profile(incoming)
    if pending_profile == incoming_profile:
        return incoming
    merged = dict(incoming)
    merged.pop("profile", None)
    return merged


def publish_session_list_changed(
    reason: str = "session_changed",
    profile: str | None = None,
) -> None:
    """Notify connected browsers that the session sidebar may be stale."""
    global _SESSION_EVENTS_VERSION
    with _SESSION_EVENTS_LOCK:
        _SESSION_EVENTS_VERSION += 1
        payload = _sessions_changed_payload(
            reason=reason,
            version=_SESSION_EVENTS_VERSION,
            profile=profile,
        )
        subscribers = list(_SESSION_EVENTS_SUBSCRIBERS)
    for q in subscribers:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pending = None
            try:
                pending = q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(_coalesced_sessions_changed_payload(pending, payload))
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
