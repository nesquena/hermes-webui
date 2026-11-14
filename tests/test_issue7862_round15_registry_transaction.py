"""#7862 re-gate (2026-10-29): the four CORE problems in the registry
transaction plus the should-fix items from the maintainer's CHANGES_REQUESTED
review at head ``ac22106df24b`` (review 5693755678).

The review confirmed round 14 GREEN ("The round-14 tests fail 11 of 15 on the
previous head and all pass here") and then blocked release on the new
process-wide transaction:

1. **A filesystem without working ``flock`` breaks every chat start**
   (BRICK, both reviewers) — ``_registry_transaction`` puts only ``os.open``
   inside the OSError fallback, so ``fcntl.flock`` raising ENOSYS/ENOLCK
   (NFS mounts, container overlays) 500s an ORDINARY start in a session with
   no goal. ``record_*``/``discard_*`` must return early when no in-memory
   handoff exists, and a lock-acquisition failure must refuse the continuation
   mutation — never fall back to an unlocked write, never fail the chat start
   itself.
2. **Restoration during another process's admission launches two goal
   workers** (CORE, Codex) — a cold restore adopts a PEER's live handoff as
   abandoned, so both processes admit and two ``goal_related=True`` workers
   launch. Keep exclusive ownership through launch or rollback: adopt a
   handoff only after proving its owner is gone (the handoff's liveness
   clock, reclaimed through the receipt path), and REJECT an unmatched
   non-empty continuation token rather than launching an ordinary turn.
3. **Another process's write erases crash-recovery ownership** (CORE, Codex)
   — snapshots serialize only LOCAL handoffs while disk handoffs are loaded
   but ignored, so process B's snapshot silently drops process A's in-flight
   handoff. Merge the complete durable handoff table under the transaction
   lock.
4. **Generations aren't comparable across processes, so a cleared goal comes
   back** (CORE) — ``_merge_disk_state_unlocked`` never raises the local
   ``_GENERATION``; only startup does. A cold process retires at generation 1
   while another process's receipt holds generation 3, and the rollback
   restores a goal the user cleared. Baseline the counter from the persisted
   global generation and every record/handoff/tombstone generation before
   allocating each mutation's generation.

Should fix: the warm-path ENOSPC rollback restore must be exempt from the
merge's "absent on disk => gone" drop; a corrupt/unreadable registry must not
wipe live memory and persist the wipe; merge-adopted peer records must
publish the prompt/token mirror (or the orphan sweep strips the marker); a
session delete with no record/handoff/receipt must not write an fsynced
tombstone snapshot.

Every test drives the REAL production shape (the real store, the real
``_start_chat_stream_for_session`` route, the real delete route, the real
``fcntl.flock``), RED-first on ``ac22106df24b``.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import json
import os
import time

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"

# ── fixtures / helpers (round-13/14 shape) ────────────────────────────────


@pytest.fixture
def clean_registry():
    """Isolate the registry between cases (round-12/13 shape)."""
    from api import goal_continuation_store as store
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    def _reset():
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._LAST_LOAD_ERROR = None
            store._LAST_WRITE_ERROR = None
            store._RETIRED_LOG.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._RETIRED_GENERATIONS.clear()
            store._TOMBSTONES.clear()
            store._CONTINUATION_HANDOFF_TOKENS.clear()
            store._ADOPTED_HANDOFF_SESSIONS.clear()
            store._GENERATION = 0
            store._RESTORE_WRITE_FAILED_SESSIONS.clear()
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

    _reset()
    yield store
    _reset()


def _cold_reset(store):
    """Simulate a fresh process: drop in-memory state, keep the registry file."""
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_PROMPTS.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._ADOPTED_HANDOFF_SESSIONS.clear()
        store._GENERATION = 0


_MEMORY_ATTRS = (
    "_TOMBSTONES",
    "_RETIRED_GENERATIONS",
    "_CONTINUATION_HANDOFF_TOKENS",
    "_ADOPTED_HANDOFF_SESSIONS",
    "_ROLLBACK_RECEIPTS",
)


def _snapshot_memory(store):
    """Deep-copy this process's registry memory (a 'second process' view)."""
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    snap = {
        "PENDING_GOAL_CONTINUATION": set(PENDING_GOAL_CONTINUATION),
        "PENDING_GOAL_CONTINUATION_PROMPTS": copy.deepcopy(PENDING_GOAL_CONTINUATION_PROMPTS),
        "PENDING_GOAL_CONTINUATION_RECORDS": copy.deepcopy(PENDING_GOAL_CONTINUATION_RECORDS),
        "_GENERATION": store._GENERATION,
    }
    for attr in _MEMORY_ATTRS:
        snap[attr] = copy.deepcopy(getattr(store, attr))
    return snap


