"""#7870 review round 2 — the tests now run against the REAL installed agent.

The re-gate found that ``tests/test_issue7870_env_keys_profile_auth.py``
replaces the very modules the code under test depends on:

- ``sys.modules["hermes_cli.config"]`` is a fake writer (~123–127, 176);
- ``sys.modules["hermes_constants"]`` is a fake override (~196–203), so the
  scope this PR now depends on is never exercised;
- ``sys.modules["hermes_cli.web_server_profiles"]`` (~177) fakes a module the
  code no longer imports.

``test_scope_never_activates_process_wide_multi_profile_hosting`` therefore
checks a fake activation counter rather than the agent's real multiplex flag.

This module drives the same routes through the **installed**
``hermes_cli.config`` / ``hermes_constants`` against temp profile homes. It is
skipped when those modules are unavailable, which is a real environment fact and
not something to paper over with a fake: an assertion that passes against a stub
is worse than no assertion, because it reads as coverage.

The one finding this module *disproves* is worth stating up front, because it is
the reason the previous round could not have found it: the reviewer asked for a
test asserting that a named-profile PUT/DELETE leaves ``os.environ`` unchanged,
on the reasoning that ``save_env_value`` → ``_publish_env_value`` writes into the
shared process env when no secret scope is bound. That is true of
``_publish_env_value`` in isolation. It is **not** true of the path WebUI takes,
and the reason is load-bearing:

    inside a ``hermes_constants`` home override to a non-base profile,
    ``serves_routed_profile()`` returns True, so ``_publish_env_value``'s
    ``targets`` list is EMPTY and it touches neither ``os.environ`` nor any
    scope mapping.

So the very override that scopes the ``.env`` write also makes the publish a
no-op. The tests below pin that property rather than assuming it, because a
future change to either module could silently reintroduce the leak.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Reuse the repository's own agent discovery instead of re-deriving it here:
# tests/conftest.py already mirrors api/config._discover_agent_dir, and a second
# implementation would drift from the first. tests/ is not a package, so the
# directory goes on sys.path first (same pattern as the other conftest
# importers in this tree).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _discover_agent_dir  # noqa: E402

# The installed agent tree. The WebUI venv does not carry it, and the modules the
# code under test imports (``hermes_cli.config`` -> ``hermes_yaml`` ->
# ``ruamel``) are exactly what is missing there, so these tests run under the
# interpreter that actually serves the WebUI.
# The installed agent tree, discovered the same way tests/conftest.py does it
# (``HERMES_WEBUI_AGENT_DIR`` → ``~/.hermes/hermes-agent`` → repo-parent → HOME
# variants, each requiring ``run_agent.py``). CI does not install the agent at
# any of those locations, so the skip below still holds there — but a
# contributor with the agent in a non-default location no longer silently loses
# this module, and the path is not pinned to one machine's layout.
_AGENT_ROOT = _discover_agent_dir()

_REAL_AGENT = bool(_AGENT_ROOT)
pytestmark = pytest.mark.skipif(
    not _REAL_AGENT, reason="the installed hermes-agent tree is not present"
)


@pytest.fixture(scope="module", autouse=True)
def _agent_on_path():
    root = str(_AGENT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    yield


def _import_real_agent():
    """Import the real modules or fail loudly — never silently stub them."""
    import hermes_cli.config as real_config
    import hermes_constants as real_constants

    return real_config, real_constants


@pytest.fixture()
def real_agent():
    return _import_real_agent()


# ── the finding that turns out NOT to reproduce, pinned as a property ────────


def test_a_profile_home_override_makes_the_env_publish_a_noop(
    real_agent, tmp_path, monkeypatch
):
    """A named-profile write must not reach the shared ``os.environ``.

    ``_publish_env_value`` writes into ``os.environ`` when no secret scope is
    bound — that is its legacy behaviour and it is what a WebUI PUT would hit if
    the write were unscoped. What protects the process is the SAME override that
    scopes the ``.env`` target: under a non-base profile home,
    ``serves_routed_profile()`` is True, so ``targets`` is empty.

    Asserted through the real writer, not through a reimplementation of its
    branching: if either module changes its mind, this fails.
    """
    config, constants = real_agent
    from agent.secret_scope import serves_routed_profile

    base_home = tmp_path / "homes" / "default"
    profile_home = tmp_path / "homes" / "alpha"
    profile_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(base_home))

    # Sanity: the process is NOT multiplexed before the scope is entered, so the
    # legacy os.environ publish WOULD fire without the override.
    assert serves_routed_profile() is False, (
        "the process reports a routed profile outside any scope; this test "
        "would then be asserting the wrong branch"
    )

    probe = "HERMES_WEBUI_PROBE_NOT_REAL"
    monkeypatch.delenv(probe, raising=False)
    before = dict(os.environ)

    token = constants.set_hermes_home_override(str(profile_home))
    try:
        assert serves_routed_profile() is True, (
            "the profile home override no longer marks the write as routed; a "
            "named-profile PUT would then publish into the shared os.environ"
        )
        config.save_env_value(probe, "written-under-scope")
    finally:
        constants.reset_hermes_home_override(token)

    # The write landed in the profile's own .env ...
    written = (profile_home / ".env").read_text(encoding="utf-8")
    assert f"{probe}=written-under-scope" in written, (
        f"the real writer did not honour the home override: {written!r}"
    )
    # ... and nowhere else.
    assert not (base_home / ".env").exists() or probe not in (
        base_home / ".env"
    ).read_text(encoding="utf-8"), "the write leaked into the launch profile's .env"
    assert probe not in os.environ, (
        "a named-profile PUT published into the shared process environment; "
        "every profile served by this WebUI process would read it"
    )
    assert dict(os.environ) == {
        k: v for k, v in before.items()
    }, "os.environ changed in ways other than the probe key"


def test_a_profile_delete_does_not_pop_a_launch_profile_value(
    real_agent, tmp_path, monkeypatch
):
    """A DELETE under a profile scope must not clear the launch profile's value.

    ``remove_env_value`` publishes ``None``, which pops the key. If that pop ever
    reached ``os.environ`` it would clear a variable the launch profile's own
    ``.env`` defines — a credential vanishing from a profile that was never the
    target of the request.
    """
    config, constants = real_agent

    base_home = tmp_path / "homes" / "default"
    profile_home = tmp_path / "homes" / "alpha"
    base_home.mkdir(parents=True, exist_ok=True)
    profile_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(base_home))

    probe = "HERMES_WEBUI_PROBE_DELETE"
    # The launch profile's .env defines it, and it is live in the process.
    (base_home / ".env").write_text(f"{probe}=launch-value\n", encoding="utf-8")
    monkeypatch.setenv(probe, "launch-value")
    # The named profile also has it, so the delete has something real to remove.
    (profile_home / ".env").write_text(f"{probe}=profile-value\n", encoding="utf-8")

    token = constants.set_hermes_home_override(str(profile_home))
    try:
        removed = config.remove_env_value(probe)
    finally:
        constants.reset_hermes_home_override(token)

    assert removed is True, "the delete did not remove the key from the profile"
    assert probe not in (profile_home / ".env").read_text(encoding="utf-8")
    # The launch profile's file AND its live process value both survive.
    assert f"{probe}=launch-value" in (base_home / ".env").read_text(encoding="utf-8")
    assert os.environ.get(probe) == "launch-value", (
        "a scoped delete popped the launch profile's live environment variable"
    )


# ── the reviewer's matrix, driven through the real writer ───────────────────


class _Capture:
    """Record what ``api.env_keys``'s own ``j`` / ``bad`` send.

    ``api.env_keys`` binds them with ``from api.helpers import bad, j``, so the
    module-level names have to be replaced ON ``api.env_keys``.
    """

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


class _Handler:
    def __init__(self):
        self.status = None
        self.payload = None


@pytest.fixture()
def env(monkeypatch, tmp_path, real_agent):
    """Wire ``api.env_keys`` to the REAL agent writer over temp profile homes.

    Only two things are faked, and both are WebUI's own seams rather than the
    agent's: the active profile (WebUI's authorization source) and the profile →
    home resolver (pointed at the temp homes this fixture creates). Everything
    the reviewer named — the config writer, the constants override, the scope's
    side effects — is the installed implementation.
    """
    import importlib

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import api.env_keys as env_keys
    import api.profiles as profiles

    homes = tmp_path / "homes"
    for name in ("default", "alpha", "beta"):
        (homes / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(homes / "default"))

    mod = importlib.reload(env_keys)

    state = {"name": "alpha"}
    monkeypatch.setattr(
        profiles, "get_active_profile_name", lambda: state["name"], raising=False
    )

    _known = {"alpha", "beta", "default"}

    def _home_for(name):
        if not name or name not in _known:
            return homes / "default"
        return homes / name

    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", _home_for, raising=False
    )
    monkeypatch.setattr(
        mod, "_authorized_profile", lambda: mod._canonical_profile_name(state["name"])
    )
    return SimpleNamespace(mod=mod, state=state, homes=homes)


def _keys(env, handler, query=""):
    parsed = SimpleNamespace(path="/api/env/keys", query=query)
    with _Capture(handler, env.mod):
        assert env.mod.handle_env_keys_get(handler, parsed) is True


def _put(env, handler, body, query=""):
    parsed = SimpleNamespace(path="/api/env/keys", query=query)
    with _Capture(handler, env.mod):
        assert env.mod.handle_env_keys_put(handler, parsed, body) is True


def _delete(env, handler, name, query=""):
    parsed = SimpleNamespace(path=f"/api/env/keys/{name}", query=query)
    with _Capture(handler, env.mod):
        assert env.mod.handle_env_key_delete(handler, name, parsed) is True


def test_put_get_delete_round_trip_through_the_real_writer(env):
    """PUT → GET → DELETE against the installed agent writer."""
    handler = _Handler()
    _put(env, handler, {"name": "ROUND_TRIP", "value": "value-one"})
    assert handler.status == 200, handler.payload
    assert (env.homes / "alpha" / ".env").read_text().find("ROUND_TRIP=value-one") != -1

    _keys(env, handler)
    assert handler.status == 200
    names = [k["name"] for k in handler.payload["keys"]]
    assert "ROUND_TRIP" in names
    entry = [k for k in handler.payload["keys"] if k["name"] == "ROUND_TRIP"][0]
    # The response carries a mask, never the value.
    assert entry["redacted_value"] == "*" * len("value-one")

    _delete(env, handler, "ROUND_TRIP")
    assert handler.status == 200, handler.payload
    assert "ROUND_TRIP" not in (env.homes / "alpha" / ".env").read_text()


def test_refused_replace_returns_non_2xx_through_the_real_writer(env, monkeypatch):
    """A write the filesystem refuses must not be reported as success.

    Driven with the real writer's own failure: the atomic replace raises, so the
    handler's ``except Exception`` arm maps it to a 4xx. What matters is that a
    refusal is never a 2xx — the old code answered ok:true for a writer that
    declined without raising, and an exception is the same refusal arriving
    loudly.
    """
    handler = _Handler()
    target = env.homes / "alpha" / ".env"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("PINNED_KEY=old-value\n", encoding="utf-8")

    real_config, _ = _import_real_agent()

    def _refuse(env_path, lines, *, preserve_mode):
        raise OSError("read-only file system")

    monkeypatch.setattr(real_config, "_write_env_lines", _refuse)

    _put(env, handler, {"name": "PINNED_KEY", "value": "new-value"})
    assert handler.status not in (200, 201), (
        f"a write the filesystem refused was reported as success ({handler.status})"
    )
    assert 400 <= handler.status < 500, (
        f"a refused write must answer a 4xx, got {handler.status}"
    )
    # The old value is still what is on disk.
    assert "PINNED_KEY=old-value" in target.read_text(encoding="utf-8")


def test_silent_refusal_returns_409_through_the_real_writer(env, monkeypatch):
    """A writer that declines WITHOUT raising must answer 409, not ok:true.

    ``save_env_value`` returns ``None`` on success and on refusal alike, so the
    only proof of the mutation is the read-back. Stubbing the writer to decline
    silently — the shape the installed agent uses for a managed ``.env`` — leaves
    the previous value in place, and reporting success would claim the new secret
    is live while the old one is.
    """
    real_config, _ = _import_real_agent()
    target = env.homes / "alpha" / ".env"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("DECLINED_KEY=old-value\n", encoding="utf-8")

    def _decline(key, value):
        return None  # refuses without raising, exactly like a managed .env

    monkeypatch.setattr(real_config, "save_env_value", _decline)

    handler = _Handler()
    _put(env, handler, {"name": "DECLINED_KEY", "value": "new-value"})
    assert handler.status == 409, (
        f"a silent refusal whose old value survives must answer 409, got "
        f"{handler.status}"
    )
    assert "DECLINED_KEY=old-value" in target.read_text(encoding="utf-8")


def test_newline_value_is_rejected_before_the_writer(env):
    """A control-character value must not reach the real writer at all."""
    real_config, _ = _import_real_agent()
    seen = []
    real_save = real_config.save_env_value

    def _recording(key, value):
        seen.append((key, value))
        return real_save(key, value)

    real_config.save_env_value = _recording
    try:
        handler = _Handler()
        _put(env, handler, {"name": "NEWLINE_VALUE", "value": "a\nb"})
        assert handler.status == 400, (
            f"a newline in the value must be rejected, got {handler.status}"
        )
        assert seen == [], "the writer was reached with a control-character value"
    finally:
        real_config.save_env_value = real_save


def test_route_auth_uses_the_active_profile_not_the_query(env):
    """``?profile=`` is not an authorization mechanism.

    The request is bound to alpha; a write aimed at ``?profile=beta`` must land
    in alpha and leave beta's file untouched.
    """
    (env.homes / "beta" / ".env").write_text("BETA_KEY=beta-value\n", encoding="utf-8")

    handler = _Handler()
    _put(env, handler, {"name": "MINE", "value": "v"}, query="profile=beta")
    assert handler.status == 200, handler.payload
    assert "MINE=v" in (env.homes / "alpha" / ".env").read_text()
    assert "MINE" not in (env.homes / "beta" / ".env").read_text()
    assert "BETA_KEY=beta-value" in (env.homes / "beta" / ".env").read_text()


def test_the_real_scope_does_not_activate_multi_profile_hosting(env):
    """The real multiplex flag must stay off across a custom-key request.

    The previous round asserted this against a fake activation counter. This
    drives the real request and reads the agent's real flag, which is the thing
    that would break concurrent chat turns if it ever flipped.
    """
    from agent.secret_scope import serves_routed_profile

    handler = _Handler()
    _put(env, handler, {"name": "NO_FLIP", "value": "v"})
    assert handler.status == 200, handler.payload
    # Outside the scope the process must not be left multiplexed.
    assert serves_routed_profile() is False, (
        "a custom-key request left the process multiplexed; every concurrent "
        "unscoped read would now fail closed"
    )
    _keys(env, handler)
    assert serves_routed_profile() is False, "not even a read may flip the flag"
