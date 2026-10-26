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
    # Inspect the main render tail: slice to the next top-level function so the slice covers the main render tail (+96k chars)
    _prox = ui_js_content.find("\nfunction ", render_fn_idx + 1)
    render_tail = ui_js_content[render_fn_idx : _prox if _prox != -1 else render_fn_idx + 120000]

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


def test_session_html_cache_preserves_pre_highlight_markup(ui_js_content):
    """Session HTML cache must snapshot inner.innerHTML before highlightCode and addCopyButtons to prevent dead handlers on cache restore."""
    render_fn_idx = ui_js_content.find("function renderMessages(options){")
    assert render_fn_idx != -1
    _prox = ui_js_content.find("\nfunction ", render_fn_idx + 1)
    render_tail = ui_js_content[render_fn_idx : _prox if _prox != -1 else render_fn_idx + 120000]

    cache_capture = "cacheHtml=inner.innerHTML;"
    highlight_call = "if(typeof highlightCode==='function') highlightCode(inner);"
    tree_init_call = "if(typeof initTreeViews==='function') initTreeViews(inner);"
    copy_btn_call = "if(typeof addCopyButtons==='function') addCopyButtons(inner);"
    cache_store = "const _html=cacheHtml;"

    assert cache_capture in render_tail, "renderMessages must snapshot inner.innerHTML into cacheHtml before synchronous passes"
    assert highlight_call in render_tail
    assert tree_init_call in render_tail
    assert copy_btn_call in render_tail
    assert cache_store in render_tail, "renderMessages must store cacheHtml into _sessionHtmlCache"

    cap_pos = render_tail.rfind(cache_capture)
    hl_pos = render_tail.rfind(highlight_call)
    store_pos = render_tail.rfind(cache_store)

    assert cap_pos < hl_pos < store_pos, "cacheHtml must be captured before highlightCode and stored in _sessionHtmlCache"


def test_cached_session_restore_runs_highlight_synchronously(ui_js_content):
    """The fast-path cached session restore must also synchronously ensure highlighting before scrolling."""
    cache_branch_idx = ui_js_content.find("_sessionHtmlCache.get(sid);")
    assert cache_branch_idx != -1, "Session HTML cache branch not found"
    # Bound at the next top-level function, never a fixed char count: a fixed
    # slice breaks on its own when the integrated cache branch grows (#7912
    # lesson — the +1200 slice here silently lost the scroll call after the
    # reader-retention integration extended the cache path). First-occurrence
    # find() semantics keep the oracle pointed at the cache branch's own calls.
    _prox = ui_js_content.find("\nfunction ", cache_branch_idx + 1)
    cache_slice = ui_js_content[cache_branch_idx : _prox if _prox != -1 else cache_branch_idx + 4000]

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
    # A fatia vai até a próxima function de topo, nunca um limite fixo de chars:
    # limite fixo quebra sozinho quando o master acrescenta linhas no TOPO da
    # função alvo. Medido no #7912 — o fix do #6948 entrou com 12 linhas a mais
    # (comentário + else-if) e a chamada passou de +1809 para +2540, estourando
    # o `+ 2500` antigo. CI reproduziu em 3 versões de Python; local não via.
    _prox = ui_js_content.find("\nfunction ", fn_idx + 1)
    _teto = _prox if _prox != -1 else fn_idx + 60000
    fn_slice = ui_js_content[fn_idx:_teto]
    assert len(fn_slice) > 500, "restoreLiveTurnHtmlForSession slice came out too short"

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


def test_render_path_stamps_canonical_pre_enhancement_markup(ui_js_content):
    """Gate-cert 9ba1d994 finding 3: the render tail stamped _messageWindowMarkup
    AFTER extension-slot hydration and the synchronous highlight/copy/tree
    passes, while _commitMessageWindow compares those stamps against raw staged
    markup. An unchanged enhanced row therefore failed its first
    full-render-to-owned-render reuse comparison and got replaced. The canonical
    stamp must be captured before any enhancement mutates the staged rows."""
    render_fn_idx = ui_js_content.find("function renderMessages(options){")
    assert render_fn_idx != -1, "function renderMessages(options) not found in ui.js"
    _prox = ui_js_content.find("\nfunction ", render_fn_idx + 1)
    render_tail = ui_js_content[render_fn_idx : _prox if _prox != -1 else render_fn_idx + 120000]

    stamp = r"for(const node of inner.children) node._messageWindowMarkup=node.outerHTML.replace(/contain-intrinsic-size: auto [\d.]+px;/g,'');"
    guarded_sync = "if(typeof _syncExtensionMessageActionSlots==='function') _syncExtensionMessageActionSlots(inner);"
    highlight_call = "if(typeof highlightCode==='function') highlightCode(inner);"

    assert render_tail.count(stamp) == 1, (
        "the canonical pre-enhancement stamp must appear exactly once in the render tail"
    )
    stamp_pos = render_tail.index(stamp)
    sync_pos = render_tail.rindex(guarded_sync)
    highlight_pos = render_tail.rindex(highlight_call)
    assert stamp_pos < sync_pos, (
        "the canonical stamp must be captured before extension-slot hydration"
    )
    assert sync_pos < highlight_pos, (
        "extension-slot hydration must precede the synchronous highlight pass"
    )


def test_commit_message_window_initializes_fresh_rows_before_reader_restore(ui_js_content):
    """Gate-cert 9ba1d994 finding 3: the owned early return skipped master's
    synchronous highlightCode/addCopyButtons/initTreeViews pass, so newly
    committed rows painted one frame of raw unhighlighted text (#7752 contract).
    _commitMessageWindow must run the lightweight initialization on freshly
    committed rows only — reused rows keep their enhanced DOM untouched — and
    before the reader restore samples geometry."""
    fn_idx = ui_js_content.find("function _commitMessageWindow(")
    assert fn_idx != -1, "function _commitMessageWindow not found in ui.js"
    _prox = ui_js_content.find("\nfunction ", fn_idx + 1)
    commit_body = ui_js_content[fn_idx : _prox if _prox != -1 else fn_idx + 12000]

    fresh_decl = "const desired=[],freshRows=[];"
    reuse_keep = "if(old&&old._messageWindowMarkup===node._messageWindowMarkup){ desired.push(old); continue; }"
    fresh_push = "freshRows.push(node);"
    init_gate = "if(freshRows.length){"
    reader_restore = "_restoreMessageWindowReader(target,anchor);"

    assert fresh_decl in commit_body, "fresh rows must be tracked separately from reused rows"
    assert reuse_keep in commit_body, "reused rows must keep their enhanced DOM (identity/selection)"
    assert fresh_push in commit_body

    init_pos = commit_body.index(init_gate)
    restore_pos = commit_body.index(reader_restore)
    assert init_pos < restore_pos, (
        "fresh-row initialization must run before the reader restore samples geometry"
    )
    init_block = commit_body[init_pos:restore_pos]
    for call in (
        "if(typeof highlightCode==='function') highlightCode(node);",
        "if(typeof addCopyButtons==='function') addCopyButtons(node);",
        "if(typeof initTreeViews==='function') initTreeViews(node);",
    ):
        assert call in init_block, f"fresh-row init must include {call.split('(')[0]}"
    assert "}catch(_){" in init_block, (
        "the fresh-row init must stay fail-safe so a bad block cannot abort the commit"
    )
