"""Hermetic harness for tests that run WebUI's managed-Agent activation.

``server.py`` (through ``managed_agent_startup``) resolves an Agent and IMPORTS
its ``hermes_bootstrap`` before anything else, and a PM-managed Agent's bootstrap
then syncs dependencies and re-execs the process. Discovery reaches well beyond a
test's temp dirs: the WebUI checkout's parent (``<repo>/..`` and
``<repo>/../hermes-agent``), the HOME / USERPROFILE / LOCALAPPDATA / XDG homes,
fixed system paths, a ``hermes`` launcher on PATH, and the selected interpreter's
site-packages. A test that runs that code against the real checkout, with the
developer's environment, can import a real Agent's bootstrap (#8529 gate).

So every launch made through this module:

* runs a COPY of this WebUI checkout whose parent directory holds nothing;
* gets an environment with every home, launcher directory and Hermes/Python
  variable replaced, plus ``HERMES_DISABLE_LAZY_INSTALLS=1``, so a real PM
  bootstrap could never relaunch or sync (the test doubles ignore it);
* is pre-flighted: the Agent the launch would activate, and the one
  ``api.config`` would run against, are resolved first in the same environment
  without importing either. If one lies outside the test's fixture root (a
  system-wide ``/usr/local/lib/hermes-agent`` or ``/opt/hermes-agent``, a
  launcher in ``/usr/bin``), the test is skipped instead of run;
* carries an import guard that ends the process (exit 86) before any
  ``hermes_bootstrap`` outside the fixture root executes, including in a
  relaunched child; ``run_guarded`` fails the test when it fires.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FOREIGN_BOOTSTRAP_EXIT = 86
FIXTURE_ROOT_ENV = "HERMETIC_AGENT_FIXTURE_ROOT"

_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "tests", "docs", "node_modules", "__pycache__", "*.pyc",
    ".venv", "venv", ".pytest_cache", ".ruff_cache",
)

# Every variable that steers discovery, interpreter selection or a real Agent's
# launch preparation is dropped, then the ones discovery reads are set to the
# fixture's own directories.
_DROP_PREFIXES = (
    "HERMES_", "PYTHON", "XDG_", "FAKE_PM_", "PM_", "VIRTUAL_ENV", "CONDA_", "UV_", "PIP_",
)
_DROP_KEYS = {"HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH", "PATH"}


def copy_webui(parent: Path) -> Path:
    """Copy this WebUI checkout to ``parent/hermes-webui``; ``parent`` holds no Agent."""
    dest = parent / "hermes-webui"
    shutil.copytree(REPO_ROOT, dest, ignore=_COPY_IGNORE)
    assert sorted(p.name for p in parent.iterdir()) == ["hermes-webui"], list(parent.iterdir())
    return dest


def _launcher_free_path(bin_dir: Path) -> str:
    dirs = [str(bin_dir)]
    for candidate in ("/usr/bin", "/bin"):
        if os.path.isdir(candidate) and shutil.which("hermes", path=candidate) is None:
            dirs.append(candidate)
    return os.pathsep.join(dirs)


def hermetic_env(fixture_root: Path, **extra: str) -> dict:
    """An environment in which discovery can only see ``fixture_root``."""
    home = fixture_root / "home"
    bin_dir = fixture_root / "bin"
    for directory in (home, bin_dir, fixture_root / "hermes-home"):
        directory.mkdir(parents=True, exist_ok=True)
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(_DROP_PREFIXES) and key.upper() not in _DROP_KEYS
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        LOCALAPPDATA=str(home / "AppData" / "Local"),
        APPDATA=str(home / "AppData" / "Roaming"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        PATH=_launcher_free_path(bin_dir),
        HERMES_HOME=str(fixture_root / "hermes-home"),
        HERMES_WEBUI_STATE_DIR=str(fixture_root / "state"),
        HERMES_WEBUI_TEST_NETWORK_BLOCK="1",
        # Discovery also asks the selected interpreter where `run_agent` lives,
        # and the test interpreter may BE a real Agent's venv.
        HERMES_WEBUI_PYTHON=str(fixture_root / "no-python"),
        # A real PM bootstrap, were one ever reached, must not relaunch or sync.
        HERMES_DISABLE_LAZY_INSTALLS="1",
    )
    env[FIXTURE_ROOT_ENV] = str(fixture_root)
    env.update(extra)
    return env


_PREFLIGHT = (
    "import sys\n"
    "from pathlib import Path\n"
    "root = Path(sys.argv[1])\n"
    "sys.path.insert(0, str(root))\n"
    "import managed_agent_startup\n"
    "from api import startup\n"
    "print('ACTIVATES=%s' % (managed_agent_startup._resolve_agent_dir(root) or ''))\n"
    "print('RUNTIME=%s' % (startup.discover_runtime_agent_identity(repo_root=root).agent_dir or ''))\n"
)


def preflight(webui: Path, env: dict, cwd: Path) -> dict:
    """Resolve, without importing it, the Agent a launch would activate and run.

    Skips the test when either lies outside the fixture root.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PREFLIGHT, str(webui)],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"pre-flight failed:\n{result.stderr[-3000:]}"
    resolved = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("ACTIVATES", "RUNTIME"):
            resolved[key] = Path(value).resolve() if value else None
    assert set(resolved) == {"ACTIVATES", "RUNTIME"}, result.stdout
    root = Path(env[FIXTURE_ROOT_ENV]).resolve()
    for key, path in resolved.items():
        if path is not None and root != path and root not in path.parents:
            pytest.skip(
                f"{key.lower()} Agent {path} lies outside the test fixture {root}: this "
                "machine has an Agent the test environment cannot hide (a system path "
                "or a launcher); refusing to import its bootstrap"
            )
    return resolved


# Prepended to every launched script. A PM relaunch re-runs the script, so the
# guard is reinstalled in the child before its own bootstrap import too.
GUARD = (
    "import importlib.machinery as _hg_machinery, os as _hg_os, sys as _hg_sys\n"
    "class _FixtureOnlyHermesBootstrap:\n"
    "    @staticmethod\n"
    "    def find_spec(name, path=None, target=None):\n"
    "        if name != 'hermes_bootstrap':\n"
    "            return None\n"
    "        spec = _hg_machinery.PathFinder.find_spec(name, path)\n"
    "        if spec is None or not spec.origin:\n"
    "            return spec\n"
    "        origin = _hg_os.path.realpath(spec.origin)\n"
    f"        root = _hg_os.path.realpath(_hg_os.environ[{FIXTURE_ROOT_ENV!r}])\n"
    "        if not origin.startswith(root + _hg_os.sep):\n"
    "            _hg_sys.stderr.write('FOREIGN_HERMES_BOOTSTRAP %s\\n' % origin)\n"
    "            _hg_sys.stderr.flush()\n"
    f"            _hg_os._exit({FOREIGN_BOOTSTRAP_EXIT})\n"
    "        return spec\n"
    "_hg_sys.meta_path.insert(0, _FixtureOnlyHermesBootstrap)\n"
)


def run_guarded(script: str, *, webui: Path, env: dict, cwd: Path, timeout: int = 180):
    """Pre-flight, then run ``GUARD + script``. Returns (result, resolved)."""
    resolved = preflight(webui, env, cwd)
    result = subprocess.run(
        [sys.executable, "-c", GUARD + script],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode != FOREIGN_BOOTSTRAP_EXIT and "FOREIGN_HERMES_BOOTSTRAP" not in result.stderr, (
        "the launch tried to import a hermes_bootstrap outside the test fixture:\n"
        f"{result.stderr[-3000:]}"
    )
    return result, resolved
