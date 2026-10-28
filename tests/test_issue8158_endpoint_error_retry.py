"""Regression tests for #8158: retained ``models_endpoint_error`` retry lifetime.

A named custom provider whose ``/v1/models`` probe fails once gets the error
written into the model catalog, and pre-fix that error was served from the
24h catalog cache until some unrelated invalidation axis moved. These tests
pin the bounded background re-probe: warm reads schedule at most one retry
per (profile, provider), never block on it, a successful retry clears the
group's error in the otherwise-warm catalog, failures respect a short retry
TTL, and a probe that finishes after the catalog generation advanced is
discarded.
"""

import copy
import io
import json
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from urllib.parse import urlparse

from api import config


_ERROR = {
    "kind": "network",
    "code": None,
    "message": "Models endpoint unreachable for broken-proxy; verify base_url.",
}

_CUSTOM_PROVIDERS = [
    {
        "name": "Broken Proxy",
        "base_url": "https://broken.example/v1",
        "api_key": "bad-key",
    },
    {
        "name": "Other Gateway",
        "base_url": "https://other.example/v1",
        "api_key": "other-key",
    },
]


class _CatalogState:
    """Save/restore the catalog globals these tests seed directly."""

    def __enter__(self):
        self._saved = {
            "cfg": config.cfg,
            "cfg_mtime": config._cfg_mtime,
            "cfg_path": config._cfg_path,
            "cache": config._available_models_cache,
            "cache_ts": config._available_models_cache_ts,
            "live_ts": config._available_models_live_rebuild_ts,
            "cache_fp": config._available_models_cache_source_fingerprint,
            "budget": config._LIVE_REBUILD_BUDGET_SECONDS,
            "seq": config._models_rebuild_seq,
            "building": config._cache_build_in_progress,
        }
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._available_models_live_rebuild_ts = 0.0
        config._available_models_cache_source_fingerprint = None
        self._clear_retry_state()
        return self

    def __exit__(self, exc_type, exc, tb):
        saved = self._saved
        config.cfg = saved["cfg"]
        config._cfg_mtime = saved["cfg_mtime"]
        config._cfg_path = saved["cfg_path"]
        config._available_models_cache = saved["cache"]
        config._available_models_cache_ts = saved["cache_ts"]
        config._available_models_live_rebuild_ts = saved["live_ts"]
        config._available_models_cache_source_fingerprint = saved["cache_fp"]
        config._LIVE_REBUILD_BUDGET_SECONDS = saved["budget"]
        config._models_rebuild_seq = saved["seq"]
        config._cache_build_in_progress = saved["building"]
        self._clear_retry_state()
        return False

    @staticmethod
    def _clear_retry_state():
        nxt = getattr(config, "_models_endpoint_error_retry_next", None)
        if isinstance(nxt, dict):
            nxt.clear()
        inflight = getattr(config, "_models_endpoint_error_retry_inflight", None)
        if isinstance(inflight, set):
            inflight.clear()


def _seed_errored_catalog(*, extra_groups=()):
    """Publish a warm catalog whose broken-proxy group carries the error."""
    config.cfg = {
        "model": {"provider": "openai-codex", "default": "gpt-5.5"},
        "providers": {},
        "fallback_providers": [],
        "custom_providers": copy.deepcopy(_CUSTOM_PROVIDERS),
    }
    try:
        config._cfg_mtime = config._get_config_path().stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    groups = [
        {
            "provider": "Broken Proxy",
            "provider_id": "custom:broken-proxy",
            "models": [],
            "models_endpoint_error": dict(_ERROR),
        },
        {
            "provider": "Other Gateway",
            "provider_id": "custom:other-gateway",
            "models": [{"id": "@custom:other-gateway:other-a", "label": "Other A"}],
        },
        *extra_groups,
    ]
    catalog = {
        "active_provider": "openai-codex",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": groups,
        "aliases": {},
    }
    config._available_models_cache = copy.deepcopy(catalog)
    config._available_models_cache_ts = time.monotonic()
    config._available_models_cache_source_fingerprint = (
        config._models_cache_source_fingerprint()
    )
    return catalog


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _payload_response(model_ids):
    body = {"data": [{"id": mid} for mid in model_ids]}
    return _FakeResponse(json.dumps(body).encode())


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _group(catalog, provider_id):
    return next(g for g in catalog["groups"] if g["provider_id"] == provider_id)


