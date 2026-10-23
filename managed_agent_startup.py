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
        _install_agent_tls_trust()


def _install_agent_tls_trust() -> None:
    """Install the Agent's process-wide TLS trust store before any WebUI import.

    The Agent patches ``ssl.SSLContext`` with truststore once per process, and its
    own entry points do it at start. Embedded here it would otherwise happen lazily
    on the first outbound call, after urllib3/botocore built contexts against the
    unpatched class, and those recurse forever on next use (Bedrock model listing
    failed with "maximum recursion depth exceeded").
    """
    try:
        ssl_verify = importlib.import_module("agent.ssl_verify")
    except ModuleNotFoundError as exc:
        if exc.name in ("agent", "agent.ssl_verify"):
            return  # Agents that predate the process-wide trust store.
        _warn_tls_trust_failure(exc)
        return
    except Exception as exc:  # noqa: BLE001 - same policy as the bootstrap above
        _warn_tls_trust_failure(exc)
        return
    install = getattr(ssl_verify, "install_truststore", None)
    if install is not None:
        install()  # Never raises; falls back to OpenSSL's default trust paths.


def _warn_tls_trust_failure(exc: BaseException) -> None:
    print(
        f"[!!] Hermes Agent TLS trust store setup failed: {type(exc).__name__}: {exc}; "
        "continuing startup with OpenSSL's default trust paths.",
        file=sys.stderr,
        flush=True,
    )
