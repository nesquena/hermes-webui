"""CLI_VISIBLE_SESSION_LIMIT is overridable via HERMES_WEBUI_VISIBLE_SESSION_LIMIT.

The sidebar recency window also bounds how many delegated subagent children can
render at once, since a child only nests when its row wins a slot in the same
payload. Operators running wide fan-outs need to raise it without editing code.

The constant is bound once at import time, so each case imports ``api.models``
in a fresh subprocess (same pattern as test_issue3283_profiles_config_import_order)
rather than ``importlib.reload``-ing it in the shared test process — reloading
would recreate the module's locks, caches, and classes while other modules keep
references to the old objects.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import api.models
print(api.models.CLI_VISIBLE_SESSION_LIMIT)
"""


def _limit_in_fresh_interpreter(tmp_path, value):
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("HERMES_WEBUI_") or key in ("HERMES_HOME", "HERMES_BASE_HOME"):
            env.pop(key)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if value is not None:
        env["HERMES_WEBUI_VISIBLE_SESSION_LIMIT"] = value
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())


def test_defaults_to_20_when_unset(tmp_path):
    assert _limit_in_fresh_interpreter(tmp_path, None) == 20


def test_env_override_raises_the_window(tmp_path):
    assert _limit_in_fresh_interpreter(tmp_path, "64") == 64


@pytest.mark.parametrize("value", ["bogus", "0", "-5"])
def test_invalid_or_nonpositive_falls_back_to_default(tmp_path, value):
    assert _limit_in_fresh_interpreter(tmp_path, value) == 20


def test_setting_is_documented():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "| `HERMES_WEBUI_VISIBLE_SESSION_LIMIT` | `20` |" in readme
    assert "# HERMES_WEBUI_VISIBLE_SESSION_LIMIT=20" in env_example
