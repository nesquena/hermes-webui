"""Followers must not sit 60s behind an in-flight models-catalog rebuild.

The browser aborts ``api()`` at 30s. A 60s server-side follower wait makes
``POST /api/session/new`` look hung ("New conversation") even though the server
would eventually return 200. ``prefer_cache`` callers must never wait, and a
plain follower should wait at most the live-rebuild budget.
"""
from __future__ import annotations

import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _catalog(label: str) -> dict:
    return {
        "active_provider": "openrouter",
        "default_model": label,
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [{"id": label, "label": label}],
            }
        ],
        "aliases": {},
    }


def _pin_config(monkeypatch, cfg, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)


def _seed_resident_cache(monkeypatch, cfg, catalog: dict) -> None:
    monkeypatch.setattr(cfg, "_available_models_cache", catalog, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(cfg, "_available_models_live_rebuild_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg,
        "_available_models_cache_source_fingerprint",
        cfg._models_cache_source_fingerprint(),
        raising=False,
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)


def test_prefer_cache_does_not_wait_on_inflight_rebuild(tmp_path, monkeypatch):
    import api.config as cfg

    catalog = _catalog("resident-model")
    _pin_config(monkeypatch, cfg, tmp_path)
    _seed_resident_cache(monkeypatch, cfg, catalog)

    def _must_not_wait(*_args, **_kwargs):
        raise AssertionError("prefer_cache must not wait on an in-flight rebuild")

    monkeypatch.setattr(cfg._cache_build_cv, "wait_for", _must_not_wait)

    started = time.monotonic()
    result = cfg.get_available_models(prefer_cache=True)
    assert time.monotonic() - started < 1.0
    assert result["default_model"] == "resident-model"
    # Serving a follower must not clear the rebuild flag for the real builder.
    assert cfg._cache_build_in_progress is True


def test_plain_follower_waits_at_most_rebuild_budget(tmp_path, monkeypatch):
    import api.config as cfg

    catalog = _catalog("resident-model")
    _pin_config(monkeypatch, cfg, tmp_path)
    _seed_resident_cache(monkeypatch, cfg, catalog)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    timeouts: list[float | None] = []

    def _wait(_predicate, timeout=None):
        timeouts.append(timeout)
        return False

    monkeypatch.setattr(cfg._cache_build_cv, "wait_for", _wait)

    started = time.monotonic()
    result = cfg.get_available_models()
    elapsed = time.monotonic() - started

    assert timeouts == [0.05]
    assert result["default_model"] == "resident-model"
    assert elapsed < 1.0
    assert cfg._cache_build_in_progress is True


def test_session_new_uses_cached_catalog():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    start = src.find('if parsed.path == "/api/session/new":')
    assert start != -1
    end = src.find('if parsed.path == "/api/session/compression-recovery/start":', start)
    body = src[start:end]
    assert "prefer_cached_catalog=True" in body
