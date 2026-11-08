"""#7862 re-gate round 18: the five P1 findings on head ``06ca6f9558``.

Greptile's CHANGES_REQUESTED on the current head confirmed round 17 GREEN and
blocked release on five P1s. Every one of them is a composition the earlier
rounds did not model:

1. **Restart leaves continuation blocked** (``goal_continuation_store.py:2409``)
   — a restart inside the 15-minute ownership window keeps the interrupted
   attempt's handoff but creates no pending record. Startup is the only
   production caller of ``restore_goal_continuations()`` and neither requests
   nor the sweep adopt it later, so the continuation stays unusable until
   ANOTHER process restart. Fix: schedule the recovery for the moment the
   ownership window ends.

2. **Late rollback repeats completed work** (``:1967``) — the guard only
   detects a successor while its handoff still exists. A pauses, B adopts and
   consumes, B launches and removes its own handoff, then A's rejected-start
   rollback finds no pending record, no foreign handoff and no retirement
   stamp, and restores the continuation B already ran. Fix: keep DURABLE
   evidence that B spent this generation and check it before accepting A's
   receipt.

3. **Expired receipt permits duplicate worker** (``:827``) — after A waits
   past the ownership window B adopts the handoff; an unrelated consume in
   A's process merges that handoff away and sweeps A's old receipt; the
   missing-key fast path in ``record_goal_continuation_stream()`` then answers
   True as though A never consumed anything and the ordinary launch path
   starts a SECOND worker for the same continuation. Fix: pass the route's
   ``consumed_goal_continuation`` state into the check instead of relying on a
   receipt that can expire.

4. **Older tabs cannot send** (``:1814``) — the consume clears the marker and
   the durable record but leaves ``PENDING_GOAL_CONTINUATION_PROMPTS``
   untouched, so an older tab that omits ``goal_continuation_id`` still gets
   the stale token from the legacy resolver and its next ordinary message hits
   the new no-marker refusal. Fix: remove the prompt/token mirror with the
   consumed record, and restore BOTH on rollback.

5. **Busy resume goes to steer** (``static/messages.js:2276``) — typing
   ``continue`` after ``/goal resume`` while a stream is active enters the
   default steer branch before the token claim; that branch checks only
   ``_goalContinuationId``, so the resume follow-up is steered instead of
   queued to the identified continuation. Fix: resolve the resume identity
   BEFORE choosing steer versus queue.

Schedules 1-4 drive the REAL store in one interpreter with the durable
registry file (the round-13/14/15 in-process shape); schedule 3's
revoked-admission composition additionally runs REAL independent processes
(the round-16/17 worker harness), because the receipt sweep and the handoff
reconciliation are per-process. Schedule 5 drives the REAL ``send()`` sliced
whole from ``static/messages.js`` in node with the REAL token helpers from
``static/ui.js`` (the round-13 method).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"

REPO_ROOT = Path(__file__).resolve().parents[1]

from api.goal_continuation_store import _ROLLBACK_RECEIPT_TTL_SECONDS as _TTL  # noqa: E402

# ── fixtures / helpers (round-13/14/15 shape) ─────────────────────────────


@pytest.fixture
def clean_registry():
    """Isolate the registry between cases (round-15 shape)."""
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
            store._DEFERRED_HANDOFF_RECOVERY.clear()
            store._SPENT_GENERATIONS.clear()
        _cancel_deferred_timer(store)
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

    _reset()
    yield store
    _reset()


def _cancel_deferred_timer(store) -> None:
    """Drop a recovery timer a schedule left behind, so no test leaks a thread."""
    timer = getattr(store, "_DEFERRED_RECOVERY_TIMER", None)
    store._DEFERRED_RECOVERY_TIMER = None
    if timer is not None and hasattr(timer, "cancel"):
        try:
            timer.cancel()
        except Exception:
            pass


def _age_durable_handoff(store, sid: str, seconds: float = _TTL + 60) -> None:
    """Age the session's durable handoff clock by *seconds*.

    The round-15 adoption-conversion helper shape: a crashed owner stops
    reclaiming, so the liveness clock on the admission is older than the
    reclaim window and a cold restore may legitimately take it over.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        for key in [k for k in store._CONTINUATION_HANDOFF_TOKENS if k[0] == sid]:
            store._CONTINUATION_HANDOFF_TOKENS[key]["_handoff_minted_at"] = (
                store.time.time() - seconds
            )
        store._write_registry_unlocked(
            PENDING_GOAL_CONTINUATION_RECORDS, context="aging"
        )


