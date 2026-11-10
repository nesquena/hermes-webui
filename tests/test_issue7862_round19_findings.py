"""#7862 re-gate round 19: the two P1 findings on head ``0299ee71b``.

Greptile's 2026-11-08 re-review (the batch posted after the round-18 push)
confirmed rounds 17/18 GREEN and blocked release on two P1s — both are the
COMPOSITION of a repaired mechanism with another, not a regression of it:

1. **Completed work returns after recovery** (``api/goal_continuation_store.py``)
   — the timer now calls ``restore_goal_continuations()`` in a RUNNING process
   (round 18's deferred handoff recovery), but that function's closing
   snapshot serialised this process's cached handoffs without removing the
   ones another process had already discharged. Startup caches two handoffs
   with different deadlines; another process launches and discharges the later
   one before the earlier timer fires; recovering the earlier handoff then
   writes the discharged handoff back to disk, and a later restart restores
   work that already ran. Fix: reconcile the cached records/handoffs with disk
   under the file lock before the live recovery saves its snapshot.

2. **Wrapped sends spend resume tokens** (``static/messages.js``) — the round-18
   fix for "busy resume goes to steer" claimed the resume-bound continuation
   identity BEFORE the busy routing, but did so unconditionally, so it also
   ran on an IDLE send. After ``/goal resume``, selecting a skill and sending
   ``continue`` claimed the token on the typed text; the ``/use`` wrapper then
   changed the posted text, and the POST-boundary final-text check was skipped
   because ``_goalContinuationId`` was already set. The server admits any
   request carrying the token, so the pending continuation was spent on a send
   that previously stayed ordinary. Fix: limit the early claim to busy
   routing; idle sends claim only after the final message is built.

Schedule 1 drives the REAL store in one interpreter with the durable registry
file, and models the peer's committed discharge by rewriting that file — the
subject of the finding is the CONTENT of this process's recovery snapshot, not
cross-process interleaving (the round-16/17 worker design keeps that). Schedule
2 drives the REAL ``send()`` sliced whole from ``static/messages.js`` in node
with the REAL token helpers from ``static/ui.js`` (the round-18 method).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"

REPO_ROOT = Path(__file__).resolve().parents[1]

from api.goal_continuation_store import _ROLLBACK_RECEIPT_TTL_SECONDS as _TTL  # noqa: E402

# ── fixtures / helpers (round-18 shape) ───────────────────────────────────

from tests.test_issue7862_round18_findings import (  # noqa: E402
    _arm_and_consume,
    _cancel_deferred_timer,
    clean_registry,
)


def _rewrite_registry_payload(store, mutate) -> dict:
    """Apply *mutate* to the on-disk payload, exactly as a peer process would.

    The test process is single-threaded and holds no transaction here, so the
    rewrite is a faithful model of "another process committed this mutation
    durably and THIS process never observed it in memory" — the precondition
    the finding's schedule needs.
    """
    payload = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
    mutate(payload)
    store._PENDING_GOAL_FILE.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _simulate_restart(store) -> None:
    """Drop every in-memory table, keeping the durable file (a cold process)."""
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._DEFERRED_HANDOFF_RECOVERY.clear()
        store._ADOPTED_HANDOFF_SESSIONS.clear()
        store._SPENT_GENERATIONS.clear()
        store._RESTORE_WRITE_FAILED_SESSIONS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
    _cancel_deferred_timer(store)


# ── finding 1: the live recovery must not republish discharged work ────────


class TestALiveRecoveryDoesNotRepublishDischargedWork:
    """Startup caches two handoffs with different deadlines and schedules a
    recovery for the earlier one. Another process launches and discharges the
    LATER one before that timer fires. The live recovery's snapshot used to
    serialise this process's cached table verbatim, writing the discharged
    handoff back to disk — a later restart then restored work that already
    ran.
    """

    def test_the_recovery_drops_a_handoff_a_peer_discharged(self, clean_registry):
        store = clean_registry
        early_sid = "sess-r19-recover-early"
        late_sid = "sess-r19-recover-late"
        early_token = "1" * 32
        late_token = "2" * 32
        _arm_and_consume(store, early_sid, "att-E", early_token)
        _arm_and_consume(store, late_sid, "att-L", late_token)

        # The restart inside BOTH ownership windows caches both handoffs and
        # schedules their recoveries; nothing is adopted yet.
        assert store.restore_goal_continuations() == 0
        assert (early_sid, "att-E") in store._CONTINUATION_HANDOFF_TOKENS
        assert (late_sid, "att-L") in store._CONTINUATION_HANDOFF_TOKENS
        assert (early_sid, "att-E") in store._DEFERRED_HANDOFF_RECOVERY
        assert (late_sid, "att-L") in store._DEFERRED_HANDOFF_RECOVERY

        def _peer_discharges_the_later_one(payload):
            handoffs = payload.get("continuation_handoffs") or {}
            late_key = f"{late_sid}\u0000att-L"
            assert late_key in handoffs, (
                "the schedule's peer mutation has no handoff to discharge"
            )
            # Another process launched the later continuation: its handoff was
            # discharged durably.
            handoffs.pop(late_key)
            # The earlier continuation's owner never came back: its window has
            # ended, so the scheduled recovery is due.
            early_key = f"{early_sid}\u0000att-E"
            handoffs[early_key]["_handoff_minted_at"] = time.time() - (_TTL + 60)

        _rewrite_registry_payload(store, _peer_discharges_the_later_one)
        assert (late_sid, "att-L") not in store._load_file_raw()[2]
        # The earlier window has ended on THIS process's clock too, so the
        # deferred timer is due (the registration was taken when the handoff
        # was still inside its window).
        with store._LOCK:
            store._DEFERRED_HANDOFF_RECOVERY[(early_sid, "att-E")] = time.time() - 1.0

        # The earlier timer fires: the recovery adopts the earlier handoff and
        # writes its snapshot. The LATER handoff was discharged by a peer and
        # must not be written back.
        adopted = store.recover_deferred_goal_continuation_handoffs(
            now=time.time() + 60
        )
        assert adopted == 1, (
            "the due handoff was not adopted: the schedule cannot exercise "
            "the recovery snapshot"
        )

        on_disk = store._load_file_raw()[2]
        assert (early_sid, "att-E") not in on_disk, (
            "the adopted handoff was not converted into a record"
        )
        assert (late_sid, "att-L") not in on_disk, (
            "the live recovery wrote a handoff another process had already "
            "discharged back to disk: a later restart restores work that "
            "already ran (finding 1)"
        )
        assert (late_sid, "att-L") not in store._CONTINUATION_HANDOFF_TOKENS, (
            "the stale cached handoff survived the recovery in memory"
        )

        # The restart the defect would have fooled: clear every in-memory
        # table, keep the file, and restore from disk only.
        _simulate_restart(store)
        store.restore_goal_continuations()

        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert early_sid in PENDING_GOAL_CONTINUATION_RECORDS, (
            "the recovered continuation is not claimable after the restart"
        )
        assert late_sid not in PENDING_GOAL_CONTINUATION_RECORDS, (
            "a later restart restored the continuation a peer had already "
            "launched: completed work runs twice (finding 1)"
        )
        assert late_sid not in store._load_file_raw()[0], (
            "the discharged continuation came back as a durable record"
        )

    def test_a_live_restore_still_preserves_an_unrelated_live_handoff(
        self, clean_registry
    ):
        """Control: the round-16 CORE preservation must survive the reconcile.

        A live peer's fresh handoff is ON disk, so the reconciliation keeps it
        even while the same restore adopts an unrelated record — the snapshot
        cannot erase the peer's crash-recovery evidence.
        """
        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )

        store = clean_registry
        live_sid = "sess-r19-live-peer"
        record_sid = "sess-r19-plain-record"
        _arm_and_consume(store, live_sid, "att-L", "3" * 32)
        # The plain record was armed by another process while this one was up.
        store.arm_pending_goal_continuation(
            record_sid, PROMPT, continuation_id="4" * 32
        )
        with store._LOCK:
            # Model "this process never saw it": it is on disk, not in memory.
            PENDING_GOAL_CONTINUATION_RECORDS.pop(record_sid, None)
            PENDING_GOAL_CONTINUATION.discard(record_sid)

        assert store.restore_goal_continuations() == 1, (
            "the unrelated plain record was not restored: the control cannot "
            "exercise the snapshot"
        )
        assert record_sid in PENDING_GOAL_CONTINUATION_RECORDS
        assert (live_sid, "att-L") in store._load_file_raw()[2], (
            "the restore erased a LIVE peer's handoff from disk: its crash "
            "recovery evidence is gone (round-16 CORE preservation)"
        )
        assert (live_sid, "att-L") in store._CONTINUATION_HANDOFF_TOKENS


# ── finding 2: an idle wrapped send must not spend the resume token ────────

NODE = shutil.which("node")

_EXTRA_SCENARIOS = r"""  } else if(scenario === 'wrapped-idle-continue'){
    // #7862 re-gate round 19 (P1, "wrapped sends spend resume tokens"): the
    // early resume claim must NOT run on an idle send. The ``/use`` wrapper
    // changes the final posted text after the claim point, and the server
    // admits any request carrying the token -- spending the pending
    // continuation on a turn that was not the literal ``continue`` the
    // resume asked for.
    _setResumeGoalContinuationToken('sid1', TOKEN);
    global.window._defaultMessageMode = 'steer';
    global.S = freshS();
    _currentMsgEl.value = 'continue';
    _forcedSkillDirectivePending = {
      sessionId:'sid1',
      promise: Promise.resolve({
        directive:"[USER OVERRIDE] You MUST follow the skill 'review'",
        name:'review',
        content:'use the review checklist',
      }),
    };
    await send();
    check(calls.length === 1, 'the wrapped send did not reach /api/chat/start: ' + JSON.stringify(calls));
    const wrapped = calls[0] && calls[0].body;
    check(Boolean(wrapped), 'the wrapped send posted no body');
    check(wrapped && /FORCED SKILL CONTEXT/.test(wrapped.message), 'the schedule did not wrap the posted text: ' + JSON.stringify(wrapped && wrapped.message));
    check(wrapped && wrapped.goal_continuation_id === '', 'an IDLE wrapped send posted the resume token: the pending continuation is spent on an ordinary turn, got ' + JSON.stringify(wrapped && wrapped.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === TOKEN, 'an IDLE wrapped send consumed the one-shot resume token');
  } else if(scenario === 'wrapped-busy-continue'){
    // Control: the busy routing the early claim exists for still carries the
    // identity into the queue, where the wrapper never rewrites the entry.
    _setResumeGoalContinuationToken('sid1', TOKEN);
    global.window._defaultMessageMode = 'steer';
    global.S = freshS('continue', [], true);
    _currentMsgEl.value = 'continue';
    _forcedSkillDirectivePending = {
      sessionId:'sid1',
      promise: Promise.resolve({
        directive:"[USER OVERRIDE] You must follow the skill 'review'",
        name:'review',
        content:'use the review checklist',
      }),
    };
    await send();
    check(steers.length === 0, 'the busy wrapped continue was steered: ' + JSON.stringify(steers));
    check(queued.length === 1, 'the busy wrapped continue was not queued: ' + JSON.stringify(queued));
    check(queued[0] && queued[0].entry.goal_continuation_id === TOKEN, 'the busy wrapped continue lost the resume identity: ' + JSON.stringify(queued[0] && queued[0].entry));
    check(_peekResumeGoalContinuationToken('sid1') === '', 'the busy wrapped continue did not consume the token');
  } else {
    throw new Error('unknown scenario ' + scenario);"""


def _driver_source() -> str:
    """The round-18 driver with the round-19 scenarios injected."""
    import tests.test_issue7862_round18_findings as r18

    tail = "  } else {\n    throw new Error('unknown scenario ' + scenario);"
    assert tail in r18._DRIVER_SRC, (
        "the round-18 driver scenario tail was not found: the round-19 "
        "scenarios would silently never run"
    )
    return r18._DRIVER_SRC.replace(tail, _EXTRA_SCENARIOS)


def _run_js_scenario(scenario: str) -> dict:
    import tempfile

    assert NODE, "node not on PATH"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(_driver_source())
        driver_path = fh.name
    try:
        env = os.environ.copy()
        # node SIGABRTs in this environment unless the channel fd is stripped.
        env.pop("NODE_CHANNEL_FD", None)
        env.pop("NODE_CHANNEL_SERIALIZATION_MODE", None)
        proc = subprocess.run(
            [
                NODE,
                driver_path,
                str(REPO_ROOT / "static" / "messages.js"),
                str(REPO_ROOT / "static" / "ui.js"),
                scenario,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        for line in (proc.stdout or "").splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        raise AssertionError(
            f"node driver produced no RESULT line\n{(proc.stdout or '')[:1000]}"
            f"\n{(proc.stderr or '')[:2000]}"
        )
    finally:
        try:
            os.unlink(driver_path)
        except OSError:
            pass


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("scenario", ["wrapped-idle-continue", "wrapped-busy-continue"])
def test_resume_claim_after_a_skill_wrapper(scenario):
    result = _run_js_scenario(scenario)
    assert result.get("ok"), (
        f"scenario {scenario!r} failed:\n- "
        + "\n- ".join(result.get("failures") or [])
    )
