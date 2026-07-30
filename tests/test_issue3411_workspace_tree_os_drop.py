"""Tests for #3411 — OS file drop on workspace tree must not attach to composer."""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


def _tree_os_drop_block(src: str) -> str:
    """Extract the file-tree OS upload DnD init block in workspace.js."""
    m = re.search(r"tree\.addEventListener\('drop', async \(e\) => \{", src)
    assert m, "fileTree drop handler must exist in workspace.js"
    return src[m.start():m.start() + 1200]


class TestIssue3411WorkspaceTreeOsDrop:
    """OS Files drops on #fileTree must not bubble to the composer handler."""

    def test_tree_drop_stops_propagation_for_os_files(self):
        block = _tree_os_drop_block(_src("workspace.js"))
        assert "e.stopPropagation()" in block, (
            "fileTree drop must stopPropagation so document drop does not call addFiles"
        )
        prevent = block.index("e.preventDefault()")
        stop = block.index("e.stopPropagation()")
        assert prevent < stop, "stopPropagation must run after preventDefault on OS file drop"

    def test_tree_dragover_stops_propagation_for_os_files(self):
        src = _src("workspace.js")
        m = re.search(r"tree\.addEventListener\('dragover', \(e\) => \{", src)
        assert m, "fileTree dragover handler must exist in workspace.js"
        block = src[m.start():m.start() + 400]
        assert "e.stopPropagation()" in block, (
            "fileTree dragover must stopPropagation so composer drag-over does not highlight"
        )

    def test_tree_dragenter_stops_propagation_for_os_files(self):
        src = _src("workspace.js")
        m = re.search(r"tree\.addEventListener\('dragenter', \(e\) => \{", src)
        assert m, "fileTree dragenter handler must exist in workspace.js"
        block = src[m.start():m.start() + 300]
        assert "e.stopPropagation()" in block, (
            "fileTree dragenter must stopPropagation so composer drag-over does not highlight"
        )

    def test_composer_os_drop_still_works_outside_tree(self):
        """Document-level addFiles path must remain for drops outside the tree."""
        src = _src("panels.js")
        m = re.search(r"document\.addEventListener\('drop'", src)
        assert m, "Global drop listener must exist"
        after = src[m.start():m.start() + 2000]
        assert "addFiles(files)" in after, (
            "Composer must still attach OS files dropped outside the workspace tree"
        )
