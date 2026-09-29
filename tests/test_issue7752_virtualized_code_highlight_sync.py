"""Tests for Issue #7752: code block paints unhighlit for one frame when scrolled into view.

In a virtualized transcript, message rows enter and leave the DOM as the user scrolls.
Previously, syntax highlighting (highlightCode) and structured tree-view initialization
(initTreeViews) were deferred entirely to requestAnimationFrame(() => _postProcessWithAnchorSuppression(inner)).
As a result, when newly mounted rows entered the viewport during a virtualized render,
the browser painted them for one frame before the rAF callback executed, causing visible
flash of raw unhighlighted code and missing tree chrome.

The fix runs highlightCode, initTreeViews, and addCopyButtons synchronously before
_scrollAfterMessageRender and first paint, while keeping the deferred pass in place
for async / heavy post-processing (diff, csv, pdf, mermaid, katex) under anchor suppression.
"""
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"


@pytest.fixture(scope="module")
def ui_js_content():
    return UI_JS_PATH.read_text(encoding="utf-8")


def test_render_messages_runs_highlight_and_tree_init_synchronously(ui_js_content):
    """renderMessages must execute highlightCode and initTreeViews synchronously before scroll restoration."""
    render_fn_idx = ui_js_content.find("function renderMessages(options){")
    assert render_fn_idx != -1, "function renderMessages(options) not found in ui.js"
    # Inspect the main render tail where scroll restoration and post-processing occur
    render_tail = ui_js_content[render_fn_idx : render_fn_idx + 60000]

    highlight_call = "if(typeof highlightCode==='function') highlightCode(inner);"
    tree_init_call = "if(typeof initTreeViews==='function') initTreeViews(inner);"
    copy_btn_call = "if(typeof addCopyButtons==='function') addCopyButtons(inner);"
    scroll_call = "_scrollAfterMessageRender(preserveScroll, scrollSnapshot);"
    post_process_raf = "requestAnimationFrame(()=>_postProcessWithAnchorSuppression(inner));"

    assert highlight_call in render_tail, "renderMessages must call highlightCode(inner) synchronously"
    assert tree_init_call in render_tail, "renderMessages must call initTreeViews(inner) synchronously"
    assert copy_btn_call in render_tail, "renderMessages must call addCopyButtons(inner) synchronously"

    highlight_pos = render_tail.rfind(highlight_call)
    scroll_pos = render_tail.rfind(scroll_call)
    raf_pos = render_tail.rfind(post_process_raf)

    assert highlight_pos < scroll_pos, (
        "highlightCode(inner) must run before _scrollAfterMessageRender so dimensions are accurate and no unhighlighted frame is painted"
    )
    assert scroll_pos < raf_pos, (
        "_scrollAfterMessageRender must run before deferred _postProcessWithAnchorSuppression"
    )


def test_cached_session_restore_runs_highlight_synchronously(ui_js_content):
    """The fast-path cached session restore must also synchronously ensure highlighting before scrolling."""
    cache_branch_idx = ui_js_content.find("_sessionHtmlCache.get(sid);")
    assert cache_branch_idx != -1, "Session HTML cache branch not found"
    cache_slice = ui_js_content[cache_branch_idx : cache_branch_idx + 1200]

    highlight_call = "if(typeof highlightCode==='function') highlightCode(inner);"
    tree_init_call = "if(typeof initTreeViews==='function') initTreeViews(inner);"
    scroll_call = "_scrollAfterMessageRender(preserveScroll, scrollSnapshot);"

    assert highlight_call in cache_slice, "Fast-path cache restore must call highlightCode(inner) synchronously"
    assert tree_init_call in cache_slice, "Fast-path cache restore must call initTreeViews(inner) synchronously"

    highlight_pos = cache_slice.find(highlight_call)
    scroll_pos = cache_slice.find(scroll_call)
    assert highlight_pos < scroll_pos, "Fast-path highlightCode must run before _scrollAfterMessageRender"


def test_restore_live_turn_html_runs_highlight_synchronously(ui_js_content):
    """restoreLiveTurnHtmlForSession must execute highlightCode and initTreeViews synchronously on restored node."""
    fn_idx = ui_js_content.find("function restoreLiveTurnHtmlForSession(sid){")
    assert fn_idx != -1, "restoreLiveTurnHtmlForSession not found in ui.js"
    fn_slice = ui_js_content[fn_idx : fn_idx + 2500]

    highlight_call = "if(typeof highlightCode==='function') highlightCode(restored);"
    tree_init_call = "if(typeof initTreeViews==='function') initTreeViews(restored);"
    copy_btn_call = "if(typeof addCopyButtons==='function') addCopyButtons(restored);"
    raf_call = "requestAnimationFrame(()=>_postProcessWithAnchorSuppression(restored));"

    assert highlight_call in fn_slice, "restoreLiveTurnHtmlForSession must call highlightCode(restored) synchronously"
    assert tree_init_call in fn_slice, "restoreLiveTurnHtmlForSession must call initTreeViews(restored) synchronously"
    assert copy_btn_call in fn_slice, "restoreLiveTurnHtmlForSession must call addCopyButtons(restored) synchronously"

    highlight_pos = fn_slice.find(highlight_call)
    raf_pos = fn_slice.find(raf_call)
    assert highlight_pos < raf_pos, "Synchronous highlight must occur before deferred _postProcessWithAnchorSuppression in restoreLiveTurn"


def test_overflow_anchor_suppression_dispatches_preserved(ui_js_content):
    """The fix must preserve all 3 deferred _postProcessWithAnchorSuppression dispatches required for mobile stability."""
    wrapped_dispatch = "requestAnimationFrame(()=>_postProcessWithAnchorSuppression("
    assert ui_js_content.count(wrapped_dispatch) >= 3, (
        "Deferred post-process under anchor suppression must be preserved across fast cache path, render tail, and live-tool remount"
    )
