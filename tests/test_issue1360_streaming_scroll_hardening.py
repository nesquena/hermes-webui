"""Regression tests for #1360: streaming must not re-pin user scroll."""

from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
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


def test_messages_scroller_disables_browser_scroll_anchoring():
    assert "overflow-anchor:none" in STYLE_CSS, (
        "#messages must disable browser scroll anchoring so tool/card inserts "
        "cannot yank the transcript while the user reads earlier content."
    )


def test_scroll_repin_dead_zone_is_wider_for_mac_app_windows():
    assert "clientHeight<250" in UI_JS, (
        "The near-bottom re-pin threshold should be at least 250px so small "
        "macOS app windows and trackpad momentum do not re-pin too eagerly."
    )


def test_queue_card_measurement_does_not_force_repin_during_streaming():
    fn = _extract_function(UI_JS, "_renderQueueChips")
    measurement_idx = fn.find("setTimeout(()=>")
    assert measurement_idx != -1, "queue card measurement timeout not found"
    measurement_block = fn[measurement_idx:measurement_idx + 500]

    assert "_syncQueueUiScroll();" in measurement_block
    assert "S.activeStreamId" not in measurement_block
    assert "scrollToBottom()" not in measurement_block


def test_queue_pill_click_does_not_force_repin_during_streaming():
    fn = _extract_function(UI_JS, "_updateQueuePill")
    click_idx = fn.find("pill.onclick=()=>")
    assert click_idx != -1, "queue pill click handler not found"
    click_block = fn[click_idx:click_idx + 1200]

    assert "_syncQueueUiScroll();" in click_block
    assert "S.activeStreamId" not in click_block
    assert "scrollToBottom()" not in click_block



def test_queue_pill_click_reapplies_queue_open_layout_before_focusing_card():
    fn = _extract_function(UI_JS, "_updateQueuePill")
    click_idx = fn.find("pill.onclick=()=>")
    assert click_idx != -1, "queue pill click handler not found"
    click_block = fn[click_idx:click_idx + 1400]

    assert "const _msgs=document.getElementById('messages');" in click_block
    assert "const _applyQueueLayout=()=>{" in click_block
    assert "_msgs.classList.add('queue-open');" in click_block
    assert "_msgs.style.setProperty('--queue-card-height', h+'px');" in click_block
    assert click_block.find("_applyQueueLayout();") < click_block.find("firstFocusable.focus();"), (
        "queue pill reopen must reserve transcript space before focusing the queue card, "
        "otherwise the flyout can reopen under the composer and disappear from view."
    )
