import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_body(src: str, name: str) -> str:
    """Full source of a top-level `[async] function name(...)` declaration.

    Delegates to the comment/string-aware scanner defined further down
    (_js_function_source) so a `'` inside a code comment cannot swallow the
    rest of the body.
    """
    fn = _js_function_source(src, name)
    assert fn is not None, f"{name}() not found in source"
    return fn


def _compact(src: str) -> str:
    return "".join(src.split())


def test_preview_toolbar_has_copy_relative_path_button():
    assert 'id="btnCopyPreviewRelPath"' in INDEX
    assert 'onclick="copyPreviewRelativePath()"' in INDEX
    assert 'data-i18n="copy_relative_path"' in INDEX
    assert "Copy relative path" in INDEX


def test_preview_copy_relative_path_uses_current_preview_path():
    body = _function_body(WORKSPACE_JS, "copyPreviewRelativePath")
    compact = _compact(body)

    assert "_previewCurrentPath" in body
    assert "_normalizeWorkspaceRelPath(_previewCurrentPath)" in body
    assert "api('/api/file/path'" not in body
    assert "constrel=_normalizeWorkspaceRelPath(_previewCurrentPath)||_previewCurrentPath" in compact


def test_preview_copy_relative_path_disables_button_while_request_is_in_flight():
    body = _function_body(WORKSPACE_JS, "copyPreviewRelativePath")
    compact = _compact(body)

    guard = "if(btn&&btn.disabled)return;"
    disable = "if(btn)btn.disabled=true;"
    enable = "finally{if(btn)btn.disabled=false;}"
    assert "$('btnCopyPreviewRelPath')" in body
    assert guard in compact
    assert disable in compact
    assert enable in compact
    assert compact.index(guard) < compact.index(disable)
    assert compact.index(disable) < compact.index("_normalizeWorkspaceRelPath")


def test_preview_copy_relative_path_reuses_clipboard_fallback_and_toasts():
    body = _function_body(WORKSPACE_JS, "copyPreviewRelativePath")
    assert "typeof _copyTextWithFallback==='function'" in body
    assert "_copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'))" in body
    assert "navigator.clipboard.writeText(rel)" in body
    assert "document.execCommand('copy')" in body
    assert "t('path_copied')" in body
    assert "t('path_copy_failed')" in body


def test_tree_context_menu_keeps_absolute_copy_and_adds_relative_copy():
    assert "copyPathItem.textContent=t('copy_file_path')" in UI_JS
    assert "copyRelPathItem.textContent=t('copy_relative_path')" in UI_JS
    assert "const rel=_normalizeWorkspaceRelPath(item.path)||item.path" in UI_JS
    assert "_copyTextWithFallback(rel,t('path_copied'),t('path_copy_failed'))" in UI_JS


def test_preview_toolbar_keeps_copy_button_from_shrinking_path_layout():
    assert ".preview-path #btnCopyPreviewRelPath" in STYLE
    selector_start = STYLE.index(".preview-path #btnCopyPreviewRelPath")
    selector_block = STYLE[selector_start : STYLE.index("}", selector_start) + 1]
    assert "flex-shrink:0" in selector_block
    assert "white-space:nowrap" in selector_block


def test_preview_copy_button_is_accessible_and_icon_only_on_narrow_pane():
    """The preview-header copy button must stay accessible when its text label is
    hidden on a narrow pane (#5548 icon-only fold-in): it carries an aria-label,
    its label span is class-tagged, and a narrow-width media query hides that label.
    """
    import re
    # The button carries an explicit aria-label (screen-reader name survives label-hide).
    assert 'id="btnCopyPreviewRelPath"' in INDEX
    btn = INDEX[INDEX.index('id="btnCopyPreviewRelPath"'):]
    btn = btn[: btn.index("</button>")]
    assert 'aria-label="Copy relative path"' in btn
    assert 'class="preview-btn-label"' in btn
    # Localized tooltip + accessible name (WCAG 2.5.3): the icon-only state must not
    # leave a Russian/German user with an English tooltip/screen-reader name.
    assert 'data-i18n-title="copy_relative_path"' in btn
    assert 'data-i18n-aria-label="copy_relative_path"' in btn
    # A narrow-PANE container query (right panel, not viewport) hides the label
    # (icon-only), keeping the glyph — so it fires on pane resize even on desktop.
    assert re.search(
        r"@container\s+rightpanel[^{]*max-width:\s*520px[^{]*\{[^}]*"
        r"\.preview-path\s+#btnCopyPreviewRelPath\s+\.preview-btn-label\s*\{\s*display:\s*none",
        STYLE,
    ), "expected a @container rightpanel query hiding the copy-button label on a narrow pane"


def test_preview_toolbar_has_copy_content_button():
    assert 'id="btnCopyPreviewContent"' in INDEX
    assert 'onclick="copyPreviewContent()"' in INDEX
    assert 'data-i18n="copy_file_contents"' in INDEX
    assert "Copy file contents" in INDEX


