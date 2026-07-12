"""Background and ephemeral task tracking for /background and /btw commands."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from api.config import SESSION_DIR
from api.models import is_safe_session_id

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_DURABLE_SCHEMA_VERSION = 1
_PROMPT_PREVIEW_LIMIT = 500
_BACKGROUND_CARD_TEXT = "Background task running…"

# parent_session_id -> list of task dicts
_BACKGROUND_TASKS: dict[str, list[dict[str, Any]]] = {}

# btw ephemeral session tracking: parent_sid -> {ephemeral_sid, stream_id, question}
_BTW_TRACKING: dict[str, dict[str, Any]] = {}


def _task_store_path(parent_sid: str):
    if not is_safe_session_id(parent_sid):
        raise ValueError(f"Unsafe session_id {parent_sid!r}; refusing background task sidecar path")
    return SESSION_DIR / f"{parent_sid}.background_tasks.json"


def _prompt_preview(prompt: str) -> str:
    text = str(prompt or "")
    if len(text) <= _PROMPT_PREVIEW_LIMIT:
        return text
    return text[:_PROMPT_PREVIEW_LIMIT] + "\n…(truncated)"


def _task_card_message_id(task_id: str) -> str:
    return f"bgcard_{task_id}"


def _task_result_message_id(task_id: str) -> str:
    return f"bgresult_{task_id}"


def _read_durable_tasks_unlocked(parent_sid: str) -> list[dict[str, Any]]:
    path = _task_store_path(parent_sid)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        logger.warning("Ignoring unreadable background task sidecar for %s", parent_sid, exc_info=True)
        return []
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks")
    return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []


def _write_durable_tasks_unlocked(parent_sid: str, tasks: list[dict[str, Any]]) -> None:
    path = _task_store_path(parent_sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _DURABLE_SCHEMA_VERSION,
        "parent_session_id": parent_sid,
        "tasks": tasks,
        "updated_at": time.time(),
    }
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.current_thread().ident}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _upsert_durable_task_unlocked(parent_sid: str, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    tasks = _read_durable_tasks_unlocked(parent_sid)
    now = time.time()
    task = None
    for candidate in tasks:
        if candidate.get("task_id") == task_id:
            task = candidate
            break
    if task is None:
        task = {
            "task_id": task_id,
            "parent_session_id": parent_sid,
            "status": "running",
            "started_at": now,
            "completed_at": None,
        }
        tasks.append(task)
    task.update(updates)
    task["updated_at"] = now
    _write_durable_tasks_unlocked(parent_sid, tasks)
    return dict(task)


def _mark_durable_parent_append(parent_sid: str, task_id: str, updates: dict[str, Any]) -> None:
    with _lock:
        _upsert_durable_task_unlocked(parent_sid, task_id, updates)


def _public_task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    """Return the durable task fields safe for status/list API responses."""
    return {
        "task_id": task.get("task_id"),
        "parent_session_id": task.get("parent_session_id"),
        "status": task.get("status") or "unknown",
        "bg_session_id": task.get("bg_session_id"),
        "stream_id": task.get("stream_id"),
        "prompt_preview": task.get("prompt_preview"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "updated_at": task.get("updated_at"),
        "answer": task.get("answer"),
        "error": task.get("error"),
        "parent_append_status": task.get("parent_append_status"),
        "parent_card_message_id": task.get("parent_card_message_id"),
        "parent_result_message_id": task.get("parent_result_message_id"),
    }


def _find_message_by_id(messages: list[dict[str, Any]], message_id: str) -> dict[str, Any] | None:
    for message in messages:
        if isinstance(message, dict) and message.get("_message_id") == message_id:
            return message
    return None


def _background_task_metadata(task: dict[str, Any], *, result_message_id: str | None = None) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "")
    meta = {
        "task_id": task_id,
        "status": task.get("status") or "unknown",
        "origin_turn_id": task.get("origin_turn_id"),
        "prompt_preview": task.get("prompt_preview"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "can_cancel": task.get("status") == "running",
    }
    if task.get("bg_session_id"):
        meta["bg_session_id"] = task.get("bg_session_id")
    if task.get("stream_id"):
        meta["stream_id"] = task.get("stream_id")
    if result_message_id:
        meta["result_message_id"] = result_message_id
    return meta


def _build_background_card(task: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "")
    started_at = task.get("started_at")
    timestamp = int(started_at if isinstance(started_at, (int, float)) and started_at > 0 else time.time())
    return {
        "role": "assistant",
        "content": _BACKGROUND_CARD_TEXT,
        "timestamp": timestamp,
        "_background": True,
        "_message_id": _task_card_message_id(task_id),
        "_background_task": _background_task_metadata(task),
    }


def _build_background_result_message(task: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "")
    completed_at = task.get("completed_at")
    timestamp = int(completed_at if isinstance(completed_at, (int, float)) and completed_at > 0 else time.time())
    answer = str(task.get("answer") or "").strip() or "(no answer produced)"
    return {
        "role": "assistant",
        "content": f"**Background result**\n\n{answer}",
        "timestamp": timestamp,
        "_background": True,
        "_message_id": _task_result_message_id(task_id),
        "_background_result": {
            "task_id": task_id,
            "origin_turn_id": task.get("origin_turn_id"),
            "completed_at": task.get("completed_at"),
            "status": task.get("status") or "done",
        },
    }


def _parent_has_live_turn(parent_session) -> bool:
    stream_id = getattr(parent_session, "active_stream_id", None)
    if not stream_id:
        return False
    try:
        from api import config as _cfg
        with _cfg.STREAMS_LOCK:
            if stream_id in (_cfg.STREAMS or {}):
                return True
        with _cfg.ACTIVE_RUNS_LOCK:
            if stream_id in (_cfg.ACTIVE_RUNS or {}):
                return True
    except Exception:
        logger.debug(
            "Could not inspect active parent stream for %s",
            getattr(parent_session, "session_id", None),
            exc_info=True,
        )
    return False


def _apply_parent_update(parent_session, task: dict[str, Any], update_kind: str) -> bool:
    messages = list(getattr(parent_session, "messages", None) or [])
    task_id = str(task.get("task_id") or "")
    card_id = _task_card_message_id(task_id)
    result_id = _task_result_message_id(task_id)
    changed = False

    card = _find_message_by_id(messages, card_id)
    if card is None:
        messages.append(_build_background_card(task))
        changed = True
        card = messages[-1]
    if isinstance(card, dict):
        desired_meta = _background_task_metadata(
            task,
            result_message_id=result_id if update_kind in {"done", "result"} else None,
        )
        if card.get("_background_task") != desired_meta:
            card["_background_task"] = desired_meta
            changed = True
        if card.get("content") != _BACKGROUND_CARD_TEXT:
            card["content"] = _BACKGROUND_CARD_TEXT
            changed = True

    if update_kind in {"done", "result"}:
        desired_result = _build_background_result_message(task)
        result_msg = _find_message_by_id(messages, result_id)
        if result_msg is None:
            messages.append(desired_result)
            changed = True
        elif result_msg != desired_result:
            result_msg.clear()
            result_msg.update(desired_result)
            changed = True

    if changed:
        parent_session.messages = messages
    return changed


def append_or_queue_background_parent_update(parent_sid: str, task_id: str, update_kind: str) -> str:
    """Persist a background task card/result into the parent session when safe.

    The durable task sidecar remains authoritative for lifecycle state. Parent
    transcript mutation is an idempotent display projection. If the parent turn
    is active, this function records a pending parent append state instead of
    writing to ``messages``; a later drain hook can safely materialize it at the
    active-turn boundary.
    """
    with _lock:
        task = next(
            (dict(t) for t in _read_durable_tasks_unlocked(parent_sid)
             if str(t.get("task_id") or "") == str(task_id or "")),
            None,
        )
    if task is None:
        return "missing_task"

    card_id = _task_card_message_id(str(task_id))
    result_id = _task_result_message_id(str(task_id))
    pending_status = "card_pending" if update_kind == "running" else "result_pending"
    written_status = "card_written" if update_kind == "running" else "result_written"
    try:
        from api.config import _get_session_agent_lock
        from api.models import Session
        with _get_session_agent_lock(parent_sid):
            parent = Session.load(parent_sid)
            if parent is None:
                _mark_durable_parent_append(parent_sid, str(task_id), {
                    "parent_append_status": "parent_missing",
                    "parent_card_message_id": card_id,
                    "parent_result_message_id": result_id if update_kind != "running" else None,
                })
                return "parent_missing"
            if _parent_has_live_turn(parent):
                _mark_durable_parent_append(parent_sid, str(task_id), {
                    "parent_append_status": pending_status,
                    "parent_card_message_id": card_id,
                    "parent_result_message_id": result_id if update_kind != "running" else None,
                })
                return pending_status
            changed = _apply_parent_update(parent, task, update_kind)
            if changed:
                parent.save()
    except Exception:
        logger.warning(
            "Failed to project background task %s into parent session %s",
            task_id,
            parent_sid,
            exc_info=True,
        )
        _mark_durable_parent_append(parent_sid, str(task_id), {
            "parent_append_status": "parent_append_failed",
            "parent_card_message_id": card_id,
            "parent_result_message_id": result_id if update_kind != "running" else None,
        })
        return "parent_append_failed"

    _mark_durable_parent_append(parent_sid, str(task_id), {
        "parent_append_status": written_status,
        "parent_card_message_id": card_id,
        "parent_result_message_id": result_id if update_kind != "running" else None,
    })
    return written_status


def drain_pending_background_parent_updates(parent_session) -> int:
    """Materialize pending background cards/results into an idle parent session.

    Streaming finalization calls this after clearing ``active_stream_id`` and
    after the foreground assistant merge, but before saving the final parent
    session. This avoids loading an older session file and overwriting the just-
    completed turn while still making background cards durable at the first safe
    active-turn boundary.
    """
    parent_sid = str(getattr(parent_session, "session_id", "") or "")
    if not parent_sid or _parent_has_live_turn(parent_session):
        return 0
    with _lock:
        pending_tasks = [
            dict(task) for task in _read_durable_tasks_unlocked(parent_sid)
            if task.get("parent_append_status") in {"card_pending", "result_pending"}
        ]
    if not pending_tasks:
        return 0

    applied = 0
    updates: list[tuple[str, dict[str, Any]]] = []
    for task in pending_tasks:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        update_kind = "running" if task.get("parent_append_status") == "card_pending" else "done"
        if _apply_parent_update(parent_session, task, update_kind):
            applied += 1
        updates.append((task_id, {
            "parent_append_status": "card_written" if update_kind == "running" else "result_written",
            "parent_card_message_id": _task_card_message_id(task_id),
            "parent_result_message_id": _task_result_message_id(task_id) if update_kind != "running" else None,
        }))

    if updates:
        with _lock:
            for task_id, update in updates:
                _upsert_durable_task_unlocked(parent_sid, task_id, update)
    return applied


def list_durable_background_tasks(parent_sid: str) -> list[dict[str, Any]]:
    """Return durable background task records for a parent session.

    This is the prototype persistence foundation for Issue 39. It intentionally
    does not consume completed results; legacy ``get_results()`` keeps the old
    polling semantics until the UI/API migration is ready.
    """
    with _lock:
        return [_public_task_snapshot(t) for t in _read_durable_tasks_unlocked(parent_sid)]


def get_durable_background_task(parent_sid: str, task_id: str) -> dict[str, Any] | None:
    """Return one durable task by id without consuming terminal state."""
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    with _lock:
        for task in _read_durable_tasks_unlocked(parent_sid):
            if str(task.get("task_id") or "") == task_id:
                return _public_task_snapshot(task)
    return None


def durable_background_status(parent_sid: str) -> dict[str, Any]:
    """Return the idempotent durable status payload for a parent session."""
    tasks = list_durable_background_tasks(parent_sid)
    return {"ok": True, "session_id": parent_sid, "tasks": tasks}


def _emit_background_task_updated(parent_sid: str, task_id: str) -> None:
    """Best-effort live nudge for durable background task state changes.

    The parent session/task sidecar remains the source of truth. This event is
    intentionally tiny: browser consumers use it as a prompt to refetch durable
    state rather than trusting SSE payloads as result storage.
    """
    task = get_durable_background_task(parent_sid, task_id)
    if not task:
        return
    payload = {
        "session_id": parent_sid,
        "task_id": task.get("task_id"),
        "status": task.get("status") or "unknown",
        "event_id": uuid.uuid4().hex,
        "updated_at": task.get("updated_at") or time.time(),
        "parent_append_status": task.get("parent_append_status"),
        "parent_card_message_id": task.get("parent_card_message_id"),
        "parent_result_message_id": task.get("parent_result_message_id"),
    }
    try:
        from api.background_process import emit_session_event
        emit_session_event(parent_sid, "background_task_updated", payload)
    except Exception:
        logger.debug(
            "Failed to emit background_task_updated for %s/%s",
            parent_sid,
            task_id,
            exc_info=True,
        )


def track_background(parent_sid: str, bg_sid: str, stream_id: str,
                     task_id: str, prompt: str) -> None:
    started_at = time.time()
    with _lock:
        _BACKGROUND_TASKS.setdefault(parent_sid, []).append({
            "task_id": task_id,
            "bg_session_id": bg_sid,
            "stream_id": stream_id,
            "prompt": prompt,
            "status": "running",
            "started_at": started_at,
            "answer": None,
            "completed_at": None,
        })
        _upsert_durable_task_unlocked(parent_sid, task_id, {
            "status": "running",
            "bg_session_id": bg_sid,
            "stream_id": stream_id,
            "prompt_preview": _prompt_preview(prompt),
            "started_at": started_at,
            "completed_at": None,
            "answer": None,
            "error": None,
            "parent_append_status": "card_pending",
            "parent_card_message_id": _task_card_message_id(task_id),
            "parent_result_message_id": None,
        })
    append_or_queue_background_parent_update(parent_sid, task_id, "running")
    _emit_background_task_updated(parent_sid, task_id)


def track_btw(parent_sid: str, ephemeral_sid: str, stream_id: str,
              question: str) -> None:
    with _lock:
        _BTW_TRACKING[parent_sid] = {
            "ephemeral_session_id": ephemeral_sid,
            "stream_id": stream_id,
            "question": question,
        }


def complete_background(parent_sid: str, task_id: str, answer: str) -> None:
    completed_at = time.time()
    with _lock:
        for t in _BACKGROUND_TASKS.get(parent_sid, []):
            if t["task_id"] == task_id and t["status"] == "running":
                t["status"] = "done"
                t["answer"] = answer
                t["completed_at"] = completed_at
                break
        _upsert_durable_task_unlocked(parent_sid, task_id, {
            "status": "done",
            "answer": str(answer or ""),
            "completed_at": completed_at,
            "error": None,
            "parent_append_status": "result_pending",
            "parent_card_message_id": _task_card_message_id(task_id),
            "parent_result_message_id": _task_result_message_id(task_id),
        })
    append_or_queue_background_parent_update(parent_sid, task_id, "done")
    _emit_background_task_updated(parent_sid, task_id)


def get_results(parent_sid: str) -> list[dict[str, Any]]:
    """Return completed background task results and remove only the done ones
    from tracking.  Tasks still in ``status="running"`` MUST stay in the list
    so that ``complete_background()`` can still find them when the worker
    thread finishes — otherwise the first poll during a long-running task
    silently drops it and the result is lost forever.
    """
    with _lock:
        tasks = _BACKGROUND_TASKS.get(parent_sid, [])
        done = [t for t in tasks if t["status"] == "done"]
        still_running = [t for t in tasks if t["status"] != "done"]
        if still_running:
            _BACKGROUND_TASKS[parent_sid] = still_running
        else:
            _BACKGROUND_TASKS.pop(parent_sid, None)
        return [{
            "task_id": t["task_id"],
            "prompt": t["prompt"],
            "answer": t["answer"],
            "completed_at": t["completed_at"],
        } for t in done]


def get_background_tasks(parent_sid: str) -> list[dict[str, Any]]:
    """Return all background tasks (running and done) for a parent session."""
    with _lock:
        return list(_BACKGROUND_TASKS.get(parent_sid, []))


def cleanup_btw(parent_sid: str) -> dict[str, Any] | None:
    """Remove and return btw tracking for a parent session."""
    with _lock:
        return _BTW_TRACKING.pop(parent_sid, None)
