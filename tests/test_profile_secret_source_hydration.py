"""
Regression tests: profile-scoped WebUI turns must see provider credentials that
live ONLY in an external secret source.

A Hermes profile can reference an external secret manager (Bitwarden Secrets
Manager, 1Password) instead of storing provider keys in the profile's plaintext
``.env``. The CLI and the gateway hydrate those sources through
``hermes_cli.env_loader.hydrate_profile_secret_sources(home)``.

``api.profiles.get_profile_runtime_env()`` is the single chokepoint that builds
the env for every profile-scoped WebUI path — the streaming turn entry
(``api/streaming.py``), ``profile_env_for_background_worker()`` and
``profile_env_for_request()``. It read only ``config.yaml``'s terminal block and
the profile's ``.env``, so an external-source-only key was silently absent from
the agent turn and the turn ran with no credential.

Contract pinned here:
  - external-source keys are hydrated into the runtime env (the bug);
  - GAP-FILL ONLY: a key the profile's ``config.yaml``/``.env`` already defines
    keeps its value, so no profile that works today changes behavior;
  - ``_PROTECTED_ENV_KEYS`` are never settable from a secret source (#4589 —
    an operator/deployment posture stays operator-only on every runtime path);
  - ``os.environ`` is never mutated;
  - a graceful no-op on older agents that lack the hydration symbol, and on a
    hydrator that raises.

The second half covers the ROOT/default profile, which short-circuits every
profile scope on the premise that its env is already process-loaded — true for
its plaintext ``.env``, false for an external secret source. See
``_root_profile_secret_source_scope``.

The agent-side module is stubbed via ``sys.modules`` injection (house pattern),
so these tests need neither a real hermes-agent install nor a real secret
manager.
"""

import os
import sys
import types
from contextlib import contextmanager

import pytest
import yaml

import api.profiles as profiles
from api.profiles import get_profile_runtime_env


def _make_profile(tmp_path, *, dotenv: str = "", terminal: dict | None = None):
    """Create a named profile home with an optional config.yaml + .env."""
    home = tmp_path / "profiles" / "vaulted"
    home.mkdir(parents=True)
    if terminal is not None:
        (home / "config.yaml").write_text(
            yaml.safe_dump({"terminal": terminal}, sort_keys=False), encoding="utf-8"
        )
    if dotenv:
        (home / ".env").write_text(dotenv, encoding="utf-8")
    return home


def _install_hydrator(monkeypatch, hydrate):
    """Inject a stub ``hermes_cli.env_loader`` exposing *hydrate*.

    ``hydrate`` may be None to simulate an OLDER agent whose ``env_loader``
    module exists but does not expose the hydration symbol at all.
    """
    module = types.ModuleType("hermes_cli.env_loader")
    if hydrate is not None:
        module.hydrate_profile_secret_sources = hydrate
    monkeypatch.setitem(sys.modules, "hermes_cli.env_loader", module)
    # Resolver caches its probe result; reset so the stub is re-resolved.
    monkeypatch.setattr(
        profiles, "_secret_source_hydrator_available", None, raising=False
    )
    return module


def _disable_hydrator(monkeypatch):
    """Simulate an agent with no hydration symbol, deterministically.

    Forces the cached "unavailable" verdict rather than relying on whether
    hermes_cli happens to be installed on the machine running the suite.
    """
    monkeypatch.delitem(sys.modules, "hermes_cli.env_loader", raising=False)
    monkeypatch.setattr(
        profiles, "_secret_source_hydrator_available", False, raising=False
    )


def test_external_secret_source_key_reaches_profile_runtime_env(tmp_path, monkeypatch):
    """[the bug] A provider key that exists ONLY in an external secret source
    must reach the profile-scoped agent turn.

    Without hydration the profile's ``.env`` is the only credential source, so
    DEEPSEEK_API_KEY is absent and the turn runs unauthenticated.
    """
    home = _make_profile(
        tmp_path,
        dotenv="HERMES_MAX_ITERATIONS=90\n",
        terminal={"backend": "ssh", "ssh_host": "pollux"},
    )
    calls = []

    def _hydrate(profile_home):
        calls.append(profile_home)
        return {"DEEPSEEK_API_KEY": "sk-from-vault", "LANGFUSE_SECRET_KEY": "lf-vault"}

    _install_hydrator(monkeypatch, _hydrate)

    env = get_profile_runtime_env(home)

    assert env["DEEPSEEK_API_KEY"] == "sk-from-vault"
    assert env["LANGFUSE_SECRET_KEY"] == "lf-vault"
    # Existing legs still work and the hydrator is asked about THIS profile home.
    assert env["HERMES_MAX_ITERATIONS"] == "90"
    assert env["TERMINAL_ENV"] == "ssh"
    assert [str(c) for c in calls] == [str(home)]


