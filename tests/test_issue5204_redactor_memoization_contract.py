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


def test_huge_tool_dump_skips_agent_pass_but_still_masks_secrets():
    """Megabyte URL-heavy dumps must not hang in agent.redact (GIL wedge)."""
    import time
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    blob = ("http://example.com/foo " * 40000) + " " + secret
    assert len(blob) > helpers._REDACT_AGENT_MAX_TEXT_LEN
    t0 = time.monotonic()
    out = helpers._redact_fn_cached(blob)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, f"huge-dump redact took {elapsed:.2f}s"
    assert secret not in out
    assert out == helpers._redact_fn_uncached(blob)


def test_residual_shapes_masked_above_agent_cap():
    """The >16KB agent-pass bypass no longer leaks the prefix-less shapes.

    Above _REDACT_AGENT_MAX_TEXT_LEN a single field skips the expensive agent
    redactor and only the local fallback runs. The fallback now also masks the
    three prefix-less agent-only shapes (bare JWT eyJ..., DB connection-string
    passwords, Telegram bot tokens) via _JWT_RE/_URI_USERINFO_RE/_TELEGRAM_RE,
    so they stay masked in an oversize field. This test pins that both below and
    above the cap. If a future change stops masking these above the cap, update
    the code comment on _REDACT_AGENT_MAX_TEXT_LEN too.
    """
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    db_pw = "s3cr3tDbPassw0rd"
    connstr = f"postgres://dbuser:{db_pw}@db.internal:5432/prod"
    tg_token = "AAHdqTcvbXsj2kd83jfhs8sJDHf83jfhsu2"
    telegram = f"123456789:{tg_token}"
    filler = "x" * (helpers._REDACT_AGENT_MAX_TEXT_LEN + 100)

    # Below the cap the agent pass runs and masks all three shapes.
    small = f"start {jwt} {connstr} {telegram} end"
    small_out = helpers._redact_fn_uncached(small)
    assert jwt not in small_out
    assert db_pw not in small_out
    assert tg_token not in small_out

    # Above the cap the agent pass is skipped, but the fallback now covers these.
    big = f"{filler} {jwt} {connstr} {telegram}"
    assert len(big) > helpers._REDACT_AGENT_MAX_TEXT_LEN
    big_out = helpers._redact_fn_uncached(big)
    assert jwt not in big_out, "JWT leaked above cap - fallback JWT pass missing"
    assert db_pw not in big_out, "DB-connstr pw leaked above cap - fallback URI pass missing"
    assert tg_token not in big_out, "Telegram token leaked above cap - fallback pass missing"
    # Structure is preserved: only the secret is masked, not the surrounding URL.
    assert "postgres://dbuser:" in big_out
    assert "@db.internal:5432/prod" in big_out
    assert "123456789:" in big_out

    # A short-prefix secret in the same oversize field also stays masked (the
    # fallback prefix list keeps the common shapes safe above the cap).
    sk = "sk-ABCDEFGHIJKLMNOP1234567890"
    assert sk not in helpers._redact_fn_uncached(f"{filler} {sk}")

    # False-positive guard: benign URLs / number-colon prose are untouched.
    benign = "See http://localhost:8080/api and ratio 12345:67 at 2026-09-01T18:11:00"
    assert helpers._redact_fn_uncached(benign) == benign
