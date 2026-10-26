"""Followers must not sit 60s behind an in-flight models-catalog rebuild.

The browser aborts ``api()`` at 30s. A 60s server-side follower wait makes
``POST /api/session/new`` look hung ("New conversation") even though the server
would eventually return 200. ``prefer_cache`` callers must never wait, and a
follower whose wait runs out serves the best catalog it already knows instead of
re-entering the cold path.

A **plain** follower deliberately keeps the full wait for the slot owner:
``tests/test_issue7481_custom_probe_budget_fairness.py`` pins that a third caller
waits for the rebuild that owns the single-flight slot and is served *that*
catalog. Shortening the plain-follower wait is a separate product decision and
does not travel in this change.
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


def _patch_catalog_cv_wait(monkeypatch, cfg, fn) -> None:
    """Install ``fn`` as the catalog condition's ``wait_for``.

    Patch the class, not the instance. ``monkeypatch`` undoes an instance
    assignment by writing the previous bound method back onto the instance,
    and that shadow hides every later ``threading.Condition.wait_for`` patch
    for the rest of the process. The force-refresh follower tests then call
    the real wait (a short budget looks like "never waited"; budget <= 0
    blocks with no timeout).
    """
    import threading

    original = threading.Condition.wait_for

    def _wait_for(self, predicate, timeout=None):
        if self is not cfg._cache_build_cv:
            return original(self, predicate, timeout)
        return fn(predicate, timeout=timeout)

    monkeypatch.setattr(threading.Condition, "wait_for", _wait_for)


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

    _patch_catalog_cv_wait(monkeypatch, cfg, _must_not_wait)

    started = time.monotonic()
    result = cfg.get_available_models(prefer_cache=True)
    assert time.monotonic() - started < 1.0
    assert result["default_model"] == "resident-model"
    # Serving a follower must not clear the rebuild flag for the real builder.
    assert cfg._cache_build_in_progress is True


def test_plain_follower_still_waits_for_the_slot_owner(tmp_path, monkeypatch):
    """A plain follower keeps waiting for the rebuild that owns the slot.

    Deliberately *not* bounded by the live-rebuild budget: upstream pins the
    waiting semantics in ``test_issue7481_custom_probe_budget_fairness.py``
    (a third caller waits for B and is served B's catalog, not a cached one).
    """
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

    _patch_catalog_cv_wait(monkeypatch, cfg, _wait)

    result = cfg.get_available_models()

    assert len(timeouts) == 1
    assert timeouts[0] is not None and timeouts[0] >= 1.0, (
        "a plain follower must wait for the slot owner; shortening that wait "
        "is a product decision upstream pins against (test_issue7481)"
    )
    assert result["default_model"] == "resident-model"
    assert cfg._cache_build_in_progress is True


def test_timed_out_follower_serves_the_resident_catalog_not_the_cold_path(
    tmp_path, monkeypatch
):
    """When the follower wait runs out mid-rebuild, it serves what it knows.

    It must not fall through into the cold path (live probes, a second build).
    """
    import api.config as cfg

    catalog = _catalog("resident-model")
    _pin_config(monkeypatch, cfg, tmp_path)
    _seed_resident_cache(monkeypatch, cfg, catalog)
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)

    def _wait(_predicate, timeout=None):
        return False

    _patch_catalog_cv_wait(monkeypatch, cfg, _wait)

    def _must_not_rebuild(*_args, **_kwargs):
        raise AssertionError("a timed-out follower must not re-enter the cold path")

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _must_not_rebuild)

    result = cfg.get_available_models()
    assert result["default_model"] == "resident-model"
    assert cfg._cache_build_in_progress is True


def test_session_new_uses_cached_catalog():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    start = src.find('if parsed.path == "/api/session/new":')
    assert start != -1
    end = src.find('if parsed.path == "/api/session/compression-recovery/start":', start)
    body = src[start:end]
    assert "prefer_cached_catalog=True" in body


def test_session_model_state_forwards_prefer_cached_catalog(monkeypatch):
    """The new-session flag must reach the resolver, not stop at the helper."""
    import api.routes as routes

    seen = {}

    def _resolve(model, provider, prefer_cached_catalog=False):
        seen["prefer_cached_catalog"] = prefer_cached_catalog
        return model, provider, False

    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", _resolve)
    model, provider = routes._session_model_state_from_request(
        "cached-model",
        None,
        prefer_cached_catalog=True,
    )
    assert (model, provider) == ("cached-model", None)
    assert seen.get("prefer_cached_catalog") is True
