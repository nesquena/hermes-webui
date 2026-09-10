"""Regression: `_redact_text` decision memo must be byte-identical to the
uncached path, byte-budgeted, and safe on toggle.

Companion to the #5204 redactor memo contract and #7439 byte-budgeted eviction:
the perf(conversation-switch) change routes `_redact_text` through a per-string
memo of the entire clean-or-redacted decision (prefilter + redactor) in two size tiers.
Worst-case retained memory equals the configured byte budget by construction.
Locks:
  * memoized results are byte-identical to `_redact_text_impl` (no behavior
    change from caching),
  * repeat calls hit the memo (that is the point),
  * strings above the big-tier ceiling stay uncached yet still redact,
  * enabled=False bypasses the memo entirely (cache only ever holds
    enabled=True results, so no staleness on toggle),
  * byte budget is strictly enforced (retained_bytes <= max_bytes at all times),
  * aliased clean strings (key is value) are accounted once,
  * thread-safe under concurrent access.
"""
import sys
import threading
from api import helpers as H

_SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def test_redact_text_matches_uncached_decision_all_sizes():
    small = f"note {_SECRET} tail"                                   # small tier
    big = ("x" * (H._REDACT_CACHE_MAX_TEXT_LEN + 100)) + f" {_SECRET}"   # big tier
    giant = ("y" * (H._REDACT_TEXT_BIG_CACHE_MAX + 100)) + f" {_SECRET}"  # uncached
    for s in (small, big, giant):
        assert H._redact_text(s, _enabled=True) == H._redact_text_impl(s)
        assert _SECRET not in H._redact_text(s, _enabled=True)


def test_redact_text_repeat_hits_memo():
    sample = f"session {getattr(H, '_REDACT_CACHE_MAX_TEXT_LEN', 0)} {_SECRET} repeat"
    H._redact_text_lru.cache_clear()
    first = H._redact_text(sample, _enabled=True)
    hits_before = H._redact_text_lru.cache_info().hits
    second = H._redact_text(sample, _enabled=True)
    assert first == second
    assert H._redact_text_lru.cache_info().hits == hits_before + 1


def test_redact_text_benign_string_returned_verbatim():
    benign = "plain conversation text with no credential shapes at all"
    assert H._redact_text(benign, _enabled=True) == benign
    assert H._redact_text_impl(benign) == benign


def test_redact_text_disabled_bypasses_memo():
    sample = f"disabled path {_SECRET} not memoized"
    H._redact_text_lru.cache_clear()
    hits_before = H._redact_text_lru.cache_info().hits
    misses_before = H._redact_text_lru.cache_info().misses
    out = H._redact_text(sample, _enabled=False)
    assert out == sample  # disabled = verbatim, no redaction
    info = H._redact_text_lru.cache_info()
    assert (info.hits, info.misses) == (hits_before, misses_before)


def test_redact_text_giant_above_ceiling_not_memoized():
    giant = ("z" * (H._REDACT_TEXT_BIG_CACHE_MAX + 1)) + f" {_SECRET}"
    H._redact_text_big_lru.cache_clear()
    before = H._redact_text_big_lru.cache_info().misses
    out = H._redact_text(giant, _enabled=True)
    assert _SECRET not in out
    assert H._redact_text_big_lru.cache_info().misses == before  # tier skipped


def test_redact_memo_budgets_defaulted_and_env_tunable(monkeypatch):
    # Process-wide memos are bounded by byte budgets per tier (#7439)
    assert H._redact_fn_lru.cache_info().max_bytes == 512 * 1024 * 1024
    assert H._redact_text_lru.cache_info().max_bytes == 512 * 1024 * 1024
    assert H._redact_text_big_lru.cache_info().max_bytes == 128 * 1024 * 1024

    # _byte_budget: positive int from env, clamped to `cap`, else default
    assert H._byte_budget(100, "PI_TEST_REDACT_BUDGET_MISSING", 200) == 100
    monkeypatch.setenv("PI_TEST_REDACT_BUDGET_MISSING", "5")
    assert H._byte_budget(100, "PI_TEST_REDACT_BUDGET_MISSING", 200) == 5
    monkeypatch.setenv("PI_TEST_REDACT_BUDGET_MISSING", "0")
    assert H._byte_budget(100, "PI_TEST_REDACT_BUDGET_MISSING", 200) == 100  # <1 rejected
    monkeypatch.setenv("PI_TEST_REDACT_BUDGET_MISSING", "not-a-number")
    assert H._byte_budget(100, "PI_TEST_REDACT_BUDGET_MISSING", 200) == 100  # invalid rejected
    monkeypatch.setenv("PI_TEST_REDACT_BUDGET_MISSING", "999999999999")
    assert H._byte_budget(100, "PI_TEST_REDACT_BUDGET_MISSING", 200) == 200  # clamped to cap

    # Cap invariants preserved
    assert H._REDACT_MEMO_BYTE_BUDGET == 1024 * 1024 * 1024
    assert H._REDACT_SMALL_TIER_CAP == H._REDACT_MEMO_BYTE_BUDGET // (2 * H._REDACT_CACHE_MAX_TEXT_LEN)
    assert H._REDACT_BIG_TIER_CAP == H._REDACT_MEMO_BYTE_BUDGET // (2 * H._REDACT_TEXT_BIG_CACHE_MAX)


