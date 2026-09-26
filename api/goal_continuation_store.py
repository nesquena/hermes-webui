"""Durable pending goal-continuation registry (#6885 slice 2a, #6888 lesson).

The marker set ``api.config.PENDING_GOAL_CONTINUATION`` is process-memory:
a restart drops every pending continuation, so a goal turn interrupted by a
server restart has no durable owner and never resumes. This module keeps a
bounded, atomic on-disk copy under the WebUI state dir.

Design rules (kept intentionally small):
- Atomic replace (tmp + ``os.replace``) — a reader never sees a torn file.
- Fail-safe: a missing/corrupt registry reads as the empty set; persistence
  never blocks or raises into the chat path.
- The registry stores only session ids (the same information as the in-memory
  set); prompt-text matching stays owned by the admission fix (#6885 slice 1,
  PR #7855) and is orthogonal to durability.
- GIL-safe: callers snapshot after add/discard with the set already mutated;
  the file write itself is a last-writer-wins whole-set replace.
"""

import json
import logging
import os

from api.config import STATE_DIR

logger = logging.getLogger("api.goal_continuation_store")

_PENDING_GOAL_FILE = STATE_DIR / "pending_goal_continuations.json"
_PENDING_GOAL_FILE_TMP = STATE_DIR / "pending_goal_continuations.json.tmp"


def save_pending_goal_continuations(session_ids) -> None:
    """Atomically persist the pending-goal session-id set (never raises)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(sorted(session_ids)).encode("utf-8")
        # fsync-free atomic replace: the tmp file may linger briefly on crash;
        # a torn tmp is ignored by the reader (name-based, not glob-based).
        _PENDING_GOAL_FILE_TMP.write_bytes(payload)
        os.replace(_PENDING_GOAL_FILE_TMP, _PENDING_GOAL_FILE)
    except Exception as exc:  # pragma: no cover - defensive; never break a turn
        logger.debug("Failed to persist pending goal continuations: %s", exc)


def load_pending_goal_continuations() -> set:
    """Read the pending registry; missing/corrupt -> empty set (never raises)."""
    try:
        raw = _PENDING_GOAL_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, list):
            return set()
        return {s for s in data if isinstance(s, str)}
    except (OSError, ValueError):
        return set()


def recover_pending_goal_continuations() -> None:
    """Merge the durable registry into the live marker set (startup hook).

    Idempotent; never removes live in-memory markers (a concurrent add in
    another thread wins over the file snapshot).
    """
    from api.config import PENDING_GOAL_CONTINUATION

    recovered = load_pending_goal_continuations() - set(PENDING_GOAL_CONTINUATION)
    if recovered:
        PENDING_GOAL_CONTINUATION.update(recovered)
        logger.info(
            "Restored %d pending goal continuation(s) from durable registry.",
            len(recovered),
        )


def snapshot_pending_goal_continuations() -> None:
    """Persist the live marker set; call right after add/discard mutations."""
    from api.config import PENDING_GOAL_CONTINUATION

    save_pending_goal_continuations(PENDING_GOAL_CONTINUATION)


def restore_at_startup() -> None:
    """Restore durable pending goal continuations; never raise into startup.

    Kept in the store (rather than inline in ``server.py``) so the process
    entrypoint stays minimal — server.py is guarded under a 750-line ceiling
    by ``tests/test_sprint10.py`` and inline recovery blocks pushed it over.
    """
    try:
        recover_pending_goal_continuations()
    except Exception as _recover_exc:  # pragma: no cover - defensive guard
        logger.warning(
            "Could not restore pending goal continuations: %s", _recover_exc
        )


# Import-time hygiene: ensure the state dir exists before any snapshot (and
# give failures a home in the log instead of the chat path).
STATE_DIR.mkdir(parents=True, exist_ok=True)