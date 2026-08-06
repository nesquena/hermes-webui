"""Lightweight persistence for WebUI composer drafts.

Composer drafts are UI metadata, not transcript content. Keep them in small
per-session sidecars so typing in a large conversation never rewrites the full
session JSON transcript.
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

_DRAFT_DIR_NAME = "_drafts"
_DRAFT_LOCKS: dict[str, threading.Lock] = {}
_DRAFT_LOCKS_GUARD = threading.Lock()


def _session_dir() -> Path:
    # Import lazily to avoid an api.models ↔ api.draft_store import cycle during
    # application startup. Tests can monkeypatch models.SESSION_DIR normally.
    from api import models

    return Path(models.SESSION_DIR)


def _is_safe_session_id(session_id: object) -> bool:
    from api import models

    return models.is_safe_session_id(session_id)


def draft_path(session_id: str) -> Path:
    """Return the isolated draft sidecar path for a safe session id."""
    if not _is_safe_session_id(session_id):
        raise ValueError(f"Unsafe session_id {session_id!r}")
    return _session_dir() / _DRAFT_DIR_NAME / f"{session_id}.json"


def _normalize_draft(draft: object) -> dict[str, Any]:
    if not isinstance(draft, dict):
        return {"text": "", "files": []}
    text = draft.get("text", "")
    files = draft.get("files", [])
    if not isinstance(text, str):
        text = ""
    if not isinstance(files, list):
        files = []
    return {"text": text, "files": copy.deepcopy(files)}


def _lock_for(session_id: str) -> threading.Lock:
    with _DRAFT_LOCKS_GUARD:
        return _DRAFT_LOCKS.setdefault(session_id, threading.Lock())


@contextmanager
def draft_lock(session_id: str) -> Iterator[None]:
    """Serialize small draft sidecar reads/writes within this WebUI process."""
    lock = _lock_for(session_id)
    with lock:
        yield


def load_draft(session_id: str, *, fallback: object = None) -> dict[str, Any]:
    """Load a draft sidecar, falling back to the legacy embedded value."""
    path = draft_path(session_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return _normalize_draft(raw)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Ignoring unreadable composer draft sidecar for %s", session_id)
    return _normalize_draft(fallback)


def save_draft(session_id: str, draft: object) -> dict[str, Any]:
    """Atomically persist only the small composer-draft payload."""
    path = draft_path(session_id)
    payload = _normalize_draft(draft)
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
