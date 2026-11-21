"""#7862 round 21: the goal-continuation recovery findings that are still open
at head ``471700e1ea525fa89be57d0eae2ed7b4337618b9``.

Six Greptile findings were re-checked against the CURRENT head. This file
covers the four runtime ones that reproduce here, plus the two test-gap
findings that needed the suite itself fixed.

Runtime findings covered here:

* **R1 (restart expires valid queued continuations)** -- the startup sweep
  retires pending intent by wall-clock age. The RFC
  (``docs/rfcs/session-sse-contract-v1.md``) says a queued continuation has
  "no wall-clock expiry": it survives a refresh and is restored into the
  composer for a later send. A 24h TTL retires continuations that are still
  perfectly usable, and the browser's own token is then refused.
* **R2 (deferred recovery erases pending goals)** -- when the registry is
  unreadable, the deferred/live recovery treated "I could not read it" as
  "the records are gone" and snapshotted the emptied state.
* **R3 (a malformed field breaks recovery)** -- valid JSON with a bad
  numeric field raised out of the reader, so startup skipped the whole
  restore and a continuation send silently degraded to an ordinary message.
* **R4 (later recovery checks never run)** -- with two handoffs whose
  ownership windows end at different times, the first timer callback could
  not arm the second one: the timer reference was still "alive" while its
  own callback was running.
* **R5 (valid continuations get rejected cross-process)** -- process B
  refused a token that process A had already persisted, purely because B's
  in-memory marker set had not caught up yet.

Every test drives the REAL production path (the real store, the real
session-recovery startup hook, the real route entry point) with a fake
clock, fake events or fake workers. No real target, no real tool, no sleep.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PROMPT = "keep refining the release checklist"


# ── shared fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def clean_registry(tmp_path, monkeypatch):
    """Isolate the registry between cases -- memory AND the durable file.

    The durable file has to be isolated too: a case that writes a corrupt
    registry would otherwise leak into the next case and turn a green
    control into a red one (the pollution looks exactly like a defect).
    """
    from api import goal_continuation_store as store
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_PROMPTS,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        store, "_PENDING_GOAL_FILE", state_dir / "pending_goal_continuations.json"
    )
    monkeypatch.setattr(
        store, "_REGISTRY_LOCK_FILE", state_dir / "pending_goal_continuations.lock"
    )

    def _reset():
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_PROMPTS.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._CONTINUATION_HANDOFF_TOKENS.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._TOMBSTONES.clear()
            store._RETIRED_GENERATIONS.clear()
            store._DEFERRED_HANDOFF_RECOVERY.clear()
            store._SPENT_GENERATIONS.clear()
            store._ADOPTED_HANDOFF_SESSIONS.clear()
            store._LAST_LOAD_ERROR = None
            store._LAST_WRITE_ERROR = None
            store._DEFERRED_RECOVERY_TIMER = None

    _reset()
    try:
        yield store
    finally:
        _reset()


def _store():
    from api import goal_continuation_store as store

    return store


# ── R1: a valid queued continuation must survive a restart, however old ───


def test_a_valid_queued_continuation_survives_a_restart_however_old(
    clean_registry,
):
    """R1: the startup sweep must not retire a still-valid queued intent.

    The RFC is explicit: a queued continuation has NO wall-clock expiry --
    "the browser deliberately keeps a queued continuation - it survives a
    refresh and is restored for the user to send later - so a TTL retired
    continuations that were still perfectly usable."

    The reproduction: arm a continuation, age it well past the 24h TTL the
    sweep used, then run the REAL startup hook. The record must still be
    claimable afterwards -- and the browser's own token must still be
    accepted.
    """
    store = _store()
    sid = "sess-r21-aged"

    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-aged")
    assert (sid, ) and sid in store.load_pending_goal_continuations(), (
        "the continuation was not persisted, so the restart case cannot run"
    )

    # Age the durable record well past the 24h wall-clock TTL.
    stale = time.time() - (40 * 24 * 60 * 60)
    with store._LOCK:
        record = store.load_pending_goal_continuations()[sid]
        assert isinstance(record, dict)
        # Write the aged record back to disk through the real writer.
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        aged = dict(record)
        aged["created_at"] = stale
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = aged
        store._write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context="r21 age the durable record",
        )

    # The REAL startup entry point.
    from api.session_recovery import restore_goal_continuations_on_startup

    result = restore_goal_continuations_on_startup(REPO_ROOT / "does-not-exist")

    # The record was already live in this process's memory, so the restore's
    # "newly adopted" count can legitimately be 0. What must hold is that the
    # intent SURVIVED the restart sweep and is still claimable.
    assert sid in store.load_pending_goal_continuations(), (
        "the aged-but-valid continuation was not restored after the restart: "
        f"startup reported {result!r}"
    )
    assert sid not in store._TOMBSTONES, (
        "the aged continuation was retired instead of kept"
    )


def test_the_browser_token_of_an_aged_continuation_is_still_accepted(
    clean_registry,
):
    """R1 (second half): the token the browser still holds must be honoured.

    Greptile's finding names the user-visible symptom: "The browser still
    holds the original token, but sending it after the restart now raises
    ``_GoalContinuationTokenUnmatched``, so the queued turn cannot run."
    """
    store = _store()
    sid = "sess-r21-aged-token"

    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-aged-2")
    stale = time.time() - (40 * 24 * 60 * 60)
    with store._LOCK:
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        record = dict(PENDING_GOAL_CONTINUATION_RECORDS[sid])
        record["created_at"] = stale
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = record
        store._write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS,
            context="r21 age the durable record (token)",
        )

    from api.session_recovery import restore_goal_continuations_on_startup

    restore_goal_continuations_on_startup(REPO_ROOT / "does-not-exist")

    outcome = store.consume_pending_goal_continuation(sid, PROMPT, "tok-aged-2", "att-1")
    assert outcome == store.CONSUME_COMMITTED, (
        "the browser's token for an aged-but-valid continuation was refused "
        f"(outcome={outcome!r}); the queued turn cannot run after a restart"
    )


def test_the_expired_sweep_still_cleans_up_dead_records(clean_registry):
    """R1 (counter-control): removing the TTL must not remove the cleanup.

    The sweep's OTHER job -- retiring an intent whose record is gone, i.e.
    a marker without a record -- must survive. Otherwise the fix would
    trade "aged intent retired" for "the registry grows forever".
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION

    sid = "sess-r21-orphan"
    with store._LOCK:
        # A marker with no record: the exact shape the sweep exists to clean.
        PENDING_GOAL_CONTINUATION.add(sid)

    from api.goals import sweep_expired_goal_continuations

    swept = sweep_expired_goal_continuations()

    assert swept >= 1, (
        "the orphan sweep stopped cleaning marker-without-record pairs"
    )
    assert sid not in PENDING_GOAL_CONTINUATION, (
        "the orphan marker survived the sweep"
    )