def test_profile_dotenv_value_wins_over_external_secret_source(tmp_path, monkeypatch):
    """[gap-fill only] Hydration must not override a key the profile already
    defines, so a profile that works today cannot change behavior."""
    home = _make_profile(
        tmp_path,
        dotenv="DEEPSEEK_API_KEY=sk-from-dotenv\n",
        terminal={"backend": "ssh", "ssh_host": "pollux-config"},
    )

    _install_hydrator(
        monkeypatch,
        lambda _home: {
            "DEEPSEEK_API_KEY": "sk-from-vault",
            "TERMINAL_SSH_HOST": "pollux-from-vault",
            "OPENAI_API_KEY": "sk-openai-vault",
        },
    )

    env = get_profile_runtime_env(home)

    assert env["DEEPSEEK_API_KEY"] == "sk-from-dotenv"
    # config.yaml-derived keys are "already defined" too.
    assert env["TERMINAL_SSH_HOST"] == "pollux-config"
    # ...but a genuinely absent key is still filled.
    assert env["OPENAI_API_KEY"] == "sk-openai-vault"


def test_secret_source_cannot_set_protected_operator_keys(tmp_path, monkeypatch):
    """[#4589] A profile-controlled secret source must not be able to flip an
    operator/deployment posture key, exactly as the ``.env`` leg forbids."""
    home = _make_profile(tmp_path, dotenv="HERMES_MAX_ITERATIONS=90\n")

    _install_hydrator(
        monkeypatch,
        lambda _home: {
            "HERMES_WEBUI_ISOLATED_PROFILE": "0",
            "DEEPSEEK_API_KEY": "sk-from-vault",
        },
    )

    env = get_profile_runtime_env(home)

    assert "HERMES_WEBUI_ISOLATED_PROFILE" not in env
    for key in profiles._PROTECTED_ENV_KEYS:
        assert key not in env
    assert env["DEEPSEEK_API_KEY"] == "sk-from-vault"


def test_hydration_does_not_mutate_process_environ(tmp_path, monkeypatch):
    """``get_profile_runtime_env`` is a pure read: callers own env application."""
    home = _make_profile(tmp_path, dotenv="HERMES_MAX_ITERATIONS=90\n")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    _install_hydrator(monkeypatch, lambda _home: {"DEEPSEEK_API_KEY": "sk-from-vault"})

    before = dict(os.environ)
    env = get_profile_runtime_env(home)

    assert env["DEEPSEEK_API_KEY"] == "sk-from-vault"
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert dict(os.environ) == before


def test_blank_and_non_mapping_hydration_results_are_ignored(tmp_path, monkeypatch):
    """Empty/None values carry no credential; a non-mapping result is ignored.

    Mirrors the ``if k and v`` rule the ``.env`` leg already applies, so an
    empty vault entry cannot shadow a later resolution.
    """
    home = _make_profile(tmp_path, dotenv="HERMES_MAX_ITERATIONS=90\n")

    _install_hydrator(
        monkeypatch,
        lambda _home: {"EMPTY_KEY": "", "NONE_KEY": None, "REAL_KEY": "v"},
    )
    env = get_profile_runtime_env(home)
    assert "EMPTY_KEY" not in env
    assert "NONE_KEY" not in env
    assert env["REAL_KEY"] == "v"

    _install_hydrator(monkeypatch, lambda _home: ["not", "a", "mapping"])
    env = get_profile_runtime_env(home)
    assert env["HERMES_MAX_ITERATIONS"] == "90"