def test_preview_copy_content_uses_current_preview_raw_content():
    """The copy button reads the cache through the shared ownership gate.

    Maintainer review defect #1: path equality alone is not ownership, because
    a delayed save re-labels the cache from the live `_previewCurrentPath`. The
    gate therefore requires the cached text to belong to the displayed file AND
    to the current preview generation. The behaviour is covered by
    test_delayed_save_* below; this pins the wiring so the caller cannot grow a
    second, weaker copy of the rules.
    """
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    compact = _compact(body)

    assert "_previewCurrentPath" in body
    assert "previewRawContentIsCopyable()" in compact
    assert "constcontent=_previewRawContent;" in compact
    # The caller must not re-implement (or weaken) the ownership check.
    assert "_previewRawContentPath!==_previewCurrentPath" not in compact
    assert "typeof_previewRawContent!=='string'" not in compact

    gate = _compact(_function_body(WORKSPACE_JS, "previewRawContentIsCopyable"))
    assert "typeof_previewRawContent==='string'" in gate
    assert "_previewRawContentPath===_previewCurrentPath" in gate
    assert "_previewRawContentGen===_previewGen" in gate
    assert "!_previewRawContentBinary" in gate


def test_preview_copy_content_fails_when_content_not_available():
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    compact = _compact(body)

    guard = "if(!previewRawContentIsCopyable()){"
    fallback_toast = "showToast(t('content_not_available'),null,'error');"
    assert guard in compact
    assert fallback_toast in compact
    guard_idx = compact.index(guard)
    assert compact.index(fallback_toast, guard_idx) == guard_idx + len(guard)
    assert compact.index(fallback_toast) < compact.index("constcontent=_previewRawContent;")
    assert "return;" in compact[compact.index(fallback_toast):compact.index(fallback_toast) + len(fallback_toast) + 10]


def test_preview_copy_content_disables_button_while_request_is_in_flight():
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    compact = _compact(body)

    guard = "if(btn&&btn.disabled)return;"
    disable = "if(btn)btn.disabled=true;"
    enable = "finally{if(btn)btn.disabled=false;}"
    assert "$('btnCopyPreviewContent')" in body
    assert guard in compact
    assert disable in compact
    assert enable in compact
    assert compact.index(guard) < compact.index(disable)
    assert compact.index(disable) < compact.index("previewRawContentIsCopyable()")


def test_preview_copy_content_reuses_clipboard_fallback_and_toasts():
    body = _function_body(WORKSPACE_JS, "copyPreviewContent")
    # copyPreviewContent must delegate to the shared clipboard helper and NOT
    # carry its own duplicated inline navigator.clipboard/execCommand fallback
    # (finding #4 — the helper always exists, so the inline block was dead code).
    assert "_copyTextWithFallback(content,t('content_copied'),t('content_copy_failed'))" in body
    assert "navigator.clipboard.writeText(content)" not in body
    assert "document.execCommand('copy')" not in body
    assert "t('content_copied')" in body
    assert "t('content_copy_failed')" in body

    # renderCodePreviewContent must cache the raw text + its path so the button
    # copies the currently-previewed code file rather than stale md/csv text
    # (finding #1).
    render = _function_body(WORKSPACE_JS, "renderCodePreviewContent")
    render_compact = _compact(render)
    assert "_previewRawContent=content;" in render_compact
    assert "_previewRawContentPath=path;" in render_compact


def test_preview_toolbar_keeps_copy_content_button_from_shrinking_path_layout():
    assert ".preview-path #btnCopyPreviewContent" in STYLE
    selector_start = STYLE.index(".preview-path #btnCopyPreviewContent")
    selector_block = STYLE[selector_start : STYLE.index("}", selector_start) + 1]
    assert "flex-shrink:0" in selector_block
    assert "white-space:nowrap" in selector_block


def test_reset_text_preview_copy_state_hides_button_and_clears_cache():
    """Greptile auto-review (PR #6957): a failed md/code/csv load must not leave
    the copy-content button visible in a stale state carried over from the
    previously-previewed file. resetTextPreviewCopyState() must clear the raw
    content cache (and its path) and hide the button.
    """
    body = _function_body(WORKSPACE_JS, "resetTextPreviewCopyState")
    compact = _compact(body)

    # The clearing itself lives in the shared chokepoint, so the cache, its
    # generation stamp, the binary flag and the button state can never be
    # cleared in three different half-ways.
    assert "invalidatePreviewRawContent()" in compact
    invalidate = _compact(_function_body(WORKSPACE_JS, "invalidatePreviewRawContent"))
    assert "_previewRawContent=''" in invalidate
    assert "_previewRawContentPath=''" in invalidate
    assert "_previewRawContentGen=-1" in invalidate
    assert "_previewRawContentBinary=false" in invalidate
    assert "syncPreviewCopyContentBtn()" in invalidate
    sync = _compact(_function_body(WORKSPACE_JS, "syncPreviewCopyContentBtn"))
    assert "$('btnCopyPreviewContent')" in sync
    assert "btn.style.display=previewRawContentIsCopyable()?'inline-flex':'none'" in sync


def test_reset_text_preview_copy_state_guards_against_stale_request_ownership():
    """Greptile P1 (PR #6957, r3768442266): if file A's request fails after file B
    has already become the current preview, A's stale catch must not clobber B's
    cached content or hide B's copy button. resetTextPreviewCopyState() takes the
    owning path and skips the reset when that path no longer matches the current
    preview.
    """
    body = _function_body(WORKSPACE_JS, "resetTextPreviewCopyState")
    compact = _compact(body)

    assert "resetTextPreviewCopyState(ownerPath,previewGen)" in compact
    guard = "if(ownerPath&&_previewCurrentPath!==ownerPath)return;"
    assert guard in compact
    # The guard must run before the cache/button are cleared.
    assert compact.index(guard) < compact.index("invalidatePreviewRawContent()")


