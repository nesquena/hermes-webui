"""Cross-profile authorization for the custom `.env` key API (#7870 review).

The API previously derived its profile scope from an untrusted ``?profile=``
query parameter, with a no-op fallback when the Agent binding was
unavailable. A request bound to profile A (the profile WebUI auth
established) could therefore list, write and delete profile B's `.env`, and
a request active on B could silently act on the server's launch profile's
``.env`` and be told ``ok:true``.

ROUND 3: this module no longer installs any module fakes. An earlier
revision replaced ``hermes_cli.config`` (a fake writer), ``hermes_constants``
(a fake contextvar override) and ``hermes_cli.web_server_profiles`` (a module
the production code no longer imports), so the scope this API depends on was
never exercised and
``test_scope_never_activates_process_wide_multi_profile_hosting`` checked a
fake activation counter instead of the agent's real multiplex flag. Every
assertion below now runs through the installed ``hermes_cli.config`` /
``hermes_constants`` / ``agent.secret_scope`` against temp profile homes.

What is still test scaffolding, and why it is not "replacing authorization":

* ``api.profiles.set_request_profile`` / ``clear_request_profile`` are the
  REAL per-request binding ``server.py`` performs from the ``hermes_profile``
  cookie (``do_GET`` / ``_handle_write``). Tests drive that function;
  ``get_active_profile_name`` is never monkeypatched.
* ``api.profiles._DEFAULT_HERMES_HOME`` and the startup snapshots
  (``_INITIAL_HERMES_HOME`` / ``_INITIAL_ISOLATED_PROFILE_OPT_IN``) are
  environment inputs, pointed at the temp base home. The real resolver
  (``get_hermes_home_for_profile``) and the real isolation predicate
  (``_is_isolated_profile_mode``) execute unmodified on top of them.
* The only function-level monkeypatches are FAULT INJECTIONS into the real
  writer/reader (a failing atomic replace, a silent refusal, a failing
  read-back). They simulate I/O failure; they do not replace the module.

The reviewer's matrix runs for both profile shapes: ``default`` (the launch
home, ``<base>``) and a named profile (``<base>/profiles/<name>``), for
PUT / GET / DELETE, refused-replace, failed read-back, a newline value and
route auth under isolated mode.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Reuse the repository's own agent discovery (tests/conftest.py mirrors
# api/config._discover_agent_dir); a second implementation would drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import _discover_agent_dir  # noqa: E402

_AGENT_ROOT = _discover_agent_dir()


def _agent_modules_importable() -> bool:
    """True when the real agent modules can be imported.

    A fake that passes is worse than a skip: it reads as coverage while
    asserting against a stub. CI installs the agent source in the dedicated
    ``env-keys-real-agent`` job (see .github/workflows/tests.yml), so this
    module runs there; elsewhere it skips with a visible reason.
    """
    try:
        import hermes_cli.config  # noqa: F401
        import hermes_constants  # noqa: F401
        from agent.secret_scope import serves_routed_profile  # noqa: F401
    except Exception:
        return False
    return True


_REAL_AGENT = _agent_modules_importable()
pytestmark = pytest.mark.skipif(
    not _REAL_AGENT,
    reason="the real hermes-agent modules are not importable; this module "
    "refuses to assert against fakes",
)


@pytest.fixture(scope="module", autouse=True)
def _agent_on_path():
    if _AGENT_ROOT:
        root = str(_AGENT_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
    yield


def _scoped_env(config, constants, home) -> dict[str, str]:
    """Read a home's ``.env`` through the agent's own scoped reader.

    ``config.load_env()`` resolves the ``.env`` of the home currently in
    scope, so scoping to a home answers exactly "what does THIS profile's
    ``.env`` define?" — through the production tokenizer, without a test
    opening a ``.env`` file directly.
    """
    token = constants.set_hermes_home_override(str(home))
    try:
        return dict(config.load_env())
    finally:
        constants.reset_hermes_home_override(token)


def _seed_env(config, constants, home, name, value) -> None:
    """Write a key into a home's ``.env`` through the agent's own writer."""
    token = constants.set_hermes_home_override(str(home))
    try:
        config.save_env_value(name, value)
    finally:
        constants.reset_hermes_home_override(token)


class _Handler:
    def __init__(self):
        self.status: int | None = None
        self.payload: dict | None = None


class _Capture:
    """Record what ``api.env_keys``'s own ``j`` / ``bad`` send.

    ``api.env_keys`` binds them with ``from api.helpers import bad, j``, so
    the module-level names have to be replaced ON ``api.env_keys``. This is
    WebUI's response helper, not an agent module: nothing about the agent
    import surface is faked here.
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


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Wire ``api.env_keys`` to the REAL agent writer over temp profile homes.

    The layout is the production one: the default (launch) profile owns the
    base home ``<base>/.env``; a named profile owns
    ``<base>/profiles/<name>/.env``. The request's authorization comes from
    the real thread-local ``set_request_profile`` path, never from a patched
    ``get_active_profile_name``.
    """
    import api.env_keys as env_keys
    import api.profiles as profiles
    import hermes_cli.config as config
    import hermes_constants as constants
    from agent.secret_scope import serves_routed_profile

    base = tmp_path / "hermes"
    profiles_dir = base / "profiles"
    for name in ("alpha", "beta"):
        (profiles_dir / name).mkdir(parents=True, exist_ok=True)
    base.mkdir(parents=True, exist_ok=True)

    # Environment inputs, NOT alternates of the real logic: the real resolver
    # reads _DEFAULT_HERMES_HOME, and the real isolation predicate reads the
    # startup snapshots. Pointing them at the temp base exercises both real
    # functions over a temp home instead of monkeypatching either one.
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(base))
    monkeypatch.setattr(profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")

    # Production pins the home this process serves as its own; the real writer
    # uses that anchor to decide whether a write is "routed" (and therefore
    # must not publish into the shared os.environ). Process-global state: the
    # previous value is restored in the finally block.
    previous_pin = getattr(constants, "_PINNED_PROCESS_HERMES_HOME", None)
    previous_webui_home = profiles._PROCESS_PROFILE_HOME
    profiles._pin_process_profile_home(base)

    # Root-profile metadata: in this temp base only "default" is a root alias.
    # _is_root_profile() would otherwise scan the base home (a subprocess
    # class call) for a rename alias this fixture never creates.
    with profiles._root_profile_name_cache_lock:
        previous_cache = set(profiles._root_profile_name_cache)
        previous_cache_loaded = profiles._root_profile_name_cache_loaded
        profiles._root_profile_name_cache.clear()
        profiles._root_profile_name_cache.add("default")
        profiles._root_profile_name_cache_loaded = True

    profiles.clear_request_profile()
    try:
        yield SimpleNamespace(
            mod=env_keys,
            profiles=profiles,
            config=config,
            constants=constants,
            base=base,
            profiles_dir=profiles_dir,
            serves_routed_profile=serves_routed_profile,
        )
    finally:
        profiles.clear_request_profile()
        profiles._PROCESS_PROFILE_HOME = previous_webui_home
        constants.pin_process_hermes_home(previous_pin)
        with profiles._root_profile_name_cache_lock:
            profiles._root_profile_name_cache.clear()
            profiles._root_profile_name_cache.update(previous_cache)
            profiles._root_profile_name_cache_loaded = previous_cache_loaded


def _activate(env, name: str) -> None:
    """Bind the request to *name* exactly as server.py does from the cookie."""
    env.profiles.clear_request_profile()
    env.profiles.set_request_profile(name)


def _home(env, profile: str) -> Path:
    """The temp home the production layout puts *profile* in."""
    if profile == "default":
        return env.base
    return env.profiles_dir / profile


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


# ── the reviewer's matrix: PUT / GET / DELETE, default and named ─────────────


@pytest.mark.parametrize("profile", ["default", "alpha"])
def test_put_get_delete_round_trip_through_the_real_writer(env, profile):
    """PUT → GET → DELETE against the installed agent writer, both shapes.

    The named home is not a variation of the default one: the structural
    "the resolved home must be this profile's own" guard only runs for a named
    profile, and the writer's routed/os.environ branch depends on where the
    home sits relative to the process pin. Both shapes therefore get their own
    proof.
    """
    _activate(env, profile)
    home = _home(env, profile)

    handler = _Handler()
    _put(env, handler, {"name": "ROUND_TRIP", "value": "value-one"})
    assert handler.status == 200, handler.payload
    assert _scoped_env(env.config, env.constants, home).get("ROUND_TRIP") == "value-one"

    _keys(env, handler)
    assert handler.status == 200, handler.payload
    names = [k["name"] for k in handler.payload["keys"]]
    assert "ROUND_TRIP" in names
    entry = [k for k in handler.payload["keys"] if k["name"] == "ROUND_TRIP"][0]
    assert entry["redacted_value"] == "*" * len("value-one")

    _delete(env, handler, "ROUND_TRIP")
    assert handler.status == 200, handler.payload
    assert "ROUND_TRIP" not in _scoped_env(env.config, env.constants, home)


# ── authorization: the caller-supplied profile is never an entitlement ───────


def test_caller_supplied_profile_is_not_honored(env):
    """A request bound to A with ``?profile=B`` must neither list nor mutate B."""
    _activate(env, "alpha")
    for profile, secret in (("alpha", "A_SECRET"), ("beta", "B_SECRET")):
        _seed_env(env.config, env.constants, _home(env, profile), "TOKEN", secret)

    handler = _Handler()
    _keys(env, handler, query="profile=beta")
    assert handler.status == 200, handler.payload
    names = [k["name"] for k in handler.payload["keys"]]
    assert "TOKEN" in names
    # The listed key came from alpha's home, not beta's.
    assert env.profiles.get_active_profile_name() == "alpha"

    _put(env, handler, {"name": "MINE", "value": "s3cret"}, query="profile=beta")
    assert handler.status == 200, handler.payload
    assert _scoped_env(env.config, env.constants, _home(env, "alpha"))["MINE"] == "s3cret"
    assert "MINE" not in _scoped_env(env.config, env.constants, _home(env, "beta"))

    _delete(env, handler, "TOKEN", query="profile=beta")
    assert handler.status == 200, handler.payload
    assert "TOKEN" not in _scoped_env(env.config, env.constants, _home(env, "alpha")), (
        "the delete must remove from the AUTHORIZED home (A)"
    )
    assert (
        _scoped_env(env.config, env.constants, _home(env, "beta"))["TOKEN"] == "B_SECRET"
    ), "B's credential must survive a delete request aimed at it"


def test_active_profile_without_query_touches_only_its_home(env):
    """No query parameter: the authenticated profile's OWN .env is used."""
    _activate(env, "beta")

    handler = _Handler()
    _put(env, handler, {"name": "BKEY", "value": "v"})
    assert handler.status == 200, handler.payload
    assert _scoped_env(env.config, env.constants, _home(env, "beta"))["BKEY"] == "v"
    assert not _scoped_env(env.config, env.constants, _home(env, "alpha"))


