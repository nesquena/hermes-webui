"""#7905 gate: the Gateway model field parses with the SESSION profile's own config.

Review 2026-11-16T01:03:48Z: ``_gateway_profile_config`` called
``get_config_for_profile_home(home)`` without ``isolate_config_override=True``, so
``HERMES_CONFIG_PATH`` was resolved through the *active* profile instead of the
explicitly requested session profile. With a named profile sticky-active and an
external root override, a ``custom:omni`` declared only in the session's own file
was missed (the Gateway got ``omni:latest`` instead of ``latest``), while the root
cases read root's own file and lost the external override (``latest`` instead of
``omni:latest``).

These cases drive the real loader: disposable homes on disk, a real
``HERMES_CONFIG_PATH``, and the real profile resolver — no monkeypatched ``cfg``.
"""

import pytest

from tests.test_session_reasoning_effort import (  # noqa: F401
    isolated_reasoning_profiles,
)

from api import config, profiles

_OMNI_PROVIDER = (
    "providers:\n"
    "  custom:omni:\n"
    "    base_url: https://omni.example/v1\n"
    "    models: [latest]\n"
)
_GENERIC_ONLY = (
    "providers:\n"
    "  custom:\n"
    "    base_url: https://generic.example/v1\n"
)


def _disposable_homes(tmp_path, monkeypatch, *, active_profile):
    """Root and ``work`` both declare ``custom:omni``; the override has neither."""
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / "config.yaml").write_text(_OMNI_PROVIDER, encoding="utf-8")
    (work / "config.yaml").write_text(_OMNI_PROVIDER, encoding="utf-8")
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(_GENERIC_ONLY, encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", active_profile)
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    return root, work, external


def _field(profile):
    from api.gateway_chat import _gateway_model_field

    return _gateway_model_field(
        "@custom:omni:latest", session_provider="custom", profile=profile
    )


def test_named_session_profile_keeps_its_own_identity_over_root_override(
    tmp_path, monkeypatch
):
    """``work`` owns ``custom:omni``; the external root override must not hide it."""
    _root, work, external = _disposable_homes(
        tmp_path, monkeypatch, active_profile="work"
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    assert profiles.get_active_hermes_home() == work
    assert _field("work") == "latest"


@pytest.mark.parametrize("profile", [None, "", "default"], ids=["none", "empty", "alias"])
def test_root_session_profile_uses_the_external_override(tmp_path, monkeypatch, profile):
    """Root (and its aliases) keep reading the external override, which is generic."""
    _root, _work, external = _disposable_homes(
        tmp_path, monkeypatch, active_profile="work"
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    assert _field(profile) == "omni:latest"


@pytest.mark.parametrize("profile", ["work", None, "", "default"], ids=["work", "none", "empty", "alias"])
def test_without_an_override_every_profile_reads_its_own_file(
    tmp_path, monkeypatch, profile
):
    """Control: no ``HERMES_CONFIG_PATH`` — root and named both own ``custom:omni``."""
    _root, _work, _external = _disposable_homes(
        tmp_path, monkeypatch, active_profile="work"
    )
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    config.reload_config()

    assert _field(profile) == "latest"


def test_named_profile_without_the_identity_still_peels(tmp_path, monkeypatch):
    """Control: a slug the session profile does not declare stays in the generic lane."""
    _root, _work, external = _disposable_homes(
        tmp_path, monkeypatch, active_profile="work"
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    from api.gateway_chat import _gateway_model_field

    assert (
        _gateway_model_field(
            "@custom:qwen3-30b-a3b:latest", session_provider="custom", profile="work"
        )
        == "qwen3-30b-a3b:latest"
    )
