"""Regression coverage for WebUI streaming provider failure handling.

The incident this guards against: WebUI-created AIAgent instances did not pass
config.yaml's max_tokens, so a fallback Claude model via OpenRouter requested its
native 64k output ceiling and failed with HTTP 402 "more credits / fewer
max_tokens". The stream then looked like a stuck Thinking card instead of a
clear quota error.
"""
from pathlib import Path


STREAMING = Path(__file__).resolve().parents[1] / "api" / "streaming.py"


def _src() -> str:
    return STREAMING.read_text(encoding="utf-8")


def _compute_agent_cache_signature_source() -> str:
    """Return the source of the `_compute_agent_cache_signature()` helper.

    The signature blob used to be inlined in the streaming send path; it now
    lives in this helper so the initial send and both self-heal retry paths
    derive the signature from the same final runtime bundle.
    """
    src = _src()
    start = src.index("def _compute_agent_cache_signature(")
    end = src.index("\ndef ", start)
    return src[start:end]


def _signature_blob() -> str:
    """Return the `_json.dumps([...])` field list the signature hashes."""
    helper = _compute_agent_cache_signature_source()
    blob_start = helper.index("_sig_blob = _json.dumps")
    blob_end = helper.index("], sort_keys=True)", blob_start)
    return helper[blob_start:blob_end]


def _production_signature_calls() -> list[str]:
    """Return the source of every production signature call site.

    Paren-balanced so the whole multi-line keyword-argument list is captured,
    and every call site is returned so a retry path cannot silently drop a
    field that the initial send still passes.
    """
    src = _src()
    marker = "_agent_sig = _compute_agent_cache_signature("
    calls: list[str] = []
    pos = src.find(marker)
    while pos != -1:
        depth = 0
        for idx in range(pos + len(marker) - 1, len(src)):
            char = src[idx]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    calls.append(src[pos:idx + 1])
                    break
        else:
            raise AssertionError("unterminated _compute_agent_cache_signature( call")
        pos = src.find(marker, pos + 1)
    return calls


def test_streaming_passes_configured_max_tokens_to_agent():
    src = _src()
    assert "_raw_max_tokens = _cfg.get('max_tokens')" in src
    assert "_agent_cfg_for_tokens.get('max_tokens')" in src
    assert "_agent_kwargs['max_tokens'] = _max_tokens_cfg" in src


def test_streaming_agent_cache_signature_includes_max_tokens_and_fallback():
    blob = _signature_blob()
    assert "max_tokens_cfg or ''" in blob, (
        "_compute_agent_cache_signature() must hash max_tokens_cfg, or a "
        "max_tokens change reuses the agent built on the old output ceiling."
    )
    assert "fallback_resolved or {}" in blob, (
        "_compute_agent_cache_signature() must hash the resolved fallback "
        "chain so a fallback edit mints a new agent."
    )

    calls = _production_signature_calls()
    assert calls, "streaming.py no longer calls _compute_agent_cache_signature()"
    for call in calls:
        assert "max_tokens_cfg=_max_tokens_cfg" in call, (
            "every signature call site (initial send and both self-heal "
            "retries) must pass the resolved max_tokens:\n" + call
        )
        assert "fallback_resolved=_fallback_resolved" in call, (
            "every signature call site must pass the resolved fallback "
            "chain:\n" + call
        )


def test_openrouter_more_credits_error_is_classified_as_quota():
    src = _src()
    assert "'more credits' in _err_lower" in src
    assert "'can only afford' in _err_lower" in src
    assert "'fewer max_tokens' in _err_lower" in src
    assert "'more credits' in _exc_lower" in src
    assert "'can only afford' in _exc_lower" in src
    assert "'fewer max_tokens' in _exc_lower" in src