def _restore_memory(store, snap):
    """Put a previously snapshotted process view back (stale by construction)."""
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION.update(snap["PENDING_GOAL_CONTINUATION"])
        PENDING_GOAL_CONTINUATION_PROMPTS.clear()
        PENDING_GOAL_CONTINUATION_PROMPTS.update(
            copy.deepcopy(snap["PENDING_GOAL_CONTINUATION_PROMPTS"])
        )
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.update(
            copy.deepcopy(snap["PENDING_GOAL_CONTINUATION_RECORDS"])
        )
        store._GENERATION = snap["_GENERATION"]
        for attr in _MEMORY_ATTRS:
            getattr(store, attr).clear()
            getattr(store, attr).update(copy.deepcopy(snap[attr]))


def _make_session(sid, workspace):
    """Create a REAL persisted session (the route serializes it)."""
    from api.models import SESSIONS, Session

    session = Session(
        session_id=sid,
        title="Goal continuation",
        workspace=str(workspace),
        model="test-model",
        model_provider="test-provider",
        messages=[{"role": "user", "content": "earlier", "timestamp": 1}],
    )
    session.save()
    SESSIONS[sid] = session
    return session


def _clean_journals(sid):
    from api.models import SESSION_DIR

    for sub in ("_run_journal", "_turn_journal"):
        target = SESSION_DIR / sub / sid
        try:
            for p in target.rglob("*"):
                if p.is_file():
                    p.unlink()
            target.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _arm_and_consume(store, sid, attempt_id=None, prompt=PROMPT):
    """Producer register + a real store consume; returns the token."""
    from api import goals

    cont_id = goals.register_pending_goal_continuation(sid, prompt)
    assert cont_id and len(cont_id) == 32
    assert (
        store.consume_pending_goal_continuation(
            sid, prompt, cont_id, attempt_id or "att-x"
        )
        == store.CONSUME_COMMITTED
    )
    return cont_id


# ── CORE 1 (BRICK): a broken flock must not break the chat path ───────────


