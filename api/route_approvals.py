"""Approval SSE state and helpers.

State-extraction prelude to the routes.py split tracked in #1907.
Extracts approval state, not handlers, by design.
"""
import queue
import threading
import uuid
from contextlib import contextmanager

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
_GATEWAY_MIRROR_RETAINED = "_gateway_mirror_retained"
_GATEWAY_ENTRY_DATA_TOKEN_KEY = "_webui_mirror_token"
_GATEWAY_AGENT_IDENTITY_V1 = "_gateway_agent_identity_v1"
_gateway_relay_owners: dict[tuple[str, str], str] = {}
_yolo_transition_lock = threading.Lock()
_yolo_transitions: dict[str, dict] = {}
_gateway_yolo_handoff_guard = threading.Lock()
_gateway_yolo_handoffs: dict[str, dict] = {}


@contextmanager
def gateway_yolo_handoff(session_key: str):
    """Serialize one session's YOLO toggles with gateway approval dispatch."""
    session_key = str(session_key or "").strip()
    with _gateway_yolo_handoff_guard:
        entry = _gateway_yolo_handoffs.get(session_key)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _gateway_yolo_handoffs[session_key] = entry
        entry["users"] += 1
    lock = entry["lock"]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _gateway_yolo_handoff_guard:
            entry["users"] -= 1
            if entry["users"] == 0:
                _gateway_yolo_handoffs.pop(session_key, None)


def begin_session_yolo_transition(session_key: str) -> object | None:
    """Register a pending YOLO enable until one approval relay settles.

    Multiple tabs may relay approvals for different runs in the same session.
    Track every in-flight enable intent so one failed relay cannot undo another
    successful or explicit enable. Do not publish an unconfirmed enable to the
    shared session flag: the gateway stream may only auto-approve later prompts
    after a relay succeeds or an explicit enable wins.
    """
    session_key = str(session_key or "").strip()
    if not session_key:
        return None
    token = object()
    with _yolo_transition_lock:
        transition = _yolo_transitions.get(session_key)
        if transition is None:
            transition = {
                "was_enabled": bool(is_session_yolo_enabled(session_key)),
                "tokens": set(),
                "committed": False,
            }
            _yolo_transitions[session_key] = transition
        transition["tokens"].add(token)
    return token


def finish_session_yolo_transition(session_key: str, token: object | None, *, succeeded: bool) -> None:
    """Settle one pending YOLO enable without exposing or applying stale state."""
    session_key = str(session_key or "").strip()
    if not session_key or token is None:
        return
    with _yolo_transition_lock:
        transition = _yolo_transitions.get(session_key)
        if transition is None or token not in transition["tokens"]:
            return
        transition["tokens"].remove(token)
        if succeeded:
            transition["committed"] = True
            # The first confirmed relay commits YOLO immediately. Any remaining
            # tokens may fail later but cannot revoke this successful enable.
            enable_session_yolo(session_key)
        if transition["tokens"]:
            return
        _yolo_transitions.pop(session_key, None)
        if transition["committed"] or transition["was_enabled"]:
            enable_session_yolo(session_key)
        else:
            disable_session_yolo(session_key)


def set_session_yolo_enabled(session_key: str, enabled: bool) -> None:
    """Apply an explicit YOLO choice and supersede in-flight rollbacks."""
    session_key = str(session_key or "").strip()
    if not session_key:
        return
    with _yolo_transition_lock:
        _yolo_transitions.pop(session_key, None)
        if enabled:
            enable_session_yolo(session_key)
        else:
            disable_session_yolo(session_key)


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


def _gateway_mirror_entry_token(entry) -> str | None:
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
    return None


def _is_gateway_mirror_entry(entry: dict | None) -> bool:
    return isinstance(entry, dict) and bool(entry.get(_GATEWAY_MIRROR_FLAG))


