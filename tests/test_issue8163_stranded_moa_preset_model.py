"""Regression tests for #8163 — a session stranded on a MoA preset name.

A chat created while the virtual ``moa`` provider was the global default stores
the MoA **preset name** as its model (``{"model": "default", "model_provider":
"moa"}``).  After the global default moved to a real model the pair was left as
``{"model": "default", "model_provider": "nous"}`` and every ``/api/chat/start``
sent ``model="default"`` upstream, which answered::

    HTTP 404: Model 'default' not found.

``_resolve_compatible_session_model_state`` fast-paths any pair where both
fields are set and the model is not ``@provider:``-qualified, so the stranded
preset name was returned verbatim and the turn 404'd forever.

The discriminator is deliberately *structural*, not the literal string
``"default"``: a model is only treated as stranded when it is an actually
configured MoA preset name (from the session's own profile config) paired with
a **non**-moa provider.  A provider that legitimately serves a model called
``default`` is never remapped, and neither is an explicit fresh user pick.
"""
import pytest

MOA_PRESET_NAME = "default"
STRANDED_PROVIDER = "nous"


def _moa_profile_config(preset_names=(MOA_PRESET_NAME,), *, enabled=True):
    return {
        "moa": {
            "enabled": enabled,
            "presets": {name: {"enabled": True} for name in preset_names},
        }
    }


def _no_moa_config():
    return {"model": {"provider": "nous", "default": "hermes-4-405b"}}


@pytest.fixture
def routes():
    import api.routes as routes_mod

    return routes_mod


def test_stranded_preset_name_repairs_to_profile_default(routes):
    """(preset, non-moa provider) must repair to the profile default."""
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        MOA_PRESET_NAME,
        STRANDED_PROVIDER,
        profile_provider=STRANDED_PROVIDER,
        profile_default_model="hermes-4-405b",
        profile_config=_moa_profile_config(),
    )

    assert changed is True, (
        "A MoA preset name paired with a non-moa provider can never resolve; "
        "the resolver must report the repair."
    )
    assert effective == "hermes-4-405b"
    assert provider == STRANDED_PROVIDER


def test_stranded_preset_name_falls_back_to_catalog_default(routes, monkeypatch):
    """Without a profile default the catalog default supplies the repair."""
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {"active_provider": "nous", "default_model": "catalog-default"},
    )

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        MOA_PRESET_NAME,
        STRANDED_PROVIDER,
        profile_config=_moa_profile_config(),
    )

    assert changed is True
    assert effective == "catalog-default"
    assert provider == STRANDED_PROVIDER


def test_model_named_default_that_is_not_a_moa_preset_is_preserved(routes):
    """The literal name alone must never trigger a remap (#8163 review gate)."""
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        MOA_PRESET_NAME,
        STRANDED_PROVIDER,
        profile_provider=STRANDED_PROVIDER,
        profile_default_model="hermes-4-405b",
        profile_config=_no_moa_config(),
    )

    assert (effective, provider, changed) == (MOA_PRESET_NAME, STRANDED_PROVIDER, False)


def test_explicit_pick_is_never_second_guessed(routes):
    """A fresh explicit pick is honoured even if it looks like a preset name."""
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        MOA_PRESET_NAME,
        STRANDED_PROVIDER,
        profile_provider=STRANDED_PROVIDER,
        profile_default_model="hermes-4-405b",
        profile_config=_moa_profile_config(),
        explicit_model_pick=True,
    )

    assert (effective, provider, changed) == (MOA_PRESET_NAME, STRANDED_PROVIDER, False)


def test_moa_provider_preset_is_left_on_the_moa_fast_path(routes):
    """The legitimate (preset, moa) pair keeps its existing fast-path result."""
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        MOA_PRESET_NAME,
        "moa",
        profile_config=_moa_profile_config(),
    )

    assert (effective, provider, changed) == (MOA_PRESET_NAME, "moa", False)


def test_disabled_moa_config_does_not_treat_name_as_preset(routes):
    """MoA turned off: the name is no proof, so the pair is left alone."""
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        MOA_PRESET_NAME,
        STRANDED_PROVIDER,
        profile_provider=STRANDED_PROVIDER,
        profile_default_model="hermes-4-405b",
        profile_config=_moa_profile_config(enabled=False),
    )

    assert (effective, provider, changed) == (MOA_PRESET_NAME, STRANDED_PROVIDER, False)


def test_ordinary_model_still_skips_the_catalog(routes, monkeypatch):
    """The #1855 fast path must survive: a normal model never hits the catalog."""
    calls = []

    def _boom(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("catalog must not be consulted for a normal model")

    monkeypatch.setattr(routes, "get_available_models", _boom)

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "gpt-5.5",
        STRANDED_PROVIDER,
        profile_provider=STRANDED_PROVIDER,
        profile_default_model="hermes-4-405b",
        profile_config=_moa_profile_config(),
    )

    assert (effective, provider, changed) == ("gpt-5.5", STRANDED_PROVIDER, False)
    assert calls == []


def test_provider_qualified_preset_name_is_not_stranded(routes):
    """An ``@provider:model`` string carries its own context — leave it alone."""
    qualified = f"@{STRANDED_PROVIDER}:{MOA_PRESET_NAME}"

    effective, provider, changed = routes._resolve_compatible_session_model_state(
        qualified,
        STRANDED_PROVIDER,
        profile_provider=STRANDED_PROVIDER,
        profile_default_model="hermes-4-405b",
        profile_config=_moa_profile_config(),
    )

    assert (effective, provider, changed) == (qualified, STRANDED_PROVIDER, False)