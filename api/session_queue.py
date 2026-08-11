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
_MAX_RECEIPTS = 256
_RETRY_DELAY_SECONDS = 0.25
_DRAINING_SESSIONS: set[str] = set()


class QueueStorageError(RuntimeError):
    """The persisted queue cannot be trusted and must not be overwritten."""


class QueueCapacityError(ValueError):
    """The session queue is full; acknowledged items are never evicted."""


class QueueItemConflictError(RuntimeError):
    """A queue mutation lost ownership because the item already started."""


def _emit_queue_changed(session_id: str) -> None:
    try:
        from api.background_process import get_session_channel

        channel = get_session_channel(session_id)
        if channel is not None:
            channel.emit("session_queue_changed", {"session_id": session_id})
    except Exception:
        logger.debug("Failed to emit queue change for %s", session_id, exc_info=True)


def _queue_dir() -> Path:
    from api.config import SESSION_DIR

    path = Path(SESSION_DIR) / "_session_queue"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _queue_path(session_id: str) -> Path:
    from api.models import is_safe_session_id

    normalized = str(session_id or "").strip()
    if not is_safe_session_id(normalized):
        raise ValueError("session_id is not path-safe")
    return _queue_dir() / f"{normalized}.json"


def _receipt_path(session_id: str) -> Path:
    queue_path = _queue_path(session_id)
    receipt_dir = queue_path.parent / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    return receipt_dir / queue_path.name


def _read_items_unlocked(session_id: str) -> list[dict[str, Any]]:
    path = _queue_path(session_id)
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.error("Corrupt session queue for %s; refusing mutation", session_id, exc_info=True)
        raise QueueStorageError(f"failed to read corrupt session queue for {session_id}") from exc
    if not isinstance(parsed, list):
        raise QueueStorageError(f"corrupt session queue for {session_id}: expected a list")
    items: list[dict[str, Any]] = []
    for raw_item in parsed:
        if not isinstance(raw_item, dict):
            raise QueueStorageError(f"corrupt session queue for {session_id}: invalid item")
        item = dict(raw_item)
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            raise QueueStorageError(f"corrupt session queue for {session_id}: item id missing")
        item["id"] = item_id
        if str(item.get("session_id") or "") != str(session_id):
            raise QueueStorageError(f"queue owner does not match requested session {session_id}")
        item.setdefault("client_queue_id", f"legacy:{item_id}")
        if item.get("blocked") and not item.get("state"):
            item["state"] = "blocked"
        item.setdefault("state", "queued")
        items.append(item)
    return items


def _read_receipts_unlocked(session_id: str) -> list[dict[str, Any]]:
    path = _receipt_path(session_id)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        raise QueueStorageError(f"failed to read corrupt queue receipts for {session_id}") from exc
    if not isinstance(parsed, list):
        raise QueueStorageError(f"corrupt queue receipts for {session_id}: expected a list")
    receipts = []
    for receipt in parsed:
        if not isinstance(receipt, dict) or str(receipt.get("session_id") or "") != session_id:
            raise QueueStorageError(f"corrupt queue receipt owner for {session_id}")
        receipts.append(dict(receipt))
    return receipts


