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
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    for tmp in store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"):
        tmp.unlink(missing_ok=True)
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
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
        tail = src[m.end():m.end() + 400]
        assert "arm_pending_goal_continuation" in tail

    def test_gateway_arms_via_locked_mutator(self):
        src = Path("api/gateway_chat.py").read_text(encoding="utf-8")
        m = re.search(r"PENDING_GOAL_CONTINUATION\.add\(session_id\)", src)
        assert m is not None
        tail = src[m.end():m.end() + 400]
        assert "arm_pending_goal_continuation" in tail

    def test_routes_consumes_via_locked_mutator(self):
        src = Path("api/routes.py").read_text(encoding="utf-8")
        m = re.search(r"PENDING_GOAL_CONTINUATION\.discard\(s\.session_id\)", src)
        assert m is not None
        tail = src[m.end():m.end() + 400]
        assert "retire_pending_goal_continuation" in tail

    def test_old_snapshot_api_removed(self):
        for name in ("api/streaming.py", "api/gateway_chat.py", "api/routes.py"):
            src = Path(name).read_text(encoding="utf-8")
            assert "snapshot_pending_goal_continuations" not in src, name
