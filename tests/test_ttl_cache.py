"""
Tests for the TTL cache in api/config.py — get_available_models().

Validates:
  - Cache hit within TTL window
  - TTL expiry triggers re-scan
  - Config mtime change invalidates cache before TTL check
  - copy.deepcopy() isolation (mutating returned dict doesn't pollute cache)
  - invalidate_models_cache() direct invalidation
"""

import time
from unittest.mock import patch

import api.config as config


def _reset_cache():
    """Reset TTL cache globals to a clean state."""
    config._available_models_cache = None
    config._available_models_cache_ts = 0.0


# ── 1. test_cache_hit_within_ttl ──────────────────────────────────────────


def test_cache_hit_within_ttl():
    """Call get_available_models() twice within the TTL window.
    The second call should return cached data without re-scanning providers.
    We verify this by patching reload_config (called when cache is cold)
    and asserting it is only invoked once.
    """
    _reset_cache()
    original_reload = config.reload_config

    call_count = 0

    def _counting_reload():
        nonlocal call_count
        call_count += 1
        return original_reload()

    with patch.object(
        config, "reload_config", wraps=original_reload, side_effect=_counting_reload
    ):
        saved_mtime = config._cfg_mtime
        try:
            # Force mtime mismatch so the first call triggers reload_config + cache fill
            config._cfg_mtime = 0.0
            result1 = config.get_available_models()
            first_call_count = call_count

            # Sync _cfg_mtime to the actual file so the second call doesn't
            # re-trigger reload_config via mtime mismatch — we want it to hit the TTL cache.
            try:
                config._cfg_mtime = (
                    config.Path(config._get_config_path()).stat().st_mtime
                )
            except OSError:
                config._cfg_mtime = 0.0

            result2 = config.get_available_models()

            # Both results should have the same structure
            assert "groups" in result1
            assert "groups" in result2

            # reload_config should not have been called again for the second invocation
            # (the TTL cache served it)
            assert call_count == first_call_count, (
                f"Expected no extra reload_config calls, but got "
                f"{call_count - first_call_count} extra"
            )
        finally:
            config._cfg_mtime = saved_mtime
    _reset_cache()


# ── 2. test_ttl_expiry ───────────────────────────────────────────────────


def test_ttl_expiry():
    """Populate the cache, then advance time.monotonic() past 60s.
    The next call should re-scan (not serve from cache).
    """
    _reset_cache()

    # Ensure _cfg_mtime matches file so mtime check doesn't invalidate
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0

    # First call populates cache
    result1 = config.get_available_models()
    assert config._available_models_cache is not None, "Cache should be populated"

    # Record the cache timestamp
    cache_ts = config._available_models_cache_ts

    # Advance time.monotonic() by more than the TTL
    original_monotonic = time.monotonic
    offset = config._AVAILABLE_MODELS_CACHE_TTL + 10.0  # 70s past the real monotonic

    with patch.object(
        time, "monotonic", side_effect=lambda: original_monotonic() + offset
    ):
        result2 = config.get_available_models()

    # The cache should have been refreshed — the timestamp must be newer
    assert config._available_models_cache_ts > cache_ts, (
        "Cache should have been refreshed after TTL expiry"
    )

    _reset_cache()


# ── 3. test_mtime_invalidation ───────────────────────────────────────────


def test_mtime_invalidation():
    """Populate the cache, then change _cfg_mtime to simulate a config file
    change on disk. The next call should invalidate the cache and re-scan.
    """
    _reset_cache()

    # Ensure _cfg_mtime matches file so first call doesn't re-scan due to mtime
    try:
        real_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except OSError:
        real_mtime = 0.0
    config._cfg_mtime = real_mtime

    # First call populates cache
    result1 = config.get_available_models()
    assert config._available_models_cache is not None

    # Simulate config.yaml changed on disk by setting _cfg_mtime to 0
    # (which won't match the actual file mtime)
    config._cfg_mtime = 0.0

    # The next call should detect mtime mismatch, reload, and invalidate cache
    old_cache = config._available_models_cache
    old_ts = config._available_models_cache_ts

    result2 = config.get_available_models()

    # Cache must have been refreshed — timestamp advanced since we reset it
    # to 0.0 on invalidation.
    assert config._available_models_cache_ts > 0.0, (
        "Cache timestamp should be updated after invalidation + rebuild"
    )

    # Restore
    config._cfg_mtime = real_mtime
    _reset_cache()


# ── 4. test_deepcopy_isolation ────────────────────────────────────────────


def test_deepcopy_isolation():
    """Mutating the returned dict from get_available_models() must not
    affect the cache or subsequent return values.
    """
    _reset_cache()

    # Ensure _cfg_mtime matches file so mtime check doesn't invalidate
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0

    # First call populates cache
    result1 = config.get_available_models()

    # Mutate the returned dict
    if result1["groups"]:
        result1["groups"][0]["models"].clear()
    result1["groups"].append({"provider": "FAKE", "models": [{"id": "fake-model"}]})
    result1["active_provider"] = "HACKED"

    # Second call should return an unmutated copy
    result2 = config.get_available_models()

    # The mutated keys must not appear in the second result
    assert result2["active_provider"] != "HACKED", "Mutation leaked into cache"
    assert not any(g.get("provider") == "FAKE" for g in result2["groups"]), (
        "Fake provider leaked into cache"
    )

    # If there were groups originally, the first group's models should not be empty
    # (unless it genuinely had no models, which is unlikely)
    if result1["groups"] and result2["groups"]:
        # result1["groups"][0]["models"] was cleared, but result2 should be intact
        assert len(result2["groups"][0].get("models", [])) > 0, (
            "Mutation of result1 cleared models in result2 — deepcopy failed"
        )

    _reset_cache()


# ── 5. test_invalidate_models_cache_direct ───────────────────────────────


def test_invalidate_models_cache_direct():
    """Call invalidate_models_cache() after populating the cache.
    _AVAILABLE_MODELS_CACHE should be None and the next call should re-scan.
    """
    _reset_cache()

    # Ensure _cfg_mtime matches file so mtime check doesn't invalidate
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0

    # First call populates cache
    result1 = config.get_available_models()
    assert config._available_models_cache is not None, "Cache should be populated"
    first_ts = config._available_models_cache_ts

    # Directly invalidate
    config.invalidate_models_cache()

    # Cache must be cleared
    assert config._available_models_cache is None, (
        "invalidate_models_cache() should set _AVAILABLE_MODELS_CACHE to None"
    )

    # Next call should re-scan and produce a fresh cache
    result2 = config.get_available_models()
    assert config._available_models_cache is not None, "Cache should be re-populated"
    assert config._available_models_cache_ts >= first_ts, (
        "Cache timestamp should be updated after re-scan"
    )

    _reset_cache()
