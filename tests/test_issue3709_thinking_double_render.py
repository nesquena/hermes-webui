"""#3709 -- Thinking card must not render twice (inside Activity AND below the answer).

Regression coverage for the double-render introduced by #3592's inline branch
(v0.51.258). In a turn that has BOTH a tool-bearing message and a trailing
thinking-only message, two code paths emitted a thinking card from the same
``assistantThinking`` map:

  1. the Activity-group path (tool-bearing message) put the thinking at the top
     of the collapsed Activity group, and
  2. the inline path (thinking-only message) appended a SECOND card via
     ``insertAdjacentHTML('beforeend')`` -- which, because the segment already
     carried the answer body + ``msg-foot`` footer, stranded the card *below*
     the "Done in ..." line.

The fix keeps the #3592 inline behaviour for genuinely thinking-only turns (so
their thinking is not buried in a collapsed group) but:

  A1. only renders inline when the turn has NO Activity group at all
      (``turnsWithActivityGroup`` gate), so a tool-bearing turn's thinking-only
      sibling does not emit a duplicate card;
  A2. inserts the inline card BEFORE the answer body / footer
      (``insertAdjacentHTML('beforebegin')`` on ``.msg-body,.msg-foot``) so it
      reads above the answer instead of orphaned below "Done in ...";
  B.  strips the thinking against the TURN's combined visible answer
      (``_turnVisibleTextByRawIdx``) so a trailing thinking-only message whose
      answer prose lives on a sibling message still gets its answer-echo removed.

These are static source-structure assertions (the render path is DOM-driven and
exercised live); they lock the invariants so the double-render cannot silently
return, and so a future blunt "just delete the inline branch" change (which would
re-break #3592) fails fast here instead.
"""
from __future__ import annotations

import re
from pathlib import Path

UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text(encoding="utf-8")


def _render_messages_body() -> str:
    """Return the body of renderMessages() (best-effort slice) for scoped asserts."""
    start = UI_JS.find("function renderMessages(")
    assert start != -1, "renderMessages() not found"
    # Slice a generous window; the activityIdxs loop + footer logic live within.
    return UI_JS[start:start + 60000]


def test_inline_thinking_branch_still_exists_for_thinking_only_turns():
    """#3592 must NOT be reverted: a thinking-only turn still renders its thinking
    inline (not buried in a collapsed Activity group)."""
    assert "!cards.length&&assistantThinking.has(aIdx)" in UI_JS, (
        "the thinking-only inline branch (#3592) must remain — deleting it "
        "re-buries thinking-only turns in a collapsed Activity group"
    )
    assert "_thinkingCardHtml(" in UI_JS


def test_inline_branch_gated_on_turn_having_no_activity_group():
    """A1: the inline card must only render when the turn has no Activity group,
    so a tool-bearing turn's thinking-only sibling does not duplicate the card."""
    body = _render_messages_body()
    assert "turnsWithActivityGroup" in body, (
        "must precompute the set of turns that already own an Activity group (#3709 A1)"
    )
    # The inline render must be guarded by a membership check on that set.
    assert re.search(
        r"turnsWithActivityGroup\.has\(\s*anchorTurn\s*\)",
        body,
    ), "the inline thinking render must be gated on turnsWithActivityGroup.has(anchorTurn)"


def test_turns_with_activity_group_built_from_tool_bearing_segments():
    """The turnsWithActivityGroup set must be populated from tool-bearing message
    segments' enclosing .assistant-turn nodes."""
    body = _render_messages_body()
    block = re.search(
        r"const turnsWithActivityGroup=new Set\(\);(.*?)const activityIdxs=",
        body,
        re.DOTALL,
    )
    assert block, "turnsWithActivityGroup population block not found"
    text = block.group(1)
    assert "closest('.assistant-turn')" in text, (
        "must map tool-bearing segments to their enclosing .assistant-turn"
    )
    assert "turnsWithActivityGroup.add(" in text


def test_inline_card_inserted_before_body_and_footer():
    """A2: when the inline render is correct, the card must land BEFORE the answer
    body / msg-foot (beforebegin), not appended after the 'Done in ...' footer."""
    body = _render_messages_body()
    # The inline branch selects the body/foot element and inserts before it.
    assert re.search(r"querySelector\(\s*'\.msg-body,\.msg-foot'\s*\)", body), (
        "inline branch must locate the .msg-body/.msg-foot element to anchor before it"
    )
    assert "insertAdjacentHTML('beforebegin'" in body, (
        "the inline thinking card must be inserted 'beforebegin' the answer body/footer "
        "(not 'beforeend', which strands it below 'Done in ...') (#3709 A2)"
    )


def test_no_unconditional_beforeend_thinking_in_inline_branch():
    """The old orphaning insert ('beforeend' of the raw thinking card on the anchor
    row) must be gone from the inline branch."""
    body = _render_messages_body()
    # The specific regression pattern: appending the thinking card to the end of
    # the anchor row unconditionally. It must no longer be the inline path.
    assert "anchorRow.insertAdjacentHTML('beforeend',_thinkingCardHtml(assistantThinking.get(aIdx)))" not in body, (
        "the inline branch must not append the thinking card to the end of the "
        "anchor row (that stranded it below the footer — the #3709 bug)"
    )


def test_turn_level_echo_strip_exists():
    """B: thinking is stripped against the TURN's combined visible answer, not only
    the same message's body — so a trailing thinking-only message that echoes the
    answer gets de-duped too."""
    body = _render_messages_body()
    assert "_turnVisibleTextByRawIdx" in body, (
        "must build a per-turn combined visible-answer map (#3709 defect B)"
    )
    # The strip site must consult the turn-level text in addition to displayContent.
    assert re.search(
        r"_turnVisibleTextByRawIdx\.get\(\s*rawIdx\s*\)",
        body,
    ), "the echo-strip must look up the turn's combined visible text"
    # And it must feed that into the echo-strip helper.
    strip_block = re.search(
        r"_turnVisibleTextByRawIdx\.get\(\s*rawIdx\s*\)(.*?)_stripVisibleAssistantEchoFromThinking\(\s*thinkingText\s*,\s*turnVisible\s*\)",
        body,
        re.DOTALL,
    )
    assert strip_block, (
        "the turn-level visible text must be passed to "
        "_stripVisibleAssistantEchoFromThinking"
    )


def test_suppressed_sibling_thinking_merged_into_group_not_dropped():
    """When the A1 gate suppresses a thinking-only sibling's inline card (because
    its turn has an Activity group), that sibling's thinking must NOT be lost — the
    group must render the TURN's merged thinking, not only the tool message's own
    entry. (Codex re-gate finding: rendering only assistantThinking.get(aIdx) for
    the tool index dropped a distinct sibling's reasoning.)"""
    body = _render_messages_body()
    # A per-turn thinking aggregation must exist...
    assert "turnThinkingParts" in body, (
        "must aggregate thinking per turn so a suppressed sibling's reasoning is "
        "carried into the Activity group, not dropped (#3709 / Codex re-gate)"
    )
    # ...and the Activity group must render the MERGED text, de-duped, once per turn.
    assert "mergedThinking" in body, (
        "the Activity group must render the turn's merged thinking"
    )
    assert "_renderedTurnThinking" in body, (
        "merged thinking must render once per turn (guard against double-emit when "
        "a turn has multiple tool messages)"
    )
    # The group node must be built from the merged text, not the single-index entry.
    assert re.search(
        r"_thinkingActivityNode\(\s*mergedThinking\s*,",
        body,
    ), "the Activity group thinking node must be built from mergedThinking"