def _normalize_pending_queue_locked(session_key: str) -> list[dict]:
    """Return the session's polling queue as a mutable list under `_lock`."""
    queue_list = _pending.setdefault(session_key, [])
    if not isinstance(queue_list, list):
        _pending[session_key] = [queue_list]
        queue_list = _pending[session_key]
    # Agent versions that predate approval identities can write directly to
    # tools.approval._pending, bypassing this module's submit_pending wrapper.
    # Assign the identity on first observation and persist it in the shared
    # queue so the browser can safely target the exact entry it rendered.
    for entry in queue_list:
        if isinstance(entry, dict) and not str(entry.get("approval_id") or "").strip():
            entry["approval_id"] = uuid.uuid4().hex
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
    live_run_id = str(live_head_data.get("run_id") or "").strip()
    # Tokenize EVERY live no-run producer, and derive `live_token` from the
    # authoritative head, whenever `_gateway_queues[session_key]` has live
    # producers. This deliberately does NOT defer to a pre-existing no-run
    # mirror: an unmatched/tokenless mirror A (which the fail-closed binding in
    # submit_gateway_pending_mirror leaves deliberately unbound) must never
    # suppress the live producer's token, or A masks the real pending approval
    # B — B never surfaces as the head and can't be actioned, while responding
    # to A resolves nothing. While any producer is live, a mirror survives only
    # if it is bound to some live producer's own token — the head's via
    # `live_token`, a non-head producer's via `live_local_tokens` (which is what
    # keeps a non-head mirror resolvable) — so unmatched and tokenless copies
    # are discarded instead of masking a real one. Tokenless-orphan retention is
    # reserved for the genuine no-producer case (#7093), which lands here with an
    # empty `live_gateway_queue` and therefore a `None` `live_token` anyway.
    live_local_tokens: set[str] = set()
    for live_entry in live_gateway_queue:
        live_data = getattr(live_entry, "data", None) or {}
        if str(live_data.get("run_id") or "").strip():
            continue
        live_entry_token = _gateway_mirror_entry_token(live_entry) or ""
        if live_entry_token:
            live_local_tokens.add(live_entry_token)
            if not str(live_data.get("approval_id") or "").strip():
                live_data["approval_id"] = f"gwlocal:{live_entry_token}"
    live_token = (
        _gateway_mirror_entry_token(live_head_entry)
        if live_head_entry and live_head_data
        else None
    )
    if live_token and live_run_id and not str(live_head_data.get("approval_id") or "").strip():
        live_head_data["approval_id"] = f"gwrun:{live_run_id}:{live_token}"
    live_approval_id = str(live_head_data.get("approval_id") or "").strip()

    rebuilt: list[dict] = []
    deferred_run_entries: list[dict] = []
    live_mirror_present = False
    for entry in queue_list:
        if not _is_gateway_mirror_entry(entry):
            rebuilt.append(entry)
            continue
        entry_run_id = str(entry.get("run_id") or "").strip()
        entry_approval_id = str(entry.get("approval_id") or "").strip()
        entry_token = str(entry.get(_GATEWAY_MIRROR_TOKEN) or "").strip()
        matches_live_head = False
        if live_token:
            if entry_token and entry_token == live_token:
                matches_live_head = True
            elif (
                live_approval_id
                and live_run_id
                and entry_approval_id == live_approval_id
                and entry_run_id == live_run_id
            ):
                matches_live_head = True

        if entry_run_id:
            if matches_live_head and not live_mirror_present:
                if entry_token != live_token:
                    entry[_GATEWAY_MIRROR_TOKEN] = live_token
                    changed = True
                rebuilt.append(entry)
                live_mirror_present = True
                continue
            if live_token:
                if entry.get(_GATEWAY_MIRROR_RETAINED):
                    rebuilt.append(entry)
                    continue
                if entry_token:
                    changed = True
                    continue
                deferred_run_entries.append(entry)
                continue
            if entry.get(_GATEWAY_MIRROR_RETAINED) or not entry_token:
                rebuilt.append(entry)
                continue
            changed = True
            continue

        # A retained mirror is one whose own producer had already vanished when
        # the user responded (the missing-producer 409 kept visible until an
        # explicit teardown). It survives ONLY while no producer is live: once
        # `_gateway_queues[session_key]` holds a real producer again, an
        # unresolvable retained mirror must not mask it, so fall through to the
        # normal matching below (which keeps it if it still matches a live
        # token and discards it otherwise).
        if entry.get(_GATEWAY_MIRROR_RETAINED) and not live_gateway_queue:
            rebuilt.append(entry)
            continue

        if matches_live_head and not live_mirror_present:
            if entry_token != live_token:
                entry[_GATEWAY_MIRROR_TOKEN] = live_token
                changed = True
            rebuilt.append(entry)
            live_mirror_present = True
            continue

        if entry_token and entry_token in live_local_tokens:
            rebuilt.append(entry)
            continue

        if not live_token:
            if entry_token:
                changed = True
                continue
            rebuilt.append(entry)
            continue

        changed = True

    if live_token and not live_mirror_present:
        mirror_entry = dict(live_head_data)
        mirror_run_id = str(mirror_entry.get("run_id") or "").strip()
        mirror_entry.setdefault(
            "approval_id",
            f"gwrun:{mirror_run_id}:{live_token}" if mirror_run_id else uuid.uuid4().hex,
        )
        mirror_entry[_GATEWAY_MIRROR_FLAG] = True
        mirror_entry[_GATEWAY_MIRROR_TOKEN] = live_token
        rebuilt.append(mirror_entry)
        live_mirror_present = True
        changed = True

    if deferred_run_entries:
        rebuilt.extend(deferred_run_entries)

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


