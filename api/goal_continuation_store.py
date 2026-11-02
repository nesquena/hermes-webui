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

Re-gate (2026-10-27) adds the process-wide transaction model:

- every registry writer runs inside ``_registry_transaction()`` — the
  in-process lock PLUS a cross-process ``flock`` — and reloads the durable
  state under it (``_merge_disk_state_unlocked``), so a second process can
  neither re-spend an intent another process consumed nor drop a peer's arm
  from its snapshot;
- a restore publishes master's prompt/token mirror
  (``PENDING_GOAL_CONTINUATION_PROMPTS``) together with the marker under
  their shared lock, and filters sessions the WebUI deleted while the
  process was down;
- only ``worker_started``, ``completed`` or worker-written journal rows
  count as execution — a route-generated compensation row does not.
"""

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import deque
from typing import Deque, NoReturn, Optional

from api.config import STATE_DIR

logger = logging.getLogger("api.goal_continuation_store")

try:  # POSIX cross-process locking; native Windows falls back below.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

_PENDING_GOAL_FILE = STATE_DIR / "pending_goal_continuations.json"
# #7862 re-gate (2026-10-27, CORE finding 1): the cross-process registry
# lock. The module ``_LOCK`` is a threading lock — it serialises WRITERS
# INSIDE one process only. Two WebUI processes (a rolling restart, a second
# worker) each hold their own in-memory copy of the registry, so a consume
# that trusts its own memory can spend an intent another process already
# spent: both starts answered 200 with ``goal_related=True`` and the
# continuation ran twice. Every registry transaction therefore takes this
# file lock as well, reloads the durable state under it, and validates the
# token and generation before committing.
_REGISTRY_LOCK_FILE = STATE_DIR / "pending_goal_continuations.lock"
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

# #7862 re-gate round 15 (BRICK): ``_LAST_LOCK_ERROR`` makes a refusal to
# take the cross-process registry lock observable (like the load/write error
# diagnostics) instead of a silent fallback.
_LAST_LOCK_ERROR: Optional[str] = None

# #7862 re-gate round 15 (CORE 4): the persisted global generation of the
# last successful ``_load_file_raw``. A cold process counts generations from
# 0, so every transaction baselines ``_GENERATION`` above this value AND
# every record/handoff/tombstone generation on disk before allocating the
# next mutation's generation — otherwise process B's ``/goal clear`` at its
# own generation 1 is incomparable with process A's receipt generation 3,
# and A's rejected-start rollback resurrects a goal the user cleared.
_LAST_FILE_GENERATION: int = 0


class _RegistryLockUnavailable(Exception):
    """The cross-process registry lock could not be acquired.

    #7862 re-gate round 15 (CORE finding 1, BRICK): the registry transaction
    must never fall back to an unlocked write — without the file lock a peer
    process's mutation cannot be serialised, which is the exact race round 12
    added the lock for. Public mutators translate this into their \"refused\"
    return value, so an ordinary chat start (which never reaches the lock —
    see ``record_goal_continuation_stream``/``discard_goal_continuation_handoff``)
    and the delete path stay healthy while a continuation mutation honestly
    refuses.
    """


@contextlib.contextmanager
def _registry_file_lock():
    """Cross-process (file) lock around one registry transaction.

    #7862 re-gate (2026-10-27, CORE finding 1): the thread lock above cannot
    see another PROCESS's writers, and the reviewers replayed exactly that —
    two processes each restored the same continuation into their own memory
    and both admitted it. ``flock`` on a dedicated lock file (a different
    open file description per acquisition, so it also excludes other threads
    in this process) makes the critical section process-wide.

    The lock file is separate from the registry payload: it must survive the
    payload's ``os.replace`` (the inode is swapped, the lock is not), and a
    missing state directory is created on demand.

    #7862 re-gate round 15 (CORE finding 1, BRICK): a filesystem without a
    working ``flock`` (ENOSYS/ENOLCK on some NFS mounts and container
    overlays) used to propagate out of BOTH the ``os.open`` fallback (which
    only covered the open) and the lock call itself, 500ing every chat start.
    Now a lock-acquisition failure raises ``_RegistryLockUnavailable``: the
    public mutators refuse the continuation mutation — never an unlocked
    write — and the chat-start path only reaches the lock when a real
    admission exists (``record_*``/``discard_*`` return early otherwise).
    When ``fcntl`` itself is unavailable (native Windows) the lock degrades
    to the in-process lock only — the same coverage the registry had before
    this round.
    """
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_REGISTRY_LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        _refuse_registry_lock("open", exc)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            _refuse_registry_lock("flock", exc)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _refuse_registry_lock(phase: str, exc: Exception) -> "NoReturn":
    """Record and raise the lock-unavailable refusal; never returns."""
    global _LAST_LOCK_ERROR
    _LAST_LOCK_ERROR = f"{phase}: {type(exc).__name__}: {exc}"
    logger.error(
        "Goal continuation registry: the cross-process lock could not be "
        "acquired (%s); refusing the registry mutation instead of writing "
        "unlocked",
        _LAST_LOCK_ERROR,
        exc_info=True,
    )
    raise _RegistryLockUnavailable(_LAST_LOCK_ERROR) from exc


@contextlib.contextmanager
def _registry_transaction():
    """The full transaction scope: thread lock + cross-process file lock."""
    with _LOCK:
        with _registry_file_lock():
            yield


def _retirement_generation_unlocked(sid: str) -> int:
    """The generation at which *sid*'s intent was retired; 0 when it was not.

    Callers must hold ``_LOCK``. Both the durable tombstone and the legacy
    in-memory stamp are consulted, exactly like the handoff adoption and the
    rollback restore do, so every reader honours ONE retirement authority.
    """
    return max(
        int(_TOMBSTONES.get(sid) or 0),
        int(_RETIRED_GENERATIONS.get(sid) or 0),
    )


def _record_is_retired(sid: str, record: dict) -> bool:
    """True when a retirement newer than *record* covers it.

    #7862 re-gate round 16 (CORE finding 3): plain-record restoration and the
    merge adoption both checked only deleted-session membership, so a record
    that outlived its own clear was republished. The handoff adoption already
    compared the handoff's ORIGINAL generation against the tombstone; a record
    carries that generation too, so the same comparison applies.

    A generation-less record (a v1 upgrade) has nothing to compare and is never
    treated as retired: it is assigned a fresh generation at restore time.
    Callers must hold ``_LOCK``.
    """
    generation = int(record.get("generation") or 0)
    retired_at = _retirement_generation_unlocked(sid)
    return bool(retired_at and generation and generation <= retired_at)


def _dirty_restore_survives_the_merge(sid: str, record: dict, fence: dict) -> bool:
    """True when the failed-restore exemption still covers *record*.

    #7862 re-gate round 16 (CORE finding 3). The exemption exists so a warm
    ENOSPC rollback restore is not dropped by the next unrelated transaction's
    "absent on disk => gone" rule.

    The fence is the ATTEMPT-SCOPED identity of the restored intent: the
    continuation token is minted per intent and the generation per arm, and
    only the attempt that consumed that intent holds the receipt that produced
    this record -- so "same token AND same generation" is exactly "the record
    this attempt restored". Anything else (a re-arm, a peer's newer intent) is
    a different intent and is NOT covered.

    The exemption also ends the moment a retirement newer than that generation
    exists. Without that, a peer's clear was imported while the dirty record
    was unconditionally retained, and the next unrelated snapshot republished a
    goal the user had already stopped.

    Callers must hold ``_LOCK``.
    """
    generation = int(record.get("generation") or 0)
    if generation != int(fence.get("generation") or 0):
        return False
    if str(record.get("continuation_id") or "") != str(
        fence.get("continuation_id") or ""
    ):
        return False
    return not _record_is_retired(sid, record)


def _merge_disk_state_unlocked() -> None:
    """Reload the durable state into memory; callers hold a transaction.

    #7862 re-gate (2026-10-27, CORE finding 1): every registry writer used
    to snapshot ITS OWN in-memory dict, so a process that had not seen a
    peer's commit silently dropped it on its next write, and a consume
    trusted a stale in-memory copy of a record another process had already
    spent. Under the file lock the disk state is reloaded and merged:

    * a peer's tombstone is folded in, so a retirement committed elsewhere
      is honoured here;
    * an in-memory record ABSENT from disk was consumed or retired by a
      peer — the stale copy is dropped, which is what makes the second
      process's consume answer ``CONSUME_NOT_MATCHING`` instead of
      re-spending the intent;
    * every surviving in-memory record is validated against the durable
      copy by token AND generation; a mismatch means the memory is stale
      and disk is authoritative.

    #7862 re-gate round 15 (CORE findings 1-4) extends the merge:

    * an unreadable/corrupt registry (``_LAST_LOAD_ERROR``) is NOT treated
      as an empty disk — that wiped live memory and persisted the wipe.
      The merge stops before any drop/adopt, and the next successful write
      repairs the file from memory;
    * a peer's HANDOFF is merged into ``_CONTINUATION_HANDOFF_TOKENS`` the
      same way: disk handoffs were loaded but ignored, so an unrelated
      writer's snapshot silently erased a peer's in-flight admission and a
      cold restore lost the continuation. With the merge, every snapshot
      serialises the complete durable handoff table. An in-memory token
      stays authoritative (a pop followed by a same-transaction restore
      write is what retires it durably);
    * ``_GENERATION`` is baselined above the persisted global generation
      and every record/handoff/tombstone generation on disk, so a cold
      process's next mutation can never mint a generation that compares
      older than another process's receipt;
    * a merge-adopted peer record publishes master's prompt/token mirror
      (``_publish_continuation_mirror_unlocked``) together with the marker,
      or the /goal orphan sweep strips the marker;
    * a session whose rollback-restore write failed (warm-path ENOSPC) is
      exempt from the \"absent on disk => gone\" drop until its record lands
      -- but only while the durable state still describes the SAME intent: a
      newer retirement tombstone ends the exemption (round 16, CORE finding 3);
    * the COMPLETE durable handoff table is reconciled per key, not unioned: a
      key disk carries is authoritative (an updated stream binding included), a
      locally retained key disk no longer carries was discharged or revoked by
      a peer and is dropped, and a token this process deliberately withheld
      from disk (an adoption conversion) is kept (round 16, CORE findings 1-2);
    * a durable record covered by a retirement tombstone is not adopted: the
      registry retirement is the same authority the handoff adoption honours.
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    disk, disk_tombstones, disk_handoffs = _load_file_raw()
    if _LAST_LOAD_ERROR:
        # Corrupt/unreadable registry: the disk is NOT evidence that records
        # are gone. Refuse to drop or adopt anything; live memory stays
        # authoritative and the next successful snapshot repairs the file.
        logger.warning(
            "Goal continuation registry: the durable state could not be "
            "read (%s); keeping live memory untouched instead of treating "
            "the unreadable file as an empty registry",
            _LAST_LOAD_ERROR,
        )
        return
    # #7862 re-gate round 15 (CORE finding 4): baseline the generation
    # counter ABOVE every generation on disk — the file's own global
    # generation, records, tombstones and handoffs alike. A cold process
    # counts from 0, so a retirement minted at "its own generation 1" would
    # otherwise stay incomparable with another process's receipt generation
    # 3, and a late rejected-start rollback would resurrect a goal the user
    # cleared. Every transaction re-baselines, so each mutation's generation
    # is strictly newer than anything any process has persisted.
    highest = max(
        [int(_LAST_FILE_GENERATION or 0)]
        + [int(rec.get("generation") or 0) for rec in disk.values()]
        + [int(gen or 0) for gen in disk_tombstones.values()]
        + [int(r.get("generation") or 0) for r in disk_handoffs.values()]
        + [0]
    )
    global _GENERATION
    if highest > _GENERATION:
        _GENERATION = highest
    for sid, gen in disk_tombstones.items():
        try:
            gen_i = int(gen or 0)
        except (TypeError, ValueError):
            continue
        if gen_i > int(_TOMBSTONES.get(sid) or 0):
            _TOMBSTONES[sid] = gen_i
    for sid in [s for s in PENDING_GOAL_CONTINUATION_RECORDS if s not in disk]:
        # #7862 re-gate round 15 (should-fix, warm-path ENOSPC): a rollback
        # whose restore snapshot could not commit left a live record with no
        # disk copy. Dropping it here ("absent on disk => gone") made the
        # warm retry run as an ordinary turn; the session stays exempt until
        # its record lands on disk again.
        #
        # #7862 re-gate round 16 (CORE finding 3): the exemption is FENCED by
        # the token and generation the restore put in memory, and a newer
        # retirement tombstone ends it. Without that fence a peer's clear was
        # imported while the dirty record was unconditionally retained, and
        # the next unrelated snapshot republished a cleared goal next to its
        # own newer tombstone.
        fence = _RESTORE_WRITE_FAILED_SESSIONS.get(sid)
        if fence is not None and _dirty_restore_survives_the_merge(
            sid, PENDING_GOAL_CONTINUATION_RECORDS.get(sid) or {}, fence
        ):
            continue
        _RESTORE_WRITE_FAILED_SESSIONS.pop(sid, None)
        # A peer consumed or retired this intent: the durable state is the
        # truth, and this stale copy must not be re-spent or republished.
        PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)
        PENDING_GOAL_CONTINUATION.discard(sid)
        _RETIRED_GENERATIONS.pop(sid, None)
    for sid, record in disk.items():
        # #7862 re-gate round 16 (CORE finding 3): a durable record covered by
        # a retirement tombstone describes an intent the user explicitly
        # stopped. Adopting it here is what republished a cleared goal; the
        # same authority the handoff adoption already honours applies to plain
        # records. A v1-upgraded record (generation 0) has no generation to
        # compare and is assigned one below, exactly like the adoption does.
        if _record_is_retired(sid, record):
            PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)
            PENDING_GOAL_CONTINUATION.discard(sid)
            _RETIRED_GENERATIONS.pop(sid, None)
            continue
        if sid in _RESTORE_WRITE_FAILED_SESSIONS:
            # The warm-ENOSPC exemption is satisfied: the record IS on disk.
            _RESTORE_WRITE_FAILED_SESSIONS.pop(sid, None)
        live = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)
        if live is None:
            # A peer armed an intent this process never saw: adopt it so
            # this writer's snapshot cannot drop it — and publish master's
            # prompt/token mirror with the marker, exactly like every other
            # adoption, or the /goal orphan sweep strips the marker and the
            # resume continue loses its identity admission (round 15
            # should-fix).
            _publish_continuation_mirror_unlocked(sid, record)
            PENDING_GOAL_CONTINUATION_RECORDS[sid] = dict(record)
            continue
        if (
            str(live.get("continuation_id") or "")
            != str(record.get("continuation_id") or "")
            or int(live.get("generation") or 0)
            != int(record.get("generation") or 0)
        ):
            # Token or generation drifted: the durable copy is authoritative.
            _publish_continuation_mirror_unlocked(sid, record)
            PENDING_GOAL_CONTINUATION_RECORDS[sid] = dict(record)
    # #7862 re-gate round 15 (CORE finding 3): merge the COMPLETE durable
    # handoff table. Before, disk handoffs were loaded but ignored and
    # snapshots serialised only local tokens, so process B's unrelated write
    # erased process A's in-flight handoff and a cold restore lost A's
    # continuation.
    #
    # #7862 re-gate round 16 (CORE finding 2): that merge only ADDED keys
    # missing locally, which is a union -- and a union lets a stale local copy
    # undo a peer's committed change. It is now a RECONCILIATION, per key:
    #
    #   * disk carries the key -> disk is authoritative. A key this process
    #     cached before the owner bound its stream identity (or before the
    #     owner discharged it after the launch) is REPLACED by the durable
    #     copy, so an unrelated snapshot can neither drop the stream identity
    #     nor republish a spent admission;
    #   * disk does NOT carry the key and this process still holds it -> a peer
    #     discharged or revoked it durably. The local copy is stale evidence
    #     for an admission that no longer exists and is dropped, so it cannot
    #     be republished. The one exception is a token this process
    #     deliberately withheld from disk -- an adoption conversion, whose
    #     in-memory copy is the receipt's liveness anchor (round 10);
    #   * a takeover therefore REVOKES the attempt it displaced: the adoption
    #     removed the durable handoff, so the old owner's next merge drops its
    #     local copy and its stream binding refuses instead of launching a
    #     second goal worker (round 16, CORE finding 1).
    for key, record in disk_handoffs.items():
        _CONTINUATION_HANDOFF_TOKENS[key] = dict(record)
    for key in [k for k in _CONTINUATION_HANDOFF_TOKENS if k not in disk_handoffs]:
        if _CONTINUATION_HANDOFF_TOKENS[key].get("_handoff_adopted_into_record"):
            # Withheld from disk by design, not lost: keep the conversion
            # marker and the token it anchors.
            continue
        del _CONTINUATION_HANDOFF_TOKENS[key]
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
# #7862 re-gate CORE: sessions whose durable handoff has already been CONVERTED
# into a restored record. The in-memory token stays (``_receipt_attempt_is_live``
# anchors a rollback receipt to it), but the durable copy must not: a handoff
# still on disk is re-adopted by the next cold restore, resurrecting a
# continuation the retry already spent.
_ADOPTED_HANDOFF_SESSIONS: "set[str]" = set()
_MAX_CONTINUATION_HANDOFFS = 64
# #7862 re-gate round 15 (should-fix): sessions whose rollback-restore
# snapshot write FAILED (warm-path ENOSPC). The record is live in memory but
# absent on disk, so the next unrelated transaction's merge must not drop it
# ("absent on disk => gone") — that is what turned the warm retry into an
# ordinary turn. Exempt until the record lands on disk again.
#
# #7862 re-gate round 16 (CORE finding 3): the exemption is a FENCE, not a
# session-wide pardon. It records the continuation token AND the generation of
# the intent the restore put in memory, so a merge can tell "the same dirty
# restore, still waiting to land" from "a peer retired this intent at a newer
# generation since". Retaining it unconditionally let a cleared goal come back:
# the merge imported the peer's tombstone, kept the dirty record, and the next
# unrelated snapshot republished it next to the newer tombstone.
_RESTORE_WRITE_FAILED_SESSIONS: "dict[str, dict]" = {}


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

    #7862 re-gate round 16 (CORE finding 1): the handoff's clock is now a
    DURABLE OWNERSHIP LEASE, and this is its renewal. The route explicitly
    allows an admission to outlive the reclaim window, so a liveness report
    that refreshed only this process's dictionaries left the durable clock
    stale: a cold restore read the expired lease as proof the owner had died
    and took the admission over while the owner was still running. The renewal
    is therefore committed in the same registry transaction as every other
    mutation, so a peer's restore sees an owner that is still reporting.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

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
        lease_keys = [k for k in keys if k in _CONTINUATION_HANDOFF_TOKENS]
    if not lease_keys:
        return True
    try:
        with _registry_transaction():
            # Reload under the cross-process lock: a peer may have discharged
            # or taken the admission over since the in-memory refresh above.
            _merge_disk_state_unlocked()
            renewed = [k for k in lease_keys if k in _CONTINUATION_HANDOFF_TOKENS]
            for key in renewed:
                _CONTINUATION_HANDOFF_TOKENS[key]["_handoff_minted_at"] = now
            if not renewed:
                logger.warning(
                    "Goal continuation registry: the admission for session %s "
                    "(attempt %s) no longer exists durably, so its ownership "
                    "lease could not be renewed",
                    sid,
                    lease_keys[0][1],
                )
                return True
            if not _write_registry_unlocked(
                PENDING_GOAL_CONTINUATION_RECORDS,
                context=f"lease-renew sid={sid} attempt={lease_keys[0][1]}",
            ):
                # The lease stays stale on disk, which only matters if this
                # attempt then stalls past the window without renewing again.
                logger.warning(
                    "Goal continuation registry: the ownership lease for session "
                    "%s (attempt %s) could not be renewed durably",
                    sid,
                    lease_keys[0][1],
                )
    except _RegistryLockUnavailable:
        logger.error(
            "Goal continuation registry: the ownership lease for session %s "
            "could not be renewed: the cross-process lock is unavailable",
            sid,
        )
    return True