def _arm_and_consume(store, sid: str, attempt: str, token: str) -> str:
    assert store.arm_pending_goal_continuation(
        sid, PROMPT, continuation_id=token
    ), "the intent could not be armed"
    outcome = store.consume_pending_goal_continuation(sid, PROMPT, token, attempt)
    assert outcome == store.CONSUME_COMMITTED, (
        f"the admission did not commit: {outcome}"
    )
    return token


# ── finding 1: a handoff skipped at startup is recovered when its window ends


class TestASkippedHandoffIsRecoveredAfterItsWindow:
    """A restart inside the ownership window keeps the interrupted attempt's
    handoff but creates no pending record. The continuation must not stay
    unusable until another process restart: the recovery is scheduled for the
    moment the window ends."""

    def test_the_startup_skip_schedules_a_recovery_for_the_window_end(
        self, clean_registry, monkeypatch
    ):
        store = clean_registry
        sid = "sess-r18-deferred-schedule"
        token = "a" * 32
        _arm_and_consume(store, sid, "att-A", token)

        created: dict = {}

        class _RecordingTimer:
            def __init__(self, delay, function):
                created["delay"] = float(delay)
                created["function"] = function

            def start(self):
                created["started"] = True

            def is_alive(self):
                return True

            def cancel(self):
                created["cancelled"] = True

        # A restart inside the ownership window: the handoff is live, so the
        # restore leaves it alone (round-15 exclusive ownership).
        monkeypatch.setattr(store.threading, "Timer", _RecordingTimer)
        assert store.restore_goal_continuations() == 0, (
            "the restore adopted a LIVE owner's admission: the schedule cannot "
            "exercise the deferred recovery"
        )
        assert sid not in store._load_file_raw()[0], (
            "the startup restore created a pending record for a live admission"
        )
        assert created.get("started") is True, (
            "the skipped handoff was left with NO scheduled recovery: the "
            "continuation stays unusable until another process restart "
            "(finding 1)"
        )
        assert 0 < created["delay"] <= (
            _TTL + store._DEFERRED_RECOVERY_MARGIN_SECONDS
        ), (
            f"the recovery was not scheduled for the ownership window: "
            f"{created.get('delay')}"
        )
        assert (sid, "att-A") in store._DEFERRED_HANDOFF_RECOVERY, (
            "the skipped handoff was not registered for deferred recovery"
        )
        assert created["function"] is store._run_deferred_handoff_recovery

    def test_the_deferred_recovery_adopts_the_skipped_handoff(self, clean_registry):
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store = clean_registry
        sid = "sess-r18-deferred-adopt"
        token = "b" * 32
        _arm_and_consume(store, sid, "att-A", token)

        # The restart inside the window skips the admission.
        assert store.restore_goal_continuations() == 0
        assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
        assert (sid, "att-A") in store._DEFERRED_HANDOFF_RECOVERY

        # The window ends and the owner never came back.
        _age_durable_handoff(store, sid)
        adopted = store.recover_deferred_goal_continuation_handoffs(
            now=store.time.time() + _TTL + 60
        )
        assert adopted == 1, (
            "the deferred recovery did not adopt the handoff whose ownership "
            "window had ended: the continuation stays unusable until another "
            "process restart (finding 1)"
        )
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS, (
            "the deferred recovery left no claimable intent behind"
        )
        assert (sid, "att-A") not in store._DEFERRED_HANDOFF_RECOVERY

        # The recovered intent is USABLE: the retry consumes it instead of
        # running as an ordinary turn.
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, token, "att-R")
            == store.CONSUME_COMMITTED
        ), "the recovered continuation could not be consumed by its retry"

    def test_a_live_owners_admission_is_not_recovered_early(self, clean_registry):
        """Control: before the window ends the admission stays untouched."""
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store = clean_registry
        sid = "sess-r18-deferred-live"
        _arm_and_consume(store, sid, "att-A", "c" * 32)
        assert store.restore_goal_continuations() == 0
        assert store.recover_deferred_goal_continuation_handoffs() == 0, (
            "the recovery took over a LIVE owner's admission"
        )
        assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
        assert (sid, "att-A") in store._load_file_raw()[2], (
            "the live owner's admission evidence was erased"
        )

    def test_the_scheduled_timer_recovers_the_skipped_handoff(
        self, clean_registry, monkeypatch
    ):
        """End-to-end: the SCHEDULED recovery (not a manual call) adopts it.

        The admission's clock is aged so its window ends in two seconds; the
        restore skips it (still inside the window) and arms the real timer,
        which then adopts it without anybody calling the recovery by hand.
        """
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store = clean_registry
        sid = "sess-r18-deferred-timer"
        token = "a" * 31 + "b"
        _arm_and_consume(store, sid, "att-A", token)
        # Two seconds of ownership left, then a short margin.
        _age_durable_handoff(store, sid, seconds=_TTL - 2)
        monkeypatch.setattr(store, "_DEFERRED_RECOVERY_MARGIN_SECONDS", 0.05)
        assert store.restore_goal_continuations() == 0
        assert (sid, "att-A") in store._DEFERRED_HANDOFF_RECOVERY

        deadline = time.time() + 10
        while time.time() < deadline and sid not in PENDING_GOAL_CONTINUATION_RECORDS:
            time.sleep(0.02)
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS, (
            "the SCHEDULED recovery never adopted the handoff whose window "
            "ended: the continuation stays unusable until another process "
            "restart (finding 1)"
        )
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, token, "att-R")
            == store.CONSUME_COMMITTED
        )