def test_reset_text_preview_copy_state_guards_against_stale_generation():
    """Maintainer review (PR #6957 comment 5272907466): a path-equality guard alone
    cannot distinguish two overlapping openFile() calls for the SAME path — request
    A for notes.md, request B for notes.md, B succeeds, A then fails must not clear
    B's fresh cache/button. resetTextPreviewCopyState() must additionally reject a
    stale generation via previewGenerationIsStale(), and that check must run before
    the cache/button are cleared.
    """
    body = _function_body(WORKSPACE_JS, "resetTextPreviewCopyState")
    compact = _compact(body)

    guard = "if(previewGenerationIsStale(previewGen))return;"
    assert guard in compact
    assert compact.index(guard) < compact.index("invalidatePreviewRawContent()")


def test_preview_generation_counter_exists_and_openfile_captures_it():
    """The preview-open generation counter mirrors the existing workspace-tree
    generation pattern (_wsTreeGen / bumpWorkspaceTreeGen, used by loadDir()) so
    that overlapping openFile() calls can be told apart. openFile() must capture
    the generation immediately after the DOWNLOAD_EXTS early return, before any
    other state is touched.
    """
    assert "let _previewGen = 0;" in WORKSPACE_JS
    assert "function bumpPreviewGeneration(){" in WORKSPACE_JS
    assert "function previewGenerationIsStale(previewGen){" in WORKSPACE_JS

    compact = _compact(WORKSPACE_JS)
    assert "constpreviewGen=bumpPreviewGeneration();" in compact

    download_guard = "if(DOWNLOAD_EXTS.has(ext)){downloadFile(path);return;}"
    assert download_guard in compact
    capture = "constpreviewGen=bumpPreviewGeneration();"
    capture_idx = compact.index(capture)
    assert compact.index(download_guard) < capture_idx
    # Nothing else in openFile() writes preview state before the generation is captured.
    assert capture_idx < compact.index("_previewServerEditable=null;", capture_idx)


def test_markdown_open_file_failure_resets_copy_state_with_request_owner():
    """The markdown branch of openFile() must call resetTextPreviewCopyState(path,
    previewGen) on load failure, passing its own request's path and generation as
    the owner, so a stale failure can't clobber a newer file's copy-button state
    (Greptile P1 PR #6957 finding r3768442266; generation guard added per
    maintainer review comment 5272907466).
    """
    # openFile()'s default-parameter signature (`opts={}`) breaks the brace-matching
    # _function_body() helper (its own `{}` closes before the real body opens), so
    # this checks the whole-file compact source instead, anchored around the
    # markdown branch's render call and failure catch.
    compact = _compact(WORKSPACE_JS)

    catch_marker = (
        "}catch(e){"
        "if(previewGenerationIsStale(previewGen))return;"
        "resetTextPreviewCopyState(path,previewGen);setStatus(t('file_open_failed'));"
        "}"
    )
    assert catch_marker in compact
    assert "renderMarkdownPreviewContent(data);" in compact
    assert compact.index("renderMarkdownPreviewContent(data);") < compact.index(catch_marker)
    assert "MD_EXTS.has(ext)" in compact
    assert compact.index("MD_EXTS.has(ext)") < compact.index(catch_marker)


def test_csv_and_code_open_file_failures_also_pass_request_owner():
    """The CSV and plain-code/text branches of openFile() must likewise pass their
    own path and generation as the owner to resetTextPreviewCopyState(), so stale
    failures in those branches can't clobber a newer preview either (Greptile P1
    PR #6957 finding r3768442266; generation guard added per maintainer review
    comment 5272907466). All three text-preview failure branches (markdown, csv,
    plain code/text) call resetTextPreviewCopyState(path, previewGen), each guarded
    by an immediately-preceding stale-generation return.
    """
    compact = _compact(WORKSPACE_JS)
    assert compact.count("resetTextPreviewCopyState(path,previewGen);") == 3
    # No call site still uses the old ownerless or generation-less signatures.
    assert "resetTextPreviewCopyState();" not in compact
    assert "resetTextPreviewCopyState(path);" not in compact
    # Every catch block that resets copy state bails out first when stale.
    stale_return_before_reset = (
        "if(previewGenerationIsStale(previewGen))return;"
        "resetTextPreviewCopyState(path,previewGen);"
    )
    assert compact.count(stale_return_before_reset) == 3


def test_openfile_checks_staleness_immediately_after_each_awaited_read():
    """Each of the markdown, csv, and plain-code/text branches must reject a stale
    response immediately after its awaited /api read and before any cache/render
    write, mirroring loadDir()'s `if(...||treeGen!==_wsTreeGen)return;` pattern
    (maintainer review PR #6957 comment 5272907466). This closes the race where
    an old file-A success could render after the user navigated to file B, or a
    stale SUCCESS could overwrite a newer same-path response.
    """
    compact = _compact(WORKSPACE_JS)
    stale_check = "if(previewGenerationIsStale(previewGen))return;"
    # markdown, csv, and plain-code/text branches each have one post-await check
    # in the try body, plus one in the catch — six total, plus one more inside
    # resetTextPreviewCopyState() itself (defense in depth) — seven total.
    assert compact.count(stale_check) == 7

    read_call = "awaitapi(_workspaceRouteForPath(path,'read'));"
    idx = 0
    found = 0
    while True:
        idx = compact.find(read_call, idx)
        if idx == -1:
            break
        after = compact[idx + len(read_call): idx + len(read_call) + len(stale_check)]
        assert after == stale_check, f"expected staleness check immediately after read at {idx}"
        found += 1
        idx += len(read_call)
    assert found == 3


