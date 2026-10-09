"""Regression coverage for models cache _built_at stamp and session-visit freshness (#7723).

The 300s refresh clock is the persisted ``_built_at`` (last live rebuild) stamp
in the cache payload, written by ``_save_models_cache_to_disk``. Caches written
before the stamp existed (or carrying an invalid/future stamp) fall back to
file mtime. Cache hits do not touch file mtime (zero writes per visit).
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path


def _catalog(label: str) -> dict:
    return {
        "active_provider": "openai",
        "default_model": label,
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": label, "label": label, "supports_fast_tier": False}],
            }
        ],
        "aliases": {},
    }


def _reset_models_memory_cache(monkeypatch):
    import api.config as cfg

    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", None, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)


def _write_disk_cache(cache_path: Path, *, label: str, built_at: float | None, mtime: float):
    """Write a cache payload and set its access/build clocks independently."""
    payload = {
        "active_provider": "openai",
        "default_model": label,
        "configured_model_badges": {},
        "groups": _catalog(label)["groups"],
    }
    if built_at is not None:
        payload["_built_at"] = built_at
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(cache_path, (mtime, mtime))


def test_fresh_disk_hit_skips_live_rebuild_and_does_not_mutate_mtime(tmp_path, monkeypatch):
    """Req: a fresh disk hit returns cached catalog without live rebuild and without modifying mtime."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    disk_catalog = _catalog("cached-model")
    cache_path = tmp_path / "models_cache.profile.json"
    now = time.time()
    _write_disk_cache(cache_path, label="cached-model", built_at=now - 50, mtime=now - 60)
    original_mtime = cache_path.stat().st_mtime

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: disk_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})

    def _unexpected_rebuild(**_kwargs):
        raise AssertionError("fresh session-visit cache must not run a live rebuild")

    monkeypatch.setattr(cfg, "get_available_models", _unexpected_rebuild)

    assert cfg.get_available_models_for_session_visit() == disk_catalog
    assert cache_path.stat().st_mtime == original_mtime  # zero writes per visit


def test_fresh_memory_hit_skips_rebuild_and_does_not_mutate_mtime(tmp_path, monkeypatch):
    """Req: a fresh memory hit returns cached catalog without modifying disk mtime."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    mem_catalog = _catalog("mem-model")
    cache_path = tmp_path / "models_cache.profile.json"
    now = time.time()
    _write_disk_cache(cache_path, label="m", built_at=now - 50, mtime=now - 60)
    original_mtime = cache_path.stat().st_mtime

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_available_models_cache", mem_catalog, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", {"profile": "demo"}, raising=False)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})

    def _unexpected_rebuild(**_kwargs):
        raise AssertionError("fresh session-visit cache must not rebuild on a memory hit")

    monkeypatch.setattr(cfg, "get_available_models", _unexpected_rebuild)

    assert cfg.get_available_models_for_session_visit() == mem_catalog
    assert cache_path.stat().st_mtime == original_mtime  # zero writes per visit


def test_future_built_at_treated_as_unstamped(tmp_path):
    """Req: a _built_at in the future is rejected and treated as unstamped."""
    import api.config as cfg

    cache_path = tmp_path / "models_cache.future.json"
    now = time.time()
    # Future stamp
    _write_disk_cache(cache_path, label="future-model", built_at=now + 500.0, mtime=now - 350.0)

    assert cfg._models_disk_cache_built_at(cache_path) is None
    # Falls back to mtime age
    age = cfg._models_cache_file_age_seconds(cache_path, now)
    assert age is not None
    assert math.isclose(age, 350.0, abs_tol=1.0)


def test_invalid_built_at_types_and_values_treated_as_unstamped(tmp_path):
    """Req: non-finite, negative, zero, or non-numeric _built_at stamps are rejected."""
    import api.config as cfg

    for bad_value in [float("nan"), float("inf"), float("-inf"), 0.0, -100.0, "2026-10-07", {}, []]:
        cache_path = tmp_path / "models_cache.bad.json"
        payload = {"_built_at": bad_value}
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        assert cfg._models_disk_cache_built_at(cache_path) is None, f"Failed for {bad_value}"


def test_stale_cache_force_refreshes_once_and_is_not_restamped_before_decision(tmp_path, monkeypatch):
    """Req: stale cache enters the forced-refresh branch exactly once and is NOT
    restamped before that decision."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    stale_catalog = _catalog("stale-model")
    rebuilt_catalog = _catalog("rebuilt-model")
    cache_path = tmp_path / "models_cache.profile.json"
    now = time.time()
    _write_disk_cache(cache_path, label="stale-model", built_at=now - 600, mtime=now - 600)
    old_mtime = cache_path.stat().st_mtime
    refresh_calls = []

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: stale_catalog)

    def _live_rebuild(**kwargs):
        refresh_calls.append(kwargs)
        return rebuilt_catalog

    monkeypatch.setattr(cfg, "get_available_models", _live_rebuild)

    assert cfg.get_available_models_for_session_visit() == rebuilt_catalog
    assert refresh_calls == [{"force_refresh": True}]  # EXACTLY ONE force_refresh=True
    assert cache_path.stat().st_mtime == old_mtime


def test_frequently_accessed_but_old_built_catalog_still_refreshes(tmp_path, monkeypatch):
    """The #7723 symptom: a catalog built 10 minutes ago but accessed very
    recently (fresh mtime) must STILL force-refresh against _built_at."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    stale_catalog = _catalog("stale-model")
    rebuilt_catalog = _catalog("rebuilt-model")
    cache_path = tmp_path / "models_cache.profile.json"
    now = time.time()
    # built 10 minutes ago (stale) but mtime is recent (60s ago)
    _write_disk_cache(cache_path, label="stale-model", built_at=now - 600, mtime=now - 60)
    refresh_calls = []

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: stale_catalog)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})

    def _live_rebuild(**kwargs):
        refresh_calls.append(kwargs)
        return rebuilt_catalog

    monkeypatch.setattr(cfg, "get_available_models", _live_rebuild)

    assert cfg.get_available_models_for_session_visit() == rebuilt_catalog
    assert refresh_calls == [{"force_refresh": True}]


def test_unstamped_legacy_cache_falls_back_to_mtime(tmp_path, monkeypatch):
    """An unstamped legacy cache (written before _built_at existed) falls back to mtime age."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    legacy_catalog = _catalog("legacy-model")
    rebuilt_catalog = _catalog("rebuilt-model")
    cache_path = tmp_path / "models_cache.profile.json"
    now = time.time()
    # Unstamped cache written 250s ago (within 300s window)
    _write_disk_cache(cache_path, label="legacy-model", built_at=None, mtime=now - 250)
    refresh_calls = []

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: legacy_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: legacy_catalog)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})

    def _live_rebuild(**kwargs):
        refresh_calls.append(kwargs)
        return rebuilt_catalog

    monkeypatch.setattr(cfg, "get_available_models", _live_rebuild)

    # First visit (within window): served from cache
    res1 = cfg.get_available_models_for_session_visit()
    assert res1 == legacy_catalog
    assert refresh_calls == []

    # Expire mtime past 300s window
    os.utime(cache_path, (now - 350, now - 350))
    _reset_models_memory_cache(monkeypatch)

    # Second visit: now stale, triggers live rebuild
    res2 = cfg.get_available_models_for_session_visit()
    assert res2 == rebuilt_catalog
    assert refresh_calls == [{"force_refresh": True}]