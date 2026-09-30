"""Durable pending goal-continuation registry (#6885 slice 2a, review round 2).

The marker set ``api.config.PENDING_GOAL_CONTINUATION`` is process-memory:
a restart drops every pending continuation, so a goal turn interrupted by a
server restart has no durable owner and never resumes. This module keeps a
bounded, atomic on-disk registry under the WebUI state dir.

Review round 2 (#7862) addresses the maintainer's correctness blockers:

- ONE locked record per session (canonical continuation prompt + generation +
  lifecycle metadata), not a bare ``set[str]`` — a restarted server can
  redispatch the prompt text, not just a session id.
- Set mutation AND the durable snapshot happen under ONE module-level
  ``threading.RLock``; writers can never serialize different generations or
  lose a newer set to an older one.
- Every snapshot writes a UNIQUE same-directory temp file (``mkstemp``) then
  ``os.replace`` — no shared ``.tmp`` name across writer threads.
- Consumption removes the exact session's record; retirement is explicit for
  consumed / cleared / deleted / expired intents so stale disk intent cannot
  survive forever (bounded ``_RETIRED_LOG`` + ``sweep_expired_goal_continuations``).
- Failures are OBSERVABLE via ``durability_diagnostics()`` instead of being
  silently swallowed into a durability claim. The public locked mutators are
  the single swallow boundary: they never raise into the chat path.
"""

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from typing import Deque, Optional

from api.config import STATE_DIR

logger = logging.getLogger("api.goal_continuation_store")

_PENDING_GOAL_FILE = STATE_DIR / "pending_goal_continuations.json"
_FILE_VERSION = 2
_MAX_RETIRED_LOG = 64
_MAX_INTENT_AGE_SECONDS = 24 * 60 * 60  # stale disk intent must not survive forever

# One owner lock: in-memory mutation + durable snapshot are a single critical
# section, so two writer threads can never interleave different generations.
_LOCK = threading.RLock()
_GENERATION = 0  # bumped under _LOCK on every accepted mutation (arm/retire)
_LAST_LOAD_ERROR: Optional[str] = None
_LAST_WRITE_ERROR: Optional[str] = None
_RETIRED_LOG: Deque[dict] = deque(maxlen=_MAX_RETIRED_LOG)


def _next_generation_unlocked() -> int:
    """Bump the registry generation; callers must hold ``_LOCK``."""
    global _GENERATION
    _GENERATION += 1
    return _GENERATION


