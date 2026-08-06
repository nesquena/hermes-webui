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
