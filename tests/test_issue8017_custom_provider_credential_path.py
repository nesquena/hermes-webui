"""
Credential-path regression tests for #8017 catalog identities.

The catalog mints a fallback identity for a custom_providers[] entry whose
name slugifies to "" (a pure-CJK name) — from its ``provider_key`` here, so
the record 晨光鑫遇专用 / ``cgxy-cpa`` is advertised as ``custom:cgxy-cpa``
and the routing lookups (``resolve_model_provider``,
``resolve_custom_provider_bundle``) round-trip it. The credential lookups
in ``api/providers.py`` matched records by name-derived slug only, so
through that same advertised identity the row was invisible:
``provider_has_usable_credential`` reported False,
``_get_provider_api_key`` returned None, and
``_clean_provider_key_from_config`` silently left the key on disk.

These tests pin all three credential lookups through the catalog identity,
plus the historical raw-name contract (``custom:local proxy`` for a record
named "Local Proxy") that the name spellings must keep honouring.
"""

import contextlib

import pytest
import yaml

import api.config as config
import api.providers as providers

CJK_NAME = "晨光鑫遇专用"
CATALOG_ID = "custom:cgxy-cpa"
ENTRY = {
    "name": CJK_NAME,
    "provider_key": "cgxy-cpa",
    "base_url": "http://127.0.0.1:8317/v1",
    "api_key": "sk-cgxy",
    "model": "shared",
}


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the models TTL cache before and after every test in this file."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


@contextlib.contextmanager
def _cfg(custom_providers=None):
    """Patch config.cfg in place (1806-fixture style), restore afterwards."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    if custom_providers is not None:
        config.cfg["custom_providers"] = custom_providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    config.invalidate_models_cache()
    try:
        yield
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()


class TestCredentialPathThroughCatalogIdentity:
    def test_has_usable_credential(self):
        with _cfg([dict(ENTRY)]):
            assert providers.provider_has_usable_credential(CATALOG_ID) is True

    def test_get_provider_api_key(self):
        with _cfg([dict(ENTRY)]):
            assert providers._get_provider_api_key(CATALOG_ID) == "sk-cgxy"

    def test_clean_provider_key_from_config(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({"custom_providers": [dict(ENTRY)]}),
            encoding="utf-8",
        )

        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(providers, "reload_config", lambda: None)
        monkeypatch.setattr(
            providers, "invalidate_providers_cache", lambda: None, raising=False
        )

        providers._clean_provider_key_from_config(CATALOG_ID)

        reloaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        entry = reloaded["custom_providers"][0]
        assert entry["name"] == CJK_NAME
        assert entry["provider_key"] == "cgxy-cpa"
        assert "api_key" not in entry


class TestRawNameContractPreserved:
    LOCAL = {
        "name": "Local Proxy",
        "base_url": "http://127.0.0.1:9000/v1",
        "api_key": "sk-local",
        "model": "m",
    }

    def test_raw_name_spelling_still_resolves(self):
        with _cfg([dict(self.LOCAL)]):
            assert providers._get_provider_api_key("custom:local proxy") == "sk-local"
            assert (
                providers.provider_has_usable_credential("custom:local proxy") is True
            )

    def test_name_derived_slug_still_resolves(self):
        with _cfg([dict(self.LOCAL)]):
            assert providers._get_provider_api_key("custom:local-proxy") == "sk-local"
