"""Drain thread for terminal(notify_on_complete=true) agent wakeup.

The hermes-agent ``tools.process_registry.ProcessRegistry`` exposes a thread-safe
``completion_queue`` (a ``queue.Queue``) that any background process pushes onto
when it exits or matches a ``watch_patterns`` rule. In the CLI and in the
gateway adapter this queue is drained by the host's main loop; in WebUI the
queue was never read, so the agent never woke up from a ``notify_on_complete``
finish. This module restores that behavior.

The drain thread:
    1. blocks on ``completion_queue.get()`` in a worker thread,
    2. looks up the WebUI session_id from ``PROCESS_SESSION_INDEX`` (keyed on
       the per-process ``session_key`` env var captured at spawn time),
    3. formats a synthetic ``[IMPORTANT: ...]`` wakeup prompt, identical in
       intent to ``cli._format_process_notification`` and
       ``gateway.run._format_gateway_process_notification`` so the agent sees
       the same payload regardless of host,
    4. emits a canonical ``bg_task_complete`` SSE event (plus a temporary
       ``process_complete`` alias for the migration window) on the active
       stream(s) for that session (DEMOTED to pure live-view — an open tab
       streams the turn live), records a server-side marker in
       ``PENDING_BG_TASK_COMPLETIONS``,
       and — Option Z PIVOT — starts the agent wakeup turn **directly
       server-side** when the session is idle (``_start_server_side_wakeup_turn``
       → ``routes.start_session_turn``). This needs NO browser round-trip, so
       the closed-tab case works exactly like CLI / Telegram / gateway
       self-wake. When a turn is already active the wakeup is NOT started here;
       the ``PENDING_BG_TASK_COMPLETIONS`` marker is left for PR #2279's
       next-turn drain (``api/streaming._drain_webui_process_notifications``).

The marker is *not* required for delivery — it's a telemetry-style flag the
turn handler can read to know "this stream is a process_complete wakeup, not a
human-typed prompt". It also lets the PR #2279 next-turn drain deliver the
wakeup when a turn was active at completion time; the marker drains harmlessly
on the next turn for the session.

Watch-pattern events share the same queue but produce a different SSE payload;
this module routes them to the same listener so the frontend's single
``process_complete`` handler can re-POST either flavor verbatim.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import Any, Optional

from api.process_event_utils import (
    ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
    claim_async_delegation_delivery,
    complete_async_delegation_delivery,
    completion_delivery_id,
    release_async_delegation_delivery,
    requeue_async_delegation_event,
    requeue_completion_event,
    schedule_async_delegation_claim_retry,
)

logger = logging.getLogger(__name__)


def _resolve_session_lineage(session_id: str):
    from api.session_lineage import resolve_session_lineage

    return resolve_session_lineage(session_id)

_DRAIN_THREAD: Optional[threading.Thread] = None
_DRAIN_STOP = threading.Event()
_PROCESS_RECOVERY_DONE = False
_PROCESS_CHECKPOINT_RECOVERED = False
_PROCESS_RECOVERY_LOCK = threading.Lock()

_REAPER_THREAD: Optional[threading.Thread] = None
_REAPER_STOP = threading.Event()
_REAPER_INTERVAL_SECS = 60.0

# Completion ids held here are active, pre-incorporation attempts. They
# intentionally diverge from the registry's durable ``_completion_consumed``
# set only until the attempt is incorporated or handed back to the deferred
# owner. ``BG_TASK_COMPLETE_EVENTS_SEEN`` spans both active and deferred local
# ownership; the reaper therefore checks both this set and the deferred map.
_PREINCORPORATION_COMPLETION_IDS: set[str] = set()

# Serializes the check-then-start of the module's daemon threads
# (``start_drain_thread`` / ``start_session_channel_reaper``). Without it two
# concurrent callers can both observe ``is_alive() == False`` and each spawn a
# thread; the loser's thread is never referenced by the module global and runs
# forever, un-joinable. A dedicated lock (not the purpose-bound
# ``SESSION_CHANNELS_LOCK`` / ``_EMIT_COALESCE_LOCK``) keeps this narrow.
_THREAD_LIFECYCLE_LOCK = threading.Lock()

# T3: per-session coalesce gate for the public bg_task_complete SSE emit.
# The server-side wakeup path remains immediate; only the browser-observation
# frame is throttled so a burst of background task completions does not flood an
# open tab. The first emit for a session fires immediately, then any further
# emits inside the 1s window are payload-replaced and flushed after 1s of quiet.
_EMIT_COALESCE_WINDOW_SECS = 1.0
_EMIT_COALESCE_LOCK = threading.Lock()
_LAST_EMIT_TS: dict[str, float] = {}
_PENDING_EMIT_PAYLOADS: dict[str, dict] = {}
_PENDING_EMIT_TIMERS: dict[str, threading.Timer] = {}


# ── Persistent per-session SSE channel (Option X) ──────────────────────────
# SESSION_CHANNELS maps WebUI session_id -> SessionChannel. Each channel owns
# zero or more queue.Queue subscribers (one per active EventSource tab) and
# is collected by ``_reaper_loop`` after the last subscriber drops + a grace
# period, or after the session has been idle for SESSION_CHANNEL_IDLE_TTL_SECS.
#
# Why a sibling registry to STREAMS:
#   STREAMS is keyed on stream_id (one per agent turn) and is torn down by
#   /api/chat/stream's `finally` when the turn ends. process_complete events
#   from background processes that exit BETWEEN turns therefore have no live
#   STREAMS channel to ride. SESSION_CHANNELS is keyed on session_id, lives
#   across turns, and gives the frontend a stable subscription that survives
#   stream_end / cancel / reconnect.
SESSION_CHANNELS: dict[str, "SessionChannel"] = {}
SESSION_CHANNELS_LOCK = threading.Lock()


class SessionChannel:
    """A long-lived multi-subscriber SSE channel for one WebUI session.

    Subscribers are ``queue.Queue`` instances owned by the SSE route
    handler — one per active EventSource (tab). ``emit`` broadcasts to every
    live subscriber; subscribers whose buffer is full silently drop the
    event (the tab will reconnect on disconnect and the SSE-level disconnect
    detection will tear it down).

    Lifecycle:
      - Created on demand by ``get_or_create_session_channel`` when the first
        tab subscribes.
      - ``subscribe`` / ``unsubscribe`` are refcount-style: zero subscribers
        does NOT immediately collect the channel; the reaper waits a 60s
        grace so a quick navigation away/back doesn't churn the registry.
      - The reaper collects the channel when subscribers stay empty past the
        grace period, OR when subscribers are empty AND ``created_at`` is
        older than SESSION_CHANNEL_IDLE_TTL_SECS (zombie cap — applies only
        when nobody is subscribed; a live subscriber keeps the channel even
        past the idle TTL).
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        now = time.time()
        self.created_at = now
        self.last_event_at = now
        self.last_subscriber_drop_at: float | None = None

    def subscribe(self, maxsize: int = 16) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
            # Cancel any pending subscribers-empty grace timer.
            self.last_subscriber_drop_at = None
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            if not self._subscribers:
                self.last_subscriber_drop_at = time.time()

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def emit(self, event: str, data: Any) -> int:
        """Broadcast (event, data) to all live subscribers. Returns delivered count."""
        delivered = 0
        with self._lock:
            subs = list(self._subscribers)
            self.last_event_at = time.time()
        for q in subs:
            try:
                q.put_nowait((event, data))
                delivered += 1
            except queue.Full:
                # Slow tab: drop this event for that tab. SSE-level disconnect
                # detection will eventually tear the connection down and the
                # browser will reconnect, replaying the live stream from
                # whatever fires next. process_complete is intrinsically
                # idempotent (frontend dedupes by ``(session_id, event_id)``
                # using a small ring-buffer in static/messages.js — see the
                # bg_task_complete consumer-side dedupe introduced in PR #2971).
                logger.debug("SessionChannel emit: subscriber buffer full, dropping")
            except Exception:
                logger.debug("SessionChannel emit failed", exc_info=True)
        return delivered

    def reaper_should_collect(self, now: float) -> bool:
        """True when the reaper should remove this channel.

        Two collection conditions (per Option X spec):
          1. Subscribers empty AND last_subscriber_drop_at is older than
             SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS (normal teardown).
          2. created_at older than SESSION_CHANNEL_IDLE_TTL_SECS AND
             subscribers empty (zombie cap — survived too long).
        """
        from api import config as _cfg

        with self._lock:
            sub_count = len(self._subscribers)
            drop_at = self.last_subscriber_drop_at
            created_at = self.created_at

        if sub_count > 0:
            # Live subscriber — never collect, even past idle TTL (a tab is
            # genuinely listening). The browser will close on its own.
            return False
        # No subscribers — check grace period.
        grace = float(getattr(_cfg, "SESSION_CHANNEL_SUBSCRIBER_GRACE_SECS", 60))
        if drop_at is not None and (now - drop_at) >= grace:
            return True
        # Hard cap on lifetime (even if subscribers oscillated): if created
        # long ago AND nobody's subscribed right now, sweep.
        ttl = float(getattr(_cfg, "SESSION_CHANNEL_IDLE_TTL_SECS", 14400))
        if (now - created_at) >= ttl:
            return True
        return False


def get_or_create_session_channel(session_id: str) -> SessionChannel:
    """Return the channel for ``session_id``, creating it on first access."""
    with SESSION_CHANNELS_LOCK:
        ch = SESSION_CHANNELS.get(session_id)
        if ch is None:
            ch = SessionChannel(session_id)
            SESSION_CHANNELS[session_id] = ch
        return ch


