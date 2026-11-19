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

The cache retains its keys, so it is also bounded in BYTES, not just in entry
count: ``sys.getsizeof(text)`` gates every entry (a character cap is not a
byte bound -- 16k four-byte code points is ~64 KiB) and the entry cap gives a
hard aggregate ceiling. The memory tests below pin that.
"""

import random
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import helpers  # noqa: E402
from api.helpers import (  # noqa: E402
    _SENSITIVE_CASE_MARKERS,
    _SENSITIVE_LOWER_MARKERS,
    _SENSITIVE_PREFILTER_CACHE_SIZE,
    _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES,
    _SENSITIVE_PREFILTER_MAX_RETAINED_KEY_BYTES,
    _might_contain_sensitive_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Widest code point CPython stores (UCS-4): every character costs 4 bytes.
_WIDE = "\U0001d54f"


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
    limit = _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES
    big_secret = "x" * limit + "sk-" + "b" * 40
    big_clean = "y" * (limit + 100)
    assert sys.getsizeof(big_secret) > limit and sys.getsizeof(big_clean) > limit

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
    limit = _SENSITIVE_PREFILTER_CACHE_SIZE
    for i in range(limit + 500):
        _might_contain_sensitive_text(f"unique-clean-string-{i}")
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize <= limit


# ── Byte bound regressions ─────────────────────────────────────────────────


def _retained_key_bytes(inserted_in_order: list) -> int:
    """Actual ``sys.getsizeof`` sum of the keys the LRU currently holds.

    ``functools.lru_cache`` exposes no key iterator, but it is a strict LRU
    and the tests below never re-touch a key before calling this, so the
    resident set is exactly the last ``currsize`` keys that were inserted.
    """
    info = helpers._might_contain_sensitive_text_lru.cache_info()
    resident = inserted_in_order[len(inserted_in_order) - info.currsize:]
    assert len(resident) == info.currsize
    return sum(sys.getsizeof(key) for key in resident)


def test_entry_gate_is_object_bytes_not_characters():
    """A short string of 4-byte code points must be judged on its byte size."""
    max_ascii = "a" * (_SENSITIVE_PREFILTER_MAX_ENTRY_BYTES - sys.getsizeof(""))
    assert sys.getsizeof(max_ascii) <= _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES
    # Same CHARACTER count in the widest storage is ~4x the bytes: must bypass.
    wide_same_len = _WIDE * len(max_ascii)
    assert len(wide_same_len) == len(max_ascii)
    assert sys.getsizeof(wide_same_len) > _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES

    before = helpers._might_contain_sensitive_text_lru.cache_info().currsize
    assert _might_contain_sensitive_text(max_ascii) is False
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize == before + 1
    assert _might_contain_sensitive_text(wide_same_len) is False
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize == before + 1, (
        "a wide-character string above the byte gate was retained"
    )
    assert _might_contain_sensitive_text(wide_same_len) == _reference(wide_same_len)


def test_maximum_width_unicode_fills_cache_within_byte_budget():
    """Fill the cache with the largest admissible 4-byte-per-char keys and
    check the aggregate stays under the hard byte budget."""
    # Largest wide string that still passes the gate.
    n = (_SENSITIVE_PREFILTER_MAX_ENTRY_BYTES - sys.getsizeof(_WIDE)) // 4 + 1
    while sys.getsizeof(_WIDE * n) > _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES:
        n -= 1
    assert n > 0
    base = _WIDE * n
    assert sys.getsizeof(base) <= _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES

    # Unique keys: vary the LAST code point (keeps width 4, same byte size).
    total = _SENSITIVE_PREFILTER_CACHE_SIZE + 64
    keys = [base[:-1] + chr(0x1D400 + i) for i in range(total)]
    assert all(sys.getsizeof(k) == sys.getsizeof(base) for k in keys[:8])
    for key in keys:
        assert _might_contain_sensitive_text(key) is False

    info = helpers._might_contain_sensitive_text_lru.cache_info()
    assert info.currsize == _SENSITIVE_PREFILTER_CACHE_SIZE
    retained = _retained_key_bytes(keys)
    assert retained <= _SENSITIVE_PREFILTER_MAX_RETAINED_KEY_BYTES
    # The bound is tight, not slack: max-width keys fill most of it.
    assert retained > _SENSITIVE_PREFILTER_MAX_RETAINED_KEY_BYTES * 0.95
    # Concretely: 16 MiB, not the ~512 MiB a character cap would have allowed.
    assert _SENSITIVE_PREFILTER_MAX_RETAINED_KEY_BYTES == 16 * 1024 * 1024


def test_sensitive_output_pairs_are_byte_gated_too():
    """A SENSITIVE verdict is cached under the same byte gate: an oversized
    secret-bearing string is neither retained nor misjudged, and an
    admissible one is cached with the right verdict."""
    small_secret = "token=" + "s" * 40
    assert sys.getsizeof(small_secret) <= _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES
    wide_secret = _WIDE * 600 + " sk-" + "c" * 40
    assert sys.getsizeof(wide_secret) > _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES

    before = helpers._might_contain_sensitive_text_lru.cache_info().currsize
    assert _might_contain_sensitive_text(small_secret) is True
    assert _might_contain_sensitive_text(small_secret) is True
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize == before + 1
    assert helpers._might_contain_sensitive_text_lru.cache_info().hits >= 1

    assert _might_contain_sensitive_text(wide_secret) is True
    assert _might_contain_sensitive_text(wide_secret) is True
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize == before + 1, (
        "oversized sensitive string retained"
    )
    assert _might_contain_sensitive_text(wide_secret) == _reference(wide_secret)


def test_eviction_keeps_aggregate_under_budget_and_verdicts_correct():
    """Churn well past the entry cap with max-size keys of mixed verdicts:
    the cache must evict (never exceed the cap) and every verdict must still
    match the oracle, including for keys that were evicted and re-scanned."""
    fill = _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES - sys.getsizeof("") - 64
    total = _SENSITIVE_PREFILTER_CACHE_SIZE * 2 + 17
    peak = 0
    inserted = []
    samples = []
    for i in range(total):
        tag = f"{i:08d}"
        if i % 3 == 0:
            text = "k" * fill + " api_key=" + tag
        else:
            text = "k" * fill + " plain " + tag
        assert sys.getsizeof(text) <= _SENSITIVE_PREFILTER_MAX_ENTRY_BYTES
        assert _might_contain_sensitive_text(text) == _reference(text)
        inserted.append(text)
        peak = max(peak, helpers._might_contain_sensitive_text_lru.cache_info().currsize)
        if i % 997 == 0:
            samples.append(text)
    assert peak <= _SENSITIVE_PREFILTER_CACHE_SIZE
    assert helpers._might_contain_sensitive_text_lru.cache_info().currsize == _SENSITIVE_PREFILTER_CACHE_SIZE
    assert _retained_key_bytes(inserted) <= _SENSITIVE_PREFILTER_MAX_RETAINED_KEY_BYTES
    # Early samples were evicted; re-querying them must still be correct.
    misses_before = helpers._might_contain_sensitive_text_lru.cache_info().misses
    for text in samples:
        assert _might_contain_sensitive_text(text) == _reference(text)
    evicted = [t for t in samples if t not in inserted[-_SENSITIVE_PREFILTER_CACHE_SIZE:]]
    assert evicted, "test did not churn past the cap"
    assert helpers._might_contain_sensitive_text_lru.cache_info().misses - misses_before >= len(evicted)


_LOW_MEMORY_PROBE = r"""
import resource, sys
sys.path.insert(0, sys.argv[1])
from api import helpers
# Address-space headroom well below the ~512 MiB a character-capped cache of
# wide keys would pin, but comfortably above the byte-bounded 16 MiB ceiling.
headroom = int(sys.argv[2]) * 1024 * 1024
soft, hard = resource.getrlimit(resource.RLIMIT_AS)
current = 0
for line in open("/proc/self/status"):
    if line.startswith("VmSize:"):
        current = int(line.split()[1]) * 1024
