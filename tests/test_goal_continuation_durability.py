"""#6885 slice 2a review round 2 (#7862): durable goal continuation intent.

The in-memory ``PENDING_GOAL_CONTINUATION`` marker set is lost on restart. The
durable registry (``api/goal_continuation_store.py``) keeps ONE locked record
per session (canonical prompt + generation + lifecycle metadata) and restores
it from a STARTUP-ONLY hook, so the four maintainer blockers are closed:

1. one owner lock + unique same-dir tmp before ``os.replace`` — overlapping
   writers end with the newest generation, never a torn/lost intermediate
2. durable payload carries the continuation prompt (not just session ids)
3. a restart with NO sessions directory still restores prompt AND marker
4. an online repair can never re-arm a consumed continuation

Explicit retirement covers consumed / cleared / deleted / expired intents,
and durability failures are OBSERVABLE in ``durability_diagnostics()``
instead of silently claiming durability.
"""

import json
import re
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clean_registry():
    """Isolate the registry between cases: in-memory mirror, file, diagnostics."""
    from api import goal_continuation_store as store
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._LAST_LOAD_ERROR = None
        store._LAST_WRITE_ERROR = None
        store._RETIRED_LOG.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._GENERATION = 0
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    for tmp in store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"):
        tmp.unlink(missing_ok=True)
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._RETIRED_GENERATIONS.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)