def get_session_channel(session_id: str) -> Optional[SessionChannel]:
    """Return an existing channel or None — does NOT auto-create."""
    with SESSION_CHANNELS_LOCK:
        return SESSION_CHANNELS.get(session_id)


def subscribe_to_session_channel(
    session_id: str, maxsize: int = 16
) -> tuple["SessionChannel", "queue.Queue"]:
    """Atomically get-or-create the channel for ``session_id`` AND register a
    subscriber on it, both under ``SESSION_CHANNELS_LOCK``.

    Closes a TOCTOU race flagged on PR #2971: callers that did
    ``ch = get_or_create_session_channel(sid); q = ch.subscribe()`` released
    ``SESSION_CHANNELS_LOCK`` between the two steps. The reaper acquires that
    same lock and evaluates+collects channels entirely within it
    (``_reaper_loop``), so a previously-idle channel with 0 subscribers past
    the grace/TTL window could be collected in the gap — leaving the SSE
    handler subscribed to a channel no longer in ``SESSION_CHANNELS``. Future
    ``bg_task_complete`` emits resolve the session via ``get_session_channel``
    and reach a different (or absent) channel, so the orphaned subscriber's
    queue never fills: keepalives keep flowing, ``onerror`` never fires, no
    auto-reconnect, and only a manual refresh recovers.

    Holding ``SESSION_CHANNELS_LOCK`` across both the get-or-create and the
    ``subscribe`` makes the two steps indivisible w.r.t. the reaper: the reaper
    cannot run its critical section concurrently, and the next time it does the
    channel already has ``sub_count >= 1`` so ``reaper_should_collect`` refuses
    to collect it.

    Lock order is ``SESSION_CHANNELS_LOCK`` → ``SessionChannel._lock`` (taken
    inside ``subscribe``), identical to the order the reaper uses
    (``SESSION_CHANNELS_LOCK`` → ``reaper_should_collect`` → ``_lock``), so no
    new lock-ordering hazard is introduced. ``emit`` only takes ``_lock``.

    Returns ``(channel, queue)``. The caller still owns the subscriber slot and
    MUST ``channel.unsubscribe(queue)`` on every exit path.
    """
    with SESSION_CHANNELS_LOCK:
        ch = SESSION_CHANNELS.get(session_id)
        if ch is None:
            ch = SessionChannel(session_id)
            SESSION_CHANNELS[session_id] = ch
        q = ch.subscribe(maxsize=maxsize)
        return ch, q


def active_stream_id_for_session(session_id: str) -> Optional[str]:
    """Return the stream_id of the live run for *session_id*, or None.

    Used by the per-session SSE handler's on-subscribe recovery: the
    ``server_turn_started`` fan-out in ``routes.start_session_turn`` is a
    fire-and-forget broadcast with NO replay buffer (SessionChannel.emit
    drops to whoever is subscribed *at that instant*). A tab whose
    ``/api/session/stream`` EventSource is momentarily absent at the emit
    instant — a transient SSE drop, a reverse-proxy idle-timeout, or browser
    connection-pool starvation — misses the frame permanently and the
    server-initiated wakeup turn never renders live (the user must hard-
    refresh). The server-side wakeup itself still ran and persisted; only the
    live-view was lost. The handler replays a synthetic ``server_turn_started``
    to a freshly-subscribed tab using this lookup so the open tab self-heals.

    Keys on ACTIVE_RUNS (worker-lifecycle registry) — the same source
    ``_session_has_active_turn`` / ``_emit_to_session_streams`` already trust
    to map a stream back to its owning session. Returns the first matching
    stream_id (a session has at most one live run; cancel/reconnect can
    briefly hold two — either is a valid attach target, the frontend dedupes
    by stream_id).
    """
    from api import config as _cfg

    try:
        with _cfg.ACTIVE_RUNS_LOCK:
            for _stream_id, meta in (_cfg.ACTIVE_RUNS or {}).items():
                if isinstance(meta, dict) and meta.get("session_id") == session_id:
                    return str(_stream_id)
    except Exception:
        logger.debug(
            "active_stream_id_for_session lookup failed for %s",
            session_id,
            exc_info=True,
        )
    return None


def persisted_message_count_for_session(session_id: str) -> Optional[int]:
    """Cheap, metadata-only persisted ``message_count`` for *session_id*, or None.

    Companion to ``active_stream_id_for_session`` for the per-session SSE
    on-subscribe self-heal. ``active_stream_id_for_session`` recovers a turn
    that is live RIGHT NOW (replay ``server_turn_started``). But a
    SERVER-initiated turn (self-wake / cron / restart hook) can start AND
    finish entirely inside an SSE gap: the fire-and-forget
    ``server_turn_started`` reached no subscriber AND the run already cleared
    from ``ACTIVE_RUNS`` by the time the tab reconnects, so the replay finds
    nothing (returns None) and the tab's transcript stays stale until a hard
    refresh — the reported visible-tab defect. To detect that case the handler
    compares the freshly-(re)subscribed tab's last-known count against this
    persisted count; a server that is AHEAD means a turn landed during the gap.

    Reads via ``metadata_only=True`` so it never parses the full transcript
    (this runs on every per-session SSE (re)connect). The persisted count is
    written by ``Session.save`` as ``meta['message_count'] = len(messages)`` —
    the SAME basis the frontend's ``S.session.message_count`` is built from —
    so the comparison is apples-to-apples. Returns None when the count is
    unknown (legacy sidecars without a persisted count); the caller treats
    None as "cannot tell, do nothing", never as a trigger.
    """
    try:
        from api.models import get_session

        s = get_session(session_id, metadata_only=True)
        count = getattr(s, "_metadata_message_count", None)
        if count is None:
            msgs = getattr(s, "messages", None)
            count = len(msgs) if isinstance(msgs, list) and msgs else None
        return int(count) if count is not None else None
    except Exception:
        logger.debug(
            "persisted_message_count_for_session lookup failed for %s",
            session_id,
            exc_info=True,
        )
        return None


def should_emit_session_updated(
    subscriber_known_count: Optional[int],
    persisted_count: Optional[int],
) -> bool:
    """Gate for the per-session SSE "finished during the gap" self-heal emit.

    Single source of truth shared by the SSE handler and its tests so the two
    cannot drift (the handler MUST call this, not inline the comparison).
    Emit a ``session-updated`` frame ONLY when:
      * the (re)subscribing tab reported a last-known count (``?known_count``),
        AND
      * the persisted server-side count is known, AND
      * the server is STRICTLY ahead (a turn landed during the gap).
    A missing known count (tab didn't report), an unknown persisted count
    (legacy sidecar), or an equal/behind count all return False — never a
    spurious reload.
    """
    if subscriber_known_count is None:
        return False
    if persisted_count is None:
        return False
    return persisted_count > subscriber_known_count


def _reaper_loop() -> None:
    logger.info("SessionChannel reaper thread started")
    while not _REAPER_STOP.is_set():
        try:
            now = time.time()
            collected: list[str] = []
            with SESSION_CHANNELS_LOCK:
                for sid, ch in list(SESSION_CHANNELS.items()):
                    if ch.reaper_should_collect(now):
                        SESSION_CHANNELS.pop(sid, None)
                        collected.append(sid)
            if collected:
                # Prune the per-session coalesce timestamp map for any collected
                # session so _LAST_EMIT_TS does not grow one permanent entry per
                # session that ever fired a bg task (greptile flag). The pending
                # payload/timer maps self-clean when their timers fire, but
                # _LAST_EMIT_TS is only ever written, never deleted — sweep it
                # here alongside the channel it belongs to. A session that fires
                # again after collection simply re-seeds its entry (first emit in
                # the new window fires immediately, which is correct).
                with _EMIT_COALESCE_LOCK:
                    for sid in collected:
                        _LAST_EMIT_TS.pop(sid, None)
                logger.debug("SessionChannel reaper collected: %s", collected)
            # Sweep the per-session completion-dedup map by DELIVERY lifecycle,
            # not channel collection. ``BG_TASK_COMPLETE_EVENTS_SEEN`` gains a
            # ``session_id -> set[process_id]`` entry the first time a bg task
            # completes for a session — in ``_process_one``, whether or not any
            # tab/SSE channel ever existed — and is otherwise never deleted, so it
            # grows unbounded. Coupling the prune to channel collection (an
            # earlier version of this fix) missed the dominant case: a headless
            # completion (task fires, tab closed or never opened) has no channel
            # to collect. Instead, once a completion has been drained (its
            # ``session_id`` removed from ``PENDING_BG_TASK_COMPLETIONS``), the
            # short ``_move_to_finished`` dedup window is closed and the entry is
            # pure leak — so sweep every delivered (not-pending) session here,
            # every tick, except while an id is an active attempt or has one
            # deferred owner. The registry's own per-``process_id``
            # ``_completion_consumed`` gate remains the primary idempotency
            # backstop, so sweeping a delivered session's set can never resurrect
            # an already-delivered completion (even in the tiny window between
            # this module's ``SEEN.add`` and ``PENDING.add`` in ``_process_one``).
            from api import config as _cfg

            with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
                deferred_ids = {
                    sid: {
                        str((entry or {}).get("process_id") or "")
                        for entry in entries or []
                    }
                    for sid, entries in _cfg.DEFERRED_PROCESS_WAKEUPS.items()
                }
            with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
                for sid in [
                    s
                    for s, process_ids in _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.items()
                    if s not in _cfg.PENDING_BG_TASK_COMPLETIONS
                    and not (
                        set(process_ids) & _PREINCORPORATION_COMPLETION_IDS
                    )
                    and not (set(process_ids) & deferred_ids.get(s, set()))
                ]:
                    _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(sid, None)
        except Exception:
            logger.warning("SessionChannel reaper iteration failed", exc_info=True)
        # Wait but wake up promptly on stop.
        if _REAPER_STOP.wait(_REAPER_INTERVAL_SECS):
            break


