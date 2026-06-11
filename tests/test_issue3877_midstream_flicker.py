"""Regression coverage for #3877 — mid-stream transcript flicker (content disappears/reappears).

#3877: in long sessions, during streaming, the latest assistant reply content flickers —
disappears and then reappears moments later.

Root cause: the live streaming text is written by the ``smd`` parser into a DOM node
*inside* ``#liveAssistantTurn``. When ``renderMessages()`` is reached mid-stream — e.g.
the clarify-response echo (``messages.js``) or a CLI-import refresh push a render while a
stream is live — its unconditional ``inner.innerHTML=''`` rebuild DETACHES that node. The
parser keeps writing into the now-orphaned element, so the visible content vanishes until
the next stream event rebuilds ``#liveAssistantTurn`` from scratch (the "disappears, then
reappears" frame).

Fix: before the ``inner.innerHTML=''`` rebuild, capture the live ``#liveAssistantTurn``
DOM node (the actual node — the parser holds a live reference into it, so serialising to
HTML would not help). After the rebuild, if the freshly-rebuilt live turn has LESS
streamed text than the preserved node (because the live assistant message's content is
still empty in ``S.messages`` until the stream settles), swap the preserved node back in
via ``_mergeRestoredLiveAssistantSegment`` so the parser target stays connected and the
visible text never blanks. Only ever runs for the streaming session's own live turn
(``INFLIGHT[sid]`` gate); a settled transcript with no live turn is untouched.

Verified RED→GREEN in an isolated browser against the real shipped ``renderMessages``:
- RED (master): after a mid-stream ``renderMessages({preserveScroll:true})``, the parser
  node is detached (``isConnected === false``), the rebuilt live turn shows only
  "Running" with no streamed text, and further tokens write into an orphaned node.
- GREEN (fix): the parser node stays connected, the same live node is preserved across
  the rebuild, the streamed text remains visible, and tokens written after the rebuild
  still land in the visible node. A settled (non-streaming) session renders normally
  (2 assistant turns + 2 user rows, both answers visible) — the fix is a no-op there.

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


def test_render_messages_captures_live_turn_before_rebuild():
    """#3877: renderMessages must capture the live turn node before innerHTML=''."""
    body = _function_body(UI_JS, "renderMessages")
    # The capture is anchored by its issue tag so it is greppable + intentional.
    assert "Mid-stream flicker fix (#3877)" in body, (
        "the #3877 live-turn preservation is missing from renderMessages"
    )
    # The capture must happen BEFORE the destructive rebuild. renderMessages has
    # several `inner.innerHTML=''` sites; the relevant one is the rebuild that
    # immediately follows the capture, so assert on the FIRST rebuild at/after the
    # capture index.
    capture_idx = body.find("_preservedLiveTurn=null")
    assert capture_idx != -1, "_preservedLiveTurn capture not found"
    rebuild_idx = body.find("inner.innerHTML=''", capture_idx)
    assert rebuild_idx != -1, "inner.innerHTML='' rebuild after capture not found"
    assert capture_idx < rebuild_idx, (
        "the live-turn capture must run BEFORE inner.innerHTML='' or the node is "
        "already detached when captured"
    )


def test_capture_is_gated_on_streaming_session():
    """The capture only runs for the streaming session's own live turn — never for a
    settled transcript (INFLIGHT[sid] gate + session-id match)."""
    body = _function_body(UI_JS, "renderMessages")
    failsafe = body[body.find("Mid-stream flicker fix (#3877)") :]
    # Gated on an in-flight stream for this session.
    assert "INFLIGHT[sid]" in failsafe
    # The captured turn must belong to the current session (no cross-session revive).
    assert "liveAssistantTurn" in failsafe
    assert "dataset.sessionId" in failsafe


def test_reattach_swaps_when_preserved_ties_or_beats_rebuilt():
    """After the rebuild, the preserved (parser-referenced) node is swapped back in
    when it carries AT LEAST AS MUCH streamed text as the rebuilt live turn
    (`_rebuiltLen <= _preservedLen`).

    The `<=` (not `<`) is the #3877-reopen fix: at the throttled-persist boundary the
    rebuilt turn's live content can EQUAL the preserved length, and the old strict-`<`
    guard then skipped the swap — leaving the smd parser writing into the detached
    original node (the residual "disappears, then reappears" frame). On a tie the
    preserved node wins because it holds the live parser reference and nothing is lost.
    """
    body = _function_body(UI_JS, "renderMessages")
    reattach = body[body.find("Re-attach the preserved live turn (#3877)") :]
    assert reattach, "the #3877 re-attach block is missing"
    # Length comparison still gates the swap...
    assert "_liveAssistantSegmentTextLength" in reattach
    # ...but now with `<=` so the equal-length tie also restores the parser node.
    assert "_rebuiltLen<=_preservedLen" in reattach, (
        "the swap must fire on a tie (<=), not only when preserved strictly wins (<) "
        "— the strict-< guard left the parser node orphaned on the equal-length frame"
    )
    # The old strict-< form must be gone (it is the bug).
    assert "_rebuiltLen<_preservedLen" not in reattach.replace("_rebuiltLen<=_preservedLen", "")


def test_reattach_swaps_at_segment_level_to_preserve_rebuilt_structure():
    """The swap replaces only the rebuilt LIVE SEGMENT with the preserved one, so a
    multi-segment turn (earlier settled segments + tool/worklog groups built by the
    rebuild) keeps that rebuilt-only structure. A whole-turn replaceWith would discard
    it when the preserved snapshot predates those segments. Whole-turn replace is only
    the fallback when the rebuilt turn has no live segment to target."""
    body = _function_body(UI_JS, "renderMessages")
    reattach = body[body.find("Re-attach the preserved live turn (#3877)") :]
    # Segment-level swap is the primary path.
    assert "_rebuiltSeg.replaceWith(_preservedSeg)" in reattach, (
        "the swap must be segment-level (replace the rebuilt live segment with the "
        "preserved one) so rebuilt-only structure in a multi-segment turn is kept"
    )
    # Whole-turn replace remains as the no-live-segment fallback.
    assert "replaceWith(_preservedLiveTurn)" in reattach
    # The preserved live segment is resolved for the swap.
    assert "_preservedSeg" in reattach and "_rebuiltSeg" in reattach


def test_reattach_runs_after_rebuild_loop():
    """The re-attach must run AFTER the rebuild (so a freshly-rebuilt live turn exists to
    compare against / replace) but is still inside renderMessages."""
    body = _function_body(UI_JS, "renderMessages")
    capture_idx = body.find("_preservedLiveTurn=null")
    reattach_idx = body.find("Re-attach the preserved live turn (#3877)")
    assert capture_idx != -1 and reattach_idx != -1
    assert reattach_idx > capture_idx, "re-attach must come after the capture/rebuild"