def test_isolated_profile_querying_another_is_rejected(env, monkeypatch):
    """Isolated mode: the startup home is the only legal one, cookie included.

    The isolation predicate runs UNMODIFIED: the fixture only supplies the two
    startup inputs it reads (HERMES_HOME's shape and the explicit opt-in),
    then asserts the real ``_is_isolated_profile_mode()`` says True. A cookie
    bound to another profile — the per-request binding server.py performs —
    must not move the API off the pinned home, and ``?profile=`` must not
    either.
    """
    isolated_home = _home(env, "alpha")
    monkeypatch.setattr(env.profiles, "_INITIAL_HERMES_HOME", str(isolated_home))
    monkeypatch.setattr(env.profiles, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "1")
    monkeypatch.setattr(env.profiles, "_DEFAULT_HERMES_HOME", isolated_home.parent.parent)

    # The real predicate, on the real inputs.
    assert env.profiles._is_isolated_profile_mode() is True

    # A cookie bound to beta (server.py's own binding) is clamped to alpha.
    _activate(env, "beta")
    assert env.profiles.get_active_profile_name() == "alpha"
    assert env.profiles.get_hermes_home_for_profile("beta") == isolated_home

    _seed_env(env.config, env.constants, isolated_home, "ISOLATED_KEY", "iso-value")

    handler = _Handler()
    _keys(env, handler, query="profile=beta")
    assert handler.status == 200, handler.payload
    names = [k["name"] for k in handler.payload["keys"]]
    assert "ISOLATED_KEY" in names, "reads must come from the pinned home"

    _put(env, handler, {"name": "ISO_WRITE", "value": "v"}, query="profile=beta")
    assert handler.status == 200, handler.payload
    assert _scoped_env(env.config, env.constants, isolated_home).get("ISO_WRITE") == "v"
    # The write cannot land in beta's directory even though the cookie and the
    # query both name beta.
    beta_home = _home(env, "beta")
    assert not beta_home.exists() or "ISO_WRITE" not in _scoped_env(
        env.config, env.constants, beta_home
    )


