"""Approval SSE state and helpers.

State-extraction prelude to the routes.py split tracked in #1907.
Extracts approval state, not handlers, by design.
"""
import queue
import threading
import uuid

from api.session_events import publish_session_list_changed

# Approval system (optional -- graceful fallback if agent not available)
try:
    from tools.approval import (
        submit_pending as _submit_pending_raw,
        approve_session,
        approve_permanent,
        save_permanent_allowlist,
        is_approved,
        _pending,
        _lock,
        _permanent_approved,
        _gateway_queues,
        resolve_gateway_approval,
        enable_session_yolo,
        disable_session_yolo,
        is_session_yolo_enabled,
    )
except ImportError:
    _submit_pending_raw = lambda *a, **k: None
    approve_session = lambda *a, **k: None
    approve_permanent = lambda *a, **k: None
    save_permanent_allowlist = lambda *a, **k: None
    is_approved = lambda *a, **k: True
    resolve_gateway_approval = lambda *a, **k: 0
    enable_session_yolo = lambda *a, **k: None
    disable_session_yolo = lambda *a, **k: None
    is_session_yolo_enabled = lambda *a, **k: False
    _pending = {}
    _lock = threading.Lock()
    _permanent_approved = set()
    _gateway_queues = {}


# ── Approval SSE subscribers (long-connection push) ──────────────────────────
_approval_sse_subscribers: dict[str, list[queue.Queue]] = {}
_GATEWAY_MIRROR_FLAG = "_gateway_mirror"
_GATEWAY_MIRROR_TOKEN = "_gateway_mirror_token"
_GATEWAY_ENTRY_DATA_TOKEN_KEY = "_webui_mirror_token"


def _approval_sse_subscribe(session_id: str) -> queue.Queue:
    """Register an SSE subscriber for approval events on a given session."""
    q = queue.Queue(maxsize=16)
    with _lock:
        _approval_sse_subscribers.setdefault(session_id, []).append(q)
    return q


def _approval_sse_unsubscribe(session_id: str, q: queue.Queue) -> None:
    """Remove an SSE subscriber."""
    with _lock:
        subs = _approval_sse_subscribers.get(session_id)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                _approval_sse_subscribers.pop(session_id, None)


def _approval_sse_notify_locked(session_id: str, head: dict | None, total: int) -> None:
    """Push an approval event to all SSE subscribers for a session.

    CALLER MUST HOLD `_lock`. Snapshots the subscriber list under the held
    lock and then calls `q.put_nowait()` on each (which is itself thread-safe).

    `head` is the approval entry currently at the head of the queue (the one
    the UI should display) — NOT the just-appended entry. With multiple
    parallel approvals (#527), the just-appended entry is at the TAIL, but
    `/api/approval/pending` always returns the HEAD, so SSE must match.

    `total` is the total number of pending approvals.

    Pass `head=None` and `total=0` when the queue has just been emptied (e.g.
    `_handle_approval_respond` popped the last entry) so the client knows to
    hide its approval card.
    """
    payload = {"pending": dict(head) if head else None, "pending_count": total}
    subs = _approval_sse_subscribers.get(session_id, ())
    for q in subs:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass  # drop if subscriber is slow (bounded queue prevents memory leak)


def _approval_sse_notify(session_id: str, head: dict | None, total: int) -> None:
    """Convenience wrapper that takes `_lock` itself.

    Use only from contexts that don't already hold `_lock`. Production call
    sites (submit_pending, _handle_approval_respond) MUST hold the lock and
    call `_approval_sse_notify_locked` directly to avoid a notify-ordering
    race where a later append's notify can fire before an earlier append's
    notify (resulting in stale `pending_count`).
    """
    with _lock:
        _approval_sse_notify_locked(session_id, head, total)


def _gateway_mirror_entry_token(entry) -> str:
    """Return a stable token for the current process lifetime of a gateway head.

    Stamps a token key into the entry's `.data` dict so
    slotted objects like `_ApprovalEntry` work without attribute mutation
    and the token survives CPython `id()` reuse after GC.
    """
    data = getattr(entry, "data", None)
    if isinstance(data, dict):
        token = data.get(_GATEWAY_ENTRY_DATA_TOKEN_KEY)
        if not token:
            token = uuid.uuid4().hex
            data[_GATEWAY_ENTRY_DATA_TOKEN_KEY] = token
        return token
    return uuid.uuid4().hex


