"""#7843 — the documented `venv/bin/python server.py` launch must activate the
Agent's managed dependency set without the caller exporting HERMES_WEBUI_AGENT_DIR.

#7895 fixed the re-exec sys.path loss and made the dependency activation happen
before any WebUI import, but it kept the hook reading the Agent location from
one variable alone. The README's manual-launch snippet
(`cd /path/to/hermes-agent; venv/bin/python /path/to/hermes-webui/server.py`)
exports nothing, so activate_managed_agent() returned before its first action
and the WebUI died on `import yaml` in api/profiles.py -- the managed runtime's
dependency set is only ever attached by the Agent's own bootstrap.

api.config already resolves the same Agent; the hook must fall back to that same
discovery (api.startup.discover_runtime_agent_identity) instead of going blind
when the variable is unset.
"""

import pytest

from tests._hermetic_agent_launch import copy_webui, hermetic_env, run_guarded


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
    # A PM launcher (v0.21.6+): the only Agents activated without the env var.
    (agent_dir / "hermes_cli" / "venv_sync.py").write_text("", encoding="utf-8")
    (agent_dir / "pm").mkdir()
    (agent_dir / "pm" / "environments.py").write_text("", encoding="utf-8")
    (agent_dir / "pyproject.toml").write_text(
        "[project]\nname='hermes-agent'\n", encoding="utf-8"
    )
    return agent_dir


@pytest.fixture(scope="module")
def webui(tmp_path_factory):
    """This WebUI, copied where neither `<repo>/..` nor `<repo>/../hermes-agent` is an Agent."""
    return copy_webui(tmp_path_factory.mktemp("isolated-webui"))


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
)


def _launch(webui, fixture_root, agent_dir, extra_env):
    # Hermetic (tests/_hermetic_agent_launch.py): discovery also probes the
    # WebUI checkout's parent, ~/.hermes, ~/hermes-agent, XDG data, system
    # paths, a `hermes` launcher on PATH and the selected interpreter's
    # site-packages (the test interpreter may be a real Agent's venv). Without
    # this a real host Agent would be found and its bootstrap run against the
    # temp home; the pre-flight skips if one cannot be hidden.
    env = hermetic_env(fixture_root, **{"HERMES_HOME": str(agent_dir.parent), **extra_env})
    result, _resolved = run_guarded(
        _SEAM_SCRIPT % str(webui), webui=webui, env=env, cwd=agent_dir, timeout=60,
    )
    return result


def test_documented_launch_activates_managed_deps_without_the_env_var(tmp_path, webui):
    """The README manual launch exports nothing; discovery must still find the Agent."""
    agent_dir = _managed_agent(tmp_path)

    result = _launch(webui, tmp_path, agent_dir, {})

    assert result.returncode == 0, (
        "activate_managed_agent() no-opped without HERMES_WEBUI_AGENT_DIR, so the "
        "Agent's bootstrap never attached the managed dependency set and the WebUI "
        "dies on `import yaml` (api/profiles.py).\n"
        f"agent_dir={agent_dir}\nstderr:\n{result.stderr}"
    )


def test_explicit_env_var_still_wins_over_discovery(tmp_path, webui):
    """Precedence is unchanged: an exported Agent dir is used as-is."""
    discovered = _managed_agent(tmp_path / "home")
    explicit = _managed_agent(tmp_path / "explicit", bootstrap=False)
    explicit_marker = explicit / "explicit-marker"

    result = _launch(
        webui, tmp_path, explicit,
        {"HERMES_HOME": str(discovered.parent), "HERMES_WEBUI_AGENT_DIR": str(explicit)},
    )

    # The explicit Agent has no bootstrap, so PM_DEPS_ACTIVATED stays unset and
    # the seam reports 3 -- which is only correct if discovery did NOT override
    # the export with the discoverable Agent (which would have activated it).
    assert result.returncode == 3, (
        f"discovery overrode the exported HERMES_WEBUI_AGENT_DIR.\n{result.stderr}"
    )
    assert not explicit_marker.exists()


def test_browser_only_empty_agent_dir_still_no_ops(tmp_path, webui):
    """An exported empty Agent directory is a deliberate browser-only signal."""
    empty = tmp_path / "browser-only" / "hermes-agent"
    empty.mkdir(parents=True)
    # A real Agent exists where discovery WOULD find it ($HERMES_HOME/hermes-agent)
    # and must NOT be picked up.
    discoverable = _managed_agent(tmp_path / "home")

    result = _launch(
        webui, tmp_path, empty,
        {"HERMES_WEBUI_AGENT_DIR": str(empty), "HERMES_HOME": str(discoverable.parent)},
    )

    # No activation: the browser-only export wins over the discoverable Agent.
    assert result.returncode == 3, (
        "an exported empty Agent directory must keep the hook a no-op; discovery "
        f"must not substitute a different Agent.\n{result.stderr}"
    )


def test_no_agent_anywhere_still_reaches_the_webui_import_seam(tmp_path, webui):
    """Discovery finding nothing must not break startup."""
    hermes_home = tmp_path / "empty-home"
    hermes_home.mkdir()

    result = _launch(webui, tmp_path, hermes_home, {})

    # 3 = seam reached, deps absent (correct: there is no Agent to activate).
    # Anything else (9, or a bootstrap's SystemExit) means the hook did not
    # return cleanly before WebUI's first import.
    assert result.returncode == 3, (
        f"activate_managed_agent() did not return cleanly with no Agent present.\n{result.stderr}"
    )


def test_broken_bootstrap_still_warns_and_continues(tmp_path, webui):
    """A broken Agent bootstrap must not stop WebUI startup."""
    agent_dir = _managed_agent(tmp_path)
    (agent_dir / "hermes_bootstrap.py").write_text(
        "import deliberately_missing_agent_dependency\n", encoding="utf-8"
    )

    result = _launch(webui, tmp_path, agent_dir, {})

    assert "Hermes Agent dependency activation failed" in result.stderr, result.stderr
    assert "deliberately_missing_agent_dependency" in result.stderr, result.stderr