def record_goal_continuation_stream(
    session_id: str, attempt_id: str, stream_id: str
) -> bool:
    """Bind this admission's stream identity to its handoff; return durability.

    #7862 re-gate CORE (Oct 10, "failed durable discharge is still ignored
    after launch"): the reviewer's exact fix was to persist the admission
    with its attempt/stream identity BEFORE worker execution, so recovery
    can reconcile against that identity instead of restoring admitted turns
    as pending intent.

    The handoff token already IS the durable admission (the consume writes
    it in the same snapshot that removes the record). What it lacked was the
    stream the admitted attempt actually ran under. With the stream id on
    the token, a launch whose discharge write failed is RECONCILABLE rather
    than merely visible: the turn executed, and a cold restore can see that.

    The write is the same atomic snapshot as every other mutation, so the
    stream id is durable the moment this returns True. A False return means
    it did not land — the caller treats the admission as unsettled.

    Never raises into the chat path.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    stream = str(stream_id or "").strip()
    if not sid or not stream:
        return False
    attempt = str(attempt_id or "").strip()
    # #7862 re-gate round 15 (CORE finding 1, BRICK): an ordinary start with
    # NO admission must never touch the cross-process lock — ``flock`` can
    # raise ENOSYS/ENOLCK on NFS mounts and container overlays, and a 500 on
    # every ordinary start is the exact brick the reviewers replayed. The
    # in-memory handoff is the ONLY reason this function exists: when this
    # attempt has none, there is nothing to bind and the early return is the
    # honest answer (the same True the transactional path used to produce).
    with _LOCK:
        if attempt:
            key = (sid, attempt)
        else:
            session_keys = [k for k in _CONTINUATION_HANDOFF_TOKENS if k[0] == sid]
            key = session_keys[0] if session_keys else None
        if key is None or key not in _CONTINUATION_HANDOFF_TOKENS:
            # #7862 re-gate round 17 (CORE finding 1): a missing LOCAL key is
            # not by itself "nothing was consumed". If this attempt's consume
            # minted a rollback receipt that is still live, the admission it
            # owned was consumed and then REVOKED: a peer discharged it after
            # a launch, or a takeover consumed it once the ownership lease
            # expired, and an earlier transaction's reconciliation already
            # dropped the local copy. The receipt is the provenance a missing
            # key cannot erase — it is only dropped by a successful launch or
            # by this attempt's own rollback. Binding now would walk the
            # genuinely-ordinary route into ``thr.start()`` for an admission
            # this process no longer owns, which is how a second goal worker
            # for one intent appeared. Refuse exactly like the in-transaction
            # refusal below.
            if _attempt_has_live_receipt_unlocked(sid, attempt):
                logger.error(
                    "Goal continuation admission for session %s (attempt %s) "
                    "was consumed and then revoked; refusing to bind its "
                    "stream identity",
                    sid,
                    attempt or "?",
                )
                return False
            # No admission to bind: nothing consumed for this attempt (an
            # ordinary turn, or the intent was never armed). Not an error.
            return True
    try:
        with _registry_transaction():
            # #7862 re-gate (2026-10-27, CORE finding 1): reload the durable
            # state under the cross-process lock before mutating.
            _merge_disk_state_unlocked()
            if key not in _CONTINUATION_HANDOFF_TOKENS:
                # #7862 re-gate round 16 (CORE finding 1): this process HAD an
                # admission for this attempt (checked above under ``_LOCK``),
                # and the merge just dropped it. The only way a durable
                # handoff disappears is that a peer discharged it after the
                # launch, or a cold restore took the admission over once the
                # ownership lease expired -- and a takeover removes the handoff
                # from the durable table. Either way this attempt no longer
                # owns the continuation, so its stream identity must NOT be
                # bound: the route launches on this Boolean, and binding here
                # is how the second goal worker for one intent appeared.
                logger.error(
                    "Goal continuation admission for session %s (attempt %s) "
                    "was discharged or taken over by another process; refusing "
                    "to bind its stream identity",
                    sid,
                    key[1],
                )
                return False
            _CONTINUATION_HANDOFF_TOKENS[key]["_handoff_stream_id"] = stream
            # #7862 re-gate round 16 (CORE finding 1): binding is the last thing
            # an owner does before its worker executes, so it is also the last
            # renewal of the ownership lease. Without it an admission that
            # legitimately outlived the reclaim window could be taken over
            # between the consume and the launch.
            _CONTINUATION_HANDOFF_TOKENS[key]["_handoff_minted_at"] = time.time()
            if _write_registry_unlocked(
                PENDING_GOAL_CONTINUATION_RECORDS,
                context=f"handoff-stream sid={sid} attempt={key[1]}",
            ):
                return True
            # The identity did not land, so the admission stays unsettled. Drop
            # the in-memory copy too: leaving it would let this process believe
            # the reconciliation is possible while disk says otherwise, and the
            # next snapshot would publish a stream id the failed write never
            # committed.
            _CONTINUATION_HANDOFF_TOKENS[key].pop("_handoff_stream_id", None)
            logger.warning(
                "Goal continuation admission for session %s (attempt %s) could not "
                "record its stream identity; the settlement stays unreconciled",
                sid,
                key[1],
            )
            return False
    except _RegistryLockUnavailable:
        # A lock we cannot take must not fall back to an unlocked write. The
        # admission stays unsettled; the caller refuses the launch, which
        # for a TRUE continuation is the honest outcome (the same refusal
        # shape as a failed snapshot).
        return False


def _attempt_has_live_receipt_unlocked(sid: str, attempt: str) -> bool:
    """True when *attempt* still holds the rollback receipt its consume minted.

    #7862 re-gate round 17 (CORE finding 1): the receipt is the provenance of
    a consume. It is minted in the same transaction as the admission handoff
    and dropped only by a successful launch, by the attempt's own rollback, or
    by the bounded sweep once the attempt is provably gone — so a live receipt
    with no handoff means "consumed and subsequently revoked", never "never
    consumed". With no ``attempt`` the session's receipt is claimed by
    insertion order (the legacy single-attempt shape). Callers hold ``_LOCK``.
    """
    if attempt:
        return (sid, attempt) in _ROLLBACK_RECEIPTS
    return any(key[0] == sid for key in _ROLLBACK_RECEIPTS)


def goal_continuation_attempt_owns_admission(
    session_id: str, attempt_id: str, stream_id: str = ""
) -> bool:
    """True when this attempt STILL owns the session's continuation admission.

    #7862 re-gate round 17 (CORE finding 1): the regeneration route renews the
    ownership lease when it binds the stream identity, then releases the file
    transaction and does the session save / workspace work before it releases
    its gated worker. A peer can take the admission over inside that window,
    and the worker used to execute anyway — elapsed age was treated as a fence
    through launch, which it is not. This re-reads the durable state under the
    cross-process lock (the same reload every other mutation performs), so a
    takeover that consumed the handoff is visible here, and answers whether
    THIS attempt still owns it.

    Never raises into the chat path. A lock that cannot be taken answers
    False: ownership cannot be proven, and the caller must not launch on a
    maybe (the same refusal shape as a failed binding).
    """
    sid = str(session_id or "").strip()
    attempt = str(attempt_id or "").strip()
    if not sid or not attempt:
        # No attempt identity to prove ownership for.
        return False
    try:
        with _registry_transaction():
            _merge_disk_state_unlocked()
            token = _CONTINUATION_HANDOFF_TOKENS.get((sid, attempt))
            if token is None:
                logger.error(
                    "Goal continuation admission for session %s (attempt %s) "
                    "no longer exists durably; its worker must not run",
                    sid,
                    attempt,
                )
                return False
            bound_stream = str(token.get("_handoff_stream_id") or "")
            if stream_id and bound_stream and bound_stream != str(stream_id):
                # The durable admission was re-bound to a different stream:
                # this attempt is not the one that owns it any more.
                logger.error(
                    "Goal continuation admission for session %s (attempt %s) "
                    "is bound to stream %s, not %s; its worker must not run",
                    sid,
                    attempt,
                    bound_stream,
                    stream_id,
                )
                return False
            return True
    except _RegistryLockUnavailable:
        logger.error(
            "Goal continuation admission for session %s (attempt %s) could "
            "not be revalidated: the cross-process lock is unavailable",
            sid,
            attempt,
        )
        return False


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
    # #7862 re-gate round 15 (CORE finding 1, BRICK): the discharge runs on
    # EVERY chat start. When this start consumed no continuation there is no
    # handoff to discharge, so return before touching the cross-process
    # lock — a broken ``flock`` (ENOSYS/ENOLCK) must never fail an ordinary
    # start.
    with _LOCK:
        if attempt:
            _had_admission = (sid, attempt) in _CONTINUATION_HANDOFF_TOKENS
        else:
            _had_admission = any(
                k[0] == sid for k in _CONTINUATION_HANDOFF_TOKENS
            )
        if not _had_admission:
            return True
    try:
        with _registry_transaction():
            # #7862 re-gate (2026-10-27, CORE finding 1): reload the durable
            # state under the cross-process lock before mutating.
            _merge_disk_state_unlocked()
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
                    # The converted handoff is gone from memory for good, so the
                    # "withhold from disk" marker has nothing left to describe.
                    _ADOPTED_HANDOFF_SESSIONS.discard(sid)
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
    except _RegistryLockUnavailable:
        # A lock we cannot take must not fall back to an unlocked write: the
        # settlement stays unreconciled and recovery reconciles at the next
        # startup, exactly like a failed snapshot discharge.
        logger.error(
            "Goal continuation handoff for session %s could not be durably "
            "discharged: the cross-process registry lock is unavailable",
            sid,
        )
        return False


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
    # The conversion marker only ever said "this session's handoff is already
    # spent"; with no token left there is nothing to withhold from disk.
    _ADOPTED_HANDOFF_SESSIONS.discard(sid)
    return len(keys)


def _record_rollback_receipt_unlocked(sid: str, record: dict, attempt_id: str) -> None:
    """Store a rollback receipt for one start attempt; callers hold ``_LOCK``."""
    attempt = str(attempt_id or "") or "attempt"
    stored = dict(record)
    stored["_receipt_minted_at"] = time.time()
    # #7862 re-gate round 17 (CORE finding 1): the receipt is this attempt's
    # PROVENANCE — the in-process evidence that IT consumed the intent. The
    # handoff it was minted with can be merged away later: a peer takeover or
    # discharge removes the durable handoff, and the next unrelated
    # transaction's reconciliation drops the local copy. A missing local key
    # must not erase that history, so the attempt identity travels with the
    # receipt. Two consumers read it: the stream binding tells "consumed and
    # subsequently revoked" (refuse the launch) from "never consumed" (the
    # genuinely ordinary lock-free path), and the rollback restore mutates
    # only the losing attempt's own custody instead of the whole session's.
    stored["attempt_id"] = attempt
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
                #
                # #7862 re-gate round 3 (finding 3): the filter is now WHOLE
                # ENTRY per handoff, keyed by the entry's own adoption marker.
                # The previous two-level filter suppressed every token of a
                # session in ``_ADOPTED_HANDOFF_SESSIONS`` AND blanked the
                # fields of every entry of a suppressed session. Both were
                # wrong in the same direction:
                #
                # * the session-wide set also hid the RETRY's fresh handoff —
                #   after an adoption, a retry's own crash before its launch
                #   left nothing durable and the continuation was lost;
                # * the inner field filter republished an already-adopted
                #   token as ``{}`` when a later discharge cleared the session
                #   from the set (``discard_goal_continuation_handoff``
                #   discards the marker), so an unrelated write restored a
                #   phantom with an empty prompt and token.
                #
                # Filtering the whole entry by its own marker fixes both: a
                # converted token is withheld for as long as the marker
                # exists, and every other token (including a retry's) is
                # published with all of its fields.
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
                        # ``_handoff_stream_id`` IS persisted: it is the durable
                        # admission of a start that LAUNCHED under that stream
                        # (#7862 re-gate). Round 3 reconciles it against the
                        # run/turn journals, so a cold restore must still be
                        # able to read it.
                        and k != "_handoff_adopted_into_record"
                    }
                    for (sid, attempt), rec in _CONTINUATION_HANDOFF_TOKENS.items()
                    # #7862 re-gate round 3 (finding 3): this session's THIS
                    # handoff has already been CONVERTED into a restored record.
                    # Publishing it again would let the next cold restore adopt
                    # it a second time. The in-memory token stays — the receipt
                    # liveness anchor needs it — only the durable copy of this
                    # entry is withheld. Note the scope: one entry, not the
                    # whole session.
                    if not rec.get("_handoff_adopted_into_record")
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
    global _LAST_LOAD_ERROR, _LAST_FILE_GENERATION
    _LAST_LOAD_ERROR = None
    try:
        raw = _PENDING_GOAL_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        _LAST_FILE_GENERATION = 0
        return {}, {}, {}
    except (OSError, ValueError) as exc:
        _LAST_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("Goal continuation registry unreadable: %s", _LAST_LOAD_ERROR)
        _LAST_FILE_GENERATION = 0
        return {}, {}, {}
    if isinstance(data, dict):
        file_generation = int(data.get("generation") or 0)
        _LAST_FILE_GENERATION = file_generation
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
                    # #7862 re-gate CORE (Oct 10): the stream identity of an
                    # admission that LAUNCHED. Same whitelist trap as the clock:
                    # dropping it would make every restored admission look like
                    # an interrupted start, and the adoption would resurrect a
                    # continuation whose turn already ran — the exact
                    # failed-discharge resurrection this round closes.
                    "_handoff_stream_id": str(rec.get("_handoff_stream_id") or ""),
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
        _LAST_FILE_GENERATION = 0
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


def session_has_registry_state(session_id: str) -> bool:
    """True when any registry state exists for *session_id*.

    #7862 re-gate round 15 (should-fix): a session delete used to write an
    fsynced registry snapshot with a tombstone for EVERY session, goal or
    not. The delete path calls this first and returns early when there is no
    record, handoff or receipt — a no-goal delete must not touch the
    registry at all. Never raises.

    #7862 re-gate round 16 (CORE finding 4): the local cache is NOT
    authoritative. It only holds what THIS process observed, so a process that
    never saw a peer's durable state answered False — and the delete route then
    set ``continuation_retired = True`` without consulting or retiring that
    state, leaving the peer's private continuation prompt claimable on disk
    while acknowledging retirement. The decision is therefore taken from the
    AUTHORITATIVE durable state, under the transaction: the file lock plus a
    reload, which is the same read every other registry writer trusts. A lock
    this process cannot take fails CLOSED — an absence it cannot prove must
    never be allowed to skip a retirement.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = str(session_id or "").strip()
    if not sid:
        return False
    with _LOCK:
        if sid in PENDING_GOAL_CONTINUATION_RECORDS:
            return True
        if any(k[0] == sid for k in _CONTINUATION_HANDOFF_TOKENS):
            return True
        if any(k[0] == sid for k in _ROLLBACK_RECEIPTS):
            return True
    try:
        with _registry_transaction():
            # Reload the durable state under the cross-process lock: the merge
            # is what makes a peer's record, handoff or tombstone visible here.
            _merge_disk_state_unlocked()
            if sid in PENDING_GOAL_CONTINUATION_RECORDS:
                return True
            if any(k[0] == sid for k in _CONTINUATION_HANDOFF_TOKENS):
                return True
            # A retirement tombstone is evidence the intent is already gone, so
            # it does not by itself justify another fsynced snapshot.
            return False
    except _RegistryLockUnavailable:
        # Cannot prove absence from authoritative state: do not skip the
        # retirement. The retire that follows refuses honestly instead, and the
        # delete reports a non-durable retirement.
        return True


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
    try:
        with _registry_transaction():
            # #7862 re-gate (2026-10-27, CORE finding 1): reload the durable
            # state under the cross-process lock before mutating.
            _merge_disk_state_unlocked()
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
    except _RegistryLockUnavailable:
        # A lock we cannot take must not fall back to an unlocked write: the
        # arm is refused and the producer must not emit a goal_continue event
        # for an intent the route could never admit.
        return False


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
    try:
        with _registry_transaction():
            # #7862 re-gate (2026-10-27, CORE finding 1): reload the durable
            # state under the cross-process lock before mutating.
            _merge_disk_state_unlocked()
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
    except _RegistryLockUnavailable:
        # A lock we cannot take must not fall back to an unlocked write. The
        # retirement is REFUSED: the caller (session delete, /goal clear) must
        # surface the failure instead of claiming a durable clear.
        return False


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