class TestBrokenRegistryLockDoesNotBreakChatStart:
    """``_registry_transaction`` leaves ``fcntl.flock`` OUTSIDE the OSError
    fallback, so ENOSYS/ENOLCK (NFS mounts, container overlays) 500s every
    chat start, goal or not. Ordinary starts must not take the file lock at
    all (no in-memory handoff), and a lock-acquisition failure must REFUSE
    the continuation mutation — never an unlocked write, never a chat-start
    failure."""

    def test_an_ordinary_chat_start_survives_a_broken_flock(
        self, clean_registry, monkeypatch, tmp_path
    ):
        """CORE 1 (BRICK): with ``flock`` raising ENOSYS, an ordinary start
        in a session with NO goal still returns 200 and launches its worker."""
        import api.routes as routes

        sid = "sess-r15-brick-ordinary"

        def _broken_flock(fd, op, *args):
            raise OSError(errno.ENOSYS, "flock not supported")

        monkeypatch.setattr(fcntl, "flock", _broken_flock)
        session = _make_session(sid, tmp_path)

        captured = []
        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
        monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)

        def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
            captured.append(kwargs)

        monkeypatch.setattr(routes, "_run_agent_streaming", _recorder)
        try:
            response = routes._start_chat_stream_for_session(
                session,
                msg="just a normal question",
                workspace=str(tmp_path),
                model="test-model",
                model_provider="test-provider",
            )
            assert int(response.get("_status", 200) or 200) == 200, (
                "an ordinary chat start failed because the registry flock "
                "raised ENOSYS: record/discard take the cross-process lock "
                "even when nothing was consumed (BRICK, CORE finding 1)"
            )
            deadline = time.time() + 2.0
            while not captured and time.time() < deadline:
                time.sleep(0.01)
            assert captured, "the ordinary worker boundary never ran"
            assert captured[0].get("goal_related") is not True
        finally:
            _clean_journals(sid)

    def test_record_and_discard_without_a_handoff_never_take_the_file_lock(
        self, clean_registry, monkeypatch
    ):
        """CORE 1 (BRICK): ``record_goal_continuation_stream`` and
        ``discard_goal_continuation_handoff`` return early under ``_LOCK``
        when no in-memory handoff exists — the file lock is only for real
        admissions."""
        store = clean_registry

        def _broken_flock(fd, op, *args):
            raise OSError(errno.ENOSYS, "flock not supported")

        monkeypatch.setattr(fcntl, "flock", _broken_flock)
        assert (
            store.record_goal_continuation_stream("sess-r15-brick-rec", "att-none", "stream-x")
            is True
        )
        assert (
            store.discard_goal_continuation_handoff("sess-r15-brick-rec", "att-none")
            is True
        )

    def test_registry_mutations_are_refused_while_the_lock_is_broken(
        self, clean_registry, monkeypatch
    ):
        """CORE 1 (BRICK): with the flock broken, an arm refuses (no unlocked
        write, no registry file), a real consume refuses with
        ``CONSUME_COMMIT_FAILED`` (never an ordinary-turn swallow), and a real
        handoff's stream binding refuses — the durable state is untouched."""
        store = clean_registry
        sid = "sess-r15-brick-arm"

        def _broken_flock(fd, op, *args):
            raise OSError(errno.ENOSYS, "flock not supported")

        # Arm while the lock works (the durable record must exist for the
        # consume refusal below).
        cont_id = _arm_and_consume(store, sid, attempt_id="att-pre")

        monkeypatch.setattr(fcntl, "flock", _broken_flock)
        assert (
            store.arm_pending_goal_continuation(
                "sess-r15-brick-arm2", PROMPT, continuation_id="b" * 32
            )
            is False
        )
        # No unlocked write: the refused arm never reached disk.
        payload = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
        assert "sess-r15-brick-arm2" not in (payload.get("records") or {}), (
            "an arm with a broken flock wrote an unlocked snapshot"
        )
        assert (
            store.consume_pending_goal_continuation(
                sid, PROMPT, cont_id, "att-after"
            )
            == store.CONSUME_COMMIT_FAILED
        ), (
            "a matched continuation with a broken lock must REFUSE admission, "
            "not fall through as an ordinary turn (CORE finding 1)"
        )
        # The handoff from the pre-lock consume is still live: binding its
        # stream identity refuses cleanly instead of writing unlocked.
        assert (
            store.record_goal_continuation_stream(sid, "att-pre", "stream-broken")
            is False
        )
        assert (
            store.discard_goal_continuation_handoff(sid, "att-pre") is False
        )
        # Nothing was written while the lock was broken: the durable handoff
        # still carries no stream id.
        records, _tombstones, handoffs = store._load_file_raw()
        assert sid not in records
        assert handoffs.get((sid, "att-pre"), {}).get("_handoff_stream_id", "") == ""


# ── CORE 2: restore must not adopt another process's live handoff ─────────


