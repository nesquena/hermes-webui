"""Regression tests for #6516: colon-named custom provider fixes.

Covers three defect classes that affected users running multiple custom
OpenAI-compatible endpoints whose names contain colons (e.g. IP:port):

  1. ``_named_custom_provider_slug_for_provider()`` failed to match a
     colon-bearing ``model.provider`` value against the hyphenated slug
     derived from ``custom_providers[].name``, causing duplicate groups
     in the model picker.

  2. ``_get_provider_base_url()`` only searched ``providers`` and
     ``model`` sections, never ``custom_providers[]``, so non-default
     custom providers returned ``None`` base_url → HTTP 404 on every
     message.

  3. ``get_providers()`` and ``load_settings()`` emitted the raw
     ``model.provider`` string without normalization, so the frontend
     ``active_provider`` field mismatched the hyphenated provider IDs
     in the providers list.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

import api.config as config
from api.providers import get_providers


@pytest.fixture(autouse=True)
def _isolate_models_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


_COLON_CFG = {
    "model": {
        "default": "qwen3.6-27b-nvfp4",
        "provider": "custom:192.168.5.242:8000",
        "base_url": "http://192.168.5.242:8000/v1",
    },
    "custom_providers": [
        {
            "name": "192.168.5.242:8000",
            "base_url": "http://192.168.5.242:8000/v1",
            "model": "qwen3.6-27b-nvfp4",
        },
        {
            "name": "192.168.5.250:8000",
            "base_url": "http://192.168.5.250:8000/v1",
            "model": "coolthor/Huihui-Qwen3.6-35B-A3B-abliterated-FP8-DYNAMIC",
        },
        {
            "name": "192.168.5.252:8888",
            "base_url": "http://192.168.5.252:8888/v1",
            "model": "deepseek-v4-flash-dspark",
        },
    ],
}


def _apply_colon_cfg():
    """Temporarily replace ``config.cfg`` with a multi-provider colon-named
    configuration and return a restore callback."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(json.loads(json.dumps(_COLON_CFG)))
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()

    def restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    return restore


# ── Bug 1: slug normalisation ──────────────────────────────────────────────


def test_named_provider_slug_matches_colon_config_provider():
    """``_resolve_configured_provider_id`` with a colon-bearing provider id
    must return the correct hyphenated custom slug even when no slug with
    an identical suffix exists in the table."""
    restore = _apply_colon_cfg()
    try:
        resolved = config._resolve_configured_provider_id(
            "custom:192.168.5.242:8000",
            config.cfg,
            base_url="http://192.168.5.242:8000/v1",
        )
    finally:
        restore()

    assert resolved == "custom:192.168.5.242-8000", (
        f"Expected hyphenated slug 'custom:192.168.5.242-8000', "
        f"got {resolved!r}"
    )


def test_named_provider_slug_matches_colon_config_provider_no_base_url():
    """Same as above but without ``base_url`` — the match must still work
    because the fix lives in ``_named_custom_provider_slug_for_provider``,
    not the base-url fallback."""
    restore = _apply_colon_cfg()
    try:
        resolved = config._resolve_configured_provider_id(
            "custom:192.168.5.242:8000",
            config.cfg,
        )
    finally:
        restore()

    assert resolved == "custom:192.168.5.242-8000", (
        f"Expected hyphenated slug, got {resolved!r}"
    )


# ── Bug 2: base_url resolution for non-default custom providers ────────────


def test_get_provider_base_url_custom_provider_list():
    """``_get_provider_base_url`` must search ``custom_providers[]`` so that
    a non-default custom provider slug resolves to the correct endpoint URL."""
    restore = _apply_colon_cfg()
    try:
        url = config._get_provider_base_url("custom:192.168.5.250-8000")
    finally:
        restore()

    assert url == "http://192.168.5.250:8000/v1", (
        f"Expected base_url for 192.168.5.250, got {url!r}"
    )


def test_get_provider_base_url_second_non_default():
    """Another non-default custom provider to ensure the lookup is not
    accidentally hardcoded to the first entry."""
    restore = _apply_colon_cfg()
    try:
        url = config._get_provider_base_url("custom:192.168.5.252-8888")
    finally:
        restore()

    assert url == "http://192.168.5.252:8888/v1", (
        f"Expected base_url for 192.168.5.252, got {url!r}"
    )


