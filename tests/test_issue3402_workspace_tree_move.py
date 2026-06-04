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
        block = ROUTES[ROUTES.index("def _handle_file_move"):ROUTES.index("def _handle_file_move") + 4000]
        assert 'require(body, "session_id", "path", "dest_dir")' in block
        # The move performs a rename. As of the #3422 security hardening the
        # primary path uses a workspace-anchored os.rename(..., src_dir_fd=...,
        # dst_dir_fd=...) (TOCTOU-safe) with a path-based source.rename(dest)
        # fallback on platforms without dir_fd support — accept either.
        assert ("os.rename(" in block and "dst_dir_fd=" in block) or "source.rename(dest)" in block
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


def test_file_move_rejects_symlinked_dest_dir_escape():
    """Security (#3422 hardening): a dest_dir that is a symlink pointing OUTSIDE
    the workspace must not let the move escape — the workspace-anchored
    open_anchored_fd (openat + O_NOFOLLOW) rejects any symlinked path component,
    and the file must remain inside the workspace."""
    import os
    import tempfile
    from pathlib import Path
    from tests.test_sprint14 import make_session, post

    created = []
    outside = tempfile.mkdtemp()
    try:
        sid, sess = make_session(created)
        ws = Path(sess["workspace"])
        post("/api/file/create", {"session_id": sid, "path": "a.txt", "content": "secret"})
        # Plant a symlink inside the workspace pointing outside it.
        link = ws / "evil_link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            return  # platform without symlink support — nothing to test
        d, status = post("/api/file/move", {
            "session_id": sid,
            "path": "a.txt",
            "dest_dir": "evil_link",
        })
        assert status == 400, f"symlinked dest_dir escape must be rejected, got {status}"
        # The file must NOT have escaped into the outside dir.
        assert not os.path.exists(os.path.join(outside, "a.txt")), (
            "file escaped the workspace via a symlinked dest_dir"
        )
        assert (ws / "a.txt").exists(), "source file should be left in place on rejection"
    finally:
        import shutil
        shutil.rmtree(outside, ignore_errors=True)
        for s in created:
            post("/api/session/delete", {"session_id": s})


def test_file_move_succeeds_under_symlinked_workspace_root():
    """Regression (#3422 hardening follow-up): a move inside a workspace whose
    configured root is itself a symlink (e.g. macOS /tmp -> /private/tmp) must
    return ok with the correct relative new_path — the returned path is computed
    against ws_root.resolve(), so the post-rename relative_to() can't raise after
    a successful on-disk move. Driven at the handler level (the workspace-trust
    layer rejects an arbitrary symlinked path via the public /api/session/new)."""
    import os
    import tempfile
    from pathlib import Path
    import api.routes as routes

    real = tempfile.mkdtemp()
    link = real + "_link"
    try:
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            return
        os.mkdir(os.path.join(real, "dest"))
        Path(real, "a.txt").write_text("x")

        class _S:
            workspace = link  # configured root is the SYMLINK, not the real dir

        cap = {}
        orig_j, orig_bad, orig_get = routes.j, routes.bad, routes.get_session_for_file_ops
        routes.j = lambda h, o: (cap.__setitem__("ok", o), True)[1]
        routes.bad = lambda h, m, c=400: (cap.__setitem__("bad", (m, c)), True)[1]
        routes.get_session_for_file_ops = lambda sid: _S()
        try:
            routes._handle_file_move(
                type("H", (), {})(),
                {"session_id": "x", "path": "a.txt", "dest_dir": "dest"},
            )
        finally:
            routes.j, routes.bad, routes.get_session_for_file_ops = orig_j, orig_bad, orig_get
        assert "ok" in cap, f"move under symlinked workspace root must succeed, got {cap}"
        assert cap["ok"]["new_path"] == "dest/a.txt", cap["ok"]
        assert os.path.exists(os.path.join(real, "dest", "a.txt"))
    finally:
        import shutil
        try:
            os.unlink(link)
        except OSError:
            pass
        shutil.rmtree(real, ignore_errors=True)


def test_file_move_rejects_symlinked_source_entry():
    """Security (#3422 hardening, round 3): dragging a SYMLINK entry must not
    move the link's resolved target. safe_resolve() follows the final symlink,
    so without a guard moving link.txt (-> dir/real.txt) would move
    dir/real.txt and leave link.txt dangling. The handler rejects a symlinked
    source (lstat on the lexically-requested path) with 400, leaving both the
    link and its target untouched."""
    import os
    import tempfile
    from pathlib import Path
    import api.routes as routes

    real_ws = tempfile.mkdtemp()
    try:
        os.mkdir(os.path.join(real_ws, "dir"))
        os.mkdir(os.path.join(real_ws, "dest"))
        Path(real_ws, "dir", "real.txt").write_text("important")
        try:
            os.symlink(os.path.join(real_ws, "dir", "real.txt"), os.path.join(real_ws, "link.txt"))
        except (OSError, NotImplementedError):
            return

        class _S:
            workspace = real_ws

        cap = {}
        orig_j, orig_bad, orig_get = routes.j, routes.bad, routes.get_session_for_file_ops
        routes.j = lambda h, o: (cap.__setitem__("ok", o), True)[1]
        routes.bad = lambda h, m, c=400: (cap.__setitem__("bad", (m, c)), True)[1]
        routes.get_session_for_file_ops = lambda sid: _S()
        try:
            routes._handle_file_move(
                type("H", (), {})(),
                {"session_id": "x", "path": "link.txt", "dest_dir": "dest"},
            )
        finally:
            routes.j, routes.bad, routes.get_session_for_file_ops = orig_j, orig_bad, orig_get
        assert "bad" in cap, f"symlinked source must be rejected, got {cap}"
        # The real target must NOT have moved, and the link must remain.
        assert os.path.exists(os.path.join(real_ws, "dir", "real.txt")), "symlink target was moved!"
        assert os.path.islink(os.path.join(real_ws, "link.txt")), "symlink source was removed!"
        assert not os.path.exists(os.path.join(real_ws, "dest", "real.txt"))
    finally:
        import shutil
        shutil.rmtree(real_ws, ignore_errors=True)


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