def _gateway_pending_mirror_locked(
    session_key: str,
    approval_id: str = "",
    run_id: str = "",
    mirror_token: str = "",
) -> dict | None:
    """Return the exact live run-backed mirror under `_lock`."""
    approval_id = str(approval_id or "").strip()
    run_id = str(run_id or "").strip()
    mirror_token = str(mirror_token or "").strip()
    queue = _pending.get(session_key)
    entries = queue if isinstance(queue, list) else [queue] if queue else []
    if approval_id:
        matched_entry: dict | None = None
        for entry in entries:
            if not _is_gateway_mirror_entry(entry):
                continue
            if entry.get("approval_id") != approval_id:
                continue
            entry_run_id = str(entry.get("run_id") or "").strip()
            if not entry_run_id:
                if not run_id:
                    return None
                continue
            if run_id and entry_run_id != run_id:
                continue
            if mirror_token and str(entry.get(_GATEWAY_MIRROR_TOKEN) or "").strip() != mirror_token:
                continue
            if run_id:
                return entry
            if matched_entry is not None:
                return None
            matched_entry = entry
        return matched_entry
    for entry in entries:
        if not _is_gateway_mirror_entry(entry) or not str(entry.get("run_id") or "").strip():
            continue
        if mirror_token and str(entry.get(_GATEWAY_MIRROR_TOKEN) or "").strip() != mirror_token:
            continue
        if run_id:
            if entry.get("run_id") == run_id:
                return entry
            continue
        # With no caller-supplied identity, the queue order is authoritative:
        # return the current run-backed projection and let its embedded
        # `(approval_id, run_id)` identify the exact relay owner.
        return entry
    return None


def gateway_pending_mirror(
    session_key: str,
    approval_id: str = "",
    run_id: str = "",
    mirror_token: str = "",
) -> dict | None:
    """Return an exact live run-backed mirror for this session."""
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)
        entry = _gateway_pending_mirror_locked(session_key, approval_id, run_id, mirror_token)
        return dict(entry) if entry else None


def gateway_pending_mirrors(session_key: str) -> list[dict]:
    """Return every currently parked run-backed mirror in queue order."""
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)
        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        return [
            dict(entry)
            for entry in entries
            if _is_gateway_mirror_entry(entry)
            and str(entry.get("run_id") or "").strip()
        ]


def claim_gateway_approval_relay_owner(session_key: str, run_id: str, approval_id: str) -> bool:
    """Claim the single-flight relay owner for one `(session, run)` pair."""
    session_key = str(session_key or "").strip()
    run_id = str(run_id or "").strip()
    approval_id = str(approval_id or "").strip()
    if not session_key or not run_id:
        return False
    with _lock:
        key = (session_key, run_id)
        if key in _gateway_relay_owners:
            return False
        _gateway_relay_owners[key] = approval_id
        return True


def release_gateway_approval_relay_owner(session_key: str, run_id: str, approval_id: str = "") -> None:
    """Release the single-flight relay owner for one `(session, run)` pair."""
    session_key = str(session_key or "").strip()
    run_id = str(run_id or "").strip()
    approval_id = str(approval_id or "").strip()
    if not session_key or not run_id:
        return
    with _lock:
        key = (session_key, run_id)
        current = str(_gateway_relay_owners.get(key) or "").strip()
        if approval_id and current and current != approval_id:
            return
        _gateway_relay_owners.pop(key, None)


