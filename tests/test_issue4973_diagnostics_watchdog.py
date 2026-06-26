"""Regression coverage for #4973 — RequestDiagnostics must not spawn one OS
thread per request.

Before the fix, every ``RequestDiagnostics(auto_start=True)`` started its own
``threading.Timer`` (one OS thread per request, held alive until ``finish()``).
Under sustained ``/api/sessions`` poll load that exhausts the per-process thread
cap (``RuntimeError: can't start new thread``) and the server stops accepting
connections. The fix replaces the per-instance timer with a single
process-global watchdog daemon thread.
"""

import logging
import threading
import time

import api.request_diagnostics as rd
from api.request_diagnostics import RequestDiagnostics


def _watchdog_thread_count() -> int:
    return sum(
        1 for t in threading.enumerate()
        if t.name == "request-diagnostics-watchdog"
    )


def test_many_concurrent_diags_do_not_spawn_a_thread_each():
    """N in-flight auto_start diags must add at most ONE watchdog thread."""
    logger = logging.getLogger("test.issue4973.bounded")
    baseline = _watchdog_thread_count()

    diags = [
        RequestDiagnostics(
            "GET", "/api/sessions", logger=logger, timeout_seconds=30
        )
        for _ in range(200)
    ]
    try:
        # The whole point: 200 concurrent diags, not 200 new threads.
        added = _watchdog_thread_count() - baseline
        assert added <= 1, f"expected <=1 watchdog thread, got {added} added"
        # All 200 are registered as pending (none lost).
        with rd._watchdog_cv:
            pending = len(rd._watchdog_pending)
        assert pending >= 200
    finally:
        for d in diags:
            d.finish()

    # finish() unregisters every diag so the pending dict stays bounded.
    with rd._watchdog_cv:
        remaining = sum(
            1 for _rid, (_dl, diag) in rd._watchdog_pending.items()
            if diag in diags
        )
    assert remaining == 0


def test_watchdog_fires_on_timeout_for_a_slow_request(caplog):
    """A request that never finishes before its deadline still gets logged by
    the process-global watchdog (behavioral, not just structural)."""
    logger = logging.getLogger("test.issue4973.fires")
    # Tiny timeout so the watchdog (1s tick) fires quickly.
    diag = RequestDiagnostics(
        "GET", "/api/sessions", logger=logger, timeout_seconds=0.05
    )
    diag.stage("slow_stage")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if any("still running" in (r.getMessage() or "") for r in caplog.records):
                break
            time.sleep(0.1)
    assert any("still running" in (r.getMessage() or "") for r in caplog.records), \
        "watchdog did not log the slow request within the window"
    diag.finish()


def test_finish_before_deadline_prevents_watchdog_log(caplog):
    """A request that finishes fast must NOT be logged by the watchdog."""
    logger = logging.getLogger("test.issue4973.fast")
    diag = RequestDiagnostics(
        "GET", "/api/sessions", logger=logger, timeout_seconds=0.05
    )
    diag.finish()  # completes immediately, well under the deadline
    with caplog.at_level(logging.WARNING, logger=logger.name):
        time.sleep(1.5)  # give the watchdog a couple of ticks
    assert not any(
        "still running" in (r.getMessage() or "") for r in caplog.records
    ), "watchdog logged a request that already finished"
