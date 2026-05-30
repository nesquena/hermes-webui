"""Regression tests for queue pill collapse / reopen layout behavior.

Reported symptom: after collapsing the queued-messages flyout into the queue pill,
clicking the pill could reopen the card without reapplying transcript padding,
which let the flyout slide behind the composer and become effectively invisible.

These tests pin the required UI invariants directly in static/ui.js.
"""

from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def test_hiding_queue_card_clears_queue_open_padding_and_height_var():
    assert "hideBtn.onclick=()=>{" in UI_JS
    hide_idx = UI_JS.find("hideBtn.onclick=()=>{")
    hide_block = UI_JS[hide_idx:hide_idx + 900]

    assert "_msgs.classList.remove('queue-open');" in hide_block
    assert "_msgs.style.removeProperty('--queue-card-height');" in hide_block
    assert "_updateQueuePill(sid,_getSessionQueue(sid,false).length);" in hide_block


def test_zero_queue_cleanup_also_clears_stale_height_var():
    render_idx = UI_JS.find("function _renderQueueChips(sid)")
    assert render_idx != -1, "_renderQueueChips not found"
    empty_idx = UI_JS.find("if(!q.length){", render_idx)
    assert empty_idx != -1, "empty-queue branch not found"
    empty_block = UI_JS[empty_idx:empty_idx + 500]

    assert "_msgs.classList.remove('queue-open');" in empty_block
    assert "_msgs.style.removeProperty('--queue-card-height');" in empty_block


def test_active_session_queue_teardown_clears_height_var():
    badge_idx = UI_JS.find("function updateQueueBadge(sessionId)")
    assert badge_idx != -1, "updateQueueBadge not found"
    badge_block = UI_JS[badge_idx:badge_idx + 1800]

    assert "_msgsEl.classList.remove('queue-open');" in badge_block
    assert "_msgsEl.style.removeProperty('--queue-card-height');" in badge_block
