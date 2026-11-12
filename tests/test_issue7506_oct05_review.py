"""October 5 maintainer regressions for bounded catalog continuation."""
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.config as cfg
from tests.test_issue7481_custom_probe_budget_fairness import (
    _configure,
    _FakeRouteHandler,
    isolate_models_catalog_state as _catalog_fixture,
)

isolate_models_catalog_state = _catalog_fixture
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_READ_LIVE_IDS = cfg._read_live_provider_model_ids


class _BuildDoneEvents:
    """Select the foreground wait outcome without racing the OS clock."""

    def __init__(self, boundary):
        self.boundary = boundary
        self.count = 0

    def __getattr__(self, name):
        return getattr(threading, name)

    def Event(self):
        self.count += 1
        event = threading.Event()
        if self.count == 2:  # abandoned, build_done, budget_exceeded
            wait = event.wait

            def foreground_wait(timeout=None):
                assert wait(5), "first pass did not finish"
                return not self.boundary

            event.wait = foreground_wait
        return event


@pytest.mark.parametrize("boundary", [False, True])
@pytest.mark.parametrize("stale", [False, True])
def test_empty_truncated_partial_uses_existing_fallback(
    monkeypatch, isolate_models_catalog_state, boundary, stale, caplog
):
    import urllib.request
    from tests.test_issue7481_custom_probe_budget_fairness import _FakeResponse, _catalog

    _configure(monkeypatch, active_base_url="https://slow.example/v1")
    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(
        provider_model_ids=lambda _pid: [], list_available_providers=lambda: [],
    ))
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cfg, "threading", _BuildDoneEvents(boundary))
    disk = _catalog("stale") if stale else None
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: disk)
    release = threading.Event()

    def probe(req, timeout=None):
        if timeout < 5:
            raise TimeoutError("slice exhausted")
        assert release.wait(5)
        return _FakeResponse({"data": [{"id": "healthy"}]})

    monkeypatch.setattr(urllib.request, "urlopen", probe)
    try:
        result = cfg.get_available_models()
        assert result["groups"], "all-truncated first pass must not empty the picker"
        if stale:
            assert result == disk
        else:
            assert any(m["id"] == "some-local-model"
                       for g in result["groups"] for m in g["models"])
        assert cfg._available_models_cache is None
        if not boundary:
            assert "exceeded" not in caplog.text
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "models-catalog-rebuild":
                thread.join(5)


@pytest.mark.parametrize("provider", ["anthropic", "ollama-cloud", "openai-codex", "nous", "lmstudio", "openrouter"])
@pytest.mark.parametrize("live_ids", [["live-model"], [], None])
def test_noncustom_live_lookup_is_memoized_only_for_one_rebuild(
    monkeypatch, isolate_models_catalog_state, live_ids, provider
):
    import urllib.request
    from tests.test_issue7481_custom_probe_budget_fairness import _FakeResponse

    _configure(monkeypatch, active_base_url=None, custom_providers=[
        {"name": "Slow", "base_url": "https://slow.example/v1"},
    ])
    cfg.cfg["model"]["provider"] = provider
    cfg.cfg["providers"][provider] = {"api_key": "test-key"}
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0)
    lookups = []
    probes = []

    def lookup(pid):
        lookups.append(pid)
        if live_ids is None:
            raise RuntimeError("discovery unavailable")
        return list(live_ids)

    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(
        provider_model_ids=lookup, list_available_providers=lambda: [],
        fetch_openrouter_models=lambda: [(mid, "") for mid in lookup("openrouter")],
    ))

    def probe(req, timeout=None):
        if "openrouter.ai" in req.full_url:
            lookups.append("openrouter-free")
            return _FakeResponse({"data": []})
        probes.append(timeout)
        if timeout < 5:
            raise TimeoutError("slice exhausted")
        return _FakeResponse({"data": [{"id": "healthy"}]})

    # Use the real generic resolver as well as each special provider branch.
    monkeypatch.setattr(cfg, "_read_live_provider_model_ids", _REAL_READ_LIVE_IDS)
    monkeypatch.setattr(urllib.request, "urlopen", probe)
    for expected in (1, 2):
        cfg.get_available_models()
        for thread in threading.enumerate():
            if thread.name == "models-catalog-rebuild":
                thread.join(5)
        assert lookups.count(provider) == expected
        if provider == "openrouter":
            assert lookups.count("openrouter-free") == expected
        assert cfg._available_models_cache is not None
        cfg.invalidate_models_cache()
    assert len(probes) == 4


@pytest.mark.parametrize("provider", [None, "lmstudio"])
def test_revoked_truncated_worker_does_not_retry_after_abandon(
    monkeypatch, isolate_models_catalog_state, provider
):
    import urllib.request
    from tests.test_issue7481_custom_probe_budget_fairness import (
        _FakeResponse, _REAL_SAVE_MODELS_CACHE_TO_DISK,
    )

    _configure(monkeypatch, active_base_url="https://slow.example/v1")
    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(
        provider_model_ids=lambda _pid: [], list_available_providers=lambda: [],
    ))
    monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", _REAL_SAVE_MODELS_CACHE_TO_DISK)
    parked = threading.Event()
    release = threading.Event()

    class Events(_BuildDoneEvents):
        def Event(self):
            event = super().Event()
            if self.count == 1:
                wait = event.wait

                def abandoned_wait(timeout=None):
                    result = wait(timeout)
                    parked.set()
                    assert release.wait(5)
                    return result

                event.wait = abandoned_wait
            return event

    monkeypatch.setattr(cfg, "threading", Events(False))
    calls = []
    builds = []

    def probe(req, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError("slice exhausted")
        return _FakeResponse({"data": [{"id": "successor-model"}]})

    def build(builder):
        builds.append(threading.current_thread())
        return builder()

    monkeypatch.setattr(urllib.request, "urlopen", probe)
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", build)
    try:
        cfg.get_available_models()
        assert parked.wait(5)
        if provider is None:
            cfg.invalidate_models_cache()
        else:
            cfg.invalidate_provider_models_cache(provider)
        successor = cfg.get_available_models()
        durable = cfg._get_models_cache_path().read_bytes()
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "models-catalog-rebuild":
                thread.join(5)
    assert len(builds) == 2, "revoked worker rebuilt after successor completed"
    assert len(calls) == 2
    assert cfg._available_models_cache == successor
    assert cfg._get_models_cache_path().read_bytes() == durable
    assert not cfg._cache_build_in_progress


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("path", ["/api/models", "/api/onboarding/status"])
@pytest.mark.parametrize("data", [1, True, "invalid", {"id": "not-a-list"}, None, []])
def test_malformed_lmstudio_data_does_not_break_routes(
    monkeypatch, isolate_models_catalog_state, path, data, active
):
    from api import onboarding, routes

    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", SimpleNamespace(
        provider_model_ids=lambda _pid: [], list_available_providers=lambda: [],
    ))
    monkeypatch.setattr(onboarding, "verify_hermes_imports", lambda: (True, [], {}))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        _configure(monkeypatch, active_base_url=base_url if active else None,
                   provider_base_url=base_url)
        handler = _FakeRouteHandler()
        routes.handle_get(handler, urlparse("http://example.com" + path))
        assert handler.status == 200
        assert isinstance(handler.json_body(), dict)
        assert requests == ["/v1/models"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