def start_session_channel_reaper() -> bool:
    """Start the SessionChannel reaper thread. Idempotent; returns True on first start."""
    global _REAPER_THREAD
    with _THREAD_LIFECYCLE_LOCK:
        if _REAPER_THREAD is not None and _REAPER_THREAD.is_alive():
            return False
        _REAPER_STOP.clear()
        _REAPER_THREAD = threading.Thread(
            target=_reaper_loop,
            name="hermes-webui-session-channel-reaper",
            daemon=True,
        )
        _REAPER_THREAD.start()
        return True


def stop_session_channel_reaper(timeout: float = 2.0) -> None:
    _REAPER_STOP.set()
    th = _REAPER_THREAD
    if th is not None and th.is_alive():
        th.join(timeout=timeout)


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…(truncated)"


def format_wakeup_prompt(evt: object) -> str | None:
    """Build the synthetic [IMPORTANT: …] message the agent will see.

    Mirrors ``cli._format_process_notification`` so wakeup payloads look the
    same in CLI and WebUI sessions.
    """
    if not isinstance(evt, dict) or not evt:
        return None

    evt_type = evt.get("type", "completion")
    sid = str(evt.get("session_id") or "").strip()
    cmd = str(evt.get("command") or "").strip()
    # The current server-side wakeup drain drops global watch-overflow events
    # before this formatter because they intentionally carry no session_key.
    # Keep this branch defensive so any future routable overflow summary is not
    # mis-rendered as a fake process completion.
    if evt_type in {"watch_overflow_tripped", "watch_overflow_released"}:
        msg = str(evt.get("message") or "").strip()
        return f"[IMPORTANT: {msg}]" if msg else None
    if evt_type == "watch_disabled":
        msg = str(evt.get("message") or "").strip()
        return f"[IMPORTANT: {msg}]" if msg else None
    if evt_type == "watch_match":
        pat = evt.get("pattern", "?")
        out = _truncate(evt.get("output", ""), 4000)
        sup = evt.get("suppressed", 0)
        body = (
            f"[IMPORTANT: Background process {sid} matched watch pattern \"{pat}\".\n"
            f"Command: {cmd}\n"
            f"Matched output:\n{out}"
        )
        if sup:
            body += f"\n({sup} earlier matches were suppressed by rate limit)"
        return body + "]"
    if evt_type == "async_delegation":
        # A background ``delegate_task`` completion. The agent-side formatter
        # renders these; delegate to it so the subagent result re-enters the
        # parent conversation instead of being silently dropped (#4912).
        try:
            from tools.process_registry import (
                format_process_notification as _agent_fmt,
            )
            result = _agent_fmt(evt)
            if result:
                return result
        except Exception:
            logger.debug(
                "agent-side format_process_notification fallback failed for "
                "evt_type=%s",
                evt_type,
                exc_info=True,
            )
        return None
    if evt_type != "completion":
        return None

    if not (sid or cmd or "exit_code" in evt or evt.get("output")):
        return None

    # Default: completion event
    exit_code = evt.get("exit_code", "?")
    out = _truncate(evt.get("output", ""), 4000)
    return (
        f"[IMPORTANT: Background process {sid} completed (exit_code={exit_code}).\n"
        f"Command: {cmd}\n"
        f"Output:\n{out}]"
    )


def _build_payload(evt: dict, session_id: str) -> dict:
    """Build the SSE data payload.

    Shape per maintainer decision on PR #2242 (R2 §Q1):
        ``{session_id, task_id, completed_at, summary?, event_id}``
    Minimal by design — consumers re-fetch task detail by ``task_id`` if
    they need ``command`` / ``exit_code`` / ``stdout_preview`` etc.

    - ``task_id``: the background process id (registry uuid). Stable across
      the process's lifetime; was previously surfaced as ``process_id``.
    - ``completed_at``: float wall-clock seconds; was ``emitted_at``.
    - ``summary``: optional one-liner derived from the completion event when
      available (e.g. ``[IMPORTANT: …]`` synthetic body's first line); omitted
      otherwise to honour "keep the payload minimal".
    - ``event_id``: server-generated uuid hex — per-emit, so a re-emit for the
      same ``task_id`` (shouldn't happen today, but the spec is forward-looking)
      still produces a distinct id. The cross-A/B dedupe stays keyed on
      ``task_id`` (it prevents double-emit at the source, which is the right
      layer); the WebUI consumer-side ring buffer (PR (b)) keys on
      ``(session_id, event_id)`` to dedupe across reconnects.
    """
    # ProcessRegistry completion events use the field name ``session_id`` for
    # the process id. Async delegation completions carry ``delegation_id``
    # instead; expose either stable delivery id as payload ``task_id``.
    process_id = completion_delivery_id(evt)
    payload: dict[str, Any] = {
        "session_id": str(session_id),
        "task_id": process_id,
        "completed_at": time.time(),
        "event_id": uuid.uuid4().hex,
    }
    # Best-effort optional summary: the first non-empty line of the synthetic
    # wakeup body, trimmed. Omitted entirely when nothing useful is available.
    try:
        wakeup_body = format_wakeup_prompt(evt)
        if wakeup_body:
            # Strip leading "[IMPORTANT: " marker noise — take the first
            # informative line, cap length.
            first_line = next(
                (
                    ln.strip().lstrip("[").rstrip("]").strip()
                    for ln in wakeup_body.splitlines()
                    if ln.strip()
                ),
                "",
            )
            if first_line:
                payload["summary"] = _truncate(first_line, 200)
    except Exception:
        # Summary is optional; never let its derivation block the emit.
        logger.debug("summary derivation failed", exc_info=True)
    return payload


def _emit_to_session_streams(session_id: str, event: str, data: dict) -> int:
    """Push (event, data) to every active SSE channel for *session_id*.

    Streams in WebUI are keyed by ``stream_id`` (not session_id) — a single
    session can have at most one active stream at a time, but cancel/reconnect
    flows can briefly hold two. We push to every channel whose tracked
    ``session_id`` matches; the channel implementation broadcasts to all live
    subscribers and buffers when offline.
    """
    from api import config as _cfg

    emitted = 0
    # Snapshot ACTIVE_RUNS under its lock before STREAMS_LOCK so owner_sid
    # lookups are consistent without nesting the two independent locks.
    if hasattr(_cfg, "ACTIVE_RUNS") and hasattr(_cfg, "ACTIVE_RUNS_LOCK"):
        with _cfg.ACTIVE_RUNS_LOCK:
            active_runs_snapshot: dict = dict(_cfg.ACTIVE_RUNS)
    elif hasattr(_cfg, "ACTIVE_RUNS"):
        active_runs_snapshot = dict(_cfg.ACTIVE_RUNS)
    else:
        active_runs_snapshot = {}
    with _cfg.STREAMS_LOCK:
        items = list(_cfg.STREAMS.items())
    for stream_id, channel in items:
        meta = active_runs_snapshot.get(stream_id)
        owner_sid = (meta or {}).get("session_id") if isinstance(meta, dict) else None
        # Copilot review #3: skip non-matching and owner-unknown STREAMS
        # channels. Cross-turn delivery is handled by SESSION_CHANNELS below.
        if owner_sid != session_id:
            continue
        try:
            channel.put_nowait((event, data))
            emitted += 1
        except Exception:
            logger.debug("process_complete emit failed for stream %s", stream_id, exc_info=True)
    # Option X: also emit to the persistent per-session SSE channel. This is
    # the path that survives between turns (when STREAMS is torn down). We
    # keep the STREAMS emit above as defense-in-depth — if a turn IS active
    # the frontend dedupes by process_id, so a double-delivery is harmless.
    ch = get_session_channel(session_id)
    if ch is not None:
        try:
            delivered = ch.emit(event, data)
            emitted += delivered
        except Exception:
            logger.debug("SessionChannel emit failed for session %s", session_id, exc_info=True)
    return emitted


def _emit_bg_task_complete_events_now(session_id: str, payload: dict) -> int:
    """Emit the canonical bg_task_complete event and temporary legacy alias."""
    # T1 emit rename: the canonical event name is now ``bg_task_complete``
    # (per maintainer decision on PR #2242). Until PR (b) lands the new
    # consumer-side listener, we ALSO emit the legacy ``process_complete``
    # name so any in-flight WebUI build (subscribed to the old listener)
    # still receives the wakeup. PR (b) will:
    #   1. Add `bg_task_complete` listeners on the WebUI side.
    #   2. Remove this dual-emit shim (drop the legacy alias).
    # Both emits carry the SAME trimmed payload + the SAME event_id, so a
    # consumer that ever sees both can dedupe by ``event_id``.
    #
    # Greptile P2: hand each emit its OWN shallow copy of the payload. The
    # same dict object would otherwise be referenced by every subscriber queue
    # across STREAMS and SESSION_CHANNELS for BOTH event names; a downstream
    # consumer that mutates it in place would silently corrupt all other
    # concurrent consumers' views. Shallow copies are sufficient — the payload
    # is a flat trimmed dict of scalars.
    return (
        _emit_to_session_streams(session_id, "bg_task_complete", dict(payload))
        + _emit_to_session_streams(session_id, "process_complete", dict(payload))
    )