# ── finding 2: a late rollback must not repeat a successor's completed work
#
# Out-of-process: the rollback receipt lives in the LOSING process's memory
# while the successor spends the intent in ANOTHER one, which is exactly what
# a single interpreter cannot model (the round-16/17 worker harness).


class TestALateRollbackCannotRepeatASuccessorsCompletedWork:
    """A pauses, B adopts and consumes the intent, B launches and removes its
    own handoff, and THEN A's rejected-start rollback lands. The pending-record
    table is empty, the successor's handoff is discharged and there is no
    retirement stamp, so every in-memory guard passes — the rollback must still
    refuse, because B durably spent this generation."""

    def test_a_rollback_is_refused_after_a_successor_spent_the_generation(
        self, workers
    ):
        sid = "sess-r18-late-rollback"
        late = _TTL + 120

        owner = workers("owner")
        peer = None
        try:
            admitted = owner.call(
                "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed", (
                "the real admission did not commit: the schedule cannot start"
            )

            # The owner never renews: B takes the admission over.
            owner.call("set_clock", offset=late)
            peer = workers("peer", clock_offset=late)
            assert peer.call("restore")["restored"] == 1, (
                "an abandoned admission was not recovered: the takeover half "
                "of the schedule cannot start"
            )

            # B spends the restored intent as its OWN attempt and launches.
            consumed = peer.call(
                "consume_restored",
                sid=sid,
                prompt=PROMPT,
                continuation_id=admitted["continuation_id"],
                attempt="att-B",
            )
            assert consumed["outcome"] == "committed", (
                "the successor did not consume the restored intent: the "
                "schedule cannot exercise the discharged-successor shape"
            )
            assert peer.call("discharge", sid=sid, attempt="att-B")["ok"] is True
            dump = peer.call("dump")
            assert sid not in dump["records"], (
                "the successor's consume left the record claimable"
            )
            assert f"{sid}\x00att-B" not in dump["handoffs"], (
                "the successor's admission handoff is still claimable: the "
                "schedule cannot exercise the discharged-successor shape"
            )

            # A's rejected start rolls back LATE. Every in-memory guard
            # passes: no live record, no foreign handoff, no tombstone.
            rolled = owner.call("rollback", sid=sid, attempt="att-A")
            assert rolled["accepted"] is False, (
                "the late rollback restored a continuation a successor "
                "attempt had already consumed AND launched: the goal loop now "
                "runs the same turn twice (finding 2)"
            )
            dump = peer.call("dump")
            assert sid not in dump["records"], (
                "the refused rollback still recreated the pending intent"
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()

    def test_the_own_attempts_rollback_still_restores(self, clean_registry):
        """Control: the spend evidence is per attempt, so the attempt that
        minted it can still roll its own rejected start back."""
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store = clean_registry
        sid = "sess-r18-own-rollback"
        token = "e" * 32
        _arm_and_consume(store, sid, "att-A", token)
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-A")
        assert receipt is not None
        assert store.restore_pending_goal_continuation(sid, receipt) is True, (
            "the spend evidence over-refused the attempt's OWN rollback"
        )
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, token, "att-A2")
            == store.CONSUME_COMMITTED
        ), "the restored intent is no longer consumable"


# ── finding 3: the route's consumed state, not an expiring receipt ─────────
#
# Out-of-process: the receipt sweep and the handoff reconciliation are
# per-process, so the revoked-admission composition needs two LIVE processes
# with their own module-level registry memory (the round-16/17 harness).

