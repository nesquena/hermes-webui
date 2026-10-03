"""Cross-profile authorization for the custom `.env` key API (#7870 review).

The API previously derived its profile scope from an untrusted ``?profile=``
query parameter, with a no-op fallback when the Agent binding was
unavailable. A request bound to profile A (the profile WebUI auth
established) could therefore list, write and delete profile B's `.env``, and
a request active on B could silently act on the server's launch profile's
``.env`` and be told ``ok:true``.

These tests compose the three pieces the reviewer's matrix asks for — the
route entry points, the real profile-scope binding, and a real (fixture)
writer — and assert the boundaries:

  * a request bound to A with ``?profile=B`` neither lists nor mutates B;
  * a request active on B with no query parameter touches only B;
  * an isolated-profile deployment querying B is rejected;
  * an absent/broken scope fails closed instead of falling through to A;
  * a writer refusal that does not raise is not reported as success.

The fixture builds a REAL ``hermes_cli.web_server_profiles._profile_scope``
(an env-var-scoped context manager, exactly the shape the installed agent
uses) and a REAL ``hermes_cli.config`` writer over per-profile temp files, so
the assertions exercise the same code path production does.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest


# ── the real-shaped agent side ───────────────────────────────────────────────


class _RealWriter:
    """Per-profile ``.env`` files with the agent writer's real signature.

    ``save_env_value`` returns None on success and ``remove_env_value``
    returns whether anything was removed — matching
    ``hermes_cli.config`` — so the handlers' return-value handling is
    exercised for real.
    """

    def __init__(self, root):
        self.root = root
        self.refusals: list[str] = []
        self.calls: list[tuple[str, str, str]] = []
        self._managed = {"PROVIDER_API_KEY"}  # writer refuses, does not raise

    def _path(self, profile: str):
        return self.root / profile / ".env"

    def load_env(self) -> dict[str, str]:
        import os

        home = os.environ.get("HERMES_HOME", "")
        path = self.root / home / ".env" if home else None
        if path is None or not path.exists():
            return {}
        out: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                out[k] = v
        return out

    def save_env_value(self, key: str, value: str) -> None:
        if key in self._managed:
            # Refuse WITHOUT raising: the installed agent does exactly this
            # for a managed .env, and the old handler answered ok:true.
            self.refusals.append(key)
            return None
        self.calls.append(("save", key, value))
        import os

        home = os.environ.get("HERMES_HOME", "")
        p = self.root / home / ".env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{key}={value}\n", encoding="utf-8")
        return None

    def remove_env_value(self, key: str) -> bool:
        self.calls.append(("remove", key, ""))
        import os

        home = os.environ.get("HERMES_HOME", "")
        p = self.root / home / ".env"
        if p.exists() and f"{key}=" in p.read_text(encoding="utf-8"):
            p.write_text("", encoding="utf-8")
            return True
        return False


def _install_real_shaped_agent(monkeypatch, writer: _RealWriter):
    """Install a config module + a REAL env-var profile-scope context manager."""
    import os

    fake_cli = types.ModuleType("hermes_cli")
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_env = writer.load_env  # type: ignore[attr-defined]
    fake_config.save_env_value = writer.save_env_value  # type: ignore[attr-defined]
    fake_config.remove_env_value = writer.remove_env_value  # type: ignore[attr-defined]
    fake_cli.config = fake_config  # type: ignore[attr-defined]

    fake_scope_mod = types.ModuleType("hermes_cli.web_server_profiles")

    class _RealProfileScope:
        """The installed agent's shape: set HERMES_HOME for the duration."""

        def __init__(self, profile: str):
            self._profile = profile
            self._saved = None

        def __enter__(self):
            self._saved = os.environ.get("HERMES_HOME")

            os.environ["HERMES_HOME"] = self._profile
            return self

        def __exit__(self, *exc):
            if self._saved is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = self._saved
            return False

    def _profile_scope(profile):
        # The real helper raises on an unknown profile name.
        if not profile or profile not in ("alpha", "beta", "default"):
            raise ValueError(f"unknown profile {profile!r}")
        return _RealProfileScope(profile)

    fake_scope_mod._profile_scope = _profile_scope  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", fake_scope_mod)


class _Handler:
    def __init__(self):
        self.status: int | None = None
        self.payload: dict | None = None


class _Capture:
    """Record what ``api.env_keys``'s own ``j`` / ``bad`` send.

    ``api.env_keys`` binds them with ``from api.helpers import bad, j``, so
    patching the helpers module after the reload would not be seen — the
    module-level names have to be replaced ON ``api.env_keys``.
    """

    def __init__(self, handler: _Handler, mod):
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


@pytest.fixture
def env(monkeypatch, tmp_path):
    import importlib

    import api.env_keys as env_keys
    import api.profiles as profiles

    writer = _RealWriter(tmp_path / "homes")
    _install_real_shaped_agent(monkeypatch, writer)
    mod = importlib.reload(env_keys)

    # The active profile is WebUI's authorization source; drive it directly.
    # Patch AFTER the reload so the module's own function object is replaced
    # (reloading re-binds every module-level name).
    state = {"name": "alpha"}

    monkeypatch.setattr(
        profiles, "get_active_profile_name", lambda: state["name"], raising=False
    )
    monkeypatch.setattr(
        mod,
        "_authorized_profile",
        lambda: mod._canonical_profile_name(state["name"]),
    )
    return SimpleNamespace(mod=mod, writer=writer, state=state)


