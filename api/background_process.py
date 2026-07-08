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

logger = logging.getLogger(__name__)

_DRAIN_THREAD: Optional[threading.Thread] = None
_DRAIN_STOP = threading.Event()

_REAPER_THREAD: Optional[threading.Thread] = None
_REAPER_STOP = threading.Event()
_REAPER_INTERVAL_SECS = 60.0

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
            # every tick. The registry's own per-``process_id``
            # ``_completion_consumed`` gate remains the primary idempotency
            # backstop, so sweeping a delivered session's set can never resurrect
            # an already-delivered completion (even in the tiny window between
            # this module's ``SEEN.add`` and ``PENDING.add`` in ``_process_one``).
            from api import config as _cfg

            with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
                for sid in [
                    s
                    for s in _cfg.BG_TASK_COMPLETE_EVENTS_SEEN
                    if s not in _cfg.PENDING_BG_TASK_COMPLETIONS
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
    # the process id. Alias it locally before exposing it as payload ``task_id``
    # to avoid confusing that wire-format name with the WebUI session id.
    process_id = str(evt.get("session_id") or "")
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
    except AttributeError:
        logger.error(
            "ProcessRegistry coupling contract VIOLATED: expected private "
            "attrs %s for cross-A/B wakeup dedupe are missing — an upstream "
            "rename has broken the shared marker; process_complete wakeups may "
            "now double-fire. A public mark_completion_consumed() upstream is "
            "the durable fix (Copilot #2242 review #4).",
            _REGISTRY_CONSUMED_CONTRACT,
            exc_info=True,
        )
        return
    try:
        with lock:
            consumed.add(process_id)
    except (AttributeError, TypeError):
        logger.error(
            "ProcessRegistry coupling contract VIOLATED: _lock/"
            "_completion_consumed changed shape (not a Lock / not a set) — "
            "cross-A/B wakeup dedupe is broken; wakeups may double-fire. "
            "Upstream public mark_completion_consumed() is the durable fix "
            "(Copilot #2242 review #4).",
            exc_info=True,
        )


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
_ENV_IMMUNE_OWNER_ATTRS = ("spawn_session_id", "owner_session_id", "turn_session_id")


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

    process_id = str(evt.get("session_id") or "")
    session_key = str(evt.get("session_key") or "")
    # Root-cause fix (t_0f447014): the notify_on_complete completion event
    # enqueued by ProcessRegistry._move_to_finished() carries NO "session_key"
    # field — only the watch_match enqueue includes one. Without it the old
    # `evt.get("session_key") or process_id` fell back to the process id
    # ("proc_xxxx"), which is never a PROCESS_SESSION_INDEX key (only
    # webui_session_id -> webui_session_id is registered at chat-start), so
    # every wakeup was silently dropped here and the frontend never POSTed an
    # ack. Recover the spawn-time session_key from the process registry's
    # ProcessSession: the terminal tool captured it synchronously at spawn
    # (while the turn's env was active), so it survives the turn-end env
    # restore and is the WebUI session_id for WebUI-spawned processes.
    if not session_key and process_id:
        try:
            if _process_registry is not None:
                _ps = _process_registry.get(process_id)
                if _ps is not None and getattr(_ps, "session_key", ""):
                    session_key = str(_ps.session_key)
        except Exception:
            logger.debug(
                "session_key recovery from process registry failed for %r",
                process_id,
                exc_info=True,
            )
    if not session_key:
        logger.debug(
            "process_complete drop: no recoverable session_key for process_id=%r",
            process_id,
        )
        return
    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        session_id = _cfg.PROCESS_SESSION_INDEX.get(session_key)
    if not session_id:
        # No mapping — could be a cron/gateway process that uses the same
        # registry but a non-WebUI session_key. Ignore.
        logger.debug("process_complete drop: no session mapping for key=%r", session_key)
        return
    # ── xsession wakeup misroute defense-in-depth (Option 3) ──────────────
    # session_id above came from PROCESS_SESSION_INDEX.get(session_key), and
    # session_key was captured by the terminal tool from the (historically
    # racy) process-global env at spawn. Option 1 binds the per-turn identity
    # to a contextvar so that capture is no longer racy — but as an INDEPENDENT
    # safety net, cross-check the resolved target against the env-immune
    # spawn owner (when the core ProcessSession exposes one). On a positive
    # mismatch this re-routes the wakeup (and the live-view emit + dedupe
    # markers below) to the TRUE owner instead of waking the wrong session.
    # Pure pass-through when no env-immune owner is available (today's core,
    # cron/CLI procs, pre-Option-1 spawns) — never suppresses a valid wakeup.
    try:
        _ps_xs = _process_registry.get(process_id) if (_process_registry is not None and process_id) else None
    except Exception:
        _ps_xs = None
    session_id = _resolve_wakeup_target(
        process_id=process_id,
        session_key_resolved_sid=session_id,
        proc_session=_ps_xs,
    )
    # ── Idempotency vs the REAL merged upstream #2279 (shared dedupe key) ──
    # The real merged #2279 next-turn drain
    # (api/streaming._drain_webui_process_notifications) dedupes ONLY via
    # process_registry.is_completion_consumed() / _completion_consumed — it
    # does NOT populate BG_TASK_COMPLETE_EVENTS_SEEN (that set is ours-original
    # and private to this module). So the cross-A/B shared dedupe contract is
    # process_registry._completion_consumed, NOT BG_TASK_COMPLETE_EVENTS_SEEN.
    # If the upstream A-drain already delivered this process_id (A-first
    # order), it marked _completion_consumed; B must early-return here or it
    # would double-fire a wakeup. This guard aligns our B-drain to the real
    # upstream key (verified against origin/master streaming.py).
    if process_id:
        try:
            if _process_registry is not None and _process_registry.is_completion_consumed(process_id):
                return
        except Exception:
            logger.debug(
                "is_completion_consumed check failed on B drain; "
                "falling back to BG_TASK_COMPLETE_EVENTS_SEEN gate",
                exc_info=True,
            )
    # Secondary (ours-original) idempotency: if we've already emitted for this
    # (session_id, process_id) pair via THIS module, skip the duplicate. Two
    # _move_to_finished() callers (kill_process racing the reader thread) can
    # occasionally enqueue twice despite the process_registry guard.
    with _cfg.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        seen = _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.setdefault(session_id, set())
        if process_id and process_id in seen:
            return
        if process_id:
            seen.add(process_id)
    payload = _build_payload(evt, session_id)
    _emit_bg_task_complete_events_coalesced(session_id, payload)
    _cfg.PENDING_BG_TASK_COMPLETIONS.add(session_id)
    # Mark the event consumed in the agent's process registry so the REAL
    # merged PR #2279's next-turn drain
    # (api/streaming._drain_webui_process_notifications) treats this process_id
    # as already-delivered and does not re-fire a wakeup (B-first order).
    # This is the SHARED upstream dedupe key (see _mark_registry_completion_
    # consumed for the coupling contract + why a future rename now fails loud).
    if process_id:
        _mark_registry_completion_consumed(process_id)

    # ── Option Z (PRIMARY): server-side wakeup, NO browser round-trip ──────
    # The SSE emit above is now demoted to a pure live-view layer (an open tab
    # streams the turn live via the per-session SSE channel). The ACTUAL agent
    # wakeup is started HERE, server-side, so a CLOSED tab still gets the turn
    # — parity with how CLI / Telegram / gateway self-wake from a
    # notify_on_complete completion. This is the fix for the structural flaw:
    # "fire a long background task, close the tab, come back later" is THE
    # primary background-task use case and browser-mediated wakeup could never
    # serve it.
    #
    #   - turn ACTIVE → do NOT start a turn. Leave the PENDING_PROCESS_
    #     COMPLETIONS marker so PR #2279's next-turn drain
    #     (api/streaming._drain_webui_process_notifications) injects the wakeup
    #     when the active turn ends. (That path already works when a turn is
    #     active — it was never the gap.)
    #   - turn IDLE → start a new server-side turn directly with wakeup_prompt
    #     as the user message (the real gap Option Z closes).
    #
    # Idempotency is already guaranteed above: BG_TASK_COMPLETE_EVENTS_SEEN +
    # the registry _completion_consumed marker mean this process_id reached
    # here at most once, so the wakeup turn starts at most once.
    try:
        # ``wakeup_prompt`` is server-internal state used only by the
        # Option Z server-side wakeup; it was previously surfaced on the
        # SSE payload but T1 trimmed the payload to the minimal shape
        # `{session_id, task_id, completed_at, summary?, event_id}`, so
        # we derive the prompt directly from the evt here (same source the
        # prior _build_payload used).
        wakeup_prompt_raw = format_wakeup_prompt(evt)
        wakeup_prompt = wakeup_prompt_raw.strip() if wakeup_prompt_raw else ""
        if wakeup_prompt:
            if _session_has_active_turn(session_id):
                # Defer-path fix: persist the prompt so a turn-teardown
                # idle-hook can redeliver it once the session goes idle.
                # The OLD behavior only logged + left a bare
                # PENDING_BG_TASK_COMPLETIONS session flag; the prompt was
                # discarded and the next-turn drain reads completion_queue
                # (already emptied by THIS drain thread), so for an
                # autonomous agent with no next user turn the wakeup was
                # lost forever. process_id is already in
                # BG_TASK_COMPLETE_EVENTS_SEEN + the registry
                # _completion_consumed marker (set above), so persisting it
                # here cannot cause a double-fire — the atomic claim in
                # ``claim_deferred_wakeups`` guarantees exactly one delivery.
                record_deferred_wakeup(session_id, process_id, wakeup_prompt)
                logger.debug(
                    "server-side wakeup deferred: turn active for session %s "
                    "(persisted for turn-teardown idle-hook redelivery)",
                    session_id,
                )
            else:
                # Idle-path sibling of the F1 (409/teardown) fix: pass
                # ``process_id`` so that if this idle wakeup's daemon thread
                # loses the per-session lock race and 409s, the re-defer in
                # ``_start_server_side_wakeup_turn`` records the entry WITH its
                # process_id — keeping the ``record_deferred_wakeup`` dedup
                # guard (``if process_id and any(...)``) live on that re-defer
                # path so a second 409 race cannot accumulate a duplicate
                # deferred entry (which would deliver the same wakeup twice).
                _start_server_side_wakeup_turn(
                    session_id, wakeup_prompt, process_id=process_id
                )
    except Exception:
        logger.warning(
            "server-side wakeup dispatch failed for session %s", session_id, exc_info=True
        )


def record_deferred_wakeup(session_id: str, process_id: str, wakeup_prompt: str) -> None:
    """Persist a deferred process-completion wakeup for later redelivery.

    Called from ``_process_one`` when a completion arrives while a turn is
    active (the Option Z drain branch cannot start a turn — it would 409).
    The turn-teardown idle-hook (``drain_deferred_wakeups_for_session``)
    redelivers it once the session goes idle, OR the PR #2279 next-turn drain
    claims it if a user turn comes first. Whoever claims first wins (atomic
    pop in ``claim_deferred_wakeups``); the other finds nothing.

    Idempotent per process_id: if the same process_id is already queued for
    this session (kill_process racing the reader thread), it is not appended
    twice. Best-effort — never raises into the drain loop.
    """
    if not session_id or not wakeup_prompt:
        return
    from api import config as _cfg

    try:
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            entries = _cfg.DEFERRED_PROCESS_WAKEUPS.setdefault(session_id, [])
            if process_id and any(
                e.get("process_id") == process_id for e in entries
            ):
                return
            entries.append(
                {"process_id": process_id, "wakeup_prompt": wakeup_prompt}
            )
    except Exception:
        logger.debug(
            "record_deferred_wakeup failed for session %s", session_id, exc_info=True
        )


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
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            return _cfg.DEFERRED_PROCESS_WAKEUPS.pop(session_id, []) or []
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
        # Multi-stream guard: only fire when the session is TRULY idle.
        if _session_has_active_turn(session_id):
            return 0
        # Peek without claiming: avoid taking the entries then discovering
        # there is nothing to do under contention.
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            if not _cfg.DEFERRED_PROCESS_WAKEUPS.get(session_id):
                return 0
        # Atomic claim — exactly one caller gets the entries.
        entries = claim_deferred_wakeups(session_id)
        if not entries:
            return 0
        # The session-level PENDING marker is server-internal telemetry; the
        # real delivery is the prompt(s) we just claimed. Discard it now that
        # the deferred wakeups are owned by this teardown.
        try:
            _cfg.PENDING_BG_TASK_COMPLETIONS.discard(session_id)
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
                record_deferred_wakeup(
                    session_id,
                    str((entry or {}).get("process_id") or ""),
                    str((entry or {}).get("wakeup_prompt") or "").strip(),
                )
            _start_server_side_wakeup_turn(
                session_id,
                str((first or {}).get("wakeup_prompt") or "").strip(),
                process_id=str((first or {}).get("process_id") or ""),
            )
            started = 1
        if started:
            logger.info(
                "turn-teardown idle-hook redelivered %d deferred wakeup(s) "
                "for session %s",
                started,
                session_id,
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
        with _cfg.ACTIVE_RUNS_LOCK:
            for _stream_id, meta in (_cfg.ACTIVE_RUNS or {}).items():
                if isinstance(meta, dict) and meta.get("session_id") == session_id:
                    return True
    except Exception:
        logger.debug("ACTIVE_RUNS active-turn check failed", exc_info=True)
    return False


def _start_server_side_wakeup_turn(
    session_id: str, wakeup_prompt: str, *, process_id: str = ""
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
        try:
            from api.routes import start_session_turn

            resp = start_session_turn(
                session_id, wakeup_prompt, source="process_wakeup"
            )
            status = int((resp or {}).get("_status", 200) or 200)
            if status == 409:
                # Raced an active turn (e.g. a human /api/chat/start, or a
                # sibling deferred-wakeup thread). Re-defer this prompt so it
                # is delivered by the winning turn's teardown / next-turn drain
                # instead of being lost. The atomic claim in
                # ``claim_deferred_wakeups`` still guarantees exactly-once
                # delivery, and BG_TASK_COMPLETE_EVENTS_SEEN already deduped
                # this process_id, so re-recording cannot double-fire.
                if wakeup_prompt:
                    record_deferred_wakeup(session_id, process_id, wakeup_prompt)
                logger.debug(
                    "server-side wakeup raced an active turn for session %s; "
                    "re-deferred for redelivery on next teardown/turn",
                    session_id,
                )
            elif status >= 400:
                logger.warning(
                    "server-side wakeup failed for session %s: status=%s err=%r",
                    session_id,
                    status,
                    (resp or {}).get("error"),
                )
            else:
                logger.info(
                    "server-side wakeup turn started for session %s (stream_id=%s)",
                    session_id,
                    (resp or {}).get("stream_id"),
                )
        except Exception:
            logger.warning(
                "server-side wakeup turn raised for session %s",
                session_id,
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
        try:
            evt = process_registry.completion_queue.get(timeout=1.0)
        except Exception:
            # queue.Empty or transient — re-check stop flag and continue.
            continue
        if not isinstance(evt, dict):
            continue
        try:
            _process_one(evt)
        except Exception:
            logger.warning("bg_task_complete event handling failed", exc_info=True)


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
        _cfg.BG_TASK_COMPLETE_EVENTS_SEEN.pop(str(session_id), None)


def start_drain_thread() -> bool:
    """Start the background drain thread idempotently. Returns True on first start."""
    global _DRAIN_THREAD
    if _DRAIN_THREAD is not None and _DRAIN_THREAD.is_alive():
        return False
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
