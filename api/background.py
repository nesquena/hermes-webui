"""Background and ephemeral task tracking for /background and /btw commands."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# parent_session_id -> list of task dicts
_BACKGROUND_TASKS: dict[str, list[dict[str, Any]]] = {}

# btw ephemeral session tracking: parent_sid -> {ephemeral_sid, stream_id, question}
_BTW_TRACKING: dict[str, dict[str, Any]] = {}


def track_background(parent_sid: str, bg_sid: str, stream_id: str,
                     task_id: str, prompt: str) -> None:
    with _lock:
        _BACKGROUND_TASKS.setdefault(parent_sid, []).append({
            "task_id": task_id,
            "bg_session_id": bg_sid,
            "stream_id": stream_id,
            "prompt": prompt,
            "status": "running",
            "started_at": time.time(),
            "answer": None,
            "completed_at": None,
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
    with _lock:
        for t in _BACKGROUND_TASKS.get(parent_sid, []):
            if t["task_id"] == task_id and t["status"] == "running":
                t["status"] = "done"
                t["answer"] = answer
                t["completed_at"] = time.time()
                break


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
