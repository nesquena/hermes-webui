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
_MAX_QUEUE_ITEMS = 50
_MAX_START_RETRIES = 3


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


def _normalize_model_provider(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": str(item.get("id") or ""),
        "session_id": str(item.get("session_id") or ""),
        "text": str(item.get("text") or ""),
        "attachments": list(item.get("attachments") or []),
        "model": str(item.get("model") or ""),
        "model_provider": _normalize_model_provider(item.get("model_provider")),
        "profile": str(item.get("profile") or ""),
        "created_at": float(item.get("created_at") or 0.0),
    }
    if item.get("blocked"):
        out["blocked"] = True
    if item.get("error"):
        out["error"] = str(item.get("error") or "")
    return out


def list_queue(session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        return [_public_item(item) for item in _read_items_unlocked(session_id)]


def enqueue(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or payload.get("message") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    if not text:
        raise ValueError("text is required")
    item = {
        "id": uuid.uuid4().hex,
        "session_id": str(session_id),
        "text": text,
        "attachments": _normalize_attachments(payload.get("attachments") or payload.get("files") or []),
        "model": str(payload.get("model") or ""),
        "model_provider": _normalize_model_provider(payload.get("model_provider")),
        "profile": str(payload.get("profile") or ""),
        "created_at": time.time(),
    }
    with _LOCK:
        items = _read_items_unlocked(session_id)
        if len(items) >= _MAX_QUEUE_ITEMS:
            items = items[-(_MAX_QUEUE_ITEMS - 1) :]
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
                item["model_provider"] = _normalize_model_provider(patch.get("model_provider"))
            if any(key in patch for key in ("text", "model", "model_provider")):
                item.pop("blocked", None)
                item.pop("error", None)
                item.pop("retry_count", None)
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


def claim_item(session_id: str, item_id: str) -> dict[str, Any] | None:
    """Remove one exact queued item and return a reversible claim receipt."""
    if not session_id or not item_id:
        return None
    with _LOCK:
        items = _read_items_unlocked(session_id)
        for index, item in enumerate(items):
            if str(item.get("id") or "") != str(item_id):
                continue
            claimed = items.pop(index)
            _write_items_unlocked(session_id, items)
            return {"item": dict(claimed), "index": index}
    return None


def restore_claim(session_id: str, claim: dict[str, Any] | None) -> None:
    """Restore a failed queue claim at its original position without duplication."""
    if not session_id or not isinstance(claim, dict):
        return
    item = claim.get("item")
    if not isinstance(item, dict):
        return
    item_id = str(item.get("id") or "")
    with _LOCK:
        items = _read_items_unlocked(session_id)
        if item_id and any(str(existing.get("id") or "") == item_id for existing in items):
            return
        try:
            index = int(claim.get("index", len(items)))
        except (TypeError, ValueError):
            index = len(items)
        items.insert(max(0, min(index, len(items))), dict(item))
        _write_items_unlocked(session_id, items)


def steer_text_for_item(item: dict[str, Any]) -> str:
    """Build active-run guidance from the backend-authoritative queued payload."""
    text = str((item or {}).get("text") or "").strip()
    paths = []
    for attachment in (item or {}).get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        path = str(attachment.get("path") or "").strip()
        if path:
            paths.append(path)
    if not paths:
        return text
    note = (
        f"[Attached files for this steer: {', '.join(paths)}]\n"
        "Use the file tools/read_file to inspect these documents if needed."
    )
    return f"{text}\n\n{note}" if text else note


def claim_next(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with _LOCK:
        items = _read_items_unlocked(session_id)
        if not items:
            return None
        idx = next((i for i, existing in enumerate(items) if not existing.get("blocked")), -1)
        if idx < 0:
            return None
        item = items.pop(idx)
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
                retry_count = int(item.get("retry_count") or 0) + 1
                item["retry_count"] = retry_count
                item["error"] = str((resp or {}).get("error") or f"start_failed_{status}")
                if status < 500 and retry_count >= _MAX_START_RETRIES:
                    item["blocked"] = True
                # Keep user intent instead of dropping it on transient or
                # configuration errors; blocked items remain editable/deletable
                # but claim_next will not churn on them every teardown.
                requeue_front(session_id, item)
                logger.warning(
                    "queued follow-up failed for session %s: status=%s retries=%s blocked=%s err=%r",
                    session_id,
                    status,
                    retry_count,
                    bool(item.get("blocked")),
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
