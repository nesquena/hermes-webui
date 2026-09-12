"""Large strings: memoize instead of re-paying ~15 regexes on every request.

``_redact_fn_cached`` excluded every string > 16,384 characters from the LRU,
to avoid evicting the thousands of small recurring strings and inflating RSS.
Measured consequence on a real 22 MB session: 59 large strings (29 unique,
0.76 MB) went through the full regex set on EVERY request, for an always
identical result -- 1.68 s per request, i.e. 99.9% of the recurring redaction
cost once the small-string cache is warm.

The fix adds a SECOND cache, dedicated to large strings, bounded both by entry
count AND by a maximum size per entry, so that RSS stays capped. Giant strings
(> cap) deliberately remain uncached.

Contract verified here:
  1. a large string is redacted exactly as before;
  2. the same large string is not recomputed on the second call;
  3. the two caches stay separate (no cross-eviction);
  4. a string above the cap is never retained.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402
from api.helpers import (  # noqa: E402
    _REDACT_CACHE_MAX_TEXT_LEN,
    _REDACT_LARGE_CACHE_MAX_TEXT_LEN,
    _redact_fn_cached,
    _redact_fn_uncached,
)

SECRET = "gh" + "p_" + "0123456789abcdefghijklmnopqrstuvwxyzAB"


@pytest.fixture(autouse=True)
def _isolate_large_cache():
    """Do not let this file pollute the process-wide global state.

    The cache is a module singleton shared by the whole suite. Clearing it
    without restoring it makes the following tests start cold, which disturbs
    those that depend on freshness windows or response times. We start from an
    empty cache AND clear it again on exit, so that this file has no observable
    effect outside of itself.
    """
    helpers._redact_fn_large_lru.cache_clear()
    yield
    helpers._redact_fn_large_lru.cache_clear()


def _big(secret: str, size: int) -> str:
    """String > small-cache threshold, carrying a secret.

    The filler must be long enough to reach ``size`` even beyond the large
    cache cap, otherwise truncation yields a string that is too short and the
    test no longer verifies what it claims.
    """
    unit = "lorem ipsum dolor sit amet "
    filler = unit * (size // len(unit) + 1)
    text = (filler + secret + filler)[:size]
    assert len(text) == size
    return text


def test_large_string_redaction_matches_uncached():
    text = _big(SECRET, _REDACT_CACHE_MAX_TEXT_LEN + 5000)
    assert len(text) > _REDACT_CACHE_MAX_TEXT_LEN
    assert _redact_fn_cached(text) == _redact_fn_uncached(text)
    assert SECRET not in _redact_fn_cached(text)


def test_large_string_is_memoized():
    helpers._redact_fn_large_lru.cache_clear()
    text = _big(SECRET, _REDACT_CACHE_MAX_TEXT_LEN + 7000)

    first = _redact_fn_cached(text)
    misses_after_first = helpers._redact_fn_large_lru.cache_info().misses

    second = _redact_fn_cached(text)
    info = helpers._redact_fn_large_lru.cache_info()

    assert second == first
    assert info.hits >= 1, "large string recomputed on second call"
    assert info.misses == misses_after_first, "unexpected additional miss"


def test_small_strings_do_not_use_the_large_cache():
    helpers._redact_fn_large_lru.cache_clear()
    small = f"small text {SECRET}"
    assert len(small) <= _REDACT_CACHE_MAX_TEXT_LEN
    _redact_fn_cached(small)
    info = helpers._redact_fn_large_lru.cache_info()
    assert info.hits == 0 and info.misses == 0, "small string routed to the large cache"


def test_giant_string_is_not_retained():
    """Above the cap: correct, but never cached (bounded RSS)."""
    helpers._redact_fn_large_lru.cache_clear()
    giant = _big(SECRET, _REDACT_LARGE_CACHE_MAX_TEXT_LEN + 10000)
    assert len(giant) > _REDACT_LARGE_CACHE_MAX_TEXT_LEN

    out = _redact_fn_cached(giant)

    assert out == _redact_fn_uncached(giant)
    assert SECRET not in out
    info = helpers._redact_fn_large_lru.cache_info()
    assert info.currsize == 0, "giant string retained in cache"


def test_large_cache_is_bounded():
    assert helpers._redact_fn_large_lru.cache_info().maxsize is not None
    assert helpers._redact_fn_large_lru.cache_info().maxsize <= 128