def test_the_sweep_does_not_retire_a_continuation_newer_than_the_stamp(
    clean_registry,
):
    """R1 (P1, "cleanup deletes newer continuations"): compare generations.

    Greptile, on this round's own head: "With two WebUI processes sharing
    state, process A can keep an old retirement stamp after process B arms
    a new continuation. ``_merge_disk_state_unlocked()`` preserves that
    stamp but correctly accepts the newer record. A's next write saves
    both. After a restart, recovery restores the valid record, then this
    sweep deletes it because it checks only whether a stamp exists -- not
    whether it covers the record's generation."

    The reproduction is the state that merge leaves BEHIND in process A:
    A still holds the OLD retirement stamp, while the record it adopted
    from process B carries a NEWER generation. (A fresh ``arm`` clears
    the stamp in the arming process, so the stamp has to be put back the
    way the merge leaves it -- which is exactly what makes this the
    cross-process shape.)

    The stamp only says "the intent as of generation N was cancelled". A
    record newer than that is a different, still-valid intent.
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r21-newer-than-stamp"

    # The session was retired at generation N.
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-old")
    assert store.retire_pending_goal_continuation(sid, reason="clear"), (
        "the setup could not retire the session"
    )
    old_stamp = int(store._TOMBSTONES.get(sid) or 0)
    assert old_stamp > 0, "the setup left no retirement stamp"

    # A NEW continuation for the same session, at a newer generation.
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-new")
    with store._LOCK:
        record = dict(PENDING_GOAL_CONTINUATION_RECORDS[sid])
        assert int(record.get("generation") or 0) > old_stamp, (
            "the setup failed: the new record is not newer than the stamp"
        )
        # Process A still holds the OLD stamp: that is the state the merge
        # leaves behind when it adopts a peer's newer record.
        store._TOMBSTONES[sid] = old_stamp
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = record

    swept = store.sweep_expired_goal_continuations()

    assert sid in PENDING_GOAL_CONTINUATION_RECORDS, (
        "the sweep retired a continuation that is NEWER than the retirement "
        "stamp: with two processes, an old stamp in one of them would "
        "delete the other's live continuation, and after the next restart "
        "the browser's saved token could no longer resume the goal"
    )
    assert swept == 0, (
        "the sweep reported retiring an intent whose generation is newer "
        "than the retirement stamp"
    )


def test_the_sweep_keeps_an_intent_with_a_live_handoff(clean_registry):
    """R1 (P1, second half): the handoff check has to actually match.

    The handoff keys are ``(sid, attempt)`` tuples, so a string ``sid``
    is never found in them -- the condition was always true and the
    handoff was never consulted. A session with an admission still in
    flight must not be swept.
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r21-live-handoff"

    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-live")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-live", "att-live")
        == store.CONSUME_COMMITTED
    )
    # The consume left a HANDOFF for the in-flight attempt. Re-plant the
    # record so the sweep has something to look at.
    with store._LOCK:
        PENDING_GOAL_CONTINUATION_RECORDS[sid] = {
            "prompt": PROMPT,
            "generation": 1,
            "created_at": time.time(),
            "reason": "goal_continue",
            "continuation_id": "tok-live",
        }
        assert any(key[0] == sid for key in store._CONTINUATION_HANDOFF_TOKENS), (
            "the setup left no handoff for the session"
        )
        # A retirement stamp that covers the record.
        store._TOMBSTONES[sid] = 99

    swept = store.sweep_expired_goal_continuations()

    assert sid in PENDING_GOAL_CONTINUATION_RECORDS, (
        "the sweep retired an intent whose admission is still in flight: the "
        "handoff keys are (sid, attempt) tuples, so a string sid never "
        "matched them and the handoff was never consulted"
    )
    assert swept == 0, (
        "the sweep reported retiring an intent with a live handoff"
    )


