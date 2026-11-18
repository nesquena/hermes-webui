"""Durable per-session FIFO of follow-up messages submitted while a turn runs.

The Telegram/Discord gateway keeps follow-ups sent during an active turn in a
server-side FIFO (``busy_input_mode: queue``) and runs them as the next turns.
The WebUI used to hold that queue only in the browser (sessionStorage /
localStorage), so a follow-up never ran if the tab closed, the user looked at a
different chat, or the page was refreshed (only the first entry was restored).

This module is the server-side equivalent.  It is deliberately small:

* one JSON file per session under ``STATE_DIR/followups`` (atomic writes);
* strict FIFO; ``processing`` marks the single in-flight head so a drain never
  double-starts an entry, and a crashed/failed start is retried;
* ``drain_followup_queue_for_session`` is called from the stream teardown hook
  (next to the deferred-wakeup drain) and starts the next turn through the same
  ``start_session_turn`` entrypoint server-side wakeups already use.

Entries carry text only.  Attachments are browser ``File`` objects that cannot
be persisted, so attachment follow-ups stay in the browser queue.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Same cap the gateway uses for busy_input_mode=queue (_BUSY_QUEUE_MAX_PENDING).
MAX_PENDING = 32
MAX_TEXT_CHARS = 100_000
# A claimed entry whose start never completed (process died mid-start) is put
# back in the queue after this long while the session is idle.
PROCESSING_STALE_SECS = 60.0

_START_RETRIES = 4
_START_RETRY_DELAY = 1.0

QUEUED = "queued"
PROCESSING = "processing"
FAILED = "failed"

_LOCK = threading.RLock()
_SAFE_SID = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _dir() -> Path:
    from api import config as _cfg

    return Path(_cfg.STATE_DIR) / "followups"


def _path(session_id: str) -> Path:
    sid = str(session_id or "")
    if not _SAFE_SID.match(sid) or sid in {".", ".."}:
        raise ValueError("invalid session id")
    return _dir() / f"{sid}.json"


def _read(session_id: str) -> list[dict]:
    p = _path(session_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("followup queue for %s unreadable; ignoring", session_id, exc_info=True)
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [e for e in (items or []) if isinstance(e, dict) and e.get("id")]


def _write(session_id: str, items: list[dict]) -> None:
    from api.paths import _atomic_write_text

    p = _path(session_id)
    if not items:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(p, json.dumps({"version": 1, "items": items}, ensure_ascii=False))


def _public(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "text": entry.get("text", ""),
        "status": entry.get("status", QUEUED),
        "queued_at": entry.get("queued_at"),
        "error": entry.get("error"),
    }


def list_entries(session_id: str) -> list[dict]:
    with _LOCK:
        return [_public(e) for e in _read(session_id)]


def enqueue(session_id: str, text: str, *, client_id: str | None = None) -> tuple[dict | None, str | None]:
    """Append *text*; returns ``(entry, error)``.  ``client_id`` makes retries idempotent."""
    text = str(text or "").strip()
    if not text:
        return None, "message is required"
    if len(text) > MAX_TEXT_CHARS:
        return None, "message too long"
    cid = str(client_id or "")[:64] or None
    with _LOCK:
        items = _read(session_id)
        if cid:
            for e in items:
                if e.get("client_id") == cid:
                    return _public(e), None  # duplicate submit/migration retry
        if len(items) >= MAX_PENDING:
            return None, f"queue is full ({MAX_PENDING} pending follow-ups)"
        entry = {
            "id": uuid.uuid4().hex,
            "client_id": cid,
            "text": text,
            "status": QUEUED,
            "queued_at": time.time(),
        }
        items.append(entry)
        _write(session_id, items)
        return _public(entry), None


def remove(session_id: str, entry_id: str) -> bool:
    """Remove one entry (never a ``processing`` one: it is already starting)."""
    with _LOCK:
        items = _read(session_id)
        kept = [e for e in items if not (e["id"] == entry_id and e.get("status") != PROCESSING)]
        if len(kept) == len(items):
            return False
        _write(session_id, kept)
        return True


def clear(session_id: str) -> int:
    with _LOCK:
        items = _read(session_id)
        kept = [e for e in items if e.get("status") == PROCESSING]
        if len(kept) != len(items):
            _write(session_id, kept)
        return len(items) - len(kept)


def discard_session(session_id: str) -> None:
    try:
        with _LOCK:
            _write(session_id, [])
    except Exception:
        logger.debug("followup queue discard failed for %s", session_id, exc_info=True)


def claim_next(session_id: str) -> dict | None:
    """Atomically mark the head ``queued`` entry ``processing`` and return it.

    Returns None when nothing is claimable or an entry is already in flight, so
    two concurrent drains can never start the same (or two) entries.
    """
    now = time.time()
    with _LOCK:
        items = _read(session_id)
        changed = False
        for e in items:
            if e.get("status") == PROCESSING:
                if now - float(e.get("claimed_at") or 0) > PROCESSING_STALE_SECS:
                    e["status"] = QUEUED  # start never completed; retry in place (keeps order)
                    changed = True
                else:
                    return None
        # FIFO: the head must be claimable; a failed head blocks nothing but is skipped.
        head = next((e for e in items if e.get("status") == QUEUED), None)
        if head is None:
            if changed:
                _write(session_id, items)
            return None
        head["status"] = PROCESSING
        head["claimed_at"] = now
        _write(session_id, items)
        return dict(head)


def complete(session_id: str, entry_id: str) -> None:
    with _LOCK:
        items = _read(session_id)
        kept = [e for e in items if e["id"] != entry_id]
        if len(kept) != len(items):
            _write(session_id, kept)


def release(session_id: str, entry_id: str, *, error: str | None = None) -> None:
    """Return a claimed entry to the queue (``error`` → mark failed instead)."""
    with _LOCK:
        items = _read(session_id)
        for e in items:
            if e["id"] == entry_id:
                e["status"] = FAILED if error else QUEUED
                e["error"] = error
                e.pop("claimed_at", None)
        _write(session_id, items)


def retry(session_id: str, entry_id: str) -> bool:
    with _LOCK:
        items = _read(session_id)
        for e in items:
            if e["id"] == entry_id and e.get("status") == FAILED:
                e["status"] = QUEUED
                e["error"] = None
                _write(session_id, items)
                return True
        return False


def drain_followup_queue_for_session(session_id: str) -> bool:
    """Start the next queued follow-up if the session is idle.  Never raises.

    Called at turn teardown (after the session leaves ACTIVE_RUNS) and right
    after an enqueue, so a message that arrives just as a turn ends is still
    run.  The turn starts on a daemon thread so teardown never blocks; one entry
    per turn, the next is drained by that turn's own teardown.
    """
    try:
        if not session_id:
            return False
        from api.background_process import _session_has_active_turn

        if _session_has_active_turn(session_id):
            return False
        entry = claim_next(session_id)
        if entry is None:
            return False
        threading.Thread(
            target=_start_claimed,
            args=(session_id, entry),
            name=f"followup-{session_id[:8]}",
            daemon=True,
        ).start()
        return True
    except Exception:
        logger.warning("followup drain failed for session %s", session_id, exc_info=True)
        return False


def _start_claimed(session_id: str, entry: dict) -> None:
    resp: dict | None = None
    status = 0
    # A 409 means another turn holds the session (a human turn that raced us, or
    # the ending turn's last bookkeeping).  Retry briefly, then leave the entry
    # queued in place: that turn's own teardown drains it.  Bounded, no hot loop.
    for attempt in range(_START_RETRIES):
        try:
            from api.routes import start_session_turn

            resp = start_session_turn(session_id, entry["text"], source="queued_followup")
            status = int((resp or {}).get("_status", 200) or 200)
        except Exception as exc:
            logger.warning("followup start crashed for %s", session_id, exc_info=True)
            release(session_id, entry["id"], error=f"{type(exc).__name__}: {exc}"[:300])
            return
        if status != 409:
            break
        time.sleep(_START_RETRY_DELAY)
    if status < 300:
        complete(session_id, entry["id"])  # the text is now a normal user turn
    elif status == 409:
        release(session_id, entry["id"])  # not a failure; stays at the head
    else:
        release(session_id, entry["id"], error=str((resp or {}).get("error") or f"HTTP {status}")[:300])
