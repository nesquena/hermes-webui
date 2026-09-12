"""The memoized prefilter must stay EXACTLY equivalent to the raw scan.

`_might_contain_sensitive_text` is the gate in front of the whole redaction
pass: a string it rejects is never inspected for secrets. Caching its verdict
is only acceptable if the answer is identical for every input -- a single
missed marker would leak a credential into an API response.

These tests pin the equivalence against a reference implementation that is a
literal copy of the pre-optimisation logic, so a future edit to the marker
lists or the cache wrapper is caught here rather than in production.

They must not leave global state behind: the LRU is shared process-wide, so
each test that touches it restores it (see the autouse fixture).
"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402
from api.helpers import (  # noqa: E402
    _SENSITIVE_CASE_MARKERS,
    _SENSITIVE_LOWER_MARKERS,
    _might_contain_sensitive_text,
)


@pytest.fixture(autouse=True)
def _isolate_prefilter_cache():
    """Keep the shared LRU out of other tests' way, before and after."""
    helpers._might_contain_sensitive_text_lru.cache_clear()
    yield
    helpers._might_contain_sensitive_text_lru.cache_clear()


def _reference(text) -> bool:
    """The exact pre-optimisation implementation, kept as the oracle."""
    if not isinstance(text, str) or not text:
        return False
    if any(marker in text for marker in _SENSITIVE_CASE_MARKERS):
        return True
    lower = text.lower()
    if any(marker in lower for marker in _SENSITIVE_LOWER_MARKERS):
        return True
    if ":" in text and helpers._SENSITIVE_TELEGRAM_MARKER_RE.search(text):
        return True
    if "<@" in text and helpers._SENSITIVE_DISCORD_MARKER_RE.search(text):
        return True
    if "+" in text and helpers._SENSITIVE_PHONE_MARKER_RE.search(text):
        return True
    return False


def test_every_case_marker_is_still_detected():
    """Each individual marker must trip the filter, alone and in context."""
    for marker in _SENSITIVE_CASE_MARKERS:
        assert _might_contain_sensitive_text(marker), marker
        assert _might_contain_sensitive_text(f"noise {marker} noise"), marker


def test_every_lower_marker_is_still_detected():
    for marker in _SENSITIVE_LOWER_MARKERS:
        assert _might_contain_sensitive_text(marker), marker
        # Upper-casing must not hide it: that is what the .lower() pass is for.
        assert _might_contain_sensitive_text(f"NOISE {marker.upper()} NOISE"), marker


def test_matches_reference_on_marker_corpus():
    """Differential test over markers, cases and embeddings."""
    corpus = []
    for marker in list(_SENSITIVE_CASE_MARKERS) + list(_SENSITIVE_LOWER_MARKERS):
        corpus += [
            marker,
            marker.upper(),
            marker.lower(),
            marker.swapcase(),
            f"prefix{marker}",
            f"{marker}suffix",
            f"a b {marker} c d",
            marker[:-1] if len(marker) > 1 else marker,  # near-miss
        ]
    for text in corpus:
        assert _might_contain_sensitive_text(text) == _reference(text), repr(text)


def test_matches_reference_on_clean_and_edge_inputs():
    samples = [
        "", "hello world", "a" * 10_000, "\n\t ", "ré­sumé accentué",
        "https", "http", "user@example.com", "1234567890",
        "+33612345678",                       # phone marker
        "<@123456789012345678>",              # discord marker
        "1234567890:AAHnotarealtelegramtokenvaluehere123",  # telegram marker
        "İstanbul", "ﬁle", "K",              # unicode lowercase traps
        "not_a_secret_at_all", "{}", "[]", "null",
    ]
    for text in samples:
        assert _might_contain_sensitive_text(text) == _reference(text), repr(text)


def test_matches_reference_on_random_fuzz():
    """Random strings built from marker fragments must agree with the oracle."""
    rnd = random.Random(20260824)
    alphabet = "abcXYZ_-.:/+<@0189{}\"' \n"
    fragments = [m[: rnd.randint(1, len(m))] for m in _SENSITIVE_CASE_MARKERS]
    fragments += [m[: rnd.randint(1, len(m))] for m in _SENSITIVE_LOWER_MARKERS]
    for _ in range(4000):
        n = rnd.randint(0, 40)
        text = "".join(rnd.choice(alphabet) for _ in range(n))
        if rnd.random() < 0.4 and fragments:
            frag = rnd.choice(fragments)
            pos = rnd.randint(0, len(text))
            text = text[:pos] + frag + text[pos:]
        assert _might_contain_sensitive_text(text) == _reference(text), repr(text)


def test_repeated_calls_are_stable_and_cached():
    """A cache hit must return the same verdict as the first, uncached call."""
    secret = "sk-" + "a" * 40
    clean = "just a normal sentence"
    for _ in range(5):
        assert _might_contain_sensitive_text(secret) is True
        assert _might_contain_sensitive_text(clean) is False
    info = helpers._might_contain_sensitive_text_lru.cache_info()
    assert info.hits > 0, "expected the memo to serve repeated lookups"


def test_oversized_text_bypasses_the_cache_but_keeps_the_verdict():
    """Huge strings must stay correct while never entering the LRU."""
    limit = helpers._SENSITIVE_PREFILTER_MAX_TEXT_LEN
    big_secret = "x" * (limit + 1) + "sk-" + "b" * 40
    big_clean = "y" * (limit + 100)

    before = helpers._might_contain_sensitive_text_lru.cache_info().currsize
    assert _might_contain_sensitive_text(big_secret) is True
    assert _might_contain_sensitive_text(big_clean) is False
    after = helpers._might_contain_sensitive_text_lru.cache_info().currsize
    assert after == before, "oversized strings must not be memoized"

    assert _might_contain_sensitive_text(big_secret) == _reference(big_secret)
    assert _might_contain_sensitive_text(big_clean) == _reference(big_clean)


def test_non_string_and_empty_inputs_are_rejected():
    for value in (None, 0, 1, [], {}, ()):
        assert _might_contain_sensitive_text(value) is False  # type: ignore[arg-type]
    assert _might_contain_sensitive_text("") is False


def test_unhashable_input_does_not_reach_the_cache():
    """Guard the wrapper's isinstance check: a list must not raise TypeError."""
    before = helpers._might_contain_sensitive_text_lru.cache_info().currsize
    assert _might_contain_sensitive_text(["sk-secret"]) is False  # type: ignore[arg-type]
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize == before


def test_cache_is_bounded():
    """The LRU must cap its own size rather than grow with traffic."""
    limit = helpers._SENSITIVE_PREFILTER_CACHE_SIZE
    for i in range(limit + 500):
        _might_contain_sensitive_text(f"unique-clean-string-{i}")
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize <= limit
