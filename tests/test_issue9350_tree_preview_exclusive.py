"""#9350 review item 2: the file tree and the open preview stay mutually exclusive.

`openFile()` hides `#fileTree` and shows the preview, but `renderFileTree()` sets
`box.style.display=''` unconditionally. Any render that lands while a preview is
open — the deferred root refresh of a same-session reload, an expand/collapse,
`loadDir('.', {preservePreview:true})`, the hidden-files toggle — therefore paints
the tree back next to the preview, each taking half of the workspace body.

The render path is the single door, so the decision belongs there (and in the
empty state), not at each caller.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")
NODE_SKIP = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_block(src: str, marker: str) -> str:
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


HARNESS = r"""
const els={};
function mk(id){
  if(els[id]) return els[id];
  const el={id,style:{display:''},textContent:'',innerHTML:'',scrollTop:0,children:[]};
  el.classList={_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},contains(c){return this._s.has(c);}};
  el.appendChild=(child)=>{el.children.push(child);};
  els[id]=el; return el;
}
const S={session:{session_id:'s1',workspace:'/ws/A'},entries:[],currentDir:'.',_dirCache:{},_expandedDirs:new Set()};
function $(id){ return els[id] || null; }
function t(k){ return k; }
function _noteWorkspaceBirthtimeSupport(){}
function _syncWorkspaceBirthtimeSupportScope(){}
function _workspaceEntriesForRender(entries){ return entries; }
function _renderTreeItems(box,entries){ box.children.push(entries.length); }
function a11yTree(){}
function a11yTreeReset(){}
function a11yTreeRememberFocus(){ return null; }
function a11yTreeRestoreFocus(){}
"""

SCENARIO = r"""
function setup({previewOpen, workspace, entries}){
  els.fileTree=mk('fileTree'); els.fileTree.style.display=''; els.fileTree.children=[];
  els.wsEmptyState=mk('wsEmptyState'); els.wsEmptyState.style.display='none';
  els.previewArea=mk('previewArea');
  els.previewArea.classList._s=new Set(previewOpen?['visible']:[]);
  if(previewOpen) els.fileTree.style.display='none';   // what openFile() leaves behind
  S.session= workspace ? {session_id:'s1',workspace} : null;
  S.entries=entries;
  renderFileTree({});
  return {tree:els.fileTree.style.display, empty:els.wsEmptyState.style.display,
          rendered:els.fileTree.children.length};
}
const out={
  preview_open_rows: setup({previewOpen:true, workspace:'/ws/A', entries:[{name:'note.txt'}]}),
  no_preview_rows:   setup({previewOpen:false,workspace:'/ws/A', entries:[{name:'note.txt'}]}),
  preview_open_empty:setup({previewOpen:true, workspace:'/ws/A', entries:[]}),
  no_preview_empty:  setup({previewOpen:false,workspace:'/ws/A', entries:[]}),
  preview_open_no_ws:setup({previewOpen:true, workspace:null,   entries:[]}),
  no_preview_no_ws:  setup({previewOpen:false,workspace:null,   entries:[]}),
};
console.log(JSON.stringify(out));
"""


def _run():
    assert NODE, "node is required"
    script = (
        _function_block(UI_JS, "function renderFileTree(")
        + HARNESS
        + SCENARIO
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def rendered():
    return _run()


@NODE_SKIP
def test_tree_render_does_not_reopen_the_pane_over_an_open_preview(rendered):
    assert rendered["preview_open_rows"]["tree"] == "none", (
        "a render that lands while the preview is visible must not paint the tree back; "
        "got display=%r" % (rendered["preview_open_rows"]["tree"],)
    )
    assert rendered["preview_open_rows"]["rendered"] == 0, (
        "the tree rows must not be built at all while the preview owns the pane"
    )


@NODE_SKIP
def test_empty_state_is_hidden_while_the_preview_is_open(rendered):
    assert rendered["preview_open_empty"]["empty"] == "none", (
        "'this folder is empty' must not show beside the preview"
    )
    assert rendered["preview_open_empty"]["tree"] == "none"
    assert rendered["preview_open_no_ws"]["empty"] == "none", (
        "neither empty state may occupy the pane the preview is using"
    )


@NODE_SKIP
def test_tree_and_empty_states_are_unchanged_without_a_preview(rendered):
    assert rendered["no_preview_rows"]["tree"] == "", (
        "without a preview the tree must still be shown"
    )
    assert rendered["no_preview_rows"]["rendered"] == 1
    assert rendered["no_preview_empty"]["empty"] == "flex"
    assert rendered["no_preview_no_ws"]["empty"] == "flex"
    assert rendered["no_preview_no_ws"]["tree"] == "none"
