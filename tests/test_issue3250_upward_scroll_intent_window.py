"""Regression test for #3250: upward-scroll intent window during streaming.

The pre-fix `MESSAGE_UPWARD_INTENT_MS` window was only 450ms. When a user
scrolled up to read earlier content during a streaming response and then
*paused* to read (>450ms since their last wheel/touch event), the intent
expired. Subsequent DOM-layout changes from the streaming markdown parser
(smd), tool-card insertions, or code re-highlighting then produced scroll
events that `_recentMessageUpwardIntent()` no longer attributed to the user
(`movedUp = false`). If the resulting position sat inside the 250px
near-bottom zone for two consecutive samples, `_scrollPinned` flipped back to
true and the next streaming token snapped the user to the bottom.

The fix widens the window to 2000ms so a brief reading pause no longer drops
the user's intent. Direction detection is unchanged — downward motion still
re-pins regardless of the timeout because `movedUp` additionally requires
`top < _lastScrollTop - 2` — so this is a pure intent-duration tuning, not a
relaxation of the re-pin semantics.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _intent_window_ms() -> int:
    """Extract the numeric value of the MESSAGE_UPWARD_INTENT_MS constant."""
    marker = "const MESSAGE_UPWARD_INTENT_MS="
    idx = UI_JS.find(marker)
    assert idx != -1, "MESSAGE_UPWARD_INTENT_MS constant not found in ui.js"
    start = idx + len(marker)
    end = UI_JS.find(";", start)
    raw = UI_JS[start:end].strip()
    return int(raw)


def test_upward_intent_window_is_widened_for_reading_pauses():
    """The intent window must be >=2000ms so a brief reading pause during a
    streaming response does not drop upward-scroll intent and re-pin the view
    (#3250). The pre-fix 450ms value was too short for a real read-pause.
    """
    window = _intent_window_ms()
    assert window >= 2000, (
        f"MESSAGE_UPWARD_INTENT_MS is {window}ms; #3250 requires >=2000ms so a "
        "reading pause during streaming does not expire upward-scroll intent "
        "and snap the user back to the bottom."
    )


def test_intent_helper_compares_against_the_window_constant():
    """_recentMessageUpwardIntent() must gate on the window constant, so the
    widened value actually takes effect (guards against the helper being
    rewritten with a hardcoded duration).
    """
    marker = "function _recentMessageUpwardIntent()"
    idx = UI_JS.find(marker)
    assert idx != -1, "_recentMessageUpwardIntent() not found in ui.js"
    body = UI_JS[idx:UI_JS.find("}", idx) + 1]
    assert "MESSAGE_UPWARD_INTENT_MS" in body, (
        "_recentMessageUpwardIntent() must compare against MESSAGE_UPWARD_INTENT_MS "
        "rather than a hardcoded duration (#3250)."
    )
    assert "_lastMessageUpwardIntentMs" in body, (
        "_recentMessageUpwardIntent() must measure elapsed time since the last "
        "recorded upward intent timestamp (#3250)."
    )


def test_downward_repin_is_independent_of_the_intent_window():
    """Widening the intent window must not weaken downward re-pin: the movedUp
    flag still requires an actual upward scrollTop delta (`top < _lastScrollTop
    - 2`), so downward motion re-pins regardless of how long the intent window
    is. This is what keeps the #3250 tuning safe.
    """
    anchor = "el.addEventListener('scroll'"
    start = UI_JS.index(anchor)
    raf_start = UI_JS.index("requestAnimationFrame", start)
    brace = UI_JS.index("{", raf_start)
    depth = 0
    block = ""
    for i in range(brace, len(UI_JS)):
        ch = UI_JS[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                block = UI_JS[brace:i + 1]
                break
    assert block, "scroll listener rAF callback not found"
    moved_idx = block.index("const movedUp=")
    moved_expr = block[moved_idx:block.find(";", moved_idx)]
    assert "_lastScrollTop-2" in moved_expr or "_lastScrollTop -" in moved_expr, (
        "movedUp must still require an explicit upward scrollTop delta so "
        "downward motion re-pins independently of the intent window (#3250)."
    )
