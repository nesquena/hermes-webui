"""Regression: prefer_cache resolutions must not wait on an in-flight rebuild.

The ``GET /api/session?...&resolve_model=1`` display path relies on
``get_available_models(prefer_cache=True)`` being non-blocking ("serve the
warm/disk cache or a network-free minimal catalog; never run or wait for
the live provider probe"). The shared ``should_wait`` gate previously made
every such call queue behind an in-flight rebuild for up to the rebuild
budget, stalling session switches for seconds (observed 4447-4843ms ==
``_LIVE_REBUILD_BUDGET_SECONDS``).
"""

import threading
import time

import api.config as cfg


def _pin_cfg_mtime(monkeypatch):
    """Avoid a real config reload inside the resolver under test."""
    try:
        monkeypatch.setattr(
            cfg, "_cfg_mtime", cfg._get_config_path().stat().st_mtime, raising=False
        )
    except Exception:
        pass


def test_prefer_cache_does_not_wait_on_inflight_rebuild(monkeypatch):
    # Cold caches so the prefer_cache fall-through path is exercised.
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg, "_available_models_cache_source_fingerprint", None, raising=False
    )
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    _pin_cfg_mtime(monkeypatch)
    # A rebuild is in progress and will NOT finish during the call.
    monkeypatch.setattr(cfg, "_cache_build_in_progress", True, raising=False)

    done = {}

    def _call():
        t0 = time.monotonic()
        result = cfg.get_available_models(prefer_cache=True)
        done["elapsed"] = time.monotonic() - t0
        done["result"] = result

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), (
        "get_available_models(prefer_cache=True) blocked on an in-flight "
        "catalog rebuild - the non-blocking contract regressed"
    )
    assert done["elapsed"] < 1.0
    assert isinstance(done["result"], dict)