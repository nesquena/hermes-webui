"""Regression tests for the #4449 state-dir contract folded into #4454.

`api.config.STATE_DIR` is a module-level global computed from the environment at
import time. Exercising how it's derived requires importing config under a
specific HERMES_HOME / HERMES_WEBUI_STATE_DIR. We do this in a SUBPROCESS rather
than `importlib.reload(config)` in-process: reloading config inside the shared
pytest process leaks recomputed globals (STATE_DIR/SESSION_DIR) and stale
references into later tests (the cancel/stream/health/state-isolation suites),
which is fragile no matter how carefully env is restored. A subprocess gives a
truly isolated import and can't pollute the test process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = "import api.config as c; print(c.STATE_DIR)"


def _state_dir_for_env(platform_home: Path, **env_overrides) -> Path:
    """Import api.config in a fresh subprocess under the given env and return
    the resolved STATE_DIR it computes. None-valued overrides unset the var."""
    env = dict(os.environ)
    # Keep every probe inside fixture-owned platform paths. Inheriting the real
    # user home lets an import-only probe reconcile pytest workspace defaults
    # into the user's settings.json before it prints STATE_DIR.
    env["HOME"] = str(platform_home)
    env["USERPROFILE"] = str(platform_home)
    env["LOCALAPPDATA"] = str(platform_home / "AppData" / "Local")
    # Start from a clean slate for the vars that can redirect state or trigger
    # startup settings reconciliation in the child process.
    env.pop("HERMES_HOME", None)
    env.pop("HERMES_BASE_HOME", None)
    env.pop("HERMES_WEBUI_STATE_DIR", None)
    env.pop("HERMES_WEBUI_TEST_STATE_DIR", None)
    env.pop("HERMES_WEBUI_DEFAULT_WORKSPACE", None)
    env.pop("HERMES_CONFIG_PATH", None)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, f"probe failed: {out.stderr}"
    return Path(out.stdout.strip()).resolve()


def _platform_default_state_dir(platform_home: Path) -> Path:
    """What STATE_DIR resolves to with HERMES_HOME + STATE_DIR both unset."""
    return _state_dir_for_env(platform_home)


def test_state_dir_probe_does_not_mutate_platform_default_settings(
    tmp_path, monkeypatch
):
    """The subprocess probe must not reconcile pytest defaults into user state."""
    platform_home = tmp_path / "platform-home"
    if os.name == "nt":
        settings_file = (
            platform_home / "AppData" / "Local" / "hermes" / "webui" / "settings.json"
        )
    else:
        settings_file = platform_home / ".hermes" / "webui" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    persisted_workspace = tmp_path / "persisted-workspace"
    persisted_workspace.mkdir()
    original = (
        json.dumps({"default_workspace": str(persisted_workspace)}) + "\n"
    ).encode()
    settings_file.write_bytes(original)

    monkeypatch.setenv("HOME", str(platform_home))
    monkeypatch.setenv("USERPROFILE", str(platform_home))
    monkeypatch.setenv(
        "HERMES_WEBUI_DEFAULT_WORKSPACE", str(tmp_path / "pytest-workspace")
    )

    state_dir = _platform_default_state_dir(platform_home)

    assert settings_file.read_bytes() == original
    assert state_dir == settings_file.parent.resolve()


def test_config_state_dir_defaults_to_hermes_home_webui(tmp_path):
    hermes_home = tmp_path / ".hermes" / "profiles" / "isolated"
    hermes_home.mkdir(parents=True)

    state_dir = _state_dir_for_env(tmp_path / "platform-home", HERMES_HOME=hermes_home)
    assert state_dir == (hermes_home / "webui").resolve()


def test_config_state_dir_unchanged_for_normal_install_hermes_home_unset(tmp_path):
    """Backward-compat: with HERMES_HOME unset, STATE_DIR stays at the platform
    default `<~/.hermes>/webui` — a normal install's state must NOT relocate
    (the #4449/#4454 state-dir move only affects an explicitly-set HERMES_HOME).

    Cross-check: the unset-HERMES_HOME result must NOT equal the result of
    pointing HERMES_HOME at an arbitrary other base — i.e. the default is
    genuinely the platform home, not whatever the test environment injected."""
    platform_home = tmp_path / "platform-home"
    default = _platform_default_state_dir(platform_home)
    assert default.name == "webui"
    # Pointing HERMES_HOME elsewhere produces a DIFFERENT dir, proving the unset
    # case resolves to the platform default rather than echoing an injected base.
    elsewhere_base = tmp_path / "elsewhere-base"
    elsewhere = _state_dir_for_env(platform_home, HERMES_HOME=elsewhere_base)
    assert elsewhere == (elsewhere_base / "webui").resolve()
    assert default != elsewhere


def test_config_state_dir_explicit_override_takes_precedence(tmp_path):
    """HERMES_WEBUI_STATE_DIR always wins over the HERMES_HOME-derived default,
    so an operator who pinned a state dir keeps it even in isolated mode."""
    hermes_home = tmp_path / ".hermes" / "profiles" / "isolated"
    hermes_home.mkdir(parents=True)
    explicit = tmp_path / "custom-state"

    state_dir = _state_dir_for_env(
        tmp_path / "platform-home",
        HERMES_HOME=hermes_home,
        HERMES_WEBUI_STATE_DIR=explicit,
    )
    assert state_dir == explicit.resolve()
