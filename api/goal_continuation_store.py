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
# Bounded diagnostics: the retired log is a ring of recent retirements.
_MAX_RETIRED_LOG = 64
# Registry file layout v3: ``tombstones`` carry the generation at which each
# session's intent was explicitly retired (``/goal clear``, session delete,
# expiry). Kept DURABLY, not in memory, because an in-process counter cannot
# survive the two orderings #7862 round 6 pins:
#
#   - 64 unrelated retirements dropped a session's in-memory stamp, so a late
#     rejected-start rollback restored a goal the user had already cleared;
#   - after a restart ``_GENERATION`` counts from 0 again while a restored
#     record still carries its old generation (41), so ``record.generation <=
#     retired_at`` compared a fresh generation against a stale one and the
#     clear was silently forgotten.
#
# A tombstone is monotonic per session and compared by generation, so it can
# never go stale in the way a bounded in-memory dict could. Bounded like the
# other log sections.
_MAX_TOMBSTONES = 512
# A rollback receipt's slot is never count-capped: see
# ``_sweep_expired_receipts_unlocked``. These constants remain only as
# documentation of the bounds that USED to evict live receipts.
_MAX_ROLLBACK_RECEIPTS = 64  # historical global cap — removed
_MAX_ROLLBACK_RECEIPTS_PER_SESSION = 4  # historical per-session cap — removed
_MAX_INTENT_AGE_SECONDS = 24 * 60 * 60  # stale disk intent must not survive forever
# A rollback receipt's TTL. Circular by design: it is only claimed by the ONE
# attempt that minted it, and that attempt either launches (discarding it) or
# rolls back (claiming it) within the same request. Anything older than this
# outlived its attempt, which is a defect -- so it is dropped on the next
# record, and the drop is logged. TTL is per-receipt and therefore cannot
# evict another session's live attempt no matter how much traffic arrives.
_ROLLBACK_RECEIPT_TTL_SECONDS = 15 * 60

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
_MAX_RETIRED_GENERATIONS = 64  # legacy in-memory mirror; tombstones are durable
_RETIRED_GENERATIONS: "dict[str, int]" = {}
# Durable clear tombstones, {session_id: generation}. Restored at startup and
# re-written on every accepted mutation, so "/goal clear" survives BOTH a
# restart and unlimited unrelated retirements -- the two orderings the
# in-memory dict could not (#7862 round 6). Compared against a receipt's own
# generation, never against the live counter, so it cannot go stale.
_TOMBSTONES: "dict[str, int]" = {}
# #7862 round 8 (CORE): ``consume_pending_goal_continuation`` returned a bare
# bool, so the route could not tell "this turn is NOT the continuation" (leave
# the intent pending, classify the turn normally) from "this turn IS the
# continuation but the durable removal did not commit" (must refuse admission,
# or the registry bytes survive and a restart restores a claimable record after
# the turn was already spent). Both returned False, so a failed commit was
# admitted as an ordinary turn. The store now answers with one of these.
CONSUME_NOT_MATCHING = "not_matching"
CONSUME_COMMITTED = "committed"
CONSUME_COMMIT_FAILED = "commit_failed"
# Durable continuation-handoff tokens (#7862 round 7, finding 2). A consume
# deletes the durable record and keeps the in-memory rollback receipt, so a
# process loss between that delete and
# ``_prepare_chat_start_session_for_stream`` writing ``pending_user_message``
# left NOTHING durable: a cold restore found zero records and the retry ran as
# an ordinary turn. A handoff token records that an admitted attempt owns a
# known pending start, so the crash seam is reconcilable instead of invisible.
#
# Keyed by (session id, start attempt id) like the receipts, guarded by the
# same ``_LOCK``, and DUABLY persisted in the same file section as the
# intentions it hands off, so a cold restore can re-adopt it. Deliberately
# named apart from #7855's ``PENDING_GOAL_CONTINUATION_RECORDS`` family to
# keep the two PRs' symbols from colliding at merge.
_CONTINUATION_HANDOFF_TOKENS: "dict[tuple[str, str], dict]" = {}
_MAX_CONTINUATION_HANDOFFS = 64


def _next_generation_unlocked() -> int:
    """Bump the registry generation; callers must hold ``_LOCK``."""
    global _GENERATION
    _GENERATION += 1
    return _GENERATION


def _drop_rollback_receipt_unlocked(sid: str) -> None:
    """Drop every rollback receipt for ``sid``; callers must hold ``_LOCK``."""
    for key in [k for k in _ROLLBACK_RECEIPTS if k[0] == sid]:
        del _ROLLBACK_RECEIPTS[key]


