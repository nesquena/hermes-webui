"""Regression coverage for the models-catalog prewarm sweep and stale claim release.

The session-visit path only trusts the disk catalog while it is younger than
``_SESSION_VISIT_MODELS_FRESHNESS_SECONDS``; past that window the visitor paid a
serial per-provider rebuild on the request thread. These tests pin the sweep
that moves that rebuild off the request path, and the release of a single-flight
claim whose builder never released it.
"""

import json
import time

import pytest

import api.config as config


def _write_cache(path, built_at):
    path.write_text(
        json.dumps(
            {
                "_schema_version": config._MODELS_CACHE_SCHEMA_VERSION,
                "_built_at": built_at,
                "_source_fingerprint": {},
                "active_provider": None,
                "default_model": "",
                "configured_model_badges": {},
                "groups": [],
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def prewarm_env(tmp_path, monkeypatch):
    """Isolated cache dir, boot grace bypassed, workers captured not started."""
    base = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", base)
    monkeypatch.setattr(config, "_models_prewarm_booted_at", time.monotonic() - 10_000)
    monkeypatch.setattr(config, "_MODELS_PREWARM_LAST_STARTED", {})

    calls = []
    monkeypatch.setattr(
        config,
        "get_available_models",
        lambda **kw: calls.append(kw) or {"groups": []},
    )

    workers = []

    class _CapturedThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            workers.append((target, args))

        def start(self):
            return None

    monkeypatch.setattr(config.threading, "Thread", _CapturedThread)
    return base, calls, workers


def test_fresh_cache_is_not_prewarmed(prewarm_env):
    base, calls, workers = prewarm_env
    _write_cache(base, time.time())

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []
    assert calls == []


def test_cache_near_expiry_is_prewarmed(prewarm_env):
    base, calls, workers = prewarm_env
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(base, time.time() - due_before - 1)

    assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
    assert len(workers) == 1

    target, args = workers[0]
    target(*args)
    assert calls == [{"force_refresh": True}]


def test_scheduled_prewarm_is_backed_off(prewarm_env):
    base, _calls, workers = prewarm_env
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(base, time.time() - due_before - 1)

    assert config.maybe_prewarm_models_catalog() == {"default": "scheduled"}
    # Same tick state again: the retry backoff keeps a failing rebuild from
    # spawning a worker every minute.
    assert config.maybe_prewarm_models_catalog() == {}
    assert len(workers) == 1


def test_named_profile_cache_is_covered(prewarm_env):
    base, _calls, workers = prewarm_env
    _write_cache(base, time.time())
    named = base.parent / "models_cache.personal.json"
    due_before = (
        config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS - config._MODELS_PREWARM_LEAD_SECONDS
    )
    _write_cache(named, time.time() - due_before - 1)

    assert config.maybe_prewarm_models_catalog() == {"personal": "scheduled"}
    assert len(workers) == 1


def test_boot_grace_suppresses_prewarm(prewarm_env, monkeypatch):
    base, _calls, workers = prewarm_env
    monkeypatch.setattr(config, "_models_prewarm_booted_at", time.monotonic())
    _write_cache(base, time.time() - 10 * config._SESSION_VISIT_MODELS_FRESHNESS_SECONDS)

    assert config.maybe_prewarm_models_catalog() == {}
    assert workers == []


def test_stale_build_claim_is_released(monkeypatch):
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(
        config,
        "_cache_build_started_at",
        time.monotonic() - config._STALE_BUILD_CLAIM_SECONDS - 1,
    )

    assert config._release_stale_build_claim() is True
    assert config._cache_build_in_progress is False
    assert config._cache_build_started_at == 0.0


def test_live_build_claim_is_kept(monkeypatch):
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(config, "_cache_build_started_at", time.monotonic())

    assert config._release_stale_build_claim() is False
    assert config._cache_build_in_progress is True


def test_claim_without_stamp_is_adopted_then_released(monkeypatch):
    monkeypatch.setattr(config, "_cache_build_in_progress", True)
    monkeypatch.setattr(config, "_cache_build_started_at", 0.0)

    # Adopted with a fresh stamp, not released immediately: an unstamped claim
    # may be a live build whose setter predates the watchdog.
    assert config._release_stale_build_claim() is False
    assert config._cache_build_in_progress is True
    assert config._cache_build_started_at > 0.0

    # Once the adopted stamp ages past the window it is reclaimed.
    monkeypatch.setattr(
        config,
        "_cache_build_started_at",
        time.monotonic() - config._STALE_BUILD_CLAIM_SECONDS - 1,
    )
    assert config._release_stale_build_claim() is True
    assert config._cache_build_in_progress is False