def test_bump_workspace_tree_gen_pattern_is_mirrored_by_preview_generation():
    """Sanity check that the preview generation guard follows the same shape as
    the pre-existing workspace-tree generation guard used by loadDir(), rather
    than diverging into a different mechanism.
    """
    ws_gen = _function_body(WORKSPACE_JS, "bumpWorkspaceTreeGen")
    preview_gen = _function_body(WORKSPACE_JS, "bumpPreviewGeneration")
    normalized_ws_gen = (
        _compact(ws_gen)
        .replace("bumpWorkspaceTreeGen", "bumpPreviewGeneration")
        .replace("_wsTreeGen", "_previewGen")
    )
    assert normalized_ws_gen == _compact(preview_gen)


def test_preview_copy_content_button_is_accessible_and_icon_only_on_narrow_pane():
    """The preview-header copy-content button must stay accessible when its text
    label is hidden on a narrow pane (#5548 icon-only fold-in): it carries an
    aria-label, its label span is class-tagged, and a narrow-width media query
    hides that label.
    """
    import re
    # The button carries an explicit aria-label (screen-reader name survives label-hide).
    assert 'id="btnCopyPreviewContent"' in INDEX
    btn = INDEX[INDEX.index('id="btnCopyPreviewContent"'):]
    btn = btn[: btn.index("</button>")]
    assert 'aria-label="Copy file contents"' in btn
    assert 'class="preview-btn-label"' in btn
    # Localized tooltip + accessible name (WCAG 2.5.3): the icon-only state must not
    # leave a Russian/German user with an English tooltip/screen-reader name.
    assert 'data-i18n-title="copy_file_contents"' in btn
    assert 'data-i18n-aria-label="copy_file_contents"' in btn
    # A narrow-PANE container query (right panel, not viewport) hides the label
    # (icon-only), keeping the glyph — so it fires on pane resize even on desktop.
    assert re.search(
        r"@container\s+rightpanel[^{]*max-width:\s*520px[^{]*\{[\s\S]*?"
        r"\.preview-path\s+#btnCopyPreviewContent\s+\.preview-btn-label\s*\{\s*display:\s*none",
        STYLE,
    ), "expected a @container rightpanel query hiding the copy-content-button label on a narrow pane"


# ── Behavioural harness: real production functions driven in Node ────────────
# Maintainer review (PR #6957, CHANGES_REQUESTED on f6405feb) found three
# defects that a source-string assert cannot see, because all three are about
# ORDERING and observable STATE rather than about which tokens appear in a
# function body:
#
#   1. a delayed save relabels file A's content as file B and copies it,
#   2. unknown/binary files are offered for copy after lossy UTF-8 replacement
#      decoding (api/workspace.py read_file_content decodes with
#      errors='replace' and sets no `binary` flag),
#   3. localized copy failures are downgraded to short informational toasts
#      (showToast()'s type sniffing only recognises English words).
#
# The helpers below pull the REAL functions out of static/workspace.js and
# static/ui.js, run them under Node against a minimal DOM stub, and assert what
# the user can observe: what landed on the clipboard, whether the copy control
# is offered, and the toast's level + dismiss delay. Functions that only exist
# after the fix are shimmed when absent, so a pre-fix tree still exercises the
# real (buggy) production ordering and fails on the assertion rather than on a
# missing symbol.


def _js_function_source(src: str, name: str):
    """Return the full source of a top-level `[async] function name(...)`
    declaration, or None when that function does not exist."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return None
    idx = src.index("(", m.end() - 1)
    depth = 0
    while idx < len(src):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                break
        idx += 1
    brace = src.index("{", idx)
    depth = 0
    j = brace
    while j < len(src):
        ch = src[j]
        if ch in "'\"`":
            quote = ch
            j += 1
            while j < len(src):
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    break
                j += 1
        elif ch == "/" and src[j + 1 : j + 2] == "/":
            j = src.index("\n", j)
        elif ch == "/" and src[j + 1 : j + 2] == "*":
            j = src.index("*/", j) + 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : j + 1]
        j += 1
    raise AssertionError(f"{name}() body did not close in source")


def _js_functions(src: str, names) -> str:
    """Concatenate the real source of every named function that exists."""
    out = []
    for name in names:
        fn = _js_function_source(src, name)
        if fn is not None:
            out.append(fn)
    return "\n".join(out)


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "--input-type=commonjs", "-e", script],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            "node harness failed\nstdout:\n" + (result.stdout or "<empty>")
            + "\nstderr:\n" + (result.stderr or "<empty>")
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


# Minimal DOM/i18n stub. `t()` returns the key so assertions can match on the
# i18n key instead of on one locale's wording.
_DOM_STUB = r"""
const ELS={};
function _mkEl(id){
  return {
    id:id, style:{display:'',cssText:''}, value:'', textContent:'', innerHTML:'',
    title:'', disabled:false, className:'', dataset:{}, _attrs:{}, _children:[],
    classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
               contains(c){return this._s.has(c);}},
    setAttribute(k,v){this._attrs[k]=String(v);},
    getAttribute(k){return Object.prototype.hasOwnProperty.call(this._attrs,k)?this._attrs[k]:null;},
    removeAttribute(k){delete this._attrs[k];},
    appendChild(c){this._children.push(c);return c;},
    removeChild(c){this._children=this._children.filter(x=>x!==c);return c;},
    remove(){}, select(){}, focus(){}, click(){},
  };
}
const $=(id)=>ELS[id]||(ELS[id]=_mkEl(id));
const document={createElement:(tag)=>_mkEl('<'+tag+'>'),
                body:{appendChild(){},removeChild(){}},
                execCommand:()=>false};
