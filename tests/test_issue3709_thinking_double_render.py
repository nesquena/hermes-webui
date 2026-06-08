"""#3709 -- #3401 Worklog Thinking must not render exact duplicates.

Master fixed a double-render bug in the older Activity rendering path: a turn
with tools plus a sibling thinking-only message could show the same Thinking
card twice. #3401 replaces that old structure with a folded Worklog made of
sibling items: process prose, Thinking Card, and Tool Card/Group.

These static assertions keep the #3709 invariant in the #3401 model:

* settled Thinking is rendered through the Worklog item path, not the old inline
  sibling path below the answer;
* exact duplicate Thinking cards are keyed by normalized content and suppressed;
* different sibling reasoning can still become distinct Worklog items.
"""
from __future__ import annotations

import re
from pathlib import Path

UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(encoding="utf-8")


def _render_messages_body() -> str:
    start = UI_JS.find("function renderMessages(")
    assert start != -1, "renderMessages() not found"
    return UI_JS[start:start + 80000]


def _function_body(name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", UI_JS)
    assert match, f"{name}() not found"
    brace = UI_JS.find("{", match.end())
    assert brace != -1, f"{name}() has no body"
    depth = 1
    i = brace + 1
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while i < len(UI_JS) and depth:
        ch = UI_JS[i]
        nxt = UI_JS[i + 1] if i + 1 < len(UI_JS) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return UI_JS[brace + 1:i - 1]


def test_settled_thinking_renders_through_worklog_item_path():
    body = _render_messages_body()
    assert "_appendWorklogStep(state.group, anchorRow, cards, thinkingText" in body, (
        "Settled Thinking should render through the #3401 Worklog item path."
    )
    assert "_thinkingActivityNode(thinkingText, false)" in UI_JS, (
        "Thinking should remain a dedicated Worklog Thinking Card node."
    )
    assert "data-worklog-thinking-card" in UI_JS, (
        "Thinking Cards need a stable Worklog-specific hook."
    )


def test_settled_worklog_thinking_uses_content_key_for_exact_duplicate_suppression():
    body_min = re.sub(r"\s+", "", _render_messages_body())
    assert "thinkingKey:thinkingText?`thinking:${_normalizeThinkingEchoCompare(thinkingText)}`:''" in body_min, (
        "Settled Worklog should suppress duplicate Thinking by normalized content, "
        "not by assistant message index."
    )
    append_body = _function_body("_appendWorklogStep")
    assert "seenReasons.has(thinkingKey)" in append_body
    assert "seenReasons.add(thinkingKey)" in append_body


def test_exact_echo_suppression_compares_turn_visible_texts():
    body = _render_messages_body()
    helper = _function_body("_worklogReasoningTextFromMessage")
    assert "assistantTurnVisibleContentByRawIdx" in body
    assert "_worklogReasoningTextFromMessage(m, rawIdx, toolCallAssistantIdxs, displayContent, turnFinalVisibleContent, turnVisibleContents)" in body
    assert "_stripVisibleAssistantEchoFromThinking(thinkingText, visibleContent, turnFinalVisibleContent, ...visibleTexts)" in helper, (
        "A thinking-only sibling that exactly echoes the visible process/final text "
        "should be suppressed after settlement."
    )


def test_distinct_sibling_reasoning_is_still_available_to_worklog():
    body = _render_messages_body()
    assert "for(const aIdx of assistantThinking.keys())" in body, (
        "Each assistant reasoning entry should still be eligible for a Worklog item."
    )
    assert "const thinkingText=thinkingIdx!==null?assistantThinking.get(thinkingIdx):''" in body
    assert "seenReasons:state.seenReasons" in body, (
        "Duplicate suppression should be scoped to rendered Worklog keys, not by "
        "dropping reasoning metadata up front."
    )


def test_old_inline_activity_double_render_path_is_not_restored():
    body = _render_messages_body()
    assert "!cards.length&&assistantThinking.has(aIdx)" not in body, (
        "The old thinking-only inline Activity branch should not return in the "
        "#3401 Worklog model."
    )
    assert "anchorRow.insertAdjacentHTML('beforeend',_thinkingCardHtml(assistantThinking.get(aIdx)))" not in body, (
        "Thinking must not be appended below the final answer/footer."
    )
    assert "mergedThinking" not in body, (
        "The old Activity mergedThinking implementation should not be required "
        "after #3401 moves Thinking into Worklog sibling items."
    )