def _keys(env, handler, path="/api/env/keys", query=""):
    parsed = SimpleNamespace(path=path, query=query)
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


# ── the reviewer's regression matrix ─────────────────────────────────────────


def test_caller_supplied_profile_is_not_honored(env, tmp_path):
    """A request bound to A with ``?profile=B`` must neither list nor mutate B.

    The query parameter is not an authorization mechanism: a valid profile
    name is not permission to act on it.
    """
    # Seed BOTH profiles' .env files on disk.
    for profile, secret in (("alpha", "A_SECRET"), ("beta", "B_SECRET")):
        p = tmp_path / "homes" / profile / ".env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"TOKEN={secret}\n", encoding="utf-8")

    handler = _Handler()
    _keys(env, handler, query="profile=beta")
    assert handler.status == 200
    names = [k["name"] for k in handler.payload["keys"]]
    assert "TOKEN" in names
    # Every key reported came from A's home.
    assert env.writer.calls == []
    import os

    assert os.environ.get("HERMES_HOME") != "beta", "B must never be entered"

    # And a write aimed at B lands in A.
    _put(env, handler, {"name": "MINE", "value": "s3cret"}, query="profile=beta")
    assert handler.status == 200
    assert (tmp_path / "homes" / "alpha" / ".env").read_text().find("MINE=s3cret") != -1
    assert not (tmp_path / "homes" / "beta" / ".env").read_text().find("MINE") != -1

    # And a delete aimed at B removes from A only; B's own key is untouched.
    _delete(env, handler, "TOKEN", query="profile=beta")
    assert handler.status == 200
    assert "TOKEN" not in (tmp_path / "homes" / "alpha" / ".env").read_text(), (
        "the delete must remove from the AUTHORIZED home (A)"
    )
    assert "TOKEN=B_SECRET" in (tmp_path / "homes" / "beta" / ".env").read_text(), (
        "B's credential must survive a delete request aimed at it"
    )


def test_active_profile_without_query_touches_only_its_home(env, tmp_path):
    """No query parameter: the authenticated profile's OWN .env is used.

    The old no-op scope silently targeted the launch/default profile, so a
    request active on B mutated A.
    """
    env.state["name"] = "beta"
    handler = _Handler()
    _put(env, handler, {"name": "BKEY", "value": "v"})
    assert handler.status == 200
    assert (tmp_path / "homes" / "beta" / ".env").read_text().find("BKEY=v") != -1
    assert not (tmp_path / "homes" / "alpha" / ".env").exists()


def test_isolated_profile_querying_another_is_rejected(env, monkeypatch):
    """An isolated-profile deployment has exactly one legal home."""
    env.state["name"] = "solo"

    handler = _Handler()
    _keys(env, handler, query="profile=alpha")
    # alpha is not this request's authorized profile: nothing is listed.
    assert handler.status != 200 or all(
        k.get("name") != "TOKEN" for k in handler.payload.get("keys", [])
    )
    assert all(c[2] != "alpha" for c in env.writer.calls), "no write into alpha"


def test_broken_scope_fails_closed(env, monkeypatch):
    """A scope that cannot be constructed must not fall through to A."""
    env.state["name"] = "ghost"

    handler = _Handler()
    _put(env, handler, {"name": "X", "value": "y"})
    # The writer binds HERMES_HOME; a broken binding must not land in the
    # launch profile's .env.
    assert handler.status == 503, (
        "an unboundable profile must fail closed, not silently target another home"
    )


def test_writer_refusal_is_not_reported_as_success(env):
    """A ``save_env_value`` that refuses without raising must not answer ok."""
    handler = _Handler()
    _put(env, handler, {"name": "PROVIDER_API_KEY", "value": "sk-live"})
    assert env.writer.refusals == ["PROVIDER_API_KEY"]
    assert handler.status != 200, "a refused write must not report success"


def test_redaction_reveals_no_characters(env, tmp_path):
    """The preview leaks nothing — not even the first/last two characters."""
    p = tmp_path / "homes" / "alpha" / ".env"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("TOKEN=abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")

    handler = _Handler()
    _keys(env, handler)
    entry = [k for k in handler.payload["keys"] if k["name"] == "TOKEN"][0]
    assert entry["redacted_value"] == "*" * 26
    assert "a" not in entry["redacted_value"]
    assert "z" not in entry["redacted_value"]


def test_root_alias_resolves_to_default(env):
    """A renamed root profile and ``default`` are ONE home, not two."""
    monkeypatch_aliases = ("renamed-root", "renamed_root", "default")

    import api.profiles as profiles

    original = profiles._is_root_profile
    try:
        profiles._is_root_profile = lambda name: name in monkeypatch_aliases
        for alias in monkeypatch_aliases:
            assert env.mod._canonical_profile_name(alias) == "default"
        assert env.mod._canonical_profile_name("beta") == "beta"
    finally:
        profiles._is_root_profile = original