class TestRestoreRespectsALivePeersHandoff:
    """A cold restore adopted a LIVE peer's durable handoff as abandoned,
    so both processes admitted and two ``goal_related=True`` workers ran.
    Adopt a handoff only after proving its owner is gone: the handoff's
    liveness clock (``_handoff_minted_at``, refreshed by the receipt reclaim)
    marks a recent handoff as exclusively owned through launch or rollback."""

    def test_a_fresh_handoff_is_left_for_its_live_owner(
        self, clean_registry, monkeypatch
    ):
        """CORE 2: process A consumed and is mid-admission (handoff minted
        moments ago). A cold restart of process B must NOT adopt it — the
        handoff stays on disk for A's launch/rollback to discharge."""
        store = clean_registry
        sid = "sess-r15-live-handoff"
        _arm_and_consume(store, sid, attempt_id="att-live")

        _cold_reset(store)
        assert store.restore_goal_continuations() == 0, (
            "a cold restore adopted a LIVE peer's handoff: both processes "
            "would launch a goal worker for the same continuation "
            "(CORE finding 2)"
        )
        # The admission's ownership evidence stays on disk for its owner.
        _records, _tombstones, handoffs = store._load_file_raw()
        assert (sid, "att-live") in handoffs, (
            "the live peer's handoff was erased from disk by the restoration"
        )

    def test_an_abandoned_handoff_is_still_adopted(self, clean_registry):
        """CORE 2 control: when the owner stops reclaiming (clock older than
        the TTL), the handoff IS adopted — crash recovery still works."""
        store = clean_registry
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS, PENDING_GOAL_CONTINUATION_PROMPTS

        sid = "sess-r15-abandoned-handoff"
        _arm_and_consume(store, sid, attempt_id="att-dead")
        # The owner's clock goes stale (it crashed before launch).
        with store._LOCK:
            store._CONTINUATION_HANDOFF_TOKENS[(sid, "att-dead")][
                "_handoff_minted_at"
            ] = time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 60
        with store._LOCK:
            store._write_registry_unlocked(PENDING_GOAL_CONTINUATION_RECORDS, context="aging")

        _cold_reset(store)
        assert store.restore_goal_continuations() == 1, (
            "an abandoned handoff (owner gone) was not adopted: recovery "
            "lost the continuation"
        )
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        assert sid in PENDING_GOAL_CONTINUATION_PROMPTS

    def test_a_token_bearing_dispatch_for_a_spent_intent_is_refused(
        self, clean_registry, monkeypatch, tmp_path
    ):
        """CORE 2: another process (or this one's earlier dispatch) already
        spent the intent; a browser dispatch that STILL carries the token
        must be REJECTED, not launched as an ordinary turn — an ordinary
        launch is how the second ``goal_related=False`` worker appeared."""
        import api.routes as routes

        store = clean_registry
        sid = "sess-r15-unmatched-token"
        cont_id = _arm_and_consume(store, sid, attempt_id="att-first")
        session = _make_session(sid, tmp_path)

        captured = []
        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
        monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)

        def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
            captured.append(kwargs)

        monkeypatch.setattr(routes, "_run_agent_streaming", _recorder)
        try:
            with pytest.raises(routes._GoalContinuationTokenUnmatched):
                routes._start_chat_stream_for_session(
                    session,
                    msg=PROMPT,
                    workspace=str(tmp_path),
                    model="test-model",
                    model_provider="test-provider",
                    goal_continuation_id=cont_id,
                )
            time.sleep(0.05)
            assert not captured, (
                "a dispatch carrying an unmatched continuation token launched "
                "an ordinary turn instead of refusing (CORE finding 2): the "
                "continuation was already admitted/spent elsewhere"
            )
        finally:
            _clean_journals(sid)

    def test_a_tokenless_ordinary_turn_still_launches(self, clean_registry, monkeypatch, tmp_path):
        """CORE 2 control: a tokenless genuine turn after the intent was spent
        stays ordinary (the approved round-13 contract is untouched)."""
        import api.routes as routes

        store = clean_registry
        sid = "sess-r15-tokenless-control"
        _arm_and_consume(store, sid, attempt_id="att-first")
        session = _make_session(sid, tmp_path)

        captured = []
        monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
        monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _cfg: False)

        def _recorder(session_id, msg, model, workspace, stream_id, attachments, **kwargs):
            captured.append(kwargs)

        monkeypatch.setattr(routes, "_run_agent_streaming", _recorder)
        try:
            response = routes._start_chat_stream_for_session(
                session,
                msg="what is the status?",
                workspace=str(tmp_path),
                model="test-model",
                model_provider="test-provider",
            )
            assert int(response.get("_status", 200) or 200) == 200
            deadline = time.time() + 2.0
            while not captured and time.time() < deadline:
                time.sleep(0.01)
            assert captured, "the ordinary worker boundary never ran"
            assert captured[0].get("goal_related") is not True
        finally:
            _clean_journals(sid)


# ── CORE 3: a peer writer's snapshot must keep my handoff ─────────────────


