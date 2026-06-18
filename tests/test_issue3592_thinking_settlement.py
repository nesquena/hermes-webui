"""#3592 / #3401 -- Thinking-only messages settle into folded Worklog detail.

Under the #3401 Worklog model, provider reasoning is not inline assistant prose
and not a Tool Card. It is preserved as an independent Thinking Card inside the
folded Worklog above the final answer.
"""
from __future__ import annotations

import re
from pathlib import Path

UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(encoding="utf-8")


def test_thinking_card_html_function_exists():
    """_thinkingCardHtml must be defined so Worklog Thinking Cards can render."""
    assert "function _thinkingCardHtml(" in UI_JS, (
        "_thinkingCardHtml function must exist in ui.js"
    )


def test_settlement_loop_does_not_inline_thinking_only_messages():
    """Thinking-only messages should flow through the Worklog group path."""
    assert "!cards.length&&assistantThinking.has(aIdx)" not in UI_JS, (
        "Thinking-only messages must not use the old inline early-continue path"
    )
    assert "_thinkingActivityNode(thinkingText, false, thinkingDisclosureKey)" in UI_JS, (
        "settled reasoning should render as a collapsed Worklog Thinking Card"
    )


def test_worklog_thinking_card_is_not_a_tool_card():
    """Thinking Cards should be sibling Worklog items, not Tool Card rows."""
    thinking_fn = UI_JS.split("function _thinkingActivityNode", 1)[1].split("function", 1)[0]
    assert "data-worklog-thinking-card" in thinking_fn
    assert "tool-card-row" not in thinking_fn
    assert "buildToolCard" not in thinking_fn


def test_final_answer_reasoning_worklog_is_placed_before_anchor():
    """If the visible final answer carries reasoning metadata, its folded
    Worklog must be inserted before that final-answer segment.
    """
    assert "beforeAnchor:!!thinkingText&&!anchorIsWorklogSource" in UI_JS
    ensure_fn = UI_JS.split("function ensureActivityGroup", 1)[1].split("function normalizeLiveActivityGroupPlacement", 1)[0]
    assert "if(opts.beforeAnchor) inner.insertBefore(group, anchor);" in ensure_fn
    assert "opts.syncAnchorReason!==false" in ensure_fn


def test_show_thinking_preference_respected():
    """The simplified render path must respect _showThinking for visible cards."""
    render_match = re.search(r"if\(thinkingText&&window\._showThinking!==false\)\{(.*?)\n\s*\}", UI_JS, re.DOTALL)
    assert render_match, "thinking render branch not found"
    assert "assistantThinking.set(rawIdx, thinkingText)" in render_match.group(1)


def test_messages_with_tool_calls_still_use_worklog_group():
    """Messages that have tool calls must still flow through the Worklog group."""
    assert "ensureActivityGroup(" in UI_JS, (
        "ensureActivityGroup must still be called for messages with tool calls"
    )


def test_thinking_only_turns_use_worklog_duration():
    """Thinking-only turns now create a folded Worklog group, so that group owns
    the "Done in ..." duration instead of the final answer footer.
    """
    m = re.search(r"const compactWorklogForMessage=isCompactWorklogMode\(\)&&([^;]+);", UI_JS)
    assert m, "compactWorklogForMessage suppression condition not found"
    cond = m.group(1)
    assert "toolCallAssistantIdxs.has(mi)" in cond
    assert "assistantThinking.has(mi)" in cond
