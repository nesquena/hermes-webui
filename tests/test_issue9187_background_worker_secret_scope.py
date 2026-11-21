"""#9187: a background worker's body must not resolve another profile's credential.

``profile_env_for_background_worker`` mirrors the session profile's env into
``os.environ`` for the worker body (agent readers still call ``os.getenv()``)
and scrubs the credential names it knows out of that mirror through
``_apply_profile_env_to_process``. The scrub set was the CURRENT profile's
``secret_env_names``: the provider-registry names, the agent-registry names and
the credential env names that profile's own ``config.yaml`` declares. A name only
ANOTHER profile's ``config.yaml`` declares — a custom provider's ``key_env`` —
is in neither set, so a concurrent worker's mirror left it in ``os.environ`` and
this profile's credential reads resolved it as its own (#9187: work for one
profile authorised with another profile's key).

WebUI runs the Agent with multiplexing off, so ``agent.secret_scope.get_secret``
falls through to ``os.environ`` on a scope miss; the scrub therefore has to cover
the credential env names every profile on the server declares, not only the body
profile's. The secret SCOPE itself stays per-profile — built from the profile's
own runtime env, as #9188 established — and the two scope paths
(``profile_env_for_background_worker`` and
``profile_env_for_active_request_readonly``) must keep installing the same
mapping for the same profile.

Agent-free: ``agent.secret_scope`` is a stand-in that records what each path
installs (the ``tests/test_issue9084_secret_scope_profile_stamp.py`` pattern);
``DUMMY``-free synthetic homes under ``tmp_path`` only.
"""

from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from api import profiles


ALPHA = "alpha"
BETA = "beta"
SECRET_A = "secret-A"
SECRET_B = "secret-B"
OPERATOR_TOKEN = "operator-token-value"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _setup_server(monkeypatch, tmp_path) -> Path:
    """A synthetic server home: root + ``alpha`` + ``beta``, each with its own credentials.

    ``alpha`` declares a custom provider whose credential env name (``ALPHA_CUSTOM_KEY``)
    only ITS ``config.yaml`` mentions; ``beta`` declares ``BETA_CUSTOM_KEY`` the same way.
    """
    base = tmp_path / ".hermes"
    alpha = base / "profiles" / ALPHA
    beta = base / "profiles" / BETA
    _write(alpha / ".env", f"ALPHA_CUSTOM_KEY={SECRET_A}\nALPHA_OWN_KEY=alpha-own\n")
    _write(
        alpha / "config.yaml",
        "model:\n  default: dummy-model\n"
        "custom_providers:\n"
        "  - name: alpha-provider\n"
        "    key_env: ALPHA_CUSTOM_KEY\n",
    )
    _write(beta / ".env", f"BETA_CUSTOM_KEY={SECRET_B}\nBETA_OWN_KEY=beta-own\n")
    _write(
        beta / "config.yaml",
        "model:\n  default: dummy-model\n"
        "custom_providers:\n"
        "  - name: beta-provider\n"
        "    key_env: BETA_CUSTOM_KEY\n",
    )
    _write(base / ".env", "")
    _write(base / "config.yaml", "model:\n  default: dummy-model\n")
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("", "default"))
    monkeypatch.setattr(profiles, "_secret_scope_available", None)
    monkeypatch.setattr(profiles, "_hermes_home_override_available", None)
    # operator_env() reads the launch snapshot and the launch-home .env recorded at
    # import; pin both so the test never depends on the host interpreter's env.
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", "")
    from api import paths as _paths

    monkeypatch.setattr(_paths, "STARTUP_ENV", {"OPERATOR_TOKEN": OPERATOR_TOKEN})
    return base


def _install_fake_secret_scope(monkeypatch, calls) -> None:
    """Model the Agent's ``agent.secret_scope`` with a recording setter (agent-free)."""
    agent_pkg: Any = types.ModuleType("agent")
    agent_pkg.__path__ = []
    module: Any = types.ModuleType("agent.secret_scope")

    def set_secret_scope(secrets, *, profile_home=None):
        calls.append((dict(secrets), profile_home))
        return "scope-token"

    module.set_secret_scope = set_secret_scope
    module.reset_secret_scope = lambda token: calls.append(("reset", token))
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)


@contextlib.contextmanager
def _enter_background_worker(profile: str):
    with profiles.profile_env_for_background_worker(
        profile, "issue 9187", scope_skill_modules=False
    ):
        yield


@contextlib.contextmanager
def _enter_active_request_readonly(profile: str):
    profiles.set_request_profile(profile)
    try:
        with profiles.profile_env_for_active_request_readonly("issue 9187"):
            yield
    finally:
        profiles.clear_request_profile()