def _flush_coalesced_bg_task_complete(session_id: str) -> None:
    """Timer callback: flush the latest pending payload for one session."""
    payload: dict | None = None
    with _EMIT_COALESCE_LOCK:
        payload = _PENDING_EMIT_PAYLOADS.pop(session_id, None)
        _PENDING_EMIT_TIMERS.pop(session_id, None)
        if payload is not None:
            _LAST_EMIT_TS[session_id] = time.time()
    if payload is None:
        return
    try:
        _emit_bg_task_complete_events_now(session_id, payload)
    except Exception:
        logger.debug(
            "coalesced bg_task_complete flush failed for session %s",
            session_id,
            exc_info=True,
        )


def _emit_bg_task_complete_events_coalesced(session_id: str, payload: dict) -> int:
    """Per-session 1s coalesce gate around the bg_task_complete dual emit.

    The first payload for a session emits immediately. If another payload for
    that session arrives within ``_EMIT_COALESCE_WINDOW_SECS`` of the last emit,
    replace the pending payload and reset the quiet timer. The deferred flush
    therefore uses the latest payload from a burst.
    """
    if not session_id:
        return 0
    should_emit_now = False
    now = time.time()
    with _EMIT_COALESCE_LOCK:
        last = _LAST_EMIT_TS.get(session_id)
        has_pending = session_id in _PENDING_EMIT_TIMERS
        if last is None or (now - last) >= _EMIT_COALESCE_WINDOW_SECS:
            _LAST_EMIT_TS[session_id] = now
            should_emit_now = True
            if has_pending:
                _PENDING_EMIT_PAYLOADS.pop(session_id, None)
                old_timer = _PENDING_EMIT_TIMERS.pop(session_id, None)
                if old_timer is not None:
                    try:
                        old_timer.cancel()
                    except Exception:
                        logger.debug(
                            "coalesced bg_task_complete timer cancel failed for session %s",
                            session_id,
                            exc_info=True,
                        )
        else:
            _PENDING_EMIT_PAYLOADS[session_id] = payload

        if not should_emit_now:
            old_timer = _PENDING_EMIT_TIMERS.get(session_id)
            if old_timer is not None:
                try:
                    old_timer.cancel()
                except Exception:
                    logger.debug(
                        "coalesced bg_task_complete timer cancel failed for session %s",
                        session_id,
                        exc_info=True,
                    )
            timer = threading.Timer(
                _EMIT_COALESCE_WINDOW_SECS,
                _flush_coalesced_bg_task_complete,
                args=(session_id,),
            )
            timer.daemon = True
            _PENDING_EMIT_TIMERS[session_id] = timer
            timer.start()

    if should_emit_now:
        return _emit_bg_task_complete_events_now(session_id, payload)
    return 0


# ── Coupling contract: agent ProcessRegistry cross-A/B dedupe key ──────────
# This WebUI drain (B) and the merged upstream PR #2279 next-turn drain (A)
# dedupe a process_id against a SINGLE shared key inside the agent's
# ``tools.process_registry.ProcessRegistry``:
#
#   * READ  side: the PUBLIC ``is_completion_consumed(process_id)`` method
#     (used in ``_process_one`` above) — stable public API.
#   * WRITE side: there is NO public ``mark_completion_consumed`` upstream, so
#     B must reach into the registry's private ``_completion_consumed`` set
#     (guarded by its private ``_lock``) to set the shared marker A reads.
#
# That private WRITE coupling is what Copilot review #2242 comment #4 flagged.
# The long-term fix is an upstream PUBLIC ``mark_completion_consumed`` — see
# the test ``test_registry_completion_consumed_contract`` which fails CI LOUD
# the moment ``_completion_consumed`` / ``_lock`` / ``is_completion_consumed``
# is renamed or retyped upstream, instead of a future rename silently
# reintroducing the double-wakeup bug. ``_mark_registry_completion_consumed``
# narrows the exception handling so a rename is logged at ERROR (visible in
# errors.log + monitoring) rather than swallowed by a broad ``except`` at
# DEBUG. ImportError stays best-effort (the registry is legitimately absent in
# non-agent unit-test contexts; this module's own locked
# ``BG_TASK_COMPLETE_EVENTS_SEEN`` gate still dedupes B's own duplicates there).
_REGISTRY_CONSUMED_CONTRACT = ("_lock", "_completion_consumed", "is_completion_consumed")


def _mark_registry_completion_consumed(process_id: str) -> None:
    """Set the shared cross-A/B dedupe marker on the agent ProcessRegistry.

    Couples to ``ProcessRegistry`` privates (``_lock`` /
    ``_completion_consumed``) because no public ``mark_completion_consumed``
    exists upstream (the read side uses the public
    ``is_completion_consumed``). A future upstream rename must FAIL LOUD, not
    silently reintroduce the double-wakeup: an ``AttributeError`` / ``TypeError``
    from the private access is logged at ERROR with a contract-violation
    message (and ``test_registry_completion_consumed_contract`` breaks CI at
    test time). Only ``ImportError`` is treated as best-effort/expected (the
    registry is absent in pure unit-test contexts).
    """
    try:
        from tools.process_registry import process_registry as _pr
    except ImportError:
        # Agent registry not importable (e.g. isolated unit test) — B's own
        # locked BG_TASK_COMPLETE_EVENTS_SEEN gate still prevents this module's
        # duplicates; cross-A/B dedupe is moot when A isn't running either.
        logger.debug(
            "tools.process_registry not importable; skipping shared "
            "completion-consumed marker (best-effort, expected off-agent)",
            exc_info=True,
        )
        return
    try:
        lock = _pr._lock
        consumed = _pr._completion_consumed
    except AttributeError as exc:
        logger.error(
            "ProcessRegistry coupling contract VIOLATED: expected private "
            "attrs %s for cross-A/B wakeup dedupe are missing — an upstream "
            "rename has broken the shared marker; process_complete wakeups may "
            "now double-fire. A public mark_completion_consumed() upstream is "
            "the durable fix (Copilot #2242 review #4).",
            _REGISTRY_CONSUMED_CONTRACT,
            exc_info=True,
        )
        raise RuntimeError("ProcessRegistry completion-consumed contract is unavailable") from exc
    try:
        with lock:
            consumed.add(process_id)
    except (AttributeError, TypeError) as exc:
        logger.error(
            "ProcessRegistry coupling contract VIOLATED: _lock/"
            "_completion_consumed changed shape (not a Lock / not a set) — "
            "cross-A/B wakeup dedupe is broken; wakeups may double-fire. "
            "Upstream public mark_completion_consumed() is the durable fix "
            "(Copilot #2242 review #4).",
            exc_info=True,
        )
        raise RuntimeError("ProcessRegistry completion-consumed contract changed shape") from exc
    if not _pr.is_completion_consumed(process_id):
        raise RuntimeError("ProcessRegistry completion ACK read-back failed")
    from api import config as _cfg

    with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        _PREINCORPORATION_COMPLETION_IDS.discard(process_id)


# ── xsession wakeup misroute defense-in-depth (Option 3) ───────────────────
# Option 1 (api/streaming._set_turn_session_identity) is the ROOT fix: it binds
# the per-turn session identity to a contextvar so a notify_on_complete spawn
# can no longer capture a concurrent turn's process-global env. Option 3 is an
# INDEPENDENT completion-time safety net at the wakeup-routing layer: even if
# some future regression reintroduces a capture race, a positively-detected
# mismatch must not wake the wrong session.
#
# The proc->owner link the WebUI drain trusts is ProcessSession.session_key,
# which the terminal tool captured from the (historically racy) env at spawn.
# An env-IMMUNE spawn-time owner would be the authoritative cross-check, but
# adding such a field to the core ProcessSession is out of scope for this
# WebUI-only change. So this resolver is forward-compatible by DUCK-TYPING:
# if a future core grows an env-immune spawn-owner attribute (any of the
# names below) AND it positively disagrees with the session_key-resolved
# target, re-route to the env-immune owner and log ERROR. When the owner is
# absent/empty/unknown (today's core, cron/CLI processes sharing the
# registry, pre-Option-1 spawns) it is a PURE PASS-THROUGH — it never
# suppresses a legitimate Option Z wakeup on uncertainty (Option Z must keep
# working).
_ENV_IMMUNE_OWNER_ATTRS = (
    "origin_ui_session_id",  # modern hermes-agent exact browser-tab return address
    "spawn_session_id",
    "owner_session_id",
    "turn_session_id",
)


def _env_immune_spawn_owner(proc_session) -> str:
    """Return the env-immune spawn-time owner sid from the ProcessSession, or
    "" when none is available (the only contract today; forward-compatible)."""
    if proc_session is None:
        return ""
    for attr in _ENV_IMMUNE_OWNER_ATTRS:
        try:
            val = getattr(proc_session, attr, "")
        except Exception:
            val = ""
        if val:
            return str(val)
    return ""


