"""
Tests for symlink cycle detection in workspace file browser.

When a workspace contains symlinks (especially to directories outside the
workspace root), the directory listing must terminate without infinite
recursion.  Covers:

- External symlink dirs (e.g. ln -s /some/path ~/workspace/link)
- Self-referencing symlink (ln -s . ~/workspace/loop)
- Ancestor symlink (ln -s .. ~/workspace/up)
- Symlink entries carry correct type / is_dir / target fields
- Browsing into a symlink directory via workspace-relative path works
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
        body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, pathlib.Path(d["session"]["workspace"])


class TestSymlinkCycleDetection:
    """Symlink cycle detection in list_dir / safe_resolve_ws."""

    def test_external_symlink_listed_as_symlink(self, cleanup_test_sessions, tmp_path_factory):
        """External symlink dir should appear with type='symlink', is_dir=True."""
        ws = tmp_path_factory.mktemp("ws")
        target = tmp_path_factory.mktemp("target")
        (target / "file.txt").write_text("hello")
        link = ws / "ext"
        link.symlink_to(target)

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        entries = listing["entries"]
        ext = [e for e in entries if e["name"] == "ext"]
        assert len(ext) == 1
        assert ext[0]["type"] == "symlink"
        assert ext[0]["is_dir"] is True
        assert ext[0]["target"] == str(target)

    def test_external_symlink_browsable(self, cleanup_test_sessions, tmp_path_factory):
        """Listing inside an external symlink dir returns its contents."""
        ws = tmp_path_factory.mktemp("ws")
        target = tmp_path_factory.mktemp("target")
        (target / "inner.txt").write_text("data")
        (ws / "ext").symlink_to(target)

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=ext")
        entries = listing["entries"]
        names = [e["name"] for e in entries]
        assert "inner.txt" in names

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

    def test_symlink_cycle_in_subdir(self, cleanup_test_sessions, tmp_path_factory):
        """Symlink cycle inside a symlink target's subtree must not recurse."""
        ws = tmp_path_factory.mktemp("ws")
        target = tmp_path_factory.mktemp("target")
        (target / "subdir").mkdir()
        # Create a symlink inside target that points back to workspace
        (target / "subdir" / "back").symlink_to(ws)
        (ws / "ext").symlink_to(target)

        sid, _ = make_session(cleanup_test_sessions, ws)
        # List root — should show ext but not recurse
        listing = get(f"/api/list?session_id={sid}&path=.")
        names = [e["name"] for e in listing["entries"]]
        assert "ext" in names

        # List inside ext/subdir — 'back' should be filtered
        listing2 = get(f"/api/list?session_id={sid}&path=ext/subdir")
        names2 = [e["name"] for e in listing2["entries"]]
        assert "back" not in names2, "Cycle symlink inside external target should be filtered"

    def test_symlink_file_entry(self, cleanup_test_sessions, tmp_path_factory):
        """Symlink to a file should have is_dir=False and include size."""
        ws = tmp_path_factory.mktemp("ws")
        real = tmp_path_factory.mktemp("real")
        (real / "data.txt").write_text("hello world")
        (ws / "link.txt").symlink_to(real / "data.txt")

        sid, _ = make_session(cleanup_test_sessions, ws)
        listing = get(f"/api/list?session_id={sid}&path=.")
        link = [e for e in listing["entries"] if e["name"] == "link.txt"]
        assert len(link) == 1
        assert link[0]["type"] == "symlink"
        assert link[0]["is_dir"] is False
        assert link[0]["size"] == 11  # len("hello world")

    def test_path_traversal_still_blocked(self, cleanup_test_sessions, tmp_path_factory):
        """Raw .. traversal must still be blocked even with symlink support."""
        ws = tmp_path_factory.mktemp("ws")
        sid, _ = make_session(cleanup_test_sessions, ws)
        try:
            get(f"/api/list?session_id={sid}&path=../../../etc")
            assert False, "Path traversal should be blocked"
        except urllib.error.HTTPError as e:
            assert e.code in (400, 404, 500)
