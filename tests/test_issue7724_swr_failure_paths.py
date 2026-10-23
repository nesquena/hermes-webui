"""#7724 re-gate — the three findings still open at this head.

The earlier round of this PR replaced the ``os.utime`` restamp with a
stale-while-revalidate design, which retired two of the reported findings on its
own (a memory hit no longer needs to extend freshness, and the "failure test
never fails" test no longer exists — it asserted the absence of the restamp).
Three were still live at the head reviewed here:

1. **Refresh failures stay silent.** The SWR worker stored its exception in a
   ``box`` dict that nothing read and nothing logged, then untracked itself. The
   stale catalog stayed in place, so every later stale visit silently launched
   another expensive retry — a permanently failing rebuild looked exactly like
   healthy staleness.
2. **A thread-start failure breaks the cache.** ``thread.start()`` was
   unguarded, so a resource-pressure ``RuntimeError`` escaped the helper and
   turned a session-visit request that already held a usable stale catalog into
   an HTTP 500. The untracking cleanup lives inside the worker, which never ran.
3. **The logger override is bypassed.** ``profile_env_for_background_worker``
   accepts ``logger_override`` and honours it on its sibling failure paths, but
   the unresolvable-profile-home branch logged through the module logger, so
   background title/compression/checkpoint/route workers lost that diagnostic
   from their intended channel — precisely when profile routing could not be
   established.

These tests drive the real functions. Where a thread is involved the worker is
exercised by calling the tracked target directly rather than by racing a real
thread, so the assertions are deterministic.
"""

from __future__ import annotations

import logging
import threading as _real_threading

import pytest

from api import config, profiles


class _FakeThreading:
    """Stand-in for the ``threading`` module inside ``api.config``.

    ``api.config`` does ``import threading`` at module level and then reaches it
    through ``config.threading``, so patching ``threading.Thread`` directly would
    replace the class for the WHOLE process and deadlock every other lock user.
    Swap the module reference instead and keep every other attribute real.
    """

    def __init__(self, thread_cls):
        self._thread_cls = thread_cls

    @property
    def Thread(self):
        return self._thread_cls

    def __getattr__(self, name):
        return getattr(_real_threading, name)


@pytest.fixture(autouse=True)
def _isolated_swr_tracker():
    """Keep each test's coalescing tracker to itself.

    The tracker is module state; a real background rebuild started by an earlier
    test would still be alive here and make the "already rebuilding" branch
    short-circuit the assertions below.

    The lock is acquired and released around each mutation and never held across
    the ``yield`` — the code under test takes the same non-reentrant lock on its
    failure path, so holding it here would deadlock the very assertion this
    fixture exists to make reachable.
    """
    with config._session_visit_rebuild_lock:
        saved = dict(config._session_visit_rebuild_threads)
        config._session_visit_rebuild_threads.clear()
    try:
        yield
    finally:
        with config._session_visit_rebuild_lock:
            config._session_visit_rebuild_threads.clear()
        with config._session_visit_rebuild_lock:
            config._session_visit_rebuild_threads.update(saved)


# ── finding 1: a failed background rebuild is visible ───────────────────────


def test_a_failed_background_rebuild_is_logged(caplog):
    """The exception must reach a log record, not only the write-only box."""
    started = {}

    class _Thread:
        def __init__(self, target=None, name=None, daemon=None):
            started["target"] = target
            started["name"] = name

        def start(self):
            started["started"] = True

        def is_alive(self):
            return False

    def _boom(force_refresh=False):
        raise RuntimeError("probe exploded")

    original_threading = config.threading
    original_get_models = config.get_available_models
    config.threading = _FakeThreading(_Thread)
    config.get_available_models = _boom
    try:
        with caplog.at_level(logging.DEBUG, logger="api.config"):
            config._maybe_start_session_visit_background_rebuild()
            assert started.get("started") is True, "the rebuild was never launched"
            # Drive the worker body synchronously: a real thread would race the
            # assertions below and the failure path is what is under test.
            started["target"]()
    finally:
        config.threading = original_threading
        config.get_available_models = original_get_models

    matching = [
        record
        for record in caplog.records
        if "session-visit background models rebuild failed" in record.getMessage()
    ]
    assert matching, (
        "a failed background rebuild produced no log record; the stale catalog "
        "stays in place and every later stale visit silently retries"
    )
    assert matching[0].exc_info is not None, (
        "the failure was logged without its traceback"
    )


def test_a_successful_background_rebuild_logs_nothing(caplog):
    """Negative control: the success path must not emit a failure line."""
    started = {}

    class _Thread:
        def __init__(self, target=None, name=None, daemon=None):
            started["target"] = target

        def start(self):
            started["started"] = True

        def is_alive(self):
            return False

    original_threading = config.threading
    config.threading = _FakeThreading(_Thread)
    try:
        with caplog.at_level(logging.DEBUG, logger="api.config"):
            config._maybe_start_session_visit_background_rebuild()
            started["target"]()
    finally:
        config.threading = original_threading

    assert not [
        record
        for record in caplog.records
        if "session-visit background models rebuild failed" in record.getMessage()
    ], "the success path emitted a failure log line"


