"""A suppressed Agent relaunch activates its dependencies inside this process.

When the process owner suppressed the relaunch (``HERMES_DISABLE_LAZY_INSTALLS``
set to a value hermes-agent reads as the opt-out), importing ``hermes_bootstrap``
activates the Agent's dependency layer *here*: ``harden_import_path`` re-fronts
the checkout and ``pm.activate_dependencies`` front-loads the venv selected for
the pending install -- another Python release's when a lazy install is pending --
while rewriting ``PYTHONPATH``/``PATH``/``VIRTUAL_ENV``. Left in place, that venv
resolves the Agent's C-extensions inside an interpreter that cannot load them.

The boundary puts the owner's environment back, drops only the entries that
belong to another release, keeps the venv the installed Agent runs from ahead of
the server's own paths, and leaves the Agent's environ-array wrappers alone --
those are what keep a concurrent read from touching a freed array.

The wrappers belong to the running Agent, not to this module's tests:
``_restore_process_state`` hands the process its own helpers and environment
back after every test here, or the rest of the suite runs without them.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading
import types

import pytest

from managed_agent_startup import LAZY_INSTALL_GUARD, agent_import_boundary

#: The Agent's launch layer, as ``pm.activate_dependencies`` leaves the process:
#: the checkout hardened onto ``sys.path``, the selected venv front-loaded, and
#: the environ helpers taken over (``install_never_free_environ``).
_STUB = """\
import os
import sys

sys.path.insert(0, {venv!r})
os.environ["PYTHONPATH"] = {agent!r}
os.environ.pop("VIRTUAL_ENV", None)
os.environ["PATH"] = {venv!r} + os.pathsep + os.environ.get("PATH", "")
os.putenv = lambda *args: None
os.unsetenv = lambda *args: None
"""

#: Stands in for ``pydantic_core``: a package whose ``__init__`` imports the
#: compiled part next to it.
_PACKAGE = "agent_generation_dep"


@pytest.fixture(autouse=True)
def _restore_process_state():
    """Hand this process back what an activated launch layer took over.

    ``install_never_free_environ`` replaces ``os.putenv``/``os.unsetenv`` so a
    concurrent read cannot touch a freed environ array, and the boundary leaves
    that protection to the Agent -- correctly, because while the Agent runs it is
    the owner of the process. In a test process it would outlive the test:
    ``os.environ`` writes stop reaching the native environment that every later
    test hands to its child processes. That is what failed shard 3 of the #7984
    CI run -- a test file running after this one could not see the ``HOME`` it
    exported, so its git credential helper was never found and git fell back to
    prompting the user.

    Snapshot before, restore after, and restore the helpers first so that the
    environment rebuild below reaches the native array.
    """
    helpers = (os.putenv, os.unsetenv)
    environ = os.environ.copy()
    sys_path = sys.path[:]
    yield
    os.putenv, os.unsetenv = helpers
    for key in set(os.environ) | set(environ):
        value = environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    sys.path[:] = sys_path


def _other_release() -> str:
    return f"{sys.version_info[0]}.{sys.version_info[1] + 1}"


def _agent_with_venv(
    tmp_path, monkeypatch, release, *, cfg_key="version_info", body=""
):
    """A fake Agent checkout whose launch layer activates a ``release`` venv.

    ``cfg_key`` selects how the venv names its generation: ``venv`` writes
    ``version``, uv writes ``version_info``. ``body`` appends statements to the
    launch layer, for a test that needs to see the activation while it runs.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    venv_root = tmp_path / "agent-venv"
    venv = venv_root / "lib" / f"python{release}" / "site-packages"
    venv.mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text(
        f"{cfg_key} = {release}.1\n", encoding="utf-8"
    )
    (agent / "hermes_bootstrap.py").write_text(
        _STUB.format(venv=str(venv), agent=str(agent)) + body,
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(agent))
    # The owner of the process suppressed the relaunch: the precondition for the
    # in-process activation these tests exercise.
    monkeypatch.setenv(LAZY_INSTALL_GUARD, "1")
    monkeypatch.setenv("VIRTUAL_ENV", "/host/venv")
    monkeypatch.setenv("PYTHONPATH", "/host/site-packages")
    monkeypatch.delitem(sys.modules, "hermes_bootstrap", raising=False)
    return agent, venv


