"""Ownership regressions for endpoint diagnostic retries and live recovery."""
import copy
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import pytest

from api import config, profiles, routes
from tests.test_issue3718_live_models_custom_probe import _install_provider_model_ids

_PROVIDER = "custom:shared"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _catalog(label):
    error = config._custom_endpoint_error(label)
    error["checked_at"] = time.time() - 3600
    return {
        "active_provider": "openai-codex",
        "default_model": label,
        "configured_model_badges": {},
        "groups": [{"provider": "Shared", "provider_id": _PROVIDER, "models": [],
                    "models_endpoint_error": error}],
    }


def _publish(label):
    with config._cache_build_cv:
        config._available_models_cache = _catalog(label)
        config._available_models_cache_ts = time.monotonic()
        config._available_models_cache_source_fingerprint = config._models_cache_source_fingerprint()
        config._sync_models_cache_provenance()


def _error():
    return config._available_models_cache["groups"][0].get("models_endpoint_error")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for name in ("default", "alpha", "beta"):
        directory = home if name == "default" else home / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text(
            "model:\n  provider: openai-codex\n  default: gpt-5.5\ncustom_providers:\n"
            f"  - name: Shared\n    base_url: https://{name}.example/v1\n"
            f"    api_key: synthetic-{name}\n", encoding="utf-8",
        )
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BASE_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles._tls, "profile", "default", raising=False)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_cfg_cache", {})
    monkeypatch.setattr(config, "cfg", config._cfg_cache)
    for attr in ("_cfg_mtime", "_cfg_path", "_cfg_fingerprint", "_models_rebuild_seq",
                 "_available_models_cache", "_available_models_cache_ts",
                 "_available_models_cache_source_fingerprint", "_models_cache_provenance",
                 "_available_models_live_rebuild_ts", "_cache_build_in_progress",
                 "_advertised_model_ids_memo"):
        monkeypatch.setattr(config, attr, getattr(config, attr))
    monkeypatch.setattr(config, "_models_endpoint_reprobe_inflight", {})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [])
    monkeypatch.setattr(config, "_has_explicit_pool_credentials", lambda *_: False)

    def no_network(*_a, **_k):
        raise urllib.error.URLError("synthetic offline")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    workers = []
    original_start = threading.Thread.start

    def start(worker, *args, **kwargs):
        if worker.name == "models-endpoint-error-reprobe":
            workers.append(worker)
        return original_start(worker, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", start)
    routes._clear_live_models_cache()
    yield home, workers
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive(), "retry worker escaped the test"
    assert not config._models_endpoint_reprobe_inflight
    routes._clear_live_models_cache()


def _join(workers):
    assert workers, "a diagnostic retry must have been scheduled"
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize("credential_source", ["inline", "key_env", "interpolated"])
def test_retry_uses_its_profile_config_after_other_profile_read(isolated, monkeypatch, credential_source):
    home, workers = isolated
    if credential_source != "inline":
        for name in ("alpha", "beta"):
            directory = home / "profiles" / name
            path = directory / "config.yaml"
            replacement = "key_env: SHARED_API_KEY" if credential_source == "key_env" else "api_key: ${SHARED_API_KEY}"
            path.write_text(path.read_text(encoding="utf-8").replace(
                f"api_key: synthetic-{name}", replacement), encoding="utf-8")
            (directory / ".env").write_text(f"SHARED_API_KEY=synthetic-{name}\n", encoding="utf-8")
    profiles.set_request_profile("alpha")
    config.get_config()
    _publish("alpha")
    entered, release = threading.Event(), threading.Event()
    seen = []
    real_probe = config._reprobe_provider_endpoint

    def delayed(provider_id):
        entered.set()
        assert release.wait(5)
        return real_probe(provider_id)

    def urlopen(req, **_kwargs):
        seen.append((req.full_url, req.get_header("Authorization")))
        return _Response(b'{"data": [{"id": "alpha-only"}]}')

    monkeypatch.setattr(config, "_reprobe_provider_endpoint", delayed)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    config._schedule_models_endpoint_error_reprobe(_PROVIDER)
    try:
        assert entered.wait(5)
        profiles.set_request_profile("beta")
        assert config.get_config()["custom_providers"][0]["base_url"] == "https://beta.example/v1"
    finally:
        release.set()
        _join(workers)
    assert seen == [("https://alpha.example/v1/models", "Bearer synthetic-alpha")]
    assert _error() is None


def test_retry_cannot_modify_other_profile_disk_snapshot(isolated, monkeypatch):
    _, workers = isolated
    profiles.set_request_profile("beta")
    config.get_config()
    config._save_models_cache_to_disk(_catalog("beta"))
    profiles.set_request_profile("alpha")
    config.get_config()
    _publish("alpha")
    entered, release = threading.Event(), threading.Event()

    def urlopen(req, **_kwargs):
        assert req.full_url == "https://alpha.example/v1/models"
        entered.set()
        assert release.wait(5)
        return _Response(b'{"data": [{"id": "alpha-only"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    config._schedule_models_endpoint_error_reprobe(_PROVIDER)
    try:
        assert entered.wait(5)
        sequence = config._models_rebuild_seq
        profiles.set_request_profile("beta")
        config.get_config()
        config.warm_models_catalog_provenance_if_cold()
        assert config._available_models_cache["default_model"] == "beta"
        assert config._models_rebuild_seq == sequence
        before = copy.deepcopy(config._available_models_cache)
    finally:
        release.set()
        _join(workers)
    assert config._available_models_cache == before
    assert config._endpoint_advertised_model_ids(_PROVIDER) == frozenset()


def test_retry_captures_generation_before_worker_starts(isolated, monkeypatch):
    _, workers = isolated
    config.get_config()
    _publish("old")
    entered, release = threading.Event(), threading.Event()
    real_worker = config._models_endpoint_error_reprobe_worker
    probes = []

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        real_worker(*args)

    def probe(provider_id):
        probes.append(provider_id)
        return [{"id": "obsolete", "label": "obsolete"}], None

    monkeypatch.setattr(config, "_models_endpoint_error_reprobe_worker", delayed)
    monkeypatch.setattr(config, "_reprobe_provider_endpoint", probe)
    config._schedule_models_endpoint_error_reprobe(_PROVIDER)
    try:
        assert entered.wait(5)
        config.invalidate_models_cache(delete_disk=False)
        _publish("new")
        before = copy.deepcopy(config._available_models_cache)
    finally:
        release.set()
        _join(workers)
    assert probes == []
    assert config._available_models_cache == before


def _live(monkeypatch):
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    _install_provider_model_ids(monkeypatch, lambda _provider: [])
    return lambda: routes._handle_live_models(object(), urlparse("/api/models/live?provider=" + _PROVIDER))


@pytest.mark.parametrize("body", [{"data": []}, {"data": [{"id": "model"}]}, []])
def test_actual_live_success_clears_error_and_preserves_group(isolated, monkeypatch, body):
    config.get_config()
    _publish("unreachable")
    live = _live(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response(json.dumps(body).encode()))
    live()
    assert _error() is None
    assert config._available_models_cache["groups"][0]["provider_id"] == _PROVIDER


@pytest.mark.parametrize("body", [None, {}, {"data": "invalid"}, {"data": [None]}])
def test_failed_or_malformed_live_probe_keeps_diagnostic(isolated, monkeypatch, body):
    config.get_config()
    _publish("unreachable")
    before = copy.deepcopy(_error())
    live = _live(monkeypatch)
    if body is not None:
        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response(json.dumps(body).encode()))
    live()
    assert _error() == before


def test_late_live_success_does_not_clear_new_endpoint_error(isolated, monkeypatch):
    config.get_config()
    _publish("old")
    live = _live(monkeypatch)
    new_error = None

    def urlopen(req, **_kwargs):
        nonlocal new_error
        assert req.full_url == "https://default.example/v1/models"
        path = config._get_config_path()
        path.write_text(path.read_text(encoding="utf-8").replace("default.example", "replacement.example"), encoding="utf-8")
        config.invalidate_models_cache(delete_disk=False)
        config.reload_config()
        _publish("new")
        new_error = copy.deepcopy(_error())
        return _Response(b'{"data": [{"id": "old-model"}]}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert live()["count"] == 1
    assert _error() == new_error


def test_live_cache_hit_is_not_new_recovery_evidence(isolated, monkeypatch):
    config.get_config()
    live = _live(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response(b'{"data": [{"id": "model"}]}'))
    assert live()["count"] == 1
    _publish("later-failure")
    before = copy.deepcopy(_error())
    assert live()["count"] == 1
    assert _error() == before


@pytest.mark.parametrize("disk", [False, True], ids=["memory", "disk"])
def test_cache_only_read_does_not_start_retry(isolated, monkeypatch, disk):
    _, workers = isolated
    config.get_config()
    _publish("expired")
    if disk:
        config._save_models_cache_to_disk(config._available_models_cache)
        config._available_models_cache = None
        config._available_models_cache_source_fingerprint = None
        config._sync_models_cache_provenance()
    seen = []

    def probe(provider_id):
        seen.append(provider_id)
        return [], config._custom_endpoint_error(provider_id)

    monkeypatch.setattr(config, "_reprobe_provider_endpoint", probe)
    result = config.get_available_models(prefer_cache=True)
    assert result["groups"][0]["models_endpoint_error"]
    for worker in workers:
        worker.join(5)
    assert workers == []
    assert seen == []
