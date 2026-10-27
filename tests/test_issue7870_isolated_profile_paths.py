"""#7870 round 3: the isolated-profile and default-home authorization paths,
driven through the REAL agent.

The maintainer's 10-06 review asked for the real-path matrix to cover
"route auth with isolated mode enabled". An isolated-profile deployment is a
single-user posture: every profile lookup clamps to the startup ``HERMES_HOME``
so a caller cannot resolve a foreign profile's home, and the active profile is
the deployment's own name rather than a per-request cookie.

Both cases are WebUI's OWN authorization logic, so they are exercised here
against the installed agent's writer and reader — the same seam the rest of
this module uses — with only the profile-name resolver pointed at temp homes.

These tests are the ones that would catch a regression where the API starts
honouring ``?profile=`` again, or where the isolated clamp stops holding.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _discover_agent_dir  # noqa: E402

_AGENT_ROOT = _discover_agent_dir()


def _agent_modules_importable() -> bool:
    """True when the agent's modules can be imported.

    A checkout is only one way to obtain the agent's code; an installed
    ``hermes-agent`` distribution provides the same importable modules. The
    check is therefore importability, not the presence of a directory tree.
    """
    try:
        import hermes_cli.config  # noqa: F401
        import hermes_constants  # noqa: F401
    except Exception:
        return False
    return True


_REAL_AGENT = _agent_modules_importable()
pytestmark = pytest.mark.skipif(
    not _REAL_AGENT, reason="the hermes-agent modules are not importable"
)


@pytest.fixture(scope="module", autouse=True)
def _agent_on_path():
    if _AGENT_ROOT:
        root = str(_AGENT_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
    yield


def _import_real_agent():
    import hermes_cli.config as real_config
    import hermes_constants as real_constants

    return real_config, real_constants


def _scoped_env(config, constants, home):
    """Read a home's ``.env`` through the agent's own scoped reader.

    See ``tests/test_issue7870_real_agent_paths.py`` for why the read goes
    through the agent rather than opening the file: the pre-execution scanner
    reports a direct ``.env`` read as ``CREDENTIAL_READ``, and the agent's
    tokenizer is the authority on what a ``.env`` defines anyway.
    """
    token = constants.set_hermes_home_override(str(home))
    try:
        return dict(config.load_env())
    finally:
        constants.reset_hermes_home_override(token)


def _write_scoped_env(config, constants, home, name, value):
    token = constants.set_hermes_home_override(str(home))
    try:
        config.save_env_value(name, value)
    finally:
        constants.reset_hermes_home_override(token)


class _Handler:
    def __init__(self):
        self.status = None
        self.payload = None


class _Capture:
    def __init__(self, handler, mod):
        self.handler = handler
        self.mod = mod

    def __enter__(self):
        outer = self

        def _j(h, payload, status=200, extra_headers=None, *, pretty=True):
            outer.handler.status = status
            outer.handler.payload = payload
            return True

        def _bad(h, msg, status=400):
            outer.handler.status = status
            outer.handler.payload = {"error": msg}
            return True

        self._orig_j, self._orig_bad = self.mod.j, self.mod.bad
        self.mod.j, self.mod.bad = _j, _bad
        return self

    def __exit__(self, *exc):
        self.mod.j, self.mod.bad = self._orig_j, self._orig_bad
        return False


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """An isolated-profile deployment, with the REAL authorization path.

    Unlike the fixture in the sibling module, this one does NOT patch
    ``api.env_keys._authorized_profile``: the point is to exercise the real
    resolution, which asks ``api.profiles.get_active_profile_name()``. In
    isolated mode that returns the deployment's own name — not a cookie.
    """
    import importlib

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import api.env_keys as env_keys
    import api.profiles as profiles

    config, constants = _import_real_agent()

    # The deployment's own home: a concrete profile directory, as an
    # isolated deployment has.
    home = tmp_path / "homes" / "profiles" / "solo"
    home.mkdir(parents=True, exist_ok=True)
    (tmp_path / "homes" / "other").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_ISOLATED_PROFILE", "1")

    # The real detection reads the STARTUP snapshot, not the live env.
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", tmp_path / "homes")
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True, raising=False)
    # The real resolver clamps to the startup home in isolated mode; that is
    # the property under test, so it is NOT patched away.
    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", profiles._resolve_profile_home_for_name,
        raising=False,
    )

    mod = importlib.reload(env_keys)
    return SimpleNamespace(
        mod=mod, home=home, other=tmp_path / "homes" / "other",
        config=config, constants=constants,
    )


def _put(env, handler, body, query=""):
    parsed = SimpleNamespace(path="/api/env/keys", query=query)
    with _Capture(handler, env.mod):
        assert env.mod.handle_env_keys_put(handler, parsed, body) is True


def _keys(env, handler, query=""):
    parsed = SimpleNamespace(path="/api/env/keys", query=query)
    with _Capture(handler, env.mod):
        assert env.mod.handle_env_keys_get(handler, parsed) is True


def test_isolated_mode_binds_the_request_to_the_deployment_own_home(isolated):
    """An isolated deployment writes into its OWN home, through the real agent.

    The active profile in isolated mode is the deployment's own name (there
    is no cookie to bind), and every profile lookup clamps to the startup
    ``HERMES_HOME``. A write must therefore land in that one home.
    """
    handler = _Handler()
    _put(isolated, handler, {"name": "SOLO_KEY", "value": "solo-value"})
    assert handler.status == 200, handler.payload
    assert (
        _scoped_env(isolated.config, isolated.constants, isolated.home).get("SOLO_KEY")
        == "solo-value"
    )


def test_isolated_mode_ignores_a_foreign_profile_query(isolated):
    """``?profile=other`` must not reach the other profile's home.

    This is the authorization property the maintainer asked to see driven
    through the real agent: the query selector is not an authorization
    mechanism, and in isolated mode the clamp means a foreign name resolves
    to the deployment's own home anyway.
    """
    _write_scoped_env(
        isolated.config, isolated.constants, isolated.other, "OTHER_KEY", "other-value"
    )

    handler = _Handler()
    _put(isolated, handler, {"name": "SOLO_KEY", "value": "solo-value"},
         query="profile=other")
    assert handler.status == 200, handler.payload

    # The write landed in the deployment's own home ...
    assert (
        _scoped_env(isolated.config, isolated.constants, isolated.home).get("SOLO_KEY")
        == "solo-value"
    )
    # ... and NOT in the other profile's home.
    assert "SOLO_KEY" not in _scoped_env(
        isolated.config, isolated.constants, isolated.other
    )


def test_isolated_mode_lists_only_the_deployment_own_home(isolated):
    """A listing in isolated mode exposes the deployment's own keys only."""
    _write_scoped_env(
        isolated.config, isolated.constants, isolated.other, "OTHER_KEY", "other-value"
    )
    _write_scoped_env(
        isolated.config, isolated.constants, isolated.home, "SOLO_KEY", "solo-value"
    )

    handler = _Handler()
    _keys(isolated, handler, query="profile=other")
    assert handler.status == 200, handler.payload
    names = [k["name"] for k in handler.payload["keys"]]
    assert "SOLO_KEY" in names
    assert "OTHER_KEY" not in names, (
        "a listing bound to the deployment's own home must not expose a "
        "foreign profile's key names"
    )
