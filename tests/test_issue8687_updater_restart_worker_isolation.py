"""#8687 — an updater success path must not leak a restart worker.

``api.updates._schedule_restart()`` spawns a daemon worker that takes
``api.updates._apply_lock`` and walks the real repository's ``__pycache__``
tree before the re-exec. The worker outlives the test that scheduled it, and
while it is alive every later ``apply_update()`` in the same pytest session is
refused with ``Update already in progress``.

Two success-path tests reached the real scheduler:

* ``tests/test_issue7293_restart_drain_policy.py::test_successful_update_keeps_admission_closed_until_restart``
* ``tests/test_update_checker.py::TestApplyUpdateDiagnostics::test_successful_update_still_returns_ok``

The first one asserts that the run-admission fence stays closed after a
successful update, so the fix cannot simply drop the whole call — the fence
is closed inside ``_schedule_restart()``, before the worker starts.

These tests pin both halves of the contract:

* the fence still closes (the original assertion keeps its meaning);
* no restart worker escapes, and ``_apply_lock`` is free afterwards.

The regression is driven by calling the two offending test functions
themselves, so it fails on the unfixed tree regardless of test order — the
leaked worker is observed directly rather than inferred from a later
refusal.
"""

from __future__ import annotations

import threading

import pytest

from api import run_drain
from api import updates
from tests._updater_restart_isolation import (
    apply_lock_is_free,
    assert_no_restart_worker_escaped,
    isolated_restart_scheduler,
    live_restart_workers,
)
from tests.test_issue7293_restart_drain_policy import (
    test_successful_update_keeps_admission_closed_until_restart,
)
from tests.test_update_checker import TestApplyUpdateDiagnostics


class TestRestartWorkerIsolation:
    """The scheduler stub must keep the fence and drop the worker."""

    def test_stub_closes_the_admission_fence(self, monkeypatch):
        """A stubbed restart still closes the fence — that is what production does."""
        with isolated_restart_scheduler(monkeypatch):
            updates._schedule_restart(delay=60)

            refusal = run_drain.run_admission_closed_response()
            assert refusal is not None
            assert refusal["_status"] == 503

    def test_stub_spawns_no_worker_thread(self, monkeypatch):
        """The stub records the request but starts no daemon worker."""
        before = set(threading.enumerate())

        with isolated_restart_scheduler(monkeypatch) as restart:
            updates._schedule_restart(delay=60)
            assert restart.calls == [60]

        spawned = [t for t in threading.enumerate() if t not in before]
        assert not [t for t in spawned if t.name.endswith("(_do)")], (
            f"the stub spawned a restart worker: {[t.name for t in spawned]}"
        )

    def test_drain_policy_success_path_leaves_no_worker(self, tmp_path, monkeypatch):
        """The #7293 success path must not leave a worker behind."""
        run_drain._reset_for_tests()
        try:
            test_successful_update_keeps_admission_closed_until_restart(tmp_path, monkeypatch)
        finally:
            run_drain._reset_for_tests()

        assert not live_restart_workers(), (
            "the #7293 success path leaked a restart worker: "
            f"{live_restart_workers()}"
        )
        assert apply_lock_is_free()

    def test_update_checker_success_path_leaves_no_worker(self, tmp_path, monkeypatch):
        """The update-checker success path must not leave a worker behind."""
        run_drain._reset_for_tests()
        try:
            TestApplyUpdateDiagnostics().test_successful_update_still_returns_ok(tmp_path)
        finally:
            run_drain._reset_for_tests()

        assert not live_restart_workers(), (
            "the update-checker success path leaked a restart worker: "
            f"{live_restart_workers()}"
        )
        assert apply_lock_is_free()

    def test_a_later_update_is_not_refused(self, tmp_path, monkeypatch):
        """A leaked worker refuses every later apply_update() in the session.

        This is the user-visible symptom the issue reports: the updater answers
        "Update already in progress" for an update nobody started.
        """
        run_drain._reset_for_tests()
        try:
            TestApplyUpdateDiagnostics().test_successful_update_still_returns_ok(tmp_path)
        finally:
            run_drain._reset_for_tests()

        # Give a leaked worker the moment it needs to take the lock.
        assert_no_restart_worker_escaped(timeout=5.0)

        result = updates.apply_update("webui")
        assert result.get("ok") is not False, result
        assert "already in progress" not in str(result.get("message", "")).lower(), result

    def test_assertion_helper_detects_a_leaked_worker(self):
        """The teardown check actually fails when a worker is still alive."""
        worker = threading.Thread(
            target=lambda: threading.Event().wait(30),
            name="Thread-leak (_do)",
            daemon=True,
        )
        worker.start()
        with pytest.raises(AssertionError, match="restart worker"):
            assert_no_restart_worker_escaped(timeout=1.0)