class TestPeerWritersKeepCrashRecoveryOwnership:
    """Snapshots serialize only LOCAL handoffs while disk handoffs are loaded
    but ignored, so process B's unrelated write erased process A's in-flight
    handoff and a cold restore lost A's continuation. The transaction must
    merge the complete durable handoff table."""

    def test_an_unrelated_writers_snapshot_preserves_a_peers_handoff(
        self, clean_registry
    ):
        store = clean_registry
        from api import goals

        sid_x = "sess-r15-xproc-x"
        sid_a = "sess-r15-xproc-a"
        sid_z = "sess-r15-xproc-z"

        goals.register_pending_goal_continuation(sid_x, PROMPT)
        # Process B's startup view: it knows X, but NOT A's in-flight handoff.
        _cold_reset(store)
        assert store.restore_goal_continuations() == 1
        process_b = _snapshot_memory(store)

        # Process A consumes its intent: the durable handoff is now the ONLY
        # crash-recovery evidence for that admission.
        _arm_and_consume(store, sid_a, attempt_id="att-A")
        records, _tombstones, handoffs = store._load_file_raw()
        assert (sid_a, "att-A") in handoffs, "the handoff did not land durably"

        # Process B (stale view, no A) arms an unrelated session: its snapshot
        # must first merge the complete durable handoff table, so A's
        # crash-recovery ownership survives B's write.
        _restore_memory(store, process_b)
        goals.register_pending_goal_continuation(sid_z, PROMPT)

        records, _tombstones, handoffs = store._load_file_raw()
        assert (sid_a, "att-A") in handoffs, (
            "B's unrelated write erased A's durable handoff: a cold restore "
            "loses A's continuation (CORE finding 3)"
        )
        # The merge also re-adopts the peer handoff into memory, so B's NEXT
        # snapshot keeps it too without depending on a fresh disk read.
        assert (sid_a, "att-A") in store._CONTINUATION_HANDOFF_TOKENS, (
            "the peer handoff was not merged into this process's memory"
        )


# ── CORE 4: generations must be comparable across processes ───────────────


class TestGenerationBaselineAcrossProcesses:
    """Promptless: a cold process counts generations from 0, so B retires at
    generation 1 while A's rollback receipt holds generation 3 and the
    rejected-start rollback resurrects a goal the user cleared. Every
    transaction must baseline ``_GENERATION`` above the persisted global
    generation and every record/handoff/tombstone generation on disk."""

    def test_a_cold_retire_baselines_the_counter_above_disk(self, clean_registry):
        store = clean_registry
        sid = "sess-r15-gen-baseline"

        # Process A arms twice (receipt generation is strictly above the
        # first arm) and consumes; the durable file carries that generation.
        from api import goals

        goals.register_pending_goal_continuation(sid, PROMPT)
        cont_id = goals.register_pending_goal_continuation(sid, PROMPT)
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-gen")
            == store.CONSUME_COMMITTED
        )
        # A's process view, including the rollback receipt for the rejected
        # start (the record it consumed, with its generation).
        process_a = _snapshot_memory(store)

        # Process B starts cold and the user clears the goal in B.
        _cold_reset(store)
        assert store.retire_pending_goal_continuation(sid, reason="clear") is True

        # The retire tombstone must be NEWER than anything A's process ever
        # minted — never B's own fresh generation-1 counter.
        tombstone = int(store._TOMBSTONES.get(sid) or 0)
        assert tombstone > process_a["_GENERATION"], (
            "a cold process retired at its own generation counter while the "
            "disk carried newer generations: the clear is incomparable with "
            "A's receipt (CORE finding 4)"
        )

        # A's rejected-start rollback lands late: the receipt's generation is
        # OLDER than the clear tombstone, so the intent must NOT come back.
        _restore_memory(store, process_a)
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-gen")
        assert receipt is not None
        accepted = store.restore_pending_goal_continuation(sid, receipt)
        assert accepted is False, (
            "A's rollback restored a continuation the user cleared in B: "
            "generations were not comparable across processes (CORE finding 4)"
        )
        records, _tombstones, _handoffs = store._load_file_raw()
        assert sid not in records


# ── should-fix: warm ENOSPC, corrupt registry, mirror on merge, no-goal ───


