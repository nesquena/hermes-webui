"""Tests for #3402 part B — OS file/folder import into workspace tree targets."""
import json
import shutil
import subprocess


def _src(name: str) -> str:
    with open(f"static/{name}", encoding="utf-8") as f:
        return f.read()


WORKSPACE_JS = _src("workspace.js")
UI_JS = _src("ui.js")


class TestIssue3402WorkspaceOsImportUi:
    def test_folder_rows_bind_os_upload_drop(self):
        assert "_bindWorkspaceOsUploadDropTarget(el,item.path)" in UI_JS

    def test_breadcrumb_binds_os_upload_drop(self):
        assert "_bindWorkspaceOsUploadDropTarget(root,'.')" in UI_JS
        assert "_bindWorkspaceOsUploadDropTarget(seg,target)" in UI_JS

    def test_os_upload_helpers_exist(self):
        assert "function uploadOsDropToWorkspace" in WORKSPACE_JS
        assert "function _collectOsDropUploads" in WORKSPACE_JS
        assert "webkitGetAsEntry" in WORKSPACE_JS

    def test_os_folder_drop_stops_propagation(self):
        block = WORKSPACE_JS[
            WORKSPACE_JS.index("function _bindWorkspaceOsUploadDropTarget"):
            WORKSPACE_JS.index("// Drag-and-drop files onto workspace file tree")
        ]
        assert block.count("e.stopPropagation()") >= 3

    def test_tree_drop_skips_folder_rows(self):
        assert 'closest(\'.file-item[data-ws-type="dir"]' in WORKSPACE_JS

    def test_file_items_expose_ws_type_dataset(self):
        assert "el.dataset.wsType=item.type" in UI_JS

    def test_os_upload_highlight_css(self):
        css = open("static/style.css", encoding="utf-8").read()
        assert ".file-item.drag-over-upload" in css
        assert ".breadcrumb-seg.drag-over-upload" in css


def test_join_workspace_path_node():
    node = shutil.which("node")
    if not node:
        return
    js = r"""
const { joinWorkspacePath, targetDirForRelDir } = (() => {
  function joinWorkspacePath(base, rel) {
    const b = base || '.';
    const r = (rel || '').replace(/^\/+|\/+$/g, '');
    if (!r) return b;
    return b === '.' ? r : `${b}/${r}`;
  }
  function targetDirForRelDir(destDir, relDir) {
    const dirPart = (relDir || '').replace(/\/+$/, '');
    if (!dirPart) return destDir || '.';
    return joinWorkspacePath(destDir, dirPart);
  }
  return { joinWorkspacePath, targetDirForRelDir };
})();

const cases = [
  [joinWorkspacePath('.', ''), '.'],
  [joinWorkspacePath('docs', ''), 'docs'],
  [joinWorkspacePath('.', 'docs/reports'), 'docs/reports'],
  [joinWorkspacePath('src', 'lib/utils'), 'src/lib/utils'],
  [targetDirForRelDir('projects', ''), 'projects'],
  [targetDirForRelDir('projects', 'bundle/'), 'projects/bundle'],
  [targetDirForRelDir('.', 'bundle/sub/'), 'bundle/sub'],
];
console.log(JSON.stringify(cases.map(([a,b]) => b)));
"""
    out = subprocess.check_output([node, "-e", js], text=True).strip()
    assert json.loads(out) == [
        ".", "docs", "docs/reports", "src/lib/utils",
        "projects", "projects/bundle", "bundle/sub",
    ]


def test_os_drop_snapshots_entries_before_async_traversal():
    node = shutil.which("node")
    if not node:
        return
    js = r"""
const fs=require('fs'),vm=require('vm');
const src=fs.readFileSync('static/workspace.js','utf8');
const block=src.slice(
  src.indexOf('async function _readAllDirectoryEntries'),
  src.indexOf('function _clearWorkspaceOsUploadDragOver'));
const uploaded=[],acquired=[],atTraversal=[];
const ctx={
  S:{session:true,currentDir:'.'},_workspacePathIsReadOnly:()=>false,
  showToast(){},t:x=>x,_targetDirForRelDir:(d,r)=>r||d,
  uploadToWorkspace:async f=>uploaded.push(f.name),loadDir:async()=>{}};
vm.createContext(ctx);vm.runInContext(block+';this.collect=_collectOsDropUploads;this.upload=uploadOsDropToWorkspace',ctx);
let locked=false;
const fileEntry=name=>({isFile:true,file:resolve=>queueMicrotask(()=>{atTraversal.push(acquired.length);locked=true;resolve({name})})});
const names=['alpha.txt','beta.txt','gamma.txt'];
const items=names.map(name=>({kind:'file',webkitGetAsEntry(){acquired.push(name);return locked?null:fileEntry(name)}}));
(async()=>{
  await ctx.upload({items,files:[]},'.');
  let reads=0;const nested={name:'nested',isDirectory:true,createReader:()=>({readEntries:resolve=>queueMicrotask(()=>resolve(reads++?[]:[fileEntry('inside.txt')]))})};
  const nestedFiles=await ctx.collect({items:[{kind:'file',getAsEntry:()=>nested}],files:[]});
  const fallback=await ctx.collect({items:[{kind:'file',getAsEntry:()=>null}],files:[{name:'fallback.txt'}]});
  console.log(JSON.stringify([acquired,atTraversal.slice(0,3),uploaded,nestedFiles.map(x=>[x.file.name,x.relDir]),fallback.map(x=>x.file.name)]));
})().catch(error=>{console.error(error);process.exit(1)});
"""
    out = subprocess.check_output([node, "-e", js], text=True).strip()
    assert json.loads(out) == [
        ["alpha.txt", "beta.txt", "gamma.txt"], [3, 3, 3],
        ["alpha.txt", "beta.txt", "gamma.txt"],
        [["inside.txt", "nested/"]], ["fallback.txt"],
    ]
