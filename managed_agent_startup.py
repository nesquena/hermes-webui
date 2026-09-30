"""Initialize a discovered Agent source checkout before importing WebUI modules."""

import importlib
import os
from pathlib import Path
import sys


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
