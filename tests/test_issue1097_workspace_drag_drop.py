"""Tests for #1097 — drag & drop workspace files into chat composer."""

import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


class TestWorkspaceDragDrop:
    """File tree items are draggable and composer accepts workspace drops."""

    def test_renderTreeItems_makes_items_draggable(self):
        """Each file-item must have draggable='true'."""
        src = _src("ui.js")
        assert "el.setAttribute('draggable','true')" in src, (
            "_renderTreeItems must set draggable=true on each item"
        )

    def test_dragstart_stores_ws_path(self):
        """dragstart must store 'application/ws-path' with item.path."""
        src = _src("ui.js")
        assert "application/ws-path" in src, (
            "dragstart must setData with application/ws-path"
        )
        assert "item.path" in src, "dragstart must include item.path in data transfer"

    def test_dragstart_stores_ws_type(self):
        """dragstart must store 'application/ws-type' (file or dir)."""
        src = _src("ui.js")
        assert "application/ws-type" in src, (
            "dragstart must setData with application/ws-type"
        )

    def test_dragstart_effectAllowed_copy(self):
        """Drag effect must be 'copy' (not move — we insert a reference)."""
        src = _src("ui.js")
        assert "effectAllowed='copy'" in src, (
            "dragstart must set effectAllowed to 'copy'"
        )

    def test_drop_handler_checks_ws_path(self):
        """Global drop handler must check for application/ws-path first."""
        src = _src("panels.js")
        m = re.search(r"document\.addEventListener\('drop'", src)
        assert m, "Global drop listener must exist"
        after = src[m.start() : m.start() + 2000]
        assert "application/ws-path" in after, (
            "Drop handler must check for workspace path data"
        )

    def test_workspace_drop_inserts_at_path(self):
        """Workspace drop must insert @path into composer textarea."""
        src = _src("panels.js")
        m = re.search(r"document\.addEventListener\('drop'", src)
        after = src[m.start() : m.start() + 2000]
        # Must insert @-prefixed path
        assert "'@'+wsPath" in after or '"@"+wsPath' in after or "@" + "" in after, (
            "Workspace drop must insert @-prefixed path into composer"
        )
        # Must position cursor after insert
        assert "selectionStart" in after, "Drop handler must update cursor position"
        # Must focus composer
        assert "msgEl.focus()" in after or "$('msg').focus()" in after, (
            "Drop handler must focus the composer textarea"
        )

    def test_workspace_drop_has_prefix_logic(self):
        """Workspace drop should add space prefix if cursor is mid-word."""
        src = _src("panels.js")
        m = re.search(r"document\.addEventListener\('drop'", src)
        after = src[m.start() : m.start() + 2000]
        assert "prefix" in after.lower(), (
            "Drop handler should handle spacing between existing text and @path"
        )

    def test_dragenter_accepts_ws_path(self):
        """dragenter must highlight composer for workspace drags too."""
        src = _src("panels.js")
        # Find dragenter listener
        m = re.search(r"document\.addEventListener\('dragenter'", src)
        assert m, "dragenter listener must exist"
        after = src[m.start() : m.start() + 300]
        assert "application/ws-path" in after, (
            "dragenter must also trigger for workspace drags (application/ws-path)"
        )

    def test_os_file_drop_still_works(self):
        """OS file drag (dataTransfer.files) must still attach files."""
        src = _src("panels.js")
        m = re.search(r"document\.addEventListener\('drop'", src)
        after = src[m.start() : m.start() + 2000]
        assert "addFiles(files)" in after, (
            "OS file drop path (addFiles) must still work after workspace drop addition"
        )
