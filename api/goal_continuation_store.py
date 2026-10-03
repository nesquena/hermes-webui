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
import re
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
# Bounded rollback receipts: one per in-flight chat start, keyed by start
# attempt. A slot is only occupied between a consume and either its
# rejected-start rollback or the successful launch that follows it, so with
# launch-time discard this can never accumulate. The cap is a backstop, not the
# primary bound: eviction is per session (a full deque must never silently
# steal ANOTHER session's in-flight receipt).
_MAX_ROLLBACK_RECEIPTS = 64
# Receipts kept per session. A single session can only have one chat start in
# flight (the session agent lock serialises them), so a handful is generous;
# the per-session cap is what actually protects an in-flight receipt.
_MAX_ROLLBACK_RECEIPTS_PER_SESSION = 4
_MAX_INTENT_AGE_SECONDS = 24 * 60 * 60  # stale disk intent must not survive forever

# One owner lock: in-memory mutation + durable snapshot are a single critical
# section, so two writer threads can never interleave different generations.
_LOCK = threading.RLock()
_GENERATION = 0  # bumped under _LOCK on every accepted mutation (arm/retire)
_LAST_LOAD_ERROR: Optional[str] = None
_LAST_WRITE_ERROR: Optional[str] = None
_RETIRED_LOG: Deque[dict] = deque(maxlen=_MAX_RETIRED_LOG)
# Rollback receipts: the record a consume popped, kept so a rejected chat
# start can restore marker + record together. Keyed by (session id, start
# attempt id) and guarded by the same _LOCK.
#
# Keying by start attempt -- not a single global FIFO -- is what keeps an
# in-flight receipt alive (#7862 round 5): with a global deque(maxlen=N), 64
# unrelated successful starts silently evicted session A's receipt, and A's
# rejected-start rollback then fell back to a bare marker its retry could not
# match. Eviction is now per session, and a successful launch discards its
# receipt immediately, so a live receipt is never evicted by unrelated traffic.
_ROLLBACK_RECEIPTS: "dict[tuple[str, str], dict]" = {}
# Generations at which each session's intent was explicitly retired
# (``/goal clear``, session delete, expiry). A rollback receipt records the
# generation of the intent it consumed, so a restore landing after a retirement
# can tell the difference between "nothing happened" and "the user cancelled
# this" -- without it, a late rejected-start rollback resurrected a goal the
# user had already cleared (#7862 round 5). Bounded like the other diagnostics.
_MAX_RETIRED_GENERATIONS = 64
_RETIRED_GENERATIONS: "dict[str, int]" = {}


def _next_generation_unlocked() -> int:
    """Bump the registry generation; callers must hold ``_LOCK``."""
    global _GENERATION
    _GENERATION += 1
    return _GENERATION


def _drop_rollback_receipt_unlocked(sid: str) -> None:
    """Drop every rollback receipt for ``sid``; callers must hold ``_LOCK``."""
    for key in [k for k in _ROLLBACK_RECEIPTS if k[0] == sid]:
        del _ROLLBACK_RECEIPTS[key]


def _evict_overflow_receipts_unlocked() -> None:
    """Enforce the global receipt backstop WITHOUT touching live attempts.

    Only reached when the registry somehow holds more than
    ``_MAX_ROLLBACK_RECEIPTS`` receipts (a leaked receipt for a session that
    never launched nor rolled back). The oldest entries are dropped -- and this
    is logged, because a receipt that outlives its attempt is a real defect, not
    a routine eviction. Crucially, this runs AFTER the per-session cap, so an
    in-flight attempt (which holds one of the per-session slots for its
    session) is the last thing to go, never collateral of unrelated traffic.
    """
    overflow = len(_ROLLBACK_RECEIPTS) - _MAX_ROLLBACK_RECEIPTS
    if overflow <= 0:
        return
    for key in list(_ROLLBACK_RECEIPTS)[:overflow]:
        del _ROLLBACK_RECEIPTS[key]
    logger.warning(
        "Rolled back %d stale goal-continuation receipt(s) past the %d cap",
        overflow,
        _MAX_ROLLBACK_RECEIPTS,
    )


