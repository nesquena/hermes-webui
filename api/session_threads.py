"""Metadata-only conversation thread helpers for Hermes WebUI.

Conversation threads group multiple WebUI sessions into one ordered human
workstream. The thread store intentionally persists display/status metadata only;
member sessions keep their own transcripts in normal session files.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

from api.config import SESSION_THREADS_FILE

VALID_THREAD_STATUSES = {"active", "later", "blocked", "archived"}
_THREAD_STORE_LOCK = threading.RLock()
_FORBIDDEN_THREAD_KEYS = {
    "messages",
    "transcript",
    "context_messages",
    "tool_calls",
}


def _now() -> float:
    return time.time()


def _clean_text(value, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _clean_thread(thread: dict) -> dict:
    if not isinstance(thread, dict):
        return {}
    cleaned = {k: v for k, v in thread.items() if k not in _FORBIDDEN_THREAD_KEYS}
    status = cleaned.get("status") or "active"
    try:
        status = validate_thread_status(status)
    except ValueError:
        status = "active"
    cleaned["status"] = status
    cleaned["thread_id"] = _clean_text(cleaned.get("thread_id"), limit=64) or uuid.uuid4().hex[:12]
    cleaned["title"] = _clean_text(cleaned.get("title"), limit=120) or "Untitled thread"
    cleaned["profile"] = _clean_text(cleaned.get("profile"), limit=120) or "default"
    cleaned["project_id"] = _clean_text(cleaned.get("project_id"), limit=120)
    cleaned["root_session_id"] = _clean_text(cleaned.get("root_session_id"), limit=120)
    cleaned["latest_session_id"] = _clean_text(cleaned.get("latest_session_id"), limit=120) or cleaned.get("root_session_id")
    try:
        cleaned["created_at"] = float(cleaned.get("created_at") or _now())
    except (TypeError, ValueError):
        cleaned["created_at"] = _now()
    try:
        cleaned["updated_at"] = float(cleaned.get("updated_at") or cleaned["created_at"])
    except (TypeError, ValueError):
        cleaned["updated_at"] = cleaned["created_at"]
    for key, limit in (("note", 1000), ("next_action", 500), ("color", 64)):
        value = _clean_text(cleaned.get(key), limit=limit)
        if value is None:
            cleaned.pop(key, None)
        else:
            cleaned[key] = value
    return cleaned


def validate_thread_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized not in VALID_THREAD_STATUSES:
        raise ValueError("Thread status must be active/later/blocked/archived")
    return normalized


def load_threads() -> list[dict]:
    with _THREAD_STORE_LOCK:
        if not SESSION_THREADS_FILE.exists():
            return []
        try:
            data = json.loads(SESSION_THREADS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    if isinstance(data, dict):
        raw_threads = data.get("threads", [])
    else:
        raw_threads = data
    if not isinstance(raw_threads, list):
        return []
    threads = []
    seen = set()
    for raw in raw_threads:
        thread = _clean_thread(raw)
        tid = thread.get("thread_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        threads.append(thread)
    return threads


def save_threads(threads: Iterable[dict]) -> None:
    clean_threads = [_clean_thread(thread) for thread in threads if isinstance(thread, dict)]
    payload = json.dumps(clean_threads, ensure_ascii=False, indent=2)
    SESSION_THREADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_THREADS_FILE.with_suffix(
        f".tmp.{os.getpid()}.{threading.current_thread().ident}"
    )
    with _THREAD_STORE_LOCK:
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, SESSION_THREADS_FILE)
        finally:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass


def find_thread(threads: Iterable[dict], thread_id: str) -> dict | None:
    for thread in threads:
        if isinstance(thread, dict) and thread.get("thread_id") == thread_id:
            return thread
    return None


def create_thread_for_session(
    session,
    *,
    title: str | None = None,
    project_id: str | None = None,
    now: float | None = None,
    thread_id: str | None = None,
) -> dict:
    timestamp = _now() if now is None else float(now)
    tid = thread_id or uuid.uuid4().hex[:12]
    session.thread_id = tid
    session.thread_root_session_id = session.session_id
    session.thread_prev_session_id = None
    session.thread_sequence = 1
    session.thread_link_type = "manual_link"
    session.thread_linked_at = timestamp
    return _clean_thread({
        "thread_id": tid,
        "title": title or getattr(session, "title", None) or "Untitled thread",
        "profile": getattr(session, "profile", None) or "default",
        "project_id": project_id if project_id is not None else getattr(session, "project_id", None),
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "root_session_id": getattr(session, "session_id", None),
        "latest_session_id": getattr(session, "session_id", None),
    })


def _sequence_value(row: dict) -> int:
    try:
        value = int(row.get("thread_sequence") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value >= 0 else 0


def _sort_session_rows(rows: Iterable[dict]) -> list[dict]:
    return sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: (
            _sequence_value(row) or 10**9,
            float(row.get("created_at") or row.get("updated_at") or 0),
            str(row.get("session_id") or ""),
        ),
    )


def _safe_session_row(row: dict) -> dict:
    allowed = (
        "session_id",
        "title",
        "workspace",
        "model",
        "model_provider",
        "message_count",
        "created_at",
        "updated_at",
        "last_message_at",
        "project_id",
        "profile",
        "thread_id",
        "thread_root_session_id",
        "thread_prev_session_id",
        "thread_sequence",
        "thread_link_type",
        "thread_linked_at",
    )
    return {key: row.get(key) for key in allowed if key in row}


def _rows_for_thread(session_rows: Iterable[dict], thread_id: str) -> list[dict]:
    return _sort_session_rows(
        row for row in session_rows
        if isinstance(row, dict) and row.get("thread_id") == thread_id
    )


def summarize_threads(threads: Iterable[dict], session_rows: Iterable[dict]) -> list[dict]:
    rows = list(session_rows or [])
    summaries = []
    for raw_thread in threads or []:
        thread = _clean_thread(raw_thread)
        tid = thread.get("thread_id")
        member_rows = _rows_for_thread(rows, tid)
        safe_rows = [_safe_session_row(row) for row in member_rows]
        session_count = len(safe_rows)
        message_count = 0
        latest_activity = thread.get("updated_at")
        latest_session_id = thread.get("latest_session_id")
        if safe_rows:
            for row in safe_rows:
                try:
                    message_count += int(row.get("message_count") or 0)
                except (TypeError, ValueError):
                    pass
            latest_activity = max(
                float(row.get("last_message_at") or row.get("updated_at") or 0)
                for row in safe_rows
            )
            latest_session_id = safe_rows[-1].get("session_id")
        summaries.append({
            **thread,
            "session_count": session_count,
            "message_count": message_count,
            "latest_session_id": latest_session_id,
            "latest_activity": latest_activity,
            "sessions_preview": safe_rows,
        })
    summaries.sort(key=lambda item: float(item.get("latest_activity") or item.get("updated_at") or 0), reverse=True)
    return summaries


def append_session_to_thread(
    session,
    thread: dict,
    *,
    prev_session_id: str | None = None,
    session_rows: Iterable[dict] | None = None,
    now: float | None = None,
    link_type: str = "manual_continue",
) -> dict:
    timestamp = _now() if now is None else float(now)
    tid = thread["thread_id"]
    rows = _rows_for_thread(session_rows or [], tid)
    next_sequence = max([_sequence_value(row) for row in rows] or [0]) + 1
    session.thread_id = tid
    session.thread_root_session_id = thread.get("root_session_id") or prev_session_id or session.session_id
    session.thread_prev_session_id = prev_session_id
    session.thread_sequence = next_sequence
    session.thread_link_type = link_type
    session.thread_linked_at = timestamp
    thread["latest_session_id"] = session.session_id
    thread["updated_at"] = timestamp
    if not thread.get("root_session_id"):
        thread["root_session_id"] = session.thread_root_session_id
    return thread


def _float_time(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _session_chronology_key(session) -> tuple[float, int, str]:
    return (
        _float_time(
            getattr(session, "created_at", None)
            or getattr(session, "last_message_at", None)
            or getattr(session, "updated_at", None)
            or getattr(session, "thread_linked_at", None)
        ),
        getattr(session, "thread_sequence", None) or 10**9,
        getattr(session, "session_id", ""),
    )


def resequence_sessions(member_sessions: Iterable) -> list:
    ordered = sorted(
        [session for session in member_sessions if getattr(session, "thread_id", None)],
        key=_session_chronology_key,
    )
    prev_id = None
    root_id = ordered[0].session_id if ordered else None
    for idx, session in enumerate(ordered, start=1):
        session.thread_root_session_id = root_id
        session.thread_prev_session_id = prev_id
        session.thread_sequence = idx
        prev_id = session.session_id
    return ordered


def update_thread(thread: dict, **fields) -> dict:
    if "status" in fields and fields["status"] is not None:
        thread["status"] = validate_thread_status(fields["status"])
    for key in ("title", "project_id", "note", "next_action", "color"):
        if key in fields:
            value = fields[key]
            if value is None:
                thread.pop(key, None)
            else:
                limit = 120 if key in {"title", "project_id"} else 1000
                thread[key] = _clean_text(value, limit=limit)
    thread["updated_at"] = _now()
    return _clean_thread(thread)


def export_thread_manifest(thread: dict, session_rows: Iterable[dict], *, include_messages: bool = False, sessions_by_id: dict | None = None) -> dict:
    rows = [_safe_session_row(row) for row in _rows_for_thread(session_rows, thread.get("thread_id"))]
    payload = {
        "thread": _clean_thread(thread),
        "include_messages": bool(include_messages),
        "sessions": rows,
    }
    if include_messages:
        scoped = []
        sessions_by_id = sessions_by_id or {}
        for row in rows:
            sid = row.get("session_id")
            session = sessions_by_id.get(sid)
            scoped.append({
                **row,
                "messages": getattr(session, "messages", []) if session is not None else [],
            })
        payload["sessions"] = scoped
    else:
        lines = [f"# Conversation Thread: {payload['thread'].get('title') or 'Untitled thread'}", ""]
        lines.append(f"Status: {payload['thread'].get('status') or 'active'}")
        if payload["thread"].get("project_id"):
            lines.append(f"Project: {payload['thread']['project_id']}")
        lines.append(f"Sessions: {len(rows)}")
        lines.append("")
        lines.append("## Sessions")
        for idx, row in enumerate(rows, start=1):
            title = row.get("title") or "Untitled"
            count = row.get("message_count") or 0
            lines.append(f"{idx}. {title} — {row.get('session_id')} — {count} messages")
        note = payload["thread"].get("note")
        next_action = payload["thread"].get("next_action")
        if note or next_action:
            lines.append("")
            lines.append("## Notes")
            if note:
                lines.append(str(note))
            if next_action:
                lines.append(f"Next action: {next_action}")
        payload["markdown"] = "\n".join(lines)
    return payload
