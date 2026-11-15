"""#9084: the per-turn secret scope must be stamped with the profile home it was built for.

``profile_env_for_background_worker`` and ``profile_env_for_active_request_readonly``
install the profile's env as the Hermes Agent secret scope. The Agent treats an
UNSTAMPED scope as the process (launch) home's, and since Agent commit e4888ba0
``agent.secret_scope.refresh_installed_secret_scope(home)`` refuses to refresh a scope
whose owner (``bound.profile_home``, else the routing process home) differs from
``home``. ``hermes_cli/plugins.py`` calls that refresh with the turn's profile home
after plugin secret-source discovery, so on a named profile the plugin-discovered
credentials never reached the turn.

WebUI now passes ``profile_home=<that profile's home>`` when the Agent's
``set_secret_scope`` accepts it, decided once from the signature (older Agents get
the legacy positional call, never a TypeError retry).
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from api import profiles


PROFILE = "coder"
CONTEXT_MANAGERS = ("background_worker", "active_request_readonly")


def _setup_profile(monkeypatch, tmp_path) -> tuple[Path, Path]:
    base = tmp_path / ".hermes"
    home = base / "profiles" / PROFILE
    home.mkdir(parents=True)
    (home / ".env").write_text("CODER_PLUGIN_KEY=coder-secret\n", encoding="utf-8")
    (base / ".env").write_text("DEFAULT_ONLY_KEY=default-secret\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("", "default"))
    monkeypatch.setattr(profiles, "_secret_scope_available", None)
    monkeypatch.setattr(profiles, "_hermes_home_override_available", None)
    return base, home


@contextlib.contextmanager
def _enter(which: str, profile: str = PROFILE):
    if which == "background_worker":
        with profiles.profile_env_for_background_worker(
            profile, "issue 9084", scope_skill_modules=False
        ):
            yield
        return
    profiles.set_request_profile(profile)
    try:
        with profiles.profile_env_for_active_request_readonly("issue 9084"):
            yield
    finally:
        profiles.clear_request_profile()


def _install_fake_secret_scope(monkeypatch, set_secret_scope, resets):
    """Model the Agent's ``agent.secret_scope`` with the given setter (agent-free)."""
    agent_pkg: Any = types.ModuleType("agent")
    agent_pkg.__path__ = []
    module: Any = types.ModuleType("agent.secret_scope")
    module.set_secret_scope = set_secret_scope
    module.reset_secret_scope = resets.append
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    return module


def _count_signature_calls(monkeypatch, target) -> list:
    seen = []
    real_signature = inspect.signature

    def counting_signature(obj, *args, **kwargs):
        if obj is target:
            seen.append(obj)
        return real_signature(obj, *args, **kwargs)

    monkeypatch.setattr(inspect, "signature", counting_signature)
    return seen


# 1 ── the stamp reaches a keyword-capable Agent for both context managers ─────

@pytest.mark.parametrize("which", CONTEXT_MANAGERS)
def test_secret_scope_is_stamped_with_the_profile_home(monkeypatch, tmp_path, which):
    _base, home = _setup_profile(monkeypatch, tmp_path)
    calls, resets = [], []

    def set_secret_scope(secrets, *, profile_home=None):
        calls.append((dict(secrets), profile_home))
        return "scope-token"

    _install_fake_secret_scope(monkeypatch, set_secret_scope, resets)
    expected_home = profiles.get_hermes_home_for_profile(PROFILE)
    assert Path(expected_home).resolve() == home.resolve()

    with _enter(which):
        assert len(calls) == 1
    secrets, stamped_home = calls[0]
    assert stamped_home == str(expected_home), (
        f"{which} installed the secret scope unstamped/foreign-stamped: {stamped_home!r}"
    )
    # The stamp names the same home the scope's mapping was built for.
    assert secrets["HERMES_HOME"] == stamped_home
    assert resets == ["scope-token"]


# 2 ── a legacy Agent (no keyword) keeps the positional install, decided once ──

@pytest.mark.parametrize("which", CONTEXT_MANAGERS)
def test_legacy_set_secret_scope_is_called_positionally_and_decided_once(
    monkeypatch, tmp_path, which
):
    _setup_profile(monkeypatch, tmp_path)
    calls, resets = [], []

    def legacy_set_secret_scope(secrets):
        calls.append(dict(secrets))
        return f"legacy-token-{len(calls)}"

    _install_fake_secret_scope(monkeypatch, legacy_set_secret_scope, resets)
    inspected = _count_signature_calls(monkeypatch, legacy_set_secret_scope)

    with _enter(which):
        # Installed exactly once, positionally (a keyword would raise TypeError,
        # which the context manager swallows, leaving ``calls`` empty).
        assert len(calls) == 1
    with _enter(which):
        assert len(calls) == 2
    assert resets == ["legacy-token-1", "legacy-token-2"]
    assert all(c["HERMES_HOME"] == str(profiles.get_hermes_home_for_profile(PROFILE)) for c in calls)
    # The capability decision is cached per Agent module: inspected once, not per turn.
    assert len(inspected) == 1


