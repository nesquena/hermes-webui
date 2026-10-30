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

from tests._owned_window_dom_harness import (
    build_script,
    run_node,
    stamp_function_source,
)

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


_TRANSITION_EXTRACTED = [
    "_messageWindowNodeKey",
    "_commitMessageWindow",
    "_messageSessionIndexBase",
    "_messageSessionIndexForRawIdx",
    "_messageRawIdxForSessionIndex",
    "_initializeMessageWindowOwnership",
]

_TRANSITION_STUBS = r"""
const assert = require('assert');
const S = { session: { session_id: 'session-1' }, messages: [] };
let _oldestIdx = 7;
let _messageWindowRevision = 0;
function _rememberMessageWindowReader(){}
function _restoreMessageWindowReader(){}
const initCalls = [];
let phase = 'normal';
function highlightCode(node){ initCalls.push({ kind: 'highlight', node, phase }); node.setAttribute('data-hl', '1'); }
function addCopyButtons(node){ initCalls.push({ kind: 'copy', node, phase }); node.setAttribute('data-copy', '1'); }
function initTreeViews(node){ initCalls.push({ kind: 'tree', node, phase }); node.setAttribute('data-tree-init', '1'); }
const frames = [];
function requestAnimationFrame(cb){ frames.push(cb); return frames.length; }
const container = miniContainer('messages');
const liveInner = miniContainer('msgInner');
function $(id){ return id === 'messages' ? container : (id === 'msgInner' ? liveInner : null); }
function rowHtml(rawIdx, role, text, bodyHtml){
  const abs = _messageSessionIndexForRawIdx(rawIdx);
  return `<div class="msg-row" data-msg-idx="${rawIdx}" data-session-msg-idx="${abs}" data-raw-text="${text}">`
    + `<div class="assistant-bubble" data-role="${role}">`
    + (bodyHtml || `<div class="msg-content">${text}</div>`)
    + `<div class="tool-worklog-group" data-worklog-group="1" data-open="0"></div>`
    + `</div></div>`;
}
"""