def test_illegal_profile_name_fails_closed(env):
    """A binding the resolver cannot map to its own home must not write.

    ``get_hermes_home_for_profile`` falls back to the BASE (default) home for
    a name it cannot resolve. Redirecting a write there would land it in the
    DEFAULT profile's ``.env`` and report it as the caller's — the exact
    "wrong-home default" failure this PR fixed once, now reachable through the
    new resolver, so it has to be rejected rather than trusted.
    """
    _activate(env, "../escape")

    handler = _Handler()
    _put(env, handler, {"name": "X", "value": "y"})
    assert handler.status == 503, (
        f"an unboundable profile must fail closed, got {handler.status}"
    )
    assert not _scoped_env(env.config, env.constants, env.base), (
        "must not write into the fallback home"
    )


# ── refusals and unverifiable mutations ──────────────────────────────────────


def test_writer_refusal_is_not_reported_as_success(env, monkeypatch):
    """A ``save_env_value`` that refuses without raising must not answer ok.

    FAULT INJECTION into the real module, not a module replacement: the
    installed writer signals a managed-.env refusal by returning without
    raising, and returns ``None`` on success too. Presence alone is therefore
    not proof; the handler must compare the stored value.
    """
    _activate(env, "alpha")
    monkeypatch.setattr(env.config, "save_env_value", lambda key, value: None)

    handler = _Handler()
    _put(env, handler, {"name": "PROVIDER_API_KEY", "value": "sk-live"})
    assert handler.status != 200, "a refused write must not report success"
    assert handler.status == 409, f"expected 409, got {handler.status}"


