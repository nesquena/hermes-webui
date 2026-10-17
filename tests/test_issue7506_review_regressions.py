"""Maintainer regressions: cache identity, admission and healthy slow probes."""
import json
import sys
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import api.config as cfg
from tests.test_issue7481_custom_probe_budget_fairness import (
    _configure,
    _models_by_provider,
    _REAL_SAVE_MODELS_CACHE_TO_DISK,
    isolate_models_catalog_state as _catalog_fixture,
)


isolate_models_catalog_state = _catalog_fixture
_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.mark.parametrize("fast_active,delay", [(False, 2.5), (True, 1.5), ("alone", 2.5)])
def test_real_slow_healthy_endpoint_is_listed_and_cached(
    monkeypatch, isolate_models_catalog_state, fast_active, delay
):
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # Restore real DNS/socket behavior: the shared fixture disables DNS for fakes.
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/slow/"):
                time.sleep(delay)
            body = json.dumps({"data": [{"id": "healthy-model"}]}).encode()
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        _configure(monkeypatch, active_base_url=base + "/fast/v1" if fast_active else None,
                   custom_providers=[{"name": "Gateway", "base_url": base + "/slow/v1"}])
        if fast_active == "alone":
            cfg.cfg["model"]["base_url"] = base + "/slow/v1"
            cfg.cfg["custom_providers"] = []
        monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0)
        for catalog in (cfg.get_available_models(), cfg.get_available_models()):
            provider = "lmstudio" if fast_active == "alone" else "custom:gateway"
            assert "healthy-model" in _models_by_provider(catalog).get(provider, [])
            assert "unreachable" not in json.dumps(catalog)
        assert requests.count("/slow/v1/models") == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("invalidate", [False, True])
def test_slice_timeout_is_not_cached_as_unreachable_and_gets_full_retry(
    monkeypatch, isolate_models_catalog_state, invalidate
):
    import urllib.request
    from tests.test_issue7481_custom_probe_budget_fairness import _FakeResponse

    _configure(monkeypatch, active_base_url=None, custom_providers=[
        {"name": "Slow", "base_url": "https://slow.example/v1"},
        {"name": "Fast", "base_url": "https://fast.example/v1"},
    ])
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _REAL_SAVE_MODELS_CACHE_TO_DISK)
    retry_started = threading.Event()
    release = threading.Event()
    calls = []

    def probe(req, timeout=None):
        calls.append((req.full_url, timeout))
        if "slow.example" in req.full_url:
            if timeout < 5:
                raise TimeoutError("slice exhausted")
            retry_started.set()
            assert release.wait(5)
        return _FakeResponse({"data": [{"id": "healthy-model"}]})

    monkeypatch.setattr(urllib.request, "urlopen", probe)
    try:
        result = cfg.get_available_models()
        assert "unreachable" not in json.dumps(result)
        assert _models_by_provider(result)["custom:fast"] == ["healthy-model"]
        assert retry_started.wait(3), "slice-truncated probe was never retried"
        assert cfg._available_models_cache is None
        assert not cfg._get_models_cache_path().exists()
        if invalidate:
            cfg.invalidate_models_cache()
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "models-catalog-rebuild":
                thread.join(5)
    if invalidate:
        assert cfg._available_models_cache is None
        assert not cfg._get_models_cache_path().exists()
        assert cfg._cache_build_in_progress is False
        return
    result = cfg.get_available_models()
    assert _models_by_provider(result)["custom:slow"] == ["healthy-model"]
    assert "unreachable" not in json.dumps(result)
    assert len([url for url, _ in calls if "slow.example" in url]) == 2


def test_lmstudio_fallback_uses_scheduled_timeout(monkeypatch, isolate_models_catalog_state):
    import urllib.request
    from tests.test_issue7481_custom_probe_budget_fairness import _FakeResponse

    _configure(monkeypatch, active_base_url=None,
               provider_base_url="https://lm-only.example/v1")
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cfg._CustomProbeSchedule, "next_timeout", lambda self: 0.125)
    calls = []

    def probe(req, timeout=None):
        calls.append((req.full_url, timeout))
        return _FakeResponse({"data": [{"id": "lm-live"}]})

    monkeypatch.setattr(urllib.request, "urlopen", probe)
    result = cfg.get_available_models()
    assert "lm-live" in _models_by_provider(result)["lmstudio"]
    assert len(calls) == 1
    assert calls[0][0] == "https://lm-only.example/v1/models"
    # The schedule limits the wait; HTTP can use the remaining shared window.
    assert 0.125 <= calls[0][1] < cfg._LIVE_REBUILD_BUDGET_SECONDS


@pytest.mark.parametrize("provider", [None, "anthropic"])
def test_invalidation_evicts_credentials_before_readmission(
    monkeypatch, isolate_models_catalog_state, provider
):
    _configure(monkeypatch, active_base_url=None)
    cfg.cfg["model"] = {}
    # This test owns the credential-pool admission signal. Agent auth discovery
    # is independent evidence: earlier tests can leave synthetic credentials in
    # the session home, and pool invalidation must not revoke those credentials.
    # Isolate that external source without replacing the pool/cache paths below.
    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(
        list_available_providers=lambda: [],
    ))
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", SimpleNamespace(
        get_auth_status=lambda _pid: {"logged_in": False},
    ))
    auth_path = isolate_models_catalog_state["auth_store_path"]
    auth_path.write_text(json.dumps({"credential_pool": {"anthropic": []}}))
    pool = SimpleNamespace(entries=lambda: [SimpleNamespace(source="manual")])
    tag = cfg._credential_pool_profile_tag()
    monkeypatch.setattr(cfg, "_CREDENTIAL_POOL_CACHE", {(tag, "anthropic"): (time.time(), pool)})
    monkeypatch.setitem(sys.modules, "agent.credential_pool", SimpleNamespace(
        load_pool=lambda pid: SimpleNamespace(entries=lambda: [])
    ))
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _REAL_SAVE_MODELS_CACHE_TO_DISK)
    epoch_finished = threading.Event()
    release = threading.Event()
    original = cfg._invalidate_models_catalog_epoch

    def pause_after_admission_reopens(**kwargs):
        original(**kwargs)
        epoch_finished.set()
        assert release.wait(10)

    monkeypatch.setattr(cfg, "_invalidate_models_catalog_epoch", pause_after_admission_reopens)
    errors = []

    def invalidate():
        try:
            if provider is None:
                cfg.invalidate_models_cache()
            else:
                cfg.invalidate_provider_models_cache(provider)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=invalidate)
    thread.start()
    try:
        assert epoch_finished.wait(5)
        catalog = cfg.get_available_models()
        assert "anthropic" not in _models_by_provider(catalog)
        assert "anthropic" not in _models_by_provider(cfg._available_models_cache)
        durable = json.loads(cfg._get_models_cache_path().read_text())
        assert "anthropic" not in _models_by_provider(durable)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not errors
