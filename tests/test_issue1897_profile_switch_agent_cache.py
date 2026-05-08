"""Regression checks for #1897 — same-session profile switch identity bleed.

When a user switches profiles mid-session in the WebUI, `session_id` stays
stable but the active profile's HERMES_HOME changes. The streaming layer
caches `AIAgent` instances by `session_id` plus a signature blob in
`api/streaming.py`. Before the fix, that signature did NOT include
`_profile_home`, so the second turn after a profile switch reused the agent
built under the previous profile — including its `_cached_system_prompt`
loaded from the OLD profile's SOUL.md. The new persona's identity files
never reached the LLM.

These tests exercise the source-string contract — a unit-level functional
test would require constructing a full streaming worker. Source-string
tests are sufficient because the signature blob is a single literal list
that drives the cache-hit comparison; if `_profile_home` is in that list,
profile switches force a cache miss and a fresh agent build.
"""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


def _signature_block() -> str:
    """Return the literal text of the cache-signature blob."""
    sig_start = STREAMING_PY.index("_sig_blob = _json.dumps")
    sig_end = STREAMING_PY.index("], sort_keys=True)", sig_start)
    return STREAMING_PY[sig_start:sig_end]


def test_cache_signature_includes_profile_home():
    """The cache signature must include `_profile_home` so that switching
    profiles mid-session forces a cache miss instead of reusing the
    previous profile's agent (and its baked-in SOUL.md / system prompt)."""
    block = _signature_block()
    assert "_profile_home" in block, (
        "SESSION_AGENT_CACHE signature is missing `_profile_home`. "
        "Without this, same-session profile switches reuse the cached "
        "agent built under the previous profile's HERMES_HOME, leaking "
        "the old persona's SOUL.md into the new profile's turns. "
        "See #1897."
    )


def test_profile_home_resolved_before_cache_signature():
    """The `_profile_home` variable must be assigned before the cache
    signature is built, otherwise the reference would NameError."""
    profile_home_assignment = STREAMING_PY.index("_profile_home = str(_profile_home_path)")
    sig_start = STREAMING_PY.index("_sig_blob = _json.dumps")
    assert profile_home_assignment < sig_start, (
        "`_profile_home` must be resolved before the SESSION_AGENT_CACHE "
        "signature is built. If this ordering changed, #1897 would "
        "regress with a NameError instead of an identity bleed."
    )


def test_signature_uses_profile_home_with_fallback():
    """The signature must use `_profile_home or ''` — the fallback to an
    empty string preserves cache stability when HERMES_HOME is unset
    (older deployments / single-profile installs)."""
    block = _signature_block()
    assert "_profile_home or ''" in block, (
        "Signature should use `_profile_home or ''` so that single-profile "
        "deployments (where _profile_home may be empty) get a stable "
        "cache key rather than churning on each turn."
    )