_EXTRA_COMMANDS_R18 = r'''            elif cmd == "bind":
                # #7862 re-gate round 18 (P1, "expired receipt permits
                # duplicate worker"): the route's OWN ``consumed`` provenance
                # is threaded into the stream binding, so an admission whose
                # receipt was swept is still refused. The TypeError fallback
                # keeps this worker runnable against a head that predates the
                # parameter -- that is the RED shape of the schedule.
                try:
                    _ok = bool(
                        store.record_goal_continuation_stream(
                            args["sid"],
                            args["attempt"],
                            args["stream"],
                            consumed=bool(args.get("consumed")),
                        )
                    )
                except TypeError:
                    _ok = bool(
                        store.record_goal_continuation_stream(
                            args["sid"],
                            args["attempt"],
                            args["stream"],
                        )
                    )
                _reply(ok=_ok, consumed=bool(args.get("consumed")))
'''

_BIND_BLOCK = '''            elif cmd == "bind":
                _reply(
                    ok=bool(
                        store.record_goal_continuation_stream(
                            args["sid"], args["attempt"], args["stream"]
                        )
                    )
                )
'''

from tests.test_issue7862_round17_findings import (  # noqa: E402
    _WORKER_SOURCE_R17,
    _Worker as _Round17Worker,
)

_WORKER_SOURCE_R18 = _WORKER_SOURCE_R17.replace(_BIND_BLOCK, _EXTRA_COMMANDS_R18)
assert _WORKER_SOURCE_R18 != _WORKER_SOURCE_R17, (
    "the round-16/17 worker bind command was not found: the consumed "
    "provenance would silently never be passed"
)


class _Worker(_Round17Worker):
    def _worker_source(self) -> str:
        return _WORKER_SOURCE_R18


@pytest.fixture
def workers(request):
    tmp_path = request.getfixturevalue("tmp_path")
    opened: list[_Worker] = []

    def _open(name, clock_offset=0.0):
        worker = _Worker(tmp_path, name, clock_offset=clock_offset)
        opened.append(worker)
        return worker

    yield _open
    for worker in opened:
        worker.close()


class TestARevokedAdmissionWithASweptReceiptCannotBind:
    """A waits past the ownership window, B adopts the handoff, an unrelated
    consume in A's process merges that handoff away and sweeps A's old
    receipt. Both entries are then missing, so the missing-key fast path
    answered True and the ordinary launch path started a SECOND worker for the
    same continuation."""

    def test_the_bind_refuses_when_the_route_consumed_the_intent(self, workers):
        sid = "sess-r18-swept-receipt"
        late = _TTL + 120

        owner = workers("owner")
        peer = None
        try:
            admitted = owner.call(
                "arm_consume", sid=sid, prompt=PROMPT, attempt="att-A"
            )
            assert admitted["outcome"] == "committed", (
                "the real admission did not commit: the schedule cannot start"
            )

            # A waits past the ownership window; B adopts its handoff.
            owner.call("set_clock", offset=late)
            peer = workers("peer", clock_offset=late)
            assert peer.call("restore")["restored"] == 1, (
                "an abandoned admission was not recovered: the takeover half "
                "of the schedule cannot start"
            )
            dump = peer.call("dump")
            assert f"{sid}\x00att-A" not in dump["handoffs"], (
                "the takeover did not consume the durable handoff"
            )

            # An UNRELATED consume in A's process: its reconciliation merges
            # the revoked handoff away and its receipt sweep drops A's old
            # receipt (A stopped reporting itself live).
            assert (
                owner.call(
                    "arm_consume",
                    sid="sess-r18-unrelated",
                    prompt=PROMPT,
                    attempt="att-U",
                )["outcome"]
                == "committed"
            )
            dump = owner.call("dump")
            assert f"{sid}\x00att-A" not in dump["memory_handoffs"], (
                "the unrelated transaction did not merge the revoked handoff "
                "away: the schedule cannot exercise the missing-key fast path"
            )
            assert f"{sid}\x00att-A" not in dump["memory_receipts"], (
                "A's receipt was not swept: the schedule no longer models the "
                "expired-receipt composition the finding describes"
            )

            # The route consumed this intent for this attempt, so the bind
            # must refuse even though no receipt survives.
            reply = owner.call(
                "bind",
                sid=sid,
                attempt="att-A",
                stream="stream-A",
                consumed=True,
            )
            assert reply["ok"] is False, (
                "the missing-key fast path answered True for an admission "
                "this process consumed and then lost: the route launches "
                "after that Boolean, so a second goal worker runs for one "
                "intent (finding 3)"
            )
        finally:
            for worker in (peer, owner):
                if worker is not None:
                    worker.close()

    def test_a_never_consumed_attempt_still_binds_lock_free(self, workers):
        """Control: the provenance flag must not over-refuse."""
        owner = workers("owner")
        try:
            reply = owner.call(
                "bind",
                sid="sess-r18-ordinary",
                attempt="att-none",
                stream="s",
                consumed=False,
            )
            assert reply["ok"] is True, (
                "an attempt that consumed nothing lost its lock-free ordinary "
                "bind: the provenance check over-refuses"
            )
        finally:
            owner.close()

    def test_the_route_passes_its_consumed_state_into_the_bind(self):
        """The store check is only reachable through the route: both launch
        paths must thread the route's own consumed state in."""
        src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        assert (
            src.count("consumed=consumed_goal_continuation")
            + src.count("consumed=goal_continuation_admitted")
        ) >= 2, (
            "the chat-start route does not pass its consumed_goal_continuation "
            "state into record_goal_continuation_stream(): the expired-receipt "
            "hole is still open on the launch path (finding 3)"
        )


