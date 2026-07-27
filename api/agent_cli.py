"""Resolve the Hermes Agent CLI from the configured source checkout."""

from __future__ import annotations

from pathlib import Path


def source_agent_cli_invocation() -> tuple[list[str], Path]:
    """Return a package-safe CLI invocation and working directory.

    The WebUI can run against a mounted Agent checkout without an installed
    ``hermes-agent`` distribution. Invoke the CLI as a module from the checkout
    root so package imports resolve without relying on a console script.
    """
    from api import config as api_config

    agent_dir = getattr(api_config, "_AGENT_DIR", None)
    if not agent_dir:
        raise FileNotFoundError("Hermes agent checkout not found")
    agent_dir = Path(agent_dir).expanduser().resolve()
    main_py = agent_dir / "hermes_cli" / "main.py"
    if not main_py.exists():
        raise FileNotFoundError("Hermes agent CLI entrypoint not found")

    python_exe = str(getattr(api_config, "PYTHON_EXE", "") or "python3")
    return [python_exe, "-m", "hermes_cli.main"], agent_dir