resource.setrlimit(resource.RLIMIT_AS, (current + headroom, hard))
wide = "\U0001d54f"
n = helpers._SENSITIVE_PREFILTER_MAX_ENTRY_BYTES // 4
total = helpers._SENSITIVE_PREFILTER_CACHE_SIZE + 256
entered = 0
for i in range(total):
    # Unique wide key of ~16k characters (~64 KiB each): what the old
    # character cap admitted. Must be scanned, must NOT be retained.
    text = wide * 16000 + chr(0x1D400 + (i % 500)) + f"{i:06d}"
    verdict = helpers._might_contain_sensitive_text(text)
    assert verdict is False, verdict
    # And a unique key that DOES pass the byte gate, to keep the cache busy.
    small = wide * (n - 8) + f"{i:06d}"
    assert helpers._might_contain_sensitive_text(small) is False
    entered += 1
info = helpers._might_contain_sensitive_text_lru.cache_info()
assert info.currsize <= helpers._SENSITIVE_PREFILTER_CACHE_SIZE, info
print("OK", entered, info.currsize)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS + /proc probe is Linux-only")
def test_low_memory_headroom_survives_wide_unicode_churn():
    """Under ~96 MiB of address-space headroom, pushing more than the entry
    cap of 16k-character wide strings through the prefilter must complete
    without MemoryError. Before the byte gate this workload retained ~512 MiB
    and died after a few thousand entries."""
    proc = subprocess.run(
        [sys.executable, "-c", _LOW_MEMORY_PROBE, str(REPO_ROOT), "96"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert proc.stdout.strip().startswith("OK"), proc.stdout
