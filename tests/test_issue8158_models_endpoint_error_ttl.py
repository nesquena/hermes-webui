"""#8158 — o prazo do diagnóstico é do RETRY, não da validade do catálogo.

Contract under test: a catalog that carries a transient ``models_endpoint_error``
stays a *valid* catalog (24 h ruler, same owner, same source fingerprint). What
the 30-second window governs is only *when to retry the diagnostic probe* — and
while that retry is in flight, readers keep being served the catalog they
already had, so healthy discovered models never disappear from the picker.

The blocking regression these tests pin: at the reviewed head the whole catalog
was given the diagnostic's 30-second lifetime, so an expired memory slot was
dropped, the foreground rebuild missed its budget, and the response fell back to
the static catalog — losing every discovered-only healthy row.
"""

import json
import time

from api import config

_FINGERPRINT = {"config": "/tmp/config.yaml", "auth": "hash"}


def _catalog(groups):
    return {
        "active_provider": "openai",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": groups,
    }


_HEALTHY = {
    "provider_id": "custom:healthy",
    "models": [{"id": "healthy/discovered", "label": "healthy/discovered"}],
}
_BROKEN = {
    "provider_id": "custom:broken",
    "models": [],
    "models_endpoint_error": {"kind": "network", "message": "unreachable"},
}


def _reset_state(monkeypatch, tmp_path):
    """Isolate the module-level catalog state and the cache path."""
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: dict(_FINGERPRINT))
    monkeypatch.setattr(config, "_mtime_is_stale", lambda: False, raising=False)
    monkeypatch.setattr(config, "reload_config", lambda *a, **k: None, raising=False)
    config._available_models_cache = None
    config._available_models_cache_ts = 0.0
    config._available_models_cache_source_fingerprint = None
    config._available_models_live_rebuild_ts = 0.0


def _install_memory_catalog(cache, age_seconds):
    config._available_models_cache = cache
    config._available_models_cache_ts = time.monotonic() - age_seconds
    config._available_models_cache_source_fingerprint = dict(_FINGERPRINT)


def _groups_by_provider(data):
    return {group["provider_id"]: group for group in data["groups"]}


def test_has_models_endpoint_error_detection():
    assert not config._has_models_endpoint_error(None)
    assert not config._has_models_endpoint_error({})
    assert not config._has_models_endpoint_error({"groups": []})
    assert not config._has_models_endpoint_error(_catalog([{"provider_id": "openai", "models": []}]))
    assert config._has_models_endpoint_error(_catalog([_HEALTHY, _BROKEN]))


def test_memory_catalog_with_diagnostic_is_still_valid(monkeypatch, tmp_path):
    """The catalog keeps the 24 h ruler; only the retry is short-lived."""
    _reset_state(monkeypatch, tmp_path)
    _install_memory_catalog(_catalog([_HEALTHY, _BROKEN]), age_seconds=3600.0)

    served = config._get_fresh_memory_models_cache(time.monotonic())

    assert served is not None, "a catalog with a transient diagnostic is still a valid catalog"
    assert [m["id"] for m in _groups_by_provider(served)["custom:healthy"]["models"]] == [
        "healthy/discovered"
    ]


def test_disk_catalog_with_old_diagnostic_still_loads(monkeypatch, tmp_path):
    """The durable snapshot is not discarded just because its diagnostic aged out."""
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_is_loadable_disk_cache", lambda cache: True)
    cache_path = tmp_path / "models_cache.json"

    payload = _catalog([_HEALTHY, _BROKEN])
    payload["_built_at"] = time.time() - 35.0
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = config._load_models_cache_from_disk()

    assert loaded is not None, "an aged diagnostic must not hide a loadable catalog"
    assert "custom:healthy" in _groups_by_provider(loaded)
    assert config._has_models_endpoint_error(loaded)