ENTER = {
    "background_worker": _enter_background_worker,
    "active_request_readonly": _enter_active_request_readonly,
}


# 1 ── the worker body must not resolve another profile's declared credential ──


def test_worker_body_drops_another_profiles_declared_credential(monkeypatch, tmp_path):
    """A name only alpha's config.yaml declares must not stay readable in beta's worker body."""
    _setup_server(monkeypatch, tmp_path)
    # alpha's worker (or a Settings save) mirrored its .env into the process env.
    monkeypatch.setenv("ALPHA_CUSTOM_KEY", SECRET_A)
    monkeypatch.setenv("ALPHA_OWN_KEY", "alpha-own")

    import os

    with _enter_background_worker(BETA):
        assert os.environ.get("ALPHA_CUSTOM_KEY") is None, (
            "beta's background-worker body can still read alpha's credential from the "
            f"process env: {os.environ.get('ALPHA_CUSTOM_KEY')!r}"
        )


def test_worker_body_drops_the_root_profiles_declared_credential(monkeypatch, tmp_path):
    """The same scrub covers the root profile's declared credential names."""
    base = _setup_server(monkeypatch, tmp_path)
    _write(
        base / "config.yaml",
        "model:\n  default: dummy-model\n"
        "custom_providers:\n"
        "  - name: root-provider\n"
        "    key_env: ROOT_CUSTOM_KEY\n",
    )
    _write(base / ".env", "ROOT_CUSTOM_KEY=root-secret\n")
    monkeypatch.setenv("ROOT_CUSTOM_KEY", "root-secret")

    import os

    with _enter_background_worker(BETA):
        assert os.environ.get("ROOT_CUSTOM_KEY") is None


def test_worker_body_keeps_the_profiles_own_credentials(monkeypatch, tmp_path):
    """The scrub must not strip the body profile's own declared credential."""
    _setup_server(monkeypatch, tmp_path)

    import os

    with _enter_background_worker(BETA):
        assert os.environ.get("BETA_CUSTOM_KEY") == SECRET_B
        assert os.environ.get("BETA_OWN_KEY") == "beta-own"


def test_worker_body_keeps_operator_env_values(monkeypatch, tmp_path):
    """A deployment value no profile declares is not a credential and survives the mirror."""
    _setup_server(monkeypatch, tmp_path)
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)

    import os

    with _enter_background_worker(BETA):
        assert os.environ.get("OPERATOR_TOKEN") == OPERATOR_TOKEN


def test_worker_exit_restores_the_other_profiles_value(monkeypatch, tmp_path):
    """The wider scrub is a body-scoped blindfold: the value returns on exit."""
    _setup_server(monkeypatch, tmp_path)
    monkeypatch.setenv("ALPHA_CUSTOM_KEY", SECRET_A)

    import os

    with _enter_background_worker(BETA):
        assert os.environ.get("ALPHA_CUSTOM_KEY") is None
    assert os.environ.get("ALPHA_CUSTOM_KEY") == SECRET_A


# 2 ── the secret scope itself stays the body profile's own, on both paths ──────


@pytest.mark.parametrize("which", ["background_worker", "active_request_readonly"])
def test_secret_scope_holds_only_the_profiles_own_values(monkeypatch, tmp_path, which):
    _setup_server(monkeypatch, tmp_path)
    # Both another profile's credential and an operator value sit in the process env.
    monkeypatch.setenv("ALPHA_CUSTOM_KEY", SECRET_A)
    monkeypatch.setenv("OPERATOR_TOKEN", OPERATOR_TOKEN)
    calls: list = []
    _install_fake_secret_scope(monkeypatch, calls)

    with ENTER[which](BETA):
        assert len(calls) == 1, f"{which} installed no secret scope"
        secrets, stamped = calls[0]
        assert SECRET_A not in secrets.values(), (
            f"{which} put alpha's credential in beta's secret scope: {secrets}"
        )
        assert secrets.get("BETA_CUSTOM_KEY") == SECRET_B
        assert secrets.get("HERMES_HOME") == stamped
        assert Path(stamped).name == BETA


def test_both_scope_paths_install_the_same_secret_scope(monkeypatch, tmp_path):
    """The background-worker path and the streaming path must agree for one profile."""
    _setup_server(monkeypatch, tmp_path)
    monkeypatch.setenv("ALPHA_CUSTOM_KEY", SECRET_A)
    installed = {}
    for which in ("background_worker", "active_request_readonly"):
        calls: list = []
        _install_fake_secret_scope(monkeypatch, calls)
        with ENTER[which](BETA):
            installed[which] = calls[0][0]
    assert installed["background_worker"] == installed["active_request_readonly"], (
        "the two scope paths disagree for the same profile: "
        f"{installed['background_worker']} vs {installed['active_request_readonly']}"
    )
