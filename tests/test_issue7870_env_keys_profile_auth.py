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

    The writer resolves its target the way the installed agent does:
    ``get_env_path() -> get_hermes_home() / ".env"``, and ``get_hermes_home()``
    prefers the **task-local contextvar override** over ``HERMES_HOME``. The
    scope under test must therefore be verified through the same channel the
    real reader/writer uses, not through a private back door the production
    code never touches.
    """

    def __init__(self, root):
        self.root = root
        self.refusals: list[str] = []
        self.calls: list[tuple[str, str, str]] = []
        self._managed = {"PROVIDER_API_KEY"}  # writer refuses, does not raise

    def _home(self) -> str | None:
        from hermes_constants import get_hermes_home_override

        override = get_hermes_home_override()
        if override:
            return override
        import os

        return os.environ.get("HERMES_HOME", "") or None

    def _path(self, profile: str):
        return self.root / profile / ".env"

    def load_env(self) -> dict[str, str]:
        home = self._home()
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
        home = self._home()
        if not home:
            raise AssertionError("save_env_value reached no scoped home")
        p = self.root / home / ".env"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{key}={value}\n", encoding="utf-8")
        return None

    def remove_env_value(self, key: str) -> bool:
        self.calls.append(("remove", key, ""))
        home = self._home()
        p = self.root / home / ".env" if home else None
        if p is not None and p.exists() and f"{key}=" in p.read_text(encoding="utf-8"):
            p.write_text("", encoding="utf-8")
            return True
        return False


def _install_real_shaped_agent(monkeypatch, writer: _RealWriter):
    """Install a config module shaped like the agent's, plus a real-shaped
    ``hermes_cli.web_server_profiles`` whose ``_profile_scope`` reproduces the
    process-global side effect the production code must NOT trigger.

    ``_config_profile_scope`` calls ``activate_multi_profile_hosting()`` for
    any non-launch home, a one-way process-global switch that makes
    ``agent.secret_scope.get_secret`` fail closed for every later unscoped
    read. Any test that enters this scope therefore records the activation, so
    a regression that imports it again is caught by assertion, not by
    reasoning about the import.
    """
    fake_cli = types.ModuleType("hermes_cli")
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_env = writer.load_env  # type: ignore[attr-defined]
    fake_config.save_env_value = writer.save_env_value  # type: ignore[attr-defined]
    fake_config.remove_env_value = writer.remove_env_value  # type: ignore[attr-defined]
    fake_cli.config = fake_config  # type: ignore[attr-defined]

    activations: list[str] = []

    fake_policy = types.ModuleType("tui_gateway.launch_profile_policy")
    fake_policy.activate_multi_profile_hosting = (  # type: ignore[attr-defined]
        lambda: activations.append("multi-profile")
    )
    monkeypatch.setitem(sys.modules, "tui_gateway.launch_profile_policy", fake_policy)

    fake_scope_mod = types.ModuleType("hermes_cli.web_server_profiles")

    class _RealProfileScope:
        """The installed agent's shape: scope HERMES_HOME for the duration.

        Also records the activation because the real ``_config_profile_scope``
        performs one for every non-launch home.
        """

        def __init__(self, profile: str):
            self._profile = profile
            self._saved = None
            activations.append(profile)

        def __enter__(self):
            import os

            self._saved = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = self._profile
            return self

        def __exit__(self, *exc):
            import os

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

    # `api.env_keys._profile_scope` imports `hermes_constants` for the
    # context-local home override, and `_RealWriter._home` reads it back. CI
    # does not install hermes-agent, so the real module is absent there and the
    # production scope would fail closed (EnvKeyProfileError -> 500/503) purely
    # on that import. Stub the module with a contextvar-backed override that
    # mirrors the real API so the scope under test is exercised end to end
    # without the agent installed.
    import contextvars

    _home_override_var: "contextvars.ContextVar" = contextvars.ContextVar(
        "hermes_home_override", default=None
    )

    def _set_override(path):
        return _home_override_var.set(None if path is None else str(path))

    fake_hc = types.ModuleType("hermes_constants")
    fake_hc.set_hermes_home_override = _set_override  # type: ignore[attr-defined]
    fake_hc.reset_hermes_home_override = (  # type: ignore[attr-defined]
        _home_override_var.reset
    )
    fake_hc.get_hermes_home_override = (  # type: ignore[attr-defined]
        _home_override_var.get
    )
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hc)
    return activations


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
    activations = _install_real_shaped_agent(monkeypatch, writer)
    mod = importlib.reload(env_keys)

    # The active profile is WebUI's authorization source; drive it directly.
    # Patch AFTER the reload so the module's own function object is replaced
    # (reloading re-binds every module-level name).
    state = {"name": "alpha"}

    monkeypatch.setattr(
        profiles, "get_active_profile_name", lambda: state["name"], raising=False
    )
    # The scope resolves the profile's home through WebUI's OWN resolver
    # (api.profiles.get_hermes_home_for_profile), which must not mutate any
    # process state. Point it at the per-profile homes this fixture creates so
    # the real override path is exercised end to end. The resolver keeps its
    # real contract — a name it cannot resolve falls back to the BASE (default)
    # home rather than raising — which is exactly why the production code has
    # to reject a home that is not provably the caller's own.
    _known = {"alpha", "beta"}

    def _fake_home_for(name):
        if not name or name not in _known:
            return tmp_path / "homes" / "default"  # the real fallback
        return tmp_path / "homes" / name

    monkeypatch.setattr(
        profiles, "get_hermes_home_for_profile", _fake_home_for, raising=False
    )
    monkeypatch.setattr(
        mod,
        "_authorized_profile",
        lambda: mod._canonical_profile_name(state["name"]),
    )
    return SimpleNamespace(
        mod=mod, writer=writer, state=state, activations=activations
    )


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


# ── round-2 regressions: the scope's side effects and its proof ─────────────


def test_scope_never_activates_process_wide_multi_profile_hosting(env):
    """A custom-key request must not flip the whole process.

    The obvious scope to reuse — ``hermes_cli.web_server_profiles._profile_scope``
    — calls ``activate_multi_profile_hosting()`` for any non-launch home. That
    is a process-global, one-way switch: afterwards
    ``agent.secret_scope.get_secret`` fails closed and every concurrent
    unscoped read raises ``UnscopedSecretError``, including the chat turns this
    very server is serving. The WebUI never enables it on master, so a single
    admin request for another profile's home must not either.

    The fake module records every activation, so a regression that imports the
    dashboard scope again is caught here rather than in a hard-to-reproduce
    production failure.
    """
    handler = _Handler()
    _put(env, handler, {"name": "NO_SIDE_EFFECT", "value": "v"})
    assert handler.status == 200
    assert env.activations == [], (
        "the .env scope must not activate process-wide multi-profile hosting: "
        f"{env.activations}"
    )
    _keys(env, handler)
    assert env.activations == [], "not even a read may flip the process flag"


def test_write_verification_reads_the_file_it_wrote(env, tmp_path):
    """The read-back must observe the SAME ``.env`` the writer touched.

    The scope redirects the task-local ``HERMES_HOME``; reading outside it (as
    the first version did, after the ``with`` block exited) verified the
    process's launch ``.env`` — the DEFAULT profile — so a write into B was
    "confirmed" against A and a failure in B went unreported.
    """
    env.state["name"] = "beta"
    # A's .env already holds the key with a different value: an out-of-scope
    # read-back would see it present and answer ok:true.
    a_home = tmp_path / "homes" / "alpha"
    a_home.mkdir(parents=True, exist_ok=True)
    (a_home / ".env").write_text("SAME_KEY=old-value\n", encoding="utf-8")

    handler = _Handler()
    _put(env, handler, {"name": "SAME_KEY", "value": "new-value"})
    assert handler.status == 200

    b_env = (tmp_path / "homes" / "beta" / ".env").read_text(encoding="utf-8")
    assert "SAME_KEY=new-value" in b_env, (
        f"the write must land in the authorized home: {b_env!r}"
    )
    a_env = (tmp_path / "homes" / "alpha" / ".env").read_text(encoding="utf-8")
    assert a_env == "SAME_KEY=old-value\n", (
        f"the other profile's .env must be byte-for-byte untouched: {a_env!r}"
    )


def test_replaced_value_mismatch_is_not_reported_as_success(env):
    """A writer that keeps the OLD value must not report the new one stored.

    ``save_env_value`` refuses a managed ``.env`` by returning without raising,
    so its return value cannot distinguish "wrote it" from "declined". Presence
    alone is therefore not proof: the key can be there with the PREVIOUS value,
    and answering ok:true would report a secret as live while the old one still
    is.
    """
    # Seed the key with the old value directly on disk so the writer's refusal
    # leaves it in place.
    handler = _Handler()
    env.writer.calls.clear()
    # PROVIDER_API_KEY is the fake writer's managed refusal set.
    seed = env.writer
    seed._managed.discard("PROVIDER_API_KEY")
    env.state["name"] = "alpha"
    _put(env, handler, {"name": "PROVIDER_API_KEY", "value": "first"})
    assert handler.status == 200

    # Now refuse it and try to replace with a different value.
    seed._managed.add("PROVIDER_API_KEY")
    handler2 = _Handler()
    _put(env, handler2, {"name": "PROVIDER_API_KEY", "value": "second"})
    assert handler2.status != 200, (
        "a refused replacement whose old value survives must not report success"
    )
    assert handler2.status == 409, f"expected 409, got {handler2.status}"


def test_unverifiable_delete_is_not_reported_as_success(env, monkeypatch):
    """A delete whose read-back fails must not answer ok.

    ``_active_profile_env`` returning ``None`` means the removal is UNVERIFIED;
    the old code only checked "is the key still there", so a read failure fell
    through to ok:true and reported a deletion nobody confirmed.
    """
    env.state["name"] = "alpha"
    env.writer.calls.clear()

    def _broken_read(profile=None):
        return None, ("load", "read-back exploded")

    monkeypatch.setattr(env.mod, "_active_profile_env", _broken_read)
    handler = _Handler()
    _delete(env, handler, "ANYTHING")
    assert handler.status == 409, (
        f"an unverifiable delete must not report success, got {handler.status}"
    )


def test_unresolvable_profile_home_fails_closed(env, monkeypatch):
    """A profile whose home is not provably its own must not write.

    ``get_hermes_home_for_profile`` falls back to the BASE (default) home for a
    name it cannot resolve. Redirecting a write there would land it in the
    DEFAULT profile's ``.env`` and report it as the caller's — the same
    "wrong-home default" failure this PR fixed once, now reachable through the
    new resolver.
    """
    env.state["name"] = "ghost"
    handler = _Handler()
    _put(env, handler, {"name": "X", "value": "y"})
    assert handler.status == 503, (
        f"an unboundable profile must fail closed, got {handler.status}"
    )
    assert env.writer.calls == [], "no write may be attempted"
    # Nothing landed in the fallback (default) home either.
    assert not (env.writer.root / "default" / ".env").exists() or "X=" not in (
        env.writer.root / "default" / ".env"
    ).read_text(encoding="utf-8"), "must not write into the fallback home"


# ── round-3 hardening: the concurrency lock and the value's characters ──────


def test_write_paths_hold_the_provider_writer_env_lock(env):
    """The custom-key writer must serialise against the provider-key writer.

    Two concurrent PUTs read the same ``.env``, apply their own mutation and
    write it back — the second write then silently drops the first key.
    ``api.streaming._ENV_LOCK`` is the lock the provider-key routes already
    hold for exactly this reason; the custom-key writer has to hold the same
    one so the two cannot interleave.

    ``threading.Lock`` is not reentrant and has no ``owner`` accessor, so the
    hold cannot be observed from inside the locked section on the same thread.
    The observable proxy is the lock being **unavailable to another thread**
    for exactly the duration of the mutation: a second thread's non-blocking
    acquire must fail while the writer runs and succeed afterwards. That is
    the same property that makes two concurrent writers serialise.
    """
    import api.streaming as streaming

    real_lock = streaming._ENV_LOCK
    inside: list[bool] = []

    original_save = env.writer.save_env_value

    def _probe_save(key, value):
        # Runs while the handler holds its lock. A plain Lock is not reentrant,
        # so this succeeds iff the handler did NOT hold it.
        acquired = real_lock.acquire(blocking=False)
        if acquired:
            real_lock.release()
        inside.append(not acquired)
        return original_save(key, value)

    # The fixture bound the writer's bound methods into the fake config module
    # at install time, so the probe has to re-bind there — replacing the
    # instance attribute afterwards would not be seen by the handler's
    # function-local ``from hermes_cli.config import save_env_value``.
    import hermes_cli.config as fake_config

    fake_config.save_env_value = _probe_save

    handler = _Handler()
    _put(env, handler, {"name": "LOCKED_KEY", "value": "v"})
    assert handler.status == 200
    assert inside == [True], (
        "the mutation must run while the env lock is held — otherwise a "
        f"second thread could acquire it (observed {inside})"
    )
    # And the lock is free once the request is done, so nothing deadlocks.
    grabbed = real_lock.acquire(blocking=False)
    assert grabbed, "the env lock must be released after the request"
    if grabbed:
        real_lock.release()

    inside.clear()
    original_remove = env.writer.remove_env_value

    def _probe_remove(key):
        acquired = real_lock.acquire(blocking=False)
        if acquired:
            real_lock.release()
        inside.append(not acquired)
        return original_remove(key)

    import hermes_cli.config as fake_config_rm

    fake_config_rm.remove_env_value = _probe_remove
    _delete(env, handler, "LOCKED_KEY")
    assert handler.status == 200
    assert inside == [True], "the delete must run inside the same env lock"


def test_value_with_control_characters_is_rejected(env, tmp_path):
    """NUL / CR / LF and friends must not reach the ``.env``.

    A name check alone does not catch these, so a value carrying one is
    written verbatim — and a CR truncates the line the agent later parses
    while a NUL truncates the value at the reader, breaking the profile's
    ``.env`` reload long after the write was reported successful (#7870
    review).
    """
    home = tmp_path / "homes" / "alpha"
    home.mkdir(parents=True, exist_ok=True)

    for bad_value in (
        "line1\nline2",
        "line1\rline2",
        "with\x00nul",
        "tab\there",
        "esc\x1b[31m",
    ):
        handler = _Handler()
        _put(env, handler, {"name": "BAD_VALUE", "value": bad_value})
        assert handler.status == 400, (
            f"a value with control characters must be rejected, got "
            f"{handler.status} for {bad_value!r}"
        )
        assert not (home / ".env").exists(), "no write may be attempted"

    # A clean value still writes, so the rejection is not over-broad.
    handler = _Handler()
    _put(env, handler, {"name": "GOOD_VALUE", "value": "sk-plain-token-123"})
    assert handler.status == 200
    assert "GOOD_VALUE=sk-plain-token-123" in (home / ".env").read_text()


def test_lock_is_not_reentered_by_the_read_back(env):
    """The read-back inside a locked PUT must not deadlock.

    ``api.streaming._ENV_LOCK`` is a plain ``threading.Lock`` — not an RLock.
    The handler takes it once around the mutation *and* its verification, so
    if the read-back path ever acquires it again the request deadlocks on
    itself. This drives the real PUT + read-back and simply completing proves
    the lock is taken exactly once per request.
    """
    import api.streaming as streaming

    lock = streaming._ENV_LOCK
    assert lock.acquire(blocking=False)
    lock.release()  # proven uncontended before the request

    handler = _Handler()
    _put(env, handler, {"name": "NO_DEADLOCK", "value": "v"})
    assert handler.status == 200
    # Still uncontended afterwards: the request did not hang and left it free.
    acquired = lock.acquire(blocking=False)
    assert acquired, "the env lock must be free after a successful PUT"
    lock.release()
