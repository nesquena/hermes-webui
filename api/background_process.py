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
    4. emits a ``process_complete`` SSE event on the active stream(s) for that
       session (DEMOTED to pure live-view — an open tab streams the turn
       live), records a server-side marker in ``PENDING_PROCESS_COMPLETIONS``,
       and — Option Z PIVOT — starts the agent wakeup turn **directly
       server-side** when the session is idle (``_start_server_side_wakeup_turn``
       → ``routes.start_session_turn``). This needs NO browser round-trip, so
       the closed-tab case works exactly like CLI / Telegram / gateway
       self-wake. When a turn is already active the wakeup is NOT started here;
       the ``PENDING_PROCESS_COMPLETIONS`` marker is left for PR #2279's
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
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DRAIN_THREAD: Optional[threading.Thread] = None
_DRAIN_STOP = threading.Event()

_REAPER_THREAD: Optional[threading.Thread] = None
_REAPER_STOP = threading.Event()
_REAPER_INTERVAL_SECS = 60.0


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
        grace period, OR when ``created_at`` is older than
        SESSION_CHANNEL_IDLE_TTL_SECS regardless of subscriber count (zombie
        cap).
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
                # idempotent (frontend dedupes by process_id).
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
                logger.debug("SessionChannel reaper collected: %s", collected)
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


def format_wakeup_prompt(evt: dict) -> str:
    """Build the synthetic [IMPORTANT: …] message the agent will see.

    Mirrors ``cli._format_process_notification`` so wakeup payloads look the
    same in CLI and WebUI sessions.
    """
    evt_type = evt.get("type", "completion")
    sid = evt.get("session_id", "unknown")
    cmd = evt.get("command", "unknown")
    if evt_type == "watch_disabled":
        return f"[IMPORTANT: {evt.get('message', '')}]"
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
    # Default: completion event
    exit_code = evt.get("exit_code", "?")
    out = _truncate(evt.get("output", ""), 4000)
    return (
        f"[IMPORTANT: Background process {sid} completed (exit_code={exit_code}).\n"
        f"Command: {cmd}\n"
        f"Output:\n{out}]"
    )


def _build_payload(evt: dict, session_id: str) -> dict:
    """Build the SSE data payload — pure data, no host-side state."""
    return {
        "session_id": str(session_id),
        "process_id": str(evt.get("session_id") or ""),
        "command": _truncate(str(evt.get("command") or ""), 200),
        "exit_code": evt.get("exit_code"),
        "type": str(evt.get("type") or "completion"),
        "stdout_preview": _truncate(str(evt.get("output") or ""), 4000),
        "wakeup_prompt": format_wakeup_prompt(evt),
        "emitted_at": time.time(),
    }


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
    with _cfg.STREAMS_LOCK:
        items = list(_cfg.STREAMS.items())
    for stream_id, channel in items:
        meta = _cfg.ACTIVE_RUNS.get(stream_id) if hasattr(_cfg, "ACTIVE_RUNS") else None
        owner_sid = (meta or {}).get("session_id") if isinstance(meta, dict) else None
        # When no ACTIVE_RUNS row matches, fall back to broadcasting — the
        # frontend ignores events for the wrong session_id by inspecting the
        # payload (data.session_id !== activeSid).
        if owner_sid and owner_sid != session_id:
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


