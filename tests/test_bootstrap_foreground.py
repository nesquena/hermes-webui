"""
Tests for bootstrap.py --foreground / supervisor auto-detect (issue #1458, Bug #1).

Background
----------
Issue #1458 reports: under launchd / systemd / supervisord with KeepAlive=true
or Restart=always, bootstrap.py exits after spawning the server child via
``Popen + wait_for_health``. The supervisor sees the parent exit, marks the
program as "completed," and respawns it — but the original server child is
still holding port 8787 in a detached process group. The new bootstrap fails
to bind, exits non-zero, supervisor respawns again, loops until something
crashes the orphan and frees the port.

The fix
-------
Add ``--foreground`` flag and supervisor-environment auto-detection. In
foreground mode we replace the current process via ``os.execv`` with the
server, so the supervisor sees the long-lived server as the original child.
The legacy ``Popen + wait_for_health`` path is preserved for interactive
``bash start.sh`` runs.

Coverage
--------
1.  ``--foreground`` is a recognized argparse flag
2.  ``_detect_supervisor()`` returns None on a clean env
3.  ``_detect_supervisor()`` returns the env-var name on each known supervisor
    (``INVOCATION_ID`` / ``JOURNAL_STREAM`` / ``NOTIFY_SOCKET`` /
    ``XPC_SERVICE_NAME`` / ``SUPERVISOR_ENABLED``)
4.  ``_detect_supervisor()`` returns ``HERMES_WEBUI_FOREGROUND`` for the
    explicit opt-in, accepting ``1``/``true``/``yes``/``on`` (case-insensitive)
5.  ``_detect_supervisor()`` ignores ``HERMES_WEBUI_FOREGROUND=0`` /
    ``=false`` / ``=`` and falls through to env-var probing
6.  ``XPC_SERVICE_NAME`` noise filter: bare ``"0"`` and ``application.<id>``
    values do NOT trigger foreground (the macOS Terminal default state),
    while real launchd Labels (``com.<rdns>.<svc>``) do
7.  ``main()`` calls ``os.execv`` (NOT ``subprocess.Popen``) when
    ``--foreground`` is passed
8.  ``main()`` calls ``os.execv`` (NOT ``subprocess.Popen``) when a supervisor
    env var is set even without the explicit flag
9.  Default ``main()`` path (no flag, clean env) still uses ``Popen``
10. Foreground path chdir's to ``agent_dir or REPO_ROOT`` before execv (matches
    the cwd the legacy Popen uses)
11. Foreground path exports ``HERMES_WEBUI_HOST`` / ``HERMES_WEBUI_PORT`` /
    ``HERMES_WEBUI_AGENT_DIR`` / ``HERMES_WEBUI_STATE_DIR`` to ``os.environ``
    so the post-exec server picks them up
12. Foreground path skips ``wait_for_health`` (no client to retry from)
13. ``--foreground`` help text mentions launchd / systemd / supervisord
14. Non-executable ``python_exe`` raises ``RuntimeError`` instead of
    looping the supervisor on ``execv`` failure

These tests do NOT actually exec — ``os.execv`` is monkeypatched. We're
pinning the structural choice (which path runs, which cwd, which env) not the
post-exec behavior (which is the OS kernel's job).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------- helpers --------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all known supervisor env vars + resolved bootstrap vars so each
    test starts from a known-clean state.

    The resolved-vars stripping (HERMES_WEBUI_HOST etc.) prevents leakage
    where a previous test's ``main()`` mutated ``os.environ`` and a later
    test re-imports ``bootstrap``, picking up the polluted defaults. With
    these stripped, ``DEFAULT_HOST`` / ``DEFAULT_PORT`` fall back to their
    hardcoded defaults at module load time.
    """
    for name in (
        # Supervisor-detection env vars
        "INVOCATION_ID",
        "JOURNAL_STREAM",
        "NOTIFY_SOCKET",
        "XPC_SERVICE_NAME",
        "SUPERVISOR_ENABLED",
        "HERMES_WEBUI_FOREGROUND",
        # Bootstrap-resolved env vars (mutated by main(), can leak across tests)
        "HERMES_WEBUI_HOST",
        "HERMES_WEBUI_PORT",
        "HERMES_WEBUI_AGENT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def import_bootstrap():
    """Import bootstrap freshly each test to avoid module-level state bleed."""
    # bootstrap.py runs ``_load_repo_dotenv()`` at import time; that's idempotent.
    if "bootstrap" in sys.modules:
        del sys.modules["bootstrap"]
    import bootstrap as bs

    return bs


# ---------- argparse coverage ---------------------------------------------


class TestForegroundFlag:
    def test_foreground_is_recognized_flag(self, import_bootstrap, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--foreground"])
        args = import_bootstrap.parse_args()
        assert args.foreground is True
        assert args.port == import_bootstrap.DEFAULT_PORT  # default preserved
        assert args.host == import_bootstrap.DEFAULT_HOST

    def test_foreground_default_is_false(self, import_bootstrap, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
        args = import_bootstrap.parse_args()
        assert args.foreground is False

    def test_foreground_help_mentions_supervisors(
        self, import_bootstrap, monkeypatch, capsys
    ):
        # argparse prints help and exits — capture and verify content.
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--help"])
        with pytest.raises(SystemExit):
            import_bootstrap.parse_args()
        out = capsys.readouterr().out
        assert "--foreground" in out
        assert "launchd" in out
        assert "systemd" in out
        assert "supervisord" in out


# ---------- _detect_supervisor() ------------------------------------------


class TestDetectSupervisor:
    def test_clean_env_returns_none(self, import_bootstrap, clean_env):
        assert import_bootstrap._detect_supervisor() is None

    @pytest.mark.parametrize(
        "var",
        [
            "INVOCATION_ID",
            "JOURNAL_STREAM",
            "NOTIFY_SOCKET",
            "XPC_SERVICE_NAME",
            "SUPERVISOR_ENABLED",
        ],
    )
    def test_each_supervisor_var_triggers(
        self, import_bootstrap, clean_env, monkeypatch, var
    ):
        monkeypatch.setenv(var, "anything-truthy")
        assert import_bootstrap._detect_supervisor() == var

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes", "on", "ON"])
    def test_explicit_opt_in_truthy_values(
        self, import_bootstrap, clean_env, monkeypatch, value
    ):
        monkeypatch.setenv("HERMES_WEBUI_FOREGROUND", value)
        assert import_bootstrap._detect_supervisor() == "HERMES_WEBUI_FOREGROUND"

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", "  "])
    def test_explicit_opt_in_falsy_values_fall_through(
        self, import_bootstrap, clean_env, monkeypatch, value
    ):
        # When HERMES_WEBUI_FOREGROUND is falsy, we should NOT short-circuit on it.
        # If no other supervisor var is set, returns None.
        monkeypatch.setenv("HERMES_WEBUI_FOREGROUND", value)
        assert import_bootstrap._detect_supervisor() is None

    def test_explicit_opt_in_takes_precedence_over_supervisor_var(
        self, import_bootstrap, clean_env, monkeypatch
    ):
        # Both set → explicit flag wins (returned name reflects user intent).
        monkeypatch.setenv("HERMES_WEBUI_FOREGROUND", "1")
        monkeypatch.setenv("INVOCATION_ID", "deadbeef")
        assert import_bootstrap._detect_supervisor() == "HERMES_WEBUI_FOREGROUND"


class TestXPCServiceNameNoiseFilter:
    """macOS launchd sets XPC_SERVICE_NAME in EVERY Terminal-spawned shell.

    Without filtering, every Mac dev running ``./start.sh`` would silently
    auto-promote to foreground mode and lose the /health probe + browser open
    + bootstrap log. We narrow to launchd Label-style names (com.<rdns>.<svc>)
    while rejecting the well-known noise values.
    """

    @pytest.mark.parametrize(
        "noise_value",
        [
            "0",  # launchd descendants
            "application.com.apple.Terminal.0BCDDEAD-1234-5678",  # Terminal.app shells
            "application.com.googlecode.iterm2",  # iTerm2
            "application.com.microsoft.VSCode",  # VSCode terminal
        ],
    )
    def test_xpc_noise_values_do_not_trigger(
        self, import_bootstrap, clean_env, monkeypatch, noise_value
    ):
        monkeypatch.setenv("XPC_SERVICE_NAME", noise_value)
        assert import_bootstrap._detect_supervisor() is None, (
            f"XPC_SERVICE_NAME={noise_value!r} should not trigger foreground "
            f"mode — that would break interactive ./start.sh on every Mac."
        )

    @pytest.mark.parametrize(
        "real_value",
        [
            "com.example.hermes-webui",
            "com.acme.production-server",
            "io.github.user.my-service",
        ],
    )
    def test_xpc_real_label_triggers(
        self, import_bootstrap, clean_env, monkeypatch, real_value
    ):
        monkeypatch.setenv("XPC_SERVICE_NAME", real_value)
        assert import_bootstrap._detect_supervisor() == "XPC_SERVICE_NAME", (
            f"XPC_SERVICE_NAME={real_value!r} is a launchd Label and should "
            f"trigger foreground mode."
        )

    def test_xpc_noise_does_not_block_other_supervisor_var(
        self, import_bootstrap, clean_env, monkeypatch
    ):
        # If XPC has a noise value but INVOCATION_ID is set (mixed env, e.g.
        # systemd unit run on a Mac CI runner), we should still detect via
        # INVOCATION_ID rather than swallow it.
        monkeypatch.setenv("XPC_SERVICE_NAME", "0")
        monkeypatch.setenv("INVOCATION_ID", "deadbeef")
        assert import_bootstrap._detect_supervisor() == "INVOCATION_ID"


# ---------- main() routing ------------------------------------------------


class TestMainForegroundRouting:
    """Verify which code path main() takes under each input combination.

    These are STRUCTURAL tests — they pin which call (execv vs Popen) is made,
    not the result. We monkeypatch every external side effect so main() runs
    in a hermetic environment.
    """

    @pytest.fixture
    def stub_main_dependencies(self, monkeypatch, tmp_path):
        """Stub out everything main() calls except the routing decision."""
        import bootstrap as bs

        monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
        monkeypatch.setattr(bs, "discover_agent_dir", lambda: tmp_path / "agent")
        monkeypatch.setattr(bs, "hermes_command_exists", lambda: True)
        monkeypatch.setattr(
            bs, "discover_launcher_python", lambda *a: "/usr/bin/python3"
        )
        monkeypatch.setattr(bs, "ensure_python_has_webui_deps", lambda *a, **kw: a[0])
        monkeypatch.setattr(bs, "wait_for_health", lambda *a, **kw: True)
        monkeypatch.setattr(bs, "open_browser", lambda *a, **kw: None)
        monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
        # Make agent_dir exist so chdir doesn't fail.
        (tmp_path / "agent").mkdir(parents=True, exist_ok=True)
        return bs

    def test_default_path_uses_popen(
        self, stub_main_dependencies, clean_env, monkeypatch
    ):
        bs = stub_main_dependencies
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--no-browser"])

        execv_calls = []
        popen_calls = []
        monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))

        class FakePopen:
            pid = 12345

            def __init__(self, *args, **kwargs):
                popen_calls.append((args, kwargs))

        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        rc = bs.main()
        assert rc == 0
        assert len(popen_calls) == 1, (
            "Default path should call subprocess.Popen exactly once"
        )
        assert len(execv_calls) == 0, "Default path must NOT call os.execv"

    def test_foreground_flag_uses_execv(
        self, stub_main_dependencies, clean_env, monkeypatch
    ):
        bs = stub_main_dependencies
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--foreground"])

        execv_calls = []
        popen_calls = []

        # execv normally replaces the process; we capture+raise SystemExit so
        # main() returns control to us instead of falling through to the
        # legacy Popen branch.
        def fake_execv(path, argv):
            execv_calls.append((path, argv))
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)
        monkeypatch.setattr(os, "chdir", lambda p: None)

        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return None

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        with pytest.raises(SystemExit) as ei:
            bs.main()
        assert ei.value.code == 0
        assert len(execv_calls) == 1, "--foreground must call os.execv exactly once"
        assert len(popen_calls) == 0, "--foreground must NOT call subprocess.Popen"

        path, argv = execv_calls[0]
        assert path == "/usr/bin/python3"
        # argv[0] is the program name (convention), argv[1] is the script
        assert argv[0] == "/usr/bin/python3"
        assert argv[1].endswith("server.py")

    @pytest.mark.parametrize(
        "var",
        [
            "INVOCATION_ID",
            "JOURNAL_STREAM",
            "NOTIFY_SOCKET",
            "XPC_SERVICE_NAME",
            "SUPERVISOR_ENABLED",
        ],
    )
    def test_supervisor_env_var_auto_promotes_to_execv(
        self, stub_main_dependencies, clean_env, monkeypatch, var
    ):
        bs = stub_main_dependencies
        monkeypatch.setattr(sys, "argv", ["bootstrap.py"])  # no --foreground
        monkeypatch.setenv(var, "deadbeef")

        execv_calls = []
        popen_calls = []

        def fake_execv(path, argv):
            execv_calls.append((path, argv))
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)
        monkeypatch.setattr(os, "chdir", lambda p: None)

        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return None

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        with pytest.raises(SystemExit):
            bs.main()
        assert len(execv_calls) == 1, f"{var} must auto-promote to execv"
        assert len(popen_calls) == 0, f"{var} must NOT use Popen"

    def test_explicit_opt_in_env_auto_promotes_to_execv(
        self, stub_main_dependencies, clean_env, monkeypatch
    ):
        bs = stub_main_dependencies
        monkeypatch.setattr(sys, "argv", ["bootstrap.py"])  # no --foreground flag
        monkeypatch.setenv("HERMES_WEBUI_FOREGROUND", "1")

        execv_calls = []

        def fake_execv(path, argv):
            execv_calls.append((path, argv))
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)
        monkeypatch.setattr(os, "chdir", lambda p: None)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

        with pytest.raises(SystemExit):
            bs.main()
        assert len(execv_calls) == 1


