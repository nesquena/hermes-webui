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
    def _simulated_claim(key):
        reclaims_observed["count"] += 1
        event = threading.Event()
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
    assert reclaims_observed["count"] == 3
    assert rebuild_called["count"] == 1


def test_cli_sessions_cache_default_max_reclaims_constant():
    """Verify default _CLI_SESSIONS_CACHE_MAX_RECLAIMS constant is defined and positive (#4966)."""
    assert hasattr(models, "_CLI_SESSIONS_CACHE_MAX_RECLAIMS")
    assert models._CLI_SESSIONS_CACHE_MAX_RECLAIMS > 0


def test_cli_sessions_cache_fallback_preserves_newer_rows_published_during_load():
    """The capped fallback's choose-and-publish is atomic under the cache lock: if a
    rebuilder publishes fresher rows (newer expiry, same stamp) while the fallback's
    load is running, both the returned AND the cached rows are the newer snapshot,
    not the fallback's older one (#4966)."""
    models.clear_cli_sessions_cache()
    cache_key = ("test_fallback_preserve_newer",)
    ttl = 10.0
    stamp = models._cli_sessions_cache_invalidation_stamp()
    newer_rows = [{"session_id": "newer-owner-session"}]
    older_rows = [{"session_id": "older-fallback-session"}]

    # While _load_and_cache_cli_sessions is "loading" (running this callback), a
    # concurrent rebuilder publishes fresher rows under the SAME stamp with a newer
    # expiry. The load itself returns the older rows, as the capped fallback would.
    def _load_sessions_with_concurrent_publish():
        models._cache_cli_sessions_if_current(
            cache_key,
            ttl + 10_000.0,  # much newer expiry, same stamp
            stamp,
            newer_rows,
        )
        return older_rows

    result = models._load_and_cache_cli_sessions(
        cache_key=cache_key,
        ttl=ttl,
        invalidation_stamp=stamp,
        load_sessions=_load_sessions_with_concurrent_publish,
        stale_sessions=None,
        stale_stamp=None,
        all_profiles=False,
        db_path=":memory:",
    )

    # The returned rows must be the newer snapshot (don't clobber with older load).
    assert result == newer_rows
    # The cached rows must also be the newer snapshot.
    with models._CLI_SESSIONS_CACHE_LOCK:
        entry = models._CLI_SESSIONS_CACHE.get(cache_key)
    assert entry is not None
    assert entry[2] == newer_rows

    models.clear_cli_sessions_cache()