def retire_gateway_pending_mirror(
    session_key: str,
    approval_id: str = "",
    run_id: str = "",
    mirror_token: str = "",
) -> bool:
    """Retire one approval, or every mirror for a terminal run."""
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)
        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        normalized_run_id = str(run_id or "").strip()
        gateway_queue = _gateway_queues.get(session_key) or []
        retained_gateway_queue = gateway_queue
        gateway_queue_changed = False
        if approval_id:
            match = _gateway_pending_mirror_locked(
                session_key,
                approval_id,
                run_id,
                mirror_token,
            )
            if match is None and not normalized_run_id:
                match = next((entry for entry in entries if _is_gateway_mirror_entry(entry)
                              and not str(entry.get("run_id") or "").strip()
                              and str(entry.get("approval_id") or "").strip() == approval_id), None)
            retired = [match] if match else []
        else:
            retired = [
                entry for entry in entries
                if _is_gateway_mirror_entry(entry)
                and str(entry.get("run_id") or "").strip() == normalized_run_id
            ] if normalized_run_id else [
                entry for entry in entries
                if _is_gateway_mirror_entry(entry)
                and not str(entry.get("run_id") or "").strip()
            ]
            if normalized_run_id:
                retained_gateway_queue = []
                for entry in gateway_queue:
                    data = getattr(entry, "data", None) or {}
                    if str(data.get("run_id") or "").strip() == normalized_run_id:
                        gateway_queue_changed = True
                        continue
                    retained_gateway_queue.append(entry)
        if not retired and not gateway_queue_changed:
            head, total, changed = reconcile_gateway_pending_mirror_locked(session_key)
            _approval_sse_notify_locked(session_key, head, total)
            if changed:
                publish_session_list_changed("attention_resolved")
            return changed
        for match in retired:
            entries.remove(match)
        if normalized_run_id and not approval_id:
            if retained_gateway_queue:
                _gateway_queues[session_key] = retained_gateway_queue
            else:
                _gateway_queues.pop(session_key, None)
        if entries:
            _pending[session_key] = entries
        else:
            _pending.pop(session_key, None)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_resolved")
    return True


def _gateway_mirrored_pending_run_id(session_key: str, approval_id: str) -> str | None:
    """Compatibility wrapper for exact run-backed lookup."""
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return None
    with _lock:
        entry = _gateway_pending_mirror_locked(session_key, approval_id=approval_id)
        if entry:
            return str(entry.get("run_id") or "").strip() or None
    return None