def _record_rollback_receipt_unlocked(sid: str, record: dict, attempt_id: str) -> None:
    """Store a rollback receipt for one start attempt; callers hold ``_LOCK``."""
    attempt = str(attempt_id or "") or "attempt"
    _ROLLBACK_RECEIPTS[(sid, attempt)] = dict(record)
    # Per-session cap first: a session with one chat start in flight keeps its
    # slot, so unrelated sessions can never push it out.
    session_keys = [k for k in _ROLLBACK_RECEIPTS if k[0] == sid]
    if len(session_keys) > _MAX_ROLLBACK_RECEIPTS_PER_SESSION:
        for key in session_keys[: len(session_keys) - _MAX_ROLLBACK_RECEIPTS_PER_SESSION]:
            del _ROLLBACK_RECEIPTS[key]
    _evict_overflow_receipts_unlocked()


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
                "continuation_id": str(rec.get("continuation_id") or ""),
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
                "continuation_id": "",
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
    continuation_id: str = "",
) -> None:
    """Arm durable intent for one session: mutate + snapshot under ONE lock.

    Adds the session to ``PENDING_GOAL_CONTINUATION`` AND stores the canonical
    continuation prompt + generation in ``PENDING_GOAL_CONTINUATION_RECORDS``,
    then persists. Never raises into the chat path; write failures are
    observable via ``durability_diagnostics()``.

    ``continuation_id`` is the opaque token the ``goal_continue`` SSE event
    hands the browser so the queued automatic continuation can be matched by
    identity instead of by text (#7862 core finding): text identity breaks
    when a ``/use <skill>`` directive wraps the queued send.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return
    prompt = "" if continuation_prompt is None else str(continuation_prompt)
    with _LOCK:
        generation = _next_generation_unlocked()
        # A fresh intent supersedes any older one: drop a stale rollback
        # receipt for this session so a late rejected-start rollback can never
        # resurrect the generation the new intent replaced. The session's
        # retirement stamp is cleared too: this arm IS the new intent, so a
        # rejected start for it is exactly what a rollback is for.
        _drop_rollback_receipt_unlocked(sid)
        _RETIRED_GENERATIONS.pop(sid, None)
        PENDING_GOAL_CONTINUATION.add(sid)
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = {
            "prompt": prompt,
            "generation": generation,
            "created_at": time.time(),
            "reason": reason,
            "continuation_id": str(continuation_id or ""),
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
        generation = _next_generation_unlocked()
        PENDING_GOAL_CONTINUATION.discard(sid)
        PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)
        # Record the retirement generation so a rollback receipt minted BEFORE
        # this retirement can never restore the intent afterwards (#7862 round
        # 5: ``/goal clear`` was silently undone by a late rejected-start
        # rollback). Also drop the session's receipts -- they describe an
        # intent that no longer exists, and keeping them only invites a claim
        # that the generation guard below would then have to refuse.
        _drop_rollback_receipt_unlocked(sid)
        _RETIRED_GENERATIONS.pop(sid, None)
        _RETIRED_GENERATIONS[sid] = generation
        while len(_RETIRED_GENERATIONS) > _MAX_RETIRED_GENERATIONS:
            del _RETIRED_GENERATIONS[next(iter(_RETIRED_GENERATIONS))]
        # A fresh arm is NOT a retirement, so the session's generation stamp is
        # left alone: an older receipt must stay blocked.
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


# The browser can PREPEND a known skill-directive envelope to the queued
# continuation text before it is sent (#7862 core finding): a `/use <skill>`
# typed while a `/goal` turn runs converts the automatic continuation send
# into `[USER OVERRIDE] …\n\n[FORCED SKILL CONTEXT: …]\n…\n[/FORCED SKILL CONTEXT]\n\n<continuation prompt>`.
# Text identity alone then never matches, so the intent stays pending and the
# goal loop dies after its first automatic continuation. These anchored
# patterns mirror static/messages.js (the ONLY producer of the envelope);
# user-authored lookalike text is never stripped by the frontend either, so
# stripping only the exact envelope keeps the admission contract intact.
_SILL_DIRECTIVE_RE = re.compile(
    r"^\[USER OVERRIDE\]\s+You MUST follow the skill '[^']*'[^\n]*(?:\n\n\[FORCED SKILL CONTEXT:[^\n]*\n.*?\n\[/FORCED SKILL CONTEXT\])?\s*",
    re.DOTALL,
)


def strip_known_skill_envelope(value: str) -> str:
    """Return ``value`` with a leading forced-skill directive envelope removed.

    Only the exact ``[USER OVERRIDE]`` + optional ``[FORCED SKILL CONTEXT]``
    envelope produced by ``/use`` is stripped; anything else (including a
    user's own text) is returned unchanged, so the comparison stays a
    "recorded prompt, optionally wrapped by a known skill directive" match
    instead of a fuzzy substring search.
    """
    text = str(value or "")
    stripped = _SILL_DIRECTIVE_RE.sub("", text, count=1)
    return stripped if stripped != text else text


def _continuation_text_matches(recorded_prompt: str, incoming_text: str) -> bool:
    """True when the incoming turn IS the recorded continuation.

    Two accepted shapes, mirroring what the browser actually sends:

    1. the recorded prompt verbatim (normalised) — the plain automatic
       continuation the ``goal_continue`` SSE event queues;
    2. the recorded prompt wrapped by the forced-skill directive envelope a
       pending ``/use`` skill prepends to that same queued send.

    An unrelated human message matches neither and leaves the intent pending.
    """
    recorded = normalize_continuation_text(recorded_prompt)
    if not recorded:
        return False
    candidates = {
        normalize_continuation_text(incoming_text),
        normalize_continuation_text(strip_known_skill_envelope(incoming_text)),
    }
    return recorded in candidates


def consume_pending_goal_continuation(
    session_id: str,
    incoming_text: str = "",
    continuation_id: str = "",
    attempt_id: str = "",
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

    ``continuation_id`` is the preferred match: when the request carries the
    token the ``goal_continue`` SSE event handed the browser, the turn must
    still present the recorded prompt — verbatim, or wrapped by the known
    forced-skill envelope a pending ``/use`` skill prepends to the queued
    send (which is exactly where text identity alone broke the goal loop in
    round 3). A token belonging to a different generation is rejected, a
    request with no token falls back to the text comparison, and unrelated
    text is never swallowed under any combination.

    ``attempt_id`` identifies THIS chat start, so the rollback receipt can be
    claimed by exactly the attempt that created it (see round 5: a global FIFO
    let unrelated successful starts evict an in-flight receipt). A start that
    succeeds must call ``discard_goal_continuation_rollback_receipt``.

    Returns True when the intent was consumed. Never raises into the chat path.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return False
    with _LOCK:
        record = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)
        if record is None:
            # No durable record: a bare in-memory marker (legacy path or a
            # v1 file upgraded without prompt text) cannot be matched safely.
            # Leave it alone rather than retire an intent we cannot verify --
            # expiry bounds it, and swallowing an unrelated turn is the exact
            # data-loss bug this function exists to prevent.
            return False
        recorded_id = str(record.get("continuation_id") or "")
        request_id = str(continuation_id or "")
        if recorded_id and request_id and recorded_id != request_id:
            # A token was carried, but not THIS continuation's token: another
            # (already consumed or restarted) generation. Treat it like any
            # other non-matching turn.
            return False
        # Identity narrows the candidate set to this exact continuation; the
        # recorded prompt still has to be present (verbatim or wrapped by the
        # known forced-skill envelope), so a replayed token carrying unrelated
        # text is never swallowed.
        if not _continuation_text_matches(record.get("prompt") or "", incoming_text):
            return False
        _next_generation_unlocked()
        PENDING_GOAL_CONTINUATION.discard(sid)
        PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)
        _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"consume sid={sid}",
        )
        _RETIRED_LOG.append({"session_id": sid, "reason": "consumed", "at": time.time()})
        # Keep the popped record as a rollback receipt: a chat start that is
        # rejected AFTER the consume (stream-registration / worker-start
        # failure, a 409) must be able to re-arm marker + record together,
        # otherwise the retry runs as an ordinary turn and the goal loop
        # loses its continuation. The receipt is claimed by
        # ``pop_goal_continuation_rollback_receipt`` (rejected-start paths),
        # dropped by ``discard_goal_continuation_rollback_receipt`` (a launch
        # that succeeded) or when a newer intent is armed for the same session.
        _record_rollback_receipt_unlocked(sid, record, attempt_id)
        return True


def pop_goal_continuation_rollback_receipt(
    session_id: str, attempt_id: str = ""
) -> Optional[dict]:
    """Claim and remove the rollback receipt for this start attempt, if any.

    Returns the record a matching ``consume_pending_goal_continuation`` popped
    (with its ``generation``), or ``None`` when there is no live receipt. The
    caller compares the receipt's generation against the CURRENT one before
    restoring, so an intent armed after the rejected start is never clobbered.

    ``attempt_id`` narrows the claim to the exact attempt that created the
    receipt. It is passed whenever the caller has one, so a late rollback can
    never claim a DIFFERENT attempt's receipt. With no ``attempt_id`` the
    session's receipt is claimed by insertion order (the legacy, still-correct
    single-attempt case). Never raises into the chat path.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    attempt = str(attempt_id or "").strip()
    with _LOCK:
        session_keys = [k for k in _ROLLBACK_RECEIPTS if k[0] == sid]
        if attempt:
            exact = (sid, attempt)
            key = exact if exact in _ROLLBACK_RECEIPTS else None
        else:
            key = session_keys[0] if session_keys else None
        if key is None:
            return None
        claimed = _ROLLBACK_RECEIPTS.pop(key)
        return claimed


