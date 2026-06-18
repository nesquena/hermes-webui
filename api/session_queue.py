"""Backend-owned queued follow-up turns for WebUI sessions.

The browser may optimistically render queue chips, but once this module
acknowledges an item the backend owns dispatch.  The invariant is:

    acknowledged queued follow-ups drain into their original session after the
    active run settles, even if no browser tab is connected.

Storage is deliberately small and local: one JSON file per session under the
WebUI session directory.  This keeps the first implementation profile/state-dir
local without introducing a database migration.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_SAFE_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _queue_dir() -> Path:
    from api.config import SESSION_DIR

    path = Path(SESSION_DIR) / "_session_queue"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _queue_path(session_id: str) -> Path:
    safe = _SAFE_SESSION_ID_RE.sub("_", str(session_id or "").strip())
    if not safe:
        safe = "unknown"
    return _queue_dir() / f"{safe}.json"


def _read_items_unlocked(session_id: str) -> list[dict[str, Any]]:
    path = _queue_path(session_id)
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning("Failed to read session queue for %s", session_id, exc_info=True)
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _write_items_unlocked(session_id: str, items: list[dict[str, Any]]) -> None:
    path = _queue_path(session_id)
    if not items:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    payload = json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _normalize_attachments(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:20]:
        if isinstance(item, dict):
            att: dict[str, Any] = {}
            for key in ("name", "filename", "path", "mime"):
                value = item.get(key)
                if value not in (None, ""):
                    att[key] = str(value)
            size = item.get("size")
            if isinstance(size, int):
                att["size"] = size
            is_image = item.get("is_image")
            if isinstance(is_image, bool):
                att["is_image"] = is_image
            if att:
                out.append(att)
        else:
            value = str(item or "").strip()
            if value:
                out.append({"name": value})
    return out


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "session_id": str(item.get("session_id") or ""),
        "text": str(item.get("text") or ""),
        "attachments": list(item.get("attachments") or []),
        "model": str(item.get("model") or ""),
        "model_provider": item.get("model_provider"),
        "profile": str(item.get("profile") or ""),
        "created_at": float(item.get("created_at") or 0.0),
    }


def list_queue(session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        return [_public_item(item) for item in _read_items_unlocked(session_id)]


def enqueue(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or payload.get("message") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    if not text and not payload.get("attachments") and not payload.get("files"):
        raise ValueError("text is required")
    item = {
        "id": uuid.uuid4().hex,
        "session_id": str(session_id),
        "text": text,
        "attachments": _normalize_attachments(payload.get("attachments") or payload.get("files") or []),
        "model": str(payload.get("model") or ""),
        "model_provider": payload.get("model_provider"),
        "profile": str(payload.get("profile") or ""),
        "created_at": time.time(),
    }
    with _LOCK:
        items = _read_items_unlocked(session_id)
        items.append(item)
        _write_items_unlocked(session_id, items)
    return _public_item(item)


def update_item(session_id: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    if not session_id or not item_id:
        return None
    with _LOCK:
        items = _read_items_unlocked(session_id)
        for item in items:
            if str(item.get("id") or "") != str(item_id):
                continue
            if "text" in patch:
                text = str(patch.get("text") or "").strip()
                if text:
                    item["text"] = text
            if "model" in patch:
                item["model"] = str(patch.get("model") or "")
            if "model_provider" in patch:
                item["model_provider"] = patch.get("model_provider")
            _write_items_unlocked(session_id, items)
            return _public_item(item)
    return None


def delete_item(session_id: str, item_id: str) -> bool:
    if not session_id or not item_id:
        return False
    with _LOCK:
        items = _read_items_unlocked(session_id)
        kept = [item for item in items if str(item.get("id") or "") != str(item_id)]
        if len(kept) == len(items):
            return False
        _write_items_unlocked(session_id, kept)
        return True


def claim_next(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with _LOCK:
        items = _read_items_unlocked(session_id)
        if not items:
            return None
        item = items.pop(0)
        _write_items_unlocked(session_id, items)
        return dict(item)


def requeue_front(session_id: str, item: dict[str, Any]) -> None:
    if not session_id or not item:
        return
    with _LOCK:
        items = _read_items_unlocked(session_id)
        # Preserve exact-once-ish behavior: do not duplicate an item id already
        # requeued by a racing 409 handler.
        item_id = str(item.get("id") or "")
        if item_id and any(str(existing.get("id") or "") == item_id for existing in items):
            return
        items.insert(0, dict(item))
        _write_items_unlocked(session_id, items)


def _session_has_active_turn(session_id: str) -> bool:
    try:
        from api import config as _cfg

        with _cfg.ACTIVE_RUNS_LOCK:
            for _stream_id, meta in (_cfg.ACTIVE_RUNS or {}).items():
                if isinstance(meta, dict) and meta.get("session_id") == session_id:
                    return True
    except Exception:
        logger.debug("ACTIVE_RUNS queue active-turn check failed", exc_info=True)
    return False


def drain_for_session(session_id: str) -> int:
    """Start at most one queued follow-up turn for an idle session.

    Called from the streaming teardown hook after ``unregister_active_run``.
    It claims one item atomically, starts it on a daemon thread, and requeues the
    item if the existing chat-start guard reports a 409 race.
    """
    if not session_id:
        return 0
    if _session_has_active_turn(session_id):
        return 0
    item = claim_next(session_id)
    if not item:
        return 0

    def _runner() -> None:
        try:
            from api.routes import start_session_turn

            resp = start_session_turn(
                session_id,
                str(item.get("text") or ""),
                source="queued_followup",
                attachments=list(item.get("attachments") or []),
                requested_model=str(item.get("model") or "") or None,
                requested_provider=item.get("model_provider"),
                queue_item_id=str(item.get("id") or "") or None,
            )
            status = int((resp or {}).get("_status", 200) or 200)
            if status == 409:
                requeue_front(session_id, item)
            elif status >= 400:
                # Keep user intent instead of dropping it on transient or
                # configuration errors; the browser can still show/edit/delete
                # it after reconnect.
                requeue_front(session_id, item)
                logger.warning(
                    "queued follow-up failed for session %s: status=%s err=%r",
                    session_id,
                    status,
                    (resp or {}).get("error"),
                )
            else:
                logger.info(
                    "queued follow-up turn started for session %s item=%s stream_id=%s",
                    session_id,
                    item.get("id"),
                    (resp or {}).get("stream_id"),
                )
        except Exception:
            requeue_front(session_id, item)
            logger.warning("queued follow-up turn raised for session %s", session_id, exc_info=True)

    threading.Thread(
        target=_runner,
        name=f"hermes-webui-queued-followup-{str(session_id)[:8]}",
        daemon=True,
    ).start()
    return 1
