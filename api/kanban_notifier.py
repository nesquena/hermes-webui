"""Kanban task-event notifier for WebUI agent wakeup.

The Hermes gateway's ``kanban_watchers.py`` delivers kanban task terminal
events (completed / blocked / gave_up / crashed / timed_out) to messaging
platforms (Telegram, Discord, etc.) and wakes the creator agent by injecting
a synthetic message. However, WebUI sessions set ``platform="webui"`` in
the subscription row, and the gateway has no ``"webui"`` adapter — so those
events are silently skipped.

This module bridges that gap. A background polling thread (started at WebUI
startup alongside the ``background_process`` drain thread) reads
``kanban_notify_subs`` rows for ``platform='webui'``, claims unseen terminal
events via ``claim_unseen_events_for_sub``, and triggers a server-side agent
wakeup turn using the same ``_start_server_side_wakeup_turn`` infrastructure
that ``notify_on_complete`` uses for background process completions.

Thread lifecycle mirrors ``background_process.start_drain_thread`` /
``stop_drain_thread``: started once at server boot, stopped on shutdown.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 15
# Kinds that trigger a wakeup prompt to the creator agent
_WAKE_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")
# Additional kinds that don't wake the agent but signal a silent terminal
# transition — we claim them so _deliver can clean up the subscription
# and dedup entry. Without this, a task that reaches done/archived via
# a non-wake event would leak its subscription forever.
_SILENT_TERMINAL_KINDS = ("status", "archived", "unblocked")
# Kinds to claim from the DB: wake kinds + silent terminal kinds
_CLAIM_KINDS = _WAKE_KINDS + _SILENT_TERMINAL_KINDS
_TERMINAL_STATUSES = {"done", "archived"}

_NOTIFIER_THREAD: Optional[threading.Thread] = None
_NOTIFIER_STOP = threading.Event()

# Dedup: per-subscription set of already-processed event IDs.
# Prevents double-wakeup if the cursor advance races with a re-poll.
# Keys are cleaned up on terminal unsubscribe, so growth is bounded by the
# number of active (non-terminal) subscriptions.
_DELIVERED_EVENTS: dict[str, set[int]] = {}
_DELIVERED_LOCK = threading.Lock()


def _dedup_key(board: str, task_id: str, chat_id: str) -> str:
    """Build the dedup dict key for a subscription."""
    return f"{board}:{task_id}:{chat_id}"


def _kb():
    """Lazily import hermes_cli.kanban_db to avoid circular imports."""
    from hermes_cli import kanban_db as kb
    return kb


def _format_wakeup_prompt(task_id: str, title: str, assignee: str,
                          board: str, kinds: set[str]) -> str:
    """Build the synthetic wakeup message injected into the agent session.

    Mirrors the gateway's i18n wake message template
    (``gateway.kanban.wake.message``) so the agent sees the same payload
    regardless of whether it was created from Telegram, Discord, or WebUI.
    """
    kind_labels = {
        "completed": "completed",
        "blocked": "blocked; needs attention",
        "gave_up": "gave up (retries exhausted)",
        "crashed": "crashed (worker exited); dispatcher will retry",
        "timed_out": "timed out; dispatcher will retry",
    }
    parts = [kind_labels.get(k, k) for k in sorted(kinds)]
    status = ", ".join(parts) or "status changed"
    assignee_str = f"@{assignee}" if assignee else ""
    return (
        f"[kanban] Task {task_id} {status}.\n"
        f"Title: {title}\n"
        f"Assignee: {assignee_str}\n"
        f"Board: {board}\n\n"
        f"Check the result or decide the next step."
    )


def _poll_once() -> list[dict]:
    """Collect all deliverable terminal events for ``platform='webui'`` subs.

    Returns a list of dicts with keys: ``sub``, ``task``, ``events``,
    ``board``. Atomically claims events (advances the cursor) so a concurrent
    gateway notifier tick cannot double-deliver.
    """
    kb = _kb()
    deliveries: list[dict] = []

    try:
        boards = kb.list_boards(include_archived=False)
    except Exception:
        boards = [kb.read_board_metadata(kb.DEFAULT_BOARD)]

    seen_db_paths: set[str] = set()
    for board_meta in boards:
        slug = board_meta.get("slug") or kb.DEFAULT_BOARD
        db_path = board_meta.get("db_path")
        try:
            if db_path:
                resolved = str(Path(db_path).expanduser().resolve())
            else:
                resolved = str(kb.kanban_db_path(slug).resolve())
        except Exception:
            resolved = f"slug:{slug}"
        if resolved in seen_db_paths:
            continue
        seen_db_paths.add(resolved)

        try:
            conn = kb.connect(board=slug)
        except Exception as exc:
            logger.debug("kanban webui notifier: cannot open board %s: %s", slug, exc)
            continue

        try:
            subs = kb.list_notify_subs(conn)
            for sub in subs:
                platform = (sub.get("platform") or "").lower()
                if platform != "webui":
                    continue

                task_id = sub["task_id"]
                old_cursor, new_cursor, events = kb.claim_unseen_events_for_sub(
                    conn,
                    task_id=task_id,
                    platform=sub["platform"],
                    chat_id=sub["chat_id"],
                    thread_id=sub.get("thread_id") or None,
                    kinds=_CLAIM_KINDS,
                )

                if not events:
                    continue

                # Filter out already-delivered events (dedup safety net)
                task_key = _dedup_key(slug, task_id, sub["chat_id"])
                with _DELIVERED_LOCK:
                    delivered = _DELIVERED_EVENTS.setdefault(task_key, set())
                    fresh = [ev for ev in events if ev.id not in delivered]
                    if not fresh:
                        continue
                    for ev in fresh:
                        delivered.add(ev.id)
                    # Trim the set to prevent unbounded growth
                    if len(delivered) > 200:
                        delivered.clear()
                        for ev in fresh:
                            delivered.add(ev.id)

                # Fetch task info for the wakeup prompt
                task = None
                try:
                    task = kb.get_task(conn, task_id)
                except Exception:
                    pass

                deliveries.append({
                    "sub": sub,
                    "task": task,
                    "events": fresh,
                    "board": slug,
                    "old_cursor": old_cursor,
                    "new_cursor": new_cursor,
                })
        finally:
            conn.close()

    return deliveries


def _deliver(deliveries: list[dict]) -> None:
    """Start server-side wakeup turns for each delivery.

    Unlike background_process._start_server_side_wakeup_turn (which spawns a
    daemon and returns before the result is known), this calls
    start_session_turn synchronously so we can distinguish success (200),
    race (409 → defer), and genuine failure (≥400 / exception → rewind cursor).
    """
    from api.background_process import (
        _session_has_active_turn,
        record_deferred_wakeup,
    )

    for d in deliveries:
        sub = d["sub"]
        task = d["task"]
        board_slug = d["board"]
        session_id = sub.get("chat_id") or ""
        if not session_id:
            continue

        task_id = sub["task_id"]
        all_kinds = {ev.kind for ev in d["events"]}
        # Split into wake kinds (trigger prompt) and silent kinds (cleanup only)
        wake_kinds = all_kinds & set(_WAKE_KINDS)
        silent_kinds = all_kinds - set(_WAKE_KINDS)

        title = (task.title if task else task_id)[:120]
        assignee = (task.assignee if task else None) or ""

        # Use a stable process_id for dedup: kanban:{task_id}:{board}
        process_id = f"kanban:{board_slug}:{task_id}"

        # If there are no wake kinds, this is a silent-terminal delivery.
        # Skip the prompt but still clean up if the task is terminal.
        if not wake_kinds:
            delivery_ok = True  # nothing to deliver, so no failure
            logger.debug(
                "kanban webui notifier: silent terminal event for task %s "
                "(kinds=%s) — skipping prompt, checking cleanup",
                task_id, silent_kinds,
            )
        else:
            prompt = _format_wakeup_prompt(
                task_id, title, assignee, board_slug, wake_kinds
            )
            delivery_ok = False
            try:
                if _session_has_active_turn(session_id):
                    # Session is busy — defer for next teardown/turn drain
                    record_deferred_wakeup(session_id, process_id, prompt)
                    logger.info(
                        "kanban webui notifier: deferred wakeup for session %s "
                        "(task %s, events %s)",
                        session_id, task_id, wake_kinds,
                    )
                    delivery_ok = True
                else:
                    # Call start_session_turn synchronously to get the real status.
                    # _start_server_side_wakeup_turn spawns a daemon that we cannot
                    # await, so we call the underlying primitive directly.
                    from api.routes import start_session_turn
                    resp = start_session_turn(
                        session_id, prompt, source="kanban_wakeup"
                    )
                    status = int((resp or {}).get("_status", 200) or 200)
                    if status == 409:
                        # Raced an active turn — defer for redelivery
                        record_deferred_wakeup(session_id, process_id, prompt)
                        logger.info(
                            "kanban webui notifier: wakeup raced active turn for "
                            "session %s (task %s); deferred",
                            session_id, task_id,
                        )
                        delivery_ok = True
                    elif status >= 400:
                        logger.warning(
                            "kanban webui notifier: wakeup failed for session %s "
                            "(task %s): status=%s err=%r",
                            session_id, task_id, status,
                            (resp or {}).get("error"),
                        )
                        delivery_ok = False
                    else:
                        logger.info(
                            "kanban webui notifier: wakeup turn started for session "
                            "%s (task %s, events %s)",
                            session_id, task_id, wake_kinds,
                        )
                        delivery_ok = True
            except Exception:
                logger.warning(
                    "kanban webui notifier: wakeup failed for session %s (task %s) "
                    "— rewinding cursor so the event is retried next tick",
                    session_id, task_id, exc_info=True,
                )
                delivery_ok = False

        if not delivery_ok:
            # Rewind the DB cursor so the event is re-delivered on the next
            # tick instead of being permanently lost. The in-memory dedup set
            # is also cleared for this sub so the retry is not skipped.
            try:
                kb = _kb()
                conn = kb.connect(board=board_slug)
                try:
                    kb.rewind_notify_cursor(
                        conn,
                        task_id=task_id,
                        platform=sub["platform"],
                        chat_id=sub["chat_id"],
                        thread_id=sub.get("thread_id") or None,
                        claimed_cursor=d["new_cursor"],
                        old_cursor=d["old_cursor"],
                    )
                finally:
                    conn.close()
                task_key = _dedup_key(board_slug, task_id, sub["chat_id"])
                with _DELIVERED_LOCK:
                    _DELIVERED_EVENTS.pop(task_key, None)
            except Exception:
                logger.warning(
                    "kanban webui notifier: cursor rewind also failed for %s — "
                    "event will be lost",
                    task_id, exc_info=True,
                )

        # If the task reached a truly terminal status and delivery succeeded,
        # clean up the subscription. Skip removal when delivery failed (cursor
        # was rewound) so the retry on the next tick can still find the sub.
        task_terminal = task and task.status in _TERMINAL_STATUSES
        if task_terminal and delivery_ok:
            try:
                kb = _kb()
                conn = kb.connect(board=board_slug)
                try:
                    kb.remove_notify_sub(
                        conn,
                        task_id=task_id,
                        platform=sub["platform"],
                        chat_id=sub["chat_id"],
                        thread_id=sub.get("thread_id") or None,
                    )
                finally:
                    conn.close()
            except Exception:
                logger.debug(
                    "kanban webui notifier: failed to unsub %s", task_id,
                    exc_info=True,
                )
            # Clean up the in-memory dedup key as well
            task_key = _dedup_key(board_slug, task_id, sub["chat_id"])
            with _DELIVERED_LOCK:
                _DELIVERED_EVENTS.pop(task_key, None)


def _notifier_loop() -> None:
    """Main polling loop — runs in a daemon thread."""
    logger.info("kanban webui notifier thread started")
    while not _NOTIFIER_STOP.is_set():
        try:
            deliveries = _poll_once()
            if deliveries:
                _deliver(deliveries)
        except Exception:
            logger.warning("kanban webui notifier tick failed", exc_info=True)

        # Sleep with cancellation checks
        for _ in range(_POLL_INTERVAL_SECONDS):
            if _NOTIFIER_STOP.is_set():
                return
            time.sleep(1)


def start_notifier_thread() -> bool:
    """Start the kanban notifier polling thread idempotently.

    Returns True on first start, False if already running.
    Called from ``server.py`` at WebUI startup, alongside
    ``background_process.start_drain_thread``.

    Gated by ``kanban.webui_notifier`` in config.yaml (default: False — opt-in).
    Enable to turn on the polling thread for kanban→agent wake notifications.
    When unset or false, the thread never starts — no always-on background
    subsystem for users who don't use kanban.
    """
    global _NOTIFIER_THREAD
    if _NOTIFIER_THREAD is not None and _NOTIFIER_THREAD.is_alive():
        return False

    # Config gate: opt-in (default-off) per the optional-capability convention.
    # A user who doesn't use kanban shouldn't get a new always-on background thread.
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        enabled = cfg_get(cfg, "kanban", "webui_notifier", default=False)
        if not enabled:
            logger.debug(
                "kanban webui notifier: not started (kanban.webui_notifier "
                "not set or false — opt-in feature)"
            )
            return False
    except Exception:
        # If config can't load, stay default-off (don't start an always-on
        # thread without explicit user opt-in)
        logger.debug(
            "kanban webui notifier: config unreadable, staying default-off"
        )
        return False

    _NOTIFIER_STOP.clear()
    _NOTIFIER_THREAD = threading.Thread(
        target=_notifier_loop,
        name="hermes-webui-kanban-notifier",
        daemon=True,
    )
    _NOTIFIER_THREAD.start()
    return True


def stop_notifier_thread(timeout: float = 2.0) -> None:
    """Signal the notifier thread to stop and wait briefly."""
    _NOTIFIER_STOP.set()
    th = _NOTIFIER_THREAD
    if th is not None and th.is_alive():
        th.join(timeout=timeout)
