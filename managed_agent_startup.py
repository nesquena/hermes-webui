"""Initialize the WebUI's Agent source checkout before importing WebUI modules."""

from __future__ import annotations

import contextlib
import importlib
import os
from pathlib import Path
import sys
import threading


#: hermes-agent's internal bridge variable (hermes_bootstrap -> venv_sync,
#: pm.install). ``hermes_cli.venv_sync.prepare_launch`` skips the relaunch into
#: the Agent's managed runtime when the process carries one of
#: ``_SUPPRESSED_VALUES``, and ``pm.install.lazy_installs_allowed`` reads it as an
#: override of ``security.allow_lazy_installs``. It belongs to whoever starts the
#: process -- a service unit, the Agent's Docker bridge, the hermetic test
#: harness -- so the WebUI only reads it: without it this process keeps the
#: Agent's own relaunch, the shape the launcher's pre-flight probe validates.
LAZY_INSTALL_GUARD = "HERMES_DISABLE_LAZY_INSTALLS"

#: The values hermes-agent treats as the opt-out. ``lazy_installs_allowed`` and
#: ``prepare_launch`` accept these spellings, so the boundary uses the same set.
_SUPPRESSED_VALUES = ("1", "true", "yes")

#: Module whose import carries that launch layer. Its body runs once -- Python
#: caches the module afterwards -- so an import that finds it already loaded
#: cannot relaunch anything and the boundary has nothing to override.
LAUNCH_LAYER_MODULE = "hermes_bootstrap"

#: os.environ and sys.path are process-global, so the snapshot/yield/restore
#: sequence needs serializing. Without it two overlapping boundaries capture each
#: other's rewrites and restore out of order: the first exit puts its snapshot
#: back while the second is still importing, and the second then reinstates the
#: paths it read from the first. Re-entrant so one thread may nest boundaries.
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
                # ``venv`` writes ``version``; uv writes ``version_info``.
                if name.strip() in ("version", "version_info"):
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


def relaunch_suppressed() -> bool:
    """True when this process's owner stopped the Agent from relaunching itself."""
    return os.environ.get(LAZY_INSTALL_GUARD, "").strip().lower() in _SUPPRESSED_VALUES


@contextlib.contextmanager
def agent_import_boundary():
    """Keep the Agent's in-process activation from resizing this interpreter.

    hermes_bootstrap relaunches the process into the interpreter the Agent runs
    from, and on a managed install that relaunch is its normal, working path
    (#7876/#7875): the launcher's pre-flight probe re-execs the same way, so the
    interpreter the server ends up on is the one the probe validated. The WebUI
    therefore does not suppress it, and this boundary stays out of the import
    while the process owner has left it enabled.

    The owner can suppress it, though -- a service unit or ``.env`` carrying
    ``HERMES_DISABLE_LAZY_INSTALLS=1``, the Agent's Docker bridge, the hermetic
    test harness -- and then the import activates the Agent's dependency layer
    *in this interpreter*. ``harden_import_path`` re-fronts the Agent checkout and
    ``pm.activate_dependencies`` rewrites ``PYTHONPATH``/``VIRTUAL_ENV``/``PATH``
    and front-loads the venv selected for the pending install, which can belong
    to another Python release. Left there, that venv resolves the Agent's
    C-extensions (``pydantic_core._pydantic_core``) inside an interpreter that
    cannot load them, so the first ``import openai`` fails and every turn fails
    with it (#7982).

    A suppressed import is therefore handed back the shape it started in: the
    three variables return untouched, and the ``sys.path`` the activation
    front-loaded loses only the entries that belong to another Python release. A
    later boundary -- the first-chat ``run_agent`` import every request can reach
    -- finds the launch layer cached, cannot relaunch anything, and applies
    nothing. The Agent's own ``os.putenv``/``os.unsetenv`` wrappers stay in
    place: they are what keeps its environ array alive under a concurrent read.
    """
    # The lock comes first, the check second: a boundary that read
    # ``launch_layer_loaded`` while another was still importing would see the
    # launch layer cached -- Python caches it before its body runs -- and
    # import its dependencies through the venv that boundary is about to drop.
    with _BOUNDARY_LOCK:
        if not relaunch_suppressed() or launch_layer_loaded():
            yield
            return
        saved_env = {key: os.environ.get(key) for key in _BOUNDARY_ENV}
        saved_path = sys.path[:]
        try:
            yield
        finally:
            # The owner's environment comes back first: it must return even if
            # the sys.path rebuild below raises. What ``pm.activate_dependencies``
            # left stays in the order it set, so the installed Agent's venv keeps
            # resolving its dependencies ahead of the server's paths -- the
            # precedence it has without this boundary, so the WebUI's earlier
            # paths cannot shadow the Agent's own version of a shared dependency.
            # Entries the activation dropped (the interpreter's own
            # site-packages, which the server booted with) come back after them,
            # so nothing the server needs is lost.
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            kept = [
                entry for entry in sys.path if entry and not _abi_incompatible(entry)
            ]
            dropped = [
                entry
                for entry in saved_path
                if entry not in kept and not _abi_incompatible(entry)
            ]
            sys.path[:] = kept + dropped


