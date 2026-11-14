"""Regression coverage for the #7170 round-9 review finding.

greptile 2026-10-17 P1 — "Shared Gateway settings disappear":

    If the default profile's ``.env`` supplies the shared Gateway URL and key,
    named profiles with no Gateway settings now lose both. The new capture
    helpers remove every key in ``_loaded_profile_env_keys``, then read only the
    session profile's settings. Normal chat and regeneration therefore use
    ``http://127.0.0.1:8642`` without a key instead of the shared Gateway.

The dispatch-side capture must distinguish two shapes that look alike once the
loaded profile env keys are stripped:

* the session profile OVERRIDES the endpoint (its own URL and/or key) — keep
  today's behaviour: its own key, the launcher key when the profile only
  overrides the URL, and an explicitly empty key for an anonymous profile;
* the session profile configures NOTHING — it inherits the deployment-level
  pair (launcher environment / the default profile's ``.env`` / ambient
  config), exactly what a single-profile deployment sends and what master's
  worker-time ``_gateway_api_key()`` / ``_gateway_base_url()`` reads.
"""

import types

import pytest

import api.gateway_chat as gateway_chat
import api.models as models
from api.config import STREAMS


@pytest.fixture(autouse=True)
def _clear_global_stream_registries():
    STREAMS.clear()
    yield
    STREAMS.clear()


def _profile_home(tmp_path, name: str, *, env_lines: list[str] | None = None):
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    if env_lines:
        home.joinpath(".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    return home


def _session_for_profile(profile: str = "b"):
    return types.SimpleNamespace(profile=profile)


def _root_shared_pair(monkeypatch, home_kind: str):
    """Model the documented multi-profile deployment.

    The DEFAULT (root) profile's ``.env`` supplies the shared Gateway pair and
    was loaded into the process environment at startup, so every one of its
    keys is marked in ``_loaded_profile_env_keys``. A named profile that
    configures no Gateway settings must still inherit that pair.
    """
    import api.profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod,
        "_loaded_profile_env_keys",
        {"HERMES_WEBUI_GATEWAY_API_KEY", "HERMES_WEBUI_GATEWAY_BASE_URL"},
    )
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "shared-key")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://shared-gw.invalid:8642")
    # The mirrored env belongs to the ROOT profile → this is the deployment pair,
    # never a sibling profile's credential.
    monkeypatch.setattr(gateway_chat, "_process_profile_is_root", lambda: True)
    return home_kind


def test_session_api_key_inherits_shared_pair_for_unconfigured_profile(tmp_path, monkeypatch):
    """RED: profile B has no .env and no config.yaml gateway settings."""
    profile_b_home = _profile_home(tmp_path, "b")
    _root_shared_pair(monkeypatch, "root")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    captured = gateway_chat._gateway_session_api_key(_session_for_profile("b"))
    assert captured == "shared-key", (
        f"_gateway_session_api_key returned {captured!r}; a named profile with NO "
        "Gateway settings must inherit the deployment-level shared key (greptile "
        "2026-10-17 P1). The stripped loaded-profile key is the ROOT profile's, "
        "i.e. the shared one — not a sibling credential."
    )


def test_session_base_url_inherits_shared_pair_for_unconfigured_profile(tmp_path, monkeypatch):
    """RED: the URL half must inherit the shared URL too (not 127.0.0.1:8642)."""
    profile_b_home = _profile_home(tmp_path, "b")
    _root_shared_pair(monkeypatch, "root")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    captured = gateway_chat._gateway_session_base_url(_session_for_profile("b"))
    assert captured == "http://shared-gw.invalid:8642", (
        f"_gateway_session_base_url returned {captured!r}; expected the shared "
        "deployment URL for a profile that configures no Gateway endpoint."
    )


def test_unconfigured_profile_still_gets_anonymous_key_for_sibling_ambient_value(
    tmp_path, monkeypatch
):
    """Safety invariant: a sibling NAMED profile's mirrored key is never 'shared'.

    When the process-active profile is NOT the root profile, the keys it
    mirrored into ``os.environ`` belong to that named profile. An unconfigured
    session profile must NOT borrow them — it fails closed on the key while
    still using the ambient deployment URL for the transport.
    """
    profile_b_home = _profile_home(tmp_path, "b")
    import api.profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod,
        "_loaded_profile_env_keys",
        {"HERMES_WEBUI_GATEWAY_API_KEY"},
    )
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "KEY_FOR_PROFILE_A")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://shared-gw.invalid:8642")
    monkeypatch.setattr(gateway_chat, "_process_profile_is_root", lambda: False)
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    captured = gateway_chat._gateway_session_api_key(_session_for_profile("b"))
    assert captured == "", (
        f"_gateway_session_api_key returned {captured!r}; profile A's mirrored key "
        "must never be sent on profile B's behalf."
    )


def test_profile_with_own_url_keeps_launcher_key(tmp_path, monkeypatch):
    """No regression of the round-2 BRICK fix: a profile that overrides the URL
    but has no key of its own keeps the launcher/deployment key (F1 contract)."""
    profile_b_home = _profile_home(
        tmp_path, "b", env_lines=['HERMES_WEBUI_GATEWAY_BASE_URL="http://profile-b.invalid:8642"']
    )
    import api.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_loaded_profile_env_keys", set())
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "launcher-key")
    monkeypatch.setattr(models, "_get_profile_home", lambda profile: profile_b_home)

    s = _session_for_profile("b")
    assert gateway_chat._gateway_session_api_key(s) == "launcher-key"
    assert gateway_chat._gateway_session_base_url(s) == "http://profile-b.invalid:8642"