def _write_registry_unlocked(records: dict, *, context: str = "") -> None:
    """Atomically persist the full registry (unique tmp + fsync + replace).

    Lock-free; callers must hold ``_LOCK``. Never raises: failures are
    recorded in ``_LAST_WRITE_ERROR`` and surfaced by
    ``durability_diagnostics()`` so durability claims stay observable.
    """
    global _LAST_WRITE_ERROR
    _LAST_WRITE_ERROR = None
    fd = None
    tmp_name = None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": _FILE_VERSION,
                "generation": _GENERATION,
                "records": records,
            },
            sort_keys=True,
        ).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(
            dir=str(STATE_DIR),
            prefix="pending_goal_continuations.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "wb") as fh:
            fd = None  # ownership transferred to the file object on success
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, _PENDING_GOAL_FILE)
        tmp_name = None  # os.replace consumed the tmp path
    except Exception as exc:
        _LAST_WRITE_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Failed to persist goal continuation registry (context=%s, generation=%d): %s",
            context or "-", _GENERATION, exc,
        )
    finally:
        # Only unlink when the tmp was NOT consumed by a successful
        # os.replace; a consumed path (tmp_name=None) has nothing to clean.
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _load_file_raw() -> dict:
    """Lock-free read of the on-disk registry -> {session_id: record}.

    Missing file -> {}; corrupt/unexpected -> {} with the parse failure
    recorded in ``_LAST_LOAD_ERROR`` (never raises). A v1 list-of-strings
    file is upgraded in memory to records with a blank prompt.
    """
    global _LAST_LOAD_ERROR
    _LAST_LOAD_ERROR = None
    try:
        raw = _PENDING_GOAL_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _LAST_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("Goal continuation registry unreadable: %s", _LAST_LOAD_ERROR)
        return {}
    if isinstance(data, dict):
        file_generation = int(data.get("generation") or 0)
        raw_records = data.get("records")
        if not isinstance(raw_records, dict):
            _LAST_LOAD_ERROR = (
                f"registry has no records mapping ({type(raw_records).__name__})"
            )
            return {}
        out: dict[str, dict] = {}
        for sid, rec in raw_records.items():
            if not isinstance(rec, dict):
                continue
            out[str(sid)] = {
                "prompt": str(rec.get("prompt") or ""),
                "generation": int(rec.get("generation") or file_generation),
                "created_at": float(rec.get("created_at") or time.time()),
                "reason": str(rec.get("reason") or "goal_continue"),
            }
        return out
    if isinstance(data, list):
        # v1 format: a bare list of session ids. Upgrade in memory: blank
        # prompt (the old format never carried text); generation is assigned
        # when the record is restored/merged.
        return {
            str(sid): {
                "prompt": "",
                "generation": 0,
                "created_at": time.time(),
                "reason": "goal_continue",
            }
            for sid in data
            if isinstance(sid, str) and sid
        }
    _LAST_LOAD_ERROR = f"unexpected registry shape: {type(data).__name__}"
    return {}


def load_pending_goal_continuations() -> dict:
    """Return the durable records {session_id: record} (locked, never raises)."""
    with _LOCK:
        return _load_file_raw()


def arm_pending_goal_continuation(
    session_id: str,
    continuation_prompt: str = "",
    reason: str = "goal_continue",
) -> None:
    """Arm durable intent for one session: mutate + snapshot under ONE lock.

    Adds the session to ``PENDING_GOAL_CONTINUATION`` AND stores the canonical
    continuation prompt + generation in ``PENDING_GOAL_CONTINUATION_RECORDS``,
    then persists. Never raises into the chat path; write failures are
    observable via ``durability_diagnostics()``.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return
    prompt = "" if continuation_prompt is None else str(continuation_prompt)
    with _LOCK:
        generation = _next_generation_unlocked()
        PENDING_GOAL_CONTINUATION.add(sid)
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = {
            "prompt": prompt,
            "generation": generation,
            "created_at": time.time(),
            "reason": reason,
        }
        _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"arm sid={sid} reason={reason}",
        )


def retire_pending_goal_continuation(
    session_id: str,
    reason: str = "consumed",
) -> None:
    """Retire durable intent for exactly one session (mutate + snapshot).

    Removes the session from the marker set AND deletes its on-disk record, so
    a later startup/repair can never re-arm a consumed continuation. The
    reason is appended to the bounded ``_RETIRED_LOG`` diagnostics.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        _next_generation_unlocked()
        PENDING_GOAL_CONTINUATION.discard(sid)
        PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)
        _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"retire sid={sid} reason={reason}",
        )
        _RETIRED_LOG.append(
            {"session_id": sid, "reason": reason, "at": time.time()}
        )


def normalize_continuation_text(value: str) -> str:
    """Canonical form used to compare an incoming turn with a recorded intent.

    The browser echoes ``continuation_prompt`` back verbatim as the next user
    turn, but whitespace is not a stable identity across the SSE hop (JSON
    escaping, ``.strip()`` on both ends, CRLF). Collapsing runs of whitespace
    makes the match tolerant without ever widening it into "any turn matches":
    an unrelated message still normalizes to a different string.
    """
    return " ".join(str(value or "").split())


