"""#7843 — the documented `venv/bin/python server.py` launch must activate the
Agent's managed dependency set without the caller exporting HERMES_WEBUI_AGENT_DIR.

#7895 fixed the re-exec sys.path loss and made the dependency activation happen
before any WebUI import, but it kept the hook reading the Agent location from
one variable alone. The README's manual-launch snippet
(`cd /path/to/hermes-agent; venv/bin/python /path/to/hermes-webui/server.py`)
exports nothing, so activate_managed_agent() returned before its first action
and the WebUI died on `import yaml` in api/profiles.py -- the managed runtime's
dependency set is only ever attached by the Agent's own bootstrap.

api.config already resolves the same Agent with _discover_agent_identity(); the
hook must fall back to that instead of going blind when the variable is unset.
"""

import os
import subprocess
import sys

WEBUI_ROOT = os.path.dirname(os.path.dirname(__file__))


def _managed_agent(fixture, *, bootstrap=True):
    """An Agent whose bootstrap activates the PM-managed dependency set."""
    agent_dir = fixture / "hermes-home" / "hermes-agent"
    pm_site = agent_dir / "pm-site-packages"
    pm_site.mkdir(parents=True)
    if bootstrap:
        (agent_dir / "hermes_bootstrap.py").write_text(
            "import os, sys\n"
            "os.environ['PM_DEPS_ACTIVATED'] = '1'\n"
            f"sys.path.insert(0, {str(pm_site)!r})\n",
            encoding="utf-8",
        )
    (agent_dir / "run_agent.py").write_text("class AIAgent: pass\n", encoding="utf-8")
    (agent_dir / "hermes_cli").mkdir()
    (agent_dir / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\n", encoding="utf-8"
    )
    return agent_dir


# Stop at the first WebUI module import: that is the seam where api/profiles.py
# needs PyYAML, and it keeps the host's real Agent out of the run.
_SEAM_SCRIPT = (
    "import builtins, os, sys\n"
    "sys.path.insert(0, %r)\n"  # a script launch puts the SCRIPT's dir on sys.path
    "original = builtins.__import__\n"
    "def check(name, *args, **kwargs):\n"
    "    if name == 'api.request_logging':\n"
    "        raise SystemExit(0 if os.environ.get('PM_DEPS_ACTIVATED') == '1' else 3)\n"
    "    return original(name, *args, **kwargs)\n"
    "builtins.__import__ = check\n"
    "import server\n"
    "raise SystemExit(9)\n"
) % WEBUI_ROOT


def _launch(agent_dir, extra_env):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("HERMES_WEBUI_AGENT_DIR", None)
    env.pop("PM_DEPS_ACTIVATED", None)
    env["HERMES_HOME"] = str(agent_dir.parent)
    env["HERMES_WEBUI_PYTHON"] = ""
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", _SEAM_SCRIPT],
        cwd=str(agent_dir), env=env, capture_output=True, text=True, timeout=60,
    )


def test_documented_launch_activates_managed_deps_without_the_env_var(tmp_path):
    """The README manual launch exports nothing; discovery must still find the Agent."""
    agent_dir = _managed_agent(tmp_path)

    result = _launch(agent_dir, {})

    assert result.returncode == 0, (
        "activate_managed_agent() no-opped without HERMES_WEBUI_AGENT_DIR, so the "
        "Agent's bootstrap never attached the managed dependency set and the WebUI "
        "dies on `import yaml` (api/profiles.py).\n"
        f"agent_dir={agent_dir}\nstderr:\n{result.stderr}"
    )


def test_explicit_env_var_still_wins_over_discovery(tmp_path):
    """Precedence is unchanged: an exported Agent dir is used as-is."""
    discovered = _managed_agent(tmp_path / "home")
    explicit = _managed_agent(tmp_path / "explicit", bootstrap=False)
    explicit_marker = explicit / "explicit-marker"

    result = _launch(
        explicit,
        {"HERMES_HOME": str(discovered.parent), "HERMES_WEBUI_AGENT_DIR": str(explicit)},
    )

    # The explicit Agent has no bootstrap, so PM_DEPS_ACTIVATED stays unset and
    # the seam reports 3 -- which is only correct if discovery did NOT override
    # the export with the discoverable Agent (which would have activated it).
    assert result.returncode == 3, (
        f"discovery overrode the exported HERMES_WEBUI_AGENT_DIR.\n{result.stderr}"
    )
    assert not explicit_marker.exists()


def test_browser_only_empty_agent_dir_still_no_ops(tmp_path):
    """An exported empty Agent directory is a deliberate browser-only signal."""
    empty = tmp_path / "home" / "hermes-agent"
    empty.mkdir(parents=True)
    # A real Agent exists at the discoverable default and must NOT be picked up.
    _managed_agent(tmp_path / "home")

    result = _launch(empty, {"HERMES_WEBUI_AGENT_DIR": str(empty)})

    # No activation: the browser-only export wins over the discoverable Agent.
    assert result.returncode == 3, (
        "an exported empty Agent directory must keep the hook a no-op; discovery "
        f"must not substitute a different Agent.\n{result.stderr}"
    )


def test_no_agent_anywhere_still_reaches_the_webui_import_seam(tmp_path):
    """Discovery finding nothing must not break startup."""
    hermes_home = tmp_path / "empty-home"
    hermes_home.mkdir()

    result = _launch(hermes_home, {})

    # 3 = seam reached, deps absent (correct: there is no Agent to activate).
    # 9 = the hook raised before WebUI could import anything.
    assert result.returncode != 9, (
        f"activate_managed_agent() raised with no Agent present.\n{result.stderr}"
    )


def test_broken_bootstrap_still_warns_and_continues(tmp_path):
    """A broken Agent bootstrap must not stop WebUI startup."""
    agent_dir = _managed_agent(tmp_path)
    (agent_dir / "hermes_bootstrap.py").write_text(
        "import deliberately_missing_agent_dependency\n", encoding="utf-8"
    )

    result = _launch(agent_dir, {})

    assert "Hermes Agent dependency activation failed" in result.stderr, result.stderr
    assert "deliberately_missing_agent_dependency" in result.stderr, result.stderr