# #7862 round 11 (re-gate CORE): the literal control ``/goal resume`` tells
# the user to type. Normalised once here so the admission compares against
# the same canonical form ``normalize_continuation_text`` produces.
_CONTINUE_CONTROL = "continue"


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
    explicit_continue: bool = False,
    goal_active: bool = False,
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

    ``explicit_continue`` + ``goal_active`` are the round-11 admission for the
    ``/goal resume`` flow: the resume message tells the user to "type
    continue", and that literal control is a brand-new human turn whose text
    is NOT the recorded prompt, so the text match alone stopped the goal loop
    (the Oct 10 re-gate finding). When the caller certifies the session has an
    active goal, this spends the intent on the literal control. It is NOT a
    bypass of the admission contract: the request must still carry THIS
    continuation's token (a tokenless or wrong-generation ``continue`` stays an
    ordinary turn), the text must normalise to exactly ``continue`` (a
    paraphrase or "Continue please!!" is not admitted), and every durable
    outcome below is unchanged.

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
    try:
        with _registry_transaction():
            # #7862 re-gate (2026-10-27, CORE finding 1): the consume is the
            # transaction that must never trust stale memory. Reload the durable
            # state under the cross-process lock first: if another process
            # already spent this intent the record is gone from disk, the merge
            # drops the stale in-memory copy, and the lookup below answers
            # ``CONSUME_NOT_MATCHING`` instead of admitting a second run.
            _merge_disk_state_unlocked()
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
            # #7862 round 11 (re-gate CORE): an explicit ``continue`` for a
            # resumed goal is admitted on identity, not on text. The resume
            # message tells the user to "type continue", and that literal
            # control is a new human turn the recorded prompt never matches, so
            # the admission ran with ``goal_related=false`` and the goal loop
            # stopped after the resume. The guard is deliberately narrow: the
            # caller must certify an active goal, the text must normalise to
            # exactly ``continue``, and the request must still carry this
            # continuation's token (checked above, and a tokenless request falls
            # through to the text comparison below rather than being admitted).
            continue_admitted = bool(
                explicit_continue
                and goal_active
                and request_id
                and normalize_continuation_text(incoming_text) == _CONTINUE_CONTROL
            )
            # #7862 re-gate round 3 (finding 4): a request carrying THIS
            # continuation's token is admitted on identity even when its text was
            # edited (or the queued entries were combined) while it sat in the
            # queue. The UI can legitimately produce that: the queue's Combine
            # action merges the queued texts into one entry, and editing a queued
            # continuation rewrites the text. Both keep the continuation's token
            # on the entry, so the turn is still the goal continuation the store
            # armed — but the recorded prompt no longer matches, the consume
            # returned NOT_MATCHING, the turn ran as an ordinary turn and the
            # goal loop stopped.
            #
            # The guard is deliberately narrow and does NOT widen the tokenless
            # contract: the token must be present, must equal the recorded one
            # (checked above, so a foreign generation is rejected before this
            # point), and the record must carry a non-empty token of its own.
            # A tokenless request falls through to the text comparison below.
            token_admitted = bool(
                request_id
                and recorded_id
                and request_id == recorded_id
            )
            if (
                not continue_admitted
                and not token_admitted
                and not _continuation_text_matches(
                    record.get("prompt") or "", incoming_text
                )
            ):
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
    except _RegistryLockUnavailable:
        # A lock we cannot take must not fall back to an unlocked write.
        # The continuation IS matched but the registry cannot be transacted:
        # refuse the admission (CONSUME_COMMIT_FAILED) instead of letting
        # the route launch it as an ordinary turn.
        return CONSUME_COMMIT_FAILED


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
    try:
        with _registry_transaction():
            # #7862 re-gate (2026-10-27, CORE finding 1): reload the durable
            # state under the cross-process lock before mutating — a peer may
            # have armed a newer intent for this session since the receipt was
            # taken, and the guard below must compare against the durable truth.
            _merge_disk_state_unlocked()
            current = PENDING_GOAL_CONTINUATION_RECORDS.get(sid)
            if current is not None:
                # A newer intent for this same session is already live: if it is
                # a DIFFERENT generation than the receipt describes, the receipt
                # is stale and must not clobber it. (A byte-identical re-arm of
                # the very same continuation_id cannot happen mid-flight, since
                # arming drops the session's receipts, so any live record here
                # is by construction newer.)
                return False
            # #7862 re-gate round 17 (CORE finding 2): an empty pending slot is
            # NOT proof that nobody owns the continuation. A successor attempt
            # can have CONSUMED it: a cold restore adopts the expired handoff
            # (preserving the original token/generation, which is not a new
            # attempt identity) and then spends the restored record as its own
            # attempt, minting its own durable handoff. The pending-record table
            # is then empty, so the checks above all pass and this rollback
            # would recreate the intent beside a RUNNING admission — and the
            # session-wide handoff drop below would delete the successor's
            # admission too. Validate against the CURRENT attempt ownership
            # under this same transaction: a live handoff belonging to any
            # other attempt is a takeover this receipt lost.
            losing_attempt = str(record.get("attempt_id") or "").strip()
            if losing_attempt:
                for token_key, token in _CONTINUATION_HANDOFF_TOKENS.items():
                    if token_key[0] != sid:
                        continue
                    if token_key[1] == losing_attempt:
                        # The losing attempt's own custody: the drop below
                        # spends exactly this, nothing else.
                        continue
                    if token.get("_handoff_adopted_into_record"):
                        # A conversion anchor, not custody: the adoption that
                        # restored the record this receipt describes already
                        # spent it (its durable copy is withheld from disk).
                        continue
                    logger.warning(
                        "Goal continuation rollback for session %s refused: "
                        "attempt %s now owns the admission this receipt "
                        "describes; the successor's handoff stays untouched",
                        sid,
                        token_key[1],
                    )
                    _RETIRED_LOG.append(
                        {
                            "session_id": sid,
                            "reason": "rollback_refused_taken_over",
                            "at": time.time(),
                        }
                    )
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
            _restore_committed = _write_registry_unlocked(
                PENDING_GOAL_CONTINUATION_RECORDS,
                context=f"rollback-restore sid={sid}",
            )
            # #7862 re-gate round 15 (should-fix, warm-path ENOSPC): when the
            # snapshot write fails, the record is live in memory but absent on
            # disk. The next unrelated transaction's merge would otherwise drop
            # it ("absent on disk => gone") and the warm retry would run as an
            # ordinary turn; the session stays exempt from that drop until its
            # record lands on disk again.
            #
            # #7862 re-gate round 16 (CORE finding 3): the exemption is a FENCE
            # recording the token and generation of the intent just restored,
            # so the merge can tell this dirty restore from a newer retirement
            # a peer committed in the meantime.
            if _restore_committed:
                _RESTORE_WRITE_FAILED_SESSIONS.pop(sid, None)
            else:
                _RESTORE_WRITE_FAILED_SESSIONS[sid] = {
                    "generation": int(restored.get("generation") or 0),
                    "continuation_id": str(restored.get("continuation_id") or ""),
                }
                logger.warning(
                    "Goal continuation registry: rollback restored session %s in "
                    "memory but its snapshot write failed; the record is exempt "
                    "from the merge drop until it lands on disk",
                    sid,
                )
            _RETIRED_LOG.append(
                {"session_id": sid, "reason": "restored_rejected_start", "at": time.time()}
            )
            return True
    except _RegistryLockUnavailable:
        # A lock we cannot take must not fall back to an unlocked write:
        # the rollback restore is refused and the retry stays unmatched
        # until the registry can be transacted again.
        return False


