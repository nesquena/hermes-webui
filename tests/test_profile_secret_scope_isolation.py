"""A profile's secret scope reads that profile's values, not another profile's live env.

While a named profile's background worker, chat turn or settings save runs, WebUI applies that
profile's ``.env`` to the shared process env. The Hermes Agent builds the launch profile's
secret scope (plugin secret refresh, cron fire scope, MCP owner mapping) from its launch env,
which, with multiplexing off, is the live process env. WebUI therefore:

- installs ``api.profiles.operator_env()`` as that launch env at startup: the launch-time
  snapshot without the values a launcher sourced from a NAMED launch profile's ``.env``, nor
  those it sourced from the ROOT ``.env``, which are taken from the current file instead (root's
  scope reads it itself; a saved active named profile gets those keys from it each time its
  launch env is built), so a key changed or removed in Settings applies without a restart;
- keeps the launch-only ``HERMES_MANAGED_DIR`` out of every profile ``.env`` path.

The launch snapshot and the startup install are import-time state, so each scenario runs in a
fresh interpreter with an explicit minimal env, a synthetic home and ``DUMMY_*`` values only.
Scenarios that drive Agent scope builders skip when the Agent is not importable.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from api import profiles

REPO = Path(__file__).resolve().parents[1]

try:
    import tui_gateway.launch_profile_policy as _LPP
except ImportError:
    _LPP = None
try:  # every Agent name the scenario children import: an Agent without one skips them
    from agent.secret_scope import current_secret_scope, get_secret, refresh_installed_secret_scope  # noqa: F401
    from hermes_constants import get_hermes_home  # noqa: F401
    from tui_gateway.launch_profile_policy import _launch_env, launch_secret_scope  # noqa: F401
    _AGENT_SCOPES = True
except ImportError:
    _AGENT_SCOPES = False

AGENT_DIR = Path(_LPP.__file__).resolve().parents[1] if _LPP is not None else None
needs_agent_scopes = pytest.mark.skipif(not _AGENT_SCOPES, reason="Hermes Agent launch-profile scopes not importable")


def _refresh_rebuilds_through_owner_scope():
    """Whether this Agent's scope refresh rebuilds through ``owner_secret_scope`` (v0.23.2 and later).

    That path carries the launch environment WebUI installs. Earlier Agents (v0.23.1) rebuild a refreshed
    scope from the profile's files only, so no launch value can reach it, with or without this change. When
    the source can't be read, assume the newer path so the test runs rather than skipping silently.
    """
    if not _AGENT_SCOPES:
        return False
    import inspect
    from agent.secret_scope import refresh_installed_secret_scope
    try:
        return "owner_secret_scope" in inspect.getsource(refresh_installed_secret_scope)
    except (OSError, TypeError):
        return True


needs_owner_scope_refresh = pytest.mark.skipif(
    not _refresh_rebuilds_through_owner_scope(),
    reason="this Agent's scope refresh rebuilds from the profile's files only (before v0.23.2), "
           "so launch values never reach it, with or without this change",
)


CHILD = r'''
import json, os, sys, threading
from pathlib import Path

repo, scenario, run = sys.argv[1], sys.argv[2], Path(sys.argv[3])
updates = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
sys.path.insert(0, repo)
if os.environ.get("HERMES_WEBUI_AGENT_DIR"):
    sys.path.insert(1, os.environ["HERMES_WEBUI_AGENT_DIR"])
os.chdir(repo)


OWNERS = {"DUMMY_KEY_PROFILE_A": "alpha", "DUMMY_KEY_PROFILE_B": "beta",
          "DUMMY_OPERATOR_TOKEN": "operator", "DUMMY_OPERATOR_OVERRIDE": "operator-override",
          "DUMMY_ROOT_KEY": "root", "DUMMY_ROOT_KEY_NEW": "root-new",
          "DUMMY_ROOT\tKEY": "root-decoded", "DUMMY_ROOT\\tKEY": "root-literal",
          "DUMMY_KEY\tPROFILE_A": "alpha-decoded", "DUMMY_KEY\\tPROFILE_A": "alpha-literal"}


def owner(value):
    if value is None:
        return None
    value = str(value)
    if value.startswith(str(run)):
        return "dir:" + Path(value).name
    return OWNERS.get(value, "other")


obs = {}
try:
    import api.config  # noqa: F401  (real startup: init_profile_state)
    from api import profiles as P

    def hold_worker(profile):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()

        def body():
            try:
                with P.profile_env_for_background_worker(profile, "concurrent worker"):
                    entered.set()
                    release.wait(30)
            finally:
                entered.set()
                done.set()

        threading.Thread(target=body, daemon=True).start()
        assert entered.wait(30), "worker did not start"
        return release, done

    def read_scope(get_secret, current_secret_scope):
        obs["scope"] = owner((current_secret_scope() or {}).get("OPENROUTER_API_KEY"))
        obs["get_secret"] = owner(get_secret("OPENROUTER_API_KEY"))
        obs["operator"] = owner(get_secret("OPERATOR_ONLY_TOKEN"))
        obs["operator_scope"] = owner((current_secret_scope() or {}).get("OPERATOR_ONLY_TOKEN"))

    if scenario == "named_launch_refresh":
        import hermes_constants as HC
        from agent.secret_scope import current_secret_scope, get_secret, refresh_installed_secret_scope
        with P.profile_env_for_background_worker("alpha", "launch profile body"):
            release, done = hold_worker("beta")
            obs["live_during"] = owner(os.environ.get("OPENROUTER_API_KEY"))
            obs["refreshed"] = bool(refresh_installed_secret_scope(Path(HC.get_hermes_home())))
            release.set()
            assert done.wait(30)
            obs["live_after"] = owner(os.environ.get("OPENROUTER_API_KEY"))
            read_scope(get_secret, current_secret_scope)
            obs["own"] = owner(get_secret("ALPHA_OWN_KEY"))

    elif scenario in ("root_fire_during_worker", "root_fire_live_only_value"):
        import cron.scheduler as CS
        from agent.secret_scope import current_secret_scope, get_secret
        if not hasattr(CS, "_install_fire_secret_scope"):
            print("RESULT " + json.dumps({"skip": "Agent cron fire scope not available"}), flush=True)
            os._exit(0)
        if scenario == "root_fire_during_worker":
            release, done = hold_worker("beta")
        else:  # any writer that holds a value in the live env while the scope is built
            os.environ["OPENROUTER_API_KEY"] = "DUMMY_KEY_PROFILE_B"
        obs["live_during"] = owner(os.environ.get("OPENROUTER_API_KEY"))
        with P.cron_profile_context_for_home(P._DEFAULT_HERMES_HOME):
            tokens = CS._install_fire_secret_scope()
        try:
            if scenario == "root_fire_during_worker":
                release.set()
                assert done.wait(30)
            else:
                os.environ.pop("OPENROUTER_API_KEY", None)
            obs["live_after"] = owner(os.environ.get("OPENROUTER_API_KEY"))
            read_scope(get_secret, current_secret_scope)
        finally:
            CS._reset_fire_secret_scope(tokens)

    elif scenario == "managed_dir":
        release, done = hold_worker("beta")
        obs["during_worker"] = owner(os.environ.get("HERMES_MANAGED_DIR"))
        obs["worker_key"] = owner(os.environ.get("BETA_OWN_KEY"))
        release.set()
        assert done.wait(30)
        P.set_request_profile("beta")
        try:
            P._reload_dotenv(P.get_active_hermes_home())
        finally:
            P.clear_request_profile()
        obs["after_reload"] = owner(os.environ.get("HERMES_MANAGED_DIR"))
        obs["reload_key"] = owner(os.environ.get("BETA_OWN_KEY"))

    elif scenario in ("root_settings_save", "launch_settings_save"):
        import cron.scheduler as CS
        from agent.secret_scope import current_secret_scope, get_secret
        from tui_gateway import launch_profile_policy as LPP
        from api.providers import _write_env_file
        if not hasattr(CS, "_install_fire_secret_scope"):
            print("RESULT " + json.dumps({"skip": "Agent cron fire scope not available"}), flush=True)
            os._exit(0)
        home = P.get_process_profile_home()  # the launch profile
        obs["startup_hermes_home"] = P._INITIAL_HERMES_HOME
        obs["launch_profile"] = "root" if P._is_root_profile_home(home) else home.name
        obs["live_before"] = owner(os.environ.get("OPENROUTER_API_KEY"))
        obs["launch_scope_before"] = owner(LPP.launch_secret_scope(home).get("OPENROUTER_API_KEY"))
        saved_home = P._DEFAULT_HERMES_HOME if scenario == "root_settings_save" else home
        _write_env_file(saved_home / ".env", updates)  # the Settings save path
        obs["live_after"] = owner(os.environ.get("OPENROUTER_API_KEY"))
        obs["launch_env"] = owner(LPP._launch_env().get("OPENROUTER_API_KEY"))
        launch_scope = LPP.launch_secret_scope(home)
        obs["launch_scope"] = owner(launch_scope.get("OPENROUTER_API_KEY"))
        obs["own"] = owner(launch_scope.get("ALPHA_OWN_KEY"))
        obs["shared"] = owner(launch_scope.get("LITELLM_API_KEY"))
        with P.cron_profile_context_for_home(home):
            tokens = CS._install_fire_secret_scope()
        try:
            read_scope(get_secret, current_secret_scope)
        finally:
            CS._reset_fire_secret_scope(tokens)

    elif scenario == "operator_env":
        env = P.operator_env()
        obs["launch_dotenv_key"] = owner(env.get("LITELLM_API_KEY"))
        obs["launch_dotenv_and_export"] = owner(env.get("LITELLM_BASE_URL"))
        obs["operator"] = owner(env.get("OPERATOR_ONLY_TOKEN"))
        obs["live_launch_key"] = owner(os.environ.get("LITELLM_API_KEY"))
    else:
        obs["error"] = "unknown scenario"
except Exception as exc:  # surfaced by the parent assertion
    import traceback
    obs["error"] = repr(exc) + traceback.format_exc()[-1200:]
print("RESULT " + json.dumps(obs, sort_keys=True), flush=True)
os._exit(0)
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(tmp_path, scenario, *, launch, beta=None, alpha=None, root_env=None, extra_env=None, updates=None):
    """Build a synthetic root + ``alpha`` + ``beta`` tree and run *scenario* in a fresh child.

    ``launch="<name>-default"`` launches with ``HERMES_HOME`` unset (the default home under ``HOME``) and
    ``<name>`` as the active profile (``root-default``: root)."""
    run = tmp_path / "run"
    root = run / "home" / ".hermes"
    for d in (root, run / "tmp", run / "state"):
        d.mkdir(parents=True, exist_ok=True)
    _write(root / "config.yaml", "model:\n  default: dummy-model\n")
    root_values = {"ROOT_OWN_KEY": "DUMMY_ROOT_KEY", **(root_env or {})}
    _write(root / ".env", "".join(f"{k}={v}\n" for k, v in root_values.items()))
    envs = {
        "alpha": {"ALPHA_OWN_KEY": "DUMMY_KEY_PROFILE_A", **(alpha or {})},
        "beta": {"OPENROUTER_API_KEY": "DUMMY_KEY_PROFILE_B", "BETA_OWN_KEY": "DUMMY_KEY_PROFILE_B", **(beta or {})},
    }
    for name, values in envs.items():
        home = root / "profiles" / name
        for sub in ("memories", "sessions", "skills", "logs", "cron", "workspace"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        _write(home / "config.yaml", "model:\n  default: dummy-model\n")
        _write(home / ".env", "".join(f"{k}={v}\n" for k, v in values.items()))
    active = launch[:-len("-default")] if launch.endswith("-default") else launch
    if active != "root":
        _write(root / "active_profile", active + "\n")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(run / "home"),
        "TMPDIR": str(run / "tmp"),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "HERMES_WEBUI_STATE_DIR": str(run / "state"),
        "HERMES_HOME": str(root if active == "root" else root / "profiles" / active),
        "OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN",
        # Any HTTP client the child might construct goes to a closed local port.
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
    }
    if launch.endswith("-default"):
        del env["HERMES_HOME"]
    if AGENT_DIR is not None:
        env["HERMES_WEBUI_AGENT_DIR"] = str(AGENT_DIR)
    env.update(extra_env or {})
    cp = subprocess.run(
        [sys.executable, "-c", CHILD, str(REPO), scenario, str(run), json.dumps(updates or {})],
        env=env, cwd=str(REPO), capture_output=True, text=True, timeout=180,
    )
    lines = [line for line in cp.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"child produced no result (rc={cp.returncode}): {cp.stderr[-1500:]}"
    obs = json.loads(lines[-1][len("RESULT "):])
    if "skip" in obs:
        pytest.skip(obs["skip"])
    assert "error" not in obs, obs["error"]
    return obs, run


# 1 ── launch-profile scope builds keep their own values while another profile's worker runs ──


@needs_agent_scopes
def test_named_launch_scope_refresh_ignores_a_concurrent_workers_values(tmp_path):
    obs, _ = _run(tmp_path, "named_launch_refresh", launch="alpha")
    assert obs["live_during"] == "beta", "precondition: the other profile's worker applies its values"
    assert obs["refreshed"] is True
    assert obs["live_after"] is None
    assert obs["scope"] is None, "the launch profile's refreshed scope holds another profile's value"
    assert obs["get_secret"] is None
    assert obs["own"] == "alpha", "the launch profile lost its own value"
    assert obs["operator"] == "operator", "the launch profile lost an operator-only value"


@needs_agent_scopes
@needs_owner_scope_refresh
def test_named_launch_scope_keeps_an_operator_value_the_root_env_also_holds(tmp_path):
    # Only a ROOT launch sources the root .env; on a named launch an equal value is the operator's.
    obs, _ = _run(tmp_path, "named_launch_refresh", launch="alpha",
                  root_env={"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"})
    assert obs["refreshed"] is True
    assert obs["operator_scope"] == "operator", "the named launch scope lost an operator value"


@needs_agent_scopes
@needs_owner_scope_refresh
def test_active_named_profile_scope_takes_the_root_env_values_its_launcher_exported_from_the_file(tmp_path):
    # HERMES_HOME unset: the launcher sources the root .env, but the active (launch) profile is alpha, whose
    # scope does not read the root .env; those launch values come from the current root .env (section 4b).
    obs, _ = _run(tmp_path, "named_launch_refresh", launch="alpha-default",
                  root_env={"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"})
    assert obs["refreshed"] is True
    assert obs["operator_scope"] == "operator", "the active profile's launch scope lost a launch value"


@needs_agent_scopes
@pytest.mark.parametrize("scenario", ["root_fire_during_worker", "root_fire_live_only_value"])
def test_root_cron_fire_scope_ignores_values_only_the_live_env_holds(tmp_path, scenario):
    obs, _ = _run(tmp_path, scenario, launch="root")
    assert obs["live_during"] == "beta", "precondition: the live env holds another profile's value"
    assert obs["live_after"] is None
    assert obs["scope"] is None, "the root fire scope holds another profile's value"
    assert obs["get_secret"] is None
    assert obs["operator"] == "operator", "the root fire scope lost an operator-only value"


# 2 ── the launch-only managed directory never follows a profile .env ──


def test_managed_dir_stays_the_operators_through_worker_and_reload(tmp_path):
    run = tmp_path / "run"
    operator_dir, profile_dir = run / "managed-op", run / "managed-beta"
    operator_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    obs, _ = _run(
        tmp_path, "managed_dir", launch="root",
        beta={"HERMES_MANAGED_DIR": str(profile_dir)},
        extra_env={"HERMES_MANAGED_DIR": str(operator_dir)},
    )
    assert obs["worker_key"] == "beta", "precondition: the worker applies the profile's values"
    assert obs["reload_key"] == "beta", "precondition: the reload applies the profile's values"
    assert obs["during_worker"] == "dir:managed-op"
    assert obs["after_reload"] == "dir:managed-op"


# 3 ── operator_env(): the launch snapshot without a named launch profile's sourced values ──


def test_operator_env_drops_values_sourced_from_a_named_launch_home(tmp_path):
    # A shell launcher sourced alpha's .env before Python started (values already exported);
    # LITELLM_BASE_URL was exported by the operator with a different value and is kept.
    obs, _ = _run(
        tmp_path, "operator_env", launch="alpha",
        alpha={"LITELLM_API_KEY": "DUMMY_KEY_PROFILE_A", "LITELLM_BASE_URL": "DUMMY_KEY_PROFILE_A"},
        extra_env={"LITELLM_API_KEY": "DUMMY_KEY_PROFILE_A", "LITELLM_BASE_URL": "DUMMY_OPERATOR_OVERRIDE"},
    )
    assert obs["live_launch_key"] == "alpha", "precondition: the launch profile's value is in the process env"
    assert obs["launch_dotenv_key"] is None, "operator_env() holds the named launch profile's value"
    assert obs["launch_dotenv_and_export"] == "operator-override"
    assert obs["operator"] == "operator"


# 4 ── a root launch whose launcher sourced the root .env: Settings changes apply without a restart ──
# ctl.sh exports the root .env's values before Python starts (simulated here: the same value in the
# launch env). The frozen launch env leaves them out, so root's scope takes them from the file.


@needs_agent_scopes
@pytest.mark.parametrize("launch", ["root", "root-default"])
def test_root_launch_scope_drops_a_key_removed_in_settings(tmp_path, launch):
    obs, _ = _run(
        tmp_path, "root_settings_save", launch=launch,
        root_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"},
        extra_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"},
        updates={"OPENROUTER_API_KEY": None},
    )
    if launch == "root-default":
        assert obs["startup_hermes_home"] == "", "precondition: HERMES_HOME unset at launch (default home)"
    assert obs["live_before"] == "root", "precondition: the launcher exported the root .env value"
    assert obs["live_after"] is None
    assert obs["launch_env"] is None, "the frozen launch env keeps a key removed from the root .env"
    assert obs["launch_scope"] is None, "root's launch scope keeps a key removed in Settings"
    assert obs["scope"] is None, "root's cron fire scope keeps a key removed in Settings"
    assert obs["get_secret"] is None
    assert obs["operator"] == "operator", "the root scope lost an operator-only value"


@needs_agent_scopes
def test_root_launch_scope_follows_a_key_changed_in_settings(tmp_path):
    obs, _ = _run(
        tmp_path, "root_settings_save", launch="root",
        root_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"},
        extra_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"},
        updates={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY_NEW"},
    )
    assert obs["live_before"] == "root", "precondition: the launcher exported the root .env value"
    assert obs["launch_scope"] == "root-new"
    assert obs["scope"] == "root-new"
    assert obs["get_secret"] == "root-new"
    assert obs["operator"] == "operator"


@needs_agent_scopes
def test_root_launch_keeps_an_operator_export_that_differs_from_the_root_env(tmp_path):
    # The launcher keeps a value the operator exported before it sourced the root .env.
    obs, _ = _run(
        tmp_path, "root_settings_save", launch="root",
        root_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"},
        extra_env={"OPENROUTER_API_KEY": "DUMMY_OPERATOR_OVERRIDE"},
        updates={"OPENROUTER_API_KEY": None},
    )
    assert obs["launch_env"] == "operator-override", "the operator's own export left the frozen launch env"
    assert obs["launch_scope"] == "operator-override"
    assert obs["scope"] == "operator-override"
    assert obs["get_secret"] == "operator-override"


@needs_agent_scopes
def test_root_launch_scope_drops_a_quoted_key_with_an_inline_comment_removed_in_settings(tmp_path):
    # The launcher decodes the quoted value without its comment, as the Agent's .env reader does.
    obs, _ = _run(
        tmp_path, "root_settings_save", launch="root",
        root_env={"OPENROUTER_API_KEY": '"DUMMY_ROOT_KEY" # provider key'},
        extra_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"},
        updates={"OPENROUTER_API_KEY": None},
    )
    assert obs["launch_scope_before"] == "root", "precondition: the root .env value as the launcher decodes it"
    assert obs["live_after"] is None
    assert obs["launch_env"] is None, "the frozen launch env keeps a key removed from the root .env"
    assert obs["launch_scope"] is None, "root's launch scope keeps a key removed in Settings"
    assert obs["scope"] is None, "root's cron fire scope keeps a key removed in Settings"
    assert obs["get_secret"] is None


@needs_agent_scopes
def test_named_launch_scope_drops_a_quoted_key_with_an_inline_comment_removed_in_settings(tmp_path):
    # A named launch home's .env, decoded by the launcher without its comment.
    obs, _ = _run(
        tmp_path, "launch_settings_save", launch="alpha",
        alpha={"OPENROUTER_API_KEY": '"DUMMY_KEY_PROFILE_A" # provider key'},
        extra_env={"OPENROUTER_API_KEY": "DUMMY_KEY_PROFILE_A"},
        updates={"OPENROUTER_API_KEY": None},
    )
    assert obs["launch_profile"] == "alpha", "precondition: the named profile is the launch profile"
    assert obs["launch_scope_before"] == "alpha", "precondition: the launch home's value as the launcher decodes it"
    assert obs["live_after"] is None
    assert obs["launch_env"] is None, "the frozen launch env keeps a key removed from the launch home's .env"
    assert obs["launch_scope"] is None, "the launch scope keeps a key removed in Settings"
    assert obs["scope"] is None, "the cron fire scope keeps a key removed in Settings"
    assert obs["get_secret"] is None


# 4b ── a saved active named profile with HERMES_HOME unset: the launcher sourced the ROOT .env ──
# Those launch values follow the current root .env (as the live env does without the install); the
# named profile's own .env still wins over them.


@needs_agent_scopes
@pytest.mark.parametrize(("update", "expected"), [("DUMMY_ROOT_KEY_NEW", "root-new"), (None, None)],
                         ids=["rotate", "delete"])
def test_active_named_profile_scope_follows_a_root_key_changed_in_settings(tmp_path, update, expected):
    obs, _ = _run(
        tmp_path, "root_settings_save", launch="alpha-default",
        root_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY", "LITELLM_API_KEY": "DUMMY_ROOT_KEY"},
        alpha={"LITELLM_API_KEY": "DUMMY_KEY_PROFILE_A"},
        extra_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY", "LITELLM_API_KEY": "DUMMY_ROOT_KEY"},
        updates={"OPENROUTER_API_KEY": update},
    )
    assert obs["startup_hermes_home"] == "", "precondition: HERMES_HOME unset at launch (default home)"
    assert obs["launch_profile"] == "alpha", "precondition: the saved active profile is the launch profile"
    assert obs["launch_scope_before"] == "root", "precondition: the launcher exported the root .env value"
    assert obs["live_after"] == expected
    assert obs["launch_scope"] == expected, "the launch scope keeps the root .env value from startup"
    assert obs["scope"] == expected, "the cron fire scope keeps the root .env value from startup"
    assert obs["get_secret"] == expected
    assert obs["own"] == "alpha", "the launch profile lost its own value"
    assert obs["shared"] == "alpha", "a root .env value replaced the launch profile's own value"
    assert obs["operator"] == "operator", "the launch scope lost an operator-only value"


# 4c ── a double-quoted root .env value with an escape: ctl.sh decodes \t (and \n \r) before exporting, the
# Agent's .env reader keeps it literal. The value must still be recognised as launcher-sourced.
ESCAPED_ROOT = {"OPENROUTER_API_KEY": '"DUMMY_ROOT\\tKEY"'}   # the file holds the two characters backslash-t
DECODED_EXPORT = {"OPENROUTER_API_KEY": "DUMMY_ROOT\tKEY"}     # what ctl.sh exported: a real tab


@needs_agent_scopes
def test_root_launch_scope_drops_an_escaped_key_removed_in_settings(tmp_path):
    obs, _ = _run(tmp_path, "root_settings_save", launch="root", root_env=ESCAPED_ROOT,
                  extra_env=DECODED_EXPORT, updates={"OPENROUTER_API_KEY": None})
    # WebUI's startup load puts the file's literal reading in the live env for a root launch; the frozen launch env
    # still holds the launcher's decoded export, which is what must not survive the delete.
    assert obs["launch_scope_before"] in ("root-decoded", "root-literal"), "precondition: the key is set before the delete"
    assert obs["live_after"] is None
    assert obs["launch_env"] is None, "the frozen launch env keeps an escaped key removed from the root .env"
    assert obs["launch_scope"] is None, "root's launch scope keeps an escaped key removed in Settings"
    assert obs["scope"] is None, "root's cron fire scope keeps an escaped key removed in Settings"
    assert obs["get_secret"] is None


@needs_agent_scopes
@needs_owner_scope_refresh
@pytest.mark.parametrize(("update", "expected"), [("DUMMY_ROOT_KEY_NEW", "root-new"), (None, None)],
                         ids=["rotate", "delete"])
def test_active_named_profile_scope_follows_an_escaped_root_key_changed_in_settings(tmp_path, update, expected):
    obs, _ = _run(tmp_path, "root_settings_save", launch="alpha-default", root_env=ESCAPED_ROOT,
                  extra_env=DECODED_EXPORT, updates={"OPENROUTER_API_KEY": update})
    assert obs["launch_profile"] == "alpha", "precondition: the saved active profile is the launch profile"
    assert obs["launch_scope_before"] == "root-decoded", "the launch scope changed the value the launcher exported"
    assert obs["launch_env"] == expected, "the launch env keeps the escaped root .env value from startup"
    assert obs["launch_scope"] == expected, "the launch scope keeps the escaped root .env value from startup"
    assert obs["scope"] == expected, "the cron fire scope keeps the escaped root .env value from startup"
    assert obs["operator"] == "operator", "the launch scope lost an operator-only value"


@needs_agent_scopes
def test_active_named_profile_scope_gets_the_launchers_value_for_a_trailing_escaped_newline(tmp_path):
    # ctl.sh decodes "...\n" inside $(...), which drops the trailing newline: the export has none.
    obs, _ = _run(tmp_path, "root_settings_save", launch="alpha-default",
                  root_env={"OPENROUTER_API_KEY": '"DUMMY_ROOT_KEY\\n"'},
                  extra_env={"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"},
                  updates={"LITELLM_API_KEY": "DUMMY_ROOT_KEY_NEW"})
    assert obs["launch_profile"] == "alpha", "precondition: the saved active profile is the launch profile"
    assert obs["launch_scope_before"] == "root", "the launch scope added a newline the launcher never exported"


# 4d ── an escaped value in a NAMED launch home's .env: ctl.sh exports it decoded, as for the root .env


@needs_agent_scopes
def test_named_launch_scope_drops_an_escaped_key_removed_in_settings(tmp_path):
    obs, _ = _run(tmp_path, "launch_settings_save", launch="alpha",
                  alpha={"OPENROUTER_API_KEY": '"DUMMY_KEY\\tPROFILE_A"'},
                  extra_env={"OPENROUTER_API_KEY": "DUMMY_KEY\tPROFILE_A"},
                  updates={"OPENROUTER_API_KEY": None})
    assert obs["launch_profile"] == "alpha", "precondition: the named profile is the launch profile"
    assert obs["launch_scope_before"] in ("alpha-decoded", "alpha-literal"), "precondition: the key is set before the delete"
    assert obs["live_after"] is None
    assert obs["launch_env"] is None, "the frozen launch env keeps an escaped key removed from the launch home's .env"
    assert obs["launch_scope"] is None, "the launch scope keeps an escaped key removed in Settings"
    assert obs["scope"] is None, "the cron fire scope keeps an escaped key removed in Settings"
    assert obs["get_secret"] is None


# 4e ── a server started after sourcing the .env in a shell (`set -a; . ~/.hermes/.env`): bash keeps `\t` literal
# inside double quotes and drops the inline comment, which is how the Agent's .env reader reads the line, not ctl.sh.
SHELL_SOURCED = {"root": ('"DUMMY_ROOT\\tKEY" # provider key', "DUMMY_ROOT\\tKEY"),
                 "alpha": ('"DUMMY_KEY\\tPROFILE_A" # provider key', "DUMMY_KEY\\tPROFILE_A")}


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("launch", sorted(SHELL_SOURCED))
def test_shell_sourcing_exports_the_value_the_shell_sourced_cases_use(tmp_path, launch):
    line, exported = SHELL_SOURCED[launch]
    env_file = tmp_path / "e.env"
    env_file.write_text(f"OPENROUTER_API_KEY={line}\n", encoding="utf-8")
    cp = subprocess.run(["bash", "-c", 'set -a; . "$1"; printf %s "$OPENROUTER_API_KEY"', "_", str(env_file)],
                        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == exported


@needs_agent_scopes
@pytest.mark.parametrize("launch", sorted(SHELL_SOURCED))
def test_launch_scope_drops_a_shell_sourced_key_removed_in_settings(tmp_path, launch):
    line, exported = SHELL_SOURCED[launch]
    scenario = "root_settings_save" if launch == "root" else "launch_settings_save"
    file_kw = {"root_env" if launch == "root" else "alpha": {"OPENROUTER_API_KEY": line}}
    obs, _ = _run(tmp_path, scenario, launch=launch, extra_env={"OPENROUTER_API_KEY": exported},
                  updates={"OPENROUTER_API_KEY": None}, **file_kw)
    assert obs["launch_profile"] == launch, "precondition: the launch profile"
    assert obs["launch_scope_before"] in (f"{launch}-decoded", f"{launch}-literal"), "precondition: the key is set"
    assert obs["live_after"] is None
    assert obs["launch_env"] is None, "the frozen launch env keeps a shell-sourced key removed from the .env"
    assert obs["launch_scope"] is None, "the launch scope keeps a shell-sourced key removed in Settings"
    assert obs["scope"] is None, "the cron fire scope keeps a shell-sourced key removed in Settings"
    assert obs["get_secret"] is None


# 5 ── the launcher reading must match ctl.sh's real decoding ──

LAUNCHER_CASES = [
    'A1="plain"', 'A2="tab\\there"', 'A3="nl\\n"', 'A4="two\\n\\n"', 'A5="mid\\nend"', 'A6="cr\\r"', 'A7="q\\"x"',
    'A8="bs\\\\"', 'A9="other\\zesc"', 'B1="v" # comment', 'B2="v"   #c', "B3='lit\\n'", "B4='s' # c", 'B5=unq # c',
    'B6=unq#nocomment', 'B7=  spaced  ', 'export B8="exp"', 'export   B9=e2', 'C1="trail\\"', 'C2="unterminated',
    'C3=', 'C4=""', '  C5="lead"', 'C 6=bad', 'C7="a"b', 'C8="tab\\t"', 'C9="x\\\\n"', 'D1="nl\\n" # c', 'D2=a b c',
    'D3="\\n"', 'D4="\\\\\\n"',
]


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
def test_launcher_reading_matches_ctl_sh_decoding(tmp_path):
    body = re.search(r"\n(_apply_env_file_safely\(\) \{.*?\n\})\n", (REPO / "ctl.sh").read_text(encoding="utf-8"), re.S)
    assert body, "ctl.sh no longer defines _apply_env_file_safely"
    env_file = tmp_path / "t.env"
    env_file.write_text("\n".join(LAUNCHER_CASES) + "\n", encoding="utf-8")
    script = tmp_path / "run.sh"
    script.write_text(body.group(1) + '\n_apply_env_file_safely "$1"\nexec "$2" -c '
                      '"import json, os; print(json.dumps(dict(os.environ)))"\n', encoding="utf-8")
    cp = subprocess.run(["bash", str(script), str(env_file), sys.executable], env={"PATH": "/usr/bin:/bin"},
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    exported = json.loads(cp.stdout)
    expected = {k: v for k, v in exported.items() if k[:1] in "ABCD" and k[1:].isdigit()}
    assert len(expected) == len(LAUNCHER_CASES), "precondition: ctl.sh exported every case"
    assert profiles._launcher_dotenv_values(env_file) == expected


# Line breaks, bytes and spaces bash's `read` loop treats differently from Python's text handling. Each file also
# has an escaped value (A1), which only the launcher reading decodes. In the C locale bash's [[:space:]] is ASCII; in
# a UTF-8 locale it is the host C library's blank set (glibc: not the no-break spaces; Darwin: those too), so each
# locale's export is compared with the reading for that locale. A NUL byte is dropped by bash 4 and later and ends
# the line in bash 3.2 (macOS's /bin/bash); the reading compared is the one for the bash these tests run.
LAUNCHER_BYTE_CASES = {
    "byte_not_utf8_in_comment": b"# DUMMY comment \xff\n",
    "byte_not_utf8_in_value": b"A2=DUMMY\xffV2\n",
    "lone_cr": b"B2=DUMMY_B2\rB3=x\n",
    "form_feed": b"B4=DUMMY_B4\x0cB5=y\n",
    "vertical_tab": b"B6=DUMMY_B6\x0bB7=z\n",
    "nul": b'C3=DUMMY\x00V3\nC4="DUMMY\x00\\tV4"\n',
    "nul_inside_key": b"C\x005=DUMMY_C5\n",
    "nbsp_before_comment": "D1=DUMMY_V1\u00a0# c\n".encode(),
    "nbsp_after_quoted": 'D2="DUMMY\\tV2"\u00a0# c\n'.encode(),
    "info_separator_before_comment": b"D6=DUMMY_V6\x1c# c\n",
    "ideographic_space_before_comment": "E1=DUMMY_V1\u3000# c\n".encode(),
    "em_space_after_quoted": 'E2="DUMMY\\tV2"\u2003# c\n'.encode(),
    "em_space_inside_key": "E\u20033=DUMMY_V3\n".encode(),
}
LAUNCHER_PATH = "/usr/bin:/bin"  # the PATH _ctl_sh_exports runs bash with (no Homebrew bash on a Mac: /bin/bash 3.2)
# The code points beyond ASCII some C library counts as [[:space:]] in a UTF-8 locale; glibc's are GLIBC_BLANKS.
GLIBC_BLANKS = "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2008\u2009\u200a\u2028\u2029\u205f\u3000"
UTF8_BLANK_CANDIDATES = GLIBC_BLANKS + "\u0085\u00a0\u2007\u202f"
ASCII_SPACE = " \t\n\r\x0c\x0b"


@functools.lru_cache(maxsize=None)
def bash_truncates_at_nul(path: str) -> bool:
    """Whether the bash found on *path* ends a ``read`` line at a NUL byte (bash 3.2, macOS's ``/bin/bash``) rather
    than dropping the NUL (bash 4 and later). Probed once per PATH, so a NUL test asserts what its shell does."""
    cp = subprocess.run(["bash", "-c", 'IFS= read -r l; printf %s "$l"'], input=b"A=x\0y\n", env={"PATH": path},
                        capture_output=True, timeout=30)
    assert cp.stdout in (b"A=x", b"A=xy"), f"bash on {path} read a NUL neither way: {cp.stdout!r} {cp.stderr!r}"
    return cp.stdout == b"A=x"


@functools.lru_cache(maxsize=None)
def bash_utf8_blanks(path: str) -> frozenset[str]:
    """The candidates the bash found on *path* counts as ``[[:space:]]`` under ``LANG=C.UTF-8`` (the host C
    library's answer); empty when that locale is missing (bash then runs in the C locale)."""
    cp = subprocess.run(["bash", "-c", 'for c in "$@"; do [[ $c == [[:space:]] ]] && printf 1 || printf 0; done',
                         "_", *UTF8_BLANK_CANDIDATES], env={"PATH": path, "LANG": "C.UTF-8"},
                        capture_output=True, text=True, timeout=30)
    assert cp.returncode == 0 and len(cp.stdout) == len(UTF8_BLANK_CANDIDATES), cp.stderr
    return frozenset(c for c, flag in zip(UTF8_BLANK_CANDIDATES, cp.stdout, strict=True) if flag == "1")


def _ctl_sh_exports(tmp_path, data: bytes, locale: dict[str, str]) -> dict[str, str]:
    """What ctl.sh's real ``_apply_env_file_safely`` exports for a file holding *data* (A-E keys only)."""
    body = re.search(r"\n(_apply_env_file_safely\(\) \{.*?\n\})\n", (REPO / "ctl.sh").read_text(encoding="utf-8"), re.S)
    assert body, "ctl.sh no longer defines _apply_env_file_safely"
    env_file = tmp_path / "t.env"
    env_file.write_bytes(data)
    script = tmp_path / "run.sh"
    script.write_text(body.group(1) + '\n_apply_env_file_safely "$1"\nexec "$2" -c '
                      '"import json, os; print(json.dumps(dict(os.environ)))"\n', encoding="utf-8")
    cp = subprocess.run(["bash", str(script), str(env_file), sys.executable], env={"PATH": LAUNCHER_PATH, **locale},
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    return {k: v for k, v in json.loads(cp.stdout).items() if k[:1] in "ABCDE" and k[1:].isdigit()}


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None, reason="needs bash and awk")
@pytest.mark.parametrize("locale", [{"LC_ALL": "C"}, {"LANG": "C.UTF-8"}], ids=["C", "C.UTF-8"])
@pytest.mark.parametrize("case", sorted(LAUNCHER_BYTE_CASES))
def test_launcher_reading_matches_ctl_sh_for_line_breaks_bytes_and_spaces(tmp_path, case, locale):
    exported = _ctl_sh_exports(tmp_path, b'A1="DUMMY\\tV1"\n' + LAUNCHER_BYTE_CASES[case], locale)
    assert exported.get("A1") == "DUMMY\tV1", "precondition: ctl.sh decoded the escaped value"
    reading = {"unicode_spaces": True} if "LANG" in locale else {}  # the UTF-8 locale's blanks: the host's
    if b"\0" in LAUNCHER_BYTE_CASES[case] and bash_truncates_at_nul(LAUNCHER_PATH):
        reading["nul_truncates"] = True  # bash 3.2: the line ends at the NUL
    assert profiles._launcher_dotenv_values(tmp_path / "t.env", **reading) == exported


def test_webui_reading_matches_its_actual_strict_utf8_text_loader(tmp_path):
    env_file = tmp_path / "t.env"
    env_file.write_bytes(b"# DUMMY comment \xff\nA1=DUMMY_V1\nA2=DUMMY\xffV2\nB2=DUMMY_B2\rB3=x\nC3=DUMMY\x00V3\n")
    assert profiles._read_dotenv_values(env_file) == {}, "WebUI cannot load a non-UTF8 file"
    env_file.write_bytes(b"A1=DUMMY_V1\nB2=DUMMY_B2\rB3=x\n")
    assert profiles._read_dotenv_values(env_file) == {"A1": "DUMMY_V1", "B2": "DUMMY_B2", "B3": "x"}


# 5b ── the UTF-8 reading's blanks are the host C library's, and both shells' NUL readings are recognised ──
DARWIN_BLANKS = GLIBC_BLANKS + "\u00a0\u2007\u202f"  # Darwin's iswspace also counts the no-break spaces
MUSL_BLANKS = GLIBC_BLANKS.replace("\u1680", "") + "\u0085"  # musl's: NEL, not the ogham space mark


def _utf8_space_builder():
    build = getattr(profiles, "_launcher_utf8_space", None)
    assert build is not None, "the UTF-8 blank set is a fixed table, not the host C library's answer"
    return build


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_launcher_utf8_space_is_the_host_bash_space_class():
    """The UTF-8 reading counts as blanks what the host's bash counts as ``[[:space:]]`` in a UTF-8 locale: glibc's
    set on Linux, the no-break spaces too on macOS."""
    host = bash_utf8_blanks(LAUNCHER_PATH)
    if "\u3000" not in host:
        pytest.skip("no C.UTF-8 locale for bash here")
    assert set(profiles._LAUNCHER_UTF8_SPACE) == set(ASCII_SPACE) | host


@pytest.mark.parametrize(("libc", "blanks"), [("glibc", GLIBC_BLANKS), ("darwin", DARWIN_BLANKS),
                                              ("musl", MUSL_BLANKS)], ids=["glibc", "darwin", "musl"])
def test_launcher_utf8_space_follows_the_c_library_it_is_given(libc, blanks):
    build = _utf8_space_builder()
    assert set(build(lambda ch: ch in blanks)) == set(ASCII_SPACE + blanks)


class _FakeLibC:
    """A C library whose ``iswspace_l`` counts *blanks* in the locale ``newlocale`` returns (None: no UTF-8 locale)."""

    def __init__(self, blanks, locale: int | None = 1):
        self.calls = []
        self.newlocale = lambda mask, name, base: self.calls.append(("newlocale", mask, name)) or locale
        self.iswspace_l = lambda wc, loc: int(loc == locale and chr(wc) in blanks)
        self.freelocale = lambda loc: self.calls.append(("freelocale", loc))


def test_launcher_utf8_space_takes_the_c_librarys_answer_in_a_utf8_locale(monkeypatch):
    """Through the C library: a Darwin-like ``iswspace_l`` gives Darwin's blanks, asked in a UTF-8 locale object of
    its own (freed afterwards), never through the process's locale."""
    import ctypes

    build = _utf8_space_builder()
    fake = _FakeLibC(DARWIN_BLANKS, locale=7)
    monkeypatch.setattr(ctypes, "CDLL", lambda name, *a, **kw: fake)
    monkeypatch.setattr(profiles, "_utf8_locale_names", lambda: ["DUMMY_LOCALE.UTF-8"])
    assert set(build()) == set(ASCII_SPACE + DARWIN_BLANKS)
    assert [call[0] for call in fake.calls] == ["newlocale", "freelocale"]
    assert fake.calls[0][2] == b"DUMMY_LOCALE.UTF-8" and fake.calls[1] == ("freelocale", 7)


@pytest.mark.parametrize("platform, expected", [("linux", "glibc"), ("darwin", "darwin")])
@pytest.mark.parametrize("failure", ["no_c_library", "no_iswspace_l", "no_utf8_locale", "answers_like_c_locale",
                                     "classifier_raises"])
def test_launcher_utf8_space_falls_back_to_the_platform_table_when_the_c_library_cannot_answer(
        monkeypatch, failure, platform, expected):
    """A Mac whose C library can't be asked must still read the no-break spaces as blanks, as its bash does."""
    import ctypes

    monkeypatch.setattr(sys, "platform", platform)
    fallback = ASCII_SPACE + (DARWIN_BLANKS if expected == "darwin" else GLIBC_BLANKS)
    build = _utf8_space_builder()

    def no_library(*_a, **_kw):
        raise OSError("DUMMY: no C library")

    def raising(_ch):
        raise RuntimeError("DUMMY: classifier failed")

    libraries = {"no_c_library": no_library,
                 "no_iswspace_l": lambda *_a, **_kw: types.SimpleNamespace(),
                 "no_utf8_locale": lambda *_a, **_kw: _FakeLibC(DARWIN_BLANKS, locale=None),
                 "answers_like_c_locale": lambda *_a, **_kw: _FakeLibC("")}
    if failure in libraries:
        monkeypatch.setattr(ctypes, "CDLL", libraries[failure])
        assert set(build()) == set(fallback)
    else:
        assert set(build(raising)) == set(fallback)


HOST_LOCALE_CHILD = r"""
import ctypes, json, locale, sys
sys.path.insert(0, sys.argv[1])
before = locale.setlocale(locale.LC_CTYPE)
from api import profiles as P
host = P._host_utf8_iswspace()
libc = ctypes.CDLL(None)
libc.iswspace.argtypes = (ctypes.c_uint32,)
print("RESULT " + json.dumps({"before": before, "after": locale.setlocale(locale.LC_CTYPE),
                              "process_iswspace_3000": bool(libc.iswspace(0x3000)),
                              "host_3000": host is not None and host("\u3000"), "space": P._LAUNCHER_UTF8_SPACE}))
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_c_library_answers_for_a_utf8_locale_under_a_c_process_locale(tmp_path):
    """Started with ``LC_ALL=C`` (no PEP 538 coercion: LC_CTYPE stays C, where ``iswspace`` is ASCII only), the
    UTF-8 reading still gets the C library's UTF-8 answer, and the process's locale is left as it was."""
    host = bash_utf8_blanks(LAUNCHER_PATH)
    if "\u3000" not in host:
        pytest.skip("no C.UTF-8 locale for bash here")
    env = {"PATH": LAUNCHER_PATH, "LC_ALL": "C", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    cp = subprocess.run([sys.executable, "-c", HOST_LOCALE_CHILD, str(REPO)], env=env, cwd=str(REPO),
                        capture_output=True, text=True, timeout=120)
    lines = [line for line in cp.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"child produced no result (rc={cp.returncode}): {cp.stderr[-1500:]}"
    obs = json.loads(lines[-1][len("RESULT "):])
    assert obs["before"] == obs["after"] == "C", "the process locale changed"
    assert obs["process_iswspace_3000"] is False, "precondition: iswspace in the process locale is ASCII only"
    assert obs["host_3000"] is True, "the C library was not asked in a UTF-8 locale"
    assert set(obs["space"]) == set(ASCII_SPACE) | host


IMPORT_INIT_CHILD = r"""
import ctypes, json, locale, sys
sys.path.insert(0, sys.argv[1])
lc_all, lc_ctype, want_mask, blanks = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), json.loads(sys.argv[5])
locale.LC_ALL, locale.LC_CTYPE = lc_all, lc_ctype  # the platform's category numbering, as the module reads it
calls = []


def newlocale(mask, name, base):
    calls.append(mask)
    return 9 if mask == want_mask else None  # only the right LC_CTYPE mask gets a UTF-8 locale


def iswspace_l(wc, loc):
    return int(loc == 9 and chr(wc) in blanks)


def freelocale(loc):
    calls.append(("free", loc))


fake = type("FakeLibC", (), {})()  # plain functions as attributes: the module sets restype/argtypes on them
fake.newlocale, fake.iswspace_l, fake.freelocale = newlocale, iswspace_l, freelocale
real_cdll = ctypes.CDLL
ctypes.CDLL = lambda name, *a, **kw: fake if name is None else real_cdll(name, *a, **kw)
from api import profiles as P  # the blank set is taken here, at import
print("RESULT " + json.dumps({"space": P._LAUNCHER_UTF8_SPACE, "calls": calls}))
"""


@pytest.mark.parametrize(("lc_all", "lc_ctype", "mask", "blanks"),
                         [(0, 2, 2, DARWIN_BLANKS), (6, 0, 1, MUSL_BLANKS)], ids=["darwin-numbering", "glibc-numbering"])
def test_the_blank_set_is_the_c_librarys_answer_from_import(tmp_path, lc_all, lc_ctype, mask, blanks):
    """At import, ``_LAUNCHER_UTF8_SPACE`` is what the C library answers, asked with the LC_CTYPE mask of the
    platform's numbering: Darwin and the BSDs count from LC_ALL = 0 (LC_CTYPE 2, mask 2), glibc and musl from 0
    (LC_CTYPE 0, mask 1). A wrong mask gets no UTF-8 locale and would fall back to the table instead."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1"}
    cp = subprocess.run([sys.executable, "-c", IMPORT_INIT_CHILD, str(REPO), str(lc_all), str(lc_ctype), str(mask),
                         json.dumps(blanks)], env=env, cwd=str(REPO), capture_output=True, text=True, timeout=120)
    lines = [line for line in cp.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"child produced no result (rc={cp.returncode}): {cp.stderr[-1500:]}"
    obs = json.loads(lines[-1][len("RESULT "):])
    assert obs["calls"] and obs["calls"][0] == mask, f"asked with LC_CTYPE mask {obs['calls'][:1]}, not {mask}"
    assert ["free", 9] in obs["calls"], "the locale object was not freed"
    assert set(obs["space"]) == set(ASCII_SPACE + blanks), "the import-time blank set is not the C library's answer"


# 6 ── the private Agent attribute the startup install replaces ──


@pytest.mark.skipif(_LPP is None, reason="Hermes Agent launch-profile policy not importable")
def test_agent_launch_scope_reads_the_replaceable_launch_env_attribute():
    """Pin: the Agent builds launch scopes through ``launch_profile_policy._launch_env()``."""
    assert _LPP is not None
    assert "_launch_env" in vars(_LPP)
    assert callable(_LPP._launch_env)
    assert "_launch_env()" in inspect.getsource(_LPP.launch_secret_scope)


def _fake_launch_policy(monkeypatch, **attrs):
    pkg = types.ModuleType("tui_gateway")
    pkg.__path__ = []
    module = types.ModuleType("tui_gateway.launch_profile_policy")
    for name, value in attrs.items():
        setattr(module, name, value)
    pkg.launch_profile_policy = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tui_gateway", pkg)
    monkeypatch.setitem(sys.modules, "tui_gateway.launch_profile_policy", module)
    return module


def test_install_freezes_operator_env_as_the_agent_launch_env(monkeypatch, tmp_path):
    from types import MappingProxyType

    import api.paths as paths

    module = _fake_launch_policy(monkeypatch, _launch_env=lambda: {"LIVE": "live"})
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType({"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", "")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(tmp_path))
    profiles._install_operator_env_as_agent_launch_env()
    monkeypatch.setenv("OPERATOR_ONLY_TOKEN", "DUMMY_KEY_PROFILE_B")
    first = module._launch_env()
    assert first == {"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}
    first["OPERATOR_ONLY_TOKEN"] = "changed"
    assert module._launch_env()["OPERATOR_ONLY_TOKEN"] == "DUMMY_OPERATOR_TOKEN", "callers share one dict"


@pytest.mark.parametrize("root_env_file", ["missing", "unreadable"])
def test_install_keeps_the_operator_env_when_the_root_env_file_cannot_be_read(monkeypatch, caplog, tmp_path,
                                                                              root_env_file):
    from types import MappingProxyType

    import api.paths as paths

    if root_env_file == "unreadable":
        (tmp_path / ".env").mkdir()
    startup = {"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY", "OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}
    module = _fake_launch_policy(monkeypatch, _launch_env=lambda: {"LIVE": "live"})
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType(startup))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", "")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="api.profiles"):
        profiles._install_operator_env_as_agent_launch_env()
    assert module._launch_env() == startup, "nothing to leave out without a readable root .env"
    assert any(".env" in record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG)


def _install_with_root_env(monkeypatch, tmp_path, root_env_text, *, launch_home):
    """Startup install for a launcher that sourced the root ``.env`` (HERMES_HOME unset)."""
    from types import MappingProxyType

    import api.paths as paths

    startup = {"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY", "OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}
    (tmp_path / ".env").write_text(root_env_text, encoding="utf-8")
    module = _fake_launch_policy(monkeypatch, _launch_env=lambda: {"LIVE": "live"})
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType(startup))
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", "")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", str(launch_home))
    profiles._install_operator_env_as_agent_launch_env()
    return module


def test_install_gives_a_named_launch_profile_the_current_root_env_values(monkeypatch, tmp_path):
    root_env = tmp_path / ".env"
    module = _install_with_root_env(monkeypatch, tmp_path, "OPENROUTER_API_KEY=DUMMY_ROOT_KEY\n",
                                    launch_home=tmp_path / "profiles" / "alpha")
    operator = {"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}
    assert module._launch_env() == {**operator, "OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"}
    root_env.write_text("OPENROUTER_API_KEY=DUMMY_ROOT_KEY_NEW\nROOT_ADDED_KEY=DUMMY_ROOT_KEY\n", encoding="utf-8")
    assert module._launch_env() == {**operator, "OPENROUTER_API_KEY": "DUMMY_ROOT_KEY_NEW"}, \
        "only the keys the launcher sourced follow the root .env"
    root_env.write_text("ROOT_ADDED_KEY=DUMMY_ROOT_KEY\n", encoding="utf-8")
    assert module._launch_env() == operator


def test_install_leaves_the_root_env_to_a_root_launch_profiles_own_read(monkeypatch, tmp_path):
    module = _install_with_root_env(monkeypatch, tmp_path, "OPENROUTER_API_KEY=DUMMY_ROOT_KEY\n", launch_home=tmp_path)
    assert module._launch_env() == {"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}


def test_install_takes_a_root_key_bash_32_exported_up_to_a_nul_from_the_current_root_env(monkeypatch, tmp_path):
    """macOS's /bin/bash 3.2 ends a ``read`` line at a NUL, so ctl.sh exported the value up to it: that export is
    the root .env's, re-read from the current file (with or without the NUL), not frozen as the operator's."""
    root_env = tmp_path / ".env"
    module = _install_with_root_env(monkeypatch, tmp_path, "OPENROUTER_API_KEY=DUMMY_ROOT_KEY\0_TAIL\n",
                                    launch_home=tmp_path / "profiles" / "alpha")
    operator = {"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}
    assert module._launch_env() == {**operator, "OPENROUTER_API_KEY": "DUMMY_ROOT_KEY"}
    root_env.write_text("OPENROUTER_API_KEY=DUMMY_ROOT_KEY_NEW\0_TAIL\n", encoding="utf-8")
    assert module._launch_env() == {**operator, "OPENROUTER_API_KEY": "DUMMY_ROOT_KEY_NEW"}, \
        "the bash 3.2 export of the root .env was frozen as an operator value"
    root_env.write_text("OPENROUTER_API_KEY=DUMMY_ROOT_KEY_CLEAN\n", encoding="utf-8")  # Settings rewrote it
    assert module._launch_env() == {**operator, "OPENROUTER_API_KEY": "DUMMY_ROOT_KEY_CLEAN"}
    root_env.write_text("", encoding="utf-8")
    assert module._launch_env() == operator


def test_install_rereads_a_key_the_agent_reading_matched_as_the_agent_reads_it(monkeypatch, tmp_path):
    """Coverage (``k not in agent_keys``): a launch value both the Agent's and the launcher's reading of the root .env
    hold is re-read as the Agent reads it, so after an edit the launcher's decoding (a real tab) doesn't replace the
    Agent's value (the escape kept literal)."""
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: profiles._read_dotenv_values)  # keeps \t literal
    module = _install_with_root_env(monkeypatch, tmp_path, "OPENROUTER_API_KEY=DUMMY_ROOT_KEY\n",
                                    launch_home=tmp_path / "profiles" / "alpha")
    (tmp_path / ".env").write_text('OPENROUTER_API_KEY="DUMMY_ROOT\\tKEY"\n', encoding="utf-8")
    assert module._launch_env()["OPENROUTER_API_KEY"] == "DUMMY_ROOT\\tKEY", \
        "a key the Agent's reading matched was re-read with the launcher's decoding"


def test_install_leaves_a_launcher_decoded_root_key_to_roots_own_scope(monkeypatch, tmp_path):
    """Coverage (root's ``k not in root_dotenv``): root is the process profile and the launch value equals only the
    launcher's decoding of a key root's own reading also holds (escaped, literal there): root's scope reads that key
    itself, so the launch env leaves it out."""
    monkeypatch.setattr(profiles, "_agent_env_file_reader", lambda: profiles._read_dotenv_values)  # keeps \n literal
    module = _install_with_root_env(monkeypatch, tmp_path, 'OPENROUTER_API_KEY="DUMMY_ROOT_KEY\\n"\n',
                                    launch_home=tmp_path)
    assert profiles._read_dotenv_values(tmp_path / ".env") == {"OPENROUTER_API_KEY": "DUMMY_ROOT_KEY\\n"}, \
        "precondition: root's own reading holds the key, not the launch value"
    assert module._launch_env() == {"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}, \
        "a key root's own scope reads was kept in the launch env"


@pytest.mark.parametrize("agent_reader", ["no_load_env_file", "no_secret_scope_module"])
def test_install_reads_the_root_env_with_webui_reader_on_an_older_agent(monkeypatch, caplog, tmp_path, agent_reader):
    older = types.ModuleType("agent.secret_scope") if agent_reader == "no_load_env_file" else None
    monkeypatch.setitem(sys.modules, "agent.secret_scope", older)
    with caplog.at_level(logging.DEBUG, logger="api.profiles"):
        module = _install_with_root_env(monkeypatch, tmp_path, "OPENROUTER_API_KEY=DUMMY_ROOT_KEY\n",
                                        launch_home=tmp_path)
    assert module._launch_env() == {"OPERATOR_ONLY_TOKEN": "DUMMY_OPERATOR_TOKEN"}
    assert any("load_env_file" in record.getMessage() for record in caplog.records if record.levelno == logging.DEBUG)


def test_install_logs_and_keeps_the_agent_default_when_the_attribute_is_missing(monkeypatch, caplog):
    module = _fake_launch_policy(monkeypatch, launch_secret_scope=lambda home: {})
    with caplog.at_level(logging.WARNING, logger="api.profiles"):
        profiles._install_operator_env_as_agent_launch_env()
    assert not hasattr(module, "_launch_env"), "nothing is installed in place of a missing attribute"
    assert any("_launch_env" in record.getMessage() for record in caplog.records)


def test_install_is_a_no_op_without_the_agent_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "tui_gateway", None)
    profiles._install_operator_env_as_agent_launch_env()