@pytest.mark.parametrize(
    "hydrate",
    [
        None,  # older agent: module present, hydration symbol absent
        pytest.param(
            lambda _home: (_ for _ in ()).throw(RuntimeError("vault unreachable")),
            id="hydrator-raises",
        ),
    ],
    ids=["symbol-absent", "hydrator-raises"],
)
def test_missing_symbol_or_failing_hydrator_degrades_gracefully(
    tmp_path, monkeypatch, hydrate
):
    """Graceful degradation: the pre-existing env is returned unchanged and no
    exception escapes to the turn."""
    home = _make_profile(
        tmp_path,
        dotenv="HERMES_MAX_ITERATIONS=90\n",
        terminal={"backend": "ssh", "ssh_host": "pollux"},
    )

    _install_hydrator(monkeypatch, hydrate)

    env = get_profile_runtime_env(home)

    assert env["HERMES_MAX_ITERATIONS"] == "90"
    assert env["TERMINAL_ENV"] == "ssh"
    assert env["TERMINAL_SSH_HOST"] == "pollux"


def test_older_agent_without_hydration_support_is_a_no_op(tmp_path, monkeypatch):
    """No hermes_cli hydration support at all: behavior is byte-identical to the
    pre-fix runtime env (terminal config + ``.env`` only)."""
    home = _make_profile(
        tmp_path,
        dotenv="HERMES_MAX_ITERATIONS=90\n",
        terminal={"backend": "ssh", "ssh_host": "pollux"},
    )
    _disable_hydrator(monkeypatch)

    assert profiles._resolve_secret_source_hydrator() is None
    assert get_profile_runtime_env(home) == {
        "TERMINAL_ENV": "ssh",
        "TERMINAL_SSH_HOST": "pollux",
        "HERMES_MAX_ITERATIONS": "90",
    }


def test_resolver_ignores_module_without_hydration_symbol(monkeypatch):
    """The resolver checks for the SYMBOL, not just an importable module."""
    _install_hydrator(monkeypatch, None)
    assert profiles._resolve_secret_source_hydrator() is None

    _install_hydrator(monkeypatch, lambda _home: {})
    assert callable(profiles._resolve_secret_source_hydrator())


def test_resolver_honors_already_imported_module_over_negative_cache(monkeypatch):
    """An agent installed/imported after a failed probe must still be picked up
    (house behavior of ``_resolve_secret_scope_module``)."""
    module = types.ModuleType("hermes_cli.env_loader")
    module.hydrate_profile_secret_sources = lambda _home: {}
    monkeypatch.setitem(sys.modules, "hermes_cli.env_loader", module)
    monkeypatch.setattr(
        profiles, "_secret_source_hydrator_available", False, raising=False
    )

    assert callable(profiles._resolve_secret_source_hydrator())


# ── Root/default profile scopes ──────────────────────────────────────────────
#
# `_reload_dotenv()` loads the root profile's PLAINTEXT `~/.hermes/.env` into
# os.environ at startup, which is why every profile scope short-circuits root.
# Nothing in the WebUI process hydrates root's EXTERNAL secret sources, so a key
# that lives only there is absent from os.environ for the whole process
# lifetime: the streaming turn (which calls get_profile_runtime_env directly)
# had it, while the short-circuited paths — background title/compression
# workers, /api/providers, /api/models/live — ran without the credential.


@pytest.fixture(autouse=True)
def _isolated_thread_env():
    """Keep the WebUI thread-local env channel clean between tests."""
    from api.config import _clear_thread_env, _thread_ctx

    _clear_thread_env()
    _thread_ctx.block_process_env_fallback = False
    yield
    _clear_thread_env()
    _thread_ctx.block_process_env_fallback = False


def _make_root_home(tmp_path, monkeypatch):
    """Point the root/default profile at an empty home under *tmp_path*."""
    base = tmp_path / ".hermes"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    return base


ROOT_SCOPES = [
    pytest.param(
        lambda: profiles.profile_env_for_background_worker("default", "background title"),
        id="background-worker",
    ),
    pytest.param(
        lambda: profiles.profile_env_for_active_request_readonly("/api/providers"),
        id="active-request-readonly",
    ),
    pytest.param(
        lambda: profiles.profile_env_for_active_request("/api/models/live"),
        id="active-request-mirrored",
    ),
    pytest.param(
        lambda: profiles.profile_scope_for_detached_worker("default", "models rebuild"),
        id="detached-worker",
    ),
]