def test_replaced_value_mismatch_is_not_reported_as_success(env, monkeypatch):
    """A refusal that leaves the OLD value must not report the new one stored."""
    _activate(env, "alpha")
    home = _home(env, "alpha")
    _seed_env(env.config, env.constants, home, "PROVIDER_API_KEY", "first")

    # The real writer declines without raising and leaves the previous value.
    monkeypatch.setattr(env.config, "save_env_value", lambda key, value: None)

    handler = _Handler()
    _put(env, handler, {"name": "PROVIDER_API_KEY", "value": "second"})
    assert handler.status == 409, (
        "a refused replacement whose old value survives must answer 409, "
        f"got {handler.status}"
    )
    assert _scoped_env(env.config, env.constants, home)["PROVIDER_API_KEY"] == "first"


def test_failed_read_back_returns_non_2xx(env, monkeypatch):
    """A read-back that cannot see the write must not be answered ok:true.

    The installed writer's return value cannot distinguish success from a
    silent refusal, so the handler verifies the mutation by reading the
    AUTHORIZED profile's ``.env`` back. If that read fails, the write is
    UNVERIFIED and must not report success.
    """

    def _broken_load():
        raise OSError("read-back exploded")

    _activate(env, "alpha")
    monkeypatch.setattr(env.config, "load_env", _broken_load)

    handler = _Handler()
    _put(env, handler, {"name": "READBACK_KEY", "value": "v"})
    assert handler.status not in (200, 201), (
        f"an unverifiable PUT must not report success ({handler.status})"
    )
    assert handler.status == 409, f"expected 409, got {handler.status}"


def test_unverifiable_delete_is_not_reported_as_success(env, monkeypatch):
    """A delete whose read-back fails must not answer ok."""
    _activate(env, "alpha")
    _seed_env(env.config, env.constants, _home(env, "alpha"), "DELETED_KEY", "v")

    def _broken_load():
        raise OSError("read-back exploded")

    monkeypatch.setattr(env.config, "load_env", _broken_load)

    handler = _Handler()
    _delete(env, handler, "DELETED_KEY")
    assert handler.status == 409, (
        f"an unverifiable delete must not report success, got {handler.status}"
    )


