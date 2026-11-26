"""#9350 review (2026-11-25): a READ-ONLY preview must not cross workspaces either.

The corrective push bound preservation to the *editor* owner: `_previewEditWorkspace`
expires when the session's workspace moves. A preview that never entered Edit mode has
no baseline (`_previewSavedText === null`) and no edit workspace, so both ownership
predicates accepted it in ANY workspace; entering Edit later stamped the session's
CURRENT workspace on the baseline and Save resolved the relative path against it —
A's content wrote into B's same-named file.

This module drives the production pieces end to end with a fake DOM and an in-memory
filesystem whose save route resolves the posted RELATIVE path against the session's
current workspace (the exact composition that produces the defect):

  real `openFile()` (read-only open) -> `/api/session/update` moves the SAME sid ->
  the forced-reload decision (`_sessionLoadWorkspaceRefreshOpts`) + the real
  `loadDir()` / deferred completion -> the real `toggleEditMode()` Edit and Save.

A second case clicks Edit *before* any reload lands: the adoption point itself must
refuse a preview that came from another workspace.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO = Path(__file__).resolve().parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO / "static" / "workspace.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
NODE_SKIP = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# State declared the way the module declares it. `_previewOpenWorkspace` is the
# workspace that supplied the open preview: pre-fix builds never assign it, so it
# stays '' and the pre-fix predicates accept the read-only preview everywhere.
STATE = """
let _previewCurrentPath='', _previewCurrentMode='', _previewDirty=false, _previewSavedText=null,
    _previewEditSessionId='', _previewEditWorkspace='', _previewOpenWorkspace='',
    _previewServerEditable=null, _previewSaveRoute='/api/file/save', _previewOfficeFormat='',
    _previewPreviewKind='', _previewRawContent='', _previewRawContentPath='',
    _wsTreeGen=0, _previewOpenGen=0, _workspacePanelMode='browse';
const IMAGE_EXTS=new Set(['.png','.jpg']), MD_EXTS=new Set(['.md','.markdown']),
      HTML_EXTS=new Set(['.html']), PDF_EXTS=new Set(['.pdf']),
      AUDIO_EXTS=new Set(['.mp3']), VIDEO_EXTS=new Set(['.mp4']),
      DOWNLOAD_EXTS=new Set(['.zip','.pdf.bin']);
"""

HELPERS = """
const document={documentElement:{dataset:{workspacePanel:'browse'}}};
const els={};
function mk(id){
  if(els[id]) return els[id];
  const el={id,style:{display:''},value:'',textContent:'',innerHTML:'',scrollTop:0,
            src:'',title:'',onclick:null,onkeydown:null};
  el.classList={_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},contains(c){return this._s.has(c);}};
  els[id]=el; return el;
}
['previewArea','previewCode','previewMd','previewImgWrap','previewMediaWrap','previewPdfWrap',
 'previewHtmlWrap','previewEditArea','previewBadge','previewPathText','btnOpenInBrowser',
 'btnEditFile','fileTree','wsEmptyState','gitBadge','breadcrumbBar','btnUpDir',
 'btnRenderMarkdownAnyway','previewImg','previewPdfFrame','previewHtmlIframe',
 'btnCopyPreviewRelPath','btnClearPreview'].forEach(mk);
const S={session:{session_id:'s1',workspace:'/ws/A'},messages:[],entries:[],toolCalls:[],
  currentDir:'.',_dirCache:{},_expandedDirs:new Set()};
