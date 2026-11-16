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

Isolation contract (this module is self-contained on purpose):

* every test starts from whatever state the process is in and is expected to
  leave the admission fence **open** and no worker behind;
* the ``_clean_restart_state`` fixture does that cleanup in a ``finally``, so
  it also runs when a test body fails on an assertion (the failing
  ``test_stub_*`` paths close the fence through the real
  ``run_drain.mark_restart_pending()`` before they fail);
* ``test_boundary_after_stub_tests_leaves_a_clean_fence`` is a
  following-test boundary check: it runs after the stub tests and fails if
  either the worker cleanup or the admission-state cleanup did not happen.
"""

from __future__ import annotations

import importlib
import tempfile
import threading
from pathlib import Path

import pytest

from api import run_drain
from tests._updater_restart_isolation import (
    apply_lock_is_free,
    assert_no_restart_worker_escaped,
    isolated_restart_scheduler,
    live_restart_workers,
    wait_for_restart_workers,
)
from tests import test_issue7293_restart_drain_policy as drain_policy
from tests import test_update_checker as update_checker

# Threads that existed when this module started running. The boundary check
# scopes itself to this snapshot so a worker leaked by some *other* test file
# earlier in the session is not blamed on the sequence tested here.
_MODULE_START_THREADS: set[threading.Thread] | None = None

# Workers still alive after a test body finished and its teardown ran. The
# following-test boundary check reads this: raising from the teardown would
# report an ERROR next to the test's own verdict, so the leak is reported by
# the test that can actually explain it.
_LEAKED_IN_TEARDOWN: list[str] = []


def _updates():
    """The live ``api.updates`` module.

    Resolved per call, not at import: other tests delete
    ``sys.modules["api.updates"]`` and re-import it, and a module object bound
    here at collection time would then be stale — its real ``_schedule_restart``
    (and its own ``_apply_lock``) would run while the stub sits on the live
    module, so the test would grade code it never patched.
    """
    return importlib.import_module("api.updates")


@pytest.fixture(autouse=True)
def _clean_restart_state():
    """Per-test cleanup: no leaked worker, admission fence reopened.

    The new regression tests drive the real ``run_drain.mark_restart_pending()``
    through the scheduler stub, and the drain-policy module's own
    ``_clean_registries`` fixture does not apply to a test function that this
    module calls directly. Without this fixture the fence stays closed after
    the module finishes — after a complete pytest teardown, not just after the
    test — and later tests see ``503``.

    The reset happens in ``finally`` **before** any assertion of this fixture,
    so an assertion failure in the test body cannot leave the fence closed for
    the following test.
    """
    global _MODULE_START_THREADS
    if _MODULE_START_THREADS is None:
        _MODULE_START_THREADS = set(threading.enumerate())

    before = set(threading.enumerate())
    try:
        yield
    finally:
        # Reopen admission regardless of how the test body ended — including
        # when the body failed an assertion after the real
        # ``mark_restart_pending()`` had already closed the fence.
        run_drain._reset_for_tests()
        _LEAKED_IN_TEARDOWN.extend(wait_for_restart_workers(before, timeout=5.0))


class TestRestartWorkerIsolation:
    """The scheduler stub must keep the fence and drop the worker."""

    def test_stub_closes_the_admission_fence(self, monkeypatch):
        """A stubbed restart still closes the fence — that is what production does."""
        with isolated_restart_scheduler(monkeypatch):
            _updates()._schedule_restart(delay=60)

            refusal = run_drain.run_admission_closed_response()
            assert refusal is not None
            assert refusal["_status"] == 503

    def test_stub_spawns_no_worker_thread(self, monkeypatch):
        """The stub records the request but starts no daemon worker."""
        before = set(threading.enumerate())

        with isolated_restart_scheduler(monkeypatch) as restart:
            _updates()._schedule_restart(delay=60)
            assert restart.calls == [60]

        spawned = [t for t in threading.enumerate() if t not in before]
        assert not [t for t in spawned if t.name.endswith("(_do)")], (
            f"the stub spawned a restart worker: {[t.name for t in spawned]}"
        )

    def test_boundary_after_stub_tests_leaves_a_clean_fence(self):
        """Following-test boundary: the previous tests left nothing behind.

        Runs directly after the two stub tests. Both of them close the fence
        through the real ``mark_restart_pending()``; if this module did not
        clean that state up, admission is still closed here and this test
        fails with the 503 payload instead of ``None``. A worker that survived
        its test is visible the same way.
        """
        assert not _LEAKED_IN_TEARDOWN, (
            "a restart worker survived its test's teardown: "
            f"{_LEAKED_IN_TEARDOWN}"
        )
        closed = run_drain.run_admission_closed_response()
        assert closed is None, (
            "restart admission is still closed after the previous test: "
            f"{closed}"
        )
        assert not live_restart_workers(_MODULE_START_THREADS or ()), (
            "a restart worker survived the previous test: "
            f"{live_restart_workers(_MODULE_START_THREADS or ())}"
        )
        assert apply_lock_is_free()

    def test_drain_policy_success_path_leaves_no_worker(self, tmp_path, monkeypatch):
        """The #7293 success path must not leave a worker behind."""
        before = set(threading.enumerate())
        run_drain._reset_for_tests()
        try:
            drain_policy.test_successful_update_keeps_admission_closed_until_restart(
                tmp_path, monkeypatch
            )
        finally:
            run_drain._reset_for_tests()

        assert not live_restart_workers(before), (
            "the #7293 success path leaked a restart worker: "
            f"{live_restart_workers(before)}"
        )
        assert apply_lock_is_free()

    def test_update_checker_success_path_leaves_no_worker(self, tmp_path, monkeypatch):
        """The update-checker success path must not leave a worker behind."""
        before = set(threading.enumerate())
        run_drain._reset_for_tests()
        try:
            update_checker.TestApplyUpdateDiagnostics().test_successful_update_still_returns_ok(
                tmp_path
            )
        finally:
            run_drain._reset_for_tests()

        assert not live_restart_workers(before), (
            "the update-checker success path leaked a restart worker: "
            f"{live_restart_workers(before)}"
        )
        assert apply_lock_is_free()

    def test_a_later_update_is_not_refused(self, tmp_path, monkeypatch):
        """A leaked worker refuses every later apply_update() in the session.

        This is the user-visible symptom the issue reports: the updater answers
        "Update already in progress" for an update nobody started.
        """
        before = set(threading.enumerate())
        run_drain._reset_for_tests()
        try:
            update_checker.TestApplyUpdateDiagnostics().test_successful_update_still_returns_ok(tmp_path)
        finally:
            run_drain._reset_for_tests()

        # Give a leaked worker the moment it needs to take the lock.
        assert_no_restart_worker_escaped(timeout=5.0, ignore=before)
        updates = _updates()

        # Drive the real refusal path (apply_update takes _apply_lock with
        # blocking=False) against a throwaway repo, so the assertion is about
        # the lock and not about anybody's git checkout.
        with tempfile.TemporaryDirectory() as repo:
            repo_path = Path(repo)
            (repo_path / ".git").mkdir()
            monkeypatch.setattr(updates, "REPO_ROOT", repo_path)
            monkeypatch.setattr(updates, "_AGENT_DIR", repo_path)
            monkeypatch.setattr(updates, "_update_cache", {"webui": None, "agent": None, "checked_at": 1})
            monkeypatch.setattr(updates, "_cache_lock", threading.Lock())
            monkeypatch.setattr(updates, "_run_git", lambda *a, **k: ("", True))
            monkeypatch.setattr(updates, "_schedule_restart", lambda *a, **k: None)

            result = updates.apply_update("webui")

        assert "already in progress" not in str(result.get("message", "")).lower(), result

    def test_assertion_helper_detects_a_leaked_worker(self):
        """The teardown check actually fails when a worker is still alive."""
        stop = threading.Event()
        worker = threading.Thread(target=stop.wait, name="Thread-leak (_do)", daemon=True)
        worker.start()
        try:
            with pytest.raises(AssertionError, match="restart worker"):
                assert_no_restart_worker_escaped(timeout=1.0)
        finally:
            # Stop the fake worker: a test about leaked workers must not leak one.
            stop.set()
            worker.join(timeout=5)
        assert not worker.is_alive()
