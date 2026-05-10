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


def test_streaming_passes_configured_max_tokens_to_agent():
    src = _src()
    assert "_raw_max_tokens = _cfg.get('max_tokens')" in src
    assert "_agent_cfg_for_tokens.get('max_tokens')" in src
    assert "_agent_kwargs['max_tokens'] = _max_tokens_cfg" in src


def test_streaming_agent_cache_signature_includes_max_tokens_and_fallback():
    src = _src()
    assert "_max_tokens_cfg or ''" in src
    assert "_fallback_resolved or {}" in src


def test_openrouter_more_credits_error_is_classified_as_quota():
    src = _src()
    assert "'more credits' in _err_lower" in src
    assert "'can only afford' in _err_lower" in src
    assert "'fewer max_tokens' in _err_lower" in src
    assert "'more credits' in _exc_lower" in src
    assert "'can only afford' in _exc_lower" in src
    assert "'fewer max_tokens' in _exc_lower" in src