const t=(k)=>k;
const toasts=[];
function showToast(msg,ms,type){toasts.push({msg:String(msg),ms:(ms===undefined?null:ms),type:type||null});}
const statuses=[];
function setStatus(msg){statuses.push(String(msg));}
const copied=[];
function _copyTextWithFallback(text,okMsg,failPrefix){copied.push(text);showToast(okMsg);return Promise.resolve();}
function requestAnimationFrame(){}
function fileExt(p){const m=String(p||'').match(/\.[^./\\]+$/);return m?m[0].toLowerCase():'';}
function _workspacePathIsReadOnly(){return false;}
function updateEditBtn(){}
function setLargeMarkdownForceRenderVisible(){}
function _prismLanguageForPath(){return '';}
function renderMarkdownPreviewContent(){}
function buildCsvTablePreview(){return {html:'<table></table>'};}
const tick=()=>new Promise(r=>setImmediate(r));

let _previewCurrentPath='';
let _previewCurrentMode='';
let _previewDirty=false;
let _previewServerEditable=null;
let _previewSaveRoute='/api/file/save';
let _previewOfficeFormat='';
let _previewPreviewKind='';
let _previewRawContent='';
let _previewRawContentPath='';
let _previewRawContentGen=-1;
let _previewRawContentBinary=false;
let _previewGen=0;
let S={session:{session_id:'sess-1'}};
let api=async()=>({});

// Shim for the post-fix ownership chokepoint so a PRE-fix tree still runs the
// real buggy production ordering and fails on the assertion below.
function _claim(path,gen){
  if(typeof claimPreviewRawContent==='function') return claimPreviewRawContent(path,gen);
  return true;
}
"""

_PREVIEW_FNS = (
    "bumpPreviewGeneration",
    "previewGenerationIsStale",
    "previewTextLooksBinary",
    "claimPreviewRawContent",
    "invalidatePreviewRawContent",
    "previewRawContentIsCopyable",
    "previewRawContentIsBinaryForCurrentPreview",
    "syncPreviewCopyContentBtn",
    "resetTextPreviewCopyState",
    "showPreview",
    "renderCodePreviewContent",
    "renderCsvPreviewContent",
    "copyPreviewContent",
    "toggleEditMode",
)


def _run_preview_scenario(scenario: str) -> dict:
    script = (
        _DOM_STUB
        + _js_functions(WORKSPACE_JS, _PREVIEW_FNS)
        + "\n(async()=>{\n" + scenario + "\n})().catch(e=>{console.error(e);process.exit(1);});\n"
    )
    return _run_node(script)


# ── Defect 1: a delayed save must not relabel file A's content as file B ─────


def _delayed_save_scenario(b_loaded: bool) -> dict:
    return _run_preview_scenario(
        """
  _previewCurrentMode='md';
  const genA=bumpPreviewGeneration();
  _previewCurrentPath='notes/a.md';
  _previewRawContent='A ORIGINAL';
  _previewRawContentPath='notes/a.md';
  _claim('notes/a.md',genA);

  // The user is editing a.md in the preview editor.
  $('previewEditArea').style.display='';
  $('previewEditArea').value='A EDITED';

  const saveCalls=[];
  let release=null;
  api=(route,opts)=>{
    const body=JSON.parse(opts.body);
    saveCalls.push(body);
    return new Promise(res=>{release=()=>res({content:body.content});});
  };

  const savePromise=toggleEditMode();   // real save path, still in flight
  await tick();

  // While the save is in flight the user opens b.md. openFile()'s synchronous
  // prologue bumps the preview generation and repoints the panel at b.md.
  const genB=bumpPreviewGeneration();
  _previewCurrentPath='notes/b.md';
  if(B_LOADED){
    // b.md's read resolves and caches its own text.
    _previewRawContent='B CONTENT';
    _previewRawContentPath='notes/b.md';
    _claim('notes/b.md',genB);
  }

  release();                            // ...and only now does the save land
  await savePromise;
  await copyPreviewContent();

  console.log(JSON.stringify({
    copied, toasts, statuses, saveCalls,
    rawContent:_previewRawContent, rawPath:_previewRawContentPath,
    currentPath:_previewCurrentPath,
  }));
