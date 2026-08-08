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


# ── Bug 4: resolve_custom_provider_connection slug collision and config snapshot ──


def _config_dict(**overrides) -> dict:
    """Build a minimal config dict with an optional ``custom_providers`` list."""
    base = {
        "model": {
            "default": "test-model",
            "provider": "custom:test-provider",
            "base_url": "http://default.test/v1",
        },
    }
    base.update(overrides)
    return base


def test_resolve_custom_provider_connection_one_blank_one_populated():
    """A lossy-slug collision with one blank entry and one populated entry
    must resolve to the populated entry's URL and key.

    Uses ``my-endpoint!`` and ``my-endpoint?`` — both slugify to
    ``custom:my-endpoint`` (the ``!`` and ``?`` are stripped by the slugger),
    so they are genuine slug siblings.  The populated entry (non-empty
    base_url) wins via the discriminator in ``_resolve_custom_provider_ambiguous``
    which uses ``model.base_url`` to pick the unique match.
    """
    cfg = _config_dict(
        model={
            "default": "test-model",
            "provider": "custom:my-endpoint",
            "base_url": "http://valid.test/v1",
        },
        custom_providers=[
            {"name": "my-endpoint!", "base_url": "http://valid.test/v1", "api_key": "real-key"},
            {"name": "my-endpoint?", "base_url": "", "api_key": ""},
        ],
    )
    key, url = config.resolve_custom_provider_connection(
        "custom:my-endpoint", config_data=cfg,
    )
    assert url == "http://valid.test/v1", (
        f"Expected URL from populated entry, got {url!r}"
    )
    assert key == "real-key", (
        f"Expected key from populated entry, got {key!r}"
    )


def test_resolve_custom_provider_connection_lossy_slug_collision_no_dummy_key():
    """Two lossy-slug-sibling entries with distinct URLs must fail closed
    (return None, None) rather than emitting a dummy key or picking the
    first entry."""
    cfg = _config_dict(
        custom_providers=[
            {"name": "endpoint-a!", "base_url": "http://a.test/v1", "api_key": "key-a"},
            {"name": "endpoint-a?", "base_url": "http://b.test/v1", "api_key": "key-b"},
        ],
    )
    key, url = config.resolve_custom_provider_connection(
        "custom:endpoint-a", config_data=cfg,
    )
    assert key is None, f"Expected None for ambiguous collision, got key={key!r}"
    assert url is None, f"Expected None for ambiguous collision, got url={url!r}"


def test_resolve_custom_provider_connection_expected_url_discriminates():
    """When slug-sibling entries collide and the model section provides a
    ``base_url`` matching exactly one sibling, that sibling wins."""
    cfg = _config_dict(
        custom_providers=[
            {"name": "sib-a", "base_url": "http://first.test/v1", "api_key": "key-a"},
            {"name": "sib-a", "base_url": "http://second.test/v1", "api_key": "key-b"},
        ],
        model={
            "default": "test-model",
            "provider": "custom:sib-a",
            "base_url": "http://second.test/v1",
        },
    )
    key, url = config.resolve_custom_provider_connection(
        "custom:sib-a", config_data=cfg,
    )
    assert url == "http://second.test/v1", (
        f"Expected discriminated URL, got {url!r}"
    )
    assert key == "key-b", (
        f"Expected discriminated key, got {key!r}"
    )


def test_resolve_custom_provider_connection_accepts_config_data():
    """``resolve_custom_provider_connection`` must accept an explicit
    ``config_data`` dict instead of reading ambient ``get_config()``."""
    cfg = _config_dict(
        custom_providers=[
            {"name": "worker", "base_url": "http://worker.test/v1", "api_key": "worker-key"},
        ],
    )
    key, url = config.resolve_custom_provider_connection(
        "custom:worker", config_data=cfg,
    )
    assert url == "http://worker.test/v1", (
        f"Expected worker URL from config_data, got {url!r}"
    )
    assert key == "worker-key", (
        f"Expected worker key from config_data, got {key!r}"
    )