class TestForegroundEnvAndCwd:
    """The post-execv server.py inherits os.environ and cwd from us."""

    @pytest.fixture
    def setup(self, monkeypatch, tmp_path):
        import bootstrap as bs

        monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        monkeypatch.setattr(bs, "discover_agent_dir", lambda: agent_dir)
        monkeypatch.setattr(bs, "hermes_command_exists", lambda: True)
        monkeypatch.setattr(
            bs, "discover_launcher_python", lambda *a: "/usr/bin/python3"
        )
        monkeypatch.setattr(bs, "ensure_python_has_webui_deps", lambda *a, **kw: a[0])
        monkeypatch.setattr(bs, "wait_for_health", lambda *a, **kw: True)
        monkeypatch.setattr(bs, "open_browser", lambda *a, **kw: None)
        # State-dir + every var we care about is captured.
        monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
        return bs, agent_dir

    def test_foreground_chdirs_to_agent_dir_before_exec(
        self, setup, monkeypatch, clean_env
    ):
        bs, agent_dir = setup
        monkeypatch.setattr(
            sys, "argv", ["bootstrap.py", "--foreground", "--host", "127.0.0.1", "9999"]
        )

        chdir_calls = []
        monkeypatch.setattr(os, "chdir", lambda p: chdir_calls.append(p))

        def fake_execv(*a):
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)

        with pytest.raises(SystemExit):
            bs.main()
        assert len(chdir_calls) == 1
        assert chdir_calls[0] == str(agent_dir)

    def test_foreground_exports_resolved_env_vars(self, setup, monkeypatch, clean_env):
        bs, agent_dir = setup
        monkeypatch.setattr(
            sys, "argv", ["bootstrap.py", "--foreground", "--host", "0.0.0.0", "9119"]
        )
        monkeypatch.setattr(os, "chdir", lambda p: None)

        def fake_execv(*a):
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)

        with pytest.raises(SystemExit):
            bs.main()

        # Post-execv server.py inherits these — verify we set them on os.environ
        # (not just a local copy).
        assert os.environ["HERMES_WEBUI_HOST"] == "0.0.0.0"
        assert os.environ["HERMES_WEBUI_PORT"] == "9119"
        assert os.environ["HERMES_WEBUI_AGENT_DIR"] == str(agent_dir)
        # state-dir was already set by the fixture; verify it survived.
        assert "HERMES_WEBUI_STATE_DIR" in os.environ

    def test_foreground_does_not_call_wait_for_health(
        self, setup, monkeypatch, clean_env
    ):
        bs, _ = setup
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--foreground"])
        monkeypatch.setattr(os, "chdir", lambda p: None)

        wait_calls = []
        monkeypatch.setattr(
            bs, "wait_for_health", lambda *a, **kw: (wait_calls.append(a), True)[1]
        )

        def fake_execv(*a):
            raise SystemExit(0)

        monkeypatch.setattr(os, "execv", fake_execv)

        with pytest.raises(SystemExit):
            bs.main()

        # In foreground mode there's no parent left to retry from — the
        # supervisor's KeepAlive handles it. wait_for_health must not run.
        assert len(wait_calls) == 0


