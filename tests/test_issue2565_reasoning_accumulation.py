"""Regression tests for issue #2565: reasoning display bugs.

Issue 1: reasoningText accumulates across turns within a single SSE stream.
  - reasoningText must be reset at each turn boundary (tool and interim_assistant
    events) so the done event only persists the current turn's reasoning.

Issue 2: ui.js display prefers m.reasoning over m.reasoning_content.
  - The rendering path must prefer m.reasoning_content (the clean per-turn value
    from the backend) over m.reasoning (which can be corrupted by Issue 1).

Both fixes are needed: Issue 2 alone cannot cover providers that stream reasoning
events without populating reasoning_content on the final API message.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


# ── Issue 1: reasoningText reset at turn boundaries ──────────────────────────


class TestReasoningTextResetOnTool:
    """reasoningText must be reset alongside liveReasoningText in the tool
    listener so multi-tool-turn sessions don't accumulate reasoning across
    turns."""

    def _tool_listener_body(self):
        """Extract the full tool listener body between the tool and
        tool_complete addEventListener calls."""
        src = read('static/messages.js')
        tool_start = src.find("source.addEventListener('tool'")
        assert tool_start >= 0, "tool listener not found"
        tool_complete_start = src.find(
            "source.addEventListener('tool_complete'", tool_start + 1,
        )
        assert tool_complete_start >= 0, "tool_complete listener not found"
        return src[tool_start:tool_complete_start]

    def test_reasoning_text_reset_in_tool_listener(self):
        body = self._tool_listener_body()
        assert "reasoningText=''" in body, (
            "reasoningText must be reset to '' inside the tool listener "
            "(Issue 1: accumulated reasoning from prior turns was assigned "
            "to the last assistant message on the done event)"
        )

    def test_live_reasoning_text_also_reset_in_tool_listener(self):
        body = self._tool_listener_body()
        assert "liveReasoningText=''" in body, (
            "liveReasoningText must also be reset in the tool listener"
        )


class TestReasoningTextResetOnInterimAssistant:
    """reasoningText must be reset at the interim_assistant boundary — the
    other turn boundary where the previous turn's reasoning closes out.
    Without this, providers that emit reasoning before an interim_assistant
    event will still co-mingle reasoning across turns."""

    def test_reasoning_text_reset_in_interim_assistant_listener(self):
        src = read('static/messages.js')
        m = re.search(
            r"source\.addEventListener\('interim_assistant'\s*,\s*(?:e|ev)\s*=>\s*\{(.*?)\n\s*\}\);",
            src, re.DOTALL,
        )
        assert m, "interim_assistant listener not found in messages.js"
        body = m.group(1)
        assert "reasoningText=''" in body, (
            "reasoningText must be reset to '' inside the interim_assistant "
            "listener (Issue 1: turn boundary where prior reasoning closes)"
        )

    def test_live_reasoning_text_reset_in_interim_assistant_listener(self):
        src = read('static/messages.js')
        m = re.search(
            r"source\.addEventListener\('interim_assistant'\s*,\s*(?:e|ev)\s*=>\s*\{(.*?)\n\s*\}\);",
            src, re.DOTALL,
        )
        assert m
        body = m.group(1)
        assert "liveReasoningText=''" in body, (
            "liveReasoningText must be reset in the interim_assistant listener"
        )


# ── Issue 2: reasoning_content preference on read ────────────────────────────


class TestReasoningContentPreference:
    """The rendering path in ui.js must prefer m.reasoning_content (the clean
    per-turn value from the backend) over m.reasoning (which can be corrupted
    by Issue 1's accumulation bug)."""

    def test_reasoning_content_checked_before_reasoning(self):
        src = read('static/ui.js')
        assert 'm.reasoning_content' in src, (
            "ui.js must reference m.reasoning_content so the clean per-turn "
            "value from the backend is used for thinking card display"
        )

    def test_reasoning_content_preferred_in_thinking_text_fallback(self):
        src = read('static/ui.js')
        lines = src.splitlines()
        for line in lines:
            if 'thinkingText' in line and 'm.reasoning' in line:
                if 'm.reasoning_content' not in line and 'reasoning_content' not in line:
                    if 'Array.isArray' not in line:
                        raise AssertionError(
                            f"Line references m.reasoning without checking "
                            f"m.reasoning_content first: {line.strip()}"
                        )

    def test_reasoning_content_has_priority_over_reasoning(self):
        """The fallback expression must evaluate reasoning_content first."""
        src = read('static/ui.js')
        m = re.search(
            r"thinkingText\s*=\s*(m\.reasoning_content\s*\|\|\s*m\.reasoning)",
            src,
        )
        assert m, (
            "thinkingText assignment must use m.reasoning_content || m.reasoning "
            "so the clean backend value takes priority over the potentially "
            "corrupted frontend-accumulated value"
        )


# ── Cross-cutting: done event still has the persist-on-done guard ────────────


class TestDoneEventReasoningPersist:
    """The done event's reasoning persistence guard must still exist —
    the reset fixes reduce the blast radius but the guard prevents double-write
    when the backend already populated .reasoning."""

    def test_done_event_has_reasoning_guard(self):
        src = read('static/messages.js')
        assert '!lastAsst.reasoning' in src, (
            "done event must guard reasoningText persistence with "
            "!lastAsst.reasoning to avoid overwriting backend-populated values"
        )

    def test_done_event_persists_reasoning_text(self):
        src = read('static/messages.js')
        assert 'lastAsst.reasoning=reasoningText' in src, (
            "done event must still persist reasoningText to lastAsst.reasoning "
            "for providers that stream reasoning events without populating "
            "reasoning_content on the final API message"
        )
