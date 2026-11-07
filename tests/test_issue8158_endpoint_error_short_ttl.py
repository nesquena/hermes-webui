"""#8158: a probe diagnostic must not inherit the 24h catalog TTL.

A named custom provider whose ``GET <base_url>/v1/models`` probe fails once for
a transport-level reason (DNS blip, a slow response past the 5s cap, a
throttled retry) gets ``models_endpoint_error`` attached to its picker group.
That group is deliberately RETAINED (see #7521) so the UI can explain the
failure, which means the catalog carrying it was served from cache for the full
``_AVAILABLE_MODELS_CACHE_TTL`` (24h) even after the endpoint recovered — no
invalidation axis (config.yaml mtime/size, auth.json, .env, plugin dirs) can
observe network reachability, so nothing could ever clear it.

The fix bounds the ERROR state to its own short TTL at both cache layers while
leaving a healthy catalog on the full 24h clock. The ages below are expressed
as literal seconds so each test fails on observable behaviour, not on the
absence of a named constant.

Contract: a cached probe diagnostic must not outlive ``ERROR_TTL_MAX_SECONDS``.
"""

import json
import socket
import time

import pytest

from api import config


# An error state that outlives this is still the 24h-class bug. Well below the
# 24h catalog TTL, well above any sane retry window.
ERROR_TTL_MAX_SECONDS = 300.0


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _ConfigState:
    """Pin the module-level cache globals so one test cannot leak into another."""

    _PINS = (
        "cfg",
        "_cfg_mtime",
        "_cfg_path",
        "_available_models_cache",
        "_available_models_cache_ts",
        "_available_models_cache_source_fingerprint",
        "_available_models_live_rebuild_ts",
        "_LIVE_REBUILD_BUDGET_SECONDS",
    )

    def __enter__(self):
        self._saved = {name: getattr(config, name) for name in self._PINS}
        config._cfg_mtime = 0.0
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._available_models_cache_source_fingerprint = None
        config._available_models_live_rebuild_ts = 0.0
        # These tests pin the error-state lifetime, not the async
        # provider-catalog budget fallback: force the synchronous rebuild path so
        # a slower shard cannot fall onto the global budget fallback and mask the
        # endpoint-specific contract under test.
        config._LIVE_REBUILD_BUDGET_SECONDS = 0.0
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, value in self._saved.items():
            setattr(config, name, value)
        return False


def _configure_named_custom_provider(tmp_path, monkeypatch, *, base_url):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    config.cfg = {
        "model": {"provider": "openai-codex", "default": "gpt-5.5"},
        "providers": {},
        "fallback_providers": [],
        "custom_providers": [
            {
                "name": "amd-radeon",
                "base_url": base_url,
                "api_key": "rad-key",
                "model": "amd-radeon/manual",
            }
        ],
    }


