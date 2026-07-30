"""Tests for native Windows support (terminal.py POSIX guards + bootstrap.py unblock).

terminal.py guards:
- _TERMINAL_SUPPORTED is False on Windows, True on POSIX
- Terminal functions raise NotImplementedError on Windows
- close_terminal returns False (no-op) on Windows
- get_terminal returns None on Windows
- Module imports cleanly on Windows (no fcntl/termios ImportError)

bootstrap.py unblock:
- ensure_supported_platform does not raise on Windows
- install_hermes_agent raises RuntimeError on Windows (calls /bin/bash)
- Foreground path uses Popen + sys.exit on Windows instead of os.execv
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ── terminal.py tests ────────────────────────────────────────────────────────


class TestTerminalPosixGuard:
    """Verify _TERMINAL_SUPPORTED flag and import guards."""

    def test_terminal_supported_matches_platform(self):
        from api import terminal
        if sys.platform == "win32":
            assert not terminal._TERMINAL_SUPPORTED
        else:
            assert terminal._TERMINAL_SUPPORTED

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only check")
    def test_posix_modules_are_loaded(self):
        from api import terminal
        assert terminal.fcntl is not None
        assert terminal.select is not None
        assert terminal.termios is not None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only check")
    def test_windows_modules_are_none(self):
        from api import terminal
        assert terminal.fcntl is None
        assert terminal.select is None
        assert terminal.termios is None


class TestTerminalWindowsFunctions:
    """Terminal functions should fail gracefully on Windows."""

    @pytest.fixture(autouse=True)
    def _force_unsupported(self, monkeypatch):
        """Force _TERMINAL_SUPPORTED=False regardless of actual platform."""
        from api import terminal
        monkeypatch.setattr(terminal, "_TERMINAL_SUPPORTED", False)

    def test_start_terminal_raises(self, tmp_path):
        from api.terminal import start_terminal
        with pytest.raises(NotImplementedError, match="not supported on Windows"):
            start_terminal("test-session", tmp_path)

    def test_write_terminal_raises(self):
        from api.terminal import write_terminal
        with pytest.raises(NotImplementedError, match="not supported on Windows"):
            write_terminal("test-session", "hello")

    def test_resize_terminal_raises(self):
        from api.terminal import resize_terminal
        with pytest.raises(NotImplementedError, match="not supported on Windows"):
            resize_terminal("test-session", 24, 80)

    def test_close_terminal_returns_false(self):
        from api.terminal import close_terminal
        assert close_terminal("test-session") is False

    def test_get_terminal_returns_none(self):
        from api.terminal import get_terminal
        assert get_terminal("test-session") is None


# ── bootstrap.py tests ───────────────────────────────────────────────────────


@pytest.fixture
def import_bootstrap():
    """Import bootstrap freshly to avoid module-level state bleed."""
    if "bootstrap" in sys.modules:
        del sys.modules["bootstrap"]
    import bootstrap as bs
    return bs


class TestBootstrapWindowsUnblock:

    def test_ensure_supported_platform_does_not_raise_on_windows(self, import_bootstrap, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(import_bootstrap, "is_wsl", lambda: False)
        # Should print a warning, not raise
        import_bootstrap.ensure_supported_platform()

    def test_ensure_supported_platform_still_passes_on_posix(self, import_bootstrap, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        import_bootstrap.ensure_supported_platform()

    def test_install_hermes_agent_raises_on_windows(self, import_bootstrap, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(import_bootstrap, "is_wsl", lambda: False)
        with pytest.raises(RuntimeError, match="not supported on native Windows"):
            import_bootstrap.install_hermes_agent()

    def test_install_hermes_agent_allowed_in_wsl(self, import_bootstrap, monkeypatch):
        """WSL should still be able to auto-install (it has /bin/bash) — the
        Windows guard must NOT fire. Stub subprocess.run so the test proves the
        guard was passed without actually launching the real installer."""
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr(import_bootstrap, "is_wsl", lambda: True)
        run_calls = []
        monkeypatch.setattr(
            import_bootstrap.subprocess, "run",
            lambda *a, **kw: run_calls.append((a, kw)),
        )
        # Must NOT raise the native-Windows guard; instead it reaches the
        # (stubbed) install subprocess.
        import_bootstrap.install_hermes_agent()
        assert len(run_calls) == 1, "WSL install should reach the install subprocess"


class TestBootstrapForegroundWindows:
    """Foreground path uses Popen + sys.exit on Windows instead of os.execv."""

    @pytest.fixture
    def stub_main_dependencies(self, monkeypatch, tmp_path):
        import bootstrap as bs
        monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
        monkeypatch.setattr(bs, "discover_agent_dir", lambda: tmp_path / "agent")
        monkeypatch.setattr(bs, "hermes_command_exists", lambda: True)
        python_exe = sys.executable
        monkeypatch.setattr(bs, "discover_launcher_python", lambda *a: python_exe)
        monkeypatch.setattr(bs, "ensure_python_has_webui_deps", lambda *a, **kw: a[0])
        monkeypatch.setattr(bs, "wait_for_health", lambda *a, **kw: True)
        monkeypatch.setattr(bs, "open_browser", lambda *a, **kw: None)
        monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
        (tmp_path / "agent").mkdir(parents=True, exist_ok=True)
        return bs

    def test_foreground_uses_popen_on_windows(self, stub_main_dependencies, monkeypatch):
        """On win32, foreground mode should use Popen + sys.exit, not os.execv."""
        bs = stub_main_dependencies
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--foreground"])
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(os, "chdir", lambda p: None)

        # Strip supervisor env vars that would interfere
        for var in ("INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET",
                    "XPC_SERVICE_NAME", "SUPERVISOR_ENABLED", "HERMES_WEBUI_FOREGROUND"):
            monkeypatch.delenv(var, raising=False)

        popen_calls = []
        execv_calls = []

        class FakePopen:
            pid = 12345
            def __init__(self, *args, **kwargs):
                popen_calls.append((args, kwargs))

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))

        with pytest.raises(SystemExit) as ei:
            bs.main()
        assert ei.value.code == 0
        assert len(popen_calls) == 1, "Windows foreground should use Popen"
        assert len(execv_calls) == 0, "Windows foreground must NOT use execv"

    def test_foreground_uses_execv_on_posix(self, stub_main_dependencies, monkeypatch):
        """On POSIX, foreground mode should still use os.execv."""
        bs = stub_main_dependencies
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--foreground"])
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "chdir", lambda p: None)

        for var in ("INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET",
                    "XPC_SERVICE_NAME", "SUPERVISOR_ENABLED", "HERMES_WEBUI_FOREGROUND"):
            monkeypatch.delenv(var, raising=False)

        execv_calls = []
        popen_calls = []

        def fake_execv(path, argv):
            execv_calls.append((path, argv))
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)

        class FakePopen:
            pid = 12345
            def __init__(self, *args, **kwargs):
                popen_calls.append((args, kwargs))

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        with pytest.raises(SystemExit) as ei:
            bs.main()
        assert ei.value.code == 0
        assert len(execv_calls) == 1, "POSIX foreground should use execv"
        assert len(popen_calls) == 0, "POSIX foreground must NOT use Popen"