@pytest.mark.parametrize("scope", ROOT_SCOPES)
def test_root_scopes_expose_external_only_secret(tmp_path, monkeypatch, scope):
    """[the bug] Every root scope must see a credential that lives ONLY in the
    root profile's external secret source.

    Before the fix these scopes short-circuited to a bare ``yield``, so
    ``/api/providers`` reported the provider unconfigured and the detached
    title/compression workers ran with no credential — even though the main
    streaming turn for the very same profile had the key.
    """
    from api.config import _thread_local_env_value

    _make_root_home(tmp_path, monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _install_hydrator(monkeypatch, lambda _home: {"DEEPSEEK_API_KEY": "sk-from-vault"})

    # The request-scoped wrappers read the active profile from thread-local
    # state; pin it to root so the assertion is about the root branch.
    profiles.set_request_profile("default")
    try:
        assert _thread_local_env_value("DEEPSEEK_API_KEY") == ""
        with scope():
            assert _thread_local_env_value("DEEPSEEK_API_KEY") == "sk-from-vault"
        # ...and the channel is restored on exit.
        assert _thread_local_env_value("DEEPSEEK_API_KEY") == ""
    finally:
        profiles.clear_request_profile()


def test_root_scope_hydrates_the_root_home(tmp_path, monkeypatch):
    """The hydrator is asked about the ROOT home, not a named profile home."""
    base = _make_root_home(tmp_path, monkeypatch)
    calls = []

    def _hydrate(home):
        calls.append(str(home))
        return {}

    _install_hydrator(monkeypatch, _hydrate)

    with profiles.profile_env_for_background_worker("default", "background title"):
        pass

    assert calls == [str(base)]


def test_renamed_root_profile_takes_additive_root_scope(tmp_path, monkeypatch):
    """A RENAMED root profile must short-circuit to the additive root scope, not
    the named-profile isolation path.

    ``_is_root_profile`` matches any name ``list_profiles_api`` reports as
    ``is_default=True``. ``profile_env_for_background_worker`` was the one scope
    still comparing against the literal ``"default"``, so a renamed root fell
    through to the isolation path — ``block_process_env_fallback=True`` plus a
    secret-name scrub that hid process-loaded credentials. It must hydrate via
    the additive root scope instead.
    """
    from api.config import _thread_ctx, _thread_local_env_value

    base = _make_root_home(tmp_path, monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    calls = []

    def _hydrate(home):
        calls.append(str(home))
        return {"DEEPSEEK_API_KEY": "kinni-root-key"}

    # Report the root profile as renamed to "kinni".
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "kinni", "is_default": True, "path": str(base)}],
    )
    profiles._invalidate_root_profile_cache()
    _install_hydrator(monkeypatch, _hydrate)
    try:
        with profiles.profile_env_for_background_worker("kinni", "background title"):
            assert _thread_local_env_value("DEEPSEEK_API_KEY") == "kinni-root-key"
            # Root scope is additive: no isolation block on process-env fallback.
            assert not getattr(_thread_ctx, "block_process_env_fallback", False)
    finally:
        profiles._invalidate_root_profile_cache()

    # The hydrator was asked about the ROOT home, not a "kinni" profile home.
    assert calls == [str(base)]


def test_process_env_wins_over_root_secret_source(tmp_path, monkeypatch):
    """[gap-fill only] For root the PROCESS env is the already-resolved profile
    env, so a credential provided by docker ``-e`` / systemd / the repo ``.env``
    must never be shadowed by a secret source."""
    from api.config import _thread_local_env_value

    _make_root_home(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-process")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _install_hydrator(
        monkeypatch,
        lambda _home: {
            "DEEPSEEK_API_KEY": "sk-from-vault",
            "OPENAI_API_KEY": "sk-openai-vault",
        },
    )

    with profiles.profile_env_for_background_worker("default", "background title"):
        assert _thread_local_env_value("DEEPSEEK_API_KEY") == "sk-from-process"
        # ...but a genuinely absent key is still filled.
        assert _thread_local_env_value("OPENAI_API_KEY") == "sk-openai-vault"


