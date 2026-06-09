"""Regression coverage for #3875 — chat transcript renders as only date separators.

#3875 (BUG: messages stopped displaying correctly): the chat transcript rendered as
nothing but a stack of date-change separators (SUNDAY / SATURDAY / dates) with no
message bodies between them. Two users confirmed it; only a restart appeared to help
(it didn't — it was a render-state bug, not server state).

Root cause: the live-to-final / Worklog redesign (#3401, v0.51.294) folds intermediate
assistant segments into a collapsed Worklog card and hides the source segment via the
``assistant-segment-worklog-source`` class (``display:none``). That is correct WHEN the
turn also has a visible final answer. But when a turn's ONLY content is folded into a
collapsed Worklog — e.g. an autonomous/interrupted run whose final assistant message is
empty, or a reload where ``S.toolCalls`` did not hydrate so the Worklog card has no
expandable tool steps — every segment is hidden and the turn paints as nothing, leaving
the transcript a bare stack of date separators.

The fix is a defensive fail-safe at the END of ``renderMessages``: a settled assistant
turn must never render with ZERO visible content. When a turn has no visible segment,
its folded Worklog group is expanded (or, as a last resort, its hidden worklog-source
segments are un-hidden) so the content is never silently swallowed. The fail-safe never
touches a turn that already has any visible segment, so the intended collapsed-Worklog
UX is preserved whenever a visible answer exists.

These are static source-structure assertions over the shipped ``renderMessages`` so the
invariant cannot silently regress.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    brace = src.find("{", start)
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body not closed")


def test_render_messages_has_blank_turn_failsafe():
    """#3875: renderMessages must carry the no-blank-turn fail-safe invariant."""
    body = _function_body(UI_JS, "renderMessages")
    # The fail-safe is anchored by its issue tag so it is greppable + intentional.
    assert "Fail-safe invariant (#3875)" in body, (
        "the #3875 no-blank-turn fail-safe is missing from renderMessages"
    )


def test_failsafe_reveals_folded_worklog_for_blank_turns():
    """The fail-safe must expand the folded Worklog group when a turn has no visible content."""
    body = _function_body(UI_JS, "renderMessages")
    # It must scan turns and skip any turn that already has visible content.
    assert "_turnHasVisibleContent" in body
    # A turn is only acted on when it lacks visible content (the skip-guard).
    assert "if(_turnHasVisibleContent(turn)) continue;" in body
    # The reveal action removes the collapsed class on the Worklog group.
    assert "tool-call-group-collapsed" in body
    assert "removeAttribute('aria-hidden')" in body


def test_failsafe_preserves_collapsed_worklog_when_visible_answer_exists():
    """The fail-safe must NOT touch turns that already render visible content.

    The skip-guard (`if(_turnHasVisibleContent(turn)) continue;`) is what preserves the
    intended collapsed-Worklog UX: a turn with any visible answer is left untouched, so
    this fix only ever ADDS visibility to otherwise-blank turns and can never re-expand a
    Worklog the user expects collapsed.
    """
    body = _function_body(UI_JS, "renderMessages")
    # The visible-content check skips worklog-source (folded) + anchor-only placeholder
    # segments, and treats any other non-empty segment as "visible".
    failsafe = body[body.find("Fail-safe invariant (#3875)") :]
    assert "assistant-segment-worklog-source" in failsafe
    assert "assistant-segment-anchor" in failsafe
    # The live turn drives its own state and must be excluded from the sweep.
    assert "liveAssistantTurn" in failsafe