def _is_gateway_mirror_entry(entry: dict | None) -> bool:
    return isinstance(entry, dict) and bool(entry.get(_GATEWAY_MIRROR_FLAG))


def _normalize_pending_queue_locked(session_key: str) -> list[dict]:
    """Return the session's polling queue as a mutable list under `_lock`."""
    queue_list = _pending.setdefault(session_key, [])
    if not isinstance(queue_list, list):
        _pending[session_key] = [queue_list]
        queue_list = _pending[session_key]
    return queue_list


def reconcile_gateway_pending_mirror_locked(session_key: str) -> tuple[dict | None, int, bool]:
    """Purge stale gateway mirrors and ensure at most one live head mirror exists.

    CALLER MUST HOLD `_lock`.
    """
    changed = False
    queue_list = list(_normalize_pending_queue_locked(session_key))
    live_gateway_queue = _gateway_queues.get(session_key) or []

    live_head_entry = live_gateway_queue[0] if live_gateway_queue else None
    live_head_data = getattr(live_head_entry, "data", None) or {}
    live_token = _gateway_mirror_entry_token(live_head_entry) if live_head_entry and live_head_data else None

    rebuilt: list[dict] = []
    live_mirror_present = False
    for entry in queue_list:
        if not _is_gateway_mirror_entry(entry):
            rebuilt.append(entry)
            continue
        if live_token and entry.get(_GATEWAY_MIRROR_TOKEN) == live_token and not live_mirror_present:
            rebuilt.append(entry)
            live_mirror_present = True
            continue
        changed = True

    if live_token and not live_mirror_present:
        mirror_entry = dict(live_head_data)
        mirror_entry.setdefault("approval_id", uuid.uuid4().hex)
        mirror_entry[_GATEWAY_MIRROR_FLAG] = True
        mirror_entry[_GATEWAY_MIRROR_TOKEN] = live_token
        rebuilt.append(mirror_entry)
        live_mirror_present = True
        changed = True

    if rebuilt:
        if rebuilt != queue_list:
            _pending[session_key] = rebuilt
            changed = True
    else:
        if session_key in _pending:
            _pending.pop(session_key, None)
            changed = True

    head = rebuilt[0] if rebuilt else None
    total = len(rebuilt)
    return head, total, changed


def _gateway_mirrored_pending_run_id(session_key: str, approval_id: str) -> str | None:
    """Return the mirrored gateway approval run_id for a matching pending card.

    Reconciles the mirror first so a live gateway head still survives a lost
    `active_stream_id` pointer.
    """
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return None
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)
        queue = _pending.get(session_key)
        if isinstance(queue, list):
            entries = queue
        elif queue:
            entries = [queue]
        else:
            return None
        for entry in entries:
            if isinstance(entry, dict) and entry.get("approval_id") == approval_id and entry.get(_GATEWAY_MIRROR_FLAG):
                run_id = str(entry.get("run_id") or "").strip()
                return run_id or None
    return None


def submit_gateway_pending_mirror(session_key: str, approval: dict) -> None:
    """Mirror the live gateway head into WebUI polling state under a typed tag."""
    del approval  # mirror from the live gateway head under `_lock`, not from callback input
    with _lock:
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_pending")


def submit_pending(session_key: str, approval: dict) -> None:
    """Append a pending approval to the per-session queue.

    Wraps the agent's submit_pending to:
    - Add a stable approval_id (uuid4 hex) so the respond endpoint can target
      a specific entry even when multiple approvals are queued simultaneously.
    - Change the storage from a single overwriting dict value to a list, so
      parallel tool calls each get their own approval slot (fixes #527).
    - Notify any connected SSE subscribers immediately.
    """
    entry = dict(approval)
    entry.setdefault("approval_id", uuid.uuid4().hex)
    with _lock:
        queue_list = _normalize_pending_queue_locked(session_key)
        queue_list.append(entry)
        total = len(queue_list)
        head = queue_list[0]  # /api/approval/pending always returns head
        # Push to SSE subscribers from inside _lock so two parallel
        # submit_pending calls can't deliver out-of-order (T2's later
        # notify arriving before T1's earlier notify with a stale count).
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_pending")
    # NOTE: We do NOT call _submit_pending_raw here — that function overwrites
    # _pending[session_key] with a single dict, which would undo the list we just
    # built. The gateway blocking path uses _gateway_queues (a separate mechanism
    # managed by check_all_command_guards / register_gateway_notify), which is
    # unaffected by _pending. The _pending dict is only used for UI polling.