# ── finding 4: the prompt/token mirror dies with the consumed record ───────


class TestConsumingARecordRemovesItsPromptMirror:
    """The consume clears the marker and the durable record but leaves
    ``PENDING_GOAL_CONTINUATION_PROMPTS`` behind, so the legacy resolver still
    hands a stale token to an older tab — whose next ordinary message then hits
    the no-marker refusal and cannot be sent at all."""

    def test_the_consume_removes_the_prompt_and_token_mirror(self, clean_registry):
        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_PROMPTS,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )
        from api.goals import (
            peek_pending_goal_continuation_id,
            register_pending_goal_continuation,
        )

        store = clean_registry
        sid = "sess-r18-mirror-consume"
        cont_id = register_pending_goal_continuation(sid, PROMPT)
        assert cont_id
        assert PENDING_GOAL_CONTINUATION_PROMPTS[sid]["continuation_id"] == cont_id

        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-1")
            == store.CONSUME_COMMITTED
        )
        assert sid not in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid) is None
        assert sid not in PENDING_GOAL_CONTINUATION_PROMPTS, (
            "the consume left the prompt/token mirror behind: an older tab "
            "that omits goal_continuation_id still resolves this stale token "
            "and its next ordinary message is refused with no marker "
            "(finding 4)"
        )
        assert peek_pending_goal_continuation_id(sid) is None, (
            "the legacy resolver still hands out the consumed continuation's "
            "token"
        )

    def test_the_legacy_resolver_stops_after_the_consume(self, clean_registry):
        from api.goals import register_pending_goal_continuation
        from api.routes import _resolve_chat_start_goal_continuation_id

        store = clean_registry
        sid = "sess-r18-mirror-resolver"
        cont_id = register_pending_goal_continuation(sid, PROMPT)
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, cont_id, "att-1")
            == store.CONSUME_COMMITTED
        )
        resolved = _resolve_chat_start_goal_continuation_id(
            {"message": "an ordinary question"}, sid
        )
        assert resolved == "", (
            f"a pre-upgrade tab's key-omitting body resolved to the consumed "
            f"continuation's token ({resolved!r}): its next ordinary message "
            f"carries a token with no pending marker and cannot be sent "
            f"(finding 4)"
        )

    def test_the_rollback_restores_both_halves_of_the_mirror(self, clean_registry):
        from api.config import (
            PENDING_GOAL_CONTINUATION,
            PENDING_GOAL_CONTINUATION_PROMPTS,
            PENDING_GOAL_CONTINUATION_RECORDS,
        )

        store = clean_registry
        sid = "sess-r18-mirror-rollback"
        token = "f" * 32
        _arm_and_consume(store, sid, "att-1", token)
        assert sid not in PENDING_GOAL_CONTINUATION_PROMPTS

        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-1")
        assert receipt is not None
        assert store.restore_pending_goal_continuation(sid, receipt) is True
        assert sid in PENDING_GOAL_CONTINUATION
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        mirror = PENDING_GOAL_CONTINUATION_PROMPTS.get(sid)
        assert mirror is not None, (
            "the rollback restored the record but not the prompt/token "
            "mirror: the orphan sweep strips the marker and the legacy "
            "resolver loses the admission (finding 4)"
        )
        assert mirror["continuation_id"] == token
        assert mirror["prompt"] == PROMPT


# ── finding 5: a busy resume queues instead of steering ───────────────────
#
# Node driver: the REAL send() sliced whole from static/messages.js with the
# REAL token helpers from static/ui.js (the round-13 method).

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

NODE = shutil.which("node")