def submit_gateway_pending_mirror(session_key: str, approval: dict) -> tuple[dict | None, int]:
    """Mirror the live gateway head into WebUI polling state under a typed tag.

    Every mirrored entry describes one pending approval to the UI. Run-backed
    mirrors carry ``run_id`` (remote gateway runs) and are bound to the parked
    ``_ApprovalEntry`` via ``approval_id``. No-run mirrors, which represent an
    in-process (legacy) approval parked in ``_gateway_queues``, have no
    ``run_id`` and instead bind back to the live entry through
    ``_GATEWAY_MIRROR_TOKEN``: ``_resolve_approval_legacy()`` matches
    ``pending[_GATEWAY_MIRROR_TOKEN]`` against the live entry's
    ``_webui_mirror_token`` before it will call ``resolve_gateway_pending_local()``
    to unblock the agent thread. A no-run mirror's token is stamped from its
    OWN live producer — resolved via the mirror's ``request_id``/
    ``approval_id`` — and from nothing else. If no live producer's identity
    matches, the mirror is left tokenless (fail closed): guessing ownership
    from "first unclaimed token" or the queue head would let approving THIS
    (possibly stale/foreign) mirror resolve a DIFFERENT live producer than
    the one the user actually saw, which is an approval-integrity violation,
    not a convenience. A tokenless mirror is only wired up automatically when
    there is truly no live producer at all (#7093); ``reconcile_gateway_
    pending_mirror_locked`` binds a fresh, correctly-bound mirror to the
    authoritative live head on its own. Without a token, THIS specific click
    returns ``ok:true`` and the card clears without unblocking any producer —
    the correct producer's own card reappears on the next reconcile.
    """
    with _lock:
        run_id = str(approval.get("run_id") or "").strip()
        approval_id = str(approval.get("approval_id") or "").strip()
        live_gateway_queue = _gateway_queues.get(session_key) or []
        exact_local_entry = next(
            (
                entry for entry in live_gateway_queue
                if getattr(entry, "data", None) is approval
            ),
            None,
        ) if not run_id else None
        if exact_local_entry is None and not run_id and approval_id:
            exact_local_entry = next(
                (
                    entry for entry in live_gateway_queue
                    if str(((getattr(entry, "data", None) or {}).get("approval_id") or "")).strip() == approval_id
                ),
                None,
            )
        if exact_local_entry is None and not run_id:
            # Fall back to matching on the core's per-approval `request_id`.
            # The gateway core notifies WebUI with a COPY of the entry payload
            # (`notify_cb(dict(entry.data))`), so the identity match above
            # (`entry.data is approval`) never holds for a real gateway head,
            # and a local `_ApprovalEntry` carries a `request_id` but no
            # `approval_id`, so the approval_id fallback misses too. The
            # `request_id` is stamped once on the source entry
            # (`_ApprovalEntry.__init__` -> `data.setdefault("request_id", ...)`)
            # and preserved through the copy, so it uniquely reunites the
            # notified copy with its queued entry. Without this, the mirror is
            # created with no token, reconcile keeps the orphan, and
            # `_session_has_pending_approval` stays True after the entry is
            # dropped (the stale-approval-card dead-end, #4948 local variant).
            request_id = str(approval.get("request_id") or "").strip()
            if request_id:
                exact_local_entry = next(
                    (
                        entry for entry in live_gateway_queue
                        if str(((getattr(entry, "data", None) or {}).get("request_id") or "")).strip() == request_id
                    ),
                    None,
                )
        if exact_local_entry is not None:
            mirror_entries = _normalize_pending_queue_locked(session_key)
            entries_to_mirror = [live_gateway_queue[0]] if live_gateway_queue else []
            if exact_local_entry not in entries_to_mirror:
                entries_to_mirror.append(exact_local_entry)
            for entry in entries_to_mirror:
                local_data = entry.data
                token = _gateway_mirror_entry_token(entry)
                entry_approval_id = str(local_data.get("approval_id") or "").strip()
                if entry is exact_local_entry:
                    entry_approval_id = approval_id or entry_approval_id or f"gwlocal:{token}"
                    approval_id = entry_approval_id
                    approval["approval_id"] = entry_approval_id
                elif not entry_approval_id:
                    entry_approval_id = f"gwlocal:{token}"
                local_data["approval_id"] = entry_approval_id
                if not any(
                    _is_gateway_mirror_entry(mirror)
                    and str(mirror.get(_GATEWAY_MIRROR_TOKEN) or "") == token
                    for mirror in mirror_entries
                ):
                    mirror_entry = dict(local_data)
                    mirror_entry["approval_id"] = entry_approval_id
                    mirror_entry[_GATEWAY_MIRROR_FLAG] = True
                    mirror_entry[_GATEWAY_MIRROR_TOKEN] = token
                    mirror_entries.append(mirror_entry)
        if run_id:
            live_head_entry = live_gateway_queue[0] if live_gateway_queue else None
            live_head_data = getattr(live_head_entry, "data", None) or {}
            live_head_run_id = str(live_head_data.get("run_id") or "").strip()
            live_head_approval_id = str(live_head_data.get("approval_id") or "").strip()
            live_token = (
                _gateway_mirror_entry_token(live_head_entry)
                if live_head_entry and live_head_data
                else None
            )
            if (
                live_token
                and live_head_run_id == run_id
                and (
                    not approval_id
                    or not live_head_approval_id
                    or live_head_approval_id == approval_id
                )
            ):
                if approval_id:
                    live_head_data["approval_id"] = approval_id
                else:
                    approval_id = live_head_approval_id
                    if not approval_id:
                        approval_id = f"gwrun:{run_id}:{live_token}"
                        live_head_data["approval_id"] = approval_id
                    approval["approval_id"] = approval_id
            else:
                if not approval_id:
                    approval_id = f"gwrun:{run_id}:{uuid.uuid4().hex}"
                    approval["approval_id"] = approval_id
                mirror_entry = dict(approval)
                mirror_entry["run_id"] = run_id
                mirror_entry["approval_id"] = approval_id
                mirror_entry[_GATEWAY_MIRROR_FLAG] = True
                mirror_entry[_GATEWAY_MIRROR_TOKEN] = uuid.uuid4().hex
                mirror_entry[_GATEWAY_MIRROR_RETAINED] = True
                if not _gateway_pending_mirror_locked(session_key, approval_id=approval_id, run_id=run_id):
                    _normalize_pending_queue_locked(session_key).append(mirror_entry)
        elif not exact_local_entry:
            if not approval_id:
                approval_id = uuid.uuid4().hex
                approval["approval_id"] = approval_id
            queue = _pending.get(session_key)
            entries = queue if isinstance(queue, list) else [queue] if queue else []
            no_run_mirror = next(
                (
                    entry for entry in reversed(entries)
                    if _is_gateway_mirror_entry(entry)
                    and not str(entry.get("run_id") or "").strip()
                    and str(entry.get("approval_id") or "").strip() == approval_id
                ),
                None,
            )
            if no_run_mirror:
                approval["approval_id"] = str(no_run_mirror.get("approval_id") or approval_id).strip()
            elif not _gateway_pending_mirror_locked(session_key, approval_id=approval_id):
                # Stamp the mirror token from the mirror's OWN live producer so
                # the first respond can link this mirror to the right
                # _ApprovalEntry in _gateway_queues. Without it,
                # _resolve_approval_legacy cannot match the no-run mirror to its
                # gateway entry (both token fields are empty) and the agent
                # thread is never unblocked on the first click (#6008 legacy).
                # We MUST NOT blindly take the live head: for a non-head mirror
                # (multiple parked producers, #7093 exact-producer isolation)
                # the head belongs to a sibling, and stamping its token would
                # bind the mirror to the wrong entry. Only an explicit
                # request_id/approval_id match may bind a token.
                #
                # FAIL CLOSED when no producer's identity matches: never infer
                # ownership from "first unclaimed token" or the queue head. A
                # stale/foreign approval (mismatched request_id, or no
                # identity at all) that borrows another live producer's token
                # would let approving THIS mirror resolve a DIFFERENT producer
                # than the one the user actually saw — an approval-integrity
                # violation, not a convenience (found in review of 818fd2fd).
                # A tokenless orphan is only legitimate when there is no live
                # producer at all (#7093); reconcile_gateway_pending_mirror_locked
                # binds a real mirror to the authoritative live head on its own.
                live_queue_for_mirror = _gateway_queues.get(session_key) or []
                request_id_for_mirror = str(approval.get("request_id") or "").strip()
                mirror_producer = None
                if request_id_for_mirror or approval_id:
                    for cand in live_queue_for_mirror:
                        cand_data = getattr(cand, "data", None) or {}
                        if (request_id_for_mirror and
                                str(cand_data.get("request_id") or "").strip() == request_id_for_mirror):
                            mirror_producer = cand
                            break
                        if (approval_id and not request_id_for_mirror and
                                str(cand_data.get("approval_id") or "").strip() == approval_id):
                            mirror_producer = cand
                            break
                mirror_token = (
                    _gateway_mirror_entry_token(mirror_producer)
                    if mirror_producer is not None else None
                )
                mirror_entry = dict(approval)
                mirror_entry["approval_id"] = approval_id
                mirror_entry[_GATEWAY_MIRROR_FLAG] = True
                if mirror_token:
                    mirror_entry[_GATEWAY_MIRROR_TOKEN] = mirror_token
                _normalize_pending_queue_locked(session_key).append(mirror_entry)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_pending")
    return (dict(head) if head else None), total


