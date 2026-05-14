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
       session and records a server-side marker in
       ``PENDING_PROCESS_COMPLETIONS``. The frontend re-POSTs ``wakeup_prompt``
       as the next user turn (see ``static/messages.js`` listener), and
       ``routes.py:_start_chat_stream_for_session`` atomically discards the
       marker on consume.

The marker is *not* required for delivery — it's a telemetry-style flag the
turn handler can read to know "this stream is a process_complete wakeup, not a
human-typed prompt". It also lets the routes layer ignore the wakeup if no SSE
client was connected when the process finished; the marker drains harmlessly
on the next ``/api/chat/start`` for the session.

Watch-pattern events share the same queue but produce a different SSE payload;
this module routes them to the same listener so the frontend's single
``process_complete`` handler can re-POST either flavor verbatim.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DRAIN_THREAD: Optional[threading.Thread] = None
_DRAIN_STOP = threading.Event()


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
    return emitted


def _process_one(evt: dict) -> None:
    """Route a single completion_queue event to the matching WebUI session."""
    from api import config as _cfg

    process_id = str(evt.get("session_id") or "")
    session_key = str(evt.get("session_key") or process_id)
    with _cfg.PROCESS_SESSION_INDEX_LOCK:
        session_id = _cfg.PROCESS_SESSION_INDEX.get(session_key)
    if not session_id:
        # No mapping — could be a cron/gateway process that uses the same
        # registry but a non-WebUI session_key. Ignore.
        logger.debug("process_complete drop: no session mapping for key=%r", session_key)
        return
    # Idempotency: if we've already emitted for this (session_id, process_id)
    # pair, skip the duplicate. Two _move_to_finished() callers (kill_process
    # racing the reader thread) can occasionally enqueue twice despite the
    # process_registry guard.
    seen = _cfg.PROCESS_COMPLETE_EVENTS_SEEN.setdefault(session_id, set())
    if process_id and process_id in seen:
        return
    if process_id:
        seen.add(process_id)
    payload = _build_payload(evt, session_id)
    _emit_to_session_streams(session_id, "process_complete", payload)
    _cfg.PENDING_PROCESS_COMPLETIONS.add(session_id)


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