def _resolve_wakeup_target(
    *,
    process_id: str,
    session_key_resolved_sid: str,
    proc_session,
) -> str:
    """Cross-check the session_key-resolved wakeup target against the
    env-immune spawn owner. Returns the sid the server-side wakeup turn should
    actually target.

    - Owner unknown/empty  -> pass-through (return session_key_resolved_sid).
    - Owner == resolved    -> pass-through (the normal + post-Option-1 case).
    - Owner != resolved    -> POSITIVE mismatch: log ERROR and RE-ROUTE to the
      env-immune owner (do NOT wake the wrong session — this is the exact
      agent.log:6632 cross-session misroute).
    """
    resolved = str(session_key_resolved_sid or "")
    owner = _env_immune_spawn_owner(proc_session)
    if not owner or owner == resolved:
        return resolved
    logger.error(
        "xsession wakeup misroute BLOCKED (Option 3 safety net): process %r "
        "session_key resolved to session %r but the env-immune spawn owner "
        "is %r — re-routing the server-side wakeup to the true owner. This "
        "means a per-turn session-identity capture race occurred upstream "
        "(Option 1 should have prevented it); investigate streaming.py "
        "_set_turn_session_identity coverage.",
        process_id, resolved, owner,
    )
    return owner


def _requeue_async_delegation_event(
    process_registry,
    evt: dict,
    *,
    claim=None,
    delay: float = 0.5,
) -> bool:
    """Retry without enqueueing new work once drain shutdown has started."""
    completion_queue = getattr(process_registry, "completion_queue", None)
    return requeue_async_delegation_event(
        evt,
        completion_queue,
        delay=delay,
        stop_event=_DRAIN_STOP,
        durable=(bool(getattr(claim, "durable", False)) if claim is not None else None),
    )


def _retry_unmapped_async_delegation_event(process_registry, evt: dict) -> None:
    """Retry durable routing, or one bounded best-effort legacy routing pass."""
    completion_queue = getattr(process_registry, "completion_queue", None)
    if schedule_async_delegation_claim_retry(
        evt,
        completion_queue,
        delay=ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
    ):
        return
    if evt.get("_webui_routing_retry_attempted"):
        return
    retry_evt = dict(evt)
    retry_evt["_webui_routing_retry_attempted"] = True
    _requeue_async_delegation_event(
        process_registry,
        retry_evt,
        delay=ASYNC_DELIVERY_ROUTING_RETRY_SECONDS,
    )


def _record_async_delegation_accepted(
    evt: dict,
    *,
    session_id: str,
    claim,
) -> None:
    """ACK durable delivery and publish live-view state after turn acceptance."""

    complete_async_delegation_delivery(evt, claim)
    payload = _build_payload(evt, session_id)
    try:
        _emit_bg_task_complete_events_coalesced(session_id, payload)
    except Exception:
        logger.debug(
            "async delegation live-view emit failed for session %s",
            session_id,
            exc_info=True,
        )


def _start_async_delegation_wakeup_turn(
    session_id: str,
    wakeup_prompt: str,
    *,
    delegation_id: str,
    evt: dict,
    claim,
    process_registry,
) -> None:
    """Start one autonomous delegation turn and ACK only after acceptance."""

    def _runner() -> None:
        try:
            from api.routes import start_session_turn
            from api.session_lineage import build_completion_delivery_context

            context_event = dict(evt)
            context_event.setdefault("origin_ui_session_id", session_id)
            context_event.setdefault("session_key", f"ui:{session_id}")
            completion_context = build_completion_delivery_context(
                context_event,
                session_id,
            )

            def accept_completion() -> None:
                _record_async_delegation_accepted(
                    evt,
                    session_id=session_id,
                    claim=claim,
                )

            resp = start_session_turn(
                session_id,
                wakeup_prompt,
                source="process_wakeup",
                completion_context=completion_context,
                completion_acceptance=accept_completion,
            )
            raw_status = (resp or {}).get("_status")
            if raw_status is None:
                status = 200 if (resp or {}).get("stream_id") else 500
            else:
                status = int(raw_status)
            if 200 <= status < 300:
                logger.info(
                    "async delegation wakeup turn accepted for session %s "
                    "(stream_id=%s)",
                    session_id,
                    (resp or {}).get("stream_id"),
                )
                return

            release_async_delegation_delivery(evt, claim)
            _requeue_async_delegation_event(process_registry, evt, claim=claim)
            if status == 409 and (resp or {}).get("error") == "process_wakeup_paused":
                logger.info(
                    "async delegation wakeup paused for session %s; delivery remains retryable",
                    session_id,
                )
            else:
                logger.debug(
                    "async delegation wakeup not accepted for session %s: "
                    "status=%s err=%r; requeued",
                    session_id,
                    status,
                    (resp or {}).get("error"),
                )
        except Exception:
            release_async_delegation_delivery(evt, claim)
            _requeue_async_delegation_event(process_registry, evt, claim=claim)
            logger.warning(
                "async delegation wakeup turn failed for session %s; requeued",
                session_id,
                exc_info=True,
            )

    threading.Thread(
        target=_runner,
        name=f"hermes-webui-delegation-wakeup-{str(session_id)[:8]}",
        daemon=True,
    ).start()


def _process_async_delegation_event(
    evt: dict,
    *,
    session_id: str,
    delegation_id: str,
    process_registry,
) -> None:
    """Claim and route one async completion without private registry markers."""

    try:
        claim = claim_async_delegation_delivery(evt, "webui-background")
    except Exception:
        _requeue_async_delegation_event(process_registry, evt)
        return
    if claim is None:
        completion_queue = getattr(process_registry, "completion_queue", None)
        schedule_async_delegation_claim_retry(evt, completion_queue)
        return

    try:
        wakeup_prompt_raw = format_wakeup_prompt(evt)
        wakeup_prompt = wakeup_prompt_raw.strip() if wakeup_prompt_raw else ""
        if not wakeup_prompt:
            raise RuntimeError("async delegation completion could not be formatted")

        # Do not persist async results in the process-local deferred list. If a
        # foreground turn owns the session, release the durable claim and retry
        # from the shared queue; the core record therefore remains restart-safe.
        if _session_has_active_turn(session_id):
            release_async_delegation_delivery(evt, claim)
            _requeue_async_delegation_event(process_registry, evt, claim=claim)
            return

        _start_async_delegation_wakeup_turn(
            session_id,
            wakeup_prompt,
            delegation_id=delegation_id,
            evt=evt,
            claim=claim,
            process_registry=process_registry,
        )
    except Exception:
        release_async_delegation_delivery(evt, claim)
        _requeue_async_delegation_event(process_registry, evt, claim=claim)
        logger.warning(
            "server-side async delegation dispatch failed for session %s",
            session_id,
            exc_info=True,
        )


def _resolve_completion_target(
    *,
    session_key_resolved_sid: str,
    origin_ui_session_id: str,
) -> str:
    """Return the WebUI session that owns a detached completion event.

    Modern Hermes Agent events carry ``origin_ui_session_id`` as an exact,
    immutable return address captured from the commissioning browser turn.
    It is authoritative over the mutable/legacy session-key index. Older
    Agent events omit it and retain the existing session-key fallback.
    """
    resolved = str(session_key_resolved_sid or "")
    owner = str(origin_ui_session_id or "") or resolved
    if resolved and resolved != owner:
        logger.error(
            "cross-session completion route BLOCKED: session_key resolved to %r "
            "but exact origin_ui_session_id is %r; routing to the exact owner",
            resolved,
            owner,
        )
    try:
        return _resolve_session_lineage(owner).delivery_session_id
    except Exception:
        logger.error(
            "completion target lineage could not be verified for %r",
            owner,
            exc_info=True,
        )
        return ""


def _requeue_unresolved_completion(process_registry, evt: dict, *, delay: float = 0.5) -> bool:
    completion_queue = getattr(process_registry, "completion_queue", None)
    return requeue_completion_event(
        evt,
        completion_queue,
        delay=delay,
        stop_event=_DRAIN_STOP,
    )





def _set_completion_attempt_active(
    coordination_session_id: str,
    process_id: str,
) -> None:
    """Move one locally owned completion into the active-attempt phase."""
    if not coordination_session_id or not process_id:
        return
    from api import config as _cfg

    with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.setdefault(
            coordination_session_id,
            set(),
        ).add(process_id)
        _PREINCORPORATION_COMPLETION_IDS.add(process_id)


def _release_completion_attempt(
    coordination_session_id: str,
    process_id: str,
    *,
    keep_seen: bool,
) -> None:
    """End an active attempt, optionally retaining its deferred-owner gate."""
    if not process_id:
        return
    from api import config as _cfg

    with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        _PREINCORPORATION_COMPLETION_IDS.discard(process_id)
        if keep_seen:
            return
        seen = _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.get(
            coordination_session_id,
            set(),
        )
        seen.discard(process_id)
        if coordination_session_id and not seen:
            _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(
                coordination_session_id,
                None,
            )


def _defer_or_requeue_failed_wakeup(
    coordination_session_id: str,
    process_id: str,
    wakeup_prompt: str,
    *,
    completion_event: dict | None = None,
) -> str:
    """Transfer a failed attempt to one existing recovery owner.

    The idempotent deferred map is the normal bounded owner. If that write
    fails, release both local claim phases before putting the immutable event
    back on the existing completion queue exactly once.
    """
    if wakeup_prompt and record_deferred_wakeup(
        coordination_session_id,
        process_id,
        wakeup_prompt,
        completion_event=completion_event,
    ):
        _release_completion_attempt(
            coordination_session_id,
            process_id,
            keep_seen=True,
        )
        return "deferred"

    _release_completion_attempt(
        coordination_session_id,
        process_id,
        keep_seen=False,
    )
    if not isinstance(completion_event, dict):
        return "unowned"
    try:
        from tools.process_registry import process_registry
    except Exception:
        return "unowned"
    if _requeue_unresolved_completion(process_registry, completion_event):
        return "requeued"
    return "unowned"