@pytest.mark.parametrize("cfg_key", ["version", "version_info"])
def test_other_release_venv_is_dropped_and_environment_restored(
    tmp_path, monkeypatch, cfg_key
):
    """#7982: a pending lazy install activates a venv this interpreter cannot load."""
    agent, venv = _agent_with_venv(
        tmp_path, monkeypatch, _other_release(), cfg_key=cfg_key
    )
    host_sys_path = sys.path[:]
    host_path = os.environ["PATH"]
    host_putenv, host_unsetenv = os.putenv, os.unsetenv

    try:
        with agent_import_boundary():
            importlib.import_module("hermes_bootstrap")
            assert str(venv) in sys.path
            assert os.environ["PYTHONPATH"] == str(agent)
            assert "VIRTUAL_ENV" not in os.environ
    finally:
        sys.modules.pop("hermes_bootstrap", None)

    assert sys.path == host_sys_path
    assert os.environ["PYTHONPATH"] == "/host/site-packages"
    assert os.environ["VIRTUAL_ENV"] == "/host/venv"
    assert os.environ["PATH"] == host_path

    # The Agent took over the environ helpers to keep a freed environ array from
    # being read; the boundary leaves that protection in place.
    # ``_restore_process_state`` returns them to this process afterwards.
    assert (os.putenv, os.unsetenv) != (host_putenv, host_unsetenv)


def test_in_process_agent_venv_stays_importable(tmp_path, monkeypatch):
    """The installed Agent's own venv is this process's dependency source."""
    release = f"{sys.version_info[0]}.{sys.version_info[1]}"
    _, venv = _agent_with_venv(tmp_path, monkeypatch, release)
    host_sys_path = sys.path[:]

    try:
        with agent_import_boundary():
            importlib.import_module("hermes_bootstrap")
    finally:
        sys.modules.pop("hermes_bootstrap", None)

    assert str(venv) in sys.path
    # ``pm.activate_dependencies`` front-loads the selected venv, so the Agent
    # keeps resolving its dependencies ahead of the server's own paths.
    assert sys.path == [str(venv)] + host_sys_path
    assert os.environ["PYTHONPATH"] == "/host/site-packages"
    assert os.environ["VIRTUAL_ENV"] == "/host/venv"


def _snapshot():
    """This process's interpreter state, as the boundary must hand it back."""
    return (
        sys.path[:],
        os.environ.get("PATH"),
        os.environ.get("PYTHONPATH"),
        os.environ.get("VIRTUAL_ENV"),
    )


def _assert_restored(snapshot):
    path, host_path, pythonpath, virtual_env = snapshot
    assert sys.path == path
    assert os.environ.get("PATH") == host_path
    assert os.environ.get("PYTHONPATH") == pythonpath
    assert os.environ.get("VIRTUAL_ENV") == virtual_env


def test_malformed_pyvenv_cfg_is_tolerated(tmp_path, monkeypatch):
    """A ``version`` without a minor part must not escape as IndexError.

    The release parser runs while the boundary rebuilds ``sys.path``, so an
    exception there would skip the rest of the repair.
    """
    _agent_with_venv(tmp_path, monkeypatch, _other_release())
    (tmp_path / "agent-venv" / "pyvenv.cfg").write_text(
        "version_info = 3\n", encoding="utf-8"
    )
    snapshot = _snapshot()

    try:
        with agent_import_boundary():
            importlib.import_module("hermes_bootstrap")
    finally:
        sys.modules.pop("hermes_bootstrap", None)

    _assert_restored(snapshot)


def test_unreadable_pyvenv_cfg_is_tolerated(tmp_path, monkeypatch):
    """An unreadable path must not escape as OSError either."""
    _agent_with_venv(tmp_path, monkeypatch, _other_release())
    real_is_file = Path.is_file

    def unreadable(self):
        if self.name == "pyvenv.cfg":
            raise PermissionError(13, "Permission denied")
        return real_is_file(self)

    snapshot = _snapshot()

    try:
        monkeypatch.setattr(Path, "is_file", unreadable)
        with agent_import_boundary():
            importlib.import_module("hermes_bootstrap")
    finally:
        sys.modules.pop("hermes_bootstrap", None)

    _assert_restored(snapshot)


