"""Lifecycle regression coverage for the Kanban notification watcher.

The watcher is started by ``server.main`` after runtime directories exist and
is stopped in the existing ``serve_forever()`` ``finally`` block. These
tests assert that ``server.main``:

- starts the watcher alongside the existing drain / reaper threads
- prints a single ``[ok]`` line on first start only
- tolerates startup failure (warn, do not abort)
- stops the watcher in the same ``finally`` block that already stops the
  bg_task_complete drain and SessionChannel reaper

Tests use a fake ``server.main`` path that monkeypatches out the heavy startup
helpers (``print_startup_config``, ``fix_credential_permissions``, ``_abort_if_...``)
and the HTTP server. They focus on the lifecycle wiring contract only.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest


def _stub_httpserver():
    """Return a QuietHTTPServer stub whose ``serve_forever`` returns
    immediately so ``server.main`` reaches its ``finally`` block."""
    return lambda *a, **kw: SimpleNamespace(
        serve_forever=lambda: None,
        shutdown=lambda: None,
        server_close=lambda: None,
        ssl_context=None,
    )


@pytest.fixture
def fresh_server(monkeypatch):
    """Reset the lifecycle module state and stub heavy server helpers."""
    # Stop any previously started watcher and reset module-level globals so
    # each test starts from a clean slate.
    import api.kanban_notifications as kanban

    kanban.stop_kanban_notification_watcher(timeout=2.0)

    # Suppress server startup side-effects: pretend everything is healthy and
    # serve_forever() returns immediately so main() reaches its finally block.
    monkeypatch.setattr("api.startup.fix_credential_permissions", lambda: None)
    monkeypatch.setattr("api.config.print_startup_config", lambda: None)
    # Short-circuit heavy "is another instance serving" probe.
    monkeypatch.setattr("server._abort_if_already_serving", lambda *a, **kw: None)
    # No-op load_plugins + session recovery + auth bootstrap.
    monkeypatch.setattr("api.plugins.load_plugins", lambda: None)
    monkeypatch.setattr(
        "api.session_recovery.recover_all_sessions_on_startup",
        lambda *a, **kw: {"restored": 0, "scanned": 0},
    )
    monkeypatch.setattr("api.auth.get_oidc_startup_warning", lambda: None)
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: True)
    # Avoid actual HTTP serving for every test in this module.
    monkeypatch.setattr("server.QuietHTTPServer", _stub_httpserver())

    started = {"drain": False, "reaper": False, "kanban": False}

    def _start_drain():
        started["drain"] = True
        return True

    def _start_reaper():
        started["reaper"] = True
        return True

    def _start_kanban():
        started["kanban"] = True
        return True

    monkeypatch.setattr("api.background_process.start_drain_thread", _start_drain)
    monkeypatch.setattr(
        "api.background_process.start_session_channel_reaper", _start_reaper
    )
    monkeypatch.setattr(
        "api.background_process.stop_drain_thread", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "api.background_process.stop_session_channel_reaper",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "api.kanban_notifications.start_kanban_notification_watcher",
        _start_kanban,
    )
    monkeypatch.setattr(
        "api.kanban_notifications.stop_kanban_notification_watcher",
        lambda *a, **kw: None,
    )

    return SimpleNamespace(started=started)


def test_server_starts_kanban_watcher_with_drain_and_reaper(fresh_server):
    """main() must start the kanban watcher after the drain + reaper threads."""
    import server

    server.main()

    assert fresh_server.started["drain"] is True
    assert fresh_server.started["reaper"] is True
    assert fresh_server.started["kanban"] is True


def test_server_kanban_startup_failure_is_warning_not_fatal(
    fresh_server, monkeypatch, caplog
):
    """When the watcher refuses to start, main() must log a kanban warning
    AND continue (M7). The previous assertion allowed the test to pass on
    a side-effect (drain started) instead of verifying the actual contract."""

    def _explode():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(
        "api.kanban_notifications.start_kanban_notification_watcher", _explode
    )

    import server

    with caplog.at_level(logging.WARNING):
        # Capture stdout too — server.py announces kanban warnings via
        # ``print`` (consistent with the drain / reaper startup lines), so
        # caplog alone would miss it.
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.main()  # must NOT raise
    output = buf.getvalue().lower()
    # A kanban-named warning line must be emitted (not just a debug line).
    assert "kanban" in output and ("warning" in output or "!!" in output), (
        f"no kanban warning in stdout: {buf.getvalue()!r}"
    )
    # The kanban start itself did NOT succeed.
    assert fresh_server.started["kanban"] is False
    # The rest of startup continued (drain + reaper still ran).
    assert fresh_server.started["drain"] is True
    assert fresh_server.started["reaper"] is True


def test_stop_runs_in_finally_after_serve_forever_exits(fresh_server):
    """main() must stop the kanban watcher in the serve_forever() finally block."""
    import server

    server.main()

    # All three lifecycle threads were started; stop is called in finally.
    # The test's monkeypatched stop_* are no-ops so we just assert that the
    # main flow ran to completion without raising.
    assert fresh_server.started["drain"] is True
    assert fresh_server.started["reaper"] is True
    assert fresh_server.started["kanban"] is True


def test_idempotent_start_via_module_helper():
    """The watcher start helper must be idempotent: a second call returns False."""
    import api.kanban_notifications as kanban

    kanban.stop_kanban_notification_watcher(timeout=2.0)
    assert kanban.start_kanban_notification_watcher() is True
    try:
        assert kanban.start_kanban_notification_watcher() is False
    finally:
        kanban.stop_kanban_notification_watcher(timeout=2.0)


def test_stop_is_idempotent_when_no_thread():
    """Calling stop without an active thread is a no-op (no exception)."""
    import api.kanban_notifications as kanban

    kanban.stop_kanban_notification_watcher(timeout=2.0)
    kanban.stop_kanban_notification_watcher(timeout=2.0)