def test_byte_budget_lru_eviction_and_accounting():
    # Construct a small ByteBudgetLRU to verify exact byte accounting & eviction
    cache = H.byte_budget_lru_cache(max_bytes=1000, name="test_lru")(lambda s: s)

    # Clean string: key is val -> single payload + tuple overhead + measured dict growth
    s1 = "hello_world_1"
    cache(s1)
    expected_bytes_s1 = (
        sys.getsizeof(s1)
        + H._TUPLE_OVERHEAD_BYTES
        + (sys.getsizeof(cache._data) - H._BASE_DICT_BYTES)
    )
    info1 = cache.cache_info()
    assert info1.retained_bytes == expected_bytes_s1
    assert info1.currsize == 1

    # Redacted string: key is not val -> dual payload + tuple overhead + measured dict growth
    cache_redact = H.byte_budget_lru_cache(max_bytes=1000, name="test_redact")(lambda s: s.replace("secret", "xxx"))
    s2 = "this_has_a_secret_here"
    val2 = cache_redact(s2)
    expected_bytes_s2 = (
        sys.getsizeof(s2)
        + sys.getsizeof(val2)
        + H._TUPLE_OVERHEAD_BYTES
        + (sys.getsizeof(cache_redact._data) - H._BASE_DICT_BYTES)
    )
    info2 = cache_redact.cache_info()
    assert info2.retained_bytes == expected_bytes_s2
    assert info2.currsize == 1

    # Exact 2-entry shape: two 100-char strings = 298B + two 56B tuples = 112B + dict growth = 248B -> exactly 658B
    # Budget of 700B accommodates exactly 2 entries (658B <= 700B) and evicts on the 3rd.
    tight_cache = H.byte_budget_lru_cache(max_bytes=700, name="tight")(lambda s: s)
    e1 = "a" * 100
    e2 = "b" * 100
    e3 = "c" * 100

    tight_cache(e1)
    tight_cache(e2)
    assert tight_cache.cache_info().currsize == 2
    # Exact shallow growth matches 658 bytes
    assert tight_cache.cache_info().retained_bytes == 658
    assert tight_cache.cache_info().retained_bytes <= 700

    # Access e1 again to make e2 the LRU
    tight_cache(e1)

    # Insert e3 -> should evict e2, retaining e1 and e3
    tight_cache(e3)
    info_tight = tight_cache.cache_info()
    assert info_tight.currsize == 2
    # Discriminating assertion on surviving keys
    assert list(tight_cache._data.keys()) == [e1, e3]
    assert info_tight.retained_bytes == 658
    assert info_tight.retained_bytes <= 700

    # Cache clear resets retained_bytes and currsize
    tight_cache.cache_clear()
    cleared_info = tight_cache.cache_info()
    assert cleared_info.currsize == 0
    assert cleared_info.retained_bytes == 0
    assert cleared_info.hits == 0
    assert cleared_info.misses == 0


def test_byte_budget_lru_resize_boundaries_and_compaction():
    """Verify shallow accounting across dict resize boundaries and compaction on eviction."""
    cache = H.byte_budget_lru_cache(max_bytes=50000, name="resize_test")(lambda s: s + "_out")

    # Step across multiple OrderedDict resize boundaries (5, 10, 20, 50, 100 entries)
    for n in (5, 10, 20, 50, 100):
        for i in range(len(cache._data), n):
            cache(f"key_{i:04d}")
        assert cache.cache_info().currsize == n
        # Assert exact measured shallow retained bytes matches primary source formula
        expected_shallow = (
            sum(sys.getsizeof(k) + sys.getsizeof(v) for k, (v, _) in cache._data.items())
            + (len(cache._data) * H._TUPLE_OVERHEAD_BYTES)
            + max(0, sys.getsizeof(cache._data) - H._BASE_DICT_BYTES)
        )
        assert cache.cache_info().retained_bytes == expected_shallow
        assert cache.cache_info().retained_bytes <= 50000

    # Peak: container has grown to high-water mark (>8000 bytes for 100 entries)
    peak_dict_size = sys.getsizeof(cache._data)
    assert peak_dict_size >= 8000

    # High-water eviction: reduce max_bytes to 2000 and insert an entry forcing bulk eviction
    cache.max_bytes = 2000
    cache("forcing_eviction_key")

    # Verify compaction took place and released table capacity slack
    compacted_dict_size = sys.getsizeof(cache._data)
    assert compacted_dict_size < 1500
    assert compacted_dict_size < peak_dict_size // 4
    info = cache.cache_info()
    assert info.retained_bytes <= 2000
    # Verify latest key survived
    assert "forcing_eviction_key" in cache._data


def test_byte_budget_lru_thread_safety():
    cache = H.byte_budget_lru_cache(max_bytes=5000, name="concurrent")(lambda s: s + "_out")
    errors = []

    def worker(worker_id):
        try:
            for i in range(50):
                k = f"key_{worker_id}_{i % 10}"
                cache(k)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    info = cache.cache_info()
    assert info.retained_bytes <= 5000
    assert info.currsize > 0