def _foreign_artifact_release() -> tuple[str, str]:
    """The other release, and the extension suffix that names it."""
    release = _other_release()
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ""
    running = f"{sys.version_info[0]}{sys.version_info[1]}"
    other = release.replace(".", "")
    if running not in suffix:
        pytest.skip(f"no extension suffix naming {running} on this platform")
    return release, suffix.replace(running, other)


def _foreign_package(tmp_path, monkeypatch) -> Path:
    """A package whose only compiled part belongs to another Python release.

    ``pydantic_core`` in production: its ``__init__`` imports the compiled
    ``_pydantic_core`` beside it, and the copy the foreign venv ships is not
    loadable by this interpreter, so the import fails with ``No module named
    'pydantic_core._pydantic_core'`` -- a real import failure, not a stand-in.
    """
    release, tag = _foreign_artifact_release()
    _, venv = _agent_with_venv(tmp_path, monkeypatch, release)
    foreign = venv / _PACKAGE
    foreign.mkdir()
    (foreign / f"_{_PACKAGE}{tag}").write_bytes(b"")
    (foreign / "__init__.py").write_text(
        f"from ._{_PACKAGE} import core\n", encoding="utf-8"
    )
    host = tmp_path / "host" / _PACKAGE
    host.mkdir(parents=True)
    (host / "__init__.py").write_text("core = 'host'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(host.parent))
    return venv


def test_a_foreign_artifact_breaks_the_import_without_the_boundary(
    tmp_path, monkeypatch
):
    """The shape the owner asked for, unguarded: this is the #7982 failure.

    Nothing here stands in for the failure -- the import system refuses the
    artifact the front-loaded venv ships, exactly as it refuses another
    release's ``_pydantic_core``.
    """
    _foreign_package(tmp_path, monkeypatch)
    host_sys_path = sys.path[:]
    try:
        importlib.import_module("hermes_bootstrap")
        with pytest.raises(ModuleNotFoundError) as failure:
            importlib.import_module(_PACKAGE)
        assert failure.value.name == f"{_PACKAGE}._{_PACKAGE}"
    finally:
        sys.modules.pop("hermes_bootstrap", None)
        sys.modules.pop(_PACKAGE, None)
        sys.path[:] = host_sys_path


def test_the_boundary_keeps_the_host_copy_importable(tmp_path, monkeypatch):
    """With the boundary the same import resolves what this interpreter owns."""
    _foreign_package(tmp_path, monkeypatch)
    try:
        with agent_import_boundary():
            importlib.import_module("hermes_bootstrap")
        assert importlib.import_module(_PACKAGE).core == "host"
    finally:
        sys.modules.pop("hermes_bootstrap", None)
        sys.modules.pop(_PACKAGE, None)


def test_the_activation_is_left_in_place_for_the_fixture(tmp_path, monkeypatch):
    """One activated launch layer -- exactly the leak the fixture cleans up."""
    _agent_with_venv(tmp_path, monkeypatch, _other_release())
    host_putenv, host_unsetenv = os.putenv, os.unsetenv

    try:
        with agent_import_boundary():
            importlib.import_module("hermes_bootstrap")
    finally:
        sys.modules.pop("hermes_bootstrap", None)

    assert (os.putenv, os.unsetenv) != (host_putenv, host_unsetenv)


def test_os_environ_reaches_child_processes_after_the_boundary(monkeypatch):
    """The wrapper has to be back, or every later test loses its own env.

    ``os.environ`` writes go through ``os.putenv``/``os.unsetenv``, so a process
    still carrying the Agent's wrappers never updates the environ its children
    inherit. Reads the child's own answer, not this process's dictionary.
    """
    monkeypatch.setenv("BOUNDARY_CHILD_PROBE", "1")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('BOUNDARY_CHILD_PROBE', ''))",
        ],
        capture_output=True,
        text=True,
    )

    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "1"