def discard_goal_continuation_rollback_receipt(
    session_id: str, attempt_id: str = ""
) -> None:
    """Drop this start attempt's receipt once its launch SUCCEEDED.

    A chat start that got as far as ``thr.start()`` can no longer be rolled
    back, so its receipt has no consumer: without this discard, every
    successful goal start leaked a receipt slot for the process lifetime. Under
    the old global FIFO that was not just a leak -- it evicted OTHER sessions'
    in-flight receipts, whose rejected-start rollback then restored a bare
    marker the store deliberately refuses to match (#7862 round 5).

    Never raises into the chat path.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return
    attempt = str(attempt_id or "").strip()
    with _LOCK:
        if attempt:
            _ROLLBACK_RECEIPTS.pop((sid, attempt), None)
        else:
            _drop_rollback_receipt_unlocked(sid)


def restore_pending_goal_continuation(session_id: str, record: dict) -> bool:
    """Re-arm a consumed continuation (marker + durable record) under ``_LOCK``.

    The rejected-start rollback path (#7249, shipped exp-v0.52.392) restored
    only the in-memory marker. Since #7862 a consume also deletes the durable
    record, a marker-only restore leaves the retry unmatched: with no record
    the consume deliberately refuses a bare marker, so the goal loop loses the
    continuation (master's marker-only consume happened to still work).

    Guarded per session: the restore only lands while NO newer intent has
    been armed for this session since the receipt was taken, so a rollback can
    never resurrect a superseded intent over a fresh one. Unrelated registry
    churn (another session's arm/expire) is deliberately tolerated -- the
    continuation contract for THIS session is still exactly what was consumed.

    Returns True when the intent was restored. Never raises into the chat path.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid or not isinstance(record, dict):
        return False
    with _LOCK:
        current = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)
        if current is not None:
            # A newer intent for this same session is already live: if it is
            # a DIFFERENT generation than the receipt describes, the receipt
            # is stale and must not clobber it. (A byte-identical re-arm of
            # the very same continuation_id cannot happen mid-flight, since
            # arming drops the session's receipts, so any live record here
            # is by construction newer.)
            return False
        retired_at = _RETIRED_GENERATIONS.get(sid)
        if retired_at is not None and int(record.get("generation") or 0) <= retired_at:
            # The intent this receipt describes was explicitly retired
            # (``/goal clear``, session delete, expiry) AFTER it was consumed
            # but BEFORE this rollback landed. Restoring it now would silently
            # undo the user's clear and queue another automatic goal
            # continuation on a goal they stopped (#7862 round 5). The receipt
            # is spent either way: it describes an intent that no longer exists.
            _RETIRED_LOG.append(
                {
                    "session_id": sid,
                    "reason": "rollback_refused_retired",
                    "at": time.time(),
                }
            )
            return False
        _next_generation_unlocked()
        restored = dict(record)
        PENDING_GOAL_CONTINUATION.add(sid)
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = restored
        _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"rollback-restore sid={sid}",
        )
        _RETIRED_LOG.append(
            {"session_id": sid, "reason": "restored_rejected_start", "at": time.time()}
        )
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
            "pending_rollback_receipts": len(_ROLLBACK_RECEIPTS),
            # Rollbacks refused because the intent was retired in the meantime
            # (a ``/goal clear`` that a late rejected-start rollback could
            # otherwise have undone). Non-zero here means a clear raced a
            # failed start -- worth seeing, not an error by itself.
            "rollback_refused_retired": sum(
                1 for entry in _RETIRED_LOG if entry.get("reason") == "rollback_refused_retired"
            ),
            "retired": list(_RETIRED_LOG),
        }


# Import-time hygiene: ensure the state dir exists before any snapshot (and
# give failures a home in the log instead of the chat path).
STATE_DIR.mkdir(parents=True, exist_ok=True)
