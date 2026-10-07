"""Regression coverage for #7404 on the ``custom_providers:`` branch.

The sibling test ``test_issue7404_models_discovered_not_allowlist.py`` covers the
``providers:`` branch.  This file pins the ``custom_providers:`` side, which had
the same asymmetry: a ``models_discovered: true`` entry was treated as an
allowlist, so the live /v1/models probe was skipped and the "Refresh models"
button just re-rendered the persisted mapping.

Contract (docs/architecture/live-models-allowlist.md, signal 1): a discovered
catalog derives **no** allowlist and defers to the live catalog -- so ids the
provider has since dropped must NOT be re-appended, and /api/models must agree
with /api/models/live.

Note on the test seam: the real reader ``_read_custom_endpoint_models`` is a
nested closure inside ``get_available_models`` (it is NOT a module global), so
``monkeypatch.setattr(config, "_read_custom_endpoint_models", ...)`` has no
effect.  These tests intercept the lowest network layer instead --
``urllib.request.urlopen`` -- which the closure resolves at call time via
``import urllib.request``.
"""
import json
import urllib.request

BASE = {
    "name": "New-API-Chat",
    "base_url": "http://192.168.2.76:3000/v1",
    "key_env": "HERMES_CUSTOM_NEW_API_CHAT_API_KEY",
    "api_mode": "chat_completions",
    "models": {"model-a": {}, "model-b": {}},
}


def _wire(monkeypatch, tmp_path, entry, live_ids):
    """Wire a single custom provider with a controllable live /v1/models probe."""
    import api.config as config

    monkeypatch.setattr(
        config, "cfg",
        {"model": {"default": "model-a", "provider": "custom:new-api-chat"},
         "custom_providers": [entry]},
        raising=False,
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "fp"})
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)

    calls = []

    class _Resp:
        def __init__(self, payload):
            self._b = json.dumps(payload).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, *a, **k):
        calls.append(getattr(req, "full_url", str(req)))
        return _Resp({"object": "list", "data": [{"id": m} for m in live_ids]})

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    config.invalidate_models_cache()
    return config, calls


def _ids(config, pid="custom:new-api-chat"):
    payload = config.get_available_models(force_refresh=True)
    for group in payload.get("groups", []):
        if group.get("provider_id") == pid:
            return [m["id"] for m in group["models"]]
    raise AssertionError("group %r missing; present: %r" % (pid, payload.get("groups")))


def test_custom_provider_discovered_defers_to_live_catalog(monkeypatch, tmp_path):
    """models_discovered -> probe runs and the live catalog is authoritative.

    ``model-b`` is configured but absent from the live probe, so it must be
    dropped (live wins); ``model-c`` is live-only and must appear.
    """
    config, calls = _wire(
        monkeypatch, tmp_path, dict(BASE, models_discovered=True), ["model-a", "model-c"]
    )
    assert calls, "live /v1/models probe was skipped -> refresh button is a no-op"
    assert _ids(config) == ["model-a", "model-c"]


def test_custom_provider_curated_allowlist_pins_without_probe(monkeypatch, tmp_path):
    """Without models_discovered a curated allowlist must still win (no probe)."""
    config, calls = _wire(monkeypatch, tmp_path, dict(BASE), ["model-a", "model-c"])
    assert not calls, "curated allowlist must NOT trigger a live probe"
    assert _ids(config) == ["model-a", "model-b"]


def test_custom_provider_discover_models_false_pins(monkeypatch, tmp_path):
    """``discover_models: false`` (and its string forms) beat models_discovered."""
    config, calls = _wire(
        monkeypatch, tmp_path,
        dict(BASE, models_discovered=True, discover_models=False), ["model-a", "model-c"],
    )
    assert not calls, "discover_models:false must suppress the probe"
    assert _ids(config) == ["model-a", "model-b"]


def test_custom_provider_empty_probe_falls_back_to_configured(monkeypatch, tmp_path):
    """An empty/failed live probe must keep the configured discovered ids."""
    config, calls = _wire(monkeypatch, tmp_path, dict(BASE, models_discovered=True), [])
    assert calls, "probe should still be attempted"
    assert _ids(config) == ["model-a", "model-b"]
