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

The agent-side module is stubbed via ``sys.modules`` injection (house pattern),
so these tests need neither a real hermes-agent install nor a real secret
manager.
"""

import os
import sys
import types

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
