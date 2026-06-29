"""Regression: the memoized redactor must stay byte-identical to the uncached
path, and the >16KB bypass must still redact (no secret leaks through the cache).

Locks the #5204 memoization contract: `_redact_fn_cached` exists only as a
performance optimization over `_redact_fn_uncached` and must never change what
gets redacted — neither for cached (small) strings nor for the large strings
that route around the cache.
"""
from api import helpers

_SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_GH = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"


def test_cached_redactor_matches_uncached_for_sensitive_input():
    sample = f"key={_SECRET} token={_GH} plain text stays"
    cached = helpers._redact_fn_cached(sample)
    uncached = helpers._redact_fn_uncached(sample)
    assert cached == uncached
    # and it actually redacted (no verbatim secret survives)
    assert _SECRET not in cached
    assert _GH not in cached


def test_cached_redactor_is_idempotent():
    sample = f"first {_SECRET} second {_SECRET}"
    once = helpers._redact_fn_cached(sample)
    twice = helpers._redact_fn_cached(sample)
    assert once == twice == helpers._redact_fn_uncached(sample)


def test_oversize_input_bypasses_cache_but_still_redacts():
    # A string longer than the per-entry cap routes around lru_cache; it must
    # still be redacted identically to the uncached path (no cache-skip leak).
    big = ("x" * (helpers._REDACT_CACHE_MAX_TEXT_LEN + 100)) + f" {_SECRET}"
    out = helpers._redact_fn_cached(big)
    assert _SECRET not in out
    assert out == helpers._redact_fn_uncached(big)


def test_clean_text_unchanged_through_cache():
    sample = "totally benign text with no secrets at all"
    assert helpers._redact_fn_cached(sample) == sample == helpers._redact_fn_uncached(sample)