def _process_one(evt: dict) -> None:
    """Route a single completion_queue event to the matching WebUI session."""
    from api import config as _cfg

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
            from tools.process_registry import process_registry as _pr
            _ps = _pr.get(process_id)
            if _ps is not None and getattr(_ps, "session_key", ""):
                session_key = str(_ps.session_key)
        except Exception:
            logger.debug(
                "session_key recovery from process registry failed for %r",
                process_id,
                exc_info=True,
            )
    if not session_key:
        session_key = process_id
    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        session_id = _cfg.PROCESS_SESSION_INDEX.get(session_key)
    if not session_id:
        # No mapping — could be a cron/gateway process that uses the same
        # registry but a non-WebUI session_key. Ignore.
        logger.debug("process_complete drop: no session mapping for key=%r", session_key)
        return
    # ── Idempotency vs the REAL merged upstream #2279 (shared dedupe key) ──
    # The real merged #2279 next-turn drain
    # (api/streaming._drain_webui_process_notifications) dedupes ONLY via
    # process_registry.is_completion_consumed() / _completion_consumed — it
    # does NOT populate PROCESS_COMPLETE_EVENTS_SEEN (that set is ours-original
    # and private to this module). So the cross-A/B shared dedupe contract is
    # process_registry._completion_consumed, NOT PROCESS_COMPLETE_EVENTS_SEEN.
    # If the upstream A-drain already delivered this process_id (A-first
    # order), it marked _completion_consumed; B must early-return here or it
    # would double-fire a wakeup. This guard aligns our B-drain to the real
    # upstream key (verified against origin/master streaming.py).
    if process_id:
        try:
            from tools.process_registry import process_registry as _pr
            if _pr.is_completion_consumed(process_id):
                return
        except Exception:
            logger.debug(
                "is_completion_consumed check failed on B drain; "
                "falling back to PROCESS_COMPLETE_EVENTS_SEEN gate",
                exc_info=True,
            )
    # Secondary (ours-original) idempotency: if we've already emitted for this
    # (session_id, process_id) pair via THIS module, skip the duplicate. Two
    # _move_to_finished() callers (kill_process racing the reader thread) can
    # occasionally enqueue twice despite the process_registry guard.
    seen = _cfg.PROCESS_COMPLETE_EVENTS_SEEN.setdefault(session_id, set())
    if process_id and process_id in seen:
        return
    if process_id:
        seen.add(process_id)
    payload = _build_payload(evt, session_id)
    _emit_to_session_streams(session_id, "process_complete", payload)
    _cfg.PENDING_PROCESS_COMPLETIONS.add(session_id)
    # Mark the event consumed in the agent's process registry so the REAL
    # merged PR #2279's next-turn drain
    # (api/streaming._drain_webui_process_notifications) treats this process_id
    # as already-delivered and does not re-fire a wakeup (B-first order).
    # This is the SHARED upstream dedupe key — best-effort private API access;
    # failures are logged-debug because the secondary PROCESS_COMPLETE_EVENTS
    # _SEEN gate still applies for THIS module's own duplicates.
    if process_id:
        try:
            from tools.process_registry import process_registry as _pr
            with _pr._lock:
                _pr._completion_consumed.add(process_id)
        except Exception:
            logger.debug("Failed to mark process completion consumed on B drain", exc_info=True)

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
    # Idempotency is already guaranteed above: PROCESS_COMPLETE_EVENTS_SEEN +
    # the registry _completion_consumed marker mean this process_id reached
    # here at most once, so the wakeup turn starts at most once.
    try:
        wakeup_prompt = str(payload.get("wakeup_prompt") or "").strip()
        if wakeup_prompt:
            if _session_has_active_turn(session_id):
                # Defer-path fix: persist the prompt so a turn-teardown
                # idle-hook can redeliver it once the session goes idle.
                # The OLD behavior only logged + left a bare
                # PENDING_PROCESS_COMPLETIONS session flag; the prompt was
                # discarded and the next-turn drain reads completion_queue
                # (already emptied by THIS drain thread), so for an
                # autonomous agent with no next user turn the wakeup was
                # lost forever. process_id is already in
                # PROCESS_COMPLETE_EVENTS_SEEN + the registry
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
                _start_server_side_wakeup_turn(session_id, wakeup_prompt)
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
            _cfg.PENDING_PROCESS_COMPLETIONS.discard(session_id)
        except Exception:
            logger.debug(
                "PENDING discard failed for session %s", session_id, exc_info=True
            )
        started = 0
        for entry in entries:
            prompt = str((entry or {}).get("wakeup_prompt") or "").strip()
            if not prompt:
                continue
            # Same server-side wakeup path the idle branch uses. It spawns its
            # own daemon thread, so the teardown thread never blocks. The
            # process_id is already in PROCESS_COMPLETE_EVENTS_SEEN +
            # registry _completion_consumed (set in _process_one before the
            # defer), so no other path re-delivers it.
            _start_server_side_wakeup_turn(session_id, prompt)
            started += 1
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


def _start_server_side_wakeup_turn(session_id: str, wakeup_prompt: str) -> None:
    """Start an agent turn server-side for a process_complete wakeup (Option Z).

    Runs on a short-lived daemon thread so the drain loop NEVER blocks:
    ``start_session_turn`` itself spawns the agent worker thread, but does
    synchronous session-load / workspace / model resolution first, which must
    not stall the single drain thread shared by every WebUI session.

    Concurrency + idempotency are enforced by the layers below, not here:
      - ``start_session_turn`` → ``_start_chat_stream_for_session`` serializes
        on the per-session agent lock and returns ``_status=409`` if a turn is
        already active. A human ``/api/chat/start`` racing this wakeup wins
        (one starts, the other 409s). On 409 the PENDING_PROCESS_COMPLETIONS
        marker is left intact (the 409 returns before the marker discard), so
        PR #2279's next-turn drain still delivers the wakeup.
      - ``PROCESS_COMPLETE_EVENTS_SEEN`` already deduped this process_id in
        ``_process_one`` before we were called, so a process wakes at most once.
    """

    def _runner() -> None:
        try:
            from api.routes import start_session_turn

            resp = start_session_turn(
                session_id, wakeup_prompt, source="process_wakeup"
            )
            status = int((resp or {}).get("_status", 200) or 200)
            if status == 409:
                logger.debug(
                    "server-side wakeup raced an active turn for session %s; "
                    "PENDING_PROCESS_COMPLETIONS marker will drain on next turn",
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
        logger.warning("process_complete drain unavailable: %s", exc)
        return
    logger.info("process_complete drain thread started")
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
            logger.warning("process_complete event handling failed", exc_info=True)


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


def start_drain_thread() -> bool:
    """Start the background drain thread idempotently. Returns True on first start."""
    global _DRAIN_THREAD
    if _DRAIN_THREAD is not None and _DRAIN_THREAD.is_alive():
        return False
    _DRAIN_STOP.clear()
    _DRAIN_THREAD = threading.Thread(
        target=_drain_loop,
        name="hermes-webui-process-complete-drain",
        daemon=True,
    )
    _DRAIN_THREAD.start()
    return True


def stop_drain_thread(timeout: float = 2.0) -> None:
    _DRAIN_STOP.set()
    th = _DRAIN_THREAD
    if th is not None and th.is_alive():
        th.join(timeout=timeout)
