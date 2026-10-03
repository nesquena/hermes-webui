"""HERMES_DISABLE_LAZY_INSTALLS is a boundary override, not a process setting.

hermes-agent's hermes_bootstrap.py relaunches the process (os.execv into its
managed sandbox via venv_sync.relaunch_command) the moment it is imported with a
pending lazy install/update. That sandbox has no WebUI dependencies, so an
unguarded Agent import replaces server.py with a process that dies on
``import yaml`` and systemd restarts it forever.

The same variable is read by pm/install.py as an unconditional override of
``security.allow_lazy_installs``. A process-wide value therefore outlives
startup and silently disables on-demand installs the operator allowed. These
tests pin the scoped contract: the override covers the Agent imports that can
still relaunch (startup activation, plus the first-chat run_agent import while
the launch layer is not loaded yet), overlapping boundaries cannot interleave
their restoration, and the operator's own value -- set, unset or ``0`` -- is
back immediately after.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap.py"
AGENT_RUNTIME = REPO_ROOT / "api" / "agent_runtime.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from managed_agent_startup import (  # noqa: E402
    LAUNCH_LAYER_MODULE,
    LAZY_INSTALL_GUARD,
    agent_import_boundary,
)


# The Agent's launch layer: with a pending lazy install/update it replaces this
# process with its sandbox interpreter. Stand in for that sandbox with a process
# that carries none of the WebUI dependencies.
FAKE_HERMES_BOOTSTRAP = """
import os
import sys

if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS", "").strip().lower() not in {"1", "true", "yes"}:
    sys.stderr.write("SANDBOX-RELAUNCH" + chr(10))
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-c", "raise SystemExit(97)"])
"""

# Real run_agent.py imports hermes_bootstrap first. Under the composed startup
# below that module is already cached, so its launch layer cannot run again; the
# not-loaded case is pinned separately in section 4.
FAKE_RUN_AGENT = """
import hermes_bootstrap  # noqa: F401  (cached during startup: no relaunch runs)


class AIAgent:
    pass
"""

COMPOSED_STARTUP_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path[0] = {repo_root!r}
    import managed_agent_startup as mas
    mas.activate_managed_agent()
    assert "hermes_bootstrap" in sys.modules, "startup activation skipped the Agent launch layer"
    from api.agent_runtime import get_ai_agent_class
    assert get_ai_agent_class() is not None, "first-chat Agent import unavailable"
    import os
    print("AFTER=" + repr(os.environ.get({guard!r})))
    """
)


def _fake_agent_dir(tmp_path: Path) -> Path:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "hermes_bootstrap.py").write_text(FAKE_HERMES_BOOTSTRAP, encoding="utf-8")
    (agent_dir / "run_agent.py").write_text(FAKE_RUN_AGENT, encoding="utf-8")
    return agent_dir


# ---------- 1. the boundary override itself ---------------------------------


@pytest.fixture
def launch_layer_unloaded(monkeypatch):
    """A process that has not imported the Agent launch layer yet."""
    monkeypatch.delitem(sys.modules, LAUNCH_LAYER_MODULE, raising=False)


def test_boundary_sets_one_and_restores_operator_value(
    launch_layer_unloaded, monkeypatch
):
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "0")
    with agent_import_boundary():
        assert os.environ[LAZY_INSTALL_GUARD] == "1"
    assert os.environ[LAZY_INSTALL_GUARD] == "0"


def test_boundary_preserves_operator_opt_out(launch_layer_unloaded, monkeypatch):
    """An operator who disabled lazy installs keeps that setting."""
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "1")
    with agent_import_boundary():
        assert os.environ[LAZY_INSTALL_GUARD] == "1"
    assert os.environ[LAZY_INSTALL_GUARD] == "1"


def test_boundary_leaves_unset_when_operator_had_nothing(
    launch_layer_unloaded, monkeypatch
):
    monkeypatch.delenv(LAZY_INSTALL_GUARD, raising=False)
    with agent_import_boundary():
        assert os.environ[LAZY_INSTALL_GUARD] == "1"
    assert LAZY_INSTALL_GUARD not in os.environ


def test_boundary_restores_after_failed_import(launch_layer_unloaded, monkeypatch):
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "0")
    with pytest.raises(ImportError):
        with agent_import_boundary():
            raise ImportError("agent import failed")
    assert os.environ[LAZY_INSTALL_GUARD] == "0"


def test_boundary_is_a_no_op_once_the_launch_layer_is_loaded(monkeypatch):
    """After startup activation every later boundary has nothing to intercept."""
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "0")
    monkeypatch.setitem(
        sys.modules, LAUNCH_LAYER_MODULE, types.ModuleType(LAUNCH_LAYER_MODULE)
    )
    with agent_import_boundary():
        assert os.environ[LAZY_INSTALL_GUARD] == "0"
    assert os.environ[LAZY_INSTALL_GUARD] == "0"