# A turn-journal ``interrupted`` row written by the ROUTE itself when a start
# is compensated is NOT execution evidence: the worker never ran. The launch
# path writes these rows when a thread-start failure (or a post-acceptance
# workspace failure) aborts the start and rolls it back — the journal then
# reads ``[submitted, interrupted/start_compensated]`` with zero workers, and
# counting that row as execution made a cold restore drop the handoff and
# lose the continuation (#7862 re-gate 2026-10-27, CORE finding 2). A
# WORKER-written interruption (``cancelled``, or an exception type) happens
# inside a turn that really executed, so it stays execution evidence.
_ROUTE_GENERATED_INTERRUPT_REASONS = frozenset(
    {
        "start_compensated",
        "post_acceptance_workspace_failure",
    }
)


def _stream_executed_durably(session_id: str, stream_id: str) -> bool:
    """True when durable run/turn state proves this stream's worker ran.

    #7862 re-gate round 3 (finding 5): an allocated stream id on a handoff is
    NOT proof of execution. The route binds it before ``thr.start()``, so a
    crash in that window leaves a stream id whose worker never ran — and
    recovery that treated the id as proof silently dropped the continuation
    (a forced exit at exactly that boundary restored 0).

    The reconciliation reads what only a WORKER can write:

    * the **turn journal** — the route's ``submitted`` event for the stream
      (written inside the launch critical section) plus the worker's own
      ``worker_started`` / ``completed`` / ``interrupted`` rows (a
      route-generated compensation row is NOT execution — see
      ``_ROUTE_GENERATED_INTERRUPT_REASONS``);
    * the **run journal** — the SSE rows the worker emits for the stream.

    Either one present means execution happened, so the admission is spent.
    Neither present means the process died before the worker started, so the
    admission is still recoverable and the handoff must stay adoptable.

    Never raises: an unreadable or absent journal is simply "no evidence",
    which is the safe direction for recovery.
    """
    sid = str(session_id or "").strip()
    stream = str(stream_id or "").strip()
    if not sid or not stream:
        return False
    try:
        from api.models import SESSION_DIR
        from api.run_journal import _run_path as _run_journal_path
        from api.turn_journal import read_turn_journal

        session_dir = SESSION_DIR
        # Turn journal: any event carrying this stream id (the route's own
        # ``submitted`` event is written after the stream id is allocated, so
        # it alone is NOT execution evidence — require the worker's rows).
        journal = read_turn_journal(sid, session_dir=session_dir)
        for event in journal.get("events") or []:
            if not isinstance(event, dict):
                continue
            if str(event.get("stream_id") or "") != stream:
                continue
            event_name = str(event.get("event") or "")
            if event_name in (
                "worker_started",
                "completed",
            ):
                return True
            if event_name == "interrupted":
                # A route-generated compensation row is NOT execution: the
                # start was aborted and rolled back with zero workers. Only
                # a WORKER-written interruption (a cancelled or failed turn
                # that really ran) proves execution.
                if (
                    str(event.get("reason") or "")
                    in _ROUTE_GENERATED_INTERRUPT_REASONS
                ):
                    continue
                return True
        # Run journal: any SSE row the worker appended for this stream.
        path = _run_journal_path(sid, stream, session_dir=session_dir)
        if path.exists():
            with open(path, "rb") as fh:
                if fh.read(1):
                    return True
    except Exception:
        logger.debug(
            "Goal continuation stream reconciliation failed for session %s "
            "stream %s; treating it as not-executed",
            sid,
            stream,
            exc_info=True,
        )
    return False


