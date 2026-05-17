"""Regression guard for the pytest "hangs at 99% then restarts from 0%" loop.

Root cause documented in tests/conftest.py — daemon threads spawned by
api.updates._schedule_restart() can fire os.execv() AFTER monkeypatch
teardown restores the real os.execv, which re-execs the entire pytest
process. The conftest installs a permanent no-op wrapper on os.execv that
shadows any late-firing daemon thread.

This test pins the guard so a future conftest refactor can't silently
remove it.
"""
import os


def test_conftest_installs_permanent_execv_guard():
    """os.execv must be replaced by the conftest's safe no-op wrapper."""
    # The wrapper is named `_pytest_session_safe_execv` in conftest.py.
    # Verify the module attribute now points to that wrapper, not the real
    # libc-bound function.
    assert os.execv.__name__ == '_pytest_session_safe_execv', (
        f"os.execv must be the conftest-installed pytest-safe no-op, but "
        f"resolves to {os.execv!r}. Did a recent conftest refactor remove "
        f"the guard? See conftest.py § 'Permanent os.execv guard for the "
        f"pytest session' — without it, late-firing _schedule_restart "
        f"daemon threads re-exec pytest and the suite loops forever."
    )


def test_safe_execv_returns_none_does_not_exec():
    """The wrapper must be a true no-op — it must not raise, exec, or block."""
    # Pass deliberately bogus args to confirm the wrapper drops them rather
    # than passing them through to the real execv.
    result = os.execv('/nonexistent/binary/path/that/should/not/be/executed',
                      ['/nonexistent/binary/path/that/should/not/be/executed'])
    assert result is None