def _install_flaky_endpoint(monkeypatch):
    """Return a mutable control: ``reachable`` toggles the probe outcome."""
    control = {"reachable": False, "calls": 0}

    def fake_urlopen(req, timeout=10):
        control["calls"] += 1
        if not control["reachable"]:
            # No HTTP status code -> the "verify base_url" network diagnostic,
            # exactly the transport-level shape #8158 reports.
            raise socket.gaierror(-2, "Name or service not known")
        return _FakeResponse({"data": [{"id": "amd-radeon-v1"}, {"id": "amd-radeon-v2"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return control


def _groups_by_provider(data):
    return {group["provider_id"]: group for group in data["groups"]}


def _rewind_catalog_cache(seconds):
    """Age the in-memory catalog clock without touching the source fingerprint."""
    config._available_models_cache_ts -= float(seconds)


# ── the reported reproduction ───────────────────────────────────────────────


def test_transient_probe_failure_is_reprobed_after_the_error_ttl(monkeypatch, tmp_path):
    """A recovered endpoint must stop reporting "unreachable" without a rebuild."""
    control = _install_flaky_endpoint(monkeypatch)

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://radon.invalid/v1"
        )

        # Stage A: the probe fails once and the diagnostic is cached.
        failed = config.get_available_models()
        error = _groups_by_provider(failed)["custom:amd-radeon"]["models_endpoint_error"]
        assert error["kind"] == "network"
        assert "verify base_url" in error["message"]
        calls_after_failure = control["calls"]
        assert calls_after_failure >= 1

        # The endpoint recovers. No config/.env/plugin change happens, so every
        # invalidation axis still reports the sources as identical — this is the
        # same shape as "only the network came back".
        control["reachable"] = True
        _rewind_catalog_cache(ERROR_TTL_MAX_SECONDS + 1)

        # Stage B: reading the catalog the way the picker does must re-probe and
        # drop the stale diagnostic.
        recovered = config.get_available_models()
        group = _groups_by_provider(recovered)["custom:amd-radeon"]
        assert control["calls"] > calls_after_failure, (
            "the recovered endpoint was never re-probed"
        )
        assert "models_endpoint_error" not in group
        # The group also keeps the provider's pinned ``model:`` entry, and
        # discovered ids are provider-prefixed (``@custom:<slug>:<id>``), so
        # assert the discovered suffixes rather than raw ids.
        discovered = {m["id"].rsplit(":", 1)[-1] for m in group["models"]}
        assert {"amd-radeon-v1", "amd-radeon-v2"} <= discovered


def test_error_state_is_served_inside_its_own_ttl(monkeypatch, tmp_path):
    """The short TTL is a retry window, not a rebuild on every request."""
    control = _install_flaky_endpoint(monkeypatch)

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://radon.invalid/v1"
        )
        config.get_available_models()
        calls_after_failure = control["calls"]

        # Barely aged: the diagnostic is still inside its retry window, so it is
        # served from cache and no new probe is issued.
        _rewind_catalog_cache(1.0)
        still_errored = config.get_available_models()
        assert _groups_by_provider(still_errored)["custom:amd-radeon"][
            "models_endpoint_error"
        ]["kind"] == "network"
        assert control["calls"] == calls_after_failure


def test_healthy_catalog_keeps_the_full_ttl(monkeypatch, tmp_path):
    """A catalog with no diagnostic must NOT be re-probed on the error clock."""
    control = _install_flaky_endpoint(monkeypatch)
    control["reachable"] = True

    with _ConfigState():
        _configure_named_custom_provider(
            tmp_path, monkeypatch, base_url="https://radon.invalid/v1"
        )
        healthy = config.get_available_models()
        assert "models_endpoint_error" not in _groups_by_provider(healthy)["custom:amd-radeon"]
        calls_after_build = control["calls"]

        # Older than the error TTL, younger than the 24h catalog TTL.
        _rewind_catalog_cache(ERROR_TTL_MAX_SECONDS + 1)
        reserved = config.get_available_models()
        assert control["calls"] == calls_after_build, (
            "a healthy catalog was rebuilt on the error-state clock"
        )
        assert "models_endpoint_error" not in _groups_by_provider(reserved)["custom:amd-radeon"]


# ── the durable layer: a restart must not resurrect a stale diagnostic ───────


def _write_disk_cache(tmp_path, monkeypatch, *, groups, built_at):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    path = config._get_models_cache_path()
    payload = {
        "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
        "_source_fingerprint": config._models_cache_source_fingerprint(),
        "_webui_version": config._current_webui_version(),
        "_built_at": built_at,
        "active_provider": "openai-codex",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": groups,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _error_group():
    return {
        "provider_id": "custom:amd-radeon",
        "provider_name": "amd-radeon",
        "models": [],
        "models_endpoint_error": {
            "kind": "network",
            "code": None,
            "message": "Models endpoint unreachable for amd-radeon; verify base_url.",
        },
    }


def _healthy_group():
    return {
        "provider_id": "custom:amd-radeon",
        "provider_name": "amd-radeon",
        "models": [{"id": "amd-radeon-v1", "label": "amd-radeon-v1"}],
    }


def test_disk_cache_with_stale_diagnostic_is_rejected(tmp_path, monkeypatch):
    """An errored catalog past its short TTL is rebuilt, not restored from disk."""
    with _ConfigState():
        _write_disk_cache(
            tmp_path,
            monkeypatch,
            groups=[_error_group()],
            built_at=time.time() - (ERROR_TTL_MAX_SECONDS + 60),
        )
        assert config._load_models_cache_from_disk() is None


def test_disk_cache_with_fresh_diagnostic_is_still_served(tmp_path, monkeypatch):
    """Inside the retry window the disk snapshot stays loadable — no probe storm."""
    with _ConfigState():
        _write_disk_cache(
            tmp_path, monkeypatch, groups=[_error_group()], built_at=time.time()
        )
        loaded = config._load_models_cache_from_disk()
        assert loaded is not None
        assert (
            _groups_by_provider(loaded)["custom:amd-radeon"]["models_endpoint_error"]["kind"]
            == "network"
        )


def test_disk_cache_without_diagnostic_is_never_age_limited(tmp_path, monkeypatch):
    """The short TTL is scoped to catalogs that actually carry a diagnostic."""
    with _ConfigState():
        _write_disk_cache(
            tmp_path,
            monkeypatch,
            groups=[_healthy_group()],
            built_at=time.time() - (ERROR_TTL_MAX_SECONDS + 60),
        )
        loaded = config._load_models_cache_from_disk()
        assert loaded is not None
        assert [m["id"] for m in _groups_by_provider(loaded)["custom:amd-radeon"]["models"]] == [
            "amd-radeon-v1"
        ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))