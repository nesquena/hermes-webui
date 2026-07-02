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
from typing import Any, Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 15
_WAKE_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")
_TERMINAL_STATUSES = {"done", "archived"}

_NOTIFIER_THREAD: Optional[threading.Thread] = None
_NOTIFIER_STOP = threading.Event()

# Dedup: per-subscription set of already-processed (task_id, event_id) pairs.
# Prevents double-wakeup if the cursor advance races with a re-poll.
_DELIVERED_EVENTS: dict[str, set[int]] = {}
_DELIVERED_LOCK = threading.Lock()


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
    return (
        f"[kanban] Task {task_id} {status}.\n"
        f"Title: {title}\n"
        f"Assignee: @{assignee}\n"
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
                    kinds=_WAKE_KINDS,
                )

                if not events:
                    continue

                # Filter out already-delivered events (dedup safety net)
                task_key = f"{slug}:{task_id}:{sub['chat_id']}"
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
    """Start server-side wakeup turns for each delivery."""
    from api.background_process import (
        _session_has_active_turn,
        _start_server_side_wakeup_turn,
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
        title = (task.title if task else task_id)[:120]
        assignee = task.assignee if task else ""
        kinds = {ev.kind for ev in d["events"]}

        prompt = _format_wakeup_prompt(task_id, title, assignee, board_slug, kinds)

        # Use a stable process_id for dedup: kanban:{task_id}:{board}
        process_id = f"kanban:{board_slug}:{task_id}"

        try:
            if _session_has_active_turn(session_id):
                # Session is busy — defer for next teardown/turn drain
                record_deferred_wakeup(session_id, process_id, prompt)
                logger.info(
                    "kanban webui notifier: deferred wakeup for session %s "
                    "(task %s, events %s)",
                    session_id, task_id, kinds,
                )
            else:
                _start_server_side_wakeup_turn(
                    session_id, prompt, process_id=process_id,
                )
                logger.info(
                    "kanban webui notifier: wakeup turn started for session %s "
                    "(task %s, events %s)",
                    session_id, task_id, kinds,
                )
        except Exception:
            logger.warning(
                "kanban webui notifier: wakeup failed for session %s (task %s)",
                session_id, task_id, exc_info=True,
            )

        # If the task reached a truly terminal status, clean up the subscription
        task_terminal = task and task.status in _TERMINAL_STATUSES
        if task_terminal:
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
    """
    global _NOTIFIER_THREAD
    if _NOTIFIER_THREAD is not None and _NOTIFIER_THREAD.is_alive():
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
