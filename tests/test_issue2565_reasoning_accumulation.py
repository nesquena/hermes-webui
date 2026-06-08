"""Regression tests for issue #2565: reasoning display bugs.

Issue 1: liveReasoningText is segment-local, while reasoningText is durable for
the whole assistant turn.
  - liveReasoningText must reset at tool and interim_assistant boundaries so
    later reasoning renders in a fresh Thinking Card.
  - reasoningText must not be reset at those boundaries; it is the fallback
    durable payload for providers that stream reasoning without final metadata.

Issue 2: provider reasoning metadata should become a Worklog Thinking Card, not
visible Worklog process prose or final-answer text.

Both fixes are needed: Issue 1 keeps live cards scoped to a segment without data
loss, while Issue 2 preserves reasoning as low-priority Worklog detail.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


# ── Issue 1: live reasoning segment reset at turn boundaries ─────────────────


class TestLiveReasoningTextResetOnTool:
    """liveReasoningText must reset in the tool listener so later provider
    reasoning renders in a fresh Worklog Thinking Card."""

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

    def test_durable_reasoning_text_not_reset_in_tool_listener(self):
        body = self._tool_listener_body()
        assert "reasoningText=''" not in body and 'reasoningText = ""' not in body, (
            "reasoningText must stay durable across tool boundaries so streamed "
            "provider reasoning is not silently dropped"
        )

    def test_live_reasoning_text_also_reset_in_tool_listener(self):
        body = self._tool_listener_body()
        assert "liveReasoningText=''" in body, (
            "liveReasoningText must also be reset in the tool listener"
        )


class TestLiveReasoningTextResetOnInterimAssistant:
    """liveReasoningText must reset at the interim_assistant boundary — the
    other segment boundary where the previous Thinking Card closes out."""

    def test_durable_reasoning_text_not_reset_in_interim_assistant_listener(self):
        src = read('static/messages.js')
        m = re.search(
            r"source\.addEventListener\('interim_assistant'\s*,\s*(?:e|ev)\s*=>\s*\{(.*?)\n\s*\}\);",
            src, re.DOTALL,
        )
        assert m, "interim_assistant listener not found in messages.js"
        body = m.group(1)
        assert "reasoningText=''" not in body and 'reasoningText = ""' not in body, (
            "reasoningText must stay durable across interim assistant boundaries "
            "so streamed provider reasoning is not silently dropped"
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


# ── Issue 2: reasoning metadata renders as Worklog Thinking Card ─────────────


class TestReasoningContentPreference:
    """Provider reasoning metadata is retained and rendered as Thinking Card
    detail, but must not become process prose or final-answer text."""

    def test_reasoning_payload_still_in_message_signature(self):
        src = read('static/ui.js')
        sig_fn = src.split("function _messageHasReasoningPayload(m)", 1)[1].split("function", 1)[0]
        assert 'm.reasoning' in sig_fn, (
            "ui.js should still treat persisted reasoning as message metadata "
            "for cache/signature invalidation"
        )

    def test_reasoning_metadata_not_used_as_inline_content_extraction(self):
        src = read('static/ui.js')
        extraction = src.split("let thinkingText='';", 1)[1].split("const isUser=m.role==='user';", 1)[0]
        assert 'm.reasoning_content' not in extraction
        assert 'm.reasoning' not in extraction

    def test_reasoning_payload_feeds_worklog_thinking_card_helper(self):
        src = read('static/ui.js')
        helper = src.split("function _worklogReasoningTextFromMessage", 1)[1].split("function _thinkingCardHtml", 1)[0]
        assert "_assistantReasoningPayloadText(m)" in helper
        assert "_stripVisibleAssistantEchoFromThinking" in helper

    def test_no_direct_reasoning_content_to_inline_thinking_assignment(self):
        """Provider reasoning should not be promoted into inline assistant prose."""
        src = read('static/ui.js')
        m = re.search(
            r"thinkingText\s*=\s*(m\.reasoning_content\s*\|\|\s*m\.reasoning)",
            src,
        )
        assert not m, (
            "thinkingText must not be assigned from reasoning_content/reasoning; "
            "those fields are Worklog Thinking Card detail, not final-answer text"
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
