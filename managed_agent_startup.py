"""Initialize a discovered Agent source checkout before importing WebUI modules."""

import contextlib
import importlib
import os
from pathlib import Path
import sys
import threading


#: hermes-agent's internal bridge variable (hermes_bootstrap -> venv_sync,
#: pm.install). hermes_bootstrap.py relaunches the process via os.execv into
#: its managed sandbox the moment it is imported with a pending lazy
#: install/update; pm.install.lazy_installs_allowed() also reads it as an
#: unconditional override of security.allow_lazy_installs. The WebUI needs the
#: relaunch disabled only while Agent code is being imported, so this guard is
#: scoped to that boundary instead of the process lifetime.
LAZY_INSTALL_GUARD = "HERMES_DISABLE_LAZY_INSTALLS"

#: Module whose import carries that launch layer. Its body runs once -- Python
#: caches the module afterwards -- so an import that finds it already loaded
#: cannot relaunch anything and the boundary has nothing to override.
LAUNCH_LAYER_MODULE = "hermes_bootstrap"

#: os.environ is process-global, so the read/set/yield/restore sequence needs
#: serializing. Without it two overlapping boundaries save each other's value and
#: restore out of order: the first one to exit removes the guard while the other
#: is still importing, and the second then re-exports "1" for the rest of the
#: process lifetime -- exactly the regression this scoping exists to remove.
#: Re-entrant so a single thread may nest boundaries.
_BOUNDARY_LOCK = threading.RLock()

#: Environment the Agent's ``pm.activate_dependencies`` rewrites in whatever
#: process imports ``hermes_bootstrap``: it exports the checkout plus the Agent's
#: own selected venv as ``PYTHONPATH``, drops ``VIRTUAL_ENV`` and prepends the
#: venv's bin to ``PATH``. That venv runs its own Python when a lazy install is
#: pending, so anything it leaves here makes this process import dependencies it
#: cannot load.
_BOUNDARY_ENV = ("PYTHONPATH", "VIRTUAL_ENV", "PATH")


#: How far above an entry to look for the ``pyvenv.cfg`` naming its venv.
_VENV_CFG_DEPTH = 4


def _release_from_dir_name(part: str) -> tuple[int, int] | None:
    """``python3.11`` -> ``(3, 11)``; None for any other directory name."""
    if not part.startswith("python"):
        return None
    major, _, minor = part[len("python") :].partition(".")
    if not (major.isdigit() and minor.isdigit()):
        return None
    return int(major), int(minor)


def _venv_release(entry: str) -> tuple[int, int] | None:
    """The Python release the ``pyvenv.cfg`` above ``entry`` names, if any.

    A config that cannot be read, or that does not spell a ``major.minor``
    release, is reported as "no release" instead of raising: this runs while
    the import boundary puts the process back together, and an exception there
    would strand the environment rewrites the boundary exists to undo.
    """
    for candidate in (Path(entry), *Path(entry).parents)[:_VENV_CFG_DEPTH]:
        config = candidate / "pyvenv.cfg"
        try:
            if not config.is_file():
                continue
            for line in config.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "version":
                    parts = value.strip().split(".")
                    if len(parts) < 2:
                        return None
                    return int(parts[0]), int(parts[1])
        except (OSError, ValueError):
            return None
        return None
    return None


def _abi_incompatible(entry: str) -> bool:
    """True when this interpreter cannot import C-extensions from ``entry``.

    ``pm.activate_dependencies`` front-loads the venv selected for the pending
    install; for a lazy install that is the Agent's own sandbox Python (a 3.14
    sandbox under a 3.11 server), while an already-installed Agent's venv is
    built for this interpreter and stays the running Agent's dependency source.
    """
    if not entry:
        return False
    release = _venv_release(entry)
    if release is not None:
        return release != sys.version_info[:2]
    for part in Path(entry).parts:
        found = _release_from_dir_name(part)
        if found is not None and found != sys.version_info[:2]:
            return True
    return False


def launch_layer_loaded() -> bool:
    """True once the Agent launch layer is imported and cannot relaunch again."""
    return LAUNCH_LAYER_MODULE in sys.modules


