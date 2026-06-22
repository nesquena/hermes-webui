"""
Tests for symlink cycle detection in workspace file browser.

When a workspace contains symlinks (especially to directories outside the
workspace root), the directory listing must terminate without infinite
recursion.  Covers:

- External symlink dirs (e.g. ln -s /some/path ~/workspace/link)
- Self-referencing symlink (ln -s . ~/workspace/loop)
- Ancestor symlink (ln -s .. ~/workspace/up)
- Internal symlink entries carry correct type / is_dir / target fields
- External symlink directories are emitted as display-only rows (target_outside_workspace=True)
  and cannot be traversed
"""
import json
import os
import pathlib
import urllib.request
import urllib.error
import tempfile

from tests._pytest_port import BASE


def get(path):
    url = BASE + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created_list, ws=None):
    body = {}
    if ws:
        # tmp_path_factory creates dirs under /var/folders or /tmp which sit
        # outside the user home tree, so they aren't trusted by default.
        # Register the workspace first via the explicit add API (intent-trusted)
        # before requesting a session against it.
        post("/api/workspaces/add", {"path": str(ws)})
        body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, pathlib.Path(d["session"]["workspace"])


class TestSymlinkCycleDetection:
    """Symlink cycle detection in list_dir / safe_resolve_ws."""

    def test_external_symlink_emitted_as_display_only(self, cleanup_test_sessions, tmp_path_factory):
        """External symlink dirs are emitted with target_outside_workspace=True (display-only)."""
        ws = tmp_path_factory.mktemp("ws")
        target = tmp_path_factory.mktemp("target")
        (target / "file.txt").write_text("hello")
        link = ws / "ext"
        link.symlink_to(target)

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        entries = {e["name"]: e for e in listing["entries"]}
        assert "ext" in entries
        assert entries["ext"]["type"] == "symlink"
        assert entries["ext"]["target_outside_workspace"] is True
        # #4581 hardening: display-only escape rows don't disclose target-derived
        # metadata (is_dir/target), so an external dir symlink reports is_dir=False.
        assert entries["ext"]["is_dir"] is False
        assert "target" not in entries["ext"]

    def test_internal_symlink_listed_as_symlink(self, cleanup_test_sessions, tmp_path_factory):
        """Internal symlink dirs should appear with type='symlink', is_dir=True."""
        ws = tmp_path_factory.mktemp("ws")
        target = ws / "target"
        target.mkdir()
        (target / "file.txt").write_text("hello")
        link = ws / "internal"
        link.symlink_to(target)

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        entries = listing["entries"]
        internal = [e for e in entries if e["name"] == "internal"]
        assert len(internal) == 1
        assert internal[0]["type"] == "symlink"
        assert internal[0]["is_dir"] is True
        assert internal[0]["target"] == str(target)

    def test_external_symlink_not_browsable(self, cleanup_test_sessions, tmp_path_factory):
        """Listing inside an external symlink dir is blocked at the workspace boundary."""
        ws = tmp_path_factory.mktemp("ws")
        target = tmp_path_factory.mktemp("target")
        (target / "inner.txt").write_text("data")
        (ws / "ext").symlink_to(target)

        sid, _ = make_session(cleanup_test_sessions, ws)
        try:
            get(f"/api/list?session_id={sid}&path=ext")
            assert False, "External symlink traversal should be blocked"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404, 500)

    def test_self_referencing_symlink_filtered(self, cleanup_test_sessions, tmp_path_factory):
        """Symlink pointing to the workspace root itself must be filtered out."""
        ws = tmp_path_factory.mktemp("ws")
        (ws / "file.txt").write_text("data")
        (ws / "loop").symlink_to(ws)

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        names = [e["name"] for e in listing["entries"]]
        assert "loop" not in names, "Self-referencing symlink should be filtered"

    def test_ancestor_symlink_filtered(self, cleanup_test_sessions, tmp_path_factory):
        """Symlink pointing to a parent of the workspace must be filtered out."""
        parent = tmp_path_factory.mktemp("parent")
        ws = parent / "workspace"
        ws.mkdir()
        (ws / "file.txt").write_text("data")
        # Symlink pointing to parent dir (ancestor of workspace)
        (ws / "up").symlink_to(parent)

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        names = [e["name"] for e in listing["entries"]]
        assert "up" not in names, "Ancestor symlink should be filtered"

    def test_mutual_symlink_loop_filtered(self, cleanup_test_sessions, tmp_path_factory):
        """Mutually recursive symlinks should be skipped instead of raising RuntimeError."""
        ws = tmp_path_factory.mktemp("ws")
        (ws / "a").symlink_to(ws / "b")
        (ws / "b").symlink_to(ws / "a")

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        names = [e["name"] for e in listing["entries"]]
        assert "a" not in names
        assert "b" not in names

    def test_symlink_cycle_in_subdir(self, cleanup_test_sessions, tmp_path_factory):
        """External symlink subpaths must be blocked instead of traversed."""
        ws = tmp_path_factory.mktemp("ws")
        target = tmp_path_factory.mktemp("target")
        (target / "subdir").mkdir()
        # Create a symlink inside target that points back to workspace
        (target / "subdir" / "back").symlink_to(ws)
        (ws / "ext").symlink_to(target)

        sid, _ = make_session(cleanup_test_sessions, ws)
        # List root — external symlink is emitted as display-only, not traversed.
        listing = get(f"/api/list?session_id={sid}&path=.")
        entries = {e["name"]: e for e in listing["entries"]}
        assert "ext" in entries
        assert entries["ext"]["target_outside_workspace"] is True

        # Traversing into ext/subdir crosses the workspace boundary and is blocked.
        try:
            get(f"/api/list?session_id={sid}&path=ext/subdir")
            assert False, "External symlink subpath traversal should be blocked"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404, 500)

    def test_internal_symlink_file_entry(self, cleanup_test_sessions, tmp_path_factory):
        """Internal symlink to a file should have is_dir=False and include size."""
        ws = tmp_path_factory.mktemp("ws")
        real = ws / "real"
        real.mkdir()
        (real / "data.txt").write_text("hello world")
        (ws / "link.txt").symlink_to(real / "data.txt")

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        link = [e for e in listing["entries"] if e["name"] == "link.txt"]
        assert len(link) == 1
        assert link[0]["type"] == "symlink"
        assert link[0]["is_dir"] is False
        assert link[0]["size"] == 11  # len("hello world")

    def test_external_symlink_file_emitted_as_display_only(self, cleanup_test_sessions, tmp_path_factory):
        """External symlink files are emitted with target_outside_workspace=True (display-only)."""
        ws = tmp_path_factory.mktemp("ws")
        real = tmp_path_factory.mktemp("real")
        (real / "data.txt").write_text("hello world")
        (ws / "link.txt").symlink_to(real / "data.txt")

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        entries = {e["name"]: e for e in listing["entries"]}
        assert "link.txt" in entries
        assert entries["link.txt"]["type"] == "symlink"
        assert entries["link.txt"]["target_outside_workspace"] is True
        assert entries["link.txt"]["is_dir"] is False

    def test_path_traversal_still_blocked(self, cleanup_test_sessions, tmp_path_factory):
        """Raw .. traversal must still be blocked even with symlink support."""
        ws = tmp_path_factory.mktemp("ws")
        sid, _ = make_session(cleanup_test_sessions, ws)
        try:
            get(f"/api/list?session_id={sid}&path=../../../etc")
            assert False, "Path traversal should be blocked"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404, 500)