def test_diagnostic_retry_becomes_due_after_the_short_window():
    fresh = _catalog([_HEALTHY, _BROKEN])
    healthy_only = _catalog([_HEALTHY])

    assert not config._models_diagnostic_retry_due(fresh, 10.0)
    assert config._models_diagnostic_retry_due(fresh, 35.0)
    assert not config._models_diagnostic_retry_due(healthy_only, 35.0)
    assert not config._models_diagnostic_retry_due(None, 35.0)
    assert not config._models_diagnostic_retry_due(fresh, None)


def test_slow_retry_keeps_serving_healthy_discovered_rows(monkeypatch, tmp_path):
    """Blocking regression: the picker must not lose Healthy while Broken retries."""
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.08, raising=False)
    tentativas = {"n": 0}

    def _rebuild_lento(builder):
        tentativas["n"] += 1
        time.sleep(0.6)  # past the foreground budget, like a hung diagnostic probe
        return _catalog([{"provider_id": "openai", "models": []}])

    monkeypatch.setattr(config, "_invoke_models_rebuild", _rebuild_lento)
    _install_memory_catalog(_catalog([_HEALTHY, _BROKEN]), age_seconds=35.0)

    data = config.get_available_models()

    grupos = _groups_by_provider(data)
    assert "custom:healthy" in grupos, "healthy rows must survive the retry"
    assert [m["id"] for m in grupos["custom:healthy"]["models"]] == ["healthy/discovered"]
    assert grupos["custom:broken"]["models_endpoint_error"]["kind"] == "network", (
        "the useful error group must be preserved until it is replaced"
    )
    assert tentativas["n"] == 1, "the aged diagnostic must still trigger exactly one retry"


def test_prefer_cache_reader_keeps_catalog_while_retrying(monkeypatch, tmp_path):
    """Wakeup turns (prefer_cache) also keep the catalog instead of the minimal one."""
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.08, raising=False)

    def _nao_deveria_rodar(builder):  # pragma: no cover - guard
        raise AssertionError("prefer_cache must never launch a live rebuild")

    monkeypatch.setattr(config, "_invoke_models_rebuild", _nao_deveria_rodar)
    _install_memory_catalog(_catalog([_HEALTHY, _BROKEN]), age_seconds=35.0)

    data = config.get_available_models(prefer_cache=True)

    grupos = _groups_by_provider(data)
    assert "custom:healthy" in grupos
    assert [m["id"] for m in grupos["custom:healthy"]["models"]] == ["healthy/discovered"]


def test_healthy_catalog_does_not_retry(monkeypatch, tmp_path):
    """No diagnostic, no retry: the 24 h catalog is served from memory as before."""
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.08, raising=False)

    def _nao_deveria_rodar(builder):  # pragma: no cover - guard
        raise AssertionError("a healthy catalog must not trigger a rebuild")

    monkeypatch.setattr(config, "_invoke_models_rebuild", _nao_deveria_rodar)
    _install_memory_catalog(_catalog([_HEALTHY]), age_seconds=3600.0)

    data = config.get_available_models()

    assert [m["id"] for m in _groups_by_provider(data)["custom:healthy"]["models"]] == [
        "healthy/discovered"
    ]


class _ImediatoCondicao:
    """Condition stand-in that never blocks: a concurrent reader that gives up."""

    def wait_for(self, predicate, timeout=None):  # noqa: D401 - test double
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def notify_all(self):
        return None


