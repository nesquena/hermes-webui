"""Helpers for discovering installed Hermes Agent import roots."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


_WRAPPER_COMMANDS = ("hermes", "hermes-agent")
_MAX_WRAPPER_HOPS = 4
_HERMES_PYTHON_RE = re.compile(
    r"""^\s*export\s+HERMES_PYTHON=(?P<quote>['"]?)(?P<path>.+?)(?P=quote)\s*$""",
    re.MULTILINE,
)
_EXEC_TARGET_RE = re.compile(
    r"""^\s*exec(?:\s+-a\s+(?:"\$0"|\$0))?\s+(?P<quote>['"])(?P<path>/[^'"]+)(?P=quote)""",
    re.MULTILINE,
)


def agent_dir_from_python(python_exe: str) -> Path | None:
    """Resolve the import root that provides ``run_agent.py`` for a Python."""
    try:
        check = subprocess.run(
            [
                python_exe,
                "-c",
                (
                    "import inspect, pathlib, run_agent\n"
                    "print(pathlib.Path(inspect.getfile(run_agent)).resolve().parent)\n"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if check.returncode != 0:
        return None
    candidate = Path(check.stdout.strip()).expanduser()
    if candidate.exists() and (candidate / "run_agent.py").exists():
        return candidate.resolve()
    return None


def hermes_python_from_cli() -> str | None:
    """Return HERMES_PYTHON exported by an installed Hermes wrapper, if any."""
    for command in _WRAPPER_COMMANDS:
        command_path = shutil.which(command)
        if not command_path:
            continue
        python_exe = _hermes_python_from_wrapper(Path(command_path))
        if python_exe:
            return python_exe
    return None


def agent_dir_from_hermes_cli() -> Path | None:
    hermes_python = hermes_python_from_cli()
    if not hermes_python:
        return None
    return agent_dir_from_python(hermes_python)


def _hermes_python_from_wrapper(path: Path) -> str | None:
    seen: set[Path] = set()
    script_path = path
    for _ in range(_MAX_WRAPPER_HOPS):
        text = _read_wrapper(script_path, seen)
        if text is None:
            return None
        python_exe = _extract_hermes_python(text)
        if python_exe:
            return python_exe
        next_wrapper = _extract_exec_target(text)
        if next_wrapper is None:
            return None
        script_path = next_wrapper
    return None


def _read_wrapper(path: Path, seen: set[Path]) -> str | None:
    try:
        script_path = path.resolve()
        if script_path in seen:
            return None
        seen.add(script_path)
        return script_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _extract_hermes_python(text: str) -> str | None:
    match = _HERMES_PYTHON_RE.search(text)
    if not match:
        return None
    python_exe = match.group("path")
    return python_exe if Path(python_exe).exists() else None


def _extract_exec_target(text: str) -> Path | None:
    match = _EXEC_TARGET_RE.search(text)
    if not match:
        return None
    return Path(match.group("path"))