@contextlib.contextmanager
def agent_import_boundary():
    """Disable the Agent's lazy-install interception for exactly one import.

    The sandbox hermes_bootstrap relaunches into carries no WebUI
    dependencies, so an unguarded Agent import replaces this process with one
    that dies on ``import yaml`` and the service restarts forever. Keeping the
    override for the whole process would instead outlive startup and silently
    disable on-demand installs the operator allowed through
    ``security.allow_lazy_installs`` -- pm/install.py treats any truthy value
    as the policy. Whatever the operator had (set or unset) is restored as
    soon as the import returns, including when it fails.

    The override is process-global, so it covers only imports that can still
    relaunch. ``activate_managed_agent`` imports hermes_bootstrap under this
    boundary before request threads exist; a later boundary -- the first-chat
    ``run_agent`` import every request can reach -- finds that module cached and
    applies nothing, instead of exposing the dual-purpose policy variable to
    unrelated threads for the length of an import that cannot relaunch.

    The import mutates this process well beyond that variable, so the boundary
    restores ``PYTHONPATH``, ``VIRTUAL_ENV``, ``PATH`` and
    ``os.putenv``/``os.unsetenv`` afterwards, and drops from ``sys.path`` whatever
    the import added for another Python release: ``harden_import_path`` re-fronts
    the Agent checkout and ``pm.activate_dependencies`` front-loads the selected
    venv -- the Agent's sandbox Python when a lazy install is pending -- while
    ``install_never_free_environ`` replaces the environ helpers. Left in place, that
    other-release venv makes the next import load the Agent's C-extensions
    (``pydantic_core._pydantic_core``) into an interpreter that cannot load them
    (#7982); a venv built for this interpreter stays, so the running Agent keeps
    resolving its dependencies in-process the way it already did.
    """
    if launch_layer_loaded():
        yield
        return
    with _BOUNDARY_LOCK:
        previous = os.environ.get(LAZY_INSTALL_GUARD)
        saved_env = {key: os.environ.get(key) for key in _BOUNDARY_ENV}
        saved_path = sys.path[:]
        saved_putenv = os.putenv
        saved_unsetenv = os.unsetenv
        os.environ[LAZY_INSTALL_GUARD] = "1"
        try:
            yield
        finally:
            # The Agent's import hardens its own checkout onto sys.path, activates
            # its selected venv and takes over os.putenv/os.unsetenv. The server
            # keeps its own interpreter: a venv of another Python release would be
            # imported as C-extensions this one cannot load (#7982). What
            # `pm.activate_dependencies` left stays in the order it set: the
            # installed Agent's venv keeps resolving its dependencies ahead of the
            # server's paths, the precedence it has without this boundary, so the
            # WebUI's earlier paths cannot shadow the Agent's own version of a
            # shared dependency. Entries the activation dropped (the interpreter's
            # own site-packages, which the server booted with) come back after
            # them, so nothing the server needs is lost.
            try:
                kept = [
                    entry
                    for entry in sys.path
                    if entry and not _abi_incompatible(entry)
                ]
                dropped = [
                    entry
                    for entry in saved_path
                    if entry not in kept and not _abi_incompatible(entry)
                ]
                sys.path[:] = kept + dropped
            finally:
                # Unconditional, so the rewrites above are undone even if the
                # sys.path rebuild raises: this boundary exists to hand the
                # guard back, and a cleanup error must not keep it set to "1"
                # (or leave the Agent's PYTHONPATH/PATH/venv in place) for the
                # rest of the process.
                os.putenv = saved_putenv
                os.unsetenv = saved_unsetenv
                for key, value in saved_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                if previous is None:
                    os.environ.pop(LAZY_INSTALL_GUARD, None)
                else:
                    os.environ[LAZY_INSTALL_GUARD] = previous


def activate_managed_agent() -> None:
    agent_dir = os.environ.get("HERMES_WEBUI_AGENT_DIR")
    if not agent_dir:
        return
    # Browser-only setups may deliberately point at an empty Agent directory.
    if not (Path(agent_dir) / "run_agent.py").is_file():
        return

    webui_root = str(Path(__file__).resolve().parent)
    if webui_root not in sys.path:
        sys.path.insert(0, webui_root)
    # Activate dependencies without importing the application: api.config must
    # select the active profile before Agent modules cache profile-sensitive paths.
    # Older Agents and browser-only shims have no bootstrap layer to activate; for
    # them leave sys.path alone so api.config appends the Agent dir at the END as
    # before (a front position lets `pip install -t .` packages in the checkout
    # shadow site-packages).
    if (Path(agent_dir) / "hermes_bootstrap.py").is_file():
        # Bootstrap's probe adds the checkout to its own PYTHONPATH, not ours.
        if agent_dir not in sys.path:
            sys.path.insert(1, agent_dir)
        try:
            # Boundary: hermes_bootstrap's module-level launch layer would
            # otherwise replace this WebUI process with its sandbox.
            with agent_import_boundary():
                importlib.import_module("hermes_bootstrap")
        except Exception as exc:  # noqa: BLE001 - SystemExit (relaunch/repair exit) still propagates
            # A broken Agent must not stop WebUI from starting: before this hook the
            # Agent import was lazy and an ImportError only disabled chat, leaving the
            # UI, diagnostics and updater reachable. Keep that behavior.
            print(
                f"[!!] Hermes Agent dependency activation failed: {type(exc).__name__}: {exc}; "
                "continuing startup. If WebUI then fails to import a dependency, run "
                "`hermes pm repair` or set HERMES_WEBUI_PYTHON.",
                file=sys.stderr,
                flush=True,
            )