def _receipt_minted_at(record: dict) -> float:
    """When a receipt was minted, for TTL sweeps. Never raises."""
    try:
        return float(record.get("_receipt_minted_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _receipt_last_claimed_at(record: dict) -> float:
    """When a live attempt last RECLAIMED its receipt. Never raises.

    Distinct from the mint time: a start attempt can legitimately take longer
    than the TTL (a slow provider handshake, a registration callback that is
    still waiting on its worker). Reclaiming refreshes this field, which is
    the attempt positively reporting "I am still live".
    """
    try:
        return float(record.get("_receipt_reclaimed_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _receipt_attempt_is_live(sid: str) -> bool:
    """True when *sid* still has an UNLAUNCHED, non-abandoned attempt.

    #7862 round 10 (finding 3): the handoff is the authoritative record that an
    attempt consumed an intent and has not yet launched — it is created in the
    same ``os.replace`` as the removal, and it disappears only when the attempt
    launches (discharge) or is rejected (rollback). That is exactly the window
    in which the attempt is still live, so a receipt whose session still holds a
    handoff must never be swept for age alone.

    Before this, the sweep compared elapsed wall time against the TTL. The route
    reclaims a receipt exactly once, immediately after the consume, and a start
    has no deadline — a slow provider handshake or a registration callback still
    waiting on its worker outlives the TTL while the attempt runs. The sweep
    then dropped the receipt, the rejection path found nothing to restore, and
    the retry ran as an ordinary turn: the goal loop silently lost its
    continuation.

    A handoff is only conclusive while the attempt still reports in. An attempt
    that crashed never discharges, so its handoff would anchor its receipt
    forever and the registry would grow without bound — the very thing the TTL
    exists to prevent. ``reclaim_goal_continuation_receipt`` refreshes the
    handoff's clock through the receipt path, so a handoff nobody has reclaimed
    past the TTL is an abandoned attempt, and its receipt is swept normally.

    Callers must hold ``_LOCK``.
    """
    now = time.time()
    for key in _CONTINUATION_HANDOFF_TOKENS:
        if key[0] != sid:
            continue
        stored = _CONTINUATION_HANDOFF_TOKENS[key]
        try:
            minted = float(stored.get("_handoff_minted_at") or 0.0)
        except (TypeError, ValueError):
            minted = 0.0
        # A handoff with no readable clock is treated as abandoned rather than
        # immortal: the conservative reading is the one that keeps the registry
        # bounded.
        if minted and now - minted <= _ROLLBACK_RECEIPT_TTL_SECONDS:
            return True
    return False


def _sweep_expired_receipts_unlocked() -> int:
    """Drop receipts whose attempt is long gone; return how many were dropped.

    #7862 round 7 (finding 3): elapsed age ALONE is not evidence that an
    attempt ended. The route carries no chat-start deadline, so a live
    registration callback whose worker is slow can hold a receipt past the TTL
    while its attempt is still very much running; sweeping it made the
    rejection un-restorable and turned the matching retry into an ordinary
    turn.

    The sweep therefore uses the more recent of the mint and the last claim,
    and an attempt that wants to be certain of survival reclaims its slot
    (``reclaim_goal_continuation_receipt``). A receipt that nobody has
    reclaimed past the TTL still goes, so storage stays bounded -- the bound
    is now backed by an attempt's own liveness report instead of by a clock
    alone.

    #7862 round 10 (finding 3): that liveness report is only advisory. The
    conclusive signal is the durable handoff: while a session still holds one,
    an attempt is provably in flight, so its receipt is kept regardless of age.
    This replaces "the route reclaims once and hopes the TTL covers it" with
    "the receipt lives exactly as long as the attempt does".

    There is still deliberately no global count cap and no per-session count
    cap: with 65 starts in flight for one session, a per-session cap of 4
    silently evicted the in-flight attempt's receipt (#7862 round 6).

    Callers must hold ``_LOCK``.
    """
    now = time.time()
    expired = []
    for key, record in _ROLLBACK_RECEIPTS.items():
        if _receipt_attempt_is_live(key[0]):
            # An attempt is provably in flight for this session. Age is not
            # evidence that it ended, so the receipt stays claimable for the
            # rejection rollback.
            continue
        # #7862 round 7 (finding 3): age is only conclusive when the attempt
        # has stopped reporting itself live. ``reclaim_goal_continuation_receipt``
        # refreshes this receipt's liveness, so whichever of the mint and the
        # last reclaim is NEWER is the stronger signal and dominates age. A
        # receipt nobody has reclaimed past the TTL is therefore an attempt that
        # stopped reporting, which is the abandonment this sweep takes as
        # conclusive -- the same bound round 6 established, without treating a
        # still-running start as finished.
        if now - max(_receipt_minted_at(record), _receipt_last_claimed_at(record)) > (
            _ROLLBACK_RECEIPT_TTL_SECONDS
        ):
            expired.append(key)
    for key in expired:
        _ROLLBACK_RECEIPTS.pop(key, None)
    if expired:
        logger.warning(
            "Rolled back %d goal-continuation receipt(s) that outlived their start attempt (ttl=%ds)",
            len(expired),
            _ROLLBACK_RECEIPT_TTL_SECONDS,
        )
    return len(expired)


def reclaim_goal_continuation_receipt(session_id: str, attempt_id: str = "") -> bool:
    """Report that this start attempt is still live; keep its receipt.

    #7862 round 7 (finding 3). The receipt's TTL is a bound for receipts whose
    attempt is gone, not a deadline the route enforces -- the chat path has no
    corresponding timeout, so a live attempt may span it. This is the positive
    liveness signal that closes that gap: the attempt calls it, the receipt's
    clock resets, and only a receipt nobody ever reclaims is swept.

    Returns True when a live receipt was refreshed. Never raises into the chat
    path.
    """
    sid = str(session_id or "").strip()
    attempt = str(attempt_id or "").strip()
    if not sid:
        return False
    with _LOCK:
        if attempt:
            keys = [(sid, attempt)] if (sid, attempt) in _ROLLBACK_RECEIPTS else []
        else:
            keys = [k for k in _ROLLBACK_RECEIPTS if k[0] == sid]
        if not keys:
            return False
        now = time.time()
        for key in keys:
            record = _ROLLBACK_RECEIPTS[key]
            record["_receipt_reclaimed_at"] = now
            # #7862 round 10 (finding 3): the handoff anchors the receipt's
            # survival, so the same liveness report has to refresh it. Without
            # this, an attempt that legitimately outlives the TTL would have its
            # receipt protected for one TTL and then lose it — the exact
            # regression this closes, just moved out by 15 minutes.
            handoff = _CONTINUATION_HANDOFF_TOKENS.get(key)
            if handoff is not None:
                handoff["_handoff_minted_at"] = now
        return True


def _record_continuation_handoff_unlocked(sid: str, record: dict, attempt_id: str) -> None:
    """Store a durable handoff for one start attempt; callers hold ``_LOCK``.

    #7862 round 7 (finding 2): the record a consume popped must have a durable
    owner before the start is admitted, so a crash between the consume and the
    pending-start write cannot strand the intent with no recoverable evidence.
    """
    attempt = str(attempt_id or "") or "attempt"
    stored = dict(record)
    stored["_handoff_attempt_id"] = attempt
    # #7862 round 10 (finding 3): the handoff is also the receipt's liveness
    # anchor, so it needs its own clock. An attempt that crashed without
    # discharging leaves the handoff behind forever otherwise, and the receipt
    # it protects would never be swept — an unbounded registry traded for a
    # bounded one. ``reclaim_goal_continuation_receipt`` refreshes this field
    # through the receipt path, so a live attempt keeps both alive.
    stored["_handoff_minted_at"] = time.time()
    _CONTINUATION_HANDOFF_TOKENS[(sid, attempt)] = stored
    # #7862 round 9 (CORE, finding "the 64-entry cap evicts the only durable
    # owner"): the cap is a memory bound, but the tokens ARE the durable
    # evidence -- an unconditional FIFO pop could evict the one outstanding
    # handoff a crash would need to restore. Evict only entries whose session
    # already has a live record or a retirement tombstone, i.e. tokens that are
    # provably spent. When every entry is still outstanding the dict is allowed
    # to exceed the bound: losing an in-flight continuation is worse than a
    # temporarily oversized registry.
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    while len(_CONTINUATION_HANDOFF_TOKENS) > _MAX_CONTINUATION_HANDOFFS:
        evictable = [
            key
            for key in _CONTINUATION_HANDOFF_TOKENS
            if key[0] in PENDING_GOAL_CONTINUATION_RECORDS
            or int(_TOMBSTONES.get(key[0]) or 0) > 0
        ]
        if not evictable:
            break
        _CONTINUATION_HANDOFF_TOKENS.pop(evictable[0], None)


def pop_goal_continuation_handoff(
    session_id: str, attempt_id: str = ""
) -> Optional[dict]:
    """Claim and remove this start attempt's durable handoff token.

    Returns the record the attempt handed off (prompt, generation,
    continuation id) or None when there is none. Used by the rejected-start
    rollback to restore a MATCHABLE intent, and by a cold restore to re-adopt
    an intent whose start was lost with the process. Never raises into the
    chat path.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    attempt = str(attempt_id or "").strip()
    with _LOCK:
        if attempt:
            key = (sid, attempt)
        else:
            session_keys = [k for k in _CONTINUATION_HANDOFF_TOKENS if k[0] == sid]
            key = session_keys[0] if session_keys else None
        if key is None or key not in _CONTINUATION_HANDOFF_TOKENS:
            return None
        claimed = dict(_CONTINUATION_HANDOFF_TOKENS.pop(key))
        claimed.pop("_handoff_attempt_id", None)
        # #7862 round 10: the handoff's liveness clock is internal bookkeeping,
        # exactly like the receipt's. Leaving it in the restored record would
        # put a wall-clock stamp into the durable intent, where a later load
        # would read it as intent metadata.
        claimed.pop("_handoff_minted_at", None)
        claimed.pop("_receipt_minted_at", None)
        claimed.pop("_receipt_reclaimed_at", None)
        claimed["attempt_id"] = key[1]
        return claimed


def discard_goal_continuation_handoff(session_id: str, attempt_id: str = "") -> bool:
    """Discharge this start attempt's handoff once its launch SUCCEEDED.

    #7862 round 7 (finding 2): the discharge is DURABLE, not just in-memory.
    A launch that succeeded owns the turn, so the handoff must stop being
    evidence of an interrupted start -- if it stayed in the registry payload, a
    later cold restore would resurrect a continuation that already ran. The in-memory
    entry and the registry snapshot commit together, exactly like the receipt
    discard that mirrors it on the same paths.

    #7862 round 9 (CORE, finding "an admitted attempt becomes claimable intent
    again"): returns whether the snapshot actually committed. A launch that
    succeeded while the discharge write failed leaves the handoff durable, so a
    cold restore would restore an intent whose turn already ran. The caller must
    be able to see that and reconcile at the next startup instead of assuming
    the discharge landed.

    Never raises into the chat path.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return False
    attempt = str(attempt_id or "").strip()
    with _LOCK:
        had = bool(attempt and (sid, attempt) in _CONTINUATION_HANDOFF_TOKENS)
        _discharged_record = (
            dict(_CONTINUATION_HANDOFF_TOKENS[(sid, attempt)]) if had else None
        )
        if attempt:
            _CONTINUATION_HANDOFF_TOKENS.pop((sid, attempt), None)
        else:
            for key in [k for k in _CONTINUATION_HANDOFF_TOKENS if k[0] == sid]:
                del _CONTINUATION_HANDOFF_TOKENS[key]
        if had:
            if _write_registry_unlocked(
                PENDING_GOAL_CONTINUATION_RECORDS,
                context=f"handoff-discharge sid={sid}",
            ):
                return True
            # The snapshot write failed, so the durable state still describes an
            # interrupted start. Put the in-memory token back so this process
            # agrees with disk -- dropping it here would make the failure
            # invisible to everything but the log, and the next discharge
            # attempt (or a startup reconciliation) needs the token to act on.
            # Report the failure: an admitted attempt whose handoff survives is
            # claimable intent again, so the caller must be able to see it.
            if attempt and _discharged_record is not None:
                _CONTINUATION_HANDOFF_TOKENS[(sid, attempt)] = _discharged_record
            logger.warning(
                "Goal continuation handoff for session %s could not be durably "
                "discharged; the attempt stays claimable until reconciled",
                sid,
            )
            return False
    return True


def _drop_continuation_handoffs_unlocked(sid: str) -> int:
    """Remove every durable handoff for *sid*; callers hold ``_LOCK``.

    #7862 round 9 (CORE): the handoff is the durable evidence that a start
    attempt consumed an intent. Three flows must clear it, and all three used
    to leave it behind:

    * **retirement** (``/goal clear``) — the intent is over, so its handoff is
      too. Leaving it let a cold restore adopt it and resurrect the cleared
      goal.
    * **a rejected-start rollback that restored the record** — the intent is
      live again as a RECORD; the handoff for the attempt that consumed it is
      spent. Leaving it let a successful retry followed by a restart restore
      the continuation a second time.
    * **an adoption that refused on a tombstone** — the handoff describes an
      intent the user explicitly cleared.

    Returns the number of in-memory tokens removed. The caller is responsible
    for the snapshot write, so this and the state it accompanies always land
    in one ``os.replace``.
    """
    keys = [key for key in _CONTINUATION_HANDOFF_TOKENS if key[0] == sid]
    for key in keys:
        _CONTINUATION_HANDOFF_TOKENS.pop(key, None)
    return len(keys)


def _record_rollback_receipt_unlocked(sid: str, record: dict, attempt_id: str) -> None:
    """Store a rollback receipt for one start attempt; callers hold ``_LOCK``."""
    attempt = str(attempt_id or "") or "attempt"
    stored = dict(record)
    stored["_receipt_minted_at"] = time.time()
    # Keyed by start attempt, so an in-flight attempt's receipt has its own
    # slot no matter how much unrelated traffic arrives.
    _ROLLBACK_RECEIPTS[(sid, attempt)] = stored
    _sweep_expired_receipts_unlocked()


def _write_registry_unlocked(records: dict, *, context: str = "") -> bool:
    """Atomically persist the full registry (unique tmp + fsync + replace).

    Writes ``records`` AND the durable tombstones in one payload: a clear must
    land on the same snapshot as the state it cleared, otherwise a crash
    between the two writes reintroduces exactly the resurrection #7862 round 6
    is about. Lock-free; callers must hold ``_LOCK``. Never raises: failures
    are recorded in ``_LAST_WRITE_ERROR`` and surfaced by
    ``durability_diagnostics()`` so durability claims stay observable.

    The RETURN VALUE is #7862 round 7: True only when ``os.replace`` actually
    swapped the payload into ``_PENDING_GOAL_FILE``. A caller must treat
    ``False`` as "this mutation did NOT become durable" -- a consume whose
    snapshot failed used to return "consumed" anyway, which dropped the
    in-memory record while the old bytes stayed on disk, so a restart restored
    a claimable record for a turn that had already been spent.
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
                "tombstones": dict(_TOMBSTONES),
                # #7862 round 7 (finding 2): the admitted-start handoffs, so a
                # crash between the consume and the pending-start write leaves
                # durable evidence of the continuations that were already spent
                # in memory. Internal bookkeeping keys are stripped first.
                "continuation_handoffs": {
                    f"{sid}\u0000{attempt}": {
                        k: v
                        for k, v in rec.items()
                        # Receipt bookkeeping never belongs in the handoff payload.
                        if not k.startswith("_receipt_")
                        # ``_handoff_attempt_id`` is redundant with the dict key.
                        # ``_handoff_minted_at`` IS persisted: it is the clock that
                        # tells a cold process whether the attempt is still live or
                        # abandoned (#7862 round 10). Dropping it would make every
                        # restored handoff look abandoned, and its receipt would be
                        # swept on the first sweep after a restart — the exact
                        # regression this round closes.
                        and k != "_handoff_attempt_id"
                    }
                    for (sid, attempt), rec in _CONTINUATION_HANDOFF_TOKENS.items()
                },
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
        return True
    except Exception as exc:
        _LAST_WRITE_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Failed to persist goal continuation registry (context=%s, generation=%d): %s",
            context or "-", _GENERATION, exc,
        )
        return False
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


def _load_file_raw() -> "tuple[dict[str, dict], dict[str, int], dict[tuple[str, str], dict]]":
    """Lock-free read of the on-disk registry -> (records, tombstones, handoffs).

    Missing file -> ({}, {}, {}); corrupt/unexpected -> empty mappings with the
    parse failure recorded in ``_LAST_LOAD_ERROR`` (never raises). A v1
    list-of-strings file is upgraded in memory to records with a blank prompt.

    ``handoffs`` are the #7862 round 7 (finding 2) admitted-start tokens: each
    is the consumption an in-flight chat start already spent, so a process that
    died between the consume and the pending-start write can still re-adopt the
    intent instead of restoring nothing. A v3 file has no such section; nothing
    can be re-adopted for it, which is the pre-round-7 behaviour.
    """
    global _LAST_LOAD_ERROR
    _LAST_LOAD_ERROR = None
    try:
        raw = _PENDING_GOAL_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return {}, {}, {}
    except (OSError, ValueError) as exc:
        _LAST_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("Goal continuation registry unreadable: %s", _LAST_LOAD_ERROR)
        return {}, {}, {}
    if isinstance(data, dict):
        file_generation = int(data.get("generation") or 0)
        raw_records = data.get("records")
        if not isinstance(raw_records, dict):
            _LAST_LOAD_ERROR = (
                f"registry has no records mapping ({type(raw_records).__name__})"
            )
            return {}, {}, {}
        raw_tombstones = data.get("tombstones")
        if not isinstance(raw_tombstones, dict):
            raw_tombstones = {}
        tombstones: "dict[str, int]" = {}
        for sid, gen in raw_tombstones.items():
            try:
                tombstones[str(sid)] = int(gen or 0)
            except (TypeError, ValueError):
                continue
        handoffs: "dict[tuple[str, str], dict]" = {}
        raw_handoffs = data.get("continuation_handoffs")
        if isinstance(raw_handoffs, dict):
            for key, rec in raw_handoffs.items():
                if not isinstance(rec, dict):
                    continue
                sid, _, attempt = str(key).partition("\u0000")
                if not sid or not attempt:
                    continue
                handoffs[(sid, attempt)] = {
                    "prompt": str(rec.get("prompt") or ""),
                    "generation": int(rec.get("generation") or file_generation),
                    "created_at": float(rec.get("created_at") or time.time()),
                    "reason": str(rec.get("reason") or "goal_continue"),
                    "continuation_id": str(rec.get("continuation_id") or ""),
                    # #7862 round 10: the liveness clock must survive the load.
                    # This builder is a whitelist, so a field not named here is
                    # dropped even though it is persisted — and a restored
                    # handoff with no clock reads as abandoned, which sweeps the
                    # receipt it is supposed to protect.
                    "_handoff_minted_at": float(rec.get("_handoff_minted_at") or 0.0),
                }
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
        return out, tombstones, handoffs
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
        }, {}, {}
    _LAST_LOAD_ERROR = f"unexpected registry shape: {type(data).__name__}"
    return {}, {}, {}


def load_pending_goal_continuations() -> dict:
    """Return the durable records {session_id: record} (locked, never raises)."""
    with _LOCK:
        records, _tombstones, _handoffs = _load_file_raw()
        return records


def load_goal_continuation_tombstones() -> dict:
    """Return the durable clear tombstones {session_id: generation}."""
    with _LOCK:
        _records, tombstones, _handoffs = _load_file_raw()
        return dict(tombstones)


def arm_pending_goal_continuation(
    session_id: str,
    continuation_prompt: str = "",
    reason: str = "goal_continue",
    continuation_id: str = "",
) -> bool:
    """Arm durable intent for one session: mutate + snapshot under ONE lock.

    Adds the session to ``PENDING_GOAL_CONTINUATION`` AND stores the canonical
    continuation prompt + generation in ``PENDING_GOAL_CONTINUATION_RECORDS``,
    then persists. Never raises into the chat path; write failures are
    observable via ``durability_diagnostics()``.

    Returns True only when the snapshot was durably replaced. A False return
    means the intent is NOT durable (the registry bytes on disk predate this
    arm), so a caller that needs to claim durability must not pretend the arm
    landed (#7862 round 7).

    ``continuation_id`` is the opaque token the ``goal_continue`` SSE event
    hands the browser so the queued automatic continuation can be matched by
    identity instead of by text (#7862 core finding): text identity breaks
    when a ``/use <skill>`` directive wraps the queued send.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return False
    prompt = "" if continuation_prompt is None else str(continuation_prompt)
    with _LOCK:
        generation = _next_generation_unlocked()
        # A fresh intent supersedes any older one: drop a stale rollback
        # receipt for this session so a late rejected-start rollback can never
        # resurrect the generation the new intent replaced. The session's
        # retirement stamp is cleared too: this arm IS the new intent, so a
        # rejected start for it is exactly what a rollback is for.
        _drop_rollback_receipt_unlocked(sid)
        # A fresh arm supersedes an older clear: the tombstone is dropped so a
        # rejected start for THIS intent can roll back normally. Clearing it
        # (rather than leaving it) is what keeps the tombstone's meaning narrow
        # -- "the intent as of generation N was cancelled" -- instead of "this
        # session is cancelled forever".
        _RETIRED_GENERATIONS.pop(sid, None)
        _TOMBSTONES.pop(sid, None)
        PENDING_GOAL_CONTINUATION.add(sid)
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = {
            "prompt": prompt,
            "generation": generation,
            "created_at": time.time(),
            "reason": reason,
            "continuation_id": str(continuation_id or ""),
        }
        return _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"arm sid={sid} reason={reason}",
        )


def retire_pending_goal_continuation(
    session_id: str,
    reason: str = "consumed",
) -> bool:
    """Retire durable intent for exactly one session (mutate + snapshot).

    Removes the session from the marker set AND deletes its on-disk record, so
    a later startup/repair can never re-arm a consumed continuation. The
    reason is appended to the bounded ``_RETIRED_LOG`` diagnostics.

    Returns True only when the snapshot was durably replaced. #7862 round 7:
    a ``False`` return means the record is gone from memory but still claimable
    on disk, so a caller must NOT report a durable retirement -- the clearing
    operation has to surface the failure instead of claiming success.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return False
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
        # #7862 round 9 (CORE, finding "a cleared goal comes back after the
        # next restart"): retire must also discharge the session's durable
        # HANDOFFS, in this same snapshot. A handoff is evidence that a start
        # attempt consumed an intent; ``/goal clear`` ends that intent, so
        # leaving the handoff behind let a later cold restore adopt it and
        # resurrect the continuation the user just stopped. Adoption only
        # compared tombstones against the RECORD's generation, and a
        # handoff-restored record re-derives that generation at adoption
        # time -- so the tombstone never matched.
        _drop_continuation_handoffs_unlocked(sid)
        # Durable tombstone: the clear must outlive this process AND any number
        # of unrelated retirements, so it is written to the same snapshot as
        # the state it cleared. The in-memory stamp below stays as the
        # same-process fast path.
        _RETIRED_GENERATIONS.pop(sid, None)
        _RETIRED_GENERATIONS[sid] = generation
        _TOMBSTONES[sid] = max(generation, int(_TOMBSTONES.get(sid) or 0))
        while len(_TOMBSTONES) > _MAX_TOMBSTONES:
            _TOMBSTONES.pop(next(iter(_TOMBSTONES)))
        # A fresh arm is NOT a retirement, so the session's generation stamp is
        # left alone: an older receipt must stay blocked.
        committed = _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"retire sid={sid} reason={reason}",
        )
        _RETIRED_LOG.append(
            {"session_id": sid, "reason": reason, "at": time.time()}
        )
        return committed


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

    If the turn is not the continuation this is a no-op and the intent stays
    pending; if the turn IS the continuation but the durable removal cannot be
    committed, the intent is left in place as well and the return is False, so
    the caller must not acknowledge the consumption.

    Returns one of the ``CONSUME_*`` constants: ``CONSUME_NOT_MATCHING`` when the
    turn is not this continuation (the intent stays pending), ``CONSUME_COMMITTED``
    when it was consumed and durably removed, and ``CONSUME_COMMIT_FAILED`` when
    the turn IS the continuation but the durable removal did not commit — the
    caller must refuse admission rather than treat it as an ordinary turn. Never
    raises into the chat path.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return CONSUME_NOT_MATCHING
    with _LOCK:
        record = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)
        if record is None:
            # No durable record: a bare in-memory marker (legacy path or a
            # v1 file upgraded without prompt text) cannot be matched safely.
            # Leave it alone rather than retire an intent we cannot verify --
            # expiry bounds it, and swallowing an unrelated turn is the exact
            # data-loss bug this function exists to prevent.
            return CONSUME_NOT_MATCHING
        recorded_id = str(record.get("continuation_id") or "")
        request_id = str(continuation_id or "")
        if recorded_id and request_id and recorded_id != request_id:
            # A token was carried, but not THIS continuation's token: another
            # (already consumed or restarted) generation. Treat it like any
            # other non-matching turn.
            return CONSUME_NOT_MATCHING
        # Identity narrows the candidate set to this exact continuation; the
        # recorded prompt still has to be present (verbatim or wrapped by the
        # known forced-skill envelope), so a replayed token carrying unrelated
        # text is never swallowed.
        if not _continuation_text_matches(record.get("prompt") or "", incoming_text):
            return CONSUME_NOT_MATCHING
        _next_generation_unlocked()
        PENDING_GOAL_CONTINUATION.discard(sid)
        popped = PENDING_GOAL_CONTINUATION_RECORDS.pop(sid)
        # #7862 round 8 (CORE, finding 2): the record removal and the handoff that
        # replaces it must land in ONE os.replace. Round 7 wrote them in two
        # passes (record gone at the first replace, handoff added at the second),
        # so a process loss between them left records=[] handoffs=[] and the retry
        # ran as an ordinary turn; and when the second write failed the handoff was
        # dropped while the consume still returned True. Both halves are staged in
        # memory first and committed together, so the durable snapshot shows either
        # "record present, no handoff" or "record absent, handoff present" — never
        # the empty middle.
        _record_rollback_receipt_unlocked(sid, record, attempt_id)
        _record_continuation_handoff_unlocked(sid, record, attempt_id)
        if not _write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context=f"consume+handoff sid={sid}",
        ):
            # Nothing became durable: undo BOTH halves so this process still
            # describes the pre-consume state, and refuse the admission.
            # The handoff key must be normalised the same way the writer
            # normalises it, or an empty attempt_id leaves the token behind
            # under a key this pop cannot reach.
            _CONTINUATION_HANDOFF_TOKENS.pop(
                (sid, str(attempt_id or "") or "attempt"), None
            )
            _drop_rollback_receipt_unlocked(sid)
            PENDING_GOAL_CONTINUATION.add(sid)
            PENDING_GOAL_CONTINUATION_RECORDS[sid] = popped
            logger.warning(
                "Refused to consume goal continuation for session %s: the "
                "durable record could not be removed atomically with its handoff",
                sid,
            )
            return CONSUME_COMMIT_FAILED
        _RETIRED_LOG.append({"session_id": sid, "reason": "consumed", "at": time.time()})
        return CONSUME_COMMITTED


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
        # Durable tombstone check: has this session's intent been explicitly
        # retired since this receipt was minted? ``_TOMBSTONES`` is loaded from
        # disk and survives both a restart and unlimited unrelated
        # retirements, unlike the in-memory stamp that the 64-entry window
        # dropped (#7862 round 6).
        tombstone = _TOMBSTONES.get(sid)
        legacy_stamp = _RETIRED_GENERATIONS.get(sid)
        retired_at = max(
            int(tombstone or 0),
            int(legacy_stamp or 0),
        )
        if retired_at and int(record.get("generation") or 0) <= retired_at:
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
        # #7862 round 9 (CORE, finding "successful retries leave claimable
        # handoffs"): the intent is live again as a RECORD, so the handoff for
        # the attempt that consumed it is spent. Keeping it let a successful
        # retry followed by a restart restore the continuation a SECOND time:
        # adoption re-armed the record from the stale handoff even though the
        # retry had already consumed it. Cleared in the same snapshot as the
        # restore, so the durable state never shows both.
        _drop_continuation_handoffs_unlocked(sid)
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
    live (a live in-memory record always wins over disk), loads the durable
    clear tombstones, and adopts the durable continuation handoffs. Returns the
    count of sessions restored. Never raises.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with _LOCK:
        disk, disk_tombstones, disk_handoffs = _load_file_raw()
        # Baseline the generation counter ABOVE every generation on disk
        # (records and tombstones alike). A cold process counts from 0, so a
        # restored record carrying generation 41 would otherwise compare as
        # OLDER than the fresh clear at generation 3 -- and the clear would be
        # forgotten, resurrecting a goal the user had cancelled (#7862 round 6,
        # scenario 4). Starting above the persisted maximum makes every later
        # generation strictly newer than anything a previous process wrote.
        highest = max(
            [int(rec.get("generation") or 0) for rec in disk.values()]
            + [int(gen or 0) for gen in disk_tombstones.values()]
            + [int(gen or 0) for gen in (r.get("generation") or 0 for r in disk_handoffs.values())]
            + [0]
        )
        global _GENERATION
        if highest > _GENERATION:
            _GENERATION = highest
            logger.info(
                "Goal continuation registry: generation baseline raised to %d from disk",
                highest,
            )
        _TOMBSTONES.clear()
        _TOMBSTONES.update(disk_tombstones)
        _RETIRED_GENERATIONS.clear()
        _RETIRED_GENERATIONS.update(disk_tombstones)
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
        # #7862 round 7 (finding 2): adopt the durable handoffs. Each one is an
        # in-flight chat start whose consume already spent the durable record;
        # if the process died before the pending-start write, this is the ONLY
        # evidence that the continuation was admitted. Re-arm it so the retry
        # can still consume it instead of running as an ordinary turn.
        adopted = 0
        for (sid, attempt), record in disk_handoffs.items():
            if sid in PENDING_GOAL_CONTINUATION_RECORDS:
                # The intent is already live as a record (a rollback restored
                # it, or an earlier adoption did). Its handoff is spent; drop
                # it rather than leaving a second claimable copy behind
                # (#7862 round 9).
                _drop_continuation_handoffs_unlocked(sid)
                continue
            if (sid, attempt) in _CONTINUATION_HANDOFF_TOKENS:
                continue
            # #7862 round 9 (CORE, finding "a cleared goal comes back after
            # the next restart"): a handoff is only evidence of an interrupted
            # start while the intent it describes still exists. If the session
            # was retired (``/goal clear``, session delete, expiry) after this
            # handoff was written, adopting it resurrects a goal the user
            # explicitly stopped -- the record's own generation cannot catch
            # this, because a handoff-restored record derives its generation
            # at adoption time. Compare against the handoff's ORIGINAL
            # generation, which the consume stamped when it spent the intent.
            handoff_generation = int(record.get("generation") or 0)
            tombstone = int(
                max(
                    int(_TOMBSTONES.get(sid) or 0),
                    int(_RETIRED_GENERATIONS.get(sid) or 0),
                )
            )
            if tombstone and handoff_generation and handoff_generation <= tombstone:
                logger.info(
                    "Goal continuation registry: refusing to adopt handoff for "
                    "session %s (generation %d <= retired generation %d)",
                    sid,
                    handoff_generation,
                    tombstone,
                )
                _drop_continuation_handoffs_unlocked(sid)
                _RETIRED_LOG.append(
                    {
                        "session_id": sid,
                        "reason": "handoff_adoption_refused_retired",
                        "at": time.time(),
                    }
                )
                continue
            _CONTINUATION_HANDOFF_TOKENS[(sid, attempt)] = dict(record)
            # The intent itself is gone from disk by construction (the consume
            # removed it), so restore the record for the matching retry: this
            # is what makes the handoff reconcilable rather than merely
            # informative.
            PENDING_GOAL_CONTINUATION.add(sid)
            PENDING_GOAL_CONTINUATION_RECORDS[sid] = dict(record)
            adopted += 1
            restored += 1
        if adopted:
            logger.info(
                "Goal continuation registry: adopted %d durable continuation handoff(s) "
                "from an interrupted start",
                adopted,
            )
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
