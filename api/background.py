"""Background and ephemeral task tracking for /background and /btw commands."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from api.config import SESSION_DIR
from api.models import is_safe_session_id

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_DURABLE_SCHEMA_VERSION = 1
_PROMPT_PREVIEW_LIMIT = 500

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
    }


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
        })


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
        })


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