""".replace("B_LOADED", "true" if b_loaded else "false")
    )


@requires_node
def test_delayed_save_cannot_relabel_previous_file_content_as_current_file():
    """Maintainer review defect #1 (production ordering).

    toggleEditMode() awaits /api/file/save and then re-labels the preview
    raw-content cache from the LIVE globals (`_previewRawContentPath =
    _previewCurrentPath`). openFile() assigns `_previewCurrentPath`
    synchronously, so a save that completes after the user opened b.md stamps
    a.md's just-saved text with b.md's path — and the copy button, whose only
    ownership check was that same path equality, then copies a.md's text while
    the panel shows b.md. Cache ownership must be bound to the displayed file
    AND the preview generation that produced the text.
    """
    out = _delayed_save_scenario(b_loaded=True)

    assert out["saveCalls"] and out["saveCalls"][0]["path"] == "notes/a.md", (
        f"the save must target the edited file: {out['saveCalls']}"
    )
    assert "A EDITED" not in out["copied"], (
        "a delayed save relabelled a.md's content as b.md and the copy button "
        f"copied it: {out}"
    )
    assert out["copied"] == ["B CONTENT"], (
        f"the copy button must copy the displayed file's own content: {out}"
    )
    assert out["rawContent"] == "B CONTENT", f"stale save clobbered b.md's cache: {out}"
    assert out["rawPath"] == "notes/b.md", f"cache label no longer matches its text: {out}"


@requires_node
def test_delayed_save_landing_on_a_still_loading_file_refuses_to_copy():
    """Same ordering, but b.md's read has not resolved yet: the stale save must
    not be able to present a.md's text as b.md's content. With no provable
    owner the copy must fail closed with a localized error, not copy the wrong
    file (guideline 3: "unknown" is not "allowed").
    """
    out = _delayed_save_scenario(b_loaded=False)

    assert out["copied"] == [], f"copied content that belongs to another file: {out}"
    assert out["rawPath"] != "notes/b.md", (
        f"a.md's text was relabelled with b.md's path: {out}"
    )
    assert out["toasts"], f"a refused copy must tell the user why: {out}"
    last = out["toasts"][-1]
    assert last["msg"] == "content_not_available", f"unexpected toast: {out['toasts']}"
    assert last["type"] == "error", (
        f"a refused copy is an error-level notification, got {last}"
    )


@requires_node
def test_preview_copy_content_still_copies_the_freshly_loaded_file():
    """Control case: the happy path must keep working — a normal load caches the
    text and the button copies exactly that text (no over-blocking)."""
    out = _run_preview_scenario(
        """
  _previewCurrentPath='docs/notes.md';
  const gen=bumpPreviewGeneration();
  _previewCurrentMode='md';
  _previewRawContent='# Title\\n\\nBody with emoji 🐾 and CJK 漢字.';
  _previewRawContentPath='docs/notes.md';
  _claim('docs/notes.md',gen);
  await copyPreviewContent();
  console.log(JSON.stringify({copied,toasts}));
"""
    )
    assert out["copied"] == ["# Title\n\nBody with emoji 🐾 and CJK 漢字."], out
    assert out["toasts"][-1]["msg"] == "content_copied", out


# ── Defect 2: binary / lossily-decoded text is never copyable ────────────────


_LOSSY_UNKNOWN_TYPE = "PK\\u0003\\u0004\\u0000\\u0000\\uFFFD\\uFFFD\\uFFFDjunk"


def _binary_gate_scenario(render_call: str, content_js: str, mode_hint: str = "") -> dict:
    return _run_preview_scenario(
        """
  _previewCurrentPath='PATH';
  const gen=bumpPreviewGeneration();
  MODE_HINT
  const content=CONTENT;
  RENDER_CALL
  const btn=$('btnCopyPreviewContent');
  await copyPreviewContent();
  console.log(JSON.stringify({
    copied, toasts, statuses,
    display:btn.style.display, ariaDisabled:btn.getAttribute('aria-disabled'),
    title:btn.title, ariaLabel:btn.getAttribute('aria-label'),
  }));
