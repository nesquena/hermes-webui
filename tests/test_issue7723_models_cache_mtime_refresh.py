"""Regression coverage for #7723 session-visit cache freshness.

mtime now doubles as *access time* (every fresh hit advances it), so it can no
longer measure freshness by itself. The 300s refresh clock is the persisted
``_built_at`` (last live rebuild) stamp in the cache payload, written by
``_save_models_cache_to_disk``. These tests drive ``get_available_models_for_session_visit()``
end-to-end (never the ``_touch_models_cache_mtime`` helper directly), so they go
red if the call-sites are deleted.
"""

from __future__ import annotations

import json
import os
import time


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


def _write_disk_cache(cache_path, *, label, built_at, mtime):
    """Write a cache payload and set its access/build clocks independently."""
    payload = {
        "_built_at": built_at,
        "active_provider": "openai",
        "default_model": label,
        "configured_model_badges": {},
        "groups": _catalog(label)["groups"],
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(cache_path, (mtime, mtime))


def test_fresh_disk_hit_advances_mtime_and_skips_live_rebuild(tmp_path, monkeypatch):
    """Req: a fresh disk hit advances mtime AND does not force-refresh."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    disk_catalog = _catalog("cached-model")
    cache_path = tmp_path / "models_cache.profile.json"
    _write_disk_cache(cache_path, label="cached-model", built_at=time.time(), mtime=time.time() - 60)
    old_mtime = cache_path.stat().st_mtime

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: disk_catalog)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})

    def _unexpected_rebuild(**_kwargs):
        raise AssertionError("fresh session-visit cache must not run a live rebuild")

    monkeypatch.setattr(cfg, "get_available_models", _unexpected_rebuild)

    assert cfg.get_available_models_for_session_visit() == disk_catalog
    assert cache_path.stat().st_mtime > old_mtime  # access time was advanced


def test_fresh_memory_hit_advances_mtime_and_skips_rebuild(tmp_path, monkeypatch):
    """Req: a fresh memory hit also advances mtime."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    mem_catalog = _catalog("mem-model")
    cache_path = tmp_path / "models_cache.profile.json"
    _write_disk_cache(cache_path, label="m", built_at=time.time(), mtime=time.time() - 60)
    old_mtime = cache_path.stat().st_mtime

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
    assert cache_path.stat().st_mtime > old_mtime  # access time was advanced


def test_utime_oserror_still_returns_cached_catalog(tmp_path, monkeypatch):
    """Req: an os.utime OSError must not swallow the cached catalog."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    mem_catalog = _catalog("mem-model")
    cache_path = tmp_path / "models_cache.profile.json"
    _write_disk_cache(cache_path, label="m", built_at=time.time(), mtime=time.time() - 60)
    old_mtime = cache_path.stat().st_mtime

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_available_models_cache", mem_catalog, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", {"profile": "demo"}, raising=False)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})

    def _raise_oserror(*_a, **_k):
        raise OSError("utime failed")

    monkeypatch.setattr(os, "utime", _raise_oserror)

    assert cfg.get_available_models_for_session_visit() == mem_catalog
    assert cache_path.stat().st_mtime == old_mtime  # touch failed, catalog still served


def test_stale_cache_force_refreshes_once_and_is_not_restamped_before_decision(tmp_path, monkeypatch):
    """Req: stale cache enters the forced-refresh branch exactly once and is NOT
    restamped before that decision."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    stale_catalog = _catalog("stale-model")
    rebuilt_catalog = _catalog("rebuilt-model")
    cache_path = tmp_path / "models_cache.profile.json"
    _write_disk_cache(cache_path, label="stale-model", built_at=time.time() - 600, mtime=time.time() - 600)
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
    assert cache_path.stat().st_mtime == old_mtime  # not restamped before the decision


def test_repeated_hits_near_boundary_keep_sliding_window_without_rebuild(tmp_path, monkeypatch):
    """Req: repeated hits near the 300s boundary keep the sliding window fresh
    without triggering a rebuild — access keeps advancing while built_at is in-window."""
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    mem_catalog = _catalog("mem-model")
    cache_path = tmp_path / "models_cache.profile.json"
    _write_disk_cache(cache_path, label="m", built_at=time.time() - 250, mtime=time.time() - 250)
    initial_mtime = cache_path.stat().st_mtime
    refresh_calls = []

    monkeypatch.setattr(cfg, "_SESSION_VISIT_MODELS_FRESHNESS_SECONDS", 300.0, raising=False)
    monkeypatch.setattr(cfg, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(cfg, "_available_models_cache", mem_catalog, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", {"profile": "demo"}, raising=False)
    monkeypatch.setattr(cfg, "_models_cache_source_fingerprint", lambda: {"profile": "demo"})

    def _unexpected_rebuild(**_kwargs):
        refresh_calls.append("rebuild")
        raise AssertionError("boundary-window hits must not rebuild")

    monkeypatch.setattr(cfg, "get_available_models", _unexpected_rebuild)

    for _ in range(3):
        assert cfg.get_available_models_for_session_visit() == mem_catalog

    assert refresh_calls == []
    assert cache_path.stat().st_mtime > initial_mtime  # access kept advancing each hit


def test_frequently_accessed_but_old_built_catalog_still_refreshes(tmp_path, monkeypatch):
    """The #7723 symptom: a catalog built 10 minutes ago but accessed very
    recently (frequent session opens reset mtime) must STILL force-refresh.

    Under the pre-fix mtime-only freshness, the fresh access clock short-circuits
    to a memory hit and the picker keeps retired models forever. This test goes
    red against that logic and green once mtime is demoted to access time.
    """
    import api.config as cfg

    _reset_models_memory_cache(monkeypatch)
    stale_catalog = _catalog("stale-model")
    rebuilt_catalog = _catalog("rebuilt-model")
    cache_path = tmp_path / "models_cache.profile.json"
    # built 10 minutes ago (stale) but accessed very recently (fresh mtime).
    _write_disk_cache(cache_path, label="stale-model", built_at=time.time() - 600, mtime=time.time())
    refresh_calls = []

    # Warm memory so the OLD (mtime-only) logic would short-circuit on the fresh mtime.
    monkeypatch.setattr(cfg, "_available_models_cache", stale_catalog, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", time.monotonic(), raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_source_fingerprint", {"profile": "demo"}, raising=False)

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