class TestStoreRoundtrip:
    def test_file_location_under_state_dir(self, clean_registry):
        from api.goal_continuation_store import _PENDING_GOAL_FILE
        from api.config import STATE_DIR
        assert _PENDING_GOAL_FILE == STATE_DIR / "pending_goal_continuations.json"

    def test_roundtrip_prompt_and_marker(self, clean_registry):
        """Arm persists the canonical prompt AND the marker; both reload."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        store.arm_pending_goal_continuation(
            "sess-a", "Please continue the standing goal.", reason="goal_continue"
        )
        assert "sess-a" in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS["sess-a"]["prompt"] == "Please continue the standing goal."
        assert PENDING_GOAL_CONTINUATION_RECORDS["sess-a"]["reason"] == "goal_continue"
        assert PENDING_GOAL_CONTINUATION_RECORDS["sess-a"]["generation"] > 0

        disk = store.load_pending_goal_continuations()
        assert disk["sess-a"]["prompt"] == "Please continue the standing goal."
        assert disk["sess-a"]["generation"] == PENDING_GOAL_CONTINUATION_RECORDS["sess-a"]["generation"]
        assert disk["sess-a"]["reason"] == "goal_continue"

    def test_arm_write_leaves_no_tmp_leftover(self, clean_registry):
        from api import goal_continuation_store as store
        store.arm_pending_goal_continuation("sess-a", "prompt-a")
        leftovers = list(store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"))
        assert leftovers == []
        assert "sess-a" in store.load_pending_goal_continuations()

    def test_load_v1_list_format_upgrades(self, clean_registry):
        """A v1 list-of-strings file upgrades to records with a blank prompt."""
        from api import goal_continuation_store as store
        store._PENDING_GOAL_FILE.write_text(
            json.dumps(["sess-old-1", "sess-old-2"]), encoding="utf-8"
        )
        records = store.load_pending_goal_continuations()
        assert set(records) == {"sess-old-1", "sess-old-2"}
        for sid in records:
            assert records[sid]["prompt"] == ""
            assert records[sid]["reason"] == "goal_continue"
        # A startup merge makes the v1 records live; they then retire normally.
        assert store.restore_goal_continuations() == 2
        store.retire_pending_goal_continuation("sess-old-1", reason="consumed")
        assert "sess-old-1" not in store.load_pending_goal_continuations()
        assert "sess-old-2" in store.load_pending_goal_continuations()

    def test_missing_and_corrupt_are_empty_and_observable(self, clean_registry):
        """Missing/corrupt reads empty AND the failure is observable."""
        from api import goal_continuation_store as store
        assert store.load_pending_goal_continuations() == {}
        assert store.durability_diagnostics()["last_load_error"] is None
        store._PENDING_GOAL_FILE.write_text("{not-json[[[", encoding="utf-8")
        assert store.load_pending_goal_continuations() == {}
        diag = store.durability_diagnostics()
        assert diag["last_load_error"] is not None
        assert "last_load_error" in diag


class TestRestoreConsumptionIsMatchGated:
    """#7862 review round 3: a restored marker is spent ONLY by its own continuation.

    The core blocker: the marker used to be retired by session id alone. That
    was safe only while it lived for the few seconds between ``goal_continue``
    firing and the browser's automatic send. #7862 makes it durable, so it can
    come back at startup long after that browser is gone -- and then the user's
    next message of ANY kind was swallowed as a goal continuation, retiring the
    marker and letting the goal machinery queue another automatic continuation
    on top of an unrelated turn.
    """

    PROMPT = "Continue the standing goal: finish the migration checklist."

    def _arm_then_simulate_restart(self, prompt=PROMPT, sid="sess-restore"):
        """Arm, then drop all in-memory state and re-restore from disk."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        store.arm_pending_goal_continuation(sid, prompt, reason="goal_continue")
        # Simulate the restart: process memory is gone, the file is all we have.
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
        assert store.restore_goal_continuations() == 1
        return sid

    def test_restore_then_unrelated_message_stays_pending(self, clean_registry):
        """REQUIRED: unrelated message must NOT be goal-related, marker survives."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = self._arm_then_simulate_restart()

        # The user's next message after the restart, unrelated to the goal.
        assert store.consume_pending_goal_continuation(sid, "thanks, also what time is it?")== store.CONSUME_NOT_MATCHING

        # Not consumed: the pending intent is still there for the real continuation.
        assert sid in PENDING_GOAL_CONTINUATION
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS
        assert store.load_pending_goal_continuations()[sid]["prompt"] == self.PROMPT

    def test_restore_then_matching_continuation_consumed_exactly_once(self, clean_registry):
        """REQUIRED: the real continuation is consumed, and only once."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = self._arm_then_simulate_restart()

        assert store.consume_pending_goal_continuation(sid, self.PROMPT)== store.CONSUME_COMMITTED
        assert sid not in PENDING_GOAL_CONTINUATION
        assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
        assert sid not in store.load_pending_goal_continuations()

        # Exactly once: a replay of the same text cannot consume a second time.
        assert store.consume_pending_goal_continuation(sid, self.PROMPT)== store.CONSUME_NOT_MATCHING

    def test_whitespace_tolerant_but_not_widened(self, clean_registry):
        """Cosmetic whitespace differences match; different text still does not."""
        from api import goal_continuation_store as store

        sid = self._arm_then_simulate_restart(prompt="Continue   the goal:\n  finish it.")

        assert store.consume_pending_goal_continuation(
            sid, "  Continue the goal:   finish it.  "
        ) == store.CONSUME_COMMITTED

        sid2 = self._arm_then_simulate_restart(prompt="Continue the goal.", sid="sess-restore-2")
        assert store.consume_pending_goal_continuation(
            sid2, "Continue the goal please, but differently."
        ) == store.CONSUME_NOT_MATCHING

    def test_promptless_record_is_never_consumed(self, clean_registry):
        """A v1-upgraded record carries no prompt, so it cannot be matched safely.

        Retiring it on an arbitrary message is the exact data-loss bug this gate
        exists to prevent; expiry bounds it instead.
        """
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        store._PENDING_GOAL_FILE.write_text(json.dumps(["sess-v1"]), encoding="utf-8")
        assert store.restore_goal_continuations() == 1

        assert store.consume_pending_goal_continuation("sess-v1", "anything at all")== store.CONSUME_NOT_MATCHING
        assert "sess-v1" in PENDING_GOAL_CONTINUATION
        assert "sess-v1" in PENDING_GOAL_CONTINUATION_RECORDS

    def test_matching_is_per_session_not_global(self, clean_registry):
        """One session's continuation text must not consume another session's intent."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        store.arm_pending_goal_continuation("sess-1", "Do the first thing.", reason="goal_continue")
        store.arm_pending_goal_continuation("sess-2", "Do the second thing.", reason="goal_continue")

        # sess-2 sends its own continuation -> only sess-2 is consumed.
        assert store.consume_pending_goal_continuation("sess-2", "Do the second thing.")== store.CONSUME_COMMITTED
        assert "sess-2" not in PENDING_GOAL_CONTINUATION
        assert "sess-1" in PENDING_GOAL_CONTINUATION
        assert "sess-1" in PENDING_GOAL_CONTINUATION_RECORDS

        # sess-1's own continuation still works afterwards.
        assert store.consume_pending_goal_continuation("sess-1", "Do the first thing.")== store.CONSUME_COMMITTED

    def test_unknown_session_and_empty_ids_are_noops(self, clean_registry):
        from api import goal_continuation_store as store
        assert store.consume_pending_goal_continuation("sess-nope", "hello")== store.CONSUME_NOT_MATCHING
        assert store.consume_pending_goal_continuation("", "hello")== store.CONSUME_NOT_MATCHING


class TestConcurrentWriters:
    def test_concurrent_arms_final_file_equals_newest_generation(self, clean_registry):
        """Barrier-controlled overlapping arms: NO lost update, no tmp leftovers."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION

        n_threads = 8
        barrier = threading.Barrier(n_threads)
        errors = []

        def _arm(sid):
            try:
                barrier.wait(timeout=10)
                store.arm_pending_goal_continuation(
                    sid, f"prompt-{sid}", reason="goal_continue"
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [
            threading.Thread(target=_arm, args=(f"sess-{i}",))
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []

        disk = store.load_pending_goal_continuations()
        assert set(disk) == {f"sess-{i}" for i in range(n_threads)}
        for sid, rec in disk.items():
            assert rec["prompt"] == f"prompt-{sid}"
            assert rec["generation"] > 0
        assert set(PENDING_GOAL_CONTINUATION) == set(disk)

        leftovers = list(store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"))
        assert leftovers == []
        # The final file is the newest in-memory generation — never a torn or
        # lost intermediate.
        raw = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
        assert raw["generation"] == store._GENERATION
        assert set(raw["records"]) == set(disk)

    def test_concurrent_arm_and_retire_overlap(self, clean_registry):
        """Barrier forces an arm and a retire to overlap; newest state wins."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION

        store.arm_pending_goal_continuation("sess-victim", "old-prompt")
        barrier = threading.Barrier(2)
        errors = []

        def _retire():
            try:
                barrier.wait(timeout=10)
                store.retire_pending_goal_continuation("sess-victim", reason="consumed")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def _arm():
            try:
                barrier.wait(timeout=10)
                store.arm_pending_goal_continuation(
                    "sess-new", "new-prompt", reason="goal_continue"
                )
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        t1 = threading.Thread(target=_retire)
        t2 = threading.Thread(target=_arm)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert errors == []

        disk = store.load_pending_goal_continuations()
        assert "sess-victim" not in disk
        assert "sess-new" in disk
        assert disk["sess-new"]["prompt"] == "new-prompt"
        assert set(PENDING_GOAL_CONTINUATION) == set(disk)
        raw = json.loads(store._PENDING_GOAL_FILE.read_text(encoding="utf-8"))
        assert raw["generation"] == store._GENERATION
        assert set(raw["records"]) == set(disk)


class TestRetirement:
    def test_retire_removes_the_exact_session_record(self, clean_registry):
        """Retiring one session leaves the other's record byte-identical."""
        from api import goal_continuation_store as store
        store.arm_pending_goal_continuation("sess-a", "prompt-a")
        store.arm_pending_goal_continuation("sess-b", "prompt-b")
        rec_b_before = store.load_pending_goal_continuations()["sess-b"]

        store.retire_pending_goal_continuation("sess-a", reason="consumed")
        disk = store.load_pending_goal_continuations()
        assert "sess-a" not in disk
        assert disk["sess-b"] == rec_b_before
        assert disk["sess-b"]["prompt"] == "prompt-b"

    def test_expired_sweep_retires_stale_intent(self, clean_registry):
        """Stale disk intent is retired by the age sweep and logged."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        store.arm_pending_goal_continuation("sess-fresh", "fresh")
        store.arm_pending_goal_continuation("sess-stale", "stale")
        PENDING_GOAL_CONTINUATION_RECORDS["sess-stale"]["created_at"] = (
            time.time() - 25 * 3600
        )
        swept = store.sweep_expired_goal_continuations(max_age_seconds=24 * 3600)
        assert swept == 1
        disk = store.load_pending_goal_continuations()
        assert "sess-stale" not in disk
        assert "sess-fresh" in disk
        retired = store.durability_diagnostics()["retired"]
        assert any(
            r["session_id"] == "sess-stale" and r["reason"] == "expired"
            for r in retired
        )

    def test_control_unrelated_sessions_untouched(self, clean_registry):
        """Arm/retire of one session never touches unrelated sessions' markers."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION

        # A session with NO durable record stays an ordinary turn.
        assert "sess-plain" not in PENDING_GOAL_CONTINUATION
        store.arm_pending_goal_continuation("sess-a", "prompt-a")
        store.retire_pending_goal_continuation("sess-a", reason="consumed")
        assert "sess-plain" not in PENDING_GOAL_CONTINUATION
        assert "sess-plain" not in store.load_pending_goal_continuations()
        assert store.load_pending_goal_continuations() == {}
        # A fresh arm of a different session leaves no ghost of the retired one.
        store.arm_pending_goal_continuation("sess-b", "prompt-b")
        assert set(store.load_pending_goal_continuations()) == {"sess-b"}


class TestStartupAndRepair:
    def test_startup_restore_without_sessions_dir(self, clean_registry):
        """Blocker #3: restore runs even when the sessions dir does not exist."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS
        from api.session_recovery import restore_goal_continuations_on_startup

        store.arm_pending_goal_continuation("sess-a", "durable prompt")
        # Simulate a process restart: in-memory state is gone, disk survives.
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()

        missing_dir = store._PENDING_GOAL_FILE.parent / "no-such-sessions-dir"
        assert not missing_dir.exists()
        report = restore_goal_continuations_on_startup(missing_dir)
        assert report["sessions_dir_exists"] is False
        assert report["restored"] == 1
        assert "sess-a" in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS["sess-a"]["prompt"] == "durable prompt"
        # Dispatch exactly once: a second restore is a no-op.
        assert restore_goal_continuations_on_startup(missing_dir)["restored"] == 0

    def test_online_repair_cannot_re_arm(self, clean_registry, tmp_path):
        """Blocker #4: the reusable repair entrypoint never restores intent.

        This asserts the OUTCOME the reviewer asked for — a consumed
        continuation is not re-armed by an online repair — by proving the
        repair entrypoint never merges disk state back into the live marker
        set. (A source-attribute monkeypatch would be vacuous: the repair path
        never resolved that attribute in the first place.)
        """
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS
        import api.session_recovery as sr

        # A durable record that was already consumed and retired: the disk is
        # gone, so repair has nothing to merge. But even if a STALE disk record
        # survived, repair must not resurrect it into the live set.
        store.arm_pending_goal_continuation("sess-consumed", "carry on")
        store.retire_pending_goal_continuation("sess-consumed", reason="consumed")
        assert "sess-consumed" not in PENDING_GOAL_CONTINUATION
        assert store.load_pending_goal_continuations() == {}

        # Simulate the dangerous case: a stale on-disk record exists that was
        # never retired (e.g. killed between the SSE event and consumption).
        stale = {
            "version": 2,
            "generation": 99,
            "records": {
                "sess-stale-disk": {
                    "prompt": "carry on",
                    "generation": 99,
                    "created_at": time.time(),
                    "reason": "goal_continue",
                }
            },
        }
        store._PENDING_GOAL_FILE.write_text(
            json.dumps(stale), encoding="utf-8"
        )
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()

        missing_dir = tmp_path / "sessions"
        assert not missing_dir.exists()
        sr.repair_safe_session_recovery(missing_dir)

        # The repair ran WITHOUT arming anything from disk.
        assert "sess-consumed" not in PENDING_GOAL_CONTINUATION
        assert "sess-stale-disk" not in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS == {}
        # The stale disk record is still on disk (untouched by repair) and can
        # only be picked up by the startup-only hook.
        assert "sess-stale-disk" in store.load_pending_goal_continuations()

    def test_recovery_scan_has_no_goal_restore_hook(self):
        """Source guard: the session scan (repair-reachable) carries no restore."""
        src = Path("api/session_recovery.py").read_text(encoding="utf-8")
        assert "def restore_goal_continuations_on_startup" in src
        scan_start = src.index("def recover_all_sessions_on_startup")
        scan_end = src.index("def _main()")
        scan_body = src[scan_start:scan_end]
        assert "goal_continuation" not in scan_body
        assert "restore_at_startup" not in scan_body


class TestObservableFailures:
    def test_write_failure_is_observable(self, clean_registry, monkeypatch):
        """A failed snapshot is recorded in diagnostics; the chat path still runs."""
        import os

        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION

        def _boom(src, dst):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(os, "replace", _boom)
        store.arm_pending_goal_continuation("sess-a", "prompt-a")  # must not raise
        diag = store.durability_diagnostics()
        assert diag["last_write_error"] is not None
        assert "disk full" in diag["last_write_error"]
        # The turn continues: the in-memory marker is live even though the
        # durable snapshot failed.
        assert "sess-a" in PENDING_GOAL_CONTINUATION


class TestSourceShapes:
    """The three writer call sites use the locked mutators (not the old API)."""

    def test_streaming_arms_via_locked_mutator(self):
        src = Path("api/streaming.py").read_text(encoding="utf-8")
        m = re.search(r"PENDING_GOAL_CONTINUATION\.add\(session_id\)", src)
        assert m is not None
        tail = src[m.end():m.end() + 700]
        assert "arm_pending_goal_continuation" in tail
        # #7862 round 3: the minted token must reach both the store and the
        # SSE payload in the same window.
        assert "continuation_id" in tail

    def test_gateway_arms_via_locked_mutator(self):
        src = Path("api/gateway_chat.py").read_text(encoding="utf-8")
        m = re.search(r"PENDING_GOAL_CONTINUATION\.add\(session_id\)", src)
        assert m is not None
        tail = src[m.end():m.end() + 700]
        assert "arm_pending_goal_continuation" in tail
        # #7862 round 3: the minted token must reach both the store and the
        # SSE payload in the same window.
        assert "continuation_id" in tail

    def test_routes_consumes_via_match_gated_mutator(self):
        """#7862: routes must consume through the match-gated mutator.

        The previous shape (bare ``discard(s.session_id)`` followed by an
        unconditional retire) is exactly the id-only consumption the reviewer
        blocked, so this guard now pins the match-gated call AND asserts the
        id-only form is gone.
        """
        src = Path("api/routes.py").read_text(encoding="utf-8")
        m = re.search(
            r"consume_pending_goal_continuation\(\s*"
            r"s\.session_id,\s*msg,\s*goal_continuation_id\s*"
            r",\s*goal_continuation_attempt_id\s*,?\s*\)",
            src,
        )
        assert m is not None
        tail = src[m.end():m.end() + 200]
        assert "goal_related = True" in tail
        # The id-only consumption must not reappear anywhere in the chat path.
        assert "PENDING_GOAL_CONTINUATION.discard(s.session_id)" not in src

    def test_old_snapshot_api_removed(self):
        for name in ("api/streaming.py", "api/gateway_chat.py", "api/routes.py"):
            src = Path(name).read_text(encoding="utf-8")
            assert "snapshot_pending_goal_continuations" not in src, name


class TestRejectedStartRollback:
    """Review round 3 (CORE finding): a rejected chat start must not lose the
    continuation, and a matching retry must still consume it.

    ``consume_pending_goal_continuation`` deletes the durable record as well
    as the marker; chat-start's rejected-start rollback used to restore only
    the marker, so the retry ran as an ordinary turn and the goal loop lost
    its continuation (master's marker-only consume hid this). The store now
    hands back a rollback receipt and restores marker + record together.
    """

    PROMPT = "Continue the standing goal, please."

    def test_failed_start_then_matching_retry_still_consumes(self, clean_registry):
        """The exact sequence the reviewer reproduced, now via the real store."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-reject"
        # 1. arm
        store.arm_pending_goal_continuation(
            sid, self.PROMPT, reason="goal_continue", continuation_id="tok-1"
        )
        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid) is not None
        # 2. the consume for the start that is about to be rejected
        assert store.consume_pending_goal_continuation(sid, self.PROMPT, "tok-1")== store.CONSUME_COMMITTED
        assert sid not in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid) is None
        # 3. rollback, as restore_consumed_continuation_markers() now does:
        #    marker True AND record True (the receipt carries the record back)
        receipt = store.pop_goal_continuation_rollback_receipt(sid)
        assert receipt is not None
        assert receipt.get("prompt") == self.PROMPT
        assert store.restore_pending_goal_continuation(sid, receipt) is True
        assert sid in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_RECORDS.get(sid) is not None
        assert PENDING_GOAL_CONTINUATION_RECORDS[sid]["prompt"] == self.PROMPT
        # 4. the retry still consumes as the continuation
        assert store.consume_pending_goal_continuation(sid, self.PROMPT, "tok-1")== store.CONSUME_COMMITTED

    def test_rollback_survives_the_disk_registry(self, clean_registry):
        """The restored intent is durable, not just an in-memory marker."""
        from api import goal_continuation_store as store

        sid = "sess-durable-rollback"
        store.arm_pending_goal_continuation(sid, self.PROMPT, continuation_id="tok-2")
        assert store.consume_pending_goal_continuation(sid, self.PROMPT, "tok-2")== store.CONSUME_COMMITTED
        receipt = store.pop_goal_continuation_rollback_receipt(sid)
        assert receipt is not None
        assert store.restore_pending_goal_continuation(sid, receipt) is True
        # What a restarted process would reload from disk is the SAME intent.
        disk = store.load_pending_goal_continuations()
        assert disk[sid]["prompt"] == self.PROMPT
        assert disk[sid]["continuation_id"] == "tok-2"
        assert store.consume_pending_goal_continuation(sid, self.PROMPT, "tok-2")== store.CONSUME_COMMITTED
        assert store.load_pending_goal_continuations() == {}
    def test_rollback_receipt_is_single_use(self, clean_registry):
        """Popping the receipt twice yields None the second time."""
        from api import goal_continuation_store as store

        sid = "sess-single-use"
        store.arm_pending_goal_continuation(sid, self.PROMPT)
        assert store.consume_pending_goal_continuation(sid, self.PROMPT)== store.CONSUME_COMMITTED
        first = store.pop_goal_continuation_rollback_receipt(sid)
        assert first is not None
        assert store.pop_goal_continuation_rollback_receipt(sid) is None

    def test_newer_intent_discards_stale_receipt(self, clean_registry):
        """An intent armed after the rejected start must not be resurrectable
        by the older receipt: arming drops the stale receipt."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-supersede"
        store.arm_pending_goal_continuation(sid, self.PROMPT, continuation_id="tok-old")
        assert store.consume_pending_goal_continuation(sid, self.PROMPT, "tok-old")== store.CONSUME_COMMITTED
        # A newer intent arrives (e.g. the goal loop queued a fresh one).
        store.arm_pending_goal_continuation(
            sid, "A NEWER continuation prompt.", continuation_id="tok-new"
        )
        # The stale receipt for the OLD generation is gone.
        assert store.pop_goal_continuation_rollback_receipt(sid) is None
        # The live intent is the newer one, untouched.
        assert PENDING_GOAL_CONTINUATION_RECORDS[sid]["prompt"] == "A NEWER continuation prompt."
        assert store.consume_pending_goal_continuation(
            sid, "A NEWER continuation prompt.", "tok-new"
        ) == store.CONSUME_COMMITTED

    def test_stale_receipt_cannot_overwrite_newer_generation(self, clean_registry):
        """Even a hand-held receipt must not clobber a newer live intent."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-gen-guard"
        store.arm_pending_goal_continuation(sid, self.PROMPT, continuation_id="tok-old")
        assert store.consume_pending_goal_continuation(sid, self.PROMPT, "tok-old")== store.CONSUME_COMMITTED
        receipt = store.pop_goal_continuation_rollback_receipt(sid)
        assert receipt is not None
        # Newer intent armed before the (late) rollback tries to land.
        store.arm_pending_goal_continuation(
            sid, "A NEWER continuation prompt.", continuation_id="tok-new"
        )
        assert store.restore_pending_goal_continuation(sid, receipt) is False
        assert PENDING_GOAL_CONTINUATION_RECORDS[sid]["prompt"] == "A NEWER continuation prompt."

    def test_receipts_are_bounded(self, clean_registry):
        """Receipts are bounded WITHOUT ever evicting a live attempt's slot.

        The bound is a TTL, not a count cap. With 65 starts in flight for one
        session, a global (or per-session) count cap silently evicted the
        in-flight attempt's receipt, and its rejected-start rollback then found
        nothing to claim and fell back to a bare marker the retry could not
        match (#7862 round 6, scenario 1). So the count cap is gone: a receipt
        whose start attempt neither launched nor rolled back is dropped once it
        is older than ``_ROLLBACK_RECEIPT_TTL_SECONDS``, which no live attempt
        can be.
        """
        from api import goal_continuation_store as store

        for i in range(store._MAX_ROLLBACK_RECEIPTS + 20):
            sid = f"sess-bound-{i}"
            store.arm_pending_goal_continuation(sid, self.PROMPT)
            store.consume_pending_goal_continuation(sid, self.PROMPT)
        # Fresh receipts are all still there: nothing was evicted by count.
        assert len(store._ROLLBACK_RECEIPTS) == store._MAX_ROLLBACK_RECEIPTS + 20

        # Age them past the TTL; the next record's sweep drops them all.
        # #7862 round 10 (finding 3): the handoff anchors a live receipt, so age
        # it as well. A fresh handoff would pin every receipt as in-flight and
        # nothing would ever be swept.
        with store._LOCK:
            stale = store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 1
            for key, rec in store._ROLLBACK_RECEIPTS.items():
                rec["_receipt_minted_at"] = stale
            for rec in store._CONTINUATION_HANDOFF_TOKENS.values():
                rec["_handoff_minted_at"] = stale
            dropped = store._sweep_expired_receipts_unlocked()
        assert dropped == store._MAX_ROLLBACK_RECEIPTS + 20
        assert len(store._ROLLBACK_RECEIPTS) == 0

    def test_a_live_attempt_survives_unrelated_traffic(self, clean_registry):
        """The in-flight receipt survives any amount of unrelated churn.

        Scenario 1 of the round-6 probe: 65 starts in flight, the first one
        rejected. The rejection lands on the FIRST attempt, so its receipt must
        still be claimable after every other start has consumed its own.
        """
        from api import goal_continuation_store as store

        sid = "sess-crowded"
        store.arm_pending_goal_continuation(sid, self.PROMPT, continuation_id="tok-live")
        assert store.consume_pending_goal_continuation(
            sid, self.PROMPT, "tok-live", "att-live"
        )
        # 64 more DISTINCT starts in flight -- one chat start per session, as
        # the session agent lock guarantees. (A second arm on THIS session
        # would drop the earlier receipt by design: a fresh intent supersedes
        # the older one.)
        for i in range(65):
            other = f"sess-bulk-{i}"
            store.arm_pending_goal_continuation(
                other, self.PROMPT, continuation_id=f"tb-{i}"
            )
            assert store.consume_pending_goal_continuation(
                other, self.PROMPT, f"tb-{i}", f"ab-{i}"
            )
        # The rejected start's rollback can still claim exactly its receipt.
        receipt = store.pop_goal_continuation_rollback_receipt(sid, "att-live")
        assert receipt is not None, "the in-flight receipt was evicted"
        assert receipt.get("continuation_id") == "tok-live"

    def test_unrelated_turn_is_not_swallowed_after_restore(self, clean_registry):
        """The restored intent keeps the match-gated consume semantics."""
        from api import goal_continuation_store as store

        sid = "sess-unrelated"
        store.arm_pending_goal_continuation(sid, self.PROMPT, continuation_id="tok-3")
        assert store.consume_pending_goal_continuation(sid, self.PROMPT, "tok-3")== store.CONSUME_COMMITTED
        receipt = store.pop_goal_continuation_rollback_receipt(sid)
        assert store.restore_pending_goal_continuation(sid, receipt) is True
        # An unrelated human message still does NOT consume the intent.
        assert store.consume_pending_goal_continuation(sid, "unrelated chatter", "")== store.CONSUME_NOT_MATCHING

    def test_pending_receipts_are_observable_in_diagnostics(self, clean_registry):
        """Pending rollback receipts surface in durability_diagnostics."""
        from api import goal_continuation_store as store

        sid = "sess-diag"
        store.arm_pending_goal_continuation(sid, self.PROMPT)
        assert store.durability_diagnostics()["pending_rollback_receipts"] == 0
        assert store.consume_pending_goal_continuation(sid, self.PROMPT)== store.CONSUME_COMMITTED
        assert store.durability_diagnostics()["pending_rollback_receipts"] == 1
        assert store.pop_goal_continuation_rollback_receipt(sid) is not None
        assert store.durability_diagnostics()["pending_rollback_receipts"] == 0


class TestRejectedStartRollbackRouteShape:
    """routes.py must restore marker + record, not the marker alone."""

    def test_rollback_restores_via_store_receipt(self):
        """routes.py rollback must restore marker + record via the store receipt.

        Pins the fix for the round-3 CORE regression: the marker-only restore
        left the retry unmatched, so a rejected chat start lost the goal
        continuation. The rollback now claims the consume's receipt and
        restores through the locked store mutator.
        """
        src = Path("api/routes.py").read_text(encoding="utf-8")
        m = re.search(r"def restore_consumed_continuation_markers\(\).*?(?=\n    session_lock)", src, re.DOTALL)
        assert m is not None
        window = m.group(0)
        assert "pop_goal_continuation_rollback_receipt" in window
        assert "restore_pending_goal_continuation" in window
        # The bare marker-only restore is only the legacy fallback (no
        # receipt), never the unconditional path it used to be.
        assert window.count("PENDING_GOAL_CONTINUATION.add(s.session_id)") <= 1
        assert "receipt is not None" in window
        # Round 6: after a store-backed consume there is no legacy fallback at
        # all. "No receipt" means the receipt is gone, and re-adding a bare
        # marker resurrects a state the store's consume will refuse to match.
        # It must be logged, not papered over with a marker.
        assert "PENDING_GOAL_CONTINUATION.add(s.session_id)" not in window, (
            "the bare-marker fallback must stay gone after a store-backed consume"
        )
        assert "no receipt for attempt" in window
