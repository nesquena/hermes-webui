"""Tests for #3402 part A — move files/folders within the workspace tree."""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


ROUTES = open("api/routes.py", encoding="utf-8").read()


class TestIssue3402WorkspaceTreeMoveApi:
    def test_file_move_route_registered(self):
        assert 'parsed.path == "/api/file/move"' in ROUTES
        assert "return _handle_file_move(handler, body)" in ROUTES

    def test_file_move_handler_requires_dest_dir(self):
        block = ROUTES[ROUTES.index("def _handle_file_move"):ROUTES.index("def _handle_file_move") + 2200]
        assert 'require(body, "session_id", "path", "dest_dir")' in block
        assert "source.rename(dest)" in block
        assert "Cannot move a folder into itself or its subfolder" in block


def test_file_move():
    """Moving a file into another folder changes its path on disk."""
    from tests.test_sprint14 import make_session, post

    created = []
    try:
        sid, _sess = make_session(created)
        post("/api/file/create-dir", {"session_id": sid, "path": "subdir"})
        post("/api/file/create", {"session_id": sid, "path": "note.txt", "content": "hello"})
        d, status = post("/api/file/move", {
            "session_id": sid,
            "path": "note.txt",
            "dest_dir": "subdir",
        })
        assert status == 200
        assert d["ok"] is True
        assert d["new_path"] == "subdir/note.txt"
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_file_move_rejects_folder_into_itself():
    from tests.test_sprint14 import make_session, post

    created = []
    try:
        sid, _sess = make_session(created)
        post("/api/file/create-dir", {"session_id": sid, "path": "parent"})
        d, status = post("/api/file/move", {
            "session_id": sid,
            "path": "parent",
            "dest_dir": "parent",
        })
        assert status == 400
        assert "subfolder" in d.get("error", "").lower() or "itself" in d.get("error", "").lower()
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_file_move_rejects_existing_target():
    from tests.test_sprint14 import make_session, post

    created = []
    try:
        sid, _sess = make_session(created)
        post("/api/file/create-dir", {"session_id": sid, "path": "dest"})
        post("/api/file/create", {"session_id": sid, "path": "a.txt", "content": "a"})
        post("/api/file/create", {"session_id": sid, "path": "dest/a.txt", "content": "b"})
        d, status = post("/api/file/move", {
            "session_id": sid,
            "path": "a.txt",
            "dest_dir": "dest",
        })
        assert status == 400
    finally:
        for s in created:
            post("/api/session/delete", {"session_id": s})


class TestIssue3402WorkspaceTreeMoveUi:
    def test_render_tree_items_bind_move_drop_on_dirs(self):
        src = _src("ui.js")
        assert "_bindWorkspaceMoveDropTarget(el,item.path)" in src

    def test_move_drop_stops_propagation(self):
        src = _src("ui.js")
        block = src[src.index("function _bindWorkspaceMoveDropTarget"):src.index("function _renderTreeItems")]
        assert block.count("e.stopPropagation()") >= 3

    def test_move_calls_file_move_api(self):
        src = _src("ui.js")
        assert "await api('/api/file/move'" in src

    def test_composer_ws_path_drag_still_copy(self):
        src = _src("ui.js")
        m = re.search(r"el\.ondragstart=\(e\)=>\{[^}]+\}", src)
        assert m
        assert "effectAllowed='copy'" in m.group(0)

    def test_move_drop_css_classes_exist(self):
        css = open("static/style.css", encoding="utf-8").read()
        assert ".file-item.dragging" in css
        assert ".file-item.drag-over" in css