def _publish_continuation_mirror_unlocked(sid: str, record: dict) -> None:
    """Publish master's marker + prompt/token mirror for a restored intent.

    ``api.goals`` owns ``PENDING_GOAL_CONTINUATION_PROMPTS``: its orphan sweep
    removes any marker whose mirror record is missing, and the legacy
    key-omission resolver (``_resolve_chat_start_goal_continuation_id``)
    peeks the same mirror. A restore that published ONLY the store's marker
    therefore lost the admission at the next sweep — ``continue`` after
    ``/goal resume`` launched with ``goal_related=False`` (#7862 re-gate
    2026-10-27, CORE finding 4). The marker and the mirror land together
    under their shared lock, so no reader can observe one without the other.

    Callers hold the store's ``_LOCK``; the config lock is a leaf lock (no
    store call happens while it is held), so the ordering is deadlock-free.
    """
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_LOCK,
        PENDING_GOAL_CONTINUATION_PROMPTS,
    )

    prompt = str(record.get("prompt") or "")
    cont_id = str(record.get("continuation_id") or "")
    with PENDING_GOAL_CONTINUATION_LOCK:
        PENDING_GOAL_CONTINUATION.add(sid)
        if prompt and cont_id:
            PENDING_GOAL_CONTINUATION_PROMPTS[sid] = {
                "prompt": prompt,
                "continuation_id": cont_id,
            }