def test_concurrent_reader_gets_preserved_catalog(monkeypatch, tmp_path):
    """A reader that coalesced behind the retry must not fall back to the static catalog."""
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.08, raising=False)
    monkeypatch.setattr(config, "_cache_build_cv", _ImediatoCondicao())
    _install_memory_catalog(_catalog([_HEALTHY, _BROKEN]), age_seconds=35.0)
    monkeypatch.setattr(config, "_cache_build_in_progress", True, raising=False)

    data = config.get_available_models()

    grupos = _groups_by_provider(data)
    assert "custom:healthy" in grupos
    assert [m["id"] for m in grupos["custom:healthy"]["models"]] == ["healthy/discovered"]


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_partial_rebuild_preserves_healthy_discovered_models(monkeypatch, tmp_path):
    """#8478 regression: partial rebuild must not drop healthy models from the picker.

    Reviewer scenario:
    1. via get_available_models(), discover a model from the Healthy endpoint.
    2. Persist and reload catalog via strict disk loader (_load_models_cache_from_disk).
    3. Keep config/source equal and age memory catalog by 35 s.
    4. On retry, let Broken recover fast and delay HTTP response of Healthy,
       with foreground budget of 0.5 s.
    5. The partial result must not lose the Healthy model.
    """
    import socket
    import sys
    from types import SimpleNamespace
    import urllib.error
    import urllib.request

    _reset_state(monkeypatch, tmp_path)
    config._cache_build_in_progress = False
    monkeypatch.setattr(config, "_read_live_provider_model_ids", lambda _pid: [])
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.models",
        SimpleNamespace(
            provider_model_ids=lambda _pid: [],
            list_available_providers=lambda: [],
        ),
    )

    def _unresolvable(*args, **kwargs):
        raise socket.gaierror("hermetic test resolver")

    monkeypatch.setattr(socket, "getaddrinfo", _unresolvable)

    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openai", "default": "gpt-5.5"},
            "providers": {},
            "fallback_providers": [],
            "custom_providers": [
                {"name": "Healthy", "base_url": "http://healthy.local/v1"},
                {"name": "Broken", "base_url": "http://broken.local/v1"},
            ],
        },
    )

    # 1. First pass: Healthy discovers a model, Broken fails
    def _urlopen_phase1(req, timeout=None):
        url = str(getattr(req, "full_url", req))
        if "healthy.local" in url:
            return _FakeResponse({"data": [{"id": "healthy/discovered"}]})
        if "broken.local" in url:
            raise urllib.error.URLError("unreachable")
        raise AssertionError(f"Unexpected probe URL: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_phase1)

    initial_catalog = config.get_available_models()
    initial_groups = _groups_by_provider(initial_catalog)
    assert "custom:healthy" in initial_groups
    assert [m["id"] for m in initial_groups["custom:healthy"]["models"]] == [
        "@custom:healthy:healthy/discovered"
    ]
    assert "custom:broken" in initial_groups
    assert initial_groups["custom:broken"].get("models_endpoint_error") is not None

    # 2. Persist and reload the catalog via the strict disk loader
    loaded = config._load_models_cache_from_disk()
    assert loaded is not None, "strict disk loader must reload the persisted catalog"
    loaded_groups = _groups_by_provider(loaded)
    assert "custom:healthy" in loaded_groups
    assert [m["id"] for m in loaded_groups["custom:healthy"]["models"]] == [
        "@custom:healthy:healthy/discovered"
    ]
    assert config._has_models_endpoint_error(loaded)

    # 3. Keep config/source identical and age the memory catalog by 35 s
    _install_memory_catalog(loaded, age_seconds=35.0)

    # 4. On retry: Broken recovers fast, Healthy delays HTTP response past foreground budget (0.5 s)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(config, "CUSTOM_MODELS_ENDPOINT_TIMEOUT_SECONDS", 5.0, raising=False)

    def _urlopen_phase2(req, timeout=None):
        url = str(getattr(req, "full_url", req))
        if "broken.local" in url:
            return _FakeResponse({"data": [{"id": "broken/discovered"}]})
        if "healthy.local" in url:
            time.sleep(0.8)
            return _FakeResponse({"data": [{"id": "healthy/discovered"}]})
        raise AssertionError(f"Unexpected probe URL: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_phase2)

    retry_data = config.get_available_models()
    retry_groups = _groups_by_provider(retry_data)

    # 5. The partial result must not lose the Healthy model
    assert "custom:healthy" in retry_groups, "healthy provider must survive partial rebuild"
    assert [m["id"] for m in retry_groups["custom:healthy"]["models"]] == [
        "@custom:healthy:healthy/discovered"
    ], "healthy model must be preserved in partial return"
    assert "custom:broken" in retry_groups
    assert [m["id"] for m in retry_groups["custom:broken"]["models"]] == [
        "@custom:broken:broken/discovered"
    ], "recovered provider should be updated from completed partial groups"