def consume_pending_goal_continuation(
    session_id: str,
    incoming_text: str = "",
) -> bool:
    """Consume this session's durable intent ONLY if the turn is the continuation.

    Before #7862 the marker was retired by session id alone, which was safe
    only while it lived for the few seconds between ``goal_continue`` firing
    and the browser's automatic send. Now the marker can come back at startup
    long after that browser is gone, so an unrelated next message would be
    swallowed as a goal continuation, retiring the marker and letting the
    goal machinery queue another automatic continuation on top of it.

    The recorded canonical prompt is the contract: a restored marker is only
    spent when the incoming turn IS that continuation. Any other send stays
    an ordinary turn and leaves the pending intent in place (expiry still
    bounds it via ``sweep_expired_goal_continuations``).

    Returns True when the intent was consumed. Never raises into the chat path.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return False
    incoming = normalize_continuation_text(incoming_text)
    with _LOCK:
        record = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)
        if record is None:
            # No durable record: a bare in-memory marker (legacy path or a
            # v1 file upgraded without prompt text) cannot be matched safely.
            # Leave it alone rather than retire an intent we cannot verify --
            # expiry bounds it, and swallowing an unrelated turn is the exact
            # data-loss bug this function exists to prevent.
            return False
        recorded = normalize_continuation_text(record.get("prompt") or "")
        if not recorded or recorded != incoming:
            return False
        _next_generation_unlocked()
        PENDING_GOAL_CONTINUATION.discard(sid)
        PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)
        _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"consume sid={sid}",
        )
        _RETIRED_LOG.append({"session_id": sid, "reason": "consumed", "at": time.time()})
        return True


def restore_goal_continuations() -> int:
    """Startup-only restore: merge durable records into the live marker set.

    Merges BOTH ``PENDING_GOAL_CONTINUATION`` and
    ``PENDING_GOAL_CONTINUATION_RECORDS`` for every durable record not already
    live (a live in-memory record always wins over disk). Returns the count of
    sessions restored. Never raises.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with _LOCK:
        disk = _load_file_raw()
        restored = 0
        for sid, record in disk.items():
            if sid in PENDING_GOAL_CONTINUATION or sid in PENDING_GOAL_CONTINUATION_RECORDS:
                continue
            generation = _next_generation_unlocked()
            record = dict(record)
            if not record.get("generation"):
                # v1 upgrade: assign the current generation at restore time.
                record["generation"] = generation
            PENDING_GOAL_CONTINUATION.add(sid)
            PENDING_GOAL_CONTINUATION_RECORDS[sid] = record
            restored += 1
        if restored:
            _write_registry_unlocked(
                PENDING_GOAL_CONTINUATION_RECORDS,
                context=f"restore count={restored}",
            )
        return restored


def sweep_expired_goal_continuations(
    max_age_seconds: float = _MAX_INTENT_AGE_SECONDS,
) -> int:
    """Retire durable intent older than ``max_age_seconds``; return count swept."""
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    now = time.time()
    with _LOCK:
        stale = [
            sid
            for sid, record in PENDING_GOAL_CONTINUATION_RECORDS.items()
            if (now - float(record.get("created_at") or now)) > max_age_seconds
        ]
        for sid in stale:
            retire_pending_goal_continuation(sid, reason="expired")
        return len(stale)


def durability_diagnostics() -> dict:
    """Observable durability state: errors + retirement log.

    Lets operators and tests distinguish "durable" from "write/load failed"
    instead of the old silent-success swallow.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    with _LOCK:
        return {
            "generation": _GENERATION,
            "last_load_error": _LAST_LOAD_ERROR,
            "last_write_error": _LAST_WRITE_ERROR,
            "registry_exists": _PENDING_GOAL_FILE.exists(),
            "live_records": len(PENDING_GOAL_CONTINUATION_RECORDS),
            "retired": list(_RETIRED_LOG),
        }


# Import-time hygiene: ensure the state dir exists before any snapshot (and
# give failures a home in the log instead of the chat path).
STATE_DIR.mkdir(parents=True, exist_ok=True)
