"""Regression tests for #1881 — phantom duplicate Custom group.

Reported scenario: ``provider: ai-gateway`` with a ``custom_providers`` entry
in ``config.yaml``. The ``/api/models`` endpoint returned the ai-gateway's
auto-detected models a second time under a bare "Custom" group with mismatched
provider prefixes, and ``custom:*`` named groups could shadow the active
provider's catalog.

The reporter's analysis suggested three fixes; on closer inspection only two
of them are needed because the symptom (duplicate group in the model picker)
lives entirely in ``get_available_models()``'s group-construction logic. The
third proposed fix (gating ``resolve_model_provider``'s custom-provider
routing on ``config_provider``) was rejected because it conflicts with the
pre-existing model-specific-override behaviour exercised by
``test_model_resolver.py::test_custom_provider_model_with_slash_routes_to_named_custom_provider``
and ``..._models_dict_routes_...`` — those tests assert that an explicit
``custom_providers`` entry wins routing even when the active provider is
``openrouter``/``xiaomi``. That intentional override is orthogonal to the
duplicate-group symptom.

The two applied fixes:

1. ``get_available_models()`` — ``custom:*`` provider IDs whose slug was NOT
   in ``_named_custom_groups`` fell through to the auto-detected-models
   fallback below, copying the active provider's models into a phantom
   Custom group. Fix: ``continue`` unconditionally for any ``custom:*`` PID.

2. ``get_available_models()`` — the bare ``"custom"`` PID, with the active
   provider being non-custom (``ai-gateway``), was hitting the
   ``elif auto_detected_models:`` branch and producing a duplicate Custom
   group. Fix: when ``pid == "custom"`` and the active provider is concrete,
   leave ``models_for_group`` empty so no phantom group is appended.
"""

from __future__ import annotations

import sys
import types

import pytest

import api.config as config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_models_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


def _with_ai_gateway_and_custom_provider():
    """provider=ai-gateway + a custom_providers entry that names a model the
    gateway also exposes."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "some-model",
                "provider": "ai-gateway",
                "base_url": "https://gateway.example.com/v1",
            },
            "custom_providers": [
                {
                    "name": "my-custom",
                    "base_url": "https://api.example.com/v1",
                    "api_key": "sk-xxx",
                    "models": {"some-model": {}},
                }
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    def restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()

    return restore


def _stub_provider_modules(monkeypatch, detected_provider_ids: list[dict]):
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: detected_provider_ids
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {"key_source": "config_yaml"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(
        config, "_get_auth_store_path", lambda: config.Path("/tmp/does-not-exist-auth.json")
    )


# ---------------------------------------------------------------------------
# Fix #1 — bare "custom" PID must not absorb auto_detected_models when the
# active provider is concrete (ai-gateway etc.)
# ---------------------------------------------------------------------------

def test_no_phantom_custom_group_when_active_provider_is_ai_gateway(monkeypatch):
    """The bare "custom" PID must not duplicate ai-gateway models (#1881)."""
    # ai-gateway is the active provider; "custom" appears as a sibling
    # detected provider (via auth store quirk in real-world setups). The
    # global auto_detected_models list (populated by ai-gateway's catalog
    # fetch) MUST NOT be copied into the bare "custom" group.
    _stub_provider_modules(
        monkeypatch,
        [
            {"id": "ai-gateway", "authenticated": True},
            {"id": "custom", "authenticated": True},
        ],
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _with_ai_gateway_and_custom_provider()
    try:
        result = config.get_available_models()
    finally:
        restore()

    groups_by_id = {g["provider_id"]: g for g in result["groups"]}

    # Either the bare-custom group is dropped entirely, or it exists with
    # no models — what MUST NOT happen is duplication of ai-gateway models.
    if "custom" in groups_by_id:
        assert groups_by_id["custom"]["models"] == [], (
            "bare 'Custom' group should be empty when active provider is "
            f"ai-gateway, got {len(groups_by_id['custom']['models'])} phantom models"
        )


# ---------------------------------------------------------------------------
# Fix #2 — unnamed custom:* PIDs must not fall through to auto_detected
# ---------------------------------------------------------------------------

def test_unnamed_custom_provider_id_does_not_inherit_auto_detected(monkeypatch):
    """A custom:* PID NOT in _named_custom_groups must skip cleanly (#1881).

    Before the fix, such a PID fell through to the auto_detected_models
    fallback and got every active-provider model copied into a phantom
    "Custom: <unknown>" group.
    """
    # Stub a stale custom:* provider id (e.g. left over from a previous
    # config) that doesn't match any current custom_providers entry.
    _stub_provider_modules(
        monkeypatch,
        [
            {"id": "ai-gateway", "authenticated": True},
            {"id": "custom:stale-config", "authenticated": True},
        ],
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _with_ai_gateway_and_custom_provider()
    try:
        result = config.get_available_models()
    finally:
        restore()

    groups_by_id = {g["provider_id"]: g for g in result["groups"]}

    # The stale custom:* PID must NOT appear with auto-detected models.
    # It either appears empty or is dropped — no phantom duplication.
    if "custom:stale-config" in groups_by_id:
        assert groups_by_id["custom:stale-config"]["models"] == [], (
            "stale custom:* PID with no _named_custom_groups entry must not "
            "absorb auto_detected_models — got "
            f"{len(groups_by_id['custom:stale-config']['models'])} phantom models"
        )


# ---------------------------------------------------------------------------
# Invariant — fixes #1 + #2 together preserve named custom groups when the
# active provider IS the named custom slug
# ---------------------------------------------------------------------------

def test_named_custom_group_still_populates_when_active_is_custom_alias(monkeypatch):
    """Named custom_providers groups still appear when the active provider IS
    the named custom slug — preserves test_issue1806 invariants."""
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {"id": "custom:my-custom", "authenticated": True},
    ]
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {"key_source": "config_yaml"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(
        config, "_get_auth_store_path", lambda: config.Path("/tmp/does-not-exist-auth.json")
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "some-model",
                "provider": "my-custom",  # active = the named custom provider
                "base_url": "https://api.example.com/v1",
            },
            "custom_providers": [
                {
                    "name": "my-custom",
                    "base_url": "https://api.example.com/v1",
                    "api_key": "sk-xxx",
                    "models": {"some-model": {}},
                }
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    try:
        result = config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime

    groups_by_id = {g["provider_id"]: g for g in result["groups"]}
    assert "custom:my-custom" in groups_by_id
    model_ids = [m["id"] for m in groups_by_id["custom:my-custom"]["models"]]
    assert "some-model" in model_ids
