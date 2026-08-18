"""Regression coverage for RTL/bidi handling outside `.msg-body`.

The `_applyAutomaticMessageDirections` helper (added for PR #6560) only walks
`.msg-body` subtrees, so two surfaces that render user-authored/model-authored
free text outside that scope never got bidi resolution:

1. The "thinking" card body (`_thinkingCardHtml` in `static/ui.js`) renders
   reasoning text in a plain `<pre>` for whitespace preservation. Without an
   explicit `dir="auto"` on that element, the browser inherits the surrounding
   chat's base direction, so a mostly-Hebrew reasoning block gets left-aligned
   and its bidi runs are resolved incorrectly.
2. The sidebar session list (`.session-title` / `.session-preview` in
   `static/style.css`) is rendered by a completely separate code path
   (`sessions.js`) that never receives a `dir` attribute at all, so Hebrew
   session titles/previews render left-aligned with no bidi resolution.

These tests pin the source-level fix for both so it cannot silently regress:
the `<pre>` tag must carry `dir="auto"`, and the CSS must give the sidebar
selectors an explicit bidi-aware right-aligned rule scoped to the RTL chat
skin (`.chat-content-rtl`), matching the pattern already used for `.msg-body`.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = UI_JS.find(f"function {name}(")
    assert start != -1, f"{name} not found in static/ui.js"
    brace = UI_JS.find("{", start)
    assert brace != -1, f"opening brace not found for {name}"
    depth = 1
    i = brace + 1
    while depth and i < len(UI_JS):
        if UI_JS[i] == "{":
            depth += 1
        elif UI_JS[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"unterminated function {name}"
    return UI_JS[start:i]


class TestThinkingCardBidi:
    def test_thinking_card_body_pre_has_dir_auto(self):
        src = _function_source("_thinkingCardHtml")
        assert '<pre dir="auto">' in src, (
            "thinking-card-body <pre> must carry dir=\"auto\" so the browser "
            "resolves per-paragraph bidi direction for Hebrew reasoning text "
            "instead of inheriting the chat's base LTR/RTL direction"
        )

    def test_thinking_card_body_pre_auto_is_right_aligned_in_rtl_skin(self):
        assert re.search(
            r'\.chat-content-rtl\s+\.thinking-card-body\s+pre\[dir="auto"\]\s*\{[^}]*text-align:\s*right',
            STYLE_CSS,
        ), (
            "expected a .chat-content-rtl .thinking-card-body pre[dir=\"auto\"] "
            "rule setting text-align:right so dir=\"auto\" resolution is "
            "reflected visually, not just in the DOM attribute"
        )


class TestSessionListBidi:
    def test_session_title_and_preview_have_bidi_rule_in_rtl_skin(self):
        for selector in (".session-title", ".session-preview"):
            pattern = (
                r"\.chat-content-rtl\s+" + re.escape(selector) + r"[,{]"
            )
            assert re.search(pattern, STYLE_CSS), (
                f"expected a .chat-content-rtl {selector} rule — the sidebar "
                "session list is rendered outside .msg-body so the automatic "
                "per-message direction pass never reaches it; it needs its "
                "own bidi-aware rule"
            )

    def test_session_title_and_preview_use_plaintext_bidi_and_right_align(self):
        block_match = re.search(
            r"\.chat-content-rtl\s+\.session-title,\s*"
            r"\.chat-content-rtl\s+\.session-preview\s*\{([^}]*)\}",
            STYLE_CSS,
        )
        assert block_match, (
            "expected a combined .chat-content-rtl .session-title, "
            ".chat-content-rtl .session-preview rule block"
        )
        body = block_match.group(1)
        assert "unicode-bidi:plaintext" in body.replace(" ", ""), (
            "unicode-bidi:plaintext is required so each title/preview "
            "resolves its own base direction from its first strong "
            "character, the same algorithm dir=\"auto\" uses (plain CSS has "
            "no direction:auto)"
        )
        assert "text-align:right" in body.replace(" ", ""), (
            "session list entries should default to right-aligned layout "
            "to match a predominantly Hebrew/RTL session list"
        )
