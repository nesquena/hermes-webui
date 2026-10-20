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

Test seam: ``_read_custom_endpoint_models``, ``_read_probe_payload`` and the
probe memo are all **closures/local state** inside ``get_available_models()`` --
they are not module globals, so
``monkeypatch.setattr(config, "_read_custom_endpoint_models", ...)`` has no
effect.  The lowest patchable seam is ``urllib.request.urlopen``: the closure
does ``import urllib.request`` and resolves the attribute at call time.

Note the ordering in every test: the probe fires from inside
``get_available_models()``, so the returned ids are read *before* ``calls`` is
asserted.  Asserting on ``calls`` first would only ever see an empty list.
"""
import json
import urllib.request

BASE = {
    "name": "Example-Relay",
    "base_url": "http://relay.invalid/v1",
    "key_env": "EXAMPLE_RELAY_API_KEY",
    "api_mode": "chat_completions",
    "models": {"model-a": {}, "model-b": {}},
}


def _wire(monkeypatch, tmp_path, entry, live_ids):
    """Wire one custom provider behind a controllable live /v1/models probe."""
    import api.config as config

    monkeypatch.setattr(
        config, "cfg",
        {"model": {"default": "model-a", "provider": "custom:example-relay"},
         "custom_providers": [entry]},
        raising=False,
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "reload_config", lambda *a, **k: None)
    monkeypatch.setattr(config, "reload_config_if_stale", lambda *a, **k: None)

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
        url = getattr(req, "full_url", str(req))
        # The rebuild also performs unrelated urlopen calls (e.g. the Copilot
        # token exchange runs on its own thread), so record only the catalog
        # probe this test is about.
        if "/models" in url:
            calls.append(url)
        return _Resp({"object": "list", "data": [{"id": m} for m in live_ids]})

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    config.invalidate_models_cache()
    return config, calls


def _ids(config, pid="custom:example-relay"):
    payload = config.get_available_models(force_refresh=True)
    for group in payload.get("groups", []):
        if group.get("provider_id") == pid:
            return [m["id"] for m in group["models"]]
    raise AssertionError("group %r missing; present: %r" % (pid, payload.get("groups")))


def test_custom_provider_discovered_defers_to_live_catalog(monkeypatch, tmp_path):
    """models_discovered -> the probe runs and the live catalog is authoritative.

    ``model-b`` is configured but absent from the live probe, so it must be
    dropped (live wins); ``model-c`` is live-only and must appear.
    """
    config, calls = _wire(
        monkeypatch, tmp_path, dict(BASE, models_discovered=True), ["model-a", "model-c"]
    )
    ids = _ids(config)
    assert calls, "live /v1/models probe was skipped -> refresh button is a no-op"
    assert ids == ["model-a", "model-c"]


def test_custom_provider_curated_allowlist_pins_without_probe(monkeypatch, tmp_path):
    """Without models_discovered a curated allowlist must still win (no probe)."""
    config, calls = _wire(monkeypatch, tmp_path, dict(BASE), ["model-a", "model-c"])
    ids = _ids(config)
    assert not calls, "curated allowlist must NOT trigger a live probe"
    assert ids == ["model-a", "model-b"]


def test_custom_provider_discover_models_false_pins(monkeypatch, tmp_path):
    """``discover_models: false`` beats models_discovered and suppresses the probe."""
    config, calls = _wire(
        monkeypatch, tmp_path,
        dict(BASE, models_discovered=True, discover_models=False), ["model-a", "model-c"],
    )
    ids = _ids(config)
    assert not calls, "discover_models:false must suppress the probe"
    assert ids == ["model-a", "model-b"]


def test_custom_provider_empty_probe_falls_back_to_configured(monkeypatch, tmp_path):
    """An empty/failed live probe must keep the configured discovered ids."""
    config, calls = _wire(monkeypatch, tmp_path, dict(BASE, models_discovered=True), [])
    ids = _ids(config)
    assert calls, "probe should still be attempted"
    assert ids == ["model-a", "model-b"]