""".replace("RENDER_CALL", render_call)
        .replace("CONTENT", content_js)
        .replace("MODE_HINT", mode_hint)
        .replace("PATH", "reports/blob.dat")
    )


@requires_node
def test_unknown_type_binary_file_is_not_offered_as_copyable_text():
    """Maintainer review defect #2 (production path).

    api/workspace.py read_file_content() decodes every non-Office file with
    `raw.decode('utf-8', errors='replace')` and returns no `binary` flag, so
    openFile()'s unknown-type branch hands renderCodePreviewContent() text in
    which undecodable bytes have become U+FFFD. That text is not the file's
    content, so it must never be offered for copy.
    """
    out = _binary_gate_scenario(
        "renderCodePreviewContent(_previewCurrentPath, content);",
        "'" + _LOSSY_UNKNOWN_TYPE + "'",
    )

    assert out["display"] == "none", (
        f"the copy control is still offered for lossily-decoded binary text: {out}"
    )
    assert out["copied"] == [], f"lossy replacement text reached the clipboard: {out}"
    assert out["toasts"], f"a refused copy must explain itself: {out}"
    last = out["toasts"][-1]
    assert last["msg"] == "content_binary_not_copyable", (
        f"expected the localized binary state, got {out['toasts']}"
    )
    assert last["type"] == "error", f"expected an error-level notification: {last}"
    assert out["ariaDisabled"] == "true", f"the control must read as disabled: {out}"
    assert out["title"] == "content_binary_not_copyable", (
        f"the disabled reason must be localized through t(): {out}"
    )


@requires_node
def test_nul_byte_binary_file_is_not_offered_as_copyable_text():
    """Sibling branch (fix the class): a NUL-carrying file that reaches the code
    preview must be gated the same way — NUL never occurs in text files."""
    out = _binary_gate_scenario(
        "renderCodePreviewContent(_previewCurrentPath, content);",
        "'sqlite format 3\\u0000\\u0000\\u0000binary'",
    )
    assert out["display"] == "none", out
    assert out["copied"] == [], out
    assert out["toasts"][-1]["msg"] == "content_binary_not_copyable", out


@requires_node
def test_csv_branch_binary_content_is_not_offered_as_copyable_text():
    """Sibling branch (fix the class): the .csv branch caches through
    renderCsvPreviewContent(), which must apply the same gate."""
    out = _binary_gate_scenario(
        "renderCsvPreviewContent(_previewCurrentPath, content);",
        "'a,b\\n\\u0000\\uFFFD,2'",
    )
    assert out["display"] == "none", out
    assert out["copied"] == [], out
    assert out["toasts"][-1]["msg"] == "content_binary_not_copyable", out


@requires_node
def test_markdown_branch_binary_content_is_not_offered_as_copyable_text():
    """Sibling branch (fix the class): the markdown branch caches inline, then
    hands the (path, generation) pair to the shared ownership chokepoint, so the
    same gate must apply to a .md file whose bytes were not valid UTF-8."""
    out = _binary_gate_scenario(
        "_previewRawContent=content;_previewRawContentPath=_previewCurrentPath;"
        "_claim(_previewCurrentPath,gen);",
        "'# Heading\\n\\uFFFD\\uFFFD\\uFFFD'",
        mode_hint="showPreview('md');",
    )
    assert out["display"] == "none", out
    assert out["copied"] == [], out
    assert out["toasts"][-1]["msg"] == "content_binary_not_copyable", out


@requires_node
def test_clean_unicode_text_is_still_copyable():
    """The gate must not over-block: real text with emoji, CJK and accents has
    no NUL and no U+FFFD, so it stays copyable."""
    out = _binary_gate_scenario(
        "renderCodePreviewContent(_previewCurrentPath, content);",
        "'const s=\"héllo 漢字 🐾\";'",
    )
    assert out["display"] == "inline-flex", out
    assert out["copied"] == ['const s="héllo 漢字 🐾";'], out
    assert out["toasts"][-1]["msg"] == "content_copied", out


@requires_node
def test_markdown_branch_routes_its_cache_write_through_the_ownership_chokepoint():
    """The three text-preview branches must all reach the SAME chokepoint, so the
    binary gate and the (path, generation) ownership stamp cannot drift apart."""
    compact = _compact(WORKSPACE_JS)
    assert "_previewRawContent=data.content;_previewRawContentPath=path;claimPreviewRawContent(path,previewGen);" in compact, (
        "the markdown branch must hand its cache write to claimPreviewRawContent()"
    )
    for renderer in ("renderCodePreviewContent", "renderCsvPreviewContent"):
        body = _compact(_function_body(WORKSPACE_JS, renderer))
        assert "claimPreviewRawContent(path)" in body, (
            f"{renderer} must claim the cache through the shared chokepoint"
        )
        assert "invalidatePreviewRawContent()" in body, (
            f"{renderer} must fail closed when it has no text to cache"
        )


# ── Defect 3: localized copy failures are error-level, long-lived toasts ─────


_TOAST_FNS = ("clearToastDismissTimer", "setToastDismissTimer", "dismissToast",
              "showToast", "copyToastText", "_copyTextWithFallback")


def _locale_string(locale: str, key: str) -> str:
    """Pull one locale's literal string for `key` out of static/i18n.js."""
    start = I18N_JS.index("\n  " + locale + ": {")
    block = I18N_JS[start:start + 200000]
    m = re.search(r"\n\s+" + re.escape(key) + r": '((?:[^'\\]|\\.)*)'", block)
    assert m, f"{key} missing from the {locale} locale"
    return m.group(1)


def _run_copy_failure_scenario(failure_prefix: str, clipboard: str) -> dict:
    toast_consts = "\n".join(
        line for line in UI_JS.splitlines()
        if line.startswith("const TOAST_DEFAULT_MS=") or line.startswith("const TOAST_ERROR_DEFAULT_MS=")
    )
    assert "TOAST_DEFAULT_MS=" in toast_consts and "TOAST_ERROR_DEFAULT_MS=" in toast_consts
    script = (
        r"""
const ELS={};
function _mkEl(id){
  return {id:id, style:{display:'',cssText:''}, value:'', textContent:'', innerHTML:'',
    className:'', dataset:{}, _t:null,
    classList:{_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},
               contains(c){return this._s.has(c);}},
    appendChild(){}, removeChild(){}, remove(){}, select(){}, focus(){},
    closest(){return this;}};
}
const $=(id)=>ELS[id]||(ELS[id]=_mkEl(id));
const document={createElement:(tag)=>_mkEl('<'+tag+'>'),
                body:{appendChild(){},removeChild(){}},
                execCommand:()=>false};
const esc=(s)=>String(s==null?'':s);
const t=(k)=>'[[' + k + ']]';
const delays=[];
const setTimeout=(cb,ms)=>{delays.push(ms);return delays.length;};
const clearTimeout=()=>{};
const navigator=CLIPBOARD;
"""
        + toast_consts + "\n"
        + _js_functions(UI_JS, _TOAST_FNS) + "\n"
        + r"""
(async()=>{
  await _copyTextWithFallback('payload','ok-message',FAILURE_PREFIX);
  await new Promise(r=>setImmediate(r));
  const el=$('toast');
  console.log(JSON.stringify({
    className:el.className, html:el.innerHTML, text:el.textContent,
    message:el.dataset.toastMessage, delays,
  }));
})().catch(e=>{console.error(e);process.exit(1);});
"""
    ).replace("CLIPBOARD", clipboard).replace("FAILURE_PREFIX", json.dumps(failure_prefix))
    return _run_node(script)


