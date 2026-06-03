import urllib.error
import urllib.request
from email.message import Message

from api import config


class _ConfigState:
    def __enter__(self):
        self.old_cfg = config.cfg
        self.old_mtime = config._cfg_mtime
        self.old_cache = config._available_models_cache
        self.old_cache_ts = config._available_models_cache_ts
        self.old_cache_fp = config._available_models_cache_source_fingerprint
        config._cfg_mtime = 0.0
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._available_models_cache_source_fingerprint = None
        return self

    def __exit__(self, exc_type, exc, tb):
        config.cfg = self.old_cfg
        config._cfg_mtime = self.old_mtime
        config._available_models_cache = self.old_cache
        config._available_models_cache_ts = self.old_cache_ts
        config._available_models_cache_source_fingerprint = self.old_cache_fp
        return False


def _configure_named_custom_provider(tmp_path, monkeypatch, *, model=None):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    entry = {
        "name": "Broken Proxy",
        "base_url": "https://broken.example/v1",
        "api_key": "bad-key",
    }
    if model:
        entry["model"] = model
    config.cfg = {
        "model": {"provider": "openai-codex", "default": "gpt-5.5"},
        "providers": {},
        "fallback_providers": [],
        "custom_providers": [entry],
    }


def _groups_by_provider(data):
    return {group["provider_id"]: group for group in data["groups"]}


def test_named_custom_provider_models_endpoint_401_surfaces_error(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(
            getattr(req, "full_url", "https://broken.example/v1/models"),
            401,
            "Unauthorized",
            hdrs=Message(),
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _ConfigState():
        _configure_named_custom_provider(tmp_path, monkeypatch, model="broken/manual")
        data = config.get_available_models()

    group = _groups_by_provider(data)["custom:broken-proxy"]
    error = group["models_endpoint_error"]
    assert error["kind"] == "auth"
    assert error["code"] == 401
    assert "check the API key" in error["message"]
    assert "broken-proxy" in error["message"]
    assert "@custom:broken-proxy:broken/manual" in {m["id"] for m in group["models"]}


def test_named_custom_provider_models_endpoint_network_error_surfaces_empty_group(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout=10):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _ConfigState():
        _configure_named_custom_provider(tmp_path, monkeypatch)
        data = config.get_available_models()

    group = _groups_by_provider(data)["custom:broken-proxy"]
    assert group["models"] == []
    assert group["models_endpoint_error"]["kind"] == "network"
    assert group["models_endpoint_error"]["code"] is None
    assert "verify base_url" in group["models_endpoint_error"]["message"]


def test_named_custom_provider_models_endpoint_5xx_preserves_status(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(
            getattr(req, "full_url", "https://broken.example/v1/models"),
            502,
            "Bad Gateway",
            hdrs=Message(),
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _ConfigState():
        _configure_named_custom_provider(tmp_path, monkeypatch, model="broken/manual")
        data = config.get_available_models()

    error = _groups_by_provider(data)["custom:broken-proxy"]["models_endpoint_error"]
    assert error["kind"] == "http"
    assert error["code"] == 502
    assert "returned 502" in error["message"]

def test_named_custom_provider_models_endpoint_network_error_uses_short_timeout(monkeypatch, tmp_path):
    observed_timeouts = []

    def fake_urlopen(req, timeout=10):
        # Only record timeouts for the broken-proxy custom endpoint — unrelated
        # background probes (Copilot token fetch, OpenRouter free-tier discovery, etc.)
        # also call urlopen during get_available_models() and would otherwise pollute
        # the assertion. The contract we're pinning: the broken-proxy /v1/models call
        # uses CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS, not the urllib default 10.
        full_url = getattr(req, "full_url", "")
        if "broken.example" in str(full_url):
            observed_timeouts.append(timeout)
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _ConfigState():
        _configure_named_custom_provider(tmp_path, monkeypatch)
        data = config.get_available_models()

    group = _groups_by_provider(data)["custom:broken-proxy"]
    assert group["models"] == []
    assert group["models_endpoint_error"]["kind"] == "network"
    assert observed_timeouts == [config.CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS]
    assert max(observed_timeouts) <= 5.0


def test_frontend_model_picker_renders_provider_endpoint_hint():
    ui = open("static/ui.js", encoding="utf-8").read()
    css = open("static/style.css", encoding="utf-8").read()

    assert "models_endpoint_error" in ui
    assert "dataset.modelsEndpointError" in ui
    assert "model-provider-hint" in ui
    assert "entry.modelsEndpointError.message" in ui
    assert ".model-provider-hint" in css
