"""The WebUI leaves the Agent's relaunch alone and repairs the shape that needs it.

``hermes_bootstrap`` re-execs the process into the interpreter the Agent runs
from whenever it is imported off that interpreter, and on a managed install that
relaunch is the normal, working path (#7876/#7875): the launcher's pre-flight
probe re-execs the same way, so the interpreter it validates is the one the server
ends up on. The WebUI therefore does not set ``HERMES_DISABLE_LAZY_INSTALLS``.

When the process owner *did* suppress it -- a service unit or ``.env`` carrying
``HERMES_DISABLE_LAZY_INSTALLS=1``, the Agent's Docker bridge, the hermetic test
harness -- the Agent's dependency layer activates inside the server process
instead, and the boundary hands the process back its own environment and the
``sys.path`` of the interpreter it runs on (#7982).

These tests pin both sides: the boundary stays out of the import while the
relaunch is enabled, the owner's own value is never rewritten, overlapping
boundaries cannot interleave their repair, and bootstrap neither exports the
override nor forces it on the capability probe.
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
    relaunch_suppressed,
)


# The Agent's launch layer: off the interpreter it runs from it re-execs into its
# own runtime, which carries none of the WebUI dependencies. Stand in for that
# runtime with a process that dies where the real one would.
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
    (agent_dir / "hermes_bootstrap.py").write_text(
        FAKE_HERMES_BOOTSTRAP, encoding="utf-8"
    )
    (agent_dir / "run_agent.py").write_text(FAKE_RUN_AGENT, encoding="utf-8")
    return agent_dir


# ---------- 1. the boundary is gated on the owner's opt-out ------------------


@pytest.fixture
def launch_layer_unloaded(monkeypatch):
    """A process that has not imported the Agent launch layer yet."""
    monkeypatch.delitem(sys.modules, LAUNCH_LAYER_MODULE, raising=False)


def _process_shape():
    """What the boundary must leave alone: paths, guard and environ helpers."""
    return (sys.path[:], os.environ.get(LAZY_INSTALL_GUARD), os.putenv, os.unsetenv)


@pytest.mark.parametrize("operator_value", [None, "0", "false"])
def test_boundary_stays_out_while_the_relaunch_is_enabled(
    launch_layer_unloaded, monkeypatch, operator_value
):
    """No owner opt-out: the Agent's relaunch decides how this process runs.

    This is the managed install shape, and suppressing its relaunch leaves a
    server that cannot import the Agent at all (`managed_agent_startup.py` is the
    boundary the review reshaped for exactly this).
    """
    if operator_value is None:
        monkeypatch.delenv(LAZY_INSTALL_GUARD, raising=False)
    else:
        monkeypatch.setenv(LAZY_INSTALL_GUARD, operator_value)
    before = _process_shape()

    assert relaunch_suppressed() is False
    with agent_import_boundary():
        assert _process_shape() == before
    assert _process_shape() == before


@pytest.mark.parametrize("operator_value", ["1", "TRUE", "yes"])
def test_boundary_never_rewrites_the_owners_own_value(
    launch_layer_unloaded, monkeypatch, operator_value
):
    """A suppressed relaunch is the owner's decision, spelled the owner's way."""
    monkeypatch.setenv(LAZY_INSTALL_GUARD, operator_value)
    before = _process_shape()

    assert relaunch_suppressed() is True
    with agent_import_boundary():
        assert _process_shape() == before
    assert _process_shape() == before


def test_boundary_restores_after_a_failed_import(launch_layer_unloaded, monkeypatch):
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "1")
    before = _process_shape()
    with pytest.raises(ImportError):
        with agent_import_boundary():
            sys.path.insert(0, "/agent/venv/lib/python3.14/site-packages")
            os.environ["PYTHONPATH"] = "/agent"
            raise ImportError("agent import failed")
    assert _process_shape() == before


def test_boundary_is_a_no_op_once_the_launch_layer_is_loaded(monkeypatch):
    """A cached launch layer cannot relaunch, so a later boundary repairs nothing."""
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "1")
    monkeypatch.setitem(
        sys.modules, LAUNCH_LAYER_MODULE, types.ModuleType(LAUNCH_LAYER_MODULE)
    )
    before = _process_shape()
    with agent_import_boundary():
        assert _process_shape() == before
    assert _process_shape() == before


def test_overlapping_boundaries_do_not_interleave_their_repair(
    launch_layer_unloaded, monkeypatch
):
    """Two threads inside the boundary cannot capture each other's rewrites.

    Unserialized, the second entry records the state the first is building and
    reinstates it on exit, leaving the Agent's venv on ``sys.path`` for the rest
    of the process lifetime.
    """
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "1")
    before = _process_shape()
    inside = 0
    peak = 0
    lock = threading.Lock()

    def body():
        nonlocal inside, peak
        with agent_import_boundary():
            sys.path.insert(0, "/agent/venv/lib/python3.14/site-packages")
            os.environ["PYTHONPATH"] = "/agent"
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.05)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=body, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive(), "boundary never exited"

    assert peak == 1, "two boundaries were inside at once"
    assert _process_shape() == before


# ---------- 2. bootstrap never exports or forces the override ---------------


def _probe_env(monkeypatch, tmp_path: Path) -> dict:
    """The environment bootstrap hands its capability probe."""
    import bootstrap as bs

    seen = {}

    class FakeCompleted:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["env"] = dict(kwargs.get("env") or {})
        return FakeCompleted()

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    assert bs._python_can_run_webui_and_agent(sys.executable, _fake_agent_dir(tmp_path))
    return seen["env"]


def test_probe_does_not_force_the_override(tmp_path, monkeypatch):
    """The probe has to run the shape the service runs.

    Forcing the opt-out onto the probe child validated an interpreter the server
    never used: the child kept the Agent's site-packages on ``sys.path`` while the
    server, relaunched into the store python, did not.
    """
    monkeypatch.delenv(LAZY_INSTALL_GUARD, raising=False)
    env = _probe_env(monkeypatch, tmp_path)
    assert LAZY_INSTALL_GUARD not in env, env
    assert LAZY_INSTALL_GUARD not in os.environ


def test_probe_passes_the_owners_own_value_through(tmp_path, monkeypatch):
    """A service that suppresses the relaunch has its probe run that shape."""
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "1")
    env = _probe_env(monkeypatch, tmp_path)
    assert env[LAZY_INSTALL_GUARD] == "1", env


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
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
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
        (REPO_ROOT / "managed_agent_startup.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        LAZY_INSTALL_GUARD
        + "=0"
        + chr(10)
        + "HERMES_WEBUI_DOTENV_CANARY=from-dotenv"
        + chr(10),
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
    for name in (
        "INVOCATION_ID",
        "JOURNAL_STREAM",
        "NOTIFY_SOCKET",
        "SUPERVISOR_ENABLED",
    ):
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


def _composed_env(tmp_path: Path, operator_value) -> dict:
    agent_dir = _fake_agent_dir(tmp_path)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop(LAZY_INSTALL_GUARD, None)
    if operator_value is not None:
        env[LAZY_INSTALL_GUARD] = operator_value
    env["HERMES_WEBUI_AGENT_DIR"] = str(agent_dir)
    return env


def _run_composed(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=60,
    )


def test_composed_startup_keeps_the_owners_shape(tmp_path):
    """Real startup seam + real first-chat seam, fake Agent launch layer.

    A service that suppressed the relaunch: both imports activate the Agent in
    this process, both boundaries hand the owner's own value back, and no import
    switches the process into an interpreter without WebUI dependencies.
    """
    result = _run_composed(
        COMPOSED_STARTUP_SCRIPT.format(
            repo_root=str(REPO_ROOT), guard=LAZY_INSTALL_GUARD
        ),
        _composed_env(tmp_path, "1"),
    )
    assert result.returncode == 0, result.stderr
    assert "SANDBOX-RELAUNCH" not in result.stderr, result.stderr
    assert ("AFTER=" + repr("1")) in result.stdout, result.stdout


@pytest.mark.parametrize("operator_value", [None, "0"])
def test_composed_startup_leaves_the_relaunch_to_the_agent(tmp_path, operator_value):
    """No owner opt-out: the launch layer re-execs, as it does in production.

    The stand-in re-execs into a process that dies, which is what a runtime
    without WebUI dependencies does; the property pinned here is that the WebUI
    does not stand in the Agent's way.
    """
    result = _run_composed(
        COMPOSED_STARTUP_SCRIPT.format(
            repo_root=str(REPO_ROOT), guard=LAZY_INSTALL_GUARD
        ),
        _composed_env(tmp_path, operator_value),
    )
    assert result.returncode == 97, (result.returncode, result.stderr)
    assert "SANDBOX-RELAUNCH" in result.stderr, result.stderr


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


def _first_chat_script(tmp_path: Path) -> str:
    return COMPOSED_FIRST_CHAT_SCRIPT.format(
        repo_root=str(REPO_ROOT),
        agent_dir=str(tmp_path / "agent"),
        guard=LAZY_INSTALL_GUARD,
    )


def test_first_chat_import_keeps_the_owners_shape(tmp_path):
    """Startup activation skipped or failed: the first-chat import lands here.

    It is the one that would relaunch, so a suppressed service gets the same
    repair from this seam as from startup activation.
    """
    env = _composed_env(tmp_path, "1")
    result = _run_composed(_first_chat_script(tmp_path), env)
    assert result.returncode == 0, result.stderr
    assert "SANDBOX-RELAUNCH" not in result.stderr, result.stderr
    assert ("AFTER=" + repr("1")) in result.stdout, result.stdout


def test_first_chat_import_is_left_to_the_agent_when_enabled(tmp_path):
    env = _composed_env(tmp_path, None)
    result = _run_composed(_first_chat_script(tmp_path), env)
    assert result.returncode == 97, (result.returncode, result.stderr)
    assert "SANDBOX-RELAUNCH" in result.stderr, result.stderr


# ---------- 5. static contract ----------------------------------------------


def test_bootstrap_source_leaves_the_override_to_the_service():
    """bootstrap.py must not carry the override, for the probe or the server."""
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "HERMES_DISABLE_LAZY_INSTALLS" not in source, source
    assert 'os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"' not in source


def test_run_agent_import_is_wrapped_by_the_boundary():
    source = AGENT_RUNTIME.read_text(encoding="utf-8")
    boundary = source.index("with agent_import_boundary():")
    assert source.index("from run_agent import AIAgent") > boundary


def test_startup_activation_is_wrapped_by_the_boundary():
    source = (REPO_ROOT / "managed_agent_startup.py").read_text(encoding="utf-8")
    boundary = source.index("with agent_import_boundary():")
    assert source.index('importlib.import_module("hermes_bootstrap")') > boundary
