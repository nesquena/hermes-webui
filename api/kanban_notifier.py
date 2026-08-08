"""Deliver Hermes Kanban events to their originating WebUI sessions.

Hermes stores WebUI auto-subscriptions with ``platform="webui"``. Messaging
adapters cannot deliver those rows because WebUI is not a gateway platform.
This module polls the shared Kanban databases and starts the same server-side
session turn used for background process wakeups.

The database subscription cursor is the durable delivery marker. Events are
read without changing it, then the cursor advances only after the WebUI accepts
a turn. Active sessions and failed deliveries leave the cursor untouched for a
later retry. A cross-process lock serializes read, delivery, and cursor advance.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - platform-specific import
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

try:  # pragma: no cover - platform-specific import
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover
    _msvcrt = None

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0
CLAIM_KINDS = (
    "completed",
    "blocked",
    "gave_up",
    "crashed",
    "timed_out",
    "status",
    "archived",
    "unblocked",
    "block_loop_detected",
)
WAKE_KINDS = frozenset(
    {
        "completed",
        "blocked",
        "gave_up",
        "crashed",
        "timed_out",
        "block_loop_detected",
    }
)
ATTENTION_STATUSES = frozenset({"blocked", "triage", "done"})
TERMINAL_STATUSES = frozenset({"done", "archived"})

_NOTIFIER_THREAD: threading.Thread | None = None
_NOTIFIER_STOP = threading.Event()
_NOTIFIER_LIFECYCLE_LOCK = threading.Lock()
_NOTIFIER_POLL_LOCK = threading.Lock()


@dataclass(frozen=True)
class Delivery:
    board: str
    sub: dict[str, Any]
    task: Any
    events: tuple[Any, ...]
    new_cursor: int

    @property
    def task_id(self) -> str:
        return str(self.sub.get("task_id") or "")

    @property
    def session_id(self) -> str:
        return str(self.sub.get("chat_id") or "")

    @property
    def idempotency_key(self) -> str:
        return f"kanban:{self.board}:{self.task_id}:{self.new_cursor}"

def _kb():
    from hermes_cli import kanban_db

    return kanban_db


def _resolved_board_key(kb, board_meta: dict[str, Any], slug: str) -> str:
    db_path = board_meta.get("db_path")
    try:
        if db_path:
            return str(Path(db_path).expanduser().resolve())
        return str(kb.kanban_db_path(slug).resolve())
    except Exception:  # noqa: BLE001 - board metadata adapters vary by Hermes release
        return f"slug:{slug}"


def _event_payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _event_needs_wakeup(event: Any) -> bool:
    kind = str(getattr(event, "kind", "") or "")
    if kind in WAKE_KINDS:
        return True
    if kind != "status":
        return False
    status = str(_event_payload(event).get("status") or "").lower()
    return status in ATTENTION_STATUSES


def _wake_events(delivery: Delivery) -> tuple[Any, ...]:
    return tuple(event for event in delivery.events if _event_needs_wakeup(event))


def _single_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _format_event_detail(event: Any) -> str:
    kind = str(getattr(event, "kind", "") or "status changed")
    payload = _event_payload(event)
    if kind == "blocked":
        reason = _single_line(payload.get("reason"), 240)
        return f"blocked: {reason}" if reason else "blocked"
    if kind == "completed":
        summary = _single_line(payload.get("summary"), 240)
        return f"completed: {summary}" if summary else "completed"
    if kind == "gave_up":
        error = _single_line(payload.get("error"), 240)
        return f"gave up: {error}" if error else "gave up"
    if kind == "crashed":
        return "worker crashed; dispatcher will retry"
    if kind == "timed_out":
        limit = payload.get("limit_seconds")
        suffix = f" after {limit}s" if limit else ""
        return f"timed out{suffix}; dispatcher will retry"
    if kind == "block_loop_detected":
        reason = _single_line(payload.get("reason"), 200)
        suffix = f": {reason}" if reason else ""
        return f"routed to triage for a human decision{suffix}"
    if kind == "status":
        status = _single_line(payload.get("status"), 40) or "unknown"
        return f"status changed to {status}"
    return _single_line(kind, 80)


def format_wakeup_prompt(delivery: Delivery) -> str:
    task = delivery.task
    title = _single_line(getattr(task, "title", None) or delivery.task_id, 160)
    assignee = _single_line(getattr(task, "assignee", None), 80)
    event_lines = "\n".join(
        f"- {_format_event_detail(event)}" for event in _wake_events(delivery)
    )
    assignee_text = f"@{assignee}" if assignee else "unassigned"
    return (
        f"[kanban] Task {delivery.task_id} needs attention.\n"
        f"Title: {title}\n"
        f"Assignee: {assignee_text}\n"
        f"Board: {delivery.board}\n"
        f"Event cursor: {delivery.new_cursor}\n"
        f"Events:\n{event_lines}\n\n"
        "Check the card, its comments, and its result. Decide the next step."
    )


def _list_boards(kb) -> list[dict[str, Any]]:
    try:
        return list(kb.list_boards(include_archived=False))
    except Exception:  # noqa: BLE001 - board registry adapters vary by Hermes release
        try:
            return [kb.read_board_metadata(kb.DEFAULT_BOARD)]
        except Exception:
            logger.warning("kanban WebUI notifier could not enumerate boards", exc_info=True)
            return []


def collect_once() -> list[Delivery]:
    """Read deliverable events for every WebUI subscription on every board."""

    kb = _kb()
    deliveries: list[Delivery] = []
    seen_db_paths: set[str] = set()

    for board_meta in _list_boards(kb):
        slug = str(board_meta.get("slug") or kb.DEFAULT_BOARD)
        board_key = _resolved_board_key(kb, board_meta, slug)
        if board_key in seen_db_paths:
            continue
        seen_db_paths.add(board_key)

        try:
            conn = kb.connect(board=slug)
        except Exception:
            logger.debug(
                "kanban WebUI notifier could not open board %s",
                slug,
                exc_info=True,
            )
            continue

        try:
            try:
                subscriptions = kb.list_notify_subs(conn)
            except Exception:
                logger.warning(
                    "kanban WebUI notifier could not list subscriptions on %s",
                    slug,
                    exc_info=True,
                )
                continue

            for sub in subscriptions:
                platform = str(sub.get("platform") or "").lower()
                session_id = str(sub.get("chat_id") or "")
                if platform != "webui" or not session_id:
                    continue
                try:
                    new_cursor, events = kb.unseen_events_for_sub(
                        conn,
                        task_id=sub["task_id"],
                        platform=sub["platform"],
                        chat_id=sub["chat_id"],
                        thread_id=sub.get("thread_id") or "",
                        kinds=CLAIM_KINDS,
                    )
                    try:
                        task = kb.get_task(conn, sub["task_id"])
                    except Exception:  # noqa: BLE001 - a deleted task must not stop other boards
                        task = None
                    if not events:
                        status = str(getattr(task, "status", "") or "").lower()
                        if status not in TERMINAL_STATUSES:
                            continue
                    deliveries.append(
                        Delivery(
                            board=slug,
                            sub=dict(sub),
                            task=task,
                            events=tuple(events),
                            new_cursor=int(new_cursor or 0),
                        )
                    )
                except Exception:
                    logger.warning(
                        "kanban WebUI subscription %s on %s could not be read",
                        sub.get("task_id"),
                        slug,
                        exc_info=True,
                    )
        finally:
            conn.close()

    return deliveries


def _advance(delivery: Delivery) -> bool:
    kb = _kb()
    try:
        conn = kb.connect(board=delivery.board)
        try:
            kb.advance_notify_cursor(
                conn,
                task_id=delivery.task_id,
                platform=delivery.sub["platform"],
                chat_id=delivery.session_id,
                thread_id=delivery.sub.get("thread_id") or "",
                new_cursor=delivery.new_cursor,
            )
        finally:
            conn.close()
        return True
    except Exception:
        logger.exception(
            "kanban WebUI cursor advance failed for %s on %s",
            delivery.task_id,
            delivery.board,
        )
        return False


def _remove_terminal_subscription(delivery: Delivery) -> None:
    status = str(getattr(delivery.task, "status", "") or "").lower()
    if status not in TERMINAL_STATUSES:
        return
    kb = _kb()
    try:
        conn = kb.connect(board=delivery.board)
        try:
            kb.remove_notify_sub(
                conn,
                task_id=delivery.task_id,
                platform=delivery.sub["platform"],
                chat_id=delivery.session_id,
                thread_id=delivery.sub.get("thread_id") or "",
            )
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "kanban WebUI terminal subscription cleanup failed for %s on %s",
            delivery.task_id,
            delivery.board,
            exc_info=True,
        )


def _response_status(response: Any) -> int:
    response = response if isinstance(response, dict) else {}
    raw_status = response.get("_status")
    if raw_status is None:
        return 200 if response.get("stream_id") else 500
    try:
        return int(raw_status)
    except (TypeError, ValueError):
        return 500


def deliver_one(delivery: Delivery) -> bool:
    """Deliver one event batch and advance its cursor only after acceptance."""

    wake_events = _wake_events(delivery)
    if not wake_events:
        if not _advance(delivery):
            return False
        _remove_terminal_subscription(delivery)
        return True

    from api.background_process import _session_has_active_turn

    prompt = format_wakeup_prompt(delivery)
    accepted = False
    try:
        if _session_has_active_turn(delivery.session_id):
            return False
        else:
            from api.routes import start_session_turn

            response = start_session_turn(
                delivery.session_id,
                prompt,
                source="process_wakeup",
                idempotency_key=delivery.idempotency_key,
            )
            status = _response_status(response)
            if 200 <= status < 300:
                accepted = True

    except Exception:
        logger.warning(
            "kanban WebUI wakeup failed for session %s and task %s",
            delivery.session_id,
            delivery.task_id,
            exc_info=True,
        )

    if not accepted:
        return False

    if not _advance(delivery):
        return False
    _remove_terminal_subscription(delivery)
    logger.info(
        "kanban WebUI wakeup accepted for session %s task %s board %s cursor %s",
        delivery.session_id,
        delivery.task_id,
        delivery.board,
        delivery.new_cursor,
    )
    return True


@contextmanager
def _notifier_process_lock():
    """Serialize cursor read, delivery, and advance across WebUI processes."""

    lock_path = _notifier_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as lock_file:
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            return
        if _msvcrt is not None:
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"\0")
            lock_file.seek(0)
            _msvcrt.locking(  # type: ignore[attr-defined]
                lock_file.fileno(), _msvcrt.LK_LOCK, 1  # type: ignore[attr-defined]
            )
            try:
                yield
            finally:
                lock_file.seek(0)
                _msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(), _msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            return
        raise RuntimeError("cross-process Kanban notifier locking is unavailable")


def _notifier_lock_path() -> Path:
    """Anchor the process lock beside the shared default Kanban database."""

    kb = _kb()
    default_board = str(getattr(kb, "DEFAULT_BOARD", "default") or "default")
    db_path = Path(kb.kanban_db_path(default_board)).expanduser().resolve()
    return db_path.parent / ".kanban-notifier.lock"


def poll_and_deliver_once() -> int:
    delivered = 0
    with _NOTIFIER_POLL_LOCK, _notifier_process_lock():
        for delivery in collect_once():
            if deliver_one(delivery):
                delivered += 1
    return delivered


def _notifier_enabled() -> bool:
    env_value = os.environ.get("HERMES_WEBUI_KANBAN_NOTIFIER_ENABLED")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.config import cfg_get, load_config

        config = load_config()
        return bool(cfg_get(config, "kanban", "webui_notifier", default=False))
    except Exception:
        logger.debug("kanban WebUI notifier config could not be read", exc_info=True)
        return False


def _notifier_loop() -> None:
    logger.info("kanban WebUI notifier thread started")
    while not _NOTIFIER_STOP.is_set():
        try:
            poll_and_deliver_once()
        except Exception:
            logger.warning("kanban WebUI notifier poll failed", exc_info=True)
        _NOTIFIER_STOP.wait(POLL_INTERVAL_SECONDS)


def start_notifier_thread() -> bool:
    """Start the opt-in process-wide notifier exactly once."""

    global _NOTIFIER_THREAD
    with _NOTIFIER_LIFECYCLE_LOCK:
        if _NOTIFIER_THREAD is not None and _NOTIFIER_THREAD.is_alive():
            return False
        if not _notifier_enabled():
            return False
        _NOTIFIER_STOP.clear()
        thread = threading.Thread(
            target=_notifier_loop,
            name="hermes-webui-kanban-notifier",
            daemon=True,
        )
        _NOTIFIER_THREAD = thread
        thread.start()
        return True


def stop_notifier_thread(timeout: float = 2.0) -> None:
    """Stop the notifier and wait briefly for its daemon thread."""

    global _NOTIFIER_THREAD
    _NOTIFIER_STOP.set()
    with _NOTIFIER_LIFECYCLE_LOCK:
        thread = _NOTIFIER_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    with _NOTIFIER_LIFECYCLE_LOCK:
        if _NOTIFIER_THREAD is thread and (thread is None or not thread.is_alive()):
            _NOTIFIER_THREAD = None
