"""Regression tests for #852 — thinking card must not mirror the main response.

The `_streamDisplay()` function in messages.js had an early return
`if(reasoningText) return raw` that bypassed think-block stripping when
the reasoning SSE event had populated `reasoningText`. Providers that emit
reasoning via BOTH `on_reasoning` AND `<think>` tags in the token stream
then showed identical content in the thinking card and the main response.
"""
import os
import re


_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


def _inline_extractor_body(js):
    start = js.index("function _extractInlineThinkingFromContent(")
    end = js.index("if(typeof window", start)
    return js[start:end]


class TestStreamDisplayStripsThinkBlocksAlways:

    def test_early_return_on_reasoning_text_is_gone(self):
        """Regression guard: the bypass that caused the thinking card to
        mirror the main response must stay removed."""
        js = _read("static/messages.js")
        m = re.search(r'function _streamDisplay\(\)\{.*?\n  \}', js, re.DOTALL)
        assert m, "_streamDisplay not found"
        fn = m.group(0)
        assert "if(reasoningText) return raw" not in fn, (
            "The early-return `if(reasoningText) return raw;` must remain "
            "removed (#852) — it caused the thinking card to mirror the main "
            "response when providers emit <think> tags AND reasoning SSE events."
        )

    def test_think_pair_stripping_still_runs(self):
        """The shared inline extractor must still strip think blocks."""
        js = _read("static/messages.js")
        m = re.search(r'function _streamDisplay\(\)\{.*?\n  \}', js, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "_extractInlineThinkingFromContent" in fn
        helper = _inline_extractor_body(js)
        assert "_thinkPairs" in helper
        assert "text.startsWith(candidate.open,index)" in helper

    def test_still_handles_incomplete_think_tag_partial_prefix(self):
        """Existing behaviour preserved: partial `<thi`, `<think` prefixes
        must still be suppressed so users don't see them mid-stream."""
        js = _read("static/messages.js")
        m = re.search(r'function _streamDisplay\(\)\{.*?\n  \}', js, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "_extractInlineThinkingFromContent" in fn
        helper = _inline_extractor_body(js)
        assert "candidate.open.startsWith(rest)" in helper