# ── R2: an unreadable registry is not an empty registry ────────────────────


def test_an_unreadable_registry_does_not_erase_live_records(clean_registry):
    """R2: "I could not read it" must never mean "the records are gone".

    The deferred/live recovery reconciles local state with disk and then
    snapshots the result. If the registry cannot be read, the reconcile must
    NOT drop the live records, and the closing snapshot must not persist that
    drop.
    """
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r21-unreadable"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-unreadable")
    assert sid in PENDING_GOAL_CONTINUATION_RECORDS

    # Make the registry unreadable for the next read: a directory where the
    # file is expected makes json.load raise IsADirectoryError. The original
    # file is MOVED aside (not copied): it has to come back byte-identical
    # after the restore, because the assertion below is about whether the
    # restore persisted an "everything is gone" view OVER it.
    registry = store._PENDING_GOAL_FILE
    backup = registry.with_suffix(".json.r21-backup")
    backup.unlink(missing_ok=True)
    if registry.exists():
        registry.replace(backup)
    registry.mkdir(parents=True, exist_ok=True)
    try:
        store.restore_goal_continuations()
    finally:
        try:
            registry.rmdir()
        except OSError:
            pass
        if backup.exists():
            backup.replace(registry)

    assert sid in PENDING_GOAL_CONTINUATION_RECORDS, (
        "an unreadable registry was treated as an empty registry and the "
        "live pending records were dropped"
    )
    assert store._LAST_LOAD_ERROR, (
        "the unreadable registry did not record a load error, so nothing "
        "could tell the failure apart from an empty registry"
    )
    # The durable file must still hold the record: the restore must not have
    # persisted the "everything is gone" view over it.
    assert sid in store.load_pending_goal_continuations(), (
        "the unreadable registry was persisted as an empty registry: the "
        "durable records are gone and the next cold start restores nothing"
    )


