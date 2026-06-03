"""Profile-scoped read state for WebUI session rows.

This module intentionally stores read cursors outside session JSON / the session
index. Marking a conversation as read must not touch ``updated_at`` or reorder
Recents; it is UI state, not transcript state.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Iterable, MutableMapping

from api.config import STATE_DIR

READ_STATE_FILE = STATE_DIR / "session_read_state.json"
_SCHEMA_VERSION = 1
_LOCK = threading.Lock()


def _blank_payload() -> dict:
    return {"version": _SCHEMA_VERSION, "profiles": {}}


def _profile_key(profile: object | None) -> str:
    value = str(profile or "default").strip()
    return value or "default"


def _clean_session_id(session_id: str | None) -> str:
    return str(session_id or "").strip()


def _clean_count(message_count: object) -> int:
    try:
        count = int(message_count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _normalize_row(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    count = _clean_count(value.get("read_message_count", value.get("message_count", 0)))
    try:
        read_at = float(value.get("read_at") or 0)
    except (TypeError, ValueError):
        read_at = 0.0
    return {
        "read_message_count": count,
        "read_at": read_at,
        "manual_unread": bool(value.get("manual_unread", False)),
    }


def _load_unlocked() -> dict:
    try:
        raw = json.loads(READ_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _blank_payload()
    except (OSError, json.JSONDecodeError):
        return _blank_payload()
    if not isinstance(raw, dict):
        return _blank_payload()
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    payload = {"version": _SCHEMA_VERSION, "profiles": {}}
    for profile, sessions in profiles.items():
        if not isinstance(sessions, dict):
            continue
        cleaned: dict[str, dict] = {}
        for sid, row in sessions.items():
            clean_sid = _clean_session_id(sid)
            clean_row = _normalize_row(row)
            if clean_sid and clean_row is not None:
                cleaned[clean_sid] = clean_row
        payload["profiles"][_profile_key(profile)] = cleaned
    return payload


def _atomic_write_unlocked(payload: dict) -> None:
    READ_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = READ_STATE_FILE.with_name(f".{READ_STATE_FILE.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, READ_STATE_FILE)


def get_session_read_state(profile: str | None, session_id: str | None) -> dict | None:
    """Return the stored read cursor for one profile/session, if present."""
    sid = _clean_session_id(session_id)
    if not sid:
        return None
    with _LOCK:
        payload = _load_unlocked()
        row = payload.get("profiles", {}).get(_profile_key(profile), {}).get(sid)
        normalized = _normalize_row(row)
        return dict(normalized) if normalized is not None else None


def mark_session_read(
    profile: str | None,
    session_id: str | None,
    message_count: object = 0,
    *,
    now: float | None = None,
) -> dict:
    """Persist a read cursor and return the stored row.

    The cursor is monotonic: a stale browser cannot reduce the stored
    ``read_message_count`` for another tab/device.
    """
    sid = _clean_session_id(session_id)
    if not sid:
        raise ValueError("session_id is required")
    profile_name = _profile_key(profile)
    next_count = _clean_count(message_count)
    with _LOCK:
        payload = _load_unlocked()
        profiles = payload.setdefault("profiles", {})
        sessions = profiles.setdefault(profile_name, {})
        current = _normalize_row(sessions.get(sid)) or {
            "read_message_count": 0,
            "read_at": 0.0,
            "manual_unread": False,
        }
        stored_count = max(_clean_count(current.get("read_message_count")), next_count)
        row = {
            "read_message_count": stored_count,
            "read_at": float(now if now is not None else time.time()),
            "manual_unread": False,
        }
        sessions[sid] = row
        payload["version"] = _SCHEMA_VERSION
        _atomic_write_unlocked(payload)
        return dict(row)


def mark_session_unread(
    profile: str | None,
    session_id: str | None,
    message_count: object | None = None,
    *,
    now: float | None = None,
) -> dict:
    """Persist a manual unread override for one profile/session."""
    sid = _clean_session_id(session_id)
    if not sid:
        raise ValueError("session_id is required")
    profile_name = _profile_key(profile)
    next_count = None if message_count is None else _clean_count(message_count)
    with _LOCK:
        payload = _load_unlocked()
        profiles = payload.setdefault("profiles", {})
        sessions = profiles.setdefault(profile_name, {})
        current = _normalize_row(sessions.get(sid)) or {
            "read_message_count": next_count or 0,
            "read_at": 0.0,
            "manual_unread": False,
        }
        stored_count = _clean_count(current.get("read_message_count"))
        if next_count is not None:
            stored_count = min(stored_count, next_count) if sessions.get(sid) is not None else next_count
        row = {
            "read_message_count": stored_count,
            "read_at": float(now if now is not None else time.time()),
            "manual_unread": True,
        }
        sessions[sid] = row
        payload["version"] = _SCHEMA_VERSION
        _atomic_write_unlocked(payload)
        return dict(row)


def prune_deleted_sessions(profile: str | None, valid_session_ids: Iterable[str]) -> bool:
    """Remove read cursors for sessions no longer valid in one profile."""
    profile_name = _profile_key(profile)
    valid = {_clean_session_id(sid) for sid in valid_session_ids if _clean_session_id(sid)}
    with _LOCK:
        payload = _load_unlocked()
        sessions = payload.get("profiles", {}).get(profile_name)
        if not isinstance(sessions, dict):
            return False
        before = set(sessions.keys())
        for sid in list(sessions.keys()):
            if sid not in valid:
                sessions.pop(sid, None)
        changed = before != set(sessions.keys())
        if changed:
            _atomic_write_unlocked(payload)
        return changed


def _row_profile(row: MutableMapping[str, object], active_profile: str | None) -> str:
    return _profile_key(row.get("profile") if row.get("profile") else active_profile)


def merge_session_read_state(sessions: list[dict], active_profile: str | None) -> list[dict]:
    """Annotate session-list rows with read cursor metadata.

    Missing stored state is reported as a non-persisted default at the current
    message count so old conversations do not suddenly appear unread on a new
    browser. Once a browser opens or marks the conversation read, the stored row
    becomes authoritative across devices.
    """
    with _LOCK:
        payload = _load_unlocked()
    profiles = payload.get("profiles", {}) if isinstance(payload, dict) else {}
    for item in sessions:
        if not isinstance(item, dict):
            continue
        sid = _clean_session_id(item.get("session_id"))
        profile = _row_profile(item, active_profile)
        stored = None
        if sid:
            stored = _normalize_row(profiles.get(profile, {}).get(sid) if isinstance(profiles.get(profile), dict) else None)
        try:
            current_count = int(item.get("message_count") or 0)
        except (TypeError, ValueError):
            current_count = 0
        if stored is None:
            item["read_state_loaded"] = True
            item["read_state_source"] = "default"
            item["read_message_count"] = max(0, current_count)
            item["read_at"] = None
            item["manual_unread"] = False
        else:
            item["read_state_loaded"] = True
            item["read_state_source"] = "stored"
            item["read_message_count"] = _clean_count(stored.get("read_message_count"))
            item["read_at"] = stored.get("read_at") or None
            item["manual_unread"] = bool(stored.get("manual_unread", False))
    return sessions
