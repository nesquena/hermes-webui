"""Regression: `_redact_text` decision memo must be byte-identical to the
uncached path, bounded, and safe on toggle.

Companion to the #5204 redactor memo contract: the perf(conversation-switch)
change routes `_redact_text` through a per-string memo of the entire
clean-or-redacted decision (prefilter + redactor) in two size tiers. Locks:
  * memoized results are byte-identical to `_redact_text_impl` (no behavior
    change from caching),
  * repeat calls hit the memo (that is the point),
  * strings above the big-tier ceiling stay uncached yet still redact,
  * enabled=False bypasses the memo entirely (cache only ever holds
    enabled=True results, so no staleness on toggle).
"""
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


def test_redact_memo_caps_defaulted_and_env_tunable(monkeypatch):
    # The process-wide memos are bounded by LRU maxsize PER TIER (memory cannot
    # grow unboundedly), and the shipped defaults are conservative (greptile P1).
    # Env vars can raise/lower them, but each tier is clamped to its byte budget.
    assert H._redact_fn_lru.cache_info().maxsize == 16384
    assert H._redact_text_lru.cache_info().maxsize == 16384
    assert H._redact_text_big_lru.cache_info().maxsize == 256

    # _lru_size: positive int from env, clamped to `cap`, else the default.
    assert H._lru_size(100, "PI_TEST_REDACT_MEMO_MISSING", 200) == 100
    monkeypatch.setenv("PI_TEST_REDACT_MEMO_MISSING", "5")
    assert H._lru_size(100, "PI_TEST_REDACT_MEMO_MISSING", 200) == 5
    monkeypatch.setenv("PI_TEST_REDACT_MEMO_MISSING", "0")
    assert H._lru_size(100, "PI_TEST_REDACT_MEMO_MISSING", 200) == 100  # <1 rejected
    monkeypatch.setenv("PI_TEST_REDACT_MEMO_MISSING", "not-a-number")
    assert H._lru_size(100, "PI_TEST_REDACT_MEMO_MISSING", 200) == 100  # invalid rejected
    # A fat-fingered huge value is clamped to the per-tier cap (greptile P1: the
    # big tier must not allow 131072 * 256KiB ≈ 32GiB).
    monkeypatch.setenv("PI_TEST_REDACT_MEMO_MISSING", "999999999")
    assert H._lru_size(100, "PI_TEST_REDACT_MEMO_MISSING", 200) == 200
    # Big-tier ceiling is byte-budget derived from the REAL per-entry caps and
    # far below a shared-count cap (greptile P1). This is not tautological: the
    # caps are derived from _REDACT_CACHE_MAX_TEXT_LEN / _REDACT_TEXT_BIG_CACHE_MAX
    # (the values the caches actually enforce), so if those drift the assertion fails.
    assert H._REDACT_BIG_TIER_CAP < 131072
    assert H._redact_text_big_lru.cache_info().maxsize <= H._REDACT_BIG_TIER_CAP
    assert H._REDACT_SMALL_TIER_CAP == H._REDACT_MEMO_BYTE_BUDGET // (2 * H._REDACT_CACHE_MAX_TEXT_LEN)
    assert H._REDACT_BIG_TIER_CAP == H._REDACT_MEMO_BYTE_BUDGET // (2 * H._REDACT_TEXT_BIG_CACHE_MAX)
