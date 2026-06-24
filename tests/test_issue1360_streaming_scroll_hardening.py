"""Regression tests for #1360: streaming must not re-pin user scroll."""

from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    idx = src.find(marker)
    assert idx != -1, f"{name} not found"
    depth = 0
    for i, ch in enumerate(src[idx:], idx):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[idx:i + 1]
    raise AssertionError(f"Could not extract {name}")


def test_messages_scroller_disables_browser_scroll_anchoring_on_desktop():
    # Desktop (mouse): overflow-anchor:none — tool/card inserts cannot yank
    # the transcript while the user reads earlier content.
    assert "@media (hover:hover) and (pointer:fine){.messages{overflow-anchor:none;}}" in STYLE_CSS, (
        "On desktop (mouse-driven devices) #messages must disable browser scroll "
        "anchoring so tool/card inserts cannot yank the transcript. "
        "On mobile (touch devices) overflow-anchor:auto is used instead to prevent "
        "scrollTop=0 jank during innerHTML rebuild (#MOBILESCROLL)."
    )


def test_messages_scroller_uses_overflow_anchor_auto_on_mobile():
    # Mobile (touch): overflow-anchor:auto — prevents scrollTop=0 jank during
    # innerHTML rebuild and streaming DOM updates (#MOBILESCROLL).
    assert "overflow-anchor:auto;" in STYLE_CSS, (
        "On mobile/touch devices #messages must default to overflow-anchor:auto "
        "to prevent the browser painting a frame with scrollTop=0 between "
        "innerHTML='' and snapshot restore."
    )


def test_streaming_render_enables_mobile_scroll_jank_guard_before_dom_writes():
    """Streaming must re-enable mobile scroll anchoring before every DOM write.

    Regression: #MOBILESCROLL originally added _fixMobileScrollJank() in the
    live-stream render tick, but a later streaming parse-cache change dropped
    that call while leaving the helper/CSS in place. On iOS PWA this lets Safari
    paint a transient scrollTop=0 frame during streamed DOM rebuilds.
    """
    guard_idx = MESSAGES_JS.find("window._fixMobileScrollJank")
    assert guard_idx != -1, (
        "attachLiveStream() must call window._fixMobileScrollJank() during the "
        "streaming render tick, before DOM writes, so iOS PWA does not jump to "
        "the first/oldest message while assistant output streams."
    )

    render_idx = MESSAGES_JS.find("_lastRenderMs=performance.now()")
    assert render_idx != -1, "streaming render timestamp not found"
    assert guard_idx < render_idx, (
        "The mobile scroll-jank guard must run before streaming DOM work begins."
    )


def test_scroll_repin_dead_zone_is_wider_for_mac_app_windows():
    assert "clientHeight<250" in UI_JS or "bottomDistance<250" in UI_JS, (
        "The near-bottom re-pin threshold should be at least 250px so small "
        "macOS app windows and trackpad momentum do not re-pin too eagerly."
    )


def test_queue_card_measurement_respects_manual_unpin_even_when_idle():
    fn = _extract_function(UI_JS, "_renderQueueChips")
    measurement_idx = fn.find("setTimeout(()=>")
    assert measurement_idx != -1, "queue card measurement timeout not found"
    measurement_block = fn[measurement_idx:measurement_idx + 500]

    assert "scrollIfPinned()" in measurement_block
    assert "scrollToBottom()" not in measurement_block, (
        "queue-card layout measurement is a background update; it must not clear "
        "manual _messageUserUnpinned state by calling explicit scrollToBottom()."
    )


def test_queue_pill_click_respects_manual_unpin_even_when_idle():
    fn = _extract_function(UI_JS, "_updateQueuePill")
    click_idx = fn.find("pill.onclick=()=>")
    assert click_idx != -1, "queue pill click handler not found"
    click_block = fn[click_idx:click_idx + 700]

    assert "scrollIfPinned()" in click_block
    assert "scrollToBottom()" not in click_block, (
        "queue-pill expansion is a layout update, not an explicit transcript jump; "
        "it must not reset manual unpin state."
    )


def test_idle_render_preserves_manual_unpin_until_explicit_bottom():
    render_body = _extract_function(UI_JS, "renderMessages")
    scroll_helper = _extract_function(UI_JS, "_scrollAfterMessageRender")

    assert "preserveScroll||_messageUserUnpinned" in render_body.replace(" ", ""), (
        "renderMessages() must capture a scroll snapshot whenever the reader is "
        "manually unpinned, even for idle/non-preserve renders."
    )
    assert "if(_messageUserUnpinned){" in scroll_helper.replace(" ", ""), (
        "idle render must restore the manually-unpinned viewport instead of "
        "falling through to scrollToBottom()."
    )
    manual_unpin_block = scroll_helper[scroll_helper.index("if(_messageUserUnpinned)") : scroll_helper.index("scrollToBottom();")]
    assert "_restoreMessageScrollSnapshot(scrollSnapshot);" in manual_unpin_block
    assert "_maybeShowNewMessageScrollCue(scrollSnapshot);" in manual_unpin_block