def test_a_corrupt_registry_does_not_erase_live_records(clean_registry):
    """R2 (corrupt variant): a corrupt file is the same failure mode."""
    store = _store()
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    sid = "sess-r21-corrupt"
    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-corrupt")
    assert sid in PENDING_GOAL_CONTINUATION_RECORDS

    registry = store._PENDING_GOAL_FILE
    backup = registry.with_suffix(".json.r21-backup2")
    if registry.exists():
        registry.replace(backup)
    registry.write_text("{not json at all", encoding="utf-8")
    try:
        store.restore_goal_continuations()
    finally:
        registry.unlink(missing_ok=True)
        if backup.exists():
            backup.replace(registry)

    assert sid in PENDING_GOAL_CONTINUATION_RECORDS, (
        "a corrupt registry was treated as an empty registry and the live "
        "pending records were dropped"
    )
    assert sid in store.load_pending_goal_continuations(), (
        "the corrupt registry was persisted as an empty registry: the durable "
        "records are gone and the next cold start restores nothing"
    )


# ── R3: a malformed field must be a reportable failure, not a crash ───────


def test_a_bad_numeric_field_is_reported_instead_of_raising(clean_registry):
    """R3: valid JSON with a bad numeric field must not raise out of the reader.

    Greptile: "Valid JSON with a bad numeric field, such as
    ``\"created_at\": \"bad\"``, raises from these conversions instead of
    recording a load error. The top-level ``generation`` conversion has the
    same problem. Startup skips the entire restore; during a continuation
    send, the route catches the error and proceeds without ``goal_related``."
    """
    store = _store()
    registry = store._PENDING_GOAL_FILE
    payload = {
        "generation": "not-a-number",
        "records": {
            "sess-r21-bad-field": {
                "prompt": PROMPT,
                "generation": 3,
                "created_at": "bad",
                "reason": "goal_continue",
                "continuation_id": "tok-bad",
            }
        },
        "tombstones": {},
        "continuation_handoffs": {},
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")

    # The reader must not raise, and must record the failure.
    records, _tombstones, _handoffs, _spent = store._load_file_sections()
    assert store._LAST_LOAD_ERROR, (
        "a malformed record produced no load error, so the failure is "
        "invisible to the operator"
    )
    assert isinstance(records, dict)


def test_a_bad_creation_time_alone_is_reported_instead_of_raising(clean_registry):
    """R3 (the exact field Greptile named): ``created_at: "bad"``."""
    store = _store()
    registry = store._PENDING_GOAL_FILE
    payload = {
        "generation": 7,
        "records": {
            "sess-r21-bad-created": {
                "prompt": PROMPT,
                "generation": 3,
                "created_at": "bad",
                "reason": "goal_continue",
                "continuation_id": "tok-bad2",
            }
        },
        "tombstones": {},
        "continuation_handoffs": {},
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")

    records, _tombstones, _handoffs, _spent = store._load_file_sections()
    assert store._LAST_LOAD_ERROR, (
        "a malformed created_at produced no load error"
    )
    assert isinstance(records, dict)


def test_a_malformed_record_does_not_silently_degrade_a_continuation(
    clean_registry,
):
    """R3 (the user-visible half): the fault must be IDENTIFIABLE.

    "During a continuation send, the route catches the error and proceeds
    without ``goal_related``." A silent degrade to an ordinary message is
    the failure mode; the fix must make the fault observable instead of
    swallowing it into an ordinary turn.
    """
    store = _store()
    registry = store._PENDING_GOAL_FILE
    payload = {
        "generation": 7,
        "records": {
            "sess-r21-degrade": {
                "prompt": PROMPT,
                "generation": 3,
                "created_at": "bad",
                "reason": "goal_continue",
                "continuation_id": "tok-degrade",
            }
        },
        "tombstones": {},
        "continuation_handoffs": {},
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")

    # The startup path must not raise out of the restore, and must surface
    # the fault rather than silently restoring nothing.
    from api.session_recovery import restore_goal_continuations_on_startup

    result = restore_goal_continuations_on_startup(REPO_ROOT / "does-not-exist")

    assert "error" not in result, (
        "the startup hook raised out of the malformed-record path instead of "
        "recording it as a load failure"
    )
    assert store._LAST_LOAD_ERROR, (
        "the malformed record was swallowed: the fault is not identifiable, "
        "so an explicit continuation can silently degrade to an ordinary "
        "message"
    )


# ── R4: the second recovery deadline must be checked in the same lifetime ──


def test_a_later_deadline_is_checked_in_the_same_process_lifetime(
    clean_registry,
    monkeypatch,
):
    """R4: two handoffs, two deadlines, ONE process lifetime.

    Greptile: "When startup skips two handoffs with different deadlines, the
    first timer cannot schedule the next check. During its callback,
    ``_DEFERRED_RECOVERY_TIMER.is_alive()`` is still true, so this guard
    rejects the replacement timer. Once the callback ends, no production path
    schedules another check. The later continuation stays blocked until
    another restart."

    The reproduction uses a fake clock and real threads with a barrier: no
    fixed sleep is used to order the two deadlines.
    """
    store = _store()

    # Two sessions whose ownership windows end at different times.
    early_sid = "sess-r21-early"
    late_sid = "sess-r21-late"
    now = time.time()

    with store._LOCK:
        store._DEFERRED_HANDOFF_RECOVERY[(early_sid, "att-early")] = now + 0.05
        store._DEFERRED_HANDOFF_RECOVERY[(late_sid, "att-late")] = now + 0.25
        store._DEFERRED_RECOVERY_TIMER = None

    # Fake clock: the timer delay is computed from this, so we control the
    # ordering without sleeping for real durations.
    monkeypatch.setattr(store.time, "time", lambda: now)

    # The recovery pass that runs when the FIRST deadline arrives.
    calls = {"count": 0}
    original = store.restore_goal_continuations

    def _fake_restore():
        calls["count"] += 1
        return original()

    monkeypatch.setattr(store, "restore_goal_continuations", _fake_restore)

    # Fire the first deadline by hand, exactly as the timer callback would.
    # The state that matters: the timer for the FIRST deadline is still
    # referenced AND still alive while its own callback runs -- that is what
    # a real ``threading.Timer`` looks like from inside its callback, and it
    # is what made the "one timer per process" guard reject the successor.
    running = threading.Timer(30.0, lambda: None)
    running.daemon = True
    running.start()
    try:
        with store._LOCK:
            store._DEFERRED_RECOVERY_TIMER = running
        store._run_deferred_handoff_recovery()

        # After the first pass, the LATER deadline must be armed again: the
        # process must check it without another restart.
        assert (late_sid, "att-late") in store._DEFERRED_HANDOFF_RECOVERY, (
            "the later deadline was dropped from the recovery schedule"
        )
        timer = store._DEFERRED_RECOVERY_TIMER
        assert timer is not None, (
            "no timer was armed for the later deadline: the later continuation "
            "stays blocked until another restart"
        )
        assert timer is not running, (
            "the recovery did not arm a NEW timer for the later deadline: the "
            "previous timer was still referenced, so its own callback could "
            "never schedule the next check"
        )
        assert timer.is_alive(), (
            "the timer for the later deadline is not running"
        )
    finally:
        with store._LOCK:
            if store._DEFERRED_RECOVERY_TIMER is not None:
                store._DEFERRED_RECOVERY_TIMER.cancel()
        running.cancel()


def test_the_deferred_recovery_arms_the_next_deadline_from_its_own_callback(
    clean_registry,
    monkeypatch,
):
    """R4 (the guard itself): the callback must clear its own timer first.

    A ``threading.Timer`` is still ``is_alive()`` while its own callback is
    executing. The "one timer per process" guard therefore rejected the
    successor the callback itself wanted to arm. The fix: the callback
    clears the reference BEFORE it runs, so the guard sees no live timer.
    """
    store = _store()
    now = time.time()

    with store._LOCK:
        store._DEFERRED_HANDOFF_RECOVERY[("sess-r21-guard", "att")] = now + 10.0
        store._DEFERRED_RECOVERY_TIMER = None

    # A timer that is alive RIGHT NOW, standing in for the callback's own
    # timer (a Timer whose callback is running is still "alive").
    alive = threading.Timer(30.0, lambda: None)
    alive.daemon = True
    alive.start()
    try:
        with store._LOCK:
            store._DEFERRED_RECOVERY_TIMER = alive
            # The callback entry point clears the reference before running.
            store._run_deferred_handoff_recovery()
            after_callback = store._DEFERRED_RECOVERY_TIMER
        assert after_callback is not alive, (
            "the callback did not clear its own timer reference: the next "
            "deadline can never be armed from the callback"
        )
        assert after_callback is not None, (
            "the callback did not arm the next deadline: the later "
            "continuation stays blocked until another restart"
        )
        if after_callback is not None:
            after_callback.cancel()
    finally:
        alive.cancel()


# ── R5: a valid continuation persisted by a peer must not be rejected ──────


def test_a_peer_persisted_continuation_is_not_rejected_by_a_stale_local_marker(
    clean_registry,
    monkeypatch,
):
    """R5: the store's locked read decides, not the local set.

    Greptile: "With two WebUI processes sharing state, process A can save a
    new continuation while process B still has no local marker. Process B
    then rejects the valid token here before ``consume_pending_goal_continuation()``
    can reload the saved record."

    The reproduction: this process has NO local marker, but the durable
    registry holds a valid record (what the peer wrote). The route's
    pre-check must not refuse the token on the strength of the empty local
    set.
    """
    store = _store()
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    sid = "sess-r21-peer"

    # This process has NOT observed the peer yet: no local marker, no local
    # record. The durable registry holds the peer's record.
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()

    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-peer")
    # Simulate the peer: the durable file holds the record, this process's
    # in-memory set does not.
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.discard(sid)
        PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)

    assert sid not in PENDING_GOAL_CONTINUATION, (
        "the setup is wrong: this process must NOT have the local marker"
    )

    # The store's own locked read is the authority: it reloads under the
    # cross-process lock and finds the peer's record.
    outcome = store.consume_pending_goal_continuation(sid, PROMPT, "tok-peer", "att-peer")
    assert outcome == store.CONSUME_COMMITTED, (
        "the store refused a valid continuation that the peer had persisted "
        f"(outcome={outcome!r})"
    )


def test_the_route_pre_check_does_not_refuse_a_peer_persisted_token(
    clean_registry,
):
    """R5 (route level): the refusal must come from the store, not the set.

    The route currently refuses a token BEFORE calling the store, using the
    local marker set alone. The fix must let the store's locked read decide.
    """
    store = _store()
    from api.config import (
        PENDING_GOAL_CONTINUATION,
        PENDING_GOAL_CONTINUATION_RECORDS,
    )

    sid = "sess-r21-peer-route"

    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-peer-route")
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.discard(sid)
        PENDING_GOAL_CONTINUATION_RECORDS.pop(sid, None)

    # The store's locked read finds it.
    outcome = store.consume_pending_goal_continuation(
        sid, PROMPT, "tok-peer-route", "att-peer-route"
    )
    assert outcome == store.CONSUME_COMMITTED, (
        "the store did not reload the peer's record under the lock"
    )

    # The route source must not reject a token from the LOCAL set before
    # the store has had its locked read.
    routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    marker = "if not goal_related and goal_continuation_id:"
    idx = routes_src.find(marker)
    assert idx != -1, (
        "the route's token guard was renamed or removed; the test must "
        "follow the current source shape"
    )
    window = routes_src[idx:idx + 1200]
    assert "if s.session_id not in PENDING_GOAL_CONTINUATION:" not in window, (
        "the route still refuses a continuation token from the LOCAL marker "
        "set before the store's locked read can reload a peer's record"
    )


def test_a_spent_continuation_is_still_refused(clean_registry):
    """R5 (counter-control): single execution right must survive the change.

    "Valid continuations get rejected" must not become "everything is
    accepted": a token whose record is gone (already consumed) must still be
    refused, and an ordinary turn must not fall into the continuation path.
    """
    store = _store()
    sid = "sess-r21-spent"

    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-spent")
    assert (
        store.consume_pending_goal_continuation(sid, PROMPT, "tok-spent", "att-1")
        == store.CONSUME_COMMITTED
    )
    # A second consume of the same token: the record is gone.
    outcome = store.consume_pending_goal_continuation(sid, PROMPT, "tok-spent", "att-2")
    assert outcome == store.CONSUME_NOT_MATCHING, (
        f"a spent continuation was admitted again (outcome={outcome!r})"
    )


def test_an_ordinary_turn_is_not_refused_by_a_stale_state(clean_registry):
    """R5 (counter-control 2): an ordinary message must not be mis-refused.

    The existing contract: "ordinary messages also are not wrongly refused
    by stale state."
    """
    store = _store()
    sid = "sess-r21-ordinary"

    store.arm_pending_goal_continuation(sid, PROMPT, continuation_id="tok-ordinary")
    # An unrelated turn: no token, unrelated text -> stays ordinary, intent
    # stays pending.
    outcome = store.consume_pending_goal_continuation(sid, "hello there", "", "att-x")
    assert outcome == store.CONSUME_NOT_MATCHING, (
        f"an ordinary turn was consumed as a continuation (outcome={outcome!r})"
    )
    assert sid in store.load_pending_goal_continuations(), (
        "an ordinary turn retired the pending continuation"
    )