class TestWarmRestoreEnospcExemption:
    """The rollback-restore write failed with ENOSPC: the record is live in
    memory but absent on disk, and the NEXT unrelated transaction's merge
    dropped it ("absent on disk => gone"), so the warm retry ran as an
    ordinary turn. A session whose restore write failed must be exempt from
    the drop until its record lands."""

    def test_a_failed_restore_write_is_exempt_from_the_merge_drop(
        self, clean_registry, monkeypatch
    ):
        store = clean_registry
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-r15-warm-enospc"
        _arm_and_consume(store, sid, attempt_id="att-warm")

        # The route's rejected-start rollback: claim the receipt (the
        # rejected start pops it, then restores).
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-warm")
        assert receipt is not None

        real_replace = os.replace

        def _enospc_registry_replace(src, dst):
            if "pending_goal_continuations" in str(dst):
                raise OSError("disk full (simulated)")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _enospc_registry_replace)
        assert store.restore_pending_goal_continuation(sid, receipt) is True, (
            "the rollback restore refused while the snapshot could not commit"
        )
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        monkeypatch.setattr(os, "replace", real_replace)

        # An unrelated transaction (another session's arm) runs its merge
        # WITHOUT the failed-restore session being treated as gone.
        from api import goals

        goals.register_pending_goal_continuation("sess-r15-warm-other", PROMPT)

        assert sid in PENDING_GOAL_CONTINUATION_RECORDS, (
            "an unrelated merge dropped the warm-path restored record whose "
            "snapshot write had failed (absent on disk => gone)"
        )
        records, _tombstones, _handoffs = store._load_file_raw()
        assert sid in records, (
            "the warm-retry record never landed durably after the failing "
            "restore write recovered"
        )


class TestCorruptRegistryDoesNotWipeMemory:
    """A corrupt/unreadable registry ``_merge_disk_state_unlocked`` treated
    as an empty disk, dropping every live record and persisting the wipe."""

    def test_a_corrupt_registry_does_not_wipe_live_memory(self, clean_registry):
        store = clean_registry
        from api import goals
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid_x = "sess-r15-corrupt-x"
        goals.register_pending_goal_continuation(sid_x, PROMPT)
        assert sid_x in PENDING_GOAL_CONTINUATION_RECORDS

        # The registry file becomes unreadable (a crashed write, EACCES).
        store._PENDING_GOAL_FILE.write_text("{definitely not json", encoding="utf-8")

        # An unrelated writer runs a transaction: the merge must NOT treat
        # the unreadable disk as "empty, so everything is gone".
        goals.register_pending_goal_continuation("sess-r15-corrupt-y", PROMPT)

        assert sid_x in PENDING_GOAL_CONTINUATION_RECORDS, (
            "a corrupt registry wiped the live in-memory records: the merge "
            "treated an unreadable disk as empty"
        )
        records, _tombstones, _handoffs = store._load_file_raw()
        assert sid_x in records, (
            "a corrupt registry was persisted as a WIPE: the live record was "
            "dropped from the closing snapshot"
        )


class TestMergeAdoptionPublishesThePromptMirror:
    """A peer record adopted by ``_merge_disk_state_unlocked`` published the
    marker and RECORDS but not master's PROMPTS mirror, so the next /goal
    orphan sweep stripped the marker and the resume continue lost its
    identity admission."""

    def test_a_merge_adopted_peer_record_publishes_the_mirror(self, clean_registry):
        store = clean_registry
        from api import goals
        from api.config import PENDING_GOAL_CONTINUATION_PROMPTS

        sid_x = "sess-r15-mirror-x"
        sid_z = "sess-r15-mirror-z"

        # Process B's stale view: it has never seen X.
        _cold_reset(store)
        process_b = _snapshot_memory(store)

        # Process A arms X durably (record + mirror in A).
        cont_id = goals.register_pending_goal_continuation(sid_x, PROMPT)

        # B (stale) arms Z: its merge adopts X as a peer record.
        _restore_memory(store, process_b)
        goals.register_pending_goal_continuation(sid_z, PROMPT)

        mirror = PENDING_GOAL_CONTINUATION_PROMPTS.get(sid_x)
        assert isinstance(mirror, dict), (
            "a merge-adopted peer record did not publish the prompt/token "
            "mirror: the /goal orphan sweep strips the marker and the "
            "resume continue loses its identity admission"
        )
        assert mirror.get("continuation_id") == cont_id
        assert goals.peek_pending_goal_continuation_id(sid_x) == cont_id


