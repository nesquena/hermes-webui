"""Large strings must NOT be memoized by input text alone.

``_redact_fn_cached`` memoizes strings up to 16,384 characters. An earlier
revision of this branch added a second LRU for larger strings, keyed only on
the text. That is unsafe: ``agent.redact.register_redaction_patterns()`` lets
a plugin extend the secret matcher at runtime, and the installed registry
exposes no policy generation the cache key could include and no hook that
could clear the cache. A large blob primed BEFORE such a registration would
keep being served with the newly-registered secret intact through session,
SSE and public-share projections.

Until the agent registry exposes a generation, strings above the small-cache
threshold fail closed: they run the full redactor on every call.

Contract verified here:
  1. a large string is redacted exactly like the uncached redactor;
  2. no module-level cache retains a large string (only the small LRU exists
     and it never sees a string above the threshold);
  3. production-registry regression: prime a large string carrying a token
     the redactor does not know yet, register a runtime pattern for it, and
     the NEXT ``redact_session_data`` response is redacted.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402
from api.helpers import (  # noqa: E402
    _REDACT_CACHE_MAX_TEXT_LEN,
    _redact_fn_cached,
    _redact_fn_uncached,
    _redact_text,
    redact_session_data,
)

SECRET = "gh" + "p_" + "0123456789abcdefghijklmnopqrstuvwxyzAB"


@pytest.fixture(autouse=True)
def _isolate_small_cache():
    """Start and finish with an empty small-string LRU."""
    helpers._redact_fn_lru.cache_clear()
    yield
    helpers._redact_fn_lru.cache_clear()


def _big(secret: str, size: int) -> str:
    """String of exactly ``size`` characters carrying ``secret``."""
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


def test_large_string_is_never_memoized():
    """Above the threshold the redactor runs every time: no cache hit, no
    cache entry, on any module-level LRU."""
    text = _big(SECRET, _REDACT_CACHE_MAX_TEXT_LEN + 7000)

    first = _redact_fn_cached(text)
    second = _redact_fn_cached(text)

    assert second == first
    info = helpers._redact_fn_lru.cache_info()
    assert info.hits == 0 and info.misses == 0 and info.currsize == 0, (
        "large string reached the small-string LRU"
    )
    assert not hasattr(helpers, "_redact_fn_large_lru"), (
        "a text-keyed large-string cache is back; it must carry a redaction "
        "policy generation before it can exist"
    )


def test_small_strings_still_use_the_small_cache():
    small = f"small text {SECRET}"
    assert len(small) <= _REDACT_CACHE_MAX_TEXT_LEN
    first = _redact_fn_cached(small)
    second = _redact_fn_cached(small)
    assert first == second
    assert SECRET not in first
    assert helpers._redact_fn_lru.cache_info().hits >= 1


# ── Production-registry regression ─────────────────────────────────────────
# Uses the REAL installed agent.redact registry (skipped when the agent is not
# importable): the module-level `_redact_fn_uncached` is `_combined_redact`
# only when `agent.redact` imported at helpers import time.

_RUNTIME_TOKEN = "nvapi-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6A7b8C9d0"
_RUNTIME_PATTERN = r"nvapi-[A-Za-z0-9]{60}"


@pytest.fixture
def agent_registry():
    redact = pytest.importorskip("agent.redact", reason="hermes-agent not installed")
    if not hasattr(redact, "register_redaction_patterns"):
        pytest.skip("installed agent.redact has no runtime pattern registry")
    if helpers._redact_fn_uncached.__name__ != "_combined_redact":
        pytest.skip("api.helpers was built without the agent redactor")
    reset = getattr(redact, "_reset_plugin_redaction_patterns", None)
    if reset is None:
        pytest.skip("installed agent.redact has no registry reset seam")
    reset()
    yield redact
    reset()


def test_runtime_pattern_registration_is_honored_for_large_strings(agent_registry):
    """Prime → register → the next response must be redacted (fail-closed)."""
    # A marker the WebUI prefilter already trips on, so the large string goes
    # through the redactor both before and after registration.
    text = ("see https://example.invalid/docs lorem ipsum " * 500) + _RUNTIME_TOKEN + " tail"
    assert len(text) > _REDACT_CACHE_MAX_TEXT_LEN
    assert helpers._might_contain_sensitive_text(text)

    # Sanity: the installed registry does not know this token shape yet.
    # If it ever does, the test still proves the cache path stays honest.
    primed = _redact_text(text, _enabled=True)
    session_before = redact_session_data(
        {"messages": [{"role": "assistant", "content": text}]}
    )
    assert session_before["messages"][0]["content"] == primed

    accepted = agent_registry.register_redaction_patterns([_RUNTIME_PATTERN], source="test-7276")
    assert accepted == 1, "the registry rejected the probe pattern"

    oracle = _redact_fn_uncached(text)
    assert _RUNTIME_TOKEN not in oracle, "the registered pattern is not effective"

    assert _redact_text(text, _enabled=True) == oracle
    session_after = redact_session_data(
        {"messages": [{"role": "assistant", "content": text}]}
    )
    assert _RUNTIME_TOKEN not in session_after["messages"][0]["content"]
    assert session_after["messages"][0]["content"] == oracle
