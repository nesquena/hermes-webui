"""Lightweight per-session runtime state persistence.

Pending user turns and stream ownership are short-lived runtime metadata. They
must survive a process restart for recovery, but they should not force a full
transcript rewrite on every chat submission.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_RUNTIME_DIR_NAME = "_runtime"
_RUNTIME_LOCKS: dict[str, Any] = {}
_RUNTIME_LOCKS_GUARD = threading.Lock()
_RUNTIME_FIELDS = (
    "active_stream_id",
    "pending_user_message",
    "pending_attachments",
    "pending_started_at",
    "pending_user_source",
    "workspace",
    "model",
    "model_provider",
    "title",
)


def _session_dir() -> Path:
    from api import models

    return Path(models.SESSION_DIR)


def _is_safe_session_id(session_id: object) -> bool:
    from api import models

    return models.is_safe_session_id(session_id)


def _sidecar_is_retired(session_id: str) -> bool:
    try:
        from api import models

        return session_id in models._load_webui_deleted_session_tombstone()
    except Exception:
        return False


def runtime_state_path(session_id: str) -> Path:
    if not _is_safe_session_id(session_id):
        raise ValueError(f"Unsafe session_id {session_id!r}")
    return _session_dir() / _RUNTIME_DIR_NAME / f"{session_id}.json"


def _lock_for(session_id: str) -> Any:
    with _RUNTIME_LOCKS_GUARD:
        return _RUNTIME_LOCKS.setdefault(session_id, threading.RLock())


@contextmanager
def runtime_state_lock(session_id: str) -> Iterator[None]:
    lock = _lock_for(session_id)
    with lock:
        yield


def _normalize_state(state: object) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    normalized: dict[str, Any] = {}
    for field in _RUNTIME_FIELDS:
        if field not in state:
            continue
        value = state[field]
        if field in {"active_stream_id", "pending_user_source", "workspace", "model", "model_provider", "title"}:
            normalized[field] = None if value is None else str(value)
        elif field == "pending_user_message":
            normalized[field] = None if value is None else str(value)
        elif field == "pending_started_at":
            try:
                normalized[field] = None if value is None else float(value)
            except (TypeError, ValueError):
                normalized[field] = None
        elif field == "pending_attachments":
            normalized[field] = copy.deepcopy(value) if isinstance(value, list) else []
    return normalized


def load_runtime_state(session_id: str) -> dict[str, Any]:
    if _sidecar_is_retired(session_id):
        return {}
    with runtime_state_lock(session_id):
        path = runtime_state_path(session_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _normalize_state(raw)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Ignoring unreadable runtime state sidecar for %s", session_id)
            return {}


def save_runtime_state(session_id: str, state: object) -> dict[str, Any]:
    with runtime_state_lock(session_id):
        path = runtime_state_path(session_id)
        payload = _normalize_state(state)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.current_thread().ident}")
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return copy.deepcopy(payload)


def clear_runtime_state(session_id: str) -> bool:
    """Remove runtime state, returning False when unlink fails."""
    with runtime_state_lock(session_id):
        path = runtime_state_path(session_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to clear runtime state sidecar for %s", session_id, exc_info=True)
            return False
        # A load can have overlaid the sidecar immediately before terminal
        # cleanup. Clear only transient fields on any cached projection so the
        # cache cannot retain a deleted pending turn.
        try:
            from api import models

            with models.LOCK:
                cached = models.SESSIONS.get(session_id)
                if cached is not None:
                    cached.active_stream_id = None
                    cached.pending_user_message = None
                    cached.pending_attachments = []
                    cached.pending_started_at = None
                    cached.pending_user_source = None
        except Exception:
            logger.debug("Failed to invalidate cached runtime state for %s", session_id, exc_info=True)
    return True


def runtime_state_from_session(session) -> dict[str, Any]:
    return _normalize_state(
        {
            field: getattr(session, field, None)
            for field in _RUNTIME_FIELDS
        }
    )
