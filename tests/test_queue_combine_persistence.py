"""Regression tests for queue combine persistence and visible-card rerender behavior.

Issue: queue actions like Combine and per-item X can mutate the live queue while
focus remains on a queue button inside the visible card. The renderer must not
skip rerender in that case, or the card looks stale even though the data changed.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_queue_persistence_uses_dual_storage_helpers():
    assert "const QUEUE_STORAGE_PREFIX='hermes-queue-';" in UI_JS
    assert "function _persistSessionQueueStorage(sid, queue){" in UI_JS
    assert "localStorage.setItem(key,payload);" in UI_JS
    assert "sessionStorage.setItem(key,payload);" in UI_JS
    assert "function _readPersistedSessionQueue(sid){" in UI_JS
    assert "const localValue=read(localStorage);" in UI_JS


def test_queue_combine_uses_common_save_refresh_path():
    """Combine should persist via _saveAndRefresh(), not an ad-hoc write path."""
    anchor = "mergeBtn.onclick=()=>{"
    start = UI_JS.index(anchor)
    end = UI_JS.index("const clearBtn=document.createElement('button');", start)
    block = UI_JS[start:end]

    assert "_saveAndRefresh();" in block, (
        "Combine flow must call _saveAndRefresh() so queue state, render cache, and "
        "badge stay in sync with later per-item actions."
    )
    assert "SESSION_QUEUES[sid]=liveQ;" not in block, (
        "Combine flow should not bypass the common persistence helper with a direct "
        "SESSION_QUEUES assignment."
    )
    assert "sessionStorage.setItem('hermes-queue-'+sid,JSON.stringify(liveQ))" not in block, (
        "Combine flow should not write queue storage directly; use _saveAndRefresh()."
    )


def test_queue_rerender_guard_only_skips_inline_text_editing():
    """Visible queue card must rerender for focused buttons like X/Combine."""
    assert "const _activeQueueEl=document.activeElement;" in UI_JS
    assert "const _editingQueueText=!!(" in UI_JS
    assert "_activeQueueEl.classList.contains('queue-card-text')" in UI_JS
    assert "if(_editingQueueText) return;" in UI_JS
    assert "if(inner.contains(document.activeElement)&&document.activeElement!==inner) return;" not in UI_JS, (
        "Broad 'any focus inside queue card' guard leaves the visible card stale after "
        "button-driven mutations."
    )


def test_queue_delete_still_targets_live_queue_by_timestamp():
    """Per-item delete must still resolve against the current live queue."""
    assert "const idx=_entryTs!=null?liveQ.findIndex(e=>e&&e._queued_at===_entryTs):i;" in UI_JS
    assert "if(idx!==-1) liveQ.splice(idx,1);" in UI_JS
