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


# ── #7345 review: oversized fields must keep the full Agent boundary ─────────
# Secrets below are assembled at runtime so no literal credential sits in the
# source. Every assertion checks that the sensitive atom is ABSENT.
import pytest

_BOUNDARY_LENS = (16384, 16385, 200_000)


def _pad_to(payload: str, total: int) -> str:
    filler = "x" * max(0, total - len(payload) - 1)
    out = filler + " " + payload
    assert len(out) >= total
    return out


def _agent():
    return pytest.importorskip("agent.redact")


def _real_redactor():
    # Production composition: rebuild from the real Agent rather than any
    # cached instance a sibling test may have swapped in.
    return helpers._build_redact_fn()


@pytest.mark.parametrize("total", _BOUNDARY_LENS)
def test_registered_vault_value_scrubbed_at_and_above_16k(total):
    agent = _agent()
    if not hasattr(agent, "register_vault_redaction_value"):
        pytest.skip("installed hermes-agent predates the vault registry")
    vault_pw = "Plain" + "Vault" + "Passw0rd" + "Q7z"  # no known prefix shape
    agent.register_vault_redaction_value(vault_pw)
    try:
        redact = _real_redactor()
        for text in (_pad_to(f"login ok {vault_pw}", total), f"short {vault_pw}"):
            assert vault_pw not in redact(text)
    finally:
        agent.clear_vault_redaction_values()


def _agent_only_shapes():
    tok = "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"
    return {
        "zhipu": ("0123456789abcdef0123456789abcdef" + "." + "AbCdEfGhIjKlMnOp", "AbCdEfGhIjKlMnOp"),
        "config_assign": ("db_password: " + "Hunter2" + "Correct" + "Horse9", "Hunter2CorrectHorse9"),
        "json_field": ('{"api_key": "' + "zq" + tok + '"}', "zq" + tok),
        "api_key_header": ("X-API-Key: " + "kk" + tok, "kk" + tok),
        "jwt_one_part": ("eyJ" + "hbGciOiJIUzI1NiJ9xyzzy", "hbGciOiJIUzI1NiJ9xyzzy"),
        "jwt_two_part": ("eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "c2lnbmF0dXJlX19f", "c2lnbmF0dXJlX19f"),
        "db_uri": ("postgres://svc:" + "Db" + "Pw" + "9q8w7e6r" + "@db.internal/prod", "DbPw9q8w7e6r"),
        "telegram": ("123456789:" + "AAH" + tok, "AAH" + tok),
    }


@pytest.mark.parametrize("total", _BOUNDARY_LENS)
@pytest.mark.parametrize("shape", sorted(_agent_only_shapes()))
def test_webui_matches_agent_boundary_for_oversized_fields(shape, total):
    """Parity: whatever the Agent's public force=True redactor masks, WebUI
    masks too, on both sides of the old 16KB cap."""
    agent = _agent()
    payload, atom = _agent_only_shapes()[shape]
    text = _pad_to(payload, total)
    if atom in agent.redact_sensitive_text(text, force=True):
        pytest.skip(f"installed hermes-agent does not cover {shape}")
    assert atom not in _real_redactor()(text)


def test_no_private_agent_regex_fast_path():
    """WebUI must not reach into private Agent regex internals (#7345 review)."""
    import inspect
    src = inspect.getsource(helpers._build_redact_fn)
    for private in ('"_PREFIX_RE"', "_agent_prefix_only_redact", "_has_known_prefix_substring", "_mask_control_split_tokens"):
        assert private not in src


def test_fallback_masks_prefixless_shapes_without_agent():
    """Defense-in-depth: the no-agent fallback also covers JWT / URI userinfo /
    Telegram, preserving surrounding structure."""
    import builtins
    real_import = builtins.__import__

    def no_agent(name, *a, **k):
        if name == "agent.redact" or name.startswith("agent.redact"):
            raise ImportError("simulated: no hermes-agent")
        return real_import(name, *a, **k)

    builtins.__import__ = no_agent
    try:
        fallback = helpers._build_redact_fn()
    finally:
        builtins.__import__ = real_import

    jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxIn0" + "." + "c2lnbmF0dXJlX19fX19f"
    db_pw = "s3cr3t" + "DbPassw0rd"
    tg = "AAH" + "dqTcvbXsj2kd83jfhs8sJDHf83jfhsu2"
    out = fallback(f"{jwt} postgres://dbuser:{db_pw}@db.internal:5432/prod 123456789:{tg}")
    assert jwt not in out and db_pw not in out and tg not in out
    assert "postgres://dbuser:" in out and "@db.internal:5432/prod" in out and "123456789:" in out
    benign = "See http://localhost:8080/api and ratio 12345:67 at 2026-09-01T18:11:00"
    assert fallback(benign) == benign


def test_fallback_prefixless_passes_are_linear_on_huge_runs():
    """The JWT / URI-userinfo / Telegram fallback regexes must not go
    quadratic on a multi-MB alphanumeric run (that would re-create the very
    /api/session GIL wedge this redactor sits on)."""
    import time
    redact = helpers._build_redact_fn()
    for blob in ("x" * 400_000, "1" * 400_000, "ab-" * 130_000):
        t0 = time.monotonic()
        redact(blob)
        assert time.monotonic() - t0 < 5.0
