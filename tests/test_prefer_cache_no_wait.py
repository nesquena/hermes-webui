"""Regression: prefer_cache resolutions must not wait on an in-flight rebuild.

The ``GET /api/session?...&resolve_model=1`` display path relies on
``get_available_models(prefer_cache=True)`` being non-blocking ("serve the
warm/disk cache or a network-free minimal catalog; never run or wait for
the live provider probe"). The shared wait gate previously made every such
call queue behind an in-flight rebuild for up to the rebuild budget
(observed 4447-4843ms == ``_LIVE_REBUILD_BUDGET_SECONDS``).

The wait is pinned with a deterministic tripwire: any attempt to wait on
the rebuild condition fails immediately, instead of relying on wall-clock
assertions that a loaded CI box can turn into flaky scheduling failures.

The mixed-flag contract is pinned too: ``prefer_cache`` and
``force_refresh`` are mutually exclusive and the combination is rejected at
entry (previously it could still wait in the forced-refresh follower block).
"""

import pytest

import api.config as cfg


class _WaitTripwire:
    """Proxy a Condition; raise if the code under test tries to wait on it."""

    def __init__(self, target):
        self._target = target

    def wait_for(self, predicate, timeout=None):
        raise AssertionError(
            "prefer_cache resolution attempted to wait on the catalog rebuild"
        )

    def __enter__(self):
        return self._target.__enter__()

    def __exit__(self, *exc_info):
        return self._target.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._target, name)


def _pin_cfg_mtime(monkeypatch):
    """Avoid a real config reload inside the resolver under test."""
    try:
        monkeypatch.setattr(
            cfg, "_cfg_mtime", cfg._get_config_path().stat().st_mtime, raising=False
        )
    except Exception:
        pass


def _cold_caches(monkeypatch):
    """Cold memory/disk caches so the prefer_cache fall-through is exercised."""
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    _pin_cfg_mtime(monkeypatch)


def test_prefer_cache_does_not_wait_on_inflight_rebuild(monkeypatch):
    _cold_caches(monkeypatch)
    # A rebuild is in progress and will NOT finish during the call.
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_cv", _WaitTripwire(cfg._cache_build_cv))

    result = cfg.get_available_models(prefer_cache=True)

    assert isinstance(result, dict)


def test_mixed_prefer_cache_and_force_refresh_is_rejected(monkeypatch):
    """The contradictory flag combination must fail fast at entry."""
    _cold_caches(monkeypatch)
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)
    monkeypatch.setattr(cfg, "_cache_build_cv", _WaitTripwire(cfg._cache_build_cv))

    with pytest.raises(ValueError, match="mutually exclusive"):
        cfg.get_available_models(prefer_cache=True, force_refresh=True)