# Private, one-shot handoff to the process a PM-managed Agent's bootstrap
# re-execs this one into. Set only around the bootstrap import, only for an
# Agent found by discovery, and consumed (popped) by the next
# ``_resolve_agent_dir`` call: it never reaches api.config, auto-install, the
# gateway, cron or terminal subprocesses. Deliberately NOT
# HERMES_WEBUI_AGENT_DIR: that is the operator's override, which api.config
# turns into a different interpreter selection and api.startup into
# auto-install authorization (#8529 gate).
_RELAUNCH_AGENT_DIR_ENV = "_HERMES_WEBUI_RELAUNCH_AGENT_DIR"

# What makes an Agent's bootstrap a PM launcher that may re-exec this process
# (Agent v0.21.6+): ``hermes_bootstrap`` calls ``hermes_cli.venv_sync.prepare_launch``,
# which resolves the store Python through ``pm.environments``. ``hermes_bootstrap.py``
# alone is not enough: pre-PM Agents ship one too (the Windows UTF-8 shim, since
# 2026-05). docker_init.bash's select_staged_agent_for_git_checkout checks the same
# files.
_PM_LAUNCH_MARKERS = ("hermes_bootstrap.py", "hermes_cli/venv_sync.py", "pm/environments.py")


def _is_pm_launcher(agent_dir: Path) -> bool:
    return all((agent_dir / marker).is_file() for marker in _PM_LAUNCH_MARKERS)


def _resolve_agent_dir(webui_root: Path) -> str | None:
    """The Agent root to activate: the exported one, else the one api.config uses.

    Direct launches (the README manual launch, a systemd unit that only sets
    PYTHONPATH, ``pip install -e``) export nothing. A PM-managed Agent
    (v0.21.6+) re-execs the process that first imports ``hermes_bootstrap``
    into its store Python under ``-I``, and that child has no third-party
    packages until the bootstrap runs again. The first import must therefore
    happen here, before ``api.profiles`` imports YAML, not later from
    ``api.streaming`` (#7843, #8529).

    * A non-blank ``HERMES_WEBUI_AGENT_DIR`` always wins, even when it names a
      directory without ``run_agent.py``: that is the browser-only signal and
      keeps this hook a no-op.
    * Otherwise (unset, empty or whitespace-only) a relaunch handoff from the
      parent process comes next: the store Python the bootstrap re-execs into
      cannot see an Agent the launching interpreter found through its own
      site-packages (``pip install -e``, a ``.pth``). It is popped here on
      every call, so it is consumed exactly once and never reaches a
      subprocess.
    * Otherwise this falls back to
      ``api.startup.discover_runtime_agent_identity``, the exact discovery
      ``api.config`` runs, so startup cannot activate one Agent's dependency
      layer while the runtime imports another Agent's code. ``api.config``
      treats a blank value as unset too; if a blank value disabled activation,
      the runtime would still import the discovered Agent, only late, which is
      the #8529 crash.

    Discovery is best-effort: any failure leaves startup exactly as before.
    """
    handoff = os.environ.pop(_RELAUNCH_AGENT_DIR_ENV, "").strip()
    explicit = os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip()
    if explicit:
        return str(Path(explicit).expanduser())
    if handoff:
        return handoff
    try:
        # Stdlib-only (api.startup -> api.paths), so safe before activation.
        from api import startup as _startup

        found = _startup.discover_runtime_agent_identity(repo_root=webui_root).agent_dir
    except Exception:  # noqa: BLE001 - discovery is best-effort, never fatal here
        return None
    return str(found) if found else None


def activate_managed_agent() -> None:
    webui_root = str(Path(__file__).resolve().parent)
    # Discovery imports api.startup: make sure it is THIS checkout's package.
    if webui_root not in sys.path:
        sys.path.insert(0, webui_root)
    explicit = bool(os.environ.get("HERMES_WEBUI_AGENT_DIR", "").strip())
    agent_dir = _resolve_agent_dir(Path(webui_root))
    if not agent_dir:
        return
    # Browser-only setups may deliberately point at an empty Agent directory.
    if not (Path(agent_dir) / "run_agent.py").is_file():
        return
    # Without the operator's variable, activate early only the Agents whose
    # bootstrap can re-exec this process: that late re-exec is the #8529 crash.
    # Any other discovered Agent keeps master's direct-launch behaviour exactly:
    # api.config appends it at the END of sys.path and its bootstrap runs lazily,
    # after WebUI's own dependencies loaded from site-packages (a pre-PM Agent's
    # bootstrap moves its checkout to the front, where copies vendored into it
    # would shadow them; #8529 gate).
    if not explicit and not _is_pm_launcher(Path(agent_dir)):
        return
    # Activate dependencies without importing the application: api.config must
    # select the active profile before Agent modules cache profile-sensitive paths.
    # Older Agents and browser-only shims have no bootstrap layer to activate; for
    # them leave sys.path alone so api.config appends the Agent dir at the END as
    # before (a front position lets `pip install -t .` packages in the checkout
    # shadow site-packages).
    if (Path(agent_dir) / "hermes_bootstrap.py").is_file():
        # The bootstrap may re-exec this process into the Agent's store Python,
        # which cannot re-discover an Agent this interpreter found through its
        # own site-packages (`pip install -e`, a .pth). Hand the child the same
        # root privately; an explicit HERMES_WEBUI_AGENT_DIR reaches it anyway.
        if not explicit:
            os.environ[_RELAUNCH_AGENT_DIR_ENV] = agent_dir
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
        finally:
            # No re-exec happened (or it failed): the handoff has no reader left.
            os.environ.pop(_RELAUNCH_AGENT_DIR_ENV, None)
