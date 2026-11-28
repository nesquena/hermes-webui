"""#9350 review item 1: a same-SID reload must not carry an editor from another workspace.

`/api/session/update` can move the SAME session to a different workspace. After
that, a focus-triggered force reload (`loadSession(sid, {force:true})` ->
`_deferWorkspaceRefreshForSession(sid, {preservePreview:true})`) must fall back to
the discard/clear semantics of a workspace transition: the save route resolves a
RELATIVE preview path against the session's CURRENT workspace, so an editor kept
from workspace A writes A's text into B's same-named file.

The scenario below drives the production pieces end to end with a fake DOM:
`_sessionLoadWorkspaceRefreshOpts()` (the decision), the real `loadDir()` with the
options a forced reload produces, the real `clearPreview()`, and then a real
`toggleEditMode()` — the Save — to show where the text would land.
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

STATE = """
let _previewCurrentPath='', _previewCurrentMode='', _previewDirty=false, _previewSavedText=null,
    _previewEditSessionId='', _previewEditWorkspace='', _previewServerEditable=null,
    _previewSaveRoute='/api/file/save', _previewOfficeFormat='', _previewPreviewKind='',
    _previewRawContent='', _previewRawContentPath='', _wsTreeGen=0, _previewOpenGen=0,
    _workspacePanelMode='browse';
"""

HELPERS = """
const document={documentElement:{dataset:{workspacePanel:'browse'}}};
const els={};
function mk(id){
  if(els[id]) return els[id];
  const el={id,style:{display:''},value:'',textContent:'',innerHTML:'',scrollTop:0};
  el.classList={_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},contains(c){return this._s.has(c);}};
  els[id]=el; return el;
}
const PREVIEW_IDS=['previewArea','previewCode','previewMd','previewImgWrap','previewMediaWrap',
  'previewPdfWrap','previewHtmlWrap','previewEditArea','previewBadge','previewPathText',
  'btnOpenInBrowser','btnEditFile','fileTree','wsEmptyState','gitBadge','msg'];
PREVIEW_IDS.forEach(mk);
const S={session:{session_id:'s1',workspace:'/ws/A'},messages:[],entries:[],toolCalls:[],
  currentDir:'.',_dirCache:{},_expandedDirs:new Set()};
const posts=[], prompts=[], refreshes=[], toast=[];
function $(id){ return els[id] || null; }
async function api(path,opts){
  if(path==='/api/file/save'){ posts.push(JSON.parse(opts.body)); return {content:JSON.parse(opts.body).content}; }
  if(String(path).indexOf('/api/list')>=0) return {entries:[]};
  return {};
}
function showConfirmDialog(opts){ prompts.push(opts); return Promise.resolve(true); }
function showToast(){}
function setStatus(){}
function t(k){ return k; }
function renderBreadcrumb(){}
function renderFileTree(){}
function renderSessionArtifacts(){}
function renderMarkdownPreviewContent(){}
function renderCsvPreviewContent(){}
function refreshOpenPreviewIfMutated(){ refreshes.push('refresh'); return Promise.resolve(); }
function _syncWorkspaceBirthtimeSupportScope(){}
function _restoreExpandedDirs(){}
function _workspaceRouteForPath(p){ return '/api/list?path='+encodeURIComponent(p||'.'); }
function _refreshGitBadge(){}
function _workspacePathIsReadOnly(){ return false; }
function closeWorkspacePanel(){}
function openWorkspacePanel(){}
function syncWorkspacePanelUI(){}
function syncWorkspaceDisplays(){}
function syncTerminalButton(){}
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
                return src[start : idx + 1]
    raise AssertionError(f"{marker!r} body did not close")


def _js() -> str:
    parts = [
        STATE,
        HELPERS,
        extract_function(SESSIONS_JS, "_sessionLoadWorkspaceRefreshOpts"),
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
async function scenario(loadedWorkspace){
  // The open preview was opened and edited while the session belonged to A.
  _previewCurrentPath='note.txt';
  _previewCurrentMode='code';
  _previewSavedText='A original';
  _previewEditSessionId='s1';
  _previewEditWorkspace='/ws/A';
  $('previewEditArea').value='A edited text';
  $('previewEditArea').style.display='';
  $('previewArea').classList.add('visible');
  $('fileTree').style.display='none';
  _syncPreviewDirty();                       // _previewDirty is derived, never hand-set
  const dirtyBefore=_previewDirty;
  // /api/session/update moved the SAME session to B and loadSession installed it.
  S.session.workspace=loadedWorkspace;
  // The forced reload of the same SID computes its deferred workspace options...
  const opts=_sessionLoadWorkspaceRefreshOpts(true,'s1','s1');
  // ...and loadDir runs 150 ms later, after the returned session is installed.
  await loadDir('.', opts);
  const afterReload={
    preserved:refreshes.length,
    prompts:prompts.length,
    editorText:$('previewEditArea').value,
    baseline:_previewSavedText,
    previewPath:_previewCurrentPath,
    editorDisplay:$('previewEditArea').style.display,
  };
  // A subsequent Save click with the editor still on screen.
  $('previewEditArea').style.display = '';
  await toggleEditMode();
  return {dirtyBefore, ...afterReload, saves:posts.slice()};
}
scenario(LOADED).then(out=>console.log(JSON.stringify(out)))
  .catch(e=>{console.error(e&&e.stack||String(e));process.exit(1);});
"""


def _run(loaded_workspace: str):
    assert NODE, "node is required"
    script = _js() + SCENARIO.replace("LOADED", json.dumps(loaded_workspace))
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@NODE_SKIP
def test_workspace_change_refuses_preservation_and_the_next_save_cannot_land_in_the_new_tree():
    """Session moved A -> B, forced reload: prompt, drop the editor, and no Save into B."""
    out = _run("/ws/B")
    assert out["dirtyBefore"] is True, "fixture must start with an on-screen dirty editor"
    assert out["prompts"] == 1, (
        "a forced reload whose loaded workspace no longer owns the editor must keep the "
        "discard/cancel protection of a workspace transition"
    )
    assert out["preserved"] == 0, (
        "the preview must not be preserved: loadDir()'s preserve branch re-reads the file "
        "instead of asking, and A's text would then save into B's note.txt"
    )
    assert out["baseline"] is None, "the editor baseline must be dropped with the preview"
    assert out["previewPath"] == "", "the relative preview path must not survive the move"
    assert out["saves"] == [], (
        "no /api/file/save may be issued for B carrying A's edited text; got %r" % (out["saves"],)
    )


@NODE_SKIP
def test_same_workspace_reload_still_preserves_the_dirty_editor_and_its_save():
    """#9338's control: inside one workspace the reload keeps the edit, and the Save still works."""
    out = _run("/ws/A")
    assert out["dirtyBefore"] is True
    assert out["prompts"] == 0, "a same-workspace reload must not nag about discarding edits"
    assert out["preserved"] == 1, "a same-workspace forced reload must refresh, not clear"
    assert out["baseline"] == "A original", "the preserved editor keeps its baseline"
    assert out["previewPath"] == "note.txt", "the preserved editor keeps its relative path"
    assert out["editorText"] == "A edited text", "loadDir() must never overwrite dirty text"
    assert len(out["saves"]) == 1, "the preserved editor must still be savable"
    assert out["saves"][0]["path"] == "note.txt"
    assert out["saves"][0]["content"] == "A edited text"
    assert out["saves"][0]["session_id"] == "s1"
