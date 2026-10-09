"""CLI_VISIBLE_SESSION_LIMIT is overridable via HERMES_WEBUI_VISIBLE_SESSION_LIMIT.

The sidebar recency window also bounds how many delegated subagent children can
render at once, since a child only nests when its row wins a slot in the same
payload. Operators running wide fan-outs need to raise it without editing code.

The constant is bound once at import time, so each case imports ``api.models``
in a fresh subprocess (same pattern as test_issue3283_profiles_config_import_order)
rather than ``importlib.reload``-ing it in the shared test process — reloading
would recreate the module's locks, caches, and classes while other modules keep
references to the old objects.

The route cap must come from that same value. A profile ``.env`` must not
override it: the constant is resolved in ``api.config`` before profile init,
and the key is in ``api.profiles._PROTECTED_ENV_KEYS``.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import api.models
import api.routes
print(api.models.CLI_VISIBLE_SESSION_LIMIT)
print(api.routes._cli_visible_session_cap())
rows = [{"source": "cli", "title": "cli", "updated_at": i} for i in range(30)]
print(len(api.routes._cap_recent_cli_sessions(rows)))
"""

_PROFILE_PROBE = """
import os
from pathlib import Path
import api.config
import api.profiles
home = Path(os.environ["HERMES_HOME"])
(home / ".env").write_text("HERMES_WEBUI_VISIBLE_SESSION_LIMIT=7\\n", encoding="utf-8")
api.profiles._reload_dotenv(home)
print("PROTECTED", "HERMES_WEBUI_VISIBLE_SESSION_LIMIT" in api.profiles._PROTECTED_ENV_KEYS)
print("ENV", os.environ.get("HERMES_WEBUI_VISIBLE_SESSION_LIMIT"))
print("LIMIT", api.config.CLI_VISIBLE_SESSION_LIMIT)
"""


def _run_probe(tmp_path, probe, value):
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("HERMES_WEBUI_") or key in ("HERMES_HOME", "HERMES_BASE_HOME"):
            env.pop(key)
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if value is not None:
        env["HERMES_WEBUI_VISIBLE_SESSION_LIMIT"] = value
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()


def _limit_in_fresh_interpreter(tmp_path, value):
    return int(_run_probe(tmp_path, _PROBE, value)[0])


def test_defaults_to_20_when_unset(tmp_path):
    assert _limit_in_fresh_interpreter(tmp_path, None) == 20


def test_env_override_raises_the_window(tmp_path):
    assert _limit_in_fresh_interpreter(tmp_path, "64") == 64


@pytest.mark.parametrize("value", ["bogus", "0", "-5"])
def test_invalid_or_nonpositive_falls_back_to_default(tmp_path, value):
    assert _limit_in_fresh_interpreter(tmp_path, value) == 20


def test_values_above_200_are_clamped(tmp_path):
    assert _limit_in_fresh_interpreter(tmp_path, "500") == 200


def test_route_cap_follows_the_shared_value(tmp_path):
    lines = _run_probe(tmp_path, _PROBE, "8")
    assert lines[0] == "8"
    assert lines[1] == "8"
    assert lines[2] == "8"


def test_profile_env_cannot_override_visible_session_limit(tmp_path):
    lines = _run_probe(tmp_path, _PROFILE_PROBE, "40")
    assert lines[0] == "PROTECTED True"
    assert lines[1] == "ENV 40"
    assert lines[2] == "LIMIT 40"


def test_setting_is_documented():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "| `HERMES_WEBUI_VISIBLE_SESSION_LIMIT` | `20` |" in readme
    assert "# HERMES_WEBUI_VISIBLE_SESSION_LIMIT=20" in env_example
    assert "200" in readme
    assert "clamped" in env_example
