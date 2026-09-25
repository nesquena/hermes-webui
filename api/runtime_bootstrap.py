"""Runtime preparation for the external WebUI server entry point."""

from __future__ import annotations

import os
import signal


_TEST_NETWORK_BLOCK_VALUES = {"1", "true", "yes"}


def activate_hermes_runtime() -> None:
    """Activate source-installed Hermes PM dependencies before API imports."""
    if os.environ.get("HERMES_WEBUI_TEST_NETWORK_BLOCK", "").strip() in _TEST_NETWORK_BLOCK_VALUES:
        return
    try:
        import hermes_bootstrap  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "hermes_bootstrap":
            raise


def ignore_sigpipe() -> None:
    """Keep broken client writes from terminating the server process."""
    if (sigpipe := getattr(signal, "SIGPIPE", None)) is not None:
        signal.signal(sigpipe, signal.SIG_IGN)
