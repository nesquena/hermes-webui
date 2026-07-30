"""Regression tests for #4217 — symlink guards on /api/file/delete and /api/file/rename."""
import os

import pytest

import api.routes as routes


@pytest.fixture()
def workspace(tmp_path):
    """Yield a real temporary workspace directory and clean up after."""
    ws = tmp_path / "ws"
    ws.mkdir()
    yield ws


class _FakeHandler:
    pass


def _patch_routes(ws):
    """Monkey-patch routes helpers so handlers run without an HTTP server."""

    class _S:
        workspace = str(ws)

    cap = {}
    orig_j = routes.j
    orig_bad = routes.bad
    orig_get = routes.get_session_for_file_ops
    routes.j = lambda h, o: (cap.__setitem__("ok", o), True)[1]
    routes.bad = lambda h, m, c=400: (cap.__setitem__("bad", (m, c)), True)[1]
    routes.get_session_for_file_ops = lambda sid: _S()
    return cap, (orig_j, orig_bad, orig_get)


def _restore_routes(originals):
    routes.j, routes.bad, routes.get_session_for_file_ops = originals


# ── DELETE symlink guards ────────────────────────────────────────────────

def test_delete_workspace_symlink_to_dir_rejected(workspace):
    real_dir = workspace / "real_dir"
    real_dir.mkdir()
    link = workspace / "link_to_dir"
    try:
        os.symlink(str(real_dir), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_delete(
            _FakeHandler(),
            {"session_id": "x", "path": "link_to_dir", "recursive": True},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot delete a symlinked entry" in cap["bad"][0]
    assert real_dir.exists(), "real target directory was deleted through the symlink"


def test_delete_workspace_symlink_to_file_rejected(workspace):
    real_file = workspace / "real_file.txt"
    real_file.write_text("important")
    link = workspace / "link_to_file.txt"
    try:
        os.symlink(str(real_file), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_delete(
            _FakeHandler(),
            {"session_id": "x", "path": "link_to_file.txt"},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot delete a symlinked entry" in cap["bad"][0]
    assert real_file.exists(), "real target file was deleted through the symlink"


# ── RENAME symlink guards ───────────────────────────────────────────────

def test_rename_workspace_symlink_to_dir_rejected(workspace):
    real_dir = workspace / "real_dir"
    real_dir.mkdir()
    link = workspace / "link_to_dir"
    try:
        os.symlink(str(real_dir), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_rename(
            _FakeHandler(),
            {"session_id": "x", "path": "link_to_dir", "new_name": "renamed"},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot rename a symlinked entry" in cap["bad"][0]


def test_rename_workspace_symlink_to_file_rejected(workspace):
    real_file = workspace / "real_file.txt"
    real_file.write_text("important")
    link = workspace / "link_to_file.txt"
    try:
        os.symlink(str(real_file), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_rename(
            _FakeHandler(),
            {"session_id": "x", "path": "link_to_file.txt", "new_name": "renamed.txt"},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot rename a symlinked entry" in cap["bad"][0]


# ── Non-symlink operations still work ───────────────────────────────────

def test_save_workspace_symlink_to_file_rejected(workspace):
    real_file = workspace / "real_file.txt"
    real_file.write_text("important")
    link = workspace / "link_to_file.txt"
    try:
        os.symlink(str(real_file), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_save(
            _FakeHandler(),
            {"session_id": "x", "path": "link_to_file.txt", "content": "changed"},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot save to a symlinked entry" in cap["bad"][0]
    assert real_file.read_text() == "important"


def test_save_dangling_symlink_rejected_not_404(workspace):
    link = workspace / "dangling_save"
    try:
        os.symlink(str(workspace / "gone_target"), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_save(
            _FakeHandler(),
            {"session_id": "x", "path": "dangling_save", "content": "changed"},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot save to a symlinked entry" in cap["bad"][0]


def test_delete_real_dir_still_works(workspace):
    real_dir = workspace / "deleteme"
    real_dir.mkdir()
    (real_dir / "child.txt").write_text("x")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_delete(
            _FakeHandler(),
            {"session_id": "x", "path": "deleteme", "recursive": True},
        )
    finally:
        _restore_routes(orig)

    assert "ok" in cap, f"expected success, got {cap}"
    assert cap["ok"]["ok"] is True
    assert not real_dir.exists()


def test_rename_real_file_still_works(workspace):
    real_file = workspace / "old_name.txt"
    real_file.write_text("content")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_rename(
            _FakeHandler(),
            {"session_id": "x", "path": "old_name.txt", "new_name": "new_name.txt"},
        )
    finally:
        _restore_routes(orig)

    assert "ok" in cap, f"expected success, got {cap}"
    assert cap["ok"]["ok"] is True
    assert not real_file.exists()
    assert (workspace / "new_name.txt").exists()


# ── Existing move guard pinned ──────────────────────────────────────────

def test_save_real_file_still_works(workspace):
    real_file = workspace / "save_me.txt"
    real_file.write_text("old")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_save(
            _FakeHandler(),
            {"session_id": "x", "path": "save_me.txt", "content": "new"},
        )
    finally:
        _restore_routes(orig)

    assert "ok" in cap, f"expected success, got {cap}"
    assert cap["ok"]["ok"] is True
    assert cap["ok"]["size"] == len("new")
    assert real_file.read_text() == "new"


def test_move_workspace_symlink_still_rejected(workspace):
    real_file = workspace / "real.txt"
    real_file.write_text("data")
    dest = workspace / "dest"
    dest.mkdir()
    link = workspace / "link.txt"
    try:
        os.symlink(str(real_file), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_move(
            _FakeHandler(),
            {"session_id": "x", "path": "link.txt", "dest_dir": "dest"},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot move a symlinked entry" in cap["bad"][0]


# ── Dangling symlinks: guard must fire BEFORE the exists() 404 (#4230 review) ──

def test_delete_dangling_symlink_rejected_not_404(workspace):
    """A dangling workspace symlink (target removed) must be rejected by the
    symlink guard (400), not misclassified as 404 'File not found' and left
    permanently undeletable. The is_symlink() check must precede exists()."""
    link = workspace / "dangling_del"
    try:
        os.symlink(str(workspace / "gone_target"), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_delete(
            _FakeHandler(),
            {"session_id": "x", "path": "dangling_del", "recursive": True},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot delete a symlinked entry" in cap["bad"][0]


def test_rename_dangling_symlink_rejected_not_404(workspace):
    """A dangling workspace symlink must be rejected by the rename guard (400),
    not 404'd before the guard fires."""
    link = workspace / "dangling_ren"
    try:
        os.symlink(str(workspace / "gone_target"), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    cap, orig = _patch_routes(workspace)
    try:
        routes._handle_file_rename(
            _FakeHandler(),
            {"session_id": "x", "path": "dangling_ren", "new_name": "renamed"},
        )
    finally:
        _restore_routes(orig)

    assert "bad" in cap, f"expected 400, got {cap}"
    assert cap["bad"][1] == 400
    assert "Cannot rename a symlinked entry" in cap["bad"][0]