def test_root_scope_is_additive_and_never_mutates_process_env(tmp_path, monkeypatch):
    """The root scope only ADDS to the thread-local channel: it neither touches
    ``os.environ`` nor blocks the process-env fallback, so every credential that
    resolves today keeps resolving inside the scope."""
    from api.config import _thread_local_env_value

    _make_root_home(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-process-only")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _install_hydrator(monkeypatch, lambda _home: {"DEEPSEEK_API_KEY": "sk-from-vault"})

    before = dict(os.environ)
    with profiles.profile_env_for_background_worker("default", "background title"):
        # A process-only credential stays visible (no block_process_env_fallback).
        assert _thread_local_env_value("ANTHROPIC_API_KEY") == "sk-process-only"
        assert "DEEPSEEK_API_KEY" not in os.environ
        assert dict(os.environ) == before
    assert dict(os.environ) == before


def test_root_scope_filters_shell_identity_and_protected_keys(tmp_path, monkeypatch):
    """A root secret source can no more override HOME/PATH or an operator
    posture key than a named profile's ``.env`` can (#4589 + gateway parity)."""
    from api.config import _thread_local_env_value

    _make_root_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", "/real/home")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("PATH", raising=False)
    _install_hydrator(
        monkeypatch,
        lambda _home: {
            "HOME": "/vault/home",
            "PATH": "/vault/bin",
            "HERMES_WEBUI_ISOLATED_PROFILE": "0",
            "DEEPSEEK_API_KEY": "sk-from-vault",
        },
    )

    with profiles.profile_env_for_background_worker("default", "background title"):
        assert _thread_local_env_value("HOME") == "/real/home"
        assert _thread_local_env_value("HERMES_WEBUI_ISOLATED_PROFILE") == ""
        assert _thread_local_env_value("DEEPSEEK_API_KEY") == "sk-from-vault"


def test_root_scope_does_not_leak_into_an_isolated_scope(tmp_path, monkeypatch):
    """A root scope entered INSIDE a profile-isolated scope must not inject
    anything: that scope owns the thread's credential view, and adding root's
    secrets to it is exactly the cross-profile leak it exists to prevent."""
    from api.config import _set_thread_env, _thread_ctx, _thread_local_env_value

    _make_root_home(tmp_path, monkeypatch)
    _install_hydrator(monkeypatch, lambda _home: {"DEEPSEEK_API_KEY": "sk-root-vault"})

    _set_thread_env(HERMES_HOME="/named/profile/home")
    _thread_ctx.block_process_env_fallback = True
    with profiles.profile_env_for_background_worker("default", "background title"):
        assert _thread_local_env_value("DEEPSEEK_API_KEY") == ""
    assert _thread_local_env_value("HERMES_HOME") == "/named/profile/home"


def test_enclosing_thread_env_wins_over_root_hydration(tmp_path, monkeypatch):
    """An enclosing (non-isolated) thread env is authoritative key-by-key; the
    root scope only fills gaps, and restores the original on exit."""
    from api.config import _set_thread_env, _thread_ctx, _thread_local_env_value

    _make_root_home(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _install_hydrator(
        monkeypatch,
        lambda _home: {"DEEPSEEK_API_KEY": "sk-from-vault", "OPENAI_API_KEY": "sk-vault"},
    )

    _set_thread_env(DEEPSEEK_API_KEY="sk-enclosing")
    with profiles.profile_env_for_background_worker("default", "background title"):
        assert _thread_local_env_value("DEEPSEEK_API_KEY") == "sk-enclosing"
        assert _thread_local_env_value("OPENAI_API_KEY") == "sk-vault"
    assert getattr(_thread_ctx, "env", {}) == {"DEEPSEEK_API_KEY": "sk-enclosing"}


def test_root_scope_does_not_install_agent_secret_scope(tmp_path, monkeypatch):
    """``agent.secret_scope`` REPLACES the agent's credential view, so a root
    scope carrying only the gap-fill keys would HIDE the process-loaded ones.
    The root path must not install one."""
    _make_root_home(tmp_path, monkeypatch)
    _install_hydrator(monkeypatch, lambda _home: {"DEEPSEEK_API_KEY": "sk-from-vault"})

    calls = []
    fake_scope = types.ModuleType("agent.secret_scope")
    fake_scope.set_secret_scope = lambda scope: calls.append(dict(scope))
    fake_scope.reset_secret_scope = lambda token: None
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_scope)
    monkeypatch.setattr(profiles, "_secret_scope_available", None, raising=False)

    with profiles.profile_env_for_background_worker("default", "background title"):
        pass

    assert calls == []


@pytest.mark.parametrize(
    "hydrate",
    [
        None,
        pytest.param(
            lambda _home: (_ for _ in ()).throw(RuntimeError("vault unreachable")),
            id="hydrator-raises",
        ),
        pytest.param(lambda _home: {}, id="no-external-secrets"),
    ],
    ids=["symbol-absent", "hydrator-raises", "no-external-secrets"],
)
def test_root_scope_degrades_to_todays_behavior(tmp_path, monkeypatch, hydrate):
    """No hydration symbol, an unreachable vault, or a root profile with no
    external source at all: the scope is a bare ``yield`` and leaves the
    thread-local channel untouched, exactly as before the fix."""
    from api.config import _thread_ctx

    _make_root_home(tmp_path, monkeypatch)
    if hydrate is None:
        _disable_hydrator(monkeypatch)
    else:
        _install_hydrator(monkeypatch, hydrate)

    with profiles.profile_env_for_background_worker("default", "background title"):
        assert getattr(_thread_ctx, "env", {}) == {}


@pytest.mark.parametrize(
    ("hydrate", "isolated"),
    [
        pytest.param(lambda _home: {"DEEPSEEK_API_KEY": "sk-vault"}, False, id="scope-active"),
        pytest.param(lambda _home: {}, False, id="scope-inactive"),
        # The isolated branch returns without injecting anything — the path most
        # likely to be written as a `yield` inside the scope's own try/except.
        pytest.param(lambda _home: {"DEEPSEEK_API_KEY": "sk-vault"}, True, id="isolated"),
        pytest.param(
            lambda _home: (_ for _ in ()).throw(RuntimeError("vault unreachable")),
            False,
            id="hydrator-raises",
        ),
    ],
)
def test_root_scope_propagates_body_exceptions(tmp_path, monkeypatch, hydrate, isolated):
    """A worker that raises inside the scope must surface ITS OWN exception.

    The scope's error handling must wrap the RESOLUTION, never the ``yield``: a
    ``try/except Exception`` around a yield catches whatever the caller's body
    raised, then yields a second time — contextlib turns that into
    ``RuntimeError: generator didn't stop after throw()`` and the real worker
    error is lost.
    """
    from api.config import _set_thread_env, _thread_ctx

    _make_root_home(tmp_path, monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _install_hydrator(monkeypatch, hydrate)
    if isolated:
        _set_thread_env(HERMES_HOME="/named/profile/home")
        _thread_ctx.block_process_env_fallback = True

    with pytest.raises(ValueError, match="worker blew up"):
        with profiles.profile_env_for_background_worker("default", "background title"):
            raise ValueError("worker blew up")

    # ...and the thread-local channel is still unwound.
    expected = {"HERMES_HOME": "/named/profile/home"} if isolated else {}
    assert getattr(_thread_ctx, "env", {}) == expected


def test_named_profile_does_not_take_the_root_hydration_path(tmp_path, monkeypatch):
    """The root scope is root-only: a named profile still resolves its whole
    runtime env through ``get_profile_runtime_env`` as it does today."""
    _make_root_home(tmp_path, monkeypatch)
    entered = []

    @contextmanager
    def _tracking_scope(purpose, logger_override=None):
        entered.append(purpose)
        yield

    monkeypatch.setattr(profiles, "_root_profile_secret_source_scope", _tracking_scope)
    _install_hydrator(monkeypatch, lambda _home: {})

    profiles.set_request_profile("work")
    try:
        with profiles.profile_env_for_active_request_readonly("/api/providers"):
            pass
    finally:
        profiles.clear_request_profile()

    assert entered == []