@pytest.mark.parametrize("operator_value", [None, "0", "1"])
def test_overlapping_boundaries_cannot_interleave_their_restoration(
    launch_layer_unloaded, monkeypatch, operator_value
):
    """Two threads inside the boundary: the guard has to survive until the last exit.

    Unserialized, the first exit removes (or restores over) the value the second
    boundary still relies on, and the second exit leaves the override set for the
    rest of the process lifetime -- the in-process regression this scoping exists
    to remove.
    """
    if operator_value is None:
        monkeypatch.delenv(LAZY_INSTALL_GUARD, raising=False)
    else:
        monkeypatch.setenv(LAZY_INSTALL_GUARD, operator_value)

    first_inside = threading.Event()
    second_attempting = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()
    release_second = threading.Event()

    def first_boundary():
        with agent_import_boundary():
            first_inside.set()
            release_first.wait(10)

    def second_boundary():
        assert first_inside.wait(10)
        second_attempting.set()
        with agent_import_boundary():
            second_inside.set()
            release_second.wait(10)

    first = threading.Thread(target=first_boundary, daemon=True)
    second = threading.Thread(target=second_boundary, daemon=True)
    first.start()
    assert first_inside.wait(10), "first boundary never entered"
    second.start()
    assert second_attempting.wait(10), "second boundary never attempted entry"
    # An unserialized second boundary records its own 'previous' right here.
    time.sleep(0.05)
    release_first.set()
    first.join(10)
    assert not first.is_alive(), "first boundary never exited"
    assert second_inside.wait(10), "second boundary never entered"
    assert os.environ.get(LAZY_INSTALL_GUARD) == "1", (
        "guard dropped while a boundary was still importing: "
        + repr(os.environ.get(LAZY_INSTALL_GUARD))
    )
    release_second.set()
    second.join(10)
    assert not second.is_alive(), "second boundary never exited"
    if operator_value is None:
        assert LAZY_INSTALL_GUARD not in os.environ
    else:
        assert os.environ[LAZY_INSTALL_GUARD] == operator_value


# ---------- 2. bootstrap never exports the override --------------------------


def test_probe_gets_the_guard_without_mutating_process_env(tmp_path, monkeypatch):
    import bootstrap as bs

    monkeypatch.delenv(LAZY_INSTALL_GUARD, raising=False)
    seen = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["env"] = dict(kwargs.get("env") or {})
        return FakeCompleted()

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    agent_dir = _fake_agent_dir(tmp_path)
    assert bs._python_can_run_webui_and_agent(sys.executable, agent_dir) is True
    assert seen["env"][LAZY_INSTALL_GUARD] == "1", "probe interpreter must be guarded"
    assert LAZY_INSTALL_GUARD not in os.environ, "bootstrap must not export the override"