def restore_goal_continuations() -> int:
    """Startup-only restore: merge durable records into the live marker set.

    Merges BOTH ``PENDING_GOAL_CONTINUATION`` and
    ``PENDING_GOAL_CONTINUATION_RECORDS`` for every durable record not already
    live (a live in-memory record always wins over disk), loads the durable
    clear tombstones, and adopts the durable continuation handoffs. Returns the
    count of sessions restored. Never raises.

    #7862 re-gate (2026-10-27): every restored intent also publishes master's
    prompt/token mirror (``PENDING_GOAL_CONTINUATION_PROMPTS``) under its
    shared lock (CORE finding 4), and sessions the WebUI deleted while this
    process was down are filtered out of BOTH the record and the handoff
    restoration (SILENT finding 3).
    """
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    # #7862 re-gate (2026-10-27, CORE finding 1): the startup restore is a
    # registry writer too — it takes the cross-process lock so a second
    # process's restore (or any concurrent writer) cannot interleave with
    # its adopt/drop decisions. It does its own disk read below, so the
    # generic merge is deliberately NOT run here.
    try:
        with _registry_transaction():
            disk, disk_tombstones, disk_handoffs = _load_file_raw()
            # SILENT finding 3: a session deleted while this process was down
            # must not have its intent restored. The delete's retirement write
            # can fail (ENOSPC) after the WebUI tombstone landed, and a cold
            # restore would then recover the deleted intent — record and handoff
            # alike. The tombstone is the durable delete marker
            # (``SESSION_DIR/_deleted_webui_sessions.json``).
            try:
                from api.models import _load_webui_deleted_session_tombstone

                _deleted_sessions = _load_webui_deleted_session_tombstone()
            except Exception:
                _deleted_sessions = frozenset()
            # Baseline the generation counter ABOVE every generation on disk
            # (records and tombstones alike). A cold process counts from 0, so a
            # restored record carrying generation 41 would otherwise compare as
            # OLDER than the fresh clear at generation 3 -- and the clear would be
            # forgotten, resurrecting a goal the user had cancelled (#7862 round 6,
            # scenario 4). Starting above the persisted maximum makes every later
            # generation strictly newer than anything a previous process wrote.
            highest = max(
                # #7862 re-gate round 15 (CORE finding 4): the FILE's own global
                # generation joins the records/tombstones/handoffs. A cold
                # process that never saw a peer's writes must still retire above
                # the persisted counter.
                [int(_LAST_FILE_GENERATION or 0)]
                + [int(rec.get("generation") or 0) for rec in disk.values()]
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
            # #7862 re-gate CORE: records and handoffs the restore deliberately
            # does NOT restore (already launched, retired while the process was
            # down, or belonging to a deleted session). Counted so the closing
            # snapshot knows the drop must be committed, not just applied in
            # memory.
            dropped = 0
            restored = 0
            for sid, record in disk.items():
                if sid in _deleted_sessions:
                    # The session was deleted while this process was down: its
                    # intent must not come back. The record never enters memory,
                    # so the closing snapshot below drops it from disk as well.
                    logger.info(
                        "Goal continuation registry: refusing to restore the "
                        "record for deleted session %s",
                        sid,
                    )
                    dropped += 1
                    _RETIRED_LOG.append(
                        {
                            "session_id": sid,
                            "reason": "record_restore_refused_deleted",
                            "at": time.time(),
                        }
                    )
                    continue
                # #7862 re-gate round 16 (CORE finding 3): plain-record
                # restoration used to check ONLY deleted-session membership,
                # so a record that outlived its own clear (a failed warm
                # rollback republished next to a newer tombstone) came back on
                # the next boot. The handoff adoption already refuses a
                # handoff whose generation is covered by the retirement
                # tombstone; a record carries the same generation, so it is
                # held to the SAME authority.
                if _record_is_retired(sid, record):
                    logger.info(
                        "Goal continuation registry: refusing to restore the "
                        "record for session %s (generation %d <= retired "
                        "generation %d)",
                        sid,
                        int(record.get("generation") or 0),
                        _retirement_generation_unlocked(sid),
                    )
                    dropped += 1
                    _RETIRED_LOG.append(
                        {
                            "session_id": sid,
                            "reason": "record_restore_refused_retired",
                            "at": time.time(),
                        }
                    )
                    continue
                if sid in PENDING_GOAL_CONTINUATION or sid in PENDING_GOAL_CONTINUATION_RECORDS:
                    continue
                generation = _next_generation_unlocked()
                record = dict(record)
                if not record.get("generation"):
                    # v1 upgrade: assign the current generation at restore time.
                    record["generation"] = generation
                # CORE finding 4: the marker AND master's prompt/token mirror
                # publish together under their shared lock, so the orphan sweep
                # and the legacy resolver both see a complete pair.
                PENDING_GOAL_CONTINUATION_RECORDS[sid] = record
                _publish_continuation_mirror_unlocked(sid, record)
                restored += 1
            # #7862 round 7 (finding 2): adopt the durable handoffs. Each one is an
            # in-flight chat start whose consume already spent the durable record;
            # if the process died before the pending-start write, this is the ONLY
            # evidence that the continuation was admitted. Re-arm it so the retry
            # can still consume it instead of running as an ordinary turn.
            adopted = 0
            # #7862 re-gate CORE: the tokens converted into records during adoption,
            # kept so a failed snapshot write can put them back. Empty when nothing
            # was adopted, which is why it is initialised outside the loop.
            adopted_tokens: dict[tuple[str, str], dict] = {}
            for (sid, attempt), record in disk_handoffs.items():
                if sid in _deleted_sessions:
                    # SILENT finding 3: the session was deleted while this
                    # process was down; its handoff is evidence for an intent
                    # that must not exist any more.
                    logger.info(
                        "Goal continuation registry: refusing to adopt the "
                        "handoff for deleted session %s (attempt %s)",
                        sid,
                        attempt,
                    )
                    _drop_continuation_handoffs_unlocked(sid)
                    dropped += 1
                    _RETIRED_LOG.append(
                        {
                            "session_id": sid,
                            "reason": "handoff_adoption_refused_deleted",
                            "at": time.time(),
                        }
                    )
                    continue
                if sid in PENDING_GOAL_CONTINUATION_RECORDS:
                    # The intent is already live as a record (a rollback restored
                    # it, or an earlier adoption did). Its handoff is spent; drop
                    # it rather than leaving a second claimable copy behind
                    # (#7862 round 9).
                    _drop_continuation_handoffs_unlocked(sid)
                    dropped += 1
                    continue
                # #7862 re-gate round 15 (CORE finding 2): a handoff whose
                # liveness clock is FRESH belongs to a process that is still in
                # its admission — the receipt reclaim refreshes the clock exactly
                # while the attempt reports in. Adopting it here would let THIS
                # process launch the same continuation the owner is launching:
                # two goal workers for one intent. Exclusive ownership runs
                # through the owner's launch (discharge) or rollback (restore);
                # a cold process adopts a handoff only after proving the owner is
                # gone — the clock older than the reclaim window, or no clock at
                # all (the round-10 abandonment convention keeps the registry
                # bounded). A skipped handoff is left on disk untouched.
                _handoff_minted_at = 0.0
                try:
                    _handoff_minted_at = float(record.get("_handoff_minted_at") or 0.0)
                except (TypeError, ValueError):
                    _handoff_minted_at = 0.0
                if _handoff_minted_at and (
                    time.time() - _handoff_minted_at
                ) <= _ROLLBACK_RECEIPT_TTL_SECONDS:
                    logger.info(
                        "Goal continuation registry: leaving the handoff for "
                        "session %s (attempt %s) alone — its owning process is "
                        "live (liveness clock %ds old)",
                        sid,
                        attempt,
                        int(time.time() - _handoff_minted_at),
                    )
                    # #7862 re-gate round 16 (CORE finding 2): this restore
                    # deliberately does not run the generic merge, so the
                    # skipped handoff never entered the local table — and the
                    # closing snapshot below serialises only that table. When
                    # this same restore recovers an unrelated record or drops
                    # another entry, the snapshot therefore ERASED the live
                    # peer's admission from disk and a crash of its owner had
                    # nothing left to recover. Preserve it: the entry is copied
                    # verbatim from disk, so republishing it is a no-op, and
                    # the merge's per-key reconciliation still lets a later
                    # bind or discharge win (this is not an unconditional
                    # union).
                    _CONTINUATION_HANDOFF_TOKENS[(sid, attempt)] = dict(record)
                    continue
                # #7862 re-gate round 3 (finding 5): a handoff carrying a STREAM
                # id is only evidence that an admission RECORDED an identity — the
                # route binds the stream id before ``thr.start()``. The reviewer's
                # new crash window sits exactly between the two: a forced exit at
                # that boundary restored 0 continuations although no worker ever
                # ran, because recovery treated the allocated id as proof of
                # execution. So the recorded stream is now RECONCILED against the
                # durable run/turn state the WORKER writes, and only a stream that
                # actually executed is treated as a spent admission:
                #
                # * the turn journal carries the route's ``submitted`` event plus
                #   the worker's own lifecycle rows (``worker_started`` /
                #   ``completed`` / ``interrupted``) for the stream;
                # * the run journal carries the SSE rows the worker emits.
                #
                # Either one proves the worker reached execution. Neither present
                # means the process died before ``thr.start()`` — the admission is
                # unrecoverable-by-any-other-means, so the handoff stays
                # reconcilable and is adopted below.
                if record.get("_handoff_stream_id"):
                    _recorded_stream = str(record.get("_handoff_stream_id") or "")
                    if _stream_executed_durably(sid, _recorded_stream):
                        logger.info(
                            "Goal continuation registry: dropping the handoff for "
                            "session %s (attempt %s) — its turn executed under "
                            "stream %s",
                            sid,
                            attempt,
                            _recorded_stream,
                        )
                        _drop_continuation_handoffs_unlocked(sid)
                        dropped += 1
                        _RETIRED_LOG.append(
                            {
                                "session_id": sid,
                                "reason": "handoff_admission_already_launched",
                                "at": time.time(),
                            }
                        )
                        continue
                    logger.warning(
                        "Goal continuation registry: the handoff for session %s "
                        "(attempt %s) recorded stream %s, but no durable run/turn "
                        "evidence exists for it — the worker never launched, so the "
                        "admission stays recoverable (#7862 re-gate round 3, "
                        "finding 5)",
                        sid,
                        attempt,
                        _recorded_stream,
                    )
                if (sid, attempt) in _CONTINUATION_HANDOFF_TOKENS:
                    # This process already owns the admission (a rollback restored
                    # the record and re-armed the handoff, or an earlier pass did).
                    # The in-memory token is authoritative and nothing is dropped
                    # here, so this does NOT count towards the closing snapshot.
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
                    dropped += 1
                    _RETIRED_LOG.append(
                        {
                            "session_id": sid,
                            "reason": "handoff_adoption_refused_retired",
                            "at": time.time(),
                        }
                    )
                    continue
                # The intent itself is gone from disk by construction (the consume
                # removed it), so restore the record for the matching retry: this
                # is what makes the handoff reconcilable rather than merely
                # informative.
                #
                # #7862 re-gate CORE: this adoption is a CONVERSION, not a copy. The
                # handoff is the durable evidence that this attempt spent the intent;
                # the restored record is the durable state that the intent is claimable
                # again. Leaving the handoff behind made both true at once, and since
                # a successful retry discharges only ITS OWN attempt's handoff, the
                # next cold restore re-adopted the original one and resurrected a
                # continuation that had already been spent.
                #
                # Popping the tokens BEFORE `_write_registry_unlocked` is what makes the
                # swap atomic: that single `os.replace` writes the restored record and
                # the emptied handoff table together, so a crash in between leaves the
                # pre-adoption state (handoff still there, no record) — which is
                # re-adoptable — rather than the double-spendable state. If the write
                # fails we put the tokens back and do not report an adoption.
                #
                # Every token for this sid is collected, not just the current
                # (sid, attempt): a retry may have minted its own handoff for a
                # different attempt of the same session, and it is spent by exactly
                # the same reasoning. (An earlier revision popped only the current key
                # here and then collected the rest below — which silently dropped the
                # token this iteration was adopting and left the write-failure path
                # with nothing to restore.)
                #
                # A token recovered from DISK has no in-memory entry to collect, so the
                # rebuilt dict must carry the liveness clock the disk record provides:
                # dropping ``_handoff_minted_at`` would make the restored handoff read
                # as abandoned and get its receipt swept on the next pass.
                adopted_keys = [k for k in _CONTINUATION_HANDOFF_TOKENS if k[0] == sid]
                adopted_tokens = {
                    k: dict(_CONTINUATION_HANDOFF_TOKENS[k]) for k in adopted_keys
                }
                adopted_tokens[(sid, attempt)] = dict(record)
                for k in adopted_keys:
                    _CONTINUATION_HANDOFF_TOKENS.pop(k, None)
                # #7862 re-gate CORE: the DURABLE handoff is consumed by this adoption,
                # but the IN-MEMORY token must survive. Two different consumers need it:
                #
                # * ``_receipt_attempt_is_live`` anchors a rollback receipt to its
                #   handoff's clock. Dropping the token makes a just-adopted attempt read
                #   as abandoned, so the next sweep drops the receipt and the retry runs
                #   as an ordinary turn — ``test_the_handoff_clock_survives_a_restart``
                #   pins that, and it is the round-10 fix this branch already carries.
                # * the record restored below is what the retry consumes.
                #
                # What must NOT survive is the durable copy: a handoff still on disk is
                # re-adopted by the next cold restore, which resurrects a continuation
                # the retry already spent. So the tokens are restored in memory and
                # marked as converted, and ``_write_registry_unlocked`` omits marked
                # sessions from the payload — one ``os.replace`` publishes the record
                # and the emptied handoff table together, so a crash in between leaves
                # the pre-adoption state (handoff, no record — re-adoptable) rather than
                # the double-spendable state (both).
                for key, value in adopted_tokens.items():
                    value["_handoff_adopted_into_record"] = True
                    _CONTINUATION_HANDOFF_TOKENS[key] = value
                _ADOPTED_HANDOFF_SESSIONS.add(sid)
                # CORE finding 4: the adopted record publishes master's
                # prompt/token mirror together with the marker, exactly like a
                # plain restored record — otherwise the resume continue loses
                # its identity admission after the orphan sweep.
                # #7862 re-gate round 3 (finding 5): a handoff that carried a
                # stream id but never executed (the crash window before
                # ``thr.start()``) still carries that id here. The restored
                # record must NOT: the id describes an attempt that did not run,
                # so leaving it on the record would let the next snapshot treat
                # the adoption as a spent admission and drop the continuation
                # again.
                _restored_record = dict(record)
                _restored_record.pop("_handoff_stream_id", None)
                _publish_continuation_mirror_unlocked(sid, _restored_record)
                PENDING_GOAL_CONTINUATION_RECORDS[sid] = _restored_record
                adopted += 1
                restored += 1
            if adopted:
                logger.info(
                    "Goal continuation registry: adopted %d durable continuation handoff(s) "
                    "from an interrupted start",
                    adopted,
                )
            # #7862 re-gate CORE (Oct 10): a REFUSED or already-launched handoff is
            # dropped in memory during the loop above, but the drop only becomes
            # real when the snapshot commits. Without this write the spent handoff
            # stays on disk, and every later cold restore re-reads it — which for
            # the already-launched case is exactly the reviewer's resurrection
            # (cold restore recovers 1 already-admitted continuation).
            if dropped:
                logger.info(
                    "Goal continuation registry: dropped %d non-adoptable durable "
                    "continuation handoff(s) (already launched or retired)",
                    dropped,
                )
            if restored or dropped:
                if _write_registry_unlocked(
                    PENDING_GOAL_CONTINUATION_RECORDS,
                    context=f"restore count={restored} dropped={dropped}",
                ):
                    return restored
                # The snapshot write failed, so the durable state still describes an
                # interrupted start with NO live record. Undo the conversion in memory
                # too: the tokens must go back to being ordinary handoffs (no marker,
                # no session in the converted set), otherwise this process would
                # withhold the crash-recovery evidence from every later snapshot and
                # the adoption would be lost for good. The records stay in memory —
                # dropping them would strand a continuation the UI still shows as
                # active.
                for key, value in adopted_tokens.items():
                    value.pop("_handoff_adopted_into_record", None)
                    _CONTINUATION_HANDOFF_TOKENS[key] = value
                for converted_sid in {k[0] for k in adopted_tokens}:
                    _ADOPTED_HANDOFF_SESSIONS.discard(converted_sid)
                logger.warning(
                    "Goal continuation registry: %d adoption(s) could not be made "
                    "durable; the handoff tokens stay claimable until reconciled",
                    adopted,
                )
                return 0
        return restored
    except _RegistryLockUnavailable:
        # A lock we cannot take must not fall back to an unlocked write;
        # the startup restore is skipped and the durable state stays
        # untouched for the next attempt.
        logger.error(
            "Goal continuation registry: the startup restore was skipped "
            "because the cross-process lock is unavailable"
        )
        return 0


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
            # #7862 re-gate round 15 (BRICK): a non-None value means the
            # cross-process lock refused a continuation mutation (no unlocked
            # fallback was taken).
            "last_lock_error": _LAST_LOCK_ERROR,
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