def test_a_second_boundary_waits_for_the_activation_in_flight(tmp_path, monkeypatch):
    """The launch layer is cached before its activation has finished.

    Every first chat imports ``run_agent``, and the import carrying the launch
    layer puts it in ``sys.modules`` before its body -- the activation that
    rewrites ``PYTHONPATH``/``PATH``/``VIRTUAL_ENV`` -- has run. A request that
    read that cache without the boundary lock would import its dependencies
    through the venv the first request is about to drop.
    """
    gate = tmp_path / "launch_gate.py"
    gate.write_text(
        "import threading\n"
        "\n"
        "entered = threading.Event()\n"
        "release = threading.Event()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    _agent_with_venv(
        tmp_path,
        monkeypatch,
        _other_release(),
        body=(
            "import launch_gate\n"
            "launch_gate.entered.set()\n"
            "launch_gate.release.wait(5)\n"
        ),
    )
    second_entered = threading.Event()

    def activation():
        with agent_import_boundary():
            importlib.import_module("hermes_bootstrap")

    def second_request():
        with agent_import_boundary():
            second_entered.set()

    started = []
    try:
        import launch_gate

        first = threading.Thread(target=activation)
        started.append(first)
        first.start()
        assert launch_gate.entered.wait(5), "the activation never started"
        second = threading.Thread(target=second_request)
        started.append(second)
        second.start()
        retried = second_entered.wait(0.5)
        launch_gate.release.set()
        first.join(5)
        second.join(5)

        assert not retried, "a request entered while the activation was running"
        assert second_entered.wait(5), "the waiting request never got through"
        assert not first.is_alive() and not second.is_alive()
    finally:
        # A failure above -- the entered wait included -- still has to hand this
        # process back as it was, so the cleanup reads only names that exist on
        # every path: the started threads are collected as they start, and the
        # gate is reached through the import cache instead of a local the failed
        # wait would leave unassigned (an UnboundLocalError here would replace
        # the assertion that says what actually broke).
        gate_module = sys.modules.get("launch_gate")
        if gate_module is not None:
            gate_module.release.set()
        for worker in started:
            worker.join(10)
        sys.modules.pop("hermes_bootstrap", None)
        sys.modules.pop("launch_gate", None)


class _NeverEntered:
    """The gate of a launch layer that never announces its activation.

    ``set`` is a no-op and ``wait`` times out at once, so the test that owns the
    gate fails on its first assertion -- the failure path whose cleanup must not
    hide the failure.
    """

    def set(self):
        return None

    def wait(self, timeout=None):
        return False


def test_a_failed_entry_wait_still_hands_the_process_back(tmp_path, monkeypatch):
    """Cleanup must survive the wait it guards failing.

    ``test_a_second_boundary_waits_for_the_activation_in_flight`` enters that
    cleanup with the activation worker started and the second thread not yet
    bound: cleanup reading a local assigned on the way down raises
    ``UnboundLocalError``, which replaces the assertion naming the real problem
    and skips the join, leaving a worker rewriting this process's state. Fails
    the entered wait on purpose and checks all three survivors.
    """
    gate = types.ModuleType("launch_gate")
    gate.entered = _NeverEntered()
    gate.release = threading.Event()
    monkeypatch.setitem(sys.modules, "launch_gate", gate)
    snapshot = _snapshot()
    already = set(threading.enumerate())

    with pytest.raises(AssertionError, match="the activation never started"):
        test_a_second_boundary_waits_for_the_activation_in_flight(tmp_path, monkeypatch)

    assert not [t for t in threading.enumerate() if t not in already], (
        "cleanup left a worker running"
    )
    assert "hermes_bootstrap" not in sys.modules
    assert "launch_gate" not in sys.modules
    # sys.path is left alone: the failing test prepends its own gate, and the
    # autouse fixture -- not this cleanup -- owns that. The environment is what
    # an unjoined activation would still be rewriting. The host values are the
    # ones the fixture set for the launch layer to front-load its own venv over.
    assert os.environ.get("PATH") == snapshot[1], "the activation's PATH survived"
    assert os.environ.get("VIRTUAL_ENV") == "/host/venv"
    assert os.environ.get("PYTHONPATH") == "/host/site-packages"