_DRIVER_SRC = r"""
const fs = require('fs');
const messagesSrc = fs.readFileSync(process.argv[2], 'utf8');
const uiSrc = fs.readFileSync(process.argv[3], 'utf8');
const scenario = process.argv[4] || '';
const failures = [];

function check(cond, msg){
  if(!cond) failures.push(msg);
}

// ---- comment/string/template-aware brace scanner (round-13 extractor) ----
function scanEnd(src, openBrace){
  let i = openBrace + 1, depth = 1, n = src.length, state = 'code', quote = '';
  while(i < n && depth > 0){
    const c = src[i], nxt = i + 1 < n ? src[i+1] : '';
    if(state === 'code'){
      if(c === '/' && nxt === '/'){ state = 'linecomment'; i += 2; continue; }
      if(c === '/' && nxt === '*'){ state = 'blockcomment'; i += 2; continue; }
      if(c === '"' || c === "'"){ state = 'string'; quote = c; i += 1; continue; }
      if(c === '`'){ state = 'template'; i += 1; continue; }
      if(c === '{') depth += 1;
      else if(c === '}'){ depth -= 1; if(depth === 0) return i; }
      i += 1;
    } else if(state === 'linecomment'){
      if(c === '\n') state = 'code';
      i += 1;
    } else if(state === 'blockcomment'){
      if(c === '*' && nxt === '/'){ state = 'code'; i += 2; continue; }
      i += 1;
    } else if(state === 'string'){
      if(c === '\\'){ i += 2; continue; }
      if(c === quote) state = 'code';
      i += 1;
    } else if(state === 'template'){
      if(c === '\\'){ i += 2; continue; }
      if(c === '`'){ state = 'code'; }
      i += 1;
    }
  }
  throw new Error('unbalanced braces in ' + src.slice(openBrace, openBrace + 40));
}
function extractTopLevel(src, name, prefix){
  const marker = prefix + ' ' + name + '(';
  const start = src.indexOf(marker);
  if(start < 0) throw new Error('missing ' + marker + ' in source');
  const brace = src.indexOf('{', start);
  if(brace < 0) throw new Error('missing brace for ' + marker);
  return src.slice(start, scanEnd(src, brace) + 1);
}

// ---- REAL token helpers from static/ui.js ----
const _RESUME_GOAL_CONTINUATION_TOKENS = {};
eval(extractTopLevel(uiSrc, '_setResumeGoalContinuationToken', 'function'));
eval(extractTopLevel(uiSrc, '_claimResumeGoalContinuationToken', 'function'));
eval(extractTopLevel(uiSrc, '_peekResumeGoalContinuationToken', 'function'));

// ---- REAL helpers + the WHOLE send() from static/messages.js ----
eval(extractTopLevel(messagesSrc, '_normalizeGoalContinuationId', 'function'));
eval(extractTopLevel(messagesSrc, '_isLiteralResumeContinue', 'function'));
const sendStart = messagesSrc.indexOf('async function send(){');
if(sendStart < 0) throw new Error('async function send(){ not found');
const sendText = messagesSrc.slice(sendStart, scanEnd(messagesSrc, messagesSrc.indexOf('{', sendStart)) + 1);
eval(sendText);

// ---- module-level slots send() assigns/reads ----
let _sendInProgress = false;
let _sendInProgressSid = null;
let _queueDrainSid = null;
let _forcedSkillDirectivePending = null;
let _approvalSessionId = null;
let _clarifySessionId = null;
const _pendingSelections = [];
const INFLIGHT = {};

// ---- recording stubs for the browser environment (round-13 shape) ----
let _currentMsgEl = null;
function $(id){ if(id === 'msg') return _currentMsgEl; return null; }
function autoResize(){}
function hideCmdDropdown(){}
function setComposerStatus(){}
function setBusy(){}
function renderMessages(){}
function clearLiveToolCards(){}
function ensureLiveWorklogShell(){}
function appendThinking(){}
function upsertActiveSessionForLocalTurn(){}
function updateSendBtn(){}
function renderSessionListFromCache(){}
function startApprovalPolling(){}
function startClarifyPolling(){}
function _fetchYoloState(){}
function applySessionTitleUpdate(){}
function saveInflightState(){}
function markInflight(){}
function attachLiveStream(){}
function stopApprovalPolling(){}
function stopClarifyPolling(){}
function hideApprovalCard(){}
function hideClarifyCard(){}
function removeThinking(){}
function clearInflightState(){}
function clearOptimisticSessionStreaming(){}
function renderSessionList(){}
function showToast(){}
function renderTray(){}
function _clearComposerDraft(){ return null; }
function _saveComposerDraftNow(){ return null; }
function _handleComposerDraftPostFailure(){}
function _setRestoredGoalContinuationDraft(){}
function _clearRestoredGoalContinuationDraft(){}
function _dismissHandoffHint(){}
function isCompressionUiRunning(){ return false; }
function _clearStaleBusyStateBeforeSend(){}
function shouldInterceptCompressionRecoveryContinuation(){ return false; }
function showCompressionRecoveryContinuationHint(){}
function parseCommand(){ return null; }
const COMMANDS = [];
function cancelStream(){ return Promise.resolve(true); }
function t(k){ return k; }
function _flushSelectionBlocksToComposer(){}
function _takeRestoredDraftGoalContinuationId(){ return ''; }
function _chatPayloadModelState(){ return {model:'test-model', model_provider:'test-provider'}; }
function _readPendingSessionModel(){ return null; }
function _clearPendingSessionModel(){}
function _clearComposerAfterQueuedSelectionSend(){}
function updateQueueBadge(){}
function _runOptionalPreStartUiStep(){}
function _runOptionalPostStartUiStep(){}
function loadSession(){ return Promise.resolve(); }
function _hydrateTodosFromSession(){}
function _syncProviderQuotaForActiveContext(){}
function _recoverCompressedSend(){ return Promise.resolve(false); }
function _writePersistedModelState(){}
function _applyModelToDropdown(){}
function syncTopbar(){}
function syncModelChip(){}
function showLiveRunStatus(){}
function newSession(){ return Promise.resolve(); }
function _composerTextWithPendingSelections(){ return _currentMsgEl ? (_currentMsgEl.value || '') : ''; }
async function uploadPendingFiles({files}){
  return (files || []).map(f => (typeof f === 'string' ? {name:f, path:'/p/'+f} : f));
}
global.window = {};
global.document = { querySelector: () => null };

const calls = [];
const steers = [];
let apiMode = 'resolve';
async function api(path, opts){
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.push({path, body});
  if(apiMode === 'reject-409') throw new Error('session already has an active stream');
  if(apiMode === 'reject') throw new Error('boom');
  return {stream_id:'stream-1', title:'T', pending_started_at: Date.now()/1000};
}
async function _trySteer(text, explicitSteer){
  steers.push({text, explicitSteer});
  return {accepted:true};
}
const queued = [];
function queueSessionMessage(sid, entry){ queued.push({sid, entry}); }
const restored = [];
function _restoreComposerDraftAfterFailedSend(draftText, filesSnapshot, sid, clearPromise, goalContinuationId){
  restored.push({draftText, sid, goalContinuationId});
}

function freshS(text, files, busy){
  return {
    session:{session_id:'sid1', workspace:'ws', model:'test-model', model_provider:'test-provider', profile:'default', title:'Untitled', active_stream_id:null, pending_started_at:null, read_only:false},
    messages:[], busy:!!busy, pendingFiles:files || [], activeProfile:'default', activeStreamId:busy?'stream-live':null, toolCalls:[], todos:[], todoStateMeta:null,
  };
}

async function run(){
  calls.length = 0; queued.length = 0; restored.length = 0; steers.length = 0;
  Object.keys(INFLIGHT).forEach(k => delete INFLIGHT[k]);
  _sendInProgress = false; _sendInProgressSid = null; _queueDrainSid = null; _forcedSkillDirectivePending = null;
  apiMode = 'resolve';
  global.window._defaultMessageMode = 'steer';
  global.S = freshS();
  _currentMsgEl = {value:'', dataset:{}};

  const TOKEN = 'tok1';
  if(scenario === 'unrelated'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'hello';
    await send();
    const posted = calls[0] && calls[0].body;
    check(Boolean(posted), 'unrelated text never reached /api/chat/start');
    check(posted && posted.goal_continuation_id === '', 'unrelated text posted the resume token: got ' + JSON.stringify(posted && posted.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === TOKEN, 'unrelated text claimed the one-shot resume token; the real continue can no longer be admitted');
  } else if(scenario === 'continue'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'continue';
    await send();
    const posted = calls[0] && calls[0].body;
    check(Boolean(posted), 'the literal continue never reached /api/chat/start');
    check(posted && posted.goal_continuation_id === TOKEN, 'the literal continue posted without the resume token: got ' + JSON.stringify(posted && posted.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === '', 'the resume token was not consumed one-shot after the continue send');
  } else if(scenario === 'continue-with-files'){
    _setResumeGoalContinuationToken('sid1', TOKEN);
    _currentMsgEl.value = 'continue';
    global.S.pendingFiles = ['f.txt'];
    await send();
    const posted = calls[0] && calls[0].body;
    check(Boolean(posted), 'the attachment send never reached /api/chat/start');
    check(posted && posted.goal_continuation_id === '', 'an attachment-annotated continue claimed the resume token: got ' + JSON.stringify(posted && posted.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === TOKEN, 'an attachment-annotated continue consumed the one-shot resume token');
  } else if(scenario === 'busy-resume-continue'){
    // #7862 re-gate round 18 (P1, "busy resume goes to steer"): the resume
    // follow-up typed while the stream is still active must be QUEUED with
    // its continuation identity, never steered.
    _setResumeGoalContinuationToken('sid1', TOKEN);
    global.S = freshS('continue', [], true);
    _currentMsgEl.value = 'continue';
    await send();
    check(steers.length === 0, 'the resume continue was STEERED into the live run: the identified continuation must not go to /api/chat/steer, got ' + JSON.stringify(steers));
    check(queued.length === 1, 'the resume continue was not queued: ' + JSON.stringify(queued));
    check(queued[0] && queued[0].sid === 'sid1', 'the resume continue was queued for the wrong session: ' + JSON.stringify(queued[0] && queued[0].sid));
    check(queued[0] && queued[0].entry.goal_continuation_id === TOKEN, 'the queued resume continue lost its continuation identity: got ' + JSON.stringify(queued[0] && queued[0].entry.goal_continuation_id));
    check(_peekResumeGoalContinuationToken('sid1') === '', 'the resume token was not consumed by the busy continue send');
  } else if(scenario === 'busy-unrelated-steers'){
    // Control: a genuine turn typed while busy still steers, and must not
    // claim the one-shot resume identity.
    _setResumeGoalContinuationToken('sid1', TOKEN);
    global.S = freshS('hello', [], true);
    _currentMsgEl.value = 'hello';
    await send();
    check(steers.length === 1, 'a genuine busy turn no longer steers: ' + JSON.stringify(steers));
    check(steers[0] && steers[0].text === 'hello', 'the steered text changed: ' + JSON.stringify(steers[0]));
    check(queued.length === 0, 'a genuine busy turn was queued instead of steered');
    check(_peekResumeGoalContinuationToken('sid1') === TOKEN, 'a genuine busy turn claimed the one-shot resume token');
  } else if(scenario === 'busy-queue-mode-resume-continue'){
    // The queue default (window._defaultMessageMode === 'queue') must keep
    // carrying the resume identity into the queue entry.
    _setResumeGoalContinuationToken('sid1', TOKEN);
    global.window._defaultMessageMode = 'queue';
    global.S = freshS('continue', [], true);
    _currentMsgEl.value = 'continue';
    await send();
    check(steers.length === 0, 'the queue-mode resume continue was steered: ' + JSON.stringify(steers));
    check(queued.length === 1 && queued[0].entry.goal_continuation_id === TOKEN, 'the queue-mode resume continue lost its identity: ' + JSON.stringify(queued[0] && queued[0].entry));
  } else {
    throw new Error('unknown scenario ' + scenario);
  }
  const result = {ok: failures.length === 0, failures};
  console.log('RESULT ' + JSON.stringify(result));
}

run().catch(err => {
  console.log('RESULT ' + JSON.stringify({ok:false, failures:['driver threw: ' + (err && err.stack ? err.stack : String(err))]}));
  process.exit(1);
});
"""

_JS_SCENARIOS = (
    "unrelated",
    "continue",
    "continue-with-files",
    "busy-resume-continue",
    "busy-unrelated-steers",
    "busy-queue-mode-resume-continue",
)


def _run_js_scenario(scenario: str) -> dict:
    import tempfile

    assert NODE, "node not on PATH"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(_DRIVER_SRC)
        driver_path = fh.name
    try:
        env = os.environ.copy()
        # node SIGABRTs in this environment unless the channel fd is stripped.
        env.pop("NODE_CHANNEL_FD", None)
        proc = subprocess.run(
            [NODE, driver_path, str(REPO_ROOT / "static" / "messages.js"),
             str(REPO_ROOT / "static" / "ui.js"), scenario],
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
@pytest.mark.parametrize("scenario", _JS_SCENARIOS)
def test_resume_claim_and_busy_routing(scenario):
    result = _run_js_scenario(scenario)
    assert result.get("ok"), (
        f"scenario {scenario!r} failed:\n- " + "\n- ".join(result.get("failures") or [])
    )
