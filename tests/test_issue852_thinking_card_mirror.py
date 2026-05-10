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


class TestStreamDisplayStripsThinkBlocksAlways:
    def test_early_return_on_reasoning_text_is_gone(self):
        """Regression guard: the bypass that caused the thinking card to
        mirror the main response must stay removed."""
        js = _read("static/messages.js")
        m = re.search(r"function _streamDisplay\(\)\{.*?\n  \}", js, re.DOTALL)
        assert m, "_streamDisplay not found"
        fn = m.group(0)
        assert "if(reasoningText) return raw" not in fn, (
            "The early-return `if(reasoningText) return raw;` must remain "
            "removed (#852) — it caused the thinking card to mirror the main "
            "response when providers emit <think> tags AND reasoning SSE events."
        )

    def test_think_pair_stripping_still_runs(self):
        """The `_thinkPairs` stripping loop must still be present so the
        fix actually strips think blocks."""
        js = _read("static/messages.js")
        m = re.search(r"function _streamDisplay\(\)\{.*?\n  \}", js, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "_thinkPairs" in fn, (
            "_streamDisplay must iterate _thinkPairs to strip think blocks"
        )
        assert "trimmed.startsWith(open)" in fn, (
            "the think-block stripping must check for the open tag"
        )

    def test_still_handles_incomplete_think_tag_partial_prefix(self):
        """Existing behaviour preserved: partial `<thi`, `<think` prefixes
        must still be suppressed so users don't see them mid-stream."""
        js = _read("static/messages.js")
        m = re.search(r"function _streamDisplay\(\)\{.*?\n  \}", js, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "open.startsWith(trimmed)" in fn, (
            "Partial-tag suppression must still be present"
        )