def test_filesystem_refusal_returns_non_2xx(env, monkeypatch):
    """A refused atomic replace must not be reported as success."""
    _activate(env, "alpha")
    home = _home(env, "alpha")
    _seed_env(env.config, env.constants, home, "PINNED_KEY", "old-value")

    def _refuse(env_path, lines, *, preserve_mode):
        raise OSError("read-only file system")

    # FAULT INJECTION on the real writer's atomic-replace seam.
    monkeypatch.setattr(env.config, "_write_env_lines", _refuse)

    handler = _Handler()
    _put(env, handler, {"name": "PINNED_KEY", "value": "new-value"})
    assert handler.status not in (200, 201), (
        f"a write the filesystem refused was reported as success ({handler.status})"
    )
    assert 400 <= handler.status < 500, f"expected 4xx, got {handler.status}"
    assert _scoped_env(env.config, env.constants, home)["PINNED_KEY"] == "old-value"


# ── values, redaction, lock ─────────────────────────────────────────────────


def test_value_with_control_characters_is_rejected(env, monkeypatch):
    """NUL / CR / LF and friends must not reach the writer."""
    _activate(env, "alpha")
    seen: list[tuple[str, str]] = []
    real_save = env.config.save_env_value

    def _recording(key, value):
        seen.append((key, value))
        return real_save(key, value)

    monkeypatch.setattr(env.config, "save_env_value", _recording)
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
    assert seen == [], "the writer was reached with a control-character value"
    assert not _scoped_env(env.config, env.constants, _home(env, "alpha"))

    # A clean value still writes, so the rejection is not over-broad.
    handler = _Handler()
    _put(env, handler, {"name": "GOOD_VALUE", "value": "plain-value"})
    assert handler.status == 200, handler.payload
    assert _scoped_env(env.config, env.constants, _home(env, "alpha"))[
        "GOOD_VALUE"
    ] == "plain-value"


def test_redaction_reveals_no_characters(env):
    """The preview leaks nothing — not even the first/last two characters."""
    _activate(env, "alpha")
    _seed_env(
        env.config,
        env.constants,
        _home(env, "alpha"),
        "TOKEN",
        "abcdefghijklmnopqrstuvwxyz",
    )

    handler = _Handler()
    _keys(env, handler)
    entry = [k for k in handler.payload["keys"] if k["name"] == "TOKEN"][0]
    assert entry["redacted_value"] == "*" * 26
    assert "a" not in entry["redacted_value"]
    assert "z" not in entry["redacted_value"]


def test_root_alias_resolves_to_default(env):
    """``default`` collapses onto the root home; a named profile keeps its name."""
    assert env.mod._canonical_profile_name("default") == "default"
    assert env.mod._canonical_profile_name("alpha") == "alpha"


def test_scope_never_activates_process_wide_multi_profile_hosting(env):
    """A custom-key request must not flip the real multiplex flag.

    ``hermes_cli.web_server_profiles._profile_scope`` calls
    ``activate_multi_profile_hosting()`` for any non-launch home, a
    process-global one-way switch. The API's own scope uses the context-local
    ``set_hermes_home_override`` instead. This asserts the REAL flag through
    ``agent.secret_scope.serves_routed_profile`` before and after a request —
    the previous round checked a fake activation counter instead.
    """
    _activate(env, "alpha")
    assert env.serves_routed_profile() is False, (
        "the process starts multiplexed; this test would assert the wrong branch"
    )

    handler = _Handler()
    _put(env, handler, {"name": "NO_SIDE_EFFECT", "value": "v"})
    assert handler.status == 200, handler.payload
    assert env.serves_routed_profile() is False, (
        "a .env request left the process multiplexed; every concurrent unscoped "
        "read would now fail closed"
    )

    _keys(env, handler)
    assert handler.status == 200, handler.payload
    assert env.serves_routed_profile() is False, "not even a read may flip the flag"


def test_write_paths_hold_the_provider_writer_env_lock(env, monkeypatch):
    """The custom-key writer must serialise against the provider-key writer."""
    import api.streaming as streaming

    real_lock = streaming._ENV_LOCK
    inside: list[bool] = []
    original_save = env.config.save_env_value

    def _probe_save(key, value):
        # Runs while the handler holds its lock. A plain Lock is not reentrant,
        # so this succeeds iff the handler did NOT hold it.
        acquired = real_lock.acquire(blocking=False)
        if acquired:
            real_lock.release()
        inside.append(not acquired)
        return original_save(key, value)

    _activate(env, "alpha")
    monkeypatch.setattr(env.config, "save_env_value", _probe_save)

    handler = _Handler()
    _put(env, handler, {"name": "LOCKED_KEY", "value": "v"})
    assert handler.status == 200, handler.payload
    assert inside == [True], (
        "the mutation must run while the env lock is held — otherwise a "
        f"second thread could acquire it (observed {inside})"
    )
    grabbed = real_lock.acquire(blocking=False)
    assert grabbed, "the env lock must be released after the request"
    if grabbed:
        real_lock.release()

    inside.clear()
    original_remove = env.config.remove_env_value

    def _probe_remove(key):
        acquired = real_lock.acquire(blocking=False)
        if acquired:
            real_lock.release()
        inside.append(not acquired)
        return original_remove(key)

    monkeypatch.setattr(env.config, "remove_env_value", _probe_remove)
    _delete(env, handler, "LOCKED_KEY")
    assert handler.status == 200, handler.payload
    assert inside == [True], "the delete must run inside the same env lock"


