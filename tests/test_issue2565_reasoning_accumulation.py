"""Regression tests for issue #2565: reasoning display bugs.

Issue 1: reasoningText accumulates across turns within a single SSE stream.
  - reasoningText must be reset at each turn boundary (tool and interim_assistant
    events) so the done event only persists the current turn's reasoning.

Issue 2: provider reasoning metadata should not become visible Worklog text.
  - The rendering path must keep m.reasoning_content / m.reasoning as diagnostics
    rather than promoting either field into thinkingText.

Both fixes are needed: Issue 1 keeps persisted diagnostics scoped to a turn,
while Issue 2 prevents those diagnostics from duplicating visible assistant prose.
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


# ── Issue 2: reasoning metadata is persisted, not rendered ───────────────────


class TestReasoningContentPreference:
    """Provider reasoning metadata is retained for diagnostics/cache signatures,
    but must not drive the normal Worklog/thinking display path."""

    def test_reasoning_payload_still_in_message_signature(self):
        src = read('static/ui.js')
        sig_fn = src.split("function _messageHasReasoningPayload(m)", 1)[1].split("function", 1)[0]
        assert 'm.reasoning' in sig_fn, (
            "ui.js should still treat persisted reasoning as message metadata "
            "for cache/signature invalidation"
        )

    def test_reasoning_metadata_not_used_as_thinking_text(self):
        src = read('static/ui.js')
        extraction = src.split("let thinkingText='';", 1)[1].split("const isUser=m.role==='user';", 1)[0]
        assert 'm.reasoning_content' not in extraction
        assert 'm.reasoning' not in extraction

    def test_no_reasoning_content_to_thinking_text_assignment(self):
        """Provider reasoning should not be promoted into Worklog prose."""
        src = read('static/ui.js')
        m = re.search(
            r"thinkingText\s*=\s*(m\.reasoning_content\s*\|\|\s*m\.reasoning)",
            src,
        )
        assert not m, (
            "thinkingText must not be assigned from reasoning_content/reasoning; "
            "those fields are diagnostics, not normal transcript Worklog text"
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
