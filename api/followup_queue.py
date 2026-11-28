"""Durable per-session FIFO of follow-up messages submitted while a turn runs.

The Telegram/Discord gateway keeps follow-ups sent during an active turn in a
server-side FIFO (``busy_input_mode: queue``) and runs them as the next turns.
The WebUI used to hold that queue only in the browser (sessionStorage /
localStorage), so a follow-up never ran if the tab closed, the user looked at a
different chat, or the page was refreshed (only the first entry was restored).

This module is the server-side equivalent.  It is deliberately small:

* one JSON file per session under ``STATE_DIR/followups`` (atomic writes);
* strict FIFO; ``processing`` marks the single in-flight head so a drain never
  double-starts an entry, and a crashed/failed start is retried (bounded);
* consumed ``client_id`` values are remembered for a short window so an HTTP
  retry of an already-started submit cannot run the message twice;
* ``drain_followup_queue_for_session`` is called from the turn-teardown hooks
  (local streaming worker and Gateway worker), after enqueue/retry, when a
  client looks at an idle session, and from a startup sweep.  It starts the
  next turn through the same ``start_session_turn`` entrypoint server-side
  wakeups already use, and refuses while the session still has a turn to
  recover (Gateway pending state) so a follow-up never replaces it.

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
# Consumed client ids are kept this long / this many, for retry de-duplication.
DONE_ID_TTL_SECS = 600.0
DONE_ID_MAX = 64
# After this many failed start rounds an entry is marked failed (visible, retryable).
MAX_START_ROUNDS = 5

_START_RETRIES = 4
_START_RETRY_DELAY = 1.0

QUEUED = "queued"
PROCESSING = "processing"
FAILED = "failed"
STARTED = "started"  # synthetic status returned for an already-consumed client_id

_LOCK = threading.RLock()
_SAFE_SID = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
# Sessions whose drain was refused because an entry was mid-claim: the claimer
# re-drains after it releases, so the wake-up of a turn that ended in between is
# not lost.
_DRAIN_REQUESTED: set[str] = set()


def _dir() -> Path:
    from api import config as _cfg

    return Path(_cfg.STATE_DIR) / "followups"


def _path(session_id: str) -> Path:
    sid = str(session_id or "")
    if not _SAFE_SID.match(sid) or sid in {".", ".."}:
        raise ValueError("invalid session id")
    return _dir() / f"{sid}.json"


def _read_doc(session_id: str) -> tuple[list[dict], list[dict]]:
    p = _path(session_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError):
        logger.warning("followup queue for %s unreadable; ignoring", session_id, exc_info=True)
        return [], []
    if not isinstance(data, dict):
        return [], []
    items = [e for e in (data.get("items") or []) if isinstance(e, dict) and e.get("id")]
    done = [d for d in (data.get("done") or []) if isinstance(d, dict) and d.get("cid")]
    return items, done


def _write_doc(session_id: str, items: list[dict], done: list[dict]) -> None:
    from api.paths import _atomic_write_text

    now = time.time()
    done = [d for d in done if now - float(d.get("at") or 0) < DONE_ID_TTL_SECS][-DONE_ID_MAX:]
    p = _path(session_id)
    if not items and not done:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(p, json.dumps({"version": 2, "items": items, "done": done}, ensure_ascii=False))


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
        return [_public(e) for e in _read_doc(session_id)[0]]


def enqueue(session_id: str, text: str, *, client_id: str | None = None) -> tuple[dict | None, str | None]:
    """Append *text*; returns ``(entry, error)``.  ``client_id`` makes retries idempotent."""
    text = str(text or "").strip()
    if not text:
        return None, "message is required"
    if len(text) > MAX_TEXT_CHARS:
        return None, "message too long"
    cid = str(client_id or "")[:64] or None
    with _LOCK:
        items, done = _read_doc(session_id)
        if cid:
            for e in items:
                if e.get("client_id") == cid:
                    return _public(e), None  # duplicate submit/migration retry
            now = time.time()
            for d in done:
                if d["cid"] == cid and now - float(d.get("at") or 0) < DONE_ID_TTL_SECS:
                    # Already started (its response was lost and the client retried): never run twice.
                    return {"id": f"consumed-{cid}", "text": text, "status": STARTED, "queued_at": d.get("at"), "error": None}, None
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
        _write_doc(session_id, items, done)
        return _public(entry), None


def remove(session_id: str, entry_id: str) -> bool:
    """Remove one entry (never a ``processing`` one: it is already starting)."""
    with _LOCK:
        items, done = _read_doc(session_id)
        kept = [e for e in items if not (e["id"] == entry_id and e.get("status") != PROCESSING)]
        if len(kept) == len(items):
            return False
        _write_doc(session_id, kept, done)
        return True


def clear(session_id: str) -> int:
    with _LOCK:
        items, done = _read_doc(session_id)
        kept = [e for e in items if e.get("status") == PROCESSING]
        if len(kept) != len(items):
            _write_doc(session_id, kept, done)
        return len(items) - len(kept)


def discard_session(session_id: str) -> None:
    """Drop everything for a deleted session (queue and consumed-id memory)."""
    try:
        with _LOCK:
            _DRAIN_REQUESTED.discard(session_id)
            _write_doc(session_id, [], [])
    except Exception:
        logger.debug("followup queue discard failed for %s", session_id, exc_info=True)


def claim_next(session_id: str, *, recovering: bool = False) -> dict | None:
    """Atomically mark the head ``queued`` entry ``processing`` and return it.

    Returns None when nothing is claimable or an entry is already in flight, so
    two concurrent drains can never start the same (or two) entries.  When a
    fresh claim blocks the caller, a re-drain is requested for the claimer to
    run after it releases (the ending turn's own drain would otherwise be lost).
    ``recovering`` (startup sweep) treats every ``processing`` entry as orphaned:
    no worker survives a restart.
    """
    now = time.time()
    with _LOCK:
        items, done = _read_doc(session_id)
        changed = False
        for e in items:
            if e.get("status") == PROCESSING:
                if recovering or now - float(e.get("claimed_at") or 0) > PROCESSING_STALE_SECS:
                    e["status"] = QUEUED  # start never completed; retry in place (keeps order)
                    changed = True
                else:
                    _DRAIN_REQUESTED.add(session_id)
                    return None
        head = next((e for e in items if e.get("status") == QUEUED), None)
        if head is None:
            if changed:
                _write_doc(session_id, items, done)
            return None
        head["status"] = PROCESSING
        head["claimed_at"] = now
        _write_doc(session_id, items, done)
        return dict(head)


def complete(session_id: str, entry_id: str) -> None:
    """The entry became a real turn: drop it, remembering its client id for retry de-dup."""
    with _LOCK:
        items, done = _read_doc(session_id)
        gone = [e for e in items if e["id"] == entry_id]
        kept = [e for e in items if e["id"] != entry_id]
        if gone and gone[0].get("client_id"):
            done.append({"cid": gone[0]["client_id"], "at": time.time()})
        if gone:
            _write_doc(session_id, kept, done)


def release(session_id: str, entry_id: str, *, error: str | None = None, count_round: bool = False) -> None:
    """Return a claimed entry to the queue (``error`` → mark failed instead).

    ``count_round`` counts one failed start round; after ``MAX_START_ROUNDS`` the
    entry is marked failed ("session stayed busy") so a stale active-stream id
    cannot loop the drain forever.
    """
    with _LOCK:
        items, done = _read_doc(session_id)
        for e in items:
            if e["id"] == entry_id:
                e.pop("claimed_at", None)
                if error:
                    e["status"], e["error"] = FAILED, error
                else:
                    e["status"], e["error"] = QUEUED, None
                    if count_round:
                        e["rounds"] = int(e.get("rounds") or 0) + 1
                        if e["rounds"] >= MAX_START_ROUNDS:
                            e["status"] = FAILED
                            e["error"] = "session stayed busy; retry when it is idle"
        _write_doc(session_id, items, done)


def retry(session_id: str, entry_id: str) -> bool:
    with _LOCK:
        items, done = _read_doc(session_id)
        for e in items:
            if e["id"] == entry_id and e.get("status") == FAILED:
                e["status"], e["error"], e["rounds"] = QUEUED, None, 0
                _write_doc(session_id, items, done)
                return True
        return False


def _load_session(session_id: str):
    """Session record, or None when the session no longer exists."""
    from api.models import get_session

    try:
        return get_session(session_id)
    except KeyError:
        return None


def _awaiting_recovery(s) -> bool:
    """True while the session still holds a turn that must be recovered, not replaced.

    A Gateway transport disconnect (and a WebUI restart mid-turn) leave
    ``gateway_run`` / ``pending_user_message`` on the record on purpose so the
    original turn can be re-attached.  Starting a follow-up now would replace
    ``active_stream_id`` and make that recovery refuse.  Normal teardown clears
    both fields before the drain runs.
    """
    return bool(getattr(s, "gateway_run", None) or getattr(s, "pending_user_message", None))


def drain_followup_queue_for_session(session_id: str, *, recovering: bool = False) -> bool:
    """Start the next queued follow-up if the session is idle.  Never raises.

    The single place that decides "safe to start": every trigger (turn teardown,
    enqueue/retry, a client viewing the session, the startup sweep) goes through
    here.  The turn starts on a daemon thread so callers never block; one entry
    per turn, the next is drained by that turn's own teardown.
    """
    try:
        if not session_id:
            return False
        from api.background_process import _session_has_active_turn

        if _session_has_active_turn(session_id):
            return False
        if not list_entries(session_id):
            return False
        s = _load_session(session_id)
        if s is None:  # session was deleted: its queue is garbage
            discard_session(session_id)
            return False
        if _awaiting_recovery(s):
            return False  # leave queued; re-drained when the recovered turn settles / a client looks
        entry = claim_next(session_id, recovering=recovering)
        if entry is None:
            return False
        threading.Thread(
            target=_start_claimed,
            args=(session_id, entry, recovering),
            name=f"followup-{session_id[:8]}",
            daemon=True,
        ).start()
        return True
    except Exception:
        logger.warning("followup drain failed for session %s", session_id, exc_info=True)
        return False


def _already_started(session_id: str, entry: dict) -> bool:
    """True when a ``processing`` entry's text already became a user turn.

    ``start_session_turn`` persists the user message before ``complete`` runs, so
    a restart between the two must not run the message a second time.
    """
    s = _load_session(session_id)
    if s is None:
        return False
    since = float(entry.get("queued_at") or 0) - 5
    for m in reversed(list(getattr(s, "messages", None) or [])[-6:]):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip() == str(entry.get("text") or "").strip():
            ts = float(m.get("_ts") or m.get("timestamp") or 0)
            return ts == 0 or ts >= since
    return False


def _start_claimed(session_id: str, entry: dict, recovering: bool = False) -> None:
    resp: dict | None = None
    status = 0
    # A 409 means another turn holds the session (a human turn that raced us, or
    # the ending turn's last bookkeeping).  Retry briefly, then leave the entry
    # queued in place: that turn's own teardown drains it.  Bounded, no hot loop.
    for _ in range(_START_RETRIES):
        try:
            from api.routes import start_session_turn

            resp = start_session_turn(session_id, entry["text"], source="queued_followup")
            status = int((resp or {}).get("_status", 200) or 200)
        except Exception:
            logger.warning("followup start crashed for %s", session_id, exc_info=True)
            release(session_id, entry["id"], count_round=True)
            _redrain_if_requested(session_id)
            return
        if status != 409:
            break
        time.sleep(_START_RETRY_DELAY)
    err = str((resp or {}).get("error") or f"HTTP {status}")[:300]
    if status < 300:
        complete(session_id, entry["id"])  # the text is now a normal user turn
    elif status == 404:
        discard_session(session_id)  # session deleted while the entry waited
    elif status == 409 or status >= 500 or recovering:
        release(session_id, entry["id"], count_round=True)  # busy / not ready: stays at the head
        _redrain_if_requested(session_id)
    else:
        release(session_id, entry["id"], error=err)  # rejected (e.g. 400): visible, retryable
    if status < 300:
        with _LOCK:
            _DRAIN_REQUESTED.discard(session_id)  # the new turn's own teardown drains the rest


def _redrain_if_requested(session_id: str) -> None:
    """A teardown drain that arrived while this entry was mid-claim asked for a retry."""
    with _LOCK:
        wanted = session_id in _DRAIN_REQUESTED
        _DRAIN_REQUESTED.discard(session_id)
    if wanted:
        drain_followup_queue_for_session(session_id)


def recover_on_startup(delay: float = 8.0) -> None:
    """After a restart, resume queues stranded by the old process.  Never raises.

    No worker survives a restart, so every ``processing`` entry is orphaned.  An
    entry whose text already became a user turn is completed (not re-run);
    others return to ``queued`` and are drained.  Sessions still holding a
    Gateway turn to recover are skipped by ``drain`` and picked up when a client
    next looks at them.
    """
    def _run():
        try:
            time.sleep(delay)
            d = _dir()
            if not d.is_dir():
                return
            for f in sorted(d.glob("*.json")):
                sid = f.stem
                try:
                    with _LOCK:
                        items, _ = _read_doc(sid)
                    for e in items:
                        if e.get("status") == PROCESSING and _already_started(sid, e):
                            complete(sid, e["id"])
                    drain_followup_queue_for_session(sid, recovering=True)
                except Exception:
                    logger.warning("followup recovery failed for %s", sid, exc_info=True)
        except Exception:
            logger.warning("followup startup recovery failed", exc_info=True)

    threading.Thread(target=_run, name="followup-recovery", daemon=True).start()