def test_lock_is_not_reentered_by_the_read_back(env):
    """The read-back inside a locked PUT must not deadlock."""
    import api.streaming as streaming

    lock = streaming._ENV_LOCK
    assert lock.acquire(blocking=False)
    lock.release()  # proven uncontended before the request

    _activate(env, "alpha")
    handler = _Handler()
    _put(env, handler, {"name": "NO_DEADLOCK", "value": "v"})
    assert handler.status == 200, handler.payload
    acquired = lock.acquire(blocking=False)
    assert acquired, "the env lock must be free after a successful PUT"
    lock.release()


# ── the shared process environment ─────────────────────────────────────────


def test_named_profile_put_and_delete_leave_os_environ_unchanged(env):
    """A named-profile PUT and DELETE must not change the shared ``os.environ``.

    The installed ``save_env_value`` / ``remove_env_value`` call
    ``_publish_env_value`` after the file write. If that reached the shared
    process environment, one PUT for a named profile would set the variable
    for every profile this WebUI serves, and one DELETE would pop it even when
    the launch profile's own ``.env`` defines the same name.

    The assertion is against the REAL writer: under a named-profile home
    override ``agent.secret_scope.serves_routed_profile()`` is True, so
    ``_publish_env_value``'s target list is empty and neither ``os.environ``
    nor a scope mapping is touched. That is the property that makes the write
    safe, and it is pinned here rather than assumed.

    WebUI's per-turn env install is why a same-request read still sees the
    value: each turn rebuilds ``get_profile_runtime_env(profile_home)`` from
    that profile's own ``.env`` and installs it (api/streaming.py). It is NOT
    a fallback for a leak — the install is an ``update`` that would leave a
    foreign key behind in ``os.environ`` — which is exactly why the writer
    must not publish in the first place, and why this test asserts the
    snapshot instead of trusting a later turn to overwrite it.
    """
    _activate(env, "alpha")
    launch_probe = "WEBUI_7870_LAUNCH_PROBE"
    named_probe = "WEBUI_7870_NAMED_PROBE"
    alpha_home = _home(env, "alpha")

    # The launch profile's own .env defines the shared probe, and the process
    # holds its live value: a stray publish or pop must not touch it.
    _seed_env(env.config, env.constants, env.base, launch_probe, "launch-value")
    os.environ[launch_probe] = "launch-value"
    os.environ.pop(named_probe, None)
    before = dict(os.environ)
    try:
        # The named profile also defines the launch probe, so its DELETE has
        # something real to remove from the named home.
        _seed_env(env.config, env.constants, alpha_home, launch_probe, "alpha-value")

        handler = _Handler()
        _put(env, handler, {"name": named_probe, "value": "named-value"})
        assert handler.status == 200, handler.payload
        assert _scoped_env(env.config, env.constants, alpha_home)[named_probe] == (
            "named-value"
        )
        assert os.environ.get(named_probe) is None, (
            "a named-profile PUT published into the shared environment"
        )

        _delete(env, handler, named_probe)
        assert handler.status == 200, handler.payload
        _delete(env, handler, launch_probe)
        assert handler.status == 200, handler.payload
        assert launch_probe not in _scoped_env(env.config, env.constants, alpha_home)

        assert dict(os.environ) == before, (
            "a named-profile PUT/DELETE changed the shared os.environ; "
            "every profile served by this WebUI process would see it"
        )
        assert os.environ.get(launch_probe) == "launch-value", (
            "a named-profile DELETE popped the launch profile's live value"
        )
    finally:
        for key in set(os.environ) - set(before):
            os.environ.pop(key, None)
        for key, value in before.items():
            os.environ[key] = value