def resolve_gateway_pending_local(
    session_key: str, approval_id: str, choice: str, reason: str | None = None
) -> tuple[int, dict | None, int]:
    """Resolve the exact parked local entry bound to an approval mirror."""
    target = None
    with _lock:
        approval_id = str(approval_id or "").strip()
        gateway_queue = _gateway_queues.get(session_key) or []
        for index, entry in enumerate(gateway_queue):
            data = getattr(entry, "data", None) or {}
            if str(data.get("approval_id") or "").strip() == approval_id:
                target = gateway_queue.pop(index)
                break
        if gateway_queue:
            _gateway_queues[session_key] = gateway_queue
        else:
            _gateway_queues.pop(session_key, None)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    if target is None:
        return 0, head, total
    target.result = choice
    if reason:
        target.reason = reason
    target.event.set()
    publish_session_list_changed("attention_resolved")
    return 1, head, total


def resolve_gateway_pending_local_no_run_mirror(
    session_key: str, approval_id: str, choice: str, reason: str | None = None
) -> tuple[bool, int, dict | None, int]:
    """Resolve an exact no-run mirror only while its parked producer still exists."""
    target = None
    with _lock:
        approval_id = str(approval_id or "").strip()
        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        matched_mirror = next(
            (
                entry for entry in entries
                if _is_gateway_mirror_entry(entry)
                and not str(entry.get("run_id") or "").strip()
                and str(entry.get("approval_id") or "").strip() == approval_id
            ),
            None,
        )
        if matched_mirror is None:
            return False, 0, entries[0] if entries else None, len(entries)

        gateway_queue = _gateway_queues.get(session_key) or []
        for index, entry in enumerate(gateway_queue):
            data = getattr(entry, "data", None) or {}
            if str(data.get("approval_id") or "").strip() == approval_id:
                target = gateway_queue.pop(index)
                break
        if target is None:
            matched_mirror[_GATEWAY_MIRROR_RETAINED] = True
            return True, 0, entries[0] if entries else None, len(entries)

        if gateway_queue:
            _gateway_queues[session_key] = gateway_queue
        else:
            _gateway_queues.pop(session_key, None)
        entries.remove(matched_mirror)
        if entries:
            _pending[session_key] = entries
        else:
            _pending.pop(session_key, None)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    target.result = choice
    if reason:
        target.reason = reason
    target.event.set()
    publish_session_list_changed("attention_resolved")
    return True, 1, head, total