def test_get_provider_base_url_default_provider_still_works():
    """The default provider must still resolve correctly (regression guard —
    the custom_providers list lookup must not shadow the model-section path)."""
    restore = _apply_colon_cfg()
    try:
        url = config._get_provider_base_url("custom:192.168.5.242-8000")
    finally:
        restore()

    assert url == "http://192.168.5.242:8000/v1", (
        f"Expected base_url for 192.168.5.242, got {url!r}"
    )


def test_available_models_no_duplicate_groups_for_active_provider():
    """The active provider must appear exactly once in the groups list,
    not as both a base-url-derived ``custom:<ip-colon-port>`` group AND a
    named ``custom:<ip-hyphen-port>`` group."""
    restore = _apply_colon_cfg()
    try:
        result = config.get_available_models(prefer_cache=True)
    finally:
        restore()

    groups = result.get("groups", [])
    provider_ids = [g["provider_id"] for g in groups]
    slug = "custom:192.168.5.242-8000"
    count = provider_ids.count(slug)
    assert count == 1, (
        f"Expected exactly one group with provider_id {slug!r}, "
        f"found {count}. All provider_ids: {provider_ids}"
    )


def test_resolve_model_provider_non_default_returns_correct_tuple():
    """``resolve_model_provider`` with an ``@provider:model`` hint for a
    non-default custom provider must return the correct model, provider,
    and base_url."""
    restore = _apply_colon_cfg()
    try:
        model, provider, base_url = config.resolve_model_provider(
            "@custom:192.168.5.250-8000:coolthor/Huihui-Qwen3.6-35B-A3B-abliterated-FP8-DYNAMIC"
        )
    finally:
        restore()

    assert model == "coolthor/Huihui-Qwen3.6-35B-A3B-abliterated-FP8-DYNAMIC"
    assert provider == "custom:192.168.5.250-8000"
    assert base_url == "http://192.168.5.250:8000/v1", (
        f"Expected base_url for non-default custom provider, "
        f"got {base_url!r}"
    )


# ── Bug 3: normalised active_provider in API responses ─────────────────────


def test_get_providers_active_provider_normalised():
    """``get_providers()`` must emit the normalized slug as
    ``active_provider``, not the raw config string with colons."""
    restore = _apply_colon_cfg()
    try:
        result = get_providers()
    finally:
        restore()

    ap = result.get("active_provider")
    assert ap == "custom:192.168.5.242-8000", (
        f"Expected normalised active_provider "
        f"'custom:192.168.5.242-8000', got {ap!r}"
    )


def test_get_providers_includes_custom_provider_ids():
    """Every custom provider from the config must appear in the providers
    list with the hyphenated slug ID."""
    restore = _apply_colon_cfg()
    try:
        result = get_providers()
    finally:
        restore()

    ids = {p["id"] for p in result.get("providers", [])}
    for expected in (
        "custom:192.168.5.242-8000",
        "custom:192.168.5.250-8000",
        "custom:192.168.5.252-8888",
    ):
        assert expected in ids, (
            f"Expected provider id {expected!r} in providers list, "
            f"got ids {ids}"
        )


def test_load_settings_default_model_provider_normalised(monkeypatch):
    """The settings dict returned by ``load_settings()`` must contain a
    normalized ``default_model_provider`` slug, not a raw colon-bearing
    provider string."""
    restore = _apply_colon_cfg()
    try:
        fake_path = pathlib.Path(tempfile.mkstemp(suffix=".json")[1])
        monkeypatch.setattr(config, "SETTINGS_FILE", fake_path)
        fake_path.write_text('{"theme": "dark"}', encoding="utf-8")

        settings = config.load_settings()
    finally:
        restore()

    dmp = settings.get("default_model_provider")
    assert dmp == "custom:192.168.5.242-8000", (
        f"Expected normalised default_model_provider "
        f"'custom:192.168.5.242-8000', got {dmp!r}"
    )
