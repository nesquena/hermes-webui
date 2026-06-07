"""Tests for the test-server boot reliability helpers in conftest.py.

These guard the diagnostics that turn an opaque "test server did not start"
timeout (which previously cascaded into hundreds of ConnectionRefused failures
with no clue as to the cause) into a single actionable failure:

  * ``_wait_for_server`` returns ``(ok, reason)`` and fails FAST when the server
    subprocess has already exited, instead of polling out the whole timeout.
  * ``_server_boot_diagnostic`` appends the tail of the captured server log so
    an import error / bind failure / traceback is visible in the failure.
"""
from __future__ import annotations

import tests.conftest as conftest


class _FakeProc:
    """Minimal stand-in for subprocess.Popen with a controllable poll()."""

    def __init__(self, returncode=None):
        self._returncode = returncode

    def poll(self):
        return self._returncode

    @property
    def returncode(self):
        return self._returncode


def test_wait_for_server_fails_fast_on_early_exit(tmp_path):
    """A dead subprocess must short-circuit the wait, not poll the full timeout."""
    log = tmp_path / "server.log"
    log.write_text("Traceback (most recent call last):\nImportError: boom\n", encoding="utf-8")
    dead = _FakeProc(returncode=1)

    # Point at a port nothing is listening on; with proc dead we must return
    # almost immediately rather than waiting out the (large) timeout.
    import time
    start = time.time()
    ok, reason = conftest._wait_for_server(
        "http://127.0.0.1:9", timeout=30, proc=dead, log_path=str(log)
    )
    elapsed = time.time() - start

    assert ok is False
    assert elapsed < 5, f"should fail fast on early exit, took {elapsed:.1f}s"
    assert "exited early with code 1" in reason
    # The captured server output must be surfaced in the diagnostic.
    assert "ImportError: boom" in reason


def test_wait_for_server_times_out_with_log_tail(tmp_path):
    """When the process stays alive but never serves, surface the log tail."""
    log = tmp_path / "server.log"
    log.write_text("starting up...\nstill binding...\n", encoding="utf-8")
    alive = _FakeProc(returncode=None)

    ok, reason = conftest._wait_for_server(
        "http://127.0.0.1:9", timeout=1, proc=alive, log_path=str(log)
    )

    assert ok is False
    assert "timed out" in reason
    assert "still binding" in reason


def test_server_boot_diagnostic_handles_missing_log():
    """A missing/None log path must not raise — diagnostics are best-effort."""
    msg = conftest._server_boot_diagnostic("headline only", None)
    assert msg == "headline only"

    msg2 = conftest._server_boot_diagnostic("hl", "/nonexistent/path/12345.log")
    assert "hl" in msg2  # does not raise; appends a soft note


def test_server_boot_diagnostic_reports_empty_output(tmp_path):
    """An empty server log should say so rather than appear truncated."""
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    msg = conftest._server_boot_diagnostic("hl", str(log))
    assert "no output" in msg


def test_self_heal_respawns_killed_server_mid_session(base_url):
    """Killing the shared test server mid-session must NOT cascade.

    Reproduces the exact failure that motivated the self-heal + new-session
    isolation: the shared server dies partway through the suite (here we kill it
    outright), and every subsequent HTTP-dependent test would otherwise fail with
    ConnectionRefused. The autouse `_ensure_test_server_alive` fixture must detect
    the dead server and respawn it before the next test, so this test (which
    depends on `base_url`) still gets a live server.

    NOTE: this test deliberately kills the session server. The autouse fixture
    runs at the START of each test, so by the time THIS test body runs the server
    is already healthy; we kill it, then assert the NEXT request after a manual
    heal succeeds — mirroring what the fixture does for the following test.
    """
    import json as _json
    import urllib.request as _ur
    import tests.conftest as _c

    # Sanity: server is up right now (the autouse fixture guarantees it).
    with _ur.urlopen(base_url + "/health", timeout=5) as r:
        assert _json.loads(r.read()).get("status") == "ok"

    # Kill it the way an external group-signal would (hard kill the process).
    proc = _c._TEST_SERVER_PROC
    assert proc is not None
    proc.kill()
    proc.wait(timeout=5)
    assert proc.poll() is not None  # confirmed dead

    # Invoke the same heal path the autouse fixture uses for the next test.
    new_proc, reason = _c._spawn_test_server(boot_attempts=2)
    assert new_proc is not None, f"respawn failed: {reason}"
    _c._TEST_SERVER_PROC = new_proc

    # The respawned server serves requests again — no cascade.
    with _ur.urlopen(base_url + "/health", timeout=10) as r:
        assert _json.loads(r.read()).get("status") == "ok"