def resolve_gateway_pending_local_all(
    session_key: str,
    choice: str,
    reason: str | None = None,
) -> tuple[int, dict | None, int]:
    """Resolve every parked local/no-run approval without touching remote runs."""
    targets = []
    removed_pending = False
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)

        gateway_queue = _gateway_queues.get(session_key) or []
        retained_gateway_queue = []
        for entry in gateway_queue:
            data = getattr(entry, "data", None) or {}
            if str(data.get("run_id") or "").strip():
                retained_gateway_queue.append(entry)
            else:
                targets.append(entry)
        if retained_gateway_queue:
            _gateway_queues[session_key] = retained_gateway_queue
        else:
            _gateway_queues.pop(session_key, None)

        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        retained_pending = [
            entry
            for entry in entries
            if _is_gateway_mirror_entry(entry)
            and str(entry.get("run_id") or "").strip()
        ]
        removed_pending = len(retained_pending) != len(entries)
        if retained_pending:
            _pending[session_key] = retained_pending
        else:
            _pending.pop(session_key, None)

        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)

    for entry in targets:
        entry.result = choice
        if reason:
            entry.reason = reason
        entry.event.set()
    if targets or removed_pending:
        publish_session_list_changed("attention_resolved")
    return len(targets), head, total


def settle_gateway_pending_local_notification(
    session_key: str,
    approval: dict,
) -> tuple[bool, dict | None, int]:
    """Auto-resolve or publish one local approval at the YOLO handoff boundary.

    The Agent adds its blocking entry before invoking WebUI's notify callback.
    Serialize that callback with session YOLO commit/disable so a waiter arriving
    after a drain snapshot cannot be parked behind an already-committed enable.
    Run-backed approvals stay on the Runs API path and are never resolved here.
    """
    with gateway_yolo_handoff(session_key):
        run_id = str((approval or {}).get("run_id") or "").strip()
        if not run_id and is_session_yolo_enabled(session_key):
            _resolved, head, total = resolve_gateway_pending_local_all(
                session_key,
                "once",
            )
            return True, head, total
        head, total = submit_gateway_pending_mirror(session_key, approval)
        return False, head, total


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