def _import_bootstrap_env(bootstrap_path: Path, cwd: Path) -> dict:
    env = os.environ.copy()
    env.pop(LAZY_INSTALL_GUARD, None)
    env["HERMES_WEBUI_DOTENV_CANARY"] = "unset"
    nl = chr(10)
    script = nl.join(
        [
            "import importlib.util, json, os",
            "spec = importlib.util.spec_from_file_location('bootstrap', "
            + repr(str(bootstrap_path))
            + ")",
            "spec.loader.exec_module(importlib.util.module_from_spec(spec))",
            "print(json.dumps({k: os.environ.get(k) for k in ("
            + repr(LAZY_INSTALL_GUARD)
            + ", 'HERMES_WEBUI_DOTENV_CANARY')}))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env, cwd=str(cwd)
    )
    assert result.returncode == 0, result.stderr
    import json

    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_bootstrap_does_not_export_the_override():
    seen = _import_bootstrap_env(BOOTSTRAP, REPO_ROOT)
    assert seen[LAZY_INSTALL_GUARD] is None, seen


def test_repo_dotenv_value_is_left_alone(tmp_path):
    """A .env entry is the operator's setting; bootstrap must not rewrite it."""
    copied = tmp_path / "bootstrap.py"
    copied.write_text(BOOTSTRAP.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "managed_agent_startup.py").write_text(
        (REPO_ROOT / "managed_agent_startup.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        LAZY_INSTALL_GUARD + "=0" + chr(10) + "HERMES_WEBUI_DOTENV_CANARY=from-dotenv" + chr(10),
        encoding="utf-8",
    )
    seen = _import_bootstrap_env(copied, tmp_path)
    assert seen["HERMES_WEBUI_DOTENV_CANARY"] == "from-dotenv", seen
    assert seen[LAZY_INSTALL_GUARD] == "0", seen


# ---------- 3. launcher paths hand the operator env to the server ------------


@pytest.fixture
def stub_main_dependencies(monkeypatch, tmp_path):
    import bootstrap as bs

    monkeypatch.setattr(bs, "ensure_supported_platform", lambda: None)
    monkeypatch.setattr(bs, "discover_agent_dir", lambda: tmp_path / "agent")
    monkeypatch.setattr(bs, "hermes_command_exists", lambda: True)
    monkeypatch.setattr(bs, "discover_launcher_python", lambda *a: sys.executable)
    monkeypatch.setattr(bs, "ensure_python_has_webui_deps", lambda *a, **kw: a[0])
    monkeypatch.setattr(bs, "wait_for_health", lambda *a, **kw: True)
    monkeypatch.setattr(bs, "open_browser", lambda *a, **kw: None)
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "agent").mkdir(parents=True, exist_ok=True)
    return bs


@pytest.mark.parametrize("mode", ["foreground", "supervisor", "detached"])
def test_launch_paths_hand_the_operator_env_to_the_server(
    stub_main_dependencies, monkeypatch, mode
):
    """Foreground (execv), supervisor-auto and detached (Popen) alike."""
    bs = stub_main_dependencies
    monkeypatch.delenv(LAZY_INSTALL_GUARD, raising=False)
    for name in ("INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "SUPERVISOR_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    argv = ["bootstrap.py", "--no-browser"]
    if mode == "foreground":
        argv.append("--foreground")
    elif mode == "supervisor":
        monkeypatch.setenv("INVOCATION_ID", "test-invocation")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "platform", "linux")

    captured = {}

    def fake_execv(path, child_argv):
        captured["execv_env"] = dict(os.environ)
        raise SystemExit(0)

    class FakePopen:
        pid = 4242

        def __init__(self, *args, **kwargs):
            captured["popen_env"] = dict(kwargs.get("env") or {})

    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setattr(os, "chdir", lambda path: None)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    try:
        assert bs.main() == 0
    except SystemExit as exc:
        assert exc.code == 0

    launched = captured.get("execv_env") or captured.get("popen_env")
    assert launched is not None, "no launch path ran"
    assert LAZY_INSTALL_GUARD not in launched, launched
    env = captured.get("popen_env")
    if env is not None:
        assert env.get(LAZY_INSTALL_GUARD) is None
    # A genuinely launched server starts with the operator's own environment.
    assert LAZY_INSTALL_GUARD not in os.environ


# ---------- 4. composed start proves both properties -------------------------


@pytest.mark.parametrize("operator_value", [None, "0", "1"])
def test_composed_startup_keeps_the_agent_interception_out_of_the_app(
    tmp_path, operator_value
):
    """Real startup seam + real first-chat seam, fake Agent launch layer.

    Property (a): neither import boundary switches the process into an
    interpreter without WebUI dependencies (the fake launch layer would exec
    one). Property (b): once startup is done the process carries the operator's
    own value, so pm/install.py still honours security.allow_lazy_installs.
    """
    agent_dir = _fake_agent_dir(tmp_path)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop(LAZY_INSTALL_GUARD, None)
    if operator_value is not None:
        env[LAZY_INSTALL_GUARD] = operator_value
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            COMPOSED_STARTUP_SCRIPT.format(repo_root=str(REPO_ROOT), guard=LAZY_INSTALL_GUARD),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "SANDBOX-RELAUNCH" not in result.stderr, result.stderr
    assert "UNGUARDED-AGENT-IMPORT" not in result.stderr, result.stderr
    assert ("AFTER=" + repr(operator_value)) in result.stdout, result.stdout


COMPOSED_FIRST_CHAT_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path[0] = {repo_root!r}
    sys.path.insert(1, {agent_dir!r})
    import managed_agent_startup as mas
    assert "hermes_bootstrap" not in sys.modules, "launch layer loaded too early"
    with mas.agent_import_boundary():
        import hermes_bootstrap
    import os
    print("AFTER=" + repr(os.environ.get({guard!r})))
    """
)


@pytest.mark.parametrize("operator_value", [None, "0", "1"])
def test_first_chat_import_is_guarded_while_the_launch_layer_is_not_loaded(
    tmp_path, operator_value
):
    """Startup activation skipped or failed: the first-chat import is the one
    that would relaunch this process, so the boundary still has to cover it.
    """
    agent_dir = _fake_agent_dir(tmp_path)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop(LAZY_INSTALL_GUARD, None)
    if operator_value is not None:
        env[LAZY_INSTALL_GUARD] = operator_value

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            COMPOSED_FIRST_CHAT_SCRIPT.format(
                repo_root=str(REPO_ROOT),
                agent_dir=str(agent_dir),
                guard=LAZY_INSTALL_GUARD,
            ),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "SANDBOX-RELAUNCH" not in result.stderr, result.stderr
    assert ("AFTER=" + repr(operator_value)) in result.stdout, result.stdout


# ---------- 5. static contract ----------------------------------------------


def test_bootstrap_source_has_no_process_wide_override():
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"' not in source
    assert "env[LAZY_INSTALL_GUARD] = " in source


def test_run_agent_import_is_wrapped_by_the_boundary():
    source = AGENT_RUNTIME.read_text(encoding="utf-8")
    boundary = source.index("with agent_import_boundary():")
    assert source.index("from run_agent import AIAgent") > boundary


def test_startup_activation_is_wrapped_by_the_boundary():
    source = (REPO_ROOT / "managed_agent_startup.py").read_text(encoding="utf-8")
    boundary = source.index("with agent_import_boundary():")
    assert source.index('importlib.import_module("hermes_bootstrap")') > boundary