def _process_one(evt: dict) -> None:
    """Route a single completion_queue event to the matching WebUI session."""
    from api import config as _cfg

    # Hoist the process-registry import once per event: it was imported in
    # three separate blocks below (session_key recovery, env-immune owner
    # cross-check, upstream is_completion_consumed dedupe) on every completion
    # event, paying repeated import-system overhead. Single local rebind keeps
    # the ImportError fallback contract (process_registry may be missing in
    # cut-down vendoring) while collapsing to one lookup per call.
    try:
        from tools.process_registry import process_registry as _process_registry
    except Exception:
        _process_registry = None

    process_id = completion_delivery_id(evt)
    session_key = str(evt.get("session_key") or "")
    origin_ui_session_id = str(evt.get("origin_ui_session_id") or "")
    # Root-cause fix (t_0f447014): the notify_on_complete completion event
    # enqueued by ProcessRegistry._move_to_finished() historically carried NO
    # "session_key" field — only the watch_match enqueue included one. Without
    # it the old `evt.get("session_key") or process_id` fell back to the
    # process id ("proc_xxxx"), which is never a PROCESS_SESSION_INDEX key.
    # Recover spawn-time routing metadata from the process registry.
    if process_id and (not session_key or not origin_ui_session_id):
        try:
            if _process_registry is not None:
                _ps = _process_registry.get(process_id)
                if _ps is not None:
                    if not session_key and getattr(_ps, "session_key", ""):
                        session_key = str(_ps.session_key)
                    if not origin_ui_session_id:
                        origin_ui_session_id = (
                            str(getattr(_ps, "origin_ui_session_id", "") or "")
                            or str(getattr(_ps, "spawn_session_id", "") or "")
                        )
        except Exception:
            logger.debug(
                "session ownership recovery from process registry failed for %r",
                process_id,
                exc_info=True,
            )
    if not session_key and not origin_ui_session_id:
        logger.debug(
            "process_complete drop: no recoverable session_key or exact UI owner "
            "for process_id=%r",
            process_id,
        )
        if evt.get("type") == "async_delegation":
            _retry_unmapped_async_delegation_event(_process_registry, evt)
        else:
            _requeue_unresolved_completion(_process_registry, evt)
        return
    session_id = ""
    if session_key:
        with _cfg.PROCESS_SESSION_INDEX_LOCK:
            session_id = _cfg.PROCESS_SESSION_INDEX.get(session_key) or ""
    if not session_id and not origin_ui_session_id:
        # No mapping — could be a cron/gateway process that uses the same
        # registry but a non-WebUI session_key. Durable delegation events stay
        # pending and are retried because their WebUI ownership mapping can be
        # registered shortly after process restore.
        logger.debug("process_complete drop: no session mapping for key=%r", session_key)
        if evt.get("type") == "async_delegation":
            _retry_unmapped_async_delegation_event(_process_registry, evt)
        else:
            _requeue_unresolved_completion(_process_registry, evt)
        return
    # ── xsession wakeup misroute defense-in-depth (Option 3) ──────────────
    # First retain the process-registry spawn-owner cross-check for legacy
    # terminal events. Then apply origin_ui_session_id as the final authority;
    # that exact owner is shared by process and async-delegation completions.
    try:
        _ps_xs = _process_registry.get(process_id) if (_process_registry is not None and process_id) else None
    except Exception:
        _ps_xs = None
    session_id = _resolve_wakeup_target(
        process_id=process_id,
        session_key_resolved_sid=session_id,
        proc_session=_ps_xs,
    )
    # origin_ui_session_id is the exact, immutable return address; it is the
    # FINAL routing authority over the (mutable) session-key/Option-3 result.
    session_id = _resolve_completion_target(
        session_key_resolved_sid=session_id,
        origin_ui_session_id=origin_ui_session_id,
    )
    if not session_id:
        logger.debug("process_complete drop: completion target resolved empty")
        # An async delegation event that resolves empty here must NOT silently
        # return: no durable claim was taken, so the core row stays pending and
        # the restart-restore sweep would re-deliver it forever. Route it into
        # the bounded retry instead (arms a durable retry / one best-effort
        # legacy requeue), so it stays retryable without a spurious ACK.
        if evt.get("type") == "async_delegation":
            _retry_unmapped_async_delegation_event(_process_registry, evt)
        else:
            _requeue_unresolved_completion(_process_registry, evt)
        return
    # ── THE SEAM: async delegations take the durable-claim delivery path,
    # routed to the origin-resolved session. The claim/complete/release
    # lifecycle (keyed on the immutable delegation_id) is the sole dedupe +
    # restart-safety authority for async events, so they early-return before
    # the terminal-process idempotency/emit/Option-Z machinery below. ──
    if evt.get("type") == "async_delegation":
        _process_async_delegation_event(
            evt,
            session_id=session_id,
            delegation_id=process_id,
            process_registry=_process_registry,
        )
        return
    if process_id:
        try:
            if (
                _process_registry is not None
                and _process_registry.is_completion_consumed(process_id)
            ):
                return
        except Exception:
            logger.debug(
                "is_completion_consumed check failed before durable incorporation",
                exc_info=True,
            )
    coordination_session_id = ""
    completion_claimed = False
    try:
        completion_lineage = _resolve_session_lineage(session_id)
        coordination_session_id = completion_lineage.root_session_id
        session_id = completion_lineage.delivery_session_id
        with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
            seen = _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.setdefault(
                coordination_session_id,
                set(),
            )
            # This is the process-local, pre-incorporation gate. It deliberately
            # uses the same deterministic completion id that becomes
            # ``process:<process_id>`` in the durable receipt and that the
            # registry exposes through ``is_completion_consumed``. The registry
            # marker remains the cross-drain durable ACK, but it is written only
            # after incorporation; this earlier atomic claim prevents a racing
            # duplicate from starting a second turn in that gap. Unresolved
            # lineage returns above before this claim, so its requeue stays live.
            if process_id and (
                process_id in seen
                or process_id in _PREINCORPORATION_COMPLETION_IDS
            ):
                return
            if process_id:
                seen.add(process_id)
                _PREINCORPORATION_COMPLETION_IDS.add(process_id)
                completion_claimed = True
        _emit_bg_task_complete_events_coalesced(
            session_id,
            _build_payload(evt, session_id),
        )
        wakeup_prompt_raw = format_wakeup_prompt(evt)
        wakeup_prompt = wakeup_prompt_raw.strip() if wakeup_prompt_raw else ""
        if not wakeup_prompt:
            raise RuntimeError("process completion could not be formatted")
        if _session_has_active_turn(session_id):
            _cfg.PENDING_BG_TASK_COMPLETIONS.add(coordination_session_id)
            owner = _defer_or_requeue_failed_wakeup(
                coordination_session_id,
                process_id,
                wakeup_prompt,
                completion_event=evt,
            )
            if owner == "unowned":
                logger.error(
                    "process completion has no recovery owner for session %s",
                    session_id,
                )
        else:
            _start_server_side_wakeup_turn(
                session_id,
                wakeup_prompt,
                process_id=process_id,
                completion_event=evt,
            )
    except Exception:
        # No dispatch owner exists when this synchronous path raises (format,
        # emit, deferred-record, or thread-start failure). Release both phases
        # of the local claim before requeueing so the same immutable completion
        # can be reclaimed. Once thread.start() returns, failures are handled by
        # that worker's durable receipt/deferred-retry path and do not reach here.
        if completion_claimed and process_id:
            _release_completion_attempt(
                coordination_session_id,
                process_id,
                keep_seen=False,
            )
        _requeue_unresolved_completion(_process_registry, evt)
        logger.warning(
            "process completion dispatch failed for session %s; requeued",
            session_id,
            exc_info=True,
        )


def record_deferred_wakeup(
    session_id: str,
    process_id: str,
    wakeup_prompt: str,
    *,
    completion_event: dict | None = None,
) -> bool:
    """Persist a deferred process-completion wakeup for later redelivery.

    Called from ``_process_one`` when a completion arrives while a turn is
    active (the Option Z drain branch cannot start a turn — it would 409).
    The turn-teardown idle-hook (``drain_deferred_wakeups_for_session``)
    redelivers it once the session goes idle, OR the PR #2279 next-turn drain
    claims it if a user turn comes first. Whoever claims first wins (atomic
    pop in ``claim_deferred_wakeups``); the other finds nothing.

    Idempotent per process_id: if the same process_id is already queued for
    this session (kill_process racing the reader thread), it is not appended
    twice. Returns whether the prompt is safely queued; never raises into the
    drain loop.
    """
    if not session_id or not wakeup_prompt:
        return False
    from api import config as _cfg

    try:
        coordination_session_id = _resolve_session_lineage(
            session_id
        ).root_session_id
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            entries = _cfg.DEFERRED_PROCESS_WAKEUPS.setdefault(
                coordination_session_id, []
            )
            if process_id:
                existing = next(
                    (
                        entry
                        for entry in entries
                        if entry.get("process_id") == process_id
                    ),
                    None,
                )
                if existing is not None:
                    if isinstance(completion_event, dict):
                        existing.setdefault(
                            "completion_event",
                            dict(completion_event),
                        )
                    return True
            entry: dict[str, Any] = {
                "process_id": process_id,
                "wakeup_prompt": wakeup_prompt,
                "origin_session_id": session_id,
            }
            if isinstance(completion_event, dict):
                entry["completion_event"] = dict(completion_event)
            entries.append(entry)
        return True
    except Exception:
        logger.debug(
            "record_deferred_wakeup failed for session %s", session_id, exc_info=True
        )
        return False


