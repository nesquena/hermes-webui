"""Regression tests for #2245 — mixed-case custom provider keys lose picker models.

When a user configures a provider key in ``config.yaml`` with mixed case
(e.g. ``CLIPpoxy``) or underscores (e.g. ``snake_case_provider``), the
WebUI model picker must still surface that provider's configured models.

Root cause: ``_build_available_models_uncached()`` iterates over
*canonicalised* provider IDs (lowercase, hyphens) but looked up
``cfg["providers"]`` using the canonical key — which doesn't match the
raw mixed-case/underscore key in the config dict.  The fix adds a
``_canonical_to_raw_provider_key`` map so the generic-provider branch
can resolve the original key and load ``provider_cfg`` correctly.
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
    """Invalidate the models TTL cache before and after every test."""
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


def _stub_hermes_cli(monkeypatch):
    """Stub hermes_cli so no real CLI/agent calls happen."""
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda _pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {"key_source": "none"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(
        config,
        "_get_auth_store_path",
        lambda: config.Path("/tmp/does-not-exist-auth.json"),
    )


def _with_config(cfg_dict: dict):
    """Replace ``config.cfg`` with *cfg_dict* and return a restore callable."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg.update(cfg_dict)
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


# ---------------------------------------------------------------------------
# Test case 1 — mixed-case provider key
# ---------------------------------------------------------------------------

def test_mixed_case_provider_key_produces_configured_models(monkeypatch):
    """A provider key like ``CLIPpoxy`` must feed its configured models into
    the picker after canonicalisation to ``clippoxy``."""
    _stub_hermes_cli(monkeypatch)
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _with_config(
        {
            "model": {
                "default": "my-model",
                "provider": "CLIPpoxy",
            },
            "providers": {
                "CLIPpoxy": {
                    "models": ["my-model", "another-model"],
                },
            },
        }
    )
    try:
        result = config.get_available_models()
    finally:
        restore()

    # Find the group for the canonicalised provider id "clippoxy"
    groups_by_pid = {g["provider_id"]: g for g in result["groups"]}
    assert "clippoxy" in groups_by_pid, (
        f"Expected canonical provider id 'clippoxy' in groups, "
        f"got: {list(groups_by_pid.keys())}"
    )
    group = groups_by_pid["clippoxy"]
    model_ids = [m["id"] for m in group["models"]]
    # Both configured models must appear (possibly with @clippoxy: prefix)
    # Strip any @provider: prefix for comparison
    bare_ids = []
    for mid in model_ids:
        if mid.startswith("@"):
            bare_ids.append(mid.split(":", 1)[-1] if ":" in mid else mid)
        else:
            bare_ids.append(mid)
    assert "my-model" in bare_ids, (
        f"Expected 'my-model' in model ids for clippoxy, got: {bare_ids}"
    )
    assert "another-model" in bare_ids, (
        f"Expected 'another-model' in model ids for clippoxy, got: {bare_ids}"
    )


# ---------------------------------------------------------------------------
# Test case 2 — underscore provider key
# ---------------------------------------------------------------------------

def test_underscore_provider_key_produces_configured_models(monkeypatch):
    """A provider key like ``snake_case_provider`` must canonicalise to
    ``snake-case-provider`` and still surface its configured models."""
    _stub_hermes_cli(monkeypatch)
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _with_config(
        {
            "model": {
                "default": "model-a",
                "provider": "snake_case_provider",
            },
            "providers": {
                "snake_case_provider": {
                    "models": ["model-a", "model-b"],
                },
            },
        }
    )
    try:
        result = config.get_available_models()
    finally:
        restore()

    groups_by_pid = {g["provider_id"]: g for g in result["groups"]}
    canonical_pid = "snake-case-provider"
    assert canonical_pid in groups_by_pid, (
        f"Expected canonical provider id '{canonical_pid}' in groups, "
        f"got: {list(groups_by_pid.keys())}"
    )
    group = groups_by_pid[canonical_pid]
    model_ids = [m["id"] for m in group["models"]]
    bare_ids = []
    for mid in model_ids:
        if mid.startswith("@"):
            bare_ids.append(mid.split(":", 1)[-1] if ":" in mid else mid)
        else:
            bare_ids.append(mid)
    assert "model-a" in bare_ids, (
        f"Expected 'model-a' in model ids for {canonical_pid}, got: {bare_ids}"
    )
    assert "model-b" in bare_ids, (
        f"Expected 'model-b' in model ids for {canonical_pid}, got: {bare_ids}"
    )


# ---------------------------------------------------------------------------
# Test case 3 — built-in provider with lowercase key still works
# ---------------------------------------------------------------------------

def test_builtin_provider_still_resolves(monkeypatch):
    """A built-in provider like ``anthropic`` must still resolve through the
    same branch without regression."""
    _stub_hermes_cli(monkeypatch)
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _with_config(
        {
            "model": {
                "default": "claude-sonnet-4-5",
                "provider": "anthropic",
            },
            "providers": {
                "anthropic": {
                    "api_key": "sk-test-key",
                },
            },
        }
    )
    try:
        result = config.get_available_models()
    finally:
        restore()

    groups_by_pid = {g["provider_id"]: g for g in result["groups"]}
    assert "anthropic" in groups_by_pid, (
        f"Expected 'anthropic' in groups, got: {list(groups_by_pid.keys())}"
    )
    # Should have at least one model (from _PROVIDER_MODELS fallback)
    group = groups_by_pid["anthropic"]
    assert len(group["models"]) > 0, "anthropic group should have models"


# ---------------------------------------------------------------------------
# Test case 4 — _PROVIDER_MODELS fallback still works when no cfg key
# ---------------------------------------------------------------------------

def test_provider_models_fallback_when_no_config_key(monkeypatch):
    """A provider in _PROVIDER_MODELS but NOT in cfg["providers"] must
    still fall back to the static model list."""
    _stub_hermes_cli(monkeypatch)
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    restore = _with_config(
        {
            "model": {
                "default": "deepseek-chat",
                "provider": "deepseek",
            },
            # No providers section at all
        }
    )
    try:
        result = config.get_available_models()
    finally:
        restore()

    groups_by_pid = {g["provider_id"]: g for g in result["groups"]}
    assert "deepseek" in groups_by_pid, (
        f"Expected 'deepseek' in groups, got: {list(groups_by_pid.keys())}"
    )
    group = groups_by_pid["deepseek"]
    assert len(group["models"]) > 0, "deepseek group should have models from _PROVIDER_MODELS"