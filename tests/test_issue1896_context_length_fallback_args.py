"""Regression checks for #1896 — context-length fallback ignores config overrides.

The two `get_model_context_length()` fallback callsites in `api/streaming.py`
(one for session persistence around line ~2950, one for the SSE usage payload
around line ~3050) were calling the resolver with only `model + base_url`,
omitting `config_context_length`, `provider`, and `custom_providers`.

When the agent's `context_compressor` reports 0 (fresh / cached / transitioning
agent), context-length resolution falls all the way through to
`DEFAULT_FALLBACK_CONTEXT = 256_000` even when the user has set
`model.context_length: 1048576` in `config.yaml` or has a 1M model with a
`custom_providers` per-model override.

For users with a context-management plugin (LCM) configured around the real
window, this cascades into a session-killing failure mode: auto-compression
triggers far too early → flood of compress requests → 429s → credential pool
exhaustion → fallback also 429s → "API call failed after 3 retries".

These tests pin the call shape so future refactors can't silently drop the
config-override args again.
"""

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")


# Both fallback callsites must pass these kwargs into get_model_context_length.
_REQUIRED_KWARGS = (
    "config_context_length=_cfg_ctx_len",
    "provider=resolved_provider or ''",
    "custom_providers=_cfg_custom_providers",
)


def _both_callsites():
    """Return the two PRIMARY `get_model_context_length(...)` callsites.

    Yields the literal text of each primary callsite. The two intentional
    legacy 2-arg fallback callsites (gated under `except TypeError:`) are
    excluded because they exist precisely to support older hermes-agent
    builds where the new kwargs aren't accepted yet.
    """
    out = []
    src = STREAMING_PY
    cursor = 0
    while True:
        # Match either `_get_cl(` or `get_model_context_length(` (renamed alias).
        idx_open = src.find("_resolved_cl = get_model_context_length(", cursor)
        idx_fb = src.find("_fb_cl = _get_cl(", cursor)
        idx_legacy = src.find("_resolved_cl = _legacy_cl(", cursor)
        # Walk to whichever callsite comes first.
        candidates = [i for i in (idx_open, idx_fb, idx_legacy) if i != -1]
        if not candidates:
            break
        idx = min(candidates)
        # Walk balanced parens.
        depth = 0
        end = idx
        while end < len(src):
            c = src[end]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end += 1
                    break
            end += 1
        block = src[idx:end]
        cursor = end
        # Skip legacy fallbacks (gated under `except TypeError:` for older builds).
        # These are intentionally 2-arg.
        # Look back ~200 chars for the legacy marker.
        lookback = src[max(0, idx - 400):idx]
        is_legacy_fallback = (
            "except TypeError:" in lookback
            and "_legacy_cl" in block + lookback
        ) or "_legacy_cl(" in block
        # Also exclude any callsite where the immediately preceding line
        # is part of a TypeError fallback block (the second callsite shape:
        # bare `_fb_cl = _get_cl(` re-call inside `except TypeError:`).
        if "except TypeError:" in lookback and "_get_cl(" in block:
            # Check whether this is the legacy retry by seeing if there's
            # NO `config_context_length=` in the block AND a `try:` follows
            # `except TypeError:` in lookback. Simpler heuristic: legacy
            # fallback blocks are always WITHOUT kwargs and always inside
            # an `except TypeError:` arm. Skip them.
            if "config_context_length=" not in block:
                is_legacy_fallback = True
        if not is_legacy_fallback:
            out.append(block)
    return out


def test_two_fallback_callsites_present():
    """Sanity: two fallback callsites still exist (one for session save, one
    for SSE usage payload). If a refactor collapsed them, this test alerts
    so the consolidated callsite can be re-checked for correctness."""
    blocks = _both_callsites()
    assert len(blocks) >= 2, (
        f"Expected at least 2 get_model_context_length() fallback callsites "
        f"in api/streaming.py; found {len(blocks)}. If they were intentionally "
        f"consolidated into one helper, update this test to point at the helper."
    )


def test_both_callsites_pass_config_context_length():
    """Both callsites must pass `config_context_length=_cfg_ctx_len`."""
    blocks = _both_callsites()
    for i, block in enumerate(blocks):
        assert "config_context_length=_cfg_ctx_len" in block, (
            f"Callsite #{i+1} is missing `config_context_length=_cfg_ctx_len`. "
            f"Without it, users who set `model.context_length: 1048576` in "
            f"config.yaml get 256K from the default fallback. See #1896.\n\n"
            f"Block:\n{block}"
        )


def test_both_callsites_pass_provider():
    """Both callsites must pass `provider=resolved_provider or ''`."""
    blocks = _both_callsites()
    for i, block in enumerate(blocks):
        assert "provider=resolved_provider" in block, (
            f"Callsite #{i+1} is missing `provider=resolved_provider...`. "
            f"Provider is needed for the registry lookup step (models.dev "
            f"provider-aware lookup). See #1896.\n\nBlock:\n{block}"
        )


def test_both_callsites_pass_custom_providers():
    """Both callsites must pass `custom_providers=_cfg_custom_providers`."""
    blocks = _both_callsites()
    for i, block in enumerate(blocks):
        assert "custom_providers=_cfg_custom_providers" in block, (
            f"Callsite #{i+1} is missing `custom_providers=_cfg_custom_providers`. "
            f"This is needed for the `custom_providers` per-model context_length "
            f"override path. See #1896.\n\nBlock:\n{block}"
        )


def test_config_context_length_parsed_safely():
    """Invalid config_context_length values must NOT crash the resolver call —
    they should fall through to provider/registry probing instead."""
    # Both blocks should wrap the int parse in try/except (TypeError, ValueError).
    assert "except (TypeError, ValueError):" in STREAMING_PY, (
        "Config context_length parse must be guarded against (TypeError, ValueError) "
        "so a string like '256K' or 'one million' falls through to the resolver "
        "instead of crashing the SSE/save path."
    )


def test_legacy_signature_fallback_present():
    """Older hermes-agent builds may not yet have config_context_length on
    get_model_context_length(). The fix must catch TypeError and retry with
    the legacy 2-arg form so the indicator still resolves *something*."""
    # The except TypeError clause should mention the legacy retry comment OR
    # contain a 2-arg fallback call.
    assert "except TypeError:" in STREAMING_PY, (
        "Both callsites must catch TypeError to support older hermes-agent "
        "builds whose get_model_context_length signature pre-dates the new "
        "kwargs. Without this fallback, an older agent build would crash "
        "the save/SSE path instead of degrading to a 2-arg call."
    )


def test_cfg_custom_providers_resolved_from_cfg_dict():
    """The kwargs source must be the per-profile config (`_cfg`), not a
    module-level snapshot — otherwise profile switches with different
    custom_providers wouldn't take effect."""
    # Look for the resolution pattern.
    assert "_cfg.get('custom_providers')" in STREAMING_PY, (
        "_cfg_custom_providers must be sourced from `_cfg.get('custom_providers')` "
        "(per-profile config) so profile-scoped custom_providers entries work."
    )
    assert "_cfg.get('model', {})" in STREAMING_PY, (
        "_cfg_ctx_len must be sourced from `_cfg.get('model', {}).get('context_length')` "
        "(per-profile config) so profile-scoped model.context_length overrides work."
    )