def test_uninspectable_set_secret_scope_falls_back_to_the_unstamped_install(
    monkeypatch, tmp_path
):
    _setup_profile(monkeypatch, tmp_path)
    calls, resets = [], []

    class Uninspectable:
        @property
        def __signature__(self):
            raise ValueError("no signature")

        def __call__(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "token"

    _install_fake_secret_scope(monkeypatch, Uninspectable(), resets)
    with _enter("background_worker"):
        pass
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs == {} and len(args) == 1
    assert resets == ["token"]


def test_kwargs_wrapper_around_a_legacy_setter_still_installs_the_scope(
    monkeypatch, tmp_path
):
    """A bare ``**kwargs`` wrapper is not proof the setter takes ``profile_home``:
    stamping through it would raise TypeError in the legacy setter and leave the
    turn with no scope. It must get today's unstamped install instead."""
    _setup_profile(monkeypatch, tmp_path)
    calls, resets = [], []

    def legacy_set_secret_scope(secrets):
        calls.append(dict(secrets))
        return "legacy-token"

    def wrapper(*args, **kwargs):  # no functools.wraps: the signature is (*args, **kwargs)
        return legacy_set_secret_scope(*args, **kwargs)

    _install_fake_secret_scope(monkeypatch, wrapper, resets)
    with _enter("background_worker"):
        pass
    assert len(calls) == 1
    assert resets == ["legacy-token"]


# 3/4 ── the REAL installed Hermes Agent secret-scope module ──────────────────

def _real_agent_modules():
    try:
        secret_scope = importlib.import_module("agent.secret_scope")
        hermes_constants = importlib.import_module("hermes_constants")
    except Exception as exc:  # pragma: no cover - depends on the local Agent install
        pytest.skip(f"Hermes Agent secret scope unavailable: {exc}")
    if not getattr(secret_scope, "__file__", None) or not hasattr(secret_scope, "_bound_secrets"):
        pytest.skip("agent.secret_scope is not the real Hermes Agent module")
    try:
        params = inspect.signature(secret_scope.set_secret_scope).parameters
    except (TypeError, ValueError):
        pytest.skip("installed set_secret_scope is not inspectable")
    if "profile_home" not in params:
        pytest.skip("installed Hermes Agent predates the set_secret_scope profile_home stamp")
    for name in ("hermes_home_key", "get_routing_process_hermes_home"):
        if not hasattr(hermes_constants, name):
            pytest.skip(f"installed hermes_constants lacks {name}")
    return secret_scope, hermes_constants


@contextlib.contextmanager
def _pinned_process_home(hermes_constants, base: Path):
    """Pin the launch home the way WebUI startup does (``_pin_process_profile_home``)."""
    pin = getattr(hermes_constants, "pin_process_hermes_home", None)
    is_pinned = getattr(hermes_constants, "process_hermes_home_is_pinned", None)
    if not callable(pin) or not callable(is_pinned):
        yield
        return
    previous = str(hermes_constants.get_routing_process_hermes_home()) if is_pinned() else None
    pin(str(base))
    try:
        yield
    finally:
        pin(previous)


@pytest.mark.parametrize("which", CONTEXT_MANAGERS)
def test_real_agent_scope_records_the_profile_home(monkeypatch, tmp_path, which):
    secret_scope, hermes_constants = _real_agent_modules()
    base, home = _setup_profile(monkeypatch, tmp_path)

    with _pinned_process_home(hermes_constants, base):
        with _enter(which):
            bound = secret_scope._bound_secrets()
            assert bound is not None, "no secret scope installed"
            assert bound.mapping["HERMES_HOME"] == str(home)
            assert bound.profile_home is not None, "secret scope installed unstamped"
            assert hermes_constants.hermes_home_key(bound.profile_home) == hermes_constants.hermes_home_key(home)
        assert secret_scope._bound_secrets() is None


@pytest.mark.parametrize("which", CONTEXT_MANAGERS)
def test_scope_owner_matches_the_profile_home_under_the_refresh_owner_rule(
    monkeypatch, tmp_path, which
):
    """Emulates e4888ba0's ``refresh_installed_secret_scope`` owner check with the
    installed Agent: an unstamped scope resolves to the launch home and the
    plugin-secret refresh for the turn's profile would be refused."""
    secret_scope, hermes_constants = _real_agent_modules()
    base, home = _setup_profile(monkeypatch, tmp_path)
    key = hermes_constants.hermes_home_key

    with _pinned_process_home(hermes_constants, base):
        with _enter(which):
            # Meaningful only while the launch home differs from the turn's profile home.
            assert key(hermes_constants.get_routing_process_hermes_home()) != key(home)
            bound = secret_scope._bound_secrets()
            assert bound is not None, "no secret scope installed"
            owner = bound.profile_home or hermes_constants.get_routing_process_hermes_home()
            assert key(owner) == key(home), (
                f"{which}: scope owner {owner!s} is not the profile home {home!s}; "
                "the Agent would skip the plugin secret refresh for this turn"
            )
