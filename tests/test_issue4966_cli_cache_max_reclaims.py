import threading
import api.models as models


def test_cli_sessions_cache_reclaim_loop_caps_at_max_reclaims(monkeypatch):
    """An adversarial clear-storm (cache cleared on every iteration after owner finishes)
    falls back to own rebuild once max_reclaims is reached (#4966)."""
    cache_key = ("test_reclaim_key",)
    reclaims_observed = {"count": 0}
    rebuild_called = {"count": 0}

    # Custom claim rebuild that simulates being a waiter that finishes waiting,
    # but finds no cache entry each time until max_reclaims.
    real_claim = models._cli_sessions_cache_claim_rebuild

    def _simulated_claim(key):
        event, is_owner = real_claim(key)
        # Immediately set event so waiter doesn't sleep, but do NOT populate cache
        event.set()
        return event, False

    monkeypatch.setattr(models, "_cli_sessions_cache_claim_rebuild", _simulated_claim)

    def _mock_load_and_cache(**kwargs):
        rebuild_called["count"] += 1
        return [{"session_id": "fallback_session"}]

    monkeypatch.setattr(models, "_load_and_cache_cli_sessions", _mock_load_and_cache)

    result = models._reload_cli_sessions_after_inflight(
        cache_key=cache_key,
        ttl=10.0,
        stale_sessions=None,
        stale_stamp=None,
        load_sessions=lambda: [],
        all_profiles=False,
        db_path=":memory:",
        max_reclaims=3,
    )

    assert result == [{"session_id": "fallback_session"}]
    assert rebuild_called["count"] == 1


def test_cli_sessions_cache_default_max_reclaims_constant():
    """Verify default _CLI_SESSIONS_CACHE_MAX_RECLAIMS constant is defined and positive (#4966)."""
    assert hasattr(models, "_CLI_SESSIONS_CACHE_MAX_RECLAIMS")
    assert models._CLI_SESSIONS_CACHE_MAX_RECLAIMS > 0