class TestNoGoalSessionDeleteSkipsRegistryWrite:
    """Every session delete writes an fsynced registry snapshot with a
    tombstone, goal or not. With no record, handoff or receipt the
    retirement must return early — no write, no tombstone."""

    def test_delete_without_goal_state_does_not_touch_the_registry(
        self, tmp_path, monkeypatch
    ):
        import api.models as models
        import api.routes as routes
        from api import goal_continuation_store as store

        session_dir = tmp_path / "sessions"
        session_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
        models.SESSIONS.clear()

        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_PROMPTS,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )

        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._GENERATION = 0
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

        sid = "sess-r15-no-goal-delete"
        models.Session(session_id=sid, title="Session").save(
            touch_updated_at=False, skip_index=True
        )

        calls = []
        real_retire = routes.retire_pending_goal_continuation

        def _spy_retire(sid, reason="consumed"):
            calls.append((sid, reason))
            return real_retire(sid, reason=reason)

        monkeypatch.setattr(routes, "retire_pending_goal_continuation", _spy_retire)
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
        monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
        monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
        monkeypatch.setattr(
            routes, "_session_is_subagent_view_only", lambda value: False
        )
        captured = {}
        handler = type(
            "H",
            (),
            {
                "command": "POST",
                "wfile": None,
                "send_response": lambda self, status: None,
                "send_header": lambda self, key, value: None,
                "end_headers": lambda self: None,
            },
        )()
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, status=200, extra_headers=None: captured.update(
                payload=payload, status=status
            )
            or True,
        )

        from types import SimpleNamespace

        assert (
            routes.handle_post(handler, SimpleNamespace(path="/api/session/delete"))
            is True
        )
        payload = captured.get("payload") or {}
        assert payload.get("ok") is True
        assert not calls, (
            "a session with no continuation record/handoff/receipt still "
            "wrote an fsynced registry snapshot with a tombstone"
        )
        assert not store._PENDING_GOAL_FILE.exists()

    def test_delete_with_goal_state_still_retires(self, tmp_path, monkeypatch):
        """Control: an armed session's delete still runs the retirement."""
        import api.models as models
        import api.routes as routes
        from api import goal_continuation_store as store
        from api import goals

        session_dir = tmp_path / "sessions"
        session_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
        models.SESSIONS.clear()

        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_PROMPTS,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )

        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._GENERATION = 0
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

        sid = "sess-r15-goal-delete"
        models.Session(session_id=sid, title="Session").save(
            touch_updated_at=False, skip_index=True
        )
        goals.register_pending_goal_continuation(sid, PROMPT)

        calls = []
        real_retire = routes.retire_pending_goal_continuation

        def _spy_retire(sid, reason="consumed"):
            calls.append((sid, reason))
            return real_retire(sid, reason=reason)

        monkeypatch.setattr(routes, "retire_pending_goal_continuation", _spy_retire)
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(routes, "read_body", lambda _handler: {"session_id": sid})
        monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
        monkeypatch.setattr(routes, "_is_messaging_session_id", lambda value: False)
        monkeypatch.setattr(
            routes, "_session_is_subagent_view_only", lambda value: False
        )
        captured = {}
        handler = type(
            "H",
            (),
            {
                "command": "POST",
                "wfile": None,
                "send_response": lambda self, status: None,
                "send_header": lambda self, key, value: None,
                "end_headers": lambda self: None,
            },
        )()
        monkeypatch.setattr(
            routes,
            "j",
            lambda _handler, payload, status=200, extra_headers=None: captured.update(
                payload=payload, status=status
            )
            or True,
        )

        from types import SimpleNamespace

        assert (
            routes.handle_post(handler, SimpleNamespace(path="/api/session/delete"))
            is True
        )
        assert any(sid in c[0] for c in calls), (
            "an armed session's delete skipped the continuation retirement"
        )