def claim_deferred_wakeups(session_id: str) -> list[dict]:
    """Atomically remove and return all deferred wakeups for *session_id*.

    The single-delivery guarantee for the defer path: the dict ``pop`` under
    ``DEFERRED_PROCESS_WAKEUPS_LOCK`` means whichever caller runs first
    (turn-teardown idle-hook OR PR #2279 next-turn drain) gets the entries and
    delivers them; every subsequent caller gets ``[]``. This is what makes the
    teardown hook idempotent with the next-turn drain (no double-fire) AND
    prevents a wakeup loop (the wakeup turn's own teardown re-runs the hook,
    finds nothing already-claimed → no re-fire).
    """
    if not session_id:
        return []
    from api import config as _cfg

    try:
        coordination_session_id = _resolve_session_lineage(
            session_id
        ).root_session_id
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            return (
                _cfg.DEFERRED_PROCESS_WAKEUPS.pop(coordination_session_id, [])
                or []
            )
    except Exception:
        logger.debug(
            "claim_deferred_wakeups failed for session %s", session_id, exc_info=True
        )
        return []


def drain_deferred_wakeups_for_session(session_id: str) -> int:
    """Turn-teardown idle-hook: redeliver deferred wakeups once idle.

    Called from ``api/streaming`` right AFTER ``unregister_active_run`` so
    ``_session_has_active_turn`` no longer counts the just-ended stream. This
    makes the active-at-completion case symmetric with the idle-at-completion
    case: idle now → fire now (Option Z idle branch); busy now → fire here
    when the turn ends and the session goes idle.

    Multi-stream / cancel-reconnect guard: if ANY other ACTIVE_RUNS row still
    exists for this session (a second stream from cancel/reconnect), the
    session is NOT yet idle — leave the deferred entries untouched so a later
    teardown (or the next-turn drain) delivers them. Only the teardown of the
    LAST active stream for the session claims + fires.

    Returns the number of wakeup turns started (0 when nothing pending or the
    session is still busy). Best-effort — never raises into the streaming
    teardown thread; the actual turn is started on the same throwaway daemon
    thread the idle branch uses, so this never blocks teardown.
    """
    if not session_id:
        return 0
    from api import config as _cfg

    try:
        resolution = _resolve_session_lineage(session_id)
        coordination_session_id = resolution.root_session_id
        delivery_session_id = resolution.delivery_session_id
        # Multi-stream guard: only fire when the session is TRULY idle.
        if _session_has_active_turn(coordination_session_id):
            return 0
        # Peek without claiming: avoid taking the entries then discovering
        # there is nothing to do under contention.
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            if not _cfg.DEFERRED_PROCESS_WAKEUPS.get(coordination_session_id):
                return 0
        # Atomic claim — exactly one caller gets the entries.
        entries = claim_deferred_wakeups(coordination_session_id)
        if not entries:
            return 0
        # The session-level PENDING marker is server-internal telemetry; the
        # real delivery is the prompt(s) we just claimed. Discard it now that
        # the deferred wakeups are owned by this teardown.
        try:
            _cfg.PENDING_BG_TASK_COMPLETIONS.discard(coordination_session_id)
        except Exception:
            logger.debug(
                "PENDING discard failed for session %s", session_id, exc_info=True
            )
        started = 0
        # Greptile P1 fix: do NOT fire one daemon-threaded wakeup per entry in
        # a tight loop. Each ``_start_server_side_wakeup_turn`` spawns a daemon
        # thread that races for the per-session agent lock; only ONE can win,
        # the rest 409. Since we already claimed + popped every entry (line
        # ~938) and discarded the PENDING marker, the losers' prompts would be
        # permanently lost. Instead: start exactly the FIRST prompt, and
        # re-defer the remaining entries so each subsequent turn-teardown
        # (or next-turn drain) delivers the next one — one wakeup per turn,
        # which matches the single-prompt-per-turn design and the
        # BG_TASK_COMPLETE_EVENTS_SEEN dedup (no double-fire).
        leftover = [e for e in entries if str((e or {}).get("wakeup_prompt") or "").strip()]
        if leftover:
            first = leftover[0]
            # Re-defer entries 2..N BEFORE starting the first turn, so they are
            # already persisted if the first wakeup's own teardown re-runs this
            # hook and tries to claim them.
            for entry in leftover[1:]:
                _defer_or_requeue_failed_wakeup(
                    coordination_session_id,
                    str((entry or {}).get("process_id") or ""),
                    str((entry or {}).get("wakeup_prompt") or "").strip(),
                    completion_event=(entry or {}).get("completion_event"),
                )
            first_process_id = str((first or {}).get("process_id") or "")
            first_prompt = str((first or {}).get("wakeup_prompt") or "").strip()
            first_event = (first or {}).get("completion_event")
            _set_completion_attempt_active(
                coordination_session_id,
                first_process_id,
            )
            try:
                _start_server_side_wakeup_turn(
                    delivery_session_id,
                    first_prompt,
                    process_id=first_process_id,
                    completion_event=first_event,
                )
            except Exception:
                owner = _defer_or_requeue_failed_wakeup(
                    coordination_session_id,
                    first_process_id,
                    first_prompt,
                    completion_event=first_event,
                )
                logger.warning(
                    "deferred wakeup thread start failed for session %s; owner=%s",
                    coordination_session_id,
                    owner,
                    exc_info=True,
                )
                return 0
            started = 1
        if started:
            logger.info(
                "turn-teardown idle-hook redelivered %d deferred wakeup(s) "
                "for session %s",
                started,
                coordination_session_id,
            )
        return started
    except Exception:
        logger.warning(
            "drain_deferred_wakeups_for_session failed for session %s",
            session_id,
            exc_info=True,
        )
        return 0


def _session_has_active_turn(session_id: str) -> bool:
    """True if a foreground/streaming agent turn is currently active for *session_id*.

    The drain thread has no Session object, so we key on ACTIVE_RUNS — the
    worker-lifecycle registry that this module already uses (see
    ``_emit_to_session_streams``) to map a stream back to its owning session.
    ACTIVE_RUNS is registered at agent-worker start and removed in the worker's
    outer ``finally``, so it survives cancel/reconnect races better than
    STREAMS. There is a brief window where ``_start_chat_stream_for_session``
    has populated STREAMS but the worker thread has not yet called
    ``register_active_run``; in that window this returns False and the
    subsequent ``start_session_turn`` is rejected with a 409 by
    ``_start_chat_stream_for_session``'s own active-stream guard — i.e. the
    same lock /api/chat/start uses is the authoritative race backstop.
    """
    from api import config as _cfg

    try:
        requested_root = _resolve_session_lineage(session_id).root_session_id
        with _cfg.ACTIVE_RUNS_LOCK:
            active_rows = list((_cfg.ACTIVE_RUNS or {}).values())
        for meta in active_rows:
            if not isinstance(meta, dict):
                return True
            row_root = str(meta.get("lineage_id") or "").strip()
            if not row_root:
                row_session_id = str(
                    meta.get("delivery_session_id")
                    or meta.get("session_id")
                    or ""
                ).strip()
                try:
                    row_root = _resolve_session_lineage(
                        row_session_id
                    ).root_session_id
                except Exception:
                    return True
            if row_root == requested_root:
                return True
    except Exception:
        logger.debug("ACTIVE_RUNS active-turn check failed", exc_info=True)
        return True
    return False