def _cached_group(provider_id):
    cache = config._available_models_cache
    assert cache is not None
    return _group(cache, provider_id)


def _retry_inflight_empty():
    inflight = getattr(config, "_models_endpoint_error_retry_inflight", None)
    return inflight is not None and not inflight


def test_errored_group_recovers_on_background_reprobe_with_unchanged_config(monkeypatch):
    requested = []

    def fake_urlopen(req, timeout=None):
        requested.append(getattr(req, "full_url", str(req)))
        return _payload_response(["recovered-a", "recovered-b"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _CatalogState():
        _seed_errored_catalog()
        fingerprint_before = copy.deepcopy(
            config._available_models_cache_source_fingerprint
        )
        cache_ts_before = config._available_models_cache_ts

        served = config.get_available_models()
        # The warm read itself is never blocked on the probe: it still serves
        # the retained diagnostic.
        assert "models_endpoint_error" in _group(served, "custom:broken-proxy")

        assert _wait_until(
            lambda: "models_endpoint_error"
            not in _cached_group("custom:broken-proxy")
        ), "background re-probe never cleared the retained error"

        recovered = _cached_group("custom:broken-proxy")
        assert {m["id"] for m in recovered["models"]} == {
            "@custom:broken-proxy:recovered-a",
            "@custom:broken-proxy:recovered-b",
        }
        # The rest of the healthy catalog is untouched and still warm.
        assert _cached_group("custom:other-gateway")["models"] == [
            {"id": "@custom:other-gateway:other-a", "label": "Other A"}
        ]
        assert config._available_models_cache_ts == cache_ts_before
        assert (
            config._available_models_cache_source_fingerprint == fingerprint_before
        )
        assert config._models_cache_source_fingerprint() == fingerprint_before

        served_again = config.get_available_models()
        assert "models_endpoint_error" not in _group(
            served_again, "custom:broken-proxy"
        )
        assert [u for u in requested if "broken.example" in u] == [
            "https://broken.example/v1/models"
        ]


def test_concurrent_readers_coalesce_to_one_probe_and_do_not_block(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    requested = []
    requested_lock = threading.Lock()

    def fake_urlopen(req, timeout=None):
        with requested_lock:
            requested.append(getattr(req, "full_url", str(req)))
        entered.set()
        assert release.wait(timeout=10)
        return _payload_response(["recovered-a"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _CatalogState():
        _seed_errored_catalog()
        results = []
        errors = []

        def reader():
            try:
                results.append(config.get_available_models())
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not errors
        # Every reader returned while the single probe was still blocked.
        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 8
        assert entered.wait(timeout=5)
        with requested_lock:
            assert [u for u in requested if "broken.example" in u] == [
                "https://broken.example/v1/models"
            ]
        for served in results:
            assert "models_endpoint_error" in _group(
                served, "custom:broken-proxy"
            )

        release.set()
        assert _wait_until(
            lambda: "models_endpoint_error"
            not in _cached_group("custom:broken-proxy")
        )


def test_errored_provider_does_not_probe_healthy_provider(monkeypatch):
    requested = []

    def fake_urlopen(req, timeout=None):
        requested.append(getattr(req, "full_url", str(req)))
        return _payload_response(["recovered-a"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _CatalogState():
        _seed_errored_catalog()
        config.get_available_models()
        assert _wait_until(
            lambda: "models_endpoint_error"
            not in _cached_group("custom:broken-proxy")
        )
        assert requested == ["https://broken.example/v1/models"]


def test_profile_keys_coalesce_independently(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    requested = []
    requested_lock = threading.Lock()

    def fake_urlopen(req, timeout=None):
        with requested_lock:
            requested.append(getattr(req, "full_url", str(req)))
        entered.set()
        assert release.wait(timeout=10)
        return _payload_response(["recovered-a"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    import api.profiles as profiles
    from contextlib import nullcontext

    monkeypatch.setattr(
        profiles,
        "profile_scope_for_detached_worker",
        lambda name, purpose="": nullcontext(),
        raising=False,
    )
    # Coalescing-key separation only: the scheduler otherwise refuses a
    # profile whose identity is not current (see the ownership tests below).
    monkeypatch.setattr(config, "_models_build_identity_current", lambda fp, profile: True)

    with _CatalogState():
        catalog = _seed_errored_catalog()
        schedule = config._maybe_schedule_models_endpoint_error_retries
        schedule(catalog, profile="profile-alpha")
        schedule(catalog, profile="profile-alpha")
        schedule(catalog, profile="profile-beta")

        assert entered.wait(timeout=5)
        assert _wait_until(lambda: len(requested) >= 2)
        inflight = config._models_endpoint_error_retry_inflight
        assert ("profile-alpha", "custom:broken-proxy") in inflight
        assert ("profile-beta", "custom:broken-proxy") in inflight
        with requested_lock:
            assert len([u for u in requested if "broken.example" in u]) == 2
        release.set()
        assert _wait_until(_retry_inflight_empty)


def test_stale_probe_result_is_discarded_after_invalidation(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def fake_urlopen(req, timeout=None):
        entered.set()
        assert release.wait(timeout=10)
        return _payload_response(["recovered-a"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _CatalogState():
        _seed_errored_catalog()
        config.get_available_models()
        assert entered.wait(timeout=5)

        # The catalog generation advances and a newer catalog is published
        # while the probe is still in flight.
        config.invalidate_models_cache(delete_disk=False)
        _seed_errored_catalog(
            extra_groups=[],
        )
        _cached_group("custom:broken-proxy")["models"] = [
            {"id": "@custom:broken-proxy:current", "label": "Current"}
        ]

        release.set()
        assert _wait_until(_retry_inflight_empty)

        group = _cached_group("custom:broken-proxy")
        assert "models_endpoint_error" in group
        assert {m["id"] for m in group["models"]} == {
            "@custom:broken-proxy:current"
        }


def test_failed_reprobe_respects_retry_ttl(monkeypatch):
    requested = []

    def fake_urlopen(req, timeout=None):
        requested.append(getattr(req, "full_url", str(req)))
        raise urllib.error.URLError("still down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _CatalogState():
        _seed_errored_catalog()
        config.get_available_models()
        assert _wait_until(_retry_inflight_empty)
        assert len([u for u in requested if "broken.example" in u]) == 1

        # A still-down endpoint is not hammered on every warm read.
        config.get_available_models()
        config.get_available_models()
        assert len([u for u in requested if "broken.example" in u]) == 1
        assert "models_endpoint_error" in _cached_group("custom:broken-proxy")

        # Once the bounded retry lifetime expires, the next read retries.
        retry_next = config._models_endpoint_error_retry_next
        assert len(retry_next) == 1
        key = next(iter(retry_next))
        retry_next[key] = 0.0
        config.get_available_models()
        assert _wait_until(
            lambda: len([u for u in requested if "broken.example" in u]) == 2
        )
        assert _wait_until(_retry_inflight_empty)


def _install_provider_model_ids(monkeypatch, fn):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = fn
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _run_live_models(monkeypatch, *, fail, during_request=None):
    import api.profiles as profiles
    import api.routes as routes

    routes._clear_live_models_cache()
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload
    )
    monkeypatch.setattr(config, "_resolve_provider_alias", lambda provider: provider)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    config_data = {
        "model": {"provider": "custom:test-gateway"},
        "custom_providers": [
            {
                "name": "Test Gateway",
                "base_url": "https://gateway.example/v1",
                "api_key": "sk-test",
            }
        ],
    }
    monkeypatch.setattr(config, "get_config", lambda: config_data)
    monkeypatch.setattr(config, "cfg", config_data)
    _install_provider_model_ids(monkeypatch, lambda provider: [])

    def fake_urlopen(req, timeout=None):
        if fail:
            raise OSError("probe failed")
        if during_request is not None:
            during_request(config_data)
        return _payload_response(["chat-a", "chat-b"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    config._available_models_cache = {
        "active_provider": "openai-codex",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "Test Gateway",
                "provider_id": "custom:test-gateway",
                "models": [],
                "models_endpoint_error": dict(_ERROR),
            }
        ],
        "aliases": {},
    }
    config._available_models_cache_ts = time.monotonic()
    config._available_models_cache_source_fingerprint = (
        config._models_cache_source_fingerprint()
    )

    parsed = urlparse("/api/models/live?provider=custom:test-gateway")
    return routes._handle_live_models(object(), parsed)


def test_live_probe_success_clears_cached_endpoint_error(monkeypatch):
    with _CatalogState():
        config.cfg = {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "providers": {},
            "fallback_providers": [],
            "custom_providers": [],
        }
        payload = _run_live_models(monkeypatch, fail=False)

        assert [m["id"] for m in payload["models"]] == ["chat-a", "chat-b"]
        group = _cached_group("custom:test-gateway")
    assert "models_endpoint_error" not in group
    assert {m["id"] for m in group["models"]} == {
        "@custom:test-gateway:chat-a",
        "@custom:test-gateway:chat-b",
    }


def test_live_probe_failure_keeps_cached_endpoint_error(monkeypatch):
    with _CatalogState():
        config.cfg = {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "providers": {},
            "fallback_providers": [],
            "custom_providers": [],
        }
        _run_live_models(monkeypatch, fail=True)
        group = _cached_group("custom:test-gateway")
    assert "models_endpoint_error" in group
    assert group["models"] == []


def test_retry_uses_scheduling_profile_config_not_a_later_global_load(monkeypatch):
    """A's worker paused; B's config (same slug, other endpoint/key) loads; A resumes."""
    import api.profiles as profiles
    from contextlib import contextmanager

    entered = threading.Event()
    release = threading.Event()
    requests = []

    @contextmanager
    def paused_scope(name, purpose=""):
        entered.set()  # worker started, has not read any provider config yet
        assert release.wait(timeout=10)
        yield

    monkeypatch.setattr(profiles, "profile_scope_for_detached_worker", paused_scope, raising=False)

    def fake_urlopen(req, timeout=None):
        requests.append((req.full_url, req.get_header("Authorization")))
        return _payload_response(["a-model"])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with _CatalogState():
        _seed_errored_catalog()
        config.get_available_models()
        assert entered.wait(timeout=5)
        config.cfg = copy.deepcopy(config.cfg)
        config.cfg["custom_providers"][0].update(
            base_url="https://profile-b.example/v1", api_key="b-key"
        )
        release.set()
        assert _wait_until(_retry_inflight_empty)

        assert requests == [("https://broken.example/v1/models", "Bearer bad-key")]
        recovered = _cached_group("custom:broken-proxy")
    assert "models_endpoint_error" not in recovered
    assert [m["id"] for m in recovered["models"]] == ["@custom:broken-proxy:a-model"]


def test_recovery_applies_serialized_allowlist(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _payload_response(
            ["chat-a", "image-unwanted", "audio-unwanted"]
        ),
    )
    with _CatalogState():
        _seed_errored_catalog()
        config.cfg["custom_providers"][0]["models"] = '["chat-a"]'
        config.get_available_models()
        assert _wait_until(
            lambda: "models_endpoint_error" not in _cached_group("custom:broken-proxy")
        )
        assert [m["id"] for m in _cached_group("custom:broken-proxy")["models"]] == [
            "@custom:broken-proxy:chat-a"
        ]


def _publish_successor_catalog(_config_data):
    config._models_rebuild_seq += 1
    config._available_models_cache = copy.deepcopy(config._available_models_cache)
    group = config._available_models_cache["groups"][0]
    group["models"] = [{"id": "@custom:test-gateway:new-current", "label": "new-current"}]


def _repoint_endpoint(config_data):
    config_data["custom_providers"][0]["base_url"] = "https://new-gateway.example/v1"


import pytest  # noqa: E402


@pytest.mark.parametrize("change", [_publish_successor_catalog, _repoint_endpoint])
def test_old_live_response_does_not_patch_a_successor_catalog(monkeypatch, change):
    with _CatalogState():
        config.cfg = {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "providers": {},
            "fallback_providers": [],
            "custom_providers": [],
        }
        _run_live_models(monkeypatch, fail=False, during_request=change)
        group = _cached_group("custom:test-gateway")
    assert "models_endpoint_error" in group
    assert not any("chat-a" in m["id"] for m in group["models"])


def test_retry_entry_is_copied_atomically_from_the_shared_config_cache(monkeypatch, tmp_path):
    """A reads the shared cache, B's real loader reloads it in place, then A copies."""
    import api.profiles as profiles

    b_config = tmp_path / "profile-b.yaml"
    b_config.write_text(
        "custom_providers:\n"
        "  - name: Broken Proxy\n"
        "    base_url: https://owner-b.example/v1\n"
        "    api_key: owner-b-key\n"
    )
    requests = []

    def fake_urlopen(req, timeout=None):
        requests.append((req.full_url, req.get_header("Authorization")))
        return _payload_response(["a-model"])

    real_entries = config._custom_provider_entries

    def entries_after_foreign_reload(config_data):
        def reload_b():
            with config._cfg_lock:
                config._refresh_config_cache(b_config)

        loader = threading.Thread(target=reload_b)
        loader.start()
        loader.join(timeout=0.5)
        return real_entries(config_data)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(profiles, "profile_scope_for_detached_worker", lambda *a, **k: _NullScope(), raising=False)
    saved_cache = copy.deepcopy(config._cfg_cache)
    saved_fingerprint = config._cfg_fingerprint
    try:
        with _CatalogState():
            seeded = _seed_errored_catalog()
            a_cfg = config.cfg
            config._cfg_cache.clear()
            config._cfg_cache.update(a_cfg)
            config.cfg = config._cfg_cache
            config._available_models_cache_source_fingerprint = (
                config._models_cache_source_fingerprint()
            )
            monkeypatch.setattr(config, "_custom_provider_entries", entries_after_foreign_reload)
            monkeypatch.setattr(config, "_models_build_identity_current", lambda fp, profile: True)
            config._maybe_schedule_models_endpoint_error_retries(seeded, profile="default")
            assert _wait_until(_retry_inflight_empty)
    finally:
        with config._cfg_lock:
            config._cfg_cache.clear()
            config._cfg_cache.update(saved_cache)
            config._cfg_fingerprint = saved_fingerprint
    assert requests == [("https://broken.example/v1/models", "Bearer bad-key")]


class _NullScope:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve_session_visit(monkeypatch):
    monkeypatch.setattr(config, "_models_cache_file_age_seconds", lambda *a: 0.0)
    return config.get_available_models_for_session_visit()


@pytest.mark.parametrize(
    "serve",
    [lambda _mp: config.get_available_models(), _serve_session_visit],
    ids=["public-warm", "session-visit"],
)
def test_retry_scheduling_resolves_config_outside_the_catalog_lock(monkeypatch, serve):
    owned = []
    real_entries = config._custom_provider_entries

    def recording_entries(config_data):
        owned.append(config._available_models_cache_lock._is_owned())
        return real_entries(config_data)

    monkeypatch.setattr(config, "_custom_provider_entries", recording_entries)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _payload_response([]))
    with _CatalogState():
        _seed_errored_catalog()
        serve(monkeypatch)
        assert _wait_until(_retry_inflight_empty)
    assert owned and not any(owned)
