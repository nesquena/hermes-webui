"""Bounded runtime state for delegated child sessions."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from api.config import STATE_DIR

_LOCK = threading.RLock()
_LIVE: dict[tuple[str, str], dict[str, Any]] = {}
_TERMINAL: dict[tuple[str, str], dict[str, Any]] | None = None
_STORE = STATE_DIR / "delegated_child_runtime.json"
_TERMINAL_STATES = {"completed", "failed", "cancelled", "unknown"}
_VISIBLE_STATES = _TERMINAL_STATES | {"running"}
_EVENTS = {"subagent.start", "subagent.tool", "subagent.progress", "subagent.complete"}
_TERMINAL_TTL_SECONDS = 15 * 60
_MAX_TERMINAL_RECORDS = 256


def _key(profile: Any, child_session_id: Any) -> tuple[str, str] | None:
    profile_value = str(profile or "").strip()
    child_value = str(child_session_id or "").strip()
    return (profile_value, child_value) if child_value else None


def _visible_runtime(record: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(record, dict):
        return {"runtime_state": "unknown", "runtime_reason": ""}
    state = str(record.get("runtime_state") or "")
    reason = str(record.get("runtime_reason") or "")
    if state not in _VISIBLE_STATES:
        return {"runtime_state": "unknown", "runtime_reason": ""}
    return {"runtime_state": state, "runtime_reason": reason}


def _coerce_timestamp(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prune_live_records(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    changed = False
    expired = [
        key
        for key, record in _LIVE.items()
        if now - (_coerce_timestamp(record.get("updated_at")) or 0) >= _TERMINAL_TTL_SECONDS
    ]
    for key in expired:
        _LIVE.pop(key, None)
        changed = True
    if len(_LIVE) <= _MAX_TERMINAL_RECORDS:
        return changed
    for key, _record in sorted(
        _LIVE.items(),
        key=lambda item: _coerce_timestamp(item[1].get("updated_at")) or 0,
    )[: len(_LIVE) - _MAX_TERMINAL_RECORDS]:
        _LIVE.pop(key, None)
        changed = True
    return changed


def _prune_terminal_records(records: dict[tuple[str, str], dict[str, Any]]) -> bool:
    now = time.time()
    changed = False
    expired = [
        key
        for key, record in records.items()
        if now - (_coerce_timestamp(record.get("updated_at")) or 0) >= _TERMINAL_TTL_SECONDS
    ]
    for key in expired:
        records.pop(key, None)
        changed = True
    if len(records) <= _MAX_TERMINAL_RECORDS:
        return changed
    for key, _record in sorted(
        records.items(),
        key=lambda item: _coerce_timestamp(item[1].get("updated_at")) or 0,
    )[: len(records) - _MAX_TERMINAL_RECORDS]:
        records.pop(key, None)
        changed = True
    return changed


def _load_terminal() -> dict[tuple[str, str], dict[str, Any]]:
    global _TERMINAL
    if _TERMINAL is not None:
        return _TERMINAL
    try:
        raw = json.loads(_STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    records = raw.get("records", {}) if isinstance(raw, dict) else {}
    _TERMINAL = {}
    dirty = False
    if isinstance(records, dict):
        for encoded, record in records.items():
            if not isinstance(record, dict) or not isinstance(encoded, str):
                dirty = True
                continue
            profile, separator, child_session_id = encoded.partition("\0")
            state = record.get("runtime_state")
            updated_at = _coerce_timestamp(record.get("updated_at"))
            if (
                separator
                and child_session_id
                and isinstance(state, str)
                and state in _TERMINAL_STATES
                and updated_at is not None
            ):
                _TERMINAL[(profile, child_session_id)] = {
                    "runtime_state": state,
                    "runtime_reason": str(record.get("runtime_reason") or ""),
                    "updated_at": updated_at,
                    "owner_session_id": str(record.get("owner_session_id") or ""),
                }
            else:
                dirty = True
    dirty = _prune_terminal_records(_TERMINAL) or dirty
    if dirty:
        _persist_terminal()
    return _TERMINAL


def _prune_cached_terminal_records() -> dict[tuple[str, str], dict[str, Any]]:
    records = _load_terminal()
    if _prune_terminal_records(records):
        _persist_terminal()
    return records


def _persist_terminal() -> None:
    records = {
        f"{profile}\0{child_session_id}": {
            "runtime_state": str(record.get("runtime_state") or ""),
            "runtime_reason": str(record.get("runtime_reason") or ""),
            "updated_at": float(record.get("updated_at") or 0),
            "owner_session_id": str(record.get("owner_session_id") or ""),
        }
        for (profile, child_session_id), record in (_TERMINAL or {}).items()
    }
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    temp = _STORE.with_name(f".{_STORE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "records": records}, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, _STORE)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def record_subagent_event(
    profile: Any,
    event_type: Any,
    payload: dict[str, Any] | None,
    *,
    owner_session_id: Any | None = None,
) -> bool:
    """Record only authoritative, bounded child lifecycle state.

    Returns whether the visible state changed, so callers can invalidate the
    existing sidebar snapshot only when the badge needs repainting.
    """
    event = str(event_type or "").strip()
    if event not in _EVENTS or not isinstance(payload, dict):
        return False
    key = _key(profile, payload.get("child_session_id"))
    if key is None:
        return False
    owner_value = str(owner_session_id or "").strip()
    now = time.time()
    if event == "subagent.complete":
        status = str(payload.get("status") or "").strip().lower()
        state = {
            "ok": "completed",
            "completed": "completed",
            "error": "failed",
            "failed": "failed",
            "timeout": "failed",
            "interrupted": "cancelled",
        }.get(status, "unknown")
        record = {
            "runtime_state": state,
            "runtime_reason": status,
            "updated_at": now,
            "terminal": True,
            "owner_session_id": owner_value,
        }
    else:
        record = {
            "runtime_state": "running",
            "runtime_reason": event,
            "updated_at": now,
            "terminal": False,
            "owner_session_id": owner_value,
        }
    with _LOCK:
        _prune_live_records(now)
        terminal_records = _prune_cached_terminal_records()
        terminal = _LIVE.get(key) or terminal_records.get(key)
        if terminal and terminal.get("terminal", terminal.get("runtime_state") in _TERMINAL_STATES):
            return False
        previous = _LIVE.get(key)
        _LIVE[key] = record
        _prune_live_records(now)
        if record["terminal"]:
            terminal_records[key] = {
                "runtime_state": record["runtime_state"],
                "runtime_reason": record["runtime_reason"],
                "updated_at": now,
                "owner_session_id": owner_value,
                "terminal": True,
            }
            _prune_terminal_records(terminal_records)
            _persist_terminal()
        return _visible_runtime(previous)["runtime_state"] != record["runtime_state"]


def child_runtime(profile: Any, child_session_id: Any) -> dict[str, str]:
    key = _key(profile, child_session_id)
    if key is None:
        return {"runtime_state": "unknown", "runtime_reason": ""}
    with _LOCK:
        _prune_live_records()
        record = _LIVE.get(key) or _prune_cached_terminal_records().get(key)
        return _visible_runtime(record)


def forget_runtime_owner(owner_session_id: Any, *, profile: Any | None = None) -> bool:
    owner_value = str(owner_session_id or "").strip()
    if not owner_value:
        return False
    profile_value = str(profile or "").strip()
    with _LOCK:
        _prune_live_records()
        terminal_records = _prune_cached_terminal_records()
        keys = [
            key
            for key in {
                *_LIVE.keys(),
                *terminal_records.keys(),
            }
            if (
                str((_LIVE.get(key) or terminal_records.get(key) or {}).get("owner_session_id") or "")
                == owner_value
                and (not profile_value or key[0] == profile_value)
            )
        ]
        if not keys:
            return False
        changed = False
        for key in keys:
            changed = _LIVE.pop(key, None) is not None or changed
            changed = terminal_records.pop(key, None) is not None or changed
        if changed:
            _persist_terminal()
        return changed


def clear_live_runtime() -> None:
    """Clear the process projection while retaining persisted terminal state."""
    with _LOCK:
        _LIVE.clear()


def reset_runtime_for_tests() -> None:
    global _TERMINAL
    with _LOCK:
        _LIVE.clear()
        _TERMINAL = {}