// two workspaces, each with its own notes.txt
const fs={'/ws/A':{'notes.txt':'A original'},'/ws/B':{'notes.txt':'B original'}};
const posts=[], downloads=[], prompts=[], refreshes=[], status=[];
function $(id){ return els[id] || null; }
function _workspaceRouteForPath(p,kind){
  const k=kind||'list';
  return '/api/file/'+k+'?path='+encodeURIComponent(p||'.');
}
async function api(path,opts){
  const u=String(path);
  if(u.indexOf('/api/file/save')===0){
    const body=JSON.parse(opts.body);
    // The save route resolves the posted RELATIVE path against the session's
    // CURRENT workspace: that is what turns a crossed preview into data loss.
    const rec={ws:S.session.workspace,path:body.path,content:body.content,session_id:body.session_id};
    posts.push(rec); fs[S.session.workspace][body.path]=body.content;
    return {content:body.content};
  }
  if(u.indexOf('/api/file/read')===0){
    const p=decodeURIComponent(/path=([^&]*)/.exec(u)[1]);
    const v=(fs[S.session.workspace]||{})[p];
    if(v===undefined){ const e=new Error('not found'); e.status=404; throw e; }
    return {content:v};
  }
  if(u.indexOf('/api/file/list')===0) return {entries:[]};
  return {};
}
function _workspaceEscapeGrantForPath(){ return null; }
function _clearWorkspaceEscapeGrant(){}
function downloadFile(p){ downloads.push(p); }
function renderFileBreadcrumb(){}
function renderBreadcrumb(){}
function renderFileTree(){}
function renderSessionArtifacts(){}
function renderMarkdownPreviewContent(){}
function renderCsvPreviewContent(){}
// seam: the renderer only paints what the read returned (DOM/Prism otherwise).
function renderCodePreviewContent(path,content){ showPreview('code'); $('previewCode').textContent=content; }
function refreshOpenPreviewIfMutated(){ refreshes.push(1); return Promise.resolve(); }
function setStatus(s){ status.push(String(s||'')); }
function setLargeMarkdownForceRenderVisible(){}
function showConfirmDialog(opts){ prompts.push(opts); return Promise.resolve(true); }
function showToast(){}
function setStatusText(){}
function t(k){ return k; }
function closeWorkspacePanel(){}
function openWorkspacePanel(){}
function syncWorkspacePanelUI(){}
function syncWorkspaceDisplays(){}
function syncTerminalButton(){}
function _syncWorkspaceBirthtimeSupportScope(){}
function _restoreExpandedDirs(){}
function _refreshGitBadge(){}
function _workspacePathIsReadOnly(){ return false; }
function shouldRenderMarkdownPreviewAsPlainText(){ return false; }
function largeMarkdownPlainTextStatus(){ return ''; }
function _awaitElementLoad(){ return Promise.resolve(true); }
function _mountMediaPlayer(){ return null; }
function _applyMediaPlaybackPreferences(){}
function _awaitMediaReady(){ return Promise.resolve(true); }
function _workspaceRawReachable(){ return Promise.resolve(true); }
"""


def _function_block(src: str, marker: str) -> str:
    """Balanced function body from an exact opening marker (handles `opts={}` signatures)."""
    start = src.find(marker)
    assert start >= 0, f"{marker!r} not found"
    params_end = src.find("){", start)
    assert params_end >= 0, f"{marker!r} body not found"
    brace = params_end + 1
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start: idx + 1]
    raise AssertionError(f"{marker!r} body did not close")


def _open_file_slice() -> str:
    start = WORKSPACE_JS.index("async function openFile(path, opts={}){")
    end = WORKSPACE_JS.index("\nfunction downloadFile")
    return WORKSPACE_JS[start:end]


def _js() -> str:
    parts = [
        STATE,
        HELPERS,
        extract_function(SESSIONS_JS, "_sessionLoadWorkspaceRefreshOpts"),
        extract_function(WORKSPACE_JS, "fileExt"),
        extract_function(WORKSPACE_JS, "_previewOpenOwned"),
        _function_block(WORKSPACE_JS, "function showPreview("),
        _open_file_slice(),
        _function_block(WORKSPACE_JS, "async function loadDir("),
        extract_function(WORKSPACE_JS, "_previewBelongsToWorkspace"),
        extract_function(WORKSPACE_JS, "_previewHiddenEditsOwnedHere"),
        extract_function(WORKSPACE_JS, "_previewHasUnsavedEdits"),
        extract_function(WORKSPACE_JS, "_previewEditorOnScreen"),
        extract_function(WORKSPACE_JS, "_syncPreviewDirty"),
        extract_function(WORKSPACE_JS, "_resetPreviewDirty"),
        extract_function(WORKSPACE_JS, "updateEditBtn"),
        extract_function(WORKSPACE_JS, "toggleEditMode", "async function"),
        _function_block(BOOT_JS, "function clearPreview("),
    ]
    return "\n".join(parts)


SCENARIO = r"""
async function scenario(loadedWorkspace, doReload){
  // 1. Open A's notes.txt NORMALLY — no Edit click, no baseline.
  S.session.workspace='/ws/A';
  await openFile('notes.txt');
  const opened={
    ownerWs:(typeof _previewOpenWorkspace==='undefined'?null:_previewOpenWorkspace),
    savedText:_previewSavedText, editWs:_previewEditWorkspace,
    path:_previewCurrentPath, visible:$('previewArea').classList.contains('visible'),
  };
  // 2. /api/session/update moves the SAME sid to another workspace.
  S.session.workspace=loadedWorkspace;
  let afterReload=null;
  if(doReload){
    // 3. Forced reload of the same SID -> deferred directory refresh.
    const opts=_sessionLoadWorkspaceRefreshOpts(true,'s1','s1');
    await loadDir('.',opts);
    afterReload={preserved:refreshes.length, path:_previewCurrentPath,
                 baseline:_previewSavedText, editWs:_previewEditWorkspace,
                 visible:$('previewArea').classList.contains('visible')};
  }
  // 4. Ordinary Edit click, then the ordinary Save click.
  await toggleEditMode();
  const adopted={editing:$('previewEditArea').style.display!=='none',
                 savedText:_previewSavedText, editWs:_previewEditWorkspace,
                 path:_previewCurrentPath};
  $('previewEditArea').value='A edited text';
  _syncPreviewDirty();
  await toggleEditMode();
  return {opened, afterReload, adopted, saves:posts.slice(),
          fs:JSON.parse(JSON.stringify(fs))};
}
scenario(LOADED, RELOAD).then(out=>console.log(JSON.stringify(out)))
  .catch(e=>{console.error(e&&e.stack||String(e));process.exit(1);});