def _fsync_parent(path: Path) -> None:
    """Make a replace/unlink durable across a process or host crash."""
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_items_unlocked(session_id: str, items: list[dict[str, Any]]) -> None:
    path = _queue_path(session_id)
    if not items:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_parent(path)
        return
    payload = json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _fsync_parent(path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _write_receipts_unlocked(session_id: str, receipts: list[dict[str, Any]]) -> None:
    path = _receipt_path(session_id)
    payload = json.dumps(receipts[-_MAX_RECEIPTS:], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _fsync_parent(path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _record_completed_unlocked(session_id: str, completed_items: list[dict[str, Any]]) -> None:
    if not completed_items:
        return
    receipts = _read_receipts_unlocked(session_id)
    by_client_id = {str(receipt.get("client_queue_id") or ""): receipt for receipt in receipts}
    for item in completed_items:
        client_queue_id = str(item.get("client_queue_id") or "")
        receipt = _public_item(item)
        receipt["state"] = "completed"
        receipt["completed_at"] = time.time()
        by_client_id[client_queue_id] = receipt
    ordered = sorted(by_client_id.values(), key=lambda value: float(value.get("completed_at") or 0.0))
    _write_receipts_unlocked(session_id, ordered[-_MAX_RECEIPTS:])


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
        "client_queue_id": str(item.get("client_queue_id") or ""),
        "session_id": str(item.get("session_id") or ""),
        "text": str(item.get("text") or ""),
        "attachments": list(item.get("attachments") or []),
        "model": str(item.get("model") or ""),
        "model_provider": _normalize_model_provider(item.get("model_provider")),
        "profile": str(item.get("profile") or ""),
        "created_at": float(item.get("created_at") or 0.0),
        "state": str(item.get("state") or "queued"),
    }
    if item.get("stream_id"):
        out["stream_id"] = str(item.get("stream_id") or "")
    if item.get("blocked"):
        out["blocked"] = True
    if item.get("error"):
        out["error"] = str(item.get("error") or "")
    if item.get("retry_count") is not None:
        out["retry_count"] = int(item.get("retry_count") or 0)
    return out


def _same_intent(item: dict[str, Any], payload: dict[str, Any], text: str) -> bool:
    return (
        str(item.get("text") or "") == text
        and list(item.get("attachments") or [])
        == _normalize_attachments(payload.get("attachments") or payload.get("files") or [])
        and str(item.get("model") or "") == str(payload.get("model") or "")
        and _normalize_model_provider(item.get("model_provider"))
        == _normalize_model_provider(payload.get("model_provider"))
        and str(item.get("profile") or "") == str(payload.get("profile") or "")
    )


def list_queue(session_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        return [_public_item(item) for item in _read_items_unlocked(session_id)]


def enqueue(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or payload.get("message") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    if not text:
        raise ValueError("text is required")
    client_queue_id = str(payload.get("client_queue_id") or "").strip()
    if not client_queue_id:
        raise ValueError("client_queue_id is required")
    with _LOCK:
        items = _read_items_unlocked(session_id)
        for existing in items:
            if str(existing.get("client_queue_id") or "") != client_queue_id:
                continue
            if not _same_intent(existing, payload, text):
                raise QueueItemConflictError("client_queue_id already owns a different queued item")
            return _public_item(existing)
        for receipt in _read_receipts_unlocked(session_id):
            if str(receipt.get("client_queue_id") or "") != client_queue_id:
                continue
            if not _same_intent(receipt, payload, text):
                raise QueueItemConflictError("client_queue_id already owns a different completed item")
            return _public_item(receipt)
        if len(items) >= _MAX_QUEUE_ITEMS:
            raise QueueCapacityError(f"session queue is at capacity ({_MAX_QUEUE_ITEMS})")
        item = {
            "id": uuid.uuid4().hex,
            "client_queue_id": client_queue_id,
            "session_id": str(session_id),
            "text": text,
            "attachments": _normalize_attachments(payload.get("attachments") or payload.get("files") or []),
            "model": str(payload.get("model") or ""),
            "model_provider": _normalize_model_provider(payload.get("model_provider")),
            "profile": str(payload.get("profile") or ""),
            "created_at": time.time(),
            "state": "queued",
        }
        items.append(item)
        _write_items_unlocked(session_id, items)
        _emit_queue_changed(session_id)
    return _public_item(item)


def update_item(session_id: str, item_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    if not session_id or not item_id:
        return None
    with _LOCK:
        items = _read_items_unlocked(session_id)
        for item in items:
            if str(item.get("id") or "") != str(item_id):
                continue
            if str(item.get("state") or "queued") not in ("queued", "blocked"):
                raise QueueItemConflictError("queued item already started")
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
                item["state"] = "queued"
            _write_items_unlocked(session_id, items)
            _emit_queue_changed(session_id)
            return _public_item(item)
    return None


def delete_item(session_id: str, item_id: str) -> bool:
    if not session_id or not item_id:
        return False
    with _LOCK:
        items = _read_items_unlocked(session_id)
        target = next((item for item in items if str(item.get("id") or "") == str(item_id)), None)
        if target is None:
            return False
        if str(target.get("state") or "queued") not in ("queued", "blocked"):
            raise QueueItemConflictError("queued item already started")
        kept = [item for item in items if str(item.get("id") or "") != str(item_id)]
        _write_items_unlocked(session_id, kept)
        _emit_queue_changed(session_id)
        return True


def _require_mutable_items(items: list[dict[str, Any]]) -> None:
    if any(str(item.get("state") or "queued") not in ("queued", "blocked") for item in items):
        raise QueueItemConflictError("queued item is already starting or started")


def reorder_items(session_id: str, ordered_ids: list[str]) -> list[dict[str, Any]]:
    """Persist an exact authoritative FIFO order in one locked write."""
    normalized = [str(item_id or "").strip() for item_id in ordered_ids]
    if not session_id or not normalized or any(not item_id for item_id in normalized):
        raise ValueError("session_id and ordered_ids are required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("ordered_ids must be unique")
    with _LOCK:
        items = _read_items_unlocked(session_id)
        _require_mutable_items(items)
        by_id = {str(item.get("id") or ""): item for item in items}
        if set(normalized) != set(by_id):
            raise QueueItemConflictError("ordered_ids no longer match the authoritative queue")
        reordered = [by_id[item_id] for item_id in normalized]
        _write_items_unlocked(session_id, reordered)
        _emit_queue_changed(session_id)
        return [_public_item(item) for item in reordered]


def combine_items(session_id: str, ordered_ids: list[str]) -> list[dict[str, Any]]:
    """Combine selected FIFO items without exposing a delete/create gap."""
    normalized = [str(item_id or "").strip() for item_id in ordered_ids]
    if not session_id or len(normalized) < 2 or len(set(normalized)) != len(normalized):
        raise ValueError("at least two unique ordered_ids are required")
    with _LOCK:
        items = _read_items_unlocked(session_id)
        _require_mutable_items(items)
        by_id = {str(item.get("id") or ""): item for item in items}
        if any(item_id not in by_id for item_id in normalized):
            raise QueueItemConflictError("combined items no longer match the authoritative queue")
        selected = [by_id[item_id] for item_id in normalized]
        first = selected[0]
        first["text"] = "\n\n".join(str(item.get("text") or "").strip() for item in selected).strip()
        attachments: list[dict[str, Any]] = []
        for item in selected:
            attachments.extend(_normalize_attachments(item.get("attachments") or []))
        first["attachments"] = attachments
        selected_ids = set(normalized)
        insertion_index = min(index for index, item in enumerate(items) if str(item.get("id") or "") in selected_ids)
        combined = [item for item in items if str(item.get("id") or "") not in selected_ids]
        combined.insert(insertion_index, first)
        _write_items_unlocked(session_id, combined)
        _emit_queue_changed(session_id)
        return [_public_item(item) for item in combined]


def clear_queue(session_id: str) -> int:
    if not session_id:
        return 0
    with _LOCK:
        items = _read_items_unlocked(session_id)
        _require_mutable_items(items)
        _write_items_unlocked(session_id, [])
        _emit_queue_changed(session_id)
        return len(items)


def claim_next(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with _LOCK:
        items = _read_items_unlocked(session_id)
        if not items:
            return None
        item = items[0]
        if str(item.get("state") or "queued") != "queued":
            return None
        item["state"] = "starting"
        item["starting_at"] = time.time()
        _write_items_unlocked(session_id, items)
        _emit_queue_changed(session_id)
        return dict(item)


def requeue_front(session_id: str, item: dict[str, Any]) -> None:
    if not session_id or not item:
        return
    with _LOCK:
        items = _read_items_unlocked(session_id)
        # Preserve exact-once-ish behavior: do not duplicate an item id already
        # requeued by a racing 409 handler.
        item_id = str(item.get("id") or "")
        for idx, existing in enumerate(items):
            if item_id and str(existing.get("id") or "") == item_id:
                restored = dict(existing)
                restored.update(item)
                restored["state"] = "blocked" if restored.get("blocked") else "queued"
                restored.pop("starting_at", None)
                restored.pop("stream_id", None)
                items[idx] = restored
                _write_items_unlocked(session_id, items)
                _emit_queue_changed(session_id)
                return
        restored = dict(item)
        restored["state"] = "blocked" if restored.get("blocked") else "queued"
        restored.pop("starting_at", None)
        restored.pop("stream_id", None)
        items.insert(0, restored)
        _write_items_unlocked(session_id, items)
        _emit_queue_changed(session_id)


def _finish_attempt(session_id: str, item_id: str, response: dict[str, Any]) -> None:
    status = int((response or {}).get("_status", 200) or 200)
    with _LOCK:
        items = _read_items_unlocked(session_id)
        item = next((entry for entry in items if str(entry.get("id") or "") == item_id), None)
        if item is None:
            return
        item.pop("starting_at", None)
        if status == 409:
            item["state"] = "queued"
        elif status >= 400:
            retry_count = int(item.get("retry_count") or 0) + 1
            item["retry_count"] = retry_count
            item["error"] = str((response or {}).get("error") or f"start_failed_{status}")
            if retry_count >= _MAX_START_RETRIES:
                item["blocked"] = True
                item["state"] = "blocked"
            else:
                item["state"] = "queued"
        else:
            stream_id = str((response or {}).get("stream_id") or "").strip()
            if not stream_id:
                item["state"] = "queued"
                item["error"] = "start response did not include a stream id"
            elif not _stream_is_still_active(session_id, stream_id):
                # The worker can finish between returning its start response
                # and this durable transition. Teardown cannot retire a
                # `starting` item, so retire it here while holding the same
                # queue lock; otherwise a completed turn leaves a tombstone.
                _record_completed_unlocked(session_id, [item])
                items.remove(item)
            else:
                item["state"] = "started"
                item["stream_id"] = stream_id
                item["started_at"] = time.time()
                item.pop("blocked", None)
                item.pop("error", None)
        _write_items_unlocked(session_id, items)
        _emit_queue_changed(session_id)


def complete_started(session_id: str, stream_id: str) -> bool:
    """Retire the queue tombstone after the correlated turn tears down."""
    if not session_id or not stream_id:
        return False
    with _LOCK:
        items = _read_items_unlocked(session_id)
        completed = [
            item
            for item in items
            if (
                str(item.get("state") or "") == "started"
                and str(item.get("stream_id") or "") == str(stream_id)
            )
        ]
        kept = [
            item
            for item in items
            if not (
                str(item.get("state") or "") == "started"
                and str(item.get("stream_id") or "") == str(stream_id)
            )
        ]
        if len(kept) == len(items):
            return False
        # Persist the idempotency receipt before removing the queue owner. If
        # the process dies between these writes, recovery sees the remaining
        # started item and retires it without allowing a duplicate enqueue.
        _record_completed_unlocked(session_id, completed)
        _write_items_unlocked(session_id, kept)
        _emit_queue_changed(session_id)
        return True


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


def _stream_is_still_active(session_id: str, stream_id: str) -> bool:
    try:
        from api import config

        with config.ACTIVE_RUNS_LOCK:
            for active_stream_id, active in config.ACTIVE_RUNS.items():
                if str(active_stream_id) == stream_id and str((active or {}).get("session_id") or "") == session_id:
                    return True
    except Exception:
        logger.debug("Could not inspect active runs for queue completion", exc_info=True)
    try:
        from api.routes import STREAMS, get_session

        if stream_id in STREAMS:
            return True
        return str(getattr(get_session(session_id), "active_stream_id", None) or "") == stream_id
    except Exception:
        return False


def _stream_has_live_runtime(session_id: str, stream_id: str) -> bool:
    """Return true only for process-live ownership, not a persisted session id."""
    try:
        from api import config

        with config.ACTIVE_RUNS_LOCK:
            active = config.ACTIVE_RUNS.get(stream_id)
            if isinstance(active, dict) and str(active.get("session_id") or "") == session_id:
                return True
    except Exception:
        logger.debug("Could not inspect active runs during queue recovery", exc_info=True)
    try:
        from api.routes import STREAMS

        return stream_id in STREAMS
    except Exception:
        return False


def drain_for_session(session_id: str) -> int:
    """Start at most one queued follow-up turn for an idle session.

    Called from the streaming teardown hook after ``unregister_active_run``.
    It claims one item atomically, starts it on a daemon thread, and requeues the
    item if the existing chat-start guard reports a 409 race.
    """
    if not session_id:
        return 0
    with _LOCK:
        if session_id in _DRAINING_SESSIONS or _session_has_active_turn(session_id):
            return 0
        item = claim_next(session_id)
        if not item:
            return 0
        _DRAINING_SESSIONS.add(session_id)

    def _runner() -> None:
        retry_needed = False
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
                queue_client_id=str(item.get("client_queue_id") or "") or None,
            )
            status = int((resp or {}).get("_status", 200) or 200)
            _finish_attempt(session_id, str(item.get("id") or ""), resp or {})
            if status >= 400:
                retry_needed = status != 409
                logger.warning(
                    "queued follow-up failed for session %s: status=%s retries=%s blocked=%s err=%r",
                    session_id,
                    status,
                    int(item.get("retry_count") or 0) + 1,
                    int(item.get("retry_count") or 0) + 1 >= _MAX_START_RETRIES,
                    (resp or {}).get("error"),
                )
            else:
                logger.info(
                    "queued follow-up turn started for session %s item=%s stream_id=%s",
                    session_id,
                    item.get("id"),
                    (resp or {}).get("stream_id"),
                )
        except Exception as exc:
            _finish_attempt(
                session_id,
                str(item.get("id") or ""),
                {"_status": 500, "error": str(exc) or type(exc).__name__},
            )
            retry_needed = True
            logger.warning("queued follow-up turn raised for session %s", session_id, exc_info=True)
        finally:
            with _LOCK:
                _DRAINING_SESSIONS.discard(session_id)
        if retry_needed:
            with _LOCK:
                queued = _read_items_unlocked(session_id)
                retry_needed = bool(
                    queued
                    and str(queued[0].get("id") or "") == str(item.get("id") or "")
                    and str(queued[0].get("state") or "") == "queued"
                )
            if retry_needed:
                timer = threading.Timer(_RETRY_DELAY_SECONDS, drain_for_session, args=(session_id,))
                timer.daemon = True
                timer.start()

    thread = threading.Thread(
        target=_runner,
        name=f"hermes-webui-queued-followup-{str(session_id)[:8]}",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        with _LOCK:
            _DRAINING_SESSIONS.discard(session_id)
        requeue_front(session_id, item)
        logger.warning("failed to start queued follow-up runner for %s", session_id, exc_info=True)
        return 0
    return 1


def recover_all_queues(*, schedule: bool = True) -> dict[str, int]:
    """Repair crash-interrupted ownership transitions and resume queued FIFO heads."""
    result = {"sessions": 0, "requeued": 0, "started": 0, "retired": 0, "errors": 0}
    to_schedule: list[str] = []
    queue_dir = _queue_dir()
    if not queue_dir.exists():
        return result
    from api.routes import get_session

    for path in sorted(queue_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or not raw:
                continue
            session_id = str(raw[0].get("session_id") or "").strip()
            if not session_id:
                raise QueueStorageError(f"queue file {path.name} has no session owner")
            session = get_session(session_id)
            active_stream_id = str(getattr(session, "active_stream_id", None) or "").strip()
            pending_item_id = str(getattr(session, "pending_queue_item_id", None) or "").strip()
            active_stream_is_live = bool(
                active_stream_id and _stream_has_live_runtime(session_id, active_stream_id)
            )
            with _LOCK:
                items = _read_items_unlocked(session_id)
                repaired: list[dict[str, Any]] = []
                changed = False
                for item in items:
                    state = str(item.get("state") or "queued")
                    item_id = str(item.get("id") or "")
                    correlated = bool(
                        pending_item_id
                        and pending_item_id == item_id
                        and active_stream_is_live
                    )
                    if state == "starting":
                        changed = True
                        item.pop("starting_at", None)
                        if correlated:
                            item["state"] = "started"
                            if active_stream_id:
                                item["stream_id"] = active_stream_id
                            result["started"] += 1
                        else:
                            item["state"] = "queued"
                            item.pop("stream_id", None)
                            result["requeued"] += 1
                    elif state == "started":
                        if correlated:
                            if active_stream_id and item.get("stream_id") != active_stream_id:
                                item["stream_id"] = active_stream_id
                                changed = True
                            result["started"] += 1
                        else:
                            changed = True
                            _record_completed_unlocked(session_id, [item])
                            result["retired"] += 1
                            continue
                    repaired.append(item)
                if changed:
                    _write_items_unlocked(session_id, repaired)
                result["sessions"] += 1
                if schedule and repaired and str(repaired[0].get("state") or "queued") == "queued":
                    to_schedule.append(session_id)
        except Exception:
            result["errors"] += 1
            logger.exception("Failed to recover persisted session queue %s", path)
    for session_id in to_schedule:
        drain_for_session(session_id)
    return result
