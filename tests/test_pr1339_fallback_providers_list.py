"""Tests for WebUI fallback config handling in streaming.py.

Before the fix, when config had `fallback_providers: [{provider, model, ...}, ...]`,
streaming.py read it as if it were a dict and called `.get('model', '')` on a list,
which would raise `AttributeError: 'list' object has no attribute 'get'`.

WebUI must now also mirror Hermes CLI/gateway fallback-chain semantics:
`fallback_providers` entries are tried first, then legacy `fallback_model`
entries are appended when they do not duplicate an earlier route.
"""
from pathlib import Path

STREAMING_PY = Path(__file__).resolve().parent.parent / "api" / "streaming.py"


def _extract_fallback_block():
    """Return the source range that handles fallback_model/fallback_providers."""
    src = STREAMING_PY.read_text(encoding="utf-8")
    # Locate the resolved-fallback region
    idx = src.find("# Fallback model chain from profile config")
    assert idx != -1, "Fallback block marker not found in streaming.py"
    end = src.find("# Build kwargs defensively", idx)
    assert end != -1, "End-of-block marker not found"
    return src[idx:end]


def test_fallback_handles_both_dict_and_list_config():
    """Block must read either fallback_model (dict) or fallback_providers (list)."""
    block = _extract_fallback_block()

    # Both keys must be consulted
    assert "fallback_model" in block, "Must still support legacy single-dict fallback_model"
    assert "fallback_providers" in block, (
        "Must support new list-form fallback_providers (PR #1339)"
    )


def test_fallback_list_iteration_builds_chain_before_legacy_fallback():
    """List-form fallback_providers must stay ahead of legacy fallback_model."""
    block = _extract_fallback_block()

    # Must isinstance-check before calling .get
    assert "isinstance(_raw, list)" in block, (
        "Must detect list-form fallback_providers explicitly to avoid AttributeError"
    )
    assert "isinstance(_raw, dict)" in block or "isinstance(_raw,dict)" in block, (
        "Must keep legacy single-dict path explicitly"
    )
    assert "for _fallback_key in ('fallback_providers', 'fallback_model')" in block, (
        "WebUI must merge fallback_providers first, then append legacy fallback_model"
    )
    assert "_fallback_resolved = _fallback_chain or None" in block, (
        "WebUI must pass the full fallback chain to AIAgent, not only the first entry"
    )

    assert "_cfg.get(_fallback_key)" in block, (
        "Fallback entries should be read through the ordered key loop"
    )
    assert "_fallback.get(" not in block, (
        "Do not call .get() on a value that may be a list"
    )


def test_fallback_resolved_initialized_to_none():
    """_fallback_resolved must default to None so AIAgent gets an explicit None when no fallback."""
    block = _extract_fallback_block()
    # The variable must be assignable to None at the top of the block
    assert "_fallback_resolved = None" in block, (
        "_fallback_resolved must be initialized to None so callers can rely on its presence"
    )


def test_fallback_resolved_preserves_credential_hints():
    """Fallback entries must keep credential hints for AIAgent fallback activation."""
    block = _extract_fallback_block()
    resolved_start = block.find("_entries.append({")
    assert resolved_start != -1, "fallback entry normalization dict not found"
    resolved_end = block.find("}", resolved_start)
    resolved_dict = block[resolved_start:resolved_end]

    assert "'api_key': _entry.get('api_key')" in resolved_dict, (
        "WebUI must preserve fallback_model/fallback_providers api_key so "
        "AIAgent._try_activate_fallback can authenticate the fallback."
    )
    assert "'key_env': _entry.get('key_env')" in resolved_dict, (
        "WebUI must preserve fallback_model/fallback_providers key_env so "
        "AIAgent._try_activate_fallback can resolve env-backed fallback keys."
    )


def test_fallback_chain_deduplicates_routes():
    """Duplicate provider/model/base_url entries must not be passed twice."""
    block = _extract_fallback_block()
    assert "_fallback_seen = set()" in block
    assert "_identity in _fallback_seen" in block
    assert "_fallback_seen.add(_identity)" in block