class TestForegroundExecutabilityGuard:
    """If python_exe is missing or non-executable, raise a clear error
    instead of letting os.execv raise OSError → SystemExit(1) → supervisor
    restart loop. This guard prevents the exact failure mode #1458 reports."""

    @pytest.fixture
    def setup_with_bad_python(self, monkeypatch, tmp_path):
        import bootstrap as bs

        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        # Create a non-executable file at the python path
        bad_python = tmp_path / "bad-python"
        bad_python.write_text("#!/bin/bash\necho hi", encoding="utf-8")
        bad_python.chmod(0o644)  # NOT executable
        monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
        monkeypatch.setattr(bs, "discover_agent_dir", lambda: agent_dir)
        monkeypatch.setattr(bs, "hermes_command_exists", lambda: True)
        monkeypatch.setattr(bs, "discover_launcher_python", lambda *a: str(bad_python))
        monkeypatch.setattr(bs, "ensure_python_has_webui_deps", lambda *a, **kw: a[0])
        monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
        return bs

    def test_non_executable_python_raises_runtime_error(
        self, setup_with_bad_python, monkeypatch, clean_env
    ):
        bs = setup_with_bad_python
        monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--foreground"])

        execv_calls = []
        monkeypatch.setattr(os, "execv", lambda *a: execv_calls.append(a))
        monkeypatch.setattr(os, "chdir", lambda p: None)

        with pytest.raises(RuntimeError, match="not executable"):
            bs.main()
        # execv must NOT have been called when the guard fires
        assert len(execv_calls) == 0