# ── finding 2: a thread that cannot start degrades instead of raising ────────


def test_a_thread_start_failure_does_not_raise():
    """The caller must still be able to return its stale catalog."""
    original_threading = config.threading

    class _Thread:
        def __init__(self, target=None, name=None, daemon=None):
            self.name = name

        def start(self):
            raise RuntimeError("can't create new thread")

        def is_alive(self):
            return False

    config.threading = _FakeThreading(_Thread)
    try:
        # Before the fix this propagated and 500'd the request.
        config._maybe_start_session_visit_background_rebuild()
    finally:
        config.threading = original_threading


def test_a_thread_start_failure_clears_the_tracker():
    """A thread that never started must not leave a stale tracker entry."""
    original_threading = config.threading
    config._session_visit_rebuild_threads.clear()

    class _Thread:
        def __init__(self, target=None, name=None, daemon=None):
            self.name = name

        def start(self):
            raise RuntimeError("can't create new thread")

        def is_alive(self):
            return False

    config.threading = _FakeThreading(_Thread)
    try:
        config._maybe_start_session_visit_background_rebuild()
        with config._session_visit_rebuild_lock:
            tracked = dict(config._session_visit_rebuild_threads)
    finally:
        config.threading = original_threading
        config._session_visit_rebuild_threads.clear()

    assert tracked == {}, (
        "a thread that never started is still tracked, so every later stale "
        f"visit short-circuits on a dead entry: {tracked!r}"
    )


# ── finding 3: the logger override is honoured ──────────────────────────────


def test_an_unresolvable_profile_home_uses_the_override_logger(monkeypatch):
    """The override must receive the diagnostic, not only the module logger."""
    override = logging.getLogger("test.7724.override")

    def _boom(profile):
        raise RuntimeError("no such home")

    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", _boom)

    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Handler()
    override.addHandler(handler)
    override.setLevel(logging.DEBUG)
    try:
        with profiles.profile_env_for_background_worker(
            "named-profile", "test worker", logger_override=override
        ):
            pass
    finally:
        override.removeHandler(handler)

    assert any(
        "Failed to resolve profile env" in record.getMessage() for record in records
    ), (
        "the unresolvable-profile-home branch logged through the module logger, "
        "so a caller that supplied an override lost the diagnostic"
    )


def test_the_default_profile_still_short_circuits():
    """Negative control: the no-op branch must not log or resolve anything."""
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("api.profiles")
    handler = _Handler()
    logger.addHandler(handler)
    try:
        with profiles.profile_env_for_background_worker(None, "test worker"):
            pass
    finally:
        logger.removeHandler(handler)

    assert not any(
        "Failed to resolve profile env" in record.getMessage() for record in records
    ), "the default-profile no-op branch emitted a failure line"


# ── regression: the failure path must not self-deadlock ─────────────────────


def test_the_failure_path_does_not_hold_the_coalescing_lock():
    """The tracker mutation and thread.start() must not share one lock scope.

    ``_session_visit_rebuild_lock`` is NOT reentrant, and the tracker entry is
    written while holding it. An earlier version of the thread-start guard took
    the same lock again in its ``except`` arm — still inside the ``with`` block —
    so a thread-start failure deadlocked the request thread permanently. That is
    strictly worse than the HTTP 500 the guard was added to prevent, and it only
    reproduces when ``start()`` actually raises, which is why it needs its own
    test rather than being covered by the two above.
    """
    original_threading = config.threading
    config._session_visit_rebuild_threads.clear()

    class _Thread:
        def __init__(self, target=None, name=None, daemon=None):
            self.name = name

        def start(self):
            raise RuntimeError("can't create new thread")

        def is_alive(self):
            return False

    config.threading = _FakeThreading(_Thread)

    finished = []

    def _run():
        config._maybe_start_session_visit_background_rebuild()
        finished.append(True)

    worker = _real_threading.Thread(target=_run, daemon=True)
    worker.start()
    # A deadlock here would hang forever; join with a bound so the test fails
    # instead of stalling the whole suite.
    worker.join(timeout=30)

    try:
        assert finished, (
            "_maybe_start_session_visit_background_rebuild did not return — the "
            "thread-start failure path deadlocked on _session_visit_rebuild_lock"
        )
        assert not worker.is_alive(), "the call is still running after 30 s"
    finally:
        config.threading = original_threading
        with config._session_visit_rebuild_lock:
            config._session_visit_rebuild_threads.clear()