"""


def _run(loaded_workspace: str, do_reload: bool):
    assert NODE, "node is required"
    script = (_js() + SCENARIO
              .replace("LOADED", json.dumps(loaded_workspace))
              .replace("RELOAD", "true" if do_reload else "false"))
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@NODE_SKIP
def test_read_only_preview_from_the_old_workspace_is_dropped_by_the_reload_and_cannot_save():
    """Open (no Edit) in A -> same-SID move to B -> forced reload -> Edit/Save."""
    out = _run("/ws/B", True)
    assert out["opened"]["savedText"] is None and out["opened"]["editWs"] == "", (
        "the fixture must stay a read-only preview: entering Edit would stamp an owner"
    )
    assert out["afterReload"]["preserved"] == 0, (
        "a preview whose content came from A must not be preserved by a reload that "
        "installs workspace B: Save resolves its relative path against B"
    )
    assert out["afterReload"]["path"] == "" and out["afterReload"]["baseline"] is None, (
        "the relative path and the baseline must not survive the workspace move"
    )
    assert out["afterReload"]["visible"] is False, "the foreign preview must be cleared"
    assert out["adopted"]["editing"] is False, "there is nothing left to edit after the clear"
    assert out["saves"] == [], "no /api/file/save may carry A's content into B; got %r" % (out["saves"],)
    assert out["fs"]["/ws/B"]["notes.txt"] == "B original", "B's file must be untouched"
    assert out["fs"]["/ws/A"]["notes.txt"] == "A original", "A's file must be untouched"
    assert out["opened"]["ownerWs"] == "/ws/A", (
        "opening a preview must record the workspace that supplied its content; "
        "without that record a read-only preview belongs to no tree in particular"
    )


@NODE_SKIP
def test_edit_before_the_reload_lands_refuses_to_adopt_a_foreign_preview():
    """The adoption point: Edit clicked while the preview still belongs to A."""
    out = _run("/ws/B", False)
    assert out["adopted"]["editing"] is False, (
        "entering Edit stamps the session's CURRENT workspace on the baseline; a preview "
        "from another workspace must be refused instead of adopted"
    )
    assert out["adopted"]["editWs"] != "/ws/B" and out["adopted"]["savedText"] is None, (
        "the baseline must not be taken in B for content that came from A; got %r"
        % (out["adopted"],)
    )
    assert out["saves"] == [], "no Save may be issued for the crossed preview; got %r" % (out["saves"],)
    assert out["fs"]["/ws/B"]["notes.txt"] == "B original", "B's file must be untouched"
    assert out["fs"]["/ws/A"]["notes.txt"] == "A original"


@NODE_SKIP
def test_same_workspace_reload_still_preserves_a_read_only_preview_and_its_save():
    """Control: inside one workspace the read-only preview survives and stays savable."""
    out = _run("/ws/A", True)
    assert out["afterReload"]["preserved"] == 1, "a same-workspace reload must refresh, not clear"
    assert out["afterReload"]["path"] == "notes.txt" and out["afterReload"]["visible"] is True
    assert out["adopted"]["editing"] is True, "the preserved preview must still enter Edit"
    assert out["adopted"]["editWs"] == "/ws/A"
    assert len(out["saves"]) == 1, "the preserved preview must still be savable"
    assert out["saves"][0]["path"] == "notes.txt"
    assert out["saves"][0]["content"] == "A edited text"
    assert out["saves"][0]["session_id"] == "s1"
    assert out["fs"]["/ws/A"]["notes.txt"] == "A edited text"
    assert out["fs"]["/ws/B"]["notes.txt"] == "B original"
    assert out["opened"]["ownerWs"] == "/ws/A", (
        "the supplying workspace is the one recorded at open time"
    )


def test_the_preview_open_records_the_supplying_workspace_and_clear_preview_drops_it():
    """Source-level lock: the record is written where the content is read and reset on close."""
    assert "_previewOpenWorkspace=" in _open_file_slice(), (
        "openFile() must record the workspace that supplied the preview"
    )
    clear = _function_block(BOOT_JS, "function clearPreview(")
    assert "_previewOpenWorkspace" in clear, (
        "clearPreview() must drop the recorded owner with the rest of the preview state"
    )
    belongs = extract_function(WORKSPACE_JS, "_previewBelongsToWorkspace")
    assert "_previewOpenWorkspace" in belongs, (
        "_previewBelongsToWorkspace() must fall back to the supplying workspace when there "
        "is no edit baseline"
    )