def _transition_script(ui_js: str, stamp_first: bool) -> str:
    """Build the first normal-render-to-owned-remount scenario.

    `stamp_first=True` is the canonical repair (stamp before enhancement);
    `stamp_first=False` replays the PREDECESSOR ordering (stamp after
    enhancement) as the control that must fail the reuse assertions.
    """
    # The control variant needs the stamp to happen only AFTER enhancement, so
    # build the two orderings explicitly instead of branching inside one body.
    if stamp_first:
        stamp_block = "_stampRenderTail(liveInner);\n"
    else:
        stamp_block = ""
    body = r"""
// ── phase 1: normal full render (non-owned) — rows land in the live container ──
S.messages = [
  { role: 'assistant', content: 'Answer body' },
  { role: 'assistant', content: 'const answer=1;' },
];
const rawRows = [
  rowHtml(0, 'assistant', 'Answer body'),
  rowHtml(1, 'assistant', 'Answer body', '<div class="msg-content"><pre><code>const answer=1;</code></pre></div>'),
];
for (const html of rawRows){
  const holder = new MiniEl('div');
  holder.innerHTML = html;
  liveInner.appendChild(holder.children[0]);
}
const enhanced = liveInner.children[0];
const enhancedCode = liveInner.children[1];
// Canonical pre-enhancement stamp FIRST — before the enhancement pass and
// before the selection/disclosure markers (any post-render mutation must not
// leak into the retained markup the reuse comparison weighs).
""" + (stamp_block if stamp_first else "") + r"""
enhanced.setAttribute('data-test-selection', '1');
enhanced.querySelector('[data-worklog-group]').setAttribute('data-open', '1');
phase = 'enhance';
highlightCode(enhancedCode); addCopyButtons(enhancedCode); initTreeViews(enhancedCode);
const initAfterEnhance = initCalls.length;
""" + ("" if stamp_first else "_stampRenderTail(liveInner);  // predecessor: stamped AFTER enhancement\n") + r"""
// The normal render tail initializes window ownership on EVERY render (the
// reuse gate in _commitMessageWindow reads it on the next owned remount).
_initializeMessageWindowOwnership(liveInner);
phase = 'owned';
// ── phase 2: owned remount (windowOnly reuse) — identical staged rows + one NEW row ──
S.messages.push({ role: 'assistant', content: 'Newer answer' });
requestAnimationFrame(() => {});   // the deferred post-processing pass queued by the render tail
const staged = new MiniEl('div');
staged.innerHTML = rawRows.join('') + rowHtml(2, 'assistant', 'Newer answer');
_commitMessageWindow(liveInner, staged, null, true);
"""
    if stamp_first:
        body += r"""
// Fresh code/copy/tree readiness happened BEFORE any queued frame flushed:
assert.strictEqual(frames.length, 1, 'the deferred frame must still be queued when the commit returns');
const freshInits = initCalls.slice(initAfterEnhance);
assert.ok(freshInits.length >= 3, 'fresh rows must be initialized synchronously during the commit');
const freshRow = liveInner.children[2];
assert.ok(freshInits.every(c => c.node !== enhanced && c.node !== enhancedCode),
  'reused rows must not be re-initialized (their enhanced DOM is untouched)');
assert.ok(freshInits.some(c => c.node === freshRow && c.kind === 'highlight'), 'fresh row: code readiness');
assert.ok(freshInits.some(c => c.node === freshRow && c.kind === 'copy'), 'fresh row: copy readiness');
assert.ok(freshInits.some(c => c.node === freshRow && c.kind === 'tree'), 'fresh row: tree readiness');
// Unchanged enhanced-node identity + selection/disclosure retention:
assert.strictEqual(liveInner.children[0], enhanced, 'unchanged enhanced row must be REUSED, not replaced');
assert.strictEqual(liveInner.children[1], enhancedCode);
assert.strictEqual(enhanced.getAttribute('data-test-selection'), '1', 'selection identity retained');
assert.strictEqual(enhanced.querySelector('[data-worklog-group]').getAttribute('data-open'), '1', 'disclosure identity retained');
console.log(JSON.stringify({ ok: true, replaced: false }));
"""
    else:
        body += r"""
// PREDECESSOR CONTROL: with the stamp taken after enhancement, the retained
// markup carried enhancement artifacts, so the first owned reuse comparison
// failed and the enhanced rows were REPLACED — selection/disclosure identity
// lost, and the replacement pays a fresh init pass. These assertions encode
// the failure mode the canonical pre-enhancement stamp repairs.
assert.notStrictEqual(liveInner.children[0], enhanced, 'control: the enhanced row is replaced under the predecessor ordering');
assert.notStrictEqual(liveInner.children[1], enhancedCode);
assert.strictEqual(liveInner.children[0].getAttribute('data-test-selection'), null, 'control: selection identity is lost');
assert.strictEqual(liveInner.children[0].getAttribute('data-open'), null, 'control: disclosure identity is lost');
assert.ok(initCalls.slice(initAfterEnhance).length >= 3, 'control: the replacement pays a fresh init pass');
console.log(JSON.stringify({ ok: true, replaced: true }));
"""
    return build_script(
        ui_js,
        _TRANSITION_EXTRACTED,
        _TRANSITION_STUBS + "\nconst STAMP_FIRST=" + ("true" if stamp_first else "false") + ";\n"
        + stamp_function_source(ui_js) + "\n",
        body,
    )


def test_first_render_to_owned_remount_retains_enhanced_rows(ui_js_content):
    """Gate-cert 6a351830 follow-up (finding 3): exercise the FIRST normal-render
    to owned-remount transition behaviorally. The two ordering tests above pin
    source positions; this one runs the real `_commitMessageWindow` over a mini
    DOM and verifies fresh code/copy/tree readiness lands BEFORE queued frames,
    while unchanged enhanced rows keep their node identity plus selection and
    disclosure state across the remount."""
    result = run_node(_transition_script(ui_js_content, stamp_first=True))
    assert result == {"ok": True, "replaced": False}


def test_predecessor_stamp_ordering_replaces_enhanced_rows(ui_js_content):
    """Repair-removal control for the transition test above: replay the
    predecessor ordering (canonical stamp captured AFTER enhancement). The
    behavioral assertions must then fail — the enhanced rows are replaced on
    the first owned remount, selection/disclosure identity is lost, and the
    replacement pays a fresh init pass. If this control ever stops replacing,
    the reuse comparison no longer discriminates the stamp ordering."""
    result = run_node(_transition_script(ui_js_content, stamp_first=False))
    assert result == {"ok": True, "replaced": True}
