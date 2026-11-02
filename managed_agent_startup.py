"""Initialize the WebUI's Agent source checkout before importing WebUI modules."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys


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
