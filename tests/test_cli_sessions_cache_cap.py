"""Tests: ``_CLI_SESSIONS_CACHE`` is LRU-bounded (drop-oldest).

Regression: the CLI/cron session projection cache was a plain ``dict`` with TTL
storage but NO size cap and only lazy, same-key eviction. Each value is a
``copy.deepcopy()`` of the full CLI/cron session list (the expensive projection
behind #4842/#4672). The cache key folds in a state.db content fingerprint that
advances on every streamed message, so the next poll builds a NEW key; the
previous key's heavy deepcopy was never looked up again, so its lazy-expiry path
never ran. A long-lived process under churn could accumulate orphaned heavy
deepcopies until the next structural ``clear_cli_sessions_cache()``.

The cache is now an ``OrderedDict`` capped at ``_CLI_SESSIONS_CACHE_MAX_ENTRIES``
with drop-oldest on write (mirroring ``_CLAUDE_CODE_PARSE_CACHE``) and
``move_to_end`` on fresh hits. TTL remains the primary freshness control; the
cap is the backstop the plain dict lacked.
"""
from __future__ import annotations

import api.models as models
from api.models import (
    _CLI_SESSIONS_CACHE_MAX_ENTRIES,
    _cache_cli_sessions_if_current,
    _cli_sessions_cache_invalidation_stamp,
    _copy_fresh_cli_sessions_cache_entry,
)


def _reset_cache():
    with models._CLI_SESSIONS_CACHE_LOCK:
        models._CLI_SESSIONS_CACHE.clear()


def test_cache_is_bounded_drops_oldest_on_write():
    """Inserting more than MAX_ENTRIES distinct keys keeps len == MAX and evicts
    the OLDEST (least-recently-used) entry."""
    _reset_cache()
    stamp = _cli_sessions_cache_invalidation_stamp()
    cap = _CLI_SESSIONS_CACHE_MAX_ENTRIES
    assert cap >= 1

    # Insert cap + 5 distinct keys, each a distinct payload so we can track them.
    for i in range(cap + 5):
        key = (f"profile-{i}",)
        ok = _cache_cli_sessions_if_current(
            key, ttl=60.0, invalidation_stamp=stamp, sessions=[{"id": i}]
        )
        assert ok

    with models._CLI_SESSIONS_CACHE_LOCK:
        assert len(models._CLI_SESSIONS_CACHE) == cap
        keys = list(models._CLI_SESSIONS_CACHE.keys())
    # The oldest 5 (profile-0..4) were evicted; the newest `cap` remain.
    assert keys[0] == ("profile-5",)
    assert keys[-1] == (f"profile-{cap + 5 - 1}",)
    assert ("profile-0",) not in keys


def test_fresh_hit_marks_entry_most_recently_used():
    """A fresh cache hit moves the entry to the end (most-recently-used), so a
    frequently-read entry is NOT evicted over colder ones."""
    _reset_cache()
    stamp = _cli_sessions_cache_invalidation_stamp()
    cap = _CLI_SESSIONS_CACHE_MAX_ENTRIES

    # Fill the cache exactly.
    for i in range(cap):
        _cache_cli_sessions_if_current(
            (f"k{i}",), ttl=60.0, invalidation_stamp=stamp, sessions=[{"id": i}]
        )
    # Touch k0 (the oldest) so it becomes most-recently-used.
    served = _copy_fresh_cli_sessions_cache_entry(("k0",))
    assert served == [{"id": 0}]
    # Now insert one more; the eviction victim should be k1, NOT k0.
    _cache_cli_sessions_if_current(
        ("k_new",), ttl=60.0, invalidation_stamp=stamp, sessions=[{"id": 999}]
    )
    with models._CLI_SESSIONS_CACHE_LOCK:
        keys = list(models._CLI_SESSIONS_CACHE.keys())
    assert ("k0",) in keys  # survived because it was just used
    assert ("k1",) not in keys  # evicted as the new oldest
    assert ("k_new",) in keys


def test_cache_is_ordered_dict_not_plain_dict():
    """The regression: the cache used to be a plain dict with no ordering. It
    must now be an OrderedDict to support move_to_end / popitem(last=False)."""
    import collections

    assert isinstance(models._CLI_SESSIONS_CACHE, collections.OrderedDict)
