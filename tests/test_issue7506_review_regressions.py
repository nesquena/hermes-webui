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
    _FakeClock,
    _models_by_provider,
    _REAL_SAVE_MODELS_CACHE_TO_DISK,
    isolate_models_catalog_state as _catalog_fixture,
)


isolate_models_catalog_state = _catalog_fixture
_REAL_GETADDRINFO = socket.getaddrinfo

# Real-time backstop for the virtual-time harness below. Only a thread that is
# really stuck waits this long; every wait in a passing run returns in ms.
_REAL_SAFETY_SECONDS = 30.0


class _LatencyClock(_FakeClock):
    """``time`` as ``api.config`` sees it: ``monotonic()`` moves only by endpoint latency.

    The rebuild window, each probe's fair-share slice and the foreground wait are
    all measured on ``cfg.time.monotonic()``. With a real ``time.sleep`` in the
    endpoint, everything else the rebuild does (provider discovery, starting the
    worker and probe threads, the loopback round trip, catalog assembly, and any
    GC pause or CPU starvation on a loaded host) was charged to the same real
    window. That left about 1.1 s of slack in a lone endpoint's 3.6 s slice,
    which a full-suite run could use up. Here only the endpoint's latency moves
    the clock, so "answers 2.5 s into the 4 s window" holds exactly on any host.
    """

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def advance(self, seconds):
        with self._lock:
            self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class _VirtualTimeThreading:
    """``threading`` as ``api.config`` sees it, with timed Event waits on the virtual clock.

    A timed ``wait`` blocks in real time until the event is set, then returns
    whether it was set within ``timeout`` virtual seconds of the wait starting.
    A wait that expires on the virtual clock is recorded in ``expired``.
    Because the wait blocks, the event is already set when an expired wait
    returns, so code that re-checks ``is_set()`` could still take the
    in-time branch; tests must assert that no timed wait expired (and none
    got stuck), not rely on the returned catalog alone.
    Threads, locks and untimed waits stay real.
    """

    def __init__(self, clock):
        self.timed_waits = 0
        self.stuck = []
        self.expired = []
        harness = self

        class Event:
            def __init__(self):
                self._real = threading.Event()
                self._set_at = None

            def is_set(self):
                return self._real.is_set()

            def set(self):
                if self._set_at is None:
                    self._set_at = clock.now
                self._real.set()

            def clear(self):
                self._set_at = None
                self._real.clear()

            def wait(self, timeout=None):
                if timeout is None:
                    return self._real.wait()
                harness.timed_waits += 1
                started = clock.now
                if not self._real.wait(_REAL_SAFETY_SECONDS):
                    harness.stuck.append(timeout)
                    return False
                in_time = self._set_at <= started + timeout
                if not in_time:
                    harness.expired.append((started, timeout, self._set_at))
                return in_time

        self.Event = Event

    def __getattr__(self, name):
        return getattr(threading, name)


@pytest.mark.parametrize("fast_active,delay", [(False, 2.5), (True, 1.5), ("alone", 2.5)])
def test_real_slow_healthy_endpoint_is_listed_and_cached(
    monkeypatch, isolate_models_catalog_state, fast_active, delay
):
    """A healthy endpoint answering ``delay`` s into the 4 s window is listed and cached.

    The window (``_LIVE_REBUILD_BUDGET_SECONDS`` from ``_configure``) and the 5 s
    per-endpoint cap are the values the product sees, and the HTTP server, the
    urllib round trip and the threads are real. Only time is virtual: ``delay`` is
    the endpoint's latency on ``_LatencyClock``. 2.5 s is more than half the
    window, so it fits only if the lone endpoint (``False``: a named provider;
    ``"alone"``: the active endpoint, which the LM Studio group reuses) keeps most
    of the window for its wait and its HTTP attempt. ``True`` puts a 1.5 s
    endpoint behind a fast active one.
    """
    import socket
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # Undo the shared fixture's DNS stub (back to the conftest-level getaddrinfo, which still resolves
    # IP literals and localhost; this test only uses 127.0.0.1).
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
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
    clock = _LatencyClock()
    events = _VirtualTimeThreading(clock)
    real_urlopen = urllib.request.urlopen

    def urlopen_with_latency(url, *args, **kwargs):
        full_url = getattr(url, "full_url", str(url))
        if not full_url.startswith(base):
            return real_urlopen(url, *args, **kwargs)
        timeout = kwargs.pop("timeout", None)
        latency = delay if "/slow/" in full_url else 0.0
        # The real request reaches the real server; its socket gets a generous
        # real timeout because the attempt's own timeout is applied virtually.
        response = real_urlopen(url, *args, timeout=_REAL_SAFETY_SECONDS, **kwargs)
        if timeout is not None and latency > timeout:
            # The endpoint outlasts this attempt, so a real read would time out.
            response.close()
            clock.advance(timeout)
            raise TimeoutError("timed out")
        clock.advance(latency)
        return response

    try:
        _configure(monkeypatch, active_base_url=base + "/fast/v1" if fast_active else None,
                   custom_providers=[{"name": "Gateway", "base_url": base + "/slow/v1"}])
        if fast_active == "alone":
            cfg.cfg["model"]["base_url"] = base + "/slow/v1"
            cfg.cfg["custom_providers"] = []
        monkeypatch.setattr(cfg, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0)
        monkeypatch.setattr(cfg, "time", clock)
        monkeypatch.setattr(cfg, "threading", events)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen_with_latency)
        catalogs = (cfg.get_available_models(), cfg.get_available_models())
        assert not events.stuck, events.stuck
        assert not events.expired, events.expired
        for catalog in catalogs:
            provider = "lmstudio" if fast_active == "alone" else "custom:gateway"
            assert "healthy-model" in _models_by_provider(catalog).get(provider, [])
            assert "unreachable" not in json.dumps(catalog)
        assert requests.count("/slow/v1/models") == 1
        # The harness engaged: the product's waits were decided on the virtual
        # clock, which moved by the endpoint latency once (the second call was
        # served from cache), and no wait fell back to the real-time backstop.
        assert events.timed_waits > 0
        assert not events.stuck, events.stuck
        assert clock.now == pytest.approx(delay)
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
