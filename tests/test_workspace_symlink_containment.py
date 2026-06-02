import pytest

from api.workspace import list_dir, read_file_content, safe_resolve_ws


def test_safe_resolve_blocks_external_symlink_directory(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)

    with pytest.raises(ValueError, match="Path traversal blocked"):
        safe_resolve_ws(workspace, "escape")

    with pytest.raises(ValueError, match="Path traversal blocked"):
        list_dir(workspace, "escape")


def test_read_file_blocks_external_symlink_file(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "secret-link.txt").symlink_to(outside / "secret.txt")

    with pytest.raises(ValueError, match="Path traversal blocked"):
        read_file_content(workspace, "secret-link.txt")


def test_internal_symlink_still_resolves_within_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "inside-link.txt").symlink_to(nested / "inside.txt")

    resolved = safe_resolve_ws(workspace, "inside-link.txt")

    assert resolved == (nested / "inside.txt").resolve()
    assert read_file_content(workspace, "inside-link.txt")["content"] == "inside"
