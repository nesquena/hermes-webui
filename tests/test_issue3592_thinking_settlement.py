"""#3592 -- Thinking-only messages must render inline, not hidden in a collapsed activity group.

Under Simplified Tool Calling mode, the settlement loop wraps ALL post-settlement
assistant content via ensureActivityGroup({collapsed:true}). When an assistant
message has thinking but no tool calls, the thinking trace vanished behind a
collapsed dropdown. Fix: early-continue guard so thinking-only messages render
inline via _thinkingCardHtml instead of being wrapped.
"""
from __future__ import annotations

import re
from pathlib import Path

UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(encoding="utf-8")


def test_thinking_card_html_function_exists():
    """_thinkingCardHtml must be defined so the inline path can call it."""
    assert "function _thinkingCardHtml(" in UI_JS, (
        "_thinkingCardHtml function must exist in ui.js"
    )


def test_settlement_loop_has_empty_cards_guard():
    """The simplified-tool-calling settlement loop must check cards.length before
    calling ensureActivityGroup, so thinking-only messages skip the collapsed group."""
    assert "!cards.length&&assistantThinking.has(aIdx)" in UI_JS, (
        "Settlement loop must guard on empty cards + thinking presence before "
        "wrapping in a collapsed activity group"
    )


def test_early_continue_present_in_settlement_loop():
    """The guard path must contain a continue statement so the activity group
    path is skipped for thinking-only messages."""
    guard_pattern = re.compile(
        r"!cards\.length&&assistantThinking\.has\(aIdx\).*?continue",
        re.DOTALL,
    )
    assert guard_pattern.search(UI_JS), (
        "The early-continue guard for thinking-only messages must be present "
        "in the settlement loop"
    )


def test_alternative_path_calls_thinking_card_html_inline():
    """The guard branch must call _thinkingCardHtml directly so thinking renders
    inline rather than inside a collapsed activity group."""
    guard_block = re.search(
        r"!cards\.length&&assistantThinking\.has\(aIdx\)(.*?)continue",
        UI_JS,
        re.DOTALL,
    )
    assert guard_block, "Guard block not found"
    block_text = guard_block.group(1)
    assert "_thinkingCardHtml(" in block_text, (
        "The early-continue branch must call _thinkingCardHtml to render "
        "thinking inline"
    )


def test_show_thinking_preference_respected():
    """The inline thinking path must check _showThinking so the preference is
    honoured the same way as the non-simplified path."""
    guard_block = re.search(
        r"!cards\.length&&assistantThinking\.has\(aIdx\)(.*?)continue",
        UI_JS,
        re.DOTALL,
    )
    assert guard_block, "Guard block not found"
    block_text = guard_block.group(1)
    assert "_showThinking" in block_text, (
        "The early-continue branch must respect window._showThinking"
    )


def test_messages_with_tool_calls_still_use_activity_group():
    """Messages that have tool calls must still flow through ensureActivityGroup
    so the existing collapsed-group behaviour is preserved."""
    assert "ensureActivityGroup(" in UI_JS, (
        "ensureActivityGroup must still be called for messages with tool calls"
    )


def test_thinking_only_turns_keep_footer_duration():
    """#3592 review regression: a thinking-only turn now renders inline with NO
    activity group, so the footer "Done in …" duration must NOT be suppressed for
    it — suppression belongs only to turns that actually build an activity group
    (tool-call turns). The old condition suppressed on assistantThinking.has(mi)
    too, which silently dropped the duration for thinking-only turns once the
    inline-render `continue` skipped group creation."""
    m = re.search(r"const compactActivityForMessage=isSimplifiedToolCalling\(\)&&([^;]+);", UI_JS)
    assert m, "compactActivityForMessage suppression condition not found"
    cond = m.group(1)
    assert "toolCallAssistantIdxs.has(mi)" in cond, (
        "duration suppression must key on toolCallAssistantIdxs (group actually created)"
    )
    assert "assistantThinking.has(mi)" not in cond, (
        "thinking-only turns must NOT suppress the footer duration (no group carries it)"
    )