def _rejecting_clipboard(reason_js: str) -> str:
    return "{clipboard:{writeText:()=>Promise.reject(" + reason_js + ")}}"


# showToast() sniffs the toast level from the message text with an English-only
# regex, so whether a copy failure reads as an error depends on the wording the
# BROWSER appended — not on the copy failing. Firefox's clipboard rejection
# ("blocked due to lack of user activation") and an empty-message rejection both
# carry no English keyword, so a localized prefix left the user with a 2.8s
# info toast and no Copy/Dismiss affordance.
_FIREFOX_REJECTION = "new Error('Clipboard write was blocked due to lack of user activation.')"
_NO_REASON_REJECTION = "undefined"
_NO_CLIPBOARD = "{}"


@requires_node
def test_localized_copy_failure_is_an_error_level_long_lived_toast():
    """Maintainer review defect #3 (production path).

    _copyTextWithFallback()'s failure branch calls showToast() with no explicit
    type, and showToast() sniffs the level from the MESSAGE TEXT against an
    English-only regex (/fail|error|denied|.../). The English prefix matches, so
    en users get the 20s dismissible error toast — but every localized prefix
    (ru/ja/de/...) falls through to a 2.8s auto-dismissed info toast with no
    Copy/Dismiss affordance. The level must not depend on the locale.
    """
    ru_prefix = _locale_string("ru", "content_copy_failed")
    out = _run_copy_failure_scenario(ru_prefix, _rejecting_clipboard(_FIREFOX_REJECTION))

    assert "error" in out["className"].split(), (
        f"a localized copy failure was downgraded to {out['className']!r}: {out}"
    )
    assert max(out["delays"]) >= 20000, (
        f"a localized copy failure auto-dismissed after {out['delays']}ms: {out}"
    )
    assert "toast-dismiss" in out["html"], (
        f"an error toast must carry the Dismiss affordance: {out}"
    )
    assert ru_prefix in out["message"], f"the localized message was lost: {out}"


@requires_node
def test_english_copy_failure_stays_error_level():
    """Control case: the English path already produced an error toast and must
    keep doing so (the fix must not flip the level for any locale)."""
    en_prefix = _locale_string("en", "content_copy_failed")
    out = _run_copy_failure_scenario(en_prefix, _rejecting_clipboard(_FIREFOX_REJECTION))
    assert "error" in out["className"].split(), out
    assert max(out["delays"]) >= 20000, out


@requires_node
def test_copy_failure_with_no_reason_text_is_still_error_level():
    """A rejection carrying no reason (engines that reject with `undefined`)
    leaves the toast text as the localized prefix alone — the level must come
    from the call site, not from sniffing the message."""
    for locale in ("ru", "ja", "de"):
        prefix = _locale_string(locale, "content_copy_failed")
        out = _run_copy_failure_scenario(prefix, _rejecting_clipboard(_NO_REASON_REJECTION))
        assert "error" in out["className"].split(), (locale, out)
        assert max(out["delays"]) >= 20000, (locale, out)


@requires_node
def test_clipboard_unavailable_failure_is_localized():
    """With no async clipboard API and execCommand('copy') refusing, the failure
    reason was the hard-coded English literal 'clipboard unavailable' appended
    to a localized prefix. It must come from t() like every other string."""
    out = _run_copy_failure_scenario(
        _locale_string("ru", "content_copy_failed"), _NO_CLIPBOARD
    )
    assert "clipboard unavailable" not in out["message"], (
        f"untranslated English literal surfaced to the user: {out}"
    )
    assert "[[clipboard_unavailable]]" in out["message"], (
        f"the clipboard-unavailable reason must be resolved through t(): {out}"
    )
    assert "error" in out["className"].split(), out


def test_new_copy_state_strings_exist_in_every_locale_that_ships_the_feature():
    """Fallbacks/defaults are contracts: the new strings must land in the SAME
    locale set as the feature's existing copy strings, not in `en` only."""
    baseline = I18N_JS.count("content_not_available:")
    assert baseline >= 15, f"unexpected locale coverage baseline: {baseline}"
    for key in ("content_binary_not_copyable", "clipboard_unavailable"):
        assert I18N_JS.count(key + ":") == baseline, (
            f"{key} ships in {I18N_JS.count(key + ':')} locales, expected {baseline}"
        )