def _start_server_side_wakeup_turn(
    session_id: str,
    wakeup_prompt: str,
    *,
    process_id: str = "",
    completion_event: dict | None = None,
) -> None:
    """Start an agent turn server-side for a process_complete wakeup (Option Z).

    Runs on a short-lived daemon thread so the drain loop NEVER blocks:
    ``start_session_turn`` itself spawns the agent worker thread, but does
    synchronous session-load / workspace / model resolution first, which must
    not stall the single drain thread shared by every WebUI session.

    Concurrency + idempotency are enforced by the layers below, not here:
      - ``start_session_turn`` → ``_start_chat_stream_for_session`` serializes
        on the per-session agent lock and returns ``_status=409`` if a turn is
        already active. A human ``/api/chat/start`` racing this wakeup wins
        (one starts, the other 409s). On 409 we re-queue the prompt via
        ``record_deferred_wakeup`` (see below) so the racing turn's own
        teardown idle-hook — or PR #2279's next-turn drain — redelivers it.
      - ``BG_TASK_COMPLETE_EVENTS_SEEN`` already deduped this process_id in
        ``_process_one`` before we were called, so a process wakes at most once.

    Why re-queue on 409 instead of trusting the PENDING marker: this helper is
    called from two sites. The idle branch of ``_process_one`` leaves the
    PENDING_BG_TASK_COMPLETIONS marker intact, but the teardown-hook caller
    ``drain_deferred_wakeups_for_session`` has ALREADY atomically claimed the
    deferred entry and discarded the marker before spawning this thread. So in
    the teardown path a 409 would otherwise lose the wakeup permanently —
    nothing is left to drain. Re-queuing here is uniformly correct: it is
    idempotent per ``process_id`` (``record_deferred_wakeup`` dedupes) and the
    claim in ``claim_deferred_wakeups`` is atomic, so re-queue can never cause
    a double delivery.
    """

    def _runner() -> None:
        coordination_session_id = session_id
        try:
            from api.routes import start_session_turn
            from api.session_lineage import build_completion_delivery_context

            resolution = _resolve_session_lineage(session_id)
            coordination_session_id = resolution.root_session_id
            delivery_session_id = resolution.delivery_session_id
            completion_context = build_completion_delivery_context(
                {
                    "type": "process_complete",
                    "process_id": process_id,
                    "session_key": f"ui:{coordination_session_id}",
                    "origin_ui_session_id": coordination_session_id,
                    "origin_profile": resolution.profile,
                },
                delivery_session_id,
            )

            def accept_completion() -> None:
                if process_id:
                    _mark_registry_completion_consumed(process_id)

            resp = start_session_turn(
                delivery_session_id,
                wakeup_prompt,
                source="process_wakeup",
                completion_context=completion_context,
                completion_acceptance=accept_completion,
            )
            status = int((resp or {}).get("_status", 200) or 200)
            if status == 409 and (resp or {}).get("error") == "process_wakeup_paused":
                # The persisted pause row is the terminal owner for this
                # attempt. It is intentionally not auto-retried, but it is no
                # longer an active pre-incorporation attempt either.
                _release_completion_attempt(
                    coordination_session_id,
                    process_id,
                    keep_seen=True,
                )
                logger.info(
                    "server-side wakeup suppressed for session %s: provider credential state is paused",
                    session_id,
                )
            elif status == 409 or (
                status == 503 and bool((resp or {}).get("retryable"))
            ):
                # Raced an active turn (e.g. a human /api/chat/start, or a
                # sibling deferred-wakeup thread). Re-defer this prompt so it
                # is delivered by the winning turn's teardown / next-turn drain
                # instead of being lost. The atomic claim in
                # ``claim_deferred_wakeups`` still guarantees exactly-once
                # delivery, and BG_TASK_COMPLETE_EVENTS_SEEN already deduped
                # this process_id, so re-recording cannot double-fire.
                owner = _defer_or_requeue_failed_wakeup(
                    coordination_session_id,
                    process_id,
                    wakeup_prompt,
                    completion_event=completion_event,
                )
                logger.debug(
                    "server-side wakeup raced an active turn for session %s; "
                    "recovery owner=%s",
                    session_id,
                    owner,
                )
            elif status >= 400:
                owner = _defer_or_requeue_failed_wakeup(
                    coordination_session_id,
                    process_id,
                    wakeup_prompt,
                    completion_event=completion_event,
                )
                logger.warning(
                    "server-side wakeup failed for session %s: status=%s err=%r; "
                    "recovery owner=%s",
                    session_id,
                    status,
                    (resp or {}).get("error"),
                    owner,
                )
            else:
                logger.info(
                    "server-side wakeup turn started for session %s (stream_id=%s)",
                    session_id,
                    (resp or {}).get("stream_id"),
                )
        except Exception:
            owner = _defer_or_requeue_failed_wakeup(
                coordination_session_id,
                process_id,
                wakeup_prompt,
                completion_event=completion_event,
            )
            logger.warning(
                "server-side wakeup turn raised for session %s; recovery owner=%s",
                session_id,
                owner,
                exc_info=True,
            )

    threading.Thread(
        target=_runner,
        name=f"hermes-webui-process-wakeup-{str(session_id)[:8]}",
        daemon=True,
    ).start()


def _drain_loop() -> None:
    try:
        from tools import process_registry as _pr_mod  # noqa: F401
        from tools.process_registry import process_registry
    except Exception as exc:
        logger.warning("bg_task_complete drain unavailable: %s", exc)
        return
    logger.info("bg_task_complete drain thread started")
    while not _DRAIN_STOP.is_set():
        # Read the queue defensively: a rebuilt/partially-initialized registry
        # may not expose ``completion_queue`` (mirrors streaming.py's
        # ``getattr(process_registry, 'completion_queue', None)`` guard). Direct
        # attribute access here would raise AttributeError, which the old broad
        # ``except Exception: continue`` swallowed silently and re-tried with no
        # backoff — a 100%-CPU tight loop. Back off on the stop event instead.
        q = getattr(process_registry, "completion_queue", None)
        if q is None:
            _DRAIN_STOP.wait(1.0)
            continue
        try:
            evt = q.get(timeout=1.0)
        except queue.Empty:
            # Nothing to drain this second — re-check the stop flag and loop.
            continue
        except Exception:
            # Unexpected queue failure: log it (not silent) and back off on the
            # stop event so a persistent error can't spin the thread hot.
            logger.warning(
                "bg_task_complete drain queue read failed", exc_info=True
            )
            _DRAIN_STOP.wait(1.0)
            continue
        if not isinstance(evt, dict):
            continue
        try:
            _process_one(evt)
        except Exception:
            logger.warning("bg_task_complete event handling failed", exc_info=True)


def recover_processes_for_webui(process_registry=None, get_session_fn=None) -> int:
    """Recover core background processes and restore WebUI routing metadata.

    The core gateway performs this during gateway startup, but this WebUI host
    previously started only the queue drain. That left checkpointed processes
    invisible after a WebUI restart.
    """
    global _PROCESS_CHECKPOINT_RECOVERED, _PROCESS_RECOVERY_DONE
    if process_registry is None:
        try:
            from tools.process_registry import process_registry
        except ImportError:
            # Hermes Agent is optional in isolated WebUI/test environments.
            # The drain loop already treats a missing registry as unavailable;
            # startup recovery must preserve that fail-soft contract.
            logger.debug("process recovery unavailable: Hermes Agent is not installed")
            return 0
    if get_session_fn is None:
        from api.models import get_session as get_session_fn

    with _PROCESS_RECOVERY_LOCK:
        if _PROCESS_RECOVERY_DONE:
            return 0

        recovered = 0
        if not _PROCESS_CHECKPOINT_RECOVERED:
            recovered = process_registry.recover_from_checkpoint()
            _PROCESS_CHECKPOINT_RECOVERED = True

        for row in process_registry.list_sessions():
            process_id = str(row.get("session_id") or "")
            if not process_id:
                continue
            try:
                proc_session = process_registry.get(process_id)
                session_key = str(getattr(proc_session, "session_key", "") or "")
                if not session_key or get_session_fn(session_key, metadata_only=True) is None:
                    continue
            except Exception:
                logger.warning(
                    "Could not resolve recovered WebUI process %r",
                    process_id,
                    exc_info=True,
                )
                continue
            register_process_session(session_key, session_key)
        from api.routes import recover_accepted_completion_delivery
        from api.session_lineage import accepted_completion_delivery_contexts

        repaired_receipts = 0
        for completion_context in accepted_completion_delivery_contexts():
            try:
                if not recover_accepted_completion_delivery(completion_context):
                    continue
                repaired_receipts += 1
                if completion_context.kind == "process":
                    _mark_registry_completion_consumed(
                        completion_context.completion_id
                    )
            except Exception:
                logger.error(
                    "Accepted completion receipt remains unresolved: %s",
                    completion_context.completion_key,
                    exc_info=True,
                )
        _PROCESS_RECOVERY_DONE = True
        if recovered:
            logger.info("Recovered %d background process(es) for WebUI", recovered)
        if repaired_receipts:
            logger.info(
                "Repaired %d accepted completion receipt(s) without execution",
                repaired_receipts,
            )
        return recovered


def register_process_session(session_key: str, session_id: str) -> None:
    """Bind a process-registry session_key to a WebUI session_id.

    Called at chat-start time, before the agent thread spawns any background
    processes. The same ``session_key`` is exported to the child via
    ``HERMES_SESSION_KEY`` (already done by streaming.py), so when the child
    pushes onto ``completion_queue`` it carries the key we registered.
    """
    if not session_key or not session_id:
        return
    from api import config as _cfg

    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        _cfg.PROCESS_SESSION_INDEX[str(session_key)] = str(session_id)


def unregister_process_session(session_key: str) -> None:
    if not session_key:
        return
    from api import config as _cfg

    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        _cfg.PROCESS_SESSION_INDEX.pop(str(session_key), None)


def forget_bg_task_completion_dedup(session_id: str) -> None:
    """Drop a session's ``BG_TASK_COMPLETE_EVENTS_SEEN`` entry.

    Called on session deletion so a session deleted while a completion is still
    pending (undelivered) — which the reaper's delivery-gated sweep deliberately
    keeps — can't leak its dedup set forever. Safe for unknown ids (no-op).
    """
    if not session_id:
        return
    from api import config as _cfg

    with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        process_ids = _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(str(session_id), set())
        _PREINCORPORATION_COMPLETION_IDS.difference_update(process_ids)


def start_drain_thread() -> bool:
    """Start the background drain thread idempotently. Returns True on first start."""
    global _DRAIN_THREAD
    with _THREAD_LIFECYCLE_LOCK:
        if _DRAIN_THREAD is not None and _DRAIN_THREAD.is_alive():
            return False
        try:
            recover_processes_for_webui()
        except Exception:
            # Recovery is best-effort. A corrupt checkpoint or transient I/O
            # error must not disable notifications for newly spawned tasks.
            logger.warning("background process recovery failed", exc_info=True)
        _DRAIN_STOP.clear()
        _DRAIN_THREAD = threading.Thread(
            target=_drain_loop,
            name="hermes-webui-bg-task-complete-drain",
            daemon=True,
        )
        _DRAIN_THREAD.start()
        return True


def stop_drain_thread(timeout: float = 2.0) -> None:
    _DRAIN_STOP.set()
    th = _DRAIN_THREAD
    if th is not None and th.is_alive():
        th.join(timeout=timeout)
